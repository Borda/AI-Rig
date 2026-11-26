"""The end-of-run remote check names what the PR branch holds after a run that did not push."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/resolve_remote_state.py"
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("git") is None, reason="compares real refs on a local bare remote"),
]


def _git(cwd: Path, *args: str) -> str:
    """Run one git command and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    """Commit one new file and return the commit id."""
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def run(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return ``(work, other, start)``: the run's checkout, a second clone, and the PR head the run started from."""
    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    _git(tmp_path, "init", "-q", "--bare", "-b", "feature", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "switch", "-q", "-c", "feature")
    start = _commit(work, "base.txt")
    _git(work, "push", "-q", "origin", "feature")
    _git(tmp_path, "clone", "-q", "-b", "feature", str(origin), str(other))
    return work, other, start


def _state(
    work: Path,
    start: str,
    push_status: str = "skipped-by-user",
    remote: str = "origin",
    *,
    history: str | None = None,
) -> dict[str, str]:
    """Run the helper and return its ``KEY=value`` lines."""
    imports = work.parent / "remote imports"
    imports.write_text(f"base {start} {start}\n" if history is None else history, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--push-status",
            push_status,
            f"--remote={remote}",
            "--branch",
            "feature",
            "--start-head",
            start,
            f"--imports-file={imports}",
        ],
        cwd=work,
        env=_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in proc.stdout.splitlines())


def test_unpushed_commits_leave_the_remote_unchanged(run: tuple[Path, Path, str]) -> None:
    """A skipped push with the remote still at the run's start reports the commits as local only."""
    work, _, start = run
    _commit(work, "fix.txt")
    assert _state(work, start)["REMOTE_STATE"] == "unchanged"


def test_remote_at_local_head_is_pushed_externally(run: tuple[Path, Path, str]) -> None:
    """The remote branch already at the run's HEAD after a skipped push is reported as pushed from elsewhere."""
    work, _, start = run
    _commit(work, "fix.txt")
    _git(work, "push", "-q", "origin", "HEAD:feature")
    assert _state(work, start)["REMOTE_STATE"] == "pushed-externally"


def test_remote_holding_part_of_the_run_is_pushed_externally(run: tuple[Path, Path, str]) -> None:
    """A remote branch carrying some of the run's commits names how many it carries."""
    work, _, start = run
    _commit(work, "one.txt")
    _commit(work, "two.txt")
    _git(work, "push", "-q", "origin", "HEAD~1:feature")
    state = _state(work, start)
    assert state["REMOTE_STATE"] == "pushed-externally"
    assert "carries 1 of this run's 2 commits" in state["REMOTE_DETAIL"]


def test_remote_and_local_tips_share_only_part_of_the_run(run: tuple[Path, Path, str]) -> None:
    """A remote-only commit does not erase the shared run commit or count toward delivery."""
    work, other, start = run
    shared = _commit(work, "shared.txt")
    _git(work, "push", "-q", "origin", f"{shared}:feature")
    _commit(work, "local.txt")
    _git(other, "pull", "-q", "--ff-only")
    _commit(other, "remote.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")

    state = _state(work, start, "rejected-non-ff")

    assert state["REMOTE_STATE"] == "pushed-externally"
    assert "carries 1 of this run's 2 commits" in state["REMOTE_DETAIL"]


def test_independent_base_merges_do_not_count_shared_base_commits(run: tuple[Path, Path, str]) -> None:
    """The base commits on both tips provide no evidence that either local resolution commit was delivered."""
    work, other, start = run
    _git(other, "switch", "-qc", "main", start)
    _commit(other, "base-one.txt")
    base = _commit(other, "base-two.txt")
    _git(other, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")
    _git(work, "merge", "-q", "--no-ff", "-m", "local base merge", "origin/main")
    _commit(work, "fix.txt")
    _git(other, "switch", "-q", "feature")
    _git(other, "merge", "-q", "--no-ff", "-m", "remote base merge", base)
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")

    assert _state(work, start, history=f"base {start} {base}\n")["REMOTE_STATE"] == "diverged"


@pytest.mark.parametrize("with_fix", [True, False])
def test_pulled_commits_do_not_count_as_run_delivery(run: tuple[Path, Path, str], with_fix: bool) -> None:
    """A fast-forward pull imports the remote's work without giving the run credit for delivering it."""
    work, other, start = run
    imported = _commit(other, "pulled.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "pull", "-q", "--ff-only", "origin", "feature")
    if with_fix:
        _commit(work, "fix.txt")

    history = f"pull-start {start} {start}\npull-end {start} {imported}\nbase {imported} {start}\n"
    state = _state(work, start, "rejected-non-ff", history=history)

    assert state["REMOTE_STATE"] == ("diverged" if with_fix else "moved")


def test_fast_forward_pull_excludes_only_new_commits(run: tuple[Path, Path, str]) -> None:
    """A fast-forward that includes earlier run work counts only that work as delivered."""
    work, other, start = run
    authored = _commit(work, "first-fix.txt")
    _git(work, "push", "-q", "origin", f"{authored}:feature")
    _git(other, "pull", "-q", "--ff-only")
    imported = _commit(other, "peer.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "pull", "-q", "--ff-only", "origin", "feature")
    assert _git(work, "rev-parse", "HEAD") == imported
    _commit(work, "last-fix.txt")

    history = f"base {start} {start}\npull-start {authored} {authored}\npull-end {authored} {imported}\n"
    state = _state(work, start, "rejected-non-ff", history=history)

    assert state["REMOTE_STATE"] == "pushed-externally"
    assert "carries 1 of this run's 2 commits" in state["REMOTE_DETAIL"]


def test_merge_pull_preserves_earlier_run_commits_on_diverged_tips(run: tuple[Path, Path, str]) -> None:
    """The merged side and pull merge add no delivery credit when the later tips diverge."""
    work, other, start = run
    shared = _commit(work, "shared-fix.txt")
    _git(work, "push", "-q", "origin", f"{shared}:feature")
    _git(other, "pull", "-q", "--ff-only")
    before = _commit(work, "local-fix.txt")
    foreign = _commit(other, "peer.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "pull", "-q", "--no-rebase", "origin", "feature")
    merged = _git(work, "rev-parse", "HEAD")
    assert _git(work, "rev-parse", "HEAD^1", "HEAD^2").splitlines() == [before, foreign]
    _commit(work, "last-fix.txt")
    _commit(other, "another-peer.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "merge-base", "HEAD", "origin/feature") == foreign

    history = f"base {start} {start}\npull-start {before} {before}\npull-end {before} {merged}\n"
    state = _state(work, start, "rejected-non-ff", history=history)

    assert state["REMOTE_STATE"] == "pushed-externally"
    assert "carries 1 of this run's 3 commits" in state["REMOTE_DETAIL"]


@pytest.mark.parametrize(
    ("history", "reason"),
    [
        pytest.param("", "empty or truncated", id="empty-ledger"),
        pytest.param("base {start} {start}", "empty or truncated", id="truncated-record"),
        pytest.param("base {start} -bad\n", "malformed record", id="malformed-object-id"),
        pytest.param("base {start} {missing}\n", "unavailable commit", id="unknown-object-id"),
        pytest.param("base {start} {side}\n", "outside the recorded local history", id="unmerged-base"),
        pytest.param("base {side} {start}\n", "does not preserve", id="discarded-checkout-head"),
        pytest.param("pull-start {start} {start}\n", "missing a base or pull-end", id="unmatched-start"),
        pytest.param("pull-end {start} {local}\n", "unexpected record", id="unmatched-end"),
        pytest.param(
            "base {start} {start}\npull-start {start} {start}\npull-end {local} {local}\n",
            "unmatched pull-start",
            id="mismatched-pull",
        ),
        pytest.param(
            "base {start} {start}\npull-start {local} {local}\npull-end {local} {start}\n",
            "pull rewrote",
            id="rewound-pull",
        ),
    ],
)
def test_same_tip_without_proven_history_is_inconclusive(
    run: tuple[Path, Path, str], history: str, reason: str
) -> None:
    """Tip equality alone cannot distinguish authored work from an external commit brought in by a pull."""
    work, _, start = run
    _git(work, "switch", "-qc", "side")
    side = _commit(work, "side.txt")
    _git(work, "switch", "-q", "feature")
    local = _commit(work, "fix.txt")
    _git(work, "push", "-q", "origin", "HEAD:feature")

    bounds = history.format(start=start, local=local, side=side, missing="0" * len(start))
    state = _state(work, start, history=bounds)

    assert state["REMOTE_STATE"] == "unknown"
    assert reason in state["REMOTE_DETAIL"]


def test_shallow_history_cannot_prove_delivery(run: tuple[Path, Path, str]) -> None:
    """Missing parent edges make ancestry inconclusive even with the starting and observed tips present."""
    work, _, _ = run
    start = _commit(work, "run-start.txt")
    authored = _commit(work, "fix.txt")
    _git(work, "push", "-q", "origin", "feature")
    shallow = work.parent / "shallow"
    _git(work, "clone", "-q", "--depth=2", (work.parent / "origin.git").as_uri(), str(shallow))
    assert _git(shallow, "rev-parse", "HEAD", "HEAD^").splitlines() == [authored, start]
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"

    state = _state(shallow, start)

    assert state["REMOTE_STATE"] == "unknown"
    assert "complete commit ancestry is unavailable" in state["REMOTE_DETAIL"]


def test_remote_ahead_of_the_run_is_pushed_externally(run: tuple[Path, Path, str]) -> None:
    """A remote branch that carries the run's commits plus later ones is reported with the extra count."""
    work, other, start = run
    _commit(work, "fix.txt")
    _git(work, "push", "-q", "origin", "HEAD:feature")
    _git(other, "pull", "-q", "--ff-only")
    _commit(other, "later.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")
    state = _state(work, start, "rejected-non-ff")
    assert state["REMOTE_STATE"] == "pushed-externally"
    assert "plus 1 more" in state["REMOTE_DETAIL"]


def test_remote_moved_elsewhere_has_diverged(run: tuple[Path, Path, str]) -> None:
    """A remote branch that moved without the run's commits is reported as diverged."""
    work, other, start = run
    _commit(work, "fix.txt")
    _commit(other, "theirs.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")
    assert _state(work, start)["REMOTE_STATE"] == "diverged"


def test_unfetched_remote_commit_is_unknown(run: tuple[Path, Path, str]) -> None:
    """A remote head this checkout has not fetched cannot be compared, so the state is unknown, never guessed."""
    work, other, start = run
    _commit(work, "fix.txt")
    _commit(other, "theirs.txt")
    _git(other, "push", "-q", "origin", "feature")
    state = _state(work, start)
    assert state["REMOTE_STATE"] == "unknown"
    assert "fetch to compare" in state["REMOTE_DETAIL"]


def test_remote_moved_while_the_run_made_no_commits(run: tuple[Path, Path, str]) -> None:
    """With no commits of its own, the run reports a moved remote branch rather than a divergence."""
    work, other, start = run
    _commit(other, "theirs.txt")
    _git(other, "push", "-q", "origin", "feature")
    _git(work, "fetch", "-q", "origin")
    assert _state(work, start)["REMOTE_STATE"] == "moved"


def test_deleted_remote_branch_is_absent(run: tuple[Path, Path, str]) -> None:
    """A remote branch that no longer exists is named as absent."""
    work, other, start = run
    _commit(work, "fix.txt")
    _git(other, "switch", "-q", "-c", "keep")
    _git(other, "push", "-q", "origin", "keep")
    _git(work.parent / "origin.git", "symbolic-ref", "HEAD", "refs/heads/keep")  # a bare remote refuses to delete HEAD
    _git(other, "push", "-q", "origin", "--delete", "feature")
    assert _state(work, start)["REMOTE_STATE"] == "absent"


@pytest.mark.parametrize(
    ("push_status", "remote", "expected"),
    [
        pytest.param("pushed", "origin", "n/a", id="run-pushed"),
        pytest.param("skipped-by-user", "", "n/a", id="no-pr-branch"),
        pytest.param("skipped-by-user", "missing", "unknown", id="unreadable-remote"),
        pytest.param("skipped-by-user", "-x", "unknown", id="option-shaped-remote"),
    ],
)
def test_cases_without_a_comparison(run: tuple[Path, Path, str], push_status: str, remote: str, expected: str) -> None:
    """A run that pushed or has no branch is not compared; an unreadable or malformed remote is unknown."""
    work, _, start = run
    _commit(work, "fix.txt")
    assert _state(work, start, push_status, remote)["REMOTE_STATE"] == expected
