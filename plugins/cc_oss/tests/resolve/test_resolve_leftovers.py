"""The final report lists the branches, worktrees and stashes a run left behind, with commands, and deletes nothing."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import resolve_leftovers as leftovers

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/resolve_leftovers.py"
_REPORT = _SCRIPT.parents[1] / "skills/resolve/modes/final-report.md"
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
    pytest.mark.skipif(shutil.which("git") is None, reason="reads real branches, worktrees and stashes"),
]


def _git(cwd: Path, *args: str) -> str:
    """Run one git command and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True).stdout.strip()


def _stash(repo: Path, text: str) -> str:
    """Create one stash of an edit to the tracked file and return its commit id."""
    (repo / "core.py").write_text(text, encoding="utf-8")
    _git(repo, "stash", "push", "-q", "-m", text.strip())
    return _git(repo, "rev-parse", "stash@{0}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return a repository with one commit and one stash that existed before the run started."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "core.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _leftovers(repo: Path, impl_dir: Path, stash_before: Path | None) -> list[str]:
    """Run the helper and return its output lines."""
    args = [sys.executable, str(_SCRIPT), "--impl-dir", str(impl_dir)]
    if stash_before is not None:
        args += ["--stash-before", str(stash_before)]
    proc = subprocess.run(args, cwd=repo, env=_ENV, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def _branch_commit(repo: Path, branch: str, name: str, text: str) -> str:
    """Commit one file on a new branch off HEAD, return to the starting branch and return the commit id."""
    start = _git(repo, "branch", "--show-current")
    _git(repo, "switch", "-q", "-c", branch)
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"{branch} work")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", start)
    return sha


def _run_cleanup(repo: Path, lines: list[str]) -> None:
    """Run every printed cleanup command in order, as a user pasting the block would; failures do not stop the rest."""
    for line in lines:
        if line.startswith("CLEANUP="):
            subprocess.run(shlex.split(line.split("=", 1)[1]), cwd=repo, env=_ENV, capture_output=True, check=False)


def test_lists_run_leftovers_with_ordered_cleanup(repo: Path, tmp_path: Path) -> None:
    """Recorded branches and worktrees that still exist and stashes made during the run are listed, never removed."""
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    before = tmp_path / "stash-before"
    before.write_text(_stash(repo, "VALUE = 2\n") + "\n", encoding="utf-8")
    _branch_commit(repo, "worktree-agent-a1", "staged_only.py", "A = 1\n")  # stage mode: never committed on HEAD
    _git(repo, "cherry-pick", _branch_commit(repo, "worktree-agent-b2", "landed.py", "B = 1\n"))
    stray = tmp_path / "stray-worktree"
    _git(repo, "worktree", "add", "-q", "-b", "stray", str(stray))
    registered = _git(repo, "worktree", "list", "--porcelain").splitlines()[-3].removeprefix("worktree ")
    rows = "worktree-agent-a1\tuncommitted\nworktree-agent-b2\tunconfirmed\nworktree-agent-a1\tuncommitted\n"
    (impl_dir / "recovery-branches.txt").write_text(rows + "worktree-agent-gone\tuncommitted\n", encoding="utf-8")
    (impl_dir / "worktrees-unreported.txt").write_text(f"{registered}\n/nowhere/else\n", encoding="utf-8")
    older = _stash(repo, "VALUE = 3\n")
    newer = _stash(repo, "VALUE = 4\n")

    lines = _leftovers(repo, impl_dir, before)

    assert [line.split("=", 1)[0] for line in lines if line.startswith("LEFTOVER_")] == [
        "LEFTOVER_WORKTREE",
        "LEFTOVER_BRANCH",
        "LEFTOVER_BRANCH",
        "LEFTOVER_STASH",
        "LEFTOVER_STASH",
    ]
    assert any("worktree-agent-a1 at " in line and "not yet committed" in line for line in lines)
    assert any("worktree-agent-a1 at " in line and "may hold work found nowhere else" in line for line in lines)
    assert any("worktree-agent-b2 at " in line and "already in HEAD" in line for line in lines)
    stashes = [line for line in lines if line.startswith("LEFTOVER_STASH=")]
    assert [line.split("=", 1)[1].split()[0] for line in stashes] == [newer, older], "stashes are named by commit id"
    assert all("not proven to be its own" in line for line in stashes)
    assert [line for line in lines if line.startswith("CLEANUP=")] == [
        f"CLEANUP=git worktree remove {registered}",
        "CLEANUP=git branch -D worktree-agent-b2",
        "CLEANUP=git cherry -v HEAD worktree-agent-a1",
        "CLEANUP=git stash list --format='%gd %H %s'",
    ]
    assert any(line.startswith("STASH_DROP=") and "current N" in line for line in lines)
    assert _git(repo, "branch", "--list", "worktree-agent-*").split() == ["worktree-agent-a1", "worktree-agent-b2"]
    assert len(_git(repo, "stash", "list").splitlines()) == 3, "nothing is deleted for the user"


@pytest.mark.parametrize(
    "reason", [pytest.param("uncommitted", id="kept-uncommitted"), pytest.param("unconfirmed", id="kept-unconfirmed")]
)
def test_pasted_cleanup_never_deletes_work_head_lacks(repo: Path, tmp_path: Path, reason: str) -> None:
    """Running the whole printed cleanup removes landed branches and keeps one whose commit HEAD does not hold.

    The recorded reason is not trusted: stage mode and a failed group commit leave a branch kept as "uncommitted" whose
    commit is the only committed copy of its work.
    """
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    _branch_commit(repo, "worktree-agent-a1", "only_here.py", "A = 1\n")
    _git(repo, "branch", "kept-upstream", "worktree-agent-a1")
    _git(repo, "branch", "--set-upstream-to=kept-upstream", "worktree-agent-a1")
    _git(repo, "cherry-pick", _branch_commit(repo, "worktree-agent-b2", "picked.py", "B = 1\n"))
    rows = f"worktree-agent-a1\t{reason}\nworktree-agent-b2\t{reason}\n"
    (impl_dir / "recovery-branches.txt").write_text(rows, encoding="utf-8", newline="\n")
    lines = _leftovers(repo, impl_dir, None)
    _run_cleanup(repo, lines)
    assert _git(repo, "branch", "--list", "worktree-agent-*").split() == ["worktree-agent-a1"]


@pytest.mark.parametrize("backed", [False, True])
def test_detached_worktree_cleanup_requires_a_surviving_ref(repo: Path, tmp_path: Path, backed: bool) -> None:
    """A clean detached worktree keeps its unique commit until a durable ref contains it."""
    worktree = tmp_path / "detached tree"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "unique detached history")
    if backed:
        _git(worktree, "tag", "keep-detached-tip")
    (tmp_path / "worktrees-unreported.txt").write_text(worktree.as_posix() + "\n", encoding="utf-8")

    lines = _leftovers(repo, tmp_path, None)
    _run_cleanup(repo, lines)

    assert any(line.startswith("LEFTOVER_WORKTREE=") for line in lines)
    assert any(line.startswith("CLEANUP=git worktree remove") for line in lines) is backed
    assert worktree.exists() is not backed


def test_worktree_cannot_rely_on_a_branch_listed_for_removal(repo: Path, tmp_path: Path) -> None:
    """The combined cleanup preserves a detached commit whose only branch is scheduled for deletion."""
    worktree = tmp_path / "detached tree"
    _git(repo, "worktree", "add", "-q", "--detach", str(worktree))
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "history unique to the detached worktree")
    tip = _git(worktree, "rev-parse", "HEAD")
    _git(repo, "branch", "recovery", tip)
    (tmp_path / "worktrees-unreported.txt").write_text(worktree.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "recovery-branches.txt").write_text("recovery\tuncommitted\n", encoding="utf-8")

    lines = _leftovers(repo, tmp_path, None)
    _run_cleanup(repo, lines)

    assert "CLEANUP=git branch -D recovery" in lines
    assert not any(line.startswith("CLEANUP=git worktree remove") for line in lines)
    assert _git(repo, "branch", "--list", "recovery") == ""
    assert _git(worktree, "rev-parse", "HEAD") == tip


def test_branch_whose_commits_were_collapsed_counts_as_landed(repo: Path, tmp_path: Path) -> None:
    """A branch whose two commits landed as one combined commit is proven landed by content and force-deleted."""
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    _git(repo, "switch", "-q", "-c", "worktree-agent-c3")
    for value in ("1", "2"):
        (repo / "collapsed.py").write_text(f"C = {value}\n", encoding="utf-8")
        _git(repo, "add", "collapsed.py")
        _git(repo, "commit", "-q", "-m", f"step {value}")
    _git(repo, "switch", "-q", "main")
    (repo / "collapsed.py").write_text("C = 2\n", encoding="utf-8")
    _git(repo, "add", "collapsed.py")
    _git(repo, "commit", "-q", "-m", "both steps as one commit")
    (impl_dir / "recovery-branches.txt").write_text("worktree-agent-c3\tuncommitted\n", encoding="utf-8")
    lines = _leftovers(repo, impl_dir, None)
    assert "CLEANUP=git branch -D worktree-agent-c3" in lines
    assert not any(line.startswith("CLEANUP=git cherry") for line in lines)


@pytest.fixture(params=["merge-resolution", "whitespace-only"])
def unlanded_equivalent_branch(repo: Path, request: pytest.FixtureRequest) -> str:
    """Build a divergent tip that patch equivalence overlooks, with different exact content from HEAD."""
    branch = "worktree-agent-hidden"
    if request.param == "merge-resolution":
        side = _branch_commit(repo, "side", "core.py", "VALUE = 2\n")
        own = _branch_commit(repo, branch, "core.py", "VALUE = 3\n")
        _git(repo, "merge", "--ff-only", own)
        for target, value in (("main", 3), (branch, 4)):
            _git(repo, "switch", "-q", target)
            conflict = subprocess.run(["git", "merge", "--no-edit", side], cwd=repo, env=_ENV, capture_output=True)
            assert conflict.returncode == 1
            (repo / "core.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
            _git(repo, "commit", "-qam", f"Resolve to {value}")
        _git(repo, "switch", "-q", "main")
    else:
        _branch_commit(repo, branch, "core.py", "VALUE  = 2\n")
        (repo / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "Equivalent patch id, different content")
    assert not any(line.startswith("+") for line in _git(repo, "cherry", "HEAD", branch).splitlines())
    return branch


def test_patch_equivalence_cannot_force_delete_unlanded_content(
    repo: Path, tmp_path: Path, unlanded_equivalent_branch: str
) -> None:
    """Cleanup keeps content omitted by patch equivalence, including merge-only and whitespace-only edits."""
    branch = unlanded_equivalent_branch
    (tmp_path / "recovery-branches.txt").write_text(f"{branch}\tunconfirmed\n", encoding="utf-8")

    lines = _leftovers(repo, tmp_path, None)

    assert _git(repo, "show", f"{branch}:core.py") != _git(repo, "show", "HEAD:core.py")
    assert any(branch in line and "not proven to be in HEAD" in line for line in lines)
    assert not any(line.startswith("CLEANUP=git branch -D") for line in lines)
    _run_cleanup(repo, lines)
    assert _git(repo, "branch", "--list", branch) == branch


@pytest.mark.parametrize("step", ["check-ref-format", "merge-base", "worktree", "stash"])
def test_timed_out_leftover_check_prints_an_inconclusive_report(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    """A timed-out lookup, content proof, worktree listing or stash listing is surfaced in the report."""
    branch = "worktree-agent-pending"
    _branch_commit(repo, branch, "pending.py", "PENDING = True\n")
    (tmp_path / "recovery-branches.txt").write_text(f"{branch}\tunconfirmed\n", encoding="utf-8")
    before = tmp_path / "stash-before"
    before.touch()
    monkeypatch.chdir(repo)
    run = subprocess.run

    def timed_call(argv: list[str], **options: object) -> subprocess.CompletedProcess:
        """Use real Git except for the process that exceeds its timeout."""
        if argv[1] == step:
            raise subprocess.TimeoutExpired(argv, options["timeout"])
        return run(argv, env=_ENV, **options)

    monkeypatch.setattr(leftovers.subprocess, "run", timed_call)
    assert leftovers.main(["--impl-dir", str(tmp_path), "--stash-before", str(before)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("LEFTOVER_CHECK=incomplete") and "timed out" in line for line in lines)
    assert any(branch in line and "not proven to be in HEAD" in line for line in lines)
    assert not any(line.startswith("CLEANUP=git branch -D") for line in lines)


def test_all_leftover_calls_share_a_budget_below_the_fence(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Individually fast calls can exhaust the shared deadline, and a later call gets only the time left."""
    [call] = [line for line in _REPORT.read_text(encoding="utf-8").splitlines() if "bin/resolve_leftovers.py" in line]
    hint = re.search(r"# timeout: (\d+)", call)
    assert hint
    assert leftovers.BUDGET_SECONDS * 1000 < int(hint.group(1))
    branch = "worktree-agent-time"
    _branch_commit(repo, branch, "pending.py", "PENDING = True\n")
    (tmp_path / "recovery-branches.txt").write_text(f"{branch}\tunconfirmed\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    run = subprocess.run
    elapsed = 0.0
    timeouts = []

    def slow_call(argv: list[str], *, timeout: float, **options: object) -> subprocess.CompletedProcess:
        """Advance a simulated wall clock by the time each real Git call would take."""
        nonlocal elapsed
        timeouts.append(timeout)
        elapsed += min(4, timeout)
        if timeout < 4:
            raise subprocess.TimeoutExpired(argv, timeout)
        return run(argv, env=_ENV, **options)

    monkeypatch.setattr(time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(leftovers.subprocess, "run", slow_call)
    assert leftovers.main(["--impl-dir", str(tmp_path)]) == 0
    assert timeouts[-1] < timeouts[0]
    assert elapsed == leftovers.BUDGET_SECONDS
    assert f"branch {branch} not proven to be in HEAD: git timed out" in capsys.readouterr().out


def test_clean_run_reports_no_leftovers(repo: Path, tmp_path: Path) -> None:
    """A run that kept nothing and made no stash prints one ``LEFTOVERS=none`` line."""
    before = tmp_path / "stash-before"
    before.write_text("", encoding="utf-8")
    assert _leftovers(repo, tmp_path, before) == ["LEFTOVERS=none"]


@pytest.mark.parametrize(
    "record", [pytest.param("unavailable\n", id="unreadable-at-start"), pytest.param(None, id="no-record")]
)
def test_stashes_are_not_compared_without_a_start_record(repo: Path, tmp_path: Path, record: str | None) -> None:
    """Without a run-start stash list every stash would look new, so the helper says it did not compare them."""
    _stash(repo, "VALUE = 2\n")
    before = None
    if record is not None:
        before = tmp_path / "stash-before"
        before.write_text(record, encoding="utf-8")
    lines = _leftovers(repo, tmp_path, before)
    assert lines[0].startswith("STASH_CHECK=unavailable")
    assert not any(line.startswith("LEFTOVER_STASH") for line in lines)
