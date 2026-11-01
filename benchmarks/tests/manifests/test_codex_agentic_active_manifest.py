"""No-model checks for the Codex agentic manifest.

The generated manifest is the contract a paid Codex agentic study is admitted against, so these tests pin its scope,
scoring identity, artifact hashes and arm semantics, check that it stays reproducible from its builder, and confirm the
runner only accepts its exact bytes. Nothing here uses a model or credentials.
"""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _manifest_helpers import load_manifest, sha256_file

ROOT = Path(__file__).resolve().parents[3]
BENCHMARKS = ROOT / "benchmarks"
BUILDER = BENCHMARKS / "build-codex-agentic-manifest.py"
MANIFEST = BENCHMARKS / "manifests" / "codex-agentic.json"
HUMAN_MANIFEST = BENCHMARKS / "manifests" / "codex-agentic.md"
METHODOLOGY = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
#: Task ids read from the shipped suite rather than counted out here, so adding a task to the suite changes the
#: expected scope instead of failing every scope assertion in this module.
AGENTIC_TASK_IDS = tuple(
    task["id"]
    for task in json.loads((BENCHMARKS / "suites" / "tasks-agentic.json").read_text(encoding="utf-8"))["tasks"]
)
AGENTIC_ARMS = ("A_plain", "B_auto", "C_strict")
#: One repetition of every suite coordinate — the default no-model plan and paid scope size.
AGENTIC_CELLS = len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS)


def _run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the deterministic builder without model or credential inputs."""
    return subprocess.run([sys.executable, str(BUILDER), *args], cwd=ROOT, check=False, capture_output=True, text=True)


@pytest.fixture(name="manifest")
def _manifest() -> dict[str, Any]:
    """Load the shipped Codex agentic manifest fresh for each test."""
    return load_manifest(MANIFEST)


@pytest.fixture(name="builder", scope="module")
def _builder() -> dict[str, Any]:
    """Namespace of the manifest builder script, loaded once because its functions are pure."""
    return runpy.run_path(str(BUILDER))


class TestManifestReproducibility:
    """The checked-in manifest bytes come from the builder, deterministically."""

    def test_is_current_and_regeneration_is_byte_stable(self, builder: dict[str, Any]) -> None:
        """Generation followed by check must reproduce identical machine and human bytes.

        The builder is run in-process to compare bytes with the checked-in files, then once through its real ``--check``
        command line, which is the gate contributors and CI actually run.
        """
        manifest = builder["_build_manifest"]()
        machine_before = builder["_json_bytes"](manifest)
        human_before = builder["_human_bytes"](manifest, hashlib.sha256(machine_before).hexdigest())
        assert machine_before == MANIFEST.read_bytes()
        assert human_before == HUMAN_MANIFEST.read_bytes()
        assert _run_builder("--check").returncode == 0

    def test_stale_error_names_exact_rebuild_command(self, builder: dict[str, Any], tmp_path: Path) -> None:
        """Internal check mode must identify the command that repairs generated drift.

        A stale file is checked against different expected bytes; the error has to end with the copy-pasteable rebuild
        command so a contributor never has to guess how to regenerate.
        """
        with pytest.raises(ValueError, match=r"run: uv run python benchmarks/build-codex-agentic-manifest\.py$"):
            builder["_write_or_check"](tmp_path / "stale.json", b"expected\n", check=True)

    @pytest.mark.parametrize(
        ("study", "command_count"),
        [pytest.param("agentic", 2, id="agentic"), pytest.param("integration", 1, id="integration")],
    )
    def test_single_model_commands_name_the_locked_model(self, study: str, command_count: int) -> None:
        """Single-model approval examples must not inherit the launcher's multi-model default.

        Each human manifest shows copyable ``run-all.sh codex`` commands; every one has to pin ``--models`` to the
        manifest's own model, or an approver could run a different, unreviewed model.
        """
        study_builder = runpy.run_path(str(BENCHMARKS / f"build-codex-{study}-manifest.py"))
        study_manifest = load_manifest(BENCHMARKS / "manifests" / f"codex-{study}.json")
        human = study_builder["_human_bytes"](study_manifest, "reviewed-machine-digest").decode("utf-8")
        commands = [line for line in human.splitlines() if "bash benchmarks/run-all.sh codex --" in line]

        assert len(commands) == command_count
        assert all(f"--models={study_manifest['model']['name']}" in line for line in commands)


class TestManifestScope:
    """The default study covers the complete shared agentic suite once per canonical arm."""

    def test_locks_experiment_identity_and_model(self, manifest: dict[str, Any]) -> None:
        """The manifest names its experiment, the locked model strata and the shared task order.

        A silent model or task-list change would alter what a paid run measures, so identity is pinned exactly.
        """
        assert manifest["experiment_id"] == "codex-agentic"
        assert manifest["model"] == {
            "name": "gpt-6.1-sol",
            "additional_strata": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"],
            "reasoning_effort": "high",
            "strict_config": True,
        }
        assert tuple(task["id"] for task in manifest["tasks"]) == AGENTIC_TASK_IDS

    def test_preregisters_every_task_once_per_arm_as_nonpoolable(self, manifest: dict[str, Any]) -> None:
        """The default scope is the complete shared suite once in each canonical arm, exploratory only.

        Prevents an apparently valid manifest from silently keeping an incomplete task subset, using a noncanonical arm
        label, or multiplying the default study beyond one repetition of every suite task in every arm.
        """
        scope = manifest["preregistered_scope"]
        assert len(AGENTIC_TASK_IDS) == 20
        assert AGENTIC_CELLS == 60
        assert tuple(scope["task_ids"]) == AGENTIC_TASK_IDS
        assert tuple(scope["arms"]) == AGENTIC_ARMS
        assert scope["repetitions"] == 1
        assert scope["total_cells"] == AGENTIC_CELLS
        assert scope["coordinate_timeout_seconds"] == 600
        assert "complete_run_max_wall_clock_seconds" not in scope
        assert scope["nonpoolable"] is True
        assert scope["pooling_eligibility"] == "ineligible; exploratory evidence only"

    def test_pins_target_source_and_frozen_index(self, manifest: dict[str, Any]) -> None:
        """The analysed source tag, commit and frozen index digest are locked.

        Every cell queries one frozen index of one source commit; a different tag or index would make results
        incomparable with the preregistered study.
        """
        assert manifest["target_source"]["tag"] == "2.6.5"
        assert manifest["target_source"]["commit"] == "be98784a1a03581b7051a355ae1084fd352d7cea"
        assert (
            manifest["frozen_index_contract"]["raw_sha256"]
            == "3c5840893e9c939baa61a6c5ce95994ff69ffe4a67d225aeb412c73deb61e0c1"
        )

    def test_scope_task_ids_match_shared_methodology_suite(self, manifest: dict[str, Any]) -> None:
        """The Codex scope and the provider-parity methodology list the same ordered agentic tasks.

        A Codex manifest drifting from the shared methodology would no longer be comparable with the Claude study.
        """
        methodology = load_manifest(METHODOLOGY)
        shared_suite = next(
            suite for suite in methodology["suites"] if suite["path"] == "benchmarks/suites/tasks-agentic.json"
        )

        assert tuple(shared_suite["ordered_task_ids"]) == AGENTIC_TASK_IDS
        assert tuple(manifest["preregistered_scope"]["task_ids"]) == AGENTIC_TASK_IDS

    def test_builder_declares_reproducible_balanced_execution_order(self, builder: dict[str, Any]) -> None:
        """A study must lock its arm rotation instead of silently changing execution order.

        The builder output, not only the checked-in file, must carry the cyclic arm order so regeneration keeps it.
        """
        manifest = builder["_build_manifest"]()
        assert manifest["execution_order"] == {
            "strategy": "cyclic-arm-order-v1",
            "arm_cycle": list(AGENTIC_ARMS),
            "index_basis": "locked-task-ordinal-plus-repetition-minus-one",
        }
        assert "three repetitions" in manifest["preregistered_scope"]["arm_order"]
        assert "summary.json" in manifest["artifact_package"]["required_files"]
        assert "exact installed launcher" in manifest["arms"]["C_strict"]["requirement"]

    @pytest.mark.parametrize(
        ("source_file", "contract_key"),
        [
            pytest.param("codex-agentic.json", "scoring", id="codex-manifest"),
            pytest.param("provider-parity-methodology.json", "agentic_execution_contract", id="shared-methodology"),
        ],
    )
    def test_graph_query_measurement_scope_is_limited(self, source_file: str, contract_key: str) -> None:
        """Graph-answer quality must not be advertised as general change-impact correctness.

        Both the Codex manifest and the shared methodology declare the measurement a static graph query with an explicit
        claim limit, so neither can be quoted as evidence of behavioural correctness.
        """
        contract = load_manifest(BENCHMARKS / "manifests" / source_file)[contract_key]

        assert contract["measurement_scope"] == "static_graph_query"
        assert (
            contract["quality_claim_limit"]
            == "Declared static graph facts only; no behavioral change-impact or implementation correctness claim."
        )

    def test_claude_and_codex_load_identical_shared_prompts(self, manifest: dict[str, Any]) -> None:
        """Both provider loaders deliver the same ordered task prompts and locked hashes.

        Parity between providers depends on identical prompt bytes; the loaders are exercised directly and their output
        compared with the prompt hashes recorded in the Codex manifest.
        """
        suite_path = BENCHMARKS / "suites" / "tasks-agentic.json"
        claude = runpy.run_path(str(BENCHMARKS / "run-claude-agentic.py"))
        codex = runpy.run_path(str(BENCHMARKS / "run-codex-agentic.py"))
        claude_tasks = claude["load_tasks_with_provenance"](suite_path, METHODOLOGY)
        codex_tasks = codex["load_agentic_tasks"](suite_path, MANIFEST)
        locked_hashes = {task["id"]: task["prompt_sha256"] for task in manifest["tasks"]}

        assert [(task.id, task.prompt) for task in claude_tasks] == [
            (task["id"], task["prompt"]) for task in codex_tasks
        ]
        assert {
            task.id: hashlib.sha256(task.prompt.encode("utf-8")).hexdigest() for task in claude_tasks
        } == locked_hashes

    @pytest.mark.parametrize(
        ("task_id", "fields", "params"),
        [
            pytest.param(
                "BA-12",
                [
                    "production_importers",
                    "excluded_test_importer_count",
                    "ranking",
                    "production_importer_count",
                ],
                {"ranking": {"candidate_set": "production_importers", "top_k": 10}},
                id="ba-12-excluded-test-count",
            ),
            pytest.param(
                "BA-16",
                [
                    "production_importers",
                    "high_centrality",
                    "test_importer_count",
                    "production_importer_count",
                    "risk_tier",
                ],
                {
                    "high_centrality": {"min_rdep_count": 10},
                    "risk_tier": {
                        "critical_min_production_importer_count": 10,
                        "critical_min_high_centrality_count": 1,
                    },
                },
                id="ba-16-risk-verdict",
            ),
        ],
    )
    def test_answer_contract_declares_every_scored_field(
        self, task_id: str, fields: list[str], params: dict[str, Any]
    ) -> None:
        """Prompt-required counts and verdicts cannot disappear behind importer recall.

        Prevents a production-only oracle from grading only the importer list while treating BA-12's excluded-test count
        or BA-16's separate test/production counts and risk verdict as unscored prose. A plausibly wrong scorer that
        preserves EREC/RREC alone fails this declaration-level contract.
        """
        suite = load_manifest(BENCHMARKS / "suites" / "tasks-agentic.json")
        contract = {task["id"]: task for task in suite["tasks"]}[task_id]["answer_contract"]

        assert contract["fields"] == fields
        assert {key: contract["params"][key] for key in params} == params


class TestManifestScoringAndRuntime:
    """Scorer formulas, artifact identities and runtime isolation are explicit and verifiable."""

    def test_scoring_names_shared_implementation_symbols_and_metrics(self, manifest: dict[str, Any]) -> None:
        """The scorer entry points and every reported metric formula are spelled out.

        Reviewers approve a study on these definitions, so a renamed symbol or reworded metric must fail loudly.
        """
        scoring = manifest["scoring"]
        assert scoring["implementation"]["semantic_symbol"] == "score_answer"
        assert scoring["implementation"]["response_symbol"] == "assess_answer_response"
        assert scoring["implementation"]["evidence_symbol"] == "score_evidence_metrics"
        assert scoring["metrics"] == {
            "quality": "graded admitted credit / all assigned cells; unknown grading is explicitly unavailable",
            "pass": "fully correct, completed, valid, uncontaminated and treatment-adherent cells / all assigned cells",
            "component": "secondary mean semantic component score over scored cells; report scored denominator",
            "EREC": "expected-importer recall in all agent text, independent of answer-envelope validity",
            "RREC": "expected-importer recall in the final report, independent of answer-envelope validity",
            "DEFF": "unbounded expected-importer exposure hits per command",
        }
        assert scoring["reporting_version"] == "agentic-graded-v2"
        assert "both cells pass" in scoring["efficiency_policy"]

    def test_artifact_hashes_are_complete_and_bind_the_runtime(self, manifest: dict[str, Any]) -> None:
        """Every locked artifact has a SHA-256, the reporting source matches it, and the runtime reuses the same map.

        The hashes are the identity of the code that scores and runs the study; a stale reporting hash or a runtime
        block carrying a different map would let changed code run under an old approval.
        """
        hashes = manifest["artifact_sha256"]
        assert len(hashes) == 13
        assert "run_all" in hashes
        assert all(len(value) == 64 for value in hashes.values())
        assert hashes["agentic_reporting"] == sha256_file(ROOT / "benchmarks/_bench_common/agentic_reporting.py")
        assert manifest["plugin_runtime"]["source_hashes"] == hashes

    @pytest.mark.parametrize(
        ("runtime_key", "plugin_manifest"),
        [
            pytest.param("codemap_version", "plugins/codemap-py/.codex-plugin/plugin.json", id="codemap"),
            pytest.param("codex_rig_version", "plugins/codex-rig/.codex-plugin/plugin.json", id="codex-rig"),
        ],
    )
    def test_plugin_runtime_versions_match_plugin_manifests(
        self, manifest: dict[str, Any], runtime_key: str, plugin_manifest: str
    ) -> None:
        """The locked plugin versions equal the versions shipped in the plugin manifests.

        A plugin release without a manifest re-lock would leave the study approved against a version nobody runs.
        """
        assert manifest["plugin_runtime"][runtime_key] == load_manifest(ROOT / plugin_manifest)["version"]

    def test_runtime_isolation_and_artifact_package_are_declared(self, manifest: dict[str, Any]) -> None:
        """The isolation manifest, B arm mapping and retained artifact list are fixed.

        The artifact list defines which evidence every run must retain for later audit, including the raw telemetry and
        checksum files.
        """
        assert manifest["runtime_isolation"]["manifest"] == "benchmarks/manifests/codex-integration.json"
        assert manifest["runtime_isolation"]["mapping"]["B_auto"].endswith("use remains optional")
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

    def test_arm_admission_semantics_are_explicit(self, manifest: dict[str, Any]) -> None:
        """Optional B use and required C Skill sequencing remain distinguishable.

        B may legitimately make no Codemap call while C must, and a non-compliant C row is retained rather than dropped,
        which keeps treatment adherence measurable.
        """
        arms = manifest["arms"]
        assert arms["A_plain"]["codemap_available"] is False
        assert arms["B_auto"]["no_call_valid"] is True
        assert arms["C_strict"]["no_call_valid"] is False
        assert arms["C_strict"]["row_retained_on_noncompliance"] is True


class TestManifestHygiene:
    """The lock files neither leak credentials nor point at ignored or historical artifacts."""

    @pytest.mark.parametrize(
        "forbidden",
        [
            "password",
            "secret",
            "api_key",
            'codex_auth_source="/users/',
            "benchmarks/results",
            "results/manifests",
            "historical/",
            "__pycache__",
            "first-slice",
        ],
    )
    def test_lock_files_omit_forbidden_text(self, forbidden: str) -> None:
        """The lock cannot leak auth material or point at result/history artifacts.

        Both the machine and human manifest are searched case-insensitively for credential words, a host-specific auth
        path, and references to ignored result directories or earlier slices.
        """
        text = MANIFEST.read_text(encoding="utf-8") + HUMAN_MANIFEST.read_text(encoding="utf-8")

        assert forbidden not in text.lower()

    def test_auth_source_is_resolved_from_home(self) -> None:
        """Approval commands resolve the Codex auth file through ``$HOME`` instead of a literal user path.

        A literal ``/Users/<name>`` path would only work on one machine and would publish the author's account name.
        """
        text = MANIFEST.read_text(encoding="utf-8") + HUMAN_MANIFEST.read_text(encoding="utf-8")

        assert 'CODEX_AUTH_SOURCE="$HOME/.codex/auth.json"' in text
        assert 'CODEX_AUTH_SOURCE="/Users/' not in text


class TestRunnerAdmission:
    """The agentic runner admits a paid run only against the exact current manifest bytes."""

    def test_admits_exact_manifest_hash(self) -> None:
        """The runner accepts the current manifest hash and reports paid execution as admitted.

        This is the happy path of the approval handshake between the generated admission contract and the runner.
        """
        runner = runpy.run_path(str(BENCHMARKS / "run-codex-agentic.py"))

        admitted = runner["validate_paid_admission"](MANIFEST, sha256_file(MANIFEST))

        assert admitted["admission"]["paid_execution"] == "admitted"

    def test_rejects_any_other_manifest_hash(self) -> None:
        """A hash that is not the exact current manifest digest is refused.

        An approval recorded for earlier manifest bytes must not authorize spending against a changed lock.
        """
        runner = runpy.run_path(str(BENCHMARKS / "run-codex-agentic.py"))

        with pytest.raises(ValueError, match="exact current manifest"):
            runner["validate_paid_admission"](MANIFEST, "0" * 64)
