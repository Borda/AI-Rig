"""Acceptance checks for read-only shim filesystem observation."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import operator
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import _platform
import pytest
from _modules import load_module_registered
from _platform import (
    DIRECTORY_SYMLINKS_AVAILABLE,
    FILE_SYMLINKS_AVAILABLE,
    HARD_LINKS_AVAILABLE,
    POSIX_FILE_MODES_AVAILABLE,
    POSIX_OBSERVER_PRIMITIVES_AVAILABLE,
    mode_is_retainable,
)
from _shim_support import canonical_json, role_ids, stat_root_identity, write_private

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
JOURNAL_PATH = SCRIPTS / "_agent_shim_journal.py"
OBSERVER_PATH = SCRIPTS / "_agent_shim_observe.py"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
PACKAGE_HASH = "a" * 64
ROLE_HASH = "b" * 64
CHALLENGER_TARGET = "codex-rig-challenger.toml"
LOCK_NAME = ".codex-rig-shims.lock"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load one sibling script with its direct dependencies available."""
    if path != GENERATOR_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    if path == OBSERVER_PATH and "_agent_shim_lifecycle" not in sys.modules:
        _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    if path == OBSERVER_PATH and "_agent_shim_journal" not in sys.modules:
        _load_module(JOURNAL_PATH, "_agent_shim_journal")
    return load_module_registered(name, path)


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

_skip_windows_posix = pytest.mark.skipif(
    not (POSIX_OBSERVER_PRIMITIVES_AVAILABLE and POSIX_FILE_MODES_AVAILABLE),
    reason="host lacks observer descriptor primitives or exact POSIX file modes",
)
_requires_fifo = pytest.mark.skipif(FIFO_UNAVAILABLE, reason="FIFO creation is unavailable")
_requires_hard_links = pytest.mark.skipif(not HARD_LINKS_AVAILABLE, reason="filesystem cannot create hard links")
_requires_file_symlinks = pytest.mark.skipif(
    not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"
)
_requires_directory_symlinks = pytest.mark.skipif(
    not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks"
)


def _unless_mode_retained(mode: int) -> pytest.MarkDecorator:
    """Skip a case when the filesystem cannot retain ``mode``."""
    return pytest.mark.skipif(not mode_is_retainable(mode), reason=f"filesystem does not retain mode {mode:04o}")


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


def _preparing_journal_payload() -> bytes:
    """Build one valid initial transaction authority for observer recovery."""
    operations = [
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
        for role_id in role_ids()
    ]
    root = {"canonical_path": "/fixture", "device": 1, "inode": 2, "owner": 3, "group": 4, "mode": "0700"}
    return canonical_json(
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
    operation["target_name"] = CHALLENGER_TARGET
    operation["after_hash"] = hashlib.sha256(after_payload).hexdigest()
    operation["after_image"] = "after/challenger.toml"
    value["after_state"]["sha256"] = hashlib.sha256(state_payload).hexdigest()
    value["journal_state"] = "PREPARED"
    write_private(transaction / "journal.json", canonical_json(value))
    write_private(transaction / "state.after.json", state_payload)
    after = transaction / "after"
    after.mkdir(mode=0o700)
    after.chmod(0o700)
    write_private(after / "challenger.toml", after_payload)
    return value


@dataclasses.dataclass(frozen=True)
class Roots:
    """Disposable roots used only by the test harness."""

    codex_home: Path
    plugin_root: Path
    target_root: Path
    state_root: Path


@pytest.fixture
def observer() -> ModuleType:
    """Load a fresh copy of the read-only observer for one test."""
    return _load_module(OBSERVER_PATH, "codex_rig_observe_under_test")


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    """Create empty codex-home, plugin, target, and private state roots under ``tmp_path``."""
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "plugin"
    target_root = codex_home / "agents"
    state_root = codex_home / "codex-rig" / "shims"
    target_root.mkdir(parents=True, mode=0o700)
    state_root.mkdir(parents=True, mode=0o700)
    state_root.chmod(0o700)
    plugin_root.mkdir(mode=0o700)
    return Roots(codex_home, plugin_root, target_root, state_root)


@pytest.fixture
def transaction(roots: Roots) -> Path:
    """Create the private transaction directory for the fixed install id."""
    directory = roots.state_root / "transactions" / INSTALL_ID
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    directory.parent.chmod(0o700)
    return directory


def _roster_hash(roles: list[dict[str, object]]) -> str:
    """Hash the persisted roster identity fields of ``roles``."""
    preimage = [{key: role[key] for key in ("role_id", "target_name", "card_path", "role_hash")} for role in roles]
    return hashlib.sha256(canonical_json(preimage)).hexdigest()


def _make_state(roots: Roots, *, status: str = "current") -> bytes:
    """Build state bound to the disposable roots and target bytes."""
    roles = [
        {
            "role_id": role_id,
            "target_name": f"codex-rig-{role_id}.toml",
            "card_path": f"roles/{role_id}/ROLE.md",
            "role_hash": ROLE_HASH,
            "file_hash": hashlib.sha256(_shim(role_id)).hexdigest(),
        }
        for role_id in role_ids()
    ]
    return canonical_json(
        {
            "schema": 1,
            "plugin": "codex-rig",
            "scope": "user",
            "install_id": INSTALL_ID,
            "plugin_version": "0.2.0",
            "package_hash": PACKAGE_HASH,
            "codex_home_identity": stat_root_identity(roots.codex_home),
            "plugin_root_identity": stat_root_identity(roots.plugin_root),
            "state_root_identity": stat_root_identity(roots.state_root),
            "target_root_identity": stat_root_identity(roots.target_root),
            "roster_hash": _roster_hash(roles),
            "bootstrap": {"protocol": 1, "helper_path": "scripts/verify_role_link.py", "helper_hash": "c" * 64},
            "generator_version": 1,
            "roles": roles,
            "transaction_status": status,
        }
    )


@pytest.fixture
def current_install(roots: Roots) -> Roots:
    """Write every roster target and a matching current state under ``roots``."""
    for role_id in role_ids():
        write_private(roots.target_root / f"codex-rig-{role_id}.toml", _shim(role_id))
    write_private(roots.state_root / "state.json", _make_state(roots))
    return roots


def _make_bare_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Create only private codex-home and plugin roots, leaving target and state roots absent."""
    codex_home = tmp_path / "codex-home"
    plugin_root = tmp_path / "plugin"
    codex_home.mkdir(mode=0o700)
    plugin_root.mkdir(mode=0o700)
    return codex_home, plugin_root


def _make_private_dirs(state_root: Path, *relatives: str) -> None:
    """Create private directories (and their parents) below ``state_root``."""
    for relative in relatives:
        residue = state_root / relative
        residue.mkdir(parents=True, mode=0o700)
        residue.chmod(0o700)
        residue.parent.chmod(0o700)


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


def _observe(observer: ModuleType, codex_home: Path, plugin_root: Path) -> object:
    """Observe the disposable roots through the public read-only entry."""
    return observer.observe_filesystem(codex_home=codex_home, plugin_root=plugin_root)


def _observe_roots(observer: ModuleType, roots: Roots) -> object:
    """Observe the standard disposable ``roots``."""
    return _observe(observer, roots.codex_home, roots.plugin_root)


def _make_symlink(path: Path, scratch: Path) -> None:
    """Make ``path`` a symlink to a sentinel file outside the observed roots."""
    outside = scratch / "outside"
    outside.write_bytes(b"sentinel")
    path.symlink_to(outside)


def _make_directory(path: Path, scratch: Path) -> None:
    """Make ``path`` a private directory."""
    path.mkdir(mode=0o700)


def _make_fifo(path: Path, scratch: Path) -> None:
    """Make ``path`` a private FIFO."""
    os.mkfifo(path, mode=0o600)


def _make_nonempty_file(path: Path, scratch: Path) -> None:
    """Make ``path`` a private regular file with unexpected content."""
    write_private(path, b"unexpected")


def _make_open_mode_file(path: Path, scratch: Path) -> None:
    """Make ``path`` an empty regular file with a world-readable mode."""
    path.write_bytes(b"")
    path.chmod(0o644)


#: Non-regular nodes an attacker could plant where a regular file is expected.
_HOSTILE_NODES = [
    pytest.param(_make_symlink, id="symlink", marks=_requires_file_symlinks),
    pytest.param(_make_directory, id="directory"),
    pytest.param(_make_fifo, id="fifo", marks=_requires_fifo),
]


def _swap_to_fifo_on_first_open(monkeypatch: pytest.MonkeyPatch, observer: ModuleType, victim: Path) -> list[bool]:
    """Replace ``victim`` with a FIFO at the observer's first nonblocking open of it; return the swap log."""
    original_open = observer.os.open
    swaps: list[bool] = []

    def _racing_open(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        """Swap the observed node for a FIFO between its stat and its nonblocking open."""
        if path == victim.name and not swaps and flags & os.O_NONBLOCK:
            swaps.append(True)
            victim.unlink()
            os.mkfifo(victim, mode=0o600)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(observer.os, "open", _racing_open)
    return swaps


def _forbid_mutators(monkeypatch: pytest.MonkeyPatch, observer: ModuleType) -> None:
    """Make every filesystem-mutating ``os`` primitive fail the test if the observer calls it."""

    def _forbidden(*args: object, **kwargs: object) -> None:
        """Fail immediately if observation invokes a filesystem mutator."""
        raise AssertionError(f"mutator called with {args!r} {kwargs!r}")

    for name in ("mkdir", "makedirs", "remove", "unlink", "rename", "replace", "link", "symlink", "write"):
        monkeypatch.setattr(observer.os, name, _forbidden)


def _tamper_artifact_bytes(transaction: Path, value: dict[str, object]) -> None:
    """Substitute the staged shim bytes."""
    write_private(transaction / "after" / "challenger.toml", b"substituted")


def _tamper_state_mode(transaction: Path, value: dict[str, object]) -> None:
    """Loosen the staged state file mode."""
    (transaction / "state.after.json").chmod(0o644)


def _add_extra_artifact(transaction: Path, value: dict[str, object]) -> None:
    """Add a staged artifact the journal does not list."""
    write_private(transaction / "after" / "other.toml", b"foreign")


def _swap_transaction_id(transaction: Path, value: dict[str, object]) -> None:
    """Rewrite the journal under a different transaction id."""
    value["transaction_id"] = "123e4567-e89b-42d3-a456-426614174001"
    value["transaction_nonce"] = value["transaction_id"]
    write_private(transaction / "journal.json", canonical_json(value))


def _write_illegal_successor(transaction: Path, value: dict[str, object]) -> None:
    """Write a next-journal whose package hash differs from the current journal."""
    value["package_hash"] = "d" * 64
    write_private(transaction / "journal.next.json", canonical_json(value))


def _stage_nothing(transaction: Path) -> None:
    """Leave the transaction directory as only the single-link journal."""


def _stage_unreadable_partial_artifact(transaction: Path) -> None:
    """Add a half-written, unreadable staged artifact."""
    after = transaction / "after"
    after.mkdir(mode=0o700)
    after.chmod(0o700)
    artifact = after / "challenger.toml"
    artifact.write_bytes(b"partial")
    artifact.chmod(0o000)


def _link_state_publish(transaction: Path) -> None:
    """Publish state as a hard link to the staged after-state inode."""
    os.link(transaction / "state.after.json", transaction / "state.publish.json")


def _copy_state_publish(transaction: Path) -> None:
    """Publish state as an independent file with identical bytes."""
    write_private(transaction / "state.publish.json", (transaction / "state.after.json").read_bytes())


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_symlink_probe_reports_unavailable_for_an_occupied_target(tmp_path: Path, target_is_directory: bool) -> None:
    """A failed file or directory link creation never enables either capability marker.

    The probe target already exists as a regular file, so link creation must fail and the probe must report that
    symlinks are unavailable rather than leaving a stale positive answer.
    """
    (tmp_path / "target").write_bytes(b"occupied\n")

    assert not _platform._symlinks_available(target_is_directory=target_is_directory, root=tmp_path)


@_skip_windows_posix
class TestRootAndLockObservation:
    """Observation of codex-home, target, and state roots and the coordination lock."""

    def test_absent_roster_is_bounded_degraded_and_zero_write(
        self, observer: ModuleType, roots: Roots, tmp_path: Path
    ) -> None:
        """Empty disposable roots remain byte-for-byte and metadata unchanged after observation.

        Roots exist but hold no state or targets: the verdict is ``degraded`` with an absent roster of 15 observations,
        and the observation itself must not touch the tree.
        """
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "degraded"
        assert result.state == "absent"
        assert result.targets == "absent"
        assert result.recovery == "none"
        assert len(result.target_observations) == 15
        assert _snapshot(tmp_path) == before

    def test_absent_roots_bind_nearest_ancestor_lock_intent_and_suffix(
        self, observer: ModuleType, tmp_path: Path
    ) -> None:
        """Missing target and state roots are described by their nearest existing ancestor and missing suffix.

        Only codex-home and the plugin root exist, so later creation can be bound to exact evidence without probing by
        mutation; the lock is reported as create-if-absent.
        """
        codex_home, plugin_root = _make_bare_roots(tmp_path)
        before = _snapshot(tmp_path)

        result = _observe(observer, codex_home, plugin_root)

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

    def test_existing_empty_lock_and_partial_state_root_are_bound_read_only(
        self, observer: ModuleType, tmp_path: Path
    ) -> None:
        """An existing empty lock and the deepest existing state ancestor are bound without changing either.

        ``codex-rig`` exists but ``shims`` does not, and the lock file is present and empty, so the lock intent must be
        open-existing and the missing suffix only ``shims``.
        """
        codex_home, plugin_root = _make_bare_roots(tmp_path)
        state_parent = codex_home / "codex-rig"
        state_parent.mkdir(mode=0o700)
        state_parent.chmod(0o700)
        write_private(codex_home / LOCK_NAME, b"")
        before = _snapshot(tmp_path)

        result = _observe(observer, codex_home, plugin_root)

        assert result.classification == "degraded"
        assert result.coordination_lock_observation.kind == "regular"
        assert result.coordination_lock_observation.intent == "open-existing"
        assert result.coordination_lock_observation.size == 0
        assert result.state_root_observation.nearest_existing_ancestor.canonical_path == str(state_parent)
        assert result.state_root_observation.missing_suffix_components == ("shims",)
        assert _snapshot(tmp_path) == before

    @pytest.mark.parametrize(
        "make_lock",
        [
            *_HOSTILE_NODES,
            pytest.param(_make_nonempty_file, id="nonempty"),
            pytest.param(_make_open_mode_file, id="mode"),
        ],
    )
    def test_unsafe_coordination_lock_blocks(
        self, observer: ModuleType, roots: Roots, tmp_path: Path, make_lock: object
    ) -> None:
        """A lock that cannot be safely opened as the fixed owned file blocks observation.

        Symlink, directory, FIFO, non-empty content, and a world-readable mode each make the lock unusable as the single
        empty private coordination file.
        """
        make_lock(roots.codex_home / LOCK_NAME, tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.coordination_lock_observation.kind == "unsafe"
        assert result.coordination_lock_observation.intent is None

    def test_group_writable_codex_home_blocks_lifecycle_authority(self, observer: ModuleType, roots: Roots) -> None:
        """A codex-home another group member could modify is rejected before any identity is trusted.

        Mode 0770 lets a second account substitute entries during mutation, so the observer returns ``blocked`` with no
        home observation or identity and names the observed mode.
        """
        roots.codex_home.chmod(0o770)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.codex_home_observation is None
        assert result.codex_home_identity is None
        assert "unsafe protected directory mode" in result.reason
        assert "observed 0770" in result.reason

    def test_owned_protected_target_root_mode_0755_is_accepted(
        self, observer: ModuleType, roots: Roots, tmp_path: Path
    ) -> None:
        """A user-owned target namespace that other users cannot mutate is accepted.

        Mode 0755 is world-readable but not group/world-writable, so the observation stays ``degraded`` with the usual
        unverified-prerequisites reason and the tree unchanged.
        """
        roots.target_root.chmod(0o755)
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "degraded"
        assert result.reason == "runtime and mutation prerequisites remain unverified"
        assert result.target_root_observation.identity.mode == "0755"
        assert result.targets == "absent"
        assert result.namespace_inventory_status == "complete"
        assert _snapshot(tmp_path) == before

    @pytest.mark.parametrize(
        ("root_name", "mode", "reason"),
        [
            pytest.param(
                "target_root",
                0o775,
                "unsafe protected directory mode: {path}; expected no group/world write or special bits, observed 0775",
                id="target-root-group-writable",
            ),
            pytest.param(
                "state_root",
                0o755,
                "unsafe private directory mode: {path}; expected 0700, observed 0755",
                id="state-root-not-private",
            ),
            pytest.param(
                "target_root",
                0o4700,
                "unsafe protected directory mode: {path}; expected no group/world write or special bits, observed 4700",
                id="target-root-setuid",
                marks=_unless_mode_retained(0o4700),
            ),
            pytest.param(
                "target_root",
                0o2700,
                "unsafe protected directory mode: {path}; expected no group/world write or special bits, observed 2700",
                id="target-root-setgid",
                marks=_unless_mode_retained(0o2700),
            ),
            pytest.param(
                "target_root",
                0o1700,
                "unsafe protected directory mode: {path}; expected no group/world write or special bits, observed 1700",
                id="target-root-sticky",
                marks=_unless_mode_retained(0o1700),
            ),
        ],
    )
    def test_unsafe_root_mode_blocks_with_exact_reason(
        self, observer: ModuleType, roots: Roots, root_name: str, mode: int, reason: str
    ) -> None:
        """A lifecycle root with an unsafe mode blocks observation and names the root and mode.

        The target root must not be group/world writable or carry setuid, setgid, or sticky bits; the state root must be
        exactly private (0700). The reason text is part of the contract because operators act on it.
        """
        root = getattr(roots, root_name)
        root.chmod(mode)
        assert stat.S_IMODE(root.stat().st_mode) == mode

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.reason == reason.format(path=root)


@_skip_windows_posix
class TestTargetObservation:
    """Observation of managed target files and the state file."""

    def test_exact_current_roster_is_observed_without_claiming_health(
        self, observer: ModuleType, current_install: Roots, tmp_path: Path
    ) -> None:
        """Exact state, root identities, markers, and complete target hashes are bound read-only.

        Every target and a matching state exist, so state and targets are ``current``; the verdict stays ``degraded``
        because runtime health is not claimed, and the tree is unchanged.
        """
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, current_install)

        assert result.classification == "degraded"
        assert result.state == "current"
        assert result.targets == "current"
        assert result.recovery == "none"
        assert result.codex_home_identity.canonical_path == str(current_install.codex_home)
        assert result.plugin_root_identity.canonical_path == str(current_install.plugin_root)
        assert result.target_root_observation.exists is True
        assert result.state_root_observation.exists is True
        assert result.state_payload == (current_install.state_root / "state.json").read_bytes()
        assert _snapshot(tmp_path) == before

    def test_historical_roster_and_old_cache_identity_are_migration_evidence(
        self, observer: ModuleType, roots: Roots, tmp_path: Path
    ) -> None:
        """Persisted retired targets are authenticated without requiring the old cache to exist.

        State written by an older release lists ``retired-specialist`` instead of ``web-explorer`` and points at a
        removed cache path; the observer must still report ``current`` and show the new role as absent.
        """
        state = json.loads(_make_state(roots))
        state["plugin_version"] = "0.1.0"
        state["plugin_root_identity"]["canonical_path"] = str(tmp_path / "removed-cache")
        retired_payload = _shim("retired-specialist")
        state["roles"] = [role for role in state["roles"] if role["role_id"] != "web-explorer"]
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
        state["roster_hash"] = _roster_hash(state["roles"])
        for role in state["roles"]:
            payload = retired_payload if role["role_id"] == "retired-specialist" else _shim(role["role_id"])
            write_private(roots.target_root / role["target_name"], payload)
        write_private(roots.state_root / "state.json", canonical_json(state))

        result = _observe_roots(observer, roots)

        assert result.classification == "degraded"
        assert result.state == "current"
        assert result.targets == "current"
        assert result.namespace_inventory_status == "complete"
        assert result.namespace_candidates == ()
        assert len(result.target_observations) == 16
        assert dict(result.target_observations)["codex-rig-web-explorer.toml"].kind == "absent"

    @pytest.mark.parametrize("make_node", _HOSTILE_NODES)
    def test_hostile_target_nodes_fail_closed(
        self, observer: ModuleType, roots: Roots, tmp_path: Path, make_node: object
    ) -> None:
        """No-follow target observation refuses aliased and non-regular nodes without touching them.

        A symlink, directory, or FIFO planted at a managed target name must make the roster ``unsafe`` and the whole
        observation ``blocked``, leaving the tree byte-for-byte unchanged.
        """
        make_node(roots.target_root / CHALLENGER_TARGET, tmp_path)
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.targets == "unsafe"
        assert _snapshot(tmp_path) == before

    def test_foreign_and_marker_only_targets_never_become_owned(self, observer: ModuleType, roots: Roots) -> None:
        """A valid marker without valid state does not establish ownership.

        A single marker-bearing target exists but no state file, so the roster is ``foreign`` and observation is
        ``blocked``; namespace and marker evidence alone never grant authority.
        """
        write_private(roots.target_root / CHALLENGER_TARGET, _marker("challenger") + b"\n")

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.targets == "foreign"

    @pytest.mark.parametrize(
        ("root_name", "name", "payload", "mode"),
        [
            pytest.param("target_root", CHALLENGER_TARGET, _shim("challenger"), 0o644, id="target-file-mode"),
            pytest.param("state_root", "control\nname", b"evidence", 0o600, id="control-character-name"),
        ],
    )
    def test_nonprivate_file_or_ambiguous_name_blocks(
        self, observer: ModuleType, roots: Roots, root_name: str, name: str, payload: bytes, mode: int
    ) -> None:
        """A non-private target file or a control character in a state-root name blocks observation.

        Lifecycle files must be private and every name unambiguous, otherwise a log line or path could be spoofed.
        """
        path = getattr(roots, root_name) / name
        path.write_bytes(payload)
        path.chmod(mode)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"

    @pytest.mark.parametrize(
        ("root_name", "name", "payload_for"),
        [
            pytest.param("state_root", "state.json", lambda observer: b'{"schema":1,"schema":2}', id="corrupt-state"),
            pytest.param(
                "state_root", "state.json", lambda observer: b'{"schema":' + b"9" * 5000 + b"}", id="huge-integer-state"
            ),
            pytest.param(
                "state_root",
                "state.json",
                lambda observer: b'{"schema":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
                id="deep-state",
            ),
            pytest.param(
                "state_root", "state.json", lambda observer: b"x" * (observer.STATE_BYTES + 1), id="oversized-state"
            ),
            pytest.param(
                "target_root",
                CHALLENGER_TARGET,
                lambda observer: b"x" * (observer.SHIM_BYTES + 1),
                id="oversized-target",
            ),
        ],
    )
    def test_corrupt_or_oversized_evidence_blocks(
        self, observer: ModuleType, roots: Roots, root_name: str, name: str, payload_for: object
    ) -> None:
        """Corrupt, hostile, or oversized state and target bytes are rejected within bounded reads.

        Duplicate keys, a 5000-digit integer, 2000-level nesting, and files one byte over their size limit must produce
        a ``blocked`` verdict instead of an exception or unbounded read.
        """
        write_private(getattr(roots, root_name) / name, payload_for(observer))

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.state in {"absent", "corrupt", "unsafe"}
        assert result.targets in {"absent", "unsafe"}

    @_requires_file_symlinks
    def test_symlinked_state_file_blocks(self, observer: ModuleType, roots: Roots, tmp_path: Path) -> None:
        """A state file that is a symlink is rejected instead of followed.

        Following the link would read evidence from outside the private state root.
        """
        _make_symlink(roots.state_root / "state.json", tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.state in {"absent", "corrupt", "unsafe"}
        assert result.targets in {"absent", "unsafe"}

    def test_target_metadata_error_is_blocked_not_raised(
        self, observer: ModuleType, roots: Roots, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permission or race failure while reading target metadata becomes a fail-closed observation.

        ``os.stat`` is made to raise ``PermissionError`` for one target name only; the observer must report an
        ``unsafe`` roster rather than propagate the exception.
        """
        write_private(roots.target_root / CHALLENGER_TARGET, _shim("challenger"))
        real_stat = observer.os.stat

        def _guarded_stat(path: object, *args: object, **kwargs: object) -> object:
            """Inject a target metadata failure while delegating unrelated stat calls."""
            if path == CHALLENGER_TARGET:
                raise PermissionError("fixture denial")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(observer.os, "stat", _guarded_stat)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.targets == "unsafe"


@_skip_windows_posix
class TestOpenRaces:
    """Swaps of an observed node between its stat and its open."""

    @_requires_fifo
    @pytest.mark.parametrize(
        ("parent_name", "name", "payload", "observed", "expected"),
        [
            pytest.param(
                "codex_home",
                LOCK_NAME,
                b"",
                operator.attrgetter("coordination_lock_observation.kind"),
                "unsafe",
                id="coordination-lock",
            ),
            pytest.param(
                "target_root",
                CHALLENGER_TARGET,
                _shim("challenger"),
                operator.attrgetter("targets"),
                "unsafe",
                id="target",
            ),
        ],
    )
    def test_swap_to_fifo_is_nonblocking_and_fails_closed(
        self,
        observer: ModuleType,
        roots: Roots,
        monkeypatch: pytest.MonkeyPatch,
        parent_name: str,
        name: str,
        payload: bytes,
        observed: object,
        expected: str,
    ) -> None:
        """A regular file replaced by a FIFO at open time cannot hang read-only observation.

        The node is swapped for a FIFO at the observer's first nonblocking open; the observation must finish, report the
        node ``unsafe``, and block, instead of waiting on the FIFO forever.
        """
        victim = getattr(roots, parent_name) / name
        write_private(victim, payload)
        swaps = _swap_to_fifo_on_first_open(monkeypatch, observer, victim)

        result = _observe_roots(observer, roots)

        assert swaps == [True]
        assert result.classification == "blocked"
        assert observed(result) == expected


@_skip_windows_posix
class TestNamespaceInventory:
    """Inventory of ``codex-rig-*`` names in the target root that are not persisted roles."""

    def test_retired_namespace_file_is_inventoried_without_ownership(
        self, observer: ModuleType, roots: Roots, tmp_path: Path
    ) -> None:
        """A retired-looking regular file is listed while lifecycle authority is refused.

        The file matches the namespace but is not in any state, so the observation is ``blocked`` and the file is
        reported as a candidate; targets stay ``absent`` and the tree is untouched.
        """
        write_private(roots.target_root / "codex-rig-retired-role.toml", b"untrusted\n")
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.targets == "absent"
        assert result.namespace_inventory_status == "complete"
        assert tuple((item.name, item.kind) for item in result.namespace_candidates) == (
            ("codex-rig-retired-role.toml", "regular"),
        )
        assert _snapshot(tmp_path) == before

    def test_candidate_preserves_exact_current_role_observations(
        self, observer: ModuleType, current_install: Roots
    ) -> None:
        """An unmanaged candidate blocks without degrading the authenticated current roles.

        A complete current install plus one extra marker-bearing namespace file: every persisted role stays
        ``regular`` and ``current`` while the extra file is reported separately.
        """
        write_private(current_install.target_root / "codex-rig-retired-role.toml", _marker("challenger") + b"\n")

        result = _observe_roots(observer, current_install)

        assert result.classification == "blocked"
        assert result.targets == "current"
        assert all(observation.kind == "regular" for _, observation in result.target_observations)
        assert tuple((item.name, item.kind) for item in result.namespace_candidates) == (
            ("codex-rig-retired-role.toml", "regular"),
        )

    @pytest.mark.parametrize("make_node", _HOSTILE_NODES)
    def test_unsafe_candidate_remains_visible(
        self, observer: ModuleType, roots: Roots, tmp_path: Path, make_node: object
    ) -> None:
        """A non-regular namespace candidate is inventoried as unsafe without being followed or read.

        A symlink, directory, or FIFO with a retired-looking name must appear in the inventory with kind ``unsafe`` and
        leave the tree unchanged.
        """
        candidate = roots.target_root / "codex-rig-retired-role.toml"
        make_node(candidate, tmp_path)
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert tuple((item.name, item.kind) for item in result.namespace_candidates) == ((candidate.name, "unsafe"),)
        assert _snapshot(tmp_path) == before

    def test_malformed_candidate_fails_closed_and_remains_visible(self, observer: ModuleType, roots: Roots) -> None:
        """A namespace-like name that violates the strict role grammar is exposed as malformed.

        ``codex-rig-Retired.toml`` has an uppercase role id, so it cannot be a valid role; it must still block and be
        listed rather than ignored.
        """
        write_private(roots.target_root / "codex-rig-Retired.toml", b"untrusted\n")

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert tuple((item.name, item.kind) for item in result.namespace_candidates) == (
            ("codex-rig-Retired.toml", "malformed"),
        )

    def test_unrelated_names_are_ignored(self, observer: ModuleType, roots: Roots) -> None:
        """Unrelated agent-root entries stay outside Codex Rig lifecycle authority.

        Names that merely resemble the namespace (``.txt`` suffix, other prefix) must not be inventoried or block
        observation.
        """
        for name in ("codex-rig-retired.txt", "notes.toml", "other-codex-rig-role.toml"):
            write_private(roots.target_root / name, b"unrelated\n")

        result = _observe_roots(observer, roots)

        assert result.classification == "degraded"
        assert result.namespace_inventory_status == "complete"
        assert result.namespace_candidates == ()

    def test_candidates_are_sorted_independently_of_enumeration_order(self, observer: ModuleType, roots: Roots) -> None:
        """Namespace candidates are reported in name order, not filesystem enumeration order.

        ``zeta`` is created before ``alpha`` so a creation- or hash-ordered listing would put it first.
        """
        for name in ("codex-rig-zeta.toml", "codex-rig-alpha.toml"):
            write_private(roots.target_root / name, b"untrusted\n")

        result = _observe_roots(observer, roots)

        assert tuple(item.name for item in result.namespace_candidates) == (
            "codex-rig-alpha.toml",
            "codex-rig-zeta.toml",
        )

    def test_directory_overflow_is_bounded_and_visible(
        self, observer: ModuleType, roots: Roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumeration stops at its entry limit and reports overflow as a fail-closed condition.

        The limit is lowered to 8 and nine unrelated entries are created, so the inventory must report ``overflow``,
        mark targets ``unsafe``, and block without scanning further.
        """
        monkeypatch.setattr(observer, "MAX_TARGET_DIRECTORY_ENTRIES", 8)
        for index in range(9):
            write_private(roots.target_root / f"unrelated-{index:03d}", b"x")
        before = _snapshot(tmp_path)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.targets == "unsafe"
        assert result.namespace_inventory_status == "overflow"
        assert result.namespace_candidates == ()
        assert _snapshot(tmp_path) == before


@_skip_windows_posix
class TestRecoveryResidue:
    """Recognition of interrupted-transaction residue in the state root."""

    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            pytest.param(f"transactions/{INSTALL_ID}", "empty-transaction", id="empty-transaction-directory"),
            pytest.param(f".probe-{INSTALL_ID}", "empty-probe", id="empty-probe-directory"),
        ],
    )
    def test_recognizes_exact_empty_residue(
        self, observer: ModuleType, roots: Roots, relative: str, expected: str
    ) -> None:
        """Only private, empty, nonce-named recovery directories are recognised as residue.

        An empty transaction directory or probe directory left by a crash before any artifact was written is safe to
        identify for cleanup, and blocks further lifecycle work until cleaned.
        """
        _make_private_dirs(roots.state_root, relative)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == expected

    def test_recognizes_initial_preparation_residue_without_parsing_partial_bytes(
        self, observer: ModuleType, roots: Roots, transaction: Path
    ) -> None:
        """The sole pre-authority crash artifact is bound for explicit cleanup.

        ``journal.initial.json`` holds truncated bytes; the observer must classify it as ``preparing-residue`` without
        trying to parse the partial JSON.
        """
        write_private(transaction / "journal.initial.json", b'{"partial"')

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == "preparing-residue"

    @_requires_hard_links
    def test_recognizes_dual_link_initial_journal_crash(
        self, observer: ModuleType, roots: Roots, transaction: Path
    ) -> None:
        """The durable window before the initial journal link retires is recognised as a journal.

        ``journal.initial.json`` and ``journal.json`` are hard links of one valid preparing journal (link count 2),
        which is what a crash between link and unlink leaves behind.
        """
        write_private(transaction / "journal.initial.json", _preparing_journal_payload())
        os.link(transaction / "journal.initial.json", transaction / "journal.json")

        result = _observe_roots(observer, roots)

        assert (transaction / "journal.json").stat().st_nlink == 2
        assert result.classification == "blocked"
        assert result.recovery == "journal"

    @pytest.mark.parametrize(
        "stage",
        [
            pytest.param(_stage_nothing, id="journal-only"),
            pytest.param(_stage_unreadable_partial_artifact, id="partial-unreadable-artifact"),
        ],
    )
    def test_single_link_preparing_journal_is_cleanable(
        self, observer: ModuleType, roots: Roots, transaction: Path, stage: object
    ) -> None:
        """Every ordinary pre-mutation preparation crash window is recognised as a journal.

        A valid single-link PREPARING journal is present either alone or with a half-written, unreadable staged
        artifact; both are states cleanup can safely resume.
        """
        write_private(transaction / "journal.json", _preparing_journal_payload())
        stage(transaction)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == "journal"

    def test_recognizes_exact_prepared_transaction(self, observer: ModuleType, roots: Roots, transaction: Path) -> None:
        """Complete hash-bound artifacts after durable preparation are recognised as a journal.

        Journal, staged state, and staged shim all match their recorded hashes and modes.
        """
        _prepared_transaction(transaction)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == "journal"

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(_tamper_artifact_bytes, id="artifact-bytes"),
            pytest.param(_tamper_state_mode, id="artifact-mode"),
            pytest.param(_add_extra_artifact, id="extra-artifact"),
            pytest.param(_swap_transaction_id, id="transaction-id"),
            pytest.param(_write_illegal_successor, id="illegal-successor"),
        ],
    )
    def test_prepared_transaction_rejects_substituted_or_expanded_authority(
        self, observer: ModuleType, roots: Roots, transaction: Path, tamper: object
    ) -> None:
        """Recovery is refused when any durable authority or staged artifact is not exact.

        Starting from a valid PREPARED transaction, changed shim bytes, a loosened mode, an unlisted artifact, a
        different transaction id, or an illegal next journal each make the residue ``blocked-unknown``.
        """
        tamper(transaction, _prepared_transaction(transaction))

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == "blocked-unknown"

    def test_prepared_transaction_accepts_one_legal_journal_successor(
        self, observer: ModuleType, roots: Roots, transaction: Path
    ) -> None:
        """A crash-preserved next journal is accepted only for one legal state transition.

        PREPARED to MUTATING is a legal step, so a ``journal.next.json`` holding it is still recognised as a journal.
        """
        successor = _prepared_transaction(transaction)
        successor["journal_state"] = "MUTATING"
        write_private(transaction / "journal.next.json", canonical_json(successor))

        result = _observe_roots(observer, roots)

        assert result.recovery == "journal"

    @_requires_hard_links
    @pytest.mark.parametrize(
        ("publish", "expected"),
        [
            pytest.param(_link_state_publish, "journal", id="same-inode"),
            pytest.param(_copy_state_publish, "blocked-unknown", id="copied-bytes"),
        ],
    )
    def test_prepared_transaction_binds_state_publish_inode(
        self, observer: ModuleType, roots: Roots, transaction: Path, publish: object, expected: str
    ) -> None:
        """A staged state publication is accepted only when it links the after-state inode.

        A copy with identical bytes is not the staged file, so only the hard-linked publication is recognised.
        """
        _prepared_transaction(transaction)
        publish(transaction)

        result = _observe_roots(observer, roots)

        assert result.recovery == expected

    @_requires_hard_links
    @pytest.mark.parametrize("journal_state", ["MUTATING", "RECOVERY_REQUIRED"])
    def test_accepts_one_unjournaled_create_publication(
        self, observer: ModuleType, roots: Roots, transaction: Path, journal_state: str
    ) -> None:
        """A published exact target is recognised when recovery authority lags by one step.

        The staged shim is already hard-linked into the target root while the journal still reads MUTATING or
        RECOVERY_REQUIRED, which is the window after publication and before the journal update.
        """
        value = _prepared_transaction(transaction)
        value["journal_state"] = journal_state
        write_private(transaction / "journal.json", canonical_json(value))
        os.link(transaction / "after" / "challenger.toml", roots.target_root / CHALLENGER_TARGET)

        result = _observe_roots(observer, roots)

        assert result.recovery == "journal"

    @pytest.mark.parametrize(
        ("relatives", "expected"),
        [
            pytest.param(("unknown",), "blocked-unknown", id="unknown-directory"),
            pytest.param(
                (f"transactions/{INSTALL_ID}", ".probe-123e4567-e89b-42d3-a456-426614174001"),
                "blocked-multiple",
                id="transaction-and-probe",
            ),
        ],
    )
    def test_unknown_or_multiple_residue_fails_closed(
        self, observer: ModuleType, roots: Roots, relatives: tuple[str, ...], expected: str
    ) -> None:
        """Ambiguous recovery authority is refused without reading outside the state root.

        An unrecognised directory is ``blocked-unknown``; two competing recognised residues (a transaction and a probe)
        are ``blocked-multiple``.
        """
        _make_private_dirs(roots.state_root, *relatives)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == expected

    def test_nonempty_receipt_stays_untrusted_until_full_schema_validation(
        self, observer: ModuleType, roots: Roots, transaction: Path
    ) -> None:
        """A bounded receipt shape alone never grants recovery authority.

        ``journal.json`` holds ``{}``, which is small and valid JSON but fails the full journal schema, so it is
        ``blocked-unknown``.
        """
        write_private(transaction / "journal.json", b"{}")

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == "blocked-unknown"

    def test_nonprivate_transactions_container_blocks_even_when_empty(self, observer: ModuleType, roots: Roots) -> None:
        """The transaction container itself must carry private ownership metadata.

        An empty ``transactions`` directory with mode 0755 is already unsafe, so recovery is ``blocked-unknown``.
        """
        transactions = roots.state_root / "transactions"
        transactions.mkdir(mode=0o755)
        transactions.chmod(0o755)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.recovery == "blocked-unknown"


@_skip_windows_posix
class TestSuppliedRoots:
    """Validation of the roots handed to the observer and descriptor hygiene."""

    @pytest.mark.parametrize(
        "codex_home",
        [
            pytest.param(Path("relative"), id="relative-path"),
            pytest.param("/tmp/\udcff", id="undecodable-string"),
        ],
    )
    def test_rejects_non_canonical_supplied_roots(self, observer: ModuleType, roots: Roots, codex_home: object) -> None:
        """Explicit absolute canonical roots are required before any supplied path is opened.

        A relative path and a string with an undecodable surrogate are both refused with ``ValueError``.
        """
        with pytest.raises(ValueError, match="absolute canonical"):
            observer.observe_filesystem(codex_home=codex_home, plugin_root=roots.plugin_root)

    @_requires_directory_symlinks
    def test_rejects_symlinked_supplied_roots(self, observer: ModuleType, roots: Roots, tmp_path: Path) -> None:
        """A supplied root whose path traversal meets a symlink is rejected.

        The plugin root is given through a symlink to the real directory; the observer must block and name the plugin
        root instead of following the alias.
        """
        linked_plugin = tmp_path / "linked-plugin"
        linked_plugin.symlink_to(roots.plugin_root, target_is_directory=True)

        result = _observe(observer, roots.codex_home, linked_plugin)

        assert result.classification == "blocked"
        assert "plugin root" in result.reason

    def test_unowned_home_is_blocked(self, observer: ModuleType, roots: Roots, monkeypatch: pytest.MonkeyPatch) -> None:
        """A codex-home owned by another user returns a blocked result instead of raising.

        The effective uid is shifted by one so the real directory looks foreign.
        """
        effective_user = observer.os.geteuid()
        monkeypatch.setattr(observer.os, "geteuid", lambda: effective_user + 1)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert "unsafe directory" in result.reason

    def test_invalid_plugin_identity_is_blocked(
        self, observer: ModuleType, roots: Roots, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing plugin-root identity read returns a blocked result that keeps the home identity.

        Identity reads are delegated to the real implementation except for the plugin root, which raises
        ``ObservationError``.
        """
        original_identity = observer._identity

        def _reject_plugin(
            path: Path,
            directory_fd: int,
            *,
            owned: bool = False,
            private: bool = False,
            protected: bool = False,
        ) -> object:
            """Reject identity reads for the plugin root while preserving other identities."""
            if path == roots.plugin_root:
                raise observer.ObservationError("invalid plugin identity")
            return original_identity(path, directory_fd, owned=owned, private=private, protected=protected)

        monkeypatch.setattr(observer, "_identity", _reject_plugin)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert result.codex_home_identity is not None
        assert "invalid plugin identity" in result.reason

    @_requires_directory_symlinks
    def test_unsafe_state_path_closes_an_already_open_target_descriptor(
        self, observer: ModuleType, roots: Roots, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated unsafe-state observations do not leak the already-open target-root descriptor.

        ``codex-rig`` is replaced by a symlink so the state path is unsafe after the target root was opened; the
        recorded target descriptor must be closed (``fstat`` fails with EBADF) once observation returns.
        """
        roots.state_root.rmdir()
        state_parent = roots.codex_home / "codex-rig"
        state_parent.rmdir()
        state_parent.symlink_to(tmp_path, target_is_directory=True)
        captured: list[int] = []
        original = observer._observe_relative_root

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

        monkeypatch.setattr(observer, "_observe_relative_root", _capture_target)

        result = _observe_roots(observer, roots)

        assert result.classification == "blocked"
        assert len(captured) == 1
        with pytest.raises(OSError, match=r"\[Errno 9\]"):
            os.fstat(captured[0])

    def test_calls_no_mutating_os_primitives(
        self, observer: ModuleType, roots: Roots, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observation never invokes a filesystem-mutating ``os`` primitive.

        Each mutator (``mkdir``, ``unlink``, ``rename``, ``link``, ``write``, ...) is replaced by a function that fails
        the test, so an accidental write fails immediately instead of silently changing the tree.
        """
        _forbid_mutators(monkeypatch, observer)

        result = _observe_roots(observer, roots)

        assert result.classification == "degraded"
