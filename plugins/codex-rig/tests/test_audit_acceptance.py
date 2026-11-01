"""Reject unsupported audit completion and optimization acceptance claims."""

from __future__ import annotations

import copy
import hashlib
import json
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_audit_consolidation import empty_report, make_report
from test_gate_source import _python_command

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
GATES = ("lint", "format", "types", "tests", "review")
AuditRun = tuple[Any, Path, dict[str, Any]]


def _evidence(directory: Path, name: str, payload: object) -> dict[str, str]:
    """Retain a comparison record with a digest of its exact bytes."""
    content = (json.dumps(payload, sort_keys=True) + "\n").encode()
    (directory / name).write_bytes(content)
    return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}


@pytest.fixture(scope="module", name="validator")
def _validator() -> dict[str, Any]:
    """Load the shipped validator module once; every test only reads its definitions."""
    return runpy.run_path(str(PLUGIN_ROOT / "shared" / "validate-artifacts.py"))


@pytest.fixture(scope="module", name="gate_template")
def _gate_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the gate runner once; its retained files hold no absolute location, so each test copies them."""
    template = tmp_path_factory.mktemp("audit-gates")
    arguments = [sys.executable, str(PLUGIN_ROOT / "shared" / "run_gates.py"), "--out", str(template)]
    command = _python_command("print(1)")
    for gate in GATES:
        arguments.extend((f"--{gate}", command))
    subprocess.run(arguments, capture_output=True, check=True)
    return template


@pytest.fixture
def audit_run(tmp_path: Path, validator: dict[str, Any], gate_template: Path) -> AuditRun:
    """Prepare a complete historical audit with executed review and retained cost evidence."""
    shutil.copytree(gate_template, tmp_path, dirs_exist_ok=True)
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


def _validate(run: AuditRun) -> None:
    """Exercise the public validator with one retained audit result."""
    validate, directory, result = run
    path = directory / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    validate("audit", directory, path)


def _edit_proof(directory: Path, holder: dict[str, Any], key: str, edit: Callable[[Any], object]) -> None:
    """Rewrite the retained proof referenced by `holder[key]` through `edit` and rebind its digest."""
    name = holder[key]["path"]
    proof = json.loads((directory / name).read_text(encoding="utf-8"))
    edit(proof)
    holder[key] = _evidence(directory, name, proof)


def _point_candidate_at_baseline(directory: Path, value: dict[str, Any]) -> None:
    """Make every retained candidate snapshot equal the baseline, rebinding each proof digest."""
    baseline = value["baseline_sha256"]
    value["candidate_sha256"] = baseline
    value["static_measurements"][0]["candidate_sha256"] = baseline
    for record in value["static_gates"]:
        _edit_proof(directory, record, "evidence", lambda proof: proof.update(candidate_sha256=baseline))
    for field in ("behavioral_comparison", "live_comparison"):
        _edit_proof(directory, value, field, lambda proof: proof.update(candidate_sha256=baseline))


def _set_consolidation(run: AuditRun, report: dict[str, Any], outcomes: list[dict[str, Any]]) -> None:
    """Turn the run into an advisory consolidation audit carrying `report` and the claimed `outcomes`."""
    _, directory, result = run
    result["metadata"].update(
        axis="consolidate",
        value_per_token={"status": "not-run"},
        consolidation={"report": _evidence(directory, "consolidation.json", report), "outcomes": outcomes},
    )


@pytest.fixture(scope="module", name="settle")
def _settle() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Load the consolidation settler used by the audit consumer once for the module."""
    return runpy.run_path(str(PLUGIN_ROOT / "shared" / "audit_consolidation.py"))["settle_consolidation"]


@pytest.fixture(name="reviewed_run")
def _reviewed_run(
    audit_run: AuditRun, settle: Callable[[dict[str, Any]], dict[str, Any]]
) -> tuple[AuditRun, dict[str, Any]]:
    """Prepare a consolidation audit whose nonempty proposal carries a holding review and a derived outcome."""
    report = make_report(audit_run[1])
    report["candidates"][0]["review"] = {
        "digest": settle(report)["review_inputs"][0]["digest"],
        "verdict": "holds",
        "evidence": "Fixture review preserves source obligations, loading and citations.",
    }
    _set_consolidation(audit_run, report, settle(report)["outcomes"])
    assert audit_run[2]["metadata"]["consolidation"]["outcomes"][0]["status"] == "reviewed"
    return audit_run, report


def test_accepts_retained_matched_audit_evidence(audit_run: AuditRun) -> None:
    """A fully matched finite comparison with every hard guard passed is accepted."""
    _validate(audit_run)


def test_consolidation_requires_bound_advisory_evidence(audit_run: AuditRun) -> None:
    """A consolidation audit is not promoted through the ordinary unmeasured-cost early return.

    The axis says consolidate but no report or outcomes are bound, so the validator must name the consolidation gap.
    """
    result = audit_run[2]
    result["metadata"]["axis"] = "consolidate"
    result["metadata"]["value_per_token"] = {"status": "not-run"}

    with pytest.raises(SystemExit, match="consolidation"):
        _validate(audit_run)


def test_accepts_empty_consolidation_discovery(audit_run: AuditRun) -> None:
    """The real audit consumer accepts a consolidation audit whose discovery found nothing to merge."""
    _set_consolidation(audit_run, empty_report(), [])

    _validate(audit_run)


def test_rejects_forged_reviewed_outcome(audit_run: AuditRun) -> None:
    """A claimed `reviewed` outcome that the report does not derive is a settlement mismatch.

    Outcomes must be derived from the bound report; an audit author cannot assert a reviewed result for empty discovery.
    """
    _set_consolidation(audit_run, empty_report(), [{"status": "reviewed"}])

    with pytest.raises(SystemExit, match="settlement-mismatch"):
        _validate(audit_run)


def test_rejects_accepted_cost_status_on_advisory_audit(audit_run: AuditRun) -> None:
    """A consolidation audit stays advisory-only and cannot also claim an accepted cost comparison."""
    _set_consolidation(audit_run, empty_report(), [])
    audit_run[2]["metadata"]["value_per_token"]["status"] = "accepted"

    with pytest.raises(SystemExit, match="advisory-only"):
        _validate(audit_run)


def test_accepts_reviewed_proposal(reviewed_run: tuple[AuditRun, dict[str, Any]]) -> None:
    """A nonempty reviewed proposal with a derived outcome reaches the public audit artifact validator."""
    _validate(reviewed_run[0])


def _damage_proposal(report: dict[str, Any], result: dict[str, Any]) -> None:
    """Append text to the reviewed replacement so the review digest no longer matches."""
    report["candidates"][0]["edits"][0]["replacement"] += " Additional explanation."


def _damage_citation(report: dict[str, Any], result: dict[str, Any]) -> None:
    """Rewrite the citing source and refresh its recorded hash."""
    source = report["sources"][-1]
    Path(source["path"]).write_text("Read first.md §Approval and preserve its actor.\n", encoding="utf-8")
    source["sha256"] = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()


def _damage_outcome(report: dict[str, Any], result: dict[str, Any]) -> None:
    """Claim a different outcome status than the report derives."""
    result["metadata"]["consolidation"]["outcomes"][0]["status"] = "accepted"


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_damage_proposal, id="proposal"),
        pytest.param(_damage_citation, id="citation"),
        pytest.param(_damage_outcome, id="outcome"),
    ],
)
def test_reviewed_proposal_drift_is_a_settlement_mismatch(
    reviewed_run: tuple[AuditRun, dict[str, Any]], damage: Callable[[dict[str, Any], dict[str, Any]], None]
) -> None:
    """Editing the proposal, its citing source or the claimed outcome after review is rejected.

    The consumer re-derives outcomes from the retained report, so any drift between review and claim must surface.
    """
    run, report = reviewed_run
    damage(report, run[2])
    run[2]["metadata"]["consolidation"]["report"] = _evidence(run[1], "consolidation.json", report)

    with pytest.raises(SystemExit, match="settlement-mismatch"):
        _validate(run)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda proof: proof.pop("tasks"), id="missing-tasks"),
        pytest.param(lambda proof: proof.update(tasks=[]), id="empty-tasks"),
        pytest.param(lambda proof: proof["tasks"][0].pop("baseline"), id="missing-baseline"),
        pytest.param(lambda proof: proof["tasks"][0].pop("candidate"), id="missing-candidate"),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].pop("completion_quality"), id="missing-quality"),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].pop("tool_failures"), id="missing-tools"),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].pop("check_failures"), id="missing-checks"),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].pop("evidence"), id="missing-transcript"),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].update(tool_failures=1), id="tool-regression"),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].update(check_failures=1), id="check-regression"),
        pytest.param(
            lambda proof: proof["tasks"][0]["candidate"].update(completion_quality=0.9), id="quality-regression"
        ),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].update(completion_quality=True), id="bool-quality"),
        pytest.param(
            lambda proof: proof["tasks"][0]["candidate"].update(completion_quality=float("nan")), id="nan-quality"
        ),
        pytest.param(lambda proof: proof["tasks"][0]["candidate"].update(completion_quality=2), id="unbounded-quality"),
        pytest.param(lambda proof: proof["tasks"].append(copy.deepcopy(proof["tasks"][0])), id="duplicate-task"),
    ],
)
def test_rejects_omitted_or_regressed_paired_task_guards(audit_run: AuditRun, damage: Callable[[Any], object]) -> None:
    """Aggregate totals cannot conceal absent or degraded per-task evidence.

    The matched task is damaged in the retained behavior proof (missing parts, regressions, non-numeric or out-of-range
    quality, duplicates) and the digest is rebound, so only the per-task guards can catch it.
    """
    _, directory, result = audit_run
    _edit_proof(directory, result["metadata"]["value_per_token"], "behavioral_comparison", damage)

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
def test_rejects_missing_acceptance_evidence(audit_run: AuditRun, field: str) -> None:
    """An accepted claim is rejected when any single required evidence component is absent."""
    del audit_run[2]["metadata"]["value_per_token"][field]

    with pytest.raises(SystemExit, match="audit-cost-"):
        _validate(audit_run)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda directory, value: value.update(schema_version=1), id="schema"),
        pytest.param(lambda directory, value: value["static_gates"][0].update(status="fail"), id="failed-guard"),
        pytest.param(
            lambda directory, value: (directory / "live.json").write_text("{}", encoding="utf-8"), id="tampered-proof"
        ),
        pytest.param(
            lambda directory, value: _evidence(directory, "obligations.json", [{"preserved": False}]),
            id="obligation-loss",
        ),
        pytest.param(lambda directory, value: value["static_measurements"][0].update(source="utf8-bytes"), id="proxy"),
        pytest.param(
            lambda directory, value: _edit_proof(
                directory,
                value,
                "live_comparison",
                lambda proof: proof["candidate_identity"].update(prompt_sha256="e" * 64),
            ),
            id="mismatched-prompt",
        ),
        pytest.param(
            lambda directory, value: _edit_proof(
                directory, value, "behavioral_comparison", lambda proof: proof.update(candidate_failures=1)
            ),
            id="regression",
        ),
        pytest.param(
            lambda directory, value: _edit_proof(
                directory, value, "live_comparison", lambda proof: proof.update(candidate_cost=float("nan"))
            ),
            id="nan-cost",
        ),
        pytest.param(
            lambda directory, value: _edit_proof(
                directory, value, "live_comparison", lambda proof: proof.update(candidate_cost=-1)
            ),
            id="negative-cost",
        ),
        pytest.param(
            lambda directory, value: _edit_proof(
                directory, value, "live_comparison", lambda proof: proof.update(candidate_cost=True)
            ),
            id="bool-cost",
        ),
        pytest.param(
            lambda directory, value: _edit_proof(
                directory, value, "live_comparison", lambda proof: proof.update(candidate_cost=9.9)
            ),
            id="insufficient-saving",
        ),
    ],
)
def test_rejects_invalid_acceptance_evidence(
    audit_run: AuditRun, damage: Callable[[Path, dict[str, Any]], object]
) -> None:
    """Dishonest guards, unmatched comparisons, invalid costs and inadequate savings are rejected.

    Each case damages exactly one retained proof or claim field of an otherwise complete accepted audit.
    """
    _, directory, result = audit_run
    damage(directory, result["metadata"]["value_per_token"])

    with pytest.raises(SystemExit, match="audit-cost-"):
        _validate(audit_run)


def test_successful_audit_requires_executed_review(audit_run: AuditRun) -> None:
    """Generic skipped-gate accounting cannot certify an audit whose review gate never ran.

    Every gate is marked not-applicable with exit code 0, which a generic validator would treat as clean.
    """
    _, directory, result = audit_run
    result["metadata"]["value_per_token"]["status"] = "not-run"
    gates = json.loads((directory / "gates.json").read_text(encoding="utf-8"))
    gates["checks"] = [
        {**gate, "status": "not-applicable", "reason": "Unavailable diagnostic.", "exit_code": 0}
        for gate in gates["checks"]
    ]
    (directory / "gates.json").write_text(json.dumps(gates), encoding="utf-8")

    with pytest.raises(SystemExit, match="audit-review-gate-required"):
        _validate(audit_run)


@pytest.mark.parametrize("status", ["not-run", "insufficient-evidence", "rejected"])
def test_honest_unaccepted_comparison_remains_valid(audit_run: AuditRun, status: str) -> None:
    """Assessed audits may disclose unavailable, insufficient or rejected optimization evidence."""
    audit_run[2]["metadata"]["value_per_token"] = {"schema_version": 2, "status": status}

    _validate(audit_run)


def test_unchanged_snapshot_cannot_certify_optimization(audit_run: AuditRun) -> None:
    """A positive cost claim is rejected when every baseline and candidate snapshot is identical.

    All retained proofs are rewritten consistently so only the unchanged-snapshot guard can reject the claim.
    """
    _, directory, result = audit_run
    _point_candidate_at_baseline(directory, result["metadata"]["value_per_token"])

    with pytest.raises(SystemExit, match="audit-cost-unchanged-snapshot"):
        _validate(audit_run)
