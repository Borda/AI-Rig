"""Throwaway Git repositories for the gh-read lane tests.

``hooks/github-read-allow.js`` allows a gh read with no grant or record, inside a repository or outside one. The tests
still run it in a real repository, under an isolated environment, to prove that no record in the git common dir — and
nothing Git would discover from the runner's own checkout — changes the verdict.

Deliberately a uniquely-named module rather than ``conftest``, for the reason ``_audit_harness.py`` gives.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Windows CPython and Git abort at startup without these; see the repository's multi-OS rule.
_WINDOWS_ENV = ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")


def git_env(home: Path, project: Path | None = None) -> dict[str, str]:
    """Return a minimal environment: a throwaway home, the runner's PATH, and ``CLAUDE_PROJECT_DIR`` when given.

    Nothing Git reads for discovery is inherited, so state in the runner's own checkout can never leak in.
    """
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    for name in _WINDOWS_ENV:
        if name in os.environ:
            env[name] = os.environ[name]
    if project is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    return env


def make_repo(base: Path, name: str = "repo") -> Path:
    """Create an empty Git work tree under ``base`` and return its path."""
    repo = base / name
    repo.mkdir(parents=True)
    home = base / "git-home"
    home.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git_env(home), timeout=30)
    return repo
