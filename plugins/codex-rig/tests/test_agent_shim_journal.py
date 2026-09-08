"""Acceptance checks for the pure agent-shim transaction journal kernel."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
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


def _load_script(path: Path, name: str) -> ModuleType:
    """Load one installed script with its sibling dependencies available."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _load_module() -> ModuleType:
    """Load the installed journal script without package assumptions."""
    if "generate_roles" not in sys.modules:
        _load_script(GENERATOR_PATH, "generate_roles")
    if "_agent_shim_lifecycle" not in sys.modules:
        _load_script(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    return _load_script(JOURNAL_PATH, "codex_rig_agent_shim_journal")


def _root(path: str) -> dict[str, object]:
    """Build one exact root identity fixture.

    Example:
        >>> _root("/fixture/codex")["mode"]
        '0700'
    """
    return {"canonical_path": path, "device": 1, "inode": 2, "owner": 3, "group": 4, "mode": "0700"}


def _snapshot(name: str) -> dict[str, object]:
    """Build one present state-snapshot fixture.

    Example:
        >>> _snapshot("state.json")["exists"]
        True
    """
    return {"exists": True, "relative_path": name, "sha256": DIGEST, "mode": "0600"}


def _operation(role_id: str, *, intent: str = "create", progress: str | None = None) -> dict[str, object]:
    """Build one exact operation fixture for an intent.

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
    return values


def _journal(
    *,
    state: str = "PREPARED",
    action: str = "install",
    intent: str = "create",
    role_ids: tuple[str, ...] = ROLE_IDS,
) -> dict[str, object]:
    """Build one complete canonical journal fixture."""
    return {
        "schema": 1,
        "transaction_id": UUID,
        "transaction_nonce": UUID,
        "install_id": UUID,
        "action": action,
        "approved_plan_digest": DIGEST,
        "package_hash": DIGEST,
        "roster_hash": DIGEST,
        "codex_home_identity": _root("/codex"),
        "target_root_identity": _root("/codex/agents"),
        "state_root_identity": _root("/codex/codex-rig/shims"),
        "before_state": _snapshot("state.before.json"),
        "after_state": _snapshot("state.after.json"),
        "rollback_state_progress": "PENDING",
        "journal_state": state,
        "operations": [_operation(role_id, intent=intent) for role_id in role_ids],
    }


def _encode(value: object) -> bytes:
    """Encode one canonical compact JSON fixture.

    Example:
        >>> _encode({"b": 2, "a": 1})
        b'{"a":1,"b":2}'
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def test_valid_prepared_is_immutable_and_has_exact_canonical_bytes() -> None:
    """Expose one immutable prepared value and stable journal serialization."""
    module = _load_module()
    payload = _encode(_journal())

    parsed = module.parse_journal(payload)

    assert parsed.journal_state == "PREPARED"
    assert tuple(item.role_id for item in parsed.operations) == ROLE_IDS
    assert module.canonical_journal_bytes(parsed) == payload
    with pytest.raises((AttributeError, TypeError)):
        parsed.operations[0].progress = "VERIFIED"


def test_preparing_journal_advances_only_to_prepared() -> None:
    """Authorize named artifacts before entering the ordinary prepared phase."""
    module = _load_module()
    preparing = _journal(state="PREPARING")
    prepared = copy.deepcopy(preparing)
    prepared["journal_state"] = "PREPARED"

    assert module.validate_successor(preparing, prepared).journal_state == "PREPARED"

    invalid = copy.deepcopy(preparing)
    invalid["operations"][0]["progress"] = "PUBLISHED"
    with pytest.raises(module.JournalDataError, match="pre-mutation"):
        module.validate_journal(invalid)


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
def test_parser_rejects_ambiguous_or_noncanonical_json(payload: bytes) -> None:
    """Reject JSON encodings that cannot carry unique bounded authority."""
    module = _load_module()

    with pytest.raises(module.JournalDataError):
        module.parse_journal(payload)

    with pytest.raises(module.JournalDataError):
        module.parse_journal(b"{" + b" " * module.MAX_JOURNAL_BYTES + b"}")


@pytest.mark.parametrize(
    "mutation",
    ["extra", "reordered", "duplicate", "invalid-role", "target-drift", "operation-extra"],
)
def test_roster_and_schema_are_exact(mutation: str) -> None:
    """Reject duplicate, reordered, or schema-expanded transaction authority."""
    module = _load_module()
    value = _journal()
    if mutation == "extra":
        value["unexpected"] = True
    elif mutation == "reordered":
        value["operations"][0], value["operations"][1] = value["operations"][1], value["operations"][0]
    elif mutation == "duplicate":
        value["operations"][1] = copy.deepcopy(value["operations"][0])
    elif mutation == "invalid-role":
        value["operations"][0]["role_id"] = "Retired"
    elif mutation == "target-drift":
        value["operations"][0]["target_name"] = "codex-rig-other.toml"
    else:
        value["operations"][0]["unexpected"] = True

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
def test_intent_specific_progress_is_strict(intent: str, action: str, bad_progress: str) -> None:
    """Reject progress values outside each intent's forward graph."""
    module = _load_module()
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
def test_variable_sorted_operation_rosters_are_bounded(role_ids: tuple[str, ...]) -> None:
    """Accept a bounded sorted active-and-historical operation union."""
    module = _load_module()

    parsed = module.validate_journal(_journal(role_ids=role_ids))

    assert tuple(item.role_id for item in parsed.operations) == role_ids


@pytest.mark.parametrize(
    "role_ids",
    [pytest.param((), id="empty"), pytest.param(tuple(f"role-{index:03d}" for index in range(257)), id="over-bound")],
)
def test_variable_operation_rosters_reject_empty_or_over_bound(role_ids: tuple[str, ...]) -> None:
    """Reject journals outside the explicit operation-count bound."""
    module = _load_module()

    with pytest.raises(module.JournalDataError, match="roster"):
        module.validate_journal(_journal(role_ids=role_ids))


def test_install_retire_uses_remove_artifacts_progress_and_rollback() -> None:
    """Retire historical roles only during install with reversible detach semantics."""
    module = _load_module()
    prepared = _journal(action="install", intent="retire", role_ids=("retired",))

    parsed = module.validate_journal(prepared)

    retired = parsed.operations[0]
    assert retired.before_image == "before/retired.toml"
    assert retired.after_image is None
    assert retired.quarantine_name == "quarantine/retired.toml"
    mutating = copy.deepcopy(prepared)
    mutating["journal_state"] = "MUTATING"
    detached = copy.deepcopy(mutating)
    detached["operations"][0]["progress"] = "DETACHED"
    assert module.validate_successor(mutating, detached).operations[0].progress == "DETACHED"
    verified = copy.deepcopy(detached)
    verified["operations"][0]["progress"] = "VERIFIED"
    assert module.validate_successor(detached, verified).operations[0].progress == "VERIFIED"
    recovery = copy.deepcopy(mutating)
    recovery["journal_state"] = "RECOVERY_REQUIRED"
    restored = copy.deepcopy(recovery)
    restored["operations"][0]["rollback_progress"] = "TARGET_RESTORED"
    assert module.validate_successor(recovery, restored).operations[0].rollback_progress == "TARGET_RESTORED"

    forbidden = _journal(action="remove", intent="remove", role_ids=("retired",))
    forbidden["operations"][0] = _operation("retired", intent="retire")
    with pytest.raises(module.JournalDataError, match="remove intent"):
        module.validate_journal(forbidden)


def test_every_illegal_journal_state_jump_is_rejected() -> None:
    """Reject every distinct state transition absent from the contract graph."""
    module = _load_module()
    for source, allowed in module.JOURNAL_STATE_SUCCESSORS.items():
        for target in module.JOURNAL_STATES:
            if target == source or target in allowed:
                continue
            before = _journal(state=source)
            after = copy.deepcopy(before)
            after["journal_state"] = target
            with pytest.raises(module.JournalTransitionError):
                module.validate_successor(before, after)


def test_illegal_progress_jumps_and_multiple_dimensions_are_rejected() -> None:
    """Allow one adjacent progress update while rejecting skips and combined writes."""
    module = _load_module()
    before = _journal(state="MUTATING")
    after = copy.deepcopy(before)
    after["operations"][0]["progress"] = "PUBLISHED"
    accepted = module.validate_successor(before, after)
    assert accepted.operations[0].progress == "PUBLISHED"

    skipped = copy.deepcopy(before)
    skipped["operations"][0]["progress"] = "VERIFIED"
    with pytest.raises(module.JournalTransitionError):
        module.validate_successor(before, skipped)

    combined = copy.deepcopy(after)
    combined["operations"][1]["progress"] = "PUBLISHED"
    with pytest.raises(module.JournalTransitionError):
        module.validate_successor(before, combined)


def test_immutable_tamper_is_rejected() -> None:
    """Reject successor bytes that alter transaction-bound immutable evidence."""
    module = _load_module()
    before = _journal(state="MUTATING")
    after = copy.deepcopy(before)
    after["operations"][0]["progress"] = "PUBLISHED"
    after["package_hash"] = "b" * 64

    with pytest.raises(module.JournalTransitionError):
        module.validate_successor(before, after)


def test_rolled_back_is_reachable_only_after_all_terminal_evidence() -> None:
    """Reach ROLLED_BACK through single durable dimensions in canonical order."""
    module = _load_module()
    current = _journal(state="RECOVERY_REQUIRED")
    for index in range(len(ROLE_IDS)):
        successor = copy.deepcopy(current)
        successor["operations"][index]["rollback_progress"] = "TARGET_RESTORED"
        module.validate_successor(current, successor)
        current = successor
    restored = copy.deepcopy(current)
    restored["rollback_state_progress"] = "RESTORED"
    module.validate_successor(current, restored)
    terminal = copy.deepcopy(restored)
    terminal["journal_state"] = "ROLLED_BACK"

    assert module.validate_successor(restored, terminal).journal_state == "ROLLED_BACK"

    invalid = copy.deepcopy(terminal)
    invalid["operations"][0]["rollback_progress"] = "NOT_STARTED"
    with pytest.raises(module.JournalDataError):
        module.validate_journal(invalid)


def test_committed_requires_verified_forward_evidence() -> None:
    """Reject COMMITTED unless all forward work is verified and rollback is untouched."""
    module = _load_module()
    value = _journal(state="COMMITTED")
    with pytest.raises(module.JournalDataError):
        module.validate_journal(value)

    for item in value["operations"]:
        item["progress"] = "VERIFIED"
    assert module.validate_journal(value).journal_state == "COMMITTED"


def test_noop_observation_depends_on_action() -> None:
    """Allow absent retired noops while preventing remove from skipping ownership."""
    module = _load_module()
    install = _journal(action="install", intent="create")
    install["operations"][0] = _operation(ROLE_IDS[0], intent="noop")
    assert module.validate_journal(install).operations[0].before_exists is False
    install["operations"][0].update(
        before_exists=True,
        before_hash=DIGEST,
        before_mode="0600",
        after_exists=True,
        after_hash=DIGEST,
        after_mode="0600",
    )
    assert module.validate_journal(install).operations[0].intent == "noop"

    remove = _journal(action="remove", intent="remove")
    remove["operations"][0] = _operation(ROLE_IDS[0], intent="noop")
    assert module.validate_journal(remove).operations[0].before_exists is False
    remove["operations"][0].update(
        before_exists=True,
        before_hash=DIGEST,
        before_mode="0600",
        after_exists=True,
        after_hash=DIGEST,
        after_mode="0600",
    )
    with pytest.raises(module.JournalDataError):
        module.validate_journal(remove)


def test_zero_write_convergence_never_creates_a_journal() -> None:
    """Keep pristine or already-converged actions outside transaction state."""
    module = _load_module()
    for action in ("install", "remove"):
        value = _journal(action=action, intent="noop")
        if action == "install":
            for item in value["operations"]:
                item.update(
                    before_exists=True,
                    before_hash=DIGEST,
                    before_mode="0600",
                    after_exists=True,
                    after_hash=DIGEST,
                    after_mode="0600",
                )
        with pytest.raises(module.JournalDataError, match="zero-write"):
            module.validate_journal(value)


def test_progress_is_confined_to_its_journal_phase() -> None:
    """Reject forward or rollback progress outside its durable phase."""
    module = _load_module()
    prepared = _journal()
    prepared["operations"][0]["progress"] = "PUBLISHED"
    with pytest.raises(module.JournalDataError):
        module.validate_journal(prepared)

    mutating = _journal(state="MUTATING")
    mutating["operations"][0]["rollback_progress"] = "TARGET_RESTORED"
    with pytest.raises(module.JournalDataError):
        module.validate_journal(mutating)


def test_terminal_authority_requires_exact_generated_modes_and_after_state() -> None:
    """Reject unsafe targets or missing state from committed authority."""
    module = _load_module()
    unsafe_mode = _journal()
    unsafe_mode["operations"][0]["after_mode"] = "0644"
    with pytest.raises(module.JournalDataError, match="mode"):
        module.validate_journal(unsafe_mode)

    committed = _journal(state="COMMITTED")
    for item in committed["operations"]:
        item["progress"] = "VERIFIED"
    committed["after_state"] = {"exists": False, "relative_path": None, "sha256": None, "mode": None}
    with pytest.raises(module.JournalDataError, match="after state"):
        module.validate_journal(committed)


def test_constructed_immutable_values_are_revalidated_before_authority() -> None:
    """Prevent frozen dataclass construction from bypassing schema checks."""
    module = _load_module()
    before = module.validate_journal(_journal(state="MUTATING"))
    after_value = _journal(state="MUTATING")
    after_value["operations"][0]["progress"] = "PUBLISHED"
    after = module.validate_journal(after_value)
    forged_before = replace(before, package_hash="not-a-digest")
    forged_after = replace(after, package_hash="not-a-digest")

    with pytest.raises(module.JournalTransitionError):
        module.validate_successor(forged_before, forged_after)
    with pytest.raises(module.JournalDataError):
        module.canonical_journal_bytes(forged_before)
    malformed = replace(before, operations=(object(),))
    with pytest.raises(module.JournalDataError):
        module.canonical_journal_bytes(malformed)


def test_legal_state_path_reaches_commit_and_recovery() -> None:
    """Exercise the two journal-state branches with valid terminal evidence."""
    module = _load_module()
    prepared = _journal()
    mutating = copy.deepcopy(prepared)
    mutating["journal_state"] = "MUTATING"
    assert module.validate_successor(prepared, mutating).journal_state == "MUTATING"

    recovery = copy.deepcopy(mutating)
    recovery["journal_state"] = "RECOVERY_REQUIRED"
    assert module.validate_successor(mutating, recovery).journal_state == "RECOVERY_REQUIRED"

    verified = _journal(state="MUTATING")
    for item in verified["operations"]:
        item["progress"] = "VERIFIED"
    state_committed = copy.deepcopy(verified)
    state_committed["journal_state"] = "STATE_COMMITTED"
    module.validate_successor(verified, state_committed)
    committed = copy.deepcopy(state_committed)
    committed["journal_state"] = "COMMITTED"
    assert module.validate_successor(state_committed, committed).journal_state == "COMMITTED"


@pytest.mark.parametrize("path", ["/codex/", "/codex/../escape", "/codex/\udcff"])
def test_root_paths_reject_noncanonical_or_unencodable_values(path: str) -> None:
    """Keep journal root identities exact instead of normalized by parsing."""
    module = _load_module()
    value = _journal()
    value["codex_home_identity"]["canonical_path"] = path

    with pytest.raises(module.JournalDataError):
        module.validate_journal(value)


@pytest.mark.parametrize("field", ["action", "journal_state", "rollback_state_progress", "intent"])
def test_unhashable_enum_values_stay_inside_data_error_contract(field: str) -> None:
    """Convert hostile JSON container types into one stable journal error."""
    module = _load_module()
    value = _journal()
    if field == "intent":
        value["operations"][0][field] = []
    else:
        value[field] = []

    with pytest.raises(module.JournalDataError):
        module.validate_journal(value)


def test_kernel_ast_has_no_write_or_process_surface() -> None:
    """Keep the journal kernel structurally incapable of external effects."""
    tree = ast.parse(JOURNAL_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported.update((node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert imported.isdisjoint({"os", "pathlib", "shutil", "subprocess", "tempfile"})
    assert calls.isdisjoint({"open", "exec", "eval", "compile", "input"})
    assert attributes.isdisjoint({"open", "write", "write_bytes", "write_text", "replace", "rename", "unlink"})
