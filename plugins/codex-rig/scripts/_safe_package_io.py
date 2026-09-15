"""Read installed package content through bounded, no-link filesystem handles.

## Purpose

Prevent package validation from following unsafe links or accepting unstable filesystem objects. The module provides the
descriptor-level guarantees required before manifest hashes can be treated as package identity.

## Scope

Provides platform-specific safe reads and inventories; package semantics and manifest comparisons live in
``_package_identity.py``. It is limited to bounded regular-file access and does not decide which files a package is
allowed to contain.

## Usage

Import ``read_safe_file`` or ``inventory_package_files`` from package-identity code rather than calling this internal
module directly. Callers should treat the returned bytes and identity metadata as one observation and rerun the read if
they need a new snapshot.

## Used by

Installed-package verification and its cross-platform safety tests call these safe I/O helpers. The identity layer
relies on their inventory to detect unlisted payload files and to hash files without following replacement links.

## Outputs

Returns bounded ``SafeFile`` values and a deterministic regular-file inventory with the identity facts needed by
manifest validation. Entries include canonical relative paths and descriptor metadata so callers can detect changes
between opening and reading.

## Failure

Path escape, symlink, reparse point, descriptor mismatch, unsupported object type, or size limit violation raises
``SafePackageIOError``. This is intentionally fail-closed: package validation must report the unsafe object instead of
hashing or skipping it.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_PATH_BYTES = 16_384
_READ_CHUNK_BYTES = 65_536
_REPARSE_POINT = 0x400


class SafePackageIOError(OSError):
    """Report a package path that cannot be read with stable identity."""


@dataclass(frozen=True)
class SafeFile:
    """Hold bounded bytes and stable metadata from one verified file handle."""

    payload: bytes
    mode: int


def _relative_parts(relative: str) -> tuple[str, ...]:
    """Validate one canonical manifest path and return its components."""
    if not isinstance(relative, str) or len(relative.encode("utf-8")) > MAX_PATH_BYTES:
        raise SafePackageIOError("invalid package path")
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or not path.parts
        or "\\" in relative
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise SafePackageIOError(f"invalid package path: {relative}")
    if any(ord(character) < 32 for character in relative):
        raise SafePackageIOError(f"invalid package path: {relative}")
    return path.parts


def _stable_stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return fields that must stay fixed across one bounded read."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_descriptor(descriptor: int, maximum: int, relative: str) -> SafeFile:
    """Read one held regular descriptor and reject concurrent identity drift."""
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SafePackageIOError(f"unsafe package node: {relative}")
    if before.st_size < 0 or before.st_size > maximum:
        raise SafePackageIOError(f"oversized package file: {relative}")
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    after = os.fstat(descriptor)
    if len(payload) > maximum:
        raise SafePackageIOError(f"oversized package file: {relative}")
    if _stable_stat_identity(before) != _stable_stat_identity(after):
        raise SafePackageIOError(f"package file changed during read: {relative}")
    return SafeFile(payload=payload, mode=stat.S_IMODE(after.st_mode))


def _open_posix_root(root: Path) -> int:
    """Open an absolute POSIX directory component-by-component without links."""
    absolute = Path(os.path.abspath(root))
    parts = absolute.parts
    if not parts or parts[0] != absolute.anchor:
        raise SafePackageIOError(f"package root must be absolute: {root}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    descriptor = os.open(absolute.anchor, flags)
    try:
        for part in parts[1:]:
            next_descriptor = os.open(part, flags | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_posix(root: Path, relative: str, maximum: int) -> SafeFile:
    """Read one relative POSIX file through held no-follow directory handles."""
    parts = _relative_parts(relative)
    directory = _open_posix_root(root)
    try:
        for part in parts[:-1]:
            next_directory = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            return _read_descriptor(descriptor, maximum, relative)
        finally:
            os.close(descriptor)
    except OSError as error:
        if isinstance(error, SafePackageIOError):
            raise
        raise SafePackageIOError(f"unsafe package node: {relative}: {error}") from error
    finally:
        os.close(directory)


def _inventory_posix(
    root: Path,
    excluded_parts: frozenset[str],
    excluded_files: frozenset[str],
) -> tuple[str, ...]:
    """Enumerate ordinary POSIX payload files without traversing links."""
    root_descriptor = _open_posix_root(root)
    discovered: list[str] = []

    def walk(directory: int, prefix: tuple[str, ...]) -> None:
        for name in sorted(os.listdir(directory)):
            relative_parts = (*prefix, name)
            if name in excluded_files or any(part in excluded_parts for part in relative_parts):
                continue
            relative = PurePosixPath(*relative_parts).as_posix()
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                discovered.append(relative)
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise SafePackageIOError(f"unsafe package node: {relative}")
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                walk(child, relative_parts)
            finally:
                os.close(child)

    try:
        walk(root_descriptor, ())
    except OSError as error:
        if isinstance(error, SafePackageIOError):
            raise
        raise SafePackageIOError(f"unsafe package inventory: {error}") from error
    finally:
        os.close(root_descriptor)
    return tuple(discovered)


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_ALL = 0x00000007
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _ByHandleFileInformation(ctypes.Structure):
        """Match Win32 BY_HANDLE_FILE_INFORMATION for stable identity checks."""

        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("link_count", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _get_information = _kernel32.GetFileInformationByHandle
    _get_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    _get_information.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL


def _windows_identity(handle: int) -> tuple[int, ...]:
    """Read stable Win32 volume, file-index, size, link, and timestamp fields."""
    information = _ByHandleFileInformation()  # type: ignore[name-defined]
    if not _get_information(handle, information):  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    return (
        information.attributes,
        information.volume_serial,
        information.file_index_high,
        information.file_index_low,
        information.size_high,
        information.size_low,
        information.link_count,
        information.write_time.dwHighDateTime,
        information.write_time.dwLowDateTime,
    )


def _windows_directory_identity(handle: int) -> tuple[int, ...]:
    """Read only the Win32 fields that change when a directory node is itself replaced.

    Excludes ``write_time``: a directory's mtime advances whenever any sibling entry is added or removed, which is
    routine concurrent filesystem activity, not evidence the directory node was swapped.
    ``file_index``/``volume_serial`` stay fixed across that churn and only change if the directory itself is replaced.
    """
    information = _ByHandleFileInformation()  # type: ignore[name-defined]
    if not _get_information(handle, information):  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    return (
        information.attributes,
        information.volume_serial,
        information.file_index_high,
        information.file_index_low,
    )


def _open_windows(path: Path, *, directory: bool) -> int:
    """Open one Win32 node itself and reject reparse points and wrong node kinds."""
    access = _FILE_READ_ATTRIBUTES if directory else _GENERIC_READ  # type: ignore[name-defined]
    flags = _FILE_FLAG_OPEN_REPARSE_POINT  # type: ignore[name-defined]
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS  # type: ignore[name-defined]
    handle = _create_file(  # type: ignore[name-defined]
        str(path),
        access,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        flags,
        None,  # type: ignore[name-defined]
    )
    if handle == _INVALID_HANDLE_VALUE:  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    try:
        identity = _windows_identity(handle)
        is_directory = bool(identity[0] & _FILE_ATTRIBUTE_DIRECTORY)  # type: ignore[name-defined]
        if not identity[0] & _REPARSE_POINT and is_directory == directory:
            return handle
    except Exception:
        _close_handle(handle)  # type: ignore[name-defined]
        raise
    _close_handle(handle)  # type: ignore[name-defined]
    raise SafePackageIOError(f"unsafe package node: {path}")


def _windows_directories(root: Path, parts: tuple[str, ...]) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    """Snapshot every Win32 parent component without following reparse points.

    Walks from the drive anchor down through ``root`` to the target file's parent. Each open rejects reparse points
    (``_open_windows``), so this is the Windows equivalent of the POSIX ``O_NOFOLLOW`` directory-descriptor chain —
    dropping any ancestor from the walk would let a junction above that point redirect the whole read undetected.
    """
    absolute = Path(os.path.abspath(root))
    current = Path(absolute.anchor)
    directories = [current]
    for part in absolute.parts[1:]:
        current /= part
        directories.append(current)
    for part in parts[:-1]:
        current /= part
        directories.append(current)
    snapshots = []
    for directory in directories:
        handle = _open_windows(directory, directory=True)
        try:
            snapshots.append((directory, _windows_directory_identity(handle)))
        finally:
            _close_handle(handle)  # type: ignore[name-defined]
    return tuple(snapshots)


def _read_windows(root: Path, relative: str, maximum: int) -> SafeFile:
    """Read one Win32 file with no-reparse handles and stable parent snapshots."""
    parts = _relative_parts(relative)
    parents = _windows_directories(root, parts)
    path = Path(os.path.abspath(root)).joinpath(*parts)
    handle = _open_windows(path, directory=False)
    owns_handle = True
    try:
        before = _windows_identity(handle)
        if before[6] != 1:
            raise SafePackageIOError(f"unsafe package node: {relative}")
        if ((before[4] << 32) | before[5]) > maximum:
            raise SafePackageIOError(f"oversized package file: {relative}")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))  # type: ignore[name-defined]
        owns_handle = False
    finally:
        if owns_handle:
            _close_handle(handle)  # type: ignore[name-defined]
    try:
        result = _read_descriptor(descriptor, maximum, relative)
        after = _windows_identity(msvcrt.get_osfhandle(descriptor))  # type: ignore[name-defined]
    finally:
        os.close(descriptor)
    if before != after:
        raise SafePackageIOError(f"package file changed during read: {relative}")
    for directory, identity in parents:
        parent_handle = _open_windows(directory, directory=True)
        try:
            if _windows_directory_identity(parent_handle) != identity:
                raise SafePackageIOError(f"package path changed during read: {relative}")
        finally:
            _close_handle(parent_handle)  # type: ignore[name-defined]
    return result


def _inventory_windows(
    root: Path,
    excluded_parts: frozenset[str],
    excluded_files: frozenset[str],
) -> tuple[str, ...]:
    """Enumerate Win32 payload files while rejecting every reparse entry."""
    absolute = Path(os.path.abspath(root))
    parents = _windows_directories(absolute, ("inventory",))
    discovered: list[str] = []

    def walk(directory: Path, prefix: tuple[str, ...]) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                relative_parts = (*prefix, entry.name)
                if entry.name in excluded_files or any(part in excluded_parts for part in relative_parts):
                    continue
                relative = PurePosixPath(*relative_parts).as_posix()
                metadata = entry.stat(follow_symlinks=False)
                if getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT or entry.is_symlink():
                    raise SafePackageIOError(f"unsafe package node: {relative}")
                if stat.S_ISREG(metadata.st_mode):
                    discovered.append(relative)
                elif stat.S_ISDIR(metadata.st_mode):
                    walk(Path(entry.path), relative_parts)
                else:
                    raise SafePackageIOError(f"unsafe package node: {relative}")

    walk(absolute, ())
    for directory, identity in parents:
        handle = _open_windows(directory, directory=True)
        try:
            if _windows_directory_identity(handle) != identity:
                raise SafePackageIOError("package path changed during inventory")
        finally:
            _close_handle(handle)  # type: ignore[name-defined]
    return tuple(discovered)


def read_safe_file(root: Path | str, relative: str, *, maximum: int) -> SafeFile:
    """Read one package-relative file through the native stable-handle backend."""
    package_root = Path(root)
    if maximum < 0:
        raise ValueError("maximum must be nonnegative")
    if os.name == "nt":
        return _read_windows(package_root, relative, maximum)
    return _read_posix(package_root, relative, maximum)


def inventory_package_files(
    root: Path | str,
    *,
    excluded_parts: frozenset[str],
    excluded_files: frozenset[str],
) -> tuple[str, ...]:
    """Return the sorted ordinary package payload paths from a stable native scan."""
    package_root = Path(root)
    if os.name == "nt":
        return _inventory_windows(package_root, excluded_parts, excluded_files)
    return _inventory_posix(package_root, excluded_parts, excluded_files)
