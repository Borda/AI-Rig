"""Tests for ``bin/commit_action_item.py``.

``subprocess.run`` and ``which`` monkeypatched — no real git invocations. Tests cover argument validation, the early
return for an empty stage, sentinel lifecycle, the successful commit path, and the pure ``_slug`` function.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

import commit_action_item as cai


# ---------------------------------------------------------------------------
# --help + argparse migration (argv → variable mapping only; git logic untouched)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_0_without_git(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Print help without invoking Git or another subprocess."""
    called: list[Any] = []
    monkeypatch.setattr(cai.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit) as exc:
        cai.main([flag])
    assert exc.value.code == 0
    assert called == []


def test_golden_build_invocation_constructs_expected_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact ``--build`` call-site argv → git add/commit argv identical to pre-argparse baseline."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(calls=calls))
    rc = cai.main(
        [
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
            "--codex",
            "--files",
            "src/a.py",
            "docs/b.md",
        ]
    )
    assert rc == 0
    add_calls = [c for c in calls if len(c) > 1 and c[1] == "add"]
    commit_calls = [c for c in calls if len(c) > 1 and c[1] == "commit"]
    assert add_calls == [["/fake/git", "add", "--", "src/a.py", "docs/b.md"]]
    assert len(commit_calls) == 1
    assert commit_calls[0][:3] == ["/fake/git", "commit", "-F"]


def test_golden_message_file_invocation_constructs_expected_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exact ``--message-file`` (grouped) call-site argv → identical git add/commit argv."""
    msg = tmp_path / "COMMIT_MSG"
    msg.write_text("style: combined summary\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(calls=calls))
    rc = cai.main(["--message-file", str(msg), "--files", "src/a.py", "docs/b.md"])
    assert rc == 0
    add_calls = [c for c in calls if len(c) > 1 and c[1] == "add"]
    commit_calls = [c for c in calls if len(c) > 1 and c[1] == "commit"]
    assert add_calls == [["/fake/git", "add", "--", "src/a.py", "docs/b.md"]]
    assert commit_calls == [["/fake/git", "commit", "-F", str(msg)]]


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def test_missing_message_file_arg_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No args → exit 1 with '--message-file required' on stderr."""
    rc = cai.main([])
    assert rc == 1
    assert "--message-file required" in capsys.readouterr().err


def test_message_file_not_found_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Reject a message-file path that does not exist."""
    rc = cai.main(["--message-file", str(tmp_path / "nonexistent.txt")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_missing_files_arg_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Valid ``--message-file`` but no ``--files`` → exit 1 '--files requires'."""
    msg = tmp_path / "msg.txt"
    msg.write_text("commit msg\n")
    rc = cai.main(["--message-file", str(msg)])
    assert rc == 1
    assert "--files requires" in capsys.readouterr().err


def test_unknown_arg_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Unrecognized flag → exit 1 with 'unknown arg' on stderr."""
    rc = cai.main(["--unknown"])
    assert rc == 1
    assert "unknown arg" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Commit path (subprocess mocked)
# ---------------------------------------------------------------------------


def _make_git_mock(
    *,
    empty_stage: bool = False,
    commit_rc: int = 0,
    calls: list[list[str]] | None = None,
) -> Any:
    """Return a fake ``subprocess.run`` for git operations."""

    def _fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Return deterministic Git responses while recording optional calls."""
        if calls is not None:
            calls.append(list(cmd))
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if binary == "git" and subcmd == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="/repo/my-project\n")
        if binary == "git" and subcmd == "branch":
            return _FakeCompleted(returncode=0, stdout="main\n")
        if binary == "git" and subcmd == "add":
            return _FakeCompleted(returncode=0)
        if binary == "git" and subcmd == "diff":
            # returncode 0 = no diff = empty stage; non-zero = has staged changes
            return _FakeCompleted(returncode=0 if empty_stage else 1)
        if binary == "git" and subcmd == "commit":
            return _FakeCompleted(returncode=commit_rc)
        return _FakeCompleted(returncode=0)

    return _fake_run


def test_empty_stage_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Staging area empty after add → exit 0, 'staging area empty' in stderr."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(empty_stage=True))
    rc = cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])
    assert rc == 0
    assert "staging area empty" in capsys.readouterr().err


def test_empty_stage_no_commit_called(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty staging area → git commit never invoked."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(empty_stage=True, calls=calls))
    cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])
    commit_calls = [c for c in calls if "commit" in c]
    assert not commit_calls


def test_successful_commit_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Staged changes + commit rc 0 → exit 0."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock())
    rc = cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])
    assert rc == 0


def test_commit_failure_forwards_returncode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Git commit exits non-zero → that exit code forwarded."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(commit_rc=1))
    rc = cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])
    assert rc == 1


def test_multiple_files_with_spaces_are_added_as_separate_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every --files path is passed to git add after -- without shell splitting."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(calls=calls))
    rc = cai.main(["--message-file", str(msg), "--files", "src/a.py", "docs/file with spaces.md"])
    assert rc == 0
    add_calls = [c for c in calls if c[1] == "add"]
    assert add_calls == [["/fake/git", "add", "--", "src/a.py", "docs/file with spaces.md"]]


@pytest.mark.parametrize("commit_rc", [0, 1])
def test_sentinel_cleaned_up_after_commit_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    commit_rc: int,
) -> None:
    """Sentinel exists for git commit but is removed before main returns on success and failure."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    fake_tmpdir = tmp_path / "tmp"
    fake_tmpdir.mkdir()
    sentinel_seen: list[bool] = []

    def _fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Emulate Git while recording whether the sentinel existed at commit time."""
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if binary == "git" and subcmd == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="/repo/my-project\n")
        if binary == "git" and subcmd == "branch":
            return _FakeCompleted(returncode=0, stdout="main\n")
        if binary == "git" and subcmd == "diff":
            return _FakeCompleted(returncode=1)
        if binary == "git" and subcmd == "commit":
            sentinel_seen.append(any(f.name.startswith("claude-commit-auth-") for f in fake_tmpdir.iterdir()))
            return _FakeCompleted(returncode=commit_rc)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _fake_run)
    monkeypatch.setenv("TMPDIR", str(fake_tmpdir))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmpdir))

    rc = cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])

    assert rc == commit_rc
    assert sentinel_seen == [True]
    assert list(fake_tmpdir.iterdir()) == []


def test_commit_called_with_message_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Commit invoked with ``-F <msg_file>``."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    calls: list[list[str]] = []
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(calls=calls))
    cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])
    commit_calls = [c for c in calls if "commit" in c]
    assert len(commit_calls) == 1
    assert "-F" in commit_calls[0]
    assert str(msg) in commit_calls[0]


def test_sentinel_created_before_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Sentinel file exists at the time git commit is called."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    sentinel_seen: list[bool] = []
    fake_tmpdir = tmp_path / "tmp"
    fake_tmpdir.mkdir()

    def _fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Emulate Git and observe the sentinel before the commit call."""
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if binary == "git" and subcmd == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="/repo/my-project\n")
        if binary == "git" and subcmd == "branch":
            return _FakeCompleted(returncode=0, stdout="main\n")
        if binary == "git" and subcmd == "commit":
            # Check sentinel files in fake tmpdir
            sentinel_seen.append(any(f.name.startswith("claude-commit-auth-") for f in fake_tmpdir.iterdir()))
        return _FakeCompleted(returncode=0 if subcmd != "diff" else 1)

    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _fake_run)
    # Commit_action_item now prefers TMPDIR / XDG_RUNTIME_DIR over
    # tempfile.gettempdir() — set TMPDIR so the sentinel lands in our fake dir.
    monkeypatch.setenv("TMPDIR", str(fake_tmpdir))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_tmpdir))
    cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])
    assert sentinel_seen == [True]


def test_git_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None for git → FileNotFoundError raised."""
    msg = tmp_path / "msg.txt"
    msg.write_text("msg\n")
    monkeypatch.setattr(cai, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="git"):
        cai.main(["--message-file", str(msg), "--files", str(tmp_path / "f.py")])


# ---------------------------------------------------------------------------
# _slug — pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("main", "main", id="main"),
        pytest.param("My/Repo Name", "my-repo-name", id="my-repo-name"),
        pytest.param("feature/add-thing!", "feature-add-thing", id="feature-add-thing"),
        pytest.param("UPPER-CASE", "upper-case", id="upper-case"),
        pytest.param("trailing-", "trailing", id="trailing"),
        pytest.param("multi---dashes", "multi-dashes", id="multi---dashes"),
    ],
)
def test_slug(text: str, expected: str) -> None:
    """Lowercase, replaces non-alnum runs, strips trailing hyphens."""
    assert cai._slug(text) == expected


# ---------------------------------------------------------------------------
# build_each_message — pure function (each-mode template)
# ---------------------------------------------------------------------------


def test_build_each_message_structure() -> None:
    """Build each-item messages with all required sections and attribution."""
    fields = cai.EachMessageFields(
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
    assert "Co-authored-by: OpenAI Codex" not in msg


def test_build_each_message_truncates_comment_to_72_chars() -> None:
    """Comment body is truncated to the first 72 chars."""
    long_comment = "x" * 200
    msg = cai.build_each_message(cai.EachMessageFields("s", "1", "a", "9", long_comment, "evidence=VALID"))
    assert '"' + "x" * 72 + '..."' in msg
    assert "x" * 73 not in msg


def test_build_each_message_codex_trailer_opt_in() -> None:
    """Append the Codex co-author trailer."""
    msg = cai.build_each_message(cai.EachMessageFields("s", "1", "a", "9", "c", "evidence=VALID", include_codex=True))
    assert "Co-authored-by: OpenAI Codex <codex@openai.com>" in msg


def test_build_mode_and_message_file_conflict_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Reject conflicting build and message-file options."""
    rc = cai.main(["--build", "--message-file", "m.txt", "--files", "a.py"])
    assert rc == 1
    assert "not both" in capsys.readouterr().err


def test_build_mode_commits_rendered_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render a temp message file and commits it via ``-F``."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cai, "which", lambda _: "/fake/git")
    monkeypatch.setattr(cai.subprocess, "run", _make_git_mock(calls=calls))
    rc = cai.main(
        [
            "--build",
            "--summary",
            "Fix typo",
            "--item-id",
            "3",
            "--author",
            "octocat",
            "--pr",
            "42",
            "--comment",
            "fix the typo",
            "--challenge",
            "evidence=VALID",
            "--files",
            "a.py",
        ]
    )
    assert rc == 0
    commit_calls = [c for c in calls if "commit" in c]
    assert len(commit_calls) == 1
    assert "-F" in commit_calls[0]
