"""No-model conformance checks shared by the Claude parity adapters.

These checks cover the adapter boundary B1 deliberately leaves open: immutable identity, condition semantics, and the
result fields required to pool neither legacy nor mismatched cells.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest


BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
AGENTIC_SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-agentic.json"
PARITY_MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "provider-parity-methodology.json"

_REQUIRED_SHARED_RESULT_FIELDS = {
    "experiment_revision",
    "task_hash",
    "prompt_hash",
    "suite_hash",
    "evaluator_id",
    "evaluator_hash",
    "envelope_hash",
    "arm_contract_hash",
    "repo_sha",
    "index_sha",
    "oracle_class",
    "headline_eligible_v1",
    "scoreable",
}


@pytest.mark.parametrize(
    ("adapter", "result_type"),
    [pytest.param("structural", "BenchRun", id="structural"), pytest.param("agentic", "BenchmarkRun", id="agentic")],
)
def test_adapter_result_uses_the_shared_provenance_schema(
    adapter: str, result_type: str, script_run_agentic: Any, script_run_bench: Any
) -> None:
    """Every current-revision result must carry all pairing and audit coordinates.

    Prevents a result that is individually scoreable but cannot be checked for its locked suite, evaluator, envelope,
    arm contract, repository, or index. The existing runner-specific tests can pass while this metadata is absent.
    """
    module = script_run_bench if adapter == "structural" else script_run_agentic
    result_fields = {field.name for field in fields(getattr(module, result_type))}

    assert _REQUIRED_SHARED_RESULT_FIELDS <= result_fields


def test_agentic_loader_rejects_a_known_id_with_tampered_task_bytes(tmp_path: Path, script_run_agentic: Any) -> None:
    """A manifest task ID cannot authorize changed task or prompt bytes.

    Prevents current-revision results from inheriting the canonical provider-parity policy after their prompt changed.
    Unknown IDs are already rejected; this covers the distinct known-ID and mismatched-hash path.
    """
    raw_suite = json.loads(AGENTIC_SUITE_PATH.read_text(encoding="utf-8"))
    tasks = raw_suite["tasks"] if isinstance(raw_suite, dict) else raw_suite
    task = next(item for item in tasks if item["id"] == "BA-01").copy()
    task["prompt"] = f"{task['prompt']}\nTampered after the canonical provider-parity lock."
    suite_path = tmp_path / "tampered-agentic-suite.json"
    suite_path.write_text(json.dumps([task]), encoding="utf-8")

    with pytest.raises(ValueError, match="(task|prompt).*hash|hash.*(task|prompt)"):
        script_run_agentic.load_tasks_with_provenance(suite_path, PARITY_MANIFEST_PATH)


def test_structural_c_strict_has_executable_required_use_support(script_run_bench: Any) -> None:
    """C must install Codemap, expose its command, and require use in its envelope.

    Prevents a C label that only falls through to the B prompt or relies on default CLI tool availability instead of a
    reproducible structural setup.
    """
    prompt = script_run_bench._build_system_prompt("C_strict", "repo", "/repo", "/index.json")

    assert "must use Codemap at least once" in prompt
    assert script_run_bench._ARM_ALLOWED["C_strict"] == ["--allowedTools", "Bash(scan-query:*)"]


def test_structural_legacy_arm_is_not_relabelled_as_a_parity_condition(script_run_bench: Any, tmp_path: Path) -> None:
    """A legacy ``plain`` record remains unassigned to A/B/C until rerun explicitly.

    Prevents historic results from being silently represented as the new arm contract merely because their old transport
    label looked similar.
    """
    tasks, policies = script_run_bench._load_primary_parity_contract()
    task = next(item for item in tasks if item["id"] == "FN-02")
    runner = script_run_bench.BenchRunner(
        "haiku", "fixture-model", tmp_path, tmp_path / "index.json", task_policies=policies
    )
    result = script_run_bench.BenchRun(
        arm="plain", task_id=task["id"], task_type=task["type"], model="haiku", success=True
    )

    runner._stamp_provenance(result, task, script_run_bench._task_hash(task))

    assert result.parity_arm in (None, "")
