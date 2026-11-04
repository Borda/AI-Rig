#!/usr/bin/env python
"""Print the PR number a saved oss:review report belongs to.

Purpose: Let resolve find the PR behind an auto-selected review report without a wildcard-bearing ``sed`` allow rule.
Scope: Read one review report and its path without modifying either; never fall back to the first digit run in the file,
so a title such as ``v2 parser`` cannot yield 2. Usage: ``python extract_review_pr.py REPORT``. Outputs: Print the PR
number alone on one line. The path wins (``pr-<N>/run-<NNN>/review-report.md``), then the first ``PR: #<N>`` line, then
the first ``## Code Review: PR #<N>`` heading. Failure: Exit 1 with no output when no source names a PR, and exit 1 when
the report cannot be read. Invalid CLI arguments exit 2. Used by: ``skills/resolve/SKILL.md`` Step 1 report selection.

Examples:
    python extract_review_pr.py .reports/review/pr-42/run-003/review-report.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: ``pr-<N>`` directory that oss:review writes its runs under.
_PATH_PR = re.compile(r"(?:^|/)pr-([1-9][0-9]*)/run-[^/]+/review-report\.md$")
#: A YAML ``PR:`` line or a ``## Code Review:`` heading, each naming the number with an optional ``PR`` and ``#``.
_HEADER_PR = re.compile(r"^(?:PR:[ \t]*|## Code Review: (?:PR )?)#?([1-9][0-9]*)(?:[ \t].*)?$")


def pr_from_path(report: Path) -> str | None:
    """Return the PR number encoded in a review report's directory names.

    Args:
        report: Path to the review report.

    Returns:
        The PR number as text, or None when the path is not an oss:review run path.

    Examples:
        >>> pr_from_path(Path(".reports/review/pr-42/run-003/review-report.md"))
        '42'
        >>> pr_from_path(Path("notes/review-report.md")) is None
        True
    """
    match = _PATH_PR.search(report.as_posix())
    return match.group(1) if match else None


def pr_from_text(text: str) -> str | None:
    """Return the first PR number a report names in a ``PR:`` line or ``## Code Review:`` heading.

    Args:
        text: Full report text.

    Returns:
        The PR number as text, or None when no such line names one.

    Examples:
        >>> pr_from_text("---\\nPR: #7\\n---\\n")
        '7'
        >>> pr_from_text("## Code Review: PR #19 parser v2\\n")
        '19'
        >>> pr_from_text("Title: v2 parser\\n") is None
        True
    """
    for line in text.splitlines():
        match = _HEADER_PR.match(line.rstrip("\r"))
        if match:
            return match.group(1)
    return None


def main(argv: list[str] | None = None) -> int:
    """Print the report's PR number, or exit 1 when none can be determined.

    Args:
        argv: Command-line arguments, or None to read the process arguments.

    Returns:
        Zero after printing a PR number, one when none is found or the report is unreadable.
    """
    parser = argparse.ArgumentParser(description="Print the PR number behind a saved oss:review report.")
    parser.add_argument("report", type=Path, help="path to the review report")
    args = parser.parse_args(argv)
    pr = pr_from_path(args.report)
    if pr is None:
        try:
            pr = pr_from_text(args.report.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return 1
    if pr is None:
        return 1
    print(pr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
