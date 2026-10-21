"""Guard the branch-mutex release on resolve stop paths that never load Phase 3."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_BASH = shutil.which("bash")
_GIT = shutil.which("git")
_SESSION = "stop-path-test"
_FENCE_MARKER = "**Stop-path release**"

#: Stop sites that end the run before ``dispatch-phase3.md`` is loaded, each located by a phrase unique to its line.
_STOP_SITES = (
    pytest.param(
        "SKILL.md", "A conflict agent that returned nothing is `timed_out`", id="step-7b-conflict-agent-timeout"
    ),
    pytest.param("modes/dispatch-phase1.md", "- (d) or unanswered", id="challenge-double-timeout-d"),
    pytest.param("modes/dispatch-phase2.md", "6. (d) Stop", id="group-preview-d"),
)


def _prelude() -> str:
    """Read the prelude, which Step 8 loads before any stop path can run."""
    return (_RESOLVE / "modes/dispatch-prelude.md").read_text(encoding="utf-8")


def _release_fence() -> str:
    """Return the executable stop-path release block from the prelude."""
    prelude = _prelude()
    start = prelude.index("```bash", prelude.index(_FENCE_MARKER)) + len("```bash\n")
    return prelude[start : prelude.index("```", start)]


@pytest.mark.parametrize(("relative_path", "anchor"), _STOP_SITES)
def test_stop_path_points_at_the_always_loaded_release_fence(relative_path: str, anchor: str) -> None:
    """Each stop that skips Phase 3 must name the prelude's release fence, not Phase 3's unloaded cleanup block.

    ``dispatch-phase3.md`` loads only once every Phase 2 group has returned, so a stop before that point told to run
    "Phase 3's cleanup block" could not find it and leaked the branch mutex until the healer's 30-minute cap.
    """
    text = (_RESOLVE / relative_path).read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if anchor in line)

    assert "stop-path release fence" in line
    assert "dispatch-prelude.md" in line
    assert "Phase 3's cleanup block" not in line


@pytest.mark.skipif(_BASH is None or _GIT is None, reason="The release fence runs under Bash against a git repo")
@pytest.mark.parametrize(
    ("holder_csid", "lock_kept"),
    [
        pytest.param(_SESSION, False, id="own-lock-released"),
        pytest.param("another-session", True, id="foreign-lock-kept"),
        pytest.param(None, False, id="no-lock-is-a-clean-noop"),
    ],
)
def test_release_fence_removes_only_this_sessions_lock(
    tmp_path: Path, holder_csid: str | None, lock_kept: bool
) -> None:
    """The fence frees the lock this session took at the prelude and never another session's.

    A real repository on a slash-named branch gives the same ``oss-resolve-<branch>.lock`` path the prelude wrote. The
    lock's third field is the holder session stamp the prelude records; a stop reached without the prelude having run
    must not delete a lock that a concurrent resolve on the same branch holds.
    """
    subprocess.run([_GIT, "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [_GIT, "symbolic-ref", "HEAD", "refs/heads/feature/x"], cwd=tmp_path, check=True, capture_output=True
    )
    lock = tmp_path / ".git" / "oss-resolve-feature-x.lock"
    if holder_csid is not None:
        lock.write_text(f"4242 2026-01-01T00:00:00Z {holder_csid}\n", encoding="utf-8", newline="\n")

    completed = subprocess.run(
        [_BASH, "-c", _release_fence()],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": _SESSION, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert lock.exists() is lock_kept
