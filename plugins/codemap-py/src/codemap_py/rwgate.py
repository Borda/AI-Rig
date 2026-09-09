"""Writer-preferred, cross-process read/write gate for the codemap index.

This module is the process-safe coordination core mandated by the dual-runtime
plan ("Process-safe read/write and version-skew contract").

What is actually guaranteed, and where:

* The gate is entered by the **engines**, not by their launchers:
  :func:`codemap_py.query.main` loads the index under :func:`read_lease`, and
  :func:`codemap_py.graph.main` builds and publishes under :func:`write_index`.
  Every route into an engine therefore leases — the ``codemap-py`` dispatcher,
  the ``bin/scan-query`` / ``bin/scan-index`` launchers, the query engine's own
  self-heal spawn, and any hook that spawns those binaries.
* A caller must **not** wrap an engine invocation in a second lease. A parent
  holding a lease while its child process waits for one is a lock-order
  inversion: the child cannot acquire (the parent's token is foreign and live to
  it) and the parent cannot release (it is blocked on the child), so the child
  burns its deadline and fails ``index_busy`` every time. This is precisely why
  leasing lives in the engines and the dispatcher no longer wraps them.
* What the gate prevents is **concurrent full scans** and reads racing a
  publish. It does not, and never did, prevent a torn read — ``os.replace``
  already makes publication atomic.

No launcher, adapter, or consumer is permitted a token-free filesystem read of
the index.

``bin/_rwgate.py`` is a compatibility shim that aliases this module in
``sys.modules`` — tests that monkeypatch private internals
(``_RELEASE_TIMEOUT``, ``_registry_for``, ...) via the bare-name import still
reach this one implementation, not a divergent copy.

Design (single-generation prototype):

* Coordination lives beside the index in ``<index-dir>/.index-rw/``:
  ``registry.lock`` (the acquisition mutex — a fixed one-byte file, locked but
  never replaced or truncated), ``readers/<uuid>.json`` (one shared-reader
  token per live read, each holding its own OS handle lock for its lifetime),
  and ``writer.json`` (writer intent, holding its handle lock exclusively).
* The registry mutex serialises *acquisition ordering only*; it is never held
  during index build, load, parse, validate, or query.
* A reader linearises by creating and locking its token under the mutex *iff*
  no live writer intent exists; otherwise it waits. A writer linearises by
  creating and locking ``writer.json`` under the mutex, which blocks later
  reader registration (writer preference), then drains earlier readers before
  entering the exclusive update phase.
* OS backing is ``fcntl`` record locks on POSIX and ``msvcrt`` byte locks on
  Windows, plus atomic filesystem operations, on host-local filesystems.

Two OS traps drive the implementation:

* POSIX ``fcntl`` record locks are released when *any* descriptor to the file
  is closed by the owning process, so ``registry.lock`` is opened exactly once
  per process and same-process acquisitions serialise through a threading lock.
* ``fcntl`` locks and process/thread ownership do not survive ``fork``; a PID
  mismatch is the authoritative fork detector. The child discards inherited
  handle/ownership state and lazily reopens; ``os.register_at_fork`` performs
  the same reset eagerly.

Public API:
    read_index(path, *, timeout=...) -> context manager yielding parsed dict|None
    write_index(path, build_fn, *, timeout=...) -> build_fn's return value
    atomic_publish(index_path, data) -> None   (helper for build_fn)
    IndexBusy, CoordinationUnavailable          (exceptions)
    set_instrument(callback) / _emit event hooks (event-order oracles)
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Optional
from uuid import uuid4

from codemap_py.index_paths import coordination_root

__all__ = [
    "read_lease",
    "read_index",
    "write_index",
    "atomic_publish",
    "publish_stream",
    "IndexBusy",
    "CoordinationUnavailable",
    "IndexUnreadable",
    "VersionSkewRefused",
    "set_instrument",
]

# ── tunables (internal 0.25.0 choices; never exposed as CLI/env config) ────────
DEFAULT_TIMEOUT = 30.0  # seconds; callers/tests may pass a shorter bound
_POLL = 0.01  # seconds between contention retries
_RELEASE_TIMEOUT = 10.0  # bounded mutex reacquire for orderly intent release

_COORD_NAME = ".index-rw"
_READERS_NAME = "readers"
_REGISTRY_NAME = "registry.lock"
_WRITER_NAME = "writer.json"

_O_BINARY = getattr(os, "O_BINARY", 0)


class IndexBusy(RuntimeError):
    """Raised when a bounded read/write deadline expires under contention.

    A stable, named condition; it never falls back to an unlocked or stale read. Diagnostics identify which phase timed
    out.
    """


class CoordinationUnavailable(RuntimeError):
    """Raised when the coordination root cannot be created or locked.

    A read-only index/coordination root is refused here rather than silently read without a token. A shared writable
    ``CODEMAP_INDEX_DIR`` is the supported path for read-only source trees.
    """


class IndexUnreadable(CoordinationUnavailable):
    """Raised when the index file exists but does not parse as JSON.

    Deliberately a subclass of :class:`CoordinationUnavailable` rather than a sibling of it. Consumers of this gate
    already catch ``(IndexBusy, CoordinationUnavailable)`` as the complete set of ways a leased read can fail and
    translate it into a bounded structured error; a brand-new top-level exception type would have escaped every one of
    them as a raw traceback on a corrupt index — the exact capability-contract violation this class exists to close.
    Subclassing makes every existing consumer correct without edits, and the distinct type still lets a caller that
    cares (``codemap-py query``) report "corrupt, rebuild" instead of "coordination unavailable".

    The message always names the offending path and the rebuild command.
    """


class VersionSkewRefused(RuntimeError):
    """Raised when a writer would overwrite an index written by a newer schema.

    The on-disk index carries a ``scan_version`` greater than the writer's own, so the writer is the older tool.
    Publishing would silently downgrade the shared index and force the newer reader to rebuild — repeatedly, if both
    tools keep running. The write is refused instead.
    """


# ── platform lock primitives ───────────────────────────────────────────────
if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
    import msvcrt

    def _os_try_lock(fd: int) -> bool:
        """Non-blocking exclusive lock of byte 0; True on success."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _os_unlock(fd: int) -> None:
        """Release the byte-0 lock; tolerant of an already-unlocked handle."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _os_try_lock(fd: int) -> bool:
        """Non-blocking exclusive ``fcntl`` record lock; True on success."""
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _os_unlock(fd: int) -> None:
        """Release the ``fcntl`` record lock; tolerant of no held lock."""
        try:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _lock_blocking(fd: int, deadline: float) -> bool:
    """Acquire an exclusive lock, retrying until *deadline* (monotonic secs).

    Returns:
        True once the lock is held, False if the deadline expires first.
    """
    while True:
        if _os_try_lock(fd):
            return True
        if not _sleep_until(deadline):
            return False


def _sleep_until(deadline: float) -> bool:
    """Sleep one poll interval if time remains before *deadline*.

    Returns:
        True if it slept (time remained), False if the deadline has passed.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(_POLL, remaining))
    return True


# ── instrumentation (event-order oracles) ──────────────────────────────────
_INSTRUMENT: Optional[Callable[[str, dict], None]] = None
_SEQ = itertools.count()


def set_instrument(callback: Optional[Callable[[str, dict], None]]) -> None:
    """Install (or clear with ``None``) an in-process event callback.

    The callback receives ``(event_name, fields)``. Cross-process tests instead
    set ``CODEMAP_RWGATE_EVENTLOG`` to a file path; every event is appended
    there as one JSON line. Cross-process ordering is the file's append order:
    ``O_APPEND`` writes are atomic at EOF on host-local filesystems, so line
    order reflects real write order and preserves causality (a process only
    emits after the state it observed was published). Each line also carries a
    wall-clock ``t`` (human diagnostics only) and a per-process monotonic
    ``seq`` (in-process tie-break); neither is the cross-process order key.

    Examples:
        >>> seen = []
        >>> set_instrument(lambda name, fields: seen.append(name))
        >>> set_instrument(None)
    """
    global _INSTRUMENT
    _INSTRUMENT = callback


def _emit(event: str, **fields: Any) -> None:
    """Record a gate lifecycle event for order oracles; never raises."""
    fields.setdefault("pid", os.getpid())
    fields["seq"] = next(_SEQ)  # per-process monotonic; in-process tie-break only
    fields["t"] = time.time()  # wall clock: human diagnostics only, not the order key
    cb = _INSTRUMENT
    if cb is not None:
        try:
            cb(event, dict(fields))
        except Exception:  # noqa: BLE001 - instrumentation must never break the gate
            pass
    log_path = os.environ.get("CODEMAP_RWGATE_EVENTLOG")
    if log_path:
        _append_event_line(log_path, event, fields)


def _append_event_line(log_path: str, event: str, fields: dict) -> None:
    """Append one ``{event, ...}`` JSON line without cross-process tearing.

    POSIX ``O_APPEND`` is kernel-atomic at EOF, so one ``os.write`` suffices. Windows CRT ``_O_APPEND`` emulates append
    as seek-to-EOF-then-write — NOT atomic across processes — so two concurrent emitters could interleave and tear a
    line (observed as ``json.decoder.JSONDecodeError: Extra data`` in the order oracles). There the seek+write runs
    under a short exclusive byte-0 lock on the log itself (same ``_os_try_lock`` primitive as the gate); on lock-timeout
    the event is dropped rather than the log corrupted.
    """
    try:
        line = json.dumps({"event": event, **fields}, sort_keys=True) + "\n"
        data = line.encode("utf-8")
        fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            if sys.platform == "win32":
                deadline = time.time() + 2.0
                while not _os_try_lock(fd):
                    if time.time() > deadline:  # pragma: no cover - contention bound
                        return
                    time.sleep(0.001)
                try:
                    os.lseek(fd, 0, os.SEEK_END)
                    os.write(fd, data)
                finally:
                    _os_unlock(fd)
            else:
                os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover - diagnostics only
        pass


# ── per-process registry (single handle discipline + fork reset) ────────────
class _Registry:
    """Per-process coordination state for one ``.index-rw`` directory.

    Holds the single ``registry.lock`` descriptor, an in-process threading mutex that serialises same-process
    acquisitions (POSIX ``fcntl`` locks are per-process, so two threads would otherwise both "hold" the mutex), and the
    owned-token guard mapping owned paths to their live descriptors. The guard spans each token's full lifetime so
    cleanup never probes or removes a token this process still owns.

    Every registry records its creator PID; a mismatch means the object was inherited across ``fork`` and its
    handles/locks are invalid.
    """

    def __init__(self, coord: Path, pid: int) -> None:
        self.coord = coord
        self.pid = pid
        self._reg_fd: Optional[int] = None
        self._reg_lock = threading.Lock()
        self._owned: dict[str, int] = {}
        self._owned_lock = threading.Lock()

    def _registry_fd(self) -> int:
        """Lazily open ``registry.lock`` once; reused for every acquisition."""
        if self._reg_fd is None:
            self._reg_fd = os.open(self.coord / _REGISTRY_NAME, os.O_RDWR | _O_BINARY)
        return self._reg_fd

    @contextlib.contextmanager
    def mutex(self, deadline: float) -> Iterator[None]:
        """Hold the registry mutex (thread lock + OS lock) or raise IndexBusy.

        The in-process threading lock serialises same-process acquisitions (POSIX ``fcntl`` locks are per-process, so
        two threads would otherwise both hold the OS lock); it is acquired with the same bounded deadline so a waiter in
        this process cannot block past the caller's timeout.
        """
        if not self._reg_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise IndexBusy("registry mutex (in-process) acquisition timed out")
        locked = False
        try:
            locked = _lock_blocking(self._registry_fd(), deadline)
            if not locked:
                raise IndexBusy("registry mutex acquisition timed out")
            yield
        finally:
            if locked:
                _os_unlock(self._registry_fd())
            self._reg_lock.release()

    def add_owned(self, path: Path, fd: int) -> None:
        with self._owned_lock:
            self._owned[str(path)] = fd

    def pop_owned(self, path: Path) -> Optional[int]:
        with self._owned_lock:
            return self._owned.pop(str(path), None)

    def owns(self, path: Path) -> bool:
        with self._owned_lock:
            return str(path) in self._owned

    def close_inherited(self) -> None:
        """Close inherited descriptors in a fork child without unlocking.

        The locks are not this (child) process's to release, and leaving the inherited descriptors open would arm the
        POSIX close-releases-all trap against freshly reopened handles. State is discarded, not unlocked.
        """
        for fd in list(self._owned.values()):
            _safe_close(fd)
        if self._reg_fd is not None:
            _safe_close(self._reg_fd)
        self._reg_fd = None
        self._owned.clear()


_REGISTRIES: dict[str, _Registry] = {}
_REGISTRIES_LOCK = threading.Lock()


def _registry_for(coord: Path) -> _Registry:
    """Return the live registry for *coord*, rebuilding after a fork."""
    key = str(coord)
    pid = os.getpid()
    with _REGISTRIES_LOCK:
        reg = _REGISTRIES.get(key)
        if reg is None or reg.pid != pid:
            reg = _Registry(coord, pid)
            _REGISTRIES[key] = reg
        return reg


def _reset_after_fork() -> None:
    """Discard all inherited registry state in a fork child."""
    with _REGISTRIES_LOCK:
        for reg in _REGISTRIES.values():
            reg.close_inherited()
        _REGISTRIES.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


# ── filesystem helpers ──────────────────────────────────────────────────────
def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        os.unlink(path)


def _safe_close(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


def _ensure_coord(index_path: Path) -> Path:
    """Create the coordination root for *index_path* and initialise the registry file.

    The root is ``.index-rw`` beside the index unless ``CODEMAP_COORDINATION_DIR`` names another directory; the shared
    resolver decides, so the gate never initialises a directory the path resolver would not lease.

    Raises:
        CoordinationUnavailable: the root is unwritable (read-only source tree
            without a writable ``CODEMAP_INDEX_DIR`` or ``CODEMAP_COORDINATION_DIR``).
    """
    coord = coordination_root(index_path.parent)
    try:
        (coord / _READERS_NAME).mkdir(parents=True, exist_ok=True)
        _init_registry_file(coord)
    except OSError as exc:
        raise CoordinationUnavailable(f"coordination root unwritable: {coord}") from exc
    return coord


def _init_registry_file(coord: Path) -> None:
    """Create ``registry.lock`` with its fixed byte once; never truncate/replace.

    ``O_EXCL`` guarantees exactly one initialiser and, critically, means an already-initialised file is never reopened
    here — reopening and closing a second descriptor would drop a lock this process may currently hold on it.
    """
    reg = coord / _REGISTRY_NAME
    try:
        fd = os.open(reg, os.O_CREAT | os.O_EXCL | os.O_RDWR | _O_BINARY, 0o644)
    except FileExistsError:
        return
    try:
        os.write(fd, b"L")
    finally:
        os.close(fd)


def _create_owned(reg: _Registry, path: Path, payload: bytes) -> Optional[int]:
    """Create, lock, and register a token/intent file owned by this process.

    Returns:
        The held descriptor, or ``None`` if the exclusive lock was lost to a
        racing owner (only possible for a shared fixed name, never a unique
        reader token).
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR | _O_BINARY, 0o644)
    if not _os_try_lock(fd):
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    reg.add_owned(path, fd)
    return fd


def _release_owned(reg: _Registry, path: Path) -> None:
    """Unlock, close, and unlink an owned token/intent; drop the guard entry."""
    fd = reg.pop_owned(path)
    if fd is not None:
        _os_unlock(fd)
        _safe_close(fd)
    _safe_unlink(path)


def _payload(reg: _Registry, kind: str) -> bytes:
    """Serialise a small owner descriptor (no timestamp — kept deterministic)."""
    return json.dumps({"kind": kind, "pid": reg.pid}, sort_keys=True).encode("utf-8")


# ── liveness probes (run under the registry mutex) ──────────────────────────
# Foreign-token probe outcomes (single-sourced open policy).
_PROBE_GONE = "gone"  # file absent → token drained or already reclaimed
_PROBE_PENDING = "pending"  # Windows pending-delete / sharing violation → retry next poll


def _open_foreign_token(path: Path) -> tuple[Optional[int], str]:
    """Open a FOREIGN token/intent file for a non-blocking liveness probe.

    Foreign-file opens are the only places that observe another process mid
    release. On POSIX an ``unlink`` is immediate, so a racing open just gets
    ``FileNotFoundError``. Windows instead keeps a just-unlinked file in a
    *pending-delete* state until the last handle closes, and a fresh
    ``CreateFile`` on it fails with access-denied / sharing-violation
    (surfaced as :class:`PermissionError`, winerror 5 or 32). That is a
    transient release-in-progress, not a live owner and not a crash.

    Returns:
        ``(fd, "")`` on success — caller closes the fd and decides live/dead by
        the handle lock; ``(None, _PROBE_GONE)`` when the file is absent
        (drained/reclaimed); ``(None, _PROBE_PENDING)`` on the Windows
        pending-delete race — the caller must treat the token as still present
        and retry on the next poll (it resolves to ``_PROBE_GONE`` within the
        poll cadence), never as live-forever.

    POSIX behaviour is byte-identical to a bare ``os.open`` guarded only by
    ``FileNotFoundError``: the pending-delete branch is Windows-only, so a real
    POSIX ``PermissionError`` still propagates.
    """
    try:
        return os.open(path, os.O_RDWR | _O_BINARY), ""
    except FileNotFoundError:
        return None, _PROBE_GONE
    except PermissionError:
        if sys.platform == "win32":
            return None, _PROBE_PENDING
        raise


def _intent_live(reg: _Registry, writer_path: Path) -> bool:
    """Report whether ``writer.json`` is held by a live process (recover if not).

    A dead owner's intent (handle lock acquirable non-blockingly) is removed so a waiting operation may proceed.
    Liveness is proven by the handle lock — never by PID or age. A Windows pending-delete probe counts as live so the
    caller retries next poll rather than racing a create over a releasing file.
    """
    if reg.owns(writer_path):
        return True
    fd, status = _open_foreign_token(writer_path)
    if fd is None:
        return status == _PROBE_PENDING
    try:
        if _os_try_lock(fd):
            _os_unlock(fd)
            _safe_unlink(writer_path)
            return False
        return True
    finally:
        os.close(fd)


def _readers_drained(reg: _Registry, readers_dir: Path) -> bool:
    """Report whether no live reader token remains (recover dead ones).

    Own tokens (this process, guard-active) count as live and are never probed or removed. Foreign tokens whose handle
    lock is acquirable are dead/complete and are removed under the mutex. A Windows pending-delete probe counts as live
    (still releasing) so drain waits one more poll instead of crashing.
    """
    live = 0
    for tok in _list_tokens(readers_dir):
        if reg.owns(tok):
            live += 1
            continue
        fd, status = _open_foreign_token(tok)
        if fd is None:
            if status == _PROBE_PENDING:
                live += 1  # releasing right now (Windows pending-delete) → not yet drained
            continue  # _PROBE_GONE → drained
        try:
            if _os_try_lock(fd):
                _os_unlock(fd)
                _safe_unlink(tok)
            else:
                live += 1
        finally:
            os.close(fd)
    return live == 0


def _list_tokens(readers_dir: Path) -> list[Path]:
    try:
        return [readers_dir / n for n in os.listdir(readers_dir) if n.endswith(".json")]
    except FileNotFoundError:
        return []


# ── reader path ─────────────────────────────────────────────────────────────
def _acquire_reader_token(reg: _Registry, coord: Path, deadline: float) -> Path:
    """Linearise and register a shared-reader token, or raise IndexBusy.

    Under the registry mutex, a reader with no live writer intent creates and locks its unique token (its linearization
    point) before releasing the mutex. If intent already exists it registers nothing and waits.
    """
    readers_dir = coord / _READERS_NAME
    writer_path = coord / _WRITER_NAME
    while True:
        with reg.mutex(deadline):
            if not _intent_live(reg, writer_path):
                token = readers_dir / f"{uuid4().hex}.json"
                if _create_owned(reg, token, _payload(reg, "reader")) is not None:
                    _emit("reader_acquire", token=token.name)
                    return token
        _emit("reader_wait")
        if not _sleep_until(deadline):
            raise IndexBusy("reader wait timed out under writer intent")


@contextlib.contextmanager
def read_lease(path: os.PathLike[str] | str, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Hold a shared-reader lease over *path* for the ``with`` block, parsing nothing.

    This is the primitive :func:`read_index` is built from, and the form a caller
    that does its own loading, validation, and schema self-check should use — the
    query engine, for one. Leasing and parsing are separate concerns: a caller that
    parses the index itself and also enters through :func:`read_index` decodes the
    whole file twice per query and, worse, gets the gate's bare
    :func:`json.loads` failure instead of its own diagnosable error path.

    Keep the body minimal — a live writer's intent already blocks *new* readers, but
    the writer still drains this lease before its exclusive phase. In particular do
    not spawn a process that will itself want a lease from inside this block (see the
    module docstring's lock-order-inversion note).

    Args:
        path: index file path; coordination is resolved beside it.
        timeout: bounded wait before :class:`IndexBusy` (never a stale read).

    Yields:
        ``None`` — the lease is the value; the caller reads *path* itself.

    Raises:
        IndexBusy: the reader wait deadline expired under writer intent.
        CoordinationUnavailable: the coordination root is unwritable.

    Examples:
        >>> import tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "idx.json")
        >>> with read_lease(p, timeout=5):
        ...     os.path.exists(p)
        False
    """
    index_path = Path(path)
    coord = _ensure_coord(index_path)
    reg = _registry_for(coord)
    deadline = time.monotonic() + timeout
    token = _acquire_reader_token(reg, coord, deadline)
    try:
        yield
    finally:
        # Emit before the drop: the token is a unique name (no recycle race), and
        # emitting first makes the append log show reader_release strictly before
        # any writer that drains this token can enter its exclusive phase.
        _emit("reader_release")
        _release_owned(reg, token)


@contextlib.contextmanager
def read_index(path: os.PathLike[str] | str, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[Optional[dict]]:
    """Acquire a shared-reader lease and yield the parsed index (or ``None``).

    A convenience wrapper over :func:`read_lease` for callers with no loader of
    their own; the token is held for the whole ``with`` block, covering the parse
    and the caller's in-memory use.

    Args:
        path: index file path; coordination is resolved beside it.
        timeout: bounded wait before :class:`IndexBusy` (never a stale read).

    Yields:
        The parsed index dict, or ``None`` when the index file is absent.

    Raises:
        IndexBusy: the reader wait deadline expired under writer intent.
        CoordinationUnavailable: the coordination root is unwritable.
        IndexUnreadable: the index file exists but is not valid JSON.

    Examples:
        >>> import tempfile, os, json
        >>> d = tempfile.mkdtemp()
        >>> p = os.path.join(d, "idx.json")
        >>> _ = open(p, "w").write(json.dumps({"schema": 1}))
        >>> with read_index(p, timeout=5) as data:
        ...     data["schema"]
        1
    """
    index_path = Path(path)
    with read_lease(index_path, timeout=timeout):
        _emit("index_open")
        data = _load_index(index_path)
        _emit("index_close")
        yield data


def _load_index(index_path: Path) -> Optional[dict]:
    """Read and parse the index JSON, or ``None`` when the file is absent.

    Raises:
        IndexUnreadable: the file exists but does not parse as JSON. Letting the
            raw :class:`json.JSONDecodeError` escape instead produced a traceback
            in every consumer that only guards the gate's own exception types,
            which the capability contract forbids for a corrupt index.
    """
    try:
        with open(index_path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, ValueError) as exc:
        raise IndexUnreadable(
            f"index is not valid JSON: {index_path} ({exc}); rebuild it with `codemap-py index`"
        ) from exc


# ── writer path ─────────────────────────────────────────────────────────────
def write_index(
    path: os.PathLike[str] | str,
    build_fn: Callable[[Path], Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    writer_version: Optional[int] = None,
) -> Any:
    """Run *build_fn* under an exclusive, writer-preferred lease.

    Sequence: acquire writer intent under the mutex (blocking later readers),
    drain earlier readers, enter the exclusive phase, run ``build_fn(index_path)``
    (freshness recheck, scan/build, validate, atomic publish, cleanup), then
    release intent under the mutex after cleanup so later readers cannot enter
    during the transition.

    Args:
        path: index file path; coordination is resolved beside it.
        build_fn: callable receiving the index path; owns publish and validation.
        timeout: bounded wait before :class:`IndexBusy`.
        writer_version: this writer's schema generation. When given, an on-disk
            index from a strictly newer generation is refused instead of being
            downgraded; ``None`` skips the check.

    Returns:
        Whatever ``build_fn`` returns.

    Raises:
        IndexBusy: intent acquisition or reader drain exceeded the deadline.
        CoordinationUnavailable: the coordination root is unwritable.
        VersionSkewRefused: the on-disk index is newer than *writer_version*.

    Examples:
        >>> import tempfile, os
        >>> d = tempfile.mkdtemp()
        >>> p = os.path.join(d, "idx.json")
        >>> def build(target):
        ...     atomic_publish(target, b'{"schema": 2}')
        ...     return "built"
        >>> write_index(p, build, timeout=5)
        'built'
    """
    index_path = Path(path)
    coord = _ensure_coord(index_path)
    reg = _registry_for(coord)
    writer_path = coord / _WRITER_NAME
    readers_dir = coord / _READERS_NAME
    deadline = time.monotonic() + timeout
    _acquire_writer_intent(reg, writer_path, deadline)
    try:
        _drain_readers(reg, readers_dir, deadline)
        _refuse_incompatible_generation(index_path, writer_version)
        _emit("writer_exclusive_enter")
        try:
            _clean_orphan_temps(index_path)
            return build_fn(index_path)
        finally:
            _emit("writer_exclusive_exit")
    finally:
        _release_writer_intent(reg, writer_path)
        _emit("writer_release")


def _release_writer_intent(reg: _Registry, writer_path: Path) -> None:
    """Release own writer intent without masking a build error or leaking the lock.

    Runs from ``write_index``'s ``finally``, so it must never raise (a raise here
    would replace the real build/drain exception). Two-stage, contract-faithful:

    * Preferred: reacquire the registry mutex (bounded) and remove the intent
      atomically. Removal is done under the mutex because ``writer.json`` is a
      *fixed* name — the registry removes writer intent only under its
      mutex" clause guards the name-recycle race where a competing writer creates
      a fresh intent between our unlock and unlink and we would otherwise delete
      *its* file. (That clause governs fixed-name / foreign-token cleanup; a
      reader's own token is a unique name and needs no mutex to release.)
    * Fallback, when the mutex cannot be reacquired within the bound: still drop
      the OS handle lock so waiters can reclaim the intent by liveness — never a
      lock leak — but do NOT unlink by path (a competing writer may have recycled
      the name). The stale, now-unlocked file is reclaimed under the next
      probe's mutex. Any secondary error is emitted as a degraded event and
      suppressed, never propagated.
    """
    try:
        with reg.mutex(time.monotonic() + _RELEASE_TIMEOUT):
            _release_owned(reg, writer_path)  # unlock + close + unlink, atomic vs probes
            return
    except IndexBusy:
        _emit("writer_release_degraded", reason="mutex_timeout")
    except OSError as exc:  # pragma: no cover - defensive; unlink/close already suppress
        _emit("writer_release_degraded", reason=repr(exc))
    fd = reg.pop_owned(writer_path)
    if fd is not None:
        _os_unlock(fd)
        _safe_close(fd)


def _acquire_writer_intent(reg: _Registry, writer_path: Path, deadline: float) -> None:
    """Create and lock ``writer.json`` under the mutex, or raise IndexBusy.

    Competing writers serialise if they acquire intent later, or return ``index_busy`` at the deadline; starvation-free
    ordering among writers is not promised.
    """
    while True:
        with reg.mutex(deadline):
            if not _intent_live(reg, writer_path):
                if _create_owned(reg, writer_path, _payload(reg, "writer")) is not None:
                    _emit("writer_intent")
                    return
        _emit("writer_wait")
        if not _sleep_until(deadline):
            raise IndexBusy("writer intent acquisition timed out")


def _drain_readers(reg: _Registry, readers_dir: Path, deadline: float) -> None:
    """Wait until earlier reader tokens drain, or raise IndexBusy.

    Deadline expiry raises rather than proceeding, so the writer never builds over a live reader and no stale read is
    authorised.
    """
    while True:
        with reg.mutex(deadline):
            if _readers_drained(reg, readers_dir):
                return
        if not _sleep_until(deadline):
            raise IndexBusy("reader drain timed out; refusing to build over live readers")


def _clean_orphan_temps(index_path: Path) -> None:
    """Remove crashed-writer temp files beside the index (exclusive phase only).

    Temps are uniquely named ``.<index>.<uuid>.tmp``; a completed publish consumes its own via ``os.replace``, so any
    survivor is from a crash between temporary write and publish. The last complete index is never touched.
    """
    parent = index_path.parent
    prefix = f".{index_path.name}."
    try:
        names = os.listdir(parent)
    except FileNotFoundError:
        return
    for name in names:
        if name.startswith(prefix) and name.endswith(".tmp"):
            _safe_unlink(parent / name)


def _refuse_incompatible_generation(index_path: Path, writer_version: Optional[int]) -> None:
    """Refuse to overwrite an index whose schema generation is newer than the writer's.

    Runs inside the exclusive phase, immediately before ``build_fn`` — the writer
    revalidates the index it is about to replace rather than trusting a check made
    before the drain.

    The comparison is deliberately one-sided. Refuse only when the on-disk
    ``scan_version`` is strictly greater than *writer_version*: that is the case
    where an older tool would silently downgrade a shared index and make the newer
    reader rebuild on its next query, forever. Equal or older on disk is the normal
    refresh path. Absent, non-integer, unreadable, or corrupt on-disk data is not a
    newer generation and must not block the writer — replacing a broken index is
    exactly the repair a caller is asking for.

    Args:
        index_path: index about to be replaced.
        writer_version: the calling writer's schema generation, or ``None`` to skip
            the check (a caller that does not persist a versioned schema).

    Raises:
        VersionSkewRefused: on-disk generation is newer than the writer's.
    """
    if writer_version is None:
        return
    try:
        data = _load_index(index_path)
    except IndexUnreadable:
        return
    if not isinstance(data, dict):
        return
    on_disk = data.get("scan_version")
    if not isinstance(on_disk, int) or on_disk <= writer_version:
        return
    _emit("writer_version_skew_refused", on_disk=on_disk, writer=writer_version)
    raise VersionSkewRefused(
        f"refusing to overwrite a newer index: {index_path} was written by schema "
        f"generation {on_disk}, this writer is generation {writer_version}; "
        "upgrade codemap-py or point CODEMAP_INDEX_DIR elsewhere"
    )


# ── atomic publish helper (for build_fn) ────────────────────────────────────
_WINDOWS_REPLACE_RETRIES = 8
_WINDOWS_REPLACE_DELAY_SECONDS = 0.025


def _replace_with_windows_retry(tmp: Path, target: Path) -> None:
    """Replace a target atomically while retrying transient NTFS sharing failures.

    Windows fails the rename with access-denied / sharing-violation while another process still holds a handle on the
    destination, where POSIX just succeeds. Both files are complete at every moment of the backoff, so a reader racing
    the retry sees the old index or the new one, never a partial one.
    """
    for attempt in range(_WINDOWS_REPLACE_RETRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            transient = sys.platform == "win32" and getattr(exc, "winerror", None) in {5, 32}
            if not transient or attempt == _WINDOWS_REPLACE_RETRIES - 1:
                raise
            time.sleep(_WINDOWS_REPLACE_DELAY_SECONDS * 2**attempt)


def _temp_beside(target: Path) -> Path:
    """Return a unique temp path beside *target*, named so orphans are reclaimable.

    Single source of the temp-name convention. :func:`_clean_orphan_temps` matches exactly this shape (leading dot,
    ``.tmp`` suffix); a writer that invents its own name instead is invisible to the cleaner and leaks one file per
    crash forever. The ``uuid4`` component is what makes concurrent writers safe on the write phase — ``os.replace`` is
    atomic for the rename, never for the bytes written before it, so two writers sharing one temp name interleave into a
    corrupt file.
    """
    return target.parent / f".{target.name}.{uuid4().hex}.tmp"


@contextlib.contextmanager
def publish_stream(index_path: os.PathLike[str] | str) -> Iterator[IO[str]]:
    """Yield a text handle to write the index into, then publish it atomically.

    The streaming counterpart of :func:`atomic_publish`, for a caller that
    serialises incrementally (``json.dump``) instead of holding a finished payload.
    That distinction is not cosmetic at this size: a real index runs to hundreds of
    megabytes, and materialising one as a ``str`` plus a ``bytes`` copy just to hand
    it over would multiply peak memory for no gain.

    On clean exit the handle is flushed, ``fsync``-ed, and renamed over
    *index_path*; on any exception the temp is removed and the exception
    propagates, leaving the previous index untouched.

    Args:
        index_path: destination index path.

    Yields:
        An open text handle for the caller to serialise into.

    Examples:
        >>> import json, tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "idx.json")
        >>> with publish_stream(p) as fh:
        ...     json.dump({"schema": 3}, fh)
        >>> json.load(open(p))["schema"]
        3
    """
    target = Path(index_path)
    tmp = _temp_beside(target)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_windows_retry(tmp, target)
    except BaseException:
        _safe_unlink(tmp)
        raise


def atomic_publish(index_path: os.PathLike[str] | str, data: bytes) -> None:
    """Atomically publish *data* to *index_path* via a uniquely named temp.

    The temp stays beside the target for a same-filesystem ``os.replace`` and is
    consumed by it, so no observer sees a partial index. Naming comes from
    :func:`_temp_beside`, so a crash before ``replace`` is recoverable: the next
    writer's exclusive phase reclaims the orphan.

    The payload is ``fsync``-ed before the rename. Without that, a crash around the
    rename can leave the file published under the real name with unflushed tail
    bytes — a structurally invalid index, which is worse than no index at all.

    Args:
        index_path: destination index path.
        data: complete index bytes to publish.

    Examples:
        >>> import tempfile, os
        >>> p = os.path.join(tempfile.mkdtemp(), "idx.json")
        >>> atomic_publish(p, b'{"schema": 1}')
        >>> open(p, "rb").read()
        b'{"schema": 1}'
    """
    target = Path(index_path)
    tmp = _temp_beside(target)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_windows_retry(tmp, target)
    except BaseException:
        _safe_unlink(tmp)
        raise
