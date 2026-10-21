#!/usr/bin/env python
"""setup_worktree.py — create ``.temp/develop/<UTC-timestamp>/`` run directory.

Team-mode subagent handoff directory per artifact-lifecycle.md. Differs from
``dev_run_dir.py`` which creates ``.developments/<ts>/`` for skill checkpoints.

No health sentinel is created: team modes liveness-check through armed ``agent_watch.py`` deadlines, never a
touched file.

Any extra token is silently ignored so the legacy always-exit-0 contract is preserved, never an argparse exit-2;
argparse is present only to supply ``-h/--help``.

Usage:
    python setup_worktree.py

Output (two lines):
    Line 1: UTC timestamp (e.g. ``2026-05-22T10-00-00Z``)
    Line 2: Run directory path (e.g. ``.temp/develop/2026-05-22T10-00-00Z``)

Exit codes:
    0 — always (matches bash behaviour); argparse exits 0 after printing ``--help``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Create a run directory, then print its timestamp and path.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    No doctest — creates a run dir and reads argv; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    args = list(sys.argv[1:] if argv is None else argv)

    # argparse supplies only ``-h/--help``; any other token is ignored so the always-exit-0 contract holds
    # (argparse's native exit-2 on bad args would break it).
    if args and args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="setup_worktree.py",
            description="Create a team-mode subagent handoff run directory under .temp/develop/.",
        )
        parser.parse_args(args)  # exits 0 after printing help

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = Path(".temp") / "develop" / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    sys.stdout.write(f"{ts}\n{run_dir.as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
