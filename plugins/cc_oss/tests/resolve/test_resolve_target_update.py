"""Run the Step 5 merge block from conflict-resolution.md against real repositories.

The block must refresh the target branch — both `origin/<base>` and the local `<base>` branch — before merging it into
the PR branch, and must never let a stale or diverged local target leak into the merge.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_MODE = Path(__file__).resolve().parents[2] / "skills/resolve/modes/conflict-resolution.md"
_GIT = shutil.which("git")
_BASH = shutil.which("bash")
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

requires_git_bash = pytest.mark.skipif(_GIT is None or _BASH is None, reason="needs git and bash")


def _git(cwd: Path, *args: str) -> str:
    """Run one git command and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    """Add one file and commit it."""
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


def _merge_block() -> str:
    """Return the Case B fenced bash block that updates both branches and merges."""
    text = _MODE.read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    return next(block for block in blocks if "# 2. update target branch" in block)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Build origin + clone, then advance origin's target past the clone's local target."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "base.txt")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "-b", "feature")
    _commit(clone, "feature.txt")
    _git(seed, "remote", "add", "origin", str(origin))
    _commit(seed, "target-new.txt")
    _git(seed, "push", "-q", "origin", "main")
    return clone, seed


def _run_block(clone: Path) -> subprocess.CompletedProcess[str]:
    """Execute the merge block on the PR branch with the skill's variables bound."""
    env = {**_ENV, "BASE_REF": "main", "HEAD_REF": "feature", "FORK_REMOTE": "origin"}
    return subprocess.run(["bash", "-c", _merge_block()], cwd=clone, env=env, capture_output=True, text=True)


@pytest.mark.integration
@requires_git_bash
def test_merge_block_updates_local_target_and_merges_fresh_target(tmp_path: Path) -> None:
    """A stale local target is fast-forwarded, and the merge brings in the newest target commit."""
    clone, seed = _setup(tmp_path)
    fresh = _git(seed, "rev-parse", "main")
    assert _git(clone, "rev-parse", "main") != fresh

    result = _run_block(clone)

    assert result.returncode == 0, result.stderr
    assert _git(clone, "rev-parse", "main") == fresh
    assert _git(clone, "rev-parse", "MERGE_HEAD") == fresh
    assert (clone / "target-new.txt").exists()


@pytest.mark.integration
@requires_git_bash
def test_merge_block_keeps_diverged_local_target_and_still_merges_remote(tmp_path: Path) -> None:
    """Local target commits are never rewritten; the merge still uses the fetched remote target."""
    clone, seed = _setup(tmp_path)
    _git(clone, "checkout", "-q", "main")
    _commit(clone, "local-only.txt")
    local = _git(clone, "rev-parse", "main")
    _git(clone, "checkout", "-q", "feature")

    result = _run_block(clone)

    assert result.returncode == 0, result.stderr
    assert "local main not updated" in result.stdout
    assert _git(clone, "rev-parse", "main") == local
    assert _git(clone, "rev-parse", "MERGE_HEAD") == _git(seed, "rev-parse", "main")
    assert not (clone / "local-only.txt").exists()
