#!/usr/bin/env python3
"""require-vars.py — assert that sentinel-derived values are non-empty, else abort the step.

Takes already-read VALUES, not variable names or sentinel paths: the caller's shell vars
are invisible to a child process, and the calling blocks need the values afterwards anyway,
so their `IFS= read -r` lines stay put. This replaces only the hand-written
`[ -z "$X" ] && { echo …; exit 1; }` chain, whose `&&`/`||` precedence is the documented
T-C1 footgun.

Usage: python require-vars.py <value> <fail-msg> [<value> <fail-msg> …] || exit 1
  Reports every empty value, not just the first, so one run surfaces all missing state.
Exit codes: 0 = all values non-empty · 1 = at least one empty · 2 = odd argument count
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    """Assert that sentinel-derived values are non-empty, else abort the step."""
    args = argv[1:]
    if len(args) < 2:
        print(f"require-vars: expected <value> <fail-msg> pairs, got {len(args)}", file=sys.stderr)
        return 2
    if len(args) % 2 != 0:
        print(
            f"require-vars: arguments must be <value> <fail-msg> pairs, got {len(args)} (odd)",
            file=sys.stderr,
        )
        return 2

    status = 0
    for value, fail_msg in zip(args[0::2], args[1::2]):
        if not value:
            print(fail_msg, file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
