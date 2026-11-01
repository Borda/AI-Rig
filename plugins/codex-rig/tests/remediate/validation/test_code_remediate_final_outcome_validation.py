"""Regression checks for complete code-remediation outcome reconciliation.

The artifact validator must accept a final resolution ledger only when its machine-readable items, its durable
``action-items.md`` rendering and its terminal handoff all describe the same dispositions, sources and evidence.
Each test starts from one valid two-item ledger and breaks or varies exactly one aspect of it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
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
V3_SCOPE = {"presentation_version": 3}
IMPLEMENTED_HOW = "Implemented: Added the missing guard."
TABLE_ERROR = "code-remediate-final-table-"


def _load_validator() -> ModuleType:
    """Load the hyphenated artifact-validator module for focused contract tests."""
    return load_module("codex_rig_validate_final_outcomes", PLUGIN_ROOT / "shared" / "validate-artifacts.py")


VALIDATOR = _load_validator()


def _status_counts(statuses: tuple[str, ...], observed: list[str]) -> dict[str, int]:
    """Return complete status counts required by the validator contract.

    Example:
        >>> _status_counts(("valid", "invalid"), ["valid", "valid"])
        {'valid': 2, 'invalid': 0}
    """
    return {status: observed.count(status) for status in statuses}


def _recount(table: dict[str, Any]) -> None:
    """Recompute both aggregate status counts so they agree with the (edited) item rows."""
    items = table["items"]
    table["triage_status_counts"] = _status_counts(TRIAGE_STATUSES, [item["triage_status"] for item in items])
    table["resolution_status_counts"] = _status_counts(
        RESOLUTION_STATUSES, [item["resolution_status"] for item in items]
    )


def _metadata() -> dict[str, Any]:
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
    table = {
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
        "selectable_rows_total": 1,
        "source_records_total": 3,
        "table_rows_total": 2,
    }
    _recount(table)
    return {"final_resolution_table": table}


def _write_action_items(metadata: dict[str, Any], out_dir: Path) -> None:
    """Render the durable Markdown table from the machine-readable item ledger."""
    items = metadata["final_resolution_table"]["items"]
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


def _edit_action_items(out_dir: Path, old: str, new: str) -> None:
    """Replace one fragment of the rendered durable table, as a careless hand edit would."""
    path = out_dir / "action-items.md"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


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


@pytest.fixture
def metadata() -> dict[str, Any]:
    """Return a fresh valid two-item ledger each test may edit freely."""
    return _metadata()


@pytest.fixture
def table(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return the final resolution table inside the ledger."""
    return metadata["final_resolution_table"]


@pytest.fixture
def items(table: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the machine item rows of the final resolution table."""
    return table["items"]


@pytest.fixture
def v3_metadata(metadata: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a version-3 ledger whose first item already carries a reasoned `Implemented:` disposition."""
    metadata["resolution_scope"] = V3_SCOPE
    items[0]["resolved_how"] = IMPLEMENTED_HOW
    return metadata


class TestFinalResolutionTable:
    """The machine ledger and its durable Markdown table must reconcile without loss."""

    def test_complete_ledger_matches_markdown(self, metadata: dict[str, Any], tmp_path: Path) -> None:
        """A complete machine ledger rendered without loss into Markdown is accepted.

        Two items with different dispositions (implemented, duplicate) and a grouped report-plus-online source are
        rendered to `action-items.md`; this is the baseline every rejection case below perturbs.
        """
        _write_action_items(metadata, tmp_path)

        assert VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path) is None

    def test_report_source_accepts_json_item_pointer(
        self, metadata: dict[str, Any], items: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """A report JSON path paired with its stable finding ID is a valid source pointer."""
        items[0]["sources"][0]["source_id"] = ".reports/codex/review/result.json#investigate:H1"
        _write_action_items(metadata, tmp_path)

        assert VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path) is None

    def test_workflow_columns_beyond_required_are_accepted(self, metadata: dict[str, Any], tmp_path: Path) -> None:
        """The wider column list the workflow mandates is accepted, since the required columns are a minimum."""
        _write_action_items(metadata, tmp_path)
        columns = ("selection index", "item id or source location", "source category", "PR/diff relation", "severity")
        _widen_resolution_table(tmp_path, (*columns, "summary"))

        assert VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path) is None

    def test_code_spans_around_ledger_text_are_accepted(self, metadata: dict[str, Any], tmp_path: Path) -> None:
        """A detail line may mark paths and commands as code without altering the ledger text."""
        _write_action_items(metadata, tmp_path)
        _edit_action_items(tmp_path, "[E1] tests/test_guard.py passed", "[E1] `tests/test_guard.py` passed")

        assert VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path) is None

    def test_source_cells_hold_only_compact_references(self, metadata: dict[str, Any], tmp_path: Path) -> None:
        """Source provenance stays readable in the table while expanded records retain full detail."""
        _write_action_items(metadata, tmp_path)

        headers, rows = VALIDATOR._parse_markdown_table(tmp_path / "action-items.md", "Review Item Resolution Table")

        source_index = headers.index("sources")
        assert rows[0][source_index] == "report [.reports/codex/investigate/run/root-cause.md:42]"
        assert rows[1][source_index] == (
            "report [.reports/codex/investigate/run/root-cause.md:57] online [thread-991/comment-27]"
        )

    def test_ledger_without_items_is_refused(
        self, metadata: dict[str, Any], table: dict[str, Any], tmp_path: Path
    ) -> None:
        """Aggregate counts that cannot prove the disposition of each input item are rejected."""
        _write_action_items(metadata, tmp_path)
        table.pop("items")

        with pytest.raises(SystemExit, match=f"{TABLE_ERROR}items-not-list"):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    def test_duplicate_item_ids_are_refused(
        self, metadata: dict[str, Any], items: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """A ledger that accounts for one ingested item twice and omits another is rejected."""
        _write_action_items(metadata, tmp_path)
        items[1]["input_item_id"] = "R1"

        with pytest.raises(SystemExit, match=f"{TABLE_ERROR}item-id-duplicate"):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    def test_markdown_row_omission_is_refused_even_when_counts_match(
        self, metadata: dict[str, Any], tmp_path: Path
    ) -> None:
        """A durable table that silently drops one machine-accounted item is rejected.

        The Markdown is rendered from a copy missing the second item while the ledger and its aggregate counts still
        list both, so only a row-by-row comparison can notice the loss.
        """
        rendered = copy.deepcopy(metadata)
        rendered["final_resolution_table"]["items"].pop()
        _write_action_items(rendered, tmp_path)

        with pytest.raises(SystemExit, match=f"{TABLE_ERROR}markdown-row-count-mismatch"):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    def test_source_record_counts_must_match(
        self, metadata: dict[str, Any], table: dict[str, Any], tmp_path: Path
    ) -> None:
        """Aggregate source counts that could conceal an omitted grouped comment are rejected."""
        table["represented_source_records_total"] = 2
        _write_action_items(metadata, tmp_path)

        with pytest.raises(SystemExit, match=f"{TABLE_ERROR}source-count-mismatch"):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    @pytest.mark.parametrize(
        ("item", "source", "field", "value", "error"),
        [
            pytest.param(
                0, 0, "source_id", "R1", f"{TABLE_ERROR}report-source-id-invalid", id="report-id-unresolvable"
            ),
            pytest.param(
                1,
                1,
                "source_id",
                "https://github.com/example/repo/pull/1#discussion_r27",
                f"{TABLE_ERROR}online-source-id-invalid",
                id="online-id-is-a-url",
            ),
            pytest.param(
                0, 0, "kind", "pr-thread", f"{TABLE_ERROR}source-kind-invalid", id="source-kind-not-report-or-online"
            ),
        ],
    )
    def test_invalid_source_pointer_or_kind_is_refused(
        self,
        metadata: dict[str, Any],
        items: list[dict[str, Any]],
        tmp_path: Path,
        item: int,
        source: int,
        field: str,
        value: str,
        error: str,
    ) -> None:
        """Provenance a reader could not follow is rejected, keeping the vocabulary to report and online.

        A report ID must locate the originating finding rather than name a bare item, an online review ID must be a
        stable identifier rather than a link, and the source category must be either report or online.
        """
        items[item]["sources"][source][field] = value
        _write_action_items(metadata, tmp_path)

        with pytest.raises(SystemExit, match=error):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    @pytest.mark.parametrize(
        ("old", "new", "error"),
        [
            pytest.param(
                "| valid | implemented |",
                "| valid | unresolved |",
                f"{TABLE_ERROR}markdown-resolution_status-mismatch",
                id="disposition-differs-from-machine-item",
            ),
            pytest.param(
                "Please add the same boundary guard here.",
                "online duplicate",
                f"{TABLE_ERROR}expanded-source-detail-missing",
                id="grouped-source-detail-incomplete",
            ),
            pytest.param(
                "[O1] Added the missing guard.",
                "[O1]",
                f"{TABLE_ERROR}symbol-detail-missing:O1",
                id="compact-outcome-detail-absent",
            ),
            pytest.param(
                "| Resolved how | Evidence |",
                "| Resolved how | Closure evidence or unresolved rationale |",
                f"{TABLE_ERROR}markdown-columns-missing:evidence",
                id="required-column-renamed",
            ),
            pytest.param(
                "[E1] tests/test_guard.py passed",
                "[E1] `tests/test_guard.py` skipped",
                f"{TABLE_ERROR}symbol-detail-missing:E1",
                id="detail-wording-altered-beyond-code-markup",
            ),
        ],
    )
    def test_tampered_markdown_is_refused(
        self, metadata: dict[str, Any], tmp_path: Path, old: str, new: str, error: str
    ) -> None:
        """A durable action-items table that no longer matches its validated machine ledger is rejected.

        One fragment of the rendered table is edited after a valid ledger is written. A user-facing disposition must
        match the machine item, a compact grouped row needs every source detail in the expanded ledger, a compact
        outcome needs its complete text below the table, a required column renamed away from its contract name is
        reported as missing, and a detail line whose wording differs from the ledger is rejected even when the code
        markup alone would be accepted.
        """
        _write_action_items(metadata, tmp_path)
        _edit_action_items(tmp_path, old, new)

        with pytest.raises(SystemExit, match=error):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)


class TestPresentationVersion3:
    """Concise presentation-version-3 results need a reasoned, honest visible disposition per item."""

    def test_bare_unresolved_state_is_refused(self, metadata: dict[str, Any], tmp_path: Path) -> None:
        """A concise remediation result may not render a bare unresolved state, only a reasoned disposition.

        The same ledger passes once every `resolved_how` opens with its disposition label and a reason.
        """
        metadata["resolution_scope"] = V3_SCOPE
        _write_action_items(metadata, tmp_path)

        with pytest.raises(SystemExit, match="code-remediate-v3-resolution-disposition-invalid"):
            VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path)

    def test_reasoned_dispositions_are_accepted(
        self, metadata: dict[str, Any], items: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """Every item with a labelled, reasoned `resolved_how` satisfies the version-3 disposition rule."""
        metadata["resolution_scope"] = V3_SCOPE
        items[0]["resolved_how"] = IMPLEMENTED_HOW
        items[1]["resolved_how"] = "Duplicate: Same obligation as R1."
        _write_action_items(metadata, tmp_path)

        assert VALIDATOR._validate_code_remediate_final_resolution_table(metadata, tmp_path) is None

    def test_needs_clarification_cannot_claim_no_code_change_closure(
        self, v3_metadata: dict[str, Any], table: dict[str, Any], items: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """An open clarification request stays visibly open even when the gates pass.

        The second item is waiting on the user yet its `resolved_how` claims verified closure without code changes.
        """
        items[1].update(
            resolution_status="needs-clarification",
            owner_status="user-response-required",
            resolved_how="Verified without code changes: The report is incomplete.",
        )
        _recount(table)
        _write_action_items(v3_metadata, tmp_path)

        with pytest.raises(SystemExit, match="code-remediate-v3-resolution-disposition-invalid"):
            VALIDATOR._validate_code_remediate_final_resolution_table(v3_metadata, tmp_path)

    @pytest.mark.parametrize("field", ["triage_status", "resolution_status"])
    def test_stale_review_status_is_refused(
        self,
        v3_metadata: dict[str, Any],
        table: dict[str, Any],
        items: list[dict[str, Any]],
        tmp_path: Path,
        field: str,
    ) -> None:
        """Outdated-comment dismissal is rejected even when row and aggregate counts agree."""
        items[1][field] = "stale"
        label = "Stale" if field == "resolution_status" else "Duplicate"
        items[1]["resolved_how"] = f"{label}: Conflict resolution moved the commented lines."
        _recount(table)
        _write_action_items(v3_metadata, tmp_path)

        with pytest.raises(SystemExit, match="code-remediate-v3-stale-status-forbidden"):
            VALIDATOR._validate_code_remediate_final_resolution_table(v3_metadata, tmp_path)

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
    def test_other_review_dispositions_are_preserved(
        self,
        v3_metadata: dict[str, Any],
        table: dict[str, Any],
        items: list[dict[str, Any]],
        tmp_path: Path,
        status: str,
        label: str,
    ) -> None:
        """The user's existing outcome choices stay valid; only `stale` is removed."""
        items[1]["resolution_status"] = status
        items[1]["resolved_how"] = f"{label}: Original concern checked against current code and regression."
        _recount(table)
        _write_action_items(v3_metadata, tmp_path)

        assert VALIDATOR._validate_code_remediate_final_resolution_table(v3_metadata, tmp_path) is None


#: Resolution scope selection heading, counters, and table header shared by every scope-selection fixture.
_SCOPE_PREAMBLE = """## Resolution Scope Selection

selectable: 1
selected: 1
deferred: 0

| Index | Severity | Item id or source location | Source | Summary | Expected closure evidence |
| --- | --- | --- | --- | --- | --- |
"""
_GROUPED_SOURCES = "report [.reports/codex/investigate/run/root-cause.md:57]"
_DETAIL_SYMBOLS = "\n\n[S1] Add guard at the request boundary.\n[C1] Focused guard test passes.\n"


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


class TestScopeSelection:
    """The scope-selection table must print compact source references with detail defined below it."""

    def test_compact_grouped_source_references_are_accepted(self, tmp_path: Path) -> None:
        """Report and online IDs without source bodies or URLs are valid in scope selection."""
        row = f"| 1 | high | R2 | {_GROUPED_SOURCES} online [thread-991/comment-27] | [S1] | [C1] |"
        (tmp_path / "resolution-scope.md").write_text(_SCOPE_PREAMBLE + row + _DETAIL_SYMBOLS, encoding="utf-8")

        assert VALIDATOR._validate_code_remediate_scope_selection(_scope_selection_metadata(), tmp_path) is None

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            pytest.param(
                f"| 1 | high | R2 | {_GROUPED_SOURCES}<br>online [thread-991/comment-27] | [S1] | [C1] |{_DETAIL_SYMBOLS}",
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
    def test_noisy_or_unreferenced_rows_are_refused(self, tmp_path: Path, body: str, error: str) -> None:
        """A scope-selection table that would print noise or hide detail before a prompt is rejected.

        Grouped source pointers must not print a literal HTML tag, the noisy source-body format must not appear, and
        summary and closure detail must be symbols defined below the table rather than long text inside it.
        """
        (tmp_path / "resolution-scope.md").write_text(_SCOPE_PREAMBLE + body, encoding="utf-8")

        with pytest.raises(SystemExit, match=error):
            VALIDATOR._validate_code_remediate_scope_selection(_scope_selection_metadata(), tmp_path)


def _handoff_row(position: int, item: dict[str, Any]) -> dict[str, Any]:
    """Render one terminal-handoff table row for a machine item, with compact outcome and evidence symbols."""
    sources = item["sources"]
    return {
        "id": item["input_item_id"],
        "cells": [
            item["input_item_id"],
            item["severity"],
            item["item_name"],
            "\n".join(f"{source['kind']} [{source['source_id']}]" for source in sources),
            f"{item['resolution_status']} — [O{position}]",
            f"[E{position}] — owner/status: {item['owner_status']}",
        ],
        "source_ids": [f"{source['kind']}:{source['source_id']}" for source in sources],
    }


def _final_handoff(metadata: dict[str, Any]) -> dict[str, Any]:
    """Render the terminal handoff (rows, symbol details and source records) that matches a machine ledger."""
    items = metadata["final_resolution_table"]["items"]
    positioned = list(enumerate(items, start=1))
    details = [
        detail
        for position, item in positioned
        for detail in (
            {"id": f"O{position}", "text": item["resolved_how"]},
            {"id": f"E{position}", "text": item["evidence"]},
        )
    ]
    source_records = [
        {"id": f"{source['kind']}:{source['source_id']}", "evidence": source["evidence"]}
        for _, item in positioned
        for source in item["sources"]
    ]
    rows = [_handoff_row(position, item) for position, item in positioned]
    return {"tables": [{"rows": rows, "details": details}], "source_records": source_records}


@pytest.fixture
def concise_handoff(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a handoff matching the ledger, marked version 3 and concise but without `overview_only` yet."""
    metadata["resolution_scope"] = V3_SCOPE
    handoff = _final_handoff(metadata)
    handoff["tables"][0]["layout"] = "concise"
    return handoff


class TestFinalHandoff:
    """The terminal handoff cells must stay value-bound to the resolution items they summarize."""

    def test_value_bound_cells_are_accepted(self, metadata: dict[str, Any]) -> None:
        """A handoff whose rows, details and source records mirror the ledger is accepted.

        This is the complete-looking final table at presentation version 1, before any concise-layout rule applies.
        """
        handoff = _final_handoff(metadata)

        assert VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, handoff) is None

    def test_concise_layout_requires_overview_only(
        self, metadata: dict[str, Any], concise_handoff: dict[str, Any]
    ) -> None:
        """A version-3 concise table that is not flagged overview-only is rejected."""
        with pytest.raises(SystemExit, match="code-remediate-final-handoff-overview-only-required"):
            VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, concise_handoff)

    def test_concise_overview_only_layout_is_accepted(
        self, metadata: dict[str, Any], concise_handoff: dict[str, Any]
    ) -> None:
        """A version-3 concise table flagged overview-only is accepted."""
        concise_handoff["tables"][0]["overview_only"] = True

        assert VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, concise_handoff) is None

    def test_changed_resolution_cell_breaks_row_coverage(
        self, metadata: dict[str, Any], concise_handoff: dict[str, Any]
    ) -> None:
        """A complete-looking final table that changes what or how an item resolved is rejected.

        The first row's resolution cell is replaced by a vague "details omitted" text that no longer matches the
        machine item it summarizes.
        """
        concise_handoff["tables"][0]["overview_only"] = True
        concise_handoff["tables"][0]["rows"][0]["cells"][4] = "implemented — details omitted"

        with pytest.raises(SystemExit, match="code-remediate-final-handoff-row-coverage-mismatch"):
            VALIDATOR._validate_code_remediate_final_handoff({"metadata": metadata}, concise_handoff)

    @pytest.mark.parametrize(
        "validator", [VALIDATOR._validate_code_remediate_final_handoff, VALIDATOR._validate_code_review_final_handoff]
    )
    def test_caller_contract_bypasses_workflow_owned_table_layout(self, validator: object) -> None:
        """An explicit caller output contract replaces the workflow-owned final tables.

        With no metadata and a `branch` marker the handoff is the caller's own format, so neither the remediate nor
        the review validator may demand its table layout.
        """
        assert validator({"metadata": {}}, {"branch": "caller-contract"}) is None
