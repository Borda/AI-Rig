"""The portable deadline primitives: mechanism selection, the supervised worker and the interrupt watchdog.

Cases where the watchdog really calls ``_thread.interrupt_main`` run in a fresh interpreter, so a missed conversion can
only fail that child, never interrupt the pytest session. Cases where no interrupt can be issued run in-process.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import codemap_py
import pytest
from codemap_py import _deadline
from codemap_py.query import cli as query_cli

_SRC = Path(codemap_py.__file__).resolve().parent.parent

_WATCHDOG_HARNESS = """\
import os, signal, sys, time
sys.path.insert(0, {src!r})
from codemap_py import _deadline

def busy(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass

{body}
"""


def _run_watchdog(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` after the harness prelude in a fresh interpreter and capture its output."""
    code = _WATCHDOG_HARNESS.format(src=str(_SRC), body=body)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False)


@pytest.fixture
def portable_deadline(monkeypatch: pytest.MonkeyPatch):
    """Yield an unarmed watchdog with the portable path selected, settled at teardown so no timer outlives it.

    The selector is injected rather than ``signal.SIGALRM`` deleted: the in-process test runner's own timeout plugin
    uses the real ``signal.SIGALRM``.
    """
    monkeypatch.setattr(_deadline, "select_mechanism", lambda: _deadline.DeadlineMechanism.PORTABLE)
    deadline = _deadline.InterruptDeadline()
    yield deadline
    deadline.run(lambda: None)


@pytest.mark.parametrize(
    ("host_signal", "expected"),
    [
        pytest.param(types.SimpleNamespace(SIGALRM=14), _deadline.DeadlineMechanism.ALARM, id="host-with-sigalrm"),
        pytest.param(types.SimpleNamespace(), _deadline.DeadlineMechanism.PORTABLE, id="host-without-sigalrm"),
    ],
)
def test_selector_follows_host_sigalrm_at_call_time(host_signal, expected, monkeypatch):
    """The selector reads the host's ``SIGALRM`` on every call, so a host without it gets the portable path.

    Both host shapes are supplied explicitly, so both cases run on every host, Windows included.
    """
    monkeypatch.setattr(_deadline, "signal", host_signal)

    assert _deadline.select_mechanism() is expected


class TestRunSupervised:
    """The parent-side deadline around a worker process."""

    def test_passes_worker_exit_status_through(self):
        """A worker finishing before its deadline hands its own exit status to the parent unchanged."""
        assert _deadline.run_supervised([sys.executable, "-c", "raise SystemExit(3)"], 30) == 3

    def test_stops_overdue_worker(self):
        """A worker still running at its deadline is stopped and reported as expired, well before it would finish."""
        started = time.monotonic()

        status = _deadline.run_supervised([sys.executable, "-c", "import time; time.sleep(60)"], 0.5, grace=1)

        assert status is None
        assert time.monotonic() - started < 10

    def test_worker_sees_marker_that_it_consumes(self):
        """The worker is marked as deadline-owned, and claiming the role removes the marker for its own children."""
        probe = (
            f"import os, sys; sys.path.insert(0, {str(_SRC)!r}); from codemap_py import _deadline; "
            "raise SystemExit(10 * _deadline.claim_worker_role() + (_deadline.WORKER_ENV in os.environ))"
        )

        assert _deadline.run_supervised([sys.executable, "-c", probe], 30) == 10


class TestInterruptWatchdog:
    """The in-process watchdog: flag first, then interrupt the main thread."""

    def test_expired_work_raises_deadline_expired(self):
        """Busy work past the deadline is interrupted and reported as ``DeadlineExpired`` with its seconds."""
        result = _run_watchdog(
            "deadline = _deadline.InterruptDeadline()\n"
            "assert deadline.arm(0.2)\n"
            "try:\n    deadline.run(lambda: busy(20))\n"
            "except _deadline.DeadlineExpired as exc:\n    print('expired', exc.seconds)\n"
        )

        assert (result.returncode, result.stdout.strip()) == (0, "expired 0.2"), result.stderr

    def test_early_completion_cancels_watchdog(self):
        """Work finishing first returns its value, stops the timer, and no interrupt arrives after the deadline."""
        result = _run_watchdog(
            "deadline = _deadline.InterruptDeadline()\n"
            "assert deadline.arm(0.3)\n"
            "value = deadline.run(lambda: 42)\n"
            "busy(0.8)\n"
            "print(value, deadline.armed, deadline.fired)\n"
        )

        assert (result.returncode, result.stdout.strip()) == (0, "42 False False"), result.stderr

    def test_real_ctrl_c_propagates_unchanged(self):
        """A KeyboardInterrupt raised while the deadline has not fired is a user interrupt, not a timeout."""
        result = _run_watchdog(
            "deadline = _deadline.InterruptDeadline()\n"
            "assert deadline.arm(30)\n"
            "def work():\n    raise KeyboardInterrupt\n"
            "try:\n    deadline.run(work)\n"
            "except _deadline.DeadlineExpired:\n    print('timeout')\n"
            "except KeyboardInterrupt:\n    print('interrupt', deadline.fired)\n"
        )

        assert (result.returncode, result.stdout.strip()) == (0, "interrupt False"), result.stderr

    def test_interrupt_pending_at_completion_is_drained(self):
        """An interrupt issued as the work returns is absorbed inside ``run`` and never reaches the caller later.

        The work fires the watchdog body itself on its last line, so the interrupt is pending exactly when the work
        completes; ``run`` must report the timeout and the caller's following busy loop must run uninterrupted.
        """
        result = _run_watchdog(
            "deadline = _deadline.InterruptDeadline()\n"
            "assert deadline.arm(30)\n"
            "try:\n    deadline.run(deadline._expire)\n"
            "except _deadline.DeadlineExpired:\n    print('expired')\n"
            "busy(0.5)\n"
            "print('caller-clean')\n"
        )

        assert (result.returncode, result.stdout.split()) == (0, ["expired", "caller-clean"]), result.stderr

    def test_owned_process_hard_exits_when_interrupt_is_ignored(self):
        """A main thread that never sees the interrupt is ended by the owner's hard-exit backstop after the grace.

        The work ignores SIGINT after arming, so the simulated interrupt does nothing; only the backstop, wired by an
        entry point that owns the process, can end the run, and it reports the configured seconds.
        """
        result = _run_watchdog(
            "def hard_exit(seconds):\n    print('hard', seconds, flush=True)\n    os._exit(2)\n"
            "deadline = _deadline.InterruptDeadline(hard_exit=hard_exit)\n"
            "assert deadline.arm(0.2, hard_exit_grace=0.3)\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "deadline.run(lambda: busy(20))\n"
            "print('not reached')\n"
        )

        assert (result.returncode, result.stdout.strip()) == (2, "hard 0.2"), result.stderr


def test_query_deadline_is_not_armed_off_the_main_thread(portable_deadline):
    """A query started on a worker thread arms no portable watchdog, exactly as the SIGALRM path refuses there."""
    args = argparse.Namespace(command="central", timeout=5)
    worker = threading.Thread(target=query_cli._start_query_timeout, args=(args, portable_deadline))

    worker.start()
    worker.join()

    assert not portable_deadline.armed


def test_query_deadline_arms_portable_watchdog_on_main_thread(portable_deadline):
    """On the main thread of a host without SIGALRM the query's ``--timeout`` arms the portable watchdog."""
    query_cli._start_query_timeout(argparse.Namespace(command="central", timeout=60), portable_deadline)

    assert portable_deadline.armed
