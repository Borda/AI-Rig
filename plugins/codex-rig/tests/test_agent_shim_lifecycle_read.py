"""Acceptance checks for the inert shim lifecycle parsing kernel."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_PATH = PLUGIN_ROOT / "scripts" / "_agent_shim_lifecycle.py"
GENERATOR_PATH = PLUGIN_ROOT / "scripts" / "generate_roles.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
DIGEST = "a" * 64


def _load_module(path: Path, name: str) -> ModuleType:
    """Load one installed script module without package assumptions."""
    if path == LIFECYCLE_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _role_ids() -> tuple[str, ...]:
    """Read the canonical roster from the generator implementation."""
    return _load_module(GENERATOR_PATH, "codex_rig_generator_roster").ROLE_IDS


def _marker_line(role_id: str, *, file_digest: str = DIGEST) -> bytes:
    """Build one exact marker fixture.

    Example:
        >>> _marker_line("challenger").startswith(b"# codex-rig")
        True
    """
    return (
        "# codex-rig-shim schema=1 plugin=codex-rig "
        f"install_id={INSTALL_ID} role_id={role_id} package_hash=sha256:{DIGEST} "
        f"role_hash=sha256:{file_digest} bootstrap=1 generator=1"
    ).encode()


def _root(path: str) -> dict[str, object]:
    """Build one exact persisted root identity.

    Example:
        >>> _root("/fixture")["canonical_path"]
        '/fixture'
    """
    return {"canonical_path": path, "device": 1, "inode": 2, "owner": 3, "group": 4, "mode": "0700"}


def _state_payload(*, status: str = "current") -> dict[str, object]:
    """Build one complete valid lifecycle state fixture."""
    roles = _role_ids()
    return {
        "schema": 1,
        "plugin": "codex-rig",
        "scope": "user",
        "install_id": INSTALL_ID,
        "plugin_version": "0.2.0",
        "package_hash": DIGEST,
        "codex_home_identity": _root("/codex"),
        "plugin_root_identity": _root("/plugin"),
        "state_root_identity": _root("/codex/codex-rig/shims"),
        "target_root_identity": _root("/codex/agents"),
        "roster_hash": DIGEST,
        "bootstrap": {"protocol": 1, "helper_path": "scripts/verify_role_link.py", "helper_hash": DIGEST},
        "generator_version": 1,
        "roles": [
            {
                "role_id": role_id,
                "target_name": f"codex-rig-{role_id}.toml",
                "card_path": f"roles/{role_id}/ROLE.md",
                "role_hash": DIGEST,
                "file_hash": DIGEST,
            }
            for role_id in roles
        ],
        "transaction_status": status,
    }


def _encode(value: object) -> bytes:
    """Encode one compact JSON fixture.

    Example:
        >>> _encode({"answer": 42})
        b'{"answer":42}'
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b'{"a":1,"a":2}', id="duplicate"),
        pytest.param(b'{"value":NaN}', id="nonfinite"),
        pytest.param(b"\xef\xbb\xbf{}", id="bom"),
        pytest.param(b"\xff", id="utf8"),
        pytest.param(b"[]", id="nonobject"),
        pytest.param(b"{} trailing", id="trailing"),
    ],
)
def test_strict_json_rejects_hostile_inputs(payload: bytes) -> None:
    """Prevent ambiguous or unbounded JSON from reaching lifecycle authority."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_json")

    with pytest.raises(module.LifecycleDataError):
        module.parse_json_object(payload, maximum=32)


def test_strict_json_converts_bounded_huge_integer_failure() -> None:
    """Keep interpreter integer limits inside the lifecycle error contract."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_huge_integer")
    payload = b'{"value":' + b"9" * 5000 + b"}"

    with pytest.raises(module.LifecycleDataError):
        module.parse_json_object(payload, maximum=len(payload))


def test_strict_json_converts_decoder_recursion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep decoder recursion failures inside the lifecycle error contract."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_deep_json")

    def _raise_recursion(*args: object, **kwargs: object) -> object:
        """Model decoder recursion failure without interpreter-specific depth assumptions."""
        raise RecursionError("fixture recursion limit")

    monkeypatch.setattr(module.json, "loads", _raise_recursion)

    with pytest.raises(module.LifecycleDataError) as caught:
        module.parse_json_object(b"{}", maximum=2)

    assert isinstance(caught.value.__cause__, RecursionError)


def test_marker_parser_accepts_current_and_historical_role_ids() -> None:
    """Accept strict role grammar without coupling old ownership to the active roster."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_marker")

    marker = module.parse_marker(_marker_line("challenger"))
    historical = module.parse_marker(_marker_line("retired-specialist"))

    assert marker == module.Marker(INSTALL_ID, "challenger", DIGEST, DIGEST)
    assert historical.role_id == "retired-specialist"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_marker_line("challenger") + b"\n", id="trailing-newline"),
        pytest.param(_marker_line("1unknown"), id="invalid-role-id"),
        pytest.param(b" " + _marker_line("challenger"), id="leading-whitespace"),
    ],
)
def test_marker_parser_rejects_noncanonical_payload(payload: bytes) -> None:
    """Reject trailing, malformed-role, and whitespace marker payloads."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_marker")

    with pytest.raises(module.LifecycleDataError):
        module.parse_marker(payload)


def test_state_parser_accepts_complete_current_and_removed_state() -> None:
    """Require exact schema and canonical roster ordering for both statuses."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_state")

    assert module.parse_state(_encode(_state_payload()))["transaction_status"] == "current"
    assert module.parse_state(_encode(_state_payload(status="removed")))["transaction_status"] == "removed"
    historical = _state_payload()
    historical["roles"] = historical["roles"][:-1]
    assert len(module.parse_state(_encode(historical))["roles"]) == len(_role_ids()) - 1


@pytest.mark.parametrize(
    "mutation",
    [
        "future",
        "boolean-schema",
        "invalid-version",
        "extra",
        "reordered",
        "bad-root",
        "aliased-root",
        "double-root",
        "control-root",
        "boolean-root",
    ],
)
def test_state_parser_rejects_incompatible_or_partial_state(mutation: str) -> None:
    """Prevent malformed ownership state from authorizing target changes."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_bad_state")
    value = _state_payload()
    if mutation == "future":
        value["schema"] = 2
    elif mutation == "boolean-schema":
        value["schema"] = True
    elif mutation == "invalid-version":
        value["plugin_version"] = "02.0"
    elif mutation == "extra":
        value["unexpected"] = True
    elif mutation == "reordered":
        value["roles"][0], value["roles"][1] = value["roles"][1], value["roles"][0]
    elif mutation == "bad-root":
        value["target_root_identity"]["mode"] = "0999"
    elif mutation == "aliased-root":
        value["target_root_identity"]["canonical_path"] = "/codex/../tmp"
    elif mutation == "double-root":
        value["target_root_identity"]["canonical_path"] = "//remote/path"
    elif mutation == "control-root":
        value["target_root_identity"]["canonical_path"] = "/codex/agents\nforeign"
    else:
        value["target_root_identity"]["device"] = True

    with pytest.raises(module.LifecycleDataError):
        module.parse_state(_encode(value))


def _observations(module: ModuleType, state: dict[str, object] | None, kind: str) -> dict[str, object]:
    """Build one complete target observation roster."""
    result = {}
    for role_id in _role_ids():
        name = f"codex-rig-{role_id}.toml"
        marker = module.parse_marker(_marker_line(role_id)) if kind == "regular" else None
        result[name] = module.TargetObservation(kind, DIGEST if kind == "regular" else None, marker)
    return result


def test_target_classification_preserves_foreign_modified_and_unsafe() -> None:
    """Separate absent, current, modified, foreign, and unsafe rosters."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_targets")
    parsed = module.parse_state(_encode(_state_payload()))

    assert module.classify_targets(None, _observations(module, None, "absent")) == "absent"
    assert module.classify_targets(None, _observations(module, None, "regular")) == "foreign"
    assert module.classify_targets(parsed, _observations(module, parsed, "regular")) == "current"
    modified = _observations(module, parsed, "regular")
    modified["codex-rig-challenger.toml"] = module.TargetObservation("regular", "b" * 64, None)
    assert module.classify_targets(parsed, modified) == "modified"
    assert module.classify_targets(parsed, _observations(module, parsed, "unsafe")) == "unsafe"


def test_target_classification_handles_missing_and_removed_tombstones() -> None:
    """Distinguish repairable absence from removed-state conflicts."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_removed")
    current = module.parse_state(_encode(_state_payload()))
    removed = module.parse_state(_encode(_state_payload(status="removed")))
    missing = _observations(module, current, "regular")
    missing["codex-rig-challenger.toml"] = module.TargetObservation("absent")

    assert module.classify_targets(current, missing) == "repairable-missing"
    assert module.classify_targets(removed, _observations(module, removed, "absent")) == "removed"
    assert module.classify_targets(removed, missing) == "removed-conflict"


@pytest.mark.parametrize(
    ("status", "unpersisted_kind", "expected"),
    [
        pytest.param("current", "regular", "foreign", id="current-foreign-outranks-modified"),
        pytest.param("removed", "regular", "removed-conflict", id="removed-conflict-outranks-modified"),
        pytest.param("current", "unsafe", "unsafe", id="current-unsafe-outranks-modified"),
        pytest.param("removed", "unsafe", "unsafe", id="removed-unsafe-outranks-modified"),
    ],
)
def test_target_classification_preserves_mixed_roster_precedence(
    status: str,
    unpersisted_kind: str,
    expected: str,
) -> None:
    """Scan later unknown and unsafe targets before returning an earlier modification."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_mixed_targets")
    state_payload = _state_payload(status=status)
    unpersisted_role = _role_ids()[-1]
    state_payload["roles"] = [role for role in state_payload["roles"] if role["role_id"] != unpersisted_role]
    parsed = module.parse_state(_encode(state_payload))
    targets = _observations(module, parsed, "regular")
    modified_name = parsed["roles"][0]["target_name"]
    targets[modified_name] = module.TargetObservation("regular", "b" * 64, None)
    unpersisted_name = f"codex-rig-{unpersisted_role}.toml"
    targets[unpersisted_name] = module.TargetObservation(unpersisted_kind)

    assert module.classify_targets(parsed, targets) == expected


def test_target_classification_rejects_an_incomplete_observation_roster() -> None:
    """Reject a partial roster before interpreting its ownership observations."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_target_roster")
    parsed = module.parse_state(_encode(_state_payload()))
    targets = _observations(module, parsed, "regular")
    targets.pop(f"codex-rig-{_role_ids()[-1]}.toml")

    with pytest.raises(module.LifecycleDataError, match="target observation roster mismatch"):
        module.classify_targets(parsed, targets)


def test_recovery_classification_is_single_exact_and_fail_closed() -> None:
    """Reject unknown or competing recovery authority."""
    module = _load_module(LIFECYCLE_PATH, "codex_rig_lifecycle_recovery")

    assert module.classify_recovery(()) == "none"
    assert module.classify_recovery((module.RecoveryObservation("journal", True),)) == "journal"
    assert module.classify_recovery((module.RecoveryObservation("probe-receipt", True),)) == "probe-receipt"
    assert module.classify_recovery((module.RecoveryObservation("empty-probe", True, True),)) == "empty-probe"
    assert module.classify_recovery((module.RecoveryObservation("unknown", False),)) == "blocked-unknown"
    assert module.classify_recovery((module.RecoveryObservation("journal", "yes"),)) == "blocked-unknown"
    assert (
        module.classify_recovery(
            (module.RecoveryObservation("journal", True), module.RecoveryObservation("probe-receipt", True))
        )
        == "blocked-multiple"
    )


def test_kernel_has_no_filesystem_mutation_surface() -> None:
    """Keep the parsing kernel structurally incapable of lifecycle writes."""
    source = LIFECYCLE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    imported.update((node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
    called_names = {
        node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    attribute_names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert imported.isdisjoint({"os", "pathlib", "shutil", "subprocess", "tempfile"})
    assert called_names.isdisjoint({"open", "exec", "eval", "compile", "input"})
    assert attribute_names.isdisjoint({"open", "write", "write_bytes", "write_text", "replace", "rename", "unlink"})
    assert not any(isinstance(node, ast.Delete) for node in ast.walk(tree))
