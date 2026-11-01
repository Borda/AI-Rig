"""Guard the branch-mutex release on resolve stop paths that never load Phase 3."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[3] / "skills/resolve"
_BASH = shutil.which("bash")
_GIT = shutil.which("git")
_SESSION = "stop-path-test"
_FENCE_MARKER = "**Stop-path release**"
_LOCK_BODY = "4242 2026-01-01T00:00:00Z {holder}\n"
_requires_bash_git = pytest.mark.skipif(
    _BASH is None or _GIT is None, reason="The release fence runs under Bash against a git repo"
)

#: Stop sites that end the run before ``dispatch-phase3.md`` is loaded, each located by a phrase unique to its line.
_STOP_SITES = (
    pytest.param(
        "SKILL.md", "A conflict agent that returned nothing is `timed_out`", id="step-7b-conflict-agent-timeout"
    ),
    pytest.param("modes/dispatch-phase1.md", "- (d) or unanswered", id="challenge-double-timeout-d"),
    pytest.param("modes/dispatch-phase2.md", "6. (d) Stop", id="group-preview-d"),
)


def _release_fence() -> str:
    """Return the executable stop-path release block from the prelude, which Step 8 loads before any stop can run."""
    prelude = (_RESOLVE / "modes/dispatch-prelude.md").read_text(encoding="utf-8")
    start = prelude.index("```bash", prelude.index(_FENCE_MARKER)) + len("```bash\n")
    return prelude[start : prelude.index("```", start)]


def _git(repo: Path, *args: str) -> None:
    """Run one git command in the scratch repository, failing the test on a non-zero exit."""
    subprocess.run([_GIT, *args], cwd=repo, check=True, capture_output=True)


def _run_release_fence(lock: Path) -> subprocess.CompletedProcess[str]:
    """Run the release fence from the repository root that owns ``lock``, as this test session."""
    root = lock.parents[1]
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": _SESSION, "TMPDIR": str(root)}
    return subprocess.run(
        [_BASH, "-c", _release_fence()], cwd=root, env=env, capture_output=True, text=True, check=False, timeout=30
    )


@pytest.fixture
def slash_branch_repo(tmp_path: Path) -> Path:
    """Create a real repository on the slash-named branch ``feature/x`` and return the lock path the prelude uses."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "symbolic-ref", "HEAD", "refs/heads/feature/x")
    return tmp_path / ".git" / "oss-resolve-feature-x.lock"


class TestStopPathRelease:
    """Pin the stop-path fence that releases only this session's lock."""

    @pytest.mark.parametrize(("relative_path", "anchor"), _STOP_SITES)
    def test_stop_site_names_the_prelude_release_fence(self, relative_path: str, anchor: str) -> None:
        """Each stop that skips Phase 3 names the prelude's release fence, not Phase 3's unloaded cleanup block.

        ``dispatch-phase3.md`` loads only once every Phase 2 group has returned, so a stop before that point told to run
        "Phase 3's cleanup block" could not find it and leaked the branch mutex until the healer's 30-minute cap.
        """
        text = (_RESOLVE / relative_path).read_text(encoding="utf-8")
        line = next(line for line in text.splitlines() if anchor in line)
        assert "stop-path release fence" in line
        assert "dispatch-prelude.md" in line
        assert "Phase 3's cleanup block" not in line

    @_requires_bash_git
    @pytest.mark.parametrize(
        ("holder_csid", "lock_kept"),
        [
            pytest.param(_SESSION, False, id="own-lock-released"),
            pytest.param("another-session", True, id="foreign-lock-kept"),
        ],
    )
    def test_removes_only_this_sessions_lock(self, slash_branch_repo: Path, holder_csid: str, lock_kept: bool) -> None:
        """The fence frees the lock this session took at the prelude and never another session's.

        A real repository on a slash-named branch gives the same ``oss-resolve-<branch>.lock`` path the prelude wrote.
        The lock's third field is the holder session stamp the prelude records; a stop reached without the prelude
        having run must not delete a lock that a concurrent resolve on the same branch holds.
        """
        slash_branch_repo.write_text(_LOCK_BODY.format(holder=holder_csid), encoding="utf-8", newline="\n")
        completed = _run_release_fence(slash_branch_repo)
        assert completed.returncode == 0, completed.stderr
        assert slash_branch_repo.exists() is lock_kept

    @_requires_bash_git
    def test_missing_lock_is_a_clean_noop(self, slash_branch_repo: Path) -> None:
        """A stop reached before any lock was taken exits cleanly and creates nothing.

        The prelude may never have run (an early stop), so the fence must not treat an absent lock file as an error.
        """
        completed = _run_release_fence(slash_branch_repo)
        assert completed.returncode == 0, completed.stderr
        assert not slash_branch_repo.exists()
