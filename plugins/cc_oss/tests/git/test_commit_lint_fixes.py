"""Tests for ``bin/commit_lint_fixes.py``.

``subprocess.run`` and module-level ``which`` are replaced by a scripted fake git, except for one scratch-repository
test that runs real git. Tests cover the no-op path (empty diff), the scoping of the commit to the files the branch
changed, and failure forwarding without a traceback. The target-branch sentinel is written under an isolated ``TMPDIR``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import commit_lint_fixes as clf
import pytest

_GIT = "/fake/git"
_MERGE_BASE = "abc123"


def _done(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Build the ``CompletedProcess`` a scripted git call returns."""
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _z(*names: str) -> bytes:
    """Encode ``names`` as ``git -z`` prints them: each followed by a NUL byte."""
    return b"".join(os.fsencode(name) + b"\0" for name in names)


class _FakeGit:
    """Scripted Git that answers by sub-command and records every argv it receives."""

    def __init__(
        self,
        *,
        dirty: tuple[str, ...] = (),
        branch: tuple[str, ...] = (),
        fail: dict[str, subprocess.CompletedProcess[bytes] | BaseException] | None = None,
    ) -> None:
        """Configure the working-tree changes, the branch's changed files and per-sub-command failures."""
        self.dirty = dirty
        self.branch = branch
        self.fail = fail or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return the scripted answer for one Git invocation."""
        cmd = [cmd[0], *cmd[3:]] if cmd[1:2] == ["-c"] else cmd
        self.calls.append(list(cmd))
        sub = next(arg for arg in cmd[1:] if not arg.startswith("--"))
        scripted = self.fail.get(sub)
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            return scripted
        if sub == "merge-base":
            return _done(stdout=f"{_MERGE_BASE}\n".encode())
        if sub == "diff":
            return _done(stdout=_z(*(self.branch if cmd[-2] == _MERGE_BASE else self.dirty)))
        return _done()

    def subcommands(self) -> list[str]:
        """List the sub-command of every recorded call, in order."""
        return [next(arg for arg in call[1:] if not arg.startswith("--")) for call in self.calls]

    def call(self, sub: str) -> list[str]:
        """Return the argv of the single recorded call to ``sub``."""
        (match,) = [c for c in self.calls if sub in c[1:3]]
        return match


@pytest.fixture
def base_ref_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Publish the target-branch sentinel under an isolated ``TMPDIR``, as ``oss:resolve`` does before its lint gate."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    sentinel = tmp_path / "resolve-base-ref-shared"
    sentinel.write_text("main\n", encoding="utf-8")
    return sentinel


@pytest.fixture
def install_git(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _FakeGit]:
    """Return a factory that builds a ``_FakeGit`` and routes the script's ``subprocess.run`` and ``which`` to it."""

    def install(**config: Any) -> _FakeGit:
        """Create the scripted git from ``config`` and install it."""
        fake = _FakeGit(**config)
        monkeypatch.setattr(clf.subprocess, "run", fake)
        monkeypatch.setattr(clf, "which", lambda _: _GIT)
        return fake

    return install


class TestArgs:
    """Argument handling and tool discovery — failures happen before any git call."""

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_exits_0_without_git(self, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
        """Help is printed and the process exits 0 without invoking Git or any other subprocess."""
        called: list[Any] = []
        monkeypatch.setattr(clf.subprocess, "run", lambda *a, **k: called.append(a))
        with pytest.raises(SystemExit) as exc:
            clf.main([flag])
        assert exc.value.code == 0
        assert called == []

    def test_git_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing ``git`` binary raises ``FileNotFoundError`` naming git."""
        monkeypatch.setattr(clf, "which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="git"):
            clf.main()


class TestScope:
    """The commit is limited to the dirty files that the branch itself changed."""

    def test_zero_arg_invocation_constructs_expected_commands(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit]
    ) -> None:
        """A zero-arg call with every dirty file inside the branch scope stages and commits exactly those files.

        The commit carries a trailing path list so that anything else already staged in the index is not swept in, and
        ``--literal-pathspecs`` keeps glob characters in a name from matching other files.
        """
        fake = install_git(dirty=("src/a.py", "docs/b.md"), branch=("src/a.py", "docs/b.md", "src/c.py"))
        rc = clf.main([])
        assert rc == 0
        assert fake.call("add") == [_GIT, "--literal-pathspecs", "add", "--", "src/a.py", "docs/b.md"]
        assert fake.call("commit") == [
            _GIT,
            "--literal-pathspecs",
            "commit",
            "-m",
            clf._COMMIT_MESSAGE,
            "--",
            "src/a.py",
            "docs/b.md",
        ]

    def test_scope_is_computed_against_the_sentinel_base_ref(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit]
    ) -> None:
        """The merge base is taken against ``origin/<base ref>`` from the sentinel; the branch diff ends at ``HEAD``."""
        fake = install_git(dirty=("a.py",), branch=("a.py",))
        clf.main([])
        assert fake.call("merge-base") == [_GIT, "merge-base", "HEAD", "origin/main"]
        assert fake.calls[2] == [_GIT, "diff", "-z", "--name-only", "--no-renames", _MERGE_BASE, "HEAD"]

    def test_no_changed_files_prints_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        install_git: Callable[..., _FakeGit],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty ``git diff HEAD`` prints the no-op message and exits 0, even with no sentinel published.

        Nothing is dirty, so the target branch is never needed and only the single ``diff`` call runs.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        fake = install_git()
        rc = clf.main()
        assert rc == 0
        assert "[lint] no changed files" in capsys.readouterr().out
        assert fake.subcommands() == ["diff"]

    def test_changed_file_names_survive_nul_parsing(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit]
    ) -> None:
        """Names with a space and an embedded newline reach ``git add`` and ``git commit`` as single arguments.

        Newline-separated ``--name-only`` output splits such a name in two and quotes some others; ``-z`` output is the
        only form that carries them verbatim.
        """
        names = ("src/a.py", "docs/file with spaces.md", "weird\nname.md")
        fake = install_git(dirty=names, branch=names)
        rc = clf.main()
        assert rc == 0
        assert fake.call("add")[3:] == ["--", *names]
        assert fake.call("commit")[-len(names) - 1 :] == ["--", *names]

    def test_files_outside_the_branch_scope_are_not_staged(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unrelated dirty file stays out of the bot commit and is reported by name.

        A tracked, locally modified file the PR never touched (a config holding a credential, say) shows up in ``git
        diff HEAD`` but lies outside the files the lint step reviews; committing it would publish it to the fork.
        """
        fake = install_git(dirty=("src/a.py", "config/local.env"), branch=("src/a.py",))
        rc = clf.main()
        assert rc == 0
        assert fake.call("add")[3:] == ["--", "src/a.py"]
        assert fake.call("commit")[-2:] == ["--", "src/a.py"]
        assert "left uncommitted (outside the branch's changed files): config/local.env" in capsys.readouterr().out

    def test_nothing_in_scope_commits_nothing(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When every dirty file is outside the branch scope, neither ``add`` nor ``commit`` runs and the exit is 0."""
        fake = install_git(dirty=("config/local.env",), branch=("src/a.py",))
        rc = clf.main()
        assert rc == 0
        assert "add" not in fake.subcommands()
        assert "commit" not in fake.subcommands()
        assert "no lint-scope changes" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "write_sentinel",
        [
            pytest.param(lambda _path: None, id="sentinel-missing"),
            pytest.param(lambda path: path.write_text("", encoding="utf-8"), id="sentinel-empty"),
        ],
    )
    def test_missing_base_ref_refuses_to_commit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        install_git: Callable[..., _FakeGit],
        capsys: pytest.CaptureFixture[str],
        write_sentinel: Callable[[Path], object],
    ) -> None:
        """Without a target branch the scope is unknowable, so nothing is staged or committed and the exit is 1.

        Guessing the scope (or falling back to every dirty file) is the over-broad behaviour this guard replaces. The
        sentinel is either absent or empty.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        write_sentinel(tmp_path / "resolve-base-ref-shared")
        fake = install_git(dirty=("src/a.py",), branch=("src/a.py",))
        rc = clf.main()
        assert rc == 1
        assert fake.subcommands() == ["diff"]
        assert "resolve-base-ref-<CSID>" in capsys.readouterr().err


class TestFailures:
    """Git failures become exit codes and one-line messages, never tracebacks."""

    @pytest.mark.parametrize(
        ("failing", "returncode"),
        [
            pytest.param("diff", 128, id="diff-fails"),
            pytest.param("merge-base", 1, id="merge-base-fails"),
            pytest.param("add", 2, id="add-fails"),
        ],
    )
    def test_plumbing_failure_is_forwarded_without_traceback(
        self,
        base_ref_sentinel: Path,
        install_git: Callable[..., _FakeGit],
        capsys: pytest.CaptureFixture[str],
        failing: str,
        returncode: int,
    ) -> None:
        """A failing plumbing call returns git's own exit code with a one-line stderr message and no commit.

        ``check=True`` used to surface these as a ``CalledProcessError`` traceback, and an empty ``git diff`` result
        from a failed call used to read as "no changed files" with exit 0.
        """
        failure = _done(returncode=returncode, stderr=b"fatal: boom\nmore detail\n")
        fake = install_git(dirty=("src/a.py",), branch=("src/a.py",), fail={failing: failure})
        rc = clf.main()
        assert rc == returncode
        assert "commit" not in fake.subcommands()
        assert f"[lint] git {failing} failed: fatal: boom" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(subprocess.TimeoutExpired(cmd="git", timeout=3), id="timeout"),
            pytest.param(OSError("exec format error"), id="oserror"),
        ],
    )
    def test_git_that_cannot_run_exits_1_without_traceback(
        self,
        base_ref_sentinel: Path,
        install_git: Callable[..., _FakeGit],
        capsys: pytest.CaptureFixture[str],
        error: BaseException,
    ) -> None:
        """A timed-out or unlaunchable plumbing call becomes exit 1 plus a stderr line, not an uncaught exception."""
        install_git(fail={"diff": error})
        rc = clf.main()
        assert rc == 1
        assert "[lint] git diff could not run" in capsys.readouterr().err

    def test_commit_failure_forwards_exit_code(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit]
    ) -> None:
        """A failing ``git commit`` exit code is forwarded as the script's own."""
        install_git(dirty=("bar.py",), branch=("bar.py",), fail={"commit": _done(returncode=1)})
        assert clf.main() == 1

    def test_commit_timeout_exits_1_without_traceback(
        self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``git commit`` that exceeds its timeout becomes exit 1 plus one stderr line, not an uncaught exception.

        A pre-commit hook or signing prompt that waits on input never returns; the bound turns the hang into a
        reportable failure.
        """
        timeout = subprocess.TimeoutExpired(cmd="git", timeout=clf._COMMIT_TIMEOUT)
        install_git(dirty=("a.py",), branch=("a.py",), fail={"commit": timeout})
        rc = clf.main()
        assert rc == 1
        assert f"[lint] git commit timed out after {clf._COMMIT_TIMEOUT} s" in capsys.readouterr().err


class TestCommitMessage:
    """The commit message the lint step writes."""

    def test_commit_message_mentions_lint(self, base_ref_sentinel: Path, install_git: Callable[..., _FakeGit]) -> None:
        """The ``-m`` message of the lint commit references lint.

        Reviewers identify the bot commit by its message, so the word must survive any rewording of the full subject.
        """
        fake = install_git(dirty=("changed.py",), branch=("changed.py",))
        clf.main()
        commit = fake.call("commit")
        assert "lint" in commit[commit.index("-m") + 1].lower()


class TestRealRepository:
    """Real git in a scratch repository — the scoping behaviour itself is the contract."""

    def test_commits_only_branch_files(self, scratch_repo: Any, base_ref_sentinel: Path) -> None:
        """In a real repository the lint commit holds the branch's file and leaves an unrelated local edit uncommitted.

        The branch changed ``src.py`` since ``origin/main``; ``base.txt`` was never part of the branch, so its local
        edit (a stand-in for a credentials file) must not land in the bot-authored commit.
        """
        scratch_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        scratch_repo.git("switch", "-q", "-c", "feature")
        (scratch_repo.path / "src.py").write_text("x=1\n", encoding="utf-8")
        scratch_repo.git("add", "src.py")
        scratch_repo.git("commit", "-q", "-m", "feature work")
        (scratch_repo.path / "src.py").write_text("x = 1\n", encoding="utf-8")
        (scratch_repo.path / "base.txt").write_text("local secret\n", encoding="utf-8")
        rc = clf.main()
        assert rc == 0
        assert scratch_repo.git("show", "--name-only", "--format=%s", "HEAD").split("\n")[:3] == [
            "lint: auto-fix violations after resolve cycle",
            "",
            "src.py",
        ]
        assert scratch_repo.git("status", "--porcelain").splitlines() == [" M base.txt"]
