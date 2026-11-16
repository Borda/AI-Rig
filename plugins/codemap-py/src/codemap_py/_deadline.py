"""Portable wall-clock deadlines for the index and query engines.

The engines historically enforced ``--timeout`` with ``SIGALRM``, which native Windows Python does not provide. This
module supplies the portable path while leaving the ``SIGALRM`` path to the engines unchanged:

* :func:`select_mechanism` picks ``SIGALRM`` whenever the host has it, evaluated at call time, so a Unix run never pays
  for a supervising process or a watchdog thread.
* Index (:func:`run_supervised`): a parent process runs the scan worker under ``subprocess`` wait with a timeout, then
  terminates and finally kills it. A killed worker cannot leave the shared-index gate wedged: the gate is OS-lock based
  and reclaims a dead writer's intent by liveness. The worker learns that its parent owns the deadline through
  :data:`WORKER_ENV`, which :func:`claim_worker_role` removes on entry so it never leaks to grandchildren.
* Query (:class:`InterruptDeadline`): the engine runs in-process (the ``codemap-py`` dispatcher calls it directly), so a
  watchdog thread first records that the deadline fired, then interrupts the main thread with
  :func:`_thread.interrupt_main`. :meth:`InterruptDeadline.run` turns that interrupt into :class:`DeadlineExpired`,
  while a real Ctrl-C stays a :class:`KeyboardInterrupt`. ``os._exit`` is reachable only through the optional
  ``hard_exit`` callback, which the caller supplies only when it owns the whole process.

Limits of the in-process path, by construction: the interrupt is checked between Python bytecodes, so a long C call
(for example ``json.loads`` of a huge index) finishes before the interrupt lands; a host that replaced Python's default
SIGINT handler gets no in-process deadline, because interrupting it would run the host's own handler.
"""

from __future__ import annotations

import _thread
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, MutableMapping, Sequence
from enum import Enum
from typing import TypeVar

#: Environment marker a supervising parent sets on its worker; the worker removes it on entry.
WORKER_ENV = "_CODEMAP_DEADLINE_WORKER"
#: Seconds a terminated worker gets to exit before it is killed.
KILL_GRACE_SECONDS = 2.0
#: Seconds the main thread gets to act on the watchdog's interrupt before an owned process exits hard.
HARD_EXIT_GRACE_SECONDS = 2.0
#: Upper bound for waiting on an interrupt that was issued just as the work finished.
_DRAIN_LIMIT_SECONDS = 1.0

_T = TypeVar("_T")


class DeadlineMechanism(str, Enum):
    """How a ``--timeout`` deadline is enforced on the current host."""

    ALARM = "alarm"
    PORTABLE = "portable"


class DeadlineExpired(Exception):
    """Raised by :meth:`InterruptDeadline.run` when the watchdog's deadline interrupted the work."""

    def __init__(self, seconds: float) -> None:
        """Keep the configured deadline so the caller can report it unchanged."""
        super().__init__(f"deadline of {seconds}s expired")
        self.seconds = seconds


def select_mechanism() -> DeadlineMechanism:
    """Choose ``SIGALRM`` when the host provides it, otherwise the portable path.

    Evaluated on every call (never cached at import), so a test can force the portable path on a Unix host by removing
    ``signal.SIGALRM`` for the duration of the test.

    Returns:
        :attr:`DeadlineMechanism.ALARM` on hosts with ``signal.SIGALRM``, else :attr:`DeadlineMechanism.PORTABLE`.

    Examples:
        >>> import signal
        >>> expected = DeadlineMechanism.ALARM if hasattr(signal, "SIGALRM") else DeadlineMechanism.PORTABLE
        >>> select_mechanism() is expected
        True
    """
    return DeadlineMechanism.ALARM if hasattr(signal, "SIGALRM") else DeadlineMechanism.PORTABLE


def claim_worker_role(environ: MutableMapping[str, str] | None = None) -> bool:
    """Report whether a supervising parent owns this process's deadline, consuming the marker.

    The marker is removed so processes this worker starts do not inherit a disabled deadline.

    Args:
        environ: Environment mapping to read and update; ``None`` means :data:`os.environ`.

    Returns:
        ``True`` when :data:`WORKER_ENV` was present.

    Examples:
        >>> env = {WORKER_ENV: "1", "PATH": "/bin"}
        >>> claim_worker_role(env), env
        (True, {'PATH': '/bin'})
        >>> claim_worker_role(env)
        False
    """
    target = os.environ if environ is None else environ
    return target.pop(WORKER_ENV, None) is not None


def run_supervised(command: Sequence[str], timeout: float, *, grace: float = KILL_GRACE_SECONDS) -> int | None:
    """Run a worker command with a wall-clock deadline that works on every host.

    The worker inherits stdin, stdout and stderr, so its output is the run's output. On expiry the worker is terminated,
    and killed if it is still alive after ``grace`` seconds. A Ctrl-C reaches the worker through the console as well;
    the worker gets ``grace`` seconds to finish its own handling before it is stopped, then the interrupt propagates.

    Args:
        command: Worker argv, executed without a shell.
        timeout: Deadline in seconds; must be positive.
        grace: Seconds between terminate and kill.

    Returns:
        The worker's exit status (a signal death is mapped to the shell's ``128 + signal`` form), or ``None`` when the
        deadline expired and the worker was stopped.

    Examples:
        >>> import sys
        >>> run_supervised([sys.executable, "-c", "raise SystemExit(3)"], 30)
        3
        >>> run_supervised([sys.executable, "-c", "import time; time.sleep(30)"], 0.2) is None
        True
    """
    process = subprocess.Popen(list(command), env={**os.environ, WORKER_ENV: "1"})  # noqa: S603 - argv list, no shell
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(process, grace)
        return None
    except KeyboardInterrupt:
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _stop(process, grace)
        raise
    return 128 - status if status < 0 else status


def _stop(process: subprocess.Popen, grace: float) -> None:
    """Terminate a worker, escalating to kill when it outlives the grace period."""
    process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class InterruptDeadline:
    """In-process deadline for hosts without ``SIGALRM``: a watchdog thread interrupts the main thread.

    Protocol: :meth:`arm` starts a daemon timer. When it expires, the watchdog takes the lock, records that it fired,
    and calls :func:`_thread.interrupt_main`; :meth:`run` settles under the same lock, so after settling either the
    watchdog never fired or its interrupt is already pending. A pending interrupt is drained inside :meth:`run`, so it
    can never surface later in the caller. A :class:`KeyboardInterrupt` that arrives without the fired flag is a real
    Ctrl-C and propagates unchanged.

    Examples:
        >>> deadline = InterruptDeadline()
        >>> deadline.run(lambda: 42)
        42
        >>> deadline.fired
        False
    """

    def __init__(self, *, hard_exit: Callable[[float], None] | None = None) -> None:
        """Create an unarmed deadline.

        Args:
            hard_exit: Called from the watchdog when the main thread has not settled ``HARD_EXIT_GRACE_SECONDS`` after
                the interrupt, with the configured seconds. Only a caller that owns the whole process may pass one
                (it is expected to record the outcome and ``os._exit``); embedded callers leave it ``None``.
        """
        self._lock = threading.Lock()
        self._hard_exit = hard_exit
        self._hard_exit_grace = HARD_EXIT_GRACE_SECONDS
        self._seconds: float = 0
        self._timer: threading.Timer | None = None
        self._backstop: threading.Timer | None = None
        self._fired = False
        self._settled = False
        self._delivered = False

    @property
    def fired(self) -> bool:
        """Whether the watchdog fired and interrupted the main thread."""
        return self._fired

    @property
    def armed(self) -> bool:
        """Whether a watchdog timer is waiting to fire."""
        return self._timer is not None and self._timer.is_alive()

    def arm(self, seconds: float, *, hard_exit_grace: float = HARD_EXIT_GRACE_SECONDS) -> bool:
        """Start the watchdog; refuse exactly where an interrupt could not be delivered safely.

        Arming is refused off the main thread (matching the ``SIGALRM`` path, which may only be armed there), when the
        deadline is already armed, for a non-positive deadline, and when SIGINT is not Python's default handler.

        Args:
            seconds: Deadline in seconds.
            hard_exit_grace: Seconds between the interrupt and the ``hard_exit`` backstop.

        Returns:
            ``True`` when the watchdog was started.

        Examples:
            >>> import threading
            >>> refused = []
            >>> worker = threading.Thread(target=lambda: refused.append(InterruptDeadline().arm(5)))
            >>> worker.start(); worker.join()
            >>> refused
            [False]
        """
        if seconds <= 0 or self._timer is not None or self._settled:
            return False
        if threading.current_thread() is not threading.main_thread():
            return False
        if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
            return False
        self._seconds = seconds
        self._hard_exit_grace = hard_exit_grace
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()
        return True

    def run(self, work: Callable[[], _T]) -> _T:
        """Run ``work`` under the deadline and settle the watchdog however the work ends.

        Args:
            work: The engine call to bound.

        Returns:
            Whatever ``work`` returns when it finishes before the deadline.

        Raises:
            DeadlineExpired: The watchdog's interrupt reached the work, or was pending when the work finished.
            KeyboardInterrupt: A real Ctrl-C, unchanged.
        """
        try:
            try:
                result = work()
            except KeyboardInterrupt:
                self._note_delivery()
                raise
            finally:
                self._settle()
        except KeyboardInterrupt:
            self._note_delivery()
            self._settle()
            if self._fired:
                raise DeadlineExpired(self._seconds) from None
            raise
        if self._fired:
            raise DeadlineExpired(self._seconds)
        return result

    def _note_delivery(self) -> None:
        """Mark the watchdog's interrupt as received once a KeyboardInterrupt reaches :meth:`run`."""
        if self._fired:
            self._delivered = True

    def _settle(self) -> None:
        """Stop the watchdog and backstop; drain an interrupt issued just before settling."""
        with self._lock:
            self._settled = True
            timers = (self._timer, self._backstop)
        for timer in timers:
            if timer is not None:
                timer.cancel()
        if self._fired and not self._delivered:
            self._drain()

    def _drain(self) -> None:
        """Let a pending interrupt land here, inside :meth:`run`, instead of in the caller."""
        limit = time.monotonic() + _DRAIN_LIMIT_SECONDS
        try:
            while time.monotonic() < limit:
                pass  # each call is an interpreter check point where the pending interrupt is raised
        except KeyboardInterrupt:
            self._delivered = True

    def _expire(self) -> None:
        """Watchdog body: flag the timeout first, then interrupt the main thread."""
        with self._lock:
            if self._settled:
                return
            self._fired = True
            _thread.interrupt_main()
            if self._hard_exit is not None:
                self._backstop = threading.Timer(self._hard_exit_grace, self._hard_stop)
                self._backstop.daemon = True
                self._backstop.start()

    def _hard_stop(self) -> None:
        """Backstop body: the main thread ignored the interrupt, so the owning process exits hard."""
        with self._lock:
            if self._settled or self._hard_exit is None:
                return
            self._settled = True
            hard_exit = self._hard_exit
        hard_exit(self._seconds)
