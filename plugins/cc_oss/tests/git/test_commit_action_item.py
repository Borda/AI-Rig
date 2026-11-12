"""Tests for ``bin/commit_action_item.py``.

``subprocess.run`` and ``which`` are replaced — no real git invocation, except the one scratch-repository test that pins
literal-pathspec staging. Tests cover argument validation, the early return for an empty stage, the commit paths
(message file, ``--build``, ``--build-group``), and the pure message builders.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import commit_action_item as cai
import pytest

_CLAUDE_TRAILER = "Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
_BUILD_ARGV = [
    "--build",
    "--summary",
    "Fix off-by-one",
    "--item-id",
    "3",
    "--author",
    "octocat",
    "--pr",
    "42",
    "--comment",
    "full comment text",
    "--challenge",
    "evidence=VALID suggestion=VALID resolution=as-suggested",
]


def _subcommand(cmd: list[str]) -> str:
    """Return the git sub-command of a recorded argv, skipping global ``--`` options such as ``--literal-pathspecs``."""
    cmd = [cmd[0], *cmd[3:]] if cmd[1:2] == ["-c"] else cmd
    return next((arg for arg in cmd[1:] if not arg.startswith("--")), "")


def _calls_to(calls: list[list[str]], sub: str) -> list[list[str]]:
    """Return the recorded argv lists whose git sub-command is ``sub``."""
    return [c for c in calls if _subcommand(c) == sub]


def _each(**overrides: Any) -> cai.EachMessageFields:
    """Build ``EachMessageFields`` with minimal valid values, overriding the given fields."""
    base = {"summary": "s", "item_id": "1", "author": "a", "pr": "9", "comment": "c", "challenge": "evidence=VALID"}
    return cai.EachMessageFields(**{**base, **overrides})


@pytest.fixture
def msg_file(tmp_path: Path) -> Path:
    """Write a minimal commit-message file and return its path."""
    path = tmp_path / "msg.txt"
    path.write_text("msg\n")
    return path


@pytest.fixture
def install_git(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Return a factory that installs a fake git and yields the list of recorded argv lists.

    ``empty_stage=True`` makes ``git diff --cached --quiet`` report nothing staged; ``commit_rc`` is the exit code of
    ``git commit``. Every other git command exits 0.
    """

    def install(*, empty_stage: bool = False, commit_rc: int = 0) -> list[list[str]]:
        """Patch ``which`` and ``subprocess.run`` for the commit script."""
        calls: list[list[str]] = []
        codes = {"diff": 0 if empty_stage else 1, "commit": commit_rc}

        def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            """Record the argv and answer with the scripted exit code for its sub-command."""
            cmd = [cmd[0], *cmd[3:]] if cmd[1:2] == ["-c"] else cmd
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, codes.get(_subcommand(cmd), 0), stdout="")

        monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
        monkeypatch.setattr(cai.subprocess, "run", fake_run)
        return calls

    return install


class TestArgs:
    """Argument parsing and validation — failures happen before any git call."""

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_exits_0_without_git(self, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
        """Help is printed and the process exits 0 without invoking Git or any other subprocess."""
        called: list[Any] = []
        monkeypatch.setattr(cai.subprocess, "run", lambda *a, **k: called.append(a))
        with pytest.raises(SystemExit) as exc:
            cai.main([flag])
        assert exc.value.code == 0
        assert called == []

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            pytest.param([], "--message-file required", id="no-args-message-file-required"),
            pytest.param(["--unknown"], "unknown arg", id="unrecognized-flag"),
            pytest.param(
                ["--build", "--message-file", "m.txt", "--files", "a.py"], "not both", id="build-and-message-file"
            ),
        ],
    )
    def test_invalid_args_exit_1_with_stderr_message(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
    ) -> None:
        """No args, an unrecognized flag, or ``--build`` combined with ``--message-file`` exits 1 with a stderr message.

        The two ways to supply a message are mutually exclusive, so the combination must be rejected rather than
        silently preferring one.
        """
        rc = cai.main(argv)
        assert rc == 1
        assert message in capsys.readouterr().err

    def test_message_file_not_found_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``--message-file`` path that does not exist is rejected with a 'not found' message."""
        rc = cai.main(["--message-file", str(tmp_path / "nonexistent.txt")])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_missing_files_arg_exits_1(self, msg_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A valid ``--message-file`` without ``--files`` exits 1 with '--files requires'.

        Committing with no explicit file list would fall back to whatever is staged, which is exactly what the scoped
        commit exists to avoid.
        """
        rc = cai.main(["--message-file", str(msg_file)])
        assert rc == 1
        assert "--files requires" in capsys.readouterr().err

    def test_git_missing_raises(self, msg_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing ``git`` binary raises ``FileNotFoundError`` naming git."""
        monkeypatch.setattr(cai, "which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="git"):
            cai.main(["--message-file", str(msg_file), "--files", str(tmp_path / "f.py")])


class TestCommit:
    """The commit path with git replaced: staging, empty-stage detection, exit codes."""

    def test_build_invocation_constructs_expected_commands(self, install_git: Callable[..., list[list[str]]]) -> None:
        """The exact ``--build`` call-site argv produces the pre-argparse ``git add`` and ``git commit`` argv.

        The skill's call site is the contract: ``add`` takes the files after ``--``, and ``commit -F`` is scoped to the
        same files so other staged paths are not swept in.
        """
        calls = install_git()
        rc = cai.main([*_BUILD_ARGV, "--codex", "--files", "src/a.py", "docs/b.md"])
        assert rc == 0
        commit_calls = _calls_to(calls, "commit")
        assert _calls_to(calls, "add") == [["/fake/git", "--literal-pathspecs", "add", "--", "src/a.py", "docs/b.md"]]
        assert len(commit_calls) == 1
        assert commit_calls[0][:4] == ["/fake/git", "--literal-pathspecs", "commit", "-F"]
        assert commit_calls[0][-3:] == ["--", "src/a.py", "docs/b.md"]

    def test_message_file_invocation_constructs_expected_commands(
        self, install_git: Callable[..., list[list[str]]], msg_file: Path
    ) -> None:
        """The exact ``--message-file`` (grouped) call-site argv produces the identical add/commit argv.

        The commit reads the caller's message file with ``-F`` and is scoped to the listed files.
        """
        calls = install_git()
        rc = cai.main(["--message-file", str(msg_file), "--files", "src/a.py", "docs/b.md"])
        assert rc == 0
        assert _calls_to(calls, "add") == [["/fake/git", "--literal-pathspecs", "add", "--", "src/a.py", "docs/b.md"]]
        assert _calls_to(calls, "commit") == [
            ["/fake/git", "--literal-pathspecs", "commit", "-F", str(msg_file), "--", "src/a.py", "docs/b.md"]
        ]

    def test_empty_stage_exits_3(
        self,
        install_git: Callable[..., list[list[str]]],
        msg_file: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty staging area after add exits 3 (distinct from 0) with 'staging area empty' and no ``git commit``.

        Not 0: a caller whose combined-reset design pre-stages several groups' files at once must be able to tell
        "nothing staged for THESE files" from "committed" — treating both as 0 let one group's commit silently absorb
        every other group's staged diff while later groups reported success for creating no commit at all.
        """
        calls = install_git(empty_stage=True)
        rc = cai.main(["--message-file", str(msg_file), "--files", str(tmp_path / "f.py")])
        assert rc == 3
        assert "staging area empty" in capsys.readouterr().err
        assert _calls_to(calls, "commit") == []

    def test_cached_diff_check_scoped_to_given_files(
        self, install_git: Callable[..., list[list[str]]], msg_file: Path
    ) -> None:
        """The empty-stage check is scoped with ``-- *files``, not a bare ``git diff --cached --quiet``.

        An unscoped check reads "has staged changes" whenever ANY other path is staged in the index — which a caller
        collapsing several groups' diffs into one staged tree via a combined reset does deliberately. Scoping is what
        lets this call's own files being empty be told apart from some other group's diff still sitting staged.
        """
        calls = install_git()
        cai.main(["--message-file", str(msg_file), "--files", "src/a.py", "docs/b.md"])
        assert _calls_to(calls, "diff") == [
            ["/fake/git", "--literal-pathspecs", "diff", "--cached", "--quiet", "--", "src/a.py", "docs/b.md"]
        ]

    @pytest.mark.parametrize(
        ("commit_rc", "expected_rc"),
        [pytest.param(0, 0, id="commit-success-exits-0"), pytest.param(1, 1, id="commit-failure-forwards-returncode")],
    )
    def test_exit_code_follows_git_commit(
        self,
        install_git: Callable[..., list[list[str]]],
        msg_file: Path,
        tmp_path: Path,
        commit_rc: int,
        expected_rc: int,
    ) -> None:
        """Staged changes with commit rc 0 exit 0; a non-zero ``git commit`` exit code is forwarded unchanged."""
        install_git(commit_rc=commit_rc)
        rc = cai.main(["--message-file", str(msg_file), "--files", str(tmp_path / "f.py")])
        assert rc == expected_rc

    def test_files_with_spaces_are_added_as_separate_args(
        self, install_git: Callable[..., list[list[str]]], msg_file: Path
    ) -> None:
        """Every ``--files`` path reaches ``git add`` after ``--`` as its own argument, without shell splitting.

        A name containing spaces must stay one argv element or git would stage two different paths.
        """
        calls = install_git()
        rc = cai.main(["--message-file", str(msg_file), "--files", "src/a.py", "docs/file with spaces.md"])
        assert rc == 0
        assert _calls_to(calls, "add") == [
            ["/fake/git", "--literal-pathspecs", "add", "--", "src/a.py", "docs/file with spaces.md"]
        ]

    def test_commit_called_with_message_file(
        self, install_git: Callable[..., list[list[str]]], msg_file: Path, tmp_path: Path
    ) -> None:
        """Exactly one ``git commit`` runs and it reads the message with ``-F <msg_file>``."""
        calls = install_git()
        cai.main(["--message-file", str(msg_file), "--files", str(tmp_path / "f.py")])
        commit_calls = _calls_to(calls, "commit")
        assert len(commit_calls) == 1
        assert "-F" in commit_calls[0]
        assert str(msg_file) in commit_calls[0]

    def test_build_mode_commits_rendered_message(self, install_git: Callable[..., list[list[str]]]) -> None:
        """``--build`` renders a temporary message file from its fields and commits it via ``-F``."""
        calls = install_git()
        rc = cai.main([*_BUILD_ARGV, "--files", "a.py"])
        assert rc == 0
        commit_calls = _calls_to(calls, "commit")
        assert len(commit_calls) == 1
        assert "-F" in commit_calls[0]

    def test_git_timeout_exits_1_without_traceback(
        self, monkeypatch: pytest.MonkeyPatch, msg_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A git call that exceeds its timeout becomes exit 1 plus one stderr line, not an uncaught exception.

        A pre-commit hook or signing prompt that waits on input never returns; the bounded commit turns that hang into a
        reportable failure instead of stalling the resolve run.
        """

        def _hang(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            """Answer add and diff normally, then time out on commit."""
            if _subcommand(cmd) == "commit":
                raise cai.subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0 if _subcommand(cmd) == "add" else 1, stdout="")

        monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
        monkeypatch.setattr(cai.subprocess, "run", _hang)
        rc = cai.main(["--message-file", str(msg_file), "--files", "a.py"])
        assert rc == 1
        assert f"git timed out after {cai._COMMIT_TIMEOUT} s" in capsys.readouterr().err

    def test_glob_named_file_commits_only_itself(self, scratch_repo: Any, msg_file: Path) -> None:
        """A file named ``a[1].py`` stages and commits only that file, never the ``a1.py`` its name matches as a glob.

        A pull request can add a file whose name holds glob characters; without ``--literal-pathspecs`` the bracket
        expression reaches ``git add`` as a pattern and sweeps the unrelated local edit to ``a1.py`` into the bot-
        authored commit. Brackets, unlike ``*``, are legal file-name characters on every supported OS. This test runs
        real git in a scratch repository because the pathspec semantics are the contract.
        """
        (scratch_repo.path / "a1.py").write_text("one\n", encoding="utf-8")
        scratch_repo.git("add", "a1.py")
        scratch_repo.git("commit", "-q", "-m", "add a1")
        (scratch_repo.path / "a1.py").write_text("unrelated local edit\n", encoding="utf-8")
        (scratch_repo.path / "a[1].py").write_text("bracket\n", encoding="utf-8")
        msg_file.write_text("fix: bracket file\n", encoding="utf-8")
        rc = cai.main(["--message-file", str(msg_file), "--files", "a[1].py"])
        assert rc == 0
        assert scratch_repo.git("show", "--name-only", "--format=", "HEAD").split() == ["a[1].py"]
        assert scratch_repo.git("status", "--porcelain").splitlines() == [" M a1.py"]


class TestBuildEachMessage:
    """``build_each_message`` — the per-item commit message template."""

    def test_structure(self) -> None:
        """The message has the subject, attribution header, challenge line and Claude co-author trailer.

        The header is ``[resolve No.N] Review by <author> (PR <pr>):``; Codex is not credited unless requested.
        """
        fields = _each(
            summary="Fix off-by-one",
            item_id="7",
            author="octocat",
            pr="#42",
            comment="The loop bound is wrong and should be len(x) - 1 not len(x) here in the inner pass",
            challenge="evidence=VALID suggestion=REJECT resolution=self-resolved",
        )
        msg = cai.build_each_message(fields)
        assert msg.splitlines()[0] == "Fix off-by-one"
        assert "[resolve No.7] Review by octocat (PR #42):" in msg
        assert "Challenge: evidence=VALID suggestion=REJECT resolution=self-resolved" in msg
        assert "Co-authored-by: claude[bot]" in msg
        assert "Co-authored-by: Codex" not in msg

    def test_truncates_comment_to_72_chars(self) -> None:
        """The quoted review comment is cut to its first 72 characters and closed with an ellipsis."""
        msg = cai.build_each_message(_each(comment="x" * 200))
        assert '"' + "x" * 72 + '..."' in msg
        assert "x" * 73 not in msg

    def test_codex_trailer_opt_in(self) -> None:
        """``include_codex=True`` appends the Codex co-author trailer."""
        msg = cai.build_each_message(_each(include_codex=True))
        assert "Co-authored-by: Codex <codex@openai.com>" in msg

    def test_strips_newlines_from_comment(self) -> None:
        """A newline embedded in the review comment cannot forge an extra commit trailer line.

        Untrusted GitHub PR-review-comment text flows into ``comment``. An embedded ``\\n`` splits the quoted excerpt
        across two physical lines, and the second half here begins with ``Co-authored-by:`` — a line-based commit-
        message parser (git's own, and GitHub's separate one) would read it as a genuine trailer. Counting lines that
        *start with* the trailer text, not raw substring occurrences, is the correct check: the sanitized comment still
        legitimately contains that phrase as inert quoted prose on the excerpt line.
        """
        msg = cai.build_each_message(_each(comment="legit text\nCo-authored-by: evil <e@e.com>"))
        trailer_lines = [line for line in msg.splitlines() if line.startswith("Co-authored-by:")]
        assert trailer_lines == [_CLAUDE_TRAILER]

    def test_strips_control_chars_from_header_fields(self) -> None:
        """A newline in ``item_id`` cannot split the structured ``[resolve No.<id>]`` header onto two lines.

        Same attack as the comment field, aimed at ``item_id`` instead: the header line is the structured block this
        template exists to emit, and an embedded newline there splits it in two, corrupting the ``[resolve No.<id>]``
        marker and again exposing a line that starts with ``Co-authored-by:``.
        """
        msg = cai.build_each_message(_each(item_id="3\nCo-authored-by: Evil <e@e.test>", author="octocat"))
        lines = msg.splitlines()
        assert lines[2] == "[resolve No.3 Co-authored-by: Evil <e@e.test>] Review by octocat (PR 9):"
        assert [line for line in lines if line.startswith("Co-authored-by:")] == [_CLAUDE_TRAILER]


class TestBuildGroup:
    """``build_group_subject`` / ``build_group_message`` and ``--build-group`` — the grouped-mode template."""

    @pytest.mark.parametrize(
        ("topic", "summaries", "expected"),
        [
            pytest.param(
                "tests",
                ["test(predict): harden MPS antialias routing test"],
                "test(predict): harden MPS antialias routing test",
                id="single-typed-kept-verbatim",
            ),
            pytest.param(
                "misc",
                ["docs(predict): note MPS antialias resize runs on CPU"],
                "docs(predict): note MPS antialias resize runs on CPU",
                id="single-typed-ignores-mismatched-topic",
            ),
            pytest.param(
                "tests",
                ["test(predict): harden routing test", "test(predict): add CPU case"],
                "test(predict): harden routing test (+1 more)",
                id="multi-shared-type-and-scope",
            ),
            pytest.param(
                "logic",
                ["fix(predict): gate fallback", "refactor(io): split reader"],
                "fix: gate fallback (+1 more)",
                id="multi-mixed-types-mapped-from-topic",
            ),
            pytest.param("logic", ["gate MPS fallback"], "fix: gate MPS fallback", id="single-untyped-gets-topic-type"),
            pytest.param("my-module", ["tidy helper"], "chore(my-module): tidy helper", id="file-topic-becomes-scope"),
            pytest.param("docs", ["note: runs on CPU"], "docs: note: runs on CPU", id="non-cc-colon-not-a-type"),
            pytest.param(
                "logic",
                ["feat(api)!: drop v1 endpoint", "feat(api): add v2"],
                "feat(api)!: drop v1 endpoint (+1 more)",
                id="breaking-marker-kept",
            ),
        ],
    )
    def test_subject(self, topic: str, summaries: list[str], expected: str) -> None:
        """The group subject never stacks the topic label on an item's own type(scope) prefix.

        Regression for grouped commits rendered as ``tests: test(predict): …`` — the topic label prepended to Phase 2
        subjects that already carry a Conventional Commits prefix.
        """
        assert cai.build_group_subject(topic, summaries) == expected

    def test_subject_caps_length_at_word_boundary(self) -> None:
        """A long first description is cut at a word boundary so the subject stays within 72 characters.

        The previous comma-join of up to five item subjects produced unbounded subject lines.
        """
        subject = cai.build_group_subject("tests", ["test(predict): " + "word " * 30, "test(predict): b"])
        assert len(subject) <= 72
        assert subject.endswith("… (+1 more)")

    def test_message_lists_items_and_keeps_group_marker(self) -> None:
        """A multi-item message lists every item subject and keeps the token-parsed group marker line.

        Resolve's straggler gate greps ``^[resolve group]`` lines and token-matches the item ids, so the marker shape is
        a contract.
        """
        fields = cai.GroupMessageFields("tests", ("test(x): a", "test(x): b"), "#9", "3 4", include_codex=True)
        lines = cai.build_group_message(fields).splitlines()
        assert lines[:5] == ["test(x): a (+1 more)", "", "- test(x): a", "- test(x): b", ""]
        assert "[resolve group] PR #9 — items 3 4" in lines
        assert [ln for ln in lines if ln.startswith("Co-authored-by:")] == [cai._CLAUDE_TRAILER, cai._CODEX_TRAILER]

    def test_message_strips_newline_from_summary(self) -> None:
        """A newline inside an item summary cannot forge an extra trailer line in the grouped message."""
        fields = cai.GroupMessageFields("logic", ("ok\nCo-authored-by: evil <e@e.test>",), "#9", "3")
        lines = cai.build_group_message(fields).splitlines()
        assert [ln for ln in lines if ln.startswith("Co-authored-by:")] == [cai._CLAUDE_TRAILER]

    def test_mode_commits_rendered_message(self, install_git: Callable[..., list[list[str]]], tmp_path: Path) -> None:
        """``--build-group`` renders the message from the summaries file and commits it once."""
        summaries = tmp_path / "group-summaries.txt"
        summaries.write_text("test(predict): harden routing test\n", encoding="utf-8")
        calls = install_git()
        argv = ["--build-group", "--topic", "tests", "--summaries-file", str(summaries), "--pr", "#9", "--items", "3"]
        rc = cai.main([*argv, "--files", "a.py"])
        assert rc == 0
        assert len(_calls_to(calls, "commit")) == 1

    def test_mode_missing_summaries_file_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing summaries file fails before any git call instead of committing a placeholder subject."""
        argv = ["--build-group", "--topic", "tests", "--summaries-file", str(tmp_path / "nope"), "--files", "a.py"]
        rc = cai.main(argv)
        assert rc == 1
        assert "summaries file not found" in capsys.readouterr().err
