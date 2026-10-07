#!/usr/bin/env python3
"""Seed the Claude codemap session marker without blocking startup.

Purpose:
    Persist the host-provided session identifier that the codemap telemetry hooks use to
    join records from separate tool invocations.

Scope:
    Accept one Claude hook payload, write one platform-temp marker, and return promptly.
    Resolve project identity through local Git-root markers without contacting
    external services or reading source contents.

Usage:
    Invoke as a Claude session-start hook with the host event JSON on standard input.

Outputs:
    Write ``codemap-<project>-session`` below the platform temp directory; emit no
    normal stdout content.

Failure:
    Non-Claude events, malformed payloads, missing session IDs, and marker write errors
    are ignored so host startup remains fail-open.

Used by:
    Codemap-py's Claude session initialization and the sibling telemetry hooks.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

#: Claude launches this hook as `python "<plugin-root>/hooks/seed-session.py"`, which
#: already puts hooks/ on sys.path — but the test suite loads it through
#: `importlib.util.spec_from_file_location`, which does not. Inserting explicitly makes
#: the shared-helper import resolve under every load mechanism.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

# This hook WRITES the marker every other layer reads, so its keying is the contract.
# Re-exported rather than re-implemented: three hooks each carried their own copy of
# this walk, and a divergence between copies is not an error — it is two files written
# under two keys that simply never join.
project_name = _hookutil.project_name


def main() -> int:
    """Persist a non-empty Claude session ID, failing open on every error."""
    try:
        if _hookutil.runtime() != "claude":
            return 0
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        session_id = _hookutil.runtime_session(payload)
        if not session_id:
            return 0
        tmp_dir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        (tmp_dir / f"codemap-{project_name()}-session").write_text(session_id, encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
