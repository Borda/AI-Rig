"""Verify that remediation integrates the fetched target into the attached PR branch."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in an isolated configuration."""
    return subprocess.run(["git", *args], cwd=cwd, env=_GIT_ENV, check=True, capture_output=True, text=True)


def _rev(cwd: Path, ref: str) -> str:
    """Return the object ID one ref points to."""
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    """Add one file and commit it."""
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.mark.integration
@_requires_git
def test_merge_integrates_fetched_target_without_updating_local_target(tmp_path: Path) -> None:
    """The merge uses the fetched commit and moves only the PR branch in the linked checkout."""
    skill = read_with_references(CODE_REMEDIATE_SKILL, "selection.md")
    assert "git fetch . <base-remote-ref>:refs/heads/<base_ref>" not in skill
    assert "The PR branch is the only branch ref this integration may move" in skill
    commands = set(re.findall(r"`(git merge --no-commit [^`]*)`", skill))
    assert len(commands) == 1, commands
    (command,) = commands
    assert "<base-remote-ref>" in command
    assert "--no-ff" in command

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "base.txt")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "checkout", "-q", "-b", "invoking")
    pr_checkout = tmp_path / "pr"
    _git(clone, "worktree", "add", "-q", "-b", "feature", str(pr_checkout))
    _commit(pr_checkout, "feature.txt")
    pr_head = _rev(pr_checkout, "HEAD")
    other_refs = {ref: _rev(clone, ref) for ref in ("main", "invoking", "origin/main")}

    _commit(upstream, "target-new.txt")
    target_oid = _rev(upstream, "HEAD")
    _git(pr_checkout, "fetch", "-q", "--no-tags", "--refmap=", "--no-write-fetch-head", "origin", target_oid)
    _git(pr_checkout, *command.replace("<base-remote-ref>", target_oid).split()[1:])
    _git(pr_checkout, "commit", "-q", "-m", "Integrate fetched target")

    assert {ref: _rev(clone, ref) for ref in other_refs} == other_refs
    assert _rev(pr_checkout, "HEAD^1") == pr_head
    assert _rev(pr_checkout, "HEAD^2") == target_oid
    assert (pr_checkout / "target-new.txt").is_file()
    assert (pr_checkout / "feature.txt").is_file()
