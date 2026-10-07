#!/usr/bin/env python
"""setup_worktree.py — create ``.temp/develop/<UTC-timestamp>/`` run directory.

Team-mode subagent handoff directory per artifact-lifecycle.md. Differs from
``dev_run_dir.py`` which creates ``.developments/<ts>/`` for skill checkpoints.

With ``--sentinel <name>``, also touches ``<sentinel-dir>/<name>-<ts>``.
Sentinel name is sanitized to ``[a-zA-Z0-9_-]+`` to prevent path traversal.

Sentinel dir honors ``$TMPDIR`` when set, else ``tempfile.gettempdir()`` — the Python
equivalent of the shell ``${TMPDIR:-/tmp}`` idiom callers use to poll the sentinel.

``--sentinel`` and its name are parsed by a direct ``argv`` check rather than argparse's
matcher so that the legacy always-exit-0 contract is preserved — a missing name, an unknown
extra token, or an all-unsafe name is silently ignored, never an argparse exit-2. argparse is
present only to supply ``-h/--help``.

Usage:
    python setup_worktree.py [--sentinel <name>]

Output (two lines):
    Line 1: UTC timestamp (e.g. ``2026-05-22T10-00-00Z``)
    Line 2: Run directory path (e.g. ``.temp/develop/2026-05-22T10-00-00Z``)

Exit codes:
    0 — always (matches bash behaviour); argparse exits 0 after printing ``--help``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

#: Matches every character outside letters, digits, underscore and hyphen, so it can be stripped from sentinel names.
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sentinel_dir() -> Path:
    """Return ``$TMPDIR`` when set, else ``tempfile.gettempdir()``.

    Matches the shell ``${TMPDIR:-/tmp}`` idiom used by callers that poll this sentinel, so both sides resolve to the
    same directory on every platform. ``os.environ`` is read first because ``tempfile.gettempdir()`` caches its result
    on first call and would not observe a later ``TMPDIR`` change.
    """
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def main(argv: list[str] | None = None) -> int:
    """Create a run directory and optional sentinel, then print their metadata.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    No doctest — creates a run dir and reads argv; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    args = list(sys.argv[1:] if argv is None else argv)

    # argparse supplies only ``-h/--help``; ``--sentinel`` and its name are parsed directly below so
    # the always-exit-0 contract holds (argparse's native exit-2 on bad args would break it).
    if args and args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="setup_worktree.py",
            description="Create a team-mode subagent handoff run directory under .temp/develop/.",
        )
        parser.add_argument(
            "--sentinel", metavar="NAME", help="Optional sentinel name to touch in the platform temp dir."
        )
        parser.parse_args(args)  # exits 0 after printing help

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = Path(".temp") / "develop" / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    if len(args) >= 2 and args[0] == "--sentinel" and args[1]:
        sentinel_name = _SAFE_NAME_RE.sub("", args[1])
        if sentinel_name:
            sentinel_path = _sentinel_dir() / f"{sentinel_name}-{ts}"
            try:
                fd, temporary_path = tempfile.mkstemp(prefix=".sentinel-", dir=sentinel_path.parent)
            except OSError:
                pass  # sentinel skipped — never abort the caller's always-exit-0 contract
            else:
                try:
                    os.close(fd)
                    # `link` atomically creates a new final entry, so an attacker link cannot be followed.
                    os.link(temporary_path, sentinel_path)
                except OSError:
                    pass  # sentinel skipped — never abort the caller's always-exit-0 contract
                finally:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass  # best-effort cleanup preserves the caller's always-exit-0 contract

    sys.stdout.write(f"{ts}\n{run_dir.as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
