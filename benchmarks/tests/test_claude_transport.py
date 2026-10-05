"""Tests for benchmarks/_bench_common/claude_transport.py.

Focus: the previously-uncovered hot path (``stream_claude`` + ``parse_result_usage``), which runs a live ``claude -p``
subprocess in production and so is exercised by no other unit test.
"""

from __future__ import annotations

from typing import Any

import pytest

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
            import subprocess

            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return self.returncode

    def kill(self) -> None:
        """Record that process termination was requested."""
        self.killed = True


def _patch_popen(monkeypatch: pytest.MonkeyPatch, module: Any, proc: Any) -> None:
    """Replace the transport's process factory with a supplied process double for the patch lifetime.

    >>> from types import SimpleNamespace
    >>> module = SimpleNamespace(subprocess=SimpleNamespace(Popen=None))
    >>> process = object()
    >>> with pytest.MonkeyPatch.context() as patch:
    ...     _patch_popen(patch, module, process)
    ...     module.subprocess.Popen(["unused"]) is process
    True
    >>> module.subprocess.Popen is None
    True
    """
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: proc)


# ---------------------------------------------------------------------------
# parse_result_usage
# ---------------------------------------------------------------------------


class TestParseResultUsage:
    """Sum cached input parts, capture cost + success from a result event."""

    def test_sums_cache_parts_into_input(self, script_claude_stream: Any) -> None:
        """Combine every input-token category and recognize a successful result event."""
        ev = {
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 70,
                "output_tokens": 5,
            },
            "total_cost_usd": 0.5,
            "subtype": "success",
        }
        u = script_claude_stream.parse_result_usage(ev)
        assert (u.input_tokens, u.output_tokens, u.cache_creation_tokens, u.cache_read_tokens) == (100, 5, 20, 70)
        assert u.cost_usd == 0.5
        assert u.success is True

    def test_missing_cost_and_error_subtype(self, script_claude_stream: Any) -> None:
        """Absent total_cost_usd → 0.0; non-'success' subtype → success False, subtype preserved."""
        u = script_claude_stream.parse_result_usage({"subtype": "error_max_turns"})
        assert u.cost_usd == 0.0
        assert u.success is False
        assert u.subtype == "error_max_turns"


# ---------------------------------------------------------------------------
# stream_claude
# ---------------------------------------------------------------------------


class TestStreamClaude:
    """Route decoded events, skip blanks/garbage, report mechanics via StreamOutcome."""

    def test_routes_valid_events_and_captures_outcome(
        self, script_claude_stream: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank and non-JSON lines are skipped; valid events reach on_event; returncode+stderr captured."""
        lines = ['{"type":"a"}\n', "\n", "not json\n", '{"type":"result","subtype":"success"}\n']
        proc = _FakeProc(lines, stderr="warn", returncode=0)
        _patch_popen(monkeypatch, script_claude_stream, proc)

        seen: list[dict] = []
        outcome = script_claude_stream.stream_claude(
            ["claude"], timeout=99, cwd=".", env={}, on_event=lambda e, ts: seen.append(e)
        )
        assert [e["type"] for e in seen] == ["a", "result"]
        assert outcome.returncode == 0
        assert outcome.stderr == "warn"
        assert outcome.exc_timeout is False
        assert outcome.error is None
        assert outcome.elapsed_s >= 0.0

    def test_negative_returncode_surfaced(self, script_claude_stream: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A signal-killed process (returncode < 0) is surfaced for the caller's timeout mapping."""
        proc = _FakeProc(['{"type":"x"}\n'], returncode=-9)
        _patch_popen(monkeypatch, script_claude_stream, proc)
        outcome = script_claude_stream.stream_claude(
            ["claude"], timeout=99, cwd=".", env={}, on_event=lambda e, ts: None
        )
        assert outcome.returncode == -9
        assert outcome.exc_timeout is False

    def test_wait_timeout_sets_exc_timeout_and_no_stderr(
        self, script_claude_stream: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When wait() raises TimeoutExpired the process is killed, exc_timeout set, stderr left empty."""
        proc = _FakeProc(['{"type":"x"}\n'], stderr="ignored", wait_raises=True)
        _patch_popen(monkeypatch, script_claude_stream, proc)
        outcome = script_claude_stream.stream_claude(
            ["claude"], timeout=99, cwd=".", env={}, on_event=lambda e, ts: None
        )
        assert outcome.exc_timeout is True
        assert outcome.stderr == ""  # stderr only recorded on a clean wait
        assert proc.killed is True

    def test_popen_failure_recorded_as_error(self, script_claude_stream: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unexpected exception (e.g. Popen failure) is captured as outcome.error, not raised."""

        def _boom(*_a: Any, **_k: Any) -> None:
            """Simulate a missing executable at the process-launch boundary."""
            raise OSError("no such binary")

        monkeypatch.setattr(script_claude_stream.subprocess, "Popen", _boom)
        outcome = script_claude_stream.stream_claude(
            ["claude"], timeout=99, cwd=".", env={}, on_event=lambda e, ts: None
        )
        assert outcome.error is not None
        assert "no such binary" in outcome.error
        assert outcome.exc_timeout is False

    def test_update_fn_receives_elapsed(self, script_claude_stream: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """update_fn is invoked with an elapsed-seconds float (throttled ≤ every 0.5 s)."""
        proc = _FakeProc(['{"type":"a"}\n'], returncode=0)
        _patch_popen(monkeypatch, script_claude_stream, proc)
        calls: list[float] = []
        script_claude_stream.stream_claude(
            ["claude"], timeout=99, cwd=".", env={}, on_event=lambda e, ts: None, update_fn=calls.append
        )
        # First event is >0.5 s from the sentinel 0.0 baseline, so exactly one throttled call fires.
        assert len(calls) == 1
        assert isinstance(calls[0], float)
