"""Exercise the shared resolver's trust and version selection boundaries."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import resolve_plugin_root as resolver


def _symlinks_supported() -> bool:
    """Probe actual symlink creation capability without assuming a host OS."""
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "target"
        target.touch()
        try:
            (Path(scratch) / "link").symlink_to(target)
        except OSError:
            return False
    return True


_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable")


def _install(home: Path, version: str, name: str = "foundry") -> Path:
    """Create the files a resolved plugin root must carry."""
    root = home / ".claude/plugins/cache/borda-ai-rig/foundry" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin/resolve_plugin_root.py").write_text("# resolver\n", encoding="utf-8")
    return root


def _registry(home: Path, records: list[dict[str, str]]) -> None:
    """Record explicit installations for the requested plugin."""
    target = home / ".claude/plugins/installed_plugins.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"plugins": {"foundry@borda-ai-rig": records}}), encoding="utf-8")


def test_registry_beats_newer_cache(tmp_path: Path) -> None:
    """The active install remains authoritative despite a newer cached version."""
    active = _install(tmp_path, "1.0.0")
    _install(tmp_path, "2.0.0")
    _registry(tmp_path, [{"installPath": str(active), "installedAt": "2026-01-01T00:00:00Z"}])
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == active.as_posix()


def test_stale_registry_falls_back_to_valid_cache(tmp_path: Path) -> None:
    """A purged install record does not prevent finding a usable cache root."""
    valid = _install(tmp_path, "1.0.0")
    missing = valid.parent / "2.0.0"
    _registry(tmp_path, [{"installPath": str(missing), "installedAt": "2026-01-01T00:00:00Z"}])
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == valid.as_posix()


def test_orphaned_registry_falls_back_with_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Discarding an orphaned recorded install keeps the version visible while selecting a live cache root."""
    orphan = _install(tmp_path, "2.0.0")
    (orphan / ".orphaned_at").touch()
    valid = _install(tmp_path, "1.0.0")
    _registry(tmp_path, [{"installPath": str(orphan), "installedAt": "2026-01-01T00:00:00Z"}])
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == valid.as_posix()
    diagnostic = capsys.readouterr()
    assert diagnostic.out == ""
    assert "orphaned" in diagnostic.err
    assert str(orphan) in diagnostic.err


@pytest.mark.parametrize("bad", ["9", "v9.0.0", "01.0.0", "1.0.0-01"])
def test_cache_ignores_non_semver(tmp_path: Path, bad: str) -> None:
    """Malformed version directories cannot outrank a release."""
    valid = _install(tmp_path, "1.0.0")
    _install(tmp_path, bad)
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == valid.as_posix()


def test_release_beats_prerelease(tmp_path: Path) -> None:
    """A release outranks its release candidate independently of creation order."""
    valid = _install(tmp_path, "1.0.0")
    _install(tmp_path, "1.0.0-rc.1")
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == valid.as_posix()


def test_registry_newest_valid_timestamp(tmp_path: Path) -> None:
    """Invalid timestamp text cannot sort ahead of an actual installation date."""
    active = _install(tmp_path, "1.0.0")
    other = _install(tmp_path, "2.0.0")
    _registry(
        tmp_path,
        [
            {"installPath": str(other), "installedAt": "zzz"},
            {"installPath": str(active), "installedAt": "2026-01-01T00:00:00Z"},
        ],
    )
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == active.as_posix()


def test_registry_wrong_marketplace_fails_closed(tmp_path: Path) -> None:
    """A matching manifest under another marketplace supplies no authority."""
    valid = _install(tmp_path, "1.0.0")
    foreign = tmp_path / ".claude/plugins/cache/foreign/foundry/1.0.0"
    foreign.parent.mkdir(parents=True)
    valid.rename(foreign)
    _registry(tmp_path, [{"installPath": str(foreign), "installedAt": "2026-01-01T00:00:00Z"}])
    with pytest.raises(ValueError, match=r"marketplace|cache"):
        resolver.resolve(tmp_path, "borda-ai-rig", "foundry")


def test_source_fallback_accepts_nested_own_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The executing resolver anchors its source tree even from a nested cwd."""
    root = tmp_path / "repo/plugins/cc_foundry"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    marketplace = tmp_path / "repo/.claude-plugin/marketplace.json"
    marketplace.parent.mkdir()
    marketplace.write_text('{"name":"borda-ai-rig"}', encoding="utf-8")
    script = root / "bin/resolve_plugin_root.py"
    script.parent.mkdir()
    script.write_text("# resolver\n", encoding="utf-8")
    nested = tmp_path / "repo/nested/project"
    nested.mkdir(parents=True)
    monkeypatch.setattr(resolver, "__file__", str(script))
    monkeypatch.chdir(nested)
    assert resolver.resolve(tmp_path / "home", "borda-ai-rig", "foundry") == root.as_posix()


def test_wrong_marketplace_cannot_select_source_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The executing checkout cannot satisfy an explicitly different marketplace selection."""
    checkout = Path(resolver.__file__).resolve().parents[3]
    monkeypatch.chdir(checkout)
    assert resolver.resolve(tmp_path, "foreign-marketplace", "foundry") is None


def test_copied_cwd_manifest_cannot_authorize_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign cwd cannot impersonate the executing resolver's checkout."""
    root = tmp_path / "plugins/cc_foundry"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolver.resolve(tmp_path / "home", "borda-ai-rig", "foundry") is None


@pytest.mark.parametrize("value", ["", "relative/poison", "/outside/poison"])
def test_environment_cannot_override_install_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An unset or hostile plugin-root environment cannot select a different install."""
    active = _install(tmp_path, "1.0.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", value)
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == active.as_posix()


def test_only_malformed_timestamp_falls_through(tmp_path: Path) -> None:
    """An undated record cannot override a valid newer cache selection."""
    older = _install(tmp_path, "1.0.0")
    newer = _install(tmp_path, "2.0.0")
    _registry(tmp_path, [{"installPath": str(older), "installedAt": "invalid"}])
    assert resolver.resolve(tmp_path, "borda-ai-rig", "foundry") == newer.as_posix()


def test_arbitrary_installed_plugin_does_not_need_shared_bootstrap(tmp_path: Path) -> None:
    """Generic plugin-root consumers can resolve third-party installs that do not ship this resolver."""
    root = tmp_path / ".claude/plugins/cache/borda-ai-rig/example/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"example"}', encoding="utf-8")
    (root / "agents").mkdir()
    registry = tmp_path / ".claude/plugins/installed_plugins.json"
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "example@borda-ai-rig": [
                        {"installPath": str(root), "installedAt": "2026-01-01T00:00:00Z"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert resolver.resolve(tmp_path, "borda-ai-rig", "example") == root.as_posix()


def test_missing_executing_resolver_cannot_authorize_source(tmp_path: Path) -> None:
    """A not-yet-shipped resolver path cannot authorize a sibling source directory."""
    collection = tmp_path / "repo/plugins"
    own = collection / "cc_develop"
    (own / ".claude-plugin").mkdir(parents=True)
    (own / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
    target = collection / "cc_foundry"
    (target / ".claude-plugin").mkdir(parents=True)
    (target / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    (target / "bin").mkdir()
    (target / "bin/resolve_plugin_root.py").write_text("# resolver\n", encoding="utf-8")
    assert (
        resolver.source_root(
            "foundry",
            resolver_file=own / "bin/resolve_plugin_root.py",
            cwd=collection.parent,
        )
        is None
    )


@_symlinks
def test_cache_symlink_escape_fails_closed(tmp_path: Path) -> None:
    """A version symlink cannot redirect cache resolution to an external executable tree."""
    valid = _install(tmp_path, "1.0.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (valid.parent / "2.0.0").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside expected cache"):
        resolver.resolve(tmp_path, "borda-ai-rig", "foundry")


@_symlinks
def test_source_manifest_symlink_escape_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source manifest outside the selected root cannot authorize the source fallback."""
    root = tmp_path / "repo/plugins/cc_foundry"
    (root / ".claude-plugin").mkdir(parents=True)
    outside = tmp_path / "plugin.json"
    outside.write_text('{"name":"foundry"}', encoding="utf-8")
    (root / ".claude-plugin/plugin.json").symlink_to(outside)
    script = root / "bin/resolve_plugin_root.py"
    script.parent.mkdir()
    script.write_text("# resolver\n", encoding="utf-8")
    monkeypatch.setattr(resolver, "__file__", str(script))
    monkeypatch.chdir(tmp_path / "repo")
    with pytest.raises(ValueError, match="manifest escapes"):
        resolver.resolve(tmp_path / "home", "borda-ai-rig", "foundry")
