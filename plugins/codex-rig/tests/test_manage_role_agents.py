"""Acceptance checks for the read-only agent-shims manager surface."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
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
OBSERVER_PATH = SCRIPTS / "_agent_shim_observe.py"
PLAN_PATH = SCRIPTS / "_agent_shim_plan.py"
APPROVAL_PATH = SCRIPTS / "_agent_shim_approval.py"
POSIX_PATH = SCRIPTS / "_agent_shim_posix.py"
TRANSACTION_PATH = SCRIPTS / "_agent_shim_transaction.py"
MANAGER_PATH = SCRIPTS / "manage_role_agents.py"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load the manager with its direct sibling dependencies available."""
    if path == LIFECYCLE_PATH and "generate_roles" not in sys.modules:
        _load_module(GENERATOR_PATH, "generate_roles")
    if path == JOURNAL_PATH and "_agent_shim_lifecycle" not in sys.modules:
        _load_module(LIFECYCLE_PATH, "_agent_shim_lifecycle")
    if path == OBSERVER_PATH:
        for dependency, module_name in (
            (GENERATOR_PATH, "generate_roles"),
            (LIFECYCLE_PATH, "_agent_shim_lifecycle"),
            (JOURNAL_PATH, "_agent_shim_journal"),
        ):
            if module_name not in sys.modules:
                _load_module(dependency, module_name)
    if path == MANAGER_PATH:
        for dependency, module_name in (
            (GENERATOR_PATH, "generate_roles"),
            (LIFECYCLE_PATH, "_agent_shim_lifecycle"),
            (JOURNAL_PATH, "_agent_shim_journal"),
            (OBSERVER_PATH, "_agent_shim_observe"),
            (PLAN_PATH, "_agent_shim_plan"),
            (APPROVAL_PATH, "_agent_shim_approval"),
            (POSIX_PATH, "_agent_shim_posix"),
            (TRANSACTION_PATH, "_agent_shim_transaction"),
        ):
            if module_name not in sys.modules:
                _load_module(dependency, module_name)
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture mutation-relevant bytes and metadata while excluding atime."""
    rows = []
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
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
                path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
            )
        )
    return tuple(rows)


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


_skip_windows_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="agent-shim lifecycle requires POSIX primitives"
)


@_skip_windows_posix
def test_doctor_validates_package_without_writing_user_state(tmp_path: Path) -> None:
    """Report verified local prerequisites while preserving every home byte."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_doctor")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    codex = _executable(tmp_path)
    before = _snapshot(tmp_path)

    result = module.diagnose(
        action=module.ManagerAction.DOCTOR,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        check_active_package=False,
    )

    assert result.classification == "degraded"
    assert result.checks["package"].status == "pass"
    assert result.checks["executables"].status == "pass"
    assert result.checks["active_package"].status == "degraded"
    assert result.state == "absent"
    assert result.targets == "absent"
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
@pytest.mark.parametrize("action", ["doctor", "status"])
def test_direct_diagnostic_does_not_write_installed_plugin_bytecode(tmp_path: Path, action: str) -> None:
    """Keep standard manager invocation read-only across the installed plugin tree."""
    plugin_root = tmp_path / "installed-plugin"
    shutil.copytree(PLUGIN_ROOT, plugin_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _executable(tmp_path)
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment["CODEX_HOME"] = str(home)
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment.get('PATH', '')}"
    before = _snapshot(plugin_root)

    completed = subprocess.run(
        [sys.executable, str(plugin_root / "scripts" / "manage_role_agents.py"), action],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    result = json.loads(completed.stdout)
    assert result["action"] == action
    assert result["classification"] == "degraded"
    assert _snapshot(plugin_root) == before


@_skip_windows_posix
def test_status_reports_corrupt_state_as_blocked_without_mutation(tmp_path: Path) -> None:
    """Expose untrusted state without repair, adoption, or cleanup writes."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_status")
    home = tmp_path / "home"
    state = home / "codex-rig" / "shims"
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    (home / "codex-rig").chmod(0o700)
    (home / "agents").mkdir(mode=0o700)
    payload = state / "state.json"
    payload.write_bytes(b"corrupt")
    payload.chmod(0o600)
    codex = _executable(tmp_path)
    before = _snapshot(tmp_path)

    result = module.diagnose(
        action=module.ManagerAction.STATUS,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        check_active_package=False,
    )

    assert result.classification == "blocked"
    assert result.state == "corrupt"
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param([], 2, id="punctuation"),
        pytest.param(["unknown"], 2, id="unknown"),
        pytest.param(["doctor", "extra"], 2, id="doctor-extra"),
        pytest.param(["install"], 5, id="install"),
        pytest.param(["remove"], 5, id="remove"),
    ],
)
def test_public_grammar_rejects_invalid_or_unwired_mutation_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected: int,
) -> None:
    """Keep the one-action grammar deterministic with no hidden bypass flags."""
    module = _load_module(MANAGER_PATH, f"codex_rig_manager_grammar_{expected}_{len(arguments)}")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("CODEX_HOME", str(home))
    before = _snapshot(tmp_path)

    assert module.main(arguments) == expected
    output = capsys.readouterr().out

    assert "classification" in output
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_install_is_platform_blocked_before_plan_or_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refuse unsupported shim selection without planning, approval, or writes."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_platform_blocked")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("CODEX_HOME", str(home))

    def _unexpected_call(*_args: object, **_kwargs: object) -> None:
        """Fail if install reaches mutation planning or interactive approval."""
        pytest.fail("platform-blocked install continued into mutation flow")

    monkeypatch.setattr(module, "plan_mutation", _unexpected_call)
    monkeypatch.setattr(module, "plan_recovery", _unexpected_call)
    monkeypatch.setattr("builtins.input", _unexpected_call)
    before = _snapshot(tmp_path)

    assert module.main(["install"]) == 5

    assert json.loads(capsys.readouterr().out) == {
        "action": "install",
        "classification": "platform-blocked",
        "detail": "active Codex collaboration has no explicit custom-agent selector",
        "writes": 0,
    }
    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_doctor_refuses_symlinked_home_alias(tmp_path: Path) -> None:
    """Block unresolved home aliases instead of silently changing authority."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_alias")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(home, target_is_directory=True)

    with pytest.raises(ValueError, match="canonical non-symlink"):
        module.diagnose(
            action="doctor",
            codex_home=linked,
            plugin_root=PLUGIN_ROOT,
            codex_binary=_executable(tmp_path),
            check_active_package=False,
        )


@_skip_windows_posix
def test_internal_approved_install_reinstall_remove_converges(tmp_path: Path) -> None:
    """Apply and remove the whole roster while repeated actions produce no writes."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_mutation")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    agents = home / "agents"
    agents.mkdir(mode=0o700)
    agents.chmod(0o755)
    codex = _executable(tmp_path)
    install_id = "123e4567-e89b-42d3-a456-426614174001"
    before_plan = _snapshot(tmp_path)
    install = module.plan_mutation(
        action=module.ManagerAction.INSTALL,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        require_active_package=False,
        install_id=install_id,
        transaction_nonce="123e4567-e89b-42d3-a456-426614174002",
    )
    assert install.approval is not None
    assert _snapshot(tmp_path) == before_plan

    committed = module.apply_mutation(install, install.approval.digest)

    assert committed.journal_state == "COMMITTED"
    targets = sorted(agents.glob("codex-rig-*.toml"))
    assert len(targets) == 15
    assert stat.S_IMODE(agents.stat().st_mode) == 0o755
    installed = _snapshot(home)
    repeated = module.plan_mutation(
        action=module.ManagerAction.INSTALL,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        require_active_package=False,
        transaction_nonce="123e4567-e89b-42d3-a456-426614174003",
    )
    assert repeated.approval is None
    assert module.apply_mutation(repeated, "") is None
    assert _snapshot(home) == installed

    removal = module.plan_mutation(
        action=module.ManagerAction.REMOVE,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        require_active_package=False,
        transaction_nonce="123e4567-e89b-42d3-a456-426614174004",
    )
    assert removal.approval is not None
    removed = module.apply_mutation(removal, removal.approval.digest)

    assert removed.journal_state == "COMMITTED"
    assert list(agents.glob("codex-rig-*.toml")) == []
    assert stat.S_IMODE(agents.stat().st_mode) == 0o755
    state = (home / "codex-rig" / "shims" / "state.json").read_text()
    assert '"transaction_status":"removed"' in state


@_skip_windows_posix
def test_large_symlinked_codex_executable_uses_package_binary_bound(tmp_path: Path) -> None:
    """Accept a stable Codex executable above the obsolete 256 MiB limit."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_large_codex")
    target = tmp_path / "codex-target"
    with target.open("wb") as stream:
        stream.truncate(268_435_457)
    target.chmod(0o700)
    link = tmp_path / "codex"
    link.symlink_to(target)

    canonical, digest = module._digest_regular_executable(link, "Codex executable")

    assert canonical == target.resolve()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


@_skip_windows_posix
def test_codex_executable_accepts_exact_package_binary_bound(tmp_path: Path) -> None:
    """Accept the inclusive 512 MiB package-wide executable boundary."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_exact_binary_bound")
    target = tmp_path / "codex-target"
    with target.open("wb") as stream:
        stream.truncate(module.MAX_BINARY_BYTES)
    target.chmod(0o700)
    link = tmp_path / "codex"
    link.symlink_to(target)
    before = tuple(
        (path.lstat().st_mode, path.lstat().st_ino, path.lstat().st_nlink, path.lstat().st_size)
        for path in (target, link)
    )

    canonical, digest = module._digest_regular_executable(link, "Codex executable")

    assert canonical == target.resolve()
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert (
        tuple(
            (path.lstat().st_mode, path.lstat().st_ino, path.lstat().st_nlink, path.lstat().st_size)
            for path in (target, link)
        )
        == before
    )


@_skip_windows_posix
def test_oversized_codex_executable_reports_observed_size_and_limit(tmp_path: Path) -> None:
    """Explain the exact bounded-file invariant when an executable is too large."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_oversized_codex")
    target = tmp_path / "codex"
    with target.open("wb") as stream:
        stream.truncate(module.MAX_BINARY_BYTES + 1)
    target.chmod(0o700)

    with pytest.raises(
        ValueError,
        match=(
            rf"Codex executable exceeds {module.MAX_BINARY_BYTES}-byte safety limit: "
            rf"{target} has {module.MAX_BINARY_BYTES + 1} bytes"
        ),
    ):
        module._digest_regular_executable(target, "Codex executable")


@_skip_windows_posix
def test_wrong_approval_digest_causes_zero_writes(tmp_path: Path) -> None:
    """Refuse mutation authority before creating the coordination lock or roots."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_wrong_approval")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    plan = module.plan_mutation(
        action=module.ManagerAction.INSTALL,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=_executable(tmp_path),
        require_active_package=False,
        install_id="123e4567-e89b-42d3-a456-426614174001",
        transaction_nonce="123e4567-e89b-42d3-a456-426614174005",
    )
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="approval digest mismatch"):
        module.apply_mutation(plan, "f" * 64)

    assert _snapshot(tmp_path) == before


@_skip_windows_posix
def test_under_lock_drift_preserves_concurrent_foreign_target(tmp_path: Path) -> None:
    """Stop before transaction creation when target evidence changes after approval."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_drift")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    plan = module.plan_mutation(
        action=module.ManagerAction.INSTALL,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=_executable(tmp_path),
        require_active_package=False,
        install_id="123e4567-e89b-42d3-a456-426614174001",
        transaction_nonce="123e4567-e89b-42d3-a456-426614174006",
    )
    agents = home / "agents"
    agents.mkdir(mode=0o700)
    foreign = agents / "codex-rig-challenger.toml"
    foreign.write_bytes(b"foreign\n")
    foreign.chmod(0o600)

    with pytest.raises(ValueError, match="under-lock filesystem observation changed|candidate changed"):
        module.apply_mutation(plan, plan.approval.digest)

    assert foreign.read_bytes() == b"foreign\n"
    assert not (home / "codex-rig").exists()


@_skip_windows_posix
def test_active_package_probe_uses_disposable_home_copy(tmp_path: Path) -> None:
    """Prove the Codex CLI cannot create temp state in the real diagnostic home."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_active_sandbox")
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
    version = manifest["version"]
    home = tmp_path / "home"
    plugin = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    plugin.parent.mkdir(parents=True, mode=0o700)
    shutil.copytree(PLUGIN_ROOT, plugin)
    (home / "config.toml").write_text(
        '[marketplaces.borda-ai-rig]\nsource_type = "local"\nsource = "/fixture"\n\n'
        '[plugins."codex-rig@borda-ai-rig"]\nenabled = true\n'
    )
    (home / "config.toml").chmod(0o600)
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
    codex = tmp_path / "codex"
    codex.write_text(f"#!/bin/sh\nprintf '%s' '{payload}'\n")
    codex.chmod(0o700)
    before = _snapshot(home)

    result = module.diagnose(
        action=module.ManagerAction.DOCTOR,
        codex_home=home,
        plugin_root=plugin,
        codex_binary=codex,
        check_active_package=True,
    )

    assert result.classification == "healthy"
    assert result.checks["active_package"].status == "pass"
    assert _snapshot(home) == before


@_skip_windows_posix
def test_killed_process_requires_approved_rollback_then_can_resume(tmp_path: Path) -> None:
    """Recover an unjournaled publication after process death and converge later."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_process_kill")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    codex = _executable(tmp_path)
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
    install_id="123e4567-e89b-42d3-a456-426614174001",
    transaction_nonce="123e4567-e89b-42d3-a456-426614174007",
)
def kill(boundary):
    if boundary == "challenger:published":
        os._exit(97)
manager.apply_mutation(plan, plan.approval.digest, checkpoint=kill)
"""

    killed = subprocess.run([sys.executable, "-c", child], check=False, timeout=30)

    assert killed.returncode == 97
    recovery = module.plan_recovery(action=module.ManagerAction.INSTALL, codex_home=home, plugin_root=PLUGIN_ROOT)
    assert recovery is not None
    assert recovery.journal.journal_state == "MUTATING"
    terminal = module.apply_recovery(recovery, recovery.digest)
    assert terminal.journal_state == "ROLLED_BACK"
    assert list((home / "agents").glob("codex-rig-*.toml")) == []
    assert module.plan_recovery(action=module.ManagerAction.INSTALL, codex_home=home, plugin_root=PLUGIN_ROOT) is None

    resumed = module.plan_mutation(
        action=module.ManagerAction.INSTALL,
        codex_home=home,
        plugin_root=PLUGIN_ROOT,
        codex_binary=codex,
        require_active_package=False,
        install_id="123e4567-e89b-42d3-a456-426614174001",
        transaction_nonce="123e4567-e89b-42d3-a456-426614174008",
    )
    committed = module.apply_mutation(resumed, resumed.approval.digest)
    assert committed.journal_state == "COMMITTED"
    assert len(list((home / "agents").glob("codex-rig-*.toml"))) == 15


@_skip_windows_posix
def test_killed_state_commit_is_approved_and_finalized(tmp_path: Path) -> None:
    """Finalize exact installed state when process death follows its durable commit."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_finalize_kill")
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    codex = _executable(tmp_path)
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
    install_id="123e4567-e89b-42d3-a456-426614174001",
    transaction_nonce="123e4567-e89b-42d3-a456-426614174009",
)
def kill(boundary):
    if boundary == "journal:state-committed":
        os._exit(98)
manager.apply_mutation(plan, plan.approval.digest, checkpoint=kill)
"""

    killed = subprocess.run([sys.executable, "-c", child], check=False, timeout=30)

    assert killed.returncode == 98
    recovery = module.plan_recovery(action=module.ManagerAction.INSTALL, codex_home=home, plugin_root=PLUGIN_ROOT)
    assert recovery.journal.journal_state == "STATE_COMMITTED"
    terminal = module.apply_recovery(recovery, recovery.digest)
    assert terminal.journal_state == "COMMITTED"
    assert len(list((home / "agents").glob("codex-rig-*.toml"))) == 15
    assert '"transaction_status":"current"' in (home / "codex-rig" / "shims" / "state.json").read_text()


@_skip_windows_posix
def test_partial_initial_journal_cleanup_requires_exact_approval(tmp_path: Path) -> None:
    """Clean the sole pre-authority artifact without parsing or target writes."""
    module = _load_module(MANAGER_PATH, "codex_rig_manager_preparing_cleanup")
    home = tmp_path / "home"
    transaction = home / "codex-rig" / "shims" / "transactions" / "123e4567-e89b-42d3-a456-426614174010"
    transaction.mkdir(parents=True, mode=0o700)
    for path in (home, home / "codex-rig", home / "codex-rig" / "shims", transaction.parent, transaction):
        path.chmod(0o700)
    initial = transaction / "journal.initial.json"
    initial.write_bytes(b'{"partial"')
    initial.chmod(0o600)

    recovery = module.plan_recovery(action=module.ManagerAction.INSTALL, codex_home=home, plugin_root=PLUGIN_ROOT)

    assert recovery.journal is None
    assert module.apply_recovery(recovery, recovery.digest) is None
    assert not transaction.exists()
    assert not (home / "agents").exists()
