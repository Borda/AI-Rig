"""Read-crop and fix scoring plus cross-run aggregation."""

import re
import statistics
from collections import defaultdict
from collections.abc import Iterator
from typing import Optional


# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.

from _bench_claude.agentic.models import BenchmarkRun, QualityScore
from _bench_claude.agentic.discovery import count_tokens
from _bench_claude.agentic.scope import run_cost_usd


def _iter_tool_result_texts(content: str | list) -> Iterator[str]:
    """Yield each text payload carried by a ``tool_result`` event's ``content`` field.

    Accepts both event shapes: a plain string, or a list of blocks where each block is either
    a string or a dict carrying ``text`` / ``content``. Any other shape yields nothing.

    Args:
        content: Raw ``content`` field from the ``tool_result`` event.

    Yields:
        Each text payload, in event order.

    Examples:
        >>> list(_iter_tool_result_texts("plain"))
        ['plain']
        >>> list(_iter_tool_result_texts([{"text": "a"}, "b", {"content": "c"}, {"other": 1}]))
        ['a', 'b', 'c', '']
        >>> list(_iter_tool_result_texts([]))
        []
    """
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    yield text
            elif isinstance(block, str):
                yield block


def _capture_tool_result_text(text: str, result: BenchmarkRun, *, is_rdeps: bool, is_semble: bool) -> None:
    """Bill one tool-result payload to *result* and file it into the matching corpus.

    Args:
        text: One tool-result text payload.
        result: The accumulating ``BenchmarkRun`` updated in place.
        is_rdeps: True when this result came from a ``codemap:query rdeps`` call.
        is_semble: True when this result came from a semble MCP tool call.

    Examples:
        >>> run = BenchmarkRun(arm="codemap", task_id="T1", task_type="t", model="haiku", success=True)
        >>> _capture_tool_result_text("modules", run, is_rdeps=True, is_semble=False)
        >>> run.codemap_results
        ['modules']
        >>> _capture_tool_result_text("<tool_use_error>boom", run, is_rdeps=True, is_semble=False)
        >>> run.codemap_results, run.tool_errors
        (['modules'], ['<tool_use_error>boom'])
    """
    result.tool_result_tokens += count_tokens(text)
    # Skip error responses and skill executor status placeholders from corpus
    if "<tool_use_error>" in text or text.startswith("Launching skill:"):
        if "<tool_use_error>" in text:
            result.tool_errors.append(text[:2000])
        return
    if is_rdeps:
        result.codemap_results.append(text)
        result.skill_result_text += ("\n" if result.skill_result_text else "") + text
    if is_semble:
        result.semble_results.append(text)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _normalize_match_text(text: str) -> str:
    """Normalise text for whitespace-tolerant keyword matching.

    Drops whitespace flanking operators and punctuation so brittle literals like ``"< 1"`` match
    ``"<1"`` (and ``"<  1"``), while preserving single spaces between word tokens so distinct
    identifiers are never merged into false matches. Case is folded to lower.

    Args:
        text: Raw diff / answer text, or a single expected keyword.

    Returns:
        The normalised, lower-cased string ready for substring comparison.

    Examples:
        >>> _normalize_match_text("if patience < 1:")
        'if patience<1:'
        >>> _normalize_match_text("< 1") in _normalize_match_text("guard patience<1 here")
        True
        >>> _normalize_match_text("raise Error")  # word-word spaces are preserved
        'raise error'
    """
    collapsed = re.sub(r"\s*([^\w\s])\s*", r"\1", text)
    return re.sub(r"\s+", " ", collapsed).strip().lower()


def score_read_crop(output_text: str, expected_keywords: list[str]) -> QualityScore:
    """Keyword-recall scorer for read_crop tasks (no rdeps ground truth).

    ``erec``/``rrec`` are reused as keyword recall so the metric flows through the existing
    report columns; the headline efficiency signal is ``tool_result_tokens`` (read cost),
    reported separately. A keyword is matched case-insensitively as a substring of the answer.

    Args:
        output_text: Full agent answer text.
        expected_keywords: Ground-truth identifiers the correct contract must mention.

    Returns:
        QualityScore with recall in ``erec``/``rrec``; ``scored=False`` when no keywords given.

    Examples:
        >>> s = score_read_crop("uses prog_bar and on_step", ["prog_bar", "on_step", "logger"])
        >>> round(s.erec, 2), s.erec_tp, s.erec_fn
        (0.67, 2, 1)
    """
    if not expected_keywords:
        return QualityScore(scored=False)
    haystack = _normalize_match_text(output_text)
    hits = sum(1 for k in expected_keywords if _normalize_match_text(k) in haystack)
    n = len(expected_keywords)
    rec = hits / n
    return QualityScore(scored=True, erec=rec, erec_tp=hits, erec_fn=n - hits, rrec=rec, rrec_tp=hits, rrec_fn=n - hits)


def score_fix(
    diff_text: str,
    expected_patch_keywords: list[str],
    expected_files: list[str],
    test_passed: Optional[bool] = None,
) -> QualityScore:
    """Keyword-recall scorer for fix_single / fix_multicaller tasks.

    Checks the unified diff of agent edits for expected change markers. ``erec`` measures
    keyword recall in added lines; ``rrec`` measures file recall (were the right files changed).
    Keyword matching is whitespace-tolerant (see ``_normalize_match_text``) so operator literals
    like ``"< 1"`` are not defeated by an agent writing ``"<1"``.

    ``test_passed`` carries a stronger, opt-in correctness signal recorded *alongside* erec (it
    never replaces the recall column): when the task declares a targeted test, the caller runs it
    on the post-edit sandbox and passes the outcome here. It stays ``None`` for tasks with no
    declared test, so tasks without one are unaffected.

    Args:
        diff_text: Output of ``diff -ru original copy`` after agent run.
        expected_patch_keywords: Strings expected in diff added lines (``+`` prefix).
        expected_files: Relative file-path fragments expected in ``+++ b/...`` headers.
        test_passed: Outcome of the task's declared targeted test (True/False), or ``None`` when
            the task declares no test or the test could not be launched.

    Returns:
        QualityScore with keyword recall in ``erec``, file-change recall in ``rrec``, and the
        opt-in ``test_passed`` correctness signal. Returns ``scored=False`` when no keywords given.

    Examples:
        >>> d = "+        if patience < 1:\\n+            raise MisconfigurationException('patience')"
        >>> s = score_fix(d, ["patience < 1", "MisconfigurationException"], ["early_stopping.py"])
        >>> s.erec
        1.0
        >>> s.rrec  # no +++ header in that diff snippet — file path absent
        0.0
    """
    if not expected_patch_keywords:
        return QualityScore(scored=False)
    added_lines = "\n".join(
        line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    haystack = _normalize_match_text(added_lines)
    hits = sum(1 for k in expected_patch_keywords if _normalize_match_text(k) in haystack)
    erec = round(hits / len(expected_patch_keywords), 3)
    # diff -ru produces "+++ /full/path\t<timestamp>"; git diff produces "+++ b/path"
    changed_files = set(re.findall(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$", diff_text, re.MULTILINE))
    rrec = 0.0
    if expected_files:
        file_hits = sum(1 for f in expected_files if any(f in cf for cf in changed_files))
        rrec = round(file_hits / len(expected_files), 3)
    return QualityScore(
        scored=True,
        erec=erec,
        erec_tp=hits,
        erec_fn=len(expected_patch_keywords) - hits,
        rrec=rrec,
        test_passed=test_passed,
    )


def _median_metrics(rlist: list[BenchmarkRun]) -> dict[str, float | None]:
    """Return one cell's success-only medians alongside its failure count and all-runs spend.

    Medians taken over successful runs only let a failure-heavy arm read as the cheap,
    fast one: a cell where two of three runs died at the wall-clock limit reported the
    survivor's cost and elapsed time, and the two that burned a full paid timeout each
    left no trace in the numbers. ``n_runs``/``n_failures`` and the ``*_all`` aggregates
    (which include failed runs) therefore accompany every cell, and a cell whose runs all
    failed now reports that spend instead of collapsing to an empty dict. Quality medians
    stay success-only — a failed run has no answer to score.
    """
    if not rlist:
        return {}
    ok = [r for r in rlist if r.success]
    spend: dict[str, float | None] = {
        "n_runs": len(rlist),
        "n_failures": len(rlist) - len(ok),
        "success_rate": len(ok) / len(rlist),
        "tool_calls_all": statistics.median([r.tools.total for r in rlist]),
        "input_tokens_all": statistics.median([r.input_tokens for r in rlist]),
        "cost_usd_all": statistics.median([run_cost_usd(r) for r in rlist]),
        "elapsed_s_all": statistics.median([r.elapsed_s for r in rlist]),
    }
    if not ok:
        return spend
    chunk_vals = [r.quality.chunk_hit_rate for r in ok if r.quality.chunk_hit_rate is not None]
    return {
        **spend,
        "tool_calls": statistics.median([r.tools.total for r in ok]),
        "input_tokens": statistics.median([r.input_tokens for r in ok]),
        "cost_usd": statistics.median([run_cost_usd(r) for r in ok]),
        "tool_result_tokens": statistics.median([r.tool_result_tokens for r in ok]),
        "tool_elapsed_s": statistics.median([r.tool_elapsed_s for r in ok]),
        "elapsed_s": statistics.median([r.elapsed_s for r in ok]),
        "rrec": statistics.median([r.quality.rrec for r in ok]),
        "erec": statistics.median([r.quality.erec for r in ok]),
        "delta": statistics.median([r.quality.delta for r in ok]),
        # None when no run in the cell carried a semble corpus (plain / codemap arms).
        "chunk_hit_rate": statistics.median(chunk_vals) if chunk_vals else None,
    }


def aggregate(
    results: list[BenchmarkRun],
    task_ids: list[str],
    model_short: str | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Return {task_id: {arm: {metric: value}}} optionally filtered to one model tier."""
    filtered = [r for r in results if model_short is None or r.model == model_short]
    by_task_arm: dict[str, dict[str, list[BenchmarkRun]]] = defaultdict(lambda: defaultdict(list))
    for r in filtered:
        by_task_arm[r.task_id][r.arm].append(r)

    out: dict[str, dict[str, dict[str, float]]] = {}
    for tid in task_ids:
        out[tid] = {}
        for arm, rlist in by_task_arm.get(tid, {}).items():
            out[tid][arm] = _median_metrics(rlist)
    return out
