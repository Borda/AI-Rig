#!/usr/bin/env python3
"""Validate and render deterministic Codex Rig final handoffs.

## Purpose

Turn one versioned, machine-readable workflow handoff into the exact Markdown or caller-controlled bytes intended for
the final user response, closing structural omissions before result promotion.

## Scope

Validate local handoff JSON, render sibling ``final.md`` output, and bind both files with SHA-256 evidence. The helper
does not decide workflow findings, execute project gates, inspect chat transcripts, or send a response.

## Usage

New workflow handoffs set ``presentation_version=2`` before ``render``; omitted presentation version remains available
only for validating historical v1 artifacts. Run ``render`` after gates and handoff creation, then run ``check``
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

All thirteen artifact-producing Codex Rig workflow skills use this helper as their post-gate presentation checkpoint;
the shared artifact validator verifies its evidence for schema-v2 workflow results. The agent-shim manager is
deliberately outside the artifact lifecycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
GATE_IDS = ("lint", "format", "types", "tests", "review")
GATE_STATUSES = {"pass", "fail", "missing-command", "not-applicable", "timeout"}
GAP_STATUSES = {"closed", "unresolved", "deferred"}
STANDARD_COLUMNS = {
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
    "release": ("Change", "SemVer impact", "Status / blocker", "Evidence"),
    "research": ("Recommendation", "Evidence", "Decision", "Caveat / next check"),
    "sync": ("Surface", "Outcome", "Verification", "Remaining limit"),
}
SUPPORTED_SKILLS = frozenset((*STANDARD_COLUMNS, "code-review"))
REVIEW_RECOMMENDATIONS = frozenset({"accept-as-is", "minor-changes", "needs-more-work", "reject", "not-aligned"})
REVIEW_TABLE_COLUMNS = {
    "PR Snapshot": ("Field", "Value"),
    "Review Findings and Merge Blocks": ("Finding / area", "Required change", "Evidence", "Status"),
}
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
PRESENTATION_VERSION = 2
REVIEW_PLAIN_SUMMARIES = {
    "accept-as-is": "This review found no blocking change requests.",
    "minor-changes": "This review can proceed after minor changes.",
    "needs-more-work": "This review needs more work before it can be accepted.",
    "reject": "This review recommends rejecting the proposed change.",
    "not-aligned": "This review is not aligned with the requested goal.",
}


class HandoffError(ValueError):
    """Report one fail-closed handoff contract violation."""


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


def _validate_tables(payload: dict[str, Any], skill: str, branch: str) -> tuple[set[str], set[str]]:
    """Validate tables and return represented row and source identifiers."""
    tables = payload.get("tables")
    if not isinstance(tables, list):
        raise HandoffError("tables-not-list")
    if branch in {"unavailable", "closed", "caller-contract"}:
        if tables:
            raise HandoffError("terminal-branch-forbids-tables")
        return set(), set()
    if skill != "code-review" and len(tables) != 1:
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
            or layout not in {"legacy", "grouped", "concise"}
            or (layout != "legacy" and (skill not in {"code-review", "code-remediate"} or heading == "PR Snapshot"))
        ):
            raise HandoffError(f"table-layout-invalid:{heading}")
        expected = REVIEW_TABLE_COLUMNS.get(heading) if skill == "code-review" else STANDARD_COLUMNS[skill]
        if expected is None or tuple(columns) != expected:
            raise HandoffError(f"table-columns-mismatch:{skill}:{heading}")
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            raise HandoffError(f"table-rows-empty:{heading}")
        table_cells: list[str] = []
        for row_index, raw_row in enumerate(rows):
            row = _require_object(raw_row, f"table-row:{heading}:{row_index}")
            row_id = _require_string(row.get("id"), f"table-row-id:{heading}:{row_index}")
            if row_id in row_ids:
                raise HandoffError(f"table-row-id-duplicate:{row_id}")
            row_ids.add(row_id)
            cells = _require_string_list(row.get("cells"), f"table-row-cells:{row_id}", allow_empty=False)
            if len(cells) != len(columns):
                raise HandoffError(f"table-row-width-mismatch:{row_id}")
            if layout in {"grouped", "concise"} and skill == "code-review":
                _require_string(row.get("title"), f"table-row-title:{row_id}")
                for field in ("summary", "closure_evidence"):
                    if field in row:
                        _require_string(row[field], f"table-row-{field}:{row_id}")
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
    return row_ids, source_ids


def _validate_source_coverage(payload: dict[str, Any], branch: str, represented_source_ids: set[str]) -> set[str]:
    """Require declared source records to match table representation exactly."""
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

    coverage = _require_object(payload.get("source_coverage"), "source-coverage")
    expected_counts = {
        "source_records_total": len(source_ids),
        "represented_source_records_total": len(represented_source_ids),
        "omitted_source_records_total": len(source_ids - represented_source_ids),
    }
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


def _validate_remaining(payload: dict[str, Any], row_ids: set[str], branch: str) -> None:
    """Require every remaining item to carry a unique owner and next action."""
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
        if row_ids and row_id not in row_ids:
            raise HandoffError(f"remaining-row-id-unknown:{row_id}")
        remaining_ids.add(row_id)
    next_steps = set(_require_string_list(payload.get("next_steps"), "next-steps"))
    if branch == "closed" and next_steps:
        raise HandoffError("closed-branch-next-steps-forbidden")
    if branch != "closed" and next_steps != remaining_ids:
        raise HandoffError("next-steps-remaining-mismatch")


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


def validate_handoff(payload: object) -> dict[str, Any]:
    """Validate and return one canonical final-handoff payload."""
    handoff = _require_object(payload, "handoff")
    presentation_version = handoff.get("presentation_version")
    expected_fields = HANDOFF_FIELDS if presentation_version is None else HANDOFF_FIELDS | {"presentation_version"}
    if set(handoff) != expected_fields:
        raise HandoffError("handoff-fields-mismatch")
    if presentation_version is not None and (
        type(presentation_version) is not int or presentation_version != PRESENTATION_VERSION
    ):
        raise HandoffError("handoff-presentation-version-invalid")
    if handoff.get("schema_version") != SCHEMA_VERSION:
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
            f"Recommendation: {recommendation}." for recommendation in REVIEW_RECOMMENDATIONS
        }:
            raise HandoffError("review-outcome-not-canonical")
    row_ids, represented_sources = _validate_tables(handoff, skill, branch)
    _validate_source_coverage(handoff, branch, represented_sources)
    _validate_verification(handoff)
    _validate_remaining(handoff, row_ids, branch)
    _validate_confidence(handoff)
    _validate_artifacts(handoff)
    _validate_caller_contract(handoff, branch)
    return handoff


def _table_cell(value: str) -> str:
    """Escape one Markdown table cell without truncating its content."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _render_table(table: dict[str, Any]) -> list[str]:
    """Render one validated table and its symbol details deterministically."""
    if table.get("layout") in {"grouped", "concise"}:
        return _render_grouped_table(table)
    columns = table["columns"]
    lines = [f"**{table['heading']}**", "", "| " + " | ".join(columns) + " |"]
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in table["rows"]:
        lines.append("| " + " | ".join(_table_cell(cell) for cell in row["cells"]) + " |")
    if table.get("details"):
        lines.append("")
        lines.extend(
            f"[{detail['id']}] {detail['text'].replace(chr(13) + chr(10), '<br>').replace(chr(10), '<br>')}"
            for detail in table["details"]
        )
    return lines


def _render_grouped_table(table: dict[str, Any]) -> list[str]:
    """Render bound rows, retaining historical bytes and separating concise facts from evidence."""
    remediation = tuple(table["columns"]) == STANDARD_COLUMNS["code-remediate"]
    concise = table.get("layout") == "concise"
    columns = ["ID", "Severity", "Finding", "Outcome"] if remediation else ["ID", "Finding", "Status"]
    if concise:
        columns.insert(-1, "Resolution" if remediation else "Resolution proposal")
    lines = [
        f"**{table['heading']}**",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
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
            overview.insert(-1, resolution)
        lines.append("| " + " | ".join(_table_cell(value) for value in overview) + " |")
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


def _validate_selection(payload: object) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """Validate source ownership, counts and choice before rendering any selection context.

    Null selected_indexes means awaiting input; a list records the confirmed choice. All items, including closed
    nonselectable entries, participate in identity and source counts. Related mentions never count as new sources.
    """
    inventory = _require_object(payload, "selection")
    presentation = inventory.get("presentation_version", 2)
    if type(presentation) is not int or presentation not in {2, 3}:
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
            if presentation == 3:
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
                kind not in {"report", "online"}
                or source_id != source_id.strip()
                or re.search(r"[\r\n\[\]]", source_id)
            ):
                raise HandoffError("selection-source-invalid")
            if (kind, source_id) in sources:
                raise HandoffError("selection-source-duplicate")
            if kind == "online" and re.fullmatch(r"(?!https?://)\S+", source_id, re.IGNORECASE) is None:
                raise HandoffError("selection-online-source-invalid")
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
                    report_id = source.get("report_id", PurePosixPath(match[1].replace("\\", "/")).as_posix())
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


def render_selection(payload: object) -> str:
    """Render a validated inventory as a short overview with named evidence groups."""
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
            if inventory.get("presentation_version") == 3
            else "| # | Severity | Finding |",
            "| --- | --- | --- | --- | --- |" if inventory.get("presentation_version") == 3 else "| --- | --- | --- |",
        )
    )
    for index, item in enumerate(selectable, 1):
        extra = ""
        if inventory.get("presentation_version") == 3:
            # Aggregate only genuine sources; related mentions are not independent evidence.
            kinds = [source["kind"] for source in item["sources"]]
            tags = "; ".join(f"{kind} ×{kinds.count(kind)}" for kind in dict.fromkeys(kinds))
            extra = f" {_table_cell(item['resolution_proposal'])} | {tags} |"
        lines.append(
            f"| {index} | {item['severity']} | {_table_cell(item['input_item_id'])} — {_table_cell(item['item_name'])} |{extra}"
        )
    for index, item in enumerate(selectable, 1):
        concise = inventory.get("presentation_version") == 3
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


def render_handoff(payload: object) -> str:
    """Render one validated handoff into exact final-response text."""
    handoff = validate_handoff(payload)
    if handoff["branch"] == "caller-contract":
        return handoff["caller_contract"]["output"]

    if handoff.get("presentation_version") == PRESENTATION_VERSION:
        return _render_v2_handoff(handoff)

    lines = ["**Outcome**", "", f"{handoff['outcome']['title']}: {handoff['outcome']['summary']}"]
    lines.extend(("", "**Results**"))
    if handoff["tables"]:
        for table in handoff["tables"]:
            lines.extend(("", *_render_table(table)))
    else:
        lines.extend(("", "No result table applies to this terminal branch."))

    lines.extend(("", "**Verification**", ""))
    lines.extend(f"- {entry['check']}: {entry['status']} — {entry['evidence']}" for entry in handoff["verification"])
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


def _render_v2_handoff(handoff: dict[str, Any]) -> str:
    """Render a plain-English-first report while retaining the validated machine fields."""
    outcome = handoff["outcome"]
    if handoff["skill"] == "code-review" and handoff["branch"] == "assessed":
        recommendation = outcome["summary"].removeprefix("Recommendation: ").removesuffix(".")
        plain_summary = REVIEW_PLAIN_SUMMARIES[recommendation]
        technical_summary = None
    else:
        plain_summary, separator, technical_detail = outcome["summary"].partition(" Reason: ")
        technical_summary = f"Reason: {technical_detail}" if separator else None
    lines = [plain_summary, "", "**Outcome**", "", outcome["title"]]
    if technical_summary:
        lines.extend(("", technical_summary))
    if handoff["tables"]:
        lines.extend(("", "**Results**"))
        for table in handoff["tables"]:
            lines.extend(("", *_render_table(table)))

    visible_checks = [entry for entry in handoff["verification"] if entry["status"] != "not-applicable"]
    lines.extend(("", "**Verification**", ""))
    if visible_checks:
        lines.extend(f"- {entry['check']}: {entry['status']} — {entry['evidence']}" for entry in visible_checks)
    else:
        lines.append("- Checks were not run; no executable verification applies to this branch.")

    remaining_by_id = {item["row_id"]: item for item in handoff["remaining"]}
    if remaining_by_id:
        lines.extend(("", "**Next steps**", ""))
        lines.extend(
            f"- {item['item']} — owner: {item['owner']} — next: {item['next_action']}"
            for item in remaining_by_id.values()
        )

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
    """Keep generated and checked files inside one resolved run directory."""
    parents = {path.resolve().parent for path in paths}
    if len(parents) != 1:
        raise HandoffError("handoff-paths-not-siblings")


def render_files(handoff_path: Path, final_path: Path, validation_path: Path) -> dict[str, Any]:
    """Render sibling files and return their digest-bound validation record."""
    _require_siblings(handoff_path, final_path, validation_path)
    handoff = validate_handoff(_load_json(handoff_path))
    rendered = render_handoff(handoff).encode("utf-8")
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
    expected_render = render_handoff(handoff).encode("utf-8")
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
            rendered = render_selection(_load_json(arguments.input)).encode("utf-8")
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
        else:
            result = check_files(arguments.handoff, arguments.final, arguments.validation)
    except HandoffError as error:
        print(f"final-handoff-error:{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
