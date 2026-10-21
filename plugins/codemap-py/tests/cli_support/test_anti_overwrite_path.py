"""Tests for ``bin/anti_overwrite_path.py`` — first free ``<dir>/<stem>[-<branch>-<date>][-N].md`` path."""

from __future__ import annotations

import datetime
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import anti_overwrite_path
import pytest


def _fake_git(stdout: str = "", returncode: int = 0) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a ``subprocess.run`` stand-in answering ``git branch --show-current``."""

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")

    return run


def _failing_git(error: Exception) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a ``subprocess.run`` stand-in that raises ``error``."""

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    return run


class TestFirstFreePath:
    """The counter starts at ``-2`` and climbs past every taken name."""

    def test_free_stem_gets_no_suffix(self, tmp_path: Path) -> None:
        """A name nobody has taken is returned bare."""
        assert anti_overwrite_path.first_free_path(str(tmp_path), "report") == f"{tmp_path}/report.md"

    def test_taken_stem_gets_counter_two(self, tmp_path: Path) -> None:
        """The first collision yields ``-2``, never ``-1``."""
        (tmp_path / "report.md").write_text("x", encoding="utf-8")

        assert anti_overwrite_path.first_free_path(str(tmp_path), "report") == f"{tmp_path}/report-2.md"

    def test_counter_climbs_past_every_taken_name(self, tmp_path: Path) -> None:
        """Three same-day reruns land on ``-3``."""
        (tmp_path / "report.md").write_text("x", encoding="utf-8")
        (tmp_path / "report-2.md").write_text("x", encoding="utf-8")

        assert anti_overwrite_path.first_free_path(str(tmp_path), "report") == f"{tmp_path}/report-3.md"

    def test_first_gap_is_used(self, tmp_path: Path) -> None:
        """With ``report.md`` and ``report-3.md`` taken the answer is ``-2``: the first free slot, not the last + 1."""
        (tmp_path / "report.md").write_text("x", encoding="utf-8")
        (tmp_path / "report-3.md").write_text("x", encoding="utf-8")

        assert anti_overwrite_path.first_free_path(str(tmp_path), "report") == f"{tmp_path}/report-2.md"

    def test_directory_squatting_on_the_name_advances_the_counter(self, tmp_path: Path) -> None:
        """A directory named ``report.md`` would make the caller's Write fail, so it counts as taken."""
        (tmp_path / "report.md").mkdir()

        assert anti_overwrite_path.first_free_path(str(tmp_path), "report") == f"{tmp_path}/report-2.md"

    def test_directory_text_is_joined_verbatim(self) -> None:
        """The printed path is textual: a leading dot directory is not normalised away."""
        assert anti_overwrite_path.first_free_path(".temp-does-not-exist", "x") == ".temp-does-not-exist/x.md"

    def test_creates_nothing(self, tmp_path: Path) -> None:
        """Pure resolution — the Write tool creates parent directories, this never does."""
        anti_overwrite_path.first_free_path(str(tmp_path / "new-dir"), "report")

        assert list(tmp_path.iterdir()) == []


class TestBranchSlug:
    """The branch slug mirrors ``git branch --show-current | tr '/' '-'`` with a ``main`` fallback."""

    @pytest.mark.parametrize(
        ("stdout", "returncode", "expected"),
        [
            pytest.param("feature\n", 0, "feature", id="plain"),
            pytest.param("feat/add-thing\n", 0, "feat-add-thing", id="slash-becomes-dash"),
            pytest.param("a/b/c\n", 0, "a-b-c", id="every-slash"),
            pytest.param("\n", 0, "main", id="detached-head-prints-nothing"),
            pytest.param("", 0, "main", id="empty"),
            pytest.param("feature\n", 128, "main", id="git-failed"),
        ],
    )
    def test_slug(self, monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: str) -> None:
        """Slashes become dashes; empty output or a failed git falls back to ``main``."""
        monkeypatch.setattr(anti_overwrite_path.subprocess, "run", _fake_git(stdout, returncode))

        assert anti_overwrite_path.branch_slug() == expected

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(FileNotFoundError("git"), id="git-not-installed"),
            pytest.param(subprocess.TimeoutExpired(cmd="git", timeout=5), id="git-hangs"),
        ],
    )
    def test_unavailable_git_falls_back_to_main(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        """No git binary, or a git that hangs, must not break report-path selection."""
        monkeypatch.setattr(anti_overwrite_path.subprocess, "run", _failing_git(error))

        assert anti_overwrite_path.branch_slug() == "main"


class TestMain:
    """CLI behaviour: printed path, ``--branch-date`` stem, argument errors."""

    def test_prints_the_free_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The free path is printed on stdout for the caller to read from the tool output."""
        rc = anti_overwrite_path.main([str(tmp_path), "output-x"])

        assert rc == 0
        assert capsys.readouterr().out == f"{tmp_path}/output-x.md\n"

    def test_branch_date_stem_and_counter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--branch-date`` appends the slugged branch and today's date, and the counter applies to that stem."""
        monkeypatch.setattr(anti_overwrite_path.subprocess, "run", _fake_git("feat/x\n"))
        today = f"{datetime.date.today():%Y-%m-%d}"
        (tmp_path / f"output-r-feat-x-{today}.md").write_text("old", encoding="utf-8")

        rc = anti_overwrite_path.main([str(tmp_path), "output-r", "--branch-date"])

        assert rc == 0
        assert capsys.readouterr().out == f"{tmp_path}/output-r-feat-x-{today}-2.md\n"

    def test_branch_date_shape_with_real_git(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without a patched git the result still has the ``-<branch>-<YYYY-MM-DD>.md`` shape."""
        anti_overwrite_path.main([str(tmp_path), "output-r", "--branch-date"])

        shape = rf"{re.escape(str(tmp_path))}/output-r-[^/\s]+-\d{{4}}-\d{{2}}-\d{{2}}\.md\n"
        assert re.fullmatch(shape, capsys.readouterr().out)

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["../up", "stem"], id="dir-traversal"),
            pytest.param([".temp", "../stem"], id="stem-traversal"),
            pytest.param(["a\\..\\b", "stem"], id="backslash-traversal"),
            pytest.param(["", "stem"], id="empty-dir"),
            pytest.param([".temp", ""], id="empty-stem"),
        ],
    )
    def test_refused_arguments_exit_2_and_print_nothing(
        self, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """Traversal or an empty argument is refused with a stderr message and no path on stdout."""
        rc = anti_overwrite_path.main(argv)

        assert rc == 2
        captured = capsys.readouterr()
        assert (captured.out, bool(captured.err)) == ("", True)

    def test_dotted_names_are_not_traversal(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``a..b`` is an ordinary name; only a whole ``..`` segment is traversal."""
        rc = anti_overwrite_path.main([".temp", "output-a..b"])

        assert rc == 0
        assert capsys.readouterr().out.startswith(".temp/output-a..b")

    def test_missing_positional_is_a_usage_error(self) -> None:
        """``STEM`` is required."""
        with pytest.raises(SystemExit) as raised:
            anti_overwrite_path.main([".temp"])

        assert raised.value.code == 2
