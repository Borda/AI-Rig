"""The coverage, accuracy, latency, query-shape, and self-consistency benchmark suites."""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from pathlib import Path

from _bench_query.cold import count_cold_calls_deps, count_cold_calls_rdeps, time_command, time_commands
from _bench_query.models import THRESHOLDS, ScenarioResult, Task, TimingStats
from _bench_query.output import log
from _bench_query.queries import (
    codemap_rdeps_result,
    grep_importers_boundary,
    run_query_shape_query,
    run_scan_query,
    run_scan_query_result,
)
from _bench_query.scoring import score_rdeps_accuracy
from _bench_query.sources import module_to_grep_pattern, module_to_package, verify_importer
from _bench_query.tasks import load_oss_tasks, load_tasks

# ---- SUITE: CALLS ----

_HIGH_RISK_TIERS = {"high", "very-high", "moderate-high"}


def _measure_coverage_gap(
    tasks: list[Task], repo_path: Path, scan_query_bin: Path, index_path: Path
) -> tuple[float, int, int, list[dict], list[str]]:
    """Measure the real importer coverage gap of codemap over boundary-anchored grep.

    For each high-risk task the codemap ``rdeps`` importer set is compared with a
    boundary-anchored grep importer set.  Every extra importer that codemap found
    but grep missed is AST-verified (:func:`verify_importer`) to confirm it is a
    genuine importer — resolving aliased, relative, and ``__init__`` re-export
    forms that grep's literal pattern cannot see.  The gap is the total verified
    extras divided by the total codemap importer count, aggregated across tasks.

    Args:
        tasks: All benchmark tasks (only high-risk tiers with an rdeps query count).
        repo_path: Root of the repository under test.
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap index.

    Returns:
        Tuple ``(coverage_gap, verified_extras_total, codemap_set_total, per_task, errors)``
        where ``per_task`` is a JSON-serializable list of per-task breakdowns and
        ``errors`` lists ``"<task-id>: <reason>"`` for any task whose rdeps query
        failed (those tasks are excluded from the totals, never scored as empty).
    """
    per_task: list[dict] = []
    errors: list[str] = []
    verified_extras_total = 0
    codemap_set_total = 0
    for task in tasks:
        if task.risk_tier not in _HIGH_RISK_TIERS:
            continue
        rdeps_q = next((q for q in task.queries if q.cmd == "rdeps" and q.args), None)
        if rdeps_q is None:
            continue
        module = rdeps_q.args[0]
        cm_set, err = codemap_rdeps_result(scan_query_bin, index_path, repo_path, module)
        if err is not None:
            errors.append(f"{task.id}: {err}")
            per_task.append({"task_id": task.id, "module": module, "errored": True, "error": err})
            continue
        grep_set = grep_importers_boundary(repo_path, module)
        extras = cm_set - grep_set
        verified = sorted(e for e in extras if verify_importer(e, module, repo_path))
        verified_extras_total += len(verified)
        codemap_set_total += len(cm_set)
        per_task.append(
            {
                "task_id": task.id,
                "module": module,
                "codemap_count": len(cm_set),
                "grep_count": len(grep_set),
                "extras": sorted(extras),
                "verified_extras": verified,
                "verified_count": len(verified),
            }
        )
    coverage_gap = verified_extras_total / max(codemap_set_total, 1)
    return coverage_gap, verified_extras_total, codemap_set_total, per_task, errors


def _measure_infeasible_paths(tasks: list[Task], repo_path: Path) -> tuple[float, int, int, list[dict]]:
    """Measure the fraction of import paths that are not discoverable in a single grep.

    A path query ``A -> B`` is *1-grep-feasible* only when ``A`` is a direct
    importer of ``B`` — i.e. a single boundary-anchored grep for importers of
    ``B`` surfaces ``A`` directly (a direct edge).  When ``A`` is absent from
    ``B``'s direct importers the path requires at least one intermediate hop and
    is counted as infeasible.  The fraction is infeasible paths over all path
    queries.

    Args:
        tasks: All benchmark tasks (only those carrying ``path`` queries contribute).
        repo_path: Root of the repository under test.

    Returns:
        Tuple ``(fraction, infeasible_count, total_path_queries, per_path)`` where
        ``per_path`` is a JSON-serializable list of per-query direct-edge results.
    """
    per_path: list[dict] = []
    infeasible_count = 0
    total_path_queries = 0
    for task in tasks:
        for q in task.queries:
            if q.cmd != "path" or len(q.args) < 2:
                continue
            total_path_queries += 1
            frm, to = q.args[0], q.args[1]
            direct_edge = frm in grep_importers_boundary(repo_path, to)
            if not direct_edge:
                infeasible_count += 1
            per_path.append({"from": frm, "to": to, "direct_edge": direct_edge})
    fraction = infeasible_count / max(total_path_queries, 1)
    return fraction, infeasible_count, total_path_queries, per_path


def run_measure_calls(repo_path: Path, scan_query_bin: Path, index_path: Path) -> list[ScenarioResult]:
    """Run Suite C — coverage gap, infeasible-path fraction, and leverage ratio.

    Evaluates three scenarios: C1 (real importer coverage gap of codemap vs a
    boundary-anchored grep, AST-verified), C2 (fraction of import paths that need
    more than one grep hop), and C3 (leverage ratio of cold vs warm call counts).

    Args:
        repo_path: Root of the pytorch-lightning repository to search.
        scan_query_bin: Path to the scan-query executable (used for C1 rdeps).
        index_path: Path to the pre-built codemap index (used for C1 rdeps).

    Returns:
        List of three :class:`ScenarioResult` objects (C1, C2, C3).
    """
    results: list[ScenarioResult] = []
    tasks = load_tasks()
    log("[calls] Starting call-savings measurement...")

    # C1: coverage gap — verified importers codemap finds that a boundary grep misses
    log("[calls] C1: coverage-gap")
    coverage_gap, verified_extras_total, codemap_set_total, cov_per_task, cov_errors = _measure_coverage_gap(
        tasks, repo_path, scan_query_bin, index_path
    )
    # A scan-query failure must not pass silently as a zero-gap empty result — fail C1 when any task errored.
    passed = coverage_gap >= THRESHOLDS["C1"]["coverage_gap_min"] and not cov_errors
    err_note = f"; {len(cov_errors)} errored: {'; '.join(cov_errors)}" if cov_errors else ""
    r = ScenarioResult(
        scenario="C1",
        name="coverage-gap",
        suite="calls",
        passed=passed,
        result={
            "coverage_gap": round(coverage_gap, 4),
            "verified_extras_total": verified_extras_total,
            "codemap_set_total": codemap_set_total,
            "errored": cov_errors,
            "per_task": cov_per_task,
        },
        threshold=THRESHOLDS["C1"],
        notes=(
            f"{verified_extras_total} verified extras / {codemap_set_total} codemap importers; "
            f"gap={coverage_gap:.2%}{err_note}"
        ),
    )
    results.append(r)

    # C2: infeasible path fraction — paths where the source is not a direct importer of the target
    log("[calls] C2: infeasible-path-fraction")
    fraction, infeasible_count, total_path_queries, path_detail = _measure_infeasible_paths(tasks, repo_path)
    passed = fraction >= THRESHOLDS["C2"]["infeasible_path_fraction_min"]
    r = ScenarioResult(
        scenario="C2",
        name="infeasible-path-fraction",
        suite="calls",
        passed=passed,
        result={
            "total_path_queries": total_path_queries,
            "infeasible_count": infeasible_count,
            "fraction": round(fraction, 4),
            "per_path": path_detail,
        },
        threshold=THRESHOLDS["C2"],
        notes=f"{infeasible_count}/{total_path_queries} paths need >1 grep hop",
    )
    results.append(r)

    # C3: leverage ratio — structural context tokens / cold exploration tokens
    log("[calls] C3: leverage-ratio")
    total_cold = 0
    total_warm = 0
    for task in tasks:
        mod = task.primary_module
        total_cold += count_cold_calls_rdeps(repo_path, mod) + count_cold_calls_deps(repo_path, mod)
        total_warm += max(sum(1 for q in task.queries if q.cmd in ("rdeps", "deps")), 1)
    leverage_ratio = total_cold / max(total_warm, 1)
    passed = leverage_ratio >= THRESHOLDS["C3"]["leverage_ratio_min"]
    r = ScenarioResult(
        scenario="C3",
        name="leverage-ratio",
        suite="calls",
        passed=passed,
        result={
            # Planned, not observed. The cold commands are executed once each, but their
            # output and exit codes are discarded and the returned figure is `len(cmds)`
            # by construction — it is the size of the planned grep plan, not a count of
            # search work observed to be necessary. The warm side is likewise the number
            # of queries the task declares. Naming them `*_planned_calls` keeps them from
            # reading as measurements alongside the genuinely measured codemap counts.
            "total_cold_planned_calls": total_cold,
            "total_warm_planned_calls": total_warm,
            "leverage_ratio": round(leverage_ratio, 2),
            "counts_are": "planned_invocations_not_observed_search_work",
            "task_count": len(tasks),
        },
        threshold=THRESHOLDS["C3"],
        notes=f"cold={total_cold} planned calls; warm={total_warm} planned; ratio={leverage_ratio:.1f}x",
    )
    results.append(r)

    return results


# ---- SUITE: ACCURACY ----


_A1_TIERS = _HIGH_RISK_TIERS  # high-risk accuracy group (high / very-high / moderate-high)
# A2 grades EVERY task not in A1 (low / low-moderate / moderate) with a precision-only rubric — the
# complement of A1, so the two sets partition the suite and no task is ever silently ungraded.


def _errored_accuracy_row(task: Task, module: str, error: str) -> dict:
    """Build a zeroed, error-flagged accuracy row that is excluded from scoring.

    Args:
        task: The benchmark task whose rdeps query failed.
        module: The rdeps module argument that was queried.
        error: Short scan-query failure reason from :func:`codemap_rdeps_result`.

    Returns:
        A per-module dict with ``errored=True`` and zeroed metrics so the row is
        never scored as a passing empty result.
    """
    return {
        "module": module,
        "task_id": task.id,
        "risk_tier": task.risk_tier,
        "errored": True,
        "error": error,
        "codemap_count": 0,
        "grep_count": 0,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "precision": 0.0,
        "recall": 0.0,
        "fp_list": [],
        "fn_list": [],
    }


def _score_accuracy_tasks(tasks: list[Task], scan_query_bin: Path, index_path: Path, repo_path: Path) -> list[dict]:
    """Score every rdeps task with AST-verified precision and a grep recall floor.

    A scan-query failure is recorded as an errored row (via :func:`_errored_accuracy_row`)
    rather than a silent empty result, so a crashed or absent-module query can never score
    precision 1.0.

    Args:
        tasks: All benchmark tasks (those without an rdeps query are skipped).
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap index.
        repo_path: Root of the repository under test.

    Returns:
        List of per-module result dicts, one per task carrying an rdeps query.
    """
    scored: list[dict] = []
    for task in tasks:
        log(f"[accuracy] {task.id}: {task.primary_module} ({task.risk_tier})")
        rdeps_queries = [q for q in task.queries if q.cmd == "rdeps"]
        if not rdeps_queries:
            continue
        rdeps_mod = rdeps_queries[0].args[0]
        cm_set, err = codemap_rdeps_result(scan_query_bin, index_path, repo_path, rdeps_mod)
        if err is not None:
            scored.append(_errored_accuracy_row(task, rdeps_mod, err))
            continue
        grep_floor = grep_importers_boundary(repo_path, rdeps_mod)
        stats = score_rdeps_accuracy(cm_set, grep_floor, rdeps_mod, repo_path)
        scored.append(
            {
                "module": rdeps_mod,
                "task_id": task.id,
                "risk_tier": task.risk_tier,
                "errored": False,
                "codemap_count": len(cm_set),
                "grep_count": stats.tp + stats.fn,
                "tp": stats.tp,
                "fp": stats.fp,
                "fn": stats.fn,
                "precision": stats.precision,
                "recall": stats.recall,
                "fp_list": stats.fp_modules,
                "fn_list": stats.fn_modules,
            }
        )
    return scored


def _a1_scenario(rows: list[dict]) -> ScenarioResult:
    """Build the A1 result: AST precision + grep recall floor for high-risk tasks.

    PASS requires EVERY scored module to meet both thresholds; group means are reported as context only and never decide
    the gate, so one failing module cannot be masked.
    """
    thr = THRESHOLDS["A1"]
    group = [m for m in rows if m["risk_tier"] in _A1_TIERS]
    scored = [m for m in group if not m["errored"]]
    errored = [m for m in group if m["errored"]]
    if not scored:
        return ScenarioResult(
            "A1",
            "rdeps-accuracy-high",
            "accuracy",
            False,
            {"error": "no scored high-risk tasks", "errored": [m["module"] for m in errored], "per_module": group},
            thr,
            notes="no high-risk tasks scored",
        )
    avg_precision = statistics.mean(m["precision"] for m in scored)
    avg_recall = statistics.mean(m["recall"] for m in scored)
    for pm in scored:
        pm["pass"] = pm["precision"] >= thr["precision_min"] and pm["recall"] >= thr["recall_min"]
    failing = [m["module"] for m in scored if not m["pass"]]
    passed = not failing and not errored
    err_note = f"; {len(errored)} errored" if errored else ""
    fail_note = f"; {len(failing)} below threshold" if failing else ""
    return ScenarioResult(
        "A1",
        "rdeps-accuracy-high",
        "accuracy",
        passed,
        {
            "avg_precision": round(avg_precision, 4),
            "avg_recall": round(avg_recall, 4),
            "failing_modules": failing,
            "errored": [m["module"] for m in errored],
            "per_module": group,
        },
        thr,
        notes=f"scored {len(scored)} high-risk tasks; every module gated{fail_note}{err_note}",
    )


def _a2_scenario(rows: list[dict]) -> ScenarioResult:
    """Build the A2 result: AST precision must be perfect for lower-risk tasks (precision-only).

    With no recall gate, a non-errored EMPTY result would score precision 1.0 vacuously; such modules are treated as N/A
    (excluded from the perfection check), never a free pass, and A2 FAILS if every module is empty or errored.
    """
    thr = THRESHOLDS["A2"]
    group = [m for m in rows if m["risk_tier"] not in _A1_TIERS]
    errored = [m for m in group if m["errored"]]
    scored = [m for m in group if not m["errored"] and m["codemap_count"] > 0]
    vacuous = [m for m in group if not m["errored"] and m["codemap_count"] == 0]
    if not scored:
        return ScenarioResult(
            "A2",
            "rdeps-accuracy-low",
            "accuracy",
            False,
            {
                "error": "no low-risk tasks with a non-empty codemap result",
                "vacuous": [m["module"] for m in vacuous],
                "errored": [m["module"] for m in errored],
                "per_module": group,
            },
            thr,
            notes="no low-risk tasks scored (all empty or errored)",
        )
    min_precision = min(m["precision"] for m in scored)
    all_perfect = all(m["precision"] >= thr["precision_min"] for m in scored)
    for pm in scored:
        pm["pass"] = pm["precision"] >= thr["precision_min"]
    passed = all_perfect and not errored
    tail = f"; {len(vacuous)} N/A (empty)" if vacuous else ""
    tail += f"; {len(errored)} errored" if errored else ""
    return ScenarioResult(
        "A2",
        "rdeps-accuracy-low",
        "accuracy",
        passed,
        {
            "min_precision": min_precision,
            "all_perfect": all_perfect,
            "vacuous": [m["module"] for m in vacuous],
            "errored": [m["module"] for m in errored],
            "per_module": group,
        },
        thr,
        notes=f"scored {len(scored)} low-risk tasks; min AST precision = {min_precision}{tail}",
    )


def _a3_scenario(rows: list[dict]) -> ScenarioResult:
    """Build the A3 result: overall AST false-positive rate across all scored tasks."""
    thr = THRESHOLDS["A3"]
    scored = [m for m in rows if not m["errored"]]
    errored = [m for m in rows if m["errored"]]
    total_codemap = sum(m["codemap_count"] for m in scored)
    total_fp = sum(m["fp"] for m in scored)
    fp_rate = total_fp / total_codemap if total_codemap > 0 else 0.0
    passed = fp_rate <= thr["fp_rate_max"] and not errored
    err_note = f"; {len(errored)} errored" if errored else ""
    return ScenarioResult(
        "A3",
        "rdeps-fp-analysis",
        "accuracy",
        passed,
        {
            "total_codemap_results": total_codemap,
            "total_false_positives": total_fp,
            "fp_rate": round(fp_rate, 4),
            "errored": [m["module"] for m in errored],
            "fp_details": [{"module": m["module"], "fp_list": m["fp_list"]} for m in scored if m["fp_list"]],
        },
        thr,
        notes=f"AST false-positive rate: {fp_rate:.2%} across {len(scored)} scored tasks{err_note}",
    )


def run_measure_accuracy(repo_path: Path, scan_query_bin: Path, index_path: Path) -> list[ScenarioResult]:
    """Run Suite A — score scan-query rdeps with an AST oracle and a grep recall floor.

    Precision is judged against an independent AST import-resolver (the authoritative
    oracle — codemap is not penalised for aliased / relative / re-export importers grep
    cannot see) and recall is a floor against a boundary-anchored grep set. A scan-query
    failure fails the scenario rather than scoring a false precision 1.0. Evaluates A1
    (high-risk accuracy), A2 (lower-risk precision), and A3 (overall AST FP rate).

    Args:
        repo_path: Root of the pytorch-lightning repository.
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap JSON index.

    Returns:
        List of three :class:`ScenarioResult` objects (A1, A2, A3).
    """
    log("[accuracy] Starting accuracy measurement...")
    rows = _score_accuracy_tasks(load_tasks(), scan_query_bin, index_path, repo_path)
    return [_a1_scenario(rows), _a2_scenario(rows), _a3_scenario(rows)]


# ---- SUITE: LATENCY ----

# Assumed number of structural queries a skill session issues before the index goes stale.
# This is an explicit stated assumption, NOT telemetry: it is the divisor used to amortize the
# one-time scan-index build cost over a session and to fold the build into the honest
# build-inclusive speedup. A conservative value; on a large repo the real build cost dominates
# and L3 is expected to fail under it — that failure is owned by the primary verdict, not hidden.
_QUERIES_PER_SESSION = 10


def run_measure_latency(
    repo_path: Path, scan_query_bin: Path, index_path: Path, scan_index_bin: Path | None
) -> list[ScenarioResult]:
    """Run Suite L — measure wall-clock latency of scan-query commands vs cold grep pipelines.

    Evaluates four scenarios: L1 (central query latency), L2 (rdeps query
    latency across 3 high-risk modules), L3 (amortized scan-index build time),
    and L4 (speedup of codemap vs equivalent cold grep baseline). L3 restores
    the supplied pre-built index byte-for-byte after timing because scan-index
    writes to the product index path.

    Args:
        repo_path: Root of the pytorch-lightning repository.
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap JSON index.
        scan_index_bin: Path to the scan-index executable, or ``None`` when not
            found.  L3 is recorded as failed when this is ``None``.

    Returns:
        List of four :class:`ScenarioResult` objects (L1, L2, L3, L4).
    """
    results: list[ScenarioResult] = []
    cwd = str(repo_path)
    sq = str(scan_query_bin)
    log("[latency] Starting latency measurement...")

    # L1: central query latency
    log("[latency] L1: scan-query central --top 5")
    l1_timing = time_command(["python3", sq, "central", "--top", "5"], n=5, cwd=cwd)
    passed = l1_timing.median_ms <= THRESHOLDS["L1"]["median_ms_max"]
    r = ScenarioResult(
        scenario="L1",
        name="latency-central",
        suite="latency",
        passed=passed,
        result={
            "min_ms": l1_timing.min_ms,
            "median_ms": l1_timing.median_ms,
            "max_ms": l1_timing.max_ms,
            "runs": l1_timing.n,
        },
        threshold=THRESHOLDS["L1"],
        notes=f"5 runs; median={l1_timing.median_ms:.1f}ms",
    )
    results.append(r)

    # L2: rdeps query latency (sample 3 high-risk modules, 5 runs each)
    log("[latency] L2: scan-query rdeps (3 modules)")
    tasks = load_tasks()
    high_risk_mods = [t.primary_module for t in tasks if t.risk_tier in ("high", "very-high")][:3]
    module_medians: list[float] = []
    all_timings: dict[str, TimingStats] = {}
    for mod in high_risk_mods:
        mod_timing = time_command(["python3", sq, "rdeps", mod], n=5, cwd=cwd)
        module_medians.append(mod_timing.median_ms)
        all_timings[mod] = mod_timing

    overall_median = statistics.median(module_medians) if module_medians else 0
    passed = overall_median <= THRESHOLDS["L2"]["median_ms_max"]
    r = ScenarioResult(
        scenario="L2",
        name="latency-rdeps",
        suite="latency",
        passed=passed,
        result={
            "median_ms": round(overall_median, 2),
            "min_ms": round(min(module_medians), 2) if module_medians else 0,
            "max_ms": round(max(module_medians), 2) if module_medians else 0,
            "per_module": {
                m: {"min_ms": ts.min_ms, "median_ms": ts.median_ms, "max_ms": ts.max_ms, "runs": ts.n}
                for m, ts in all_timings.items()
            },
            "runs": 5,
        },
        threshold=THRESHOLDS["L2"],
        notes=f"median across {len(high_risk_mods)} modules = {overall_median:.1f}ms",
    )
    results.append(r)

    # L3: index build time (amortized over 10)
    log("[latency] L3: scan-index build time")
    if scan_index_bin:
        si = str(scan_index_bin)
        original_index = index_path.read_bytes()
        start = time.perf_counter()
        try:
            try:
                subprocess.run(["python3", si, "--root", str(repo_path)], capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                log("[latency] L3: scan-index timed out at 120s")
        finally:
            # L3 measures the product-path build but must not invalidate a caller's
            # frozen benchmark input for later provider runs.
            index_path.write_bytes(original_index)
        build_ms = (time.perf_counter() - start) * 1000
        amortized_ms = build_ms / _QUERIES_PER_SESSION
        passed = amortized_ms <= THRESHOLDS["L3"]["amortized_ms_max"]
        r = ScenarioResult(
            scenario="L3",
            name="latency-index-build",
            suite="latency",
            passed=passed,
            result={
                "build_ms": round(build_ms, 2),
                "amortized_ms": round(amortized_ms, 2),
                "amortization_factor": _QUERIES_PER_SESSION,
                "median_ms": round(amortized_ms, 2),
                "min_ms": round(amortized_ms, 2),
                "max_ms": round(build_ms, 2),
            },
            threshold=THRESHOLDS["L3"],
            notes=(
                f"build={build_ms:.0f}ms; amortized over {_QUERIES_PER_SESSION} queries/session "
                f"= {amortized_ms:.0f}ms (assumption, not telemetry)"
            ),
        )
    else:
        r = ScenarioResult(
            scenario="L3",
            name="latency-index-build",
            suite="latency",
            passed=False,
            result={"error": "scan-index binary not found", "median_ms": 0, "min_ms": 0, "max_ms": 0},
            threshold=THRESHOLDS["L3"],
            notes="scan-index binary not found; cannot measure build time",
        )
    results.append(r)

    # L4: cold grep baseline vs codemap
    log("[latency] L4: cold grep baseline vs codemap")
    repo = str(repo_path)
    test_mod = high_risk_mods[0] if high_risk_mods else tasks[0].primary_module
    pattern = module_to_grep_pattern(test_mod)
    pkg = module_to_package(test_mod) or test_mod

    cold_central = time_commands(
        [
            ["find", repo, "-name", "*.py", "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*"],
            ["grep", "-rn", r"^from \|^import ", repo, "--include=*.py", "-l"],
            ["grep", "-roh", r"from \([a-z_][a-z_.]*\) import\|^import \([a-z_][a-z_.]*\)", repo, "--include=*.py"],
        ]
    )
    cold_rdeps = time_commands(
        [
            ["grep", "-rn", pattern, repo, "--include=*.py"],
            ["grep", "-rn", f"from {pkg} import", repo, "--include=*.py"],
        ]
    )
    cold_total_median = cold_central.median_ms + cold_rdeps.median_ms
    warm_central_median = results[0].result["median_ms"]  # L1
    warm_rdeps_median = results[1].result["median_ms"]  # L2
    warm_total = warm_central_median + warm_rdeps_median
    speedup = cold_total_median / warm_total if warm_total > 0 else 0
    # Build-inclusive variant: fold the amortized one-time index build (from L3) into the warm side so
    # the showcase figure is honest. The pass gate stays on the warm-only (steady-state) speedup.
    build_ms = results[2].result.get("build_ms", 0)  # L3
    amortized_build_ms = build_ms / _QUERIES_PER_SESSION
    warm_total_build_inclusive = warm_total + amortized_build_ms
    speedup_build_inclusive = cold_total_median / warm_total_build_inclusive if warm_total_build_inclusive > 0 else 0
    passed = speedup >= THRESHOLDS["L4"]["speedup_min"]
    r = ScenarioResult(
        scenario="L4",
        name="latency-cold-grep-baseline",
        suite="latency",
        passed=passed,
        result={
            "cold_central_median_ms": cold_central.median_ms,
            "cold_rdeps_median_ms": cold_rdeps.median_ms,
            "cold_total_median_ms": round(cold_total_median, 2),
            "warm_total_ms": round(warm_total, 2),
            "speedup": round(speedup, 2),
            "amortized_build_ms": round(amortized_build_ms, 2),
            "warm_total_build_inclusive_ms": round(warm_total_build_inclusive, 2),
            "speedup_build_inclusive": round(speedup_build_inclusive, 2),
            "queries_per_session": _QUERIES_PER_SESSION,
            "median_ms": round(cold_total_median, 2),
            "min_ms": round(cold_central.min_ms + cold_rdeps.min_ms, 2),
            "max_ms": round(cold_central.max_ms + cold_rdeps.max_ms, 2),
        },
        threshold=THRESHOLDS["L4"],
        notes=(
            f"cold grep = {cold_total_median:.0f}ms; codemap warm = {warm_total:.0f}ms; "
            f"warm-only speedup = {speedup:.1f}x (gate); build-inclusive = {speedup_build_inclusive:.1f}x"
        ),
    )
    results.append(r)

    return results


# ---- SUITE: QUERY SHAPE ----


def _validate_skill_group(
    skill: str, tasks_for_skill: list[Task], scan_query_bin: Path, index_path: Path, repo_path: Path, threshold_key: str
) -> ScenarioResult:
    """Validate the scan-query output SHAPE for a skill group of tasks.

    For each task in ``tasks_for_skill``, runs every query defined on the task
    via :func:`run_query_shape_query` and checks that the result is both present
    (non-null) and structurally valid.  This checks output shape only — it does
    NOT invoke the skill or exercise its SKILL.md injection block.  Aggregates
    per-query pass/fail (and any scan-query errors) into a single
    :class:`ScenarioResult` for the whole skill group.

    Args:
        skill: Skill name, e.g. ``"fix"``, ``"feature"``, or ``"refactor"``.
            Used only for display labels and log messages.
        tasks_for_skill: Tasks to validate, filtered to this skill group.
        scan_query_bin: Path to the ``scan-query`` executable.
        index_path: Path to the pre-built codemap index JSON file.
        repo_path: Root directory of the repository under test.
        threshold_key: Key into :data:`THRESHOLDS` for this group (e.g.
            ``"Q_fix"``, ``"Q_feature"``, ``"Q_refactor"``).

    Returns:
        :class:`ScenarioResult` whose ``passed`` field is ``True`` only when
        every query across all tasks in the group is present and valid, and
        (for ``threshold_key="Q_refactor"``) at least one rdeps and one deps
        result was returned.
    """
    log(f"[query-shape] {threshold_key}: develop:{skill} ({len(tasks_for_skill)} tasks)")

    per_task_details: list[dict] = []
    errors: list[str] = []
    all_ok = True

    for task in tasks_for_skill:
        task_detail: dict = {"task_id": task.id, "module": task.primary_module}

        for q in task.queries:
            bp, jv, data, err = run_query_shape_query(scan_query_bin, index_path, repo_path, q)
            if err is not None:
                errors.append(f"{task.id} {q.cmd}: {err}")
            if q.cmd in ("central", "coupled"):
                task_detail[f"{q.cmd}_present"] = bp
                task_detail[f"{q.cmd}_valid"] = jv
                if not (bp and jv):
                    all_ok = False
            elif q.cmd == "rdeps":
                task_detail["rdeps_present"] = bp
                task_detail["rdeps_valid"] = jv
                task_detail["rdeps_count"] = len(data.get("imported_by", [])) if data else 0
                if not (bp and jv):
                    all_ok = False
            elif q.cmd == "deps":
                task_detail["deps_present"] = bp
                task_detail["deps_valid"] = jv
                task_detail["deps_count"] = len(data.get("direct_imports", [])) if data else 0
                if not (bp and jv):
                    all_ok = False
            elif q.cmd == "path":
                task_detail["path_present"] = bp
                if not bp:
                    all_ok = False

        per_task_details.append(task_detail)

    threshold = THRESHOLDS[threshold_key]
    has_rdeps = any(d.get("rdeps_present", False) for d in per_task_details)
    has_deps = any(d.get("deps_present", False) for d in per_task_details)

    passed = all_ok
    if threshold.get("has_rdeps"):
        passed = passed and has_rdeps
    if threshold.get("has_deps"):
        passed = passed and has_deps

    err_note = f"; {len(errors)} scan-query errors: {'; '.join(errors)}" if errors else ""
    return ScenarioResult(
        scenario=threshold_key,
        name=f"develop:{skill}",
        suite="query-shape",
        passed=passed,
        result={
            "block_present": all_ok,
            "json_valid": all_ok,
            "has_rdeps": has_rdeps,
            "has_deps": has_deps,
            "errors": errors,
            "task_count": len(tasks_for_skill),
            "per_task": per_task_details,
        },
        threshold=threshold,
        notes=f"validated {len(tasks_for_skill)} {skill} tasks (shape only, not the injection path){err_note}",
    )


def run_measure_query_shape(
    plugin_root: Path, repo_path: Path, scan_query_bin: Path, index_path: Path
) -> list[ScenarioResult]:
    """Run Suite Q — verify each skill group's scan-query output SHAPE is valid.

    Runs the same scan-query commands that develop:fix, develop:feature, and
    develop:refactor would issue, then validates the output structure.  The skill
    is NOT invoked and its SKILL.md injection block is NOT exercised; only the
    binary output shape is checked — a pass proves the queries return
    well-formed JSON, not that injection wiring is present or active.

    Args:
        plugin_root: Root of the plugin repository (used as ``cwd`` fallback).
        repo_path: Root of the pytorch-lightning repository.
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap JSON index.

    Returns:
        List of three :class:`ScenarioResult` objects (Q_fix, Q_feature, Q_refactor).
    """
    results: list[ScenarioResult] = []
    log("[query-shape] Starting query-shape validation...")

    for skill, key in [("fix", "Q_fix"), ("feature", "Q_feature"), ("refactor", "Q_refactor")]:
        r = _validate_skill_group(skill, load_tasks(skill_filter=skill), scan_query_bin, index_path, repo_path, key)
        results.append(r)

    return results


# ---- VERIFY TASKS ----


def run_verify_tasks(scan_query_bin: Path, index_path: Path, repo_path: Path) -> None:
    """Verify that all task primary_modules exist in the index with status 'ok'."""
    log("[verify] Checking task modules against index...")
    tasks = load_tasks()

    if not index_path.exists():
        log(f"[verify] ERROR: index not found at {index_path}")
        return

    try:
        with index_path.open() as f:
            idx = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log(f"[verify] ERROR: cannot read index: {exc}")
        return

    module_map = {m["name"]: m for m in idx.get("modules", [])}

    central_data = run_scan_query(scan_query_bin, ["central", "--top", "15"], index_path, repo_path)
    top_names: list[str] = (
        [c["name"] for c in central_data["central"]] if central_data and "central" in central_data else []
    )

    task_modules = {t.primary_module for t in tasks}
    missing: list[str] = []

    for task in tasks:
        entry = module_map.get(task.primary_module)
        if entry is None:
            log(f"[verify] WARN: {task.id} {task.primary_module} -- NOT FOUND in index")
            missing.append(task.primary_module)
        elif entry.get("status") != "ok":
            log(f"[verify] WARN: {task.id} {task.primary_module} -- status={entry.get('status')} (not 'ok')")
        else:
            log(f"[verify] OK: {task.id} {task.primary_module} -- rdep_count={entry.get('rdep_count', 0)}, status=ok")

    if missing:
        candidates = [n for n in top_names if n not in task_modules]
        if candidates:
            log(f"[verify] Suggested substitutes from central --top 15: {candidates[: len(missing)]}")
        else:
            log("[verify] No substitute candidates available from central --top 15")


# ---- SUITE S — SYMBOL LOOKUP ----


def run_suite_symbol(
    scan_query_bin: Path,
    index_path: Path,
    repo_path: Path,
) -> list[ScenarioResult]:
    """Suite S: symbol line-range self-consistency / determinism check.

    Runs ``symbol`` for each S-task in tasks-bench.json and compares
    ``start_line`` / ``end_line`` against ground truth (±3 lines, for decorator
    vs def).  Ground truth is scan-query-derived, so this validates determinism /
    index-version stability, not correctness — self-consistency track, EXCLUDED
    from the primary verdict.

    Args:
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap index.
        repo_path: Root of the pytorch-lightning repository.

    Returns:
        List of ScenarioResult — one per S-task, plus an aggregate S2 (all-pass rate).
    """
    tasks = load_oss_tasks(type_filter="symbol_extraction")
    if not tasks:
        log("[suite-S] tasks-bench.json not found or no symbol_extraction tasks — skipping")
        return []

    results: list[ScenarioResult] = []
    passed_count = 0

    for task in tasks:
        task_id = task["id"]
        gt = task.get("ground_truth", {})
        qname = gt.get("qualified_name", "")
        module = gt.get("module", "")
        expected_start = gt.get("start_line", 0)
        expected_end = gt.get("end_line", 0)

        sq = run_scan_query_result(scan_query_bin, ["symbol", qname], index_path, repo_path)
        if not sq.ok:
            results.append(
                ScenarioResult(
                    scenario=f"S_{task_id}",
                    name=f"symbol-{task_id}",
                    suite="symbol",
                    passed=False,
                    result={"error": sq.error},
                    threshold=THRESHOLDS["S1"],
                    notes=f"qname={qname}: {sq.error}",
                )
            )
            continue
        data = sq.data

        symbols = data.get("symbols", [])
        match = next(
            (s for s in symbols if s.get("qualified_name") == qname and s.get("module") == module),
            None,
        ) or next((s for s in symbols if s.get("qualified_name") == qname), None)

        symbol_found = match is not None
        start_ok = symbol_found and abs(match.get("start_line", 0) - expected_start) <= 3
        end_ok = symbol_found and abs(match.get("end_line", 0) - expected_end) <= 3
        passed = symbol_found and start_ok

        if passed:
            passed_count += 1

        results.append(
            ScenarioResult(
                scenario=f"S_{task_id}",
                name=f"symbol-{task_id}",
                suite="symbol",
                passed=passed,
                result={
                    "symbol_found": symbol_found,
                    "start_line_ok": start_ok,
                    "end_line_ok": end_ok,
                    "got_start": match.get("start_line") if match else None,
                    "got_end": match.get("end_line") if match else None,
                    "expected_start": expected_start,
                    "expected_end": expected_end,
                    "count_returned": len(symbols),
                },
                threshold=THRESHOLDS["S1"],
                notes=f"qname={qname} module={module}",
            )
        )

    # Aggregate: S2 = all symbol tasks passed
    total = len(tasks)
    all_pass_rate = passed_count / total if total else 0.0
    results.append(
        ScenarioResult(
            scenario="S2",
            name="symbol-all-pass-rate",
            suite="symbol",
            passed=passed_count == total,
            result={"passed": passed_count, "total": total, "pass_rate": round(all_pass_rate, 3)},
            threshold=THRESHOLDS["S2"],
            notes=f"{passed_count}/{total} symbol tasks passed",
        )
    )
    return results


# ---- SUITE H — HEALTH (undocumented / uncovered) ----


def run_suite_health(
    scan_query_bin: Path,
    index_path: Path,
    repo_path: Path,
) -> list[ScenarioResult]:
    """Suite H: undocumented/uncovered count self-consistency / determinism check.

    Runs each code_quality task using ``undocumented`` / ``uncovered`` and checks the returned
    ``total`` against the ground-truth ``*_count_scan`` diagnostic (the frozen scan-query snapshot
    recorded alongside the now-independent AST-oracle GT — see generate-tasks-bench.py
    ``_validate_undocumented_ast``/``_validate_uncovered_ast``). This is a regression/determinism
    check against scan-query's own prior output, not independent correctness — self-consistency
    track, EXCLUDED from the verdict.

    Args:
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap index.
        repo_path: Root of the pytorch-lightning repository.

    Returns:
        List of ScenarioResult — one per CQ-task plus H1/H2 aggregates.
    """
    tasks = load_oss_tasks(type_filter="code_quality")
    if not tasks:
        log("[suite-H] tasks-bench.json not found or no code_quality tasks — skipping")
        return []

    results: list[ScenarioResult] = []
    undoc_tasks_passed = undoc_tasks_total = 0
    uncov_tasks_passed = uncov_tasks_total = 0

    for task in tasks:
        task_id = task["id"]
        gt = task.get("ground_truth", {})
        check = gt.get("check", "")
        expected_queries = task.get("expected_queries", [])

        for q in expected_queries:
            cmd = q.get("cmd", "")
            if cmd not in ("undocumented", "uncovered"):
                continue

            args = [cmd] + q.get("args", [])
            sq = run_scan_query_result(scan_query_bin, args, index_path, repo_path)

            # ``*_count`` is now the independent AST oracle's authoritative value (see
            # generate-tasks-bench.py _validate_undocumented_ast / _validate_uncovered_ast); this
            # suite checks scan-query determinism against its OWN prior output, so it reads the
            # ``*_count_scan`` diagnostic field, falling back to ``*_count`` for older task entries
            # that predate the oracle migration and never got a ``*_count_scan`` field written.
            if cmd == "undocumented":
                expected_count = gt.get("undocumented_count_scan", gt.get("undocumented_count", gt.get("count", 0)))
                suite_key = "H1"
                undoc_tasks_total += 1
            else:
                expected_count = gt.get("uncovered_count_scan", gt.get("uncovered_count", gt.get("count", 0)))
                suite_key = "H2"
                uncov_tasks_total += 1

            if not sq.ok:
                passed = False
                got_count = None
            else:
                got_count = (sq.data or {}).get("total", None)
                passed = got_count == expected_count

            if passed:
                if cmd == "undocumented":
                    undoc_tasks_passed += 1
                else:
                    uncov_tasks_passed += 1

            results.append(
                ScenarioResult(
                    scenario=f"H_{task_id}_{cmd}",
                    name=f"health-{task_id}-{cmd}",
                    suite="health",
                    passed=passed,
                    result={
                        "count_match": passed,
                        "expected": expected_count,
                        "got": got_count,
                        "check": check,
                    },
                    threshold=THRESHOLDS[suite_key],
                    notes=f"task={task_id} cmd={cmd} args={q.get('args', [])}",
                )
            )

    # Aggregates
    if undoc_tasks_total:
        results.append(
            ScenarioResult(
                scenario="H1",
                name="health-undocumented",
                suite="health",
                passed=undoc_tasks_passed == undoc_tasks_total,
                result={
                    "count_match": undoc_tasks_passed == undoc_tasks_total,
                    "passed": undoc_tasks_passed,
                    "total": undoc_tasks_total,
                },
                threshold=THRESHOLDS["H1"],
                notes=f"{undoc_tasks_passed}/{undoc_tasks_total} undocumented tasks matched",
            )
        )
    if uncov_tasks_total:
        results.append(
            ScenarioResult(
                scenario="H2",
                name="health-uncovered",
                suite="health",
                passed=uncov_tasks_passed == uncov_tasks_total,
                result={
                    "count_match": uncov_tasks_passed == uncov_tasks_total,
                    "passed": uncov_tasks_passed,
                    "total": uncov_tasks_total,
                },
                threshold=THRESHOLDS["H2"],
                notes=f"{uncov_tasks_passed}/{uncov_tasks_total} uncovered tasks matched",
            )
        )
    return results


# ---- SUITE X — XREFS BROKEN ----


def run_suite_xrefs(
    scan_query_bin: Path,
    index_path: Path,
    repo_path: Path,
) -> list[ScenarioResult]:
    """Suite X: ``xrefs --broken`` self-consistency / determinism check.

    Runs ``xrefs --broken`` for each OSS task with ``check == "xrefs_broken"`` and confirms both
    the broken count and the broken target/line pairs against the ground-truth ``broken_*_scan``
    diagnostic (the frozen scan-query snapshot recorded alongside the now-independent AST-oracle
    GT — see generate-tasks-bench.py ``_validate_xrefs_ast``). This validates scan-query
    determinism against its own prior output, not independent correctness — self-consistency
    track, EXCLUDED from the primary verdict.

    Args:
        scan_query_bin: Path to the scan-query executable.
        index_path: Path to the pre-built codemap index.
        repo_path: Root of the pytorch-lightning repository.

    Returns:
        List of ScenarioResult — one per xrefs_broken task plus X1 aggregate.
    """
    tasks = [
        t
        for t in load_oss_tasks(type_filter="code_quality")
        if t.get("ground_truth", {}).get("check") == "xrefs_broken"
    ]
    if not tasks:
        log("[suite-X] no xrefs_broken tasks in tasks-bench.json — skipping")
        return []

    results: list[ScenarioResult] = []
    x_passed = x_total = 0

    for task in tasks:
        task_id = task["id"]
        gt = task.get("ground_truth", {})
        # broken_count/broken_targets are now the AST oracle's authoritative value; this suite
        # checks scan-query against its OWN prior output, so it reads the *_scan diagnostic,
        # falling back for older task entries that predate the oracle migration.
        expected_count = gt.get("broken_count_scan", gt.get("broken_count", 0))
        expected_targets = {
            (t["target"], t["line"]) for t in gt.get("broken_targets_scan", gt.get("broken_targets", []))
        }
        expected_queries = task.get("expected_queries", [])

        q = next((q for q in expected_queries if q.get("cmd") == "xrefs"), None)
        if q is None:
            continue

        args = ["xrefs"] + q.get("args", [])
        sq = run_scan_query_result(scan_query_bin, args, index_path, repo_path)
        x_total += 1

        if not sq.ok:
            results.append(
                ScenarioResult(
                    scenario=f"X_{task_id}",
                    name=f"xrefs-broken-{task_id}",
                    suite="xrefs",
                    passed=False,
                    result={"error": sq.error, "count_match": False},
                    threshold=THRESHOLDS["X1"],
                    notes=f"task={task_id}: {sq.error}",
                )
            )
            continue

        data = sq.data
        broken = data.get("broken", [])
        got_count = data.get("count", len(broken))
        got_targets = {(b.get("target", ""), b.get("line", 0)) for b in broken}

        count_match = got_count == expected_count
        targets_match = got_targets == expected_targets
        passed = count_match and targets_match

        if passed:
            x_passed += 1

        results.append(
            ScenarioResult(
                scenario=f"X_{task_id}",
                name=f"xrefs-broken-{task_id}",
                suite="xrefs",
                passed=passed,
                result={
                    "count_match": count_match,
                    "targets_match": targets_match,
                    "expected_count": expected_count,
                    "got_count": got_count,
                    "missing_targets": sorted(expected_targets - got_targets),
                    "extra_targets": sorted(got_targets - expected_targets),
                },
                threshold=THRESHOLDS["X1"],
                notes=f"task={task_id} args={q.get('args', [])}",
            )
        )

    if x_total:
        results.append(
            ScenarioResult(
                scenario="X1",
                name="xrefs-broken-all",
                suite="xrefs",
                passed=x_passed == x_total,
                result={"count_match": x_passed == x_total, "passed": x_passed, "total": x_total},
                threshold=THRESHOLDS["X1"],
                notes=f"{x_passed}/{x_total} xrefs_broken tasks matched",
            )
        )
    return results
