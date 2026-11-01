"""Verify safe migration of legacy GitHub rules into the default permission profile."""

from __future__ import annotations

import hashlib
import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

import pytest
from _github_read_helpers import allow_rule, installed_plugin
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "install_github_read_rules.py"
PR_URL = "https://github.com/example/project/pull/7"
WINDOWS_HOME = PureWindowsPath(r"D:\User Space\codex")
OTHER_WINDOWS_HOME = PureWindowsPath(r"D:\Other\codex")
_requires_file_symlinks = pytest.mark.skipif(
    not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"
)


def _run(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run setup or removal against an isolated Codex home."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--codex-home", str(home), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def _windows_helper(home: PureWindowsPath, name: str) -> PureWindowsPath:
    """Return the cached shared helper path for a native Windows Codex home."""
    return home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.2.3" / "shared" / name


def _substitute_reader(root: Path) -> None:
    """Replace the verified reader helper with different bytes."""
    (root / "shared/github_read.py").write_bytes(b'print("substituted code")\n')


def _delete_manifest(root: Path) -> None:
    """Remove the package manifest that carries the integrity records."""
    (root / "package-manifest.json").unlink()


@pytest.fixture
def installer(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Load the installer script namespace with its sibling imports resolvable."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    return runpy.run_path(str(SCRIPT))


class TestSetup:
    """Setup migrates legacy grants into the default profile and refuses unverified state."""

    def test_migrates_exact_legacy_prefix_and_preserves_other_rules(self, home: Path) -> None:
        """Only the old canonical reader grant is removed, after backing up the original bytes.

        The default rules file holds an unrelated rule, the exact legacy reader grant, and a narrower rule that merely
        starts with the same prefix; only the exact grant may go and CRLF endings must survive.
        """
        current = installed_plugin(home, "2.0.0")
        old_reader = str(current.parent / "1.2.3" / "shared" / "github_read.py")
        other = allow_rule(["python", "other.py"], "\r\n")
        narrow = allow_rule(["python", old_reader, "--out", "one.log"])
        default = home / "rules" / "default.rules"
        default.parent.mkdir()
        original = other + allow_rule(["python", old_reader], "\r\n") + narrow
        default.write_bytes(original)

        result = _run(home, "--plugin-root", str(current))

        assert result.returncode == 0, result.stderr
        assert default.read_bytes() == other + narrow
        assert not (default.parent / "codex-rig-github-read.rules").exists()
        assert original in [backup.read_bytes() for backup in (home / "backups" / "codex-rig").iterdir()]
        assert (home / "config.toml").exists()

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param(b"# user-owned file\n", id="unowned"),
            pytest.param(b"# codex-rig:github-read sha256=invalid\n", id="invalid-marker"),
        ],
    )
    def test_rejects_unowned_or_invalid_old_rule(self, home: Path, plugin_root: Path, existing: bytes) -> None:
        """An old rule file without verified ownership and integrity is never deleted.

        Setup must exit nonzero and leave the file, config, and profile state untouched.
        """
        managed = home / "rules" / "codex-rig-github-read.rules"
        managed.parent.mkdir()
        managed.write_bytes(existing)

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert managed.read_bytes() == existing
        assert not (home / "config.toml").exists()
        assert not (home / "codex-rig-github-read-profile.json").exists()

    @pytest.mark.parametrize(
        ("rule_name", "marker"),
        [
            pytest.param("codex-rig-github-read.rules", b"# codex-rig:github-read sha256=", id="github-read"),
            pytest.param("codex-rig-pr-collection.rules", b"# codex-rig:pr-collection sha256=", id="pr-collection"),
        ],
    )
    def test_rejects_rehashed_broad_old_rule(
        self, home: Path, plugin_root: Path, rule_name: str, marker: bytes
    ) -> None:
        """A valid checksum cannot authorize deletion of a broadened old rule.

        The rule body grants plain `python` rather than the canonical helper command, so recomputing the marker digest
        over it must not make it count as installer-owned.
        """
        managed = home / "rules" / rule_name
        managed.parent.mkdir()
        body = allow_rule(["python"])
        original = marker + hashlib.sha256(body).hexdigest().encode() + b"\n" + body
        managed.write_bytes(original)

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert managed.read_bytes() == original
        assert not (home / "config.toml").exists()

    def test_rejects_plugin_outside_target_home(self, tmp_path: Path) -> None:
        """Setup runs only from the selected home's versioned installed cache.

        A package installed under a different home must not be accepted, and the target home is not even created.
        """
        foreign = installed_plugin(tmp_path / "other")
        target = tmp_path / "target"

        result = _run(target, "--plugin-root", str(foreign))

        assert result.returncode == 2
        assert not target.exists()

    @pytest.mark.parametrize("launcher", ["python", "python3", "env"])
    def test_preserves_user_collector_approvals(self, home: Path, plugin_root: Path, launcher: str) -> None:
        """Setup and clear leave single-launcher user collector approvals byte-identical.

        The user saved approvals for the collector with a bare launcher, once bare and once with arguments; the profile
        never claims them, so neither setup nor clear may touch them.
        """
        collector = str(plugin_root / "shared" / "collect_pr.py")
        rules = home / "rules" / "default.rules"
        rules.parent.mkdir()
        original = allow_rule([launcher, collector], "\r\n") + allow_rule(
            [launcher, collector, "--target", "7", "--out", "report"], "\r\n"
        )
        rules.write_bytes(original)

        setup = _run(home, "--plugin-root", str(plugin_root))
        assert setup.returncode == 0, setup.stderr
        assert rules.read_bytes() == original
        assert "[permissions.github-read]" in (home / "config.toml").read_text()

        clear = _run(home, "--remove")

        assert clear.returncode == 0, clear.stderr
        assert rules.read_bytes() == original
        assert not (home / "config.toml").exists()

    @pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
    def test_rejects_linked_rules_directory(self, tmp_path: Path, home: Path, plugin_root: Path) -> None:
        """A rules directory that is a symlink is refused so writes stay inside the selected home."""
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "file"
        target.write_bytes(b"# external bytes\n")
        (home / "rules").symlink_to(outside, target_is_directory=True)

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert "linked path" in result.stderr
        assert target.read_bytes() == b"# external bytes\n"
        assert not (home / "config.toml").exists()

    @_requires_file_symlinks
    @pytest.mark.parametrize(
        ("component", "expect_config_link"),
        [
            pytest.param("reader", False, id="reader-file-link"),
            pytest.param("default", False, id="default-file-link"),
            pytest.param("config", True, id="config-file-link"),
        ],
    )
    def test_rejects_linked_file(
        self, tmp_path: Path, home: Path, plugin_root: Path, component: str, expect_config_link: bool
    ) -> None:
        """A symlinked reader helper, default rules file, or config file is refused without following it.

        Each link points at a file outside the home; setup must exit nonzero, name the linked path, and leave the
        external bytes alone. Only the config link legitimately remains afterwards, because it existed beforehand.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "file"
        target.write_bytes(b"# external bytes\n")
        (home / "rules").mkdir()
        link = {
            "reader": plugin_root / "shared" / "github_read.py",
            "default": home / "rules" / "default.rules",
            "config": home / "config.toml",
        }[component]
        link.unlink(missing_ok=True)
        link.symlink_to(target)

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert "linked path" in result.stderr
        assert target.read_bytes() == b"# external bytes\n"
        assert (home / "config.toml").is_symlink() is expect_config_link
        assert (home / "config.toml").exists() is expect_config_link

    def test_rejects_wrong_manifest_identity_before_migration(self, home: Path, plugin_root: Path) -> None:
        """A package whose plugin identity is wrong leaves existing grants untouched.

        The plugin manifest claims another plugin name while the legacy reader grant is present; migration must not
        start, and no config may appear.
        """
        (plugin_root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "another-plugin", "version": "1.2.3"}), encoding="utf-8"
        )
        default = home / "rules" / "default.rules"
        default.parent.mkdir()
        original = allow_rule(["python", str(plugin_root / "shared" / "github_read.py")])
        default.write_bytes(original)

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert "identity" in result.stderr
        assert default.read_bytes() == original
        assert not (home / "config.toml").exists()

    def test_rejects_invalid_backup_path_before_profile_write(self, home: Path, plugin_root: Path) -> None:
        """Migration backup prerequisites are checked before the new profile is selected.

        A regular file already occupies the backups path while a legacy grant needs backing up, so setup must stop with
        the grant, the unrelated file, and the config state all unchanged.
        """
        default = home / "rules" / "default.rules"
        default.parent.mkdir()
        legacy = allow_rule(["python", str(plugin_root / "shared/github_read.py")])
        default.write_bytes(legacy)
        (home / "backups").write_bytes(b"unrelated existing file")

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert "not a directory" in result.stderr
        assert default.read_bytes() == legacy
        assert not (home / "config.toml").exists()
        assert (home / "backups").read_bytes() == b"unrelated existing file"

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(_substitute_reader, id="reader-bytes-changed"),
            pytest.param(_delete_manifest, id="manifest-missing"),
        ],
    )
    def test_rejects_unverified_package_bytes(
        self, home: Path, plugin_root: Path, tamper: Callable[[Path], None]
    ) -> None:
        """Package changes that break integrity verification are rejected before any profile is selected."""
        tamper(plugin_root)

        result = _run(home, "--plugin-root", str(plugin_root))

        assert result.returncode == 2
        assert not (home / "config.toml").exists()
        assert not (home / "rules").exists()


class TestClear:
    """Removal against malformed targets."""

    def test_rejects_regular_file_home(self, tmp_path: Path) -> None:
        """A regular file used as the Codex home is refused even when no managed state exists."""
        home = tmp_path / "not-a-directory"
        home.write_bytes(b"user file")

        result = _run(home, "--remove")

        assert result.returncode == 2
        assert home.read_bytes() == b"user file"


class TestWindowsGrantRecognition:
    """Native Windows reader and collector coordinates are recognized on every host, without host path conversion."""

    @pytest.mark.parametrize(
        ("rules_home", "removed"),
        [
            pytest.param(WINDOWS_HOME, True, id="same-home"),
            pytest.param(OTHER_WINDOWS_HOME, False, id="foreign-home"),
        ],
    )
    def test_legacy_reader_rule_belongs_to_its_home(
        self, installer: dict[str, object], rules_home: PureWindowsPath, removed: bool
    ) -> None:
        """A legacy reader grant is stripped only for the home whose cache path it names.

        The grant uses a native Windows path, so recognition must compare Windows coordinates instead of converting
        through the host's path type.
        """
        unchanged = b"# user comment\r\n"
        legacy = allow_rule(["python3", str(_windows_helper(WINDOWS_HOME, "github_read.py"))], "\r\n")

        stripped = installer["strip_legacy_rules"](unchanged + legacy, rules_home)

        assert stripped == (unchanged if removed else unchanged + legacy)

    @pytest.mark.parametrize(
        ("rules_home", "removed"),
        [
            pytest.param(WINDOWS_HOME, True, id="same-home"),
            pytest.param(OTHER_WINDOWS_HOME, False, id="foreign-home"),
        ],
    )
    def test_canonical_collector_grant_belongs_to_its_home(
        self, installer: dict[str, object], rules_home: PureWindowsPath, removed: bool
    ) -> None:
        """A canonical collector grant is stripped for its own home and preserved for a foreign home.

        An unrelated `git status` rule beside the grant must survive in both cases.
        """
        collector = _windows_helper(WINDOWS_HOME, "collect_pr.py")
        canonical = installer["render_pr_rules"](collector, [PR_URL]).partition(b"\n")[2]
        unrelated = allow_rule(["git", "status"])

        stripped = installer["strip_legacy_rules"](canonical + unrelated, rules_home)

        assert stripped == (unrelated if removed else canonical + unrelated)

    def test_mixed_foreign_and_local_collector_grant_is_unrecognized(self, installer: dict[str, object]) -> None:
        """A single rule naming both a foreign and this home's collector is refused as unrecognized.

        A foreign path in the same pattern must not conceal a noncanonical grant to the local collector.
        """
        foreign_collector = _windows_helper(OTHER_WINDOWS_HOME, "collect_pr.py")
        local_collector = _windows_helper(WINDOWS_HOME, "collect_pr.py")
        mixed = allow_rule(
            [["python", "python3"], [str(foreign_collector), str(local_collector)], "--target", [PR_URL]]
        )

        with pytest.raises(installer["UnsafeRulesState"], match="unrecognized collector grant"):
            installer["strip_legacy_rules"](mixed, WINDOWS_HOME)
