#!/usr/bin/env python3
"""Record search and read telemetry for codemap guidance.

Purpose:
    Track low-cost tool-use signals and print one advisory when a source file is read
    repeatedly, helping callers choose structural codemap queries.

Scope:
    Parse one host tool event, append one compact JSONL record, and inspect a bounded
    tail for the repeated-read threshold. It does not run searches or alter source files.
    Matched events must supply mapping-shaped ``tool_input`` when that field is truthy.

Usage:
    Invoke as a Claude or Codex tool-use hook with the host event JSON on standard input.

Outputs:
    Append one runtime-scoped JSONL record and, at most once per qualifying read, print
    a codemap query hint.

Failure:
    JSON decoding, type/value, and filesystem errors are ignored. Other errors propagate;
    a truthy non-mapping ``tool_input`` raises ``AttributeError`` rather than failing open.

Used by:
    Codemap-py tool-use hook configuration and the runtime telemetry joiner.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Claude launches this hook as `python "<plugin-root>/hooks/log-tool-use.py"`, which
# already puts hooks/ on sys.path — but the test suite loads it through
# `importlib.util.spec_from_file_location`, which does not. Inserting explicitly makes
# the shared-helper import resolve under every load mechanism.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

_LOG_MAX_BYTES = 10 * 1024 * 1024
_BASH_SEARCH = re.compile(r"(^|[|;&(]\s*)(rg|grep|egrep|fgrep)\s")
#: Bytes of the shard the repeated-read nudge inspects. It runs on every matched Read, so
#: scanning the whole 10 MB budget to decide one advisory was the dominant cost of a hook
#: whose entire contract is to stay cheap. ~1.7K records fit here, far more than the three
#: the nudge counts; beyond that window the hint can fire one read late, never spuriously.
_NUDGE_TAIL_BYTES = 256 * 1024
#: Same sanitizer as ``codemap_py.telemetry`` — the shard names must agree to join.
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")


def iso_now() -> str:
    """Return the compact UTC timestamp format used by codemap telemetry."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Re-exported from the shared helper so this hook and ``seed-session.py`` — the marker's
# reader and its writer — cannot key on different names.
project_name = _hookutil.project_name


def session_id(payload: dict | None = None) -> str:
    """Prefer the current host event; use Claude's marker only for legacy events."""
    session = _hookutil.runtime_session(payload, telemetry=True)
    if session or _hookutil.runtime() == "codex":
        return session
    marker = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"codemap-{project_name()}-session"
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def rotate(log_file: Path) -> None:
    """Rotate the bounded telemetry file, retaining two prior generations."""
    for generation in (2, 1):
        source = Path(f"{log_file}.{generation}")
        if source.exists():
            source.replace(Path(f"{log_file}.{generation + 1}"))
    if log_file.exists():
        log_file.replace(Path(f"{log_file}.1"))


def target_for(tool_name: str, tool_input: dict) -> str:
    """Return the one telemetry target field appropriate for the host tool."""
    if tool_name == "Read":
        return str(tool_input.get("file_path", ""))
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))[:200]
    return str(tool_input.get("pattern") or tool_input.get("path") or "")


def tail_lines(log_file: Path, limit: int) -> list[str]:
    """Return the last *limit* bytes of *log_file* as whole lines.

    A window that starts mid-record would otherwise hand the caller a truncated first line
    that can still contain a searched-for substring, so it is dropped whenever the read did
    not start at byte 0.

    Args:
        log_file: The telemetry shard to read.
        limit: Maximum number of trailing bytes to inspect.

    Returns:
        Complete lines from the window, oldest first.
    """
    with log_file.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - limit)
        stream.seek(start)
        window = stream.read()
    lines = window.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start and lines else lines


def maybe_nudge_repeated_read(record: dict, log_file: Path) -> None:
    """Print one hint when a non-test Python source file is read for the third time."""
    if record["tool"] != "Read" or not record["target"].endswith(".py"):
        return
    if re.search(r"/tests?/", record["target"]):
        return
    escaped_target = json.dumps(record["target"])
    try:
        count = sum('"Read"' in line and escaped_target in line for line in tail_lines(log_file, _NUDGE_TAIL_BYTES))
    except OSError:
        return
    if count == 3:
        base = Path(record["target"]).stem
        print(
            f"[codemap] {base}.py read 3x this session — structural queries may be cheaper: "
            "codemap-py query symbol --with-imports <name>, rdeps <module>, fn-rdeps <module::fn>"
        )


def main() -> int:
    """Record one matched tool use, respecting the opt-out and fail-open contracts."""
    if os.environ.get("CODEMAP_LOGGING", "true").lower() == "false":
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        tool_name = str(payload.get("tool_name", ""))
        if tool_name not in {"Grep", "Read", "Glob", "Bash"}:
            return 0
        tool_input = payload.get("tool_input") or {}
        if tool_name == "Bash":
            command = str(tool_input.get("command", ""))
            if not _BASH_SEARCH.search(command) or "scan-query" in command:
                return 0
        session = session_id(payload)
        safe_session = _UNSAFE_KEY.sub("-", session)
        record = {
            "ts": iso_now(),
            "layer": "tool",
            "runtime": _hookutil.runtime(),
            "v": _hookutil.plugin_version(),
            "tool": tool_name,
            "session": session,
            "project": _hookutil.project_root().as_posix(),
            "target": target_for(tool_name, tool_input),
        }
        if tool_name in {"Grep", "Glob"}:
            # Capture scope while the producer can inspect it; the analysis host must not stat old paths.
            search_path = str(tool_input.get("path") or Path.cwd())
            record["search_path"] = search_path
            record["search_scope"] = "unknown"
            try:
                path = Path(search_path)
                if path.is_file():
                    record["search_scope"] = "file"
                elif path.is_dir():
                    record["search_scope"] = "directory"
            except OSError:
                pass
        log_dir = _hookutil.log_dir()
        log_file = log_dir / (f"tools_{safe_session}.jsonl" if safe_session else "tools.jsonl")
        log_dir.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > _LOG_MAX_BYTES:
            rotate(log_file)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        maybe_nudge_repeated_read(record, log_file)
    except (OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
