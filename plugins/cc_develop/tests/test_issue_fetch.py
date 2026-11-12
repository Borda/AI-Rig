"""Tests for ``bin/issue_fetch.py``.

The script strips a leading ``#`` from the issue number, validates digits-only, then delegates to ``gh issue view`` with
``--comments``. ``subprocess.run`` is mocked throughout — no actual ``gh`` invocation. ``which`` is mocked to return a
fake path so ``_resolve`` succeeds even when ``gh`` is not installed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from unittest import mock

import issue_fetch  # type: ignore[import-not-found]
import pytest

_GH = "/fake/path/to/gh"


@pytest.fixture
def gh_which() -> Iterator[mock.MagicMock]:
    """Mock executable lookup inside the script so ``gh`` resolves to a fake path."""
    with mock.patch("issue_fetch.which", autospec=True, return_value=_GH) as which:
        yield which


@pytest.fixture
def gh_run(gh_which: mock.MagicMock) -> Iterator[mock.MagicMock]:
    """Mock ``subprocess.run`` inside the script; it reports success unless a test sets ``return_value``."""
    with mock.patch("issue_fetch.subprocess.run", autospec=True) as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        yield run


def _delegated_argv(gh_run: mock.MagicMock) -> list[list[str]]:
    """Return the argv of every delegated ``gh`` call."""
    return [call.args[0] for call in gh_run.call_args_list]


class TestIssueNumber:
    """The issue number argument is normalized, or rejected before ``gh`` ever starts."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("42", "42", id="plain-number"),
            pytest.param("#42", "42", id="hash-prefixed-number"),
            pytest.param("001", "001", id="leading-zeroes"),
            pytest.param(" 42 ", "42", id="surrounding-whitespace"),
            pytest.param("#7 --repo evil/repo --foo bar", "7", id="blob-with-dash-tokens-uses-first-token"),
        ],
    )
    def test_valid_forms_are_normalized(self, gh_run: mock.MagicMock, raw: str, expected: str) -> None:
        """Valid issue number forms are normalized before invoking gh.

        The last case is the raw ``$ARGUMENTS`` blob: only its first token is the number and the embedded ``--repo`` is
        not consumed as an outer flag (the blob is never handed to argparse), so no ``--repo`` reaches gh.
        """
        rc = issue_fetch.main([raw])
        assert (rc, _delegated_argv(gh_run)) == (0, [[_GH, "issue", "view", expected, "--comments"]])

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["abc"], id="letters"),
            pytest.param(["#"], id="bare-hash"),
            pytest.param(["##42"], id="double-hash"),
            pytest.param(["-1"], id="negative"),
            pytest.param(["1.2"], id="decimal"),
            pytest.param(["12abc"], id="digits-then-letters"),
            pytest.param([], id="no-argument"),
        ],
    )
    def test_invalid_number_exits_1(
        self, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """Non-numeric or missing input → exit 1; gh never invoked; stderr carries the diagnostic.

        No argument is treated as an empty string, which is just as invalid as a non-digit one.
        """
        rc = issue_fetch.main(argv)
        assert (rc, gh_run.called, "invalid issue number" in capsys.readouterr().err) == (1, False, True)


class TestRepoOption:
    """``--repo`` is forwarded only when it is a well-formed ``[HOST/]OWNER/REPO``."""

    @pytest.mark.parametrize(
        "repo",
        [
            pytest.param("owner/repo", id="owner-repo"),
            pytest.param("ghe.example.com/owner/repo", id="host-qualified"),
            pytest.param("owner/.github", id="dot-leading-repo"),
        ],
    )
    def test_valid_repo_is_forwarded_unchanged(
        self, gh_run: mock.MagicMock, repo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``[HOST/]OWNER/REPO`` forms ``gh --repo`` understands, including a ``.github`` repo, are forwarded unchanged.

        The plain ``owner/repo`` case is the golden call site ``issue_fetch.py "$ARGUMENTS" --repo owner/repo``.
        """
        monkeypatch.setenv("GH_HOST", "ghe.example.com")
        rc = issue_fetch.main(["42", "--repo", repo])
        assert (rc, _delegated_argv(gh_run)) == (0, [[_GH, "issue", "view", "42", "--comments", "--repo", repo]])

    @pytest.mark.parametrize(
        "repo",
        [
            pytest.param("-owner/repo", id="flag-shaped-owner"),
            pytest.param("owner/-repo", id="flag-shaped-repo"),
            pytest.param("../repo", id="dot-dot-owner"),
            pytest.param("owner", id="single-segment"),
            pytest.param("a/b/c/d", id="too-many-segments"),
            pytest.param("owner/re po", id="whitespace"),
            pytest.param("owner/repo\nx", id="newline"),
        ],
    )
    def test_malformed_repo_exits_1(
        self, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str], repo: str
    ) -> None:
        """A ``--repo`` that is not ``[HOST/]OWNER/REPO`` exits 1 and gh is never started.

        The override can be read from a shared-temp sentinel another local process may have planted, so a malformed
        value is refused instead of being handed to ``gh`` as an argument.
        """
        rc = issue_fetch.main(["42", "--repo", repo])
        assert (rc, gh_run.called, "invalid --repo" in capsys.readouterr().err) == (1, False, True)


class TestDelegation:
    """The wrapper hands the terminal to ``gh`` and reports its status."""

    def test_passes_through_exit_code(self, gh_run: mock.MagicMock) -> None:
        """Preserve a delegated command's exit status.

        Callers branch on the status, so a failing ``gh issue view`` must not be flattened to success.
        """
        gh_run.return_value = subprocess.CompletedProcess(args=[], returncode=3)
        assert issue_fetch.main(["99"]) == 3

    def test_missing_gh_raises(self, gh_which: mock.MagicMock, gh_run: mock.MagicMock) -> None:
        """Propagate ``FileNotFoundError`` when ``gh`` is not on PATH, without starting any subprocess.

        A missing CLI is an install problem; surfacing it as an exception beats a confusing "invalid issue" exit.
        """
        gh_which.return_value = None
        with pytest.raises(FileNotFoundError, match="gh"):
            issue_fetch.main(["1"])
        gh_run.assert_not_called()

    def test_output_streams_are_inherited(self, gh_run: mock.MagicMock) -> None:
        """``subprocess.run`` is called WITHOUT capture, so ``gh`` output reaches the user directly.

        Both stdout and stderr stay inherited from the caller (mirrors bash ``2>&1``).
        """
        issue_fetch.main(["7"])
        kwargs = gh_run.call_args.kwargs
        assert (kwargs.get("capture_output"), kwargs.get("stdout"), kwargs.get("stderr")) in {
            (None, None, None),
            (False, None, None),
        }

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 (argparse) before any gh call.

        ``--help`` needs no ``gh`` lookup, so it works even where the CLI is not installed.
        """
        with pytest.raises(SystemExit) as exc:
            issue_fetch.main(["--help"])
        assert (exc.value.code, "issue_fetch.py" in capsys.readouterr().out) == (0, True)
