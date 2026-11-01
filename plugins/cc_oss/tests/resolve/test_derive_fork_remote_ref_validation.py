"""Name-validation tests for ``bin/derive_fork_remote.py``.

The remote name and both refs reach git argv and a ``<remote>/<ref>..HEAD`` range string, so a value git would reject or
read as a revision expression (``a..b``, ``@{-1}``) must stop the push scope before any git call.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from unittest import mock

import derive_fork_remote as dfr
import pytest
from _process import completed_git


@pytest.fixture
def git() -> Iterator[mock.Mock]:
    """Mock ``subprocess.run`` so every git command succeeds with no output until a test sets ``side_effect``."""
    with mock.patch("derive_fork_remote.subprocess.run", autospec=True, return_value=completed_git()) as run:
        yield run


class TestIsValidRefName:
    """is_valid_ref_name: the core ``git check-ref-format`` rules, without spawning git."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("main", id="plain"),
            pytest.param("feature/x-1", id="slash-and-dash"),
            pytest.param("release/1.2.x", id="dots-inside"),
        ],
    )
    def test_accepts_ordinary_branch_names(self, name: str) -> None:
        """Common branch shapes are accepted.

        A plain name, a namespaced one and one with dots inside are what real pull requests use.
        """
        assert dfr.is_valid_ref_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("", id="empty"),
            pytest.param("a..b", id="double-dot-range"),
            pytest.param("a b", id="space"),
            pytest.param("@{-1}", id="previous-branch-expression"),
            pytest.param("@", id="at-sign-alone"),
            pytest.param("x.lock", id="lock-suffix"),
            pytest.param("a/", id="trailing-slash"),
            pytest.param("a//b", id="double-slash"),
            pytest.param("-x", id="leading-dash"),
            pytest.param("a/.hidden", id="dot-component"),
            pytest.param("a~1", id="tilde"),
            pytest.param("a\nb", id="newline"),
        ],
    )
    def test_rejects_names_git_would_refuse(self, name: str) -> None:
        """A name git rejects, or reads as a revision expression, is refused.

        Each case is a distinct ``git check-ref-format`` rule or a revision syntax that would change the range meaning.
        """
        assert dfr.is_valid_ref_name(name) is False


class TestMainRefusesBadNames:
    """Main: a bad remote or ref returns 1 before any git interaction."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--fork-remote", "a..b", "--head-ref", "x"], id="remote-double-dot"),
            pytest.param(["--fork-remote", "has space", "--head-ref", "x"], id="remote-space"),
            pytest.param(["--fork-remote", "ok", "--head-ref", "@{-1}"], id="head-previous-branch"),
            pytest.param(["--fork-remote", "ok", "--head-ref", "x", "--base-ref", "a..b"], id="base-double-dot"),
        ],
    )
    def test_invalid_names_return_one(self, git: mock.Mock, argv: list[str]) -> None:
        """The run stops with exit code 1 and never reaches git.

        The remote name, head ref and base ref are each checked, so a bad value in any one of them stops the run.
        """
        assert dfr.main(argv) == 1
        git.assert_not_called()

    @pytest.mark.parametrize(
        "fork_repo",
        [
            pytest.param("a/b", id="slash"),
            pytest.param("-x", id="leading-dash"),
            pytest.param("..", id="parent-dir"),
            pytest.param(".", id="current-dir"),
            pytest.param("has space", id="space"),
        ],
    )
    def test_invalid_fork_repo_returns_one(self, git: mock.Mock, fork_repo: str) -> None:
        """A ``--fork-repo`` that would corrupt the derived URL is refused before any git call.

        The name is spliced into the fork URL, so path separators, dot segments and option-like values are rejected.
        """
        assert dfr.main(["--fork-remote", "ok", "--head-ref", "x", f"--fork-repo={fork_repo}"]) == 1
        git.assert_not_called()

    def test_leading_dot_fork_repo_reaches_push_scope(self, git: mock.Mock) -> None:
        """A dot-led repository name such as ``.github`` passes validation and the push scope is printed.

        ``oss:resolve`` Step 10 passes every PR's head repository name, so refusing a legal GitHub name would block the
        push for every pull request to an organisation's ``.github`` repository.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Answer the count, diffstat and last-commit lookups by git subcommand."""
            return completed_git(
                stdout={"rev-list": "1", "diff": " 1 file changed", "log": "fix: docs"}.get(cmd[1], "")
            )

        git.side_effect = fake_run
        assert dfr.main(["--fork-remote", "ok", "--head-ref", "x", "--fork-repo=.github"]) == 0


class TestForkRepoOverride:
    """ensure_fork_remote: a renamed fork gets its own repository name in the derived URL."""

    def test_fork_repo_replaces_upstream_name(self, git: mock.Mock) -> None:
        """With ``fork_repo`` set, the remote is added under that name, not under origin's.

        A contributor who renamed their fork would otherwise get a remote URL that points at a repository that does not
        exist, or at someone else's.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Fail the fork remote lookup, answer origin's HTTPS URL, and accept every other command."""
            if cmd[:4] == ["git", "remote", "get-url", "contrib"]:
                return completed_git(returncode=1)
            if cmd[:3] == ["git", "remote", "get-url"]:
                return completed_git(stdout="https://github.com/upstream/repo.git")
            return completed_git()

        git.side_effect = fake_run
        dfr.ensure_fork_remote("contrib", 5, "renamed-fork")
        added = [call.args[0] for call in git.call_args_list]
        assert ["git", "remote", "add", "contrib", "https://github.com/contrib/renamed-fork.git"] in added
