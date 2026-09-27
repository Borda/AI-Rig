"""Console detection, progress narration, and scenario JSONL emission."""

from __future__ import annotations

import json
import sys
from typing import Any


from _bench_common.presentation import benchmark_console, make_progress

from _bench_query.models import ScenarioResult


try:
    # benchmark_console imports rich lazily, so a missing rich still raises ImportError here and
    # the runner keeps its plain-stderr fallback. Sharing the console is what puts this runner's
    # progress bar and messages at the same width as every other benchmark surface.
    _console: Any | None = benchmark_console()
    _IS_RICH_AVAILABLE = True
except ImportError:
    _console = None
    _IS_RICH_AVAILABLE = False


# ---- OUTPUT HELPERS ----


class _OutputState:
    """Module-level output-verbosity flag (mirrors the module-level ``_console``).

    ``quiet`` is set to ``True`` by :func:`main` in ``--json-only`` mode so that human progress narration via
    :func:`log` is suppressed and stdout carries only the scenario JSONL and the final summary envelope.
    """

    quiet: bool = False


_OUT = _OutputState()


def emit(result: ScenarioResult) -> None:
    """Print one compact JSON line describing a single scenario result to stdout.

    Emits the dataclass fields (scenario/name/suite/passed/result/threshold/notes)
    for ``--json-only`` mode (one JSON object per line).

    Args:
        result: The :class:`ScenarioResult` to serialize.

    Examples:
        >>> import io, json, contextlib
        >>> r = ScenarioResult("C1", "coverage-gap", "calls", True, {"coverage_gap": 0.5}, {})
        >>> buf = io.StringIO()
        >>> with contextlib.redirect_stdout(buf):
        ...     emit(r)
        >>> json.loads(buf.getvalue())["scenario"]
        'C1'
    """
    line = {
        "scenario": result.scenario,
        "name": result.name,
        "suite": result.suite,
        "passed": result.passed,
        "result": result.result,
        "threshold": result.threshold,
        "notes": result.notes,
    }
    print(json.dumps(line, separators=(",", ":"), default=str))


def log(msg: str) -> None:
    """Print a progress message to the rich console or stderr.

    Suppressed entirely when :data:`_OUT` is in quiet (``--json-only``) mode so
    that stdout is not polluted for machine consumers.

    Args:
        msg: Message string to display.
    """
    if _OUT.quiet:
        return
    if _IS_RICH_AVAILABLE and _console is not None:
        # soft_wrap keeps a message with a long path on one line, so the rich branch says exactly
        # what the plain-stderr fallback below says instead of folding at the framing width.
        _console.print(msg, soft_wrap=True)
    else:
        print(msg, file=sys.stderr)


def _run_all_suites(suites: list[tuple[str, object]], use_progress: bool) -> list[ScenarioResult]:
    """Run every suite callable, optionally under a rich progress bar.

    Args:
        suites: List of ``(label, zero-arg callable returning list[ScenarioResult])``.
        use_progress: When True and rich is available, render a progress bar.

    Returns:
        Flattened list of all scenario results across every suite.
    """
    all_results: list[ScenarioResult] = []
    if use_progress and _IS_RICH_AVAILABLE and _console is not None:
        with make_progress(_console) as progress:
            bar = progress.add_task("Benchmark", total=len(suites))
            for label, run_fn in suites:
                progress.update(bar, description=label)
                all_results.extend(run_fn())  # type: ignore[operator]
                progress.advance(bar)
    else:
        for _label, run_fn in suites:
            all_results.extend(run_fn())  # type: ignore[operator]
    return all_results
