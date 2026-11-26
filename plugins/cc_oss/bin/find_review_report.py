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

``--pr N --reuse`` looks for the last usable publication and prints ``PRIOR_REPORT=`` and ``PENDING_RUN=`` paths.
The pending path identifies a newer unpublished attempt that the review workflow may resume separately.
An allocated versioned run needs ``review-publication.published`` to establish that the parent released it.
The release helpers replace the pending marker with this record after checking the session's allocation, frozen head
and any fill-gaps carry. Detached archive inputs still use their existing envelope checks.

``--report PATH --producer`` is the publication gate ``oss:review`` runs on its own freshly written report. On top of
the intake check it applies two producer rules: the header carries ``Review contract: 1`` (the current producer always
writes the version-one contract, so a legacy or marker-free header is a producer defect, never an archive), and the
``findings.jsonl`` rows are ones ``oss:resolve``'s merge accepts (``findings_problem``): every row carries every
``FINDING_FIELDS`` entry as non-empty text, with the JSON types the merge accepts and a severity in
``FINDING_SEVERITIES``, and no two rows share an id. A failure prints the same ``incomplete-review-report`` marker and
``[gate] reason:`` line as an intake failure and exits 1. A ``REJECT_`` gate skips the producer rules and keeps the
reject route (exit 1 without that marker). Without ``--producer`` intake is unchanged and still accepts a legacy
report.

The producer gate also refuses the verdict states ``skills/review/modes/verdict-states.md`` marks ``producer``: an
UNVERIFIED candidate without a ``missing_capability_kind``, with one outside ``MISSING_CAPABILITY_KINDS``, or with
``package`` and no ``provisioning_failure`` (readiness blocked only by provisionable tooling); an ``APPROVE`` outcome
beside a CONFIRMED finding of a ``REQUIRED_CHANGE_SEVERITIES`` severity; and a header ``Confidence:`` above the
``READINESS_CONFIDENCE_CEILINGS`` value for its Readiness. The Step 2 lineup record ``lineup.md`` must sit beside the
report, and every spawned unit must name the exact roles that each need one completed or missing assessment. Every
unit marked ``dropped`` or ``promoted-unslotted`` needs its generated rerun line in Review Confidence. A
``promoted-unslotted`` unit forces readiness ``incomplete`` and needs a missing assessment for every named role.
``--fill-gaps`` names the one exemption: a fill-gaps run
writes no lineup record, and a candidate its ``fill-gaps.json`` carries verbatim (``carried.candidates``) may lack the
kind, since the run may not alter a record that predates the key; the plan must sit beside the report. An unparsable
confidence is not refused. Intake never applies these rules: saved reports carry none of them.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --pr "$PR_NUMBER" --path-out "$SENTINEL"
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --report "$REPORT_DIR/review-report.md" --producer
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --report "$REPORT" --producer --fill-gaps
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_review_report.py" --report "$REPORT" --rerun-hints

``--rerun-hints`` renders review commands from the launch lineup before the report exists.

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
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from _sentinel_io import write_private_text

#: Label that prefixes the gate text in the one-line report summary.
_GATE_PREFIX: Final = "Gate:"
#: A recorded commit id can be an abbreviation, a full SHA-1 or a full SHA-256 id.
_COMMIT_ID_PATTERN: Final = r"[0-9a-f]{64}|[0-9a-f]{7,40}"
_SHA_RE: Final = re.compile(rf"@({_COMMIT_ID_PATTERN})(?!\w)")
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
_OUTCOME_VERDICT_RE: Final = re.compile(
    r"^(?:APPROVE|MAJOR[ _-]REVISION|NEEDS[ _-]WORK|REQUEST[ _-]CHANGES)(?![\w-])", re.IGNORECASE
)
#: A gate line resolve can act on (full match): PASS or BLOCK, or a ``REJECT_<GROUND>`` it re-checks against the PR
#: head. Anything else (``Gate: REJECTED``, a lowercase ``reject_scope``) is an ambiguous verdict, never a pass.
_ACTIONABLE_GATE_RE: Final = re.compile(r"Gate: (?:PASS|BLOCK)(?:\s+.*)?|Gate: REJECT_[A-Z]+(?:\s+.*)?")
#: A recorded commit id (full match): the abbreviated or full lowercase hex SHA a review run writes to ``head-sha.txt``,
#: a full SHA-256 id included.
COMMIT_ID_RE: Final = re.compile(_COMMIT_ID_PATTERN)
#: Fields every ``findings.jsonl`` row must carry as non-empty text before ``oss:resolve`` can merge it into an action
#: item. ``merge_action_items`` imports this list and ``finding_row_problem``, so the producer gate and the merge can
#: never disagree on what a mergeable row is.
FINDING_FIELDS: Final = ("id", "section", "severity", "title", "change", "author")
#: Severity words a ``findings.jsonl`` row may carry, compared after stripping and lower-casing exactly as the merge
#: normalizes them. ``merge_action_items`` scores these words and ``finding_row_problem`` refuses any other, so the
#: producer gate and the merge share one vocabulary; ``mint_finding_ids.SEVERITIES`` is pinned equal by a test.
FINDING_SEVERITIES: Final = ("critical", "high", "medium", "low", "cosmetic")
#: Version of the Gate × Outcome × Readiness truth table in ``skills/review/modes/verdict-states.md``. A contract test
#: holds the two equal, so a changed table and a changed validator move together.
VERDICT_STATES_VERSION: Final = 1
#: Closed set of ``missing_capability_kind`` values an UNVERIFIED candidate may carry: an absent hardware class, a
#: package (or tool, or Python version) the review environment can provision, a decision only the user or a maintainer
#: can make, and state outside the review host.
MISSING_CAPABILITY_KINDS: Final = ("hardware", "package", "approval", "external")
#: Highest aggregate confidence a fresh publication may state for each non-complete Readiness. Ceilings only, never a
#: floor: ``verdict-states.md`` §Confidence ceilings lists them beside the ceilings only the consolidator can apply.
READINESS_CONFIDENCE_CEILINGS: Final = MappingProxyType({"blocked": 0.80, "incomplete": 0.70})
#: Severities that make a CONFIRMED finding a required source change, so an ``APPROVE`` outcome cannot stand beside it.
REQUIRED_CHANGE_SEVERITIES: Final = ("critical", "high")
#: Leading aggregate score of a header ``Confidence:`` field ("0.86 — key gaps"); anything else is left unchecked.
_CONFIDENCE_RE: Final = re.compile(r"(0(?:\.\d+)?|1(?:\.0+)?)(?![\d.])")
#: States a Step 2 ``lineup.md`` line may record for a unit (``modes/agent-launch.md``); ``spawned`` may carry
#: ``| promoted: <trigger>`` and the other two ``: <trigger>`` after the state word.
LINEUP_STATES: Final = ("spawned", "promoted-unslotted", "dropped")
_PENDING_PUBLICATION: Final = "review-publication.pending"
_PUBLISHED: Final = "review-publication.published"
_REFUSED_REPORT: Final = "review-report.refused.md"
#: Ranked spawn names for paired dimensions whose fill-gaps tasks use different task identifiers.
_MERGED_UNITS: Final = MappingProxyType({"review-docs-lint": "docs+lint", "review-perf-api": "perf+arch"})
#: Artifact names at the start of reasons that the parent can repair from launch evidence.
_PARENT_EVIDENCE: Final = frozenset(
    {"lineup.md", "fill-gaps.json", "head-sha.txt", "report-dir.txt", "review-mode.txt"}
)


def _run_sort_key(run_dir: Path) -> tuple[int, str]:
    """Order run directories numerically, not lexically — ``run-1000`` must outrank ``run-999``."""
    match = _RUN_RE.fullmatch(run_dir.name)
    return (int(match.group(1)), run_dir.name) if match else (-1, run_dir.name)


def _legacy_report_for_pr(pr_number: str, base: Path, *, usable: bool = False) -> Path | None:
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
        if not lines or lines[0].strip() != "---":
            continue
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration:
            continue
        if any(header.fullmatch(line) for line in lines[1:end]) and (not usable or _usable_publication(report)):
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
    runs = _pr_runs(base, pr_number)
    if runs:
        # Resolve must not let an unfinished run erase a standing rejection.
        return runs[0] / "review-report.md"
    return _legacy_report_for_pr(pr_number, base)


def _pr_runs(base: Path, pr_number: str) -> list[Path]:
    """Return allocated run directories in descending run number."""
    pr_dir = base / ".reports" / "review" / f"pr-{pr_number}"
    return sorted(
        (path for path in pr_dir.glob("run-*") if _RUN_RE.fullmatch(path.name) and path.is_dir()),
        key=_run_sort_key,
        reverse=True,
    )


def _usable_publication(report: Path) -> bool:
    """Require both an actionable intake gate and mergeable evidence to reuse a report."""
    return bool(_ACTIONABLE_GATE_RE.fullmatch(gate_line(report))) and not assess_report(report).critical


def _review_reuse(pr_number: str, path_out: str) -> int:
    """Return the last usable publication and identify the newest unpublished run ahead of it."""
    base = Path.cwd()
    report = pending = None
    for run in _pr_runs(base, pr_number):
        candidate = run / "review-report.md"
        if _usable_publication(candidate):
            report = candidate
            break
        if pending is None and (not candidate.is_file() or publication_state_problem(candidate)):
            pending = run
    report = report or _legacy_report_for_pr(pr_number, base, usable=True)
    if not _write_path_out(path_out, report):
        return 1
    print(f"PRIOR_REPORT={report.as_posix() if report else ''}")
    print(f"PENDING_RUN={pending.as_posix() if pending else ''}")
    return 0


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


_SOURCE_RATINGS: Final = {
    "APPROVE": 1,
    "MINOR_EDITS": 2,
    "MAJOR_REVISION": 3,
    "INSUFFICIENT_BUT_PROMISING": 4,
    "REJECT": 5,
}
#: Scale labels a report or sidecar may carry. ``source-5-v2`` names level 3 ``MAJOR_REVISION``; the historical
#: ``source-5-v1`` named it ``NEEDS_WORK``, and archived publications keep reading under it.
_SCALE_V1: Final = "source-5-v1"
_SCALE_V2: Final = "source-5-v2"
_KNOWN_SCALES: Final = {_SCALE_V1, _SCALE_V2}
_LEGACY_OUTCOMES: Final = {"NEEDS_WORK": "MAJOR_REVISION"}


def _scale_consistent(data: dict) -> bool:
    """Check that no outcome in a sidecar uses the level-3 name of the other scale version.

    Examples:
        >>> _scale_consistent({"rating_scale": "source-5-v2", "source_outcome": "MAJOR_REVISION"})
        True
        >>> _scale_consistent({"rating_scale": "source-5-v2", "source_outcome": "NEEDS_WORK"})
        False
        >>> _scale_consistent({"rating_scale": "source-5-v1", "source_outcome": "NEEDS_WORK"})
        True
    """
    assessments = data.get("assessments")
    outcomes = [data.get("source_outcome")]
    outcomes += (
        [item.get("source_outcome") for item in assessments if isinstance(item, dict)]
        if isinstance(assessments, list)
        else []
    )
    banned = "MAJOR_REVISION" if data.get("rating_scale") == _SCALE_V1 else "NEEDS_WORK"
    return banned not in outcomes


def source_rating(outcome: str) -> int:
    """Return the rating of a source outcome, reading the historical level-3 name as ``MAJOR_REVISION``.

    Examples:
        >>> source_rating("MAJOR_REVISION"), source_rating("NEEDS_WORK")
        (3, 3)
    """
    return _SOURCE_RATINGS[_LEGACY_OUTCOMES.get(outcome, outcome)]


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
    return outcome in {*_SOURCE_RATINGS, *_LEGACY_OUTCOMES} and type(rating) is int and rating == source_rating(outcome)


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
        or data.get("rating_scale") not in _KNOWN_SCALES
    ):
        raise _GateRejected(
            "review-validation.json is not contract_version=1 rating_scale=source-5-v2 (or source-5-v1)"
        )
    if not _scale_consistent(data) or fields.get("Rating scale") != data["rating_scale"]:
        raise _GateRejected("review-validation.json outcomes or the header Rating scale disagree with its rating_scale")
    if not _source_assessment(data) or not data.get("readiness_reason"):
        raise _GateRejected("aggregate source judgment, readiness or readiness_reason invalid")
    outcome = _VERDICT_SYMBOL_RE.sub("", fields["Outcome"])
    if outcome != (data["source_outcome"] or "null") or fields.get("Readiness") != data["readiness"]:
        raise _GateRejected("header Outcome/Readiness disagree with review-validation.json")
    gate = fields["Gate"]
    if not gate.startswith("REVIEW_V1_"):
        raise _GateRejected("versioned header Gate lacks REVIEW_V1_ prefix")
    gate = gate.removeprefix("REVIEW_V1_")
    if re.fullmatch(r"(?:PASS|BLOCK)|REJECT_[A-Z]+ @(?:[0-9a-f]{7,40}|[0-9a-f]{64})", gate) is None:
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
    lines = report.with_name("findings.jsonl").read_text(encoding="utf-8").split("\n")
    return [json.loads(line) for line in lines if line.strip()]


def _validation_gate(report: Path, fields: dict[str, str], producer: bool = False) -> tuple[str, str]:
    """Validate the publication envelope before normalizing its legacy-incompatible gate prefix.

    Returns:
        ``(gate line, "")`` for a valid publication, or ``("", reason)`` naming the first broken rule.
    """
    if fields.get("Review contract") != "1" or fields.get("Rating scale") not in _KNOWN_SCALES:
        return "", "header lacks Review contract: 1 / Rating scale: source-5-v2 (or source-5-v1)"
    try:
        data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
        findings = _read_findings(report)
        gate = _check_envelope_header(data, fields)
        current_producer = producer and fields.get("Rating scale") == _SCALE_V2
        reviewed_revision = _read_reviewed_revision(
            report, required=current_producer and not gate.startswith("REJECT_")
        )
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


def _read_reviewed_revision(report: Path, *, required: bool = False) -> str:
    """Read the frozen head and require a valid regular sidecar for a producer."""
    head = report.with_name("head-sha.txt")
    if required and (head.is_symlink() or not head.is_file()):
        raise _GateRejected("head-sha.txt missing or is not a regular file")
    try:
        revision = head.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        revision = ""
    except (OSError, ValueError) as exc:
        raise _GateRejected(f"head-sha.txt unreadable ({type(exc).__name__})") from exc
    if required and not COMMIT_ID_RE.fullmatch(revision):
        raise _GateRejected("head-sha.txt missing or does not contain a valid commit id")
    return revision


def _is_versioned(fields: dict[str, str]) -> bool:
    """Distinguish the envelope contract from a legacy report header."""
    return bool(
        {"Review contract", "Rating scale", "Readiness"} & fields.keys() or fields["Gate"].startswith("REVIEW_")
    )


def _allocated_pr(report: Path) -> str:
    """Return the PR of an allocated review directory, or an empty string for a detached report."""
    parts = report.parent.parts
    if parts[-4:-2] != (".reports", "review"):
        return ""
    pr_dir, run_dir = parts[-2:]
    pr = pr_dir.removeprefix("pr-")
    return pr if pr != pr_dir and _PR_NUMBER_RE.fullmatch(pr) and _RUN_RE.fullmatch(run_dir) else ""


def _needs_publication_record(report: Path) -> bool:
    """Require proof for an allocated versioned run, even if the pending marker predates this reader."""
    if not _allocated_pr(report) or not report.is_file():
        return False
    if report.with_name("review-validation.json").exists():
        return True
    fields = _parse_header_fields(report.read_text(encoding="utf-8").splitlines())
    return fields is not None and _is_versioned(fields)


def publication_state_problem(report: Path) -> str:
    """Require positive release evidence for versioned runs and refuse pending or parked drafts."""
    try:
        for path in {report.absolute(), report.resolve()}:
            if path.name == _REFUSED_REPORT:
                return "review publication was parked after refusal"
            pending = path.with_name(_PENDING_PUBLICATION)
            if pending.exists() or pending.is_symlink():
                return "review publication has not passed the parent gates"
            published = path.with_name(_PUBLISHED)
            if published.is_file() and not published.is_symlink():
                continue
            if path.with_name(_REFUSED_REPORT).is_file() or _needs_publication_record(path):
                return "review publication has no record of passing the parent gates"
    except (OSError, RuntimeError, ValueError) as exc:
        return f"review publication state unreadable ({type(exc).__name__})"
    return ""


def gate_verdict(report: Path, *, producer: bool = False) -> tuple[str, str]:
    """Return the decision from a complete, unambiguous report header, or why there is none.

    Args:
        report: Path to a review report.
        producer: Inspect a staged report while the parent still owns the publication marker.

    Returns:
        ``(gate line, "")`` for a usable publication, or ``("", reason)`` naming why it is missing or incomplete.

    Examples:
        >>> from pathlib import Path
        >>> gate_verdict(Path("no-such-dir/review-report.md"))
        ('', 'report unreadable or absent')
    """
    if not producer and (problem := publication_state_problem(report)):
        return "", problem
    try:
        lines = report.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return "", "report unreadable or absent"
    fields = _parse_header_fields(lines)
    if fields is None:
        return "", "header missing, unterminated, duplicated, or lacks Title/PR/Gate/Outcome/Summary"
    if _is_versioned(fields):
        return _validation_gate(report, fields, producer)
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


def _producer_problem(report: Path, fill_gaps: bool = False) -> str:
    """Name the first producer rule a publication that passed intake still breaks, or return an empty string.

    Intake must keep reading archives, so it accepts a legacy header and checks rows only for the strict envelope rules.
    The producer always writes the version-one contract, and resolve's merge needs more from every row than the envelope
    rules ask, so ``oss:review`` holds its own fresh publication to both before delivering it. Evidence a rule reads
    (the lineup record, a candidate's capability kind) is required, never skipped when absent; ``fill_gaps`` names the
    one documented exemption, read from the run's own ``fill-gaps.json``.
    """
    try:
        fields = _parse_header_fields(report.read_text(encoding="utf-8").splitlines()) or {}
        if fields.get("Review contract") != "1":
            return (
                "header lacks Review contract: 1 — the current producer writes the version-one contract; a legacy or "
                "marker-free header is a producer defect, not an archive"
            )
        if fields.get("Rating scale") != _SCALE_V2:
            return (
                f"header lacks Rating scale: {_SCALE_V2} — the current producer writes the current review scale; "
                f"{_SCALE_V1} is an archive-only reader"
            )
        findings = _read_findings(report)
        data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return f"unreadable or malformed sidecar ({type(exc).__name__}: {exc})"
    problem = findings_problem(findings)
    if problem:
        return f"findings.jsonl {problem}"
    carried, problem = _fill_gaps_carried(report) if fill_gaps else (frozenset(), "")
    return (
        problem
        or _verdict_state_problem(data, findings, fields, carried)
        or _lineup_record_problem(report, data, fill_gaps)
    )


def _fill_gaps_carried(report: Path) -> tuple[frozenset[str], str]:
    """Return the candidate ids a fill-gaps run carries verbatim from its prior run, or the problem reading them.

    The run allocation step copies the plan to ``fill-gaps.json`` beside the report; ``carried.candidates`` lists the
    candidate ids copied unchanged from the prior run. ``--fill-gaps`` without a readable plan is no exemption.

    Returns:
        ``(carried ids, "")``, or ``(frozenset(), problem)`` naming what is missing.
    """
    try:
        plan = json.loads(report.with_name("fill-gaps.json").read_text(encoding="utf-8"))
        revision = _read_reviewed_revision(report, required=True)
    except (OSError, ValueError, RecursionError, _GateRejected) as exc:
        return frozenset(), (
            f"fill-gaps.json beside the report is unreadable ({type(exc).__name__}) — restore the original plan copy"
        )
    if not isinstance(plan, dict) or plan.get("head_sha") != revision:
        return frozenset(), "fill-gaps.json head_sha does not match the frozen head-sha.txt"
    carried = plan.get("carried")
    ids = carried.get("candidates") if isinstance(carried, dict) else None
    if not isinstance(ids, list) or not all(isinstance(identifier, str) for identifier in ids):
        return (
            frozenset(),
            "fill-gaps.json has no carried.candidates list of ids — restore the original plan copy",
        )
    return frozenset(ids), ""


def _lineup_record_problem(report: Path, data: dict, fill_gaps: bool) -> str:
    """Hold the report to the Step 2 lineup record beside it; a fresh run without that record is refused.

    Only a fill-gaps run (``fill_gaps``) writes no lineup: its lineup is the plan's units, with no ranking or cap.
    """
    if fill_gaps:
        return ""
    try:
        entries = _read_lineup(report)
    except _GateRejected as exc:
        return str(exc)
    try:
        report_text = report.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return f"report unreadable for the lineup check ({type(exc).__name__}: {exc})"
    return _lineup_assessment_problem(entries, data) or _lineup_disclosure_problem(report, report_text, entries)


def _capability_kind_problem(data: dict, carried: frozenset[str] = frozenset()) -> str:
    """Name an UNVERIFIED candidate whose recorded missing capability cannot hold readiness blocked, or return "".

    Every UNVERIFIED candidate a fresh publication writes carries ``missing_capability_kind``: without it the
    provisionable-tooling rule below could be evaded by omitting the field. The one exemption is a candidate id in
    ``carried``, which a fill-gaps run copied verbatim from a run that predates the key and may not alter. A recorded
    kind must come from ``MISSING_CAPABILITY_KINDS``. A ``package`` kind names something the review environment can
    provision, so the candidate stays UNVERIFIED only beside a ``provisioning_failure`` naming the failed step; without
    one the check is feasible and undone, which is a null candidate and readiness ``incomplete``.

    Args:
        data: A ``review-validation.json`` envelope the intake rules already accepted.
        carried: Candidate ids a fill-gaps plan carries verbatim from its prior run.

    Returns:
        The problem, naming the candidate, or an empty string.

    Examples:
        >>> _capability_kind_problem({"candidates": [{"id": "V1", "disposition": "UNVERIFIED"}]}).split(" — ")[0]
        "UNVERIFIED candidate 'V1' carries no missing_capability_kind"
        >>> _capability_kind_problem({"candidates": [{"id": "V1", "disposition": "UNVERIFIED"}]}, frozenset({"V1"}))
        ''
        >>> _capability_kind_problem(
        ...     {"candidates": [{"id": "V1", "disposition": "UNVERIFIED", "missing_capability_kind": "package"}]}
        ... ).split(" — ")[0]
        "UNVERIFIED candidate 'V1' is blocked only by provisionable tooling (kind package, no provisioning_failure)"
        >>> _capability_kind_problem({"candidates": [{"id": "V1", "disposition": "UNVERIFIED",
        ...     "missing_capability_kind": "package", "provisioning_failure": "fetch: no cp313 wheel for x==1"}]})
        ''
    """
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict) or candidate.get("disposition") != "UNVERIFIED":
            continue
        if "missing_capability_kind" not in candidate:
            if candidate.get("id") in carried:
                continue
            return (
                f"UNVERIFIED candidate {candidate.get('id')!r} carries no missing_capability_kind — record one of "
                f"{', '.join(MISSING_CAPABILITY_KINDS)} (only a fill-gaps run's verbatim carried candidate may lack it)"
            )
        identifier, kind = candidate.get("id"), candidate["missing_capability_kind"]
        if kind not in MISSING_CAPABILITY_KINDS:
            return (
                f"UNVERIFIED candidate {identifier!r} has unknown missing_capability_kind {kind!r} — use one of "
                f"{', '.join(MISSING_CAPABILITY_KINDS)}"
            )
        failure = candidate.get("provisioning_failure")
        if kind == "package" and not (isinstance(failure, str) and failure.strip()):
            return (
                f"UNVERIFIED candidate {identifier!r} is blocked only by provisionable tooling (kind package, no "
                "provisioning_failure) — provision the environment and run the check, record the failed step in "
                "provisioning_failure, or record the candidate null with readiness incomplete"
            )
    return ""


def _approve_problem(data: dict, findings: list) -> str:
    """Name a CONFIRMED required source change published beside an ``APPROVE`` outcome, or return "".

    ``APPROVE`` beside blocked or incomplete readiness is a valid state (no required change, verification unfinished);
    beside a CONFIRMED finding of a ``REQUIRED_CHANGE_SEVERITIES`` severity it is not, since that finding is a required
    source change. Severity is read the way the merge reads it, case and surrounding spaces ignored. Only the
    publication gate applies this rule; it never decides which revision outcome replaces ``APPROVE``.

    Examples:
        >>> _approve_problem({"source_outcome": "APPROVE"}, [{"id": "f-1", "severity": " High "}]).split(" — ")[0]
        'APPROVE outcome beside CONFIRMED high finding f-1'
        >>> _approve_problem({"source_outcome": "MINOR_EDITS"}, [{"id": "f-1", "severity": "high"}])
        ''
        >>> _approve_problem({"source_outcome": "APPROVE"}, [{"id": "f-1", "severity": "medium"}])
        ''
    """
    if data.get("source_outcome") != "APPROVE":
        return ""
    for row in findings:
        severity = row.get("severity") if isinstance(row, dict) else None
        if isinstance(severity, str) and severity.strip().lower() in REQUIRED_CHANGE_SEVERITIES:
            return (
                f"APPROVE outcome beside CONFIRMED {severity.strip().lower()} finding {row.get('id')} — a required "
                "source change means MINOR_EDITS or MAJOR_REVISION by evidenced impact"
            )
    return ""


def _header_confidence(value: str) -> float | None:
    """Read the leading aggregate score of a header ``Confidence:`` field, or ``None`` when it has none.

    Examples:
        >>> _header_confidence("0.86 — mypy not reproduced"), _header_confidence("1.0"), _header_confidence("n/a")
        (0.86, 1.0, None)
        >>> _header_confidence("0.8.1 — malformed") is None
        True
    """
    match = _CONFIDENCE_RE.match(value.strip())
    return float(match.group(1)) if match else None


def _confidence_ceiling_problem(data: dict, fields: dict[str, str]) -> str:
    """Name a header ``Confidence:`` above the ceiling its Readiness allows, or return "".

    Examples:
        >>> _confidence_ceiling_problem({"readiness": "blocked"}, {"Confidence": "0.86 — gaps"}).split(" — ")[0]
        'header Confidence 0.86 exceeds the 0.80 ceiling for readiness=blocked'
        >>> _confidence_ceiling_problem({"readiness": "blocked"}, {"Confidence": "0.80 — no CUDA host"})
        ''
        >>> _confidence_ceiling_problem({"readiness": "complete"}, {"Confidence": "0.95"})
        ''
    """
    ceiling = READINESS_CONFIDENCE_CEILINGS.get(data.get("readiness"))
    value = _header_confidence(fields.get("Confidence", ""))
    if ceiling is None or value is None or value <= ceiling:
        return ""
    return (
        f"header Confidence {value:.2f} exceeds the {ceiling:.2f} ceiling for readiness={data['readiness']} — cap the "
        "aggregate at the ceiling and name that gap first (verdict-states.md §Confidence ceilings)"
    )


@dataclass(frozen=True)
class _LineupEntry:
    """Record one launch decision and the required assessment roles it names."""

    unit: str
    state: str
    trigger: str = ""
    roles: tuple[str, ...] = ()


def _lineup_roles(raw_roles: str) -> tuple[str, ...]:
    """Parse distinct role names that can be matched exactly to assessments."""
    roles = tuple(role.strip() for role in raw_roles.split(","))
    if "" in roles or len(set(roles)) != len(roles) or any(re.search(r'["`\\|]', role) for role in roles):
        raise ValueError("expected distinct, comma-separated role names")
    return roles


def _check_unslotted_roles(unit: str, roles: tuple[str, ...]) -> None:
    """Require names that the fill-gaps role map can route to the recorded unit."""
    # fill_gaps_scope imports this module's intake rules, so import the role map after module initialization.
    from fill_gaps_scope import FillGapsBlocked, map_role

    for role in roles:
        try:
            dimension = map_role(role)
        except FillGapsBlocked as exc:
            raise ValueError(f"unroutable role {role!r}") from exc
        standalone = dimension.file.rsplit("--", 1)[-1].removesuffix(".md")
        if unit not in {standalone, _MERGED_UNITS.get(dimension.unit)}:
            raise ValueError(f"role {role!r} does not belong to unit {unit!r}")


def _parse_lineup_entry(line: str) -> _LineupEntry:
    """Accept only the state and metadata forms the launch turn writes."""
    unit, _, details = map(str.strip, line.partition("|"))
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[+-][A-Za-z0-9]+)*", unit):
        raise ValueError("expected a launch unit name")
    if details == "dropped":
        return _LineupEntry(unit, "dropped")
    state, separator, raw_roles = details.rpartition(" | roles: ")
    roles = _lineup_roles(raw_roles) if separator else ()
    state = state if separator else details
    promoted = re.fullmatch(r"spawned \| promoted: (.+)", state)
    if state == "spawned" or promoted:
        return _LineupEntry(unit, "spawned", promoted[1].strip() if promoted else "", roles)
    trigger = state.removeprefix("promoted-unslotted: ").strip()
    if state.startswith("promoted-unslotted: "):
        if trigger and roles:
            _check_unslotted_roles(unit, roles)
            return _LineupEntry(unit, "promoted-unslotted", trigger, roles)
        raise ValueError("promoted-unslotted requires a trigger and `| roles: <roles>` from scope preselection")
    raise ValueError(f"expected one of {', '.join(LINEUP_STATES)}")


def _original_lineup(report: Path) -> str:
    """Require the original Step 2 record and validate all scope decisions before using it."""
    original = report.with_name("lineup.launch.md")
    try:
        if original.is_symlink():
            raise ValueError("symlink")
        text = original.read_bytes().decode("utf-8")
        _parse_lineup(text)
    except (OSError, ValueError, _GateRejected) as exc:
        raise _GateRejected(f"lineup.md original lineup.launch.md unavailable or invalid ({exc})") from exc
    return text


def _read_lineup(report: Path) -> list[_LineupEntry]:
    """Require the working lineup to match the original for a staged publication."""
    try:
        text = report.with_name("lineup.md").read_bytes().decode("utf-8")
    except (OSError, ValueError) as exc:
        raise _GateRejected(f"lineup.md unavailable ({type(exc).__name__}) — restore the Step 2 launch record") from exc
    original = report.with_name("lineup.launch.md")
    if original.exists() or report.with_name(_PENDING_PUBLICATION).exists():
        if text != _original_lineup(report):
            raise _GateRejected("lineup.md differs from the original Step 2 lineup.launch.md")
    return _parse_lineup(text)


def _parse_lineup(text: str) -> list[_LineupEntry]:
    """Parse an entire lineup and reject absent, duplicate or malformed units."""
    entries: dict[str, _LineupEntry] = {}
    roles: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = _parse_lineup_entry(line)
            if entry.unit in entries:
                raise ValueError(f"duplicate unit {entry.unit!r}")
            if repeated := roles.intersection(entry.roles):
                raise ValueError(f"roles recorded by more than one unit: {', '.join(sorted(repeated))}")
        except ValueError as exc:
            raise _GateRejected(f"lineup.md line {number}: {exc}") from exc
        entries[entry.unit] = entry
        roles.update(entry.roles)
    if not entries:
        raise _GateRejected("lineup.md names no unit — the launch turn writes one line per unit")
    return list(entries.values())


def _lineup_assessment_problem(entries: list[_LineupEntry], data: dict) -> str:
    """Reconcile each launched role and require missing assessments for every unslotted role."""
    counts = Counter(assessment["role"] for assessment in data["assessments"])
    recorded = {role for role, count in counts.items() if count == 1}
    missing = {assessment["role"] for assessment in data["assessments"] if assessment["presence"] == "missing"}
    for entry in entries:
        if entry.state == "spawned" and not entry.roles:
            return f"lineup.md spawned unit {entry.unit!r} has no required roles"
        if entry.state == "promoted-unslotted" and data["readiness"] != "incomplete":
            return (
                f"report readiness conflicts with lineup unit {entry.unit!r}: it is promoted-unslotted, "
                "so readiness must be incomplete — record each role it "
                "covers as presence: missing (readiness incomplete)"
            )
        unslotted = entry.state == "promoted-unslotted"
        unsatisfied = set(entry.roles) - (recorded & missing if unslotted else recorded)
        if unsatisfied:
            presence = "presence: missing " if unslotted else "completed or missing "
            return (
                f"report requires exactly one {presence}assessment for {entry.unit!r} roles: "
                f"{', '.join(sorted(unsatisfied))}"
            )
    return ""


_NON_PROSE: Final = re.compile(
    r"<!--[\s\S]*?(?:-->|\Z)"
    r"|^ {0,3}(?P<backticks>`{3,})(?!`)[^`\n]*\n[\s\S]*?(?:^ {0,3}(?P=backticks)`*[ \t]*$|\Z)"
    r"|^ {0,3}(?P<tildes>~{3,})(?!~)[^\n]*\n[\s\S]*?(?:^ {0,3}(?P=tildes)~*[ \t]*$|\Z)"
    r"|^ {0,3}<(?P<tag>[A-Za-z][\w-]*)(?=[\s>/])[\s\S]*?</(?P=tag)[ \t]*>"
    r"|^ {0,3}<(?:[A-Za-z/!?])[\s\S]*?(?=\n[ \t]*\n|\Z)",
    re.MULTILINE,
)
_HEADING: Final = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")


def _review_confidence_lines(report_text: str) -> set[str]:
    """Collect literal report lines in Review Confidence outside Markdown code and HTML blocks."""
    # Leave a non-matching line in place so removing a comment cannot construct a generated bullet.
    visible = _NON_PROSE.sub(lambda match: "\x00" + "\n" * match[0].count("\n") + "\x00", report_text)
    depth = 0
    lines = set()
    for line in visible.splitlines():
        if heading := _HEADING.fullmatch(line):
            level = len(heading[1])
            if heading[2] == "Review Confidence":
                depth = level
            elif level <= depth:
                depth = 0
        elif re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            depth = 0
        elif depth:
            lines.add(line)
    return lines


def _rerun_hint(entry: _LineupEntry, pr: str, prior: str) -> str:
    """Render the one disclosure line accepted for a capped unit."""
    full = f"/oss:review {pr} --full"
    prefix = f"- Unreviewed dimension: {entry.unit} — "
    if entry.state == "dropped":
        return f"{prefix}dropped by FANOUT_MAX. Rerun with `{full}`."
    roles = ",".join(entry.roles)
    fill = f'/oss:review {pr} --fill-gaps={prior} --roles="{roles}"'
    return f"{prefix}{entry.trigger}. Rerun with `{full}` or `{fill}`."


def _lineup_disclosure_problem(report: Path, text: str, entries: list[_LineupEntry]) -> str:
    """Require each generated rerun line in the report's visible Review Confidence section."""
    capped = [entry for entry in entries if entry.state != "spawned"]
    if not capped:
        return ""
    pr = _allocated_pr(report)
    if not pr:
        return "lineup.md rerun disclosures require the allocated PR report path"
    prior = Path(*report.parts[-5:]).as_posix()
    visible = _review_confidence_lines(text)
    for entry in capped:
        if _rerun_hint(entry, pr, prior) not in visible:
            return f"Review Confidence lacks the generated `Unreviewed dimension: {entry.unit}` line with its rerun command"
    return ""


def _print_rerun_hints(report: Path) -> int:
    """Render copy-ready commands using preflight's PR, boolean flag and value flag grammar."""
    pr = _allocated_pr(report)
    if not pr or report.name != "review-report.md":
        print("[gate] reason: rerun hints need the allocated PR report path")
        return 1
    try:
        entries = _read_lineup(report)
    except _GateRejected as exc:
        _print_gate_reason(str(exc))
        return 1
    # The allocated suffix contains only fixed names and digits. It also works from the original checkout
    # when the parent generated these hints from a review worktree or a checkout path containing quotes.
    prior = Path(*report.parts[-5:]).as_posix()
    for entry in entries:
        if entry.state != "spawned":
            print(_rerun_hint(entry, pr, prior))
    return 0


def _verdict_state_problem(
    data: dict, findings: list, fields: dict[str, str], carried: frozenset[str] = frozenset()
) -> str:
    """Name the first producer-only impossible verdict state a fresh publication carries, or return "".

    These are the ``verdict-states.md`` rows enforced by ``producer``: intake keeps reading saved reports that predate
    them, so only ``oss:review``'s own publication gate applies them.

    Args:
        data: The publication's ``review-validation.json`` envelope, already accepted by the intake rules.
        findings: Its parsed ``findings.jsonl`` rows.
        fields: Its parsed header fields.
        carried: Candidate ids a fill-gaps plan carries verbatim, the only records that may lack a capability kind.

    Returns:
        The first problem, or an empty string.
    """
    return (
        _capability_kind_problem(data, carried)
        or _approve_problem(data, findings)
        or _confidence_ceiling_problem(data, fields)
    )


def publication_verdict(report: Path, producer: bool = False, fill_gaps: bool = False) -> tuple[str, str]:
    """Return the gate decision for a report, with the producer's own rules applied on top when asked.

    Args:
        report: Path to a review report.
        producer: Also require the version-one contract and merge-ready ``findings.jsonl`` rows, as ``oss:review``
            does for the report it just published. A ``REJECT_`` gate skips these rules: the producer never
            publishes a rejection as finished work, so it keeps the reject route whatever its rows hold.
        fill_gaps: The publication is a fill-gaps run (``--fill-gaps``): it writes no lineup record, and candidates its
            ``fill-gaps.json`` carries verbatim may lack a capability kind. Ignored without ``producer``.

    Returns:
        ``(gate line, "")`` when the report may be used, or ``("", reason)`` naming the first rule it breaks.

    Examples:
        >>> from pathlib import Path
        >>> publication_verdict(Path("no-such-dir/review-report.md"), producer=True)
        ('', 'report unreadable or absent')
    """
    line, reason = gate_verdict(report, producer=producer)
    # Same rejection test as _evaluate_gate: a row rule must not turn a consolidator-published rejection into an
    # incomplete-review-report, which the review would route to a row repair instead of its rejection handling.
    if producer and _ACTIONABLE_GATE_RE.fullmatch(line) and "REJECT_" not in line:
        problem = _producer_problem(report, fill_gaps)
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
_JUDGED_STAND_IN: Final = "MAJOR_REVISION"


def supported_outcomes(assessments: list) -> list[str | None]:
    """List the aggregate outcome kinds the completed reviewers support, null first.

    A null aggregate outcome needs a completed undecidable reviewer and a judged one a reviewer that judged; a missing
    reviewer supports neither. ``fill_gaps_scope`` chooses a rerun's aggregate outcome by the same rule, and the gap
    grader's residual check tries these outcomes when the recorded one is a mislabel.

    Args:
        assessments: Assessment records; entries that are not mappings are ignored.

    Returns:
        ``None`` (null outcome) and/or ``"MAJOR_REVISION"`` (any judgment), in that order; empty when no completed
        reviewer supports either.

    Examples:
        >>> judged = {"presence": "completed", "source_outcome": "APPROVE"}
        >>> undecided = {"presence": "completed", "source_outcome": None}
        >>> missing = {"presence": "missing", "source_outcome": None}
        >>> supported_outcomes([judged]), supported_outcomes([undecided, missing]), supported_outcomes([missing])
        (['MAJOR_REVISION'], [None], [])
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
            corrected["source_rating"] = None if outcome is None else source_rating(outcome)
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
    if fields.get("Review contract") != "1" or fields.get("Rating scale") not in _KNOWN_SCALES:
        result.add("envelope-header", GapSeverity.CRITICAL, "-", "header lacks Review contract: 1 / Rating scale")
    try:
        data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
        findings = _read_findings(report)
    except (OSError, ValueError) as exc:
        result.add("sidecar-unreadable", GapSeverity.CRITICAL, "-", f"{type(exc).__name__}: {exc}")
        return
    if (
        not isinstance(data, dict)
        or data.get("contract_version") != 1
        or not _source_assessment(data)
        or not _scale_consistent(data)
    ):
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

    Never edits and never raises on the report's content. A report the strict gate accepts still needs mergeable
    findings; a report the strict gate refuses yields at least one gap. A legacy report with a usable gate yields one
    degraded gap for absent candidate, coverage and open-concern data. Any other legacy gate is ambiguous and
    critical. A parsed header whose ``Gate:`` field holds ``REJECT_`` is echoed as ``recorded_rejection`` whatever
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
    if problem := publication_state_problem(report):
        result = GapAssessment("unknown")
        result.add("publication-withheld", GapSeverity.CRITICAL, "-", problem)
        return result
    line, reason = gate_verdict(report)
    try:
        fields = _parse_header_fields(report.read_text(encoding="utf-8").splitlines())
    except (OSError, ValueError):
        fields = None
    if fields is None:
        result = GapAssessment("unknown")
        result.add("header-invalid", GapSeverity.CRITICAL, "-", "report unreadable or header incomplete")
        return result
    rejection = fields["Gate"] if "REJECT_" in fields["Gate"] else ""
    if not _is_versioned(fields):
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
    # The strict gate's exception families plus AttributeError and RecursionError (a pathologically nested value): a
    # malformed sidecar is a graded gap, never a traceback.
    try:
        if line:
            _assess_accepted_findings(report, line, result)
        else:
            _assess_versioned(report, fields, result, reason)
    except OSError as exc:
        result.add("sidecar-unreadable", GapSeverity.CRITICAL, "-", f"{type(exc).__name__}: {exc}")
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        result.add("sidecar-malformed", GapSeverity.CRITICAL, "-", f"{type(exc).__name__}: {exc}")
    return result


def _assess_accepted_findings(report: Path, line: str, result: GapAssessment) -> None:
    """Grade merge constraints on rows the strict gate already matched to confirmed candidates."""
    findings = _read_findings(report)
    if findings_problem(findings):
        matched = {row["candidate_id"] for row in findings}
        _assess_findings(findings, matched, matched, result)
    else:
        result.gate = line


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
        write_private_text(Path(path_out), f"{report.as_posix()}\n" if report else "")
    except OSError as exc:
        print(f"[gate] ⚠ could not write --path-out {path_out}: {exc} — a stale sentinel may remain", file=sys.stderr)
        return False
    return True


def _print_gate_reason(reason: str) -> None:
    """Label parent-owned inputs separately from publication defects."""
    artifact = reason.partition(" ")[0]
    if artifact in _PARENT_EVIDENCE:
        print(f"[gate] parent evidence: {artifact}")
    print(f"[gate] reason: {reason}")


def _print_incomplete_report(report: Path, reason: str) -> None:
    """Emit the publication failure signal followed by its actionable reason."""
    print(
        f"⛔ BLOCKED — incomplete-review-report: {report}; finish or rerun /oss:review; upgrade oss:resolve when the review contract is unsupported before resolve"
    )
    _print_gate_reason(reason)


def _evaluate_gate(
    report: Path, pr_number: str, timeout: int, path_out: str, producer: bool = False, fill_gaps: bool = False
) -> int:
    """Check the report's gate line and, on a rejection, whether the head has since moved.

    Args:
        report: Path to the resolved review report.
        pr_number: PR number without the leading ``#``.
        timeout: Maximum subprocess wait in seconds.
        path_out: Destination for the resolved report path, forwarded to ``_write_path_out``.
        producer: Also apply the producer rules (``publication_verdict``); a breach prints the same
            ``incomplete-review-report`` marker and ``[gate] reason:`` line as an intake failure.
        fill_gaps: The producer gate checks a fill-gaps run (``publication_verdict``).

    Returns:
        Exit code — 0 when resolve may proceed, 1 when the report boundary is blocked.
    """
    line, reason = publication_verdict(report, producer, fill_gaps)
    if _ACTIONABLE_GATE_RE.fullmatch(line) is None:
        _write_path_out(path_out, None)
        _print_incomplete_report(report, reason or f"unrecognized gate line {line!r}")
        return 1
    if not _write_path_out(path_out, report):
        return 1
    if "REJECT_" not in line:
        print(f"[gate] {line or 'no Gate: field'} ({report}) — no restriction")
        return 0

    recorded = reject_sha(line)
    current = current_head_sha(pr_number, timeout) if pr_number else ""
    # `recorded` may be an abbreviated SHA while `current` is
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


def _restore_lineup(report: Path) -> int:
    """Restore the working copy only from the valid original launch record."""
    try:
        write_private_text(report.with_name("lineup.md"), _original_lineup(report))
    except (OSError, _GateRejected) as exc:
        print(f"! BLOCKED — cannot restore original Step 2 lineup: {exc}")
        return 1
    return 0


def _release_artifact(path: Path) -> bytes:
    """Read a parent-owned allocation artifact without following a symlink."""
    try:
        if not path.is_file() or path.is_symlink():
            raise ValueError("expected a regular file")
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        raise _GateRejected(f"{path.name} could not be read ({exc})") from exc


def _allocated_release_run(report: Path) -> Path:
    """Locate the session's allocation and authenticate the selected report directory."""
    session = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or str(os.getppid())
    temp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    try:
        sentinel = _release_artifact(temp / f"oss-review-run-dir-{session}").decode("utf-8").strip()
        if not sentinel:
            raise ValueError("empty run-dir sentinel")
        run = Path(sentinel)
        original = _release_artifact(run / "report-dir.txt").decode("utf-8").strip()
        if not original or Path(original).resolve() != report.parent.resolve():
            raise ValueError("selected report directory disagrees with the Step 2 run record")
    except (OSError, ValueError, RuntimeError, _GateRejected) as exc:
        raise _GateRejected(f"report-dir.txt run identity unavailable or invalid ({exc})") from exc
    return run


def _release_mode(report: Path, run: Path, requested_fill: bool | None) -> bool:
    """Authenticate the run mode and reject an unsealed or mismatching plan."""
    mode = _release_artifact(run / "review-mode.txt").strip()
    if mode not in (b"fresh", b"fill-gaps"):
        raise _GateRejected("review-mode.txt does not record fresh or fill-gaps")
    fill_gaps = mode == b"fill-gaps"
    if requested_fill is not None and requested_fill != fill_gaps:
        raise _GateRejected("review-mode.txt disagrees with the requested publication mode")
    original = run / "fill-gaps.json"
    copy = report.with_name(original.name)
    if fill_gaps:
        if _release_artifact(original) != _release_artifact(copy):
            raise _GateRejected("fill-gaps.json differs from the Step 2 plan")
        _, problem = _fill_gaps_carried(report)
        if problem:
            raise _GateRejected(problem)
    elif any(plan.exists() or plan.is_symlink() for plan in (original, copy)):
        raise _GateRejected("review-mode.txt records a fresh run but a fill-gaps plan is present")
    return fill_gaps


def _release_evidence(report: Path, requested_fill: bool | None) -> tuple[str, bool]:
    """Bind release to the pending canonical report, the original head and the allocation mode."""
    marker = report.with_name(_PENDING_PUBLICATION)
    if not marker.is_file() or marker.is_symlink() or report.name != "review-report.md" or report.is_symlink():
        raise _GateRejected("expected a canonical report with its parent publication marker")
    run = _allocated_release_run(report)
    head = _read_reviewed_revision(report, required=True)
    if _release_artifact(run / "head-sha.txt") != _release_artifact(report.with_name("head-sha.txt")):
        raise _GateRejected("head-sha.txt differs from the Step 2 frozen head")
    return head, _release_mode(report, run, requested_fill)


def _carry_allows_release(report: Path, fill_gaps: bool) -> bool:
    """Keep prior evidence refusals and consolidator repair signals distinct at release."""
    if not fill_gaps:
        return True
    # The verifier imports this module's intake rules.
    from fill_gaps_scope import verify_carry

    problems = verify_carry(report)
    if not problems:
        return True
    print("\n".join(problems))
    if all(problem.startswith("[carry] ") for problem in problems):
        _print_incomplete_report(report, "; ".join(problems))
    return False


def _publish_report(report: Path, fill_gaps: bool, timeout: int) -> int:
    """Release the report only after its allocation, carry and producer checks pass."""
    try:
        _release_evidence(report, fill_gaps)
    except _GateRejected as exc:
        _print_gate_reason(str(exc))
        return 1
    if not _carry_allows_release(report, fill_gaps):
        return 1
    if _evaluate_gate(report, "", timeout, "", producer=True, fill_gaps=fill_gaps):
        return 1
    return _release_publication(report)


def _release_publication(report: Path) -> int:
    """Replace the pending marker with the positive release record in one filesystem operation."""
    try:
        report.with_name(_PENDING_PUBLICATION).replace(report.with_name(_PUBLISHED))
    except OSError as exc:
        print(f"! BLOCKED — cannot record the parent publication release: {exc}")
        return 1
    return 0


def _publish_rejection(report: Path) -> int:
    """Release the parent-written versioned rejection for the exact frozen run and head."""
    try:
        head, fill_gaps = _release_evidence(report, None)
        fields = _parse_header_fields(_release_artifact(report).decode("utf-8").splitlines()) or {}
    except (_GateRejected, ValueError) as exc:
        _print_gate_reason(str(exc))
        return 1
    if not re.fullmatch(rf"REVIEW_V1_REJECT_[A-Z]+ @{head}", fields.get("Gate", "")):
        _print_gate_reason("terminal rejection gate disagrees with the frozen review head")
        return 1
    if not _carry_allows_release(report, fill_gaps):
        return 1
    line, reason = gate_verdict(report, producer=True)
    if not (_ACTIONABLE_GATE_RE.fullmatch(line) and "REJECT_" in line):
        print(f"! BLOCKED — invalid terminal rejection: {reason or line}")
        return 1
    return _release_publication(report)


def _check_publication_state(report: Path) -> int:
    """Print why the report is withheld from reuse, or exit silently when eligible."""
    if problem := publication_state_problem(report):
        print(f"! BLOCKED — {problem}")
        return 1
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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--reuse",
        action="store_true",
        help="With --pr: find the last usable publication and print the newest unpublished run ahead of it.",
    )
    modes.add_argument(
        "--assess",
        action="store_true",
        help="Print every graded gap and head_moved (true/false/null) as one JSON line instead of gating; exit 2 on "
        "any critical gap, else 0.",
    )
    modes.add_argument(
        "--producer",
        action="store_true",
        help="With --report: oss:review's publication gate. Also require the header's Review contract: 1, every "
        "findings.jsonl row resolve's merge needs, the lineup.md record beside the report and a capability kind on "
        "every UNVERIFIED candidate; a breach is an incomplete-review-report, exit 1.",
    )
    modes.add_argument(
        "--rerun-hints", action="store_true", help="With --report: render rerun commands from the Step 2 lineup."
    )
    modes.add_argument(
        "--restore-lineup",
        dest="report_action",
        action="store_const",
        const=_restore_lineup,
        help="Restore lineup.md from lineup.launch.md.",
    )
    modes.add_argument(
        "--publication-state",
        dest="report_action",
        action="store_const",
        const=_check_publication_state,
        help="Refuse a pending or parked report.",
    )
    modes.add_argument(
        "--publish-rejection",
        dest="report_action",
        action="store_const",
        const=_publish_rejection,
        help="Release a parent-written late rejection.",
    )
    parser.add_argument("--publish", action="store_true", help="With --producer: release the report after its checks.")
    parser.add_argument(
        "--fill-gaps",
        action="store_true",
        help="With --producer: the report is a fill-gaps run. It writes no lineup.md, and candidates its fill-gaps.json "
        "carries verbatim may lack a capability kind; that plan must sit beside the report.",
    )
    parser.add_argument(
        "--path-out",
        default="",
        help="File to write the resolved report path to (empty file when this PR has no report).",
    )
    args = parser.parse_args(argv)
    if args.producer and args.report is None:
        parser.error("--producer needs --report")
    if args.reuse and (args.report or not _PR_NUMBER_RE.fullmatch(args.pr.strip())):
        parser.error("--reuse needs a numeric --pr")
    if (args.rerun_hints or args.report_action) and args.report is None:
        parser.error("the selected mode needs --report")
    if args.fill_gaps and not args.producer:
        parser.error("--fill-gaps qualifies the --producer gate only")
    if args.publish and (not args.producer or args.path_out):
        parser.error("--publish needs --producer and cannot use --path-out")
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

    if args.report is None and not _PR_NUMBER_RE.fullmatch(pr_number):
        _write_path_out(args.path_out, None)
        print(f"find_review_report: --pr must be digits only, got {pr_number!r}", file=sys.stderr)
        return 1

    if args.reuse:
        return _review_reuse(pr_number, args.path_out)
    report = args.report if args.report is not None else newest_report_for_pr(pr_number)
    if report is None:
        return _nothing_to_gate(args.path_out, f"[gate] no review report names PR #{pr_number} — no restriction")

    return _run_report_command(report, args, pr_number)


def _run_report_command(report: Path, args: argparse.Namespace, pr_number: str) -> int:
    """Run the selected review operation on the resolved input report path."""
    if args.report_action:
        return args.report_action(report)
    if args.publish:
        return _publish_report(report, args.fill_gaps, args.timeout)
    if args.assess:
        return _print_assessment(report, args.path_out, pr_number, args.timeout)
    if args.rerun_hints:
        return _print_rerun_hints(report)
    return _evaluate_gate(report, pr_number, args.timeout, args.path_out, args.producer, args.fill_gaps)


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
