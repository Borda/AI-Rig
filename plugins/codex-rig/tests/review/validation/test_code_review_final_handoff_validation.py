"""Regression checks for assessed code-review snapshot reconciliation and terminal handoff binding.

The shipped validators reconcile a rendered final handoff (snapshot table, suggestion, reviewers, findings) with the
structured result it summarizes. These checks build small results and handoffs in memory, plus a few on-disk
unavailable-review runs, and assert that each mismatch is rejected with its specific code.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest
from _modules import load_module
from test_review_completion_gate import _assessed_pr

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
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


def _raises(code: str) -> contextlib.AbstractContextManager[object]:
    """Return the expectation that the validator exits with a message containing ``code``."""
    return pytest.raises(SystemExit, match=code)


def _load_validator(
    path: Path = PLUGIN_ROOT / "shared" / "validate-artifacts.py", name: str = "codex_rig_review_handoff_validator"
) -> ModuleType:
    """Load the hyphenated shared artifact validator for focused checks."""
    return load_module(name, path)


VALIDATOR = _load_validator()
CODE_REVIEW = _load_validator(CODE_REVIEW_VALIDATOR, "code_review_handoff_validator")
FINALIZER_MODULE = _load_validator(FINALIZER, "code_review_handoff_finalizer")
_PASSES = contextlib.nullcontext()
_ATTRIBUTION_ERROR = r"code-review-final-handoff-(candidate-attribution-missing|review-summary-mismatch)"


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
    recovery_action = "Check effective runtime access; if required access is missing or unknown and requests are allowed, request runtime approval for the complete collector. Respect an explicit denial or non-overridable restriction; retry only after approval or an evidenced state change."
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
    validation = FINALIZER_MODULE.render_files(handoff_path, final_path, validation_path)
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


#: Collector checkout states a terminal unavailable handoff must bind, from the oldest to the current writer.
CHECKOUT_STATES = [
    pytest.param({"status": "checkout-command-started", "local_state": "changed-or-unknown"}, id="legacy-started"),
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
]
_ACCEPTED_SUGGESTIONS = [
    pytest.param(recommendation, text, id=recommendation) for recommendation, text in SUGGESTIONS.items()
]
_REJECTED_SUGGESTIONS = [
    pytest.param(recommendation, "needs work" if text == "approve" else "approve", id=recommendation)
    for recommendation, text in SUGGESTIONS.items()
]


def _keep_pr_snapshot(snapshot: dict[str, object]) -> None:
    """Leave a snapshot table with the PR heading and fields it was built with."""


def _use_review_snapshot(snapshot: dict[str, object]) -> None:
    """Turn a snapshot table into the local `Review Snapshot` with an `approve` suggestion."""
    _reshape_snapshot(snapshot, "Review Snapshot", "approve")


_SCOPE_SHAPES = [
    pytest.param("pr", _keep_pr_snapshot, id="pr"),
    pytest.param("working-tree", _use_review_snapshot, id="working-tree"),
]
_SCOPE_OTHER_HEADINGS = [
    pytest.param("pr", _keep_pr_snapshot, "Review Snapshot", id="pr"),
    pytest.param("working-tree", _use_review_snapshot, "PR Snapshot", id="working-tree"),
]


def _assessed(scope: str, shape: Callable[[dict[str, object]], None]) -> tuple[dict[str, object], dict[str, object]]:
    """Return an unattributed schema-2 assessed result and handoff whose snapshot table fits the given scope."""
    result = _result("accept-as-is")
    result["schema_version"] = 2
    result["metadata"].update(scope=scope, finding_records_version=1, review_findings=[], operational_blockers=[])
    handoff = _handoff("accept-as-is", "approve")
    shape(handoff["tables"][0])
    return result, handoff


def _attribute(result: dict[str, object], handoff: dict[str, object]) -> None:
    """Attach one reviewer assessment to the result and mirror it, with the summary, in the snapshot table."""
    assessments = [{"role": "QA specialist", "rating": 1, "evidence": "specialists/qa.md"}]
    result["metadata"]["reviewer_assessments"] = assessments
    handoff["tables"][0].update(reviewers=assessments, summary=result["metadata"]["review_decision"]["summary"])


def _drop_assessments(result: dict[str, object], handoff: dict[str, object], other_heading: str) -> None:
    """Remove the reviewer assessments from the result metadata."""
    del result["metadata"]["reviewer_assessments"]


def _drop_snapshot(result: dict[str, object], handoff: dict[str, object], other_heading: str) -> None:
    """Remove every table from the handoff."""
    handoff["tables"] = []


def _swap_heading(result: dict[str, object], handoff: dict[str, object], other_heading: str) -> None:
    """Re-head the snapshot table with the heading that belongs to the other scope."""
    _reshape_snapshot(handoff["tables"][0], other_heading, "approve")


def _drop_reviewers(result: dict[str, object], handoff: dict[str, object], other_heading: str) -> None:
    """Remove the reviewer list from the snapshot table."""
    del handoff["tables"][0]["reviewers"]


def _drop_summary(result: dict[str, object], handoff: dict[str, object], other_heading: str) -> None:
    """Remove the aggregate summary from the snapshot table."""
    del handoff["tables"][0]["summary"]


class _Current(NamedTuple):
    """A schema-3 local review result and handoff bound to the retained `diff.patch` digest."""

    result: dict[str, object]
    handoff: dict[str, object]
    digest: str


@pytest.fixture(name="local_snapshot")
def _local_snapshot() -> tuple[dict[str, object], dict[str, object]]:
    """Return a needs-work working-tree result and a handoff whose only table is the `Review Snapshot`."""
    result = _result("needs-more-work")
    result["metadata"]["scope"] = "working-tree"
    handoff = _handoff("needs-more-work", "needs work")
    _reshape_snapshot(handoff["tables"][0], "Review Snapshot", "needs work")
    return result, handoff


@pytest.fixture(name="current_snapshot")
def _current_snapshot(tmp_path: Path) -> _Current:
    """Write a retained `diff.patch` and return a current local review whose snapshot is bound to its digest."""
    diff = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "diff.patch").write_bytes(diff)
    digest = hashlib.sha256(diff).hexdigest()
    result = _result("needs-more-work")
    result["schema_version"] = 3
    metadata = result["metadata"]
    metadata.update(
        scope="working-tree",
        review_input_sha256=digest,
        reviewer_assessments=[{"role": "QA specialist", "rating": 3, "evidence": "specialists/qa.md"}],
        finding_records_version=1,
        review_findings=[],
        operational_blockers=[],
    )
    handoff = _handoff("needs-more-work", "needs work")
    handoff["presentation_version"] = 3
    snapshot = handoff["tables"][0]
    _reshape_snapshot(snapshot, "Review Snapshot", "needs work")
    snapshot["reviewers"] = metadata["reviewer_assessments"]
    snapshot["summary"] = metadata["review_decision"]["summary"]
    snapshot["rows"][0]["cells"][1] = "working-tree"
    snapshot["rows"][1]["cells"][1] = f"diff sha256:{digest}"
    return _Current(result, handoff, digest)


@pytest.fixture(name="rated_review")
def _rated_review() -> tuple[dict[str, object], dict[str, object]]:
    """Return a PR result and handoff whose snapshot reviewers and summary mirror the retained assessments."""
    result = _result("needs-more-work")
    assessments = [{"role": "QA specialist", "rating": 4, "evidence": "specialists/qa.md"}]
    result["metadata"]["reviewer_assessments"] = assessments
    handoff = _handoff("needs-more-work", "needs work")
    snapshot = handoff["tables"][0]
    snapshot["reviewers"] = [dict(assessments[0])]
    snapshot["summary"] = result["metadata"]["review_decision"]["summary"]
    return result, handoff


class TestReviewSnapshot:
    """The snapshot table of an assessed handoff must agree with the validated result."""

    def test_accepts_local_snapshot_fields(self, local_snapshot: tuple[dict[str, object], dict[str, object]]) -> None:
        """A non-PR snapshot with its own field set and a matching suggestion validates.

        A working-tree review shows `Scope` and `Revision` instead of PR fields, with the suggestion that its
        recommendation implies.
        """
        VALIDATOR._validate_code_review_final_handoff(*local_snapshot)

    @pytest.mark.parametrize(
        ("row", "cell", "value", "error"),
        [
            pytest.param(0, 0, "PR", "code-review-final-handoff-review-snapshot-fields-mismatch", id="pr-field-reused"),
            pytest.param(
                -1, 1, "approve", "code-review-final-handoff-review-snapshot-suggestion-mismatch", id="suggestion"
            ),
        ],
    )
    def test_rejects_local_snapshot_reusing_pr_fields_or_contradicting_decision(
        self,
        local_snapshot: tuple[dict[str, object], dict[str, object]],
        row: int,
        cell: int,
        value: str,
        error: str,
    ) -> None:
        """A non-PR snapshot that reuses PR fields or contradicts the recommendation is rejected.

        The first row either regains the PR field name, or the last row's suggestion says `approve` while the
        recommendation is `needs-more-work`.
        """
        result, handoff = local_snapshot
        handoff["tables"][0]["rows"][row]["cells"][cell] = value

        with pytest.raises(SystemExit, match=error):
            VALIDATOR._validate_code_review_final_handoff(result, handoff)

    def test_accepts_current_local_snapshot_bound_to_diff(self, tmp_path: Path, current_snapshot: _Current) -> None:
        """A current local review whose scope, revision, CI, and type match the retained patch validates.

        The revision cell carries the sha256 digest of the retained `diff.patch`, which is the only evidence of what was
        reviewed in a working tree.
        """
        VALIDATOR._validate_code_review_final_handoff(
            current_snapshot.result, current_snapshot.handoff, out_dir=tmp_path
        )

    @pytest.mark.parametrize(
        ("row", "value", "error"),
        [
            pytest.param(2, "passing", "code-review-final-handoff-review-snapshot-ci-mismatch", id="ci"),
            pytest.param(3, "invented", "code-review-final-handoff-review-snapshot-type-invalid", id="type"),
            pytest.param(0, "path", "code-review-final-handoff-review-snapshot-scope-mismatch", id="scope"),
            pytest.param(
                1,
                f"diff sha256:{'0' * 64}",
                "code-review-final-handoff-review-snapshot-revision-mismatch",
                id="revision",
            ),
        ],
    )
    def test_rejects_snapshot_cell_contradicting_retained_evidence(
        self, tmp_path: Path, current_snapshot: _Current, row: int, value: str, error: str
    ) -> None:
        """A displayed CI, type, scope, or revision that the retained evidence contradicts is rejected.

        Each row overwrites one cell of an otherwise valid current snapshot, so the error names the single field whose
        binding stopped being checked.
        """
        current_snapshot.handoff["tables"][0]["rows"][row]["cells"][1] = value

        with pytest.raises(SystemExit, match=error):
            VALIDATOR._validate_code_review_final_handoff(
                current_snapshot.result, current_snapshot.handoff, out_dir=tmp_path
            )

    def test_rejects_diff_changed_after_review(self, tmp_path: Path, current_snapshot: _Current) -> None:
        """A snapshot whose digest no longer matches the retained patch is rejected as an invalid source.

        The patch file is rewritten after the review recorded its digest, so the displayed revision cannot be trusted.
        """
        (tmp_path / "diff.patch").write_bytes(b"changed after review\n")

        with pytest.raises(SystemExit, match="code-review-final-handoff-review-snapshot-source-invalid"):
            VALIDATOR._validate_code_review_final_handoff(
                current_snapshot.result, current_snapshot.handoff, out_dir=tmp_path
            )

    @pytest.mark.parametrize("candidate", [False, True])
    def test_rejects_extra_pr_snapshot_for_local_review(self, tmp_path: Path, candidate: bool) -> None:
        """A PR-looking table must not bypass the retained local diff binding.

        A working-tree review carries a second, PR-style snapshot beside the real `Review Snapshot`; the handoff is
        refused as a scope mismatch both as a stored report and as a new candidate.
        """
        diff = b"diff --git a/widget.py b/widget.py\n"
        (tmp_path / "diff.patch").write_bytes(diff)
        digest = hashlib.sha256(diff).hexdigest()
        result = _result("accept-as-is")
        result["schema_version"] = 3
        metadata = result["metadata"]
        metadata.update(
            scope="working-tree",
            review_input_sha256=digest,
            reviewer_assessments=[{"role": "QA specialist", "rating": 1, "evidence": "specialists/qa.md"}],
            finding_records_version=1,
            review_findings=[],
            operational_blockers=[],
        )
        handoff = _handoff("accept-as-is", "approve")
        handoff["presentation_version"] = 3
        pr_snapshot = handoff["tables"][0]
        review_snapshot = {**pr_snapshot, "rows": [dict(row) for row in pr_snapshot["rows"]]}
        _reshape_snapshot(review_snapshot, "Review Snapshot", "approve")
        review_snapshot["rows"][0]["cells"][1] = "working-tree"
        review_snapshot["rows"][1]["cells"][1] = "diff sha256:invalid"
        review_snapshot["reviewers"] = metadata["reviewer_assessments"]
        review_snapshot["summary"] = metadata["review_decision"]["summary"]
        handoff["tables"].append(review_snapshot)
        pr = {
            "number": 1399,
            "title": "Pack targets",
            "url": "https://github.com/example/project/pull/1399",
            "author": {"login": "contributor"},
            "statusCheckRollup": [],
        }
        (tmp_path / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
        pr_snapshot["rows"][2]["cells"][1] = "unavailable"

        with pytest.raises(SystemExit, match="code-review-final-handoff-snapshot-scope-mismatch"):
            VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=candidate, out_dir=tmp_path)

    @pytest.mark.parametrize(
        "malformed_fields",
        [
            pytest.param(("PR", "Author", "CI", "Type"), id="pr-author-ci-type"),
            pytest.param(("PR", "Author", "CI", "Type", "State"), id="pr-author-ci-type-state"),
        ],
    )
    def test_rejects_missing_or_replaced_suggestion(self, malformed_fields: tuple[str, ...]) -> None:
        """A complete-looking PR summary cannot omit or rename its review outcome.

        The table either stops after `Type` or ends with `State` instead of `Suggestion`.
        """
        handoff = _handoff("needs-more-work", "needs work")
        handoff["tables"][0]["rows"] = [
            {"id": f"PR-{index}", "cells": [field, "value"], "source_ids": [f"source-{index}"]}
            for index, field in enumerate(malformed_fields, start=1)
        ]

        with pytest.raises(SystemExit, match="code-review-final-handoff-pr-snapshot-fields-mismatch"):
            VALIDATOR._validate_code_review_final_handoff(_result("needs-more-work"), handoff)

    @pytest.mark.parametrize(("recommendation", "suggestion"), _ACCEPTED_SUGGESTIONS)
    def test_accepts_suggestion_matching_decision(self, recommendation: str, suggestion: str) -> None:
        """Every recommendation has one user-facing suggestion that the handoff may show.

        The suggestion wording is a fixed mapping from the structured decision, so each pair must validate.
        """
        VALIDATOR._validate_code_review_final_handoff(_result(recommendation), _handoff(recommendation, suggestion))

    @pytest.mark.parametrize(("recommendation", "wrong_suggestion"), _REJECTED_SUGGESTIONS)
    def test_rejects_suggestion_contradicting_decision(self, recommendation: str, wrong_suggestion: str) -> None:
        """A suggestion that differs from the validated decision is rejected.

        The wrong wording is `approve` for every recommendation except `accept-as-is`, which gets `needs work`.
        """
        handoff = _handoff(recommendation, wrong_suggestion)

        with pytest.raises(SystemExit, match="code-review-final-handoff-pr-snapshot-suggestion-mismatch"):
            VALIDATOR._validate_code_review_final_handoff(_result(recommendation), handoff)

    def test_outcome_is_bound_to_canonical_recommendation(self) -> None:
        """The prose outcome cannot approve a decision that needs more work.

        The snapshot says `needs-more-work` while the outcome summary claims `accept-as-is`.
        """
        handoff = _handoff("needs-more-work", "needs work")
        handoff["outcome"] = {"title": "Review Decision", "summary": "Recommendation: accept-as-is."}

        with pytest.raises(SystemExit, match="code-review-final-handoff-outcome-mismatch"):
            VALIDATOR._validate_code_review_final_handoff(_result("needs-more-work"), handoff)

    def test_accepts_ratings_and_summary_mirroring_assessments(
        self, rated_review: tuple[dict[str, object], dict[str, object]]
    ) -> None:
        """Snapshot reviewers and summary that mirror the retained assessments validate.

        This is the unaltered baseline for the two rejection checks below.
        """
        VALIDATOR._validate_code_review_final_handoff(*rated_review)

    @pytest.mark.parametrize(
        ("damage", "error"),
        [
            pytest.param(
                lambda snapshot: snapshot["reviewers"][0].update(rating=1),
                "code-review-final-handoff-reviewers-mismatch",
                id="altered-rating",
            ),
            pytest.param(
                lambda snapshot: snapshot.pop("summary"),
                "code-review-final-handoff-review-summary-mismatch",
                id="removed-summary",
            ),
        ],
    )
    def test_rejects_altered_ratings_or_removed_summary(
        self, rated_review: tuple[dict[str, object], dict[str, object]], damage: Callable[[dict], object], error: str
    ) -> None:
        """Altered ratings or a removed aggregate summary are rejected while the PR decision is preserved.

        The reviewers listed in the handoff must equal the retained assessments, and the aggregate summary must exist.
        """
        result, handoff = rated_review
        damage(handoff["tables"][0])

        with pytest.raises(SystemExit, match=error):
            VALIDATOR._validate_code_review_final_handoff(result, handoff)


class TestCandidateAttribution:
    """A new assessed candidate must attribute its snapshot to reviewers; stored reports stay readable."""

    @pytest.mark.parametrize(("scope", "shape"), _SCOPE_SHAPES)
    def test_accepts_unattributed_stored_report(self, scope: str, shape: Callable[[dict[str, object]], None]) -> None:
        """The same unattributed stored report stays readable but cannot be promoted as a new candidate.

        Historical reports predate attribution, so validating them without the candidate flag must not fail.
        """
        result, handoff = _assessed(scope, shape)

        VALIDATOR._validate_code_review_final_handoff(result, handoff)

    @pytest.mark.parametrize(("scope", "shape"), _SCOPE_SHAPES)
    def test_accepts_fully_attributed_candidate(self, scope: str, shape: Callable[[dict[str, object]], None]) -> None:
        """A candidate with assessments, reviewers, and summary validates for both PR and local scopes.

        This is the baseline from which each missing part below is removed.
        """
        result, handoff = _assessed(scope, shape)
        _attribute(result, handoff)

        VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)

    @pytest.mark.parametrize(("scope", "shape"), _SCOPE_SHAPES)
    def test_rejects_candidate_without_attribution(
        self, scope: str, shape: Callable[[dict[str, object]], None]
    ) -> None:
        """A new candidate with no attribution at all is rejected.

        The result carries no reviewer assessments and the snapshot no reviewers or summary, which a fresh review must
        never promote.
        """
        result, handoff = _assessed(scope, shape)

        with pytest.raises(SystemExit, match=_ATTRIBUTION_ERROR):
            VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)

    @pytest.mark.parametrize(("scope", "shape", "other_heading"), _SCOPE_OTHER_HEADINGS)
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_drop_assessments, id="assessments"),
            pytest.param(_drop_snapshot, id="snapshot"),
            pytest.param(_swap_heading, id="wrong-heading"),
            pytest.param(_drop_reviewers, id="reviewers"),
            pytest.param(_drop_summary, id="summary"),
        ],
    )
    def test_rejects_candidate_losing_one_attribution_part(
        self,
        scope: str,
        shape: Callable[[dict[str, object]], None],
        other_heading: str,
        damage: Callable[[dict[str, object], dict[str, object], str], None],
    ) -> None:
        """Removing any single piece of attribution from a complete candidate makes it unpromotable.

        The assessments, the snapshot table, its heading for the scope, the reviewer list, and the aggregate summary
        each carry attribution, so losing one must be caught for both PR and local scopes.
        """
        result, handoff = _assessed(scope, shape)
        _attribute(result, handoff)
        damage(result, handoff, other_heading)

        with pytest.raises(SystemExit, match=_ATTRIBUTION_ERROR):
            VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)

    @pytest.mark.parametrize("branch", ["unavailable", "closed", "caller-contract"])
    def test_terminal_review_keeps_attribution_exception(self, branch: str) -> None:
        """No participants are invented for terminal or explicitly caller-defined output.

        An unavailable, closed, or caller-contract handoff has no table to attribute, and as a candidate it is valid.
        """
        result = _result("accept-as-is")
        result["metadata"]["review_status"] = branch
        handoff = {"branch": branch, "tables": []}

        VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True)

    @pytest.mark.parametrize(
        ("candidate", "expectation"),
        [
            pytest.param(False, _PASSES, id="stored-report"),
            pytest.param(True, _raises("code-review-final-handoff-candidate-attribution-missing"), id="new-candidate"),
        ],
    )
    def test_candidate_flag_reaches_attribution_gate(
        self, tmp_path: Path, candidate: bool, expectation: contextlib.AbstractContextManager[object]
    ) -> None:
        """A digest-valid historical handoff fails the new-candidate path when attribution is absent.

        An unavailable-review artifact is re-rendered as an assessed handoff with a fresh digest and no attribution. As
        a stored report it validates; the `candidate` flag must reach the attribution gate and reject it.
        """
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
        validation = FINALIZER_MODULE.render_files(
            handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
        )
        metadata["final_handoff"].update(
            branch="assessed",
            handoff_sha256=validation["handoff_sha256"],
            rendered_sha256=validation["rendered_sha256"],
        )
        gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))

        with expectation:
            VALIDATOR._validate_final_handoff(result, "code-review", tmp_path, gates, candidate=candidate)


class TestFindingIdentity:
    """The digest-bound final findings table must keep the identity and authors of the reviewed findings."""

    @pytest.mark.parametrize(
        ("table_id", "result_extra", "row_extra", "expectation"),
        [
            pytest.param(
                "CR-2", {}, {}, _raises("code-review-final-handoff-finding-identity-mismatch"), id="replaced-identity"
            ),
            pytest.param("CR-1", {}, {}, _PASSES, id="matching-identity"),
            pytest.param(
                "CR-1",
                {"authors": ["QA specialist"]},
                {},
                _raises("code-review-final-handoff-finding-authors-mismatch"),
                id="authors-missing-from-table",
            ),
            pytest.param(
                "CR-1", {"authors": ["QA specialist"]}, {"authors": ["QA specialist"]}, _PASSES, id="matching-authors"
            ),
        ],
    )
    def test_binds_table_row_to_reviewed_finding(
        self,
        table_id: str,
        result_extra: dict[str, object],
        row_extra: dict[str, object],
        expectation: contextlib.AbstractContextManager[object],
    ) -> None:
        """A digest-bound final table must not replace the finding reviewed in the source notes.

        The result holds finding CR-1; the table row either names another finding, or names CR-1 with or without the
        authors the result records for it.
        """
        result = _result("needs-more-work")
        result["schema_version"] = 2
        result["findings"]["high"] = 1
        result["metadata"]["review_findings"] = [{"id": "CR-1", "severity": "high", **result_extra}]
        result["metadata"]["finding_records_version"] = 1
        handoff = _handoff("needs-more-work", "needs work")
        row = {"id": "row-1", "title": "CR-1", "cells": [table_id, "Fix", "source.py:1", "Required"], **row_extra}
        handoff["tables"].append({"heading": "Review Findings and Merge Blocks", "layout": "grouped", "rows": [row]})

        with expectation:
            VALIDATOR._validate_code_review_final_handoff(result, handoff)

    def test_rejects_new_candidate_missing_canonical_marker(self) -> None:
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
@pytest.mark.parametrize("checkout_state", CHECKOUT_STATES)
@pytest.mark.parametrize(
    "presentation_version",
    [
        pytest.param(2, id="legacy-v2"),
        pytest.param(3, id="legacy-v3"),
        pytest.param(8, id="retained-v8"),
        pytest.param(9, id="current-v9"),
    ],
)
def test_unavailable_versioned_handoff_binds_collection_diagnostics_to_safe_artifacts(
    tmp_path: Path, invalid_action: str, checkout_state: dict[str, object], presentation_version: int
) -> None:
    """Reject a generic checkout-repair message that omits the observed collection failure.

    A terminal unavailable handoff must name the classified failure, bind its artifacts, and give a recovery action the
    evidence supports. One run walks the handoff from a rejected generic message to each accepted specific form, for
    every checkout state, presentation version, and invalid generic recovery action. The phases share one run directory
    because each builds on the artifacts the previous one wrote.
    """
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
        "presentation_version": presentation_version,
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

    with pytest.raises(SystemExit, match="unavailable-review-final-handoff-summary-mismatch"):
        CODE_REVIEW._validate_unavailable_final_handoff(tmp_path, metadata)

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

    CODE_REVIEW._validate_unavailable_final_handoff(tmp_path, metadata)

    omitted_artifact = handoff["artifacts"].pop()
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(SystemExit, match="unavailable-review-final-handoff-artifact-binding-mismatch"):
        CODE_REVIEW._validate_unavailable_final_handoff(tmp_path, metadata)
    handoff["artifacts"].append(omitted_artifact)

    handoff["remaining"][0]["next_action"] = (
        "Code-review may refetch the `local-pr-checkout` pull ref after a maintainer confirms the protected-worktree "
        "decision. Resume only after a fresh collector run produces and validates the PR source bundle."
    )
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")

    CODE_REVIEW._validate_unavailable_final_handoff(tmp_path, metadata)

    handoff["remaining"][0]["next_action"] = invalid_action
    (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(SystemExit, match="unavailable-review-final-handoff-recovery-mismatch"):
        CODE_REVIEW._validate_unavailable_final_handoff(tmp_path, metadata)

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

    CODE_REVIEW._validate_unavailable_final_handoff(tmp_path, metadata)


@pytest.mark.parametrize("checkout_state", CHECKOUT_STATES)
def test_unavailable_v2_handoff_checkout_states_pass_both_validators(
    tmp_path: Path, checkout_state: dict[str, object]
) -> None:
    """Bind rendered unavailable handoffs to legacy and current collector checkout states.

    A complete unavailable-review run is rendered through the real finalizer for each checkout state, then both the
    code-review validator and the shared artifact validator must accept it.
    """
    result_path = _write_complete_unavailable_v2_artifact(tmp_path, checkout_state)

    CODE_REVIEW._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
    VALIDATOR.validate("code-review", tmp_path, result_path)


@pytest.fixture(name="assessed_run")
def _assessed_run(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    """Build one complete assessed PR run and return its result, final handoff, and directory."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    return result, handoff, run


class TestFreshPresentation:
    """A fresh review must use the compact presentation profile while retained older handoffs stay valid."""

    def test_accepts_current_handoff_as_fresh_candidate(
        self, assessed_run: tuple[dict[str, object], dict[str, object], Path]
    ) -> None:
        """The presentation a complete assessed review writes passes as a fresh candidate.

        This is the baseline that older presentation versions are compared against.
        """
        result, handoff, run = assessed_run

        VALIDATOR._validate_code_review_final_handoff(
            result, handoff, candidate=True, fresh_candidate=True, out_dir=run
        )

    @pytest.mark.parametrize(
        "historical_version", [pytest.param(3, id="legacy"), pytest.param(7, id="compact-with-table-paths")]
    )
    def test_accepts_retained_historical_handoff(
        self, assessed_run: tuple[dict[str, object], dict[str, object], Path], historical_version: int
    ) -> None:
        """A retained version-three or version-seven handoff stays valid when it is not a fresh candidate.

        Earlier completed reviews are revalidated for reuse lookup, so older presentation versions must not be rejected
        retroactively.
        """
        result, handoff, run = assessed_run
        handoff["presentation_version"] = historical_version

        VALIDATOR._validate_code_review_final_handoff(result, handoff, candidate=True, out_dir=run)

    @pytest.mark.parametrize(
        "historical_version", [pytest.param(3, id="legacy"), pytest.param(7, id="compact-with-table-paths")]
    )
    def test_rejects_historical_handoff_as_fresh_candidate(
        self, assessed_run: tuple[dict[str, object], dict[str, object], Path], historical_version: int
    ) -> None:
        """Fresh presentation cannot bypass the compact output by reusing an older presentation version.

        The same older handoff accepted above is refused once the review is a fresh candidate.
        """
        result, handoff, run = assessed_run
        handoff["presentation_version"] = historical_version

        with pytest.raises(SystemExit, match="review-current-handoff-presentation-v8-required"):
            VALIDATOR._validate_code_review_final_handoff(
                result, handoff, candidate=True, fresh_candidate=True, out_dir=run
            )
