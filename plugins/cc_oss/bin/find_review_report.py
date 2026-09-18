#!/usr/bin/env python
"""find_review_report.py — enforce the oss:review reject gate before oss:resolve starts fixing a PR.

Finds the newest run under ``.reports/review/pr-<N>/run-<NNN>/review-report.md`` for the given PR (falling back to a
pre-rename flat ``.reports/review/<timestamp>/`` report if no ``pr-<N>`` directory exists yet), reads its ``Gate:``
line, and blocks when that line still carries a ``REJECT_<GROUND>`` verdict. A rejection is a premise problem — wrong
goal, conduct, scope, licence, duplicate, revert, spam, or philosophy — and editing code cannot clear it.

The block is lifted only when the PR head has moved since the rejection was recorded: new state, so the ground may no
longer hold and the run continues with a warning. Every other outcome (no report, no ``Gate:`` line, ``PASS``,
``BLOCK``) imposes no restriction — those are ordinary findings that resolve exists to fix.

``--path-out FILE`` additionally publishes the resolved report path (empty file when the PR has none), so
``oss:resolve`` reuses this PR-scoped lookup for its report-merge step instead of running a second,
newest-of-any-PR glob of its own.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --pr "$PR_NUMBER" --path-out "$SENTINEL"

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

_GATE_PREFIX: Final = "Gate:"
_SHA_RE: Final = re.compile(r"@([0-9a-f]{7,40})")
_RUN_RE: Final = re.compile(r"^run-(\d+)$")
_LEGACY_REPORT_GLOB: Final = ".reports/review/*/review-report.md"


def _run_sort_key(run_dir: Path) -> tuple[int, str]:
    """Order run directories numerically, not lexically — ``run-1000`` must outrank ``run-999``."""
    match = _RUN_RE.match(run_dir.name)
    return (int(match.group(1)), run_dir.name) if match else (-1, run_dir.name)


def _legacy_report_for_pr(pr_number: str, base: Path) -> Path | None:
    """Fall back to the pre-rename flat ``.reports/review/<timestamp>/`` layout.

    Kept only for the ~30-day TTL window (``artifact-lifecycle.md``) during which a report written
    before the ``pr-<N>/run-<NNN>`` rename can still be on disk. This function backs the
    ``/oss:resolve`` reject gate — a miss here must never make a still-standing ``REJECT_<GROUND>``
    fail open just because the report predates the rename.

    Args:
        pr_number: PR number without the leading ``#``.
        base: Directory to search from.

    Returns:
        The newest matching legacy report, or ``None`` when none names this PR.
    """
    header = re.compile(rf"^PR: *#{re.escape(pr_number)}$")
    reports = sorted(base.glob(_LEGACY_REPORT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    for report in reports:
        try:
            lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(header.match(line) for line in lines):
            return report
    return None


def newest_report_for_pr(pr_number: str, root: Path | None = None) -> Path | None:
    """Return the highest-numbered run's report for ``pr_number``, falling back to legacy reports.

    Reports live at ``.reports/review/pr-<N>/run-<NNN>/review-report.md`` — the PR number is the
    directory itself, so no header parse is needed to match it; the run number orders runs
    deterministically without relying on mtime. A PR with no ``pr-<N>`` directory yet may still have
    a pre-rename flat-layout report on disk (see ``_legacy_report_for_pr``).

    Args:
        pr_number: PR number without the leading ``#``.
        root: Directory to search from; defaults to the current working directory.

    Returns:
        The matching report path, or ``None`` when this PR has no report at all.
    """
    base = root or Path.cwd()
    pr_dir = base / ".reports/review" / f"pr-{pr_number}"
    if pr_dir.is_dir():
        run_dirs = sorted((d for d in pr_dir.glob("run-*") if d.is_dir()), key=_run_sort_key, reverse=True)
        for run_dir in run_dirs:
            report = run_dir / "review-report.md"
            if report.is_file():
                return report
    return _legacy_report_for_pr(pr_number, base)


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


def _write_path_out(path_out: str, report: Path | None) -> None:
    """Publish the resolved report path so the caller reuses this lookup instead of re-globbing.

    The gate already resolves the newest report *for this PR*; ``oss:resolve`` previously parsed only the
    printed verdict and then ran its own newest-of-any-PR glob. Writing the path here gives both the reject
    gate and the report-merge step one PR-scoped answer.

    A write failure never changes the exit code: the gate's verdict is the load-bearing output, and the caller
    treats a missing or empty sentinel as "no report", falling back to its own lookup. It is reported on
    stderr rather than swallowed, because the caller may still hold a *stale* sentinel from an earlier run —
    the consumer's ``[ -f ]`` check catches a vanished report, but not a wrong one.

    Args:
        path_out: Destination file; no-op when empty.
        report: The resolved report, or ``None`` when this PR has none.

    Examples:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     out = Path(tmp) / "sentinel"
        ...     _write_path_out(str(out), None)
        ...     out.read_text(encoding="utf-8")
        ''
    """
    if not path_out:
        return
    try:
        Path(path_out).write_text(f"{report.as_posix()}\n" if report else "", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[gate] ⚠ could not write --path-out {path_out}: {exc} — a stale sentinel may remain", file=sys.stderr)


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
    parser.add_argument(
        "--path-out",
        default="",
        help="File to write the resolved report path to (empty file when this PR has no report).",
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    pr_number = args.pr.strip()
    if not pr_number or pr_number == "n/a":
        _write_path_out(args.path_out, None)
        print("[gate] no PR number — reject-gate check skipped")
        return 0

    report = newest_report_for_pr(pr_number)
    _write_path_out(args.path_out, report)
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
