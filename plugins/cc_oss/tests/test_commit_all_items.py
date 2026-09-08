"""Tests for ``bin/commit_all_items.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. ``build_commit_message``
is tested directly as a pure function. Stdin, counts, and optional ``--codex`` / summaries-file paths are covered via
direct ``main(argv)`` calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import commit_all_items as cai


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture(name="fake_git")
def _fake_git(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch subprocess.run and which; record command lists.

    Default: exit 0.
    """
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record Git argv and return the response needed by the bulk-commit path."""
        recorded.append(list(cmd))
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if subcmd == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="/repo/my-project\n")
        if subcmd == "branch":
            return _FakeCompleted(returncode=0, stdout="main\n")
        if subcmd == "diff":
            return _FakeCompleted(returncode=1)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cai.subprocess, "run", _fake_run)
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    return recorded


def _commit_calls(recorded: list[list[str]]) -> list[list[str]]:
    """Return recorded ``git commit`` calls.

    Examples:
        >>> _commit_calls([["git", "add", "x"], ["git", "commit", "-m", "done"]])
        [['git', 'commit', '-m', 'done']]
    """
    return [cmd for cmd in recorded if len(cmd) > 1 and cmd[1] == "commit"]


def test_missing_pr_number_exits_1(fake_git: list[list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    """No PR_NUMBER → exit 1 with Usage message; git never invoked."""
    rc = cai.main([])
    assert rc == 1
    assert "Usage" in capsys.readouterr().err
    assert fake_git == []


@pytest.mark.parametrize("args", [["123", "abc", "0", "0"], ["123", "5", "abc", "0"], ["123", "5", "5", "abc"]])
def test_non_integer_count_exits_2(
    fake_git: list[list[str]],
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    """Non-integer count in any position → exit 2 with 'expected integer' on stderr."""
    rc = cai.main(args)
    assert rc == 2
    assert "expected integer" in capsys.readouterr().err


def test_negative_count_exits_2(fake_git: list[list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    """Negative integer (leading dash) fails isdigit() → exit 2."""
    rc = cai.main(["123", "-5", "0", "0"])
    assert rc == 2
    assert "expected integer" in capsys.readouterr().err


def test_successful_commit_exits_0(fake_git: list[list[str]]) -> None:
    """Valid args → git commit invoked once, exit 0."""
    rc = cai.main(["42", "3", "1", "0"])
    assert rc == 0
    commit_calls = _commit_calls(fake_git)
    assert len(commit_calls) == 1
    assert commit_calls[0][1:3] == ["commit", "-m"]


def test_commit_message_contains_pr_and_counts(fake_git: list[list[str]]) -> None:
    """Commit message includes PR reference and all three challenge-log counts."""
    cai.main(["#99", "2", "1", "0"])
    msg = _commit_calls(fake_git)[0][3]
    assert "PR #99" in msg
    assert "2 as-suggested" in msg
    assert "1 self-resolved" in msg
    assert "0 rejected" in msg


def test_codex_flag_adds_trailer(fake_git: list[list[str]]) -> None:
    """Include the Codex co-author trailer when requested."""
    cai.main(["42", "3", "1", "0", "--codex"])
    msg = _commit_calls(fake_git)[0][3]
    assert "Co-authored-by: OpenAI Codex" in msg


def test_no_codex_flag_omits_trailer(fake_git: list[list[str]]) -> None:
    """Without ``--codex``, OpenAI Codex trailer absent from message."""
    cai.main(["42", "3", "1", "0"])
    msg = _commit_calls(fake_git)[0][3]
    assert "OpenAI Codex" not in msg


def test_summaries_file_content_in_message(fake_git: list[list[str]], tmp_path: Path) -> None:
    """Summaries file text appears verbatim in commit message body."""
    sfile = tmp_path / "summaries.txt"
    sfile.write_text("- fixed foo\n- refactored bar\n")
    cai.main(["42", "3", "1", "0", str(sfile)])
    msg = _commit_calls(fake_git)[0][3]
    assert "- fixed foo" in msg
    assert "- refactored bar" in msg


def test_missing_summaries_file_ignored(fake_git: list[list[str]], tmp_path: Path) -> None:
    """Non-existent summaries file path → no error, commit proceeds normally."""
    rc = cai.main(["42", "3", "1", "0", str(tmp_path / "nosuchfile.txt")])
    assert rc == 0
    assert len(_commit_calls(fake_git)) == 1


def test_git_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(cai, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="git"):
        cai.main(["42", "3", "1", "0"])


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_0_without_git(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Print help without invoking Git or another subprocess."""
    called: list[Any] = []
    monkeypatch.setattr(cai.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit) as exc:
        cai.main([flag])
    assert exc.value.code == 0
    assert called == []


def test_golden_all_mode_invocation_constructs_expected_commit(fake_git: list[list[str]]) -> None:
    """Exact COMMIT_MODE=all call-site argv → single ``git commit -m`` with expected PR/counts."""
    rc = cai.main(["#42", "3", "1", "0", "", "--codex"])
    assert rc == 0
    commit_calls = _commit_calls(fake_git)
    assert len(commit_calls) == 1
    assert commit_calls[0][:3] == ["/fake/git", "commit", "-m"]
    msg = commit_calls[0][3]
    assert "PR #42" in msg
    assert "3 as-suggested, 1 self-resolved, 0 rejected" in msg
    assert "Co-authored-by: OpenAI Codex" in msg


def test_build_commit_message_pure() -> None:
    """Return expected subject and counts; no subprocess needed."""
    msg = cai.build_commit_message("#7", 5, 2, 1, "", False)
    assert "PR #7" in msg
    assert "5 as-suggested" in msg
    assert "2 self-resolved" in msg
    assert "1 rejected" in msg
    assert "Co-authored-by: claude[bot]" in msg
    assert "OpenAI Codex" not in msg
