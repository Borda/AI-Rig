"""The merge-back cleanup records every specialist branch it keeps, so the final report can list it for cleanup."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _text_io import write_lf

_PHASE3 = Path(__file__).resolve().parents[3] / "skills/resolve/modes/dispatch-phase3.md"
_BASH = shutil.which("bash")
_SESSION = "resolve-recovery-record-test"
pytestmark = pytest.mark.skipif(_BASH is None or shutil.which("git") is None, reason="the cleanup fence is Bash")
_ENV = {
    **os.environ,
    "CLAUDE_CODE_SESSION_ID": _SESSION,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _cleanup_fence() -> str:
    """Return the shipped worktree cleanup fence of the merge-back."""
    text = _PHASE3.read_text(encoding="utf-8")
    position = text.index('done < "$IMPL_DIR/specialist-worktrees.txt"')
    start = text.rindex("```bash\n", 0, position) + len("```bash\n")
    return text[start : text.index("```", position)]


def _git(cwd: Path, *args: str) -> str:
    """Run one git command and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(
    ("mode", "reason"),
    [pytest.param("grouped", "uncommitted", id="grouped"), pytest.param("each", "unconfirmed", id="each")],
)
def test_kept_branch_is_recorded_with_its_reason(tmp_path: Path, mode: str, reason: str) -> None:
    """Specialist branches with content missing from HEAD survive merge-back even when patch IDs match."""
    repo, tmp, impl = tmp_path / "repo", tmp_path / "tmp", tmp_path / "impl"
    for directory in (repo, tmp, impl):
        directory.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "core.py")
    _git(repo, "commit", "-q", "-m", "base")
    worktree = tmp_path / "agent-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "worktree-agent-a1", str(worktree))
    write_lf(worktree / "core.py", "VALUE  = 2\n")
    _git(worktree, "commit", "-qam", "specialist work")
    write_lf(repo / "core.py", "VALUE = 2\n")
    _git(repo, "commit", "-qam", "equivalent patch id")
    # LF only, as the run writes them: the fence's `read -r` keeps a Windows carriage return in each path
    write_lf(impl / "specialist-worktrees.txt", f"{worktree}\n")
    write_lf(tmp / f"resolve-impl-dir-{_SESSION}", f"{impl}\n")
    write_lf(tmp / f"resolve-commit-mode-{_SESSION}", f"{mode}\n")

    assert _BASH is not None
    result = subprocess.run(
        [_BASH, "-c", _cleanup_fence()],
        cwd=repo,
        env=_ENV | {"TMPDIR": str(tmp)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (impl / "recovery-branches.txt").read_text(encoding="utf-8") == f"worktree-agent-a1\t{reason}\n"
    assert _git(repo, "branch", "--list", "worktree-agent-a1") != "", "the branch is kept, never deleted"
