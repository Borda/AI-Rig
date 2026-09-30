"""Regression checks for the local target-branch update before an authorized remediation merge."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
_requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git executable not available")


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one git command in an isolated configuration."""
    return subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, check=check, capture_output=True, text=True)


def _rev(cwd: Path, ref: str) -> str:
    """Return the object ID one ref points to."""
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    """Add one file and commit it."""
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def fetched_target(tmp_path: Path) -> tuple[Path, str]:
    """Clone on a PR branch whose target advanced upstream and was fetched by object ID only."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "base.txt")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "checkout", "-q", "-b", "feature")
    _commit(upstream, "target-new.txt")
    _git(clone, "fetch", "-q", "--no-tags", "--refmap=", "origin", "main")
    return clone, _rev(clone, "FETCH_HEAD")


def _update_local_target(clone: Path, oid: str) -> subprocess.CompletedProcess[str]:
    """Run the documented local, fast-forward-only target update."""
    return _git(clone, "fetch", ".", f"{oid}:refs/heads/main", check=False)


def test_merge_step_updates_local_target_before_merging_fetched_object() -> None:
    """The merge step fast-forwards the local target, then merges the fetched object, never the local branch.

    Keeping the maintainer's target branch current is best effort; the merge source must stay the immutable fetched
    target so a stale or diverged local branch never reaches the PR.
    """
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "run `git fetch . <base-remote-ref>:refs/heads/<base_ref>` as argv" in skill
    assert "with no second network fetch" in skill
    assert "never forces it, and never blocks the merge" in skill
    assert "always the fetched target object, never the local `<base_ref>` branch" in skill
    assert "local target-branch update outcome" in skill


@pytest.mark.integration
@_requires_git
def test_local_target_update_fast_forwards_stale_branch(fetched_target: tuple[Path, str]) -> None:
    """A stale local target branch moves to the fetched object without any network access."""
    clone, oid = fetched_target

    result = _update_local_target(clone, oid)

    assert result.returncode == 0, result.stderr
    assert _rev(clone, "main") == oid


@pytest.mark.integration
@_requires_git
def test_local_target_update_keeps_diverged_branch(fetched_target: tuple[Path, str]) -> None:
    """A local target with its own commits is refused and left untouched."""
    clone, oid = fetched_target
    _git(clone, "checkout", "-q", "main")
    _commit(clone, "local-only.txt")
    _git(clone, "checkout", "-q", "feature")
    local = _rev(clone, "main")

    result = _update_local_target(clone, oid)

    assert result.returncode != 0
    assert _rev(clone, "main") == local


@pytest.mark.integration
@_requires_git
def test_local_target_update_keeps_branch_checked_out_elsewhere(
    fetched_target: tuple[Path, str], tmp_path: Path
) -> None:
    """A target branch checked out in another worktree is refused and left untouched."""
    clone, oid = fetched_target
    _git(clone, "worktree", "add", "-q", str(tmp_path / "other"), "main")
    local = _rev(clone, "main")

    result = _update_local_target(clone, oid)

    assert result.returncode != 0
    assert _rev(clone, "main") == local
