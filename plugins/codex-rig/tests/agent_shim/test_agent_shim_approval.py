"""Acceptance checks for complete no-write convergence approval binding.

The approval kernel turns a candidate plan plus filesystem and runtime evidence into canonical approval bytes and a
digest. These tests build matching and deliberately forged evidence and prove what the kernel binds or refuses.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest
from _modules import load_module_registered

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
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
LOCK_PATH = "/codex/.codex-rig-shims.lock"


class Modules(NamedTuple):
    """Hold the freshly loaded script modules one test works against."""

    generator: ModuleType
    lifecycle: ModuleType
    observer: ModuleType
    planner: ModuleType
    approval: ModuleType


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
    return generator.GeneratedRoster("0.2.0", DIGEST, "c" * 64, 2, tuple(roles))


def _root(observer: ModuleType, path: str, inode: int) -> object:
    """Build one exact directory identity."""
    return observer.RootIdentity(path, 1, inode, 501, 20, "0700")


def _existing_root(observer: ModuleType, identity: object) -> object:
    """Build the observation of a root that exists exactly as ``identity`` describes."""
    return observer.RootObservation(True, identity.canonical_path, identity, (), identity)


def _root_observation(observer: ModuleType, path: str, inode: int, *, exists: bool) -> object:
    """Build one exact existing or single-component absent root observation."""
    home_path = path.rsplit("/", maxsplit=1)[0]
    ancestor = _root(observer, path if exists else home_path, inode if exists else 1)
    identity = ancestor if exists else None
    suffix = () if exists else (path.rsplit("/", maxsplit=1)[1],)
    return observer.RootObservation(exists, path, ancestor, suffix, identity)


def _absent_lock(observer: ModuleType, path: str = LOCK_PATH) -> object:
    """Build the observation of a coordination lock that does not exist."""
    return observer.LockObservation("absent", path, None, None, None, None, None, None, None)


def _filesystem_observation(
    observer: ModuleType, *, state: str, roots: tuple, state_payload: bytes | None, targets: dict
):
    """Build one degraded complete-scan observation; ``roots`` is home, plugin, target-root and state-root."""
    home, plugin, target_root, state_root = roots
    return observer.FilesystemObservation(
        "degraded",
        "runtime and mutation prerequisites remain unverified",
        state,
        state,
        "none",
        home,
        plugin,
        target_root,
        state_root,
        _absent_lock(observer),
        state_payload,
        tuple(targets.items()),
        (),
        "complete",
    )


def _role_entries(roster: object) -> list[dict[str, str]]:
    """Return the persisted-state role entries that describe every role in a roster."""
    return [
        {
            "role_id": role.role_id,
            "target_name": role.target_name,
            "card_path": role.card_path,
            "role_hash": role.role_hash,
            "file_hash": role.file_hash,
        }
        for role in roster.roles
    ]


def _encode(value: object) -> bytes:
    """Encode one canonical compact JSON fixture."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _candidate_and_observation(
    loaded: Modules, *, action: str = "install", roots_exist: bool = False
) -> tuple[object, object, object]:
    """Build matching candidate operations and immutable filesystem evidence."""
    roster = _generated_roster(loaded.generator)
    targets = {role.target_name: loaded.lifecycle.TargetObservation("absent") for role in roster.roles}
    candidate = loaded.planner.build_candidate(
        action=action,
        mode="converge",
        roster=roster,
        state_payload=None,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=NONCE,
    )
    observer = loaded.observer
    home_identity = _root(observer, "/codex", 1)
    roots = (
        _existing_root(observer, home_identity),
        _root(observer, "/plugin", 4),
        _root_observation(observer, "/codex/agents", 2, exists=roots_exist),
        _root_observation(observer, "/codex/codex-rig/shims", 3, exists=roots_exist),
    )
    observation = _filesystem_observation(observer, state="absent", roots=roots, state_payload=None, targets=targets)
    return roster, candidate, observation


def _runtime(approval: ModuleType, *, eligible: bool = True) -> object:
    """Build one doctor-supplied executable binding fixture."""
    return approval.RuntimeBinding("/usr/bin/python3", "d" * 64, "/usr/bin/codex", "e" * 64, eligible)


def _removed_candidate_and_observation(loaded: Modules, *, target_inode: int) -> tuple[object, object, object]:
    """Build a removed tombstone and one current empty target-root identity."""
    observer = loaded.observer
    roster = _generated_roster(loaded.generator)
    home_identity = _root(observer, "/codex", 1)
    plugin_identity = _root(observer, "/plugin", 4)
    state_identity = _root(observer, "/codex/codex-rig/shims", 3)
    persisted_target = _root(observer, "/codex/agents", 2)
    role_tuples = tuple((role.role_id, role.target_name, role.card_path, role.role_hash) for role in roster.roles)
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
        "roster_hash": loaded.generator.roster_identity_hash(role_tuples),
        "bootstrap": {
            "protocol": 1,
            "helper_path": "scripts/verify_role_link.py",
            "helper_hash": roster.bootstrap_hash,
        },
        "generator_version": roster.generator_version,
        "roles": _role_entries(roster),
        "transaction_status": "removed",
    }
    state_payload = _encode(state)
    targets = {role.target_name: loaded.lifecycle.TargetObservation("absent") for role in roster.roles}
    candidate = loaded.planner.build_candidate(
        action="install",
        mode="converge",
        roster=roster,
        state_payload=state_payload,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=NONCE,
    )
    roots = (
        _existing_root(observer, home_identity),
        plugin_identity,
        _existing_root(observer, _root(observer, "/codex/agents", target_inode)),
        _existing_root(observer, state_identity),
    )
    observation = _filesystem_observation(
        observer, state="removed", roots=roots, state_payload=state_payload, targets=targets
    )
    return roster, candidate, observation


def _migration_candidate_and_observation(loaded: Modules) -> tuple[object, object, object, dict[str, object]]:
    """Build a forward migration from a different cached plugin root."""
    observer, lifecycle = loaded.observer, loaded.lifecycle
    roster = _generated_roster(loaded.generator)
    home_identity = _root(observer, "/codex", 1)
    target_identity = _root(observer, "/codex/agents", 2)
    state_identity = _root(observer, "/codex/codex-rig/shims", 3)
    prior_plugin_identity = _root(observer, "/plugin-cache/prior", 4)
    active_plugin_identity = _root(observer, "/plugin-cache/active", 5)
    retired = {
        "role_id": "retired-specialist",
        "target_name": "codex-rig-retired-specialist.toml",
        "card_path": "roles/retired-specialist/ROLE.md",
        "role_hash": "d" * 64,
        "file_hash": "e" * 64,
    }
    roles = sorted([*_role_entries(roster), retired], key=lambda role: role["role_id"])
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
            _encode(
                [{key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles]
            )
        ).hexdigest(),
        "bootstrap": {"protocol": 1, "helper_path": "scripts/verify_role_link.py", "helper_hash": "9" * 64},
        "generator_version": 1,
        "roles": roles,
        "transaction_status": "current",
    }
    state_payload = _encode(state)
    targets = {
        role["target_name"]: lifecycle.TargetObservation(
            "regular",
            role["file_hash"],
            lifecycle.Marker(INSTALL_ID, role["role_id"], prior_package_hash, role["role_hash"]),
        )
        for role in roles
    }
    targets = dict(sorted(targets.items()))
    candidate = loaded.planner.build_candidate(
        action="install",
        mode="converge",
        roster=roster,
        state_payload=state_payload,
        targets=targets,
        install_id=INSTALL_ID,
        transaction_nonce=NONCE,
    )
    roots = (
        _existing_root(observer, home_identity),
        active_plugin_identity,
        _existing_root(observer, target_identity),
        _existing_root(observer, state_identity),
    )
    observation = _filesystem_observation(
        observer, state="current", roots=roots, state_payload=state_payload, targets=targets
    )
    return roster, candidate, observation, state


def _keep(loaded: Modules, observation: object) -> object:
    """Return the observation unchanged."""
    return observation


def _block(loaded: Modules, observation: object) -> object:
    """Mark the observation as blocked."""
    return replace(observation, classification="blocked")


def _stale_first_target(loaded: Modules, observation: object) -> object:
    """Replace the first target observation with a present file the plan never saw."""
    targets = list(observation.target_observations)
    targets[0] = (targets[0][0], loaded.lifecycle.TargetObservation("regular", DIGEST))
    return replace(observation, target_observations=tuple(targets))


def _forge_root_owner(loaded: Modules, observation: object) -> object:
    """Give the Codex-home identity a boolean owner."""
    identity = replace(observation.codex_home_observation.identity, owner=True)
    home = replace(observation.codex_home_observation, nearest_existing_ancestor=identity, identity=identity)
    return replace(observation, codex_home_observation=home)


def _forge_root_ancestry(loaded: Modules, observation: object) -> object:
    """Claim the target root is missing below an ancestor that is not its parent."""
    ancestor = observation.codex_home_observation.identity
    bad_target = loaded.observer.RootObservation(False, "/codex/agents", ancestor, ("other",), None)
    return replace(observation, target_root_observation=bad_target)


def _forge_classification(loaded: Modules, observation: object) -> object:
    """Claim the targets are current when the observed targets are absent."""
    return replace(observation, targets="current")


def _wrong_lock_path(loaded: Modules, observation: object) -> object:
    """Observe the coordination lock at a path other than the fixed one."""
    return replace(observation, coordination_lock_observation=_absent_lock(loaded.observer, "/codex/other.lock"))


@pytest.fixture
def loaded() -> Modules:
    """Load the approval kernel and its direct value authorities as fresh modules."""
    generator = load_module_registered("generate_roles", GENERATOR_PATH)
    lifecycle = load_module_registered("_agent_shim_lifecycle", LIFECYCLE_PATH)
    load_module_registered("_agent_shim_journal", JOURNAL_PATH)
    observer = load_module_registered("_agent_shim_observe", OBSERVER_PATH)
    planner = load_module_registered("_agent_shim_plan", PLAN_PATH)
    approval = load_module_registered("codex_rig_agent_shim_approval", APPROVAL_PATH)
    return Modules(generator, lifecycle, observer, planner, approval)


@pytest.fixture
def approval(loaded: Modules) -> ModuleType:
    """Return the approval kernel module."""
    return loaded.approval


@pytest.fixture
def runtime(approval: ModuleType) -> object:
    """Return an eligible doctor-supplied runtime binding."""
    return _runtime(approval)


@pytest.fixture
def install_evidence(loaded: Modules) -> tuple[object, object, object]:
    """Return matching roster, install candidate and observation for a host with no roots."""
    return _candidate_and_observation(loaded)


@pytest.fixture
def migration_evidence(loaded: Modules) -> tuple[object, object, object, dict[str, object]]:
    """Return roster, candidate, observation and prior state for a forward cache migration."""
    return _migration_candidate_and_observation(loaded)


@pytest.fixture
def initial_plan(approval: ModuleType, runtime: object, install_evidence: tuple) -> object:
    """Return the approval built for an initial install from matching evidence."""
    roster, candidate, observation = install_evidence
    return approval.build_convergence_approval(candidate, roster, observation, runtime)


@pytest.fixture
def initial_value(initial_plan: object) -> dict[str, object]:
    """Return the parsed canonical value of the initial-install approval."""
    return json.loads(initial_plan.canonical_bytes)


class TestApprovalDocument:
    """Group the exact bytes, fields and bindings a valid approval carries."""

    def test_digest_is_deterministic_over_canonical_bytes(
        self, approval: ModuleType, runtime: object, install_evidence: tuple, initial_plan: object
    ) -> None:
        """Building the same evidence twice yields equal approvals whose digest hashes the canonical bytes.

        The user approves a digest, so identical evidence must always reproduce identical bytes, and the digest must be
        exactly SHA-256 over them with no trailing newline.
        """
        roster, candidate, observation = install_evidence

        second = approval.build_convergence_approval(candidate, roster, observation, runtime)

        assert initial_plan == second
        assert set(vars(initial_plan)) == {"canonical_bytes", "digest"}
        assert initial_plan.digest == hashlib.sha256(initial_plan.canonical_bytes).hexdigest()
        assert not initial_plan.canonical_bytes.endswith(b"\n")
        approval.revalidate_approval_digest(initial_plan, initial_plan.digest)

    def test_top_level_fields_exact(self, initial_value: dict[str, object]) -> None:
        """The approval document has exactly the contracted top-level fields.

        An extra or missing field changes what the user approves, so the key set of a freshly built plan is pinned.
        """
        assert set(initial_value) == {
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

    def test_operation_fields_exact(self, initial_value: dict[str, object]) -> None:
        """Each approved operation has exactly the contracted before-and-after fields.

        Operations are what the apply step executes, so their key set is pinned for the first operation of an initial
        install.
        """
        assert set(initial_value["operations"][0]) == {
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

    def test_initial_install_observations(self, initial_value: dict[str, object]) -> None:
        """An initial install on a pristine host binds creation intents and absent observations.

        With no roots, no lock, no prior state and no recovery residue, the approval must say so explicitly instead of
        leaving the fields out.
        """
        assert initial_value["target_root_intent"] == "create"
        assert initial_value["source_state"] is None
        assert initial_value["coordination_lock_intent"] == "create-if-absent"
        assert initial_value["coordination_lock_observation"] == {
            "kind": "absent",
            "canonical_path": LOCK_PATH,
            "device": None,
            "inode": None,
            "owner": None,
            "group": None,
            "mode": None,
            "link_count": None,
            "size": None,
        }
        assert initial_value["target_root_observation"]["missing_suffix_components"] == ["agents"]
        assert initial_value["recovery_observation"] == {
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

    def test_existing_roots_keep_unchanged_intent(self, loaded: Modules, approval: ModuleType, runtime: object) -> None:
        """Existing target and state roots are not asked to be created again.

        When the exact roots already exist the approval must request no root creation and list no missing suffix.
        """
        roster, candidate, observation = _candidate_and_observation(loaded, roots_exist=True)

        plan = approval.build_convergence_approval(candidate, roster, observation, runtime)

        value = json.loads(plan.canonical_bytes)
        assert value["target_root_intent"] == "unchanged"
        assert value["target_root_observation"]["missing_suffix_components"] == []

    @pytest.mark.parametrize(
        ("target_inode", "expected"),
        [pytest.param(2, "unchanged", id="same-root"), pytest.param(99, "rebind-removed-root", id="replaced-root")],
    )
    def test_removed_tombstone_binds_target_root_identity(
        self, loaded: Modules, approval: ModuleType, runtime: object, target_inode: int, expected: str
    ) -> None:
        """An empty removed root that was replaced needs explicit rebind approval.

        After a complete remove the tombstone remembers the target root's identity; the same inode needs no change and a
        different inode must surface as a rebind the user approves.
        """
        roster, candidate, observation = _removed_candidate_and_observation(loaded, target_inode=target_inode)

        plan = approval.build_convergence_approval(candidate, roster, observation, runtime)

        assert json.loads(plan.canonical_bytes)["target_root_intent"] == expected

    def test_forward_migration_binds_source_and_historical_union(
        self, approval: ModuleType, runtime: object, migration_evidence: tuple
    ) -> None:
        """A forward cache migration binds the prior package evidence while the active cache root differs.

        Upgrading from an older cached plugin retires a role the new roster no longer has, so the approval carries the
        authenticated prior state, the active plugin root and a retire operation next to retained ones.
        """
        roster, candidate, observation, state = migration_evidence

        plan = approval.build_convergence_approval(candidate, roster, observation, runtime)

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
    def test_each_source_state_field_changes_digest(
        self, approval: ModuleType, runtime: object, migration_evidence: tuple, field: str, replacement: object
    ) -> None:
        """Every prior-state approval field is independently digest-significant.

        If editing one source-state field left the digest unchanged, the user could approve a plan whose prior-package
        evidence was swapped afterwards.
        """
        roster, candidate, observation, _ = migration_evidence
        plan = approval.build_convergence_approval(candidate, roster, observation, runtime)
        value = json.loads(plan.canonical_bytes)
        value["source_state"][field] = replacement

        assert hashlib.sha256(_encode(value)).hexdigest() != plan.digest

    def test_existing_lock_is_bound(
        self, loaded: Modules, approval: ModuleType, runtime: object, install_evidence: tuple
    ) -> None:
        """An existing coordination lock is bound by its device and inode.

        Apply must acquire the very lock file the user approved, so a regular lock observation is carried verbatim into
        the plan.
        """
        roster, candidate, observation = install_evidence
        existing_lock = loaded.observer.LockObservation("regular", LOCK_PATH, 1, 9, 501, 20, "0600", 1, 0)

        plan = approval.build_convergence_approval(
            candidate, roster, replace(observation, coordination_lock_observation=existing_lock), runtime
        )

        assert json.loads(plan.canonical_bytes)["coordination_lock_observation"] == {
            "kind": "regular",
            "canonical_path": LOCK_PATH,
            "device": 1,
            "inode": 9,
            "owner": 501,
            "group": 20,
            "mode": "0600",
            "link_count": 1,
            "size": 0,
        }


class TestApprovalRejections:
    """Group the stale, forged or ineligible evidence that must never yield an approval."""

    @pytest.mark.parametrize("status", ["unavailable", "overflow", "malformed", "unreadable"])
    def test_rejects_incomplete_namespace_inventory(
        self, approval: ModuleType, runtime: object, install_evidence: tuple, status: str
    ) -> None:
        """Each incomplete namespace scan status blocks approval.

        An approval is only safe when the whole agent namespace was read, so an unavailable, overflowing, malformed or
        unreadable scan must not be treated as empty.
        """
        roster, candidate, observation = install_evidence

        with pytest.raises(approval.ApprovalBindingError, match="approval-eligible"):
            approval.build_convergence_approval(
                candidate, roster, replace(observation, namespace_inventory_status=status), runtime
            )

    def test_rejects_unmanaged_namespace_evidence(
        self, loaded: Modules, approval: ModuleType, runtime: object, install_evidence: tuple
    ) -> None:
        """A complete scan that still reports an unmanaged namespace entry blocks approval.

        A look-alike shim file the plugin does not own must be reported to the user, not silently overwritten.
        """
        roster, candidate, observation = install_evidence
        unmanaged = (loaded.observer.NamespaceCandidateObservation("codex-rig-retired.toml", "regular"),)

        with pytest.raises(approval.ApprovalBindingError, match="approval-eligible"):
            approval.build_convergence_approval(
                candidate, roster, replace(observation, namespace_candidates=unmanaged), runtime
            )

    @pytest.mark.parametrize(
        ("tamper", "eligible"),
        [
            pytest.param(_keep, False, id="ineligible-runtime"),
            pytest.param(_block, True, id="blocked-classification"),
            pytest.param(_stale_first_target, True, id="stale-target"),
        ],
    )
    def test_rejects_ineligible_evidence(
        self, loaded: Modules, approval: ModuleType, install_evidence: tuple, tamper, eligible: bool
    ) -> None:
        """Non-eligible doctor, observer or target evidence cannot emit an approval.

        Each case starts from matching evidence and breaks one eligibility input: the live doctor verdict, the
        observer classification, or a target that changed since planning.
        """
        roster, candidate, observation = install_evidence

        with pytest.raises(approval.ApprovalBindingError):
            approval.build_convergence_approval(
                candidate, roster, tamper(loaded, observation), _runtime(approval, eligible=eligible)
            )

    def test_rejects_zero_write_candidate(self, loaded: Modules, approval: ModuleType, runtime: object) -> None:
        """A candidate whose operations are all noops cannot be approved.

        Removing from a pristine host converges without writes, so there is nothing for the user to approve.
        """
        roster, candidate, observation = _candidate_and_observation(loaded, action="remove")

        with pytest.raises(approval.ApprovalBindingError, match="zero-write"):
            approval.build_convergence_approval(candidate, roster, observation, runtime)

        assert all(operation.intent == "noop" for operation in candidate.operations)

    @pytest.mark.parametrize(
        ("tamper", "match"),
        [
            pytest.param(_forge_root_owner, "root owner", id="root-owner"),
            pytest.param(_forge_root_ancestry, "ancestry", id="root-ancestry"),
        ],
    )
    def test_rejects_inconsistent_root_evidence(
        self, loaded: Modules, approval: ModuleType, runtime: object, install_evidence: tuple, tamper, match: str
    ) -> None:
        """Forged root metadata and ancestry are rejected before canonicalization.

        A boolean masquerading as an owner id, or a missing root claimed below the wrong ancestor, would otherwise be
        serialized into an approval.
        """
        roster, candidate, observation = install_evidence

        with pytest.raises(approval.ApprovalBindingError, match=match):
            approval.build_convergence_approval(candidate, roster, tamper(loaded, observation), runtime)

    @pytest.mark.parametrize(
        ("tamper", "match"),
        [
            pytest.param(_forge_classification, "classification", id="classification"),
            pytest.param(_wrong_lock_path, "lock path", id="lock-path"),
        ],
    )
    def test_rejects_forged_state_classification_or_lock(
        self, loaded: Modules, approval: ModuleType, runtime: object, install_evidence: tuple, tamper, match: str
    ) -> None:
        """Classification claims and lock evidence not proven by observation are rejected.

        The observer states what it saw; a classification the targets do not support, or a lock at another path, must
        not reach the approval.
        """
        roster, candidate, observation = install_evidence

        with pytest.raises(approval.ApprovalBindingError, match=match):
            approval.build_convergence_approval(candidate, roster, tamper(loaded, observation), runtime)

    def test_rejects_incomplete_historical_union(
        self, approval: ModuleType, runtime: object, migration_evidence: tuple
    ) -> None:
        """A retired prior target missing from the observations blocks approval.

        Migration must account for every role in the prior state, so dropping the retired role's target observation
        looks like an incomplete roster rather than a clean upgrade.
        """
        roster, candidate, observation, _ = migration_evidence
        incomplete = tuple(
            item for item in observation.target_observations if item[0] != "codex-rig-retired-specialist.toml"
        )

        with pytest.raises(approval.ApprovalBindingError, match=r"rebuilt|rosters"):
            approval.build_convergence_approval(
                candidate, roster, replace(observation, target_observations=incomplete), runtime
            )

    def test_rejects_semantically_forged_candidate(
        self, approval: ModuleType, runtime: object, install_evidence: tuple
    ) -> None:
        """Self-consistent candidate bytes whose action contradicts their operations are rejected.

        The forged candidate has a valid digest over its own bytes, so only an authoritative rebuild from the evidence
        can notice that an install plan was relabelled as a remove.
        """
        roster, candidate, observation = install_evidence
        value = json.loads(candidate.canonical_bytes)
        value["action"] = "remove"
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        forged = replace(
            candidate, action="remove", canonical_bytes=canonical, digest=hashlib.sha256(canonical).hexdigest()
        )

        with pytest.raises(approval.ApprovalBindingError, match="authoritative rebuild"):
            approval.build_convergence_approval(forged, roster, observation, runtime)
