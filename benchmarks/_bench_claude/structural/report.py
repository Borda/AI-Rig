"""Paired-arm tables, workflow breakdowns, and result persistence."""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd
from _bench_common.benchmark_paths import RESULTS_DIR
from _bench_common.presentation import (
    fmt_time,
    print_section_rule,
)

from _bench_claude.structural.config import _BLUE, _GREEN, _RED, _RESET, _console
from _bench_claude.structural.models import BenchRun
from _bench_claude.structural.tasks import _run_from_cached

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _run_correct_symbol(run: BenchRun) -> str:
    """Single-character status symbol for a completed run.

    Returns:
        'c' — contaminated (plain arm accessed codemap binary)
        '!' — incomplete (budget exhausted)
        '+' — correct
        '-' — scored but incorrect
        '?' — not scored
    """
    if run.error == "contaminated":
        return "c"
    if run.incomplete:
        return "!"
    if run.quality.correct:
        return "+"
    if run.quality.scored:
        return "-"
    return "?"


def _effective_recall(run: BenchRun | None) -> float | None:
    """Recall value in [0, 1] for summary display.

    Returns the true recall when an evaluator sets it. Evaluators that score by
    line tolerance or count (symbol_extraction, code_quality, count-based
    review_assistance) never populate ``recall``; for those the 0-1 signal is
    binary correctness over the task's ground truth — 1.0 when the answer landed
    within tolerance, 0.0 otherwise (a scored-but-failed extraction is a genuine
    miss, not an unknown). The former ``metric_got / metric_expected`` fallback
    is a raw line-number / count ratio that can exceed 1.0, so it is *not* recall
    and must not wear the recall label; the raw values remain in scoring_detail
    for diagnostics.

    Args:
        run: A completed benchmark run, or None.

    Returns:
        Recall as a float in [0, 1], or None when the run was not scored or its
        answer could not be parsed (extraction_failed) — parse failures are a
        separate signal from a wrong-but-parsed answer (which scores 0.0).

    Examples:
        >>> _effective_recall(None) is None
        True
    """
    if run is None or not run.quality.scored:
        return None
    if run.quality.extraction_failed:
        # Parse failure — the harness could not extract an answer from the output.
        # This is a parser-coverage signal, NOT evaluation degradation, so it is
        # kept out of the recall metric (surfaced separately with a distinct sign).
        return None
    if run.quality.recall is not None:
        return run.quality.recall
    return 1.0 if run.quality.correct else 0.0


def _safe_ratio(num: float | None, den: float | None) -> float:
    """Divide num by den; return NaN when den is zero or None.

    Args:
        num: Numerator (int or float, or None).
        den: Denominator (int or float, or None).

    Returns:
        num / den, or float('nan') when division is undefined.

    Examples:
        >>> _safe_ratio(10, 4)
        2.5
        >>> import math; math.isnan(_safe_ratio(10, 0))
        True
    """
    if num is None or den is None:
        return float("nan")
    return num / den if den else float("nan")


@dataclass
class TaskRatioRow:
    """One row in the per-task token-ratio summary table."""

    task_id: str
    task_type: str
    plain_tok: int
    codemap_tok: int
    ratio: float
    plain_recall: float | None
    codemap_recall: float | None
    plain_correct: bool | None
    codemap_correct: bool | None
    plain_elapsed_s: float | None
    codemap_elapsed_s: float | None
    time_ratio: float | None
    plain_cost: float | None
    codemap_cost: float | None
    cost_ratio: float | None  # codemap $ / plain $ — price-accurate cross-arm comparison


#: Control arm names in preference order; the first one present in a run set is the baseline.
_BASELINE_ARM_ORDER = ("A_plain", "plain")
#: Treatment arm names in the order their comparisons are rendered.
_TREATMENT_ARM_ORDER = ("C_strict", "B_auto", "codemap")


def _arms_present(runs: list[BenchRun]) -> list[str]:
    """Return the baseline and treatment arms present in *runs*, baseline first.

    Args:
        runs: All benchmark runs.

    Returns:
        Arm names in canonical render order; arms outside the known rosters are appended sorted.

    Examples:
        >>> mk = lambda arm: BenchRun(arm=arm, task_id="X", task_type="t", model="haiku", success=True)
        >>> _arms_present([mk("C_strict"), mk("A_plain"), mk("B_auto")])
        ['A_plain', 'B_auto', 'C_strict']
    """
    present = {r.arm for r in runs}
    known = [a for a in _BASELINE_ARM_ORDER if a in present][:1]
    known += [a for a in ("B_auto", "C_strict", "codemap") if a in present]
    return known + sorted(present - set(known))


def _arm_pairs(runs: list[BenchRun]) -> list[tuple[str, str]]:
    """Return every (baseline, treatment) arm pair to compare for *runs*.

    A parity run carries two treatment arms against one control, so the comparison is rendered once
    per treatment instead of collapsing both onto a single ``codemap`` key, which would silently drop
    whichever arm was recorded first.

    Args:
        runs: All benchmark runs.

    Returns:
        Pairs in render order; empty when no control arm ran.

    Examples:
        >>> mk = lambda arm: BenchRun(arm=arm, task_id="X", task_type="t", model="haiku", success=True)
        >>> _arm_pairs([mk("A_plain"), mk("B_auto"), mk("C_strict")])
        [('A_plain', 'C_strict'), ('A_plain', 'B_auto')]
        >>> _arm_pairs([mk("plain"), mk("codemap")])
        [('plain', 'codemap')]
        >>> _arm_pairs([mk("B_auto")])
        []
    """
    present = {r.arm for r in runs}
    baseline = next((a for a in _BASELINE_ARM_ORDER if a in present), None)
    if baseline is None:
        return []
    return [(baseline, t) for t in _TREATMENT_ARM_ORDER if t in present]


def _token_ratio_table(runs: list[BenchRun], baseline: str = "plain", treatment: str = "codemap") -> pd.DataFrame:
    """Build a per-task token-ratio table comparing one treatment arm against the control arm.

    Args:
        runs: All benchmark runs.
        baseline: Control arm name whose figures fill the ``plain_*`` columns.
        treatment: Codemap-bearing arm name whose figures fill the ``codemap_*`` columns.

    Returns:
        DataFrame with columns: task_id, task_type, plain_tok, codemap_tok, ratio, delta_correct.
    """
    by_task: dict[str, dict[str, BenchRun]] = defaultdict(dict)
    for r in runs:
        by_task[r.task_id][r.arm] = r

    rows = []
    for task_id, arms in sorted(by_task.items()):
        plain = arms.get(baseline)
        codemap = arms.get(treatment)
        plain_tok = plain.input_tokens if plain else 0
        codemap_tok = codemap.input_tokens if codemap else 0
        ratio = _safe_ratio(codemap_tok, plain_tok)
        plain_ok = plain.quality.correct if plain and plain.quality.scored else None
        codemap_ok = codemap.quality.correct if codemap and codemap.quality.scored else None
        task_type = (plain or codemap).task_type if (plain or codemap) else ""
        plain_elapsed = plain.elapsed_s if plain else None
        codemap_elapsed = codemap.elapsed_s if codemap else None
        time_ratio = _safe_ratio(codemap_elapsed, plain_elapsed)
        # Price-accurate cross-arm cost from each run's captured total_cost_usd (None when absent).
        plain_cost = plain.cost_usd if plain and plain.cost_usd else None
        codemap_cost = codemap.cost_usd if codemap and codemap.cost_usd else None
        cost_ratio = _safe_ratio(codemap_cost, plain_cost)
        rows.append(
            TaskRatioRow(
                task_id=task_id,
                task_type=task_type,
                plain_tok=plain_tok,
                codemap_tok=codemap_tok,
                ratio=ratio,
                plain_recall=_effective_recall(plain),
                codemap_recall=_effective_recall(codemap),
                plain_correct=plain_ok,
                codemap_correct=codemap_ok,
                plain_elapsed_s=plain_elapsed,
                codemap_elapsed_s=codemap_elapsed,
                time_ratio=time_ratio,
                plain_cost=plain_cost,
                codemap_cost=codemap_cost,
                cost_ratio=cost_ratio,
            )
        )
    return pd.DataFrame([asdict(r) for r in rows])


def _workflow_type_of(run: BenchRun) -> str:
    """Return the workflow grouping key for a run.

    Falls back to ``task_type`` when ``workflow_type`` is unset (legacy task
    files that predate the field).

    Args:
        run: A completed benchmark run.

    Returns:
        The workflow grouping key (e.g. ``"query"``, ``"debug"``).

    Examples:
        >>> run = BenchRun(
        ...     arm="plain", task_id="X", task_type="symbol_extraction", model="haiku", success=True,
        ...     workflow_type="query",
        ... )
        >>> _workflow_type_of(run)
        'query'
        >>> run = BenchRun(arm="plain", task_id="X", task_type="symbol_extraction", model="haiku", success=True)
        >>> _workflow_type_of(run)
        'symbol_extraction'
    """
    return run.workflow_type or run.task_type


def _print_workflow_breakdown(runs: list[BenchRun], baseline: str = "plain", treatment: str = "codemap") -> None:
    """Print a per-workflow_type breakdown of token ratio and accuracy.

    Groups runs by :func:`_workflow_type_of`. For each workflow type, reports
    the median and mean treatment/baseline token ratio (computed per task that has both arms)
    and the treatment-arm accuracy over scored, completed runs.

    Args:
        runs: All benchmark runs (may span multiple arms and workflow types).
        baseline: Control arm name used as the ratio denominator.
        treatment: Codemap-bearing arm name used as the ratio numerator.
    """
    by_wf: dict[str, list[BenchRun]] = defaultdict(list)
    for r in runs:
        by_wf[_workflow_type_of(r)].append(r)
    if not by_wf:
        return

    print("\nPer-workflow_type breakdown:")
    hdr = f"  {'workflow_type':<22}  {'n_tasks':>7}  {'tok× (med)':>10}  {'tok× (mean)':>11}  {'cm_acc':>10}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for wf in sorted(by_wf):
        wf_runs = by_wf[wf]
        # Token ratio per task: codemap_tok / plain_tok where both arms ran.
        by_task: dict[str, dict[str, BenchRun]] = defaultdict(dict)
        for r in wf_runs:
            by_task[r.task_id][r.arm] = r
        ratios: list[float] = []
        for arms in by_task.values():
            plain = arms.get(baseline)
            codemap = arms.get(treatment)
            if plain and codemap and plain.input_tokens:
                ratios.append(codemap.input_tokens / plain.input_tokens)
        ratio_str = f"{statistics.median(ratios):>10.2f}" if ratios else f"{'n/a':>10}"
        ratio_mean_str = f"{statistics.mean(ratios):>11.2f}" if ratios else f"{'n/a':>11}"

        # Headline accuracy excludes self-consistency (index-derived GT) runs — same rule as the
        # top-level per-arm accuracy, so the per-workflow_type figure stays consistent with it.
        cm_scored = [
            r
            for r in wf_runs
            if r.arm == treatment
            and r.quality.scored
            and not r.quality.extraction_failed
            and not r.incomplete
            and not _is_self_consistency(r)
        ]
        if cm_scored:
            n_correct = sum(1 for r in cm_scored if r.quality.correct)
            acc_str = f"{n_correct / len(cm_scored):>9.1%}"
        else:
            acc_str = f"{'n/a':>10}"
        n_tasks = len(by_task)
        print(f"  {wf:<22}  {n_tasks:>7}  {ratio_str}  {ratio_mean_str}  {acc_str}")


def _arm_extracted(run: BenchRun | None) -> bool:
    """Return True when *run* produced a scored, extracted, completed metric.

    A run counts as "extracted" only when it was scored, did not fail extraction, and was not cut
    off by the turn budget. Contaminated / answer-file-read runs carry ``scored=False`` and are
    therefore excluded too.

    Args:
        run: A benchmark run for one arm, or None when that arm did not run.

    Returns:
        True when the run yielded a usable score for the paired comparison.
    """
    return bool(run and run.quality.scored and not run.quality.extraction_failed and not run.incomplete)


def _is_self_consistency(run: BenchRun | None) -> bool:
    """Return True when *run* is a self-consistency (index-derived ground truth) task.

    Args:
        run: A benchmark run, or None.

    Returns:
        True when the run carries the ``self_consistency`` flag.

    Examples:
        >>> _is_self_consistency(None)
        False
        >>> r = BenchRun(arm="codemap", task_id="CQ-02", task_type="code_quality", model="haiku", success=True)
        >>> r.self_consistency = True
        >>> _is_self_consistency(r)
        True
    """
    return bool(run and run.self_consistency)


def _paired_accuracy(
    runs: list[BenchRun], baseline: str = "plain", treatment: str = "codemap"
) -> dict[str, int] | None:
    """Compute paired accuracy over tasks where BOTH arms extracted successfully.

    The per-arm accuracy printed elsewhere drops ``extraction_failed`` runs independently per arm, so
    the plain and codemap figures are computed over different task subsets and different n — an
    unpaired comparison. This view restricts both arms to the SAME task set: only tasks where the
    plain AND the codemap run were each scored, extracted a metric, and completed. Both arm accuracies
    then share one denominator, the paired-n, so the headline comparison is like-for-like.

    Self-consistency tasks (index-derived ground truth) are excluded — the codemap arm would be scored
    against the same index it queries. They are reported separately by :func:`_print_self_consistency`.

    Args:
        runs: All benchmark runs (both arms, all tasks).
        baseline: Control arm name counted as ``plain_correct``.
        treatment: Codemap-bearing arm name counted as ``codemap_correct``.

    Returns:
        Dict with ``n`` (paired task count), ``plain_correct``, and ``codemap_correct``; None when no
        task has both arms extracted.
    """
    by_task: dict[str, dict[str, BenchRun]] = defaultdict(dict)
    for r in runs:
        by_task[r.task_id][r.arm] = r
    paired = [
        arms
        for arms in by_task.values()
        if _arm_extracted(arms.get(baseline))
        and _arm_extracted(arms.get(treatment))
        and not _is_self_consistency(arms.get(treatment))
    ]
    if not paired:
        return None
    return {
        "n": len(paired),
        "plain_correct": sum(1 for a in paired if a[baseline].quality.correct),
        "codemap_correct": sum(1 for a in paired if a[treatment].quality.correct),
    }


def _print_self_consistency(runs: list[BenchRun]) -> None:
    """Print a separate self-consistency accuracy row (index-derived ground truth).

    These tasks (e.g. uncovered / broken-xref counts) are excluded from the headline accuracy
    aggregates because the codemap arm is scored against the very index it queries. Reporting them
    apart keeps the headline honest while still surfacing the self-agreement signal.

    Args:
        runs: All benchmark runs (both arms, all tasks).
    """
    sc = [r for r in runs if _is_self_consistency(r) and _arm_extracted(r)]
    if not sc:
        return
    ids = sorted({r.task_id for r in sc})
    print(f"\n  Self-consistency (index-derived GT — excluded from headline accuracy; tasks: {', '.join(ids)}):")
    for arm in _arms_present(sc):
        arm_sc = [r for r in sc if r.arm == arm]
        if not arm_sc:
            continue
        n_correct = sum(1 for r in arm_sc if r.quality.correct)
        print(f"    {arm}   = {n_correct / len(arm_sc):.1%}  ({n_correct}/{len(arm_sc)})")


def _print_paired_accuracy(runs: list[BenchRun], baseline: str = "plain", treatment: str = "codemap") -> None:
    """Print the paired accuracy view (both arms extracted, shared denominator).

    Args:
        runs: All benchmark runs (both arms, all tasks).
        baseline: Control arm name.
        treatment: Codemap-bearing arm name.
    """
    paired = _paired_accuracy(runs, baseline, treatment)
    if paired is None:
        return
    n = paired["n"]
    pc = paired["plain_correct"]
    cc = paired["codemap_correct"]
    width = max(len(baseline), len(treatment))
    print(f"\n  Paired accuracy (both arms extracted, paired-n={n}):")
    print(f"    {baseline:<{width}} = {pc / n:.1%}  ({pc}/{n})")
    print(f"    {treatment:<{width}} = {cc / n:.1%}  ({cc}/{n})")


def _print_pair_table(df: pd.DataFrame, baseline: str, treatment: str) -> None:
    """Print the per-task comparison table for one (baseline, treatment) arm pair.

    Args:
        df: Ratio table produced by :func:`_token_ratio_table` for this pair.
        baseline: Control arm name labelling the left-hand columns.
        treatment: Codemap-bearing arm name labelling the right-hand columns.
    """
    hdr = (
        f"{'task_id':<9}  {baseline + '_tok':>12}  {treatment + '_tok':>12}  {'tok×':>5}  {'$×':>5}  "
        f"{baseline + '_t':>9}  {treatment + '_t':>9}  {'t×':>5}  {'Δrecall':>9}"
    )
    print(hdr)
    print("-" * len(hdr))
    for _, row in df.iterrows():
        tid = str(row["task_id"])
        ptok = f"{int(row['plain_tok']):>12,}" if pd.notna(row["plain_tok"]) else f"{'n/a':>12}"
        ctok = f"{int(row['codemap_tok']):>12,}" if pd.notna(row["codemap_tok"]) else f"{'n/a':>12}"
        tratio = f"{row['ratio']:>5.2f}" if pd.notna(row["ratio"]) else f"{'n/a':>5}"
        cratio = f"{row['cost_ratio']:>5.2f}" if pd.notna(row.get("cost_ratio", float("nan"))) else f"{'n/a':>5}"
        pt = f"{row['plain_elapsed_s'] / 60:>8.1f}m" if pd.notna(row["plain_elapsed_s"]) else f"{'n/a':>9}"
        ct = f"{row['codemap_elapsed_s'] / 60:>8.1f}m" if pd.notna(row["codemap_elapsed_s"]) else f"{'n/a':>9}"
        trm = f"{row['time_ratio']:>5.2f}" if pd.notna(row.get("time_ratio", float("nan"))) else f"{'n/a':>5}"
        pr = row["plain_recall"]
        cr = row["codemap_recall"]
        if pd.notna(pr) and pd.notna(cr):
            delta = cr - pr
            if abs(delta) < 0.01:
                sym = f"{_BLUE}~{delta:.2f}{_RESET}"
                vis = f"~{delta:.2f}"
            elif delta > 0:
                sym = f"{_GREEN}+{delta:.2f}{_RESET}"
                vis = f"+{delta:.2f}"
            else:
                sym = f"{_RED}{delta:.2f}{_RESET}"
                vis = f"{delta:.2f}"
            pad = 7 - len(vis)
            recall_col = " " * max(pad, 0) + sym
        elif pd.notna(cr):
            recall_col = f"{'cm:' + f'{cr:.2f}':>9}"
        elif pd.notna(pr):
            recall_col = f"{'pl:' + f'{pr:.2f}':>9}"
        else:
            recall_col = f"{'n/a':>9}"
        print(f"{tid:<9}  {ptok}  {ctok}  {tratio}  {cratio}  {pt}  {ct}  {trm}  {recall_col}")


def _print_pair_ratios(df: pd.DataFrame, baseline: str, treatment: str) -> None:
    """Print the median, mean, and range of the token, cost, and time ratios for one arm pair.

    Args:
        df: Ratio table produced by :func:`_token_ratio_table` for this pair.
        baseline: Control arm name forming the ratio denominator.
        treatment: Codemap-bearing arm name forming the ratio numerator.
    """
    label = f"({treatment}/{baseline})"
    valid = df.dropna(subset=["ratio"])
    if not valid.empty:
        ratios = valid["ratio"].tolist()
        print(
            f"\nToken ratio {label}:  median={statistics.median(ratios):.2f}  mean={statistics.mean(ratios):.2f}  [{min(ratios):.2f}–{max(ratios):.2f}]"
        )

    valid_c = df.dropna(subset=["cost_ratio"])
    if not valid_c.empty:
        cost_ratios = valid_c["cost_ratio"].tolist()
        print(
            f"Cost ratio  {label}:  median={statistics.median(cost_ratios):.2f}  mean={statistics.mean(cost_ratios):.2f}  [{min(cost_ratios):.2f}–{max(cost_ratios):.2f}]  (price-accurate)"
        )

    valid_t = df.dropna(subset=["time_ratio"])
    if not valid_t.empty:
        time_ratios = valid_t["time_ratio"].tolist()
        plain_times = valid_t["plain_elapsed_s"].tolist()
        codemap_times = valid_t["codemap_elapsed_s"].tolist()
        width = max(len(baseline), len(treatment))
        print(
            f"Time ratio  {label}:  median={statistics.median(time_ratios):.2f}  mean={statistics.mean(time_ratios):.2f}  [{min(time_ratios):.2f}–{max(time_ratios):.2f}]"
        )
        print(
            f"  {baseline:<{width}} median={fmt_time(statistics.median(plain_times))}  mean={fmt_time(statistics.mean(plain_times))}"
        )
        print(
            f"  {treatment:<{width}} median={fmt_time(statistics.median(codemap_times))}  mean={fmt_time(statistics.mean(codemap_times))}"
        )


def _print_arm_diagnostics(runs: list[BenchRun]) -> None:
    """Print per-arm accuracy and exclusion counts for every arm present in *runs*.

    Reported once per arm rather than once per comparison, so a parity run with two treatment arms
    does not repeat the control arm's figures under each pair.

    Args:
        runs: All benchmark runs (may span multiple arms).
    """
    treatments = set(_TREATMENT_ARM_ORDER)
    for arm in _arms_present(runs):
        # Denominator matches canonical accuracy and _arm_extracted: scored, parsed, and
        # NOT budget-cut. Timeout-incomplete runs get scored on partial output but must be excluded
        # here too, else the per-arm % counts a run the headline verdict drops.
        arm_runs = [r for r in runs if r.arm == arm and _arm_extracted(r)]
        extraction_failed_runs = [r for r in runs if r.arm == arm and r.quality.extraction_failed]
        incomplete_runs = [r for r in runs if r.arm == arm and r.incomplete]
        contaminated_runs = [r for r in runs if r.arm == arm and r.error == "contaminated"]
        # Headline accuracy excludes self-consistency (index-derived GT) runs; they are reported by
        # _print_self_consistency below so the codemap arm is never credited for agreeing with itself.
        headline_runs = [r for r in arm_runs if not _is_self_consistency(r)]
        if headline_runs:
            n_correct = sum(1 for r in headline_runs if r.quality.correct)
            acc = n_correct / len(headline_runs)
            print(f"  {arm} accuracy = {acc:.1%}  ({n_correct}/{len(headline_runs)} scored)")
        if extraction_failed_runs:
            ids = ", ".join(r.task_id for r in extraction_failed_runs)
            print(
                f"  {arm} extraction_failed = {len(extraction_failed_runs)} (metric not found in output — excluded: {ids})"
            )
        if incomplete_runs:
            ids = ", ".join(r.task_id for r in incomplete_runs)
            print(f"  {arm} incomplete = {len(incomplete_runs)} (budget exhausted — not scored: {ids})")
        if contaminated_runs:
            ids = ", ".join(r.task_id for r in contaminated_runs)
            print(
                f"  {arm} contaminated = {len(contaminated_runs)} (codemap accessed in plain arm — not scored: {ids})"
            )
        answer_read_runs = [r for r in runs if r.arm == arm and r.error == "answer_file_read"]
        if answer_read_runs:
            ids = ", ".join(r.task_id for r in answer_read_runs)
            print(f"  {arm} answer_file_read = {len(answer_read_runs)} (GT file accessed — not scored: {ids})")
        _SAFETY_GRADE_TYPES = {"develop_blast_radius", "fn_call_graph"}
        safety_runs = [
            r
            for r in arm_runs
            if r.task_type in _SAFETY_GRADE_TYPES and r.quality.scoring_detail.get("safety_grade") is not None
        ]
        if safety_runs:
            n_safe = sum(1 for r in safety_runs if r.quality.scoring_detail.get("safety_grade"))
            print(f"  {arm} safety-grade (recall>=0.90) = {n_safe}/{len(safety_runs)}")
        if arm in treatments:
            all_nc = sorted({nc for r in arm_runs for nc in r.codemap_not_covered})
            if all_nc:
                print(f"  {arm} not_covered gaps: {', '.join(all_nc)}")
            all_methods = sorted({m for r in arm_runs for m in r.codemap_methods})
            if all_methods:
                print(f"  {arm} query methods used: {', '.join(all_methods)}")

    # Tier E: patch pass rate (failing test → fix → test pass). Only shown when any
    # run carries a patch_pass signal; agents emitting prose without a diff score 0.
    patch_runs = [r for r in runs if r.patch_pass is not None]
    for arm in _arms_present(patch_runs):
        arm_patch = [r for r in patch_runs if r.arm == arm]
        n_pass = sum(1 for r in arm_patch if r.patch_pass)
        rate = n_pass / len(arm_patch)
        print(f"  patch_pass_rate ({arm}) = {n_pass}/{len(arm_patch)}  ({rate:.1%})")


def _print_summary(runs: list[BenchRun], model: str) -> None:
    """Print token-ratio and accuracy summaries to stdout, once per treatment arm.

    A parity run carries one control arm and two treatment arms, so the comparison table, its ratio
    aggregates, the paired accuracy, and the workflow breakdown are rendered per (control, treatment)
    pair. Per-arm diagnostics and the self-consistency row cover every arm once.

    Args:
        runs: All benchmark runs (may span multiple arms).
        model: Short model name shown in header.
    """
    if not runs:
        print("No runs to summarise.")
        return

    print("\n")
    print_section_rule(f"Codemap benchmark — model={model}", console=_console)

    pairs = _arm_pairs(runs)
    if not pairs:
        print(f"  no control arm among {', '.join(_arms_present(runs))} — comparison tables omitted")
    for baseline, treatment in pairs:
        df = _token_ratio_table(runs, baseline, treatment)
        if df.empty:
            continue
        print(f"\n-- {baseline} (control) vs {treatment} (treatment) --")
        _print_pair_table(df, baseline, treatment)
        _print_pair_ratios(df, baseline, treatment)
        # Paired accuracy: both arms scored over the SAME both-extracted task set. The per-arm
        # figures below use different denominators (each arm drops its own extraction failures),
        # so the paired view is the like-for-like headline comparison with its shared n stated.
        _print_paired_accuracy(runs, baseline, treatment)
        _print_workflow_breakdown(runs, baseline, treatment)

    _print_arm_diagnostics(runs)

    # Self-consistency row: index-derived GT tasks, reported apart from the headline accuracy.
    _print_self_consistency(runs)


def _runs_from_results_file(path: Path) -> list[BenchRun]:
    """Reconstruct the runs stored in one ``bench-*.jsonl`` results file.

    Args:
        path: Path to a results file written by :func:`_save_results`.

    Returns:
        One :class:`BenchRun` per parseable line, in file order.

    Raises:
        SystemExit: When the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"ERROR: cannot read results file {path}: {exc}")
    runs: list[BenchRun] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict):
            runs.append(_run_from_cached(line))
    return runs


def _print_report_only(path: Path) -> None:
    """Re-render the summary for an already-recorded results file, without running any model.

    Reporting code evolves after a paid run has been recorded, and the rows carry every figure the
    summary needs, so replaying them is the supported way to correct a report instead of re-spending
    on the same cells.

    Args:
        path: Path to a results file written by :func:`_save_results`.
    """
    runs = _runs_from_results_file(path)
    if not runs:
        sys.exit(f"ERROR: no result rows in {path}")
    models = sorted({r.model for r in runs})
    for model in models:
        _print_summary([r for r in runs if r.model == model], model)
    print(f"\nReplayed {len(runs)} rows from {path}")


def _save_results(runs: list[BenchRun], model: str) -> Path:
    """Serialise run results to JSONL in the results directory.

    Args:
        runs: All benchmark runs.
        model: Short model name used in filename.

    Returns:
        Path to the written JSONL file.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"bench-{model}-{ts}.jsonl"
    with out.open("w") as f:
        for r in runs:
            d = asdict(r)
            json.dump(d, f)
            f.write("\n")
    return out
