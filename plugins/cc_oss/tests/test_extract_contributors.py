"""Tests for ``bin/extract_contributors.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. ``is_bot``,
``dedupe_by_email``, and ``_build_range`` are tested directly as pure functions; arg validation and the ``git log`` path
are covered via ``main(argv)`` calls.
"""

from __future__ import annotations

from typing import Any

import pytest

import extract_contributors as ec


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Store the status and streams returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>", True, id="bot-login-privacy-domain"
        ),
        pytest.param(
            "pre-commit-ci[bot] <66853113+pre-commit-ci[bot]@users.noreply.github.com>",
            True,
            id="bot-login-privacy-domain-ci",
        ),
        pytest.param("CI <ci@noreply.example.com>", True, id="noreply-email-generic"),
        pytest.param("Jane Doe <jane@example.com>", False, id="human-plain"),
        pytest.param("Jirka Borovec <6035284+Borda@users.noreply.github.com>", False, id="human-privacy-email"),
        pytest.param("Alice <123+alice@users.noreply.github.com>", False, id="human-web-ui-commit"),
    ],
)
def test_is_bot(line: str, expected: bool) -> None:
    """Flag ``[bot]`` logins and generic noreply; keeps GitHub privacy-email humans."""
    assert ec.is_bot(line) is expected


def test_dedupe_by_email_keeps_first_name_drops_bots_sorted() -> None:
    """Dedup by email, drop bots, keep first display name, sort case-folded."""
    result = ec.dedupe_by_email(
        [
            "Jane Doe <jane@example.com>",
            "",
            "J. Doe <jane@example.com>",
            "bot[bot] <bot@noreply.github.com>",
            "Al Pace <al@example.com>",
        ]
    )
    assert result == ["Al Pace <al@example.com>", "Jane Doe <jane@example.com>"]


def test_dedupe_by_email_can_retain_bots_for_release_credits() -> None:
    """Release mode retains bot identities so prose can aggregate their credits."""
    result = ec.dedupe_by_email(
        [
            "Jane Doe <jane@example.com>",
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
            "Jirka Borovec <6035284+Borda@users.noreply.github.com>",
        ],
        include_bots=True,
    )
    assert result == [
        "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
        "Jane Doe <jane@example.com>",
        "Jirka Borovec <6035284+Borda@users.noreply.github.com>",
    ]


@pytest.mark.parametrize(
    ("range_arg", "from_ref", "to_ref", "expected"),
    [
        pytest.param("v1..v2", "", "", "v1..v2", id="explicit-range"),
        pytest.param("", "v1", "v2", "v1..v2", id="from-to"),
        pytest.param("", "v1", "", "v1..HEAD", id="from-defaults-head"),
        pytest.param("", "", "", "", id="none"),
    ],
)
def test_build_range(range_arg: str, from_ref: str, to_ref: str, expected: str) -> None:
    """Resolve ``--range`` / ``--from`` / ``--to`` into a git range."""
    assert ec._build_range(range_arg, from_ref, to_ref) == expected


# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------


def test_no_range_arg_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """No range given → exit 1 with '--range or --from required'."""
    rc = ec.main([])
    assert rc == 1
    assert "--range or --from required" in capsys.readouterr().err


def test_range_and_from_conflict_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Both ``--range`` and ``--from`` → exit 1 with conflict message."""
    rc = ec.main(["--range", "v1..v2", "--from", "v1"])
    assert rc == 1
    assert "not both" in capsys.readouterr().err


def test_unknown_arg_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Unrecognized flag → exit 1 with 'unknown arg'."""
    rc = ec.main(["--bogus", "x"])
    assert rc == 1
    assert "unknown arg" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# git log path (subprocess mocked)
# ---------------------------------------------------------------------------


def test_emits_deduped_bot_free_list(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Successful git log → sorted, bot-free, email-deduped stdout list."""
    stdout = (
        "Jane Doe <jane@example.com>\nJ. Doe <jane@example.com>\nbot[bot] <b@noreply.github.com>\nAl <al@example.com>\n"
    )
    monkeypatch.setattr(ec, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ec.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=0, stdout=stdout))
    rc = ec.main(["--range", "v1..v2"])
    assert rc == 0
    assert capsys.readouterr().out == "Al <al@example.com>\nJane Doe <jane@example.com>\n"


def test_include_bots_emits_bot_and_privacy_email_human(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Release extraction keeps bot credits while retaining GitHub privacy-email humans."""
    stdout = (
        "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>\n"
        "Jirka Borovec <6035284+Borda@users.noreply.github.com>\n"
    )
    monkeypatch.setattr(ec, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ec.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=0, stdout=stdout))

    rc = ec.main(["--range", "v1..v2", "--include-bots"])

    assert rc == 0
    assert capsys.readouterr().out == stdout


def test_git_failure_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Git log non-zero exit → exit 2 with stderr surfaced."""
    monkeypatch.setattr(ec, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ec.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=128, stderr="bad revision"))
    rc = ec.main(["--range", "v1..v2"])
    assert rc == 2
    assert "bad revision" in capsys.readouterr().err


def test_repo_flag_inserts_git_c(monkeypatch: pytest.MonkeyPatch) -> None:
    """Insert ``-C <root>`` into the git command."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record the contributor extraction command and return no contributors."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(ec, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ec.subprocess, "run", _fake_run)
    ec.main(["--repo", "/repo/x", "--range", "v1..v2"])
    assert recorded[0][1:3] == ["-C", "/repo/x"]


def test_git_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None for git → FileNotFoundError raised."""
    monkeypatch.setattr(ec, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="git"):
        ec.main(["--range", "v1..v2"])
