#!/usr/bin/env python
"""Compare a scheduled API removal version with a release target.

Purpose: Preserve the release audit's dependency-free numeric version comparison. Scope: Strip non-digit, non-dot
characters and compare the resulting integer tuples; this deliberately retains the inline implementation's behavior
rather than adopting SemVer. Usage: ``python compare_removal_version.py REMOVE_IN TARGET``. Outputs: Print exactly
``OVERDUE`` when the removal tuple is at most the target tuple, otherwise ``FUTURE``, followed by a newline. No files or
session state are written. Failure: Invalid CLI arguments exit 2; a successful comparison exits 0. Used by:
``skills/release/modes/audit.md`` when checking scheduled API removals.
"""

from __future__ import annotations

import argparse
import re
import sys


def parse_ver(version: str) -> tuple[int, ...]:
    """Extract the original audit's numeric tuple, defaulting to zero when no digits exist.

    Args:
        version: Version token from a removal marker or release target.

    Returns:
        Integer components without padding or prerelease interpretation.

    Examples:
        >>> parse_ver(' "v2.4rc1" ')
        (2, 41)
        >>> parse_ver("next")
        (0,)
    """
    version = version.strip('"v ')
    parts = [int(x) for x in re.sub(r"[^0-9.]", "", version).split(".") if x]
    return tuple(parts) if parts else (0,)


def main(argv: list[str] | None = None) -> int:
    """Print whether an API removal is due at the target release.

    Args:
        argv: Command-line arguments, or None to read the process arguments.

    Returns:
        Zero after printing the comparison result.
    """
    parser = argparse.ArgumentParser(description="Compare an API removal version with a release target.")
    parser.add_argument("remove_in")
    parser.add_argument("target")
    args = parser.parse_args(argv)
    rv, tv = parse_ver(args.remove_in), parse_ver(args.target)
    print("OVERDUE" if rv <= tv else "FUTURE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
