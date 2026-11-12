"""Exercise selective setup cleanup without deleting user hook commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import settings_json


@pytest.fixture
def hook_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Provide settings and a marked foundry cache installation with two shipped hooks."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = tmp_path / ".claude/plugins/cache/market/foundry/2.0.0"
    (root / "hooks").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    (root / "hooks/statusline.js").touch()
    (root / "hooks/rule-inject.js").touch()
    (root / "hooks/hooks.json").write_text('{"hooks":{}}', encoding="utf-8")
    settings = tmp_path / ".claude/settings.json"
    settings.write_text('{"theme":"dark"}', encoding="utf-8")
    return settings, root


@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_hook_filter_preserves_user_commands_and_matcher_metadata(
    hook_settings: tuple[Path, Path], mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a hook executed from the installed foundry lineage is eligible for removal."""
    settings, root = hook_settings
    owned = {"type": "command", "command": f'node "{root.parent / "1.0.0/hooks/rule-inject.js"}"'}
    user = {"type": "command", "command": 'node "/user/hooks/rule-inject.js"'}
    group = {"matcher": "Write", "timeout": 7, "hooks": [owned, user]}
    original = json.dumps({"theme": "dark", "hooks": {"PostToolUse": [group]}})
    settings.write_text(original, encoding="utf-8")
    assert settings_json.main(["filter-foundry-hooks", str(settings), "--root", str(root), mode]) == 0
    expected = (
        original
        if mode == "--dry-run"
        else json.dumps({"theme": "dark", "hooks": {"PostToolUse": [{**group, "hooks": [user]}]}}, indent=2) + "\n"
    )
    assert settings.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().out.strip() == ("foundry: 1, user: 1" if mode == "--dry-run" else "removed: 1, kept: 1")


def test_hook_filter_keeps_unrecognized_shapes(hook_settings: tuple[Path, Path]) -> None:
    """Malformed user groups and event values remain untouched while owned groups are removed."""
    settings, root = hook_settings
    events = {
        "PostToolUse": [{"matcher": "Write", "hooks": [{"command": f'node "{root}/hooks/rule-inject.js"'}]}],
        "OtherEvent": [None, "user-value", {"matcher": "Read", "hooks": None}, {"hooks": []}],
        "CustomEvent": "keep-me",
    }
    settings.write_text(json.dumps({"hooks": events}), encoding="utf-8")
    assert settings_json.main(["filter-foundry-hooks", str(settings), "--root", str(root), "--apply"]) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "hooks": {"OtherEvent": events["OtherEvent"], "CustomEvent": "keep-me"}
    }


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param('node "{root}/hooks/statusline.js"', 0, id="current-root"),
        pytest.param('node "{lineage}/1.0.0/hooks/statusline.js"', 0, id="previous-version"),
        pytest.param('node "$HOME/.claude/hooks/statusline.js"', 0, id="legacy-home"),
        pytest.param('node "${{CLAUDE_PLUGIN_ROOT}}/hooks/statusline.js"', 0, id="plugin-placeholder"),
        pytest.param('node "{lineage}/../other/1.0.0/hooks/statusline.js"', 1, id="another-plugin"),
        pytest.param('echo "{root}/hooks/statusline.js"', 1, id="not-executed"),
        pytest.param('node "{root}/hooks/user-script.js"', 1, id="unshipped-basename"),
    ],
)
def test_statusline_ownership_requires_executed_shipped_path(
    hook_settings: tuple[Path, Path], command: str, expected: int
) -> None:
    """Ownership follows the executed script path rather than a basename or substring match."""
    settings, root = hook_settings
    value = command.format(root=root, lineage=root.parent)
    original = json.dumps({"statusLine": {"command": value}})
    settings.write_text(original, encoding="utf-8")
    assert settings_json.main(["statusline-owned", str(settings), "--root", str(root)]) == expected
    assert settings.read_text(encoding="utf-8") == original


def test_hook_filter_refuses_unmarked_root(hook_settings: tuple[Path, Path]) -> None:
    """A directory without the foundry manifest cannot authorize hook deletion."""
    settings, root = hook_settings
    (root / ".claude-plugin/plugin.json").write_text('{"name":"other"}', encoding="utf-8")
    original = settings.read_bytes()
    assert settings_json.main(["filter-foundry-hooks", str(settings), "--root", str(root), "--apply"]) == 2
    assert settings.read_bytes() == original


def test_hook_filter_noop_preserves_file_identity(hook_settings: tuple[Path, Path]) -> None:
    """Applying an empty cleanup preserves bytes, timestamp and file identity."""
    settings, root = hook_settings
    original = settings.read_bytes()
    before = settings.stat()
    assert settings_json.main(["filter-foundry-hooks", str(settings), "--root", str(root), "--apply"]) == 0
    after = settings.stat()
    assert (settings.read_bytes(), after.st_mtime_ns, after.st_ino) == (original, before.st_mtime_ns, before.st_ino)


def test_hook_filter_recognizes_exact_shipped_wrapper(hook_settings: tuple[Path, Path]) -> None:
    """The trusted manifest's complete launcher wrapper is eligible without accepting an appended user command."""
    settings, root = hook_settings
    wrapper = 'for n in node; do exec "$n" "${CLAUDE_PLUGIN_ROOT}/hooks/rule-inject.js"; done; exit 0'
    owned = {"type": "command", "command": wrapper}
    user = {"type": "command", "command": wrapper + "; echo user-task"}
    (root / "hooks/hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [owned]}]}}), encoding="utf-8"
    )
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [owned, user]}]}}), encoding="utf-8")
    assert settings_json.main(["filter-foundry-hooks", str(settings), "--root", str(root), "--apply"]) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {"hooks": {"SessionStart": [{"hooks": [user]}]}}


def test_hook_filter_consumes_real_shipped_manifest(hook_settings: tuple[Path, Path]) -> None:
    """Setup can remove its actual shipped launcher wrappers using the current plugin's manifest."""
    settings, _ = hook_settings
    root = Path(settings_json.__file__).resolve().parent.parent
    manifest = json.loads((root / "hooks/hooks.json").read_text(encoding="utf-8"))
    settings.write_text(json.dumps({"theme": "dark", "hooks": manifest["hooks"]}), encoding="utf-8")
    assert settings_json.main(["filter-foundry-hooks", str(settings), "--root", str(root), "--apply"]) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark"}


def test_delete_hooks_command_is_retired(hook_settings: tuple[Path, Path]) -> None:
    """The setup API cannot delete all hook registrations through the obsolete command."""
    settings, _ = hook_settings
    original = settings.read_bytes()
    with pytest.raises(SystemExit, match="2"):
        settings_json.main(["delete-hooks", str(settings)])
    assert settings.read_bytes() == original
