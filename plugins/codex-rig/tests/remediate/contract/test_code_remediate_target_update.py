"""Regression checks for the local target-branch update before an authorized remediation merge.

Before merging, remediation fast-forwards the maintainer's local target branch to the already fetched target object
without any further network access. The instruction text pins that contract; real `git` runs pin its behaviour for a
stale, diverged and elsewhere-checked-out local target.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
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


def _diverge_local_target(clone: Path, tmp_path: Path) -> None:
    """Give the local target branch a commit of its own, so it can no longer fast-forward."""
    _git(clone, "checkout", "-q", "main")
    _commit(clone, "local-only.txt")
    _git(clone, "checkout", "-q", "feature")


def _check_out_target_elsewhere(clone: Path, tmp_path: Path) -> None:
    """Check the local target branch out in a second worktree, so git refuses to move it."""
    _git(clone, "worktree", "add", "-q", str(tmp_path / "other"), "main")


def test_merge_step_updates_local_target_before_merging_fetched_object() -> None:
    """The merge step fast-forwards the local target, then merges the fetched object, never the local branch.

    Keeping the maintainer's target branch current is best effort; the merge source must stay the immutable fetched
    target so a stale or diverged local branch never reaches the PR.
    """
    skill = " ".join(
        read_with_references(
            CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md"
        ).split()
    )
    phrases = (
        "run `git fetch . <base-remote-ref>:refs/heads/<base_ref>` as argv",
        "with no second network fetch",
        "never forces it, and never blocks the merge",
        "always the fetched target object, never the local `<base_ref>` branch",
        "local target-branch update outcome",
    )

    assert [phrase for phrase in phrases if phrase not in skill] == []


@pytest.mark.integration
@_requires_git
class TestLocalTargetUpdate:
    """The documented `git fetch .

    <oid>:refs/heads/<base>` moves a stale local target and refuses anything else.
    """

    def test_fast_forwards_stale_branch(self, fetched_target: tuple[Path, str]) -> None:
        """A stale local target branch moves to the fetched object without any network access.

        The clone's `main` is behind upstream; the object was fetched by ID only, so the update must succeed from the
        local object store alone.
        """
        clone, oid = fetched_target

        result = _update_local_target(clone, oid)

        assert (result.returncode, _rev(clone, "main")) == (0, oid), result.stderr

    @pytest.mark.parametrize(
        "blocker",
        [
            pytest.param(_diverge_local_target, id="diverged-branch"),
            pytest.param(_check_out_target_elsewhere, id="checked-out-elsewhere"),
        ],
    )
    def test_keeps_blocked_branch_untouched(
        self, fetched_target: tuple[Path, str], tmp_path: Path, blocker: Callable[[Path, Path], None]
    ) -> None:
        """A local target with its own commits, or checked out in another worktree, is refused and left untouched.

        Updating the maintainer's branch is best effort, so git's refusal must leave the branch exactly where it was.
        """
        clone, oid = fetched_target
        blocker(clone, tmp_path)
        local = _rev(clone, "main")

        result = _update_local_target(clone, oid)

        assert (result.returncode != 0, _rev(clone, "main")) == (True, local)
