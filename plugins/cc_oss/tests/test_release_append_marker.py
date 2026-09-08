"""Tests for ``bin/release_append_marker.py``.

``_is_valid_commit``/``_tag_advanced_past`` mock ``subprocess.run``/``which`` — no real ``git`` invocations.
``_marker_path``/``_read_marker`` are exercised against ``tmp_path`` via the ``--marker-dir`` override so no test
touches a real ``.temp/`` directory.

Two-subprocess-call sequencing: ``resolve``/``is-valid`` call ``_is_valid_commit`` then (only when it's True)
``_tag_advanced_past`` — tests covering that combined path use an iterator side effect to give each call its own return
code rather than one blanket value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import release_append_marker as ram


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0) -> None:
        """Store the status returned by a fake Git command."""
        self.returncode = returncode


def _sequenced_run(monkeypatch: pytest.MonkeyPatch, *returncodes: int) -> None:
    """Patch ``subprocess.run`` to return each of ``returncodes`` in call order."""
    calls = iter(returncodes)
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=next(calls)))


# ---------------------------------------------------------------------------
# Pure functions — _marker_path / _read_marker
# ---------------------------------------------------------------------------


def test_marker_path_joins_branch_into_filename(tmp_path: Path) -> None:
    """Build ``<dir>/release-last-processed-<branch>``."""
    path = ram._marker_path("main", str(tmp_path))
    assert path == tmp_path / "release-last-processed-main"


def test_read_marker_missing_file_is_empty(tmp_path: Path) -> None:
    """No marker file yet → empty string, no exception."""
    assert ram._read_marker("main", str(tmp_path)) == ""


def test_read_marker_strips_whitespace(tmp_path: Path) -> None:
    """Stored sha is stripped of surrounding whitespace/newline."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef123\n", encoding="utf-8")
    assert ram._read_marker("main", str(tmp_path)) == "deadbeef123"


# ---------------------------------------------------------------------------
# Pure function — _is_valid_commit (reachability, not object-DB existence)
# ---------------------------------------------------------------------------


def test_is_valid_commit_empty_sha_is_false() -> None:
    """Blank sha short-circuits to False without invoking git."""
    assert ram._is_valid_commit("") is False


def test_is_valid_commit_git_missing_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """No git on PATH → False, no subprocess call attempted."""
    monkeypatch.setattr(ram, "which", lambda _: None)
    assert ram._is_valid_commit("deadbeef") is False


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [pytest.param(0, True, id="is-ancestor-of-head"), pytest.param(1, False, id="not-ancestor-of-head-rebased-away")],
)
def test_is_valid_commit_reflects_ancestor_check(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    """Mirror ``git merge-base --is-ancestor``'s exit code."""
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=returncode))
    assert ram._is_valid_commit("deadbeef") is expected


def test_is_valid_commit_uses_merge_base_is_ancestor_not_cat_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: must call ``merge-base --is-ancestor``, not ``cat-file -e`` (dangling-commit bug).

    ``cat-file -e`` only tests object-database existence — a commit orphaned by rebase/force-push stays reflog-protected
    (~90 days) and would still report "exists" under that check, silently treating a stale marker as valid. Git's
    ancestor check tests reachability from HEAD instead.
    """
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record reachability argv and return a successful ancestor check."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_run)
    ram._is_valid_commit("deadbeef")
    assert recorded == [["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "HEAD"]]


# ---------------------------------------------------------------------------
# Pure function — _tag_advanced_past (tag-blind marker fix)
# ---------------------------------------------------------------------------


def test_tag_advanced_past_empty_inputs_are_false() -> None:
    """No marker or no tag → nothing to compare, False without invoking git."""
    assert ram._tag_advanced_past("", "v1.0.0") is False
    assert ram._tag_advanced_past("deadbeef", "") is False


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        pytest.param(0, True, id="marker-is-ancestor-of-tag-superseded"),
        pytest.param(1, False, id="tag-predates-marker-normal-case"),
    ],
)
def test_tag_advanced_past_reflects_ancestor_check(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    """Mirror whether the marker is an ancestor of the tag."""
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=returncode))
    assert ram._tag_advanced_past("deadbeef", "v2.0.0") is expected


def test_tag_advanced_past_records_git_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invokes ``git merge-base --is-ancestor <marker> <last_tag>``."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record marker-versus-tag argv and return a successful ancestor check."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_run)
    ram._tag_advanced_past("deadbeef", "v2.0.0")
    assert recorded == [["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "v2.0.0"]]


# ---------------------------------------------------------------------------
# CLI: write
# ---------------------------------------------------------------------------


def test_write_persists_sha_creating_parent_dirs(tmp_path: Path) -> None:
    """Create the marker dir and stores the sha with a trailing newline."""
    marker_dir = tmp_path / "nested" / ".temp"
    rc = ram.main(["write", "--branch", "main", "--sha", "abc123", "--marker-dir", str(marker_dir)])
    assert rc == 0
    assert (marker_dir / "release-last-processed-main").read_text(encoding="utf-8") == "abc123\n"


# ---------------------------------------------------------------------------
# CLI: is-valid
# ---------------------------------------------------------------------------


def test_is_valid_cli_prints_false_when_no_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No marker on disk → CLI prints "false"."""
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


def test_is_valid_cli_prints_true_for_valid_unsuperseded_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker is an ancestor of HEAD and the tag predates it → CLI prints "true"."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 1)  # ancestor-of-HEAD: yes; ancestor-of-tag: no (tag predates marker)
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"


def test_is_valid_cli_prints_false_when_superseded_by_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker valid but a release tag landed at/after it → CLI prints "false" (tag-blind fix)."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 0)  # ancestor-of-HEAD: yes; ancestor-of-tag: yes (superseded)
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v2.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


def test_is_valid_cli_prints_false_when_rebased_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker no longer an ancestor of HEAD (rebase/force-push) → CLI prints "false"."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=1))
    rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


# ---------------------------------------------------------------------------
# CLI: resolve
# ---------------------------------------------------------------------------


def test_resolve_no_marker_falls_back_to_last_tag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No prior marker → RANGE = <last-tag>..HEAD; stderr notes first-baseline."""
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "v1.2.0..HEAD"
    assert "establishing first append baseline" in captured.err


def test_resolve_valid_marker_uses_incremental_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker valid + tag predates it → RANGE = <sha>..HEAD, not <last-tag>..HEAD."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef1234\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 1)  # ancestor-of-HEAD: yes; ancestor-of-tag: no
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "deadbeef1234..HEAD"


def test_resolve_invalid_marker_falls_back_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Marker present but not resolvable in history (rebase-dangling) → falls back, stderr warns."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef1234\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=1))
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "v1.2.0..HEAD"
    assert "not found in history" in captured.err


def test_resolve_marker_valid_but_superseded_by_later_tag_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tag-blind fix: a release cut between the marker and now overrides a stale-but-reachable marker."""
    (tmp_path / "release-last-processed-main").write_text("deadbeef1234\n", encoding="utf-8")
    _sequenced_run(monkeypatch, 0, 0)  # ancestor-of-HEAD: yes; ancestor-of-tag: yes (tag cut at/after marker)
    rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v2.0.0", "--marker-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "v2.0.0..HEAD"
    assert "superseded by a release tag" in captured.err


def test_resolve_records_git_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Record reachability checks against both ``HEAD`` and the latest tag."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record both marker validation checks and report success for each."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    (tmp_path / "release-last-processed-main").write_text("deadbeef\n", encoding="utf-8")
    monkeypatch.setattr(ram, "which", lambda _: "/fake/git")
    monkeypatch.setattr(ram.subprocess, "run", _fake_run)
    ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
    assert recorded[0] == ["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "HEAD"]
    assert recorded[1] == ["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", "deadbeef", "v1.2.0"]
