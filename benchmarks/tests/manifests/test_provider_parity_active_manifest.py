"""Lock the post-pilot provider-parity manifest without running a model.

The committed methodology manifest, its policy seed, and the generator that rebuilds one from the other are checked
against the tracked suites, the Codex manifests, and the shared contract library. No provider CLI or model is run.
"""

from __future__ import annotations

import json
import re
import runpy
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from _manifest_helpers import load_manifest, sha256_file

from benchmarks._bench_common import provider_parity_contracts as core
from benchmarks._bench_common.artifact_hashing import module_sha256, runner_sha256

ROOT = Path(__file__).resolve().parents[3]
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


def _run_methodology_builder(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the provider-neutral relock utility without model or auth access."""
    return subprocess.run(
        [sys.executable, str(METHODOLOGY_BUILDER), *args], cwd=ROOT, check=False, capture_output=True, text=True
    )


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

    >>> _assert_manifest_matches_materialized_suite_inputs(load_manifest(METHODOLOGY_MANIFEST))
    """
    semantic_hashes: dict[str, str] = {}
    for suite in manifest["suites"]:
        assert suite["raw_sha256"] == sha256_file(ROOT / suite["path"])
        source_tasks = core.load_task_suite(ROOT / suite["path"])
        semantic_hashes[suite["path"]] = core.semantic_suite_hash(source_tasks)
        assert suite["semantic_suite_sha256"] == semantic_hashes[suite["path"]]
        assert [task["id"] for task in suite["tasks"]] == [task["id"] for task in source_tasks]
        for manifest_task, source_task in zip(suite["tasks"], source_tasks, strict=True):
            assert manifest_task["canonical_task_sha256"] == core.canonical_task_hash(source_task)
            assert manifest_task["prompt_sha256"] == core.prompt_hash(source_task)
    assert manifest["suite_integrity"]["semantic_suite_sha256"] == semantic_hashes


@pytest.fixture(name="builder")
def _builder() -> dict[str, Any]:
    """Load the methodology generator's namespace without running its command line (fresh per test).

    A new namespace per test keeps the path overrides that tests install on it from leaking into other tests.
    """
    return runpy.run_path(str(METHODOLOGY_BUILDER))


@pytest.fixture(name="expected_bytes")
def _expected_bytes(builder: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> bytes:
    """Build the manifest bytes the generator would write, with its output path redirected into ``tmp_path``."""
    monkeypatch.setitem(builder["_build_manifest"].__globals__, "OUTPUT_MANIFEST", tmp_path / "methodology.json")
    return builder["_manifest_bytes"](builder["_build_manifest"]())


class TestCodexManifestSelection:
    """The Codex manifests agree with the methodology on task selection and model strata."""

    def test_uses_the_generic_task_selection_contract(self) -> None:
        """Codex selection is task/family based with stage-native execution metadata.

        The selector option, separator, stage order, and totals are the launcher's public contract, and the retired
        study-mode, repetition, and arm fields must stay gone from the selection block.
        """
        manifest = load_manifest(CODEX_MANIFEST)
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

    @pytest.mark.parametrize("manifest_name", ["codex-integration.json", "codex-agentic.json"])
    def test_every_manifest_admits_the_same_declared_strata(self, manifest_name: str) -> None:
        """Each Codex runner admits exactly the strata the methodology declares, in the same set.

        ``--models`` resolves a name against the methodology manifest, while each runner admits a name against its own
        manifest's model block. A stratum missing from one of those blocks is a name the launcher accepts and the
        runner then refuses: the agentic manifest once omitted gpt-6-sol while the structural one declared it.
        """
        declared = load_manifest(METHODOLOGY_MANIFEST)["agentic_execution_contract"]["models_by_provider"]["codex"]
        model = load_manifest(BENCHMARKS / "manifests" / manifest_name)["model"]

        assert [model["name"], *model["additional_strata"]] == declared

    def test_agentic_contract_records_provider_specific_default_cells(self) -> None:
        """The shared lock keeps Claude's three-model multiplicity instead of erasing it.

        Claude runs three models and Codex four strata, so the default cell totals differ by provider.
        """
        contract = load_manifest(METHODOLOGY_MANIFEST)["agentic_execution_contract"]

        assert contract["default_repetitions"] == 1
        assert contract["default_total_cells_by_provider"] == {"claude": 180, "codex": 60}
        assert contract["models_by_provider"] == {
            "claude": ["haiku", "sonnet", "opus"],
            "codex": ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"],
        }


class TestMethodologyBuilder:
    """The relock utility is deterministic, rejects stale output explicitly, and protects the policy seed."""

    def test_check_mode_rejects_missing_output(
        self, builder: dict[str, Any], expected_bytes: bytes, tmp_path: Path
    ) -> None:
        """Check mode refuses to repair a methodology file that does not exist.

        The explicit relock path must report stale output instead of silently creating it.
        """
        with pytest.raises(ValueError, match=r"stale"):
            builder["_write_or_check"](tmp_path / "provider-parity-methodology.json", expected_bytes, check=True)

    def test_check_mode_rejects_divergent_output(
        self, builder: dict[str, Any], expected_bytes: bytes, tmp_path: Path
    ) -> None:
        """Check mode refuses a methodology file whose bytes differ from the generated ones.

        A hand-edited or out-of-date file must fail the check rather than be overwritten implicitly.
        """
        output = tmp_path / "provider-parity-methodology.json"
        output.write_bytes(b"{}\n")

        with pytest.raises(ValueError, match=r"stale"):
            builder["_write_or_check"](output, expected_bytes, check=True)

    def test_written_output_passes_check_mode(
        self, builder: dict[str, Any], expected_bytes: bytes, tmp_path: Path
    ) -> None:
        """Writing the generated bytes makes a following check-mode call succeed.

        This is the relock round trip: generate once explicitly, then verify without error.
        """
        output = tmp_path / "provider-parity-methodology.json"

        builder["_write_or_check"](output, expected_bytes, check=False)

        builder["_write_or_check"](output, expected_bytes, check=True)
        assert output.read_bytes() == expected_bytes

    def test_manifest_bytes_are_deterministic(self, builder: dict[str, Any], expected_bytes: bytes) -> None:
        """Building the manifest twice yields identical bytes.

        A non-deterministic generator would make every relock look like a change.
        """
        assert builder["_manifest_bytes"](builder["_build_manifest"]()) == expected_bytes

    def test_stale_error_names_the_exact_rebuild_command(self, builder: dict[str, Any], tmp_path: Path) -> None:
        """The stale error ends with the full rebuild command and never mentions the internal check flag.

        Internal check mode must never tell launcher users to remove a flag they did not pass.
        """
        with pytest.raises(ValueError, match=r"stale") as raised:
            builder["_write_or_check"](tmp_path / "stale.json", b"expected\n", check=True)

        assert str(raised.value).endswith("run: uv run python benchmarks/build-provider-parity-methodology-manifest.py")

    def test_rejects_a_tampered_passthrough_policy_seed(
        self, builder: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A generated methodology file can never bootstrap changed policy into acceptance.

        The policy seed is copied into the manifest verbatim; a seed whose execution budget was altered must make the
        generator exit with status 1 in check mode.
        """
        seed = load_manifest(BENCHMARKS / "policy" / "provider-parity-methodology.json")
        seed["execution_controls"]["budget"] = "tampered"
        seed_path = tmp_path / "provider-parity-methodology.json"
        seed_path.write_text(json.dumps(seed), encoding="utf-8")
        monkeypatch.setitem(builder["_load_policy_seed"].__globals__, "POLICY_SEED", seed_path)

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
    def test_derives_policy_for_each_task_case(
        self, builder: dict[str, Any], suite_path: str, task: dict[str, Any], expected: tuple[str, bool, bool]
    ) -> None:
        """Each task shape maps to its oracle class, effective scoreability, and headline eligibility.

        Current policy classification is covered by small independent edge cases, one per task shape.
        """
        headline_ids = set(builder["_load_policy_seed"]()["headline_structural_v1"]["task_ids"])

        row = builder["_task_row"](task, suite_path, headline_ids, set(), "diagnostic")

        assert (row["oracle_class"], row["effective_scoreable"], row["headline_eligible_v1"]) == expected

    def test_committed_output_is_current(self) -> None:
        """The committed provider-neutral source equals the explicit deterministic relock output.

        This runs the real generator in check mode as a subprocess: its exit status is the contract users and hooks
        depend on.
        """
        result = _run_methodology_builder("--check")

        assert result.returncode == 0, result.stderr


class TestMethodologyManifest:
    """The committed methodology manifest binds the current suites, policy seed, and implementation bytes."""

    def test_locks_exact_provider_neutral_structural_selection(self) -> None:
        """The ordered non-RI structural suite and its headline complement are locked identically for both providers.

        Execution order follows the repaired suite, real-issue tasks are excluded, and the diagnostic list is the
        execution list minus the confirmatory tasks.
        """
        manifest = load_manifest(METHODOLOGY_MANIFEST)
        cells = manifest["preregistered_cells"]
        codex_cells = load_manifest(CODEX_MANIFEST)["preregistered_cells"]

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

    def test_locks_the_complete_current_suite_universe(self) -> None:
        """The lock cannot silently omit a committed suite or task group.

        Each committed suite appears with its task count, and the integrity totals equal the sum over those suites.
        """
        methodology = load_manifest(METHODOLOGY_MANIFEST)
        observed = {suite["path"]: len(suite["tasks"]) for suite in methodology["suites"]}

        assert observed == EXPECTED_SUITE_TASK_COUNTS
        assert methodology["suite_integrity"]["suite_count"] == len(EXPECTED_SUITE_TASK_COUNTS)
        assert methodology["suite_integrity"]["task_count"] == sum(EXPECTED_SUITE_TASK_COUNTS.values())

    def test_has_no_prior_run_history(self) -> None:
        """The shared committed source carries no methodology change ledger.

        Prior-run history belongs outside the lock, which binds only the current suites and policy.
        """
        assert "methodology_change_ledger" not in load_manifest(METHODOLOGY_MANIFEST)

    def test_binds_the_materialized_suite_inputs(self) -> None:
        """Suite hashes, task order, and per-task hashes equal what the current source artifacts produce.

        A task or suite edited after the lock must show up as a hash mismatch here.
        """
        _assert_manifest_matches_materialized_suite_inputs(load_manifest(METHODOLOGY_MANIFEST))

    @pytest.mark.parametrize("field", ["target_source", "headline_structural_v1", "validation"])
    def test_copies_policy_seed_blocks(self, field: str) -> None:
        """Each policy-owned top-level block is identical to the policy seed's.

        The policy seed is the single source for these blocks; a manifest value that diverged would bypass it.
        """
        policy = load_manifest(BENCHMARKS / "policy" / "provider-parity-methodology.json")

        assert load_manifest(METHODOLOGY_MANIFEST)[field] == policy[field]

    @pytest.mark.parametrize(
        "field",
        [
            "confirmatory_repetitions",
            "pilot_repetitions",
            "providers",
            "smoke_task_ids",
            "structural_confirmatory_task_ids",
            "structural_pilot_task_ids",
        ],
    )
    def test_copies_policy_seed_preregistered_cells(self, field: str) -> None:
        """Each policy-owned preregistered-cell field is identical to the policy seed's.

        Repetition counts, providers, and task selections are preregistered in the seed and must not drift.
        """
        policy = load_manifest(BENCHMARKS / "policy" / "provider-parity-methodology.json")

        assert load_manifest(METHODOLOGY_MANIFEST)["preregistered_cells"][field] == policy["preregistered_cells"][field]

    def test_index_lock_matches_the_builder(self, builder: dict[str, Any]) -> None:
        """The manifest's index lock equals the generator's explicit index lock.

        Both name the scan version and raw hash of the Codemap index the runs query.
        """
        assert load_manifest(METHODOLOGY_MANIFEST)["index"] == builder["INDEX_LOCK"]

    @pytest.mark.parametrize("task_id", ["RV-01", "RV-02", "RV-03", "RV-04", "RV-05"])
    def test_binds_every_review_subquestion_in_the_provider_prompt(self, task_id: str) -> None:
        """Review follow-ups are hashed as delivered provider input, not as evaluator-only metadata.

        The delivered prompt differs from the bare task prompt because it appends every sub-question, and the manifest's
        prompt hash must be the hash of that delivered text.
        """
        methodology = load_manifest(METHODOLOGY_MANIFEST)
        suite = _suites_by_path(methodology)[REPAIRED_SUITE_PATH]
        manifest_tasks = {task["id"]: task for task in suite["tasks"]}
        source_tasks = {
            task["id"]: task
            for task in core.load_task_suite(ROOT / REPAIRED_SUITE_PATH)
            if task["id"].startswith("RV-")
        }
        task = source_tasks[task_id]

        delivered = core.materialize_task_prompt(task)

        assert source_tasks.keys() == {"RV-01", "RV-02", "RV-03", "RV-04", "RV-05"}
        assert delivered != task["prompt"]
        assert all(sub_question["prompt"] in delivered for sub_question in task["sub_questions"])
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
    def test_review_caller_tasks_lock_production_fn_rdeps_queries(self, task_id: str, target: str) -> None:
        """RV-03 and RV-04 query production callers only, exactly as their prompts and counts require.

        The expected query excludes tests, and the delivered prompt tells the model the same.
        """
        tasks = {task["id"]: task for task in core.load_task_suite(ROOT / REPAIRED_SUITE_PATH)}
        task = tasks[task_id]

        assert task["expected_queries"] == [{"cmd": "fn-rdeps", "args": [target, "--exclude-tests"]}]
        assert "production functions (excluding test files)" in core.materialize_task_prompt(task)

    def test_locks_codex_model_stratum_and_exact_implementation_identities(self) -> None:
        """The shared methodology binds the one allowed Codex model setting and the hash of every code artifact.

        Changing any benchmark runner, contract module, query skill, or Codemap source must move the pinned hash.
        """
        manifest = load_manifest(METHODOLOGY_MANIFEST)
        implementation = manifest["implementation_contract"]

        assert implementation["codex_model_stratum"] == {
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
            "strict_config": True,
        }
        assert implementation["artifact_sha256"] == {
            "agentic_contracts": sha256_file(BENCHMARKS / "_bench_common" / "agentic_contracts.py"),
            "agentic_reporting": sha256_file(BENCHMARKS / "_bench_common" / "agentic_reporting.py"),
            "claude_query_skill": sha256_file(ROOT / "plugins/codemap-py/claude-skills/query-code/SKILL.md"),
            "codemap_graph": module_sha256(ROOT / "plugins/codemap-py/src/codemap_py/graph.py"),
            # query.py is now a package; module_sha256 tree-hashes it, matching the generator.
            "codemap_query": module_sha256(ROOT / "plugins/codemap-py/src/codemap_py/query"),
            "codex_query_skill": sha256_file(ROOT / "plugins/codemap-py/codex-skills/query-code/SKILL.md"),
            "edit_patch_contracts": sha256_file(BENCHMARKS / "_bench_common" / "edit_patch_contracts.py"),
            "mutation_isolation": sha256_file(BENCHMARKS / "_bench_common" / "mutation_isolation.py"),
            "paid_lifecycle": sha256_file(BENCHMARKS / "_bench_common" / "paid_lifecycle.py"),
            "presentation": sha256_file(BENCHMARKS / "_bench_common" / "presentation.py"),
            "patch_index_locks": sha256_file(BENCHMARKS / "suites" / "patch-index-locks.json"),
            "provider_parity_contracts": sha256_file(BENCHMARKS / "_bench_common" / "provider_parity_contracts.py"),
            "run_all": sha256_file(BENCHMARKS / "run-all.sh"),
            "run_claude_agentic": runner_sha256(
                BENCHMARKS / "run-claude-agentic.py", BENCHMARKS / "_bench_claude" / "agentic"
            ),
            # Split runner: the pin covers the shim plus its package, so a change to the code that
            # actually runs still moves the hash. See _bench_common.artifact_hashing.runner_sha256.
            "run_claude_structural": runner_sha256(
                BENCHMARKS / "run-claude-structural.py", BENCHMARKS / "_bench_claude" / "structural"
            ),
            "run_codex_agentic": sha256_file(BENCHMARKS / "run-codex-agentic.py"),
            "run_codex_structural": runner_sha256(
                BENCHMARKS / "run-codex-structural.py", BENCHMARKS / "_bench_codex" / "structural"
            ),
        }
        assert "CODEMAP_BIN" in manifest["codex_permission_profiles"]["shell_environment"]["set_allowlist"]

    def test_locks_shared_continuous_fitness_and_observed_capability_strata(self) -> None:
        """Fitness and capability labels are shared, explicit, and derived from the suite.

        The capability-strata counts must equal what the tasks in the structural suite actually exhibit, and the
        prospective acceptance thresholds and arm-order rule are pinned verbatim.
        """
        manifest = load_manifest(METHODOLOGY_MANIFEST)
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
