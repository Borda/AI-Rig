"""Tests for ``bin/search_downstream_consumers.py``.

The script queries GitHub code search via ``gh`` for Python imports of changed symbols. ``subprocess.run`` and
``shutil.which`` are monkeypatched throughout — no actual ``gh`` invocation. Stdin-based symbol input tested via
monkeypatching ``sys.stdin``.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
import search_downstream_consumers as sdc  # type: ignore[import-not-found]


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake GitHub CLI command."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture(name="fake_gh")
def _fake_gh(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch subprocess.run and which; record command lists.

    Default: succeeds with empty output.
    """
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record a successful empty code-search response."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(sdc.subprocess, "run", _fake_run)
    monkeypatch.setattr(sdc, "which", lambda _: "/fake/gh")
    return recorded


def test_symbol_count_above_cap_is_refused_before_any_search(
    fake_gh: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """More symbols than the cap → exit 1 with no ``gh`` call.

    Each symbol costs one rate-limited code-search call, and truncating the list would silently drop consumers from the
    union, so the run is refused instead.
    """
    symbols = [f"Sym{i}" for i in range(sdc._MAX_SYMBOLS + 1)]
    rc = sdc.main(["--package", "pkg", *symbols])
    assert rc == 1
    assert "more than" in capsys.readouterr().err
    assert fake_gh == []


def test_endless_stdin_stops_reading_at_the_cap(
    monkeypatch: pytest.MonkeyPatch, fake_gh: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A stdin stream longer than the cap is cut off at cap + 1 lines instead of being drained."""
    consumed: list[int] = []

    class _Stream:
        """Endless line source that records how many lines were taken."""

        def __iter__(self) -> _Stream:
            """Return the stream itself as its own iterator."""
            return self

        def __next__(self) -> str:
            """Yield one more symbol line and count it."""
            consumed.append(1)
            return f"Sym{len(consumed)}\n"

        def isatty(self) -> bool:
            """Report a pipe, so the symbols are read from stdin."""
            return False

    monkeypatch.setattr(sdc.sys, "stdin", _Stream())
    rc = sdc.main(["--package", "pkg"])
    assert rc == 1
    assert len(consumed) == sdc._MAX_SYMBOLS + 1
    assert fake_gh == []


def test_missing_package_exits_1(fake_gh: list[list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    """No ``--package`` → exit 1, stderr message."""
    rc = sdc.main(["SomeSymbol"])
    assert rc == 1
    assert "--package required" in capsys.readouterr().err
    assert fake_gh == []


def test_no_symbols_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify command-line option behavior.

    --package present, empty stdin, no argv symbols → exit 1.
    """
    monkeypatch.setattr(sdc.sys, "stdin", io.StringIO(""))
    rc = sdc.main(["--package", "mylib"])
    assert rc == 1
    assert "no symbols provided" in capsys.readouterr().err


def test_successful_search_prints_repos(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Successful gh query → print sorted deduplicated repo names, exit 0."""
    call_count = 0

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return duplicate repositories for every successful search call."""
        nonlocal call_count
        call_count += 1
        return _FakeCompleted(returncode=0, stdout="owner/repo-b\nowner/repo-a\n")

    monkeypatch.setattr(sdc.subprocess, "run", _fake_run)
    monkeypatch.setattr(sdc, "which", lambda _: "/fake/gh")
    rc = sdc.main(["--package", "mylib", "SymA", "SymB"])
    assert rc == 0
    assert call_count == 2
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln]
    assert lines == sorted(set(lines))
    assert "owner/repo-a" in lines
    assert "owner/repo-b" in lines


def test_all_queries_fail_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """All gh queries return non-zero → exit 2."""
    monkeypatch.setattr(
        sdc.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    monkeypatch.setattr(sdc, "which", lambda _: "/fake/gh")
    rc = sdc.main(["--package", "pkg", "Sym"])
    assert rc == 2
    assert "all symbol queries failed" in capsys.readouterr().err


def test_partial_failure_still_exits_0(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """First symbol fails, second succeeds → exit 0 with results from second."""
    calls = [0]

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Fail the first search and return one repository on the next call."""
        calls[0] += 1
        if calls[0] == 1:
            return _FakeCompleted(returncode=1, stdout="")
        return _FakeCompleted(returncode=0, stdout="org/repo\n")

    monkeypatch.setattr(sdc.subprocess, "run", _fake_run)
    monkeypatch.setattr(sdc, "which", lambda _: "/fake/gh")
    rc = sdc.main(["--package", "pkg", "Sym1", "Sym2"])
    assert rc == 0
    assert "org/repo" in capsys.readouterr().out


def test_deduplicates_repos(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Same repo from multiple symbol queries → printed once."""
    monkeypatch.setattr(
        sdc.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="org/shared-repo\n"),
    )
    monkeypatch.setattr(sdc, "which", lambda _: "/fake/gh")
    rc = sdc.main(["--package", "pkg", "SymA", "SymB", "SymC"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
    assert lines.count("org/shared-repo") == 1


def test_gh_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(sdc, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="gh"):
        sdc.main(["--package", "pkg", "Sym"])


def test_stdin_symbols(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Symbols read from stdin when none on argv."""
    monkeypatch.setattr(
        sdc.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="org/repo\n"),
    )
    monkeypatch.setattr(sdc, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(sdc.sys, "stdin", io.StringIO("SymFromStdin\n"))
    monkeypatch.setattr(sdc.sys.stdin, "isatty", lambda: False)
    rc = sdc.main(["--package", "pkg"])
    assert rc == 0
    assert "org/repo" in capsys.readouterr().out
