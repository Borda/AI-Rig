#!/usr/bin/env python
"""Detect dead windows and suspicious improvements in a sanitized experiment history.

Purpose:
    make retrospective window and jump arithmetic repeatable.

Scope:
    read the T1 sanitized ledger without modifying experiment state.

Usage:
    ``python retro_scan.py --jsonl <path> --out-dir <retro-dir> [--threshold N] [--direction higher|lower]``.

Outputs:
    ``dead-iters.json`` and ``suspicious-jumps.json`` beneath the supplied output directory.

Failure:
    exit 2 with a diagnostic for invalid inputs or failed writes; exit 0 after both outputs exist.

Used by:
    research:retro T3 and T4. Uses the same bounded JSONL reader as its significance analysis.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

from retro_analyze import _load_jsonl

DEFAULT_THRESHOLD = 0.001


def _number(value: object) -> float:
    """Require a finite numeric value rather than accepting booleans or numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("metric and delta values must be finite numbers or null deltas")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("metric and delta values must be finite numbers")
    return number


def _iterations(records: list[dict]) -> list[dict]:
    """Validate chronological iteration records and omit their baseline row."""
    iterations = []
    previous = -1
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("each ledger row must be an object")
        iteration = record.get("iteration")
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration <= previous:
            raise ValueError("iteration numbers must be nonnegative integers in increasing order")
        previous = iteration
        if record.get("delta") is not None:
            _number(record["delta"])
        if iteration != 0 and record.get("status") != "baseline":
            iterations.append(record)
    return iterations


def _delta(record: dict, direction: str) -> float | None:
    """Exclude timeout and potentially unmeasured reverted improvements from valid deltas."""
    value = record.get("delta")
    if value is None or record.get("status") == "timeout":
        return None
    delta = _number(value)
    improved = delta > 0 if direction == "higher" else delta < 0
    if record.get("status") == "reverted" and improved:
        return None
    return delta


def _window(records: list[dict]) -> dict:
    """Describe one contiguous dead window, preserving the kept-only plateau distinction."""
    return {
        "start": records[0]["iteration"],
        "end": records[-1]["iteration"],
        "count": len(records),
        "type": "dead-plateau" if all(record.get("status") == "kept" for record in records) else "dead-churn",
    }


def dead_windows(records: list[dict], threshold: float, direction: str) -> dict:
    """Group three or more adjacent null or sub-threshold deltas into dead windows."""
    windows = []
    pending: list[dict] = []
    for record in records:
        delta = _delta(record, direction)
        dead = delta is None or abs(delta) < threshold
        adjacent = not pending or record["iteration"] == pending[-1]["iteration"] + 1
        if not dead or not adjacent:
            if len(pending) >= 3:
                windows.append(_window(pending))
            pending = []
        if dead:
            pending.append(record)
    if len(pending) >= 3:
        windows.append(_window(pending))
    total = sum(window["count"] for window in windows)
    return {
        "windows": windows,
        "total_dead": total,
        "total_iterations": len(records),
        "dead_pct": 100 * total / len(records) if records else 0.0,
        "threshold": threshold,
    }


def suspicious_jumps(records: list[dict], direction: str) -> dict:
    """Compare each kept improvement against the previous five kept absolute deltas."""
    kept = [record for record in records if record.get("status") == "kept" and record.get("delta") is not None]
    jumps = []
    for index in range(5, len(kept)):
        record = kept[index]
        delta = _number(record["delta"])
        if delta <= 0 if direction == "higher" else delta >= 0:
            continue
        previous = [abs(_number(item["delta"])) for item in kept[index - 5 : index]]
        mean, std = statistics.mean(previous), statistics.stdev(previous)
        if abs(delta) <= mean + 2 * std:
            continue
        jumps.append(
            {
                "iteration": record["iteration"],
                "delta": delta,
                "sigma": (abs(delta) - mean) / std if std else None,
                "severity": "HIGH" if abs(delta) > mean + 3 * std else "MEDIUM",
                "commit": record.get("commit"),
                "files": record.get("files", []),
                "label": "suspicious — investigate",
            }
        )
    result = {"jumps": jumps, "kept_samples": len(kept)}
    if len(kept) < 6:
        result["reason"] = f"⚠ Insufficient data for trend analysis (need ≥6 data points, have {len(kept)})"
    return result


def _threshold(records: list[dict], requested: float | None) -> float:
    """Apply metric-scale advice only when the user did not supply a threshold."""
    threshold = DEFAULT_THRESHOLD if requested is None else requested
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("threshold must be finite and greater than zero")
    baseline = next((_number(record.get("metric")) for record in records if record.get("status") == "baseline"), None)
    if baseline is None:
        raise ValueError("no baseline record found")
    if baseline > 100 * threshold:
        advisory = baseline * 0.0001
        print(
            f"! Threshold advisory: baseline_metric={baseline:g}; consider --threshold {advisory:.4f}", file=sys.stderr
        )
        if requested is None:
            threshold = advisory
    return threshold


def main(argv: list[str] | None = None) -> int:
    """Write deterministic retrospective summaries from the sanitized history."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jsonl", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--direction", choices=("higher", "lower"), default="higher")
    args = parser.parse_args(argv)
    try:
        records = _load_jsonl(args.jsonl)
        iterations = _iterations(records)
        threshold = _threshold(records, args.threshold)
        dead = dead_windows(iterations, threshold, args.direction)
        jumps = suspicious_jumps(iterations, args.direction)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in (("dead-iters.json", dead), ("suspicious-jumps.json", jumps)):
            (args.out_dir / name).write_text(
                json.dumps(payload, allow_nan=False) + "\n", encoding="utf-8", newline="\n"
            )
    except (OSError, ValueError, ArithmeticError, RecursionError) as exc:
        print(f"retro_scan: {exc}", file=sys.stderr)
        return 2
    print(f"dead_iterations={dead['total_dead']} suspicious_jumps={len(jumps['jumps'])} threshold={threshold:g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
