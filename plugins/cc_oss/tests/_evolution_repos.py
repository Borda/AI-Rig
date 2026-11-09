"""Fixture-repository builder for the ``/oss:analyse evolution`` tests.

Builds small, fully deterministic git repositories: fixed author/committer dates, an isolated git configuration (no
global or system config, no hooks, ``core.autocrlf=false``), and a Keep a Changelog renderer. Tests describe history
with :class:`RepoBuilder` and write their expected associations before running any scoring code.

Only test code imports this module; shipped ``bin/`` scripts never do.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which

#: Day zero for fixture timestamps; ``day=N`` means this date plus N days at noon UTC.
_EPOCH = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

#: Identity used for every fixture commit.
_IDENTITY = {"NAME": "Fixture Author", "EMAIL": "fixture@example.invalid"}


@dataclass
class ChangelogSection:
    """One released section of a Keep a Changelog file.

    Attributes:
        version: Version text as written in the heading, e.g. ``"1.1.0"``.
        day: Release day offset used for the heading date.
        added: "Added" entries.
        changed: "Changed" entries.
        fixed: "Fixed" entries.
    """

    version: str
    day: int = 0
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)


def render_changelog(sections: list[ChangelogSection]) -> str:
    """Render sections (newest first, as given) into Keep a Changelog Markdown."""
    lines = ["# Changelog", ""]
    for section in sections:
        date = (_EPOCH + timedelta(days=section.day)).date().isoformat()
        lines += [f"## [{section.version}] - {date}", ""]
        for title, entries in (("Added", section.added), ("Changed", section.changed), ("Fixed", section.fixed)):
            if entries:
                lines += [f"### {title}", "", *[f"- {entry}" for entry in entries], ""]
    return "\n".join(lines)


def isolated_git_env(config_dir: Path) -> dict[str, str]:
    """Return an environment that isolates git from the machine's global and system configuration."""
    config_dir.mkdir(parents=True, exist_ok=True)
    empty = config_dir / "gitconfig"
    empty.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update({"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    for role in ("AUTHOR", "COMMITTER"):
        for key, value in _IDENTITY.items():
            env[f"GIT_{role}_{key}"] = value
    return env


class RepoBuilder:
    """Deterministic git history builder bound to one repository directory.

    Examples:
        >>> # repo = RepoBuilder.create(tmp_path / "repo", tmp_path / "cfg")
        >>> # repo.write("src/a.py", "x = 1\\n"); sha = repo.commit("feat: add a", day=1); repo.tag("v1.0.0")
    """

    def __init__(self, path: Path, env: dict[str, str]) -> None:
        """Bind the builder to an existing repository and its isolated environment."""
        self.path = path
        self.env = env
        git = which("git")
        assert git is not None, "git is required for the evolution fixture repositories"
        self._git = git

    @classmethod
    def create(cls, path: Path, config_dir: Path, branch: str = "main") -> RepoBuilder:
        """Create an empty repository with an isolated config and return its builder."""
        path.mkdir(parents=True, exist_ok=True)
        builder = cls(path, isolated_git_env(config_dir))
        builder.git("init", "-q", "-b", branch)
        builder.git("config", "core.autocrlf", "false")
        builder.git("config", "commit.gpgsign", "false")
        builder.git("config", "tag.gpgsign", "false")
        return builder

    def git(self, *args: str, day: int | None = None) -> str:
        """Run one git command in the repository; ``day`` pins author and committer dates."""
        env = dict(self.env)
        if day is not None:
            stamp = (_EPOCH + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            env["GIT_AUTHOR_DATE"] = stamp
            env["GIT_COMMITTER_DATE"] = stamp
        proc = subprocess.run(
            [self._git, "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )
        assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
        return proc.stdout

    def write(self, rel: str, text: str) -> None:
        """Write a file with LF line endings, creating parent directories."""
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")

    def delete(self, rel: str) -> None:
        """Remove a tracked file from the working tree and index."""
        self.git("rm", "-q", rel)

    def move(self, src: str, dst: str) -> None:
        """Rename a tracked file."""
        (self.path / dst).parent.mkdir(parents=True, exist_ok=True)
        self.git("mv", src, dst)

    def commit(self, message: str, *, day: int, allow_empty: bool = False) -> str:
        """Stage everything and commit at the given day; return the new commit SHA."""
        self.git("add", "-A")
        args = ["commit", "-q", "-m", message] + (["--allow-empty"] if allow_empty else [])
        self.git(*args, day=day)
        return self.head()

    def head(self) -> str:
        """Return the current HEAD SHA."""
        return self.git("rev-parse", "HEAD").strip()

    def tag(self, name: str, *, day: int, ref: str = "HEAD") -> None:
        """Create an annotated tag at ``ref`` dated at ``day``."""
        self.git("tag", "-a", name, "-m", name, ref, day=day)

    def branch(self, name: str, start: str = "HEAD") -> None:
        """Create a branch at ``start`` without switching to it."""
        self.git("branch", name, start)

    def checkout(self, name: str) -> None:
        """Switch to an existing branch."""
        self.git("checkout", "-q", name)

    def merge(self, branch: str, message: str, *, day: int) -> str:
        """Merge ``branch`` with a merge commit (merge workflow); return the merge SHA."""
        self.git("merge", "-q", "--no-ff", "-m", message, branch, day=day)
        return self.head()

    def squash_merge(self, branch: str, message: str, *, day: int) -> str:
        """Squash-merge ``branch`` into one commit (squash workflow); return its SHA."""
        self.git("merge", "-q", "--squash", branch, day=day)
        return self.commit(message, day=day)

    def cherry_pick(self, sha: str, *, day: int) -> str:
        """Cherry-pick ``sha`` onto the current branch; return the new SHA."""
        self.git("cherry-pick", "-x", sha, day=day)
        return self.head()

    def revert(self, sha: str, *, day: int) -> str:
        """Revert ``sha`` on the current branch; return the revert SHA."""
        self.git("revert", "--no-edit", sha, day=day)
        return self.head()

    def short(self, sha: str) -> str:
        """Return the 7-character abbreviation used in changelog references."""
        return sha[:7]

    def write_changelog(self, sections: list[ChangelogSection], rel: str = "CHANGELOG.md") -> None:
        """Write the changelog file (not committed)."""
        self.write(rel, render_changelog(sections))
