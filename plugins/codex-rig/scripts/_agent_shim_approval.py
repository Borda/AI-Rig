"""Bind an immutable shim-convergence approval to observed lifecycle evidence.

## Purpose

Make a later shim mutation prove it acts on exactly the reviewed filesystem and roster state. The binding step turns
read-only observations and a candidate operation list into approval bytes that cannot be silently substituted later.

## Scope

Validates and serializes approval data only; it neither reads the filesystem nor performs lifecycle writes. Its boundary
keeps approval evidence independent from the code that observes paths or applies operations.

## Usage

Import ``build_convergence_approval`` after observation and planning have completed; do not expose it as a standalone
CLI. Callers must supply the same observed roots, roster, runtime identities, and candidate plan that the manager will
present for approval.

## Used by

``manage_role_agents.py`` calls this module between its read-only diagnosis and explicitly approved transaction stages.
The transaction layer consumes the resulting digest to reject plans that no longer match the approved evidence.

## Outputs

Returns a canonical ``ApprovalPlan`` and digest whose bytes bind the candidate plan to the exact observed identity and
runtime state. The canonical bytes are suitable for display, user confirmation, and later byte-for-byte revalidation.

## Failure

Any digest, mode, path, roster, or lifecycle mismatch raises ``ApprovalBindingError`` and prevents the manager from
entering mutation. Rejection is fail-closed, so callers must collect fresh evidence rather than treating a partial
approval as executable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import re
from dataclasses import dataclass
from typing import Any, NoReturn

from _agent_shim_lifecycle import LifecycleDataError, TargetObservation, classify_targets, parse_state
from _agent_shim_observe import FilesystemObservation, RootIdentity, RootObservation
from _agent_shim_plan import CandidateError, CandidatePlan, Operation, build_candidate
from generate_roles import GeneratedRoster

DIGEST = re.compile(r"[0-9a-f]{64}")
MODE = re.compile(r"0[0-7]{3}")


class ApprovalBindingError(ValueError):
    """Signal incomplete or inconsistent approval evidence."""


@dataclass(frozen=True)
class RuntimeBinding:
    """Carry executable identities proven by a later live doctor."""

    python_executable_path: str
    python_executable_hash: str
    codex_binary_path: str
    codex_binary_hash: str
    doctor_eligible: bool


@dataclass(frozen=True)
class ApprovalPlan:
    """Expose only exact user-approvable bytes and their identity digest."""

    canonical_bytes: bytes
    digest: str


def _fail(message: str) -> NoReturn:
    """Raise the approval kernel's fail-closed error."""
    raise ApprovalBindingError(message)


def _path(value: object, label: str) -> str:
    """Require one canonical absolute bounded UTF-8 path."""
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise ApprovalBindingError(f"invalid {label}") from error
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
        or len(encoded) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail(f"invalid {label}")
    return value


def _digest(value: object, label: str) -> str:
    """Require one lowercase SHA-256 digest."""
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    """Require one nonnegative integer that is not a Boolean."""
    if type(value) is not int or value < 0:
        _fail(f"invalid {label}")
    return value


def _mode(value: object, label: str) -> str:
    """Require one four-digit POSIX permission mode."""
    if not isinstance(value, str) or MODE.fullmatch(value) is None:
        _fail(f"invalid {label}")
    return value


def _suffix_component(value: object) -> str:
    """Require one bounded canonical relative path component."""
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise ApprovalBindingError("invalid missing root suffix") from error
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or "/" in value
        or len(encoded) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _fail("invalid missing root suffix")
    return value


def _identity_value(identity: RootIdentity) -> dict[str, object]:
    """Project one descriptor-bound identity into exact JSON fields."""
    if not isinstance(identity, RootIdentity):
        _fail("root identity required")
    return {
        "canonical_path": _path(identity.canonical_path, "root path"),
        "device": _nonnegative_integer(identity.device, "root device"),
        "inode": _nonnegative_integer(identity.inode, "root inode"),
        "owner": _nonnegative_integer(identity.owner, "root owner"),
        "group": _nonnegative_integer(identity.group, "root group"),
        "mode": _mode(identity.mode, "root mode"),
    }


def _root_value(observation: RootObservation) -> dict[str, object]:
    """Flatten one exact existing-or-absent root observation."""
    if not isinstance(observation, RootObservation):
        _fail("root observation required")
    ancestor = _identity_value(observation.nearest_existing_ancestor)
    identity = _identity_value(observation.identity) if observation.identity is not None else None
    if type(observation.exists) is not bool or not isinstance(observation.missing_suffix_components, tuple):
        _fail("root observation consistency failure")
    suffix = tuple(_suffix_component(component) for component in observation.missing_suffix_components)
    canonical_path = _path(observation.canonical_path, "intended root path")
    if observation.exists != (identity is not None) or (observation.exists and suffix):
        _fail("root observation consistency failure")
    if observation.exists and (identity != ancestor or canonical_path != identity["canonical_path"]):
        _fail("existing root identity mismatch")
    if not observation.exists and not suffix:
        _fail("absent root suffix missing")
    if not observation.exists and posixpath.join(str(ancestor["canonical_path"]), *suffix) != canonical_path:
        _fail("absent root ancestry mismatch")
    return {
        "exists": observation.exists,
        "canonical_path": canonical_path,
        "nearest_existing_ancestor_path": ancestor["canonical_path"],
        "nearest_existing_ancestor_device": ancestor["device"],
        "nearest_existing_ancestor_inode": ancestor["inode"],
        "nearest_existing_ancestor_owner": ancestor["owner"],
        "nearest_existing_ancestor_group": ancestor["group"],
        "nearest_existing_ancestor_mode": ancestor["mode"],
        "missing_suffix_components": list(suffix),
        "device": identity["device"] if identity is not None else None,
        "inode": identity["inode"] if identity is not None else None,
        "owner": identity["owner"] if identity is not None else None,
        "group": identity["group"] if identity is not None else None,
        "mode": identity["mode"] if identity is not None else None,
    }


def _operation_value(operation: Operation) -> dict[str, object]:
    """Project one candidate operation into the frozen approval schema."""
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


def _validate_state_binding(
    candidate: CandidatePlan,
    observation: FilesystemObservation,
    targets: dict[str, TargetObservation],
) -> dict[str, Any] | None:
    """Bind the candidate to the observer's exact state bytes and classification."""
    if observation.state_payload is None:
        state = None
        expected_state = "absent"
        if candidate.transition != "initial" or candidate.prior_state_hash is not None:
            _fail("candidate source state differs")
    else:
        try:
            state = parse_state(observation.state_payload)
        except LifecycleDataError as error:
            raise ApprovalBindingError("invalid observed lifecycle state") from error
        expected_state = state["transaction_status"]
        if (
            candidate.transition not in {"same-package", "development-rebuild", "forward-upgrade"}
            or candidate.prior_state_hash != hashlib.sha256(observation.state_payload).hexdigest()
        ):
            _fail("candidate source state differs")
        state_root = observation.state_root_observation
        home = observation.codex_home_observation
        if state_root is None or state_root.identity is None or home is None or home.identity is None:
            _fail("state identity evidence missing")
        if state["codex_home_identity"] != _identity_value(home.identity) or state[
            "state_root_identity"
        ] != _identity_value(state_root.identity):
            _fail("state root identity changed")
        target_root = observation.target_root_observation
        if expected_state == "current" and (
            target_root is None
            or target_root.identity is None
            or state["target_root_identity"] != _identity_value(target_root.identity)
        ):
            _fail("state target identity changed")
        if state["install_id"] != candidate.install_id:
            _fail("candidate and state install identities differ")
        persisted_roles = {role["role_id"]: role for role in state["roles"]}
        for operation in candidate.operations:
            persisted = persisted_roles.get(operation.role_id)
            if persisted is None:
                if operation.before_card_path is not None or operation.before_role_hash is not None:
                    _fail("candidate and state role identities differ")
                continue
            if (
                persisted["target_name"] != operation.target_name
                or persisted["card_path"] != operation.before_card_path
                or persisted["role_hash"] != operation.before_role_hash
            ):
                _fail("candidate and state role identities differ")
        if set(persisted_roles) - {operation.role_id for operation in candidate.operations}:
            _fail("candidate historical roster is incomplete")
    try:
        expected_targets = classify_targets(state, targets)
    except LifecycleDataError as error:
        raise ApprovalBindingError("invalid observed target classification") from error
    if observation.state != expected_state or observation.targets != expected_targets:
        _fail("filesystem classification changed")
    return state


def _validate_lock(observation: FilesystemObservation, expected_path: str) -> dict[str, object]:
    """Require the fixed lock observation and its exact safe shape."""
    lock = observation.coordination_lock_observation
    if lock is None or lock.canonical_path != expected_path:
        _fail("coordination lock path mismatch")
    optional = (lock.device, lock.inode, lock.owner, lock.group, lock.mode, lock.link_count, lock.size)
    if lock.kind == "absent":
        if any(value is not None for value in optional):
            _fail("absent coordination lock has metadata")
        return {
            "kind": "absent",
            "canonical_path": expected_path,
            "device": None,
            "inode": None,
            "owner": None,
            "group": None,
            "mode": None,
            "link_count": None,
            "size": None,
        }
    if lock.kind != "regular" or lock.mode != "0600":
        _fail("unsafe coordination lock observation")
    for value, label in zip(optional[:4], ("device", "inode", "owner", "group"), strict=True):
        _nonnegative_integer(value, f"coordination lock {label}")
    if _nonnegative_integer(lock.link_count, "coordination lock link count") != 1:
        _fail("unsafe coordination lock observation")
    if _nonnegative_integer(lock.size, "coordination lock size") != 0:
        _fail("unsafe coordination lock observation")
    return {
        "kind": "regular",
        "canonical_path": expected_path,
        "device": lock.device,
        "inode": lock.inode,
        "owner": lock.owner,
        "group": lock.group,
        "mode": lock.mode,
        "link_count": lock.link_count,
        "size": lock.size,
    }


def _validate_observation(candidate: CandidatePlan, observation: FilesystemObservation) -> dict[str, Any] | None:
    """Require one complete non-recovery filesystem candidate observation."""
    if (
        not isinstance(observation, FilesystemObservation)
        or observation.classification != "degraded"
        or observation.recovery != "none"
        or observation.codex_home_observation is None
        or observation.plugin_root_identity is None
        or observation.target_root_observation is None
        or observation.state_root_observation is None
        or observation.coordination_lock_observation is None
        or observation.coordination_lock_observation.intent is None
        or observation.namespace_inventory_status != "complete"
        or not isinstance(observation.namespace_candidates, tuple)
        or bool(observation.namespace_candidates)
    ):
        _fail("filesystem observation is not approval-eligible")
    targets = dict(observation.target_observations)
    if len(targets) != len(observation.target_observations) or tuple(targets) != tuple(
        operation.target_name for operation in candidate.operations
    ):
        _fail("candidate and target observation rosters differ")
    for operation in candidate.operations:
        target = targets[operation.target_name]
        if not isinstance(target, TargetObservation) or target.kind not in {"absent", "regular"}:
            _fail("candidate target observation invalid")
        if operation.before_exists != (target.kind == "regular"):
            _fail("candidate target existence changed")
        if operation.before_exists and operation.before_hash != target.file_hash:
            _fail("candidate target hash changed")
    return _validate_state_binding(candidate, observation, targets)


def _source_state_value(candidate: CandidatePlan, state: dict[str, Any] | None) -> dict[str, object] | None:
    """Project the exact prior lifecycle source separately from active package identity."""
    if state is None:
        return None
    return {
        "sha256": candidate.prior_state_hash,
        "transition": candidate.transition,
        "plugin_version": state["plugin_version"],
        "package_hash": state["package_hash"],
        "bootstrap": dict(state["bootstrap"]),
        "generator_version": state["generator_version"],
        "roster_hash": state["roster_hash"],
        "plugin_root_identity": dict(state["plugin_root_identity"]),
    }


def _runtime_value(binding: RuntimeBinding) -> tuple[str, str, str, str]:
    """Validate doctor-supplied executable identity fields."""
    if not isinstance(binding, RuntimeBinding) or binding.doctor_eligible is not True:
        _fail("live doctor eligibility required")
    return (
        _path(binding.python_executable_path, "Python executable path"),
        _digest(binding.python_executable_hash, "Python executable hash"),
        _path(binding.codex_binary_path, "Codex binary path"),
        _digest(binding.codex_binary_hash, "Codex binary hash"),
    )


def build_convergence_approval(
    candidate: CandidatePlan,
    roster: GeneratedRoster,
    observation: FilesystemObservation,
    runtime: RuntimeBinding,
) -> ApprovalPlan:
    """Build exact convergence approval bytes from complete read-only evidence.

    The result is not mutation authority by itself. Apply must acquire the approved fixed lock, rerun doctor and
    observation, rebuild these bytes, and require byte and digest equality before any lifecycle write.
    """
    if not isinstance(candidate, CandidatePlan):
        _fail("candidate plan required")
    try:
        rebuilt = build_candidate(
            action=candidate.action,
            mode=candidate.mode,
            roster=roster,
            state_payload=observation.state_payload,
            targets=dict(observation.target_observations),
            install_id=candidate.install_id,
            transaction_nonce=candidate.transaction_nonce,
        )
    except (CandidateError, TypeError, ValueError) as error:
        raise ApprovalBindingError("candidate cannot be rebuilt from observed evidence") from error
    if rebuilt != candidate:
        _fail("candidate differs from authoritative rebuild")
    if all(operation.intent == "noop" for operation in candidate.operations):
        _fail("zero-write convergence requires no approval")
    observed_state = _validate_observation(candidate, observation)
    python_path, python_hash, codex_path, codex_hash = _runtime_value(runtime)
    home = observation.codex_home_observation
    target = observation.target_root_observation
    state = observation.state_root_observation
    plugin = observation.plugin_root_identity
    lock = observation.coordination_lock_observation
    assert home is not None and target is not None and state is not None and plugin is not None and lock is not None
    expected_target = f"{home.canonical_path}/agents"
    expected_state = f"{home.canonical_path}/codex-rig/shims"
    expected_lock = f"{home.canonical_path}/.codex-rig-shims.lock"
    if target.canonical_path != expected_target or state.canonical_path != expected_state:
        _fail("lifecycle root path mismatch")
    lock_value = _validate_lock(observation, expected_lock)
    if observation.state == "removed":
        assert observed_state is not None
        root_intent = (
            "unchanged"
            if target.identity is not None
            and observed_state["target_root_identity"] == _identity_value(target.identity)
            else "rebind-removed-root"
        )
    elif target.exists:
        root_intent = "unchanged"
    else:
        root_intent = "create"
    value = {
        "schema": 1,
        "action": candidate.action,
        "scope": "user",
        "mode": "converge",
        "target_root_intent": root_intent,
        "recovery_disposition": None,
        "codex_home_observation": _root_value(home),
        "coordination_lock_intent": lock.intent,
        "coordination_lock_observation": lock_value,
        "target_root_observation": _root_value(target),
        "state_root_observation": _root_value(state),
        "canonical_target_root": expected_target,
        "canonical_state_root": expected_state,
        "canonical_plugin_root": plugin.canonical_path,
        "plugin_root_identity": _identity_value(plugin),
        "plugin_version": candidate.plugin_version,
        "package_hash": candidate.package_hash,
        "bootstrap_protocol": 1,
        "bootstrap_path": "scripts/verify_role_link.py",
        "bootstrap_hash": candidate.bootstrap_hash,
        "python_executable_path": python_path,
        "python_executable_hash": python_hash,
        "codex_binary_path": codex_path,
        "codex_binary_hash": codex_hash,
        "generator_version": candidate.generator_version,
        "install_id": candidate.install_id,
        "transaction_nonce": candidate.transaction_nonce,
        "roster_hash": candidate.roster_hash,
        "source_state": _source_state_value(candidate, observed_state),
        "journal_observation": {
            "exists": False,
            "transaction_id": None,
            "journal_hash": None,
            "journal_state": None,
        },
        "recovery_observation": {
            "kind": None,
            "relative_path": None,
            "sha256": None,
            "device": None,
            "inode": None,
            "owner": None,
            "group": None,
            "mode": None,
            "link_count": None,
            "entries": None,
        },
        "operations": [_operation_value(operation) for operation in candidate.operations],
    }
    canonical = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return ApprovalPlan(canonical, hashlib.sha256(canonical).hexdigest())


def revalidate_approval_digest(plan: ApprovalPlan, expected_digest: str) -> None:
    """Require exact approval byte identity; apply must rebuild its semantics."""
    if not isinstance(plan, ApprovalPlan):
        _fail("approval plan required")
    expected = _digest(expected_digest, "approval digest")
    calculated = hashlib.sha256(plan.canonical_bytes).hexdigest()
    if not hmac.compare_digest(calculated, plan.digest) or not hmac.compare_digest(calculated, expected):
        _fail("approval digest mismatch")
