"""Measure observable context composition in Claude Code and Codex transcript windows.

Purpose: Expose context overhead alongside the existing session cost analyzer. Scope: Read main and subagent JSONL
transcripts independently; Claude usage totals     use analyzer message-id dedupe and cache-duration accounting. Codex
rollouts     use a sibling token-only parser with response-id or cumulative-event dedupe. Usage: Imported by
cost_analyzer.py for --composition; the caller supplies its     module so discovery, selection, usage parsing and output
retain one owner. Outputs: Markdown lines with per-window context, attachment and tool-result     measurements, Read
repetitions, hook injections and likely cache rebuilds.     Claude sessions include logged compactions and gate
estimates versus realised windows. Failure: Missing sessions or usage return 1.     Malformed JSON lines are skipped
with a warning; filesystem errors propagate. Used by: cost_analyzer.main and the context-composition regression tests.

Attachment bytes are UTF-8 payload bytes, excluding transcript metadata. Tool tokens are estimates, because transcripts
do not expose their exact tokenization. Measurements describe content observed within each window, not an exact
allocation of every context token: system prompts and retained compacted content are opaque.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cost_analyzer import Call

# Preserve Claude discovery and pricing unless Codex is explicitly selected.
DEFAULT_RUNTIME = "claude"
# Match the requested fallback: a post-call context below 70% starts a window.
CONTEXT_DROP_RATIO = 0.70
# Large cache rebuilds are interesting only above this token count.
LARGE_WRITE_TOKENS = 50_000
# Older transcripts without a duration split use the analyzer's five-minute default.
DEFAULT_CACHE_TTL_SECONDS = 300
# The explicit one-hour cache-write tier survives longer idle gaps.
ONE_HOUR_CACHE_TTL_SECONDS = 3_600
# A transparent byte-based estimate avoids a tokenizer dependency or model download.
ESTIMATED_BYTES_PER_TOKEN = 4
COMPACTION_COLUMNS = (
    "ts",
    "event",
    "trigger",
    "decision",
    "reason",
    "ctx",
    "calls_ahead",
    "K",
    "summary_len",
    "has_custom_instructions",
)


@dataclass
class ObservedCall:
    """Keep usage and timing evidence for one deduplicated API call."""

    call: Call
    timestamp: float | None
    context: int
    cache_ttl: int


@dataclass
class ReadObservation:
    """Compare a Read result with the previous Read of the same recorded path."""

    path: str
    previous: ReadObservation | None = None
    digest: str | None = None

    @property
    def change(self) -> str:
        """Return changed, unchanged or unknown according to available content hashes."""
        if self.digest is None or self.previous is None or self.previous.digest is None:
            return "unknown"
        return "unchanged" if self.digest == self.previous.digest else "changed"


@dataclass
class Window:
    """Collect independent measurements for one uninterrupted context window."""

    boundary: str = "session start"
    calls: list[ObservedCall] = field(default_factory=list)
    attachments: dict[str, int] = field(default_factory=dict)
    tool_bytes: int = 0
    tool_tokens: int = 0
    identical_tool_bytes: int = 0
    result_hashes: set[str] = field(default_factory=set)
    reads: list[ReadObservation] = field(default_factory=list)
    hooks: dict[tuple[int, str], list[int]] = field(default_factory=dict)
    prompts: set[int] = field(default_factory=set)
    large_writes: list[tuple[int, int, str]] = field(default_factory=list)


@dataclass
class TranscriptState:
    """Track call dedupe and tool correlation without joining unrelated contexts."""

    windows: list[Window] = field(default_factory=lambda: [Window()])
    calls: dict[str, Call] = field(default_factory=dict)
    previous: ObservedCall | None = None
    cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS
    prompt: int = 0
    compacted: bool = False
    tools: dict[str, ReadObservation | None] = field(default_factory=dict)
    read_paths: dict[str, ReadObservation] = field(default_factory=dict)


def _require_mapping(value: object, name: str) -> dict:
    """Reject malformed nested objects before any row changes accumulated measurements."""
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_string(container: dict, key: str, name: str) -> None:
    """Reject a present non-string identifier before it reaches a dictionary key or sort."""
    if container.get(key) is not None and not isinstance(container[key], str):
        raise ValueError(f"{name}.{key} must be a string")


def _validate_message(message: dict) -> None:
    """Check tool identifiers and Read input containers the observers key on, allowing absent optional values."""
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                _require_string(block, "id", "tool_use")
                if block.get("input") is not None:
                    _require_mapping(block["input"], "tool_use.input")
            elif block.get("type") == "tool_result":
                _require_string(block, "tool_use_id", "tool_result")


def _validate_attachment(attachment: object) -> None:
    """Require a string attachment type so window tables never sort mixed key types."""
    if isinstance(attachment, dict) and "type" in attachment and not isinstance(attachment["type"], str):
        raise ValueError("attachment.type must be a string")


def _validate_payload(payload: dict) -> None:
    """Check Codex event fields, identities and spawn metadata before identity or usage processing."""
    for key in ("type", "role", "id", "call_id", "session_id", "model", "response_id"):
        _require_string(payload, key, "payload")
    if payload.get("info") is not None:
        _require_mapping(payload["info"], "payload.info")
    source = payload.get("source")
    # Codex encodes plain subagent kinds ("review", "compact", ...) as strings and thread spawns as objects.
    if isinstance(source, dict) and "subagent" in source and not isinstance(source["subagent"], str):
        subagent = _require_mapping(source["subagent"], "source.subagent")
        if "thread_spawn" in subagent:
            spawn = _require_mapping(subagent["thread_spawn"], "subagent.thread_spawn")
            _require_string(spawn, "parent_thread_id", "thread_spawn")


def _warn_row(path: Path, number: int, error: Exception) -> None:
    """Identify the original failing line without preventing later transcript measurements."""
    kind = "malformed JSON" if isinstance(error, json.JSONDecodeError) else "malformed row"
    sys.stderr.write(f"warning: {kind} at {path}:{number}; skipped ({error})\n")


def _read_numbered_rows(path: Path, analyzer: ModuleType) -> list[tuple[int, dict]]:
    """Read fully validated transcript objects, so observers never meet a shape that fails midway through a row."""
    rows = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            try:
                row = _require_mapping(json.loads(line), "row")
                for key in ("type", "subtype"):
                    if row.get(key) is not None and not isinstance(row[key], str):
                        raise ValueError(f"{key} must be a string")
                if "message" in row:
                    _validate_message(_require_mapping(row["message"], "message"))
                if "payload" in row:
                    _validate_payload(_require_mapping(row["payload"], "payload"))
                _validate_attachment(row.get("attachment"))
                error = analyzer.row_shape_error(row)
                if error:
                    raise ValueError(error)
            except (ValueError, TypeError, AttributeError, RecursionError) as error:
                _warn_row(path, number, error)
                continue
            rows.append((number, row))
    return rows


def _bounded_number(value: object, limit: int) -> bool:
    """Accept a finite int or float within ``limit``, rejecting booleans, infinities and NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return abs(value) <= limit


def _parse_compaction_event(line: str, analyzer: ModuleType) -> dict | None:
    """Validate event identity and timing while ignoring future event types."""
    row = json.loads(line)
    if not isinstance(row, dict):
        raise ValueError("expected an event object")
    if row.get("event") not in ("gate", "precompact", "postcompact"):
        return None
    timestamp = row.get("ts")
    session = row.get("session_id")
    if not isinstance(timestamp, str) or not isinstance(session, str) or not session:
        raise ValueError("expected a timestamp and session id")
    analyzer.parse_ts(timestamp)
    for key in ("ctx", "calls_ahead", "K"):
        # Rendering formats these as numbers; an unbounded value would overflow float conversion.
        if row.get(key) is not None and not _bounded_number(row[key], analyzer.MAX_TOKEN_COUNT):
            raise ValueError(f"{key} must be a bounded number")
    return row


def _read_compactions(path: Path, analyzer: ModuleType) -> dict[str, list[dict]]:
    """Group recognised log events by session and sort their ISO timestamps by instant."""
    sessions: dict[str, list[dict]] = {}
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return sessions
    with handle:
        for number, line in enumerate(handle, 1):
            try:
                row = _parse_compaction_event(line, analyzer)
            except (ValueError, RecursionError):
                sys.stderr.write(f"warning: malformed compaction event at {path}:{number}; skipped\n")
                continue
            if row is not None:
                sessions.setdefault(row["session_id"], []).append(row)
    for events in sessions.values():
        events.sort(key=lambda event: analyzer.parse_ts(event["ts"]))
    return sessions


def _is_compaction(row: dict) -> bool:
    """Recognize explicit boundary and summary rows, including Claude's native spellings."""
    markers = {"compact-boundary", "compact-summary", "compact_boundary", "compact_summary"}
    return row.get("type") in markers or row.get("subtype") in markers or bool(row.get("isCompactSummary"))


def _payload_bytes(content: object) -> bytes:
    """Extract UTF-8 content without counting wrapper metadata or JSON escaping.

    Examples:
        >>> len(_payload_bytes([{"type": "text", "text": "café"}]))
        5
    """
    chunks: list[bytes] = []
    # An explicit stack keeps document order without recursion, so deeply nested payloads cannot exhaust the stack.
    pending = [content]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            # Replace lone surrogates with '?' to keep bytes valid UTF-8 for Codex text decoding.
            chunks.append(item.encode("utf-8", errors="replace"))
        elif isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            # Native instruction/file wrappers expose one payload; rawContent is a duplicate.
            key = next((key for key in ("text", "content", "files", "file") if key in item), None)
            if key is not None:
                pending.append(item[key])
    return b"".join(chunks)


def _timestamp(row: dict, analyzer: ModuleType) -> float | None:
    """Parse timing evidence through the analyzer, preserving missing timestamps as unknown."""
    try:
        return analyzer.parse_ts(row["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None


def _new_window(state: TranscriptState, boundary: str) -> None:
    """Coalesce adjacent summary/boundary markers while retaining the compaction trigger."""
    if state.windows[-1].calls:
        state.windows.append(Window(boundary))
    else:
        state.windows[-1].boundary = boundary
    state.compacted = True


def _write_trigger(previous: ObservedCall | None, current: ObservedCall, compacted: bool) -> str:
    """Label a large cache write by strongest available evidence, without claiming causality."""
    if compacted:
        return "compaction"
    if previous is None:
        return "other"
    if previous.call.model != current.call.model:
        return "model change"
    if previous.timestamp is not None and current.timestamp is not None:
        if current.timestamp - previous.timestamp > previous.cache_ttl:
            return f"idle gap beyond the cache TTL ({previous.cache_ttl}s)"
    return "other"


def _observe_call(row: dict, state: TranscriptState, analyzer: ModuleType, explicit: bool) -> None:
    """Reuse analyzer dedupe, split fallback windows and record cache rebuild evidence."""
    call = analyzer._row_to_call(row, state.calls)
    if call is None:
        return
    state.calls[call.message_id] = call
    context = analyzer.prompt_tokens(call.usage) + call.output
    if not explicit and state.previous and context < state.previous.context * CONTEXT_DROP_RATIO:
        _new_window(state, "70% context drop")
    write_5m, write_1h = analyzer.cache_write_split(call.usage)
    if write_5m or write_1h:
        state.cache_ttl = ONE_HOUR_CACHE_TTL_SECONDS if write_1h else DEFAULT_CACHE_TTL_SECONDS
    current = ObservedCall(call, _timestamp(row, analyzer), context, state.cache_ttl)
    window = state.windows[-1]
    window.calls.append(current)
    if call.cache_write > LARGE_WRITE_TOKENS:
        window.large_writes.append(
            (len(state.calls), call.cache_write, _write_trigger(state.previous, current, state.compacted))
        )
    state.previous = current
    state.compacted = False


def _observe_tool_use(block: dict, state: TranscriptState) -> None:
    """Correlate unique Read invocations with later results, preserving recorded path spelling."""
    tool_id = block.get("id")
    if not tool_id or tool_id in state.tools:
        return
    state.tools[tool_id] = None
    if block.get("name") != "Read":
        return
    path = (block.get("input") or {}).get("file_path")
    if not isinstance(path, str):
        return
    observation = ReadObservation(path, state.read_paths.get(path))
    state.tools[tool_id] = observation
    state.read_paths[path] = observation
    state.windows[-1].reads.append(observation)


def _observe_tool_result(block: dict, state: TranscriptState) -> None:
    """Measure returned payloads and redundant bytes within the current window."""
    payload = _payload_bytes(block.get("content"))
    digest = hashlib.sha256(payload).hexdigest()
    window = state.windows[-1]
    window.tool_bytes += len(payload)
    window.tool_tokens += (len(payload) + ESTIMATED_BYTES_PER_TOKEN - 1) // ESTIMATED_BYTES_PER_TOKEN
    if digest in window.result_hashes:
        window.identical_tool_bytes += len(payload)
    window.result_hashes.add(digest)
    read = state.tools.get(block.get("tool_use_id"))
    if read is not None and "content" in block and block["content"] is not None and not block.get("is_error"):
        read.digest = digest


def _observe_content(row: dict, state: TranscriptState) -> None:
    """Track prompts and tool blocks even on rows whose usage was already counted."""
    message = row.get("message") or {}
    content = message.get("content", [])
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    is_user = row.get("type") == "user" or message.get("role") == "user"
    if is_user and not row.get("isMeta") and not row.get("isCompactSummary"):
        if isinstance(content, str) or any(block.get("type") == "text" for block in blocks):
            state.prompt += 1
            state.windows[-1].prompts.add(state.prompt)
    for block in blocks:
        if block.get("type") == "tool_use":
            _observe_tool_use(block, state)
        elif block.get("type") == "tool_result":
            _observe_tool_result(block, state)


def _observe_attachment(row: dict, state: TranscriptState) -> None:
    """Count attachment payloads by type and hook injections by prompt and event."""
    attachment = row.get("attachment")
    if not isinstance(attachment, dict):
        return
    kind = attachment.get("type", "unknown")
    size = len(_payload_bytes(attachment))
    window = state.windows[-1]
    window.attachments[kind] = window.attachments.get(kind, 0) + size
    if kind in {"hook_additional_context", "hookAdditionalContext"}:
        event = str(attachment.get("hookEvent") or attachment.get("hookName") or "unknown")
        window.hooks.setdefault((state.prompt, event), []).append(size)


def analyze_transcript(path: Path, analyzer: ModuleType) -> list[Window]:
    """Measure windows of one transcript using the cost analyzer's usage contract.

    Explicit compaction records take precedence throughout a transcript. Otherwise, a post-call context drop below 70%
    starts a new window. Subagent transcripts must be analyzed separately because their context lifetimes are
    independent.
    """
    rows = [row for _, row in _read_numbered_rows(path, analyzer)]
    explicit = any(_is_compaction(row) for row in rows)
    state = TranscriptState()
    # Rows were validated whole; an exception here is a defect and stays visible instead of leaving partial state.
    for row in rows:
        if _is_compaction(row):
            _new_window(state, "compaction record")
        _observe_call(row, state, analyzer, explicit)
        _observe_content(row, state)
        _observe_attachment(row, state)
    return [window for window in state.windows if window.calls]


def _cell(value: str) -> str:
    """Escape transcript-controlled text so table rows retain their Markdown structure."""
    return (
        value.replace("&", "&amp;").replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ").replace("\r", " ")
    )


def _render_hooks(window: Window) -> list[str]:
    """Show prompt totals separately from hook event frequency and individual sizes."""
    lines = [
        "",
        "Hook injections:",
        "",
        "| prompt | event | event count | total bytes | bytes per event |",
        "|---:|---|---:|---:|---|",
    ]
    prompts = window.prompts | {prompt for prompt, _ in window.hooks}
    for prompt in sorted(prompts):
        events = [(event, sizes) for (owner, event), sizes in window.hooks.items() if owner == prompt]
        count = sum(len(sizes) for _, sizes in events)
        size = sum(sum(sizes) for _, sizes in events)
        lines.append(f"| {prompt} | **all events** | {count} | {size:,} | - |")
        for event, sizes in events:
            lines.append(
                f"| {prompt} | {_cell(event)} | {len(sizes)} | {sum(sizes):,} | {', '.join(map(str, sizes))} |"
            )
    if not prompts:
        lines.append("| - | none observed | 0 | 0 | - |")
    return lines


def _render_repetitions(window: Window) -> list[str]:
    """List repeated Reads and all large writes without applying the ranking limit."""
    lines = ["", "Repeated Read calls:", "", "| path | content |", "|---|---|"]
    repeated = [read for read in window.reads if read.previous is not None]
    lines += [f"| {_cell(read.path)} | {read.change} |" for read in repeated] or ["| none observed | - |"]
    return lines + _render_large_writes(window)


def _render_large_writes(window: Window, *, token_only: bool = False) -> list[str]:
    """Render large-call trigger evidence using the runtime's measured input category."""
    lines = [
        "",
        "Inputs above 50,000 tokens:" if token_only else "Cache writes above 50,000 tokens:",
        "",
        "| call | input tokens | likely trigger |" if token_only else "| call | write tokens | likely trigger |",
        "|---:|---:|---|",
    ]
    lines += [f"| {call} | {tokens:,} | {trigger} |" for call, tokens, trigger in window.large_writes] or [
        "| - | - | none observed |"
    ]
    return lines


def _render_window(window: Window, number: int, analyzer: ModuleType, *, token_only: bool = False) -> list[str]:
    """Render one window's measured usage and observable content overhead."""
    prompt = sum(analyzer.prompt_tokens(item.call.usage) for item in window.calls)
    cached = sum(item.call.cache_read for item in window.calls)
    share = cached / prompt if prompt else 0.0
    lines = [
        f"#### Context window {number} ({window.boundary})",
        "",
        "| start context tokens | end context tokens | calls | cached share of prompt tokens |",
        "|---:|---:|---:|---:|",
        f"| {window.calls[0].context:,} | {window.calls[-1].context:,} | {len(window.calls)} | {share:.1%} |",
        "",
        "Injection payloads (UTF-8 bytes):" if token_only else "Attachment payloads (UTF-8 bytes):",
        "",
        "| injection kind | bytes |" if token_only else "| attachment type | bytes |",
        "|---|---:|",
        f"| **{'AGENTS.md' if token_only else 'instructions'}** | {window.attachments.get('instructions', 0):,} |",
    ]
    lines += [
        f"| {_cell(kind)} | {size:,} |" for kind, size in sorted(window.attachments.items()) if kind != "instructions"
    ]
    lines += [
        "",
        f"Tool-result tokens (estimated): {window.tool_tokens:,}; tool-result bytes: {window.tool_bytes:,}; "
        f"in-window identical tool-result bytes (copies after the first): {window.identical_tool_bytes:,}.",
    ]
    if token_only:
        return lines + _render_large_writes(window, token_only=True) + [""]
    return lines + _render_hooks(window) + _render_repetitions(window) + [""]


def _compaction_cell(value: object, column: str) -> str:
    """Display optional log fields with whole-token costs and readable boolean values.

    Examples:
        >>> _compaction_cell(None, "ctx")
        ''
        >>> _compaction_cell(False, "has_custom_instructions")
        'false'
        >>> _compaction_cell(240000.6, "K")
        '240001'
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if column == "K" and isinstance(value, (int, float)):
        return f"{value:.0f}"
    return _cell(str(value))


def _compaction_window(
    precompact: dict, next_precompact: dict | None, windows: list[Window], analyzer: ModuleType
) -> tuple[int, Window] | None:
    """Match a logged compaction to a main window whose surrounding calls bracket it."""
    timestamp = analyzer.parse_ts(precompact["ts"])
    upper = analyzer.parse_ts(next_precompact["ts"]) if next_precompact else float("inf")
    for number, window in enumerate(windows[1:], 2):
        previous = windows[number - 2].calls[-1].timestamp
        first = window.calls[0].timestamp
        if window.boundary not in ("compaction record", "70% context drop"):
            continue
        if previous is not None and first is not None and previous <= timestamp <= first < upper:
            return number, window
    return None


def _render_gate_estimate(gate: dict, following: list[dict], windows: list[Window], analyzer: ModuleType) -> str:
    """Compare a gate with its next compaction without inventing missing transcript evidence."""
    label = f"- estimate vs realised (gate {_cell(gate['ts'])}"
    if not following:
        return label + "): no compaction followed."
    precompact = following[0]
    label += f", precompact {_cell(precompact['ts'])}"
    match = _compaction_window(precompact, following[1] if len(following) > 1 else None, windows, analyzer)
    if match is None:
        return label + "): realised window unavailable (no matching boundary with surrounding call timestamps)."
    number, window = match
    # The plugin loader registers this sibling after the composition module initializes.
    from compact_gate import DEFAULT_FLOOR_TOKENS

    estimate = _compaction_cell(gate.get("calls_ahead"), "calls_ahead") or "unavailable"
    return (
        label + f", main window {number}): calls {estimate} vs {len(window.calls)}; "
        f"post-compaction first-call context {DEFAULT_FLOOR_TOKENS:,} tokens (gate floor assumption) "
        f"vs {window.calls[0].context:,} tokens (post-call)."
    )


def _render_compactions(events: list[dict], windows: list[Window], analyzer: ModuleType) -> list[str]:
    """Render session log metadata and gate comparisons independently of subagent usage."""
    if not events:
        return ["no logged compaction events.", ""]
    lines = [
        "#### Logged compaction events",
        "",
        "| " + " | ".join(COMPACTION_COLUMNS) + " |",
        "|" + "---|" * len(COMPACTION_COLUMNS),
    ]
    for event in events:
        cells = [_compaction_cell(event.get(column), column) for column in COMPACTION_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    for index, event in enumerate(events):
        if event["event"] == "gate":
            following = [row for row in events[index + 1 :] if row["event"] == "precompact"]
            lines.append(_render_gate_estimate(event, following, windows, analyzer))
    return [*lines, ""]


def _render_session(path: Path, analyzer: ModuleType, events: list[dict] | None = None) -> list[str]:
    """Render session log comparisons and separate main/subagent windows with cache-write totals."""
    transcripts = [path, *sorted((path.parent / path.stem / "subagents").glob("agent-*.jsonl"))]
    measured = [(transcript, analyze_transcript(transcript, analyzer)) for transcript in transcripts]
    all_calls = [item.call for _, windows in measured for window in windows for item in window.calls]
    if not all_calls:
        return []
    splits = [analyzer.cache_write_split(call.usage) for call in all_calls]
    lines = [
        f"### Session `{_cell(path.stem)}`",
        "",
        f"Cache-write split (main + subagents): 5-minute {sum(short for short, _ in splits):,} tokens; "
        f"1-hour {sum(long for _, long in splits):,} tokens.",
        "",
    ]
    lines += _render_compactions(events or [], measured[0][1], analyzer)
    for transcript, windows in measured:
        label = "main" if transcript == path else f"subagent {_cell(transcript.stem)}"
        lines += [f"#### Transcript: {label}", ""]
        for number, window in enumerate(windows, 1):
            lines += _render_window(window, number, analyzer)
    return lines


def run_composition(
    root: Path, args: argparse.Namespace, analyzer: ModuleType, *, projects_root_explicit: bool = True
) -> list[str] | int:
    """Dispatch composition reports using existing session selection and caller-owned output."""
    if args.runtime != DEFAULT_RUNTIME:
        # Delay the circular import until shared Window types and rendering helpers exist.
        import _codex_composition

        codex_root = root if projects_root_explicit else _codex_composition.default_root()
        return _codex_composition.run_composition(codex_root, args, analyzer)
    if args.session_id:
        path = analyzer._find_session(root, args.session_id)
        if path is None:
            sys.stderr.write(f"no session matching {args.session_id!r} under {root}\n")
            return 1
        paths = [path]
    else:
        cutoff = time.time() - analyzer.parse_since(args.since)
        paths = [session.path for session in analyzer._collect_window_sessions(root, cutoff)]
    lines = ["## Context composition", ""]
    compactions = _read_compactions(Path(args.compactions_log).expanduser(), analyzer)
    reports = [_render_session(path, analyzer, compactions.get(path.stem, [])) for path in paths]
    if not any(reports):
        sys.stderr.write("no sessions with usage found for context composition\n")
        return 1
    for report in reports:
        lines += report
    lines += [
        "### Measurement notes",
        "",
        "- Context tokens are post-call prompt + output; cached share is cache-read / total prompt tokens across calls.",
        "- Content bytes count payloads observed in each window, excluding transcript metadata and JSON escaping.",
        "- Attachments without content/text/files/file payload fields report 0; their rendered bytes are unavailable.",
        f"- Tool-result tokens are estimates: ceil(UTF-8 bytes / {ESTIMATED_BYTES_PER_TOKEN}) per result.",
        "- Opaque system prompts and retained summaries prevent an exact allocation of every context token.",
        "- Read flags compare returned content hashes across the transcript; missing/error results are unknown.",
        "- Hook prompt 0 means before the first observed prompt; events follow transcript order.",
        "- Cache-write triggers are likely explanations, not proof; missing duration splits default to 5-minute.",
        "",
    ]
    return lines
