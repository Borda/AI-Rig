"""Regression contracts for the 2026-08 structural benchmark remediation.

Each class pins one remediated behaviour of the shared provider-parity library or the structural runners: delivered
review prompts, review and code-quality scoring, task and skill wording, treatment adherence, token accounting, and the
canonical result ordering. No model or provider CLI is executed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmarks._bench_common import provider_parity_contracts as core

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
CODEX_RUNNER_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
CODEX_QUERY_SKILL_PATH = BENCHMARKS_DIR.parent / "plugins" / "codemap-py" / "codex-skills" / "query-code" / "SKILL.md"
CLAUDE_QUERY_SKILL_PATH = BENCHMARKS_DIR.parent / "plugins" / "codemap-py" / "claude-skills" / "query-code" / "SKILL.md"
_TURN_COMPLETED = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})


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


@pytest.fixture(name="task_by_id", scope="module")
def _task_by_id() -> dict[str, dict[str, Any]]:
    """Index the committed structural benchmark suite by task identifier (read only)."""
    return {task["id"]: task for task in core.load_task_suite(SUITE_PATH)}


def _flat(text: str) -> str:
    """Collapse all whitespace runs to single spaces so a phrase matches wherever its prose wraps.

    >>> _flat("two\\n  words")
    'two words'
    """
    return " ".join(text.split())


def _count_question(count: int, question_id: str = "q1") -> dict[str, Any]:
    """Build a review sub-question expecting the integer *count* from an unlabelled answer.

    >>> _count_question(10)["ground_truth"]
    {'count': 10}
    """
    return {"id": question_id, "match": "integer_extract", "ground_truth": {"count": count}}


def _rv_task(*sub_questions: Any, task_id: str = "RV-fixture") -> dict[str, Any]:
    """Build a review-assistance task whose scoring comes from the given sub-questions.

    >>> _rv_task(_count_question(1))["type"]
    'review_assistance'
    """
    return {"id": task_id, "type": "review_assistance", "sub_questions": list(sub_questions)}


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


class TestMaterializedReviewPrompt:
    """Nested review questions are part of the prompt each provider actually receives."""

    def test_orders_subquestions_and_hashes_the_delivered_bytes(self) -> None:
        """The delivered prompt keeps the task prompt first, then each sub-question in order, and hashes exactly that.

        Prevents nested RV questions being graded but omitted from the provider prompt, or hashed from different bytes
        than the ones delivered.
        """
        task = _review_task()

        delivered = core.materialize_task_prompt(task)

        assert delivered.startswith(task["prompt"])
        assert delivered.index("How many reverse dependencies") < delivered.index(
            "Name the affected production callers"
        )
        assert core.prompt_hash(task) == hashlib.sha256(delivered.encode("utf-8")).hexdigest()

    def test_claude_runner_delivers_the_materialized_prompt(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The Claude runner passes the materialized prompt as the last command argument and records its hash.

        The stream is replaced by a recorder so no model runs; only the command the runner would have launched matters.
        """
        commands: list[list[str]] = []

        def _stream(command: list[str], result: Any, *_args: Any, **_kwargs: Any) -> None:
            """Record the delivered command and mark the synthetic Claude result successful."""
            commands.append(command)
            result.success = True
            result.input_tokens = 1
            result.output_tokens = 1

        task = _review_task()
        runner = script_run_bench.BenchRunner("fixture", "fixture-model", tmp_path, tmp_path / "index.json")
        monkeypatch.setattr(runner, "_stream", _stream)
        delivered = core.materialize_task_prompt(task)

        result = runner.run(task, "plain")

        assert commands[-1][-1] == delivered
        assert result.prompt_hash == hashlib.sha256(delivered.encode("utf-8")).hexdigest()

    def test_codex_runner_delivers_the_materialized_prompt(self, script_run_codex: Any, tmp_path: Path) -> None:
        """The Codex runner ends its command with the materialized prompt and records the same hash as Claude.

        The transport is replaced by a recorder returning a successful native stream, so no model runs.
        """
        commands: list[list[str]] = []

        def _transport(command: list[str], **_kwargs: Any) -> str:
            """Record the delivered command and return a successful native Codex stream."""
            commands.append(command)
            message = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
            return "\n".join((json.dumps(message), _TURN_COMPLETED))

        task = _review_task()
        codex = script_run_codex.CodexRunner("gpt-6.1-sol", tmp_path, transport=_transport)
        delivered = core.materialize_task_prompt(task)

        result = codex.run(task, "A_plain")

        assert commands[-1][-1].endswith(delivered)
        assert result.prompt_hash == hashlib.sha256(delivered.encode("utf-8")).hexdigest()


class TestReviewEvaluation:
    """Review answers are scored on every required component, continuously, and fail closed on bad contracts."""

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            pytest.param("64 reverse dependencies", 64, id="reverse-dependencies"),
            pytest.param("37 distinct production functions", 37, id="distinct-production-functions"),
            pytest.param("24 unique production callers", 24, id="unique-production-callers"),
        ],
    )
    def test_extraction_accepts_delivered_count_labels(self, script_run_bench: Any, answer: str, expected: int) -> None:
        """A correct count written with any documented label is extracted rather than failing extraction.

        Prevents correct RV answers using the documented count labels becoming extraction failures.
        """
        quality = script_run_bench._evaluate_rv(_rv_task(_count_question(expected), task_id="RV-count"), answer)

        assert quality.metric_got == expected
        assert quality.correct is True
        assert quality.extraction_failed is False

    @pytest.mark.parametrize(
        ("output_template", "expected_extraction_failure"),
        [
            pytest.param("999 undocumented symbols.\n## Symbols\n{symbols}", False, id="wrong-count"),
            pytest.param("7 undocumented symbols.", True, id="missing-required-names"),
        ],
    )
    def test_both_required_components_must_be_answered(
        self,
        script_run_bench: Any,
        task_by_id: dict[str, dict[str, Any]],
        output_template: str,
        expected_extraction_failure: bool,
    ) -> None:
        """RV-01 multi-part fitness needs both the count and the required symbol set; one perfect part is half credit.

        A right symbol list under a wrong count, or a right count with no names, scores 0.5 recall and is never correct.
        """
        task = task_by_id["RV-01"]
        output = output_template.format(symbols="\n".join(task["sub_questions"][1]["ground_truth"]["symbols"]))

        quality = script_run_bench._evaluate_rv(task, output)

        assert quality.correct is False
        assert quality.recall == pytest.approx(0.5)
        assert quality.extraction_failed is expected_extraction_failure

    def test_count_fitness_is_continuous_outside_the_correctness_gate(self, script_run_bench: Any) -> None:
        """A near-miss count keeps partial fitness while remaining outside the 10% correctness gate.

        Asking for 10 and answering 12 is 20% off: not correct, but worth 0.8 rather than a binary zero.
        """
        quality = script_run_bench._evaluate_rv(_rv_task(_count_question(10)), "12 undocumented symbols.")

        assert quality.correct is False
        assert quality.recall == pytest.approx(0.8)
        assert quality.scoring_detail["components"]["q1.count"]["relative_error"] == pytest.approx(0.2)

    def test_codex_uses_the_same_continuous_component_fitness(self, script_run_codex: Any) -> None:
        """Codex consumes the shared Claude evaluator rather than maintaining a divergent count score.

        The same 10-versus-12 near miss must produce the same 0.8 component on both providers.
        """
        result = script_run_codex._default_evaluator(_rv_task(_count_question(10)), "12 undocumented symbols.")

        assert result.correct is False
        assert result.quality_score == pytest.approx(0.8)
        assert result.components["subanswer:q1.count"] == pytest.approx(0.8)

    def test_codex_preserves_shared_evaluator_extraction_failure(self, script_run_codex: Any, tmp_path: Path) -> None:
        """Codex keeps a scored evaluator extraction failure instead of reclassifying it as a valid result.

        The injected evaluator reports an extraction failure with no quality score; the runner result must carry it.
        """
        evaluator_result = core.EvaluationResult(scored=True, correct=False, quality_score=None, extraction_failed=True)
        runner = script_run_codex.CodexRunner(
            "gpt-6.1-sol",
            tmp_path,
            transport=lambda *_args, **_kwargs: _TURN_COMPLETED,
            evaluator=lambda *_args: evaluator_result,
        )

        result = runner.run({"id": "EV-01", "type": "demo", "prompt": "Answer."}, "A_plain")

        assert result.extraction_failed is True
        assert result.quality_score is None
        assert result.correct is False

    @pytest.mark.parametrize(
        "sub_question",
        [
            pytest.param({"id": "q1", "match": "unsupported", "ground_truth": {"count": 10}}, id="unknown-match"),
            pytest.param({"id": "q1", "match": "integer_extract", "ground_truth": {}}, id="missing-count"),
            "not-an-object",
        ],
    )
    def test_invalid_required_subquestions_fail_closed(self, script_run_bench: Any, sub_question: Any) -> None:
        """Invalid required review contracts raise instead of leaving a scoreable remainder.

        An unknown matcher, a missing count, or a non-object entry must not be skipped silently and score the rest.
        """
        task = _rv_task(sub_question, task_id="RV-invalid-contract")

        with pytest.raises(ValueError, match="review sub-question"):
            script_run_bench._evaluate_rv(task, "10 undocumented symbols.")

    def test_multiple_count_components_fail_closed_without_answer_scoping(self, script_run_bench: Any) -> None:
        """One unlabelled answer-region count cannot safely answer two distinct required questions.

        With two integer questions and an answer holding two numbers, there is no way to know which number answers which
        question, so scoring is refused.
        """
        task = _rv_task(_count_question(10), _count_question(20, "q2"), task_id="RV-ambiguous-counts")

        with pytest.raises(ValueError, match="multiple required count"):
            script_run_bench._evaluate_rv(task, "10 undocumented symbols; 20 uncovered symbols.")


class TestOracleViews:
    """Independent AST truth stays separate from Codemap's declaration-level view of the same code."""

    def test_cq01_keeps_unique_ast_oracle_separate_from_codemap_declarations(
        self, script_run_bench: Any, task_by_id: dict[str, dict[str, Any]]
    ) -> None:
        """CQ-01 scores seven unique AST names even when Codemap reports 11 declarations.

        Prevents the 11 Codemap declarations being scored as if they were the seven unique AST names.
        """
        task = task_by_id["CQ-01"]
        output = "\n".join(
            (
                "Codemap static view: 11 declarations.",
                "Independent AST view: 7 unique names.",
                "## Symbols",
                *task["ground_truth"]["undocumented_symbols"],
            )
        )

        quality = script_run_bench._evaluate_oss(task, output)

        views = quality.scoring_detail["oracle_views"]
        assert quality.correct is True
        assert quality.metric_expected == 7
        assert quality.metric_got == 7
        assert views["independent_ast"]["count"] == 7
        assert views["independent_ast"]["symbols"] == task["ground_truth"]["undocumented_symbols"]
        assert views["codemap_static"]["count"] == 11
        assert "declaration" in views["codemap_static"]["semantics"]

    @pytest.mark.parametrize(
        ("task_id", "output"),
        [
            pytest.param("CQ-01", "Independent AST: 7 unique names.", id="cq01-unique-qualified-names"),
            pytest.param("CQ-02", "11 uncovered symbols.", id="cq02-unique-public-names"),
        ],
    )
    def test_correct_count_alone_is_half_credit(
        self, script_run_bench: Any, task_by_id: dict[str, dict[str, Any]], task_id: str, output: str
    ) -> None:
        """A correct count without the requested unique names is half credit and an extraction failure.

        CQ-01 requires every requested unique qualified name and CQ-02 every requested unique public name, in addition
        to the independent AST count.
        """
        quality = script_run_bench._evaluate_oss(task_by_id[task_id], output)

        assert quality.correct is False
        assert quality.recall == pytest.approx(0.5)
        assert quality.extraction_failed is True

    def test_cq02_views_match_the_ground_truth(self, task_by_id: dict[str, dict[str, Any]]) -> None:
        """CQ-02's independent view lists the uncovered symbols and its Codemap view keeps the scan count.

        The two views must stay tied to their own ground-truth fields so neither silently replaces the other.
        """
        ground_truth = task_by_id["CQ-02"]["ground_truth"]

        assert ground_truth["oracle_views"]["independent_ast"]["symbols"] == ground_truth["uncovered_symbols"]
        assert ground_truth["oracle_views"]["codemap_static"]["count"] == ground_truth["uncovered_count_scan"]

    def test_rv05_exposes_independent_and_codemap_views_without_conflation(
        self, task_by_id: dict[str, dict[str, Any]]
    ) -> None:
        """RV-05 labels its independent AST evidence and its Codemap static evidence separately.

        Prevents the two views sharing one unlabelled count: they differ in content and in declared semantics.
        """
        task = task_by_id["RV-05"]
        views = task["ground_truth"]["oracle_views"]

        assert views["independent_ast"]["count"] == 11
        assert views["independent_ast"]["symbols"] == task["sub_questions"][1]["ground_truth"]["symbols"]
        assert views["codemap_static"] != views["independent_ast"]
        assert "count" in views["codemap_static"]
        assert views["independent_ast"]["semantics"] != views["codemap_static"]["semantics"]


class TestTaskAndSkillWording:
    """Task prompts and the Codemap query skills state the contract the oracles and runners rely on."""

    def test_codex_query_skill_allows_repository_reads_after_the_required_query(self) -> None:
        """The Codex query skill keeps its required query while allowing repository reads for a distinct oracle view.

        Phrases are matched after whitespace collapse so reflowing the skill prose cannot break the check.
        """
        skill = _flat(CODEX_QUERY_SKILL_PATH.read_text(encoding="utf-8")).lower()

        assert "ordinary repository reads remain allowed" in skill
        assert "distinct independent ast/oracle view" in skill

    @pytest.mark.parametrize("task_id", ["RV-05", "CQ-02"])
    def test_distinct_oracle_prompts_name_the_ast_oracle_and_codemap(
        self, task_by_id: dict[str, dict[str, Any]], task_id: str
    ) -> None:
        """Tasks with a distinct independent oracle say so and still mention Codemap.

        C keeps its required Skill/query evidence while repository inspection stays allowed for these tasks.
        """
        prompt = _flat(task_by_id[task_id]["prompt"]).lower()

        assert "independent ast oracle" in prompt
        assert "codemap" in prompt

    @pytest.mark.parametrize("task_id", ["SE-01", "SE-02"])
    def test_source_with_imports_prompts_name_imports(
        self, task_by_id: dict[str, dict[str, Any]], task_id: str
    ) -> None:
        """Source-with-imports task prompts explicitly name imports.

        The query skills key their ``--with-imports`` guidance on the prompt asking for imports.
        """
        assert "import" in task_by_id[task_id]["prompt"].lower()

    @pytest.mark.parametrize(
        "skill_path",
        [
            pytest.param(CODEX_QUERY_SKILL_PATH, id="codex-query-skill"),
            pytest.param(CLAUDE_QUERY_SKILL_PATH, id="claude-query-skill"),
        ],
    )
    def test_query_skills_require_with_imports_for_source_requests_naming_imports(self, skill_path: Path) -> None:
        """Both query skills document ``symbol <name> --with-imports`` and the ``query_complete`` signal.

        Prevents source-with-imports tasks from falling back to redundant repository reads.
        """
        skill = _flat(skill_path.read_text(encoding="utf-8"))

        assert "symbol <name> --with-imports" in skill
        assert "query_complete" in skill

    @pytest.mark.parametrize("task_id", ["RV-05", "CQ-02"])
    def test_uncovered_prompts_expose_every_referenced_identifier(
        self, task_by_id: dict[str, dict[str, Any]], task_id: str
    ) -> None:
        """Task wording names every Name/Attribute and patch-string reference the generator records.

        If the prompt asked for less than the oracle counts, a correct answer could still be scored as missing symbols.
        """
        prompt = _flat(task_by_id[task_id]["prompt"]).lower()

        assert "every ast `name` and `attribute` identifier" in prompt
        assert "final dotted component of every string argument" in prompt


class TestTreatmentAdherence:
    """Treatment adherence is a different fact from Codemap-use compliance."""

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
            pytest.param("C_strict", False, False, False, id="skill-required-use-not-observed"),
            pytest.param("C_strict", True, True, False, id="skill-contamination-breaks-adherence"),
        ],
    )
    def test_is_distinct_from_codemap_use_compliance(
        self, arm: str, compliance: bool | None, contaminated: bool, expected: bool
    ) -> None:
        """Adherence follows each arm's use contract, and contamination always breaks it.

        Plain A has no Codemap-use status, so its N/A must not read as failed adherence. B is an optional-use canary, so
        skipping queries is compliant; treating it as required-use would drop exactly the no-query cells from pooling
        and bias the pooled B result toward runs that happened to use Codemap. C keeps its required-use contract on both
        providers.
        """
        adherence = core.treatment_adherence(arm, codemap_use_compliance=compliance, contaminated=contaminated)

        assert adherence is expected


class TestTokenAccounting:
    """Native gross, cached, and fresh input token views stay consistent."""

    @pytest.mark.parametrize(
        ("input_tokens", "cached_input_tokens", "expected", "inconsistent"),
        [
            pytest.param(120, 80, 40, False, id="gross-minus-cached"),
            pytest.param(80, 80, 0, False, id="fully-cached"),
            pytest.param(25, 80, None, True, id="cache-over-gross-is-unscoreable"),
        ],
    )
    def test_fresh_tokens_expose_gross_cached_and_fresh_views(
        self, input_tokens: int, cached_input_tokens: int, expected: int | None, inconsistent: bool
    ) -> None:
        """Fresh tokens are gross minus cached, and a cache larger than gross is flagged instead of going negative.

        Prevents malformed native usage from becoming a plausible zero fresh-token value.
        """
        assert core.fresh_input_tokens(input_tokens, cached_input_tokens) == expected
        assert core.token_accounting_inconsistent(input_tokens, cached_input_tokens) is inconsistent

    @pytest.mark.parametrize(
        ("input_tokens", "cached_input_tokens"),
        [pytest.param(-1, 0, id="negative-gross"), pytest.param(0, -1, id="negative-cache")],
    )
    def test_fresh_tokens_reject_negative_native_usage(self, input_tokens: int, cached_input_tokens: int) -> None:
        """Native token counters cannot become plausible fresh-input values when malformed.

        A negative counter from a broken provider payload raises rather than flowing into pooled statistics.
        """
        with pytest.raises(ValueError, match="token"):
            core.fresh_input_tokens(input_tokens, cached_input_tokens)


class TestCanonicalResultRows:
    """Published sidecars are sorted views; the raw execution order is never rewritten."""

    def test_sorts_a_derived_view_without_reordering_raw_execution(self) -> None:
        """Canonical rows follow task, repetition, then arm order while the raw input list keeps its order.

        Prevents random append order from being overwritten while a reproducible sidecar order is published.
        """
        raw_rows = [
            {"task_id": "CQ-01", "repetition": 1, "arm": "C_strict", "execution_index": 0},
            {"task_id": "RV-02", "repetition": 2, "arm": "B_auto", "execution_index": 1},
            {"task_id": "RV-02", "repetition": 1, "arm": "C_strict", "execution_index": 2},
            {"task_id": "RV-02", "repetition": 1, "arm": "A_plain", "execution_index": 3},
            {"task_id": "RV-02", "repetition": 1, "arm": "B_auto", "execution_index": 4},
        ]

        canonical = core.canonical_result_rows(
            raw_rows, task_order=("RV-02", "CQ-01"), arm_order=("A_plain", "B_auto", "C_strict")
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
                [{"task_id": "unknown", "repetition": 1, "arm": "A_plain", "execution_index": 0}], id="unknown-task"
            ),
        ],
    )
    def test_rejects_ambiguous_or_unknown_coordinates(self, rows: list[dict[str, Any]]) -> None:
        """A sidecar must not present duplicate or unknown evidence as a complete canonical run.

        Two rows for one task, repetition and arm, or a task outside the declared order, make the run ambiguous.
        """
        with pytest.raises(ValueError, match=r"duplicate|unknown"):
            core.canonical_result_rows(rows, task_order=("RV-02",), arm_order=("A_plain",))


class TestCodexCanonicalSidecar:
    """The Codex runner keeps raw telemetry append-only and publishes a separate canonical sidecar."""

    def test_sidecar_is_sorted_while_raw_telemetry_keeps_execution_order(
        self, script_run_codex: Any, tmp_path: Path
    ) -> None:
        """Raw interruption evidence stays in execution order and the sidecar is sorted with a matching hash.

        Two cells are appended C first, then A; the raw file keeps that order, the sidecar lists A before C, and the
        returned hash is the digest of the sidecar bytes.
        """
        telemetry = tmp_path / "telemetry.jsonl"
        telemetry.touch()
        first = script_run_codex.CodexRun("C_strict", "SE-01", "symbol_extraction", "fixture")
        second = script_run_codex.CodexRun("A_plain", "SE-01", "symbol_extraction", "fixture")
        script_run_codex._append_run(telemetry, first, execution_index=0)
        script_run_codex._append_run(telemetry, second, execution_index=1)
        canonical_path = script_run_codex._canonical_telemetry_path(telemetry)

        sidecar_hash = script_run_codex._write_canonical_telemetry(telemetry, canonical_path, task_order=("SE-01",))

        raw_rows = [json.loads(line) for line in telemetry.read_text(encoding="utf-8").splitlines()]
        canonical_rows = [json.loads(line) for line in canonical_path.read_text(encoding="utf-8").splitlines()]
        assert [row["arm"] for row in raw_rows] == ["C_strict", "A_plain"]
        assert [row["execution_index"] for row in raw_rows] == [0, 1]
        assert [row["arm"] for row in canonical_rows] == ["A_plain", "C_strict"]
        assert hashlib.sha256(canonical_path.read_bytes()).hexdigest() == sidecar_hash

    def test_initial_metadata_marks_the_sidecar_not_written_and_non_poolable(
        self, script_run_codex: Any, tmp_path: Path
    ) -> None:
        """A freshly started run records its canonical sidecar as not written and not eligible for pooling.

        Until the run completes and the sidecar is written, partial evidence must never be pooled.
        """
        telemetry = tmp_path / "telemetry.jsonl"
        telemetry.touch()

        metadata = script_run_codex._initial_run_metadata(
            manifest_path=BENCHMARKS_DIR / "manifests" / "codex-integration.json",
            repo_path=tmp_path,
            index_path=None,
            output_path=telemetry,
            metadata_path=tmp_path / "run-metadata.json",
            model="gpt-6.1-sol",
            reasoning_effort="high",
            repetitions=1,
            task_arms={("SE-01", 1): ("A_plain", "B_auto", "C_strict")},
            cell_wall_clock_seconds=600,
            auth_provisioned=False,
        )

        assert metadata["artifacts"]["canonical_telemetry_status"] == "not_written"
        assert metadata["artifacts"]["canonical_telemetry_pooling_eligible"] is False

    def test_result_block_presents_persisted_arms_in_fixed_order(
        self, script_run_codex: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Human output stays task-first and A/B/C even when raw execution order is randomized.

        Rows arrive C, A, B; they print A, B, C with continuing progress counters, and the next counter is returned.
        """
        printed: list[tuple[str, str]] = []
        monkeypatch.setattr(script_run_codex.runtime, "print_arm_row", lambda row, arm: printed.append((arm, row)))

        next_progress = script_run_codex._print_result_block(
            (("C_strict", "C row"), ("A_plain", "A row"), ("B_auto", "B row")), printed_cells=4, planned_cells=9
        )

        assert printed == [("A_plain", "(5/9) A row"), ("B_auto", "(6/9) B row"), ("C_strict", "(7/9) C row")]
        assert next_progress == 7
