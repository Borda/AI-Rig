"""Bind isolated reviewer evidence to the canonical review acceptance path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _module(path: Path) -> ModuleType:
    """Load existing installed helpers and test-only artifact construction."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_server_manifest_requires_execution_evidence(tmp_path: Path) -> None:
    """Do not admit the new independent route on a manifest declaration alone."""
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    review_input = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "diff.patch").write_bytes(review_input)
    manifest = {
        "schema_version": 4,
        "review_run_id": "isolated-review",
        "parent_thread_id": "parent-correlation",
        "review_input_sha256": hashlib.sha256(review_input).hexdigest(),
        "passes": [],
    }

    with pytest.raises(SystemExit, match="review-app-server-execution-missing"):
        validator._validate_manifest_entries(tmp_path, manifest, [], set(), tmp_path, "parent-correlation", tmp_path)


@pytest.fixture
def isolated_review(tmp_path: Path, text_newline_default: None) -> Path:
    """Build a full synthetic HIGH_RISK review through existing artifact constructors."""
    completion_tests = _module(Path(__file__).with_name("test_review_completion_gate.py"))
    run = completion_tests._assessed_pr.__wrapped__(tmp_path)
    evidence_tests = _module(Path(__file__).with_name("test_app_server_review.py"))
    plan_path, evidence_path = evidence_tests.review_evidence_files(run, roles=("qa-specialist", "challenger"))
    result_path = run / "result.json"
    result = json.loads(result_path.read_text())
    metadata = result["metadata"]
    plan = json.loads(plan_path.read_text())
    evidence = json.loads(evidence_path.read_text())
    for node in evidence["nodes"]:
        role = node["role_id"]
        retained_card = run / "role-cards" / role / "ROLE.md"
        retained_card.parent.mkdir(parents=True, exist_ok=True)
        retained_card.write_bytes((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes())
        output = evidence_path.parent / node["output_path"]
        output.write_text(
            json.dumps(
                {
                    "source_sha256": plan["source_sha256"],
                    "diff_sha256": plan["review_input_sha256"],
                    "assessment": {"rating": 1, "rationale": "The inspected scope is clean."},
                    "findings": [],
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        node["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    for payload in (plan, evidence):
        payload.update(
            review_run_id=metadata["review_run_id"],
            parent_thread_id="thread",
            review_input_sha256=metadata["review_input_sha256"],
        )
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    evidence["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    passes = [
        {
            "role": node["role_id"],
            "role_card_sha256": node["role_card_sha256"],
            "mode": "app-server",
            "axis": "independent verification",
            "trigger": "HIGH_RISK",
            "confidence": 0.95,
            "blocking_findings": 0,
            "output_path": str(evidence_path.parent / node["output_path"]),
        }
        for node in evidence["nodes"]
    ]
    manifest = {
        "schema_version": 4,
        "review_run_id": metadata["review_run_id"],
        "parent_thread_id": "thread",
        "review_input_sha256": metadata["review_input_sha256"],
        "passes": passes,
        "app_server_execution": {
            "plan_path": str(plan_path),
            "evidence_path": str(evidence_path),
            "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        },
    }
    (run / "specialist-manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_text())
    routing["risk_tier"] = "HIGH_RISK"
    routing["signals"].update(high_candidate=True, behavior_change=True)
    routing["triggered_roles"] = sorted(item["role"] for item in passes)
    routing["trigger_reasons"] = {item["role"]: ["HIGH_RISK fixture"] for item in passes}
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    metadata.update(
        risk_tier="HIGH_RISK",
        specialist_passes=passes,
        independence_required=True,
        independence_satisfied=True,
        execution_mode="independent-spawned",
        execution_evidence_level="app-server-parent-observed",
        write_parallel_eligible=False,
    )
    metadata["reviewer_assessments"].extend(
        {
            "role": "QA specialist" if item["role"] == "qa-specialist" else "Challenger",
            "rating": 1,
            "evidence": item["output_path"],
        }
        for item in passes
    )
    handoff_path = run / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["reviewers"] = metadata["reviewer_assessments"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    binding = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, run / "final.md", run / "final-handoff.validation.json"
    )
    metadata["final_handoff"].update({key: binding[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    return run


def test_high_risk_app_server_review_completes_and_is_discoverable(isolated_review: Path) -> None:
    """Run both canonical validators and report lookup with genuinely distinct fixture threads."""
    finder = PLUGIN_ROOT / "shared/find-review-report.py"
    completed = subprocess.run(
        [sys.executable, str(finder), "--complete-run", str(isolated_review), "--parent-thread-id", "thread"],
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == (isolated_review / "final.md").read_bytes()
    lookup = subprocess.run(
        [sys.executable, str(finder), "--target", "#123", "--reports-dir", str(isolated_review.parent.parent)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert lookup.returncode == 0, lookup.stderr
    assert Path(lookup.stdout.strip()) == isolated_review / "result.json"


@pytest.mark.parametrize(
    ("roles", "output_tamper", "expected_error"),
    [
        pytest.param(("challenger",), False, None, id="one-challenger"),
        pytest.param(("qa-specialist",), False, "manifest-triggered-role-set-mismatch", id="wrong-role"),
        pytest.param(("qa-specialist", "challenger"), False, "manifest-triggered-role-set-mismatch", id="extra-role"),
        pytest.param(("challenger",), True, "evidence-output-sha256-mismatch", id="forged-output"),
    ],
)
def test_challenge_manifest_requires_exact_authenticated_challenger(
    isolated_review: Path, roles: tuple[str, ...], output_tamper: bool, expected_error: str | None
) -> None:
    """Admit one verified challenger independently of generic Code Review routing."""
    manifest_path = isolated_review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    plan_path = Path(manifest["app_server_execution"]["plan_path"])
    evidence_path = Path(manifest["app_server_execution"]["evidence_path"])
    plan = json.loads(plan_path.read_text())
    evidence = json.loads(evidence_path.read_text())
    plan["nodes"] = [node for node in plan["nodes"] if node["role_id"] in roles]
    evidence["nodes"] = [node for node in evidence["nodes"] if node["role_id"] in roles]
    manifest["passes"] = [item for item in manifest["passes"] if item["role"] in roles]
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    evidence["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    manifest["app_server_execution"]["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    if output_tamper:
        Path(manifest["passes"][0]["output_path"]).write_text(
            "Forged reviewer output\n", encoding="utf-8", newline="\n"
        )

    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py"),
            "--out",
            str(isolated_review),
            "--manifest-only",
            "--challenge-only",
            "--parent-thread-id",
            "thread",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if expected_error is None:
        assert completed.returncode == 0, completed.stderr
    else:
        assert completed.returncode != 0
        assert expected_error in completed.stderr


def test_code_review_validator_rejects_legacy_app_server_plan_dispatch(isolated_review: Path) -> None:
    """Keep historical adapter evidence readable without allowing it to satisfy a new review run."""
    manifest_path = isolated_review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    plan_path = Path(manifest["app_server_execution"]["plan_path"])
    evidence_path = Path(manifest["app_server_execution"]["evidence_path"])
    plan = json.loads(plan_path.read_text())
    plan["schema_version"] = 1
    for key in ("source_path", "source_sha256", "diff_path", "diff_sha256"):
        del plan[key]
    for node in plan["nodes"]:
        del node["capacity_receipt"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    evidence = json.loads(evidence_path.read_text())
    evidence["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    manifest["app_server_execution"]["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")

    with pytest.raises(SystemExit, match="review-app-server-evidence-invalid:plan-legacy-dispatch-forbidden"):
        validator._validate_app_server_review(isolated_review, manifest, manifest["passes"])


@pytest.mark.parametrize("tamper", ["response", "input", "execution-digest", "native-attempt"])
def test_review_completion_rejects_changed_app_server_evidence(isolated_review: Path, tamper: str) -> None:
    """Reject changed evidence at final completion, not only at initial adapter validation."""
    manifest_path = isolated_review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if tamper == "response":
        Path(manifest["passes"][0]["output_path"]).write_bytes(b"Replaced final response.\n")
    elif tamper == "input":
        (isolated_review / "diff.patch").write_bytes(b"Replaced input.\n")
    elif tamper == "execution-digest":
        manifest["app_server_execution"]["evidence_sha256"] = "0" * 64
    else:
        manifest["passes"][0]["attempts"] = [{"status": "completed"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/find-review-report.py"),
            "--complete-run",
            str(isolated_review),
            "--parent-thread-id",
            "thread",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "Review handoff blocked:" in completed.stderr
    assert not completed.stdout


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-source-digest",
        "missing-diff-digest",
        "invalid-rating",
        "invalid-finding",
    ],
)
def test_review_completion_rejects_rehashed_invalid_reviewer_output(isolated_review: Path, tamper: str) -> None:
    """A consistent receipt cannot authorize reviewer JSON that violates the turn contract."""
    manifest_path = isolated_review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    evidence_path = Path(manifest["app_server_execution"]["evidence_path"])
    evidence = json.loads(evidence_path.read_text())
    node = evidence["nodes"][0]
    output_path = evidence_path.parent / node["output_path"]
    output = json.loads(output_path.read_text())
    if tamper == "missing-source-digest":
        del output["source_sha256"]
    elif tamper == "missing-diff-digest":
        del output["diff_sha256"]
    elif tamper == "invalid-rating":
        output["assessment"]["rating"] = True
    else:
        output["findings"] = [{"signature": "unsupported"}]
    output_path.write_text(json.dumps(output), encoding="utf-8", newline="\n")
    node["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    manifest["app_server_execution"]["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/find-review-report.py"),
            "--complete-run",
            str(isolated_review),
            "--parent-thread-id",
            "thread",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "review-app-server-evidence-invalid:app-server-review-output-" in completed.stderr
    assert not completed.stdout


def test_high_risk_review_cannot_replace_one_required_thread_with_parent_text(isolated_review: Path) -> None:
    """A valid challenger wave cannot lend its independence to substituted QA."""
    manifest_path = isolated_review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    plan_path = Path(manifest["app_server_execution"]["plan_path"])
    evidence_path = Path(manifest["app_server_execution"]["evidence_path"])
    plan = json.loads(plan_path.read_text())
    evidence = json.loads(evidence_path.read_text())
    plan["nodes"] = [node for node in plan["nodes"] if node["role_id"] == "challenger"]
    evidence["nodes"] = [node for node in evidence["nodes"] if node["role_id"] == "challenger"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    evidence["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    manifest["app_server_execution"]["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    for item in manifest["passes"]:
        if item["role"] == "qa-specialist":
            item["mode"] = "substituted"
            substitute_path = isolated_review / "qa-specialist-parent.md"
            substitute_path.write_text(
                "role_id: qa-specialist\n## Reviewer Assessment\n\nRating: 3\n"
                "Rationale: Parent inspection cannot establish independent QA.\n",
                encoding="utf-8",
                newline="\n",
            )
            item["output_path"] = str(substitute_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    result_path = isolated_review / "result.json"
    result = json.loads(result_path.read_text())
    result["metadata"].update(
        specialist_passes=manifest["passes"], fanout_substituted=True, independence_satisfied=False
    )
    for assessment in result["metadata"]["reviewer_assessments"]:
        if assessment["role"] == "QA specialist":
            assessment.update(role="QA specialist (parent substitute)", rating=3, evidence="qa-specialist-parent.md")
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")

    with pytest.raises(SystemExit, match="independent-review-required-for-pass:qa-specialist"):
        validator._validate_result(isolated_review, result_path, isolated_review, "thread", isolated_review)
