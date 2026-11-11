"""Exercise resolve checkout, mutex, change-detection, and branch-recovery boundaries."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _text_io import bash_block_after, bash_blocks, bash_path, write_lf

_RESOLVE = Path(__file__).resolve().parents[3] / "skills/resolve"
_BASH = shutil.which("bash")
_GIT = shutil.which("git")
_SESSION = "integrity-contract"
_requires_shell = pytest.mark.skipif(_BASH is None or _GIT is None, reason="Resolve fences require Bash and Git")


def _doc(relative: str) -> str:
    """Read a shipped resolve instruction file without changing its line structure."""
    return (_RESOLVE / relative).read_text(encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    """Run Git only in a test-owned scratch repository with a deterministic identity."""
    result = subprocess.run(
        [_GIT, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def pr_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a stale feature branch and an alias at the newer PR head in a scratch repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feature")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "alias")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "PR head")
    return repo, old, _git(repo, "rev-parse", "HEAD")


def _run(block: str, repo: Path, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    """Run a shipped shell fragment with session state isolated to the test directory."""
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": _SESSION, "CSID": _SESSION, "TMPDIR": bash_path(tmp_path)} | extra
    return subprocess.run(
        [_BASH, "-c", "gh() { return 91; }\n" + block],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _sentinels(tmp_path: Path, values: dict[str, str | None]) -> None:
    """Persist each shell input as the same session-scoped sentinel the workflow reads."""
    for name, value in values.items():
        if value is not None:
            write_lf(tmp_path / f"resolve-{name}-{_SESSION}", value + "\n")


@_requires_shell
@pytest.mark.parametrize(
    ("target", "allowed", "expected_branch"),
    [
        pytest.param("feature", False, "alias", id="existing-stale-branch-blocked"),
        pytest.param("new-feature", True, "new-feature", id="missing-branch-created-at-pr-head"),
        pytest.param("aligned", True, "aligned", id="existing-matching-branch-retained"),
    ],
)
def test_sha_shortcut_preserves_pr_head(
    pr_repo: tuple[Path, str, str], tmp_path: Path, target: str, allowed: bool, expected_branch: str
) -> None:
    """Re-aligning an alias must never enter a stale existing branch or overwrite its local history."""
    repo, old, head = pr_repo
    _git(repo, "branch", "aligned", head)
    _sentinels(tmp_path, {"is-cross-repo": "false", "local-sha": head})
    block = bash_block_after(_doc("SKILL.md"), "**Branch-safety pre-check**")
    fragment = block[block.index("# SHA-first:") : block.index("# FORK_REMOTE")]
    result = _run(fragment, repo, tmp_path, PR_HEAD_REF=target, PR_HEAD_OID=head, LOCAL_SHA=head)
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "rev-parse", "feature") == old
    assert _git(repo, "branch", "--show-current") == expected_branch
    assert ("! BLOCKED" in result.stdout) is (not allowed)


@_requires_shell
@pytest.mark.parametrize(
    ("checkout_branch", "message"),
    [
        pytest.param("feature", "checkout HEAD", id="behind-pr-head"),
        pytest.param("alias", "unpushed local commits or a newer push", id="ahead-of-pr-head"),
    ],
)
def test_successful_gh_checkout_at_wrong_oid_is_blocked(
    pr_repo: tuple[Path, str, str], tmp_path: Path, checkout_branch: str, message: str
) -> None:
    """A successful checkout and correct branch name do not authorize edits on the wrong commit."""
    repo, old, head = pr_repo
    _git(repo, "commit", "-q", "--allow-empty", "-m", "local work or newer push")
    checkout_oid = _git(repo, "rev-parse", checkout_branch)
    _sentinels(tmp_path, {"is-cross-repo": "false", "local-sha": old})
    block = bash_block_after(_doc("SKILL.md"), "**Branch-safety pre-check**")
    fragment = block[block.index("# SHA-first:") : block.index("# FORK_REMOTE")]
    result = _run(
        'gh() { git switch "$PR_HEAD_REF"; }\n' + fragment,
        repo,
        tmp_path,
        PR_HEAD_REF=checkout_branch,
        PR_HEAD_OID=head,
        LOCAL_SHA=old,
        PR_NUMBER="12",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "! BLOCKED" in result.stdout
    assert message in result.stdout
    assert _git(repo, "rev-parse", "HEAD") == checkout_oid


@_requires_shell
@pytest.mark.parametrize(
    ("current", "head_args", "message"),
    [
        pytest.param("feature", ("--quiet",), "on default branch", id="default-branch-blocked"),
        pytest.param("alias", ("--quiet",), "", id="feature-branch-admitted"),
        pytest.param("alias", ("--delete",), "cannot determine default branch", id="unknown-default-blocked"),
    ],
)
def test_local_branch_safety_guard(
    pr_repo: tuple[Path, str, str], tmp_path: Path, current: str, head_args: tuple[str, ...], message: str
) -> None:
    """Local report and comment dispatch must reject the default branch and an unknown default."""
    repo, _, head = pr_repo
    _git(repo, "update-ref", "refs/remotes/origin/feature", head)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feature")
    _git(repo, "symbolic-ref", *head_args, "refs/remotes/origin/HEAD")
    _git(repo, "switch", "-q", current)
    block = bash_block_after(_doc("SKILL.md"), "### Local branch safety guard")
    result = _run(block, repo, tmp_path)
    assert (result.returncode != 0) is bool(message), result.stdout + result.stderr
    assert ("! BLOCKED" in result.stdout) is bool(message)
    assert message in result.stdout
    assert _git(repo, "branch", "--show-current") == current


@_requires_shell
@pytest.mark.parametrize("commit_mode", [None, "", "stage"])
def test_straggler_gate_requires_recorded_commit_mode(
    pr_repo: tuple[Path, str, str], tmp_path: Path, commit_mode: str | None
) -> None:
    """A missing or empty Step 3d answer must block confirmation instead of selecting per-item commits."""
    repo, _, _ = pr_repo
    impl = tmp_path / "impl"
    impl.mkdir()
    write_lf(impl / "item-tasks.tsv", "1\titem-task\n")
    write_lf(impl / "challenge-log.txt", "")
    write_lf(impl / "skipped-items.txt", "")
    _sentinels(tmp_path, {"impl-dir": bash_path(impl), "commit-mode": commit_mode})
    result = _run(bash_block_after(_doc("SKILL.md"), "**Straggler gate"), repo, tmp_path)
    assert (result.returncode == 0) is (commit_mode == "stage"), result.stdout + result.stderr
    assert ("! BLOCKED" in result.stdout) is (commit_mode != "stage")


def _mutex_prefixes() -> list[str]:
    """Extract lock identity computation from the acquisition and two release fences."""
    documents = ["modes/dispatch-prelude.md", "modes/dispatch-phase3.md"]
    prefixes = []
    for document in documents:
        for block in bash_blocks(_doc(document)):
            if "_GITDIR=" not in block or "oss-resolve-" not in block:
                continue
            lines = block[block.index("_GITDIR=") :].splitlines()
            stop = next(i for i, line in enumerate(lines) if ".lock" in line)
            prefixes.append("\n".join(lines[:stop]))
    return prefixes


@_requires_shell
@pytest.mark.parametrize(
    ("ref", "checkout_args"),
    [
        pytest.param("refs/heads/feat/x", ("switch", "-q", "-c", "feat/x"), id="slash-branch"),
        pytest.param("refs/heads/feat-x", ("switch", "-q", "-c", "feat-x"), id="hyphen-branch"),
        pytest.param("detached", ("switch", "-q", "--detach"), id="detached-head"),
    ],
)
def test_mutex_keys_agree_across_acquire_and_release(
    pr_repo: tuple[Path, str, str], tmp_path: Path, ref: str, checkout_args: tuple[str, ...]
) -> None:
    """Acquisition and both cleanup paths must compute one nonempty lock identity for each ref state."""
    repo, _, _ = pr_repo
    _git(repo, *checkout_args)
    results = [
        _run(prefix + '\nprintf "%s\\n" "$_GITDIR/oss-resolve-${_BRANCH_KEY:-$_BRANCH}.lock"', repo, tmp_path)
        for prefix in _mutex_prefixes()
    ]
    assert [result.returncode for result in results] == [0, 0, 0]
    keys = [result.stdout.strip() for result in results]
    digest = (
        subprocess.check_output([_GIT, "hash-object", "--stdin"], input=(ref + "\n").encode(), cwd=repo)
        .decode()
        .strip()
    )
    assert len(keys) == 3
    assert keys == [f".git/oss-resolve-{digest}.lock"] * 3


@_requires_shell
@pytest.mark.parametrize("untracked_setting", ["normal", "no"])
def test_comment_review_detects_untracked_only_changes(
    pr_repo: tuple[Path, str, str], tmp_path: Path, untracked_setting: str
) -> None:
    """Creating a new source file must enter review even when the tracked diff is empty."""
    repo, _, _ = pr_repo
    _git(repo, "config", "status.showUntrackedFiles", untracked_setting)
    write_lf(repo / "new_source.py", "value = 1\n")
    block = bash_block_after(_doc("modes/comment-dispatch.md"), "### 12b:")
    result = _run(block, repo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "?? new_source.py" in result.stdout


@_requires_shell
@pytest.mark.parametrize(
    ("saved_branch", "push_status"),
    [
        pytest.param("missing-caller-branch", "pushed", id="missing-caller-branch"),
        pytest.param("", "pushed", id="detached-caller-pushed"),
        pytest.param("HEAD", "pushed", id="abbrev-ref-detached"),
        pytest.param("", "none", id="detached-caller-unpushed"),
        pytest.param("HEAD", "none", id="abbrev-ref-detached-unpushed"),
    ],
)
def test_restore_failure_or_detached_caller_is_visible(
    pr_repo: tuple[Path, str, str], tmp_path: Path, saved_branch: str, push_status: str
) -> None:
    """Recovery must name the saved caller commit when a branch cannot be restored or the caller was detached."""
    repo, old, head = pr_repo
    _sentinels(
        tmp_path, {"saved-branch": saved_branch, "saved-sha": old, "commit-mode": "each", "push-status": push_status}
    )
    block = next(block for block in bash_blocks(_doc("modes/final-report.md")) if "SAVED_BRANCH" in block)
    result = _run(block, repo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "⚠" in result.stdout
    assert old in result.stdout
    assert "git switch HEAD" not in result.stdout
    assert "Could not restore HEAD" not in result.stdout
    assert _git(repo, "rev-parse", "HEAD") == head


@_requires_shell
def test_final_report_missing_commit_mode_stays_unset(pr_repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """Final reporting must preserve a missing commit choice instead of inventing a per-item choice."""
    repo, old, _ = pr_repo
    _sentinels(tmp_path, {"saved-branch": "feature", "saved-sha": old, "push-status": "none"})
    block = next(block for block in bash_blocks(_doc("modes/final-report.md")) if "SAVED_BRANCH" in block)
    result = _run(block + '\nprintf "COMMIT_MODE=%s\\n" "$COMMIT_MODE"', repo, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "COMMIT_MODE=unset\n" in result.stdout
    assert "commits not pushed" in result.stdout
    assert _git(repo, "branch", "--show-current") == "alias"


def test_stage_recovery_keeps_edits_on_pr_branch() -> None:
    """Stage-mode advice must require committing on the PR branch before restoring the caller."""
    line = next(line for line in _doc("modes/final-report.md").splitlines() if 'echo "⚠ COMMIT_MODE=stage:' in line)
    assert "Commit on this PR branch first" in line
    assert "git stash" not in line


def test_resume_keeps_scope_bearing_push_confirmation() -> None:
    """A boundary-2 resume must preserve Step 10's independent confirmation rather than spend push intent."""
    line = next(line for line in _doc("SKILL.md").splitlines() if '"resume: re-cat own agent-resolution.md' in line)
    assert "push intent recorded at Step 3d" in line
    assert "Step 10 still asks the git-push confirmation (modes/push.md)" in line
    assert "never re-ask" not in line


def test_isolation_spawns_are_exempt_from_work_displacement_threshold() -> None:
    """The inline cost threshold cannot replace challenge independence or specialist worktree isolation."""
    lines = _doc("modes/dispatch-prelude.md").splitlines()
    rule = next(line for line in lines if "work-displacement" in line)
    assert "Phase 1 challengers" in rule
    assert "Phase 2 worktree specialists" in rule
    assert "always spawn regardless of size" in rule
    dispatch_rule = next(line for line in lines if "`DISPATCH_MODE` (Step 3d)" in line)
    assert "never suppresses the required Phase 1/2 spawns" in dispatch_rule


@_requires_shell
def test_file_group_topics_use_portable_extended_regex(pr_repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """Filename topics must collapse consecutive separators using portable POSIX extended expressions."""
    repo, _, _ = pr_repo
    line = next(
        line for line in _doc("modes/dispatch-phase3.md").splitlines() if "topic = the item's `file` basename" in line
    )
    pipeline = line.split("derived with `", 1)[1].split("`", 1)[0]
    assert "sed -E 's/-+/-/g" in pipeline
    result = _run(pipeline, repo, tmp_path, _FILE="my___module.py")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "my-module"
