"""Tests for the output-path fence of ``build_codemap_batch.py`` and the bounded search of ``resolve_review_target.py``.

Both scripts take a path from the skill's arguments, so a hostile or mistyped value must not write outside the project
and temp dir, nor walk an unbounded directory tree.
"""

from __future__ import annotations

from pathlib import Path

import build_codemap_batch
import pytest
import resolve_review_target


class TestBuildCodemapBatchOutputFence:
    """The batch request is written only under the working directory or the session temp dir."""

    @pytest.fixture(name="dirs")
    def _dirs_fixture(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
        """Separate working, temp and foreign directories, with the first two active."""
        work, temp, foreign = tmp_path / "work", tmp_path / "temp", tmp_path / "foreign"
        for directory in (work, temp, foreign):
            directory.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("TMPDIR", str(temp))
        return work, temp, foreign

    @pytest.mark.parametrize(
        "location",
        [
            pytest.param(0, id="working-directory"),
            pytest.param(1, id="session-temp-dir"),
        ],
    )
    def test_writes_inside_allowed_roots(self, dirs: tuple[Path, Path, Path], location: int) -> None:
        """A request path under the project or ``$TMPDIR`` is written and exit 0 is returned."""
        out = dirs[location] / "batch.json"

        rc = build_codemap_batch.main([str(out), "--modules", "pkg.mod"])

        assert rc == 0
        assert out.is_file()

    def test_refuses_a_path_outside_both_roots(
        self, dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A path elsewhere on disk exits 1 with a message and creates nothing.

        The script would otherwise write JSON to any location the caller names, including a file the user owns.
        """
        out = dirs[2] / "batch.json"

        rc = build_codemap_batch.main([str(out), "--modules", "pkg.mod"])

        assert rc == 1
        assert "outside the workspace" in capsys.readouterr().err
        assert not out.exists()


class TestHasPythonUnderIsBounded:
    """The Python-presence search stops after a fixed number of entries instead of walking a whole filesystem."""

    def test_finds_a_python_file_in_a_nested_directory(self, tmp_path: Path) -> None:
        """A ``.py`` file below the target is found."""
        (tmp_path / "pkg" / "sub").mkdir(parents=True)
        (tmp_path / "pkg" / "sub" / "mod.py").write_text("", encoding="utf-8")

        assert resolve_review_target.has_python_under(str(tmp_path)) is True

    def test_reports_no_python_in_a_small_tree_without_it(self, tmp_path: Path) -> None:
        """A tree under the cap with no ``.py`` file answers False."""
        (tmp_path / "notes.txt").write_text("", encoding="utf-8")

        assert resolve_review_target.has_python_under(str(tmp_path)) is False

    def test_stops_at_the_cap_and_assumes_python(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Past the entry cap the search gives up and answers True, since absence cannot be proven cheaply."""
        for index in range(6):
            (tmp_path / f"file{index}.txt").write_text("", encoding="utf-8")
        monkeypatch.setattr(resolve_review_target, "_MAX_SCAN_ENTRIES", 3)

        assert resolve_review_target.has_python_under(str(tmp_path)) is True
