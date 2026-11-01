"""Acceptance checks for canonical findings and readable selection handoffs."""

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest
from _final_handoff_support import FINALIZER, handoff_payload, write_schema_v2_assess
from _platform import FILE_SYMLINKS_AVAILABLE
from markdown_it import MarkdownIt
from rich.console import Console
from rich.markdown import Markdown
from test_code_remediate_final_outcome_validation import VALIDATOR, _write_action_items
from test_code_remediate_final_outcome_validation import _metadata as resolution_metadata
from test_code_remediate_work_bucket_validation import _write_workplan
from test_review_completion_gate import _assessed_pr
from test_review_finding_identity import _load_validator, _metadata, _result

FIND_REPORT = Path(__file__).resolve().parents[3] / "shared/find-review-report.py"
WRITE_RESULT = FINALIZER.with_name("write-result.py")
PR_URL = "https://github.com/acme/widgets/pull/123"
PASSING_ROLLUP = [{"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
CORRECT_PR_VALUES = {
    "PR": f"[#123 — Correct widget spelling]({PR_URL})",
    "Author": "@contributor",
    "CI": "passing",
    "Type": "docs",
}


def _load_finalizer() -> ModuleType:
    """Load a fresh copy of the standalone finalizer script without requiring a package import."""
    specification = importlib.util.spec_from_file_location("codex_rig_final_handoff", FINALIZER)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


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


def _with_proposals(payload: dict, proposal: str = "Add a distinguishing regression.") -> dict:
    """Give every selection item a resolution proposal, as presentation version 3 requires."""
    for item in payload["items"]:
        item["resolution_proposal"] = proposal
    return payload


def _write_selection_files(directory: Path, payload: dict) -> None:
    """Write ``selection.json`` and its canonical ``resolution-scope.md`` display into ``directory``."""
    (directory / "selection.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(payload).encode("utf-8"))


def _run_selection_cli(inventory: Path, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the real finalizer ``selection`` command for ``inventory`` writing its display to ``output``."""
    command = [sys.executable, str(FINALIZER), "selection", "--input", str(inventory), "--out-scope", str(output)]
    return subprocess.run([*command, *extra], capture_output=True, text=True, check=False)


def _bind_final_handoff(handoff_path: Path, metadata: dict) -> dict:
    """Render the handoff beside itself and record its digests in ``metadata['final_handoff']``."""
    out = handoff_path.parent
    proof = _load_finalizer().render_files(handoff_path, out / "final.md", out / "final-handoff.validation.json")
    metadata["final_handoff"].update(handoff_sha256=proof["handoff_sha256"], rendered_sha256=proof["rendered_sha256"])
    return proof


def _load_handoff(run: Path) -> dict:
    """Read the ``final-handoff.json`` payload of a review run directory."""
    return json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))


def _remediation_payload(layout: str, **table_fields: object) -> dict:
    """Return a remediation handoff whose first table uses ``layout`` with bracketed outcome/evidence details."""
    payload = handoff_payload()
    table = payload["tables"][0]
    table["layout"] = layout
    table.update(table_fields)
    table["rows"][0]["cells"][4] = "implemented — [O1]"
    table["rows"][0]["cells"][5] = "[E1] — owner/status: fixed"
    table["details"] = [{"id": "O1", "text": "Guard added."}, {"id": "E1", "text": "Tests pass."}]
    return payload


@pytest.mark.installed_plugin
def test_selection_cli_renders_named_groups_before_prompt(tmp_path: Path) -> None:
    """Selection display keeps indexes distinct from stable IDs and avoids premature deferral.

    The real finalizer command renders a fresh inventory with no selection yet: it must present numbered, named rows
    awaiting the user's choice instead of marking anything deferred.
    """
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(_selection()), encoding="utf-8")
    output = tmp_path / "resolution-scope.md"

    completed = _run_selection_cli(path, output)

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


def _duplicate_source(payload: dict) -> None:
    """Make both items own the same source record."""
    payload["items"][1]["sources"] = copy.deepcopy(payload["items"][0]["sources"])


def _duplicate_finding(payload: dict) -> None:
    """Make the second item reuse the first item's report finding under another source id."""
    source = copy.deepcopy(payload["items"][0]["sources"][0])
    source["source_id"] = "result.json#another-mention"
    payload["items"][1]["sources"] = [source]


def _bad_selection(payload: dict) -> None:
    """Select an index that no item has."""
    payload["selected_indexes"] = [7]


def _wrong_count(payload: dict) -> None:
    """Declare a source-record total that disagrees with the inventory."""
    payload["source_records_total"] = 19


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_duplicate_source, id="duplicate-source"),
        pytest.param(_duplicate_finding, id="duplicate-finding"),
        pytest.param(_bad_selection, id="bad-selection"),
        pytest.param(_wrong_count, id="wrong-count"),
    ],
)
def test_selection_rejects_invalid_inventory(mutate: Callable[[dict], None]) -> None:
    """Mismatched source/selection identities are rejected before a user can select work.

    Each case corrupts a valid inventory in one way (shared source, reused finding, unknown selected index, wrong
    declared total); rendering must refuse with a selection-specific error rather than present bad choices.
    """
    payload = _selection()
    mutate(payload)

    with pytest.raises(
        ValueError, match=r"selection-(count-mismatch|index-invalid|source-duplicate|source-finding-id-mismatch)"
    ):
        _load_finalizer().render_selection(payload)


@pytest.fixture
def embedded_selection() -> dict:
    """Two findings from one review, each owning its own online fragment of that review."""
    payload = _selection()
    payload["source_records_total"] = 2
    payload["items_total"] = 2
    for ordinal, item in enumerate(payload["items"], 1):
        item["sources"] = [
            {
                "kind": "online",
                "source_id": f"review-42#finding-{ordinal}",
                "location": "src/parser.py:12",
                "body": item["summary"],
                "evidence": "pr/reviews.json",
            }
        ]
    return payload


def test_embedded_findings_stay_separate_choices(embedded_selection: dict) -> None:
    """Two obligations embedded in one review render as two selectable rows without mutating the input.

    One review carries two findings; each must keep its own numbered row and its own source reference exactly once.
    """
    original = copy.deepcopy(embedded_selection)

    rendered = _load_finalizer().render_selection(embedded_selection)

    assert "| 1 | high | F7 — Preserve input compatibility |" in rendered
    assert "| 2 | medium | F9 — Cover the boundary |" in rendered
    assert rendered.count("online [review-42#finding-1]") == 1
    assert rendered.count("online [review-42#finding-2]") == 1
    assert embedded_selection == original


def test_embedded_findings_reject_dropped_fragment(embedded_selection: dict) -> None:
    """Dropping one embedded finding while declaring both source records is a count mismatch."""
    embedded_selection["items"].pop()

    with pytest.raises(ValueError, match="selection-count-mismatch:source_records_total"):
        _load_finalizer().render_selection(embedded_selection)


def test_embedded_findings_reject_reused_fragment(embedded_selection: dict) -> None:
    """Two findings claiming the same review fragment are rejected as duplicate source ownership."""
    embedded_selection["items"][1]["sources"][0]["source_id"] = "review-42#finding-1"

    with pytest.raises(ValueError, match="selection-source-duplicate"):
        _load_finalizer().render_selection(embedded_selection)


def test_grouped_final_remediation_keeps_evidence_without_symbols() -> None:
    """Grouped layout renders bound machine cells as short rows and per-finding details.

    The payload references outcome and evidence detail by symbol (``[O1]``/``[E1]``); the reader must see the
    substituted text and never the raw symbols.
    """
    rendered = _load_finalizer().render_handoff(_remediation_payload("grouped"))

    assert "| ID | Severity | Finding | Outcome |" in rendered
    assert "- Outcome: implemented — Guard added." in rendered
    assert "- Evidence / next action: Tests pass. — owner/status: fixed" in rendered
    assert "[O1]" not in rendered
    assert "[E1]" not in rendered


def test_concise_review_states_actions_once_and_names_gate() -> None:
    """Version-three review proposals and supporting context keep their original concise view.

    Two findings (one with context and closure evidence, one operational gate) render each title and required change
    once, with the supporting bullets, and without the redundant status/required-change/issue bullets.
    """
    payload = handoff_payload()
    payload.update(
        skill="code-review",
        branch="assessed",
        outcome={"title": "Review Decision", "summary": "Recommendation: needs-more-work."},
        presentation_version=3,
    )
    payload["tables"] = [
        {
            "heading": "Review Findings and Merge Blocks",
            "layout": "concise",
            "columns": ["Finding / area", "Required change", "Evidence", "Status"],
            "rows": [
                {
                    "id": "CR-1",
                    "title": "Boundary regression unprotected",
                    "summary": "Both selectors satisfy the current assertion.",
                    "closure_evidence": "The regression fails with the wrong selector.",
                    "cells": ["CR-1", "Use inputs that distinguish selectors.", "tests/test_parser.py:42", "Required"],
                    "source_ids": ["report:CR-1"],
                },
                {
                    "id": "CR-2",
                    "title": "Canonical gates unavailable",
                    "cells": ["CR-2", "Rerun gates in the supported environment.", "gates.json", "Verify"],
                    "source_ids": ["report:CR-2"],
                },
            ],
        }
    ]
    texts = [
        "Boundary regression unprotected",
        "Use inputs that distinguish selectors.",
        "Canonical gates unavailable",
        "Rerun gates in the supported environment.",
    ]

    rendered = _load_finalizer().render_handoff(payload)

    assert "| ID | Finding | Resolution proposal | Status |" in rendered
    assert [rendered.count(text) for text in texts] == [1, 1, 1, 1]
    assert "**CR-1**" in rendered
    assert "- Context: Both selectors satisfy the current assertion." in rendered
    assert "- Done when: The regression fails with the wrong selector." in rendered
    assert "- Evidence: gates.json" in rendered
    assert "- Status:" not in rendered
    assert "- Required change:" not in rendered
    assert "- Issue:" not in rendered


def test_concise_selection_aggregates_sources_without_losing_references() -> None:
    """Concise selection shows bounded source tags and proposals while keeping full provenance once.

    The first item gains a second (online) source; the overview must summarise counts per kind, while every source id
    still appears exactly once in the detail section.
    """
    payload = _with_proposals(_selection())
    payload["presentation_version"] = 3
    payload["items"][0]["sources"].append(
        {
            "kind": "online",
            "source_id": "thread-2",
            "location": "src/parser.py:12",
            "body": "Protect this input.",
            "evidence": "threads.json",
        }
    )
    items = payload["items"]

    rendered = _load_finalizer().render_selection(payload)

    assert "| # | Severity | Finding | Resolution proposal | Sources |" in rendered
    assert "| report ×1; online ×1 |" in rendered
    assert "| online ×1 |" in rendered
    assert [rendered.count(item["item_name"]) for item in items] == [1, 1]
    assert [rendered.count(f"{s['kind']} [{s['source_id']}]") for item in items for s in item["sources"]] == [1, 1, 1]
    assert "- Context: Existing supported inputs must remain accepted." in rendered
    assert "### 1 · F7\n" in rendered
    assert "- Issue:" not in rendered


def test_concise_selection_requires_proposal() -> None:
    """Selectable work without a concrete resolution proposal is not offered.

    A version-three inventory whose items lack ``resolution_proposal`` must fail rendering instead of showing choices
    the user cannot evaluate.
    """
    payload = _selection()
    payload["presentation_version"] = 3

    with pytest.raises(ValueError, match="selection-item-resolution_proposal"):
        _load_finalizer().render_selection(payload)


def _gate_blocker_metadata() -> dict:
    """Review metadata carrying one operational gate blocker."""
    metadata = _metadata()
    metadata["operational_blockers"] = [
        {
            "id": "GATE-1",
            "title": "Canonical gates unavailable",
            "required_change": "Rerun canonical gates.",
            "evidence": ["gates.json"],
        }
    ]
    return metadata


def test_operational_blocker_accepts_bound_description() -> None:
    """A gate failure with a bound description is named as an operational blocker, not a source defect."""
    identities = _load_validator()._operational_blocker_identities(_gate_blocker_metadata(), {"F7"})

    assert identities == {"GATE-1"}


def test_operational_blocker_rejects_empty_title() -> None:
    """A gate blocker whose title was blanked is rejected instead of shown as an anonymous failure."""
    metadata = _gate_blocker_metadata()
    metadata["operational_blockers"][0]["title"] = ""

    with pytest.raises(SystemExit, match="review-operational-blocker"):
        _load_validator()._operational_blocker_identities(metadata, {"F7"})


def test_concise_remediation_expands_resolution_once() -> None:
    """Overview-only concise remediation shows each resolution once and leaves source details to the ledger.

    The row references its outcome and evidence by symbol; the table must substitute the text inline, link the item
    ledger, and omit the per-finding detail block.
    """
    rendered = _load_finalizer().render_handoff(_remediation_payload("concise", overview_only=True))

    assert "| ID | Severity | Finding | Resolution | Outcome |" in rendered
    assert "| Guard added. | implemented |" in rendered
    assert rendered.count("Guard added.") == 1
    assert rendered.count("Preserve a \\| boundary") == 1
    assert "- Sources:" not in rendered
    assert "- Evidence / next action:" not in rendered
    assert "**CR-1**" not in rendered
    assert "Ledger: .reports/codex/code-remediate/run/action-items.md" in rendered
    assert "- Outcome:" not in rendered
    assert "[O1]" not in rendered
    assert "[E1]" not in rendered


def test_prior_concise_remediation_keeps_detail_blocks() -> None:
    """Byte-bound reports written before overview-only handoffs keep their per-finding detail blocks."""
    rendered = _load_finalizer().render_handoff(_remediation_payload("concise"))

    assert "**CR-1**" in rendered
    assert "- Sources:\n  - report [CR-1]" in rendered
    assert "- Evidence / next action: Tests pass. — owner/status: fixed" in rendered


def test_new_concise_remediation_requires_overview_only() -> None:
    """New version-three concise handoffs must avoid repeating linked ledger details in chat."""
    finalizer = _load_finalizer()
    payload = handoff_payload()
    payload["presentation_version"] = 3
    payload["tables"][0]["layout"] = "concise"

    with pytest.raises(finalizer.HandoffError, match="remediation-overview-only-required"):
        finalizer.render_handoff(payload)


def test_overview_only_remediation_requires_linked_ledger() -> None:
    """A compact chat table is rejected when its complete Markdown evidence ledger has no link."""
    payload = handoff_payload()
    payload["tables"][0].update(layout="concise", overview_only=True)
    payload["artifacts"] = [{"label": "Result", "path": "run/result.json"}]

    with pytest.raises(ValueError, match="remediation-overview-ledger-missing"):
        _load_finalizer().render_handoff(payload)


@pytest.mark.parametrize("resolution_status", ["Unresolved", "needs-clarification", "needs-clarification "])
def test_overview_only_remediation_requires_action_for_unresolved_row(resolution_status: str) -> None:
    """An action for one open row cannot cover another open row.

    Row CR-1 is unresolved with a matching follow-up; row CR-2 is open under a differently spelled status and has none,
    so CR-2 must be named as missing its action.
    """
    payload = handoff_payload()
    payload["tables"][0].update(layout="concise", overview_only=True)
    payload["tables"][0]["rows"][0]["cells"][4] = "Unresolved — local follow-up required"
    payload["tables"][0]["rows"][1]["cells"][4] = f"{resolution_status} — external runner unavailable"
    payload["remaining"] = [
        {"row_id": "CR-1", "item": "Local follow-up", "owner": "developer", "next_action": "Fix CR-1."}
    ]
    payload["next_steps"] = ["CR-1"]

    with pytest.raises(ValueError, match="remediation-unresolved-action-missing:CR-2"):
        _load_finalizer().render_handoff(payload)


def test_v3_remediation_next_steps_identify_each_open_row() -> None:
    """Identical action descriptions stay bound to distinct result IDs.

    Two open rows share the same item, owner and next action text; each next step must still print its own row ID so the
    reader can tell the rows apart.
    """
    payload = handoff_payload()
    payload["presentation_version"] = 3
    payload["tables"][0].update(layout="concise", overview_only=True)
    payload["tables"][0]["rows"][0]["cells"][4] = "Unresolved — local follow-up required"
    payload["remaining"] = [
        {"row_id": row_id, "item": "Same action", "owner": "developer", "next_action": "Investigate."}
        for row_id in ("CR-1", "CR-2")
    ]
    payload["next_steps"] = ["CR-1", "CR-2"]

    rendered = _load_finalizer().render_handoff(payload)

    assert "- CR-1 — Same action — owner: developer — next: Investigate." in rendered
    assert "- CR-2 — Same action — owner: developer — next: Investigate." in rendered


@pytest.mark.parametrize(
    "ledger_path",
    [
        pytest.param("../../other-run/action-items.md", id="relative-escape"),
        pytest.param(".reports/codex/code-remediate/other-run/action-items.md", id="other-run"),
    ],
)
def test_overview_only_remediation_rejects_ledger_outside_result_run(ledger_path: str) -> None:
    """The compact handoff must link to its own run's complete item ledger, not another run's."""
    payload = handoff_payload()
    payload["tables"][0].update(layout="concise", overview_only=True)
    payload["artifacts"][1]["path"] = ledger_path

    with pytest.raises(ValueError, match="remediation-overview-ledger-missing"):
        _load_finalizer().render_handoff(payload)


def test_concise_remediation_renders_reasoned_block_without_raw_unresolved() -> None:
    """An unresolved machine status never appears as a misleading visible outcome.

    The row is unresolved with a ``Blocked:`` detail; the table must show the stated reason beside a ``Blocked`` outcome
    instead of the raw ``unresolved`` token.
    """
    payload = handoff_payload()
    table = payload["tables"][0]
    table["layout"] = "concise"
    table["rows"][0]["cells"][4] = "unresolved — [O1]"
    table["rows"][0]["cells"][5] = "[E1] — owner/status: external-reviewer"
    table["details"] = [
        {"id": "O1", "text": "Blocked: The reviewer cannot inspect the supplied context."},
        {"id": "E1", "text": "Review environment record."},
    ]

    rendered = _load_finalizer().render_handoff(payload)

    assert "| The reviewer cannot inspect the supplied context. | Blocked |" in rendered
    assert "| unresolved |" not in rendered


def test_historical_presentation_keeps_digest_bound_bytes() -> None:
    """Saved version-2 selection and legacy/grouped handoffs from release 0.14.3 render to identical bytes.

    Older reports are admitted by digest, so any change to the legacy renderers' output must fail here.
    """
    renderer = _load_finalizer()
    payload = handoff_payload()
    legacy = hashlib.sha256(renderer.render_handoff(payload).encode()).hexdigest()
    payload["tables"][0]["layout"] = "grouped"
    grouped = hashlib.sha256(renderer.render_handoff(payload).encode()).hexdigest()
    selection = hashlib.sha256(renderer.render_selection(_selection()).encode()).hexdigest()

    assert (legacy, grouped, selection) == (
        "acb1961071e2667d46ee791a4b8b01ef8881695922cc8ea86f0abac587d93ba6",
        "24eb8eb5e23dac060295fadc5fead206e3e6b176abcebc0b552788bbf446cf31",
        "2e3d8bc414fd5b5af44557801d3314f340ac97b656e90d63162f946a2bba5731",
    )


WRONG_PR_VALUES = [
    pytest.param("PR", f"[#999 — Correct widget spelling]({PR_URL})", id="wrong-number"),
    pytest.param("PR", f"[#123 — Other]({PR_URL})", id="wrong-title"),
    pytest.param("PR", "[#123 — Correct widget spelling](https://github.com/acme/widgets/pull/999)", id="wrong-url"),
    pytest.param("Author", "@someone-else", id="wrong-author"),
    pytest.param("CI", "failing — tests", id="wrong-ci"),
    pytest.param("Type", "imaginary", id="invalid-type"),
]


def _pr_snapshot_candidate(
    tmp_path: Path, values: dict[str, str], *, title: str = "Correct widget spelling", rollup: object = PASSING_ROLLUP
) -> tuple[Path, dict, dict]:
    """Build a digest-valid assessed PR review whose snapshot rows show ``values``; return (run, result, gates).

    The collected ``pr.json`` evidence carries ``title`` and ``rollup``; the snapshot rows are written independently of
    it so a test can make them agree or contradict the evidence.
    """
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    pr = json.loads((run / "pr.json").read_text(encoding="utf-8"))
    pr.update(title=title, author={"login": "contributor"}, statusCheckRollup=rollup)
    (run / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
    assessment = {"role": "QA specialist", "rating": 1, "evidence": "specialists/qa.md"}
    result["metadata"]["reviewer_assessments"] = [assessment]
    handoff_path = run / "final-handoff.json"
    handoff = _load_handoff(run)
    snapshot = handoff["tables"][0]
    snapshot.update(reviewers=[assessment], summary=result["metadata"]["review_decision"]["summary"])
    for row in snapshot["rows"]:
        row["cells"][1] = values.get(row["cells"][0], row["cells"][1])
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    _bind_final_handoff(handoff_path, result["metadata"])
    gates = json.loads((run / "gates.json").read_text(encoding="utf-8"))
    return run, result, gates


def test_candidate_pr_snapshot_matching_evidence_is_accepted(tmp_path: Path) -> None:
    """A candidate whose PR snapshot agrees with the collected evidence passes final-handoff validation."""
    run, result, gates = _pr_snapshot_candidate(tmp_path, CORRECT_PR_VALUES)

    VALIDATOR._validate_final_handoff(result, "code-review", run, gates, candidate=True)


@pytest.mark.parametrize(("field", "wrong_value"), WRONG_PR_VALUES)
def test_candidate_pr_snapshot_values_match_collected_evidence(tmp_path: Path, field: str, wrong_value: str) -> None:
    """A digest-valid candidate whose snapshot contradicts the collected PR evidence is rejected.

    The collected PR says number 123, title "Correct widget spelling", author contributor, CI passing, type docs. Each
    case changes one snapshot value so it disagrees with that evidence while the digests still bind.
    """
    run, result, gates = _pr_snapshot_candidate(tmp_path, {**CORRECT_PR_VALUES, field: wrong_value})

    with pytest.raises(SystemExit, match="code-review-final-handoff-pr-snapshot-value-mismatch"):
        VALIDATOR._validate_final_handoff(result, "code-review", run, gates, candidate=True)


@pytest.mark.parametrize(("field", "wrong_value"), WRONG_PR_VALUES)
def test_retained_pr_snapshot_is_not_reconciled_with_evidence(tmp_path: Path, field: str, wrong_value: str) -> None:
    """A retained (non-candidate) report is validated by digest only, so its recorded snapshot values stand."""
    run, result, gates = _pr_snapshot_candidate(tmp_path, {**CORRECT_PR_VALUES, field: wrong_value})

    VALIDATOR._validate_final_handoff(result, "code-review", run, gates)


def test_candidate_pr_snapshot_escapes_collected_title_link_syntax(tmp_path: Path) -> None:
    """Link syntax in a collected PR title stays inert while canonical destinations move to resource lists.

    The title contains a closing bracket and a hostile URL; the escaped form must render a single real link (the PR
    itself), keep the hostile text out of every table row, and still list the report path.
    """
    values = {
        "PR": f"[#123 — Fix &#93;(https://evil.example/other) widget]({PR_URL})",
        "Author": "@contributor",
        "CI": "unavailable",
        "Type": "docs",
    }
    run, result, gates = _pr_snapshot_candidate(
        tmp_path, values, title="Fix ](https://evil.example/other) widget", rollup=[]
    )

    VALIDATOR._validate_final_handoff(result, "code-review", run, gates, candidate=True)

    rendered = (run / "final.md").read_text(encoding="utf-8")
    links = [
        child.attrs["href"]
        for token in MarkdownIt("default").parse(rendered)
        for child in token.children or []
        if child.type == "link_open"
    ]
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert links == [PR_URL]
    assert f"- PR (Value): {values['PR']}" in rendered
    assert "https://evil.example/other" not in table
    assert f"- Report: {(run / 'review-notes.md').resolve().as_posix()}" in rendered
    assert "Fix &#93;(https://evil.example/other) widget" in rendered


def test_candidate_pr_snapshot_rejects_unescaped_title_link(tmp_path: Path) -> None:
    """A snapshot that embeds unescaped link syntax from the title is not the escaped canonical value."""
    values = {
        "PR": f"[#123 — Fix ](https://evil.example/other) widget]({PR_URL})",
        "Author": "@contributor",
        "CI": "unavailable",
        "Type": "docs",
    }
    run, result, gates = _pr_snapshot_candidate(
        tmp_path, values, title="Fix ](https://evil.example/other) widget", rollup=[]
    )

    with pytest.raises(SystemExit, match="code-review-final-handoff-pr-snapshot-value-mismatch"):
        VALIDATOR._validate_final_handoff(result, "code-review", run, gates, candidate=True)


def _forged_non_pr_snapshots(tmp_path: Path) -> tuple[Path, dict, dict, Path]:
    """Build a working-tree review whose handoff also carries a forged PR snapshot; return (run, result, handoff,
    path)."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    result["metadata"]["scope"] = "working-tree"
    handoff_path = run / "final-handoff.json"
    handoff = _load_handoff(run)
    pr_snapshot = handoff["tables"][0]
    pr_snapshot.pop("reviewers")
    pr_snapshot.pop("summary")
    review_snapshot = copy.deepcopy(pr_snapshot)
    review_snapshot["heading"] = "Review Snapshot"
    review_snapshot["reviewers"] = result["metadata"]["reviewer_assessments"]
    review_snapshot["summary"] = result["metadata"]["review_decision"]["summary"]
    values = [
        ("Scope", "working-tree"),
        ("Revision", f"diff sha256:{'0' * 64}"),
        ("CI", "unavailable"),
        ("Type", "docs"),
        ("Commit", result["metadata"]["reviewed_commit_sha"] + " (working-tree base)"),
        ("Suggestion", "approve"),
    ]
    assert result["metadata"]["review_input_sha256"] != "0" * 64
    for index, (row, value) in enumerate(zip(review_snapshot["rows"], values, strict=True), start=1):
        row.update(id=f"R{index}", cells=list(value), source_ids=[f"review:{value[0]}"])
    handoff["tables"].append(review_snapshot)
    handoff["source_records"].extend({"id": f"review:{field}", "evidence": "diff.patch"} for field, _ in values)
    total = len(handoff["source_records"])
    handoff["source_coverage"].update(source_records_total=total, represented_source_records_total=total)
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    return run, result, handoff, handoff_path


def test_rendering_rejects_forged_pr_snapshot_in_non_pr_handoff(tmp_path: Path) -> None:
    """Rendering a working-tree review that also lists a PR snapshot fails with a snapshot-required error.

    The extra snapshot mixes PR and local-review identities in one handoff; the renderer must refuse before any final
    artifact is produced.
    """
    run, _, _, handoff_path = _forged_non_pr_snapshots(tmp_path)

    with pytest.raises(ValueError, match="review-snapshot-required"):
        _load_finalizer().render_files(handoff_path, run / "final.md", run / "final-handoff.validation.json")


@pytest.mark.parametrize("candidate", [False, True])
def test_source_reconciliation_rejects_forged_pr_snapshot_in_non_pr_handoff(tmp_path: Path, candidate: bool) -> None:
    """Source reconciliation rejects the mixed snapshots for both fresh (candidate) and retained reports."""
    run, result, handoff, _ = _forged_non_pr_snapshots(tmp_path)

    with pytest.raises(SystemExit, match="code-review-final-handoff-snapshot-scope-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=candidate, out_dir=run)


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        pytest.param(None, "unavailable", id="absent"),
        pytest.param([], "unavailable", id="empty"),
        pytest.param(
            [{"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "NEUTRAL"}],
            "passing",
            id="neutral-completed",
        ),
        pytest.param(
            [{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS", "conclusion": None}],
            "pending — tests",
            id="incomplete",
        ),
        pytest.param(
            [
                {"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "conclusion": None},
                {"__typename": "StatusContext", "context": "tests", "state": "FAILURE"},
            ],
            "failing — tests",
            id="failure-before-pending",
        ),
    ],
)
def test_pr_snapshot_ci_uses_collected_rollup(rollup: object, expected: str) -> None:
    """No checks read as unavailable, and a failing check outranks a pending one."""
    assert VALIDATOR._code_review_pr_ci_snapshot(rollup) == expected


def test_enriched_review_records_accept_titles_without_changing_identity() -> None:
    """Canonical descriptions are allowed on review records while legacy records and severity binding stay valid.

    A bare legacy record validates; adding title, summary, required change, evidence and closure evidence still
    validates; blanking the title is then rejected.
    """
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


def _confirmed_selection(tmp_path: Path, selected: list[int], presentation: int) -> dict:
    """Write a confirmed explicit selection for two items and return remediation metadata that binds to it."""
    payload = _with_proposals(_selection(), "Add the missing regression.")
    payload["presentation_version"] = presentation
    payload["selected_indexes"] = selected
    _write_selection_files(tmp_path, payload)
    return {
        "resolution_scope": {
            "presentation_version": presentation,
            "selection_source": "explicit-input",
            "prompt_presented": False,
            "selection_confirmed_by_user": True,
            "selected_indexes": selected,
            "selected_severity_groups": [],
            "deferred_indexes": [index for index in (1, 2) if index not in selected],
        },
        "final_resolution_table": {"items": copy.deepcopy(payload["items"])},
    }


SELECTED_INDEXES = [
    pytest.param([], id="none"),
    pytest.param([1], id="first"),
    pytest.param([2], id="second"),
    pytest.param([1, 2], id="all"),
]


@pytest.mark.parametrize("selected", SELECTED_INDEXES)
@pytest.mark.parametrize("presentation", [2, 3])
def test_selection_confirmation_accepts_matching_final_inventory(
    tmp_path: Path, selected: list[int], presentation: int
) -> None:
    """A confirmed selection whose final table equals the presented inventory passes validation."""
    metadata = _confirmed_selection(tmp_path, selected, presentation)

    VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)


@pytest.mark.parametrize("selected", SELECTED_INDEXES)
@pytest.mark.parametrize("presentation", [2, 3])
def test_selection_confirmation_rejects_changed_item_type(
    tmp_path: Path, selected: list[int], presentation: int
) -> None:
    """Retyping an item in the final table breaks the binding to the presented inventory."""
    metadata = _confirmed_selection(tmp_path, selected, presentation)
    metadata["final_resolution_table"]["items"][0]["item_type"] = "review-gate"

    with pytest.raises(SystemExit, match="selection-final-inventory-mismatch"):
        VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)


@pytest.mark.parametrize("selected", SELECTED_INDEXES)
@pytest.mark.parametrize("presentation", [2, 3])
def test_selection_confirmation_rejects_substituted_source(
    tmp_path: Path, selected: list[int], presentation: int
) -> None:
    """Replacing a source id in the final table breaks the binding to the presented inventory."""
    metadata = _confirmed_selection(tmp_path, selected, presentation)
    metadata["final_resolution_table"]["items"][0]["sources"][0]["source_id"] = "result.json#substituted"

    with pytest.raises(SystemExit, match="selection-final-inventory-mismatch"):
        VALIDATOR._validate_code_remediate_scope_selection(metadata, tmp_path)


def test_pending_selection_cannot_pass_final_validation(tmp_path: Path) -> None:
    """A valid pending preview is not authorization to remediate.

    The prompt was presented but the user has not confirmed any selection; final validation must demand confirmation.
    """
    _write_selection_files(tmp_path, _selection())
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
    """A manually altered selection display fails the read-only CLI check and is left untouched.

    The real finalizer ``--check`` run compares the on-disk display to a fresh render; a hand-edited file must be
    reported as a render mismatch with exit code 2 and not be overwritten.
    """
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(_selection()), encoding="utf-8")
    output = tmp_path / "resolution-scope.md"
    output.write_text("Wrong finding", encoding="utf-8")

    completed = _run_selection_cli(path, output, "--check")

    assert completed.returncode == 2
    assert "selection-render-mismatch" in completed.stderr
    assert output.read_text() == "Wrong finding"


@pytest.mark.parametrize("width", [80, 120])
def test_selection_preview_retains_names_and_references(width: int) -> None:
    """Short overview and separate evidence groups survive common terminal widths.

    The selection Markdown is rendered through rich at 80 and 120 columns; names, done-when text and source ids must
    still be visible, no line may overflow, and internal source tags must not leak.
    """
    output = StringIO()
    console = Console(file=output, width=width, force_terminal=False, color_system=None)

    console.print(Markdown(_load_finalizer().render_selection(_selection())))

    text = output.getvalue()
    assert all(len(line) <= width for line in text.splitlines())
    assert "F7" in text
    assert "Preserve input compatibility" in text
    assert "Done when:" in text
    assert "result.json#F7" in text
    assert "comment-9" in text
    assert "[S1]" not in text
    assert "[C1]" not in text


def test_report_alias_mentions_share_one_canonical_owner() -> None:
    """Markdown and JSON views of the same finding are rejected as separate sources.

    The second item re-mentions F7 through ``review-notes.md:62`` of the same report; it must collapse onto the
    canonical owner and be flagged as a duplicate rather than listed as new work.
    """
    payload = _selection()
    repeated = copy.deepcopy(payload["items"][0]["sources"][0])
    repeated["source_id"] = "review-notes.md:62"
    repeated["report_id"] = "result.json"
    payload["items"][1]["sources"] = [repeated]

    with pytest.raises(ValueError, match="selection-canonical-finding-duplicate"):
        _load_finalizer().render_selection(payload)


def test_distinct_report_files_can_reuse_finding_ids() -> None:
    """Finding IDs are scoped to a report, not to every report in its directory."""
    payload = _selection()
    payload["items"][0]["sources"][0]["source_id"] = "first.json#F7"
    payload["items"][1]["sources"] = copy.deepcopy(payload["items"][0]["sources"])
    payload["items"][1]["sources"][0]["source_id"] = "second.json#F7"

    rendered = _load_finalizer().render_selection(payload)

    assert "report [first.json#F7]" in rendered
    assert "report [second.json#F7]" in rendered


def test_selection_cli_never_overwrites_its_input(tmp_path: Path) -> None:
    """A mistaken output path must not destroy the canonical selection inventory.

    The real finalizer command is asked to write its display onto the inventory file itself; it must exit 2 and leave
    the original bytes intact.
    """
    inventory = tmp_path / "selection.json"
    original = json.dumps(_selection()).encode("utf-8")
    inventory.write_bytes(original)

    completed = _run_selection_cli(inventory, inventory)

    assert completed.returncode == 2
    assert inventory.read_bytes() == original


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks")
def test_selection_cli_rejects_output_symlinks(tmp_path: Path) -> None:
    """The output link is not followed even when it stays within the run directory.

    ``resolution-scope.md`` is a symlink to an unrelated file; the real command must exit 2 and leave that file alone.
    """
    inventory = tmp_path / "selection.json"
    inventory.write_bytes(json.dumps(_selection()).encode("utf-8"))
    target = tmp_path / "unrelated.md"
    target.write_bytes(b"preserve unrelated evidence")
    output = tmp_path / "resolution-scope.md"
    output.symlink_to(target)

    completed = _run_selection_cli(inventory, output)

    assert completed.returncode == 2
    assert target.read_bytes() == b"preserve unrelated evidence"


@pytest.mark.parametrize("declared_count", [0, 2])
def test_grouped_intake_rejects_miscounted_gate_items(tmp_path: Path, declared_count: int) -> None:
    """A display-word bypass cannot hide or invent canonical report gate obligations.

    The final table holds one review-gate item; declaring zero or two gate items in the intake counts must be rejected
    as an inventory mismatch.
    """
    item = _selection()["items"][0]
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
    """New producers cannot silently fall back to bare historical finding records.

    Review metadata that marks canonical records but lists only id and severity must be rejected as missing details.
    """
    metadata = _metadata()
    metadata["review_findings"] = [{"id": "R1", "severity": "high"}, {"id": "R2", "severity": "medium"}]

    with pytest.raises(SystemExit, match="review-finding-canonical-details-missing"):
        _load_validator()._validate_review_decision(metadata, _result())


def test_empty_inventory_never_claims_user_confirmation() -> None:
    """An empty work list is neither a pending prompt nor a user selection."""
    rendered = _load_finalizer().render_selection({"schema_version": 1, "selected_indexes": [], "items": []})

    assert "none-selectable" in rendered
    assert "Confirmed selected" not in rendered
    assert "Awaiting selection" not in rendered


def test_grouped_review_gate_intake_does_not_depend_on_display_words(tmp_path: Path) -> None:
    """Canonical gate inventory coverage must not require an incidental phrase in the item title.

    The only item is a review-gate named "Independent verification" with no gate wording in the ledger; both the
    selection and intake validators must still accept it from the typed fields alone.
    """
    payload = _selection()
    payload["items"] = [payload["items"][0]]
    payload["items"][0].update(item_name="Independent verification", item_type="review-gate")
    payload["selected_indexes"] = [1]
    _write_selection_files(tmp_path, payload)
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


def _write_remediation_producer(tmp_path: Path, items: list[dict]) -> Path:
    """Build a complete producer whose canonical findings match the consumer's retained source bodies."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result_path = run / "result.json"
    result = json.loads(result_path.read_bytes())
    metadata = result["metadata"]
    records = []
    for item in items:
        source = next(source for source in item["sources"] if source["kind"] == "report")
        record = {
            "id": item["input_item_id"],
            "severity": item["severity"],
            "title": item["item_name"],
            "summary": source["body"],
            "required_change": f"Resolve {item['item_name']}.",
            "evidence": ["widget.txt:1"],
            "closure_evidence": "The closure regression passes.",
            "authors": ["Main reviewer"],
        }
        records.append(record)
        source["finding_id"] = record["id"]
        source["body"] = "\n".join(
            [
                record["title"],
                record["summary"],
                record["required_change"],
                *record["evidence"],
                record["closure_evidence"],
                *metadata["confidence_gaps"],
                *metadata["confidence_recovery"]["remaining_limits"],
            ]
        )
    metadata["review_findings"] = records
    metadata["review_decision"].update(recommendation="needs-more-work", summary="Resolve the retained findings.")
    metadata["review_validation"].update(
        source_verdict="needs-more-work",
        candidates=[
            {
                "id": record["id"],
                "severity": record["severity"],
                "claim": record["summary"],
                "source_revision": "head:" + json.loads((run / "pr-routing.json").read_bytes())["head_oid"],
                "contract": record["closure_evidence"],
                "trigger": record["summary"],
                "confirmation_evidence": "; ".join(record["evidence"]),
                "falsification_evidence": "Inspect the source for an existing boundary guard.",
                "counterevidence": [],
                "check": "Inspected the frozen source boundary and its callers.",
                "result": record["summary"],
                "environment": "Frozen source inspection",
                "limits": [],
                "disposition": "confirmed",
                "reason": record["required_change"],
                "feasible_checks_complete": True,
                "missing_prerequisite": "",
                "owner": "review owner",
                "next_check": record["required_change"],
                "closure_condition": record["closure_evidence"],
            }
            for record in records
        ],
    )
    metadata["reviewer_assessments"][0]["rating"] = 3
    result["findings"] = {
        severity: sum(record["severity"] == severity for record in records) for severity in result["findings"]
    }
    notes_path = run / "review-notes.md"
    notes = notes_path.read_text(encoding="utf-8").replace("Rating: 1", "Rating: 3")
    notes += "\n\n## Review Findings and Merge Blocks\n\n"
    notes += "| Finding / area | Author | Required change | Evidence | Status |\n| --- | --- | --- | --- | --- |\n"
    notes += "\n".join(
        f"| {record['id']} | Main reviewer | {record['required_change']} | widget.txt:1 | Required |"
        for record in records
    )
    notes_path.write_text(notes, encoding="utf-8")
    handoff_path = run / "final-handoff.json"
    handoff = json.loads(handoff_path.read_bytes())
    handoff["outcome"]["summary"] = "Recommendation: needs-more-work."
    handoff["tables"][0]["summary"] = metadata["review_decision"]["summary"]
    handoff["tables"][0]["reviewers"] = metadata["reviewer_assessments"]
    handoff["tables"][0]["rows"][-1]["cells"][1] = "major revision"
    handoff["tables"].append(
        {
            "heading": "Review Findings and Merge Blocks",
            "layout": "concise",
            "overview_only": True,
            "columns": ["Finding / area", "Required change", "Evidence", "Status"],
            "rows": [
                {
                    "id": record["id"],
                    "title": record["title"],
                    "summary": record["summary"],
                    "closure_evidence": record["closure_evidence"],
                    "authors": record["authors"],
                    "cells": [record["id"], record["required_change"], "widget.txt:1", "Required"],
                    "source_ids": [record["id"]],
                }
                for record in records
            ],
        }
    )
    handoff["source_records"].extend({"id": record["id"], "evidence": "review-notes.md"} for record in records)
    handoff["source_coverage"] = {
        "source_records_total": len(handoff["source_records"]),
        "represented_source_records_total": len(handoff["source_records"]),
        "omitted_source_records_total": 0,
    }
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    finalizer = _load_finalizer()
    with notes_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n\n" + finalizer.render_review_report_details(handoff))
    _bind_final_handoff(handoff_path, metadata)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    admitted = subprocess.run(
        [sys.executable, str(FIND_REPORT), "--result", str(result_path)], capture_output=True, text=True, check=False
    )
    assert admitted.returncode == 0, admitted.stderr
    return result_path


def _write_remediation_candidate(
    tmp_path: Path, item_type: str, selected_resolution: tuple[str, str] | None = None
) -> Path:
    """Write a complete report candidate, optionally selecting one explicit outcome for closure checks."""
    result_path = write_schema_v2_assess(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    metadata.update(resolution_metadata())
    table = metadata["final_resolution_table"]
    for item in table["items"]:
        item.update(
            item_type=item_type, selectable=False, triage_status="already-fixed", resolution_status="already-fixed"
        )
    producer_root = tmp_path / "review-producer"
    producer_root.mkdir()
    producer = _write_remediation_producer(producer_root, table["items"])
    (tmp_path / "findings-input.txt").write_bytes(producer.read_bytes())
    table["items"][0]["sources"].extend(VALIDATOR.report_obligation_sources(json.loads(producer.read_bytes())))
    source_total = sum(len(item["sources"]) for item in table["items"])
    table.update(
        source_records_total=source_total,
        represented_source_records_total=source_total,
        grouped_items_total=sum(len(item["sources"]) > 1 for item in table["items"]),
    )
    if selected_resolution is not None:
        table["items"][0].update(
            selectable=True,
            triage_status="valid",
            resolution_status=selected_resolution[0],
            resolved_how=selected_resolution[1],
        )
    for field, key in (("triage_status_counts", "triage_status"), ("resolution_status_counts", "resolution_status")):
        table[field] = {status: sum(item[key] == status for item in table["items"]) for status in table[field]}
    selected_indexes = [1] if selected_resolution is not None else []
    table.update(selectable_rows_total=len(selected_indexes), nonselectable_rows_total=2 - len(selected_indexes))
    inventory = {"schema_version": 1, "selected_indexes": selected_indexes, "items": copy.deepcopy(table["items"])}
    for item in inventory["items"]:
        item.update(summary="The original issue is already closed.", closure_evidence="Existing regression passes.")
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(inventory).encode("utf-8"))
    metadata.update(
        mode="report",
        resolution_scope={
            "presentation_version": 2,
            "selection_source": "explicit-input" if selected_indexes else "none-selectable",
            "prompt_presented": False,
            "selection_confirmed_by_user": bool(selected_indexes),
            "selected_indexes": selected_indexes,
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
            "obligation_records_version": 1,
            "admission_evidence": {"producer_result_path": str(producer)},
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
    if selected_indexes:
        metadata["resolution_workplan"].update(
            parallel_approval_status="parent-only",
            parallel_approval_source="workflow-default",
            groups_total=1,
            parent_owned_groups=1,
            verifier_groups=1,
            work_buckets=[
                {
                    "bucket_id": "B1",
                    "selected_indexes": selected_indexes,
                    "owner": "parent",
                    "verifier": "qa-specialist",
                    "context_pack_path": "resolution-workplan.md",
                    "owned_paths": ["src/guard.py"],
                    "execution_mode": "parent",
                }
            ],
        )
        _write_workplan(metadata, tmp_path)
        workplan_path = tmp_path / "resolution-workplan.md"
        workplan_path.write_text(
            workplan_path.read_text(encoding="utf-8").replace(
                "## Parallel Approval\n",
                "## Parallel Approval\n\nIneligibility reason: One selected closure item forms one coherent bucket.\n",
            ),
            encoding="utf-8",
        )
    _write_action_items(metadata, tmp_path)
    with (tmp_path / "action-items.md").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## Review Report Intake\n\nTwo report items already closed.\n")
    (tmp_path / "closure-log.md").write_text("## Closure Evidence\n\nExisting tests pass.\n", encoding="utf-8")
    (tmp_path / "unresolved.txt").write_text("None\n", encoding="utf-8")
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff.update(skill="code-remediate", outcome={"title": "Remediation Summary", "summary": "Already closed."})
    handoff["commit_disposition"] = {
        "status": "not-applicable",
        "reason": "All findings were already closed; no remediation-owned change exists.",
        "evidence": "closure-log.md",
    }
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
                    f"{item['resolution_status']} — [O{position}]",
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
                "layout": "concise",
                "overview_only": True,
                "columns": ["Item", "Severity", "Finding", "Sources", "Outcome", "Evidence / next action"],
                "rows": rows,
                "details": details,
            }
        ],
        source_records=sources,
        source_coverage={
            "source_records_total": source_total,
            "represented_source_records_total": source_total,
            "omitted_source_records_total": 0,
        },
    )
    for position, row in enumerate(rows, 1):
        item = table["items"][position - 1]
        row["selected"] = "Closed" if not item["selectable"] else "Yes"
    handoff["artifacts"].append({"label": "Item ledger", "path": str(tmp_path / "action-items.md")})
    handoff["remaining"] = [
        {
            "row_id": item["input_item_id"],
            "item": item["item_name"],
            "owner": item["owner_status"],
            "next_action": item["resolved_how"],
        }
        for item in table["items"]
        if item["resolution_status"] in {"unresolved", "deferred", "blocked", "needs-clarification"}
    ]
    handoff["next_steps"] = [item["row_id"] for item in handoff["remaining"]]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    _bind_final_handoff(handoff_path, metadata)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    return result_path


def _record_commit_disposition(
    tmp_path: Path, result_path: Path, result: dict, status: str, reason: str, evidence: str = "commit-plan.md"
) -> None:
    """Rewrite the handoff's commit disposition, rebind its digests and persist ``result``."""
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["commit_disposition"] = {"status": status, "reason": reason, "evidence": evidence}
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    _bind_final_handoff(handoff_path, result["metadata"])
    result_path.write_text(json.dumps(result), encoding="utf-8")


def test_remediation_rejects_summary_that_drops_selected_blocked_item(tmp_path: Path) -> None:
    """A no-commit handoff cannot hide an open selected finding behind zero totals.

    One selected code item is recorded as blocked, but the unresolved summary still reports zero selected items.
    """
    result_path = _write_remediation_candidate(
        tmp_path, "code", ("unresolved", "Blocked: Original failure still reproduces.")
    )

    with pytest.raises(SystemExit, match="code-remediate-unresolved-selected-count-mismatch"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


def test_remediation_cannot_pass_with_accurately_reported_required_work(tmp_path: Path) -> None:
    """Passing gates stay separate from a selected code defect that remains blocked.

    The summary now truthfully reports the open local item, yet the result still claims a pass; validation must refuse
    the pass because required local work is unresolved.
    """
    result_path = _write_remediation_candidate(
        tmp_path, "code", ("unresolved", "Blocked: Original failure still reproduces.")
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["unresolved_summary"].update(
        selected_items_total=1,
        selected_items_unresolved=1,
        local_actionable_items_unresolved=1,
        all_local_actionable_items_closed=False,
        unresolved_reason_groups=[
            {
                "reason": "local-code-or-doc",
                "count": 1,
                "owner": "codex",
                "next_action": "Fix the original failing input.",
                "evidence_path": "closure-log.md",
            }
        ],
    )
    result_path.write_text(json.dumps(result), encoding="utf-8")
    (tmp_path / "unresolved.txt").write_text(
        "## Unresolved Work Summary\n\nOne required item remains.\n\n"
        "## Why Selected Items Remain Unresolved\n\nClosure class: local-code-or-doc. "
        "Next owner: codex. Attempted evidence: closure-log.md.\n\n"
        "## Next Action\n\nFix the original failing input.\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="code-remediate-pass-with-required-unresolved-work"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


def test_remediation_intake_cannot_omit_a_canonical_report_finding(tmp_path: Path) -> None:
    """Internally consistent intake counts cannot omit a finding from the copied report.

    The retained report lists finding ``F-missing`` that no remediation row covers, so the omission must be named.
    """
    result_path = _write_remediation_candidate(tmp_path, "code")
    review = {
        "schema_version": 3,
        "artifact_path": "review/result.json",
        "checks_failed": [],
        "follow_up": [],
        "metadata": {"review_findings": [{"id": "F-missing", "severity": "high"}]},
    }
    (tmp_path / "findings-input.txt").write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(SystemExit, match="code-remediate-report-finding-omitted:F-missing"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


OBLIGATION = "Independent numerical equivalence evidence remains required."


@pytest.mark.parametrize(
    ("top_level", "metadata_fields"),
    [
        pytest.param({"checks_failed": [OBLIGATION]}, {}, id="checks_failed"),
        pytest.param({"follow_up": [OBLIGATION]}, {}, id="follow_up"),
        pytest.param({}, {"confidence_gaps": [OBLIGATION]}, id="confidence_gaps"),
        pytest.param({}, {"review_decision": {"required_next_work": [OBLIGATION]}}, id="required_next_work"),
        pytest.param({}, {"confidence_recovery": {"remaining_limits": [OBLIGATION]}}, id="remaining_limits"),
    ],
)
def test_remediation_intake_cannot_drop_report_evidence_obligations(
    tmp_path: Path, top_level: dict, metadata_fields: dict
) -> None:
    """Non-code closure obligations are kept when the original report is explicitly requested.

    The retained report carries one extra obligation in a single field (failed checks, follow-up, confidence gap,
    required next work or remaining limit) that no remediation row covers; each field must be flagged when omitted.
    """
    result_path = _write_remediation_candidate(tmp_path, "code")
    review = {
        "schema_version": 3,
        "artifact_path": "review/result.json",
        "checks_failed": [],
        "follow_up": [],
        **top_level,
        "metadata": {"review_findings": [], "confidence_gaps": [], "review_decision": {}, **metadata_fields},
    }
    (tmp_path / "findings-input.txt").write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(SystemExit, match="code-remediate-report-obligation-omitted"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


def test_remediation_report_coverage_preserves_grouped_obligations(tmp_path: Path) -> None:
    """One canonical item may retain repeated review and confidence obligations losslessly.

    The report repeats the first finding's body in failed checks, follow-up, gaps, limits and next work, and maps an
    operational blocker onto the second item; coverage validation must accept this grouping.
    """
    result_path = _write_remediation_candidate(tmp_path, "code")
    metadata = json.loads(result_path.read_text(encoding="utf-8"))["metadata"]
    report = json.loads((tmp_path / "findings-input.txt").read_text(encoding="utf-8"))
    body = metadata["final_resolution_table"]["items"][0]["sources"][0]["body"]
    report.update(checks_failed=[body], follow_up=[body])
    report["metadata"].update(
        operational_blockers=[
            {"id": "R2", "required_change": metadata["final_resolution_table"]["items"][1]["sources"][0]["body"]}
        ],
        confidence_gaps=[body],
        confidence_recovery={"remaining_limits": [body]},
        review_decision={"required_next_work": [body]},
    )
    (tmp_path / "findings-input.txt").write_text(json.dumps(report), encoding="utf-8")

    VALIDATOR._validate_code_remediate_report_coverage(metadata, tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("closure_evidence", "Compare both actual downstream outcomes.", id="closure_evidence"),
        pytest.param("title", "The downstream state remains invalid after the reported success.", id="title"),
        pytest.param("evidence", ["The downstream state remains invalid after the reported success."], id="evidence"),
    ],
)
def test_remediation_report_keeps_original_finding_details(tmp_path: Path, field: str, value: object) -> None:
    """Retain the original finding's label, evidence, and closure contract, not just its intake ID.

    A remediation result may keep a finding's ID in the intake ledger while dropping the detail a reader needs: the
    closure evidence that proves it fixed, the title that names it, or the evidence that located it. Each case changes
    one original detail so the remediation summary no longer reproduces it, and coverage validation must name it.
    """
    result_path = _write_remediation_candidate(tmp_path, "code")
    metadata = json.loads(result_path.read_text(encoding="utf-8"))["metadata"]
    path = tmp_path / "findings-input.txt"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["metadata"]["review_findings"][0][field] = value
    path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(SystemExit, match=f"code-remediate-report-finding-detail-omitted:R1:{field}"):
        VALIDATOR._validate_code_remediate_report_coverage(metadata, tmp_path)


def test_remediation_requested_report_requires_retained_input(tmp_path: Path) -> None:
    """Report-backed remediation cannot complete after its original input disappeared."""
    result_path = _write_remediation_candidate(tmp_path, "code")
    (tmp_path / "findings-input.txt").unlink()

    with pytest.raises(SystemExit, match="code-remediate-report-input-missing"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


ENVIRONMENT_GATE = {
    "blocked_how": "Blocked: complete docs environment unavailable.",
    "counters": {"environment_blocked_items": 1},
    "reason": "environment-blocked",
    "owner": "environment",
    "next_action": "Run the docs build in the complete environment.",
    "headline": "One environment blocker.",
    "attempted": "docs build failed",
    "closing": "Run the docs build.",
    "remaining": "Docs build blocked by the incomplete environment.",
    "disposition": "User requested a local commit with the docs-build environment limit disclosed.",
}
REVIEW_GATE = {
    "blocked_how": "Blocked: independent reviewer has not inspected the change.",
    "counters": {"process_gate_items_unresolved": 1, "external_owner_items": 1},
    "reason": "independent-review",
    "owner": "external-reviewer",
    "next_action": "Review the local change independently.",
    "headline": "Independent review is open.",
    "attempted": "no independent review receipt",
    "closing": "Review the local change independently.",
    "remaining": "Independent review remains open with the external reviewer.",
    "disposition": "User requested a local commit with independent review disclosed as open.",
}
ENVIRONMENT_INTAKE = {"review_gate_items_selectable": 1}
REVIEW_INTAKE = {"review_gate_items_selectable": 1, "review_gate_items_total": 2}


def _stage_explicit_commit(tmp_path: Path, item_type: str, gate: dict, intake: dict) -> tuple[Path, dict]:
    """Stage a failed remediation whose one selected item is open behind ``gate`` and whose commit is pending.

    The user explicitly asked for a local commit; the unresolved summary, unresolved work file and commit plan all
    describe the same open gate. ``intake`` adjusts the review-report intake counts for gate-typed items.
    """
    result_path = _write_remediation_candidate(tmp_path, item_type, ("unresolved", gate["blocked_how"]))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["status"] = "fail"
    metadata = result["metadata"]
    metadata["review_report_intake"].update(intake)
    metadata["unresolved_summary"].update(
        selected_items_total=1,
        selected_items_unresolved=1,
        **gate["counters"],
        unresolved_reason_groups=[
            {
                "reason": gate["reason"],
                "count": 1,
                "owner": gate["owner"],
                "next_action": gate["next_action"],
                "evidence_path": "unresolved.txt",
            }
        ],
    )
    (tmp_path / "unresolved.txt").write_text(
        f"## Unresolved Work Summary\n\n{gate['headline']}\n\n"
        "## Why Selected Items Remain Unresolved\n\n"
        "| Closure class | Next owner | Attempted evidence |\n"
        "| --- | --- | --- |\n"
        f"| {gate['reason']} | {gate['owner']} | {gate['attempted']} |\n\n"
        f"## Next Action\n\n{gate['closing']}\n",
        encoding="utf-8",
    )
    (tmp_path / "commit-plan.md").write_text(
        "## Explicit Commit Request\n\nUser requested one local commit for the verified source paths.\n\n"
        f"## Remaining Verification\n\n{gate['remaining']}\n",
        encoding="utf-8",
    )
    _record_commit_disposition(tmp_path, result_path, result, "pending", gate["disposition"])
    return result_path, result


def test_explicit_commit_accepts_open_environment_gate(tmp_path: Path) -> None:
    """An explicit commit may keep a failed result when only environment verification is missing.

    The open item is a review-gate typed environment limit. Validation accepts it, but the same commit plan with its
    required sections emptied must then be rejected so the limit stays disclosed.
    """
    result_path, _ = _stage_explicit_commit(tmp_path, "review-gate", ENVIRONMENT_GATE, ENVIRONMENT_INTAKE)

    VALIDATOR.validate("code-remediate", tmp_path, result_path)

    (tmp_path / "commit-plan.md").write_text(
        "## Explicit Commit Request\n\n## Remaining Verification\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="remediation-commit-plan-section-empty"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


def test_explicit_commit_accepts_open_independent_review_gate(tmp_path: Path) -> None:
    """An explicit commit may keep a failed result when only an independent review gate is open."""
    result_path, _ = _stage_explicit_commit(tmp_path, "confidence-gap", REVIEW_GATE, REVIEW_INTAKE)

    VALIDATOR.validate("code-remediate", tmp_path, result_path)


@pytest.mark.parametrize(
    "gate",
    [
        pytest.param(ENVIRONMENT_GATE, id="mislabeled-as-environment"),
        pytest.param(REVIEW_GATE, id="mislabeled-as-review"),
    ],
)
def test_explicit_commit_rejects_code_defect_labeled_as_gate(tmp_path: Path, gate: dict) -> None:
    """An open code finding cannot ride an explicit commit by borrowing a gate's unresolved summary.

    The item type is plain code, so the same environment or review wording that is acceptable for a gate item must still
    block the commit.
    """
    result_path, _ = _stage_explicit_commit(tmp_path, "code", gate, {})

    with pytest.raises(SystemExit, match="remediation-commit-result-blocked"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


def _write_pr_run_artifacts(pr_dir: Path, base_oid: str, head_oid: str) -> None:
    """Write a consistent, clean, conflict-free set of collected PR artifacts into ``pr_dir``."""
    remote_url = "https://github.com/acme/widgets.git"
    files = {
        "pr.json": {"body": "Fixes the widget.", "baseRefOid": base_oid, "headRefOid": head_oid},
        "pr-routing.json": {
            "base_identity_source": "pr_url",
            "pr_state": "OPEN",
            "base_host": "github.com",
            "base_repo": "acme/widgets",
            "pr_number": 123,
            "pr_url": PR_URL,
            "local_checkout_required": True,
            "local_checkout_command": f"git checkout --detach {head_oid}",
            "force_policy": "forbidden",
            "base_oid": base_oid,
            "head_oid": head_oid,
        },
        "remote-selection.json": {
            "expected": {"host": "github.com", "repository": "acme/widgets"},
            "remote": "origin",
            "remote_url": remote_url,
        },
        "target-branch.json": {
            "status": "fetched",
            "remote": "origin",
            "remote_url": remote_url,
            "remote_ref": base_oid,
            "expected_base_oid": base_oid,
            "local_head": base_oid,
            "expected_base_is_ancestor": True,
            "base_matches_pr_metadata": True,
            "base_relation": "matches-pr-metadata",
        },
        "pr-head-fetch.json": {
            "status": "fetched",
            "remote_ref": "FETCH_HEAD",
            "local_head": head_oid,
            "expected_head_oid": head_oid,
            "head_matches_pr_metadata": True,
        },
        "local-checkout.json": {
            "status": "checked-out",
            "pr_url": PR_URL,
            "command": f"git checkout --detach {head_oid}",
            "force_policy": "forbidden",
            "head_matches_pr": True,
            "expected_head": head_oid,
            "local_head": head_oid,
            "diff_source": "verified-local-checkout",
            "diff_base_oid": base_oid,
            "diff_head_oid": head_oid,
            "diff_command": f"git diff --binary {base_oid}...{head_oid} --",
        },
        "worktree-preflight.json": {
            "phase": "after-checkout",
            "status": "clean",
            "current_head": head_oid,
            "expected_head": head_oid,
            "dirty_paths": [],
            "unmerged_paths": [],
            "pr_paths": ["widget.py"],
            "checkout_paths": [],
            "overlapping_paths": [],
            "overlapping_pr_paths": [],
        },
        "online-review-summary.json": {"review_threads_status": "available", "review_threads_error": None},
        "merge-resolution.json": {
            "schema_version": 1,
            "conflicts_detected": False,
            "status": "not-needed",
            "authorization": "not-required",
            "base_remote_ref": "origin/main",
            "target_oid": base_oid,
            "pre_merge_head": head_oid,
            "post_merge_head": head_oid,
            "merge_commit": None,
            "resolved_paths": [],
            "unmerged_paths": [],
            "evidence": ["merge-tree.txt"],
        },
        "comments.json": [],
        "reviews.json": [],
        "review-threads.json": [],
        "unresolved-review-threads.json": [],
    }
    for name, payload in files.items():
        (pr_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    (pr_dir / "merge-base.txt").write_text(f"{base_oid}\n", encoding="utf-8")
    (pr_dir / "merge-tree.txt").write_text("clean\n", encoding="utf-8")


@pytest.mark.parametrize("item_type", ["code", "review-gate"])
def test_all_closed_selection_passes_complete_artifact_validation(tmp_path: Path, item_type: str) -> None:
    """The outer validator accepts an all-closed selection, as a report and as a PR run, and checks merge heads.

    Beyond the no-selectable renderer branch, the full artifact set is validated twice: as a report candidate and again
    after converting it to a clean PR run. Corrupting the recorded pre-merge head must then be rejected, which shows the
    PR validation really reads the collected merge artifacts.
    """
    result_path = _write_remediation_candidate(tmp_path, item_type)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    inventory = json.loads((tmp_path / "selection.json").read_text(encoding="utf-8"))
    VALIDATOR.validate("code-remediate", tmp_path, result_path)

    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    metadata.update(
        mode="pr",
        pr_relevance={
            "evaluated": True,
            "connected_items_marked_out_of_scope": 0,
            "connected_open_items_total": 0,
            "connected_selectable_items_total": 0,
            "connected_required_followup_total": 0,
        },
        merge_resolution={
            "artifact_path": str(pr_dir / "merge-resolution.json"),
            "authorization": "not-required",
            "conflicts_detected": False,
            "status": "not-needed",
        },
    )
    inventory["pr_relevance"] = metadata["pr_relevance"]
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8")
    (tmp_path / "resolution-scope.md").write_bytes(_load_finalizer().render_selection(inventory).encode("utf-8"))
    with (tmp_path / "action-items.md").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n## PR Relevance Summary\n\nNo open selected items are PR-connected.\n")
    _write_pr_run_artifacts(pr_dir, "a" * 40, "b" * 40)
    (tmp_path / "merge-prestage.md").write_text(
        "\n".join(
            f"## {heading}\n\nRecorded evidence."
            for heading in (
                "PR And Target Refresh",
                "Clean PR Implementation Context",
                "Target Branch Context",
                "Conflict Risk",
                "Resolution Strategy",
                "Merge Execution",
            )
        ),
        encoding="utf-8",
    )
    result_path.write_text(json.dumps(result), encoding="utf-8")
    VALIDATOR.validate("code-remediate", tmp_path, result_path)

    merge_resolution_path = pr_dir / "merge-resolution.json"
    merge_resolution = json.loads(merge_resolution_path.read_text(encoding="utf-8"))
    merge_resolution["pre_merge_head"] = "c" * 40
    merge_resolution_path.write_text(json.dumps(merge_resolution), encoding="utf-8")
    with pytest.raises(SystemExit, match="code-remediate-merge-resolution-pre-merge-head-mismatch"):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


BLOCKED = ("unresolved", "Blocked: Required regression remains.")
DEFERRED = ("unresolved", "Deferred: User postponed this excluded path.")
CLARIFICATION = ("needs-clarification", "Deferred: No matching user decision.")
IMPLEMENTED = ("implemented", "Implemented: Required regression passes.")
PLAN = "Verified local commit plan; user-deferred work excluded.\n"
OPEN_WORK = (
    "## Unresolved Work Summary\n\nOne required item remains.\n\n"
    "## Why Selected Items Remain Unresolved\n\nClosure class: local-code-or-doc. "
    "Next owner: codex. Attempted evidence: closure-log.md.\n\n"
    "## Next Action\n\nComplete the required regression.\n"
)
PLAN_ONLY = {"commit-plan.md": PLAN}
PLAN_AND_OPEN_WORK = {**PLAN_ONLY, "unresolved.txt": OPEN_WORK}
COMMIT_STATUSES = pytest.mark.parametrize("status", ["pending", "committed"])


def _open_summary(*, local: int, deferred: bool) -> dict:
    """Unresolved-summary fields for one selected item left open, locally actionable or user-deferred."""
    reason, owner = ("user-deferred", "user") if deferred else ("local-code-or-doc", "codex")
    return {
        "selected_items_total": 1,
        "selected_items_unresolved": 1,
        "local_actionable_items_unresolved": local,
        "all_local_actionable_items_closed": local == 0,
        "user_deferred_items": int(deferred),
        "unresolved_reason_groups": [
            {
                "reason": reason,
                "count": 1,
                "owner": owner,
                "next_action": "Complete the required regression.",
                "evidence_path": "closure-log.md",
            }
        ],
    }


def _stage_commit_candidate(
    tmp_path: Path,
    status: str,
    resolution: tuple[str, str] | None,
    summary: dict,
    files: dict,
    links: dict,
    evidence: str,
) -> Path:
    """Stage a complete code candidate whose commit disposition cites ``evidence`` with the given state of work.

    ``summary`` is merged into the unresolved summary, ``files`` (name to text) and ``links`` (name to target) are
    created in the run directory, and ``{tmp}`` in ``evidence`` expands to the run directory's name.
    """
    result_path = _write_remediation_candidate(tmp_path, "code", resolution)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["unresolved_summary"].update(summary)
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    for name, target in links.items():
        (tmp_path / name).symlink_to(tmp_path / target)
    _record_commit_disposition(
        tmp_path, result_path, result, status, "Recorded commit state.", evidence.format(tmp=tmp_path.name)
    )
    return result_path


@COMMIT_STATUSES
@pytest.mark.parametrize(
    ("resolution", "summary", "files", "links", "evidence"),
    [
        pytest.param(None, {}, PLAN_ONLY, {}, "commit-plan.md", id="closed-with-plan"),
        pytest.param(
            DEFERRED,
            _open_summary(local=0, deferred=True),
            PLAN_AND_OPEN_WORK,
            {},
            "commit-plan.md",
            id="explicit-deferment-outside-plan",
        ),
        pytest.param(
            IMPLEMENTED,
            {"selected_items_total": 1, "selected_items_resolved": 1},
            PLAN_ONLY,
            {},
            "commit-plan.md",
            id="resolved-selected-row",
        ),
        pytest.param(
            None,
            {},
            {**PLAN_ONLY, "decision.md": "Matching user decision for the recorded plan.\n"},
            {},
            "decision.md",
            id="separate-decision-record",
        ),
    ],
)
def test_commit_readiness_accepts_closed_candidate_with_plan(
    tmp_path: Path,
    status: str,
    resolution: tuple[str, str] | None,
    summary: dict,
    files: dict,
    links: dict,
    evidence: str,
) -> None:
    """Commit state is accepted when the candidate is closed or explicitly deferred and cites a valid plan.

    Closed work, an explicitly user-deferred item outside the plan, a resolved selected row and a separate decision
    record all leave nothing unfinished, so both a pending and a committed disposition validate.
    """
    result_path = _stage_commit_candidate(tmp_path, status, resolution, summary, files, links, evidence)

    VALIDATOR.validate("code-remediate", tmp_path, result_path)


@COMMIT_STATUSES
@pytest.mark.parametrize(
    ("resolution", "summary", "files", "links", "evidence", "error"),
    [
        pytest.param(None, {}, {}, {}, "commit-plan.md", "remediation-commit-plan-missing", id="missing-plan"),
        pytest.param(
            BLOCKED,
            _open_summary(local=1, deferred=False),
            PLAN_AND_OPEN_WORK,
            {},
            "commit-plan.md",
            "remediation-commit-closure-blocked",
            id="required-open",
        ),
        pytest.param(
            CLARIFICATION,
            _open_summary(local=0, deferred=True),
            PLAN_AND_OPEN_WORK,
            {},
            "commit-plan.md",
            "remediation-commit-closure-blocked",
            id="clarification-is-not-deferment",
        ),
        pytest.param(
            BLOCKED,
            {},
            PLAN_ONLY,
            {},
            "commit-plan.md",
            "code-remediate-unresolved-selected-count-mismatch",
            id="stale-zero-summary",
        ),
        pytest.param(
            BLOCKED,
            _open_summary(local=0, deferred=True),
            PLAN_AND_OPEN_WORK,
            {},
            "commit-plan.md",
            "remediation-commit-closure-blocked",
            id="blocked-row-masked-as-deferred",
        ),
        pytest.param(
            BLOCKED,
            _open_summary(local=1, deferred=True),
            PLAN_AND_OPEN_WORK,
            {},
            "commit-plan.md",
            "remediation-commit-closure-blocked",
            id="deferment-cannot-mask-required-count",
        ),
        pytest.param(
            None, {}, PLAN_ONLY, {}, "missing.md", "remediation-commit-evidence-invalid", id="missing-declared-evidence"
        ),
        pytest.param(
            None,
            {},
            PLAN_ONLY,
            {},
            "../{tmp}/commit-plan.md",
            "remediation-commit-evidence-invalid",
            id="escaped-evidence",
        ),
        pytest.param(
            None,
            {},
            PLAN_ONLY,
            {"linked.md": "commit-plan.md"},
            "linked.md",
            "remediation-commit-evidence-invalid",
            id="symlink-evidence",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"),
        ),
    ],
)
def test_commit_readiness_rejects_unfinished_work_or_invalid_plan(
    tmp_path: Path,
    status: str,
    resolution: tuple[str, str] | None,
    summary: dict,
    files: dict,
    links: dict,
    evidence: str,
    error: str,
) -> None:
    """Commit state is rejected when the candidate declares unfinished work or lacks a valid plan.

    Each case starts from a complete candidate and changes one thing: a missing plan, an open or masked required
    item, a stale zero summary, or evidence that is missing, escapes the run directory or is a symlink.
    """
    result_path = _stage_commit_candidate(tmp_path, status, resolution, summary, files, links, evidence)

    with pytest.raises(SystemExit, match=error):
        VALIDATOR.validate("code-remediate", tmp_path, result_path)


def _grouped_review_handoff(layout: str) -> tuple[dict, dict, dict, list[dict]]:
    """Return (metadata, result, payload, rows) for a failing working-tree review rendered in ``layout``."""
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
    payload = handoff_payload()
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
        row.update({key: record[key] for key in ("summary", "closure_evidence") if key in record})
        rows.append(row)
    payload.update(
        skill="code-review",
        branch="assessed",
        outcome={"title": "Review Decision", "summary": "Recommendation: needs-more-work."},
        tables=[
            {
                "heading": "Review Findings and Merge Blocks",
                "layout": layout,
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
    return metadata, result, payload, rows


def _substitute(row: dict, path: tuple) -> None:
    """Replace the value at ``path`` inside ``row`` with text that matches no recorded finding."""
    *parents, last = path
    target = row
    for key in parents:
        target = target[key]
    target[last] = "Substituted content"


@pytest.mark.parametrize(
    ("layout", "expected_columns"),
    [
        pytest.param("grouped", "| ID | Finding | Status |", id="grouped"),
        pytest.param("concise", "| ID | Finding | Resolution proposal | Status |", id="concise"),
    ],
)
@pytest.mark.parametrize(
    "path",
    [
        pytest.param(("title",), id="title"),
        pytest.param(("summary",), id="summary"),
        pytest.param(("closure_evidence",), id="closure_evidence"),
        pytest.param(("cells", 1), id="action"),
        pytest.param(("cells", 2), id="evidence"),
    ],
)
def test_grouped_review_binds_every_display_field(layout: str, expected_columns: str, path: tuple) -> None:
    """Plausible but substituted display content is rejected without weakening stable ID coverage.

    A valid review (all IDs, severities and details recorded) validates and renders in both layouts; swapping any one
    displayed field of a row for different text must then be reported as a finding mismatch.
    """
    metadata, result, payload, rows = _grouped_review_handoff(layout)
    _load_validator()._validate_review_decision(metadata, result)
    VALIDATOR._validate_code_review_final_handoff(result, payload)

    rendered = _load_finalizer().render_handoff(payload)

    assert expected_columns in rendered
    assert "- Done when: Compatibility regression passes." in rendered
    _substitute(rows[0], path)
    with pytest.raises(SystemExit, match=r"code-review-final-handoff-finding-.*-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, payload)


def _compact_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, presentation: int, heading: str) -> tuple:
    """Build an assessed PR run with a findings table and its full report; return (run, payload, row, report, retained).

    The report file is read with a Windows code page whenever a caller omits the encoding, so a lossy default decode in
    the renderer would corrupt its non-ASCII text.
    """
    report_root = tmp_path / "review with spaces"
    report_root.mkdir()
    run = _assessed_pr.__wrapped__(report_root)
    payload = _load_handoff(run)
    payload["presentation_version"] = presentation
    payload["tables"][0]["heading"] = heading
    row = {
        "id": "boundary-check",
        "authors": ["Main reviewer"],
        "title": "Boundary regression unprotected",
        "summary": "Both selectors satisfy the current assertion.",
        "closure_evidence": "The regression fails with the wrong selector.",
        "cells": ["boundary-check", "Use inputs that distinguish selectors.", "tests/test_parser.py:42", "Required"],
        "source_ids": ["finding:boundary-check"],
    }
    payload["tables"].append(
        {
            "heading": "Review Findings and Merge Blocks",
            "layout": "concise",
            "overview_only": True,
            "columns": ["Finding / area", "Required change", "Evidence", "Status"],
            "rows": [row],
        }
    )
    payload["source_records"].append({"id": "finding:boundary-check", "evidence": "review-notes.md"})
    total = len(payload["source_records"])
    payload["source_coverage"].update(source_records_total=total, represented_source_records_total=total)
    report = run / "review-notes.md"
    original_open = Path.open

    def open_with_windows_default(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        """Exercise real report I/O with a Windows code page when encoding is omitted."""
        if path == report and "b" not in mode and encoding in {None, "locale"}:
            encoding = "cp1252"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_with_windows_default)
    with report.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "\n\n## Findings\n\n"
            + "\n\n".join((row["title"], row["summary"], *row["cells"][1:3], row["closure_evidence"]))
        )
        handle.write("\n\n" + _load_finalizer().render_review_report_details(payload))
    return run, payload, row, report, report.read_bytes()


def _assert_details_only_in_report(
    rendered: str, payload: dict, original: dict, row: dict, report: Path, retained: bytes
) -> None:
    """Chat output shows the finding table but none of the row's supporting detail, which stays in the report."""
    details = (row["title"], row["summary"], *row["cells"][1:3], row["closure_evidence"])
    report_text = report.read_text(encoding="utf-8")
    assert "| ID | Author | Finding | Resolution proposal | Status |" in rendered
    assert "| boundary-check | Main reviewer | Boundary regression unprotected |" in rendered
    assert "**boundary-check**" not in rendered
    assert not any(f"- {label}:" in rendered for label in ("Context", "Evidence", "Done when"))
    assert row["summary"] not in rendered
    assert row["closure_evidence"] not in rendered
    assert report.read_bytes() == retained
    assert payload == original
    assert [value for value in details if value not in report_text] == []


@pytest.mark.parametrize("snapshot_heading", ["PR Snapshot", "Review Snapshot"])
def test_compact_review_historical_links_full_report_in_snapshot_table(
    tmp_path: Path, snapshot_heading: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical (version 7) compact review suppresses supporting chat groups and links the report in the table.

    The full Markdown report holds every finding detail. Chat shows only the findings table plus one ``Report`` row
    between the reviewers and the legend; the report bytes and the input payload are left unchanged.
    """
    run, payload, row, report, retained = _compact_review(tmp_path, monkeypatch, 7, snapshot_heading)
    original = copy.deepcopy(payload)

    rendered = _load_finalizer().render_handoff(payload, report_root=run)

    destination = quote(report.resolve().as_posix(), safe="/:")
    assert f"| Report | [Markdown report](<{destination}>) |" in rendered
    assert rendered.count("| Report |") == 1
    assert rendered.index("| Reviewers |") < rendered.index("| Report |") < rendered.index("Legend:")
    _assert_details_only_in_report(rendered, payload, original, row, report, retained)


@pytest.mark.parametrize("snapshot_heading", ["PR Snapshot", "Review Snapshot"])
def test_compact_review_current_links_full_report_outside_tables(
    tmp_path: Path, snapshot_heading: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current (version 8) compact review links the full report as a bullet, never inside a table row.

    Same fixture as the historical profile; the report path is printed once as ``- Report:`` and no table line may
    contain the destination.
    """
    run, payload, row, report, retained = _compact_review(tmp_path, monkeypatch, 8, snapshot_heading)
    original = copy.deepcopy(payload)

    rendered = _load_finalizer().render_handoff(payload, report_root=run)

    destination = quote(report.resolve().as_posix(), safe="/:")
    assert f"- Report: {destination}" in rendered
    assert "| Report |" not in rendered
    assert all(destination not in line for line in rendered.splitlines() if line.startswith("|"))
    _assert_details_only_in_report(rendered, payload, original, row, report, retained)


def _compact_run_with_decoy_final(tmp_path: Path, presentation: int) -> tuple[Path, dict]:
    """Assessed PR run whose ``final.md`` is a compact decoy linked as the review notes artifact."""
    run = _assessed_pr.__wrapped__(tmp_path)
    payload = _load_handoff(run)
    payload["presentation_version"] = presentation
    (run / "final.md").write_text("A compact final exists, but it is not the full report.", encoding="utf-8")
    payload["artifacts"].append({"label": "Review notes", "path": "final.md"})
    return run, payload


PRESENTATIONS = [pytest.param(7, id="historical"), pytest.param(8, id="current")]


@pytest.mark.parametrize("presentation", PRESENTATIONS)
def test_compact_review_rejects_missing_full_report(tmp_path: Path, presentation: int) -> None:
    """Details are never hidden behind a missing report, nor is the compact final artifact substituted for it."""
    run, payload = _compact_run_with_decoy_final(tmp_path, presentation)
    (run / "review-notes.md").unlink()

    with pytest.raises(ValueError, match="review-report-missing"):
        _load_finalizer().render_handoff(payload, report_root=run)


@pytest.mark.parametrize("presentation", PRESENTATIONS)
@pytest.mark.parametrize("contents", ["", "   "])
def test_compact_review_rejects_empty_full_report(tmp_path: Path, contents: str, presentation: int) -> None:
    """Details are never hidden behind an empty or blank report, nor is the compact final artifact substituted."""
    run, payload = _compact_run_with_decoy_final(tmp_path, presentation)
    (run / "review-notes.md").write_text(contents, encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="review-report-empty"):
        _load_finalizer().render_handoff(payload, report_root=run)


@pytest.mark.parametrize(
    "limits",
    [pytest.param([], id="no-limits"), pytest.param(["First limit (-0.00)", "Second limit (-0.00)"], id="two-limits")],
)
def test_compact_review_confidence_uses_one_bullet_per_limit_and_gap(tmp_path: Path, limits: list[str]) -> None:
    """Each retained uncertainty record gets its own bullet without changing text, evidence or order.

    Three gaps (two open, one closed with evidence) plus zero or two limits are rendered; the confidence section must
    parse as exactly one list item per limit and gap, limits first.
    """
    run = _assessed_pr.__wrapped__(tmp_path)
    payload = _load_handoff(run)
    payload["presentation_version"] = 7
    gaps = [
        {"gap": "First open gap (-0.03)", "status": "unresolved", "rationale": "First recovery."},
        {"gap": "Second open gap (-0.02)", "status": "unresolved", "rationale": "Second recovery."},
        {"gap": "Closed gap (-0.00)", "status": "closed", "evidence": "retained-evidence.json"},
    ]
    payload["confidence"].update(limits=limits, gaps=gaps)

    rendered = _load_finalizer().render_handoff(payload, report_root=run)

    confidence = rendered.split("**Confidence**\n\n", 1)[1].split("\n**Artifact**", 1)[0]
    tokens = MarkdownIt("default").parse(confidence)
    bullet_text = [token.content for token in tokens if token.type == "inline"][1:]
    assert sum(token.type == "list_item_open" for token in tokens) == len(limits) + len(gaps)
    assert bullet_text == [
        *(f"Limit: {limit}" for limit in limits),
        *(f"Gap [{gap['status']}]: {gap['gap']} — {gap.get('evidence') or gap.get('rationale')}" for gap in gaps),
    ]
    assert confidence.startswith("0.95 (fair).\n\n")
    assert "\nLimits:" not in confidence


def _findings_table(layout: str, overview_only: bool) -> dict:
    """Empty findings table with the given layout flags."""
    return {
        "heading": "Review Findings and Merge Blocks",
        "layout": layout,
        "overview_only": overview_only,
        "columns": ["Finding / area", "Required change", "Evidence", "Status"],
        "rows": [],
    }


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda p: {"tables": [*p["tables"], _findings_table("concise", False)]}, id="overview"),
        pytest.param(lambda p: {"tables": [*p["tables"], _findings_table("grouped", True)]}, id="layout"),
        pytest.param(lambda p: {"tables": []}, id="no-snapshot"),
        pytest.param(lambda p: {"branch": "closed"}, id="terminal"),
        pytest.param(lambda p: {"skill": "implement"}, id="other-skill"),
    ],
)
def test_compact_review_rejects_invalid_presentation_scope(tmp_path: Path, change: Callable[[dict], dict]) -> None:
    """The compact profile stays confined to assessed reviews that carry a linked snapshot.

    Starting from a valid version-7 payload, each case breaks one precondition (non-overview findings table, wrong
    layout, missing snapshot, terminal branch, or a different skill) and rendering must refuse.
    """
    run = _assessed_pr.__wrapped__(tmp_path)
    payload = _load_handoff(run)
    payload["presentation_version"] = 7
    payload.update(change(payload))

    with pytest.raises(
        ValueError, match=r"review-(overview-only|concise-layout|snapshot)-required|presentation-version-invalid"
    ):
        _load_finalizer().render_handoff(payload, report_root=run)


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks")
def test_compact_review_rejects_full_report_symlink(tmp_path: Path) -> None:
    """A compact final artifact cannot stand in for the owning full Markdown report.

    ``review-notes.md`` is replaced by a symlink to ``final.md``; the renderer must treat the report as missing.
    """
    run = _assessed_pr.__wrapped__(tmp_path)
    payload = _load_handoff(run)
    report = run / "review-notes.md"
    report.unlink()
    report.symlink_to(run / "final.md")

    with pytest.raises(ValueError, match="review-report-missing"):
        _load_finalizer().render_handoff(payload, report_root=run)


def test_compact_review_report_link_without_reviewer_display(tmp_path: Path) -> None:
    """The full report stays reachable even when the snapshot has no attributed reviewer row."""
    run = _assessed_pr.__wrapped__(tmp_path)
    payload = _load_handoff(run)
    payload["tables"][0].pop("reviewers")

    rendered = _load_finalizer().render_handoff(payload, report_root=run)

    assert "| Reviewers |" not in rendered
    assert rendered.count("- Report: ") == 1


@pytest.mark.parametrize("field", ["title", "summary", "closure_evidence"])
def test_completed_compact_review_rejects_missing_full_report_detail(tmp_path: Path, field: str) -> None:
    """Completion cannot hide a canonical finding field in neither the terminal nor the linked report.

    The real report finder completes a run whose finding details were copied into the report; blanking one field's text
    in that report must make completion fail with a details mismatch and print nothing.
    """
    result_path = _write_remediation_producer(tmp_path, _selection()["items"][:1])
    run = result_path.parent
    record = json.loads(result_path.read_text(encoding="utf-8"))["metadata"]["review_findings"][0]
    report = run / "review-notes.md"
    before, details = report.read_text(encoding="utf-8").split("## Finding Details", 1)
    assert record[field] in details
    report.write_text(
        before + "## Finding Details" + details.replace(record[field], "Omitted detail"), encoding="utf-8", newline="\n"
    )

    completed = subprocess.run(
        [sys.executable, str(FIND_REPORT), "--complete-run", str(run), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "review-report-details-mismatch" in completed.stderr
    assert completed.stdout == ""


@COMMIT_STATUSES
@pytest.mark.parametrize("gate_status", ["fail", "timeout", "missing-command"])
def test_explicit_local_snapshot_survives_failed_gate_pipeline(tmp_path: Path, status: str, gate_status: str) -> None:
    """Explicit local snapshots are honored end to end while failed checks and an unresolved regression stay visible.

    The user asked to commit despite a failing, timed-out or missing tests gate and a known regression. The real result
    writer persists the failed status and checks; validation passes for the retained plan, rejects a tampered local-
    unresolved count, and rejects a plan that lost its explicit commit request section.
    """
    result_path = _write_remediation_candidate(tmp_path, "code", ("unresolved", "Blocked: known input regression."))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result_status = "timeout" if gate_status == "timeout" else "fail"
    result.update(status=result_status, checks_failed=["tests"])
    metadata = result["metadata"]
    metadata["unresolved_summary"].update(
        selected_items_total=1,
        selected_items_unresolved=1,
        local_actionable_items_unresolved=1,
        all_local_actionable_items_closed=False,
        unresolved_reason_groups=[
            {
                "reason": "local-code-or-doc",
                "count": 1,
                "owner": "codex",
                "next_action": "Correct the known regression after preserving the requested snapshot.",
                "evidence_path": "unresolved.txt",
            }
        ],
    )
    (tmp_path / "unresolved.txt").write_text(
        "## Unresolved Work Summary\n\nOne local regression remains.\n\n"
        "## Why Selected Items Remain Unresolved\n\n"
        "| Closure class | Next owner | Attempted evidence |\n| --- | --- | --- |\n"
        "| local-code-or-doc | codex | tests failed |\n\n## Next Action\n\nFix the regression.\n",
        encoding="utf-8",
    )
    plan = tmp_path / "commit-plan.md"
    plan.write_text(
        "## Explicit Commit Request\n\nUser requested commit this, including the known regression.\n\n"
        "## Remaining Verification\n\nTests remain failed; no passing remediation claim.\n",
        encoding="utf-8",
    )
    gates_path = tmp_path / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    gates.update(status=result_status, checks_failed=["tests"])
    gates["checks"][3].update(
        status=gate_status,
        exit_code={"fail": 1, "timeout": 124, "missing-command": 127}[gate_status],
        reason="The recorded test attempt remains incomplete or failed.",
    )
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    (tmp_path / "tests.stdout.txt").write_text("Known input regression remains.\n", encoding="utf-8")
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["verification"][3]["status"] = gate_status
    handoff["outcome"] = {"title": "Local snapshot", "summary": "Remediation remains incomplete: known regression."}
    handoff["commit_disposition"] = {
        "status": status,
        "reason": "Explicit local snapshot; the regression and failed checks remain open.",
        "evidence": "commit-plan.md",
    }
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    _bind_final_handoff(handoff_path, metadata)
    written = subprocess.run(
        [
            sys.executable,
            str(WRITE_RESULT),
            "--out",
            str(result_path),
            "--gates",
            str(gates_path),
            "--status",
            result_status,
            "--checks-run",
            ",".join(result["checks_run"]),
            "--checks-failed",
            "tests",
            "--confidence",
            str(result["confidence"]),
            "--artifact-path",
            result["artifact_path"],
            "--metadata",
            json.dumps(metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    VALIDATOR.validate("code-remediate", tmp_path, result_path)
    canonical = tmp_path / "result.json"
    result_path.replace(canonical)
    VALIDATOR.validate("code-remediate", tmp_path, canonical)
    saved = json.loads(canonical.read_text(encoding="utf-8"))
    assert saved["status"] == result_status
    assert saved["checks_failed"] == ["tests"]
    assert saved["metadata"]["final_resolution_table"]["items"][0]["resolution_status"] == "unresolved"
    assert f"tests: {gate_status}" in (tmp_path / "final.md").read_text(encoding="utf-8")

    saved["metadata"]["unresolved_summary"]["local_actionable_items_unresolved"] = 2
    canonical.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(SystemExit, match="remediation-commit-result-blocked"):
        VALIDATOR.validate("code-remediate", tmp_path, canonical)
    saved["metadata"]["unresolved_summary"]["local_actionable_items_unresolved"] = 1
    canonical.write_text(json.dumps(saved), encoding="utf-8")

    plan.write_text("## Standing approval\n\nApprove always; default grouping only.\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="remediation-commit-plan-section-missing:Explicit Commit Request"):
        VALIDATOR.validate("code-remediate", tmp_path, canonical)
