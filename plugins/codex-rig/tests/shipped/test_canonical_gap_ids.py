"""Exercise Codex gap identity from retained gate and confidence evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _fake_tools import fake_bin
from _modules import load_module_scoped


def test_gate_ids_use_distribution_names_and_name_skipped_workflow(monkeypatch):
    """A package gap follows a failed attempt, and a skipped attempt names the gate."""
    path = Path(__file__).resolve().parents[2] / "shared" / "run_gap_records.py"
    records = load_module_scoped("run_gap_records", path, monkeypatch)
    row = {"id": "tests", "status": "fail", "cause": "missing-python-package", "missing_modules": ["cv2", "unknown"]}
    (draft,) = records.gate_drafts({"checks": [row]})[0]
    assert (draft.id, draft.gap_class) == ("provisioning-not-attempted-tests", "process")
    assert [draft.id for draft in records.gate_drafts({"checks": [row], "project_env": "/env"})[0]] == [
        "provisioning-failed-opencv-python",
        "provisioning-failed-unknown",
    ]


def test_confidence_and_gate_refusal_share_cross_harness_ids(monkeypatch):
    """Known confidence descriptions and refusal rows resolve to the published ids."""
    path = Path(__file__).resolve().parents[2] / "shared" / "run_gap_records.py"
    records = load_module_scoped("run_gap_records", path, monkeypatch)
    descriptions = [
        "Verification readiness is incomplete",
        "Unreviewed dimension: API stability",
        "Full test suite was not run",
        "Push did not land",
        "Codex co-review absent: quota-exhausted",
        "PR code was not executed",
        "An unusual limitation",
    ]
    assert [draft.id for draft in records.confidence_gap_drafts({"confidence_gaps": descriptions})[0]] == [
        "readiness-incomplete",
        "unreviewed-dimension-api-stability",
        "full-suite-not-run",
        "push-not-landed",
        "codex-co-review-quota-exhausted",
        "pr-code-not-executed",
        records.text_gap_id(descriptions[-1]),
    ]
    row = {"id": "tests", "status": "refused", "cause": "sandbox-refused"}
    refusal = {"cause": "source-dirty", "reason": "unexpected tracked changes"}
    (draft,) = records.gate_drafts({"checks": [row], "refusal": refusal})[0]
    assert draft.id == "pr-code-not-executed"
    assert "sandbox-refused" in draft.summary
    assert "source-dirty" in draft.summary


@pytest.mark.parametrize(
    ("output_name", "install_exit", "status", "expected"),
    [
        pytest.param(
            "apply-result.json",
            1,
            "failed",
            [("provisioning-failed-opencv-python", "gate-provisioning.json")],
            id="failed-install-with-custom-output",
        ),
        pytest.param("gate-provisioning.json", 0, "ready", [], id="ready-with-canonical-output"),
    ],
)
def test_apply_retains_the_gate_provisioning_receipt(
    tmp_path, monkeypatch, output_name, install_exit, status, expected
):
    """The apply CLI preserves its result and the promoted ledger consumes its receipt."""
    shared = Path(__file__).resolve().parents[2] / "shared"
    monkeypatch.syspath_prepend(str(shared))
    helper = load_module_scoped("gate_env_provision", shared / "gate_env_provision.py", monkeypatch)
    records = load_module_scoped("run_gap_records", shared / "run_gap_records.py", monkeypatch)
    for name in helper.USER_ROUTING_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "demo"\ndependencies = ["numpy"]\n', encoding="utf-8")
    identity = ["-c", "user.name=Codex Rig Test", "-c", "user.email=codex-rig@example.invalid"]
    for command in (["init", "-q"], ["add", "pyproject.toml"], [*identity, "commit", "-qm", "base"]):
        subprocess.run(["git", *command], cwd=checkout, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True)
    tools = fake_bin(tmp_path / "bin", "uv", exit_code=install_exit)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tools), os.environ.get("PATH", ""))))
    plan = tmp_path / "plan.json"
    sha = head.stdout.strip()
    inputs = [
        "--repo", str(checkout), "--base", sha, "--head", sha, "--python", sys.executable,
        "--env-root", str(tmp_path / "envs"),
    ]  # fmt: skip
    assert helper.main(["plan", *inputs, "--out", str(plan)]) == 0
    digest = json.loads(plan.read_bytes())["plan_digest"]
    run = tmp_path / "review-run"
    output = run / output_name

    assert helper.main(["apply", *inputs, "--plan", str(plan), "--plan-digest", digest, "--out", str(output)]) == (
        install_exit
    )

    receipt = run / "gate-provisioning.json"
    assert receipt.read_bytes() == output.read_bytes()
    result = json.loads(receipt.read_bytes())
    assert (result["status"], result["steps"][-1]["id"], result["steps"][-1]["exit_code"]) == (
        status,
        "install",
        install_exit,
    )
    (run / "pr-routing.json").write_text(
        json.dumps({"base_repo": "example/project", "pr_number": 1, "head_oid": "a" * 40}), encoding="utf-8"
    )
    (run / "result.json").write_text(json.dumps({"metadata": {"execution_mode": "parallel"}}), encoding="utf-8")
    row = {"id": "tests", "status": "fail", "cause": "missing-python-package", "missing_modules": ["cv2"]}
    (run / "gates.json").write_text(json.dumps({"checks": [row]}), encoding="utf-8")

    summary = records.record_promoted_run(run, "code-review")

    assert summary["status"] == "recorded", summary
    ledger = records.gap_ledger.load_ledger(run / "gaps.jsonl")
    assert not ledger.errors
    assert [(record["id"], record["evidence"]) for record in ledger.records] == expected
