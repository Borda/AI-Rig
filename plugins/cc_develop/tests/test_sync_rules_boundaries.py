"""Prove rule-source provenance and preservation of user-authored destinations."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_develop_rule_boundary_installer", Path(__file__).resolve().parents[1] / "bin/sync_rules.py"
)
sync_rules = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = sync_rules
_SPEC.loader.exec_module(sync_rules)


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a registered plugin and a disposable rule destination."""
    home = tmp_path / "home"
    root = home / ".claude/plugins/cache/example/develop/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"develop"}', encoding="utf-8")
    (root / "rules").mkdir()
    (root / "rules/example.md").write_text("# Plugin rule\n", encoding="utf-8")
    registry = home / ".claude/plugins/installed_plugins.json"
    registry.write_text(
        json.dumps({"plugins": {"develop@example": [{"installPath": str(root), "installedAt": "2026-01-01"}]}}),
        encoding="utf-8",
    )
    dest = home / ".claude/rules/develop-example.md"
    dest.parent.mkdir()
    return home, root, dest


@pytest.mark.parametrize("approve", [False, True])
def test_headless_approval_keeps_user_file(source: tuple[Path, Path, Path], approve: bool) -> None:
    """Approval of plugin links never authorizes overwriting a real user file."""
    home, root, dest = source
    dest.write_bytes(b"User-authored rule\r\n")
    result = sync_rules.sync("develop", root, home, approve=approve)
    assert not dest.is_symlink()
    assert dest.read_bytes() == b"User-authored rule\r\n"
    assert result.conflicts == ["develop-example.md  (real file)"]
    assert "conflict, kept as-is" in sync_rules._report(result)
    assert list(dest.parent.glob("*.bak-*")) == []


def test_explicit_user_file_replacement_preserves_bytes(source: tuple[Path, Path, Path]) -> None:
    """An explicit replacement first preserves the user's exact content in a sibling backup."""
    home, root, dest = source
    dest.write_bytes(b"User-authored rule\r\n")
    result = sync_rules.sync("develop", root, home, replace_user_files=True)
    assert dest.is_symlink()
    assert dest.resolve() == root / "rules/example.md"
    backups = list(dest.parent.glob("develop-example.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"User-authored rule\r\n"
    assert result.replaced == [dest.name]


def test_dry_run_preserves_user_file_without_backup(source: tuple[Path, Path, Path]) -> None:
    """Planning an explicit replacement never mutates the destination or creates a backup."""
    home, root, dest = source
    dest.write_text("mine", encoding="utf-8")
    result = sync_rules.sync("develop", root, home, replace_user_files=True, dry_run=True)
    assert not dest.is_symlink()
    assert dest.read_text(encoding="utf-8") == "mine"
    assert list(dest.parent.glob("*.bak-*")) == []
    assert result.replaced == [dest.name]


def test_unregistered_source_is_rejected_before_mutation(source: tuple[Path, Path, Path]) -> None:
    """A manifest declaring the requested name cannot substitute for an install record."""
    home, root, dest = source
    (home / ".claude/plugins/installed_plugins.json").unlink()
    with pytest.raises(sync_rules.SourceError, match="installed plugin"):
        sync_rules.sync("develop", root, home, approve=True)
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_registry_for_another_plugin_cannot_authorize_source(source: tuple[Path, Path, Path]) -> None:
    """Another plugin's recorded path never authorizes a same-named manifest."""
    home, root, dest = source
    (home / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"other@example": [{"installPath": str(root)}]}}), encoding="utf-8"
    )
    with pytest.raises(sync_rules.SourceError, match="installed plugin"):
        sync_rules.sync("develop", root, home)
    assert not dest.exists()


def test_stale_record_cannot_authorize_old_install(source: tuple[Path, Path, Path]) -> None:
    """An older record is not current ownership proof after another version was installed."""
    home, root, dest = source
    entries = [
        {"installPath": str(root), "installedAt": "2026-01-01"},
        {"installPath": str(root.parent / "2.0.0"), "installedAt": "2026-02-01"},
    ]
    (home / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"develop@example": entries}}), encoding="utf-8"
    )
    with pytest.raises(sync_rules.SourceError, match="installed plugin"):
        sync_rules.sync("develop", root, home)
    assert not dest.exists()


def test_registered_external_install_is_accepted(source: tuple[Path, Path, Path], tmp_path: Path) -> None:
    """A registered external installation remains valid without depending on cache layout."""
    home, root, dest = source
    external = tmp_path / "external-install"
    root.rename(external)
    (home / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"develop@example": [{"installPath": str(external)}]}}), encoding="utf-8"
    )
    result = sync_rules.sync("develop", external, home)
    assert result.linked == [dest.name]
    assert dest.resolve() == external / "rules/example.md"


def test_backup_collision_keeps_user_file(source: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed exclusive backup leaves both the destination and previous backup intact."""
    home, root, dest = source
    dest.write_text("mine", encoding="utf-8")
    backup = dest.with_name(f"{dest.name}.bak-123")
    backup.write_text("older backup", encoding="utf-8")
    monkeypatch.setattr(sync_rules.time, "time_ns", lambda: 123)
    result = sync_rules.sync("develop", root, home, replace_user_files=True)
    assert not dest.is_symlink()
    assert dest.read_text(encoding="utf-8") == "mine"
    assert backup.read_text(encoding="utf-8") == "older backup"
    assert len(result.failed) == 1
    assert result.replaced == []


def test_cli_explicit_user_file_choice_creates_backup(
    source: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The explicit CLI choice reaches replacement while the headless approve flag stays separate."""
    home, root, dest = source
    dest.write_text("mine", encoding="utf-8")
    argv = ["--plugin-name", "develop", "--plugin-root", str(root), "--home", str(home), "--replace-user-files"]
    assert sync_rules.main(argv) == 0
    assert "replaced (explicit approval): develop-example.md" in capsys.readouterr().out
    assert dest.is_symlink()
    backups = list(dest.parent.glob("*.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "mine"


def test_executing_source_checkout_is_trusted(tmp_path: Path) -> None:
    """Using the already executing checkout itself needs no installation registry."""
    root = Path(sync_rules.__file__).resolve().parents[1]
    home = tmp_path / "home"
    home.mkdir()
    result = sync_rules.sync("develop", root, home)
    assert not result.failed
    assert result.linked
    assert (home / ".claude/rules/develop-quality-gates.md").resolve() == root / "rules/quality-gates.md"
