"""Verify conditional post-publication removal against disposable linked Git worktrees."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _remediation_cleanup import SHARED, command, published_worktree, write_json


def cleanup(caller: Path, checkpoint: Path, slot: Path, owner: str) -> subprocess.CompletedProcess:
    """Invoke the separate local CLI from the invoking checkout."""
    return subprocess.run(
        [
            sys.executable,
            str(SHARED / "remediation_worktree_cleanup.py"),
            "--run",
            str(checkpoint),
            "--slot",
            str(slot),
            "--owner",
            owner,
        ],
        cwd=caller,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        text=True,
        capture_output=True,
    )


@pytest.mark.integration
@pytest.mark.parametrize("followup", [False, True])
def test_verified_push_removes_worktree_and_preserves_branch_and_caller(tmp_path: Path, followup: bool) -> None:
    """A verified combined decision removes just its original allocation and supports a later replay."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path, followup=followup)
    checkpoint = run / "publication-request" if followup else run
    head = command(checkout, "git", "rev-parse", "HEAD")
    (caller / "local").write_bytes(b"caller notes")
    result = cleanup(caller, checkpoint, slot, owner)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "removed"
    assert not checkout.exists()
    assert command(caller, "git", "rev-parse", "refs/heads/fix") == head
    assert command(caller, "git", "branch", "--show-current") == "main"
    assert (caller / "local").read_bytes() == b"caller notes"
    assert cleanup(caller, checkpoint, slot, owner).returncode == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "defect",
    [
        "ignored",
        "untracked",
        "tracked",
        "staged",
        "empty-directory",
        "hidden-index",
        "merge",
        "lock",
        "wrong-head",
        "lost-owner",
        "old-run",
        "missing-readback",
        "push-failed",
        "answer-denied",
        "different-slot-owner",
        "in-delivery-checkout",
    ],
)
def test_cleanup_preserves_changed_or_unproven_delivery(tmp_path: Path, defect: str) -> None:
    """Refused cleanup retains the checkout, live Git registration and branch for inspection."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path)
    admin = Path(command(checkout, "git", "rev-parse", "--absolute-git-dir"))
    scenarios = {
        "ignored": lambda: (checkout / "ignored").write_bytes(b"user"),
        "untracked": lambda: (checkout / "user").write_bytes(b"user"),
        "tracked": lambda: (checkout / "tracked").write_bytes(b"user"),
        "staged": lambda: command(checkout, "git", "rm", "tracked"),
        "empty-directory": lambda: (checkout / "empty").mkdir(),
        "hidden-index": lambda: command(checkout, "git", "update-index", "--assume-unchanged", "tracked"),
        "merge": lambda: (admin / "MERGE_HEAD").write_bytes(b"a" * 40 + b"\n"),
        "lock": lambda: command(caller, "git", "worktree", "lock", str(checkout)),
        "wrong-head": lambda: command(checkout, "git", "commit", "--allow-empty", "-m", "Later"),
        "lost-owner": lambda: (admin / "codex-remediation-owner").unlink(),
        "old-run": lambda: (run / "remediation-worktree-owner.json").unlink(),
        "missing-readback": lambda: (run / "publication-readback.json").unlink(),
        "push-failed": lambda: write_json(run / "publication-execution-result.json", {"status": "failed"}),
        "answer-denied": lambda: write_json(run / "publication-answer.json", {"answer": "Deny"}),
        "different-slot-owner": lambda: None,
        "in-delivery-checkout": lambda: None,
    }
    scenarios[defect]()
    result = cleanup(
        checkout if defect == "in-delivery-checkout" else caller,
        run,
        slot,
        "peer" if defect == "different-slot-owner" else owner,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "retained"
    assert checkout.is_dir()
    assert admin.is_dir()
    assert command(checkout, "git", "branch", "--show-current") == "fix"


@pytest.mark.integration
def test_git_cached_stat_cannot_hide_local_bytes_from_cleanup(tmp_path: Path) -> None:
    """Preserve a same-size local edit that Git's configured stat cache reports clean."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path)
    command(checkout, "git", "config", "core.trustctime", "false")
    command(checkout, "git", "config", "core.checkStat", "minimal")
    tracked = checkout / "tracked"
    os.utime(tracked, (946684800, 946684800))
    command(checkout, "git", "update-index", "--refresh")
    tracked.write_bytes(b"user data\n")
    os.utime(tracked, (946684800, 946684800))
    assert command(checkout, "git", "status", "--porcelain") == ""
    result = cleanup(caller, run, slot, owner)
    assert result.returncode == 1, result.stdout
    assert tracked.read_bytes() == b"user data\n"


@pytest.mark.integration
def test_verified_legacy_push_only_consent_retains_allocated_checkout(tmp_path: Path) -> None:
    """Even a verified push and authentic fresh worktree cannot upgrade a push-only approval."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path, combined=False)
    result = cleanup(caller, run, slot, owner)
    assert result.returncode == 1
    assert "combined publication and removal approval missing" in result.stdout
    assert checkout.is_dir()
    assert not (run / "remediation-worktree-cleanup.json").exists()


@pytest.mark.integration
def test_subsequent_worktree_on_same_branch_and_path_is_retained(tmp_path: Path) -> None:
    """Matching source, HEAD, location and branch cannot replace the original Git creation marker."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path)
    command(caller, "git", "worktree", "remove", str(checkout))
    command(caller, "git", "worktree", "add", str(checkout), "fix")
    result = cleanup(caller, run, slot, owner)
    assert result.returncode == 1
    assert checkout.is_dir()
    assert not (run / "remediation-worktree-cleanup.json").exists()


@pytest.mark.integration
def test_checkout_writer_blocks_cleanup_under_matching_pr_claim(tmp_path: Path) -> None:
    """A peer holding the local checkout cannot lose its worktree while this run holds the target scope."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path)
    command(
        caller,
        sys.executable,
        str(SHARED / "review_checkout_slot.py"),
        "acquire",
        "--checkout",
        str(checkout),
        "--writer",
        "--owner",
        "peer",
    )
    result = cleanup(caller, run, slot, owner)
    assert result.returncode == 1, result.stdout
    assert "concurrent checkout claim" in result.stdout
    assert checkout.is_dir()


@pytest.mark.integration
def test_replay_after_git_reuses_administration_directory_preserves_new_registration(tmp_path: Path) -> None:
    """The next same-named worktree retains its Git registration when the prior removal is replayed."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path)
    original_admin = command(checkout, "git", "rev-parse", "--absolute-git-dir")
    assert cleanup(caller, run, slot, owner).returncode == 0
    later = tmp_path / "later" / checkout.name
    command(caller, "git", "worktree", "add", "--detach", str(later), "main")
    assert command(later, "git", "rev-parse", "--absolute-git-dir") == original_admin
    result = cleanup(caller, run, slot, owner)
    assert result.returncode == 0, result.stdout
    assert later.is_dir()
    assert (later / "tracked").read_bytes() == b"initial\n"


@pytest.mark.integration
def test_private_reflog_with_unpublished_commit_retains_checkout(tmp_path: Path) -> None:
    """An unpublished detached commit reachable only from the worktree reflog survives a cleanup attempt."""
    caller, checkout, run, slot, owner = published_worktree(tmp_path)
    command(checkout, "git", "switch", "--detach")
    command(checkout, "git", "commit", "--allow-empty", "-m", "Local experiment")
    unpublished = command(checkout, "git", "rev-parse", "HEAD")
    command(checkout, "git", "switch", "fix")
    assert command(checkout, "git", "status", "--porcelain") == ""
    result = cleanup(caller, run, slot, owner)
    assert result.returncode == 1, result.stdout
    assert unpublished in command(checkout, "git", "reflog", "show", "--format=%H", "HEAD")
