"""Regression coverage for terminal unavailable review-result candidates."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WRITE_RESULT = PLUGIN_ROOT / "shared" / "write-result.py"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
FINALIZER = PLUGIN_ROOT / "shared" / "final_handoff.py"
SHARED_VALIDATOR = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
COLLECTION_FAILURE = "github-network:gh-pr-view"
CONFIDENCE_GAP = "Core PR source verification did not complete; no source review or merge decision was made."


def _load_validator() -> object:
    """Load the standalone code-review validator from its shipped location."""
    specification = importlib.util.spec_from_file_location("code_review_validator", REVIEW_VALIDATOR)
    assert specification is not None and specification.loader is not None
    validator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(validator)
    return validator


def _load_module(path: Path, name: str) -> object:
    """Load one shipped helper without changing the process import path."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


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
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}),
        encoding="utf-8",
    )
    (run_dir / "pr-error.txt").write_text(COLLECTION_FAILURE + "\n", encoding="utf-8")
    (run_dir / "pr-target.txt").write_text("123\n", encoding="utf-8")
    (run_dir / "review-notes.md").write_text(
        "# PR Review Availability: unavailable\n\n"
        "Source findings: not assessed\n\n"
        "Merge decision: not made\n\n"
        "Process diagnostic: `github-network:gh-pr-view`. This is a workflow/integration failure, not a PR finding or merge block.\n\n"
        "Recovery: Retry the unchanged collector later; no review or merge decision was made.\n\n"
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
                "rationale": "Core source verification did not complete; retained collection artifacts may be partial and were not assessed.",
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
            "remaining_limits": ["PR correctness was not assessed."],
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


def test_write_result_unavailable_review_emits_validator_accepted_candidate(tmp_path: Path) -> None:
    """Prevent unavailable PR collection failures from receiving assessed-review result fields."""
    metadata = _write_unavailable_pr_evidence(tmp_path)
    candidate_path = tmp_path / "result.candidate.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(WRITE_RESULT),
            "--out",
            str(candidate_path),
            "--gates",
            str(tmp_path / "gates.json"),
            "--status",
            "fail",
            "--checks-run",
            ",".join(GATE_IDS),
            "--confidence",
            "0.9",
            "--artifact-path",
            str(tmp_path / "result.json"),
            "--metadata",
            json.dumps(metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate["schema_version"] == 2
    assert candidate["status"] == "fail"
    assert candidate["findings"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}
    assert candidate["metadata"]["review_status"] == "unavailable"
    assert candidate["metadata"]["collection_failure"] == {"code": COLLECTION_FAILURE, "artifact": "pr-error.txt"}
    assert "recommendations" not in candidate
    assert "follow_up" not in candidate

    _load_validator()._validate_result(tmp_path, candidate_path, tmp_path, "thread", tmp_path)

    result_path = tmp_path / "result.json"
    handoff_path = tmp_path / "final-handoff.json"
    final_path = tmp_path / "final.md"
    validation_path = tmp_path / "final-handoff.validation.json"
    recovery = candidate["metadata"]["confidence_recovery"]
    closures = candidate["metadata"]["confidence_gap_closures"]
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
        "confidence": {"score": 0.9, "band": "fair", "limits": recovery["remaining_limits"], "gaps": closures},
        "artifacts": [
            {"label": "Collection failure", "path": "pr-error.txt"},
            {"label": "Result", "path": str(result_path)},
        ],
        "caller_contract": None,
    }
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    finalizer = _load_module(FINALIZER, "unavailable_finalizer")
    validation = finalizer.render_files(handoff_path, final_path, validation_path)
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

    _load_validator()._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
    _load_module(SHARED_VALIDATOR, "unavailable_shared_validator").validate("code-review", tmp_path, result_path)

    gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))
    gates["checks"][0]["status"] = "pass"
    gates["checks"][0].pop("reason")
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")

    with pytest.raises(SystemExit, match="unavailable-review-gates-must-be-not-applicable"):
        _load_validator()._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
    with pytest.raises(SystemExit, match="code-review-unavailable-gates-must-be-not-applicable"):
        _load_module(SHARED_VALIDATOR, "unavailable_shared_validator_rejects_pass").validate(
            "code-review", tmp_path, result_path
        )
