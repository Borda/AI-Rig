#!/usr/bin/env python
"""next_run_dir.py — atomically allocate the next ``pr-<N>/run-<NNN>`` report directory.

Prints the created directory's path on success. Used by ``/oss:review`` Step 2 so two concurrent
reviews of the same PR, or a hand-deleted run leaving a gap in the sequence, can never allocate the
same run directory and silently overwrite each other's report.

Usage:
    python next_run_dir.py --pr-dir .reports/review/pr-1481

Exit codes:
    0 — printed the created directory path
    1 — could not allocate a run directory after retrying (contested on every attempt)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Final

_RUN_RE: Final = re.compile(r"^run-(\d{3,})$")
_MAX_ATTEMPTS: Final = 50


def next_index(pr_dir: Path) -> int:
    """Return one past the highest existing ``run-<NNN>`` index under ``pr_dir``.

    Args:
        pr_dir: The ``pr-<N>`` directory to scan for existing runs.

    Returns:
        1 when ``pr_dir`` has no run directories yet, otherwise the highest existing run number
        plus one. A malformed or non-directory sibling (an operator's stray file, a symlink) is
        ignored rather than raised on — it must never block allocation of the next run.
    """
    indexes = [
        int(match.group(1)) for child in pr_dir.glob("run-*") if child.is_dir() and (match := _RUN_RE.match(child.name))
    ]
    return max(indexes, default=0) + 1


def allocate_run_dir(pr_dir: Path) -> Path:
    """Create and return the next ``run-<NNN>`` directory under ``pr_dir``.

    Args:
        pr_dir: The ``pr-<N>`` directory to allocate a run under; created if absent.

    Returns:
        The newly created, previously-nonexistent run directory.

    Raises:
        RuntimeError: every attempt lost the exclusive-create race within ``_MAX_ATTEMPTS`` tries.
    """
    pr_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(_MAX_ATTEMPTS):
        candidate = pr_dir / f"run-{next_index(pr_dir):03d}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not allocate a run directory under {pr_dir} after {_MAX_ATTEMPTS} attempts")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 on success, 1 when allocation failed.
    """
    parser = argparse.ArgumentParser(
        prog="next_run_dir.py",
        description="Atomically allocate the next pr-<N>/run-<NNN> report directory.",
    )
    parser.add_argument("--pr-dir", required=True, type=Path, help="Parent pr-<N> directory (created if absent).")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    try:
        print(allocate_run_dir(args.pr_dir))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
