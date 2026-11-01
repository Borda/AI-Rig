"""Tests for ``bin/anti_overwrite_path.py`` — first free ``<dir>/<stem>[-<branch>-<date>][-N].md`` path.

``git`` is mocked at the ``subprocess.run`` boundary wherever a test only checks slug or stem logic; one test keeps the
real ``git`` call so the whole branch-and-date path shape is still proven against a real process.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from pathlib import Path
from unittest import mock

import anti_overwrite_path
import pytest

_FIXED_DAY = datetime.date(2026, 1, 2)


def _git_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Return the ``git branch --show-current`` result a mocked ``subprocess.run`` hands back."""
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


def _occupy(directory: Path, files: tuple[str, ...] = (), dirs: tuple[str, ...] = ()) -> None:
    """Create each named file and directory under *directory*, so those report names are taken."""
    for name in files:
        (directory / name).write_text("x", encoding="utf-8")
    for name in dirs:
        (directory / name).mkdir()


class TestFirstFreePath:
    """The counter starts at ``-2`` and climbs past every taken name."""

    @pytest.mark.parametrize(
        ("files", "dirs", "expected"),
        [
            pytest.param((), (), "report.md", id="free-stem-gets-no-suffix"),
            pytest.param(("report.md",), (), "report-2.md", id="taken-stem-gets-counter-two-never-one"),
            pytest.param(("report.md", "report-2.md"), (), "report-3.md", id="counter-climbs-past-every-taken-name"),
            pytest.param(("report.md", "report-3.md"), (), "report-2.md", id="first-gap-is-used-not-last-plus-one"),
            pytest.param((), ("report.md",), "report-2.md", id="directory-squatting-on-name-counts-as-taken"),
        ],
    )
    def test_picks_first_free_name(self, tmp_path: Path, files: tuple[str, ...], dirs: tuple[str, ...], expected: str):
        """The first unused ``report[-N].md`` is returned.

        A directory named ``report.md`` also counts as taken, because the caller's Write would fail on it. Three same-
        day reruns therefore land on ``-3``, and a gap at ``-2`` is reused before ``-4``.
        """
        _occupy(tmp_path, files, dirs)

        assert anti_overwrite_path.first_free_path(str(tmp_path), "report") == f"{tmp_path}/{expected}"

    def test_directory_text_is_joined_verbatim(self) -> None:
        """The printed path is textual: a leading dot directory is not normalised away."""
        assert anti_overwrite_path.first_free_path(".temp-does-not-exist", "x") == ".temp-does-not-exist/x.md"

    def test_creates_nothing(self, tmp_path: Path) -> None:
        """Pure resolution — the Write tool creates parent directories, this never does.

        Resolving a path inside a directory that does not exist yet must leave the filesystem untouched.
        """
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
    @mock.patch("anti_overwrite_path.subprocess.run", autospec=True)
    def test_slug(self, mock_run: mock.Mock, stdout: str, returncode: int, expected: str) -> None:
        """Slashes become dashes; empty output or a failed git falls back to ``main``.

        The mocked ``git branch --show-current`` answers each documented outcome: a named branch, a nested branch, a
        detached HEAD with no output, and a non-zero exit.
        """
        mock_run.return_value = _git_result(stdout, returncode)

        assert anti_overwrite_path.branch_slug() == expected

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(FileNotFoundError("git"), id="git-not-installed"),
            pytest.param(subprocess.TimeoutExpired(cmd="git", timeout=5), id="git-hangs"),
        ],
    )
    @mock.patch("anti_overwrite_path.subprocess.run", autospec=True)
    def test_unavailable_git_falls_back_to_main(self, mock_run: mock.Mock, error: Exception) -> None:
        """No git binary, or a git that hangs, must not break report-path selection.

        Either failure surfaces from ``subprocess.run``; the slug must degrade to ``main`` instead of raising.
        """
        mock_run.side_effect = error

        assert anti_overwrite_path.branch_slug() == "main"


class TestMain:
    """CLI behaviour: printed path, ``--branch-date`` stem, argument errors."""

    def test_prints_free_path(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The free path is printed on stdout for the caller to read from the tool output."""
        rc = anti_overwrite_path.main([str(tmp_path), "output-x"])

        assert rc == 0
        assert capsys.readouterr().out == f"{tmp_path}/output-x.md\n"

    @mock.patch("anti_overwrite_path.datetime", autospec=True)
    @mock.patch("anti_overwrite_path.subprocess.run", autospec=True)
    def test_branch_date_stem_and_counter(
        self, mock_run: mock.Mock, mock_datetime: mock.Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--branch-date`` appends the slugged branch and today's date, and the counter applies to that stem.

        A report for the same branch and day already exists, so the printed path must carry the ``-2`` counter after the
        date. Branch and date are pinned so the expected name does not depend on the clock or the checkout.
        """
        mock_run.return_value = _git_result("feat/x\n")
        mock_datetime.date.today.return_value = _FIXED_DAY
        (tmp_path / "output-r-feat-x-2026-01-02.md").write_text("old", encoding="utf-8")

        rc = anti_overwrite_path.main([str(tmp_path), "output-r", "--branch-date"])

        assert rc == 0
        assert capsys.readouterr().out == f"{tmp_path}/output-r-feat-x-2026-01-02-2.md\n"

    def test_branch_date_shape_with_real_git(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without a patched git the result still has the ``-<branch>-<YYYY-MM-DD>.md`` shape.

        Keeps the real ``git branch --show-current`` and real clock in the loop, so the mocked tests above cannot hide a
        break in the actual process call.
        """
        anti_overwrite_path.main([str(tmp_path), "output-r", "--branch-date"])

        # The date part is a raw string: docformatter 1.7.8 on CPython 3.12.3 corrupts "\d{{4}}" inside an f-string.
        shape = rf"{re.escape(str(tmp_path))}/output-r-[^/\s]+-" + r"\d{4}-\d{2}-\d{2}\.md\n"
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
        """Traversal or an empty argument is refused with a stderr message and no path on stdout.

        A refused call must not hand the caller a path it could pass to the Write tool.
        """
        rc = anti_overwrite_path.main(argv)

        assert rc == 2
        captured = capsys.readouterr()
        assert (captured.out, bool(captured.err)) == ("", True)

    def test_dotted_names_are_not_traversal(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``a..b`` is an ordinary name; only a whole ``..`` segment is traversal."""
        rc = anti_overwrite_path.main([".temp", "output-a..b"])

        assert rc == 0
        assert capsys.readouterr().out.startswith(".temp/output-a..b")

    def test_missing_positional_is_usage_error(self) -> None:
        """``STEM`` is required.

        Argparse rejects a call with only ``DIR`` by exiting with status 2.
        """
        with pytest.raises(SystemExit) as raised:
            anti_overwrite_path.main([".temp"])

        assert raised.value.code == 2
