"""Check scoped coordinator promotion against real retained child evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_adversarial_loop_artifacts import _bind_real_chunk_run, _collect_chunk_sessions, _write_loop
from test_final_handoff import _load_shared_validator
from test_loop_review_evidence import _module, _repository, _rewrite_native_outputs
from test_scoped_interactions import _write_authenticated_verdict

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CHECKER = PLUGIN_ROOT / "skills" / "challenge-resolve" / "chunk_diff.py"


def _scope(mode: str) -> dict[str, str]:
    """Return one fixed coordinator declaration."""
    return {"mode": mode, "manifest_path": "chunks/chunks.json", "results_path": "chunks/results.json"}


def _json(path: Path, value: object) -> None:
    """Write a fixture artifact with portable text bytes."""
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")


def _bound_child(
    chunks: Path,
    repository: Path,
    chunk: dict[str, Any],
    request: dict[str, str],
    requirements: list[dict[str, Any]],
    identity: str,
    sessions: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: str | None = None,
) -> str:
    """Bind an actual native reviewer fixture and its assigned interaction verdicts."""
    run = chunks / "runs" / identity
    run.mkdir(parents=True)
    payload: dict[str, Any] = {}
    candidate = _write_loop(run, monkeypatch, evidence_out=payload)
    assigned = request | {
        "specification": request["specification"]
        + "\nInteraction assessment:\n"
        + json.dumps(requirements, sort_keys=True, ensure_ascii=True)
    }
    source = (chunks / chunk["source_path"]).read_bytes()
    diff = (chunks / chunk["diff_path"]).read_bytes()
    _bind_real_chunk_run(run, payload, repository, chunk["scope_paths"], source, diff, assigned, identity)
    evidence = json.loads((run / "loop-evidence.json").read_bytes())
    evidence.update(schema_version=3, interaction_requirements=requirements)
    if base is not None:
        evidence["diff_base_oid"] = base
    _json(run / "loop-evidence.json", evidence)
    native = payload["fixture"]
    native["manifest"] = json.loads((run / "review/specialist-manifest.json").read_bytes())
    native["plan"] = json.loads((run / "review/inspection-plan.json").read_bytes())
    verdicts = []
    for requirement in requirements:
        verdict: dict[str, Any] = {"id": requirement["id"], "verdict": "reviewed", "evidence": ["Checked source."]}
        if requirement["id"] == "interaction-inventory-audit":
            verdict["unit_verdicts"] = [{"id": unit, "verdict": "reviewed"} for unit in requirement["unit_ids"]]
        verdicts.append(verdict)
    _write_authenticated_verdict(native, verdicts)
    (run / "result.json").write_bytes(candidate.read_bytes())
    _collect_chunk_sessions(payload, sessions)
    return f"runs/{identity}/result.json"


def _scoped_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pinned: bool = False
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Create a real scoped planner, native child, audit, and shared host event store."""
    repository = _repository(tmp_path)
    origin = repository
    base = None
    if pinned:
        base = subprocess.check_output(["git", "-C", str(origin), "rev-parse", "HEAD"], text=True).strip()
        (origin / "widget.py").write_bytes(b"VALUE = 2\n")
        receipt = tmp_path / "receipt"
        collected = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared/collect_diff.py"),
                "--review-worktree",
                "--repository",
                str(origin),
                "--out",
                str(receipt),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert collected.returncode == 0, collected.stderr
        repository = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    chunks = tmp_path / "chunks"
    request = {"goal": "Review the value", "specification": "The value remains one", "done_when": "Review is clean"}
    planned = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "plan",
            "--repository",
            str(repository),
            "--out",
            str(chunks),
            "--scope-mode",
            "all",
            "--budget-bytes",
            "500000",
            "--goal",
            request["goal"],
            "--specification",
            request["specification"],
            "--done-when",
            request["done_when"],
        ]
        + (["--diff-base", base, "--origin-repository", str(origin)] if pinned else []),
        capture_output=True,
        text=True,
        check=False,
    )
    assert planned.returncode == 0, planned.stderr
    assignments = chunks / "assignments.json"
    _json(assignments, {"interactions": [], "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001"]}]})
    scoped = chunks / "scoped.json"
    generated = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "scoped-plan",
            "--manifest",
            str(chunks / "chunks.json"),
            "--assignments",
            str(assignments),
            "--out",
            str(scoped),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    (chunks / "chunks.json").write_bytes(scoped.read_bytes())
    manifest = json.loads(scoped.read_bytes())
    assert manifest["schema_version"] == 2
    assert len(manifest["chunks"]) == 1

    sessions = tmp_path / "session-home/sessions"
    sessions.mkdir(parents=True)
    child = _bound_child(chunks, repository, manifest["chunks"][0], request, [], "child", sessions, monkeypatch, base)
    checker = _module(CHECKER)
    audit_requirements = checker._scoped_audit_requirements(manifest, manifest["audit_assignments"][0])
    audit = _bound_child(
        chunks, repository, manifest["chunks"][0], request, audit_requirements, "audit", sessions, monkeypatch, base
    )
    _json(
        chunks / "results.json",
        {
            "schema_version": 2,
            "chunks": [{"id": manifest["chunks"][0]["id"], "result_path": child}],
            "audits": [{"id": "audit-001", "result_path": audit}],
        },
    )
    monkeypatch.setenv("CODEX_HOME", str(sessions.parent))
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    return _load_shared_validator(), manifest, {"scope": _scope("scoped"), "request": request}


def test_scoped_coordinator_uses_actual_authenticated_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Promote scoped coverage only after real reviewer and independent audit checks."""
    validator, manifest, context = _scoped_fixture(tmp_path, monkeypatch, pinned=True)
    summary = validator._validate_challenge_scope({"challenge_scope": context["scope"]}, tmp_path, summary=True)
    assert summary["schema_version"] == 2
    assert len(summary["runs"]) == 2
    handoff = {
        "presentation_version": 8,
        "child_runs": summary["runs"],
        "tables": [{"rows": [{"cells": ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]}]}],
        "confidence": {"limits": [f"(-0.00) {summary['coverage_limit']}"]},
        "remaining": [],
        "next_steps": [],
    }
    _json(tmp_path / "final-handoff.json", handoff)
    result = {
        "status": "pass",
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "metadata": {
            "challenge_scope": context["scope"],
            "chunked_review": summary,
            "challenge_origin": manifest["origin"],
        },
    }
    gates = {"checks": [{"id": "review", "status": "pass"}]}

    validator._validate_chunked_coordinator(result, tmp_path, gates, candidate=True, current_result=True)
    result["metadata"]["chunked_review"]["runs"][0]["scores"] = [12, 0]
    with pytest.raises(SystemExit, match="adversarial-loop-chunk-summary-mismatch"):
        validator._validate_chunked_coordinator(result, tmp_path, gates, candidate=True, current_result=True)


def test_scoped_coordinator_requires_verified_comparison_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route scoped evidence to the real checker but never bypass its quality gate."""
    validator, _manifest, context = _scoped_fixture(tmp_path, monkeypatch)
    metadata = {"challenge_scope": context["scope"], "challenge_table_contract_version": 2}
    assert validator._is_chunked_coordinator("challenge-resolve", {"metadata": metadata})

    with pytest.raises(
        SystemExit, match=r"adversarial-loop-chunk-coverage-invalid:.*scoped-comparison-profile-unverified"
    ):
        validator._validate_challenge_scope(metadata, tmp_path, summary=True)


@pytest.mark.parametrize(
    ("mode", "version"),
    [
        pytest.param("scoped", 1, id="scoped-cannot-promote-legacy"),
        pytest.param("chunked", 2, id="legacy-cannot-promote-scoped"),
    ],
)
def test_coordinator_mode_never_cross_promotes_manifest_versions(tmp_path: Path, mode: str, version: int) -> None:
    """A scope label cannot reinterpret another coordinator's manifest semantics."""
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    _json(chunks / "chunks.json", {"schema_version": version})
    _json(chunks / "results.json", {"schema_version": version})

    with pytest.raises(SystemExit, match=r"adversarial-loop-.*schema.*mismatch"):
        _load_shared_validator()._validate_challenge_scope({"challenge_scope": _scope(mode)}, tmp_path)


@pytest.mark.parametrize(
    "mode", [pytest.param(["scoped"], id="list-mode"), pytest.param({"scoped": True}, id="object-mode")]
)
def test_coordinator_rejects_nonstring_mode(tmp_path: Path, mode: object) -> None:
    """Untrusted scope data must fail closed without a validator exception."""
    scope = {"mode": mode, "manifest_path": "chunks/chunks.json", "results_path": "chunks/results.json"}

    with pytest.raises(SystemExit, match="adversarial-loop-challenge-scope-invalid"):
        _load_shared_validator()._validate_challenge_scope({"challenge_scope": scope}, tmp_path)


def test_scoped_result_rejects_forged_checker_summary(tmp_path: Path) -> None:
    """A plausible parent-supplied summary cannot replace real coordinator checking."""
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    _json(chunks / "chunks.json", {"schema_version": 2})
    _json(
        chunks / "results.json",
        {"schema_version": 2, "chunks": [], "audits": [{"id": "audit-001", "result_path": "missing"}]},
    )
    forged = {"schema_version": 2, "runs": [{"result_path": "missing", "scores": [0], "decision": "clean"}]}
    result = {
        "status": "pass",
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "metadata": {"challenge_scope": _scope("scoped"), "chunked_review": forged},
    }

    with pytest.raises(SystemExit, match="adversarial-loop-chunk-coverage-invalid"):
        _load_shared_validator()._validate_chunked_coordinator(
            result, tmp_path, {"checks": [{"id": "review", "status": "pass"}]}, candidate=True, current_result=True
        )


@pytest.mark.parametrize("deduction", ["(-0.00)", "(-0.05)"])
def test_scoped_coverage_limit_is_visible_without_forcing_a_deduction(deduction: str) -> None:
    """The checked limitation remains visible whether or not it reduces confidence."""
    limit = "Independent source-aware review cannot prove every semantic interaction was discovered."
    checked = {"coverage_limit": limit}
    handoff = {"confidence": {"limits": [f"{deduction} {limit}"]}, "remaining": []}

    _load_shared_validator()._validate_scoped_coverage_limit(checked, handoff)


def test_scoped_coverage_limit_can_be_a_visible_remaining_item() -> None:
    """An actionable remaining item can disclose the exact checked coverage limit."""
    limit = "Independent source-aware review cannot prove every semantic interaction was discovered."
    checked = {"coverage_limit": limit}
    handoff = {"confidence": {"limits": []}, "remaining": [{"item": limit}]}

    _load_shared_validator()._validate_scoped_coverage_limit(checked, handoff)


@pytest.mark.parametrize(
    "limits", [pytest.param([], id="omitted"), pytest.param(["(-0.00) No limitations."], id="replaced")]
)
def test_scoped_coverage_limit_cannot_be_omitted_or_replaced(limits: list[str]) -> None:
    """A checked semantic-coverage limit cannot disappear from the human handoff."""
    checked = {
        "coverage_limit": "Independent source-aware review cannot prove every semantic interaction was discovered."
    }
    handoff = {"confidence": {"limits": limits}, "remaining": []}

    with pytest.raises(SystemExit, match="adversarial-loop-scoped-coverage-limit-missing"):
        _load_shared_validator()._validate_scoped_coverage_limit(checked, handoff)


def test_scoped_stopped_result_requires_a_real_stopped_checker(tmp_path: Path) -> None:
    """Fail closed rather than reinterpreting incomplete scoped results as legacy stopped coverage."""
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    _json(chunks / "chunks.json", {"schema_version": 2})
    _json(
        chunks / "results.json",
        {"schema_version": 2, "chunks": [], "audits": [{"id": "audit-001", "result_path": None}]},
    )

    with pytest.raises(SystemExit, match="adversarial-loop-chunk-coverage-invalid"):
        _load_shared_validator()._validate_challenge_scope(
            {"challenge_scope": _scope("scoped")}, tmp_path, summary=True, stopped=True
        )


def test_single_schema_three_requires_real_validated_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Promote schema-three single results only after their real reviewer evidence passes."""
    payload: dict[str, Any] = {}
    candidate = _write_loop(tmp_path, monkeypatch, evidence_out=payload)
    evidence_path = tmp_path / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_bytes())
    evidence.update(schema_version=3, interaction_requirements=[])
    evidence["request"]["specification"] += "\nInteraction assessment:\n[]"
    _json(evidence_path, evidence)
    _rewrite_native_outputs(
        payload["fixture"],
        (tmp_path / "source-1.json").read_bytes(),
        (tmp_path / "round-1.diff").read_bytes(),
        [],
        full=True,
        request=evidence["request"],
        coverage_paths=["widget.py"],
    )
    _write_authenticated_verdict(payload["fixture"], [])
    validator = _load_shared_validator()

    validator.validate("challenge-resolve", tmp_path, candidate)
    downgraded = dict(evidence)
    downgraded["schema_version"] = 2
    downgraded.pop("interaction_requirements")
    _json(evidence_path, downgraded)
    with pytest.raises(SystemExit, match=r"adversarial-loop-evidence-invalid:.*report-block-shape-invalid"):
        validator.validate("challenge-resolve", tmp_path, candidate)
    evidence.pop("origin")
    _json(evidence_path, evidence)
    with pytest.raises(SystemExit, match="adversarial-loop-evidence-invalid"):
        validator.validate("challenge-resolve", tmp_path, candidate)
