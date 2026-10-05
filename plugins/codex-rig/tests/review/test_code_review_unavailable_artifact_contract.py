"""End-to-end contract checks for unavailable PR-review artifacts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
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


def test_network_recovery_requests_missing_access_instead_of_blind_retry() -> None:
    """Keep unavailable-review recovery actionable without authorizing denial bypass."""
    validator = _load_module(REVIEW_VALIDATOR_PATH, "permission_recovery_validator")
    recovery = validator._unavailable_recovery_action("github-network:gh-pr-view", False)

    assert "request runtime approval for the complete collector" in recovery
    assert "missing or unknown" in recovery
    assert "denial" in recovery
    assert "non-overridable" in recovery
    assert "Retry the unchanged collector later" not in recovery


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
    recovery_action = "Check effective runtime access; if required access is missing or unknown and requests are allowed, request runtime approval for the complete collector. Respect an explicit denial or non-overridable restriction; retry only after approval or an evidenced state change."
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


@pytest.mark.integration
@pytest.mark.parametrize(
    "dirty,changed,recorded,status,admissible",
    [
        pytest.param(["pkg/item.py"], ["pkg/item.py"], ["pkg/item.py"], "blocked-pr-dirty-paths", True, id="exact"),
        pytest.param(["pkg"], ["pkg/item.py"], ["pkg"], "blocked-pr-dirty-paths", True, id="dirty-ancestor"),
        pytest.param(["pkg/item.py"], ["pkg"], ["pkg/item.py"], "blocked-pr-dirty-paths", True, id="dirty-descendant"),
        pytest.param(
            ["Widget.py"], ["widget.py"], ["Widget.py"], "blocked-pr-dirty-paths", True, id="collector-case-alias"
        ),
        pytest.param(
            ["pkg/item.py"],
            ["pkg/item.py"],
            ["pkg/item.py", "unknown.py"],
            "blocked-pr-dirty-paths",
            False,
            id="overlap-not-dirty",
        ),
        pytest.param(
            ["pkg/item.py", "other.py"],
            ["pkg/item.py", "other.py"],
            ["other.py"],
            "blocked-pr-dirty-paths",
            False,
            id="missing-exact-collision",
        ),
        pytest.param(
            ["pkg", "other.py"],
            ["pkg/item.py", "other.py"],
            ["other.py"],
            "blocked-pr-dirty-paths",
            False,
            id="missing-ancestor-collision",
        ),
        pytest.param(
            ["pkg/item.py"],
            ["pkg/item.py"],
            ["pkg/item.py"],
            "safe-unrelated-dirty-paths",
            False,
            id="safe-status-hides-overlap",
        ),
    ],
)
def test_blocked_preflight_receipt_reaches_actionable_unavailable_finalization(
    tmp_path: Path, dirty: list[str], changed: list[str], recorded: list[str], status: str, admissible: bool
) -> None:
    """Consume bounded collector overlap evidence without reassessing source or consulting mutable original paths."""
    result_path = _write_unavailable_artifact(tmp_path)
    result = json.loads(result_path.read_bytes())
    result_path.unlink()
    code = "collector:dirty-pr-worktree-before-pr-checkout"
    action = "Preserve or move the local changes that overlap PR files, then start a fresh collector run."
    (tmp_path / "pr-error.txt").write_text(code + "\n", encoding="utf-8", newline="\n")
    notes_path = tmp_path / "review-notes.md"
    notes = notes_path.read_text(encoding="utf-8").replace("github-network:gh-pr-view", code)
    notes = notes.replace(
        "Check effective runtime access; if required access is missing or unknown and requests are allowed, request runtime approval for the complete collector. Respect an explicit denial or non-overridable restriction; retry only after approval or an evidenced state change.",
        action,
    )
    notes_path.write_text(notes, encoding="utf-8", newline="\n")
    metadata = result["metadata"]
    metadata["collection_failure"]["code"] = code
    current_head, expected_head = "a" * 40, "b" * 40
    preflight_path = tmp_path / "worktree-preflight.json"
    preflight = {
        "status": status,
        "current_head": current_head,
        "expected_head": expected_head,
        "dirty_paths": dirty,
        "checkout_paths": changed,
        "overlapping_paths": recorded,
        "pr_paths": changed,
        "overlapping_pr_paths": recorded,
        "unmerged_paths": [],
        "phase": "before-checkout",
    }
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8", newline="\n")
    head_diagnostic = f"Worktree preflight: local head `{current_head}`; expected PR head `{expected_head}`."
    handoff = {
        "schema_version": 1,
        "presentation_version": 3,
        "skill": "code-review",
        "branch": "unavailable",
        "outcome": {
            "title": "PR Review Availability",
            "summary": (
                "I stopped before checkout because local changes overlap PR files, so the review has not started. "
                f"Reason: `{code}`. {head_diagnostic} The collector did not retain a more specific cause."
            ),
        },
        "tables": [],
        "source_records": [],
        "source_coverage": {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        },
        "remaining": [
            {
                "row_id": "collection-recovery",
                "owner": "code-review",
                "item": f"PR collection stopped at `{code}`.",
                "next_action": f"Inspect the `dirty-pr-worktree-before-pr-checkout` receipt. {action} Resume only after a fresh collector run produces and validates the PR source bundle.",
            }
        ],
        "next_steps": ["collection-recovery"],
        "artifacts": [
            {"label": "Collection failure", "path": "pr-error.txt"},
            {"label": "Worktree preflight", "path": "worktree-preflight.json"},
        ],
        "caller_contract": None,
    }
    metadata_path, handoff_path = tmp_path / "metadata-draft.json", tmp_path / "handoff-draft.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    retained = {path: path.read_bytes() for path in (preflight_path, tmp_path / "pr-error.txt", notes_path)}
    finalized = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/remediation_finalize.py"),
            "finalize",
            "--skill",
            "code-review",
            "--parent-thread-id",
            "thread",
            "--run",
            str(tmp_path),
            "--metadata",
            str(metadata_path),
            "--handoff",
            str(handoff_path),
            "--status",
            "fail",
            "--confidence",
            "0.9",
            "--artifact-path",
            str(result_path),
            "--promote",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    outcome = json.loads(finalized.stdout)
    assert {path: path.read_bytes() for path in retained} == retained
    if not admissible:
        assert finalized.returncode == 1 and outcome["promoted"] is False, outcome
        assert "unavailable-review-worktree-preflight-invalid" in json.dumps(outcome), outcome
        assert not result_path.exists()
        return
    assert finalized.returncode == 0 and outcome["promoted"] is True, outcome
    promoted = json.loads(result_path.read_bytes())
    assert promoted["status"] == "fail"
    assert promoted["metadata"]["review_status"] == "unavailable"
    assert promoted["findings"] == dict.fromkeys(("critical", "high", "medium", "low"), 0)
    assert "review_decision" not in promoted["metadata"]
    assert "Source findings: not assessed" in notes_path.read_text(encoding="utf-8")
    final_bytes = (tmp_path / "final.md").read_bytes()
    assert head_diagnostic.encode("utf-8") in final_bytes
    assert action.encode("utf-8") in final_bytes
    assert all(path.encode("utf-8") not in final_bytes for path in dirty)
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/find-review-report.py"),
            "--complete-run",
            str(tmp_path),
            "--parent-thread-id",
            "thread",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == final_bytes
