"""Acceptance checks for the pure shim approval kernel."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered
from _shim_support import absent_roster, canonical_json, role_ids, root_identity

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
PLAN_PATH = SCRIPTS / "_agent_shim_plan.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
TRANSACTION_NONCE = "123e4567-e89b-42d3-a456-426614174001"
PACKAGE_HASH = "a" * 64
ROLE_HASH = "b" * 64
BOOTSTRAP_HASH = "c" * 64
CHALLENGER_TARGET = "codex-rig-challenger.toml"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load sibling scripts without package installation assumptions."""
    if name in {"generate_roles", "_agent_shim_lifecycle"} and name in sys.modules:
        return sys.modules[name]
    if path != GENERATOR_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    if path == PLAN_PATH and "_agent_shim_lifecycle" not in sys.modules:
        _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    return load_module_registered(name, path)


@pytest.fixture
def plan() -> ModuleType:
    """Load a fresh copy of the approval kernel for one test."""
    return _load_module(PLAN_PATH, "codex_rig_plan_under_test")


@pytest.fixture
def lifecycle() -> ModuleType:
    """Return the lifecycle module the approval kernel itself imports."""
    return _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")


@pytest.fixture
def roster() -> object:
    """Build the default generated roster."""
    return _generated_roster()


@pytest.fixture
def absent_targets(lifecycle: ModuleType) -> dict[str, object]:
    """Observe every roster target as absent."""
    return absent_roster(lifecycle)


@pytest.fixture
def upgraded_roster() -> object:
    """Build a newer roster with a different package hash than the persisted state."""
    return _generated_roster(version="0.3.0", package_hash="d" * 64)


@pytest.fixture
def fresh_candidate(plan: ModuleType, roster: object, absent_targets: dict[str, object]) -> object:
    """Build one fresh-install candidate."""
    return _build(plan, roster=roster, targets=absent_targets)


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
    for role_id in role_ids():
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
    return generator.GeneratedRoster(version, package_hash, BOOTSTRAP_HASH, 2, tuple(roles))


def _state_document(roles: list[dict[str, object]], *, plugin_root: str, generator_version: int) -> dict[str, object]:
    """Build one persisted state document whose roster hash matches ``roles``."""
    roster_preimage = [
        {key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles
    ]
    return {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": "0.2.0",
        "package_hash": PACKAGE_HASH,
        "codex_home_identity": root_identity("/fixture/codex"),
        "plugin_root_identity": root_identity(plugin_root),
        "state_root_identity": root_identity("/fixture/codex/codex-rig/shims"),
        "target_root_identity": root_identity("/fixture/codex/agents"),
        "roster_hash": hashlib.sha256(canonical_json(roster_preimage)).hexdigest(),
        "bootstrap": {"protocol": 1, "helper_path": "scripts/verify_role_link.py", "helper_hash": BOOTSTRAP_HASH},
        "generator_version": generator_version,
        "roles": roles,
        "transaction_status": "current",
    }


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
                lifecycle.Marker(INSTALL_ID, role.role_id, PACKAGE_HASH, role.role_hash, roster.generator_version),
            )
        )
    value = _state_document(state_roles, plugin_root="/fixture/plugin", generator_version=roster.generator_version)
    return lifecycle.parse_state(canonical_json(value)), targets


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
        roles.append(
            {
                "role_id": active.role_id,
                "target_name": active.target_name,
                "card_path": active.card_path,
                "role_hash": active.role_hash,
                "file_hash": file_hash,
            }
        )
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
    state = _state_document(roles, plugin_root="/fixture/old-plugin", generator_version=1)
    return state, dict(sorted(targets.items()))


@pytest.fixture
def historical_install(upgraded_roster: object) -> tuple[dict[str, object], dict[str, object]]:
    """Build prior state and targets that differ from ``upgraded_roster`` by one added and one retired role."""
    return _historical_state_and_targets(upgraded_roster)


def _build(plan: ModuleType, *, roster: object, targets: dict[str, object], state: object = None, **overrides: object):
    """Invoke the pure candidate builder with explicit immutable identifiers.

    Keyword ``overrides`` replace any ``build_candidate`` argument, so a test can pass an invalid value directly.
    """
    arguments = {
        "action": "install",
        "mode": "converge",
        "roster": roster,
        "state_payload": None if state is None else canonical_json(state),
        "targets": targets,
        "install_id": INSTALL_ID,
        "transaction_nonce": TRANSACTION_NONCE,
        **overrides,
    }
    return plan.build_candidate(**arguments)


def _intents(result: object) -> dict[str, str]:
    """Map each role to its derived intent, preserving operation order."""
    return {operation.role_id: operation.intent for operation in result.operations}


def _make_foreign(lifecycle: ModuleType, state: object, targets: dict[str, object]) -> object:
    """Drop persisted state so every regular target is foreign."""
    return None


def _modify_challenger(lifecycle: ModuleType, state: object, targets: dict[str, object]) -> object:
    """Replace the challenger target with a hash-mismatched regular file."""
    targets[CHALLENGER_TARGET] = lifecycle.TargetObservation("regular", "f" * 64, None)
    return state


def _make_challenger_unsafe(lifecycle: ModuleType, state: object, targets: dict[str, object]) -> object:
    """Replace the challenger target with an unsafe node."""
    targets[CHALLENGER_TARGET] = lifecycle.TargetObservation("unsafe")
    return state


class TestInstallIntents:
    """Install-action intents derived from state, observed targets, and generated bytes."""

    def test_fresh_install_creates_all_roles_deterministically(
        self, plan: ModuleType, roster: object, absent_targets: dict[str, object]
    ) -> None:
        """A fresh install creates every role and rebuilds byte-identical canonical output.

        With no prior state and an all-absent target roster, two builds from identical input must produce equal
        candidates whose digest covers the exact canonical bytes and whose operations follow roster order.
        """
        first = _build(plan, roster=roster, targets=absent_targets)
        second = _build(plan, roster=roster, targets=absent_targets)
        payload = json.loads(first.canonical_bytes)

        assert {operation.intent for operation in first.operations} == {"create"}
        assert first == second
        assert first.canonical_bytes == canonical_json(payload)
        assert not first.canonical_bytes.endswith(b"\n")
        assert first.digest == hashlib.sha256(first.canonical_bytes).hexdigest()
        assert payload["schema"] == 1
        assert payload["authorization"] == "candidate-only"
        assert payload["package_hash"] == roster.package_hash
        assert payload["bootstrap_hash"] == roster.bootstrap_hash
        assert payload["prior_state_hash"] is None
        assert [item["role_id"] for item in payload["operations"]] == list(role_ids())

    def test_derives_noop_update_and_repair_missing(
        self, plan: ModuleType, roster: object, absent_targets: dict[str, object]
    ) -> None:
        """Owned roles are no-op when current, updated when bytes changed, repaired when missing.

        State records an old ``cicd-steward`` file and ``curator`` is missing on disk; the other roles match, so one
        install must yield all three non-create intents.
        """
        state, targets = _validated_state(roster, missing={"curator"})

        intents = _intents(_build(plan, roster=roster, targets=targets, state=state))

        assert intents["challenger"] == "noop"
        assert intents["cicd-steward"] == "update"
        assert intents["curator"] == "repair-missing"
        assert set(intents.values()) == {"noop", "update", "repair-missing"}

    def test_forward_upgrade_migrates_active_and_historical_union(
        self,
        plan: ModuleType,
        upgraded_roster: object,
        historical_install: tuple[dict[str, object], dict[str, object]],
    ) -> None:
        """Added roles are created, retained roles updated, and authenticated retired roles retired.

        The newer roster adds ``web-explorer`` and drops ``retired-specialist``; operations cover the sorted union so
        approval observations stay aligned with the prior ownership.
        """
        state, targets = historical_install

        result = _build(plan, roster=upgraded_roster, targets=targets, state=state)
        operations = {operation.role_id: operation for operation in result.operations}
        intents = _intents(result)

        assert result.transition == "forward-upgrade"
        assert result.prior_state_hash == hashlib.sha256(canonical_json(state)).hexdigest()
        assert tuple(intents) == tuple(sorted(intents))
        assert intents["challenger"] == "noop"
        assert intents["cicd-steward"] == "update"
        assert intents["web-explorer"] == "create"
        assert intents["retired-specialist"] == "retire"
        assert operations["retired-specialist"].before_card_path == "roles/retired-specialist/ROLE.md"
        assert operations["retired-specialist"].after_card_path is None
        assert operations["web-explorer"].before_role_hash is None
        assert operations["web-explorer"].after_role_hash == ROLE_HASH

    def test_same_package_cache_relink_updates_only_changed_generated_bytes(self, plan: ModuleType) -> None:
        """An identical package at a new cache root regenerates thin links without other changes.

        The active roster has the same package hash but different challenger bytes (a new cache path); only roles whose
        generated bytes differ from the recorded state may become updates.
        """
        state, targets = _validated_state(_generated_roster())
        active = _generated_roster({"challenger": b"new-cache:challenger\n"})

        result = _build(plan, roster=active, targets=targets, state=state)
        intents = _intents(result)

        assert result.transition == "same-package"
        assert intents["challenger"] == "update"
        assert intents["cicd-steward"] == "update"
        assert set(intents.values()) == {"noop", "update"}


class TestRemoveIntents:
    """Remove-action intents."""

    def test_aligns_operations_with_active_and_historical_union(
        self,
        plan: ModuleType,
        upgraded_roster: object,
        historical_install: tuple[dict[str, object], dict[str, object]],
    ) -> None:
        """Removal covers the sorted active-plus-historical union and touches only state-owned roles.

        ``retired-specialist`` and ``challenger`` are owned by state and are removed; ``web-explorer`` was never
        installed, so it stays a no-op.
        """
        state, targets = historical_install

        intents = _intents(_build(plan, action="remove", roster=upgraded_roster, targets=targets, state=state))

        assert intents["retired-specialist"] == "remove"
        assert intents["challenger"] == "remove"
        assert intents["web-explorer"] == "noop"
        assert tuple(intents) == tuple(sorted(intents))

    def test_removes_owned_targets_and_keeps_absent_noop(self, plan: ModuleType, roster: object) -> None:
        """Only exact owned targets are removed; an already absent role is a no-op.

        ``curator`` is missing on disk while state still lists it, so removal must not try to delete it.
        """
        state, targets = _validated_state(roster, missing={"curator"})

        intents = _intents(_build(plan, action="remove", roster=roster, targets=targets, state=state))

        assert intents["challenger"] == "remove"
        assert intents["curator"] == "noop"
        assert set(intents.values()) == {"remove", "noop"}

    def test_pristine_removal_is_all_noop(
        self, plan: ModuleType, roster: object, absent_targets: dict[str, object]
    ) -> None:
        """Removing when nothing was ever installed yields a zero-change complete roster.

        No state and all targets absent: every operation is a no-op with neither a before nor an after file.
        """
        result = _build(plan, action="remove", roster=roster, targets=absent_targets)

        assert {operation.intent for operation in result.operations} == {"noop"}
        assert all(not operation.before_exists and not operation.after_exists for operation in result.operations)


class TestFailClosed:
    """Inputs that must raise ``CandidateError`` instead of producing a candidate."""

    @pytest.mark.parametrize(
        "degrade",
        [
            pytest.param(_make_foreign, id="foreign"),
            pytest.param(_modify_challenger, id="modified"),
            pytest.param(_make_challenger_unsafe, id="unsafe"),
        ],
    )
    def test_untrusted_targets(self, plan: ModuleType, lifecycle: ModuleType, roster: object, degrade: object) -> None:
        """A target roster lacking exact state-backed ownership is refused.

        Starting from a fully owned install, state is dropped (foreign), or one target is modified or unsafe; the
        builder must not plan changes over files it cannot prove it owns.
        """
        state, targets = _validated_state(roster)
        state = degrade(lifecycle, state, targets)

        with pytest.raises(plan.CandidateError):
            _build(plan, roster=roster, targets=targets, state=state)

    @pytest.mark.parametrize(
        "reshape",
        [
            pytest.param(lambda lifecycle, exact: dict(list(exact.items())[:-1]), id="partial"),
            pytest.param(
                lambda lifecycle, exact: {**exact, "codex-rig-extra.toml": lifecycle.TargetObservation("absent")},
                id="extra",
            ),
            pytest.param(lambda lifecycle, exact: dict(reversed(tuple(exact.items()))), id="reordered"),
        ],
    )
    def test_non_exact_target_rosters(
        self,
        plan: ModuleType,
        lifecycle: ModuleType,
        roster: object,
        absent_targets: dict[str, object],
        reshape: object,
    ) -> None:
        """Any deviation from the exact generator-owned role order is refused.

        The observation roster is shortened, extended with an unknown role, or reversed; approval alignment depends on
        the exact order, so each variant must fail.
        """
        targets = reshape(lifecycle, absent_targets)

        with pytest.raises(plan.CandidateError):
            _build(plan, roster=roster, targets=targets)

    def test_unhashable_target_kind(
        self, plan: ModuleType, lifecycle: ModuleType, roster: object, absent_targets: dict[str, object]
    ) -> None:
        """A forged target observation with an unhashable kind raises the stable candidate error.

        Dataclass fields are not validated at construction, so a list-valued ``kind`` can reach the builder and must not
        escape as ``TypeError``.
        """
        absent_targets[CHALLENGER_TARGET] = lifecycle.TargetObservation([])

        with pytest.raises(plan.CandidateError):
            _build(plan, roster=roster, targets=absent_targets)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"install_id": "not-a-uuid"}, id="install-id-not-uuid"),
            pytest.param({"transaction_nonce": "not-a-uuid"}, id="nonce-not-uuid"),
            pytest.param({"action": "adopt"}, id="unknown-action"),
            pytest.param({"action": []}, id="unhashable-action"),
            pytest.param({"mode": "recovery"}, id="unsupported-mode"),
        ],
    )
    def test_invalid_arguments(
        self, plan: ModuleType, roster: object, absent_targets: dict[str, object], overrides: dict[str, object]
    ) -> None:
        """Identities that are not canonical UUIDs and unsupported actions or modes are refused.

        Everything else is a valid fresh install, so each failure comes from the single invalid argument.
        """
        with pytest.raises(plan.CandidateError):
            _build(plan, roster=roster, targets=absent_targets, **overrides)


class TestVersionPolicy:
    """Version and package-identity checks across transitions."""

    def test_rejects_permissive_version(
        self, plan: ModuleType, roster: object, absent_targets: dict[str, object]
    ) -> None:
        """A version that is not strict SemVer is refused even on a fresh install.

        ``1.0.0-01`` has a numeric pre-release identifier with a leading zero, which SemVer forbids.
        """
        invalid = dataclasses.replace(roster, plugin_version="1.0.0-01")

        with pytest.raises(plan.CandidateError):
            _build(plan, roster=invalid, targets=absent_targets)

    def test_rejects_same_version_package_substitution(self, plan: ModuleType, roster: object) -> None:
        """The same version with a different package hash is refused.

        State records version ``0.2.0`` with one package hash; an active roster claiming the same version but other
        content must not be treated as an upgrade.
        """
        state, targets = _validated_state(roster)
        substituted = dataclasses.replace(roster, package_hash="d" * 64)

        with pytest.raises(plan.CandidateError, match="same-version package content differs"):
            _build(plan, roster=substituted, targets=targets, state=state)

    def test_rejects_state_that_is_not_exact_bytes(self, plan: ModuleType, roster: object) -> None:
        """A parsed dict passed as the state payload is refused; only exact state bytes are accepted.

        Re-serializing a parsed state could normalise away tampering, so the hash must bind to the bytes read.
        """
        state, targets = _validated_state(roster)

        with pytest.raises(plan.CandidateError, match="exact state bytes"):
            _build(plan, roster=roster, targets=targets, state_payload=state)

    @pytest.mark.parametrize(
        ("active_version", "prior_version", "transition"),
        [
            pytest.param("0.2.1", "0.2.0", "forward-upgrade", id="0.2.1"),
            pytest.param("0.3.0", "0.2.9", "forward-upgrade", id="0.3.0"),
            pytest.param("1.0.0", "1.0.0-rc.1", "forward-upgrade", id="1.0.0"),
            pytest.param("1.0.0-rc.2", "1.0.0-rc.1", "forward-upgrade", id="1.0.0-rc.2"),
            pytest.param("0.2.0+codex.2", "0.2.0+codex.1", "development-rebuild", id="0.2.0-codex.2"),
        ],
    )
    def test_accepts_forward_and_ordered_rebuild_transitions(
        self, plan: ModuleType, active_version: str, prior_version: str, transition: str
    ) -> None:
        """Forward releases and ordered local ``+codex.N`` rebuilds are accepted.

        The prior state is rewritten to ``prior_version`` and the active roster carries a new package hash; local
        rebuilds are reported as ``development-rebuild`` and release upgrades as ``forward-upgrade``.
        """
        state, targets = _validated_state(_generated_roster())
        state["plugin_version"] = prior_version
        active = _generated_roster(version=active_version, package_hash="d" * 64)

        result = _build(plan, roster=active, targets=targets, state=state)

        assert result.transition == transition

    @pytest.mark.parametrize(
        ("active_version", "prior_version"),
        [
            pytest.param("0.1.9", "0.2.0", id="0.1.9"),
            pytest.param("1.0.0-rc.1", "1.0.0", id="1.0.0-rc.1"),
            pytest.param("0.2.0+other.2", "0.2.0+other.1", id="0.2.0-other.2"),
        ],
    )
    def test_rejects_downgrade_and_unordered_rebuilds(
        self, plan: ModuleType, active_version: str, prior_version: str
    ) -> None:
        """Downgrades, a pre-release after its release, and non-``codex`` build metadata are refused.

        Build metadata other than ``+codex.N`` carries no ordering, so it cannot justify replacing prior state.
        """
        state, targets = _validated_state(_generated_roster())
        state["plugin_version"] = prior_version
        active = _generated_roster(version=active_version, package_hash="d" * 64)

        with pytest.raises(plan.CandidateError):
            _build(plan, roster=active, targets=targets, state=state)


class TestDigestRevalidation:
    """Digest revalidation of a built candidate."""

    def test_accepts_the_exact_candidate_digest(self, plan: ModuleType, fresh_candidate: object) -> None:
        """The digest of the exact rebuilt canonical bytes is accepted.

        Revalidation returns nothing and raises on any mismatch, so a clean return is the contract.
        """
        assert plan.revalidate_candidate_digest(fresh_candidate, fresh_candidate.digest) is None

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(lambda candidate: (candidate, "0" * 64), id="wrong-digest"),
            pytest.param(
                lambda candidate: (
                    dataclasses.replace(candidate, canonical_bytes=candidate.canonical_bytes + b" "),
                    candidate.digest,
                ),
                id="extended-canonical-bytes",
            ),
            pytest.param(
                lambda candidate: (
                    dataclasses.replace(
                        candidate,
                        operations=(
                            dataclasses.replace(candidate.operations[0], intent="noop"),
                            *candidate.operations[1:],
                        ),
                    ),
                    candidate.digest,
                ),
                id="changed-operation",
            ),
        ],
    )
    def test_rejects_tamper(self, plan: ModuleType, fresh_candidate: object, tamper: object) -> None:
        """A wrong digest, altered canonical bytes, or an altered operation is refused.

        The candidate is rebuilt from its fields and compared with the claimed bytes and digest, so editing any one of
        them without the others must fail.
        """
        candidate, digest = tamper(fresh_candidate)

        with pytest.raises(plan.CandidateError):
            plan.revalidate_candidate_digest(candidate, digest)
