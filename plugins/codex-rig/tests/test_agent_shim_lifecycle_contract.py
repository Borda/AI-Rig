"""Acceptance checks for the public thin-shim lifecycle contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PLUGIN_ROOT / "runtime" / "agent-shim-lifecycle.json"
TEST_REQUIREMENTS_PATH = PLUGIN_ROOT / "tests" / "requirements.txt"
EXPECTED_ROLES = {
    "challenger",
    "cicd-steward",
    "curator",
    "data-steward",
    "delegation-lead",
    "doc-scribe",
    "linting-expert",
    "oss-shepherd",
    "qa-specialist",
    "scientist",
    "security-auditor",
    "solution-architect",
    "squeezer",
    "sw-engineer",
    "web-explorer",
}


def _load_contract() -> dict[str, object]:
    """Load the lifecycle contract as one JSON object."""
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_public_grammar_scope_and_exit_contract_are_frozen() -> None:
    """Prevent lifecycle scope or user interaction from widening silently."""
    contract = _load_contract()

    assert contract["schema"] == 1
    assert contract["release"] == "0.2.0"
    assert contract["public_actions"] == {
        "arguments": "exactly one positional action and no extras",
        "actions": ["doctor", "status", "install", "remove"],
        "read_only": ["doctor", "status"],
        "whole_roster_mutations": ["install", "remove"],
        "approval": "install and remove require explicit approval of the exact canonical plan digest",
        "fresh_session_after_mutation": True,
        "forbidden": ["subset mutation", "force", "adoption", "overwrite unowned", "noninteractive approval bypass"],
    }
    assert contract["exit_codes"] == {
        "0": "success or already converged",
        "2": "usage error",
        "3": "user cancelled before mutation",
        "4": "drift, conflict, or blocked or partial removal",
        "5": "environment or active-plugin prerequisite blocked",
        "6": "corrupt, future, inconsistent, or untrusted state",
        "7": "internal failure or failed recovery",
    }
    assert contract["scope"]["name"] == "user"
    assert contract["scope"]["target_root"] == "<CODEX_HOME>/agents"
    assert contract["scope"]["state_root"] == "<CODEX_HOME>/codex-rig/shims"
    assert contract["scope"]["coordination_lock"] == "<CODEX_HOME>/.codex-rig-shims.lock"
    assert {"project scope", "native Windows", "network or distributed filesystems"} <= set(
        contract["scope"]["unsupported"]
    )


def test_exact_whole_roster_and_target_names_are_frozen() -> None:
    """Prevent subset behavior, filename drift, or namespace-only cleanup."""
    contract = _load_contract()
    roles = contract["roles"]
    role_ids = [item["id"] for item in roles]
    targets = [item["target"] for item in roles]

    assert set(role_ids) == EXPECTED_ROLES
    assert role_ids == sorted(role_ids)
    assert len(targets) == len(set(targets)) == 15
    assert targets == [f"codex-rig-{role_id}.toml" for role_id in role_ids]
    assert contract["marker"]["ownership_rule"].startswith("marker and filename never prove ownership")
    assert contract["safe_removal_predicates"] == [
        "target name is derived from the exact canonical role allowlist",
        "target is a contained regular file reached without following symlinks",
        "valid state matches plugin, scope, install ID, package, roots, bootstrap, role, card, and full file hash",
        "line-one marker matches the same valid state",
        "complete current file hash matches the state file hash",
        "under-lock observation matches the approved plan",
    ]


def test_marker_state_and_input_bounds_are_complete() -> None:
    """Prevent ambiguous provenance parsing or unbounded lifecycle inputs."""
    contract = _load_contract()
    marker = contract["marker"]
    state = contract["state"]

    assert marker["line"] == 1
    assert marker["encoding"] == "UTF-8 without BOM"
    assert marker["newline"] == "LF"
    assert marker["maximum_bytes"] == 1024
    assert marker["grammar"].startswith("# codex-rig-shim schema=1 plugin=codex-rig")
    assert set(marker["required_fields"]) == {
        "schema",
        "plugin",
        "install_id",
        "role_id",
        "package_hash",
        "role_hash",
        "bootstrap",
        "generator",
    }
    assert state["schema"] == 1
    assert state["mode"] == "0600"
    assert state["directory_mode"] == "0700"
    assert state["extra_fields"] == "rejected at every object level"
    assert state["ownership_reconstruction"] is False
    assert state["state_root_rebind"] is False
    assert "explicitly approved rebind-removed-root" in state["removed_tombstone_rebind"]
    assert state["field_schema"]["roles"].startswith("array of one through 128 entries sorted")
    assert set(state["root_identity_fields"]) == {
        "canonical_path",
        "device",
        "inode",
        "owner",
        "group",
        "mode",
    }
    assert set(state["role_fields"]) == {"role_id", "target_name", "card_path", "role_hash", "file_hash"}
    assert state["role_schema"]["target_name"].startswith("derived basename exactly codex-rig-")
    assert any("same precedence" in refusal for refusal in state["compatibility_refusals"])
    assert any("historical roster" in refusal for refusal in state["compatibility_refusals"])
    assert "authenticate the persisted package" in state["forward_migration"]
    assert all(isinstance(value, int) and value > 0 for value in contract["bounded_inputs"].values())
    assert {
        key: contract["bounded_inputs"][key]
        for key in (
            "state_roles",
            "operation_roles",
            "target_directory_entries",
            "recovery_directory_entries",
        )
    } == {
        "state_roles": 128,
        "operation_roles": 256,
        "target_directory_entries": 4096,
        "recovery_directory_entries": 256,
    }


def test_target_matrix_preserves_every_untrusted_or_modified_file() -> None:
    """Prevent adoption, marker-only removal, or partial whole-roster mutation."""
    contract = _load_contract()
    matrix = {item["state"]: item for item in contract["target_classification"]}

    assert matrix["regular target without matching ownership state"] == {
        "state": "regular target without matching ownership state",
        "install": "preserve and block whole roster",
        "remove": "preserve and refuse ownership claim",
    }
    assert matrix["owned marker but full file hash differs"] == {
        "state": "owned marker but full file hash differs",
        "install": "preserve and block whole roster",
        "remove": "preserve and report modified",
    }
    for unsafe in (
        "missing, corrupt, future, or inconsistent state",
        "symlink, non-regular, escaped, aliased, or case-colliding target",
    ):
        assert "preserve" in matrix[unsafe]["install"]
        assert "preserve" in matrix[unsafe]["remove"]
    repaired = matrix["valid current state with one or more owned targets absent and every present owned target exact"]
    assert repaired["install"] == "transactionally repair missing owned targets after whole-roster approval"
    assert repaired["remove"] == ("transactionally remove exact present owned targets and commit a removed tombstone")
    assert matrix["valid removed tombstone with every allowlisted target absent"]["remove"] == (
        "zero-write converged success"
    )
    assert matrix["valid removed tombstone with any allowlisted target present"]["install"] == (
        "preserve and block whole roster"
    )


class TestLifecycleApprovalContract:
    """Group approval-plan schema, digest, and intent contracts."""

    def test_has_identity_and_operation_bounds(self) -> None:
        """Keep approval inputs, observations, and bounds exact."""
        approval = _load_contract()["approval_plan"]

        assert approval["encoding"].startswith("json.dumps with ensure_ascii=true")
        assert approval["digest"].startswith("SHA-256 over the complete canonical plan bytes")
        assert approval["hash_preimages"] == {
            "package_hash": "exact package-manifest.json bytes",
            "bootstrap_hash": "exact scripts/verify_role_link.py bytes",
            "role_hash": "exact ROLE.md bytes",
            "file_hash": "exact complete generated shim bytes including final LF",
            "roster_hash": (
                "canonical JSON array sorted by role_id; each exact object contains role_id, target_name, card_path, "
                "and role_hash"
            ),
        }
        assert "apply receives both as immutable approved inputs" in approval["generated_identifiers"]
        assert approval["operation_order"].startswith("one through 256 unique operations")
        assert approval["intent_values"] == ["noop", "create", "update", "repair-missing", "retire", "remove"]
        assert approval["target_root_intent_values"] == ["unchanged", "create", "rebind-removed-root"]
        assert "JSON null" in approval["absent_value"]
        assert {
            "transaction_nonce",
            "package_hash",
            "bootstrap_hash",
            "roster_hash",
            "source_state",
            "operations",
            "codex_home_observation",
            "coordination_lock_intent",
            "target_root_observation",
            "state_root_observation",
            "python_executable_hash",
            "codex_binary_hash",
            "journal_observation",
            "recovery_observation",
            "recovery_disposition",
        } <= set(approval["required_fields"])

    def test_has_root_observation_schema(self) -> None:
        """Keep existing and not-yet-created root observations exact."""
        approval = _load_contract()["approval_plan"]

        assert set(approval["root_observation_fields"]) == {
            "exists",
            "canonical_path",
            "nearest_existing_ancestor_path",
            "nearest_existing_ancestor_device",
            "nearest_existing_ancestor_inode",
            "nearest_existing_ancestor_owner",
            "nearest_existing_ancestor_group",
            "nearest_existing_ancestor_mode",
            "missing_suffix_components",
            "device",
            "inode",
            "owner",
            "group",
            "mode",
        }
        assert approval["root_observation_schema"] == {
            "exists": "JSON boolean",
            "canonical_path": "absolute normalized UTF-8 path string for the intended root",
            "nearest_existing_ancestor_path": "absolute canonical UTF-8 path string",
            "nearest_existing_ancestor_device": "nonnegative JSON integer",
            "nearest_existing_ancestor_inode": "nonnegative JSON integer",
            "nearest_existing_ancestor_owner": "nonnegative JSON integer",
            "nearest_existing_ancestor_group": "nonnegative JSON integer",
            "nearest_existing_ancestor_mode": "four-character lowercase octal permission string",
            "missing_suffix_components": (
                "JSON array of zero or more nonempty UTF-8 basename strings in creation order; dot, dot-dot, slash, "
                "NUL, and control characters rejected"
            ),
            "device": "nonnegative JSON integer when exists=true, otherwise JSON null",
            "inode": "nonnegative JSON integer when exists=true, otherwise JSON null",
            "owner": "nonnegative JSON integer when exists=true, otherwise JSON null",
            "group": "nonnegative JSON integer when exists=true, otherwise JSON null",
            "mode": "four-character lowercase octal permission string when exists=true, otherwise JSON null",
        }

    def test_has_recovery_observation_schema(self) -> None:
        """Keep recovery intent and evidence fields complete and bounded."""
        approval = _load_contract()["approval_plan"]

        assert approval["coordination_lock_intent_values"] == ["open-existing", "create-if-absent"]
        assert "refreshes every non-lock observation" in approval["coordination_lock_intent_rule"]
        assert approval["recovery_disposition_values"] == [
            None,
            "rollback",
            "finalize-bookkeeping",
            "clean-preparing",
            "clean-probe",
            "clean-empty-retirement",
            "clean-empty-probe",
        ]
        assert "committed state file and every complete target equal" in approval["recovery_disposition_rule"]
        assert approval["recovery_observation_kinds"] == [
            None,
            "preparing-residue",
            "journal",
            "probe-receipt",
            "empty-transaction",
            "empty-probe",
        ]
        assert set(approval["recovery_observation_schema"]) == set(approval["recovery_observation_fields"])
        assert approval["recovery_entry_schema"]["sha256"] == (
            "64-character lowercase SHA-256 string for a readable regular file; JSON null only for a directory or "
            "the sole umask-reduced unreadable journal.initial.json preparing residue"
        )
        assert set(approval["operation_schema"]) == set(approval["operation_fields"])

    def test_binds_digest_and_apply_inputs(self) -> None:
        """Keep canonical bytes and approved apply inputs immutable."""
        approval = _load_contract()["approval_plan"]

        vector = approval["canonical_test_vector"]
        vector_bytes = json.dumps(
            vector["value"],
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert vector_bytes.decode() == vector["utf8"]
        assert hashlib.sha256(vector_bytes).hexdigest() == vector["sha256"]
        assert set(approval["operation_fields"]) == {
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
        assert "reuse the approved identifiers" in approval["apply_rule"]
        assert "preserve the approved coordination_lock_intent" in approval["apply_rule"]
        assert "recovery completes alone" in approval["recovery_plan_rule"]

    def test_has_top_level_schema(self) -> None:
        """Keep every top-level field and nested schema link exact."""
        approval = _load_contract()["approval_plan"]

        assert approval["top_level_field_schema"] == {
            "schema": "JSON integer exactly 1",
            "action": "string exactly install or remove",
            "scope": "string exactly user",
            "mode": "string exactly converge or recovery",
            "target_root_intent": "string exactly unchanged, create, or rebind-removed-root",
            "recovery_disposition": (
                "JSON null in converge mode; otherwise one exact non-null recovery_disposition_values string"
            ),
            "codex_home_observation": "object with exactly root_observation_fields and root_observation_schema",
            "coordination_lock_intent": "string exactly open-existing or create-if-absent",
            "coordination_lock_observation": (
                "object with exactly coordination_lock_observation_fields and coordination_lock_observation_schema"
            ),
            "target_root_observation": "object with exactly root_observation_fields and root_observation_schema",
            "state_root_observation": "object with exactly root_observation_fields and root_observation_schema",
            "canonical_target_root": "absolute normalized UTF-8 path exactly <CODEX_HOME>/agents",
            "canonical_state_root": "absolute normalized UTF-8 path exactly <CODEX_HOME>/codex-rig/shims",
            "canonical_plugin_root": "absolute canonical UTF-8 path of the one verified installed plugin root",
            "plugin_root_identity": "object with exactly state.root_identity_fields and state.root_identity_schema",
            "plugin_version": "non-empty SemVer string equal to the verified package and active-plugin versions",
            "package_hash": "64-character lowercase SHA-256 over exact package-manifest.json bytes",
            "bootstrap_protocol": "JSON integer exactly 1",
            "bootstrap_path": "package-relative string exactly scripts/verify_role_link.py",
            "bootstrap_hash": "64-character lowercase SHA-256 over exact verifier helper bytes",
            "python_executable_path": "absolute canonical UTF-8 path to the held verified regular executable",
            "python_executable_hash": "64-character lowercase SHA-256 over exact Python executable bytes",
            "codex_binary_path": "absolute canonical UTF-8 path to the held verified regular executable",
            "codex_binary_hash": "64-character lowercase SHA-256 over exact Codex executable bytes",
            "generator_version": "JSON integer exactly 1",
            "install_id": "canonical lowercase RFC 4122 UUID string",
            "transaction_nonce": "canonical lowercase RFC 4122 UUID string generated once before approval",
            "roster_hash": "64-character lowercase SHA-256 over the exact hash_preimages.roster_hash preimage",
            "source_state": (
                "JSON null for an initial install; otherwise an object with exactly source_state_fields and "
                "source_state_schema"
            ),
            "journal_observation": "object with exactly journal_observation_fields and journal_observation_schema",
            "recovery_observation": "object with exactly recovery_observation_fields and recovery_observation_schema",
            "operations": "array of one through 256 objects with exactly operation_fields and operation_schema",
        }
        assert set(approval["top_level_field_schema"]) == set(approval["required_fields"])
        assert approval["source_state_fields"] == [
            "sha256",
            "transition",
            "plugin_version",
            "package_hash",
            "bootstrap",
            "generator_version",
            "roster_hash",
            "plugin_root_identity",
        ]
        assert set(approval["source_state_schema"]) == set(approval["source_state_fields"])
        assert set(approval["coordination_lock_observation_schema"]) == set(
            approval["coordination_lock_observation_fields"]
        )

    def test_enforces_cross_field_rules(self) -> None:
        """Keep identities and package inputs mutually consistent."""
        approval = _load_contract()["approval_plan"]

        assert approval["top_level_cross_field_rules"] == {
            "action_scope": "action is install or remove and scope is user; no other action or scope is representable",
            "mode": (
                "mode is converge exactly when recovery_observation.kind and recovery_disposition are JSON null; mode is "
                "recovery exactly when one recognized recovery observation and its required non-null disposition are bound"
            ),
            "paths": (
                "canonical target and state roots are exact descendants of the observed canonical Codex home; the plugin "
                "root and both executable paths equal the paths used for every bound hash and generated byte"
            ),
            "identities": (
                "plugin_root_identity equals the held canonical plugin root; each existing root observation equals its held "
                "device, inode, owner, group, and mode; an absent root binds its nearest existing ancestor and ordered suffix; "
                "an existing coordination lock binds the fixed-path device and inode that apply must acquire"
            ),
            "package": (
                "top-level plugin_version, package_hash, bootstrap protocol, bootstrap path, bootstrap hash, generator "
                "version, every after-role hash, and roster hash come from one verified active package identity; non-null "
                "source_state separately binds the authenticated prior package"
            ),
            "executables": (
                "Python and Codex paths are absolute canonical held regular executable files and each supplied hash equals "
                "the complete bytes read from that same descriptor"
            ),
            "install_id": (
                "a valid state supplies its unchanged install_id; absent state uses the one newly generated approved "
                "install_id; recovery uses the artifact-bound install_id"
            ),
            "transaction_nonce": (
                "one new nonce is generated exactly once for convergence or copied exactly from the recognized recovery "
                "artifact when recovery requires that transaction identity"
            ),
            "operations": (
                "operations use every role in the sorted active and authenticated persisted union exactly once; action, "
                "mode, state, target observations, generated bytes, and operation_intent_consistency determine every "
                "before and after field"
            ),
        }

    def test_rejects_invalid_operation_intents(self) -> None:
        """Keep action-specific before-to-after intent rules exact."""
        approval = _load_contract()["approval_plan"]

        assert approval["operation_intent_consistency"] == {
            "noop": (
                "before and after are both absent with null hashes and modes, or both present with identical hashes and "
                "modes; no filesystem publication, detachment, removal, or state change is attributed to this operation"
            ),
            "create": (
                "install convergence only; before is absent with null hash and mode, after is present with the exact "
                "generated file hash and mode 0600, and no valid current ownership record names the absent target"
            ),
            "update": (
                "install convergence only; before is present with the exact valid-state file hash and observed mode, after "
                "is present with the exact generated file hash and mode 0600, and before_hash differs from after_hash"
            ),
            "repair-missing": (
                "install convergence only; valid current state names the role, before is absent with null hash and mode, "
                "and after is present with the exact generated file hash and mode 0600"
            ),
            "retire": (
                "install convergence only; authenticated prior state names a role absent from the active roster, before "
                "is present with the exact valid-state file hash and observed mode, and after is absent with null hash and "
                "mode"
            ),
            "remove": (
                "remove convergence only; before is present with the exact valid-state file hash and observed mode, after "
                "is absent with null hash and mode, and the complete roster commits a removed tombstone"
            ),
        }
        assert approval["action_intent_rule"] == {
            "install": ["noop", "create", "update", "repair-missing", "retire"],
            "remove": ["noop", "remove"],
            "recovery": "uses the artifact-bound operation intents and does not add a convergence operation",
        }


class TestLifecycleTransactionContract:
    """Group durable transaction, probe, and retirement contracts."""

    def test_has_lock_and_journal_shape(self) -> None:
        """Keep lock validation and durable journal fields exact."""
        transaction = _load_contract()["transaction"]

        assert transaction["lock"].startswith("one persistent 0600 coordination file")
        assert "link count one" in transaction["lock_validation"]
        assert "blocks without replacement" in transaction["lock_validation"]
        assert "fixed coordination lock" in transaction["preliminary_write_rule"]
        assert "share one device" in transaction["root_creation_rule"]
        assert transaction["journal_states"] == [
            "PREPARING",
            "PREPARED",
            "MUTATING",
            "STATE_COMMITTED",
            "COMMITTED",
            "RECOVERY_REQUIRED",
            "ROLLED_BACK",
        ]
        assert (
            transaction["journal_path"] == "transactions/<transaction_id>/journal.json under the canonical state root"
        )
        assert set(transaction["journal_required_fields"]) == {
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
        assert transaction["journal_identity_schema"]["transaction_id"].startswith("the exact transaction_nonce string")
        assert set(transaction["journal_operation_schema"]) == set(transaction["journal_operation_fields"])
        assert set(transaction["journal_operation_fields"]) == {
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
        assert transaction["journal_operation_progress"] == ["PLANNED", "DETACHED", "PUBLISHED", "VERIFIED"]
        assert "noop operation is written as VERIFIED" in transaction["journal_noop_progress"]
        assert transaction["journal_rollback_state_progress_schema"].startswith(
            "one exact rollback_state_progress_values string"
        )

    def test_orders_mutation_phases(self) -> None:
        """Keep mutation order and no-clobber primitives exact."""
        transaction = _load_contract()["transaction"]

        assert transaction["phases"] == [
            "read-only whole-roster preflight",
            "explicit digest approval",
            "open or create the fixed coordination file and acquire its nonblocking advisory lock",
            "directory-handle no-follow revalidation with immutable approved identifiers and byte-for-byte plan equality",
            "create or verify roots and run nonce-bound private filesystem probes",
            "read exact before-images and generate desired bytes in memory without named transaction artifacts",
            "fsynced PREPARING journal publication through journal.initial.json and journal.json",
            "write and verify every journal-listed artifact, then fsync PREPARED",
            "set and fsync MUTATING before canonical role-order no-clobber create or detach-and-verify update or remove",
            "persist and fsync each operation progress transition",
            "exact roster verification",
            "atomic fsynced committed state publication",
            "set and fsync STATE_COMMITTED after the state file and its parent are durable",
            "fsynced COMMITTED journal",
            "individual cleanup of recognized ephemeral files",
        ]
        assert "fails when the target exists" in transaction["create_primitive"]
        assert "verify approved hash" in transaction["update_remove_primitive"]
        assert "restore those exact detached bytes" in transaction["detach_mismatch_rule"]
        assert "never publish the journal before-image" in transaction["detach_mismatch_rule"]
        assert "journal.next.json with exclusive create" in transaction["journal_update_primitive"]
        assert "journal.json remains the authority" in transaction["journal_temp_recovery"]

    def test_tracks_probe_cleanup(self) -> None:
        """Keep probe receipt progress and cleanup recoverable."""
        transaction = _load_contract()["transaction"]

        assert transaction["probe_directory"].startswith("<state_root>/.probe-<transaction_nonce>")
        assert transaction["probe_artifacts"] == ["source", "published", "replacement", "fsync-file", "fsync-directory"]
        assert "valid residue is recoverable only through an approved clean-probe" in transaction["probe_receipt"]
        assert set(transaction["probe_receipt_identity_schema"]) == set(transaction["probe_receipt_required_fields"])
        assert transaction["probe_receipt_extra_fields"].startswith("rejected")
        assert set(transaction["probe_receipt_artifact_schema"]) == set(transaction["probe_receipt_artifact_fields"])
        assert transaction["probe_artifact_kinds"]["fsync-directory"] == "directory"
        assert transaction["probe_receipt_progress"] == ["PREPARED", "RUNNING", "CLEANING", "CLEANUP_REQUIRED"]
        assert transaction["probe_artifact_progress"] == ["PLANNED", "PUBLISHED", "VERIFIED", "REMOVED"]
        assert "probe.next.json with exclusive create" in transaction["probe_receipt_update_primitive"]
        assert "PLANNED plus absent is expected and persists REMOVED" in transaction["probe_cleanup_progress_rule"]
        assert "PLANNED, PUBLISHED, or VERIFIED plus an exact present" in transaction["probe_cleanup_progress_rule"]
        assert "persists the missed REMOVED transition" in transaction["probe_cleanup_progress_rule"]
        assert "approved clean-empty-probe directory" in transaction["probe_retirement"]

    def test_has_resumable_retirement(self) -> None:
        """Keep commit and rollback retirement resumable."""
        transaction = _load_contract()["transaction"]

        assert transaction["transaction_retirement_order"] == [
            "remove remaining exact journal-listed artifact files including state.publish.json when present",
            "remove valid journal.next.json if present",
            "remove every empty allowlisted transaction subdirectory while valid COMMITTED journal.json remains",
            "remove exact valid COMMITTED journal.json",
            "remove the now-empty transaction directory",
        ]
        assert "never remove journal.json while any child entry remains" in transaction["transaction_retirement"]
        assert transaction["rollback_retirement_order"] == [
            "require journal state ROLLED_BACK, rollback state progress RESTORED, every operation TARGET_RESTORED, the exact durable before-state, and no unknown transaction entry",
            "remove each remaining exact authenticated operation and state snapshot artifact plus state.publish.json in canonical recorded order, accepting prior absence only under this same terminal journal",
            "remove valid journal.next.json if present",
            "remove every empty allowlisted transaction subdirectory while valid ROLLED_BACK journal.json remains",
            "remove exact valid ROLLED_BACK journal.json",
            "remove the now-empty transaction directory",
        ]
        assert "after ROLLED_BACK" in transaction["rollback_retirement"]
        assert "resumes at the first remaining artifact" in transaction["rollback_retirement"]
        assert {"recursive delete", "replace of an unowned target", "stale-lock takeover"} <= set(
            transaction["forbidden_primitives"]
        )

    def test_has_durable_successors(self) -> None:
        """Keep journal and operation successor states monotonic."""
        transaction = _load_contract()["transaction"]

        assert transaction["journal_state_successors"] == {
            "PREPARING": ["PREPARED", "RECOVERY_REQUIRED"],
            "PREPARED": ["MUTATING", "RECOVERY_REQUIRED"],
            "MUTATING": ["MUTATING", "STATE_COMMITTED", "RECOVERY_REQUIRED"],
            "STATE_COMMITTED": ["COMMITTED", "RECOVERY_REQUIRED"],
            "COMMITTED": [],
            "RECOVERY_REQUIRED": ["RECOVERY_REQUIRED", "ROLLED_BACK"],
            "ROLLED_BACK": [],
        }
        assert transaction["journal_successor_rule"] == (
            "a successor preserves schema, transaction, install, action, approved digest, package, roster, root identities, "
            "before state, after state, operation order, operation intent, and artifact names; it changes only along one listed "
            "journal state, operation progress, operation rollback progress, or rollback state progress successor and never "
            "decreases durable progress"
        )
        assert transaction["journal_operation_progress_successors"] == {
            "noop": {"VERIFIED": []},
            "create": {"PLANNED": ["PUBLISHED"], "PUBLISHED": ["VERIFIED"], "VERIFIED": []},
            "repair-missing": {"PLANNED": ["PUBLISHED"], "PUBLISHED": ["VERIFIED"], "VERIFIED": []},
            "update": {
                "PLANNED": ["DETACHED"],
                "DETACHED": ["PUBLISHED"],
                "PUBLISHED": ["VERIFIED"],
                "VERIFIED": [],
            },
            "remove": {"PLANNED": ["DETACHED"], "DETACHED": ["VERIFIED"], "VERIFIED": []},
            "retire": {"PLANNED": ["DETACHED"], "DETACHED": ["VERIFIED"], "VERIFIED": []},
        }

    def test_recognizes_exact_crash_observations(self) -> None:
        """Keep intent-specific recoverable observations exact."""
        transaction = _load_contract()["transaction"]

        assert transaction["journal_operation_crash_observations"] == {
            "noop": {"VERIFIED": ["the exact unchanged before and after observation"]},
            "create": {
                "PLANNED": ["target absent", "target equals exact after image"],
                "PUBLISHED": ["target equals exact after image"],
                "VERIFIED": ["target equals exact after image"],
            },
            "repair-missing": {
                "PLANNED": ["target absent", "target equals exact after image"],
                "PUBLISHED": ["target equals exact after image"],
                "VERIFIED": ["target equals exact after image"],
            },
            "update": {
                "PLANNED": [
                    "target equals exact before image and quarantine absent",
                    "target absent and quarantine equals exact before image",
                ],
                "DETACHED": [
                    "target absent and quarantine equals exact before image",
                    "target equals exact after image and quarantine equals exact before image",
                ],
                "PUBLISHED": ["target equals exact after image and quarantine equals exact before image"],
                "VERIFIED": ["target equals exact after image and quarantine equals exact before image"],
            },
            "remove": {
                "PLANNED": [
                    "target equals exact before image and quarantine absent",
                    "target absent and quarantine equals exact before image",
                ],
                "DETACHED": ["target absent and quarantine equals exact before image"],
                "VERIFIED": ["target absent and quarantine equals exact before image"],
            },
            "retire": {
                "PLANNED": [
                    "target equals exact before image and quarantine absent",
                    "target absent and quarantine equals exact before image",
                ],
                "DETACHED": ["target absent and quarantine equals exact before image"],
                "VERIFIED": ["target absent and quarantine equals exact before image"],
            },
        }
        assert transaction["crash_observation_rule"] == (
            "journal progress is a durable lower bound because a crash may occur after a target or quarantine mutation is "
            "durable but before its successor journal is durable; only the listed current-or-next physical observations "
            "are recognized, and every mismatch or observation farther ahead preserves evidence and requires failed recovery"
        )

    def test_restores_monotonic_progress(self) -> None:
        """Keep rollback progress monotonic through restored state."""
        transaction = _load_contract()["transaction"]

        assert transaction["journal_operation_rollback_progress"] == ["NOT_STARTED", "TARGET_RESTORED"]
        assert transaction["journal_operation_rollback_progress_successors"] == {
            "NOT_STARTED": ["TARGET_RESTORED"],
            "TARGET_RESTORED": [],
        }
        assert transaction["rollback_state_progress_values"] == ["PENDING", "RESTORED"]
        assert transaction["rollback_state_progress_successors"] == {"PENDING": ["RESTORED"], "RESTORED": []}
        assert "exact restored observation one transition ahead" in transaction["rollback_progress_crash_rule"]
        assert "persists the missed RESTORED successor" in transaction["rollback_state_progress_crash_rule"]
        assert transaction["rollback_state_progress_rule"].endswith("persist journal state ROLLED_BACK")


class TestLifecycleRecoveryContract:
    """Group fail-closed rollback and pristine outcome contracts."""

    def test_fails_closed(self) -> None:
        """Keep recovery evidence-preserving and idempotent."""
        recovery = _load_contract()["recovery"]

        assert "never forward-resume mutation" in recovery["policy"]
        assert recovery["idempotent"] is True
        assert "never clobber" in recovery["concurrent_target_rule"]
        assert "exact empty-retirement residue" in recovery["entry"]
        assert "exact empty-probe residue" in recovery["entry"]
        assert "canonical recovery_observation" in recovery["approval_binding"]
        assert "clean-empty-retirement and clean-empty-probe" in recovery["mutation_behavior"]
        assert "exact durable after-state" in recovery["mutation_behavior"]
        assert "persists ROLLED_BACK before canonical resumable retirement" in recovery["mutation_behavior"]
        assert recovery["post_recovery"].endswith("the user reruns install or remove")

    def test_has_ordered_decision_matrix(self) -> None:
        """Keep exact rollback rows and fallback priority frozen."""
        recovery = _load_contract()["recovery"]

        assert recovery["rollback_decision_matrix"] == [
            {
                "priority": 1,
                "target": "absent",
                "before": "absent",
                "after": "absent or exact after image",
                "quarantine": "absent",
                "decision": "before state is already restored; perform no target mutation",
            },
            {
                "priority": 2,
                "target": "absent",
                "before": "exact before image",
                "after": "absent or exact after image",
                "quarantine": "exact before image",
                "decision": (
                    "publish the exact quarantine before image to the still-absent target with no-clobber semantics, verify "
                    "and fsync it, and preserve the quarantine link and every listed recovery artifact until ROLLED_BACK"
                ),
            },
            {
                "priority": 3,
                "target": "exact before image",
                "before": "exact before image",
                "after": "absent or exact after image",
                "quarantine": "absent or exact before image",
                "decision": (
                    "before state is already restored; preserve the target, the exact duplicate quarantine, and every "
                    "listed recovery artifact unchanged until ROLLED_BACK"
                ),
            },
            {
                "priority": 4,
                "target": "exact after image",
                "before": "absent",
                "after": "exact after image",
                "quarantine": "absent",
                "decision": "remove only the verified exact after image durably to restore target absence",
            },
            {
                "priority": 5,
                "target": "exact after image",
                "before": "exact before image",
                "after": "exact after image",
                "quarantine": "exact before image",
                "decision": (
                    "detach and verify only the exact after target, publish the exact quarantine before image with "
                    "no-clobber semantics, verify and fsync the restored target, and preserve the quarantine link and every "
                    "listed recovery artifact until ROLLED_BACK"
                ),
            },
            {
                "priority": 6,
                "target": "absent",
                "before": "exact before image",
                "after": "absent or exact after image",
                "quarantine": "absent",
                "decision": (
                    "restore only from the exact authenticated journal before-image with no-clobber semantics; missing or "
                    "mismatched before-image evidence preserves all evidence and fails recovery"
                ),
            },
            {
                "priority": 7,
                "target": "any observation not matched by an earlier exact row",
                "before": "any",
                "after": "any",
                "quarantine": "any observation not matched by an earlier exact row",
                "decision": "preserve the target and every recovery artifact unchanged and remain recovery-required",
            },
        ]
        priorities = [row["priority"] for row in recovery["rollback_decision_matrix"]]
        assert priorities == list(range(1, 8))

    def test_selects_one_terminal_path(self) -> None:
        """Keep rollback selection and terminal behavior deterministic."""
        recovery = _load_contract()["recovery"]

        assert recovery["rollback_matrix_selection_rule"] == (
            "evaluate rows by ascending unique priority; exact rows 1 through 6 are mutually exclusive; the final "
            "catch-all is selected only when no exact row matches; exactly one row is effective for every complete "
            "observation"
        )
        assert recovery["rollback_order_rule"] == (
            "evaluate every role in canonical role order from held descriptors, select exactly one effective matrix row "
            "for every role before any mutation, stop when any role selects the preservation catch-all, and never use "
            "namespace, marker, or a later package as substitute ownership evidence"
        )
        assert recovery["rollback_terminal_rule"] == (
            "after every role is durably TARGET_RESTORED and every recovery artifact remains available, restore and verify "
            "the exact authenticated before-state, persist rollback state progress RESTORED, persist journal state "
            "ROLLED_BACK, then execute only canonical resumable rollback_retirement; any failure before ROLLED_BACK "
            "preserves the journal and all recovery evidence in RECOVERY_REQUIRED"
        )
        assert recovery["approval_completion_rule"] == (
            "an approved recovery performs only the bound rollback, bookkeeping finalization, probe cleanup, or empty "
            "directory retirement; it returns after that recovery outcome and never continues the originally requested "
            "install or remove convergence"
        )

    def test_keeps_pristine_paths_zero_write(self) -> None:
        """Keep pristine doctor, status, and remove outcomes zero-write."""
        contract = _load_contract()

        assert contract["pristine_exit_outcomes"] == {
            "doctor": {
                "precondition": (
                    "no lifecycle state, allowlisted target, or recovery residue exists and every live read-only prerequisite "
                    "passes"
                ),
                "result": "healthy with approved-apply probes pending",
                "exit_code": 0,
                "writes": False,
            },
            "status": {
                "precondition": "no lifecycle state, allowlisted target, or recovery residue exists",
                "result": "absent and converged",
                "exit_code": 0,
                "writes": False,
            },
            "remove": {
                "precondition": "no lifecycle state, allowlisted target, or recovery residue exists",
                "result": "already absent and converged without approval",
                "exit_code": 0,
                "writes": False,
            },
        }


def test_rollback_contract_reaches_terminal_retirement_from_prepared_create() -> None:
    """Keep a crash before create publication recoverable through retirement."""
    contract = _load_contract()
    transaction = contract["transaction"]
    recovery = contract["recovery"]

    assert "ROLLED_BACK" in transaction["journal_state_successors"]["RECOVERY_REQUIRED"]
    assert transaction["journal_operation_rollback_progress_successors"]["NOT_STARTED"] == ["TARGET_RESTORED"]
    assert transaction["rollback_state_progress_successors"]["PENDING"] == ["RESTORED"]

    prepared_create = recovery["rollback_decision_matrix"][0]
    assert prepared_create == {
        "priority": 1,
        "target": "absent",
        "before": "absent",
        "after": "absent or exact after image",
        "quarantine": "absent",
        "decision": "before state is already restored; perform no target mutation",
    }
    assert recovery["rollback_decision_matrix"][-1]["priority"] == 7
    for row in recovery["rollback_decision_matrix"][:-1]:
        decision = row["decision"].lower()
        assert "retire" not in decision
        assert "delete" not in decision
        assert "remove recovery artifact" not in decision
    assert transaction["rollback_retirement_order"][0].startswith("require journal state ROLLED_BACK")
    assert "prior absence" in transaction["rollback_retirement_order"][1]


def test_doctor_and_approved_apply_probes_do_not_contradict_zero_write_status() -> None:
    """Prevent read-only diagnostics from secretly depending on mutating probes."""
    contract = _load_contract()
    doctor = contract["doctor"]
    invariants = contract["zero_write_invariants"]

    assert doctor["writes"] is False
    assert all("probe" not in check for check in doctor["read_only_install_checks"])
    assert len(doctor["approved_apply_probes"]) == 4
    assert "write-dependent probes are explicitly pending approved apply" in doctor["classification_rule"]
    assert "under lock before journal, state, or target mutation" in doctor["install_rule"]
    assert "recovery plan" in doctor["recovery_rule"]
    assert invariants == [
        (
            "doctor and status issue no mutating filesystem operation and do not change content, ownership, "
            "permissions, link identity, modification time, or change time; access time is excluded because read "
            "effects follow host mount policy"
        ),
        "usage errors and cancelled approval do not write",
        "blocked or indeterminate read-only doctor prevents approval and every install write",
        (
            "stale approved digest may create only the fixed persistent coordination lock and never creates target "
            "roots, state roots, journals, state, or shims"
        ),
        (
            "approved apply probe failure with successful cleanup leaves no probe, journal, state file, or target "
            "shim; cleanup uncertainty may leave only an authenticated private probe receipt and recognized probe "
            "artifacts for approved clean-probe recovery, plus the fixed lock and safely created roots"
        ),
        "foreign, modified, unsafe, ambiguous, or incompatible target blocks before journal creation",
        "repeated current install and repeated complete remove converge without writes",
    ]


def test_thin_shim_format_is_exact_and_contains_no_role_body() -> None:
    """Prevent generator implementations from inventing incompatible shim bytes."""
    contract = _load_contract()
    shim = contract["shim_format"]
    thin_link = contract["thin_link"]

    assert shim["encoding"] == "UTF-8 without BOM"
    assert shim["newline"] == "LF with one final LF"
    assert shim["mode"] == "0600"
    assert shim["maximum_bytes"] == contract["bounded_inputs"]["shim_bytes"]
    assert shim["template_lines"][0] == "{marker}"
    assert shim["template_lines"][-1].endswith('fallback role body."""')
    assert shim["verifier_argv"][0] == "<absolute-python-executable>"
    assert shim["verifier_argv"][1].endswith("/scripts/verify_role_link.py")
    assert {"canonical role-card body", "substantive behavioral fallback", "unbound executable or cache lookup"} <= set(
        shim["forbidden_content"]
    )
    assert set(shim["unavailable_reason_values"]) == {
        "invalid-arguments",
        "plugin-root-mismatch",
        "helper-hash-mismatch",
        "manifest-hash-mismatch",
        "manifest-invalid",
        "package-identity-mismatch",
        "bootstrap-manifest-mismatch",
        "invalid-codex-binary",
        "codex-binary-mismatch",
        "codex-home-invalid",
        "active-package-oracle-failed",
        "active-package-oracle-invalid",
        "active-package-oracle-oversized",
        "active-package-mismatch",
        "active-package-transition",
        "codex-binary-transition",
        "role-not-allowlisted",
        "invalid-package-path",
        "role-manifest-mismatch",
        "role-hash-mismatch",
        "unsafe-file",
        "oversized-file",
        "invalid-digest",
        "verification-error",
    }
    assert thin_link["contains"] == [
        "documented custom-agent configuration",
        "one canonical ownership marker",
        "minimal routing description",
        "exact verifier, package, role, and runtime identity arguments",
        "instruction to verify and load the current canonical role card before substantive work",
        "bounded unavailable-response contract",
    ]
    assert "unavailable-link traces" in thin_link["release_gate"]


def test_verifier_argv_survives_json_and_toml_parsing() -> None:
    """Prevent legal quoted or backslashed paths from changing verifier arguments."""
    contract = _load_contract()
    encoding_rule = contract["shim_format"]["dynamic_fields"]["toml_escaped_verifier_argv_json"]
    argv = ['/tmp/python"quoted', "/tmp/plugin\\root/scripts/verify_role_link.py", "--role", "challenger"]
    argv_json = json.dumps(argv, ensure_ascii=True, separators=(",", ":"))
    toml_escaped = argv_json.replace("\\", "\\\\").replace('"', '\\"')
    parsed = tomllib.loads(f'developer_instructions = """\n{toml_escaped}\n"""\n')

    assert "replace each backslash with two backslashes" in encoding_rule
    assert json.loads(parsed["developer_instructions"].strip()) == argv


def test_python_310_toml_parser_is_exactly_pinned() -> None:
    """Prevent the compatibility parser from depending on user-site packages."""
    assert TEST_REQUIREMENTS_PATH.read_text(encoding="utf-8") == ('tomli==2.2.1; python_version < "3.11"\n')


def test_runtime_unknowns_have_named_evidence_owners() -> None:
    """Prevent platform limitations from being presented as closed contracts."""
    contract = _load_contract()
    owners = contract["runtime_evidence_owners"]

    assert set(owners) == {
        "active plugin and cache selection",
        "bootstrap before substantive role work",
        "custom model, sandbox, approval, and nesting fidelity",
        "macOS and Linux filesystem behavior",
        "session activation after mutation",
    }
    assert contract["thin_link"]["copies_role_body"] is False
    assert contract["thin_link"]["copies_helper_or_card_outside_cache"] is False
    assert contract["thin_link"]["broken_after_plugin_removal"] is True
    assert contract["thin_link"]["stale_after_plugin_version_or_cache_change"] is True
    assert "does not natively enforce" in contract["thin_link"]["behavioral_limit"]


def test_contract_contains_no_machine_local_absolute_paths() -> None:
    """Keep the tracked lifecycle contract portable across user homes."""
    text = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "/Users/" not in text
    assert "/home/" not in text
