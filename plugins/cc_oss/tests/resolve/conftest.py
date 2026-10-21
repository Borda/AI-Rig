"""Shared fixtures for the resolve commit-helper tests.

The commit helpers act on the current working directory, so a real-repository test must run them from inside a scratch
repository: ``scratch_repo`` builds one under ``tmp_path``, changes into it, and isolates git from the machine's global
and system configuration so that no user hook, signing setting or template can run.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import which

import pytest

#: Identity used for every scratch-repository commit.
_IDENTITY = {"NAME": "Scratch Test", "EMAIL": "scratch@example.invalid"}


@dataclass(frozen=True)
class ScratchRepo:
    """A throwaway git repository and a runner bound to it.

    Attributes:
        path: Repository root, also the current working directory for the test.
        git: Runs ``git -C <path> <args>`` and returns stdout; raises on a non-zero exit.
    """

    path: Path
    git: Callable[..., str]


@pytest.fixture
def scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ScratchRepo:
    """Create an isolated repository with one base commit on ``main`` and enter it."""
    git_exe = which("git")
    assert git_exe is not None, "git is required for the scratch-repository tests"
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        for field, value in _IDENTITY.items():
            monkeypatch.setenv(f"GIT_{role}_{field}", value)
    repo = tmp_path / "repo"
    repo.mkdir()

    def run_git(*args: str) -> str:
        """Run one git command inside the scratch repository."""
        proc = subprocess.run([git_exe, "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=30)
        return proc.stdout

    run_git("init", "-q", "-b", "main")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    run_git("add", "base.txt")
    run_git("commit", "-q", "-m", "base")
    monkeypatch.chdir(repo)
    return ScratchRepo(path=repo, git=run_git)
