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
        pytest.param("commit", "HEAD..HEAD", set(), id="two-endpoint-range"),
        pytest.param("commit", "HEAD...HEAD", set(), id="merge-base-range"),
        pytest.param("commit", "..HEAD", set(), id="implicit-left-head"),
        pytest.param("commit", "HEAD..", set(), id="implicit-right-head"),
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


def test_commit_target_cannot_inject_diff_output(nested_repository: NestedRepository, tmp_path: Path) -> None:
    """Reject option-shaped revisions before Git can truncate a caller-selected file."""
    sentinel = tmp_path / "preserved.txt"
    sentinel.write_bytes(b"preserve me")
    completed = subprocess.run(
        [
            sys.executable,
            str(COLLECTOR),
            "--scope",
            "commit",
            f"--target=--output={sentinel}",
            "--out",
            str(tmp_path / "out"),
        ],
        cwd=nested_repository.nested,
        env=nested_repository.environment,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert sentinel.read_bytes() == b"preserve me"
    assert (tmp_path / "out" / "scope-error.txt").read_text(encoding="utf-8") == "invalid-commit-target\n"


@pytest.fixture(scope="module")
def revision_repository(tmp_path_factory: pytest.TempPathFactory) -> NestedRepository:
    """Create two distinct commits for Git revision expansion comparisons."""
    repository = tmp_path_factory.mktemp("revisions")
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    identity = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]

    def git(*arguments: str) -> None:
        """Set up only the disposable repository's commit history."""
        subprocess.run(["git", *arguments], cwd=repository, env=environment, capture_output=True, check=True)

    git("init", "-q")
    (repository / "source.txt").write_text("base\n", encoding="utf-8")
    git("add", "source.txt")
    git(*identity, "commit", "-qm", "base")
    git("tag", "A")
    (repository / "source.txt").write_text("tip\n", encoding="utf-8")
    git("add", "source.txt")
    git(*identity, "commit", "-qm", "tip")
    git("tag", "B")
    return NestedRepository(repository, environment)


@pytest.mark.parametrize("target", ["HEAD^!", "HEAD^@", "A..B", "A...B", "..B", "A.."])
def test_commit_revision_expansions_match_git(
    revision_repository: NestedRepository, tmp_path: Path, target: str
) -> None:
    """Accept Git's commit-set syntax and preserve its patch semantics."""
    expected = subprocess.run(
        ["git", "diff", "--binary", "--end-of-options", target, "--"],
        cwd=revision_repository.nested,
        env=revision_repository.environment,
        capture_output=True,
        check=True,
    ).stdout
    _collect(revision_repository, tmp_path / "out", "commit", target)
    assert (tmp_path / "out" / "diff.patch").read_bytes() == expected
    assert (tmp_path / "out" / "untracked.txt").read_bytes() == b""


@pytest.mark.parametrize("target", ["unknown-revision", "source.txt"])
def test_invalid_commit_target_records_scope_error(
    revision_repository: NestedRepository, tmp_path: Path, target: str
) -> None:
    """Reject both unresolved revisions and non-object rev-parse output with a diagnostic artifact."""
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), "--scope", "commit", "--target", target, "--out", str(tmp_path / "out")],
        cwd=revision_repository.nested,
        env=revision_repository.environment,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert (tmp_path / "out" / "scope-error.txt").read_text(encoding="utf-8") == "invalid-commit-target\n"
