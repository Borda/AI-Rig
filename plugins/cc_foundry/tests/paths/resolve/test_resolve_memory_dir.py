"""Tests for ``bin/resolve_memory_dir.py``.

``slugify`` is checked against Claude Code's own directory naming (doctests in the module plus the cases below); the
rest exercises git-bound behaviour with the git subprocess boundary mocked and the CLI surface via ``capsys``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
import resolve_memory_dir

_RUN = "resolve_memory_dir.subprocess.run"


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build the ``git rev-parse`` result the resolver's subprocess call would return."""
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(name="set_home")
def _set_home(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Return a setter that points the home directory at a value (HOME on POSIX, USERPROFILE on Windows)."""

    def set_home(path: str) -> None:
        """Export ``path`` as the user's home for the rest of the test."""
        monkeypatch.setenv("HOME", path)
        monkeypatch.setenv("USERPROFILE", path)

    return set_home


class TestSlugify:
    """Slugify: Claude Code's project-directory naming (one hyphen per non-alphanumeric code unit, case kept)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("MyProject", "MyProject", id="case-kept"),
            pytest.param("foo_bar", "foo-bar", id="underscore"),
            pytest.param("foo/bar/baz", "foo-bar-baz", id="separators"),
            pytest.param("foo--bar", "foo--bar", id="runs-not-collapsed"),
            pytest.param("trailing-", "trailing-", id="single-trailing-hyphen-kept"),
            pytest.param("trailing---", "trailing---", id="multiple-trailing-hyphens-kept"),
            pytest.param("/abs/path/Borda.local", "-abs-path-Borda-local", id="abs-path-dot"),
            pytest.param("UPPER_CASE!@#mix", "UPPER-CASE---mix", id="punctuation-run"),
            pytest.param("/a b/c.d/e_f", "-a-b-c-d-e-f", id="space-dot-underscore"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_canonical_forms(self, raw: str, expected: str) -> None:
        """All edge cases map to the documented canonical slug.

        Scenario: case is kept, every non-alphanumeric character becomes one hyphen, runs are not collapsed and the
        empty string stays empty.
        """
        assert resolve_memory_dir.slugify(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("/Users/x/Workspace/AI-Rig", "-Users-x-Workspace-AI-Rig", id="posix-root"),
            pytest.param(
                "/Users/x/Workspace/repo/.claude/worktrees/oss-review-1",
                "-Users-x-Workspace-repo--claude-worktrees-oss-review-1",
                id="hidden-dir-gives-double-hyphen",
            ),
            pytest.param(
                "/private/var/folders/ab/c_defg/T", "-private-var-folders-ab-c-defg-T", id="underscore-in-macos-tmp"
            ),
            pytest.param(
                "/private/tmp/claude-501/-Users-x-Workspace-AI-Rig/scratchpad",
                "-private-tmp-claude-501--Users-x-Workspace-AI-Rig-scratchpad",
                id="leading-hyphen-segment",
            ),
            pytest.param("C:\\Users\\me\\proj name", "C--Users-me-proj-name", id="windows-backslash-space"),
            pytest.param("C:/Users/me/proj.v2_x", "C--Users-me-proj-v2-x", id="windows-forward-slash"),
            pytest.param("/tmp/\u00e9x", "-tmp--x", id="bmp-non-ascii-one-hyphen"),
            pytest.param("/tmp/\U0001f600x", "-tmp---x", id="astral-two-hyphens"),
        ],
    )
    def test_observed_claude_code_directory_names(self, raw: str, expected: str) -> None:
        """Real ``~/.claude/projects`` shaped paths and Windows/Unicode forms resolve as Claude Code names them.

        Scenario: hidden directories give a double hyphen, Windows separators and drive colons are replaced, and a
        non-BMP character counts as two UTF-16 code units and so yields two hyphens.
        """
        assert resolve_memory_dir.slugify(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("/" + "a" * 199, "-" + "a" * 199, id="200-characters-kept-verbatim"),
            pytest.param("/" + "a" * 200, "-" + "a" * 199 + "-b6ymvl", id="201-characters-cut-plus-hash"),
        ],
    )
    def test_length_boundary_at_200(self, raw: str, expected: str) -> None:
        """A 200-character slug is kept whole; 201 characters switch to cut-plus-hash.

        Scenario: the two inputs sit on either side of Claude Code's 200-character limit, so an off-by-one in the
        threshold would flip one of them.
        """
        assert resolve_memory_dir.slugify(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected_suffix"),
        [
            pytest.param("/Users/x/" + "segment-0123456789/" * 12 + "leaf", "egd9zf", id="ascii-path"),
            pytest.param("/" + "\u00e9\U0001f600x_" * 70, "40r4i7", id="non-ascii-path"),
        ],
    )
    def test_long_path_cut_to_200_with_node_reference_hash(self, raw: str, expected_suffix: str) -> None:
        """Over-long slugs are cut to 200 characters and suffixed with Claude Code's base-36 hash.

        Expected suffixes come from running the bundled JavaScript expression (``Math.abs`` of the 32-bit
        ``(e << 5) - e + charCodeAt | 0`` fold, ``toString(36)``) under node, not from this module.
        """
        slug = resolve_memory_dir.slugify(raw)
        assert len(slug) == 200 + 1 + len(expected_suffix)
        assert slug.endswith("-" + expected_suffix)
        assert set(slug[:200]) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


class TestResolveMemoryDir:
    """resolve_memory_dir: combines git fallback + slug + HOME."""

    @mock.patch(_RUN, autospec=True)
    def test_explicit_project_root_skips_git(self, mock_run: mock.Mock, set_home: Callable[[str], None]) -> None:
        """Explicit arg bypasses git lookup entirely.

        Scenario: the caller names the project root, so no subprocess may run — the answer must not depend on git being
        installed or on the working directory being a repository.
        """
        set_home("/home/test")

        result = resolve_memory_dir.resolve_memory_dir("/some/Project")

        mock_run.assert_not_called()
        assert Path(result).as_posix() == "/home/test/.claude/projects/-some-Project/memory"

    @pytest.mark.parametrize("project", [pytest.param(None, id="none"), pytest.param("", id="empty-string")])
    @mock.patch(_RUN, autospec=True, return_value=_completed("/Users/x/Project\n"))
    def test_missing_project_falls_back_to_git_root(
        self, mock_run: mock.Mock, set_home: Callable[[str], None], project: str | None
    ) -> None:
        """No project argument, or an empty one, engages the ``git rev-parse`` fallback exactly once.

        Scenario: ``None`` and ``""`` mean the same thing — "use the repository the caller is in" — and the git root
        feeds the slug.
        """
        set_home("/home/test")

        result = resolve_memory_dir.resolve_memory_dir(project)

        mock_run.assert_called_once()
        assert Path(result).as_posix() == "/home/test/.claude/projects/-Users-x-Project/memory"

    @pytest.mark.parametrize(
        ("result", "error"),
        [
            pytest.param(_completed(returncode=128, stderr="fatal: not a git repository\n"), None, id="no-repo"),
            pytest.param(None, FileNotFoundError("git"), id="git-not-on-path"),
        ],
    )
    @mock.patch(_RUN, autospec=True)
    def test_unresolvable_git_root_returns_none(
        self, mock_run: mock.Mock, result: subprocess.CompletedProcess[str] | None, error: Exception | None
    ) -> None:
        """A failing or missing git yields ``None`` instead of raising.

        Scenario: git exits non-zero outside a repository, or the binary is not on PATH (``FileNotFoundError``). Either
        way there is no project root to derive a memory directory from.
        """
        mock_run.return_value = result
        mock_run.side_effect = error

        assert resolve_memory_dir.resolve_memory_dir(None) is None


class TestMain:
    """Main: CLI surface — stdout + exit codes."""

    @mock.patch(_RUN, autospec=True, return_value=_completed("/Users/x/Repo\n"))
    def test_no_arg_inside_git_repo_prints_path(
        self, mock_run: mock.Mock, set_home: Callable[[str], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No CLI arg + git repo → prints resolved path, exit 0.

        Scenario: the CLI is run without arguments inside a repository whose root git reports as ``/Users/x/Repo``.
        """
        set_home("/h")

        rc = resolve_memory_dir.main([])

        assert rc == 0
        assert Path(capsys.readouterr().out.strip()).as_posix() == "/h/.claude/projects/-Users-x-Repo/memory"

    @pytest.mark.parametrize(
        ("project", "home", "expected"),
        [
            pytest.param("/some/Project", "/h", "/h/.claude/projects/-some-Project/memory", id="explicit-root"),
            pytest.param("/x", "/custom/home", "/custom/home/.claude/projects/-x/memory", id="custom-home-honoured"),
        ],
    )
    def test_explicit_arg_prints_path_under_home(
        self,
        set_home: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
        project: str,
        home: str,
        expected: str,
    ) -> None:
        """An explicit project root prints the matching memory path under the configured home, exit 0.

        Scenario: the home directory comes from the environment, proving ``os.path.expanduser`` integration — a custom
        home must move the whole ``.claude/projects`` tree with it.
        """
        set_home(home)

        rc = resolve_memory_dir.main([project])

        assert rc == 0
        assert Path(capsys.readouterr().out.strip()).as_posix() == expected

    @mock.patch(_RUN, autospec=True, return_value=_completed(returncode=128))
    def test_exit_1_when_no_git_root(self, mock_run: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """No arg + git failure → exit 1, no stdout.

        Scenario: the CLI runs outside any repository without an argument, so there is nothing to print.
        """
        rc = resolve_memory_dir.main([])

        assert rc == 1
        assert capsys.readouterr().out == ""
