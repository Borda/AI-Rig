"""Acceptance checks for the pure shim approval kernel."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
PLAN_PATH = SCRIPTS / "_agent_shim_plan.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
TRANSACTION_NONCE = "123e4567-e89b-42d3-a456-426614174001"
PACKAGE_HASH = "a" * 64
ROLE_HASH = "b" * 64
BOOTSTRAP_HASH = "c" * 64


def _load_module(path: Path, name: str) -> ModuleType:
    """Load sibling scripts without package installation assumptions."""
    if name in {"generate_roles", "_agent_shim_lifecycle"} and name in sys.modules:
        return sys.modules[name]
    if path != GENERATOR_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    if path == PLAN_PATH and "_agent_shim_lifecycle" not in sys.modules:
        _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    """Encode the public canonical JSON representation.

    Example:
        >>> _canonical({"b": 2, "a": 1})
        b'{"a":1,"b":2}'
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _role_ids() -> tuple[str, ...]:
    """Return the generator-owned exact role roster."""
    return _load_module(GENERATOR_PATH, "codex_rig_plan_roster").ROLE_IDS


def _generated_roster(
    overrides: dict[str, bytes] | None = None,
    *,
    version: str = "0.2.0",
    package_hash: str = PACKAGE_HASH,
) -> object:
    """Build one immutable generated roster fixture."""
    generator = _load_module(GENERATOR_PATH, "generate_roles")
    replacements = overrides or {}
    roles = []
    for role_id in _role_ids():
        payload = replacements.get(role_id, f"generated:{role_id}\n".encode())
        roles.append(
            generator.GeneratedRole(
                role_id=role_id,
                target_name=f"codex-rig-{role_id}.toml",
                card_path=f"roles/{role_id}/ROLE.md",
                role_hash=ROLE_HASH,
                shim_bytes=payload,
                file_hash=hashlib.sha256(payload).hexdigest(),
            )
        )
    return generator.GeneratedRoster(version, package_hash, BOOTSTRAP_HASH, 1, tuple(roles))


def _root_identity(path: str) -> dict[str, object]:
    """Build one exact persisted root identity.

    Example:
        >>> _root_identity("/fixture")["mode"]
        '0700'
    """
    return {"canonical_path": path, "device": 1, "inode": 2, "owner": 3, "group": 4, "mode": "0700"}


def _validated_state(roster: object, *, missing: set[str] | None = None) -> tuple[dict[str, object], dict[str, object]]:
    """Build parsed current state and its exact target observations."""
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    missing_roles = missing or set()
    state_roles = []
    targets = {}
    for role in roster.roles:
        old_payload = f"before:{role.role_id}\n".encode() if role.role_id == "cicd-steward" else role.shim_bytes
        old_hash = hashlib.sha256(old_payload).hexdigest()
        state_roles.append(
            {
                "role_id": role.role_id,
                "target_name": role.target_name,
                "card_path": role.card_path,
                "role_hash": role.role_hash,
                "file_hash": old_hash,
            }
        )
        targets[role.target_name] = (
            lifecycle.TargetObservation("absent")
            if role.role_id in missing_roles
            else lifecycle.TargetObservation(
                "regular",
                old_hash,
                lifecycle.Marker(INSTALL_ID, role.role_id, PACKAGE_HASH, role.role_hash),
            )
        )
    roster_preimage = [
        {key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in state_roles
    ]
    value = {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": "0.2.0",
        "package_hash": PACKAGE_HASH,
        "codex_home_identity": _root_identity("/fixture/codex"),
        "plugin_root_identity": _root_identity("/fixture/plugin"),
        "state_root_identity": _root_identity("/fixture/codex/codex-rig/shims"),
        "target_root_identity": _root_identity("/fixture/codex/agents"),
        "roster_hash": hashlib.sha256(_canonical(roster_preimage)).hexdigest(),
        "bootstrap": {
            "protocol": 1,
            "helper_path": "scripts/verify_role_link.py",
            "helper_hash": BOOTSTRAP_HASH,
        },
        "generator_version": 1,
        "roles": state_roles,
        "transaction_status": "current",
    }
    return lifecycle.parse_state(_canonical(value)), targets


def _absent_targets(lifecycle: ModuleType) -> dict[str, object]:
    """Build one exact absent target roster."""
    return {f"codex-rig-{role_id}.toml": lifecycle.TargetObservation("absent") for role_id in _role_ids()}


def _historical_state_and_targets(roster: object) -> tuple[dict[str, object], dict[str, object]]:
    """Build authenticated prior ownership with one added and one retired role."""
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    roles = []
    targets = {}
    for active in roster.roles:
        if active.role_id == "web-explorer":
            continue
        payload = active.shim_bytes if active.role_id == "challenger" else f"old:{active.role_id}\n".encode()
        file_hash = hashlib.sha256(payload).hexdigest()
        role = {
            "role_id": active.role_id,
            "target_name": active.target_name,
            "card_path": active.card_path,
            "role_hash": active.role_hash,
            "file_hash": file_hash,
        }
        roles.append(role)
        targets[active.target_name] = lifecycle.TargetObservation(
            "regular",
            file_hash,
            lifecycle.Marker(INSTALL_ID, active.role_id, PACKAGE_HASH, active.role_hash),
        )
    retired = {
        "role_id": "retired-specialist",
        "target_name": "codex-rig-retired-specialist.toml",
        "card_path": "roles/retired-specialist/ROLE.md",
        "role_hash": "e" * 64,
        "file_hash": hashlib.sha256(b"old:retired-specialist\n").hexdigest(),
    }
    roles.append(retired)
    roles.sort(key=lambda item: item["role_id"])
    targets[retired["target_name"]] = lifecycle.TargetObservation(
        "regular",
        retired["file_hash"],
        lifecycle.Marker(INSTALL_ID, retired["role_id"], PACKAGE_HASH, retired["role_hash"]),
    )
    targets["codex-rig-web-explorer.toml"] = lifecycle.TargetObservation("absent")
    roster_preimage = [
        {key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles
    ]
    state = {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": "0.2.0",
        "package_hash": PACKAGE_HASH,
        "codex_home_identity": _root_identity("/fixture/codex"),
        "plugin_root_identity": _root_identity("/fixture/old-plugin"),
        "state_root_identity": _root_identity("/fixture/codex/codex-rig/shims"),
        "target_root_identity": _root_identity("/fixture/codex/agents"),
        "roster_hash": hashlib.sha256(_canonical(roster_preimage)).hexdigest(),
        "bootstrap": {
            "protocol": 1,
            "helper_path": "scripts/verify_role_link.py",
            "helper_hash": BOOTSTRAP_HASH,
        },
        "generator_version": 1,
        "roles": roles,
        "transaction_status": "current",
    }
    return state, dict(sorted(targets.items()))


def _build(
    module: ModuleType,
    *,
    action: str,
    roster: object,
    state: dict[str, object] | None,
    targets: dict[str, object],
) -> object:
    """Invoke the pure candidate builder with explicit immutable identifiers."""
    return module.build_candidate(
        action=action,
        mode="converge",
        roster=roster,
        state_payload=_canonical(state) if state is not None else None,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=TRANSACTION_NONCE,
    )


def test_fresh_install_derives_all_creates_and_rebuilds_deterministically() -> None:
    """Prove canonical bytes and digest are stable for identical input."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_create")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    roster = _generated_roster()

    first = _build(module, action="install", roster=roster, state=None, targets=_absent_targets(lifecycle))
    second = _build(module, action="install", roster=roster, state=None, targets=_absent_targets(lifecycle))

    assert {operation.intent for operation in first.operations} == {"create"}
    assert first == second
    assert first.canonical_bytes == _canonical(json.loads(first.canonical_bytes))
    assert not first.canonical_bytes.endswith(b"\n")
    assert first.digest == hashlib.sha256(first.canonical_bytes).hexdigest()
    payload = json.loads(first.canonical_bytes)
    assert payload["schema"] == 1
    assert payload["authorization"] == "candidate-only"
    assert payload["package_hash"] == roster.package_hash
    assert payload["bootstrap_hash"] == roster.bootstrap_hash
    assert payload["prior_state_hash"] is None
    assert [item["role_id"] for item in payload["operations"]] == list(_role_ids())


def test_install_derives_noop_update_and_repair_missing() -> None:
    """Derive each owned install intent from state, target, and generated bytes."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_install_intents")
    roster = _generated_roster()
    state, targets = _validated_state(roster, missing={"curator"})

    result = _build(module, action="install", roster=roster, state=state, targets=targets)
    intents = {operation.role_id: operation.intent for operation in result.operations}

    assert intents["challenger"] == "noop"
    assert intents["cicd-steward"] == "update"
    assert intents["curator"] == "repair-missing"
    assert set(intents.values()) == {"noop", "update", "repair-missing"}


def test_forward_upgrade_migrates_the_sorted_active_and_historical_union() -> None:
    """Create added roles, update retained roles, and retire authenticated old roles."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_forward_upgrade")
    roster = _generated_roster(version="0.3.0", package_hash="d" * 64)
    state, targets = _historical_state_and_targets(roster)

    result = _build(module, action="install", roster=roster, state=state, targets=targets)
    intents = {operation.role_id: operation.intent for operation in result.operations}

    assert result.transition == "forward-upgrade"
    assert result.prior_state_hash == hashlib.sha256(_canonical(state)).hexdigest()
    assert tuple(intents) == tuple(sorted(intents))
    assert intents["challenger"] == "noop"
    assert intents["cicd-steward"] == "update"
    assert intents["web-explorer"] == "create"
    assert intents["retired-specialist"] == "retire"
    retired = next(operation for operation in result.operations if operation.role_id == "retired-specialist")
    assert retired.before_card_path == "roles/retired-specialist/ROLE.md"
    assert retired.after_card_path is None
    added = next(operation for operation in result.operations if operation.role_id == "web-explorer")
    assert added.before_role_hash is None
    assert added.after_role_hash == ROLE_HASH


def test_same_package_cache_relink_updates_only_changed_generated_bytes() -> None:
    """Allow an identical package at a new cache root to regenerate thin links."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_cache_relink")
    old_roster = _generated_roster()
    state, targets = _validated_state(old_roster)
    active = _generated_roster({"challenger": b"new-cache:challenger\n"})

    result = _build(module, action="install", roster=active, state=state, targets=targets)
    intents = {operation.role_id: operation.intent for operation in result.operations}

    assert result.transition == "same-package"
    assert intents["challenger"] == "update"
    assert intents["cicd-steward"] == "update"
    assert set(intents.values()) == {"noop", "update"}


def test_remove_aligns_operations_with_the_active_and_historical_union() -> None:
    """Keep approval observations aligned while removing only state-owned roles."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_changed_roster_remove")
    roster = _generated_roster(version="0.3.0", package_hash="d" * 64)
    state, targets = _historical_state_and_targets(roster)

    result = _build(module, action="remove", roster=roster, state=state, targets=targets)
    intents = {operation.role_id: operation.intent for operation in result.operations}

    assert intents["retired-specialist"] == "remove"
    assert intents["challenger"] == "remove"
    assert intents["web-explorer"] == "noop"
    assert tuple(intents) == tuple(sorted(intents))


def test_remove_derives_owned_remove_and_absent_noop() -> None:
    """Remove only exact owned targets and preserve already absent roles."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_remove")
    roster = _generated_roster()
    state, targets = _validated_state(roster, missing={"curator"})

    result = _build(module, action="remove", roster=roster, state=state, targets=targets)
    intents = {operation.role_id: operation.intent for operation in result.operations}

    assert intents["challenger"] == "remove"
    assert intents["curator"] == "noop"
    assert set(intents.values()) == {"remove", "noop"}


def test_pristine_remove_is_all_noop() -> None:
    """Represent pristine removal as a zero-change complete roster."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_pristine_remove")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    result = _build(
        module,
        action="remove",
        roster=_generated_roster(),
        state=None,
        targets=_absent_targets(lifecycle),
    )

    assert {operation.intent for operation in result.operations} == {"noop"}
    assert all(not operation.before_exists and not operation.after_exists for operation in result.operations)


@pytest.mark.parametrize("kind", ["foreign", "modified", "unsafe"])
def test_untrusted_targets_fail_closed(kind: str) -> None:
    """Refuse any target roster that lacks exact state-backed ownership."""
    module = _load_module(PLAN_PATH, f"codex_rig_plan_{kind}")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    roster = _generated_roster()
    state, targets = _validated_state(roster)
    if kind == "foreign":
        state = None
    elif kind == "modified":
        targets["codex-rig-challenger.toml"] = lifecycle.TargetObservation("regular", "f" * 64, None)
    else:
        targets["codex-rig-challenger.toml"] = lifecycle.TargetObservation("unsafe")

    with pytest.raises(module.CandidateError):
        _build(module, action="install", roster=roster, state=state, targets=targets)


@pytest.mark.parametrize("variant", ["partial", "extra", "reordered"])
def test_partial_extra_and_reordered_rosters_fail_closed(variant: str) -> None:
    """Reject any deviation from the exact generator-owned role order."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_roster_refusal")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    roster = _generated_roster()
    exact = _absent_targets(lifecycle)
    if variant == "partial":
        targets = dict(list(exact.items())[:-1])
    elif variant == "extra":
        targets = {**exact, "codex-rig-extra.toml": lifecycle.TargetObservation("absent")}
    else:
        targets = dict(reversed(tuple(exact.items())))

    with pytest.raises(module.CandidateError):
        _build(module, action="install", roster=roster, state=None, targets=targets)


def test_unhashable_candidate_inputs_fail_with_stable_error() -> None:
    """Keep forged dataclass fields inside the candidate error contract."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_unhashable")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    targets = _absent_targets(lifecycle)
    targets["codex-rig-challenger.toml"] = lifecycle.TargetObservation([])

    with pytest.raises(module.CandidateError):
        _build(module, action="install", roster=_generated_roster(), state=None, targets=targets)
    with pytest.raises(module.CandidateError):
        module.build_candidate(
            action=[],
            mode="converge",
            roster=_generated_roster(),
            state_payload=None,
            targets=_absent_targets(lifecycle),
            install_id=INSTALL_ID,
            transaction_nonce=TRANSACTION_NONCE,
        )


@pytest.mark.parametrize(
    ("install_id", "transaction_nonce"),
    [
        pytest.param("not-a-uuid", TRANSACTION_NONCE, id="not-a-uuid"),
        pytest.param(INSTALL_ID, "not-a-uuid", id="install_id"),
    ],
)
def test_invalid_identifiers_fail_closed(install_id: str, transaction_nonce: str) -> None:
    """Reject candidate identities not supplied as canonical UUIDs."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_invalid_identifiers")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")

    with pytest.raises(module.CandidateError):
        module.build_candidate(
            action="install",
            mode="converge",
            roster=_generated_roster(),
            state_payload=None,
            targets=_absent_targets(lifecycle),
            install_id=install_id,
            transaction_nonce=transaction_nonce,
        )


def test_invalid_action_mode_and_nonfinite_input_fail_closed() -> None:
    """Reject unsupported transitions and non-canonical JSON values."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_invalid_transition")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    roster = _generated_roster()
    targets = _absent_targets(lifecycle)
    with pytest.raises(module.CandidateError):
        module.build_candidate(
            action="adopt",
            mode="converge",
            roster=roster,
            state_payload=None,
            targets=targets,
            install_id=INSTALL_ID,
            transaction_nonce=TRANSACTION_NONCE,
        )
    with pytest.raises(module.CandidateError):
        module.build_candidate(
            action="install",
            mode="recovery",
            roster=roster,
            state_payload=None,
            targets=targets,
            install_id=INSTALL_ID,
            transaction_nonce=TRANSACTION_NONCE,
        )


def test_state_requires_strict_semver_and_exact_package_content_identity() -> None:
    """Reject permissive versions and same-version package substitutions."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_package_identity")
    generator = _load_module(GENERATOR_PATH, "generate_roles")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    roster = _generated_roster()
    invalid_version = generator.GeneratedRoster(
        "1.0.0-01",
        roster.package_hash,
        roster.bootstrap_hash,
        roster.generator_version,
        roster.roles,
    )
    with pytest.raises(module.CandidateError):
        _build(
            module,
            action="install",
            roster=invalid_version,
            state=None,
            targets=_absent_targets(lifecycle),
        )

    state, targets = _validated_state(roster)
    substituted = generator.GeneratedRoster(
        roster.plugin_version,
        "d" * 64,
        roster.bootstrap_hash,
        roster.generator_version,
        roster.roles,
    )
    with pytest.raises(module.CandidateError, match="same-version package content differs"):
        _build(module, action="install", roster=substituted, state=state, targets=targets)
    with pytest.raises(module.CandidateError, match="exact state bytes"):
        module.build_candidate(
            action="install",
            mode="converge",
            roster=roster,
            state_payload=state,
            targets=targets,
            install_id=INSTALL_ID,
            transaction_nonce=TRANSACTION_NONCE,
        )


@pytest.mark.parametrize(
    ("active_version", "prior_version", "accepted"),
    [
        pytest.param("0.2.1", "0.2.0", True, id="0.2.1"),
        pytest.param("0.3.0", "0.2.9", True, id="0.3.0"),
        pytest.param("1.0.0", "1.0.0-rc.1", True, id="1.0.0"),
        pytest.param("1.0.0-rc.2", "1.0.0-rc.1", True, id="1.0.0-rc.2"),
        pytest.param("0.1.9", "0.2.0", False, id="0.1.9"),
        pytest.param("1.0.0-rc.1", "1.0.0", False, id="1.0.0-rc.1"),
        pytest.param("0.2.0+other.2", "0.2.0+other.1", False, id="0.2.0-other.2"),
        pytest.param("0.2.0+codex.2", "0.2.0+codex.1", True, id="0.2.0-codex.2"),
    ],
)
def test_semver_transition_policy(active_version: str, prior_version: str, accepted: bool) -> None:
    """Allow forward releases and ordered local rebuilds while refusing downgrade."""
    module = _load_module(PLAN_PATH, f"codex_rig_plan_semver_{active_version}_{prior_version}")
    prior_roster = _generated_roster()
    state, targets = _validated_state(prior_roster)
    state["plugin_version"] = prior_version
    active = _generated_roster(version=active_version, package_hash="d" * 64)

    if accepted:
        result = _build(module, action="install", roster=active, state=state, targets=targets)
        expected = "development-rebuild" if "+codex." in active_version else "forward-upgrade"
        assert result.transition == expected
    else:
        with pytest.raises(module.CandidateError):
            _build(module, action="install", roster=active, state=state, targets=targets)


def test_digest_revalidation_rejects_tamper() -> None:
    """Accept only the digest of the exact rebuilt canonical bytes."""
    module = _load_module(PLAN_PATH, "codex_rig_plan_digest")
    lifecycle = _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    result = _build(
        module,
        action="install",
        roster=_generated_roster(),
        state=None,
        targets=_absent_targets(lifecycle),
    )

    module.revalidate_candidate_digest(result, result.digest)
    with pytest.raises(module.CandidateError):
        module.revalidate_candidate_digest(result, "0" * 64)
    tampered = module.CandidatePlan(
        action=result.action,
        mode=result.mode,
        plugin_version=result.plugin_version,
        package_hash=result.package_hash,
        bootstrap_hash=result.bootstrap_hash,
        generator_version=result.generator_version,
        install_id=result.install_id,
        transaction_nonce=result.transaction_nonce,
        roster_hash=result.roster_hash,
        transition=result.transition,
        prior_state_hash=result.prior_state_hash,
        operations=result.operations,
        canonical_bytes=result.canonical_bytes + b" ",
        digest=result.digest,
    )
    with pytest.raises(module.CandidateError):
        module.revalidate_candidate_digest(tampered, result.digest)
    changed_operation = module.Operation(
        **{**result.operations[0].__dict__, "intent": "noop"},
    )
    tampered_operation = module.CandidatePlan(
        action=result.action,
        mode=result.mode,
        plugin_version=result.plugin_version,
        package_hash=result.package_hash,
        bootstrap_hash=result.bootstrap_hash,
        generator_version=result.generator_version,
        install_id=result.install_id,
        transaction_nonce=result.transaction_nonce,
        roster_hash=result.roster_hash,
        transition=result.transition,
        prior_state_hash=result.prior_state_hash,
        operations=(changed_operation, *result.operations[1:]),
        canonical_bytes=result.canonical_bytes,
        digest=result.digest,
    )
    with pytest.raises(module.CandidateError):
        module.revalidate_candidate_digest(tampered_operation, result.digest)


def test_kernel_has_no_filesystem_or_process_surface() -> None:
    """Keep approval derivation structurally pure and incapable of writes."""
    tree = ast.parse(PLAN_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".", maxsplit=1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

    assert not ({"os", "pathlib", "shutil", "subprocess", "tempfile"} & imported)
    assert not ({"open", "exec", "eval", "compile", "__import__"} & called)
