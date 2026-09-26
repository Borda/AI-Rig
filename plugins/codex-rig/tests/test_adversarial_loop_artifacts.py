"""Check convergence evidence through the public workflow artifact validator."""

import json
import hashlib
import itertools
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_final_handoff import _load_finalizer, _load_shared_validator, _write_schema_v2_assess
from test_loop_review_evidence import _loop_evidence_run, _rewrite_native_outputs, _validator as _load_loop_validator


def _write_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    findings: list[dict[str, object]] | None = None,
    evidence_out: dict[str, object] | None = None,
) -> Path:
    """Create a complete independently clean loop using the common handoff fixture."""
    result_path = _write_schema_v2_assess(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    evidence = _loop_evidence_run(tmp_path, run_dir=tmp_path, findings=findings, current_contract=True)
    if evidence_out is not None:
        evidence_out.update(evidence)
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
    result["metadata"]["action_contract_version"] = 2
    result["metadata"]["challenge_table_contract_version"] = 1
    result["metadata"]["challenge_scope"] = {"mode": "single"}
    result["metadata"]["challenge_origin"] = {
        "kind": "first-run",
        "caller_run": None,
        "prior_ledger_sha256": None,
    }
    open_findings = [
        finding for finding in findings or [] if finding["disposition"] in {"open", "fixed-pending-verification"}
    ]
    (tmp_path / "loop-actions.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
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
    handoff["skill"] = "challenge-resolve"
    handoff["outcome"] = {"title": "Parser review", "summary": "Parser review is clean; no code fixes were needed."}
    handoff["tables"] = [
        {
            "heading": "Iterations",
            "columns": [
                "Challenge",
                "Security",
                "Critical",
                "High",
                "Medium",
                "Low",
                "Nits",
                "Weighted score",
                "Decision",
                "Evidence",
            ],
            "rows": [
                {
                    "id": "1",
                    "cells": [
                        "1",
                        *(f"0 + {counts[tier]}" for tier in ("security", "critical", "high", "medium", "low", "nit")),
                        "0 + 0",
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


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_public_validator_accepts_clean_current_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """Accept complete current evidence and reject a changed snapshot for either filename."""
    candidate = _write_loop(tmp_path, monkeypatch)
    result_path = tmp_path / filename
    if result_path != candidate:
        result_path.write_bytes(candidate.read_bytes())
    validator = _load_shared_validator()
    validator.validate("challenge-resolve", tmp_path, result_path)
    (tmp_path / "current.diff").write_bytes(b"unreviewed change")
    with pytest.raises(SystemExit, match="adversarial-loop-snapshot-digest-mismatch:current.diff"):
        validator.validate("challenge-resolve", tmp_path, result_path)


def test_public_validator_accepts_remediation_report_heading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept the new review-fix wording while retaining the historical fixture."""
    result_path = _write_loop(tmp_path, monkeypatch)
    report_path = tmp_path / "loop-report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace("# Recovery", "# Remediation"),
        encoding="utf-8",
    )

    _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_public_validator_requires_review_action_report_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject reports that omit both the current and historical action sections."""
    result_path = _write_loop(tmp_path, monkeypatch)
    report_path = tmp_path / "loop-report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace("# Recovery\nNone.\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="missing-artifact-section:loop-report.md:Remediation"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_public_validator_rejects_remediation_word_in_body_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body mention cannot replace the required review-action heading."""
    result_path = _write_loop(tmp_path, monkeypatch)
    report_path = tmp_path / "loop-report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace(
            "# Recovery\nNone.\n", "Remediation is mentioned here but has no heading.\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="missing-artifact-section:loop-report.md:Remediation"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_current_result_requires_explicit_challenge_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """Neither result filename can bypass chunk coverage by omitting its scope."""
    candidate = _write_loop(tmp_path, monkeypatch)
    result_path = tmp_path / filename
    if result_path != candidate:
        result_path.write_bytes(candidate.read_bytes())
    result = json.loads(result_path.read_text(encoding="utf-8"))
    del result["metadata"]["challenge_scope"]
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-challenge-scope-required"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_current_result_requires_ranked_table_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """Both current result filenames require the ranked table contract."""
    candidate = _write_loop(tmp_path, monkeypatch)
    result_path = tmp_path / filename
    result = json.loads(candidate.read_text(encoding="utf-8"))
    del result["metadata"]["challenge_table_contract_version"]
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-table-contract-required"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_single_scope_cannot_claim_child_scores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a digest-bound child score list when no chunk checker owns it."""
    candidate = _write_loop(tmp_path, monkeypatch)
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 3
    handoff["tables"][0]["rows"][0]["cells"] = ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]
    handoff["child_runs"] = [{"result_path": "chunks/runs/001/result.json", "scores": [6, 0], "decision": "clean"}]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(candidate.read_text(encoding="utf-8"))
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    candidate.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-child-scores-scope-invalid"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, candidate)


def test_chunked_candidate_rechecks_coverage_before_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a clean parent candidate when its declared chunk coverage is invalid."""
    result_path = _write_loop(tmp_path, monkeypatch)
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "chunks.json").write_text("{}", encoding="utf-8")
    (chunks / "results.json").write_text('{"schema_version":1,"chunks":[]}', encoding="utf-8")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["challenge_scope"] = {
        "mode": "chunked",
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-chunk-coverage-invalid"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_chunked_candidate_rejects_unanchored_coverage_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate cannot redirect chunk evidence outside its declared run."""
    result_path = _write_loop(tmp_path, monkeypatch)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["challenge_scope"] = {
        "mode": "chunked",
        "manifest_path": "../chunks/chunks.json",
        "results_path": "chunks/results.json",
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-challenge-scope-invalid"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_promoted_chunked_result_rejects_old_table_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A promoted result cannot use the historical table contract as current evidence."""
    candidate = _write_loop(tmp_path, monkeypatch)
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "chunks.json").write_text(
        json.dumps({"schema_version": 1, "origin": {"kind": "first-run", "caller_run": None}}), encoding="utf-8"
    )
    (chunks / "results.json").write_text("current", encoding="utf-8")
    result = json.loads(candidate.read_text(encoding="utf-8"))
    result["metadata"]["challenge_scope"] = {
        "mode": "chunked",
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }
    result["metadata"]["challenge_table_contract_version"] = 2
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["rows"][0]["cells"][1:8] = ["0"] * 7
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    promoted = tmp_path / "result.json"
    promoted.write_text(json.dumps(result), encoding="utf-8")
    validator = _load_shared_validator()
    with pytest.raises(SystemExit, match="adversarial-loop-table-contract-version-required"):
        validator.validate("challenge-resolve", tmp_path, promoted)


def test_candidate_cannot_call_chunk_artifacts_single_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Visible chunk artifacts prevent relabeling a chunked candidate as single scope."""
    result_path = _write_loop(tmp_path, monkeypatch)
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "chunks.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-challenge-scope-mismatch"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_current_result_requires_request_bound_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """Keep old JSON readable while requiring request delivery for current validation."""
    candidate = _write_loop(tmp_path, monkeypatch)
    evidence_path = tmp_path / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["schema_version"] = 1
    for key in ("request", "supporting_paths", "current_supporting_source_path", "origin"):
        evidence.pop(key)
    for round_record in evidence["rounds"]:
        round_record.pop("supporting_source_path")
        round_record.pop("triage")
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    validator = _load_shared_validator()
    result_path = tmp_path / filename
    if result_path != candidate:
        result_path.write_bytes(candidate.read_bytes())
    assert json.loads(result_path.read_text(encoding="utf-8"))["metadata"]["adversarial_loop"]["scores"] == [0]
    with pytest.raises(SystemExit, match="adversarial-loop-evidence-contract-required"):
        validator.validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_current_result_rejects_untriaged_schema_two_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """An earlier untriaged schema-two record cannot validate under either result filename."""
    candidate = _write_loop(tmp_path, monkeypatch)
    evidence_path = tmp_path / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    for round_record in evidence["rounds"]:
        del round_record["triage"]
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    result_path = tmp_path / filename
    if result_path != candidate:
        result_path.write_bytes(candidate.read_bytes())

    with pytest.raises(SystemExit, match="adversarial-loop-evidence-invalid:loop-evidence-round-shape-invalid"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize("column_index", [1, 2, 3, 4, 5, 6])
def test_public_validator_rejects_wrong_ranked_severity_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column_index: int
) -> None:
    """Reject a displayed severity count that disagrees with the reviewed ledger."""
    result_path = _write_loop(tmp_path, monkeypatch)
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["rows"][0]["cells"][column_index] = "1"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-table-mismatch"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_results_rows_show_carried_and_new_findings_in_each_round(tmp_path: Path) -> None:
    """Keep newly found defects visible even when a later round scores worse."""
    first = [{"signature": name, "tier": "high", "disposition": "open"} for name in ("first", "second", "third")]
    ledger = {
        "rounds": [
            {"report_path": "review-1.json", "findings": first},
            {
                "report_path": "review-2.json",
                "findings": [*first, {"signature": "fourth", "tier": "high", "disposition": "open"}],
            },
        ]
    }
    empty = {tier: 0 for tier in ("security", "critical", "high", "medium", "low", "nit")}
    summary = {
        "reason": "nonconverging",
        "rounds": [
            {"index": 1, "counts": {**empty, "high": 3}, "score": 18, "decision": "baseline"},
            {"index": 2, "counts": {**empty, "high": 4}, "score": 24, "decision": "nonconverging"},
        ],
    }
    zero = ["0 + 0"] * 6
    (tmp_path / "final-handoff.json").write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "rows": [
                            {"cells": ["1", *zero[:2], "0 + 3", *zero[3:], "0 + 18", "baseline", "review-1.json"]},
                            {"cells": ["2", *zero[:2], "3 + 1", *zero[3:], "18 + 6", "nonconverging", "review-2.json"]},
                        ]
                    }
                ],
                "remaining": ["four open findings"],
                "next_steps": ["resolve them"],
            }
        ),
        encoding="utf-8",
    )
    validator = _load_shared_validator()
    validator._validate_adversarial_loop_rows(tmp_path, ledger, summary, table_version=1)

    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["rows"][1]["cells"][3] = "4"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-table-mismatch"):
        validator._validate_adversarial_loop_rows(tmp_path, ledger, summary, table_version=1)


def test_chunked_coordinator_reports_children_without_a_combined_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate a stopped child-backed coordinator without inventing a parent review."""
    candidate = _write_loop(tmp_path, monkeypatch)
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "chunks.json").write_text(
        json.dumps({"schema_version": 1, "origin": {"kind": "first-run", "caller_run": None}}), encoding="utf-8"
    )
    (chunks / "results.json").write_text("results", encoding="utf-8")
    summary = {
        "schema_version": 1,
        "manifest_sha256": "a" * 64,
        "results_sha256": "b" * 64,
        "runs": [{"result_path": "chunks/runs/001/result.json", "scores": [6, 0], "decision": "clean"}],
    }
    gates_path = tmp_path / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    next(check for check in gates["checks"] if check["id"] == "review").update(status="fail", exit_code=1)
    gates.update(status="fail", checks_failed=["review"])
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 3
    handoff["outcome"] = {"title": "Chunk review", "summary": "Child reviews passed; overall review is incomplete."}
    handoff["tables"][0]["rows"][0]["cells"] = ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]
    handoff["child_runs"] = json.loads(json.dumps(summary["runs"]))
    handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": gates["checks"][-1]["stdout"]}
    handoff["remaining"] = [
        {
            "row_id": "1",
            "item": "Full scope has no independent review.",
            "owner": "parent",
            "next_action": "Seek a full scope review when capacity permits.",
        }
    ]
    handoff["next_steps"] = ["1"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(candidate.read_text(encoding="utf-8"))
    result.update(status="fail", checks_failed=["review"])
    result["metadata"].pop("adversarial_loop")
    result["metadata"]["challenge_scope"] = {
        "mode": "chunked",
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }
    result["metadata"]["chunked_review"] = json.loads(json.dumps(summary))
    result["metadata"]["challenge_origin"] = {"kind": "first-run", "caller_run": None}
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    candidate.write_text(json.dumps(result), encoding="utf-8")
    (tmp_path / "loop-ledger.json").unlink()
    validator = _load_shared_validator()
    real_run = subprocess.run

    def child_summary(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Supply the validated child summary from the checker boundary."""
        if Path(command[1]).name == "chunk_diff.py":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(summary), stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(validator.subprocess, "run", child_summary)
    validator.validate("challenge-resolve", tmp_path, candidate)

    handoff["child_runs"][0]["scores"] = [0]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    candidate.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-child-scores-mismatch"):
        validator.validate("challenge-resolve", tmp_path, candidate)

    handoff["child_runs"] = json.loads(json.dumps(summary["runs"]))
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})

    result["metadata"]["chunked_review"]["runs"][0]["scores"] = [0]
    candidate.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-chunk-summary-mismatch"):
        validator.validate("challenge-resolve", tmp_path, candidate)

    result["metadata"]["chunked_review"] = json.loads(json.dumps(summary))
    handoff["presentation_version"] = 2
    del handoff["child_runs"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    candidate.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-child-scores-presentation-required"):
        validator.validate("challenge-resolve", tmp_path, candidate)
    (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-child-scores-presentation-required"):
        validator.validate("challenge-resolve", tmp_path, tmp_path / "result.json")


def test_complete_clean_chunk_coverage_permits_success_without_combined_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete clean child summary must have a truthful coordinator success form."""
    validator = _load_shared_validator()
    summary = {
        "schema_version": 1,
        "manifest_sha256": "a" * 64,
        "results_sha256": "b" * 64,
        "runs": [{"result_path": "chunks/runs/001/result.json", "scores": [0], "decision": "clean"}],
    }
    handoff = {
        "presentation_version": 3,
        "child_runs": summary["runs"],
        "tables": [{"rows": [{"cells": ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]}]}],
        "remaining": [],
        "next_steps": [],
    }
    monkeypatch.setattr(validator, "_validate_challenge_scope", lambda *_, **__: summary)
    monkeypatch.setattr(
        validator, "_load_json", lambda path: json.loads(path.read_text()) if path.name == "chunks.json" else handoff
    )
    result = {
        "status": "pass",
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "metadata": {"chunked_review": summary},
    }
    gates = {"checks": [{"id": "review", "status": "pass"}]}

    validator._validate_chunked_coordinator(result, tmp_path, gates, candidate=True)
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    (chunks / "chunks.json").write_text('{"schema_version":3,"prior_findings":{"ledger_path":null}}')
    with pytest.raises(SystemExit, match="adversarial-loop-chunk-origin-contract-required"):
        validator._validate_chunked_coordinator(result, tmp_path, gates, candidate=True, current_result=True)


def _bind_real_chunk_run(
    run: Path,
    evidence: dict[str, object],
    repository: Path,
    paths: list[str],
    source: bytes,
    diff: bytes,
    request: dict[str, str],
    identity: str,
    findings: list[dict[str, object]] | None = None,
) -> None:
    """Bind a complete native review fixture to one planned child or interaction."""
    fixture = evidence["fixture"]
    assert isinstance(fixture, dict)
    review = run / "review"
    (review / "diff.patch").write_bytes(diff)
    for record in (fixture["manifest"], fixture["plan"]):
        record["review_input_sha256"] = hashlib.sha256(diff).hexdigest()
    _rewrite_native_outputs(fixture, source, diff, findings or [], full=True, request=request, coverage_paths=paths)
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((review / selected["output_path"]).read_bytes())
    for name in ("current-source.json", "source-1.json"):
        (run / name).write_bytes(source)
    for name in ("current.diff", "round-1.diff"):
        (run / name).write_bytes(diff)
    snapshot = json.loads(source)
    ledger_path = run / "loop-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["current_snapshot"] = {"revision": snapshot["revision"], "diff_digest": hashlib.sha256(diff).hexdigest()}
    ledger["rounds"][0]["snapshot"] = ledger["current_snapshot"]
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    loop_path = run / "loop-evidence.json"
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    loop.update(repository=repository.as_posix(), scope_paths=paths, request=request)
    loop_path.write_text(json.dumps(loop), encoding="utf-8")

    # Rollout fixture identities must be unique in the shared native session store.
    home = evidence["codex_home"]
    assert isinstance(home, Path)
    for file in [*review.rglob("*.json"), *home.rglob("*.jsonl")]:
        content = file.read_text(encoding="utf-8")
        for old in ("child-1", "child-2"):
            content = content.replace(old, f"{identity}-{old}")
        file.write_text(content, encoding="utf-8", newline="\n")
    for file in (home / "sessions").glob("rollout-*.jsonl"):
        file.rename(file.with_name(file.name.replace("child-", f"{identity}-child-")))
    manifest_path = review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inspection_execution"]["plan_sha256"] = hashlib.sha256(
        (review / "inspection-plan.json").read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["rounds"][0]["reviewer"]["identity"] = f"{identity}-child-2"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")


def _collect_chunk_sessions(evidence: dict[str, object], sessions: Path) -> None:
    """Keep one active parent rollout containing each independent child spawn."""
    home = evidence["codex_home"]
    assert isinstance(home, Path)
    for file in (home / "sessions").glob("*.jsonl"):
        target = sessions / file.name
        if file.name == "rollout-parent-thread.jsonl" and target.exists():
            rows = file.read_text(encoding="utf-8").splitlines(keepends=True)
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.writelines(rows[1:])
        else:
            shutil.copy2(file, target)


@pytest.mark.packaging
def test_chunked_coordinator_validates_real_children_and_interaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coordinator accepts complete native child reviews and rejects changed child evidence."""
    repository = tmp_path / "repository"
    repository.mkdir()
    for name in ("alpha.py", "beta.py", "gamma.py"):
        (repository / name).write_text(f"VALUE = '{name}'\n", encoding="utf-8", newline="\n")
    for arguments in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)
    chunks = tmp_path / "chunks"
    checker = Path(__file__).resolve().parents[1] / "skills" / "challenge-resolve" / "chunk_diff.py"
    request = {
        "goal": "Review all three files",
        "specification": "All three files retain their values",
        "done_when": "Each child and their interactions are independently clean",
    }
    planned = subprocess.run(
        [
            sys.executable,
            str(checker),
            "plan",
            "--repository",
            str(repository),
            "--out",
            str(chunks),
            "--scope-mode",
            "all",
            "--budget-bytes",
            "1",
            "--goal",
            request["goal"],
            "--specification",
            request["specification"],
            "--done-when",
            request["done_when"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stderr
    manifest = json.loads((chunks / "chunks.json").read_text(encoding="utf-8"))
    assert len(manifest["chunks"]) == 3
    session_home = tmp_path / "session-home"
    sessions = session_home / "sessions"
    sessions.mkdir(parents=True)
    result_map = {"schema_version": 1, "chunks": [], "interactions": [], "groups": []}
    for left, right in itertools.combinations(manifest["chunks"], 2):
        result_map["interactions"].append(
            {
                "chunk_ids": [left["id"], right["id"]],
                "decision": "reviewed",
                "evidence": f"{left['id']} and {right['id']} jointly define the fixture behavior",
                "result_path": "runs/interaction/result.json",
            }
        )
    result_map["groups"].append(
        {
            "chunk_ids": [chunk["id"] for chunk in manifest["chunks"]],
            "evidence": "All three chunks jointly define the fixture contract",
            "result_path": "runs/interaction/result.json",
        }
    )
    for index, chunk in enumerate(manifest["chunks"], 1):
        result_map["chunks"].append({"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"})
        run = chunks / "runs" / chunk["id"]
        run.mkdir(parents=True)
        evidence: dict[str, object] = {}
        candidate = _write_loop(run, monkeypatch, evidence_out=evidence)
        _bind_real_chunk_run(
            run,
            evidence,
            repository,
            chunk["scope_paths"],
            (chunks / chunk["source_path"]).read_bytes(),
            (chunks / chunk["diff_path"]).read_bytes(),
            request,
            f"chunk-{index}",
        )
        (run / "result.json").write_bytes(candidate.read_bytes())
        _collect_chunk_sessions(evidence, sessions)

    run = chunks / "runs" / "interaction"
    run.mkdir(parents=True)
    evidence = {}
    candidate = _write_loop(run, monkeypatch, evidence_out=evidence)
    paths = manifest["inventory"]
    decisions = [
        {key: decision[key] for key in ("chunk_ids", "decision", "evidence")} for decision in result_map["interactions"]
    ] + [
        {"chunk_ids": group["chunk_ids"], "decision": "reviewed", "evidence": group["evidence"]}
        for group in result_map["groups"]
    ]
    assessment = "Interaction assessment:\n" + json.dumps(decisions, sort_keys=True, ensure_ascii=True)
    interaction_request = request | {"specification": request["specification"] + "\n" + assessment}
    chunker_source = _load_loop_validator().capture_source_snapshot(repository, paths)
    source = (json.dumps(chunker_source, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    _bind_real_chunk_run(run, evidence, repository, paths, source, b"", interaction_request, "interaction")
    (run / "result.json").write_bytes(candidate.read_bytes())
    _collect_chunk_sessions(evidence, sessions)
    (chunks / "results.json").write_text(json.dumps(result_map), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(session_home))
    checked = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
            "--summary",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    summary = json.loads(checked.stdout)
    assert len(summary["runs"]) == 4

    coordinator = _write_loop(tmp_path, monkeypatch)
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 3
    handoff["outcome"] = {"title": "Chunk review", "summary": "All declared child and interaction reviews are clean."}
    handoff["tables"][0]["rows"][0]["cells"] = ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]
    handoff["child_runs"] = summary["runs"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(coordinator.read_text(encoding="utf-8"))
    result["metadata"].pop("adversarial_loop", None)
    result["metadata"]["challenge_scope"] = {
        "mode": "chunked",
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }
    result["metadata"]["chunked_review"] = summary
    result["metadata"]["challenge_origin"] = manifest["origin"]
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    coordinator.write_text(json.dumps(result), encoding="utf-8")
    (tmp_path / "loop-ledger.json").unlink()
    monkeypatch.setenv("CODEX_HOME", str(session_home))
    validator = _load_shared_validator()
    validator.validate("challenge-resolve", tmp_path, coordinator)
    (tmp_path / "result.json").write_bytes(coordinator.read_bytes())
    validator.validate("challenge-resolve", tmp_path, tmp_path / "result.json")
    original_result_map = (chunks / "results.json").read_bytes()
    original_canonical = (tmp_path / "result.json").read_bytes()
    result_map["groups"][0]["evidence"] = "Substituted group conclusion"
    (chunks / "results.json").write_text(json.dumps(result_map), encoding="utf-8", newline="\n")
    tampered_coordinator = json.loads(original_canonical)
    tampered_coordinator["metadata"]["chunked_review"]["results_sha256"] = hashlib.sha256(
        (chunks / "results.json").read_bytes()
    ).hexdigest()
    (tmp_path / "result.json").write_text(json.dumps(tampered_coordinator), encoding="utf-8", newline="\n")
    with pytest.raises(
        SystemExit, match="adversarial-loop-chunk-coverage-invalid:.*chunk-interaction-assessment-missing"
    ):
        validator.validate("challenge-resolve", tmp_path, tmp_path / "result.json")
    (tmp_path / "result.json").write_bytes(original_canonical)
    (chunks / "results.json").write_bytes(original_result_map)
    for result_path in (coordinator, tmp_path / "result.json"):
        tampered = json.loads(result_path.read_text(encoding="utf-8"))
        tampered["metadata"]["challenge_origin"] = {"kind": "continuation", "caller_run": str(tmp_path)}
        result_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(SystemExit, match="adversarial-loop-chunk-origin-mismatch"):
            validator.validate("challenge-resolve", tmp_path, result_path)
        result_path.write_text(json.dumps(result), encoding="utf-8")
    manifest_path = chunks / "chunks.json"
    original_manifest = manifest_path.read_bytes()
    canonical = tmp_path / "result.json"
    for archive_version in (3, 4):
        archive_manifest = json.loads(original_manifest)
        archive_manifest["schema_version"] = archive_version
        manifest_path.write_text(json.dumps(archive_manifest), encoding="utf-8", newline="\n")
        archived = subprocess.run(
            [sys.executable, str(checker), "check", "--manifest", str(manifest_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert archived.returncode != 0
        assert "chunk-manifest-invalid" in archived.stderr
        with pytest.raises(
            SystemExit,
            match="adversarial-loop-chunk-coverage-invalid:chunk-diff-error:chunk-manifest-invalid",
        ):
            validator.validate("challenge-resolve", tmp_path, canonical)
    manifest_path.write_bytes(original_manifest)

    # The first child has an independently clean review but a failed test gate.
    failed_run = chunks / "runs" / manifest["chunks"][0]["id"]
    failed_gates_path = failed_run / "gates.json"
    failed_gates = json.loads(failed_gates_path.read_text(encoding="utf-8"))
    next(check for check in failed_gates["checks"] if check["id"] == "tests").update(status="fail", exit_code=1)
    failed_gates.update(status="fail", checks_failed=["tests"])
    failed_gates_path.write_text(json.dumps(failed_gates), encoding="utf-8")
    failed_handoff_path = failed_run / "final-handoff.json"
    failed_handoff = json.loads(failed_handoff_path.read_text(encoding="utf-8"))
    next(check for check in failed_handoff["verification"] if check["check"] == "tests")["status"] = "fail"
    failed_handoff["outcome"]["summary"] = "Independent review is clean; test gate failed."
    failed_handoff_path.write_text(json.dumps(failed_handoff), encoding="utf-8")
    failed_validation = _load_finalizer().render_files(
        failed_handoff_path, failed_run / "final.md", failed_run / "final-handoff.validation.json"
    )
    failed_result_path = failed_run / "result.json"
    failed_result = json.loads(failed_result_path.read_text(encoding="utf-8"))
    failed_result.update(status="fail", checks_failed=["tests"])
    failed_result["metadata"]["final_handoff"].update(
        {key: failed_validation[key] for key in ("handoff_sha256", "rendered_sha256")}
    )
    failed_result_path.write_text(json.dumps(failed_result), encoding="utf-8")
    stopped_map = {"schema_version": 1, "chunks": [result_map["chunks"][0]], "interactions": [], "groups": []}
    (chunks / "results.json").write_text(json.dumps(stopped_map), encoding="utf-8")
    stopped = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check-stopped",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
            "--summary",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stopped.returncode == 0, stopped.stderr
    stopped_summary = json.loads(stopped.stdout)
    assert stopped_summary["status"] == "stopped"
    assert stopped_summary["pending_chunks"] == [chunk["id"] for chunk in manifest["chunks"][1:]]
    assert stopped_summary["pending_interactions"] == [decision["chunk_ids"] for decision in result_map["interactions"]]
    assert stopped_summary["pending_groups"] == []
    coordinator = tmp_path / "result.candidate.json"
    gates_path = tmp_path / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    next(check for check in gates["checks"] if check["id"] == "review").update(status="fail", exit_code=1)
    gates.update(status="fail", checks_failed=["review"])
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 3
    handoff["outcome"] = {"title": "Chunk review", "summary": "Child review stopped; full review remains open."}
    handoff["tables"][0]["rows"][0]["cells"] = ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]
    handoff["child_runs"] = stopped_summary["runs"]
    handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": gates["checks"][-1]["stdout"]}
    handoff["remaining"] = [
        {
            "row_id": "1",
            "item": "Full scope has no independent review.",
            "owner": "parent",
            "next_action": "Seek a full scope review.",
        }
    ]
    handoff["next_steps"] = ["1"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
    for run_summary in stopped_summary["runs"]:
        assert f"{run_summary['result_path']}: {' → '.join(map(str, run_summary['scores']))}" in rendered
    assert "combined score" not in rendered.casefold()
    result = json.loads(coordinator.read_text(encoding="utf-8"))
    result.update(status="fail", checks_failed=["review"])
    result["metadata"].pop("adversarial_loop", None)
    result["metadata"]["challenge_scope"] = {
        "mode": "chunked",
        "manifest_path": "chunks/chunks.json",
        "results_path": "chunks/results.json",
    }
    result["metadata"]["chunked_review"] = stopped_summary
    result["metadata"]["challenge_origin"] = manifest["origin"]
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    coordinator.write_text(json.dumps(result), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(session_home))
    validator = _load_shared_validator()
    validator.validate("challenge-resolve", tmp_path, coordinator)

    child_evidence_path = chunks / "runs" / manifest["chunks"][0]["id"] / "loop-evidence.json"
    child_evidence = json.loads(child_evidence_path.read_text(encoding="utf-8"))
    child_evidence["request"]["goal"] = "Unreviewed replacement goal"
    child_evidence_path.write_text(json.dumps(child_evidence), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-chunk-coverage-invalid"):
        validator.validate("challenge-resolve", tmp_path, coordinator)
    child_evidence["request"]["goal"] = request["goal"]
    child_evidence_path.write_text(json.dumps(child_evidence), encoding="utf-8")
    child_source = chunks / manifest["chunks"][0]["source_path"]
    child_source.write_bytes(child_source.read_bytes() + b" ")
    with pytest.raises(SystemExit, match="adversarial-loop-chunk-coverage-invalid"):
        validator.validate("challenge-resolve", tmp_path, coordinator)


@pytest.mark.packaging
def test_chunk_recovery_requires_prior_signature_in_real_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a clean recovery when its authenticated current reviewer omits an earlier finding."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    for arguments in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)
    finding = {
        "signature": "caller-origin",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["Caller accepts the invalid value."],
    }
    snapshot = {"revision": "earlier", "diff_digest": "0" * 64}
    prior = {
        "schema_version": 1,
        "implementation_author": "parent-thread",
        "current_snapshot": snapshot,
        "rounds": [
            {
                "index": 1,
                "reviewer": {"identity": "earlier-child", "independent": True},
                "snapshot": snapshot,
                "report_path": "review-1.md",
                "findings": [finding],
            }
        ],
    }
    prior_path = tmp_path / "caller-run" / "loop-ledger.json"
    prior_path.parent.mkdir()
    prior_path.write_text(json.dumps(prior), encoding="utf-8", newline="\n")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            [
                {
                    "signature": "caller-origin",
                    "source_paths": ["alpha.py"],
                    "review_kind": "chunk",
                    "chunk_ids": ["chunk-001"],
                }
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    chunks = tmp_path / "chunks"
    checker = Path(__file__).resolve().parents[1] / "skills" / "challenge-resolve" / "chunk_diff.py"
    planned = subprocess.run(
        [
            sys.executable,
            str(checker),
            "plan",
            "--repository",
            str(repository),
            "--out",
            str(chunks),
            "--scope-mode",
            "all",
            "--goal",
            "Review caller behavior",
            "--specification",
            "Invalid values must be rejected",
            "--done-when",
            "Independent review resolves every prior finding",
            "--prior-ledger",
            str(prior_path),
            "--prior-coverage",
            str(coverage_path),
            "--caller-run",
            str(prior_path.parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stderr
    manifest = json.loads((chunks / "chunks.json").read_text(encoding="utf-8"))
    chunk = manifest["chunks"][0]
    request = manifest["request"]
    assert "Caller accepts the invalid value." in request["specification"]
    run = chunks / "runs" / "chunk-001"
    run.mkdir(parents=True)
    resolved = finding | {
        "disposition": "verified-fixed",
        "evidence": ["Caller now rejects the original invalid value."],
    }
    evidence: dict[str, object] = {}
    candidate = _write_loop(run, monkeypatch, findings=[resolved], evidence_out=evidence)
    _bind_real_chunk_run(
        run,
        evidence,
        repository,
        chunk["scope_paths"],
        (chunks / chunk["source_path"]).read_bytes(),
        (chunks / chunk["diff_path"]).read_bytes(),
        request,
        "recovery",
        findings=[resolved],
    )
    (run / "result.json").write_bytes(candidate.read_bytes())
    result_map = {
        "schema_version": 1,
        "chunks": [{"id": "chunk-001", "result_path": "runs/chunk-001/result.json"}],
        "interactions": [],
        "groups": [],
    }
    (chunks / "results.json").write_text(json.dumps(result_map), encoding="utf-8", newline="\n")
    checked = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    omitted_run = chunks / "runs" / "omitted-finding"
    omitted_run.mkdir()
    omitted_evidence: dict[str, object] = {}
    omitted_candidate = _write_loop(omitted_run, monkeypatch, evidence_out=omitted_evidence)
    _bind_real_chunk_run(
        omitted_run,
        omitted_evidence,
        repository,
        chunk["scope_paths"],
        (chunks / chunk["source_path"]).read_bytes(),
        (chunks / chunk["diff_path"]).read_bytes(),
        request,
        "omitted",
    )
    (omitted_run / "result.json").write_bytes(omitted_candidate.read_bytes())
    result_map["chunks"][0]["result_path"] = "runs/omitted-finding/result.json"
    (chunks / "results.json").write_text(json.dumps(result_map), encoding="utf-8", newline="\n")
    checked = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "prior-finding-not-resolved:caller-origin" in checked.stderr
    omitted_gates_path = omitted_run / "gates.json"
    omitted_gates = json.loads(omitted_gates_path.read_text(encoding="utf-8"))
    next(check for check in omitted_gates["checks"] if check["id"] == "review").update(status="fail", exit_code=1)
    omitted_gates.update(status="fail", checks_failed=["review"])
    omitted_gates_path.write_text(json.dumps(omitted_gates), encoding="utf-8")
    omitted_handoff_path = omitted_run / "final-handoff.json"
    omitted_handoff = json.loads(omitted_handoff_path.read_text(encoding="utf-8"))
    omitted_handoff["verification"][-1]["status"] = "fail"
    omitted_handoff["remaining"] = [
        {
            "row_id": "1",
            "item": "Prior finding remains unresolved.",
            "owner": "parent",
            "next_action": "Resolve the prior finding in a fresh independent review.",
        }
    ]
    omitted_handoff["next_steps"] = ["1"]
    omitted_handoff_path.write_text(json.dumps(omitted_handoff), encoding="utf-8")
    omitted_validation = _load_finalizer().render_files(
        omitted_handoff_path, omitted_run / "final.md", omitted_run / "final-handoff.validation.json"
    )
    omitted_result_path = omitted_run / "result.json"
    omitted_result = json.loads(omitted_result_path.read_text(encoding="utf-8"))
    omitted_result.update(status="fail", checks_failed=["review"])
    omitted_result["metadata"]["final_handoff"].update(
        {key: omitted_validation[key] for key in ("handoff_sha256", "rendered_sha256")}
    )
    omitted_result_path.write_text(json.dumps(omitted_result), encoding="utf-8")
    stopped_map = result_map | {"schema_version": 1}
    (chunks / "results.json").write_text(json.dumps(stopped_map), encoding="utf-8", newline="\n")
    stopped = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check-stopped",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
            "--summary",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stopped.returncode == 0, stopped.stderr
    assert json.loads(stopped.stdout)["pending_prior_signatures"] == ["caller-origin"]
    result_map["chunks"][0]["result_path"] = "runs/chunk-001/result.json"
    (chunks / "results.json").write_text(json.dumps(result_map), encoding="utf-8", newline="\n")
    manifest.pop("prior_findings")
    manifest.pop("origin")
    manifest["schema_version"] = 2
    (chunks / "chunks.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    downgraded = subprocess.run(
        [
            sys.executable,
            str(checker),
            "check",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "chunk-manifest-invalid" in downgraded.stderr


def test_historical_loop_result_keeps_its_compact_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep pre-change canonical results readable without weakening new candidates."""
    candidate = _write_loop(tmp_path, monkeypatch)
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["columns"] = ["Iteration", "Open findings", "Weighted score", "Decision", "Evidence"]
    handoff["tables"][0]["rows"][0]["cells"] = [
        "1",
        "security=0, critical=0, high=0, medium=0, low=0, nit=0",
        "0",
        "clean",
        "review-1.md",
    ]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(candidate.read_text(encoding="utf-8"))
    del result["metadata"]["challenge_table_contract_version"]
    del result["metadata"]["challenge_scope"]
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    canonical = tmp_path / "result.json"
    canonical.write_text(json.dumps(result), encoding="utf-8")
    result["metadata"]["challenge_scope"] = {"mode": "single"}
    candidate.write_text(json.dumps(result), encoding="utf-8")

    assert json.loads(canonical.read_text(encoding="utf-8"))["metadata"]["adversarial_loop"]["scores"] == [0]
    with pytest.raises(SystemExit, match="adversarial-loop-table-contract-required"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, canonical)
    with pytest.raises(SystemExit, match="adversarial-loop-table-contract-required"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, candidate)


def test_historical_ranked_totals_remain_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read a prior version-2 result while requiring splits for new candidates."""
    candidate = _write_loop(tmp_path, monkeypatch)
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["rows"][0]["cells"][1:8] = ["0"] * 7
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(candidate.read_text(encoding="utf-8"))
    result["metadata"]["challenge_table_contract_version"] = 2
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    historical = tmp_path / "result.json"
    historical.write_text(json.dumps(result), encoding="utf-8")
    candidate.write_text(json.dumps(result), encoding="utf-8")

    validator = _load_shared_validator()
    assert json.loads(historical.read_text(encoding="utf-8"))["metadata"]["challenge_table_contract_version"] == 2
    with pytest.raises(SystemExit, match="adversarial-loop-table-contract-version-required"):
        validator.validate("challenge-resolve", tmp_path, historical)
    with pytest.raises(SystemExit, match="adversarial-loop-table-contract-version-required"):
        validator.validate("challenge-resolve", tmp_path, candidate)


def test_public_validator_requires_bound_parent_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a new result whose finding actions are missing or bypassed."""
    result_path = _write_loop(tmp_path, monkeypatch)
    validator = _load_shared_validator()
    (tmp_path / "loop-actions.json").unlink()
    with pytest.raises(SystemExit, match="adversarial-loop-invalid-actions:"):
        validator.validate("challenge-resolve", tmp_path, result_path)

    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"].pop("action_contract_version")
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-action-contract-required"):
        validator.validate("challenge-resolve", tmp_path, result_path)
    final_path = tmp_path / "result.json"
    final_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(SystemExit, match="adversarial-loop-action-contract-required"):
        validator.validate("challenge-resolve", tmp_path, final_path)


def test_current_result_rejects_old_action_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep schema-1 action records readable without using them to certify a current loop."""
    result_path = _write_loop(tmp_path, monkeypatch)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["action_contract_version"] = 1
    result_path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(SystemExit, match="adversarial-loop-action-contract-required"):
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


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

    _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)
    ledger = json.loads((tmp_path / "loop-ledger.json").read_text(encoding="utf-8"))
    assert ledger["rounds"][0]["findings"] == findings


def test_scored_review_remains_valid_with_failed_review_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a scored review visible when its review gate stops completion."""
    result_path = _write_loop(tmp_path, monkeypatch)
    gates_path = tmp_path / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    review = next(check for check in gates["checks"] if check["id"] == "review")
    review.update(status="fail", exit_code=1)
    gates.update(status="fail", checks_failed=["review"])
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    handoff_path = tmp_path / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["verification"][-1]["status"] = "fail"
    handoff["remaining"] = [
        {
            "row_id": "1",
            "item": "Independent review has stopped.",
            "owner": "parent",
            "next_action": "Resolve the review gate before completion.",
        }
    ]
    handoff["next_steps"] = ["1"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(status="fail", checks_failed=["review"])
    result["metadata"]["final_handoff"].update({key: validation[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result), encoding="utf-8")

    _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)
    assert result["metadata"]["adversarial_loop"]["scores"] == [0]


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
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


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
        handoff["tables"][0]["rows"][0]["cells"][8] = "converging"
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
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


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
        "0 + 1",
        "0 + 0",
        "0 + 0",
        "0 + 0",
        "0 + 0",
        "0 + 1",
        "0 + 21",
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

    _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


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
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


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
    (tmp_path / "loop-actions.json").write_text(json.dumps({"schema_version": 2, "rounds": []}), encoding="utf-8")
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
        cells=["not-run", *(["N/A"] * 7), "independence-unavailable", "loop-report.md"],
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
            _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)
    else:
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)
    rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
    assert (
        "| not-run | N/A | N/A | N/A | N/A | N/A | N/A | N/A | independence-unavailable | loop-report.md |" in rendered
    )
    assert "| 1 |" not in rendered
