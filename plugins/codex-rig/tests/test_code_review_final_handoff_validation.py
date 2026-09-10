"""Regression checks for assessed code-review snapshot reconciliation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
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
def test_unavailable_v2_handoff_binds_collection_diagnostics_to_safe_artifacts(
    tmp_path: Path, invalid_action: str
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
    (tmp_path / "checkout-state.json").write_text(
        json.dumps({"status": "checkout-command-started", "local_state": "changed-or-unknown"}), encoding="utf-8"
    )
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
        "`checkout-command-started`. The collector did not retain a more specific cause."
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
            }
        ),
        encoding="utf-8",
    )
    handoff["artifacts"].append({"label": "Worktree preflight", "path": "worktree-preflight.json"})
    handoff["outcome"]["summary"] = (
        "I could not check out the latest PR commit, so the review has not started. "
        "Reason: `command-failed:local-pr-checkout`. Command diagnostic: `local-pr-checkout` exited 1 "
        "(`github-command-failed`; reason `unclassified`). Checkout diagnostic: local worktree state is changed or unknown after "
        f"`checkout-command-started`. Worktree preflight: local head `{current_head}`; expected PR head "
        f"`{expected_head}`. The collector did not retain a more specific cause."
    )
    handoff["remaining"][0]["next_action"] = (
        "Code-review must inspect the classified `local-pr-checkout` collector failure and compare the retained current "
        "and expected PR heads before choosing a permitted retry. Resume only after a fresh collector run produces and "
        "validates the PR source bundle."
    )
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")

    review_validator._validate_unavailable_final_handoff(tmp_path, metadata)
