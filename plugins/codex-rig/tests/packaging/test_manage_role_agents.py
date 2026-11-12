"""Acceptance checks for the read-only agent-shims manager surface."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile  # noqa: F401
from collections.abc import Callable, Generator
from pathlib import Path
from types import ModuleType

import pytest
from _fixture_files import tree_snapshot
from _modules import load_module_scoped
from _platform import (
    DIRECTORY_SYMLINKS_AVAILABLE,
    FILE_SYMLINKS_AVAILABLE,
    POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE,
    POSIX_FILE_MODES_AVAILABLE,
    POSIX_OBSERVER_PRIMITIVES_AVAILABLE,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PLUGIN_ROOT / "scripts"
MANAGER_PATH = SCRIPTS / "manage_role_agents.py"
#: Sibling modules the manager imports by name, in dependency order.
MANAGER_DEPENDENCIES = (
    ("generate_roles", SCRIPTS / "generate_roles.py"),
    ("_agent_shim_lifecycle", SCRIPTS / "_agent_shim_lifecycle.py"),
    ("_agent_shim_journal", SCRIPTS / "_agent_shim_journal.py"),
    ("_agent_shim_observe", SCRIPTS / "_agent_shim_observe.py"),
    ("_agent_shim_plan", SCRIPTS / "_agent_shim_plan.py"),
    ("_agent_shim_approval", SCRIPTS / "_agent_shim_approval.py"),
    ("_agent_shim_posix", SCRIPTS / "_agent_shim_posix.py"),
    ("_agent_shim_transaction", SCRIPTS / "_agent_shim_transaction.py"),
)
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174001"
#: Number of ``codex-rig-*.toml`` agent files a full-roster install writes.
ROSTER_SIZE = 15

_skip_windows_posix = pytest.mark.skipif(
    not (POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE and POSIX_FILE_MODES_AVAILABLE and POSIX_OBSERVER_PRIMITIVES_AVAILABLE),
    reason="host lacks agent-shim descriptor, ownership, or POSIX file-mode primitives",
)
_requires_directory_symlinks = pytest.mark.skipif(
    not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks"
)
_requires_file_symlinks = pytest.mark.skipif(
    not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"
)


def _identity(*paths: Path) -> tuple[tuple[int, int, int, int], ...]:
    """Return the (mode, inode, link count, size) of each path without following symlinks."""
    return tuple((p.lstat().st_mode, p.lstat().st_ino, p.lstat().st_nlink, p.lstat().st_size) for p in paths)


def _executable(tmp_path: Path) -> Path:
    """Create one bounded executable used only as Codex identity evidence.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     _executable(Path(directory)).name
        'codex'
    """
    path = tmp_path / "codex"
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def _nonce(number: int) -> str:
    """Return the deterministic transaction nonce numbered ``number``."""
    return f"123e4567-e89b-42d3-a456-4266141740{number:02d}"


def _plan(
    manager: ModuleType, home: Path, codex: Path, action: str, nonce: int, install_id: str | None = None
) -> object:
    """Plan one mutation for ``ManagerAction.<action>`` against the real package without the active-package probe."""
    optional = {} if install_id is None else {"install_id": install_id}
    return manager.plan_mutation(
        action=manager.ManagerAction[action],
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        require_active_package=False,
        transaction_nonce=_nonce(nonce),
        **optional,
    )


def _roster_files(home: Path) -> list[Path]:
    """Return the Codex Rig agent files currently installed under ``home``."""
    return sorted((home / "agents").glob("codex-rig-*.toml"))


def _state_text(home: Path) -> str:
    """Return the manager's persisted state document for ``home``."""
    return (home / "codex-rig" / "shims" / "state.json").read_text()


def _kill_install_at(
    home: Path, codex: Path, nonce: int, boundary: str, exit_code: int
) -> subprocess.CompletedProcess[bytes]:
    """Run a real child process that approves an install and dies with ``exit_code`` at checkpoint ``boundary``."""
    child = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(SCRIPTS)!r})
import manage_role_agents as manager
plan = manager.plan_mutation(
    action="install",
    codex_home=Path({str(home)!r}),
    plugin_root=Path({str(PLUGIN_ROOT)!r}),
    codex_binary=Path({str(codex)!r}),
    require_active_package=False,
    install_id={INSTALL_ID!r},
    transaction_nonce={_nonce(nonce)!r},
)
def kill(boundary):
    if boundary == {boundary!r}:
        os._exit({exit_code})
manager.apply_mutation(plan, plan.approval.digest, checkpoint=kill)
"""
    return subprocess.run([sys.executable, "-c", child], check=False, timeout=30)


def _write_plugin_list_codex(path: Path, version: str) -> None:
    """Write an executable that reports Codex Rig ``version`` as installed and enabled."""
    payload = json.dumps(
        {
            "installed": [
                {
                    "pluginId": "codex-rig@borda-ai-rig",
                    "name": "codex-rig",
                    "marketplaceName": "borda-ai-rig",
                    "installed": True,
                    "enabled": True,
                    "version": version,
                }
            ]
        },
        separators=(",", ":"),
    )
    path.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n")
    path.chmod(0o700)


def _chmod_private(*paths: Path) -> None:
    """Restrict each directory to its owner, as the manager requires of its state tree."""
    for path in paths:
        path.chmod(0o700)


def _forbidden(*_args: object, **_kwargs: object) -> None:
    """Fail the test if a platform-blocked install reaches mutation planning or interactive approval."""
    pytest.fail("platform-blocked install continued into mutation flow")


@pytest.fixture(scope="module")
def shim_dependencies() -> Generator[None, None, None]:
    """Load the manager's sibling modules once per module and unregister them afterwards."""
    registry = pytest.MonkeyPatch()
    for name, path in MANAGER_DEPENDENCIES:
        load_module_scoped(name, path, registry)
    yield
    registry.undo()


@pytest.fixture
def manager(shim_dependencies: None, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load a fresh copy of the manager module for one test."""
    return load_module_scoped("codex_rig_manager", MANAGER_PATH, monkeypatch)


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    """Create an owner-only isolated Codex home."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


@pytest.fixture
def codex_binary(tmp_path: Path) -> Path:
    """Create the bounded executable that stands in for the Codex CLI."""
    return _executable(tmp_path)


@pytest.fixture
def agents_dir(codex_home: Path) -> Path:
    """Create the agents directory with the group-readable mode a real Codex home uses."""
    agents = codex_home / "agents"
    agents.mkdir(mode=0o700)
    agents.chmod(0o755)
    return agents


@pytest.fixture
def installed_roster(manager: ModuleType, codex_home: Path, codex_binary: Path, agents_dir: Path) -> Path:
    """Commit one full-roster install into the Codex home and return its agents directory."""
    install = _plan(manager, codex_home, codex_binary, "INSTALL", 2, INSTALL_ID)
    manager.apply_mutation(install, install.approval.digest)
    return agents_dir


@_skip_windows_posix
class TestDiagnose:
    """``diagnose`` reports local health without ever writing user state."""

    def test_doctor_validates_package_without_writing_user_state(
        self, manager: ModuleType, tmp_path: Path, codex_home: Path, codex_binary: Path
    ) -> None:
        """Doctor reports verified local prerequisites as degraded while preserving every home byte.

        With a valid package, an executable and an empty home, the package and executable checks pass, the active
        package cannot be confirmed (hence degraded), and the whole temp tree stays unchanged.
        """
        before = tree_snapshot(tmp_path)

        result = manager.diagnose(
            action=manager.ManagerAction.DOCTOR,
            codex_home=codex_home,
            plugin_root=PLUGIN_ROOT,
            codex_binary=codex_binary,
            check_active_package=False,
        )

        assert result.classification == "degraded"
        assert result.checks["package"].status == "pass"
        assert result.checks["executables"].status == "pass"
        assert result.checks["active_package"].status == "degraded"
        assert result.state == "absent"
        assert result.targets == "absent"
        assert tree_snapshot(tmp_path) == before

    @pytest.mark.parametrize("action", ["doctor", "status"])
    def test_direct_run_does_not_write_installed_plugin_bytecode(
        self, tmp_path: Path, codex_home: Path, codex_binary: Path, action: str
    ) -> None:
        """Running the manager script directly leaves the installed plugin tree byte-identical.

        The plugin is copied to an "installed" location and run as a real subprocess with bytecode writing enabled; a
        stray ``__pycache__`` would show up in the before/after snapshot.
        """
        plugin_root = tmp_path / "installed-plugin"
        shutil.copytree(PLUGIN_ROOT, plugin_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        environment = os.environ.copy()
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment["CODEX_HOME"] = str(codex_home)
        environment["PATH"] = f"{codex_binary.parent}{os.pathsep}{environment.get('PATH', '')}"
        before = tree_snapshot(plugin_root)

        completed = subprocess.run(
            [sys.executable, str(plugin_root / "scripts" / "manage_role_agents.py"), action],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            timeout=30,
        )

        assert completed.returncode == 0, completed.stderr.decode()
        result = json.loads(completed.stdout)
        assert result["action"] == action
        assert result["classification"] == "degraded"
        assert tree_snapshot(plugin_root) == before

    def test_status_reports_corrupt_state_as_blocked_without_mutation(
        self, manager: ModuleType, tmp_path: Path, codex_home: Path, codex_binary: Path
    ) -> None:
        """Untrusted shim state is exposed as blocked without repair, adoption, or cleanup writes.

        A state file that is not valid JSON must be reported as corrupt and left exactly as found.
        """
        state = codex_home / "codex-rig" / "shims"
        state.mkdir(parents=True, mode=0o700)
        _chmod_private(state, codex_home / "codex-rig")
        (codex_home / "agents").mkdir(mode=0o700)
        payload = state / "state.json"
        payload.write_bytes(b"corrupt")
        payload.chmod(0o600)
        before = tree_snapshot(tmp_path)

        result = manager.diagnose(
            action=manager.ManagerAction.STATUS,
            codex_home=codex_home,
            plugin_root=PLUGIN_ROOT,
            codex_binary=codex_binary,
            check_active_package=False,
        )

        assert result.classification == "blocked"
        assert result.state == "corrupt"
        assert tree_snapshot(tmp_path) == before

    @_requires_directory_symlinks
    def test_doctor_refuses_symlinked_home_alias(
        self, manager: ModuleType, tmp_path: Path, codex_home: Path, codex_binary: Path
    ) -> None:
        """Doctor blocks an unresolved home alias instead of silently changing which directory it trusts."""
        linked = tmp_path / "linked"
        linked.symlink_to(codex_home, target_is_directory=True)

        with pytest.raises(ValueError, match="canonical non-symlink"):
            manager.diagnose(
                action="doctor",
                codex_home=linked,
                plugin_root=PLUGIN_ROOT,
                codex_binary=codex_binary,
                check_active_package=False,
            )

    def test_active_package_probe_uses_disposable_home_copy(
        self, manager: ModuleType, tmp_path: Path, codex_home: Path
    ) -> None:
        """The Codex CLI probe runs against a disposable copy, so it cannot create temp state in the real home.

        A cached plugin install, its config and a stub CLI that reports it enabled make the doctor healthy; the real
        home snapshot must be unchanged afterwards.
        """
        version = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())["version"]
        plugin = codex_home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
        plugin.parent.mkdir(parents=True, mode=0o700)
        # Local run reports and bytecode are not payload; copying them can exceed the doctor's metadata budget.
        shutil.copytree(PLUGIN_ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".reports"))
        config = codex_home / "config.toml"
        config.write_text(
            '[marketplaces.borda-ai-rig]\nsource_type = "local"\nsource = "/fixture"\n\n'
            '[plugins."codex-rig@borda-ai-rig"]\nenabled = true\n'
        )
        config.chmod(0o600)
        codex = tmp_path / "codex-list"
        _write_plugin_list_codex(codex, version)
        before = tree_snapshot(codex_home)

        result = manager.diagnose(
            action=manager.ManagerAction.DOCTOR,
            codex_home=codex_home,
            plugin_root=plugin,
            codex_binary=codex,
            check_active_package=True,
        )

        assert result.classification == "healthy"
        assert result.checks["active_package"].status == "pass"
        assert tree_snapshot(codex_home) == before


@_skip_windows_posix
class TestMain:
    """The one-action command grammar of ``main``."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param([], 2, id="no-action"),
            pytest.param(["unknown"], 2, id="unknown"),
            pytest.param(["doctor", "extra"], 2, id="doctor-extra"),
            pytest.param(["install"], 5, id="install"),
            pytest.param(["remove"], 5, id="remove"),
        ],
    )
    def test_rejects_invalid_or_unwired_mutation_actions(
        self,
        manager: ModuleType,
        tmp_path: Path,
        codex_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        arguments: list[str],
        expected: int,
    ) -> None:
        """The one-action grammar is deterministic: bad usage exits 2, install/remove exit 5, nothing is written.

        No hidden bypass flags exist, so a missing, unknown or extra argument is a usage error and the mutating actions
        are not wired yet.
        """
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        before = tree_snapshot(tmp_path)

        assert manager.main(arguments) == expected

        assert "classification" in capsys.readouterr().out
        assert tree_snapshot(tmp_path) == before

    def test_install_is_platform_blocked_before_plan_or_approval(
        self,
        manager: ModuleType,
        tmp_path: Path,
        codex_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unsupported shim selection is refused without planning, approval prompts, or writes.

        Planning, recovery planning and ``input`` are rigged to fail the test if reached, proving the block happens
        first.
        """
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr(manager, "plan_mutation", _forbidden)
        monkeypatch.setattr(manager, "plan_recovery", _forbidden)
        monkeypatch.setattr("builtins.input", _forbidden)
        before = tree_snapshot(tmp_path)

        assert manager.main(["install"]) == 5

        assert json.loads(capsys.readouterr().out) == {
            "action": "install",
            "classification": "platform-blocked",
            "detail": "active Codex collaboration has no explicit custom-agent selector",
            "writes": 0,
        }
        assert tree_snapshot(tmp_path) == before


@_skip_windows_posix
class TestInstallRemove:
    """Approved install and remove transactions over a Codex home."""

    def test_install_commits_full_roster_only_after_approval(
        self, manager: ModuleType, tmp_path: Path, codex_home: Path, codex_binary: Path, agents_dir: Path
    ) -> None:
        """A planned install stages nothing until approved, then commits all 15 agent files.

        Planning alone returns an approval and leaves the whole temp-tree snapshot untouched; applying it with that
        approval's digest commits every roster file and preserves the agents directory mode.
        """
        before_plan = tree_snapshot(tmp_path)
        install = _plan(manager, codex_home, codex_binary, "INSTALL", 2, INSTALL_ID)
        assert install.approval is not None
        assert tree_snapshot(tmp_path) == before_plan

        committed = manager.apply_mutation(install, install.approval.digest)

        assert committed.journal_state == "COMMITTED"
        assert len(_roster_files(codex_home)) == ROSTER_SIZE
        assert stat.S_IMODE(agents_dir.stat().st_mode) == 0o755

    def test_repeated_install_over_converged_roster_is_a_noop(
        self, manager: ModuleType, codex_home: Path, codex_binary: Path, installed_roster: Path
    ) -> None:
        """A second install plan against an already-installed roster writes nothing.

        The repeated plan carries no approval, applying it returns nothing, and the home snapshot stays byte-identical.
        """
        installed = tree_snapshot(codex_home)

        repeated = _plan(manager, codex_home, codex_binary, "INSTALL", 3)

        assert repeated.approval is None
        assert manager.apply_mutation(repeated, "") is None
        assert tree_snapshot(codex_home) == installed

    def test_remove_over_converged_roster_deletes_files_and_marks_state_removed(
        self, manager: ModuleType, codex_home: Path, codex_binary: Path, installed_roster: Path
    ) -> None:
        """Removing a converged install deletes every roster file and records the removal.

        The agents directory is left empty with its mode preserved, and state.json persists a "removed" transaction
        status.
        """
        removal = _plan(manager, codex_home, codex_binary, "REMOVE", 4)
        assert removal.approval is not None

        removed = manager.apply_mutation(removal, removal.approval.digest)

        assert removed.journal_state == "COMMITTED"
        assert _roster_files(codex_home) == []
        assert stat.S_IMODE(installed_roster.stat().st_mode) == 0o755
        assert '"transaction_status":"removed"' in _state_text(codex_home)

    def test_wrong_approval_digest_causes_zero_writes(
        self, manager: ModuleType, tmp_path: Path, codex_home: Path, codex_binary: Path
    ) -> None:
        """A wrong approval digest is refused before the coordination lock or any root is created."""
        plan = _plan(manager, codex_home, codex_binary, "INSTALL", 5, INSTALL_ID)
        before = tree_snapshot(tmp_path)

        with pytest.raises(ValueError, match="approval digest mismatch"):
            manager.apply_mutation(plan, "f" * 64)

        assert tree_snapshot(tmp_path) == before

    def test_under_lock_drift_preserves_concurrent_foreign_target(
        self, manager: ModuleType, codex_home: Path, codex_binary: Path
    ) -> None:
        """Evidence that changes after approval stops the install before any transaction exists.

        A foreign file appears at one of the roster targets between planning and applying; it must survive untouched and
        no manager state may be created.
        """
        plan = _plan(manager, codex_home, codex_binary, "INSTALL", 6, INSTALL_ID)
        agents = codex_home / "agents"
        agents.mkdir(mode=0o700)
        foreign = agents / "codex-rig-challenger.toml"
        foreign.write_bytes(b"foreign\n")
        foreign.chmod(0o600)

        with pytest.raises(ValueError, match=r"under-lock filesystem observation changed|candidate changed"):
            manager.apply_mutation(plan, plan.approval.digest)

        assert foreign.read_bytes() == b"foreign\n"
        assert not (codex_home / "codex-rig").exists()


@_skip_windows_posix
class TestRecovery:
    """Interrupted transactions are recovered only through an approved recovery plan."""

    def test_killed_process_requires_approved_rollback_then_can_resume(
        self, manager: ModuleType, codex_home: Path, codex_binary: Path
    ) -> None:
        """An unjournaled publication left by process death is rolled back on approval, and a later install converges.

        A real child process is killed right after publishing the first agent file. Recovery must report a mutating
        journal, roll back to no roster files, and then a fresh approved install must commit the full roster.
        """
        killed = _kill_install_at(codex_home, codex_binary, 7, "challenger:published", 97)

        recovery = manager.plan_recovery(
            action=manager.ManagerAction.INSTALL, codex_home=codex_home, plugin_root=PLUGIN_ROOT
        )
        assert killed.returncode == 97
        assert recovery is not None
        assert recovery.journal.journal_state == "MUTATING"
        terminal = manager.apply_recovery(recovery, recovery.digest)
        assert terminal.journal_state == "ROLLED_BACK"
        assert _roster_files(codex_home) == []
        assert (
            manager.plan_recovery(action=manager.ManagerAction.INSTALL, codex_home=codex_home, plugin_root=PLUGIN_ROOT)
            is None
        )
        resumed = _plan(manager, codex_home, codex_binary, "INSTALL", 8, INSTALL_ID)
        committed = manager.apply_mutation(resumed, resumed.approval.digest)
        assert committed.journal_state == "COMMITTED"
        assert len(_roster_files(codex_home)) == ROSTER_SIZE

    def test_killed_state_commit_is_approved_and_finalized(
        self, manager: ModuleType, codex_home: Path, codex_binary: Path
    ) -> None:
        """Process death right after the durable state commit is finalized on approval with exact installed state.

        The child dies at ``journal:state-committed``; recovery must see STATE_COMMITTED and finalize to COMMITTED with
        the full roster present and state.json marked current.
        """
        killed = _kill_install_at(codex_home, codex_binary, 9, "journal:state-committed", 98)

        recovery = manager.plan_recovery(
            action=manager.ManagerAction.INSTALL, codex_home=codex_home, plugin_root=PLUGIN_ROOT
        )
        assert killed.returncode == 98
        assert recovery.journal.journal_state == "STATE_COMMITTED"
        terminal = manager.apply_recovery(recovery, recovery.digest)
        assert terminal.journal_state == "COMMITTED"
        assert len(_roster_files(codex_home)) == ROSTER_SIZE
        assert '"transaction_status":"current"' in _state_text(codex_home)

    def test_partial_initial_journal_cleanup_requires_exact_approval(
        self, manager: ModuleType, codex_home: Path
    ) -> None:
        """The sole pre-authority artifact, a partial initial journal, is cleaned up on approval without parsing it.

        Only ``journal.initial.json`` with truncated JSON exists; recovery has no journal to resume, cleanup removes the
        transaction directory, and no agent target is written.
        """
        transaction = codex_home / "codex-rig" / "shims" / "transactions" / "123e4567-e89b-42d3-a456-426614174010"
        transaction.mkdir(parents=True, mode=0o700)
        _chmod_private(
            codex_home, codex_home / "codex-rig", codex_home / "codex-rig" / "shims", transaction.parent, transaction
        )
        initial = transaction / "journal.initial.json"
        initial.write_bytes(b'{"partial"')
        initial.chmod(0o600)

        recovery = manager.plan_recovery(
            action=manager.ManagerAction.INSTALL, codex_home=codex_home, plugin_root=PLUGIN_ROOT
        )

        assert recovery.journal is None
        assert manager.apply_recovery(recovery, recovery.digest) is None
        assert not transaction.exists()
        assert not (codex_home / "agents").exists()


@_skip_windows_posix
class TestCodexExecutableBound:
    """The Codex executable digest is bounded by the package-wide binary limit."""

    @_requires_file_symlinks
    @pytest.mark.parametrize(
        "size_for",
        [
            pytest.param(lambda limit: 268_435_457, id="above-obsolete-256-mib-limit"),
            pytest.param(lambda limit: limit, id="exact-package-bound"),
        ],
    )
    def test_accepts_symlinked_executable_within_package_bound(
        self, manager: ModuleType, tmp_path: Path, size_for: Callable[[int], int]
    ) -> None:
        """A stable symlinked Codex executable is digested above the obsolete 256 MiB limit and at the 512 MiB cap.

        The bound is inclusive, so a file exactly ``MAX_BINARY_BYTES`` long is accepted; the digest is a SHA-256 hex
        string, the canonical path is the link target, and neither file's metadata changes.
        """
        target = tmp_path / "codex-target"
        with target.open("wb") as stream:
            stream.truncate(size_for(manager.MAX_BINARY_BYTES))
        target.chmod(0o700)
        link = tmp_path / "codex"
        link.symlink_to(target)
        before = _identity(target, link)

        canonical, digest = manager._digest_regular_executable(link, "Codex executable")

        assert canonical == target.resolve()
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert _identity(target, link) == before

    def test_rejects_oversized_executable_naming_observed_size_and_limit(
        self, manager: ModuleType, tmp_path: Path
    ) -> None:
        """An executable one byte over the limit is refused with a message naming its path, size and the limit."""
        target = tmp_path / "codex"
        with target.open("wb") as stream:
            stream.truncate(manager.MAX_BINARY_BYTES + 1)
        target.chmod(0o700)

        with pytest.raises(
            ValueError,
            match=(
                rf"Codex executable exceeds {manager.MAX_BINARY_BYTES}-byte safety limit: "
                rf"{target} has {manager.MAX_BINARY_BYTES + 1} bytes"
            ),
        ):
            manager._digest_regular_executable(target, "Codex executable")
