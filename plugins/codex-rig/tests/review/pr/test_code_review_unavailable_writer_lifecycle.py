"""Regression coverage for terminal unavailable review-result candidates.

The real result writer, handoff finalizer and both artifact validators run over a tmp-dir fixture of an unavailable PR
collection, so a candidate that the writer emits must also survive finalization and validator re-checks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
WRITE_RESULT = PLUGIN_ROOT / "shared" / "write-result.py"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
FINALIZER = PLUGIN_ROOT / "shared" / "final_handoff.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
COLLECTION_FAILURE = "github-network:gh-pr-view"
CONFIDENCE_GAP = "Core PR source verification did not complete; no source review or merge decision was made."


@dataclass
class Finalized:
    """Paths and handoff of an unavailable result that went through the real finalizer."""

    result_path: Path
    final_path: Path
    handoff: dict[str, object]


def _check_review(module: ModuleType, run_dir: Path, result_path: Path) -> None:
    """Run the code-review validator's result check against one run directory."""
    module._validate_result(run_dir, result_path, run_dir, "thread", run_dir)


def _check_shared(module: ModuleType, run_dir: Path, result_path: Path) -> None:
    """Run the shared artifact validator's code-review check against one run directory."""
    module.validate("code-review", run_dir, result_path)


def _write_unavailable_pr_evidence(run_dir: Path) -> dict[str, object]:
    """Write the operational evidence required before an unavailable candidate is created."""
    checks = []
    for gate_id in GATE_IDS:
        for suffix in ("command.txt", "stdout.txt", "stderr.txt"):
            (run_dir / f"{gate_id}.{suffix}").write_text("", encoding="utf-8")
        checks.append(
            {
                "id": gate_id,
                "status": "not-applicable",
                "exit_code": 0,
                "duration_seconds": 0.0,
                "command_path": f"{gate_id}.command.txt",
                "stdout": f"{gate_id}.stdout.txt",
                "stderr": f"{gate_id}.stderr.txt",
                "reason": "Source collection stopped before the PR verification gates ran.",
            }
        )
    (run_dir / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8"
    )
    (run_dir / "pr-error.txt").write_text(COLLECTION_FAILURE + "\n", encoding="utf-8")
    (run_dir / "pr-target.txt").write_text("123\n", encoding="utf-8")
    (run_dir / "review-notes.md").write_text(
        "# PR Review Availability: unavailable\n\n"
        "Source findings: not assessed\n\n"
        "Merge decision: not made\n\n"
        "Process diagnostic: `github-network:gh-pr-view`. This is a workflow/integration failure, not a PR finding or merge block.\n\n"
        "Recovery: Check effective runtime access; if required access is missing or unknown and requests are allowed, request runtime approval for the complete collector. Respect an explicit denial or non-overridable restriction; retry only after approval or an evidenced state change.\n\n"
        "Evidence: `pr-error.txt`.\n",
        encoding="utf-8",
    )
    return {
        "scope": "pr",
        "risk_tier": "HIGH_RISK",
        "review_status": "unavailable",
        "collection_failure": {"code": COLLECTION_FAILURE, "artifact": "pr-error.txt"},
        "confidence_gaps": [CONFIDENCE_GAP],
        "confidence_gap_closures": [
            {
                "gap": CONFIDENCE_GAP,
                "status": "unresolved",
                "rationale": "(-0.10) Core source verification did not complete; retained collection artifacts may be partial and were not assessed.",
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.9,
            "final_confidence": 0.9,
            "status": "fair",
            "evidence": [
                "The classified collection failure and any current-attempt collector artifacts were retained."
            ],
            "recovery_actions": ["Stopped before source review."],
            "remaining_limits": [
                "(-0.00) PR correctness was not assessed. No additional deduction; "
                "the canonical source-verification gap accounts for this limitation."
            ],
        },
        "final_handoff": {
            "schema_version": 1,
            "handoff_path": str(run_dir / "final-handoff.json"),
            "handoff_sha256": "a" * 64,
            "rendered_path": str(run_dir / "final.md"),
            "rendered_sha256": "b" * 64,
            "validation_path": str(run_dir / "final-handoff.validation.json"),
            "branch": "unavailable",
        },
    }


def _run_write_result(run_dir: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the real result writer over unavailable-PR evidence; return its process and candidate path."""
    metadata = _write_unavailable_pr_evidence(run_dir)
    candidate_path = run_dir / "result.candidate.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(WRITE_RESULT),
            "--out",
            str(candidate_path),
            "--gates",
            str(run_dir / "gates.json"),
            "--status",
            "fail",
            "--checks-run",
            ",".join(GATE_IDS),
            "--confidence",
            "0.9",
            "--artifact-path",
            str(run_dir / "result.json"),
            "--metadata",
            json.dumps(metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, candidate_path


@pytest.fixture
def finalized(tmp_path: Path) -> Finalized:
    """Unavailable result promoted from the writer's candidate through the real handoff finalizer."""
    completed, candidate_path = _run_write_result(tmp_path)
    assert completed.returncode == 0, completed.stderr
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    result_path = tmp_path / "result.json"
    handoff_path = tmp_path / "final-handoff.json"
    final_path = tmp_path / "final.md"
    validation_path = tmp_path / "final-handoff.validation.json"
    handoff = {
        "schema_version": 1,
        "presentation_version": 2,
        "skill": "code-review",
        "branch": "unavailable",
        "outcome": {
            "title": "PR Review Availability",
            "summary": (
                "I could not retrieve the PR metadata, so the review has not started. "
                "Reason: `github-network:gh-pr-view`. The collector did not retain a more specific cause."
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
                "next_action": "Inspect the classified `gh-pr-view` collector failure before choosing recovery. Resume only after a fresh collector run produces and validates the PR source bundle.",
            }
        ],
        "next_steps": ["collection-recovery"],
        "confidence": {
            "score": 0.9,
            "band": "fair",
            "limits": candidate["metadata"]["confidence_recovery"]["remaining_limits"],
            "gaps": candidate["metadata"]["confidence_gap_closures"],
        },
        "artifacts": [
            {"label": "Collection failure", "path": "pr-error.txt"},
            {"label": "Result", "path": str(result_path)},
        ],
        "caller_contract": None,
    }
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = load_module("unavailable_finalizer", FINALIZER).render_files(handoff_path, final_path, validation_path)
    candidate["metadata"]["final_handoff"] = {
        "schema_version": 1,
        "handoff_path": str(handoff_path),
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": str(final_path),
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": str(validation_path),
        "branch": "unavailable",
    }
    result_path.write_text(json.dumps(candidate), encoding="utf-8")
    return Finalized(result_path, final_path, handoff)


@pytest.mark.integration
def test_write_result_emits_validator_accepted_unavailable_candidate(tmp_path: Path) -> None:
    """An unavailable PR collection failure gets no assessed-review result fields and passes the validator.

    The real writer runs over a fixture whose collector stopped before any gate ran; its candidate must carry zero
    findings, the classified failure and the closure provenance, and no recommendation or follow-up.
    """
    completed, candidate_path = _run_write_result(tmp_path)

    assert completed.returncode == 0, completed.stderr
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["schema_version"] == 3
    assert candidate["status"] == "fail"
    assert candidate["findings"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}
    assert candidate["metadata"]["review_status"] == "unavailable"
    assert candidate["metadata"]["collection_failure"] == {"code": COLLECTION_FAILURE, "artifact": "pr-error.txt"}
    assert candidate["metadata"]["confidence_gap_closures"][0]["gap"] == CONFIDENCE_GAP
    assert "recommendations" not in candidate
    assert "follow_up" not in candidate
    _check_review(load_module("code_review_validator", REVIEW_VALIDATOR), tmp_path, candidate_path)


@pytest.mark.integration
def test_finalized_unavailable_result_renders_confidence_accounting(tmp_path: Path, finalized: Finalized) -> None:
    """A finalized unavailable result passes both validators and renders the visible confidence deductions.

    The writer's candidate is finalized into the rendered handoff; the gap, its `(-0.10)` closure and the `(-0.00)`
    limit with its no-additional-deduction note must all appear in the user-facing text.
    """
    _check_review(load_module("code_review_validator", REVIEW_VALIDATOR), tmp_path, finalized.result_path)
    _check_shared(load_module("unavailable_shared_validator", SHARED_VALIDATOR), tmp_path, finalized.result_path)

    rendered = finalized.final_path.read_text(encoding="utf-8")
    assert "0.90 (fair)." in rendered
    assert CONFIDENCE_GAP in rendered
    assert finalized.handoff["confidence"]["gaps"][0]["gap"] == CONFIDENCE_GAP
    assert f"Gap [unresolved]: {CONFIDENCE_GAP} — (-0.10) " in rendered
    assert "Limits: (-0.00) PR correctness was not assessed." in rendered
    assert "No additional deduction" in rendered


@pytest.mark.integration
@pytest.mark.parametrize(
    ("validator_path", "check", "error"),
    [
        pytest.param(
            REVIEW_VALIDATOR, _check_review, "unavailable-review-gates-must-be-not-applicable", id="code-review"
        ),
        pytest.param(
            SHARED_VALIDATOR, _check_shared, "code-review-unavailable-gates-must-be-not-applicable", id="shared"
        ),
    ],
)
def test_validator_rejects_passing_gate_in_unavailable_review(
    tmp_path: Path,
    finalized: Finalized,
    validator_path: Path,
    check: Callable[[ModuleType, Path, Path], None],
    error: str,
) -> None:
    """An unavailable review whose gate claims a pass is rejected, since no gate ran.

    The finalized result is valid until `gates.json` is rewritten to report one gate as passed; both validators must
    then refuse it so a collection failure can never masquerade as a verified review.
    """
    gates_path = tmp_path / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    gates["checks"][0]["status"] = "pass"
    gates["checks"][0].pop("reason")
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    module = load_module(f"unavailable_rejects_pass_{validator_path.stem}", validator_path)

    with pytest.raises(SystemExit, match=error):
        check(module, tmp_path, finalized.result_path)
