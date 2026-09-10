"""Acceptance contract for the agentic answer oracle.

Each test pins one failure mode the audit found in the oracle itself: a scored answer being marked wrong because the
oracle, not the model, resolved imports differently; free evidence credit from substring containment; and the emitted
prompt withholding the convention it scores against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchmarks._bench_common import agentic_contracts


_AGENTIC_TASK_IDS = (
    "BA-01",
    "BA-02",
    "BA-03",
    "BA-04",
    "BA-05",
    "BA-06",
    "BA-07",
    "BA-08",
    "BA-09",
    "BA-10",
    "BA-11",
    "BA-12",
    "BA-13",
    "BA-14",
    "BA-15",
    "BA-16",
    "BA-17",
    "BA-18",
    "BA-19",
    "BA-20",
)


@pytest.fixture(name="agentic_tasks", scope="module")
def _agentic_tasks() -> list[dict[str, Any]]:
    """Load the shared agentic suite once for read-only prompt checks."""
    suite_path = Path(__file__).parents[1] / "suites" / "tasks-agentic.json"
    return json.loads(suite_path.read_text(encoding="utf-8"))["tasks"]


def _task(**overrides: Any) -> dict[str, Any]:
    """Build a production-importer task, replacing defaults with explicit overrides.

    >>> task = _task(primary_module="example.core")
    >>> task["primary_module"], task["answer_contract"]
    ('example.core', {'fields': ['production_importers']})
    """
    task = {
        "id": "BA-TEST",
        "primary_module": "pkg.trainer.trainer",
        "answer_contract": {"fields": ["production_importers"]},
    }
    task.update(overrides)
    return task


def _write(root: Path, relative: str, source: str) -> None:
    """Create missing parents and replace a synthetic module's UTF-8 source.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory)
    ...     _write(root, "pkg/example.py", "VALUE = 3")
    ...     (root / "pkg/example.py").read_text(encoding="utf-8")
    'VALUE = 3'
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.fixture(name="mixed_import_tree")
def _mixed_import_tree(tmp_path: Path) -> Path:
    """Source tree whose only importer uses one mixed ``from`` statement.

    ``utilities`` is a package holding a real ``parsing`` submodule, so the importer's statement resolves one alias
    concretely and one not at all. The module under test (``pkg.trainer.trainer``) is reached only through the package
    import that statement also carries.

    Example:
        >>> root = getfixture("mixed_import_tree")
        >>> (root / "pkg/utilities/parsing.py").is_file(), (root / "pkg/trainer/trainer.py").is_file()
        (True, True)
    """
    root = tmp_path / "src"
    for package in ("pkg", "pkg.trainer", "pkg.utilities"):
        _write(root, f"{package.replace('.', '/')}/__init__.py", "")
    _write(root, "pkg/trainer/trainer.py", "class Trainer:\n    pass\n")
    _write(root, "pkg/utilities/parsing.py", "def parse():\n    return None\n")
    _write(
        root,
        "pkg/consumer.py",
        "from pkg.utilities import GradClipAlgorithmType, parsing\nfrom pkg.trainer import trainer\n",
    )
    return root


@pytest.fixture(name="nested_package_tree")
def _nested_package_tree(tmp_path: Path) -> Path:
    """Source tree whose package chain stops three directories below the repository root.

    ``examples/fabric/rl/`` holds the only ``__init__.py`` in that branch, so its modules are importable only as
    ``rl.*`` and the sibling script writes exactly that. This is the layout the index already names by package root.

    Example:
        >>> root = getfixture("nested_package_tree")
        >>> (root / "examples/fabric/rl/agent.py").is_file()
        True
    """
    _write(tmp_path, "examples/fabric/rl/__init__.py", "")
    _write(tmp_path, "examples/fabric/rl/agent.py", "VALUE = 1\n")
    _write(tmp_path, "examples/fabric/train.py", "from rl.agent import VALUE\n")
    return tmp_path


def test_module_inside_a_package_is_named_from_its_package_root(nested_package_tree: Path) -> None:
    """The oracle names a nested module the way the index the arms query names it.

    Scenario: the only importer writes ``from rl.agent import VALUE`` because that is the sole importable name for a
    package whose chain stops below the repository root, and the index records the module under that same name. An
    oracle naming it by repository-relative path disagreed with the tool on both the key and its count, so an arm
    reporting exactly what the tool returned was scored wrong.
    """
    task = _task(primary_module="rl.agent", answer_contract={"fields": ["production_importers"]})

    oracle = agentic_contracts.build_oracle(task, nested_package_tree)

    assert oracle.expected["production_importers"] == ("examples.fabric.train",)


def test_same_named_scripts_outside_any_package_stay_distinct(nested_package_tree: Path) -> None:
    """Two loose ``train.py`` scripts keep separate repository-relative names instead of merging.

    Naming every file from its own directory would give both the name ``train``, collapsing two unrelated importers into
    one module whose count is the sum of theirs — an error that looks like an ordinary graph fact.
    """
    _write(nested_package_tree, "examples/dcgan/train.py", "from rl.agent import VALUE\n")
    task = _task(primary_module="rl.agent", answer_contract={"fields": ["production_importers"]})

    oracle = agentic_contracts.build_oracle(task, nested_package_tree)

    assert oracle.expected["production_importers"] == ("examples.dcgan.train", "examples.fabric.train")


def test_mixed_from_import_credits_package_and_submodule(mixed_import_tree: Path) -> None:
    """A partially resolving ``from`` import still credits its package.

    Before the fix a single concretely resolving alias suppressed package credit entirely, so the importer the task
    exists to find never appeared in the expected set and a correct answer was scored as a false positive.
    """
    oracle = agentic_contracts.build_oracle(_task(), mixed_import_tree)
    assert oracle.expected["production_importers"] == ("pkg.consumer",)


def test_package_import_credit_is_not_all_or_nothing(mixed_import_tree: Path) -> None:
    """The concrete submodule keeps its credit alongside the package."""
    oracle = agentic_contracts.build_oracle(
        _task(primary_module="pkg.utilities.parsing"),
        mixed_import_tree,
    )
    assert oracle.expected["production_importers"] == ("pkg.consumer",)


def test_correct_answer_scores_one_on_mixed_import_tree(mixed_import_tree: Path) -> None:
    """Naming the true importer is scored correct, not penalized."""
    oracle = agentic_contracts.build_oracle(_task(), mixed_import_tree)
    score = agentic_contracts.score_answer(oracle, {"production_importers": ["pkg.consumer"]})
    assert score.correct is True
    assert score.quality_score == pytest.approx(1.0)
    assert score.graded_score == pytest.approx(1.0)
    assert score.graded_components == {"production_importers": 1.0}


def test_import_convention_is_disclosed_in_the_scored_prompt() -> None:
    """The oracle's import convention reaches the model it scores."""
    instruction = agentic_contracts.answer_format_instruction(_task())
    assert agentic_contracts.IMPORT_CONVENTION_INSTRUCTION in instruction
    assert "from a.b import c" in instruction
    assert "a.b.c" in instruction


def test_agentic_prompt_suite_matches_the_explicit_cases(agentic_tasks: list[dict[str, Any]]) -> None:
    """Every declared task has exactly one static prompt case in suite order."""
    assert tuple(task["id"] for task in agentic_tasks) == _AGENTIC_TASK_IDS


@pytest.mark.parametrize("task_id", _AGENTIC_TASK_IDS)
def test_oracle_population_convention_is_disclosed_in_every_agentic_prompt(
    agentic_tasks: list[dict[str, Any]], task_id: str
) -> None:
    """Every shared task exposes the oracle's production and naming rules."""
    task = next(task for task in agentic_tasks if task["id"] == task_id)
    assert "never `a.b.__init__`" in agentic_contracts.ORACLE_POPULATION_INSTRUCTION
    assert "leading `src` layout directory is stripped" in agentic_contracts.ORACLE_POPULATION_INSTRUCTION
    prompt = agentic_contracts.materialize_agentic_prompt(task)
    assert agentic_contracts.ORACLE_POPULATION_INSTRUCTION in prompt
    assert "interpreter stdin" in prompt
    assert "not scratch work" in prompt
    assert "deterministic code for counts, set filtering and ranking" in prompt


def test_duplicate_ranking_is_rejected_before_semantic_grading() -> None:
    """Repeating a name cannot inflate ordered overlap through the public answer parser."""
    task = _task(
        answer_contract={
            "fields": ["ranking"],
            "params": {"ranking": {"candidate_set": "production_importers", "top_k": 3}},
        }
    )
    with pytest.raises(ValueError, match="invalid shape"):
        agentic_contracts.parse_labeled_answer(
            task, 'BEGIN_ANSWER_JSON\n{"ranking": ["pkg.a", "pkg.a"]}\nEND_ANSWER_JSON'
        )


def test_src_layout_directory_does_not_prefix_oracle_module_names(mixed_import_tree: Path) -> None:
    """A source root containing ``src/`` still reports package-relative module names."""
    oracle = agentic_contracts.build_oracle(_task(), mixed_import_tree.parent)

    assert oracle.expected["production_importers"] == ("pkg.consumer",)


def test_ba12_defers_to_the_shared_test_module_convention(agentic_tasks: list[dict[str, Any]]) -> None:
    """BA-12 cannot contradict the common test-module definition the oracle applies."""
    prompt = next(task["prompt"] for task in agentic_tasks if task["id"] == "BA-12")

    assert "Use the shared test-module convention" in prompt
    assert "filename merely starts with `test_`" not in prompt


def test_oracle_uses_the_shared_import_convention(mixed_import_tree: Path) -> None:
    """Agentic and MB/GR oracles resolve imports through one helper."""
    import ast

    from benchmarks._bench_common import python_source

    tree = ast.parse((mixed_import_tree / "pkg" / "consumer.py").read_text(encoding="utf-8"))
    names = {"pkg.utilities", "pkg.utilities.parsing", "pkg.trainer", "pkg.trainer.trainer"}
    shared = python_source.extract_import_targets(tree, package="pkg", keep=names, credit_submodules=True)
    assert agentic_contracts._import_targets(tree, "pkg.consumer", False, names) == shared


def _evidence_oracle() -> agentic_contracts.AgenticOracle:
    """Build an oracle expecting exactly one importer for raw-text evidence scoring.

    >>> oracle = _evidence_oracle()
    >>> oracle.fields, oracle.expected
    (('production_importers',), {'production_importers': ('pkg.core',)})
    """
    return agentic_contracts.AgenticOracle(
        task_id="BA-TEST",
        fields=("production_importers",),
        expected={"production_importers": ("pkg.core",)},
    )


def test_answer_failure_details_classifies_bounded_semantic_mismatches() -> None:
    """Details separate absent facts, extras, count errors, ranking errors, and scalar errors."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="BA-TEST",
        fields=(
            "production_importers",
            "rdep_counts",
            "ranking",
            "buckets",
            "overlap_count",
            "dependency_chain",
        ),
        expected={
            "production_importers": ("pkg.expected", "pkg.present"),
            "rdep_counts": {"pkg.missing": 2, "pkg.present": 1, "pkg.wrong": 3},
            "ranking": ("pkg.first", "pkg.second"),
            "buckets": {"internal": ("pkg.internal",), "public": ("pkg.public",)},
            "overlap_count": 2,
            "dependency_chain": ("pkg.source", "pkg.target"),
        },
    )
    answer = {
        "production_importers": ["pkg.present", "pkg.unexpected"],
        "rdep_counts": {"pkg.present": 1, "pkg.unexpected": 4, "pkg.wrong": 8},
        "ranking": ["pkg.second"],
        "buckets": {"internal": ["pkg.internal", "pkg.unexpected"], "public": []},
        "overlap_count": 3,
        "dependency_chain": ["pkg.target", "pkg.source"],
    }

    assert agentic_contracts.answer_failure_details(oracle, answer) == [
        {
            "category": "missing_facts",
            "field": "production_importers",
            "expected": ["pkg.expected"],
            "actual": [],
        },
        {
            "category": "unexpected_facts",
            "field": "production_importers",
            "expected": [],
            "actual": ["pkg.unexpected"],
        },
        {
            "category": "missing_facts",
            "field": "rdep_counts",
            "expected": {"pkg.missing": 2},
            "actual": {},
        },
        {
            "category": "unexpected_facts",
            "field": "rdep_counts",
            "expected": {},
            "actual": {"pkg.unexpected": 4},
        },
        {
            "category": "wrong_counts",
            "field": "rdep_counts",
            "expected": {"pkg.wrong": 3},
            "actual": {"pkg.wrong": 8},
        },
        {"category": "wrong_rankings", "field": "ranking[0]", "expected": "pkg.first", "actual": "pkg.second"},
        {"category": "wrong_rankings", "field": "ranking[1]", "expected": "pkg.second", "actual": None},
        {
            "category": "unexpected_facts",
            "field": "buckets.internal",
            "expected": [],
            "actual": ["pkg.unexpected"],
        },
        {
            "category": "missing_facts",
            "field": "buckets.public",
            "expected": ["pkg.public"],
            "actual": [],
        },
        {"category": "wrong_counts", "field": "overlap_count", "expected": 2, "actual": 3},
        {
            "category": "wrong_values",
            "field": "dependency_chain",
            "expected": ["pkg.source", "pkg.target"],
            "actual": ["pkg.target", "pkg.source"],
        },
    ]


def test_answer_failure_details_accepts_correct_values_for_every_current_field() -> None:
    """Every currently declared field has a no-mismatch diagnostic path."""
    expected = {
        "production_importers": ("pkg.importer",),
        "rdep_counts": {"pkg.importer": 1},
        "ranking": ("pkg.importer",),
        "buckets": {"internal": ("pkg.importer",)},
        "overlap_importers": ("pkg.importer",),
        "overlap_count": 1,
        "cross_namespace_importers": ("pkg.importer",),
        "dependency_chain": ("pkg.source", "pkg.target"),
        "affected_module_count": 2,
        "production_importer_count": 1,
        "excluded_test_importer_count": 0,
        "test_importer_count": 0,
        "isolation_verdict": "isolated",
        "risk_tier": "low",
        "high_centrality": {"pkg.importer": 1},
    }
    answer = {
        key: list(value) if isinstance(value, tuple) else dict(value) if isinstance(value, dict) else value
        for key, value in expected.items()
    }
    answer["buckets"] = {label: list(values) for label, values in expected["buckets"].items()}
    oracle = agentic_contracts.AgenticOracle(task_id="BA-TEST", fields=tuple(expected), expected=expected)

    assert agentic_contracts.answer_failure_details(oracle, answer) == []


def test_score_answer_adds_proportional_graded_credit_without_changing_exact_count_score() -> None:
    """A near count earns prospective credit while the established exact result stays failed."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-COUNT",
        fields=("affected_module_count",),
        expected={"affected_module_count": 57},
    )

    score = agentic_contracts.score_answer(oracle, {"affected_module_count": 56})

    assert score.components == {"affected_module_count": 0.0}
    assert score.quality_score == pytest.approx(0.0)
    assert score.correct is False
    assert score.graded_components == {"affected_module_count": pytest.approx(56 / 57)}
    assert score.graded_score == pytest.approx(56 / 57)


@pytest.mark.parametrize(
    ("expected", "actual", "credit"),
    [
        pytest.param(0, 0, 1.0, id="equal-zeroes"),
        pytest.param(0, 1, 0.0, id="expected-zero"),
        pytest.param(1, 0, 0.0, id="actual-zero"),
        pytest.param(1, -1, 0.0, id="negative"),
        pytest.param(1, True, 0.0, id="boolean"),
        pytest.param(1, None, 0.0, id="missing"),
    ],
)
def test_score_answer_graded_counts_reject_invalid_values(expected: int, actual: Any, credit: float) -> None:
    """Only non-negative integer counts receive proportional graded credit."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-COUNT",
        fields=("production_importer_count",),
        expected={"production_importer_count": expected},
    )

    score = agentic_contracts.score_answer(oracle, {"production_importer_count": actual})

    assert score.graded_components["production_importer_count"] == pytest.approx(credit)


def test_score_answer_graded_count_mappings_require_matching_keys_and_values() -> None:
    """Mapping credit combines key coverage with proportional values, including zero counts."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-MAPPING",
        fields=("rdep_counts", "high_centrality"),
        expected={
            "rdep_counts": {"pkg.alpha": 57, "pkg.zero": 0},
            "high_centrality": {"pkg.alpha": 2},
        },
    )

    score = agentic_contracts.score_answer(
        oracle,
        {
            "rdep_counts": {"pkg.alpha": 56, "pkg.zero": 0},
            "high_centrality": {"pkg.alpha": 2, "pkg.extra": 1},
        },
    )

    assert score.components == {"rdep_counts": 0.5, "high_centrality": 2 / 3}
    assert score.graded_components["rdep_counts"] == pytest.approx((1 + 56 / 57) / 2)
    assert score.graded_components["high_centrality"] == pytest.approx(2 / 3)
    assert score.correct is False


def test_score_answer_graded_count_mappings_do_not_hide_missing_or_invalid_values() -> None:
    """Absent keys, booleans, and all-zero mismatches cannot receive mapping credit."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-MAPPING",
        fields=("rdep_counts",),
        expected={"rdep_counts": {"pkg.alpha": 1, "pkg.beta": 1}},
    )

    missing = agentic_contracts.score_answer(oracle, {"rdep_counts": {"pkg.alpha": 1}})
    invalid = agentic_contracts.score_answer(oracle, {"rdep_counts": {"pkg.alpha": True, "pkg.beta": 1}})
    zero_oracle = agentic_contracts.AgenticOracle(
        task_id="T-ZERO-MAPPING",
        fields=("rdep_counts",),
        expected={"rdep_counts": {"pkg.zero": 0}},
    )
    all_zero_mismatch = agentic_contracts.score_answer(zero_oracle, {"rdep_counts": {"pkg.zero": 1}})

    assert missing.graded_components["rdep_counts"] == pytest.approx(2 / 3)
    assert invalid.graded_components["rdep_counts"] == pytest.approx(0.5)
    assert all_zero_mismatch.graded_components["rdep_counts"] == pytest.approx(0.0)


def test_score_answer_graded_ranking_uses_longest_common_subsequence() -> None:
    """One omitted rank preserves ordered credit after the resulting position shift."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-RANKING",
        fields=("ranking",),
        expected={"ranking": ("pkg.alpha", "pkg.beta", "pkg.gamma")},
    )

    score = agentic_contracts.score_answer(oracle, {"ranking": ["pkg.alpha", "pkg.gamma"]})

    assert score.components == {"ranking": pytest.approx(1 / 3)}
    assert score.graded_components == {"ranking": pytest.approx(2 / 3)}

    assert score.quality_score == pytest.approx(1 / 3)
    assert score.graded_score == pytest.approx(2 / 3)
    assert score.correct is False


def test_score_answer_graded_ranking_penalizes_trailing_items() -> None:
    """The LCS denominator includes extra reported ranks even when legacy ranking remains exact."""
    oracle = agentic_contracts.AgenticOracle(
        task_id="T-RANKING",
        fields=("ranking",),
        expected={"ranking": ("pkg.alpha", "pkg.beta")},
    )

    score = agentic_contracts.score_answer(oracle, {"ranking": ["pkg.alpha", "pkg.beta", "pkg.extra"]})

    assert score.components == {"ranking": 1.0}
    assert score.correct is True
    assert score.graded_components == {"ranking": pytest.approx(2 / 3)}
    details = agentic_contracts.answer_failure_details(oracle, {"ranking": ["pkg.alpha", "pkg.beta", "pkg.extra"]})
    assert {"category": "unexpected_facts", "field": "ranking", "expected": [], "actual": ["pkg.extra"]} in details


@pytest.mark.parametrize(
    "text",
    ["inspected pkg.core_utils only", "inspected other.pkg.core.helpers only", "inspected mypkg.core only"],
)
def test_evidence_recall_rejects_substring_containment(text: str) -> None:
    """A longer dotted name no longer donates recall to a shorter one."""
    metrics = agentic_contracts.score_evidence_metrics(_evidence_oracle(), exposure_text=text, tool_calls=1)
    assert metrics.erec == pytest.approx(0.0)
    assert metrics.deff == pytest.approx(0.0)


@pytest.mark.parametrize(
    "text",
    [
        "pkg.core",
        "importers: pkg.core, pkg.other",
        "found 'pkg.core' in the graph",
        "src/pkg/core.py imports pkg.core",
        "the only importer is pkg.core.",
        "(pkg.core)",
    ],
)
def test_evidence_recall_credits_a_whole_name_mention(text: str) -> None:
    """Genuine whole-name mentions keep their credit."""
    metrics = agentic_contracts.score_evidence_metrics(_evidence_oracle(), exposure_text=text, tool_calls=1)
    assert metrics.erec == pytest.approx(1.0)


def test_report_recall_uses_the_same_whole_name_rule() -> None:
    """Exposure and report recall share one matching rule."""
    oracle = _evidence_oracle()
    metrics = agentic_contracts.score_evidence_metrics(
        oracle,
        exposure_text="pkg.core",
        report_text="pkg.core_utils",
        tool_calls=1,
    )
    assert metrics.erec == pytest.approx(1.0)
    assert metrics.rrec == pytest.approx(0.0)
