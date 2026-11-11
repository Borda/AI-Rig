"""Regression coverage for executable input and filesystem boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import compaction_boundary as cb
import find_polluter
import get_plugin_install_path as gip
import make_run_dir as mrd
import plugin_layout
import pytest
from _hook_env import _symlinks_supported

_requires_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


@pytest.mark.integration
def test_compaction_does_not_execute_source_tree_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest in the current checkout cannot authorize execution of its gate."""
    home = tmp_path / "home"
    home.mkdir()
    plugin = tmp_path / "plugins" / "cc_foundry"
    (plugin / "bin").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    marker = tmp_path / "executed"
    (plugin / "bin" / "compact_gate.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert cb.run_gate() is False
    assert not marker.exists()


@pytest.mark.parametrize("absolute", [False, True])
def test_polluter_rejects_victim_outside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], absolute: bool
) -> None:
    """Victim paths outside the project are refused before pytest can load their conftest."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    victim = str(tmp_path / "test_outside.py") if absolute else "../test_outside.py"
    monkeypatch.setattr(find_polluter, "_resolve_pytest_cmd", lambda: None)
    assert find_polluter.main([victim + "::test_case"]) == 1
    assert "failing_test must be within project root" in capsys.readouterr().err


@_requires_symlink
def test_polluter_rejects_victim_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A project-local link to an external test is subject to the same containment check."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_link.py").symlink_to(tmp_path / "test_outside.py")
    monkeypatch.chdir(project)
    monkeypatch.setattr(find_polluter, "_resolve_pytest_cmd", lambda: None)
    assert find_polluter.main(["test_link.py::test_case"]) == 1
    assert "failing_test must be within project root" in capsys.readouterr().err


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param([None, {"installPath": "/valid", "installedAt": "2026"}], id="non-object-entry"),
        pytest.param(
            [{"installPath": "/invalid", "installedAt": 7}, {"installPath": "/valid", "installedAt": "2026"}],
            id="non-string-timestamp",
        ),
        pytest.param(
            [{"installPath": ["invalid"], "installedAt": "2099"}, {"installPath": "/valid"}],
            id="non-string-path",
        ),
    ],
)
def test_registry_ignores_malformed_entries(tmp_path: Path, entries: list[object]) -> None:
    """Malformed registry entries neither crash sorting nor shadow a usable installation."""
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"plugins": {"foundry@market": entries}}), encoding="utf-8")
    assert gip.resolve_install_path(registry, "market", "foundry") == "/valid"


def test_registry_invalid_utf8_is_unavailable(tmp_path: Path) -> None:
    """An undecodable registry follows the missing-installation path instead of raising."""
    registry = tmp_path / "registry.json"
    registry.write_bytes(b"\xff")
    assert gip.resolve_install_path(registry, "market", "foundry") is None


@_requires_symlink
def test_relative_run_base_cannot_escape_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative output-base link must not create even an empty directory outside the project."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".reports").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(project)
    with pytest.raises(ValueError, match="outside project root"):
        mrd.make_run_dir(".reports")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("value", ["x\nPLUGIN_LAYOUT: forged", "x | agents=forged", "a/../b", "../x", "/abs"])
def test_layout_rejects_injected_or_escaping_directory(value: str) -> None:
    """Manifest directory fields cannot introduce output records or escape the plugin."""
    with pytest.raises(ValueError, match="skills"):
        plugin_layout.declared_dir({"skills": value}, "skills", "skills")


@pytest.mark.parametrize("value", ["./nested/skills/", ".hidden/skills", "a..b/skills"])
def test_layout_preserves_safe_relative_directory(value: str) -> None:
    """Safe dot-prefixed and nested directory names survive normalization."""
    assert plugin_layout.declared_dir({"skills": value}, "skills", "skills") == value.removeprefix("./").rstrip("/")
