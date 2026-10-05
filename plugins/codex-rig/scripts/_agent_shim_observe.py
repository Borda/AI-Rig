"""Observe bounded filesystem evidence for the Codex Rig shim lifecycle.

## Purpose

Collect identity-checked state needed to diagnose shims without trusting symlinks, unbounded files, or mutable path
traversal. Descriptor-relative reads let later planning compare the objects that were actually observed with the objects
used during mutation.

## Scope

Opens and reads local filesystem objects only; it never changes a target, lock, journal, or managed instruction file. It
owns the bridge from filesystem descriptors to immutable observations, while lifecycle parsing and operation planning
remain separate.

## Usage

Import ``observe_filesystem``-style helpers through the manager rather than running this internal module directly.
Supply canonical roots and package expectations from the manager so every observation is constrained to the intended
installation.

## Used by

``manage_role_agents.py`` and the approval/transaction safety chain consume these observation helpers. Planning and
approval use their records as the evidence boundary before any write is authorized.

## Outputs

Returns immutable observations for roots, locks, namespaces, targets, state, and journals with their verified
identities. Each record carries enough metadata for callers to detect replacement, link traversal, stale state, and
recovery residue.

## Failure

Unsafe paths, links, oversized reads, descriptor mismatches, or unreadable local state raise ``ObservationError`` before
planning can begin. The manager must report the observation failure and withhold mutation rather than downgrade it to a
warning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from _agent_shim_journal import Journal, JournalDataError, parse_journal, validate_successor
from _agent_shim_lifecycle import (
    STATE_BYTES,
    LifecycleDataError,
    RecoveryObservation,
    TargetObservation,
    classify_recovery,
    classify_targets,
    parse_marker,
    parse_state,
)
from generate_roles import ROLE_IDS

SHIM_BYTES = 262_144
JOURNAL_BYTES = 4_194_304
PATH_BYTES = 4_096
MAX_DIRECTORY_ENTRIES = 256
MAX_TARGET_DIRECTORY_ENTRIES = 4096
MARKER_PREFIX = b"# codex-rig-shim "
TARGET_NAMESPACE_PREFIX = "codex-rig-"
TARGET_NAMESPACE_SUFFIX = ".toml"
STRICT_TARGET_NAME = re.compile(r"codex-rig-[a-z][a-z0-9-]{0,63}\.toml")


class ObservationError(ValueError):
    """Signal unsafe or unbounded filesystem evidence."""


@dataclass(frozen=True)
class RootIdentity:
    """Bind an opened directory to its canonical path and metadata."""

    canonical_path: str
    device: int
    inode: int
    owner: int
    group: int
    mode: str


@dataclass(frozen=True)
class RootObservation:
    """Bind an intended root to its exact existing node or nearest ancestor."""

    exists: bool
    canonical_path: str
    nearest_existing_ancestor: RootIdentity
    missing_suffix_components: tuple[str, ...]
    identity: RootIdentity | None


@dataclass(frozen=True)
class LockObservation:
    """Bind the fixed coordination lock name to exact absence or file identity."""

    kind: str
    canonical_path: str
    device: int | None
    inode: int | None
    owner: int | None
    group: int | None
    mode: str | None
    link_count: int | None
    size: int | None

    @property
    def intent(self) -> str | None:
        """Return the only lock action compatible with this observation."""
        if self.kind == "absent":
            return "create-if-absent"
        if self.kind == "regular":
            return "open-existing"
        return None


@dataclass(frozen=True)
class NamespaceCandidateObservation:
    """Report unmanaged namespace evidence without claiming file ownership."""

    name: str
    kind: str


@dataclass(frozen=True)
class FilesystemObservation:
    """Return one immutable read-only lifecycle observation."""

    classification: str
    reason: str
    state: str
    targets: str
    recovery: str
    codex_home_observation: RootObservation | None
    plugin_root_identity: RootIdentity | None
    target_root_observation: RootObservation | None
    state_root_observation: RootObservation | None
    coordination_lock_observation: LockObservation | None
    state_payload: bytes | None
    target_observations: tuple[tuple[str, TargetObservation], ...]
    namespace_candidates: tuple[NamespaceCandidateObservation, ...] = ()
    namespace_inventory_status: str = "unavailable"

    @property
    def codex_home_identity(self) -> RootIdentity | None:
        """Return the existing Codex-home identity for compatibility."""
        return self.codex_home_observation.identity if self.codex_home_observation is not None else None

    @property
    def target_root_identity(self) -> RootIdentity | None:
        """Return the existing target-root identity, if present."""
        return self.target_root_observation.identity if self.target_root_observation is not None else None

    @property
    def state_root_identity(self) -> RootIdentity | None:
        """Return the existing state-root identity, if present."""
        return self.state_root_observation.identity if self.state_root_observation is not None else None


def _absolute_path(path: Path | str, label: str) -> Path:
    """Require one explicit bounded canonical absolute path."""
    value = os.fspath(path)
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an absolute canonical path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be an absolute canonical path") from error
    if (
        not value.startswith("/")
        or value.startswith("//")
        or os.path.normpath(value) != value
        or len(encoded) > PATH_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be an absolute canonical path")
    return Path(value)


def _open_absolute_directory(path: Path, label: str) -> int:
    """Open every absolute directory component without following links."""
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except OSError as error:
        os.close(directory_fd)
        raise ObservationError(f"unsafe {label}: {error.strerror}") from error


def _identity(
    path: Path,
    directory_fd: int,
    *,
    owned: bool = False,
    private: bool = False,
    protected: bool = False,
) -> RootIdentity:
    """Capture identity from one held directory descriptor."""
    metadata = os.fstat(directory_fd)
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ObservationError(f"unsafe directory type: {path}; expected directory")
    if owned and metadata.st_uid != os.geteuid():
        raise ObservationError(
            f"unsafe directory owner: {path}; expected uid {os.geteuid()}, observed {metadata.st_uid}"
        )
    if private and mode != 0o700:
        raise ObservationError(f"unsafe private directory mode: {path}; expected 0700, observed {mode:04o}")
    if protected and mode & 0o7022 != 0:
        raise ObservationError(
            f"unsafe protected directory mode: {path}; "
            f"expected no group/world write or special bits, observed {mode:04o}"
        )
    return RootIdentity(
        canonical_path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner=metadata.st_uid,
        group=metadata.st_gid,
        mode=f"{mode:04o}",
    )


def _existing_root_observation(path: Path, identity: RootIdentity) -> RootObservation:
    """Build the exact observation for one held existing root."""
    return RootObservation(True, str(path), identity, (), identity)


def _observe_relative_root(
    parent_fd: int,
    parent_path: Path,
    parts: tuple[str, ...],
    label: str,
    *,
    private: bool = True,
) -> tuple[int | None, RootObservation]:
    """Open a contained root or bind its deepest held existing ancestor."""
    directory_fd = os.dup(parent_fd)
    current_path = parent_path
    try:
        ancestor = _identity(current_path, directory_fd, owned=True)
        for index, part in enumerate(parts):
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                intended = parent_path.joinpath(*parts)
                os.close(directory_fd)
                return None, RootObservation(False, str(intended), ancestor, parts[index:], None)
            except OSError as error:
                raise ObservationError(f"unsafe {label}: {error.strerror}") from error
            os.close(directory_fd)
            directory_fd = next_fd
            current_path /= part
            ancestor = _identity(current_path, directory_fd, owned=True)
        identity = _identity(
            current_path,
            directory_fd,
            owned=True,
            private=private,
            protected=not private,
        )
        return directory_fd, _existing_root_observation(current_path, identity)
    except Exception:
        try:
            os.close(directory_fd)
        except OSError:
            pass
        raise


def _observe_lock(home_fd: int, home: Path) -> LockObservation:
    """Observe the fixed coordination file without creating or reading it."""
    name = ".codex-rig-shims.lock"
    canonical_path = str(home / name)
    try:
        observed = os.stat(name, dir_fd=home_fd, follow_symlinks=False)
    except FileNotFoundError:
        return LockObservation("absent", canonical_path, None, None, None, None, None, None, None)
    except OSError as error:
        raise ObservationError(f"unsafe coordination lock: {error.strerror}") from error
    preflight_safe = (
        stat.S_ISREG(observed.st_mode)
        and observed.st_uid == os.geteuid()
        and stat.S_IMODE(observed.st_mode) == 0o600
        and observed.st_nlink == 1
        and observed.st_size == 0
    )
    if not preflight_safe:
        return LockObservation(
            "unsafe",
            canonical_path,
            observed.st_dev,
            observed.st_ino,
            observed.st_uid,
            observed.st_gid,
            f"{stat.S_IMODE(observed.st_mode):04o}",
            observed.st_nlink,
            observed.st_size,
        )
    try:
        lock_fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=home_fd,
        )
    except OSError:
        return LockObservation("unsafe", canonical_path, None, None, None, None, None, None, None)
    try:
        opened = os.fstat(lock_fd)
        stable = (observed.st_dev, observed.st_ino) == (opened.st_dev, opened.st_ino)
        safe = (
            stable
            and stat.S_ISREG(opened.st_mode)
            and opened.st_uid == os.geteuid()
            and stat.S_IMODE(opened.st_mode) == 0o600
            and opened.st_nlink == 1
            and opened.st_size == 0
        )
        return LockObservation(
            "regular" if safe else "unsafe",
            canonical_path,
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            opened.st_gid,
            f"{stat.S_IMODE(opened.st_mode):04o}",
            opened.st_nlink,
            opened.st_size,
        )
    finally:
        os.close(lock_fd)


def _entries(directory_fd: int, label: str) -> tuple[str, ...]:
    """List a bounded directory through its held descriptor."""
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                try:
                    encoded = entry.name.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise ObservationError(f"invalid UTF-8 name in {label}") from error
                if (
                    len(encoded) > PATH_BYTES
                    or entry.name in {".", ".."}
                    or any(ord(character) < 32 or ord(character) == 127 for character in entry.name)
                ):
                    raise ObservationError(f"unsafe name in {label}")
                names.append(entry.name)
                if len(names) > MAX_DIRECTORY_ENTRIES:
                    raise ObservationError(f"too many entries in {label}")
    except OSError as error:
        raise ObservationError(f"cannot list {label}: {error.strerror}") from error
    return tuple(sorted(names))


def _target_entries(directory_fd: int | None) -> tuple[tuple[str, ...], str]:
    """List the target root within a fixed bound and retain scan failures."""
    if directory_fd is None:
        return (), "complete"
    names: list[str] = []
    malformed = False
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                names.append(entry.name)
                if len(names) > MAX_TARGET_DIRECTORY_ENTRIES:
                    return (), "overflow"
                try:
                    encoded = entry.name.encode("utf-8")
                except UnicodeEncodeError:
                    malformed = True
                    continue
                if (
                    len(encoded) > PATH_BYTES
                    or entry.name in {".", ".."}
                    or any(ord(character) < 32 or ord(character) == 127 for character in entry.name)
                ):
                    malformed = True
    except OSError:
        return (), "unreadable"
    return tuple(sorted(names)), "malformed" if malformed else "complete"


def _read_regular(
    directory_fd: int,
    name: str,
    maximum: int,
    label: str,
    *,
    required_mode: int | None,
    required_links: int = 1,
) -> bytes | None:
    """Read one bounded regular file and reject descriptor races."""
    try:
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ObservationError(f"unsafe {label}: {error.strerror}") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != required_links
        or observed.st_uid != os.geteuid()
        or observed.st_size > maximum
        or (required_mode is not None and stat.S_IMODE(observed.st_mode) != required_mode)
    ):
        raise ObservationError(f"unsafe {label}")
    try:
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise ObservationError(f"unsafe {label}: {error.strerror}") from error
    try:
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != required_links
            or opened.st_uid != os.geteuid()
            or opened.st_size > maximum
            or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino)
            or (required_mode is not None and stat.S_IMODE(opened.st_mode) != required_mode)
        ):
            raise ObservationError(f"changed or unsafe {label}")
        try:
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(file_fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            final = os.fstat(file_fd)
        except OSError as error:
            raise ObservationError(f"changed or unreadable {label}: {error.strerror}") from error
        stable_fields = (
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
        if (
            len(payload) > maximum
            or len(payload) != opened.st_size
            or any(getattr(opened, field) != getattr(final, field) for field in stable_fields)
            or (current.st_dev, current.st_ino) != (final.st_dev, final.st_ino)
        ):
            raise ObservationError(f"changed or oversized {label}")
        return payload
    finally:
        os.close(file_fd)


def _observe_target(directory_fd: int | None, name: str) -> TargetObservation:
    """Read one allowlisted target as absent, regular, or unsafe."""
    if directory_fd is None:
        return TargetObservation("absent")
    try:
        payload = _read_regular(directory_fd, name, SHIM_BYTES, name, required_mode=0o600)
    except ObservationError:
        return TargetObservation("unsafe")
    if payload is None:
        return TargetObservation("absent")
    marker = None
    first_line, separator, _ = payload.partition(b"\n")
    if separator and payload.count(MARKER_PREFIX) == 1:
        try:
            marker = parse_marker(first_line)
        except LifecycleDataError:
            pass
    return TargetObservation("regular", hashlib.sha256(payload).hexdigest(), marker)


def _target_observations(
    directory_fd: int | None,
    state: dict[str, object] | None = None,
) -> tuple[dict[str, TargetObservation], tuple[NamespaceCandidateObservation, ...], str]:
    """Collect active-and-persisted targets plus non-authoritative namespace evidence."""
    names, inventory_status = _target_entries(directory_fd)
    active_names = {f"codex-rig-{role_id}.toml" for role_id in ROLE_IDS}
    persisted_names = (
        {str(role["target_name"]) for role in state["roles"]}
        if state is not None and isinstance(state.get("roles"), list)
        else set()
    )
    expected_names = tuple(sorted(active_names | persisted_names))
    if inventory_status in {"overflow", "unreadable"}:
        return (
            {name: TargetObservation("unsafe") for name in expected_names},
            (),
            inventory_status,
        )
    folded: dict[str, list[str]] = {}
    for name in names:
        folded.setdefault(name.casefold(), []).append(name)
    observations: dict[str, TargetObservation] = {}
    for name in expected_names:
        collision = folded.get(name.casefold(), [])
        observations[name] = (
            TargetObservation("unsafe") if collision and collision != [name] else _observe_target(directory_fd, name)
        )
    candidates: list[NamespaceCandidateObservation] = []
    for name in names:
        if (
            name in expected_names
            or not name.startswith(TARGET_NAMESPACE_PREFIX)
            or not name.endswith(TARGET_NAMESPACE_SUFFIX)
        ):
            continue
        if STRICT_TARGET_NAME.fullmatch(name) is None:
            candidates.append(NamespaceCandidateObservation(name, "malformed"))
            continue
        try:
            payload = _read_regular(directory_fd, name, SHIM_BYTES, name, required_mode=0o600)
        except ObservationError:
            payload = None
        candidates.append(NamespaceCandidateObservation(name, "regular" if payload is not None else "unsafe"))
    return observations, tuple(candidates), inventory_status


def _matches_identity(persisted: object, observed: RootIdentity) -> bool:
    """Compare one parsed persisted identity to an opened root."""
    return isinstance(persisted, dict) and persisted == {
        "canonical_path": observed.canonical_path,
        "device": observed.device,
        "inode": observed.inode,
        "owner": observed.owner,
        "group": observed.group,
        "mode": observed.mode,
    }


def _roster_hash(state: dict[str, object]) -> str:
    """Recompute a structurally validated historical roster digest."""
    roles = state["roles"]
    assert isinstance(roles, list)
    value = [{key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles]
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _read_state(state_fd: int | None) -> tuple[str, dict[str, object] | None, bytes | None]:
    """Read and parse state while separating absence from unsafe evidence."""
    if state_fd is None:
        return "absent", None, None
    try:
        payload = _read_regular(state_fd, "state.json", STATE_BYTES, "state file", required_mode=0o600)
    except ObservationError:
        return "unsafe", None, None
    if payload is None:
        return "absent", None, None
    try:
        return "parsed", parse_state(payload), payload
    except LifecycleDataError:
        return "corrupt", None, payload


def _canonical_uuid(value: str) -> bool:
    """Return whether one basename is a canonical RFC 4122 UUID."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.variant == uuid.RFC_4122


def _private_directory(metadata: os.stat_result, *, empty: bool) -> bool:
    """Require current-user private recovery-directory metadata."""
    return (
        stat.S_ISDIR(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o700
        and metadata.st_uid == os.geteuid()
        and (not empty or metadata.st_nlink == 2)
    )


def _open_child_directory(parent_fd: int, name: str) -> tuple[int, os.stat_result] | None:
    """Open one exact child directory without following links."""
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    except OSError:
        return None
    opened = os.fstat(child_fd)
    if (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(child_fd)
        return None
    return child_fd, opened


def _preparing_regular(directory_fd: int, name: str, *, links: tuple[int, ...] = (1,)) -> os.stat_result | None:
    """Validate one possibly partial current-user preparation file by metadata."""
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink not in links
        or metadata.st_size > JOURNAL_BYTES
        or mode & ~0o600 != 0
    ):
        return None
    return metadata


def _preparing_entries_exact(directory_fd: int, entries: tuple[str, ...], journal: Journal) -> bool:
    """Validate the bounded names cleanable before target mutation starts."""
    expected_children = {"before": set(), "after": set(), "quarantine": set()}
    for operation in journal.operations:
        for field, directory in (
            (operation.before_image, "before"),
            (operation.after_image, "after"),
            (operation.quarantine_name, "quarantine"),
        ):
            if field is not None:
                expected_children[directory].add(field.removeprefix(f"{directory}/"))
    allowed_root = {
        "journal.json",
        "journal.next.json",
        "state.before.json",
        "state.after.json",
        "state.publish.json",
        "before",
        "after",
        "quarantine",
    }
    if not set(entries).issubset(allowed_root):
        return False
    for name in set(entries) & {"journal.next.json", "state.before.json", "state.after.json"}:
        if _preparing_regular(directory_fd, name) is None:
            return False
    for directory, allowed in expected_children.items():
        if directory not in entries:
            continue
        opened = _open_child_directory(directory_fd, directory)
        if opened is None:
            return False
        child_fd, metadata = opened
        try:
            children = _entries(child_fd, f"preparing {directory} artifacts")
            if not _private_directory(metadata, empty=False) or not set(children).issubset(allowed):
                return False
            if any(_preparing_regular(child_fd, name) is None for name in children):
                return False
        except ObservationError:
            return False
        finally:
            os.close(child_fd)
    if "state.publish.json" in entries:
        if "state.after.json" not in entries:
            return False
        published = _preparing_regular(directory_fd, "state.publish.json", links=(2,))
        after = _preparing_regular(directory_fd, "state.after.json", links=(2,))
        if published is None or after is None or (published.st_dev, published.st_ino) != (after.st_dev, after.st_ino):
            return False
    return True


def _recovery_artifact(
    directory_fd: int,
    name: str,
    *,
    expected_hash: str,
    expected_mode: str,
    expected_links: tuple[int, ...],
) -> os.stat_result | None:
    """Validate one journal-bound recovery artifact and stable identity."""
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    if before.st_nlink not in expected_links:
        return None
    try:
        payload = _read_regular(
            directory_fd,
            name,
            JOURNAL_BYTES,
            f"recovery artifact {name}",
            required_mode=int(expected_mode, 8),
            required_links=before.st_nlink,
        )
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except (OSError, ObservationError, ValueError):
        return None
    if (
        payload is None
        or hashlib.sha256(payload).hexdigest() != expected_hash
        or (before.st_dev, before.st_ino, before.st_nlink) != (after.st_dev, after.st_ino, after.st_nlink)
    ):
        return None
    return after


def _recovery_child_exact(
    directory_fd: int,
    child: str,
    expected: dict[str, tuple[str, str, tuple[int, ...]]],
    *,
    optional: frozenset[str] = frozenset(),
) -> bool:
    """Require an exact private artifact directory with bounded crash gaps."""
    opened = _open_child_directory(directory_fd, child)
    if opened is None:
        return not (set(expected) - optional)
    child_fd, metadata = opened
    try:
        entries = _entries(child_fd, f"recovery {child} artifacts")
        required = set(expected) - optional
        return (
            _private_directory(metadata, empty=not entries)
            and required.issubset(entries)
            and set(entries).issubset(expected)
            and all(
                _recovery_artifact(
                    child_fd,
                    name,
                    expected_hash=evidence[0],
                    expected_mode=evidence[1],
                    expected_links=evidence[2],
                )
                is not None
                for name, evidence in expected.items()
                if name in entries
            )
        )
    except ObservationError:
        return False
    finally:
        os.close(child_fd)


def _durable_recovery_entries_exact(
    directory_fd: int,
    entries: tuple[str, ...],
    transaction_name: str,
    payload: bytes,
    journal: Journal,
) -> bool:
    """Bind one PREPARED-or-later journal to its exact durable artifacts."""
    if journal.transaction_id != transaction_name:
        return False
    allowed_root = {
        "journal.json",
        "journal.next.json",
        "state.before.json",
        "state.after.json",
        "state.publish.json",
        "before",
        "after",
        "quarantine",
    }
    if not set(entries).issubset(allowed_root):
        return False

    if "journal.next.json" in entries:
        try:
            successor_payload = _read_regular(
                directory_fd,
                "journal.next.json",
                JOURNAL_BYTES,
                "journal successor",
                required_mode=0o600,
            )
            if successor_payload != payload:
                validate_successor(journal, parse_journal(successor_payload or b""))
        except (JournalDataError, ObservationError):
            return False

    expected_root: dict[str, tuple[str, str, tuple[int, ...]]] = {}
    if journal.before_state.exists:
        assert journal.before_state.sha256 is not None and journal.before_state.mode is not None
        expected_root["state.before.json"] = (journal.before_state.sha256, journal.before_state.mode, (1,))
    crash_window = journal.journal_state in {"MUTATING", "RECOVERY_REQUIRED"}
    state_was_published = journal.journal_state in {"STATE_COMMITTED", "COMMITTED"}
    state_after_links = (
        (1, 2) if crash_window else (2,) if state_was_published or "state.publish.json" in entries else (1,)
    )
    if journal.after_state.exists:
        assert journal.after_state.sha256 is not None and journal.after_state.mode is not None
        expected_root["state.after.json"] = (
            journal.after_state.sha256,
            journal.after_state.mode,
            state_after_links,
        )
    if not set(expected_root).issubset(entries):
        return False
    state_names = {"state.before.json", "state.after.json"}
    if set(entries) & state_names != set(expected_root):
        return False
    root_evidence: dict[str, os.stat_result] = {}
    for name, evidence in expected_root.items():
        observed = _recovery_artifact(
            directory_fd,
            name,
            expected_hash=evidence[0],
            expected_mode=evidence[1],
            expected_links=evidence[2],
        )
        if observed is None:
            return False
        root_evidence[name] = observed
    if "state.publish.json" in entries:
        after = root_evidence.get("state.after.json")
        published = (
            _recovery_artifact(
                directory_fd,
                "state.publish.json",
                expected_hash=journal.after_state.sha256 or "",
                expected_mode=journal.after_state.mode or "",
                expected_links=(2,),
            )
            if after is not None
            else None
        )
        if published is None or (published.st_dev, published.st_ino) != (after.st_dev, after.st_ino):
            return False

    expected_children: dict[str, dict[str, tuple[str, str, tuple[int, ...]]]] = {
        "before": {},
        "after": {},
        "quarantine": {},
    }
    for operation in journal.operations:
        rolled_back = operation.rollback_progress == "TARGET_RESTORED"
        if operation.before_image is not None:
            assert operation.before_hash is not None and operation.before_mode is not None
            expected_children["before"][operation.before_image.removeprefix("before/")] = (
                operation.before_hash,
                operation.before_mode,
                (1,),
            )
        if operation.after_image is not None:
            assert operation.after_hash is not None and operation.after_mode is not None
            published = operation.progress in {"PUBLISHED", "VERIFIED"} and not rolled_back
            expected_children["after"][operation.after_image.removeprefix("after/")] = (
                operation.after_hash,
                operation.after_mode,
                (1, 2) if crash_window and not rolled_back else (2,) if published else (1,),
            )
        detached = operation.progress in {"DETACHED", "PUBLISHED", "VERIFIED"} and not rolled_back
        if operation.quarantine_name is not None and (detached or crash_window):
            assert operation.before_hash is not None and operation.before_mode is not None
            expected_children["quarantine"][operation.quarantine_name.removeprefix("quarantine/")] = (
                operation.before_hash,
                operation.before_mode,
                (1,),
            )
    optional_quarantine = frozenset(expected_children["quarantine"]) if crash_window else frozenset()
    return all(
        _recovery_child_exact(
            directory_fd,
            child,
            expected,
            optional=optional_quarantine if child == "quarantine" else frozenset(),
        )
        for child, expected in expected_children.items()
    )


def _recovery_directory(parent_fd: int, name: str, kind: str) -> RecoveryObservation:
    """Classify one nonce-bound transaction or probe directory."""
    opened = _open_child_directory(parent_fd, name)
    if opened is None:
        return RecoveryObservation("unknown", False)
    directory_fd, metadata = opened
    try:
        entries = _entries(directory_fd, f"recovery directory {name}")
        if not entries:
            empty_kind = "empty-transaction" if kind == "journal" else "empty-probe"
            exact = _private_directory(metadata, empty=True)
            return RecoveryObservation(empty_kind, exact, exact)
        if kind == "journal" and entries == ("journal.initial.json",):
            try:
                initial = os.stat(
                    "journal.initial.json",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError:
                return RecoveryObservation("unknown", False)
            initial_mode = stat.S_IMODE(initial.st_mode)
            exact = (
                stat.S_ISREG(initial.st_mode)
                and initial.st_uid == os.geteuid()
                and initial.st_nlink == 1
                and initial.st_size <= JOURNAL_BYTES
                and initial_mode & ~0o600 == 0
                and _private_directory(metadata, empty=False)
            )
            return RecoveryObservation("preparing-residue", exact)
        if kind == "journal" and entries == ("journal.initial.json", "journal.json"):
            try:
                initial_identity = os.stat(
                    "journal.initial.json",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                journal_identity = os.stat(
                    "journal.json",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                initial = _read_regular(
                    directory_fd,
                    "journal.initial.json",
                    JOURNAL_BYTES,
                    "linked initial journal",
                    required_mode=0o600,
                    required_links=2,
                )
                authoritative = _read_regular(
                    directory_fd,
                    "journal.json",
                    JOURNAL_BYTES,
                    "linked authoritative journal",
                    required_mode=0o600,
                    required_links=2,
                )
                parsed = parse_journal(authoritative or b"")
            except (JournalDataError, ObservationError):
                return RecoveryObservation("unknown", False)
            exact = (
                initial is not None
                and authoritative is not None
                and initial == authoritative
                and (initial_identity.st_dev, initial_identity.st_ino)
                == (journal_identity.st_dev, journal_identity.st_ino)
                and parsed.journal_state == "PREPARING"
                and _private_directory(metadata, empty=False)
            )
            return RecoveryObservation("journal", exact)
        receipt_name = "journal.json" if kind == "journal" else "probe.json"
        allowed = (
            {
                "journal.json",
                "journal.initial.json",
                "journal.next.json",
                "state.before.json",
                "state.after.json",
                "state.publish.json",
                "before",
                "after",
                "quarantine",
            }
            if kind == "journal"
            else {
                "probe.json",
                "probe.next.json",
                "source",
                "published",
                "replacement",
                "fsync-file",
                "fsync-directory",
            }
        )
        if (
            receipt_name not in entries
            or not set(entries).issubset(allowed)
            or not _private_directory(metadata, empty=False)
        ):
            return RecoveryObservation("unknown", False)
        try:
            receipt = _read_regular(
                directory_fd,
                receipt_name,
                JOURNAL_BYTES,
                f"recovery receipt {receipt_name}",
                required_mode=0o600,
            )
        except ObservationError:
            return RecoveryObservation("unknown", False)
        if kind == "journal" and receipt is not None:
            try:
                parsed = parse_journal(receipt)
            except JournalDataError:
                return RecoveryObservation("unknown", False)
            if parsed.journal_state == "PREPARING":
                return RecoveryObservation("journal", _preparing_entries_exact(directory_fd, entries, parsed))
            return RecoveryObservation(
                "journal",
                _durable_recovery_entries_exact(directory_fd, entries, name, receipt, parsed),
            )
        # Probe recovery remains blocked until its separate schema lands.
        return RecoveryObservation(kind, False)
    except ObservationError:
        return RecoveryObservation("unknown", False)
    finally:
        os.close(directory_fd)


def _recovery_observations(state_fd: int | None) -> tuple[RecoveryObservation, ...]:
    """Collect recognized or unknown contained state-root residue."""
    if state_fd is None:
        return ()
    try:
        names = _entries(state_fd, "state root")
    except ObservationError:
        return (RecoveryObservation("unknown", False),)
    observations: list[RecoveryObservation] = []
    allowed_root = {"state.json", "transactions"}
    for name in names:
        if name in allowed_root:
            continue
        if name.startswith(".probe-") and _canonical_uuid(name.removeprefix(".probe-")):
            observations.append(_recovery_directory(state_fd, name, "probe-receipt"))
        else:
            observations.append(RecoveryObservation("unknown", False))
    if "transactions" not in names:
        return tuple(observations)
    opened = _open_child_directory(state_fd, "transactions")
    if opened is None:
        return (*observations, RecoveryObservation("unknown", False))
    transactions_fd, transactions_metadata = opened
    try:
        if not _private_directory(transactions_metadata, empty=False):
            observations.append(RecoveryObservation("unknown", False))
            return tuple(observations)
        for name in _entries(transactions_fd, "transactions root"):
            if not _canonical_uuid(name):
                observations.append(RecoveryObservation("unknown", False))
            else:
                observations.append(_recovery_directory(transactions_fd, name, "journal"))
    except ObservationError:
        observations.append(RecoveryObservation("unknown", False))
    finally:
        os.close(transactions_fd)
    return tuple(observations)


def _blocked(
    reason: str,
    *,
    codex: RootIdentity | None = None,
    plugin: RootIdentity | None = None,
    target: RootObservation | None = None,
    state: RootObservation | None = None,
    lock: LockObservation | None = None,
    state_payload: bytes | None = None,
) -> FilesystemObservation:
    """Build one fail-closed result when roots cannot be safely observed."""
    codex_observation = _existing_root_observation(Path(codex.canonical_path), codex) if codex is not None else None
    return FilesystemObservation(
        "blocked",
        reason,
        "unsafe",
        "unsafe",
        "blocked-unknown",
        codex_observation,
        plugin,
        target,
        state,
        lock,
        state_payload,
        (),
    )


def observe_filesystem(*, codex_home: Path | str, plugin_root: Path | str) -> FilesystemObservation:
    """Observe lifecycle roots, state, targets, and recovery residue without writes.

    This layer intentionally cannot report a healthy runtime. It proves only bounded local filesystem facts for a later
    doctor and mutation manager.
    """
    home = _absolute_path(codex_home, "Codex home")
    plugin = _absolute_path(plugin_root, "plugin root")
    try:
        home_fd = _open_absolute_directory(home, "Codex home")
    except ObservationError as error:
        return _blocked(str(error))
    try:
        try:
            home_identity = _identity(home, home_fd, owned=True, protected=True)
        except ObservationError as error:
            return _blocked(str(error))
        home_observation = _existing_root_observation(home, home_identity)
        try:
            lock_observation = _observe_lock(home_fd, home)
        except ObservationError as error:
            return _blocked(str(error), codex=home_identity)
        if lock_observation.kind == "unsafe":
            return _blocked("unsafe coordination lock", codex=home_identity, lock=lock_observation)
        try:
            plugin_fd = _open_absolute_directory(plugin, "plugin root")
        except ObservationError as error:
            return _blocked(str(error), codex=home_identity)
        try:
            try:
                plugin_identity = _identity(plugin, plugin_fd)
            except ObservationError as error:
                return _blocked(str(error), codex=home_identity)
        finally:
            os.close(plugin_fd)
        target_fd = None
        state_fd = None
        target_observation = None
        state_observation = None
        try:
            target_fd, target_observation = _observe_relative_root(
                home_fd,
                home,
                ("agents",),
                "target root",
                private=False,
            )
            state_fd, state_observation = _observe_relative_root(
                home_fd,
                home,
                ("codex-rig", "shims"),
                "state root",
            )
        except ObservationError as error:
            if target_fd is not None:
                os.close(target_fd)
            if state_fd is not None:
                os.close(state_fd)
            return _blocked(
                str(error),
                codex=home_identity,
                plugin=plugin_identity,
                target=target_observation,
                state=state_observation,
                lock=lock_observation,
            )
        try:
            assert target_observation is not None
            assert state_observation is not None
            target_identity = target_observation.identity
            state_identity = state_observation.identity
            state_kind, state, state_payload = _read_state(state_fd)
            targets, namespace_candidates, namespace_inventory_status = _target_observations(
                target_fd,
                state if state_kind == "parsed" else None,
            )
            target_kind = classify_targets(state if state_kind == "parsed" else None, targets)
            recovery_kind = classify_recovery(_recovery_observations(state_fd))
            if state_kind == "parsed":
                assert state is not None
                identities_match = (
                    _matches_identity(state["codex_home_identity"], home_identity)
                    and state_identity is not None
                    and _matches_identity(state["state_root_identity"], state_identity)
                    and state["roster_hash"] == _roster_hash(state)
                )
                if state["transaction_status"] == "current":
                    identities_match = (
                        identities_match
                        and target_identity is not None
                        and _matches_identity(state["target_root_identity"], target_identity)
                    )
                state_kind = str(state["transaction_status"]) if identities_match else "inconsistent"
            blocked = (
                state_kind in {"unsafe", "corrupt", "inconsistent"}
                or target_kind in {"unsafe", "foreign", "modified", "removed-conflict"}
                or recovery_kind != "none"
                or namespace_inventory_status != "complete"
                or bool(namespace_candidates)
            )
            reason = (
                "filesystem evidence blocks lifecycle authority"
                if blocked
                else "runtime and mutation prerequisites remain unverified"
            )
            return FilesystemObservation(
                "blocked" if blocked else "degraded",
                reason,
                state_kind,
                target_kind,
                recovery_kind,
                home_observation,
                plugin_identity,
                target_observation,
                state_observation,
                lock_observation,
                state_payload,
                tuple(targets.items()),
                namespace_candidates,
                namespace_inventory_status,
            )
        except (LifecycleDataError, ObservationError) as error:
            return _blocked(
                str(error),
                codex=home_identity,
                plugin=plugin_identity,
                target=target_observation,
                state=state_observation,
                lock=lock_observation,
            )
        finally:
            if target_fd is not None:
                os.close(target_fd)
            if state_fd is not None:
                os.close(state_fd)
    finally:
        os.close(home_fd)
