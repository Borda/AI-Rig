"""Regression checks for complete code-remediation outcome reconciliation."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TRIAGE_STATUSES = (
    "already-applied",
    "already-fixed",
    "duplicate",
    "needs-clarification",
    "out-of-scope",
    "resolved",
    "stale",
    "valid",
)
RESOLUTION_STATUSES = (
    "already-applied",
    "already-fixed",
    "duplicate",
    "implemented",
    "needs-clarification",
    "not-applicable",
    "rejected",
    "resolved",
    "stale",
    "unresolved",
)


def _load_validator() -> ModuleType:
    """Load the hyphenated artifact-validator module for focused contract tests."""
    path = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
    spec = importlib.util.spec_from_file_location("codex_rig_validate_final_outcomes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _status_counts(statuses: tuple[str, ...], observed: list[str]) -> dict[str, int]:
    """Return complete status counts required by the validator contract.

    Example:
        >>> _status_counts(("valid", "invalid"), ["valid", "valid"])
        {'valid': 2, 'invalid': 0}
    """
    return {status: observed.count(status) for status in statuses}


def _metadata() -> dict[str, object]:
    """Return a valid two-item final resolution ledger with different dispositions."""
    items = [
        {
            "input_item_id": "R1",
            "item_name": "Boundary guard",
            "item_type": "code",
            "severity": "high",
            "sources": [
                {
                    "kind": "report",
                    "source_id": ".reports/codex/investigate/run/root-cause.md:42",
                    "location": "src/guard.py:12",
                    "body": "The report says the boundary guard is missing.",
                    "evidence": "findings-input.txt",
                }
            ],
            "triage_status": "valid",
            "resolution_status": "implemented",
            "owner_status": "fixed",
            "resolved_how": "Added the missing guard.",
            "evidence": "tests/test_guard.py passed",
            "selectable": True,
        },
        {
            "input_item_id": "R2",
            "item_name": "Repeated comment",
            "item_type": "process",
            "severity": "medium",
            "sources": [
                {
                    "kind": "report",
                    "source_id": ".reports/codex/investigate/run/root-cause.md:57",
                    "location": "src/guard.py:12",
                    "body": "The report repeats the boundary guard finding.",
                    "evidence": "findings-input.txt",
                },
                {
                    "kind": "online",
                    "source_id": "thread-991/comment-27",
                    "location": "src/guard.py:12",
                    "body": "Please add the same boundary guard here.",
                    "evidence": "pr/unresolved-review-threads.json",
                },
            ],
            "triage_status": "duplicate",
            "resolution_status": "duplicate",
            "owner_status": "not-actionable",
            "resolved_how": "Same obligation as R1.",
            "evidence": "R1 closure evidence",
            "selectable": False,
        },
    ]
    return {
        "final_resolution_table": {
            "ingested_entries_total": 2,
            "grouped_items_total": 1,
            "items": items,
            "nonselectable_rows_total": 1,
            "omitted_entries_total": 0,
            "omitted_source_records_total": 0,
            "represented_source_records_total": 3,
            "required_columns": [
                "input item",
                "item name",
                "item type",
                "sources",
                "triage status",
                "resolution",
                "owner/status",
                "resolved how",
                "evidence",
            ],
            "resolution_status_counts": _status_counts(
                RESOLUTION_STATUSES, [item["resolution_status"] for item in items]
            ),
            "selectable_rows_total": 1,
            "source_records_total": 3,
            "table_rows_total": 2,
            "triage_status_counts": _status_counts(TRIAGE_STATUSES, [item["triage_status"] for item in items]),
        }
    }


def _write_action_items(metadata: dict[str, object], out_dir: Path) -> None:
    """Render the durable Markdown table from the machine-readable item ledger."""
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    rows = []
    expanded_sources = []
    table_details = []
    for position, item in enumerate(items, start=1):
        sources = " ".join("{kind} [{source_id}]".format(**source) for source in item["sources"])
        expanded_sources.extend(
            "- {kind} [{source_id}] @ {location} — {body} — {evidence}".format(**source) for source in item["sources"]
        )
        row_values = {**item, "sources": sources, "resolved_how": f"[O{position}]", "evidence": f"[E{position}]"}
        table_details.extend((f"[O{position}] {item['resolved_how']}", f"[E{position}] {item['evidence']}"))
        rows.append(
            "| {input_item_id} | {item_name} | {item_type} | {sources} | {triage_status} | "
            "{resolution_status} | {owner_status} | {resolved_how} | {evidence} |".format(**row_values)
        )
    rendered_rows = "\n".join(rows)
    rendered_table_details = "\n".join(table_details)
    rendered_sources = "\n".join(expanded_sources)
    (out_dir / "action-items.md").write_text(
        f"""# Action Items

## Review Item Resolution Table

| Input item | Item name | Item type | Sources | Triage status | Resolution | Owner/status | Resolved how | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{rendered_rows}

{rendered_table_details}

## Expanded Source Details

{rendered_sources}

## Final Resolution Summary

Ingested entries: 2. All selected local actionable items are closed.

## Final Resolution Table Completeness

- Ingested entries: 2
- Table rows: 2
- Omitted entries: 0
- Triage status counts: valid=1, duplicate=1
- Resolution status counts: implemented=1, duplicate=1
""",
        encoding="utf-8",
    )


def test_final_resolution_items_reconcile_with_durable_markdown(tmp_path: Path) -> None:
    """Accept a complete machine ledger rendered without loss into Markdown."""
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_v3_resolution_requires_a_reasoned_visible_disposition(tmp_path: Path) -> None:
    """Prevent a concise remediation result from rendering a bare unresolved state."""
    metadata = _metadata()
    metadata["resolution_scope"] = {"presentation_version": 3}
    _write_action_items(metadata, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-v3-resolution-disposition-invalid"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    items[0]["resolved_how"] = "Implemented: Added the missing guard."
    items[1]["resolved_how"] = "Duplicate: Same obligation as R1."
    _write_action_items(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_v3_needs_clarification_cannot_claim_no_code_change_closure(tmp_path: Path) -> None:
    """Keep an open clarification request visibly open even when gates pass."""
    metadata = _metadata()
    metadata["resolution_scope"] = {"presentation_version": 3}
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    items[0]["resolved_how"] = "Implemented: Added the missing guard."
    items[1].update(
        resolution_status="needs-clarification",
        owner_status="user-response-required",
        resolved_how="Verified without code changes: The report is incomplete.",
    )
    table["resolution_status_counts"] = _status_counts(
        RESOLUTION_STATUSES, [item["resolution_status"] for item in items]
    )
    _write_action_items(metadata, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-v3-resolution-disposition-invalid"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


@pytest.mark.parametrize("field", ["triage_status", "resolution_status"])
def test_v3_rejects_stale_review_status(tmp_path: Path, field: str) -> None:
    """Reject outdated-comment dismissal even when row and aggregate counts agree."""
    metadata = _metadata()
    metadata["resolution_scope"] = {"presentation_version": 3}
    table = metadata["final_resolution_table"]
    items = table["items"]
    items[0]["resolved_how"] = "Implemented: Added the missing guard."
    items[1][field] = "stale"
    label = "Stale" if field == "resolution_status" else "Duplicate"
    items[1]["resolved_how"] = f"{label}: Conflict resolution moved the commented lines."
    table["triage_status_counts"] = _status_counts(TRIAGE_STATUSES, [item["triage_status"] for item in items])
    table["resolution_status_counts"] = _status_counts(
        RESOLUTION_STATUSES, [item["resolution_status"] for item in items]
    )
    _write_action_items(metadata, tmp_path)
    with pytest.raises(SystemExit, match="code-remediate-v3-stale-status-forbidden"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


@pytest.mark.parametrize(
    ("status", "label"),
    [
        pytest.param("implemented", "Implemented", id="implemented"),
        pytest.param("resolved", "Verified without code changes", id="resolved"),
        pytest.param("already-fixed", "Verified without code changes", id="already-fixed"),
        pytest.param("already-applied", "Verified without code changes", id="already-applied"),
        pytest.param("rejected", "Rejected", id="rejected"),
        pytest.param("not-applicable", "Not applicable", id="not-applicable"),
        pytest.param("duplicate", "Duplicate", id="duplicate"),
    ],
)
def test_v3_preserves_other_review_dispositions(tmp_path: Path, status: str, label: str) -> None:
    """Keep the user's existing outcome choices while removing only stale."""
    metadata = _metadata()
    metadata["resolution_scope"] = {"presentation_version": 3}
    table = metadata["final_resolution_table"]
    items = table["items"]
    items[0]["resolved_how"] = "Implemented: Added the missing guard."
    items[1]["resolution_status"] = status
    items[1]["resolved_how"] = f"{label}: Original concern checked against current code and regression."
    table["resolution_status_counts"] = _status_counts(
        RESOLUTION_STATUSES, [item["resolution_status"] for item in items]
    )
    _write_action_items(metadata, tmp_path)
    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_final_resolution_machine_items_are_required(tmp_path: Path) -> None:
    """Reject aggregate counts that cannot prove the disposition of each input item."""
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    table.pop("items")

    with pytest.raises(SystemExit, match="code-remediate-final-table-items-not-list"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_duplicate_machine_item_ids_fail_closed(tmp_path: Path) -> None:
    """Reject ledgers that account for one ingested item twice and omit another."""
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    items[1]["input_item_id"] = "R1"

    with pytest.raises(SystemExit, match="code-remediate-final-table-item-id-duplicate"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_markdown_row_omission_fails_even_when_aggregate_counts_match(tmp_path: Path) -> None:
    """Reject a durable table that silently drops one machine-accounted item."""
    metadata = _metadata()
    rendered = copy.deepcopy(metadata)
    table = rendered["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    items.pop()
    _write_action_items(rendered, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-final-table-markdown-row-count-mismatch"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        pytest.param(
            "| valid | implemented |",
            "| valid | unresolved |",
            "code-remediate-final-table-markdown-resolution_status-mismatch",
            id="disposition-differs-from-machine-item",
        ),
        pytest.param(
            "Please add the same boundary guard here.",
            "online duplicate",
            "code-remediate-final-table-expanded-source-detail-missing",
            id="grouped-source-detail-incomplete",
        ),
        pytest.param(
            "[O1] Added the missing guard.",
            "[O1]",
            "code-remediate-final-table-symbol-detail-missing:O1",
            id="compact-outcome-detail-absent",
        ),
        pytest.param(
            "| Resolved how | Evidence |",
            "| Resolved how | Closure evidence or unresolved rationale |",
            "code-remediate-final-table-markdown-columns-missing:evidence",
            id="required-column-renamed",
        ),
        pytest.param(
            "[E1] tests/test_guard.py passed",
            "[E1] `tests/test_guard.py` skipped",
            "code-remediate-final-table-symbol-detail-missing:E1",
            id="detail-wording-altered-beyond-code-markup",
        ),
    ],
)
def test_durable_table_rejects_tampered_markdown(tmp_path: Path, old: str, new: str, error: str) -> None:
    """Reject a durable action-items table that no longer matches its validated machine ledger.

    One fragment of the rendered table is edited after a valid ledger is written. A user-facing disposition must
    match the machine item, a compact grouped row needs every source detail in the expanded ledger, a compact
    outcome needs its complete text below the table, a required column renamed away from its contract name is
    reported as missing, and a detail line whose wording differs from the ledger is rejected even when the code
    markup alone would be accepted.
    """
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)
    path = tmp_path / "action-items.md"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_durable_source_cells_use_only_compact_references(tmp_path: Path) -> None:
    """Keep source provenance readable while expanded records retain full detail."""
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)

    headers, rows = VALIDATOR._parse_markdown_table(tmp_path / "action-items.md", "Review Item Resolution Table")
    source_index = headers.index("sources")

    assert rows[0][source_index] == "report [.reports/codex/investigate/run/root-cause.md:42]"
    assert (
        rows[1][source_index]
        == "report [.reports/codex/investigate/run/root-cause.md:57] online [thread-991/comment-27]"
    )
    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


@pytest.mark.parametrize(
    ("item", "source", "field", "value", "error"),
    [
        pytest.param(
            0, 0, "source_id", "R1", "code-remediate-final-table-report-source-id-invalid", id="report-id-unresolvable"
        ),
        pytest.param(
            1,
            1,
            "source_id",
            "https://github.com/example/repo/pull/1#discussion_r27",
            "code-remediate-final-table-online-source-id-invalid",
            id="online-id-is-a-url",
        ),
        pytest.param(
            0,
            0,
            "kind",
            "pr-thread",
            "code-remediate-final-table-source-kind-invalid",
            id="source-kind-not-report-or-online",
        ),
    ],
)
def test_source_provenance_rejects_invalid_pointer_or_kind(
    tmp_path: Path, item: int, source: int, field: str, value: str, error: str
) -> None:
    """Reject provenance a reader could not follow, keeping the vocabulary to report and online.

    A report ID must locate the originating finding rather than name a bare item, an online review ID must be a
    stable identifier rather than a link, and the source category must be either report or online.
    """
    metadata = _metadata()
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    items[item]["sources"][source][field] = value
    _write_action_items(metadata, tmp_path)

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_report_source_accepts_a_json_item_pointer(tmp_path: Path) -> None:
    """Accept a report JSON path paired with its stable finding ID."""
    metadata = _metadata()
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    items[0]["sources"][0]["source_id"] = ".reports/codex/review/result.json#investigate:H1"
    _write_action_items(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


#: Resolution scope selection heading, counters, and table header shared by every scope-selection fixture.
_SCOPE_PREAMBLE = """## Resolution Scope Selection

selectable: 1
selected: 1
deferred: 0

| Index | Severity | Item id or source location | Source | Summary | Expected closure evidence |
| --- | --- | --- | --- | --- | --- |
"""


def _scope_selection_metadata() -> dict[str, object]:
    """Return an explicit-input confirmed selection of the first row."""
    return {
        "resolution_scope": {
            "selection_source": "explicit-input",
            "prompt_presented": False,
            "selection_confirmed_by_user": True,
            "selected_indexes": [1],
            "deferred_indexes": [],
            "selected_severity_groups": [],
        }
    }


def test_scope_selection_accepts_compact_grouped_source_references(tmp_path: Path) -> None:
    """Accept report and online IDs without source bodies or URLs in scope selection."""
    (tmp_path / "resolution-scope.md").write_text(
        _SCOPE_PREAMBLE
        + "| 1 | high | R2 | report [.reports/codex/investigate/run/root-cause.md:57] online [thread-991/comment-27] "
        "| [S1] | [C1] |\n\n[S1] Add guard at the request boundary.\n[C1] Focused guard test passes.\n",
        encoding="utf-8",
    )

    VALIDATOR._validate_code_remediate_scope_selection(_scope_selection_metadata(), tmp_path)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        pytest.param(
            "| 1 | high | R2 | report [.reports/codex/investigate/run/root-cause.md:57]<br>online "
            "[thread-991/comment-27] | [S1] | [C1] |\n\n[S1] Add guard at the request boundary.\n"
            "[C1] Focused guard test passes.\n",
            "code-remediate-scope-source-not-compact",
            id="terminal-visible-html-separator",
        ),
        pytest.param(
            "| 1 | high | R1 | report [.reports/codex/investigate/run/root-cause.md:42] @ metadata.review_decision "
            "— full body — findings-input.txt | Add guard | Focused test passes |\n",
            "code-remediate-scope-source-not-compact",
            id="expanded-source-content",
        ),
        pytest.param(
            "| 1 | high | R1 | report [.reports/codex/investigate/run/root-cause.md:42] "
            "| Add the missing request-boundary guard | Focused test passes |\n",
            "code-remediate-scope-detail-reference-invalid",
            id="long-text-inside-table",
        ),
    ],
)
def test_scope_selection_rejects_noisy_or_unreferenced_rows(tmp_path: Path, body: str, error: str) -> None:
    """Reject a scope-selection table that would print noise or hide detail before a prompt is shown.

    Grouped source pointers must not print a literal HTML tag, the noisy source-body format must not appear, and
    summary and closure detail must be symbols defined below the table rather than long text inside it.
    """
    (tmp_path / "resolution-scope.md").write_text(_SCOPE_PREAMBLE + body, encoding="utf-8")

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_scope_selection(_scope_selection_metadata(), tmp_path)


def test_final_handoff_cells_are_value_bound_to_resolution_items() -> None:
    """Reject a complete-looking final table that changes what or how an item resolved."""
    metadata = _metadata()
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    items = table["items"]
    assert isinstance(items, list)
    rows = []
    details = []
    source_records = []
    for position, item in enumerate(items, start=1):
        source_ids = [f"{source['kind']}:{source['source_id']}" for source in item["sources"]]
        rendered_sources = [f"{source['kind']} [{source['source_id']}]" for source in item["sources"]]
        rows.append(
            {
                "id": item["input_item_id"],
                "cells": [
                    item["input_item_id"],
                    item["severity"],
                    item["item_name"],
                    "\n".join(rendered_sources),
                    f"{item['resolution_status']} — [O{position}]",
                    f"[E{position}] — owner/status: {item['owner_status']}",
                ],
                "source_ids": source_ids,
            }
        )
        details.extend(
            (
                {"id": f"O{position}", "text": item["resolved_how"]},
                {"id": f"E{position}", "text": item["evidence"]},
            )
        )
        source_records.extend(
            {"id": source_id, "evidence": source["evidence"]}
            for source_id, source in zip(source_ids, item["sources"], strict=True)
        )
    handoff = {"tables": [{"rows": rows, "details": details}], "source_records": source_records}
    result = {"metadata": metadata}

    VALIDATOR._validate_code_remediate_final_handoff(result, handoff)

    metadata["resolution_scope"] = {"presentation_version": 3}
    handoff["tables"][0]["layout"] = "concise"
    with pytest.raises(SystemExit, match="code-remediate-final-handoff-overview-only-required"):
        VALIDATOR._validate_code_remediate_final_handoff(result, handoff)
    handoff["tables"][0]["overview_only"] = True
    VALIDATOR._validate_code_remediate_final_handoff(result, handoff)

    rows[0]["cells"][4] = "implemented — details omitted"
    with pytest.raises(SystemExit, match="code-remediate-final-handoff-row-coverage-mismatch"):
        VALIDATOR._validate_code_remediate_final_handoff(result, handoff)


@pytest.mark.parametrize(
    "validator",
    [VALIDATOR._validate_code_remediate_final_handoff, VALIDATOR._validate_code_review_final_handoff],
)
def test_caller_contract_bypasses_workflow_owned_table_layout(validator: object) -> None:
    """Let an explicit caller output contract replace workflow-owned final tables."""
    validator({"metadata": {}}, {"branch": "caller-contract"})


def test_source_record_counts_fail_closed(tmp_path: Path) -> None:
    """Reject aggregate source counts that could conceal an omitted grouped comment."""
    metadata = _metadata()
    table = metadata["final_resolution_table"]
    assert isinstance(table, dict)
    table["represented_source_records_total"] = 2
    _write_action_items(metadata, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-final-table-source-count-mismatch"):
        VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def _widen_resolution_table(out_dir: Path, columns: tuple[str, ...]) -> None:
    """Append extra workflow columns to every row of the durable table."""
    path = out_dir / "action-items.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Review Item Resolution Table") + 2
    separators = tuple("---" for _ in columns)
    placeholders = tuple(f"detail for {column}" for column in columns)
    for offset, line in enumerate(lines[start:]):
        if not line.startswith("|"):
            break
        cells = (columns, separators)[offset] if offset < 2 else placeholders
        lines[start + offset] = line + "".join(f" {cell} |" for cell in cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_durable_table_accepts_workflow_columns_beyond_the_required_set(tmp_path: Path) -> None:
    """Accept the wider column list the workflow mandates, since required columns are a minimum."""
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)
    _widen_resolution_table(
        tmp_path,
        ("selection index", "item id or source location", "source category", "PR/diff relation", "severity", "summary"),
    )

    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


def test_symbol_detail_accepts_code_spans_around_ledger_text(tmp_path: Path) -> None:
    """Accept a detail line that marks paths and commands as code without altering the ledger text."""
    metadata = _metadata()
    _write_action_items(metadata, tmp_path)
    path = tmp_path / "action-items.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[E1] tests/test_guard.py passed",
            "[E1] `tests/test_guard.py` passed",
            1,
        ),
        encoding="utf-8",
    )

    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)
