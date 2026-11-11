#!/usr/bin/env python
"""health_monitor_start.py — create research health-monitoring sentinel.

Prints ``LAUNCH_AT`` (epoch seconds) and ``SENTINEL`` (path, forward-slash
separated for bash compatibility) to stdout.

Sentinels use ``TMPDIR`` or the hook's platform fallback and carry the active session identity.

Usage:
    health_monitor_start.py <skill-id>

Output (stdout):
    Two lines: ``LAUNCH_AT=<epoch>`` and ``SENTINEL=<posix-path>``.

Exit codes:
    0 — success
    1 — missing skill-id argument or sentinel creation failure
    2 — invalid skill-id or session identity (unsafe characters), or bad argparse usage (argparse default)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path

#: Allowed shape of a skill id: letters, digits, underscore and hyphen only.
_SKILL_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _sentinel_dir() -> Path:
    """Return the configured session temp directory, falling back to the hook's platform root."""
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback)


def main(argv: list[str] | None = None) -> int:
    """Create a health sentinel and print its launch metadata.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` on missing skill-id or sentinel creation failure;
        ``2`` on invalid skill-id, invalid session identity or bad argparse usage.

    Examples:
        No doctest — argv-dependent and touches the filesystem; covered by pytest
        with ``capsys``.
    """
    parser = argparse.ArgumentParser(
        prog="health_monitor_start.py",
        description="Create a research health-monitoring sentinel and print LAUNCH_AT + SENTINEL.",
    )
    # nargs="?" keeps the empty-argv case as exit 1 (handled below) rather than
    # argparse's exit 2 for a missing required positional — preserves the legacy contract.
    parser.add_argument(
        "skill_id",
        nargs="?",
        help="Skill identifier ([a-zA-Z0-9_-]); names the sentinel research-<skill-id>-check-<ts>-<session>.",
    )
    # argparse exits with code 2 on unknown flags or extra positionals — matches legacy bash contract.
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    if args.skill_id is None:
        print("health_monitor_start: skill-id required", file=sys.stderr)
        return 1

    skill_id = args.skill_id
    if not _SKILL_RE.fullmatch(skill_id):
        print(f"health_monitor_start: invalid SKILL_ID: {skill_id!r}", file=sys.stderr)
        return 2

    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", csid) or ".." in csid:
        print("health_monitor_start: invalid session identity", file=sys.stderr)
        return 2
    launch_at = int(time.time())
    sentinel = _sentinel_dir() / f"research-{skill_id}-check-{launch_at}-{csid}"
    # Exclusive create, not Path.touch(): touch() follows a symlink pre-planted at this predictable
    # path in world-writable /tmp. O_NOFOLLOW absent on win32 (hence getattr).
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        print(f"health_monitor_start: cannot create sentinel: {exc}", file=sys.stderr)
        return 1
    else:
        os.close(fd)

    sys.stdout.write(f"LAUNCH_AT={launch_at}\nSENTINEL={sentinel.as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
