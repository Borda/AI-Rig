"""Tests for check_plugin_layout bin script.

Covers each Check 8 sub-check in isolation against synthetic plugin trees, plus the CLI contract (skip when the plugin
directory is absent, exit code mapping).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_plugin_layout as cpl


def _make_plugin(root: Path, *, name: str = "foundry") -> Path:
    """Build a minimally valid plugin tree and return its directory."""
    plugin = root / "plugins" / "cc_foundry"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0", "description": "d"}), encoding="utf-8"
    )
    (plugin / "agents").mkdir()
    (plugin / "skills" / "setup").mkdir(parents=True)
    (plugin / "skills" / "setup" / "SKILL.md").write_text(
        "statusLine permissions.allow bridge@borda-ai-rig link", encoding="utf-8"
    )
    hooks = plugin / "hooks"
    hooks.mkdir()
    (hooks / "demo.js").write_text("// hook\n", encoding="utf-8")
    (hooks / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/demo.js"'}]}]}}),
        encoding="utf-8",
    )
    return plugin


class TestCheckManifest:
    """Covers Check 8a."""

    def test_valid_manifest_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A manifest with all required fields and the expected name reports no failure."""
        plugin = _make_plugin(tmp_path)
        assert cpl.check_manifest(plugin, "foundry") == 0
        assert "✓: Check 8a" in capsys.readouterr().out

    def test_missing_manifest_is_critical(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An absent plugin.json is reported as CRITICAL and counts as a failure."""
        assert cpl.check_manifest(tmp_path / "nope", "foundry") == 1
        assert "! CRITICAL: Check 8a — manifest not found" in capsys.readouterr().out

    def test_invalid_json_is_critical(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Unparsable JSON is reported as CRITICAL, not crashed on."""
        plugin = _make_plugin(tmp_path)
        (plugin / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
        assert cpl.check_manifest(plugin, "foundry") == 1
        assert "invalid JSON or missing required fields" in capsys.readouterr().out

    def test_missing_required_field_is_critical(self, tmp_path: Path) -> None:
        """A manifest without `description` fails even though it parses."""
        plugin = _make_plugin(tmp_path)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "foundry", "version": "0.1.0"}), encoding="utf-8"
        )
        assert cpl.check_manifest(plugin, "foundry") == 1

    def test_wrong_name_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A manifest naming a different plugin is reported as HIGH."""
        plugin = _make_plugin(tmp_path, name="other")
        assert cpl.check_manifest(plugin, "foundry") == 1
        assert "manifest name is 'other', expected 'foundry'" in capsys.readouterr().out


class TestCheckDirectories:
    """Covers Check 8b."""

    def test_real_dirs_pass(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Real agents/ and skills/ directories in the plugin report no failure."""
        plugin = _make_plugin(tmp_path)
        assert cpl.check_directories(plugin, tmp_path / ".claude") == 0
        assert "real directory" in capsys.readouterr().out

    def test_missing_dir_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing agents/ directory is one failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / "agents").rmdir()
        assert cpl.check_directories(plugin, tmp_path / ".claude") == 1
        assert "agents directory not found" in capsys.readouterr().out

    def test_real_agent_file_in_claude_dir_is_medium(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A real .md file under .claude/agents/ is flagged but is not a hard failure."""
        plugin = _make_plugin(tmp_path)
        claude = tmp_path / ".claude"
        (claude / "agents").mkdir(parents=True)
        (claude / "agents" / "x.md").write_text("x", encoding="utf-8")
        assert cpl.check_directories(plugin, claude) == 0
        assert "non-symlink .md file(s) in .claude/agents/" in capsys.readouterr().out


class TestCheckHooks:
    """Covers Check 8c and 8d."""

    def test_resolving_reference_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A hooks.json command pointing at an existing hook file passes."""
        plugin = _make_plugin(tmp_path)
        assert cpl.check_hooks(plugin) == 0
        assert "hooks.json references all resolve" in capsys.readouterr().out

    def test_dangling_reference_is_high(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A hooks.json command naming a missing file is one failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / "hooks" / "demo.js").unlink()
        assert cpl.check_hooks(plugin) == 1
        assert "references missing file: hooks/demo.js" in capsys.readouterr().out

    def test_missing_hooks_dir_is_high(self, tmp_path: Path) -> None:
        """No hooks/ directory at all is one failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / "hooks" / "demo.js").unlink()
        (plugin / "hooks" / "hooks.json").unlink()
        (plugin / "hooks").rmdir()
        assert cpl.check_hooks(plugin) == 1

    def test_hooks_json_valid(self, tmp_path: Path) -> None:
        """Valid hooks.json reports no failure."""
        assert cpl.check_hooks_json(_make_plugin(tmp_path)) == 0

    def test_hooks_json_malformed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Malformed hooks.json is reported rather than raising."""
        plugin = _make_plugin(tmp_path)
        (plugin / "hooks" / "hooks.json").write_text("{", encoding="utf-8")
        assert cpl.check_hooks_json(plugin) == 1
        assert "not valid JSON" in capsys.readouterr().out


class TestCheckPermissionsDrift:
    """Covers Check 8f."""

    def test_in_sync_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Identical allow lists report in-sync and no failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / ".claude-plugin" / "permissions-allow.json").write_text(json.dumps(["Bash(ls:*)"]), encoding="utf-8")
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}), encoding="utf-8")
        assert cpl.check_permissions_drift(plugin, claude) == 0
        assert "in sync" in capsys.readouterr().out

    def test_entry_missing_from_plugin_is_medium_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An allow entry only in settings.json is a MEDIUM finding and a failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / ".claude-plugin" / "permissions-allow.json").write_text(json.dumps([]), encoding="utf-8")
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}), encoding="utf-8")
        assert cpl.check_permissions_drift(plugin, claude) == 1
        assert "absent from permissions-allow.json" in capsys.readouterr().out

    def test_entry_missing_from_settings_is_low_not_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An allow entry only in the plugin is LOW and does not fail the check."""
        plugin = _make_plugin(tmp_path)
        (plugin / ".claude-plugin" / "permissions-allow.json").write_text(json.dumps(["Bash(ls:*)"]), encoding="utf-8")
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
        assert cpl.check_permissions_drift(plugin, claude) == 0
        assert "⚠ LOW: Check 8f" in capsys.readouterr().out

    def test_no_settings_file_skips(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A marketplace install with no .claude/settings.json skips the drift check."""
        plugin = _make_plugin(tmp_path)
        (plugin / ".claude-plugin" / "permissions-allow.json").write_text(json.dumps([]), encoding="utf-8")
        assert cpl.check_permissions_drift(plugin, tmp_path / ".claude") == 0
        assert "skipping drift check" in capsys.readouterr().out


class TestCheckCliValidate:
    """Covers Check 8e, the only sub-check that shells out."""

    def test_claude_absent_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No CLI on PATH is a skip, not a finding."""
        monkeypatch.setattr(cpl.shutil, "which", lambda _name: None)
        assert cpl.check_cli_validate(tmp_path, timeout=1.0) == 0

    def test_timeout_degrades_to_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hung `claude plugin validate` must not abort 8f and 8g.

        Letting TimeoutExpired propagate kills the process mid-run, so the two later sub-checks never execute and the
        audit gets a traceback instead of its issue count.
        """
        monkeypatch.setattr(cpl.shutil, "which", lambda _name: "/usr/bin/claude")

        def _hang(*_args: object, **_kwargs: object) -> None:
            raise cpl.subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

        monkeypatch.setattr(cpl.subprocess, "run", _hang)
        assert cpl.check_cli_validate(tmp_path, timeout=1.0) == 0
        assert "⚠ SKIPPED: Check 8e" in capsys.readouterr().out


class TestCheckSetupSkill:
    """Covers Check 8g."""

    def test_full_coverage_passes(self, tmp_path: Path) -> None:
        """A setup SKILL.md naming every required keyword passes."""
        assert cpl.check_setup_skill(_make_plugin(tmp_path), tmp_path / ".claude") == 0

    def test_missing_keyword_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A setup SKILL.md missing a required keyword is one failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / "skills" / "setup" / "SKILL.md").write_text("statusLine only", encoding="utf-8")
        assert cpl.check_setup_skill(plugin, tmp_path / ".claude") == 1
        assert "does not mention 'permissions.allow'" in capsys.readouterr().out

    def test_missing_setup_skill_is_high(self, tmp_path: Path) -> None:
        """An absent setup SKILL.md is one failure."""
        plugin = _make_plugin(tmp_path)
        (plugin / "skills" / "setup" / "SKILL.md").unlink()
        assert cpl.check_setup_skill(plugin, tmp_path / ".claude") == 1


class TestCli:
    """Covers the command-line contract."""

    def test_absent_plugin_dir_skips_with_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No plugin directory means the whole check is skipped, not failed."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["check_plugin_layout.py", "--plugin-dir", "plugins/absent"])
        assert cpl.main() == 0
        out = capsys.readouterr().out
        assert "⚠ SKIPPED: Check 8" in out

    def test_broken_plugin_reports_issue_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad manifest prints the issue count and still exits 0 (report-only)."""
        _make_plugin(tmp_path, name="other")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["check_plugin_layout.py", "--plugin-dir", "plugins/cc_foundry", "--expect-name", "foundry"],
        )
        monkeypatch.setattr(cpl.shutil, "which", lambda _name: None)
        assert cpl.main() == 0
        assert "✗: Check 8 —" in capsys.readouterr().out


class TestHookRefs:
    """Covers the hooks.json command parser."""

    def test_extracts_plugin_root_reference(self, tmp_path: Path) -> None:
        """A ${CLAUDE_PLUGIN_ROOT}/hooks/<name>.js command yields that basename."""
        hooks_json = tmp_path / "hooks.json"
        hooks_json.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/a.js"'}]}]}}),
            encoding="utf-8",
        )
        assert cpl._hook_refs(hooks_json) == {"a.js"}

    def test_unparseable_file_yields_empty_set(self, tmp_path: Path) -> None:
        """A malformed hooks.json yields no references rather than raising."""
        hooks_json = tmp_path / "hooks.json"
        hooks_json.write_text("{", encoding="utf-8")
        assert cpl._hook_refs(hooks_json) == set()
