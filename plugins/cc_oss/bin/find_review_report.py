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

``--report PATH --producer`` is the publication gate ``oss:review`` runs on its own freshly written report. On top of
the intake check it applies two producer rules: the header carries ``Review contract: 1`` (the current producer always
writes the version-one contract, so a legacy or marker-free header is a producer defect, never an archive), and the
``findings.jsonl`` rows are ones ``oss:resolve``'s merge accepts (``findings_problem``): every row carries every
``FINDING_FIELDS`` entry as non-empty text, with the JSON types the merge accepts and a severity in
``FINDING_SEVERITIES``, and no two rows share an id. A failure prints the same ``incomplete-review-report`` marker and
``[gate] reason:`` line as an intake failure and exits 1. A ``REJECT_`` gate skips the producer rules and keeps the
reject route (exit 1 without that marker). Without ``--producer`` intake is unchanged and still accepts a legacy
report.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --pr "$PR_NUMBER" --path-out "$SENTINEL"
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --report "$REPORT_DIR/review-report.md" --producer

``--assess`` grades every publication gap instead of stopping at the first, printing one JSON line: ``critical`` gaps
(a lost, unbacked or stale actionable obligation, a confirmed row resolve's merge would refuse, an ambiguous verdict, a
blocked/missing QA reviewer under a claimed-complete review) forbid consuming the report; ``degraded`` gaps (a wrong
readiness or aggregate-outcome label, another reviewer's coverage gap, open-concern bookkeeping, a legacy report) leave
the confirmed findings usable. A report the strict gate
refuses always yields at least one gap. It never edits the report. The line also carries ``head_moved``: ``true`` or
``false`` when the run's ``head-sha.txt`` holds a commit id, a PR number is known (``--pr``, else the header's ``PR:``)
and the PR's current head can be fetched; ``null`` otherwise. A moved head rules out a targeted ``--fill-gaps`` rerun.
``recorded_rejection`` echoes the header's ``Gate:`` field when it holds ``REJECT_`` (else ``""``), even when a
critical gap leaves ``gate`` empty; it never changes the exit code.

Exit codes:
    0 — no restriction, or the head moved since the rejection; with ``--assess``, no critical gap
    1 — rejected, incomplete report, non-numeric --pr, or failed report-path publication
    2 — with ``--assess`` only: at least one critical gap
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
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
#: A gate line resolve can act on (full match): PASS or BLOCK, or a ``REJECT_<GROUND>`` it re-checks against the PR
#: head. Anything else (``Gate: REJECTED``, a lowercase ``reject_scope``) is an ambiguous verdict, never a pass.
_ACTIONABLE_GATE_RE: Final = re.compile(r"Gate: (?:PASS|BLOCK)(?:\s+.*)?|Gate: REJECT_[A-Z]+(?:\s+.*)?")
#: A recorded commit id (full match): the abbreviated or full lowercase hex SHA a review run writes to ``head-sha.txt``.
COMMIT_ID_RE: Final = re.compile(r"[0-9a-f]{7,40}")
#: Fields every ``findings.jsonl`` row must carry as non-empty text before ``oss:resolve`` can merge it into an action
#: item. ``merge_action_items`` imports this list and ``finding_row_problem``, so the producer gate and the merge can
#: never disagree on what a mergeable row is.
FINDING_FIELDS: Final = ("id", "section", "severity", "title", "change", "author")
#: Severity words a ``findings.jsonl`` row may carry, compared after stripping and lower-casing exactly as the merge
#: normalizes them. ``merge_action_items`` scores these words and ``finding_row_problem`` refuses any other, so the
#: producer gate and the merge share one vocabulary; ``mint_finding_ids.SEVERITIES`` is pinned equal by a test.
FINDING_SEVERITIES: Final = ("critical", "high", "medium", "low", "cosmetic")


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
#: Candidate dispositions the version-one envelope accepts; ``None`` means feasible checks remain undone.
_DISPOSITIONS: Final = {"CONFIRMED", "REFUTED", "ALREADY_FIXED", "UNVERIFIED", "OPTIONAL", None}


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


class _GateRejected(Exception):
    """Carry the first broken publication rule out of the envelope checks.

    The reason travels to the ``incomplete-review-report`` message, so a producer (``oss:review``'s post-consolidation
    gate) or a consumer (``oss:resolve``) learns which rule failed without re-running the checks by hand. Deliberately
    not a ``ValueError`` subclass: malformed JSON and a broken contract rule must stay distinguishable.
    """


def _check_assessment(assessment: object, data: dict) -> bool:
    """Validate one assessment record against the aggregate readiness.

    A role must be a non-empty string: a number or a mapping can be neither told apart from the security reviewer nor
    named in a targeted rerun, and the gap grader already treats such a record as malformed.

    Returns:
        Whether the record is a completed undecidable (null-judgment) reviewer.

    Raises:
        _GateRejected: naming the first assessment rule the record breaks.
    """
    role = assessment.get("role") if isinstance(assessment, dict) else None
    if not isinstance(role, str) or not role or not _source_assessment(assessment):
        raise _GateRejected("assessment lacks role or a valid source judgment/readiness")
    if data["readiness"] == "complete" and assessment["readiness"] != "complete":
        raise _GateRejected(
            f"assessment {role!r} readiness={assessment['readiness']} under aggregate readiness=complete"
        )
    presence = assessment.get("presence")
    if presence == "missing":
        if (
            assessment["source_outcome"] is not None
            or assessment["readiness"] != "incomplete"
            or data["readiness"] != "incomplete"
        ):
            raise _GateRejected(f"missing assessment {role!r} requires null judgment and incomplete readiness")
        return False
    if presence != "completed":
        raise _GateRejected(f"assessment {role!r} presence must be completed or missing")
    if not all(assessment.get(key) for key in ("coverage", "rationale", "evidence")):
        raise _GateRejected(f"completed assessment {role!r} lacks coverage, rationale or evidence")
    # A single string is equivalent to a one-item list; only the type differs, not the meaning.
    if not isinstance(assessment.get("evidence"), (list, str)) or not isinstance(assessment.get("limits"), (list, str)):
        raise _GateRejected(f"assessment {role!r} evidence/limits not a list or string")
    if assessment["source_outcome"] is not None:
        return False
    if assessment["readiness"] != "blocked":
        raise _GateRejected(f"completed null assessment {role!r} must have readiness=blocked")
    return True


#: Strict reason for a null aggregate outcome whose readiness or reviewers cannot carry it. The completed null
#: assessment is required whichever readiness applies; only the readiness depends on whether anything is still owed.
_NULL_OUTCOME_RULE: Final = (
    "null aggregate outcome requires a completed null assessment in every case, plus readiness=incomplete when an "
    "assessment is missing or a candidate is pending (null disposition), otherwise readiness=blocked"
)


def _null_outcome_ready(readiness: object, completed_null: bool, owed: bool) -> bool:
    """Tell whether a null aggregate outcome fits the aggregate readiness and the records behind it.

    A null aggregate is a completed undecidable review, so it needs at least one completed undecidable reviewer and
    ``blocked`` readiness. Anything still owed makes the aggregate ``incomplete``, and that outranks ``blocked``: a
    ``presence: missing`` reviewer, or a pending candidate (null disposition, its feasible checks unfinished). The null
    aggregate must then be ``incomplete`` instead. Without this precedence a null outcome beside a missing reviewer or a
    pending candidate had no valid label, since each of those already requires ``incomplete`` on its own.

    Args:
        readiness: The aggregate ``readiness`` value.
        completed_null: Whether some assessment is a completed reviewer with a null judgment.
        owed: Whether some assessment is ``presence: missing`` or some candidate is pending.

    Returns:
        True when the null aggregate outcome is consistent.

    Examples:
        >>> _null_outcome_ready("blocked", True, False), _null_outcome_ready("incomplete", True, True)
        (True, True)
        >>> _null_outcome_ready("incomplete", True, False), _null_outcome_ready("blocked", True, True)
        (False, False)
        >>> _null_outcome_ready("blocked", False, False), _null_outcome_ready("incomplete", False, True)
        (False, False)
    """
    return completed_null and readiness == ("incomplete" if owed else "blocked")


def _pending_candidates(data: dict) -> list:
    """List the ids of pending candidates: records whose disposition is recorded as null.

    This is the condition under which ``_candidate_label_problem`` requires aggregate readiness ``incomplete``. A record
    without a disposition key is malformed and left to the candidate checks.

    Examples:
        >>> _pending_candidates({"candidates": [{"id": "V1", "disposition": None}, {"id": "V2"}, "V3"]})
        ['V1']
        >>> _pending_candidates({"candidates": "not a list"})
        []
    """
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        record.get("id")
        for record in candidates
        if isinstance(record, dict) and "disposition" in record and record["disposition"] is None
    ]


def _null_outcome_problem(data: dict, completed_null: bool, missing: bool) -> str:
    """Name why the aggregate outcome breaks the null-outcome rule, or return an empty string when it does not.

    Shared by the strict check and the gap grader so both apply one precedence. The reason names the first pending
    candidate, since that candidate, not a reviewer, is what forces ``incomplete``.
    """
    if data["source_outcome"] is not None:
        return ""
    pending = _pending_candidates(data)
    if _null_outcome_ready(data["readiness"], completed_null, missing or bool(pending)):
        return ""
    return f"{_NULL_OUTCOME_RULE} (pending candidate {pending[0]!r})" if pending else _NULL_OUTCOME_RULE


def _check_assessments(data: dict) -> None:
    """Keep completed undecidable inspection separate from missing required coverage.

    Reads ``data["candidates"]`` too, when it is a list: a pending candidate decides which readiness a null aggregate
    outcome needs. Every other candidate rule stays in ``_check_candidates``.

    Raises:
        _GateRejected: naming the first assessment rule the envelope breaks.
    """
    assessments = data.get("assessments")
    if not isinstance(assessments, list) or not assessments:
        raise _GateRejected("assessments missing or empty")
    completed_null = missing = False
    for assessment in assessments:
        completed_null = _check_assessment(assessment, data) or completed_null
        # The record passed its own checks, so it is a mapping whose presence is "completed" or "missing".
        missing = missing or assessment["presence"] == "missing"
    problem = _null_outcome_problem(data, completed_null, missing)
    if problem:
        raise _GateRejected(problem)


def _check_candidate(candidate: object, data: dict, reviewed_revision: str) -> None:
    """Validate one candidate record's evidence binding and its disposition-specific obligations.

    Raises:
        _GateRejected: naming the candidate and the first rule its record breaks.
    """
    if not isinstance(candidate, dict) or not all(
        candidate.get(key) for key in ("id", "claim", "revision", "contract", "trigger", "checks", "reason")
    ):
        raise _GateRejected("candidate lacks id, claim, revision, contract, trigger, checks or reason")
    identifier, disposition, revision = candidate["id"], candidate.get("disposition"), candidate["revision"]
    if not isinstance(revision, str) or (reviewed_revision and revision != reviewed_revision):
        raise _GateRejected(f"candidate {identifier!r} revision does not match reviewed head")
    if "disposition" not in candidate or disposition not in _DISPOSITIONS:
        raise _GateRejected(f"candidate {identifier!r} has unknown disposition {disposition!r}")
    if not isinstance(candidate.get("counterevidence"), list) or not isinstance(candidate.get("limits"), list):
        raise _GateRejected(f"candidate {identifier!r} counterevidence/limits not lists")
    checks = candidate["checks"]
    if not isinstance(checks, list) or not all(
        isinstance(check, dict)
        and all(check.get(key) for key in ("method", "result", "revision", "environment"))
        and isinstance(check.get("limits"), list)
        and check["revision"] == revision
        for check in checks
    ):
        raise _GateRejected(f"candidate {identifier!r} has an incomplete check or one bound to another revision")
    if type(candidate.get("feasible_checks_complete")) is not bool or candidate["feasible_checks_complete"] != (
        disposition is not None
    ):
        raise _GateRejected(f"candidate {identifier!r} feasible_checks_complete disagrees with its disposition")
    if disposition == "ALREADY_FIXED" and not all(candidate.get(key) for key in ("original_revision", "fix_evidence")):
        raise _GateRejected(f"ALREADY_FIXED candidate {identifier!r} lacks original_revision or fix_evidence")
    problem = _candidate_label_problem(candidate, data)
    if problem:
        raise _GateRejected(problem)


def _candidate_label_problem(candidate: dict, data: dict) -> str:
    """Name a readiness-label disagreement for an otherwise well-formed candidate, or return an empty string.

    Kept apart from the structural checks because gap assessment grades these differently: the candidate's evidence
    is intact, only the aggregate readiness label or the concern's capability note is wrong.
    """
    identifier, disposition = candidate["id"], candidate.get("disposition")
    if disposition is None and data["readiness"] != "incomplete":
        return f"null candidate {identifier!r} requires aggregate readiness=incomplete"
    if disposition == "UNVERIFIED" and not candidate.get("missing_capability"):
        return f"UNVERIFIED candidate {identifier!r} lacks missing_capability"
    if disposition in {None, "UNVERIFIED"} and data["readiness"] == "complete":
        return f"open candidate {identifier!r} ({disposition or 'null'}) under readiness=complete"
    return ""


def _check_candidates(data: dict, findings: list[dict], reviewed_revision: str = "") -> None:
    """Require evidence-bound candidates and exact separation of actionable and open obligations.

    Raises:
        _GateRejected: naming the first candidate, open-concern or finding-row rule the envelope breaks.
    """
    candidates, concerns = data.get("candidates"), data.get("open_concerns")
    if not isinstance(candidates, list) or not isinstance(concerns, list):
        raise _GateRejected("candidates/open_concerns not lists")
    seen_ids, open_ids, confirmed_ids = set(), set(), set()
    for candidate in candidates:
        _check_candidate(candidate, data, reviewed_revision)
        identifier, disposition = candidate["id"], candidate.get("disposition")
        if not isinstance(identifier, str) or identifier in seen_ids:
            raise _GateRejected(f"candidate id {identifier!r} not a unique string")
        seen_ids.add(identifier)
        if disposition == "CONFIRMED":
            confirmed_ids.add(identifier)
        if disposition in {None, "UNVERIFIED"}:
            open_ids.add(identifier)
    concern_ids = []
    for concern in concerns:
        if not isinstance(concern, dict) or not all(
            concern.get(key) for key in ("candidate_id", "owner", "next_check", "closure")
        ):
            raise _GateRejected("open concern lacks candidate_id, owner, next_check or closure")
        concern_ids.append(concern["candidate_id"])
    if set(concern_ids) != open_ids or len(concern_ids) != len(set(concern_ids)):
        raise _GateRejected(
            f"open_concerns {sorted(map(str, concern_ids))} must match open candidates {sorted(open_ids)} exactly once"
        )
    finding_candidates = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or type(finding.get("contract_version")) is not int
            or finding["contract_version"] != 1
            or finding.get("validation_status") != "CONFIRMED"
        ):
            raise _GateRejected("findings.jsonl row is not contract_version=1 validation_status=CONFIRMED")
        finding_candidates.append(finding.get("candidate_id"))
    missing = sorted(confirmed_ids - set(finding_candidates))
    if missing:
        raise _GateRejected(f"CONFIRMED candidate(s) without findings.jsonl row: {', '.join(missing)}")
    if set(finding_candidates) != confirmed_ids or len(finding_candidates) != len(set(finding_candidates)):
        raise _GateRejected("findings.jsonl rows must map one-to-one onto CONFIRMED candidates")


def _row_type_problem(finding: object, n: int) -> str | None:
    """Name the first field of a finding row whose JSON type the merge cannot use, or return ``None``.

    The merge feeds these fields to string operations (section slug, path normalization, summary) and uses the id as a
    key, so a number or a list in their place must refuse the merge, not raise mid-way.

    >>> _row_type_problem({"id": "a", "section": 5}, 3)
    'finding 3 field section is int, not text'
    >>> _row_type_problem(["a"], 1)
    'finding 1 is not a JSON object'
    """
    if not isinstance(finding, dict):
        return f"finding {n} is not a JSON object"
    # A null required field is reported as missing by the caller, not as a wrong type.
    for name in (*FINDING_FIELDS, "file"):
        if finding.get(name) is not None and not isinstance(finding[name], str):
            return f"finding {n} field {name} is {type(finding[name]).__name__}, not text"
    if finding.get("line") is not None and type(finding["line"]) not in (int, str):
        return f"finding {n} field line is {type(finding['line']).__name__}, not a number"
    return None


def finding_row_problem(finding: object, n: int) -> str | None:
    """Name why one ``findings.jsonl`` row cannot become an ``oss:resolve`` action item, or return ``None``.

    These are the row rules the merge applies before anything else, kept here so ``oss:review``'s producer gate refuses
    a row the merge would refuse, instead of delivering a report as finished that resolve then cannot merge: every
    ``FINDING_FIELDS`` entry is present as non-empty text, no field the merge reads has a JSON type it cannot use, and
    the severity is one of ``FINDING_SEVERITIES`` once stripped and lower-cased. The merge's own rules on contract
    version and validation status stay in the merge; ``findings_problem`` adds the cross-row id rule.

    Args:
        finding: One parsed ``findings.jsonl`` row.
        n: The row's 1-based position, named in the message.

    Returns:
        The problem, naming the row and the field, or ``None`` when the row is mergeable as far as these rules go.

    Examples:
        >>> row = {"id": "a-1", "section": "Architecture", "severity": "high", "title": "t", "change": "code"}
        >>> finding_row_problem(row, 2)
        'finding 2 missing author — run mint_finding_ids.py and complete the record'
        >>> finding_row_problem({**row, "author": "qa", "line": [1]}, 2)
        'finding 2 field line is list, not a number — the review must write it as one JSON object of text fields'
        >>> finding_row_problem({**row, "author": "qa", "severity": "major"}, 2)
        "finding 2 has unknown severity 'major' — use one of critical, high, medium, low, cosmetic"
        >>> finding_row_problem({**row, "author": "qa", "severity": " High "}, 2) is None
        True
    """
    type_problem = _row_type_problem(finding, n)
    if type_problem:
        return f"{type_problem} — the review must write it as one JSON object of text fields"
    if finding.get("merged_into") is not None and not isinstance(finding["merged_into"], str):
        # The survivor lookup keys on this value; a list or number would raise mid-merge instead of refusing it.
        kind = type(finding["merged_into"]).__name__
        return f"finding {n} field merged_into is {kind}, not text — it must name the surviving row's candidate_id"
    missing = [name for name in FINDING_FIELDS if not (finding.get(name) or "").strip()]
    if missing:
        return f"finding {n} missing {', '.join(missing)} — run mint_finding_ids.py and complete the record"
    if finding["severity"].strip().lower() not in FINDING_SEVERITIES:
        return f"finding {n} has unknown severity {finding['severity']!r} — use one of {', '.join(FINDING_SEVERITIES)}"
    return None


def duplicate_finding_id(findings: list) -> tuple[int, int] | None:
    """Find the first finding id two ``findings.jsonl`` rows share.

    The merge keys action items by finding id, so a repeated id would fold the later row into the earlier row's item and
    silently lose a confirmed finding. Ids compare exactly, as the merge compares them. A row that is not an object or
    carries no text id is ``finding_row_problem``'s to refuse, never counted here.

    Args:
        findings: Parsed ``findings.jsonl`` rows.

    Returns:
        The 1-based positions of the earlier and the later row, or ``None`` when no id repeats.

    Examples:
        >>> duplicate_finding_id([{"id": "a"}, {"id": "b"}, {"id": "a"}])
        (1, 3)
        >>> duplicate_finding_id([{"id": "a"}, {"id": ["a"]}, {"id": " "}, {"id": " "}, "a"]) is None
        True
    """
    first: dict[str, int] = {}
    for n, finding in enumerate(findings, 1):
        identifier = finding.get("id") if isinstance(finding, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        if identifier in first:
            return first[identifier], n
        first[identifier] = n
    return None


def _duplicate_id_problem(findings: list, positions: tuple[int, int]) -> str:
    """Word the refusal for the two rows ``duplicate_finding_id`` found sharing an id."""
    earlier, later = positions
    identifier = findings[later - 1]["id"]
    return (
        f"duplicate finding id {identifier} on findings {earlier} and {later} — run mint_finding_ids.py so every row "
        "has its own id"
    )


def findings_problem(
    findings: list, row_problem: Callable[[object, int], str | None] = finding_row_problem
) -> str | None:
    """Name why a ``findings.jsonl`` row set cannot become ``oss:resolve`` action items, or return ``None``.

    Applies ``row_problem`` to every row in order, then requires every id to be unique (``duplicate_finding_id``).
    ``oss:review``'s producer gate and the merge's verdict re-check use the shared row rule; the merge passes its own,
    which adds the contract and validation-status rules on top of it.

    Args:
        findings: Parsed ``findings.jsonl`` rows.
        row_problem: Per-row rule taking the row and its 1-based position.

    Returns:
        The first problem, naming the row or the two rows sharing an id, or ``None`` when the whole set is mergeable.

    Examples:
        >>> row = {"id": "a-1", "section": "API", "severity": "high", "title": "t", "change": "c", "author": "qa"}
        >>> findings_problem([row, {**row, "title": "u"}])
        'duplicate finding id a-1 on findings 1 and 2 — run mint_finding_ids.py so every row has its own id'
        >>> findings_problem([row, {**row, "id": "a-2"}]) is None
        True
    """
    problem = next((problem for n, row in enumerate(findings, 1) if (problem := row_problem(row, n))), None)
    if problem:
        return problem
    repeated = duplicate_finding_id(findings)
    return _duplicate_id_problem(findings, repeated) if repeated else None


def _check_envelope_header(data: object, fields: dict[str, str]) -> str:
    """Match the sidecar's version and aggregate judgment to the header, returning the bare gate.

    Raises:
        _GateRejected: naming the first header/envelope disagreement.
    """
    if (
        not isinstance(data, dict)
        or type(data.get("contract_version")) is not int
        or data["contract_version"] != 1
        or data.get("rating_scale") != "source-5-v1"
    ):
        raise _GateRejected("review-validation.json is not contract_version=1 rating_scale=source-5-v1")
    if not _source_assessment(data) or not data.get("readiness_reason"):
        raise _GateRejected("aggregate source judgment, readiness or readiness_reason invalid")
    outcome = _VERDICT_SYMBOL_RE.sub("", fields["Outcome"])
    if outcome != (data["source_outcome"] or "null") or fields.get("Readiness") != data["readiness"]:
        raise _GateRejected("header Outcome/Readiness disagree with review-validation.json")
    gate = fields["Gate"]
    if not gate.startswith("REVIEW_V1_"):
        raise _GateRejected("versioned header Gate lacks REVIEW_V1_ prefix")
    gate = gate.removeprefix("REVIEW_V1_")
    if re.fullmatch(r"(?:PASS|BLOCK)|REJECT_[A-Z]+ @[0-9a-f]{7,40}", gate) is None:
        raise _GateRejected(f"unrecognized Gate value {fields['Gate']!r}")
    if (data["source_outcome"] == "REJECT") != gate.startswith("REJECT_"):
        raise _GateRejected("REJECT outcome and REJECT_ gate must appear together")
    return gate


def _read_findings(report: Path) -> list:
    """Parse the ``findings.jsonl`` beside a report, one value per non-blank line, exactly as written.

    Raises:
        OSError: the file cannot be read.
        ValueError: a line is not JSON.
    """
    lines = report.with_name("findings.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _validation_gate(report: Path, fields: dict[str, str]) -> tuple[str, str]:
    """Validate the publication envelope before normalizing its legacy-incompatible gate prefix.

    Returns:
        ``(gate line, "")`` for a valid publication, or ``("", reason)`` naming the first broken rule.
    """
    if fields.get("Review contract") != "1" or fields.get("Rating scale") != "source-5-v1":
        return "", "header lacks Review contract: 1 / Rating scale: source-5-v1"
    try:
        data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
        findings = _read_findings(report)
        gate = _check_envelope_header(data, fields)
        head_file = report.with_name("head-sha.txt")
        reviewed_revision = head_file.read_text(encoding="utf-8").strip() if head_file.exists() else ""
        _check_assessments(data)
        _check_candidates(data, findings, reviewed_revision)
    except _GateRejected as exc:
        return "", str(exc)
    # RecursionError: a pathologically nested sidecar value is malformed input, never a traceback.
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        return "", f"unreadable or malformed sidecar ({type(exc).__name__}: {exc})"
    if data["readiness"] != "complete" and not gate.startswith("REJECT_"):
        gate = "BLOCK"
    return f"Gate: {gate}", ""


def gate_verdict(report: Path) -> tuple[str, str]:
    """Return the decision from a complete, unambiguous report header, or why there is none.

    Args:
        report: Path to a review report.

    Returns:
        ``(gate line, "")`` for a usable publication, or ``("", reason)`` naming why it is missing or incomplete.

    Examples:
        >>> from pathlib import Path
        >>> gate_verdict(Path("no-such-dir/review-report.md"))
        ('', 'report unreadable or absent')
    """
    try:
        lines = report.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", "report unreadable or absent"
    fields = _parse_header_fields(lines)
    if fields is None:
        return "", "header missing, unterminated, duplicated, or lacks Title/PR/Gate/Outcome/Summary"
    if {"Review contract", "Rating scale", "Readiness"} & fields.keys() or fields["Gate"].startswith("REVIEW_"):
        return _validation_gate(report, fields)
    outcome = _VERDICT_SYMBOL_RE.sub("", fields["Outcome"])
    if fields["Gate"].startswith("REJECT_"):
        if not outcome.startswith("N/A"):
            return "", "legacy REJECT_ gate requires Outcome N/A"
    elif _OUTCOME_VERDICT_RE.match(outcome) is None:
        return "", f"legacy Outcome {outcome!r} is not a verdict"
    return f"{_GATE_PREFIX} {fields['Gate']}", ""


def gate_line(report: Path) -> str:
    """Return the decision only from a complete, unambiguous report header.

    Args:
        report: Path to a review report.

    Returns:
        The normalized gate line, or an empty string for missing or incomplete publication.
    """
    return gate_verdict(report)[0]


def _producer_problem(report: Path) -> str:
    """Name the first producer rule a publication that passed intake still breaks, or return an empty string.

    Intake must keep reading archives, so it accepts a legacy header and checks rows only for the strict envelope rules.
    The producer always writes the version-one contract, and resolve's merge needs more from every row than the envelope
    rules ask, so ``oss:review`` holds its own fresh publication to both before delivering it.
    """
    try:
        fields = _parse_header_fields(report.read_text(encoding="utf-8", errors="replace").splitlines()) or {}
        if fields.get("Review contract") != "1":
            return (
                "header lacks Review contract: 1 — the current producer writes the version-one contract; a legacy or "
                "marker-free header is a producer defect, not an archive"
            )
        findings = _read_findings(report)
    except (OSError, ValueError, RecursionError) as exc:
        return f"unreadable or malformed sidecar ({type(exc).__name__}: {exc})"
    problem = findings_problem(findings)
    return f"findings.jsonl {problem}" if problem else ""


def publication_verdict(report: Path, producer: bool = False) -> tuple[str, str]:
    """Return the gate decision for a report, with the producer's own rules applied on top when asked.

    Args:
        report: Path to a review report.
        producer: Also require the version-one contract and merge-ready ``findings.jsonl`` rows, as ``oss:review``
            does for the report it just published. A ``REJECT_`` gate skips these rules: the producer never
            publishes a rejection as finished work, so it keeps the reject route whatever its rows hold.

    Returns:
        ``(gate line, "")`` when the report may be used, or ``("", reason)`` naming the first rule it breaks.

    Examples:
        >>> from pathlib import Path
        >>> publication_verdict(Path("no-such-dir/review-report.md"), producer=True)
        ('', 'report unreadable or absent')
    """
    line, reason = gate_verdict(report)
    # Same rejection test as _evaluate_gate: a row rule must not turn a consolidator-published rejection into an
    # incomplete-review-report, which the review would route to a row repair instead of its rejection handling.
    if producer and _ACTIONABLE_GATE_RE.fullmatch(line) and "REJECT_" not in line:
        problem = _producer_problem(report)
        if problem:
            return "", problem
    return line, reason


class GapSeverity(str, Enum):
    """How much a publication gap undermines what ``oss:resolve`` would act on."""

    #: The actionable set itself is untrustworthy: a lost or unbacked obligation, stale evidence, an ambiguous verdict.
    CRITICAL = "critical"
    #: The confirmed findings stand; only a label, coverage or bookkeeping obligation is wrong or missing.
    DEGRADED = "degraded"


@dataclass(frozen=True)
class Gap:
    """One broken publication rule, graded and attributed to the role or candidate it concerns."""

    rule: str
    severity: GapSeverity
    subject: str
    detail: str


@dataclass
class GapAssessment:
    """Every gap found in one report, plus the gate a degraded-only report can still be consumed under."""

    contract: str
    gate: str = ""
    gaps: list[Gap] = field(default_factory=list)
    #: The header's ``Gate:`` field when it records a ``REJECT_`` verdict, whatever the gaps; ``gate`` stays empty under
    #: a critical gap, so without this a consumer would never learn the report records a rejection.
    recorded_rejection: str = ""

    @property
    def critical(self) -> bool:
        """Whether any gap forbids consuming the report's valid parts."""
        return any(gap.severity is GapSeverity.CRITICAL for gap in self.gaps)

    def add(self, rule: str, severity: GapSeverity, subject: str, detail: str) -> None:
        """Record one gap."""
        self.gaps.append(Gap(rule, severity, subject, detail))

    def to_dict(self, report: Path) -> dict:
        """Serialize for the ``--assess`` CLI, enum values as plain strings."""
        return {
            "report": report.as_posix(),
            "contract": self.contract,
            "gate": self.gate,
            "critical": sum(gap.severity is GapSeverity.CRITICAL for gap in self.gaps),
            "degraded": sum(gap.severity is GapSeverity.DEGRADED for gap in self.gaps),
            "gaps": [{**asdict(gap), "severity": gap.severity.value} for gap in self.gaps],
            "recorded_rejection": self.recorded_rejection,
        }


#: Roles served by ``foundry:qa-specialist``, whose per-PR security scan (OWASP) is mandatory: a blocked or missing one
#: under a claimed-complete review is never a mere coverage gap. ``fill_gaps_scope`` routes the same roles to the QA
#: spawn unit through this one pattern, so the grader and the rerun can never disagree on which reviewer that is.
QA_ROLE_RE: Final = re.compile(r"\bqa\b|security|\btest", re.IGNORECASE)
#: Detail of the one ``readiness-mislabel`` gap that names a reviewer role rather than a candidate; fill-gaps triage
#: selects its subject as a role to re-run, so both sides match on this one string.
COMPLETED_NULL_MISLABEL: Final = "completed null must be blocked"


def _coverage_severity(role: str, data: dict) -> GapSeverity:
    """Grade a blocked or missing reviewer: critical only for the security role under a claimed-complete review."""
    claimed_complete = data.get("readiness") == "complete"
    return GapSeverity.CRITICAL if claimed_complete and QA_ROLE_RE.search(role) else GapSeverity.DEGRADED


def _assess_assessment(assessment: object, data: dict, result: GapAssessment) -> bool:
    """Grade one assessment record, returning whether it is a completed undecidable (null-judgment) reviewer.

    Mirrors the record rules of ``_check_assessments``. Only a disagreement with the aggregate readiness, or a completed
    undecidable reviewer not marked ``blocked``, is a label; a missing or broken record is critical.
    """
    role = assessment.get("role") if isinstance(assessment, dict) else None
    # A role that is not a name can be neither told apart from the security reviewer nor named in a rerun.
    subject = role if isinstance(role, str) and role else "-"
    if subject == "-" or not _source_assessment(assessment):
        result.add("assessment-malformed", GapSeverity.CRITICAL, subject, "no role or invalid source judgment")
        return False
    readiness, presence = assessment["readiness"], assessment.get("presence")
    if presence == "missing":
        if assessment["source_outcome"] is not None or readiness != "incomplete":
            result.add("assessment-malformed", GapSeverity.CRITICAL, subject, "missing needs null judgment, incomplete")
        result.add("assessment-missing", _coverage_severity(subject, data), subject, "reviewer output missing")
        return False
    if presence != "completed":
        result.add("assessment-malformed", GapSeverity.CRITICAL, subject, f"presence {presence!r}")
        return False
    if not all(assessment.get(key) for key in ("coverage", "rationale", "evidence")):
        result.add("assessment-unbacked", GapSeverity.CRITICAL, subject, "lacks coverage, rationale or evidence")
    elif not isinstance(assessment.get("evidence"), (list, str)) or not isinstance(
        assessment.get("limits"), (list, str)
    ):
        result.add("assessment-unbacked", GapSeverity.CRITICAL, subject, "evidence/limits not a list or string")
    if readiness != "complete":
        result.add("assessment-not-complete", _coverage_severity(subject, data), subject, f"readiness={readiness}")
    if assessment["source_outcome"] is not None:
        return False
    if readiness != "blocked":
        result.add("readiness-mislabel", GapSeverity.DEGRADED, subject, COMPLETED_NULL_MISLABEL)
    return True


def _assess_assessments(data: dict, result: GapAssessment) -> None:
    """Record malformed or unbacked assessments and every blocked, missing or mislabelled reviewer."""
    assessments = data.get("assessments")
    if not isinstance(assessments, list) or not assessments:
        result.add("assessments-missing", GapSeverity.CRITICAL, "-", "assessments missing or empty")
        return
    completed_null = missing = False
    for assessment in assessments:
        completed_null = _assess_assessment(assessment, data, result) or completed_null
        missing = missing or (isinstance(assessment, dict) and assessment.get("presence") == "missing")
    # Same precedence as the strict check: a missing reviewer or a pending candidate makes a null aggregate incomplete
    # instead of blocked.
    if _null_outcome_problem(data, completed_null, missing):
        result.add(
            "readiness-mislabel",
            GapSeverity.DEGRADED,
            "-",
            "null outcome needs a completed null, and incomplete with a missing reviewer or pending candidate, else "
            "blocked",
        )


def _assess_candidates(data: dict, reviewed_revision: str, result: GapAssessment) -> tuple[set, set, set]:
    """Grade each candidate record; return the known, open and confirmed candidate ids."""
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        result.add("candidates-missing", GapSeverity.CRITICAL, "-", "candidates not a list")
        return set(), set(), set()
    known, open_ids, confirmed = set(), set(), set()
    for candidate in candidates:
        identifier = candidate.get("id") if isinstance(candidate, dict) else None
        subject = identifier if isinstance(identifier, str) and identifier else "-"
        try:
            _check_candidate(candidate, data, reviewed_revision)
        except _GateRejected as exc:
            label = str(exc) == _candidate_label_problem(candidate, data) if subject != "-" else False
            rule, severity = (
                ("readiness-mislabel", GapSeverity.DEGRADED) if label else ("candidate-invalid", GapSeverity.CRITICAL)
            )
            result.add(rule, severity, subject, str(exc))
        else:
            if subject == "-":
                # A truthy non-string id (5, True, a list) passes the record check; only the id rule rejects it.
                result.add("candidate-invalid", GapSeverity.CRITICAL, "-", "candidate id not a string")
        if subject == "-":
            continue
        if subject in known:
            result.add("candidate-duplicate", GapSeverity.CRITICAL, subject, "candidate id repeated")
        known.add(subject)
        disposition = candidate.get("disposition")
        if disposition == "CONFIRMED":
            confirmed.add(subject)
        if disposition in {None, "UNVERIFIED"}:
            open_ids.add(subject)
    return known, open_ids, confirmed


def _assess_concerns(data: dict, known: set, open_ids: set, result: GapAssessment) -> None:
    """Grade open-concern bookkeeping: unknown candidates are critical, missing or extra entries degraded."""
    concerns = data.get("open_concerns")
    if not isinstance(concerns, list):
        result.add("concerns-missing", GapSeverity.DEGRADED, "-", "open_concerns not a list")
        return
    seen = set()
    for concern in concerns:
        target = concern.get("candidate_id") if isinstance(concern, dict) else None
        if not isinstance(concern, dict) or not all(
            concern.get(key) for key in ("candidate_id", "owner", "next_check", "closure")
        ):
            result.add("concern-incomplete", GapSeverity.DEGRADED, str(target or "-"), "owner/next_check/closure")
        if target is None:
            continue
        # Candidate ids are strings; any other target (a list, a number) names no candidate and must not be hashed.
        if not isinstance(target, str) or target not in known:
            result.add("concern-unknown-candidate", GapSeverity.CRITICAL, str(target), "no such candidate")
            continue
        if target not in open_ids or target in seen:
            result.add("concern-extra", GapSeverity.DEGRADED, target, "not open, or listed twice")
        seen.add(target)
    for missing in sorted(open_ids - seen):
        result.add("concern-missing", GapSeverity.DEGRADED, missing, "open candidate without an open concern")


def _recorded_subject(finding: dict, known: set) -> str:
    """Return a row's ``candidate_id`` when it names a recorded candidate, else ``-``; never hashes a non-string id."""
    target = finding.get("candidate_id")
    return target if isinstance(target, str) and target in known else "-"


def _assess_findings(findings: list, known: set, confirmed: set, result: GapAssessment) -> None:
    """Grade the actionable rows: any lost, unbacked, duplicated or unmergeable obligation is critical.

    A row resolve's merge would refuse is ``finding-unmergeable``: one ``finding_row_problem`` names, or an id another
    row repeats. Graded degraded-only, its report would be offered to proceed into a merge that blocks. The subject is
    the row's candidate when that candidate is recorded, so a targeted rerun re-validates it and thereby replaces the
    row; the row still counts as that candidate's row, so the same candidate never also grades ``finding-missing``.
    """
    seen = set()
    for n, finding in enumerate(findings, 1):
        if (
            not isinstance(finding, dict)
            or type(finding.get("contract_version")) is not int
            or finding["contract_version"] != 1
            or finding.get("validation_status") != "CONFIRMED"
        ):
            result.add("finding-invalid", GapSeverity.CRITICAL, "-", "row not contract v1 CONFIRMED")
            continue
        problem = finding_row_problem(finding, n)
        if problem:
            result.add("finding-unmergeable", GapSeverity.CRITICAL, _recorded_subject(finding, known), problem)
        target = finding.get("candidate_id")
        # A non-string candidate_id (a list, a number) backs no candidate and must not be hashed.
        if not isinstance(target, str) or target not in confirmed:
            result.add("finding-unbacked", GapSeverity.CRITICAL, str(target), "no matching CONFIRMED candidate")
            continue
        if target in seen:
            result.add("finding-duplicate", GapSeverity.CRITICAL, target, "candidate published twice")
        seen.add(target)
    repeated = duplicate_finding_id(findings)
    if repeated:
        later = findings[repeated[1] - 1]
        result.add(
            "finding-unmergeable",
            GapSeverity.CRITICAL,
            _recorded_subject(later, known),
            _duplicate_id_problem(findings, repeated),
        )
    for missing in sorted(confirmed - seen):
        result.add("finding-missing", GapSeverity.CRITICAL, missing, "CONFIRMED candidate without findings.jsonl row")


#: Stand-in text the residual check writes into a label it corrects; it lives only in a throwaway copy, never on disk.
_CORRECTED_LABEL: Final = "corrected for the residual check"


def _label_corrected(data: dict, readiness: str) -> dict:
    """Copy the envelope with every degraded-class label corrected, leaving each record's evidence untouched.

    The corrections are what gap assessment grades as a label or bookkeeping slip: the aggregate readiness, a completed
    undecidable reviewer's readiness, an UNVERIFIED candidate's missing-capability note, and the open-concern list,
    rebuilt from the open candidates. The aggregate outcome, the one further label, is varied on the returned copy by
    ``_residual_problem``. Called only when no critical gap was found, so ``assessments`` and ``candidates`` are lists.

    Only the levels it changes are copied (the envelope, each corrected record); every other value is shared with
    ``data``. A deep copy would recurse into arbitrarily nested evidence and overflow the interpreter stack on a
    pathological sidecar, while the strict checks never descend that far.
    """
    corrected = dict(data, readiness=readiness)
    corrected["assessments"] = [
        dict(assessment, readiness="blocked")
        if isinstance(assessment, dict)
        and assessment.get("presence") == "completed"
        and assessment.get("source_outcome") is None
        else assessment
        for assessment in data["assessments"]
    ]
    candidates, open_ids = [], []
    for candidate in data["candidates"]:
        # Tuple membership compares by equality, so an unhashable disposition cannot raise here.
        if isinstance(candidate, dict) and candidate.get("disposition") in (None, "UNVERIFIED"):
            if candidate.get("disposition") == "UNVERIFIED" and not candidate.get("missing_capability"):
                candidate = dict(candidate, missing_capability=_CORRECTED_LABEL)
            open_ids.append(candidate.get("id"))
        candidates.append(candidate)
    corrected["candidates"] = candidates
    corrected["open_concerns"] = [
        {"candidate_id": cid, "owner": _CORRECTED_LABEL, "next_check": _CORRECTED_LABEL, "closure": _CORRECTED_LABEL}
        for cid in open_ids
    ]
    return corrected


#: Judged aggregate outcome standing in for any judgment: the record rules tell a null outcome from a judged one, never
#: one judgment from another.
_JUDGED_STAND_IN: Final = "NEEDS_WORK"


def supported_outcomes(assessments: list) -> list[str | None]:
    """List the aggregate outcome kinds the completed reviewers support, null first.

    A null aggregate outcome needs a completed undecidable reviewer and a judged one a reviewer that judged; a missing
    reviewer supports neither. ``fill_gaps_scope`` chooses a rerun's aggregate outcome by the same rule, and the gap
    grader's residual check tries these outcomes when the recorded one is a mislabel.

    Args:
        assessments: Assessment records; entries that are not mappings are ignored.

    Returns:
        ``None`` (null outcome) and/or ``"NEEDS_WORK"`` (any judgment), in that order; empty when no completed reviewer
        supports either.

    Examples:
        >>> judged = {"presence": "completed", "source_outcome": "APPROVE"}
        >>> undecided = {"presence": "completed", "source_outcome": None}
        >>> missing = {"presence": "missing", "source_outcome": None}
        >>> supported_outcomes([judged]), supported_outcomes([undecided, missing]), supported_outcomes([missing])
        (['NEEDS_WORK'], [None], [])
    """
    completed = [record for record in assessments if isinstance(record, dict) and record.get("presence") == "completed"]
    outcomes: list[str | None] = []
    if any("source_outcome" in record and record["source_outcome"] is None for record in completed):
        outcomes.append(None)
    if any(record.get("source_outcome") is not None for record in completed):
        outcomes.append(_JUDGED_STAND_IN)
    return outcomes


def _residual_problem(data: dict, findings: list, reviewed_revision: str) -> str:
    """Name the strict record rule that no label correction satisfies, or return an empty string when one does.

    Both non-complete aggregate readiness values are tried: ``incomplete`` admits missing reviewers, pending candidates
    and a null aggregate outcome beside either, ``blocked`` admits a null aggregate outcome otherwise. The aggregate
    outcome is a label as well (a null one without a fitting reviewer grades ``readiness-mislabel``), so after the
    recorded outcome the other kind ``supported_outcomes`` allows is tried, as a recomputing consolidator would write
    it. A rule still broken under every choice is one gap assessment did not grade, so it never passes as a label slip.
    """
    recorded = data["source_outcome"]
    others = [outcome for outcome in supported_outcomes(data["assessments"]) if (outcome is None) != (recorded is None)]
    reasons = []
    for index, outcome in enumerate([recorded, *others]):
        stand_in = f"outcome={outcome or 'null'}, " if index else ""
        for readiness in ("incomplete", "blocked"):
            corrected = _label_corrected(data, readiness)
            corrected["source_outcome"] = outcome
            corrected["source_rating"] = None if outcome is None else _SOURCE_RATINGS[outcome]
            try:
                _check_assessments(corrected)
                _check_candidates(corrected, findings, reviewed_revision)
            except _GateRejected as exc:
                reasons.append(f"{stand_in}readiness={readiness}: {exc}")
            else:
                return ""
    return "; ".join(reasons)


def _assess_residual(
    data: dict, findings: list, reviewed_revision: str, strict_reason: str, result: GapAssessment
) -> None:
    """Keep the invariant that a report the strict gate refused never grades as consumable without a gap.

    Runs only when every graded gap is degraded: an ungraded record rule becomes a critical ``ungraded-rule`` gap, and
    a refusal no graded gap explains becomes one degraded ``readiness-mislabel`` gap carrying the strict reason.
    """
    if result.critical:
        return
    residual = _residual_problem(data, findings, reviewed_revision)
    if residual:
        result.add("ungraded-rule", GapSeverity.CRITICAL, "-", residual)
    elif not result.gaps:
        result.add("readiness-mislabel", GapSeverity.DEGRADED, "-", strict_reason)


def _assess_versioned(report: Path, fields: dict[str, str], result: GapAssessment, strict_reason: str) -> None:
    """Collect every gap of a version-one publication the strict gate refused, instead of stopping at the first."""
    if fields.get("Review contract") != "1" or fields.get("Rating scale") != "source-5-v1":
        result.add("envelope-header", GapSeverity.CRITICAL, "-", "header lacks Review contract: 1 / Rating scale")
    try:
        data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
        findings = _read_findings(report)
    except (OSError, ValueError) as exc:
        result.add("sidecar-unreadable", GapSeverity.CRITICAL, "-", f"{type(exc).__name__}: {exc}")
        return
    if not isinstance(data, dict) or data.get("contract_version") != 1 or not _source_assessment(data):
        result.add("contract-unsupported", GapSeverity.CRITICAL, "-", "not a valid contract_version=1 envelope")
        return
    try:
        gate = _check_envelope_header(data, fields)
    except _GateRejected as exc:
        result.add("envelope-header", GapSeverity.CRITICAL, "-", str(exc))
        gate = ""
    head_file = report.with_name("head-sha.txt")
    reviewed_revision = head_file.read_text(encoding="utf-8").strip() if head_file.exists() else ""
    _assess_assessments(data, result)
    known, open_ids, confirmed = _assess_candidates(data, reviewed_revision, result)
    _assess_concerns(data, known, open_ids, result)
    _assess_findings(findings, known, confirmed, result)
    _assess_residual(data, findings, reviewed_revision, strict_reason, result)
    if gate and not result.critical:
        result.gate = f"Gate: {gate if gate.startswith('REJECT_') else 'BLOCK'}"


def assess_report(report: Path) -> GapAssessment:
    """Grade every gap in a review publication so a consumer can offer a scoped choice instead of a bare block.

    Never edits and never raises on the report's content. A report the strict gate accepts yields no gaps; one it
    refuses yields at least one. A legacy report with a gate resolve can act on yields one degraded gap, since it
    carries no candidate, coverage or open-concern data to consume; any other legacy gate line is an ambiguous verdict
    and critical. A parsed header whose ``Gate:`` field holds ``REJECT_`` is echoed as ``recorded_rejection`` whatever
    the gaps, so a critical gap never hides that the report records a rejection.

    Args:
        report: Path to a review report.

    Returns:
        The graded gaps, the contract family, the gate under which a degraded-only report may be consumed, and any
        recorded rejection.

    Examples:
        >>> from pathlib import Path
        >>> assess_report(Path("no-such-dir/review-report.md")).critical
        True
    """
    line, reason = gate_verdict(report)
    try:
        fields = _parse_header_fields(report.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        fields = None
    if fields is None:
        result = GapAssessment("unknown")
        result.add("header-invalid", GapSeverity.CRITICAL, "-", "report unreadable or header incomplete")
        return result
    rejection = fields["Gate"] if "REJECT_" in fields["Gate"] else ""
    if not ({"Review contract", "Rating scale", "Readiness"} & fields.keys() or fields["Gate"].startswith("REVIEW_")):
        result = GapAssessment("legacy", recorded_rejection=rejection)
        if not line:
            result.add("legacy-header", GapSeverity.CRITICAL, "-", reason)
        elif _ACTIONABLE_GATE_RE.fullmatch(line) is None:
            result.add("legacy-gate-ambiguous", GapSeverity.CRITICAL, "-", f"unrecognized gate line {line!r}")
        else:
            result.gate = line
            result.add("legacy-contract", GapSeverity.DEGRADED, "-", "no candidate, coverage or open-concern data")
        return result
    result = GapAssessment("v1", recorded_rejection=rejection)
    if line:
        result.gate = line
        return result
    # The strict gate's exception families plus AttributeError and RecursionError (a pathologically nested value): a
    # malformed sidecar is a graded gap, never a traceback.
    try:
        _assess_versioned(report, fields, result, reason)
    except OSError as exc:
        result.add("sidecar-unreadable", GapSeverity.CRITICAL, "-", f"{type(exc).__name__}: {exc}")
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        result.add("sidecar-malformed", GapSeverity.CRITICAL, "-", f"{type(exc).__name__}: {exc}")
    return result


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


def _evaluate_gate(report: Path, pr_number: str, timeout: int, path_out: str, producer: bool = False) -> int:
    """Check the report's gate line and, on a rejection, whether the head has since moved.

    Args:
        report: Path to the resolved review report.
        pr_number: PR number without the leading ``#``.
        timeout: Maximum subprocess wait in seconds.
        path_out: Destination for the resolved report path, forwarded to ``_write_path_out``.
        producer: Also apply the producer rules (``publication_verdict``); a breach prints the same
            ``incomplete-review-report`` marker and ``[gate] reason:`` line as an intake failure.

    Returns:
        Exit code — 0 when resolve may proceed, 1 when the report boundary is blocked.
    """
    line, reason = publication_verdict(report, producer)
    if _ACTIONABLE_GATE_RE.fullmatch(line) is None:
        _write_path_out(path_out, None)
        print(
            f"⛔ BLOCKED — incomplete-review-report: {report}; finish or rerun /oss:review; upgrade oss:resolve when the review contract is unsupported before resolve"
        )
        print(f"[gate] reason: {reason or f'unrecognized gate line {line!r}'}")
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


def _nothing_to_gate(path_out: str, message: str) -> int:
    """Publish an empty report path and say why there is nothing to gate.

    Returns:
        0, or 1 when the empty path cannot be published (a stale sentinel could still name another report).
    """
    if not _write_path_out(path_out, None):
        return 1
    print(message)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line; ``--producer`` is accepted only with ``--report`` and without ``--assess``."""
    parser = argparse.ArgumentParser(
        prog="find_review_report.py",
        description="Enforce the oss:review reject gate for a PR before oss:resolve starts.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pr", default="", help="PR number (empty or 'n/a' skips the check).")
    source.add_argument("--report", type=Path, help="Validate the selected report-only input before consuming it.")
    parser.add_argument("--timeout", type=int, default=6, help="Max subprocess wait in seconds (default: 6).")
    parser.add_argument(
        "--assess",
        action="store_true",
        help="Print every graded gap and head_moved (true/false/null) as one JSON line instead of gating; exit 2 on "
        "any critical gap, else 0.",
    )
    parser.add_argument(
        "--producer",
        action="store_true",
        help="With --report: oss:review's publication gate. Also require the header's Review contract: 1 and every "
        "findings.jsonl row resolve's merge needs; a breach is an incomplete-review-report, exit 1.",
    )
    parser.add_argument(
        "--path-out",
        default="",
        help="File to write the resolved report path to (empty file when this PR has no report).",
    )
    args = parser.parse_args(argv)
    if args.producer and (args.report is None or args.assess):
        parser.error("--producer needs --report and cannot be combined with --assess")
    return args


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 when resolve may proceed, 1 when the report boundary is blocked or
        ``--pr`` is not a bare number.
    """
    args = _parse_args(argv)

    # Both streams carry gate verdicts and report paths that shell consumers decode as UTF-8; a Windows
    # console code page would otherwise encode "—" or a non-ASCII path as bytes no consumer can read.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    pr_number = args.pr.strip()
    if args.report is None and (not pr_number or pr_number == "n/a"):
        return _nothing_to_gate(args.path_out, "[gate] no PR number — reject-gate check skipped")

    if args.report is None and not _PR_NUMBER_RE.match(pr_number):
        _write_path_out(args.path_out, None)
        print(f"find_review_report: --pr must be digits only, got {pr_number!r}", file=sys.stderr)
        return 1

    report = args.report if args.report is not None else newest_report_for_pr(pr_number)
    if report is None:
        return _nothing_to_gate(args.path_out, f"[gate] no review report names PR #{pr_number} — no restriction")

    if args.assess:
        return _print_assessment(report, args.path_out, pr_number, args.timeout)
    return _evaluate_gate(report, pr_number, args.timeout, args.path_out, args.producer)


def _head_moved(report: Path, pr_number: str, timeout: int) -> bool | None:
    """Tell whether the PR head moved past the revision the review run recorded.

    A prefix match, as in the reject gate: ``head-sha.txt`` may hold an abbreviated SHA while ``gh`` returns the full
    one. ``gh`` is called only when both a recorded commit id and a PR number are known.

    Args:
        report: Path to the review report; ``head-sha.txt`` sits beside it.
        pr_number: PR number from ``--pr``; empty falls back to the digits of the report header's ``PR:`` field.
        timeout: Maximum ``gh`` wait in seconds.

    Returns:
        ``True`` or ``False`` when the recorded commit id, the PR number and the PR's current head are all known;
        ``None`` otherwise.

    Examples:
        >>> from pathlib import Path
        >>> _head_moved(Path("no-such-dir/review-report.md"), "7", 1) is None
        True
    """
    try:
        recorded = report.with_name("head-sha.txt").read_text(encoding="utf-8").strip()
        if not pr_number:
            fields = _parse_header_fields(report.read_text(encoding="utf-8", errors="replace").splitlines()) or {}
            match = re.fullmatch(r"#?\s*([0-9]+)", fields.get("PR", ""))
            pr_number = match.group(1) if match else ""
    except (OSError, ValueError):
        return None
    if not COMMIT_ID_RE.fullmatch(recorded) or not pr_number:
        return None
    current = current_head_sha(pr_number, timeout)
    return not current.startswith(recorded) if current else None


def _print_assessment(report: Path, path_out: str, pr_number: str = "", timeout: int = 6) -> int:
    """Emit the graded gaps; publish the report path only when its valid parts may be consumed.

    The reject gate's head-moved check is not evaluated here: callers run the normal gate first and assess only an
    ``incomplete-review-report``. An incomplete publication can still carry a ``REJECT_`` header with degraded-only
    gaps; its ``gate`` field then reads ``Gate: REJECT_…`` and the caller must treat it as blocked, never consumable.
    The ``head_moved`` key only informs the rerun choice (a moved head rules out ``--fill-gaps``); it never lifts a
    rejection and never changes the exit code.

    Returns:
        2 when any gap is critical, 1 when the path publication fails, else 0.
    """
    assessment = assess_report(report)
    payload = {**assessment.to_dict(report), "head_moved": _head_moved(report, pr_number, timeout)}
    print(json.dumps(payload, ensure_ascii=False))
    if assessment.critical:
        _write_path_out(path_out, None)
        return 2
    return 0 if _write_path_out(path_out, report) else 1


if __name__ == "__main__":
    sys.exit(main())
