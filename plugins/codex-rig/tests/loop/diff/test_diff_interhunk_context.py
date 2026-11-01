"""Check that the local review patch is independent of the user's Git diff configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parents[3] / "shared" / "collect_diff.py"
ALGORITHM_BASE = "4 1 1 2 3 4 4 1 4 4 2 4 3 3 2 0 2 3 2 4 3 0 4 3 2 2 4 1 0 0 2 2 1 0 2 0 2 1 4 2".split()
ALGORITHM_CHANGED = "4 3 1 1 2 3 1 4 4 3 2 4 3 3 2 0 2 3 3 1 4 3 0 4 2 2 2 4 1 0 2 1 0 2 0 2 2 1 3 2".split()


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's global Git configuration from every collector run."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.delenv("GIT_DIFF_OPTS", raising=False)


def _git(repository: Path, *arguments: str) -> None:
    """Run one Git command in the fixture repository."""
    subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)


def _repository_with_change(tmp_path: Path, base: list[str], changed: list[str]) -> Path:
    """Commit `module.py` with the base lines, then leave the changed lines uncommitted."""
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "module.py"
    source.write_text("\n".join(base) + "\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", source.name)
    _git(
        repository, "-c", "user.name=Review fixture", "-c", "user.email=review@example.invalid", "commit", "-qm", "base"
    )
    source.write_text("\n".join(changed) + "\n", encoding="utf-8")
    return repository


def _collect_patch(repository: Path, out: Path) -> bytes:
    """Run the review diff collector and return the frozen patch bytes."""
    subprocess.run([sys.executable, str(COLLECTOR), "--out", str(out)], cwd=repository, check=True)
    return (out / "diff.patch").read_bytes()


def test_ignores_interhunk_context(tmp_path: Path) -> None:
    """A local hunk-merging setting must not change the frozen review patch.

    Two edits ten lines apart stay two hunks until `diff.interHunkContext` would merge them; the collector must emit the
    same two-hunk patch before and after that setting is configured, so a reviewer never sees a different frozen diff.
    """
    base = [f"line {number}" for number in range(30)]
    changed = [*base[:3], "first change", *base[4:13], "second change", *base[14:]]
    repository = _repository_with_change(tmp_path, base, changed)
    original = _collect_patch(repository, tmp_path / "first")
    _git(repository, "config", "diff.interHunkContext", "10")

    assert original.count(b"@@ -") == 2
    assert _collect_patch(repository, tmp_path / "second") == original


def test_ignores_diff_algorithm(tmp_path: Path) -> None:
    """A configured diff algorithm must not change a frozen local review patch.

    The changed digit sequence is one where `myers` and `histogram` produce different hunks when Git honours the user
    setting; the collector must emit byte-identical patches under both.
    """
    repository = _repository_with_change(tmp_path, ALGORITHM_BASE, ALGORITHM_CHANGED)
    _git(repository, "config", "diff.algorithm", "myers")
    first = _collect_patch(repository, tmp_path / "first")
    _git(repository, "config", "diff.algorithm", "histogram")

    assert _collect_patch(repository, tmp_path / "second") == first
