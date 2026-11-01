"""Verify continuation verdicts preserve reviewer-authenticated prior finding identity."""

from __future__ import annotations

import json
from contextlib import nullcontext
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


@pytest.fixture(autouse=True)
def _native_runtime_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the controlled native-review receipts to their parent thread."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")


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
    restores_severity = raw_tier != "high"
    evidence["rounds"][0]["triage"] = [
        {
            "reported_signature": "missing-guard",
            "signature": "missing-guard",
            "tier": "high",
            "reason": "Parent restores prior severity." if restores_severity else "",
            "evidence": ["Parent asserts the original severity."] if restores_severity else [],
        }
    ]
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8", newline="\n")
    return current


@pytest.mark.parametrize(
    ("raw_tier", "raw_structural", "outcome"),
    [
        pytest.param("low", False, pytest.raises(ValueError, match="loop-evidence-prior"), id="parent-restores-tier"),
        pytest.param(
            "high",
            True,
            pytest.raises(ValueError, match="loop-evidence-report-findings-ledger-mismatch"),
            id="parent-restores-structural-flag",
        ),
        pytest.param("high", False, nullcontext(), id="original-raw-finding"),
    ],
)
def test_continuation_verdict(tmp_path: Path, raw_tier: str, raw_structural: bool, outcome: Any) -> None:
    """A continuation accepts only the reviewer's own verdict on a prior finding, never a parent rewrite.

    The reviewer closes a prior high finding with its own tier and structural flag while the parent triage and final
    ledger keep the original values: a different reviewer tier or flag replaced by the parent must be rejected, and a
    reviewer that preserves the original attributes must be accepted.
    """
    current = _continuation(tmp_path, raw_tier=raw_tier, raw_structural=raw_structural)

    with outcome:
        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])
