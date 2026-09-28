#!/usr/bin/env python
"""fix_jupytext_blank_md.py — correction gate for notebook-style.md §Markdown blank lines.

jupytext percent-format markdown cells (``# %% [markdown]``) prefix every
source line with ``# ``. A blank line in the original markdown must stay a
truly empty line in the ``.py`` file — a bare ``#`` (or ``##``, ``###``, ...)
on its own renders as a literal character or an empty heading once the
markdown is viewed, instead of the intended blank line. Verified with
jupytext v1.x round-trip: a genuinely empty line inside a markdown block
still merges into the same cell (cells only split on ``# %%`` marker
lines), so clearing the stray hash-only line is safe.

Scoped to ``# %% [markdown]`` cells only — a bare ``#`` inside a code cell
is left untouched.

Usage::

    python fix_jupytext_blank_md.py FILE [FILE ...]
    python fix_jupytext_blank_md.py --check FILE [FILE ...]   # exit 1, no write

<!-- file: fix_jupytext_blank_md.py — consumers: kaggle/SKILL.md -->
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CELL_MARKER = re.compile(r"^# %%(\s|$)")
MARKDOWN_MARKER = re.compile(r"^# %%.*\[markdown\]")
BARE_HASH_LINE = re.compile(r"^#+\s*$")


def fix_text(text: str) -> tuple[str, int]:
    """Clear bare '#' heading-spacer lines inside markdown cells; return (text, count)."""
    lines = text.split("\n")
    in_markdown = False
    fixed = 0
    for i, line in enumerate(lines):
        if CELL_MARKER.match(line):
            in_markdown = bool(MARKDOWN_MARKER.match(line))
            continue
        if in_markdown and BARE_HASH_LINE.match(line):
            lines[i] = ""
            fixed += 1
    return "\n".join(lines), fixed


def main(argv: list[str] | None = None) -> int:
    """Fix blank markdown cells so they stay truly empty in jupytext percent format."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report violations without writing; exit 1 if any found",
    )
    args = parser.parse_args(argv)

    total_fixed = 0
    for path in args.files:
        text = path.read_text(encoding="utf-8")
        new_text, fixed = fix_text(text)
        if not fixed:
            continue
        total_fixed += fixed
        if args.check:
            print(f"{path}: {fixed} bare '#' heading-spacer line(s) in markdown cells")
        else:
            path.write_text(new_text, encoding="utf-8")
            print(f"{path}: cleared {fixed} bare '#' heading-spacer line(s)")

    if args.check and total_fixed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
