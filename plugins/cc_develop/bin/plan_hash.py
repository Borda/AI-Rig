#!/usr/bin/env python
"""plan_hash.py — detect a plan file edited while a develop skill builds from it.

``.plans/`` is gitignored, so a plan rewritten mid-build leaves no diff. ``record`` stores the plan's SHA-256 in a
session sentinel; ``check`` re-hashes it and prints a ``! BLOCKED`` line when the plan changed or vanished. Detection
only: nothing is reverted and the exit status is always 0 so a mismatch never aborts the report.

Usage:
    python plan_hash.py record <plan-file>   # empty path clears the sentinel (no plan this run)
    python plan_hash.py check

Output (stdout):
    ``check`` prints ``! BLOCKED — plan changed during build: <path>`` on mismatch, nothing otherwise.

Exit codes:
    0 — always
    2 — bad arguments (argparse default)
"""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from pathlib import Path

#: Separates the stored digest from the plan path inside the sentinel file.
_SEP = "  "


def _sentinel() -> Path:
    """Return the session sentinel path, ``$TMPDIR/dev-plan-sha-<session>``."""
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"dev-plan-sha-{csid}"


def _digest(path: Path) -> str | None:
    """Return the SHA-256 hex digest of ``path``, or ``None`` when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def record(plan: str) -> None:
    """Store the plan digest, or clear the sentinel when ``plan`` is empty or unreadable."""
    sentinel = _sentinel()
    digest = _digest(Path(plan)) if plan else None
    if digest is None:
        sentinel.unlink(missing_ok=True)
        return
    sentinel.write_text(f"{digest}{_SEP}{plan}\n", encoding="utf-8")


def check() -> None:
    """Print a ``!

    BLOCKED`` line when the recorded plan changed or disappeared.
    """
    try:
        line = _sentinel().read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return
    digest, _, plan = line.partition(_SEP)
    if digest and plan and _digest(Path(plan)) != digest:
        print(f"! BLOCKED — plan changed during build: {plan}")


def main(argv: list[str] | None = None) -> int:
    """Run ``record`` or ``check``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("record").add_argument("plan", nargs="?", default="")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    if args.cmd == "record":
        record(args.plan)
    else:
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
