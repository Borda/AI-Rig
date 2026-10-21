#!/usr/bin/env python
"""resolve_review_target.py — resolve a review target and its changed Python files.

Extracted from ``skills/review/SKILL.md`` Step 1. One ``git diff`` capture feeds the target
line, the report-header warnings, and the no-Python early exit.

Everything this prints is read by the model; nothing is persisted, so there are no sentinels.
The ``--`` separator at the call site keeps argparse from reading a path that starts with a
dash as an option.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_review_target.py" [--committed] [--timeout SECS] -- "$REVIEW_ARGS"

``--committed`` swaps the working-tree diff for the last commit (``HEAD~1..HEAD``): a fresh ``--worktree`` checkout
starts clean at ``HEAD``, so ``git diff HEAD`` is empty there while the change under review is the committed one.

Exit codes:
    0 — a reviewable Python target was resolved
    1 — stop: the explicit path does not exist, or the target holds no Python file (the printed ``!`` line says which);
        the caller runs ``|| exit 1`` so the stop is enforced, not just printed
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

#: Directory entries examined by the Python-presence search before it stops and assumes Python exists.
_MAX_SCAN_ENTRIES = 200_000

#: Matches changed paths that are dependency manifests: pyproject.toml, setup.cfg and requirements files.
_DEPENDENCY_RE = re.compile(r"(pyproject\.toml|setup\.cfg|requirements.*\.txt)")
#: Matches changed paths that are container configuration: Dockerfile and docker-compose files.
_CONTAINER_RE = re.compile(r"(Dockerfile|docker-compose.*\.yml)")

#: Warning shown when the diff touches dependency manifests, which the review does not cover.
_DEPENDENCY_WARNING = "⚠ dependency changes detected — not reviewed; verify Python imports still resolve"
#: Warning shown when the diff touches container configuration, which the review does not cover.
_CONTAINER_WARNING = "⚠ container config changes detected — not reviewed"
#: Warning shown when every changed Python file is a test, so the review has no source change to examine.
_TESTS_ONLY_WARNING = "⚠ diff contains only test files (tests/) — no src/ changes; review may be uninformative"
#: Message printed when the diff contains no Python files, since the review skill is scoped to Python.
_NO_PYTHON = (
    "! Diff contains non-Python files only. This skill is scoped to Python. "
    "For other languages, use a general-purpose code reviewer."
)


def changed_files(timeout: int = 5, committed: bool = False) -> list[str]:
    """Return the diff against HEAD (or the last commit) as a list of paths.

    A failing or absent git returns an empty list, exactly as the ``2>/dev/null`` capture did.

    Args:
        timeout: Maximum seconds to wait for git.
        committed: Diff ``HEAD~1`` against ``HEAD`` instead of the working tree against ``HEAD``.

    Returns:
        Changed path strings, empty when git produced nothing.
    """
    cmd = ["git", "diff", "--name-only", *(["HEAD~1", "HEAD"] if committed else ["HEAD"])]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, git resolved via PATH on purpose
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.splitlines() if line]


def has_python_under(target: str) -> bool:
    """Report whether at least one ``.py`` file lives at or below ``target``.

    Mirrors ``find <target> -name '*.py' -type f``: a path that *is* a Python file counts, and a
    directory is searched recursively. ``Path.rglob`` alone would miss the file case.

    Args:
        target: Path given by the user.

    Returns:
        True when a Python file is reachable from ``target``.

    Examples:
        >>> has_python_under("definitely/not/here")
        False
    """
    path = Path(target)
    if path.is_file():
        return path.suffix == ".py"
    if path.is_dir():
        scanned = 0
        for _root, dirs, files in os.walk(path):
            if any(name.endswith(".py") for name in files):
                return True
            scanned += len(dirs) + len(files)
            if scanned > _MAX_SCAN_ENTRIES:
                return True  # a target such as `/` must not walk the whole filesystem; absence is unproven, so proceed
    return False


def collect_warnings(diff_files: list[str], diff_mode: bool) -> list[str]:
    """Build the non-Python warning lines for the report header.

    Args:
        diff_files: Paths from the working-tree diff.
        diff_mode: True when no explicit path was given, so the tests-only check applies.

    Returns:
        Warning lines in the order the inline block emitted them.

    Examples:
        >>> collect_warnings(["pyproject.toml"], False)[0].startswith("⚠ dependency")
        True
        >>> collect_warnings(["tests/test_a.py"], True)[-1].startswith("⚠ diff contains only test")
        True
    """
    warnings: list[str] = []
    if any(_DEPENDENCY_RE.search(f) for f in diff_files):
        warnings.append(_DEPENDENCY_WARNING)
    if any(_CONTAINER_RE.search(f) for f in diff_files):
        warnings.append(_CONTAINER_WARNING)
    if diff_mode:
        # Explicit tests/ path in REVIEW_ARGS is deliberate and never warned — diff mode only.
        py_diff = [f for f in diff_files if f.endswith(".py")]
        if py_diff and all(f.startswith("tests/") for f in py_diff):
            warnings.append(_TESTS_ONLY_WARNING)
    return warnings


def main(argv: list[str] | None = None) -> int:
    """Print the review target, the diff context, and any non-Python warnings.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` for a reviewable Python target, ``1`` when the path is missing or holds no Python file.
    """
    parser = argparse.ArgumentParser(
        prog="resolve_review_target.py",
        description="Resolve a review target and its changed Python files.",
    )
    parser.add_argument("review_args", nargs="?", default="", help="Explicit path to review; empty = diff mode.")
    parser.add_argument("--timeout", type=int, default=5, help="Max seconds to wait for git (default: 5).")
    parser.add_argument(
        "--committed",
        action="store_true",
        help="Diff mode reads the last commit (HEAD~1..HEAD) instead of the working tree — for a fresh worktree.",
    )
    args = parser.parse_args(argv)

    review_args = args.review_args.strip()
    diff_files = changed_files(args.timeout, args.committed)
    diff_mode = not review_args

    if diff_mode:
        print("\n".join(diff_files))
        py_count = sum(1 for f in diff_files if f.endswith(".py"))
        source = "last commit (HEAD~1..HEAD)" if args.committed else "working-tree diff"
        print(f"Reviewing: {source} ({py_count} Python files)")
        has_python = py_count > 0
    elif not Path(review_args).exists():
        print(f"! Review target not found: {review_args}")
        return 1
    else:
        print(f"Reviewing: {review_args}")
        has_python = has_python_under(review_args)

    warnings = collect_warnings(diff_files, diff_mode)
    # Warnings print before the early stop — they must emit even when no Python file exists.
    if not has_python:
        print(_NO_PYTHON)
    for line in warnings:
        print(line)
    return 0 if has_python else 1


if __name__ == "__main__":
    sys.exit(main())
