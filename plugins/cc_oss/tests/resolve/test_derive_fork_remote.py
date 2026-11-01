"""Tests for ``bin/derive_fork_remote.py``.

URL derivation is pure; every git interaction mocks ``subprocess.run`` and inspects the commands issued, so the remote-
add and upstream-tracking behaviour is asserted without touching a real repository.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from unittest import mock

import derive_fork_remote as dfr
import pytest
from _process import completed_git

_FORK_ARGS = ["--fork-remote", "contrib", "--head-ref", "feature", "--base-ref", "main"]


def _commands(git: mock.Mock) -> list[list[str]]:
    """Return the argument vector of every git call made so far."""
    return [call.args[0] for call in git.call_args_list]


@pytest.fixture
def git() -> Iterator[mock.Mock]:
    """Mock ``subprocess.run`` so every git command fails until a test sets ``side_effect``."""
    with mock.patch(
        "derive_fork_remote.subprocess.run", autospec=True, return_value=completed_git(returncode=1)
    ) as run:
        yield run


class TestRepoNameFromOrigin:
    """Extracting the bare repository name from an origin URL."""

    @pytest.mark.parametrize(
        ("origin", "expected"),
        [
            pytest.param("git@github.com:upstream/repo.git", "repo", id="ssh"),
            pytest.param("https://github.com/upstream/repo.git", "repo", id="https"),
            pytest.param("https://github.com/upstream/repo", "repo", id="https-no-suffix"),
        ],
    )
    def test_returns_last_segment_without_git_suffix(self, origin: str, expected: str) -> None:
        """The bare repository name is the last path segment without ``.git``.

        SSH and HTTPS origins, with or without the suffix, all name the same repository.
        """
        assert dfr.repo_name_from_origin(origin) == expected


class TestForkUrl:
    """Deriving the contributor's fork URL from origin."""

    @pytest.mark.parametrize(
        ("origin", "expected"),
        [
            pytest.param("git@github.com:upstream/repo.git", "git@github.com:contrib/repo.git", id="ssh-mirrors-ssh"),
            pytest.param("https://github.com/upstream/repo.git", "https://github.com/contrib/repo.git", id="https"),
            pytest.param("", "https://github.com/contrib/repo.git", id="unknown-origin-defaults-to-https"),
        ],
    )
    def test_mirrors_origin_transport(self, origin: str, expected: str) -> None:
        """The fork URL uses the same transport as origin — an SSH-only checkout has no HTTPS credentials.

        An unknown origin has no transport to mirror, so HTTPS is the default.
        """
        assert dfr.fork_url(origin, "contrib", "repo") == expected


class TestEnsureForkRemote:
    """Adding the fork remote only when it is missing."""

    def test_skips_when_present(self, git: mock.Mock) -> None:
        """An already-known remote is left alone.

        ``git remote get-url`` succeeds for the fork remote, so no ``add`` command may be issued.
        """
        git.side_effect = lambda *_a, **_k: completed_git(stdout="git@github.com:contrib/repo.git")
        dfr.ensure_fork_remote("contrib", 5)
        assert not any("add" in cmd for cmd in _commands(git))

    def test_adds_when_missing(self, git: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing remote is added with a transport-matched URL and announced.

        The fork remote lookup fails while origin resolves to an SSH URL, so the new remote must be SSH as well.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Fail the fork remote lookup, answer origin's, and accept everything else."""
            if cmd[:3] == ["git", "remote", "get-url"] and cmd[3] == "contrib":
                return completed_git(returncode=1)
            if cmd[:3] == ["git", "remote", "get-url"]:
                return completed_git(stdout="git@github.com:upstream/repo.git")
            return completed_git()

        git.side_effect = fake_run
        dfr.ensure_fork_remote("contrib", 5)
        assert "→ Added remote contrib → git@github.com:contrib/repo.git" in capsys.readouterr().out


class TestPushScope:
    """Measuring how much a push would send."""

    def test_uses_fork_range(self, git: mock.Mock) -> None:
        """When the fork branch exists, the scope is measured against it.

        The fork branch resolves, so the commit count and diffstat come from that range.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Report 3 commits for the count and a one-file diffstat for everything else."""
            if "rev-list" in cmd:
                return completed_git(stdout="3")
            return completed_git(stdout=" file.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)")

        git.side_effect = fake_run
        count, stat = dfr.push_scope("contrib", "feature", "main", 5)
        assert count == "3"
        assert stat == "1 file changed, 1 insertion(+), 1 deletion(-)"

    def test_falls_back_to_base_range(self, git: mock.Mock) -> None:
        """A branch not yet on the fork is measured against the base branch instead.

        The ``contrib/feature..HEAD`` range fails, so the scope must be computed from ``origin/main..HEAD``.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Fail the fork range, count 5 commits, and report a two-file diffstat."""
            if "contrib/feature..HEAD" in cmd:
                return completed_git(returncode=1)
            if "rev-list" in cmd:
                return completed_git(stdout="5")
            return completed_git(stdout=" 2 files changed, 9 insertions(+)")

        git.side_effect = fake_run
        count, stat = dfr.push_scope("contrib", "feature", "main", 5)
        assert count == "5"
        assert stat == "2 files changed, 9 insertions(+)"
        assert any("origin/main..HEAD" in cmd for cmd in _commands(git))


class TestMain:
    """The CLI: refuse unsafe input before git, and print the scope line Step 10 shows."""

    @pytest.mark.parametrize(
        ("fork_remote", "head_ref"),
        [pytest.param("", "feature", id="no-remote"), pytest.param("contrib", "", id="no-head-ref")],
    )
    def test_blocks_on_unresolved_refs(
        self, git: mock.Mock, fork_remote: str, head_ref: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unresolved remote or head ref refuses to push to an unresolved target.

        Exit code 1 with the "unresolved" notice, and git is never consulted.
        """
        assert dfr.main(["--fork-remote", fork_remote, "--head-ref", head_ref, "--base-ref", "main"]) == 1
        assert "FORK_REMOTE/HEAD_REF unresolved" in capsys.readouterr().out
        git.assert_not_called()

    @pytest.mark.parametrize(
        "flag",
        [
            pytest.param("--fork-remote", id="fork-remote"),
            pytest.param("--head-ref", id="head-ref"),
            pytest.param("--base-ref", id="base-ref"),
        ],
    )
    def test_rejects_leading_dash_argv(self, git: mock.Mock, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        """A leading-dash value on any of the three git-argv fields is refused before any git call.

        Each field reaches git argv unquoted (remote get-url/add, branch --set-upstream-to); a value starting with '-'
        would be parsed as an option instead of an operand. The targeted flag uses the ``--flag=<value>`` single-token
        form: argparse's own tokenizer treats a bare two-token ``--flag -evil`` pair as a *missing* value
        (unknown-option lookahead) and exits 2 through argparse itself, never reaching this guard.
        """
        defaults = {"--fork-remote": "contrib", "--head-ref": "feature", "--base-ref": "main"}
        args = [f"{name}=-evil" if name == flag else f"{name}={value}" for name, value in defaults.items()]
        assert dfr.main(args) == 1
        assert "must not start with" in capsys.readouterr().err
        git.assert_not_called()

    def test_blocks_when_scope_uncomputable(self, git: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """With no commit count, Step 10 never pushes a scope it cannot describe.

        Every git command fails, so the commit count is unavailable and the run stops with the "could not be computed"
        notice.
        """
        assert dfr.main(_FORK_ARGS) == 1
        assert "push scope could not be computed" in capsys.readouterr().out

    def test_prints_push_summary(self, git: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """The success path prints the exact scope line Step 10 shows before an authorized push.

        Git reports 2 commits, a diffstat and a last subject; the line combines them with the remote and branch.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Answer the commit count, diffstat and last-commit lookups, and accept everything else."""
            if "rev-list" in cmd:
                return completed_git(stdout="2")
            if "diff" in cmd:
                return completed_git(stdout=" 3 files changed, 47 insertions(+), 12 deletions(-)")
            if "log" in cmd:
                return completed_git(stdout="fix(core): guard empty input")
            return completed_git()

        git.side_effect = fake_run
        assert dfr.main(_FORK_ARGS) == 0
        expected = (
            "→ 2 commits ready to push to contrib/feature (3 files changed, 47 insertions(+), 12 deletions(-)); "
            'last commit: "fix(core): guard empty input"'
        )
        assert expected in capsys.readouterr().out
