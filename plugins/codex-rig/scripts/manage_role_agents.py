#!/usr/bin/env python3
"""Diagnose and explicitly manage the complete Codex Rig user-agent shim roster.

## Purpose

Coordinate safe read-only diagnosis and, only after approval, transactional installation, refresh, or removal of role
shims. It is the narrow public boundary that turns a requested lifecycle action into evidence, an approval prompt, and
an auditable transaction.

## Scope

Is the public lifecycle orchestrator; pure planning, observation, approval, journals, and filesystem primitives stay in
focused sibling modules. It owns action routing and result reporting, but does not duplicate their validation or
foundational safety rules.

## Usage

Run the documented ``agent-shims`` command path for one requested action and inspect its structured result before any
follow-up. Diagnostic actions remain read-only; mutation actions require the manager's explicit approval path and digest
revalidation.

## Used by

The agent-shims skill, optional session-start health reporting, and lifecycle acceptance tests call this orchestrator.
These callers use its stable action names and structured output to distinguish diagnosis, approval requirements,
completion, and recovery.

## Outputs

Emits structured diagnosis, mutation plans, approval requirements, and post-transaction recovery state for a single
requested action. The output preserves enough local identity and classified status for a human or follow-up command to
make the next decision without hidden retries.

## Failure

Unsupported platform, missing active package, unsafe filesystem evidence, denied approval, or transaction drift exits
with a classified local failure and no hidden retry. A recovery-required result is reported as an explicit incomplete
outcome rather than being mistaken for successful installation or removal.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import NoReturn


class ManagerAction(str, Enum):
    """The four verbs of the public manager grammar.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``, so members serialise into the
    manager's JSON output as plain strings. ``DOCTOR`` and ``STATUS`` are zero-write diagnostics; ``INSTALL`` and
    ``REMOVE`` are the mutations.
    """

    DOCTOR = "doctor"
    STATUS = "status"
    INSTALL = "install"
    REMOVE = "remove"


# The two disjoint halves of the grammar — every guard below tests one of these.
_DIAGNOSTIC_ACTIONS = (ManagerAction.DOCTOR, ManagerAction.STATUS)
_MUTATION_ACTIONS = (ManagerAction.INSTALL, ManagerAction.REMOVE)

# Direct manager commands must not mutate the installed plugin cache with import bytecode.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

MINIMUM_PYTHON = (3, 10)
DIAGNOSTIC_INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
SUPPORTED_PLATFORMS = ("darwin", "linux")
MARKETPLACE = "borda-ai-rig"
PLUGIN_NAME = "codex-rig"
MAX_BINARY_BYTES = 512 * 1024 * 1024

# Unsupported hosts must reach the stable refusal protocol without importing
# lifecycle modules whose POSIX primitives are intentionally unavailable there.
if sys.platform.startswith(SUPPORTED_PLATFORMS):
    from _agent_shim_approval import ApprovalPlan, RuntimeBinding, build_convergence_approval
    from _agent_shim_journal import Journal, parse_journal, validate_journal
    from _agent_shim_lifecycle import parse_state
    from _agent_shim_observe import FilesystemObservation, observe_filesystem
    from _agent_shim_plan import CandidatePlan, build_candidate
    from _agent_shim_posix import (
        DirectoryIdentity,
        acquire_coordination_lock,
        create_directory_at,
        create_private_path,
        directory_identity,
        open_directory_at,
        read_regular_at,
        remove_transaction_entries_at,
        unlink_verified_at,
        write_exclusive_at,
        write_initial_journal,
    )
    from _agent_shim_transaction import (
        TransactionDirectories,
        TransactionError,
        apply_transaction,
        cleanup_transaction,
        finalize_state_committed,
        mark_transaction_prepared,
        rollback_transaction,
    )
    from generate_roles import GeneratedRoster, load_generated_roster, roster_identity_hash


class ManagerArgumentParser(argparse.ArgumentParser):
    """Emit stable usage errors without writing parser diagnostics itself."""

    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


@dataclass(frozen=True)
class CheckResult:
    """Describe one live manager prerequisite without claiming more evidence."""

    status: str
    detail: str


@dataclass(frozen=True)
class DiagnosticResult:
    """Expose one complete read-only doctor or status result."""

    action: ManagerAction
    classification: str
    plugin_version: str | None
    python_version: str
    platform: str
    codex_home: str
    plugin_root: str
    checks: dict[str, CheckResult]
    state: str
    targets: str
    recovery: str
    namespace_candidates: tuple[str, ...]


@dataclass(frozen=True)
class MutationPlan:
    """Bind one read-only mutation candidate to exact approval evidence."""

    action: ManagerAction
    codex_home: Path
    plugin_root: Path
    python_path: Path
    python_hash: str
    codex_path: Path
    codex_hash: str
    roster: GeneratedRoster
    observation: FilesystemObservation
    candidate: CandidatePlan
    approval: ApprovalPlan | None


@dataclass(frozen=True)
class RecoveryPlan:
    """Bind one exact recognized recovery residue to explicit approval bytes."""

    action: ManagerAction
    codex_home: Path
    plugin_root: Path
    observation: FilesystemObservation
    transaction_id: str
    journal: Journal | None
    inventory: tuple[dict[str, object], ...]
    canonical_bytes: bytes
    digest: str


def _digest_regular_executable(path: Path, label: str) -> tuple[Path, str]:
    """Resolve and hash one stable current-user executable without links."""
    try:
        canonical = path.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
    except OSError as error:
        raise ValueError(f"{label} is unavailable: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} is not a regular file: {canonical}")
        if before.st_size < 0 or before.st_size > MAX_BINARY_BYTES:
            raise ValueError(
                f"{label} exceeds {MAX_BINARY_BYTES}-byte safety limit: {canonical} has {before.st_size} bytes"
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            digest.update(chunk)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise ValueError(f"{label} changed during hashing")
    finally:
        os.close(descriptor)
    if not os.access(canonical, os.X_OK):
        raise ValueError(f"{label} is not executable")
    return canonical, digest.hexdigest()


def _codex_executable(explicit: Path | None) -> Path:
    """Resolve the configured or PATH-selected Codex executable name."""
    if explicit is not None:
        return explicit
    discovered = shutil.which("codex")
    if discovered is None:
        raise ValueError("Codex executable is unavailable on PATH")
    return Path(discovered)


def _active_package_check(
    plugin_root: Path,
    codex_home: Path,
    codex_binary: Path,
    codex_hash: str,
    plugin_version: str,
) -> CheckResult:
    """Run the active-cache oracle in a disposable copy of Codex metadata."""
    try:
        expected_root = codex_home / "plugins" / "cache" / MARKETPLACE / PLUGIN_NAME / plugin_version
        if not expected_root.is_dir() or not os.path.samefile(plugin_root, expected_root):
            return CheckResult("degraded", "plugin root is not the selected cache-version path")
        with tempfile.TemporaryDirectory(prefix="codex-rig-doctor-") as temporary:
            sandbox_home = Path(temporary)
            sandbox_home.chmod(0o700)
            config = codex_home / "config.toml"
            if config.exists():
                if not stat.S_ISREG(config.lstat().st_mode):
                    return CheckResult("degraded", "Codex config is not a regular file")
                shutil.copyfile(config, sandbox_home / "config.toml")
                (sandbox_home / "config.toml").chmod(0o600)
            for relative in (Path("plugins"), Path(".tmp") / "marketplaces"):
                source = codex_home / relative
                if source.is_dir():
                    if not stat.S_ISDIR(source.lstat().st_mode):
                        return CheckResult("degraded", f"Codex metadata root is linked: {relative}")
                    shutil.copytree(source, sandbox_home / relative, symlinks=True)
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(sandbox_home)
            completed = subprocess.run(
                [str(codex_binary), "plugin", "list", "--marketplace", MARKETPLACE, "--json"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=15,
            )
    except (OSError, shutil.Error, subprocess.TimeoutExpired) as error:
        return CheckResult("degraded", f"active package oracle failed: {error}")
    if completed.returncode != 0 or len(completed.stdout) > 1_048_576 or len(completed.stderr) > 1_048_576:
        detail = completed.stderr.decode("utf-8", "replace").strip() or "active package oracle refused this cache"
        return CheckResult("degraded", detail[:512])
    try:
        payload = json.loads(completed.stdout)
        installed = payload["installed"] if isinstance(payload, dict) else None
        matches = [
            item
            for item in installed
            if isinstance(item, dict)
            and item.get("pluginId") == f"{PLUGIN_NAME}@{MARKETPLACE}"
            and item.get("name") == PLUGIN_NAME
            and item.get("marketplaceName") == MARKETPLACE
            and item.get("installed") is True
            and item.get("enabled") is True
            and item.get("version") == plugin_version
        ]
    except (KeyError, TypeError, ValueError):
        matches = []
    if len(matches) != 1:
        return CheckResult("degraded", "active package oracle returned no unique enabled version")
    try:
        _, current_hash = _digest_regular_executable(codex_binary, "Codex executable")
    except ValueError as error:
        return CheckResult("blocked", str(error))
    if not hmac.compare_digest(current_hash, codex_hash):
        return CheckResult("blocked", "Codex executable changed during active-package diagnosis")
    return CheckResult("pass", "active package, manifest, helper, and representative card match")


def _check_classification(checks: dict[str, CheckResult], filesystem_classification: str) -> str:
    """Reduce prerequisite checks and filesystem state to one classification."""
    if filesystem_classification == "blocked" or any(item.status == "blocked" for item in checks.values()):
        return "blocked"
    if any(item.status != "pass" for item in checks.values()):
        return "degraded"
    return "healthy"


def _classification(checks: dict[str, CheckResult], observation: FilesystemObservation) -> str:
    """Reduce exact prerequisite evidence to the stable doctor classification."""
    return _check_classification(checks, observation.classification)


def _windows_inventory(codex_home: Path) -> tuple[CheckResult, tuple[str, ...]]:
    """Inventory legacy shim names on Windows without authenticating or mutating them."""
    agents = codex_home / "agents"
    if not agents.exists():
        return CheckResult("pass", "agents directory absent; no shim candidates"), ()
    metadata = agents.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if agents.is_symlink() or attributes & reparse_flag or not agents.is_dir():
        return CheckResult("blocked", f"agents path is linked or not a directory: {agents}"), ()
    candidates: list[str] = []
    for entry in agents.iterdir():
        if not entry.name.startswith("codex-rig-") or entry.suffix != ".toml":
            continue
        entry_metadata = entry.lstat()
        entry_attributes = getattr(entry_metadata, "st_file_attributes", 0)
        if entry.is_symlink() or entry_attributes & reparse_flag or not entry.is_file():
            return CheckResult("blocked", f"shim candidate is linked or not a file: {entry.name}"), ()
        candidates.append(entry.name)
    return (
        CheckResult("pass", "Windows inventory is read-only; persistent shim mutation is unavailable"),
        tuple(sorted(candidates)),
    )


def _diagnose_windows(
    *,
    action: ManagerAction,
    codex_home: Path,
    plugin_root: Path,
    codex_binary: Path | None,
    check_active_package: bool,
) -> DiagnosticResult:
    """Run portable Windows package, executable, and shim-inventory checks."""
    from _package_identity import verify_package

    home = codex_home.resolve(strict=True)
    root = plugin_root.resolve(strict=True)
    if (
        codex_home.is_symlink()
        or plugin_root.is_symlink()
        or (
            os.name == "nt"
            and (Path(os.path.abspath(codex_home)) != home or Path(os.path.abspath(plugin_root)) != root)
        )
    ):
        raise ValueError("Codex home and plugin root must be canonical non-symlink paths")
    python_version = ".".join(str(item) for item in sys.version_info[:3])
    checks = {
        "python": CheckResult(
            "pass" if sys.version_info >= MINIMUM_PYTHON else "blocked",
            f"Python {python_version}; minimum is 3.10",
        ),
        "platform": CheckResult("pass", f"platform {sys.platform}; native read-only diagnostics supported"),
    }
    filesystem, candidates = _windows_inventory(home)
    checks["filesystem"] = filesystem
    plugin_version: str | None = None
    try:
        identity = verify_package(root, enforce_modes=False)
        plugin_version = identity.version
        _, _ = _digest_regular_executable(Path(sys.executable), "Python executable")
        codex_path, codex_hash = _digest_regular_executable(_codex_executable(codex_binary), "Codex executable")
        checks["package"] = CheckResult(
            "pass",
            f"verified {identity.files_verified} package files; POSIX mode checks are not applicable",
        )
        checks["executables"] = CheckResult("pass", "canonical Python and Codex executables are stable")
        checks["active_package"] = (
            _active_package_check(root, home, codex_path, codex_hash, identity.version)
            if check_active_package
            else CheckResult("degraded", "active package oracle was explicitly skipped")
        )
    except (OSError, ValueError) as error:
        checks["package"] = CheckResult("blocked", str(error))
        checks["executables"] = CheckResult("blocked", str(error))
        checks["active_package"] = CheckResult("blocked", "package or executable validation failed first")
    return DiagnosticResult(
        action,
        _check_classification(checks, "blocked" if filesystem.status == "blocked" else "pass"),
        plugin_version,
        python_version,
        sys.platform,
        str(home),
        str(root),
        checks,
        "not-applicable",
        "inventory-only",
        "not-applicable",
        candidates,
    )


def diagnose(
    *,
    action: ManagerAction | str,
    codex_home: Path,
    plugin_root: Path,
    codex_binary: Path | None = None,
    check_active_package: bool = True,
) -> DiagnosticResult:
    """Run a zero-write live doctor and optional installed-state summary."""
    if action not in _DIAGNOSTIC_ACTIONS:
        raise ValueError("diagnostic action must be doctor or status")
    action = ManagerAction(action)
    if sys.platform == "win32":
        return _diagnose_windows(
            action=action,
            codex_home=codex_home,
            plugin_root=plugin_root,
            codex_binary=codex_binary,
            check_active_package=check_active_package,
        )
    home = codex_home.resolve(strict=True)
    root = plugin_root.resolve(strict=True)
    if Path(os.path.abspath(codex_home)) != home or Path(os.path.abspath(plugin_root)) != root:
        raise ValueError("Codex home and plugin root must be canonical non-symlink paths")
    checks: dict[str, CheckResult] = {}
    python_version = ".".join(str(item) for item in sys.version_info[:3])
    checks["python"] = CheckResult(
        "pass" if sys.version_info >= MINIMUM_PYTHON else "blocked",
        f"Python {python_version}; minimum is 3.10",
    )
    checks["platform"] = CheckResult(
        "pass" if sys.platform.startswith(SUPPORTED_PLATFORMS) else "blocked",
        f"platform {sys.platform}; native Windows and unknown POSIX hosts are unsupported",
    )
    observation = observe_filesystem(codex_home=home, plugin_root=root)
    checks["filesystem"] = CheckResult(
        "pass" if observation.classification != "blocked" else "blocked",
        observation.reason,
    )
    roster: GeneratedRoster | None = None
    try:
        python_path, python_hash = _digest_regular_executable(Path(sys.executable), "Python executable")
        codex_path, codex_hash = _digest_regular_executable(_codex_executable(codex_binary), "Codex executable")
        roster = load_generated_roster(
            root,
            install_id=DIAGNOSTIC_INSTALL_ID,
            python_executable=python_path,
            python_executable_hash=python_hash,
            codex_binary=codex_path,
            codex_binary_hash=codex_hash,
        )
        checks["package"] = CheckResult("pass", "package manifest, generator, verifier, and all roles match")
        checks["executables"] = CheckResult("pass", "canonical Python and Codex executables are stable")
        checks["active_package"] = (
            _active_package_check(root, home, codex_path, codex_hash, roster.plugin_version)
            if check_active_package
            else CheckResult("degraded", "active package oracle was explicitly skipped")
        )
    except (OSError, ValueError) as error:
        checks["package"] = CheckResult("blocked", str(error))
        checks["executables"] = CheckResult("blocked", str(error))
        checks["active_package"] = CheckResult("blocked", "package or executable validation failed first")
    candidates = tuple(item.name for item in observation.namespace_candidates)
    return DiagnosticResult(
        action,
        _classification(checks, observation),
        roster.plugin_version if roster is not None else None,
        python_version,
        sys.platform,
        str(home),
        str(root),
        checks,
        observation.state,
        observation.targets,
        observation.recovery,
        candidates,
    )


def _canonical(value: object) -> bytes:
    """Encode one exact canonical JSON lifecycle payload."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _directory_value(path: Path, identity: DirectoryIdentity) -> dict[str, object]:
    """Project one held directory identity into lifecycle JSON fields."""
    return {
        "canonical_path": str(path),
        "device": identity.device,
        "inode": identity.inode,
        "owner": identity.owner,
        "group": identity.group,
        "mode": identity.mode,
    }


def _observed_identity_value(identity: object) -> dict[str, object]:
    """Project one validated observer root identity without accepting mappings."""
    return {
        "canonical_path": identity.canonical_path,
        "device": identity.device,
        "inode": identity.inode,
        "owner": identity.owner,
        "group": identity.group,
        "mode": identity.mode,
    }


def _state_bytes(
    plan: MutationPlan,
    *,
    target_path: Path,
    target_identity: DirectoryIdentity,
    state_path: Path,
    state_identity: DirectoryIdentity,
) -> bytes:
    """Build the exact current state or removed tombstone for one transaction."""
    home = plan.observation.codex_home_identity
    plugin = plan.observation.plugin_root_identity
    assert home is not None and plugin is not None
    roles = [
        {
            "role_id": role.role_id,
            "target_name": role.target_name,
            "card_path": role.card_path,
            "role_hash": role.role_hash,
            "file_hash": role.file_hash,
        }
        for role in plan.roster.roles
    ]
    rows = tuple((role.role_id, role.target_name, role.card_path, role.role_hash) for role in plan.roster.roles)
    value = {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": plan.candidate.install_id,
        "plugin_version": plan.roster.plugin_version,
        "package_hash": plan.roster.package_hash,
        "codex_home_identity": _observed_identity_value(home),
        "plugin_root_identity": _observed_identity_value(plugin),
        "state_root_identity": _directory_value(state_path, state_identity),
        "target_root_identity": _directory_value(target_path, target_identity),
        "roster_hash": roster_identity_hash(rows),
        "bootstrap": {
            "protocol": 1,
            "helper_path": "scripts/verify_role_link.py",
            "helper_hash": plan.roster.bootstrap_hash,
        },
        "generator_version": plan.roster.generator_version,
        "roles": roles,
        "transaction_status": "current" if plan.action == ManagerAction.INSTALL else "removed",
    }
    return _canonical(value)


def _journal(
    plan: MutationPlan,
    *,
    target_path: Path,
    target_identity: DirectoryIdentity,
    state_path: Path,
    state_identity: DirectoryIdentity,
    state_after: bytes,
) -> Journal:
    """Build the immutable PREPARING journal before staging any payload."""
    home = plan.observation.codex_home_identity
    assert home is not None and plan.approval is not None
    operations = []
    for operation in plan.candidate.operations:
        before_artifact = (
            f"before/{operation.role_id}.toml" if operation.intent in {"update", "remove", "retire"} else None
        )
        after_artifact = (
            f"after/{operation.role_id}.toml" if operation.intent in {"create", "repair-missing", "update"} else None
        )
        quarantine = (
            f"quarantine/{operation.role_id}.toml" if operation.intent in {"update", "remove", "retire"} else None
        )
        operations.append(
            {
                "role_id": operation.role_id,
                "intent": operation.intent,
                "target_name": operation.target_name,
                "before_exists": operation.before_exists,
                "before_hash": operation.before_hash,
                "before_mode": operation.before_mode,
                "after_exists": operation.after_exists,
                "after_hash": operation.after_hash,
                "after_mode": operation.after_mode,
                "before_image": before_artifact,
                "after_image": after_artifact,
                "quarantine_name": quarantine,
                "progress": "VERIFIED" if operation.intent == "noop" else "PLANNED",
                "rollback_progress": "NOT_STARTED",
            }
        )
    before_state = (
        {
            "exists": True,
            "relative_path": "state.before.json",
            "sha256": plan.candidate.prior_state_hash,
            "mode": "0600",
        }
        if plan.observation.state_payload is not None
        else {"exists": False, "relative_path": None, "sha256": None, "mode": None}
    )
    value = {
        "schema": 1,
        "transaction_id": plan.candidate.transaction_nonce,
        "transaction_nonce": plan.candidate.transaction_nonce,
        "install_id": plan.candidate.install_id,
        "action": plan.action.value,
        "approved_plan_digest": plan.approval.digest,
        "package_hash": plan.candidate.package_hash,
        "roster_hash": plan.candidate.roster_hash,
        "codex_home_identity": _observed_identity_value(home),
        "target_root_identity": _directory_value(target_path, target_identity),
        "state_root_identity": _directory_value(state_path, state_identity),
        "before_state": before_state,
        "after_state": {
            "exists": True,
            "relative_path": "state.after.json",
            "sha256": hashlib.sha256(state_after).hexdigest(),
            "mode": "0600",
        },
        "rollback_state_progress": "PENDING",
        "journal_state": "PREPARING",
        "operations": operations,
    }
    return validate_journal(value)


def plan_mutation(
    *,
    action: ManagerAction | str,
    codex_home: Path,
    plugin_root: Path,
    codex_binary: Path | None = None,
    require_active_package: bool = True,
    install_id: str | None = None,
    transaction_nonce: str | None = None,
) -> MutationPlan:
    """Build one complete read-only install/remove plan and approval digest."""
    if action not in _MUTATION_ACTIONS:
        raise ValueError("mutation action must be install or remove")
    action = ManagerAction(action)
    diagnostic = diagnose(
        action=ManagerAction.DOCTOR,
        codex_home=codex_home,
        plugin_root=plugin_root,
        codex_binary=codex_binary,
        check_active_package=require_active_package,
    )
    blocked = [name for name, check in diagnostic.checks.items() if check.status == "blocked"]
    if blocked or (require_active_package and diagnostic.classification != "healthy"):
        raise ValueError(f"doctor blocks mutation: {','.join(blocked) or diagnostic.classification}")
    home = codex_home.resolve(strict=True)
    root = plugin_root.resolve(strict=True)
    observation = observe_filesystem(codex_home=home, plugin_root=root)
    if observation.classification == "blocked":
        raise ValueError(f"filesystem blocks mutation: {observation.reason}")
    state = parse_state(observation.state_payload) if observation.state_payload is not None else None
    stable_install_id = install_id or (state["install_id"] if state is not None else str(uuid.uuid4()))
    stable_nonce = transaction_nonce or str(uuid.uuid4())
    python_path, python_hash = _digest_regular_executable(Path(sys.executable), "Python executable")
    codex_path, codex_hash = _digest_regular_executable(_codex_executable(codex_binary), "Codex executable")
    roster = load_generated_roster(
        root,
        install_id=stable_install_id,
        python_executable=python_path,
        python_executable_hash=python_hash,
        codex_binary=codex_path,
        codex_binary_hash=codex_hash,
    )
    candidate = build_candidate(
        action=action.value,
        mode="converge",
        roster=roster,
        state_payload=observation.state_payload,
        targets=dict(observation.target_observations),
        install_id=stable_install_id,
        transaction_nonce=stable_nonce,
    )
    approval = None
    if any(operation.intent != "noop" for operation in candidate.operations):
        approval = build_convergence_approval(
            candidate,
            roster,
            observation,
            RuntimeBinding(str(python_path), python_hash, str(codex_path), codex_hash, True),
        )
    return MutationPlan(
        action,
        home,
        root,
        python_path,
        python_hash,
        codex_path,
        codex_hash,
        roster,
        observation,
        candidate,
        approval,
    )


def _directory_entries(directory_fd: int) -> tuple[str, ...]:
    """List one already-validated bounded recovery directory."""
    names = []
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            names.append(entry.name)
            if len(names) > 512:
                raise ValueError("recovery directory is oversized")
    return tuple(sorted(names))


def _recovery_inventory(transaction_fd: int) -> tuple[dict[str, object], ...]:
    """Bind every exact recovery artifact without following links."""
    inventory: list[dict[str, object]] = []
    children = {"before", "after", "quarantine"}
    for name in _directory_entries(transaction_fd):
        if name in children:
            child_fd = open_directory_at(transaction_fd, name)
            try:
                identity = directory_identity(child_fd)
                inventory.append(
                    {
                        "path": name,
                        "kind": "directory",
                        "device": identity.device,
                        "inode": identity.inode,
                        "mode": identity.mode,
                    }
                )
                for artifact in _directory_entries(child_fd):
                    _, file_identity = read_regular_at(
                        child_fd,
                        artifact,
                        expected_mode=None,
                        expected_links=None,
                    )
                    inventory.append(
                        {
                            "path": f"{name}/{artifact}",
                            "kind": "file",
                            "sha256": file_identity.sha256,
                            "mode": file_identity.mode,
                            "links": file_identity.link_count,
                            "size": file_identity.size,
                        }
                    )
            finally:
                os.close(child_fd)
            continue
        _, identity = read_regular_at(
            transaction_fd,
            name,
            expected_mode=None,
            expected_links=None,
        )
        inventory.append(
            {
                "path": name,
                "kind": "file",
                "sha256": identity.sha256,
                "mode": identity.mode,
                "links": identity.link_count,
                "size": identity.size,
            }
        )
    return tuple(inventory)


def _recovery_transaction(codex_home: Path) -> tuple[str, Journal | None, tuple[dict[str, object], ...]]:
    """Open and inventory the sole observer-recognized transaction residue."""
    home_fd = os.open(codex_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    descriptors: list[int] = []
    try:
        codex_rig_fd = open_directory_at(home_fd, "codex-rig")
        state_fd = open_directory_at(codex_rig_fd, "shims")
        transactions_fd = open_directory_at(state_fd, "transactions")
        descriptors.extend((codex_rig_fd, state_fd, transactions_fd))
        names = _directory_entries(transactions_fd)
        if len(names) != 1:
            raise ValueError("exactly one recovery transaction is required")
        transaction_id = names[0]
        try:
            parsed = uuid.UUID(transaction_id)
        except ValueError as error:
            raise ValueError("invalid recovery transaction name") from error
        if str(parsed) != transaction_id or parsed.variant != uuid.RFC_4122:
            raise ValueError("invalid recovery transaction name")
        transaction_fd = open_directory_at(transactions_fd, transaction_id)
        descriptors.append(transaction_fd)
        inventory = _recovery_inventory(transaction_fd)
        try:
            payload, _ = read_regular_at(
                transaction_fd,
                "journal.json",
                expected_mode=0o600,
                expected_links=None,
            )
        except OSError as error:
            if not isinstance(error.__cause__, FileNotFoundError):
                raise
            journal = None
        else:
            journal = parse_journal(payload)
        return transaction_id, journal, inventory
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(home_fd)


def plan_recovery(*, action: ManagerAction | str, codex_home: Path, plugin_root: Path) -> RecoveryPlan | None:
    """Build an exact explicit-approval plan for recognized recovery residue."""
    if action not in _MUTATION_ACTIONS:
        raise ValueError("recovery action must be install or remove")
    action = ManagerAction(action)
    home = codex_home.resolve(strict=True)
    root = plugin_root.resolve(strict=True)
    observation = observe_filesystem(codex_home=home, plugin_root=root)
    if observation.recovery == "none":
        return None
    if observation.recovery not in {"empty-transaction", "preparing-residue", "journal"}:
        raise ValueError(f"recovery evidence is not safely actionable: {observation.recovery}")
    transaction_id, journal, inventory = _recovery_transaction(home)
    lock = observation.coordination_lock_observation
    assert lock is not None
    value = {
        "schema": 1,
        "action": action,
        "disposition": "finalize-or-rollback",
        "codex_home": str(home),
        "plugin_root": str(root),
        "transaction_id": transaction_id,
        "journal_hash": _journal_hash(journal) if journal is not None else None,
        "journal_state": journal.journal_state if journal is not None else None,
        "recovery_kind": observation.recovery,
        "lock": {
            "kind": lock.kind,
            "device": lock.device,
            "inode": lock.inode,
            "mode": lock.mode,
            "link_count": lock.link_count,
        },
        "inventory": inventory,
    }
    canonical = _canonical(value)
    return RecoveryPlan(
        action,
        home,
        root,
        observation,
        transaction_id,
        journal,
        inventory,
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def _journal_hash(journal: Journal) -> str:
    """Hash one canonical journal for recovery approval."""
    return hashlib.sha256(_canonical_journal(journal)).hexdigest()


def _cleanup_preparing(transaction_fd: int) -> None:
    """Delete only exact pre-mutation artifacts from a recognized transaction."""
    for child in ("before", "after", "quarantine"):
        try:
            child_fd = open_directory_at(transaction_fd, child)
        except OSError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                continue
            raise
        try:
            for name in _directory_entries(child_fd):
                _, identity = read_regular_at(child_fd, name, expected_mode=None, expected_links=None)
                unlink_verified_at(
                    child_fd,
                    name,
                    expected_hash=identity.sha256,
                    expected_mode=int(identity.mode, 8),
                    expected_links=identity.link_count,
                )
        finally:
            os.close(child_fd)
    for name in _directory_entries(transaction_fd):
        if name in {"before", "after", "quarantine"}:
            continue
        _, identity = read_regular_at(transaction_fd, name, expected_mode=None, expected_links=None)
        unlink_verified_at(
            transaction_fd,
            name,
            expected_hash=identity.sha256,
            expected_mode=int(identity.mode, 8),
            expected_links=identity.link_count,
        )
    remove_transaction_entries_at(
        transaction_fd,
        (("before", None, None), ("after", None, None), ("quarantine", None, None)),
        allow_absent=True,
    )


def apply_recovery(plan: RecoveryPlan, approved_digest: str) -> Journal | None:
    """Finalize, roll back, or clean one exact approved recovery transaction."""
    if not hmac.compare_digest(plan.digest, approved_digest):
        raise ValueError("recovery approval digest mismatch")
    home_fd = os.open(plan.codex_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lock = plan.observation.coordination_lock_observation
    assert lock is not None and lock.intent is not None
    expected_lock = (lock.device, lock.inode) if lock.kind == "regular" else None
    lock_fd = acquire_coordination_lock(home_fd, intent=lock.intent, expected_identity=expected_lock)
    descriptors: list[int] = []
    try:
        fresh = plan_recovery(action=plan.action, codex_home=plan.codex_home, plugin_root=plan.plugin_root)
        if (
            fresh is None
            or fresh.transaction_id != plan.transaction_id
            or fresh.inventory != plan.inventory
            or (fresh.journal is None) != (plan.journal is None)
            or (fresh.journal is not None and _canonical_journal(fresh.journal) != _canonical_journal(plan.journal))
        ):
            raise ValueError("recovery evidence changed under lock")
        codex_rig_fd = open_directory_at(home_fd, "codex-rig")
        state_fd = open_directory_at(codex_rig_fd, "shims")
        transactions_fd = open_directory_at(state_fd, "transactions")
        transaction_fd = open_directory_at(transactions_fd, plan.transaction_id)
        descriptors.extend((codex_rig_fd, state_fd, transactions_fd, transaction_fd))
        journal = fresh.journal
        if journal is None or journal.journal_state == "PREPARING":
            _cleanup_preparing(transaction_fd)
            terminal = None
        else:
            target_fd = open_directory_at(home_fd, "agents", private=False)
            before_fd = open_directory_at(transaction_fd, "before")
            after_fd = open_directory_at(transaction_fd, "after")
            quarantine_fd = open_directory_at(transaction_fd, "quarantine")
            descriptors.extend((target_fd, before_fd, after_fd, quarantine_fd))
            handles = TransactionDirectories(
                transaction_fd,
                target_fd,
                state_fd,
                before_fd,
                after_fd,
                quarantine_fd,
            )
            if journal.journal_state == "STATE_COMMITTED":
                terminal = finalize_state_committed(journal, handles)
            elif journal.journal_state == "COMMITTED" or journal.journal_state == "ROLLED_BACK":
                terminal = journal
            else:
                terminal = rollback_transaction(journal, handles)
            cleanup_transaction(terminal, handles)
        remove_transaction_entries_at(
            transactions_fd,
            ((plan.transaction_id, None, None),),
        )
        return terminal
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(lock_fd)
        os.close(home_fd)


def _revalidate_under_lock(plan: MutationPlan, lock_fd: int) -> FilesystemObservation:
    """Rebuild exact approval bytes while substituting only the acquired lock."""
    fresh = observe_filesystem(codex_home=plan.codex_home, plugin_root=plan.plugin_root)
    fresh_lock = fresh.coordination_lock_observation
    held = os.fstat(lock_fd)
    if (
        fresh.classification == "blocked"
        or fresh_lock is None
        or fresh_lock.kind != "regular"
        or (fresh_lock.device, fresh_lock.inode) != (held.st_dev, held.st_ino)
    ):
        raise ValueError("under-lock filesystem observation changed")
    trusted = replace(fresh, coordination_lock_observation=plan.observation.coordination_lock_observation)
    rebuilt = build_candidate(
        action=plan.action.value,
        mode="converge",
        roster=plan.roster,
        state_payload=trusted.state_payload,
        targets=dict(trusted.target_observations),
        install_id=plan.candidate.install_id,
        transaction_nonce=plan.candidate.transaction_nonce,
    )
    if rebuilt != plan.candidate:
        raise ValueError("candidate changed under lock")
    assert plan.approval is not None
    approval = build_convergence_approval(
        rebuilt,
        plan.roster,
        trusted,
        RuntimeBinding(
            str(plan.python_path),
            plan.python_hash,
            str(plan.codex_path),
            plan.codex_hash,
            True,
        ),
    )
    if not hmac.compare_digest(approval.canonical_bytes, plan.approval.canonical_bytes):
        raise ValueError("approval changed under lock")
    return trusted


def _stage_transaction(
    plan: MutationPlan,
    handles: TransactionDirectories,
    *,
    target_path: Path,
    target_identity: DirectoryIdentity,
    state_path: Path,
    state_identity: DirectoryIdentity,
) -> Journal:
    """Write all journal-bound artifacts before marking the transaction prepared."""
    state_after = _state_bytes(
        plan,
        target_path=target_path,
        target_identity=target_identity,
        state_path=state_path,
        state_identity=state_identity,
    )
    journal = _journal(
        plan,
        target_path=target_path,
        target_identity=target_identity,
        state_path=state_path,
        state_identity=state_identity,
        state_after=state_after,
    )
    write_initial_journal(handles.transaction_fd, _canonical_journal(journal))
    if plan.observation.state_payload is not None:
        write_exclusive_at(handles.transaction_fd, "state.before.json", plan.observation.state_payload)
    write_exclusive_at(handles.transaction_fd, "state.after.json", state_after)
    generated = {role.role_id: role for role in plan.roster.roles}
    for operation in plan.candidate.operations:
        artifact = f"{operation.role_id}.toml"
        if operation.intent in {"update", "remove", "retire"}:
            payload, identity = read_regular_at(
                handles.target_fd,
                operation.target_name,
                expected_mode=0o600,
                expected_links=1,
            )
            if identity.sha256 != operation.before_hash:
                raise ValueError("before-image changed during staging")
            write_exclusive_at(handles.before_fd, artifact, payload)
        if operation.intent in {"create", "repair-missing", "update"}:
            role = generated.get(operation.role_id)
            if role is None or role.file_hash != operation.after_hash:
                raise ValueError("after-image is absent from active roster")
            write_exclusive_at(handles.after_fd, artifact, role.shim_bytes)
    return mark_transaction_prepared(journal, handles.transaction_fd)


def _canonical_journal(journal: Journal) -> bytes:
    """Import-locally project one journal through its canonical encoder."""
    from _agent_shim_journal import canonical_journal_bytes

    return canonical_journal_bytes(journal)


def apply_mutation(
    plan: MutationPlan,
    approved_digest: str,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> Journal | None:
    """Apply one exact approved plan under lock and bounded transaction roots."""
    if plan.approval is None:
        return None
    if not hmac.compare_digest(plan.approval.digest, approved_digest):
        raise ValueError("approval digest mismatch")
    home_fd = os.open(plan.codex_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lock = plan.observation.coordination_lock_observation
    assert lock is not None and lock.intent is not None
    expected_lock = (lock.device, lock.inode) if lock.kind == "regular" else None
    lock_fd = acquire_coordination_lock(home_fd, intent=lock.intent, expected_identity=expected_lock)
    descriptors: list[int] = []
    try:
        _revalidate_under_lock(plan, lock_fd)
        target_fd, _ = create_directory_at(home_fd, "agents", private=False)
        state_fd, _ = create_private_path(home_fd, ("codex-rig", "shims"))
        descriptors.extend((target_fd, state_fd))
        transactions_fd, _ = create_directory_at(state_fd, "transactions")
        transaction_fd, created = create_directory_at(transactions_fd, plan.candidate.transaction_nonce)
        descriptors.extend((transactions_fd, transaction_fd))
        if not created:
            raise ValueError("transaction nonce already exists")
        before_fd, _ = create_directory_at(transaction_fd, "before")
        after_fd, _ = create_directory_at(transaction_fd, "after")
        quarantine_fd, _ = create_directory_at(transaction_fd, "quarantine")
        descriptors.extend((before_fd, after_fd, quarantine_fd))
        handles = TransactionDirectories(
            transaction_fd,
            target_fd,
            state_fd,
            before_fd,
            after_fd,
            quarantine_fd,
        )
        target_path = plan.codex_home / "agents"
        state_path = plan.codex_home / "codex-rig" / "shims"
        prepared = _stage_transaction(
            plan,
            handles,
            target_path=target_path,
            target_identity=directory_identity(target_fd, private=False),
            state_path=state_path,
            state_identity=directory_identity(state_fd),
        )
        try:
            terminal = apply_transaction(prepared, handles, checkpoint=checkpoint)
        except TransactionError as error:
            if error.journal.journal_state == "ROLLED_BACK":
                cleanup_transaction(error.journal, handles)
                remove_transaction_entries_at(
                    transactions_fd,
                    ((plan.candidate.transaction_nonce, None, None),),
                )
            raise
        cleanup_transaction(terminal, handles)
        remove_transaction_entries_at(
            transactions_fd,
            ((plan.candidate.transaction_nonce, None, None),),
        )
        return terminal
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        os.close(lock_fd)
        os.close(home_fd)


def _encode(result: DiagnosticResult) -> str:
    """Encode one deterministic machine-readable diagnostic result."""
    return json.dumps(asdict(result), ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the exact one-action public manager grammar."""
    parser = ManagerArgumentParser(prog="agent-shims", add_help=True)
    parser.add_argument("action", choices=[a.value for a in ManagerAction])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the public manager action with stable exit codes and JSON output."""
    try:
        arguments = parse_args(argv)
    except ValueError as error:
        print(json.dumps({"classification": "usage-error", "detail": str(error)}, sort_keys=True))
        return 2
    except SystemExit as error:
        return int(error.code)
    # argparse already gated the value against the enum's own choices, so this cannot raise.
    action = ManagerAction(arguments.action)
    if not sys.platform.startswith(SUPPORTED_PLATFORMS) and sys.platform != "win32":
        print(
            json.dumps(
                {
                    "action": action.value,
                    "classification": "blocked",
                    "detail": f"platform {sys.platform}; native Windows and unknown POSIX hosts are unsupported",
                    "platform": sys.platform,
                    "writes": 0,
                },
                sort_keys=True,
            )
        )
        return 0 if action in _DIAGNOSTIC_ACTIONS else 5
    home_value = os.environ.get("CODEX_HOME")
    codex_home = Path(home_value) if home_value else Path.home() / ".codex"
    plugin_root = Path(__file__).resolve().parents[1]
    # Only remove may recover prior transactions while new named-agent activation is unsupported.
    if action == ManagerAction.INSTALL:
        print(
            json.dumps(
                {
                    "action": "install",
                    "classification": "platform-blocked",
                    "detail": "active Codex collaboration has no explicit custom-agent selector",
                    "writes": 0,
                },
                sort_keys=True,
            )
        )
        return 5
    if action == ManagerAction.REMOVE and sys.platform == "win32":
        print(
            json.dumps(
                {
                    "action": "remove",
                    "classification": "platform-blocked",
                    "detail": "persistent shim mutation requires the POSIX lifecycle backend",
                    "writes": 0,
                },
                sort_keys=True,
            )
        )
        return 5
    if action in _MUTATION_ACTIONS:
        try:
            recovery = plan_recovery(
                action=action,
                codex_home=codex_home,
                plugin_root=plugin_root,
            )
        except (OSError, ValueError) as error:
            print(
                json.dumps(
                    {"action": action.value, "classification": "blocked", "detail": str(error)},
                    sort_keys=True,
                )
            )
            return 6
        if recovery is not None:
            print(
                json.dumps(
                    {
                        "action": action.value,
                        "approval_digest": recovery.digest,
                        "classification": "recovery-required",
                        "journal_state": recovery.journal.journal_state if recovery.journal is not None else None,
                        "transaction_id": recovery.transaction_id,
                    },
                    sort_keys=True,
                )
            )
            try:
                approved = input("Type the approval digest to recover this transaction: ").strip()
            except EOFError:
                approved = ""
            if not hmac.compare_digest(approved, recovery.digest):
                print(json.dumps({"action": action.value, "classification": "cancelled"}, sort_keys=True))
                return 3
            try:
                terminal = apply_recovery(recovery, approved)
            except (OSError, ValueError, TransactionError) as error:
                print(
                    json.dumps(
                        {"action": action.value, "classification": "recovery-required", "detail": str(error)},
                        sort_keys=True,
                    )
                )
                return 7
            print(
                json.dumps(
                    {
                        "action": action.value,
                        "classification": "recovered",
                        "journal_state": terminal.journal_state if terminal is not None else None,
                        "repeat_action_required": True,
                    },
                    sort_keys=True,
                )
            )
            return 0
        try:
            plan = plan_mutation(
                action=action,
                codex_home=codex_home,
                plugin_root=plugin_root,
            )
        except (OSError, ValueError) as error:
            print(
                json.dumps(
                    {"action": action.value, "classification": "blocked", "detail": str(error)},
                    sort_keys=True,
                )
            )
            return 5
        if plan.approval is None:
            print(json.dumps({"action": action.value, "classification": "complete", "writes": 0}, sort_keys=True))
            return 0
        intents = [
            {"role_id": operation.role_id, "intent": operation.intent}
            for operation in plan.candidate.operations
            if operation.intent != "noop"
        ]
        print(
            json.dumps(
                {
                    "action": action.value,
                    "approval_digest": plan.approval.digest,
                    "canonical_target_root": str(plan.codex_home / "agents"),
                    "plugin_root": str(plan.plugin_root),
                    "operations": intents,
                },
                sort_keys=True,
            )
        )
        try:
            approved = input("Type the approval digest to apply this whole-roster plan: ").strip()
        except EOFError:
            approved = ""
        if not hmac.compare_digest(approved, plan.approval.digest):
            print(json.dumps({"action": action.value, "classification": "cancelled"}, sort_keys=True))
            return 3
        try:
            terminal = apply_mutation(plan, approved)
        except ValueError as error:
            print(
                json.dumps(
                    {"action": action.value, "classification": "drift", "detail": str(error)},
                    sort_keys=True,
                )
            )
            return 4
        except (OSError, TransactionError) as error:
            print(
                json.dumps(
                    {"action": action.value, "classification": "recovery-required", "detail": str(error)},
                    sort_keys=True,
                )
            )
            return 7
        print(
            json.dumps(
                {
                    "action": action.value,
                    "classification": "complete",
                    "journal_state": terminal.journal_state if terminal is not None else None,
                    "fresh_session_required": True,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        result = diagnose(action=action, codex_home=codex_home, plugin_root=plugin_root)
    except (OSError, ValueError) as error:
        print(json.dumps({"action": action.value, "classification": "blocked", "detail": str(error)}, sort_keys=True))
        return 5
    print(_encode(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
