"""Tests for ``bin/search_downstream_consumers.py``.

The script queries GitHub code search via ``gh`` for Python imports of changed symbols. ``subprocess.run`` and ``which``
are patched throughout, so no actual ``gh`` invocation happens; symbol input from stdin is driven by replacing
``sys.stdin``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from unittest import mock

import pytest
import search_downstream_consumers as sdc  # type: ignore[import-not-found]
from _process import done


class _EndlessLines:
    """Stdin stand-in yielding an endless stream of symbol lines while counting how many were taken."""

    def __init__(self) -> None:
        """Start with no line consumed."""
        self.taken = 0

    def __iter__(self) -> _EndlessLines:
        """Return the stream itself as its own iterator."""
        return self

    def __next__(self) -> str:
        """Yield one more symbol line and count it."""
        self.taken += 1
        return f"Sym{self.taken}\n"

    def isatty(self) -> bool:
        """Report a pipe, so the symbols are read from stdin."""
        return False


@pytest.fixture
def gh_run() -> Iterator[mock.MagicMock]:
    """Patch ``which`` to find ``gh`` and ``subprocess.run`` to a mock answering success with no output."""
    with (
        mock.patch.object(sdc, "which", autospec=True, return_value="/fake/gh"),
        mock.patch.object(sdc.subprocess, "run", autospec=True, return_value=done()) as run,
    ):
        yield run


class TestRefusals:
    """Main: invalid requests exit 1 before spending any rate-limited code-search call."""

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            pytest.param(
                ["--package", "pkg", *[f"Sym{i}" for i in range(sdc._MAX_SYMBOLS + 1)]],
                "more than",
                id="symbols-above-cap",
            ),
            pytest.param(["SomeSymbol"], "--package required", id="missing-package"),
            pytest.param(["--package", "mylib"], "no symbols provided", id="no-symbols-on-argv-or-stdin"),
        ],
    )
    def test_exits_1_without_any_search(
        self,
        gh_run: mock.MagicMock,
        feed_stdin: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
        message: str,
    ) -> None:
        """More symbols than the cap, no ``--package`` or no symbols at all → exit 1, stderr message, no ``gh`` call.

        Each symbol costs one rate-limited code-search call, and truncating an over-long list would silently drop
        consumers from the union, so the run is refused instead.
        """
        feed_stdin("")
        rc = sdc.main(argv)
        assert (rc, message in capsys.readouterr().err, gh_run.call_count) == (1, True, 0)

    def test_endless_stdin_stops_reading_at_the_cap(
        self, monkeypatch: pytest.MonkeyPatch, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A stdin stream longer than the cap is cut off at cap + 1 lines instead of being drained."""
        stream = _EndlessLines()
        monkeypatch.setattr(sys, "stdin", stream)
        rc = sdc.main(["--package", "pkg"])
        assert (rc, stream.taken, gh_run.call_count) == (1, sdc._MAX_SYMBOLS + 1, 0)

    @mock.patch("search_downstream_consumers.which", autospec=True, return_value=None)
    def test_missing_gh_binary_raises(self, which: mock.MagicMock) -> None:
        """No ``gh`` on PATH → ``FileNotFoundError`` naming it propagates instead of a silent empty result."""
        with pytest.raises(FileNotFoundError, match="gh"):
            sdc.main(["--package", "pkg", "Sym"])


class TestSearch:
    """Main: query code search once per symbol and print the union of consuming repositories."""

    def test_prints_sorted_unique_repos(self, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        """Successful gh queries → one search per symbol, then sorted deduplicated repo names, exit 0."""
        gh_run.return_value = done(stdout="owner/repo-b\nowner/repo-a\n")
        rc = sdc.main(["--package", "mylib", "SymA", "SymB"])
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert (rc, gh_run.call_count, lines) == (0, 2, ["owner/repo-a", "owner/repo-b"])

    def test_prints_shared_repo_once(self, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        """The same repo returned by several symbol queries is printed once."""
        gh_run.return_value = done(stdout="org/shared-repo\n")
        rc = sdc.main(["--package", "pkg", "SymA", "SymB", "SymC"])
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert (rc, lines.count("org/shared-repo")) == (0, 1)

    def test_all_queries_failing_exits_2(self, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        """All gh queries return non-zero → exit 2, so an outage is never read as 'no consumers'."""
        gh_run.return_value = done(returncode=1)
        rc = sdc.main(["--package", "pkg", "Sym"])
        assert (rc, "all symbol queries failed" in capsys.readouterr().err) == (2, True)

    def test_partial_failure_still_exits_0(self, gh_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        """First symbol fails, second succeeds → exit 0 with the repositories from the second."""
        gh_run.side_effect = [done(returncode=1), done(stdout="org/repo\n")]
        rc = sdc.main(["--package", "pkg", "Sym1", "Sym2"])
        assert (rc, "org/repo" in capsys.readouterr().out) == (0, True)

    def test_reads_symbols_from_stdin_when_none_on_argv(
        self, gh_run: mock.MagicMock, feed_stdin: Callable[[str], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Symbols piped on stdin are searched when none are given on the command line."""
        gh_run.return_value = done(stdout="org/repo\n")
        feed_stdin("SymFromStdin\n")
        rc = sdc.main(["--package", "pkg"])
        assert (rc, "org/repo" in capsys.readouterr().out, gh_run.call_count) == (0, True, 1)
