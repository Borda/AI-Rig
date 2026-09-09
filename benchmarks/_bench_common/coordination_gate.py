"""Safe construction, validation, and removal of the Codemap read/write gate skeleton.

The gate is the ``.index-rw`` directory the product's ``rwgate`` coordinates readers and writers through: a ``readers/``
directory of per-lease token files and a one-byte ``registry.lock`` acquisition mutex. A measured cell hands the model a
sandbox that is read-only everywhere else, so this directory is the single writable path in the whole profile — which is
why it is built away from the frozen index and discarded with the cell that owns it.

Admission and teardown ask different questions of the same directory. Admission is fail-closed: a gate is entered only
when every entry in it is a known-safe skeleton part, because a dirty gate would let one cell's residue reach the next.
Teardown is not an admission decision — the directory is about to cease existing, and the sandbox profile explicitly
grants the model write access to it, so scratch the model left behind is permitted behaviour rather than a fault. A
teardown therefore removes dead residue and reports it, and fails only on the two conditions that mean something is
wrong outside this directory: a symlink anywhere in the path, or a lock still held by a living process.

Extracted from the public Codex runner: the locking rules here are gate plumbing that the runner only calls, and the
runner is held to a maintenance size limit that this block was consuming without belonging to it.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
    import msvcrt
else:
    import fcntl

COORDINATION_NAME = ".index-rw"
READERS_NAME = "readers"
REGISTRY_NAME = "registry.lock"


def assert_safe_path_components(path: Path) -> None:
    """Reject symlink components in an existing absolute filesystem path.

    Examples:
        >>> assert_safe_path_components(Path(os.path.abspath(os.sep)))
    """
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"permission path contains a symlink: {current}")


def try_lock_coordination_file(fd: int) -> bool:
    """Try to lock one rwgate coordination handle without blocking.

    Examples:
        >>> try_lock_coordination_file.__name__
        'try_lock_coordination_file'
    """
    try:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock_coordination_file(fd: int) -> None:
    """Release one rwgate coordination handle held by this process.

    Examples:
        >>> unlock_coordination_file.__name__
        'unlock_coordination_file'
    """
    try:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.lockf(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def reap_stale_reader_tokens(readers: Path, registry: Path) -> None:
    """Remove unlocked reader tokens while preserving live rwgate leases.

    A token whose lock is still held names a reader that has not finished, so it is reported rather than removed; an
    unlocked one is the residue of a process that died mid-lease.

    Examples:
        >>> reap_stale_reader_tokens.__name__
        'reap_stale_reader_tokens'
    """
    registry_fd = os.open(registry, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    registry_locked = False
    try:
        registry_locked = try_lock_coordination_file(registry_fd)
        if not registry_locked:
            raise ValueError("Codemap coordination registry is busy")
        live_tokens: list[str] = []
        for token in sorted(readers.iterdir()):
            try:
                metadata = token.lstat()
            except FileNotFoundError:
                continue
            if token.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError(f"Codemap readers path has an unsafe entry: {token.name}")
            try:
                token_fd = os.open(token, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            except FileNotFoundError:
                continue
            try:
                if not try_lock_coordination_file(token_fd):
                    live_tokens.append(token.name)
                    continue
                unlock_coordination_file(token_fd)
            finally:
                os.close(token_fd)
            with contextlib.suppress(FileNotFoundError):
                token.unlink()
        if live_tokens:
            raise ValueError(f"Codemap coordination root has live reader tokens: {live_tokens}")
    finally:
        if registry_locked:
            unlock_coordination_file(registry_fd)
        os.close(registry_fd)


def validate_coordination_root(coordination_root: Path) -> None:
    """Fail unless the coordination root is a safe, idle rwgate skeleton.

    Examples:
        >>> validate_coordination_root.__name__
        'validate_coordination_root'
    """
    assert_safe_path_components(coordination_root)
    try:
        root_metadata = coordination_root.lstat()
    except OSError as exc:
        raise ValueError("Codemap coordination root is unavailable") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Codemap coordination root must be a directory")

    allowed = {READERS_NAME, REGISTRY_NAME}
    entries = {entry.name for entry in coordination_root.iterdir()}
    if entries != allowed:
        raise ValueError(f"Codemap coordination root has unexpected or live entries: {sorted(entries - allowed)}")

    readers = coordination_root / READERS_NAME
    registry = coordination_root / REGISTRY_NAME
    readers_metadata = readers.lstat()
    registry_metadata = registry.lstat()
    if not stat.S_ISDIR(readers_metadata.st_mode) or readers.is_symlink():
        raise ValueError("Codemap readers path must be a real directory")
    if not stat.S_ISREG(registry_metadata.st_mode) or registry.is_symlink():
        raise ValueError("Codemap registry must be a regular file")
    if registry_metadata.st_nlink != 1 or registry.read_bytes() != b"L":
        raise ValueError("Codemap registry identity is invalid")
    reap_stale_reader_tokens(readers, registry)


def prepare_coordination_root(index_dir: Path, coordination_root: Path | None = None) -> Path:
    """Create a clean rwgate skeleton and return its path.

    Args:
        index_dir: Canonical directory holding the index this gate guards.
        coordination_root: Directory to build the skeleton in. Omitted, it is ``.index-rw`` inside *index_dir*, the
            product's own layout. A measured cell passes a directory outside the workspace instead, because the gate is
            the only path its sandbox may write to and a model that treats it as scratch space must not be able to
            dirty the frozen index's directory.

    Returns:
        The prepared coordination root.

    Raises:
        ValueError: If an existing root is not an idle skeleton, or a new one cannot be created safely.

    Examples:
        >>> prepare_coordination_root.__name__
        'prepare_coordination_root'
    """
    if coordination_root is None:
        coordination_root = index_dir / COORDINATION_NAME
    if coordination_root.exists() or coordination_root.is_symlink():
        validate_coordination_root(coordination_root)
        return coordination_root

    try:
        coordination_root.mkdir(mode=0o700, parents=True)
        readers = coordination_root / READERS_NAME
        readers.mkdir(mode=0o700)
        registry = coordination_root / REGISTRY_NAME
        fd = os.open(
            registry,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.write(fd, b"L")
        finally:
            os.close(fd)
    except OSError as exc:
        raise ValueError("Codemap coordination root could not be created safely") from exc
    validate_coordination_root(coordination_root)
    return coordination_root


def remove_residue_entry(entry: Path) -> None:
    """Delete one non-skeleton gate entry without ever following a symlink.

    A directory is emptied depth-first through ``lstat`` rather than handed to a bulk remover, so a symlink inside the
    residue is unlinked as a link and never descended into.

    Examples:
        >>> remove_residue_entry.__name__
        'remove_residue_entry'
    """
    metadata = entry.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        entry.unlink()
        return
    for child in sorted(entry.iterdir()):
        remove_residue_entry(child)
    entry.rmdir()


def assert_registry_idle(registry: Path) -> None:
    """Fail when a living process still holds the rwgate acquisition mutex.

    An absent, replaced, or non-regular registry is residue for the caller to clear: nothing holds a lock that no longer
    exists, and the gate is per-cell, so no second reader can be waiting on it.

    Examples:
        >>> assert_registry_idle.__name__
        'assert_registry_idle'
    """
    try:
        metadata = registry.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return
    fd = os.open(registry, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not try_lock_coordination_file(fd):
            raise ValueError("Codemap coordination registry is busy")
        unlock_coordination_file(fd)
    finally:
        os.close(fd)


def classify_reader_entries(readers: Path) -> tuple[list[str], list[Path], list[Path]]:
    """Sort one readers directory into live leases, dead leases, and entries that were never leases.

    Returns:
        Names of tokens whose lock is still held, paths of tokens whose holder is gone, and paths of entries that do
        not have a token's shape at all.

    Examples:
        >>> classify_reader_entries.__name__
        'classify_reader_entries'
    """
    live: list[str] = []
    dead: list[Path] = []
    residue: list[Path] = []
    for entry in sorted(readers.iterdir()):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            residue.append(entry)
            continue
        try:
            token_fd = os.open(entry, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            continue
        try:
            if not try_lock_coordination_file(token_fd):
                live.append(entry.name)
                continue
            unlock_coordination_file(token_fd)
        finally:
            os.close(token_fd)
        dead.append(entry)
    return live, dead, residue


def assert_coordination_root_idle(coordination_root: Path) -> None:
    """Fail when the gate is unreachable, unsafe, or still held, tolerating scratch the sandbox permitted.

    This is the question a cell can be judged on: whether anything is still alive in the gate or the path to it has
    been redirected. What the model wrote inside a directory it was granted write access to is not part of it.

    Raises:
        ValueError: If the root is missing, is not a real directory, is reached through a symlink, or still carries a
            held lock.

    Examples:
        >>> assert_coordination_root_idle.__name__
        'assert_coordination_root_idle'
    """
    assert_safe_path_components(coordination_root)
    try:
        root_metadata = coordination_root.lstat()
    except OSError as exc:
        raise ValueError("Codemap coordination root is unavailable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("Codemap coordination root must be a directory")
    assert_registry_idle(coordination_root / REGISTRY_NAME)
    readers = coordination_root / READERS_NAME
    if not readers.is_dir() or readers.is_symlink():
        return
    live, _dead, _residue = classify_reader_entries(readers)
    if live:
        raise ValueError(f"Codemap coordination root has live reader tokens: {live}")


def drain_reader_tokens(readers: Path) -> list[str]:
    """Drop dead reader leases, refuse live ones, and report the entries that were never leases.

    Returns:
        Relative names of removed entries that did not have the shape of a reader token.

    Raises:
        ValueError: If any token's lock is still held, which names a reader process that outlived its cell.

    Examples:
        >>> drain_reader_tokens.__name__
        'drain_reader_tokens'
    """
    live, dead, residue = classify_reader_entries(readers)
    if live:
        raise ValueError(f"Codemap coordination root has live reader tokens: {live}")
    for token in dead:
        with contextlib.suppress(FileNotFoundError):
            token.unlink()
    for entry in residue:
        remove_residue_entry(entry)
    return [f"{READERS_NAME}/{entry.name}" for entry in residue]


def cleanup_coordination_root(coordination_root: Path) -> list[str]:
    """Discard one cell's rwgate skeleton along with whatever the cell left inside it.

    Returns:
        Sorted relative paths of the removed entries that were not part of the skeleton. An empty list means the gate
        was untouched; anything else is an observation about the cell, not a cleanup failure.

    Raises:
        ValueError: If the root is missing or is not a real directory, if the path reaches it through a symlink, if a
            lock is still held, or if the removal itself fails.

    Examples:
        >>> cleanup_coordination_root.__name__
        'cleanup_coordination_root'
    """
    assert_coordination_root_idle(coordination_root)
    residue: list[str] = []
    readers = coordination_root / READERS_NAME
    if readers.is_dir() and not readers.is_symlink():
        residue.extend(drain_reader_tokens(readers))

    try:
        for entry in sorted(coordination_root.iterdir()):
            if entry.name in {READERS_NAME, REGISTRY_NAME} and not entry.is_symlink():
                continue
            residue.append(entry.name)
            remove_residue_entry(entry)
        for name in (REGISTRY_NAME, READERS_NAME):
            skeleton_part = coordination_root / name
            if skeleton_part.exists() or skeleton_part.is_symlink():
                remove_residue_entry(skeleton_part)
        coordination_root.rmdir()
    except OSError as exc:
        raise ValueError("Codemap coordination root cleanup failed") from exc
    return sorted(residue)
