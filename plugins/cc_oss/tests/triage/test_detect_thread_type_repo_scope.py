"""Repository-scope tests for ``bin/detect_thread_type.py``.

A thread URL names its own repository, so the probe must target that repository rather than the current checkout, and a
number written with non-ASCII digits must not be accepted as a thread number. ``subprocess.run`` is replaced so no real
``gh`` call happens.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import detect_thread_type as dtt
import pytest

_ISSUE = json.dumps({"updated_at": "2024-01-01T00:00:00Z"})


@pytest.fixture
def script_gh(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Return a factory that scripts successive ``gh`` replies and yields the list of recorded argv lists."""

    def install(*replies: subprocess.CompletedProcess[str]) -> list[list[str]]:
        """Answer each ``gh`` call with the next reply in order, recording its argv."""
        calls: list[list[str]] = []
        queue = iter(replies)

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Record the command and return the next scripted reply."""
            calls.append(cmd)
            return next(queue)

        monkeypatch.setattr(dtt.subprocess, "run", fake_run)
        return calls

    return install


class TestParseNumber:
    """``parse_number``: only ASCII digits form a thread number."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("١٢٣", id="arabic-indic-digits"),
            pytest.param("²", id="superscript-two"),
            pytest.param("https://github.com/o/r/issues/١٢٣", id="url-with-arabic-indic-digits"),
        ],
    )
    def test_non_ascii_digits_are_rejected(self, raw: str) -> None:
        """Unicode digits are not accepted as a number, since they would reach the ``gh api`` path unchanged.

        Each of Arabic-Indic digits, a superscript, and a URL ending in Arabic-Indic digits satisfies ``str.isdigit``
        yet must not be treated as a thread number.
        """
        assert dtt.parse_number(raw) is None


class TestParseRepo:
    """``parse_repo``: the URL's ``owner/repo`` is extracted, and only for a well-formed URL."""

    def test_thread_url_yields_owner_and_repo(self) -> None:
        """A pull-request URL yields its ``owner/repo`` pair.

        The pair later addresses the ``gh api`` probe so a URL for another repository is not looked up locally.
        """
        assert dtt.parse_repo("https://github.com/o/r/pull/9") == "o/r"

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("123", id="bare-number"),
            pytest.param("#7", id="hash-number"),
            pytest.param("https://github.com/../r/issues/7", id="dot-dot-owner"),
            pytest.param("https://github.com/o/r/wiki/Home", id="not-a-thread-url"),
        ],
    )
    def test_no_repo_for_non_url_or_malformed_url(self, raw: str) -> None:
        """A bare number or a malformed URL leaves the repository to ``gh``'s working-directory lookup.

        A ``..`` owner segment in particular must never become part of an API path.
        """
        assert dtt.parse_repo(raw) is None


class TestDetectTargetsUrlRepository:
    """``detect``: the probes address the URL's repository when one is given."""

    @pytest.mark.parametrize(
        ("kwargs", "expected_path"),
        [
            pytest.param({"repo": "owner/other"}, "repos/owner/other/issues/7", id="url-repo"),
            pytest.param({}, "repos/{owner}/{repo}/issues/7", id="no-repo-keeps-placeholders"),
        ],
    )
    def test_issue_probe_path(
        self, script_gh: Callable[..., list[list[str]]], kwargs: dict[str, str], expected_path: str
    ) -> None:
        """The ``gh api`` issues path names the URL's repository, or keeps ``{owner}/{repo}`` placeholders without one.

        The placeholders make ``gh`` resolve the working directory's repository, which is the right default for a bare
        thread number.
        """
        calls = script_gh(subprocess.CompletedProcess([], 0, stdout=_ISSUE))
        dtt.detect("7", None, "gh", 5, **kwargs)
        assert calls[0][-1] == expected_path

    def test_discussion_probe_uses_url_repo(self, script_gh: Callable[..., list[list[str]]]) -> None:
        """When the issues probe finds nothing, the GraphQL fallback passes the URL's owner and repo.

        The first reply fails (no issue or PR), the second is a discussion; the second argv must carry the URL's
        ``owner=`` and ``repo=`` fields.
        """
        discussion = {"data": {"repository": {"discussion": {"title": "t", "updatedAt": "2024-01-01T00:00:00Z"}}}}
        calls = script_gh(
            subprocess.CompletedProcess([], 1, stdout=""),
            subprocess.CompletedProcess([], 0, stdout=json.dumps(discussion)),
        )
        dtt.detect("3", None, "gh", 5, repo="owner/other")
        assert [a for a in calls[1] if a.startswith(("owner=", "repo="))] == ["owner=owner", "repo=other"]
