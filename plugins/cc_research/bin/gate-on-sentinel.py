#!/usr/bin/env python3
"""gate-on-sentinel.py — abort a phase unless a prior phase's sentinel holds the expected value.

Read-default-compare, the shape both phase gates in this plugin share. The <default> is what
a missing or empty sentinel means, and it is deliberately per-caller: fortify fails closed
(a missing judge verdict must not read as APPROVED) while verify fails open (V2 may legitimately
not have written a status yet).

Usage: python gate-on-sentinel.py <sentinel-path> <expected> <default> <fail-msg> || exit 1
  On mismatch prints <fail-msg> then "  actual: <value>" to stderr, so the message itself
  never has to interpolate the value it is complaining about.
Exit codes: 0 = value matches <expected> · 1 = mismatch (gate closed) · 2 = bad args
"""

from __future__ import annotations

import sys
from pathlib import Path


def _read_sentinel(path: Path) -> str:
    """First newline-terminated line of *path*; empty string otherwise.

    Mirrors `{ IFS= read -r VALUE < "$SENTINEL"; } 2>/dev/null || VALUE=""`: `read` exits non-zero on a final line with
    no trailing newline, and that `||` branch then wipes the partial value — so an unterminated line reads as empty here
    too and falls to <default>.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            line = handle.readline()
    except OSError:
        return ""
    return line[:-1] if line.endswith("\n") else ""


def main(argv: list[str]) -> int:
    """Abort a phase unless a prior phase's sentinel holds the expected value."""
    args = argv[1:]
    if len(args) != 4:
        print(
            f"gate-on-sentinel: expected 4 args (sentinel-path expected default fail-msg), got {len(args)}",
            file=sys.stderr,
        )
        return 2

    sentinel, expected, default, fail_msg = args
    if not sentinel:
        print("gate-on-sentinel: <sentinel-path> must not be empty", file=sys.stderr)
        return 2

    value = _read_sentinel(Path(sentinel)) or default
    if value == expected:
        return 0

    print(fail_msg, file=sys.stderr)
    print(f"  actual: {value}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
