"""Acceptance checks for deterministic plugin package generation.

The package builder writes ``package-manifest.json``, the hash list installed plugins verify themselves against. These
tests prove the committed manifest is current and deterministic, that ``--check`` and ``--update`` behave on a source-
independent installed copy, and that an incomplete roster, runtime debris or a symlinked payload cannot corrupt it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE, POSIX_FILE_MODES_AVAILABLE

_requires_posix_file_modes = pytest.mark.skipif(
    not POSIX_FILE_MODES_AVAILABLE, reason="filesystem does not preserve POSIX file modes"
)
_requires_file_symlinks = pytest.mark.skipif(
    not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"
)
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
BUILD_SCRIPT = PLUGIN_ROOT / "scripts" / "build_package.py"
MANIFEST_CURRENT = "Package manifest is current.\n"
MANIFEST_UPDATED = "Updated package manifest: package-manifest.json\n"


def _load_builder(path: Path = BUILD_SCRIPT) -> ModuleType:
    """Load one package builder directly from its file path."""
    return load_module("codex_rig_build_package", path)


def _run_builder(plugin_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run an isolated package builder with bytecode writes disabled."""
    return subprocess.run(
        [sys.executable, str(plugin_root / "scripts" / "build_package.py"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _copied_plugin(tmp_path: Path) -> Path:
    """Copy only installed-package bytes into a source-independent fixture."""
    destination = tmp_path / "installed-plugin"
    shutil.copytree(
        PLUGIN_ROOT, destination, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".coverage")
    )
    assert not (tmp_path / ".codex").exists()
    return destination


def _manifest(sha256: str, mode: str) -> dict[str, object]:
    """Return a one-file manifest with the given hash and mode."""
    return {"schema": 1, "files": [{"path": "payload.txt", "sha256": sha256, "mode": mode}]}


def _remove_role(root: Path) -> None:
    """Delete the challenger role card."""
    (root / "roles" / "challenger" / "ROLE.md").unlink()


def _remove_skill(root: Path) -> None:
    """Delete the assess workflow skill."""
    (root / "skills" / "assess" / "SKILL.md").unlink()


@pytest.fixture
def builder() -> ModuleType:
    """Load the repository's package builder."""
    return _load_builder()


@pytest.fixture
def stale_mode_manifest_installed_plugin(tmp_path: Path) -> Path:
    """Copy the plugin and give its manifest bytes a normalizable stale ``mode`` value."""
    installed = _copied_plugin(tmp_path)
    manifest = installed / "package-manifest.json"
    original = manifest.read_bytes()
    stale = original.replace(b'"mode": "0644"', b'"mode": "0000"', 1)
    assert stale != original
    manifest.write_bytes(stale)
    return installed


@pytest.mark.packaging
class TestManifestGeneration:
    """Building the package manifest."""

    @pytest.mark.parametrize("directory", ["__pycache__", "scripts/__pycache__"])
    def test_inventory_ignores_only_bytecode_cache_files(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
    ) -> None:
        """Bytecode cache files never enter the builder's source payload roster."""
        monkeypatch.setattr(builder, "PACKAGE_ROOT", tmp_path)
        source = tmp_path / "helper.py"
        source.write_text("value = 1\n", encoding="utf-8")
        cache = tmp_path / directory / "helper.cpython-312.pyc"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"untrusted bytecode")

        assert builder.iter_payload_files() == [source]

    @pytest.mark.parametrize("relative", ["payload.txt", ".hidden.pyc", "nested/payload.pyc"])
    def test_inventory_refuses_noncache_payload_inside_bytecode_directory(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
    ) -> None:
        """Manifest generation refuses cache directories concealing other payload."""
        monkeypatch.setattr(builder, "PACKAGE_ROOT", tmp_path)
        payload = tmp_path / "scripts" / "__pycache__" / relative
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"unrecorded")

        with pytest.raises(ValueError, match="unexpected bytecode cache"):
            builder.iter_payload_files()

    @_requires_file_symlinks
    def test_inventory_refuses_bytecode_symlink(
        self, builder: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A .pyc suffix cannot exempt a symlink from the builder's payload refusal."""
        monkeypatch.setattr(builder, "PACKAGE_ROOT", tmp_path)
        source = tmp_path / "helper.py"
        source.write_text("value = 1\n", encoding="utf-8")
        cache = tmp_path / "__pycache__" / "helper.pyc"
        cache.parent.mkdir()
        cache.symlink_to(source)

        with pytest.raises(ValueError, match="symlink payload forbidden"):
            builder.iter_payload_files()

    @_requires_posix_file_modes
    def test_is_deterministic_and_embeds_no_user_paths(self, builder: ModuleType) -> None:
        """Generation is repeatable and embeds no user paths.

        Prevents nondeterministic fields or a machine-specific home path leaking into the shipped bytes. The committed
        manifest staying current is enforced by the ``codex-rig-package-manifest`` hook, which regenerates it.
        """
        first = builder.encode_manifest(builder.build_manifest())
        second = builder.encode_manifest(builder.build_manifest())

        assert first == second
        assert b"/Us" + b"ers/" not in first
        assert b"/ho" + b"me/" not in first

    def test_encoding_matches_pre_commit_json_order(self, builder: ModuleType) -> None:
        """Generated bytes equal the sorted, indented JSON the repository formatter produces.

        If they diverged, running the formatter would rewrite the manifest and make the next check report it stale.
        """
        manifest = builder.build_manifest()

        expected = (json.dumps(manifest, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode()

        assert builder.encode_manifest(manifest) == expected

    @_requires_posix_file_modes
    def test_binds_pure_role_generator(self, builder: ModuleType) -> None:
        """The manifest records the role generator's version, path and hash.

        Prevents installed managers from importing generator bytes the manifest does not vouch for.
        """
        manifest = builder.build_manifest()
        generator = PLUGIN_ROOT / "scripts" / "generate_roles.py"
        record = next(item for item in manifest["files"] if item["path"] == "scripts/generate_roles.py")

        assert manifest["generator"] == {
            "version": 2,
            "path": "scripts/generate_roles.py",
            "sha256": builder.sha256(generator.read_bytes()),
        }
        assert record == {
            "path": "scripts/generate_roles.py",
            "sha256": manifest["generator"]["sha256"],
            "mode": "0644",
        }

    @pytest.mark.parametrize(
        ("remove", "expected"),
        [
            pytest.param(_remove_role, "missing canonical role card", id="missing-role"),
            pytest.param(_remove_skill, "missing workflow skill", id="missing-skill"),
        ],
    )
    def test_rejects_incomplete_public_roster(
        self, tmp_path: Path, remove: Callable[[Path], None], expected: str
    ) -> None:
        """An installed copy missing a role card or workflow skill cannot produce a manifest on any platform."""
        installed = _copied_plugin(tmp_path)
        remove(installed)
        installed_builder = _load_builder(installed / "scripts" / "build_package.py")

        with pytest.raises(ValueError, match=expected):
            installed_builder.build_manifest()

    @_requires_posix_file_modes
    @_requires_file_symlinks
    def test_rejects_symlinked_payload(self, tmp_path: Path) -> None:
        """Updating refuses a payload file replaced by a symlink instead of blessing it in the manifest."""
        installed = _copied_plugin(tmp_path)
        target = installed / "roles" / "challenger" / "ROLE.md"
        target.unlink()
        target.symlink_to(installed / "roles" / "curator" / "ROLE.md")

        result = _run_builder(installed, "--update")

        assert result.returncode == 2
        assert "symlink payload forbidden" in result.stderr


@pytest.mark.packaging
class TestManifestComparison:
    """Comparing a committed manifest with a generated one."""

    @pytest.mark.parametrize(
        ("generated", "enforce_modes", "matches"),
        [
            pytest.param(_manifest("a" * 64, "0666"), False, True, id="mode-ignored-when-not-enforced"),
            pytest.param(_manifest("a" * 64, "0666"), True, False, id="mode-compared-when-enforced"),
            pytest.param(_manifest("b" * 64, "0666"), False, False, id="hash-always-compared"),
        ],
    )
    def test_ignores_only_mode_values(
        self, builder: ModuleType, generated: dict[str, object], enforce_modes: bool, matches: bool
    ) -> None:
        """Without mode enforcement only the ``mode`` field is ignored; hashes always decide equality.

        Lets native Windows check release bytes without rewriting POSIX modes while still catching content drift.
        """
        current = _manifest("a" * 64, "0644")

        assert builder.manifests_match(current, generated, enforce_modes=enforce_modes) == matches


@pytest.mark.packaging
class TestUpdateMode:
    """The explicit ``--update`` mode."""

    def test_refuses_without_posix_mode_enforcement(self, builder: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Authoritative release-mode generation is refused where POSIX modes cannot be enforced (native Windows)."""
        monkeypatch.setattr(builder, "parse_args", lambda: SimpleNamespace(check=False, update=True))
        monkeypatch.setattr(builder, "enforces_posix_modes", lambda: False)
        monkeypatch.setattr(builder, "build_manifest", lambda: pytest.fail("update continued into generation"))

        with pytest.raises(SystemExit, match="package-manifest-update-requires-posix"):
            builder.main()

    @_requires_posix_file_modes
    def test_rewrites_stale_manifest(self, tmp_path: Path) -> None:
        """Updating after a payload edit turns a failing check into a passing one.

        Editing a role card makes the manifest stale (``--check`` fails); ``--update`` rewrites it and a following
        ``--check`` is clean, so update mode cannot leave stale package hashes behind.
        """
        installed = _copied_plugin(tmp_path)
        role = installed / "roles" / "challenger" / "ROLE.md"
        role.write_text(role.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        stale = _run_builder(installed, "--check")
        updated = _run_builder(installed, "--update")
        current = _run_builder(installed, "--check")

        assert stale.returncode != 0
        assert updated.returncode == 0, updated.stderr
        assert updated.stdout == MANIFEST_UPDATED
        assert current.returncode == 0, current.stderr

    @_requires_posix_file_modes
    def test_normalizes_stale_manifest_mode(self, stale_mode_manifest_installed_plugin: Path) -> None:
        """A manifest differing only in a ``mode`` value is rewritten, not reported as a failed update."""
        result = _run_builder(stale_mode_manifest_installed_plugin, "--update")

        assert result.returncode == 0, result.stderr
        assert result.stdout == MANIFEST_UPDATED

    @_requires_posix_file_modes
    def test_is_idempotent_after_normalizing_stale_manifest(self, stale_mode_manifest_installed_plugin: Path) -> None:
        """A second update after normalization reports current and leaves the manifest file untouched.

        Prevents platform mode normalization from causing a rewrite (and a changed mtime) on every update.
        """
        installed = stale_mode_manifest_installed_plugin
        manifest = installed / "package-manifest.json"
        _run_builder(installed, "--update")
        before = manifest.stat().st_mtime_ns

        result = _run_builder(installed, "--update")

        assert result.returncode == 0, result.stderr
        assert result.stdout == MANIFEST_CURRENT
        assert manifest.stat().st_mtime_ns == before


@pytest.mark.packaging
class TestCheckMode:
    """The ``--check`` mode."""

    def test_uses_verified_manifest_without_posix_mode_enforcement(
        self, builder: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where POSIX modes cannot be enforced, checking verifies the installed bytes instead of regenerating.

        Native Windows checks stay bound to the verified installed-package manifest and never rebuild it.
        """
        calls: list[tuple[Path, bool]] = []
        monkeypatch.setattr(builder, "parse_args", lambda: SimpleNamespace(check=True, update=False))
        monkeypatch.setattr(builder, "enforces_posix_modes", lambda: False)
        monkeypatch.setattr(
            builder, "verify_package", lambda root, *, enforce_modes: calls.append((root, enforce_modes))
        )
        monkeypatch.setattr(builder, "build_manifest", lambda: pytest.fail("native Windows regenerated the manifest"))

        builder.main()

        assert calls == [(builder.PACKAGE_ROOT, False)]
        assert capsys.readouterr().out == MANIFEST_CURRENT

    @_requires_posix_file_modes
    def test_passes_on_source_independent_installed_copy(self, tmp_path: Path) -> None:
        """Manifest validation of a copied plugin needs no repository source tree."""
        installed = _copied_plugin(tmp_path)

        result = _run_builder(installed, "--check")

        assert result.returncode == 0, result.stderr
        assert result.stdout == MANIFEST_CURRENT

    def test_ignores_runtime_report_debris(self, tmp_path: Path) -> None:
        """Runtime `.reports/` artifacts never enter or invalidate the package manifest.

        A calibration run writes gitignored, machine-local artifacts under `.reports/` inside the plugin tree; a later
        manifest refresh must neither sweep those paths into the tracked manifest nor report the manifest stale because
        of them.
        """
        installed = _copied_plugin(tmp_path)
        debris = installed / ".reports" / "codex" / "calibration" / "run" / "debris.txt"
        debris.parent.mkdir(parents=True)
        debris.write_text("machine-local runtime artifact\n", encoding="utf-8")

        result = _run_builder(installed, "--check")

        assert result.returncode == 0, result.stderr
        assert result.stdout == MANIFEST_CURRENT
