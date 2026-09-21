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
GH_CHECKOUT_FAILURE = {
    "command": "gh pr checkout https://github.com/Borda/AI-Rig/pull/123",
    "code": "github-network:local-pr-checkout",
    "diagnostics": {
        "exit_code": 1,
        "failure_class": "github-network",
        "failure_reason": "connection-reset",
        "label": "local-pr-checkout",
    },
}


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
    checkout_state: dict[str, object] | None = None,
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
    if checkout_state is not None:
        (out_dir / "checkout-state.json").write_text(json.dumps(checkout_state), encoding="utf-8")
    recovery_action = "Retry the unchanged collector later; no review or merge decision was made."
    if checkout_state is not None:
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
                    if checkout_state is not None
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
                if checkout_state is not None
                else "The classified collection failure and any current-attempt collector artifacts were retained."
            ],
            "recovery_actions": ["Stopped before source review."],
            "remaining_limits": [
                "PR correctness was not assessed; inspect local checkout state before retrying."
                if checkout_state is not None
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


@pytest.mark.parametrize(
    ("code", "reason", "expected_action"),
    [
        pytest.param(
            "command-failed:pr-head-fetch",
            "repository-unavailable",
            "Confirm the canonical repository identity and availability after a state change, then start a fresh collector run.",
            id="repository-unavailable-no-auth-guess",
        ),
        pytest.param(
            "collector:dirty-pr-worktree-before-pr-checkout",
            None,
            "Preserve or move the local changes that overlap PR files, then start a fresh collector run.",
            id="dirty-pr-paths",
        ),
        pytest.param(
            "collector:unresolved-index-before-pr-checkout",
            None,
            "Resolve the Git index entries, then start a fresh collector run.",
            id="unresolved-index",
        ),
    ],
)
def test_unavailable_recovery_is_actionable_without_auth_guess(
    code: str, reason: str | None, expected_action: str
) -> None:
    """Keep fetch and worktree recovery tied to the classified safe cause."""
    validator = _load_module(REVIEW_VALIDATOR_PATH, "review_validator_recovery")

    assert validator._unavailable_recovery_action(code, False, reason) == expected_action


def test_unavailable_pr_artifact_is_accepted_without_assessed_review_evidence(tmp_path: Path) -> None:
    """Allow a terminal collector failure while preserving a canonical result artifact."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    shared_validator = _load_module(SHARED_VALIDATOR_PATH, "shared_artifact_validator")
    result_path = _write_unavailable_artifact(tmp_path)

    review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
    shared_validator.validate("code-review", tmp_path, result_path)


@pytest.mark.parametrize(
    "checkout_state",
    [
        pytest.param(
            {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
            id="historical-checkout-started",
        ),
        pytest.param(
            {"status": "checkout-command-succeeded-unverified", "local_state": "changed-or-unknown"},
            id="historical-succeeded-unverified",
        ),
        pytest.param(
            {
                "status": "checkout-command-succeeded-unverified",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": None,
            },
            id="collector-succeeded-unverified",
        ),
        pytest.param(
            {
                "status": "gh-checkout-failed-recovery-assessment-started",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": GH_CHECKOUT_FAILURE,
            },
            id="collector-failed-gh-recovery",
        ),
        pytest.param(
            {
                "status": "checkout-command-succeeded-unverified",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": GH_CHECKOUT_FAILURE,
            },
            id="collector-fallback-remains-unverified",
        ),
    ],
)
def test_unavailable_pr_artifact_accepts_collector_checkout_states(
    tmp_path: Path, checkout_state: dict[str, object]
) -> None:
    """Accept bounded legacy and current collector checkout-state records."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    result_path = _write_unavailable_artifact(tmp_path, checkout_state=checkout_state)

    review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


@pytest.mark.parametrize(
    "checkout_state",
    [
        pytest.param(
            {"status": "unknown", "local_state": "changed-or-unknown"},
            id="unknown-status",
        ),
        pytest.param(
            {
                "status": "checkout-command-succeeded-unverified",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": {**GH_CHECKOUT_FAILURE, "stderr": "credential=secret"},
            },
            id="failure-diagnostics-raw-stderr",
        ),
        pytest.param(
            {
                "status": "gh-checkout-failed-recovery-assessment-started",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": {**GH_CHECKOUT_FAILURE, "diagnostics": {"label": "wrong"}},
            },
            id="failure-diagnostics-wrong-label",
        ),
        pytest.param(
            {
                "status": "gh-checkout-failed-recovery-assessment-started",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": {
                    **GH_CHECKOUT_FAILURE,
                    "diagnostics": {
                        "failure_class": "github-network",
                        "label": "local-pr-checkout",
                        "stderr": "credential=secret",
                    },
                },
            },
            id="failure-diagnostics-extra-field",
        ),
        pytest.param(
            {
                "status": "gh-checkout-failed-recovery-assessment-started",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": {
                    **GH_CHECKOUT_FAILURE,
                    "command": "gh pr checkout https://token@github.com/Borda/AI-Rig/pull/123",
                },
            },
            id="failure-command-credential-url",
        ),
        pytest.param(
            {
                "status": "checkout-command-succeeded-unverified",
                "local_state": "changed-or-unknown",
                "gh_checkout_failure": None,
                "unexpected": True,
            },
            id="state-extra-field",
        ),
    ],
)
def test_unavailable_pr_artifact_rejects_malformed_checkout_state(
    tmp_path: Path, checkout_state: dict[str, object]
) -> None:
    """Reject checkout-state fields that are not produced by the bounded collector schema."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    result_path = _write_unavailable_artifact(tmp_path, checkout_state=checkout_state)

    with pytest.raises(SystemExit, match="unavailable-review-invalid-checkout-state"):
        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


def test_unavailable_checkout_diagnostic_omits_collector_command_and_diagnostics(tmp_path: Path) -> None:
    """Render only the fixed checkout status, never command or diagnostic payloads."""
    review_validator = _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")
    state = {
        "status": "gh-checkout-failed-recovery-assessment-started",
        "local_state": "changed-or-unknown",
        "gh_checkout_failure": GH_CHECKOUT_FAILURE,
    }
    _write_unavailable_artifact(tmp_path, checkout_state=state)

    diagnostic = review_validator._unavailable_checkout_diagnostic(tmp_path)

    assert diagnostic == (
        "Checkout diagnostic: local worktree state is changed or unknown after "
        "`gh-checkout-failed-recovery-assessment-started`."
    )
    assert GH_CHECKOUT_FAILURE["command"] not in diagnostic
    assert GH_CHECKOUT_FAILURE["code"] not in diagnostic


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
