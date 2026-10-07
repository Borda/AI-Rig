#!/usr/bin/env python3
"""_telemetry.py — shared cli.jsonl logging for codemap core CLI tools.

Used by scan-query and scan-index (the two core CLI entry points). Each invocation appends one JSON record to
``.cache/codemap/logs/cli_<session>.jsonl``, falling back to ``cli.jsonl`` when no session id has been seeded. Separate
filenames for each session keep concurrent sessions from interleaving appends into one file.

The session id is seeded once per Claude Code session by the SessionStart hook (seed-session.js) into
``$TMPDIR/codemap-<project>-session``; this module reads it back so CLI records carry the same join key as the skill
layer.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

LOG_MAX_BYTES = 10 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_PLUGIN_VERSION: str | None = None


def plugin_version() -> str:
    """Return the codemap plugin version (``.claude-plugin/plugin.json``), cached.

    Stamped into every record as ``v`` so before/after comparisons across plugin
    releases stay possible when analysing merged logs. ``"?"`` when unreadable.

    Examples:
        >>> isinstance(plugin_version(), str)
        True
    """
    global _PLUGIN_VERSION
    if _PLUGIN_VERSION is None:
        try:
            manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
            _PLUGIN_VERSION = str(json.loads(manifest.read_text()).get("version", "?"))
        except Exception:
            _PLUGIN_VERSION = "?"
    return _PLUGIN_VERSION


def session_id() -> str:
    """Return the seeded session id for this project, or ``""`` if none."""
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        proj = Path(root).name
    except Exception:
        proj = Path.cwd().name
    sid_file = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"codemap-{proj}-session"
    try:
        return sid_file.read_text().strip()
    except OSError:
        return ""


def log_path_for(session: str, log_dir: Path) -> Path:
    """Resolve the per-session log file path (``cli.jsonl`` when session empty)."""
    name = f"cli_{_SAFE.sub('-', session)}.jsonl" if session else "cli.jsonl"
    return log_dir / name


def _rotate(path: Path) -> None:
    for i in range(2, 0, -1):
        src = path.parent / f"{path.name}.{i}"
        if src.exists():
            src.rename(path.parent / f"{path.name}.{i + 1}")
    if path.exists():
        path.rename(path.parent / f"{path.name}.1")


def log_cli(cmd: str, argv: list[str], result: object, t0: float, *, log_dir: Path | None = None) -> None:
    """Append one cli-layer telemetry record.

    Best-effort — never raises.
    """
    if os.environ.get("CODEMAP_LOGGING", "true").lower() == "false":
        return
    try:
        log_dir = log_dir or Path(os.environ.get("CODEMAP_LOG_DIR", ".cache/codemap/logs"))
        sid = session_id()
        log_file = log_path_for(sid, log_dir)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > LOG_MAX_BYTES:
            _rotate(log_file)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "layer": "cli",
            "v": plugin_version(),
            "cmd": cmd,
            "session": sid,
            "argv": argv,
            "timing_ms": max(0, int((time.time() - t0) * 1000)),
            "result": result if isinstance(result, dict) else {},
        }
        # Benchmark / demo runs tag themselves (export CODEMAP_TELEMETRY_SOURCE=bench)
        # so debrief can separate scripted load from organic usage — untagged demo
        # loops skewed the 2026-07 audit's per-project stats.
        source = os.environ.get("CODEMAP_TELEMETRY_SOURCE")
        if source:
            record["source"] = source
        with log_file.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:  # noqa: S110 - best-effort diagnostics; failure ignored
        pass
