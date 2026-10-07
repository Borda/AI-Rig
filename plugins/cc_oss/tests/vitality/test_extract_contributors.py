"""Tests for ``bin/extract_contributors.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. ``is_bot``,
``dedupe_by_email``, and ``_build_range`` are tested directly as pure functions; arg validation and the ``git log`` path
are covered via ``main(argv)`` calls.
"""

from __future__ import annotations

from typing import Any

import extract_contributors as ec
import pytest


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


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param([], "--range or --from required", id="no-range-given"),
        pytest.param(["--range", "v1..v2", "--from", "v1"], "not both", id="range-and-from-conflict"),
        pytest.param(["--bogus", "x"], "unknown arg", id="unrecognized-flag"),
    ],
)
def test_invalid_args_exit_1_with_stderr_message(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    """No range, both ``--range`` and ``--from``, or an unrecognized flag → exit 1 with the matching message."""
    rc = ec.main(argv)
    assert rc == 1
    assert message in capsys.readouterr().err


# ---------------------------------------------------------------------------
# git log path (subprocess mocked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra_args", "stdout", "expected"),
    [
        pytest.param(
            [],
            "Jane Doe <jane@example.com>\nJ. Doe <jane@example.com>\nbot[bot] <b@noreply.github.com>\n"
            "Al <al@example.com>\n",
            "Al <al@example.com>\nJane Doe <jane@example.com>\n",
            id="sorted-bot-free-email-deduped",
        ),
        pytest.param(
            ["--include-bots"],
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>\n"
            "Jirka Borovec <6035284+Borda@users.noreply.github.com>\n",
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>\n"
            "Jirka Borovec <6035284+Borda@users.noreply.github.com>\n",
            id="include-bots-keeps-bot-and-privacy-email-human",
        ),
    ],
)
def test_emits_contributor_list_from_git_log(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
    stdout: str,
    expected: str,
) -> None:
    """A successful git log → sorted, email-deduped stdout list, bot-free unless ``--include-bots`` is given.

    Release extraction with ``--include-bots`` keeps bot credits while retaining GitHub privacy-email humans.
    """
    monkeypatch.setattr(ec, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ec.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=0, stdout=stdout))
    rc = ec.main(["--range", "v1..v2", *extra_args])
    assert rc == 0
    assert capsys.readouterr().out == expected


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
