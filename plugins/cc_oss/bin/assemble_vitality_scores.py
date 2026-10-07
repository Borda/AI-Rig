#!/usr/bin/env python3
"""assemble_vitality_scores.py — merge 3 parallel axis-scoring partials into unified health score.

Reads three partial JSON files (one per oss:repo-warden axis group A/B/C), loads
axis weights from the vitality-scoring.md rubric, renormalizes weights for
unavailable axes (score==null or label==⚪), and writes the assembled result to
SCORES_FILE.  Extracted from oss:analyse vitality Step 3 inline python -c block.

Usage:
    assemble_vitality_scores.py PARTIAL_A PARTIAL_B PARTIAL_C SCORING_FILE SCORES_FILE

Exit codes:
    0 — on success
    1 — wrong argument count, I/O error, or JSON parse error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Axis identifiers — names mirror `_shared/vitality-scoring.md`'s Weights table.
#: Axis id for how quickly issues and pull requests get a first maintainer response.
RESPONSIVENESS = 1
#: Axis id for how recently and steadily the repository is maintained.
MAINTENANCE_ACTIVITY = 2
#: Axis id for the breadth and health of the contributor base.
CONTRIBUTOR_HEALTH = 3
#: Axis id for the health of the issue and pull-request backlog.
ISSUE_PR_HEALTH = 4
#: Axis id for CI/CD setup and code-quality signals.
CI_CD_CODE_QUALITY = 5
#: Axis id for documentation completeness.
DOCUMENTATION = 6
#: Axis id for governance files and project policies.
GOVERNANCE = 7
#: Axis id for security practices and posture.
SECURITY_POSTURE = 8
#: Axis id for the project's direction of travel over time.
TRAJECTORY = 9

#: Default weight of each vitality axis in the overall score, keyed by axis id; used when no weights file loads.
_DEFAULT_WEIGHTS: dict[int, float] = {
    RESPONSIVENESS: 0.10,
    MAINTENANCE_ACTIVITY: 0.08,
    CONTRIBUTOR_HEALTH: 0.10,
    ISSUE_PR_HEALTH: 0.07,
    CI_CD_CODE_QUALITY: 0.07,
    DOCUMENTATION: 0.05,
    GOVERNANCE: 0.06,
    SECURITY_POSTURE: 0.11,
    TRAJECTORY: 0.07,
}

_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB guard against runaway reads


def _read_text_guarded(path: Path) -> str:
    """Read text file after enforcing a 10 MB size cap.

    Args:
        path: Filesystem path to read.

    Returns:
        File contents as string.

    Raises:
        ValueError: when file exceeds ``_MAX_FILE_SIZE`` bytes.

    Examples:
        >>> import tempfile, pathlib
        >>> tmp = pathlib.Path(tempfile.mktemp(suffix=".txt"))
        >>> _ = tmp.write_text("hello")
        >>> _read_text_guarded(tmp)
        'hello'
        >>> tmp.unlink()
    """
    if path.stat().st_size > _MAX_FILE_SIZE:
        raise ValueError(f"File too large ({path.stat().st_size} bytes): {path}")
    return path.read_text()


def load_weights(scoring_file: Path) -> dict[int, float]:
    """Load per-axis weights from the vitality-scoring.md rubric table.

    Parses lines of the form ``| N  axis-name | 0.XX |``.  Falls back to
    ``_DEFAULT_WEIGHTS`` when the file is absent, is malformed (e.g. duplicate
    axis rows), or does not contain at least axes 1-9.

    Args:
        scoring_file: Path to the vitality-scoring.md rubric file.

    Returns:
        Dict mapping axis number (1–9) to float weight.

    Examples:
        >>> import tempfile, pathlib
        >>> tmp = pathlib.Path(tempfile.mktemp(suffix=".md"))
        >>> _ = tmp.write_text("| 1 foo | 0.17 |\\n| 2 bar | 0.18 |\\n")
        >>> w = load_weights(tmp)
        >>> len(w)  # only 2 entries → fallback
        9
        >>> tmp.unlink()
    """
    weights: dict[int, float] = {}
    seen: set[int] = set()
    malformed = False
    read_error: OSError | ValueError | None = None
    try:
        text = _read_text_guarded(scoring_file)
    except (OSError, ValueError) as exc:
        read_error = exc
        text = ""
    for line in text.splitlines():
        m = re.match(r"\|\s*(\d+)\s+[^|]+\|\s*(0\.\d+)\s*\|", line)
        if m:
            axis = int(m.group(1))
            if axis in seen:
                malformed = True
            seen.add(axis)
            weights[axis] = float(m.group(2))

    # Superset (not exact-set) guard — keeps axes 10-13 once real 13-axis rubric parses.
    if read_error is None and not malformed and set(weights) >= set(_DEFAULT_WEIGHTS):
        return weights

    if read_error is not None:
        reason = f"{scoring_file} unreadable ({read_error})"
    elif malformed:
        reason = f"{scoring_file} malformed (duplicate axis rows)"
    else:
        reason = f"{scoring_file} missing required axes 1-9 (found {sorted(weights)})"
    print(f"[vitality] WARN: load_weights falling back to _DEFAULT_WEIGHTS — {reason}", file=sys.stderr)
    return _DEFAULT_WEIGHTS.copy()


def assemble_scores(
    partial_a: Path,
    partial_b: Path,
    partial_c: Path,
    scoring_file: Path,
) -> dict:
    """Merge three axis-group partial JSON files into a unified vitality scores dict.

    Weights are renormalized over available axes only (unavailable = score is None
    or label is "⚪") so the health percentage remains meaningful even when data is
    missing for some axes.

    Args:
        partial_a: Group A partial JSON (axes 1, 2, 5, 6).
        partial_b: Group B partial JSON (axes 4, 7, 8).
        partial_c: Group C partial JSON (axes 3 → 9).
        scoring_file: vitality-scoring.md rubric for weight table.

    Returns:
        Assembled scores dict suitable for JSON serialization.

    Examples:
        No doctest — requires on-disk JSON fixtures; covered by pytest.
    """
    weights = load_weights(scoring_file)

    axes: dict[str, dict] = {}
    for path in (partial_a, partial_b, partial_c):
        d = json.loads(_read_text_guarded(path))
        axes.update(d["axes"])

    available = {k: v for k, v in axes.items() if v.get("label") != "⚪" and v.get("score") is not None}
    total_w = sum(weights[int(k)] for k in available)
    health = sum(weights[int(k)] * v["score"] / 10.0 for k, v in available.items()) / total_w * 100 if total_w else 0.0

    conf_vals = [v["conf"] for v in available.values() if v.get("conf", 0) > 0]
    overall_conf = sum(conf_vals) / len(conf_vals) if conf_vals else 0.0

    partial_c_data = json.loads(_read_text_guarded(partial_c))
    partial_a_data = json.loads(_read_text_guarded(partial_a))

    return {
        "analysis_now": partial_a_data["scored_at"],
        "health_score_pct": round(health, 1),
        "overall_confidence": round(overall_conf, 2),
        "axes": {str(k): v for k, v in axes.items()},
        "weights": {str(k): v for k, v in weights.items()},
        "axis3_weeks": partial_c_data.get("axis3_weeks"),
        "axis3_202_pending": any(
            axes.get(str(k), {}).get("label") == "⚪" and "stats" in axes.get(str(k), {}).get("unavailable_reason", "")
            for k in [3]
        ),
        "total_passes": 1,
        "confidence_history": str(round(overall_conf, 2)),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to sys.argv[1:].

    Returns:
        Exit code: 0 on success, 1 on error; argparse exits 2 on bad ``-h``/unknown flag.

    Examples:
        No doctest — requires on-disk fixtures; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="assemble_vitality_scores.py",
        description="Merge 3 parallel axis-scoring partials into a unified health score.",
    )
    # Positionals are optional (nargs="*") so the legacy wrong-count contract
    # (exit 1, not argparse's exit 2) is preserved by the explicit check below.
    parser.add_argument("paths", nargs="*", help="PARTIAL_A PARTIAL_B PARTIAL_C SCORING_FILE SCORES_FILE (5 paths).")
    args = parser.parse_args(argv)

    if len(args.paths) != 5:
        print(
            f"Usage: {parser.prog} PARTIAL_A PARTIAL_B PARTIAL_C SCORING_FILE SCORES_FILE",
            file=sys.stderr,
        )
        return 1

    partial_a, partial_b, partial_c, scoring_file, scores_file = (Path(a) for a in args.paths)

    try:
        result = assemble_scores(partial_a, partial_b, partial_c, scoring_file)
        scores_file.write_text(json.dumps(result, indent=2))
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"[vitality] assembled: health={result['health_score_pct']}% conf={result['overall_confidence']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
