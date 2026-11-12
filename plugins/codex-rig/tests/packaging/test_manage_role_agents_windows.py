"""Native-Windows acceptance checks for read-only shim diagnostics."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from _fixture_files import tree_bytes
from _modules import load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
MANAGER = PLUGIN_ROOT / "scripts" / "manage_role_agents.py"


@pytest.fixture
def windows_manager(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the manager as if running on native Windows, through its Windows import boundary."""
    monkeypatch.setattr(sys, "platform", "win32")
    return load_module_scoped("codex_rig_windows_diagnostics", MANAGER, monkeypatch)


@pytest.fixture(params=["manage_role_agents.py", "sync_codex.py"])
def command_module(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load each owner of native Windows executable discovery."""
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "scripts"))
    return load_module_scoped("windows_command_discovery", PLUGIN_ROOT / "scripts" / request.param, monkeypatch)


@pytest.mark.parametrize("extensions", [".EXE;.CMD", ""])
def test_windows_path_lookup_expands_extensions_outside_current_directory(
    command_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extensions: str
) -> None:
    """Find batch shims with legacy which semantics while ignoring empty and explicit current-directory entries."""
    current, trusted = tmp_path / "current", tmp_path / "trusted"
    current.mkdir()
    trusted.mkdir()
    launcher = trusted / "codex.CMD"
    launcher.write_bytes(b"@exit /b 0\r\n")
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATHEXT", extensions)
    monkeypatch.setattr(command_module.os, "get_exec_path", lambda: ["", str(current), str(trusted)])
    # Before Python 3.12, which does not expand PATHEXT when its argument contains a directory.
    which = Mock(side_effect=lambda candidate: str(launcher) if Path(candidate) == launcher else None)
    monkeypatch.setattr(command_module.shutil, "which", which)

    assert command_module._windows_path_executable("codex") == str(launcher)
    assert which.call_args_list[0].args == (str(trusted / "codex"),)
    assert all(Path(call.args[0]).is_absolute() for call in which.call_args_list)
    assert all(Path(call.args[0]).parent != current for call in which.call_args_list)


def test_windows_path_lookup_rejects_launcher_redirected_to_current_directory(
    command_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a lookup result in the current directory outside the executable trust boundary."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(command_module.os, "get_exec_path", lambda: [str(trusted)])
    monkeypatch.setattr(command_module.shutil, "which", Mock(return_value=str(tmp_path / "codex.CMD")))

    assert command_module._windows_path_executable("codex") is None


class TestSimulatedWindows:
    """The manager's native-Windows surface, simulated on every host."""

    def test_doctor_verifies_package_and_inventories_shims(self, windows_manager: ModuleType, tmp_path: Path) -> None:
        """Windows diagnostics verify the package and inventory shims with zero writes and no POSIX lifecycle imports.

        A copied plugin, an empty executable stand-in and one legacy-looking agent file are diagnosed with the platform
        reported as win32; the candidate is inventoried but not adopted, and no file in the tree changes.
        """
        plugin_root = tmp_path / "plugin"
        shutil.copytree(PLUGIN_ROOT, plugin_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        codex_home = tmp_path / "codex-home"
        agents = codex_home / "agents"
        agents.mkdir(parents=True)
        (agents / "codex-rig-challenger.toml").write_text("legacy candidate\n", encoding="utf-8")
        executable = tmp_path / "codex.exe"
        executable.write_bytes(b"bounded executable\n")
        executable.chmod(0o755)
        before = tree_bytes(tmp_path)

        result = windows_manager.diagnose(
            action=windows_manager.ManagerAction.STATUS,
            codex_home=codex_home,
            plugin_root=plugin_root,
            codex_binary=executable,
            check_active_package=False,
        )

        assert result.classification == "degraded"
        assert result.plugin_version is not None
        assert result.checks["platform"].status == "pass"
        assert result.checks["package"].status == "pass"
        assert result.checks["filesystem"].status == "pass"
        assert result.checks["executables"].status == "pass"
        assert result.checks["active_package"].status == "degraded"
        assert result.state == "not-applicable"
        assert result.targets == "inventory-only"
        assert result.recovery == "not-applicable"
        assert result.namespace_candidates == ("codex-rig-challenger.toml",)
        assert tree_bytes(tmp_path) == before

    @pytest.mark.parametrize("action", ["install", "remove"])
    def test_mutation_stays_explicitly_blocked_without_writes(
        self,
        windows_manager: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        action: str,
    ) -> None:
        """Install and remove report platform-blocked with zero writes, separate from native diagnostics.

        Persistent shim mutation is unsupported on native Windows; the command must exit 5 with a machine-readable
        refusal and leave the (absent) Codex home absent.
        """
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
        before = sorted(tmp_path.rglob("*"))

        assert windows_manager.main([action]) == 5

        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == action
        assert payload["classification"] == "platform-blocked"
        assert payload["writes"] == 0
        assert sorted(tmp_path.rglob("*")) == before
