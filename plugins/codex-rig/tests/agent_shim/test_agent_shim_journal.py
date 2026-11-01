"""Acceptance checks for the pure agent-shim transaction journal kernel.

The journal kernel parses, validates and orders durable transaction journals without touching the filesystem. These
tests build canonical journal dictionaries and prove which values, intents and state successors the kernel accepts.
"""

from __future__ import annotations

import copy
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered
from _shim_support import canonical_json, root_identity

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
JOURNAL_PATH = PLUGIN_ROOT / "scripts" / "_agent_shim_journal.py"
LIFECYCLE_PATH = PLUGIN_ROOT / "scripts" / "_agent_shim_lifecycle.py"
GENERATOR_PATH = PLUGIN_ROOT / "scripts" / "generate_roles.py"
ROLE_IDS = (
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
)
DIGEST = "a" * 64
UUID = "123e4567-e89b-42d3-a456-426614174000"
#: Operation fields for a noop whose target is present and unchanged.
PRESENT_NOOP = {
    "before_exists": True,
    "before_hash": DIGEST,
    "before_mode": "0600",
    "after_exists": True,
    "after_hash": DIGEST,
    "after_mode": "0600",
}


def _load_module() -> ModuleType:
    """Load the installed journal script without package assumptions."""
    if "generate_roles" not in sys.modules:
        load_module_registered("generate_roles", GENERATOR_PATH)
    if "_agent_shim_lifecycle" not in sys.modules:
        load_module_registered("_agent_shim_lifecycle", LIFECYCLE_PATH)
    return load_module_registered("codex_rig_agent_shim_journal", JOURNAL_PATH)


_JOURNAL = _load_module()
_ILLEGAL_JOURNAL_TRANSITIONS = tuple(
    pytest.param(source, target, id=f"{source.lower()}-to-{target.lower()}")
    for source, allowed in _JOURNAL.JOURNAL_STATE_SUCCESSORS.items()
    for target in _JOURNAL.JOURNAL_STATES
    if target != source and target not in allowed
)


def _snapshot(name: str) -> dict[str, object]:
    """Build one present state-snapshot fixture.

    Example:
        >>> _snapshot("state.json")["exists"]
        True
    """
    return {"exists": True, "relative_path": name, "sha256": DIGEST, "mode": "0600"}


def _operation(
    role_id: str, *, intent: str = "create", progress: str | None = None, **fields: object
) -> dict[str, object]:
    """Build one exact operation fixture for an intent, with optional field overrides.

    Example:
        >>> _operation("challenger")["target_name"]
        'codex-rig-challenger.toml'
    """
    values: dict[str, object] = {
        "role_id": role_id,
        "intent": intent,
        "target_name": f"codex-rig-{role_id}.toml",
        "before_exists": False,
        "before_hash": None,
        "before_mode": None,
        "after_exists": True,
        "after_hash": DIGEST,
        "after_mode": "0600",
        "before_image": None,
        "after_image": f"after/{role_id}.toml",
        "quarantine_name": None,
        "progress": progress or ("VERIFIED" if intent == "noop" else "PLANNED"),
        "rollback_progress": "NOT_STARTED",
    }
    if intent == "noop":
        values.update(after_exists=False, after_hash=None, after_mode=None, after_image=None)
    elif intent == "update":
        values.update(
            before_exists=True,
            before_hash="b" * 64,
            before_mode="0644",
            before_image=f"before/{role_id}.toml",
            quarantine_name=f"quarantine/{role_id}.toml",
        )
    elif intent in {"remove", "retire"}:
        values.update(
            before_exists=True,
            before_hash="b" * 64,
            before_mode="0644",
            after_exists=False,
            after_hash=None,
            after_mode=None,
            before_image=f"before/{role_id}.toml",
            after_image=None,
            quarantine_name=f"quarantine/{role_id}.toml",
        )
    values.update(fields)
    return values


def _journal(
    *,
    state: str = "PREPARED",
    action: str = "install",
    intent: str = "create",
    role_ids: tuple[str, ...] = ROLE_IDS,
    progress: str | None = None,
    **fields: object,
) -> dict[str, object]:
    """Build one complete canonical journal fixture; ``progress`` and ``fields`` apply to every operation."""
    return {
        "schema": 1,
        "transaction_id": UUID,
        "transaction_nonce": UUID,
        "install_id": UUID,
        "action": action,
        "approved_plan_digest": DIGEST,
        "package_hash": DIGEST,
        "roster_hash": DIGEST,
        "codex_home_identity": root_identity("/codex"),
        "target_root_identity": root_identity("/codex/agents"),
        "state_root_identity": root_identity("/codex/codex-rig/shims"),
        "before_state": _snapshot("state.before.json"),
        "after_state": _snapshot("state.after.json"),
        "rollback_state_progress": "PENDING",
        "journal_state": state,
        "operations": [_operation(role_id, intent=intent, progress=progress, **fields) for role_id in role_ids],
    }


def _recovery_journal(*, restored: int, state: str = "RECOVERY_REQUIRED", state_progress: str = "PENDING") -> dict:
    """Build a recovery journal whose first ``restored`` operations are already TARGET_RESTORED."""
    value = _journal(state=state)
    value["rollback_state_progress"] = state_progress
    for operation in value["operations"][:restored]:
        operation["rollback_progress"] = "TARGET_RESTORED"
    return value


def _with_state(value: dict, state: str) -> dict:
    """Return a deep copy of a journal with only its journal state changed."""
    successor = copy.deepcopy(value)
    successor["journal_state"] = state
    return successor


def _with_operations(value: dict, changes: dict[int, dict[str, object]]) -> dict:
    """Return a deep copy of a journal with the given per-operation field changes applied."""
    successor = copy.deepcopy(value)
    for index, fields in changes.items():
        successor["operations"][index].update(fields)
    return successor


def _top_level(value: dict) -> dict:
    """Select the journal itself as the mutation target."""
    return value


def _first_operation(value: dict) -> dict:
    """Select the first journal operation as the mutation target."""
    return value["operations"][0]


def _add_top_level_field(value: dict) -> None:
    """Add a field the journal schema does not define."""
    value["unexpected"] = True


def _swap_first_two_operations(value: dict) -> None:
    """Reorder the sorted operation roster."""
    operations = value["operations"]
    operations[0], operations[1] = operations[1], operations[0]


def _duplicate_second_operation(value: dict) -> None:
    """Replace the second operation with a copy of the first."""
    value["operations"][1] = copy.deepcopy(value["operations"][0])


def _invalidate_role_id(value: dict) -> None:
    """Give the first operation a role id outside the role grammar."""
    value["operations"][0]["role_id"] = "Retired"


def _drift_target_name(value: dict) -> None:
    """Point the first operation at a target not derived from its role id."""
    value["operations"][0]["target_name"] = "codex-rig-other.toml"


def _add_operation_field(value: dict) -> None:
    """Add a field the operation schema does not define."""
    value["operations"][0]["unexpected"] = True


@pytest.fixture(scope="module")
def module() -> ModuleType:
    """Return the journal kernel loaded once at import; it is a pure, stateless validator."""
    return _JOURNAL


@pytest.fixture
def mutating_journal() -> dict[str, object]:
    """Return a canonical MUTATING journal with every operation PLANNED."""
    return _journal(state="MUTATING")


class TestParseJournal:
    """Group byte-level parsing and canonical serialization contracts."""

    def test_round_trips_canonical_bytes(self, module: ModuleType) -> None:
        """A canonical prepared journal parses to a value that serializes back to the same bytes.

        Starts from the compact sorted JSON encoding of a complete PREPARED journal and checks state, role order and
        byte-for-byte re-serialization, which approval digests depend on.
        """
        payload = canonical_json(_journal())

        parsed = module.parse_journal(payload)

        assert parsed.journal_state == "PREPARED"
        assert tuple(item.role_id for item in parsed.operations) == ROLE_IDS
        assert module.canonical_journal_bytes(parsed) == payload

    def test_parsed_value_is_immutable(self, module: ModuleType) -> None:
        """Mutating a parsed operation raises instead of changing journal authority.

        A parsed journal is passed between recovery steps, so an in-place edit would let one step rewrite another's
        evidence.
        """
        parsed = module.parse_journal(canonical_json(_journal()))

        with pytest.raises((AttributeError, TypeError)):
            parsed.operations[0].progress = "VERIFIED"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b'{"schema":1,"schema":1}', id="duplicate"),
            pytest.param(b'{"value":NaN}', id="nonfinite"),
            pytest.param(b"[]", id="nonobject"),
            pytest.param(b"\xef\xbb\xbf{}", id="bom"),
            pytest.param(b'{ "schema":1}', id="noncanonical"),
        ],
    )
    def test_rejects_ambiguous_or_noncanonical_json(self, module: ModuleType, payload: bytes) -> None:
        """JSON encodings that cannot carry unique bounded authority are rejected.

        Duplicate keys, non-finite numbers, non-objects, a byte-order mark and non-canonical spacing would each let two
        parsers read different journals from the same bytes.
        """
        with pytest.raises(module.JournalDataError):
            module.parse_journal(payload)

    def test_rejects_oversized_payload(self, module: ModuleType) -> None:
        """A payload larger than the journal byte bound is rejected before parsing.

        The bound keeps recovery reads finite even when a hostile or corrupt journal file is enormous.
        """
        with pytest.raises(module.JournalDataError):
            module.parse_journal(b"{" + b" " * module.MAX_JOURNAL_BYTES + b"}")


class TestValidateJournal:
    """Group schema, roster, intent and phase validation of a single journal."""

    @pytest.mark.parametrize(
        "mutation",
        [
            pytest.param(_add_top_level_field, id="extra"),
            pytest.param(_swap_first_two_operations, id="reordered"),
            pytest.param(_duplicate_second_operation, id="duplicate"),
            pytest.param(_invalidate_role_id, id="invalid-role"),
            pytest.param(_drift_target_name, id="target-drift"),
            pytest.param(_add_operation_field, id="operation-extra"),
        ],
    )
    def test_rejects_roster_or_schema_drift(self, module: ModuleType, mutation) -> None:
        """Duplicate, reordered or schema-expanded transaction authority is rejected.

        Starting from a valid journal, each case breaks exactly one rule of the exact roster and schema.
        """
        value = _journal()
        mutation(value)

        with pytest.raises(module.JournalDataError):
            module.validate_journal(value)

    @pytest.mark.parametrize(
        ("intent", "action", "bad_progress"),
        [
            pytest.param("noop", "install", "PLANNED", id="noop"),
            pytest.param("create", "install", "DETACHED", id="create"),
            pytest.param("repair-missing", "install", "DETACHED", id="repair-missing"),
            pytest.param("update", "install", "INVALID", id="update"),
            pytest.param("retire", "install", "PUBLISHED", id="retire"),
            pytest.param("remove", "remove", "PUBLISHED", id="remove"),
        ],
    )
    def test_rejects_progress_outside_intent_graph(
        self, module: ModuleType, intent: str, action: str, bad_progress: str
    ) -> None:
        """Progress values outside an intent's forward graph are rejected.

        Each intent owns its own progress path (for example create never detaches), so a MUTATING journal carrying
        another path's value must not validate.
        """
        value = _journal(state="MUTATING", action=action, intent=intent)
        value["operations"][0]["progress"] = bad_progress

        with pytest.raises(module.JournalDataError):
            module.validate_journal(value)

    @pytest.mark.parametrize(
        "role_ids",
        [
            pytest.param(("historical",), id="one"),
            pytest.param(("active", "historical", "retired"), id="migration-union"),
            pytest.param(tuple(f"role-{index:03d}" for index in range(256)), id="maximum"),
        ],
    )
    def test_accepts_bounded_sorted_operation_rosters(self, module: ModuleType, role_ids: tuple[str, ...]) -> None:
        """A sorted operation roster of one through 256 roles validates.

        Migration journals carry the union of active and historical roles, so the roster is variable-size rather than
        always the fifteen shipped roles.
        """
        parsed = module.validate_journal(_journal(role_ids=role_ids))

        assert tuple(item.role_id for item in parsed.operations) == role_ids

    @pytest.mark.parametrize(
        "role_ids",
        [
            pytest.param((), id="empty"),
            pytest.param(tuple(f"role-{index:03d}" for index in range(257)), id="over-bound"),
        ],
    )
    def test_rejects_empty_or_over_bound_rosters(self, module: ModuleType, role_ids: tuple[str, ...]) -> None:
        """A journal with no operations or more than 256 operations is rejected.

        Both ends of the explicit operation-count bound are exercised so neither an empty nor an unbounded transaction
        can validate.
        """
        with pytest.raises(module.JournalDataError, match="roster"):
            module.validate_journal(_journal(role_ids=role_ids))

    def test_preparing_rejects_forward_progress(self, module: ModuleType) -> None:
        """A PREPARING journal cannot carry mutation progress.

        PREPARING only authorizes named artifacts, so an operation already PUBLISHED would claim a mutation that has not
        been journaled as started.
        """
        invalid = _journal(state="PREPARING")
        invalid["operations"][0]["progress"] = "PUBLISHED"

        with pytest.raises(module.JournalDataError, match="pre-mutation"):
            module.validate_journal(invalid)

    def test_retire_uses_remove_artifacts(self, module: ModuleType) -> None:
        """Retiring a historical role during install uses before-image and quarantine artifacts only.

        Retire detaches an installed file reversibly, so it carries a before image and quarantine name but no after
        image.
        """
        parsed = module.validate_journal(_journal(action="install", intent="retire", role_ids=("retired",)))

        retired = parsed.operations[0]
        assert retired.before_image == "before/retired.toml"
        assert retired.after_image is None
        assert retired.quarantine_name == "quarantine/retired.toml"

    def test_remove_action_rejects_retire_intent(self, module: ModuleType) -> None:
        """A remove-action journal cannot contain a retire operation.

        Retire exists only to drop historical roles while installing; accepting it under remove would blur the two
        ownership paths.
        """
        forbidden = _journal(action="remove", intent="remove", role_ids=("retired",))
        forbidden["operations"][0] = _operation("retired", intent="retire")

        with pytest.raises(module.JournalDataError, match="remove intent"):
            module.validate_journal(forbidden)

    def test_committed_rejects_unverified_progress(self, module: ModuleType) -> None:
        """COMMITTED requires every forward operation to be VERIFIED.

        A journal claiming commit while operations are still PLANNED would let recovery skip real unfinished work.
        """
        with pytest.raises(module.JournalDataError):
            module.validate_journal(_journal(state="COMMITTED"))

    def test_committed_accepts_verified_progress(self, module: ModuleType) -> None:
        """COMMITTED validates once every operation is VERIFIED and rollback is untouched.

        This is the positive twin of the unverified rejection above, so the rejection cannot pass for an unrelated
        reason.
        """
        parsed = module.validate_journal(_journal(state="COMMITTED", progress="VERIFIED"))

        assert parsed.journal_state == "COMMITTED"

    @pytest.mark.parametrize(
        ("action", "base_intent", "overrides", "before_exists"),
        [
            pytest.param("install", "create", {}, False, id="install-absent"),
            pytest.param("install", "create", PRESENT_NOOP, True, id="install-present"),
            pytest.param("remove", "remove", {}, False, id="remove-absent"),
        ],
    )
    def test_accepts_noop_observation_for_action(
        self, module: ModuleType, action: str, base_intent: str, overrides: dict, before_exists: bool
    ) -> None:
        """A noop operation validates for the observations its action permits.

        An install may skip an absent retired role or an already converged present one; a remove may skip a role that is
        already absent.
        """
        value = _journal(action=action, intent=base_intent)
        value["operations"][0] = _operation(ROLE_IDS[0], intent="noop", **overrides)

        parsed = module.validate_journal(value)

        assert parsed.operations[0].intent == "noop"
        assert parsed.operations[0].before_exists is before_exists

    def test_remove_rejects_present_noop(self, module: ModuleType) -> None:
        """A remove journal cannot mark a present target as a noop.

        Remove must take ownership of every present file, so skipping a present target as unchanged would leave an owned
        shim behind.
        """
        value = _journal(action="remove", intent="remove")
        value["operations"][0] = _operation(ROLE_IDS[0], intent="noop", **PRESENT_NOOP)

        with pytest.raises(module.JournalDataError):
            module.validate_journal(value)

    @pytest.mark.parametrize(
        ("action", "overrides"),
        [
            pytest.param("install", PRESENT_NOOP, id="install-converged"),
            pytest.param("remove", {}, id="remove-pristine"),
        ],
    )
    def test_rejects_zero_write_convergence(self, module: ModuleType, action: str, overrides: dict) -> None:
        """Pristine or already converged actions never produce a journal.

        When every operation is a noop there is nothing to mutate, so a journal would be transaction state for a zero-
        write outcome.
        """
        value = _journal(action=action, intent="noop", **overrides)

        with pytest.raises(module.JournalDataError, match="zero-write"):
            module.validate_journal(value)

    @pytest.mark.parametrize(
        ("state", "field", "value"),
        [
            pytest.param("PREPARED", "progress", "PUBLISHED", id="forward-in-prepared"),
            pytest.param("MUTATING", "rollback_progress", "TARGET_RESTORED", id="rollback-in-mutating"),
        ],
    )
    def test_rejects_progress_outside_its_phase(self, module: ModuleType, state: str, field: str, value: str) -> None:
        """Forward or rollback progress outside its durable phase is rejected.

        Forward progress belongs to MUTATING onward and rollback progress to recovery, so each is refused in the wrong
        phase.
        """
        journal = _journal(state=state)
        journal["operations"][0][field] = value

        with pytest.raises(module.JournalDataError):
            module.validate_journal(journal)

    def test_rejects_unsafe_after_mode(self, module: ModuleType) -> None:
        """A generated target with a mode other than 0600 is rejected.

        Shims are generated owner-only, so a more permissive recorded after-mode means the journal does not describe
        what the kernel would publish.
        """
        journal = _journal()
        journal["operations"][0]["after_mode"] = "0644"

        with pytest.raises(module.JournalDataError, match="mode"):
            module.validate_journal(journal)

    def test_committed_rejects_missing_after_state(self, module: ModuleType) -> None:
        """A COMMITTED journal must record an existing after-state snapshot.

        Commit authority rests on the published state file, so a committed journal whose after state is absent cannot be
        trusted.
        """
        committed = _journal(state="COMMITTED", progress="VERIFIED")
        committed["after_state"] = {"exists": False, "relative_path": None, "sha256": None, "mode": None}

        with pytest.raises(module.JournalDataError, match="after state"):
            module.validate_journal(committed)

    def test_rolled_back_rejects_unrestored_operation(self, module: ModuleType) -> None:
        """ROLLED_BACK requires every operation to be TARGET_RESTORED.

        One operation still NOT_STARTED means a target was never restored, so the terminal state would hide unfinished
        recovery.
        """
        invalid = _recovery_journal(restored=len(ROLE_IDS), state="ROLLED_BACK", state_progress="RESTORED")
        invalid["operations"][0]["rollback_progress"] = "NOT_STARTED"

        with pytest.raises(module.JournalDataError):
            module.validate_journal(invalid)

    @pytest.mark.parametrize("path", ["/codex/", "/codex/../escape", "/codex/\udcff"])
    def test_rejects_noncanonical_root_path(self, module: ModuleType, path: str) -> None:
        """Root identity paths must be exact canonical strings, not normalized by parsing.

        A trailing slash, a parent reference and an unencodable surrogate each name a root ambiguously, so they are
        refused instead of silently rewritten.
        """
        value = _journal()
        value["codex_home_identity"]["canonical_path"] = path

        with pytest.raises(module.JournalDataError):
            module.validate_journal(value)

    @pytest.mark.parametrize(
        ("select", "field"),
        [
            pytest.param(_top_level, "action", id="action"),
            pytest.param(_top_level, "journal_state", id="journal-state"),
            pytest.param(_top_level, "rollback_state_progress", id="rollback-state-progress"),
            pytest.param(_first_operation, "intent", id="intent"),
        ],
    )
    def test_unhashable_enum_values_raise_data_error(self, module: ModuleType, select, field: str) -> None:
        """Hostile JSON containers in an enum field raise the stable journal error.

        A list where a string enum belongs is unhashable, which could surface as a raw TypeError instead of the one
        error type callers handle.
        """
        value = _journal()
        select(value)[field] = []

        with pytest.raises(module.JournalDataError):
            module.validate_journal(value)


class TestValidateSuccessor:
    """Group legal and illegal transitions between two journals."""

    def test_preparing_advances_to_prepared(self, module: ModuleType) -> None:
        """A PREPARING journal advances to PREPARED with nothing else changed.

        PREPARING authorizes the named artifacts and PREPARED opens the ordinary mutation phase.
        """
        preparing = _journal(state="PREPARING")

        parsed = module.validate_successor(preparing, _with_state(preparing, "PREPARED"))

        assert parsed.journal_state == "PREPARED"

    @pytest.mark.parametrize(
        ("source", "target", "progress"),
        [
            pytest.param("PREPARED", "MUTATING", None, id="prepared-to-mutating"),
            pytest.param("MUTATING", "RECOVERY_REQUIRED", None, id="mutating-to-recovery"),
            pytest.param("MUTATING", "STATE_COMMITTED", "VERIFIED", id="mutating-to-state-committed"),
            pytest.param("STATE_COMMITTED", "COMMITTED", "VERIFIED", id="state-committed-to-committed"),
        ],
    )
    def test_accepts_legal_state_edge(self, module: ModuleType, source: str, target: str, progress: str | None) -> None:
        """Each legal journal-state edge validates when its terminal evidence is present.

        Covers entering mutation, failing into recovery, and reaching commit, where the commit edges additionally
        require every operation VERIFIED.
        """
        before = _journal(state=source, progress=progress)

        parsed = module.validate_successor(before, _with_state(before, target))

        assert parsed.journal_state == target

    @pytest.mark.parametrize(("source", "target"), _ILLEGAL_JOURNAL_TRANSITIONS)
    def test_rejects_illegal_state_jump(self, module: ModuleType, source: str, target: str) -> None:
        """Every journal-state transition absent from the contract graph is rejected.

        The cases are generated from the kernel's own state table, so a state edge cannot be added without a matching
        rejection test existing for all the others.
        """
        before = _journal(state=source)

        with pytest.raises(module.JournalTransitionError):
            module.validate_successor(before, _with_state(before, target))

    def test_accepts_one_adjacent_progress_step(self, module: ModuleType, mutating_journal: dict) -> None:
        """A single operation may advance one adjacent progress step.

        PLANNED to PUBLISHED is the create path's next step and is the baseline the rejection cases below break.
        """
        after = _with_operations(mutating_journal, {0: {"progress": "PUBLISHED"}})

        parsed = module.validate_successor(mutating_journal, after)

        assert parsed.operations[0].progress == "PUBLISHED"

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({0: {"progress": "VERIFIED"}}, id="skipped-step"),
            pytest.param({0: {"progress": "PUBLISHED"}, 1: {"progress": "PUBLISHED"}}, id="two-operations"),
        ],
    )
    def test_rejects_skipped_or_combined_progress(
        self, module: ModuleType, mutating_journal: dict, changes: dict
    ) -> None:
        """Skipping a progress step or advancing two operations at once is rejected.

        Each durable write may change one dimension by one step, otherwise a crash could leave a state the recovery
        table does not recognize.
        """
        after = _with_operations(mutating_journal, changes)

        with pytest.raises(module.JournalTransitionError):
            module.validate_successor(mutating_journal, after)

    def test_rejects_immutable_tamper(self, module: ModuleType, mutating_journal: dict) -> None:
        """A successor that alters transaction-bound evidence is rejected.

        The package hash is fixed when the plan is approved, so changing it alongside a legal progress step must still
        fail.
        """
        after = _with_operations(mutating_journal, {0: {"progress": "PUBLISHED"}})
        after["package_hash"] = "b" * 64

        with pytest.raises(module.JournalTransitionError):
            module.validate_successor(mutating_journal, after)

    @pytest.mark.parametrize(
        ("state", "before_progress", "field", "value"),
        [
            pytest.param("MUTATING", None, "progress", "DETACHED", id="planned-to-detached"),
            pytest.param("MUTATING", "DETACHED", "progress", "VERIFIED", id="detached-to-verified"),
            pytest.param("RECOVERY_REQUIRED", None, "rollback_progress", "TARGET_RESTORED", id="rollback-restored"),
        ],
    )
    def test_retire_progress_and_rollback_successors(
        self, module: ModuleType, state: str, before_progress: str | None, field: str, value: str
    ) -> None:
        """A retire operation follows detach-and-verify progress and reversible rollback.

        Retire reuses the remove path: PLANNED detaches, DETACHED verifies, and recovery restores the target.
        """
        before = _journal(
            state=state, action="install", intent="retire", role_ids=("retired",), progress=before_progress
        )

        parsed = module.validate_successor(before, _with_operations(before, {0: {field: value}}))

        assert getattr(parsed.operations[0], field) == value

    @pytest.mark.parametrize("index", [pytest.param(index, id=role_id) for index, role_id in enumerate(ROLE_IDS)])
    def test_rollback_restores_one_operation_at_a_time(self, module: ModuleType, index: int) -> None:
        """Recovery restores operations one per durable write in canonical order.

        With the operations before this one already restored, restoring the next is the only legal single step.
        """
        before = _recovery_journal(restored=index)

        parsed = module.validate_successor(
            before, _with_operations(before, {index: {"rollback_progress": "TARGET_RESTORED"}})
        )

        assert parsed.operations[index].rollback_progress == "TARGET_RESTORED"

    def test_rollback_state_restores_after_all_operations(self, module: ModuleType) -> None:
        """The before-state is marked RESTORED once every operation is TARGET_RESTORED.

        State restoration is the second rollback dimension and follows all target restorations.
        """
        before = _recovery_journal(restored=len(ROLE_IDS))
        after = copy.deepcopy(before)
        after["rollback_state_progress"] = "RESTORED"

        parsed = module.validate_successor(before, after)

        assert parsed.rollback_state_progress == "RESTORED"

    def test_rolled_back_follows_all_terminal_evidence(self, module: ModuleType) -> None:
        """ROLLED_BACK is reachable only after targets and state are restored.

        Terminal rollback requires every operation TARGET_RESTORED and the before-state RESTORED.
        """
        restored = _recovery_journal(restored=len(ROLE_IDS), state_progress="RESTORED")

        parsed = module.validate_successor(restored, _with_state(restored, "ROLLED_BACK"))

        assert parsed.journal_state == "ROLLED_BACK"


class TestRevalidation:
    """Group checks that frozen dataclass construction cannot bypass the schema."""

    @pytest.fixture
    def mutating(self, module: ModuleType):
        """Return the validated MUTATING journal value."""
        return module.validate_journal(_journal(state="MUTATING"))

    def test_successor_rejects_forged_values(self, module: ModuleType, mutating) -> None:
        """validate_successor re-validates constructed values instead of trusting their types.

        ``dataclasses.replace`` bypasses parsing, so a package hash that is not a digest must still be refused.
        """
        published = _journal(state="MUTATING")
        published["operations"][0]["progress"] = "PUBLISHED"
        after = module.validate_journal(published)

        with pytest.raises(module.JournalTransitionError):
            module.validate_successor(
                replace(mutating, package_hash="not-a-digest"), replace(after, package_hash="not-a-digest")
            )

    def test_canonical_bytes_reject_forged_package_hash(self, module: ModuleType, mutating) -> None:
        """Serializing a value whose package hash was forged raises the journal error.

        Canonical bytes feed approval digests, so a forged field must never serialize.
        """
        with pytest.raises(module.JournalDataError):
            module.canonical_journal_bytes(replace(mutating, package_hash="not-a-digest"))

    def test_canonical_bytes_reject_malformed_operations(self, module: ModuleType, mutating) -> None:
        """Serializing a value holding a non-operation object raises the journal error.

        A foreign object in the operations tuple would otherwise fail with an unrelated AttributeError.
        """
        with pytest.raises(module.JournalDataError):
            module.canonical_journal_bytes(replace(mutating, operations=(object(),)))
