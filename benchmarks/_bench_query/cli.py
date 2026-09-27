"""Command-line entry point for the codemap-cli benchmark."""

from __future__ import annotations

import json
import sys
from pathlib import Path


from _bench_common.codemap_discovery import (
    find_codemap_bin,
)

from _bench_query.scoring import compute_verdict
from _bench_query.output import _OUT, _run_all_suites, emit, log
from _bench_query.report import build_summary_envelope, write_report_file
from _bench_query.paths import (
    _SELF_CONSISTENCY_MIN_VER,
    _ensure_index,
    _index_scan_version,
    _resolve_plugin_root,
    resolve_index_path,
    resolve_repo_path,
)
from _bench_query.suites import (
    run_measure_accuracy,
    run_measure_calls,
    run_measure_latency,
    run_measure_query_shape,
    run_suite_health,
    run_suite_symbol,
    run_suite_xrefs,
    run_verify_tasks,
)
from _bench_query.fixtures import (
    run_correctness_batch,
    run_correctness_diff_impact,
    run_correctness_self_check,
    run_correctness_src_roots,
    run_correctness_uncovered_xrefs,
)


def main(
    repo_path: str = None,
    index_path: str = None,
    report: bool = False,
    json_only: bool = False,
    verify_tasks: bool = False,
) -> None:
    """Run the codemap scan-query benchmark suite against a pytorch-lightning clone.

    Runs primary suites C (coverage gap), A (accuracy), L (latency), Q (query
    shape), then the self-consistency suites S/H/X (skipped on an index older
    than ``_SELF_CONSISTENCY_MIN_VER``).  Prints the primary verdict plus the
    separate self-consistency line; optionally writes a markdown report.  Exposed
    via ``python benchmarks/run-codemap-cli.py`` (:func:`fire.Fire`); CLI flags
    are the parameter names with ``_``→``-`` (e.g. ``--repo-path``).

    Args:
        repo_path: pytorch-lightning clone; falls back to
            ``$PYTORCH_LIGHTNING_PATH`` then ``./pytorch-lightning``.
        index_path: Pre-built codemap JSON index; auto-resolved when omitted.
        report: Write ``benchmarks/results/code-<date>.md`` (ignored under ``json_only``).
        json_only: Suppress the report; emit scenario JSONL + envelope only.
        verify_tasks: Before running suites, check each task's ``primary_module``
            exists in the index with status ``ok``.

    Examples:
        # Full benchmark with markdown report
        python benchmarks/run-codemap-cli.py --repo-path ./pytorch-lightning --report
    """
    write_report = report and not json_only
    _OUT.quiet = json_only  # suppress human progress narration for machine consumers

    plugin_root = _resolve_plugin_root()
    repo_path = resolve_repo_path(repo_path)
    if repo_path is None:
        sys.exit(1)

    scan_query_bin = find_codemap_bin("scan-query", plugin_root)
    scan_index_bin = find_codemap_bin("scan-index", plugin_root)
    if scan_query_bin is None:
        log("ERROR: scan-query not found in PATH or plugin directory")
        sys.exit(1)

    index_path = _ensure_index(resolve_index_path(index_path, repo_path), repo_path, scan_index_bin)

    # Verify tasks if requested (runs before suites, does not skip them)
    if verify_tasks:
        run_verify_tasks(scan_query_bin, index_path, repo_path)

    suites: list[tuple[str, object]] = [
        ("C — Coverage gap", lambda: run_measure_calls(repo_path, scan_query_bin, index_path)),
        ("A — Accuracy", lambda: run_measure_accuracy(repo_path, scan_query_bin, index_path)),
        ("L — Latency", lambda: run_measure_latency(repo_path, scan_query_bin, index_path, scan_index_bin)),
        (
            "Q — Query shape",
            lambda: run_measure_query_shape(plugin_root or Path.cwd(), repo_path, scan_query_bin, index_path),
        ),
        # Deterministic correctness suites: self-contained fixture repos (own tmp dir + index),
        # KNOWN ground truth → independent-oracle, so they join the primary verdict. Independent of
        # repo_path/index_path; each skips internally when scan-index is unavailable.
        ("D — diff-impact (fixture)", lambda: run_correctness_diff_impact(scan_query_bin, scan_index_bin)),
        ("B — batch (fixture)", lambda: run_correctness_batch(scan_query_bin, scan_index_bin)),
        ("R — src_roots (fixture)", lambda: run_correctness_src_roots(scan_query_bin, scan_index_bin)),
        ("K — self-check (fixture)", lambda: run_correctness_self_check(scan_query_bin, scan_index_bin)),
        ("U — uncovered/xrefs (fixture)", lambda: run_correctness_uncovered_xrefs(scan_query_bin, scan_index_bin)),
    ]
    # Stale-index guard: skip the self-consistency track (never the verdict) when the index
    # predates the fields S/H/X read, rather than letting each suite fail cryptically.
    found_ver = _index_scan_version(index_path)
    if found_ver >= _SELF_CONSISTENCY_MIN_VER:
        suites += [
            ("S — Symbol lookup", lambda: run_suite_symbol(scan_query_bin, index_path, repo_path)),
            ("H — Health (doc/cov)", lambda: run_suite_health(scan_query_bin, index_path, repo_path)),
            ("X — Xrefs broken", lambda: run_suite_xrefs(scan_query_bin, index_path, repo_path)),
        ]
    else:
        log(
            f"[index] scan_version {found_ver} < {_SELF_CONSISTENCY_MIN_VER} — self-consistency suites "
            f"(S/H/X) skipped (no compatible ground truth). Rebuild: scan-index --root {repo_path}"
        )

    all_results = _run_all_suites(suites, use_progress=not json_only)
    verdict = compute_verdict(all_results)
    envelope = build_summary_envelope(all_results, repo_path, index_path, verdict)

    # ``--json-only``: emit scenario JSONL + summary envelope on stdout, nothing else.
    if json_only:
        for r in all_results:
            emit(r)
        print(json.dumps(envelope, separators=(",", ":"), default=str))
        return

    # Default mode: human verdict line, summary envelope, optional markdown report.
    report_path_str: str | None = None
    if write_report and all_results:
        report_path_str = write_report_file(all_results, repo_path, index_path)

    sc = envelope["self_consistency"]
    print(f"\n{verdict}  {envelope['primary']['passed']}/{envelope['primary']['total']} primary scenarios passed")
    print(f"self-consistency: {sc['verdict']}  {sc['passed']}/{sc['total']} (determinism track, not in verdict)")
    print(json.dumps(envelope, separators=(",", ":"), default=str))
    if report_path_str:
        print(f"→ {report_path_str}")
