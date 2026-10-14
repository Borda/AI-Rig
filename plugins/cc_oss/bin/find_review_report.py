#!/usr/bin/env python
"""find_review_report.py — enforce the oss:review reject gate before oss:resolve starts fixing a PR.

Finds the newest run under ``.reports/review/pr-<N>/run-<NNN>/review-report.md`` for the given PR (falling back to a
pre-rename flat ``.reports/review/<timestamp>/`` report if no ``pr-<N>`` directory exists yet), reads its ``Gate:``
line, and blocks when that line still carries a ``REJECT_<GROUND>`` verdict. A rejection is a premise problem — wrong
goal, conduct, scope, licence, duplicate, revert, spam, or philosophy — and editing code cannot clear it.

The block is lifted only when the PR head has moved since the rejection was recorded: new state, so the ground may no
longer hold and the run continues with a warning. No report, or a complete ``PASS``/``BLOCK`` report, permits
the PR workflow to proceed. A newer missing, malformed, or incomplete publication blocks; it cannot erase a rejection.
Explicit ``--report`` intake applies the same completeness check to report-only mode; a rejection remains blocked
there until the user selects the PR-aware workflow or reruns the review.

``--path-out FILE`` additionally publishes the resolved report path (empty file when the PR has none), so
``oss:resolve`` reuses this PR-scoped lookup for its report-merge step instead of running a second,
newest-of-any-PR glob of its own.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --pr "$PR_NUMBER" --path-out "$SENTINEL"

Exit codes:
    0 — no restriction, or the head moved since the rejection
    1 — rejected, incomplete report, non-numeric --pr, or failed report-path publication
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

#: Label that prefixes the gate text in the one-line report summary.
_GATE_PREFIX: Final = "Gate:"
#: Extracts the abbreviated or full commit SHA recorded after ``@`` in a report field.
_SHA_RE: Final = re.compile(r"@([0-9a-f]{7,40})")
#: Matches review run directory names of the form ``run-<N>``.
_RUN_RE: Final = re.compile(r"^run-(\d+)$")
#: Accepts only digit-only PR numbers, guarding the path glob against injection.
_PR_NUMBER_RE: Final = re.compile(r"^[0-9]+$")
#: Glob for review reports stored in the older flat ``.reports/review/`` layout.
_LEGACY_REPORT_GLOB: Final = ".reports/review/*/review-report.md"
#: Matches the leading status symbol (check, warning or cross) on a report's Outcome field.
_VERDICT_SYMBOL_RE: Final = re.compile(r"^[✓⚠✗]\s*")
#: Verdict token must lead the field. Tolerated: any case, `_`/space/`-` between words, and trailing
#: detail or punctuation ("APPROVE — 0 critical", "needs work;"). Still rejected: a glued suffix
#: ("APPROVED"), a free-form verdict ("LGTM"), or an unfilled placeholder ("[review outcome]").
_OUTCOME_VERDICT_RE: Final = re.compile(r"^(?:APPROVE|NEEDS[ _-]WORK|REQUEST[ _-]CHANGES)(?![\w-])", re.IGNORECASE)


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
            if _RUN_RE.fullmatch(run_dir.name):
                # The newest allocated run owns the decision, even when its producer has not published yet.
                return run_dir / "review-report.md"
    return _legacy_report_for_pr(pr_number, base)


def _parse_header_fields(lines: list[str]) -> dict[str, str] | None:
    """Parse a report's leading ``---``-delimited header into a fields dict.

    Args:
        lines: The report's lines, already split but not yet validated as a header.

    Returns:
        The parsed ``key: value`` fields, or ``None`` when the header is missing, unterminated,
        carries a malformed or duplicate key, or lacks one of the required fields.
    """
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator or key.strip() in fields:
            return None
        fields[key.strip()] = value.strip()
    if not all(fields.get(key) for key in ("Title", "PR", "Gate", "Outcome", "Summary")):
        return None
    return fields


_SOURCE_RATINGS: Final = {"APPROVE": 1, "MINOR_EDITS": 2, "NEEDS_WORK": 3, "INSUFFICIENT_BUT_PROMISING": 4, "REJECT": 5}
_READINESS: Final = {"complete", "blocked", "incomplete"}


def _source_assessment(record: dict) -> bool:
    """Check the version-one nullable source judgment without treating absence as rejection."""
    outcome, rating = record.get("source_outcome"), record.get("source_rating")
    if "source_outcome" not in record or "source_rating" not in record:
        return False
    if record.get("readiness") not in _READINESS:
        return False
    if outcome is None:
        return rating is None and record["readiness"] in {"blocked", "incomplete"}
    return outcome in _SOURCE_RATINGS and type(rating) is int and rating == _SOURCE_RATINGS[outcome]


def _assessments_valid(data: dict) -> bool:
    """Keep completed undecidable inspection separate from missing required coverage."""
    assessments = data.get("assessments")
    if not isinstance(assessments, list) or not assessments:
        return False
    completed_null = False
    for assessment in assessments:
        if not isinstance(assessment, dict) or not assessment.get("role") or not _source_assessment(assessment):
            return False
        if data["readiness"] == "complete" and assessment["readiness"] != "complete":
            return False
        if assessment.get("presence") == "completed":
            if not all(assessment.get(key) for key in ("coverage", "rationale", "evidence")):
                return False
            # A single string is equivalent to a one-item list; only the type differs, not the meaning.
            if not isinstance(assessment.get("evidence"), (list, str)) or not isinstance(
                assessment.get("limits"), (list, str)
            ):
                return False
            if assessment["source_outcome"] is None:
                if assessment["readiness"] != "blocked":
                    return False
                completed_null = True
        elif assessment.get("presence") == "missing":
            if (
                assessment["source_outcome"] is not None
                or assessment["readiness"] != "incomplete"
                or data["readiness"] != "incomplete"
            ):
                return False
        else:
            return False
    return data["source_outcome"] is not None or (data["readiness"] == "blocked" and completed_null)


def _candidates_valid(data: dict, findings: list[dict], reviewed_revision: str = "") -> bool:
    """Require evidence-bound candidates and exact separation of actionable and open obligations."""
    candidates, concerns = data.get("candidates"), data.get("open_concerns")
    if not isinstance(candidates, list) or not isinstance(concerns, list):
        return False
    by_id, open_ids, confirmed_ids = {}, set(), set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not all(
            candidate.get(key) for key in ("id", "claim", "revision", "contract", "trigger", "checks", "reason")
        ):
            return False
        identifier, disposition = candidate["id"], candidate.get("disposition")
        revision = candidate["revision"]
        if not isinstance(revision, str) or (reviewed_revision and revision != reviewed_revision):
            return False
        if not isinstance(identifier, str) or identifier in by_id:
            return False
        if "disposition" not in candidate or disposition not in {
            "CONFIRMED",
            "REFUTED",
            "ALREADY_FIXED",
            "UNVERIFIED",
            "OPTIONAL",
            None,
        }:
            return False
        if not isinstance(candidate.get("counterevidence"), list) or not isinstance(candidate.get("limits"), list):
            return False
        checks = candidate["checks"]
        if not isinstance(checks, list) or not all(
            isinstance(check, dict)
            and all(check.get(key) for key in ("method", "result", "revision", "environment"))
            and isinstance(check.get("limits"), list)
            and check["revision"] == revision
            for check in checks
        ):
            return False
        if type(candidate.get("feasible_checks_complete")) is not bool or candidate["feasible_checks_complete"] != (
            disposition is not None
        ):
            return False
        if disposition == "ALREADY_FIXED" and not all(
            candidate.get(key) for key in ("original_revision", "fix_evidence")
        ):
            return False
        by_id[identifier] = candidate
        if disposition == "CONFIRMED":
            confirmed_ids.add(identifier)
        if disposition in {None, "UNVERIFIED"}:
            open_ids.add(identifier)
            if disposition is None and data["readiness"] != "incomplete":
                return False
            if disposition == "UNVERIFIED" and not candidate.get("missing_capability"):
                return False
            if data["readiness"] == "complete":
                return False
    concern_ids = []
    for concern in concerns:
        if not isinstance(concern, dict) or not all(
            concern.get(key) for key in ("candidate_id", "owner", "next_check", "closure")
        ):
            return False
        concern_ids.append(concern["candidate_id"])
    if set(concern_ids) != open_ids or len(concern_ids) != len(set(concern_ids)):
        return False
    finding_candidates = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or type(finding.get("contract_version")) is not int
            or finding["contract_version"] != 1
            or finding.get("validation_status") != "CONFIRMED"
        ):
            return False
        finding_candidates.append(finding.get("candidate_id"))
    return set(finding_candidates) == confirmed_ids and len(finding_candidates) == len(set(finding_candidates))


def _validation_gate(report: Path, fields: dict[str, str]) -> str:
    """Validate the publication envelope before normalizing its legacy-incompatible gate prefix."""
    if fields.get("Review contract") != "1" or fields.get("Rating scale") != "source-5-v1":
        return ""
    try:
        data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
        findings = [
            json.loads(line)
            for line in report.with_name("findings.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if (
            not isinstance(data, dict)
            or type(data.get("contract_version")) is not int
            or data["contract_version"] != 1
            or data.get("rating_scale") != "source-5-v1"
        ):
            return ""
        if not _source_assessment(data) or not data.get("readiness_reason"):
            return ""
        outcome = _VERDICT_SYMBOL_RE.sub("", fields["Outcome"])
        if outcome != (data["source_outcome"] or "null") or fields.get("Readiness") != data["readiness"]:
            return ""
        gate = fields["Gate"]
        if not gate.startswith("REVIEW_V1_"):
            return ""
        gate = gate.removeprefix("REVIEW_V1_")
        if re.fullmatch(r"(?:PASS|BLOCK)|REJECT_[A-Z]+ @[0-9a-f]{7,40}", gate) is None:
            return ""
        if (data["source_outcome"] == "REJECT") != gate.startswith("REJECT_"):
            return ""
        head_file = report.with_name("head-sha.txt")
        reviewed_revision = head_file.read_text(encoding="utf-8").strip() if head_file.exists() else ""
        if not _assessments_valid(data) or not _candidates_valid(data, findings, reviewed_revision):
            return ""
        if data["readiness"] != "complete" and not gate.startswith("REJECT_"):
            gate = "BLOCK"
        return f"Gate: {gate}"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def gate_line(report: Path) -> str:
    """Return the decision only from a complete, unambiguous report header.

    Args:
        report: Path to a review report.

    Returns:
        The normalized gate line, or an empty string for missing or incomplete publication.
    """
    try:
        lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    fields = _parse_header_fields(lines)
    if fields is None:
        return ""
    if {"Review contract", "Rating scale", "Readiness"} & fields.keys() or fields["Gate"].startswith("REVIEW_"):
        return _validation_gate(report, fields)
    outcome = _VERDICT_SYMBOL_RE.sub("", fields["Outcome"])
    if fields["Gate"].startswith("REJECT_"):
        if not outcome.startswith("N/A"):
            return ""
    elif _OUTCOME_VERDICT_RE.match(outcome) is None:
        return ""
    return f"{_GATE_PREFIX} {fields['Gate']}"


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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603 - argv list, no shell
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _write_path_out(path_out: str, report: Path | None) -> bool:
    """Publish the resolved report path so the caller reuses this lookup instead of re-globbing.

    The gate already resolves the newest report *for this PR*; ``oss:resolve`` previously parsed only the
    printed verdict and then ran its own newest-of-any-PR glob. Writing the path here gives both the reject
    gate and the report-merge step one PR-scoped answer.

    A write failure returns false and blocks the consumer: an earlier run's sentinel may still name a real but wrong
    report. A warning alone cannot establish the next consumer's input identity.

    Args:
        path_out: Destination file; no-op when empty.
        report: The resolved report, or ``None`` when this PR has none.

    Examples:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     out = Path(tmp) / "sentinel"
        ...     _ = _write_path_out(str(out), None)
        ...     out.read_text(encoding="utf-8")
        ''
    """
    if not path_out:
        return True
    try:
        Path(path_out).write_text(f"{report.as_posix()}\n" if report else "", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"[gate] ⚠ could not write --path-out {path_out}: {exc} — a stale sentinel may remain", file=sys.stderr)
        return False
    return True


def _evaluate_gate(report: Path, pr_number: str, timeout: int, path_out: str) -> int:
    """Check the report's gate line and, on a rejection, whether the head has since moved.

    Args:
        report: Path to the resolved review report.
        pr_number: PR number without the leading ``#``.
        timeout: Maximum subprocess wait in seconds.
        path_out: Destination for the resolved report path, forwarded to ``_write_path_out``.

    Returns:
        Exit code — 0 when resolve may proceed, 1 when the report boundary is blocked.
    """
    line = gate_line(report)
    if re.fullmatch(r"Gate: (?:PASS|BLOCK)(?:\s+.*)?|Gate: REJECT_[A-Z]+(?:\s+.*)?", line) is None:
        _write_path_out(path_out, None)
        print(
            f"⛔ BLOCKED — incomplete-review-report: {report}; finish or rerun /oss:review; upgrade oss:resolve when the review contract is unsupported before resolve"
        )
        return 1
    if not _write_path_out(path_out, report):
        return 1
    if "REJECT_" not in line:
        print(f"[gate] {line or 'no Gate: field'} ({report}) — no restriction")
        return 0

    recorded = reject_sha(line)
    current = current_head_sha(pr_number, timeout) if pr_number else ""
    # `recorded` may be an abbreviated SHA (_SHA_RE accepts 7-40 hex chars) while `current` is
    # always the full head SHA from `gh pr view --json headRefOid` — a prefix match is required,
    # never string equality (policy-sibling: review/SKILL.md's own reject-gate comment states the
    # same rule). String equality would read every abbreviated recorded SHA as "head moved" even
    # when it did not, failing the reject gate open.
    if recorded and current and not current.startswith(recorded):
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 when resolve may proceed, 1 when the report boundary is blocked or
        ``--pr`` is not a bare number.
    """
    parser = argparse.ArgumentParser(
        prog="find_review_report.py",
        description="Enforce the oss:review reject gate for a PR before oss:resolve starts.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pr", default="", help="PR number (empty or 'n/a' skips the check).")
    source.add_argument("--report", type=Path, help="Validate the selected report-only input before consuming it.")
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
    if args.report is None and (not pr_number or pr_number == "n/a"):
        if not _write_path_out(args.path_out, None):
            return 1
        print("[gate] no PR number — reject-gate check skipped")
        return 0

    if args.report is None and not _PR_NUMBER_RE.match(pr_number):
        _write_path_out(args.path_out, None)
        print(f"find_review_report: --pr must be digits only, got {pr_number!r}", file=sys.stderr)
        return 1

    report = args.report if args.report is not None else newest_report_for_pr(pr_number)
    if report is None:
        if not _write_path_out(args.path_out, None):
            return 1
        print(f"[gate] no review report names PR #{pr_number} — no restriction")
        return 0

    return _evaluate_gate(report, pr_number, args.timeout, args.path_out)


if __name__ == "__main__":
    sys.exit(main())
