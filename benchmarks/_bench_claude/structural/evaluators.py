"""Per-task-series answer evaluators and the registry that dispatches to them."""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    EvaluationResult,
    EvaluatorRegistry,
)

from _bench_claude.structural.config import _REPO_NAMESPACE
from _bench_claude.structural.models import BenchQuality, _BenchEvaluationResult

# ---------------------------------------------------------------------------
# Quality evaluators — extract key metric from model output text
# ---------------------------------------------------------------------------

_EVAL_VER_NAME_RECALL = "v5"  # _evaluate_develop_br (v5: drop .md file-dump; tighten precision for fuzzy tiers)
_EVAL_VER_SYMBOL = "v2"  # _evaluate_symbol — accepts conventional source-location ranges
_EVAL_VER_REVIEW = "v8"  # _evaluate_rv — adds trailing-qualifier and numbered sub-answer counts
_EVAL_VER_OSS = "v7"  # _evaluate_oss — explicit label-first count grammar for required AST components
_EVAL_VER_DEBUG = "v2"  # _evaluate_debug — v2: structured-block + stem-blocklist matching
_EVAL_VER_FEATURE = "v4"  # _evaluate_feature — accepts one terminal sentence period after the exact entry point
_EVAL_VER_REAL_ISSUE = "v2"  # _evaluate_real_issue — v2: path-with-parent matching in answer block

# Substring-inflation guard. Common single-token file/symbol stems that saturate any
# discussion of the target repo (a bare mention of `trainer` in prose is a free hit). These must
# appear as a QUALIFIED reference — pathed (`.../trainer`), dotted (`x.trainer`), or with a `.py`
# suffix — to count; a bare word never does. Applied symmetrically to both arms (scoring is arm-agnostic).
_STEM_BLOCKLIST: frozenset[str] = frozenset({"trainer", "utils", "core", "types", "base"})

# Section headings that mark the start of a structured answer block. Matching is restricted to text
# AT OR AFTER the earliest such heading so exploration prose before the final answer cannot score.
_ANSWER_LABELS_FILES: tuple[str, ...] = ("files", "root cause", "root-cause", "answer")
_ANSWER_LABELS_SYMBOLS: tuple[str, ...] = ("symbols", "undocumented", "uncovered", "answer")
# Conclusion-only headings for numeric/count answers. Deliberately generic (no early working-section
# nouns like "importers"/"callers") so _answer_region anchors on the FINAL answer, not an exploratory
# heading — a stray count in exploration ("0 symbols of its own") must never outrank the conclusion.
_ANSWER_LABELS_COUNT: tuple[str, ...] = ("answer", "conclusion", "summary", "result", "total")


def _answer_region(output_text: str, labels: tuple[str, ...]) -> tuple[str, bool]:
    """Return the structured answer block of *output_text*, or the full text when none is present.

    Locates the earliest line that is a bare answer heading — a markdown header (``## Files``) or a
    labelled line (``Files:`` / ``**Files**``) whose only content is one of *labels* — and returns
    everything from there to the end. When no such heading exists the full text is returned with a
    ``degraded`` flag so callers can record that block-scoped matching was unavailable.

    Args:
        output_text: The agent's full response text.
        labels: Candidate heading labels for this evaluator family (case-insensitive).

    Returns:
        ``(region, degraded)`` — ``region`` is the answer block (or full text); ``degraded`` is True
        only when no heading matched and the full text was used as a fallback.

    Examples:
        >>> _answer_region("exploring trainer\\n## Files\\npkg/mod.py\\n", ("files",))
        ('## Files\\npkg/mod.py\\n', False)
        >>> region, degraded = _answer_region("just prose about trainer", ("files",))
        >>> degraded
        True
    """
    earliest: int | None = None
    for label in labels:
        pat = rf"(?im)^[ \t]*(?:#{{1,6}}[ \t]*)?\*{{0,2}}[ \t]*{re.escape(label)}[ \t]*:?[ \t]*\*{{0,2}}[ \t]*$"
        m = re.search(pat, output_text)
        if m and (earliest is None or m.start() < earliest):
            earliest = m.start()
    if earliest is None:
        return output_text, True
    return output_text[earliest:], False


def _stem_matches(stem: str, region: str) -> bool:
    """Return True when *stem* is present in *region* as a countable reference.

    Blocklisted ultra-common stems (:data:`_STEM_BLOCKLIST`) count only as a qualified reference —
    preceded by ``/`` or ``.`` (a path or dotted name) or carrying a ``.py`` suffix — never as a bare
    word. All other stems count on a plain word-boundary match.

    Args:
        stem: File stem or symbol short name to look for.
        region: Text to search (typically the structured answer block).

    Returns:
        True when a countable reference to *stem* is found.

    Examples:
        >>> _stem_matches("trainer", "the trainer orchestrates the loop")
        False
        >>> _stem_matches("trainer", "see trainer.py for details")
        True
        >>> _stem_matches("fit_loop", "the fit_loop advances")
        True
    """
    esc = re.escape(stem)
    if stem in _STEM_BLOCKLIST:
        return bool(
            re.search(r"[/.]" + esc + r"\b", region, re.IGNORECASE)
            or re.search(r"\b" + esc + r"\.py\b", region, re.IGNORECASE)
        )
    return bool(re.search(r"\b" + esc + r"\b", region, re.IGNORECASE))


def _ri_file_matches(file_path: str, region: str) -> bool:
    """Return True when *file_path* is referenced in *region* by a pathed form.

    A real_issue file counts only via its full repository-relative path or a path-with-parent form
    (``connectors/logger_connector``) — never a bare basename stem, which for common names (``trainer``)
    is a near-free hit. Leading ``src/`` layout prefixes and the ``.py`` suffix are treated as optional.

    Args:
        file_path: Repository-relative ground-truth path (e.g. ``src/pkg/connectors/logger_connector.py``).
        region: Text to search (typically the structured answer block).

    Returns:
        True when any pathed candidate for *file_path* appears in *region*.

    Examples:
        >>> _ri_file_matches("src/pkg/connectors/logger_connector.py", "edit connectors/logger_connector")
        True
        >>> _ri_file_matches("src/pkg/trainer.py", "the trainer handles this")
        False
    """
    parts = file_path.split("/")
    stem = parts[-1].removesuffix(".py")
    candidates: set[str] = {file_path}
    if file_path.endswith(".py"):
        candidates.add(file_path[:-3])
    if file_path.startswith("src/"):
        candidates.add(file_path[4:])
        if file_path.endswith(".py"):
            candidates.add(file_path[4:-3])
    if len(parts) >= 2:
        candidates.add(f"{parts[-2]}/{parts[-1]}")
        candidates.add(f"{parts[-2]}/{stem}")
    return any(re.search(r"(?<![\w/.-])" + re.escape(cand) + r"(?![\w/.-])", region) for cand in candidates)


def _extract_int(text: str, patterns: list[str]) -> int | None:
    """Extract the first integer matching any of the given regex patterns.

    Args:
        text: Model output text to search.
        patterns: List of regex patterns; each must have one capture group for the integer.

    Returns:
        Extracted integer, or None when no pattern matched.

    Examples:
        >>> _extract_int("found 42 callers", [r"(\\d+) caller"])
        42
        >>> _extract_int("nothing here", [r"(\\d+) caller"])
    """
    text = re.sub(r"[*`]+", " ", text)  # strip bold (*) and inline-code (`) markers before matching
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except (IndexError, ValueError):
                continue
    return None


def _numbered_subanswer_count(text: str) -> int | None:
    """Extract a bare integer answering an enumerated sub-question.

    Review tasks pose numbered sub-questions, and a compliant reply may answer the first one with the
    number alone ("1. **11**"). No noun follows it, so every noun-anchored count pattern misses a
    correct answer. This reads the integer that opens an enumerated line, which is the shape the
    sub-question numbering itself invites. It runs only after the noun-anchored patterns, so a
    phrased answer still wins.

    Args:
        text: Model output text to search.

    Returns:
        The first such integer, or None when no enumerated line opens with one.

    Examples:
        >>> _numbered_subanswer_count("1. **11**\\n2. LayerSummary\\n")
        11
        >>> _numbered_subanswer_count("1. eleven uncovered symbols")
    """
    stripped = re.sub(r"[*`]+", " ", text)
    match = re.search(r"^\s{0,3}\d+[.)]\s+(\d+)\b", stripped, re.MULTILINE)
    return int(match.group(1)) if match else None


def _extract_count_answer_first(
    output_text: str, patterns: list[str], labels: tuple[str, ...] = _ANSWER_LABELS_COUNT
) -> int | None:
    """Extract an integer count, preferring the structured answer/conclusion region.

    :func:`_extract_int` returns the first pattern that matches *anywhere*, so on verbose codemap
    output a stray number in exploration ("0 symbols of its own") can outrank the real answer
    ("65 importers"). This scopes extraction to the answer region (:func:`_answer_region`) first and
    only falls back to the full text when that region yields no count — so it never reduces
    extraction success versus a full-text scan, it only prefers the conclusion when one carries a
    count. When no answer heading is present the region *is* the full text (``degraded``), so the
    single region scan already covers everything and the fallback is skipped.

    Args:
        output_text: The agent's full response text.
        patterns: Ordered regex patterns passed through to :func:`_extract_int`.
        labels: Answer-heading labels for :func:`_answer_region` (conclusion markers by default).

    Returns:
        The extracted integer, or ``None`` when neither the answer region nor the full text matches.

    Examples:
        >>> pats = [r"(\\d+)\\s+(?:symbol|method)", r"(\\d+)\\s+importers?"]
        >>> _extract_count_answer_first("has 0 symbols\\n## Answer\\n65 importers\\n", pats)
        65
        >>> _extract_count_answer_first("no answer heading, just 7 methods here", pats)
        7
        >>> _extract_count_answer_first("nothing numeric to find", pats)
    """
    region, degraded = _answer_region(output_text, labels)
    got = _extract_int(region, patterns)
    if got is None and not degraded:
        got = _extract_int(output_text, patterns)
    return got


def _extract_names(text: str) -> list[str]:
    """Extract dotted module names matching the repo namespace from model output.

    Args:
        text: Model output text.

    Returns:
        Deduplicated sorted list of dotted names matching the repo namespace.

    Examples:
        >>> _extract_names("see lightning.pytorch.trainer.trainer and lightning.pytorch.loops.loop")
        ['lightning.pytorch.loops.loop', 'lightning.pytorch.trainer.trainer']
    """
    ns_alt = "|".join(re.escape(n) for n in _REPO_NAMESPACE)
    found = re.findall(rf"\b(?:{ns_alt})(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+", text)
    return sorted(set(found))


def _int_close(got: int | None, expected: int, tolerance: float = 0.10) -> bool:
    """Return True when got is within tolerance of expected.

    Args:
        got: Extracted integer (None → always False).
        expected: Ground-truth integer.
        tolerance: Fractional tolerance (0.10 = ±10%).

    Returns:
        True when ``abs(got - expected) / max(expected, 1) <= tolerance``.

    Examples:
        >>> _int_close(42, 40, tolerance=0.10)
        True
        >>> _int_close(42, 30, tolerance=0.10)
        False
        >>> _int_close(None, 40)
        False
    """
    if got is None or not isinstance(expected, (int, float)):
        return False
    return abs(got - expected) / max(expected, 1) <= tolerance


def _count_tol_detail(expected: Any, got: Any, **extra: Any) -> dict[str, Any]:
    """Build a count-tolerance scoring_detail dict (threshold fixed at 10%).

    Args:
        expected: Ground-truth count.
        got: Extracted count.
        **extra: Optional additional keys merged into the dict.

    Returns:
        Dict with metric_expected, metric_got, threshold, method, plus any extras.

    Examples:
        >>> _count_tol_detail(10, 9)
        {'metric_expected': 10, 'metric_got': 9, 'threshold': 0.1, 'method': 'count_tolerance'}
        >>> _count_tol_detail(10, 9, check="coupled")
        {'metric_expected': 10, 'metric_got': 9, 'threshold': 0.1, 'method': 'count_tolerance', 'check': 'coupled'}
    """
    return {"metric_expected": expected, "metric_got": got, "threshold": 0.10, "method": "count_tolerance", **extra}


def _score_required_components(
    *,
    count_components: list[tuple[str, Any, int | None]],
    symbol_components: list[tuple[str, list[str], str, bool]],
    evaluator_used: str,
    evaluator_version: str,
    oracle_views: Mapping[str, Any] | None = None,
    check: str | None = None,
) -> BenchQuality:
    """Score every required count or symbol answer and average their fitness.

    A required count contributes bounded relative-error fitness while its documented 10% tolerance remains the binary
    correctness gate. A required symbol set contributes its recall. A task is correct only when every component meets
    its own gate; a missing component is an extraction failure.
    """
    components: dict[str, dict[str, Any]] = {}
    primary_expected: Any = None
    primary_got: Any = None

    for name, expected, got in count_components:
        correct = _int_close(got, expected, tolerance=0.10)
        relative_error: float | None = None
        fitness = 0.0
        if got is not None and isinstance(expected, (int, float)) and not isinstance(expected, bool):
            relative_error = abs(got - expected) / max(abs(expected), 1)
            fitness = max(0.0, 1.0 - relative_error)
        components[name] = {
            "kind": "count",
            "expected": expected,
            "got": got,
            "threshold": 0.10,
            "fitness": fitness,
            "correct": correct,
            "extraction_failed": got is None,
            "relative_error": relative_error,
            "method": "bounded_relative_error",
        }
        if primary_expected is None:
            primary_expected, primary_got = expected, got

    for name, expected_symbols, region, degraded in symbol_components:
        found = sum(1 for symbol in expected_symbols if _stem_matches(symbol.split(".")[-1], region))
        recall = found / max(len(expected_symbols), 1)
        correct = recall >= 0.70
        components[name] = {
            "kind": "symbols",
            "expected": len(expected_symbols),
            "got": found,
            "threshold": 0.70,
            "fitness": recall,
            "correct": correct,
            "extraction_failed": found == 0,
            "extraction_degraded": degraded,
        }
        if primary_expected is None:
            primary_expected, primary_got = len(expected_symbols), found

    if not components:
        return BenchQuality(scored=False)

    details: dict[str, Any] = {
        "metric_expected": primary_expected,
        "metric_got": primary_got,
        "method": "required_component_mean",
        "components": components,
        "fitness_aggregation": "unweighted mean of every required component",
    }
    if check is not None:
        details["check"] = check
    if oracle_views is not None:
        details["oracle_views"] = dict(oracle_views)

    component_values = [float(component["fitness"]) for component in components.values()]
    return BenchQuality(
        scored=True,
        correct=all(bool(component["correct"]) for component in components.values()),
        metric_expected=primary_expected,
        metric_got=primary_got,
        recall=round(sum(component_values) / len(component_values), 3),
        extraction_failed=any(bool(component["extraction_failed"]) for component in components.values()),
        extraction_degraded=any(bool(component.get("extraction_degraded")) for component in components.values()),
        evaluator_used=evaluator_used,
        evaluator_version=evaluator_version,
        extracted_metric={name: component["got"] for name, component in components.items()},
        scoring_detail=details,
    )


def _evaluate_symbol(task: dict, output_text: str) -> BenchQuality:
    """Evaluate symbol_extraction task: check whether start_line matches ground truth.

    Args:
        task: Task dict from tasks-bench.json.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with correct=True when start_line extracted and within ±5 lines.
    """
    gt = task["ground_truth"]
    expected_start = gt["start_line"]
    qname = gt["qualified_name"]

    # Strip markdown bold (*) and inline-code (`) markers — but NOT underscores (would destroy the
    # start_line key). Agents routinely format the value as `start_line: ``213`` ` (backticks), which
    # left a backtick between the colon and the first digit and defeated every pattern below → !parse.
    cleaned = re.sub(r"[*`]+", "", output_text)

    got_start: int | None = None

    # 1. "start_line: N" or "start line: N" — most specific; check before range patterns
    m = re.search(r"\bstart[_ ]line\s*[:\s]+(\d+)", cleaned, re.IGNORECASE)
    if m:
        got_start = int(m.group(1))

    # 1b. "Start: line N" — bold-stripped form of "**Start**: line N"
    if got_start is None:
        m = re.search(r"\bstart\s*:\s*line\s+(\d+)", cleaned, re.IGNORECASE)
        if m:
            got_start = int(m.group(1))

    # 2. "starts at line N"
    if got_start is None:
        m = re.search(r"\bstarts?\s+at\s+line\s+(\d+)", cleaned, re.IGNORECASE)
        if m:
            got_start = int(m.group(1))

    # 3. Compact ``path.py:N-M`` source locations are common final answers and carry an
    # unambiguous source-file anchor; accept them before the deliberately broader prose range.
    if got_start is None:
        m = re.search(r"(?:^|\s)[\w./-]+\.py:(\d+)\s*[-–]\s*\d+\b", cleaned)
        if m:
            got_start = int(m.group(1))

    # 4. Explicit range "Lines N-M" → first number is start (fallback; can match import ranges)
    if got_start is None:
        m = re.search(r"\blines?\W+(\d+)\s*[-–]\s*\d+", cleaned, re.IGNORECASE)
        if m:
            got_start = int(m.group(1))

    # 5. "line N" near the short symbol name (last component of qualified name)
    if got_start is None:
        short = re.escape(qname.split(".")[-1])
        m = re.search(r"line\s+(\d+).*?" + short, cleaned, re.IGNORECASE | re.DOTALL)
        if m:
            got_start = int(m.group(1))

    correct = got_start is not None and abs(got_start - expected_start) <= 5
    # metric_got/metric_expected are raw line numbers (diagnostics in scoring_detail);
    # the recall column derives from `correct` via _effective_recall, not this ratio.
    return BenchQuality(
        scored=True,
        correct=correct,
        metric_expected=expected_start,
        metric_got=got_start,
        extraction_failed=got_start is None,
        evaluator_used="_evaluate_symbol",
        evaluator_version=_EVAL_VER_SYMBOL,
        extracted_metric=got_start,
        scoring_detail={
            "metric_expected": expected_start,
            "metric_got": got_start,
            "threshold": 5,
            "method": "line_tolerance",
        },
    )


def _evaluate_rv(task: dict, output_text: str) -> BenchQuality:
    """Evaluate every required review subanswer with continuous component fitness."""
    sub_questions = task.get("sub_questions", [])
    if not sub_questions:
        return BenchQuality(scored=False)
    if not isinstance(sub_questions, list):
        raise ValueError("review sub-questions must be a list")

    _count_patterns = [
        r"(\d+)\s+undocumented",
        r"(\d+)\s+(?:unique\s+)?(?:public\s+)?symbols?(?:\s+are)?\s+uncovered",
        r"(\d+)\s+uncovered",
        r"(\d+)\s+reverse\s+dependenc(?:y|ies)",
        r"(\d+)\s+(?:distinct|unique)\s+production\s+functions?",
        r"(\d+)\s+(?:distinct|unique)\s+production\s+callers?",
        # "24 production functions uniquely call X" puts the qualifier after the noun, so the
        # two patterns above cannot match a correct answer that reads naturally.
        r"(\d+)\s+production\s+(?:function|caller)s?",
        r"(\d+)\s+(?:function|symbol|method|class)",
        r"(\d+)\s+(?:production\s+)?call\s*site",
        r"(\d+)\s+(?:production\s+)?calls?\b",
        r"(\d+)\s+(?:total\s+)?(?:unique\s+)?importers?",  # "61 total importers", "56 importers"
        r"(\d+)\s+(?:total\s+)?modules?\s+including\s+\d+\s+test\s+modules?",
        r"(\d+)\s+(?:unique\s+)?modules?\s+(?:directly\s+)?(?:import|depend)",  # "N modules [directly] import"
        r"(\d+)\s+total\s+importer",
        r"total[:\s]+(\d+)",
        r"count[:\s]+(\d+)",
        r"found\s+(\d+)",
    ]

    validated_questions: list[tuple[str, str, Mapping[str, Any]]] = []
    for index, sub_question in enumerate(sub_questions, start=1):
        if not isinstance(sub_question, Mapping):
            raise ValueError(f"review sub-question {index} must be an object")
        question_id = sub_question.get("id")
        match = sub_question.get("match")
        ground_truth = sub_question.get("ground_truth")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError(f"review sub-question {index} requires a non-empty id")
        if not isinstance(ground_truth, Mapping):
            raise ValueError(f"review sub-question {question_id!r} requires ground_truth")
        if match == "integer_extract":
            expected_count = ground_truth.get("count")
            if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
                raise ValueError(f"review sub-question {question_id!r} requires a non-negative integer count")
        elif match == "symbol_name_set":
            expected_symbols = ground_truth.get("symbols")
            if not isinstance(expected_symbols, list) or not all(
                isinstance(symbol, str) for symbol in expected_symbols
            ):
                raise ValueError(f"review sub-question {question_id!r} requires a symbol string list")
        else:
            raise ValueError(f"review sub-question {question_id!r} has unsupported match {match!r}")
        validated_questions.append((question_id, match, ground_truth))

    count_question_count = sum(match == "integer_extract" for _, match, _ in validated_questions)
    if count_question_count > 1:
        raise ValueError("review task has multiple required count components without answer scoping")

    count_components: list[tuple[str, Any, int | None]] = []
    symbol_components: list[tuple[str, list[str], str, bool]] = []
    symbol_region, symbol_degraded = _answer_region(output_text, _ANSWER_LABELS_SYMBOLS)
    for question_id, match, ground_truth in validated_questions:
        if match == "integer_extract":
            got_count = _extract_count_answer_first(output_text, _count_patterns)
            if got_count is None:
                got_count = _numbered_subanswer_count(output_text)
            if got_count is None:
                list_items = re.findall(r"^\s*[-*•]\s+\S", output_text, re.MULTILINE)
                if list_items:
                    got_count = len(list_items)
            count_components.append((f"{question_id}.count", ground_truth["count"], got_count))
        else:
            expected_symbols = ground_truth["symbols"]
            symbol_components.append((f"{question_id}.symbols", expected_symbols, symbol_region, symbol_degraded))

    oracle_views = task.get("ground_truth", {}).get("oracle_views")
    return _score_required_components(
        count_components=count_components,
        symbol_components=symbol_components,
        evaluator_used="_evaluate_rv",
        evaluator_version=_EVAL_VER_REVIEW,
        oracle_views=oracle_views if isinstance(oracle_views, Mapping) else None,
    )


def _extract_coupled_ranking(output_text: str, metric_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Extract numbered coupled-ranking rows from bullet or Markdown-table answers."""
    rows_by_rank: dict[int, dict[str, Any]] = {}
    for line in output_text.splitlines():
        rank_match = re.match(r"^\s*\|?\s*(\d+)\s*(?:[.)]|\|)\s*(.*)$", line)
        if rank_match is None:
            continue
        rank = int(rank_match.group(1))
        body = rank_match.group(2)
        name_match = re.search(r"`([^`]+)`|\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b", body)
        if name_match is None:
            continue
        row: dict[str, Any] = {"name": name_match.group(1) or name_match.group(2)}
        for metric_field in metric_fields:
            count_match = re.search(rf"\b{re.escape(metric_field)}\s*[:=]\s*(\d+)", body)
            if count_match is not None:
                row[metric_field] = int(count_match.group(1))
        if len(metric_fields) == 1 and metric_fields[0] not in row:
            count_values = re.findall(r"\b\d+\b", body)
            if not count_values:
                continue
            row[metric_fields[0]] = int(count_values[-1])
        if any(metric_field not in row for metric_field in metric_fields):
            continue
        rows_by_rank.setdefault(rank, row)
    return [rows_by_rank[rank] for rank in sorted(rows_by_rank)]


def _evaluate_oss(task: dict, output_text: str) -> BenchQuality:
    """Evaluate code_quality task: check primary count from ground_truth.

    Args:
        task: Task dict from tasks-bench.json.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with correct=True when primary count extracted within 10%.
    """
    gt = task["ground_truth"]
    check = gt.get("check", "")

    if check == "coupled":
        expected_ranking = gt.get("top_modules")
        if isinstance(expected_ranking, list) and expected_ranking:
            metric_fields = ("dep_count",)
            if not all(isinstance(row, Mapping) and "name" in row and "dep_count" in row for row in expected_ranking):
                raise ValueError("coupled top_modules requires name and dep_count fields")
            expected = [
                {"name": row.get("name"), **{field: row.get(field) for field in metric_fields}}
                for row in expected_ranking
                if isinstance(row, Mapping)
            ]
            got = _extract_coupled_ranking(output_text, metric_fields)
            correct = got == expected
            return BenchQuality(
                scored=True,
                correct=correct,
                metric_expected=expected,
                metric_got=got,
                extraction_failed=not got,
                evaluator_used="_evaluate_oss",
                evaluator_version=_EVAL_VER_OSS,
                extracted_metric=got,
                scoring_detail={
                    "metric_expected": expected,
                    "metric_got": got,
                    "threshold": 0,
                    "method": "ordered_coupled_ranking",
                    "metric_fields": metric_fields,
                },
            )
        expected = gt.get("top_dep_count", 0)
        # Anchor to number immediately before "dep" — avoids forward-scan into summary totals.
        # dep_count field name first (structured output), then "N dep*" literal.
        # Answer-region-scoped (count-fragility fix): prefer the count in the conclusion over a
        # stray dep count in exploratory prose; falls back to full text when the region has none.
        got = _extract_count_answer_first(
            output_text,
            [
                r"dep_count[:\s=]+(\d+)",
                r"(\d+)\s+dep(?:endenc|t)",
                r"(\d+)\s+total\s+dep(?:endenc|t)",
                r"\|\s*1\s*\|[^\n]*\|\s*(\d+)\s*\|",  # rank-1 row in dep_count markdown table
            ],
        )
        correct = _int_close(got, expected, tolerance=0.10)
        return BenchQuality(
            scored=True,
            correct=correct,
            metric_expected=expected,
            metric_got=got,
            extraction_failed=got is None,
            evaluator_used="_evaluate_oss",
            evaluator_version=_EVAL_VER_OSS,
            extracted_metric=got,
            scoring_detail=_count_tol_detail(expected, got, check=check),
        )

    if check == "xrefs_broken":
        expected = gt.get("broken_count", 0)
        broken_targets = gt.get("broken_targets", [])
        if broken_targets:
            # Count how many known broken target symbols appear in the output — more reliable
            # than parsing "N broken" prose which can grab unrelated sentence counts.
            short_names = [t["target"].split("::")[-1] for t in broken_targets]
            got = sum(1 for name in short_names if name in output_text)
            if got == 0:
                # Fallback: prose extraction when model didn't name symbols
                got_prose = _extract_count_answer_first(output_text, [r"(\d+)\s+broken", r"broken[:\s]+(\d+)"])
                got = got_prose
        else:
            got = _extract_count_answer_first(output_text, [r"(\d+)\s+broken", r"broken[:\s]+(\d+)", r"(\d+)\s+xref"])
        correct = got == expected
        return BenchQuality(
            scored=True,
            correct=correct,
            metric_expected=expected,
            metric_got=got,
            extraction_failed=got is None,
            evaluator_used="_evaluate_oss",
            evaluator_version=_EVAL_VER_OSS,
            extracted_metric=got,
            scoring_detail={
                "metric_expected": expected,
                "metric_got": got,
                "threshold": 0,
                "method": "exact_match",
                "check": check,
            },
        )

    if check == "combined_health":
        undocumented_expected = gt.get("undocumented_count", 0)
        uncovered_expected = gt.get("uncovered_count", 0)
        undocumented_got = _extract_count_answer_first(
            output_text,
            [r"undocumented[\s_-]+count\s*[:=]\s*(\d+)"],
        )
        uncovered_got = _extract_count_answer_first(
            output_text,
            [r"uncovered[\s_-]+count\s*[:=]\s*(\d+)"],
        )
        return _score_required_components(
            count_components=[
                ("undocumented_count", undocumented_expected, undocumented_got),
                ("uncovered_count", uncovered_expected, uncovered_got),
            ],
            symbol_components=[],
            evaluator_used="_evaluate_oss",
            evaluator_version=_EVAL_VER_OSS,
            check=check,
        )

    if check == "undocumented":
        oracle_views = gt.get("oracle_views")
        independent_ast = oracle_views.get("independent_ast", {}) if isinstance(oracle_views, Mapping) else {}
        expected = independent_ast.get("count", gt.get("undocumented_count", 0))
        required_components = gt.get("required_answer_components", [])
        count_patterns = [
            r"independent[\s_-]+ast[\s_-]+count\s*[:=]\s*(\d+)",
        ]
        if "independent_ast_count" in required_components:
            count_patterns.extend(
                [
                    r"independent\s+ast(?:\s+view)?\s*[:=]\s*(\d+)\s+(?:unique\s+)?(?:qualified\s+)?(?:names|symbols)\b",
                ]
            )
        if "independent_ast_count" not in required_components:
            count_patterns.extend(
                [
                    r"independent\s+AST(?:\s+view)?\s*[:—–-]?\s*(\d+)",
                    r"(\d+)\s+unique\s+(?:undocumented\s+)?(?:qualified\s+)?(?:names|symbols)",
                    r"(\d+)\s+undocumented",
                    r"undocumented[:\s]+(\d+)",
                    r"undocumented[^:\n]*[:\s—–]+(\d+)",
                    r"(\d+)\s+(?:public\s+)?symbols?\s+lack",
                    r"without\s+docstring.*?(\d+)",
                ]
            )
        got = _extract_count_answer_first(
            output_text,
            count_patterns,
        )
        expected_symbols = independent_ast.get("symbols", gt.get("undocumented_symbols"))
        symbol_components: list[tuple[str, list[str], str, bool]] = []
        if (
            isinstance(required_components, list)
            and "independent_ast_symbols" in required_components
            and isinstance(expected_symbols, list)
            and all(isinstance(symbol, str) for symbol in expected_symbols)
        ):
            region, degraded = _answer_region(output_text, _ANSWER_LABELS_SYMBOLS)
            symbol_components.append(("independent_ast_symbols", expected_symbols, region, degraded))
        return _score_required_components(
            count_components=[("independent_ast_count", expected, got)],
            symbol_components=symbol_components,
            evaluator_used="_evaluate_oss",
            evaluator_version=_EVAL_VER_OSS,
            oracle_views=oracle_views if isinstance(oracle_views, Mapping) else None,
            check=check,
        )

    if check == "uncovered":
        expected = gt.get("uncovered_count", 0)
        required_components = gt.get("required_answer_components", [])
        count_patterns = [
            r"independent[\s_-]+ast[\s_-]+count\s*[:=]\s*(\d+)",
        ]
        if "independent_ast_count" in required_components:
            count_patterns.extend(
                [
                    r"(\d+)\s+uncovered\s+(?:public\s+)?symbols?\b",
                    r"uncovered\s+(?:public\s+)?symbols?\s*[:=]\s*(\d+)",
                    r"uncovered\s+(?:public\s+)?symbols?\s*\(\s*(\d+)\s*\)",
                ]
            )
        if "independent_ast_count" not in required_components:
            count_patterns.extend(
                [
                    r"(\d+)\s+uncovered",
                    r"(\d+)\s+(?:public\s+)?symbols?\s+uncovered",
                    r"uncovered[:\s]+(\d+)",
                    r"uncovered\s+public\s+symbols?[:\s—–]+(\d+)",
                    r"uncovered\s+(?:public\s+)?symbols?\s*\(\s*(\d+)\s*\)",
                    r"without\s+test.*?(\d+)",
                ]
            )
        got = _extract_count_answer_first(
            output_text,
            count_patterns,
        )
        expected_symbols = gt.get("uncovered_symbols")
        symbol_components: list[tuple[str, list[str], str, bool]] = []
        if (
            isinstance(required_components, list)
            and "independent_ast_symbols" in required_components
            and isinstance(expected_symbols, list)
            and all(isinstance(symbol, str) for symbol in expected_symbols)
        ):
            region, degraded = _answer_region(output_text, _ANSWER_LABELS_SYMBOLS)
            symbol_components.append(("independent_ast_symbols", expected_symbols, region, degraded))
        oracle_views = gt.get("oracle_views")
        return _score_required_components(
            count_components=[("independent_ast_count", expected, got)],
            symbol_components=symbol_components,
            evaluator_used="_evaluate_oss",
            evaluator_version=_EVAL_VER_OSS,
            oracle_views=oracle_views if isinstance(oracle_views, Mapping) else None,
            check=check,
        )

    return BenchQuality(scored=False)


# Generic method names that recur across many unrelated classes/modules. A bare
# Class.method tail ending in one of these is too weak a signal to credit a specific caller via the
# no-module fallback (Form 11): the same "Trainer.setup" / "Loop.run" tail can name a different
# caller in a different module. Distinctive names (e.g. `_evaluation_step`) are not blocklisted, so
# legitimate unqualified codemap answers still score.
_COMMON_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "run",
        "setup",
        "teardown",
        "main",
        "forward",
        "step",
        "reset",
        "close",
        "open",
        "start",
        "stop",
        "call",
        "fit",
        "test",
        "validate",
        "predict",
        "update",
        "configure",
        "build",
        "init",
        "load",
        "save",
    }
)


def _module_compatible(gt_module: str, found_module: str) -> bool:
    """Return True when *found_module* may denote the same module as *gt_module*.

    Modules match when they are equal or when one is a dotted suffix of the other — the latter allows
    the legitimate abbreviated-path form (``loops.evaluation_loop`` for
    ``lightning.pytorch.loops.evaluation_loop``) while still rejecting a genuinely different module
    that merely shares a class/method tail (``a.wrong`` vs ``a.right``).

    Args:
        gt_module: Module component of a ground-truth caller (before ``::``).
        found_module: Module component of a caller extracted from the agent output.

    Returns:
        True when the two module strings are compatible.

    Examples:
        >>> _module_compatible("lightning.pytorch.loops.evaluation_loop", "loops.evaluation_loop")
        True
        >>> _module_compatible("a.right", "a.wrong")
        False
        >>> _module_compatible("x.mod", "x.mod")
        True
    """
    if gt_module == found_module:
        return True
    return gt_module.endswith("." + found_module) or found_module.endswith("." + gt_module)


def _norm_cls(qualname: str) -> str:
    """Normalize a qualified caller to its underscore-insensitive ``Class.method`` tail.

    Drops the module prefix and strips leading underscores from the class component, so the
    fuzzy caller tier credits format variants like ``EvaluationLoop.run`` against a ground-truth
    ``_EvaluationLoop.run``. Bare function tails (no ``.``) are returned unchanged.

    Args:
        qualname: Caller in ``module::Class.method`` form (or any tail subset of it).

    Returns:
        The normalized tail used for fuzzy comparison.

    Examples:
        >>> _norm_cls("lightning.loops.evaluation_loop::_EvaluationLoop.run")
        'EvaluationLoop.run'
        >>> _norm_cls("pkg.mod::plain_function")
        'plain_function'
    """
    tail = qualname.split("::")[-1]
    if "." not in tail:
        return tail
    cls, _, meth = tail.partition(".")
    return f"{cls.lstrip('_')}.{meth}"


def _evaluate_develop_br(task: dict, output_text: str) -> BenchQuality:
    """Evaluate develop_blast_radius task: measure caller recall.

    Primary metric: fraction of expected fn_callers found in output (recall ≥ 0.7 = correct).
    Measures whether developer framing + codemap yields comprehensive caller enumeration.

    Args:
        task: Task dict from tasks-bench.json with ground_truth.fn_callers list.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with correct=True when recall >= 0.7; metric_got = TP count.
    """
    gt = task["ground_truth"]
    expected_callers: list[str] = gt.get("fn_callers", [])
    if not expected_callers:
        return BenchQuality(scored=False)

    expected_set = set(expected_callers)
    found_qualnames = _match_callers(output_text, expected_callers)

    true_positives = len(expected_set & found_qualnames)
    recall = true_positives / max(len(expected_set), 1)
    correct = recall >= 0.70

    # Tail-recall diagnostics only — does not affect the recall scalar or the 0.70 threshold above.
    matched_callers, missed_callers = _split_matched_missed(expected_set, found_qualnames)

    return BenchQuality(
        scored=True,
        correct=correct,
        metric_expected=len(expected_set),
        metric_got=true_positives,
        recall=round(recall, 3),
        caller_count_gt=gt["unique_caller_count"],
        extraction_failed=len(found_qualnames) == 0,
        evaluator_used="_evaluate_develop_br",
        evaluator_version=_EVAL_VER_NAME_RECALL,
        extracted_metric=sorted(found_qualnames),
        scoring_detail={
            "metric_expected": len(expected_set),
            "metric_got": true_positives,
            "threshold": 0.70,
            "method": "recall",
            "safety_grade": recall >= 0.90,
            "matched_callers": matched_callers,
            "missed_callers": missed_callers,
        },
    )


def _match_callers(output_text: str, expected_callers: list[str]) -> set[str]:
    """Extract ground-truth callers named in *output_text* for caller and diff-impact scoring.

    The multi-form caller matcher shared by the develop_blast_radius / fn_call_graph evaluators and the
    diff-impact evaluator. It recognises the eleven output shapes agents emit for caller
    lists — canonical ``module::Class.method``, multi-``::`` chains, file-path forms, grouped headers +
    bullets, markdown tables, bold-backtick + numbered lists, slash-paired abbreviations, a fully-dotted
    reverse lookup, an always-on underscore-insensitive fuzzy tier gated on module compatibility, and a
    bare ``Class.method`` fallback that fires only when every other form produced nothing and rejects
    generic method tails. Only ``output_text`` is scored — no file is read (a ``→ foo.md`` pointer is
    ordinary text, since Write/Edit are blocked on both arms).

    Args:
        output_text: The agent's full response text.
        expected_callers: Ground-truth caller qualified names (``module::Class.method``).

    Returns:
        Candidate qualified names found in *output_text*. Callers intersect this with their expected
        set to get the true positives; the returned set may include normalization artifacts (e.g. an
        alternate ``::`` split) that fall outside the expected set and are discarded by that
        intersection.

    Examples:
        >>> found = _match_callers("caller: a.b::Foo.bar", ["a.b::Foo.bar", "a.b::Baz.qux"])
        >>> sorted(found & {"a.b::Foo.bar", "a.b::Baz.qux"})
        ['a.b::Foo.bar']
        >>> _match_callers("nothing relevant", ["a.b::Foo.bar"]) & {"a.b::Foo.bar"}
        set()
    """
    found_raw = _extract_caller_raw_forms(output_text)
    return _normalize_caller_forms(found_raw, output_text, expected_callers)


def _extract_caller_raw_forms(output_text: str) -> list[str]:
    """Extract raw ``module::callee`` tokens from *output_text* across ten regex output shapes.

    The first phase of :func:`_match_callers`: scans the agent output for every caller
    shape agents emit — canonical ``ns.x::Class.method``, multi-``::`` chains, ``.py:``/``src/`` file
    paths, grouped headers + bullets, markdown tables, section-header + rows, bold-backtick + numbered
    lists, and slash-paired abbreviations — and returns the raw tokens (pre-normalization). Kept as a
    single function because the forms share the ``_ns_pat`` alternation and header-position scans;
    ``# noqa: C901`` because splitting the ten independent, extensively-tested regex blocks further
    would fragment tightly-coupled matching logic without reducing real risk.

    Args:
        output_text: The agent's full response text.

    Returns:
        Raw ``module::callee`` candidate tokens (unnormalized; may contain duplicates/artifacts).
    """
    # Extract qualified names. Ten regex forms; :func:`_normalize_caller_forms` maps them to canonical.
    _ns_alt = "|".join(re.escape(n) for n in _REPO_NAMESPACE)
    _ns_pat = f"(?:{_ns_alt})"

    found_raw: list[str] = []
    # Form 1: canonical dotted-namespace form (ns.x.y::Class.method)
    for m in re.finditer(rf"\b({_ns_pat}(?:\.[\w]+)+)::([\w]+(?:\.[\w]+)*)", output_text):
        found_raw.append(f"{m.group(1)}::{m.group(2)}")
    # Form 2: multi-:: chain — captures full chain including Class::method suffix.
    # Matches fabric.x::Class::method AND ns.x::Class::method greedily.
    # Strip trailing backtick/space — backtick-wrapped markdown cells add trailing ` to match.
    for m in re.finditer(r"\b([\w.]+(?:::[\w][^:\s]*)+)", output_text):
        found_raw.append(m.group(1).rstrip("`"))
    # Form 3: file-path with single colon: ns/x/y.py:Class.method
    for m in re.finditer(rf"\b({_ns_pat}(?:/[\w]+)+\.py):([\w]+\.[\w]+)\b", output_text):
        mod = m.group(1).replace("/", ".")[: -len(".py")]
        found_raw.append(f"{mod}::{m.group(2)}")
    # Form 4: src-rooted file path: src/ns/x/y.py::Class.method
    for m in re.finditer(rf"\bsrc/({_ns_pat}(?:/[\w]+)+\.py)::([\w]+(?:\.[\w]+)*)\b", output_text):
        mod = m.group(1).replace("/", ".")[: -len(".py")]
        found_raw.append(f"{mod}::{m.group(2)}")
    # Form 6: grouped "### ns.module.sub" headers + "- Class.method" bullets.
    # Reconstruct module::Class.method from the nearest preceding header.
    _hdr_positions = [
        (hm.start(), hm.group(1)) for hm in re.finditer(rf"^#+\s+({_ns_pat}(?:\.[\w]+)+)", output_text, re.MULTILINE)
    ]
    for bm in re.finditer(r"^[-*]\s+([\w]+\.[\w]+)\s*$", output_text, re.MULTILINE):
        bpos = bm.start()
        cur_mod = None
        for hpos, hmod in _hdr_positions:
            if hpos < bpos:
                cur_mod = hmod
            else:
                break
        if cur_mod:
            found_raw.append(f"{cur_mod}::{bm.group(1)}")
    # Form 7: markdown table "| module | Class.method |" — adjacent cells.
    # Handles backtick-wrapped cells and module-level functions (no Class prefix).
    for tm in re.finditer(
        rf"\|\s*`?({_ns_pat}(?:\.[\w]+)+)`?\s*\|\s*`?([\w]+(?:\.[\w]+)?)`?\s*\|",
        output_text,
    ):
        found_raw.append(f"{tm.group(1)}::{tm.group(2)}")
    # Form 8: section header "### `module` (desc)" or "### `module::Class`" + table function rows.
    # First column of each table row is the function (or method) name.
    _sec_hdr_re = re.compile(rf"^#+\s+`?({_ns_pat}(?:\.[\w]+)+(?:::[\w.]+)?)`?(?:[\s(]|$)", re.MULTILINE)
    _sec_positions = [(hm.start(), hm.group(1)) for hm in _sec_hdr_re.finditer(output_text)]
    _SKIP_CELLS = frozenset({"function", "module", "caller", "class", "method", "name", "#"})
    for row_m in re.finditer(r"^\|\s*`?([\w][^|`\n]*?)`?\s*\|", output_text, re.MULTILINE):
        fn_name = row_m.group(1).strip()
        if not fn_name or fn_name.lower() in _SKIP_CELLS or set(fn_name) <= set("-| "):
            continue
        row_start = row_m.start()
        cur_hdr = None
        for hpos, hval in _sec_positions:
            if hpos < row_start:
                cur_hdr = hval
            else:
                break
        if not cur_hdr:
            continue
        if "::" in cur_hdr:
            found_raw.append(f"{cur_hdr}.{fn_name}")
        else:
            found_raw.append(f"{cur_hdr}::{fn_name}")
    # Form 9: bold-backtick module header + numbered backtick list.
    # Handles: **`ns.mod::Class`** or **ns.module** followed by "1. `Class.method`".
    # Agent used this format when outputting caller lists for FN/BR tasks.
    _bold_hdrs_9 = [
        (bm.start(), bm.group(1))
        for bm in re.finditer(
            rf"\*\*`?({_ns_pat}(?:\.[\w]+)+(?:::[\w.]+)?)`?\*\*",
            output_text,
        )
    ]
    for nm in re.finditer(r"^\s*\d+\.\s+`([\w.]+(?:::[\w.]+)?)`", output_text, re.MULTILINE):
        item_text = nm.group(1).rstrip("`")
        item_pos = nm.start()
        cur_hdr_9 = None
        for hpos, hval in _bold_hdrs_9:
            if hpos < item_pos:
                cur_hdr_9 = hval
            else:
                break
        if "::" in item_text:
            found_raw.append(item_text)
        elif cur_hdr_9:
            hdr_mod = cur_hdr_9.split("::")[0] if "::" in cur_hdr_9 else cur_hdr_9
            found_raw.append(f"{hdr_mod}::{item_text}")
    # Form 10: slash-paired abbreviated callers under same class prefix.
    # Handles "ns.mod::Class.fn1/fn2" (agent collapsed two callers into one token).
    for sm in re.finditer(
        rf"\b({_ns_pat}(?:\.[\w]+)*(?:::[\w]+)?\.[\w]+)/([\w]+)\b",
        output_text,
    ):
        left = sm.group(0).split("/")[0]
        right_fn = sm.group(2)
        found_raw.append(left)
        cls_prefix = left.rsplit(".", 1)[0] if "." in left else left
        found_raw.append(f"{cls_prefix}.{right_fn}")

    return found_raw


def _normalize_caller_forms(found_raw: list[str], output_text: str, expected_callers: list[str]) -> set[str]:
    """Map raw caller tokens to canonical ``module::Class.method`` and match them to *expected_callers*.

    The second phase of :func:`_match_callers`: normalizes each raw token from
    :func:`_extract_caller_raw_forms` (default split, multi-``::`` split points, ``module.Class::method``
    reclassification, abbreviated-suffix match), adds the fully-dotted reverse lookup (Form 5), the
    always-on underscore-insensitive fuzzy tier (gated on :func:`_module_compatible`), and the bare
    ``Class.method`` fallback (Form 11, module-blind, fired only when nothing else matched and rejecting
    generic method tails). ``# noqa: C901`` because these normalization tiers are interdependent and
    order-sensitive; splitting further would fragment tested matching semantics without reducing risk.

    Args:
        found_raw: Raw ``module::callee`` tokens from :func:`_extract_caller_raw_forms`.
        output_text: The agent's full response text (needed for the Form 5 / Form 11 reverse lookups).
        expected_callers: Ground-truth caller qualified names.

    Returns:
        Candidate canonical qualified names (intersect with expected set for true positives).
    """
    expected_set = set(expected_callers)
    # Normalize all extracted tokens → canonical module::Class.method
    _ns_prefixes = [""] + [f"{n}." for n in _REPO_NAMESPACE]
    found_qualnames: set[str] = set()
    for raw in found_raw:
        parts = raw.split("::")
        if len(parts) < 2:
            continue
        # Default: first segment = module, rest = callee
        default = f"{parts[0]}::{'.'.join(parts[1:])}"
        found_qualnames.add(default)
        for pfx in _ns_prefixes:
            candidate = f"{pfx}{default}"
            if candidate in expected_set:
                found_qualnames.add(candidate)
        # Multi-:: form: agent may use :: as path separator throughout.
        # Try all other split points; keep any whose module::callee is in expected_set.
        for split_i in range(2, len(parts)):
            module = ".".join(parts[:split_i])
            callee = ".".join(parts[split_i:])
            for pfx in _ns_prefixes:
                candidate = f"{pfx}{module}::{callee}"
                if candidate in expected_set:
                    found_qualnames.add(candidate)
        # module.Class::method form: agent wrote dot before :: instead of canonical ::.
        # Reclassify by splitting module on its last dot to surface the class component.
        # Fuzzy tier below then handles underscore-prefix mismatch (Class vs _Class).
        if "." in parts[0]:
            m_mod, _, m_cls = parts[0].rpartition(".")
            reclassified = f"{m_mod}::{m_cls}.{'.'.join(parts[1:])}"
            found_qualnames.add(reclassified)
            for pfx in _ns_prefixes:
                cand = f"{pfx}{reclassified}"
                if cand in expected_set:
                    found_qualnames.add(cand)
        # Suffix match: abbreviated module path (e.g. "loops.x" instead of "ns.pytorch.loops.x").
        # Match when callee is exact and GT module ends with .abbreviated_module.
        callee_suffix = ".".join(parts[1:])
        mod_token = parts[0]
        for canonical in expected_callers:
            can_parts = canonical.split("::")
            if len(can_parts) >= 2 and ".".join(can_parts[1:]) == callee_suffix:
                can_mod = can_parts[0]
                if can_mod.endswith("." + mod_token) or can_mod == mod_token:
                    found_qualnames.add(canonical)

    # Form 5: fully-dotted form — agent writes lightning.x.y.fn (no :: separator).
    # Reverse-lookup: for each expected caller, check its dotted equivalent with word-boundary
    # lookarounds to avoid substring FPs (e.g. _fn matching _fn_helper).
    dotted_to_canonical = {c.replace("::", "."): c for c in expected_callers}
    for dotted, canonical in dotted_to_canonical.items():
        pattern = r"(?<![.\w])" + re.escape(dotted) + r"(?![.\w])"
        if re.search(pattern, output_text):
            found_qualnames.add(canonical)

    # Fuzzy tier (always-on): same method name exact, class name underscore-insensitive.
    # Catches format variants like "EvaluationLoop.method" when GT is "_EvaluationLoop.method" —
    # agent clearly identified the caller, just dropped the access-modifier underscore convention.
    # The module is now compared too (via _module_compatible): matching on the
    # Class.method tail alone credited a wrong-module same-tail caller (a `Loop.run` in a different
    # module scored the GT caller). Requiring module compatibility keeps the underscore tolerance
    # while rejecting cross-module tail collisions.
    already_exact = expected_set & found_qualnames
    for canonical in expected_callers:
        if canonical not in already_exact and "." in canonical.split("::")[-1]:
            norm = _norm_cls(canonical)
            gt_mod = canonical.split("::")[0]
            if any(_norm_cls(qn) == norm and _module_compatible(gt_mod, qn.split("::")[0]) for qn in found_qualnames):
                found_qualnames.add(canonical)

    # Form 11: bare Class.method fallback — fires ONLY when all other forms produced nothing.
    # Codemap arm sometimes outputs callers as "_EvaluationLoop._evaluation_step" without module
    # prefix; reverse-lookup against GT by matching the tail component of each expected caller.
    # This tier carries NO module qualification, so a bare Class.method whose method is
    # a generic name (`Loop.run`, `Trainer.setup`) is rejected: the same tail can name a different
    # caller in a different module. Distinctive method tails (`_evaluation_step`) still credit.
    if not found_qualnames:
        for canonical in expected_callers:
            parts = canonical.split("::")
            if len(parts) < 2:
                continue
            tail = parts[-1]  # e.g. "_EvaluationLoop._evaluation_step"
            if "." not in tail:
                continue  # skip bare function names (too short, high FP risk)
            if tail.rsplit(".", 1)[-1].lstrip("_") in _COMMON_METHOD_NAMES:
                continue  # unqualified bare common tail — too weak to credit a specific caller
            pattern = r"(?<![.\w])" + re.escape(tail) + r"(?![.\w])"
            if re.search(pattern, output_text):
                found_qualnames.add(canonical)
                continue
            # Also match without leading underscores on the class part (EvaluationLoop.method)
            tail_parts = tail.split(".", 1)
            if tail_parts[0].startswith("_"):
                stripped_tail = tail_parts[0].lstrip("_") + "." + tail_parts[1]
                pattern2 = r"(?<![.\w])" + re.escape(stripped_tail) + r"(?![.\w])"
                if re.search(pattern2, output_text):
                    found_qualnames.add(canonical)

    return found_qualnames


def _evaluate_debug(task: dict, output_text: str) -> BenchQuality:
    """Evaluate debug_from_trace task: function name + file basename both present.

    Correct when both the function name and the file basename (without .py) appear
    in the output. recall = hits / 2; correct requires recall == 1.0.

    Args:
        task: Task dict with ground_truth.file, .function, .start_line.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with recall = fraction of {function, file_basename} found.
    """
    gt = task["ground_truth"]
    fn_name: str = gt.get("function", "")
    file_path: str = gt.get("file", "")
    file_stem = file_path.split("/")[-1].replace(".py", "")

    # Score inside the structured answer block; require blocklisted file stems to appear
    # as a qualified reference (`x.py` or a path), never as a bare prose word.
    region, degraded = _answer_region(output_text, _ANSWER_LABELS_FILES)
    fn_found = bool(fn_name) and bool(re.search(r"\b" + re.escape(fn_name) + r"\b", region, re.IGNORECASE))
    file_found = bool(file_stem) and _stem_matches(file_stem, region)

    total = sum([bool(fn_name), bool(file_stem)])
    hits = sum([fn_found, file_found])
    recall = hits / total if total > 0 else 0.0

    return BenchQuality(
        scored=True,
        correct=fn_found and file_found,
        recall=round(recall, 3),
        extraction_failed=not fn_found and not file_found,
        extraction_degraded=degraded,
        evaluator_used="_evaluate_debug",
        evaluator_version=_EVAL_VER_DEBUG,
        scoring_detail={
            "function": fn_name,
            "fn_found": fn_found,
            "file": file_path,
            "file_found": file_found,
            "recall": recall,
            "method": "answer_block_stem_match",
        },
    )


def _evaluate_feature(task: dict, output_text: str) -> BenchQuality:
    """Evaluate feature_scaffolding task: exact labelled entry point and file path.

    Correct when the answer explicitly labels the task's full entry point and
    repository-relative primary file. This prevents exploratory prose from
    accidentally crediting a different final recommendation.

    Args:
        task: Task dict with ground_truth.entry_point, .primary_file.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with correct=True when both components found.
    """
    gt = task["ground_truth"]
    entry_point: str = gt.get("entry_point", "")
    primary_file: str = gt.get("primary_file", "")
    # Score only explicit conclusion fields. A Class.method can appear during exploration while the
    # final answer names a different extension point, so substring matching is not a valid oracle.
    region, degraded = _answer_region(output_text, _ANSWER_LABELS_FILES)
    entry_pattern = r"(?im)^\s*(?:[-*]\s*)?entry[\s_-]*point\s*:\s*`?" + re.escape(entry_point) + r"`?\.?\s*$"
    file_pattern = r"(?im)^\s*(?:[-*]\s*)?primary[\s_-]*file\s*:\s*`?" + re.escape(primary_file) + r"`?\s*$"
    ep_found = bool(entry_point) and bool(re.search(entry_pattern, region))
    file_found = bool(primary_file) and bool(re.search(file_pattern, region))

    total = sum([bool(entry_point), bool(primary_file)])
    hits = sum([ep_found, file_found])
    recall = hits / total if total > 0 else 0.0

    return BenchQuality(
        scored=True,
        correct=ep_found and file_found,
        recall=round(recall, 3),
        extraction_failed=not ep_found and not file_found,
        extraction_degraded=degraded,
        evaluator_used="_evaluate_feature",
        evaluator_version=_EVAL_VER_FEATURE,
        scoring_detail={
            "entry_point": entry_point,
            "ep_found": ep_found,
            "primary_file": primary_file,
            "file_found": file_found,
            "recall": recall,
            "method": "labelled_entry_point_and_primary_file",
        },
    )


_RI_RECALL_THRESHOLD = 0.70


def _evaluate_real_issue(task: dict, output_text: str) -> BenchQuality:
    """Evaluate real_issue task: file-set recall over ground_truth.files_changed.

    Recall = |GT files referenced by a pathed form in the answer block| / |GT files|.
    A file counts only via its full relative path or a path-with-parent form — a bare
    basename stem no longer scores. Correct when recall >= 0.70.

    Args:
        task: Task dict with ground_truth.files_changed list.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with recall and correct=True when recall >= 0.70.
    """
    gt = task["ground_truth"]
    gt_files: list[str] = gt.get("files_changed", [])
    if not gt_files:
        return BenchQuality(scored=False)

    # Require a full relative path or path-with-parent inside the structured answer block —
    # a bare basename stem (`trainer`) is a near-free hit and no longer counts.
    region, degraded = _answer_region(output_text, _ANSWER_LABELS_FILES)
    found = sum(1 for fp in gt_files if _ri_file_matches(fp, region))
    recall = found / len(gt_files)

    return BenchQuality(
        scored=True,
        correct=recall >= _RI_RECALL_THRESHOLD,
        recall=round(recall, 3),
        metric_expected=len(gt_files),
        metric_got=found,
        extraction_failed=found == 0,
        extraction_degraded=degraded,
        evaluator_used="_evaluate_real_issue",
        evaluator_version=_EVAL_VER_REAL_ISSUE,
        scoring_detail={
            "gt_files": gt_files,
            "files_found": found,
            "recall": recall,
            "threshold": _RI_RECALL_THRESHOLD,
            "method": "answer_block_path_match",
        },
    )


# ---------------------------------------------------------------------------
# Diff-impact and graph evaluators
# ---------------------------------------------------------------------------

_DI_RECALL_THRESHOLD = 0.70  # caller recall AND test-file recall must each clear this for DI correctness
_GR_RECALL_THRESHOLD = 0.70  # central set-overlap / fn-blast recall threshold
_MB_RECALL_THRESHOLD = 0.70  # module_blast_radius importer (import fan-in) recall threshold

# Tail-recall instrumentation (additive diagnostics only): matched/missed name lists are recorded in
# scoring_detail so a run's exact hit/miss split can be inspected without re-scoring. They never feed
# the recall scalar or the pass threshold. Lists are sorted and bounded to keep the JSONL line small on
# high-fan-in tasks (hundreds of callers/importers); the count fields carry the untruncated totals.
_TAIL_LIST_CAP = 50


def _split_matched_missed(expected: set[str], found: set[str]) -> tuple[list[str], list[str]]:
    """Return ``(matched, missed)`` sorted, each bounded to :data:`_TAIL_LIST_CAP` names (tail-recall).

    Additive diagnostics for the caller / importer recall evaluators: the intersection is the matched
    set, the difference is the missed set. Both are sorted for stable output and truncated to the cap so
    a high-fan-in task does not bloat the result line — the evaluator's own count fields remain the
    authoritative totals.

    Args:
        expected: Ground-truth qualified names.
        found: Names matched in the agent output.

    Returns:
        ``(matched, missed)`` — sorted name lists, each at most :data:`_TAIL_LIST_CAP` long.

    Examples:
        >>> _split_matched_missed({"a", "b", "c"}, {"a", "c"})
        (['a', 'c'], ['b'])
        >>> _split_matched_missed({"x"}, set())
        ([], ['x'])
    """
    matched = sorted(expected & found)[:_TAIL_LIST_CAP]
    missed = sorted(expected - found)[:_TAIL_LIST_CAP]
    return matched, missed


def _module_mentioned(module: str, output_text: str) -> bool:
    """Return True when dotted *module* is named in *output_text* (exact or abbreviated-suffix form).

    A module counts when its full dotted name appears, or when a distinctive dotted suffix of it
    appears (``loops.evaluation_loop`` for ``lightning.pytorch.loops.evaluation_loop``) — the same
    abbreviation tolerance the caller matcher grants. A single-component tail is too weak and is not
    accepted on its own. Word-boundary lookarounds prevent substring false positives.

    Args:
        module: Dotted module name from ground truth.
        output_text: Agent's full response text.

    Returns:
        True when the module is named by an exact or ≥2-component-suffix form.

    Examples:
        >>> _module_mentioned("lightning.pytorch.loops.evaluation_loop", "see loops.evaluation_loop")
        True
        >>> _module_mentioned("a.b.c", "unrelated prose")
        False
    """
    forms = {module}
    parts = module.split(".")
    for start in range(1, len(parts) - 1):  # ≥2-component suffixes only
        forms.add(".".join(parts[start:]))
    return any(re.search(r"(?<![\w.])" + re.escape(f) + r"(?![\w.])", output_text) for f in forms)


def _set_recall(expected: list[str], predicate: Any) -> tuple[int, float]:
    """Return ``(hits, recall)`` for *expected* items, each tested by *predicate*.

    Args:
        expected: Ground-truth items.
        predicate: One-argument callable returning True when the item is present in the output.

    Returns:
        ``(hits, recall)`` — recall is ``hits / len(expected)`` (0.0 when *expected* is empty).

    Examples:
        >>> _set_recall(["a", "b"], lambda x: x == "a")
        (1, 0.5)
        >>> _set_recall([], lambda x: True)
        (0, 0.0)
    """
    hits = sum(1 for item in expected if predicate(item))
    return hits, (hits / len(expected) if expected else 0.0)


def _explicit_h2_section(output_text: str, label: str) -> tuple[str, bool]:
    """Return one exact Markdown H2 answer section without cross-section fallback.

    Diff-impact prompts require separate ``## Callers`` and ``## Tests`` lists. Restricting their evaluators to these
    bounded sections keeps exploratory prose and misplaced answers from becoming scoreable evidence.
    """
    pattern = rf"(?im)^##[ \t]+{re.escape(label)}[ \t]*$"
    match = re.search(pattern, output_text)
    if match is None:
        return "", False
    next_heading = re.search(r"(?m)^##[ \t]+", output_text[match.end() :])
    end = match.end() + next_heading.start() if next_heading is not None else len(output_text)
    return output_text[match.end() : end], True


def _evaluate_diff_impact(task: dict, output_text: str) -> BenchQuality:
    """Evaluate scoped DI callers/tests with continuous precision-recall fitness.

    A diff-impact task stages a change and asks for the blast radius: which modules/callers are affected
    and which tests to run. Correctness requires BOTH the caller recall (reusing the develop_br
    multi-form matcher, :func:`_match_callers`) and the test-module recall (dotted-name match) to clear
    0.70 — a good blast-radius answer names both what breaks and what to re-run.

    Args:
        task: Task dict; reads ``ground_truth.fn_callers`` and ``.test_modules``.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with averaged caller/test F1 fitness; binary correctness keeps
        the transparent per-component recall thresholds.
    """
    gt = task["ground_truth"]
    expected_callers: list[str] = gt.get("fn_callers", [])
    expected_tests: list[str] = gt.get("test_modules", [])
    if not expected_callers and not expected_tests:
        return BenchQuality(scored=False)

    callers_section, callers_section_found = _explicit_h2_section(output_text, "Callers")
    tests_section, tests_section_found = _explicit_h2_section(output_text, "Tests")
    found_callers = _match_callers(callers_section, expected_callers)
    caller_hits = len(set(expected_callers) & found_callers)
    caller_recall = caller_hits / len(expected_callers) if expected_callers else 1.0
    caller_candidates = set(_extract_caller_raw_forms(callers_section))
    caller_precision = caller_hits / len(caller_candidates) if caller_candidates else 0.0
    caller_fitness = (
        2 * caller_precision * caller_recall / (caller_precision + caller_recall)
        if caller_precision + caller_recall
        else 0.0
    )

    test_hits, test_recall = _set_recall(expected_tests, lambda m: _module_mentioned(m, tests_section))
    if not expected_tests:
        test_recall = 1.0
    test_candidates = set(re.findall(r"\b(?:tests|parity)_[\w.]+", tests_section))
    test_precision = test_hits / len(test_candidates) if test_candidates else 0.0
    test_fitness = (
        2 * test_precision * test_recall / (test_precision + test_recall) if test_precision + test_recall else 0.0
    )

    correct = caller_recall >= _DI_RECALL_THRESHOLD and test_recall >= _DI_RECALL_THRESHOLD
    return BenchQuality(
        scored=True,
        correct=correct,
        metric_expected=len(expected_callers),
        metric_got=caller_hits,
        recall=round((caller_fitness + test_fitness) / 2, 3),
        caller_count_gt=gt.get("unique_caller_count"),
        extraction_failed=not found_callers and test_hits == 0,
        evaluator_used="_evaluate_diff_impact",
        scoring_detail={
            "caller_recall": round(caller_recall, 3),
            "test_recall": round(test_recall, 3),
            "caller_precision": round(caller_precision, 3),
            "test_precision": round(test_precision, 3),
            "caller_fitness": round(caller_fitness, 3),
            "test_fitness": round(test_fitness, 3),
            "caller_expected": len(expected_callers),
            "caller_got": caller_hits,
            "test_expected": len(expected_tests),
            "test_got": test_hits,
            "callers_section_found": callers_section_found,
            "tests_section_found": tests_section_found,
            "threshold": _DI_RECALL_THRESHOLD,
            "method": "scoped_caller_test_f1_with_recall_gates",
        },
    )


def _evaluate_graph_central(task: dict, output_text: str) -> BenchQuality:
    """Evaluate whether a ``graph_central`` result overlaps the top-N central modules by at least 0.70.

    The agent lists the most-imported modules; correctness is the fraction of ground-truth central
    modules named in the output (order-insensitive set overlap).

    Args:
        task: Task dict; reads ``ground_truth.central_modules``.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with recall = set overlap.
    """
    gt = task["ground_truth"]
    expected: list[str] = gt.get("central_modules", [])
    if not expected:
        return BenchQuality(scored=False)
    hits, recall = _set_recall(expected, lambda m: _module_mentioned(m, output_text))
    return BenchQuality(
        scored=True,
        correct=recall >= _GR_RECALL_THRESHOLD,
        metric_expected=len(expected),
        metric_got=hits,
        recall=round(recall, 3),
        extraction_failed=hits == 0,
        evaluator_used="_evaluate_graph_central",
        scoring_detail={
            "metric_expected": len(expected),
            "metric_got": hits,
            "threshold": _GR_RECALL_THRESHOLD,
            "method": "set_overlap",
        },
    )


def _evaluate_graph_path(task: dict, output_text: str) -> BenchQuality:
    """Evaluate whether a ``graph_path`` result equals the oracle path.

    The ground-truth path is the unique shortest import chain. The agent's chain matches when every GT
    hop module is named in the output AND they appear in the GT order — a correct answer must trace the
    same chain. Because pairs are chosen where the shortest path is unique (generator enforces
    ``path_is_unique``), the single oracle path is the only valid answer.

    Args:
        task: Task dict; reads ``ground_truth.import_path`` (list of module names, source→target).
        output_text: Agent's full response text.

    Returns:
        BenchQuality; correct when every hop is present in GT order.
    """
    gt = task["ground_truth"]
    path: list[str] = gt.get("import_path") or []
    if not path:
        return BenchQuality(scored=False)
    positions = [_module_first_pos(hop, output_text) for hop in path]
    all_present = all(pos is not None for pos in positions)
    in_order = all_present and all(
        positions[i] < positions[i + 1]  # type: ignore[operator]
        for i in range(len(positions) - 1)
    )
    hits = sum(1 for pos in positions if pos is not None)
    return BenchQuality(
        scored=True,
        correct=bool(in_order),
        metric_expected=len(path),
        metric_got=hits,
        recall=round(hits / len(path), 3),
        extraction_failed=hits == 0,
        evaluator_used="_evaluate_graph_path",
        scoring_detail={
            "expected_path": path,
            "hops_found": hits,
            "in_order": bool(in_order),
            "method": "ordered_chain_match",
        },
    )


def _module_first_pos(module: str, output_text: str) -> int | None:
    """Return the first character offset at which *module* is named, or ``None``.

    Uses the same exact/≥2-component-suffix matching as :func:`_module_mentioned`, returning the
    earliest match offset across all accepted forms so path-order can be checked.

    Args:
        module: Dotted module name.
        output_text: Agent's full response text.

    Returns:
        Earliest match offset, or None when the module is not named.

    Examples:
        >>> _module_first_pos("a.b.c", "start a.b.c end")
        6
        >>> _module_first_pos("a.b.c", "nothing") is None
        True
    """
    forms = {module}
    parts = module.split(".")
    for start in range(1, len(parts) - 1):
        forms.add(".".join(parts[start:]))
    positions = [
        m.start()
        for f in forms
        if (m := re.search(r"(?<![\w.])" + re.escape(f) + r"(?![\w.])", output_text)) is not None
    ]
    return min(positions) if positions else None


def _evaluate_graph_fn_blast(task: dict, output_text: str) -> BenchQuality:
    """Evaluate whether ``graph_fn_blast`` transitive-caller recall is at least 0.70.

    Reuses the develop_br multi-form caller matcher (:func:`_match_callers`) against the depth-N
    transitive caller closure.

    Args:
        task: Task dict; reads ``ground_truth.blast_callers``.
        output_text: Agent's full response text.

    Returns:
        BenchQuality with recall over the transitive closure.
    """
    gt = task["ground_truth"]
    expected: list[str] = gt.get("blast_callers", [])
    if not expected:
        return BenchQuality(scored=False)
    found = _match_callers(output_text, expected)
    hits = len(set(expected) & found)
    recall = hits / len(expected)
    return BenchQuality(
        scored=True,
        correct=recall >= _GR_RECALL_THRESHOLD,
        metric_expected=len(expected),
        metric_got=hits,
        recall=round(recall, 3),
        extraction_failed=not found,
        evaluator_used="_evaluate_graph_fn_blast",
        scoring_detail={
            "metric_expected": len(expected),
            "metric_got": hits,
            "threshold": _GR_RECALL_THRESHOLD,
            "method": "recall",
        },
    )


def _evaluate_module_blast_radius(task: dict, output_text: str) -> BenchQuality:
    """Evaluate whether ``module_blast_radius`` importer recall is at least 0.70.

    The reverse relation of the develop_br per-function caller recall, at module granularity: given a
    target module, the agent enumerates the modules that IMPORT it (its rdeps). Correctness is the
    fraction of ground-truth importers named in the output, matched by :func:`_module_mentioned` — an
    exact dotted name or a ≥2-component dotted suffix, NEVER a bare single-component leaf. Handling of extraction
    failures mirrors :func:`_evaluate_develop_br`: when not a single importer is named the run
    is flagged ``extraction_failed`` and excluded from the accuracy denominator upstream.

    Args:
        task: Task dict; reads ``ground_truth.importers`` (dotted module names).
        output_text: Agent's full response text.

    Returns:
        BenchQuality with recall = importer recall; correct when recall ≥ 0.70.
    """
    gt = task["ground_truth"]
    expected_importers: list[str] = gt.get("importers", [])
    if not expected_importers:
        return BenchQuality(scored=False)

    expected_set = set(expected_importers)
    found = {m for m in expected_set if _module_mentioned(m, output_text)}
    hits = len(found)
    recall = hits / max(len(expected_set), 1)
    correct = recall >= _MB_RECALL_THRESHOLD

    # Tail-recall diagnostics only — does not affect the recall scalar or the threshold above.
    matched_importers, missed_importers = _split_matched_missed(expected_set, found)

    return BenchQuality(
        scored=True,
        correct=correct,
        metric_expected=len(expected_set),
        metric_got=hits,
        recall=round(recall, 3),
        extraction_failed=hits == 0,
        evaluator_used="_evaluate_module_blast_radius",
        extracted_metric=sorted(found),
        scoring_detail={
            "metric_expected": len(expected_set),
            "metric_got": hits,
            "threshold": _MB_RECALL_THRESHOLD,
            "method": "recall",
            "matched_importers": matched_importers,
            "missed_importers": missed_importers,
        },
    )


_EVALUATORS = {
    "symbol_extraction": _evaluate_symbol,
    "fn_call_graph": _evaluate_develop_br,  # name-recall, not count-tolerance: callers are enumerated, not counted
    "review_assistance": _evaluate_rv,
    "code_quality": _evaluate_oss,
    "develop_blast_radius": _evaluate_develop_br,
    "debug_from_trace": _evaluate_debug,
    "feature_scaffolding": _evaluate_feature,
    "real_issue": _evaluate_real_issue,
    "diff_impact": _evaluate_diff_impact,
    "graph_central": _evaluate_graph_central,
    "graph_path": _evaluate_graph_path,
    "graph_fn_blast": _evaluate_graph_fn_blast,
    "module_blast_radius": _evaluate_module_blast_radius,
}


@dataclass(frozen=True)
class _BenchEvaluatorAdapter:
    """Callable adapter from one detailed Claude evaluator to the provider-neutral contract.

    Attributes:
        evaluator: Existing evaluator that returns the detailed Claude score object.

    Examples:
        >>> detailed = BenchQuality(scored=True, correct=True, recall=0.5)
        >>> adapter = _BenchEvaluatorAdapter(lambda _task, _output: detailed)
        >>> adapter({}, "answer").quality_score
        0.5
    """

    evaluator: Callable[[Mapping[str, Any], str], BenchQuality]

    def __call__(self, task: Mapping[str, Any], output_text: str) -> EvaluationResult:
        """Evaluate one response once and package its normalized and detailed scores.

        Args:
            task: Task mapping handed to the wrapped evaluator.
            output_text: Agent response text to score.

        Returns:
            A shared evaluator result that preserves that exact detailed score.
        """
        quality = self.evaluator(task, output_text)
        quality_score = quality.recall if quality.scored and quality.recall is not None else float(quality.correct)
        if not quality.scored:
            quality_score = None
        components: dict[str, float] = {}
        if quality.scored and quality.recall is not None:
            components["recall"] = float(quality.recall)
        required_components = quality.scoring_detail.get("components")
        if isinstance(required_components, Mapping):
            for name, component in required_components.items():
                if not isinstance(name, str) or not isinstance(component, Mapping):
                    continue
                fitness = component.get("fitness")
                if isinstance(fitness, (int, float)) and not isinstance(fitness, bool):
                    components[f"subanswer:{name}"] = float(fitness)
        for name in ("caller_recall", "test_recall"):
            value = quality.scoring_detail.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                components[name] = float(value)
        return _BenchEvaluationResult(
            scored=quality.scored,
            correct=quality.correct,
            quality_score=quality_score,
            extraction_failed=quality.extraction_failed,
            components=components,
            bench_quality=quality,
        )


def _wrap_bench_evaluator(
    evaluator: Callable[[Mapping[str, Any], str], BenchQuality],
) -> Callable[[Mapping[str, Any], str], EvaluationResult]:
    """Adapt one detailed Claude evaluator to the provider-neutral result contract.

    Args:
        evaluator: Existing evaluator that returns the detailed Claude score object.

    Returns:
        A callable that scores one response into a shared evaluator result, preserving
        that exact detailed score.

    Examples:
        >>> detailed = BenchQuality(scored=False, correct=False)
        >>> wrapped = _wrap_bench_evaluator(lambda _task, _output: detailed)
        >>> wrapped({}, "answer").scored
        False
    """
    return _BenchEvaluatorAdapter(evaluator)


_SHARED_EVALUATORS = EvaluatorRegistry(
    {task_type: _wrap_bench_evaluator(evaluator) for task_type, evaluator in _EVALUATORS.items()}
)


def _evaluate_shared_task(
    task: Mapping[str, Any], output_text: str, *, registry: EvaluatorRegistry | None = None
) -> BenchQuality:
    """Score one task through the shared registry and retain Claude diagnostics.

    Args:
        task: Raw scoreable task definition.
        output_text: Claude response text.
        registry: Test-only replacement registry; uses the locked runner registry by default.

    Returns:
        The exact detailed score produced by the one shared evaluator invocation.

    Raises:
        TypeError: If a scoreable task's registry result lacks Claude diagnostics.
        ValueError: If a scoreable task type is not registered.
    """
    result = (registry or _SHARED_EVALUATORS).evaluate(task, output_text)
    if not isinstance(result, _BenchEvaluationResult):
        raise TypeError("shared evaluator did not return Claude benchmark diagnostics")
    return result.bench_quality


def _evaluator_provenance(task: Mapping[str, Any]) -> tuple[str, str]:
    """Return the exact evaluator identity and source hash for one task contract."""
    if task.get("scoreable") is False:
        evaluator_id = "unscored"
        return evaluator_id, hashlib.sha256(b"scoreable=false bypass").hexdigest()
    task_type = task.get("type")
    evaluator = _EVALUATORS.get(task_type) if isinstance(task_type, str) else None
    if evaluator is None:
        raise ValueError(f"unknown evaluator for scoreable task type {task_type!r}")
    evaluator_id = evaluator.__name__
    source = inspect.getsource(evaluator).encode("utf-8")
    return evaluator_id, hashlib.sha256(evaluator_id.encode("utf-8") + b"\n" + source).hexdigest()


def _arm_contract_hash(arm: str) -> str:
    """Return the locked semantic arm hash for canonical arms only."""
    return ARM_CONTRACTS[arm]["contract_sha256"] if arm in ARM_CONTRACTS else ""
