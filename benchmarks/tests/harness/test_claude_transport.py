"""Tests for benchmarks/_bench_common/claude_transport.py.

Focus: the previously-uncovered hot path (``stream_claude`` + ``parse_result_usage``), which runs a live ``claude -p``
subprocess in production and so is exercised by no other unit test. The process boundary is replaced by a small double,
so no test here launches ``claude``.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest import mock

# ---------------------------------------------------------------------------
# Fake subprocess plumbing for stream_claude
# ---------------------------------------------------------------------------


class _FakeStderr:
    def __init__(self, data: str) -> None:
        """Initialize the test double's fixture-controlled state."""
        self._data = data

    def read(self) -> str:
        """Return the stored stderr text without consuming a real stream."""
        return self._data


class _FakeProc:
    """Minimal stand-in for subprocess.Popen used by stream_claude."""

    def __init__(self, lines: list[str], *, stderr: str = "", returncode: int = 0, wait_raises: bool = False) -> None:
        """Initialize the test double's fixture-controlled state."""
        self.stdout = iter(lines)
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode
        self._wait_raises = wait_raises
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        """Return the configured exit code or simulate the configured process timeout."""
        if self._wait_raises:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return self.returncode

    def kill(self) -> None:
        """Record that process termination was requested."""
        self.killed = True


def _stream(module: Any, **overrides: Any) -> Any:
    """Call ``stream_claude`` with inert defaults so a test names only the argument it varies.

    >>> from types import SimpleNamespace
    >>> module = SimpleNamespace(stream_claude=lambda *args, **kwargs: (args, kwargs["timeout"]))
    >>> _stream(module, timeout=5)
    ((['claude'],), 5)
    """
    kwargs = {"timeout": 99, "cwd": ".", "env": {}, "on_event": lambda event, ts: None, **overrides}
    return module.stream_claude(["claude"], **kwargs)


# ---------------------------------------------------------------------------
# parse_result_usage
# ---------------------------------------------------------------------------


class TestParseResultUsage:
    """Sum cached input parts, capture cost + success from a result event."""

    def test_sums_cache_parts_into_input(self, script_claude_stream: Any) -> None:
        """Every input-token category is folded into input_tokens and a success event is recognized.

        A result event reports fresh, cache-creation and cache-read tokens separately. Cost accounting needs the gross
        input, so the three must add up while the cache parts stay individually visible.
        """
        event = {
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 70,
                "output_tokens": 5,
            },
            "total_cost_usd": 0.5,
            "subtype": "success",
        }
        usage = script_claude_stream.parse_result_usage(event)
        assert (usage.input_tokens, usage.output_tokens, usage.cache_creation_tokens, usage.cache_read_tokens) == (
            100,
            5,
            20,
            70,
        )
        assert usage.cost_usd == 0.5
        assert usage.success is True

    def test_defaults_missing_cost_and_flags_error_subtype(self, script_claude_stream: Any) -> None:
        """Absent total_cost_usd becomes 0.0 and a non-success subtype is kept and reported as failure.

        An aborted run (for example ``error_max_turns``) carries no cost field; it must still parse, and the caller must
        be able to tell it apart from a successful run.
        """
        usage = script_claude_stream.parse_result_usage({"subtype": "error_max_turns"})
        assert usage.cost_usd == 0.0
        assert usage.success is False
        assert usage.subtype == "error_max_turns"


# ---------------------------------------------------------------------------
# stream_claude
# ---------------------------------------------------------------------------


class TestStreamClaude:
    """Route decoded events, skip blanks/garbage, report mechanics via StreamOutcome."""

    @mock.patch("subprocess.Popen", autospec=True)
    def test_routes_valid_events_and_captures_outcome(self, popen: mock.Mock, script_claude_stream: Any) -> None:
        """Blank and non-JSON lines are skipped, valid events reach on_event, returncode and stderr are captured.

        The stream mixes real events with a blank line and a garbage line, as a CLI interleaving diagnostics would. Only
        decodable events may reach the callback, and the outcome must describe a clean exit.
        """
        popen.return_value = _FakeProc(
            ['{"type":"a"}\n', "\n", "not json\n", '{"type":"result","subtype":"success"}\n'], stderr="warn"
        )
        seen: list[dict] = []

        outcome = _stream(script_claude_stream, on_event=lambda event, ts: seen.append(event))

        assert [event["type"] for event in seen] == ["a", "result"]
        assert outcome.returncode == 0
        assert outcome.stderr == "warn"
        assert outcome.exc_timeout is False
        assert outcome.error is None
        assert outcome.elapsed_s >= 0.0

    @mock.patch("subprocess.Popen", autospec=True)
    def test_surfaces_negative_returncode(self, popen: mock.Mock, script_claude_stream: Any) -> None:
        """A signal-killed process (returncode < 0) is surfaced for the caller's timeout mapping.

        A negative status is how a SIGKILLed child reports; the transport must pass it through untouched instead of
        treating it as a transport-level timeout.
        """
        popen.return_value = _FakeProc(['{"type":"x"}\n'], returncode=-9)

        outcome = _stream(script_claude_stream)

        assert outcome.returncode == -9
        assert outcome.exc_timeout is False

    @mock.patch("subprocess.Popen", autospec=True)
    def test_wait_timeout_kills_process_and_drops_stderr(self, popen: mock.Mock, script_claude_stream: Any) -> None:
        """When wait() raises TimeoutExpired the process is killed, exc_timeout is set and stderr stays empty.

        Stderr is only recorded after a clean wait, so a hung process must not leak a partial or blocking read.
        """
        popen.return_value = _FakeProc(['{"type":"x"}\n'], stderr="ignored", wait_raises=True)

        outcome = _stream(script_claude_stream)

        assert outcome.exc_timeout is True
        assert outcome.stderr == ""
        assert popen.return_value.killed is True

    @mock.patch("subprocess.Popen", autospec=True, side_effect=OSError("no such binary"))
    def test_popen_failure_recorded_as_error(self, popen: mock.Mock, script_claude_stream: Any) -> None:
        """An unexpected exception at launch is captured as outcome.error instead of being raised.

        A missing ``claude`` executable must degrade into a reportable outcome so one bad cell cannot abort a benchmark.
        """
        outcome = _stream(script_claude_stream)

        assert outcome.error is not None
        assert "no such binary" in outcome.error
        assert outcome.exc_timeout is False

    @mock.patch("subprocess.Popen", autospec=True)
    def test_update_fn_receives_elapsed(self, popen: mock.Mock, script_claude_stream: Any) -> None:
        """update_fn is invoked with an elapsed-seconds float, throttled to at most once every 0.5 s.

        The first event is more than 0.5 s from the sentinel 0.0 baseline, so exactly one throttled call fires.
        """
        popen.return_value = _FakeProc(['{"type":"a"}\n'])
        calls: list[float] = []

        _stream(script_claude_stream, update_fn=calls.append)

        assert len(calls) == 1
        assert isinstance(calls[0], float)
