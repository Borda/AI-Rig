"""Tests for ``bin/commit_lint_fixes.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. Tests cover the no-op
path (empty diff) and the stage-and-commit path, including commit-failure forwarding.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import subprocess

import pytest

import commit_lint_fixes as clf


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_0_without_git(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Print help without invoking Git or another subprocess."""
    called: list[Any] = []
    monkeypatch.setattr(clf.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit) as exc:
        clf.main([flag])
    assert exc.value.code == 0
    assert called == []


def test_golden_zeroarg_invocation_constructs_expected_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact zero-arg call-site (empty argv) → git add/commit argv identical to pre-argparse baseline."""
    call_n = [0]
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return changed files on the discovery call and empty output thereafter."""
        calls.append(list(cmd))
        call_n[0] += 1
        stdout = "src/a.py\ndocs/b.md\n" if call_n[0] == 1 else ""
        return _FakeCompleted(returncode=0, stdout=stdout)

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    rc = clf.main([])
    assert rc == 0
    add_calls = [c for c in calls if len(c) > 1 and c[1] == "add"]
    commit_calls = [c for c in calls if len(c) > 1 and c[1] == "commit"]
    assert add_calls == [["/fake/git", "add", "--", "src/a.py", "docs/b.md"]]
    assert commit_calls == [["/fake/git", "commit", "-m", clf._COMMIT_MESSAGE]]


def test_no_changed_files_prints_message(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Empty ``git diff HEAD`` → prints no-op message and exits 0."""
    monkeypatch.setattr(
        clf.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=""),
    )
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    rc = clf.main()
    assert rc == 0
    assert "[lint] no changed files" in capsys.readouterr().out


def test_no_changed_files_no_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty diff → ``git commit`` never invoked."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record commands while making the working tree appear unchanged."""
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    clf.main()
    assert all("commit" not in c for c in calls)


def test_changed_files_stages_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changed file in diff → ``git add`` then ``git commit`` both invoked."""
    call_n = [0]
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return one changed file, then successful responses for Git mutations."""
        calls.append(list(cmd))
        call_n[0] += 1
        stdout = "foo.py\n" if call_n[0] == 1 else ""
        return _FakeCompleted(returncode=0, stdout=stdout)

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    rc = clf.main()
    assert rc == 0
    subcmds = [c[1] for c in calls]
    assert "add" in subcmds
    assert "commit" in subcmds


def test_changed_files_are_added_before_commit_with_exact_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple changed files, including spaces, are passed as separate git-add args before commit."""
    call_n = [0]
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return two changed paths followed by successful Git responses."""
        calls.append(list(cmd))
        call_n[0] += 1
        stdout = "src/a.py\ndocs/file with spaces.md\n" if call_n[0] == 1 else ""
        return _FakeCompleted(returncode=0, stdout=stdout)

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    rc = clf.main()

    assert rc == 0
    add_index = next(i for i, cmd in enumerate(calls) if cmd[1] == "add")
    commit_index = next(i for i, cmd in enumerate(calls) if cmd[1] == "commit")
    assert add_index < commit_index
    assert calls[add_index] == ["/fake/git", "add", "--", "src/a.py", "docs/file with spaces.md"]
    assert calls[commit_index][:3] == ["/fake/git", "commit", "-m"]


def test_git_add_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git add check=True failures are not converted into a misleading successful commit."""
    call_n = [0]

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Raise only for ``git add`` after reporting one changed file."""
        call_n[0] += 1
        if call_n[0] == 1:
            return _FakeCompleted(returncode=0, stdout="src/a.py\n")
        if cmd[1] == "add":
            raise subprocess.CalledProcessError(returncode=2, cmd=cmd)
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        clf.main()
    assert exc_info.value.returncode == 2


def test_changed_files_commit_message_contains_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit message body references lint fix."""
    call_n = [0]
    commit_msgs: list[str] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Expose the commit message after reporting one changed file."""
        call_n[0] += 1
        if call_n[0] == 1:
            return _FakeCompleted(returncode=0, stdout="changed.py\n")
        if "commit" in cmd:
            commit_msgs.append(cmd[cmd.index("-m") + 1])
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    clf.main()
    assert commit_msgs
    assert "lint" in commit_msgs[0].lower()


def test_commit_failure_forwards_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Propagate a failing Git commit exit code."""
    call_n = [0]

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return a failing commit response after reporting one changed file."""
        call_n[0] += 1
        if call_n[0] == 1:
            return _FakeCompleted(returncode=0, stdout="bar.py\n")
        if "commit" in cmd:
            return _FakeCompleted(returncode=1, stdout="")
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(clf.subprocess, "run", _fake_run)
    monkeypatch.setattr(clf, "which", lambda _: "/fake/git")
    rc = clf.main()
    assert rc == 1


def test_git_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(clf, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="git"):
        clf.main()


def test_windows_sentinel_uses_native_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A POSIX TMPDIR inherited by Windows cannot redirect the commit sentinel."""
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setattr(clf.sys, "platform", "win32")
    monkeypatch.setattr(clf.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        clf.subprocess,
        "run",
        lambda cmd, **_kwargs: _FakeCompleted(stdout="/repo/my-project\n" if cmd[1] == "rev-parse" else "main\n"),
    )

    assert clf._sentinel_path("/fake/git") == tmp_path / "claude-commit-auth-my-project-main"
