"""Acceptance contract for the provider-neutral parity contract library.

These tests intentionally define the small B1 surface shared by future provider adapters. They do not exercise a
provider CLI or execute a model.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from benchmarks._bench_common import agentic_contracts
from benchmarks._bench_common import provider_parity_contracts as core


BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "provider-parity-methodology.json"
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
EXPERIMENT_REVISION = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["experiment_revision"]


def _record(**overrides: Any) -> Any:
    """Build an eligible synthetic record, overriding selected coordinates or measurements.

    >>> record = _record(input_tokens=0, quality_score=1.0)
    >>> record.input_tokens, record.quality_score, record.repetition
    (0, 1.0, 1)
    """
    values = {
        "revision": EXPERIMENT_REVISION,
        "provider": "claude",
        "model": "test-model",
        "task_id": "FN-02",
        "repetition": 1,
        "arm": "A_plain",
        "input_tokens": 100,
        "quality_score": 0.6,
        "treatment_adherence": True,
    }
    values.update(overrides)
    return core.ResultRecord(**values)


def _synthetic_policies() -> dict[str, Any]:
    """Return a fresh independent-task policy mapping for the synthetic pairing task.

    >>> policy, = _synthetic_policies().values()
    >>> policy.oracle_class, policy.headline_eligible_v1, policy.scoreable
    ('independent', True, True)
    """
    policy = core.TaskPolicy(
        experiment_revision=EXPERIMENT_REVISION,
        task_id="FN-02",
        oracle_class="independent",
        headline_eligible_v1=True,
        scoreable=True,
    )
    return {policy.task_id: policy}


def _manifest_task(task_id: str) -> dict[str, Any]:
    """Read the locked task row, failing explicitly when no suite contains the requested identifier.

    >>> _manifest_task("not-a-real-task")
    Traceback (most recent call last):
        ...
    AssertionError: manifest task 'not-a-real-task' was not found
    """
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for suite in manifest["suites"]:
        for task in suite["tasks"]:
            if task["id"] == task_id:
                return task
    raise AssertionError(f"manifest task {task_id!r} was not found")


class TestTaskSuiteLoading:
    """Raw task loading must not silently apply runner-specific normalization."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(
                [{"id": "L-01", "prompt": "list task", "custom": {"nested": True}}],
                [{"id": "L-01", "prompt": "list task", "custom": {"nested": True}}],
                id="bare-list",
            ),
            pytest.param(
                {
                    "repo": {"name": "fixture"},
                    "tasks": [{"id": "O-01", "prompt": "object task", "scoreable": False}],
                },
                [{"id": "O-01", "prompt": "object task", "scoreable": False}],
                id="object-wrapper",
            ),
        ],
    )
    def test_load_task_suite_valid_shape_preserves_raw_task_mapping(
        self, tmp_path: Path, payload: list[dict[str, Any]] | dict[str, Any], expected: list[dict[str, Any]]
    ) -> None:
        """A list or wrapper loads exactly its raw task mappings without inferred fields."""
        suite_path = tmp_path / "suite.json"
        suite_path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = core.load_task_suite(suite_path)

        assert loaded == expected

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            pytest.param({"repo": {"name": "missing-tasks"}}, "tasks", id="wrapper-without-tasks"),
            pytest.param({"tasks": {"id": "not-a-list"}}, "list", id="wrapper-tasks-not-list"),
            pytest.param(["not a mapping"], "object", id="list-entry-not-object"),
            pytest.param([{"prompt": "missing id"}], "id", id="missing-id"),
            pytest.param([{"id": "M-01"}], "prompt", id="missing-prompt"),
            pytest.param([{"id": "", "prompt": "empty id"}], "id", id="empty-id"),
            pytest.param([{"id": "M-02", "prompt": ""}], "prompt", id="empty-prompt"),
            pytest.param(
                [{"id": "D-01", "prompt": "first"}, {"id": "D-01", "prompt": "duplicate"}],
                "duplicate",
                id="duplicate-id",
            ),
        ],
    )
    def test_load_task_suite_rejects_invalid_contract(self, tmp_path: Path, payload: object, error: str) -> None:
        """Malformed task files fail instead of changing suite membership or identity."""
        suite_path = tmp_path / "invalid-suite.json"
        suite_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match=error):
            core.load_task_suite(suite_path)


class TestCanonicalTaskIdentity:
    """Task and prompt identity must match the tracked methodology byte for byte."""

    def test_canonical_task_hash_and_prompt_hash_match_locked_manifest(self) -> None:
        """FN-02 has the manifest's raw-object hash and exact delivered prompt hash."""
        suite = core.load_task_suite(SUITE_PATH)
        task = next(task for task in suite if task["id"] == "FN-02")
        locked = _manifest_task("FN-02")
        expected_bytes = json.dumps(task, sort_keys=True).encode("utf-8")

        assert core.canonical_task_bytes(task) == expected_bytes
        assert core.canonical_task_hash(task) == locked["canonical_task_sha256"]
        assert core.prompt_hash(task) == locked["prompt_sha256"]
        assert core.prompt_hash(task) == hashlib.sha256(core.materialize_task_prompt(task).encode("utf-8")).hexdigest()

    def test_canonical_task_hash_includes_raw_fields_without_normalization(self) -> None:
        """Adding a raw field changes identity, preventing inferred-field substitution."""
        task = {"id": "T-01", "prompt": "Inspect this.", "custom": {"order": 1}}
        altered_task = {**task, "scoreable": False}

        assert core.canonical_task_hash(task) != core.canonical_task_hash(altered_task)

    def test_semantic_suite_hash_tracks_ordered_tasks_not_root_wrapper_metadata(self) -> None:
        """Suite identity ignores transport metadata but detects task content and ordering drift."""
        tasks = [
            {"id": "T-01", "prompt": "First.", "type": "demo"},
            {"id": "T-02", "prompt": "Second.", "type": "demo"},
        ]

        expected = core.semantic_suite_hash(tasks)

        assert core.semantic_suite_hash(list(tasks)) == expected
        assert core.semantic_suite_hash(list(reversed(tasks))) != expected
        assert core.semantic_suite_hash([{**tasks[0], "prompt": "Changed."}, tasks[1]]) != expected


class TestArmContracts:
    """A/B/C instructions are locked separately from canonical task prompts."""

    def test_parity_timeout_is_one_shared_provider_neutral_budget(self) -> None:
        """Every provider adapter receives the same 600-second parity timeout."""
        assert core.PARITY_TIMEOUT_SECONDS == 600

    def test_arm_contracts_match_manifest_and_are_immutable(self) -> None:
        """The current methodology names the same immutable Claude arm contracts."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        assert set(core.ARM_CONTRACTS) == set(manifest["preregistered_cells"]["arms"])
        for expected in core.ARM_CONTRACTS.values():
            assert expected["contract_sha256"] == hashlib.sha256(expected["contract"].encode("utf-8")).hexdigest()

        with pytest.raises(TypeError):
            core.ARM_CONTRACTS["A_plain"]["contract"] = "changed"

    def test_comparison_arm_registry_shares_one_arm_set_and_still_reads_pre_alignment_names(self) -> None:
        """Both providers run the same three arms, and frozen Codex rows still resolve.

        Codex once named its treatment arms after their delivery mechanism. Those runs are immutable, so their names
        have to keep resolving for grouping and pairing even though no new run can produce them; a registry that
        recognized only the canonical three would fail on frozen rows.
        """
        assert core.COMPARISON_ARMS_BY_PROVIDER == {
            "claude": frozenset({"A_plain", "B_auto", "C_strict"}),
            "codex": frozenset({"A_plain", "B_auto", "C_strict"}),
        }
        assert core.CANONICAL_ARM_NAMES == frozenset({"A_plain", "B_auto", "C_strict"})
        assert core.COMPARISON_ARM_NAMES == frozenset(
            {"A_plain", "B_auto", "C_strict", "B_direct_required", "C_skill_required"}
        )
        assert core.canonical_arm("B_direct_required") == "B_auto"
        assert core.canonical_arm("C_skill_required") == "C_strict"
        assert set(core.ARM_CONTRACTS) == {"A_plain", "B_auto", "C_strict"}

        with pytest.raises(TypeError):
            core.COMPARISON_ARMS_BY_PROVIDER["codex"] = frozenset()

    def test_arm_contract_lookup_does_not_mutate_task_prompt(self) -> None:
        """Arm metadata remains separate so every provider receives identical task bytes."""
        task = {"id": "T-02", "prompt": "Keep these exact bytes: café."}
        original_prompt = task["prompt"]
        original_bytes = core.canonical_task_bytes(task)

        for arm in core.ARM_CONTRACTS:
            _ = core.ARM_CONTRACTS[arm]["contract"]

        assert task["prompt"] == original_prompt
        assert core.canonical_task_bytes(task) == original_bytes

    def test_arm_order_is_revision_bound_and_rejects_invalid_coordinates(self) -> None:
        """One block has a stable complete order that changes with experiment identity."""
        fixed_revision = "arm-order-fixed-oracle"
        fixed_order = core.deterministic_arm_order(
            fixed_revision,
            "claude",
            "gpt-5.6-luna",
            "FN-02",
            1,
        )

        assert set(fixed_order) == set(core.ARM_CONTRACTS)
        revision_orders = {
            core.deterministic_arm_order(
                f"{fixed_revision}:variant-{index}",
                "claude",
                "gpt-5.6-luna",
                "FN-02",
                1,
            )
            for index in range(8)
        }
        assert any(order != fixed_order for order in revision_orders)
        with pytest.raises(ValueError, match="repetition"):
            core.deterministic_arm_order(fixed_revision, "claude", "model", "FN-02", 0)

    def test_arm_order_returns_the_named_provider_own_arms(self) -> None:
        """The shared ordering is provider-keyed, not Claude-only."""
        coordinates = ("arm-order-fixed-oracle", "gpt-5.6-luna", "FN-02", 1)
        revision, model, task_id, repetition = coordinates

        claude = core.deterministic_arm_order(revision, "claude", model, task_id, repetition)
        codex = core.deterministic_arm_order(revision, "codex", model, task_id, repetition)

        assert set(claude) == core.COMPARISON_ARMS_BY_PROVIDER["claude"]
        assert set(codex) == core.COMPARISON_ARMS_BY_PROVIDER["codex"]
        with pytest.raises(ValueError, match="unknown provider"):
            core.deterministic_arm_order(revision, "gemini", model, task_id, repetition)

    def test_arm_order_includes_reasoning_effort_in_the_model_stratum(self) -> None:
        """Effort drift must change the randomized block identity."""
        high = [
            core.deterministic_arm_order(
                "effort-aware-revision",
                "claude",
                "gpt-5.6-luna",
                f"T-{index}",
                1,
                reasoning_effort="high",
            )
            for index in range(12)
        ]
        medium = [
            core.deterministic_arm_order(
                "effort-aware-revision",
                "claude",
                "gpt-5.6-luna",
                f"T-{index}",
                1,
                reasoning_effort="medium",
            )
            for index in range(12)
        ]

        assert high == [
            core.deterministic_arm_order(
                "effort-aware-revision",
                "claude",
                "gpt-5.6-luna",
                f"T-{index}",
                1,
                reasoning_effort="high",
            )
            for index in range(12)
        ]
        assert any(left != right for left, right in zip(high, medium))

    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            pytest.param(
                {"type": "develop_blast_radius", "ground_truth": {"fn_callers": [str(i) for i in range(20)]}},
                ("direct_reverse_call", "high_fan_in"),
                id="type-develop_blast_radius-ground_truth-fn_callers-str-i-for-i-in-range-2",
            ),
            pytest.param(
                {"type": "graph_fn_blast", "ground_truth": {"blast_callers": ["a", "b"]}},
                ("transitive_reverse_call",),
                id="type-graph_fn_blast-ground_truth-blast_callers-a-b",
            ),
            pytest.param(
                {"type": "graph_path", "ground_truth": {"import_path": ["a", "b", "c"]}},
                ("dependency_path",),
                id="type-graph_path-ground_truth-import_path-a-b-c",
            ),
            pytest.param(
                {"type": "diff_impact", "ground_truth": {"fn_callers": ["a"], "test_modules": ["test_a"]}},
                ("diff_impact", "test_selection"),
                id="type-diff_impact-ground_truth-fn_callers-a-test_modules-test_a",
            ),
        ],
    )
    def test_capability_strata_expose_where_codemap_can_help(
        self,
        task: dict[str, Any],
        expected: tuple[str, ...],
    ) -> None:
        """The suite must expose named structural-capability strata."""
        assert core.capability_strata(task) == expected

    def test_active_manifest_locks_split_profiles_and_provider_adapters(self) -> None:
        """The active manifest must describe the exact tested Codex permission and adapter boundary."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        profiles = manifest["codex_permission_profiles"]

        assert manifest["experiment_revision"] == EXPERIMENT_REVISION
        assert profiles["plain"]["extends"] == ":read-only"
        assert profiles["plain"]["write_roots"] == []
        assert profiles["plain"]["filesystem_overrides"]["<locked-index-parent>"] == "deny"
        assert profiles["treatment"]["extends"] == ":read-only"
        assert profiles["treatment"]["write_roots"] == ["<locked-index-parent>/.index-rw"]
        assert profiles["plain"]["network_enabled"] is False
        assert profiles["treatment"]["network_enabled"] is False
        assert profiles["shell_environment"]["inherit"] == "none"
        assert profiles["shell_environment"]["secret_inheritance"] is False
        assert profiles["treatment_runtime"] == {
            "required_major_minor": [3, 11],
            "resolution": (
                "first executable Python reporting the required major/minor from the reviewed runtime path candidates"
            ),
            "scope": ["B_auto", "C_strict"],
        }
        assert manifest["execution_controls"]["codex_transport"] == (
            "run-codex-structural.py and run-codex-agentic.py provider adapters"
        )


class TestTaskPolicies:
    """The tracked methodology manifest is the sole source of headline eligibility policy."""

    def test_load_task_policies_matches_every_unique_manifest_task_and_revision(self) -> None:
        """Every manifest row becomes one immutable policy carrying its experiment revision."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        expected_rows = [task for suite in manifest["suites"] for task in suite["tasks"]]

        policies = core.load_task_policies(MANIFEST_PATH)

        assert set(policies) == {task["id"] for task in expected_rows}
        assert len(policies) == len(expected_rows)
        for task in expected_rows:
            policy = policies[task["id"]]
            assert policy.experiment_revision == manifest["experiment_revision"]
            assert policy.task_id == task["id"]
            assert policy.oracle_class == task["oracle_class"]
            assert policy.headline_eligible_v1 is task["headline_eligible_v1"]
            assert policy.scoreable is task["effective_scoreable"]

        with pytest.raises(TypeError):
            policies["FN-02"] = policies["FN-02"]
        with pytest.raises(AttributeError):
            policies["FN-02"].scoreable = False

    @pytest.mark.parametrize("task_id", ("SE-01", "RV-05", "CQ-02", "CQ-03", "CQ-04", "CQ-05", "RI-05"))
    def test_manifest_policy_keeps_known_diagnostic_tasks_out_of_headline_pairing(self, task_id: str) -> None:
        """Policy, not optional record flags, blocks approved diagnostic and unscoreable tasks."""
        policies = core.load_task_policies(MANIFEST_PATH)

        assert policies["SE-01"].oracle_class == "static_reference"
        assert policies["RI-05"].scoreable is False
        assert core.result_eligibility(_record(task_id=task_id), policies) is False


class TestEvaluatorRegistry:
    """Scoring dispatch is explicit, provider-neutral, and fail-closed."""

    def test_evaluate_scoreable_task_uses_registered_evaluator_without_provider(self) -> None:
        """Identical adapter-labelled calls receive an identical shared score."""
        calls: list[tuple[dict[str, Any], str]] = []

        def _evaluate_demo(task: dict[str, Any], output_text: str) -> Any:
            """Record evaluator inputs and return a fixed partial-quality result."""
            calls.append((task, output_text))
            return core.EvaluationResult(scored=True, correct=True, quality_score=0.75)

        registry = core.EvaluatorRegistry({"demo": _evaluate_demo})
        task = {"id": "EV-01", "prompt": "Score me", "type": "demo", "scoreable": True}
        output_text = "shared answer"

        adapter_results = {adapter: registry.evaluate(task, output_text) for adapter in ("claude", "codex")}

        assert adapter_results["claude"] == adapter_results["codex"]
        assert adapter_results["claude"].scored is True
        assert adapter_results["claude"].correct is True
        assert adapter_results["claude"].quality_score == pytest.approx(0.75)
        assert calls == [(task, output_text), (task, output_text)]

    def test_evaluate_unscoreable_task_bypasses_unknown_evaluator(self) -> None:
        """A deliberately unscoreable task remains diagnostic without registry dispatch."""
        registry = core.EvaluatorRegistry({})

        result = registry.evaluate({"id": "U-01", "type": "unknown", "scoreable": False}, "answer")

        assert result == core.EvaluationResult(scored=False, correct=False, quality_score=None)

    def test_evaluate_unknown_scoreable_task_rejects_methodology_drift(self) -> None:
        """A new scoreable type cannot silently become an unscored result."""
        registry = core.EvaluatorRegistry({})

        with pytest.raises(ValueError, match="unknown"):
            registry.evaluate({"id": "U-02", "type": "unknown", "scoreable": True}, "answer")


class TestResultEligibility:
    """Headline pairing excludes preregistered diagnostic and failed-result classes."""

    def test_result_record_requires_explicit_treatment_adherence(self) -> None:
        """Pairing cannot infer adherence from the arm name or a permissive default."""
        with pytest.raises(TypeError, match="treatment_adherence"):
            core.ResultRecord(
                revision=EXPERIMENT_REVISION,
                provider="claude",
                model="test-model",
                task_id="FN-02",
                repetition=1,
                arm="A_plain",
                input_tokens=100,
                quality_score=0.6,
            )

    def test_result_eligibility_requires_explicit_policy_mapping(self) -> None:
        """Eligibility without B0 policy provenance cannot infer a headline result."""
        with pytest.raises(TypeError):
            core.result_eligibility(_record())

    def test_result_eligibility_accepts_clean_independent_score(self) -> None:
        """A complete independent scoreable result is eligible for paired headline metrics."""
        assert core.result_eligibility(_record(), _synthetic_policies()) is True

    @pytest.mark.parametrize(
        ("provider", "baseline_arm", "treatment_arm", "invalid_arm"),
        [
            pytest.param("claude", "A_plain", "B_auto", "A_plain", id="plain-adherence-false"),
            pytest.param("claude", "A_plain", "C_strict", "C_strict", id="claude-required-adherence-false"),
            pytest.param("codex", "A_plain", "B_auto", "B_auto", id="codex-direct-adherence-false"),
            pytest.param("codex", "A_plain", "C_strict", "C_strict", id="codex-skill-adherence-false"),
        ],
    )
    def test_false_treatment_adherence_is_ineligible_and_rejects_pairing(
        self, provider: str, baseline_arm: str, treatment_arm: str, invalid_arm: str
    ) -> None:
        """Every arm needs observed adherence; required-use arms receive no special permissiveness."""
        records = [
            _record(provider=provider, arm=baseline_arm, treatment_adherence=baseline_arm != invalid_arm),
            _record(provider=provider, arm=treatment_arm, treatment_adherence=treatment_arm != invalid_arm),
        ]
        invalid_record = next(record for record in records if record.arm == invalid_arm)

        assert core.result_eligibility(invalid_record, _synthetic_policies()) is False
        with pytest.raises(ValueError, match="ineligible"):
            core.pair_effects(
                records,
                baseline_arm=baseline_arm,
                treatment_arm=treatment_arm,
                policies=_synthetic_policies(),
            )

    @pytest.mark.parametrize("arm", ["B_auto", "B_auto"])
    def test_zero_query_b_cell_is_adherent_on_both_providers(self, arm: str) -> None:
        """B is an optional-use canary, so declining to query is compliant.

        Treating the Codex B arm as required-use marked exactly the no-query cells non-adherent, dropping them from
        pooling and biasing the pooled B result toward the runs that happened to use Codemap.
        """
        assert core.treatment_adherence(arm, codemap_use_compliance=False, contaminated=False) is True

    @pytest.mark.parametrize("arm", ["C_strict", "C_strict"])
    def test_zero_query_c_cell_remains_non_adherent(self, arm: str) -> None:
        """The strict arm keeps its required-use contract on both providers."""
        assert core.treatment_adherence(arm, codemap_use_compliance=False, contaminated=False) is False

    def test_contamination_still_overrides_optional_use_adherence(self) -> None:
        """Optional use never excuses a contaminated cell."""
        assert core.treatment_adherence("B_auto", codemap_use_compliance=False, contaminated=True) is False

    def test_zero_query_codex_b_cell_stays_pooling_eligible(self) -> None:
        """Adherence alone is useless if the row is still dropped from pooling."""
        record = _record(
            provider="codex",
            arm="B_auto",
            treatment_adherence=core.treatment_adherence("B_auto", codemap_use_compliance=False, contaminated=False),
        )

        assert core.result_eligibility(record, _synthetic_policies()) is True

    def test_pairing_rejects_non_boolean_treatment_adherence(self) -> None:
        """An untyped telemetry value cannot be interpreted as observed adherence."""
        records = [_record(arm="A_plain", treatment_adherence=1), _record(arm="B_auto", treatment_adherence=True)]

        assert core.result_eligibility(records[0], _synthetic_policies()) is False
        with pytest.raises(ValueError, match="treatment_adherence must be a boolean"):
            core.pair_effects(
                records,
                baseline_arm="A_plain",
                treatment_arm="B_auto",
                policies=_synthetic_policies(),
            )

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"scoreable": False}, id="unscoreable"),
            pytest.param({"self_consistency": True}, id="self-consistency"),
            pytest.param({"approximate": True}, id="approximate-oracle"),
            pytest.param({"static_reference": True}, id="static-reference"),
            pytest.param({"incomplete": True}, id="incomplete"),
            pytest.param({"extraction_failed": True}, id="extraction-failed"),
            pytest.param({"contaminated": True}, id="contaminated"),
            pytest.param({"diagnostic_only": True}, id="diagnostic-only-semantic-score"),
        ],
    )
    def test_result_eligibility_rejects_excluded_result_class(self, overrides: dict[str, Any]) -> None:
        """Each labelled diagnostic/failure state stays out of the headline pair population."""
        assert core.result_eligibility(_record(**overrides), _synthetic_policies()) is False

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(_record(task_id="unknown"), id="unknown-task"),
            pytest.param(_record(revision="wrong-revision"), id="wrong-revision"),
        ],
    )
    def test_result_eligibility_rejects_unknown_policy_or_revision(self, record: Any) -> None:
        """An omitted or mismatched policy coordinate cannot become headline-eligible by default."""
        with pytest.raises(ValueError):
            core.result_eligibility(record, _synthetic_policies())


class TestPairedEffects:
    """Pair construction is repetition-preserving and block-local."""

    @pytest.mark.parametrize(
        ("baseline_arm", "treatment_arm"),
        [
            pytest.param("A_plain", "C_strict", id="skill-vs-plain"),
            pytest.param("A_plain", "B_auto", id="optional-vs-plain"),
        ],
    )
    def test_pair_effects_over_no_records_reports_no_effects(self, baseline_arm: str, treatment_arm: str) -> None:
        """A valid estimand with nothing measured yields no effects rather than a zero effect.

        Both providers now run the same three arms, so naming a pair no longer implies it was run. The empty result is
        what keeps an unmeasured comparison out of a table instead of entering it as a difference of zero.
        """
        assert (
            core.pair_effects(
                [],
                baseline_arm=baseline_arm,
                treatment_arm=treatment_arm,
                policies=_synthetic_policies(),
            )
            == []
        )

    @pytest.mark.parametrize(
        ("baseline_arm", "treatment_arm"),
        [
            pytest.param("A_plain", "B_auto", id="direct-cli-vs-plain"),
            pytest.param("A_plain", "C_strict", id="skill-vs-plain"),
            pytest.param("B_auto", "C_strict", id="skill-vs-direct-cli"),
        ],
    )
    def test_pair_effects_accepts_each_codex_estimand(self, baseline_arm: str, treatment_arm: str) -> None:
        """Codex's direct and Skill interventions remain distinct valid comparisons."""
        effects = core.pair_effects(
            [
                _record(arm=baseline_arm, provider="codex", input_tokens=100, quality_score=0.6),
                _record(arm=treatment_arm, provider="codex", input_tokens=50, quality_score=0.8),
            ],
            baseline_arm=baseline_arm,
            treatment_arm=treatment_arm,
            policies=_synthetic_policies(),
        )

        assert len(effects) == 1
        assert effects[0].provider == "codex"
        assert effects[0].baseline_arm == baseline_arm
        assert effects[0].treatment_arm == treatment_arm

    def test_pair_effects_preserves_repetitions_and_computes_effects(self) -> None:
        """Each task repetition produces its own log token ratio and quality delta."""
        records = [
            _record(arm="A_plain", repetition=1, input_tokens=100, quality_score=0.6),
            _record(arm="C_strict", repetition=1, input_tokens=50, quality_score=0.8),
            _record(arm="A_plain", repetition=2, input_tokens=80, quality_score=1.0),
            _record(arm="C_strict", repetition=2, input_tokens=160, quality_score=0.5),
        ]

        effects = core.pair_effects(
            records,
            baseline_arm="A_plain",
            treatment_arm="C_strict",
            policies=_synthetic_policies(),
        )
        by_repetition = {effect.repetition: effect for effect in effects}

        assert set(by_repetition) == {1, 2}
        assert by_repetition[1].provider == "claude"
        assert by_repetition[1].model == "test-model"
        assert by_repetition[1].revision == EXPERIMENT_REVISION
        assert by_repetition[1].task_id == "FN-02"
        assert by_repetition[1].log_input_token_ratio == pytest.approx(math.log(0.5))
        assert by_repetition[1].quality_delta == pytest.approx(0.2)
        assert by_repetition[2].log_input_token_ratio == pytest.approx(math.log(2.0))
        assert by_repetition[2].quality_delta == pytest.approx(-0.5)

    def test_pair_effects_keeps_provider_blocks_separate(self) -> None:
        """Equivalent coordinates stay in separate provider-valid treatment comparisons."""
        claude_records = [
            _record(arm="A_plain", provider="claude"),
            _record(arm="B_auto", provider="claude", input_tokens=50, quality_score=0.7),
        ]
        codex_records = [
            _record(arm="A_plain", provider="codex"),
            _record(arm="B_auto", provider="codex", input_tokens=200, quality_score=0.4),
        ]

        claude_effect = core.pair_effects(
            claude_records,
            baseline_arm="A_plain",
            treatment_arm="B_auto",
            policies=_synthetic_policies(),
        )[0]
        codex_effect = core.pair_effects(
            codex_records,
            baseline_arm="A_plain",
            treatment_arm="B_auto",
            policies=_synthetic_policies(),
        )[0]

        assert claude_effect.provider == "claude"
        assert claude_effect.log_input_token_ratio == pytest.approx(math.log(0.5))
        assert claude_effect.quality_delta == pytest.approx(0.1)
        assert codex_effect.provider == "codex"
        assert codex_effect.log_input_token_ratio == pytest.approx(math.log(2.0))
        assert codex_effect.quality_delta == pytest.approx(-0.2)

    @pytest.mark.parametrize(
        ("cached_input_tokens", "token_accounting_inconsistent", "fresh_input_tokens", "message"),
        [
            pytest.param(101, False, None, "flag disagrees", id="raw-inconsistency-cannot-be-hidden"),
            pytest.param(80, True, None, "flag disagrees", id="stale-true-flag-is-rejected"),
            pytest.param(80, False, 19, "fresh_input_tokens disagrees", id="stale-derived-fresh-is-rejected"),
            pytest.param(
                101, True, None, "token accounting is inconsistent", id="explicit-inconsistent-record-is-rejected"
            ),
        ],
    )
    def test_pair_effects_rejects_inconsistent_token_accounting(
        self,
        cached_input_tokens: int,
        token_accounting_inconsistent: bool,
        fresh_input_tokens: int | None,
        message: str,
    ) -> None:
        """Raw token counts, the explicit flag, and any derived fresh value must agree."""
        records = [
            _record(
                input_tokens=100,
                cached_input_tokens=cached_input_tokens,
                token_accounting_inconsistent=token_accounting_inconsistent,
                fresh_input_tokens=fresh_input_tokens,
            ),
            _record(arm="B_auto", input_tokens=50, cached_input_tokens=0),
        ]

        with pytest.raises(ValueError, match=message):
            core.pair_effects(
                records,
                baseline_arm="A_plain",
                treatment_arm="B_auto",
                policies=_synthetic_policies(),
            )

    @pytest.mark.parametrize(
        "records",
        [
            pytest.param([_record(arm="A_plain")], id="missing-treatment-arm"),
            pytest.param(
                [_record(arm="A_plain"), _record(arm="B_auto", provider="codex")],
                id="cross-provider-arms-cannot-pair",
            ),
            pytest.param(
                [_record(arm="A_plain"), _record(arm="A_plain", input_tokens=90), _record(arm="B_auto")],
                id="duplicate-baseline-cell",
            ),
        ],
    )
    def test_pair_effects_rejects_missing_or_duplicate_cells(self, records: list[Any]) -> None:
        """Missing arms and duplicate arm cells must fail instead of being silently discarded."""
        with pytest.raises(ValueError):
            core.pair_effects(
                records,
                baseline_arm="A_plain",
                treatment_arm="B_auto",
                policies=_synthetic_policies(),
            )

    def test_pair_effects_requires_explicit_policy_mapping(self) -> None:
        """Pairing without B0 policy provenance is rejected instead of inferring eligibility."""
        records = [_record(arm="A_plain"), _record(arm="B_auto")]

        with pytest.raises(TypeError):
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto")

    def test_pair_effects_rejects_policy_ineligible_task_with_clean_record_flags(self) -> None:
        """Diagnostic task IDs remain excluded even when every ResultRecord flag uses its default."""
        policies = core.load_task_policies(MANIFEST_PATH)
        records = [_record(arm="A_plain", task_id="RV-05"), _record(arm="B_auto", task_id="RV-05")]

        with pytest.raises(ValueError, match="ineligible"):
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto", policies=policies)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"input_tokens": 0}, id="zero-input-tokens"),
            pytest.param({"quality_score": True}, id="bool-quality-score"),
            pytest.param({"quality_score": float("nan")}, id="nan-quality-score"),
        ],
    )
    def test_pair_effects_rejects_invalid_numeric_record_values(self, overrides: dict[str, Any]) -> None:
        """Invalid token and quality values cannot produce undefined or misleading effects."""
        records = [_record(arm="A_plain", **overrides), _record(arm="B_auto")]

        with pytest.raises(ValueError):
            core.pair_effects(
                records,
                baseline_arm="A_plain",
                treatment_arm="B_auto",
                policies=_synthetic_policies(),
            )


class TestAgenticAnswerContracts:
    """The shared agentic oracle scores explicit fields without provider heuristics."""

    def test_oracle_scores_production_and_test_importers_with_stable_ranking(self, tmp_path: Path) -> None:
        """AST truth excludes tests, self-imports, dynamic imports, and broken files.

        A response that omits an expected-empty field or reverses a tied ranking must lose its component instead of
        receiving credit from prose recall.
        """
        package = tmp_path / "pkg"
        tests = tmp_path / "tests"
        package.mkdir()
        tests.mkdir()
        for path, source in {
            package / "__init__.py": "",
            package / "target.py": "import pkg.target\n",
            package / "alpha.py": "import pkg.target\n",
            package / "beta.py": "from pkg import target\n",
            package / "consumer.py": "import pkg.alpha\n",
            package / "dynamic.py": "import importlib\nimportlib.import_module('pkg.target')\n",
            package / "broken.py": "import pkg.target\nnot valid python\n",
            tests / "test_target.py": "import pkg.target\n",
        }.items():
            path.write_text(source, encoding="utf-8")

        task = {
            "id": "T-01",
            "primary_module": "pkg.target",
            "answer_contract": {
                "fields": [
                    "production_importers",
                    "test_importer_count",
                    "rdep_counts",
                    "ranking",
                    "cross_namespace_importers",
                    "high_centrality",
                ],
                "params": {
                    "ranking": {"candidate_set": "production_importers", "top_k": 2},
                    "cross_namespace_importers": {"prefix": "other"},
                    "high_centrality": {"min_rdep_count": 1},
                },
            },
        }

        oracle = agentic_contracts.build_oracle(task, tmp_path)
        score = agentic_contracts.score_answer(
            oracle,
            {
                "production_importers": ["pkg.alpha", "pkg.beta"],
                "test_importer_count": 1,
                "rdep_counts": {"pkg.alpha": 1, "pkg.beta": 0},
                "ranking": ["pkg.alpha", "pkg.beta"],
                "cross_namespace_importers": [],
                "high_centrality": {"pkg.alpha": 1},
            },
            exposure_text="pkg.alpha pkg.beta",
            report_text="pkg.alpha",
            tool_calls=2,
        )

        assert oracle.expected["production_importers"] == ("pkg.alpha", "pkg.beta")
        assert oracle.expected["test_importer_count"] == 1
        assert oracle.expected["rdep_counts"] == {"pkg.alpha": 1, "pkg.beta": 0}
        assert oracle.expected["high_centrality"] == {"pkg.alpha": 1}
        assert score.components == {
            "production_importers": 1.0,
            "test_importer_count": 1.0,
            "rdep_counts": 1.0,
            "ranking": 1.0,
            "cross_namespace_importers": 1.0,
            "high_centrality": 1.0,
        }
        assert score.quality_score == pytest.approx(1.0)
        assert score.correct is True
        assert score.erec == pytest.approx(1.0)
        assert score.rrec == pytest.approx(0.5)
        assert score.deff == pytest.approx(1.0)

        incomplete = agentic_contracts.score_answer(
            oracle,
            {
                "production_importers": ["pkg.alpha", "pkg.beta"],
                "test_importer_count": 1,
                "rdep_counts": {"pkg.alpha": 1, "pkg.beta": 0},
                "ranking": ["pkg.beta", "pkg.alpha"],
            },
        )

        assert incomplete.components["ranking"] == 0.0
        assert incomplete.components["cross_namespace_importers"] == 0.0
        assert incomplete.quality_score == pytest.approx(0.5)

    def test_answer_contract_rejects_unbounded_or_unknown_field_parameters(self) -> None:
        """A task cannot add evaluator behavior through an unreviewed parameter."""
        task = {
            "id": "T-02",
            "primary_module": "pkg.target",
            "answer_contract": {
                "fields": ["ranking"],
                "params": {"ranking": {"candidate_set": "production_importers", "top_k": 0}},
            },
        }

        with pytest.raises(ValueError, match="top_k"):
            agentic_contracts.validate_answer_contract(task)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(
                '{"production_importers": ["pkg.alpha"], "rdep_counts": {"pkg.alpha": 2}, "ranking": ["pkg.alpha"], "production_importer_count": 1, "isolation_verdict": "isolated"}',
                {
                    "production_importers": ["pkg.alpha"],
                    "rdep_counts": {"pkg.alpha": 2},
                    "ranking": ["pkg.alpha"],
                    "production_importer_count": 1,
                    "isolation_verdict": "isolated",
                },
                id="set-mapping-ranking-scalar-category",
            ),
            pytest.param(
                '{"production_importers": "EMPTY", "rdep_counts": "EMPTY", "ranking": "EMPTY", "production_importer_count": 0, "isolation_verdict": "isolated"}',
                {
                    "production_importers": [],
                    "rdep_counts": {},
                    "ranking": [],
                    "production_importer_count": 0,
                    "isolation_verdict": "isolated",
                },
                id="explicit-empty",
            ),
        ],
    )
    def test_parse_labeled_answer_requires_exact_labels_and_preserves_declared_shapes(
        self, payload: str, expected: dict[str, Any]
    ) -> None:
        """The shared parser accepts all canonical answer shapes without provider-specific extraction."""
        task = {
            "id": "T-03",
            "primary_module": "pkg.target",
            "answer_contract": {
                "fields": [
                    "production_importers",
                    "rdep_counts",
                    "ranking",
                    "production_importer_count",
                    "isolation_verdict",
                ],
                "params": {"ranking": {"candidate_set": "production_importers", "top_k": 1}},
            },
        }

        parsed = agentic_contracts.parse_labeled_answer(task, f"notes\nBEGIN_ANSWER_JSON\n{payload}\nEND_ANSWER_JSON\n")

        assert parsed == expected

    @pytest.mark.parametrize(
        ("text", "answer", "diagnostic_only", "error"),
        [
            pytest.param(
                'BEGIN_ANSWER_JSON\n{"production_importers": ["pkg.caller"]}\nEND_ANSWER_JSON',
                {"production_importers": ["pkg.caller"]},
                False,
                None,
                id="strict-envelope",
            ),
            pytest.param(
                '{"production_importers": ["pkg.caller"]}',
                {"production_importers": ["pkg.caller"]},
                True,
                "BEGIN_ANSWER_JSON",
                id="unique-bare-json-diagnostic-recovery",
            ),
            pytest.param(
                '{"production_importers": ["pkg.first"]}\n{"production_importers": ["pkg.second"]}',
                None,
                False,
                "exactly one bare JSON object",
                id="ambiguous-bare-json-rejected",
            ),
            pytest.param(
                '{"production_importers": ["pkg.caller"]',
                None,
                False,
                "bare JSON is invalid",
                id="malformed-bare-json-rejected",
            ),
        ],
    )
    def test_assess_answer_response_keeps_strict_validity_separate_from_diagnostic_recovery(
        self, text: str, answer: dict[str, Any] | None, diagnostic_only: bool, error: str | None
    ) -> None:
        """Only one bare JSON object may supply a clearly ineligible diagnostic semantic answer."""
        task = {
            "id": "T-diagnostic",
            "primary_module": "pkg.target",
            "answer_contract": {"fields": ["production_importers"], "params": {}},
        }

        assessment = agentic_contracts.assess_answer_response(task, text)

        assert assessment.answer == answer
        assert assessment.strict_envelope_valid is (not diagnostic_only and answer is not None)
        assert assessment.diagnostic_only is diagnostic_only
        assert assessment.pooling_eligible is (not diagnostic_only and answer is not None)
        if error is None:
            assert assessment.error is None
        else:
            assert error in assessment.error

    def test_bare_json_recovery_preserves_raw_evidence_metrics_without_pooling_eligibility(self) -> None:
        """A missing envelope cannot turn observed importer mentions into zero recall."""
        task = {
            "id": "T-evidence",
            "primary_module": "pkg.target",
            "answer_contract": {"fields": ["production_importers"], "params": {}},
        }
        oracle = agentic_contracts.AgenticOracle(
            task_id="T-evidence",
            fields=("production_importers",),
            expected={"production_importers": ("pkg.alpha", "pkg.beta")},
        )

        assessment = agentic_contracts.assess_answer_response(
            task,
            '{"production_importers": ["pkg.alpha", "pkg.beta"]}',
        )
        evidence = agentic_contracts.score_evidence_metrics(
            oracle,
            exposure_text="pkg.alpha pkg.beta",
            report_text="pkg.alpha pkg.beta",
            tool_calls=1,
        )

        assert assessment.diagnostic_only is True
        assert assessment.pooling_eligible is False
        assert evidence.erec == pytest.approx(1.0)
        assert evidence.rrec == pytest.approx(1.0)
        assert evidence.deff == pytest.approx(2.0)

    @pytest.mark.parametrize("wrapper", ["```json\n{payload}\n```", "```\n{payload}\n```"])
    def test_parse_labeled_answer_strips_cosmetic_markdown_fence(self, wrapper: str) -> None:
        """A markdown code fence inside the envelope is cosmetic, not a scoring failure.

        Scenario: some providers habitually fence JSON inside BEGIN/END_ANSWER_JSON;
        the fenced payload must parse identically to the bare payload (previously it
        was rejected as invalid JSON, zeroing erec/rrec on format alone).
        """
        task = {
            "id": "T-03b",
            "primary_module": "pkg.target",
            "answer_contract": {"fields": ["production_importers"], "params": {}},
        }
        payload = wrapper.format(payload='{"production_importers": ["pkg.caller"]}')

        parsed = agentic_contracts.parse_labeled_answer(task, f"notes\nBEGIN_ANSWER_JSON\n{payload}\nEND_ANSWER_JSON\n")

        assert parsed == {"production_importers": ["pkg.caller"]}

    def test_parse_labeled_answer_rejects_unclosed_markdown_fence(self) -> None:
        """A cosmetic fence must close before its enclosed payload is accepted."""
        task = {
            "id": "T-03c",
            "primary_module": "pkg.target",
            "answer_contract": {"fields": ["production_importers"], "params": {}},
        }

        with pytest.raises(ValueError, match="markdown code fence"):
            agentic_contracts.parse_labeled_answer(
                task,
                'BEGIN_ANSWER_JSON\n```json\n{"production_importers": ["pkg.caller"]}\nEND_ANSWER_JSON',
            )

    def test_parse_labeled_answer_rejects_missing_contract_label(self) -> None:
        """A partial answer cannot be converted into accidental component credit."""
        task = {
            "id": "T-04",
            "primary_module": "pkg.target",
            "answer_contract": {
                "fields": ["production_importers", "production_importer_count"],
                "params": {},
            },
        }

        with pytest.raises(ValueError, match="missing"):
            agentic_contracts.parse_labeled_answer(
                task,
                'BEGIN_ANSWER_JSON\n{"production_importers": []}\nEND_ANSWER_JSON',
            )

    def test_answer_contract_changes_the_shared_delivered_prompt_and_hash(self) -> None:
        """Both providers must hash the same labelled-answer instruction bytes."""
        task = {
            "id": "T-05",
            "prompt": "Inspect pkg.target.",
            "primary_module": "pkg.target",
            "answer_contract": {"fields": ["production_importers"], "params": {}},
        }

        delivered = agentic_contracts.materialize_agentic_prompt(task)

        assert delivered == (
            "Inspect pkg.target.\n\n"
            "Return one JSON object containing exactly these labels: production_importers.\n"
            "Use exactly these JSON value shapes:\n"
            "- production_importers: array of full dotted module-name strings.\n"
            f"{agentic_contracts.IMPORT_CONVENTION_INSTRUCTION}\n"
            f"{agentic_contracts.ORACLE_POPULATION_INSTRUCTION}\n"
            "Run temporary analysis in memory or through interpreter stdin; do not create helper files in the "
            "repository or tool coordination/lock directories. Writable coordination storage is reserved for the "
            "tool protocol, not scratch work. Use deterministic code for counts, set filtering and ranking; "
            "check any reported count against the corresponding complete collection.\n"
            "Do not put objects or counts inside array fields. Values outside these shapes are invalid.\n"
            "Wrap your answer between a BEGIN_ANSWER_JSON line and an END_ANSWER_JSON line, "
            "exactly once, with the JSON object alone between them.\n"
            "Example using synthetic values only:\n"
            "BEGIN_ANSWER_JSON\n"
            '{"production_importers":["pkg.consumer"]}\n'
            "END_ANSWER_JSON"
        )
        assert core.prompt_hash(task) == hashlib.sha256(delivered.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                {
                    "production_importers": ["pkg.consumer"],
                    "ranking": [{"module": "pkg.consumer", "rdep_count": 2}],
                    "buckets": {"public": ["pkg.consumer"], "internal": []},
                    "high_centrality": {"pkg.consumer": 2},
                },
                id="ranking-object",
            ),
            pytest.param(
                {
                    "production_importers": ["pkg.consumer"],
                    "ranking": ["pkg.consumer"],
                    "buckets": {"pkg.consumer": "public"},
                    "high_centrality": {"pkg.consumer": 2},
                },
                id="inverted-buckets",
            ),
            pytest.param(
                {
                    "production_importers": ["pkg.consumer"],
                    "ranking": ["pkg.consumer"],
                    "buckets": {"public": ["pkg.consumer"], "internal": []},
                    "high_centrality": [{"module": "pkg.consumer", "rdep_count": 2}],
                },
                id="high-centrality-object-list",
            ),
        ],
    )
    def test_parse_labeled_answer_rejects_shapes_not_advertised_to_models(self, payload: dict[str, Any]) -> None:
        """Model-friendly rich shapes cannot be silently accepted and scored as zero."""
        task = {
            "id": "T-06",
            "primary_module": "pkg.target",
            "answer_contract": {
                "fields": ["production_importers", "ranking", "buckets", "high_centrality"],
                "params": {
                    "ranking": {"candidate_set": "production_importers", "top_k": 1},
                    "buckets": {"labels": ["public", "internal"]},
                    "high_centrality": {"min_rdep_count": 1},
                },
            },
        }

        with pytest.raises(ValueError, match="shape"):
            agentic_contracts.parse_labeled_answer(
                task,
                f"BEGIN_ANSWER_JSON\n{json.dumps(payload)}\nEND_ANSWER_JSON",
            )

    def test_ba04_requires_the_exact_deduplicated_affected_count(self) -> None:
        """BA-04 cannot permit approximation while its scalar scorer requires equality."""
        tasks = json.loads((Path(__file__).parents[1] / "suites" / "tasks-agentic.json").read_text(encoding="utf-8"))[
            "tasks"
        ]
        prompt = next(task["prompt"] for task in tasks if task["id"] == "BA-04")

        assert "may approximate" not in prompt
        assert "exact total number of unique modules affected" in prompt

    def test_risk_tier_is_derived_from_locked_counts_not_audited_metadata(self, tmp_path: Path) -> None:
        """BA-16 risk must follow its declared thresholds even when stale metadata disagrees."""
        for relative, source in {
            "pkg/target.py": "",
            "pkg/alpha.py": "import pkg.target\n",
            "pkg/beta.py": "import pkg.target\n",
            "pkg/consumer.py": "import pkg.alpha\n",
        }.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        task = {
            "id": "BA-16-synthetic",
            "primary_module": "pkg.target",
            "audited_risk_tier": "low",
            "answer_contract": {
                "fields": ["production_importers", "high_centrality", "risk_tier"],
                "params": {
                    "high_centrality": {"min_rdep_count": 1},
                    "risk_tier": {
                        "critical_min_production_importer_count": 2,
                        "critical_min_high_centrality_count": 1,
                    },
                },
            },
        }

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        assert oracle.expected["high_centrality"] == {"pkg.alpha": 1}
        assert oracle.expected["risk_tier"] == "critical"

    def test_ba03_declares_the_prefix_bucket_taxonomy_used_by_its_oracle(self, tmp_path: Path) -> None:
        """Trainer-core excludes core and loop modules; callbacks remain a separate prefix bucket."""
        for relative, source in {
            "lightning/pytorch/utilities/model_helpers.py": "",
            "lightning/pytorch/trainer/fit_loop.py": "import lightning.pytorch.utilities.model_helpers\n",
            "lightning/pytorch/callbacks/early_stopping.py": "import lightning.pytorch.utilities.model_helpers\n",
            "lightning/pytorch/core/module.py": "import lightning.pytorch.utilities.model_helpers\n",
        }.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        task = {
            "id": "BA-03-synthetic",
            "primary_module": "lightning.pytorch.utilities.model_helpers",
            "answer_contract": {
                "fields": ["buckets"],
                "params": {"buckets": {"labels": ["trainer-core", "callbacks", "everything-else"]}},
            },
        }
        prompt = next(
            item["prompt"]
            for item in json.loads(
                (Path(__file__).parents[1] / "suites" / "tasks-agentic.json").read_text(encoding="utf-8")
            )["tasks"]
            if item["id"] == "BA-03"
        )

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        assert "only names beginning with `lightning.pytorch.trainer.`" in prompt
        assert oracle.expected["buckets"] == {
            "trainer-core": ("lightning.pytorch.trainer.fit_loop",),
            "callbacks": ("lightning.pytorch.callbacks.early_stopping",),
            "everything-else": ("lightning.pytorch.core.module",),
        }

    @pytest.mark.parametrize(
        ("relative", "expected_bucket"),
        [
            pytest.param("lightning/pytorch/trainer/fit_loop.py", "trainer-core", id="trainer-descendant"),
            pytest.param("lightning/pytorch/trainerfoo/fit_loop.py", "everything-else", id="trainer-impostor"),
            pytest.param("lightning/pytorch/callbacks/early_stopping.py", "callbacks", id="callbacks-descendant"),
            pytest.param(
                "lightning/pytorch/callbacks_extra/early_stopping.py",
                "everything-else",
                id="callbacks-impostor",
            ),
        ],
    )
    def test_ba03_bucket_prefixes_require_a_dotted_segment_boundary(
        self, tmp_path: Path, relative: str, expected_bucket: str
    ) -> None:
        """BA-03 accepts namespace descendants without admitting adjacent near-prefix names."""
        primary = tmp_path / "lightning/pytorch/utilities/model_helpers.py"
        primary.parent.mkdir(parents=True)
        primary.write_text("", encoding="utf-8")
        importer = tmp_path / relative
        importer.parent.mkdir(parents=True)
        importer.write_text("import lightning.pytorch.utilities.model_helpers\n", encoding="utf-8")
        task = {
            "id": "BA-03-prefix-boundary",
            "primary_module": "lightning.pytorch.utilities.model_helpers",
            "answer_contract": {
                "fields": ["buckets"],
                "params": {"buckets": {"labels": ["trainer-core", "callbacks", "everything-else"]}},
            },
        }
        module_name = relative.removesuffix(".py").replace("/", ".")

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        expected = {label: () for label in ("trainer-core", "callbacks", "everything-else")}
        expected[expected_bucket] = (module_name,)
        assert oracle.expected["buckets"] == expected

    def test_ba05_declares_package_public_and_examples_internal_boundaries(self, tmp_path: Path) -> None:
        """Package initializers are public; non-package and example importers are internal."""
        # Every package directory carries its own __init__.py: module names come from the __init__.py chain, so a tree
        # that skips the intermediate ones names this module `callbacks.finetuning` and no longer describes a package
        # importable as `lightning.pytorch.callbacks.finetuning`.
        for relative, source in {
            "lightning/__init__.py": "",
            "lightning/pytorch/__init__.py": "",
            "lightning/pytorch/callbacks/finetuning.py": "",
            "lightning/pytorch/callbacks/__init__.py": "import lightning.pytorch.callbacks.finetuning\n",
            "lightning/pytorch/callbacks/consumer.py": "import lightning.pytorch.callbacks.finetuning\n",
            "examples/finetuning_demo.py": "import lightning.pytorch.callbacks.finetuning\n",
        }.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        task = {
            "id": "BA-05-synthetic",
            "primary_module": "lightning.pytorch.callbacks.finetuning",
            "answer_contract": {
                "fields": ["buckets"],
                "params": {"buckets": {"labels": ["public", "internal"]}},
            },
        }
        prompt = next(
            item["prompt"]
            for item in json.loads(
                (Path(__file__).parents[1] / "suites" / "tasks-agentic.json").read_text(encoding="utf-8")
            )["tasks"]
            if item["id"] == "BA-05"
        )

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        assert "package initializer (`__init__.py`)" in prompt
        assert "including `examples.*`" in prompt
        assert oracle.expected["buckets"] == {
            "public": ("lightning.pytorch.callbacks",),
            "internal": ("examples.finetuning_demo", "lightning.pytorch.callbacks.consumer"),
        }

    def test_affected_count_uses_the_exact_deduplicated_second_wave(self, tmp_path: Path) -> None:
        """Overlapping second-wave importers count once and an estimate loses only its scalar component."""
        for relative, source in {
            "pkg/__init__.py": "",
            "pkg/target.py": "",
            "pkg/alpha.py": "import pkg.target\n",
            "pkg/beta.py": "import pkg.target\n",
            "pkg/consumer_one.py": "import pkg.alpha\nimport pkg.beta\n",
            "pkg/consumer_two.py": "import pkg.alpha\n",
        }.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        task = {
            "id": "BA-04-synthetic",
            "primary_module": "pkg.target",
            "answer_contract": {
                "fields": ["production_importers", "high_centrality", "affected_module_count"],
                "params": {
                    "high_centrality": {"min_rdep_count": 2},
                    "affected_module_count": {"min_rdep_count": 2},
                },
            },
        }
        oracle = agentic_contracts.build_oracle(task, tmp_path)
        exact = {
            "production_importers": ["pkg.alpha", "pkg.beta"],
            "high_centrality": {"pkg.alpha": 2},
            "affected_module_count": 4,
        }

        exact_score = agentic_contracts.score_answer(oracle, exact)
        estimated_score = agentic_contracts.score_answer(oracle, {**exact, "affected_module_count": 3})

        assert oracle.expected["affected_module_count"] == 4
        assert exact_score.correct is True
        assert estimated_score.components["affected_module_count"] == 0.0
        assert estimated_score.quality_score == pytest.approx(2 / 3)

    @pytest.mark.parametrize(
        ("relative_path", "expected_test"),
        [
            pytest.param("tests/test_a.py", True, id="top-level-tests"),
            pytest.param("tests_pytorch/test_a.py", True, id="pytorch-tests-root"),
            pytest.param("tests_fabric/test_a.py", True, id="fabric-tests-root"),
            pytest.param("pkg/tests/test_a.py", True, id="nested-tests"),
            pytest.param("pkg/contest.py", False, id="production-substring"),
        ],
    )
    def test_ast_oracle_test_detection_and_src_module_names(
        self, tmp_path: Path, relative_path: str, expected_test: bool
    ) -> None:
        """Test roots do not contaminate production truth and ``src`` is not a namespace segment."""
        source = tmp_path / "src" / relative_path
        source.parent.mkdir(parents=True)
        source.write_text("", encoding="utf-8")

        modules = agentic_contracts._scan_modules(tmp_path)

        module = next(iter(modules.values()))
        assert module.name != "src." + module.name.removeprefix("src.")
        assert module.is_test is expected_test

    def test_ast_oracle_resolves_relative_imports_from_the_importer_package(self, tmp_path: Path) -> None:
        """Single- and double-dot imports retain their local static dependency targets."""
        for relative_path, source in {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/target.py": "",
            "pkg/sub/client.py": "from . import target\n",
            "pkg/sub/child/__init__.py": "",
            "pkg/sub/child/client.py": "from .. import target\n",
        }.items():
            path = tmp_path / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")

        modules = agentic_contracts._scan_modules(tmp_path)

        assert "pkg.sub.target" in modules["pkg.sub.client"].imports
        assert "pkg.sub.target" in modules["pkg.sub.child.client"].imports


def test_answer_instruction_states_the_envelope_requirement_outside_the_example() -> None:
    """The wrapping requirement must be an instruction, not an inference from a synthetic example.

    Codex returned bare JSON in 12 of 48 agentic cells on 2026-09-06 because the delimiters appeared only inside a block
    labelled "Example using synthetic values only".
    """
    task = {
        "id": "T-06",
        "prompt": "Inspect pkg.target.",
        "primary_module": "pkg.target",
        "answer_contract": {"fields": ["production_importers"], "params": {}},
    }

    instruction = agentic_contracts.answer_format_instruction(task)
    before_example = instruction.split("Example using synthetic values only:")[0]

    assert "BEGIN_ANSWER_JSON" in before_example
    assert "END_ANSWER_JSON" in before_example
