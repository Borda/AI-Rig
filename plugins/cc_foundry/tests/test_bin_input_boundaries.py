"""Exercise registry, resolver and cleanup boundaries with real local files."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest
import remove_hook_from_registry as registry
import resolve_shared_path as resolver
import settings_json
import symlink_with_guard as links
from _hook_env import _symlinks_supported

_requires_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")
_REGISTRY = {"env": {"secret": "private"}, "hooks": {"PreToolUse": [{"hooks": [{"command": "foo.js"}]}]}}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the permitted project distinct from sibling files."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


@_requires_symlink
def test_hook_rewrite_does_not_follow_predictable_temporary_link(project: Path) -> None:
    """A planted .tmp link neither receives secrets nor replaces settings with a symlink."""
    target = project / "settings.json"
    target.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    victim = project / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    (project / "settings.json.tmp").symlink_to(victim)

    assert registry.run(target, "foo", r"foo\.js") == 0
    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8"))["hooks"] == {}


def test_hook_rewrite_keeps_private_file_mode(project: Path) -> None:
    """Replacement retains the target's actual mode, including private POSIX mode bits."""
    target = project / "settings.json"
    target.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    target.chmod(0o600)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    assert registry.run(target, "foo", r"foo\.js") == 0
    assert stat.S_IMODE(target.stat().st_mode) == original_mode


@pytest.mark.parametrize("filename", ["settings.json", "hooks.json", "settings.local.json"])
def test_hook_removal_refuses_registry_outside_project(project: Path, filename: str) -> None:
    """A known registry basename does not authorize writing a sibling project."""
    outside = project.parent / filename
    raw = json.dumps(_REGISTRY)
    outside.write_text(raw, encoding="utf-8")

    assert registry.run(outside, "foo", "foo") == 3
    assert outside.read_text(encoding="utf-8") == raw


def test_hook_removal_refuses_unrelated_json(project: Path) -> None:
    """An arbitrary project JSON file remains byte-for-byte unchanged."""
    target = project / "package.json"
    raw = '{"name":"example"}'
    target.write_text(raw, encoding="utf-8")

    assert registry.run(target, "foo", "foo") == 3
    assert target.read_text(encoding="utf-8") == raw


def test_hookless_settings_are_not_rewritten(project: Path) -> None:
    """A valid settings object without hooks receives neither a hooks key nor formatting changes."""
    target = project / "settings.json"
    raw = '{"env":{"x":"y"}}'
    target.write_text(raw, encoding="utf-8")

    assert registry.run(target, "foo", "foo") == 0
    assert target.read_text(encoding="utf-8") == raw
    assert registry.filter_hooks({"env": {}}, re.compile("foo")) == {"env": {}}


def test_hook_pattern_length_is_bounded(project: Path) -> None:
    """Oversized regular expressions fail before registry matching or writing."""
    target = project / "settings.json"
    raw = json.dumps(_REGISTRY)
    target.write_text(raw, encoding="utf-8")

    assert registry.run(target, "foo", "x" * 4097) == 3
    assert target.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("plugin", ["foundry", "oss"])
def test_environment_root_cannot_execute_checkout_helper(project: Path, plugin: str) -> None:
    """A matching manifest outside the cache authorizes neither tier zero nor helper execution."""
    root = project / "checkout"
    (root / "skills/_shared").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    (root / "bin").mkdir()
    marker = project / "executed"
    (root / "bin/get_plugin_install_path.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n", encoding="utf-8"
    )

    path, tier = resolver.resolve(plugin, "skills/_shared", home=project, env_root=str(root))

    assert not marker.exists()
    assert tier == -1
    assert Path(path) != root / "skills/_shared"


@pytest.mark.parametrize("model", ["arbitrary-model", "opus\nsonnet", "--danger", "https://example.org/model"])
def test_advisor_refuses_unrecognized_model_without_write(project: Path, model: str) -> None:
    """Repository values outside supported Claude model forms cannot enter user settings."""
    target = project / "settings.json"
    raw = '{"theme":"dark"}'
    target.write_text(raw, encoding="utf-8")

    assert settings_json.main(["set-advisor", str(target), "--model=" + model]) == 2
    assert target.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    "model", ["opus", "sonnet", "haiku", "fable", "best", "opusplan", "opus[1m]", "sonnet[1m]", "claude-fable-5-1"]
)
def test_advisor_accepts_supported_aliases_and_family_ids(project: Path, model: str) -> None:
    """Known model aliases, context variants and Claude family IDs retain exact writeback."""
    target = project / "settings.json"
    target.write_text('{"theme":"dark"}', encoding="utf-8")

    assert settings_json.main(["set-advisor", str(target), "--model", model]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"theme": "dark", "advisorModel": model}


@_requires_symlink
@pytest.mark.parametrize("scope", ["skills", "agents"])
def test_cleanup_preserves_foreign_links_with_marketplace_marker(project: Path, scope: str) -> None:
    """A user's own target containing the marketplace name is not owned by foundry."""
    home = project / "home"
    plugin = home / ".claude/plugins/cache/borda-ai-rig/foundry/1.0.0"
    plugin.mkdir(parents=True)
    destination = home / ".claude" / scope / "custom"
    destination.parent.mkdir(parents=True)
    foreign = project / "my-borda-ai-rig/foundry/0.1.0" / scope / "custom"
    destination.symlink_to(foreign)

    assert links.cleanup(plugin, home, "borda-ai-rig/foundry/") == []
    assert destination.is_symlink()
