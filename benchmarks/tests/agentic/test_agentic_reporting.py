"""Prospective pass rates and quality-gated paired agentic efficiency contracts.

Summaries are built from synthetic answer rows, so every expectation here is a reporting rule (what counts as a pass,
which cells stay in the denominator, which pairs may feed an efficiency headline) rather than a property of a model run.
"""

import math

import pytest
from _bench_common.agentic_reporting import cell_passes, summarize_agentic, summary_lines


def _passing_row(task: str, arm: str, **changes: object) -> dict:
    """Create one fully admitted answer with native token and time measurements."""
    return {
        "task_id": task,
        "repetition": 1,
        "arm": arm,
        "success": True,
        "answer_contract_valid": True,
        "answer_pooling_eligible": True,
        "treatment_adherence": True,
        "quality": {"correct": True, "quality_score": 1.0, "graded_score": 1.0, "components": {"rdep_counts": 1.0}},
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "fresh_input_tokens": 40,
        "output_tokens": 20,
        "elapsed_s": 10.0,
        **changes,
    }


def _rendered(summary: dict) -> str:
    """Join the printable summary lines, dropping their style tags."""
    return "\n".join(line for line, _ in summary_lines(summary))


class TestCellPasses:
    """The exact-pass predicate applied to one answer row."""

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"success": False}, id="execution"),
            pytest.param({"answer_contract_valid": False}, id="envelope"),
            pytest.param({"answer_pooling_eligible": False}, id="pooling"),
            pytest.param({"treatment_adherence": False}, id="treatment"),
            pytest.param({"incomplete": True}, id="incomplete"),
            pytest.param({"contaminated": True}, id="contaminated"),
            pytest.param({"diagnostic_only": True}, id="diagnostic"),
            pytest.param({"quality": {"correct": False, "quality_score": 0.999}}, id="partial"),
            pytest.param({"quality": None}, id="absent"),
        ],
    )
    def test_requires_fully_correct_admitted_completion(self, changes: dict) -> None:
        """Transport success or near-perfect partial credit never counts as a pass.

        A fully admitted, fully correct row passes; each single defect (failed run, bad envelope, pooling or treatment
        violation, incomplete, contaminated, diagnostic-only, partial or missing quality) flips it to a non-pass.
        """
        assert cell_passes(_passing_row("task", "A_plain"))
        assert not cell_passes(_passing_row("task", "A_plain", **changes))

    @pytest.mark.parametrize("grade", [None, -1, 1.1, True, math.nan, math.inf])
    def test_rejects_invalid_grade_as_unknown(self, grade: object) -> None:
        """Malformed native grades neither pass nor become fabricated numeric credit.

        A row claiming exact correctness but carrying an out-of-range, boolean, non-finite or missing graded score is
        not an exact pass, and its arm reports no quality mean at all.
        """
        row = _passing_row("one", "A_plain", quality={"correct": True, "quality_score": 1.0, "graded_score": grade})
        summary = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["A_plain"])

        assert not cell_passes(row)
        assert summary["all_assigned"]["A_plain"]["quality_mean"] is None


class TestSummarizeAgentic:
    """Denominators, failure accounting and scope checks of the per-arm summary."""

    def test_planned_denominator_retains_failures_and_unobserved_cells(self) -> None:
        """An interrupted run does not report perfect accuracy on its surviving answers.

        Three tasks are planned, one passes, one fails to execute and one never ran: the rate is 1/3 and the headline
        wording names pass/component rather than the legacy uppercase labels.
        """
        rows = [_passing_row("one", "A_plain"), _passing_row("two", "A_plain", success=False)]
        summary = summarize_agentic(rows, task_ids=["one", "two", "three"], repetitions=1)
        plain = summary["all_assigned"]["A_plain"]
        output = _rendered(summary)

        assert (plain["passed_cells"], plain["assigned_cells"], plain["observed_cells"], plain["unobserved_cells"]) == (
            (1, 3, 2, 1)
        )
        assert plain["pass_rate"] == 1 / 3
        assert plain["component_mean"] == 1.0
        assert plain["component_scored_cells"] == 2
        assert plain["failure_counts"] == {"execution_failure": 1, "unobserved": 1}
        assert "pass=1/3" in output
        assert "component=1.000" in output
        assert not any(label in output for label in ("PASS=", "COMPONENT=", "SCORE="))

    def test_counts_failed_cell_once_per_category_and_keeps_exact_details(self) -> None:
        """Multiple wrong counts keep their detail without inflating the failed-cell category counts.

        One cell has two wrong rdep counts and also violated the treatment: each category counts the cell once while
        the failure list still carries both exact detail records.
        """
        details = [
            {"category": "wrong_counts", "field": "rdep_counts", "key": key, "expected": 2, "actual": 4}
            for key in ["pkg.one", "pkg.two"]
        ]
        row = _passing_row(
            "one",
            "C_strict",
            quality={"correct": False, "quality_score": 0.5},
            failure_details=details,
            treatment_adherence=False,
        )
        summary = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["C_strict"])

        assert summary["all_assigned"]["C_strict"]["failure_counts"] == {"wrong_counts": 1, "treatment_violation": 1}
        assert summary["failures"][0]["details"][:2] == details

    @pytest.mark.parametrize(
        ("row_copies", "task_ids", "message"),
        [
            pytest.param(2, ["one"], "duplicate", id="duplicate-row"),
            pytest.param(1, ["different"], "scope", id="foreign-coordinate"),
        ],
    )
    def test_rejects_duplicate_and_out_of_scope_coordinates(
        self, row_copies: int, task_ids: list[str], message: str
    ) -> None:
        """Duplicate rows or foreign coordinates fail closed instead of silently altering the denominator.

        The same task and arm reported twice, or a row for a task outside the planned set, would shift the pass rate
        without any visible error if accepted.
        """
        rows = [_passing_row("one", "A_plain")] * row_copies

        with pytest.raises(ValueError, match=message):
            summarize_agentic(rows, task_ids=task_ids, repetitions=1)

    def test_unscored_valid_answer_gets_explicit_failure_reason(self) -> None:
        """A valid answer with no scorer output still carries a diagnostic failure record.

        Without it the cell would fail with no evidence of why, indistinguishable from a scoring bug.
        """
        row = _passing_row("one", "A_plain", quality=None)
        result = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["A_plain"])

        assert result["failures"][0]["details"] == [{"category": "unscored_answer"}]

    def test_historical_summary_keeps_pass_labels(self) -> None:
        """Previously saved summaries stay readable without new grading keys or relabeling.

        A v1 summary lacks the quality keys added later; rendering it must print only the pass line.
        """
        summary = summarize_agentic([_passing_row("one", "A_plain")], task_ids=["one"], repetitions=1, arms=["A_plain"])
        summary["reporting_version"] = "agentic-pass-v1"
        del summary["all_assigned"]["A_plain"]["quality_mean"]
        del summary["all_assigned"]["A_plain"]["quality_unavailable_cells"]
        output = _rendered(summary)

        assert "pass=1/1" in output
        assert "quality=" not in output
        assert "exact_pass=" not in output


class TestEfficiencyPairs:
    """Paired efficiency comparison between the baseline arm and a treatment arm."""

    def test_uses_only_both_passing_pairs_and_reports_transitions(self) -> None:
        """Cheap incorrect or non-adherent answers cannot improve an efficiency headline.

        Four tasks cover both-pass, improved, regressed and both-fail pairs plus one unobserved task; only the single
        both-pass pair feeds the median change, and its cheaper treatment shows the exact percentage drops.
        """
        rows = []
        for task in ["both", "improvement", "regression", "neither"]:
            for arm in ["A_plain", "C_strict"]:
                passes = (
                    task == "both"
                    or (task == "improvement" and arm == "C_strict")
                    or (task == "regression" and arm == "A_plain")
                )
                rows.append(
                    _passing_row(task, arm, quality={"correct": passes, "quality_score": 1.0 if passes else 0.5})
                )
        rows[1].update(input_tokens=80, cached_input_tokens=60, fresh_input_tokens=20, output_tokens=10, elapsed_s=5)
        summary = summarize_agentic(
            rows, task_ids=["both", "improvement", "regression", "neither", "unobserved"], repetitions=1
        )
        comparison = summary["comparisons"]["C_strict"]
        efficiency = comparison["efficiency"]

        assert comparison["outcomes"] == {
            "both_pass": 1,
            "improved": 1,
            "regressed": 1,
            "both_fail": 1,
            "unobserved": 1,
        }
        assert efficiency["fresh_input_tokens"]["paired_cells"] == 1
        assert efficiency["fresh_input_tokens"]["median_percent_change"] == -50.0
        assert efficiency["input_tokens"]["median_percent_change"] == -20.0
        assert efficiency["elapsed_s"]["median_percent_change"] == -50.0

    @pytest.mark.parametrize("invalid", [pytest.param(None, id="missing"), -1, math.nan, math.inf, True])
    def test_unavailable_measurement_never_becomes_zero_savings(self, invalid: object) -> None:
        """Each efficiency metric reports its own missing-data population.

        The treatment row has an unusable elapsed time (absent, negative, non-finite or boolean): that metric drops the
        pair as unavailable while the token metrics still pair.
        """
        rows = [_passing_row("one", "A_plain"), _passing_row("one", "C_strict", elapsed_s=invalid)]
        result = summarize_agentic(rows, task_ids=["one"], repetitions=1)["comparisons"]["C_strict"]["efficiency"]

        assert result["elapsed_s"]["paired_cells"] == 0
        assert result["elapsed_s"]["unavailable_pairs"] == 1
        assert result["elapsed_s"]["median_percent_change"] is None
        assert result["input_tokens"]["paired_cells"] == 1

    def test_zero_baseline_and_inconsistent_cache_are_explicitly_ineligible(self) -> None:
        """Zero-denominator ratios and inconsistent cache accounting have separate counters.

        A zero-second baseline cannot give a percentage change, and cached tokens above total input make the fresh-token
        pair unavailable.
        """
        rows = [_passing_row("one", "A_plain", elapsed_s=0), _passing_row("one", "C_strict", cached_input_tokens=101)]
        result = summarize_agentic(rows, task_ids=["one"], repetitions=1)["comparisons"]["C_strict"]["efficiency"]

        assert result["elapsed_s"]["zero_baseline_pairs"] == 1
        assert result["elapsed_s"]["median_percent_change"] is None
        assert result["fresh_input_tokens"]["unavailable_pairs"] == 1

    def test_graded_trailing_rank_cannot_enter_exact_pass_efficiency(self) -> None:
        """Legacy perfect-prefix credit cannot admit a prospectively overlong ranking.

        The row claims exact correctness but carries an unexpected extra ranking item and a graded score below 1: it is
        no pass, joins no efficiency pair, and keeps its failure detail.
        """
        details = [{"category": "unexpected_facts", "field": "ranking", "expected": [], "actual": ["pkg.extra"]}]
        row = _passing_row(
            "one",
            "C_strict",
            quality={"correct": True, "quality_score": 1.0, "graded_score": 2 / 3},
            failure_details=details,
        )
        summary = summarize_agentic([_passing_row("one", "A_plain"), row], task_ids=["one"], repetitions=1)

        assert not cell_passes(row)
        assert summary["comparisons"]["C_strict"]["efficiency"]["elapsed_s"]["paired_cells"] == 0
        assert summary["all_assigned"]["C_strict"]["passed_cells"] == 0
        assert next(item for item in summary["failures"] if item["arm"] == "C_strict")["details"] == details


class TestGradedQuality:
    """Proportional graded credit reported beside, never instead of, the exact pass."""

    def test_retains_small_errors_without_hiding_exact_mismatches(self) -> None:
        """A one-count error gets proportional credit, not full correctness or zero quality.

        The treatment answer is off by one of 57: its quality mean is 56/57, it is not an exact pass, it is a lower
        graded pair, and the rendered line shows both the percentage and the exact-pass count.
        """
        rows = [
            _passing_row("one", "A_plain"),
            _passing_row("one", "C_strict", quality={"correct": False, "quality_score": 0.0, "graded_score": 56 / 57}),
        ]
        summary = summarize_agentic(rows, task_ids=["one"], repetitions=1)
        strict = summary["all_assigned"]["C_strict"]
        comparison = summary["comparisons"]["C_strict"]
        output = _rendered(summary)

        assert strict["quality_mean"] == pytest.approx(56 / 57)
        assert strict["passed_cells"] == 0
        assert comparison["graded_quality"]["lower"] == 1
        assert comparison["graded_quality"]["mean_percentage_point_change"] == pytest.approx(-100 / 57)
        assert comparison["efficiency"]["input_tokens"]["paired_cells"] == 0
        assert "quality=98.2%" in output
        assert "exact_pass=0/1" in output

    @pytest.mark.parametrize(
        "flag", ["success", "answer_contract_valid", "answer_pooling_eligible", "treatment_adherence"]
    )
    def test_headline_counts_nonadmitted_and_unobserved_cells_as_zero(self, flag: str) -> None:
        """Perfect recovered answers cannot erase execution, format, treatment, or missing cells.

        Of three planned tasks one is perfect, one is perfect but not admitted (the parametrized flag is off) and one
        never ran, so the graded headline is 1/3.
        """
        rows = [_passing_row("one", "A_plain"), _passing_row("two", "A_plain", **{flag: False})]
        summary = summarize_agentic(rows, task_ids=["one", "two", "three"], repetitions=1, arms=["A_plain"])

        assert summary["all_assigned"]["A_plain"]["quality_mean"] == pytest.approx(1 / 3)

    def test_legacy_rows_do_not_masquerade_as_graded_scores(self) -> None:
        """A legacy partial-credit score is not relabeled as proportional grading.

        A row with a quality score but no graded score has an unknown quality mean and counts as unavailable.
        """
        row = _passing_row("one", "A_plain", quality={"correct": True, "quality_score": 1.0})
        summary = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["A_plain"])

        assert summary["all_assigned"]["A_plain"]["quality_mean"] is None
        assert summary["all_assigned"]["A_plain"]["quality_unavailable_cells"] == 1
