"""Tests for check_install_state bin script.

Covers each install sub-check against a fixture home directory: registry shapes for I1, the four settings conditions for
I2, and both link expectations for I3.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_install_state as cis

_GOOD_SETTINGS = {
    "statusLine": {"command": "node /x/statusline.js"},
    "permissions": {"allow": [f"Bash(cmd{i}:*)" for i in range(11)]},
    "enabledPlugins": {"bridge@borda-ai-rig": True},
}


def _write_settings(home: Path, settings: dict) -> None:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings), encoding="utf-8")


def _write_registry(home: Path, payload: dict) -> None:
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestCheckCache:
    """Covers Check I1."""

    def test_nested_registry_shape_resolves(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The real `{"plugins": {id: [record]}}` shape is understood."""
        cache = tmp_path / "cache" / "foundry"
        cache.mkdir(parents=True)
        _write_registry(
            tmp_path,
            {"plugins": {"foundry@borda-ai-rig": [{"installPath": str(cache), "version": "0.54.1"}]}},
        )
        assert cis.check_cache(tmp_path) == 0
        assert "✓: Check I1 — foundry cache intact" in capsys.readouterr().out

    def test_flat_registry_shape_still_resolves(self, tmp_path: Path) -> None:
        """A flat `{id: record}` registry is accepted too."""
        cache = tmp_path / "cache" / "foundry"
        cache.mkdir(parents=True)
        _write_registry(tmp_path, {"foundry@borda-ai-rig": {"installPath": str(cache), "version": "1"}})
        assert cis.check_cache(tmp_path) == 0

    def test_missing_registry_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No installed_plugins.json is a HIGH finding."""
        assert cis.check_cache(tmp_path) == 1
        assert "installed_plugins.json not found" in capsys.readouterr().out

    def test_foundry_absent_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A registry without a foundry entry is a HIGH finding with an install hint."""
        _write_registry(tmp_path, {"plugins": {"other@x": [{"installPath": "/nope"}]}})
        assert cis.check_cache(tmp_path) == 1
        assert "foundry not found in installed_plugins.json" in capsys.readouterr().out

    def test_cache_dir_missing_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A registered plugin whose cache directory is gone is a HIGH finding."""
        _write_registry(
            tmp_path,
            {"plugins": {"foundry@borda-ai-rig": [{"installPath": str(tmp_path / "gone"), "version": "1"}]}},
        )
        assert cis.check_cache(tmp_path) == 1
        assert "install cache missing" in capsys.readouterr().out


class TestCheckSettings:
    """Covers Check I2."""

    def test_complete_merge_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """All four conditions satisfied reports a complete merge."""
        _write_settings(tmp_path, _GOOD_SETTINGS)
        assert cis.check_settings(tmp_path) == 0
        assert "✓: Check I2 — settings merge complete" in capsys.readouterr().out

    def test_missing_settings_file_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No settings.json at all is a HIGH finding."""
        assert cis.check_settings(tmp_path) == 1
        assert "settings.json not found" in capsys.readouterr().out

    def test_statusline_missing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A statusLine not pointing at statusline.js is an I2a finding."""
        settings = {**_GOOD_SETTINGS, "statusLine": {"command": "echo hi"}}
        _write_settings(tmp_path, settings)
        assert cis.check_settings(tmp_path) == 1
        assert "Check I2a — statusLine not set" in capsys.readouterr().out

    def test_short_allow_list(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Ten or fewer allow entries reads as an unmerged list."""
        settings = {**_GOOD_SETTINGS, "permissions": {"allow": ["Bash(ls:*)"]}}
        _write_settings(tmp_path, settings)
        assert cis.check_settings(tmp_path) == 1
        assert "Check I2b" in capsys.readouterr().out

    def test_bridge_not_enabled(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The bridge plugin not set to true is an I2c finding."""
        settings = {**_GOOD_SETTINGS, "enabledPlugins": {"bridge@borda-ai-rig": False}}
        _write_settings(tmp_path, settings)
        assert cis.check_settings(tmp_path) == 1
        assert "Check I2c" in capsys.readouterr().out

    def test_stale_hooks_block(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A top-level hooks key is an I2d finding with its own fix hint."""
        settings = {**_GOOD_SETTINGS, "hooks": {}}
        _write_settings(tmp_path, settings)
        assert cis.check_settings(tmp_path) == 1
        out = capsys.readouterr().out
        assert "Check I2d" in out
        assert "offer to remove the stale hooks block" in out

    def test_passing_subchecks_still_reported(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """One failing sub-check does not suppress the other three pass lines."""
        settings = {**_GOOD_SETTINGS, "statusLine": {"command": "echo"}}
        _write_settings(tmp_path, settings)
        cis.check_settings(tmp_path)
        out = capsys.readouterr().out
        assert "✓: Check I2b" in out
        assert "✓: Check I2c" in out
        assert "✓: Check I2d" in out


class TestCheckLinks:
    """Covers Check I3."""

    def test_no_links_skips_staleness(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An untouched home reports the skip line and passes."""
        assert cis.check_links(tmp_path) == 0
        assert "skipping staleness check" in capsys.readouterr().out

    def test_resolving_rule_links_pass(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A rules symlink that resolves is counted and passes."""
        rules = tmp_path / ".claude" / "rules"
        rules.mkdir(parents=True)
        real = tmp_path / "real.md"
        real.write_text("x", encoding="utf-8")
        (rules / "a.md").symlink_to(real)
        assert cis.check_links(tmp_path) == 0
        assert "1 rules/TEAM_PROTOCOL symlink(s) all resolve correctly" in capsys.readouterr().out

    def test_broken_rule_link_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A dangling rules symlink is a HIGH finding."""
        rules = tmp_path / ".claude" / "rules"
        rules.mkdir(parents=True)
        (rules / "a.md").symlink_to(tmp_path / "gone.md")
        assert cis.check_links(tmp_path) == 1
        out = capsys.readouterr().out
        assert "broken symlink" in out
        assert "1 of 1 symlink(s) broken" in out

    def test_foundry_agent_symlink_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A foundry symlink under agents/ must not exist."""
        agents = tmp_path / ".claude" / "agents"
        agents.mkdir(parents=True)
        target = tmp_path / "cache" / "borda-ai-rig" / "foundry" / "0.1" / "x.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        (agents / "x.md").symlink_to(target)
        assert cis.check_links(tmp_path) == 1
        out = capsys.readouterr().out
        assert "foundry symlink must not exist" in out
        assert "Step 10 Phase 1 purges" in out

    def test_foundry_target_with_windows_separators_is_high(self) -> None:
        """A Windows-form cache target remains Foundry-owned for link-health checks."""
        target = r"C:\Users\runner\.claude\plugins\cache\borda-ai-rig\foundry\0.1\agents\x.md"

        assert cis._is_foundry_target(target) is True

    def test_unrelated_symlink_ignored(self, tmp_path: Path) -> None:
        """A symlink to a non-foundry target under agents/ is not a finding."""
        agents = tmp_path / ".claude" / "agents"
        agents.mkdir(parents=True)
        target = tmp_path / "other.md"
        target.write_text("x", encoding="utf-8")
        (agents / "x.md").symlink_to(target)
        assert cis.check_links(tmp_path) == 0


class TestCli:
    """Covers the command-line contract."""

    def test_check_flag_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting --check is an argparse error."""
        monkeypatch.setattr("sys.argv", ["check_install_state.py"])
        with pytest.raises(SystemExit):
            cis.main()

    def test_dispatches_to_named_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--check I3 runs the link check against --home."""
        monkeypatch.setattr("sys.argv", ["check_install_state.py", "--check", "I3", "--home", str(tmp_path)])
        assert cis.main() == 0
        assert "Check I3" in capsys.readouterr().out
