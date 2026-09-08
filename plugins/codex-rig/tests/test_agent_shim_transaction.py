"""Acceptance checks for transactional shim application and rollback."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile  # noqa: F401 - used by executable doctest examples
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
GENERATOR_PATH = SCRIPTS / "generate_roles.py"
LIFECYCLE_PATH = SCRIPTS / "_agent_shim_lifecycle.py"
JOURNAL_PATH = SCRIPTS / "_agent_shim_journal.py"
POSIX_PATH = SCRIPTS / "_agent_shim_posix.py"
TRANSACTION_PATH = SCRIPTS / "_agent_shim_transaction.py"
TRANSACTION_ID = "123e4567-e89b-42d3-a456-426614174000"
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174001"
DIGEST = "a" * 64


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
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _canonical(value: object) -> bytes:
    """Encode one canonical JSON fixture.

    Example:
        >>> _canonical({"b": 2, "a": 1})
        b'{"a":1,"b":2}'
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _write_private(path: Path, payload: bytes) -> None:
    """Write one exact private transaction fixture.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     path = Path(directory) / "transaction.json"
        ...     _write_private(path, b"prepared")
        ...     path.read_bytes()
        b'prepared'
    """
    path.write_bytes(payload)
    path.chmod(0o600)


def _root_identity(path: Path) -> dict[str, object]:
    """Return journal identity fields for one fixture root.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     _root_identity(Path(directory))["canonical_path"] == directory
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
        "codex_home_identity": _root_identity(roots["home"]),
        "target_root_identity": _root_identity(roots["target"]),
        "state_root_identity": _root_identity(roots["state"]),
        "before_state": {"exists": False, "relative_path": None, "sha256": None, "mode": None},
        "after_state": {
            "exists": True,
            "relative_path": "state.after.json",
            "sha256": hashlib.sha256(state_payload).hexdigest(),
            "mode": "0600",
        },
        "rollback_state_progress": "PENDING",
        "journal_state": "PREPARED",
        "operations": [
            {
                "role_id": "challenger",
                "intent": "create",
                "target_name": "codex-rig-challenger.toml",
                "before_exists": False,
                "before_hash": None,
                "before_mode": None,
                "after_exists": True,
                "after_hash": hashlib.sha256(after_payload).hexdigest(),
                "after_mode": "0600",
                "before_image": None,
                "after_image": "after/challenger.toml",
                "quarantine_name": None,
                "progress": "PLANNED",
                "rollback_progress": "NOT_STARTED",
            }
        ],
    }


def _prepare_owned_transaction(
    roots: dict[str, Path],
    *,
    intent: str,
    after_payload: bytes | None,
) -> dict[str, object]:
    """Replace the create fixture with one owned update or remove transaction."""
    before_payload = b"previous challenger shim\n"
    before_state = b'{"transaction_status":"previous"}'
    after_state = b'{"transaction_status":"current"}'
    value = _create_journal(roots, after_payload or b"unused", after_state)
    operation = value["operations"][0]
    operation.update(
        {
            "intent": intent,
            "before_exists": True,
            "before_hash": hashlib.sha256(before_payload).hexdigest(),
            "before_mode": "0600",
            "before_image": "before/challenger.toml",
            "quarantine_name": "quarantine/challenger.toml",
        }
    )
    if intent == "remove":
        value["action"] = "remove"
        operation.update(
            {
                "after_exists": False,
                "after_hash": None,
                "after_mode": None,
                "after_image": None,
            }
        )
        (roots["after"] / "challenger.toml").unlink()
    else:
        assert after_payload is not None
        operation["after_hash"] = hashlib.sha256(after_payload).hexdigest()
        _write_private(roots["after"] / "challenger.toml", after_payload)
    value["before_state"] = {
        "exists": True,
        "relative_path": "state.before.json",
        "sha256": hashlib.sha256(before_state).hexdigest(),
        "mode": "0600",
    }
    value["after_state"]["sha256"] = hashlib.sha256(after_state).hexdigest()
    _write_private(roots["target"] / "codex-rig-challenger.toml", before_payload)
    _write_private(roots["before"] / "challenger.toml", before_payload)
    _write_private(roots["state"] / "state.json", before_state)
    _write_private(roots["transaction"] / "state.before.json", before_state)
    _write_private(roots["transaction"] / "state.after.json", after_state)
    _write_private(roots["transaction"] / "journal.json", _canonical(value))
    return value


@pytest.fixture(name="transaction_fixture")
def _transaction_fixture(tmp_path: Path) -> tuple[ModuleType, object, object, dict[str, Path]]:
    """Create one prepared transaction with held exact directory descriptors."""
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
    after_payload = b"generated challenger shim\n"
    state_payload = b'{"transaction_status":"current"}'
    journal_value = _create_journal(roots, after_payload, state_payload)
    _write_private(roots["after"] / "challenger.toml", after_payload)
    _write_private(roots["transaction"] / "state.after.json", state_payload)
    _write_private(roots["transaction"] / "journal.json", _canonical(journal_value))
    journal = sys.modules["_agent_shim_journal"].validate_journal(journal_value)
    fds = {
        name: os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        for name, path in roots.items()
        if name in {"target", "state", "transaction", "before", "after", "quarantine"}
    }
    handles = module.TransactionDirectories(
        fds["transaction"],
        fds["target"],
        fds["state"],
        fds["before"],
        fds["after"],
        fds["quarantine"],
    )
    try:
        yield module, journal, handles, roots
    finally:
        for descriptor in fds.values():
            os.close(descriptor)


def test_create_commits_exact_target_state_and_bounded_cleanup(transaction_fixture: tuple[object, ...]) -> None:
    """Publish exact create outputs and remove only terminal transaction residue."""
    module, journal, handles, roots = transaction_fixture

    committed = module.apply_transaction(journal, handles)

    assert committed.journal_state == "COMMITTED"
    assert (roots["target"] / "codex-rig-challenger.toml").read_bytes() == b"generated challenger shim\n"
    assert (roots["state"] / "state.json").read_bytes() == b'{"transaction_status":"current"}'
    assert (roots["after"] / "challenger.toml").stat().st_nlink == 2
    removed = module.cleanup_transaction(committed, handles)
    assert "journal.json" in removed
    assert list(roots["transaction"].iterdir()) == []
    assert (roots["target"] / "codex-rig-challenger.toml").stat().st_nlink == 1
    assert (roots["state"] / "state.json").stat().st_nlink == 1


def test_create_failure_after_publication_rolls_back_exactly(transaction_fixture: tuple[object, ...]) -> None:
    """Remove an unjournaled exact create and reach terminal rollback."""
    module, journal, handles, roots = transaction_fixture

    def _fail_after_publication(name: str) -> None:
        """Inject a failure after the named publication boundary completes."""
        if name == "challenger:published":
            raise RuntimeError("injected crash")

    with pytest.raises(module.TransactionError) as captured:
        module.apply_transaction(journal, handles, checkpoint=_fail_after_publication)

    assert captured.value.journal.journal_state == "ROLLED_BACK"
    assert not (roots["target"] / "codex-rig-challenger.toml").exists()
    assert not (roots["state"] / "state.json").exists()
    assert module.rollback_transaction(captured.value.journal, handles) == captured.value.journal
    os.link(roots["transaction"] / "state.after.json", roots["transaction"] / "state.publish.json")
    module.cleanup_transaction(captured.value.journal, handles)
    assert list(roots["transaction"].iterdir()) == []


def test_concurrent_foreign_create_never_gets_deleted(transaction_fixture: tuple[object, ...]) -> None:
    """Preserve a target that appears after approval and require recovery."""
    module, journal, handles, roots = transaction_fixture
    foreign = roots["target"] / "codex-rig-challenger.toml"
    _write_private(foreign, b"foreign\n")

    with pytest.raises(module.TransactionError) as captured:
        module.apply_transaction(journal, handles)

    assert captured.value.journal.journal_state == "RECOVERY_REQUIRED"
    assert foreign.read_bytes() == b"foreign\n"
    assert (roots["after"] / "challenger.toml").read_bytes() == b"generated challenger shim\n"


def test_substituted_staged_artifact_is_preserved_as_blocking_evidence(
    transaction_fixture: tuple[object, ...],
) -> None:
    """Reject changed staged bytes and never erase the untrusted artifact."""
    module, journal, handles, roots = transaction_fixture
    _write_private(roots["after"] / "challenger.toml", b"substituted\n")

    with pytest.raises(module.TransactionError) as captured:
        module.apply_transaction(journal, handles)

    assert captured.value.journal.journal_state == "ROLLED_BACK"
    assert not (roots["target"] / "codex-rig-challenger.toml").exists()
    with pytest.raises(module.TransactionError, match="cleanup artifact hash mismatch"):
        module.cleanup_transaction(captured.value.journal, handles)
    assert (roots["after"] / "challenger.toml").read_bytes() == b"substituted\n"


@pytest.mark.parametrize("intent", ["update", "remove"])
def test_owned_mutation_commits_and_cleans_exact_artifacts(
    transaction_fixture: tuple[object, ...], intent: str
) -> None:
    """Commit authenticated update/remove operations and bounded cleanup."""
    module, _, handles, roots = transaction_fixture
    after_payload = b"replacement challenger shim\n" if intent == "update" else None
    value = _prepare_owned_transaction(roots, intent=intent, after_payload=after_payload)
    journal = sys.modules["_agent_shim_journal"].validate_journal(value)

    committed = module.apply_transaction(journal, handles)

    target = roots["target"] / "codex-rig-challenger.toml"
    if intent == "update":
        assert target.read_bytes() == after_payload
    else:
        assert not target.exists()
    assert (roots["quarantine"] / "challenger.toml").read_bytes() == b"previous challenger shim\n"
    module.cleanup_transaction(committed, handles)
    assert list(roots["transaction"].iterdir()) == []


@pytest.mark.parametrize("intent", ["update", "remove"])
def test_owned_mutation_failure_after_detach_restores_before_image(
    transaction_fixture: tuple[object, ...], intent: str
) -> None:
    """Restore the exact owned target when failure follows atomic detach."""
    module, _, handles, roots = transaction_fixture
    after_payload = b"replacement challenger shim\n" if intent == "update" else None
    value = _prepare_owned_transaction(roots, intent=intent, after_payload=after_payload)
    journal = sys.modules["_agent_shim_journal"].validate_journal(value)

    def _fail_after_detach(name: str) -> None:
        """Inject a failure after detaching the prior target artifact."""
        if name == "challenger:detached":
            raise RuntimeError("injected crash")

    with pytest.raises(module.TransactionError) as captured:
        module.apply_transaction(journal, handles, checkpoint=_fail_after_detach)

    assert captured.value.journal.journal_state == "ROLLED_BACK"
    assert (roots["target"] / "codex-rig-challenger.toml").read_bytes() == b"previous challenger shim\n"
    assert not (roots["quarantine"] / "challenger.toml").exists()
    module.cleanup_transaction(captured.value.journal, handles)
    assert list(roots["transaction"].iterdir()) == []


@pytest.mark.parametrize(
    ("intent", "boundary"),
    [
        pytest.param("create", "challenger:published", id="create-challenger-published"),
        pytest.param("create", "challenger:verified", id="create-challenger-verified"),
        pytest.param("create", "state:published", id="create-state-published"),
        pytest.param("update", "challenger:detached", id="update-challenger-detached"),
        pytest.param("update", "challenger:published", id="update-challenger-published"),
        pytest.param("update", "challenger:verified", id="update-challenger-verified"),
        pytest.param("update", "state:published", id="update-state-published"),
        pytest.param("remove", "challenger:detached", id="remove-challenger-detached"),
        pytest.param("remove", "challenger:verified", id="remove-challenger-verified"),
        pytest.param("remove", "state:published", id="remove-state-published"),
    ],
)
def test_each_forward_mutation_boundary_rolls_back_exactly(
    transaction_fixture: tuple[object, ...], intent: str, boundary: str
) -> None:
    """Recover the exact before-image after every durable forward boundary."""
    module, create, handles, roots = transaction_fixture
    if intent == "create":
        journal = create
    else:
        after_payload = b"replacement challenger shim\n" if intent == "update" else None
        value = _prepare_owned_transaction(roots, intent=intent, after_payload=after_payload)
        journal = sys.modules["_agent_shim_journal"].validate_journal(value)

    def _fail_at_boundary(name: str) -> None:
        """Inject a failure at the parameterized transaction checkpoint."""
        if name == boundary:
            raise RuntimeError("injected boundary failure")

    with pytest.raises(module.TransactionError) as captured:
        module.apply_transaction(journal, handles, checkpoint=_fail_at_boundary)

    assert captured.value.journal.journal_state == "ROLLED_BACK"
    target = roots["target"] / "codex-rig-challenger.toml"
    state = roots["state"] / "state.json"
    if intent == "create":
        assert not target.exists()
        assert not state.exists()
    else:
        assert target.read_bytes() == b"previous challenger shim\n"
        assert state.read_bytes() == b'{"transaction_status":"previous"}'
    assert stat.S_IMODE(roots["target"].stat().st_mode) == 0o755


@pytest.mark.parametrize(
    ("intent", "boundary"),
    [
        pytest.param("create", "challenger:published", id="create-challenger-published"),
        pytest.param("create", "challenger:verified", id="create-challenger-verified"),
        pytest.param("create", "state:published", id="create-state-published"),
        pytest.param("update", "challenger:detached", id="update-challenger-detached"),
        pytest.param("update", "challenger:published", id="update-challenger-published"),
        pytest.param("update", "challenger:verified", id="update-challenger-verified"),
        pytest.param("update", "state:published", id="update-state-published"),
        pytest.param("remove", "challenger:detached", id="remove-challenger-detached"),
        pytest.param("remove", "challenger:verified", id="remove-challenger-verified"),
        pytest.param("remove", "state:published", id="remove-state-published"),
    ],
)
def test_process_death_at_each_forward_boundary_recovers_exactly(
    transaction_fixture: tuple[object, ...], intent: str, boundary: str
) -> None:
    """Prove durable recovery after uncatchable process death at every forward boundary."""
    module, _, handles, roots = transaction_fixture
    if intent != "create":
        after_payload = b"replacement challenger shim\n" if intent == "update" else None
        _prepare_owned_transaction(roots, intent=intent, after_payload=after_payload)
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

    assert killed.returncode == 99
    journal_module = sys.modules["_agent_shim_journal"]
    journal = journal_module.parse_journal((roots["transaction"] / "journal.json").read_bytes())
    terminal = module.rollback_transaction(journal, handles)
    assert terminal.journal_state == "ROLLED_BACK"
    target = roots["target"] / "codex-rig-challenger.toml"
    state = roots["state"] / "state.json"
    if intent == "create":
        assert not target.exists()
        assert not state.exists()
    else:
        assert target.read_bytes() == b"previous challenger shim\n"
        assert state.read_bytes() == b'{"transaction_status":"previous"}'
    assert stat.S_IMODE(roots["target"].stat().st_mode) == 0o755
    module.cleanup_transaction(terminal, handles)
    assert list(roots["transaction"].iterdir()) == []
