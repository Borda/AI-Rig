#!/usr/bin/env python3
"""Record codemap-py skill starts in session-sharded telemetry.

Purpose:
    Capture the selected codemap-py skill and a bounded intent snippet so later timing
    analysis can attribute skill startup to the correct runtime session.

Scope:
    Parse one hook payload, resolve the shared project/session marker, and append one
    compact JSONL record. It does not inspect source files or invoke external commands.
    Matched events must supply mapping-shaped ``tool_input`` when that field is truthy.

Usage:
    Invoke as a Claude ``PreToolUse`` hook matching ``Skill``, with event JSON on
    standard input.

Outputs:
    Append one record under the runtime-selected codemap log directory; emit no normal
    stdout content.

Failure:
    JSON decoding, type/value, and filesystem errors are ignored. Other errors propagate;
    a truthy non-mapping ``tool_input`` raises ``AttributeError`` rather than failing open.

Used by:
    The codemap-py Claude skill-start hook and the telemetry/profile reporting path.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Claude launches this hook as `python "<plugin-root>/hooks/log-skill-start.py"`, which
# already puts hooks/ on sys.path — but the test suite loads it through
# `importlib.util.spec_from_file_location`, which does not. Inserting explicitly makes
# the shared-helper import resolve under every load mechanism.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

#: Same sanitizer as ``codemap_py.telemetry`` — the shard names must agree to join.
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")


def iso_now() -> str:
    """Return the compact UTC timestamp format used by codemap telemetry."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Re-exported from the shared helper: this hook, ``seed-session.py``, ``log-tool-use.py``
# and ``codemap_py.telemetry`` all address ONE per-project marker, and a divergent copy
# here minted a second, unjoinable session id rather than raising anything.
project_name = _hookutil.project_name


def resolve_session(session_file: Path) -> str:
    """Return the seeded session id, minting and persisting one when none exists."""
    try:
        session = session_file.read_text(encoding="utf-8").strip()
    except OSError:
        session = ""
    if session:
        return session
    session = str(uuid.uuid4())
    try:
        session_file.write_text(session, encoding="utf-8")
    except OSError:
        pass
    return session


def main() -> int:
    """Log one codemap-py Skill event, generating a local session ID if needed."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if payload.get("tool_name") != "Skill":
            return 0
        tool_input = payload.get("tool_input") or {}
        skill = str(tool_input.get("skill", ""))
        if not skill.startswith("codemap-py:"):
            return 0
        tmp_dir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        session = _hookutil.runtime_session(payload, telemetry=True)
        if not session and _hookutil.runtime() != "codex":
            session = resolve_session(tmp_dir / f"codemap-{project_name()}-session")
        safe_session = _UNSAFE_KEY.sub("-", session)
        # Resolved through the shared helper so this shard lands in the same directory
        # log-tool-use.py and the cli layer write to — CODEMAP_LOG_DIR honoured, and the
        # default anchored to the project root rather than to whatever CWD Claude had.
        log_dir = _hookutil.log_dir()
        log_file = log_dir / (f"skills_{safe_session}.jsonl" if safe_session else "skills.jsonl")
        record = {
            "ts": iso_now(),
            "layer": "skill",
            "runtime": _hookutil.runtime(),
            "v": _hookutil.plugin_version(),
            "session": session,
            "project": _hookutil.project_root().as_posix(),
            "skill": skill,
            "event": "start",
            "intent": str(tool_input.get("args", ""))[:300],
            "hook_session": payload.get("session_id") or "",
        }
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
