"""End-to-end contract checks for unavailable PR-review artifacts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REVIEW_VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
SHARED_VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")


def _load_module(path: Path, name: str) -> object:
    """Load one standalone validator module from its shipped path."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _write_unavailable_artifact(
    out_dir: Path,
    *,
    decision: bool = False,
    finding_table: bool = False,
    extra_result: dict[str, object] | None = None,
    extra_notes: str = "",
    checkout_started: bool = False,
) -> Path:
    """Write the smallest terminal PR-collection artifact accepted by both validators."""
    for gate_id in GATE_IDS:
        for suffix in ("command.txt", "stdout.txt", "stderr.txt"):
            (out_dir / f"{gate_id}.{suffix}").write_text("", encoding="utf-8")
    checks = [
        {
            "id": gate_id,
            "status": "not-applicable",
            "exit_code": 0,
            "duration_seconds": 0.0,
            "command_path": f"{gate_id}.command.txt",
            "stdout": f"{gate_id}.stdout.txt",
            "stderr": f"{gate_id}.stderr.txt",
            "reason": "PR evidence collection stopped before review gates.",
        }
        for gate_id in GATE_IDS
    ]
    (out_dir / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8"
    )
    (out_dir / "pr-error.txt").write_text("github-network:gh-pr-view\n", encoding="utf-8")
    (out_dir / "pr-target.txt").write_text("123\n", encoding="utf-8")
    if checkout_started:
        (out_dir / "checkout-state.json").write_text(
            json.dumps({"status": "checkout-command-started", "local_state": "changed-or-unknown"}),
            encoding="utf-8",
        )
    recovery_action = "Retry the unchanged collector later; no review or merge decision was made."
    if checkout_started:
        recovery_action += " Inspect the local checkout state before retrying."
    notes = (
        "# PR Review Availability: unavailable\n\n"
        "Source findings: not assessed\n\n"
        "Merge decision: not made\n\n"
        "Process diagnostic: `github-network:gh-pr-view`. This is a workflow/integration failure, not a PR finding or "
        "merge block.\n\n"
        f"Recovery: {recovery_action}\n\n"
        "Evidence: `pr-error.txt`.\n"
    )
    if finding_table:
        notes += (
            "\n## Review Findings and Merge Blocks\n\n"
            "| Finding / area | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| Wrong | Remove this table. | Fixture. | Required |\n"
        )
    notes += extra_notes
    (out_dir / "review-notes.md").write_text(notes, encoding="utf-8")
    metadata: dict[str, object] = {
        "scope": "pr",
        "risk_tier": "HIGH_RISK",
        "review_status": "unavailable",
        "collection_failure": {"code": "github-network:gh-pr-view", "artifact": "pr-error.txt"},
        "confidence_gaps": [
            "Core PR source verification did not complete; no source review or merge decision was made."
        ],
        "confidence_gap_closures": [
            {
                "gap": "Core PR source verification did not complete; no source review or merge decision was made.",
                "status": "unresolved",
                "rationale": (
                    "A local checkout command may have changed state, but no verified source bundle was produced."
                    if checkout_started
                    else "Core source verification did not complete; retained collection artifacts may be partial and were not assessed."
                ),
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.9,
            "final_confidence": 0.9,
            "status": "fair",
            "evidence": [
                "The classified collection failure and conservative checkout-state evidence were retained."
                if checkout_started
                else "The classified collection failure and any current-attempt collector artifacts were retained."
            ],
            "recovery_actions": ["Stopped before source review."],
            "remaining_limits": [
                "PR correctness was not assessed; inspect local checkout state before retrying."
                if checkout_started
                else "PR correctness was not assessed."
            ],
        },
    }
    if decision:
        metadata["review_decision"] = {"recommendation": "needs-more-work", "summary": "Wrong.", "rationale": "Wrong."}
    result_path = out_dir / "result.json"
    result = {
        "status": "fail",
        "checks_run": list(GATE_IDS),
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "confidence": 0.9,
        "artifact_path": str(result_path),
        "metadata": metadata,
    }
    if extra_result:
        result.update(extra_result)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return result_path


def test_unavailable_pr_artifact_is_accepted_without_assessed_review_evidence(tmp_path: Path) -> None:
    """Allow a terminal collector failure while preserving a canonical result artifact."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    shared_validator = _load_module(SHARED_VALIDATOR_PATH, "shared_artifact_validator")
    result_path = _write_unavailable_artifact(tmp_path)

    review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
    shared_validator.validate("code-review", tmp_path, result_path)


def test_unavailable_pr_artifact_retains_conservative_checkout_state(tmp_path: Path) -> None:
    """Allow terminal evidence that warns a failed checkout may have changed local state."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    result_path = _write_unavailable_artifact(tmp_path, checkout_started=True)

    review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


@pytest.mark.parametrize(
    ("decision", "finding_table", "extra_result", "extra_notes"),
    [
        pytest.param(True, False, None, "", id="true"),
        pytest.param(False, True, None, "", id="false-true"),
        pytest.param(
            False, False, {"recommendations": ["needs-more-work"]}, "", id="false-false-recommendations-needs-more-work"
        ),
        pytest.param(
            False, False, {"follow_up": ["merge must be blocked"]}, "", id="false-false-follow_up-merge-must-be-blocked"
        ),
        pytest.param(
            False,
            False,
            {"findings": {"critical": 0, "high": 0, "medium": 0, "low": 0, "source_review": {}}},
            "",
            id="false-false-findings-critical-0-high-0-medium-0-low-0-source_review",
        ),
        pytest.param(
            False,
            False,
            None,
            "\n## Decision Summary\n\nRecommendation: needs-more-work\n",
            id="false-false-none-decision-summary-recommendation-needs-more-work",
        ),
        pytest.param(
            False,
            False,
            None,
            "\nSource assessment: implementation requires changes.\n",
            id="false-false-none-source-assessment-implementation-requires-changes.",
        ),
    ],
)
def test_unavailable_pr_artifact_rejects_assessed_review_content(
    tmp_path: Path,
    decision: bool,
    finding_table: bool,
    extra_result: dict[str, object] | None,
    extra_notes: str,
) -> None:
    """Keep process failure output distinct from a source-review outcome."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    result_path = _write_unavailable_artifact(
        tmp_path,
        decision=decision,
        finding_table=finding_table,
        extra_result=extra_result,
        extra_notes=extra_notes,
    )

    with pytest.raises(SystemExit, match="unavailable-review"):
        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


def test_unavailable_pr_artifact_retains_current_attempt_evidence_without_assessing_it(tmp_path: Path) -> None:
    """Allow diagnostic evidence from the failed attempt without inventing source findings."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    result_path = _write_unavailable_artifact(tmp_path)
    (tmp_path / "pr.json").write_text(json.dumps({"number": 123, "body": "Contributor intent"}), encoding="utf-8")
    (tmp_path / "diff.patch").write_text("partial current-attempt evidence\n", encoding="utf-8")

    review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


def test_unavailable_pr_artifact_rejects_process_diagnostic_table(tmp_path: Path) -> None:
    """Keep workflow failures out of the PR findings/action-table visual language."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    result_path = _write_unavailable_artifact(tmp_path, extra_notes="\n| Area | Recovery |\n| --- | --- |\n")

    with pytest.raises(SystemExit, match="unavailable-review-process-table-forbidden"):
        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
