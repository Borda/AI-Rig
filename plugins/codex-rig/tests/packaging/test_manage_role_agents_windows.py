"""Native-Windows acceptance checks for read-only shim diagnostics."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

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
