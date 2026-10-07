"""Validate immutable state-transition journals for shim transactions.

## Purpose

Provide a replayable, fail-closed record of transaction progress and rollback eligibility. The journal makes each
durable checkpoint explicit so recovery can distinguish committed work from an interrupted mutation.

## Scope

Parses, validates, and transitions journal values in memory; filesystem I/O belongs to the transaction and POSIX
modules. It owns the schema and transition graph, but does not decide which operation should be planned or how a path is
written.

## Usage

Import journal constructors and transition helpers from the shim manager; this module has no direct user CLI. Callers
should validate a parsed journal before advancing it and persist only the canonical serialization returned by these
helpers.

## Used by

``_agent_shim_transaction.py``, lifecycle recovery logic, and journal contract tests call these journal helpers. The
transaction caller uses their successor checks as the durable gate between filesystem checkpoints.

## Outputs

Returns validated journal dataclasses and canonical serialized values describing allowed forward and rollback
transitions. These values retain operation hashes, modes, images, and progress needed to verify both completion and
restoration.

## Failure

Malformed records, forbidden transitions, oversized data, or hash/mode mismatches raise typed journal errors before a
transaction advances. A rejected transition leaves the caller responsible for recovery handling and never authorizes an
inferred next state.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import NoReturn

from _agent_shim_lifecycle import LifecycleDataError, parse_json_object

#: Largest journal payload, in bytes (4 MiB), that parse_journal will accept.
MAX_JOURNAL_BYTES = 4_194_304
#: Maximum number of per-role operation records a single journal may contain.
MAX_JOURNAL_OPERATIONS = 256
#: Pattern for a lowercase 64-character hexadecimal SHA-256 digest string.
DIGEST = re.compile(r"[0-9a-f]{64}")
#: Pattern for a four-digit octal file-mode string such as 0600.
MODE = re.compile(r"0[0-7]{3}")
#: Pattern for a role identifier: a lowercase letter then up to 63 lowercase letters, digits, or hyphens.
ROLE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
#: Every state a whole journal may be in, from PREPARING through COMMITTED or ROLLED_BACK.
JOURNAL_STATES = (
    "PREPARING",
    "PREPARED",
    "MUTATING",
    "STATE_COMMITTED",
    "COMMITTED",
    "RECOVERY_REQUIRED",
    "ROLLED_BACK",
)
#: Journal states allowed to follow each journal state; an empty tuple marks a terminal state.
JOURNAL_STATE_SUCCESSORS = {
    "PREPARING": ("PREPARED", "RECOVERY_REQUIRED"),
    "PREPARED": ("MUTATING", "RECOVERY_REQUIRED"),
    "MUTATING": ("MUTATING", "STATE_COMMITTED", "RECOVERY_REQUIRED"),
    "STATE_COMMITTED": ("COMMITTED", "RECOVERY_REQUIRED"),
    "COMMITTED": (),
    "RECOVERY_REQUIRED": ("RECOVERY_REQUIRED", "ROLLED_BACK"),
    "ROLLED_BACK": (),
}
#: Per-intent graph of allowed operation progress steps, such as PLANNED to PUBLISHED to VERIFIED.
FORWARD_SUCCESSORS = {
    "noop": {"VERIFIED": ()},
    "create": {"PLANNED": ("PUBLISHED",), "PUBLISHED": ("VERIFIED",), "VERIFIED": ()},
    "repair-missing": {"PLANNED": ("PUBLISHED",), "PUBLISHED": ("VERIFIED",), "VERIFIED": ()},
    "update": {
        "PLANNED": ("DETACHED",),
        "DETACHED": ("PUBLISHED",),
        "PUBLISHED": ("VERIFIED",),
        "VERIFIED": (),
    },
    "remove": {"PLANNED": ("DETACHED",), "DETACHED": ("VERIFIED",), "VERIFIED": ()},
    "retire": {"PLANNED": ("DETACHED",), "DETACHED": ("VERIFIED",), "VERIFIED": ()},
}
#: Allowed transitions of an operation's rollback progress, from NOT_STARTED to TARGET_RESTORED.
ROLLBACK_SUCCESSORS = {"NOT_STARTED": ("TARGET_RESTORED",), "TARGET_RESTORED": ()}
#: Allowed transitions of the journal-wide state-file rollback progress, from PENDING to RESTORED.
ROLLBACK_STATE_SUCCESSORS = {"PENDING": ("RESTORED",), "RESTORED": ()}
#: Exact key set required in a root identity record (canonical path, device, inode, ownership, mode).
ROOT_FIELDS = frozenset({"canonical_path", "device", "inode", "owner", "group", "mode"})
#: Exact key set required in a file image snapshot record (existence, relative path, digest, mode).
SNAPSHOT_FIELDS = frozenset({"exists", "relative_path", "sha256", "mode"})
#: Exact key set required in each journal operation record; also the field list used to serialize an operation.
OPERATION_FIELDS = frozenset(
    {
        "role_id",
        "intent",
        "target_name",
        "before_exists",
        "before_hash",
        "before_mode",
        "after_exists",
        "after_hash",
        "after_mode",
        "before_image",
        "after_image",
        "quarantine_name",
        "progress",
        "rollback_progress",
    }
)
#: Exact key set required at the top level of a journal record.
JOURNAL_FIELDS = frozenset(
    {
        "schema",
        "transaction_id",
        "transaction_nonce",
        "install_id",
        "action",
        "approved_plan_digest",
        "package_hash",
        "roster_hash",
        "codex_home_identity",
        "target_root_identity",
        "state_root_identity",
        "before_state",
        "after_state",
        "rollback_state_progress",
        "journal_state",
        "operations",
    }
)


class JournalDataError(ValueError):
    """Signal invalid or internally inconsistent journal data."""


class JournalTransitionError(JournalDataError):
    """Signal a successor that violates the durable transition graph."""


@dataclass(frozen=True)
class RootIdentity:
    """Bind one journal root to its immutable descriptor identity."""

    canonical_path: str
    device: int
    inode: int
    owner: int
    group: int
    mode: str


@dataclass(frozen=True)
class StateSnapshot:
    """Bind one optional state snapshot to exact transaction evidence."""

    exists: bool
    relative_path: str | None
    sha256: str | None
    mode: str | None


@dataclass(frozen=True)
class JournalOperation:
    """Represent one immutable role operation and its durable progress."""

    role_id: str
    intent: str
    target_name: str
    before_exists: bool
    before_hash: str | None
    before_mode: str | None
    after_exists: bool
    after_hash: str | None
    after_mode: str | None
    before_image: str | None
    after_image: str | None
    quarantine_name: str | None
    progress: str
    rollback_progress: str


@dataclass(frozen=True)
class Journal:
    """Expose a deeply immutable validated transaction journal."""

    schema: int
    transaction_id: str
    transaction_nonce: str
    install_id: str
    action: str
    approved_plan_digest: str
    package_hash: str
    roster_hash: str
    codex_home_identity: RootIdentity
    target_root_identity: RootIdentity
    state_root_identity: RootIdentity
    before_state: StateSnapshot
    after_state: StateSnapshot
    rollback_state_progress: str
    journal_state: str
    operations: tuple[JournalOperation, ...]


def _fail(message: str) -> NoReturn:
    """Raise the kernel's fail-closed data error."""
    raise JournalDataError(message)


def _integer(value: object, label: str) -> int:
    """Require one nonnegative JSON integer rather than a boolean."""
    if type(value) is not int or value < 0:
        _fail(f"invalid {label}")
    return value


def _uuid(value: object, label: str) -> str:
    """Require one canonical lowercase RFC 4122 UUID."""
    if not isinstance(value, str):
        _fail(f"invalid {label}")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise JournalDataError(f"invalid {label}") from error
    if str(parsed) != value or parsed.variant != uuid.RFC_4122:
        _fail(f"invalid {label}")
    return value


def _digest(value: object, label: str) -> str:
    """Require one lowercase SHA-256 digest."""
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _mode(value: object, label: str) -> str:
    """Require one exact four-character octal permission string."""
    if not isinstance(value, str) or MODE.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _path(value: object, label: str) -> str:
    """Require one canonical absolute POSIX path without control characters."""
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise JournalDataError(f"invalid {label}") from error
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "//" in value
        or "/./" in value
        or "/../" in value
        or value.endswith(("/.", "/.."))
        or (value != "/" and value.endswith("/"))
        or len(encoded) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(f"invalid {label}")
    return value


def _root(value: object, label: str) -> RootIdentity:
    """Validate one exact root-identity object."""
    if not isinstance(value, dict) or set(value) != ROOT_FIELDS:
        _fail(f"invalid {label}")
    return RootIdentity(
        _path(value["canonical_path"], f"{label} path"),
        _integer(value["device"], f"{label} device"),
        _integer(value["inode"], f"{label} inode"),
        _integer(value["owner"], f"{label} owner"),
        _integer(value["group"], f"{label} group"),
        _mode(value["mode"], f"{label} mode"),
    )


def _snapshot(value: object, expected_path: str, label: str) -> StateSnapshot:
    """Validate one exact present snapshot or canonical absence."""
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS or type(value["exists"]) is not bool:
        _fail(f"invalid {label}")
    if value["exists"]:
        if value["relative_path"] != expected_path:
            _fail(f"invalid {label} path")
        return StateSnapshot(
            True, expected_path, _digest(value["sha256"], f"{label} hash"), _mode(value["mode"], f"{label} mode")
        )
    if any(value[field] is not None for field in ("relative_path", "sha256", "mode")):
        _fail(f"invalid absent {label}")
    return StateSnapshot(False, None, None, None)


def _optional_digest(exists: object, digest: object, mode: object, label: str) -> tuple[bool, str | None, str | None]:
    """Validate one before-or-after existence/hash/mode triple."""
    if type(exists) is not bool:
        _fail(f"invalid {label} existence")
    if exists:
        return True, _digest(digest, f"{label} hash"), _mode(mode, f"{label} mode")
    if digest is not None or mode is not None:
        _fail(f"invalid absent {label}")
    return False, None, None


def _artifact(value: object, expected: str | None, label: str) -> str | None:
    """Require one exact derived artifact path or canonical null."""
    if value != expected:
        _fail(f"invalid {label}")
    return expected


def _operation(value: object, expected_role: str, action: str) -> JournalOperation:
    """Validate one exact intent-specific journal operation."""
    if not isinstance(value, dict) or set(value) != OPERATION_FIELDS or value["role_id"] != expected_role:
        _fail("invalid journal operation roster")
    if value["target_name"] != f"codex-rig-{expected_role}.toml":
        _fail("invalid operation target")
    intent = value["intent"]
    if not isinstance(intent, str) or intent not in FORWARD_SUCCESSORS:
        _fail("invalid operation intent")
    if action == "remove" and intent not in {"noop", "remove"}:
        _fail("invalid remove intent")
    if action == "install" and intent not in {"noop", "create", "repair-missing", "update", "retire"}:
        _fail("invalid install intent")
    before_exists, before_hash, before_mode = _optional_digest(
        value["before_exists"], value["before_hash"], value["before_mode"], "before image"
    )
    after_exists, after_hash, after_mode = _optional_digest(
        value["after_exists"], value["after_hash"], value["after_mode"], "after image"
    )
    before_path = f"before/{expected_role}.toml" if before_exists and intent in {"update", "remove", "retire"} else None
    after_path = (
        f"after/{expected_role}.toml" if after_exists and intent in {"create", "repair-missing", "update"} else None
    )
    quarantine_path = f"quarantine/{expected_role}.toml" if intent in {"update", "remove", "retire"} else None
    _artifact(value["before_image"], before_path, "before artifact")
    _artifact(value["after_image"], after_path, "after artifact")
    _artifact(value["quarantine_name"], quarantine_path, "quarantine artifact")
    if intent == "noop" and (before_exists != after_exists or before_hash != after_hash or before_mode != after_mode):
        _fail("invalid noop observation")
    if intent == "noop" and action == "remove" and before_exists:
        _fail("invalid noop action observation")
    if intent in {"create", "repair-missing"} and (before_exists or not after_exists):
        _fail("invalid create observation")
    if intent == "update" and (not before_exists or not after_exists or before_hash == after_hash):
        _fail("invalid update observation")
    if intent in {"remove", "retire"} and (not before_exists or after_exists):
        _fail("invalid remove observation")
    if intent in {"create", "repair-missing", "update"} and after_mode != "0600":
        _fail("generated after mode mismatch")
    progress = value["progress"]
    if not isinstance(progress, str) or progress not in FORWARD_SUCCESSORS[intent]:
        _fail("invalid operation progress")
    rollback_progress = value["rollback_progress"]
    if not isinstance(rollback_progress, str) or rollback_progress not in ROLLBACK_SUCCESSORS:
        _fail("invalid operation rollback progress")
    return JournalOperation(
        expected_role,
        intent,
        value["target_name"],
        before_exists,
        before_hash,
        before_mode,
        after_exists,
        after_hash,
        after_mode,
        before_path,
        after_path,
        quarantine_path,
        progress,
        rollback_progress,
    )


def validate_journal(value: object) -> Journal:
    """Validate a parsed journal object into a deeply immutable value."""
    if not isinstance(value, dict) or set(value) != JOURNAL_FIELDS:
        _fail("journal fields mismatch")
    if type(value["schema"]) is not int or value["schema"] != 1:
        _fail("journal schema mismatch")
    transaction_id = _uuid(value["transaction_id"], "transaction ID")
    transaction_nonce = _uuid(value["transaction_nonce"], "transaction nonce")
    if transaction_id != transaction_nonce:
        _fail("transaction identity mismatch")
    action = value["action"]
    if not isinstance(action, str) or action not in {"install", "remove"}:
        _fail("invalid journal action")
    operations = value["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_JOURNAL_OPERATIONS:
        _fail("journal operation roster mismatch")
    role_ids: list[str] = []
    previous_role: str | None = None
    for item in operations:
        role_id = item.get("role_id") if isinstance(item, dict) else None
        if (
            not isinstance(role_id, str)
            or ROLE_ID.fullmatch(role_id) is None
            or (previous_role is not None and role_id <= previous_role)
        ):
            _fail("journal operation roster mismatch")
        role_ids.append(role_id)
        previous_role = role_id
    immutable_operations = tuple(
        _operation(item, role_id, action) for role_id, item in zip(role_ids, operations, strict=True)
    )
    if all(operation.intent == "noop" for operation in immutable_operations):
        _fail("zero-write convergence must not create a journal")
    rollback_state = value["rollback_state_progress"]
    journal_state = value["journal_state"]
    if (
        not isinstance(rollback_state, str)
        or rollback_state not in ROLLBACK_STATE_SUCCESSORS
        or not isinstance(journal_state, str)
        or journal_state not in JOURNAL_STATES
    ):
        _fail("invalid journal progress state")
    if journal_state in {"PREPARING", "PREPARED"} and any(
        operation.progress != ("VERIFIED" if operation.intent == "noop" else "PLANNED")
        or operation.rollback_progress != "NOT_STARTED"
        for operation in immutable_operations
    ):
        _fail("pre-mutation journal has advanced progress")
    if journal_state in {"STATE_COMMITTED", "COMMITTED"}:
        if any(operation.progress != "VERIFIED" for operation in immutable_operations):
            _fail("committed journal has incomplete operations")
        after_state = value["after_state"]
        if (
            not isinstance(after_state, dict)
            or after_state.get("exists") is not True
            or after_state.get("mode") != "0600"
        ):
            _fail("committed journal lacks durable after state")
    rollback_started = rollback_state != "PENDING" or any(
        operation.rollback_progress != "NOT_STARTED" for operation in immutable_operations
    )
    if journal_state not in {"RECOVERY_REQUIRED", "ROLLED_BACK"} and rollback_started:
        _fail("rollback progress outside recovery")
    if journal_state in {"STATE_COMMITTED", "COMMITTED"} and (
        rollback_state != "PENDING"
        or any(operation.rollback_progress != "NOT_STARTED" for operation in immutable_operations)
    ):
        _fail("committed journal has rollback progress")
    if rollback_state == "RESTORED" and any(
        operation.rollback_progress != "TARGET_RESTORED" for operation in immutable_operations
    ):
        _fail("restored state precedes target restoration")
    if journal_state == "ROLLED_BACK" and (
        rollback_state != "RESTORED"
        or any(operation.rollback_progress != "TARGET_RESTORED" for operation in immutable_operations)
    ):
        _fail("rolled-back journal lacks terminal evidence")
    return Journal(
        1,
        transaction_id,
        transaction_nonce,
        _uuid(value["install_id"], "install ID"),
        action,
        _digest(value["approved_plan_digest"], "approved plan digest"),
        _digest(value["package_hash"], "package hash"),
        _digest(value["roster_hash"], "roster hash"),
        _root(value["codex_home_identity"], "Codex home identity"),
        _root(value["target_root_identity"], "target root identity"),
        _root(value["state_root_identity"], "state root identity"),
        _snapshot(value["before_state"], "state.before.json", "before state"),
        _snapshot(value["after_state"], "state.after.json", "after state"),
        rollback_state,
        journal_state,
        immutable_operations,
    )


def parse_journal(payload: bytes) -> Journal:
    """Parse one bounded canonical journal into immutable authority."""
    try:
        value = parse_json_object(payload, maximum=MAX_JOURNAL_BYTES)
    except LifecycleDataError as error:
        raise JournalDataError("invalid journal JSON") from error
    return validate_journal(value)


def _root_value(value: RootIdentity) -> dict[str, object]:
    """Project one root identity into canonical JSON fields."""
    return {
        "canonical_path": value.canonical_path,
        "device": value.device,
        "inode": value.inode,
        "owner": value.owner,
        "group": value.group,
        "mode": value.mode,
    }


def _snapshot_value(value: StateSnapshot) -> dict[str, object]:
    """Project one state snapshot into canonical JSON fields."""
    return {
        "exists": value.exists,
        "relative_path": value.relative_path,
        "sha256": value.sha256,
        "mode": value.mode,
    }


def _operation_value(value: JournalOperation) -> dict[str, object]:
    """Project one journal operation into canonical JSON fields."""
    return {field: getattr(value, field) for field in OPERATION_FIELDS}


def journal_value(value: Journal) -> dict[str, object]:
    """Project one immutable journal into a fresh canonical JSON object."""
    if not isinstance(value, Journal):
        _fail("validated journal required")
    try:
        return {
            "schema": value.schema,
            "transaction_id": value.transaction_id,
            "transaction_nonce": value.transaction_nonce,
            "install_id": value.install_id,
            "action": value.action,
            "approved_plan_digest": value.approved_plan_digest,
            "package_hash": value.package_hash,
            "roster_hash": value.roster_hash,
            "codex_home_identity": _root_value(value.codex_home_identity),
            "target_root_identity": _root_value(value.target_root_identity),
            "state_root_identity": _root_value(value.state_root_identity),
            "before_state": _snapshot_value(value.before_state),
            "after_state": _snapshot_value(value.after_state),
            "rollback_state_progress": value.rollback_state_progress,
            "journal_state": value.journal_state,
            "operations": [_operation_value(operation) for operation in value.operations],
        }
    except (AttributeError, TypeError) as error:
        raise JournalDataError("malformed immutable journal") from error


def canonical_journal_bytes(value: Journal) -> bytes:
    """Encode one immutable journal using the exact canonical JSON format."""
    validated = validate_journal(journal_value(value))
    return json.dumps(
        journal_value(validated), ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _coerce(value: object) -> Journal:
    """Validate either a parsed object or an existing immutable journal."""
    if isinstance(value, Journal):
        return validate_journal(journal_value(value))
    return validate_journal(value)


def _operation_changes(before: JournalOperation, after: JournalOperation) -> tuple[str, ...]:
    """Return mutable progress dimensions while rejecting immutable changes."""
    immutable = (
        "role_id",
        "intent",
        "target_name",
        "before_exists",
        "before_hash",
        "before_mode",
        "after_exists",
        "after_hash",
        "after_mode",
        "before_image",
        "after_image",
        "quarantine_name",
    )
    if any(getattr(before, field) != getattr(after, field) for field in immutable):
        raise JournalTransitionError("journal operation authority changed")
    changes = []
    if before.progress != after.progress:
        changes.append("progress")
    if before.rollback_progress != after.rollback_progress:
        changes.append("rollback_progress")
    return tuple(changes)


def validate_successor(before: object, after: object) -> Journal:
    """Validate one and only one legal durable journal progress transition."""
    try:
        previous = _coerce(before)
        successor = _coerce(after)
    except JournalDataError as error:
        raise JournalTransitionError("invalid journal successor evidence") from error
    immutable = (
        "schema",
        "transaction_id",
        "transaction_nonce",
        "install_id",
        "action",
        "approved_plan_digest",
        "package_hash",
        "roster_hash",
        "codex_home_identity",
        "target_root_identity",
        "state_root_identity",
        "before_state",
        "after_state",
    )
    if any(getattr(previous, field) != getattr(successor, field) for field in immutable):
        raise JournalTransitionError("journal immutable authority changed")
    changes: list[tuple[str, int | None]] = []
    if previous.journal_state != successor.journal_state:
        changes.append(("journal_state", None))
    if previous.rollback_state_progress != successor.rollback_state_progress:
        changes.append(("rollback_state_progress", None))
    for index, (old, new) in enumerate(zip(previous.operations, successor.operations, strict=True)):
        changes.extend((field, index) for field in _operation_changes(old, new))
    if len(changes) != 1:
        raise JournalTransitionError("successor must change exactly one progress dimension")
    field, index = changes[0]
    if field == "journal_state":
        if successor.journal_state not in JOURNAL_STATE_SUCCESSORS[previous.journal_state]:
            raise JournalTransitionError("illegal journal state successor")
    elif field == "rollback_state_progress":
        if (
            previous.journal_state != "RECOVERY_REQUIRED"
            or successor.rollback_state_progress not in ROLLBACK_STATE_SUCCESSORS[previous.rollback_state_progress]
        ):
            raise JournalTransitionError("illegal rollback state successor")
    else:
        if index is None:
            raise RuntimeError("index must not be None")
        old = previous.operations[index]
        new = successor.operations[index]
        if field == "progress":
            if previous.journal_state != "MUTATING" or new.progress not in FORWARD_SUCCESSORS[old.intent][old.progress]:
                raise JournalTransitionError("illegal operation progress successor")
        elif (
            previous.journal_state != "RECOVERY_REQUIRED"
            or new.rollback_progress not in ROLLBACK_SUCCESSORS[old.rollback_progress]
        ):
            raise JournalTransitionError("illegal operation rollback successor")
    return successor
