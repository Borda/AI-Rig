"""Regression tests for benchmark-launcher capability admission."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
from _launcher_capability import _pinned_frozen_checkout_is_available

_UNUSED_COMMIT = "a" * 40


def _git(repo_path: Path, *arguments: str) -> str:
    """Run one local Git command for an isolated fixture checkout."""
    return subprocess.run(
        ["git", "-C", str(repo_path), *arguments], check=True, capture_output=True, text=True, timeout=10
    ).stdout.strip()


@pytest.fixture(scope="module")
def baseline_checkout(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """Build one clean local checkout with a committed baseline, shared read-only as a copy source."""
    checkout = tmp_path_factory.mktemp("baseline") / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.email", "benchmark@example.invalid")
    _git(checkout, "config", "user.name", "Benchmark Fixture")
    (checkout / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "--quiet", "-m", "baseline")
    return checkout, _git(checkout, "rev-parse", "HEAD")


@pytest.fixture
def pinned_checkout(tmp_path: Path, baseline_checkout: tuple[Path, str]) -> tuple[Path, str]:
    """Copy the clean baseline checkout into a private directory; return it with its HEAD commit."""
    template, commit = baseline_checkout
    checkout = tmp_path / "checkout"
    shutil.copytree(template, checkout)
    return checkout, commit


def _checkout_root(checkout: Path, _scratch: Path, _commit: str) -> Path:
    """Return the checkout root itself."""
    return checkout


def _linked_worktree(checkout: Path, scratch: Path, commit: str) -> Path:
    """Return a detached linked worktree of the checkout at the baseline commit."""
    worktree = scratch / "linked-worktree"
    _git(checkout, "worktree", "add", "--detach", str(worktree), commit)
    return worktree


def _subdirectory(checkout: Path, _scratch: Path, _commit: str) -> Path:
    """Return a plain child directory of the checkout."""
    child = checkout / "nested"
    child.mkdir()
    return child


def _modify_tracked(checkout: Path) -> None:
    """Change a tracked file without staging it."""
    (checkout / "tracked.txt").write_text("changed\n", encoding="utf-8")


def _stage_modification(checkout: Path) -> None:
    """Change a tracked file and stage the change."""
    _modify_tracked(checkout)
    _git(checkout, "add", "tracked.txt")


def _add_untracked(checkout: Path) -> None:
    """Add an untracked file."""
    (checkout / "untracked.txt").write_text("present\n", encoding="utf-8")


def _add_untracked_hidden_by_config(checkout: Path) -> None:
    """Add an untracked file while local Git config hides untracked files from status."""
    _git(checkout, "config", "status.showUntrackedFiles", "no")
    _add_untracked(checkout)


def _status_unavailable(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Return a matching head, then model an unavailable porcelain-status command."""
    if arguments[-2:] == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(arguments, 0, _UNUSED_COMMIT + "\n", "")
    return subprocess.CompletedProcess(arguments, 128, "", "not a Git repository")


class TestPinnedFrozenCheckoutIsAvailable:
    """Admission of a local checkout as a frozen fixture source: clean root at the exact pinned HEAD only."""

    @pytest.mark.parametrize(
        ("locate_source", "expected_available"),
        [
            pytest.param(_checkout_root, True, id="checkout-root"),
            pytest.param(_linked_worktree, True, id="linked-worktree-root"),
            pytest.param(_subdirectory, False, id="non-checkout-subdirectory"),
        ],
    )
    def test_requires_a_clean_root_at_the_exact_head(
        self,
        tmp_path: Path,
        pinned_checkout: tuple[Path, str],
        locate_source: Callable[[Path, Path, str], Path],
        expected_available: bool,
    ) -> None:
        """Ordinary and linked roots are admitted, but Git discovery cannot admit a child directory.

        A child directory of a clean checkout is inside a work tree for Git, yet it is not the checkout root the fixture
        contract pins, so it must be refused even though `git rev-parse` succeeds there.
        """
        checkout, expected_commit = pinned_checkout
        source = locate_source(checkout, tmp_path, expected_commit)

        assert _pinned_frozen_checkout_is_available(source, expected_commit) is expected_available

    @pytest.mark.parametrize(
        "dirty",
        [
            pytest.param(_modify_tracked, id="tracked"),
            pytest.param(_stage_modification, id="staged"),
            pytest.param(_add_untracked, id="untracked"),
            pytest.param(_add_untracked_hidden_by_config, id="configured-untracked"),
        ],
    )
    def test_rejects_a_dirty_tree(self, pinned_checkout: tuple[Path, str], dirty: Callable[[Path], None]) -> None:
        """Tracked, staged, and untracked bytes cannot enter a frozen fixture.

        Each kind of local change must be seen, including an untracked file that a local `status.showUntrackedFiles`
        setting would otherwise hide from a default status.
        """
        checkout, expected_commit = pinned_checkout
        dirty(checkout)

        assert _pinned_frozen_checkout_is_available(checkout, expected_commit) is False

    def test_rejects_the_wrong_head(self, pinned_checkout: tuple[Path, str]) -> None:
        """A clean checkout at another revision cannot substitute for the locked baseline.

        The checkout is pristine but its HEAD differs from the pinned commit, so only the revision comparison can refuse
        it.
        """
        checkout, _ = pinned_checkout

        assert _pinned_frozen_checkout_is_available(checkout, "0" * 40) is False

    def test_rejects_a_git_directory_without_head(self, tmp_path: Path) -> None:
        """A partial Git directory cannot admit source-dependent contract or lifecycle coverage.

        An empty `.git` directory looks like a checkout to a path check but holds no commit, so admission must fail
        instead of treating it as a usable baseline.
        """
        source = tmp_path / "source"
        (source / ".git").mkdir(parents=True)

        assert _pinned_frozen_checkout_is_available(source, _UNUSED_COMMIT) is False

    @mock.patch("_launcher_capability.subprocess.run", autospec=True, side_effect=_status_unavailable)
    def test_rejects_a_checkout_whose_status_is_unavailable(self, mock_run: mock.Mock, tmp_path: Path) -> None:
        """A checkout is unavailable when Git cannot report its working-tree status.

        The HEAD query succeeds with the pinned commit but the porcelain status command fails, so cleanliness is
        unprovable and admission must fail closed. The Git boundary is mocked; the real-Git cases above cover status.
        """
        checkout = tmp_path / "checkout"
        (checkout / ".git").mkdir(parents=True)

        assert _pinned_frozen_checkout_is_available(checkout, _UNUSED_COMMIT) is False
        assert [call.args[0][3:] for call in mock_run.call_args_list] == [
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
    @mock.patch("_launcher_capability.subprocess.run", autospec=True)
    def test_rejects_a_git_launch_failure_or_timeout(
        self, mock_run: mock.Mock, tmp_path: Path, failure: BaseException
    ) -> None:
        """Git launch and timeout failures close fixture admission rather than enabling it.

        The first Git call raises, so admission must give up after that single call and report the checkout unavailable
        instead of propagating the error or probing further.
        """
        checkout = tmp_path / "checkout"
        (checkout / ".git").mkdir(parents=True)
        mock_run.side_effect = failure

        assert _pinned_frozen_checkout_is_available(checkout, _UNUSED_COMMIT) is False
        assert [call.args for call in mock_run.call_args_list] == [(["git", "-C", str(checkout), "rev-parse", "HEAD"],)]
