"""Regression coverage for offline Claude answer-envelope rescoring."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks._bench_common import agentic_contracts

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
ENVELOPE_ERROR = "answer requires exactly one BEGIN_ANSWER_JSON and END_ANSWER_JSON envelope"


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


def _row(output_text: str) -> dict[str, Any]:
    """Build a failed-envelope result row carrying the given final response text."""
    return {
        "task_id": "T-rescore",
        "model": "fixture-model",
        "arm": "A_plain",
        "output_text": output_text,
        "tools": {},
        "quality": {"erec": 0.0, "rrec": 0.0},
        "answer_quality_score": None,
        "answer_correct": False,
        "answer_error": ENVELOPE_ERROR,
    }


@pytest.fixture
def rescore_row() -> Any:
    """Return a callable that rescores a row for the single-field importer task and yields ``(record, row)``."""
    rescorer = _load_rescorer()
    task = SimpleNamespace(answer_task={"answer_contract": {"fields": ["production_importers"], "params": {}}})
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-rescore", fields=("production_importers",), expected={"production_importers": ("pkg.caller",)}
    )
    runner = SimpleNamespace(ToolCounts=lambda **_kwargs: SimpleNamespace(total=0))

    def rescore(row: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """Rescore ``row`` in place and return the rescorer's record beside the mutated row."""
        return rescorer._rescore_row(row, task, oracle, runner), row

    return rescore


class TestRescoreRow:
    """Offline rescoring of a result row whose final response failed the strict answer envelope."""

    def test_keeps_unique_bare_json_diagnostic_only(self, rescore_row: Any) -> None:
        """Offline rescore matches runtime's recoverable-but-unpoolable state for a bare JSON answer.

        The response is one correct JSON object with no BEGIN/END envelope. Runtime scores it for diagnosis but refuses
        to pool it, so an offline pass must reach the same state rather than silently promoting it to a valid answer.
        """
        record, row = rescore_row(_row('{"production_importers": ["pkg.caller"]}'))

        assert record is not None
        assert "after" in record
        assert row["answer_contract_valid"] is False
        assert row["answer_diagnostic_only"] is True
        assert row["answer_pooling_eligible"] is False
        assert row["answer_error"].startswith("answer requires exactly one BEGIN_ANSWER_JSON")
        assert row["answer_quality_score"] == 1.0

    def test_never_recovers_strict_eligibility_from_an_approximate_report(self, rescore_row: Any) -> None:
        """A valid earlier envelope cannot attest an invalid final response offline.

        The text holds a correct envelope followed by prose that omits the envelope, so the final response itself is not
        strictly valid. Recovering eligibility from the earlier envelope would pool an answer the run never gave.
        """
        output_text = (
            'BEGIN_ANSWER_JSON\n{"production_importers": ["pkg.caller"]}\nEND_ANSWER_JSON\n'
            "Final response omitted the required envelope."
        )

        record, row = rescore_row(_row(output_text))

        assert record is not None
        assert "after" in record
        assert row["answer_contract_valid"] is False
        assert row["answer_diagnostic_only"] is True
        assert row["answer_pooling_eligible"] is False
        assert "approximate report boundary" in row["answer_error"]
