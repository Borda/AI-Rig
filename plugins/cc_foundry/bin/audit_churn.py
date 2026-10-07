#!/usr/bin/env python3
"""audit_churn.py — git-history recurrence signal for /foundry:audit (Layer 3).

Audit attention should follow measured churn: files and change-classes that keep
getting re-fixed are where the next defect most likely hides. This script reads
recent git history and emits a compact JSON signal — commit-type distribution,
top-churned files, and the most recurring fix-theme — that the audit report uses
to weight findings and surface "recurring fix class" rather than only point
findings.

Pure parsing (classify_commit / parse_churn / recurring_theme) is unit-tested;
``main`` shells out to ``git log`` and prints the JSON signal.

Usage:
    audit_churn.py [--limit 300] [--path plugins]

Exit 0 on success, 0 with an empty signal when git is unavailable (never blocks).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter

#: Matches the leading conventional-commit type, e.g. ``fix`` in ``fix(oss)!: ...``, capturing it as group 1.
_TYPE = re.compile(r"^(\w+)(?:\([^)]*\))?!?:")
#: Commit types counted as real categories; any other leading word is bucketed as ``other``.
_KNOWN = {"fix", "feat", "refactor", "perf", "test", "docs", "ci", "chore", "refine", "compress", "revert", "style"}


def classify_commit(subject: str) -> str:
    """Return the conventional-commit type of a subject line, or ``other``.

    Args:
        subject: A commit subject line (e.g. ``fix(oss): repair x``).

    Returns:
        The lowercased type when it is a known conventional type, else ``other``.

    Examples:
        >>> classify_commit("fix(oss): repair x")
        'fix'
        >>> classify_commit("refactor!: drop y")
        'refactor'
        >>> classify_commit("just some text")
        'other'
    """
    m = _TYPE.match(subject.strip())
    if m and m.group(1).lower() in _KNOWN:
        return m.group(1).lower()
    return "other"


def parse_churn(name_only: str, path_prefix: str | None = None) -> Counter[str]:
    """Count file occurrences across a ``git log --name-only`` dump.

    Args:
        name_only: Output of ``git log --name-only --pretty=format:`` (blank-line separated).
        path_prefix: When set, only count files under this prefix.

    Returns:
        A Counter mapping file path → number of commits that touched it.

    Examples:
        >>> dict(parse_churn("a.py\\nb.py\\n\\na.py\\n"))
        {'a.py': 2, 'b.py': 1}
        >>> dict(parse_churn("a.py\\nx/b.py\\n", "x/"))
        {'x/b.py': 1}
    """
    counts: Counter[str] = Counter()
    for line in name_only.splitlines():
        f = line.strip()
        if not f:
            continue
        if path_prefix and not f.startswith(path_prefix):
            continue
        counts[f] += 1
    return counts


def recurring_theme(types: Counter[str]) -> str:
    """Return a one-line hint naming the dominant recurring change class.

    Args:
        types: Counter of commit types.

    Returns:
        A short human hint (empty string when there is no history).

    Examples:
        >>> recurring_theme(Counter({"fix": 5, "feat": 1}))
        'fix dominates recent history (5/6) — audit the most-churned fix targets first'
        >>> recurring_theme(Counter())
        ''
    """
    total = sum(types.values())
    if not total:
        return ""
    top, n = types.most_common(1)[0]
    return f"{top} dominates recent history ({n}/{total}) — audit the most-churned {top} targets first"


def _git(args: list[str]) -> str:
    """Run a git command from repo root, returning stdout ('' on any failure)."""
    try:
        return subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", *args],  # noqa: S607 - git resolved via PATH on purpose
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Emit a git-churn recurrence signal for the audit")
    parser.add_argument("--limit", type=int, default=300, help="commits to analyze (default 300)")
    parser.add_argument("--path", help="restrict churn to files under this path prefix")
    args = parser.parse_args(argv)

    subjects = _git(["log", f"-{args.limit}", "--pretty=format:%s"])
    names = _git(["log", f"-{args.limit}", "--name-only", "--pretty=format:"])

    types: Counter[str] = Counter(classify_commit(s) for s in subjects.splitlines() if s.strip())
    churn = parse_churn(names, args.path)
    signal = {
        "commits_analyzed": sum(types.values()),
        "commit_types": dict(types.most_common()),
        "top_churn": [{"file": f, "commits": c} for f, c in churn.most_common(15)],
        "recurring_hint": recurring_theme(types),
    }
    print(json.dumps(signal, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
