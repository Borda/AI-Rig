"""Tests for the advisory :func:`codemap_py.rwgate.writer_active` probe.

The probe exists so an opportunistic refresh can decline to start a second scan over one already running. Before it, a
query's self-heal spawned a writer regardless: the prompt hook starts a detached refresh on its own schedule, so the
second writer could only queue behind the first and then be killed at the heal timeout — full timeout paid, nothing
healed, and a writer killed mid-flight.

It is advisory, never a lease. These tests pin that distinction: it must report liveness, must not acquire anything, and
must not leave the gate in a state that blocks a real writer or reader afterwards.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import codemap_py.rwgate as rwgate  # noqa: E402  (needs the sys.path insert above)

_JOIN_TIMEOUT = 30.0


@pytest.fixture(name="index_path")
def _index_path(tmp_path: Path) -> Path:
    """Return a path for an index that does not exist yet; coordination sits beside it."""
    return tmp_path / "idx.json"


def _holder_program(index_path: Path) -> str:
    """Build a program that takes the writer lease, announces it, and holds it until its stdin closes."""
    return "\n".join(
        [
            "import sys",
            f"sys.path.insert(0, {str(_SRC)!r})",
            "from pathlib import Path",
            "from codemap_py import rwgate",
            "def build(target):",
            "    print('HELD', flush=True)",
            "    sys.stdin.readline()",
            "    rwgate.atomic_publish(target, b'{\"schema\": 1}')",
            "    return None",
            f"rwgate.write_index(Path({str(index_path)!r}), build, timeout=30)",
        ]
    )


def _spawn_holder(index_path: Path) -> subprocess.Popen:
    """Start a separate process holding the writer lease, returning once it has the lease.

    The holder keeps the lease until the caller closes its stdin (see ``_release``), so no timing guess is involved.

    A separate process, not a thread: POSIX ``fcntl`` locks are per-process, so an in-process holder would make the
    probe report this process's own ownership instead of exercising the foreign-token path a real second writer takes.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _holder_program(index_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    if proc.stdout.readline().strip() != "HELD":
        proc.kill()
        pytest.fail(f"holder failed to take the lease: {proc.communicate()[1]}")
    return proc


def _release(holder: subprocess.Popen) -> None:
    """Let the holder finish its build and exit, waiting for the lease to be released, then close its pipes."""
    assert holder.stdin is not None
    holder.stdin.close()
    holder.wait(timeout=_JOIN_TIMEOUT)
    for stream in (holder.stdout, holder.stderr):
        if stream is not None:
            stream.close()


def test_reports_false_when_no_writer_holds_intent(index_path: Path) -> None:
    """An idle gate reports no active writer.

    Nothing has ever written to the index path, so the probe has no intent to find.
    """
    assert rwgate.writer_active(index_path) is False


def test_reports_true_while_a_foreign_writer_holds_intent(index_path: Path) -> None:
    """A live writer in another process is visible to the probe.

    The holder subprocess keeps the writer lease until released; the probe runs while it is held and must see the
    foreign token. The holder is then released and awaited so no process outlives the test.
    """
    holder = _spawn_holder(index_path)
    try:
        assert rwgate.writer_active(index_path) is True
    finally:
        _release(holder)


def test_reports_false_again_once_the_writer_exits(index_path: Path) -> None:
    """Intent is released on writer exit, so the probe stops reporting it.

    A short-lived holder takes and drops the lease; afterwards a stale token must not make the probe report a writer.
    """
    holder = _spawn_holder(index_path)
    _release(holder)

    assert rwgate.writer_active(index_path) is False


def test_probe_does_not_acquire_a_lease(index_path: Path) -> None:
    """Probing must not take a reader token that a later writer would have to drain.

    If the probe leaked a lease, the writer below would block until its deadline and raise ``IndexBusy`` instead of
    publishing; the short timeout keeps that failure fast without any wall-clock assertion.
    """
    assert rwgate.writer_active(index_path) is False

    rwgate.write_index(index_path, lambda target: rwgate.atomic_publish(target, b'{"schema": 1}'), timeout=2)

    assert json.loads(index_path.read_text(encoding="utf-8")) == {"schema": 1}


def test_probe_leaves_a_later_read_unblocked(index_path: Path) -> None:
    """A read after probing still succeeds — the probe holds nothing across its return.

    A published index is probed once, then read through the gate; a leaked lock from the probe would block the read.
    """
    rwgate.write_index(index_path, lambda target: rwgate.atomic_publish(target, b'{"schema": 7}'), timeout=5)
    rwgate.writer_active(index_path)

    with rwgate.read_index(index_path, timeout=5) as data:
        assert data == {"schema": 7}


def test_probe_reports_false_when_coordination_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unwritable coordination root means no writer can hold intent through it.

    Reporting True there would make every caller stand down permanently on a broken setup, which is worse than the
    redundant scan the probe exists to avoid.
    """

    def _raise(_path):
        raise rwgate.CoordinationUnavailable("unwritable")

    monkeypatch.setattr(rwgate, "_ensure_coord", _raise)

    assert rwgate.writer_active(tmp_path / "idx.json") is False


def test_probe_is_exported() -> None:
    """The probe is public API; callers outside the package rely on it by name.

    Wrappers import ``writer_active`` from ``rwgate``, so dropping it from ``__all__`` breaks them at import time.
    """
    assert "writer_active" in rwgate.__all__
