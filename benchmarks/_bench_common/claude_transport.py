"""Claude model configuration and stream-json subprocess transport."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:  # imported as a package member (benchmarks._bench_common.claude_transport)
    from .process_group import NEW_PROCESS_GROUP, terminate_process_group
except ImportError:  # loaded standalone by file path; the benchmarks dir is on sys.path
    from _bench_common.process_group import NEW_PROCESS_GROUP, terminate_process_group

# Short tier name → concrete model id, shared by the agentic and real-codebase runners.
MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
}

# Per-model wall-clock timeout (seconds). Opus needs more time for complex reasoning.
MODEL_TIMEOUT: dict[str, int] = {"haiku": 210, "sonnet": 420, "opus": 600}


@dataclass
class ResultUsage:
    """Token/cost fields parsed from a stream-json ``result`` event."""

    input_tokens: int = 0  # uncached input + cache_creation + cache_read (real context size)
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0  # Anthropic's total_cost_usd (current prices); 0.0 when absent
    subtype: str = ""  # e.g. "success", "error_max_turns", "error_non_zero_exit"
    success: bool = False


def parse_result_usage(event: dict) -> ResultUsage:
    """Parse the usage/cost fields from a stream-json ``result`` event.

    ``input_tokens`` in the event is only the uncached portion, so real context usage sums it
    with the cache-creation and cache-read parts. ``cost_usd`` is Anthropic's own per-run
    ``total_cost_usd`` (cache-aware, current prices), or ``0.0`` when the event omits it.

    Args:
        event: A decoded stream-json event with ``type == "result"``.

    Returns:
        A :class:`ResultUsage` with tokens, cost, subtype, and success flag.

    Examples:
        >>> ev = {"usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
        ...       "total_cost_usd": 0.25, "subtype": "success"}
        >>> u = parse_result_usage(ev)
        >>> (u.input_tokens, u.output_tokens, u.cost_usd, u.success)
        (100, 5, 0.25, True)
        >>> parse_result_usage({"subtype": "error_max_turns"}).success
        False
    """
    usage = event.get("usage", {})
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    subtype = event.get("subtype", "")
    return ResultUsage(
        input_tokens=usage.get("input_tokens", 0) + cache_creation + cache_read,
        output_tokens=usage.get("output_tokens", 0),
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost_usd=event.get("total_cost_usd", 0.0) or 0.0,
        subtype=subtype,
        success=subtype == "success",
    )


@dataclass
class StreamOutcome:
    """Result of :func:`stream_claude` — mechanics only; callers map to their own run dataclass."""

    elapsed_s: float = 0.0
    returncode: int | None = None  # process exit code (negative → killed by signal)
    stderr: str = ""  # captured stderr, only when the process was waited on cleanly
    error: str | None = None  # message from an unexpected exception (not a timeout)
    exc_timeout: bool = False  # True when proc.wait() raised TimeoutExpired


def stream_claude(
    cmd: list[str],
    *,
    timeout: float,
    cwd: Path | str,
    env: dict[str, str],
    on_event: Callable[[dict, float], None],
    update_fn: Callable[[float], None] | None = None,
) -> StreamOutcome:
    """Run a ``claude -p`` stream-json subprocess: kill-timer, line-by-line event parse, timing.

    Launches ``cmd``, arms a ``threading.Timer`` that kills the process at ``timeout`` seconds,
    reads stdout line-by-line decoding each JSON event and passing it to ``on_event(event, ts)``,
    and calls ``update_fn(elapsed_s)`` at most every 0.5 s. This is the shared measurement loop;
    all per-arm/\u200bper-dataclass scoring lives in the caller's ``on_event`` closure. The returned
    :class:`StreamOutcome` reports mechanics (elapsed, returncode, stderr, timeout) — the caller
    maps those onto its own run object, since the error-precedence and any ``incomplete`` flag
    differ per runner.

    Args:
        cmd: Full ``claude`` CLI command list.
        timeout: Wall-clock kill deadline in seconds.
        cwd: Working directory for the subprocess.
        env: Environment mapping for the subprocess.
        on_event: Called once per decoded stream-json event as ``(event, monotonic_ts)``.
        update_fn: Optional throttled progress callback ``(elapsed_seconds,)``; ≤ every 0.5 s.

    Returns:
        A :class:`StreamOutcome` describing how the process ended.
    """
    t0 = time.monotonic()
    outcome = StreamOutcome()
    last_update = 0.0
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            cwd=str(cwd),
            env=env,
            **NEW_PROCESS_GROUP,
        )
        # The kill must reach the whole tree: killing only the direct child left the
        # descendants it spawned alive, consuming paid budget outside the measured window.
        kill_timer = threading.Timer(timeout, terminate_process_group, args=(proc,))
        kill_timer.start()
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                ts = time.monotonic()
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                on_event(event, ts)
                if update_fn is not None and (ts - last_update) >= 0.5:
                    update_fn(ts - t0)
                    last_update = ts
            stderr_read = proc.stderr.read() if proc.stderr else ""
            proc.wait(timeout=10)
            outcome.stderr = stderr_read  # only reached when wait() did not raise
        finally:
            kill_timer.cancel()
        outcome.returncode = proc.returncode
    except subprocess.TimeoutExpired:
        if proc is not None:
            proc.kill()
        outcome.exc_timeout = True
    except Exception as exc:  # noqa: BLE001
        outcome.error = str(exc)[:300]
    finally:
        outcome.elapsed_s = time.monotonic() - t0
    return outcome
