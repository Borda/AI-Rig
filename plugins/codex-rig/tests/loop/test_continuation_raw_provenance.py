"""Verify continuation verdicts preserve reviewer-authenticated prior finding identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_loop_review_evidence import (
    REQUEST,
    _ledger_digest,
    _loop_evidence_run,
    _read_ledger,
    _rewrite_native_outputs,
    _write_ledger,
)


def _continuation(tmp_path: Path, *, raw_tier: str, raw_structural: bool) -> dict[str, Any]:
    """Create a continuation where parent triage preserves the earlier recorded tier."""
    prior_finding = {
        "signature": "missing-guard",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["The widget lacks a guard."],
    }
    reviewer_finding = {
        "signature": "missing-guard",
        "tier": raw_tier,
        "structural": raw_structural,
        "disposition": "verified-fixed",
        "evidence": ["The guard is present."],
    }
    prior_root, current_root = tmp_path / "prior", tmp_path / "current"
    prior_root.mkdir()
    current_root.mkdir()
    prior = _loop_evidence_run(prior_root, findings=[prior_finding])
    current = _loop_evidence_run(current_root, findings=[reviewer_finding], current_contract=True)
    run, fixture = current["run"], current["fixture"]
    prior_block = "Prior findings to reassess:\n" + json.dumps([prior_finding], sort_keys=True, ensure_ascii=True)
    request = REQUEST | {"specification": REQUEST["specification"] + "\n" + prior_block}
    _rewrite_native_outputs(
        fixture,
        (run / "source-1.json").read_bytes(),
        (run / "round-1.diff").read_bytes(),
        [reviewer_finding],
        full=True,
        request=request,
        coverage_paths=["widget.py"],
    )
    attempt = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((run / "review" / attempt["output_path"]).read_bytes())
    ledger_path = run / "loop-ledger.json"
    ledger = _read_ledger(ledger_path)
    ledger["rounds"][0]["findings"][0]["tier"] = "high"
    ledger["rounds"][0]["findings"][0]["structural"] = False
    _write_ledger(ledger_path, ledger)
    evidence_path = run / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["request"] = request
    evidence["origin"] = {
        "kind": "continuation",
        "caller_run": prior["run"].resolve().as_posix(),
        "prior_ledger_sha256": _ledger_digest(prior["run"] / "loop-ledger.json"),
    }
    evidence["rounds"][0]["triage"] = [
        {
            "reported_signature": "missing-guard",
            "signature": "missing-guard",
            "tier": "high",
            "reason": "Parent restores prior severity." if raw_tier != "high" else "",
            "evidence": ["Parent asserts the original severity."] if raw_tier != "high" else [],
        }
    ]
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    return current


def test_continuation_rejects_parent_restoring_prior_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent cannot turn a different reviewer tier into the prior finding's verdict."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    current = _continuation(tmp_path, raw_tier="low", raw_structural=False)

    with pytest.raises(ValueError, match="loop-evidence-prior"):
        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_continuation_rejects_parent_restoring_structural_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent cannot replace the reviewer's structural flag in the final ledger."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    current = _continuation(tmp_path, raw_tier="high", raw_structural=True)

    with pytest.raises(ValueError, match="loop-evidence-report-findings-ledger-mismatch"):
        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_continuation_accepts_original_raw_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewer may close a prior finding while preserving its original attributes."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    current = _continuation(tmp_path, raw_tier="high", raw_structural=False)

    current["validator"].validate_loop_evidence(current["run"], current["codex_home"])
