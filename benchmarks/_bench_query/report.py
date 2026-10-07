"""Markdown report rendering and the machine-readable summary envelope."""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import date
from pathlib import Path

import pandas as pd

from _bench_query.models import ScenarioResult, SuiteStats
from _bench_query.scoring import _PRIMARY_SUITES, _tally, compute_self_consistency, compute_verdict

# ---- REPORT ----


def _hardware_info() -> dict[str, str | int | None]:
    """Capture stdlib-only host identity (platform/processor/cpu_count/python).

    L1/L2/L3 latency gates are hardware-calibrated; recording the host in the report header and JSON envelope keeps a
    slow CI runner's pass/fail flip interpretable.
    """
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }


def render_report(
    results: list[ScenarioResult],
    repo_path: Path,
    index_path: Path,
    report_path: Path,
) -> None:
    """Render a markdown benchmark report with numeric values and relative margins.

    Args:
        results: Evaluated scenario results from all suites.
        repo_path: Path to the repository under test.
        index_path: Path to the codemap JSON index.
        report_path: Destination path for the markdown report.
    """
    lines: list[str] = []
    today = date.today().isoformat()

    # --- Gather repo info ---
    git_sha = "unknown"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git/tool resolved via PATH on purpose
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_path),
        )
        if r.returncode == 0:
            git_sha = r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass

    # --- Count modules in index ---
    mod_count = 0
    degraded_count = 0
    if index_path.exists():
        try:
            with index_path.open() as f:
                idx = json.load(f)
            mods = idx.get("modules", [])
            mod_count = len(mods)
            degraded_count = sum(1 for m in mods if m.get("status") == "degraded")
        except (json.JSONDecodeError, OSError):
            pass

    # --- Compute suite verdicts ---
    suite_results: dict[str, SuiteStats] = {}
    for r_item in results:
        s = suite_results.setdefault(r_item.suite, SuiteStats())
        s.total += 1
        if r_item.passed:
            s.passed += 1
        else:
            s.failed += 1

    verdict = compute_verdict(results)
    primary_passed, primary_total = _tally(results, _PRIMARY_SUITES)
    self_consistency = compute_self_consistency(results)

    # --- Header ---
    lines.append(f"# Codemap Benchmark Report -- {today}")
    lines.append("")
    pass_pct = primary_passed / primary_total if primary_total else 0
    lines.append(
        f"**Verdict** (primary correctness): {verdict} — {primary_passed}/{primary_total} "
        f"primary scenarios ({pass_pct:.0%})"
    )
    lines.append(
        f"**Self-consistency** (determinism track, excluded from verdict): "
        f"{self_consistency['verdict']} — {self_consistency['passed']}/{self_consistency['total']}"
    )
    lines.append(f"**pytorch-lightning**: commit {git_sha}")
    lines.append(f"**Index**: {index_path} ({mod_count} modules, {degraded_count} degraded)")
    hw = _hardware_info()
    lines.append(
        f"**Hardware** (latency thresholds are hardware-calibrated): {hw['platform']} · "
        f"{hw['processor']} · {hw['cpu_count']} CPUs · Python {hw['python']}"
    )
    lines.append("")

    # --- Summary table (primary suites decide the verdict) ---
    lines.append("## Summary Table — primary suites")
    lines.append("")
    suite_display = {
        "calls": "Call Savings",
        "accuracy": "Accuracy",
        "latency": "Latency",
        "query-shape": "Query Shape",
        "correctness": "Correctness (fixtures)",
    }
    suite_rows: list[dict] = []
    for key, label in suite_display.items():
        if key in suite_results:
            s = suite_results[key]
            rate = s.passed / s.total if s.total else 0
            if s.failed == 0:
                status = "\u2713"
            elif s.passed > 0:
                status = "~"
            else:
                status = "\u2717"
            suite_rows.append(
                {
                    "Suite": label,
                    "Scenarios": s.total,
                    "Pass Rate": f"{s.passed}/{s.total} ({rate:.0%})",
                    "Status": status,
                }
            )
    lines.append(pd.DataFrame(suite_rows).to_markdown(index=False))
    lines.append("")

    # --- Self-consistency (determinism) table — NOT counted in the verdict ---
    sc_display = {"symbol": "Symbol (S)", "health": "Health (H)", "xrefs": "Xrefs (X)"}
    sc_rows: list[dict] = []
    for key, label in sc_display.items():
        if key not in suite_results:
            continue
        s = suite_results[key]
        rate = s.passed / s.total if s.total else 0
        status = "✓" if s.failed == 0 else ("~" if s.passed > 0 else "✗")
        sc_rows.append(
            {"Suite": label, "Scenarios": s.total, "Pass Rate": f"{s.passed}/{s.total} ({rate:.0%})", "Status": status}
        )
    if sc_rows:
        lines.append("## Self-Consistency (determinism) Checks — NOT in the verdict")
        lines.append("")
        lines.append(
            "> Ground truth here is derived from scan-query's own output (frozen in tasks-bench.json), "
            "so a pass confirms determinism / index-version stability, not independent correctness."
        )
        lines.append("")
        lines.append(pd.DataFrame(sc_rows).to_markdown(index=False))
        lines.append("")
    elif self_consistency["verdict"] == "SKIPPED":
        lines.append("## Self-Consistency (determinism) Checks — skipped (no ground truth)")
        lines.append("")
        lines.append(
            "> Suites S/H/X did not run (tasks-bench.json absent or index too old); the primary "
            "verdict above is unaffected — it is computed from the primary track only."
        )
        lines.append("")

    # --- Call Savings table ---
    calls_items = [r for r in results if r.suite == "calls"]
    if calls_items:
        calls_rows: list[dict] = []
        for r_item in calls_items:
            res = r_item.result
            scen = r_item.scenario
            if scen == "C1":
                val = res.get("coverage_gap", 0)
                calls_rows.append(
                    {
                        "Scenario": f"{scen} {r_item.name}",
                        "Value": f"{val:.1%}",
                        "Notes": f"{res.get('verified_extras_total', '?')} verified extras / {res.get('codemap_set_total', '?')} importers",
                    }
                )
            elif scen == "C2":
                val = res.get("fraction", 0)
                calls_rows.append(
                    {
                        "Scenario": f"{scen} {r_item.name}",
                        "Value": f"{val:.1%}",
                        "Notes": f"{res.get('infeasible_count', '?')}/{res.get('total_path_queries', '?')} paths need >1 grep",
                    }
                )
            elif scen == "C3":
                val = res.get("leverage_ratio", 0)
                calls_rows.append(
                    {
                        "Scenario": f"{scen} {r_item.name}",
                        "Value": f"{val:.1f}×",
                        "Notes": (
                            f"{res.get('total_cold_planned_calls', '?')} cold / "
                            f"{res.get('total_warm_planned_calls', '?')} warm (planned invocations)"
                        ),
                    }
                )
        lines.append("## Call Savings\n")
        lines.append(pd.DataFrame(calls_rows).to_markdown(index=False))
        lines.append("")

    # --- Accuracy table (unified A1 + A2 with Suite column; A3 as summary line) ---
    acc_items = [r for r in results if r.suite == "accuracy"]
    if acc_items:
        a1 = next((r for r in acc_items if r.scenario == "A1"), None)
        a2 = next((r for r in acc_items if r.scenario == "A2"), None)
        a3 = next((r for r in acc_items if r.scenario == "A3"), None)

        acc_rows: list[dict] = []
        for suite_label, item in [("A1", a1), ("A2", a2)]:
            if item and "per_module" in item.result:
                for pm in item.result["per_module"]:
                    acc_rows.append(
                        {
                            "Suite": suite_label,
                            "Module": pm["module"],
                            "Recall": f"{pm['recall']:.2f}",
                            "Precision": f"{pm['precision']:.2f}",
                            "Codemap": pm["codemap_count"],
                            "Grep": pm["grep_count"],
                            "TP": pm["tp"],
                            "FP": pm["fp"],
                            "FN": pm["fn"],
                        }
                    )

        summary_parts = []
        if a1 and "avg_precision" in a1.result:
            summary_parts.append(
                f"A1 avg precision={a1.result['avg_precision']:.2f}  recall={a1.result.get('avg_recall', 0):.2f}"
            )
        if a2 and "min_precision" in a2.result:
            summary_parts.append(f"A2 min precision={a2.result['min_precision']:.2f}")
        if a3:
            fp_rate = a3.result.get("fp_rate", 0)
            total_cm = a3.result.get("total_codemap_results", "?")
            total_fp = a3.result.get("total_false_positives", "?")
            summary_parts.append(f"A3 FP rate={fp_rate:.2%} ({total_fp} FP / {total_cm} total)")

        lines.append("## Accuracy\n")
        if summary_parts:
            lines.append("> " + "  |  ".join(summary_parts))
            lines.append("")
        if acc_rows:
            lines.append(pd.DataFrame(acc_rows).to_markdown(index=False))
        lines.append("")

    # --- Latency table ---
    lat_items = [r for r in results if r.suite == "latency"]
    if lat_items:
        lat_rows: list[dict] = []
        for r_item in lat_items:
            res = r_item.result
            scen = r_item.scenario
            if scen == "L4":
                speedup = res.get("speedup", 0)
                lat_rows.append(
                    {
                        "Scenario": f"{scen} {r_item.name}",
                        "Measured": f"{speedup:.1f}×",
                        "Notes": f"cold {res.get('cold_total_median_ms', 0):.0f} ms  codemap {res.get('warm_total_ms', 0):.0f} ms",
                    }
                )
            else:
                median_ms = res.get("median_ms", 0)
                lat_rows.append(
                    {
                        "Scenario": f"{scen} {r_item.name}",
                        "Measured": f"{median_ms:.1f} ms",
                        "Notes": f"min {res.get('min_ms', 0):.1f}  max {res.get('max_ms', 0):.1f}",
                    }
                )
        lines.append("## Latency\n")
        lines.append(
            f"> Thresholds are hardware-calibrated; measured on {hw['platform']} "
            f"({hw['cpu_count']} CPUs). Compare cross-machine numbers against this host.\n"
        )
        lines.append(pd.DataFrame(lat_rows).to_markdown(index=False))
        lines.append("")

    # --- Query-shape table ---
    inj_items = [r for r in results if r.suite == "query-shape"]
    if inj_items:
        inj_rows: list[dict] = []
        for r_item in inj_items:
            res = r_item.result
            per_task = res.get("per_task", [])
            total = res.get("task_count", len(per_task))
            ok_count = sum(
                1 for d in per_task if all(v for k, v in d.items() if k.endswith("_present") or k.endswith("_valid"))
            )
            coverage = f"{ok_count / total:.0%}" if total else "N/A"
            inj_rows.append(
                {
                    "Scenario": r_item.scenario,
                    "Skill": r_item.name,
                    "Tasks OK": f"{ok_count}/{total}",
                    "Coverage": coverage,
                    "has_rdeps": "Yes" if res.get("has_rdeps") else "No",
                    "has_deps": "Yes" if res.get("has_deps") else "No",
                }
            )
        lines.append("## Query-Shape Validation (scan-query output shape only — not the injection path)\n")
        lines.append(pd.DataFrame(inj_rows).to_markdown(index=False))
        lines.append("")

    # --- Deterministic correctness table (fixture repos with KNOWN ground truth) ---
    corr_items = [r for r in results if r.suite == "correctness"]
    if corr_items:
        corr_rows: list[dict] = []
        for r_item in corr_items:
            checks = r_item.result.get("checks", {})
            passed_n = sum(1 for ok in checks.values() if ok)
            failed = r_item.result.get("failed_checks", []) or (
                [r_item.result["error"]] if r_item.result.get("error") else []
            )
            corr_rows.append(
                {
                    "Scenario": r_item.scenario,
                    "Name": r_item.name,
                    "Checks": f"{passed_n}/{len(checks)}" if checks else "setup-error",
                    "Status": "✓" if r_item.passed else "✗",
                    "Failed": ", ".join(failed) if failed else "-",
                }
            )
        lines.append("## Deterministic Correctness (fixture repos — independent-oracle, in the verdict)\n")
        lines.append(
            "> Each suite builds a self-contained tmp repo whose ground truth is KNOWN by construction "
            "(not derived from scan-query output), so a pass is genuine correctness — these count in the verdict.\n"
        )
        lines.append(pd.DataFrame(corr_rows).to_markdown(index=False))
        lines.append("")

    # --- False positive analysis (unchanged) ---
    fp_modules: list[dict] = []
    for r_item in results:
        if r_item.suite == "accuracy":
            res = r_item.result
            if "per_module" in res:
                fp_modules.extend(pm for pm in res["per_module"] if pm.get("fp_list"))
            elif res.get("fp_list"):
                fp_modules.append(res)
    if fp_modules:
        lines.append("## False Positive Analysis")
        lines.append("")
        for pm in fp_modules:
            mod_name = pm.get("module", "unknown")
            for fp_item in pm["fp_list"]:
                lines.append(
                    f"- **{mod_name}**: false positive `{fp_item}`"
                    " -- likely conditional/dynamic import"
                    " or re-export via __init__.py"
                )
        lines.append("")

    # --- Limitations (unchanged) ---
    lines.append("## Limitations")
    lines.append("")
    lines.append("- Cold call simulation is a lower bound -- real agents may issue more exploratory calls")
    lines.append("- Accuracy tested at one point in time against one version of pytorch-lightning")
    lines.append("- Latency results are hardware-dependent; thresholds calibrated for modern laptop (M1/M2)")
    lines.append("- Query-shape (Q) suite validates scan-query output structure only, NOT the skill injection path")
    lines.append("- Symbol/health/xrefs (S/H/X) are self-consistency/determinism checks, excluded from the verdict")
    lines.append("- Index staleness detection is not tested")
    lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def build_summary_envelope(results: list[ScenarioResult], repo_path: Path, index_path: Path, verdict: str) -> dict:
    """Build the final one-line summary envelope for machine consumption.

    Aggregates per-suite pass/total counts (keyed by ``suite``), the primary
    pass/total the verdict derives from, the separate self-consistency track,
    scenario totals, the hardware fingerprint, date, repo, and index path.

    Args:
        results: All scenario results produced by the benchmark run.
        repo_path: Path to the repository under test.
        index_path: Path to the codemap index used.
        verdict: Primary correctness verdict (``PASS`` / ``PARTIAL`` / ``FAIL``).

    Returns:
        A JSON-serializable summary envelope dict.

    Examples:
        >>> r = ScenarioResult("C1", "x", "calls", True, {}, {})
        >>> env = build_summary_envelope([r], Path("/repo"), Path("/i.json"), "PASS")
        >>> env["primary"], env["suites"]["calls"]
        ({'passed': 1, 'total': 1}, {'passed': 1, 'total': 1})
    """
    suites: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = suites.setdefault(r.suite, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if r.passed:
            bucket["passed"] += 1
    primary_passed, primary_total = _tally(results, _PRIMARY_SUITES)
    return {
        "verdict": verdict,
        "scenarios_passed": sum(1 for r in results if r.passed),
        "scenarios_total": len(results),
        "primary": {"passed": primary_passed, "total": primary_total},
        "self_consistency": compute_self_consistency(results),
        "suites": suites,
        "hardware": _hardware_info(),
        "date": date.today().isoformat(),
        "repo": str(repo_path),
        "index": str(index_path),
    }


def write_report_file(results: list[ScenarioResult], repo_path: Path, index_path: Path) -> str:
    """Resolve the report path once, create its parent, render, and return the path.

    Resolving the destination a single time (not re-calling
    :func:`resolve_report_path` after the file exists) guarantees the returned
    path is exactly the file written — not a ``-2`` sibling that never got created.

    Args:
        results: Scenario results to render.
        repo_path: Path to the repository under test.
        index_path: Path to the codemap index used.

    Returns:
        String path of the markdown report actually written.
    """
    report_path = resolve_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    render_report(results, repo_path, index_path, report_path)
    return str(report_path)


# ---- REPORT PATH ----


def resolve_report_path() -> Path:
    """Return a non-conflicting path for the benchmark markdown report (pure — no I/O).

    Computes ``benchmarks/results/code-<YYYY-MM-DD>.md`` for the first run on a
    given day, appending ``-2``, ``-3``, ... when earlier files already exist.
    Creating the parent directory is the caller's responsibility (see
    :func:`write_report_file`) so resolution stays side-effect free.

    Returns:
        :class:`~pathlib.Path` to a file that does not yet exist.
    """
    today = date.today().isoformat()
    base_dir = Path("benchmarks") / "results"
    candidate = base_dir / f"code-{today}.md"
    counter = 2
    while candidate.exists():
        candidate = base_dir / f"code-{today}-{counter}.md"
        counter += 1
    return candidate
