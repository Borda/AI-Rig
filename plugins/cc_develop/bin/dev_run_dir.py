#!/usr/bin/env python
"""dev_run_dir.py — create ``.developments/<UTC-timestamp>/`` run directory.

With ``--sentinel <name>``, also touches ``<sentinel-dir>/<name>-<ts>``.
Sentinel name is sanitized to ``[a-zA-Z0-9_-]+`` to prevent path traversal.

Sentinel dir honors ``$TMPDIR`` when set, else ``tempfile.gettempdir()`` — the Python
equivalent of the shell ``${TMPDIR:-/tmp}`` idiom callers use to poll the sentinel.

Usage:
    python dev_run_dir.py [--sentinel <name>]

Output (stdout):
    Relative path of the created run directory.

Exit codes:
    0 — always (matches bash behaviour)
    2 — bad/missing required argument (argparse default)
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
    """Create a run directory and optional sentinel, then print its path.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        ``0`` always; argparse exits ``2`` on bad args.

    No doctest — creates directories and touches sentinel files; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="dev_run_dir.py",
        description="Create a timestamped .developments/ run directory, optionally with a sentinel.",
    )
    # nargs="?" so a bare ``--sentinel`` (no name) is accepted and simply skips the
    # sentinel touch — preserves the legacy bash contract where the name was optional.
    parser.add_argument("--sentinel", nargs="?", default="", help="Sentinel base name (sanitized to [a-zA-Z0-9_-]).")
    args = parser.parse_args(argv)

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = Path(".developments") / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.sentinel:
        sentinel_name = _SAFE_NAME_RE.sub("", args.sentinel)
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

    sys.stdout.write(run_dir.as_posix() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
