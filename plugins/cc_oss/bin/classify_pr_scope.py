#!/usr/bin/env python
"""classify_pr_scope.py — classify a Pull Request as CHORE/FIX/REFACTOR/FEATURE/MIXED.

Deterministic classifier for the oss:review skill. Reads pre-computed PR signals
(Python file count, LOC delta, new public-API lines, labels, title) from argv
and emits the scope label on a single line of stdout — designed for direct
shell capture via ``SCOPE=$(python classify_pr_scope.py ...)``.

Classification rules (mirror of the original SKILL.md bash block, applied in order):

1. No non-config Python and zero Python files → ``CHORE``
2. New public-API lines (``src/**/__init__.py`` additions) > 0 → ``FEATURE``
3. Fewer than 3 Python files **and** total LOC delta < 50 → ``FIX``
4. 3 or more Python files → ``REFACTOR``
5. Anything else → ``MIXED``

A label or title keyword override applies after the small-change heuristic: if that heuristic
fires but PR labels or title carry a refactor/perf/cleanup signal, the verdict
is upgraded to ``REFACTOR`` so perf-optimizer and solution-architect still run.

Usage:
    classify_pr_scope.py --py-files <N> --loc-delta <N> --new-api-lines <N> \\
                        --labels "<csv>" --title "<str>"

Exit codes:
    0 — classification emitted
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import sys
from enum import Enum


class PRScope(str, Enum):
    """Scope verdict driving which oss:review specialists get dispatched.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``. Values are the uppercase labels
    the skill's bash blocks read off stdout, so the CLI surface is unchanged.
    """

    CHORE = "CHORE"
    FIX = "FIX"
    REFACTOR = "REFACTOR"
    FEATURE = "FEATURE"
    MIXED = "MIXED"


#: Substrings that, when present in PR labels (csv) or title, upgrade a FIX
#: verdict to REFACTOR — a short-diff change can still be a refactor when it
#: spans multiple concerns. Matched case-insensitively against the joined
#: ``labels + " " + title`` haystack so callers can pass either field.
_REFACTOR_SIGNAL_TOKENS: tuple[str, ...] = (
    "perf",
    "performance",
    "optimization",
    "refactor",
    "architecture",
    "cleanup",
    "rewrite",
)


def _has_refactor_signal(labels: str, title: str) -> bool:
    """Return True if labels or title carry a refactor/perf/cleanup signal.

    Match is case-insensitive substring across the joined ``labels + " " + title``
    haystack. Empty inputs return ``False``.

    Args:
        labels: Comma-separated PR label list (may be empty).
        title: PR title (may be empty).

    Returns:
        ``True`` when any token in :data:`_REFACTOR_SIGNAL_TOKENS` appears in the
        lowered combined string.

    Examples:
        >>> _has_refactor_signal("perf", "fix bug")
        True
        >>> _has_refactor_signal("bug", "fix typo")
        False
        >>> _has_refactor_signal("", "Refactor parser module")
        True
        >>> _has_refactor_signal("", "")
        False
    """
    haystack = f"{labels} {title}".lower()
    return any(token in haystack for token in _REFACTOR_SIGNAL_TOKENS)


def classify(
    py_files: int,
    loc_delta: int,
    new_api_lines: int,
    labels: str,
    title: str,
) -> PRScope:
    """Return the ``PRScope`` verdict for the supplied PR signals.

    Args:
        py_files: Count of changed ``.py`` files (any path).
        loc_delta: Total Python churn — added plus deleted lines, NOT net delta.
        new_api_lines: Added lines in ``src/**/__init__.py`` (new public exports).
        labels: Comma-separated PR labels (may be empty).
        title: PR title (may be empty).

    Returns:
        Scope verdict.

    Examples:
        >>> classify(0, 0, 0, "", "").value
        'CHORE'
        >>> classify(2, 10, 5, "", "Add public API").value
        'FEATURE'
        >>> classify(1, 20, 0, "", "fix small bug").value
        'FIX'
        >>> classify(5, 200, 0, "", "Restructure modules").value
        'REFACTOR'
        >>> classify(2, 30, 0, "perf", "Speed up loop").value
        'REFACTOR'
        >>> classify(2, 80, 0, "", "Some change").value
        'MIXED'
    """
    # Rule 1: no Python at all → CHORE (deps/tooling/config only)
    if py_files == 0:
        return PRScope.CHORE

    # Rule 2: new public exports → FEATURE
    if new_api_lines > 0:
        return PRScope.FEATURE

    # Rule 3: small diff → FIX (with refactor-signal override)
    if py_files < 3 and loc_delta < 50:
        if _has_refactor_signal(labels, title):
            return PRScope.REFACTOR
        return PRScope.FIX

    # Rule 4: many files → REFACTOR
    if py_files >= 3:
        return PRScope.REFACTOR

    # Rule 5: catch-all (e.g. 1–2 files with large LOC delta)
    return PRScope.MIXED


def main(argv: list[str] | None = None) -> int:
    """Classify pull-request scope from command-line metrics.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success. Argparse exits ``2`` for invalid arguments.

    No doctest is provided because behavior depends on command-line arguments; pytest covers it with ``capsys``.
    """
    parser = argparse.ArgumentParser(
        prog="classify_pr_scope.py",
        description="Classify a PR as CHORE/FIX/REFACTOR/FEATURE/MIXED for oss:review.",
    )
    parser.add_argument("--py-files", type=int, required=True, help="Count of changed .py files.")
    parser.add_argument("--loc-delta", type=int, required=True, help="Total Python churn (added + deleted lines).")
    parser.add_argument(
        "--new-api-lines",
        type=int,
        required=True,
        help="Added lines in src/**/__init__.py (new public exports).",
    )
    parser.add_argument("--labels", type=str, default="", help="Comma-separated PR labels (may be empty).")
    parser.add_argument("--title", type=str, default="", help="PR title (may be empty).")
    args = parser.parse_args(argv)

    scope = classify(
        py_files=args.py_files,
        loc_delta=args.loc_delta,
        new_api_lines=args.new_api_lines,
        labels=args.labels,
        title=args.title,
    )
    print(scope.value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
