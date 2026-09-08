"""Acceptance checks for read-only shim filesystem observation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest


WINDOWS_POSIX_SKIP_REASON = "requires POSIX filesystem modes, links, and executable semantics"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
JOURNAL_PATH = SCRIPTS / "_agent_shim_journal.py"
OBSERVER_PATH = SCRIPTS / "_agent_shim_observe.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
PACKAGE_HASH = "a" * 64
ROLE_HASH = "b" * 64


def _load_module(path: Path, name: str) -> ModuleType:
    """Load one sibling script with its direct dependencies available."""
    if path != GENERATOR_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    if path == OBSERVER_PATH and "_agent_shim_lifecycle" not in sys.modules:
        _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    if path == OBSERVER_PATH and "_agent_shim_journal" not in sys.modules:
        _load_module(JOURNAL_PATH, "_agent_shim_journal")
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    """Encode canonical lifecycle JSON.

    Example:
        >>> _canonical({"z": 0, "a": 1})
        b'{"a":1,"z":0}'
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _mode_is_retainable(mode: int) -> bool:
    """Return whether the host filesystem preserves one requested permission mode."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "mode-probe"
        path.mkdir(mode=0o700)
        path.chmod(mode)
        return stat.S_IMODE(path.stat().st_mode) == mode


def _fifo_is_creatable() -> bool:
    """Return whether the host permits a FIFO in its temporary directory."""
    if not hasattr(os, "mkfifo"):
        return False
    with tempfile.TemporaryDirectory() as temporary:
        try:
            os.mkfifo(Path(temporary) / "fifo")
        except OSError:
            return False
    return True


FIFO_UNAVAILABLE = not _fifo_is_creatable()


def _identity(path: Path) -> dict[str, object]:
    """Capture one persisted root identity fixture.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     _identity(Path(directory))["canonical_path"] == directory
        True
    """
    metadata = path.stat()
    return {
        "canonical_path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "owner": metadata.st_uid,
        "group": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _marker(role_id: str) -> bytes:
    """Return one exact ownership marker.

    Example:
        >>> _marker("challenger").startswith(b"# codex-rig")
        True
    """
    return (
        "# codex-rig-shim schema=1 plugin=codex-rig "
        f"install_id={INSTALL_ID} role_id={role_id} package_hash=sha256:{PACKAGE_HASH} "
        f"role_hash=sha256:{ROLE_HASH} bootstrap=1 generator=1"
    ).encode()


def _shim(role_id: str) -> bytes:
    """Return one minimal marker-bearing target fixture.

    Example:
        >>> b"challenger" in _shim("challenger")
        True
    """
    return _marker(role_id) + b'\nname = "fixture"\n'


def _write_private(path: Path, payload: bytes) -> None:
    """Write test evidence with the lifecycle file mode.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     path = Path(directory) / "state.json"
        ...     _write_private(path, b"state")
        ...     path.read_bytes()
        b'state'
    """
    path.write_bytes(payload)
    path.chmod(0o600)


def _preparing_journal_payload() -> bytes:
    """Build one valid initial transaction authority for observer recovery."""
    operations = []
    for role_id in _role_ids():
        operations.append(
            {
                "role_id": role_id,
                "intent": "create",
                "target_name": f"codex-rig-{role_id}.toml",
                "before_exists": False,
                "before_hash": None,
                "before_mode": None,
                "after_exists": True,
                "after_hash": PACKAGE_HASH,
                "after_mode": "0600",
                "before_image": None,
                "after_image": f"after/{role_id}.toml",
                "quarantine_name": None,
                "progress": "PLANNED",
                "rollback_progress": "NOT_STARTED",
            }
        )
    root = {"canonical_path": "/fixture", "device": 1, "inode": 2, "owner": 3, "group": 4, "mode": "0700"}
    return _canonical(
        {
            "schema": 1,
            "transaction_id": INSTALL_ID,
            "transaction_nonce": INSTALL_ID,
            "install_id": INSTALL_ID,
            "action": "install",
            "approved_plan_digest": PACKAGE_HASH,
            "package_hash": PACKAGE_HASH,
            "roster_hash": PACKAGE_HASH,
            "codex_home_identity": root,
            "target_root_identity": root,
            "state_root_identity": root,
            "before_state": {"exists": False, "relative_path": None, "sha256": None, "mode": None},
            "after_state": {
                "exists": True,
                "relative_path": "state.after.json",
                "sha256": PACKAGE_HASH,
                "mode": "0600",
            },
            "rollback_state_progress": "PENDING",
            "journal_state": "PREPARING",
            "operations": operations,
        }
    )


def _prepared_transaction(transaction: Path) -> dict[str, object]:
    """Write one exact PREPARED create transaction and return its journal."""
    after_payload = _shim("challenger")
    state_payload = b'{"fixture":"after"}'
    value = json.loads(_preparing_journal_payload())
    operation = value["operations"][0]
    value["operations"] = [operation]
    operation["role_id"] = "challenger"
    operation["target_name"] = "codex-rig-challenger.toml"
    operation["after_hash"] = hashlib.sha256(after_payload).hexdigest()
    operation["after_image"] = "after/challenger.toml"
    value["after_state"]["sha256"] = hashlib.sha256(state_payload).hexdigest()
    value["journal_state"] = "PREPARED"
    _write_private(transaction / "journal.json", _canonical(value))
    _write_private(transaction / "state.after.json", state_payload)
    after = transaction / "after"
    after.mkdir(mode=0o700)
    after.chmod(0o700)
    _write_private(after / "challenger.toml", after_payload)
    return value


def _make_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create disposable roots used only by the test harness."""
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "plugin"
    target_root = codex_home / "agents"
    state_root = codex_home / "codex-rig" / "shims"
    target_root.mkdir(parents=True, mode=0o700)
    state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    plugin_root.mkdir(mode=0o700)
    return codex_home, plugin_root, target_root, state_root


def _role_ids() -> tuple[str, ...]:
    """Read the exact generator-owned roster."""
    return _load_module(GENERATOR_PATH, "codex_rig_observe_roster").ROLE_IDS


def _make_state(
    codex_home: Path,
    plugin_root: Path,
    target_root: Path,
    state_root: Path,
    *,
    status: str = "current",
) -> bytes:
    """Build state bound to the disposable roots and target bytes."""
    roles = [
        {
            "role_id": role_id,
            "target_name": f"codex-rig-{role_id}.toml",
            "card_path": f"roles/{role_id}/ROLE.md",
            "role_hash": ROLE_HASH,
            "file_hash": hashlib.sha256(_shim(role_id)).hexdigest(),
        }
        for role_id in _role_ids()
    ]
    roster = [{key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles]
    return _canonical(
        {
            "schema": 1,
            "plugin": "codex-rig",
            "scope": "user",
            "install_id": INSTALL_ID,
            "plugin_version": "0.2.0",
            "package_hash": PACKAGE_HASH,
            "codex_home_identity": _identity(codex_home),
            "plugin_root_identity": _identity(plugin_root),
            "state_root_identity": _identity(state_root),
            "target_root_identity": _identity(target_root),
            "roster_hash": hashlib.sha256(_canonical(roster)).hexdigest(),
            "bootstrap": {
                "protocol": 1,
                "helper_path": "scripts/verify_role_link.py",
                "helper_hash": "c" * 64,
            },
            "generator_version": 1,
            "roles": roles,
            "transaction_status": status,
        }
    )


def _snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture bytes and mutation-relevant metadata without following links."""
    rows = []
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        payload = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
        rows.append(
            (
                str(path.relative_to(root)),
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                payload,
                os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else None,
            )
        )
    return tuple(rows)


def _observe(module: ModuleType, codex_home: Path, plugin_root: Path) -> object:
    """Observe the disposable roots through the public read-only entry."""
    return module.observe_filesystem(codex_home=codex_home, plugin_root=plugin_root)


_skip_windows_posix = pytest.mark.skipif(sys.platform == "win32", reason=WINDOWS_POSIX_SKIP_REASON)


@_skip_windows_posix
def test_absent_roster_observation_is_bounded_degraded_and_zero_write(tmp_path: Path) -> None:
    """Prove empty disposable roots remain byte-for-byte and metadata unchanged."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_absent")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.state == "absent"
    assert result.targets == "absent"
    assert result.recovery == "none"
    assert len(result.target_observations) == 15
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_absent_roots_bind_nearest_ancestor_lock_intent_and_suffix(tmp_path: Path) -> None:
    """Preserve exact root-creation evidence without probing by mutation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_absent_roots")
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "plugin"
    codex_home.mkdir(mode=0o700)
    plugin_root.mkdir(mode=0o700)
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.target_root_observation.exists is False
    assert result.target_root_observation.canonical_path == str(codex_home / "agents")
    assert result.target_root_observation.nearest_existing_ancestor.canonical_path == str(codex_home)
    assert result.target_root_observation.missing_suffix_components == ("agents",)
    assert result.state_root_observation.exists is False
    assert result.state_root_observation.nearest_existing_ancestor.canonical_path == str(codex_home)
    assert result.state_root_observation.missing_suffix_components == ("codex-rig", "shims")
    assert result.coordination_lock_observation.kind == "absent"
    assert result.coordination_lock_observation.intent == "create-if-absent"
    assert result.state_payload is None
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_writable_codex_home_blocks_lifecycle_authority(tmp_path: Path) -> None:
    """Reject a namespace another account could substitute during mutation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_writable_home")
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "plugin"
    codex_home.mkdir(mode=0o700)
    codex_home.chmod(0o770)
    plugin_root.mkdir(mode=0o700)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.codex_home_observation is None


@_skip_windows_posix
def test_existing_empty_lock_and_partial_state_root_are_bound_read_only(tmp_path: Path) -> None:
    """Bind an existing lock and deepest state ancestor without changing either."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_existing_lock")
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "plugin"
    state_parent = codex_home / "codex-rig"
    state_parent.mkdir(parents=True, mode=0o700)
    state_parent.chmod(0o700)
    plugin_root.mkdir(mode=0o700)
    lock = codex_home / ".codex-rig-shims.lock"
    _write_private(lock, b"")
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.coordination_lock_observation.kind == "regular"
    assert result.coordination_lock_observation.intent == "open-existing"
    assert result.coordination_lock_observation.size == 0
    assert result.state_root_observation.nearest_existing_ancestor.canonical_path == str(state_parent)
    assert result.state_root_observation.missing_suffix_components == ("shims",)
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
@pytest.mark.parametrize(
    "node",
    [
        "symlink",
        "directory",
        pytest.param("fifo", marks=pytest.mark.skipif(FIFO_UNAVAILABLE, reason="FIFO creation is unavailable")),
        "nonempty",
        "mode",
    ],
)
def test_unsafe_coordination_lock_blocks(node: str, tmp_path: Path) -> None:
    """Reject a lock that cannot be safely opened as the fixed owned file."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_lock_{node}")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)
    lock = codex_home / ".codex-rig-shims.lock"
    if node == "symlink":
        outside = tmp_path / "outside-lock"
        outside.write_bytes(b"")
        lock.symlink_to(outside)
    elif node == "directory":
        lock.mkdir(mode=0o700)
    elif node == "fifo":
        os.mkfifo(lock, mode=0o600)
    elif node == "nonempty":
        _write_private(lock, b"unexpected")
    else:
        lock.write_bytes(b"")
        lock.chmod(0o644)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.coordination_lock_observation.kind == "unsafe"
    assert result.coordination_lock_observation.intent is None


@_skip_windows_posix
def test_lock_swap_to_fifo_is_nonblocking_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent a stat-to-open lock race from hanging read-only observation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_lock_fifo_race")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)
    lock = codex_home / ".codex-rig-shims.lock"
    _write_private(lock, b"")
    original_open = module.os.open
    swapped = False

    def _racing_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        """Replace the observed lock with a FIFO at the first nonblocking open."""
        nonlocal swapped
        if path == lock.name and not swapped and flags & os.O_NONBLOCK:
            swapped = True
            lock.unlink()
            os.mkfifo(lock, mode=0o600)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", _racing_open)

    result = _observe(module, codex_home, plugin_root)

    assert swapped is True
    assert result.classification == "blocked"
    assert result.coordination_lock_observation.kind == "unsafe"


@_skip_windows_posix
def test_target_swap_to_fifo_is_nonblocking_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent a stat-to-open target race from hanging read-only observation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_target_fifo_race")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    target = target_root / "codex-rig-challenger.toml"
    _write_private(target, _shim("challenger"))
    original_open = module.os.open
    swapped = False

    def _racing_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        """Replace the observed target with a FIFO at the first nonblocking open."""
        nonlocal swapped
        if path == target.name and not swapped and flags & os.O_NONBLOCK:
            swapped = True
            target.unlink()
            os.mkfifo(target, mode=0o600)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", _racing_open)

    result = _observe(module, codex_home, plugin_root)

    assert swapped is True
    assert result.classification == "blocked"
    assert result.targets == "unsafe"


@_skip_windows_posix
def test_exact_current_roster_is_observed_without_claiming_health(tmp_path: Path) -> None:
    """Bind exact state, root identities, markers, and complete target hashes."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_current")
    codex_home, plugin_root, target_root, state_root = _make_roots(tmp_path)
    for role_id in _role_ids():
        _write_private(target_root / f"codex-rig-{role_id}.toml", _shim(role_id))
    _write_private(state_root / "state.json", _make_state(codex_home, plugin_root, target_root, state_root))
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.state == "current"
    assert result.targets == "current"
    assert result.recovery == "none"
    assert result.codex_home_identity.canonical_path == str(codex_home)
    assert result.plugin_root_identity.canonical_path == str(plugin_root)
    assert result.target_root_observation.exists is True
    assert result.state_root_observation.exists is True
    assert result.state_payload == (state_root / "state.json").read_bytes()
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_historical_roster_and_old_cache_identity_are_migration_evidence(tmp_path: Path) -> None:
    """Authenticate persisted retired targets without requiring the old cache to exist."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_historical_roster")
    codex_home, plugin_root, target_root, state_root = _make_roots(tmp_path)
    state = json.loads(_make_state(codex_home, plugin_root, target_root, state_root))
    state["plugin_version"] = "0.1.0"
    state["plugin_root_identity"]["canonical_path"] = str(tmp_path / "removed-cache")
    state["roles"] = [role for role in state["roles"] if role["role_id"] != "web-explorer"]
    retired_payload = _shim("retired-specialist")
    state["roles"].append(
        {
            "role_id": "retired-specialist",
            "target_name": "codex-rig-retired-specialist.toml",
            "card_path": "roles/retired-specialist/ROLE.md",
            "role_hash": ROLE_HASH,
            "file_hash": hashlib.sha256(retired_payload).hexdigest(),
        }
    )
    state["roles"].sort(key=lambda role: role["role_id"])
    roster = [
        {key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in state["roles"]
    ]
    state["roster_hash"] = hashlib.sha256(_canonical(roster)).hexdigest()
    for role in state["roles"]:
        payload = retired_payload if role["role_id"] == "retired-specialist" else _shim(role["role_id"])
        _write_private(target_root / role["target_name"], payload)
    _write_private(state_root / "state.json", _canonical(state))

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.state == "current"
    assert result.targets == "current"
    assert result.namespace_inventory_status == "complete"
    assert result.namespace_candidates == ()
    assert len(result.target_observations) == 16
    assert dict(result.target_observations)["codex-rig-web-explorer.toml"].kind == "absent"


@_skip_windows_posix
@pytest.mark.parametrize(
    "node",
    [
        "symlink",
        "directory",
        pytest.param("fifo", marks=pytest.mark.skipif(FIFO_UNAVAILABLE, reason="FIFO creation is unavailable")),
    ],
)
def test_hostile_target_nodes_fail_closed(tmp_path: Path, node: str) -> None:
    """Prevent no-follow target observation from accepting aliased or non-files."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_target_{node}")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    target = target_root / "codex-rig-challenger.toml"
    if node == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"sentinel")
        target.symlink_to(outside)
    elif node == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.targets == "unsafe"
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_foreign_and_marker_only_targets_never_become_owned(tmp_path: Path) -> None:
    """Keep namespace and marker evidence insufficient without valid state."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_foreign")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    _write_private(target_root / "codex-rig-challenger.toml", _marker("challenger") + b"\n")

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.targets == "foreign"


@_skip_windows_posix
def test_retired_namespace_file_is_inventoried_without_ownership(tmp_path: Path) -> None:
    """List a retired-looking regular file while refusing lifecycle authority."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_retired_namespace")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    _write_private(target_root / "codex-rig-retired-role.toml", b"untrusted\n")
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.targets == "absent"
    assert result.namespace_inventory_status == "complete"
    assert tuple((item.name, item.kind) for item in result.namespace_candidates) == (
        ("codex-rig-retired-role.toml", "regular"),
    )
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_namespace_candidate_preserves_exact_current_role_observations(tmp_path: Path) -> None:
    """Block an unmanaged candidate without degrading authenticated current roles."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_current_with_retired")
    codex_home, plugin_root, target_root, state_root = _make_roots(tmp_path)
    for role_id in _role_ids():
        _write_private(target_root / f"codex-rig-{role_id}.toml", _shim(role_id))
    _write_private(state_root / "state.json", _make_state(codex_home, plugin_root, target_root, state_root))
    _write_private(target_root / "codex-rig-retired-role.toml", _marker("challenger") + b"\n")

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.targets == "current"
    assert all(observation.kind == "regular" for _, observation in result.target_observations)
    assert tuple((item.name, item.kind) for item in result.namespace_candidates) == (
        ("codex-rig-retired-role.toml", "regular"),
    )


@_skip_windows_posix
@pytest.mark.parametrize(
    "node",
    [
        "symlink",
        "directory",
        pytest.param("fifo", marks=pytest.mark.skipif(FIFO_UNAVAILABLE, reason="FIFO creation is unavailable")),
    ],
)
def test_unsafe_namespace_candidate_remains_visible(tmp_path: Path, node: str) -> None:
    """Inventory a nonregular namespace candidate without following or reading it."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_retired_{node}")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    candidate = target_root / "codex-rig-retired-role.toml"
    if node == "symlink":
        outside = tmp_path / "outside-retired"
        outside.write_bytes(b"sentinel")
        candidate.symlink_to(outside)
    elif node == "directory":
        candidate.mkdir()
    else:
        os.mkfifo(candidate)
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert tuple((item.name, item.kind) for item in result.namespace_candidates) == ((candidate.name, "unsafe"),)
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_malformed_namespace_candidate_fails_closed_and_remains_visible(tmp_path: Path) -> None:
    """Expose a namespace-like basename that violates the strict role grammar."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_malformed_namespace")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    _write_private(target_root / "codex-rig-Retired.toml", b"untrusted\n")

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert tuple((item.name, item.kind) for item in result.namespace_candidates) == (
        ("codex-rig-Retired.toml", "malformed"),
    )


@_skip_windows_posix
def test_unrelated_target_names_are_ignored_by_namespace_inventory(tmp_path: Path) -> None:
    """Leave unrelated agent-root entries outside Codex Rig lifecycle authority."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_unrelated_namespace")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    for name in ("codex-rig-retired.txt", "notes.toml", "other-codex-rig-role.toml"):
        _write_private(target_root / name, b"unrelated\n")

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.namespace_inventory_status == "complete"
    assert result.namespace_candidates == ()


@_skip_windows_posix
def test_namespace_inventory_order_is_deterministic(tmp_path: Path) -> None:
    """Sort namespace candidates independently of filesystem enumeration order."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_namespace_order")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    for name in ("codex-rig-zeta.toml", "codex-rig-alpha.toml"):
        _write_private(target_root / name, b"untrusted\n")

    result = _observe(module, codex_home, plugin_root)

    assert tuple(item.name for item in result.namespace_candidates) == (
        "codex-rig-alpha.toml",
        "codex-rig-zeta.toml",
    )


@_skip_windows_posix
def test_target_root_inventory_overflow_is_bounded_and_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop bounded enumeration and expose overflow as a fail-closed condition."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_namespace_overflow")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    monkeypatch.setattr(module, "MAX_TARGET_DIRECTORY_ENTRIES", 8)
    for index in range(9):
        _write_private(target_root / f"unrelated-{index:03d}", b"x")
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.targets == "unsafe"
    assert result.namespace_inventory_status == "overflow"
    assert result.namespace_candidates == ()
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
@pytest.mark.parametrize(
    "evidence",
    ["corrupt-state", "huge-integer-state", "deep-state", "oversized-state", "oversized-target", "state-symlink"],
)
def test_corrupt_oversized_and_aliased_evidence_blocks(tmp_path: Path, evidence: str) -> None:
    """Bound state and target reads and reject unsafe lifecycle evidence."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_{evidence}")
    codex_home, plugin_root, target_root, state_root = _make_roots(tmp_path)
    state_path = state_root / "state.json"
    if evidence == "corrupt-state":
        _write_private(state_path, b'{"schema":1,"schema":2}')
    elif evidence == "huge-integer-state":
        _write_private(state_path, b'{"schema":' + b"9" * 5000 + b"}")
    elif evidence == "deep-state":
        _write_private(state_path, b'{"schema":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}")
    elif evidence == "oversized-state":
        _write_private(state_path, b"x" * (module.STATE_BYTES + 1))
    elif evidence == "oversized-target":
        _write_private(target_root / "codex-rig-challenger.toml", b"x" * (module.SHIM_BYTES + 1))
    else:
        outside = tmp_path / "outside-state"
        outside.write_bytes(b"sentinel")
        state_path.symlink_to(outside)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.state in {"absent", "corrupt", "unsafe"}
    assert result.targets in {"absent", "unsafe"}


@_skip_windows_posix
def test_owned_protected_target_root_mode_0755_is_accepted(tmp_path: Path) -> None:
    """Accept a user-owned target namespace that other users cannot mutate."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_protected_target")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    target_root.chmod(0o755)
    before = _snapshot(tmp_path)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
    assert result.reason == "runtime and mutation prerequisites remain unverified"
    assert result.target_root_observation.identity.mode == "0755"
    assert result.targets == "absent"
    assert result.namespace_inventory_status == "complete"
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(
            mode,
            id=case_id,
            marks=pytest.mark.skipif(
                not _mode_is_retainable(mode), reason=f"filesystem does not retain mode {mode:04o}"
            ),
        )
        for mode, case_id in ((0o4700, "setuid"), (0o2700, "setgid"), (0o1700, "sticky"))
    ],
)
def test_protected_target_root_special_bits_block(tmp_path: Path, mode: int) -> None:
    """Reject every special permission bit from the mutable target namespace."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_target_special_{mode:04o}")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    target_root.chmod(mode)
    observed_mode = stat.S_IMODE(target_root.stat().st_mode)
    assert observed_mode == mode

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.reason == (
        f"unsafe protected directory mode: {target_root}; "
        f"expected no group/world write or special bits, observed {mode:04o}"
    )


@_skip_windows_posix
@pytest.mark.parametrize("evidence", ["target-root-mode", "state-root-mode", "target-file-mode", "control-name"])
def test_nonprivate_or_ambiguous_local_evidence_blocks(tmp_path: Path, evidence: str) -> None:
    """Require private owned lifecycle roots and unambiguous contained names."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_private_{evidence}")
    codex_home, plugin_root, target_root, state_root = _make_roots(tmp_path)
    if evidence == "target-root-mode":
        target_root.chmod(0o775)
    elif evidence == "state-root-mode":
        state_root.chmod(0o755)
    elif evidence == "target-file-mode":
        target = target_root / "codex-rig-challenger.toml"
        target.write_bytes(_shim("challenger"))
        target.chmod(0o644)
    else:
        _write_private(state_root / "control\nname", b"evidence")

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    if evidence == "target-root-mode":
        assert result.reason == (
            f"unsafe protected directory mode: {target_root}; "
            "expected no group/world write or special bits, observed 0775"
        )
    elif evidence == "state-root-mode":
        assert result.reason == (f"unsafe private directory mode: {state_root}; expected 0700, observed 0755")


@_skip_windows_posix
@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        pytest.param(
            "transactions/123e4567-e89b-42d3-a456-426614174000",
            "empty-transaction",
            id="transactions-123e4567-e89b-42d3-a456-426614174000",
        ),
        pytest.param(
            ".probe-123e4567-e89b-42d3-a456-426614174000",
            "empty-probe",
            id=".probe-123e4567-e89b-42d3-a456-426614174000",
        ),
    ],
)
def test_exact_empty_recovery_residue_is_recognized(tmp_path: Path, relative: str, expected: str) -> None:
    """Recognize only private empty nonce-bound recovery directories."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_{expected}")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    residue = state_root / relative
    residue.mkdir(parents=True, mode=0o700)
    residue.chmod(0o700)
    if relative.startswith("transactions/"):
        (state_root / "transactions").chmod(0o700)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == expected


@_skip_windows_posix
def test_initial_preparation_residue_is_recognized_without_parsing_partial_bytes(tmp_path: Path) -> None:
    """Bind the sole pre-authority crash artifact for explicit cleanup."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_preparing_residue")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    _write_private(transaction / "journal.initial.json", b'{"partial"')

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == "preparing-residue"


@_skip_windows_posix
def test_dual_link_initial_journal_crash_is_recognized(tmp_path: Path) -> None:
    """Recognize the durable window before the initial journal link retires."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_linked_initial_journal")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    _write_private(transaction / "journal.initial.json", _preparing_journal_payload())
    os.link(transaction / "journal.initial.json", transaction / "journal.json")

    result = _observe(module, codex_home, plugin_root)

    assert (transaction / "journal.json").stat().st_nlink == 2
    assert result.classification == "blocked"
    assert result.recovery == "journal"


@_skip_windows_posix
@pytest.mark.parametrize("partial", [False, True])
def test_single_link_preparing_journal_is_cleanable(tmp_path: Path, partial: bool) -> None:
    """Recognize every ordinary pre-mutation preparation crash window."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_preparing_{partial}")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    _write_private(transaction / "journal.json", _preparing_journal_payload())
    if partial:
        after = transaction / "after"
        after.mkdir(mode=0o700)
        after.chmod(0o700)
        artifact = after / "challenger.toml"
        artifact.write_bytes(b"partial")
        artifact.chmod(0o000)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == "journal"


@_skip_windows_posix
def test_exact_prepared_transaction_is_recognized(tmp_path: Path) -> None:
    """Recognize complete hash-bound artifacts after durable preparation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_prepared")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    _prepared_transaction(transaction)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == "journal"


@_skip_windows_posix
@pytest.mark.parametrize(
    "tamper",
    ["artifact-bytes", "artifact-mode", "extra-artifact", "transaction-id", "illegal-successor"],
)
def test_prepared_transaction_rejects_substituted_or_expanded_authority(tmp_path: Path, tamper: str) -> None:
    """Reject recovery when any durable authority or artifact is not exact."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_prepared_{tamper}")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    value = _prepared_transaction(transaction)
    if tamper == "artifact-bytes":
        _write_private(transaction / "after" / "challenger.toml", b"substituted")
    elif tamper == "artifact-mode":
        (transaction / "state.after.json").chmod(0o644)
    elif tamper == "extra-artifact":
        _write_private(transaction / "after" / "other.toml", b"foreign")
    elif tamper == "transaction-id":
        value["transaction_id"] = "123e4567-e89b-42d3-a456-426614174001"
        value["transaction_nonce"] = value["transaction_id"]
        _write_private(transaction / "journal.json", _canonical(value))
    else:
        value["package_hash"] = "d" * 64
        _write_private(transaction / "journal.next.json", _canonical(value))

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == "blocked-unknown"


@_skip_windows_posix
def test_prepared_transaction_accepts_one_legal_journal_successor(tmp_path: Path) -> None:
    """Accept a crash-preserved next journal only for one legal transition."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_prepared_successor")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    successor = _prepared_transaction(transaction)
    successor["journal_state"] = "MUTATING"
    _write_private(transaction / "journal.next.json", _canonical(successor))

    result = _observe(module, codex_home, plugin_root)

    assert result.recovery == "journal"


@_skip_windows_posix
@pytest.mark.parametrize("same_inode", [True, False])
def test_prepared_transaction_binds_state_publish_inode(tmp_path: Path, same_inode: bool) -> None:
    """Accept a staged state publication only when it links the after-state inode."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_state_publish_{same_inode}")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    _prepared_transaction(transaction)
    if same_inode:
        os.link(transaction / "state.after.json", transaction / "state.publish.json")
    else:
        _write_private(transaction / "state.publish.json", (transaction / "state.after.json").read_bytes())

    result = _observe(module, codex_home, plugin_root)

    assert result.recovery == ("journal" if same_inode else "blocked-unknown")


@_skip_windows_posix
@pytest.mark.parametrize("journal_state", ["MUTATING", "RECOVERY_REQUIRED"])
def test_recovery_accepts_one_unjournaled_create_publication(tmp_path: Path, journal_state: str) -> None:
    """Recognize a published exact target when recovery authority lags one step."""
    module = _load_module(OBSERVER_PATH, f"codex_rig_observe_recovery_create_window_{journal_state}")
    codex_home, plugin_root, target_root, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    (state_root / "transactions").chmod(0o700)
    value = _prepared_transaction(transaction)
    value["journal_state"] = journal_state
    _write_private(transaction / "journal.json", _canonical(value))
    os.link(
        transaction / "after" / "challenger.toml",
        target_root / "codex-rig-challenger.toml",
    )

    result = _observe(module, codex_home, plugin_root)

    assert result.recovery == "journal"


@_skip_windows_posix
def test_unknown_or_multiple_recovery_residue_fails_closed(tmp_path: Path) -> None:
    """Refuse ambiguous recovery authority without reading outside the state root."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_recovery_blocked")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    (state_root / "unknown").mkdir()

    unknown = _observe(module, codex_home, plugin_root)

    assert unknown.classification == "blocked"
    assert unknown.recovery == "blocked-unknown"
    (state_root / "unknown").rmdir()
    for name in (
        "transactions/123e4567-e89b-42d3-a456-426614174000",
        ".probe-123e4567-e89b-42d3-a456-426614174001",
    ):
        residue = state_root / name
        residue.mkdir(parents=True, mode=0o700)
        residue.chmod(0o700)

    multiple = _observe(module, codex_home, plugin_root)

    assert multiple.classification == "blocked"
    assert multiple.recovery == "blocked-multiple"


@_skip_windows_posix
def test_nonempty_recovery_receipt_stays_untrusted_until_full_schema_validation(tmp_path: Path) -> None:
    """Never grant recovery authority from a bounded receipt shape alone."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_untrusted_receipt")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transaction = state_root / "transactions" / INSTALL_ID
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    _write_private(transaction / "journal.json", b"{}")

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == "blocked-unknown"


@_skip_windows_posix
def test_nonprivate_transactions_container_blocks_even_when_empty(tmp_path: Path) -> None:
    """Require private ownership metadata on the transaction container itself."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_transactions_mode")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    transactions = state_root / "transactions"
    transactions.mkdir(mode=0o755)
    transactions.chmod(0o755)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.recovery == "blocked-unknown"


@_skip_windows_posix
def test_observer_rejects_relative_and_symlinked_supplied_roots(tmp_path: Path) -> None:
    """Require explicit canonical absolute roots and no-follow every component."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_roots")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)
    linked_plugin = tmp_path / "linked-plugin"
    linked_plugin.symlink_to(plugin_root, target_is_directory=True)

    with pytest.raises(ValueError, match="absolute canonical"):
        module.observe_filesystem(codex_home=Path("relative"), plugin_root=plugin_root)
    with pytest.raises(ValueError, match="absolute canonical"):
        module.observe_filesystem(codex_home="/tmp/\udcff", plugin_root=plugin_root)
    result = _observe(module, codex_home, linked_plugin)

    assert result.classification == "blocked"
    assert "plugin root" in result.reason


@_skip_windows_posix
def test_unowned_home_and_invalid_plugin_identity_return_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep root identity failures inside the fail-closed result contract."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_identity_failures")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)
    effective_user = module.os.geteuid()
    monkeypatch.setattr(module.os, "geteuid", lambda: effective_user + 1)

    unowned = _observe(module, codex_home, plugin_root)

    assert unowned.classification == "blocked"
    assert "unsafe directory" in unowned.reason

    monkeypatch.setattr(module.os, "geteuid", lambda: effective_user)
    original_identity = module._identity

    def _reject_plugin(
        path: Path,
        directory_fd: int,
        *,
        owned: bool = False,
        private: bool = False,
        protected: bool = False,
    ) -> object:
        """Reject identity reads for the plugin root while preserving other identities."""
        if path == plugin_root:
            raise module.ObservationError("invalid plugin identity")
        return original_identity(path, directory_fd, owned=owned, private=private, protected=protected)

    monkeypatch.setattr(module, "_identity", _reject_plugin)
    invalid_plugin = _observe(module, codex_home, plugin_root)

    assert invalid_plugin.classification == "blocked"
    assert invalid_plugin.codex_home_identity is not None
    assert "invalid plugin identity" in invalid_plugin.reason


@_skip_windows_posix
def test_group_writable_home_returns_blocked(tmp_path: Path) -> None:
    """Reject observation when another group member can replace home entries."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_writable_home")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)
    codex_home.chmod(0o770)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.codex_home_identity is None
    assert "unsafe protected directory mode" in result.reason
    assert "observed 0770" in result.reason


@_skip_windows_posix
def test_unsafe_state_path_closes_an_already_open_target_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent repeated unsafe-state observations from exhausting descriptors."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_descriptor_cleanup")
    codex_home, plugin_root, _, state_root = _make_roots(tmp_path)
    state_root.rmdir()
    state_parent = codex_home / "codex-rig"
    state_parent.rmdir()
    state_parent.symlink_to(tmp_path, target_is_directory=True)
    captured: list[int] = []
    original = module._observe_relative_root

    def _capture_target(
        parent_fd: int,
        parent_path: Path,
        parts: tuple[str, ...],
        label: str,
        *,
        private: bool = True,
    ) -> tuple[int | None, object]:
        """Record the target descriptor so the test can verify it is closed on failure."""
        descriptor, observation = original(parent_fd, parent_path, parts, label, private=private)
        if parts == ("agents",) and descriptor is not None:
            captured.append(descriptor)
        return descriptor, observation

    monkeypatch.setattr(module, "_observe_relative_root", _capture_target)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert len(captured) == 1
    with pytest.raises(OSError):
        os.fstat(captured[0])


@_skip_windows_posix
def test_target_metadata_error_is_blocked_not_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convert a target permission/race failure into a fail-closed observation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_metadata_error")
    codex_home, plugin_root, target_root, _ = _make_roots(tmp_path)
    target_name = "codex-rig-challenger.toml"
    _write_private(target_root / target_name, _shim("challenger"))
    real_stat = module.os.stat

    def _guarded_stat(path: object, *args: object, **kwargs: object) -> object:
        """Inject a target metadata failure while delegating unrelated stat calls."""
        if path == target_name:
            raise PermissionError("fixture denial")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "stat", _guarded_stat)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "blocked"
    assert result.targets == "unsafe"


@_skip_windows_posix
def test_observer_calls_no_mutating_os_primitives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make accidental filesystem mutation fail immediately during observation."""
    module = _load_module(OBSERVER_PATH, "codex_rig_observe_mutators")
    codex_home, plugin_root, _, _ = _make_roots(tmp_path)

    def _forbidden(*args: object, **kwargs: object) -> None:
        """Fail immediately if observation invokes a filesystem mutator."""
        raise AssertionError(f"mutator called with {args!r} {kwargs!r}")

    for name in ("mkdir", "makedirs", "remove", "unlink", "rename", "replace", "link", "symlink", "write"):
        monkeypatch.setattr(module.os, name, _forbidden)

    result = _observe(module, codex_home, plugin_root)

    assert result.classification == "degraded"
