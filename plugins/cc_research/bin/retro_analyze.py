#!/usr/bin/env python
"""retro_analyze.py — one-sample signed-rank significance test for retro analysis.

Reads an experiments JSONL file produced by /research:run, extracts metric values
for kept iterations, and tests whether they differ from a single baseline metric.

This is a **one-sample** Wilcoxon signed-rank test (a signed-rank sign test of
"kept iterations vs the baseline constant"), NOT a paired test: the run records a
single baseline metric, so there is no per-iteration matched baseline to pair
against. The baseline scalar is repeated across the kept iterations and passed to
``scipy.stats.wilcoxon``, which reduces to the one-sample location test. Interpret
results accordingly — the framing is "are kept iterations located above/below the
baseline?", not "is each iteration paired-superior to its own baseline?".

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/retro_analyze.py" \\
        --jsonl <path>.jsonl \\
        --baseline <label> \\
        [--alpha 0.05] \\
        [--direction higher|lower] \\
        [--timeout 30]

Inputs:
    JSONL file with one record per iteration. Each record must contain:
        - ``status``: ``"baseline"`` for iteration 0, ``"kept"`` / ``"reverted"`` / etc.
        - ``metric``: numeric metric value for the iteration.

Output (stdout):
    Single line of JSON:
        {"significant": bool, "p_value": float, "statistic": float, "n": int}

    Additional keys on partial / failure paths:
        - ``"error"`` (str) on input error (exit 2).
        - ``"reason"`` (str) when N < 6 or scipy unavailable.

Exit codes:
    0   improvement is statistically significant at the given alpha.
    1   not significant (or insufficient data / scipy unavailable).
    2   input error (missing file, malformed JSON, no baseline record).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

MIN_SAMPLES_FOR_TEST: Final[int] = 6


@dataclass(frozen=True, slots=True)
class WilcoxonResult:
    """Outcome of one signed-rank comparison of candidate scores against a baseline.

    ``reason`` is set only when no test could be run — too few paired samples, or
    scipy missing — in which case ``p_value`` and ``statistic`` stay ``None``.

    Examples:
        >>> WilcoxonResult(significant=False, p_value=None, statistic=None, n=3, reason="too few").as_payload()
        {'significant': False, 'p_value': None, 'statistic': None, 'n': 3, 'reason': 'too few'}
        >>> WilcoxonResult(significant=True, p_value=0.01, statistic=2.0, n=8).as_payload()
        {'significant': True, 'p_value': 0.01, 'statistic': 2.0, 'n': 8}
    """

    significant: bool
    p_value: float | None
    statistic: float | None
    n: int
    reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """Return the stdout JSON mapping, omitting ``reason`` when no reason applies.

        The published CLI contract (module docstring, ``retro/SKILL.md``) lists ``reason`` as an extra key on the
        partial paths only, so a conclusive run must not emit ``"reason": null``.
        """
        payload = dataclasses.asdict(self)
        if self.reason is None:
            del payload["reason"]
        return payload


class Direction(str, Enum):
    """Which way a metric has to move to count as an improvement.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``. Values match the
    ``metric.direction`` field stored in a run's ``state.json`` and the ``--direction`` CLI flag.
    """

    HIGHER = "higher"
    LOWER = "lower"


def _validate_jsonl_path(raw: str) -> Path | None:
    """Resolve and validate that ``raw`` stays within a safe base directory.

    Permitted base directories (any one is sufficient):
      * The current working directory (treated as the project root)
      * The project's ``.experiments`` subdirectory
      * ``~/.claude/projects`` (per-project session data; narrower than the
        full ``~/.claude`` tree — )
      * The OS temporary directory — needed for pytest ``tmp_path`` runs.

    Args:
        raw: Raw value from ``--jsonl``.

    Returns:
        Resolved ``Path`` when the file exists and is within an allowed root;
        ``None`` when the file is missing or outside every allowed root.
    """
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_file():
        return None
    project_root = Path.cwd().resolve()
    allowed_roots = [
        project_root,
        (project_root / ".experiments").resolve(),
        (Path(os.path.expanduser("~")) / ".claude" / "projects").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    for root in allowed_roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return candidate
    return None


def run_wilcoxon(
    baseline_scores: list[float],
    candidate_scores: list[float],
    alpha: float = 0.05,
    direction: Direction | str = Direction.HIGHER,
) -> WilcoxonResult:
    """Run a Wilcoxon signed-rank test comparing candidate scores to baseline scores.

    Compares candidate scores against ``baseline_scores`` element-wise (same index).
    When ``baseline_scores`` is a repeated constant (the retro CLI case — a single
    baseline metric repeated across kept iterations), this reduces to a **one-sample**
    signed-rank test of "candidates differ from the baseline constant", not a paired
    test with per-iteration matched baselines. Uses the one-sided alternative matching
    ``direction``:

    - ``Direction.HIGHER`` → ``alternative="greater"`` (improvement = candidate > baseline)
    - ``Direction.LOWER`` → ``alternative="less"`` (improvement = candidate < baseline)

    Args:
        baseline_scores: Baseline metric values; must equal length of ``candidate_scores``.
        candidate_scores: Candidate metric values (e.g. all kept iterations).
        alpha: Significance threshold (typically 0.05).
        direction: A :class:`Direction` member, or its plain value when the caller
            forwards a raw CLI/JSON string.

    Returns:
        A :class:`WilcoxonResult`. On insufficient data or missing scipy, its ``p_value``
        and ``statistic`` are ``None`` and ``reason`` explains which case applied.

    Raises:
        ValueError: if ``direction`` is not a legal :class:`Direction` value or the score
            lengths differ.

    Examples:
        >>> # Insufficient data (< 6 paired samples) returns a reason rather than a p-value.
        >>> out = run_wilcoxon([1.0, 1.0, 1.0], [2.0, 2.0, 2.0], alpha=0.05, direction=Direction.HIGHER)
        >>> out.significant
        False
        >>> out.n
        3
        >>> out.reason is not None
        True
        >>> # Bad direction raises.
        >>> run_wilcoxon([1.0], [2.0], direction="sideways")
        Traceback (most recent call last):
            ...
        ValueError: direction must be 'higher' or 'lower', got: 'sideways'
    """
    try:
        direction = Direction(direction)
    except ValueError:
        legal = " or ".join(repr(d.value) for d in Direction)
        raise ValueError(f"direction must be {legal}, got: {direction!r}") from None
    if len(baseline_scores) != len(candidate_scores):
        raise ValueError(
            f"baseline_scores ({len(baseline_scores)}) and candidate_scores "
            f"({len(candidate_scores)}) must have the same length"
        )

    n = len(candidate_scores)
    if n < MIN_SAMPLES_FOR_TEST:
        return WilcoxonResult(
            significant=False,
            p_value=None,
            statistic=None,
            n=n,
            reason=f"insufficient data for significance testing (N={n}, minimum {MIN_SAMPLES_FOR_TEST} required)",
        )

    try:
        from scipy.stats import wilcoxon
    except ImportError:
        return WilcoxonResult(
            significant=False,
            p_value=None,
            statistic=None,
            n=n,
            reason="scipy not installed — run: pip install scipy",
        )

    alternative = "greater" if direction == Direction.HIGHER else "less"
    result = wilcoxon(candidate_scores, baseline_scores, alternative=alternative)
    statistic = float(result.statistic)
    p_value = float(result.pvalue)
    return WilcoxonResult(
        significant=p_value < alpha,
        p_value=p_value,
        statistic=statistic,
        n=n,
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file; return list of parsed records.

    Tolerates a trailing truncated line by silently skipping it (matches retro SKILL.md behaviour). Raises
    ``ValueError`` on the first non-final malformed line.
    """
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            records.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            if idx == len(lines) - 1:
                # Trailing truncated line — tolerate.
                break
            raise ValueError(f"malformed JSON at line {idx + 1}: {exc.msg}") from exc
    return records


def _extract_scores(records: list[dict[str, Any]], baseline_label: str) -> tuple[float, list[float]]:
    """Return (baseline_metric, candidate_metrics) parsed from records.

    Baseline record is the first entry with ``status == baseline_label`` (default label per retro SKILL.md is
    ``"baseline"``). Candidate records are every entry with ``status == "kept"``. Records lacking a numeric ``metric``
    are skipped.
    """
    baseline_metric: float | None = None
    candidates: list[float] = []
    for rec in records:
        status = rec.get("status")
        metric = rec.get("metric")
        if not isinstance(metric, int | float):
            continue
        if baseline_metric is None and status == baseline_label:
            baseline_metric = float(metric)
            continue
        if status == "kept":
            candidates.append(float(metric))
    if baseline_metric is None:
        raise ValueError(f"no baseline record found (looking for status == {baseline_label!r})")
    return baseline_metric, candidates


def main(argv: list[str] | None = None) -> int:
    """Run retrospective analysis from command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="retro_analyze",
        description="Wilcoxon signed-rank significance test for /research:retro JSONL output.",
    )
    parser.add_argument(
        "--jsonl",
        required=True,
        metavar="PATH",
        help="Path to experiments.jsonl produced by /research:run.",
    )
    parser.add_argument(
        "--baseline",
        default="baseline",
        metavar="LABEL",
        help="Status label that marks the baseline record (default: 'baseline').",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.05,
        help="Significance threshold (default: 0.05).",
    )
    parser.add_argument(
        "--direction",
        choices=[d.value for d in Direction],
        default=Direction.HIGHER.value,
        help="Improvement direction for the metric (default: higher).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Max wall-clock seconds (advisory; default: 30). Currently unused: the "
        "test is pure-CPU and bounded by N — kept for API parity with bash callers.",
    )
    args = parser.parse_args(argv)

    path = _validate_jsonl_path(args.jsonl)
    if path is None:
        print(
            json.dumps(
                {
                    "error": (
                        f"jsonl not found or outside allowed roots (project root, "
                        f".experiments/, ~/.claude/projects, tempdir): {args.jsonl}"
                    )
                }
            )
        )
        return 2

    try:
        records = _load_jsonl(path)
        baseline_metric, candidate_scores = _extract_scores(records, args.baseline)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    # One-sample framing: the run has a single baseline metric, so we repeat it and
    # test whether the kept iterations are located above/below that constant. This is a
    # one-sample signed-rank test, not a paired Wilcoxon (no per-iteration matched
    # baseline exists in the JSONL). See module docstring.
    baseline_repeated = [baseline_metric] * len(candidate_scores)
    result = run_wilcoxon(
        baseline_repeated,
        candidate_scores,
        alpha=args.alpha,
        direction=Direction(args.direction),
    )
    print(json.dumps(result.as_payload()))
    return 0 if result.significant else 1


if __name__ == "__main__":
    sys.exit(main())
