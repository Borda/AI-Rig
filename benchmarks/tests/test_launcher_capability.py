"""Regression tests for benchmark-launcher capability admission."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import _launcher_capability
from _launcher_capability import _pinned_frozen_checkout_is_available


def _git(repo_path: Path, *arguments: str) -> str:
    """Run one local Git command for an isolated fixture checkout."""
    return subprocess.run(
        ["git", "-C", str(repo_path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _pinned_checkout(tmp_path: Path) -> tuple[Path, str]:
    """Create one clean local checkout with a committed baseline."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.email", "benchmark@example.invalid")
    _git(checkout, "config", "user.name", "Benchmark Fixture")
    (checkout / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "--quiet", "-m", "baseline")
    return checkout, _git(checkout, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("source_kind", "expected_available"),
    [
        pytest.param("checkout", True, id="checkout-root"),
        pytest.param("linked-worktree", True, id="linked-worktree-root"),
        pytest.param("subdirectory", False, id="non-checkout-subdirectory"),
    ],
)
def test_pinned_frozen_checkout_requires_a_clean_root_at_the_exact_head(
    tmp_path: Path, source_kind: str, expected_available: bool
) -> None:
    """Ordinary and linked roots are admitted, but Git discovery cannot admit a child directory."""
    checkout, expected_commit = _pinned_checkout(tmp_path)
    source = checkout
    if source_kind == "linked-worktree":
        source = tmp_path / "linked-worktree"
        _git(checkout, "worktree", "add", "--detach", str(source), expected_commit)
    elif source_kind == "subdirectory":
        source = checkout / "nested"
        source.mkdir()

    assert _pinned_frozen_checkout_is_available(source, expected_commit) is expected_available


@pytest.mark.parametrize("change_kind", ["tracked", "staged", "untracked", "configured-untracked"])
def test_pinned_frozen_checkout_dirty_tree_is_rejected(tmp_path: Path, change_kind: str) -> None:
    """Tracked, staged, and untracked bytes cannot enter a frozen fixture."""
    checkout, expected_commit = _pinned_checkout(tmp_path)
    if change_kind == "tracked":
        (checkout / "tracked.txt").write_text("changed\n", encoding="utf-8")
    elif change_kind == "staged":
        (checkout / "tracked.txt").write_text("changed\n", encoding="utf-8")
        _git(checkout, "add", "tracked.txt")
    else:
        if change_kind == "configured-untracked":
            _git(checkout, "config", "status.showUntrackedFiles", "no")
        (checkout / "untracked.txt").write_text("present\n", encoding="utf-8")

    assert _pinned_frozen_checkout_is_available(checkout, expected_commit) is False


def test_pinned_frozen_checkout_wrong_head_is_rejected(tmp_path: Path) -> None:
    """A clean checkout at another revision cannot substitute for the locked baseline."""
    checkout, _ = _pinned_checkout(tmp_path)

    assert _pinned_frozen_checkout_is_available(checkout, "0" * 40) is False


def test_pinned_frozen_checkout_missing_status_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout is unavailable when Git cannot report its working-tree status."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    expected_commit = "a" * 40
    calls: list[list[str]] = []

    def _status_unavailable(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a matching head then model an unavailable porcelain-status command."""
        calls.append(arguments)
        if arguments[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(arguments, 0, expected_commit + "\n", "")
        return subprocess.CompletedProcess(arguments, 128, "", "not a Git repository")

    monkeypatch.setattr(_launcher_capability.subprocess, "run", _status_unavailable)

    assert _pinned_frozen_checkout_is_available(checkout, expected_commit) is False
    assert [arguments[3:] for arguments in calls] == [
        ["rev-parse", "HEAD"],
        ["status", "--porcelain", "--untracked-files=all"],
    ]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(OSError("Git is unavailable"), id="launch-error"),
        pytest.param(subprocess.TimeoutExpired(["git"], 10), id="timeout"),
    ],
)
def test_pinned_frozen_checkout_git_failure_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """Git launch and timeout failures close fixture admission rather than enabling it."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    def _raise_git_failure(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Model one unavailable external Git subprocess."""
        calls.append(arguments)
        raise failure

    monkeypatch.setattr(_launcher_capability.subprocess, "run", _raise_git_failure)

    assert _pinned_frozen_checkout_is_available(checkout, "a" * 40) is False
    assert calls == [["git", "-C", str(checkout), "rev-parse", "HEAD"]]
