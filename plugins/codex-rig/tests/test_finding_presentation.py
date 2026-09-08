"""Acceptance checks for canonical findings and readable selection handoffs."""

import copy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys

import pytest
from _platform import SYMLINKS_AVAILABLE
from rich.console import Console
from rich.markdown import Markdown

from test_final_handoff import FINALIZER, _handoff_payload, _load_finalizer
from test_code_remediate_final_outcome_validation import VALIDATOR
from test_code_remediate_final_outcome_validation import _metadata as resolution_metadata, _write_action_items
from test_final_handoff import _write_schema_v2_change_analysis
from test_review_finding_identity import _load_validator, _metadata, _result


def _selection() -> dict:
    """Return two differently indexed findings with complete source provenance.

    Example:
        >>> [item["input_item_id"] for item in _selection()["items"]]
        ['F7', 'F9']
    """
    return {
        "schema_version": 1,
        "selected_indexes": None,
        "items": [
            {
                "input_item_id": "F7",
                "item_name": "Preserve input compatibility",
                "item_type": "code",
                "severity": "high",
                "selectable": True,
                "summary": "Existing supported inputs must remain accepted.",
                "closure_evidence": "A focused compatibility regression passes.",
                "sources": [
                    {
                        "kind": "report",
                        "source_id": "result.json#F7",
                        "finding_id": "F7",
                        "location": "src/parser.py:12",
                        "body": "Compatibility regression.",
                        "evidence": "review-notes.md",
                        "related_mentions": ["review-notes.md:53", "review-notes.md:62"],
                    }
                ],
            },
            {
                "input_item_id": "F9",
                "item_name": "Cover the boundary",
                "item_type": "test",
                "severity": "medium",
                "selectable": True,
                "summary": "The empty case needs coverage.",
                "closure_evidence": "Empty and nonempty cases pass.",
                "sources": [
                    {
                        "kind": "online",
                        "source_id": "comment-9",
                        "location": "general",
                        "body": "Add the empty case.",
                        "evidence": "comments.json",
                    }
                ],
            },
        ],
    }


@pytest.mark.installed_plugin
def test_selection_cli_renders_named_groups_before_prompt(tmp_path: Path) -> None:
    """Keep selection indexes distinct from stable IDs and avoid premature deferral."""
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(_selection()), encoding="utf-8")
    output = tmp_path / "resolution-scope.md"
    completed = subprocess.run(
        [sys.executable, str(FINALIZER), "selection", "--input", str(path), "--out-scope", str(output)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = output.read_text(encoding="utf-8")
    assert "| # | Severity | Finding |" in rendered
    assert "| 1 | high | F7 — Preserve input compatibility |" in rendered
    assert "### 1 · F7 — Preserve input compatibility" in rendered
    assert "- Done when: A focused compatibility regression passes." in rendered
    assert "Awaiting selection" in rendered
    assert "Deferred by your selection" not in rendered
    assert "[S1]" not in rendered
    assert rendered.count("report [result.json#F7]") == 1
    assert "Related mentions: review-notes.md:53, review-notes.md:62" in rendered


@pytest.mark.parametrize("mutation", ["duplicate-source", "duplicate-finding", "bad-selection", "wrong-count"])
def test_selection_rejects_invalid_inventory(mutation: str) -> None:
    """Reject mismatched source/selection identities before a user can select work."""
    payload = _selection()
    if mutation == "duplicate-source":
        payload["items"][1]["sources"] = copy.deepcopy(payload["items"][0]["sources"])
    elif mutation == "duplicate-finding":
        source = copy.deepcopy(payload["items"][0]["sources"][0])
        source["source_id"] = "result.json#another-mention"
        payload["items"][1]["sources"] = [source]
    elif mutation == "bad-selection":
        payload["selected_indexes"] = [7]
    else:
        payload["source_records_total"] = 19
    with pytest.raises(ValueError):
        _load_finalizer().render_selection(payload)


def test_grouped_final_remediation_keeps_evidence_without_symbols() -> None:
    """Render the same bound machine cells as short rows and per-finding details."""
    payload = _handoff_payload()
    table = payload["tables"][0]
    table["layout"] = "grouped"
    table["rows"][0]["cells"][4] = "implemented — [O1]"
    table["rows"][0]["cells"][5] = "[E1] — owner/status: fixed"
    table["details"] = [{"id": "O1", "text": "Guard added."}, {"id": "E1", "text": "Tests pass."}]
    rendered = _load_finalizer().render_handoff(payload)
    assert "| ID | Severity | Finding | Outcome |" in rendered
    assert "- Outcome: implemented — Guard added." in rendered
    assert "- Evidence / next action: Tests pass. — owner/status: fixed" in rendered
    assert "[O1]" not in rendered
    assert "[E1]" not in rendered


def test_enriched_review_records_accept_titles_without_changing_identity() -> None:
    """Allow canonical descriptions while retaining legacy records and severity binding."""
    metadata = _metadata()
    _load_validator()._validate_review_decision(metadata, _result())
    record = metadata["review_findings"][0]
    record.update(
        title="Preserve input compatibility",
        summary="Supported inputs fail.",
        required_change="Restore the supported input path.",
        evidence=["src/parser.py:12"],
        closure_evidence="Compatibility regression passes.",
    )
    _load_validator()._validate_review_decision(metadata, _result())
    record["title"] = ""
    with pytest.raises(SystemExit, match="review-finding"):
        _load_validator()._validate_review_decision(metadata, _result())


@pytest.mark.parametrize(
    "selected",
    [
        pytest.param([], id="none"),
        pytest.param([1], id="first"),
        pytest.param([2], id="second"),
        pytest.param([1, 2], id="all"),
    ],
)
def test_selection_confirmation_binds_final_inventory(tmp_path: Path, selected: list[int]) -> None:
    """Bind the selected indexes and stable inventory to the final validation boundary."""
    payload = _selection()
    payload["selected_indexes"] = selected
    (tmp_path / "selection.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(payload).encode("utf-8"))
    metadata = {
        "resolution_scope": {
            "presentation_version": 2,
            "selection_source": "explicit-input",
            "prompt_presented": False,
            "selection_confirmed_by_user": True,
            "selected_indexes": selected,
            "selected_severity_groups": [],
            "deferred_indexes": [index for index in (1, 2) if index not in selected],
        },
        "final_resolution_table": {"items": copy.deepcopy(payload["items"])},
    }
    VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)
    metadata["final_resolution_table"]["items"][0]["item_type"] = "review-gate"
    with pytest.raises(SystemExit, match="selection-final-inventory-mismatch"):
        VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)
    metadata["final_resolution_table"]["items"][0]["item_type"] = payload["items"][0]["item_type"]
    metadata["final_resolution_table"]["items"][0]["sources"][0]["source_id"] = "result.json#substituted"
    with pytest.raises(SystemExit, match="selection-final-inventory-mismatch"):
        VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)


def test_pending_selection_cannot_pass_final_validation(tmp_path: Path) -> None:
    """A valid pending preview is not authorization to remediate."""
    payload = _selection()
    (tmp_path / "selection.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(payload).encode("utf-8"))
    metadata = {
        "resolution_scope": {
            "presentation_version": 2,
            "selection_source": "user-prompt",
            "prompt_presented": True,
            "selection_confirmed_by_user": False,
            "selected_indexes": [],
            "selected_severity_groups": [],
            "deferred_indexes": [],
        }
    }
    with pytest.raises(SystemExit, match="selection-not-confirmed"):
        VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)


def test_selection_check_detects_modified_display(tmp_path: Path) -> None:
    """A manually altered selection display must fail the read-only CLI check."""
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(_selection()), encoding="utf-8")
    output = tmp_path / "resolution-scope.md"
    output.write_text("Wrong finding", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(FINALIZER), "selection", "--input", str(path), "--out-scope", str(output), "--check"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "selection-render-mismatch" in completed.stderr
    assert output.read_text() == "Wrong finding"


@pytest.mark.parametrize("width", [80, 120])
def test_selection_preview_retains_names_and_references(width: int) -> None:
    """Prove short overview and separate evidence groups survive common terminal widths."""
    output = StringIO()
    console = Console(file=output, width=width, force_terminal=False, color_system=None)
    console.print(Markdown(_load_finalizer().render_selection(_selection())))
    text = output.getvalue()
    assert all(len(line) <= width for line in text.splitlines())
    assert "F7" in text and "Preserve input compatibility" in text
    assert "Done when:" in text and "result.json#F7" in text and "comment-9" in text
    assert "[S1]" not in text and "[C1]" not in text


def test_report_alias_mentions_share_one_canonical_owner() -> None:
    """Reject Markdown and JSON views of the same finding as separate sources."""
    payload = _selection()
    repeated = copy.deepcopy(payload["items"][0]["sources"][0])
    repeated["source_id"] = "review-notes.md:62"
    repeated["report_id"] = "result.json"
    payload["items"][1]["sources"] = [repeated]
    with pytest.raises(ValueError, match="selection-canonical-finding-duplicate"):
        _load_finalizer().render_selection(payload)


def test_distinct_report_files_can_reuse_finding_ids() -> None:
    """Finding IDs are scoped to a report, not every report in its directory."""
    payload = _selection()
    payload["items"][0]["sources"][0]["source_id"] = "first.json#F7"
    payload["items"][1]["sources"] = copy.deepcopy(payload["items"][0]["sources"])
    payload["items"][1]["sources"][0]["source_id"] = "second.json#F7"
    rendered = _load_finalizer().render_selection(payload)
    assert "report [first.json#F7]" in rendered
    assert "report [second.json#F7]" in rendered


def test_selection_cli_never_overwrites_its_input(tmp_path: Path) -> None:
    """A mistaken output path must not destroy the canonical selection inventory."""
    inventory = tmp_path / "selection.json"
    original = json.dumps(_selection()).encode("utf-8")
    inventory.write_bytes(original)
    completed = subprocess.run(
        [sys.executable, str(FINALIZER), "selection", "--input", str(inventory), "--out-scope", str(inventory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert inventory.read_bytes() == original


@pytest.mark.skipif(not SYMLINKS_AVAILABLE, reason="filesystem cannot create symlinks")
def test_selection_cli_rejects_output_symlinks(tmp_path: Path) -> None:
    """Do not follow an output link even when it stays within the run directory."""
    inventory = tmp_path / "selection.json"
    inventory.write_bytes(json.dumps(_selection()).encode("utf-8"))
    target = tmp_path / "unrelated.md"
    target.write_bytes(b"preserve unrelated evidence")
    output = tmp_path / "resolution-scope.md"
    output.symlink_to(target)
    completed = subprocess.run(
        [sys.executable, str(FINALIZER), "selection", "--input", str(inventory), "--out-scope", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert target.read_bytes() == b"preserve unrelated evidence"


@pytest.mark.parametrize("declared_count", [0, 2])
def test_grouped_intake_rejects_miscounted_gate_items(tmp_path: Path, declared_count: int) -> None:
    """A display-word bypass cannot hide or invent canonical report gate obligations."""
    payload = _selection()
    item = payload["items"][0]
    item["item_type"] = "review-gate"
    metadata = {
        "resolution_scope": {"presentation_version": 2},
        "final_resolution_table": {"items": [item]},
        "review_report_intake": {
            "requested_report": True,
            "report_items_total": 1,
            "review_gate_items_total": declared_count,
            "review_gate_items_selectable": declared_count,
            "report_items_marked_out_of_scope": 0,
        },
    }
    with pytest.raises(SystemExit, match="review-intake-inventory-mismatch"):
        VALIDATOR._validate_code_remediate_report_intake({"metadata": metadata}, tmp_path)


def test_new_canonical_marker_requires_complete_records() -> None:
    """Do not allow new producers to silently fall back to bare historical records."""
    metadata = _metadata()
    metadata["finding_records_version"] = 1
    with pytest.raises(SystemExit, match="review-finding-canonical-details-missing"):
        _load_validator()._validate_review_decision(metadata, _result())


def test_empty_inventory_never_claims_user_confirmation() -> None:
    """An empty work list is neither a pending prompt nor a user selection."""
    rendered = _load_finalizer().render_selection({"schema_version": 1, "selected_indexes": [], "items": []})
    assert "none-selectable" in rendered
    assert "Confirmed selected" not in rendered
    assert "Awaiting selection" not in rendered


def test_grouped_review_gate_intake_does_not_depend_on_display_words(tmp_path: Path) -> None:
    """Canonical inventory coverage must not require an incidental phrase in its title."""
    payload = _selection()
    payload["items"] = [payload["items"][0]]
    payload["items"][0].update(item_name="Independent verification", item_type="review-gate")
    payload["selected_indexes"] = [1]
    (tmp_path / "selection.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(payload).encode("utf-8"))
    (tmp_path / "action-items.md").write_text("## Review Report Intake\n\nOne review-gate item.\n", encoding="utf-8")
    metadata = {
        "resolution_scope": {
            "presentation_version": 2,
            "selection_source": "explicit-input",
            "prompt_presented": False,
            "selection_confirmed_by_user": True,
            "selected_indexes": [1],
            "deferred_indexes": [],
            "selected_severity_groups": [],
        },
        "final_resolution_table": {"items": copy.deepcopy(payload["items"])},
        "review_report_intake": {
            "requested_report": True,
            "report_items_total": 1,
            "review_gate_items_total": 1,
            "review_gate_items_selectable": 1,
            "report_items_marked_out_of_scope": 0,
        },
    }
    VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)
    VALIDATOR._validate_code_remediate_report_intake({"metadata": metadata}, tmp_path)


@pytest.mark.parametrize("item_type", ["code", "review-gate"])
def test_all_closed_selection_passes_complete_artifact_validation(tmp_path: Path, item_type: str) -> None:
    """Exercise the outer validator, not just the no-selectable renderer branch."""
    result_path = _write_schema_v2_change_analysis(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    metadata.update(resolution_metadata())
    table = metadata["final_resolution_table"]
    for item in table["items"]:
        item.update(
            item_type=item_type, selectable=False, triage_status="already-fixed", resolution_status="already-fixed"
        )
    for field in ("triage_status_counts", "resolution_status_counts"):
        table[field] = {status: 2 if status == "already-fixed" else 0 for status in table[field]}
    table.update(selectable_rows_total=0, nonselectable_rows_total=2)
    inventory = {"schema_version": 1, "selected_indexes": [], "items": copy.deepcopy(table["items"])}
    for item in inventory["items"]:
        item.update(summary="The original issue is already closed.", closure_evidence="Existing regression passes.")
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(inventory).encode("utf-8"))
    metadata.update(
        mode="report",
        resolution_scope={
            "presentation_version": 2,
            "selection_source": "none-selectable",
            "prompt_presented": False,
            "selection_confirmed_by_user": False,
            "selected_indexes": [],
            "deferred_indexes": [],
            "selected_severity_groups": [],
        },
        resolution_workplan={
            "groups_total": 0,
            "parent_owned_groups": 0,
            "specialist_owned_groups": 0,
            "verifier_groups": 0,
            "unassigned_selected_items": 0,
            "max_items_per_bucket": 5,
            "execution_mode": "parent-owned",
            "parallel_eligible": False,
            "parallel_approval_required": False,
            "parallel_prompt_presented": False,
            "parallel_approval_status": "not-required",
            "parallel_approval_source": "not-required",
            "parallel_approval_response": "not-required",
            "approved_plan_sha256": None,
            "work_buckets": [],
            "workplan_path": "resolution-workplan.md",
            "bucket_plan_path": "work-bucket-plan.json",
            "parallel_approval_path": "parallel-approval.json",
            "bucket_plan_sha256": "0" * 64,
        },
        review_report_intake={
            "requested_report": True,
            "report_items_total": 2,
            "review_gate_items_total": 2 if item_type == "review-gate" else 0,
            "review_gate_items_selectable": 0,
            "report_items_marked_out_of_scope": 0,
        },
        out_of_scope_confirmation={"count": 0, "all_confirmed_by_user": True, "items": []},
        pr_relevance={
            "evaluated": False,
            "connected_items_marked_out_of_scope": 0,
            "connected_open_items_total": 0,
            "connected_selectable_items_total": 0,
            "connected_required_followup_total": 0,
        },
        unresolved_summary={
            "selected_items_total": 0,
            "selected_items_resolved": 0,
            "selected_items_unresolved": 0,
            "local_actionable_items_unresolved": 0,
            "process_gate_items_unresolved": 0,
            "environment_blocked_items": 0,
            "external_owner_items": 0,
            "user_deferred_items": 0,
            "all_local_actionable_items_closed": True,
            "unresolved_reason_groups": [],
        },
    )
    _write_action_items(metadata, tmp_path)
    with (tmp_path / "action-items.md").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Review Report Intake\n\nTwo report items already closed.\n")
    (tmp_path / "closure-log.md").write_text("## Closure Evidence\n\nExisting tests pass.\n", encoding="utf-8")
    (tmp_path / "unresolved.txt").write_text("None\n", encoding="utf-8")
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff.update(skill="code-remediate", outcome={"title": "Remediation Summary", "summary": "Already closed."})
    rows, details, sources = [], [], []
    for position, item in enumerate(table["items"], 1):
        source_ids = [f"{source['kind']}:{source['source_id']}" for source in item["sources"]]
        sources.extend({"id": key, "evidence": source["evidence"]} for key, source in zip(source_ids, item["sources"]))
        rows.append(
            {
                "id": item["input_item_id"],
                "source_ids": source_ids,
                "cells": [
                    item["input_item_id"],
                    item["severity"],
                    item["item_name"],
                    "\n".join(f"{s['kind']} [{s['source_id']}]" for s in item["sources"]),
                    f"already-fixed — [O{position}]",
                    f"[E{position}] — owner/status: {item['owner_status']}",
                ],
            }
        )
        details.extend(
            [{"id": f"O{position}", "text": item["resolved_how"]}, {"id": f"E{position}", "text": item["evidence"]}]
        )
    handoff.update(
        tables=[
            {
                "heading": "Final Outcome Table",
                "layout": "grouped",
                "columns": ["Item", "Severity", "Finding", "Sources", "Outcome", "Evidence / next action"],
                "rows": rows,
                "details": details,
            }
        ],
        source_records=sources,
        source_coverage={
            "source_records_total": 3,
            "represented_source_records_total": 3,
            "omitted_source_records_total": 0,
        },
    )
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    binding = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    for field in ("handoff_sha256", "rendered_sha256"):
        metadata["final_handoff"][field] = binding[field]
    result_path.write_text(json.dumps(result), encoding="utf-8")

    VALIDATOR.validate("code-remediate", tmp_path, result_path)


@pytest.mark.parametrize("field", ["title", "summary", "closure_evidence", "action", "evidence"])
def test_grouped_review_binds_every_display_field(field: str) -> None:
    """Reject plausible but substituted content without weakening stable ID coverage."""
    metadata = _metadata()
    metadata.update(scope="working-tree", finding_records_version=1)
    for record in metadata["review_findings"]:
        record.update(
            title=f"Fix {record['id']}",
            summary="Supported inputs fail.",
            required_change="Restore the supported input path.",
            evidence=["src/parser.py:12"],
            closure_evidence="Compatibility regression passes.",
        )
    result = {**_result(), "metadata": metadata, "status": "fail"}
    payload = _handoff_payload()
    rows = []
    for record in metadata["review_findings"] + metadata["operational_blockers"]:
        row = {
            "id": record["id"],
            "title": record.get("title", record["id"]),
            "cells": [
                record["id"],
                record.get("required_change", "Run the gate."),
                "; ".join(record.get("evidence", ["gate.log"])),
                "Required",
            ],
            "source_ids": [record["id"]],
        }
        for detail in ("summary", "closure_evidence"):
            if detail in record:
                row[detail] = record[detail]
        rows.append(row)
    payload.update(
        skill="code-review",
        branch="assessed",
        outcome={"title": "Review Decision", "summary": "Recommendation: needs-more-work."},
        tables=[
            {
                "heading": "Review Findings and Merge Blocks",
                "layout": "grouped",
                "columns": ["Finding / area", "Required change", "Evidence", "Status"],
                "rows": rows,
            }
        ],
        source_records=[{"id": row["id"], "evidence": "review-notes.md"} for row in rows],
        source_coverage={
            "source_records_total": 3,
            "represented_source_records_total": 3,
            "omitted_source_records_total": 0,
        },
        remaining=[],
        next_steps=[],
    )
    _load_validator()._validate_review_decision(metadata, result)
    VALIDATOR._validate_code_review_final_handoff(result, payload)
    rendered = _load_finalizer().render_handoff(payload)
    assert "| ID | Finding | Status |" in rendered
    assert "- Done when: Compatibility regression passes." in rendered
    if field in {"action", "evidence"}:
        rows[0]["cells"][1 if field == "action" else 2] = "Substituted content"
    else:
        rows[0][field] = "Substituted content"
    with pytest.raises(SystemExit, match="code-review-final-handoff-finding-.*-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, payload)
