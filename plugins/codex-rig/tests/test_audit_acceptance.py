"""Reject unsupported audit completion and optimization acceptance claims."""

from __future__ import annotations

import copy
import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_gate_source import _python_command

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
GATES = ("lint", "format", "types", "tests", "review")


def _evidence(directory: Path, name: str, payload: object) -> dict[str, str]:
    """Retain a comparison record with a digest of its exact bytes."""
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    (directory / name).write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


@pytest.fixture
def audit_run(tmp_path: Path) -> tuple[Any, Path, dict[str, Any]]:
    """Prepare a complete historical audit with executed review and retained cost evidence."""
    validator = runpy.run_path(str(PLUGIN_ROOT / "shared" / "validate-artifacts.py"))
    arguments = [sys.executable, str(PLUGIN_ROOT / "shared" / "run_gates.py"), "--out", str(tmp_path)]
    command = _python_command("print(1)")
    for gate in GATES:
        arguments.extend((f"--{gate}", command))
    subprocess.run(arguments, capture_output=True, check=True)
    for name, sections in validator["SKILL_REQUIREMENTS"]["audit"]["files"].items():
        (tmp_path / name).write_text(
            "\n\n".join(f"## {section}\n\nRetained local evidence." for section in sections), encoding="utf-8"
        )
    baseline, candidate = "a" * 64, "b" * 64
    identity = {"model": "same-model", "effort": "high", "task_contract_sha256": "c" * 64, "prompt_sha256": "d" * 64}
    value = {
        "schema_version": 2,
        "status": "accepted",
        "scope_roots": ["skills"],
        "baseline_sha256": baseline,
        "candidate_sha256": candidate,
        "static_measurements": [
            {
                "source": "tiktoken:o200k_base",
                "baseline_sha256": baseline,
                "candidate_sha256": candidate,
                "baseline_tokens": 100,
                "candidate_tokens": 90,
            }
        ],
        "conditional_load_trace": ["Both exercised paths include all required referenced contracts."],
        "obligation_map_path": "obligations.json",
        "static_gates": [
            {
                "id": name,
                "status": "pass",
                "evidence": _evidence(
                    tmp_path,
                    f"{name}.json",
                    {"status": "pass", "baseline_sha256": baseline, "candidate_sha256": candidate},
                ),
            }
            for name in ("package", "tests", "calibration", "contract-markers", "adversarial-review")
        ],
        "behavioral_comparison": _evidence(
            tmp_path,
            "behavior.json",
            {
                "baseline_sha256": baseline,
                "candidate_sha256": candidate,
                "critical_regressions": 0,
                "baseline_failures": 0,
                "candidate_failures": 0,
                "tasks": [
                    {
                        "id": "matched-task",
                        "baseline": {
                            "completion_quality": 1,
                            "tool_failures": 0,
                            "check_failures": 0,
                            "evidence": ["Baseline task transcript and assessment."],
                        },
                        "candidate": {
                            "completion_quality": 1,
                            "tool_failures": 0,
                            "check_failures": 0,
                            "evidence": ["Candidate task transcript and assessment."],
                        },
                    }
                ],
            },
        ),
        "live_comparison": _evidence(
            tmp_path,
            "live.json",
            {
                "baseline_sha256": baseline,
                "candidate_sha256": candidate,
                "baseline_identity": identity,
                "candidate_identity": identity,
                "source": "provider-native",
                "baseline_cost": 10,
                "candidate_cost": 9,
                "min_cost_reduction": 0.05,
            },
        ),
        "decision": "All retained guards passed.",
        "residual_limits": ["Evidence truth remains workflow-owner responsibility."],
    }
    _evidence(
        tmp_path,
        "obligations.json",
        [
            {
                "baseline": "required approval",
                "candidate": "same required approval",
                "evidence": "contract-markers.json",
                "preserved": True,
            }
        ],
    )
    value["obligation_map_sha256"] = hashlib.sha256((tmp_path / "obligations.json").read_bytes()).hexdigest()
    result = {
        "status": "pass",
        "checks_run": list(GATES),
        "checks_failed": [],
        "findings": {name: 0 for name in ("critical", "high", "medium", "low")},
        "confidence": 0.95,
        "artifact_path": str(tmp_path / "result.json"),
        "metadata": {
            "value_per_token": value,
            "confidence_gaps": ["Evidence truth remains workflow-owner responsibility."],
            "confidence_gap_closures": [
                {
                    "gap": "Evidence truth remains workflow-owner responsibility.",
                    "status": "deferred",
                    "rationale": "Offline validation cannot authenticate provider provenance.",
                }
            ],
            "confidence_recovery": {
                "initial_confidence": 0.95,
                "final_confidence": 0.95,
                "status": "fair",
                "evidence": ["retained artifacts"],
                "recovery_actions": ["Check retained comparison digests."],
                "remaining_limits": ["Evidence truth remains workflow-owner responsibility."],
            },
        },
    }
    return validator["validate"], tmp_path, result


def _validate(run: tuple[Any, Path, dict[str, Any]]) -> None:
    """Exercise the public validator with one retained audit result."""
    validate, directory, result = run
    path = directory / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    validate("audit", directory, path)


def test_accepts_retained_matched_audit_evidence(audit_run: tuple[Any, Path, dict[str, Any]]) -> None:
    """Permit a fully matched finite comparison with every hard guard passed."""
    _validate(audit_run)


@pytest.mark.parametrize(
    "damage",
    [
        "missing-tasks",
        "empty-tasks",
        "missing-baseline",
        "missing-candidate",
        "missing-quality",
        "missing-tools",
        "missing-checks",
        "missing-transcript",
        "tool-regression",
        "check-regression",
        "quality-regression",
        "bool-quality",
        "nan-quality",
        "unbounded-quality",
        "duplicate-task",
    ],
)
def test_rejects_omitted_or_regressed_paired_task_guards(
    audit_run: tuple[Any, Path, dict[str, Any]], damage: str
) -> None:
    """Prevent aggregate totals from concealing absent or degraded per-task evidence."""
    _, directory, result = audit_run
    proof = json.loads((directory / "behavior.json").read_text(encoding="utf-8"))
    task = proof["tasks"][0]
    if damage == "missing-tasks":
        del proof["tasks"]
    elif damage == "empty-tasks":
        proof["tasks"] = []
    elif damage.startswith("missing-"):
        field = damage.removeprefix("missing-")
        if field in {"baseline", "candidate"}:
            del task[field]
        else:
            del task["candidate"][
                {
                    "quality": "completion_quality",
                    "tools": "tool_failures",
                    "checks": "check_failures",
                    "transcript": "evidence",
                }[field]
            ]
    elif damage == "duplicate-task":
        proof["tasks"].append(copy.deepcopy(task))
    else:
        field, value = {
            "tool-regression": ("tool_failures", 1),
            "check-regression": ("check_failures", 1),
            "quality-regression": ("completion_quality", 0.9),
            "bool-quality": ("completion_quality", True),
            "nan-quality": ("completion_quality", float("nan")),
            "unbounded-quality": ("completion_quality", 2),
        }[damage]
        task["candidate"][field] = value
    result["metadata"]["value_per_token"]["behavioral_comparison"] = _evidence(directory, "behavior.json", proof)

    with pytest.raises(SystemExit, match="audit-cost-"):
        _validate(audit_run)


@pytest.mark.parametrize(
    "field",
    [
        "baseline_sha256",
        "candidate_sha256",
        "scope_roots",
        "static_measurements",
        "conditional_load_trace",
        "obligation_map_path",
        "obligation_map_sha256",
        "static_gates",
        "behavioral_comparison",
        "live_comparison",
        "decision",
        "residual_limits",
    ],
)
def test_rejects_missing_acceptance_evidence(audit_run: tuple[Any, Path, dict[str, Any]], field: str) -> None:
    """Reject an accepted claim when any required evidence component is absent."""
    del audit_run[2]["metadata"]["value_per_token"][field]
    with pytest.raises(SystemExit, match="audit-cost-"):
        _validate(audit_run)


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "failed-guard",
        "tampered-proof",
        "obligation-loss",
        "proxy",
        "mismatched-prompt",
        "regression",
        "nan-cost",
        "negative-cost",
        "bool-cost",
        "insufficient-saving",
    ],
)
def test_rejects_invalid_acceptance_evidence(audit_run: tuple[Any, Path, dict[str, Any]], damage: str) -> None:
    """Reject dishonest guards, unmatched comparisons, invalid costs, and inadequate savings."""
    _, directory, result = audit_run
    value = result["metadata"]["value_per_token"]
    if damage == "schema":
        value["schema_version"] = 1
    elif damage == "failed-guard":
        value["static_gates"][0]["status"] = "fail"
    elif damage == "tampered-proof":
        (directory / "live.json").write_text("{}", encoding="utf-8")
    elif damage == "obligation-loss":
        _evidence(directory, "obligations.json", [{"preserved": False}])
    elif damage == "proxy":
        value["static_measurements"][0]["source"] = "utf8-bytes"
    else:
        name = "behavior.json" if damage == "regression" else "live.json"
        proof = json.loads((directory / name).read_text(encoding="utf-8"))
        if damage == "mismatched-prompt":
            proof["candidate_identity"] = copy.deepcopy(proof["candidate_identity"])
            proof["candidate_identity"]["prompt_sha256"] = "e" * 64
        elif damage == "regression":
            proof["candidate_failures"] = 1
        else:
            proof["candidate_cost"] = {
                "nan-cost": float("nan"),
                "negative-cost": -1,
                "bool-cost": True,
                "insufficient-saving": 9.9,
            }[damage]
        value["behavioral_comparison" if damage == "regression" else "live_comparison"] = _evidence(
            directory, name, proof
        )
    with pytest.raises(SystemExit, match="audit-cost-"):
        _validate(audit_run)


def test_successful_audit_requires_executed_review(audit_run: tuple[Any, Path, dict[str, Any]]) -> None:
    """Keep generic skipped-gate accounting from certifying an unassessed audit."""
    _, directory, result = audit_run
    result["metadata"]["value_per_token"]["status"] = "not-run"
    gates = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
    for gate in gates["checks"]:
        gate.update(status="not-applicable", reason="Unavailable diagnostic.", exit_code=0)
    (directory / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    with pytest.raises(SystemExit, match="audit-review-gate-required"):
        _validate(audit_run)


@pytest.mark.parametrize("status", ["not-run", "insufficient-evidence", "rejected"])
def test_honest_unaccepted_comparison_remains_valid(audit_run: tuple[Any, Path, dict[str, Any]], status: str) -> None:
    """Allow assessed audits to disclose unavailable or rejected optimization evidence."""
    audit_run[2]["metadata"]["value_per_token"] = {"schema_version": 2, "status": status}
    _validate(audit_run)


def test_unchanged_snapshot_cannot_certify_optimization(audit_run: tuple[Any, Path, dict[str, Any]]) -> None:
    """Reject a positive cost claim when every baseline and candidate snapshot is identical."""
    _, directory, result = audit_run
    value = result["metadata"]["value_per_token"]
    value["candidate_sha256"] = value["baseline_sha256"]
    value["static_measurements"][0]["candidate_sha256"] = value["baseline_sha256"]
    for record in value["static_gates"]:
        name = record["evidence"]["path"]
        proof = json.loads((directory / name).read_text(encoding="utf-8"))
        proof["candidate_sha256"] = value["baseline_sha256"]
        record["evidence"] = _evidence(directory, name, proof)
    for field in ("behavioral_comparison", "live_comparison"):
        name = value[field]["path"]
        proof = json.loads((directory / name).read_text(encoding="utf-8"))
        proof["candidate_sha256"] = value["baseline_sha256"]
        value[field] = _evidence(directory, name, proof)
    with pytest.raises(SystemExit, match="audit-cost-unchanged-snapshot"):
        _validate(audit_run)
