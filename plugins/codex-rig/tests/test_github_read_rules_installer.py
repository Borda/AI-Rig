"""Verify safe migration of legacy GitHub rules into the default permission profile."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_github_read_rules.py"


def _installed_plugin(home: Path, version: str = "1.2.3") -> Path:
    """Create an installed package with valid identity and integrity records."""
    root = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-rig", "version": version}), encoding="utf-8"
    )
    (root / "shared").mkdir()
    (root / "shared" / "github_read.py").write_text('"""Fixture reader."""\n', encoding="utf-8")
    (root / "shared" / "collect_pr.py").write_text('"""Fixture collector."""\n', encoding="utf-8")
    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    reader = next(record for record in records if record["path"] == "shared/github_read.py")
    (root / "package-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "codex-rig",
                "version": version,
                "files": records,
                "skills": [],
                "roles": [],
                "bootstrap": {"protocol": "fixture", "helper": reader["path"], "sha256": reader["sha256"]},
                "generator": {"version": "fixture", "path": reader["path"], "sha256": reader["sha256"]},
            }
        ),
        encoding="utf-8",
    )
    return root


def _run(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run setup or removal against an isolated Codex home."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-home", str(home), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def test_setup_migrates_exact_legacy_prefix_and_preserves_other_rules(tmp_path: Path) -> None:
    """Remove only the old canonical reader grant after backing up original bytes."""
    home = tmp_path / "home"
    current = _installed_plugin(home, "2.0.0")
    old_reader = current.parent / "1.2.3" / "shared" / "github_read.py"
    legacy = f'prefix_rule(pattern={json.dumps(["python", str(old_reader)])}, decision="allow")\r\n'.encode()
    other = b'prefix_rule(pattern=["python", "other.py"], decision="allow")\r\n'
    narrow = f'prefix_rule(pattern={json.dumps(["python", str(old_reader), "--out", "one.log"])}, decision="allow")\n'.encode()
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    original = other + legacy + narrow
    default.write_bytes(original)

    result = _run(home, "--plugin-root", str(current))

    assert result.returncode == 0, result.stderr
    assert default.read_bytes() == other + narrow
    assert not (default.parent / "codex-rig-github-read.rules").exists()
    backups = list((home / "backups" / "codex-rig").iterdir())
    assert original in [backup.read_bytes() for backup in backups]
    assert (home / "config.toml").exists()


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(b"# user-owned file\n", id="unowned"),
        pytest.param(b"# codex-rig:github-read sha256=invalid\n", id="invalid-marker"),
    ],
)
def test_setup_rejects_unowned_or_invalid_old_rule(tmp_path: Path, existing: bytes) -> None:
    """Refuse to delete an old rule file without verified ownership and integrity."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    managed = home / "rules" / "codex-rig-github-read.rules"
    managed.parent.mkdir()
    managed.write_bytes(existing)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert managed.read_bytes() == existing
    assert not (home / "config.toml").exists()
    assert not (home / "codex-rig-github-read-profile.json").exists()


@pytest.mark.parametrize("rule_name", ["codex-rig-github-read.rules", "codex-rig-pr-collection.rules"])
def test_rehashed_broad_old_rule_is_not_owned(tmp_path: Path, rule_name: str) -> None:
    """A valid checksum cannot authorize deletion of a broadened old rule."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    managed = home / "rules" / rule_name
    managed.parent.mkdir()
    body = b'prefix_rule(pattern=["python"], decision="allow")\n'
    marker = (
        b"# codex-rig:github-read sha256="
        if rule_name.endswith("github-read.rules")
        else b"# codex-rig:pr-collection sha256="
    )
    original = marker + hashlib.sha256(body).hexdigest().encode() + b"\n" + body
    managed.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert managed.read_bytes() == original
    assert not (home / "config.toml").exists()


def test_clear_rejects_regular_file_home(tmp_path: Path) -> None:
    """Reject a malformed target even when no managed state exists."""
    home = tmp_path / "not-a-directory"
    home.write_bytes(b"user file")

    result = _run(home, "--remove")

    assert result.returncode == 2
    assert home.read_bytes() == b"user file"


def test_setup_rejects_plugin_outside_target_home(tmp_path: Path) -> None:
    """Allow setup only from the selected home's versioned installed cache."""
    foreign = _installed_plugin(tmp_path / "other")
    target = tmp_path / "target"

    result = _run(target, "--plugin-root", str(foreign))

    assert result.returncode == 2
    assert not target.exists()


def test_windows_legacy_rule_recognition_on_every_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recognize native Windows reader coordinates without host path conversion."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    functions = runpy.run_path(str(SCRIPT))
    home = PureWindowsPath(r"D:\User Space\codex")
    reader = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.2.3" / "shared" / "github_read.py"
    legacy = f'prefix_rule(pattern={json.dumps(["python3", str(reader)])}, decision="allow")\r\n'.encode()
    unchanged = b"# user comment\r\n"

    assert functions["strip_legacy_rules"](unchanged + legacy, home) == unchanged
    assert functions["strip_legacy_rules"](unchanged + legacy, PureWindowsPath(r"D:\Other\codex")) == unchanged + legacy


def test_windows_collector_grant_migration_on_every_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove this home's Windows grant while preserving a foreign home's grant."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    functions = runpy.run_path(str(SCRIPT))
    home = PureWindowsPath(r"D:\User Space\codex")
    collector = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.2.3" / "shared" / "collect_pr.py"
    target = "https://github.com/example/project/pull/7"
    canonical = functions["render_pr_rules"](collector, [target]).partition(b"\n")[2]
    unrelated = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'

    assert functions["strip_legacy_rules"](canonical + unrelated, home) == unrelated
    assert functions["strip_legacy_rules"](canonical + unrelated, PureWindowsPath(r"D:\Other\codex")) == (
        canonical + unrelated
    )

    foreign = PureWindowsPath(r"D:\Other\codex") / "plugins" / "cache" / "borda-ai-rig" / "codex-rig"
    foreign_collector = foreign / "1.2.3" / "shared" / "collect_pr.py"
    mixed = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], [str(foreign_collector), str(collector)], '--target', [target]])}, "
        'decision="allow")\n'
    ).encode()
    with pytest.raises(functions["UnsafeRulesState"], match="unrecognized collector grant"):
        functions["strip_legacy_rules"](mixed, home)


@pytest.mark.parametrize(
    "component",
    [
        pytest.param(
            "rules",
            id="rules-directory-link",
            marks=pytest.mark.skipif(
                not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks"
            ),
        ),
        pytest.param(
            "reader",
            id="reader-file-link",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"),
        ),
        pytest.param(
            "default",
            id="default-file-link",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"),
        ),
        pytest.param(
            "config",
            id="config-file-link",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"),
        ),
    ],
)
def test_setup_rejects_linked_paths(tmp_path: Path, component: str) -> None:
    """Keep profile and migration writes inside the selected home."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file"
    original = b"# external bytes\n"
    target.write_bytes(original)
    rules = home / "rules"
    if component == "rules":
        rules.symlink_to(outside, target_is_directory=True)
    else:
        rules.mkdir()
        link = {
            "reader": root / "shared" / "github_read.py",
            "default": rules / "default.rules",
            "config": home / "config.toml",
        }[component]
        if link.exists():
            link.unlink()
        link.symlink_to(target)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert "linked path" in result.stderr
    assert target.read_bytes() == original
    if component == "config":
        assert (home / "config.toml").is_symlink()
    else:
        assert not (home / "config.toml").exists()


def test_setup_rejects_wrong_manifest_identity_before_migration(tmp_path: Path) -> None:
    """Do not alter old grants when the proposed installed package has wrong identity."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "another-plugin", "version": "1.2.3"}), encoding="utf-8"
    )
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    original = f'prefix_rule(pattern={json.dumps(["python", str(root / "shared" / "github_read.py")])}, decision="allow")\n'.encode()
    default.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert "identity" in result.stderr
    assert default.read_bytes() == original
    assert not (home / "config.toml").exists()


def test_setup_rejects_invalid_backup_path_before_profile_write(tmp_path: Path) -> None:
    """Check migration backup prerequisites before selecting the new profile."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    default = home / "rules" / "default.rules"
    default.parent.mkdir()
    legacy = f'prefix_rule(pattern={json.dumps(["python", str(root / "shared/github_read.py")])}, decision="allow")\n'.encode()
    default.write_bytes(legacy)
    (home / "backups").write_bytes(b"unrelated existing file")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert "not a directory" in result.stderr
    assert default.read_bytes() == legacy
    assert not (home / "config.toml").exists()
    assert (home / "backups").read_bytes() == b"unrelated existing file"


@pytest.mark.parametrize("component", ["reader", "manifest"])
def test_setup_rejects_unverified_package_bytes(tmp_path: Path, component: str) -> None:
    """Reject package changes before selecting the new profile."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    if component == "reader":
        (root / "shared/github_read.py").write_bytes(b'print("substituted code")\n')
    else:
        (root / "package-manifest.json").unlink()

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 2
    assert not (home / "config.toml").exists()
    assert not (home / "rules").exists()
