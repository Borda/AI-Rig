"""Protect PR relevance requirements before remediation edits begin."""

import json
import subprocess
from pathlib import Path

import pytest
from test_finding_presentation import _selection
from test_remediation_finalize import HELPER
from test_remediation_preflight import _planning_draft, _run_workplan, _small_sequential_plan


def _relevance(**overrides: object) -> dict[str, object]:
    """Return a valid evaluated PR relevance record with all counters at zero, with ``overrides`` applied."""
    record = {
        "evaluated": True,
        "connected_open_items_total": 0,
        "connected_selectable_items_total": 0,
        "connected_required_followup_total": 0,
        "connected_items_marked_out_of_scope": 0,
    }
    return {**record, **overrides}


_DIRECT_DIFF_SUMMARY = "## PR Relevance Summary\n- direct-diff\n"


def _write_pr_selection(run: Path, **inventory_update: object) -> dict[str, object]:
    """Write a PR run's inventory with one connected selectable item and its relevance summary; return the inventory."""
    (run / "pr").mkdir()
    inventory = _selection()
    inventory["pr_relevance"] = _relevance(connected_open_items_total=1, connected_selectable_items_total=1)
    inventory.update(inventory_update)
    (run / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
    (run / "action-items.md").write_text(_DIRECT_DIFF_SUMMARY, encoding="utf-8", newline="\n")
    # Valid PR preflight now also requires source evidence that the fetched target is included.
    subprocess.run(["git", "init", str(run)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(run),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-m",
            "Initial target",
        ],
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(["git", "-C", str(run), "rev-parse", "HEAD"], text=True).strip()
    receipts = {
        "local-checkout.json": {"worktree": str(run.resolve())},
        "target-branch.json": {"local_head": head},
        "pr-routing.json": {"head_oid": head},
        "merge-resolution.json": {
            "schema_version": 2,
            "conflicts_detected": False,
            "status": "not-needed",
            "authorization": "not-required",
            "base_remote_ref": head,
            "target_oid": head,
            "pre_merge_head": head,
            "post_merge_head": head,
            "merge_commit": None,
            "resolved_paths": [],
            "unmerged_paths": [],
            "evidence": ["Target equals PR head"],
        },
    }
    for name, payload in receipts.items():
        (run / "pr" / name).write_text(json.dumps(payload), encoding="utf-8")
    return inventory


@pytest.mark.parametrize("stage", ["selection", "plan"])
@pytest.mark.parametrize(
    ("inventory_update", "summary", "error"),
    [
        pytest.param({}, "## PR Relevance Summary\n", "preflight-pr-relevance-missing", id="missing-relevance"),
        pytest.param(
            {"pr_relevance": _relevance(evaluated=False)},
            "## PR Relevance Summary\n",
            "preflight-pr-relevance-not-evaluated",
            id="unevaluated-relevance",
        ),
        pytest.param(
            {"pr_relevance": _relevance(connected_open_items_total=-1)},
            "## PR Relevance Summary\n",
            "preflight-pr-relevance-count-invalid",
            id="invalid-count",
        ),
        pytest.param(
            {"pr_relevance": _relevance(connected_open_items_total=False)},
            "## PR Relevance Summary\n",
            "preflight-pr-relevance-count-invalid",
            id="boolean-is-not-count",
        ),
        pytest.param(
            {"pr_relevance": _relevance(connected_open_items_total=1)},
            "## PR Relevance Summary\n",
            "preflight-pr-relevance-coverage-invalid",
            id="uncovered-connected-item",
        ),
        pytest.param(
            {"pr_relevance": _relevance()},
            "## Other\n",
            "preflight-pr-relevance-summary-missing",
            id="missing-action-summary",
        ),
        pytest.param(
            {"pr_relevance": _relevance(connected_open_items_total=1, connected_selectable_items_total=1)},
            "## PR Relevance Summary\n",
            "preflight-pr-relevance-relation-missing",
            id="missing-relation",
        ),
    ],
)
def test_pr_selection_requires_relevance_before_edits(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    inventory_update: dict[str, object],
    summary: str,
    error: str,
    stage: str,
) -> None:
    """Reject the concrete PR artifacts that otherwise fail only at finalization.

    A PR run (it has a ``pr`` directory) carries a missing, unevaluated, miscounted, uncovered, unsummarized or
    unrelated relevance record; preflight must exit 1 with the matching code at both the selection and plan stages.
    """
    (tmp_path / "pr").mkdir()
    (tmp_path / "selection.json").write_text(
        json.dumps({**_selection(), **inventory_update}), encoding="utf-8", newline="\n"
    )
    (tmp_path / "action-items.md").write_text(summary, encoding="utf-8", newline="\n")

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", stage]) == 1

    assert error in json.loads(capsys.readouterr().out)["code"]


def test_local_selection_preflight_needs_no_pr_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Preserve local runs: without a ``pr`` directory no relevance record or PR summary is required.

    Only PR runs owe the relevance contract, so a plain local selection must still pass preflight untouched.
    """
    (tmp_path / "selection.json").write_text(json.dumps(_selection()), encoding="utf-8", newline="\n")

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_valid_pr_selection_passes_preflight(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Accept a PR run whose relevance is evaluated, covered, related and summarized.

    The relevance record names one connected selectable item and the action items carry a direct-diff summary line.
    """
    _write_pr_selection(tmp_path)

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "selection"]) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "pass"


def test_valid_pr_plan_preflight_reaches_existing_workplan_gate(tmp_path: Path) -> None:
    """Valid PR triage continues through the ordinary pre-edit planning checks.

    With the sequential two-bucket plan and a valid PR relevance record, ``workplan`` renders and the plan-stage
    preflight then passes, showing the relevance gate does not block later checks.
    """
    _small_sequential_plan(tmp_path)
    _write_pr_selection(tmp_path, selected_indexes=[1, 2])
    draft = _planning_draft(tmp_path)

    assert _run_workplan(tmp_path, draft) == 0

    assert HELPER.main(["preflight", "--run", str(tmp_path), "--stage", "plan", "--metadata", str(draft)]) == 0
