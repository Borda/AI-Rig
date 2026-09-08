"""No-model checks for the Codex agentic manifest."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = ROOT / "benchmarks"
BUILDER = BENCHMARKS / "build-codex-agentic-manifest.py"
MANIFEST = BENCHMARKS / "manifests" / "codex-agentic.json"
HUMAN_MANIFEST = BENCHMARKS / "manifests" / "codex-agentic.md"
AGENTIC_TASK_IDS = tuple(f"BA-{number:02d}" for number in range(1, 17))
AGENTIC_ARMS = ("A_plain", "B_auto", "C_strict")


def _load(path: Path) -> dict[str, Any]:
    """Read a UTF-8 JSON manifest and reject non-object roots.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "manifest.json"
    ...     _ = path.write_text('{"schema": 1}', encoding="utf-8")
    ...     _load(path)
    {'schema': 1}
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _sha256(path: Path) -> str:
    """Hash exact artifact bytes without newline or text normalization.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "artifact"
    ...     _ = path.write_bytes(b"abc")
    ...     _sha256(path)
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the deterministic builder without model or credential inputs."""
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_manifest_is_current_and_regeneration_is_byte_stable() -> None:
    """Generation followed by check must reproduce identical machine and human bytes."""
    builder = runpy.run_path(str(BUILDER))
    manifest = builder["_build_manifest"]()
    machine_before = builder["_json_bytes"](manifest)
    human_before = builder["_human_bytes"](manifest, hashlib.sha256(machine_before).hexdigest())
    assert machine_before == MANIFEST.read_bytes()
    assert human_before == HUMAN_MANIFEST.read_bytes()
    assert _run_builder("--check").returncode == 0


@pytest.mark.parametrize(
    "study,command_count", [pytest.param("agentic", 2, id="agentic"), pytest.param("integration", 1, id="integration")]
)
def test_single_model_manifest_commands_name_the_locked_model(study: str, command_count: int) -> None:
    """Single-model approval examples must not inherit the launcher's multi-model default."""
    builder = runpy.run_path(str(BENCHMARKS / f"build-codex-{study}-manifest.py"))
    manifest = _load(BENCHMARKS / "manifests" / f"codex-{study}.json")
    human = builder["_human_bytes"](manifest, "reviewed-machine-digest").decode("utf-8")
    commands = [line for line in human.splitlines() if "bash benchmarks/run-all.sh codex --" in line]

    assert len(commands) == command_count
    assert all(f"--models={manifest['model']['name']}" in line for line in commands)


def test_builder_stale_error_names_exact_rebuild_command(tmp_path: Path) -> None:
    """Internal check mode must identify the command that repairs generated drift."""
    builder = runpy.run_path(str(BUILDER))

    with pytest.raises(ValueError, match=r"run: uv run python benchmarks/build-codex-agentic-manifest\.py$"):
        builder["_write_or_check"](tmp_path / "stale.json", b"expected\n", check=True)


def test_manifest_locks_shared_scope_and_identity() -> None:
    """The default scope is the complete shared suite once in each canonical arm."""
    manifest = _load(MANIFEST)
    assert manifest["experiment_id"] == "codex-agentic"
    assert manifest["model"] == {
        "name": "gpt-5.6-luna",
        "additional_strata": ["gpt-5.6-terra", "gpt-5.6-sol"],
        "reasoning_effort": "high",
        "strict_config": True,
    }
    assert tuple(task["id"] for task in manifest["tasks"]) == AGENTIC_TASK_IDS
    scope = manifest["preregistered_scope"]
    assert tuple(scope["task_ids"]) == AGENTIC_TASK_IDS
    assert tuple(scope["arms"]) == AGENTIC_ARMS
    assert scope["repetitions"] == 1
    assert scope["total_cells"] == 48
    assert scope["coordinate_timeout_seconds"] == 600
    assert "complete_run_max_wall_clock_seconds" not in scope
    assert scope["nonpoolable"] is True
    assert scope["pooling_eligibility"] == "ineligible; exploratory evidence only"
    assert manifest["target_source"]["tag"] == "2.6.5"
    assert manifest["target_source"]["commit"] == "be98784a1a03581b7051a355ae1084fd352d7cea"
    assert manifest["frozen_index_contract"]["raw_sha256"] == (
        "3c5840893e9c939baa61a6c5ce95994ff69ffe4a67d225aeb412c73deb61e0c1"
    )


def test_builder_declares_reproducible_balanced_execution_order() -> None:
    """A study must lock its arm rotation instead of silently changing execution order."""
    builder = runpy.run_path(str(BUILDER))
    manifest = builder["_build_manifest"]()
    assert manifest["execution_order"] == {
        "strategy": "cyclic-arm-order-v1",
        "arm_cycle": list(AGENTIC_ARMS),
        "index_basis": "locked-task-ordinal-plus-repetition-minus-one",
    }
    assert "three repetitions" in manifest["preregistered_scope"]["arm_order"]
    assert "summary.json" in manifest["artifact_package"]["required_files"]
    assert "exact installed launcher" in manifest["arms"]["C_strict"]["requirement"]


def test_manifest_locks_the_full_shared_agentic_scope_with_one_default_repeat() -> None:
    """The Codex study uses every shared agentic task once in each canonical arm.

    Prevents an apparently valid Codex manifest from silently retaining the incomplete task subset, using a noncanonical
    arm label, or multiplying the default study beyond the reviewed 16 × 3 × 1 coordinate set.
    """
    manifest = _load(MANIFEST)
    methodology = _load(BENCHMARKS / "manifests" / "provider-parity-methodology.json")
    shared_suite = next(
        suite for suite in methodology["suites"] if suite["path"] == "benchmarks/suites/tasks-agentic.json"
    )
    scope = manifest["preregistered_scope"]

    assert tuple(shared_suite["ordered_task_ids"]) == AGENTIC_TASK_IDS
    assert tuple(scope["task_ids"]) == AGENTIC_TASK_IDS
    assert tuple(scope["arms"]) == AGENTIC_ARMS
    assert scope["repetitions"] == 1
    assert scope["total_cells"] == 48


def test_claude_and_codex_load_identical_shared_agentic_prompts() -> None:
    """Both provider loaders deliver the same ordered task prompts and locked hashes."""
    suite_path = BENCHMARKS / "suites" / "tasks-agentic.json"
    methodology_path = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
    claude = runpy.run_path(str(BENCHMARKS / "run-claude-agentic.py"))
    codex = runpy.run_path(str(BENCHMARKS / "run-codex-agentic.py"))
    claude_tasks = claude["load_tasks_with_provenance"](suite_path, methodology_path)
    codex_tasks = codex["load_agentic_tasks"](suite_path, MANIFEST)
    locked_hashes = {task["id"]: task["prompt_sha256"] for task in _load(MANIFEST)["tasks"]}

    assert [task.id for task in claude_tasks] == [task["id"] for task in codex_tasks]
    for claude_task, codex_task in zip(claude_tasks, codex_tasks, strict=True):
        assert claude_task.prompt == codex_task["prompt"]
        assert hashlib.sha256(claude_task.prompt.encode("utf-8")).hexdigest() == locked_hashes[claude_task.id]


def test_ba12_and_ba16_declare_every_answer_contract_field_for_scoring() -> None:
    """Prompt-required counts and verdicts cannot disappear behind importer recall.

    Prevents a production-only oracle from grading only the importer list while treating BA-12's excluded-test count or
    BA-16's separate test/production counts and risk verdict as unscored prose. A plausibly wrong scorer that preserves
    EREC/RREC alone fails this declaration-level contract.
    """
    suite = _load(BENCHMARKS / "suites" / "tasks-agentic.json")
    tasks = {task["id"]: task for task in suite["tasks"]}

    assert tasks["BA-12"]["answer_contract"]["fields"] == [
        "production_importers",
        "excluded_test_importer_count",
        "ranking",
        "production_importer_count",
    ]
    assert tasks["BA-12"]["answer_contract"]["params"]["ranking"] == {
        "candidate_set": "production_importers",
        "top_k": 10,
    }
    assert tasks["BA-16"]["answer_contract"]["fields"] == [
        "production_importers",
        "high_centrality",
        "test_importer_count",
        "production_importer_count",
        "risk_tier",
    ]
    assert tasks["BA-16"]["answer_contract"]["params"]["high_centrality"] == {
        "min_rdep_count": 10,
    }
    assert tasks["BA-16"]["answer_contract"]["params"]["risk_tier"] == {
        "critical_min_production_importer_count": 10,
        "critical_min_high_centrality_count": 1,
    }


def test_manifest_has_exact_shared_scoring_and_plugin_hashes() -> None:
    """The scorer formulas and all integration/runtime identities are explicit."""
    manifest = _load(MANIFEST)
    assert manifest["scoring"]["implementation"]["semantic_symbol"] == "score_answer"
    assert manifest["scoring"]["implementation"]["response_symbol"] == "assess_answer_response"
    assert manifest["scoring"]["implementation"]["evidence_symbol"] == "score_evidence_metrics"
    assert manifest["scoring"]["metrics"] == {
        "SCORE": "mean semantic component score for each declared answer-contract field",
        "EREC": "expected-importer recall in all agent text, independent of answer-envelope validity",
        "RREC": "expected-importer recall in the final report, independent of answer-envelope validity",
        "DEFF": "unbounded expected-importer exposure hits per command",
    }
    hashes = manifest["artifact_sha256"]
    assert len(hashes) == 12
    assert all(len(value) == 64 for value in hashes.values())
    assert manifest["plugin_runtime"]["source_hashes"] == hashes
    assert (
        manifest["plugin_runtime"]["codemap_version"]
        == _load(ROOT / "plugins/codemap-py/.codex-plugin/plugin.json")["version"]
    )
    assert (
        manifest["plugin_runtime"]["codex_rig_version"]
        == _load(ROOT / "plugins/codex-rig/.codex-plugin/plugin.json")["version"]
    )
    assert manifest["runtime_isolation"]["manifest"] == "benchmarks/manifests/codex-integration.json"
    assert manifest["runtime_isolation"]["mapping"]["B_auto"].endswith("use remains optional")
    assert "run_all" in hashes
    assert manifest["artifact_package"]["required_files"] == [
        "run.log",
        "telemetry.jsonl (raw)",
        "telemetry-canonical.jsonl",
        "run-metadata.json",
        "summary.json",
        "inputs/ (frozen input snapshot)",
        "runtime-isolation.jsonl (0600 expected/observed plugin identity evidence; may be empty)",
        "checksums.sha256",
    ]


def test_arm_admission_semantics_are_explicit() -> None:
    """Optional B use and required C Skill sequencing remain distinguishable."""
    arms = _load(MANIFEST)["arms"]
    assert arms["A_plain"]["codemap_available"] is False
    assert arms["B_auto"]["no_call_valid"] is True
    assert arms["C_strict"]["no_call_valid"] is False
    assert arms["C_strict"]["row_retained_on_noncompliance"] is True


def test_manifest_contains_no_credentials_or_ignored_historical_paths() -> None:
    """The lock cannot leak auth material or point at result/history artifacts."""
    text = MANIFEST.read_text(encoding="utf-8") + HUMAN_MANIFEST.read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in ("password", "secret", "api_key", 'codex_auth_source="/users/'):
        assert forbidden not in lowered
    assert 'CODEX_AUTH_SOURCE="$HOME/.codex/auth.json"' in text
    assert 'CODEX_AUTH_SOURCE="/Users/' not in text
    assert "benchmarks/results" not in MANIFEST.read_text(encoding="utf-8")
    assert "results/manifests" not in text
    assert "historical/" not in text
    assert "__pycache__" not in text
    assert "first-slice" not in lowered


def test_runner_accepts_exact_manifest_authorization() -> None:
    """The runner and generated admission contract must accept only the exact lock hash."""
    runner = runpy.run_path(str(BENCHMARKS / "run-codex-agentic.py"))
    manifest_hash = _sha256(MANIFEST)
    admitted = runner["validate_paid_admission"](MANIFEST, manifest_hash)
    assert admitted["admission"]["paid_execution"] == "admitted"
    with pytest.raises(ValueError, match="exact current manifest"):
        runner["validate_paid_admission"](MANIFEST, "0" * 64)
