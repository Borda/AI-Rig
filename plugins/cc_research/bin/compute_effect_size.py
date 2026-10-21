#!/usr/bin/env python
"""compute_effect_size.py — rank-biserial correlation effect size for Wilcoxon results.

Reads a single JSON object from stdin (the output of ``retro_analyze.py``) and prints
the rank-biserial correlation ``r`` derived from the Wilcoxon signed-rank ``statistic``
and sample size ``n``. Preserves the exact output contract of the original inline block
in ``plugins/cc_research/skills/retro/SKILL.md`` (Step T2):

    r = 4 * statistic / (n * (n + 1)) - 1

Output:
    - ``r`` as a Python ``float`` ``str`` on a single line (e.g. ``0.5``).
    - Empty line when ``statistic`` is ``None`` (insufficient data or scipy missing).

Usage:
    echo "$RETRO_RESULT" | python "${CLAUDE_PLUGIN_ROOT}/bin/compute_effect_size.py"

Input contract (JSON on stdin):
    {"n": <int>, "statistic": <float | null>, ...other keys ignored...}

Exit codes:
    0   success (effect size printed, or empty line for None statistic).
    2   input error (malformed JSON, missing ``n``, non-numeric values, stdin over ``MAX_STDIN_CHARS``).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

#: Most characters read from stdin; the input is one ``retro_analyze.py`` JSON line, far below this.
MAX_STDIN_CHARS = 1 << 20


def rank_biserial_r(statistic: float, n: int) -> float:
    """Compute rank-biserial correlation ``r`` from a Wilcoxon signed-rank statistic.

    Formula: ``r = 4 * statistic / (n * (n + 1)) - 1``.

    Interpretation (per retro SKILL.md): ``|r| < 0.3`` small, ``0.3-0.5`` medium,
    ``> 0.5`` large.

    Args:
        statistic: Wilcoxon signed-rank ``W`` statistic.
        n: Number of paired samples used in the test (must be positive).

    Returns:
        Rank-biserial correlation in ``[-1, 1]``.

    Raises:
        ValueError: if ``n <= 0``.

    Examples:
        >>> rank_biserial_r(0.0, 8)
        -1.0
        >>> rank_biserial_r(36.0, 8)
        1.0
        >>> round(rank_biserial_r(27.0, 8), 4)
        0.5
    """
    if n <= 0:
        raise ValueError(f"n must be positive, got: {n}")
    return 4 * statistic / (n * (n + 1)) - 1


def compute_from_payload(payload: dict[str, Any]) -> str:
    """Derive the printed effect-size line from a parsed retro_analyze payload.

    Mirrors the original inline block: empty string for missing/None statistic,
    otherwise ``str()`` of the computed ``r`` (same shape Python ``print`` emits).

    Args:
        payload: Parsed JSON dict; must contain key ``n`` (int) and optionally
            ``statistic`` (numeric or ``None``).

    Returns:
        Line to print to stdout — either ``""`` or ``str(r)``.

    Raises:
        ValueError: if ``n`` is missing, non-integer, or ``statistic`` is non-numeric.

    Examples:
        >>> compute_from_payload({"n": 8, "statistic": None})
        ''
        >>> compute_from_payload({"n": 8, "statistic": 36.0})
        '1.0'
        >>> compute_from_payload({"n": 3, "statistic": None, "reason": "insufficient"})
        ''
    """
    if "n" not in payload:
        raise ValueError("missing required key 'n' in input JSON")
    n = payload["n"]
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError(f"'n' must be int, got: {type(n).__name__}")
    statistic = payload.get("statistic")
    if statistic is None:
        return ""
    if not isinstance(statistic, int | float) or isinstance(statistic, bool):
        raise ValueError(f"'statistic' must be numeric or null, got: {type(statistic).__name__}")
    return str(rank_biserial_r(float(statistic), n))


def main(argv: list[str] | None = None) -> int:
    """Read a Wilcoxon result from stdin and print its effect size.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``); only ``-h/--help`` is honored.

    Returns:
        Process exit code (0 on success, 2 on input error).
    """
    parser = argparse.ArgumentParser(
        prog="compute_effect_size.py",
        description="Rank-biserial correlation effect size for Wilcoxon results (reads JSON from stdin).",
    )
    parser.parse_args(argv)  # no positional/flag args — enables -h/--help, rejects stray tokens (exit 2)
    # bounded read: one extra char tells an oversized input from one that exactly fills the cap
    raw = sys.stdin.read(MAX_STDIN_CHARS + 1)
    if len(raw) > MAX_STDIN_CHARS:
        print(
            f"error: stdin exceeds {MAX_STDIN_CHARS} characters — expected one retro_analyze JSON line", file=sys.stderr
        )
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: malformed JSON on stdin: {exc.msg}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print(f"error: expected JSON object on stdin, got: {type(payload).__name__}", file=sys.stderr)
        return 2
    try:
        line = compute_from_payload(payload)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
