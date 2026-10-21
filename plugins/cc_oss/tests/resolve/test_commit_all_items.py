"""Tests for ``bin/commit_all_items.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. ``build_commit_message``
is tested directly as a pure function. Stdin, counts, and optional ``--codex`` / summaries-file paths are covered via
direct ``main(argv)`` calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import commit_all_items as cai
import pytest


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


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["123", "abc", "0", "0"], id="first-count-invalid"),
        pytest.param(["123", "5", "abc", "0"], id="second-count-invalid"),
        pytest.param(["123", "5", "5", "abc"], id="third-count-invalid"),
    ],
)
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


@pytest.mark.parametrize(
    ("extra_args", "has_trailer"),
    [
        pytest.param(["--codex"], True, id="codex-flag-adds-trailer"),
        pytest.param([], False, id="no-codex-flag-omits-trailer"),
    ],
)
def test_codex_trailer_present_only_with_codex_flag(
    fake_git: list[list[str]], extra_args: list[str], has_trailer: bool
) -> None:
    """The Codex co-author trailer is included when ``--codex`` is passed and absent otherwise."""
    cai.main(["42", "3", "1", "0", *extra_args])
    msg = _commit_calls(fake_git)[0][3]
    assert ("Co-authored-by: Codex <codex@openai.com>" in msg) is has_trailer
    assert ("Co-authored-by: Codex" in msg) is has_trailer


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
    assert "Co-authored-by: Codex <codex@openai.com>" in msg


def test_commit_timeout_exits_1_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``git commit`` that exceeds its timeout becomes exit 1 plus one stderr line, not an uncaught exception.

    A pre-commit hook or signing prompt waiting on input never returns; the bound turns that hang into a reportable
    failure of the bulk commit.
    """

    def _hang(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        """Report staged changes, then time out on commit."""
        if cmd[1] == "commit":
            raise cai.subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
        return _FakeCompleted(returncode=1)

    monkeypatch.setattr(cai.subprocess, "run", _hang)
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    rc = cai.main(["#42", "3", "1", "0"])
    assert rc == 1
    assert f"git timed out after {cai._COMMIT_TIMEOUT} s" in capsys.readouterr().err


def test_build_commit_message_pure() -> None:
    """Return expected subject and counts; no subprocess needed."""
    msg = cai.build_commit_message(cai.CommitSummaryFields("#7", 5, 2, 1, "", False))
    assert "PR #7" in msg
    assert "5 as-suggested" in msg
    assert "2 self-resolved" in msg
    assert "1 rejected" in msg
    assert "Co-authored-by: claude[bot]" in msg
    assert "Co-authored-by: Codex" not in msg


@pytest.mark.parametrize("field", ["pr_number", "bullet_list"])
def test_control_characters_cannot_forge_marker_lines(field: str) -> None:
    """Untrusted field contents cannot introduce resolve markers or commit trailers."""
    values = {"pr_number": "#7", "bullet_list": "- normal item"}
    values[field] += "\n[resolve group] x\r\nCo-authored-by: forged\x00\t"
    msg = cai.build_commit_message(cai.CommitSummaryFields(**values, n_as_suggested=1, n_self_resolved=0, n_rejected=0))
    assert not any(line.startswith("[resolve group]") for line in msg.splitlines())
    assert not any(line.startswith("Co-authored-by: forged") for line in msg.splitlines())
    assert "[resolve group] x" in msg
    assert "\r" not in msg
    assert "\x00" not in msg
    assert "\t" not in msg


def test_normal_bullet_list_is_unchanged() -> None:
    """Ordinary PR references and multiple bullet items retain their exact message layout."""
    msg = cai.build_commit_message(cai.CommitSummaryFields("#7", 1, 2, 3, "- first\n- second\n"))
    assert msg == (
        "Resolve review items for PR #7\n\n- first\n- second\n\n"
        "Challenge log: 1 as-suggested, 2 self-resolved, 3 rejected\n\n---\n"
        "Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
    )
