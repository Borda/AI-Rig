#!/usr/bin/env python3
"""Extract vitality score variables as shell assignments or private session files.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/extract_vitality_vars.py" [--keep-state PREFIX] SCORES_FILE

Replaces 45+ separate ``python -c`` subprocess calls per oss:analyse vitality
invocation (one call per axis × 5 fields + 6 top-level fields) with a single
JSON pass.  Emits newline-separated shell-quoted ``VAR=value`` assignments
suitable for legacy shell consumers. With ``--keep-state PREFIX``, stdout stays empty and each value is written to
``<TMPDIR>/<prefix>-<lowercase-hyphenated-variable>-<CSID>`` using the plugin's private sentinel writer. Empty values
are written too, so ``IFS= read -r`` clears stale shell variables. ``TMPDIR`` falls back to the platform temp directory;
``CSID`` falls back to ``CLAUDE_CODE_SESSION_ID``, then ``shared``.

Variables emitted:
    ANALYSIS_NOW, OVERALL_CONFIDENCE, HEALTH_SCORE_PCT, AXIS3_202_PENDING,
    TOTAL_PASSES, CONFIDENCE_HISTORY, AXIS{1-9}_SCORE, AXIS{1-9}_CONF,
    AXIS{1-9}_STATUS, AXIS{1-9}_SIGNAL, WEIGHT_{1-9}

Exit codes:
    0 — on success
    1 — missing argument, I/O error, JSON parse error, or invalid state identity
    2 — CLI usage error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from _sentinel_io import write_private_text

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
        >>> import os, tempfile, pathlib
        >>> fd, name = tempfile.mkstemp(suffix=".txt")
        >>> os.close(fd)
        >>> tmp = pathlib.Path(name)
        >>> _ = tmp.write_text("hello")
        >>> _read_text_guarded(tmp)
        'hello'
        >>> tmp.unlink()
    """
    if path.stat().st_size > _MAX_FILE_SIZE:
        raise ValueError(f"File too large ({path.stat().st_size} bytes): {path}")
    return path.read_text()


def extract_vars(scores: dict) -> dict[str, str]:
    """Extract shell variable values from an assembled vitality scores dict.

    Args:
        scores: Dict as produced by ``assemble_vitality_scores.py``.

    Returns:
        Ordered dict mapping shell variable names to string values.

    Examples:
        >>> d = {
        ...     "analysis_now": "1234567890",
        ...     "overall_confidence": 0.85,
        ...     "health_score_pct": 72.3,
        ...     "axis3_202_pending": False,
        ...     "total_passes": 1,
        ...     "confidence_history": "0.85",
        ...     "axes": {"1": {"score": 7.5, "conf": 0.9, "label": "🟢", "signal": "ok"}},
        ...     "weights": {"1": 0.17},
        ... }
        >>> v = extract_vars(d)
        >>> v["ANALYSIS_NOW"]
        '1234567890'
        >>> v["HEALTH_SCORE_PCT"]
        '72.3'
        >>> v["AXIS3_202_PENDING"]
        'false'
        >>> v["AXIS1_SCORE"]
        '7.5'
        >>> v["WEIGHT_1"]
        '17'
    """
    result: dict[str, str] = {
        "ANALYSIS_NOW": str(scores.get("analysis_now", "")),
        "OVERALL_CONFIDENCE": str(scores.get("overall_confidence", "0.0")),
        "HEALTH_SCORE_PCT": str(scores.get("health_score_pct", "0")),
        "AXIS3_202_PENDING": "true" if scores.get("axis3_202_pending") else "false",
        "TOTAL_PASSES": str(scores.get("total_passes", 1)),
        "CONFIDENCE_HISTORY": str(
            scores.get(
                "confidence_history",
                str(scores.get("overall_confidence", "0.0")),
            )
        ),
    }

    axes = scores.get("axes", {})
    weights = scores.get("weights", {})

    for n in range(1, 10):
        k = str(n)
        axis = axes.get(k, {})
        score = axis.get("score")
        result[f"AXIS{n}_SCORE"] = str(score) if score is not None else "-1"
        result[f"AXIS{n}_CONF"] = str(axis.get("conf", -1))
        result[f"AXIS{n}_STATUS"] = str(axis.get("label", "⚪"))
        result[f"AXIS{n}_SIGNAL"] = str(axis.get("signal", ""))
        result[f"WEIGHT_{n}"] = str(_weight_pct(weights.get(k, 0)))

    return result


def _weight_pct(raw: object) -> int:
    """Convert an axis weight fraction to a whole percent; a malformed weight counts as 0.

    Examples:
        >>> _weight_pct(0.17), _weight_pct("0.2"), _weight_pct("n/a"), _weight_pct(None)
        (17, 20, 0, 0)
    """
    try:
        return round(float(raw) * 100)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def emit(variables: dict[str, str]) -> str:
    """Render a variable dict as newline-separated shell-quoted ``VAR=value`` assignments.

    Args:
        variables: Mapping of shell variable name to string value.

    Returns:
        Multi-line string safe for ``eval``.

    Examples:
        >>> print(emit({"FOO": "bar", "BAZ": "hello world"}))
        FOO=bar
        BAZ='hello world'
    """
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in variables.items())


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to sys.argv[1:].

    Returns:
        Exit code: 0 on success, 1 on error; argparse exits 2 on ``-h``/unknown flag.

    Examples:
        No doctest — requires on-disk fixture; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="extract_vitality_vars.py",
        description="Emit shell variable assignments from a vitality scores JSON.",
    )
    # nargs="*" preserves the legacy exit-1-on-missing-arg contract.
    parser.add_argument("--keep-state", metavar="PREFIX", help="Write private session values instead of assignments.")
    parser.add_argument("paths", nargs="*", help="SCORES_FILE (1 path).")
    args = parser.parse_args(argv)

    if not args.paths:
        print("Usage: extract_vitality_vars.py SCORES_FILE", file=sys.stderr)
        return 1

    scores_file = Path(args.paths[0])
    try:
        scores = json.loads(_read_text_guarded(scores_file))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"Error reading {scores_file}: {e}", file=sys.stderr)
        return 1

    variables = extract_vars(scores)
    if args.keep_state is not None:
        try:
            _write_state(args.keep_state, variables)
        except (OSError, ValueError) as exc:
            print(f"extract_vitality_vars: cannot write state: {exc}", file=sys.stderr)
            return 1
    else:
        print(emit(variables))
    return 0


def _write_state(prefix: str, variables: dict[str, str]) -> None:
    """Persist literal values under validated session filenames, including empty values to replace stale state."""
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or ".." in value for value in (prefix, csid)):
        raise ValueError("--keep-state prefix and CSID must be plain filename components without '..'")
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    tmpdir = Path(os.environ.get("TMPDIR") or tmp_fallback)
    for key, value in variables.items():
        suffix = key.lower().replace("_", "-")
        # one value per line: the caller's `IFS= read -r` keeps only the first line, so fold a multi-line signal
        write_private_text(tmpdir / f"{prefix}-{suffix}-{csid}", " ".join(value.splitlines()) + "\n")


if __name__ == "__main__":
    sys.exit(main())
