#!/usr/bin/env python
"""anti_overwrite_path.py — print the first free ``<dir>/<stem>[-<branch>-<date>][-N].md`` report path.

Implements the repo-wide anti-overwrite counter-suffix rule for ``.temp/output-*`` reports: a same-day
re-run must never replace an earlier report, so the counter starts at ``-2`` and climbs until the path is
free. ``--branch-date`` appends ``-<branch>-<YYYY-MM-DD>`` to the stem, where ``<branch>`` is
``git branch --show-current`` with ``/`` replaced by ``-`` (``main`` when empty, detached, or git is
unavailable) and the date is local time — the same slug the inline shell loops produced.

Pure path resolution: creates nothing (the Write tool creates parent directories). The path is printed
for the caller to read from the tool output, so no command substitution is involved.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/anti_overwrite_path.py" .temp output-test-impact --branch-date

Exit codes:
    0 — path printed
    2 — missing/empty argument or ``..`` path traversal in ``DIR`` or ``STEM``
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

_SCRIPT_NAME = "anti_overwrite_path"
_DEFAULT_BRANCH = "main"
#: ``git`` is fast; the bound only keeps a wedged repo from hanging the skill.
_GIT_TIMEOUT_SECONDS = 5


def has_traversal(component: str) -> bool:
    """Return ``True`` when any ``/``- or ``\\``-separated segment of ``component`` is ``..``.

    Examples:
        >>> has_traversal("../x"), has_traversal("a\\\\..\\\\b"), has_traversal(".temp"), has_traversal("a..b")
        (True, True, False, False)
    """
    return any(part == ".." for part in component.replace("\\", "/").split("/"))


def branch_slug() -> str:
    """Return the current git branch with ``/`` replaced by ``-``, ``main`` when unavailable.

    Examples:
        >>> isinstance(branch_slug(), str) and "/" not in branch_slug()
        True
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _DEFAULT_BRANCH
    branch = result.stdout.strip().replace("/", "-") if result.returncode == 0 else ""
    return branch or _DEFAULT_BRANCH


def first_free_path(directory: str, stem: str) -> str:
    """Return the first non-existing ``<directory>/<stem>[-N].md``, counting ``N`` up from 2.

    The path is joined textually (not through :mod:`pathlib`) because callers hand the printed string
    to the Write tool verbatim and pathlib would rewrite ``./x`` and, on Windows, the separators.
    ``exists()`` rather than ``is_file()``: a directory squatting on the name must also advance the
    counter, else the caller's Write fails on a path declared free here.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     first_free_path(d, "r") == f"{d}/r.md"
        True
        >>> with tempfile.TemporaryDirectory() as d:
        ...     _ = pathlib.Path(d, "r.md").write_text("x"); _ = pathlib.Path(d, "r-2.md").write_text("x")
        ...     first_free_path(d, "r") == f"{d}/r-3.md"
        True
    """
    out = f"{directory}/{stem}.md"
    counter = 2
    while Path(out).exists():
        out = f"{directory}/{stem}-{counter}.md"
        counter += 1
    return out


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser."""
    parser = argparse.ArgumentParser(prog=_SCRIPT_NAME, description="Print the first free anti-overwrite report path.")
    parser.add_argument("directory", metavar="DIR", help="Directory the report goes in, e.g. .temp")
    parser.add_argument("stem", metavar="STEM", help="Filename stem without extension, e.g. output-test-impact")
    parser.add_argument(
        "--branch-date",
        action="store_true",
        help="Append -<branch>-<YYYY-MM-DD> to STEM (branch slash replaced by '-', 'main' fallback; local date).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns the process exit code (see module docstring)."""
    args = _build_parser().parse_args(argv)
    if not args.directory or not args.stem:
        sys.stderr.write(f"{_SCRIPT_NAME}: DIR and STEM must be non-empty\n")
        return 2
    if has_traversal(args.directory) or has_traversal(args.stem):
        sys.stderr.write(f"{_SCRIPT_NAME}: '..' path traversal not allowed in DIR or STEM\n")
        return 2
    stem = args.stem
    if args.branch_date:
        stem = f"{stem}-{branch_slug()}-{datetime.date.today():%Y-%m-%d}"
    print(first_free_path(args.directory, stem))
    return 0


if __name__ == "__main__":
    sys.exit(main())
