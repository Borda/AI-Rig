"""Fortify cleanup registration remains bound to the selected run and variant."""

from __future__ import annotations

import tempfile
from pathlib import Path

import fortify_next_variant
import pytest


def _symlinks_supported() -> bool:
    """Probe directory symlink support before running symlink-specific path cases."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to(Path(directory), target_is_directory=True)
        except OSError:
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepare a first full variant and isolated session sentinels."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "variants.jsonl").write_text('{"variant_name":"full"}\n', encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "variant-path")
    monkeypatch.delenv("FORTIFY_WORKTREE", raising=False)
    return run_dir


def test_ambient_worktree_override_cannot_redirect_cleanup(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup always registers the path the skill derives for the selected variant."""
    monkeypatch.setenv("FORTIFY_WORKTREE", str(run.parent / "unrelated-worktree"))
    assert fortify_next_variant.main([str(run)]) == 0
    assert (run / "worktree-paths.txt").read_text(encoding="utf-8") == (f"{run.as_posix()}/worktrees/variant-full\n")


@_skip_no_symlink
def test_cleanup_accumulator_symlink_is_not_followed(run: Path) -> None:
    """A planted accumulator link cannot append cleanup paths to an unrelated file."""
    target = run.parent / "keep.txt"
    target.write_text("keep\n", encoding="utf-8")
    (run / "worktree-paths.txt").symlink_to(target)
    assert fortify_next_variant.main([str(run)]) == 1
    assert target.read_text(encoding="utf-8") == "keep\n"


@_skip_no_symlink
def test_validated_fortify_directory_is_resolved(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation returns the canonical path rather than the raw symlink coordinate."""
    monkeypatch.chdir(run.parent)
    Path("alias").symlink_to(run, target_is_directory=True)
    assert fortify_next_variant._validate_fortify_dir("alias") == run.resolve()
