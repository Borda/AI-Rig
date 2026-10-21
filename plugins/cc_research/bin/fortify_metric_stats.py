#!/usr/bin/env python
"""fortify_metric_stats.py — repeated-run statistics and noise-aware classification for fortify ablations.

A single metric run per variant cannot tell a real component effect from run-to-run noise, so
``skills/fortify/SKILL.md`` measures every variant N times. This script owns the arithmetic the
skill must not do in prose:

``status``  — after each staged metric run is appended to ``<fortify_dir>/metric-runs.jsonl``,
              report whether the current variant needs another run, failed, or is done (mean and
              sample sigma). For the ``full`` variant it also applies the full-first divergence
              gate against the source run's ``best_metric``.
``rank``    — after the loop, compute every completed ablation's delta from ``full`` and classify
              it against ``max(noise floor, 2 * sigma_delta)``; writes ``results-deltas.jsonl``.

Statistics:
    mean        arithmetic mean of the first N recorded runs
    sigma       sample standard deviation (Bessel, n - 1); undefined for n = 1, treated as 0
    sigma_delta standard error of the difference of two means,
                ``sqrt(sigma_full**2 / n_full + sigma_ablated**2 / n_ablated)``
    threshold   ``max(noise_floor_pct, 2 * sigma_delta_pct)``, in percent of ``|full mean|``

The full-variant divergence tolerance uses ``2 * sigma_full`` (not the standard error): ``best_metric``
is one draw from the source run, so its own noise is a whole sigma, not sigma / sqrt(N).

Usage:
    python fortify_metric_stats.py status <fortify_dir> --variant <name> --runs N [--state-json PATH]
        [--divergence-pct 2.0]
    python fortify_metric_stats.py rank <fortify_dir> --runs N [--program-file PATH] [--noise-floor-pct 2.0]

Stdout markers ``status`` prints (the skill branches on them):
    ``FORTIFY_METRIC_NEXT_RUN=<k>``  — run 4d again; k is the next 1-based run number
    ``FORTIFY_METRIC_DONE=1``        — N runs recorded; mean and sigma follow on the same line
    ``FORTIFY_METRIC_FAILED=1``      — a run exited non-zero or printed no numeric metric
    ``FORTIFY_FULL_DIVERGED=1``      — full mean diverges from best_metric beyond tolerance; abort

Exit codes:
    0 — status reported (any marker except FORTIFY_FULL_DIVERGED), or ranking written
    1 — full variant diverged or its best_metric is unreadable (status); full variant missing,
        not completed, or without runs (rank); unwritable output
    2 — argument error
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Default percent tolerance between the full variant and the source run's best_metric.
DEFAULT_DIVERGENCE_PCT = 2.0
#: Default noise floor in percent of |full mean|; a delta at or below it never counts as an effect.
DEFAULT_NOISE_FLOOR_PCT = 2.0
#: Importance class boundaries, percent of |full mean| (SKILL.md constants block).
CRITICAL_PCT = 50.0
SIGNIFICANT_PCT = 10.0
HARMFUL_PCT = -5.0
#: Ceiling on runs per variant — each run may take the full 4d timeout.
MAX_RUNS = 10

_METRIC_SECTION_RE = re.compile(r"^##\s+Metric\b(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
_DIRECTION_RE = re.compile(r"^\s*[-*]?\s*direction\s*:\s*(higher|lower)\b", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class RunStats:
    """Mean and sample sigma of one variant's recorded metric runs."""

    runs: int
    mean: float
    sigma: float | None


def bare_variant(name: str) -> str:
    """Return a variant name without its ``variant-`` prefix, so both spellings compare equal.

    Examples:
        >>> bare_variant("variant-no-dropout")
        'no-dropout'
        >>> bare_variant("full")
        'full'
    """
    return name[len("variant-") :] if name.startswith("variant-") else name


def summarise(values: list[float]) -> RunStats:
    """Compute mean and sample standard deviation (n - 1) of metric values.

    Args:
        values: At least one finite metric value.

    Returns:
        A :class:`RunStats`; ``sigma`` is ``None`` for a single value, where it is undefined.

    Raises:
        ValueError: if ``values`` is empty.

    Examples:
        >>> summarise([1.0, 2.0, 3.0])
        RunStats(runs=3, mean=2.0, sigma=1.0)
        >>> summarise([0.5])
        RunStats(runs=1, mean=0.5, sigma=None)
    """
    if not values:
        raise ValueError("summarise needs at least one value")
    count = len(values)
    mean = sum(values) / count
    if count < 2:
        return RunStats(runs=count, mean=mean, sigma=None)
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    return RunStats(runs=count, mean=mean, sigma=math.sqrt(variance))


def _read_jsonl(path: Path) -> list[dict]:
    """Return every JSON object line of ``path``; unreadable files and malformed lines are skipped."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _finite(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None`` for anything else (bools included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def variant_runs(fortify_dir: Path, variant: str) -> list[dict]:
    """Return the recorded metric runs of one variant, in append order."""
    wanted = bare_variant(variant)
    return [
        record
        for record in _read_jsonl(fortify_dir / "metric-runs.jsonl")
        if bare_variant(str(record.get("variant", ""))) == wanted
    ]


def failed_run(records: list[dict]) -> int | None:
    """Return the 1-based index of the first run that exited non-zero or printed no metric, else ``None``."""
    for position, record in enumerate(records, start=1):
        if record.get("exit", 0) != 0 or _finite(record.get("metric")) is None:
            return position
    return None


def best_metric(state_json: Path) -> float | None:
    """Read ``best_metric`` from a source-run ``state.json``; ``None`` when absent or non-numeric."""
    try:
        state = json.loads(state_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _finite(state.get("best_metric")) if isinstance(state, dict) else None


def divergence(full: RunStats, best: float, tolerance_pct: float) -> tuple[bool, float, float]:
    """Decide whether the full variant fails to reproduce the source run's best metric.

    Args:
        full: Statistics of the full variant's runs.
        best: ``best_metric`` from the source run.
        tolerance_pct: Fixed tolerance in percent of ``|best|``.

    Returns:
        ``(diverged, gap_pct, allowed_pct)``. With ``best == 0`` percentages are undefined, so both
        are reported in metric units and only the ``2 * sigma`` term applies.

    Examples:
        >>> divergence(RunStats(3, 99.0, 0.5), 100.0, 2.0)
        (False, 1.0, 2.0)
        >>> divergence(RunStats(3, 90.0, 0.5), 100.0, 2.0)
        (True, 10.0, 2.0)
        >>> divergence(RunStats(3, 90.0, 6.0), 100.0, 2.0)
        (False, 10.0, 12.0)
    """
    sigma = full.sigma or 0.0
    gap = abs(full.mean - best)
    if best == 0:
        return gap > 2 * sigma, gap, 2 * sigma
    scale = abs(best) / 100.0
    allowed_pct = max(tolerance_pct, 2 * sigma / scale)
    gap_pct = gap / scale
    return gap_pct > allowed_pct, gap_pct, allowed_pct


def classify(importance_pct: float, threshold_pct: float) -> str:
    """Map a signed importance to its class, treating anything within the noise threshold as BORDERLINE.

    Args:
        importance_pct: Positive when removing the component hurt the metric (component helpful).
        threshold_pct: ``max(noise floor, 2 * sigma_delta)`` in percent of ``|full mean|``.

    Returns:
        One of ``BORDERLINE``, ``CRITICAL``, ``SIGNIFICANT``, ``MARGINAL``, ``NEUTRAL``, ``HARMFUL``.

    Examples:
        >>> classify(1.5, 2.0)
        'BORDERLINE'
        >>> classify(60.0, 2.0), classify(20.0, 2.0), classify(4.0, 2.0)
        ('CRITICAL', 'SIGNIFICANT', 'MARGINAL')
        >>> classify(-3.0, 2.0), classify(-8.0, 2.0)
        ('NEUTRAL', 'HARMFUL')
    """
    if abs(importance_pct) <= threshold_pct:
        return "BORDERLINE"
    if importance_pct > CRITICAL_PCT:
        return "CRITICAL"
    if importance_pct >= SIGNIFICANT_PCT:
        return "SIGNIFICANT"
    if importance_pct > 0:
        return "MARGINAL"
    return "HARMFUL" if importance_pct < HARMFUL_PCT else "NEUTRAL"


def metric_direction(program_file: Path | None) -> str:
    """Read ``direction`` from the program file's ``## Metric`` section; ``higher`` when absent.

    Examples:
        >>> import tempfile, pathlib
        >>> program = pathlib.Path(tempfile.mkdtemp()) / "program.md"
        >>> _ = program.write_text("## Metric\\n```yaml\\ndirection: lower\\n```\\n## Guard\\n", encoding="utf-8")
        >>> metric_direction(program)
        'lower'
        >>> metric_direction(None)
        'higher'
    """
    if program_file is None:
        return "higher"
    try:
        text = program_file.read_text(encoding="utf-8")
    except OSError:
        return "higher"
    section = _METRIC_SECTION_RE.search(text)
    found = _DIRECTION_RE.search(section.group(1)) if section else None
    return found.group(1).lower() if found else "higher"


def _first_values(records: list[dict], runs: int) -> list[float]:
    """Return the metric values of the first ``runs`` records (extra runs from a resume are ignored)."""
    return [value for value in (_finite(record.get("metric")) for record in records[:runs]) if value is not None]


def _format_sigma(stats: RunStats) -> str:
    """Render sigma for the terminal, naming why it is missing for a single run."""
    return f"{stats.sigma:.6g}" if stats.sigma is not None else "n/a (1 run — threshold falls back to the noise floor)"


def _full_gate(stats: RunStats, state_json: Path, tolerance_pct: float) -> int:
    """Apply the full-first divergence gate; return the exit code for ``status``."""
    best = best_metric(state_json)
    if best is None:
        print(f"! BLOCKED — best_metric unreadable in {state_json}; cannot check that full reproduces the source run")
        return 1
    diverged, gap, allowed = divergence(stats, best, tolerance_pct)
    unit = "" if best == 0 else "%"
    if not diverged:
        print(
            f"full-first gate: PASS — full mean {stats.mean:.6g} vs best_metric {best:.6g} ({gap:.3g}{unit} ≤ {allowed:.3g}{unit})"
        )
        return 0
    print("FORTIFY_FULL_DIVERGED=1")
    print(
        f"! BLOCKED — full variant mean {stats.mean:.6g} diverges from best_metric {best:.6g} by {gap:.3g}{unit} "
        f"(tolerance {allowed:.3g}{unit}) — worktree does not reproduce the source run; halting F4"
    )
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Report the current variant's run progress; gate the full variant against best_metric."""
    records = variant_runs(Path(args.fortify_dir), args.variant)
    failed = failed_run(records[: args.runs])
    if failed is not None:
        print(f"FORTIFY_METRIC_FAILED=1 — run {failed} of {args.variant} exited non-zero or printed no numeric metric")
        return 0
    if len(records) < args.runs:
        print(
            f"FORTIFY_METRIC_NEXT_RUN={len(records) + 1} — {len(records)}/{args.runs} runs recorded for {args.variant}"
        )
        return 0
    stats = summarise(_first_values(records, args.runs))
    print(f"FORTIFY_METRIC_DONE=1 runs={stats.runs} mean={stats.mean:.6g} sigma={_format_sigma(stats)}")
    if bare_variant(args.variant) == "full" and args.state_json:
        return _full_gate(stats, Path(args.state_json), args.divergence_pct)
    return 0


def _last_status_by_variant(fortify_dir: Path) -> dict[str, str]:
    """Return the last recorded ``status`` per bare variant name in ``results.jsonl``."""
    statuses: dict[str, str] = {}
    for record in _read_jsonl(fortify_dir / "results.jsonl"):
        statuses[bare_variant(str(record.get("variant", "")))] = str(record.get("status", ""))
    return statuses


def _delta_row(name: str, ablated: RunStats, full: RunStats, sign: int, floor_pct: float) -> dict:
    """Build one ``results-deltas.jsonl`` record for a completed ablation."""
    scale = abs(full.mean) / 100.0
    delta = ablated.mean - full.mean
    sigma_delta = math.sqrt((full.sigma or 0.0) ** 2 / full.runs + (ablated.sigma or 0.0) ** 2 / ablated.runs)
    importance = (full.mean - ablated.mean) * sign / scale if scale else 0.0
    sigma_delta_pct = sigma_delta / scale if scale else 0.0
    threshold = max(floor_pct, 2 * sigma_delta_pct)
    return {
        "variant": name,
        "runs": ablated.runs,
        "metric": ablated.mean,
        "sigma": ablated.sigma,
        "full_metric": full.mean,
        "full_sigma": full.sigma,
        "delta_from_full": delta,
        "delta_pct": delta / scale if scale else 0.0,
        "importance_pct": importance,
        "sigma_delta_pct": sigma_delta_pct,
        "threshold_pct": threshold,
        "class": classify(importance, threshold),
    }


def cmd_rank(args: argparse.Namespace) -> int:
    """Write ``results-deltas.jsonl``: per-ablation delta, noise threshold and class."""
    fortify_dir = Path(args.fortify_dir)
    statuses = _last_status_by_variant(fortify_dir)
    full_values = _first_values(variant_runs(fortify_dir, "full"), args.runs)
    if statuses.get("full") != "completed" or not full_values:
        print("! BLOCKED — full variant not completed or without recorded runs; cannot rank ablations")
        return 1
    full = summarise(full_values)
    sign = 1 if metric_direction(Path(args.program_file) if args.program_file else None) == "higher" else -1
    rows = []
    for name, status in statuses.items():
        if name == "full" or status != "completed":
            continue
        values = _first_values(variant_runs(fortify_dir, name), args.runs)
        if not values:
            print(f"⚠ {name}: completed but no metric runs recorded — excluded from ranking")
            continue
        rows.append(_delta_row(name, summarise(values), full, sign, args.noise_floor_pct))
    out = fortify_dir / "results-deltas.jsonl"
    try:
        out.write_text(
            "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8", newline="\n"
        )
    except OSError as exc:
        print(f"! BLOCKED — cannot write {out}: {exc}")
        return 1
    for row in rows:
        print(
            f"{row['variant']}: importance {row['importance_pct']:.3g}% (threshold {row['threshold_pct']:.3g}%) → {row['class']}"
        )
    print(f"ranked {len(rows)} ablation(s) → {out}")
    return 0


def _runs_arg(raw: str) -> int:
    """Parse ``--runs``: an integer from 1 to :data:`MAX_RUNS`."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--runs must be an integer, got {raw!r}") from None
    if not 1 <= value <= MAX_RUNS:
        raise argparse.ArgumentTypeError(f"--runs must be between 1 and {MAX_RUNS}, got {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    """Dispatch the ``status`` or ``rank`` subcommand.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The subcommand's exit code.
    """
    parser = argparse.ArgumentParser(prog="fortify_metric_stats.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="report run progress for one variant")
    status.add_argument("fortify_dir")
    status.add_argument("--variant", required=True)
    status.add_argument("--runs", type=_runs_arg, required=True)
    status.add_argument("--state-json", default="")
    status.add_argument("--divergence-pct", type=float, default=DEFAULT_DIVERGENCE_PCT)
    status.set_defaults(handler=cmd_status)
    rank = sub.add_parser("rank", help="classify completed ablations against the noise threshold")
    rank.add_argument("fortify_dir")
    rank.add_argument("--runs", type=_runs_arg, required=True)
    rank.add_argument("--program-file", default="")
    rank.add_argument("--noise-floor-pct", type=float, default=DEFAULT_NOISE_FLOOR_PCT)
    rank.set_defaults(handler=cmd_rank)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
