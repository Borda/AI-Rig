"""Regression checks for assessed code-review snapshot reconciliation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
FINALIZER = PLUGIN_ROOT / "shared" / "final_handoff.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
SUGGESTIONS = {
    "accept-as-is": "approve",
    "minor-changes": "minor changes",
    "needs-more-work": "needs work",
    "reject": "reject",
    "not-aligned": "not aligned",
}


def _load_validator(
    path: Path = PLUGIN_ROOT / "shared" / "validate-artifacts.py", name: str = "codex_rig_review_handoff_validator"
) -> ModuleType:
    """Load the hyphenated shared artifact validator for focused checks."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _write_complete_unavailable_v2_artifact(out_dir: Path, checkout_state: dict[str, object]) -> Path:
    """Write one rendered unavailable-review result bound to current checkout-state evidence."""
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
    code = "github-network:gh-pr-view"
    (out_dir / "pr-error.txt").write_text(code + "\n", encoding="utf-8")
    (out_dir / "pr-target.txt").write_text("123\n", encoding="utf-8")
    (out_dir / "checkout-state.json").write_text(json.dumps(checkout_state), encoding="utf-8")
    recovery_action = "Retry the unchanged collector later; no review or merge decision was made."
    recovery_action += " Inspect the local checkout state before retrying."
    (out_dir / "review-notes.md").write_text(
        "# PR Review Availability: unavailable\n\n"
        "Source findings: not assessed\n\n"
        "Merge decision: not made\n\n"
        "Process diagnostic: `github-network:gh-pr-view`. This is a workflow/integration failure, not a PR finding or "
        "merge block.\n\n"
        f"Recovery: {recovery_action}\n\n"
        "Evidence: `pr-error.txt`.\n",
        encoding="utf-8",
    )
    confidence_gap = "Core PR source verification did not complete; no source review or merge decision was made."
    closures = [
        {
            "gap": confidence_gap,
            "status": "unresolved",
            "rationale": "A local checkout command may have changed state, but no verified source bundle was produced.",
        }
    ]
    recovery = {
        "initial_confidence": 0.9,
        "final_confidence": 0.9,
        "status": "fair",
        "evidence": ["The classified collection failure and conservative checkout-state evidence were retained."],
        "recovery_actions": ["Stopped before source review."],
        "remaining_limits": ["PR correctness was not assessed; inspect local checkout state before retrying."],
    }
    result_path = out_dir / "result.json"
    checkout_status = checkout_state["status"]
    handoff = {
        "schema_version": 1,
        "presentation_version": 2,
        "skill": "code-review",
        "branch": "unavailable",
        "outcome": {
            "title": "PR Review Availability",
            "summary": (
                "I could not retrieve the PR metadata, so the review has not started. "
                "Reason: `github-network:gh-pr-view`. Checkout diagnostic: local worktree state is changed or unknown "
                f"after `{checkout_status}`. The collector did not retain a more specific cause."
            ),
        },
        "tables": [],
        "source_records": [],
        "source_coverage": {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": gate_id, "status": "not-applicable", "evidence": f"{gate_id}.stdout.txt"} for gate_id in GATE_IDS
        ],
        "remaining": [
            {
                "row_id": "collection-recovery",
                "item": "PR collection stopped at `github-network:gh-pr-view`.",
                "owner": "code-review",
                "next_action": (
                    "Inspect the classified `gh-pr-view` collector failure and local checkout state before retrying. "
                    "Resume only after a fresh collector run produces and validates the PR source bundle."
                ),
            }
        ],
        "next_steps": ["collection-recovery"],
        "confidence": {"score": 0.9, "band": "fair", "limits": recovery["remaining_limits"], "gaps": closures},
        "artifacts": [
            {"label": "Collection failure", "path": "pr-error.txt"},
            {"label": "Checkout state", "path": "checkout-state.json"},
            {"label": "Result", "path": str(result_path)},
        ],
        "caller_contract": None,
    }
    handoff_path = out_dir / "final-handoff.json"
    final_path = out_dir / "final.md"
    validation_path = out_dir / "final-handoff.validation.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    finalizer = _load_validator(FINALIZER, "code_review_unavailable_finalizer")
    validation = finalizer.render_files(handoff_path, final_path, validation_path)
    metadata = {
        "scope": "pr",
        "risk_tier": "HIGH_RISK",
        "review_status": "unavailable",
        "collection_failure": {"code": code, "artifact": "pr-error.txt"},
        "confidence_gaps": [confidence_gap],
        "confidence_gap_closures": closures,
        "confidence_recovery": recovery,
        "final_handoff": {
            "schema_version": 1,
            "handoff_path": str(handoff_path),
            "handoff_sha256": validation["handoff_sha256"],
            "rendered_path": str(final_path),
            "rendered_sha256": validation["rendered_sha256"],
            "validation_path": str(validation_path),
            "branch": "unavailable",
        },
    }
    result = {
        "schema_version": 2,
        "status": "fail",
        "checks_run": list(GATE_IDS),
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "confidence": 0.9,
        "artifact_path": str(result_path),
        "metadata": metadata,
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return result_path


def _result(recommendation: str) -> dict[str, object]:
    """Return one assessed PR result with a structured decision.

    Example:
        >>> _result("accept-as-is")["metadata"]["review_decision"]["recommendation"]
        'accept-as-is'
    """
    return {
        "metadata": {
            "scope": "pr",
            "review_decision": {
                "recommendation": recommendation,
                "summary": "The review decision is evidence-backed.",
                "rationale": "The inspected diff and gates determine this outcome.",
            },
        },
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    }


def _handoff(recommendation: str, suggestion: str) -> dict[str, object]:
    """Return one compact assessed PR snapshot with the supplied suggestion.

    Example:
        >>> _handoff("accept-as-is", "approved")["tables"][0]["heading"]
        'PR Snapshot'
    """
    rows = [
        ("PR", "[#1399 — Pack targets](https://github.com/example/project/pull/1399)"),
        ("Author", "@contributor"),
        ("CI", "passing"),
        ("Type", "perf"),
        ("Suggestion", suggestion),
    ]
    return {
        "branch": "assessed",
        "outcome": {"title": "Review Decision", "summary": f"Recommendation: {recommendation}."},
        "tables": [
            {
                "heading": "PR Snapshot",
                "columns": ["Field", "Value"],
                "rows": [
                    {"id": f"PR-{index}", "cells": [field, value], "source_ids": [f"source-{index}"]}
                    for index, (field, value) in enumerate(rows, start=1)
                ],
            }
        ],
    }


SNAPSHOT_FIELDS = {
    "PR Snapshot": ("PR", "Author", "CI", "Type", "Suggestion"),
    "Review Snapshot": ("Scope", "Revision", "CI", "Type", "Suggestion"),
}


def _reshape_snapshot(snapshot: dict[str, object], heading: str, suggestion: str) -> None:
    """Point one snapshot table at the field set its own heading requires.

    Example:
        >>> table = {"heading": "PR Snapshot", "columns": ["Field", "Value"], "rows": []}
        >>> _reshape_snapshot(table, "Review Snapshot", "approve")
        >>> [row["cells"][0] for row in table["rows"]]
        ['Scope', 'Revision', 'CI', 'Type', 'Suggestion']
    """
    values = {
        "PR": "[#1399 — Pack targets](https://github.com/example/project/pull/1399)",
        "Author": "@contributor",
        "Scope": "working tree",
        "Revision": "0f1e2d3",
        "CI": "unavailable",
        "Type": "perf",
        "Suggestion": suggestion,
    }
    snapshot["heading"] = heading
    snapshot["rows"] = [
        {"id": f"PR-{index}", "cells": [field, values[field]], "source_ids": [f"source-{index}"]}
        for index, field in enumerate(SNAPSHOT_FIELDS[heading], start=1)
    ]


def test_review_snapshot_rows_are_bound_to_their_non_pr_heading() -> None:
    """Reject a non-PR snapshot that reuses PR fields or contradicts the recommendation."""
    result = _result("needs-more-work")
    result["metadata"]["scope"] = "working-tree"
    handoff = _handoff("needs-more-work", "needs work")
    snapshot = handoff["tables"][0]
    _reshape_snapshot(snapshot, "Review Snapshot", "needs work")
    VALIDATOR._validate_code_review_final_handoff(result, handoff)

    snapshot["rows"][0]["cells"][0] = "PR"
    with pytest.raises(SystemExit, match="code-review-final-handoff-review-snapshot-fields-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)
    snapshot["rows"][0]["cells"][0] = "Scope"
    snapshot["rows"][-1]["cells"][1] = "approve"
    with pytest.raises(SystemExit, match="code-review-final-handoff-review-snapshot-suggestion-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)


@pytest.mark.parametrize(
    "malformed_fields",
    [
        pytest.param(("PR", "Author", "CI", "Type"), id="pr-author-ci-type"),
        pytest.param(("PR", "Author", "CI", "Type", "State"), id="pr-author-ci-type-state"),
    ],
)
def test_review_snapshot_rejects_missing_or_replaced_suggestion(malformed_fields: tuple[str, ...]) -> None:
    """Prevent a complete-looking PR summary from omitting its review outcome."""
    handoff = _handoff("needs-more-work", "needs work")
    snapshot = handoff["tables"][0]
    snapshot["rows"] = [
        {"id": f"PR-{index}", "cells": [field, "value"], "source_ids": [f"source-{index}"]}
        for index, field in enumerate(malformed_fields, start=1)
    ]

    with pytest.raises(SystemExit, match="code-review-final-handoff-pr-snapshot-fields-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(_result("needs-more-work"), handoff)


@pytest.mark.parametrize(
    ("recommendation", "suggestion"),
    [pytest.param(recommendation, suggestion, id=recommendation) for recommendation, suggestion in SUGGESTIONS.items()],
)
def test_review_snapshot_suggestion_is_bound_to_structured_decision(recommendation: str, suggestion: str) -> None:
    """Keep every user-facing suggestion synchronized with the validated decision."""
    handoff = _handoff(recommendation, suggestion)

    VALIDATOR._validate_code_review_final_handoff(_result(recommendation), handoff)

    handoff["tables"][0]["rows"][-1]["cells"][1] = "approve" if suggestion != "approve" else "needs work"
    with pytest.raises(SystemExit, match="code-review-final-handoff-pr-snapshot-suggestion-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(_result(recommendation), handoff)


def test_review_outcome_is_bound_to_the_canonical_recommendation() -> None:
    """Prevent the prose outcome from approving a decision that needs more work."""
    handoff = _handoff("needs-more-work", "needs work")
    handoff["outcome"] = {"title": "Review Decision", "summary": "Recommendation: accept-as-is."}

    with pytest.raises(SystemExit, match="code-review-final-handoff-outcome-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(_result("needs-more-work"), handoff)


def test_review_ratings_and_summary_are_bound_to_retained_assessments() -> None:
    """Reject altered ratings or a removed aggregate summary while preserving the PR decision."""
    result = _result("needs-more-work")
    assessments = [{"role": "QA specialist", "rating": 4, "evidence": "specialists/qa.md"}]
    result["metadata"]["reviewer_assessments"] = assessments
    handoff = _handoff("needs-more-work", "needs work")
    snapshot = handoff["tables"][0]
    snapshot["reviewers"] = [dict(assessments[0])]
    snapshot["summary"] = result["metadata"]["review_decision"]["summary"]
    VALIDATOR._validate_code_review_final_handoff(result, handoff)

    snapshot["reviewers"][0]["rating"] = 1
    with pytest.raises(SystemExit, match="code-review-final-handoff-reviewers-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)
    snapshot["reviewers"][0]["rating"] = 4
    del snapshot["summary"]
    with pytest.raises(SystemExit, match="code-review-final-handoff-review-summary-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)


@pytest.mark.parametrize("scope", ["pr", "working-tree"])
@pytest.mark.parametrize("missing", ["all", "assessments", "snapshot", "reviewers", "summary", "wrong-heading"])
def test_new_assessed_candidate_requires_attribution(scope: str, missing: str) -> None:
    """Reject missing candidate attribution without retroactively invalidating historical reports."""
    result = _result("accept-as-is")
    result["schema_version"] = 2
    metadata = result["metadata"]
    metadata.update(scope=scope, finding_records_version=1, review_findings=[], operational_blockers=[])
    handoff = _handoff("accept-as-is", "approve")
    snapshot = handoff["tables"][0]
    if scope != "pr":
        _reshape_snapshot(snapshot, "Review Snapshot", "approve")
    # The same unattributed stored report stays readable, but cannot be promoted as a new candidate.
    VALIDATOR._validate_code_review_final_handoff(result, handoff)
    if missing != "all":
        assessments = [{"role": "QA specialist", "rating": 1, "evidence": "specialists/qa.md"}]
        metadata["reviewer_assessments"] = assessments
        snapshot.update(reviewers=assessments, summary=metadata["review_decision"]["summary"])
        VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)
        if missing == "assessments":
            del metadata["reviewer_assessments"]
        elif missing == "snapshot":
            handoff["tables"] = []
        elif missing == "wrong-heading":
            _reshape_snapshot(snapshot, "Review Snapshot" if scope == "pr" else "PR Snapshot", "approve")
        else:
            del snapshot[missing]
    with pytest.raises(
        SystemExit, match="code-review-final-handoff-(candidate-attribution-missing|review-summary-mismatch)"
    ):
        VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)


@pytest.mark.parametrize("branch", ["unavailable", "closed", "caller-contract"])
def test_new_terminal_review_keeps_attribution_exception(branch: str) -> None:
    """Do not invent participants for terminal or explicitly caller-defined output."""
    result = _result("accept-as-is")
    result["metadata"]["review_status"] = branch
    handoff = {"branch": branch, "tables": []}
    VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)


def test_candidate_flag_reaches_review_attribution_gate(tmp_path: Path) -> None:
    """A digest-valid historical handoff must fail the new-candidate path when attribution is absent."""
    result_path = _write_complete_unavailable_v2_artifact(tmp_path, {"status": "not-attempted"})
    result = json.loads(result_path.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    metadata.pop("review_status")
    metadata.update(
        scope="working-tree",
        finding_records_version=1,
        review_findings=[],
        operational_blockers=[],
        review_decision={
            "recommendation": "needs-more-work",
            "summary": "Evidence is incomplete.",
            "rationale": "Verify before accepting.",
        },
    )
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["branch"] = "assessed"
    handoff["outcome"] = {"title": "Review Decision", "summary": "Recommendation: needs-more-work."}
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    finalizer = _load_validator(FINALIZER, "candidate_attribution_finalizer")
    validation = finalizer.render_files(handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json")
    metadata["final_handoff"].update(
        branch="assessed", handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"]
    )
    gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))
    VALIDATOR._validate_final_handoff(result, "code-review", tmp_path, gates)
    with pytest.raises(SystemExit, match="code-review-final-handoff-candidate-attribution-missing"):
        VALIDATOR._validate_final_handoff(result, "code-review", tmp_path, gates, candidate=True)


def test_review_handoff_rejects_replaced_finding_identity() -> None:
    """A digest-bound final table must not replace the finding reviewed in the source notes."""
    result = _result("needs-more-work")
    result["schema_version"] = 2
    result["findings"]["high"] = 1
    result["metadata"]["review_findings"] = [{"id": "CR-1", "severity": "high"}]
    result["metadata"]["finding_records_version"] = 1
    handoff = _handoff("needs-more-work", "needs work")
    handoff["tables"].append(
        {
            "heading": "Review Findings and Merge Blocks",
            "layout": "grouped",
            "rows": [{"id": "row-1", "title": "CR-1", "cells": ["CR-2", "Fix", "source.py:1", "Required"]}],
        }
    )

    with pytest.raises(SystemExit, match="code-review-final-handoff-finding-identity-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)

    handoff["tables"][-1]["rows"][0]["cells"][0] = "CR-1"
    VALIDATOR._validate_code_review_final_handoff(result, handoff)
    result["metadata"]["review_findings"][0]["authors"] = ["QA specialist"]
    with pytest.raises(SystemExit, match="code-review-final-handoff-finding-authors-mismatch"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)
    handoff["tables"][-1]["rows"][0]["authors"] = ["QA specialist"]
    VALIDATOR._validate_code_review_final_handoff(result, handoff)


def test_review_handoff_rejects_a_new_candidate_missing_the_canonical_marker() -> None:
    """A schema-v2 handoff cannot omit the canonical records marker to reach the bare-record path.

    CR8: without the marker the grouped-layout requirement was skipped entirely, so a new candidate could
    ship an ungrouped table carrying none of the canonical detail fields.
    """
    result = _result("needs-more-work")
    result["schema_version"] = 2
    result["findings"]["high"] = 1
    result["metadata"]["review_findings"] = [{"id": "CR-1", "severity": "high"}]
    handoff = _handoff("needs-more-work", "needs work")
    handoff["tables"].append(
        {
            "heading": "Review Findings and Merge Blocks",
            "rows": [{"id": "row-1", "cells": ["CR-1", "Fix", "source.py:1", "Required"]}],
        }
    )

    with pytest.raises(SystemExit, match="code-review-final-handoff-records-version-missing"):
        VALIDATOR._validate_code_review_final_handoff(result, handoff)


@pytest.mark.parametrize(
    "invalid_action",
    [
        "Repair the checkout and retry. Resume only after a fresh collector run produces and validates the PR source bundle.",
        "Rerun CI. Resume only after a fresh collector run produces and validates the PR source bundle.",
    ],
)
@pytest.mark.parametrize(
    "checkout_state",
    [
        pytest.param(
            {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
            id="legacy-started",
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
                "gh_checkout_failure": {
                    "command": "gh pr checkout https://github.com/Borda/AI-Rig/pull/123",
                    "code": "github-network:local-pr-checkout",
                    "diagnostics": {
                        "exit_code": 1,
                        "failure_class": "github-network",
                        "failure_reason": "connection-reset",
                        "label": "local-pr-checkout",
                    },
                },
            },
            id="collector-failed-gh-recovery",
        ),
    ],
)
def test_unavailable_v2_handoff_binds_collection_diagnostics_to_safe_artifacts(
    tmp_path: Path, invalid_action: str, checkout_state: dict[str, object]
) -> None:
    """Reject a generic checkout-repair message that omits the observed collection failure."""
    code = "command-failed:local-pr-checkout"
    (tmp_path / "pr-error.txt").write_text(code + "\n", encoding="utf-8")
    (tmp_path / "command-failure.json").write_text(
        json.dumps(
            {
                "exit_code": 1,
                "failure_class": "github-command-failed",
                "failure_reason": "unclassified",
                "label": "local-pr-checkout",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "checkout-state.json").write_text(json.dumps(checkout_state), encoding="utf-8")
    checkout_status = checkout_state["status"]
    assert isinstance(checkout_status, str)
    handoff = {
        "presentation_version": 2,
        "branch": "unavailable",
        "outcome": {
            "title": "PR Review Availability",
            "summary": "Repair the checkout and retry.",
        },
        "tables": [],
        "artifacts": [
            {"label": "Collection failure", "path": "pr-error.txt"},
            {"label": "Command diagnostic", "path": "command-failure.json"},
            {"label": "Checkout state", "path": "checkout-state.json"},
        ],
        "remaining": [],
        "next_steps": [],
    }
    metadata = {
        "collection_failure": {"code": code, "artifact": "pr-error.txt"},
        "final_handoff": {"handoff_path": "final-handoff.json"},
    }
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")

    review_validator = _load_validator(CODE_REVIEW_VALIDATOR, "code_review_terminal_handoff_validator")
    with pytest.raises(SystemExit, match="unavailable-review-final-handoff-summary-mismatch"):
        review_validator._validate_unavailable_final_handoff(tmp_path, metadata)

    handoff["outcome"]["summary"] = (
        "I could not check out the latest PR commit, so the review has not started. "
        "Reason: `command-failed:local-pr-checkout`. Command diagnostic: `local-pr-checkout` exited 1 "
        "(`github-command-failed`; reason `unclassified`). Checkout diagnostic: local worktree state is changed or unknown after "
        f"`{checkout_status}`. The collector did not retain a more specific cause."
    )
    handoff["remaining"] = [
        {
            "row_id": "local-checkout-recovery",
            "item": "PR collection stopped at `command-failed:local-pr-checkout`.",
            "owner": "code-review",
            "next_action": (
                "Code-review must inspect the classified `local-pr-checkout` collector failure and local checkout state "
                "before retrying. "
                "Resume only after a fresh collector run produces and validates the PR source bundle."
            ),
        }
    ]
    handoff["next_steps"] = ["local-checkout-recovery"]
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")

    review_validator._validate_unavailable_final_handoff(tmp_path, metadata)

    handoff["remaining"][0]["next_action"] = (
        "Code-review may refetch the `local-pr-checkout` pull ref after a maintainer confirms the protected-worktree "
        "decision. Resume only after a fresh collector run produces and validates the PR source bundle."
    )
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")

    review_validator._validate_unavailable_final_handoff(tmp_path, metadata)

    handoff["remaining"][0]["next_action"] = invalid_action
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(SystemExit, match="unavailable-review-final-handoff-recovery-mismatch"):
        review_validator._validate_unavailable_final_handoff(tmp_path, metadata)

    handoff["remaining"][0]["next_action"] = (
        "Code-review must inspect the classified `local-pr-checkout` collector failure and local checkout state before "
        "retrying. Resume only after a fresh collector run produces and validates the PR source bundle."
    )
    current_head = "a" * 40
    expected_head = "b" * 40
    (tmp_path / "worktree-preflight.json").write_text(
        json.dumps(
            {
                "status": "clean",
                "current_head": current_head,
                "expected_head": expected_head,
                "dirty_paths": [],
                "checkout_paths": ["changed.py"],
                "overlapping_paths": [],
                "pr_paths": ["changed.py"],
                "overlapping_pr_paths": [],
                "unmerged_paths": [],
                "phase": "before-checkout",
            }
        ),
        encoding="utf-8",
    )
    handoff["artifacts"].append({"label": "Worktree preflight", "path": "worktree-preflight.json"})
    handoff["outcome"]["summary"] = (
        "I could not check out the latest PR commit, so the review has not started. "
        "Reason: `command-failed:local-pr-checkout`. Command diagnostic: `local-pr-checkout` exited 1 "
        "(`github-command-failed`; reason `unclassified`). Checkout diagnostic: local worktree state is changed or unknown after "
        f"`{checkout_status}`. Worktree preflight: local head `{current_head}`; expected PR head "
        f"`{expected_head}`. The collector did not retain a more specific cause."
    )
    handoff["remaining"][0]["next_action"] = (
        "Code-review must inspect the classified `local-pr-checkout` collector failure and compare the retained current "
        "and expected PR heads before choosing a permitted retry. Resume only after a fresh collector run produces and "
        "validates the PR source bundle."
    )
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")

    review_validator._validate_unavailable_final_handoff(tmp_path, metadata)


@pytest.mark.parametrize(
    "checkout_state",
    [
        pytest.param(
            {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
            id="legacy-started",
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
                "gh_checkout_failure": {
                    "command": "gh pr checkout https://github.com/Borda/AI-Rig/pull/123",
                    "code": "github-network:local-pr-checkout",
                    "diagnostics": {
                        "exit_code": 1,
                        "failure_class": "github-network",
                        "failure_reason": "connection-reset",
                        "label": "local-pr-checkout",
                    },
                },
            },
            id="collector-failed-gh-recovery",
        ),
    ],
)
def test_unavailable_v2_handoff_current_checkout_states_pass_both_validators(
    tmp_path: Path, checkout_state: dict[str, object]
) -> None:
    """Bind rendered unavailable handoffs to legacy and current collector checkout states."""
    result_path = _write_complete_unavailable_v2_artifact(tmp_path, checkout_state)
    review_validator = _load_validator(CODE_REVIEW_VALIDATOR, "code_review_complete_handoff_validator")

    review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
    VALIDATOR.validate("code-review", tmp_path, result_path)
