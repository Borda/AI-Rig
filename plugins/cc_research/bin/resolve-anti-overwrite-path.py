#!/usr/bin/env python3
"""resolve-anti-overwrite-path.py — print the first free "<dir>/<stem>.md", appending -2, -3, … when taken.

Implements the repo-wide anti-overwrite counter-suffix rule (quality-gates.md §Output Routing).
Usage: OUT=$(python resolve-anti-overwrite-path.py <dir> <stem>)
  <stem> carries any branch/date component already interpolated by the caller — this script
  never derives slugs, so callers keep whichever slug form their report path requires.
Pure path resolution: creates nothing. Callers keep their own `mkdir -p`.
Exit codes: 0 = path printed · 2 = missing/empty argument
"""

from __future__ import annotations

import sys
from pathlib import Path


# True when any `/`- or `\`-separated segment of `component` is `..` (path traversal).
def _has_traversal(component: str) -> bool:
    return any(part == ".." for part in component.replace("\\", "/").split("/"))


def main(argv: list[str]) -> int:
    directory = argv[1] if len(argv) > 1 else ""
    stem = argv[2] if len(argv) > 2 else ""
    if not directory:
        print("resolve-anti-overwrite-path: missing <dir> argument", file=sys.stderr)
        return 2
    if not stem:
        print("resolve-anti-overwrite-path: missing <stem> argument", file=sys.stderr)
        return 2
    if _has_traversal(directory) or _has_traversal(stem):
        print("resolve-anti-overwrite-path: '..' path traversal not allowed in <dir> or <stem>", file=sys.stderr)
        return 2

    # The printed value is joined textually rather than through pathlib: callers capture this
    # string verbatim as their report path, and pathlib would rewrite it ("." + "x" -> "x.md",
    # never "./x.md", and backslashes on Windows). pathlib still does the filesystem probing.
    out = f"{directory}/{stem}.md"
    counter = 2
    # exists(), not is_file(): a directory squatting on the target name must also push the
    # counter forward, else the caller's Write call fails on a path declared free here.
    while Path(out).exists():
        out = f"{directory}/{stem}-{counter}.md"
        counter += 1

    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
