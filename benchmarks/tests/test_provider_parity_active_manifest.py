"""Lock the post-pilot provider-parity manifest without running a model."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from benchmarks._bench_common import provider_parity_contracts as core


ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = ROOT / "benchmarks"
METHODOLOGY_MANIFEST = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
CODEX_MANIFEST = BENCHMARKS / "manifests" / "codex-integration.json"
METHODOLOGY_BUILDER = BENCHMARKS / "build-provider-parity-methodology-manifest.py"
FIXTURE_DIR = BENCHMARKS / "tests" / "fixtures"
REPAIRED_SUITE_PATH = "benchmarks/suites/tasks-bench.json"
EXPECTED_SUITE_TASK_COUNTS = {
    "benchmarks/suites/tasks-agentic.json": 20,
    "benchmarks/suites/tasks-bench.json": 60,
    "benchmarks/suites/tasks-code.json": 15,
    "benchmarks/suites/tasks-fix-multi.json": 3,
    "benchmarks/suites/tasks-fix-single.json": 4,
    "benchmarks/suites/tasks-patch.json": 5,
    "benchmarks/suites/tasks-readcrop.json": 6,
}
EXPECTED_STRUCTURAL_EXECUTION_TASK_IDS = [
    "SE-01",
    "SE-02",
    "SE-03",
    "SE-04",
    "SE-05",
    "FN-01",
    "FN-02",
    "FN-03",
    "FN-04",
    "FN-05",
    "RV-01",
    "RV-02",
    "RV-03",
    "RV-04",
    "RV-05",
    "CQ-01",
    "CQ-02",
    "CQ-03",
    "CQ-04",
    "CQ-05",
    "BR-01",
    "BR-02",
    "BR-03",
    "BR-04",
    "BR-05",
    "BR-06",
    "BR-07",
    "BR-08",
    "BR-09",
    "DG-01",
    "DG-02",
    "DG-03",
    "DG-04",
    "DG-05",
    "DG-06",
    "FT-01",
    "FT-02",
    "FT-03",
    "FT-04",
    "FT-05",
    "DI-01",
    "DI-02",
    "DI-03",
    "DI-04",
    "DI-05",
    "DI-06",
    "GR-01",
    "GR-02",
    "GR-03",
    "GR-04",
    "MB-01",
    "MB-02",
    "MB-03",
    "MB-04",
    "MB-05",
]
EXPECTED_STRUCTURAL_DIAGNOSTIC_TASK_IDS = [
    "SE-01",
    "SE-02",
    "SE-03",
    "SE-04",
    "SE-05",
    "RV-05",
    "CQ-02",
    "CQ-03",
    "CQ-04",
    "CQ-05",
]
SHORTHAND_REVISION = re.compile(
    r"(?<![A-Za-z0-9_-])r[0-9]+(?![A-Za-z0-9_-])|"
    r"(?<![A-Za-z0-9_-])r[0-9]+_(?=manifest|revision|lock|policy|profile|runtime|execution)"
)


def _sha256(path: Path) -> str:
    """Hash exact locked artifact bytes without decoding or normalizing their contents.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "artifact"
    ...     _ = path.write_bytes(b"abc")
    ...     _sha256(path)
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    """Read a UTF-8 manifest and assert that its root is a JSON object.

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


def _run_methodology_builder(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the provider-neutral relock utility without model or auth access."""
    return subprocess.run(
        [sys.executable, str(METHODOLOGY_BUILDER), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_codex_manifest_uses_generic_task_selection_contract() -> None:
    """Codex selection is task/family based with stage-native execution metadata."""
    manifest = _load(CODEX_MANIFEST)
    selection = manifest["task_selection"]

    assert selection["selector_option"] == "--tasks"
    assert selection["separator"] == ","
    assert selection["stage_order"] == ["structural", "readcrop", "fix-single", "fix-multi", "patch"]
    assert selection["default_total_cells"] == 219
    assert len(selection["allowed_task_ids"]) == 73
    assert "study_mode" not in selection
    assert "repetitions" not in selection
    assert "arms" not in selection
    assert selection["nonpoolable"] is True
    assert selection["resolution_policy"]["exact_id_first"] is True
    assert selection["resolution_policy"]["deduplicate"].startswith("selector tokens")
    assert selection["aggregate_execution_scope"]["approval_option"] == "--paid-approval"
    assert selection["aggregate_execution_scope"]["dry_run_marker"] == "SCOPE"
    assert "post_fix_diagnostic" not in manifest


def test_methodology_builder_is_deterministic_and_check_mode_rejects_stale_output(tmp_path: Path) -> None:
    """The explicit relock path must reject stale output instead of repairing it implicitly."""
    builder = runpy.run_path(str(METHODOLOGY_BUILDER))
    output = tmp_path / "provider-parity-methodology.json"
    builder["_build_manifest"].__globals__["OUTPUT_MANIFEST"] = output
    expected = builder["_manifest_bytes"](builder["_build_manifest"]())

    try:
        builder["_write_or_check"](output, expected, check=True)
    except ValueError as error:
        assert "stale" in str(error)
    else:
        raise AssertionError("check mode accepted a missing methodology output")

    output.write_bytes(b"{}\n")
    try:
        builder["_write_or_check"](output, expected, check=True)
    except ValueError as error:
        assert "stale" in str(error)
    else:
        raise AssertionError("check mode accepted stale methodology output")

    builder["_write_or_check"](output, expected, check=False)
    builder["_write_or_check"](output, expected, check=True)
    assert expected == builder["_manifest_bytes"](builder["_build_manifest"]())


def test_agentic_execution_contract_records_provider_specific_default_cells() -> None:
    """The shared lock must not erase Claude's three-model multiplicity."""
    contract = _load(METHODOLOGY_MANIFEST)["agentic_execution_contract"]

    assert contract["default_repetitions"] == 1
    assert contract["default_total_cells_by_provider"] == {"claude": 180, "codex": 60}
    assert contract["models_by_provider"] == {
        "claude": ["haiku", "sonnet", "opus"],
        "codex": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
    }


@pytest.mark.parametrize("manifest_name", ["codex-integration.json", "codex-agentic.json"])
def test_every_codex_manifest_admits_the_same_declared_strata(manifest_name: str) -> None:
    """Each Codex runner admits exactly the strata the methodology declares, in the same set.

    Scenario: --models resolves a name against the methodology manifest, while each runner admits a
    name against its own manifest's model block. A stratum missing from one of those blocks is a
    name the launcher accepts and the runner then refuses — the agentic manifest omitted gpt-5.6-sol
    while the structural one declared it, so the two lanes disagreed about what could be selected.
    """
    declared = _load(METHODOLOGY_MANIFEST)["agentic_execution_contract"]["models_by_provider"]["codex"]
    model = _load(BENCHMARKS / "manifests" / manifest_name)["model"]

    assert [model["name"], *model["additional_strata"]] == declared


def test_methodology_builder_rejects_tampered_passthrough_policy_seed(tmp_path: Path) -> None:
    """A generated methodology file can never bootstrap changed policy into acceptance."""
    builder = runpy.run_path(str(METHODOLOGY_BUILDER))
    seed = _load(BENCHMARKS / "policy" / "provider-parity-methodology.json")
    seed["execution_controls"]["budget"] = "tampered"
    seed_path = tmp_path / "provider-parity-methodology.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    builder["_load_policy_seed"].__globals__["POLICY_SEED"] = seed_path

    with pytest.raises(SystemExit) as excinfo:
        builder["main"](check=True)

    assert excinfo.value.code == 1


@pytest.mark.parametrize(
    ("suite_path", "task", "expected"),
    [
        pytest.param(
            "benchmarks/suites/tasks-agentic.json",
            {"id": "EDGE-AGENTIC", "prompt": "agentic", "type": "blast_radius_analysis"},
            ("independent", True, False),
            id="agentic-independent",
        ),
        pytest.param(
            "benchmarks/suites/tasks-bench.json",
            {"id": "FN-01", "prompt": "structural", "type": "fn_call_graph"},
            ("independent", True, True),
            id="structural-independent-headline",
        ),
        pytest.param(
            "benchmarks/suites/tasks-bench.json",
            {"id": "SE-01", "prompt": "reference", "type": "symbol_extraction"},
            ("static_reference", True, False),
            id="structural-static-reference",
        ),
        pytest.param(
            "benchmarks/suites/tasks-bench.json",
            {"id": "CQ-03", "prompt": "repeat", "type": "code_quality", "self_consistency": True},
            ("self_consistency", True, False),
            id="structural-self-consistency",
        ),
        pytest.param(
            "benchmarks/suites/tasks-bench.json",
            {"id": "RI-05", "prompt": "unscoreable", "type": "real_issue", "scoreable": False},
            ("unscoreable", False, False),
            id="structural-unscoreable",
        ),
        pytest.param(
            "benchmarks/suites/tasks-code.json",
            {"id": "EDGE-CODE", "prompt": "cli", "skill": "fix"},
            ("unscoreable", False, False),
            id="cli-unscoreable",
        ),
        pytest.param(
            "benchmarks/suites/tasks-fix-multi.json",
            {"id": "EDGE-FIX", "prompt": "fix", "type": "fix_multicaller"},
            ("static_reference", True, False),
            id="optional-static-reference",
        ),
    ],
)
def test_methodology_builder_derives_policy_for_each_task_case(
    suite_path: str, task: dict[str, Any], expected: tuple[str, bool, bool]
) -> None:
    """Current policy classification is covered by small independent edge cases."""
    builder = runpy.run_path(str(METHODOLOGY_BUILDER))
    headline_ids = set(builder["_load_policy_seed"]()["headline_structural_v1"]["task_ids"])

    row = builder["_task_row"](task, suite_path, headline_ids, set(), "diagnostic")

    assert (row["oracle_class"], row["effective_scoreable"], row["headline_eligible_v1"]) == expected


def test_methodology_builder_output_is_current() -> None:
    """The committed provider-neutral source must equal the explicit deterministic relock output."""
    result = _run_methodology_builder("--check")

    assert result.returncode == 0, result.stderr


def test_methodology_builder_locks_exact_provider_neutral_structural_selection() -> None:
    """Lock the ordered non-RI structural suite and its headline complement."""
    manifest = _load(METHODOLOGY_MANIFEST)
    cells = manifest["preregistered_cells"]
    codex_cells = _load(CODEX_MANIFEST)["preregistered_cells"]

    assert cells["structural_execution_task_ids"] == EXPECTED_STRUCTURAL_EXECUTION_TASK_IDS
    assert cells["structural_diagnostic_task_ids"] == EXPECTED_STRUCTURAL_DIAGNOSTIC_TASK_IDS
    assert cells["structural_execution_task_ids"] == codex_cells["structural_execution_task_ids"]
    assert cells["structural_diagnostic_task_ids"] == codex_cells["structural_diagnostic_task_ids"]
    assert len(cells["structural_execution_task_ids"]) == 55
    assert len(cells["structural_diagnostic_task_ids"]) == 10
    assert not any(task_id.startswith("RI-") for task_id in cells["structural_execution_task_ids"])
    assert cells["structural_execution_task_ids"] == [
        task["id"]
        for task in _suites_by_path(manifest)[REPAIRED_SUITE_PATH]["tasks"]
        if task["effective_type"] != "real_issue"
    ]
    assert cells["structural_diagnostic_task_ids"] == [
        task_id
        for task_id in cells["structural_execution_task_ids"]
        if task_id not in set(cells["structural_confirmatory_task_ids"])
    ]


def test_methodology_manifest_locks_the_complete_current_suite_universe() -> None:
    """The current lock cannot silently omit a committed suite or task group."""
    methodology = _load(METHODOLOGY_MANIFEST)
    observed = {suite["path"]: len(suite["tasks"]) for suite in methodology["suites"]}

    assert observed == EXPECTED_SUITE_TASK_COUNTS
    assert methodology["suite_integrity"]["suite_count"] == len(EXPECTED_SUITE_TASK_COUNTS)
    assert methodology["suite_integrity"]["task_count"] == sum(EXPECTED_SUITE_TASK_COUNTS.values())


def test_methodology_builder_stale_error_names_exact_rebuild_command(tmp_path: Path) -> None:
    """Internal check mode must never tell launcher users to remove a flag they did not pass."""
    builder = runpy.run_path(str(METHODOLOGY_BUILDER))

    try:
        builder["_write_or_check"](tmp_path / "stale.json", b"expected\n", check=True)
    except ValueError as exc:
        assert str(exc).endswith("run: uv run python benchmarks/build-provider-parity-methodology-manifest.py")
    else:
        raise AssertionError("stale methodology output was accepted")


def _suites_by_path(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index suites by source path, asserting that duplicate paths cannot silently discard rows.

    >>> _suites_by_path({"suites": [{"path": "example.json", "tasks": []}]})
    {'example.json': {'path': 'example.json', 'tasks': []}}
    >>> with pytest.raises(AssertionError):
    ...     _suites_by_path({"suites": [{"path": "same"}, {"path": "same"}]})
    """
    suites = {suite["path"]: suite for suite in manifest["suites"]}
    assert len(suites) == len(manifest["suites"])
    return suites


def _assert_manifest_matches_materialized_suite_inputs(manifest: dict[str, Any]) -> None:
    """Assert that task order, task hashes, and suite identities match the current source artifacts.

    >>> _assert_manifest_matches_materialized_suite_inputs(_load(METHODOLOGY_MANIFEST))
    """
    semantic_hashes: dict[str, str] = {}
    for suite in manifest["suites"]:
        assert suite["raw_sha256"] == _sha256(ROOT / suite["path"])
        source_tasks = core.load_task_suite(ROOT / suite["path"])
        semantic_hashes[suite["path"]] = core.semantic_suite_hash(source_tasks)
        assert suite["semantic_suite_sha256"] == semantic_hashes[suite["path"]]
        assert [task["id"] for task in suite["tasks"]] == [task["id"] for task in source_tasks]
        for manifest_task, source_task in zip(suite["tasks"], source_tasks, strict=True):
            assert manifest_task["canonical_task_sha256"] == core.canonical_task_hash(source_task)
            assert manifest_task["prompt_sha256"] == core.prompt_hash(source_task)
    assert manifest["suite_integrity"]["semantic_suite_sha256"] == semantic_hashes


def test_methodology_manifest_uses_policy_seed_and_current_suite_inputs() -> None:
    """The shared committed source binds current suites without prior-run history."""
    policy = _load(BENCHMARKS / "policy" / "provider-parity-methodology.json")
    methodology = _load(METHODOLOGY_MANIFEST)

    assert "methodology_change_ledger" not in methodology
    _assert_manifest_matches_materialized_suite_inputs(methodology)
    for field in ("target_source", "headline_structural_v1", "validation"):
        assert methodology[field] == policy[field]
    active_index = methodology["index"]
    assert active_index == _builder_index_lock()
    for field in (
        "confirmatory_repetitions",
        "pilot_repetitions",
        "providers",
        "smoke_task_ids",
        "structural_confirmatory_task_ids",
        "structural_pilot_task_ids",
    ):
        assert methodology["preregistered_cells"][field] == policy["preregistered_cells"][field]


@pytest.mark.parametrize("task_id", ("RV-01", "RV-02", "RV-03", "RV-04", "RV-05"))
def test_methodology_manifest_binds_every_review_subquestion_in_provider_prompt(task_id: str) -> None:
    """Review follow-ups must be hashed as delivered provider input, not evaluator-only metadata."""
    methodology = _load(METHODOLOGY_MANIFEST)
    suite = _suites_by_path(methodology)[REPAIRED_SUITE_PATH]
    manifest_tasks = {task["id"]: task for task in suite["tasks"]}
    source_tasks = {
        task["id"]: task for task in core.load_task_suite(ROOT / REPAIRED_SUITE_PATH) if task["id"].startswith("RV-")
    }

    assert source_tasks.keys() == {"RV-01", "RV-02", "RV-03", "RV-04", "RV-05"}
    task = source_tasks[task_id]
    delivered = core.materialize_task_prompt(task)
    assert delivered != task["prompt"]
    for sub_question in task["sub_questions"]:
        assert sub_question["prompt"] in delivered
    assert manifest_tasks[task_id]["prompt_sha256"] == core.prompt_hash(task)


@pytest.mark.parametrize(
    ("task_id", "target"),
    [
        pytest.param(
            "RV-03", "lightning.pytorch.trainer.call::_call_lightning_module_hook", id="lightning-module-hook"
        ),
        pytest.param("RV-04", "lightning.pytorch.trainer.call::_call_callback_hooks", id="callback-hooks"),
    ],
)
def test_review_caller_tasks_lock_production_fn_rdeps_queries(task_id: str, target: str) -> None:
    """RV-03/RV-04 must exclude tests, exactly as their prompts and counts require."""
    tasks = {task["id"]: task for task in core.load_task_suite(ROOT / REPAIRED_SUITE_PATH)}
    assert task_id in tasks
    task = tasks[task_id]
    assert task["expected_queries"] == [{"cmd": "fn-rdeps", "args": [target, "--exclude-tests"]}]
    assert "production functions (excluding test files)" in core.materialize_task_prompt(task)


def test_methodology_manifest_locks_luna_high_and_exact_implementation_identities() -> None:
    """The shared methodology must bind the only allowed model and code bytes."""
    manifest = _load(METHODOLOGY_MANIFEST)
    implementation = manifest["implementation_contract"]

    assert implementation["codex_model_stratum"] == {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "strict_config": True,
    }
    assert implementation["artifact_sha256"] == {
        "agentic_contracts": _sha256(BENCHMARKS / "_bench_common" / "agentic_contracts.py"),
        "agentic_reporting": _sha256(BENCHMARKS / "_bench_common" / "agentic_reporting.py"),
        "claude_query_skill": _sha256(ROOT / "plugins/codemap-py/claude-skills/query-code/SKILL.md"),
        "codemap_graph": _sha256(ROOT / "plugins/codemap-py/src/codemap_py/graph.py"),
        "codemap_query": _sha256(ROOT / "plugins/codemap-py/src/codemap_py/query.py"),
        "codex_query_skill": _sha256(ROOT / "plugins/codemap-py/codex-skills/query-code/SKILL.md"),
        "edit_patch_contracts": _sha256(BENCHMARKS / "_bench_common" / "edit_patch_contracts.py"),
        "mutation_isolation": _sha256(BENCHMARKS / "_bench_common" / "mutation_isolation.py"),
        "paid_lifecycle": _sha256(BENCHMARKS / "_bench_common" / "paid_lifecycle.py"),
        "presentation": _sha256(BENCHMARKS / "_bench_common" / "presentation.py"),
        "patch_index_locks": _sha256(BENCHMARKS / "suites" / "patch-index-locks.json"),
        "provider_parity_contracts": _sha256(BENCHMARKS / "_bench_common" / "provider_parity_contracts.py"),
        "run_all": _sha256(BENCHMARKS / "run-all.sh"),
        "run_claude_agentic": _sha256(BENCHMARKS / "run-claude-agentic.py"),
        "run_claude_structural": _sha256(BENCHMARKS / "run-claude-structural.py"),
        "run_codex_agentic": _sha256(BENCHMARKS / "run-codex-agentic.py"),
        "run_codex_structural": _sha256(BENCHMARKS / "run-codex-structural.py"),
    }
    assert "CODEMAP_BIN" in manifest["codex_permission_profiles"]["shell_environment"]["set_allowlist"]


def test_methodology_manifest_locks_shared_continuous_fitness_and_observed_capability_strata() -> None:
    """Fitness and capability labels must be shared, explicit, and suite-derived."""
    manifest = _load(METHODOLOGY_MANIFEST)
    evaluation = manifest["evaluation_contract"]
    tasks = core.load_task_suite(BENCHMARKS / "suites" / "tasks-bench.json")
    observed = Counter(stratum for task in tasks for stratum in core.capability_strata(task))

    assert evaluation["provider_neutral"] is True
    assert evaluation["quality_primary"] == "continuous task-family quality_score in [0,1]"
    assert evaluation["binary_guardrail"] == "correct"
    assert evaluation["components"] == ["recall", "caller_recall", "test_recall"]
    assert evaluation["precision_f1_status"] == "not reported without a frozen false-positive oracle"
    assert evaluation["capability_strata_counts"] == dict(sorted(observed.items()))
    assert evaluation["prospective_product_acceptance"] == {
        "efficiency_path": {
            "c_a_gross_input_ratio_95_upper": "< 1.00",
            "c_a_paired_quality_mean": ">= 0.00",
            "c_a_paired_quality_95_lower": ">= -0.02",
        },
        "quality_path": {
            "c_a_gross_input_ratio_95_upper": "<= 1.05",
            "c_a_paired_quality_95_lower": "> 0",
        },
        "task_family_block": "Any repeated task-family mean C_strict-A_plain quality difference < -0.10 blocks acceptance.",
        "historical_evidence": "Historical nonpoolable evidence cannot satisfy this prospective policy.",
    }
    assert manifest["preregistered_cells"]["arm_order"] == (
        "sort arms by "
        "sha256(experiment_revision|provider|model|reasoning_effort|task_id|repetition|arm), "
        "ascending raw digest; Claude uses an empty effort coordinate"
    )


def _builder_index_lock() -> dict[str, Any]:
    """Import the methodology generator's explicit index lock without running its CLI.

    >>> lock = _builder_index_lock()
    >>> isinstance(lock["scan_version"], int), len(lock["raw_sha256"])
    (True, 64)
    """
    return runpy.run_path(str(METHODOLOGY_BUILDER))["INDEX_LOCK"]
