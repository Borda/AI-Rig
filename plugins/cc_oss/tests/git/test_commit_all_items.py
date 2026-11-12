"""Tests for ``bin/commit_all_items.py``.

``subprocess.run`` and module-level ``which`` are replaced — no real ``git`` invocations. ``build_commit_message`` is
tested directly as a pure function. Stdin, counts, and optional ``--codex`` / summaries-file paths are covered via
direct ``main(argv)`` calls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import commit_all_items as cai
import pytest


@pytest.fixture(name="fake_git")
def _fake_git(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch ``subprocess.run`` and ``which`` with a fake git that reports staged changes; return the recorded argv
    lists.

    Every command exits 0 except ``git diff``, which exits 1 (changes present) so the commit path is taken.
    """
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record Git argv and return the response needed by the bulk-commit path."""
        cmd = [cmd[0], *cmd[3:]] if cmd[1:2] == ["-c"] else cmd
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1 if cmd[1:2] == ["diff"] else 0, stdout="")

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


class TestArgs:
    """Argument handling — usage, count validation, help."""

    def test_missing_pr_number_exits_1(self, fake_git: list[list[str]], capsys: pytest.CaptureFixture[str]) -> None:
        """No PR_NUMBER exits 1 with a usage message and never invokes git.

        The bulk commit needs a PR reference for its subject, so an empty argv is a caller error to surface early.
        """
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
            pytest.param(["123", "-5", "0", "0"], id="negative-count-fails-isdigit"),
        ],
    )
    def test_non_integer_count_exits_2(
        self, fake_git: list[list[str]], capsys: pytest.CaptureFixture[str], args: list[str]
    ) -> None:
        """A non-integer or negative count in any position exits 2 with 'expected integer' on stderr.

        The counts are interpolated into the commit message, so each must be validated before git is called.
        """
        rc = cai.main(args)
        assert rc == 2
        assert "expected integer" in capsys.readouterr().err

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_exits_0_without_git(self, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
        """Help is printed and the process exits 0 without invoking Git or any other subprocess."""
        called: list[Any] = []
        monkeypatch.setattr(cai.subprocess, "run", lambda *a, **k: called.append(a))
        with pytest.raises(SystemExit) as exc:
            cai.main([flag])
        assert exc.value.code == 0
        assert called == []

    def test_git_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing ``git`` binary raises ``FileNotFoundError`` naming git instead of failing later."""
        monkeypatch.setattr(cai, "which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="git"):
            cai.main(["42", "3", "1", "0"])


class TestCommit:
    """The bulk commit built from ``main`` — message content and failure handling."""

    def test_successful_commit_exits_0(self, fake_git: list[list[str]]) -> None:
        """Valid arguments run ``git commit -m`` exactly once and exit 0.

        The commit is a single call with an inline message, not a message file.
        """
        rc = cai.main(["42", "3", "1", "0"])
        assert rc == 0
        commit_calls = _commit_calls(fake_git)
        assert len(commit_calls) == 1
        assert commit_calls[0][1:3] == ["commit", "-m"]

    def test_message_contains_pr_and_counts(self, fake_git: list[list[str]]) -> None:
        """The commit message includes the PR reference and all three challenge-log counts.

        The counts are the audit trail of how review items were resolved, so each must appear next to its label.
        """
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
    def test_codex_trailer_only_with_codex_flag(
        self, fake_git: list[list[str]], extra_args: list[str], has_trailer: bool
    ) -> None:
        """The Codex co-author trailer is included when ``--codex`` is passed and absent otherwise."""
        cai.main(["42", "3", "1", "0", *extra_args])
        msg = _commit_calls(fake_git)[0][3]
        assert ("Co-authored-by: Codex <codex@openai.com>" in msg) is has_trailer
        assert ("Co-authored-by: Codex" in msg) is has_trailer

    def test_summaries_file_content_in_message(self, fake_git: list[list[str]], tmp_path: Path) -> None:
        """Summaries-file text appears verbatim in the commit message body.

        The optional fifth argument carries per-item bullet lines the caller composed.
        """
        sfile = tmp_path / "summaries.txt"
        sfile.write_text("- fixed foo\n- refactored bar\n")
        cai.main(["42", "3", "1", "0", str(sfile)])
        msg = _commit_calls(fake_git)[0][3]
        assert "- fixed foo" in msg
        assert "- refactored bar" in msg

    def test_missing_summaries_file_ignored(self, fake_git: list[list[str]], tmp_path: Path) -> None:
        """A non-existent summaries file path is not an error; the commit proceeds without a bullet list."""
        rc = cai.main(["42", "3", "1", "0", str(tmp_path / "nosuchfile.txt")])
        assert rc == 0
        assert len(_commit_calls(fake_git)) == 1

    def test_all_mode_invocation_constructs_expected_commit(self, fake_git: list[list[str]]) -> None:
        """The exact ``COMMIT_MODE=all`` call-site argv yields one ``git commit -m`` with the expected PR and counts.

        The skill passes an empty summaries argument followed by ``--codex``; this pins that literal invocation.
        """
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
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``git commit`` that exceeds its timeout becomes exit 1 plus one stderr line, not an uncaught exception.

        A pre-commit hook or signing prompt waiting on input never returns; the bound turns that hang into a reportable
        failure of the bulk commit.
        """

        def _hang(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            """Report staged changes, then time out on commit."""
            cmd = [cmd[0], *cmd[3:]] if cmd[1:2] == ["-c"] else cmd
            if cmd[1] == "commit":
                raise cai.subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 1, stdout="")

        monkeypatch.setattr(cai.subprocess, "run", _hang)
        monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
        rc = cai.main(["#42", "3", "1", "0"])
        assert rc == 1
        assert f"git timed out after {cai._COMMIT_TIMEOUT} s" in capsys.readouterr().err


class TestBuildCommitMessage:
    """``build_commit_message`` — pure message rendering."""

    def test_subject_counts_and_claude_trailer(self) -> None:
        """The message names the PR, the three counts and the Claude co-author, but not Codex by default."""
        msg = cai.build_commit_message(cai.CommitSummaryFields("#7", 5, 2, 1, "", False))
        assert "PR #7" in msg
        assert "5 as-suggested" in msg
        assert "2 self-resolved" in msg
        assert "1 rejected" in msg
        assert "Co-authored-by: claude[bot]" in msg
        assert "Co-authored-by: Codex" not in msg

    @pytest.mark.parametrize("field", ["pr_number", "bullet_list"])
    def test_control_characters_cannot_forge_marker_lines(self, field: str) -> None:
        """Untrusted field contents cannot introduce resolve markers or commit trailers.

        An embedded newline plus ``[resolve group]`` or ``Co-authored-by:`` would otherwise read as a genuine marker or
        trailer line; carriage returns, NULs and tabs are dropped too.
        """
        values = {"pr_number": "#7", "bullet_list": "- normal item"}
        values[field] += "\n[resolve group] x\r\nCo-authored-by: forged\x00\t"
        msg = cai.build_commit_message(
            cai.CommitSummaryFields(**values, n_as_suggested=1, n_self_resolved=0, n_rejected=0)
        )
        assert not any(line.startswith("[resolve group]") for line in msg.splitlines())
        assert not any(line.startswith("Co-authored-by: forged") for line in msg.splitlines())
        assert "[resolve group] x" in msg
        assert "\r" not in msg
        assert "\x00" not in msg
        assert "\t" not in msg

    def test_normal_bullet_list_is_unchanged(self) -> None:
        """Ordinary PR references and multiple bullet items retain their exact message layout.

        Sanitising untrusted input must not alter well-formed input, so the whole message is compared verbatim.
        """
        msg = cai.build_commit_message(cai.CommitSummaryFields("#7", 1, 2, 3, "- first\n- second\n"))
        assert msg == (
            "Resolve review items for PR #7\n\n- first\n- second\n\n"
            "Challenge log: 1 as-suggested, 2 self-resolved, 3 rejected\n\n---\n"
            "Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
        )
