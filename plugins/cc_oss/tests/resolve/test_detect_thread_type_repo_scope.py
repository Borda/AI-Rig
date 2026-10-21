"""Repository-scope tests for ``bin/detect_thread_type.py``.

A thread URL names its own repository, so the probe must target that repository rather than the current checkout, and a
number written with non-ASCII digits must not be accepted as a thread number. ``subprocess.run`` is monkeypatched so no
real ``gh`` call happens.
"""

from __future__ import annotations

import json

import detect_thread_type as dtt
import pytest


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the detector."""
        self.returncode = returncode
        self.stdout = stdout


class TestParseNumber:
    """parse_number: only ASCII digits form a thread number."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("١٢٣", id="arabic-indic-digits"),
            pytest.param("²", id="superscript-two"),
            pytest.param("https://github.com/o/r/issues/١٢٣", id="url-with-arabic-indic-digits"),
        ],
    )
    def test_non_ascii_digits_are_rejected(self, raw: str) -> None:
        """Unicode digits are not accepted as a number, since they would reach the ``gh api`` path unchanged."""
        assert dtt.parse_number(raw) is None


class TestParseRepo:
    """parse_repo: the URL's ``owner/repo`` is extracted, and only for a well-formed URL."""

    def test_thread_url_yields_owner_and_repo(self) -> None:
        """A pull-request URL yields its ``owner/repo`` pair."""
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
        """A bare number or a malformed URL leaves the repository to ``gh``'s working-directory lookup."""
        assert dtt.parse_repo(raw) is None


class TestDetectTargetsUrlRepository:
    """Detect: the issues probe addresses the URL's repository when one is given."""

    def test_issue_probe_uses_url_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With ``repo`` set the ``gh api`` path names that repository instead of the placeholders."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
            """Record the command and answer as an issue."""
            calls.append(cmd)
            return _FakeCompleted(stdout=json.dumps({"updated_at": "2024-01-01T00:00:00Z"}))

        monkeypatch.setattr(dtt.subprocess, "run", fake_run)
        dtt.detect("7", None, "gh", 5, repo="owner/other")
        assert calls[0][-1] == "repos/owner/other/issues/7"

    def test_issue_probe_without_repo_keeps_placeholders(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``repo`` the path keeps ``{owner}/{repo}`` so ``gh`` resolves the working directory's repository."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
            """Record the command and answer as an issue."""
            calls.append(cmd)
            return _FakeCompleted(stdout=json.dumps({"updated_at": "2024-01-01T00:00:00Z"}))

        monkeypatch.setattr(dtt.subprocess, "run", fake_run)
        dtt.detect("7", None, "gh", 5)
        assert calls[0][-1] == "repos/{owner}/{repo}/issues/7"

    def test_discussion_probe_uses_url_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the issues probe finds nothing, the GraphQL fallback passes the URL's owner and repo."""
        calls: list[list[str]] = []
        discussion = {"data": {"repository": {"discussion": {"title": "t", "updatedAt": "2024-01-01T00:00:00Z"}}}}
        replies = iter([_FakeCompleted(returncode=1), _FakeCompleted(stdout=json.dumps(discussion))])

        def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
            """Record the command and return the next canned reply."""
            calls.append(cmd)
            return next(replies)

        monkeypatch.setattr(dtt.subprocess, "run", fake_run)
        dtt.detect("3", None, "gh", 5, repo="owner/other")
        assert ["owner=owner", "repo=other"] == [a for a in calls[1] if a.startswith(("owner=", "repo="))]
