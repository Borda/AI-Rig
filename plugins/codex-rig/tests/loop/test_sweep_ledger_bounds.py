"""Reject oversized workflow ledgers before parsing or allocating their full contents."""

from pathlib import Path

import pytest
from _modules import load_module_registered

SHARED = Path(__file__).resolve().parents[2] / "shared"


def test_runtime_ledger_size_has_coded_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime rows reject a valid but oversized JSONL file."""
    module = load_module_registered("sweep_parallel_execution", SHARED / "parallel_execution.py")
    monkeypatch.setattr(module, "MAX_LEDGER_BYTES", 64, raising=False)
    ledger = tmp_path / "runtime.jsonl"
    ledger.write_text('{"type":"event","payload":{"value":"' + "x" * 65 + '"}}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="runtime-child-rollout-oversized"):
        module._runtime_rows(ledger, "child-rollout")


def test_resolution_ledger_size_has_coded_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prior review cannot feed an unbounded resolution ledger to the current run."""
    module = load_module_registered("sweep_find_review_report", SHARED / "find-review-report.py")
    monkeypatch.setattr(module, "MAX_LEDGER_BYTES", 64, raising=False)
    prior = tmp_path / "pr-7" / "run-001"
    prior.mkdir(parents=True)
    (prior / "result.json").write_text("{}", encoding="utf-8")
    (prior / module.RESOLUTION_LEDGER).write_text(" " * 65, encoding="utf-8")
    with pytest.raises(ValueError, match="prior-resolutions-ledger-oversized"):
        module.prior_resolutions(prior.with_name("run-002"))


def test_event_ledger_size_has_coded_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A remediation ledger is bounded before any individual events are validated."""
    module = load_module_registered("sweep_remediation_finalize", SHARED / "remediation_finalize.py")
    monkeypatch.setattr(module, "MAX_LEDGER_BYTES", 64, raising=False)
    (tmp_path / module.EVENTS_LEDGER).write_text(" " * 65, encoding="utf-8")
    with pytest.raises(module.DeriveError, match="events-ledger-oversized"):
        module.read_events(tmp_path)
