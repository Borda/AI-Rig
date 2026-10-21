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
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

#: Matches a Jupytext percent-format cell marker line (`# %%`).
CELL_MARKER = re.compile(r"^# %%(\s|$)")
#: Matches a Jupytext cell marker that opens a markdown cell.
MARKDOWN_MARKER = re.compile(r"^# %%.*\[markdown\]")
#: Matches a line made only of `#` characters, which Jupyter renders as an empty heading.
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


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` via a sibling temp file and ``os.replace``.

    An interrupted write never leaves a truncated notebook behind. Newline translation and file mode match the
    ``Path.write_text`` rewrite this replaces (``mkstemp`` creates 0600, so the original mode is copied over).
    """
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        shutil.copymode(path, tmp_name)
        os.replace(tmp_name, path)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise


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
    cwd = Path.cwd().resolve()
    for path in args.files:
        # Only jupytext percent-format scripts, never through a symlink: an in-place rewrite of
        # an arbitrary argv path would clobber whatever file the link points at.
        if path.suffix != ".py" or path.is_symlink():
            print(f"{path}: skipped — not a regular .py jupytext script", file=sys.stderr)
            continue
        # The only caller (kaggle) passes its own .experiments/kaggle/<name>.py output; a path that resolves outside
        # the working tree (absolute, or `..` segments) is never a notebook this skill generated.
        if not path.resolve().is_relative_to(cwd):
            print(f"{path}: skipped — outside the working directory {cwd}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8")
        new_text, fixed = fix_text(text)
        if not fixed:
            continue
        total_fixed += fixed
        if args.check:
            print(f"{path}: {fixed} bare '#' heading-spacer line(s) in markdown cells")
        else:
            _write_atomic(path, new_text)
            print(f"{path}: cleared {fixed} bare '#' heading-spacer line(s)")

    if args.check and total_fixed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
