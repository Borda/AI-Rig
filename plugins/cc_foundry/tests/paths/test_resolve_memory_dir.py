"""Tests for ``bin/resolve_memory_dir.py``.

``slugify`` is checked against Claude Code's own directory naming (doctests in the module plus the cases below); the
rest exercises git-bound behaviour via ``monkeypatch`` and the CLI surface via ``capsys``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import resolve_memory_dir


class _FakeCompleted:
    """Stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        """Store the configured completed-process fields."""
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_run(monkeypatch: pytest.MonkeyPatch, responder: Any) -> None:
    """Patch only the resolver module's subprocess runner with ``responder``."""
    monkeypatch.setattr(resolve_memory_dir.subprocess, "run", responder)


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
        """All edge cases map to the documented canonical slug."""
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
                "/private/var/folders/ab/c_defg/T",
                "-private-var-folders-ab-c-defg-T",
                id="underscore-in-macos-tmp",
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
        """Paths shaped like real ``~/.claude/projects`` entries and Windows/Unicode forms resolve as Claude Code names
        them."""
        assert resolve_memory_dir.slugify(raw) == expected

    def test_length_boundary_at_200_is_verbatim(self) -> None:
        """A 200-character slug is kept whole; 201 characters switch to cut-plus-hash."""
        assert resolve_memory_dir.slugify("/" + "a" * 199) == "-" + "a" * 199
        assert resolve_memory_dir.slugify("/" + "a" * 200) == "-" + "a" * 199 + "-b6ymvl"

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

    def test_explicit_project_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit arg bypasses git lookup entirely."""

        def _boom(*_: Any, **__: Any) -> _FakeCompleted:  # pragma: no cover
            """Fail if the explicit-project path unexpectedly runs subprocesses."""
            raise AssertionError("subprocess.run must not be called when project given")

        _patch_run(monkeypatch, _boom)
        monkeypatch.setenv("HOME", "/home/test")
        result = resolve_memory_dir.resolve_memory_dir("/some/Project")
        assert Path(result).as_posix() == "/home/test/.claude/projects/-some-Project/memory"

    def test_git_fallback_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty arg → git rev-parse provides the root."""

        def _fake_run(_argv: Sequence[str], **_: Any) -> _FakeCompleted:
            """Return the git root for this fallback-success scenario."""
            return _FakeCompleted(stdout="/Users/x/Project\n", returncode=0)

        _patch_run(monkeypatch, _fake_run)
        monkeypatch.setenv("HOME", "/home/test")
        result = resolve_memory_dir.resolve_memory_dir(None)
        assert Path(result).as_posix() == "/home/test/.claude/projects/-Users-x-Project/memory"

    def test_git_fallback_no_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Git fails (non-zero) → None."""

        def _fake_run(_argv: Sequence[str], **_: Any) -> _FakeCompleted:
            """Return git's non-repository result for this fallback scenario."""
            return _FakeCompleted(stdout="", stderr="fatal: not a git repository\n", returncode=128)

        _patch_run(monkeypatch, _fake_run)
        assert resolve_memory_dir.resolve_memory_dir(None) is None

    def test_git_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Git not on PATH → FileNotFoundError swallowed, returns None."""

        def _fake_run(*_: Any, **__: Any) -> _FakeCompleted:
            """Raise the missing-git error expected by this fallback case."""
            raise FileNotFoundError("git")

        _patch_run(monkeypatch, _fake_run)
        assert resolve_memory_dir.resolve_memory_dir(None) is None

    def test_empty_string_arg_falls_back_to_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty-string arg treated the same as None — git fallback engaged."""
        calls = {"n": 0}

        def _fake_run(_argv: Sequence[str], **_: Any) -> _FakeCompleted:
            """Count and satisfy the fallback lookup for an empty argument."""
            calls["n"] += 1
            return _FakeCompleted(stdout="/repo\n", returncode=0)

        _patch_run(monkeypatch, _fake_run)
        monkeypatch.setenv("HOME", "/h")
        result = resolve_memory_dir.resolve_memory_dir("")
        assert Path(result).as_posix() == "/h/.claude/projects/-repo/memory"
        assert calls["n"] == 1


class TestMain:
    """Main: CLI surface — stdout + exit codes."""

    def test_no_arg_with_git(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No CLI arg + git repo → prints resolved path, exit 0."""

        def _fake_run(_argv: Sequence[str], **_: Any) -> _FakeCompleted:
            """Return the git root consumed by the CLI success path."""
            return _FakeCompleted(stdout="/Users/x/Repo\n", returncode=0)

        _patch_run(monkeypatch, _fake_run)
        monkeypatch.setenv("HOME", "/h")
        rc = resolve_memory_dir.main([])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert Path(out).as_posix() == "/h/.claude/projects/-Users-x-Repo/memory"

    def test_explicit_arg(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Explicit project root prints corresponding memory path, exit 0."""
        monkeypatch.setenv("HOME", "/h")
        rc = resolve_memory_dir.main(["/some/Project"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert Path(out).as_posix() == "/h/.claude/projects/-some-Project/memory"

    def test_exit_1_when_no_git_root(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No arg + git failure → exit 1, no stdout."""

        def _fake_run(_argv: Sequence[str], **_: Any) -> _FakeCompleted:
            """Return git failure for the CLI's no-root path."""
            return _FakeCompleted(stdout="", returncode=128)

        _patch_run(monkeypatch, _fake_run)
        rc = resolve_memory_dir.main([])
        assert rc == 1
        assert capsys.readouterr().out == ""

    def test_home_expansion(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """HOME env var honoured — proves ``os.path.expanduser`` integration."""
        monkeypatch.setenv("HOME", "/custom/home")
        rc = resolve_memory_dir.main(["/x"])
        assert rc == 0
        assert Path(capsys.readouterr().out.strip()).as_posix().startswith("/custom/home/.claude/projects/")
