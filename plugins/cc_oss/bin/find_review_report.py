#!/usr/bin/env python
"""find_review_report.py — enforce the oss:review reject gate before oss:resolve starts fixing a PR.

Finds the newest ``.reports/review/*/review-report.md`` whose header names the given PR, reads its ``Gate:`` line, and
blocks when that line still carries a ``REJECT_<GROUND>`` verdict. A rejection is a premise problem — wrong goal,
conduct, scope, licence, duplicate, revert, spam, or philosophy — and editing code cannot clear it.

The block is lifted only when the PR head has moved since the rejection was recorded: new state, so the ground may no
longer hold and the run continues with a warning. Every other outcome (no report, no ``Gate:`` line, ``PASS``,
``BLOCK``) imposes no restriction — those are ordinary findings that resolve exists to fix.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --pr "$PR_NUMBER"

Exit codes:
    0 — no restriction, or the head moved since the rejection
    1 — the PR is still rejected at its current head
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

_REPORT_GLOB: Final = ".reports/review/*/review-report.md"
_GATE_PREFIX: Final = "Gate:"
_SHA_RE: Final = re.compile(r"@([0-9a-f]{7,40})")


def newest_report_for_pr(pr_number: str, root: Path | None = None) -> Path | None:
    """Return the most recently modified review report whose header names ``pr_number``.

    Args:
        pr_number: PR number without the leading ``#``.
        root: Directory to search from; defaults to the current working directory.

    Returns:
        The matching report path, or ``None`` when no report names this PR.
    """
    base = root or Path.cwd()
    header = re.compile(rf"^PR: *#{re.escape(pr_number)}$")
    reports = sorted(base.glob(_REPORT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    for report in reports:
        try:
            lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(header.match(line) for line in lines):
            return report
    return None


def gate_line(report: Path) -> str:
    """Return the report's first ``Gate:`` line, or an empty string when it has none.

    Args:
        report: Path to a review report.

    Returns:
        The full gate line, stripped of its trailing newline.
    """
    try:
        lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return next((line for line in lines if line.startswith(_GATE_PREFIX)), "")


def reject_sha(line: str) -> str:
    """Extract the commit SHA a rejection was recorded against.

    Args:
        line: A ``Gate:`` line, e.g. ``Gate: REJECT_SCOPE @a1b2c3d``.

    Returns:
        The bare SHA, or an empty string when the line carries none.

    Examples:
        >>> reject_sha("Gate: REJECT_SCOPE @a1b2c3d4e5")
        'a1b2c3d4e5'
        >>> reject_sha("Gate: PASS")
        ''
    """
    match = _SHA_RE.search(line)
    return match.group(1) if match else ""


def current_head_sha(pr_number: str, timeout: int) -> str:
    """Fetch the PR's current head commit.

    Args:
        pr_number: PR number without the leading ``#``.
        timeout: Maximum wait in seconds.

    Returns:
        The head SHA, or an empty string when it cannot be fetched.
    """
    cmd = ["gh", "pr", "view", pr_number, "--json", "headRefOid", "--jq", ".headRefOid"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 when resolve may proceed, 1 when the PR is still rejected.
    """
    parser = argparse.ArgumentParser(
        prog="find_review_report.py",
        description="Enforce the oss:review reject gate for a PR before oss:resolve starts.",
    )
    parser.add_argument("--pr", default="", help="PR number (empty or 'n/a' skips the check).")
    parser.add_argument("--timeout", type=int, default=6, help="Max subprocess wait in seconds (default: 6).")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    pr_number = args.pr.strip()
    if not pr_number or pr_number == "n/a":
        print("[gate] no PR number — reject-gate check skipped")
        return 0

    report = newest_report_for_pr(pr_number)
    if report is None:
        print(f"[gate] no review report names PR #{pr_number} — no restriction")
        return 0

    line = gate_line(report)
    if "REJECT_" not in line:
        print(f"[gate] {line or 'no Gate: field'} ({report}) — no restriction")
        return 0

    recorded = reject_sha(line)
    current = current_head_sha(pr_number, args.timeout)
    if recorded and current and recorded != current:
        print(
            f"⚠ PR #{pr_number} rejected ({line}), head moved {recorded}→{current} — state changed, proceeding. "
            f"Re-run /oss:review {pr_number} after to confirm the ground is gone."
        )
        return 0
    print(
        f"⛔ BLOCKED — PR #{pr_number} rejected ({line}), head unchanged (or unverifiable) — premise problem, "
        f"resolve can't fix it. Address the ground, then /oss:review {pr_number} again."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
