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
   and
4. Payload membership under an included directory draws
   from the SAME map as modes — an untracked file there (e.g. a concurrent wave's WIP)
   is invisible to the payload rather than a hard build failure, so the two sources of
   truth can no longer diverge.
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


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in ``cwd``, raising ``CalledProcessError`` on failure."""
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30, check=True)


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
    commands synthesize a throwaway index. When ``filemode_false`` the copy's config sets     ``core.filemode false``
    before adding — simulating a build host where this is the default (e.g. Windows).
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


# --- (1) synthesized-copy-index degradation is real -------------------------


@pytest.mark.packaging
def test_synthesized_copy_index_degrades_under_core_filemode_false(tmp_path: Path) -> None:
    """A fresh copy index under ``core.filemode=false`` reports 100644 for a 755 file."""
    real_repo = _make_fixture_repo(tmp_path, "real")
    copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy-degraded", filemode_false=True)

    modes = builder._git_exec_modes(copy)

    assert modes["bin/launcher"] is False, "core.filemode=false must strip the copy's own recorded mode"


_skip_windows_posix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executable bit is POSIX-only; git on Windows/NTFS cannot record 100755",
)


@_skip_windows_posix
@pytest.mark.packaging
def test_synthesized_copy_index_is_correct_when_filemode_true(tmp_path: Path) -> None:
    """Control: the same copy under default ``core.filemode`` records the bit correctly."""
    real_repo = _make_fixture_repo(tmp_path, "real")
    copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy-clean", filemode_false=False)

    modes = builder._git_exec_modes(copy)

    assert modes["bin/launcher"] is True


# --- (2) authoritative external map overrides the degraded copy index ------


@pytest.mark.packaging
def test_build_with_degraded_copy_own_index_loses_executable_bit(tmp_path: Path) -> None:
    """Building the DEGRADED copy WITHOUT an override reproduces the original defect."""
    real_repo = _make_fixture_repo(tmp_path, "real")
    copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy-degraded", filemode_false=True)

    manifest = builder.build_package(copy, tmp_path / "built-degraded")

    record = next(r for r in manifest["files"] if r["path"] == "bin/launcher")
    assert record["exec"] is False, "unpatched copy-index path must still be able to reproduce the defect"


@_skip_windows_posix
@pytest.mark.packaging
def test_build_with_real_mode_map_preserves_executable_bit_despite_degraded_copy(tmp_path: Path) -> None:
    """The authoritative real-repo map overrides a degraded copy index; 755 survives."""
    real_repo = _make_fixture_repo(tmp_path, "real")
    real_mode_map = builder._git_exec_modes(real_repo)
    copy = _copy_and_synthesize_index(real_repo, tmp_path / "copy-degraded", filemode_false=True)

    manifest = builder.build_package(copy, tmp_path / "built-authoritative", mode_map=real_mode_map)

    record = next(r for r in manifest["files"] if r["path"] == "bin/launcher")
    assert record["exec"] is True
    if sys.platform != "win32":
        assert (tmp_path / "built-authoritative" / "bin" / "launcher").stat().st_mode & 0o111


# --- (3) missing mode-map entry raises, never defaults ----------------------


@pytest.mark.packaging
def test_build_raises_on_payload_path_missing_from_mode_map(tmp_path: Path) -> None:
    """A required top-level document absent from the effective mode map raises, not defaults.

    Since the MEDIUM fix below, an ``_INCLUDE_DIRS`` file can never reach this raise (membership is now pre-filtered to
    the map's own keys) — an empty map here excludes ``bin/launcher``/``bin/lib.py`` from the payload entirely and the
    raise instead fires on the first required top-level document (e.g. ``README.md``), which is still admitted by
    filesystem presence rather than map membership.
    """
    real_repo = _make_fixture_repo(tmp_path, "real")

    with pytest.raises(ValueError, match="missing mode-map entry"):
        builder.build_package(real_repo, tmp_path / "built", mode_map={})


@pytest.mark.packaging
def test_cli_exits_nonzero_on_missing_mode_map_entry(tmp_path: Path) -> None:
    """The CLI surfaces a missing mode-map entry as a named, nonzero-exit error."""
    empty_map = tmp_path / "empty-map.json"
    empty_map.write_text("{}")

    result = subprocess.run(
        [sys.executable, str(_BUILDER), "--out", str(tmp_path / "out"), "--mode-map", str(empty_map)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 2
    assert "missing mode-map entry" in result.stderr


# --- (4) untracked include-dir file is excluded, not a build failure --------
# Payload membership shares the mode map's authority, so it can no longer diverge from it.


@pytest.mark.packaging
def test_untracked_file_under_include_dir_is_excluded_not_raised(tmp_path: Path) -> None:
    """An untracked file under an include dir (e.g. concurrent WIP under ``src/``) ships nothing and raises nothing.

    Falsifies the pre-fix behavior directly: before the fix, ``_iter_source_payload``
    walked the filesystem, admitted this untracked file as a payload candidate, and the
    (already-existing) missing-mode-map-entry raise then hard-failed the whole build.
    """
    real_repo = _make_fixture_repo(tmp_path, "real")
    untracked = real_repo / "src" / "codemap_py" / "wip.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("wip = True\n")

    manifest = builder.build_package(real_repo, tmp_path / "built")

    assert not (tmp_path / "built" / "src").exists()
    assert all(not record["path"].startswith("src/") for record in manifest["files"])


@pytest.mark.packaging
def test_check_succeeds_with_untracked_file_under_include_dir(tmp_path: Path) -> None:
    """Allow untracked files inside an included directory during package checks."""
    real_repo = _make_fixture_repo(tmp_path, "real")
    untracked = real_repo / "src" / "codemap_py" / "wip.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("wip = True\n")

    exit_code = builder._run_check(real_repo, tmp_path / "out")

    assert exit_code == 0


@pytest.mark.packaging
def test_untracked_required_doc_position_still_raises_via_mode_map(tmp_path: Path) -> None:
    """An untracked file does NOT weaken the required-document mode-map check.

    Distinguishes "untracked include-dir file: silently excluded" (this fix) from
    "untracked required top-level document: still raises" (unchanged invariant) —
    the two must not be conflated.
    """
    real_repo = _make_fixture_repo(tmp_path, "real")
    tracked_modes = builder._git_exec_modes(real_repo)
    incomplete_map = {k: v for k, v in tracked_modes.items() if k != "README.md"}

    with pytest.raises(ValueError, match="missing mode-map entry for shipped payload path: README.md"):
        builder.build_package(real_repo, tmp_path / "built", mode_map=incomplete_map)


# --- _load_mode_map contract -------------------------------------------------


@pytest.mark.packaging
def test_load_mode_map_reads_valid_json(tmp_path: Path) -> None:
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
def test_load_mode_map_rejects_malformed_payload(tmp_path: Path, payload: str) -> None:
    """A malformed mode-map file raises ``ValueError`` rather than propagating a parse error."""
    path = tmp_path / "modes.json"
    path.write_text(payload)

    with pytest.raises(ValueError):
        builder._load_mode_map(path)


@pytest.mark.packaging
def test_load_mode_map_missing_file_raises(tmp_path: Path) -> None:
    """A nonexistent mode-map path raises ``ValueError``, not an unhandled ``OSError``."""
    with pytest.raises(ValueError, match="cannot read mode map"):
        builder._load_mode_map(tmp_path / "absent.json")


# --- CLI ``--mode-map`` integration against the real tracked tree ---------------


@pytest.mark.packaging
def test_cli_mode_map_matches_default_git_derived_build(tmp_path: Path) -> None:
    """Reproduce the default build from an explicit mode map.

    This builds against the live ``SOURCE_ROOT``, so it previously flaked whenever an untracked file sat under an
    include dir (e.g. concurrent Wave 2 WIP under ``src/``) — the old filesystem-walk membership would admit it into one
    build's candidate set but not the other's derivation path consistently, and the missing-mode-map-entry raise would
    fire. Now that membership is git-tracked-only for both invocations, an untracked file is excluded identically from
    both, so this test is hermetic to tree cleanliness without needing a fixture rewrite.
    """
    real_modes = builder._git_exec_modes(builder.SOURCE_ROOT)
    mode_map_path = tmp_path / "real-modes.json"
    mode_map_path.write_text(json.dumps(real_modes))

    default_out, mapped_out = tmp_path / "default", tmp_path / "mapped"
    default_result = subprocess.run(
        [sys.executable, str(_BUILDER), "--out", str(default_out)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    mapped_result = subprocess.run(
        [sys.executable, str(_BUILDER), "--out", str(mapped_out), "--mode-map", str(mode_map_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert default_result.returncode == 0, default_result.stderr
    assert mapped_result.returncode == 0, mapped_result.stderr
    assert builder._tree_bytes(default_out) == builder._tree_bytes(mapped_out)
