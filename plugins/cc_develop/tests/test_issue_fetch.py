"""Tests for ``bin/issue_fetch.py``.

The script strips a leading ``#`` from the issue number, validates digits-only, then delegates to ``gh issue view`` with
``--comments``. ``subprocess.run`` is monkeypatched throughout — no actual ``gh`` invocation. ``shutil.which`` is
patched to return a fake path so ``_resolve`` succeeds even when ``gh`` is not installed.
"""

from __future__ import annotations

from typing import Any

import pytest

import issue_fetch  # type: ignore[import-not-found]  # noqa: E402


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0) -> None:
        """Store the subprocess status exposed to the wrapper under test."""
        self.returncode = returncode


@pytest.fixture(name="captured_argv")
def _captured_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch ``subprocess.run`` and ``shutil.which`` inside the script; record argv lists."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record delegated GitHub CLI argv and return success."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(issue_fetch.subprocess, "run", _fake_run)
    monkeypatch.setattr(issue_fetch, "which", lambda _name: "/fake/path/to/gh")
    return recorded


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param("42", "42", id="plain-number"),
        pytest.param("#42", "42", id="hash-prefixed-number"),
        pytest.param("001", "001", id="leading-zeroes"),
        pytest.param(" 42 ", "42", id="surrounding-whitespace"),
    ],
)
def test_valid_issue_numbers(captured_argv: list[list[str]], raw: str, expected: str) -> None:
    """Valid issue number forms are normalized before invoking gh."""
    rc = issue_fetch.main([raw])
    assert rc == 0
    assert len(captured_argv) == 1
    assert captured_argv[0] == ["/fake/path/to/gh", "issue", "view", expected, "--comments"]


@pytest.mark.parametrize("raw", ["abc", "#", "##42", "-1", "1.2", "12abc"])
def test_rejects_invalid_issue_numbers(
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
    raw: str,
) -> None:
    """Non-numeric input → exit 1; gh never invoked; stderr contains diagnostic."""
    rc = issue_fetch.main([raw])
    assert rc == 1
    assert captured_argv == []  # gh never called
    err = capsys.readouterr().err
    assert "invalid issue number" in err


def test_rejects_empty_input(
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No argument → empty string → invalid; gh not invoked."""
    rc = issue_fetch.main([])
    assert rc == 1
    assert captured_argv == []
    assert "invalid issue number" in capsys.readouterr().err


def test_passes_through_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve a delegated command's exit status."""

    def _fake_run(_cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return the delegated command's failure status without running it."""
        return _FakeCompleted(returncode=3)

    monkeypatch.setattr(issue_fetch.subprocess, "run", _fake_run)
    monkeypatch.setattr(issue_fetch, "which", lambda _name: "/fake/gh")
    assert issue_fetch.main(["99"]) == 3


def test_resolve_raises_when_gh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Propagate ``FileNotFoundError`` when ``gh`` not on PATH."""

    def _no_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:  # pragma: no cover
        """Fail if command execution occurs after the executable lookup fails."""
        raise AssertionError("subprocess.run should not be called when gh is missing")

    monkeypatch.setattr(issue_fetch.subprocess, "run", _no_run)
    monkeypatch.setattr(issue_fetch, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="gh"):
        issue_fetch.main(["1"])


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage and exit 0 (argparse) before any gh call."""
    with pytest.raises(SystemExit) as exc:
        issue_fetch.main(["--help"])
    assert exc.value.code == 0
    assert "issue_fetch.py" in capsys.readouterr().out


def test_golden_invocation_with_repo(captured_argv: list[list[str]]) -> None:
    """Golden call site ``issue_fetch.py "$ARGUMENTS" --repo owner/repo`` forwards both."""
    rc = issue_fetch.main(["42", "--repo", "owner/repo"])
    assert rc == 0
    assert captured_argv[0] == ["/fake/path/to/gh", "issue", "view", "42", "--comments", "--repo", "owner/repo"]


def test_blob_with_dash_tokens_reaches_extraction_unmangled(captured_argv: list[list[str]]) -> None:
    """A blob whose first token is the issue number but which also carries ``--``-shaped tokens still yields the correct
    issue number — the blob is never handed to argparse."""
    rc = issue_fetch.main(["#7 --repo evil/repo --foo bar"])
    assert rc == 0
    # Only the first token (#7 → 7) is used; embedded --repo inside the blob is ignored,
    # not consumed as an outer flag, so no --repo reaches gh.
    assert captured_argv[0] == ["/fake/path/to/gh", "issue", "view", "7", "--comments"]


def test_passthrough_no_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: ``subprocess.run`` is called WITHOUT ``capture_output=True``.

    Both stdout and stderr must remain inherited from the caller so the user sees
    ``gh``'s output directly (mirrors bash ``2>&1``).
    """
    recorded_kwargs: dict[str, Any] = {}

    def _fake_run(_cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        """Capture subprocess keyword arguments while returning success."""
        recorded_kwargs.update(kwargs)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(issue_fetch.subprocess, "run", _fake_run)
    monkeypatch.setattr(issue_fetch, "which", lambda _name: "/fake/gh")
    issue_fetch.main(["7"])
    # No capture flags — stdout/stderr inherit by default.
    assert recorded_kwargs.get("capture_output") in (None, False)
    assert "stdout" not in recorded_kwargs or recorded_kwargs["stdout"] is None
    assert "stderr" not in recorded_kwargs or recorded_kwargs["stderr"] is None
