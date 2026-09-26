"""Tests for the exact installed-and-enabled bridge selector check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_bridge  # noqa: E402


def _write_manifest(home: Path) -> None:
    """Write a registry manifest containing an installed entry for the target selector."""
    manifest = home / ".claude" / "plugins" / "installed_plugins.json"
    manifest.parent.mkdir(parents=True)
    entry = {"scope": "user", "installPath": str(home / "cache" / "bridge" / "0.2.0"), "version": "0.2.0"}
    manifest.write_text(json.dumps({"plugins": {check_bridge.TARGET_SELECTOR: [entry]}}), encoding="utf-8")


class TestBridgeStatus:
    """Bridge status resolution keeps absence distinct from disablement."""

    def test_absent_when_no_exact_target_is_installed(self, tmp_path: Path) -> None:
        """An unrelated plugin and the Codex CLI never qualify as the bridge."""
        manifest = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"other@borda-ai-rig": []}), encoding="utf-8")
        assert check_bridge.bridge_status(tmp_path) == "absent"

    def test_available_from_exact_registry_selector(self, tmp_path: Path) -> None:
        """The bridge registry selector makes the enabled target available."""
        _write_manifest(tmp_path)
        assert check_bridge.bridge_status(tmp_path) == "available"
        assert check_bridge.bridge_available(tmp_path) is True

    def test_flat_registry_shape_does_not_false_positive(self, tmp_path: Path) -> None:
        """Reject the obsolete flat fixture shape that hid the real registry contract."""
        manifest = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({check_bridge.TARGET_SELECTOR: []}), encoding="utf-8")
        assert check_bridge.bridge_status(tmp_path) == "absent"

    def test_registry_entry_without_install_path_is_absent(self, tmp_path: Path) -> None:
        """A selector key with no installed entry must not read as installed.

        The root ``Makefile`` only treats a plugin as installed when an entry carries ``installPath``. A checker that
        accepted the bare key would tell a skill the bridge is available while the installer that placed it there
        disagrees.
        """
        manifest = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"plugins": {check_bridge.TARGET_SELECTOR: []}}), encoding="utf-8")
        assert check_bridge.bridge_status(tmp_path) == "absent"

    def test_available_from_marketplace_cache(self, tmp_path: Path) -> None:
        """The exact marketplace cache layout answers only when no registry exists.

        A machine whose registry file has not been written yet still has a usable bridge on disk, so the cache is the
        fallback for that one case.
        """
        (tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / check_bridge.TARGET_PLUGIN / "0.1.0").mkdir(
            parents=True
        )
        assert check_bridge.bridge_status(tmp_path) == "available"

    def test_stale_cache_never_overrides_a_readable_registry(self, tmp_path: Path) -> None:
        """Leftover cache directories from an uninstall must not report available.

        ``claude plugin uninstall`` drops the registry entry but leaves the version directory behind — real machines
        accumulate several per plugin. Consulting the cache alongside a readable registry would dispatch skills into an
        uninstalled plugin for as long as those directories survive.
        """
        manifest = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"plugins": {"other@borda-ai-rig": []}}), encoding="utf-8")
        (tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / check_bridge.TARGET_PLUGIN / "0.1.0").mkdir(
            parents=True
        )
        assert check_bridge.bridge_status(tmp_path) == "absent"

    def test_disabled_overrides_installed_target(self, tmp_path: Path) -> None:
        """An explicit local opt-out reports disabled rather than available."""
        _write_manifest(tmp_path)
        settings = tmp_path / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {check_bridge.TARGET_SELECTOR: False}}), encoding="utf-8")
        assert check_bridge.bridge_status(tmp_path, tmp_path) == "disabled"
        assert check_bridge.bridge_available(tmp_path, tmp_path) is False


class TestMain:
    """CLI output exposes both branchable and diagnostic forms."""

    def test_status_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The status flag reports the reason a bridge dispatch cannot run."""
        monkeypatch.setattr(check_bridge.Path, "home", classmethod(lambda _cls: tmp_path))
        assert check_bridge.main(["--status"]) == 0
        assert capsys.readouterr().out.strip() == "absent"

    def test_boolean_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Default output remains a simple shell-friendly boolean."""
        _write_manifest(tmp_path)
        monkeypatch.setattr(check_bridge.Path, "home", classmethod(lambda _cls: tmp_path))
        assert check_bridge.main([]) == 0
        assert capsys.readouterr().out.strip() == "true"
