#!/usr/bin/env python
"""Find deferred C1 files also touched by Phase 2 commits.

Purpose: Preserve resolve's file-disjointness guard before merging specialist commits. Scope: Normalize two newline-
delimited UTF-8 path lists with the host path rules; absolute paths become relative to the supplied repository root when
it is nonempty. Usage: ``python compare_deferred_files.py ROOT DIFF_FILE DEFERRED_FILE``. Outputs: Print overlapping
normalized deferred paths joined with spaces, in deferred order including duplicates. No overlap prints only a newline.
No files are written. Failure: Read or UTF-8 decoding errors propagate with exit 1, so callers distinguish an unreadable
list from no overlap. Invalid CLI arguments exit 2; success exits 0. Used by: ``skills/resolve/modes/dispatch-
phase3.md`` before the merge fence.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def norm(path: str, root: str) -> str:
    """Normalize a file-list entry relative to the repository root when available.

    Args:
        path: One file-list line, possibly blank or absolute.
        root: Repository root, or an empty string to retain absolute paths.

    Returns:
        The host-normalized path, or an empty string for blank input.

    Examples:
        >>> norm(" ./file.py ", "")
        'file.py'
        >>> norm("  ", "")
        ''
    """
    path = path.strip()
    if not path:
        return ""
    if os.path.isabs(path) and root:
        path = os.path.relpath(path, root)
    return os.path.normpath(path)


def main(argv: list[str] | None = None) -> int:
    """Print overlapping file paths without hiding input failures.

    Args:
        argv: Command-line arguments, or None to read the process arguments.

    Returns:
        Zero after printing the overlap.

    Raises:
        OSError: An input file cannot be read.
        UnicodeError: A file-list entry is not valid UTF-8.
    """
    parser = argparse.ArgumentParser(description="Compare Phase 2 changed files with deferred C1 files.")
    parser.add_argument("root")
    parser.add_argument("diff_file", type=Path)
    parser.add_argument("deferred_file", type=Path)
    args = parser.parse_args(argv)
    with args.diff_file.open(encoding="utf-8") as source:
        diff_set = {norm(line, args.root) for line in source if line.strip()}
    with args.deferred_file.open(encoding="utf-8") as source:
        overlap = [norm(line, args.root) for line in source if line.strip() and norm(line, args.root) in diff_set]
    print(" ".join(overlap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
