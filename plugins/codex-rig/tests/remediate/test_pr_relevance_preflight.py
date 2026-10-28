"""Protect PR relevance requirements before remediation edits begin."""

import json
from pathlib import Path

import pytest
from test_finding_presentation import _selection
from test_remediation_finalize import HELPER
from test_remediation_preflight import _small_sequential_plan


@pytest.mark.parametrize("stage", ["selection", "plan"])
@pytest.mark.parametrize(
    ("damage", "error"),
    [
        pytest.param("missing", "preflight-pr-relevance-missing", id="missing-relevance"),
        pytest.param("unevaluated", "preflight-pr-relevance-not-evaluated", id="unevaluated-relevance"),
        pytest.param("count", "preflight-pr-relevance-count-invalid", id="invalid-count"),
        pytest.param("boolean", "preflight-pr-relevance-count-invalid", id="boolean-is-not-count"),
        pytest.param("coverage", "preflight-pr-relevance-coverage-invalid", id="uncovered-connected-item"),
        pytest.param("summary", "preflight-pr-relevance-summary-missing", id="missing-action-summary"),
        pytest.param("relation", "preflight-pr-relevance-relation-missing", id="missing-relation"),
    ],
)
def test_pr_selection_requires_relevance_before_edits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str, error: str, stage: str
) -> None:
    """Reject the concrete PR artifacts that otherwise fail only at finalization."""
    (tmp_path / "pr").mkdir()
    inventory = _selection()
    relevance = {
        "evaluated": damage != "unevaluated",
        "connected_open_items_total": 0,
        "connected_selectable_items_total": 0,
        "connected_required_followup_total": 0,
        "connected_items_marked_out_of_scope": 0,
    }
    if damage == "count":
        relevance["connected_open_items_total"] = -1
    elif damage == "boolean":
        relevance["connected_open_items_total"] = False
    elif damage in {"coverage", "relation"}:
        relevance["connected_open_items_total"] = 1
        relevance["connected_selectable_items_total"] = int(damage == "relation")
    if damage != "missing":
        inventory["pr_relevance"] = relevance
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    (tmp_path / "action-items.md").write_text(
        "## Other\n" if damage == "summary" else "## PR Relevance Summary\n", encoding="utf-8", newline="\n"
    )

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", stage]) == 1
    assert error in json.loads(capsys.readouterr().out)["code"]


@pytest.mark.parametrize("pr_mode", [False, True])
def test_selection_preflight_preserves_valid_routes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pr_mode: bool
) -> None:
    """Accept valid PR relevance and preserve local runs without a PR summary."""
    inventory = _selection()
    if pr_mode:
        (tmp_path / "pr").mkdir()
        inventory["pr_relevance"] = {
            "evaluated": True,
            "connected_open_items_total": 1,
            "connected_selectable_items_total": 1,
            "connected_required_followup_total": 0,
            "connected_items_marked_out_of_scope": 0,
        }
        (tmp_path / "action-items.md").write_text(
            "## PR Relevance Summary\n- direct-diff\n", encoding="utf-8", newline="\n"
        )
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_valid_pr_plan_preflight_reaches_existing_workplan_gate(tmp_path: Path) -> None:
    """Valid PR triage continues through the ordinary pre-edit planning checks."""
    (tmp_path / "pr").mkdir()
    _small_sequential_plan(tmp_path)
    inventory = _selection()
    inventory["selected_indexes"] = [1, 2]
    inventory["pr_relevance"] = {
        "evaluated": True,
        "connected_open_items_total": 1,
        "connected_selectable_items_total": 1,
        "connected_required_followup_total": 0,
        "connected_items_marked_out_of_scope": 0,
    }
    (tmp_path / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    (tmp_path / "action-items.md").write_text(
        "## PR Relevance Summary\n- direct-diff\n", encoding="utf-8", newline="\n"
    )
    draft = tmp_path / "planning.json"
    draft.write_text(
        json.dumps({"resolution_workplan": {"parallel_eligible": False, "parallel_approval_required": False}}),
        encoding="utf-8",
        newline="\n",
    )
    assert (
        HELPER.main(
            [
                "workplan",
                "--run",
                str(tmp_path),
                "--metadata",
                str(draft),
                "--ineligibility-reason",
                "Documentation depends on source closure.",
            ]
        )
        == 0
    )
    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "plan", "--metadata", str(draft)]) == 0
