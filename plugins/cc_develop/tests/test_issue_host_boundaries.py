"""Keep issue fetch arguments within ASCII IDs and explicitly configured hosts."""

from unittest import mock

import issue_fetch
import pytest


@pytest.mark.parametrize("number", ["²3", "\uff11\uff12", "١٢"])
@mock.patch("issue_fetch.subprocess.run", autospec=True)
@mock.patch("issue_fetch.which", autospec=True, return_value="gh")
def test_non_ascii_issue_is_rejected(lookup: mock.MagicMock, run: mock.MagicMock, number: str) -> None:
    """Unicode digit characters must be refused before locating or starting gh."""
    assert issue_fetch.main([number]) == 1
    lookup.assert_not_called()
    run.assert_not_called()


def test_unconfigured_repo_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo override cannot select an arbitrary host from untrusted issue context."""
    monkeypatch.delenv("GH_HOST", raising=False)
    assert not issue_fetch.is_valid_repo("evil.example.com/o/r")
    assert issue_fetch.is_valid_repo("github.com/o/r")


def test_explicit_enterprise_host_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit gh host configuration preserves enterprise issue fetching."""
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    assert issue_fetch.is_valid_repo("ghe.example.com/o/r")
    assert issue_fetch.is_valid_repo("github.com/o/r")
    assert not issue_fetch.is_valid_repo("elsewhere.example.com/o/r")
