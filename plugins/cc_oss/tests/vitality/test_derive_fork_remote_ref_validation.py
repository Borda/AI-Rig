"""Name-validation tests for ``bin/derive_fork_remote.py``.

The remote name and both refs reach git argv and a ``<remote>/<ref>..HEAD`` range string, so a value git would reject or
read as a revision expression (``a..b``, ``@{-1}``) must stop the push scope before any git call.
"""

from __future__ import annotations

import derive_fork_remote as dfr
import pytest


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
        """Common branch shapes are accepted."""
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
        """A name git rejects, or reads as a revision expression, is refused."""
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
    def test_invalid_names_return_one(self, argv: list[str]) -> None:
        """The run stops with exit code 1 and never reaches git."""
        assert dfr.main(argv) == 1

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
    def test_invalid_fork_repo_returns_one(self, fork_repo: str) -> None:
        """A ``--fork-repo`` that would corrupt the derived URL is refused before any git call."""
        assert dfr.main(["--fork-remote", "ok", "--head-ref", "x", f"--fork-repo={fork_repo}"]) == 1

    def test_leading_dot_fork_repo_reaches_push_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dot-led repository name such as ``.github`` passes validation and the push scope is printed.

        ``oss:resolve`` Step 10 passes every PR's head repository name, so refusing a legal GitHub name would block the
        push for every pull request to an organisation's ``.github`` repository.
        """

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            stdout = {"rev-list": "1", "diff": " 1 file changed", "log": "fix: docs"}.get(cmd[1], "")
            return type("R", (), {"returncode": 0, "stdout": stdout})()

        monkeypatch.setattr(dfr.subprocess, "run", fake_run)
        assert dfr.main(["--fork-remote", "ok", "--head-ref", "x", "--fork-repo=.github"]) == 0


class TestForkRepoOverride:
    """ensure_fork_remote: a renamed fork gets its own repository name in the derived URL."""

    def test_fork_repo_replaces_upstream_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With ``fork_repo`` set, the remote is added under that name, not under origin's.

        A contributor who renamed their fork would otherwise get a remote URL that points at a repository that does not
        exist, or at someone else's.
        """
        added: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> object:
            if cmd[:4] == ["git", "remote", "get-url", "contrib"]:
                return type("R", (), {"returncode": 1, "stdout": ""})()
            if cmd[:3] == ["git", "remote", "get-url"]:
                return type("R", (), {"returncode": 0, "stdout": "https://github.com/upstream/repo.git"})()
            added.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": ""})()

        monkeypatch.setattr(dfr.subprocess, "run", fake_run)
        dfr.ensure_fork_remote("contrib", 5, "renamed-fork")
        assert ["git", "remote", "add", "contrib", "https://github.com/contrib/renamed-fork.git"] in added
