"""Tests for ``bin/extract_changed_symbols.py``.

``subprocess.run`` and ``which`` monkeypatched — no real ``git`` calls. ``tmp_path`` + ``monkeypatch.chdir`` control
which ``__init__.py`` files are visible to ``_find_init_files``. Tests cover range validation, empty diff, symbol
extraction, deduplication, and default range behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import extract_changed_symbols as ecs
import pytest


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


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param("nonexistent..HEAD", id="left-ref-of-range-unresolved"),
        pytest.param("nonexistent_ref", id="single-ref-unresolved"),
    ],
)
def test_unresolvable_ref_exits_0_with_empty_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], ref: str
) -> None:
    """A range whose left ref, or a single ref, does not resolve → exit 0, empty stdout."""
    _patch_git(monkeypatch, rev_parse_rc=1)
    monkeypatch.chdir(tmp_path)
    rc = ecs.main([ref])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        pytest.param("module.py", "class Foo: pass\n", id="no-init-py-in-tree"),
        pytest.param("__init__.py", "", id="empty-diff-for-unchanged-initializer"),
    ],
)
def test_nothing_to_extract_exits_0_with_empty_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], filename: str, content: str
) -> None:
    """No ``__init__.py`` in the tree, or an empty diff for an unchanged initializer → exit 0, empty stdout."""
    (tmp_path / filename).write_text(content)
    _patch_git(monkeypatch, diff_stdout="")
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["HEAD~1..HEAD"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


@pytest.mark.parametrize(
    ("diff_stdout", "expected"),
    [
        pytest.param("+class Foo:\n+def bar():\n+    pass\n", ["Foo", "bar"], id="class-and-def-names-extracted"),
        pytest.param(
            "+def zoo():\n+class Alpha:\n+def Beta():\n", ["Alpha", "Beta", "zoo"], id="symbols-sorted-sort-u-behaviour"
        ),
        pytest.param("+class Dup:\n-class Dup:\n+class Dup:\n", ["Dup"], id="repeated-symbol-printed-once"),
        pytest.param(" class Context: pass\n+class Added:\n", ["Added"], id="context-lines-not-extracted"),
        pytest.param(
            "--- a/__init__.py\n+++ b/__init__.py\n+class Real:\n", ["Real"], id="diff-header-lines-not-symbol-lines"
        ),
    ],
)
def test_symbols_extracted_from_changed_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    diff_stdout: str,
    expected: list[str],
) -> None:
    """Only added or removed ``class``/``def`` lines yield symbols, printed sorted and without duplicates.

    Context lines (no leading +/-) and the ``---``/``+++`` diff header lines are not symbol lines; the same name from
    several diff lines is printed once; output order is the ``sort -u`` order.
    """
    (tmp_path / "__init__.py").write_text("")
    _patch_git(monkeypatch, diff_stdout=diff_stdout)
    monkeypatch.chdir(tmp_path)
    rc = ecs.main(["HEAD~1..HEAD"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == expected


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
