"""Check convergence evidence through the public workflow artifact validator."""

import json
from pathlib import Path

import pytest

from test_final_handoff import _load_finalizer, _load_shared_validator, _write_schema_v2_assess
from test_loop_review_evidence import _loop_evidence_run


def _write_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, findings: list[dict[str, object]] | None = None
) -> Path:
    """Create a complete independently clean loop using the common handoff fixture."""
    result_path = _write_schema_v2_assess(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    evidence = _loop_evidence_run(tmp_path, run_dir=tmp_path, findings=findings)
    monkeypatch.setenv("CODEX_HOME", str(evidence["codex_home"]))
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    (tmp_path / "current.diff").write_bytes((tmp_path / "round-1.diff").read_bytes())
    (tmp_path / "loop-report.md").write_text(
        "# Scope\nParser.\n# Rounds\nOne.\n# Findings\nNone.\n# Recovery\nNone.\n# Verification\nPassed.\n",
        encoding="utf-8",
    )
    counts = {"security": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0}
    result["metadata"]["adversarial_loop"] = {
        "status": "stopped",
        "reason": "clean",
        "scores": [0],
        "rounds": [{"index": 1, "score": 0, "counts": counts, "decision": "clean"}],
    }
    result["metadata"]["action_contract_version"] = 1
    open_findings = [
        finding for finding in findings or [] if finding["disposition"] in {"open", "fixed-pending-verification"}
    ]
    (tmp_path / "loop-actions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rounds": [
                    {
                        "index": 1,
                        "actions": [
                            {
                                "signature": finding["signature"],
                                "decision": (
                                    "escalate" if finding["tier"] in {"security", "critical", "high"} else "defer"
                                ),
                                "evidence": ["The finding remains open pending the next scoped action."],
                                "owner": "parent",
                                "next_action": "Request the needed scope or independent verification.",
                                "root_cause": None,
                            }
                            for finding in open_findings
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["skill"] = "adversarial-loop"
    handoff["outcome"] = {"title": "Parser review", "summary": "Parser review is clean; no code fixes were needed."}
    handoff["tables"] = [
        {
            "heading": "Iterations",
            "columns": ["Iteration", "Open findings", "Weighted score", "Decision", "Evidence"],
            "rows": [
                {
                    "id": "1",
                    "cells": [
                        "1",
                        ", ".join(f"{tier}={count}" for tier, count in counts.items()),
                        "0",
                        "clean",
                        "review-1.md",
                    ],
                    "source_ids": ["analysis:CA-1"],
                }
            ],
        }
    ]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return result_path


def test_public_validator_accepts_clean_current_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept the complete loop and reject a changed current snapshot."""
    result_path = _write_loop(tmp_path, monkeypatch)
    validator = _load_shared_validator()
    validator.validate("adversarial-loop", tmp_path, result_path)
    (tmp_path / "current.diff").write_bytes(b"unreviewed change")
    with pytest.raises(SystemExit, match="adversarial-loop-snapshot-digest-mismatch:current.diff"):
        validator.validate("adversarial-loop", tmp_path, result_path)


def test_public_validator_requires_bound_parent_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a new result whose finding actions are missing or bypassed."""
    result_path = _write_loop(tmp_path, monkeypatch)
    validator = _load_shared_validator()
    (tmp_path / "loop-actions.json").unlink()
    with pytest.raises(SystemExit, match="adversarial-loop-invalid-actions:"):
        validator.validate("adversarial-loop", tmp_path, result_path)

    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"].pop("action_contract_version")
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-action-contract-required"):
        validator.validate("adversarial-loop", tmp_path, result_path)
    final_path = tmp_path / "result.json"
    final_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-action-contract-required"):
        validator.validate("adversarial-loop", tmp_path, final_path)

    validator.validate("adversarial-loop", tmp_path, final_path, allow_legacy_loop_actions=True)
    with pytest.raises(SystemExit, match="adversarial-loop-legacy-actions-final-only"):
        validator.validate("adversarial-loop", tmp_path, result_path, allow_legacy_loop_actions=True)


def test_public_validator_accepts_verified_structural_carryover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep independently verified structural closure visible in a separately approved correction run."""
    findings = [
        {
            "signature": "contract-change",
            "tier": "high",
            "structural": True,
            "disposition": "verified-fixed",
            "evidence": ["The separately approved contract correction is independently verified in this review."],
        }
    ]
    result_path = _write_loop(tmp_path, monkeypatch, findings=findings)

    _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)
    ledger = json.loads((tmp_path / "loop-ledger.json").read_text(encoding="utf-8"))
    assert ledger["rounds"][0]["findings"] == findings


@pytest.mark.parametrize("mutation", ["forged-reviewer", "changed-source", "missing-provenance", "active-owner"])
def test_public_validator_rejects_unbound_review_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Reject clean-looking handoffs when actual source or independent provenance is invalid."""
    result_path = _write_loop(tmp_path, monkeypatch)
    if mutation == "forged-reviewer":
        ledger_path = tmp_path / "loop-ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["rounds"][0]["reviewer"]["identity"] = "invented-reviewer"
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    elif mutation == "changed-source":
        evidence = json.loads((tmp_path / "loop-evidence.json").read_text(encoding="utf-8"))
        (Path(evidence["repository"]) / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif mutation == "active-owner":
        monkeypatch.setenv("CODEX_THREAD_ID", "another-parent-thread")
    else:
        (tmp_path / "review" / "specialist-manifest.json").unlink()
    with pytest.raises(SystemExit, match="adversarial-loop-evidence-invalid:"):
        _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)


@pytest.mark.parametrize("mutation", ["drop-review", "claim-clean", "escape-report", "change-table"])
def test_public_validator_rejects_false_loop_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Keep actual ledger, local reports, and visible decisions bound to completion."""
    result_path = _write_loop(tmp_path, monkeypatch)
    ledger_path = tmp_path / "loop-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if mutation == "drop-review":
        (tmp_path / "review-1.md").unlink()
    elif mutation == "claim-clean":
        ledger["rounds"] = []
    elif mutation == "escape-report":
        ledger["rounds"][0]["report_path"] = "../review-1.md"
    else:
        handoff_path = tmp_path / "final-handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["tables"][0]["rows"][0]["cells"][3] = "converging"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        validation = _load_finalizer().render_files(
            handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["metadata"]["final_handoff"].update(
            {key: validation[key] for key in ("handoff_sha256", "rendered_sha256")}
        )
        result_path.write_text(json.dumps(result), encoding="utf-8")
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-"):
        _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)


def test_public_validator_accepts_stopped_loop_with_failed_review_and_folded_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep an authority-blocked structural finding and folded result counts visible.

    A structural flag alone leaves the score active; the parent still fails acceptance when the required contract-change
    authority is absent, without losing security and nit findings in common result counts.
    """
    findings = [
        {
            "signature": "shared-contract-boundary",
            "tier": "security",
            "structural": True,
            "disposition": "open",
            "evidence": ["review-1.md identifies a shared contract change."],
        },
        {
            "signature": "spelling",
            "tier": "nit",
            "structural": False,
            "disposition": "open",
            "evidence": ["review-1.md identifies a spelling correction."],
        },
    ]
    result_path = _write_loop(tmp_path, monkeypatch, findings=findings)

    gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    review_gate.update(status="fail", exit_code=1)
    gates.update(status="fail", checks_failed=["review"])
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")

    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(
        status="fail",
        checks_failed=["review"],
        findings={"critical": 1, "high": 0, "medium": 0, "low": 1},
        metadata={
            **result["metadata"],
            "adversarial_loop": {
                "status": "active",
                "reason": "baseline",
                "scores": [21],
                "rounds": [
                    {
                        "index": 1,
                        "score": 21,
                        "counts": {"security": 1, "critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 1},
                        "decision": "baseline",
                    }
                ],
            },
        },
    )
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["outcome"] = {
        "title": "Parser review",
        "summary": "Parser review cannot fix the shared contract without approval.",
    }
    handoff["tables"][0]["rows"][0]["cells"] = [
        "1",
        "security=1, critical=0, high=0, medium=0, low=0, nit=1",
        "21",
        "baseline",
        "review-1.md",
    ]
    handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": "review.stdout.txt"}
    handoff["remaining"] = [
        {
            "row_id": "1",
            "item": "Shared contract boundary remains open.",
            "owner": "parent",
            "next_action": "Approve a separately scoped contract change or reject the finding with evidence.",
        }
    ]
    handoff["next_steps"] = ["1"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8")

    _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)


def test_public_validator_rejects_nonclean_loop_hidden_as_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an accepted result when its real review gate recorded a failure.

    Prevents a lifecycle owner from hiding a failed convergence review by clearing the result failure list while
    retaining a passing status.
    """
    result_path = _write_loop(tmp_path, monkeypatch)
    gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    review_gate.update(status="fail", exit_code=1)
    gates.update(status="fail", checks_failed=["review"])
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")

    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(status="pass", checks_failed=[])
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": "review.stdout.txt"}
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="result-checks-failed-gate-mismatch"):
        _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)


@pytest.mark.parametrize("review_status", ["fail", "pass"])
def test_public_validator_accepts_unavailable_loop_with_not_run_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review_status: str
) -> None:
    """Preserve unavailable independent review as a failed handoff without fake clean coverage.

    Prevents an empty ledger from being presented with a clean iteration row instead of the contract-required not-run
    recovery decision.
    """
    result_path = _write_loop(tmp_path, monkeypatch)
    ledger = json.loads((tmp_path / "loop-ledger.json").read_text(encoding="utf-8"))
    ledger["rounds"] = []
    (tmp_path / "loop-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    (tmp_path / "loop-actions.json").write_text(json.dumps({"schema_version": 1, "rounds": []}), encoding="utf-8")
    evidence_path = tmp_path / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["rounds"] = []
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    gates = json.loads((tmp_path / "gates.json").read_text(encoding="utf-8"))
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    review_gate.update(status=review_status, exit_code=1 if review_status == "fail" else 0)
    failed_checks = ["review"] if review_status == "fail" else []
    gates.update(status=review_status, checks_failed=failed_checks)
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")

    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(
        status="fail",
        checks_failed=failed_checks,
        metadata={
            **result["metadata"],
            "adversarial_loop": {"status": "stopped", "reason": "independence-unavailable", "scores": [], "rounds": []},
        },
    )
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["outcome"] = {
        "title": "Parser review",
        "summary": "Parser review stopped because independent review was unavailable.",
    }
    handoff["tables"][0]["rows"][0].update(
        id="not-run",
        cells=["not-run", "Not assessed", "N/A", "independence-unavailable", "loop-report.md"],
    )
    handoff["verification"][-1] = {"check": "review", "status": review_status, "evidence": "review.stdout.txt"}
    handoff["remaining"] = [
        {
            "row_id": "not-run",
            "item": "Independent review was unavailable.",
            "owner": "parent",
            "next_action": "Obtain an allowed independent review route before claiming closure.",
        }
    ]
    handoff["next_steps"] = ["not-run"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8")

    if review_status == "pass":
        with pytest.raises(SystemExit, match="adversarial-loop-nonclean-review-gate"):
            _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)
    else:
        _load_shared_validator().validate("adversarial-loop", tmp_path, result_path)
    rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
    assert "| not-run | Not assessed | N/A | independence-unavailable | loop-report.md |" in rendered
    assert "| 1 |" not in rendered
