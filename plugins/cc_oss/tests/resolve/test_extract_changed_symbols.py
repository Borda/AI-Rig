"""Tests for ``bin/extract_changed_symbols.py``.

``subprocess.run`` and ``which`` monkeypatched — no real ``git`` calls. ``tmp_path`` + ``monkeypatch.chdir`` control
which ``__init__.py`` files are visible to ``_find_init_files``. Tests cover range validation, empty diff, symbol
extraction, deduplication, and default range behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import extract_changed_symbols as ecs


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


def _patch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rev_parse_rc: int = 0,
    diff_stdout: str = "",
) -> None:
    """Patch subprocess.run dispatching on git subcommand."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return the configured ref-validation result or diff payload."""
        if "rev-parse" in cmd:
            return _FakeCompleted(returncode=rev_parse_rc)
        return _FakeCompleted(returncode=0, stdout=diff_stdout)

    monkeypatch.setattr(ecs.subprocess, "run", _fake_run)
    monkeypatch.setattr(ecs, "which", lambda _: "/fake/git")


def test_invalid_left_ref_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Left ref of range does not resolve → exit 0, empty stdout."""
    _patch_git(monkeypatch, rev_parse_rc=1)
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["nonexistent..HEAD"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_invalid_single_ref_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-existent single ref → exit 0, empty stdout."""
    _patch_git(monkeypatch, rev_parse_rc=1)
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["nonexistent_ref"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_no_init_py_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``__init__.py`` in tree → exit 0, empty stdout."""
    (tmp_path / "module.py").write_text("class Foo: pass\n")
    _patch_git(monkeypatch)
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["HEAD~1..HEAD"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_empty_diff_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return empty successful output for an unchanged initializer."""
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout="")
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["HEAD~1..HEAD"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_extracts_class_and_def_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Diff with class and def additions → both symbol names in stdout."""
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout="+class Foo:\n+def bar():\n+    pass\n")
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["HEAD~1..HEAD"])
    assert rc == 0
    symbols = capsys.readouterr().out.splitlines()
    assert "Foo" in symbols
    assert "bar" in symbols


def test_symbols_sorted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Output symbols are sorted (sort -u behaviour)."""
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout="+def zoo():\n+class Alpha:\n+def Beta():\n")
    monkeypatch.chdir(tmp_path)
    ecs.main(["HEAD~1..HEAD"])
    symbols = capsys.readouterr().out.splitlines()
    assert symbols == sorted(symbols)


def test_symbols_deduplicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same symbol name from multiple diff lines → printed only once."""
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout="+class Dup:\n-class Dup:\n+class Dup:\n")
    monkeypatch.chdir(tmp_path)
    ecs.main(["HEAD~1..HEAD"])
    symbols = capsys.readouterr().out.splitlines()
    assert symbols.count("Dup") == 1


def test_context_lines_not_extracted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Diff context lines (no leading +/-) not included in output."""
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout=" class Context: pass\n+class Added:\n")
    monkeypatch.chdir(tmp_path)
    ecs.main(["HEAD~1..HEAD"])
    symbols = capsys.readouterr().out.splitlines()
    assert "Context" not in symbols
    assert "Added" in symbols


def test_diff_header_lines_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Diff ``---``/``+++`` header lines not treated as symbol lines."""
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout="--- a/__init__.py\n+++ b/__init__.py\n+class Real:\n")
    monkeypatch.chdir(tmp_path)
    ecs.main(["HEAD~1..HEAD"])
    symbols = capsys.readouterr().out.splitlines()
    assert "Real" in symbols
    assert len(symbols) == 1


def test_default_range_used_when_no_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No argv → script uses ``HEAD~1..HEAD`` (rev-parse called for both ends)."""
    (tmp_path / "__init__.py").write_text("")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Record Git calls and return an empty successful diff response."""
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(ecs.subprocess, "run", _fake_run)
    monkeypatch.setattr(ecs, "which", lambda _: "/fake/git")
    monkeypatch.chdir(tmp_path)
    ecs.main([])
    rev_parse_calls = [c for c in calls if "rev-parse" in c]
    assert any("HEAD~1" in c for c in rev_parse_calls)
    assert any("HEAD" in c for c in rev_parse_calls)
