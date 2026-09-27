"""Accuracy scoring and the primary/self-consistency verdict tallies."""

from __future__ import annotations

from pathlib import Path

from _bench_query.models import AccuracyStats, ScenarioResult
from _bench_query.sources import verify_importer


def compute_precision_recall(codemap_set: set[str], grep_set: set[str]) -> AccuracyStats:
    """Compute precision and recall of codemap rdeps relative to a grep baseline.

    Uses grep as the reference set (not ground truth — grep can miss aliased or
    conditional imports).  Precision measures how much of what codemap returns
    is confirmed by grep; recall measures how much of what grep finds codemap
    also returns.

    Args:
        codemap_set: Set of module names returned by scan-query rdeps.
        grep_set: Set of module names found by the grep baseline.

    Returns:
        :class:`AccuracyStats` with precision, recall, TP/FP/FN counts, and the
        sorted lists of false-positive and false-negative module names.
        Precision defaults to ``1.0`` when ``codemap_set`` is empty; recall
        defaults to ``1.0`` when ``grep_set`` is empty.
    """
    tp_set = codemap_set & grep_set
    fp_set = codemap_set - grep_set
    fn_set = grep_set - codemap_set
    precision = len(tp_set) / len(codemap_set) if codemap_set else 1.0
    recall = len(tp_set) / len(grep_set) if grep_set else 1.0
    return AccuracyStats(
        precision=round(precision, 4),
        recall=round(recall, 4),
        tp=len(tp_set),
        fp=len(fp_set),
        fn=len(fn_set),
        fp_modules=sorted(fp_set),
        fn_modules=sorted(fn_set),
    )


def score_rdeps_accuracy(
    codemap_set: set[str], grep_floor: set[str], target_module: str, repo_root: Path
) -> AccuracyStats:
    """Score codemap rdeps with an AST-verified precision oracle and a grep recall floor.

    Precision is judged against an independent AST import-resolver: every module
    codemap returns is confirmed (or refuted) by parsing its source
    (:func:`verify_importer`), so codemap is never penalised for surfacing
    aliased, relative, or ``__init__`` re-export importers that a literal grep
    cannot see.  Recall is a *floor* — the fraction of the conservative
    boundary-anchored grep importer set that codemap also returns; because
    boundary-grep matches are genuine importers, codemap should contain them all.

    Args:
        codemap_set: Set of module names returned by scan-query rdeps.
        grep_floor: Boundary-anchored grep importer set used as the recall floor.
        target_module: Module whose importers are under test (for AST verification).
        repo_root: Repository root used to resolve candidate source files.

    Returns:
        :class:`AccuracyStats` where ``precision`` is AST-verified precision,
        ``recall`` is grep-floor coverage, ``fp``/``fp_modules`` are codemap
        members the AST oracle rejects (genuine false positives), ``fn``/
        ``fn_modules`` are grep-floor importers codemap missed, and ``tp`` is the
        codemap∩grep_floor overlap (the recall numerator).
    """
    verified = {m for m in codemap_set if verify_importer(m, target_module, repo_root)}
    ast_false_positives = codemap_set - verified
    floor_hits = codemap_set & grep_floor
    missed_floor = grep_floor - codemap_set
    precision = len(verified) / len(codemap_set) if codemap_set else 1.0
    recall = len(floor_hits) / len(grep_floor) if grep_floor else 1.0
    return AccuracyStats(
        precision=round(precision, 4),
        recall=round(recall, 4),
        tp=len(floor_hits),
        fp=len(ast_false_positives),
        fn=len(missed_floor),
        fp_modules=sorted(ast_false_positives),
        fn_modules=sorted(missed_floor),
    )


# Suites that check codemap against an INDEPENDENT oracle — these alone decide the primary verdict.
# "correctness" holds the fixture-based deterministic suites (D/B/R/K/U): each builds its own tmp
# repo with KNOWN ground truth (never scan-query-derived), so a pass is genuine correctness, not
# self-consistency — they join the verdict alongside calls/accuracy/latency/query-shape.
_PRIMARY_SUITES = frozenset({"calls", "accuracy", "latency", "query-shape", "correctness"})
# Suites validated against frozen scan-query-derived ground truth — determinism/regression only.
_SELF_CONSISTENCY_SUITES = frozenset({"symbol", "health", "xrefs"})


def _tally(results: list[ScenarioResult], suites: frozenset[str]) -> tuple[int, int]:
    """Return ``(passed, total)`` for the scenarios whose suite is in ``suites``.

    Args:
        results: All scenario results produced by the benchmark run.
        suites: Set of suite names to count.

    Returns:
        ``(passed, total)`` counts for that subset.

    Examples:
        >>> a = ScenarioResult("C1", "x", "calls", True, {}, {})
        >>> b = ScenarioResult("S2", "x", "symbol", False, {}, {})
        >>> _tally([a, b], _PRIMARY_SUITES)
        (1, 1)
    """
    subset = [r for r in results if r.suite in suites]
    return sum(1 for r in subset if r.passed), len(subset)


def compute_verdict(results: list[ScenarioResult]) -> str:
    """Compute the PRIMARY correctness verdict (independent-oracle suites only).

    Only the primary suites (calls, accuracy, latency, query-shape) count; the
    self-consistency suites (symbol, health, xrefs), whose ground truth is
    scan-query-derived, are excluded so circular passes cannot float the verdict up.

    Args:
        results: All :class:`ScenarioResult` objects produced by the benchmark run.

    Returns:
        ``"PASS"`` (all primary passed), ``"PARTIAL"`` (≥ half), or ``"FAIL"``
        (< half, or no primary scenarios).
    """
    passed, total = _tally(results, _PRIMARY_SUITES)
    if total == 0:
        return "FAIL"
    if passed == total:
        return "PASS"
    if passed / total >= 0.5:
        return "PARTIAL"
    return "FAIL"


def compute_self_consistency(results: list[ScenarioResult]) -> dict:
    """Summarize the self-consistency / determinism track (symbol, health, xrefs).

    Never contributes to :func:`compute_verdict`; reports whether scan-query
    output is stable against its own frozen ground truth.

    Args:
        results: All scenario results produced by the benchmark run.

    Returns:
        ``{"verdict", "passed", "total"}`` — verdict is ``"CONSISTENT"`` (all),
        ``"PARTIAL"`` (≥50%), ``"INCONSISTENT"`` (<50%), or ``"SKIPPED"`` (none ran).
    """
    passed, total = _tally(results, _SELF_CONSISTENCY_SUITES)
    if total == 0:
        verdict = "SKIPPED"
    elif passed == total:
        verdict = "CONSISTENT"
    elif passed / total >= 0.5:
        verdict = "PARTIAL"
    else:
        verdict = "INCONSISTENT"
    return {"verdict": verdict, "passed": passed, "total": total}
