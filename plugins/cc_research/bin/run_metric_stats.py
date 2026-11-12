#!/usr/bin/env python
"""run_metric_stats.py — repeated-measurement statistics and the noise-aware keep decision for research:run.

With ``metric_repeats`` > 1 in a program's ``## Config``, ``skills/run/SKILL.md`` measures the metric k times per
iteration — R4 for the baseline (iteration 0), Phase 5 for every later iteration — and stages each run as one line of
``<state_dir>/metric-runs.jsonl``. This script owns the arithmetic the skill must not do in prose:

``status``  — after each staged run, report whether the iteration needs another run, failed, or is done (mean and
              sample sigma). The ledger, never memory, is the cursor, so a compaction mid-iteration resumes at the
              next run.
``decide``  — Phase 7: is the iteration's mean an improvement over ``best_metric`` beyond run-to-run noise?

An iteration's runs group into consecutive blocks of k, and the newest block is the current measurement: a Phase 7
guard rework amends the commit and re-measures it as a fresh block under the same iteration number, so the first
staged run of that block already reads as run 1 of k.

Noise model — the one ``fortify_metric_stats.py`` applies to ablations:
    sigma_delta    sqrt(sigma_new**2 / n_new + sigma_best**2 / n_best)
    threshold_pct  max(min_delta_pct, 2 * sigma_delta as percent of |best_metric|)
    improved       direction-signed gain, in percent of |best_metric|, strictly above threshold_pct
With ``best_metric == 0`` percentages are undefined: the gain must exceed ``2 * sigma_delta`` in metric units.

Every input comes from ``<state_dir>/state.json`` — ``config.metric_repeats`` (k), ``config.min_delta_pct``,
``config.metric.direction``, ``best_metric``, ``best_metric_sigma``, ``best_metric_runs`` — so the skill's blocks pass
only the run directory and the iteration number.

Usage:
    python run_metric_stats.py status <state_dir> --iteration I
    python run_metric_stats.py decide <state_dir> --iteration I

Stdout markers the skill branches on:
    ``RUN_METRIC_NEXT_RUN=<k>``  — run the metric again; k is the next 1-based run number
    ``RUN_METRIC_DONE=1 …``      — k runs recorded; runs, mean and sigma follow on the same line
    ``RUN_METRIC_FAILED=1 …``    — a run exited non-zero or printed no numeric metric
    ``RUN_METRIC_IMPROVED=1|0 …`` — decide's verdict, with mean, sigma, runs, delta and threshold

Exit codes:
    0 — a marker was printed
    1 — ``state.json`` unreadable or ``config.metric_repeats`` invalid; for ``decide``, ``best_metric`` unreadable or
        the iteration's runs incomplete or failed (``! BLOCKED`` printed)
    2 — argument error
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Directory of this script, added to ``sys.path`` so the sibling stats module imports under ``python -I`` too.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from fortify_metric_stats import RunStats, failed_run, summarise  # noqa: E402

#: Metric runs per measurement when ``config.metric_repeats`` is absent — one sample, the single-run keep rule.
DEFAULT_REPEATS = 1
#: Ceiling on ``config.metric_repeats``; each run may take the full Phase 5 verify timeout.
MAX_REPEATS = 10
#: Smallest gain, in percent of ``|best_metric|``, kept when ``config.min_delta_pct`` is absent.
DEFAULT_MIN_DELTA_PCT = 0.1
#: Run-state file and repeated-run ledger, both inside the run's state directory.
STATE_FILE = "state.json"
LEDGER_FILE = "metric-runs.jsonl"
#: Maximum bytes accepted from one run-state file or metric ledger.
MAX_JSONL_BYTES = 64 * 1024 * 1024


class Direction(str, Enum):
    """Which way the metric improves."""

    HIGHER = "higher"
    LOWER = "lower"


@dataclass(frozen=True)
class KeepConfig:
    """The ``state.json`` settings that shape repeated measurement and the keep decision."""

    repeats: int
    min_delta_pct: float
    direction: Direction


@dataclass(frozen=True)
class Decision:
    """Outcome of comparing an iteration's repeated runs against the best metric so far."""

    improved: bool
    delta: float
    threshold: float
    percent: bool


def _finite(value: object) -> float | None:
    """Return ``value`` as a finite float, or ``None`` for anything else (bools included).

    Examples:
        >>> _finite(1), _finite(True), _finite(float("nan")), _finite("1")
        (1.0, None, None, None)
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _read_limited_text(path: Path) -> str:
    """Read UTF-8 run input within the metric-ledger byte ceiling, rejecting oversized files."""
    with path.open("rb") as handle:
        raw = handle.read(MAX_JSONL_BYTES + 1)
    if len(raw) > MAX_JSONL_BYTES:
        raise ValueError(f"{path} exceeds {MAX_JSONL_BYTES} bytes")
    return raw.decode("utf-8")


def load_state(state_dir: Path) -> dict | None:
    """Read ``state.json`` from the run's state directory; ``None`` when unreadable or not a JSON object."""
    try:
        state = json.loads(_read_limited_text(state_dir / STATE_FILE))
    except (OSError, ValueError, ArithmeticError, RecursionError):
        return None
    return state if isinstance(state, dict) else None


def keep_config(state: dict) -> KeepConfig:
    """Extract the repeat count, minimum delta and direction from ``state["config"]``.

    Absent keys take the run defaults. An invalid ``metric_repeats`` raises rather than silently measuring once: R1
    validates it, so a bad value here means a hand-edited ``state.json``.

    Raises:
        ValueError: if ``metric_repeats`` is invalid or an explicit direction is neither ``higher`` nor ``lower``.

    Examples:
        >>> keep_config({"config": {"metric_repeats": 3, "metric": {"direction": "lower"}}})
        KeepConfig(repeats=3, min_delta_pct=0.1, direction=<Direction.LOWER: 'lower'>)
        >>> keep_config({}).repeats
        1
    """
    config = state.get("config")
    config = config if isinstance(config, dict) else {}
    repeats = config.get("metric_repeats", DEFAULT_REPEATS)
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= MAX_REPEATS:
        raise ValueError(f"config.metric_repeats must be an integer from 1 to {MAX_REPEATS}, got {repeats!r}")
    min_delta = _finite(config.get("min_delta_pct"))
    metric = config.get("metric")
    raw_direction = metric.get("direction", "higher") if isinstance(metric, dict) else "higher"
    try:
        direction = Direction(raw_direction)
    except ValueError:
        raise ValueError(f"config.metric.direction must be 'higher' or 'lower', got {raw_direction!r}") from None
    return KeepConfig(
        repeats=repeats,
        min_delta_pct=min_delta if min_delta is not None and min_delta >= 0 else DEFAULT_MIN_DELTA_PCT,
        direction=direction,
    )


def iteration_runs(state_dir: Path, iteration: int) -> list[dict]:
    """Return staged runs in append order, reporting the number of malformed lines skipped on stderr."""
    try:
        lines = _read_limited_text(state_dir / LEDGER_FILE).splitlines()
    except OSError:
        return []
    runs = []
    skipped = 0
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, ArithmeticError, RecursionError):
            skipped += 1
            continue
        number = _as_iteration(record.get("iteration")) if isinstance(record, dict) else None
        if number is None:
            skipped += 1
            continue
        if number == iteration:
            runs.append(record)
    if skipped:
        print(
            f"run_metric_stats: skipped {skipped} malformed ledger lines in {state_dir / LEDGER_FILE}", file=sys.stderr
        )
    return runs


def _as_iteration(value: object) -> int | None:
    """Read a staged record's ``iteration`` as an int, accepting the string form a hand-written line may carry.

    A model-written ``"iteration":"6"`` must still count as iteration 6 — otherwise no run ever matches and ``status``
    asks for run 1 forever.

    Examples:
        >>> _as_iteration(6), _as_iteration("6"), _as_iteration(6.0), _as_iteration(True), _as_iteration("x")
        (6, 6, 6, None, None)
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def current_block(records: list[dict], repeats: int) -> list[dict]:
    """Return the newest block of up to ``repeats`` runs — the measurement in progress or just finished.

    Examples:
        >>> [r["n"] for r in current_block([{"n": i} for i in range(1, 5)], 3)]
        [4]
        >>> [r["n"] for r in current_block([{"n": i} for i in range(1, 7)], 3)]
        [4, 5, 6]
        >>> current_block([], 3)
        []
    """
    if not records:
        return []
    return records[((len(records) - 1) // repeats) * repeats :]


def decide(new: RunStats, best: float, best_sigma: float, best_runs: int, config: KeepConfig) -> Decision:
    """Decide whether ``new`` beats ``best`` by more than the noise threshold.

    Args:
        new: Statistics of this iteration's runs.
        best: ``best_metric`` so far (mean of the runs that produced it).
        best_sigma: Sample sigma behind ``best``; 0 when it came from one run.
        best_runs: Number of runs behind ``best``.
        config: Direction and minimum delta.

    Returns:
        A :class:`Decision`; ``percent`` is ``False`` when ``best == 0`` and delta/threshold are in metric units.

    Examples:
        >>> cfg = KeepConfig(repeats=3, min_delta_pct=0.1, direction=Direction.HIGHER)
        >>> decide(RunStats(3, 101.0, 0.2), 100.0, 0.2, 3, cfg).improved
        True
        >>> decide(RunStats(3, 101.0, 2.0), 100.0, 2.0, 3, cfg).improved
        False
        >>> low = KeepConfig(repeats=3, min_delta_pct=0.1, direction=Direction.LOWER)
        >>> decide(RunStats(3, 90.0, 0.5), 100.0, 0.5, 3, low).improved
        True
    """
    if new.runs <= 0 or best_runs <= 0:
        raise ValueError("metric runs must be positive")
    sign = 1.0 if config.direction is Direction.HIGHER else -1.0
    gain = (new.mean - best) * sign
    sigma_delta = math.sqrt((new.sigma or 0.0) ** 2 / new.runs + best_sigma**2 / best_runs)
    if not math.isfinite(gain) or not math.isfinite(2 * sigma_delta):
        raise ValueError("non-finite metric gain or noise threshold")
    if best == 0:
        return Decision(improved=gain > 2 * sigma_delta, delta=gain, threshold=2 * sigma_delta, percent=False)
    scale = abs(best) / 100.0
    if scale == 0:
        raise ValueError("best_metric is too small for a percentage comparison")
    threshold = max(config.min_delta_pct, 2 * sigma_delta / scale)
    gain_pct = gain / scale
    if not math.isfinite(threshold) or not math.isfinite(gain_pct):
        raise ValueError("non-finite metric percentage comparison")
    return Decision(improved=gain_pct > threshold, delta=gain_pct, threshold=threshold, percent=True)


def _values(block: list[dict]) -> list[float]:
    """Return the numeric metric values of a block of runs."""
    return [value for value in (_finite(record.get("metric")) for record in block) if value is not None]


def _block_stats(block: list[dict]) -> RunStats:
    """Summarise a completed block, rejecting non-finite aggregates before a verdict is printed."""
    stats = summarise(_values(block))
    if not math.isfinite(stats.mean) or (stats.sigma is not None and not math.isfinite(stats.sigma)):
        raise ValueError("non-finite metric mean or sigma")
    return stats


def _format_sigma(stats: RunStats) -> str:
    """Render sigma for the terminal; ``n/a`` for a single run, where it is undefined."""
    return f"{stats.sigma:.6g}" if stats.sigma is not None else "n/a"


def _load(state_dir: Path) -> tuple[dict, KeepConfig] | None:
    """Load ``state.json`` and its keep settings; on failure print the BLOCKED reason and return ``None``."""
    state = load_state(state_dir)
    if state is None:
        print(f"! BLOCKED — {state_dir / STATE_FILE} unreadable; run R1 or --resume first")
        return None
    try:
        return state, keep_config(state)
    except ValueError as exc:
        print(f"! BLOCKED — {exc}")
        return None


def cmd_status(args: argparse.Namespace) -> int:
    """Report the iteration's repeated-run progress."""
    state_dir = Path(args.state_dir)
    loaded = _load(state_dir)
    if loaded is None:
        return 1
    config = loaded[1]
    block = current_block(iteration_runs(state_dir, args.iteration), config.repeats)
    failed = failed_run(block)
    if failed is not None:
        print(f"RUN_METRIC_FAILED=1 — run {failed} of iteration {args.iteration} exited non-zero or printed no metric")
        return 0
    if len(block) < config.repeats:
        print(
            f"RUN_METRIC_NEXT_RUN={len(block) + 1} — {len(block)}/{config.repeats} runs recorded "
            f"for iteration {args.iteration}"
        )
        return 0
    stats = _block_stats(block)
    print(f"RUN_METRIC_DONE=1 runs={stats.runs} mean={stats.mean:.6g} sigma={_format_sigma(stats)}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """Print whether the iteration's mean improves on ``best_metric`` beyond noise."""
    state_dir = Path(args.state_dir)
    loaded = _load(state_dir)
    if loaded is None:
        return 1
    state, config = loaded
    block = current_block(iteration_runs(state_dir, args.iteration), config.repeats)
    if len(block) < config.repeats or failed_run(block) is not None:
        print(
            f"! BLOCKED — iteration {args.iteration} has no {config.repeats} complete metric runs; "
            "finish Phase 5 (status prints RUN_METRIC_DONE) first"
        )
        return 1
    best = _finite(state.get("best_metric"))
    if best is None:
        print(f"! BLOCKED — best_metric unreadable in {state_dir / STATE_FILE}; R4 baseline missing")
        return 1
    best_runs = state.get("best_metric_runs")
    best_runs = best_runs if isinstance(best_runs, int) and not isinstance(best_runs, bool) and best_runs > 0 else 1
    new = _block_stats(block)
    verdict = decide(new, best, _finite(state.get("best_metric_sigma")) or 0.0, best_runs, config)
    unit, note = ("%", "") if verdict.percent else ("", " (metric units — best_metric is 0)")
    print(
        f"RUN_METRIC_IMPROVED={int(verdict.improved)} mean={new.mean:.6g} sigma={_format_sigma(new)} "
        f"runs={new.runs} delta={verdict.delta:.4g}{unit} threshold={verdict.threshold:.4g}{unit}{note}"
    )
    return 0


def _iteration_arg(raw: str) -> int:
    """Parse ``--iteration``: a non-negative integer (0 is the R4 baseline)."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--iteration must be an integer, got {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"--iteration must be >= 0, got {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    """Dispatch the ``status`` or ``decide`` subcommand.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        The subcommand's exit code.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(prog="run_metric_stats.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, text in (
        ("status", cmd_status, "report repeated-run progress for one iteration"),
        ("decide", cmd_decide, "decide whether the iteration improves on best_metric beyond noise"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("state_dir")
        command.add_argument("--iteration", type=_iteration_arg, required=True)
        command.set_defaults(handler=handler)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    try:
        return args.handler(args)
    except (ValueError, ArithmeticError) as exc:
        print(f"! BLOCKED — invalid metric data: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
