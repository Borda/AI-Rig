"""Verify review untracked inventories from a nested Git working directory."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parents[2] / "shared" / "collect_diff.py"


@pytest.mark.parametrize(
    ("scope", "target", "expected"),
    [
        pytest.param("working-tree", "", {"nested/inside.py", "sibling.py"}, id="whole-working-tree"),
        pytest.param("path", "inside.py", {"inside.py"}, id="caller-relative-path"),
        pytest.param("commit", "HEAD", set(), id="committed-scope"),
    ],
)
def test_untracked_inventory_from_subdirectory(tmp_path: Path, scope: str, target: str, expected: set[str]) -> None:
    """Keep working-tree inventory complete without changing path and commit scope semantics."""
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=repository, env=environment, capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    git("add", "tracked.py")
    git("commit", "-qm", "base")
    (repository / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "sibling.py").write_text("sibling = 1\n", encoding="utf-8")
    nested = repository / "nested"
    nested.mkdir()
    (nested / "inside.py").write_text("inside = 1\n", encoding="utf-8")
    output = tmp_path / "result"
    subprocess.run(
        [sys.executable, str(COLLECTOR), "--scope", scope, "--target", target, "--out", str(output)],
        cwd=nested,
        env=environment,
        capture_output=True,
        check=True,
    )
    assert set((output / "untracked.txt").read_text(encoding="utf-8").splitlines()) == expected
    if scope == "working-tree":
        assert b"tracked.py" in (output / "diff.patch").read_bytes()
