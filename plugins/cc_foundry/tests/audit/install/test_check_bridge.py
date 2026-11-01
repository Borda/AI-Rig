"""Tests for the exact installed-and-enabled bridge selector check."""

from __future__ import annotations

import json
from pathlib import Path

import check_bridge
import pytest


def _write_registry(home: Path, payload: dict) -> None:
    """Write ``payload`` as the plugin registry manifest under ``home``."""
    manifest = home / ".claude" / "plugins" / "installed_plugins.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(payload), encoding="utf-8")


def _write_manifest(home: Path) -> None:
    """Write a registry manifest containing an installed entry for the target selector."""
    entry = {"scope": "user", "installPath": str(home / "cache" / "bridge" / "0.2.0"), "version": "0.2.0"}
    _write_registry(home, {"plugins": {check_bridge.TARGET_SELECTOR: [entry]}})


def _make_cache_dir(home: Path) -> None:
    """Create the marketplace cache directory a bridge install leaves on disk."""
    (home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / check_bridge.TARGET_PLUGIN / "0.1.0").mkdir(parents=True)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty home directory that ``Path.home()`` resolves to for the CLI under test."""
    monkeypatch.setattr(check_bridge.Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path


class TestBridgeStatus:
    """Bridge status resolution keeps absence distinct from disablement."""

    @pytest.mark.parametrize(
        "registry",
        [
            # An unrelated plugin and the Codex CLI never qualify as the bridge.
            pytest.param({"other@borda-ai-rig": []}, id="unrelated-plugin-only"),
            # The obsolete flat fixture shape hid the real registry contract and must not false-positive.
            pytest.param({check_bridge.TARGET_SELECTOR: []}, id="obsolete-flat-registry-shape"),
            # The root Makefile only treats a plugin as installed when an entry carries installPath; a bare selector key
            # would tell a skill the bridge is available while the installer that placed it there disagrees.
            pytest.param({"plugins": {check_bridge.TARGET_SELECTOR: []}}, id="selector-without-install-entry"),
        ],
    )
    def test_absent_when_no_exact_target_is_installed(self, tmp_path: Path, registry: dict) -> None:
        """A registry with no installed entry for the exact bridge selector reports absent.

        Covers a registry listing only an unrelated plugin, the obsolete flat registry shape, and a selector key that
        carries no installed entry.
        """
        _write_registry(tmp_path, registry)

        assert check_bridge.bridge_status(tmp_path) == "absent"

    def test_registry_selector_is_available(self, tmp_path: Path) -> None:
        """An installed registry entry for the exact bridge selector makes the target available."""
        _write_manifest(tmp_path)

        assert check_bridge.bridge_status(tmp_path) == "available"
        assert check_bridge.bridge_available(tmp_path) is True

    def test_marketplace_cache_is_available_without_registry(self, tmp_path: Path) -> None:
        """The exact marketplace cache layout answers only when no registry exists.

        A machine whose registry file has not been written yet still has a usable bridge on disk, so the cache is the
        fallback for that one case.
        """
        _make_cache_dir(tmp_path)

        assert check_bridge.bridge_status(tmp_path) == "available"

    def test_stale_cache_never_overrides_a_readable_registry(self, tmp_path: Path) -> None:
        """Leftover cache directories from an uninstall must not report available.

        ``claude plugin uninstall`` drops the registry entry but leaves the version directory behind — real machines
        accumulate several per plugin. Consulting the cache alongside a readable registry would dispatch skills into an
        uninstalled plugin for as long as those directories survive.
        """
        _write_registry(tmp_path, {"plugins": {"other@borda-ai-rig": []}})
        _make_cache_dir(tmp_path)

        assert check_bridge.bridge_status(tmp_path) == "absent"

    def test_local_opt_out_overrides_installed_target(self, tmp_path: Path) -> None:
        """An explicit local opt-out reports disabled rather than available.

        The bridge is installed, but ``enabledPlugins`` sets its selector to false in the project settings, which wins.
        """
        _write_manifest(tmp_path)
        settings = tmp_path / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {check_bridge.TARGET_SELECTOR: False}}), encoding="utf-8")

        assert check_bridge.bridge_status(tmp_path, tmp_path) == "disabled"
        assert check_bridge.bridge_available(tmp_path, tmp_path) is False


class TestMarketplaceFromEnv:
    """The marketplace override becomes a cache directory component, so only a plain name is accepted."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("my-fork", "my-fork", id="plain-name"),
            pytest.param("Fork_2", "Fork_2", id="underscore-and-digits"),
            pytest.param(None, "borda-ai-rig", id="unset"),
            pytest.param("", "borda-ai-rig", id="empty"),
            pytest.param("../../elsewhere", "borda-ai-rig", id="parent-traversal"),
            pytest.param("a/b", "borda-ai-rig", id="path-separator"),
            pytest.param("..", "borda-ai-rig", id="dot-dot"),
            pytest.param("evil\n", "borda-ai-rig", id="trailing-newline"),
        ],
    )
    def test_only_plain_names_are_used(self, raw: str | None, expected: str) -> None:
        """A value that could leave the marketplace cache directory falls back to the default marketplace.

        Scenario: ``AI_RIG_MARKETPLACE=../../elsewhere`` would make the cache probe look at an arbitrary directory and
        report the bridge available; the default marketplace is used instead, as for an unset or empty value.
        """
        assert check_bridge._marketplace_from_env(raw) == expected


class TestMain:
    """CLI output exposes both branchable and diagnostic forms."""

    def test_status_flag_prints_reason(self, fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The status flag reports the reason a bridge dispatch cannot run.

        An empty home has no registry and no cache, so ``--status`` prints ``absent`` and still exits 0.
        """
        assert check_bridge.main(["--status"]) == 0
        assert capsys.readouterr().out.strip() == "absent"

    def test_default_output_is_shell_boolean(self, fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Default output remains a simple shell-friendly boolean.

        With the bridge installed in the registry, skills branching on the output read the literal ``true``.
        """
        _write_manifest(fake_home)

        assert check_bridge.main([]) == 0
        assert capsys.readouterr().out.strip() == "true"
