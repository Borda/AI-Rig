"""Authoritative executable-mode map contract for ``build_package``.

Falsifies the defect the disposable install probes previously carried silently: a
copy's freshly synthesized git index (``git init`` + ``git add -A``) is not the mode
authority — on a ``core.filemode=false`` host it records ``100644`` for every file
regardless of the real on-disk executable bit, and a missing mode-map entry used to
default to non-executable rather than raising. This module proves, against the real
builder code path (not just raw git commands):

1. the synthesized-copy-index degradation is real (``core.filemode=false`` strips a
   tracked launcher's executable bit from the copy's own index);
2. an externally supplied authoritative mode map (captured from the REAL repo before
   the copy exists) overrides the degraded copy index and preserves the bit;
3. a shipped payload path missing from the effective mode map raises, both from the
   Python API and the CLI (nonzero exit), never silently defaulting to non-executable;
4. payload membership under an included directory draws from the SAME map as modes — an
   untracked file there (e.g. a concurrent wave's WIP) is invisible to the payload rather
   than a hard build failure, so the two sources of truth can no longer diverge.

Every test builds a real throwaway git repository: git's recorded modes are the contract.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_BUILDER = _PLUGIN_ROOT / "scripts" / "build_package.py"
if str(_BUILDER.parent) not in sys.path:
    sys.path.insert(0, str(_BUILDER.parent))

import build_package as builder  # noqa: E402  (needs the scripts path insert above)

_skip_windows_posix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executable bit is POSIX-only; git on Windows/NTFS cannot record 100755",
)


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in ``cwd``, raising ``CalledProcessError`` on failure."""
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30, check=True)


def _run_builder(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` (a ``build_package.py`` copy) as a CLI with ``args`` and capture its output."""
    return subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, timeout=120, check=False
    )


def _make_fixture_repo(root: Path, name: str) -> Path:
    """Create a minimal committed git repo shaped like a buildable ``build_package`` source.

    Holds one executable (``bin/launcher``, ``100755``) and one plain (``bin/lib.py``, ``100644``) tracked file, plus
    the required top-level documents and Claude manifest.
    """
    repo = root / name
    (repo / "bin").mkdir(parents=True)
    for doc in ("README.md", "LICENSE", "NOTICE", "CHANGELOG.md"):
        (repo / doc).write_text(f"{doc}\n")
    (repo / ".claude-plugin").mkdir()
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "fixture", "version": "0.0.1"}))
    launcher = repo / "bin" / "launcher"
    launcher.write_text("#!/bin/sh\necho hi\n")
    launcher.chmod(0o755)
    (repo / "bin" / "lib.py").write_text("x = 1\n")
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.com"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


def _copy_and_synthesize_index(real_repo: Path, dest: Path, *, filemode_false: bool) -> Path:
    """Copytree ``real_repo`` into ``dest`` and re-init a fresh git index over the copy.

    Mirrors ``_probe_runtime.stage_disposable_source``: ``copy2`` preserves the on-disk executable bit, then Git
    commands synthesize a throwaway index. When ``filemode_false`` the copy's config sets ``core.filemode false`` before
    adding — simulating a build host where this is the default (e.g. Windows).
    """
    shutil.copytree(real_repo / "bin", dest / "bin")
    for doc in ("README.md", "LICENSE", "NOTICE", "CHANGELOG.md"):
        shutil.copy2(real_repo / doc, dest / doc)
    shutil.copytree(real_repo / ".claude-plugin", dest / ".claude-plugin")
    _git(["init", "-q"], dest)
    if filemode_false:
        _git(["config", "core.filemode", "false"], dest)
    _git(["add", "-A"], dest)
    return dest


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    """Build a committed fixture repo (``bin/launcher`` is 100755) acting as the authoritative real checkout."""
    return _make_fixture_repo(tmp_path, "real")


@pytest.fixture
def repo_with_untracked_wip(real_repo: Path) -> Path:
    """Add an untracked ``src/codemap_py/wip.py`` to ``real_repo``, as left by a concurrent wave's work in progress."""
    wip = real_repo / "src" / "codemap_py" / "wip.py"
    wip.parent.mkdir(parents=True)
    wip.write_text("wip = True\n")
    return real_repo


def _launcher_record(manifest: dict) -> dict:
    """Return the ``bin/launcher`` file record of a built-package ``manifest``."""
    return next(r for r in manifest["files"] if r["path"] == "bin/launcher")


class TestSynthesizedCopyIndex:
    """The copy's own freshly synthesized git index is a degraded mode source on some hosts."""

    @pytest.mark.parametrize(
        ("filemode_false", "expect_exec"),
        [
            pytest.param(True, False, id="filemode-false-strips-bit"),
            pytest.param(False, True, id="filemode-default-keeps-bit", marks=_skip_windows_posix),
        ],
    )
    @pytest.mark.packaging
    def test_exec_bit_follows_core_filemode(self, tmp_path: Path, real_repo: Path, filemode_false, expect_exec):
        """A fresh copy index records the launcher's executable bit only when ``core.filemode`` is on.

        The real repo holds ``bin/launcher`` as 755. The copy is re-indexed from scratch with ``core.filemode`` either
        disabled (the degraded host this suite models) or left default (the control case).
        """
        copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy", filemode_false=filemode_false)

        modes = builder._git_exec_modes(copy)

        assert modes["bin/launcher"] is expect_exec


class TestAuthoritativeModeMap:
    """An externally captured mode map is the mode authority; the copy's own index is not."""

    @pytest.mark.packaging
    def test_degraded_copy_without_override_loses_exec_bit(self, tmp_path: Path, real_repo: Path):
        """Building the DEGRADED copy WITHOUT an override reproduces the original defect.

        Guards the falsification baseline: if this stopped losing the bit, the override test below would pass
        vacuously.
        """
        copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy-degraded", filemode_false=True)

        manifest = builder.build_package(copy, tmp_path / "built-degraded")

        assert _launcher_record(manifest)["exec"] is False, "unpatched copy-index path must still reproduce the defect"

    @_skip_windows_posix
    @pytest.mark.packaging
    def test_real_mode_map_preserves_exec_bit_despite_degraded_copy(self, tmp_path: Path, real_repo: Path):
        """The authoritative real-repo map overrides a degraded copy index; 755 survives on disk and in the manifest.

        The map is captured from the real repo before the copy exists, so the build must trust it over the copy's
        ``core.filemode=false`` index.
        """
        real_mode_map = builder._git_exec_modes(real_repo)
        copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy-degraded", filemode_false=True)

        manifest = builder.build_package(copy, tmp_path / "built-authoritative", mode_map=real_mode_map)

        assert _launcher_record(manifest)["exec"] is True
        assert (tmp_path / "built-authoritative" / "bin" / "launcher").stat().st_mode & 0o111


class TestMissingModeMapEntry:
    """A shipped payload path absent from the effective mode map raises instead of defaulting."""

    @pytest.mark.packaging
    def test_empty_map_raises(self, tmp_path: Path, real_repo: Path):
        """A required top-level document absent from the effective mode map raises, not defaults.

        An ``_INCLUDE_DIRS`` file can never reach this raise (membership is pre-filtered to the map's own keys): an
        empty map excludes ``bin/launcher``/``bin/lib.py`` from the payload entirely and the raise fires on the first
        required top-level document (e.g. ``README.md``), which is admitted by filesystem presence, not map membership.
        """
        with pytest.raises(ValueError, match="missing mode-map entry"):
            builder.build_package(real_repo, tmp_path / "built", mode_map={})

    @pytest.mark.packaging
    def test_cli_exits_nonzero(self, tmp_path: Path):
        """The CLI surfaces a missing mode-map entry as a named, nonzero-exit error.

        Real subprocess on purpose: the exit code and stderr text are what automation consuming the CLI sees.
        """
        empty_map = tmp_path / "empty-map.json"
        empty_map.write_text("{}")

        result = _run_builder(_BUILDER, "--out", str(tmp_path / "out"), "--mode-map", str(empty_map))

        assert result.returncode == 2
        assert "missing mode-map entry" in result.stderr

    @pytest.mark.packaging
    def test_required_document_missing_from_map_still_raises(self, tmp_path: Path, real_repo: Path):
        """Dropping ``README.md`` from an otherwise complete map still raises, naming the document.

        Distinguishes "untracked include-dir file: silently excluded" from "required top-level document missing
        from the map: still raises" — the two must not be conflated.
        """
        tracked_modes = builder._git_exec_modes(real_repo)
        incomplete_map = {k: v for k, v in tracked_modes.items() if k != "README.md"}

        with pytest.raises(ValueError, match=r"missing mode-map entry for shipped payload path: README.md"):
            builder.build_package(real_repo, tmp_path / "built", mode_map=incomplete_map)


class TestUntrackedIncludeDirFile:
    """Payload membership shares the mode map's authority, so untracked WIP can no longer diverge from it."""

    @pytest.mark.packaging
    def test_build_excludes_file_without_raising(self, tmp_path: Path, repo_with_untracked_wip: Path):
        """An untracked file under an include dir (e.g. concurrent WIP under ``src/``) ships nothing and raises nothing.

        Falsifies the pre-fix behavior: ``_iter_source_payload`` walked the filesystem, admitted this untracked file as
        a payload candidate, and the missing-mode-map-entry raise then hard-failed the whole build.
        """
        manifest = builder.build_package(repo_with_untracked_wip, tmp_path / "built")

        assert not (tmp_path / "built" / "src").exists()
        assert all(not record["path"].startswith("src/") for record in manifest["files"])

    @pytest.mark.packaging
    def test_check_succeeds(self, tmp_path: Path, repo_with_untracked_wip: Path):
        """Package ``--check`` mode exits 0 although an included directory holds an untracked file."""
        exit_code = builder._run_check(repo_with_untracked_wip, tmp_path / "out")

        assert exit_code == 0


class TestLoadModeMap:
    """``_load_mode_map`` reads a ``{path: bool}`` JSON file and reports every defect as ``ValueError``."""

    @pytest.mark.packaging
    def test_reads_valid_json(self, tmp_path: Path):
        """A well-formed ``{path: bool}`` JSON file loads as-is."""
        path = tmp_path / "modes.json"
        path.write_text(json.dumps({"bin/x": True, "README.md": False}))

        assert builder._load_mode_map(path) == {"bin/x": True, "README.md": False}

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            pytest.param(json.dumps(["bin/x"]), id="not-an-object"),
            pytest.param(json.dumps({"bin/x": "yes"}), id="non-bool-value"),
            pytest.param(json.dumps({"bin/x": 1}), id="int-not-bool-value"),
        ],
    )
    @pytest.mark.packaging
    def test_rejects_malformed_payload(self, tmp_path: Path, payload: str):
        """A malformed mode-map file raises ``ValueError`` rather than propagating a parse error.

        Covers unparsable text, a non-object top level, and values that are not strict booleans (``1`` is rejected).
        """
        path = tmp_path / "modes.json"
        path.write_text(payload)

        with pytest.raises(ValueError, match=r"cannot read mode map|must be a JSON object"):
            builder._load_mode_map(path)

    @pytest.mark.packaging
    def test_missing_file_raises(self, tmp_path: Path):
        """A nonexistent mode-map path raises ``ValueError``, not an unhandled ``OSError``."""
        with pytest.raises(ValueError, match="cannot read mode map"):
            builder._load_mode_map(tmp_path / "absent.json")


class TestCliModeMap:
    """CLI ``--mode-map`` against a self-contained tracked fixture source."""

    @pytest.mark.packaging
    def test_matches_default_git_derived_build(self, tmp_path: Path, real_repo: Path):
        """A build from an explicit mode map is byte-identical to the default git-derived build.

        Uses a fixture repo carrying its own copy of the builder, so the result never depends on unrelated work in the
        surrounding checkout. Real subprocesses on purpose: this is the CLI contract.
        """
        script = real_repo / "scripts" / "build_package.py"
        script.parent.mkdir()
        shutil.copy2(_BUILDER, script)
        mode_map_path = tmp_path / "real-modes.json"
        mode_map_path.write_text(json.dumps(builder._git_exec_modes(real_repo)))
        default_out, mapped_out = tmp_path / "default", tmp_path / "mapped"

        default_result = _run_builder(script, "--out", str(default_out))
        mapped_result = _run_builder(script, "--out", str(mapped_out), "--mode-map", str(mode_map_path))

        assert default_result.returncode == 0, default_result.stderr
        assert mapped_result.returncode == 0, mapped_result.stderr
        assert builder._tree_bytes(default_out) == builder._tree_bytes(mapped_out)


class TestNativeAssets:
    """Newly added native assets need explicit mode-map candidates; other untracked files stay out."""

    @pytest.fixture
    def source_with_native_assets(self, real_repo: Path) -> Path:
        """Add untracked ``.codex-mcp.json``, ``user_questions_mcp.py`` and ``unrelated.py`` to ``real_repo``."""
        (real_repo / ".codex-mcp.json").write_bytes(b"{}\n")
        provider = real_repo / "shared" / "user_questions_mcp.py"
        provider.parent.mkdir()
        provider.write_bytes(b"print('stdio provider')\n")
        (provider.parent / "unrelated.py").write_bytes(b"raise RuntimeError('untracked')\n")
        return real_repo

    @pytest.mark.packaging
    def test_undeclared_asset_raises(self, tmp_path: Path, source_with_native_assets: Path):
        """A native asset absent from the tracked-only mode map raises, naming the asset.

        Admitting files by filesystem presence would let unreviewed assets into the package.
        """
        tracked_modes = builder._git_exec_modes(source_with_native_assets)

        with pytest.raises(ValueError, match=r"missing mode-map entry for shipped payload path: .codex-mcp.json"):
            builder.build_package(source_with_native_assets, tmp_path / "missing-modes", mode_map=tracked_modes)

    @pytest.mark.packaging
    def test_declared_assets_admitted_unrelated_untracked_excluded(
        self, tmp_path: Path, source_with_native_assets: Path
    ):
        """Declared assets ship with the declared mode while an unrelated untracked sibling stays excluded.

        The map is the sole authority: declaring ``.codex-mcp.json`` and ``shared/user_questions_mcp.py`` admits exactly
        those, not the untracked ``shared/unrelated.py`` beside the provider.
        """
        tracked_modes = builder._git_exec_modes(source_with_native_assets)
        declared_modes = {**tracked_modes, ".codex-mcp.json": False, "shared/user_questions_mcp.py": False}

        manifest = builder.build_package(source_with_native_assets, tmp_path / "candidate", mode_map=declared_modes)

        records = {record["path"]: record for record in manifest["files"]}
        provider = source_with_native_assets / "shared" / "user_questions_mcp.py"
        assert records[".codex-mcp.json"]["exec"] is False
        assert records["shared/user_questions_mcp.py"]["exec"] is False
        assert records["bin/launcher"]["exec"] == tracked_modes["bin/launcher"]
        assert "shared/unrelated.py" not in records
        assert not (tmp_path / "candidate" / "shared" / "unrelated.py").exists()
        assert (tmp_path / "candidate" / "shared" / "user_questions_mcp.py").read_bytes() == provider.read_bytes()
