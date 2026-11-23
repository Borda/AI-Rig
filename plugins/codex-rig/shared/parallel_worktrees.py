"""Manage approval-bound generated-fixture and remediation worktree lifecycles.

## Purpose

Preserve the narrow generated-fixture lifecycle and enforce the frozen code-remediate-local production-write boundary
without promoting generic parallel writes.

## Scope

Validate exact frozen plan and approval bytes, create detached managed child worktrees, verify parent-joined handovers
against observed Git changes, derive hash-bound patches, integrate them deterministically, apply one checked source
bundle or fast-forward verified topic commits, and remove only successful worktrees after durable evidence exists. The
module rejects edits to approved source paths, plan drift, unauthorized child commits, untracked or undeclared paths,
symlink traversal, mutable integration drift, partial joins, and ambiguous rollback.

## Usage

Generated-fixture callers use the existing Python functions. Code-remediate invokes explicit lifecycle subcommands after
a consumer-specific plan digest is approved.

## Outputs

Private JSON lifecycle state, source-local child and integration evidence, parent-derived forward and rollback patches,
and JSON CLI results. Successful cleanup removes managed worktrees without force; failure evidence stays available for
inspection.

## Failure

``PilotError`` stops dispatch, integration, source application, cleanup, or promotion. Failed, conflicted, cancelled,
drifted, or cleanup-uncertain worktrees remain for explicit resolution. Operational path and postcondition containment
does not claim per-child capability isolation or global atomicity. Source application holds an exclusive per-run apply
lock across its write, rollback and record, so a second apply of the same run stops with ``lifecycle-apply-locked``.

## Used by

Codex Rig's generated-fixture acceptance proof, code-remediate production boundary, artifact validation, and focused
installed-package tests. This module is not a scheduler, general worktree manager, or independent authority source.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import errno
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

_DIFF_SPEC = importlib.util.spec_from_file_location(
    "codex_rig_worktree_diff", Path(__file__).with_name("collect_diff.py")
)
if _DIFF_SPEC is None or _DIFF_SPEC.loader is None:
    raise ImportError("canonical diff arguments unavailable")
_DIFF_MODULE = importlib.util.module_from_spec(_DIFF_SPEC)
_DIFF_SPEC.loader.exec_module(_DIFF_MODULE)

#: Module logger for worktree pilot lifecycle events.
LOGGER = logging.getLogger(__name__)
#: Path components every generated develop pilot path must begin with.
_GENERATED_PREFIX = (".reports", "codex", "develop")
#: Path components every code-remediate pilot path must begin with.
_CODE_REMEDIATE_PREFIX = (".reports", "codex", "code-remediate")
#: Authority a pilot plan must request exactly: serial edits plus one local pilot, no network or remote changes.
_EXPECTED_AUTHORITY = {
    "serial_repository_edits": True,
    "one_local_parallel_write_pilot": True,
    "pilot_scope": "generated-fixture-repository-only",
    "network": "denied-by-plan",
    "external_or_paid_parent_process": False,
    "remote_mutation": False,
    "user_data_deletion": False,
    "automatic_retry_count": 0,
    "general_parallel_write_enablement": False,
}
#: Scope an approval must grant exactly, matching the requested authority with network recorded as plain `False`.
_EXPECTED_APPROVAL_SCOPE = {**_EXPECTED_AUTHORITY, "network": False}


class PilotError(RuntimeError):
    """Report one fail-closed generated worktree lifecycle violation."""


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of exact file bytes."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PilotError("evidence-file-unreadable") from error


def _load_json(path: Path, label: str) -> dict[str, Any]:
    """Load one bounded JSON object used as lifecycle authority or state."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, RecursionError) as error:
        raise PilotError(f"{label}-invalid") from error
    if not isinstance(payload, dict):
        raise PilotError(f"{label}-invalid")
    return payload


def _persist(path: Path, payload: dict[str, Any]) -> None:
    """Atomically and durably persist LF-terminated private lifecycle evidence.

    The temporary file is flushed to disk before it replaces the state, so a crash right after the replace never leaves
    an empty or partial state, and the directory is flushed afterwards so the replace itself survives a crash
    (``_fsync_directory``). A regular temporary file left beside the state is an earlier save that died before its
    atomic replace. It is overwritten unread: the committed state stays authoritative, and resumed transitions reconcile
    from Git.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise PilotError("state-path-symlink-forbidden")
    if temporary.exists() and not temporary.is_file():
        raise PilotError("state-temporary-exists")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    # os.replace, not Path.replace: Python 3.10's pathlib binds os.replace at import, so a host-level spy never sees it.
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory's entries to disk on POSIX hosts, so a rename inside it survives a crash.

    Best effort: native Windows cannot open a directory for flushing, so it is skipped there, and some POSIX file
    systems reject flushing a directory. Such a failure is logged, never raised, because the replace has already
    happened and the saved state is visible.
    """
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        LOGGER.warning("Lifecycle directory %s was not flushed: %s", directory, error)
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        LOGGER.warning("Lifecycle directory %s was not flushed: %s", directory, error)
    finally:
        os.close(descriptor)


def _json_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` as it reads back once saved, detached from later in-place changes."""
    return json.loads(json.dumps(payload))


class _GuardedStateFile:
    """Save one invocation's lifecycle state only while the file still holds what it last read or saved.

    Two invocations can load the same state. A save therefore rereads the file first and stops with
    ``lifecycle-state-concurrent-update`` when another process changed it since this invocation's last read or save,
    instead of replacing a transition Git may already have completed, such as a recorded delivery. The comparison is
    against what this invocation read, never against what it is about to write: two applies derive identical intent
    records. The reread and the atomic replace are two steps; source application closes that interval by holding the
    run's apply lock (``_run_apply_lock``) across every compare, write and save, so two applies never interleave there.
    The compare still stops any writer that takes no lock, such as a hand edit or a lock removed by hand.

    A plain class rather than a dataclass: this module is also loaded by file path without a ``sys.modules`` entry,
    which the dataclass decorator requires.
    """

    __slots__ = ("expected", "path")

    def __init__(self, path: Path, expected: dict[str, Any]) -> None:
        """Bind the state file to the content this invocation expects to find there before its next save."""
        self.path = path
        self.expected = expected

    @classmethod
    def loaded(cls, path: Path, state: dict[str, Any]) -> _GuardedStateFile:
        """Guard later saves of ``state`` as just loaded from ``path``, before any in-place change."""
        return cls(path, _json_snapshot(state))

    def require_current(self) -> None:
        """Stop unless the state file still equals this invocation's last read or save.

        Raises:
            PilotError: ``lifecycle-state-concurrent-update`` when another process changed the file.
        """
        if _load_json(self.path, "lifecycle-state") != self.expected:
            raise PilotError("lifecycle-state-concurrent-update")

    def save(self, state: dict[str, Any]) -> None:
        """Persist ``state`` when the file still equals this invocation's last read or save.

        Raises:
            PilotError: ``lifecycle-state-concurrent-update`` when another process changed the file; nothing is
                saved.
        """
        self.require_current()
        _persist(self.path, state)
        self.expected = _json_snapshot(state)


#: Win32 ``PROCESS_QUERY_LIMITED_INFORMATION``: the least access right that opens a handle to another process.
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: Win32 ``ERROR_INVALID_PARAMETER``: what ``OpenProcess`` reports for a process ID that no process holds.
_WIN_ERROR_INVALID_PARAMETER = 87
#: Win32 ``STILL_ACTIVE``: the exit code ``GetExitCodeProcess`` reports while the process runs.
_WIN_STILL_ACTIVE = 259
#: Largest process ID a lock record may name; any other value leaves the record unreadable and the lock held.
_MAX_LOCK_PID = 2**31 - 1


def _windows_kernel32() -> Any:
    """Load ``kernel32`` with its own last-error slot and the prototypes the liveness probe calls (native Windows)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def _windows_process_alive(pid: int) -> bool:
    """Return whether a Windows process ID still names a running process, counting every unclear answer as running.

    ``os.kill(pid, 0)`` is no probe on Windows: Python turns it into a console control event or ``TerminateProcess``. A
    handle opened with the least query right answers instead. Only ``ERROR_INVALID_PARAMETER`` from ``OpenProcess`` (no
    such process) or a readable exit code other than ``STILL_ACTIVE`` proves the process ended. Any other failure, and a
    process that exited with code 259, count as running, so a lock is never taken from a holder the probe missed.
    """
    kernel32 = _windows_kernel32()
    handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() != _WIN_ERROR_INVALID_PARAMETER
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == _WIN_STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_alive(pid: int) -> bool:
    """Return whether a POSIX process ID names an existing process, counting every unclear answer as existing.

    Signal 0 checks for the process without delivering anything. ``PermissionError`` means it exists under another user,
    and a zombie exists until its parent reaps it; any other probe failure proves nothing.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _process_alive(pid: int) -> bool:
    """Return whether ``pid`` names a live process on this host, using the probe that is harmless on this platform."""
    return _windows_process_alive(pid) if os.name == "nt" else _posix_process_alive(pid)


def _lock_record() -> tuple[str, str]:
    """Return a fresh owner token and the lock text naming it, this process, this host and when the lock was taken."""
    token = secrets.token_hex(16)
    record = {
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "token": token,
    }
    return token, json.dumps(record, sort_keys=True) + "\n"


def _lock_field(text: str, key: str) -> object:
    """Return one field of lock text, or ``None`` when the text is not a JSON object, as an interrupted write leaves."""
    try:
        record = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return record.get(key) if isinstance(record, dict) else None


#: Longest text kept from any one lock-record field when the holder is quoted in a stop message.
_MAX_LOCK_FIELD_TEXT = 256


def _lock_holder_summary(text: str) -> str:
    """Return the holder's process ID, host and lock time as compact JSON, safe to quote in a stop message.

    The lock file is plain data anyone could write, so only an integer ID and bounded strings are kept, and JSON
    encoding escapes any control character.
    """
    summary: dict[str, object] = {}
    for key in ("host", "pid", "started_at"):
        value = _lock_field(text, key)
        if type(value) is int or isinstance(value, str):
            summary[key] = value[:_MAX_LOCK_FIELD_TEXT] if isinstance(value, str) else value
    return json.dumps(summary, sort_keys=True)


def _require_abandoned_lock(text: str) -> None:
    """Stop unless lock text names a holder on this host whose process no longer exists.

    A holder on another host may still run there, and its process IDs mean nothing here, so it stops with its own code
    and the holder's record, letting the operator check that host before removing the lock. Everything else also keeps
    the lock held: unreadable or partly written text, and any process the probe still sees, including an unrelated one
    that reused the ID.

    Raises:
        PilotError: ``lifecycle-apply-locked-foreign-host:<holder record>`` for a holder on another host, and
            ``lifecycle-apply-locked`` for any other holder that may still run.
    """
    host = _lock_field(text, "host")
    if isinstance(host, str) and host != socket.gethostname():
        raise PilotError(f"lifecycle-apply-locked-foreign-host:{_lock_holder_summary(text)}")
    pid = _lock_field(text, "pid")
    if host is None or type(pid) is not int or not 0 < pid <= _MAX_LOCK_PID or _process_alive(pid):
        raise PilotError("lifecycle-apply-locked")


def _read_lock_text(path: Path) -> str | None:
    """Return a lock file's text, or ``None`` when no lock exists.

    Raises:
        PilotError: ``lifecycle-apply-lock-invalid`` when the path is a symbolic link or not a regular file, and
            ``lifecycle-apply-locked`` when an existing lock cannot be read, since its holder may still run.
    """
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise PilotError("lifecycle-apply-lock-invalid")
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PilotError("lifecycle-apply-locked") from error


def _create_lock_file(path: Path, text: str) -> bool:
    """Create ``path`` holding ``text`` only when nothing exists there, and return whether this call created it.

    ``O_CREAT | O_EXCL`` creates atomically on POSIX and native Windows alike and never follows a symbolic link at
    ``path``, so exactly one of several racing creators wins.

    Raises:
        PilotError: ``lifecycle-apply-lock-unavailable`` when the file can be neither created nor found, or its record
            cannot be written; a partly written file is removed again.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    # Native Windows has no O_NOFOLLOW and raises PermissionError, not FileExistsError,
    # for a directory: an occupied path is reported as existing so the caller classifies it,
    # and a dangling link is never followed to create its target.
    if os.path.lexists(path):
        return False
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    except OSError as error:
        raise PilotError("lifecycle-apply-lock-unavailable") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        with contextlib.suppress(OSError):
            path.unlink()
        raise PilotError("lifecycle-apply-lock-unavailable") from error
    return True


def _release_lock_file(path: Path, token: str) -> None:
    """Remove ``path`` only while it still names ``token``, leaving any other content for its owner.

    A removal the host refuses, as native Windows does while another process has the file open, is logged and left: the
    record names this process, so the lock becomes reclaimable once the process exits.
    """
    try:
        text = _read_lock_text(path)
    except PilotError as error:
        LOGGER.warning("Apply lock %s was not released: %s", path, error)
        return
    if text is None or _lock_field(text, "token") != token:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        LOGGER.warning("Apply lock %s was not released: %s", path, error)


def _remove_abandoned_lock(lock: Path, abandoned: str) -> None:
    """Remove ``lock`` only while it still holds ``abandoned``, deciding under the reclaim file so no live lock is lost.

    Every take-over first creates ``<lock>.reclaim`` exclusively and rereads the lock while holding it. Apart from a
    holder releasing its own lock, only a take-over removes a lock, and the exited holder can no longer release; so
    while this take-over holds the reclaim file the lock can change only by being created where none exists. Text equal
    to ``abandoned`` after the reread is therefore still the exited holder's lock, and the removal never takes a lock a
    live holder created in between. A lock already gone is left alone, because any process may create one there at any
    moment. A reclaim file left by a process that died within these few steps is never taken over automatically: the
    operator removes it after confirming that no apply of the run is running.

    Raises:
        PilotError: ``lifecycle-apply-locked`` when another take-over is running or the lock changed hands.
    """
    reclaim = lock.with_name(f"{lock.name}.reclaim")
    token, text = _lock_record()
    if not _create_lock_file(reclaim, text):
        raise PilotError("lifecycle-apply-locked")
    try:
        current = _read_lock_text(lock)
        if current is None:
            return
        if current != abandoned:
            raise PilotError("lifecycle-apply-locked")
        lock.unlink()
    except OSError as error:
        raise PilotError("lifecycle-apply-locked") from error
    finally:
        _release_lock_file(reclaim, token)


def _acquire_apply_lock(lock: Path) -> str:
    """Take the run's apply lock, reclaiming it only from a provably exited holder, and return this owner's token.

    Raises:
        PilotError: ``lifecycle-apply-locked`` while any other holder on this host may still run,
            ``lifecycle-apply-locked-foreign-host:<holder record>`` for a holder on another host,
            ``lifecycle-apply-lock-invalid`` for a lock path that is not a regular file, and
            ``lifecycle-apply-lock-unavailable`` when the lock cannot be created.
    """
    token, text = _lock_record()
    if _create_lock_file(lock, text):
        return token
    held = _read_lock_text(lock)
    if held is not None:
        _require_abandoned_lock(held)
        _remove_abandoned_lock(lock, held)
    if not _create_lock_file(lock, text):
        raise PilotError("lifecycle-apply-locked")
    return token


@contextlib.contextmanager
def _run_apply_lock(state_path: Path) -> Iterator[None]:
    """Hold the run's exclusive lock for the ``with`` body, releasing it however the body ends.

    Source application (with its rollback and resume) and cleanup both run inside this one lock, so no two of them
    write the source, the worktrees or the state of one run at the same time. The lock is the file
    ``.<state file name>.apply-lock`` beside the state, created with ``O_CREAT | O_EXCL`` and holding the owner's random
    token, process ID, host name and the UTC time it was taken. It is not an OS lock such as ``flock``, so it behaves
    the same on POSIX and native Windows, and a holder killed before its ``finally`` runs leaves it behind. Such a lock
    is reclaimed only when it names this host and the probe shows that process no longer exists
    (``_remove_abandoned_lock``). A lock from a live process or that cannot be read is never taken over and stops with
    ``lifecycle-apply-locked``; a lock from another host stops with ``lifecycle-apply-locked-foreign-host`` and the
    holder's record. The operator removes such a lock after confirming the holder is gone. Release removes the file
    only while it still holds this owner's token.

    Raises:
        PilotError: ``lifecycle-apply-locked`` or ``lifecycle-apply-locked-foreign-host:<holder record>`` while another
            holder may still run; the lock is left untouched.
    """
    lock = state_path.with_name(f".{state_path.name}.apply-lock")
    token = _acquire_apply_lock(lock)
    try:
        yield
    finally:
        _release_lock_file(lock, token)


def _git_environment() -> dict[str, str]:
    """Return the host environment without Git redirection overrides."""
    return {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}


def _git(repository: Path, *arguments: str) -> str:
    """Run Git without a shell or inherited Git redirection overrides."""
    if arguments and arguments[0] in {
        "status",
        "diff",
        "hash-object",
        "worktree",
        "restore",
        "apply",
        "merge",
        "cherry-pick",
    }:
        try:
            _DIFF_MODULE.reject_executable_filters(repository, environment=_git_environment())
        except RuntimeError as error:
            raise PilotError(str(error)) from error
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", str(repository), *_safe_git_arguments(arguments)],  # noqa: S607 - explicit local tool
        check=False,
        text=True,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise PilotError(f"git-command-failed:{arguments[0] if arguments else 'unknown'}")
    return completed.stdout.strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    """Run Git and return exact stdout bytes for patch hashing."""
    if arguments and arguments[0] in {"status", "diff", "diff-files", "diff-index", "hash-object"}:
        try:
            _DIFF_MODULE.reject_executable_filters(repository, environment=_git_environment())
        except RuntimeError as error:
            raise PilotError(str(error)) from error
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", str(repository), *_safe_git_arguments(arguments)],  # noqa: S607 - explicit local tool
        check=False,
        text=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise PilotError(f"git-command-failed:{arguments[0] if arguments else 'unknown'}")
    return completed.stdout


def _safe_git_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Disable diff transforms, hooks and filesystem monitors for lifecycle Git reads."""
    canonical = _DIFF_MODULE.canonical_diff_arguments(arguments)
    if "--raw" in arguments:
        canonical = tuple(
            argument for argument in canonical if argument not in {"--unified=3", "--inter-hunk-context=0"}
        )
    return (
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        *canonical,
    )


def _gitfile_target(gitfile: Path) -> Path:
    """Read a real linked-worktree gitfile without following a filesystem alias."""
    if gitfile.is_symlink() or not gitfile.is_file() or gitfile.stat().st_size > 4096:
        raise PilotError("worktree-gitfile-invalid")
    text = gitfile.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir: ") or "\n" in text:
        raise PilotError("worktree-gitfile-invalid")
    return (gitfile.parent / text.removeprefix("gitdir: ")).resolve(strict=True)


def _source_common_directory(source: Path) -> Path:
    """Return the source repository's common Git directory, following a linked source checkout's ``commondir``.

    Raises:
        PilotError: ``worktree-gitfile-source-invalid`` for a symbolic link at the source ``.git``.
        OSError: If the Git directory cannot be read; callers map it to their own stop code.
    """
    source_git = source / ".git"
    if source_git.is_symlink():
        raise PilotError("worktree-gitfile-source-invalid")
    common = source_git.resolve(strict=True) if source_git.is_dir() else _gitfile_target(source_git)
    if (common / "commondir").is_file():
        common = (common / (common / "commondir").read_text(encoding="utf-8").strip()).resolve(strict=True)
    return common


def _verify_worktree_gitfile(source: Path, worktree: Path) -> Path:
    """Bind a child's gitfile to the trusted source common directory and reciprocal registration.

    Returns:
        The worktree's administrative directory under the source common directory.
    """
    try:
        common = _source_common_directory(source)
        administrative = _gitfile_target(worktree / ".git")
        registered = (administrative / "gitdir").read_text(encoding="utf-8").strip()
        registered_common = administrative / (administrative / "commondir").read_text(encoding="utf-8").strip()
        if (
            administrative.parent != common / "worktrees"
            or (administrative / registered).resolve(strict=True) != (worktree / ".git").resolve(strict=True)
            or registered_common.resolve(strict=True) != common
        ):
            raise PilotError("worktree-gitfile-target-mismatch")
    except (OSError, UnicodeError) as error:
        raise PilotError("worktree-gitfile-invalid") from error
    return administrative


def _gitfile_missing(worktree: Path) -> bool:
    """Return whether a worktree directory has lost its ``.git`` entry, as Git's own recursive delete can leave it."""
    gitfile = worktree / ".git"
    return not gitfile.exists() and not gitfile.is_symlink()


def _verify_state_worktrees(
    state: dict[str, Any], workspace: Path, source: Path, *, include_integration: bool = True
) -> None:
    """Verify every retained child and integration registration before parent-side lifecycle reads.

    One worktree is exempt while its ``.git`` file is missing: the one a saved cleanup receipt names as being removed,
    whose ``.git`` file a crash inside ``git worktree remove`` already deleted. The cleanup resume proves its
    registration through Git's administrative directory instead (``_interrupted_removal_admin``); such a receipt exists
    only once the source application is recorded, so every earlier transition checks every worktree.
    """
    receipt = state.get("cleanup")
    pending_removal = receipt.get("removing") if isinstance(receipt, dict) else None
    paths = [node["worktree_path"] for node in state["nodes"]]
    if include_integration and state.get("integration_worktree"):
        paths.append(state["integration_worktree"])
    for relative in paths:
        if state.get("consumer") == "code-remediate":
            _, worktree = _code_remediate_worktree_path(
                workspace, state["source_repository"], relative, "managed-worktree"
            )
        else:
            _, worktree = _workspace_path(workspace, relative, "managed-worktree")
        if worktree.is_dir() and not (relative == pending_removal and _gitfile_missing(worktree)):
            _verify_worktree_gitfile(source, worktree)


def _git_batched_lines(repository: Path, *arguments: str, expected: int) -> list[str] | None:
    """Answer ``expected`` single-line read-only questions with one Git process.

    Returns ``None`` when Git fails or does not answer with exactly one line per question, so the caller can ask each
    question on its own and surface the exact result or error of the unbatched command.
    """
    try:
        output = _git(repository, *arguments)
    except PilotError:
        return None
    lines = [line.strip() for line in output.split("\n")]
    return lines if len(lines) == expected else None


def _git_head_and_tree(repository: Path) -> tuple[str, str]:
    """Return the checked-out commit and its tree identifiers from one Git process."""
    answers = _git_batched_lines(repository, "rev-parse", "HEAD", "HEAD^{tree}", expected=2)
    if answers is None:
        return _git(repository, "rev-parse", "HEAD"), _git(repository, "rev-parse", "HEAD^{tree}")
    return answers[0], answers[1]


def _git_filtered_oid(repository: Path, relative_path: str) -> str:
    """Return Git's clean-filtered object identifier for one working-tree path."""
    path = repository.joinpath(*PurePosixPath(relative_path).parts)
    object_id = _git(repository, "hash-object", f"--path={relative_path}", "--filters", str(path))
    if re.fullmatch(r"[0-9a-f]+", object_id) is None:
        raise PilotError("git-filtered-object-invalid")
    return object_id


def _portable_relative(value: object, label: str) -> str:
    """Normalize a portable repository-relative path and reject Windows aliases."""
    if not isinstance(value, str) or not value or "\x00" in value or any(character in value for character in "*?["):
        raise PilotError(f"{label}-invalid")
    windows = PureWindowsPath(value)
    normalized = value.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if windows.drive or windows.root or posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise PilotError(f"{label}-invalid")
    if any(":" in part for part in posix.parts):
        raise PilotError(f"{label}-invalid")
    if any(part.endswith((".", " ")) for part in posix.parts):
        raise PilotError(f"{label}-nonportable")
    return posix.as_posix()


def _canonical_path(value: str) -> str:
    """Return a Windows-conservative canonical key for collision checks."""
    return "/".join(part.casefold() for part in PurePosixPath(value).parts)


def _workspace_path(workspace_root: Path, value: object, label: str) -> tuple[str, Path]:
    """Resolve a generated workspace-relative path without following symlinks."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    if tuple(parts[: len(_GENERATED_PREFIX)]) != _GENERATED_PREFIX:
        raise PilotError(f"{label}-outside-generated-root")
    current = workspace_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    candidate = workspace_root.joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    return relative, candidate


def _code_remediate_evidence_path(source_repository: Path, value: object, label: str) -> tuple[str, Path]:
    """Resolve source-local remediation evidence below its bounded run root."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    if tuple(parts[: len(_CODE_REMEDIATE_PREFIX)]) != _CODE_REMEDIATE_PREFIX:
        raise PilotError(f"{label}-outside-code-remediate-root")
    current = source_repository
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    candidate = source_repository.joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(source_repository.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-source") from error
    return relative, candidate


def _code_remediate_source_path(workspace_root: Path, value: object, label: str) -> tuple[str, Path]:
    """Resolve the authoritative source inside the workspace without symlink traversal."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    current = workspace_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    source = workspace_root.joinpath(*parts)
    try:
        source.resolve(strict=True).relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    return relative, source


def _code_remediate_worktree_path(
    workspace_root: Path, source_relative: str, value: object, label: str
) -> tuple[str, Path]:
    """Resolve a managed sibling or ignored source-local worktree path."""
    relative = _portable_relative(value, label)
    parts = PurePosixPath(relative).parts
    source_relative, source_path = _code_remediate_source_path(workspace_root, source_relative, "source-repository")
    source = _canonical_path(source_relative)
    candidate = _canonical_path(relative)
    source_parts = PurePosixPath(source_relative).parts
    local_parts = (*source_parts, ".reports", "codex", "code-remediate-worktrees")
    sibling = parts[0] == ".codex-rig-worktrees" and candidate != source and not candidate.startswith(f"{source}/")
    source_local = parts[: len(local_parts)] == local_parts and len(parts) in {
        len(local_parts) + 1,
        len(local_parts) + 2,
    }
    if not sibling and not source_local:
        raise PilotError(f"{label}-not-managed-sibling")
    current = workspace_root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PilotError("managed-path-symlink-forbidden")
    worktree = workspace_root.joinpath(*parts)
    try:
        resolved = worktree.resolve(strict=False)
        resolved.relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    if source_local:
        try:
            resolved.relative_to(source_path.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise PilotError(f"{label}-outside-source") from error
        source_local_relative = PurePosixPath(*parts[len(source_parts) :]).as_posix()
        # Nested worktrees must stay invisible to the authoritative checkout's status and patch boundary.
        try:
            _git(source_path, "check-ignore", "--", source_local_relative)
        except PilotError as error:
            raise PilotError(f"{label}-not-ignored") from error
        return relative, worktree
    try:
        resolved.relative_to(source_path.resolve(strict=True))
    except ValueError:
        return relative, worktree
    except OSError as error:
        raise PilotError(f"{label}-outside-workspace") from error
    raise PilotError(f"{label}-not-managed-sibling")


def _relative_workspace_path(workspace_root: Path, path: Path, label: str) -> str:
    """Return a workspace-relative path without trusting its string form."""
    try:
        relative = path.resolve(strict=False).relative_to(workspace_root.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-workspace") from error
    return relative


def _relative_evidence_path(workspace_root: Path, path: Path, label: str) -> str:
    """Return a generated-root workspace-relative lifecycle evidence path."""
    relative = _relative_workspace_path(workspace_root, path, label)
    _workspace_path(workspace_root, relative, label)
    return relative


def _stage_three(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the unique two-node generated-fixture write stage."""
    if plan.get("schema_version") != 1 or plan.get("requested_authority") != _EXPECTED_AUTHORITY:
        raise PilotError("plan-authority-invalid")
    stages = plan.get("stages")
    if not isinstance(stages, list):
        raise PilotError("plan-stage-invalid")
    matches = [stage for stage in stages if isinstance(stage, dict) and stage.get("stage_id") == "S3"]
    if len(matches) != 1:
        raise PilotError("plan-stage-invalid")
    stage = matches[0]
    if stage.get("mode") != "parallel-write" or stage.get("configured_limit") != 2:
        raise PilotError("plan-stage-invalid")
    nodes = stage.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 2:
        raise PilotError("plan-node-invalid")
    return stage


def _planned_nodes(stage: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the exact two disjoint frozen write buckets without Git."""
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_outputs: set[str] = set()
    seen_locks: set[str] = set()
    for raw_node in stage["nodes"]:
        if not isinstance(raw_node, dict):
            raise PilotError("plan-node-invalid")
        node_id = raw_node.get("node_id")
        if not isinstance(node_id, str) or re.fullmatch(r"[A-Z0-9-]+", node_id) is None or node_id in seen_ids:
            raise PilotError("plan-node-invalid")
        owned = raw_node.get("owned_paths")
        if not isinstance(owned, list) or not owned:
            raise PilotError(f"owned-path-invalid:{node_id}")
        normalized: list[str] = []
        for value in owned:
            path = _portable_relative(value, "owned-path")
            canonical = _canonical_path(path)
            if canonical in seen_paths:
                raise PilotError(f"owned-path-alias:{node_id}")
            if any(
                canonical.startswith(f"{existing}/") or existing.startswith(f"{canonical}/") for existing in seen_paths
            ):
                raise PilotError(f"owned-path-overlap:{node_id}")
            seen_paths.add(canonical)
            normalized.append(path)
        locks = raw_node.get("resource_locks")
        if not isinstance(locks, list) or any(not isinstance(lock, str) or not lock for lock in locks):
            raise PilotError(f"resource-lock-invalid:{node_id}")
        if seen_locks.intersection(locks):
            raise PilotError(f"resource-lock-overlap:{node_id}")
        seen_locks.update(locks)
        output = _portable_relative(raw_node.get("output"), "patch-output")
        if "/" in output or _canonical_path(output) in seen_outputs:
            raise PilotError(f"patch-output-invalid:{node_id}")
        seen_outputs.add(_canonical_path(output))
        seen_ids.add(node_id)
        nodes.append(
            {
                "node_id": node_id,
                "owned_paths": sorted(normalized),
                "resource_locks": list(locks),
                "output": output,
            }
        )
    return sorted(nodes, key=lambda item: item["node_id"])


def _require_tracked_paths(repository: Path, nodes: list[dict[str, Any]]) -> None:
    """Check owned paths in one fresh Git query, retaining per-node failure attribution."""
    paths = list(dict.fromkeys(path for node in nodes for path in node["owned_paths"]))
    try:
        _git(repository, "ls-files", "--error-unmatch", "--", *paths)
    except PilotError:
        pass
    except OSError as error:
        if error.errno != errno.E2BIG and getattr(error, "winerror", None) != 206:
            raise
    else:
        return
    # Failed or oversized batches use the original checks to identify the owning node without caching index state.
    LOGGER.debug("Checking tracked paths individually after an unavailable batch query")
    for node in nodes:
        for path in node["owned_paths"]:
            try:
                _git(repository, "ls-files", "--error-unmatch", "--", path)
            except PilotError as error:
                raise PilotError(f"owned-path-not-tracked:{node['node_id']}") from error


def _validated_nodes(stage: dict[str, Any], repository: Path) -> list[dict[str, Any]]:
    """Require every frozen bucket to remain tracked regular repository content."""
    nodes = _planned_nodes(stage)
    for node in nodes:
        for path in node["owned_paths"]:
            target = repository.joinpath(*PurePosixPath(path).parts)
            if target.is_symlink():
                raise PilotError(f"owned-path-symlink-forbidden:{node['node_id']}")
    _require_tracked_paths(repository, nodes)
    return nodes


def _validate_approval(plan_path: Path, approval_path: Path) -> tuple[dict[str, Any], str, str]:
    """Bind exact plan bytes to one explicit generated-fixture approval record."""
    plan = _load_json(plan_path, "plan")
    approval = _load_json(approval_path, "approval")
    plan_sha256 = _sha256(plan_path)
    if approval.get("plan_sha256") != plan_sha256:
        raise PilotError("plan-approval-digest-mismatch")
    if approval.get("response") != "approve" or approval.get("source") not in {"explicit-input", "user-prompt"}:
        raise PilotError("write-approval-invalid")
    if approval.get("scope") != _EXPECTED_APPROVAL_SCOPE:
        raise PilotError("write-approval-scope-invalid")
    return plan, plan_sha256, _sha256(approval_path)


def _repository_unchanged(repository: Path, baseline_head: str, label: str) -> None:
    """Require an exact clean generated source baseline."""
    if _git(repository, "rev-parse", "HEAD") != baseline_head:
        raise PilotError(f"{label}-head-drift")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PilotError(f"{label}-dirty")


def _retained_worktree_fingerprint(worktree: Path, source: Path) -> dict[str, str]:
    """Fingerprint one retained diagnostic worktree without modifying it."""
    if not worktree.is_dir():
        raise PilotError("prior-attempt-worktree-missing")
    _verify_worktree_gitfile(source, worktree)
    return {
        "head": _git(worktree, "rev-parse", "HEAD"),
        "status_sha256": hashlib.sha256(
            _git_bytes(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        ).hexdigest(),
        "diff_sha256": hashlib.sha256(_git_bytes(worktree, "diff", "--binary", "HEAD", "--")).hexdigest(),
        "untracked_sha256": hashlib.sha256(
            _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
        ).hexdigest(),
    }


def _prior_attempt_fingerprint(
    plan: dict[str, Any], stage: dict[str, Any], workspace: Path
) -> dict[str, object] | None:
    """Fingerprint a declared failed attempt so later work cannot rewrite its diagnostics."""
    prior = plan.get("prior_attempt")
    if prior is None:
        return None
    required = {"runtime_record", "status", "retry_count", "retained_worktree_root"}
    if not isinstance(prior, dict) or set(prior) != required:
        raise PilotError("prior-attempt-invalid")
    if prior.get("status") != "failed-retained" or prior.get("retry_count") != 0:
        raise PilotError("prior-attempt-invalid")
    runtime_relative, runtime = _workspace_path(workspace, prior.get("runtime_record"), "prior-runtime")
    root_relative, root = _workspace_path(workspace, prior.get("retained_worktree_root"), "prior-worktree-root")
    if not runtime.is_file() or not root.is_dir():
        raise PilotError("prior-attempt-invalid")
    worktrees: dict[str, object] = {}
    for node in _planned_nodes(stage):
        relative = f"{root_relative}/{node['node_id']}"
        _, worktree = _workspace_path(workspace, relative, "prior-worktree")
        worktrees[node["node_id"]] = {
            "path": relative,
            **_retained_worktree_fingerprint(
                worktree, _workspace_path(workspace, stage.get("fixture_repository"), "fixture-repository")[1]
            ),
        }
    return {
        "runtime_record": runtime_relative,
        "runtime_sha256": _sha256(runtime),
        "retained_worktree_root": root_relative,
        "worktrees": worktrees,
    }


def prepare_write_pilot(
    *,
    plan_path: Path,
    approval_path: Path,
    workspace_root: Path,
    state_path: Path,
) -> dict[str, object]:
    """Validate authority and create two detached generated child worktrees.

    Args:
        plan_path: Frozen generated-fixture plan inside the workspace.
        approval_path: Explicit approval record bound to the plan bytes.
        workspace_root: Authorized workspace containing the generated evidence root.
        state_path: Private lifecycle JSON artifact written before dispatch.

    Returns:
        Persisted lifecycle state with frozen hashes, baseline, and child paths.

    Raises:
        PilotError: If authority, paths, baseline, or worktree creation is unsafe.
    """
    workspace_root = workspace_root.resolve(strict=True)
    plan, plan_sha256, approval_sha256 = _validate_approval(plan_path, approval_path)
    stage = _stage_three(plan)
    repository_relative, repository = _workspace_path(
        workspace_root, stage.get("fixture_repository"), "fixture-repository"
    )
    worktree_root_relative, worktree_root = _workspace_path(workspace_root, stage.get("worktree_root"), "worktree-root")
    state_relative = _relative_evidence_path(workspace_root, state_path, "state-path")
    if not repository.is_dir() or not (repository / ".git").exists():
        raise PilotError("fixture-repository-invalid")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PilotError("fixture-repository-dirty")
    baseline_head, baseline_tree = _git_head_and_tree(repository)
    nodes = _validated_nodes(stage, repository)
    prior_fingerprint = _prior_attempt_fingerprint(plan, stage, workspace_root)
    if worktree_root.exists():
        raise PilotError("worktree-root-exists")
    worktree_root.mkdir(parents=True)
    state: dict[str, Any] = {
        "schema_version": 1,
        "status": "preparing",
        "workspace_root": str(workspace_root),
        "plan_path": _relative_workspace_path(workspace_root, plan_path, "plan-path"),
        "plan_sha256": plan_sha256,
        "approval_path": _relative_workspace_path(workspace_root, approval_path, "approval-path"),
        "approval_sha256": approval_sha256,
        "state_path": state_relative,
        "repository": repository_relative,
        "worktree_root": worktree_root_relative,
        "baseline_head": baseline_head,
        "baseline_tree": baseline_tree,
        "retry_count": 0,
        "nodes": [],
        "integration_status": "not-started",
        "cleanup_status": "not-started",
    }
    if prior_fingerprint is not None:
        state["prior_attempt_fingerprint"] = prior_fingerprint
    try:
        for node in nodes:
            worktree_relative = f"{worktree_root_relative}/{node['node_id']}"
            worktree = workspace_root.joinpath(*PurePosixPath(worktree_relative).parts)
            _git(repository, "worktree", "add", "--detach", str(worktree), baseline_head)
            state["nodes"].append(
                {
                    **node,
                    "worktree_path": worktree_relative,
                    "created": True,
                    "terminal_status": "pending",
                    "joined": False,
                    "patch_status": "not-collected",
                }
            )
        state["status"] = "prepared"
        _persist(state_path, state)
    except PilotError:
        state["status"] = "prepare-failed"
        _persist(state_path, state)
        raise
    LOGGER.info("Prepared generated worktree pilot with %d nodes", len(nodes))
    return state


def _read_state(state_path: Path) -> tuple[dict[str, Any], Path]:
    """Load lifecycle state and reject a non-generated state location."""
    state = _load_json(state_path, "lifecycle-state")
    workspace_value = state.get("workspace_root")
    if not isinstance(workspace_value, str):
        raise PilotError("lifecycle-state-invalid")
    workspace = Path(workspace_value)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise PilotError("lifecycle-state-invalid")
    if state.get("schema_version") == 1:
        state_relative, expected_state = _workspace_path(workspace, state.get("state_path"), "state-path")
    elif _is_code_remediate_state(state):
        _, source = _code_remediate_source_path(workspace, state.get("source_repository"), "source-repository")
        state_relative, expected_state = _code_remediate_evidence_path(source, state.get("state_path"), "state-path")
    else:
        raise PilotError("lifecycle-state-schema-invalid")
    if state_relative != state.get("state_path") or expected_state.resolve(strict=False) != state_path.resolve(
        strict=False
    ):
        raise PilotError("lifecycle-state-path-mismatch")
    return state, workspace


def _state_nodes(state: dict[str, Any], nodes: list[dict[str, Any]], workspace: Path, state_path: Path) -> None:
    """Compare state node paths and outputs with the frozen plan-derived values."""
    persisted = state.get("nodes")
    if not isinstance(persisted, list) or len(persisted) != len(nodes):
        raise PilotError("lifecycle-state-node-drift")
    for planned, recorded in zip(nodes, persisted, strict=True):
        if not isinstance(recorded, dict):
            raise PilotError("lifecycle-state-node-drift")
        if any(recorded.get(key) != planned[key] for key in ("node_id", "owned_paths", "resource_locks", "output")):
            raise PilotError("lifecycle-state-node-drift")
        expected_worktree = f"{state['worktree_root']}/{planned['node_id']}"
        _, worktree = _workspace_path(workspace, recorded.get("worktree_path"), "child-worktree-path")
        if recorded.get("worktree_path") != expected_worktree or worktree != workspace.joinpath(
            *PurePosixPath(expected_worktree).parts
        ):
            raise PilotError("lifecycle-state-node-drift")
        if "patch_path" in recorded:
            expected_patch = _relative_evidence_path(workspace, state_path.parent / planned["output"], "patch-path")
            if recorded.get("patch_path") != expected_patch:
                raise PilotError("lifecycle-state-patch-path-drift")
            _workspace_path(workspace, recorded["patch_path"], "patch-path")


def _rederive_authority(
    state: dict[str, Any], workspace: Path, state_path: Path
) -> tuple[Path, Path, list[dict[str, Any]]]:
    """Rebuild frozen authority and managed paths before a lifecycle Git transition."""
    plan_relative = _portable_relative(state.get("plan_path"), "plan-path")
    approval_relative = _portable_relative(state.get("approval_path"), "approval-path")
    plan_path = workspace.joinpath(*PurePosixPath(plan_relative).parts)
    approval_path = workspace.joinpath(*PurePosixPath(approval_relative).parts)
    if _relative_workspace_path(workspace, plan_path, "plan-path") != plan_relative:
        raise PilotError("lifecycle-state-plan-path-drift")
    if _relative_workspace_path(workspace, approval_path, "approval-path") != approval_relative:
        raise PilotError("lifecycle-state-approval-path-drift")
    plan, plan_sha256, approval_sha256 = _validate_approval(plan_path, approval_path)
    if plan_sha256 != state.get("plan_sha256"):
        raise PilotError("plan-drift-before-transition")
    if approval_sha256 != state.get("approval_sha256"):
        raise PilotError("approval-drift-before-transition")
    stage = _stage_three(plan)
    nodes = _planned_nodes(stage)
    repository_relative, repository = _workspace_path(workspace, stage.get("fixture_repository"), "fixture-repository")
    worktree_root_relative, worktree_root = _workspace_path(workspace, stage.get("worktree_root"), "worktree-root")
    if repository_relative != state.get("repository") or worktree_root_relative != state.get("worktree_root"):
        raise PilotError("lifecycle-state-root-drift")
    if state.get("retry_count") != 0:
        raise PilotError("lifecycle-state-retry-drift")
    _state_nodes(state, nodes, workspace, state_path)
    _verify_state_worktrees(state, workspace, repository)
    if not isinstance(state.get("baseline_head"), str) or not isinstance(state.get("baseline_tree"), str):
        raise PilotError("lifecycle-state-baseline-invalid")
    if state.get("prior_attempt_fingerprint") != _prior_attempt_fingerprint(plan, stage, workspace):
        raise PilotError("prior-attempt-fingerprint-mismatch")
    return repository, worktree_root, nodes


def _node(state: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Return one unique lifecycle node by identifier."""
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        raise PilotError("lifecycle-state-invalid")
    matches = [node for node in nodes if isinstance(node, dict) and node.get("node_id") == node_id]
    if len(matches) != 1:
        raise PilotError(f"lifecycle-node-invalid:{node_id}")
    return matches[0]


def _nul_paths(output: str) -> list[str]:
    """Return non-empty NUL-separated portable paths from Git output."""
    return [PurePosixPath(value.replace("\\", "/")).as_posix() for value in output.split("\x00") if value]


def _raw_content_updates(worktree: Path, node: dict[str, Any], *, base: str = "HEAD") -> None:
    """Reject staged, mode, type, deletion, rename, and multi-content child changes."""
    if _git_bytes(worktree, "diff", "--cached", "--name-only", "-z"):
        raise PilotError(f"child-index-change-forbidden:{node['node_id']}")
    raw = _git_bytes(worktree, "diff", "--raw", "--no-renames", "-z", base, "--", *node["owned_paths"])
    fields = [field for field in raw.split(b"\0") if field]
    if len(fields) != len(node["owned_paths"]) * 2:
        raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")
    paths: set[str] = set()
    for metadata, raw_path in zip(fields[::2], fields[1::2], strict=True):
        parts = metadata.split()
        if len(parts) != 5 or not parts[0].startswith(b":"):
            raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")
        old_mode, new_mode = parts[0][1:], parts[1]
        old_object, new_object, status = parts[2:]
        path = _portable_relative(raw_path.decode("utf-8", errors="strict"), "changed-path")
        if old_mode != new_mode or status != b"M" or old_object == new_object:
            raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")
        paths.add(path)
    if paths != set(node["owned_paths"]):
        raise PilotError(f"child-patch-shape-forbidden:{node['node_id']}")


def _child_patch(state: dict[str, Any], workspace: Path, node: dict[str, Any]) -> tuple[list[str], bytes]:
    """Return one safe parent-observed child patch and its exact changed paths."""
    node_id = str(node["node_id"])
    worktree = workspace.joinpath(*PurePosixPath(node["worktree_path"]).parts)
    if not worktree.is_dir():
        raise PilotError(f"child-worktree-missing:{node_id}")
    _verify_worktree_gitfile(workspace.joinpath(*PurePosixPath(state["repository"]).parts), worktree)
    if _git(worktree, "rev-parse", "HEAD") != state.get("baseline_head"):
        raise PilotError(f"child-commit-forbidden:{node_id}")
    if _git(worktree, "ls-files", "--others", "--exclude-standard", "-z"):
        raise PilotError(f"child-untracked-path-forbidden:{node_id}")
    changed_paths = sorted(_nul_paths(_git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
    if set(changed_paths) != set(node["owned_paths"]):
        raise PilotError(f"child-owned-path-mismatch:{node_id}")
    _raw_content_updates(worktree, node)
    for relative in changed_paths:
        if worktree.joinpath(*PurePosixPath(relative).parts).is_symlink():
            raise PilotError(f"child-symlink-path-forbidden:{node_id}")
    patch = _git_bytes(worktree, "diff", "--binary", "--full-index", "HEAD", "--", *node["owned_paths"])
    if not patch:
        raise PilotError(f"child-patch-empty:{node_id}")
    return changed_paths, patch


def create_completed_child_handover(
    *, state_path: str | os.PathLike[str], node_id: str, summary: str, verification_path: Path | None = None
) -> dict[str, object]:
    """Create one canonical completed report from the child's frozen worktree.

    This is the policy-compatible child boundary for patch hashing: it uses the same raw Git subprocess bytes that the
    parent join later verifies, without sending ``git diff`` output through an output-rendering shell wrapper.

    Args:
        state_path: Prepared lifecycle state path owned by the parent workflow.
        node_id: Frozen node whose worktree contains the completed edit.
        summary: Concise child result summary, limited to 2,000 characters.
        verification_path: Retained worker receipt required for a grouped commit. It is advisory: the worker writes it
            itself, so the parent checks only its internal consistency and still requires the integration-tip gates.

    Returns:
        The completed handover accepted by ``join_child_handovers``.

    Raises:
        PilotError: If authority drifted, the lifecycle is no longer prepared, the summary is invalid, or the child
            worktree does not contain one safe exact-owned-path patch.
    """
    try:
        normalized_state_path = Path(state_path)
    except TypeError as error:
        raise PilotError("lifecycle-state-path-invalid") from error
    state, workspace = _read_state(normalized_state_path)
    if _is_code_remediate_state(state):
        return _create_code_remediate_child_handover(
            state, workspace, normalized_state_path, node_id, summary, verification_path
        )
    if verification_path is not None:
        raise PilotError("child-advisory-receipt-unexpected")
    _require_authority_unchanged(state, workspace, normalized_state_path)
    if state.get("status") != "prepared":
        raise PilotError("child-handover-after-join-forbidden")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
        raise PilotError(f"child-handover-summary-invalid:{node_id}")
    node = _node(state, node_id)
    changed_paths, patch = _child_patch(state, workspace, node)
    return {
        "node_id": node_id,
        "status": "completed",
        "summary": summary.strip(),
        "changed_paths": changed_paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


def _record_join_failure(
    state_path: Path,
    state: dict[str, Any],
    handovers: list[dict[str, object]],
    reason: str,
) -> None:
    """Persist bounded child-report diagnostics while retaining all worktrees."""
    planned_ids = {str(node["node_id"]) for node in state.get("nodes", []) if isinstance(node, dict)}
    reason_node = reason.rsplit(":", maxsplit=1)[-1]
    received: list[dict[str, str]] = []
    for handover in handovers:
        if not isinstance(handover, dict):
            continue
        report: dict[str, str] = {}
        node_id = handover.get("node_id")
        status = handover.get("status")
        summary = handover.get("summary")
        if isinstance(node_id, str) and node_id in planned_ids:
            report["node_id"] = node_id
        if isinstance(status, str) and status in {"completed", "failed", "cancelled"}:
            report["status"] = status
        if isinstance(summary, str) and summary.strip():
            report["summary"] = summary.strip()[:2_000]
        received.append(report)
    state["status"] = "join-failed"
    state["join_failure"] = {
        "reason": reason,
        "failed_node": reason_node if reason_node in planned_ids else None,
        "received_reports": received,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _persist(state_path, state)
    LOGGER.warning("Child handover join failed: %s", reason)


def join_child_handovers(*, state_path: Path, handovers: list[dict[str, object]]) -> dict[str, object]:
    """Verify both completed child reports against their frozen worktrees.

    Args:
        state_path: Prepared lifecycle state owned by the parent workflow.
        handovers: Exactly one completed result per frozen node. Each result contains ``node_id``, ``status``,
            ``summary``, ``changed_paths``, and the SHA-256 of the canonical Git patch bytes.

    Returns:
        Updated lifecycle state with both verified handovers and a parent join timestamp.

    Raises:
        PilotError: If either report is missing, malformed, unsuccessful, or disagrees with the observed worktree.
    """
    state, workspace = _read_state(state_path)
    if _is_code_remediate_state(state):
        return _join_code_remediate_child_handovers(state_path, state, workspace, handovers)
    repository, _, planned_nodes = _require_authority_unchanged(state, workspace, state_path)
    if state.get("status") != "prepared":
        raise PilotError("child-handovers-incomplete")
    if any(isinstance(node, dict) and "handover" in node for node in state.get("nodes", [])):
        raise PilotError("child-handovers-already-joined")
    try:
        if len(handovers) != len(planned_nodes):
            raise PilotError("child-handovers-incomplete")
        reports: dict[str, dict[str, object]] = {}
        required = {"node_id", "status", "summary", "changed_paths", "patch_sha256"}
        for handover in handovers:
            if not isinstance(handover, dict) or set(handover) != required:
                raise PilotError("child-handover-invalid")
            node_id = handover.get("node_id")
            if not isinstance(node_id, str) or node_id in reports:
                raise PilotError("child-handover-invalid")
            reports[node_id] = handover
        if set(reports) != {str(node["node_id"]) for node in planned_nodes}:
            raise PilotError("child-handovers-incomplete")
        persisted_nodes = state.get("nodes")
        if not isinstance(persisted_nodes, list) or any(not isinstance(node, dict) for node in persisted_nodes):
            raise PilotError("lifecycle-state-invalid")
        verified: dict[str, dict[str, object]] = {}
        for node in persisted_nodes:
            node_id = str(node["node_id"])
            handover = reports[node_id]
            summary = handover.get("summary")
            changed_paths = handover.get("changed_paths")
            patch_sha256 = handover.get("patch_sha256")
            if handover.get("status") != "completed":
                raise PilotError(f"child-handover-not-completed:{node_id}")
            if not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
                raise PilotError(f"child-handover-summary-invalid:{node_id}")
            if not isinstance(changed_paths, list) or any(not isinstance(path, str) for path in changed_paths):
                raise PilotError(f"child-handover-paths-invalid:{node_id}")
            normalized_paths = [_portable_relative(path, "child-handover-path") for path in changed_paths]
            if normalized_paths != node["owned_paths"]:
                raise PilotError(f"child-handover-paths-mismatch:{node_id}")
            if not isinstance(patch_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", patch_sha256) is None:
                raise PilotError(f"child-handover-patch-hash-invalid:{node_id}")
            observed_paths, patch = _child_patch(state, workspace, node)
            if observed_paths != normalized_paths or hashlib.sha256(patch).hexdigest() != patch_sha256:
                raise PilotError(f"child-handover-patch-mismatch:{node_id}")
            verified[node_id] = {
                "status": "completed",
                "summary": summary.strip(),
                "changed_paths": normalized_paths,
                "patch_sha256": patch_sha256,
            }
    except PilotError as error:
        _record_join_failure(state_path, state, handovers, str(error))
        raise
    for node in persisted_nodes:
        node["handover"] = verified[str(node["node_id"])]
        node["terminal_status"] = "completed"
        node["joined"] = True
    _repository_unchanged(repository, state["baseline_head"], "fixture-repository")
    state["status"] = "joined"
    state["joined_at"] = datetime.now(timezone.utc).isoformat()
    _persist(state_path, state)
    LOGGER.info("Joined and verified %d child handovers", len(persisted_nodes))
    return state


def collect_write_patch(
    *,
    state_path: Path,
    node_id: str,
) -> dict[str, object]:
    """Derive one patch after the parent verifies both child handovers.

    Args:
        state_path: Prepared lifecycle state.
        node_id: Frozen node identifier.

    Returns:
        Updated node evidence including parent-derived patch path and SHA-256.

    Raises:
        PilotError: If the child committed, failed, is unjoined, or changed unsafe paths.
    """
    state, workspace = _read_state(state_path)
    if _is_code_remediate_state(state):
        return _collect_code_remediate_patch(state_path, state, workspace, node_id)
    repository, _, _ = _rederive_authority(state, workspace, state_path)
    node = _node(state, node_id)
    if state.get("status") != "joined" or any(
        not isinstance(record, dict)
        or record.get("terminal_status") != "completed"
        or record.get("joined") is not True
        or not isinstance(record.get("handover"), dict)
        or record["handover"].get("status") != "completed"
        for record in state.get("nodes", [])
    ):
        raise PilotError("pilot-children-not-joined")
    if node.get("patch_status") != "not-collected":
        raise PilotError(f"child-patch-already-collected:{node_id}")
    _repository_unchanged(repository, state["baseline_head"], "fixture-repository")
    changed_paths, patch = _child_patch(state, workspace, node)
    handover = node["handover"]
    if (
        handover.get("changed_paths") != changed_paths
        or handover.get("patch_sha256") != hashlib.sha256(patch).hexdigest()
    ):
        raise PilotError(f"child-handover-drift:{node_id}")
    patch_relative = _relative_evidence_path(workspace, state_path.parent / node["output"], "patch-path")
    _workspace_path(workspace, patch_relative, "patch-path")
    # The validators resolve links; adoption must see the recorded name so a symlinked output is refused.
    _write_or_adopt_patch(state_path.parent / node["output"], patch, node_id)
    node.update(
        {
            "terminal_status": "completed",
            "joined": True,
            "changed_paths": changed_paths,
            "patch_path": patch_relative,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "patch_derived_at": datetime.now(timezone.utc).isoformat(),
            "patch_status": "collected",
        }
    )
    _persist(state_path, state)
    LOGGER.info("Collected parent-derived patch for %s", node_id)
    return node


def _require_authority_unchanged(
    state: dict[str, Any], workspace: Path, state_path: Path
) -> tuple[Path, Path, list[dict[str, Any]]]:
    """Rebuild frozen authority and require its source baseline before Git mutation."""
    repository, worktree_root, nodes = _rederive_authority(state, workspace, state_path)
    _repository_unchanged(repository, state["baseline_head"], "fixture-repository")
    return repository, worktree_root, nodes


def integrate_write_pilot(*, state_path: Path) -> dict[str, object]:
    """Apply every joined hash-bound patch to a fresh generated integration worktree.

    Args:
        state_path: Lifecycle state whose two child patches have been collected.

    Returns:
        Updated state with deterministic integration order and exact final hashes.

    Raises:
        PilotError: If authority drifted, a join is missing, or any patch cannot integrate.
    """
    state, workspace = _read_state(state_path)
    if _is_code_remediate_state(state):
        return _integrate_code_remediate_pilot(state_path, state, workspace)
    repository, _, _ = _require_authority_unchanged(state, workspace, state_path)
    nodes = state.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict)
        or node.get("terminal_status") != "completed"
        or node.get("joined") is not True
        or node.get("patch_status") != "collected"
        for node in nodes
    ):
        raise PilotError("pilot-children-not-joined")
    ordered = sorted(nodes, key=lambda item: item["node_id"])
    for node in ordered:
        patch_path = workspace.joinpath(*PurePosixPath(node["patch_path"]).parts)
        if _sha256(patch_path) != node.get("patch_sha256"):
            raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
    integration_relative = f"{state['worktree_root']}/integration"
    _, integration = _workspace_path(workspace, integration_relative, "integration-worktree")
    if integration.exists():
        raise PilotError("integration-worktree-exists")
    _require_authority_unchanged(state, workspace, state_path)
    _git(repository, "worktree", "add", "--detach", str(integration), state["baseline_head"])
    state["integration_worktree"] = integration_relative
    state["integration_status"] = "applying"
    _persist(state_path, state)
    applied: list[str] = []
    try:
        for node in ordered:
            patch_path = workspace.joinpath(*PurePosixPath(node["patch_path"]).parts)
            try:
                _require_authority_unchanged(state, workspace, state_path)
                if _sha256(patch_path) != node.get("patch_sha256"):
                    raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
                _git(integration, "apply", "--check", str(patch_path))
                _require_authority_unchanged(state, workspace, state_path)
                if _sha256(patch_path) != node.get("patch_sha256"):
                    raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
                _git(integration, "apply", str(patch_path))
            except PilotError as error:
                state["integration_status"] = "failed"
                state["integration_failed_node"] = node["node_id"]
                _persist(state_path, state)
                raise PilotError(f"patch-integration-failed:{node['node_id']}") from error
            applied.append(node["node_id"])
        if _git(integration, "ls-files", "--others", "--exclude-standard", "-z"):
            raise PilotError("integration-untracked-path-forbidden")
        changed = set(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
        expected = {path for node in ordered for path in node["owned_paths"]}
        if changed != expected:
            raise PilotError("integration-changed-path-mismatch")
        final_hashes = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in sorted(expected)}
        state.update(
            {
                "status": "integrated",
                "integration_status": "passed",
                "integration_order": applied,
                "integration_final_sha256": final_hashes,
            }
        )
        _persist(state_path, state)
    except PilotError:
        if state.get("integration_status") != "failed":
            state["integration_status"] = "failed"
            _persist(state_path, state)
        raise
    LOGGER.info("Integrated generated patches in order %s", applied)
    return state


def cleanup_write_pilot(*, state_path: Path) -> dict[str, object]:
    """Remove successful generated worktrees after preserving patch and integration evidence.

    Args:
        state_path: Persisted successful lifecycle state.

    Returns:
        Updated state with per-worktree cleanup commands and postconditions.

    Raises:
        PilotError: If integration did not pass or any non-force cleanup is uncertain.
    """
    state, workspace = _read_state(state_path)
    repository, _, _ = _require_authority_unchanged(state, workspace, state_path)
    if state.get("integration_status") != "passed" or not state.get("integration_order"):
        raise PilotError("cleanup-before-integration-forbidden")
    integration_relative = f"{state['worktree_root']}/integration"
    if state.get("integration_worktree") != integration_relative:
        raise PilotError("lifecycle-state-integration-path-drift")
    _workspace_path(workspace, integration_relative, "integration-worktree")
    records: list[dict[str, str]] = []
    cleanup_targets = [(node["node_id"], node["worktree_path"], list(node["owned_paths"])) for node in state["nodes"]]
    cleanup_targets.append(
        (
            "integration",
            state["integration_worktree"],
            sorted({path for node in state["nodes"] for path in node["owned_paths"]}),
        )
    )
    state["cleanup_status"] = "in-progress"
    state["cleanup"] = records
    _persist(state_path, state)
    for label, relative, owned_paths in cleanup_targets:
        _, worktree = _workspace_path(workspace, relative, "cleanup-worktree")
        record = {
            "worktree": relative,
            "command": "git worktree remove",
            "result": "pending",
            "postcondition": "present",
        }
        records.append(record)
        _persist(state_path, state)
        try:
            # Patches are durable; restore only the generated bucket paths so non-force removal can succeed.
            _require_authority_unchanged(state, workspace, state_path)
            _git(worktree, "restore", "--staged", "--worktree", "--source=HEAD", "--", *owned_paths)
            _git(repository, "worktree", "remove", str(worktree))
        except PilotError as error:
            record["result"] = "failed"
            state["cleanup_status"] = "failed"
            _persist(state_path, state)
            raise PilotError(f"cleanup-failed:{label}") from error
        record["result"] = "removed"
        record["postcondition"] = "absent" if not worktree.exists() else "present"
        if worktree.exists():
            state["cleanup_status"] = "failed"
            _persist(state_path, state)
            raise PilotError(f"cleanup-postcondition-failed:{label}")
        _persist(state_path, state)
    state["status"] = "complete"
    state["cleanup_status"] = "removed"
    _persist(state_path, state)
    LOGGER.info("Removed %d successful generated worktrees", len(records))
    return state


def _is_code_remediate_state(state: dict[str, Any]) -> bool:
    """Return whether state belongs to a supported remediation lifecycle."""
    return state.get("schema_version") in (2, 3) and state.get("consumer") == "code-remediate"


#: Pending-operation marker files inside the Git directory as (marker name, label), in reporting order.
_PENDING_OPERATIONS = (
    ("MERGE_HEAD", "merging"),
    ("CHERRY_PICK_HEAD", "cherry-picking"),
    ("REVERT_HEAD", "reverting"),
)


def _git_operation(repository: Path, *, allow_patch_operations: bool = False) -> str | None:
    """Return a pending Git operation that makes source state unsafe to apply."""
    queries = [argument for name, _ in _PENDING_OPERATIONS for argument in ("--git-path", name)]
    # One process answers every marker path plus the Git directory; None sends each question to its own process.
    answers = _git_batched_lines(repository, "rev-parse", *queries, "--git-dir", expected=len(_PENDING_OPERATIONS) + 1)
    for index, (name, label) in enumerate(_PENDING_OPERATIONS):
        marker = answers[index] if answers is not None else _git(repository, "rev-parse", "--git-path", name)
        if (repository / marker).is_file() and not (allow_patch_operations and name == "CHERRY_PICK_HEAD"):
            return label
    git_dir = repository / (answers[-1] if answers is not None else _git(repository, "rev-parse", "--git-dir"))
    if (git_dir / "rebase-apply").exists() or (git_dir / "rebase-merge").exists():
        return "rebasing"
    if allow_patch_operations and (git_dir / "sequencer").exists():
        return "sequencing"
    return None


def _require_unchanged_code_remediate_source(
    repository: Path, owned_paths: list[str], baseline_head: str | None = None
) -> None:
    """Reject source operations, HEAD drift, and edits overlapping approved paths."""
    operation = _git_operation(repository)
    if operation is not None:
        raise PilotError(f"source-repository-{operation}")
    if _git_bytes(repository, "ls-files", "--unmerged", "-z"):
        raise PilotError("source-repository-unmerged")
    if baseline_head is not None and _git(repository, "rev-parse", "HEAD") != baseline_head:
        raise PilotError("source-repository-head-drift")
    protected = {_canonical_path(path) for path in owned_paths}
    records = _git_bytes(repository, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    position = 0
    while position < len(records) and records[position]:
        record = records[position]
        if len(record) < 4 or record[2:3] != b" ":
            raise PilotError("source-repository-status-invalid")
        status = record[:2]
        paths = [os.fsdecode(record[3:])]
        # Porcelain -z emits the original name as a second record for renames and copies.
        if b"R" in status or b"C" in status:
            position += 1
            if position >= len(records) or not records[position]:
                raise PilotError("source-repository-status-invalid")
            paths.append(os.fsdecode(records[position]))
        for path in paths:
            candidate = _canonical_path(path)
            if any(
                candidate == owned or candidate.startswith(f"{owned}/") or owned.startswith(f"{candidate}/")
                for owned in protected
            ):
                raise PilotError("source-repository-dirty")
        position += 1


def _validate_code_remediate_approval(plan_path: Path, approval_path: Path) -> tuple[dict[str, Any], str, str]:
    """Bind the plan to its exact four-field dispatch record."""
    plan = _load_json(plan_path, "plan")
    approval = _load_json(approval_path, "approval")
    plan_sha256 = _sha256(plan_path)
    if set(approval) != {"plan_sha256", "prompt_presented", "response", "source"}:
        raise PilotError("write-approval-invalid")
    if approval.get("plan_sha256") != plan_sha256:
        raise PilotError("plan-approval-digest-mismatch")
    source = approval.get("source")
    if approval.get("response") != "approve" or source not in {
        "explicit-input",
        "user-prompt",
        "workflow-default",
    }:
        raise PilotError("write-approval-invalid")
    if not isinstance(approval.get("prompt_presented"), bool):
        raise PilotError("write-approval-invalid")
    if source == "workflow-default" and approval["prompt_presented"]:
        raise PilotError("write-approval-invalid")
    return plan, plan_sha256, _sha256(approval_path)


def _code_remediate_nodes(
    plan: dict[str, Any], repository: Path, evidence_root: str, *, allow_missing: bool = False
) -> list[dict[str, Any]]:
    """Validate the small, closed set of remediation write buckets.

    ``allow_missing`` lets a resumed delivery see an approved file a failed or interrupted ``git apply`` deleted before
    it could write the replacement; the resume restores it from Git or stops, so no missing file is ever delivered.
    """
    buckets = plan.get("work_buckets")
    if not isinstance(buckets, list) or not 2 <= len(buckets) <= 4:
        raise PilotError("plan-node-invalid")
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: dict[str, str] = {}
    seen_outputs: set[str] = set()
    seen_contexts: set[str] = set()
    seen_context_paths: set[str] = set()
    seen_locks: set[str] = set()
    for raw in buckets:
        required_fields = {
            "bucket_id",
            "context_pack_path",
            "context_sha256",
            "execution_mode",
            "owner",
            "output",
            "owned_paths",
            "resource_locks",
            "selected_indexes",
            "verifier",
        }
        grouped = plan["schema_version"] == 3
        if grouped:
            required_fields.add("verification_commands")
        raw_fields = set(raw) if isinstance(raw, dict) else set()
        if (
            not isinstance(raw, dict)
            or not required_fields.issubset(raw_fields)
            or not raw_fields.issubset(required_fields | {"singleton_rationale"})
        ):
            raise PilotError("plan-node-invalid")
        node_id = raw.get("bucket_id")
        if not isinstance(node_id, str) or re.fullmatch(r"[A-Z0-9-]+", node_id) is None or node_id in seen_ids:
            raise PilotError("plan-node-invalid")
        if grouped:
            _require_grouped_verification_commands(raw.get("verification_commands"))
        selected_indexes = raw.get("selected_indexes")
        if (
            not isinstance(selected_indexes, list)
            or not selected_indexes
            or any(not isinstance(index, int) or isinstance(index, bool) for index in selected_indexes)
            or len(selected_indexes) != len(set(selected_indexes))
            or raw.get("execution_mode") != "parallel"
            or not isinstance(raw.get("owner"), str)
            or not raw["owner"]
            or not isinstance(raw.get("verifier"), str)
            or not raw["verifier"]
        ):
            raise PilotError(f"bucket-metadata-invalid:{node_id}")
        rationale = raw.get("singleton_rationale")
        if (len(selected_indexes) == 1) != isinstance(rationale, str) or (isinstance(rationale, str) and not rationale):
            raise PilotError(f"bucket-metadata-invalid:{node_id}")
        owned = raw.get("owned_paths")
        if not isinstance(owned, list) or not owned:
            raise PilotError(f"owned-path-invalid:{node_id}")
        paths: list[str] = []
        bucket_paths: set[str] = set()
        for value in owned:
            path = _portable_relative(value, "owned-path")
            canonical = _canonical_path(path)
            if (
                canonical in bucket_paths
                or (canonical in seen_paths and (seen_paths[canonical] != path or plan.get("schema_version") == 3))
                or any(
                    canonical.startswith(f"{existing}/") or existing.startswith(f"{canonical}/")
                    for existing in seen_paths
                )
            ):
                raise PilotError(f"owned-path-overlap:{node_id}")
            target = repository.joinpath(*PurePosixPath(path).parts)
            if target.is_symlink() or not (target.is_file() or (allow_missing and not target.exists())):
                raise PilotError(f"owned-path-not-regular:{node_id}")
            seen_paths[canonical] = path
            bucket_paths.add(canonical)
            paths.append(path)
        locks = raw.get("resource_locks")
        if not isinstance(locks, list) or any(not isinstance(lock, str) or not lock for lock in locks):
            raise PilotError(f"resource-lock-invalid:{node_id}")
        if seen_locks.intersection(locks):
            raise PilotError(f"resource-lock-overlap:{node_id}")
        context = raw.get("context_sha256")
        if not isinstance(context, str) or re.fullmatch(r"[0-9a-f]{64}", context) is None or context in seen_contexts:
            raise PilotError(f"context-hash-invalid:{node_id}")
        context_relative = _portable_relative(raw.get("context_pack_path"), "context-pack-path")
        _, context_path = _code_remediate_evidence_path(
            repository, f"{evidence_root}/{context_relative}", "context-pack-path"
        )
        canonical_context = _canonical_path(context_relative)
        if canonical_context in seen_context_paths:
            raise PilotError(f"context-pack-path-duplicate:{node_id}")
        if not context_path.is_file():
            raise PilotError(f"context-pack-missing:{node_id}")
        if _sha256(context_path) != context:
            raise PilotError(f"context-pack-digest-mismatch:{node_id}")
        output = _portable_relative(raw.get("output"), "patch-output")
        if "/" in output or _canonical_path(output) in seen_outputs:
            raise PilotError(f"patch-output-invalid:{node_id}")
        seen_ids.add(node_id)
        seen_locks.update(locks)
        seen_contexts.add(context)
        seen_context_paths.add(canonical_context)
        seen_outputs.add(_canonical_path(output))
        nodes.append(
            {
                "node_id": node_id,
                "selected_indexes": list(selected_indexes),
                "owner": raw["owner"],
                "verifier": raw["verifier"],
                "owned_paths": sorted(paths),
                "resource_locks": list(locks),
                "context_pack_path": context_relative,
                "context_sha256": context,
                "output": output,
                "execution_mode": "parallel",
                **({"verification_commands": raw["verification_commands"]} if grouped else {}),
                **({"singleton_rationale": rationale} if rationale is not None else {}),
            }
        )
    if [node["node_id"] for node in nodes] != sorted(node["node_id"] for node in nodes):
        raise PilotError("plan-node-order-invalid")
    _require_tracked_paths(repository, nodes)
    return nodes


def prepare_code_remediate_pilot(
    *, plan_path: Path, approval_path: Path, workspace_root: Path, state_path: Path
) -> dict[str, object]:
    """Prepare bounded remediation worktrees from an approved source baseline."""
    workspace = workspace_root.resolve(strict=True)
    plan, plan_sha256, approval_sha256 = _validate_code_remediate_approval(plan_path, approval_path)
    if (
        not _is_code_remediate_state(plan)
        or (plan.get("schema_version") == 3 and plan.get("commit_mode") != "grouped")
        or plan.get("write_parallel_promoted") is not False
        or plan.get("status") != "frozen-awaiting-explicit-approval"
        or plan.get("rollback_policy") != "approved-paths-if-preapply-baseline-matches"
        or plan.get("cleanup_policy") != "non-force-after-durable-source-application"
        or plan.get("verification_gate") != "code-remediate-shared-quality-gates"
    ):
        raise PilotError("plan-authority-invalid")
    source_relative, source = _code_remediate_source_path(workspace, plan.get("source_repository"), "source-repository")
    if tuple(PurePosixPath(source_relative).parts[: len(_CODE_REMEDIATE_PREFIX)]) == _CODE_REMEDIATE_PREFIX:
        raise PilotError("source-repository-invalid")
    if not source.is_dir() or not (source / ".git").exists():
        raise PilotError("source-repository-invalid")
    plan_relative, expected_plan = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, plan_path, "plan-path"), "plan-path"
    )
    approval_relative, expected_approval = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, approval_path, "approval-path"), "approval-path"
    )
    evidence_root = str(PurePosixPath(plan_relative).parent)
    if PurePosixPath(approval_relative).parent != PurePosixPath(evidence_root):
        raise PilotError("approval-path-outside-run")
    state_relative, expected_state = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, state_path, "state-path"), "state-path"
    )
    if expected_plan != plan_path or expected_approval != approval_path or expected_state != state_path:
        raise PilotError("lifecycle-state-path-mismatch")
    planned_state = _portable_relative(plan.get("state_path"), "planned-state-path")
    if "/" in planned_state or state_relative != f"{evidence_root}/{planned_state}":
        raise PilotError("lifecycle-state-plan-mismatch")
    if state_path.exists() or state_path.is_symlink():
        raise PilotError("lifecycle-state-exists")
    state_temporary = state_path.with_name(f".{state_path.name}.tmp")
    if state_temporary.exists() or state_temporary.is_symlink():
        raise PilotError("state-temporary-exists")
    worktree_relative, worktree_root = _code_remediate_worktree_path(
        workspace, source_relative, plan.get("worktree_root"), "worktree-root"
    )
    nodes = _code_remediate_nodes(plan, source, evidence_root)
    _require_unchanged_code_remediate_source(source, [path for node in nodes for path in node["owned_paths"]])
    baseline_head, baseline_tree = _git_head_and_tree(source)
    if plan.get("baseline_head") != baseline_head or plan.get("baseline_tree") != baseline_tree:
        raise PilotError("source-repository-baseline-mismatch")
    if plan["schema_version"] == 3:
        _require_grouped_commit_choice(plan.get("commit_choice"), plan_path.parent, plan.get("tests_command"))
        tests_command = plan.get("tests_command")
        if (
            not isinstance(tests_command, str)
            or not tests_command.strip()
            or any(char in tests_command for char in "\r\n\0")
        ):
            raise PilotError("grouped-plan-tests-command-invalid")
        _require_grouped_source_branch(source, plan.get("source_branch"))
        if _git_bytes(source, "diff", "--cached", "--name-only", "-z"):
            raise PilotError("source-repository-index-dirty")
    for output in [*(node["output"] for node in nodes), "source-application.patch", "rollback.patch"]:
        _, output_path = _code_remediate_evidence_path(source, f"{evidence_root}/{output}", "lifecycle-output")
        if output_path.exists() or output_path.is_symlink():
            raise PilotError(f"lifecycle-output-exists:{output}")
    if worktree_root.exists():
        raise PilotError("worktree-root-exists")
    worktree_root.mkdir(parents=True)
    state: dict[str, Any] = {
        "schema_version": 2,
        "consumer": "code-remediate",
        "status": "preparing",
        "workspace_root": str(workspace),
        "state_path": state_relative,
        "evidence_root": evidence_root,
        "plan_path": plan_relative,
        "plan_sha256": plan_sha256,
        "approval_path": approval_relative,
        "approval_sha256": approval_sha256,
        "source_repository": source_relative,
        "worktree_root": worktree_relative,
        "baseline_head": baseline_head,
        "baseline_tree": baseline_tree,
        "source": {"baseline_head": baseline_head, "baseline_tree": baseline_tree},
        "nodes": [],
        "verification_gate": "code-remediate-shared-quality-gates",
        "integration_status": "not-started",
        "cleanup_status": "not-started",
        "containment": {"mode": "parent-authoritative-worktrees", "capability_sandbox_verified": False},
    }
    if plan["schema_version"] == 3:
        state["schema_version"] = 3  # literal producers only: the HEAD continuity checker cannot follow a copied value
        state.update({key: plan[key] for key in ("commit_mode", "commit_choice", "source_branch", "tests_command")})
    try:
        for node in nodes:
            worktree_path = f"{worktree_relative}/{node['node_id']}"
            _, worktree = _code_remediate_worktree_path(
                workspace, source_relative, worktree_path, "child-worktree-path"
            )
            _git(source, "worktree", "add", "--detach", str(worktree), baseline_head)
            state["nodes"].append(
                {
                    **node,
                    "worktree_path": worktree_path,
                    "terminal_status": "pending",
                    "joined": False,
                    "patch_status": "not-collected",
                }
            )
        state["status"] = "prepared"
        _persist(state_path, state)
    except PilotError:
        state["status"] = "prepare-failed"
        _persist(state_path, state)
        raise
    LOGGER.info("Prepared code-remediate worktree pilot with %d buckets", len(nodes))
    return state


def _code_remediate_authority(
    state: dict[str, Any], workspace: Path, state_path: Path, *, clean: bool, verify_integration: bool = True
) -> tuple[Path, list[dict[str, Any]]]:
    """Re-derive frozen authority before each parent-controlled transition."""
    if not _is_code_remediate_state(state):
        raise PilotError("lifecycle-state-schema-invalid")
    source_relative, source = _code_remediate_source_path(
        workspace, state.get("source_repository"), "source-repository"
    )
    if source_relative != state.get("source_repository") or not source.is_dir():
        raise PilotError("lifecycle-state-root-drift")
    plan_relative, plan_path = _code_remediate_evidence_path(source, state.get("plan_path"), "plan-path")
    approval_relative, approval_path = _code_remediate_evidence_path(
        source, state.get("approval_path"), "approval-path"
    )
    if plan_relative != state.get("plan_path") or approval_relative != state.get("approval_path"):
        raise PilotError("lifecycle-state-path-mismatch")
    evidence_root = str(PurePosixPath(plan_relative).parent)
    if state.get("evidence_root") != evidence_root or PurePosixPath(approval_relative).parent != PurePosixPath(
        evidence_root
    ):
        raise PilotError("lifecycle-state-root-drift")
    plan, plan_sha256, approval_sha256 = _validate_code_remediate_approval(plan_path, approval_path)
    if plan_sha256 != state.get("plan_sha256") or approval_sha256 != state.get("approval_sha256"):
        raise PilotError("plan-drift-before-transition")
    if plan.get("schema_version") != state["schema_version"]:
        raise PilotError("lifecycle-state-plan-mismatch")
    if state["schema_version"] == 3:
        if plan.get("commit_mode") != "grouped" or any(
            plan.get(key) != state.get(key)
            for key in ("commit_mode", "commit_choice", "source_branch", "tests_command")
        ):
            raise PilotError("lifecycle-state-plan-mismatch")
        _require_grouped_commit_choice(plan.get("commit_choice"), state_path.parent, plan.get("tests_command"))
        _require_grouped_source_branch(source, state.get("source_branch"))
    if _portable_relative(plan.get("source_repository"), "source-repository") != source_relative:
        raise PilotError("lifecycle-state-root-drift")
    planned_state = _portable_relative(plan.get("state_path"), "planned-state-path")
    if (
        "/" in planned_state
        or state.get("state_path") != f"{evidence_root}/{planned_state}"
        or plan.get("verification_gate") != "code-remediate-shared-quality-gates"
        or state.get("verification_gate") != "code-remediate-shared-quality-gates"
    ):
        raise PilotError("lifecycle-state-plan-mismatch")
    worktree_root, _ = _code_remediate_worktree_path(
        workspace, source_relative, plan.get("worktree_root"), "worktree-root"
    )
    if worktree_root != state.get("worktree_root"):
        raise PilotError("lifecycle-state-root-drift")
    nodes = _code_remediate_nodes(plan, source, evidence_root, allow_missing=not clean)
    recorded = state.get("nodes")
    if not isinstance(recorded, list) or len(recorded) != len(nodes):
        raise PilotError("lifecycle-state-node-drift")
    for planned, node in zip(nodes, recorded, strict=True):
        if not isinstance(node, dict) or any(node.get(key) != planned[key] for key in planned):
            raise PilotError("lifecycle-state-node-drift")
        expected = f"{state['worktree_root']}/{planned['node_id']}"
        if node.get("worktree_path") != expected:
            raise PilotError("lifecycle-state-node-drift")
        _code_remediate_worktree_path(workspace, source_relative, expected, "child-worktree-path")
    _verify_state_worktrees(state, workspace, source, include_integration=verify_integration)
    if plan.get("baseline_head") != state.get("baseline_head") or plan.get("baseline_tree") != state.get(
        "baseline_tree"
    ):
        raise PilotError("lifecycle-state-baseline-invalid")
    if clean:
        _require_unchanged_code_remediate_source(
            source, [path for node in nodes for path in node["owned_paths"]], state["baseline_head"]
        )
    elif _git_operation(source) is not None:
        raise PilotError("source-repository-operation-in-progress")
    return source, nodes


def _require_grouped_source_branch(source: Path, expected: object) -> None:
    """Bind delivery to the frozen attached branch even when other refs share its tip."""
    if (
        not isinstance(expected, str)
        or not expected.startswith("refs/heads/")
        or _git(source, "symbolic-ref", "HEAD") != expected
    ):
        raise PilotError("source-repository-branch-drift")


def _require_grouped_commit_choice(choice: object, run: Path, tests_command: object) -> None:
    """Match the frozen choice and full-suite invocation against the upfront record."""
    if (
        not isinstance(choice, dict)
        or set(choice) != {"mode", "source"}
        or choice["mode"] != "grouped"
        or choice["source"] not in ("explicit-input", "user-prompt")
    ):
        raise PilotError("grouped-commit-choice-required")
    try:
        workplan = _grouped_check_path(run, "resolution-workplan.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError, PilotError) as error:
        raise PilotError("grouped-commit-choice-required") from error
    heading = list(re.finditer(r"(?m)^## Upfront Decisions[ \t]*$", workplan))
    if len(heading) != 1:
        raise PilotError("grouped-commit-choice-required")
    section = re.split(r"(?m)^#{1,2} ", workplan[heading[0].end() :], maxsplit=1)[0]
    decisions = re.findall(r"(?m)^Commit choice: (.+)$", section)
    if decisions != [f"group findings by topic ({choice['source']})"]:
        raise PilotError("grouped-commit-choice-required")
    if not isinstance(tests_command, str) or re.findall(r"(?m)^Full test command: (.+)$", section) != [tests_command]:
        raise PilotError("grouped-full-test-command-mismatch")


def _require_grouped_verification_commands(commands: object) -> None:
    """Require an ordered set of exact targeted command lines for a grouped worker."""
    if (
        not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(command, str) or not command.strip() or any(c in command for c in "\r\n\0")
            for command in commands
        )
        or len(commands) != len(set(commands))
    ):
        raise PilotError("child-verification-commands-invalid")


def _grouped_check_path(run: Path, relative: object) -> Path:
    """Resolve a regular worker evidence file below the run without aliases or symlinks."""
    parts = PurePosixPath(_portable_relative(relative, "child-advisory-receipt-path")).parts
    path = run
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise PilotError("child-advisory-receipt-path-invalid")
    if not path.is_file() or not path.resolve().is_relative_to(run.resolve()):
        raise PilotError("child-advisory-receipt-path-invalid")
    return path


def _require_grouped_worker_verification(
    run: Path, reference: object, node_id: str, commit_oid: str, commands: object
) -> None:
    """Check that a worker's self-reported receipt is intact and names the frozen commands for its exact commit.

    The receipt is advisory. The worker that is being judged writes both the receipt and the output files it points
    to, so this check proves only that those bytes agree with each other and have not changed since the handover. It
    does not prove that any command ran or passed. The only execution evidence the parent trusts is the full gate run
    on the integration tip, which ``apply_code_remediate_source`` requires before it moves the source branch. Error
    codes therefore say ``child-advisory-receipt`` rather than claiming verification.

    Args:
        run: Run evidence directory that holds the receipt and its output files.
        reference: Handover record with the receipt's run-relative ``path`` and its ``sha256``.
        node_id: Bucket whose worker wrote the receipt.
        commit_oid: Exact draft commit the receipt must name.
        commands: Frozen targeted command lines, in order.

    Raises:
        PilotError: If the receipt is missing, changed, malformed, names another commit or command list, reports a
            non-zero exit code, or points at output bytes that no longer match its recorded digest.
    """
    _require_grouped_verification_commands(commands)
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise PilotError(f"child-advisory-receipt-required:{node_id}")
    receipt_path = _grouped_check_path(run, reference["path"])
    if _sha256(receipt_path) != reference["sha256"]:
        raise PilotError(f"child-advisory-receipt-drift:{node_id}")
    receipt = _load_json(receipt_path, "child-advisory-receipt")
    checks = receipt.get("checks")
    if (
        set(receipt) != {"node_id", "commit_oid", "checks"}
        or receipt["node_id"] != node_id
        or receipt["commit_oid"] != commit_oid
        or not isinstance(checks, list)
        or len(checks) != len(commands)
    ):
        raise PilotError(f"child-advisory-receipt-invalid:{node_id}")
    for command, check in zip(commands, checks, strict=True):
        if (
            not isinstance(check, dict)
            or set(check) != {"command", "exit_code", "stdout", "stdout_sha256"}
            or check["command"] != command
            or type(check["exit_code"]) is not int
            or check["exit_code"] != 0
            or _sha256(_grouped_check_path(run, check["stdout"])) != check["stdout_sha256"]
        ):
            raise PilotError(f"child-advisory-receipt-invalid:{node_id}")


def _code_remediate_child_patch(
    state: dict[str, Any], workspace: Path, node: dict[str, Any]
) -> tuple[list[str], bytes, str | None]:
    """Observe one remediation child patch, including ignored-file violations."""
    node_id = str(node["node_id"])
    _, worktree = _code_remediate_worktree_path(
        workspace, state["source_repository"], node.get("worktree_path"), "child-worktree-path"
    )
    if not worktree.is_dir():
        raise PilotError(f"child-worktree-missing:{node_id}")
    _verify_worktree_gitfile(workspace.joinpath(*PurePosixPath(state["source_repository"]).parts), worktree)
    baseline = state["baseline_head"]
    head = _git(worktree, "rev-parse", "HEAD")
    commit_oid = None
    if state["schema_version"] == 3:
        if _git(worktree, "rev-list", "--parents", "-n", "1", head).split() != [head, baseline]:
            raise PilotError(f"child-commit-parent-mismatch:{node_id}")
        if _git(worktree, "branch", "--show-current") or _git_operation(worktree):
            raise PilotError(f"child-commit-worktree-invalid:{node_id}")
        if _git_bytes(worktree, "diff", "--name-only", "-z", "HEAD", "--"):
            raise PilotError(f"child-commit-worktree-dirty:{node_id}")
        if _git(worktree, "show", "-s", "--format=%B", head).splitlines()[-1:] != [
            "Co-authored-by: Codex <codex@openai.com>"
        ]:
            raise PilotError(f"child-commit-attribution-invalid:{node_id}")
        commit_oid = head
    elif head != baseline:
        raise PilotError(f"child-commit-forbidden:{node_id}")
    if _git_bytes(worktree, "ls-files", "--others", "-z"):
        raise PilotError(f"child-untracked-path-forbidden:{node_id}")
    changed_paths = sorted(_nul_paths(_git(worktree, "diff", "--name-only", "--no-renames", "-z", baseline, "--")))
    if set(changed_paths) != set(node["owned_paths"]):
        raise PilotError(f"child-owned-path-mismatch:{node_id}")
    _raw_content_updates(worktree, node, base=baseline)
    patch = _git_bytes(worktree, "diff", "--binary", "--full-index", baseline, "--", *node["owned_paths"])
    if not patch:
        raise PilotError(f"child-patch-empty:{node_id}")
    return changed_paths, patch, commit_oid


def _create_code_remediate_child_handover(
    state: dict[str, Any], workspace: Path, state_path: Path, node_id: str, summary: str, verification_path: Path | None
) -> dict[str, object]:
    """Create a handover with the parent-observed patch and any draft commit."""
    _code_remediate_authority(state, workspace, state_path, clean=True)
    if state.get("status") != "prepared" or not isinstance(summary, str) or not summary.strip() or len(summary) > 2_000:
        raise PilotError(f"child-handover-summary-invalid:{node_id}")
    node = _node(state, node_id)
    paths, patch, commit_oid = _code_remediate_child_patch(state, workspace, node)
    evidence = {}
    if commit_oid is not None:
        if verification_path is None:
            raise PilotError(f"child-advisory-receipt-required:{node_id}")
        receipt = _code_remediate_cli_artifact_path(state_path, verification_path, "child-advisory-receipt")
        verification = {"path": receipt.relative_to(state_path.parent.resolve()).as_posix(), "sha256": _sha256(receipt)}
        _require_grouped_worker_verification(
            state_path.parent, verification, node_id, commit_oid, node["verification_commands"]
        )
        evidence = {"commit_oid": commit_oid, "verification": verification}
    elif verification_path is not None:
        raise PilotError("child-advisory-receipt-unexpected")
    return {
        "node_id": node_id,
        "status": "completed",
        "summary": summary.strip(),
        "changed_paths": paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        **evidence,
    }


def _join_code_remediate_child_handovers(
    state_path: Path, state: dict[str, Any], workspace: Path, handovers: list[dict[str, object]]
) -> dict[str, object]:
    """Join every child handover against parent-observed Git bytes."""
    source, nodes = _code_remediate_authority(state, workspace, state_path, clean=True)
    if state.get("status") != "prepared" or len(handovers) != len(nodes):
        raise PilotError("child-handovers-incomplete")
    reports = {report.get("node_id"): report for report in handovers if isinstance(report, dict)}
    if len(reports) != len(nodes) or set(reports) != {node["node_id"] for node in nodes}:
        raise PilotError("child-handovers-incomplete")
    handover_fields = {"node_id", "status", "summary", "changed_paths", "patch_sha256"}
    if state["schema_version"] == 3:
        handover_fields.update({"commit_oid", "verification"})
    for node in state["nodes"]:
        report = reports[node["node_id"]]
        if (
            set(report) != handover_fields
            or report.get("status") != "completed"
            or not isinstance(report.get("summary"), str)
            or not report["summary"].strip()
            or len(report["summary"]) > 2_000
        ):
            raise PilotError(f"child-handover-invalid:{node['node_id']}")
        paths, patch, commit_oid = _code_remediate_child_patch(state, workspace, node)
        if (
            report.get("changed_paths") != paths
            or report.get("patch_sha256") != hashlib.sha256(patch).hexdigest()
            or report.get("commit_oid") != commit_oid
        ):
            raise PilotError(f"child-handover-patch-mismatch:{node['node_id']}")
        if commit_oid is not None:
            _require_grouped_worker_verification(
                state_path.parent, report["verification"], node["node_id"], commit_oid, node["verification_commands"]
            )
        node["handover"] = {key: value for key, value in report.items() if key != "node_id"}
        node["terminal_status"] = "completed"
        node["joined"] = True
    _require_unchanged_code_remediate_source(
        source, [path for node in nodes for path in node["owned_paths"]], state["baseline_head"]
    )
    state["joined_nodes"] = [
        {
            "node_id": node["node_id"],
            "owned_paths": node["owned_paths"],
            "patch_sha256": node["handover"]["patch_sha256"],
            **({"commit_oid": node["handover"]["commit_oid"]} if state["schema_version"] == 3 else {}),
        }
        for node in state["nodes"]
    ]
    state["status"] = "joined"
    state["joined_at"] = datetime.now(timezone.utc).isoformat()
    _persist(state_path, state)
    return state


def _write_or_adopt_patch(path: Path, patch: bytes, node_id: str) -> None:
    """Write a new patch or adopt a crash-left regular file with the same bytes."""
    if not (path.exists() or path.is_symlink()):
        try:
            with path.open("xb") as stream:
                stream.write(patch)
            return
        except FileExistsError:
            pass
    try:
        if not path.is_symlink() and path.is_file() and path.read_bytes() == patch:
            return
    except OSError as error:
        raise PilotError(f"patch-output-exists:{node_id}") from error
    raise PilotError(f"patch-output-exists:{node_id}")


def _collect_code_remediate_patch(
    state_path: Path, state: dict[str, Any], workspace: Path, node_id: str
) -> dict[str, object]:
    """Persist one parent-derived child patch after the complete join."""
    _code_remediate_authority(state, workspace, state_path, clean=True)
    node = _node(state, node_id)
    if state.get("status") != "joined" or node.get("patch_status") != "not-collected":
        raise PilotError("pilot-children-not-joined")
    paths, patch, commit_oid = _code_remediate_child_patch(state, workspace, node)
    if (
        node.get("handover", {}).get("patch_sha256") != hashlib.sha256(patch).hexdigest()
        or node.get("handover", {}).get("commit_oid") != commit_oid
    ):
        raise PilotError(f"child-handover-drift:{node_id}")
    source = workspace.joinpath(*PurePosixPath(state["source_repository"]).parts)
    patch_relative, _ = _code_remediate_evidence_path(
        source, _relative_workspace_path(source, state_path.parent / node["output"], "patch-path"), "patch-path"
    )
    # The validators resolve links; adoption must see the recorded name so a symlinked output is refused.
    _write_or_adopt_patch(state_path.parent / node["output"], patch, node_id)
    node.update(
        {
            "changed_paths": paths,
            "patch_path": patch_relative,
            "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "patch_status": "collected",
            **({"commit_oid": commit_oid} if commit_oid is not None else {}),
        }
    )
    _persist(state_path, state)
    return node


def _require_fresh_integration_worktree(source: Path, integration: Path, baseline: str) -> None:
    """Accept a crash-left integration checkout only when Git shows exactly a fresh detached baseline checkout.

    The checkout must be registered with the source repository, carry a reciprocal gitfile, sit detached at the
    baseline with no pending operation, and hold no tracked, untracked, or ignored change.

    Raises:
        PilotError: ``integration-worktree-exists`` when any of those facts cannot be shown.
    """
    try:
        _verify_worktree_gitfile(source, integration)
        fresh = (
            integration in _registered_worktree_paths(source)
            and not _git(integration, "branch", "--show-current")
            and _git_operation(integration) is None
            and _git(integration, "rev-parse", "HEAD") == baseline
            and not _git_bytes(integration, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
        )
    except PilotError as error:
        raise PilotError("integration-worktree-exists") from error
    if not fresh:
        raise PilotError("integration-worktree-exists")


def _integration_edits_within_owned_paths(integration: Path, owned: set[str]) -> bool:
    """Check index and worktree changes separately without refreshing the retained index."""
    options = ("--name-only", "--no-renames", "--ignore-submodules=none", "-z")
    changes = (
        _git_bytes(integration, "diff-index", "--cached", *options, "HEAD", "--"),
        _git_bytes(integration, "diff-files", *options, "--"),
        _git_bytes(integration, "ls-files", "--others", "-z"),
    )
    if any(os.fsdecode(path) not in owned for records in changes for path in records.split(b"\0") if path):
        return False
    # Git's ordinary diffs omit worktree changes hidden by assume-unchanged or skip-worktree.
    index = _git_bytes(integration, "ls-files", "-v", "-z")
    return not any(
        os.fsdecode(entry[2:]) not in owned
        for entry in index.split(b"\0")
        if entry.startswith(b"S ") or entry[:1].islower()
    )


def _same_authored_commit(integration: Path, commit: str, worker_commit: str) -> bool:
    """Tell whether ``commit`` is this run's cherry-pick of the recorded worker commit (author, date and message)."""
    fields = "--format=%an%x00%ae%x00%aI%x00%B"
    try:
        return _git_bytes(integration, "show", "-s", fields, commit) == _git_bytes(
            integration, "show", "-s", fields, worker_commit
        )
    except PilotError:
        return False


def _matches_grouped_integration_prefix(
    source: Path, integration: Path, baseline: str, ordered: list[dict[str, Any]]
) -> bool:
    """Verify that the detached history contains only the ordered prefix of recorded patches."""
    history = _git(
        integration, "rev-list", "--reverse", f"--max-count={len(ordered) + 1}", f"{baseline}..HEAD"
    ).splitlines()
    if len(history) > len(ordered):
        return False
    parent = baseline
    for commit, node in zip(history, ordered):
        if _git(integration, "rev-list", "--parents", "-n", "1", commit).split() != [commit, parent]:
            return False
        _, patch_path = _code_remediate_evidence_path(source, node.get("patch_path"), "patch-path")
        patch = patch_path.read_bytes()
        changed = _git_bytes(integration, "diff", "--name-only", "-z", parent, commit, "--")
        if (
            hashlib.sha256(patch).hexdigest() != node.get("patch_sha256")
            or not _same_authored_commit(integration, commit, str(node["handover"].get("commit_oid")))
            or {os.fsdecode(path) for path in changed.split(b"\0") if path} != set(node.get("changed_paths", ()))
            or _git_bytes(integration, "diff", "--binary", "--full-index", parent, commit, "--") != patch
        ):
            return False
        parent = commit
    return _git(integration, "rev-parse", "HEAD") == parent


def _require_restartable_integration_worktree(
    source: Path, integration: Path, state: dict[str, Any], ordered: list[dict[str, Any]]
) -> None:
    """Prove the interrupted checkout can be discarded without losing unowned work."""
    try:
        _verify_worktree_gitfile(source, integration)
        owned = {path for node in ordered for path in node["owned_paths"]}
        baseline = state["baseline_head"]
        restartable = (
            integration in _registered_worktree_paths(source)
            and not _git(integration, "branch", "--show-current")
            and _git_operation(integration, allow_patch_operations=True) is None
            and _integration_edits_within_owned_paths(integration, owned)
            and (
                _matches_grouped_integration_prefix(source, integration, baseline, ordered)
                if state["schema_version"] == 3
                else _git(integration, "rev-parse", "HEAD") == baseline
            )
        )
    except (OSError, PilotError) as error:
        raise PilotError("integration-worktree-exists") from error
    if not restartable:
        raise PilotError("integration-worktree-exists")


def _open_integration_worktree(
    state_path: Path, state: dict[str, Any], source: Path, workspace: Path, ordered: list[dict[str, Any]]
) -> Path:
    """Open a fresh checkout or restart a recorded, provably disposable integration."""
    integration_relative = f"{state['worktree_root']}/integration"
    status = state.get("integration_status")
    try:
        _, integration = _code_remediate_worktree_path(
            workspace, state["source_repository"], integration_relative, "integration-worktree"
        )
    except PilotError as error:
        if status == "applying":
            raise PilotError("integration-worktree-exists") from error
        raise
    exists = integration.exists() or integration.is_symlink()
    if exists and state.get("integration_worktree") != integration_relative:
        raise PilotError("integration-worktree-exists")
    if exists and status == "applying":
        _require_restartable_integration_worktree(source, integration, state, ordered)
        try:
            # Git itself refuses a worktree with any unrecorded change, so a clean checkout leaves no window at all.
            _git(source, "worktree", "remove", str(integration))
        except PilotError:
            # The checkout holds only this run's own edits: prove it again right before the forced removal.
            _require_restartable_integration_worktree(source, integration, state, ordered)
            _git(source, "worktree", "remove", "--force", str(integration))
        LOGGER.info("Restarting integration after an interrupted application")
        exists = False
    if exists and status == "creating":
        _require_fresh_integration_worktree(source, integration, state["baseline_head"])
    elif exists:
        raise PilotError("integration-worktree-exists")
    else:
        state.update({"integration_worktree": integration_relative, "integration_status": "creating"})
        _persist(state_path, state)
        _git(source, "worktree", "add", "--detach", str(integration), state["baseline_head"])
    state["integration_status"] = "applying"
    _persist(state_path, state)
    return integration


def _integrate_code_remediate_pilot(state_path: Path, state: dict[str, Any], workspace: Path) -> dict[str, object]:
    """Integrate the joined drafts, retaining failures for the parent."""
    source, _ = _code_remediate_authority(
        state, workspace, state_path, clean=True, verify_integration=state.get("integration_status") != "applying"
    )
    nodes = state.get("nodes")
    if (
        state.get("status") != "joined"
        or not isinstance(nodes, list)
        or any(node.get("patch_status") != "collected" for node in nodes)
    ):
        raise PilotError("pilot-children-not-joined")
    ordered = sorted(nodes, key=lambda node: node["node_id"])
    integration = _open_integration_worktree(state_path, state, source, workspace, ordered)
    applied_paths: set[str] = set()
    grouped = state["schema_version"] == 3
    baseline = state["baseline_head"] if grouped else "HEAD"
    integrated_commits: list[str] = []
    try:
        for node in ordered:
            _, patch_path = _code_remediate_evidence_path(source, node.get("patch_path"), "patch-path")
            if _sha256(patch_path) != node.get("patch_sha256"):
                raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
            if grouped:
                paths, patch, commit_oid = _code_remediate_child_patch(state, workspace, node)
                if patch != patch_path.read_bytes() or commit_oid != node["handover"].get("commit_oid"):
                    raise PilotError(f"child-handover-drift:{node['node_id']}")
                _require_grouped_worker_verification(
                    state_path.parent,
                    node["handover"].get("verification"),
                    node["node_id"],
                    commit_oid,
                    node["verification_commands"],
                )
                parent = integrated_commits[-1] if integrated_commits else baseline
                _git(integration, "cherry-pick", str(commit_oid))
                head = _git(integration, "rev-parse", "HEAD")
                if (
                    _git(integration, "rev-list", "--parents", "-n", "1", head).split() != [head, parent]
                    or sorted(
                        _nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", parent, head, "--"))
                    )
                    != paths
                    or _git_bytes(integration, "diff", "--binary", "--full-index", parent, head, "--", *paths) != patch
                ):
                    raise PilotError(f"integration-commit-drift:{node['node_id']}")
                integrated_commits.append(head)
                applied_paths.update(paths)
                continue
            try:
                _git(integration, "apply", "--check", str(patch_path))
                _git(integration, "apply", str(patch_path))
            except PilotError as error:
                conflict_paths = sorted(applied_paths.intersection(node["owned_paths"]))
                if not conflict_paths:
                    raise
                state.update(
                    {
                        "status": "integration-needs-reconciliation",
                        "integration_status": "needs-reconciliation",
                        "integration_failed_node": node["node_id"],
                        "integration_conflict_paths": conflict_paths,
                        "integration_order": [item["node_id"] for item in ordered],
                    }
                )
                _persist(state_path, state)
                raise PilotError(f"patch-integration-needs-reconciliation:{node['node_id']}") from error
            applied_paths.update(node["owned_paths"])
        if _git_bytes(integration, "ls-files", "--others", "-z"):
            raise PilotError("integration-untracked-path-forbidden")
        expected = sorted({path for node in ordered for path in node["owned_paths"]})
        changed = sorted(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", baseline, "--")))
        if changed != expected:
            raise PilotError("integration-changed-path-mismatch")
        if grouped:
            _require_grouped_integration_tip(integration, baseline, integrated_commits)
            objects = [
                base64.b64encode(_git_bytes(integration, "cat-file", "commit", commit)).decode("ascii")
                for commit in integrated_commits
            ]
            _require_grouped_commit_chain(baseline, integrated_commits, objects)
            state.update(
                {
                    "integration_head": integrated_commits[-1],
                    "integrated_commits": integrated_commits,
                    "integrated_commit_objects": objects,
                }
            )
        final = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in expected}
        final_filtered = {path: _git_filtered_oid(integration, path) for path in expected}
        state.update(
            {
                "status": "integrated",
                "integration_status": "structurally-verified",
                "integration_order": [node["node_id"] for node in ordered],
                "integration_final_sha256": final,
                "integration_final_filtered_oid": final_filtered,
                "integration": {
                    "status": "structurally-verified",
                    "order": [node["node_id"] for node in ordered],
                    "paths": expected,
                },
            }
        )
        _persist(state_path, state)
    except PilotError:
        if state["integration_status"] != "needs-reconciliation":
            state["integration_status"] = "failed"
            _persist(state_path, state)
        raise
    return state


def _require_grouped_integration_tip(worktree: Path, baseline: str, commits: object) -> None:
    """Verify the disposable integration checkout still contains exactly its recorded committed wave."""
    if (
        not isinstance(commits, list)
        or not commits
        or _git(worktree, "branch", "--show-current")
        or _git_operation(worktree)
        or _git_bytes(worktree, "diff", "--name-only", "-z", "HEAD", "--")
        or _git(worktree, "rev-list", "--reverse", f"{baseline}..HEAD").splitlines() != commits
    ):
        raise PilotError("integration-worktree-drift")


def _require_grouped_commit_chain(baseline: str, commits: list[str], objects: object) -> None:
    """Authenticate each ordered parent link offline using the tested tip's Git object identity."""
    if not isinstance(baseline, str) or len(baseline) not in (40, 64) or not isinstance(objects, list):
        raise PilotError("grouped-commit-chain-invalid")
    if len(commits) != len(objects):
        raise PilotError("grouped-commit-chain-invalid")
    parent = baseline
    algorithm = "sha1" if len(baseline) == 40 else "sha256"
    for oid, encoded in zip(commits, objects, strict=True):
        try:
            if not isinstance(encoded, str):
                raise TypeError("commit proof must be base64 text")
            content = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as error:
            raise PilotError("grouped-commit-chain-invalid") from error
        git_object = f"commit {len(content)}\0".encode("ascii") + content
        header, separator, _ = content.partition(b"\n\n")
        parents = [line.removeprefix(b"parent ") for line in header.splitlines() if line.startswith(b"parent ")]
        if (
            not separator
            or parents != [parent.encode("ascii")]
            or hashlib.new(algorithm, git_object, usedforsecurity=False).hexdigest() != oid
        ):
            raise PilotError("grouped-commit-chain-invalid")
        parent = oid


def reconcile_code_remediate_integration(*, state_path: Path, record_path: Path) -> dict[str, object]:
    """Verify a parent-edited integration worktree against an exact run-local resolution record."""
    state, workspace = _read_state(state_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    if (
        state.get("status") != "integration-needs-reconciliation"
        or state.get("integration_status") != "needs-reconciliation"
    ):
        raise PilotError("integration-reconciliation-not-required")
    _, integration = _code_remediate_worktree_path(
        workspace, state["source_repository"], state.get("integration_worktree"), "integration-worktree"
    )
    record_file = _code_remediate_cli_artifact_path(state_path, record_path, "reconciliation-record")
    record = _load_json(record_file, "reconciliation-record")
    ordered = sorted(state["nodes"], key=lambda node: node["node_id"])
    paths = sorted({path for node in ordered for path in node["owned_paths"]})
    patch_hashes = {node["node_id"]: node["patch_sha256"] for node in ordered}
    required = {
        "baseline_head",
        "failed_node",
        "integration_order",
        "paths",
        "patch_sha256",
        "postimage_sha256",
        "summary",
    }
    if (
        set(record) != required
        or record["baseline_head"] != state["baseline_head"]
        or record["failed_node"] != state["integration_failed_node"]
        or record["integration_order"] != [node["node_id"] for node in ordered]
        or record["paths"] != paths
        or record["patch_sha256"] != patch_hashes
        or not isinstance(record["summary"], str)
        or not record["summary"].strip()
        or len(record["summary"]) > 2_000
    ):
        raise PilotError("integration-reconciliation-record-invalid")
    for node in ordered:
        _, patch_path = _code_remediate_evidence_path(source, node["patch_path"], "patch-path")
        if _sha256(patch_path) != patch_hashes[node["node_id"]]:
            raise PilotError(f"patch-digest-mismatch:{node['node_id']}")
    if _git(integration, "rev-parse", "HEAD") != state["baseline_head"]:
        raise PilotError("integration-reconciliation-worktree-drift")
    if _git_bytes(integration, "ls-files", "--others", "-z"):
        raise PilotError("integration-reconciliation-untracked-path")
    changed = sorted(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
    if changed != paths:
        raise PilotError("integration-reconciliation-path-mismatch")
    for node in ordered:
        _raw_content_updates(integration, node)
    postimages = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in paths}
    if record["postimage_sha256"] != postimages:
        raise PilotError("integration-reconciliation-postimage-mismatch")
    filtered = {path: _git_filtered_oid(integration, path) for path in paths}
    record_relative = _relative_workspace_path(state_path.parent, record_file, "reconciliation-record")
    state.update(
        {
            "status": "integrated",
            "integration_status": "structurally-verified",
            "integration_final_sha256": postimages,
            "integration_final_filtered_oid": filtered,
            "integration": {
                "status": "structurally-verified",
                "order": [node["node_id"] for node in ordered],
                "paths": paths,
            },
            "reconciliation": {
                "status": "verified",
                "record_path": record_relative,
                "record_sha256": _sha256(record_file),
                "changed_paths": changed,
                "failed_node": state["integration_failed_node"],
                "postimage_sha256": postimages,
            },
        }
    )
    _persist(state_path, state)
    return state


def _require_grouped_gates(gates: dict[str, Any], worktree: PurePath, head: str, *, tests_command: object) -> None:
    """Require ordinary-validated gate evidence for the frozen tests and exact clean integration source."""
    if not isinstance(tests_command, str) or not tests_command.strip():
        raise PilotError("grouped-plan-tests-command-invalid")
    tests_digest = hashlib.sha256(f"{tests_command}\n".encode()).hexdigest()
    source = gates.get("source")
    checks = gates.get("checks")
    required = {"lint", "format", "types", "tests", "review"}
    if (
        gates.get("status") != "pass"
        or gates.get("checks_failed") != []
        or gates.get("failed_count") != 0
        or not isinstance(source, dict)
        or source.get("worktree") != worktree.as_posix()
        or source.get("expected_head") != head
        or not isinstance(checks, list)
        or len(checks) != len(required)
    ):
        raise PilotError("grouped-gate-source-mismatch")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or check["id"] not in required:
            raise PilotError("grouped-gate-source-mismatch")
        required.remove(check["id"])
        if check.get("status") == "not-applicable" and check["id"] != "tests":
            continue
        binding = check.get("source")
        if (
            check.get("status") != "pass"
            or check.get("exit_code") != 0
            or not isinstance(binding, dict)
            or binding.get("expected_head") != head
            or binding.get("worktree") != worktree.as_posix()
            or binding.get("guard_error")
        ):
            raise PilotError("grouped-gate-source-mismatch")
        artifacts = binding.get("artifacts")
        required_artifacts = {check[key] for key in ("command_path", "stdout", "stderr")}
        if "python_imports" in check:
            required_artifacts.add("checks/tests.python-imports.json")
        if not isinstance(artifacts, dict) or not required_artifacts.issubset(artifacts):
            raise PilotError("grouped-gate-artifact-hashes-missing")
        if check["id"] == "tests" and artifacts[check["command_path"]] != tests_digest:
            raise PilotError("grouped-gate-tests-command-mismatch")
        for observation in (binding.get("before"), binding.get("after")):
            if (
                not isinstance(observation, dict)
                or observation.get("head") != head
                or observation.get("status") != ""
                or "error" in observation
            ):
                raise PilotError("grouped-gate-source-mismatch")
            try:
                ignored = json.loads(observation.get("ignored"))
            except (TypeError, ValueError) as error:
                raise PilotError("grouped-gate-source-mismatch") from error
            if not isinstance(ignored, list) or any(
                not isinstance(path, str)
                or PurePosixPath(path).suffix in {".py", ".pth", ".ini"}
                or PurePosixPath(path).name in {"pyproject.toml", "setup.cfg"}
                for path in ignored
            ):
                raise PilotError("grouped-gate-source-mismatch")


def _validate_grouped_gate_artifacts(directory: Path, integration: Path, head: str, tests_command: object) -> None:
    """Apply the existing artifact validator before moving the authoritative branch."""
    # The validator imports our source-binding check, so resolve it only at the transition that uses it.
    spec = importlib.util.spec_from_file_location(
        "codex_rig_validate_artifacts", Path(__file__).with_name("validate-artifacts.py")
    )
    if spec is None or spec.loader is None:
        raise PilotError("grouped-gate-validator-unavailable")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    try:
        gates = validator._validate_gates(directory)
        _require_grouped_gates(gates, integration, head, tests_command=tests_command)
    except (OSError, ValueError, SystemExit) as error:
        raise PilotError(f"grouped-gate-artifacts-invalid:{error}") from error


def _require_grouped_worker_receipts(state_path: Path, state: dict[str, Any]) -> None:
    """Recheck every joined worker's advisory receipt against the bytes recorded at handover."""
    for node in state["nodes"]:
        _require_grouped_worker_verification(
            state_path.parent,
            node["handover"].get("verification"),
            node["node_id"],
            node["commit_oid"],
            node["verification_commands"],
        )


def _rebind_grouped_delivery_evidence(state_path: Path, state: dict[str, Any], integration: Path) -> None:
    """Re-bind every artifact the delivery decision rests on immediately before the irreversible branch move.

    The first validation pass runs before patches are written, so a log swapped afterwards would otherwise reach the
    fast-forward unseen and fail final validation only after the branch had moved.
    """
    application = state["source_application"]
    _require_grouped_worker_receipts(state_path, state)
    _validate_grouped_gate_artifacts(
        state_path.parent, integration, state["integration_head"], state.get("tests_command")
    )
    if _sha256(state_path.parent / str(application["gates_path"])) != application["gates_sha256"]:
        raise PilotError("grouped-gate-evidence-drift")


def _record_source_applied(state_file: _GuardedStateFile, state: dict[str, Any], source: Path) -> dict[str, object]:
    """Persist a delivery Git has already completed, with the source postimages observed now."""
    application = state["source_application"]
    paths = application["applied_paths"]
    application["status"] = "applied"
    state["source"] = {
        "baseline_head": state["baseline_head"],
        "baseline_tree": state["baseline_tree"],
        "applied_head": _git(source, "rev-parse", "HEAD"),
        "preimage_sha256": application["preimage_sha256"],
        "postimage_sha256": {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths},
    }
    state["status"] = "source-applied"
    state_file.save(state)
    return application


class _IndexEntry(NamedTuple):
    """One stage-0 index entry: its Git mode (``100644``, ``100755`` or ``120000``) and blob ID."""

    mode: str
    blob: str


def _stage_zero_entries(source: Path, paths: list[str]) -> dict[str, _IndexEntry] | None:
    """Return each path's index mode and blob, or ``None`` when any path is missing from the index or unmerged."""
    entries: dict[str, _IndexEntry] = {}
    for record in _git_bytes(source, "ls-files", "--stage", "-z", "--", *paths).split(b"\0"):
        if not record:
            continue
        metadata, _, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if len(fields) != 3 or fields[2] != b"0":
            return None
        entries[os.fsdecode(raw_path)] = _IndexEntry(fields[0].decode("ascii"), fields[1].decode("ascii"))
    return entries if sorted(entries) == sorted(paths) else None


def _working_git_mode(st_mode: int) -> str:
    """Return the Git mode a working file with POSIX ``st_mode`` would be staged with.

    Git stages a symbolic link as ``120000`` and a regular file as ``100755`` when its owner may execute it, else
    ``100644``. Any other file type gets its raw octal type, which matches no tracked mode.
    """
    if stat.S_ISLNK(st_mode):
        return "120000"
    if stat.S_ISREG(st_mode):
        return "100755" if st_mode & stat.S_IXUSR else "100644"
    return f"{stat.S_IFMT(st_mode):06o}"


def _approved_modes_unchanged(source: Path, baseline_head: str, index_modes: dict[str, str]) -> bool:
    """Return whether every approved path keeps its baseline mode in the index and in the working tree.

    No delivery changes a mode (the child patch shape check rejects mode changes), so any difference is the user's work,
    and a restore to the baseline would discard it. A missing working file is left to the content check. Hosts without
    POSIX permission bits, such as native Windows, compare only the index mode, the one Git tracks there.
    """
    baseline: dict[str, str] = {}
    for record in _git_bytes(source, "ls-tree", "-z", baseline_head, "--", *index_modes).split(b"\0"):
        if record:
            metadata, _, raw_path = record.partition(b"\t")
            baseline[os.fsdecode(raw_path)] = metadata.split()[0].decode("ascii")
    if baseline != index_modes:
        return False
    if os.name != "posix":
        return True
    for path, mode in baseline.items():
        try:
            metadata = source.joinpath(*PurePosixPath(path).parts).lstat()
        except FileNotFoundError:
            continue
        if _working_git_mode(metadata.st_mode) != mode:
            return False
    return True


_ABSENT = "absent"


def _observed_working_image(source: Path, relative_path: str) -> tuple[str, str | None]:
    """Return a working-tree path's SHA-256 and Git filtered object ID, or ``_ABSENT`` and ``None`` when it is missing.

    ``git apply`` deletes a file before it writes the new one, so a failed or interrupted apply can leave an approved
    file missing; that state is observable and restorable, not unreadable.
    """
    path = source.joinpath(*PurePosixPath(relative_path).parts)
    if not path.is_file() and not path.is_symlink():
        return _ABSENT, None
    return _sha256(path), _git_filtered_oid(source, relative_path)


def _backup_unrestored_bytes(source: Path, paths: list[str], backup_dir: Path, observed: dict[str, str]) -> bool:
    """Copy each existing approved file aside and return whether every copy equals the bytes the restore verified.

    The check and ``git restore`` are separate steps, so an edit landing between them would be overwritten unseen. The
    backup is taken last, so any difference from the verified bytes is caught and reported instead of discarded.
    """
    for path in paths:
        if observed[path] == _ABSENT:
            continue
        target = backup_dir.joinpath(*PurePosixPath(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.joinpath(*PurePosixPath(path).parts), target)
        if _sha256(target) != observed[path]:
            return False
    return True


def _restore_known_approved_paths(
    state_file: _GuardedStateFile, state: dict[str, Any], source: Path, *, refuse_landed: bool
) -> tuple[bool, dict[str, str]]:
    """Restore approved paths to the baseline only when Git shows each one holding a recorded image.

    Every approved path's working-tree content and stage-0 index entry must equal its recorded preimage or postimage,
    and its index mode and working file mode must equal the baseline, so the restore can discard nothing that the
    delivery did not write, including a user's staged edit or executable bit. A missing working file counts as a
    recorded image only while its index entry still holds the preimage, which is the state a failed or interrupted
    ``git apply`` leaves. Existing files are copied to ``rollback-backup`` beside the state right before the restore.

    Immediately before that first write, the state file is reread: a record another process saved since this apply
    read it may describe these very paths as its delivery, so the restore stops instead of undoing it. With
    ``refuse_landed`` it also restores nothing while Git shows the recorded delivery landed exactly.

    Returns:
        Whether every approved path matches its recorded preimage afterwards, and the SHA-256 of each path as last
        observed (empty when the paths could not be read).

    Raises:
        PilotError: ``lifecycle-state-concurrent-update`` when the state file changed; nothing was restored.
    """
    backup_dir = state_file.path.parent / "rollback-backup"
    application = state["source_application"]
    paths = application["applied_paths"]
    preimage = application["preimage_filtered_oid"]
    known = {path: {preimage[path], state["integration_final_filtered_oid"][path]} for path in paths}
    try:
        images = {path: _observed_working_image(source, path) for path in paths}
        observed = {path: images[path][0] for path in paths}
        index = _stage_zero_entries(source, paths)
        modes_kept = index is not None and _approved_modes_unchanged(
            source, state["baseline_head"], {path: index[path].mode for path in paths}
        )
    except (OSError, PilotError):
        return False, {}
    if (
        index is None
        or not modes_kept
        or any(
            index[path].blob not in known[path]
            or (
                images[path][1] not in known[path]
                if images[path][1] is not None
                else index[path].blob != preimage[path]
            )
            for path in paths
        )
    ):
        return False, observed
    state_file.require_current()
    if refuse_landed and _delivery_landed(source, state):
        return False, observed
    restored_hashes: dict[str, str] = {}
    try:
        if not _backup_unrestored_bytes(source, paths, backup_dir, observed):
            return False, observed
        _git(source, "restore", "--source", state["baseline_head"], "--staged", "--worktree", "--", *paths)
        restored_hashes = {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths}
        restored = {path: _git_filtered_oid(source, path) for path in paths} == application["preimage_filtered_oid"]
    except (OSError, PilotError):
        return False, restored_hashes or observed
    if restored:
        # Every copied byte equalled what was verified, so nothing unrecorded was discarded; keep no stray files.
        shutil.rmtree(backup_dir, ignore_errors=True)
    return restored, restored_hashes


def _roll_back_patch_delivery(state_file: _GuardedStateFile, state: dict[str, Any], source: Path) -> bool:
    """Restore approved paths after a failed patch delivery only from a known pre- or postimage, and record it.

    The rollback runs inside the run's apply lock, so no other apply has written the source since this one saved
    ``source-applying``: approved paths at the recorded postimage are this apply's own write and are restored even
    though Git alone cannot tell them from a landed delivery. A writer that bypassed the lock and saved its record is
    caught by the state reread right before the restore.

    Returns:
        Whether every approved path now matches its recorded preimage again.

    Raises:
        PilotError: ``lifecycle-state-concurrent-update`` when the state file changed; nothing was restored or saved.
    """
    application = state["source_application"]
    restored, observed = _restore_known_approved_paths(state_file, state, source, refuse_landed=False)
    application["status"] = "failed-rolled-back" if restored else "rollback-ambiguous"
    application["rollback_observed_sha256"] = observed
    application["rollback_preimages_verified"] = restored
    state["status"] = application["status"]
    state_file.save(state)
    return restored


def _preflight_source_delivery(
    state_file: _GuardedStateFile, state: dict[str, Any], source: Path, integration: Path, patch_path: Path
) -> None:
    """Prove the source still accepts the recorded delivery and re-bind its evidence, writing nothing to the source."""
    _require_unchanged_code_remediate_source(
        source, state["source_application"]["applied_paths"], state["baseline_head"]
    )
    _git(source, "apply", "--check", str(patch_path))
    if state["schema_version"] == 3:
        _require_grouped_source_branch(source, state["source_branch"])
        if _git_bytes(source, "diff", "--cached", "--name-only", "-z"):
            raise PilotError("source-repository-index-dirty")
        _rebind_grouped_delivery_evidence(state_file.path, state, integration)


def _write_source_delivery(state: dict[str, Any], source: Path, patch_path: Path) -> None:
    """Write the recorded delivery into the source and require every approved path to hold its recorded postimage."""
    paths = state["source_application"]["applied_paths"]
    if state["schema_version"] == 3:
        _git(source, "merge", "--ff-only", "--no-overwrite-ignore", "--no-autostash", state["integration_head"])
        _require_grouped_source_branch(source, state["source_branch"])
        _require_unchanged_code_remediate_source(source, paths, state["integration_head"])
    else:
        _git(source, "apply", str(patch_path))
    observed_filtered_oids = {path: _git_filtered_oid(source, path) for path in paths}
    if observed_filtered_oids != state["integration_final_filtered_oid"]:
        raise PilotError("source-application-postimage-mismatch")


def _reopen_integrated_run(state_file: _GuardedStateFile, state: dict[str, Any]) -> None:
    """Record the attempt as ``preflight-failed`` and reopen the run as ``integrated``, so a later apply can retry."""
    state["source_application"]["status"] = "preflight-failed"
    state["status"] = "integrated"
    state_file.save(state)


def _record_failed_source_write(
    state_file: _GuardedStateFile, state: dict[str, Any], source: Path, error: PilotError
) -> None:
    """Persist what a failed source write left behind; the caller re-raises the write's error.

    Raises:
        PilotError: ``rollback-ambiguous`` when a patch rollback could not prove every approved path restored, and
            ``lifecycle-state-concurrent-update`` when another process changed the state file.
    """
    application = state["source_application"]
    if state["schema_version"] != 3:
        if not _roll_back_patch_delivery(state_file, state, source):
            raise PilotError("rollback-ambiguous") from error
        return
    # Both checks require the branch still at the baseline, so a landed delivery can never be reopened here.
    if _delivery_untouched(source, state) or _restore_interrupted_delivery(state_file, state, source):
        # Git refused the fast-forward before it moved the branch (index lock, signature policy, a read-only directory
        # part-way through the checkout), and every approved path is at the baseline or was restored from a recorded
        # image, so the run reopens exactly as a preflight failure does.
        _reopen_integrated_run(state_file, state)
        return
    # A fast-forward can have moved the branch before a postcondition fails. Preserve its observed state.
    state["status"] = application["status"] = "failed"
    application["observed_head"] = _git(source, "rev-parse", "HEAD")
    state_file.save(state)


def _deliver_source_application(
    state_file: _GuardedStateFile, state: dict[str, Any], source: Path, integration: Path, patch_path: Path
) -> dict[str, object]:
    """Run the guarded source write recorded as ``source-applying`` and persist its outcome.

    A preflight failure writes nothing to the source, so it reopens the run as ``integrated`` with the attempt recorded
    as ``preflight-failed``: the caller repairs the drift and applies again. When the preflight failed only because Git
    already shows this exact delivery landed, the delivery is recorded as applied and returned instead, because a
    reopened run whose source no longer matches the baseline could never be applied, resumed or cleaned up. A grouped
    fast-forward Git refuses without moving the branch is reopened the same way, after any approved path it had already
    written is restored from a recorded image. A grouped fast-forward that moved the branch is never moved back: when
    Git shows the exact delivery landed, only a later step failed and the delivery is recorded as applied for the same
    reason; any other moved-branch state stays ``failed``. A failed patch write is rolled back from recorded images.

    Raises:
        PilotError: If preflight, the write, or its postcondition fails and Git does not show the delivery landed;
            failure evidence is persisted first.
    """
    # Preflight failures have not changed approved paths, so they must never invoke rollback restoration.
    try:
        _preflight_source_delivery(state_file, state, source, integration, patch_path)
    except PilotError:
        if _delivery_landed(source, state):
            return _record_source_applied(state_file, state, source)
        _reopen_integrated_run(state_file, state)
        raise
    try:
        _write_source_delivery(state, source, patch_path)
    except PilotError as error:
        if state["schema_version"] == 3 and _delivery_landed(source, state):
            return _record_source_applied(state_file, state, source)
        _record_failed_source_write(state_file, state, source, error)
        raise
    return _record_source_applied(state_file, state, source)


def _recorded_delivery_patch(state: dict[str, Any], source: Path) -> Path:
    """Return the recorded forward patch after proving the delivery intent and both retained patches are intact.

    Raises:
        PilotError: ``source-application-resume-invalid`` when the intent record is incomplete or a patch changed.
    """
    application = state.get("source_application")
    paths = sorted({path for node in state["nodes"] for path in node["owned_paths"]})
    required = {
        "status",
        "patch_path",
        "patch_sha256",
        "rollback_patch_path",
        "rollback_patch_sha256",
        "applied_paths",
        "preimage_sha256",
        "preimage_filtered_oid",
    }
    if (
        not isinstance(application, dict)
        or not required.issubset(application)
        or application["status"] != "applying"
        or application["applied_paths"] != paths
        or not isinstance(application["preimage_sha256"], dict)
        or not isinstance(application["preimage_filtered_oid"], dict)
        or sorted(application["preimage_sha256"]) != paths
        or sorted(application["preimage_filtered_oid"]) != paths
    ):
        raise PilotError("source-application-resume-invalid")
    patches = []
    for path_key, digest_key in (("patch_path", "patch_sha256"), ("rollback_patch_path", "rollback_patch_sha256")):
        relative = _portable_relative(application[path_key], "source-application-patch")
        _, patch = _code_remediate_evidence_path(
            source, f"{state['evidence_root']}/{relative}", "source-application-patch"
        )
        if _sha256(patch) != application[digest_key]:
            raise PilotError("source-application-resume-invalid")
        patches.append(patch)
    return patches[0]


def _delivery_landed(source: Path, state: dict[str, Any]) -> bool:
    """Return whether Git shows the recorded delivery completed exactly.

    Grouped delivery has landed when the frozen source branch is attached, its tip is exactly the recorded integration
    tip, the commits between the baseline and that tip are exactly the recorded integrated commits (so the tip sits on
    the baseline), and the approved paths are clean against it. A branch moved to any other commit has not landed. Patch
    delivery has landed when no Git operation is in progress, HEAD is still the baseline, nothing is staged on the
    approved paths, and every approved path holds its recorded postimage. The branch and operation checks live here, not
    only in the caller's authority pass, because a failed preflight asks this question without re-deriving authority
    first. A Git or file read that fails proves nothing landed, so it answers ``False`` and lets the caller keep its own
    failure handling.
    """
    paths = state["source_application"]["applied_paths"]
    grouped = state["schema_version"] == 3
    expected_head = state["integration_head"] if grouped else state["baseline_head"]
    try:
        if _git(source, "rev-parse", "HEAD") != expected_head:
            return False
        if grouped:
            _require_grouped_source_branch(source, state["source_branch"])
            if _git(source, "rev-list", "--reverse", f"{state['baseline_head']}..HEAD").splitlines() != state.get(
                "integrated_commits"
            ):
                return False
            _require_unchanged_code_remediate_source(source, paths, expected_head)
        elif _git_operation(source) is not None or _git_bytes(
            source, "diff", "--cached", "--name-only", "-z", "--", *paths
        ):
            return False
        observed = {path: _observed_working_image(source, path)[1] for path in paths}
    except (OSError, PilotError):
        return False
    return observed == state["integration_final_filtered_oid"]


def _delivery_untouched(source: Path, state: dict[str, Any]) -> bool:
    """Return whether Git shows the source still exactly at the recorded pre-delivery baseline for approved paths."""
    application = state["source_application"]
    if _git(source, "rev-parse", "HEAD") != state["baseline_head"]:
        return False
    try:
        observed = {path: _observed_working_image(source, path)[1] for path in application["applied_paths"]}
    except (OSError, PilotError):
        return False
    return observed == application["preimage_filtered_oid"]


def _restore_interrupted_delivery(state_file: _GuardedStateFile, state: dict[str, Any], source: Path) -> bool:
    """Undo a delivery a crash cut short part-way, returning whether the approved paths are back at the baseline.

    ``git apply`` and a fast-forward checkout write paths one by one and move the branch last, so a crash can leave the
    branch at the baseline with some approved paths at their recorded postimage, or missing because ``git apply``
    deletes a file before it writes it. Only that state is restored, and only while every approved path keeps its
    baseline mode, because no delivery changes a mode and a mode changed after the crash is the user's work. Nothing is
    restored while Git shows the recorded delivery landed exactly, or once another process changed the state file.

    Raises:
        PilotError: ``lifecycle-state-concurrent-update`` when the state file changed; nothing was restored.
    """
    if _git(source, "rev-parse", "HEAD") != state["baseline_head"]:
        return False
    restored, _ = _restore_known_approved_paths(state_file, state, source, refuse_landed=True)
    return restored


def _resume_source_delivery(
    state_file: _GuardedStateFile, state: dict[str, Any], workspace: Path, gates_path: Path | None
) -> dict[str, object]:
    """Finish or continue a delivery a crash interrupted after ``source-applying`` was saved, deciding from Git.

    A delivery Git shows as landed is recorded as applied. A source Git shows untouched, or cut short with every
    approved path at a recorded image (restored to the baseline first), runs the full guarded delivery, including the
    last-moment evidence re-binding. A leftover index lock or any other source state stops without changing the state.

    Raises:
        PilotError: If the intent record or retained patches are not intact, the gate path differs from the recorded
            one, the index is locked, or Git shows neither the baseline, a recorded per-path mix, nor the exact
            delivered state.
    """
    state_path = state_file.path
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=False)
    patch_path = _recorded_delivery_patch(state, source)
    if state["schema_version"] == 3:
        if gates_path is None:
            raise PilotError("grouped-gate-evidence-required")
        recorded_gates = state_path.parent / str(state["source_application"].get("gates_path"))
        if Path(os.path.abspath(gates_path)) != Path(os.path.abspath(recorded_gates)):
            raise PilotError("grouped-gate-path-mismatch")
    elif gates_path is not None:
        raise PilotError("grouped-gate-unexpected")
    # A killed Git command can leave its index lock; every index write would fail until the operator clears it.
    if (source / _git(source, "rev-parse", "--git-path", "index.lock")).exists():
        raise PilotError("source-repository-index-locked")
    if _delivery_landed(source, state):
        return _record_source_applied(state_file, state, source)
    if not _delivery_untouched(source, state) and not _restore_interrupted_delivery(state_file, state, source):
        raise PilotError("source-application-resume-ambiguous")
    _, integration = _code_remediate_worktree_path(
        workspace, state["source_repository"], state.get("integration_worktree"), "integration-worktree"
    )
    return _deliver_source_application(state_file, state, source, integration, patch_path)


def _require_verified_reconciliation(state: dict[str, Any], source: Path) -> None:
    """Require a conflicted integration to carry its verified reconciliation record with unchanged bytes.

    Raises:
        PilotError: ``integration-reconciliation-evidence-drift`` when the record is missing, unverified or changed.
    """
    reconciliation = state.get("reconciliation")
    if state.get("integration_failed_node") is not None and reconciliation is None:
        raise PilotError("integration-reconciliation-evidence-drift")
    if reconciliation is None:
        return
    if not isinstance(reconciliation, dict) or reconciliation.get("status") != "verified":
        raise PilotError("integration-reconciliation-evidence-drift")
    _, record_file = _code_remediate_evidence_path(
        source, f"{state['evidence_root']}/{reconciliation.get('record_path')}", "reconciliation-record"
    )
    if _sha256(record_file) != reconciliation.get("record_sha256"):
        raise PilotError("integration-reconciliation-evidence-drift")


def _recorded_integration_paths(state: dict[str, Any]) -> list[str]:
    """Return the approved paths after requiring the recorded integration postimages to cover exactly those paths.

    Raises:
        PilotError: ``integration-worktree-drift`` when either postimage record is missing or covers other paths.
    """
    postimages = state.get("integration_final_sha256")
    postimage_filtered_oids = state.get("integration_final_filtered_oid")
    paths = sorted({path for node in state["nodes"] for path in node["owned_paths"]})
    if (
        not isinstance(postimages, dict)
        or not isinstance(postimage_filtered_oids, dict)
        or sorted(postimages) != paths
        or sorted(postimage_filtered_oids) != paths
    ):
        raise PilotError("integration-worktree-drift")
    return paths


def _grouped_gate_evidence(
    state_path: Path, state: dict[str, Any], integration: Path, gates_path: Path | None
) -> dict[str, str]:
    """Validate the run-local gates a grouped delivery needs and return their evidence; patch delivery takes none.

    Raises:
        PilotError: If grouped gates are missing, misplaced or invalid, or gates are passed for patch delivery.
    """
    if state["schema_version"] != 3:
        if gates_path is not None:
            raise PilotError("grouped-gate-unexpected")
        return {}
    if gates_path is None:
        raise PilotError("grouped-gate-evidence-required")
    _require_grouped_worker_receipts(state_path, state)
    gates_file = _code_remediate_cli_artifact_path(state_path, gates_path, "grouped-gates")
    if gates_file != state_path.parent / "gates.json":
        raise PilotError("grouped-gate-path-mismatch")
    gates_digest = _sha256(gates_file)
    _validate_grouped_gate_artifacts(
        state_path.parent, integration, state["integration_head"], state.get("tests_command")
    )
    return {"gates_path": gates_file.name, "gates_sha256": gates_digest}


def _require_integration_unchanged(state: dict[str, Any], integration: Path, paths: list[str]) -> None:
    """Re-establish the retained integration worktree's exact structural evidence before the source is written.

    The retained integration worktree is mutable host state, so its head, changed paths, postimages, child updates
    and, for grouped delivery, its commit chain must still match the record before any authoritative source patch is
    written or applied.

    Raises:
        PilotError: ``integration-worktree-drift`` when anything differs or cannot be read.
    """
    grouped = state["schema_version"] == 3
    baseline = state["baseline_head"] if grouped else "HEAD"
    expected_head = state["integration_head"] if grouped else state["baseline_head"]
    try:
        changed = sorted(_nul_paths(_git(integration, "diff", "--name-only", "--no-renames", "-z", baseline, "--")))
        observed_postimages = {path: _sha256(integration.joinpath(*PurePosixPath(path).parts)) for path in paths}
        observed_filtered_oids = {path: _git_filtered_oid(integration, path) for path in paths}
        for node in state["nodes"]:
            _raw_content_updates(integration, node, base=baseline)
        if grouped:
            _require_grouped_integration_tip(integration, baseline, state.get("integrated_commits"))
            _require_grouped_commit_chain(baseline, state["integrated_commits"], state.get("integrated_commit_objects"))
        if (
            _git(integration, "rev-parse", "HEAD") != expected_head
            or _git_bytes(integration, "ls-files", "--others", "-z")
            or changed != paths
            or observed_postimages != state["integration_final_sha256"]
            or observed_filtered_oids != state["integration_final_filtered_oid"]
        ):
            raise PilotError("integration-worktree-drift")
    except (OSError, PilotError) as error:
        raise PilotError("integration-worktree-drift") from error


def apply_code_remediate_source(*, state_path: Path, gates_path: Path | None = None) -> dict[str, object]:
    """Apply one verified integrated bundle to the unchanged authoritative source.

    The lifecycle saves ``source-applying`` with the forward and rollback patches and the source preimages before it
    writes to the source. A rerun after a crash resumes from that record: it records the delivery when Git shows it
    landed exactly, performs it when Git shows the source untouched, and otherwise stops without changing anything.
    Everything from writing the patches to saving the outcome, including any rollback or resume, runs inside the run's
    exclusive apply lock (``_run_apply_lock``), so a second apply of the same run stops with ``lifecycle-apply-locked``
    instead of writing the source at the same time. Right after taking the lock, and before every save, the state file
    must still hold what this call loaded or last saved, so an apply that finished in between is never overwritten.

    Args:
        state_path: Persisted lifecycle state of an integrated remediation run.
        gates_path: Run-local ``gates.json`` for grouped commits; must be omitted for patch delivery.

    Returns:
        The persisted source-application record.

    Raises:
        PilotError: If authority, integration evidence, gates, preflight, or the delivery postcondition fail, a
            resumed delivery cannot be proven from Git, another apply holds the run's lock (``lifecycle-apply-locked``),
            or another process changed the state file during this call (``lifecycle-state-concurrent-update``).
    """
    state, workspace = _read_state(state_path)
    state_file = _GuardedStateFile.loaded(state_path, state)
    if state.get("status") == "source-applying":
        with _run_apply_lock(state_path):
            # The resume may restore paths before its first save, so prove first that no apply finished meanwhile.
            state_file.require_current()
            return _resume_source_delivery(state_file, state, workspace, gates_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    if state.get("status") != "integrated" or state.get("integration_status") != "structurally-verified":
        raise PilotError("source-apply-before-integration-forbidden")
    _require_verified_reconciliation(state, source)
    _, integration = _code_remediate_worktree_path(
        workspace, state["source_repository"], state.get("integration_worktree"), "integration-worktree"
    )
    paths = _recorded_integration_paths(state)
    gates_evidence = _grouped_gate_evidence(state_path, state, integration, gates_path)
    _require_integration_unchanged(state, integration, paths)
    if state["schema_version"] == 3 and _git_bytes(source, "diff", "--cached", "--name-only", "-z"):
        raise PilotError("source-repository-index-dirty")
    with _run_apply_lock(state_path):
        # Another apply of the run may have finished between this call's read and the lock; never write over it.
        state_file.require_current()
        patch_path = _save_delivery_intent(state_file, state, source, integration, gates_evidence)
        return _deliver_source_application(state_file, state, source, integration, patch_path)


def _save_delivery_intent(
    state_file: _GuardedStateFile,
    state: dict[str, Any],
    source: Path,
    integration: Path,
    gates_evidence: dict[str, str],
) -> Path:
    """Write the forward and rollback patches, save ``source-applying`` with the source preimages, return the patch.

    Runs inside the run's apply lock, so no other apply writes these patches or reads them while they are rewritten.

    Raises:
        PilotError: If either patch is empty, an output holds other bytes, or another process changed the state file.
    """
    state_path = state_file.path
    paths = sorted({path for node in state["nodes"] for path in node["owned_paths"]})
    baseline = state["baseline_head"] if state["schema_version"] == 3 else "HEAD"
    preimages = {path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths}
    preimage_filtered_oids = {path: _git_filtered_oid(source, path) for path in paths}
    _, patch_path = _code_remediate_evidence_path(
        source,
        _relative_workspace_path(source, state_path.parent / "source-application.patch", "source-application-patch"),
        "source-application-patch",
    )
    _, rollback_path = _code_remediate_evidence_path(
        source,
        _relative_workspace_path(source, state_path.parent / "rollback.patch", "rollback-patch"),
        "rollback-patch",
    )
    forward = _git_bytes(integration, "diff", "--binary", "--full-index", baseline, "--", *paths)
    reverse = _git_bytes(integration, "diff", "-R", "--binary", "--full-index", baseline, "--", *paths)
    if not forward or not reverse:
        raise PilotError("source-application-patch-empty")
    for output, data in ((patch_path, forward), (rollback_path, reverse)):
        # A crash after these writes but before the intent record is saved leaves identical bytes; only those may stay.
        if output.exists() and (not output.is_file() or output.read_bytes() != data):
            raise PilotError("source-application-output-exists")
        output.write_bytes(data)
    # Preimages belong to the intent record: a crash after the source write must still be able to finish the receipt.
    application = {
        "status": "applying",
        "patch_path": _relative_workspace_path(state_path.parent, patch_path, "source-application-patch"),
        "patch_sha256": _sha256(patch_path),
        "rollback_patch_path": _relative_workspace_path(state_path.parent, rollback_path, "rollback-patch"),
        "rollback_patch_sha256": _sha256(rollback_path),
        "applied_paths": paths,
        "preimage_sha256": preimages,
        "preimage_filtered_oid": preimage_filtered_oids,
        **gates_evidence,
    }
    state.update({"status": "source-applying", "source_application": application})
    state_file.save(state)
    return patch_path


def _worktree_records(source: Path) -> dict[Path, dict[str, str]]:
    """Read Git's worktree registrations with their annotations, including entries whose directories vanished.

    Each registered path maps the attribute lines ``git worktree list --porcelain`` prints for it, keyed by their first
    word (``HEAD``, ``detached``, ``locked``, ``prunable``) with the rest of the line as the value.
    """
    records: dict[Path, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in _git_bytes(source, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        if line.startswith(b"worktree "):
            current = records.setdefault(Path(os.fsdecode(line.removeprefix(b"worktree "))), {})
        elif line and current is not None:
            key, _, value = os.fsdecode(line).partition(" ")
            current[key] = value
    return records


def _registered_worktree_paths(source: Path) -> set[Path]:
    """Read Git's worktree registrations, including entries whose directories vanished."""
    return set(_worktree_records(source))


def _prunable_worktree_paths(source: Path) -> set[Path]:
    """Return every registered worktree Git reports as prunable, such as one whose directory is gone.

    Git marks such an entry with a ``prunable`` line in ``git worktree list --porcelain``; a locked worktree is never
    reported prunable, and Git versions without the annotation report none, which keeps every caller fail-closed.
    """
    return {path for path, record in _worktree_records(source).items() if "prunable" in record}


def _prune_pending_registration(source: Path, worktree: Path) -> None:
    """Drop Git's registration of the pending worktree whose directory a crashed removal already deleted.

    ``git worktree prune`` cannot be limited to one worktree, so it runs only while Git reports exactly this worktree
    as prunable: another missing worktree, which this cleanup cannot prove it removed, keeps the error, and so does a
    locked one, which Git never prunes.

    Raises:
        PilotError: ``cleanup-unrecorded-worktree-removal`` when any other worktree is prunable or Git still registers
            the target afterwards.
    """
    if _prunable_worktree_paths(source) != {worktree}:
        raise PilotError("cleanup-unrecorded-worktree-removal")
    _git(source, "worktree", "prune")
    if worktree in _registered_worktree_paths(source):
        raise PilotError("cleanup-unrecorded-worktree-removal")


def _registered_administrative_directory(source: Path, worktree: Path) -> Path:
    """Return the one administrative directory Git keeps for a worktree whose own ``.git`` file is gone.

    Until Git unregisters a worktree, ``<common>/worktrees/<id>/gitdir`` still names the worktree's ``.git`` path, so
    the registration stays provable after a crashed removal deleted that file.

    Raises:
        PilotError: ``cleanup-unrecorded-worktree-removal`` unless exactly one administrative directory names it.
    """
    try:
        expected = (worktree / ".git").resolve(strict=False)
        matches = [
            administrative
            for administrative in (_source_common_directory(source) / "worktrees").iterdir()
            if (administrative / "gitdir").is_file()
            and (administrative / (administrative / "gitdir").read_text(encoding="utf-8").strip()).resolve(strict=False)
            == expected
        ]
    except (OSError, UnicodeError) as error:
        raise PilotError("cleanup-unrecorded-worktree-removal") from error
    if len(matches) != 1:
        raise PilotError("cleanup-unrecorded-worktree-removal")
    return matches[0]


def _administered_worktree_git(source: Path, administrative: Path, worktree: Path, *arguments: str) -> bytes:
    """Run Git for a linked worktree through its administrative directory, which still works once its ``.git`` is gone.

    Explicit ``--git-dir`` and ``--work-tree`` keep Git from discovering a repository upward from a directory without a
    ``.git`` entry, which for a source-local worktree root would answer for the source checkout itself. Executable
    clean, smudge and process filters are refused first, as every other lifecycle status read does.
    """
    try:
        _DIFF_MODULE.reject_executable_filters(source, environment=_git_environment())
    except RuntimeError as error:
        raise PilotError(str(error)) from error
    return _git_bytes(
        source, "--no-optional-locks", f"--git-dir={administrative}", f"--work-tree={worktree}", *arguments
    )


def _partial_worktree_status(source: Path, administrative: Path, worktree: Path) -> list[bytes]:
    """Return Git's porcelain status records for a worktree, listing untracked files one by one and ignored ones not.

    Git's own non-force removal check reads the same status without ignored files, because ``git worktree remove``
    deletes them with the directory.
    """
    output = _administered_worktree_git(
        source,
        administrative,
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    return [record for record in output.split(b"\0") if record]


def _has_populated_submodule(source: Path, administrative: Path, worktree: Path) -> bool:
    """Return whether the worktree's index records a submodule whose directory still holds a ``.git`` entry.

    ``git worktree remove`` refuses such a worktree even when it is clean, since the submodule's own history would go
    with it.
    """
    staged = _administered_worktree_git(source, administrative, worktree, "ls-files", "--stage", "-z")
    for record in staged.split(b"\0"):
        mode, _, entry = record.partition(b" ")
        if mode == b"160000":
            relative = os.fsdecode(entry.partition(b"\t")[2])
            gitlink = worktree.joinpath(*PurePosixPath(relative).parts) / ".git"
            if gitlink.exists() or gitlink.is_symlink():
                return True
    return False


def _interrupted_removal_admin(source: Path, worktree: Path) -> Path | None:
    """Return the administrative directory of the pending worktree when a crashed removal left it partly deleted.

    ``git worktree remove`` checks the worktree is clean, deletes its directory entry by entry and unregisters it last.
    A crash during the delete leaves either the ``.git`` file gone, or it in place with tracked files deleted and every
    other entry as clean as Git found it. ``None`` means the removal never started (the directory is whole, or holds
    changes other than deletions), and the ordinary non-force removal runs and reports whatever Git refuses.
    """
    if worktree.is_symlink():
        return None
    if _gitfile_missing(worktree):
        return _registered_administrative_directory(source, worktree)
    try:
        administrative = _verify_worktree_gitfile(source, worktree)
    except PilotError:
        return None
    status = _partial_worktree_status(source, administrative, worktree)
    return administrative if status and all(record.startswith(b" D ") for record in status) else None


def _finish_interrupted_removal(source: Path, worktree: Path, administrative: Path) -> None:
    """Finish a non-force removal a crash cut short, deleting the rest of the worktree only under Git's own guarantees.

    ``git worktree remove`` deletes a worktree only while Git shows nothing untracked or changed and no populated
    submodule, and it deletes ignored files with the directory. The rest of the directory is deleted under that same
    check, read through the administrative directory with deleted tracked files as the only change accepted, and only
    while the worktree is not locked and no other worktree is prunable, because the prune that follows cannot be limited
    to this one. ``shutil.rmtree`` never follows a symbolic link or a Windows junction inside the directory. Nothing is
    saved here, so a delete that fails part-way keeps the pending mark and the next resume takes this path again.

    Raises:
        PilotError: ``cleanup-unrecorded-worktree-removal`` for a locked target or another prunable worktree,
            ``cleanup-partial-worktree-unclean`` for anything Git does not show as a deleted or unmodified tracked file
            or an ignored one, and ``cleanup-partial-worktree-removal-failed`` when the directory cannot be deleted.
    """
    records = _worktree_records(source)
    prunable = {path for path, record in records.items() if "prunable" in record}
    if "locked" in records.get(worktree, {}) or prunable != ({worktree} if _gitfile_missing(worktree) else set()):
        raise PilotError("cleanup-unrecorded-worktree-removal")
    status = _partial_worktree_status(source, administrative, worktree)
    if any(not record.startswith(b" D ") for record in status) or _has_populated_submodule(
        source, administrative, worktree
    ):
        raise PilotError("cleanup-partial-worktree-unclean")
    try:
        shutil.rmtree(worktree)
    except OSError as error:
        raise PilotError("cleanup-partial-worktree-removal-failed") from error
    _prune_pending_registration(source, worktree)


def _prior_cleanup_receipt(state: dict[str, Any], targets: list[str]) -> tuple[list[str], object]:
    """Return the removals an earlier cleanup recorded and the target it was removing, after checking its receipt.

    Raises:
        PilotError: ``cleanup-receipt-invalid`` unless the receipt is absent, or an in-progress or failed non-force
            receipt whose removals are a prefix of ``targets`` and whose pending target, if any, is the next one.
    """
    prior = state.get("cleanup")
    prior_status = state.get("cleanup_status")
    if prior_status == "not-started" and prior is None:
        return [], None
    if (
        prior_status not in {"in-progress", "failed"}
        or not isinstance(prior, dict)
        or prior.get("status") != prior_status
        or prior.get("force") is not False
        or not isinstance(prior.get("worktrees"), list)
        or prior["worktrees"] != targets[: len(prior["worktrees"])]
        or prior.get("removing") not in (None, *targets[len(prior["worktrees"]) : len(prior["worktrees"]) + 1])
    ):
        raise PilotError("cleanup-receipt-invalid")
    return list(prior["worktrees"]), prior.get("removing")


def _resumed_cleanup_removals(
    state: dict[str, Any], source: Path, targets: list[str], worktrees: list[Path]
) -> list[str]:
    """Return the removals a resumed cleanup may treat as done, reconciling one a crash left unrecorded.

    Each removal is preceded by a saved ``removing`` record naming its target. A crash after Git removed that target
    but before the receipt was saved leaves it missing; it counts as removed only when it is that recorded pending
    target and its directory is gone. ``git worktree remove`` deletes the directory before Git's own registration, so
    a crash inside it can leave the pending target still registered; once every other target checks out, that
    registration is pruned (``_prune_pending_registration``) and the target counts as removed. A crash part-way through
    Git's delete leaves the pending directory partly present, which Git no longer removes without force; once every
    other target checks out, the rest is deleted under Git's own non-force guarantees and the registration pruned
    (``_finish_interrupted_removal``). Every other missing worktree stays an error. Nothing is saved before those
    checks pass, so a refused resume keeps the pending mark.

    Raises:
        PilotError: If the prior receipt is malformed, a recorded removal reappeared, a worktree vanished without
            Git-provable removal by this lifecycle, or a partly removed pending worktree holds anything Git does not
            show as clean.
    """
    removed, pending = _prior_cleanup_receipt(state, targets)
    registered = _registered_worktree_paths(source)
    still_registered: Path | None = None
    interrupted: tuple[Path, Path] | None = None
    for relative, worktree in zip(targets, worktrees, strict=True):
        if relative in removed:
            if worktree.exists() or worktree in registered:
                raise PilotError("cleanup-removed-worktree-reappeared")
        elif relative == pending and not worktree.exists() and not worktree.is_symlink():
            still_registered = worktree if worktree in registered else None
            removed.append(relative)
        elif not worktree.is_dir() or worktree not in registered:
            raise PilotError("cleanup-unrecorded-worktree-removal")
        elif relative == pending and (administrative := _interrupted_removal_admin(source, worktree)) is not None:
            interrupted = (worktree, administrative)
            removed.append(relative)
    if interrupted is not None:
        _finish_interrupted_removal(source, *interrupted)
    elif still_registered is not None:
        _prune_pending_registration(source, still_registered)
    return removed


class _CleanupTargets(NamedTuple):
    """The worktrees one cleanup removes, with the source and approved paths each removal needs."""

    source: Path
    paths: list[str]
    relatives: list[str]
    worktrees: list[Path]


def _verified_cleanup_targets(state: dict[str, Any], workspace: Path, state_path: Path) -> _CleanupTargets:
    """Prove the delivery is applied with intact rollback evidence, then return every worktree cleanup removes.

    Raises:
        PilotError: If authority, the applied record, the source postimages or the rollback evidence do not match.
    """
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=False)
    application = state.get("source_application")
    if (
        state.get("status") != "source-applied"
        or not isinstance(application, dict)
        or application.get("status") != "applied"
    ):
        raise PilotError("cleanup-before-source-application-forbidden")
    paths = application.get("applied_paths")
    if not isinstance(paths, list) or state.get("source", {}).get("postimage_sha256") != {
        path: _sha256(source.joinpath(*PurePosixPath(path).parts)) for path in paths
    }:
        raise PilotError("cleanup-source-postimage-mismatch")
    if state["schema_version"] == 3:
        _require_unchanged_code_remediate_source(source, paths, state["integration_head"])
    rollback_relative = _portable_relative(application.get("rollback_patch_path"), "rollback-patch")
    _, rollback = _code_remediate_evidence_path(
        source, f"{state['evidence_root']}/{rollback_relative}", "rollback-patch"
    )
    if _sha256(rollback) != application.get("rollback_patch_sha256"):
        raise PilotError("cleanup-rollback-evidence-mismatch")
    relatives = [node["worktree_path"] for node in state["nodes"]] + [state["integration_worktree"]]
    worktrees = [
        _code_remediate_worktree_path(workspace, state["source_repository"], relative, "cleanup-worktree")[1]
        for relative in relatives
    ]
    return _CleanupTargets(source, paths, relatives, worktrees)


def _remove_cleanup_worktree(
    state_file: _GuardedStateFile, state: dict[str, Any], workspace: Path, targets: _CleanupTargets, relative: str
) -> None:
    """Remove one worktree without force, recording the pending target first and the receipt afterwards.

    The worktree path is re-derived and its Git link re-verified right before the removal, so a path changed since the
    targets were listed fails this removal instead of reaching ``git worktree remove``.

    Raises:
        PilotError: ``cleanup-failed`` or ``cleanup-postcondition-failed`` after saving the failed receipt.
    """
    cleanup = state["cleanup"]
    # Name the target before Git removes it, so a crash right after the removal can be reconciled on resume.
    cleanup["removing"] = relative
    state_file.save(state)
    try:
        _, worktree = _code_remediate_worktree_path(workspace, state["source_repository"], relative, "cleanup-worktree")
        _verify_worktree_gitfile(targets.source, worktree)
        if state["schema_version"] == 2:
            _git(worktree, "restore", "--staged", "--worktree", "--source=HEAD", "--", *targets.paths)
        _git(targets.source, "worktree", "remove", str(worktree))
    except PilotError as error:
        # A reported failure is for the operator, not crash recovery. Drop the pending mark so a later out-of-band
        # removal, possibly forced, can never be recorded as this non-force one.
        del cleanup["removing"]
        cleanup["status"] = "failed"
        state["cleanup_status"] = "failed"
        state_file.save(state)
        raise PilotError("cleanup-failed") from error
    if worktree.exists() or worktree.is_symlink() or worktree in _registered_worktree_paths(targets.source):
        cleanup["status"] = "failed"
        state["cleanup_status"] = "failed"
        state_file.save(state)
        raise PilotError("cleanup-postcondition-failed")
    cleanup["worktrees"].append(relative)
    del cleanup["removing"]
    state_file.save(state)


def cleanup_code_remediate_pilot(*, state_path: Path) -> dict[str, object]:
    """Resume non-force removals after checking each durable removal against the repository.

    Cleanup holds the run's exclusive lock (``_run_apply_lock``) for its whole run, rereads the state right after
    taking it, and saves every receipt through the guarded state file. A second cleanup or apply of the same run
    therefore stops with ``lifecycle-apply-locked``, or with ``lifecycle-state-concurrent-update`` when it read the
    state before this one recorded it, instead of saving a stale receipt over a completed one.

    Args:
        state_path: Persisted lifecycle state of a run whose delivery is recorded as applied.

    Returns:
        The cleanup outcome, always ``removed`` and non-force.

    Raises:
        PilotError: If the applied delivery or its evidence no longer matches, a worktree vanished without
            Git-provable removal, a removal fails, another holder has the run's lock, or another process changed the
            state file.
    """
    state, workspace = _read_state(state_path)
    state_file = _GuardedStateFile.loaded(state_path, state)
    with _run_apply_lock(state_path):
        state_file.require_current()
        targets = _verified_cleanup_targets(state, workspace, state_path)
        removed = _resumed_cleanup_removals(state, targets.source, targets.relatives, targets.worktrees)
        state.update(
            {
                "cleanup_status": "in-progress",
                "cleanup": {"status": "in-progress", "force": False, "worktrees": removed},
            }
        )
        state_file.save(state)
        for relative in targets.relatives[len(removed) :]:
            _remove_cleanup_worktree(state_file, state, workspace, targets, relative)
        state["cleanup"]["status"] = "removed"
        state.update({"status": "completed", "cleanup_status": "removed"})
        state_file.save(state)
    return {"cleanup_status": "removed", "force": False}


def _code_remediate_cli_artifact_path(state_path: Path, artifact_path: Path, label: str) -> Path:
    """Resolve one CLI handover artifact below the exact run evidence root without symlinks."""
    state, workspace = _read_state(state_path)
    source, _ = _code_remediate_authority(state, workspace, state_path, clean=True)
    try:
        absolute = Path(os.path.abspath(artifact_path))
        source_relative = absolute.relative_to(source).as_posix()
    except (OSError, ValueError) as error:
        raise PilotError(f"{label}-outside-run") from error
    relative, candidate = _code_remediate_evidence_path(source, source_relative, label)
    root_parts = PurePosixPath(state["evidence_root"]).parts
    if PurePosixPath(relative).parts[: len(root_parts)] != root_parts or len(PurePosixPath(relative).parts) == len(
        root_parts
    ):
        raise PilotError(f"{label}-outside-run")
    return candidate


def _write_handover_json(state_path: Path, output_path: Path, handover: dict[str, object]) -> None:
    """Persist one child handover only inside its prepared remediation run directory."""
    output_path = _code_remediate_cli_artifact_path(state_path, output_path, "child-handover-output")
    if output_path.exists() or output_path.is_symlink():
        raise PilotError("child-handover-output-exists")
    output_path.write_text(json.dumps(handover, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _cli_parser() -> argparse.ArgumentParser:
    """Build the narrow explicit command surface for the schema-v2 lifecycle."""
    parser = argparse.ArgumentParser(description="Run the frozen code-remediate parallel-write lifecycle.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare approved remediation worktrees")
    prepare.add_argument("--plan", type=Path, required=True)
    prepare.add_argument("--approval", type=Path, required=True)
    prepare.add_argument("--workspace", type=Path, required=True)
    prepare.add_argument("--state", type=Path, required=True)
    handover = commands.add_parser("create-handover", help="write one parent-verifiable child handover JSON")
    handover.add_argument("--state", type=Path, required=True)
    handover.add_argument("--node", required=True)
    handover.add_argument("--summary", required=True)
    handover.add_argument("--output", type=Path, required=True)
    handover.add_argument(
        "--verification", type=Path, help="retained advisory worker receipt for a grouped commit (self-reported)"
    )
    join = commands.add_parser("join", help="join completed child handover JSON files")
    join.add_argument("--state", type=Path, required=True)
    join.add_argument("--handover", type=Path, action="append", required=True)
    collect = commands.add_parser("collect", help="collect one parent-derived child patch")
    collect.add_argument("--state", type=Path, required=True)
    collect.add_argument("--node", required=True)
    reconcile = commands.add_parser("reconcile", help="verify a parent-edited conflicting integration")
    reconcile.add_argument("--state", type=Path, required=True)
    reconcile.add_argument("--record", type=Path, required=True)
    for command, help_text in (
        ("integrate", "integrate all collected child patches"),
        ("apply-source", "apply the verified integrated source bundle"),
        ("cleanup", "remove successful worktrees without force"),
    ):
        subparser = commands.add_parser(command, help=help_text)
        subparser.add_argument("--state", type=Path, required=True)
        if command == "apply-source":
            subparser.add_argument("--gates", type=Path, help="verified run-local gates.json for grouped commits")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one explicit code-remediate lifecycle subcommand and emit JSON evidence."""
    args = _cli_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_code_remediate_pilot(
                plan_path=args.plan, approval_path=args.approval, workspace_root=args.workspace, state_path=args.state
            )
        elif args.command == "create-handover":
            result = create_completed_child_handover(
                state_path=args.state, node_id=args.node, summary=args.summary, verification_path=args.verification
            )
            _write_handover_json(args.state, args.output, result)
        elif args.command == "join":
            result = join_child_handovers(
                state_path=args.state,
                handovers=[
                    _load_json(_code_remediate_cli_artifact_path(args.state, path, "child-handover"), "child-handover")
                    for path in args.handover
                ],
            )
        elif args.command == "collect":
            result = collect_write_patch(state_path=args.state, node_id=args.node)
        elif args.command == "integrate":
            result = integrate_write_pilot(state_path=args.state)
        elif args.command == "reconcile":
            result = reconcile_code_remediate_integration(state_path=args.state, record_path=args.record)
        elif args.command == "apply-source":
            result = apply_code_remediate_source(state_path=args.state, gates_path=args.gates)
        else:
            result = cleanup_code_remediate_pilot(state_path=args.state)
    except PilotError as error:
        LOGGER.error("Code-remediate lifecycle failed: %s", error)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
