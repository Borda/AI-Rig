"""Acceptance checks for transactional shim application and rollback.

A prepared transaction (create, update, or remove of one role shim plus manager state) is applied through descriptor
handles. Failures are injected at each named forward boundary, and one scenario kills a real child process, to prove the
journal reaches a terminal state and the exact before-image is recoverable without erasing foreign or untrusted bytes.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile  # noqa: F401
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered
from _shim_support import canonical_json, sha256_hex, stat_root_identity, write_private

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
JOURNAL_PATH = SCRIPTS / "_agent_shim_journal.py"
POSIX_PATH = SCRIPTS / "_agent_shim_posix.py"
TRANSACTION_PATH = SCRIPTS / "_agent_shim_transaction.py"
TRANSACTION_ID = "123e4567-e89b-42d3-a456-426614174000"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174001"
DIGEST = "a" * 64
GENERATED_SHIM = b"generated challenger shim\n"
PREVIOUS_SHIM = b"previous challenger shim\n"
REPLACEMENT_SHIM = b"replacement challenger shim\n"
PREVIOUS_STATE = b'{"transaction_status":"previous"}'
CURRENT_STATE = b'{"transaction_status":"current"}'
TARGET_NAME = "codex-rig-challenger.toml"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load the transaction kernel with its sibling modules available."""
    if path == LIFECYCLE_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    if path == JOURNAL_PATH and "_agent_shim_lifecycle" not in sys.modules:
        _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    if path == TRANSACTION_PATH:
        dependencies = (
            (JOURNAL_PATH, "_agent_shim_journal"),
            (POSIX_PATH, "_agent_shim_posix"),
        )
        for dependency, module_name in dependencies:
            if module_name not in sys.modules:
                _load_module(dependency, module_name)
    return load_module_registered(name, path)


def _read_or_none(path: Path) -> bytes | None:
    """Return a file's bytes, or None when it does not exist."""
    return path.read_bytes() if path.exists() else None


def _create_journal(roots: dict[str, Path], after_payload: bytes, state_payload: bytes) -> dict[str, object]:
    """Build one valid PREPARED single-role create journal."""
    return {
        "schema": 1,
        "transaction_id": TRANSACTION_ID,
        "transaction_nonce": TRANSACTION_ID,
        "install_id": INSTALL_ID,
        "action": "install",
        "approved_plan_digest": DIGEST,
        "package_hash": DIGEST,
        "roster_hash": DIGEST,
        "codex_home_identity": stat_root_identity(roots["home"]),
        "target_root_identity": stat_root_identity(roots["target"]),
        "state_root_identity": stat_root_identity(roots["state"]),
        "before_state": {"exists": False, "relative_path": None, "sha256": None, "mode": None},
        "after_state": {
            "exists": True,
            "relative_path": "state.after.json",
            "sha256": sha256_hex(state_payload),
            "mode": "0600",
        },
        "rollback_state_progress": "PENDING",
        "journal_state": "PREPARED",
        "operations": [
            {
                "role_id": "challenger",
                "intent": "create",
                "target_name": TARGET_NAME,
                "before_exists": False,
                "before_hash": None,
                "before_mode": None,
                "after_exists": True,
                "after_hash": sha256_hex(after_payload),
                "after_mode": "0600",
                "before_image": None,
                "after_image": "after/challenger.toml",
                "quarantine_name": None,
                "progress": "PLANNED",
                "rollback_progress": "NOT_STARTED",
            }
        ],
    }


def _owned_journal_value(roots: dict[str, Path], after_payload: bytes) -> dict[str, object]:
    """Build a journal for a role already owned on disk, with before-images and quarantine name set."""
    value = _create_journal(roots, after_payload, CURRENT_STATE)
    value["operations"][0].update(
        {
            "before_exists": True,
            "before_hash": sha256_hex(PREVIOUS_SHIM),
            "before_mode": "0600",
            "before_image": "before/challenger.toml",
            "quarantine_name": "quarantine/challenger.toml",
        }
    )
    value["before_state"] = {
        "exists": True,
        "relative_path": "state.before.json",
        "sha256": sha256_hex(PREVIOUS_STATE),
        "mode": "0600",
    }
    return value


def _write_owned_files(roots: dict[str, Path], value: dict[str, object]) -> None:
    """Write the on-disk previous target, before-images, state files and journal for an owned transaction."""
    write_private(roots["target"] / TARGET_NAME, PREVIOUS_SHIM)
    write_private(roots["before"] / "challenger.toml", PREVIOUS_SHIM)
    write_private(roots["state"] / "state.json", PREVIOUS_STATE)
    write_private(roots["transaction"] / "state.before.json", PREVIOUS_STATE)
    write_private(roots["transaction"] / "state.after.json", CURRENT_STATE)
    write_private(roots["transaction"] / "journal.json", canonical_json(value))


@dataclass(frozen=True)
class Prepared:
    """One prepared create transaction with its modules, held descriptors and directory layout."""

    module: ModuleType
    journal_module: ModuleType
    journal: object
    handles: object
    roots: dict[str, Path]


def _prepare_create(prepared: Prepared) -> object:
    """Use the fixture's create journal unchanged."""
    return prepared.journal


def _prepare_update(prepared: Prepared) -> object:
    """Replace the create fixture with an owned update to the replacement shim and return its journal."""
    roots = prepared.roots
    value = _owned_journal_value(roots, REPLACEMENT_SHIM)
    value["operations"][0]["intent"] = "update"
    write_private(roots["after"] / "challenger.toml", REPLACEMENT_SHIM)
    _write_owned_files(roots, value)
    return prepared.journal_module.validate_journal(value)


def _prepare_remove(prepared: Prepared) -> object:
    """Replace the create fixture with an owned removal (no after-image) and return its journal."""
    roots = prepared.roots
    value = _owned_journal_value(roots, b"unused")
    value["action"] = "remove"
    value["operations"][0].update(
        {"intent": "remove", "after_exists": False, "after_hash": None, "after_mode": None, "after_image": None}
    )
    (roots["after"] / "challenger.toml").unlink()
    _write_owned_files(roots, value)
    return prepared.journal_module.validate_journal(value)


#: Per intent: how to prepare the journal, the target bytes expected after rollback, and the state bytes expected.
_INTENTS = {
    "create": (_prepare_create, None, None),
    "update": (_prepare_update, PREVIOUS_SHIM, PREVIOUS_STATE),
    "remove": (_prepare_remove, PREVIOUS_SHIM, PREVIOUS_STATE),
}
_FORWARD_BOUNDARIES = {
    "create": ("challenger:published", "challenger:verified", "state:published"),
    "update": ("challenger:detached", "challenger:published", "challenger:verified", "state:published"),
    "remove": ("challenger:detached", "challenger:verified", "state:published"),
}
_BOUNDARY_CASES = [
    pytest.param(intent, boundary, id=f"{intent}-{boundary.replace(':', '-')}")
    for intent, boundaries in _FORWARD_BOUNDARIES.items()
    for boundary in boundaries
]
_OWNED_PREPARERS = [pytest.param(_prepare_update, id="update"), pytest.param(_prepare_remove, id="remove")]
_OWNED_CASES = [
    pytest.param(_prepare_update, REPLACEMENT_SHIM, id="update"),
    pytest.param(_prepare_remove, None, id="remove"),
]


@pytest.fixture(name="prepared")
def _prepared(tmp_path: Path) -> Iterator[Prepared]:
    """Create one prepared create transaction with held exact directory descriptors."""
    module = _load_module(TRANSACTION_PATH, "codex_rig_transaction_fixture")
    roots = {
        "home": tmp_path / "home",
        "target": tmp_path / "home" / "agents",
        "state": tmp_path / "home" / "state",
        "transaction": tmp_path / "home" / "state" / "transactions" / TRANSACTION_ID,
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    roots["target"].chmod(0o755)
    for child in ("before", "after", "quarantine"):
        path = roots["transaction"] / child
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        roots[child] = path
    journal_value = _create_journal(roots, GENERATED_SHIM, CURRENT_STATE)
    write_private(roots["after"] / "challenger.toml", GENERATED_SHIM)
    write_private(roots["transaction"] / "state.after.json", CURRENT_STATE)
    write_private(roots["transaction"] / "journal.json", canonical_json(journal_value))
    journal_module = sys.modules["_agent_shim_journal"]
    names = ("target", "state", "transaction", "before", "after", "quarantine")
    fds = {name: os.open(roots[name], os.O_RDONLY | os.O_DIRECTORY) for name in names}
    handles = module.TransactionDirectories(
        fds["transaction"], fds["target"], fds["state"], fds["before"], fds["after"], fds["quarantine"]
    )
    yield Prepared(module, journal_module, journal_module.validate_journal(journal_value), handles, roots)
    for descriptor in fds.values():
        os.close(descriptor)


def _fail_at(boundary: str, error: str = "injected boundary failure") -> Callable[[str], None]:
    """Return a checkpoint callback that raises once the named boundary completes."""

    def _checkpoint(name: str) -> None:
        """Raise when the transaction reports the selected boundary."""
        if name == boundary:
            raise RuntimeError(error)

    return _checkpoint


def _journal_after_failure(prepared: Prepared, journal: object, checkpoint: Callable[[str], None]) -> object:
    """Apply a transaction expected to fail and return the terminal journal carried by the error."""
    with pytest.raises(prepared.module.TransactionError) as captured:
        prepared.module.apply_transaction(journal, prepared.handles, checkpoint=checkpoint)
    return captured.value.journal


def _assert_before_image(roots: dict[str, Path], expected_target: bytes | None, expected_state: bytes | None) -> None:
    """Assert target and state hold the expected before-image and the shared target keeps mode 0755."""
    assert _read_or_none(roots["target"] / TARGET_NAME) == expected_target
    assert _read_or_none(roots["state"] / "state.json") == expected_state
    assert stat.S_IMODE(roots["target"].stat().st_mode) == 0o755


class TestApplyCreate:
    """Applying and cleaning up a fresh create transaction."""

    def test_commits_exact_target_and_state(self, prepared: Prepared):
        """Publish the exact generated shim and state and reach COMMITTED.

        Staged artifacts stay hardlinked to their published names (link count 2) until cleanup retires them.
        """
        committed = prepared.module.apply_transaction(prepared.journal, prepared.handles)

        roots = prepared.roots
        assert committed.journal_state == "COMMITTED"
        assert (roots["target"] / TARGET_NAME).read_bytes() == GENERATED_SHIM
        assert (roots["state"] / "state.json").read_bytes() == CURRENT_STATE
        assert (roots["after"] / "challenger.toml").stat().st_nlink == 2

    def test_cleanup_removes_only_terminal_residue(self, prepared: Prepared):
        """Remove every staged transaction artifact after commit and leave single-link published files."""
        committed = prepared.module.apply_transaction(prepared.journal, prepared.handles)

        removed = prepared.module.cleanup_transaction(committed, prepared.handles)

        roots = prepared.roots
        assert "journal.json" in removed
        assert list(roots["transaction"].iterdir()) == []
        assert (roots["target"] / TARGET_NAME).stat().st_nlink == 1
        assert (roots["state"] / "state.json").stat().st_nlink == 1

    def test_foreign_target_is_preserved_and_requires_recovery(self, prepared: Prepared):
        """Preserve a target that appears after approval and mark the journal RECOVERY_REQUIRED."""
        foreign = prepared.roots["target"] / TARGET_NAME
        write_private(foreign, b"foreign\n")

        with pytest.raises(prepared.module.TransactionError) as captured:
            prepared.module.apply_transaction(prepared.journal, prepared.handles)

        assert captured.value.journal.journal_state == "RECOVERY_REQUIRED"
        assert foreign.read_bytes() == b"foreign\n"
        assert (prepared.roots["after"] / "challenger.toml").read_bytes() == GENERATED_SHIM

    def test_substituted_staged_artifact_is_kept_as_blocking_evidence(self, prepared: Prepared):
        """Reject changed staged bytes and never erase the untrusted artifact.

        The staged after-image is overwritten before apply; the transaction rolls back and later cleanup refuses to
        delete the mismatching file.
        """
        write_private(prepared.roots["after"] / "challenger.toml", b"substituted\n")

        with pytest.raises(prepared.module.TransactionError) as captured:
            prepared.module.apply_transaction(prepared.journal, prepared.handles)

        assert captured.value.journal.journal_state == "ROLLED_BACK"
        assert not (prepared.roots["target"] / TARGET_NAME).exists()
        with pytest.raises(prepared.module.TransactionError, match="cleanup artifact hash mismatch"):
            prepared.module.cleanup_transaction(captured.value.journal, prepared.handles)
        assert (prepared.roots["after"] / "challenger.toml").read_bytes() == b"substituted\n"


class TestRollback:
    """Rollback and cleanup after a failed create."""

    def test_repeat_returns_the_same_terminal_journal(self, prepared: Prepared):
        """Rolling back an already rolled-back journal is a no-op returning the same journal."""
        terminal = _journal_after_failure(prepared, prepared.journal, _fail_at("challenger:published"))

        repeated = prepared.module.rollback_transaction(terminal, prepared.handles)

        assert repeated == terminal

    def test_cleanup_removes_residual_publish_link(self, prepared: Prepared):
        """Clean up a rolled-back transaction even when a state publish link was left behind.

        A crash can leave ``state.publish.json`` linked to ``state.after.json``; terminal cleanup removes it along with
        every other named artifact.
        """
        terminal = _journal_after_failure(prepared, prepared.journal, _fail_at("challenger:published"))
        transaction = prepared.roots["transaction"]
        os.link(transaction / "state.after.json", transaction / "state.publish.json")

        prepared.module.cleanup_transaction(terminal, prepared.handles)

        assert list(transaction.iterdir()) == []


class TestApplyOwnedMutation:
    """Update and remove of a role the manager already owns."""

    @pytest.mark.parametrize(("prepare", "expected_target"), _OWNED_CASES)
    def test_commits_and_quarantines_previous_shim(
        self, prepared: Prepared, prepare: Callable[[Prepared], object], expected_target: bytes | None
    ):
        """Commit an authenticated update/remove and keep the previous bytes in quarantine."""
        journal = prepare(prepared)

        prepared.module.apply_transaction(journal, prepared.handles)

        roots = prepared.roots
        assert _read_or_none(roots["target"] / TARGET_NAME) == expected_target
        assert (roots["quarantine"] / "challenger.toml").read_bytes() == PREVIOUS_SHIM

    @pytest.mark.parametrize("prepare", _OWNED_PREPARERS)
    def test_cleanup_after_commit_empties_transaction(self, prepared: Prepared, prepare: Callable[[Prepared], object]):
        """Remove every exact transaction artifact, including quarantine, after an owned mutation commits."""
        committed = prepared.module.apply_transaction(prepare(prepared), prepared.handles)

        prepared.module.cleanup_transaction(committed, prepared.handles)

        assert list(prepared.roots["transaction"].iterdir()) == []

    @pytest.mark.parametrize("prepare", _OWNED_PREPARERS)
    def test_failure_after_detach_restores_before_image_and_cleans(
        self, prepared: Prepared, prepare: Callable[[Prepared], object]
    ):
        """Restore the exact owned target when failure follows the atomic detach, then clean up fully.

        The previous shim returns from quarantine to its target name; the quarantine slot is empty again and terminal
        cleanup leaves no residue.
        """
        journal = _journal_after_failure(prepared, prepare(prepared), _fail_at("challenger:detached", "injected crash"))

        prepared.module.cleanup_transaction(journal, prepared.handles)

        roots = prepared.roots
        assert journal.journal_state == "ROLLED_BACK"
        assert (roots["target"] / TARGET_NAME).read_bytes() == PREVIOUS_SHIM
        assert not (roots["quarantine"] / "challenger.toml").exists()
        assert list(roots["transaction"].iterdir()) == []


class TestForwardBoundaries:
    """Failure or process death at every durable forward boundary recovers the exact before-image."""

    @pytest.mark.parametrize(("intent", "boundary"), _BOUNDARY_CASES)
    def test_injected_failure_rolls_back_exactly(self, prepared: Prepared, intent: str, boundary: str):
        """Recover the exact before-image after a failure raised at each durable forward boundary.

        Create must leave neither target nor state; update/remove must restore previous target and state bytes, and the
        protected target directory keeps its 0755 mode throughout.
        """
        prepare, expected_target, expected_state = _INTENTS[intent]

        terminal = _journal_after_failure(prepared, prepare(prepared), _fail_at(boundary))

        assert terminal.journal_state == "ROLLED_BACK"
        _assert_before_image(prepared.roots, expected_target, expected_state)

    @pytest.mark.parametrize(("intent", "boundary"), _BOUNDARY_CASES)
    def test_process_death_recovers_exactly(self, prepared: Prepared, intent: str, boundary: str):
        """Recover the exact before-image after an uncatchable process death at each forward boundary.

        A real child process applies the transaction and ``os._exit`` at the boundary, so no in-process cleanup runs;
        rollback from the persisted journal alone must restore the before-image and allow full cleanup.
        """
        prepare, expected_target, expected_state = _INTENTS[intent]
        prepare(prepared)
        roots = prepared.roots
        child = f"""
import os
import sys
sys.path.insert(0, {str(TRANSACTION_PATH.parent)!r})
import _agent_shim_journal as journal_module
import _agent_shim_transaction as transaction_module
root = {str(roots["transaction"])!r}
fds = {{
    "transaction": os.open(root, os.O_RDONLY | os.O_DIRECTORY),
    "target": os.open({str(roots["target"])!r}, os.O_RDONLY | os.O_DIRECTORY),
    "state": os.open({str(roots["state"])!r}, os.O_RDONLY | os.O_DIRECTORY),
    "before": os.open(root + "/before", os.O_RDONLY | os.O_DIRECTORY),
    "after": os.open(root + "/after", os.O_RDONLY | os.O_DIRECTORY),
    "quarantine": os.open(root + "/quarantine", os.O_RDONLY | os.O_DIRECTORY),
}}
handles = transaction_module.TransactionDirectories(
    fds["transaction"], fds["target"], fds["state"],
    fds["before"], fds["after"], fds["quarantine"],
)
journal = journal_module.parse_journal(open(root + "/journal.json", "rb").read())
def kill(name):
    if name == {boundary!r}:
        os._exit(99)
transaction_module.apply_transaction(journal, handles, checkpoint=kill)
"""
        killed = subprocess.run([sys.executable, "-c", child], check=False, timeout=30)
        persisted = prepared.journal_module.parse_journal((roots["transaction"] / "journal.json").read_bytes())

        terminal = prepared.module.rollback_transaction(persisted, prepared.handles)
        prepared.module.cleanup_transaction(terminal, prepared.handles)

        assert killed.returncode == 99
        assert terminal.journal_state == "ROLLED_BACK"
        _assert_before_image(roots, expected_target, expected_state)
        assert list(roots["transaction"].iterdir()) == []
