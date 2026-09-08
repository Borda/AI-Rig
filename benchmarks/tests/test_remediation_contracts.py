"""Regression contracts for the 2026-08 structural benchmark remediation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmarks._bench_common import provider_parity_contracts as core


BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
CODEX_RUNNER_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
CODEX_QUERY_SKILL_PATH = BENCHMARKS_DIR.parent / "plugins" / "codemap-py" / "codex-skills" / "query-code" / "SKILL.md"
CLAUDE_QUERY_SKILL_PATH = BENCHMARKS_DIR.parent / "plugins" / "codemap-py" / "claude-skills" / "query-code" / "SKILL.md"


@pytest.fixture(name="script_run_codex", scope="module")
def _script_run_codex() -> Any:
    """Import the Codex runner for remediation tests without executing its command-line entry point.

    >>> getfixture("script_run_codex").__name__
    'remediation_run_codex'
    """
    spec = importlib.util.spec_from_file_location("remediation_run_codex", CODEX_RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Codex runner at {CODEX_RUNNER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _review_task() -> dict[str, Any]:
    """Build an unscoreable review task with ordered count and caller-name subquestions.

    >>> task = _review_task()
    >>> task["scoreable"], [question["match"] for question in task["sub_questions"]]
    (False, ['integer_extract', 'symbol_name_set'])
    """
    return {
        "id": "RV-fixture",
        "type": "review_assistance",
        "scoreable": False,
        "prompt": "Review the change before approving it.",
        "sub_questions": [
            {
                "id": "q1",
                "match": "integer_extract",
                "prompt": "How many reverse dependencies are there?",
                "ground_truth": {"count": 1},
            },
            {
                "id": "q2",
                "match": "symbol_name_set",
                "prompt": "Name the affected production callers.",
                "ground_truth": {"symbols": ["fixture.caller"]},
            },
        ],
    }


def test_materialized_review_prompt_is_ordered_and_hashes_the_exact_delivered_bytes(
    script_run_bench: Any, script_run_codex: Any, tmp_path: Path
) -> None:
    """Prevent nested RV questions being graded but omitted from either provider prompt."""
    task = _review_task()
    delivered = core.materialize_task_prompt(task)
    expected_hash = hashlib.sha256(delivered.encode("utf-8")).hexdigest()

    assert delivered.startswith(task["prompt"])
    assert delivered.index("How many reverse dependencies") < delivered.index("Name the affected production callers")
    assert core.prompt_hash(task) == expected_hash

    claude_commands: list[list[str]] = []
    claude = script_run_bench.BenchRunner("fixture", "fixture-model", tmp_path, tmp_path / "index.json")

    def _claude_stream(command: list[str], result: Any, *_args: Any, **_kwargs: Any) -> None:
        """Record the delivered command and mark the synthetic Claude result successful."""
        claude_commands.append(command)
        result.success = True
        result.input_tokens = 1
        result.output_tokens = 1

    claude._stream = _claude_stream
    claude_result = claude.run(task, "plain")

    codex_commands: list[list[str]] = []

    def _codex_transport(command: list[str], **_kwargs: Any) -> str:
        """Record the delivered command and return a successful native Codex stream."""
        codex_commands.append(command)
        return "\n".join(
            (
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
            )
        )

    codex = script_run_codex.CodexRunner("fixture-model", tmp_path, transport=_codex_transport)
    codex_result = codex.run(task, "A_plain")

    assert claude_commands[-1][-1] == delivered
    assert codex_commands[-1][-1].endswith(delivered)
    assert claude_result.prompt_hash == expected_hash
    assert codex_result.prompt_hash == expected_hash


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param("64 reverse dependencies", 64, id="reverse-dependencies"),
        pytest.param("37 distinct production functions", 37, id="distinct-production-functions"),
        pytest.param("24 unique production callers", 24, id="unique-production-callers"),
    ],
)
def test_review_count_extraction_accepts_delivered_count_labels(
    script_run_bench: Any, answer: str, expected: int
) -> None:
    """Prevent correct RV answers with the documented count labels becoming extraction failures."""
    task = {
        "id": "RV-count",
        "type": "review_assistance",
        "sub_questions": [{"id": "q1", "match": "integer_extract", "ground_truth": {"count": expected}}],
    }

    quality = script_run_bench._evaluate_rv(task, answer)

    assert quality.metric_got == expected
    assert quality.correct is True
    assert quality.extraction_failed is False


def test_codex_preserves_shared_evaluator_extraction_failure(script_run_codex: Any, tmp_path: Path) -> None:
    """Prevent Codex from silently reclassifying a scored evaluator extraction failure as valid."""
    evaluator_result = core.EvaluationResult(
        scored=True,
        correct=False,
        quality_score=None,
        extraction_failed=True,
    )
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
        ),
        evaluator=lambda *_args: evaluator_result,
    )

    result = runner.run({"id": "EV-01", "type": "demo", "prompt": "Answer."}, "A_plain")

    assert result.extraction_failed is True
    assert result.quality_score is None
    assert result.correct is False


def test_cq01_keeps_unique_ast_oracle_separate_from_codemap_declaration_view(script_run_bench: Any) -> None:
    """Prevent CQ-01's 11 Codemap declarations from being scored as seven unique AST names."""
    task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "CQ-01")
    output = "\n".join(
        (
            "Codemap static view: 11 declarations.",
            "Independent AST view: 7 unique names.",
            "## Symbols",
            *task["ground_truth"]["undocumented_symbols"],
        )
    )

    quality = script_run_bench._evaluate_oss(task, output)

    assert quality.correct is True
    assert quality.metric_expected == 7
    assert quality.metric_got == 7
    assert quality.scoring_detail["oracle_views"]["independent_ast"]["count"] == 7
    assert (
        quality.scoring_detail["oracle_views"]["independent_ast"]["symbols"]
        == task["ground_truth"]["undocumented_symbols"]
    )
    assert quality.scoring_detail["oracle_views"]["codemap_static"]["count"] == 11
    assert "declaration" in quality.scoring_detail["oracle_views"]["codemap_static"]["semantics"]


@pytest.mark.parametrize(
    ("mode", "expected_extraction_failure"),
    [
        pytest.param("wrong-count", False, id="wrong-count"),
        pytest.param("missing-names", True, id="missing-required-names"),
    ],
)
def test_required_review_components_cannot_be_bypassed_by_one_perfect_subanswer(
    script_run_bench: Any, mode: str, expected_extraction_failure: bool
) -> None:
    """RV multi-part fitness must include both count and required symbol-set answers."""
    task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "RV-01")
    symbols = task["sub_questions"][1]["ground_truth"]["symbols"]
    output = (
        "999 undocumented symbols.\n## Symbols\n" + "\n".join(symbols)
        if mode == "wrong-count"
        else "7 undocumented symbols."
    )

    quality = script_run_bench._evaluate_rv(task, output)

    assert quality.correct is False
    assert quality.recall == pytest.approx(0.5)
    assert quality.extraction_failed is expected_extraction_failure


def test_cq01_required_names_cannot_be_bypassed_by_a_correct_count(script_run_bench: Any) -> None:
    """CQ-01 needs its independent AST count and every requested unique qualified name."""
    task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "CQ-01")

    quality = script_run_bench._evaluate_oss(task, "Independent AST: 7 unique names.")

    assert quality.correct is False
    assert quality.recall == pytest.approx(0.5)
    assert quality.extraction_failed is True


def test_cq02_required_names_cannot_be_bypassed_by_a_correct_count(script_run_bench: Any) -> None:
    """CQ-02 needs its independent AST count and every requested unique public name."""
    task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "CQ-02")

    quality = script_run_bench._evaluate_oss(task, "11 uncovered symbols.")

    assert quality.correct is False
    assert quality.recall == pytest.approx(0.5)
    assert quality.extraction_failed is True


def test_required_count_fitness_is_continuous_outside_the_correctness_gate(script_run_bench: Any) -> None:
    """A near-miss count must retain partial fitness while remaining outside the 10% correctness gate."""
    task = {
        "id": "RV-continuous-count",
        "type": "review_assistance",
        "sub_questions": [{"id": "q1", "match": "integer_extract", "ground_truth": {"count": 10}}],
    }

    quality = script_run_bench._evaluate_rv(task, "12 undocumented symbols.")

    assert quality.correct is False
    assert quality.recall == pytest.approx(0.8)
    assert quality.scoring_detail["components"]["q1.count"]["relative_error"] == pytest.approx(0.2)


def test_codex_uses_the_same_continuous_review_component_fitness(script_run_codex: Any) -> None:
    """Codex consumes the shared Claude evaluator rather than maintaining a divergent count score."""
    task = {
        "id": "RV-continuous-count",
        "type": "review_assistance",
        "sub_questions": [{"id": "q1", "match": "integer_extract", "ground_truth": {"count": 10}}],
    }

    result = script_run_codex._default_evaluator(task, "12 undocumented symbols.")

    assert result.correct is False
    assert result.quality_score == pytest.approx(0.8)
    assert result.components["subanswer:q1.count"] == pytest.approx(0.8)


@pytest.mark.parametrize(
    "sub_question",
    [
        pytest.param({"id": "q1", "match": "unsupported", "ground_truth": {"count": 10}}, id="unknown-match"),
        pytest.param({"id": "q1", "match": "integer_extract", "ground_truth": {}}, id="missing-count"),
        "not-an-object",
    ],
)
def test_required_review_subquestions_fail_closed_instead_of_being_silently_skipped(
    script_run_bench: Any, sub_question: Any
) -> None:
    """Invalid required review contracts must not leave a scoreable remainder."""
    task = {
        "id": "RV-invalid-contract",
        "type": "review_assistance",
        "sub_questions": [sub_question],
    }

    with pytest.raises(ValueError, match="review sub-question"):
        script_run_bench._evaluate_rv(task, "10 undocumented symbols.")


def test_multiple_review_count_components_fail_closed_without_answer_scoping(script_run_bench: Any) -> None:
    """One unlabelled answer-region count cannot safely answer two distinct required questions."""
    task = {
        "id": "RV-ambiguous-counts",
        "type": "review_assistance",
        "sub_questions": [
            {"id": "q1", "match": "integer_extract", "ground_truth": {"count": 10}},
            {"id": "q2", "match": "integer_extract", "ground_truth": {"count": 20}},
        ],
    }

    with pytest.raises(ValueError, match="multiple required count"):
        script_run_bench._evaluate_rv(task, "10 undocumented symbols; 20 uncovered symbols.")


def test_distinct_independent_oracle_tasks_allow_repository_reads_after_required_skill_query() -> None:
    """C keeps its required Skill/query evidence while allowing distinct-oracle repository inspection."""
    task_by_id = {task["id"]: task for task in core.load_task_suite(SUITE_PATH)}
    skill = CODEX_QUERY_SKILL_PATH.read_text(encoding="utf-8").lower()

    assert "ordinary repository reads remain allowed" in skill
    assert "distinct independent ast/oracle view" in skill
    for task_id in ("RV-05", "CQ-02"):
        prompt = task_by_id[task_id]["prompt"].lower()
        assert "independent ast oracle" in prompt
        assert "codemap" in prompt


def test_query_skills_require_with_imports_for_source_requests_that_name_imports() -> None:
    """Prevent source-with-imports tasks from falling back to redundant repository reads."""
    task_by_id = {task["id"]: task for task in core.load_task_suite(SUITE_PATH)}

    for task_id in ("SE-01", "SE-02"):
        assert "import" in task_by_id[task_id]["prompt"].lower()

    for skill_path in (CODEX_QUERY_SKILL_PATH, CLAUDE_QUERY_SKILL_PATH):
        skill = skill_path.read_text(encoding="utf-8")
        assert "symbol <name> --with-imports" in skill
        assert "query_complete" in skill


def test_uncovered_task_prompts_and_views_match_the_independent_ast_oracle() -> None:
    """Task wording exposes every Name/Attribute and patch-string reference the generator records."""
    task_by_id = {task["id"]: task for task in core.load_task_suite(SUITE_PATH)}
    required_oracle_words = (
        "every ast `name` and `attribute` identifier",
        "final dotted component of every string argument",
    )

    for task_id in ("RV-05", "CQ-02"):
        prompt = task_by_id[task_id]["prompt"].lower()
        assert all(words in prompt for words in required_oracle_words)

    cq02_views = task_by_id["CQ-02"]["ground_truth"]["oracle_views"]
    assert cq02_views["independent_ast"]["symbols"] == task_by_id["CQ-02"]["ground_truth"]["uncovered_symbols"]
    assert cq02_views["codemap_static"]["count"] == task_by_id["CQ-02"]["ground_truth"]["uncovered_count_scan"]


def test_rv05_exposes_independent_and_codemap_static_oracle_views_without_conflation() -> None:
    """Prevent RV-05's independent AST evidence and Codemap static evidence sharing one unlabeled count."""
    task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "RV-05")
    views = task["ground_truth"]["oracle_views"]

    assert views["independent_ast"]["count"] == 11
    assert views["independent_ast"]["symbols"] == task["sub_questions"][1]["ground_truth"]["symbols"]
    assert views["codemap_static"] != views["independent_ast"]
    assert "count" in views["codemap_static"]
    assert views["independent_ast"]["semantics"] != views["codemap_static"]["semantics"]


@pytest.mark.parametrize(
    ("arm", "compliance", "contaminated", "expected"),
    [
        pytest.param("A_plain", None, False, True, id="plain-adheres-without-codemap"),
        pytest.param("A_plain", None, True, False, id="plain-contamination-breaks-adherence"),
        pytest.param("B_auto", None, False, True, id="optional-no-call-adheres"),
        pytest.param("B_auto", True, False, True, id="direct-optional-use-observed"),
        # B is an optional-use canary on both providers, so a no-query B cell is
        # adherent. Its non-compliance is still recorded separately as observed evidence.
        pytest.param("B_auto", False, False, True, id="direct-optional-no-call-adheres"),
        pytest.param("B_auto", False, True, False, id="direct-contamination-breaks-adherence"),
        pytest.param("C_strict", True, False, True, id="skill-required-use-observed"),
        pytest.param("C_strict", True, True, False, id="skill-contamination-breaks-adherence"),
    ],
)
def test_treatment_adherence_is_distinct_from_codemap_use_compliance(
    arm: str, compliance: bool | None, contaminated: bool, expected: bool
) -> None:
    """Prevent display of A's N/A Codemap-use status as failed treatment adherence."""
    assert (
        core.treatment_adherence(
            arm,
            codemap_use_compliance=compliance,
            contaminated=contaminated,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("input_tokens", "cached_input_tokens", "expected", "inconsistent"),
    [
        pytest.param(120, 80, 40, False, id="gross-minus-cached"),
        pytest.param(80, 80, 0, False, id="fully-cached"),
        pytest.param(25, 80, None, True, id="cache-over-gross-is-unscoreable"),
    ],
)
def test_fresh_input_tokens_exposes_native_gross_cached_and_fresh_views(
    input_tokens: int, cached_input_tokens: int, expected: int | None, inconsistent: bool
) -> None:
    """Prevent malformed native usage from becoming a plausible zero fresh-token value."""
    assert core.fresh_input_tokens(input_tokens, cached_input_tokens) == expected
    assert core.token_accounting_inconsistent(input_tokens, cached_input_tokens) is inconsistent


@pytest.mark.parametrize(
    "input_tokens,cached_input_tokens",
    [pytest.param(-1, 0, id="negative-gross"), pytest.param(0, -1, id="negative-cache")],
)
def test_fresh_input_tokens_rejects_negative_native_usage(input_tokens: int, cached_input_tokens: int) -> None:
    """Native token counters cannot become plausible fresh-input values when malformed."""
    with pytest.raises(ValueError, match="token"):
        core.fresh_input_tokens(input_tokens, cached_input_tokens)


def test_canonical_result_rows_sorts_a_derived_view_without_reordering_raw_execution() -> None:
    """Prevent random append order from being overwritten while publishing a reproducible sidecar order."""
    raw_rows = [
        {"task_id": "CQ-01", "repetition": 1, "arm": "C_strict", "execution_index": 0},
        {"task_id": "RV-02", "repetition": 2, "arm": "B_auto", "execution_index": 1},
        {"task_id": "RV-02", "repetition": 1, "arm": "C_strict", "execution_index": 2},
        {"task_id": "RV-02", "repetition": 1, "arm": "A_plain", "execution_index": 3},
        {"task_id": "RV-02", "repetition": 1, "arm": "B_auto", "execution_index": 4},
    ]

    canonical = core.canonical_result_rows(
        raw_rows,
        task_order=("RV-02", "CQ-01"),
        arm_order=("A_plain", "B_auto", "C_strict"),
    )

    assert [row["execution_index"] for row in raw_rows] == [0, 1, 2, 3, 4]
    assert [row["execution_index"] for row in canonical] == [3, 4, 2, 1, 0]


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [
                {"task_id": "RV-02", "repetition": 1, "arm": "A_plain", "execution_index": 0},
                {"task_id": "RV-02", "repetition": 1, "arm": "A_plain", "execution_index": 1},
            ],
            id="duplicate-coordinate",
        ),
        pytest.param(
            [{"task_id": "unknown", "repetition": 1, "arm": "A_plain", "execution_index": 0}],
            id="unknown-task",
        ),
    ],
)
def test_canonical_result_rows_rejects_ambiguous_or_unknown_coordinates(rows: list[dict[str, Any]]) -> None:
    """A sidecar must not present duplicate or unknown evidence as a complete canonical run."""
    with pytest.raises(ValueError, match="duplicate|unknown"):
        core.canonical_result_rows(rows, task_order=("RV-02",), arm_order=("A_plain",))


def test_codex_canonical_sidecar_keeps_raw_order_and_starts_non_poolable(script_run_codex: Any, tmp_path: Path) -> None:
    """Raw interruption evidence remains append-only while the sidecar is canonical and marked partial."""
    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.touch()
    first = script_run_codex.CodexRun("C_strict", "SE-01", "symbol_extraction", "fixture")
    second = script_run_codex.CodexRun("A_plain", "SE-01", "symbol_extraction", "fixture")
    script_run_codex._append_run(telemetry, first, execution_index=0)
    script_run_codex._append_run(telemetry, second, execution_index=1)

    canonical_path = script_run_codex._canonical_telemetry_path(telemetry)
    sidecar_hash = script_run_codex._write_canonical_telemetry(
        telemetry,
        canonical_path,
        task_order=("SE-01",),
    )
    raw_rows = [json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines()]
    canonical_rows = [json.loads(line) for line in canonical_path.read_text(encoding="utf-8").splitlines()]

    assert [row["arm"] for row in raw_rows] == ["C_strict", "A_plain"]
    assert [row["execution_index"] for row in raw_rows] == [0, 1]
    assert [row["arm"] for row in canonical_rows] == ["A_plain", "C_strict"]
    assert hashlib.sha256(canonical_path.read_bytes()).hexdigest() == sidecar_hash

    metadata = script_run_codex._initial_run_metadata(
        manifest_path=BENCHMARKS_DIR / "manifests" / "codex-integration.json",
        repo_path=tmp_path,
        index_path=None,
        output_path=telemetry,
        metadata_path=tmp_path / "run-metadata.json",
        model="gpt-5.6-luna",
        reasoning_effort="high",
        repetitions=1,
        task_arms={("SE-01", 1): ("A_plain", "B_auto", "C_strict")},
        cell_wall_clock_seconds=600,
        auth_provisioned=False,
    )

    assert metadata["artifacts"]["canonical_telemetry_status"] == "not_written"
    assert metadata["artifacts"]["canonical_telemetry_pooling_eligible"] is False


def test_codex_result_block_presents_persisted_arms_in_fixed_order(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Human output stays task-first/A-B-C even when raw execution is randomized."""
    printed: list[tuple[str, str]] = []
    monkeypatch.setattr(script_run_codex.runtime, "print_arm_row", lambda row, arm: printed.append((arm, row)))

    next_progress = script_run_codex._print_result_block(
        (("C_strict", "C row"), ("A_plain", "A row"), ("B_auto", "B row")),
        printed_cells=4,
        planned_cells=9,
    )

    assert printed == [("A_plain", "(5/9) A row"), ("B_auto", "(6/9) B row"), ("C_strict", "(7/9) C row")]
    assert next_progress == 7
