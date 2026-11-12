"""Require gate receipts to retain their recorded bytes and executed test evidence."""

import hashlib
import json
from pathlib import Path

import pytest
from _modules import load_module

VALIDATOR = load_module("gate_evidence_validator", Path(__file__).resolve().parents[3] / "shared/validate-artifacts.py")
GATE_IDS = ["lint", "format", "types", "tests", "review"]


def _write_json(path, value):
    """Write a deterministic JSON fixture and return its exact digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def local_gates(tmp_path):
    """Retain local gate source bindings, the receipt, snapshot and three logs for each check."""
    mirror = tmp_path / "local-source-review-worktree"
    source = {
        "mode": "local-review-mirror",
        "worktree": mirror.as_posix(),
        "receipt_path": "local-source/review-worktree.json",
        "snapshot_path": "checks/local-source-snapshot.json",
    }
    source["receipt_sha256"] = _write_json(
        tmp_path / source["receipt_path"],
        {
            "schema_version": 2,
            "diff_format": "canonical-v1",
            "review_worktree": mirror.as_posix(),
            "revision": "a" * 40,
        },
    )
    source["snapshot_sha256"] = _write_json(
        tmp_path / source["snapshot_path"], {"repository": mirror.as_posix(), "revision": "a" * 40, "files": []}
    )
    checks = []
    for gate_id in GATE_IDS:
        logs = {
            key: f"checks/{gate_id}.{name}.txt"
            for key, name in (("command_path", "command"), ("stdout", "stdout"), ("stderr", "stderr"))
        }
        artifacts = {}
        for key, relative in logs.items():
            path = tmp_path / relative
            path.write_text("python -m pytest\n" if key == "command_path" else "", encoding="utf-8")
            artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        observation = {"snapshot_sha256": source["snapshot_sha256"]}
        checks.append(
            {
                "id": gate_id,
                "status": "pass",
                "exit_code": 0,
                "duration_seconds": 1,
                **logs,
                "source": {**source, "before": observation, "after": observation, "artifacts": artifacts},
            }
        )
    gates = {"status": "pass", "checks_failed": [], "checks": checks, "source": source}
    _write_json(tmp_path / "gates.json", gates)
    return tmp_path, gates


@pytest.mark.parametrize(
    "relative", ["checks/tests.stdout.txt", "local-source/review-worktree.json", "checks/local-source-snapshot.json"]
)
def test_validator_rejects_changed_gate_artifacts(local_gates, relative):
    """Fail validation when a log, receipt or frozen snapshot changes after the gate records its digest."""
    output, _ = local_gates
    assert VALIDATOR._validate_gates(output)["status"] == "pass"
    (output / relative).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="gate-evidence"):
        VALIDATOR._validate_gates(output)


def test_validator_rejects_removed_artifact_hashes(local_gates):
    """Do not allow a local gate to bypass hash validation by deleting its artifact map."""
    output, gates = local_gates
    gates["checks"][3]["source"].pop("artifacts")
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence"):
        VALIDATOR._validate_gates(output)


def test_runtime_snapshot_requires_the_top_level_local_source(local_gates):
    """A runner-owned snapshot prevents removing the source binding from an executed local gate run."""
    output, gates = local_gates
    gates.pop("source")
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-local-source-missing"):
        VALIDATOR._validate_gates(output)


def test_local_receipt_accepts_the_top_level_project_environment(local_gates):
    """The runner adds the project environment only at top level after constructing per-check source receipts."""
    output, gates = local_gates
    gates["source"]["project_env"] = (output / "environment").as_posix()
    _write_json(output / "gates.json", gates)
    assert VALIDATOR._validate_gates(output)["status"] == "pass"


def test_validator_rejects_a_removed_local_source_mode(local_gates):
    """Keep each executed gate bound to the declared local mirror even if its mode field is removed."""
    output, gates = local_gates
    gates["checks"][3]["source"].pop("mode")
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-local-source-mismatch"):
        VALIDATOR._validate_gates(output)


def test_validator_rejects_a_changed_snapshot_observation(local_gates):
    """A passing gate must observe the admitted snapshot both before and after execution."""
    output, gates = local_gates
    gates["checks"][3]["source"]["after"] = {"snapshot_sha256": "b" * 64}
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-local-observation-mismatch"):
        VALIDATOR._validate_gates(output)


def test_validator_binds_python_proof_to_the_hashed_sidecar(local_gates):
    """Do not accept an inline passing proof that disagrees with the unchanged retained sidecar."""
    output, gates = local_gates
    proof = {"status": "fail"}
    relative = "checks/tests.python-imports.json"
    digest = _write_json(output / relative, proof)
    check = gates["checks"][3]
    check["python_imports"] = {"status": "pass"}
    check["source"]["artifacts"][relative] = digest
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-python-proof-mismatch"):
        VALIDATOR._validate_gates(output)


def test_retained_python_proof_cannot_be_removed_from_the_gate(local_gates):
    """A retained import-origin proof cannot be ignored by stripping both inline proof and its hash entry."""
    output, gates = local_gates
    _write_json(output / "checks/tests.python-imports.json", {"status": "fail"})
    _write_json(output / "gates.json", gates)
    with pytest.raises(SystemExit, match="gate-evidence-python-proof-mismatch"):
        VALIDATOR._validate_gates(output)


def test_pass_requires_an_executed_gate():
    """Five skipped gates cannot justify a passing result."""
    gates = {
        "status": "pass",
        "checks_failed": [],
        "checks": [{"id": gate_id, "status": "not-applicable"} for gate_id in GATE_IDS],
    }
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": []}
    with pytest.raises(SystemExit, match="result-pass-without-executed-gates"):
        VALIDATOR._reconcile_result_with_gates(result, gates)


def test_terminal_review_can_retain_inapplicable_code_tests():
    """A completed terminal review still passes when code tests were intentionally inapplicable."""
    gates = {
        "status": "pass",
        "checks_failed": [],
        "checks": [
            {"id": gate_id, "status": "pass" if gate_id == "review" else "not-applicable"} for gate_id in GATE_IDS
        ],
    }
    VALIDATOR._reconcile_result_with_gates(
        {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {"review_status": "closed"}},
        gates,
        "code-review",
    )


def test_incomplete_result_can_retain_skipped_test_gate():
    """An honest failed result may keep tests unexecuted while preserving the verification gap."""
    gates = {
        "status": "pass",
        "checks_failed": [],
        "checks": [{"id": gate_id, "status": "not-applicable"} for gate_id in GATE_IDS],
    }
    VALIDATOR._reconcile_result_with_gates({"status": "fail", "checks_run": GATE_IDS, "checks_failed": []}, gates)
