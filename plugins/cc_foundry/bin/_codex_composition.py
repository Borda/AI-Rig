"""Measure token-only context composition in Codex rollout files.

Purpose: Extend the foundry cost analyzer with observable Codex context overhead. Scope: Read session metadata, per-
response usage, injected messages, tool results     and compaction markers; parent links keep each thread's windows
independent. Usage: Imported by _context_composition.run_composition after its shared window     types initialize. The
CLI supplies the analyzer module, avoiding a second     cost_analyzer import when its entrypoint runs as __main__.
Outputs: Markdown window measurements and family-wide subagent token shares,     attempted code edits and AGENTS.md byte
exposure estimates. No prices apply. Failure: Missing selections or usable usage return 1; ambiguous prefixes and
duplicate thread identities fail with diagnostics. Malformed rows and usage     warn on stderr; filesystem errors
propagate. No rollout is modified. Used by: cost_analyzer.py --runtime codex and composition regression tests.

Codex input already includes cached tokens; reasoning is part of output. New rollouts dedupe by response_id. Legacy
token-count events use sequential cumulative usage signatures, not content hashes or turn ids. Opaque base prompts and
retained summaries cannot be allocated exactly; tool tokens use bytes / 4.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _context_composition import ObservedCall, TranscriptState, Window


# Match only recorded injection wrappers; arbitrary XML in user text stays other.
INJECTION_TAGS = re.compile(
    r"<(?P<tag>INSTRUCTIONS|skills_instructions|model_switch|multi_agent_[\w]+|hook[\w]*)(?:\s[^>]*)?>"
    r"(?P<text>.*?)</(?P=tag)>",
    re.DOTALL,
)
CODE_SUFFIXES = r"(?:py|pyi|js|jsx|ts|tsx|c|cc|cpp|h|hpp|rs|go|java|sh|rb|cs|swift|kt)"
CODE_WRITE = re.compile(
    rf"(?:>{{1,2}}\s*|(?:Set-Content|Out-File)\s+(?:-Path\s+)?)[\"']?[^\s\"'<>]+\.{CODE_SUFFIXES}\b",
    re.IGNORECASE,
)


@dataclass
class CodexThread:
    """Keep identity and measurements for one independently deduplicated rollout."""

    session_id: str
    parent_id: str | None
    windows: list[Window]
    edited_code: bool = False
    usage_source: str = "response_id"
    notes: list[str] = field(default_factory=list)

    @property
    def calls(self) -> list[ObservedCall]:
        """Flatten calls for family totals without joining context windows."""
        return [item for window in self.windows for item in window.calls]


@dataclass
class CodexState:
    """Track runtime-specific evidence alongside the shared window measurements."""

    shared: ModuleType
    context: TranscriptState
    model: str = "unknown"
    edited_code: bool = False
    previous_total: str | None = None
    tool_outputs: set[str] = field(default_factory=set)
    notes: set[str] = field(default_factory=set)


def default_root() -> Path:
    """Locate sessions using Codex's configurable home without assuming host separators."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def _identity(rows: list[dict], path: Path) -> tuple[str, str | None]:
    """Read thread identity and the verified spawn-parent link from session metadata."""
    meta = next((row.get("payload", {}) for row in rows if row.get("type") == "session_meta"), {})
    source = meta.get("source")
    parent = None
    subagent = source.get("subagent") if isinstance(source, dict) else None
    if isinstance(subagent, dict):
        parent = subagent.get("thread_spawn", {}).get("parent_thread_id")
    return str(meta.get("id") or meta.get("session_id") or path.stem), parent


def _valid_usage(usage: object, limit: int) -> bool:
    """Require bounded integer counters and prevent cached or reasoning tokens exceeding their totals."""
    if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
        return False
    counters = [
        usage.get(key, 0) for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    ]
    if any(type(value) is not int or not 0 <= value <= limit for value in counters):
        return False
    incoming, cached, outgoing, reasoning = counters
    return cached <= incoming and reasoning <= outgoing


def _usage_sample(row: dict, state: CodexState, authoritative: bool, limit: int) -> tuple[str, dict] | None:
    """Select exact response usage or deduplicated legacy last-response observations."""
    payload = row.get("payload", {})
    if row.get("type") == "token_usage_record":
        key, usage = payload.get("response_id"), payload.get("usage")
    elif not authoritative and row.get("type") == "event_msg" and payload.get("type") == "token_count":
        info = payload.get("info") or {}
        usage = info.get("last_token_usage")
        total = info.get("total_token_usage")
        if not isinstance(total, dict) or not total:
            state.notes.add("Legacy events without cumulative usage keys were skipped.")
            return None
        signature = json.dumps(total, sort_keys=True)
        if signature == state.previous_total:
            return None
        state.previous_total = signature
        key = f"legacy-{len(state.context.calls) + 1}"
    else:
        return None
    if not key or not _valid_usage(usage, limit):
        state.notes.add("Usage without a response key or valid last-response counters was skipped.")
        return None
    return str(key), usage


def _observe_usage(row: dict, state: CodexState, analyzer: ModuleType, explicit: bool, authoritative: bool) -> None:
    """Normalize Codex tokens and record each deduplicated call in its context window."""
    sample = _usage_sample(row, state, authoritative, analyzer.MAX_TOKEN_COUNT)
    if sample is None:
        return
    key, usage = sample
    context = state.context
    if key in context.calls:
        return
    normalized = {
        "input_tokens": usage["input_tokens"] - usage.get("cached_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
        "output_tokens": usage["output_tokens"],
    }
    call = analyzer.Call(key, state.model, normalized)
    context.calls[key] = call
    tokens = usage["input_tokens"] + usage["output_tokens"]
    if not explicit and context.previous and tokens < context.previous.context * state.shared.CONTEXT_DROP_RATIO:
        state.shared._new_window(context, "70% context drop")
    current = state.shared.ObservedCall(call, state.shared._timestamp(row, analyzer), tokens, 0)
    window = context.windows[-1]
    window.calls.append(current)
    if usage["input_tokens"] > state.shared.LARGE_WRITE_TOKENS:
        trigger = "other"
        if context.compacted:
            trigger = "compaction"
        elif context.previous and context.previous.call.model != current.call.model:
            trigger = "model change"
        window.large_writes.append((len(context.calls), usage["input_tokens"], trigger))
    context.previous = current
    context.compacted = False


def _observe_message(payload: dict, state: CodexState) -> None:
    """Partition input message text into tagged injection payloads and other bytes."""
    if payload.get("role") not in {"user", "developer", "system"}:
        return
    window = state.context.windows[-1]
    text = state.shared._payload_bytes(payload.get("content")).decode("utf-8")
    cursor = 0
    for match in INJECTION_TAGS.finditer(text):
        tag = match["tag"]
        # INSTRUCTIONS is a project injection only when the native AGENTS heading identifies it.
        if tag == "INSTRUCTIONS" and "AGENTS.md" not in text[: match.start()]:
            continue
        kind = "instructions" if tag == "INSTRUCTIONS" else tag
        if kind.startswith("hook"):
            kind = "hook_developer" if payload.get("role") == "developer" else "other"
        size = len(match["text"].encode("utf-8"))
        window.attachments[kind] = window.attachments.get(kind, 0) + size
        prefix = text[cursor : match.start()]
        if tag == "INSTRUCTIONS":
            prefix = re.sub(r"(?m)^# AGENTS\.md instructions[^\n]*\n?", "", prefix)
        other = len(prefix.encode("utf-8"))
        if other:
            window.attachments["other"] = window.attachments.get("other", 0) + other
        cursor = match.end()
    remainder = len(text[cursor:].encode("utf-8"))
    if remainder:
        window.attachments["other"] = window.attachments.get("other", 0) + remainder


def _is_code_edit(payload: dict) -> bool:
    """Recognize attempted patches and explicit writes to code files, not filename mentions."""
    name = str(payload.get("name", "")).rsplit(".", 1)[-1]
    if name == "apply_patch":
        return True
    if name not in {"exec", "exec_command", "js", "write_file", "Write", "edit_file", "Edit"}:
        return False
    arguments = payload.get("arguments", payload.get("input", ""))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return bool(CODE_WRITE.search(arguments) or re.search(r"\b(?:tools\.)?apply_patch\s*\(", arguments))
    if not isinstance(arguments, dict):
        return False
    if name in {"write_file", "Write", "edit_file", "Edit"}:
        path = str(arguments.get("path") or arguments.get("file_path") or "")
        return bool(re.search(rf"\.{CODE_SUFFIXES}$", path, re.IGNORECASE))
    command = arguments.get("cmd", arguments.get("command", ""))
    return isinstance(command, str) and bool(CODE_WRITE.search(command) or "*** Begin Patch" in command)


def _observe_item(payload: dict, state: CodexState) -> None:
    """Measure native input messages and tool results once while flagging edit attempts."""
    kind = payload.get("type")
    if kind == "message":
        _observe_message(payload, state)
    elif kind in {"function_call", "custom_tool_call"}:
        state.edited_code |= _is_code_edit(payload)
    elif kind in {"function_call_output", "custom_tool_call_output"}:
        key = payload.get("call_id") or payload.get("id")
        if key and key in state.tool_outputs:
            return
        if key:
            state.tool_outputs.add(key)
        state.shared._observe_tool_result({"content": payload.get("output")}, state.context)


def analyze_transcript(path: Path, analyzer: ModuleType) -> CodexThread:
    """Measure a rollout independently, preferring native per-response records over mirrored events."""
    # Foundry loads private modules alphabetically; shared types exist only at analysis time.
    import _context_composition as shared

    rows = [row for _, row in shared._read_numbered_rows(path, analyzer)]
    session_id, parent_id = _identity(rows, path)
    authoritative = any(row.get("type") == "token_usage_record" for row in rows)
    explicit = any(row.get("type") == "compacted" for row in rows)
    state = CodexState(shared, shared.TranscriptState())
    # Rows were validated whole; an exception here is a defect and stays visible instead of leaving partial state.
    for row in rows:
        kind, payload = row.get("type"), row.get("payload", {})
        if kind == "turn_context":
            state.model = payload.get("model") or state.model
        elif kind == "compacted":
            shared._new_window(state.context, "compaction record")
        elif kind == "response_item":
            _observe_item(payload, state)
        _observe_usage(row, state, analyzer, explicit, authoritative)
    windows = [window for window in state.context.windows if window.calls]
    for note in sorted(state.notes):
        sys.stderr.write(f"warning: {path}: {note}\n")
    source = "response_id" if authoritative else "sequential cumulative total_token_usage"
    return CodexThread(session_id, parent_id, windows, state.edited_code, source, sorted(state.notes))


def _family_key(thread: CodexThread, threads: dict[str, CodexThread]) -> str:
    """Follow recorded ancestors, preserving sibling families even when the parent is absent."""
    key = thread.session_id
    visited = set()
    while key in threads and key not in visited:
        visited.add(key)
        parent = threads[key].parent_id
        if not parent:
            return key
        key = parent
    return min(visited) if key in visited else key


def _discover(root: Path, analyzer: ModuleType) -> dict[str, list[CodexThread]]:
    """Group recursively discovered rollouts by metadata family and reject duplicate identities."""
    paths = [root] if root.is_file() else sorted(root.rglob("rollout-*.jsonl"))
    threads: dict[str, CodexThread] = {}
    for path in paths:
        thread = analyze_transcript(path, analyzer)
        if thread.session_id in threads:
            raise ValueError(f"duplicate Codex thread identity {thread.session_id!r} under {root}")
        threads[thread.session_id] = thread
    families: dict[str, list[CodexThread]] = {}
    for thread in threads.values():
        key = _family_key(thread, threads)
        families.setdefault(key, []).append(thread)
    return families


def _select(families: dict[str, list[CodexThread]], args: argparse.Namespace, analyzer: ModuleType) -> list[str]:
    """Select entire families by unambiguous thread-id prefix or recent recorded usage."""
    if args.session_id:
        keys = [
            key
            for key, threads in families.items()
            if key.startswith(args.session_id)
            or any(thread.session_id.startswith(args.session_id) for thread in threads)
        ]
        if len(keys) > 1:
            raise ValueError(f"ambiguous Codex session prefix {args.session_id!r}")
        return keys
    cutoff = time.time() - analyzer.parse_since(args.since)
    return [
        key
        for key, threads in families.items()
        if any(item.timestamp is not None and item.timestamp >= cutoff for thread in threads for item in thread.calls)
    ]


def _render_family(key: str, threads: list[CodexThread], analyzer: ModuleType, shared: ModuleType) -> list[str]:
    """Render family shares and each thread's independent windows with no Claude pricing."""
    all_calls = [item for thread in threads for item in thread.calls]
    if not all_calls:
        return []
    sub_calls = [item for thread in threads if thread.parent_id for item in thread.calls]
    tokens = sum(item.context for item in all_calls)
    sub_tokens = sum(item.context for item in sub_calls)
    lines = [
        f"### Session `{shared._cell(key)}`",
        "",
        f"Subagent share: {len(sub_calls)}/{len(all_calls)} calls ({len(sub_calls) / len(all_calls):.1%}); {sub_tokens:,}/{tokens:,} tokens ({sub_tokens / tokens if tokens else 0:.1%}).",
        "",
        "AGENTS.md bytes × calls (whole-thread upper exposure estimate):",
        "",
        "| thread | edited code | AGENTS.md bytes | calls | bytes × calls |",
        "|---|---|---:|---:|---:|",
    ]
    for thread in sorted(threads, key=lambda item: (bool(item.parent_id), item.session_id)):
        label = f"subagent {shared._cell(thread.session_id)}" if thread.parent_id else "main"
        agents = sum(window.attachments.get("instructions", 0) for window in thread.windows)
        edited = "yes (attempt observed)" if thread.edited_code else "no edit observed"
        lines.append(f"| {label} | {edited} | {agents:,} | {len(thread.calls)} | {agents * len(thread.calls):,} |")
    for thread in sorted(threads, key=lambda item: (bool(item.parent_id), item.session_id)):
        label = f"subagent {shared._cell(thread.session_id)}" if thread.parent_id else "main"
        lines += ["", f"#### Transcript: {label}", "", f"Usage dedupe: {thread.usage_source}.", ""]
        lines += [f"Measurement limit: {note}" for note in thread.notes]
        for number, window in enumerate(thread.windows, 1):
            lines += shared._render_window(window, number, analyzer, token_only=True)
    return lines


def run_composition(root: Path, args: argparse.Namespace, analyzer: ModuleType) -> list[str] | int:
    """Discover and render Codex families while retaining caller-owned report writing."""
    # Runtime dispatch also reaches this module through the shared composition module.
    import _context_composition as shared

    try:
        families = _discover(root, analyzer)
        keys = _select(families, args, analyzer)
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    if args.session_id and not keys:
        sys.stderr.write(f"no session matching {args.session_id!r} under {root}\n")
        return 1
    reports = [_render_family(key, families[key], analyzer, shared) for key in keys]
    if not any(reports):
        sys.stderr.write("no sessions with usage found for context composition\n")
        return 1
    lines = [
        "## Context composition",
        "",
        "Runtime: Codex (tokens only); shipped in the Claude foundry plugin.",
        "",
        f"Sessions found: {sum(bool(report) for report in reports)}.",
        "",
    ]
    for report in reports:
        lines += report
    lines += [
        "### Measurement notes",
        "",
        "- Context tokens are post-call input + output; input includes cached tokens and output includes reasoning.",
        "- Cached share is cached input / total input. No Claude prices, cache-write tiers or TTLs apply.",
        "- Native response_id records take precedence for the entire thread; mirrored token-count events are excluded. Mixed historical event-only usage in such a thread is unavailable.",
        "- Legacy events dedupe adjacent cumulative total_token_usage snapshots; only valid last_token_usage supplies a call. Cumulative-only events cannot establish calls or context sizes.",
        "- Injection bytes count tagged payloads and other input-message text, excluding wrappers, the AGENTS.md heading and transcript metadata. Opaque base instructions and retained compaction history are unallocated.",
        "- Tool-result tokens are estimates: ceil(UTF-8 bytes / 4) per output. Identical bytes count copies after the first within each window.",
        "- AGENTS.md bytes × calls is a whole-thread upper exposure estimate, not tokenizer usage; edit flags identify attempts, not confirmed successful writes.",
        "- Large-input triggers are likely explanations, not proof. Explicit compaction records override the strict 70% context-drop fallback.",
        "",
    ]
    return lines
