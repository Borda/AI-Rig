"""Pin how release evidence resolves its pinned head coordinates against real Git repositories."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _git(repository: Path, *arguments: str) -> str:
    """Run one Git command in a fixture repository and return its trimmed output."""
    completed = subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


@pytest.fixture
def evidence(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the shipped release evidence helper with its sibling imports resolvable."""
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    spec = importlib.util.spec_from_file_location(
        "release_evidence_under_test", PLUGIN_ROOT / "shared/release_evidence.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def repositories(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Build one committed source repository plus empty, bare, and non-Git directories, once per module."""
    root = tmp_path_factory.mktemp("release-evidence-rev-parse")
    source, empty, bare, plain = (root / name for name in ("source", "empty", "bare.git", "plain"))
    for directory in (source, empty, bare, plain):
        directory.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Release Test")
    _git(source, "config", "user.email", "release@example.invalid")
    _git(source, "commit", "--allow-empty", "-q", "-m", "fixture")
    _git(empty, "init", "-q")
    _git(bare, "init", "-q", "--bare")
    return SimpleNamespace(
        source=source,
        empty=empty,
        bare=bare,
        plain=plain,
        head=_git(source, "rev-parse", "HEAD"),
        tree=_git(source, "rev-parse", "HEAD^{tree}"),
        absent="b" * 40,
        wrong="0" * 40,
    )


@pytest.mark.integration
def test_pinned_head_coordinates_resolve_in_one_git_process(
    evidence: ModuleType, repositories: SimpleNamespace
) -> None:
    """A passing receipt asks Git for its work-tree, HEAD, and tree answers in a single call.

    The three probes used to be separate processes, which dominates release validation time on hosts where process
    creation is slow. Candidate accounting after them must keep working, so the scope is validated end to end.
    """
    receipt = {
        "repository": str(repositories.source),
        "final_tree": repositories.tree,
        "baseline": None,
        "candidates": [{"sha": repositories.head, "disposition": "internal-only", "reason": "fixture"}],
    }

    with mock.patch.object(subprocess, "run", wraps=subprocess.run) as spawned:
        repository, expected, included = evidence._validate_scope(receipt, {"release_head": repositories.head})

    rev_parse_calls = [call for call in spawned.call_args_list if "rev-parse" in call.args[0]]
    assert len(rev_parse_calls) == 1
    assert (repository, expected, included) == (repositories.source, {repositories.head}, set())


@pytest.mark.parametrize(
    ("repository", "head", "tree", "reason"),
    [
        pytest.param("source", "absent", "tree", "head", id="absent-head"),
        pytest.param("source", "head", "wrong", "final-tree", id="wrong-final-tree"),
        pytest.param("empty", "absent", "tree", "git:rev-parse", id="repository-without-commits"),
        pytest.param("bare", "head", "tree", "repository", id="bare-repository"),
        pytest.param("plain", "head", "tree", "git:rev-parse", id="not-a-repository"),
    ],
)
@pytest.mark.integration
def test_pinned_head_failures_keep_their_reason(
    evidence: ModuleType,
    repositories: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    head: str,
    tree: str,
    reason: str,
) -> None:
    """Each rejected coordinate reports the step that rejected it, even when Git refuses part of a combined query.

    A well-formed head that is absent from the repository, an unborn HEAD, a bare repository, and a directory outside
    any repository all make a combined Git query fail or answer unexpectedly. Attributing that to the combined query
    would change the stable failure reason, so each case must still fail exactly where one-at-a-time probing did.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repositories.plain.parent))
    receipt = {
        "repository": str(getattr(repositories, repository)),
        "final_tree": getattr(repositories, tree),
        "baseline": None,
        "candidates": [],
    }

    with pytest.raises(SystemExit, match=f"^release-evidence-{reason}$"):
        evidence._validate_scope(receipt, {"release_head": getattr(repositories, head)})
