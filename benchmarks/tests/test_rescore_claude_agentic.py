"""Regression coverage for offline Claude answer-envelope rescoring."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from benchmarks._bench_common import agentic_contracts

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent


def _load_rescorer() -> Any:
    """Load and register the offline rescorer without reading or rewriting result artifacts.

    >>> module = _load_rescorer()
    >>> module is sys.modules["rescore_claude_agentic_test"]
    True
    """
    module_name = "rescore_claude_agentic_test"
    spec = importlib.util.spec_from_file_location(module_name, BENCHMARKS_DIR / "rescore-claude-agentic.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_rescore_row_keeps_unique_bare_json_diagnostic_only() -> None:
    """Offline rescore must match runtime's recoverable-but-unpoolable response state."""
    rescorer = _load_rescorer()
    task = SimpleNamespace(answer_task={"answer_contract": {"fields": ["production_importers"], "params": {}}})
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-rescore",
        fields=("production_importers",),
        expected={"production_importers": ("pkg.caller",)},
    )
    row = {
        "task_id": "T-rescore",
        "model": "fixture-model",
        "arm": "A_plain",
        "output_text": '{"production_importers": ["pkg.caller"]}',
        "tools": {},
        "quality": {"erec": 0.0, "rrec": 0.0},
        "answer_quality_score": None,
        "answer_correct": False,
        "answer_error": "answer requires exactly one BEGIN_ANSWER_JSON and END_ANSWER_JSON envelope",
    }
    runner = SimpleNamespace(ToolCounts=lambda **_kwargs: SimpleNamespace(total=0))

    record = rescorer._rescore_row(row, task, oracle, runner)

    assert record is not None
    assert "after" in record
    assert row["answer_contract_valid"] is False
    assert row["answer_diagnostic_only"] is True
    assert row["answer_pooling_eligible"] is False
    assert row["answer_error"].startswith("answer requires exactly one BEGIN_ANSWER_JSON")
    assert row["answer_quality_score"] == 1.0


def test_rescore_row_never_recovers_strict_eligibility_from_an_approximate_report() -> None:
    """A valid earlier envelope cannot attest an invalid final response offline."""
    rescorer = _load_rescorer()
    task = SimpleNamespace(answer_task={"answer_contract": {"fields": ["production_importers"], "params": {}}})
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-rescore",
        fields=("production_importers",),
        expected={"production_importers": ("pkg.caller",)},
    )
    row = {
        "task_id": "T-rescore",
        "model": "fixture-model",
        "arm": "A_plain",
        "output_text": (
            'BEGIN_ANSWER_JSON\n{"production_importers": ["pkg.caller"]}\nEND_ANSWER_JSON\n'
            "Final response omitted the required envelope."
        ),
        "tools": {},
        "quality": {"erec": 0.0, "rrec": 0.0},
        "answer_quality_score": None,
        "answer_correct": False,
        "answer_error": "answer requires exactly one BEGIN_ANSWER_JSON and END_ANSWER_JSON envelope",
    }
    runner = SimpleNamespace(ToolCounts=lambda **_kwargs: SimpleNamespace(total=0))

    record = rescorer._rescore_row(row, task, oracle, runner)

    assert record is not None
    assert "after" in record
    assert row["answer_contract_valid"] is False
    assert row["answer_diagnostic_only"] is True
    assert row["answer_pooling_eligible"] is False
    assert "approximate report boundary" in row["answer_error"]
