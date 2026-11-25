#!/usr/bin/env python3
"""Validate and render deterministic Codex Rig final handoffs.

## Purpose

Turn one versioned, machine-readable workflow handoff into the exact Markdown or caller-controlled bytes intended for
the final user response, closing structural omissions before result promotion.

## Scope

Validate local handoff JSON, render sibling ``final.md`` output, and bind both files with SHA-256 evidence. The helper
does not decide workflow findings, execute project gates, inspect chat transcripts, or send a response.

## Usage

Conclusive PR reviews use presentation 17 to show Reject with the close reason and reviewed commit; a duplicate adds
the maintainer's linked action. Assessed PR reviews use presentation 16 to open with the source-bound PR subject.
Other new handoffs use presentation 15.
These layouts include substantive outcome judgments, closing recommendations, readable prose, confidence
tables, linked artifacts, and verification evidence in rows with other paths outside tables.
New writers set ``action_policy=owned`` for concrete follow-ups or ``best-way-forward`` for one unblocking option.
Unmarked handoffs retain their exact presentation.
Run ``render`` after gates and handoff creation, then run ``check``
directly or through the shared artifact validator
before promoting ``result.candidate.json``. Before remediation selection, run ``selection --input selection.json
--out-scope resolution-scope.md`` to validate source ownership and render the scope; add ``--check`` for a read-only
byte comparison. Grouped review/remediation views retain the same machine identity and evidence bindings.

## Outputs

``render`` writes deterministic UTF-8/LF final text and ``final-handoff.validation.json`` containing the handoff and
render digests, skill, branch, schema version, and pass status.

## Failure

Invalid schemas, branch/table mismatches, hidden source records, incomplete verification, unresolved work without an
owner/action, confidence contradictions, path escapes, or post-render drift exit non-zero without claiming a valid
handoff.

## Used by

All artifact-producing Codex Rig workflow skills use this helper as their post-gate presentation checkpoint;
the shared artifact validator verifies its evidence for schema-v2 workflow results. The agent-shim manager is
deliberately outside the artifact lifecycle.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import re
import shutil
import string
import sys
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

#: Schema revision of the final-handoff JSON document.
SCHEMA_VERSION = 1
#: Evidence-backed reasons for recommending closure of an open pull request.
CLOSE_CODES = frozenset(
    {
        "FALSE_GOAL",
        "BREAKING_CONDUCT",
        "WRONG_SCOPE",
        "WRONG_PROVENANCE",
        "DUPLICATE",
        "UNADDRESSED_REVERT",
        "SPAM",
        "ARCHITECTURE_VIOLATION",
    }
)
#: Presentation of every new terminal PR close handoff, one step after the assessed PR review presentation (16).
CLOSED_REVIEW_PRESENTATION = 17
#: Line openings that start a Markdown block instead of continuing a paragraph. Characters with no inline meaning are
#: matched outright: an ATX heading, quote, plus or dash bullet, dash thematic break or setext underline, table row and
#: ordered item delimiter. Characters that also open inline markup match only in their block form: a star bullet (a
#: star before a space or the end), a star or underscore thematic break, a backtick fence (no later backtick on the
#: line, otherwise it is a code span), a tilde fence, a link reference or footnote definition, and a comment,
#: declaration or processing-instruction HTML block, which a line opens even where the text would be an email autolink.
_BLOCK_OPENING = re.compile(
    r"[#>+\-=|]|[0-9]{1,9}[.)]|\*(?= |$)|(?:\* *){3,}$|(?:_ *){3,}$|`{3,}(?=[^`]*$)|~{3}"
    r"|\[(?:[^\[\]\\]|\\.)*\]:|<[!?]"
)
#: Characters that may follow ``<`` when it opens raw HTML: a tag name, closing tag, comment, declaration or
#: processing instruction.
_RAW_HTML_START = frozenset(string.ascii_letters + "/!?")
#: A CommonMark URI or email autolink, which a renderer reads before raw HTML at the same ``<``.
_AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\x00-\x20\x7f<>]*"
    r"|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*)>"
)
#: A maximal run of backticks, which opens a code span closed by the next run of the same length.
_BACKTICK_RUN = re.compile(r"`+")
#: ASCII punctuation, the only characters a CommonMark backslash escapes.
_ASCII_PUNCTUATION = frozenset(string.punctuation)
#: Open-parenthesis nesting at which markdown-it stops reading a link destination; deeper text counts as consumed.
_MAX_DESTINATION_PARENS = 32
#: Identifiers of the verification gates, in the order a handoff must report them.
GATE_IDS = ("lint", "format", "types", "tests", "review")
#: Allowed result statuses for a verification gate.
GATE_STATUSES = {"pass", "fail", "missing-command", "not-applicable", "timeout"}
#: Allowed closure statuses for a confidence gap listed in a handoff.
GAP_STATUSES = {"closed", "unresolved", "deferred"}
#: Required column headings of the challenge-resolve handoff table, with one column per severity tier.
CHALLENGE_SEVERITY_COLUMNS = (
    "Challenge",
    "Security",
    "Critical",
    "High",
    "Medium",
    "Low",
    "Nits",
    "Weighted score",
    "Decision",
    "Evidence",
)
#: Column headings of the older challenge-resolve table layout, still accepted so earlier handoffs stay readable.
LEGACY_CHALLENGE_COLUMNS = ("Iteration", "Open findings", "Weighted score", "Decision", "Evidence")
#: Required table column headings for each skill's handoff table.
STANDARD_COLUMNS = {
    "challenge-resolve": CHALLENGE_SEVERITY_COLUMNS,
    "audit": ("Item", "Severity / impact", "Decision", "Evidence", "Next action"),
    "calibrate": ("Check / metric", "Result", "Evidence", "Next action"),
    "assess": ("Finding", "Impact", "Decision", "Evidence", "Next action"),
    # Keep pre-rename report handoffs readable without registering a second skill.
    "change-analysis": ("Finding", "Impact", "Decision", "Evidence", "Next action"),
    "code-remediate": ("Item", "Severity", "Finding", "Sources", "Outcome", "Evidence / next action"),
    "implement": ("Surface", "Outcome", "Verification", "Remaining limit"),
    "investigate": ("Hypothesis", "Evidence", "Disposition", "Next action"),
    "kaggle": ("Artifact", "Mode", "Verification", "Runtime limit"),
    "manage": ("Surface", "Outcome", "Verification", "Remaining limit"),
    "optimize": ("Iteration", "Baseline", "After", "Delta", "Guard", "Decision"),
    "profile": ("Session / agent", "Observed tokens", "Inclusive tool time", "Cost estimate", "Coverage limit"),
    "release": ("Change", "SemVer impact", "Status / blocker", "Evidence"),
    "research": ("Recommendation", "Evidence", "Decision", "Caveat / next check"),
    "sync": ("Surface", "Outcome", "Verification", "Remaining limit"),
}
#: Skills the handoff renderer and validator accept: every standard-table skill plus code-review.
SUPPORTED_SKILLS = frozenset((*STANDARD_COLUMNS, "code-review"))
#: Historical lowercase code-review recommendations: ``source_rating_version`` 1 and unversioned handoffs.
HISTORICAL_REVIEW_RECOMMENDATIONS = (
    "accept-as-is",
    "minor-changes",
    "needs-more-work",
    "insufficient-but-promising",
    "reject",
    "not-aligned",
)
#: Unified ``source-5-v2`` source verdict tokens written under ``source_rating_version`` 2, level 1 to level 5.
SOURCE_VERDICT_TOKENS = ("APPROVE", "MINOR_EDITS", "MAJOR_REVISION", "INSUFFICIENT_BUT_PROMISING", "REJECT")
#: Snapshot ``source_rating_version`` values that select the five-level source legend; 1 is the historical reader.
SOURCE_RATING_VERSIONS = frozenset({1, 2})
#: Allowed overall recommendations for a code-review handoff.
REVIEW_RECOMMENDATIONS = frozenset((*HISTORICAL_REVIEW_RECOMMENDATIONS, *SOURCE_VERDICT_TOKENS))
#: Required column headings for each named table in a code-review handoff.
REVIEW_TABLE_COLUMNS = {
    "PR Snapshot": ("Field", "Value"),
    "Review Snapshot": ("Field", "Value"),
    "Review Findings and Merge Blocks": ("Finding / area", "Required change", "Evidence", "Status"),
}
#: Legend line printed under the reviewers row to explain the 1-5 rating scale.
REVIEWER_LEGEND = (
    "Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence · 5 = Block / Reject."
)
#: Self-described source judgments use a distinct source-judgment family; omitted markers retain historical semantics.
SOURCE_REVIEWER_LEGEND = "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block."
#: Exact set of top-level fields a handoff document must contain.
HANDOFF_FIELDS = {
    "schema_version",
    "skill",
    "branch",
    "outcome",
    "tables",
    "source_records",
    "source_coverage",
    "verification",
    "remaining",
    "next_steps",
    "confidence",
    "artifacts",
    "caller_contract",
}
#: Current presentation layout revision for rendered handoffs, written into new handoff documents.
PRESENTATION_VERSION = 13
#: Reserved absence markers for authored actions in current handoffs.
ABSENT_VALUES = frozenset({"-", "—", "none", "n/a", "not applicable", "tbd"})
NO_ACTION = ABSENT_VALUES | {
    "done",
    "completed",
    "no change",
    "no action",
    "no action required",
    "no further action is required",
}
NO_OWNER = ABSENT_VALUES | {"unknown", "unassigned"}
#: One-sentence plain-English summary shown for each code-review recommendation.
REVIEW_PLAIN_SUMMARIES = {
    "accept-as-is": "This review found no blocking change requests.",
    "minor-changes": "This review can proceed after minor changes.",
    "needs-more-work": "This review needs more work before it can be accepted.",
    "reject": "This review recommends rejecting the proposed change.",
    "not-aligned": "This review is not aligned with the requested goal.",
    "insufficient-but-promising": "This review finds a promising contribution that needs substantially more implementation.",
    "None": "This review returned no source judgment; verification remains open.",
}
# A version 2 token reads exactly like the version 1 name for the same level.
REVIEW_PLAIN_SUMMARIES.update(
    {
        token: REVIEW_PLAIN_SUMMARIES[name]
        for name, token in zip(HISTORICAL_REVIEW_RECOMMENDATIONS, SOURCE_VERDICT_TOKENS)
    }
)


class HandoffError(ValueError):
    """Report one fail-closed handoff contract violation."""


def review_commit_sha(run: Path, scope: str) -> str:
    """Resolve the full reviewed commit from retained producer receipts, never the invoking checkout."""
    if scope not in {"pr", "commit", "working-tree", "path"}:
        raise HandoffError("review-commit-scope-invalid")
    sources = [("reviewed-commit.txt", ()), ("batch-inventory.json", ("source_snapshot", "revision"))]
    if scope == "pr":
        sources.extend([("pr-routing.json", ("head_oid",)), ("local-checkout.json", ("expected_head",))])
    elif scope in {"working-tree", "path"}:
        sources.append(("local-source/source-snapshot.json", ("revision",)))
    else:
        sources.append(("gates.json", ()))
    commits: set[str] = set()
    for name, keys in sources:
        path = run / name
        if path.is_symlink() or (path.parent != run and path.parent.is_symlink()):
            raise HandoffError("review-commit-source-invalid")
        if not path.exists():
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
            if name == "gates.json":
                gates = json.loads(value)
                if not isinstance(gates, dict) or not isinstance(gates.get("checks"), list):
                    raise HandoffError("review-commit-source-invalid")
                values = []
                for check in gates["checks"]:
                    if not isinstance(check, dict):
                        raise HandoffError("review-commit-source-invalid")
                    source = check.get("source")
                    if source is None:
                        continue
                    if not isinstance(source, dict):
                        raise HandoffError("review-commit-source-invalid")
                    expected = source.get("expected_head")
                    # Optional reviewer routes share gate observations; an expected SHA alone proves no source read.
                    for phase in ("before", "after"):
                        observed = source.get(phase)
                        if (
                            not isinstance(observed, dict)
                            or "error" in observed
                            or observed.get("head") != expected
                            or observed.get("status") != ""
                        ):
                            raise HandoffError("review-commit-source-unverified")
                    values.append(expected)
                if values and "source" in gates:
                    declared = gates["source"]
                    if not isinstance(declared, dict) or any(declared.get("expected_head") != sha for sha in values):
                        raise HandoffError("review-commit-conflict")
            elif keys:
                value = json.loads(value)
                for key in keys:
                    value = value[key]
                values = [value]
            else:
                values = [value]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HandoffError("review-commit-source-invalid") from error
        for value in values:
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
                raise HandoffError("review-commit-sha-invalid")
            commits.add(value)
    if len(commits) != 1:
        raise HandoffError("review-commit-missing" if not commits else "review-commit-conflict")
    return commits.pop()


def _source_rating_version(table: dict[str, Any]) -> int | None:
    """Return a snapshot table's supported source rating version, or ``None`` when it carries none.

    Every supported version selects the same five-level source legend; only the verdict spelling differs.

    Examples:
        >>> _source_rating_version({"source_rating_version": 2})
        2
        >>> _source_rating_version({"source_rating_version": True}) is None
        True
    """
    version = table.get("source_rating_version")
    return version if type(version) is int and version in SOURCE_RATING_VERSIONS else None


def _require_source_vocabulary(outcome_summary: str, tables: object) -> None:
    """Refuse a review outcome whose verdict spelling belongs to another source rating version than its snapshot.

    Version 2 snapshots take only the unified tokens; version 1 snapshots take only the historical lowercase names. A
    handoff without a versioned snapshot keeps the spelling its result artifact already validated.
    """
    versions = {
        _source_rating_version(table)
        for table in (tables if isinstance(tables, list) else [])
        if isinstance(table, dict)
    } - {None}
    recommendation = outcome_summary.removeprefix("Recommendation: ").removesuffix(".")
    if (2 in versions and recommendation in HISTORICAL_REVIEW_RECOMMENDATIONS) or (
        1 in versions and recommendation in SOURCE_VERDICT_TOKENS
    ):
        raise HandoffError("review-outcome-source-rating-version-mismatch")


def _require_object(value: object, label: str) -> dict[str, Any]:
    """Return a JSON object or raise a stable contract error."""
    if not isinstance(value, dict):
        raise HandoffError(f"{label}-not-object")
    return value


def _require_string(value: object, label: str) -> str:
    """Return one non-empty string stripped only for validation."""
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{label}-missing")
    return value


def _require_string_list(value: object, label: str, *, allow_empty: bool = True) -> list[str]:
    """Return a JSON string list with explicit empty-list policy."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise HandoffError(f"{label}-invalid")
    if not allow_empty and not value:
        raise HandoffError(f"{label}-empty")
    return value


def _expected_band(score: float) -> str:
    """Return the shared confidence-band status for a score."""
    if score <= 0.8:
        return "not-acceptable-failed"
    if score < 0.85:
        return "very-questionable"
    if score < 0.9:
        return "cautious-low"
    return "fair"


def _validate_branch(skill: str, branch: object) -> str:
    """Validate the closed branch vocabulary for one skill."""
    branch_name = _require_string(branch, "branch")
    allowed = (
        {"assessed", "unavailable", "closed", "caller-contract"}
        if skill == "code-review"
        else {
            "standard",
            "caller-contract",
        }
    )
    if branch_name not in allowed:
        raise HandoffError(f"branch-invalid:{skill}:{branch_name}")
    return branch_name


def _validate_review_attribution(tables: list[dict[str, Any]], skill: str) -> None:
    """Bind finding authors to a single evidence-backed reviewer list while accepting historical tables."""
    roles: set[str] = set()
    for table in tables:
        if "reviewers" not in table:
            continue
        if skill != "code-review" or table["heading"] not in {"PR Snapshot", "Review Snapshot"} or roles:
            raise HandoffError("reviewers-table-invalid")
        reviewers = table["reviewers"]
        if not isinstance(reviewers, list) or not reviewers:
            raise HandoffError("reviewers-empty")
        if "summary" in table:
            _require_string(table["summary"], "review-summary")
        current = _source_rating_version(table) is not None
        if "source_rating_version" in table and not current:
            raise HandoffError("source-rating-version-invalid")
        if current and table.get("verification_readiness") not in {"complete", "blocked", "incomplete"}:
            raise HandoffError("review-verification-readiness-invalid")
        for reviewer in reviewers:
            if not isinstance(reviewer, dict) or set(reviewer) != {"role", "rating", "evidence"}:
                raise HandoffError("reviewer-fields-invalid")
            role = _require_string(reviewer["role"], "reviewer-role")
            _require_string(reviewer["evidence"], "reviewer-evidence")
            if role in roles:
                raise HandoffError("reviewer-role-duplicate")
            if not (
                (current and reviewer["rating"] is None and table["verification_readiness"] != "complete")
                or (type(reviewer["rating"]) is int and reviewer["rating"] in range(1, 6))
            ):
                raise HandoffError("reviewer-rating-invalid")
            roles.add(role)
        if any(row["cells"][0] == "Reviewers" for row in table["rows"]):
            raise HandoffError("reviewers-row-duplicate")
    for table in tables:
        for row in table["rows"]:
            if "authors" not in row and not (roles and table["heading"] == "Review Findings and Merge Blocks"):
                continue
            if skill != "code-review" or table["heading"] != "Review Findings and Merge Blocks":
                raise HandoffError("finding-authors-table-invalid")
            authors = _require_string_list(row.get("authors"), "finding-authors", allow_empty=False)
            if len(set(authors)) != len(authors) or not set(authors) <= roles:
                raise HandoffError("finding-authors-unbound")


def _validate_tables(
    payload: dict[str, Any], skill: str, branch: str, *, allow_empty_remediation: bool = False
) -> tuple[set[str], set[str]]:
    """Validate tables and return represented row and source identifiers."""
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise HandoffError("tables-not-list")
    if branch in {"unavailable", "closed", "caller-contract"}:
        if tables:
            raise HandoffError("terminal-branch-forbids-tables")
        return set(), set()
    if skill == "release" and len(tables) == 2:
        if not any(isinstance(table, dict) and table.get("heading") == "Readiness" for table in tables):
            raise HandoffError("release-readiness-table-missing")
    elif skill != "code-review" and len(tables) != 1:
        raise HandoffError("standard-branch-requires-one-table")

    row_ids: set[str] = set()
    source_ids: set[str] = set()
    table_headings: set[str] = set()
    for table_index, raw_table in enumerate(tables):
        table = _require_object(raw_table, f"table:{table_index}")
        heading = _require_string(table.get("heading"), f"table-heading:{table_index}")
        if heading in table_headings:
            raise HandoffError(f"table-heading-duplicate:{heading}")
        table_headings.add(heading)
        columns = _require_string_list(table.get("columns"), f"table-columns:{heading}", allow_empty=False)
        layout = table.get("layout", "legacy")
        if (
            not isinstance(layout, str)
            or layout not in {"legacy", "grouped", "concise", "challenge-history"}
            or (
                layout in {"grouped", "concise"}
                and (skill not in {"code-review", "code-remediate"} or heading in {"PR Snapshot", "Review Snapshot"})
            )
        ):
            raise HandoffError(f"table-layout-invalid:{heading}")
        if layout == "challenge-history" and (
            skill != "challenge-resolve" or tuple(columns) != CHALLENGE_SEVERITY_COLUMNS
        ):
            raise HandoffError("challenge-history-layout-invalid")
        compact_review = skill == "code-review" and payload.get("presentation_version") == 7
        if compact_review and heading not in {"PR Snapshot", "Review Snapshot"}:
            if layout != "concise":
                raise HandoffError("review-concise-layout-required")
            if table.get("overview_only") is not True:
                raise HandoffError("review-overview-only-required")
        if "overview_only" in table and (
            table["overview_only"] is not True
            or (skill != "code-remediate" and not compact_review)
            or layout != "concise"
        ):
            raise HandoffError(f"table-overview-only-invalid:{heading}")
        if (
            skill == "code-remediate"
            and payload.get("presentation_version") in {3, 4, 5, 6}
            and table.get("overview_only") is not True
        ):
            raise HandoffError("remediation-overview-only-required")
        if skill == "code-remediate" and payload.get("presentation_version") in {5, 6} and layout != "concise":
            raise HandoffError("remediation-concise-layout-required")
        expected = REVIEW_TABLE_COLUMNS.get(heading) if skill == "code-review" else STANDARD_COLUMNS[skill]
        if skill == "release" and heading == "Readiness":
            expected = ("Check", "Status", "Evidence", "Blocker / next action")
        if skill == "challenge-resolve" and tuple(columns) == LEGACY_CHALLENGE_COLUMNS:
            expected = LEGACY_CHALLENGE_COLUMNS
        if expected is None or tuple(columns) != expected:
            raise HandoffError(f"table-columns-mismatch:{skill}:{heading}")
        rows = table.get("rows")
        empty_remediation = skill == "code-remediate" and branch == "standard" and allow_empty_remediation
        if not isinstance(rows, list) or (not rows and not empty_remediation):
            raise HandoffError(f"table-rows-empty:{heading}")
        table_cells: list[str] = []
        for row_index, raw_row in enumerate(rows):
            row = _require_object(raw_row, f"table-row:{heading}:{row_index}")
            row_id = _require_string(row.get("id"), f"table-row-id:{heading}:{row_index}")
            if row_id in row_ids:
                raise HandoffError(f"table-row-id-duplicate:{row_id}")
            row_ids.add(row_id)
            if skill == "code-remediate" and payload.get("presentation_version") == 5:
                if type(row.get("selected")) is not bool:
                    raise HandoffError(f"remediation-row-selected-invalid:{row_id}")
            if skill == "code-remediate" and payload.get("presentation_version") == 6:
                if row.get("selected") not in ("Yes", "No", "Closed"):
                    raise HandoffError(f"remediation-row-selected-invalid:{row_id}")
            cells = _require_string_list(row.get("cells"), f"table-row-cells:{row_id}", allow_empty=False)
            if len(cells) != len(columns):
                raise HandoffError(f"table-row-width-mismatch:{row_id}")
            if layout in {"grouped", "concise"} and skill == "code-review":
                _require_string(row.get("title"), f"table-row-title:{row_id}")
                for field in ("summary", "closure_evidence"):
                    if field in row:
                        _require_string(row[field], f"table-row-{field}:{row_id}")
            if layout == "challenge-history":
                _validate_challenge_history_row(row)
            elif "admission" in row or "admission_reason" in row:
                raise HandoffError("challenge-history-layout-required")
            table_cells.extend(cells)
            row_sources = _require_string_list(
                row.get("source_ids"), f"table-row-source-ids:{row_id}", allow_empty=False
            )
            for source_id in row_sources:
                if source_id in source_ids:
                    raise HandoffError(f"source-id-represented-twice:{source_id}")
                source_ids.add(source_id)
        details = table.get("details", [])
        if not isinstance(details, list):
            raise HandoffError(f"table-details-not-list:{heading}")
        detail_ids: set[str] = set()
        rendered_cells = "\n".join(table_cells)
        for detail_index, raw_detail in enumerate(details):
            detail = _require_object(raw_detail, f"table-detail:{heading}:{detail_index}")
            if set(detail) != {"id", "text"}:
                raise HandoffError(f"table-detail-fields-invalid:{heading}:{detail_index}")
            detail_id = _require_string(detail.get("id"), f"table-detail-id:{heading}:{detail_index}")
            _require_string(detail.get("text"), f"table-detail-text:{detail_id}")
            if detail_id in detail_ids:
                raise HandoffError(f"table-detail-id-duplicate:{detail_id}")
            if f"[{detail_id}]" not in rendered_cells:
                raise HandoffError(f"table-detail-unreferenced:{detail_id}")
            detail_ids.add(detail_id)
    if skill == "code-review" and payload.get("presentation_version") == 7:
        if len(table_headings & {"PR Snapshot", "Review Snapshot"}) != 1:
            raise HandoffError("review-snapshot-required")
    _validate_review_attribution(tables, skill)
    return row_ids, source_ids


def _validate_challenge_history_row(row: dict[str, Any]) -> None:
    """Separate descriptive history from evidence admitted to the canonical loop."""
    admission = row.get("admission")
    cells = row["cells"]
    if admission not in {"validated", "advisory", "unavailable"}:
        raise HandoffError("challenge-history-admission-invalid")
    if admission == "unavailable":
        if cells[0] != "not-run" or cells[1:8] != ["N/A"] * 7:
            raise HandoffError("challenge-history-unavailable-row-invalid")
    else:
        if admission == "advisory":
            _require_string(row.get("admission_reason"), "challenge-history-admission-reason")
            if not re.fullmatch(r"(?:Advisory|Checkpoint) [1-9][0-9]*", cells[0]):
                raise HandoffError("challenge-history-advisory-identity-invalid")
        elif not re.fullmatch(r"[1-9][0-9]*", cells[0]):
            raise HandoffError("challenge-history-validated-identity-invalid")
        if any(not re.fullmatch(r"(?:0|[1-9][0-9]*) \+ (?:0|[1-9][0-9]*)", cell) for cell in cells[1:8]):
            raise HandoffError("challenge-history-split-invalid")
        splits = [tuple(map(int, cell.split(" + "))) for cell in cells[1:8]]
        weights = (20, 10, 6, 4, 2, 1)
        if any(
            sum(pair[index] * weight for pair, weight in zip(splits[:6], weights)) != splits[6][index]
            for index in (0, 1)
        ):
            raise HandoffError("challenge-history-weight-mismatch")
    if admission != "advisory" and "admission_reason" in row:
        raise HandoffError("challenge-history-reason-without-advisory")


def challenge_report_path(report_root: Path, report: str) -> Path:
    """Resolve a retained nonempty report inside its owning run without path escape."""
    if (
        not report.strip()
        or ":" in report
        or "\\" in report
        or PurePosixPath(report).is_absolute()
        or PureWindowsPath(report).is_absolute()
        or ".." in PurePosixPath(report).parts
    ):
        raise HandoffError("challenge-history-report-path-invalid")
    target = (report_root / report).resolve()
    if not target.is_relative_to(report_root.resolve()) or not target.is_file():
        raise HandoffError("challenge-history-report-missing-or-outside-run")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise HandoffError("challenge-history-report-unreadable") from error
    if not text.strip():
        raise HandoffError("challenge-history-report-empty")
    return target


def _validate_source_coverage(
    payload: dict[str, Any], branch: str, represented_source_ids: set[str], *, derive_counts: bool = False
) -> set[str]:
    """Check source identity coverage and optionally fill omitted draft counts from those identities."""
    raw_records = payload.get("source_records")
    if not isinstance(raw_records, list):
        raise HandoffError("source-records-not-list")
    source_ids: set[str] = set()
    for index, raw_record in enumerate(raw_records):
        record = _require_object(raw_record, f"source-record:{index}")
        source_id = _require_string(record.get("id"), f"source-record-id:{index}")
        _require_string(record.get("evidence"), f"source-record-evidence:{source_id}")
        if source_id in source_ids:
            raise HandoffError(f"source-record-id-duplicate:{source_id}")
        source_ids.add(source_id)

    expected_counts = {
        "source_records_total": len(source_ids),
        "represented_source_records_total": len(represented_source_ids),
        "omitted_source_records_total": len(source_ids - represented_source_ids),
    }
    if derive_counts:
        payload.setdefault("source_coverage", expected_counts)
    coverage = _require_object(payload.get("source_coverage"), "source-coverage")
    for key, expected in expected_counts.items():
        if coverage.get(key) != expected:
            raise HandoffError(f"source-coverage-count-mismatch:{key}")
    if coverage.get("omitted_source_records_total") != 0:
        raise HandoffError("omitted-source-records")
    if branch not in {"unavailable", "closed", "caller-contract"} and represented_source_ids != source_ids:
        raise HandoffError("source-record-coverage-mismatch")
    if branch in {"unavailable", "closed", "caller-contract"} and source_ids:
        raise HandoffError("tableless-branch-source-records-forbidden")
    return source_ids


def _validate_verification(payload: dict[str, Any]) -> None:
    """Require complete five-gate evidence for one artifact workflow."""
    entries = payload.get("verification")
    if not isinstance(entries, list) or not entries:
        raise HandoffError("verification-empty")
    observed: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry = _require_object(raw_entry, f"verification:{index}")
        check = _require_string(entry.get("check"), f"verification-check:{index}")
        status = _require_string(entry.get("status"), f"verification-status:{check}")
        _require_string(entry.get("evidence"), f"verification-evidence:{check}")
        if check in observed:
            raise HandoffError(f"verification-check-duplicate:{check}")
        if status not in GATE_STATUSES:
            raise HandoffError(f"verification-status-invalid:{check}")
        observed.add(check)
    if observed != set(GATE_IDS):
        raise HandoffError("verification-gate-coverage-mismatch")


def _validate_unavailable_review_verification(
    payload: dict[str, Any], skill: str, branch: str, presentation_version: object
) -> None:
    """Require new terminal PR-review handoffs to label unrun gates accurately."""
    if skill != "code-review" or branch != "unavailable" or presentation_version not in {2, 3}:
        return
    verification = payload.get("verification")
    if not isinstance(verification, list) or any(entry.get("status") != "not-applicable" for entry in verification):
        raise HandoffError("unavailable-review-verification-must-be-not-applicable")


def pr_publication_action(row_ids: set[str]) -> dict[str, str]:
    """Allocate the post-delivery approval step independently of user-supplied finding IDs."""
    row_id = "pr-worktree-publication"
    while row_id in row_ids:
        row_id += "-next"
    return {
        "row_id": row_id,
        "owner": "code-remediate",
        "item": "Local PR commit is ready for publication review.",
        "next_action": (
            "Verify the commit and destination from the delivery worktree, then ask Approve / Deny. "
            "On approval and runtime authorization, Codex pushes the commit and verifies the remote branch."
        ),
    }


def is_pr_publication_action(item: object, action: dict[str, str]) -> bool:
    """Recognize a generated publication step, including its earlier wording in retained handoffs."""
    return item == action or item == {
        **action,
        "next_action": "Verify the delivered commit and destination, then ask Approve / Deny to push from the worktree.",
    }


def _validate_remaining(payload: dict[str, Any], row_ids: set[str], branch: str, *, owned_actions: bool) -> None:
    """Bind every compact unresolved item to a unique owner and next action."""
    remaining = payload.get("remaining")
    if not isinstance(remaining, list):
        raise HandoffError("remaining-not-list")
    remaining_ids: set[str] = set()
    for index, raw_item in enumerate(remaining):
        item = _require_object(raw_item, f"remaining:{index}")
        row_id = _require_string(item.get("row_id"), f"remaining-row-id:{index}")
        _require_string(item.get("item"), f"remaining-item:{row_id}")
        _require_string(item.get("owner"), f"remaining-owner:{row_id}")
        _require_string(item.get("next_action"), f"remaining-next-action:{row_id}")
        if row_id in remaining_ids:
            raise HandoffError(f"remaining-row-id-duplicate:{row_id}")
        current_review = payload["skill"] == "code-review" and any(
            _source_rating_version(table) is not None for table in payload["tables"]
        )
        if row_ids and row_id not in row_ids and not current_review:
            if not (
                owned_actions
                and branch == "standard"
                and payload["skill"] == "code-remediate"
                and payload.get("commit_disposition", {}).get("status") == "committed"
                and is_pr_publication_action(item, pr_publication_action(row_ids))
            ):
                raise HandoffError(f"remaining-row-id-unknown:{row_id}")
        remaining_ids.add(row_id)
    next_steps = set(_require_string_list(payload.get("next_steps"), "next-steps"))
    if payload.get("presentation_version") == 17 and payload["close_reason"]["code"] != "DUPLICATE":
        if remaining or next_steps:
            raise HandoffError("reject-review-actions-unexpected")
    elif payload.get("presentation_version") == 17:
        # A duplicate is rejected like every other close, plus one maintainer action linking the earlier change.
        if (
            remaining_ids != {"duplicate"}
            or remaining[0]["owner"] != "Maintainer"
            or payload["next_steps"] != ["duplicate"]
        ):
            raise HandoffError("duplicate-review-action-invalid")
        _validate_owned_actions(payload, remaining_ids)
        if any(inline_text(remaining[0][field]) != remaining[0][field] for field in ("item", "next_action")):
            raise HandoffError("close-text-not-inline")
    elif branch == "closed" and next_steps:
        raise HandoffError("closed-branch-next-steps-forbidden")
    if owned_actions and branch not in {"closed", "caller-contract"}:
        if not next_steps <= remaining_ids or bool(next_steps) != bool(remaining_ids):
            raise HandoffError("next-steps-remaining-mismatch")
        if len(next_steps) != len(payload["next_steps"]):
            raise HandoffError("next-steps-duplicate")
        _validate_owned_actions(payload, remaining_ids)
    elif branch != "closed" and next_steps != remaining_ids:
        raise HandoffError("next-steps-remaining-mismatch")
    if payload["skill"] == "code-remediate":
        for table in payload["tables"]:
            if not table.get("overview_only"):
                continue
            for row in table["rows"]:
                disposition = row["cells"][4].split(" — ", 1)[0].strip().casefold()
                if (
                    disposition in {"unresolved", "deferred", "blocked", "needs-clarification"}
                    and row["id"] not in remaining_ids
                ):
                    raise HandoffError(f"remediation-unresolved-action-missing:{row['id']}")


def _validate_owned_actions(handoff: dict[str, Any], remaining_ids: set[str]) -> None:
    """Keep concrete result-row actions bound to owners and absence markers out of the closing recommendations."""
    for item in handoff["remaining"]:
        for field, placeholders in (("owner", NO_OWNER), ("next_action", NO_ACTION)):
            if item[field].strip().casefold().rstrip(".") in placeholders:
                raise HandoffError(f"remaining-{field}-placeholder:{item['row_id']}")
    for table in handoff["tables"]:
        if "Next action" not in table["columns"]:
            continue
        action_index = table["columns"].index("Next action")
        for row in table["rows"]:
            if (
                row["id"] not in remaining_ids
                and row["cells"][action_index].strip().casefold().rstrip(".") not in NO_ACTION
            ):
                raise HandoffError(f"table-action-requires-remaining:{row['id']}")


def _validate_confidence(payload: dict[str, Any]) -> None:
    """Validate score, band, limits, and gap closure evidence."""
    confidence = _require_object(payload.get("confidence"), "confidence")
    score = confidence.get("score")
    if not isinstance(score, int | float) or not 0.0 <= float(score) <= 1.0:
        raise HandoffError("confidence-score-invalid")
    band = _require_string(confidence.get("band"), "confidence-band")
    if band != _expected_band(float(score)):
        raise HandoffError("confidence-band-mismatch")
    _require_string_list(confidence.get("limits"), "confidence-limits")
    gaps = confidence.get("gaps")
    if not isinstance(gaps, list):
        raise HandoffError("confidence-gaps-not-list")
    if float(score) < 1.0 and not gaps:
        raise HandoffError("confidence-gaps-required")
    seen_gaps: set[str] = set()
    for index, raw_gap in enumerate(gaps):
        gap = _require_object(raw_gap, f"confidence-gap:{index}")
        label = _require_string(gap.get("gap"), f"confidence-gap-label:{index}")
        status = _require_string(gap.get("status"), f"confidence-gap-status:{label}")
        if status not in GAP_STATUSES:
            raise HandoffError(f"confidence-gap-status-invalid:{label}")
        closure_field = "evidence" if status == "closed" else "rationale"
        _require_string(gap.get(closure_field), f"confidence-gap-{closure_field}:{label}")
        if label in seen_gaps:
            raise HandoffError(f"confidence-gap-duplicate:{label}")
        seen_gaps.add(label)


def _validate_artifacts(payload: dict[str, Any]) -> None:
    """Require at least one labeled artifact or explicit no-artifact statement."""
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise HandoffError("artifacts-empty")
    labels: set[str] = set()
    for index, raw_artifact in enumerate(artifacts):
        artifact = _require_object(raw_artifact, f"artifact:{index}")
        label = _require_string(artifact.get("label"), f"artifact-label:{index}")
        _require_string(artifact.get("path"), f"artifact-path:{label}")
        if label in labels:
            raise HandoffError(f"artifact-label-duplicate:{label}")
        labels.add(label)
    if payload["skill"] == "code-remediate" and any(table.get("overview_only") for table in payload["tables"]):
        result_paths = [artifact["path"].replace("\\", "/") for artifact in artifacts if artifact["label"] == "Result"]
        if len(result_paths) != 1 or PurePosixPath(result_paths[0]).name != "result.json":
            raise HandoffError("remediation-overview-ledger-missing")
        ledger = str(PurePosixPath(result_paths[0]).with_name("action-items.md"))
        ledgers = [
            artifact["path"].replace("\\", "/")
            for artifact in artifacts
            if PurePosixPath(artifact["path"].replace("\\", "/")).name == "action-items.md"
        ]
        if ".." in PurePosixPath(result_paths[0]).parts or ledgers != [ledger]:
            raise HandoffError("remediation-overview-ledger-missing")


def _validate_caller_contract(payload: dict[str, Any], branch: str) -> None:
    """Validate the narrow exact-output override without weakening normal branches."""
    caller_contract = payload.get("caller_contract")
    if branch != "caller-contract":
        if caller_contract is not None:
            raise HandoffError("caller-contract-unexpected")
        return
    contract = _require_object(caller_contract, "caller-contract")
    if set(contract) != {"format", "evidence", "output"}:
        raise HandoffError("caller-contract-fields-invalid")
    _require_string(contract.get("format"), "caller-contract-format")
    _require_string(contract.get("evidence"), "caller-contract-evidence")
    _require_string(contract.get("output"), "caller-contract-output")


def _validate_commit_disposition(handoff: dict[str, Any]) -> None:
    """Validate an explicit remediation commit outcome without rewriting historical handoffs."""
    if "commit_disposition" not in handoff:
        return
    if handoff["skill"] != "code-remediate":
        raise HandoffError("commit-disposition-skill-mismatch")
    disposition = _require_object(handoff["commit_disposition"], "commit-disposition")
    if set(disposition) != {"status", "reason", "evidence"}:
        raise HandoffError("commit-disposition-fields-invalid")
    status = _require_string(disposition["status"], "commit-disposition-status")
    if status not in {"pending", "blocked", "not-applicable", "declined", "committed"}:
        raise HandoffError("commit-disposition-status-invalid")
    _require_string(disposition["reason"], "commit-disposition-reason")
    _require_string(disposition["evidence"], "commit-disposition-evidence")


def validate_handoff(payload: object) -> dict[str, Any]:
    """Validate and return one canonical final-handoff payload."""
    return _validate_handoff(payload)


def _visible_line(text: str) -> str:
    """Collapse text to one line, turning control characters into spaces and dropping invisible format characters.

    Format characters (Unicode category ``Cf``) include bidirectional overrides and isolates, which can reorder how the
    surrounding text displays without appearing themselves.
    """
    kept = "".join(
        " " if unicodedata.category(char) == "Cc" else "" if unicodedata.category(char) == "Cf" else char
        for char in text
    )
    return " ".join(kept.split())


def _escape_block_opening(line: str) -> str:
    """Backslash-escape the marker that would make a paragraph line open a Markdown block; keep other lines as is."""
    opening = _BLOCK_OPENING.match(line)
    if not opening:
        return line
    # An ordered item needs its delimiter escaped, never its number; every other marker needs its first character.
    marker = opening.end() - 1 if opening[0][-1] in ".)" else opening.start()
    return f"{line[:marker]}\\{line[marker:]}"


def _backtick_runs(line: str) -> dict[int, list[tuple[int, int]]]:
    """Group the spans of every maximal backtick run in a line by run length, in line order."""
    runs: dict[int, list[tuple[int, int]]] = {}
    for run in _BACKTICK_RUN.finditer(line):
        runs.setdefault(len(run[0]), []).append(run.span())
    return runs


def _code_span_end(line: str, start: int, runs: dict[int, list[tuple[int, int]]]) -> int | None:
    """Return where the code span opened by the backtick run at ``start`` ends, or None when no run closes it.

    The closing run is the next maximal run of the same length in the raw text, ignoring backslashes and every other
    construct, as CommonMark does. ``runs`` is the line's ``_backtick_runs``, so each lookup is a binary search.
    """
    opening = _BACKTICK_RUN.match(line, start)
    candidates = runs.get(opening.end() - start, [])
    position = bisect.bisect_left(candidates, (opening.end(), 0))
    return candidates[position][1] if position < len(candidates) else None


def _skip_spaces(line: str, index: int) -> int:
    """Return the first position at or after ``index`` that is not a space."""
    while index < len(line) and line[index] == " ":
        index += 1
    return index


def _delimited_end(line: str, start: int, closer: str, forbidden: str) -> int | None:
    """Return the index just past ``closer`` for text opened at ``start``, or None at a forbidden character.

    A backslash makes the next character literal, so an escaped closer or forbidden character does not count.
    """
    index = start + 1
    while index < len(line):
        if line[index] == "\\":
            index += 2
        elif line[index] == closer:
            return index + 1
        elif line[index] in forbidden:
            return None
        else:
            index += 1
    return None


def _raw_destination_end(line: str, start: int) -> int | None:
    """Return where a link destination without angle brackets ends: at a space or an unbalanced ``)``.

    Returns None for unbalanced parentheses at the end of the line. Past ``_MAX_DESTINATION_PARENS`` open parentheses it
    returns the end of the line, which treats the rest as consumed and keeps the scan of a line linear.
    """
    depth, index = 0, start
    while index < len(line) and line[index] != " ":
        if line[index] == "\\" and line[index + 1 : index + 2] in _ASCII_PUNCTUATION:
            index += 1
        elif line[index] == "(":
            depth += 1
            if depth > _MAX_DESTINATION_PARENS:
                return len(line)
        elif line[index] == ")":
            if depth == 0:
                break
            depth -= 1
        index += 1
    return index if depth == 0 else None


def _link_tail(line: str, start: int) -> tuple[int, int]:
    """Return where a link tail after ``]`` at ``start`` ends if a link consumes it, and where its raw reading ends.

    The tail is a link destination and title in parentheses or a reference label in brackets. A renderer consumes it
    whole when a link forms, so a backtick inside opens no code span. The first value is ``start`` when the text cannot
    be such a tail; the parse follows CommonMark and accepts a tail whether or not an opening bracket or link definition
    exists, which only ever treats more text as consumed. The second value is one past the last character a renderer
    reads raw while trying the destination and title, link formed or not: markdown-it, for example, resumes there with a
    reference-label lookahead after a failed inline link.
    """
    if line.startswith("[", start):
        end = _delimited_end(line, start, "]", "[")
        return (start if end is None else end), start
    if not line.startswith("(", start):
        return start, start
    index = _skip_spaces(line, start + 1)
    if line.startswith("<", index):
        destination = _delimited_end(line, index, ">", "<")
    else:
        destination = _raw_destination_end(line, index)
    if destination is None:
        return start, index + 1
    if destination == len(line):
        # A destination that may run to the end of the line could still be consumed by a link.
        return destination, destination
    index = _skip_spaces(line, destination)
    if index > destination and index < len(line) and line[index] in "\"'(":
        parenthesized = line[index] == "("
        title = _delimited_end(line, index, ")" if parenthesized else line[index], "(" if parenthesized else "")
        if title is None:
            return start, index + 1
        index = _skip_spaces(line, title)
    return (index + 1 if line.startswith(")", index) else start), index + 1


def _escape_raw_html(line: str, *, trust_code_spans: bool) -> tuple[str, bool]:
    """Backslash-escape every ``<`` that could open raw HTML, reading the line from left to right as CommonMark does.

    Backslash escapes and URI or email autolinks are copied unchanged, because a renderer reads no raw HTML inside them;
    so are code spans when ``trust_code_spans`` is set. A link destination, title or reference label after ``]`` is
    treated as consumed by a link, so a backtick there opens no code span; when no link forms, this only leaves more
    text exposed to escaping. Returns the escaped line and whether its code spans can be trusted: every backtick belongs
    to a copied code span and none lies where a renderer reads a link tail raw.
    """
    pieces: list[str] = []
    runs = _backtick_runs(line)
    index, consumed_until, paired, read_raw = 0, 0, 0, False
    while index < len(line):
        char, piece = line[index], None
        if char == "\\" and line[index + 1 : index + 2] in _ASCII_PUNCTUATION:
            end = index + 2
        elif char == "`" and trust_code_spans and index >= consumed_until:
            span_end = _code_span_end(line, index, runs)
            end = _BACKTICK_RUN.match(line, index).end() if span_end is None else span_end
            paired += 0 if span_end is None else line.count("`", index, end)
        elif char == "<" and (autolink := _AUTOLINK.match(line, index)):
            end = autolink.end()
        else:
            end = index + 1
            if char == "<" and line[index + 1 : index + 2] in _RAW_HTML_START:
                piece = "\\<"
            elif char == "]":
                consumed, read_end = _link_tail(line, end)
                consumed_until = max(consumed_until, consumed)
                # A lookahead resuming after a raw read can start inside a code span opened by a backtick read there.
                read_raw = read_raw or "`" in line[end:read_end]
        pieces.append(piece or line[index:end])
        index = end
    return "".join(pieces), paired == line.count("`") and not read_raw


def _escape_line_html(line: str) -> str:
    """Escape raw HTML openings, keeping code spans as written only when the line pairs its backticks unambiguously.

    A backtick left unpaired, or read raw inside a link tail, lets renderers disagree about which runs pair: markdown-
    it, for example, caches a backtick scan made during a link-label lookahead, which can start after a failed link
    tail, and then misses a closer the specification would find. Such a line therefore does not trust its code spans,
    and a tag-like ``<`` inside them is escaped too, at the cost of a visible backslash.
    """
    escaped, all_paired = _escape_raw_html(line, trust_code_spans=True)
    return escaped if all_paired else _escape_raw_html(line, trust_code_spans=False)[0]


def inline_text(text: str) -> str:
    """Return free text as one Markdown paragraph line that cannot open a block or raw HTML, keeping inline markup.

    Whitespace and line breaks collapse to single spaces and invisible format characters are dropped. A leading block
    marker is backslash-escaped so it displays literally. A leading star, underscore, bracket or backtick run is
    escaped only in its block form, so bold, emphasis, links and code spans opening the text still render.

    Every ``<`` that could open raw HTML is escaped too, because renderers such as GitHub pass inline tags like ``<h1>``
    through and display them as blocks. URI and email autolinks keep their ``<``, and so do code spans on a line whose
    every backtick pairs into a code span outside any link destination or title. On any other line a tag-like ``<``
    inside a code span is escaped as well and shows a backslash. A link destination in angle brackets that is not an
    absolute URI is escaped too, so such a link may change target.

    Escaping repeats until the line is stable, so the result is unchanged when normalized again, which lets a validator
    require already-normalized text.

    Examples:
        >>> inline_text("x\\n\\n| Recommendation | Merge |\\n\\n# Approved")
        'x | Recommendation | Merge | # Approved'
        >>> print(inline_text("# Approved"), inline_text("12. Listed"), inline_text("2026 release"))
        \\# Approved 12\\. Listed 2026 release
        >>> print(inline_text("x <h1>Approved</h1> a < b"), inline_text("\\\\\\\\<b>"))
        x \\<h1>Approved\\</h1> a < b \\\\\\<b>
        >>> print(inline_text("**Fixed** `List<int>` per <https://x.example>"), inline_text("* item"))
        **Fixed** `List<int>` per <https://x.example> \\* item
    """
    line = _visible_line(text)
    # Each escape can change how later text pairs (an escaped angle destination may stop being a link), so repeat
    # until the scan of the final text itself adds nothing.
    while (normalized := _escape_line_html(_escape_block_opening(line))) != line:
        line = normalized
    return line


def _closed_review_view(handoff: dict[str, Any]) -> dict[str, Any]:
    """Return a close handoff's reason and maintainer actions with every string normalized to inline text.

    The close presentation renders only from this view, so a free-text field the validator does not name still cannot
    open a Markdown block. Pattern-validated identifiers such as the close code, PR URL and commit are unchanged by it.
    """
    return {
        "close_reason": {key: inline_text(value) for key, value in handoff["close_reason"].items()},
        "remaining": [{key: inline_text(value) for key, value in item.items()} for item in handoff["remaining"]],
    }


def literal_code_span(text: str) -> str:
    """Return untrusted text as one inline code span, which Markdown renderers display literally.

    Code span content is never parsed for links, automatic links, emphasis or HTML, including by renderers that find
    bare URLs after parsing. Control and invisible format characters such as bidirectional overrides are removed first.
    The fence is one backtick longer than the longest backtick run in the text, and text that starts or ends with a
    backtick is padded with a space on both sides, which renderers strip again. ``Reason:`` keeps a no-break space so
    the text cannot start the technical detail that a summary's first `` Reason: `` introduces. Empty text stays empty.

    Examples:
        >>> print(literal_code_span("Fix https://x.example *now*"))
        `Fix https://x.example *now*`
        >>> print(literal_code_span("Use `code` here"))
        ``Use `code` here``
        >>> literal_code_span("\\u202e")
        ''
    """
    line = _visible_line(text).replace("Reason: ", "Reason:\u00a0")
    if not line:
        return ""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", line)), default=0) + 1)
    pad = " " if line.startswith("`") or line.endswith("`") else ""
    return f"{fence}{pad}{line}{pad}{fence}"


def _review_pr_subject(handoff: dict[str, Any]) -> tuple[str, str]:
    """Read the base repository, number and display-safe title from the source-bound PR snapshot link."""
    values = [
        row["cells"][1]
        for table in handoff["tables"]
        if table["heading"] == "PR Snapshot"
        for row in table["rows"]
        if row["cells"][0] == "PR"
    ]
    # The canonical snapshot escapes brackets and HTML in the collected title. Keep those escapes as text.
    label = re.fullmatch(
        r"\[#(?P<number>[1-9][0-9]*) — (?P<title>[^<>\[\]]+)\]\("
        r"(?P<url>(?i:https://github[.]com)/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)"
        r"/pull/(?P=number))\)",
        values[0] if len(values) == 1 else "",
    )
    if not label:
        raise HandoffError("review-pr-subject-invalid")
    title = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", label["title"]).split())
    if not title:
        raise HandoffError("review-pr-subject-invalid")
    return f"Review PR {label['repo']}#{label['number']} — {title}", label["url"]


def _validate_handoff(
    payload: object,
    *,
    allow_empty_remediation: bool = False,
    owned_actions: bool = False,
    derive_source_counts: bool = False,
) -> dict[str, Any]:
    """Validate a handoff while preserving the original presentation's empty-result rule."""
    handoff = _require_object(payload, "handoff")
    presentation_version = handoff.get("presentation_version")
    if type(presentation_version) is int and presentation_version in {8, 9, 10, 11, 12, 13, 14, 15, 16}:
        # Only terminal layout changes; use the existing branch's complete factual contract.
        previous = 7 if handoff.get("skill") == "code-review" and handoff.get("branch") == "assessed" else 3
        if handoff.get("skill") == "code-remediate":
            previous = 6
        base = {**handoff, "presentation_version": previous}
        if "action_policy" in base:
            if presentation_version not in {15, 16} or base.pop("action_policy") not in ("owned", "best-way-forward"):
                raise HandoffError("handoff-action-policy-invalid")
            owned_actions = True
        _validate_handoff(
            base,
            owned_actions=owned_actions,
            derive_source_counts=derive_source_counts,
            allow_empty_remediation=handoff.get("skill") == "code-remediate"
            and presentation_version in {11, 12, 13, 14, 15},
        )
        if presentation_version == 16:
            if handoff["skill"] != "code-review" or handoff["branch"] != "assessed":
                raise HandoffError("handoff-presentation-version-invalid")
            _review_pr_subject(handoff)
        if handoff.get("action_policy") == "best-way-forward" and (
            handoff["branch"] in {"closed", "caller-contract"} or len(handoff["next_steps"]) != 1
        ):
            raise HandoffError("best-way-forward-requires-one-step")
        if derive_source_counts:
            handoff.setdefault("source_coverage", base["source_coverage"])
        return handoff
    expected_fields = HANDOFF_FIELDS if presentation_version is None else HANDOFF_FIELDS | {"presentation_version"}
    if presentation_version == 17:
        expected_fields = expected_fields | {"close_reason"}
    if presentation_version == 17 and "action_policy" in handoff:
        expected_fields = expected_fields | {"action_policy"}
        if handoff["action_policy"] != "owned":
            raise HandoffError("handoff-action-policy-invalid")
    if "commit_disposition" in handoff:
        expected_fields = expected_fields | {"commit_disposition"}
    if derive_source_counts and "source_coverage" not in handoff:
        expected_fields = expected_fields - {"source_coverage"}
    if "child_runs" in handoff and handoff.get("skill") == "challenge-resolve" and presentation_version == 3:
        expected_fields = expected_fields | {"child_runs"}
    if set(handoff) != expected_fields:
        missing = ",".join(sorted(expected_fields - handoff.keys()))
        unexpected = ",".join(sorted(handoff.keys() - expected_fields))
        raise HandoffError(f"handoff-fields-mismatch:missing={missing}:unexpected={unexpected}")
    presentation_versions = {2, 3}
    if handoff.get("skill") == "code-remediate":
        presentation_versions.update({4, 5, 6})
    elif handoff.get("skill") == "code-review" and handoff.get("branch") == "assessed":
        presentation_versions.add(7)
    elif handoff.get("skill") == "code-review" and handoff.get("branch") == "closed":
        presentation_versions.add(17)
    if presentation_version is not None and (
        type(presentation_version) is not int or presentation_version not in presentation_versions
    ):
        raise HandoffError("handoff-presentation-version-invalid")
    if type(handoff.get("schema_version")) is not int or handoff["schema_version"] != SCHEMA_VERSION:
        raise HandoffError("handoff-schema-version-invalid")
    skill = _require_string(handoff.get("skill"), "skill")
    if skill not in SUPPORTED_SKILLS:
        raise HandoffError(f"skill-unsupported:{skill}")
    branch = _validate_branch(skill, handoff.get("branch"))
    outcome = _require_object(handoff.get("outcome"), "outcome")
    if set(outcome) != {"title", "summary"}:
        raise HandoffError("outcome-fields-invalid")
    outcome_title = _require_string(outcome.get("title"), "outcome-title")
    outcome_summary = _require_string(outcome.get("summary"), "outcome-summary")
    if skill == "code-review" and branch == "assessed":
        if outcome_title != "Review Decision" or outcome_summary not in {
            f"Recommendation: {recommendation}." for recommendation in (*REVIEW_RECOMMENDATIONS, None)
        }:
            raise HandoffError("review-outcome-not-canonical")
        _require_source_vocabulary(outcome_summary, handoff.get("tables"))
    elif presentation_version == 17:
        # Every conclusive close, a duplicate included, is a rejection with its reason and reviewed commit.
        reason = _require_object(handoff["close_reason"], "close-reason")
        if set(reason) != {"code", "pr_url", "head_sha", "summary", "rationale"}:
            raise HandoffError("close-reason-fields-invalid")
        for field, value in reason.items():
            _require_string(value, f"close-reason-{field}")
        if (
            reason["code"] not in CLOSE_CODES
            or not re.fullmatch(
                r"(?i:https://github[.]com)/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*/pull/[1-9][0-9]*",
                reason["pr_url"],
            )
            or not re.fullmatch(r"[0-9a-f]{40}", reason["head_sha"])
        ):
            raise HandoffError("close-reason-invalid")
        if outcome_title != "Review Decision" or outcome_summary != "Recommendation: Reject.":
            raise HandoffError("review-outcome-not-canonical")
        # Free text renders inline, so it must already be one line that cannot open a Markdown block.
        if any(inline_text(reason[field]) != reason[field] for field in ("summary", "rationale")):
            raise HandoffError("close-text-not-inline")
    if "child_runs" in handoff:
        runs = handoff["child_runs"]
        if not isinstance(runs, list) or not runs:
            raise HandoffError("challenge-child-runs-invalid")
        for run in runs:
            if (
                not isinstance(run, dict)
                or set(run)
                not in ({"result_path", "scores", "decision"}, {"result_path", "scores", "decision", "reason"})
                or not isinstance(run.get("result_path"), str)
                or not run["result_path"].strip()
                or not isinstance(run.get("scores"), list)
                or any(type(score) is not int or score < 0 for score in run["scores"])
                or not isinstance(run.get("decision"), str)
                or not run["decision"].strip()
                or ("reason" in run and (not isinstance(run["reason"], str) or not run["reason"].strip()))
            ):
                raise HandoffError("challenge-child-runs-invalid")
    row_ids, represented_sources = _validate_tables(
        handoff, skill, branch, allow_empty_remediation=allow_empty_remediation
    )
    if "child_runs" in handoff and [row["cells"] for table in handoff["tables"] for row in table["rows"]] != [
        ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]
    ]:
        raise HandoffError("challenge-child-runs-scope-invalid")
    _validate_source_coverage(handoff, branch, represented_sources, derive_counts=derive_source_counts)
    _validate_verification(handoff)
    _validate_commit_disposition(handoff)
    _validate_unavailable_review_verification(handoff, skill, branch, presentation_version)
    _validate_remaining(handoff, row_ids, branch, owned_actions=owned_actions)
    _validate_confidence(handoff)
    _validate_artifacts(handoff)
    _validate_caller_contract(handoff, branch)
    return handoff


def _table_cell(value: str) -> str:
    """Escape one Markdown table cell without truncating its content."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _render_table(
    table: dict[str, Any],
    *,
    suppress_heading: bool = False,
    show_selection: bool = False,
    show_resolution: bool = False,
    report_root: Path | None = None,
    review_report: Path | None = None,
) -> list[str]:
    """Render one validated table and its symbol details deterministically."""
    if table.get("layout") in {"grouped", "concise"}:
        return _render_grouped_table(
            table,
            suppress_heading=suppress_heading,
            show_selection=show_selection,
            show_resolution=show_resolution,
            role_only=review_report is not None,
        )
    if table.get("layout") == "challenge-history":
        if report_root is None:
            raise HandoffError("challenge-history-report-root-required")
        return _render_challenge_history(table, report_root, suppress_heading=suppress_heading)
    columns = list(table["columns"])
    attributed = any("authors" in row for row in table["rows"])
    if attributed:
        columns.insert(1, "Author")
    lines = [] if suppress_heading else [f"**{table['heading']}**", ""]
    if table.get("summary"):
        lines.extend((table["summary"], ""))
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in table["rows"]:
        cells = list(row["cells"])
        if attributed:
            cells.insert(
                1,
                ", ".join(
                    author.removesuffix(" (parent substitute)") if review_report is not None else author
                    for author in row["authors"]
                ),
            )
        lines.append("| " + " | ".join(_table_cell(cell) for cell in cells) + " |")
    report_row = None
    if review_report is not None and table["heading"] in {"PR Snapshot", "Review Snapshot"}:
        destination = quote(review_report.as_posix(), safe="/:")
        report_row = f"| Report | [Markdown report](<{_table_cell(destination)}>) |"
    if table.get("reviewers"):
        if review_report is not None:
            reviewers = ", ".join(
                f"{item['role'].removesuffix(' (parent substitute)')} "
                f"[{item['rating'] if item['rating'] is not None else 'no source judgment'}]"
                for item in table["reviewers"]
            )
        else:
            reviewers = ", ".join(
                f"{item['role']} ({item['rating'] if item['rating'] is not None else 'no source judgment'})"
                for item in table["reviewers"]
            )
        current = _source_rating_version(table) is not None
        lines.append(f"| Reviewers | {_table_cell(reviewers)}. |")
        if report_row is not None:
            lines.append(report_row)
        lines.extend(("", SOURCE_REVIEWER_LEGEND if current else REVIEWER_LEGEND))
        if current:
            lines.extend(("", f"Verification readiness: {table['verification_readiness']}."))
    elif report_row is not None:
        lines.append(report_row)
    if table.get("details"):
        lines.append("")
        lines.extend(
            f"[{detail['id']}] {detail['text'].replace(chr(13) + chr(10), '<br>').replace(chr(10), '<br>')}"
            for detail in table["details"]
        )
    return lines


def _render_challenge_history(table: dict[str, Any], report_root: Path, *, suppress_heading: bool) -> list[str]:
    """Show complete labelled history with compact cells and linked resources below."""
    display = {**table, "layout": "legacy", "rows": []}
    resources: dict[Path, str] = {}
    reasons = []
    for row in table["rows"]:
        target = challenge_report_path(report_root, row["cells"][-1])
        resource = resources.setdefault(target, f"R{len(resources) + 1}")
        cells = list(row["cells"])
        cells[-1] = f"[{resource}]"
        if row["admission"] == "advisory":
            cells[-2] = f"advisory — {cells[-2]}"
            reasons.append(f"- {cells[0]}: {_table_cell(row['admission_reason'])}")
        elif row["admission"] == "validated":
            cells[-2] = f"validated — {cells[-2]}"
        display["rows"].append({**row, "cells": cells})
    lines = _render_table(display, suppress_heading=suppress_heading)
    lines.extend(("", "Counts are old + new open findings; advisory history never establishes certified completion."))
    if not any(row["admission"] == "validated" for row in table["rows"]):
        message = (
            "Coordinator coverage is recorded by validated child runs."
            if table["rows"][0]["cells"][-2] == "chunk-only"
            else "No validated rounds; certification remains unavailable."
        )
        lines.extend(("", message))
    if reasons:
        lines.extend(("", *reasons))
    lines.extend(("", "**Resources**", ""))
    lines.extend(
        f"- [{resource}] [{resource} — {_table_cell(target.name)}](<{quote(target.as_posix(), safe='/:')}>)"
        for target, resource in resources.items()
    )
    return lines


def _render_grouped_table(
    table: dict[str, Any],
    *,
    suppress_heading: bool = False,
    show_selection: bool = False,
    show_resolution: bool = False,
    role_only: bool = False,
) -> list[str]:
    """Render bound rows, retaining historical bytes and separating concise facts from evidence."""
    remediation = tuple(table["columns"]) == STANDARD_COLUMNS["code-remediate"]
    concise = table.get("layout") == "concise"
    if remediation and not table["rows"]:
        lines = [] if suppress_heading else [f"**{table['heading']}**", ""]
        return [*lines, "No findings to remediate."]
    columns = ["ID", "Severity", "Finding", "Outcome"] if remediation else ["ID", "Finding", "Status"]
    if concise:
        columns.insert(-1, "Selected" if show_selection else "Resolution" if remediation else "Resolution proposal")
    if concise and remediation and show_resolution:
        columns.append("How resolved")
    attributed = not remediation and any("authors" in row for row in table["rows"])
    if attributed:
        columns.insert(1, "Author")
    lines = [] if suppress_heading else [f"**{table['heading']}**", ""]
    lines.extend(
        (
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        )
    )
    definitions = {entry["id"]: entry["text"] for entry in table.get("details", [])}
    for row in table["rows"]:
        cells = row["cells"]
        overview = [*cells[:3], cells[4].split(" — [", 1)[0]] if remediation else [cells[0], row["title"], cells[3]]
        if concise:
            resolution = cells[1]
            if remediation:
                resolution = re.sub(
                    r"\[([^\]]+)\]",
                    lambda match: definitions.get(match[1], match[0]),
                    cells[4].split(" — ", 1)[-1],
                )
                disposition, separator, reason = resolution.partition(": ")
                if separator:
                    resolution = reason
                    overview[-1] = disposition
            selection = row.get("selected")
            if type(selection) is bool:
                selection = "Yes" if selection else "No"
            overview.insert(-1, selection if show_selection else resolution)
            if remediation and show_resolution:
                # Reuse the bound reason; selection must not hide how this item was handled.
                overview.append(resolution)
        if attributed:
            overview.insert(
                1,
                ", ".join(
                    author.removesuffix(" (parent substitute)") if role_only else author for author in row["authors"]
                ),
            )
        lines.append("| " + " | ".join(_table_cell(value) for value in overview) + " |")
    if table.get("overview_only"):
        return lines
    for row in table["rows"]:
        cells = row["cells"]
        title = cells[2] if remediation else row["title"]
        heading = _table_cell(cells[0]) if concise else f"{_table_cell(cells[0])} — {_table_cell(title)}"
        lines.extend(("", f"**{heading}**", ""))
        if row.get("summary"):
            lines.append(f"- {'Context' if concise else 'Issue'}: {_table_cell(row['summary'])}")
        fields = zip(table["columns"][3:], cells[3:]) if remediation else zip(table["columns"][1:], cells[1:])
        for label, value in fields:
            if concise and label in {"Required change", "Status", "Outcome"}:
                continue
            # Expand only declared symbols; source IDs remain literal and fully visible.
            if label == "Sources":
                lines.append("- Sources:")
                lines.extend(f"  - {source}" for source in value.splitlines())
                continue
            expanded = re.sub(r"\[([^\]]+)\]", lambda match: definitions.get(match[1], match[0]), value)
            lines.append(f"- {label}: " + expanded.replace("\r\n", "\n").replace("\n", "\n  "))
        if row.get("closure_evidence"):
            lines.append(f"- Done when: {_table_cell(row['closure_evidence'])}")
    return lines


def render_review_report_details(handoff: dict[str, Any]) -> str:
    """Render the complete finding section that must survive compact terminal presentation.

    The producer writes this section beside the existing action table; rendering checks its exact content so omission or
    later edits cannot silently hide canonical context and closure obligations behind the report link.
    """
    rows = [
        row
        for table in handoff.get("tables", [])
        if table.get("heading") == "Review Findings and Merge Blocks"
        for row in table["rows"]
    ]
    if not rows:
        return ""
    lines = ["## Finding Details", "", "<!-- codex-rig:review-details -->", ""]
    for row in rows:
        cells = row["cells"]
        lines.extend((f"### {_table_cell(cells[0])} — {_table_cell(row['title'])}", ""))
        fields = (
            ("Context", row.get("summary")),
            ("Required change", cells[1]),
            ("Evidence", cells[2]),
            ("Done when", row.get("closure_evidence")),
        )
        lines.extend(f"- {label}: {_table_cell(value)}" for label, value in fields if value)
        lines.append("")
    return "\n".join(lines)


def _report_identity(report_path: str) -> str:
    """Name the review run a report path belongs to, so all views of one run share finding identity.

    Code-review runs live at ``<root>/pr-<number>/run-<NNN>/``. Every artifact under one such directory (the JSON
    result, ``review-notes.md``) describes the same review, while two runs of one pull request may legitimately reuse a
    finding ID. The identity therefore keeps the path up to and including the ``run-<NNN>`` segment; the report root
    stays in the key because each root allocates run numbers independently. Paths outside that topology keep their full
    file path.

    Examples:
        >>> _report_identity(".reports/codex/code-review/pr-12/run-003/result.json")
        '.reports/codex/code-review/pr-12/run-003'
        >>> _report_identity("C:\\\\work\\\\.reports\\\\codex\\\\code-review\\\\pr-12\\\\run-003\\\\review-notes.md")
        'C:/work/.reports/codex/code-review/pr-12/run-003'
        >>> _report_identity("first.json")
        'first.json'
    """
    parts = PurePosixPath(report_path.replace("\\", "/")).parts
    for index in range(len(parts) - 1, 0, -1):
        if re.fullmatch(r"run-[0-9]{3,}", parts[index]) and re.fullmatch(r"pr-[1-9][0-9]*", parts[index - 1]):
            return PurePosixPath(*parts[: index + 1]).as_posix()
    return PurePosixPath(*parts).as_posix() if parts else report_path


def _validate_selection(payload: object) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """Validate source ownership, counts and choice before rendering any selection context.

    Null selected_indexes means awaiting input; a list records the confirmed choice. All items, including closed
    nonselectable entries, participate in identity and source counts. Related mentions never count as new sources.
    """
    inventory = _require_object(payload, "selection")
    presentation = inventory.get("presentation_version", 2)
    if type(presentation) is not int or presentation not in {2, 3, 4, 5}:
        raise HandoffError("selection-presentation-version-invalid")
    if (
        type(inventory.get("schema_version")) is not int
        or inventory["schema_version"] != 1
        or not isinstance(inventory.get("items"), list)
    ):
        raise HandoffError("selection-schema-invalid")
    items = inventory["items"]
    identities: set[str] = set()
    sources: set[tuple[str, str]] = set()
    canonical: set[tuple[str, str]] = set()
    selectable = []
    grouped = 0
    for item in items:
        item = _require_object(item, "selection-item")
        for field in ("input_item_id", "item_name", "item_type", "severity", "summary", "closure_evidence"):
            _require_string(item.get(field), f"selection-item-{field}")
        identity = item["input_item_id"]
        if (
            identity != identity.strip()
            or identity in identities
            or item["severity"] not in {"critical", "high", "medium", "low"}
        ):
            raise HandoffError("selection-item-identity-or-severity-invalid")
        identities.add(identity)
        if not isinstance(item.get("selectable"), bool):
            raise HandoffError("selection-item-selectable-invalid")
        if item["selectable"]:
            if presentation in {3, 4, 5}:
                _require_string(item.get("resolution_proposal"), "selection-item-resolution_proposal")
            selectable.append(item)
        records = item.get("sources")
        if not isinstance(records, list) or not records:
            raise HandoffError("selection-sources-missing")
        grouped += len(records) > 1
        for source in records:
            source = _require_object(source, "selection-source")
            for field in ("kind", "source_id", "location", "body", "evidence"):
                _require_string(source.get(field), f"selection-source-{field}")
            kind, source_id = source["kind"], source["source_id"]
            if (
                kind not in {"report", "online", "user"}
                or source_id != source_id.strip()
                or re.search(r"[\r\n\[\]]", source_id)
            ):
                raise HandoffError("selection-source-invalid")
            if (kind, source_id) in sources:
                raise HandoffError("selection-source-duplicate")
            if kind == "online" and re.fullmatch(r"(?!https?://)\S+", source_id, re.IGNORECASE) is None:
                raise HandoffError("selection-online-source-invalid")
            if kind == "user" and presentation not in {4, 5}:
                raise HandoffError("selection-user-source-requires-v4")
            if kind == "user" and re.fullmatch(r"user-[A-Za-z0-9_-]+#finding-[1-9]\d*", source_id) is None:
                raise HandoffError("selection-user-source-invalid")
            sources.add((kind, source_id))
            if kind == "report":
                match = re.fullmatch(r"(.+?)(?::[1-9][0-9]*|#([^#\s]+))", source_id)
                if match is None:
                    raise HandoffError("selection-report-source-invalid")
                if match[2] is not None and not match[1].lower().endswith(".json"):
                    raise HandoffError("selection-report-source-invalid")
                finding_id = source.get("finding_id", match[2])
                if finding_id is not None:
                    _require_string(finding_id, "selection-source-finding-id")
                    if match[2] is not None and match[2] != finding_id:
                        raise HandoffError("selection-source-finding-id-mismatch")
                    report_id = source.get("report_id", _report_identity(match[1]))
                    _require_string(report_id, "selection-source-report-id")
                    key = (report_id, finding_id)
                    if key in canonical:
                        raise HandoffError("selection-canonical-finding-duplicate")
                    canonical.add(key)
            if "related_mentions" in source:
                _require_string_list(source["related_mentions"], "selection-related-mentions")
    counts = {"source_records_total": len(sources), "grouped_items_total": grouped, "items_total": len(items)}
    for key, value in counts.items():
        if key in inventory and (type(inventory[key]) is not int or inventory[key] != value):
            raise HandoffError(f"selection-count-mismatch:{key}")
    selected = inventory.get("selected_indexes")
    if selected is not None and (
        not isinstance(selected, list)
        or any(type(index) is not int or not 1 <= index <= len(selectable) for index in selected)
        or len(set(selected)) != len(selected)
    ):
        raise HandoffError("selection-index-invalid")
    return inventory, selectable, counts


def _selection_pr_subject(run: Path) -> str | None:
    """Identify the base PR for chat from this run's collector metadata."""
    path = run / "pr" / "pr.json"
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    # Only the selection CLI needs the collector's URL grammar. Renderers are also loaded as standalone files.
    from create_run import PR_URL_PATTERN, same_pr_url

    try:
        pr = _load_json(path)
        routing = path.with_name("pr-routing.json")
        if (routing.exists() or routing.is_symlink()) and not same_pr_url(
            _load_json(routing).get("pr_url"), pr.get("url")
        ):
            return None
    except HandoffError:
        return None
    url = pr.get("url")
    target = PR_URL_PATTERN.fullmatch(url) if isinstance(url, str) else None
    if not target or type(pr.get("number")) is not int or str(pr["number"]) != target["number"]:
        return None
    identity = f"Remediate PR {target['owner']}/{target['repository']}#{target['number']}"
    title = pr.get("title")
    if isinstance(title, str):
        title = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", title).split())
        if title:
            return f"{identity} — {title}"
    return identity


def render_selection(payload: object) -> str:
    """Render a validated inventory as a short overview with named evidence groups."""
    if isinstance(payload, dict) and payload.get("presentation_version") == 5:
        return _externalize_table_paths(render_selection({**payload, "presentation_version": 4}))
    inventory, selectable, counts = _validate_selection(payload)
    items = inventory["items"]
    selected = inventory.get("selected_indexes")
    grouped = counts["grouped_items_total"]
    source_count = counts["source_records_total"]
    lines = [
        "## Resolution Scope Selection",
        "",
        "Choose by #; IDs identify findings and do not change with selection order.",
        "",
    ]
    if not selectable:
        lines.extend(
            ("No selectable findings (none-selectable).", "", "Selected indexes: none. Deferred indexes: none.")
        )
    elif selected is None:
        high = ", ".join(item["input_item_id"] for item in selectable if item["severity"] in {"critical", "high"})
        lines.append(f"Awaiting selection. High-priority findings: {high or 'none'}.")
    else:
        deferred = [item["input_item_id"] for index, item in enumerate(selectable, 1) if index not in selected]
        lines.extend(
            (
                f"Confirmed selected indexes: {', '.join(map(str, selected)) or 'none'}.",
                "",
                f"Deferred by your selection: {', '.join(deferred) or 'none'}.",
            )
        )
    lines.extend(
        (
            "",
            f"Items: {len(items)}; selectable: {len(selectable)}; sources: {source_count}; grouped items: {grouped}.",
            "",
            "| # | Severity | Finding | Resolution proposal | Sources |"
            if inventory.get("presentation_version") in {3, 4}
            else "| # | Severity | Finding |",
            "| --- | --- | --- | --- | --- |"
            if inventory.get("presentation_version") in {3, 4}
            else "| --- | --- | --- |",
        )
    )
    for index, item in enumerate(selectable, 1):
        extra = ""
        if inventory.get("presentation_version") in {3, 4}:
            # Aggregate only genuine sources; related mentions are not independent evidence.
            kinds = [source["kind"] for source in item["sources"]]
            tags = "; ".join(f"{kind} ×{kinds.count(kind)}" for kind in dict.fromkeys(kinds))
            extra = f" {_table_cell(item['resolution_proposal'])} | {tags} |"
        lines.append(
            f"| {index} | {item['severity']} | {_table_cell(item['input_item_id'])} — {_table_cell(item['item_name'])} |{extra}"
        )
    for index, item in enumerate(selectable, 1):
        concise = inventory.get("presentation_version") in {3, 4}
        title = "" if concise else f" — {_table_cell(item['item_name'])}"
        lines.extend(
            (
                "",
                f"### {index} · {_table_cell(item['input_item_id'])}{title}",
                "",
                f"- {'Context' if concise else 'Issue'}: {_table_cell(item['summary'])}",
                f"- Done when: {_table_cell(item['closure_evidence'])}",
            )
        )
        for source in item["sources"]:
            lines.append(f"- Evidence: {source['kind']} [{source['source_id']}]")
            if source.get("related_mentions"):
                lines.append(
                    f"  - Related mentions: {', '.join(_table_cell(value) for value in source['related_mentions'])}"
                )
    if "pr_relevance" in inventory:
        relevance = _require_object(inventory["pr_relevance"], "selection-pr-relevance")
        lines.extend(("", "## PR Relevance Summary", ""))
        for field in (
            "connected_open_items_total",
            "connected_selectable_items_total",
            "connected_required_followup_total",
            "connected_items_marked_out_of_scope",
        ):
            if type(relevance.get(field)) is not int or relevance[field] < 0:
                raise HandoffError(f"selection-pr-relevance-invalid:{field}")
            lines.append(f"- {field.replace('_', ' ')}: {relevance[field]}")
    return "\n".join(lines) + "\n"


def _externalize_table_paths(
    markdown: str,
    *,
    readable_breaks: bool = False,
    keep_verification_table: bool = False,
    confidence_table: bool = False,
    actionable_summary: bool = False,
) -> str:
    """Move table destinations into item-labelled lists, retaining surrounding action text.

    Input is our deterministic table renderer, not an arbitrary Markdown parser. Historical renderings never enter this
    boundary. Explicit links and recognized quoted paths have known boundaries. Ambiguous plain or quoted cells move
    verbatim to labelled details instead of guessing path boundaries or classifying code as a filename.
    """
    # Complex inline targets stay in the lossless detail fallback; never extract a balanced target's prefix.
    link = re.compile(r"\[([^\]]+)\]\((<[^>]+>|[^\s()]+)\)")
    filename = re.compile(r"[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,11}(?::\d+){0,2}")
    reference = re.compile(
        r"`(?P<quoted>[^`]+)`|(?P<plain>(?<![\w])(?:[A-Za-z]:)?[^\s`<>|()\[\]]*[/\\][^\s`<>|()\[\]]+"
        r"|(?<![\w.])[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,11}(?::\d+){0,2})"
    )

    def is_path(value: str, *, quoted: bool = False) -> bool:
        """Distinguish path references from ordinary slash-separated prose."""
        explicit_prefix = value.startswith(("/", "./", "../", "~/", "\\")) or re.match(r"[A-Za-z]:[/\\]", value)
        # Commands and expressions can contain path arguments. Without a path prefix, keep ambiguous code intact.
        if actionable_summary and quoted and not explicit_prefix and not re.fullmatch(r"[\w./:\\-]+", value):
            return False
        basename = value.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
        return bool(filename.fullmatch(basename) or explicit_prefix or (quoted and ("/" in value or "\\" in value)))

    def whole_path(value: str) -> bool:
        """Recognize an unambiguous plain path with no prose or whitespace boundary."""
        return (
            is_path(value)
            and not any(character.isspace() for character in value)
            and not any(marker in value for marker in (";", ",", "<br>", "[", "`"))
        )

    lines = markdown.splitlines()
    rendered: list[str] = []
    resource_items: dict[str, list[str]] = {}
    index = 0
    while index < len(lines):
        if not lines[index].startswith("|"):
            resource = re.fullmatch(r"- \[(R\d+)\] \[.*\]\(<(.*)>\)", lines[index])
            if resource and resource[1] in resource_items:
                rendered.extend(f"- {item}: {resource[2]}" for item in resource_items[resource[1]])
            else:
                rendered.append(lines[index])
            index += 1
            continue
        table_start = index
        table: list[list[str]] = []
        while index < len(lines) and lines[index].startswith("|"):
            table.append(
                [
                    cell.strip().replace(r"\|", "|").replace("\\\\", "\\")
                    for cell in re.split(r"(?<!\\)\|", lines[index][1:-1])
                ]
            )
            index += 1
        if len(table) < 2:
            rendered.extend("| " + " | ".join(_table_cell(cell) for cell in row) + " |" for row in table)
            continue
        # Current verification rows retain their evidence links; other tables keep external path lists.
        if keep_verification_table and table[0] == ["Check", "Status", "Evidence"]:
            rendered.extend(lines[table_start:index])
            continue
        # Evidence-only columns disappear; mixed cells retain a reference to their complete detail.
        removed = {
            column
            for column, heading in enumerate(table[0])
            if heading.casefold() == "evidence"
            and all(
                link.fullmatch(row[column])
                or whole_path(row[column])
                or filename.fullmatch(row[column])
                or re.fullmatch(r"\[R\d+\]", row[column])
                for row in table[2:]
            )
        }
        resources: list[tuple[str, str]] = []
        display = table[:2]
        for row_number, row in enumerate(table[2:], 1):
            destinations: list[str] = []
            details: list[tuple[str, str]] = []
            cells = []

            def shorten_plain(value: str, *, record: bool = True) -> str:
                """Compact references once, without interpreting rendered labels as new evidence."""

                def shorten(match: re.Match[str]) -> str:
                    quoted = match["quoted"] is not None
                    raw = match["quoted"] if quoted else match["plain"]
                    destination = raw if quoted else raw.rstrip(",;.")
                    if not is_path(destination, quoted=quoted):
                        return match[0]
                    if record:
                        destinations.append(destination)
                    suffix = "" if quoted else raw[len(destination) :]
                    return destination.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] + suffix

                return reference.sub(shorten, value)

            for column, value in enumerate(row):
                if (
                    confidence_table
                    and table[0] == ["Status", "Deduction", "Gap or limitation", "Closure evidence"]
                    and column == 1
                ):
                    cells.append(value)
                    continue
                # Visible link labels need the same path checks as prose; destinations alone cannot preserve them.
                plain = link.sub(lambda match: match[1], value)
                if readable_breaks:
                    plain = plain.replace("<br>", " ")
                ambiguous_quote = "``" in plain or (
                    not actionable_summary
                    and any(not is_path(match[1], quoted=True) for match in re.finditer(r"`([^`]+)`", plain))
                )
                plain = re.sub(r"`[^`]+`", "", plain)
                # Classify complete tokens before extracting references; a filename prefix is never evidence.
                ambiguous_name = any(
                    "." in token.rstrip(",;.")
                    and not is_path(token)
                    and not (
                        actionable_summary and re.fullmatch(r"v?\d+(?:\.\d+)+(?:[-+][\w.-]+)?", token.rstrip(",;."))
                    )
                    for token in plain.split()
                )
                references = [
                    match["plain"].rstrip(",;.") for match in reference.finditer(plain) if match["plain"] is not None
                ]
                if (
                    ambiguous_quote
                    or ambiguous_name
                    or "](" in plain
                    or any(
                        (is_path(candidate) and re.search(r"[^\w./:\\-]", value))
                        or (
                            not actionable_summary
                            and not is_path(candidate)
                            and ("/" in candidate or "\\" in candidate)
                        )
                        for candidate in references
                    )
                ):
                    details.append((table[0][column], value))
                    cells.append(
                        f"Item {row_number}"
                        if column == 0 or (readable_breaks and table[0] == ["Owner", "Issue", "Action"] and column == 1)
                        else "See detail"
                    )
                    continue
                if (
                    table[0][column].casefold() in {"surface", "artifact", "path", "evidence"}
                    and whole_path(value)
                    and ("/" in value or "\\" in value)
                ):
                    destinations.append(value)
                    cells.append(value.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1])
                    continue
                parts: list[str] = []
                cursor = 0
                for match in link.finditer(value):
                    parts.append(shorten_plain(value[cursor : match.start()]))
                    destinations.append(match[2].removeprefix("<").removesuffix(">"))
                    parts.append(shorten_plain(match[1], record=False))
                    cursor = match.end()
                parts.append(shorten_plain(value[cursor:]))
                cells.append("".join(parts))
            item = (
                cells[2]
                if confidence_table and table[0] == ["Status", "Deduction", "Gap or limitation", "Closure evidence"]
                else (cells[1] if readable_breaks and table[0] == ["Owner", "Issue", "Action"] else cells[0])
            )
            for destination in destinations:
                pair = (item, destination)
                if pair not in resources:
                    resources.append(pair)
            resources.extend((f"{item} ({column})", value) for column, value in details)
            if row[-1].startswith("[R") and row[-1].endswith("]"):
                resource_items.setdefault(row[-1][1:-1], []).append(item)
            # Only the generated two-column snapshot pointer contains no work-row facts to retain.
            report_pointer = (
                table[0] == ["Field", "Value"]
                and len(row) == 2
                and row[0] == "Report"
                and (report_link := link.fullmatch(row[1])) is not None
                and report_link[1] == "Markdown report"
            )
            if not report_pointer:
                display.append(cells)
        rendered.extend(
            "| " + " | ".join(_table_cell(cell) for column, cell in enumerate(row) if column not in removed) + " |"
            for row in display
        )
        if resources:
            rendered.extend(("", *(f"- {item}: {destination}" for item, destination in resources)))
    return "\n".join(rendered) + "\n"


def render_handoff(payload: object, *, report_root: Path | None = None) -> str:
    """Render one validated handoff into exact final-response text."""
    handoff = validate_handoff(payload)
    if handoff["branch"] == "caller-contract":
        return handoff["caller_contract"]["output"]

    review_subject = None
    closed_review = handoff.get("presentation_version") == CLOSED_REVIEW_PRESENTATION
    if handoff.get("presentation_version") in {16, CLOSED_REVIEW_PRESENTATION}:
        if not closed_review:
            review_subject = _review_pr_subject(handoff)
        layout_version = (
            15  # versions 16 and 17 render through the version 15 layout, adding a PR subject or close reason
        )
        handoff = {**handoff, "presentation_version": layout_version}
    if handoff.get("presentation_version") in {8, 9, 10, 11, 12, 13, 14, 15}:
        previous = 7 if handoff["skill"] == "code-review" and handoff["branch"] == "assessed" else 3
        if handoff["skill"] == "code-remediate":
            previous = 6
        rendered = _externalize_table_paths(
            _render_v2_handoff(
                {**handoff, "presentation_version": previous},
                report_root=report_root,
                readable=handoff["presentation_version"] in {9, 10, 11, 12, 13, 14, 15},
                show_resolution=handoff["presentation_version"] in {10, 11, 12, 13, 14, 15},
                status_table=handoff["presentation_version"] in {11, 12, 13, 14, 15},
                closed_review=closed_review,
                actions_last=handoff["presentation_version"] in {12, 13, 14, 15},
                current_review_summary=handoff["presentation_version"] in {12, 13, 14, 15},
                field_tables=handoff["presentation_version"] in {13, 14, 15},
                confidence_table=handoff["presentation_version"] in {14, 15},
                actionable_summary=handoff["presentation_version"] == 15,
                owned_actions="action_policy" in handoff,
                review_subject=review_subject,
            ),
            readable_breaks=handoff["presentation_version"] in {13, 14, 15},
            keep_verification_table=handoff["presentation_version"] in {13, 14, 15},
            confidence_table=handoff["presentation_version"] in {14, 15},
            actionable_summary=handoff["presentation_version"] == 15,
        )
        if closed_review and handoff["close_reason"]["code"] == "DUPLICATE":
            # Prose keeps the links to both PRs clickable and follows them with the target's evidence of the overlap.
            item = _closed_review_view(handoff)["remaining"][0]
            rendered += f"\n**Next steps**\n\n**{item['owner']}** — {item['next_action']}\n\n{item['item']}\n"
        return rendered

    if handoff.get("presentation_version") in {2, 3, 4, 5, 6, 7}:
        return _render_v2_handoff(handoff, report_root=report_root)

    lines = ["**Outcome**", "", f"{handoff['outcome']['title']}: {handoff['outcome']['summary']}"]
    lines.extend(("", "**Results**"))
    if handoff["tables"]:
        for table in handoff["tables"]:
            lines.extend(("", *_render_table(table, report_root=report_root)))
    else:
        lines.extend(("", "No result table applies to this terminal branch."))

    lines.extend(("", "**Verification**", ""))
    lines.extend(f"- {entry['check']}: {entry['status']} — {entry['evidence']}" for entry in handoff["verification"])
    if disposition := handoff.get("commit_disposition"):
        lines.extend(
            ("", f"Commit: {disposition['status']} — {disposition['reason']} Evidence: {disposition['evidence']}")
        )
    lines.extend(("", "**Remaining**", ""))
    remaining_by_id = {item["row_id"]: item for item in handoff["remaining"]}
    if remaining_by_id:
        lines.extend(
            f"- {row_id} — {item['item']} — owner: {item['owner']} — next: {item['next_action']}"
            for row_id, item in remaining_by_id.items()
        )
    else:
        lines.append("None")

    lines.extend(("", "**Next steps**", ""))
    if handoff["next_steps"]:
        lines.extend(
            f"- {row_id} — {remaining_by_id[row_id]['owner']}: {remaining_by_id[row_id]['next_action']}"
            for row_id in handoff["next_steps"]
        )
    else:
        lines.append("None")

    confidence = handoff["confidence"]
    lines.extend(("", "**Confidence**", "", f"{confidence['score']:.2f} ({confidence['band']})."))
    if confidence["limits"]:
        lines.append("Limits: " + "; ".join(confidence["limits"]))
    for gap in confidence["gaps"]:
        detail = gap.get("evidence") or gap.get("rationale")
        lines.append(f"Gap [{gap['status']}]: {gap['gap']} — {detail}")

    lines.extend(("", "**Artifact**", ""))
    lines.extend(f"{artifact['label']}: {artifact['path']}" for artifact in handoff["artifacts"])
    return "\n".join(lines) + "\n"


def _render_confidence_table(confidence: dict[str, Any]) -> list[str]:
    """Display existing deductions and closure records without modifying their machine identities."""
    gaps = confidence["gaps"]
    labels = {gap["gap"] for gap in gaps}
    records = [("limit", limit, "—") for limit in dict.fromkeys(confidence["limits"]) if limit not in labels]
    records.extend(
        (
            gap["status"],
            gap["gap"],
            " — ".join(dict.fromkeys(gap[key] for key in ("rationale", "evidence") if gap.get(key))),
        )
        for gap in gaps
    )
    if not records:
        return []
    lines = ["", "| Status | Deduction | Gap or limitation | Closure evidence |", "| --- | --- | --- | --- |"]
    for status, label, detail in records:
        contribution = re.search(r"\(-0\.\d{2}\)", label) or re.search(r"\(-0\.\d{2}\)", detail)
        deduction = contribution[0] if contribution else "—"
        description = re.sub(r"^\(-0\.\d{2}\)\s*", "", label)
        lines.append("| " + " | ".join(_table_cell(cell) for cell in (status, deduction, description, detail)) + " |")
    return lines


def _render_v2_handoff(
    handoff: dict[str, Any],
    *,
    report_root: Path | None = None,
    readable: bool = False,
    show_resolution: bool = False,
    status_table: bool = False,
    closed_review: bool = False,
    actions_last: bool = False,
    current_review_summary: bool = False,
    field_tables: bool = False,
    confidence_table: bool = False,
    actionable_summary: bool = False,
    owned_actions: bool = False,
    review_subject: tuple[str, str] | None = None,
) -> str:
    """Render a plain-English-first report while retaining the validated machine fields."""
    outcome = handoff["outcome"]
    if handoff["skill"] == "code-review" and handoff["branch"] == "assessed":
        recommendation = outcome["summary"].removeprefix("Recommendation: ").removesuffix(".")
        plain_summary = (
            "The reviewed change needs more work before it can be accepted."
            if current_review_summary and recommendation in {"needs-more-work", "MAJOR_REVISION"}
            else REVIEW_PLAIN_SUMMARIES[recommendation]
        )
        technical_summary = None
    else:
        plain_summary, separator, technical_detail = outcome["summary"].partition(" Reason: ")
        technical_summary = f"Reason: {technical_detail}" if separator else None
    review_report = None
    if handoff["presentation_version"] == 7:
        if report_root is None:
            raise HandoffError("review-report-root-required")
        review_report = report_root.resolve() / "review-notes.md"
        if review_report.is_symlink() or not review_report.is_file():
            raise HandoffError("review-report-missing")
        try:
            report_text = review_report.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise HandoffError("review-report-unreadable") from error
        if not report_text.strip():
            raise HandoffError("review-report-empty")
        expected_details = render_review_report_details(handoff)
        if expected_details:
            sections = re.findall(r"(?ms)^## Finding Details[^\S\r\n]*\r?\n.*?(?=^## |\Z)", report_text)
            if len(sections) != 1 or sections[0].strip() != expected_details.strip():
                raise HandoffError("review-report-details-mismatch")
    # The authored judgment is the opening; a label-only title adds no decision for the reader.
    lines = [plain_summary] if actionable_summary else [plain_summary, "", "**Outcome**", "", outcome["title"]]
    if review_subject:
        subject, url = review_subject
        lines = [subject, "", f"{plain_summary} [View pull request]({url})"]
    table_actions: dict[str, dict[str, str]] = {}
    if technical_summary:
        lines.extend(("", technical_summary))
    if handoff["tables"]:
        lines.extend(("", "**Results**"))
        for table in handoff["tables"]:
            if review_subject and table["heading"] == "PR Snapshot":
                # Its source-bound identity and URL have moved to the opening; preserve the original machine rows.
                table = {**table, "rows": [row for row in table["rows"] if row["cells"][0] != "PR"]}
            if actionable_summary and "Next action" in table["columns"]:
                # Move authored recommendations, not inferred tasks. Canonical machine rows remain untouched.
                action_index = table["columns"].index("Next action")
                if not owned_actions:
                    for row in table["rows"]:
                        table_actions[row["id"]] = {
                            "owner": "—",
                            "item": row["cells"][0],
                            "next_action": row["cells"][action_index],
                        }
                table = {
                    **table,
                    "columns": [value for index, value in enumerate(table["columns"]) if index != action_index],
                    "rows": [
                        {**row, "cells": [value for index, value in enumerate(row["cells"]) if index != action_index]}
                        for row in table["rows"]
                    ],
                }
            lines.extend(
                (
                    "",
                    *_render_table(
                        table,
                        suppress_heading=table["heading"] == "Results",
                        show_selection=handoff["presentation_version"] in {5, 6},
                        show_resolution=show_resolution,
                        report_root=report_root,
                        review_report=review_report,
                    ),
                )
            )
    elif closed_review:
        # Pattern-validated identifiers render as recorded; free text comes from the normalized close view.
        reason = _closed_review_view(handoff)["close_reason"]
        url = reason["pr_url"]
        lines.extend(
            (
                "",
                "**Results**",
                "",
                "| Field | Value |",
                "| -- | -- |",
                "| Recommendation | Reject |",
                f"| Reason | {reason['code']} |",
                f"| Reviewed commit | `{reason['head_sha']}` |",
                # A close is terminal: it assesses no source finding, runs no detailed review and changes nothing.
                "| Source findings | Not assessed |",
                "| Detailed review | Skipped |",
                "| GitHub mutation | Not performed |",
                "",
                f"[PR #{url.rsplit('/', 1)[-1]}]({url}) — {reason['summary']}",
                "",
                reason["rationale"],
            )
        )
    elif status_table:
        # Terminal branches have no assessed records; derive a display-only anchor from their actual disposition.
        lines.extend(("", "**Results**", "", "| Field | Value |", "| -- | -- |"))
        lines.append(f"| Status | {handoff['branch']} |")
        if handoff["skill"] == "code-review":
            lines.extend(("| Source findings | Not assessed |", "| Merge decision | Not made |"))
            if handoff["branch"] == "closed":
                lines.append("| Detailed review | Skipped |")
    if runs := handoff.get("child_runs"):
        lines.extend(("", "Validated child and interaction scores:"))
        lines.extend(
            f"- {run['result_path']}: {' → '.join(map(str, run['scores'])) if run['scores'] else 'not-run'} "
            f"({run['decision']})"
            for run in runs
        )

    visible_checks = [entry for entry in handoff["verification"] if entry["status"] != "not-applicable"]
    lines.extend(("", "**Verification**", ""))
    if visible_checks:
        if field_tables:
            lines.extend(("| Check | Status | Evidence |", "| -- | -- | -- |"))
        for entry in visible_checks:
            evidence = entry["evidence"]
            if readable and report_root is not None:
                # Link complete existing destinations only; ordinary verification prose stays untouched.
                destination = report_root / evidence
                try:
                    existing_file = "\n" not in evidence and destination.is_file()
                except (OSError, ValueError):
                    existing_file = False
                if existing_file:
                    label = "Log" if field_tables else evidence
                    evidence = f"[{label}](<{destination.resolve().as_posix()}>)"
            if field_tables:
                lines.append(
                    "| "
                    + " | ".join(_table_cell(value) for value in (entry["check"], entry["status"], evidence))
                    + " |"
                )
            else:
                lines.append(f"- {entry['check']}: {entry['status']} — {evidence}")
    else:
        lines.append("- Checks were not run; no executable verification applies to this branch.")
    if disposition := handoff.get("commit_disposition"):
        lines.extend(
            ("", f"Commit: {disposition['status']} — {disposition['reason']} Evidence: {disposition['evidence']}")
        )

    remaining_by_id = {item["row_id"]: item for item in handoff["remaining"]}
    if owned_actions and handoff["branch"] != "closed":
        selected = {row_id: remaining_by_id[row_id] for row_id in handoff["next_steps"]}
        waiting = [item for row_id, item in remaining_by_id.items() if row_id not in selected]
        if waiting:
            lines.extend(("", "**Waiting items**", "", "| Owner | Issue |", "| -- | -- |"))
            lines.extend(f"| {_table_cell(item['owner'])} | {_table_cell(item['item'])} |" for item in waiting)
        remaining_by_id = selected
    elif actionable_summary:
        # Owned follow-up supersedes its display-only recommendation; unrelated recommendations remain visible.
        remaining_by_id = {**table_actions, **remaining_by_id}
    action_lines = [] if actions_last else lines
    if remaining_by_id and (not actions_last or handoff["branch"] != "closed"):
        action_lines.extend(("", "**Recommendations / next steps**" if actionable_summary else "**Next steps**", ""))
        if field_tables:
            action_lines.extend(("| Owner | Issue | Action |", "| -- | -- | -- |"))
            action_lines.extend(
                "| " + " | ".join(_table_cell(item[field]) for field in ("owner", "item", "next_action")) + " |"
                for item in remaining_by_id.values()
            )
        elif readable:
            for number, item in enumerate(remaining_by_id.values(), start=1):
                if number > 1:
                    action_lines.append("")
                indent = " " * (len(str(number)) + 2)
                owner, status, action = (
                    item[field].replace("\n", f"\n{indent}") for field in ("owner", "item", "next_action")
                )
                action_lines.extend(
                    (f"{number}. **Owner: {owner}**", "", f"{indent}Status: {status}", "", f"{indent}Action: {action}")
                )
        else:
            action_lines.extend(
                f"- {row_id} — {item['item']} — owner: {item['owner']} — next: {item['next_action']}"
                if handoff["presentation_version"] in {3, 4, 5, 6, 7}
                else f"- {item['item']} — owner: {item['owner']} — next: {item['next_action']}"
                for row_id, item in remaining_by_id.items()
            )

    confidence = handoff["confidence"]
    lines.extend(("", "**Confidence**", "", f"{confidence['score']:.2f} ({confidence['band']})."))
    if confidence_table:
        lines.extend(_render_confidence_table(confidence))
    else:
        compact_review = readable or handoff["presentation_version"] == 7
        if compact_review and (confidence["limits"] or confidence["gaps"]):
            lines.append("")
        if confidence["limits"]:
            if compact_review:
                limits = confidence["limits"]
                if readable:
                    # Exact matching joins repeated labels without hiding distinct limitations or gap closure details.
                    gap_labels = {gap["gap"] for gap in confidence["gaps"]}
                    limits = [limit for limit in dict.fromkeys(limits) if limit not in gap_labels]
                lines.extend("- Limit: " + (limit.replace("\n", "\n  ") if readable else limit) for limit in limits)
            else:
                lines.append("Limits: " + "; ".join(confidence["limits"]))
        for gap in confidence["gaps"]:
            detail = gap.get("evidence") or gap.get("rationale")
            if readable:
                detail = " — ".join(dict.fromkeys(gap[key] for key in ("rationale", "evidence") if gap.get(key)))
            prefix = "- " if compact_review else ""
            gap_text = f"Gap [{gap['status']}]: {gap['gap']} — {detail}"
            lines.append(prefix + (gap_text.replace("\n", "\n  ") if readable else gap_text))
    lines.extend(("", "**Artifact**", ""))
    lines.extend(
        f"- [{artifact['label']}](<{artifact['path']}>)"
        if readable or handoff["presentation_version"] in {5, 6}
        else f"{artifact['label']}: {artifact['path']}"
        for artifact in handoff["artifacts"]
    )
    if actionable_summary and not remaining_by_id and handoff["branch"] != "closed":
        action_lines.extend(("", "**Recommendations / next steps**", "", "No further action is required."))
    if actions_last:
        lines.extend(action_lines)
    return "\n".join(lines) + "\n"


def _sha256(payload: bytes) -> str:
    """Return a lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    """Load one non-symlink UTF-8 JSON object."""
    if path.is_symlink() or not path.is_file():
        raise HandoffError(f"input-not-regular-file:{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HandoffError(f"invalid-json:{path}:{error}") from error
    return _require_object(payload, f"json:{path.name}")


def _require_siblings(*paths: Path) -> None:
    """Keep input and output files distinct inside one resolved run directory."""
    resolved = [path.resolve() for path in paths]
    parents = {path.parent for path in resolved}
    if len(parents) != 1:
        raise HandoffError("handoff-paths-not-siblings")
    if len(set(resolved)) != len(paths) or any(path.is_symlink() for path in paths[1:]):
        raise HandoffError("handoff-paths-alias")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.exists() and other.exists() and path.samefile(other):
                raise HandoffError("handoff-paths-alias")


def render_files(handoff_path: Path, final_path: Path, validation_path: Path) -> dict[str, Any]:
    """Render and bind a new handoff, preserving the bytes of a previously validated historical presentation."""
    _require_siblings(handoff_path, final_path, validation_path)
    handoff = _load_json(handoff_path)
    default_policy = handoff.get("presentation_version") in {15, 16, 17} and "action_policy" not in handoff
    if default_policy:
        if validation_path.exists():
            return check_files(handoff_path, final_path, validation_path)
        # Alternate output filenames can still refer to a canonical, already bound input.
        canonical_proof = handoff_path.with_name("final-handoff.validation.json")
        if handoff_path.name == "final-handoff.json" and canonical_proof.exists():
            check_files(handoff_path, handoff_path.with_name("final.md"), canonical_proof)
            default_policy = False
        else:
            handoff["action_policy"] = "owned"
    validate_handoff(handoff)
    if handoff["skill"] == "code-remediate" and "commit_disposition" not in handoff:
        raise HandoffError("remediation-commit-disposition-missing")
    rendered = render_handoff(handoff, report_root=handoff_path.parent).encode("utf-8")
    if default_policy:
        # Replace the validated draft atomically so an interrupted write cannot truncate it or change a hard link.
        with tempfile.TemporaryDirectory(dir=handoff_path.parent) as staging:
            temporary = Path(staging) / handoff_path.name
            temporary.write_bytes((json.dumps(handoff, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            shutil.copymode(handoff_path, temporary)
            temporary.replace(handoff_path)
    final_path.write_bytes(rendered)
    validation = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "skill": handoff["skill"],
        "branch": handoff["branch"],
        "handoff_sha256": _sha256(handoff_path.read_bytes()),
        "rendered_sha256": _sha256(rendered),
    }
    validation_path.write_text(json.dumps(validation, indent=2) + "\n", encoding="utf-8", newline="\n")
    return validation


def check_files(handoff_path: Path, final_path: Path, validation_path: Path) -> dict[str, Any]:
    """Verify current sibling files against schema and recorded digests."""
    _require_siblings(handoff_path, final_path, validation_path)
    handoff = validate_handoff(_load_json(handoff_path))
    validation = _load_json(validation_path)
    expected_render = render_handoff(handoff, report_root=handoff_path.parent).encode("utf-8")
    if not final_path.is_file() or final_path.is_symlink() or final_path.read_bytes() != expected_render:
        raise HandoffError("rendered-final-mismatch")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "skill": handoff["skill"],
        "branch": handoff["branch"],
        "handoff_sha256": _sha256(handoff_path.read_bytes()),
        "rendered_sha256": _sha256(expected_render),
    }
    if validation != expected:
        raise HandoffError("handoff-validation-record-mismatch")
    return validation


def parse_args() -> argparse.Namespace:
    """Parse final handoff and pre-selection rendering/check contracts."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    render = subparsers.add_parser("render", help="Validate a handoff and write final output plus digests.")
    render.add_argument("--handoff", type=Path, required=True)
    render.add_argument("--out-final", type=Path, required=True)
    render.add_argument("--out-validation", type=Path, required=True)
    check = subparsers.add_parser("check", help="Verify existing handoff, final output, and digest record.")
    check.add_argument("--handoff", type=Path, required=True)
    check.add_argument("--final", type=Path, required=True)
    check.add_argument("--validation", type=Path, required=True)
    selection = subparsers.add_parser("selection", help="Validate the inventory before rendering or checking scope.")
    selection.add_argument("--input", type=Path, required=True, help="Canonical selection.json inventory.")
    selection.add_argument("--out-scope", type=Path, required=True)
    selection.add_argument("--check", action="store_true", help="Check exact existing scope bytes without writing.")
    return parser.parse_args()


def main() -> int:
    """Execute one final-handoff action with stable error output."""
    arguments = parse_args()
    try:
        if arguments.action == "render":
            result = render_files(arguments.handoff, arguments.out_final, arguments.out_validation)
        elif arguments.action == "selection":
            _require_siblings(arguments.input, arguments.out_scope)
            if arguments.out_scope.is_symlink() or arguments.input.resolve() == arguments.out_scope.resolve():
                raise HandoffError("selection-output-aliases-input-or-symlink")
            if (
                arguments.input.is_file()
                and arguments.out_scope.exists()
                and arguments.input.samefile(arguments.out_scope)
            ):
                raise HandoffError("selection-output-aliases-input-or-symlink")
            inventory = _load_json(arguments.input)
            rendered = render_selection(inventory).encode("utf-8")
            if arguments.check:
                if not arguments.out_scope.is_file() or arguments.out_scope.read_bytes() != rendered:
                    raise HandoffError("selection-render-mismatch")
            else:
                arguments.out_scope.write_bytes(rendered)
            result = {
                "status": "pass",
                "input_sha256": _sha256(arguments.input.read_bytes()),
                "rendered_sha256": _sha256(rendered),
            }
            if not arguments.check and "pr_relevance" in inventory:
                subject = _selection_pr_subject(arguments.input.parent)
                if subject:
                    result["pr_subject"] = subject
        else:
            result = check_files(arguments.handoff, arguments.final, arguments.validation)
    except HandoffError as error:
        print(f"final-handoff-error:{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
