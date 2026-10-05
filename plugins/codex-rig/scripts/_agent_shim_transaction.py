"""Execute or roll back a previously approved Codex Rig shim transaction.

## Purpose

Apply the exact approved operation plan while checkpointing journal state so partial failures can be verified and
restored. The executor is the only sibling module that sequences approved filesystem changes across prepare, mutation,
commit, and cleanup.

## Scope

Owns transactional sequencing and rollback; it relies on separate modules for observation, planning, approval, journals,
and POSIX I/O. It must not create a new plan or broaden an approved target set while a transaction is in progress.

## Usage

Call only from the explicit manager mutation flow after approval-digest revalidation; never invoke as an independent
CLI. Pass the manager's approved plan and journal context unchanged so the executor can compare live identities at each
checkpoint.

## Used by

``manage_role_agents.py`` and transaction failure/rollback acceptance tests call this executor. The manager remains
responsible for user approval and for reporting any recovery state returned by this layer.

## Outputs

Advances a validated journal through prepare/apply/commit/cleanup states, returning remaining recovery evidence when
rollback is required. Successful completion records durable committed state, while interruption preserves the journal
and exact residue needed for a later recovery action.

## Failure

Checkpoint failure, identity drift, write error, or journal inconsistency triggers verified rollback or a typed
transaction error; no best-effort overwrite occurs. If rollback cannot establish the expected identities, the caller
receives recovery-required evidence instead of a success result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

from _agent_shim_journal import (
    Journal,
    JournalOperation,
    canonical_journal_bytes,
    journal_value,
    parse_journal,
    validate_journal,
    validate_successor,
)
from _agent_shim_posix import (
    PRIVATE_FILE_MODE,
    FileIdentity,
    PosixPrimitiveError,
    detach_verified,
    publish_noclobber,
    publish_state_from_transaction,
    read_regular_at,
    remove_transaction_entries_at,
    replace_owned_at,
    restore_quarantine_at,
    restore_state_from_transaction,
    unlink_verified_at,
)

Checkpoint = Callable[[str], None]


class TransactionError(RuntimeError):
    """Signal a transaction that stopped without claiming success."""

    def __init__(self, message: str, *, journal: Journal) -> None:
        super().__init__(message)
        self.journal = journal


class _ForwardError(RuntimeError):
    """Carry the last durable journal out of an interrupted operation."""

    def __init__(self, *, journal: Journal) -> None:
        super().__init__("forward operation interrupted")
        self.journal = journal


@dataclass(frozen=True)
class TransactionDirectories:
    """Hold already-open exact directories required by the transaction kernel."""

    transaction_fd: int
    target_fd: int
    state_fd: int
    before_fd: int
    after_fd: int
    quarantine_fd: int


def _fail(message: str, journal: Journal) -> NoReturn:
    """Raise one journal-bearing fail-closed transaction error."""
    raise TransactionError(message, journal=journal)


def _checkpoint(callback: Checkpoint | None, name: str) -> None:
    """Expose one deterministic fault-injection and event boundary."""
    if callback is not None:
        callback(name)


def _journal_hash(journal: Journal) -> str:
    """Return the exact digest of one canonical validated journal."""
    return hashlib.sha256(canonical_journal_bytes(journal)).hexdigest()


def _discard_valid_successor(transaction_fd: int, journal: Journal) -> None:
    """Remove only an identical or legal crash-preserved successor artifact."""
    try:
        payload, identity = read_regular_at(
            transaction_fd,
            "journal.next.json",
            expected_mode=PRIVATE_FILE_MODE,
            expected_links=1,
        )
    except PosixPrimitiveError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return
        raise
    successor = parse_journal(payload)
    if canonical_journal_bytes(successor) != canonical_journal_bytes(journal):
        validate_successor(journal, successor)
    unlink_verified_at(
        transaction_fd,
        "journal.next.json",
        expected_hash=identity.sha256,
        expected_mode=PRIVATE_FILE_MODE,
    )


def _advance(
    transaction_fd: int,
    journal: Journal,
    *,
    journal_state: str | None = None,
    rollback_state: str | None = None,
    operation_index: int | None = None,
    progress: str | None = None,
    rollback_progress: str | None = None,
) -> Journal:
    """Publish one legal single-dimension journal successor."""
    changes = sum(value is not None for value in (journal_state, rollback_state, progress, rollback_progress))
    operation_change = progress is not None or rollback_progress is not None
    if changes != 1 or (operation_index is not None) != operation_change:
        raise ValueError("exactly one journal progress change is required")
    value = journal_value(journal)
    if journal_state is not None:
        value["journal_state"] = journal_state
    elif rollback_state is not None:
        value["rollback_state_progress"] = rollback_state
    else:
        assert operation_index is not None
        operations = value["operations"]
        assert isinstance(operations, list)
        operation = operations[operation_index]
        assert isinstance(operation, dict)
        operation["progress" if progress is not None else "rollback_progress"] = (
            progress if progress is not None else rollback_progress
        )
    successor = validate_successor(journal, validate_journal(value))
    replace_owned_at(
        transaction_fd,
        "journal.json",
        canonical_journal_bytes(successor),
        temporary="journal.next.json",
        expected_hash=_journal_hash(journal),
    )
    return successor


def _read_optional(parent_fd: int, name: str) -> tuple[bytes, FileIdentity] | None:
    """Read one optional private regular file without weakening other failures."""
    try:
        return read_regular_at(
            parent_fd,
            name,
            expected_mode=PRIVATE_FILE_MODE,
            expected_links=None,
        )
    except PosixPrimitiveError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return None
        raise


def _require_target(target_fd: int, operation: JournalOperation) -> None:
    """Verify one exact forward target after publication or removal."""
    current = _read_optional(target_fd, operation.target_name)
    if operation.after_exists:
        if current is None:
            raise PosixPrimitiveError("published target is absent")
        _, identity = current
        if identity.sha256 != operation.after_hash or identity.link_count != 2:
            raise PosixPrimitiveError("published target evidence mismatch")
    elif current is not None:
        raise PosixPrimitiveError("removed target still exists")


def _forward_operation(
    handles: TransactionDirectories,
    operation: JournalOperation,
    journal: Journal,
    index: int,
    checkpoint: Checkpoint | None,
) -> Journal:
    """Apply one canonical operation and durably advance its journal progress."""
    try:
        if operation.intent == "noop":
            return journal
        artifact = f"{operation.role_id}.toml"
        if operation.intent in {"update", "remove", "retire"}:
            assert operation.before_hash is not None
            detach_verified(
                handles.target_fd,
                operation.target_name,
                handles.quarantine_fd,
                artifact,
                expected_hash=operation.before_hash,
            )
            _checkpoint(checkpoint, f"{operation.role_id}:detached")
            journal = _advance(handles.transaction_fd, journal, operation_index=index, progress="DETACHED")
        if operation.intent in {"create", "repair-missing", "update"}:
            assert operation.after_hash is not None
            publish_noclobber(
                handles.after_fd,
                artifact,
                handles.target_fd,
                operation.target_name,
                expected_hash=operation.after_hash,
            )
            _checkpoint(checkpoint, f"{operation.role_id}:published")
            journal = _advance(handles.transaction_fd, journal, operation_index=index, progress="PUBLISHED")
        _require_target(handles.target_fd, operation)
        _checkpoint(checkpoint, f"{operation.role_id}:verified")
        return _advance(handles.transaction_fd, journal, operation_index=index, progress="VERIFIED")
    except BaseException as error:
        raise _ForwardError(journal=journal) from error


def _restore_operation(handles: TransactionDirectories, operation: JournalOperation) -> None:
    """Restore one operation's exact before-image from bounded physical states."""
    target = _read_optional(handles.target_fd, operation.target_name)
    quarantine_name = f"{operation.role_id}.toml"
    quarantine = _read_optional(handles.quarantine_fd, quarantine_name)
    if operation.intent in {"create", "repair-missing"}:
        if target is not None:
            _, identity = target
            if identity.sha256 != operation.after_hash:
                raise PosixPrimitiveError("created target changed before rollback")
            unlink_verified_at(
                handles.target_fd,
                operation.target_name,
                expected_hash=identity.sha256,
                expected_mode=PRIVATE_FILE_MODE,
                expected_links=2,
                parent_private=False,
            )
        return
    if operation.intent == "noop":
        return
    assert operation.before_hash is not None
    if quarantine is not None:
        _, quarantined = quarantine
        if quarantined.sha256 != operation.before_hash or quarantined.link_count != 1:
            raise PosixPrimitiveError("quarantine evidence mismatch")
        if target is not None:
            _, current = target
            if current.sha256 != operation.after_hash or current.link_count != 2:
                raise PosixPrimitiveError("replacement target changed before rollback")
            unlink_verified_at(
                handles.target_fd,
                operation.target_name,
                expected_hash=current.sha256,
                expected_mode=PRIVATE_FILE_MODE,
                expected_links=2,
                parent_private=False,
            )
        restore_quarantine_at(
            handles.quarantine_fd,
            quarantine_name,
            handles.target_fd,
            operation.target_name,
            expected_hash=operation.before_hash,
        )
        return
    if target is None:
        raise PosixPrimitiveError("before-image is absent during rollback")
    _, current = target
    if current.sha256 != operation.before_hash or current.link_count != 1:
        raise PosixPrimitiveError("restored target evidence mismatch")


def _restore_state(handles: TransactionDirectories, journal: Journal) -> None:
    """Restore or confirm the exact state snapshot that preceded the transaction."""
    current = _read_optional(handles.state_fd, "state.json")
    before = journal.before_state
    after = journal.after_state
    if before.exists and current is not None and current[1].sha256 == before.sha256:
        return
    if not before.exists and current is None:
        return
    if not after.exists or after.sha256 is None:
        raise PosixPrimitiveError("transaction lacks an after-state recovery artifact")
    restore_state_from_transaction(
        handles.transaction_fd,
        handles.state_fd,
        before_exists=before.exists,
        expected_before_hash=before.sha256,
        expected_current_hash=after.sha256,
        expected_current_links=2,
        allow_current_absent=True,
    )


def rollback_transaction(
    journal: Journal,
    handles: TransactionDirectories,
    *,
    checkpoint: Checkpoint | None = None,
) -> Journal:
    """Roll one recognized transaction back in deterministic reverse order."""
    journal = validate_journal(journal_value(journal))
    _discard_valid_successor(handles.transaction_fd, journal)
    if journal.journal_state == "ROLLED_BACK":
        for operation in reversed(journal.operations):
            _restore_operation(handles, operation)
        _restore_state(handles, journal)
        return journal
    if journal.journal_state != "RECOVERY_REQUIRED":
        journal = _advance(handles.transaction_fd, journal, journal_state="RECOVERY_REQUIRED")
    try:
        for index in range(len(journal.operations) - 1, -1, -1):
            operation = journal.operations[index]
            if operation.rollback_progress == "TARGET_RESTORED":
                continue
            _restore_operation(handles, operation)
            _checkpoint(checkpoint, f"{operation.role_id}:rollback-restored")
            journal = _advance(
                handles.transaction_fd,
                journal,
                operation_index=index,
                rollback_progress="TARGET_RESTORED",
            )
        if journal.rollback_state_progress != "RESTORED":
            _restore_state(handles, journal)
            _checkpoint(checkpoint, "state:rollback-restored")
            journal = _advance(handles.transaction_fd, journal, rollback_state="RESTORED")
        if journal.journal_state != "ROLLED_BACK":
            journal = _advance(handles.transaction_fd, journal, journal_state="ROLLED_BACK")
        return journal
    except TransactionError:
        raise
    except BaseException as error:
        raise TransactionError("rollback requires exact recovery", journal=journal) from error


def apply_transaction(
    journal: Journal,
    handles: TransactionDirectories,
    *,
    checkpoint: Checkpoint | None = None,
) -> Journal:
    """Apply one prepared transaction or return journal-bearing failure evidence."""
    journal = validate_journal(journal_value(journal))
    if journal.journal_state != "PREPARED":
        _fail("prepared journal required", journal)
    _discard_valid_successor(handles.transaction_fd, journal)
    current = _advance(handles.transaction_fd, journal, journal_state="MUTATING")
    try:
        for index, operation in enumerate(current.operations):
            current = _forward_operation(handles, operation, current, index, checkpoint)
        after = current.after_state
        if not after.exists or after.sha256 is None:
            raise PosixPrimitiveError("transaction lacks committed state")
        publish_state_from_transaction(
            handles.transaction_fd,
            handles.state_fd,
            expected_after_hash=after.sha256,
            expected_before_hash=current.before_state.sha256,
        )
        _checkpoint(checkpoint, "state:published")
        current = _advance(handles.transaction_fd, current, journal_state="STATE_COMMITTED")
        _checkpoint(checkpoint, "journal:state-committed")
        return _advance(handles.transaction_fd, current, journal_state="COMMITTED")
    except BaseException as error:
        if isinstance(error, _ForwardError):
            current = error.journal
        try:
            recovered = rollback_transaction(current, handles, checkpoint=checkpoint)
        except TransactionError as rollback_error:
            raise TransactionError(
                "transaction and rollback both failed",
                journal=rollback_error.journal,
            ) from rollback_error
        raise TransactionError("transaction failed and was rolled back", journal=recovered) from error


def mark_transaction_prepared(journal: Journal, transaction_fd: int) -> Journal:
    """Publish the sole PREPARING-to-PREPARED transition after exact staging."""
    journal = validate_journal(journal_value(journal))
    if journal.journal_state != "PREPARING":
        _fail("preparing journal required", journal)
    return _advance(transaction_fd, journal, journal_state="PREPARED")


def finalize_state_committed(journal: Journal, handles: TransactionDirectories) -> Journal:
    """Finalize an exact after-image whose durable state commit already completed."""
    journal = validate_journal(journal_value(journal))
    if journal.journal_state != "STATE_COMMITTED":
        _fail("state-committed journal required", journal)
    for operation in journal.operations:
        _require_target(handles.target_fd, operation)
    after = journal.after_state
    if not after.exists or after.sha256 is None:
        _fail("state-committed journal lacks after state", journal)
    _, state = read_regular_at(
        handles.state_fd,
        "state.json",
        expected_mode=PRIVATE_FILE_MODE,
        expected_links=2,
    )
    if state.sha256 != after.sha256:
        _fail("committed state evidence mismatch", journal)
    return _advance(handles.transaction_fd, journal, journal_state="COMMITTED")


def cleanup_transaction(journal: Journal, handles: TransactionDirectories) -> tuple[str, ...]:
    """Delete only exact terminal artifacts, leaving transaction-root removal to the manager."""
    journal = validate_journal(journal_value(journal))
    if journal.journal_state not in {"COMMITTED", "ROLLED_BACK"}:
        _fail("terminal journal required for cleanup", journal)
    _discard_valid_successor(handles.transaction_fd, journal)
    removed: list[str] = []
    for directory_fd, operations, field in (
        (handles.before_fd, journal.operations, "before"),
        (handles.after_fd, journal.operations, "after"),
        (handles.quarantine_fd, journal.operations, "quarantine"),
    ):
        entries: list[tuple[str, str | None, int | None]] = []
        for operation in operations:
            path = getattr(operation, f"{field}_image" if field != "quarantine" else "quarantine_name")
            if path is None:
                continue
            digest = operation.before_hash if field in {"before", "quarantine"} else operation.after_hash
            current = _read_optional(directory_fd, f"{operation.role_id}.toml")
            if current is None:
                continue
            _, identity = current
            if identity.sha256 != digest:
                _fail("cleanup artifact hash mismatch", journal)
            entries.append((f"{operation.role_id}.toml", identity.sha256, identity.link_count))
        removed.extend(remove_transaction_entries_at(directory_fd, tuple(entries), allow_absent=True))
    published = _read_optional(handles.transaction_fd, "state.publish.json")
    if published is not None:
        _, identity = published
        if not journal.after_state.exists or identity.sha256 != journal.after_state.sha256 or identity.link_count != 2:
            _fail("cleanup state publication mismatch", journal)
        removed.extend(
            remove_transaction_entries_at(
                handles.transaction_fd,
                (("state.publish.json", identity.sha256, 2),),
            )
        )
    root_entries: list[tuple[str, str | None, int | None]] = []
    for name, snapshot in (
        ("state.before.json", journal.before_state),
        ("state.after.json", journal.after_state),
    ):
        current = _read_optional(handles.transaction_fd, name)
        if current is None:
            continue
        _, identity = current
        if not snapshot.exists or identity.sha256 != snapshot.sha256:
            _fail("cleanup state artifact mismatch", journal)
        root_entries.append((name, identity.sha256, identity.link_count))
    removed.extend(remove_transaction_entries_at(handles.transaction_fd, tuple(root_entries), allow_absent=True))
    removed.extend(
        remove_transaction_entries_at(
            handles.transaction_fd,
            (("before", None, None), ("after", None, None), ("quarantine", None, None)),
            allow_absent=True,
        )
    )
    current_payload, current_identity = read_regular_at(
        handles.transaction_fd,
        "journal.json",
        expected_mode=PRIVATE_FILE_MODE,
    )
    if parse_journal(current_payload) != journal:
        _fail("cleanup journal identity mismatch", journal)
    removed.extend(
        remove_transaction_entries_at(
            handles.transaction_fd,
            (("journal.json", current_identity.sha256, 1),),
        )
    )
    return tuple(removed)
