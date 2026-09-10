"""Acceptance checks for complete no-write convergence approval binding."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
OBSERVER_PATH = SCRIPTS / "_agent_shim_observe.py"
JOURNAL_PATH = SCRIPTS / "_agent_shim_journal.py"
PLAN_PATH = SCRIPTS / "_agent_shim_plan.py"
APPROVAL_PATH = SCRIPTS / "_agent_shim_approval.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
NONCE = "123e4567-e89b-42d3-a456-426614174001"
DIGEST = "a" * 64


def _load_script(path: Path, name: str) -> ModuleType:
    """Load one installed script with exact sibling module names."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _modules() -> tuple[ModuleType, ModuleType, ModuleType, ModuleType]:
    """Load the approval kernel and its direct value authorities."""
    _load_script(GENERATOR_PATH, "generate_roles")
    lifecycle = _load_script(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    _load_script(JOURNAL_PATH, "_agent_shim_journal")
    observer = _load_script(OBSERVER_PATH, "_agent_shim_observe")
    planner = _load_script(PLAN_PATH, "_agent_shim_plan")
    approval = _load_script(APPROVAL_PATH, "codex_rig_agent_shim_approval")
    return lifecycle, observer, planner, approval


def _generated_roster(generator: ModuleType) -> object:
    """Build one complete immutable generated roster fixture."""
    roles = []
    for role_id in generator.ROLE_IDS:
        payload = f"shim:{role_id}\n".encode()
        roles.append(
            generator.GeneratedRole(
                role_id,
                f"codex-rig-{role_id}.toml",
                f"roles/{role_id}/ROLE.md",
                "b" * 64,
                payload,
                hashlib.sha256(payload).hexdigest(),
            )
        )
    return generator.GeneratedRoster("0.2.0", DIGEST, "c" * 64, 1, tuple(roles))


def _root(observer: ModuleType, path: str, inode: int) -> object:
    """Build one exact directory identity."""
    return observer.RootIdentity(path, 1, inode, 501, 20, "0700")


def _root_observation(observer: ModuleType, path: str, inode: int, *, exists: bool) -> object:
    """Build one exact existing or single-component absent root observation."""
    home_path = path.rsplit("/", maxsplit=1)[0]
    ancestor = _root(observer, path if exists else home_path, inode if exists else 1)
    identity = ancestor if exists else None
    suffix = () if exists else (path.rsplit("/", maxsplit=1)[1],)
    return observer.RootObservation(exists, path, ancestor, suffix, identity)


def _candidate_and_observation(
    loaded: tuple[ModuleType, ModuleType, ModuleType, ModuleType],
    *,
    action: str = "install",
    roots_exist: bool = False,
) -> tuple[object, object, object]:
    """Build matching candidate operations and immutable filesystem evidence."""
    lifecycle, observer, planner, _ = loaded
    generator = sys.modules["generate_roles"]
    roster = _generated_roster(generator)
    targets = {role.target_name: lifecycle.TargetObservation("absent") for role in roster.roles}
    candidate = planner.build_candidate(
        action=action,
        mode="converge",
        roster=roster,
        state_payload=None,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=NONCE,
    )
    home_identity = _root(observer, "/codex", 1)
    home = observer.RootObservation(True, "/codex", home_identity, (), home_identity)
    target = _root_observation(observer, "/codex/agents", 2, exists=roots_exist)
    state = _root_observation(observer, "/codex/codex-rig/shims", 3, exists=roots_exist)
    plugin = _root(observer, "/plugin", 4)
    lock = observer.LockObservation("absent", "/codex/.codex-rig-shims.lock", None, None, None, None, None, None, None)
    observation = observer.FilesystemObservation(
        "degraded",
        "runtime and mutation prerequisites remain unverified",
        "absent",
        "absent",
        "none",
        home,
        plugin,
        target,
        state,
        lock,
        None,
        tuple(targets.items()),
        (),
        "complete",
    )
    return roster, candidate, observation


def _runtime(approval: ModuleType, *, eligible: bool = True) -> object:
    """Build one doctor-supplied executable binding fixture."""
    return approval.RuntimeBinding("/usr/bin/python3", "d" * 64, "/usr/bin/codex", "e" * 64, eligible)


def _removed_candidate_and_observation(
    loaded: tuple[ModuleType, ModuleType, ModuleType, ModuleType],
    *,
    target_inode: int,
) -> tuple[object, object, object]:
    """Build a removed tombstone and one current empty target-root identity."""
    lifecycle, observer, planner, _ = loaded
    generator = sys.modules["generate_roles"]
    roster = _generated_roster(generator)
    home_identity = _root(observer, "/codex", 1)
    plugin_identity = _root(observer, "/plugin", 4)
    state_identity = _root(observer, "/codex/codex-rig/shims", 3)
    persisted_target = _root(observer, "/codex/agents", 2)
    roles = [
        {
            "role_id": role.role_id,
            "target_name": role.target_name,
            "card_path": role.card_path,
            "role_hash": role.role_hash,
            "file_hash": role.file_hash,
        }
        for role in roster.roles
    ]
    state = {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": roster.plugin_version,
        "package_hash": roster.package_hash,
        "codex_home_identity": vars(home_identity),
        "plugin_root_identity": vars(plugin_identity),
        "state_root_identity": vars(state_identity),
        "target_root_identity": vars(persisted_target),
        "roster_hash": generator.roster_identity_hash(
            tuple((role.role_id, role.target_name, role.card_path, role.role_hash) for role in roster.roles)
        ),
        "bootstrap": {
            "protocol": 1,
            "helper_path": "scripts/verify_role_link.py",
            "helper_hash": roster.bootstrap_hash,
        },
        "generator_version": roster.generator_version,
        "roles": roles,
        "transaction_status": "removed",
    }
    state_payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    targets = {role.target_name: lifecycle.TargetObservation("absent") for role in roster.roles}
    candidate = planner.build_candidate(
        action="install",
        mode="converge",
        roster=roster,
        state_payload=state_payload,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=NONCE,
    )
    current_target = _root(observer, "/codex/agents", target_inode)
    observation = observer.FilesystemObservation(
        "degraded",
        "runtime and mutation prerequisites remain unverified",
        "removed",
        "removed",
        "none",
        observer.RootObservation(True, "/codex", home_identity, (), home_identity),
        plugin_identity,
        observer.RootObservation(True, "/codex/agents", current_target, (), current_target),
        observer.RootObservation(True, "/codex/codex-rig/shims", state_identity, (), state_identity),
        observer.LockObservation(
            "absent",
            "/codex/.codex-rig-shims.lock",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        state_payload,
        tuple(targets.items()),
        (),
        "complete",
    )
    return roster, candidate, observation


def _migration_candidate_and_observation(
    loaded: tuple[ModuleType, ModuleType, ModuleType, ModuleType],
) -> tuple[object, object, object, dict[str, object]]:
    """Build a forward migration from a different cached plugin root."""
    lifecycle, observer, planner, _ = loaded
    generator = sys.modules["generate_roles"]
    roster = _generated_roster(generator)
    home_identity = _root(observer, "/codex", 1)
    target_identity = _root(observer, "/codex/agents", 2)
    state_identity = _root(observer, "/codex/codex-rig/shims", 3)
    prior_plugin_identity = _root(observer, "/plugin-cache/prior", 4)
    active_plugin_identity = _root(observer, "/plugin-cache/active", 5)
    roles = [
        {
            "role_id": role.role_id,
            "target_name": role.target_name,
            "card_path": role.card_path,
            "role_hash": role.role_hash,
            "file_hash": role.file_hash,
        }
        for role in roster.roles
    ]
    retired = {
        "role_id": "retired-specialist",
        "target_name": "codex-rig-retired-specialist.toml",
        "card_path": "roles/retired-specialist/ROLE.md",
        "role_hash": "d" * 64,
        "file_hash": "e" * 64,
    }
    roles.append(retired)
    roles.sort(key=lambda role: role["role_id"])
    prior_package_hash = "f" * 64
    state = {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": "0.1.0",
        "package_hash": prior_package_hash,
        "codex_home_identity": vars(home_identity),
        "plugin_root_identity": vars(prior_plugin_identity),
        "state_root_identity": vars(state_identity),
        "target_root_identity": vars(target_identity),
        "roster_hash": hashlib.sha256(
            json.dumps(
                [{key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "bootstrap": {
            "protocol": 1,
            "helper_path": "scripts/verify_role_link.py",
            "helper_hash": "9" * 64,
        },
        "generator_version": 1,
        "roles": roles,
        "transaction_status": "current",
    }
    state_payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    targets = {
        role["target_name"]: lifecycle.TargetObservation(
            "regular",
            role["file_hash"],
            lifecycle.Marker(INSTALL_ID, role["role_id"], prior_package_hash, role["role_hash"]),
        )
        for role in roles
    }
    targets = dict(sorted(targets.items()))
    candidate = planner.build_candidate(
        action="install",
        mode="converge",
        roster=roster,
        state_payload=state_payload,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=NONCE,
    )
    observation = observer.FilesystemObservation(
        "degraded",
        "runtime and mutation prerequisites remain unverified",
        "current",
        "current",
        "none",
        observer.RootObservation(True, "/codex", home_identity, (), home_identity),
        active_plugin_identity,
        observer.RootObservation(True, "/codex/agents", target_identity, (), target_identity),
        observer.RootObservation(True, "/codex/codex-rig/shims", state_identity, (), state_identity),
        observer.LockObservation(
            "absent",
            "/codex/.codex-rig-shims.lock",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        state_payload,
        tuple(targets.items()),
        (),
        "complete",
    )
    return roster, candidate, observation, state


def test_approval_has_exact_contract_fields_and_deterministic_digest() -> None:
    """Freeze complete canonical convergence bytes from matching evidence."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)

    first = approval.build_convergence_approval(candidate, roster, observation, _runtime(approval))
    second = approval.build_convergence_approval(candidate, roster, observation, _runtime(approval))
    value = json.loads(first.canonical_bytes)

    assert first == second
    assert set(vars(first)) == {"canonical_bytes", "digest"}
    assert first.digest == hashlib.sha256(first.canonical_bytes).hexdigest()
    assert not first.canonical_bytes.endswith(b"\n")
    assert set(value) == {
        "schema",
        "action",
        "scope",
        "mode",
        "target_root_intent",
        "recovery_disposition",
        "codex_home_observation",
        "coordination_lock_intent",
        "coordination_lock_observation",
        "target_root_observation",
        "state_root_observation",
        "canonical_target_root",
        "canonical_state_root",
        "canonical_plugin_root",
        "plugin_root_identity",
        "plugin_version",
        "package_hash",
        "bootstrap_protocol",
        "bootstrap_path",
        "bootstrap_hash",
        "python_executable_path",
        "python_executable_hash",
        "codex_binary_path",
        "codex_binary_hash",
        "generator_version",
        "install_id",
        "transaction_nonce",
        "roster_hash",
        "source_state",
        "journal_observation",
        "recovery_observation",
        "operations",
    }
    assert value["target_root_intent"] == "create"
    assert value["source_state"] is None
    assert set(value["operations"][0]) == {
        "role_id",
        "target_name",
        "before_card_path",
        "before_role_hash",
        "after_card_path",
        "after_role_hash",
        "before_exists",
        "before_hash",
        "before_mode",
        "after_exists",
        "after_hash",
        "after_mode",
        "intent",
    }
    assert value["coordination_lock_intent"] == "create-if-absent"
    assert value["coordination_lock_observation"] == {
        "kind": "absent",
        "canonical_path": "/codex/.codex-rig-shims.lock",
        "device": None,
        "inode": None,
        "owner": None,
        "group": None,
        "mode": None,
        "link_count": None,
        "size": None,
    }
    assert value["target_root_observation"]["missing_suffix_components"] == ["agents"]
    assert value["recovery_observation"] == {
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
    }
    approval.revalidate_approval_digest(first, first.digest)


def test_existing_roots_remain_unchanged_intent() -> None:
    """Do not request target-root creation when the exact root exists."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded, roots_exist=True)

    value = json.loads(
        approval.build_convergence_approval(candidate, roster, observation, _runtime(approval)).canonical_bytes
    )

    assert value["target_root_intent"] == "unchanged"
    assert value["target_root_observation"]["missing_suffix_components"] == []


@pytest.mark.parametrize(
    ("target_inode", "expected"),
    [pytest.param(2, "unchanged", id="same-root"), pytest.param(99, "rebind-removed-root", id="replaced-root")],
)
def test_removed_tombstone_binds_target_root_identity(target_inode: int, expected: str) -> None:
    """Require explicit rebind approval when an empty removed root was replaced."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation = _removed_candidate_and_observation(loaded, target_inode=target_inode)

    plan = approval.build_convergence_approval(candidate, roster, observation, _runtime(approval))

    assert json.loads(plan.canonical_bytes)["target_root_intent"] == expected


def test_forward_cache_migration_binds_exact_source_and_historical_union() -> None:
    """Bind prior package evidence while allowing the active cache root to differ."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation, state = _migration_candidate_and_observation(loaded)

    plan = approval.build_convergence_approval(candidate, roster, observation, _runtime(approval))
    value = json.loads(plan.canonical_bytes)

    assert candidate.transition == "forward-upgrade"
    assert value["canonical_plugin_root"] == "/plugin-cache/active"
    assert value["source_state"] == {
        "sha256": hashlib.sha256(observation.state_payload).hexdigest(),
        "transition": "forward-upgrade",
        "plugin_version": "0.1.0",
        "package_hash": "f" * 64,
        "bootstrap": state["bootstrap"],
        "generator_version": 1,
        "roster_hash": state["roster_hash"],
        "plugin_root_identity": state["plugin_root_identity"],
    }
    retired = next(operation for operation in value["operations"] if operation["role_id"] == "retired-specialist")
    assert retired == {
        "role_id": "retired-specialist",
        "target_name": "codex-rig-retired-specialist.toml",
        "before_card_path": "roles/retired-specialist/ROLE.md",
        "before_role_hash": "d" * 64,
        "after_card_path": None,
        "after_role_hash": None,
        "before_exists": True,
        "before_hash": "e" * 64,
        "before_mode": "0600",
        "after_exists": False,
        "after_hash": None,
        "after_mode": None,
        "intent": "retire",
    }
    retained = next(operation for operation in value["operations"] if operation["role_id"] == "challenger")
    assert retained["before_card_path"] == "roles/challenger/ROLE.md"
    assert retained["before_role_hash"] == "b" * 64
    assert retained["after_card_path"] == "roles/challenger/ROLE.md"
    assert retained["after_role_hash"] == "b" * 64


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        pytest.param("sha256", "0" * 64, id="sha256"),
        pytest.param("transition", "development-rebuild", id="transition"),
        pytest.param("plugin_version", "0.1.1", id="plugin_version"),
        pytest.param("package_hash", "0" * 64, id="package_hash"),
        pytest.param(
            "bootstrap",
            {"protocol": 1, "helper_path": "scripts/verify_role_link.py", "helper_hash": "0" * 64},
            id="bootstrap",
        ),
        pytest.param("generator_version", 2, id="generator_version"),
        pytest.param("roster_hash", "0" * 64, id="roster_hash"),
        pytest.param(
            "plugin_root_identity",
            {
                "canonical_path": "/plugin-cache/other",
                "device": 1,
                "inode": 6,
                "owner": 501,
                "group": 20,
                "mode": "0700",
            },
            id="plugin_root_identity",
        ),
    ],
)
def test_each_source_state_field_changes_approval_digest(field: str, replacement: object) -> None:
    """Make every prior-state approval field independently digest-significant."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation, _ = _migration_candidate_and_observation(loaded)
    plan = approval.build_convergence_approval(candidate, roster, observation, _runtime(approval))
    value = json.loads(plan.canonical_bytes)
    value["source_state"][field] = replacement
    changed = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    assert hashlib.sha256(changed).hexdigest() != plan.digest


def test_historical_target_union_must_remain_complete() -> None:
    """Reject approval when a retired prior target disappears from observations."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation, _ = _migration_candidate_and_observation(loaded)
    incomplete = tuple(
        item for item in observation.target_observations if item[0] != "codex-rig-retired-specialist.toml"
    )

    with pytest.raises(approval.ApprovalBindingError, match="rebuilt|rosters"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, target_observations=incomplete),
            _runtime(approval),
        )


@pytest.mark.parametrize("status", ["unavailable", "overflow", "malformed", "unreadable"])
def test_namespace_inventory_must_be_complete_and_empty(status: str) -> None:
    """Reject each incomplete namespace scan status."""
    loaded = _modules()
    _, observer, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)

    with pytest.raises(approval.ApprovalBindingError, match="approval-eligible"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, namespace_inventory_status=status),
            _runtime(approval),
        )


def test_namespace_inventory_rejects_unmanaged_namespace_evidence() -> None:
    """Reject a complete scan that still reports an unmanaged namespace entry."""
    loaded = _modules()
    _, observer, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)

    candidate_evidence = (observer.NamespaceCandidateObservation("codex-rig-retired.toml", "regular"),)
    with pytest.raises(approval.ApprovalBindingError, match="approval-eligible"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, namespace_candidates=candidate_evidence),
            _runtime(approval),
        )


def test_blocked_stale_or_unhealthy_evidence_cannot_emit_approval() -> None:
    """Reject noneligible doctor, observer, target, and zero-write candidates."""
    loaded = _modules()
    lifecycle, _, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)
    with pytest.raises(approval.ApprovalBindingError):
        approval.build_convergence_approval(candidate, roster, observation, _runtime(approval, eligible=False))

    blocked = replace(observation, classification="blocked")
    with pytest.raises(approval.ApprovalBindingError):
        approval.build_convergence_approval(candidate, roster, blocked, _runtime(approval))

    stale_targets = list(observation.target_observations)
    stale_targets[0] = (stale_targets[0][0], lifecycle.TargetObservation("regular", DIGEST))
    stale = replace(observation, target_observations=tuple(stale_targets))
    with pytest.raises(approval.ApprovalBindingError):
        approval.build_convergence_approval(candidate, roster, stale, _runtime(approval))

    remove_roster, noop, clean = _candidate_and_observation(loaded, action="remove")
    assert all(operation.intent == "noop" for operation in noop.operations)
    with pytest.raises(approval.ApprovalBindingError, match="zero-write"):
        approval.build_convergence_approval(noop, remove_roster, clean, _runtime(approval))


def test_inconsistent_root_evidence_cannot_emit_approval() -> None:
    """Reject forged root metadata and ancestry before canonicalization."""
    loaded = _modules()
    _, observer, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)

    bad_identity = replace(observation.codex_home_observation.identity, owner=True)
    bad_home = replace(
        observation.codex_home_observation,
        nearest_existing_ancestor=bad_identity,
        identity=bad_identity,
    )
    with pytest.raises(approval.ApprovalBindingError, match="root owner"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, codex_home_observation=bad_home),
            _runtime(approval),
        )

    bad_target = observer.RootObservation(
        False,
        "/codex/agents",
        observation.codex_home_observation.identity,
        ("other",),
        None,
    )
    with pytest.raises(approval.ApprovalBindingError, match="ancestry"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, target_root_observation=bad_target),
            _runtime(approval),
        )


def test_forged_state_classification_or_lock_cannot_emit_approval() -> None:
    """Reject classification claims and lock evidence not proven by observation."""
    loaded = _modules()
    _, observer, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)

    with pytest.raises(approval.ApprovalBindingError, match="classification"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, targets="current"),
            _runtime(approval),
        )

    wrong_lock = observer.LockObservation(
        "absent",
        "/codex/other.lock",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    with pytest.raises(approval.ApprovalBindingError, match="lock path"):
        approval.build_convergence_approval(
            candidate,
            roster,
            replace(observation, coordination_lock_observation=wrong_lock),
            _runtime(approval),
        )

    existing_lock = observer.LockObservation(
        "regular",
        "/codex/.codex-rig-shims.lock",
        1,
        9,
        501,
        20,
        "0600",
        1,
        0,
    )
    bound = approval.build_convergence_approval(
        candidate,
        roster,
        replace(observation, coordination_lock_observation=existing_lock),
        _runtime(approval),
    )
    assert json.loads(bound.canonical_bytes)["coordination_lock_observation"] == {
        "kind": "regular",
        "canonical_path": "/codex/.codex-rig-shims.lock",
        "device": 1,
        "inode": 9,
        "owner": 501,
        "group": 20,
        "mode": "0600",
        "link_count": 1,
        "size": 0,
    }


def test_semantically_forged_candidate_cannot_emit_approval() -> None:
    """Reject self-consistent bytes whose action contradicts their operations."""
    loaded = _modules()
    _, _, _, approval = loaded
    roster, candidate, observation = _candidate_and_observation(loaded)
    value = json.loads(candidate.canonical_bytes)
    value["action"] = "remove"
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    forged = replace(
        candidate,
        action="remove",
        canonical_bytes=canonical,
        digest=hashlib.sha256(canonical).hexdigest(),
    )

    with pytest.raises(approval.ApprovalBindingError, match="authoritative rebuild"):
        approval.build_convergence_approval(forged, roster, observation, _runtime(approval))


def test_approval_kernel_has_no_filesystem_or_process_surface() -> None:
    """Keep approval binding structurally incapable of external effects."""
    tree = ast.parse(APPROVAL_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported.update((node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

    assert imported.isdisjoint({"os", "pathlib", "shutil", "subprocess", "tempfile"})
    assert calls.isdisjoint({"open", "exec", "eval", "compile", "input"})
