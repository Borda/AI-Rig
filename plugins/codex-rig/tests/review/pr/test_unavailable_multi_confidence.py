"""Exercise current unavailable handoff wording against bound text injections.

An unavailable review (PR collection failed before source review) reports its confidence as named deduction gaps. These
tests run the real finalizer and completion/intake helpers on throwaway runs, accepting only honest multi-gap reports,
and expose the finalize/intake helpers that the sibling bounded-text tests reuse.
"""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_code_review_unavailable_artifact_contract import (
    GH_CHECKOUT_FAILURE,
    _load_module,
    _write_unavailable_artifact,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CHECKOUT_STARTED = {"status": "checkout-command-started", "local_state": "changed-or-unknown"}
GH_CHECKOUT_RETAINED = {
    "status": "gh-checkout-failed-recovery-assessment-started",
    "local_state": "changed-or-unknown",
    "gh_checkout_failure": GH_CHECKOUT_FAILURE,
}
FIRST_TEXT = "No command-failure.json was retained."
SECOND_TEXT = "Checkout state is changed or unknown."
FIRST = f"(-0.02) {FIRST_TEXT}"
SECOND = f"(-0.03) {SECOND_TEXT}"
NOT_ACCEPTABLE = "confidence-not-acceptable"


def write_command_failure(run_dir: Path) -> None:
    """Retain a classified command-failure diagnostic beside the collection failure."""
    diagnostic = {
        "label": "gh-pr-view",
        "exit_code": 1,
        "failure_class": "network-error",
        "failure_reason": "unclassified",
    }
    (run_dir / "command-failure.json").write_text(json.dumps(diagnostic))


def unavailable_handoff(checkout_state: dict[str, Any] | None, *, command_failure: bool) -> dict[str, Any]:
    """Build the unavailable-review handoff for a run with the given retained diagnostics."""
    checkout_started = checkout_state is not None
    artifacts = [{"label": "Collection failure", "path": "pr-error.txt"}]
    summary = (
        "I could not retrieve the PR metadata, so the review has not started. Reason: `github-network:gh-pr-view`. "
    )
    next_action = (
        "Inspect the classified `gh-pr-view` collector failure. Check effective runtime access; if required access is "
        "missing or unknown and requests are allowed, request runtime approval for the complete collector. Respect an "
        "explicit denial or non-overridable restriction; retry only after approval or an evidenced state change."
    )
    if command_failure:
        artifacts.append({"label": "Command failure", "path": "command-failure.json"})
        summary += "Command diagnostic: `gh-pr-view` exited 1 (`network-error`; reason `unclassified`). "
    if checkout_started:
        artifacts.append({"label": "Checkout state", "path": "checkout-state.json"})
        status = checkout_state["status"]
        summary += f"Checkout diagnostic: local worktree state is changed or unknown after `{status}`. "
        next_action += " Inspect the local checkout state before retrying."
    summary += "The collector did not retain a more specific cause."
    next_action += " Resume only after a fresh collector run produces and validates the PR source bundle."
    return {
        "schema_version": 1,
        "skill": "code-review",
        "branch": "unavailable",
        "outcome": {"title": "PR Review Availability", "summary": summary},
        "tables": [],
        "source_records": [],
        "source_coverage": {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        },
        "verification": [],
        "remaining": [
            {
                "row_id": "collection-recovery",
                "item": "PR collection stopped at `github-network:gh-pr-view`.",
                "owner": "code-review",
                "next_action": next_action,
            }
        ],
        "next_steps": ["collection-recovery"],
        "confidence": {},
        "artifacts": artifacts,
        "caller_contract": None,
    }


def promote_unavailable(
    run_dir: Path,
    metadata: dict[str, Any],
    checkout_state: dict[str, Any] | None,
    *,
    command_failure: bool,
    confidence: str,
    extra_checks_failed: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real finalizer with `--promote` on an unavailable run and return the finished process."""
    (run_dir / "result.json").unlink()
    if command_failure:
        write_command_failure(run_dir)
    metadata_path = run_dir / "metadata-draft.json"
    handoff_path = run_dir / "handoff-draft.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    handoff_path.write_text(
        json.dumps(unavailable_handoff(checkout_state, command_failure=command_failure)), encoding="utf-8"
    )
    extra = [] if extra_checks_failed is None else ["--extra-checks-failed", extra_checks_failed]
    return subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared" / "remediation_finalize.py"),
            "finalize",
            "--skill",
            "code-review",
            "--run",
            str(run_dir),
            "--metadata",
            str(metadata_path),
            "--handoff",
            str(handoff_path),
            "--status",
            "fail",
            "--confidence",
            confidence,
            *extra,
            "--artifact-path",
            str(run_dir / "result.json"),
            "--parent-thread-id",
            "test-thread",
            "--promote",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def assert_promoted_unavailable_blocks_intake(run_dir: Path, completed: subprocess.CompletedProcess[str]) -> None:
    """Assert a promoted unavailable run completes as unavailable and is refused as remediation intake."""
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert json.loads(completed.stdout)["promoted"] is True
    finder = _load_module(PLUGIN_ROOT / "shared" / "find-review-report.py", "unavailable_finder")
    assert finder.review_result_kind(run_dir / "result.json") == "unavailable"
    with pytest.raises(LookupError, match="matching-review-unavailable"):
        finder.require_assessed_review_result(run_dir / "result.json", parent_thread_id="test-thread")
    assert finder.complete_review_run(run_dir, parent_thread_id="test-thread") == (run_dir / "final.md").read_bytes()
    intake = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared" / "find-review-report.py"),
            "--result",
            str(run_dir / "result.json"),
            "--parent-thread-id",
            "test-thread",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert intake.returncode != 0
    assert "matching-review-unavailable-rerun-code-review" in intake.stderr + intake.stdout


def two_gap_baseline(metadata: dict[str, Any], score: float, *, checkout_started: bool) -> None:
    """Give the metadata two honest unresolved gaps plus a recovery block for the given confidence score."""
    metadata["confidence_gaps"] = [FIRST, SECOND]
    metadata["confidence_gap_closures"] = [
        {
            "gap": FIRST,
            "status": "unresolved",
            "rationale": "No command-failure.json was retained; pr-error.txt records the classification. Other diagnostics may exist.",
        },
        {
            "gap": SECOND,
            "status": "unresolved",
            "rationale": "checkout-state.json records the changed-or-unknown checkout state.",
        },
    ]
    metadata["confidence_recovery"] = {
        "initial_confidence": score,
        "final_confidence": score,
        "status": "not-acceptable-failed" if score <= 0.8 else "fair",
        "evidence": [
            "Confidence assesses the correctness of this failure report and its production and checks; the score is a subjective judgment.",
            "The classified collection failure and conservative checkout-state evidence were retained."
            if checkout_started
            else "The classified collection failure and any current-attempt collector artifacts were retained.",
        ],
        "recovery_actions": ["Stopped before source review."],
        "remaining_limits": [
            "(-0.00) PR correctness was not assessed; inspect local checkout state before retrying."
            if checkout_started
            else "(-0.00) PR correctness was not assessed.",
            "(-0.00) Confidence is a subjective judgment of report correctness and its production and checks.",
        ],
    }


def _restate_gaps(metadata: dict[str, Any], *gaps: str) -> None:
    """Replace the gap texts, keeping each closure's rationale; surplus closures and gaps are dropped together."""
    metadata["confidence_gaps"] = list(gaps)
    closures = metadata["confidence_gap_closures"]
    metadata["confidence_gap_closures"] = [
        {**closure, "gap": gap} for closure, gap in zip(closures, gaps, strict=False)
    ]


def _no_change(metadata: dict[str, Any]) -> None:
    """Leave the baseline gaps untouched."""


def _zero_gaps(metadata: dict[str, Any]) -> None:
    """Report no gaps at all, as a perfect score would."""
    metadata["confidence_gaps"] = []
    metadata["confidence_gap_closures"] = []


def _known_zero_impact(metadata: dict[str, Any]) -> None:
    """Keep both known gaps but with zero deduction each."""
    _restate_gaps(metadata, f"(-0.00) {FIRST_TEXT}", f"(-0.00) {SECOND_TEXT}")


def _half_each(metadata: dict[str, Any]) -> None:
    """Deduct 0.50 for each of the two known gaps."""
    _restate_gaps(metadata, f"(-0.50) {FIRST_TEXT}", f"(-0.50) {SECOND_TEXT}")


def _single_total_loss(metadata: dict[str, Any]) -> None:
    """Report one known gap deducting the whole score."""
    _restate_gaps(metadata, f"(-1.00) {FIRST_TEXT}")


def _tenth_each(metadata: dict[str, Any]) -> None:
    """Deduct 0.10 for each of the two known gaps."""
    _restate_gaps(metadata, f"(-0.10) {FIRST_TEXT}", f"(-0.10) {SECOND_TEXT}")


def _wrong_total(metadata: dict[str, Any]) -> None:
    """Understate the first deduction so the amounts no longer sum to the score shortfall."""
    _restate_gaps(metadata, f"(-0.01) {FIRST_TEXT}", SECOND)


def _duplicate_gap(metadata: dict[str, Any]) -> None:
    """Repeat the first gap and its closure in place of the second."""
    metadata["confidence_gaps"][1] = FIRST
    metadata["confidence_gap_closures"][1] = dict(metadata["confidence_gap_closures"][0])


def _forged_rationale(metadata: dict[str, Any]) -> None:
    """Replace the first closure rationale with an unsupported approval claim."""
    metadata["confidence_gap_closures"][0]["rationale"] = "The PR is approved for merging."


def _foreign_gap(metadata: dict[str, Any]) -> None:
    """Replace the first gap with an unrelated approval claim."""
    _restate_gaps(metadata, "(-0.02) The PR is approved for merging.", SECOND)


def _residual_only(metadata: dict[str, Any]) -> None:
    """Replace both gaps with a single residual-uncertainty gap carrying the operator's subjective judgment."""
    gap = "(-0.05) Residual uncertainty in the failure report and its production and checks."
    metadata["confidence_gaps"] = [gap]
    metadata["confidence_gap_closures"] = [
        {
            "gap": gap,
            "status": "unresolved",
            "rationale": "The retained records do not establish every aspect of report correctness; this is the operator's subjective judgment, not a substitute for a known gap.",
        }
    ]


def _legacy_evidence(metadata: dict[str, Any]) -> None:
    """Replace the required confidence-meaning evidence line with a different claim."""
    metadata["confidence_recovery"]["evidence"][0] = "A different claim"


def _failed_check_for(score: float) -> str:
    """Return the extra failed check the finalizer is told about for this score."""
    return NOT_ACCEPTABLE if score <= 0.8 else ""


def _promote_two_gap_run(
    run_dir: Path,
    checkout_state: dict[str, Any] | None,
    mutate: Callable[[dict[str, Any]], None],
    score: float,
    *,
    command_failure: bool,
) -> subprocess.CompletedProcess[str]:
    """Write an unavailable run, give it the two-gap profile altered by `mutate`, and finalize with promotion."""
    result = json.loads(_write_unavailable_artifact(run_dir, checkout_state=checkout_state).read_text())
    metadata = result["metadata"]
    two_gap_baseline(metadata, score, checkout_started=checkout_state is not None)
    mutate(metadata)
    return promote_unavailable(
        run_dir,
        metadata,
        checkout_state,
        command_failure=command_failure,
        confidence=str(score),
        extra_checks_failed=_failed_check_for(score),
    )


class TestMultipleReportConfidenceGaps:
    """An unavailable review may report several diagnostic gaps only when each is honest, unique and summed."""

    @pytest.mark.parametrize(
        ("checkout_state", "mutate", "score", "in_final", "absent_from_final", "checkout_fields"),
        [
            pytest.param(CHECKOUT_STARTED, _no_change, 0.95, (FIRST, SECOND), (), {}, id="valid"),
            pytest.param(
                GH_CHECKOUT_RETAINED,
                _no_change,
                0.95,
                (FIRST_TEXT, "pr-error.txt", "checkout-state.json"),
                ("No command-failure diagnostic was retained",),
                {"gh_checkout_failure": GH_CHECKOUT_FAILURE},
                id="gh-checkout-retained",
            ),
            pytest.param(CHECKOUT_STARTED, _half_each, 0.0, (), (), {}, id="score-zero-two"),
            pytest.param(CHECKOUT_STARTED, _zero_gaps, 1.0, (), (), {}, id="zero"),
            pytest.param(CHECKOUT_STARTED, _known_zero_impact, 1.0, (), (), {}, id="zero-known"),
            pytest.param(CHECKOUT_STARTED, _tenth_each, 0.8, (), (), {}, id="low"),
            pytest.param(CHECKOUT_STARTED, _residual_only, 0.95, (), (), {}, id="residual"),
        ],
    )
    def test_accepts_honest_gaps_and_blocks_intake(
        self,
        tmp_path: Path,
        checkout_state: dict[str, Any],
        mutate: Callable[[dict[str, Any]], None],
        score: float,
        in_final: tuple[str, ...],
        absent_from_final: tuple[str, ...],
        checkout_fields: dict[str, Any],
    ) -> None:
        """Honest gap sets promote as unavailable, render into the final report, and never feed remediation.

        Covers the two-gap report, a retained gh checkout failure, whole-score and zero deductions, a low score that
        also fails the acceptability check, and a single residual-uncertainty gap.
        """
        completed = _promote_two_gap_run(tmp_path, checkout_state, mutate, score, command_failure=False)

        assert_promoted_unavailable_blocks_intake(tmp_path, completed)
        final = (tmp_path / "final.md").read_text()
        retained = json.loads((tmp_path / "checkout-state.json").read_text())
        failed = json.loads((tmp_path / "result.json").read_text())["checks_failed"]
        assert all(text in final for text in in_final)
        assert not any(text in final for text in absent_from_final)
        assert {key: retained[key] for key in checkout_fields} == checkout_fields
        assert "(-0.00) PR correctness was not assessed" in final
        assert f"{score:.2f}" in final
        assert score > 0.8 or NOT_ACCEPTABLE in failed

    @pytest.mark.parametrize(
        ("checkout_state", "mutate", "score", "command_failure"),
        [
            pytest.param(CHECKOUT_STARTED, _single_total_loss, 0.0, False, id="score-zero-one"),
            pytest.param(None, _no_change, 0.95, False, id="no-checkout"),
            pytest.param(CHECKOUT_STARTED, _wrong_total, 0.95, False, id="wrong-total"),
            pytest.param(CHECKOUT_STARTED, _duplicate_gap, 0.95, False, id="duplicate"),
            pytest.param(CHECKOUT_STARTED, _forged_rationale, 0.95, False, id="forged-rationale"),
            pytest.param(CHECKOUT_STARTED, _foreign_gap, 0.95, False, id="foreign-gap"),
            pytest.param(CHECKOUT_STARTED, _legacy_evidence, 0.95, False, id="legacy"),
            pytest.param(CHECKOUT_STARTED, _no_change, 0.95, True, id="both-diagnostics"),
        ],
    )
    def test_rejects_dishonest_or_mismatched_gaps(
        self,
        tmp_path: Path,
        checkout_state: dict[str, Any] | None,
        mutate: Callable[[dict[str, Any]], None],
        score: float,
        command_failure: bool,
    ) -> None:
        """Gap sets that misstate the retained diagnostics are refused and leave no promoted result.

        A single whole-score gap, gaps naming a checkout that was never started or a missing command diagnostic that
        exists, deductions not summing to the shortfall, duplicates, forged or foreign claims, and the legacy evidence
        wording must each fail finalization instead of promoting.
        """
        completed = _promote_two_gap_run(tmp_path, checkout_state, mutate, score, command_failure=command_failure)

        assert completed.returncode != 0, completed.stderr + completed.stdout
        assert not (tmp_path / "result.json").exists()
