"""Prospective pass rates and quality-gated paired agentic efficiency contracts."""

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
def test_pass_requires_fully_correct_admitted_completion(changes: dict) -> None:
    """Transport success or near-perfect partial credit must never count as pass."""
    assert cell_passes(_passing_row("task", "A_plain"))
    assert not cell_passes(_passing_row("task", "A_plain", **changes))


def test_planned_denominator_retains_execution_failures_and_unobserved_cells() -> None:
    """An interrupted run must not report perfect accuracy on its surviving answers."""
    rows = [_passing_row("one", "A_plain"), _passing_row("two", "A_plain", success=False)]
    summary = summarize_agentic(rows, task_ids=["one", "two", "three"], repetitions=1)
    plain = summary["all_assigned"]["A_plain"]
    assert (plain["passed_cells"], plain["assigned_cells"], plain["observed_cells"], plain["unobserved_cells"]) == (
        1,
        3,
        2,
        1,
    )
    assert plain["pass_rate"] == 1 / 3
    assert plain["component_mean"] == 1.0
    assert plain["component_scored_cells"] == 2
    assert plain["failure_counts"] == {"execution_failure": 1, "unobserved": 1}
    output = "\n".join(line for line, _ in summary_lines(summary))
    assert "pass=1/3" in output and "component=1.000" in output
    assert "PASS=" not in output and "COMPONENT=" not in output
    assert "SCORE=" not in output


def test_efficiency_only_uses_both_passing_pairs_and_reports_transitions() -> None:
    """Cheap incorrect or non-adherent answers cannot improve an efficiency headline."""
    rows = []
    for task in ["both", "improvement", "regression", "neither"]:
        for arm in ["A_plain", "C_strict"]:
            passes = (
                task == "both"
                or (task == "improvement" and arm == "C_strict")
                or (task == "regression" and arm == "A_plain")
            )
            rows.append(_passing_row(task, arm, quality={"correct": passes, "quality_score": 1.0 if passes else 0.5}))
    rows[1].update(input_tokens=80, cached_input_tokens=60, fresh_input_tokens=20, output_tokens=10, elapsed_s=5)
    summary = summarize_agentic(
        rows, task_ids=["both", "improvement", "regression", "neither", "unobserved"], repetitions=1
    )
    comparison = summary["comparisons"]["C_strict"]
    assert comparison["outcomes"] == {"both_pass": 1, "improved": 1, "regressed": 1, "both_fail": 1, "unobserved": 1}
    fresh = comparison["efficiency"]["fresh_input_tokens"]
    assert fresh["paired_cells"] == 1
    assert fresh["median_percent_change"] == -50.0
    assert comparison["efficiency"]["input_tokens"]["median_percent_change"] == -20.0
    assert comparison["efficiency"]["elapsed_s"]["median_percent_change"] == -50.0


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(None, id="missing"),
        -1,
        math.nan,
        math.inf,
        True,
    ],
)
def test_unavailable_measurements_never_become_zero_savings(invalid: object) -> None:
    """Each efficiency metric reports its own missing-data population."""
    rows = [_passing_row("one", "A_plain"), _passing_row("one", "C_strict", elapsed_s=invalid)]
    result = summarize_agentic(rows, task_ids=["one"], repetitions=1)["comparisons"]["C_strict"]["efficiency"]
    assert result["elapsed_s"]["paired_cells"] == 0
    assert result["elapsed_s"]["unavailable_pairs"] == 1
    assert result["elapsed_s"]["median_percent_change"] is None
    assert result["input_tokens"]["paired_cells"] == 1


def test_zero_baseline_and_inconsistent_cache_are_explicitly_ineligible() -> None:
    """Zero-denominator ratios and inconsistent cache accounting have separate counters."""
    rows = [_passing_row("one", "A_plain", elapsed_s=0), _passing_row("one", "C_strict", cached_input_tokens=101)]
    result = summarize_agentic(rows, task_ids=["one"], repetitions=1)["comparisons"]["C_strict"]["efficiency"]
    assert result["elapsed_s"]["zero_baseline_pairs"] == 1
    assert result["elapsed_s"]["median_percent_change"] is None
    assert result["fresh_input_tokens"]["unavailable_pairs"] == 1


def test_failure_categories_count_cells_once_and_keep_exact_details() -> None:
    """Multiple wrong counts retain detail without inflating failed-cell category counts."""
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


def test_duplicate_and_out_of_scope_coordinates_fail_closed() -> None:
    """Duplicate rows or foreign coordinates must not silently alter the denominator."""
    row = _passing_row("one", "A_plain")
    with pytest.raises(ValueError, match="duplicate"):
        summarize_agentic([row, row], task_ids=["one"], repetitions=1)
    with pytest.raises(ValueError, match="scope"):
        summarize_agentic([row], task_ids=["different"], repetitions=1)


def test_unscored_valid_answer_has_an_explicit_failure_reason() -> None:
    """Missing scorer output must not leave a failing cell without diagnostic evidence."""
    row = _passing_row("one", "A_plain", quality=None)
    result = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["A_plain"])
    assert result["failures"][0]["details"] == [{"category": "unscored_answer"}]


def test_graded_quality_retains_small_errors_without_hiding_exact_mismatches() -> None:
    """A one-count error gets proportional credit, not full correctness or zero quality."""
    rows = [
        _passing_row("one", "A_plain"),
        _passing_row("one", "C_strict", quality={"correct": False, "quality_score": 0.0, "graded_score": 56 / 57}),
    ]
    summary = summarize_agentic(rows, task_ids=["one"], repetitions=1)
    strict = summary["all_assigned"]["C_strict"]
    assert strict["quality_mean"] == pytest.approx(56 / 57)
    assert strict["passed_cells"] == 0
    comparison = summary["comparisons"]["C_strict"]
    assert comparison["graded_quality"]["lower"] == 1
    assert comparison["graded_quality"]["mean_percentage_point_change"] == pytest.approx(-100 / 57)
    assert comparison["efficiency"]["input_tokens"]["paired_cells"] == 0
    output = "\n".join(line for line, _ in summary_lines(summary))
    assert "quality=98.2%" in output and "exact_pass=0/1" in output


@pytest.mark.parametrize("flag", ["success", "answer_contract_valid", "answer_pooling_eligible", "treatment_adherence"])
def test_graded_headline_includes_nonadmitted_and_unobserved_zeros(flag: str) -> None:
    """Perfect recovered answers cannot erase execution, format, treatment, or missing cells."""
    rows = [_passing_row("one", "A_plain"), _passing_row("two", "A_plain", **{flag: False})]
    summary = summarize_agentic(rows, task_ids=["one", "two", "three"], repetitions=1, arms=["A_plain"])
    assert summary["all_assigned"]["A_plain"]["quality_mean"] == pytest.approx(1 / 3)


def test_legacy_rows_do_not_masquerade_as_new_graded_scores() -> None:
    """A legacy partial-credit score must not be relabeled as proportional grading."""
    row = _passing_row("one", "A_plain", quality={"correct": True, "quality_score": 1.0})
    summary = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["A_plain"])
    assert summary["all_assigned"]["A_plain"]["quality_mean"] is None
    assert summary["all_assigned"]["A_plain"]["quality_unavailable_cells"] == 1


def test_graded_trailing_rank_cannot_enter_exact_pass_efficiency() -> None:
    """Legacy perfect-prefix credit cannot admit a prospectively overlong ranking."""
    details = [{"category": "unexpected_facts", "field": "ranking", "expected": [], "actual": ["pkg.extra"]}]
    row = _passing_row(
        "one",
        "C_strict",
        quality={"correct": True, "quality_score": 1.0, "graded_score": 2 / 3},
        failure_details=details,
    )
    assert not cell_passes(row)
    summary = summarize_agentic([_passing_row("one", "A_plain"), row], task_ids=["one"], repetitions=1)
    assert summary["comparisons"]["C_strict"]["efficiency"]["elapsed_s"]["paired_cells"] == 0
    assert summary["all_assigned"]["C_strict"]["passed_cells"] == 0
    assert next(item for item in summary["failures"] if item["arm"] == "C_strict")["details"] == details


@pytest.mark.parametrize("grade", [None, -1, 1.1, True, math.nan, math.inf])
def test_invalid_grade_is_unknown_and_not_exact(grade: object) -> None:
    """Native malformed grades must not become perfect answers or fabricated numeric credit."""
    row = _passing_row("one", "A_plain", quality={"correct": True, "quality_score": 1.0, "graded_score": grade})
    assert not cell_passes(row)
    summary = summarize_agentic([row], task_ids=["one"], repetitions=1, arms=["A_plain"])
    assert summary["all_assigned"]["A_plain"]["quality_mean"] is None


def test_historical_summary_keeps_pass_labels() -> None:
    """Previously saved summaries remain readable without new grading keys or relabeling."""
    summary = summarize_agentic([_passing_row("one", "A_plain")], task_ids=["one"], repetitions=1, arms=["A_plain"])
    summary["reporting_version"] = "agentic-pass-v1"
    del summary["all_assigned"]["A_plain"]["quality_mean"]
    del summary["all_assigned"]["A_plain"]["quality_unavailable_cells"]
    output = "\n".join(line for line, _ in summary_lines(summary))
    assert "pass=1/1" in output and "quality=" not in output and "exact_pass=" not in output
