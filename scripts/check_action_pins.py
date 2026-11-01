#!/usr/bin/env python3
"""Flag external GitHub Actions that are not pinned to a full commit SHA with a version comment.

A mutable tag or branch lets an upstream retag change what CI executes. The trailing ``# vX.Y.Z`` comment keeps the pin
reviewable and lets dependency tooling propose updates. A workflows directory without a single external action is also
reported: an empty scan (moved directory, changed ``uses:`` syntax) would otherwise pass vacuously. This is a hook
rather than a test because it inspects workflow text only: nothing in the product depends on it.

Usage: ``check_action_pins.py [--root DIR] [FILE...]``. Without files, every ``.github/workflows/*.yml|yaml`` below
``--root`` is checked. Local ``./`` actions are exempt. Exit 1 on any finding.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

USES_LINE = re.compile(r"^\s*-?\s*uses:\s*(?P<target>\S+?)(?:\s+#\s*(?P<comment>.+))?\s*$")
PINNED_ACTION = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^v\d+(?:\.\d+){0,2}$")


def check_text(text: str, name: str) -> tuple[int, list[str]]:
    """Return ``(external reference count, findings)`` for one workflow file's text.

    Examples:
        >>> sha = "a" * 40
        >>> check_text(f"      - uses: actions/checkout@{sha} # v4.1.0\\n", "ci.yml")
        (1, [])
        >>> check_text("      - uses: actions/checkout@v4\\n", "ci.yml")[1][0].split(": ")[0]
        'ci.yml:1'
        >>> check_text("      - uses: ./.github/actions/local\\n", "ci.yml")
        (0, [])
    """
    count = 0
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = USES_LINE.match(line)
        if match is None or match.group("target").startswith("./"):
            continue
        count += 1
        target, comment = match.group("target"), match.group("comment")
        location = f"{name}:{line_number}"
        if not PINNED_ACTION.fullmatch(target):
            findings.append(f"{location}: mutable or malformed action {target}; pin it to a full 40-hex commit SHA")
        if comment is None or not VERSION_COMMENT.fullmatch(comment):
            findings.append(
                f"{location}: missing human-readable version comment on {target}; append `# vX.Y.Z` after the SHA"
            )
    return count, findings


def main(argv: list[str] | None = None) -> int:
    """Check the given workflows (or all of them) and print findings; exit 1 when any exist."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    parser.add_argument("paths", nargs="*", type=Path, help="workflow files to check; default: all workflows")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    workflows = root / ".github" / "workflows"
    paths = [path if path.is_absolute() else root / path for path in args.paths]
    scan_all = not paths
    if scan_all:
        paths = sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml")))
    total = 0
    findings: list[str] = []
    for path in paths:
        name = path.resolve().relative_to(root).as_posix() if path.resolve().is_relative_to(root) else path.as_posix()
        count, found = check_text(path.read_text(encoding="utf-8"), name)
        total += count
        findings.extend(found)
    if scan_all and total == 0:
        findings.append(
            f"{workflows.relative_to(root).as_posix()}: no external action reference found; "
            "the workflow scan is vacuous (moved directory or changed `uses:` syntax)"
        )
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
