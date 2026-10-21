#!/usr/bin/env python
"""compact_gate.py — decide whether a boundary compaction saves read-token cost.

Read the CWD-relative ``.temp/state/skill-contract.md`` metadata and the main Claude Code transcript selected by
``CLAUDE_CODE_SESSION_ID`` under ``~/.claude/projects/*/``. Reuse ``cost_analyzer`` prices, usage deduplication, prompt
counts, and cache-write splits; subagent transcript files are deliberately excluded from the session estimate.

Compaction pays off only when ``(ctx_now - floor) * calls_ahead > K``, where K includes a cache rewrite, the current
context read, and the summarizer's prompt and output, all expressed in cache-read-token units. Missing evidence or a
noninteractive boundary produces a continue decision. With no cache writes recorded, use the analyzer's legacy 5-minute
write rate.

When compaction pays off, the decision is ``advise`` by default: the skill keeps working and only mentions ``/compact``
at its next natural pause. The run is never held at a boundary unless the user opted in with the environment variable
``FOUNDRY_COMPACT_GATE=stop``, which turns that decision into ``stop``. A held run idles until the user acts, and a
Claude Code turn cannot resume on a timer, so the stop cannot be a timed wait and stays opt-in.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/compact_gate.py"

Print exactly one ``compact-gate:`` decision line (``continue``, ``advise`` or ``stop``). Best-effort append a gate
event to ``~/.claude/logs/compactions.jsonl``. Exit 0 after any decision; arguments print usage to stderr and exit 2.
Unexpected parsing/runtime failures remain visible as crashes rather than guessed decisions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Measured first-call context after compaction ranged from 112K to 125K tokens.
DEFAULT_FLOOR_TOKENS = 120_000
# Rebuilding the compacted context writes the full estimated floor to cache once.
REWRITE_TOKENS = DEFAULT_FLOOR_TOKENS
# Measured summarizer uncached prompts were about 10.4K tokens.
SUMMARY_PROMPT_TOKENS = 10_000
# Measured summaries ranged from 3.5K to 4.7K output tokens.
SUMMARY_OUT_TOKENS = 5_000
# The writer places the active boundary contract relative to the skill's working directory.
CONTRACT_PATH = Path(".temp/state/skill-contract.md")
# Only the final dedicated block supplies metadata; ordinary contract prose remains opaque.
CONTRACT_MAX_BYTES = 256 * 1024
OWNER_MAX_BYTES = 4 * 1024
METADATA_SENTINEL = "- gate-metadata:"
METADATA_LINE = re.compile(r"    - (?:(calls_ahead): ([0-9]{1,6})|(drop): (.*)|(interactive): (false))")
# The session id is spliced into a transcript glob, so wildcard and separator characters are rejected up front.
SESSION_ID = re.compile(r"[0-9A-Za-z-]+")
# Fixed argument-free invocation keeps a skill's calling shell block deterministic.
USAGE = "usage: compact_gate.py (no arguments)"
# Opt-in switch for holding the run at a paying boundary; any other value keeps the non-blocking advise default.
STOP_ENV = "FOUNDRY_COMPACT_GATE"


def _read_regular(path: Path, limit: int) -> bytes | None:
    """Read bounded regular-file bytes, rejecting named links and replacements during open."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        # Nonblocking open prevents a raced FIFO from hanging; no-follow is optional on Windows.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > limit
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            ):
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(limit + 1)
        finally:
            os.close(descriptor)
        return raw if len(raw) <= limit else None
    except OSError:
        return None


def _owner_reason(raw: bytes, session_id: str | None) -> str:
    """Require a digest-matched owner before distinguishing current and foreign sessions."""
    owner_raw = _read_regular(CONTRACT_PATH.with_suffix(".owner"), OWNER_MAX_BYTES)
    try:
        owner = json.loads(owner_raw) if owner_raw is not None else None
    except (ValueError, UnicodeError, RecursionError):
        return "unverified contract"
    if not isinstance(owner, dict) or owner.get("sha256") != hashlib.sha256(raw).hexdigest():
        return "unverified contract"
    owner_session = owner.get("session_id")
    if not isinstance(owner_session, str) or not owner_session:
        return "unverified contract"
    if owner_session != session_id:
        return "contract written by another session"
    return ""


def _read_boundary(raw: bytes) -> dict[str, str]:
    """Parse supported metadata only from the final dedicated block at the contract's end."""
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        return {}
    sentinel = next((i for i in range(len(lines) - 1, -1, -1) if lines[i] == METADATA_SENTINEL), None)
    if sentinel is None:
        return {}
    metadata: dict[str, str] = {}
    for line in lines[sentinel + 1 :]:
        match = METADATA_LINE.fullmatch(line)
        if match is None:
            return {}
        groups = match.groups()
        key, value = next((groups[i], groups[i + 1]) for i in range(0, len(groups), 2) if groups[i])
        if key in metadata:
            # The writer emits each key once; a repeated key means forged lines, so trust none of them.
            return {}
        metadata[key] = value
    return metadata


def _transcript_decision(row: dict[str, Any], home: Path) -> str:
    """Compute the decision from the last deduplicated usage and session cache-write weights."""
    # Foundry's test loader imports bin modules alphabetically; defer until cost_analyzer has been loaded.
    import cost_analyzer

    transcript = next(iter(sorted((home / ".claude" / "projects").glob(f"*/{row['session_id']}.jsonl"))), None)
    if transcript is None:
        return "transcript missing"
    calls_by_id, _, _, _ = cost_analyzer._parse_rows(transcript)
    calls = list(calls_by_id.values())
    if not calls:
        return "no usage"
    latest = calls[-1]
    prompt = cost_analyzer.prompt_tokens(latest.usage)
    row.update(ctx=prompt + latest.output, model=latest.model)
    key = cost_analyzer.price_key(latest.model)
    if key is None:
        return "unknown model"
    rate = cost_analyzer.PRICES[key].rate(prompt)
    splits = [cost_analyzer.cache_write_split(call.usage) for call in calls]
    write_5m = sum(split[0] for split in splits)
    write_1h = sum(split[1] for split in splits)
    multiplier = cost_analyzer.CACHE_WRITE_5M_MULTIPLIER
    if write_5m + write_1h:
        multiplier = (
            write_5m * cost_analyzer.CACHE_WRITE_5M_MULTIPLIER + write_1h * cost_analyzer.CACHE_WRITE_1H_MULTIPLIER
        ) / (write_5m + write_1h)
    row["K"] = (
        REWRITE_TOKENS * rate.input * multiplier / rate.cache_read
        + row["ctx"]
        + SUMMARY_PROMPT_TOKENS * rate.input / rate.cache_read
        + SUMMARY_OUT_TOKENS * rate.output / rate.cache_read
    )
    saving = (row["ctx"] - DEFAULT_FLOOR_TOKENS) * row["calls_ahead"]
    if saving > row["K"]:
        row["decision"] = "stop" if os.environ.get(STOP_ENV, "").strip().lower() == "stop" else "advise"
        return "saving exceeds rebuild cost"
    return "saving does not exceed rebuild cost"


def _boundary_decision(row: dict[str, Any], home: Path, metadata: dict[str, str]) -> str:
    """Apply attendance and call estimates after the boundary bytes have been verified."""
    if "calls_ahead" not in metadata:
        return "no calls_ahead"
    row["calls_ahead"] = int(metadata["calls_ahead"])
    if metadata.get("interactive") == "false":
        return "interactive: false in contract"
    if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0":
        return "session unattended"
    return _transcript_decision(row, home)


def _decide(row: dict[str, Any], home: Path, raw: bytes | None) -> str:
    """Require a safe contract owned by the active session before reading boundary inputs."""
    if raw is None:
        return "no contract"
    if not row["session_id"]:
        return "no session id"
    if not SESSION_ID.fullmatch(row["session_id"]):
        return "invalid session id"
    reason = _owner_reason(raw, row["session_id"])
    if reason:
        return reason
    return _boundary_decision(row, home, _read_boundary(raw))


def _log_decision(row: dict[str, Any], home: Path) -> None:
    """Append one JSON event without letting an unavailable log affect the decision."""
    try:
        path = home / ".claude" / "logs" / "compactions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Print one boundary decision and log its evidence, rejecting every CLI argument."""
    args = sys.argv[1:] if argv is None else argv
    if args:
        print(USAGE, file=sys.stderr)
        return 2
    home = Path.home()
    row: dict[str, Any] = {
        "event": "gate",
        "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "decision": "continue",
        "reason": "",
        "ctx": None,
        "calls_ahead": None,
        "K": None,
        "model": None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    raw = _read_regular(CONTRACT_PATH, CONTRACT_MAX_BYTES)
    row["reason"] = _decide(row, home, raw)
    _log_decision(row, home)
    if row["K"] is None:
        evidence = f"({row['reason']})"
    else:
        saving = (row["ctx"] - DEFAULT_FLOOR_TOKENS) * row["calls_ahead"]
        evidence = (
            f"ctx={row['ctx']} floor={DEFAULT_FLOOR_TOKENS} calls_ahead={row['calls_ahead']} "
            f"K={row['K']} saving={saving}"
        )
    # One line only: the skill's paste line is fixed text, so the contract body is no longer echoed into context.
    print(f"compact-gate: {row['decision']} {evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
