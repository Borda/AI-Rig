"""Native-Windows acceptance checks for read-only shim diagnostics."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANAGER = PLUGIN_ROOT / "scripts" / "manage_role_agents.py"


def _load_windows_manager(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the manager through its Windows import boundary."""
    monkeypatch.setattr(sys, "platform", "win32")
    specification = importlib.util.spec_from_file_location("codex_rig_windows_diagnostics", MANAGER)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    return module


@pytest.mark.flaky(reruns=2, reruns_delay=1, condition=sys.platform == "win32")
def test_simulated_windows_doctor_verifies_package_and_inventories_shims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide useful zero-write Windows diagnostics without POSIX lifecycle imports."""
    module = _load_windows_manager(monkeypatch)
    plugin_root = tmp_path / "plugin"
    shutil.copytree(PLUGIN_ROOT, plugin_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    codex_home = tmp_path / "codex-home"
    agents = codex_home / "agents"
    agents.mkdir(parents=True)
    candidate = agents / "codex-rig-challenger.toml"
    candidate.write_text("legacy candidate\n", encoding="utf-8")
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"bounded executable\n")
    executable.chmod(0o755)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    result = module.diagnose(
        action=module.ManagerAction.STATUS,
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
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("action", ("install", "remove"))
def test_simulated_windows_mutation_remains_explicitly_blocked_without_writes(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep unsupported persistent-shim mutation separate from native diagnostics."""
    module = _load_windows_manager(monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    before = tuple(tmp_path.rglob("*"))

    assert module.main([action]) == 5

    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == action
    assert payload["classification"] == "platform-blocked"
    assert payload["writes"] == 0
    assert tuple(tmp_path.rglob("*")) == before
