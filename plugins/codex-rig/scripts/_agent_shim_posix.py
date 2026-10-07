"""Provide fail-closed POSIX filesystem primitives for shim transactions.

## Purpose

Confine shim writes to identity-checked descriptors with private modes, bounded content, and recoverable cleanup
behavior. These primitives make filesystem mutation explicit and let the transaction layer detect replacement races at
descriptor boundaries.

## Scope

Implements low-level POSIX operations only; planning, approval, lifecycle classification, and transaction sequencing
remain separate. It does not choose targets or interpret journal intent, which keeps platform safety checks reusable by
the transaction flow.

## Usage

Import descriptor-based primitives from the transaction layer; this is intentionally not a user-facing command. Callers
must pass already validated names and roots and must preserve returned descriptors until the associated operation is
verified.

## Used by

``_agent_shim_transaction.py`` and POSIX-specific shim safety tests call these primitives. The transaction module
combines their operations with journal checkpoints and rollback decisions.

## Outputs

Returns checked descriptors, identities, and created-path records that let the transaction detect replacement or cleanup
races. Results describe what was opened or created, allowing callers to close, verify, quarantine, or remove objects
without following a newly substituted path.

## Failure

Link traversal, unsafe names, non-private modes, oversized content, lock contention, or inode changes raise typed POSIX
primitive errors. The transaction layer must convert these failures into rollback or recovery evidence rather than
retrying an unchecked write.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
import sys
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from typing import NoReturn

#: Largest file, in bytes (4 MiB), that these POSIX helpers will read or write.
MAX_FILE_BYTES = 4_194_304
#: Permission mode (owner-only) required for and applied to private managed directories.
PRIVATE_DIRECTORY_MODE = 0o700
#: Permission mode (owner read/write only) required for and applied to private managed files.
PRIVATE_FILE_MODE = 0o600
#: Pattern for a lowercase 64-character hexadecimal SHA-256 digest string.
DIGEST = re.compile(r"[0-9a-f]{64}")


class PosixPrimitiveError(OSError):
    """Signal a primitive that could not preserve lifecycle invariants."""


class LockBusyError(PosixPrimitiveError):
    """Signal that another process currently owns the coordination lock."""


class DetachedMismatchError(PosixPrimitiveError):
    """Signal a detached target whose bytes differed from approved evidence."""

    def __init__(self, message: str, *, restored: bool) -> None:
        super().__init__(message)
        self.restored = restored


@dataclass(frozen=True)
class FileIdentity:
    """Bind one opened regular file to exact descriptor metadata and content."""

    device: int
    inode: int
    owner: int
    group: int
    mode: str
    link_count: int
    size: int
    sha256: str


@dataclass(frozen=True)
class DirectoryIdentity:
    """Bind one opened private directory to exact descriptor metadata."""

    device: int
    inode: int
    owner: int
    group: int
    mode: str


def _fail(message: str) -> NoReturn:
    """Raise the primitive layer's fail-closed error."""
    raise PosixPrimitiveError(message)


def _basename(value: object, label: str) -> str:
    """Require one bounded UTF-8 basename without control characters."""
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise PosixPrimitiveError(f"invalid {label}") from error
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or "/" in value
        or len(encoded) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(f"invalid {label}")
    return value


def _digest(value: object, label: str) -> str:
    """Require one exact lowercase SHA-256 digest."""
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _mode(value: object, label: str) -> int:
    """Require one exact POSIX permission mode integer."""
    if type(value) is not int or not 0 <= value <= 0o7777:
        _fail(f"invalid {label}")
    return value


def _directory_identity(fd: int, *, private: bool, protected: bool = False) -> DirectoryIdentity:
    """Validate and identify one held directory descriptor."""
    metadata = os.fstat(fd)
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("unsafe directory descriptor type")
    if metadata.st_uid != os.geteuid():
        _fail(f"unsafe directory descriptor owner: expected uid {os.geteuid()}, observed {metadata.st_uid}")
    if private and mode != PRIVATE_DIRECTORY_MODE:
        _fail(f"unsafe private directory mode: expected 0700, observed {mode:04o}")
    if protected and mode & 0o7022 != 0:
        _fail(f"unsafe protected directory mode: expected no group/world write or special bits, observed {mode:04o}")
    return DirectoryIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        f"{mode:04o}",
    )


def directory_identity(fd: int, *, private: bool = True) -> DirectoryIdentity:
    """Return the validated identity of one held directory descriptor."""
    return _directory_identity(fd, private=private, protected=not private)


def open_directory_at(parent_fd: int, name: str, *, private: bool = True) -> int:
    """Open one existing child directory without following a link."""
    child = _basename(name, "directory name")
    try:
        fd = os.open(
            child,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        _directory_identity(fd, private=private, protected=not private)
        return fd
    except OSError as error:
        if "fd" in locals():
            os.close(fd)
        detail = error.strerror or str(error)
        raise PosixPrimitiveError(f"unsafe directory {child}: {detail}") from error


def create_directory_at(parent_fd: int, name: str, *, private: bool = True) -> tuple[int, bool]:
    """Create a private child or open one with the requested protection."""
    child = _basename(name, "directory name")
    _directory_identity(parent_fd, private=False, protected=True)
    created = False
    try:
        os.mkdir(child, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
        created = True
        # Linux lacks descriptor-relative no-follow chmod in Python 3.10.
        # The name is newly created under a held current-user private parent;
        # open it no-follow immediately after establishing the exact mode.
        os.chmod(child, PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = open_directory_at(parent_fd, child, private=private)
    try:
        if created:
            os.fsync(fd)
            os.fsync(parent_fd)
    except BaseException:
        os.close(fd)
        raise
    return fd, created


def create_private_path(parent_fd: int, parts: tuple[str, ...]) -> tuple[int, tuple[str, ...]]:
    """Create a fixed relative private directory path one component at a time."""
    if not isinstance(parts, tuple) or not parts:
        _fail("nonempty directory path required")
    current_fd = os.dup(parent_fd)
    created: list[str] = []
    try:
        _directory_identity(current_fd, private=False, protected=True)
        for part in parts:
            next_fd, was_created = create_directory_at(current_fd, part)
            os.close(current_fd)
            current_fd = next_fd
            if was_created:
                created.append(part)
        return current_fd, tuple(created)
    except BaseException:
        os.close(current_fd)
        raise


def _read_fd(fd: int, maximum: int) -> tuple[bytes, os.stat_result]:
    """Read bounded bytes from one stable opened regular descriptor."""
    before = os.fstat(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink < 1
        or before.st_size < 0
        or before.st_size > maximum
    ):
        _fail("unsafe regular file")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(65_536, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            _fail("oversized regular file")
    after = os.fstat(fd)
    stable = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in stable):
        _fail("regular file changed during read")
    return b"".join(chunks), after


def read_regular_at(
    parent_fd: int,
    name: str,
    *,
    maximum: int = MAX_FILE_BYTES,
    expected_mode: int | None = None,
    expected_links: int | None = 1,
) -> tuple[bytes, FileIdentity]:
    """Read and identify one contained regular file without following links."""
    child = _basename(name, "file name")
    if type(maximum) is not int or maximum < 0:
        _fail("invalid file size bound")
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(child, flags, dir_fd=parent_fd)
    except OSError as error:
        raise PosixPrimitiveError(f"unsafe file {child}: {error.strerror}") from error
    try:
        payload, metadata = _read_fd(fd, maximum)
    finally:
        os.close(fd)
    mode = stat.S_IMODE(metadata.st_mode)
    if expected_mode is not None and mode != expected_mode:
        _fail(f"unexpected mode for {child}")
    if expected_links is not None and metadata.st_nlink != expected_links:
        _fail(f"unexpected link count for {child}")
    return payload, FileIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        f"{mode:04o}",
        metadata.st_nlink,
        metadata.st_size,
        hashlib.sha256(payload).hexdigest(),
    )


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte or fail without accepting a short write."""
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            _fail("short filesystem write")
        view = view[written:]


def _cleanup_created_file(parent_fd: int, name: str, identity: os.stat_result) -> None:
    """Remove only the exact file created by a failed pre-journal write."""
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(linked.st_mode)
        or linked.st_uid != os.geteuid()
        or linked.st_nlink != 1
        or (linked.st_dev, linked.st_ino) != (identity.st_dev, identity.st_ino)
    ):
        _fail("failed write artifact cleanup is uncertain")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def write_exclusive_at(parent_fd: int, name: str, payload: bytes) -> FileIdentity:
    """Create one durable file after recovery authority names its parent scope."""
    child = _basename(name, "file name")
    if not isinstance(payload, bytes) or len(payload) > MAX_FILE_BYTES:
        _fail("invalid file payload")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(child, flags, PRIVATE_FILE_MODE, dir_fd=parent_fd)
    except OSError as error:
        raise PosixPrimitiveError(f"exclusive create failed for {child}: {error.strerror}") from error
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        _write_all(fd, payload)
        os.fsync(fd)
    except BaseException:
        identity = os.fstat(fd)
        os.close(fd)
        try:
            _cleanup_created_file(parent_fd, child, identity)
        except OSError as cleanup_error:
            raise PosixPrimitiveError("failed write artifact cleanup is uncertain") from cleanup_error
        raise
    os.close(fd)
    os.fsync(parent_fd)
    observed, identity = read_regular_at(parent_fd, child, expected_mode=PRIVATE_FILE_MODE)
    if observed != payload:
        _fail(f"durable write verification failed for {child}")
    return identity


def _unlink_same_inode(parent_fd: int, name: str, identity: FileIdentity) -> None:
    """Unlink one exact held-name identity and fsync its parent."""
    child = _basename(name, "file name")
    linked = os.stat(child, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(linked.st_mode)
        or linked.st_uid != os.geteuid()
        or (linked.st_dev, linked.st_ino) != (identity.device, identity.inode)
    ):
        _fail("unlink identity changed")
    os.unlink(child, dir_fd=parent_fd)
    os.fsync(parent_fd)


def unlink_verified_at(
    parent_fd: int,
    name: str,
    *,
    expected_hash: str,
    expected_mode: int,
    expected_links: int = 1,
    allow_absent: bool = False,
    parent_private: bool = True,
) -> bool:
    """Unlink one exact current-user regular file and durably record absence."""
    if (
        type(allow_absent) is not bool
        or type(parent_private) is not bool
        or type(expected_links) is not int
        or expected_links < 1
    ):
        _fail("invalid absence permission")
    digest = _digest(expected_hash, "unlink hash")
    mode = _mode(expected_mode, "unlink mode")
    _directory_identity(parent_fd, private=parent_private, protected=not parent_private)
    try:
        _, identity = read_regular_at(
            parent_fd,
            name,
            expected_mode=mode,
            expected_links=expected_links,
        )
    except PosixPrimitiveError as error:
        if allow_absent and isinstance(error.__cause__, FileNotFoundError):
            return False
        raise
    if identity.sha256 != digest:
        _fail("unlink hash mismatch")
    _unlink_same_inode(parent_fd, name, identity)
    return True


def write_initial_journal(transaction_fd: int, payload: bytes) -> FileIdentity:
    """Publish the first journal through a recoverable same-inode initial link."""
    initial = write_exclusive_at(transaction_fd, "journal.initial.json", payload)
    published = publish_noclobber(
        transaction_fd,
        "journal.initial.json",
        transaction_fd,
        "journal.json",
        expected_hash=initial.sha256,
    )
    if (initial.device, initial.inode) != (published.device, published.inode):
        _fail("initial journal publication identity mismatch")
    _unlink_same_inode(transaction_fd, "journal.initial.json", initial)
    observed, journal = read_regular_at(
        transaction_fd,
        "journal.json",
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=1,
    )
    if observed != payload:
        _fail("initial journal verification failed")
    return journal


def publish_noclobber(
    source_fd: int,
    source_name: str,
    target_fd: int,
    target_name: str,
    *,
    expected_hash: str,
) -> FileIdentity:
    """Hard-link an exact staged file into an absent target name."""
    source = _basename(source_name, "source name")
    target = _basename(target_name, "target name")
    _directory_identity(source_fd, private=True)
    _directory_identity(target_fd, private=False, protected=True)
    payload, identity = read_regular_at(source_fd, source, expected_mode=PRIVATE_FILE_MODE)
    if identity.sha256 != expected_hash:
        _fail("staged publication hash mismatch")
    try:
        os.link(source, target, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)
    except OSError as error:
        raise PosixPrimitiveError(f"no-clobber publication failed for {target}: {error.strerror}") from error
    os.fsync(target_fd)
    target_payload, target_identity = read_regular_at(
        target_fd,
        target,
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=2,
    )
    if target_payload != payload or (target_identity.device, target_identity.inode) != (
        identity.device,
        identity.inode,
    ):
        _fail("published target identity mismatch")
    return target_identity


def detach_verified(
    target_fd: int,
    target_name: str,
    quarantine_fd: int,
    quarantine_name: str,
    *,
    expected_hash: str,
) -> FileIdentity:
    """Atomically detach a target and restore mismatched bytes without clobber."""
    target = _basename(target_name, "target name")
    quarantine = _basename(quarantine_name, "quarantine name")
    target_root = _directory_identity(target_fd, private=False, protected=True)
    quarantine_root = _directory_identity(quarantine_fd, private=True)
    if target_root.device != quarantine_root.device:
        _fail("detach roots must share one device")
    # Refuse an already-invalid target without moving it. A later swap is
    # detected from the detached bytes and restored from that observation.
    _, approved = read_regular_at(target_fd, target, expected_mode=PRIVATE_FILE_MODE)
    if approved.sha256 != expected_hash:
        _fail("target hash changed before detach")
    try:
        os.stat(quarantine, dir_fd=quarantine_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        _fail("quarantine destination already exists")
    _rename_noreplace(target_fd, target, quarantine_fd, quarantine)
    os.fsync(target_fd)
    if quarantine_fd != target_fd:
        os.fsync(quarantine_fd)
    _, identity = read_regular_at(quarantine_fd, quarantine, expected_mode=PRIVATE_FILE_MODE)
    if identity.sha256 == expected_hash:
        return identity
    restored = False
    try:
        os.link(quarantine, target, src_dir_fd=quarantine_fd, dst_dir_fd=target_fd, follow_symlinks=False)
        os.fsync(target_fd)
        _, target_identity = read_regular_at(
            target_fd,
            target,
            expected_mode=PRIVATE_FILE_MODE,
            expected_links=None,
        )
        restored = (target_identity.device, target_identity.inode) == (identity.device, identity.inode)
    except OSError:
        restored = False
    raise DetachedMismatchError("detached target hash mismatch", restored=restored)


def _rename_noreplace(source_fd: int, source: str, target_fd: int, target: str) -> None:
    """Rename one basename atomically while refusing an occupied destination."""
    library = CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        flags = 1
    else:
        function = None
        flags = 0
    if function is None:
        _fail("atomic no-replace rename is unavailable")
    function.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
    function.restype = c_int
    result = function(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flags)
    if result != 0:
        error_number = get_errno()
        error = OSError(error_number, os.strerror(error_number))
        raise PosixPrimitiveError(f"no-replace detach failed for {source}: {error.strerror}") from error


def restore_quarantine_at(
    quarantine_fd: int,
    quarantine_name: str,
    target_fd: int,
    target_name: str,
    *,
    expected_hash: str,
) -> FileIdentity:
    """Move one exact quarantine file back to an absent target without clobber."""
    source = _basename(quarantine_name, "quarantine name")
    target = _basename(target_name, "target name")
    digest = _digest(expected_hash, "quarantine hash")
    quarantine_root = _directory_identity(quarantine_fd, private=True)
    target_root = _directory_identity(target_fd, private=False, protected=True)
    if quarantine_root.device != target_root.device:
        _fail("restore roots must share one device")
    _, before = read_regular_at(
        quarantine_fd,
        source,
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=1,
    )
    if before.sha256 != digest:
        _fail("quarantine hash mismatch")
    _rename_noreplace(quarantine_fd, source, target_fd, target)
    os.fsync(quarantine_fd)
    if target_fd != quarantine_fd:
        os.fsync(target_fd)
    _, restored = read_regular_at(
        target_fd,
        target,
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=1,
    )
    if restored.sha256 != digest or (restored.device, restored.inode) != (before.device, before.inode):
        _fail("quarantine restore identity mismatch")
    return restored


def replace_owned_at(
    parent_fd: int,
    destination: str,
    payload: bytes,
    *,
    temporary: str,
    expected_hash: str | None,
) -> FileIdentity:
    """Atomically replace only an absent or exact manager-owned regular file."""
    name = _basename(destination, "destination name")
    next_name = _basename(temporary, "temporary name")
    if (name, next_name) not in {
        ("journal.json", "journal.next.json"),
        ("probe.json", "probe.next.json"),
    }:
        _fail("unsupported owned replacement pair")
    try:
        _, current = read_regular_at(parent_fd, name, expected_mode=PRIVATE_FILE_MODE)
    except PosixPrimitiveError as error:
        if not isinstance(error.__cause__, FileNotFoundError):
            raise
        current = None
    if expected_hash is None:
        if current is not None:
            _fail("unexpected owned destination")
    elif current is None or current.sha256 != expected_hash:
        _fail("owned destination hash mismatch")
    write_exclusive_at(parent_fd, next_name, payload)
    try:
        os.replace(next_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        raise
    observed, identity = read_regular_at(parent_fd, name, expected_mode=PRIVATE_FILE_MODE)
    if observed != payload:
        _fail("replacement verification failed")
    return identity


def publish_state_from_transaction(
    transaction_fd: int,
    state_fd: int,
    *,
    expected_after_hash: str,
    expected_before_hash: str | None,
) -> FileIdentity:
    """Consume a journal-bound after-state link into manager-owned state.json."""
    transaction = _directory_identity(transaction_fd, private=True)
    state_root = _directory_identity(state_fd, private=True)
    if transaction.device != state_root.device:
        _fail("state publication roots must share one device")
    after_payload, after = read_regular_at(
        transaction_fd,
        "state.after.json",
        expected_mode=PRIVATE_FILE_MODE,
    )
    if after.sha256 != expected_after_hash:
        _fail("after-state artifact hash mismatch")
    try:
        _, before = read_regular_at(state_fd, "state.json", expected_mode=PRIVATE_FILE_MODE)
    except PosixPrimitiveError as error:
        if not isinstance(error.__cause__, FileNotFoundError):
            raise
        before = None
    if expected_before_hash is None:
        if before is not None:
            _fail("unexpected current state")
    elif before is None or before.sha256 != expected_before_hash:
        _fail("current state hash mismatch")
    published = publish_noclobber(
        transaction_fd,
        "state.after.json",
        transaction_fd,
        "state.publish.json",
        expected_hash=expected_after_hash,
    )
    if (published.device, published.inode) != (after.device, after.inode):
        _fail("state publish link identity mismatch")
    try:
        os.replace("state.publish.json", "state.json", src_dir_fd=transaction_fd, dst_dir_fd=state_fd)
        os.fsync(state_fd)
    except OSError as error:
        raise PosixPrimitiveError(f"state publication failed: {error.strerror}") from error
    observed, state = read_regular_at(
        state_fd,
        "state.json",
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=2,
    )
    if observed != after_payload or (state.device, state.inode) != (after.device, after.inode):
        _fail("published state identity mismatch")
    return state


def restore_state_from_transaction(
    transaction_fd: int,
    state_fd: int,
    *,
    before_exists: bool,
    expected_before_hash: str | None,
    expected_current_hash: str,
    expected_current_links: int = 1,
    allow_current_absent: bool = False,
) -> FileIdentity | None:
    """Restore journal-bound prior state or its approved prior absence."""
    if (
        type(before_exists) is not bool
        or type(allow_current_absent) is not bool
        or type(expected_current_links) is not int
        or expected_current_links < 1
    ):
        _fail("invalid state restore presence")
    current_hash = _digest(expected_current_hash, "current state hash")
    transaction = _directory_identity(transaction_fd, private=True)
    state_root = _directory_identity(state_fd, private=True)
    if transaction.device != state_root.device:
        _fail("state restore roots must share one device")
    _, after = read_regular_at(
        transaction_fd,
        "state.after.json",
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=None,
    )
    if after.sha256 != current_hash:
        _fail("after state hash mismatch")
    if not before_exists:
        if expected_before_hash is not None:
            _fail("unexpected absent before-state hash")
        unlink_verified_at(
            state_fd,
            "state.json",
            expected_hash=current_hash,
            expected_mode=PRIVATE_FILE_MODE,
            expected_links=expected_current_links,
            allow_absent=allow_current_absent,
        )
        return None
    before_hash = _digest(expected_before_hash, "before state hash")
    _, before = read_regular_at(
        transaction_fd,
        "state.before.json",
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=None,
    )
    if before.sha256 != before_hash:
        _fail("before state hash mismatch")
    try:
        _, current = read_regular_at(
            state_fd,
            "state.json",
            expected_mode=PRIVATE_FILE_MODE,
            expected_links=expected_current_links,
        )
    except PosixPrimitiveError as error:
        if not allow_current_absent or not isinstance(error.__cause__, FileNotFoundError):
            raise
        current = None
    if current is not None:
        if current.sha256 != current_hash:
            _fail("current state hash mismatch")
        unlink_verified_at(
            state_fd,
            "state.json",
            expected_hash=current_hash,
            expected_mode=PRIVATE_FILE_MODE,
            expected_links=expected_current_links,
        )
    restored = publish_noclobber(
        transaction_fd,
        "state.before.json",
        state_fd,
        "state.json",
        expected_hash=before_hash,
    )
    return restored


def remove_transaction_entries_at(
    parent_fd: int,
    entries: tuple[tuple[str, str | None, int | None], ...],
    *,
    allow_absent: bool = False,
) -> tuple[str, ...]:
    """Remove named exact artifacts or empty private children without recursion."""
    if not isinstance(entries, tuple) or type(allow_absent) is not bool:
        _fail("invalid transaction cleanup entries")
    _directory_identity(parent_fd, private=True)
    removed: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 3:
            _fail("invalid transaction cleanup entry")
        name = _basename(entry[0], "transaction cleanup name")
        expected_hash = entry[1]
        expected_links = entry[2]
        if name in seen:
            _fail("duplicate transaction cleanup entry")
        seen.add(name)
        if expected_hash is not None:
            if type(expected_links) is not int or expected_links < 1:
                _fail("invalid transaction artifact link count")
            if unlink_verified_at(
                parent_fd,
                name,
                expected_hash=_digest(expected_hash, "transaction artifact hash"),
                expected_mode=PRIVATE_FILE_MODE,
                expected_links=expected_links,
                allow_absent=allow_absent,
            ):
                removed.append(name)
            continue
        if expected_links is not None:
            _fail("unexpected transaction directory link count")
        try:
            child_fd = open_directory_at(parent_fd, name)
        except PosixPrimitiveError as error:
            if allow_absent and isinstance(error.__cause__, FileNotFoundError):
                continue
            raise
        try:
            child = directory_identity(child_fd)
            with os.scandir(child_fd) as iterator:
                if next(iterator, None) is not None:
                    _fail(f"transaction child {name} is not empty")
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or linked.st_uid != os.geteuid()
                or stat.S_IMODE(linked.st_mode) != PRIVATE_DIRECTORY_MODE
                or (linked.st_dev, linked.st_ino) != (child.device, child.inode)
            ):
                _fail("transaction child identity changed")
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            removed.append(name)
        finally:
            os.close(child_fd)
    return tuple(removed)


def acquire_coordination_lock(
    home_fd: int,
    *,
    intent: str,
    expected_identity: tuple[int, int] | None,
) -> int:
    """Open or exclusively create the fixed lock and acquire it nonblocking."""
    name = ".codex-rig-shims.lock"
    if intent not in {"open-existing", "create-if-absent"}:
        _fail("invalid coordination lock intent")
    if (intent == "create-if-absent") != (expected_identity is None):
        _fail("coordination lock approval identity mismatch")
    if expected_identity is not None and (
        not isinstance(expected_identity, tuple)
        or len(expected_identity) != 2
        or any(type(value) is not int or value < 0 for value in expected_identity)
    ):
        _fail("invalid approved coordination lock identity")
    _directory_identity(home_fd, private=False, protected=True)
    flags = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    if intent == "create-if-absent":
        flags |= os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(name, flags, PRIVATE_FILE_MODE, dir_fd=home_fd)
        if intent == "create-if-absent":
            os.fchmod(fd, PRIVATE_FILE_MODE)
            os.fsync(fd)
            os.fsync(home_fd)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            _fail("unsafe coordination lock")
        if expected_identity is not None and (metadata.st_dev, metadata.st_ino) != expected_identity:
            _fail("approved coordination lock changed")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        linked = os.stat(name, dir_fd=home_fd, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != (metadata.st_dev, metadata.st_ino):
            _fail("coordination lock path changed")
        return fd
    except BlockingIOError as error:
        if "fd" in locals():
            os.close(fd)
        raise LockBusyError(errno.EWOULDBLOCK, "coordination lock busy") from error
    except PosixPrimitiveError:
        if "fd" in locals():
            os.close(fd)
        raise
    except OSError as error:
        if "fd" in locals():
            os.close(fd)
        raise PosixPrimitiveError(f"coordination lock failed: {error.strerror}") from error
