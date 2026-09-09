"""Report prospective agentic success and efficiency against the complete assigned scope.

Both adapters normalize native rows here. Missing cells stay in the quality denominator. Graded quality and exact
correctness remain separate. Efficiency requires two fully passing answers. All-assigned outcomes remain visible. This
module reads no files and cannot rewrite historical artifacts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import math
from statistics import median
from typing import Any

from _bench_common.agentic_contracts import AGENTIC_ARMS


REPORTING_VERSION = "agentic-graded-v2"
EFFICIENCY_FIELDS = ("input_tokens", "fresh_input_tokens", "output_tokens", "elapsed_s")


def _number(value: Any) -> float | None:
    """Accept finite non-negative native measurements, never boolean or missing values."""
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _component(row: Mapping[str, Any]) -> float | None:
    """Read valid partial credit without treating an unscored answer as zero."""
    quality = row.get("quality")
    score = _number(quality.get("quality_score")) if isinstance(quality, Mapping) else None
    return score if score is not None and score <= 1 else None


def cell_passes(row: Mapping[str, Any]) -> bool:
    """Require exact correctness, valid format, completion, and assigned treatment adherence."""
    quality = row.get("quality")
    return (
        isinstance(quality, Mapping)
        and quality.get("correct") is True
        and _component(row) == 1.0
        and ("graded_score" not in quality or _number(quality["graded_score"]) == 1.0)
        and all(
            row.get(key) is True
            for key in ("success", "answer_contract_valid", "answer_pooling_eligible", "treatment_adherence")
        )
        and not any(row.get(key) for key in ("incomplete", "contaminated", "diagnostic_only"))
    )


def cell_quality(row: Mapping[str, Any]) -> float | None:
    """Return admitted graded credit, zero for invalid cells, or unknown for absent grading.

    Legacy partial-credit fields cannot substitute for the new grading contract. Missing assigned cells and failed
    execution/format/treatment gates contribute zero, even when a correct answer was recovered diagnostically.
    """
    if not all(
        row.get(key) is True
        for key in ("success", "answer_contract_valid", "answer_pooling_eligible", "treatment_adherence")
    ) or any(row.get(key) for key in ("incomplete", "contaminated", "diagnostic_only")):
        return 0.0
    quality = row.get("quality")
    value = _number(quality.get("graded_score")) if isinstance(quality, Mapping) else None
    return value if value is not None and value <= 1 else None


def cell_failure_details(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Combine semantic evidence with independent harness failures without guessing missing facts."""
    if row.get("observed") is False:
        return [{"category": "unobserved"}]
    details = [
        dict(item)
        for item in (row.get("failure_details") or [])
        if isinstance(item, Mapping) and isinstance(item.get("category"), str)
    ]
    if row.get("success") is not True or row.get("incomplete"):
        details.append({"category": "execution_failure", "error_type": row.get("error_type") or "incomplete_or_failed"})
    if (
        row.get("answer_contract_valid") is not True
        or row.get("answer_pooling_eligible") is not True
        or row.get("diagnostic_only")
    ):
        details.append(
            {"category": "formatting_failure", "error": row.get("answer_error") or "invalid_or_missing_envelope"}
        )
    if row.get("treatment_adherence") is not True:
        details.append({"category": "treatment_violation"})
    if row.get("contaminated"):
        details.append({"category": "contamination"})
    quality = row.get("quality")
    if not details and not cell_passes(row):
        if isinstance(quality, Mapping):
            details.append({"category": "semantic_mismatch", "components": quality.get("components", {})})
        else:
            details.append({"category": "unscored_answer"})
    unique = []
    for detail in details:
        if detail not in unique:
            unique.append(detail)
    return unique


def _measurement(row: Mapping[str, Any], field: str) -> float | None:
    """Reject incomplete usage and inconsistent cache data before deriving fresh input."""
    if field == "elapsed_s":
        return _number(row.get(field))
    if row.get("usage_complete") is False or row.get("token_accounting_inconsistent") is True:
        return None
    if field != "fresh_input_tokens":
        return _number(row.get(field))
    gross, cached = _number(row.get("input_tokens")), _number(row.get("cached_input_tokens"))
    if gross is None or cached is None or cached > gross:
        return None
    fresh = _number(row.get("fresh_input_tokens", gross - cached))
    return fresh if fresh == gross - cached else None


def _efficiency(pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], field: str) -> dict[str, Any]:
    """Summarize per-pair percent changes and native totals with explicit exclusion counts."""
    values: list[tuple[float, float]] = []
    unavailable = zero = 0
    for control, treatment in pairs:
        left, right = _measurement(control, field), _measurement(treatment, field)
        if left is None or right is None:
            unavailable += 1
        elif left == 0:
            zero += 1
        else:
            values.append((left, right))
    changes = [100 * (right - left) / left for left, right in values]
    return {
        "paired_cells": len(values),
        "unavailable_pairs": unavailable,
        "zero_baseline_pairs": zero,
        "median_percent_change": median(changes) if changes else None,
        "control_total": sum(left for left, _ in values) if values else None,
        "treatment_total": sum(right for _, right in values) if values else None,
        "treatment_lower": sum(right < left for left, right in values),
        "treatment_higher": sum(right > left for left, right in values),
        "tie": sum(right == left for left, right in values),
    }


def summarize_agentic(
    rows: Sequence[Mapping[str, Any]], *, task_ids: Sequence[str], repetitions: int, arms: Sequence[str] = AGENTIC_ARMS
) -> dict[str, Any]:
    """Report all assigned outcomes and both-passing paired efficiency within one model.

    Duplicate or foreign coordinates fail closed. Unobserved cells count as non-passes, but remain distinguishable from
    observed failures; their pairwise transitions are unknown, not fabricated improvements or regressions.
    """
    if type(repetitions) is not int or repetitions < 1 or not task_ids or len(set(task_ids)) != len(task_ids):
        raise ValueError("invalid assigned task/repetition scope")
    if not arms or len(set(arms)) != len(arms) or any(arm not in AGENTIC_ARMS for arm in arms):
        raise ValueError("invalid assigned arm scope")
    assigned = {(task, rep, arm) for task in task_ids for rep in range(1, repetitions + 1) for arm in arms}
    observed: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for row in rows:
        coordinate = (row.get("task_id"), row.get("repetition"), row.get("arm"))
        if type(coordinate[1]) is not int or coordinate not in assigned:
            raise ValueError(f"result outside assigned scope: {coordinate}")
        if coordinate in observed:
            raise ValueError(f"duplicate result coordinate: {coordinate}")
        observed[coordinate] = row
    failures = []
    totals = {}
    for arm in arms:
        arm_rows = [row for (*_, row_arm), row in observed.items() if row_arm == arm]
        passed = sum(cell_passes(row) for row in arm_rows)
        component = [value for row in arm_rows if (value := _component(row)) is not None]
        categories: Counter[str] = Counter()
        for task in task_ids:
            for rep in range(1, repetitions + 1):
                row = observed.get((task, rep, arm), {"observed": False})
                if cell_passes(row):
                    continue
                details = cell_failure_details(row)
                categories.update({item["category"] for item in details})
                failures.append({"task_id": task, "repetition": rep, "arm": arm, "details": details})
        denominator = len(task_ids) * repetitions
        grades = [cell_quality(row) for row in arm_rows]
        unavailable = sum(value is None for value in grades)
        totals[arm] = {
            "assigned_cells": denominator,
            "observed_cells": len(arm_rows),
            "unobserved_cells": denominator - len(arm_rows),
            "passed_cells": passed,
            "failed_cells": denominator - passed,
            "pass_rate": passed / denominator,
            "quality_mean": None if unavailable else sum(value for value in grades if value is not None) / denominator,
            "quality_unavailable_cells": unavailable,
            "component_mean": sum(component) / len(component) if component else None,
            "component_scored_cells": len(component),
            "failure_counts": dict(sorted(categories.items())),
        }
    comparisons = {}
    for treatment in (arm for arm in arms if arm != "A_plain" and "A_plain" in arms):
        outcomes = dict.fromkeys(("both_pass", "improved", "regressed", "both_fail", "unobserved"), 0)
        pairs = []
        coordinates = []
        grade_changes = []
        grade_unavailable = 0
        for task in task_ids:
            for rep in range(1, repetitions + 1):
                left, right = observed.get((task, rep, "A_plain")), observed.get((task, rep, treatment))
                if left is None or right is None:
                    outcome = "unobserved"
                else:
                    left_grade, right_grade = cell_quality(left), cell_quality(right)
                    if left_grade is None or right_grade is None:
                        grade_unavailable += 1
                    else:
                        grade_changes.append(100 * (right_grade - left_grade))
                    control_pass, treatment_pass = cell_passes(left), cell_passes(right)
                    outcome = {
                        (True, True): "both_pass",
                        (False, True): "improved",
                        (True, False): "regressed",
                        (False, False): "both_fail",
                    }[(control_pass, treatment_pass)]
                    if outcome == "both_pass":
                        pairs.append((left, right))
                outcomes[outcome] += 1
                coordinates.append({"task_id": task, "repetition": rep, "outcome": outcome})
        comparisons[treatment] = {
            "diagnostic_only": treatment == "B_auto",
            "outcomes": outcomes,
            "coordinates": coordinates,
            "efficiency": {field: _efficiency(pairs, field) for field in EFFICIENCY_FIELDS},
            "graded_quality": {
                "paired_cells": len(grade_changes),
                "unavailable_pairs": grade_unavailable,
                "unobserved_pairs": outcomes["unobserved"],
                "higher": sum(value > 0 for value in grade_changes),
                "lower": sum(value < 0 for value in grade_changes),
                "tie": sum(value == 0 for value in grade_changes),
                "mean_percentage_point_change": sum(grade_changes) / len(grade_changes) if grade_changes else None,
            },
        }
    return {
        "reporting_version": REPORTING_VERSION,
        "all_assigned": totals,
        "comparisons": comparisons,
        "failures": failures,
    }


def summary_lines(summary: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """Render versioned graded headlines without relabeling historical pass summaries."""
    lines = []
    graded = summary.get("reporting_version") == REPORTING_VERSION
    for arm, values in summary["all_assigned"].items():
        component = values["component_mean"]
        component_text = "n/a" if component is None else f"{component:.3f}"
        headline = "pass="
        if graded:
            quality = values["quality_mean"]
            quality_text = "n/a" if quality is None else f"{100 * quality:.1f}%"
            headline = f"quality={quality_text} quality_unavailable={values['quality_unavailable_cells']} exact_pass="
        lines.append(
            (
                f"SUMMARY  cohort=all_assigned  {arm:<10} {headline}{values['passed_cells']}/{values['assigned_cells']} ({100 * values['pass_rate']:.1f}%) component={component_text} scored={values['component_scored_cells']} observed={values['observed_cells']} unobserved={values['unobserved_cells']}",
                arm,
            )
        )
        lines.append(
            (
                f"FAILURES  {arm:<10} "
                + " ".join(f"{category}={count}" for category, count in values["failure_counts"].items()),
                arm,
            )
        )
    for arm, comparison in summary["comparisons"].items():
        if graded:
            quality = comparison["graded_quality"]
            delta = quality["mean_percentage_point_change"]
            change = "n/a" if delta is None else f"{delta:+.2f}pp"
            lines.append(
                (
                    f"quality  A_plain-vs-{arm} n={quality['paired_cells']} mean_change={change} higher={quality['higher']} lower={quality['lower']} tie={quality['tie']} unavailable={quality['unavailable_pairs']} unobserved={quality['unobserved_pairs']}",
                    None,
                )
            )
        lines.append(
            (
                f"COMPARISON  A_plain-vs-{arm} diagnostic={str(comparison['diagnostic_only']).lower()} "
                + " ".join(f"{key}={count}" for key, count in comparison["outcomes"].items()),
                None,
            )
        )
        for field, metric in comparison["efficiency"].items():
            delta = metric["median_percent_change"]
            change = "n/a" if delta is None else f"{delta:+.1f}%"
            lines.append(
                (
                    f"efficiency  A_plain-vs-{arm} both_pass_only metric={field} n={metric['paired_cells']} median_change={change} unavailable={metric['unavailable_pairs']} zero_baseline={metric['zero_baseline_pairs']}",
                    None,
                )
            )
    for failure in summary["failures"]:
        categories = ",".join(sorted({detail["category"] for detail in failure["details"]}))
        lines.append(
            (
                f"FAILURE  {failure['task_id']} rep={failure['repetition']} {failure['arm']} categories={categories}",
                failure["arm"],
            )
        )
    return lines
