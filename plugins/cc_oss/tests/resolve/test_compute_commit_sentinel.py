"""Tests for compute_commit_sentinel.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from compute_commit_sentinel import get_sentinel_path, main, to_slug


class TestToSlug:
    """to_slug: lowercase, collapse non-alphanumeric runs to one dash, strip edge dashes."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("MyRepo", "myrepo", id="lowercase"),
            pytest.param("MyRepo.local", "myrepo-local", id="dots-become-dash"),
            pytest.param("feature/my-branch", "feature-my-branch", id="slash-becomes-dash"),
            pytest.param("UPPER-CASE--extra-", "upper-case-extra", id="consecutive-non-alnum-collapsed"),
            pytest.param("foo-", "foo", id="trailing-dash-stripped"),
            pytest.param("", "", id="empty-string"),
            pytest.param("main", "main", id="plain-main"),
            pytest.param("repo123", "repo123", id="numeric-preserved"),
        ],
    )
    def test_slug(self, raw: str, expected: str) -> None:
        """Names are lowercased, each non-alphanumeric run becomes one dash, and edge dashes are stripped."""
        assert to_slug(raw) == expected


class TestGetSentinelPath:
    def _mock_git(self, repo_root: str, branch: str):
        """Patch Git lookups to return a deterministic repository and branch."""

        def _fake_check_output(cmd, **_kwargs):
            """Return the requested deterministic repository metadata."""
            if "--show-toplevel" in cmd:
                return repo_root + "\n"
            if "--show-current" in cmd:
                return branch + "\n"
            raise AssertionError(f"unexpected git command: {cmd}")

        return patch("compute_commit_sentinel.subprocess.check_output", side_effect=_fake_check_output)

    @pytest.mark.parametrize(
        ("repo_root", "branch", "expected_name"),
        [
            pytest.param("/home/user/MyRepo", "main", "claude-commit-auth-myrepo-main", id="plain-repo-and-branch"),
            pytest.param(
                "/projects/borda.local",
                "feature/add-tests",
                "claude-commit-auth-borda-local-feature-add-tests",
                id="dotted-repo-and-slashed-branch",
            ),
            pytest.param("/repo/proj", "HOTFIX/MY-FIX", "claude-commit-auth-proj-hotfix-my-fix", id="uppercase-branch"),
        ],
    )
    def test_slugs_repo_and_branch_into_the_sentinel_name(
        self, monkeypatch, tmp_path, repo_root: str, branch: str, expected_name: str
    ) -> None:
        """Repository name and branch are slugified into the sentinel filename.

        The base directory is the session ``TMPDIR`` here, so the assertion covers the filename the pre-commit hook
        matches on rather than the host's temp-dir layout.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        with self._mock_git(repo_root, branch):
            path = get_sentinel_path()
        assert path == str(tmp_path / expected_name)

    def test_tmpdir_preferred_over_platform_default(self, monkeypatch, tmp_path) -> None:
        """Regression: a per-user TMPDIR must take precedence over the platform temp dir.

        Both candidates are real directories here, so the assertion fails if the lookup order ever flips back to
        consulting ``tempfile.gettempdir()`` first.
        """
        preferred = tmp_path / "preferred"
        preferred.mkdir()
        monkeypatch.setenv("TMPDIR", str(preferred))
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr("compute_commit_sentinel.tempfile.gettempdir", lambda: str(tmp_path))
        with self._mock_git("/home/user/Project", "main"):
            path = get_sentinel_path()
        assert path == str(preferred / "claude-commit-auth-project-main")

    def test_windows_keeps_a_drive_absolute_tmpdir(self, monkeypatch) -> None:
        """A Windows TMPDIR naming a drive is per-user state and must beat the platform default.

        The literal path stands in for the value a Windows session exports; the host's own ``tmp_path`` cannot serve
        here, because a POSIX runner's temp dir is drive-less and would be rejected by the very check under test.
        """
        tmpdir = r"C:\Users\ci\AppData\Local\Temp"
        monkeypatch.setenv("TMPDIR", tmpdir)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr("compute_commit_sentinel.sys.platform", "win32")
        monkeypatch.setattr("compute_commit_sentinel.tempfile.gettempdir", lambda: r"C:\unused-platform-default")
        with self._mock_git("C:/repo/Project", "main"):
            path = get_sentinel_path()
        assert path == str(Path(tmpdir) / "claude-commit-auth-project-main")

    def test_windows_uses_native_tempdir_when_tmpdir_is_posix(self, monkeypatch, tmp_path) -> None:
        """A Windows sentinel ignores a POSIX TMPDIR that has no native directory."""
        monkeypatch.setenv("TMPDIR", "/tmp")
        monkeypatch.setattr("compute_commit_sentinel.sys.platform", "win32")
        monkeypatch.setattr("compute_commit_sentinel.tempfile.gettempdir", lambda: str(tmp_path))
        with self._mock_git("C:/repo/Project", "main"):
            path = get_sentinel_path()
        assert path == str(tmp_path / "claude-commit-auth-project-main")

    def test_git_failure_raises(self) -> None:
        with patch(
            "compute_commit_sentinel.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                get_sentinel_path()


class TestMain:
    def test_success_prints_path(self, capsys) -> None:
        with patch(
            "compute_commit_sentinel.get_sentinel_path",
            return_value="/tmp/claude-commit-auth-repo-main",
        ):
            rc = main([])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "/tmp/claude-commit-auth-repo-main"

    def test_git_error_returns_1(self, capsys) -> None:
        with patch(
            "compute_commit_sentinel.get_sentinel_path",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            rc = main([])
        assert rc == 1
