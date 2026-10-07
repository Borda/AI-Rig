#!/usr/bin/env python
"""check_fence_symmetry.py — Validate code fence pairing and nesting in .md files.

Detects two failure modes:
  1. Unclosed fence — opening ``` or ```lang with no matching closing ```.
  2. Bad nesting — inner fence uses same or more backticks than outer fence
     (outer must use ```` or more to wrap inner ```).

Deliberately NOT split into selectable subchecks, unlike check_tag_symmetry and
check_readme_drift. Those detect independently: tag-symmetry's three modes each scan
for their own pattern, and readme-drift's two read different sources (plugin.json vs
the bin/ listing). Here both modes are projections of ONE stack parse — a nesting
violation still pushes its malformed opener (see below), so it cascades into unclosed
findings for the same file. Verified: a file with one nesting violation emits that
violation PLUS two unclosed findings. Offering ``--check unclosed`` alone would show
those two findings with their actual cause filtered out of view.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_fence_symmetry.py" [files...] [--timeout SECS]

Output (stdout):
    One finding line per violation prefixed "! C14b:", or a single pass line.

Exit codes:
    0   all files pass
    1   one or more violations found

Examples:
    >>> import tempfile, pathlib
    >>> p = pathlib.Path(tempfile.mktemp(suffix=".md"))

    >>> # balanced pair passes
    >>> _ = p.write_text("```python\\ncode\\n```\\n")
    >>> check_file(p)
    []

    >>> # unclosed fence
    >>> _ = p.write_text("```python\\nno close\\n")
    >>> check_file(p)  # doctest: +ELLIPSIS
    ["...unclosed fence opened at line 1 with 3 backtick(s) ('```python')"]

    >>> # valid nesting: outer 4, inner 3
    >>> _ = p.write_text("````markdown\\n```python\\ncode\\n```\\n````\\n")
    >>> check_file(p)
    []

    >>> # invalid nesting: inner same backtick count as outer
    >>> _ = p.write_text("````outer\\n````inner\\ncode\\n````\\n````\\n")
    >>> check_file(p)  # doctest: +ELLIPSIS
    ['...nesting violation...']

    >>> import os; os.unlink(p)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Matches a fence delimiter line: optional leading whitespace, 3+ backticks, optional info string.
# Group 1 = backticks, group 2 = info string (stripped).
#: Matches a code fence line of three or more backticks, capturing the fence and any info string.
_FENCE_RE = re.compile(r"^\s*(`{3,})(.*?)$")


def check_file(path: Path) -> list[str]:
    """Return violation strings for path, empty list if clean.

    Args:
        path: Path to the .md file to check.

    Returns:
        List of human-readable violation messages with file path and line number.

    Examples:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mktemp(suffix=".md"))
        >>> _ = p.write_text("```python\\ncode\\n```\\n")
        >>> check_file(p)
        []
        >>> import os; os.unlink(p)
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path}: cannot read — {exc}"]

    # Stack entries: (line_number, backtick_count, info_string)
    stack: list[tuple[int, int, str]] = []
    violations: list[str] = []

    for lineno, line in enumerate(text.splitlines(), 1):
        m = _FENCE_RE.match(line)
        if not m:
            continue

        backticks, info = m.group(1), m.group(2).strip()
        count = len(backticks)

        if stack and info == "" and count == stack[-1][1]:
            # Closing fence — no info string, same backtick count as innermost open.
            stack.pop()
        else:
            # Opening fence — any fence line that is not a valid close is an opener.
            if stack:
                outer_count = stack[-1][1]
                if count >= outer_count:
                    violations.append(
                        f"{path}:{lineno}: nesting violation — inner fence uses {count} backtick(s) "
                        f"but outer fence (line {stack[-1][0]}) uses {outer_count}; "
                        f"inner must use fewer backticks than outer"
                    )
                    # Still push so subsequent lines parse relative to this malformed opener.

            stack.append((lineno, count, info))

    for open_lineno, open_count, open_info in stack:
        label = "`" * open_count + open_info
        violations.append(
            f"{path}:{open_lineno}: unclosed fence opened at line {open_lineno} "
            f"with {open_count} backtick(s) ({label!r})"
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    """Run fence symmetry check on provided files.

    Args:
        argv: Argument list; defaults to sys.argv[1:] when None.

    Returns:
        0 if all files pass, 1 if violations found.

    Examples:
        >>> main([])
        ✓: fence — no files provided
        0
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="*", help="Files to check (.md)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Timeout in seconds (default: 10; unused — pure file I/O)",
    )
    args = parser.parse_args(argv)

    if not args.files:
        print("✓: fence — no files provided")
        return 0

    all_violations: list[str] = []
    checked = 0

    for file_arg in args.files:
        p = Path(file_arg)
        if not p.is_file():
            continue
        checked += 1
        all_violations.extend(check_file(p))

    if all_violations:
        for v in all_violations:
            print(f"! C14b: {v}")
        return 1

    print(f"✓: Check 14b — no fence violations ({checked} files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
