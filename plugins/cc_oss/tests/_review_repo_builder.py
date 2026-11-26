"""Build small real git repositories with a base and a head commit for the reviewer-environment and claim tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Identity and signing settings for test commits; a developer's global config must not change what a test sees.
_GIT_CONFIG = ("-c", "user.name=review-test", "-c", "user.email=review-test@example.invalid", "-c",
               "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false")  # fmt: skip


def git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` with test-only settings and return its stripped stdout."""
    done = subprocess.run(
        ["git", *_GIT_CONFIG, "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=60
    )
    return done.stdout.strip()


def init_repo(path: Path) -> Path:
    """Create an empty repository at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet")
    return path


def commit(repo: Path, files: dict[str, str | None], message: str = "change") -> str:
    """Write (or, for ``None``, delete) files, commit them all and return the commit id."""
    for rel, text in files.items():
        target = repo / rel
        if text is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def pyproject(
    name: str = "demo", deps: tuple[str, ...] = (), extras: dict[str, list[str]] | None = None, tail: str = ""
) -> str:
    """Render a minimal ``pyproject.toml`` with runtime dependencies, extras and an optional raw tail."""
    lines = ["[project]", f'name = "{name}"', 'version = "0.1.0"', "dependencies = ["]
    lines += [f'  "{d}",' for d in deps] + ["]"]
    if extras:
        lines.append("[project.optional-dependencies]")
        lines += [
            f"{key} = [{', '.join(repr(v).replace(chr(39), chr(34)) for v in vals)}]" for key, vals in extras.items()
        ]
    return "\n".join(lines) + "\n" + tail
