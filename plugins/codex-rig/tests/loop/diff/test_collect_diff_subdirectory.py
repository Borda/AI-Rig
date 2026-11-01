"""Verify review untracked inventories from a nested Git working directory."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parents[3] / "shared" / "collect_diff.py"


@dataclass(frozen=True)
class NestedRepository:
    """A Git repository with a tracked edit, a root-level and a nested untracked file."""

    nested: Path
    environment: dict[str, str]


@pytest.fixture(scope="module")
def nested_repository(tmp_path_factory: pytest.TempPathFactory) -> NestedRepository:
    """Build the repository once; every collector run only reads it, so sharing it is safe."""
    repository = tmp_path_factory.mktemp("repository")
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]

    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=repository, env=environment, capture_output=True, check=True)

    (repository / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    git("init", "-q")
    git("add", "tracked.py")
    git(*identity, "commit", "-qm", "base")
    (repository / "tracked.py").write_text("value = 2\n", encoding="utf-8")
    (repository / "sibling.py").write_text("sibling = 1\n", encoding="utf-8")
    nested = repository / "nested"
    nested.mkdir()
    (nested / "inside.py").write_text("inside = 1\n", encoding="utf-8")
    return NestedRepository(nested, environment)


def _collect(fixture: NestedRepository, out: Path, scope: str, target: str) -> None:
    """Run the collector from the nested directory for one scope."""
    subprocess.run(
        [sys.executable, str(COLLECTOR), "--scope", scope, "--target", target, "--out", str(out)],
        cwd=fixture.nested,
        env=fixture.environment,
        capture_output=True,
        check=True,
    )


@pytest.mark.parametrize(
    ("scope", "target", "expected"),
    [
        pytest.param("working-tree", "", {"nested/inside.py", "sibling.py"}, id="whole-working-tree"),
        pytest.param("path", "inside.py", {"inside.py"}, id="caller-relative-path"),
        pytest.param("commit", "HEAD", set(), id="committed-scope"),
    ],
)
def test_untracked_inventory(
    nested_repository: NestedRepository, tmp_path: Path, scope: str, target: str, expected: set[str]
) -> None:
    """Keep working-tree inventory complete without changing path and commit scope semantics.

    The collector runs from a subdirectory: the whole working tree must list untracked files repository-relative, a path
    scope resolves against the caller's directory, and a committed scope lists no untracked files.
    """
    _collect(nested_repository, tmp_path / "result", scope, target)

    assert set((tmp_path / "result" / "untracked.txt").read_text(encoding="utf-8").splitlines()) == expected


def test_working_tree_diff_includes_tracked_edit(nested_repository: NestedRepository, tmp_path: Path) -> None:
    """The working-tree patch carries the tracked edit even when collected from a subdirectory.

    Guards against a collector that scopes its diff to the caller's directory and silently drops edits outside it.
    """
    _collect(nested_repository, tmp_path / "result", "working-tree", "")

    assert b"tracked.py" in (tmp_path / "result" / "diff.patch").read_bytes()
