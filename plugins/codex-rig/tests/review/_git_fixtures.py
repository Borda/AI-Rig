"""Run real Git against the disposable repositories that the review and loop suites build."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def git_bytes(root: Path, *arguments: str) -> bytes:
    """Run real Git against ``root`` and return its raw standard output, raising when Git fails."""
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True).stdout


def git_text(root: Path, *arguments: str) -> str:
    """Run real Git against ``root`` and return its standard output as text with surrounding whitespace removed."""
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def historical_review_workspace(caller: Path, target: str) -> Path:
    """Reconstruct a pre-worktree-only installation's retained repository and caller location receipt."""
    caller = caller.resolve()
    identity = os.path.normcase(caller.as_posix()) + "\0" + target.lower()
    name = hashlib.sha256(identity.encode("utf-8", errors="surrogateescape")).hexdigest()
    common = Path(git_text(caller, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    root = Path(os.environ.get("XDG_STATE_HOME", str(common))) / "codex-rig" / "review-workspaces" / name
    root.mkdir(mode=0o700, parents=True)
    git_text(root, "init", "-q")
    git_text(root, "remote", "add", "origin", target.rsplit("/pull/", 1)[0] + ".git")
    git_text(root, "config", "codexRig.reviewCaller", str(caller))
    git_text(root, "config", "codexRig.reviewTarget", target.lower())
    location = common / "codex-review-locations" / name / "location"
    location.parent.mkdir(parents=True)
    location.write_bytes(os.fsencode(root.as_posix()))
    return root
