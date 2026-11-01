"""Acceptance contract for the provider-neutral parity contract library.

These tests intentionally define the small B1 surface shared by future provider adapters: task identity, arm contracts,
task policies, evaluator dispatch, result eligibility, paired effects, and the agentic answer contract. They do not
exercise a provider CLI or execute a model.
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

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "provider-parity-methodology.json"
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
AGENTIC_SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-agentic.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
EXPERIMENT_REVISION = MANIFEST["experiment_revision"]
BUCKET_LABELS = ["trainer-core", "callbacks", "everything-else"]


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
    for suite in MANIFEST["suites"]:
        for task in suite["tasks"]:
            if task["id"] == task_id:
                return task
    raise AssertionError(f"manifest task {task_id!r} was not found")


def _agentic_task(
    task_id: str,
    fields: list[str],
    params: dict[str, Any] | None = None,
    *,
    primary_module: str = "pkg.target",
    **extra: Any,
) -> dict[str, Any]:
    """Build an agentic task whose answer contract lists *fields* with optional per-field *params*.

    >>> _agentic_task("T-00", ["ranking"])["answer_contract"]
    {'fields': ['ranking'], 'params': {}}
    """
    contract = {"fields": fields, "params": params or {}}
    return {"id": task_id, "primary_module": primary_module, "answer_contract": contract, **extra}


def _write_tree(root: Path, files: dict[str, str]) -> None:
    """Write each ``relative path -> source`` entry under *root*, creating parent directories.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     _write_tree(Path(directory), {"pkg/a.py": "x = 1\\n"})
    ...     (Path(directory) / "pkg" / "a.py").read_text(encoding="utf-8")
    'x = 1\\n'
    """
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _effort_orders(effort: str) -> list[Any]:
    """Return the arm order of twelve tasks for one reasoning effort.

    >>> len(_effort_orders("high"))
    12
    """
    return [
        core.deterministic_arm_order(
            "effort-aware-revision", "claude", "gpt-5.6-luna", f"T-{index}", 1, reasoning_effort=effort
        )
        for index in range(12)
    ]


@pytest.fixture(name="policies", scope="module")
def _policies() -> Any:
    """Load the immutable task-policy mapping from the committed methodology manifest (read only)."""
    return core.load_task_policies(MANIFEST_PATH)


@pytest.fixture(name="agentic_prompts", scope="module")
def _agentic_prompts() -> dict[str, str]:
    """Map every committed agentic task identifier to its prompt text (read only)."""
    tasks = json.loads(AGENTIC_SUITE_PATH.read_text(encoding="utf-8"))["tasks"]
    return {task["id"]: task["prompt"] for task in tasks}


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
                {"repo": {"name": "fixture"}, "tasks": [{"id": "O-01", "prompt": "object task", "scoreable": False}]},
                [{"id": "O-01", "prompt": "object task", "scoreable": False}],
                id="object-wrapper",
            ),
        ],
    )
    def test_valid_shape_preserves_raw_task_mapping(
        self, tmp_path: Path, payload: list[dict[str, Any]] | dict[str, Any], expected: list[dict[str, Any]]
    ) -> None:
        """A list or wrapper loads exactly its raw task mappings without inferred fields.

        Both accepted file shapes must return the tasks untouched, including nested custom fields.
        """
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
    def test_rejects_invalid_contract(self, tmp_path: Path, payload: object, error: str) -> None:
        """Malformed task files fail instead of changing suite membership or identity.

        Each case breaks one rule (shape, required field, emptiness, uniqueness) and must raise naming that rule.
        """
        suite_path = tmp_path / "invalid-suite.json"
        suite_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match=error):
            core.load_task_suite(suite_path)


class TestCanonicalTaskIdentity:
    """Task and prompt identity must match the tracked methodology byte for byte."""

    def test_task_and_prompt_hashes_match_the_locked_manifest(self) -> None:
        """FN-02 has the manifest's raw-object hash and the hash of its exact delivered prompt.

        The locked hashes are what lets a result prove it ran the committed task and not an edited one.
        """
        suite = core.load_task_suite(SUITE_PATH)
        task = next(task for task in suite if task["id"] == "FN-02")
        locked = _manifest_task("FN-02")

        assert core.canonical_task_bytes(task) == json.dumps(task, sort_keys=True).encode("utf-8")
        assert core.canonical_task_hash(task) == locked["canonical_task_sha256"]
        assert core.prompt_hash(task) == locked["prompt_sha256"]
        assert core.prompt_hash(task) == hashlib.sha256(core.materialize_task_prompt(task).encode("utf-8")).hexdigest()

    def test_task_hash_includes_raw_fields_without_normalization(self) -> None:
        """Adding a raw field changes identity, preventing inferred-field substitution.

        A task with ``scoreable`` added must hash differently, even though the field has a default.
        """
        task = {"id": "T-01", "prompt": "Inspect this.", "custom": {"order": 1}}
        altered_task = {**task, "scoreable": False}

        assert core.canonical_task_hash(task) != core.canonical_task_hash(altered_task)

    def test_suite_hash_tracks_ordered_tasks_not_root_wrapper_metadata(self) -> None:
        """Suite identity ignores transport metadata but detects task content and ordering drift.

        A copied list hashes the same; reversing the tasks or editing one prompt changes the hash.
        """
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
        """Every provider adapter receives the same 600-second parity timeout.

        One constant, not a per-provider value, keeps cells comparable across providers.
        """
        assert core.PARITY_TIMEOUT_SECONDS == 600

    def test_arm_names_match_the_manifest(self) -> None:
        """The current methodology names exactly the arms the library defines contracts for.

        An arm present in one place and not the other would leave cells with no instructions or no preregistration.
        """
        assert set(core.ARM_CONTRACTS) == set(MANIFEST["preregistered_cells"]["arms"])

    @pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
    def test_contract_hash_matches_contract_text(self, arm: str) -> None:
        """Each arm's recorded digest is the SHA-256 of its exact instruction text.

        The digest is what results cite as the arm contract they ran under.
        """
        expected = core.ARM_CONTRACTS[arm]

        assert expected["contract_sha256"] == hashlib.sha256(expected["contract"].encode("utf-8")).hexdigest()

    def test_arm_contracts_are_immutable(self) -> None:
        """The arm-contract registry rejects assignment.

        Arm instructions are locked; a mutable registry would let one test or adapter change what every run was told.
        """
        with pytest.raises(TypeError):
            core.ARM_CONTRACTS["A_plain"]["contract"] = "changed"

    def test_comparison_arm_registry_shares_one_arm_set_and_reads_pre_alignment_names(self) -> None:
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

    def test_comparison_arm_registry_is_immutable(self) -> None:
        """The per-provider arm registry rejects assignment.

        The arm set decides which comparisons are valid, so it must not be editable at run time.
        """
        with pytest.raises(TypeError):
            core.COMPARISON_ARMS_BY_PROVIDER["codex"] = frozenset()

    def test_arm_contract_lookup_leaves_the_task_prompt_untouched(self) -> None:
        """Reading every arm contract changes neither the task prompt nor its canonical bytes.

        Arm metadata stays separate so every provider receives identical task bytes.
        """
        task = {"id": "T-02", "prompt": "Keep these exact bytes: café."}
        original_prompt = task["prompt"]
        original_bytes = core.canonical_task_bytes(task)

        _ = [core.ARM_CONTRACTS[arm]["contract"] for arm in core.ARM_CONTRACTS]

        assert task["prompt"] == original_prompt
        assert core.canonical_task_bytes(task) == original_bytes

    def test_arm_order_covers_every_arm(self) -> None:
        """One block's arm order is a complete ordering of the defined arms.

        Dropping or repeating an arm would silently remove a cell from the randomized block.
        """
        order = core.deterministic_arm_order("arm-order-fixed-oracle", "claude", "gpt-5.6-luna", "FN-02", 1)

        assert set(order) == set(core.ARM_CONTRACTS)

    def test_arm_order_changes_with_experiment_revision(self) -> None:
        """Across several revision variants at least one produces a different order than the base revision.

        The order is seeded by experiment identity, so a new revision must not replay the old randomization. Eight
        variants make an unchanged order for all of them vanishingly unlikely.
        """
        fixed = core.deterministic_arm_order("arm-order-fixed-oracle", "claude", "gpt-5.6-luna", "FN-02", 1)
        variants = {
            core.deterministic_arm_order(
                f"arm-order-fixed-oracle:variant-{index}", "claude", "gpt-5.6-luna", "FN-02", 1
            )
            for index in range(8)
        }

        assert any(order != fixed for order in variants)

    def test_arm_order_rejects_non_positive_repetition(self) -> None:
        """Repetition zero is not a valid block coordinate.

        Repetitions are numbered from one; accepting zero would create an unaddressable block.
        """
        with pytest.raises(ValueError, match="repetition"):
            core.deterministic_arm_order("arm-order-fixed-oracle", "claude", "model", "FN-02", 0)

    @pytest.mark.parametrize("provider", ["claude", "codex"])
    def test_arm_order_returns_the_named_provider_own_arms(self, provider: str) -> None:
        """The shared ordering is provider-keyed and returns exactly that provider's arm set.

        Ordering is not Claude-only: Codex gets its own registry entry through the same function.
        """
        order = core.deterministic_arm_order("arm-order-fixed-oracle", provider, "gpt-5.6-luna", "FN-02", 1)

        assert set(order) == core.COMPARISON_ARMS_BY_PROVIDER[provider]

    def test_arm_order_rejects_unknown_provider(self) -> None:
        """An unregistered provider is refused instead of receiving a default arm set.

        A typo'd provider name must not silently run with another provider's arms.
        """
        with pytest.raises(ValueError, match="unknown provider"):
            core.deterministic_arm_order("arm-order-fixed-oracle", "gemini", "gpt-5.6-luna", "FN-02", 1)

    def test_arm_order_includes_reasoning_effort_in_the_model_stratum(self) -> None:
        """Effort drift changes the randomized block identity while the same effort replays identically.

        Twelve tasks ordered at high effort repeat exactly, and at least one differs from the medium-effort order.
        """
        high = _effort_orders("high")

        assert high == _effort_orders("high")
        assert any(left != right for left, right in zip(high, _effort_orders("medium")))

    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            pytest.param(
                {"type": "develop_blast_radius", "ground_truth": {"fn_callers": [str(i) for i in range(20)]}},
                ("direct_reverse_call", "high_fan_in"),
                id="direct-callers-high-fan-in",
            ),
            pytest.param(
                {"type": "graph_fn_blast", "ground_truth": {"blast_callers": ["a", "b"]}},
                ("transitive_reverse_call",),
                id="transitive-callers",
            ),
            pytest.param(
                {"type": "graph_path", "ground_truth": {"import_path": ["a", "b", "c"]}},
                ("dependency_path",),
                id="dependency-path",
            ),
            pytest.param(
                {"type": "diff_impact", "ground_truth": {"fn_callers": ["a"], "test_modules": ["test_a"]}},
                ("diff_impact", "test_selection"),
                id="diff-impact-with-tests",
            ),
        ],
    )
    def test_capability_strata_expose_where_codemap_can_help(
        self, task: dict[str, Any], expected: tuple[str, ...]
    ) -> None:
        """Each task shape maps to its named structural-capability strata.

        The strata are how results are grouped by what kind of help Codemap could give.
        """
        assert core.capability_strata(task) == expected

    def test_active_manifest_locks_split_profiles_and_provider_adapters(self) -> None:
        """The active manifest describes the exact tested Codex permission and adapter boundary.

        Plain runs get a read-only profile with the locked index parent denied; treatment runs may write only their own
        index directory; neither has network access or inherited secrets.
        """
        profiles = MANIFEST["codex_permission_profiles"]

        assert MANIFEST["experiment_revision"] == EXPERIMENT_REVISION
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
        assert MANIFEST["execution_controls"]["codex_transport"] == (
            "run-codex-structural.py and run-codex-agentic.py provider adapters"
        )


class TestTaskPolicies:
    """The tracked methodology manifest is the sole source of headline eligibility policy."""

    def test_load_matches_every_unique_manifest_task_and_revision(self, policies: Any) -> None:
        """Every manifest row becomes exactly one policy carrying its experiment revision and classification.

        Policy fields are compared to the manifest row for each task, so a dropped, extra, or mislabelled task fails.
        """
        expected_rows = [task for suite in MANIFEST["suites"] for task in suite["tasks"]]

        observed = {
            task_id: (p.experiment_revision, p.task_id, p.oracle_class, p.headline_eligible_v1, p.scoreable)
            for task_id, p in policies.items()
        }

        assert len(policies) == len(expected_rows)
        assert observed == {
            task["id"]: (
                MANIFEST["experiment_revision"],
                task["id"],
                task["oracle_class"],
                task["headline_eligible_v1"],
                task["effective_scoreable"],
            )
            for task in expected_rows
        }

    def test_policy_mapping_rejects_assignment(self, policies: Any) -> None:
        """The loaded policy mapping is read only.

        A writable mapping would let a test or adapter grant headline eligibility to a task.
        """
        with pytest.raises(TypeError):
            policies["FN-02"] = policies["FN-02"]

    def test_policy_rejects_attribute_assignment(self, policies: Any) -> None:
        """A single policy is immutable.

        Flipping ``scoreable`` on a shared policy object would change the pairing population for every later caller.
        """
        with pytest.raises(AttributeError):
            policies["FN-02"].scoreable = False

    def test_classifies_se01_as_static_reference_and_ri05_as_unscoreable(self, policies: Any) -> None:
        """The manifest marks SE-01 a static reference and RI-05 unscoreable.

        These two facts are what keeps the approved diagnostic and unscoreable tasks out of headline pairing.
        """
        assert policies["SE-01"].oracle_class == "static_reference"
        assert policies["RI-05"].scoreable is False

    @pytest.mark.parametrize("task_id", ["SE-01", "RV-05", "CQ-02", "CQ-03", "CQ-04", "CQ-05", "RI-05"])
    def test_known_diagnostic_tasks_are_ineligible_for_headline_pairing(self, policies: Any, task_id: str) -> None:
        """Policy, not optional record flags, blocks approved diagnostic and unscoreable tasks.

        The record is otherwise clean (default flags), so only the manifest policy can make it ineligible.
        """
        assert core.result_eligibility(_record(task_id=task_id), policies) is False


class TestEvaluatorRegistry:
    """Scoring dispatch is explicit, provider-neutral, and fail-closed."""

    def test_scoreable_task_uses_the_registered_evaluator_without_provider(self) -> None:
        """Identical adapter-labelled calls receive an identical shared score.

        The evaluator is looked up by task type only, so Claude and Codex calls with the same output get one score.
        """
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

    def test_unscoreable_task_bypasses_an_unknown_evaluator(self) -> None:
        """A deliberately unscoreable task remains diagnostic without registry dispatch.

        An unknown type is harmless when the task opts out of scoring, so no evaluator lookup is attempted.
        """
        registry = core.EvaluatorRegistry({})

        result = registry.evaluate({"id": "U-01", "type": "unknown", "scoreable": False}, "answer")

        assert result == core.EvaluationResult(scored=False, correct=False, quality_score=None)

    def test_unknown_scoreable_task_rejects_methodology_drift(self) -> None:
        """A new scoreable type cannot silently become an unscored result.

        If the suite gains a scoreable type with no registered evaluator, scoring fails loudly.
        """
        registry = core.EvaluatorRegistry({})

        with pytest.raises(ValueError, match="unknown"):
            registry.evaluate({"id": "U-02", "type": "unknown", "scoreable": True}, "answer")


class TestResultEligibility:
    """Headline pairing excludes preregistered diagnostic and failed-result classes."""

    def test_result_record_requires_explicit_treatment_adherence(self) -> None:
        """Pairing cannot infer adherence from the arm name or a permissive default.

        Building a record without ``treatment_adherence`` raises, so adherence must always be observed and passed.
        """
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

    def test_requires_explicit_policy_mapping(self) -> None:
        """Eligibility without policy provenance cannot infer a headline result.

        Omitting the policies argument raises rather than assuming every task is eligible.
        """
        with pytest.raises(TypeError):
            core.result_eligibility(_record())

    def test_accepts_clean_independent_score(self) -> None:
        """A complete independent scoreable result is eligible for paired headline metrics.

        This is the baseline that every exclusion case below departs from.
        """
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
        """Every arm needs observed adherence; required-use arms receive no special permissiveness.

        One arm of each pair is marked non-adherent: that record is ineligible and the pair cannot be built.
        """
        records = [
            _record(provider=provider, arm=baseline_arm, treatment_adherence=baseline_arm != invalid_arm),
            _record(provider=provider, arm=treatment_arm, treatment_adherence=treatment_arm != invalid_arm),
        ]
        invalid_record = next(record for record in records if record.arm == invalid_arm)

        assert core.result_eligibility(invalid_record, _synthetic_policies()) is False
        with pytest.raises(ValueError, match="ineligible"):
            core.pair_effects(
                records, baseline_arm=baseline_arm, treatment_arm=treatment_arm, policies=_synthetic_policies()
            )

    def test_zero_query_codex_b_cell_stays_pooling_eligible(self) -> None:
        """A Codex B cell that made no Codemap query is still eligible for pooling.

        Adherence alone is useless if the row is still dropped from pooling, so the whole path from observed adherence
        to eligibility is exercised.
        """
        record = _record(
            provider="codex",
            arm="B_auto",
            treatment_adherence=core.treatment_adherence("B_auto", codemap_use_compliance=False, contaminated=False),
        )

        assert core.result_eligibility(record, _synthetic_policies()) is True

    def test_non_boolean_treatment_adherence_is_ineligible(self) -> None:
        """An untyped adherence telemetry value is not treated as observed adherence.

        The integer 1 is truthy but not ``True``, so the record is ineligible instead of counting as adherent.
        """
        record = _record(arm="A_plain", treatment_adherence=1)

        assert core.result_eligibility(record, _synthetic_policies()) is False

    def test_pairing_rejects_non_boolean_treatment_adherence(self) -> None:
        """Pairing refuses a record whose adherence is not a boolean, naming the field.

        An untyped telemetry value cannot be interpreted as observed adherence.
        """
        records = [_record(arm="A_plain", treatment_adherence=1), _record(arm="B_auto", treatment_adherence=True)]

        with pytest.raises(ValueError, match="treatment_adherence must be a boolean"):
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto", policies=_synthetic_policies())

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
    def test_rejects_excluded_result_class(self, overrides: dict[str, Any]) -> None:
        """Each labelled diagnostic or failure state stays out of the headline pair population.

        Each case sets one exclusion flag on an otherwise eligible record.
        """
        assert core.result_eligibility(_record(**overrides), _synthetic_policies()) is False

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(_record(task_id="unknown"), id="unknown-task"),
            pytest.param(_record(revision="wrong-revision"), id="wrong-revision"),
        ],
    )
    def test_rejects_unknown_policy_or_revision(self, record: Any) -> None:
        """An omitted or mismatched policy coordinate cannot become headline-eligible by default.

        A task with no locked policy, or a record from another experiment revision, raises instead of passing.
        """
        with pytest.raises(ValueError, match=r"no locked task policy|does not match task policy"):
            core.result_eligibility(record, _synthetic_policies())


class TestPairEffects:
    """Pair construction is repetition-preserving and block-local."""

    @pytest.mark.parametrize(
        ("baseline_arm", "treatment_arm"),
        [
            pytest.param("A_plain", "C_strict", id="skill-vs-plain"),
            pytest.param("A_plain", "B_auto", id="optional-vs-plain"),
        ],
    )
    def test_over_no_records_reports_no_effects(self, baseline_arm: str, treatment_arm: str) -> None:
        """A valid estimand with nothing measured yields no effects rather than a zero effect.

        Both providers now run the same three arms, so naming a pair no longer implies it was run. The empty result is
        what keeps an unmeasured comparison out of a table instead of entering it as a difference of zero.
        """
        effects = core.pair_effects(
            [], baseline_arm=baseline_arm, treatment_arm=treatment_arm, policies=_synthetic_policies()
        )

        assert effects == []

    @pytest.mark.parametrize(
        ("baseline_arm", "treatment_arm"),
        [
            pytest.param("A_plain", "B_auto", id="direct-cli-vs-plain"),
            pytest.param("A_plain", "C_strict", id="skill-vs-plain"),
            pytest.param("B_auto", "C_strict", id="skill-vs-direct-cli"),
        ],
    )
    def test_accepts_each_codex_estimand(self, baseline_arm: str, treatment_arm: str) -> None:
        """Codex's direct and Skill interventions remain distinct valid comparisons.

        Each of the three arm pairs produces exactly one effect carrying the pair's arms and the provider.
        """
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

    def test_preserves_repetitions_and_computes_effects(self) -> None:
        """Each task repetition produces its own log token ratio and quality delta.

        Repetition 1 halves the tokens and gains 0.2 quality; repetition 2 doubles the tokens and loses 0.5.
        """
        records = [
            _record(arm="A_plain", repetition=1, input_tokens=100, quality_score=0.6),
            _record(arm="C_strict", repetition=1, input_tokens=50, quality_score=0.8),
            _record(arm="A_plain", repetition=2, input_tokens=80, quality_score=1.0),
            _record(arm="C_strict", repetition=2, input_tokens=160, quality_score=0.5),
        ]

        effects = core.pair_effects(
            records, baseline_arm="A_plain", treatment_arm="C_strict", policies=_synthetic_policies()
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

    def test_keeps_provider_blocks_separate(self) -> None:
        """Equivalent coordinates stay in separate provider-valid treatment comparisons.

        Claude and Codex records share task, model and repetition; each provider pairs only with itself.
        """
        claude_records = [
            _record(arm="A_plain", provider="claude"),
            _record(arm="B_auto", provider="claude", input_tokens=50, quality_score=0.7),
        ]
        codex_records = [
            _record(arm="A_plain", provider="codex"),
            _record(arm="B_auto", provider="codex", input_tokens=200, quality_score=0.4),
        ]

        claude_effect = core.pair_effects(
            claude_records, baseline_arm="A_plain", treatment_arm="B_auto", policies=_synthetic_policies()
        )[0]
        codex_effect = core.pair_effects(
            codex_records, baseline_arm="A_plain", treatment_arm="B_auto", policies=_synthetic_policies()
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
    def test_rejects_inconsistent_token_accounting(
        self,
        cached_input_tokens: int,
        token_accounting_inconsistent: bool,
        fresh_input_tokens: int | None,
        message: str,
    ) -> None:
        """Raw token counts, the explicit flag, and any derived fresh value must agree.

        A cache larger than gross input, a flag that disagrees with the counts, and a stale derived fresh count each
        make the record untrustworthy for pairing.
        """
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
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto", policies=_synthetic_policies())

    @pytest.mark.parametrize(
        "records",
        [
            pytest.param([_record(arm="A_plain")], id="missing-treatment-arm"),
            pytest.param(
                [_record(arm="A_plain"), _record(arm="B_auto", provider="codex")], id="cross-provider-arms-cannot-pair"
            ),
            pytest.param(
                [_record(arm="A_plain"), _record(arm="A_plain", input_tokens=90), _record(arm="B_auto")],
                id="duplicate-baseline-cell",
            ),
        ],
    )
    def test_rejects_missing_or_duplicate_cells(self, records: list[Any]) -> None:
        """Missing arms and duplicate arm cells fail instead of being silently discarded.

        A treatment with no baseline partner, a pair split across providers, and two baseline cells all break pairing.
        """
        with pytest.raises(ValueError, match=r"missing paired cell|duplicate A_plain cell"):
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto", policies=_synthetic_policies())

    def test_requires_explicit_policy_mapping(self) -> None:
        """Pairing without policy provenance is rejected instead of inferring eligibility.

        Omitting the policies argument raises rather than treating every record as eligible.
        """
        records = [_record(arm="A_plain"), _record(arm="B_auto")]

        with pytest.raises(TypeError):
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto")

    def test_rejects_policy_ineligible_task_with_clean_record_flags(self, policies: Any) -> None:
        """Diagnostic task IDs remain excluded even when every ResultRecord flag uses its default.

        RV-05 is a diagnostic task in the manifest, so its clean-looking records still cannot be paired.
        """
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
    def test_rejects_invalid_numeric_record_values(self, overrides: dict[str, Any]) -> None:
        """Invalid token and quality values cannot produce undefined or misleading effects.

        Zero tokens would divide by zero, a boolean is not a quality score, and NaN is not a finite value in [0, 1].
        """
        records = [_record(arm="A_plain", **overrides), _record(arm="B_auto")]

        with pytest.raises(ValueError, match=r"positive integer|finite value in \[0, 1\]"):
            core.pair_effects(records, baseline_arm="A_plain", treatment_arm="B_auto", policies=_synthetic_policies())


class TestAgenticAnswerContracts:
    """The shared agentic oracle scores explicit fields without provider heuristics."""

    @pytest.fixture(name="ranking_oracle")
    def _ranking_oracle(self, tmp_path: Path) -> Any:
        """Build the oracle for a package whose importers include tests, self-imports, dynamic and broken files."""
        _write_tree(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/target.py": "import pkg.target\n",
                "pkg/alpha.py": "import pkg.target\n",
                "pkg/beta.py": "from pkg import target\n",
                "pkg/consumer.py": "import pkg.alpha\n",
                "pkg/dynamic.py": "import importlib\nimportlib.import_module('pkg.target')\n",
                "pkg/broken.py": "import pkg.target\nnot valid python\n",
                "tests/test_target.py": "import pkg.target\n",
            },
        )
        task = _agentic_task(
            "T-01",
            [
                "production_importers",
                "test_importer_count",
                "rdep_counts",
                "ranking",
                "cross_namespace_importers",
                "high_centrality",
            ],
            {
                "ranking": {"candidate_set": "production_importers", "top_k": 2},
                "cross_namespace_importers": {"prefix": "other"},
                "high_centrality": {"min_rdep_count": 1},
            },
        )
        return agentic_contracts.build_oracle(task, tmp_path)

    def test_oracle_excludes_tests_self_dynamic_and_broken_importers(self, ranking_oracle: Any) -> None:
        """AST truth counts only production static importers and keeps the test importer separate.

        The self-import, the dynamic ``import_module`` call, the file with a syntax error, and the test file are not
        production importers; the test file is counted only in ``test_importer_count``.
        """
        assert ranking_oracle.expected["production_importers"] == ("pkg.alpha", "pkg.beta")
        assert ranking_oracle.expected["test_importer_count"] == 1
        assert ranking_oracle.expected["rdep_counts"] == {"pkg.alpha": 1, "pkg.beta": 0}
        assert ranking_oracle.expected["high_centrality"] == {"pkg.alpha": 1}

    def test_complete_answer_earns_full_credit(self, ranking_oracle: Any) -> None:
        """An answer matching every field scores 1.0 per component, with evidence metrics from the exposure text.

        Exposure and report text feed the evidence metrics: both importers were exposed, one was reported, and two
        tool calls were made.
        """
        score = agentic_contracts.score_answer(
            ranking_oracle,
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

    def test_omitted_field_and_reversed_ranking_lose_their_components(self, ranking_oracle: Any) -> None:
        """A response that omits an expected-empty field or reverses a tied ranking loses that component.

        Credit never comes from prose recall: the missing ``cross_namespace_importers`` and the reversed ``ranking``
        each score 0, leaving half the quality.
        """
        incomplete = agentic_contracts.score_answer(
            ranking_oracle,
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

    def test_rejects_unbounded_or_unknown_field_parameters(self) -> None:
        """A task cannot add evaluator behavior through an unreviewed parameter.

        A ranking with ``top_k`` of zero is out of bounds and the contract validation refuses it.
        """
        task = _agentic_task("T-02", ["ranking"], {"ranking": {"candidate_set": "production_importers", "top_k": 0}})

        with pytest.raises(ValueError, match="top_k"):
            agentic_contracts.validate_answer_contract(task)

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(
                {
                    "production_importers": ["pkg.alpha"],
                    "rdep_counts": {"pkg.alpha": 2},
                    "ranking": ["pkg.alpha"],
                    "production_importer_count": 1,
                    "isolation_verdict": "isolated",
                },
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
                {
                    "production_importers": "EMPTY",
                    "rdep_counts": "EMPTY",
                    "ranking": "EMPTY",
                    "production_importer_count": 0,
                    "isolation_verdict": "isolated",
                },
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
    def test_parse_labeled_answer_preserves_declared_shapes(
        self, payload: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        """The shared parser accepts all canonical answer shapes without provider-specific extraction.

        Set, mapping, ranking, scalar and category fields round-trip unchanged, and the explicit ``EMPTY`` marker
        becomes the empty value of each field's declared shape.
        """
        task = _agentic_task(
            "T-03",
            ["production_importers", "rdep_counts", "ranking", "production_importer_count", "isolation_verdict"],
            {"ranking": {"candidate_set": "production_importers", "top_k": 1}},
        )

        parsed = agentic_contracts.parse_labeled_answer(
            task, f"notes\nBEGIN_ANSWER_JSON\n{json.dumps(payload)}\nEND_ANSWER_JSON\n"
        )

        assert parsed == expected

    @pytest.mark.parametrize(
        ("text", "answer", "diagnostic_only", "error"),
        [
            pytest.param(
                'BEGIN_ANSWER_JSON\n{"production_importers": ["pkg.caller"]}\nEND_ANSWER_JSON',
                {"production_importers": ["pkg.caller"]},
                False,
                "",
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
    def test_assess_answer_response_separates_strict_validity_from_diagnostic_recovery(
        self, text: str, answer: dict[str, Any] | None, diagnostic_only: bool, error: str
    ) -> None:
        """Only one bare JSON object may supply a clearly ineligible diagnostic semantic answer.

        A strict envelope is pooling-eligible with no error; a lone bare JSON object is recovered but diagnostic only;
        ambiguous or malformed bare JSON is rejected with a message naming the problem (``error`` is empty when no error
        is expected).
        """
        task = _agentic_task("T-diagnostic", ["production_importers"])

        assessment = agentic_contracts.assess_answer_response(task, text)

        assert assessment.answer == answer
        assert assessment.strict_envelope_valid is (not diagnostic_only and answer is not None)
        assert assessment.diagnostic_only is diagnostic_only
        assert assessment.pooling_eligible is (not diagnostic_only and answer is not None)
        assert (assessment.error is None) is (error == "")
        assert error in (assessment.error or "")

    def test_bare_json_recovery_keeps_evidence_metrics_without_pooling_eligibility(self) -> None:
        """A missing envelope cannot turn observed importer mentions into zero recall.

        The recovered answer is diagnostic only and not poolable, but the evidence metrics still reflect that both
        expected importers were exposed and reported.
        """
        task = _agentic_task("T-evidence", ["production_importers"])
        oracle = agentic_contracts.AgenticOracle(
            task_id="T-evidence",
            fields=("production_importers",),
            expected={"production_importers": ("pkg.alpha", "pkg.beta")},
        )

        assessment = agentic_contracts.assess_answer_response(
            task, '{"production_importers": ["pkg.alpha", "pkg.beta"]}'
        )
        evidence = agentic_contracts.score_evidence_metrics(
            oracle, exposure_text="pkg.alpha pkg.beta", report_text="pkg.alpha pkg.beta", tool_calls=1
        )

        assert assessment.diagnostic_only is True
        assert assessment.pooling_eligible is False
        assert evidence.erec == pytest.approx(1.0)
        assert evidence.rrec == pytest.approx(1.0)
        assert evidence.deff == pytest.approx(2.0)

    @pytest.mark.parametrize(
        "wrapper",
        [
            pytest.param("```json\n{payload}\n```", id="json-fence"),
            pytest.param("```\n{payload}\n```", id="plain-fence"),
        ],
    )
    def test_parse_labeled_answer_strips_cosmetic_markdown_fence(self, wrapper: str) -> None:
        """A markdown code fence inside the envelope is cosmetic, not a scoring failure.

        Some providers habitually fence JSON inside BEGIN/END_ANSWER_JSON; the fenced payload must parse identically to
        the bare payload (previously it was rejected as invalid JSON, zeroing erec/rrec on format alone).
        """
        task = _agentic_task("T-03b", ["production_importers"])
        payload = wrapper.format(payload='{"production_importers": ["pkg.caller"]}')

        parsed = agentic_contracts.parse_labeled_answer(task, f"notes\nBEGIN_ANSWER_JSON\n{payload}\nEND_ANSWER_JSON\n")

        assert parsed == {"production_importers": ["pkg.caller"]}

    @pytest.mark.parametrize(
        ("task", "answer", "match"),
        [
            pytest.param(
                _agentic_task("T-03c", ["production_importers"]),
                'BEGIN_ANSWER_JSON\n```json\n{"production_importers": ["pkg.caller"]}\nEND_ANSWER_JSON',
                "markdown code fence",
                id="unclosed-markdown-fence",
            ),
            pytest.param(
                _agentic_task("T-04", ["production_importers", "production_importer_count"]),
                'BEGIN_ANSWER_JSON\n{"production_importers": []}\nEND_ANSWER_JSON',
                "missing",
                id="missing-contract-label",
            ),
        ],
    )
    def test_parse_labeled_answer_rejects_malformed_envelopes(
        self, task: dict[str, Any], answer: str, match: str
    ) -> None:
        """A cosmetic fence must close before its payload is accepted, and a partial answer earns no component credit.

        An unclosed markdown fence is rejected before its enclosed payload is accepted, and a partial answer cannot be
        converted into accidental component credit.
        """
        with pytest.raises(ValueError, match=match):
            agentic_contracts.parse_labeled_answer(task, answer)

    def test_answer_contract_changes_the_delivered_prompt_and_hash(self) -> None:
        """Both providers hash the same labelled-answer instruction bytes.

        The delivered prompt is the task prompt plus the exact answer-format instructions, and the prompt hash is the
        digest of that full text.
        """
        task = _agentic_task("T-05", ["production_importers"], prompt="Inspect pkg.target.")

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

    def test_answer_instruction_states_the_envelope_requirement_outside_the_example(self) -> None:
        """The wrapping requirement is an instruction, not an inference from a synthetic example.

        Codex returned bare JSON in 12 of 48 agentic cells on 2026-09-06 because the delimiters appeared only inside a
        block labelled "Example using synthetic values only".
        """
        task = _agentic_task("T-06", ["production_importers"], prompt="Inspect pkg.target.")

        instruction = agentic_contracts.answer_format_instruction(task)
        before_example = instruction.split("Example using synthetic values only:")[0]

        assert "BEGIN_ANSWER_JSON" in before_example
        assert "END_ANSWER_JSON" in before_example

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
        """Model-friendly rich shapes cannot be silently accepted and scored as zero.

        A ranking of objects, buckets keyed by module, and a list of centrality objects are all outside the advertised
        shapes and must be refused.
        """
        task = _agentic_task(
            "T-06",
            ["production_importers", "ranking", "buckets", "high_centrality"],
            {
                "ranking": {"candidate_set": "production_importers", "top_k": 1},
                "buckets": {"labels": ["public", "internal"]},
                "high_centrality": {"min_rdep_count": 1},
            },
        )

        with pytest.raises(ValueError, match="shape"):
            agentic_contracts.parse_labeled_answer(task, f"BEGIN_ANSWER_JSON\n{json.dumps(payload)}\nEND_ANSWER_JSON")

    def test_ba04_prompt_requires_the_exact_deduplicated_affected_count(self, agentic_prompts: dict[str, str]) -> None:
        """BA-04 cannot permit approximation while its scalar scorer requires equality.

        The committed prompt must ask for the exact unique count and must not allow an estimate.
        """
        prompt = agentic_prompts["BA-04"]

        assert "may approximate" not in prompt
        assert "exact total number of unique modules affected" in prompt

    def test_risk_tier_is_derived_from_locked_counts_not_audited_metadata(self, tmp_path: Path) -> None:
        """BA-16 risk follows its declared thresholds even when stale metadata disagrees.

        The task carries an ``audited_risk_tier`` of low, but two production importers and one high-centrality module
        meet the critical thresholds, so the oracle must say critical.
        """
        _write_tree(
            tmp_path,
            {
                "pkg/target.py": "",
                "pkg/alpha.py": "import pkg.target\n",
                "pkg/beta.py": "import pkg.target\n",
                "pkg/consumer.py": "import pkg.alpha\n",
            },
        )
        task = _agentic_task(
            "BA-16-synthetic",
            ["production_importers", "high_centrality", "risk_tier"],
            {
                "high_centrality": {"min_rdep_count": 1},
                "risk_tier": {"critical_min_production_importer_count": 2, "critical_min_high_centrality_count": 1},
            },
            audited_risk_tier="low",
        )

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        assert oracle.expected["high_centrality"] == {"pkg.alpha": 1}
        assert oracle.expected["risk_tier"] == "critical"

    def test_ba03_declares_the_prefix_bucket_taxonomy_used_by_its_oracle(
        self, tmp_path: Path, agentic_prompts: dict[str, str]
    ) -> None:
        """Trainer-core excludes core and loop modules; callbacks remain a separate prefix bucket.

        The committed BA-03 prompt names the ``lightning.pytorch.trainer.`` prefix, and the oracle must bucket one
        importer under each of the three labels accordingly.
        """
        _write_tree(
            tmp_path,
            {
                "lightning/pytorch/utilities/model_helpers.py": "",
                "lightning/pytorch/trainer/fit_loop.py": "import lightning.pytorch.utilities.model_helpers\n",
                "lightning/pytorch/callbacks/early_stopping.py": "import lightning.pytorch.utilities.model_helpers\n",
                "lightning/pytorch/core/module.py": "import lightning.pytorch.utilities.model_helpers\n",
            },
        )
        task = _agentic_task(
            "BA-03-synthetic",
            ["buckets"],
            {"buckets": {"labels": BUCKET_LABELS}},
            primary_module="lightning.pytorch.utilities.model_helpers",
        )

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        assert "only names beginning with `lightning.pytorch.trainer.`" in agentic_prompts["BA-03"]
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
                "lightning/pytorch/callbacks_extra/early_stopping.py", "everything-else", id="callbacks-impostor"
            ),
        ],
    )
    def test_ba03_bucket_prefixes_require_a_dotted_segment_boundary(
        self, tmp_path: Path, relative: str, expected_bucket: str
    ) -> None:
        """BA-03 accepts namespace descendants without admitting adjacent near-prefix names.

        ``trainerfoo`` and ``callbacks_extra`` merely start with the prefix text; they are not descendants and fall in
        the catch-all bucket.
        """
        _write_tree(
            tmp_path,
            {
                "lightning/pytorch/utilities/model_helpers.py": "",
                relative: "import lightning.pytorch.utilities.model_helpers\n",
            },
        )
        task = _agentic_task(
            "BA-03-prefix-boundary",
            ["buckets"],
            {"buckets": {"labels": BUCKET_LABELS}},
            primary_module="lightning.pytorch.utilities.model_helpers",
        )
        module_name = relative.removesuffix(".py").replace("/", ".")

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        expected = {label: () for label in BUCKET_LABELS}
        expected[expected_bucket] = (module_name,)
        assert oracle.expected["buckets"] == expected

    def test_ba05_declares_package_public_and_examples_internal_boundaries(
        self, tmp_path: Path, agentic_prompts: dict[str, str]
    ) -> None:
        """Package initializers are public; non-package and example importers are internal.

        Every package directory carries its own ``__init__.py``: module names come from the ``__init__.py`` chain, so a
        tree that skips the intermediate ones names this module ``callbacks.finetuning`` and no longer describes a
        package importable as ``lightning.pytorch.callbacks.finetuning``.
        """
        _write_tree(
            tmp_path,
            {
                "lightning/__init__.py": "",
                "lightning/pytorch/__init__.py": "",
                "lightning/pytorch/callbacks/finetuning.py": "",
                "lightning/pytorch/callbacks/__init__.py": "import lightning.pytorch.callbacks.finetuning\n",
                "lightning/pytorch/callbacks/consumer.py": "import lightning.pytorch.callbacks.finetuning\n",
                "examples/finetuning_demo.py": "import lightning.pytorch.callbacks.finetuning\n",
            },
        )
        task = _agentic_task(
            "BA-05-synthetic",
            ["buckets"],
            {"buckets": {"labels": ["public", "internal"]}},
            primary_module="lightning.pytorch.callbacks.finetuning",
        )

        oracle = agentic_contracts.build_oracle(task, tmp_path)

        assert "package initializer (`__init__.py`)" in agentic_prompts["BA-05"]
        assert "including `examples.*`" in agentic_prompts["BA-05"]
        assert oracle.expected["buckets"] == {
            "public": ("lightning.pytorch.callbacks",),
            "internal": ("examples.finetuning_demo", "lightning.pytorch.callbacks.consumer"),
        }

    def test_affected_count_uses_the_exact_deduplicated_second_wave(self, tmp_path: Path) -> None:
        """Overlapping second-wave importers count once and an estimate loses only its scalar component.

        Two consumers import overlapping first-wave modules; the unique affected total is 4, and answering 3 loses the
        affected-count component only.
        """
        _write_tree(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/target.py": "",
                "pkg/alpha.py": "import pkg.target\n",
                "pkg/beta.py": "import pkg.target\n",
                "pkg/consumer_one.py": "import pkg.alpha\nimport pkg.beta\n",
                "pkg/consumer_two.py": "import pkg.alpha\n",
            },
        )
        task = _agentic_task(
            "BA-04-synthetic",
            ["production_importers", "high_centrality", "affected_module_count"],
            {"high_centrality": {"min_rdep_count": 2}, "affected_module_count": {"min_rdep_count": 2}},
        )
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
        """Test roots do not contaminate production truth and ``src`` is not a namespace segment.

        A file under a tests root is a test module; a production file merely containing ``test`` in its name is not. The
        ``src/`` source root is stripped from the module name.
        """
        _write_tree(tmp_path, {f"src/{relative_path}": ""})

        modules = agentic_contracts._scan_modules(tmp_path)

        module = next(iter(modules.values()))
        assert not module.name.startswith("src.")
        assert module.is_test is expected_test

    def test_ast_oracle_resolves_relative_imports_from_the_importer_package(self, tmp_path: Path) -> None:
        """Single- and double-dot imports retain their local static dependency targets.

        ``from . import target`` and ``from .. import target`` in sibling and child packages must both resolve to the
        same absolute module.
        """
        _write_tree(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/target.py": "",
                "pkg/sub/client.py": "from . import target\n",
                "pkg/sub/child/__init__.py": "",
                "pkg/sub/child/client.py": "from .. import target\n",
            },
        )

        modules = agentic_contracts._scan_modules(tmp_path)

        assert "pkg.sub.target" in modules["pkg.sub.client"].imports
        assert "pkg.sub.target" in modules["pkg.sub.child.client"].imports
