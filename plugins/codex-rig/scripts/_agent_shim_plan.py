"""Derive immutable candidate shim operations from validated evidence.

## Purpose

Separate pure mutation planning from filesystem access so the proposed shim change can be reviewed and cryptographically
bound. Its operation list is the narrow contract between lifecycle evidence and the transaction executor.

## Scope

Validates roster/state/observations and builds operation values in memory; it performs no writes or subprocess calls. It
does not approve a plan or inspect live paths, so callers must obtain fresh observations before invoking it.

## Usage

Import ``build_candidate`` after lifecycle observation, then bind it with the approval module before transaction
execution. Keep the returned canonical bytes and digest attached to the same observation set through the approval step.

## Used by

``manage_role_agents.py``, approval binding, and shim planning acceptance tests call this planner. The manager presents
its candidate to the user, while the transaction module executes only the separately bound operations.

## Outputs

Returns a ``CandidatePlan`` with canonical operation order, state transitions, and a digest suitable for immutable
approval binding. The plan records expected before/after identities so execution can verify every role target instead of
applying a best-effort diff.

## Failure

Inconsistent state, unsupported version ordering, duplicate targets, or invalid roster data raises ``CandidateError``
and stops planning. No caller may reinterpret a partial operation list as safe because the digest is produced only for a
fully validated candidate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, NoReturn

from _agent_shim_lifecycle import LifecycleDataError, TargetObservation, classify_targets, parse_state
from generate_roles import ROLE_IDS, SEMVER_PATTERN, GeneratedRole, GeneratedRoster, roster_identity_hash

DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_OPERATION_ROLES = 256


class CandidateError(ValueError):
    """Signal incomplete, inconsistent, or untrusted candidate input."""


@dataclass(frozen=True)
class Operation:
    """Represent one immutable role target transition candidate."""

    role_id: str
    target_name: str
    before_card_path: str | None
    before_role_hash: str | None
    after_card_path: str | None
    after_role_hash: str | None
    before_exists: bool
    before_hash: str | None
    before_mode: str | None
    after_exists: bool
    after_hash: str | None
    after_mode: str | None
    intent: str


@dataclass(frozen=True)
class CandidatePlan:
    """Bind candidate operations to exact canonical bytes and their digest.

    This value is not an approval. A live doctor and complete descriptor-bound observations must wrap it in the full
    lifecycle approval before mutation.
    """

    action: str
    mode: str
    plugin_version: str
    package_hash: str
    bootstrap_hash: str
    generator_version: int
    install_id: str
    transaction_nonce: str
    roster_hash: str
    transition: str
    prior_state_hash: str | None
    operations: tuple[Operation, ...]
    canonical_bytes: bytes
    digest: str


def _fail(message: str) -> NoReturn:
    """Raise the kernel's single fail-closed error type."""
    raise CandidateError(message)


def _canonical_bytes(value: object) -> bytes:
    """Encode exact canonical JSON while rejecting non-finite values."""
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise CandidateError("candidate is not canonical JSON") from error


def _digest(value: object, label: str) -> str:
    """Require one exact lowercase SHA-256 digest."""
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _uuid(value: object, label: str) -> str:
    """Require one canonical lowercase RFC 4122 UUID."""
    if not isinstance(value, str):
        _fail(f"invalid {label}")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise CandidateError(f"invalid {label}") from error
    if str(parsed) != value or parsed.variant != uuid.RFC_4122:
        _fail(f"invalid {label}")
    return value


def _validate_roster(roster: object) -> GeneratedRoster:
    """Require one exact immutable generated roster and its byte identities."""
    if not isinstance(roster, GeneratedRoster):
        _fail("generated roster required")
    if (
        not isinstance(roster.plugin_version, str)
        or SEMVER_PATTERN.fullmatch(roster.plugin_version) is None
        or type(roster.generator_version) is not int
        or roster.generator_version != 1
    ):
        _fail("invalid generated package identity")
    _digest(roster.package_hash, "package hash")
    _digest(roster.bootstrap_hash, "bootstrap hash")
    if not isinstance(roster.roles, tuple) or len(roster.roles) != len(ROLE_IDS):
        _fail("generated roster length mismatch")
    for role_id, role in zip(ROLE_IDS, roster.roles, strict=True):
        if not isinstance(role, GeneratedRole):
            _fail("invalid generated role")
        if (
            role.role_id != role_id
            or role.target_name != f"codex-rig-{role_id}.toml"
            or role.card_path != f"roles/{role_id}/ROLE.md"
            or not isinstance(role.shim_bytes, bytes)
        ):
            _fail("generated role identity mismatch")
        _digest(role.role_hash, "generated role hash")
        _digest(role.file_hash, "generated file hash")
        if hashlib.sha256(role.shim_bytes).hexdigest() != role.file_hash:
            _fail("generated file hash mismatch")
    return roster


def _roster_hash(roster: GeneratedRoster) -> str:
    """Use the generator-owned canonical role identity hash."""
    return roster_identity_hash(
        tuple((role.role_id, role.target_name, role.card_path, role.role_hash) for role in roster.roles)
    )


def _state_roster_hash(state: dict[str, Any]) -> str:
    """Recompute the persisted roster identity independently of the active package."""
    value = [
        {key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in state["roles"]
    ]
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _semver_precedence(version: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    """Return the SemVer core and prerelease identifiers, excluding build metadata."""
    without_build = version.split("+", maxsplit=1)[0]
    core_text, separator, prerelease = without_build.partition("-")
    core = tuple(int(part) for part in core_text.split("."))
    assert len(core) == 3
    return core, tuple(prerelease.split(".")) if separator else None


def _compare_semver(left: str, right: str) -> int:
    """Compare two already-validated SemVer strings by normative precedence."""
    left_core, left_pre = _semver_precedence(left)
    right_core, right_pre = _semver_precedence(right)
    if left_core != right_core:
        return 1 if left_core > right_core else -1
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1
    for left_item, right_item in zip(left_pre, right_pre):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_item) > int(right_item) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_item > right_item else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def _validate_state(
    state_payload: object,
    roster: GeneratedRoster,
    install_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Parse exact prior ownership state and enforce forward compatibility."""
    if state_payload is None:
        return None, "initial"
    if not isinstance(state_payload, bytes):
        _fail("exact state bytes required")
    try:
        validated = parse_state(state_payload)
    except LifecycleDataError as error:
        raise CandidateError("invalid lifecycle state") from error
    if validated["install_id"] != install_id:
        _fail("state install ID mismatch")
    if validated["roster_hash"] != _state_roster_hash(validated):
        _fail("persisted roster identity mismatch")
    comparison = _compare_semver(roster.plugin_version, validated["plugin_version"])
    if comparison < 0:
        _fail("plugin downgrade is not supported")
    if comparison == 0:
        current_roles = {role.role_id: (role.target_name, role.card_path, role.role_hash) for role in roster.roles}
        persisted_roles = {
            role["role_id"]: (role["target_name"], role["card_path"], role["role_hash"]) for role in validated["roles"]
        }
        exact_package = not (
            validated["package_hash"] != roster.package_hash
            or validated["bootstrap"]["helper_hash"] != roster.bootstrap_hash
            or validated["generator_version"] != roster.generator_version
            or validated["roster_hash"] != _roster_hash(roster)
            or persisted_roles != current_roles
        )
        if exact_package:
            return validated, "same-package"
        prior_build = validated["plugin_version"].partition("+")[2]
        active_build = roster.plugin_version.partition("+")[2]
        if not (
            roster.plugin_version != validated["plugin_version"]
            and prior_build.startswith("codex.")
            and active_build.startswith("codex.")
            and active_build > prior_build
        ):
            _fail("same-version package content differs")
        return validated, "development-rebuild"
    return validated, "forward-upgrade"


def _validate_targets(
    targets: object,
    state: dict[str, Any] | None,
    roster: GeneratedRoster,
) -> dict[str, TargetObservation]:
    """Require the exact canonical union of active and persisted targets."""
    active_names = {role.target_name for role in roster.roles}
    persisted_names = {role["target_name"] for role in state["roles"]} if state is not None else set()
    names = tuple(sorted(active_names | persisted_names))
    if len(names) > MAX_OPERATION_ROLES:
        _fail("target observation union is too large")
    if not isinstance(targets, dict) or tuple(targets) != names:
        _fail("target observation roster mismatch")
    if any(not isinstance(value, TargetObservation) or not isinstance(value.kind, str) for value in targets.values()):
        _fail("invalid target observation")
    return dict(targets)


def _operation(
    *,
    role_id: str,
    target_name: str,
    before_card_path: str | None,
    before_role_hash: str | None,
    after_card_path: str | None,
    after_role_hash: str | None,
    desired_hash: str | None,
    observation: TargetObservation,
    intent: str,
) -> Operation:
    """Build one operation from already classified exact evidence."""
    before_exists = observation.kind == "regular"
    before_hash = observation.file_hash if before_exists else None
    before_mode = "0600" if before_exists else None
    if intent in {"remove", "retire"} or intent == "noop" and not before_exists:
        after_exists, after_hash, after_mode = False, None, None
    elif intent == "noop":
        after_exists, after_hash, after_mode = True, before_hash, before_mode
    else:
        if desired_hash is None:
            _fail("desired shim hash missing")
        after_exists, after_hash, after_mode = True, desired_hash, "0600"
    return Operation(
        role_id,
        target_name,
        before_card_path,
        before_role_hash,
        after_card_path,
        after_role_hash,
        before_exists,
        before_hash,
        before_mode,
        after_exists,
        after_hash,
        after_mode,
        intent,
    )


def _derive_operations(
    action: str,
    roster: GeneratedRoster,
    state: dict[str, Any] | None,
    targets: dict[str, TargetObservation],
) -> tuple[Operation, ...]:
    """Derive the exact whole-roster candidate or refuse unsafe evidence."""
    try:
        classification = classify_targets(state, targets)
    except LifecycleDataError as error:
        raise CandidateError("target classification failed") from error
    allowed = {"absent", "current", "repairable-missing", "removed"}
    if classification not in allowed:
        _fail(f"untrusted target classification: {classification}")
    active_roles = {role.role_id: role for role in roster.roles}
    persisted_roles = {item["role_id"]: item for item in state["roles"]} if state is not None else {}
    role_ids = sorted(active_roles.keys() | persisted_roles)
    operations = []
    for role_id in role_ids:
        active = active_roles.get(role_id)
        persisted = persisted_roles.get(role_id)
        target_name = active.target_name if active is not None else persisted["target_name"]
        observation = targets[target_name]
        if action == "remove":
            intent = "remove" if observation.kind == "regular" else "noop"
        elif active is None:
            intent = "retire" if observation.kind == "regular" else "noop"
        elif persisted is None or state is None or state["transaction_status"] == "removed":
            intent = "create"
        elif observation.kind == "absent":
            intent = "repair-missing"
        elif (
            persisted["file_hash"] == active.file_hash
            and persisted["card_path"] == active.card_path
            and persisted["role_hash"] == active.role_hash
        ):
            intent = "noop"
        else:
            intent = "update"
        operations.append(
            _operation(
                role_id=role_id,
                target_name=target_name,
                before_card_path=persisted["card_path"] if persisted is not None else None,
                before_role_hash=persisted["role_hash"] if persisted is not None else None,
                after_card_path=active.card_path if active is not None and action == "install" else None,
                after_role_hash=active.role_hash if active is not None and action == "install" else None,
                desired_hash=active.file_hash if active is not None else None,
                observation=observation,
                intent=intent,
            )
        )
    return tuple(operations)


def _operation_value(operation: Operation) -> dict[str, object]:
    """Project one immutable operation into its canonical JSON object."""
    return {
        "role_id": operation.role_id,
        "target_name": operation.target_name,
        "before_card_path": operation.before_card_path,
        "before_role_hash": operation.before_role_hash,
        "after_card_path": operation.after_card_path,
        "after_role_hash": operation.after_role_hash,
        "before_exists": operation.before_exists,
        "before_hash": operation.before_hash,
        "before_mode": operation.before_mode,
        "after_exists": operation.after_exists,
        "after_hash": operation.after_hash,
        "after_mode": operation.after_mode,
        "intent": operation.intent,
    }


def _candidate_value(candidate: CandidatePlan) -> dict[str, object]:
    """Project one immutable candidate into its complete canonical value."""
    return {
        "schema": 1,
        "authorization": "candidate-only",
        "action": candidate.action,
        "mode": candidate.mode,
        "plugin_version": candidate.plugin_version,
        "package_hash": candidate.package_hash,
        "bootstrap_hash": candidate.bootstrap_hash,
        "generator_version": candidate.generator_version,
        "install_id": candidate.install_id,
        "transaction_nonce": candidate.transaction_nonce,
        "roster_hash": candidate.roster_hash,
        "transition": candidate.transition,
        "prior_state_hash": candidate.prior_state_hash,
        "operations": [_operation_value(operation) for operation in candidate.operations],
    }


def build_candidate(
    *,
    action: str,
    mode: str,
    roster: GeneratedRoster,
    state_payload: bytes | None,
    targets: dict[str, TargetObservation],
    install_id: str,
    transaction_nonce: str,
) -> CandidatePlan:
    """Build one deterministic operation candidate for later approval binding.

    The result cannot authorize mutation. It omits live doctor, root, lock, executable, recovery, and descriptor
    observations required by the public lifecycle approval contract.
    """
    if not isinstance(action, str) or action not in {"install", "remove"} or mode != "converge":
        _fail("unsupported candidate action or mode")
    stable_install_id = _uuid(install_id, "install ID")
    stable_nonce = _uuid(transaction_nonce, "transaction nonce")
    generated = _validate_roster(roster)
    validated_state, transition = _validate_state(state_payload, generated, stable_install_id)
    observations = _validate_targets(targets, validated_state, generated)
    operations = _derive_operations(action, generated, validated_state, observations)
    candidate = CandidatePlan(
        action,
        mode,
        generated.plugin_version,
        generated.package_hash,
        generated.bootstrap_hash,
        generated.generator_version,
        stable_install_id,
        stable_nonce,
        _roster_hash(generated),
        transition,
        hashlib.sha256(state_payload).hexdigest() if state_payload is not None else None,
        operations,
        b"",
        "",
    )
    canonical = _canonical_bytes(_candidate_value(candidate))
    return CandidatePlan(
        candidate.action,
        candidate.mode,
        candidate.plugin_version,
        candidate.package_hash,
        candidate.bootstrap_hash,
        candidate.generator_version,
        candidate.install_id,
        candidate.transaction_nonce,
        candidate.roster_hash,
        candidate.transition,
        candidate.prior_state_hash,
        candidate.operations,
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def revalidate_candidate_digest(candidate: CandidatePlan, expected_digest: str) -> None:
    """Require rebuilt candidate bytes to match their expected digest.

    Digest equality proves only candidate determinism and is never lifecycle approval or mutation authorization.
    """
    if not isinstance(candidate, CandidatePlan):
        _fail("candidate value required")
    expected = _digest(expected_digest, "candidate digest")
    rebuilt = _canonical_bytes(_candidate_value(candidate))
    calculated = hashlib.sha256(rebuilt).hexdigest()
    if (
        not hmac.compare_digest(rebuilt, candidate.canonical_bytes)
        or not hmac.compare_digest(calculated, candidate.digest)
        or not hmac.compare_digest(calculated, expected)
    ):
        _fail("candidate digest mismatch")
