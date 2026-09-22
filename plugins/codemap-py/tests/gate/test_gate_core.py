"""Process-safe read/write gate tests.

Each cross-process case uses the ``spawn`` start method so every child is a genuinely independent process with its own
``fcntl``/``msvcrt`` handles — the only faithful way to exercise the coordination contract. POSIX ``fork`` cases use
``os.fork`` directly and are skipped on Windows with a named reason. Every test is bounded well under 10s.

Cross-process ordering is reconstructed from the gate's event log (``CODEMAP_RWGATE_EVENTLOG``): every lifecycle
transition appends one JSON line, and the file's APPEND order (atomic ``O_APPEND`` writes) is the order key — not the
wall-clock ``t`` field, which is diagnostics only. A parent asserts global ordering (e.g. no index open during any
writer's exclusive phase) from that.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path

import pytest

_BIN = str(Path(__file__).resolve().parent.parent.parent / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)
# Repo root on sys.path so ``spawn`` children can re-import this test module by its
# importlib dotted name (plugins.codemap.tests.test_gate_core) via PEP 420 namespace
# packages; multiprocessing inherits the parent's sys.path.
_REPO = str(Path(__file__).resolve().parents[4])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import _rwgate  # noqa: E402  (path set above; also re-imported in spawn children)

_SPAWN = multiprocessing.get_context("spawn")
WIN = sys.platform == "win32"
NO_FORK = pytest.mark.skipif(WIN, reason="POSIX fork semantics — no fork() on Windows")


# ── worker functions (must be top-level for spawn pickling) ──────────────────
def _load_current(target: str) -> dict:
    """Read the current JSON index, treating absent or invalid data as empty."""
    try:
        with open(target, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _w_reader_once(index: str, out: str, timeout: float) -> None:
    """Read one gated index snapshot and write its version to an output file."""
    with _rwgate.read_index(index, timeout=timeout) as data:
        Path(out).write_text(json.dumps({"v": (data or {}).get("v")}))


def _w_reader_hold(index: str, ready: str, hold: float, timeout: float) -> None:
    """Hold a read lease after signaling readiness to a coordinating test."""
    with _rwgate.read_index(index, timeout=timeout) as data:
        Path(ready).write_text(json.dumps({"v": (data or {}).get("v")}))
        time.sleep(hold)


def _w_reader_loop(index: str, duration: float, timeout: float) -> None:
    """Exercise repeated bounded reads for the requested duration."""
    end = time.time() + duration
    while time.time() < end:
        try:
            with _rwgate.read_index(index, timeout=timeout):
                pass
        except _rwgate.IndexBusy:
            pass


def _w_reader_expect_busy(index: str, out: str, timeout: float) -> None:
    """Record whether a gated read succeeds or reports index contention."""
    try:
        with _rwgate.read_index(index, timeout=timeout) as data:
            Path(out).write_text(json.dumps({"stale": True, "v": (data or {}).get("v")}))
    except _rwgate.IndexBusy:
        Path(out).write_text(json.dumps({"busy": True}))


def _w_writer_increment(index: str, timeout: float) -> None:
    """Increment the published version while holding the write lease."""

    def _build(target: Path) -> None:
        """Increment and publish the version in the child writer process."""
        cur = _load_current(str(target))
        cur["v"] = cur.get("v", 0) + 1
        _rwgate.atomic_publish(target, json.dumps(cur).encode("utf-8"))

    _rwgate.write_index(index, _build, timeout=timeout)


def _w_writer_marker(index: str, marker: str, timeout: float) -> None:
    """Publish version 42 and mark completion from inside the write lease."""

    def _build(target: Path) -> None:
        """Publish the marker version and completion file."""
        _rwgate.atomic_publish(target, b'{"v": 42}')
        Path(marker).write_text("done")

    _rwgate.write_index(index, _build, timeout=timeout)


def _w_writer_slow(index: str, exclusive: float, timeout: float) -> None:
    """Delay inside an exclusive write lease before publishing version 1."""

    def _build(target: Path) -> None:
        """Delay within the lease before publishing the test version."""
        time.sleep(exclusive)
        _rwgate.atomic_publish(target, b'{"v": 1}')

    _rwgate.write_index(index, _build, timeout=timeout)


def _w_writer_hold_intent(index: str, ready: str, hold: float) -> None:
    """Advertise a pending writer, hold the lease, then publish version 99."""

    def _build(target: Path) -> None:
        """Signal writer intent, hold it, and then publish the version."""
        Path(ready).write_text("x")
        time.sleep(hold)
        _rwgate.atomic_publish(target, b'{"v": 99}')

    _rwgate.write_index(index, _build, timeout=30.0)


def _w_writer_crash_midwrite(index: str, ready: str, hold: float) -> None:
    """Leave an orphan temporary payload while simulating a stalled writer."""

    def _build(target: Path) -> None:
        """Write an orphan temp payload while holding the lease."""
        tmp = Path(target).parent / f".{Path(target).name}.orphan.tmp"
        tmp.write_bytes(b'{"v": 777}')  # written but never os.replace'd
        Path(ready).write_text("x")
        time.sleep(hold)

    _rwgate.write_index(index, _build, timeout=30.0)


# ── helpers ──────────────────────────────────────────────────────────────────
def _wait_file(path: str, timeout: float = 5.0) -> bool:
    """Wait briefly for a coordination file and report whether it appeared."""
    end = time.time() + timeout
    while time.time() < end:
        if Path(path).exists():
            return True
        time.sleep(0.01)
    return False


def _join(procs: list, timeout: float = 9.0) -> None:
    """Join workers within the test bound and fail if one remains alive."""
    for proc in procs:
        proc.join(timeout)
        assert not proc.is_alive(), "worker did not finish within bound"


def _events(log: str) -> list[dict]:
    """Return gate events in file-APPEND order — the cross-process order key.

    ``O_APPEND`` writes are atomic at EOF on host-local filesystems, so line order reflects real write order and
    preserves causality. We deliberately do NOT sort by wall-clock ``t`` because it is diagnostic only and can move
    backward when the clock is adjusted.
    """
    if not Path(log).exists():
        return []
    return [json.loads(ln) for ln in Path(log).read_text().splitlines() if ln.strip()]


def _wait_for_event(log: str, name: str, timeout: float = 6.0) -> None:
    """Block until *name* appears in the gate event log, or fail the test.

    A fixed ``time.sleep`` cannot stand in for this. Spawning a child costs a fresh interpreter, and under a loaded
    parallel run (``pytest -n 4``) that start-up routinely outlasts the tenth-of-a-second a caller would guess, so the
    "later" worker races ahead of the writer it is supposed to follow. Waiting on the writer's own recorded transition
    removes the guess: the event is appended once and never removed, so a slow observer still sees it.

    Args:
        log: Path to the gate event log.
        name: Event name to wait for.
        timeout: Seconds to wait before failing.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(event["event"] == name for event in _events(log)):
            return
        time.sleep(0.01)
    raise AssertionError(f"{name!r} never appeared in the gate event log within {timeout}s")


def _pos(events: list[dict], name: str) -> int:
    """Return the append-order index of the first event named *name*.

    >>> _pos([{"event": "read"}, {"event": "write"}], "write")
    1
    """
    return next(i for i, e in enumerate(events) if e["event"] == name)


@pytest.fixture(name="index")
def _index(tmp_path: Path) -> Path:
    """Publish an initial index (``v=1``) through the gate; return its path."""
    path = tmp_path / "idx.json"
    _rwgate.write_index(path, lambda t: _rwgate.atomic_publish(t, b'{"v": 1}'), timeout=10.0)
    return path


@pytest.fixture(name="eventlog")
def _eventlog(tmp_path: Path, monkeypatch) -> str:
    """Point the gate's cross-process event log at a temp file."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("CODEMAP_RWGATE_EVENTLOG", str(log))
    return str(log)


@pytest.fixture(autouse=True)
def _clear_instrument():
    """Clear gate instrumentation after each test."""
    yield
    _rwgate.set_instrument(None)


# ── reader / writer coordination ─────────────────────────────────────────────
def test_reader_before_writer_drains_first(index: Path, tmp_path: Path, eventlog: str) -> None:
    """A reader linearized before a writer must drain before the exclusive phase."""
    ready = str(tmp_path / "r.ready")
    reader = _SPAWN.Process(target=_w_reader_hold, args=(str(index), ready, 0.5, 5.0))
    reader.start()
    assert _wait_file(ready), "reader never acquired"
    writer = _SPAWN.Process(target=_w_writer_marker, args=(str(index), str(tmp_path / "w.done"), 5.0))
    writer.start()
    _join([reader, writer])

    ev = _events(eventlog)
    # append order: the reader marks its lease end before dropping the token, and
    # the writer cannot enter exclusive until that token drains — deterministic.
    assert _pos(ev, "reader_release") < _pos(ev, "writer_exclusive_enter"), (
        "writer entered exclusive phase before the reader drained"
    )


def test_writer_blocks_later_readers(index: Path, tmp_path: Path, eventlog: str) -> None:
    """Once writer intent is live, a later reader waits until release."""
    writer = _SPAWN.Process(target=_w_writer_slow, args=(str(index), 1.0, 5.0))
    writer.start()
    _wait_for_event(eventlog, "writer_exclusive_enter")
    out = str(tmp_path / "r.out")
    reader = _SPAWN.Process(target=_w_reader_once, args=(str(index), out, 5.0))
    reader.start()
    _join([writer, reader])

    assert json.loads(Path(out).read_text())["v"] == 1  # reader saw the published value
    ev = _events(eventlog)
    exit_pos = _pos(ev, "writer_exclusive_exit")
    open_positions = [i for i, e in enumerate(ev) if e["event"] == "index_open"]
    assert open_positions and min(open_positions) > exit_pos, "a later reader opened before the writer released"


def test_no_index_open_during_exclusive_phase(index: Path, tmp_path: Path, eventlog: str) -> None:
    """Event-order oracle: no reader parses the index inside any exclusive window."""
    writer = _SPAWN.Process(target=_w_writer_slow, args=(str(index), 0.4, 6.0))
    writer.start()
    _wait_for_event(eventlog, "writer_exclusive_enter")
    readers = [_SPAWN.Process(target=_w_reader_once, args=(str(index), str(tmp_path / f"r{i}"), 6.0)) for i in range(4)]
    for proc in readers:
        proc.start()
    _join([writer, *readers])

    ev = _events(eventlog)
    # Walk the append-ordered log; assert no index_open falls between any writer's
    # exclusive enter and its matching exit. Append order = real write order, so a
    # reader parse during the exclusive window would land between the two markers.
    open_writers: set[int] = set()
    saw_window = False
    for e in ev:
        if e["event"] == "writer_exclusive_enter":
            open_writers.add(e["pid"])
            saw_window = True
        elif e["event"] == "writer_exclusive_exit":
            open_writers.discard(e["pid"])
        elif e["event"] == "index_open":
            assert not open_writers, "index opened during an exclusive phase"
    assert saw_window, "no exclusive window recorded"


def test_concurrent_readers_after_update(index: Path, tmp_path: Path) -> None:
    """Acquisition race: many readers acquire tokens concurrently and all succeed."""
    outs = [str(tmp_path / f"r{i}.out") for i in range(8)]
    procs = [_SPAWN.Process(target=_w_reader_once, args=(str(index), out, 6.0)) for out in outs]
    for proc in procs:
        proc.start()
    _join(procs)
    for out in outs:
        assert json.loads(Path(out).read_text())["v"] == 1


def test_competing_writers_serialize_without_lost_update(index: Path) -> None:
    """Three writers serialize under the gate; no increment is lost."""
    procs = [_SPAWN.Process(target=_w_writer_increment, args=(str(index), 8.0)) for _ in range(3)]
    for proc in procs:
        proc.start()
    _join(procs)
    with _rwgate.read_index(index, timeout=5.0) as data:
        assert data["v"] == 1 + 3, "a concurrent writer's update was lost"


def test_writer_not_starved_under_reader_load(index: Path, tmp_path: Path) -> None:
    """Writer preference: a sustained reader stream does not starve the writer."""
    marker = str(tmp_path / "w.done")
    load = [_SPAWN.Process(target=_w_reader_loop, args=(str(index), 1.2, 5.0)) for _ in range(6)]
    writer = _SPAWN.Process(target=_w_writer_marker, args=(str(index), marker, 8.0))
    for proc in load:
        proc.start()
    writer.start()
    _join([writer, *load])
    assert Path(marker).exists(), "writer was starved by continuous readers"


# ── timeouts: never a stale read ─────────────────────────────────────────────
def test_reader_timeout_returns_busy_never_stale(index: Path, tmp_path: Path) -> None:
    """A reader blocked by writer intent hits IndexBusy — it never reads stale."""
    ready = str(tmp_path / "w.ready")
    holder = _SPAWN.Process(target=_w_writer_hold_intent, args=(str(index), ready, 1.5))
    holder.start()
    assert _wait_file(ready), "writer never took intent"
    out = str(tmp_path / "r.out")
    reader = _SPAWN.Process(target=_w_reader_expect_busy, args=(str(index), out, 0.3))
    reader.start()
    _join([reader], timeout=5.0)
    holder.join(5.0)
    assert json.loads(Path(out).read_text()) == {"busy": True}


def test_writer_drain_timeout_returns_busy(index: Path, tmp_path: Path) -> None:
    """A writer that cannot drain a live reader raises IndexBusy (no build over it)."""
    ready = str(tmp_path / "r.ready")
    reader = _SPAWN.Process(target=_w_reader_hold, args=(str(index), ready, 1.5, 5.0))
    reader.start()
    assert _wait_file(ready), "reader never acquired"
    with pytest.raises(_rwgate.IndexBusy):
        _rwgate.write_index(index, lambda t: _rwgate.atomic_publish(t, b'{"v": 2}'), timeout=0.3)
    reader.join(5.0)
    with _rwgate.read_index(index, timeout=5.0) as data:
        assert data["v"] == 1, "index was mutated despite drain timeout"


# ── crash recovery (liveness, never PID/age) ─────────────────────────────────
def test_dead_writer_intent_is_recovered(index: Path, tmp_path: Path) -> None:
    """A hard-killed writer's intent is reclaimed by the next writer via liveness."""
    ready = str(tmp_path / "w.ready")
    victim = _SPAWN.Process(target=_w_writer_hold_intent, args=(str(index), ready, 30.0))
    victim.start()
    assert _wait_file(ready), "victim never took intent"
    victim.kill()  # SIGKILL on POSIX, TerminateProcess on Windows — no cleanup runs
    victim.join(5.0)
    # next writer must recover the orphaned intent and publish
    _rwgate.write_index(index, lambda t: _rwgate.atomic_publish(t, b'{"v": 5}'), timeout=5.0)
    with _rwgate.read_index(index, timeout=5.0) as data:
        assert data["v"] == 5


def test_midwrite_crash_preserves_last_index(index: Path, tmp_path: Path) -> None:
    """A crash between temp write and publish preserves the last complete index."""
    ready = str(tmp_path / "w.ready")
    victim = _SPAWN.Process(target=_w_writer_crash_midwrite, args=(str(index), ready, 30.0))
    victim.start()
    assert _wait_file(ready), "victim never started build"
    victim.kill()  # SIGKILL on POSIX, TerminateProcess on Windows — no cleanup runs
    victim.join(5.0)
    assert json.loads(index.read_text())["v"] == 1, "last complete index was corrupted"
    assert list(index.parent.glob(".idx.json.*.tmp")), "orphan temp expected before recovery"
    # a clean writer recovers: orphan temp removed, new index published
    _rwgate.write_index(index, lambda t: _rwgate.atomic_publish(t, b'{"v": 6}'), timeout=5.0)
    assert json.loads(index.read_text())["v"] == 6
    assert not list(index.parent.glob(".idx.json.*.tmp")), "orphan temp not cleaned"


# ── same-process thread safety ───────────────────────────────────────────────
def test_same_process_reader_writer_no_false_stale(index: Path) -> None:
    """One thread's live reader token is never treated as stale by another thread."""
    order: list[str] = []
    _rwgate.set_instrument(lambda name, fields: order.append(name))
    hold_started = threading.Event()

    def _reader() -> None:
        """Hold a read lease while the writer attempts to enter."""
        with _rwgate.read_index(index, timeout=5.0):
            hold_started.set()
            time.sleep(0.3)

    def _writer() -> None:
        """Attempt the competing write after the reader signals readiness."""
        hold_started.wait(2.0)
        _rwgate.write_index(index, lambda t: _rwgate.atomic_publish(t, b'{"v": 2}'), timeout=5.0)

    tr, tw = threading.Thread(target=_reader), threading.Thread(target=_writer)
    tr.start()
    hold_started.wait(2.0)
    tw.start()
    tr.join(6.0)
    tw.join(6.0)
    assert order.index("reader_release") < order.index("writer_exclusive_enter")


# ── single registry handle discipline (POSIX trap) ───────────────────────────
@NO_FORK
def test_single_registry_handle_serializes_in_process(index: Path) -> None:
    """Nested/sequential same-process acquisitions never drop another holder's lock."""
    coord = index.parent / ".index-rw"
    reg = _rwgate._registry_for(coord)
    order: list[str] = []
    holding = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        """Hold the registry mutex until the outer test releases it."""
        with reg.mutex(time.monotonic() + 5.0):
            order.append("a_in")
            holding.set()
            release.wait(3.0)
            order.append("a_out")

    ta = threading.Thread(target=_holder)
    ta.start()
    assert holding.wait(2.0)
    fd_before = reg._registry_fd()
    # a second thread's acquire must not proceed while A holds (bounded → IndexBusy)
    with pytest.raises(_rwgate.IndexBusy):
        with reg.mutex(time.monotonic() + 0.3):
            order.append("b_in")
    release.set()
    ta.join(5.0)
    assert "b_in" not in order and order == ["a_in", "a_out"]
    # sequential reacquire reuses the single per-process handle
    with reg.mutex(time.monotonic() + 5.0):
        pass
    assert reg._registry_fd() == fd_before, "registry handle was reopened (trap)"


# ── POSIX fork resets inherited coordination state ───────────────────────────
def _run_in_fork(child) -> int:
    """Run a callback in a forked child and return its normalized exit code."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child branch
        code = 0
        try:
            child()
        except BaseException:  # noqa: BLE001 - any failure → nonzero child exit
            code = 1
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


@NO_FORK
def test_fork_idle_parent_child_operates(index: Path) -> None:
    """Child of an idle parent gets fresh handles and completes a full cycle."""

    def _child() -> None:
        """Exercise a complete write/read cycle in the forked child."""
        _rwgate.write_index(index, lambda t: _rwgate.atomic_publish(t, b'{"v": 3}'), timeout=5.0)
        with _rwgate.read_index(index, timeout=5.0) as data:
            assert data["v"] == 3

    assert _run_in_fork(_child) == 0


@NO_FORK
def test_fork_reader_parent_child_sees_foreign_token(index: Path) -> None:
    """Child cannot steal the parent's live reader token; it must wait/IndexBusy."""
    with _rwgate.read_index(index, timeout=5.0):

        def _child() -> None:
            """Attempt a write while the parent reader token remains live."""
            # parent's reader token is foreign & live → writer drain must time out
            try:
                _rwgate.write_index(index, lambda t: None, timeout=0.4)
            except _rwgate.IndexBusy:
                return
            raise AssertionError("child drained a live parent reader")

        assert _run_in_fork(_child) == 0


@NO_FORK
def test_fork_writer_intent_parent_child_blocked(index: Path, tmp_path: Path) -> None:
    """Child cannot inherit or steal the parent's live writer intent."""
    forked: dict[str, int] = {}

    def _build(target: Path) -> None:
        """Fork a reader during writer intent, then publish the test version."""

        def _child() -> None:
            """Attempt to read while the parent writer intent is live."""
            try:
                with _rwgate.read_index(index, timeout=0.4):
                    raise AssertionError("child read under live writer intent")
            except _rwgate.IndexBusy:
                return

        forked["code"] = _run_in_fork(_child)
        _rwgate.atomic_publish(target, b'{"v": 4}')

    _rwgate.write_index(index, _build, timeout=5.0)
    assert forked["code"] == 0


@NO_FORK
def test_fork_mutex_holder_parent_child_no_deadlock(index: Path) -> None:
    """Child forked while parent holds the registry mutex times out cleanly."""
    coord = index.parent / ".index-rw"
    reg = _rwgate._registry_for(coord)
    with reg.mutex(time.monotonic() + 5.0):

        def _child() -> None:
            """Attempt a write while the parent holds the registry mutex."""
            try:
                _rwgate.write_index(index, lambda t: None, timeout=0.4)
            except _rwgate.IndexBusy:
                return
            raise AssertionError("child acquired mutex held by parent")

        assert _run_in_fork(_child) == 0


# ── read-only coordination root ──────────────────────────────────────────────
@pytest.mark.skipif(WIN, reason="POSIX permission bits — unreliable chmod on Windows")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses mode bits")
def test_readonly_root_refused(tmp_path: Path) -> None:
    """An unwritable coordination root is refused, never read without a token."""
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        with pytest.raises(_rwgate.CoordinationUnavailable):
            with _rwgate.read_index(ro / "idx.json", timeout=1.0):
                pass
    finally:
        os.chmod(ro, 0o700)  # restore so pytest can clean up


# ── release-path exception safety ──────────────────────────
def test_build_failure_with_contended_release_propagates_and_recovers(index: Path, monkeypatch) -> None:
    """Preserve build errors during contended release and intent cleanup.

    Forces the release-mutex reacquire to time out (a holder thread keeps the registry mutex through the writer's
    release window) while build_fn raises. The original exception must propagate (not IndexBusy), the writer's OS lock
    must still be dropped (no leak), and a later writer must reclaim the stale intent by liveness and publish.
    """
    monkeypatch.setattr(_rwgate, "_RELEASE_TIMEOUT", 0.2)  # force the release-mutex timeout
    coord = index.parent / ".index-rw"
    reg = _rwgate._registry_for(coord)
    writer_path = coord / "writer.json"

    build_started = threading.Event()
    holder_has_mutex = threading.Event()
    let_holder_release = threading.Event()
    seen: list[str] = []
    _rwgate.set_instrument(lambda name, fields: seen.append(name))

    def _hold_mutex() -> None:
        """Hold the registry mutex through the writer's degraded release path."""
        build_started.wait(3.0)  # writer has intent + is inside build
        with reg.mutex(time.monotonic() + 5.0):
            holder_has_mutex.set()
            let_holder_release.wait(3.0)  # keep the registry mutex through the release window

    def _build(target: Path) -> None:
        """Raise the original build failure after the holder takes the mutex."""
        build_started.set()
        assert holder_has_mutex.wait(3.0), "holder never took the registry mutex"
        raise RuntimeError("boom")

    holder = threading.Thread(target=_hold_mutex)
    holder.start()
    with pytest.raises(RuntimeError, match="boom"):  # original error, NOT masked by IndexBusy
        _rwgate.write_index(index, _build, timeout=5.0)
    let_holder_release.set()
    holder.join(5.0)

    assert "writer_release_degraded" in seen, "release fallback path was not exercised"
    # OS lock was dropped under contention (no leak); a later writer reclaims + publishes.
    _rwgate.write_index(index, lambda t: _rwgate.atomic_publish(t, b'{"v": 7}'), timeout=5.0)
    assert not writer_path.exists(), "writer intent leaked after recovery"
    with _rwgate.read_index(index, timeout=5.0) as data:
        assert data["v"] == 7


# ── event-log append: Windows branch runs on every host (no OS skip) ─────────
def test_append_event_line_win32_branch_writes_parseable_lines(tmp_path: Path, monkeypatch) -> None:
    """The lock-guarded Windows append path emits whole, parseable JSON lines.

    Windows CRT ``_O_APPEND`` is seek-then-write, not atomic across processes; the guarded branch is the fix for torn
    event lines (JSONDecodeError "Extra data" in the order oracles). Simulate ``win32`` on every host — the branch only
    touches the portable ``_os_try_lock``/``_os_unlock`` seam, so no host-only API is missing on POSIX.
    """
    monkeypatch.setattr(_rwgate.sys, "platform", "win32")
    log = tmp_path / "ev.jsonl"
    for i in range(5):
        _rwgate._append_event_line(str(log), "probe", {"pid": os.getpid(), "seq": i})
    lines = log.read_text().splitlines()
    assert len(lines) == 5
    assert [json.loads(ln)["seq"] for ln in lines] == list(range(5))


def test_append_event_line_win32_branch_spins_until_lock(tmp_path: Path, monkeypatch) -> None:
    """A briefly contended log lock delays the append instead of tearing or dropping.

    First acquisition attempt is refused (simulated contention); the spin loop must retry and still land the complete
    event line.
    """
    monkeypatch.setattr(_rwgate.sys, "platform", "win32")
    attempts = {"n": 0}
    real_try = _rwgate._os_try_lock

    def _flaky_try(fd: int) -> bool:
        """Refuse the first lock attempt, then delegate to the real lock."""
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False
        return real_try(fd)

    monkeypatch.setattr(_rwgate, "_os_try_lock", _flaky_try)
    log = tmp_path / "ev.jsonl"
    _rwgate._append_event_line(str(log), "probe", {"pid": os.getpid(), "seq": 0})
    assert attempts["n"] >= 2, "spin loop never retried the refused lock"
    assert json.loads(log.read_text().splitlines()[0])["event"] == "probe"
