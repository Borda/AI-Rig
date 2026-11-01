"""Tests for ``bin/resolve_review_target.py`` — resolve a review target and its Python files.

Covers:
* Explicit-path mode: a file target and a directory target both count as "has Python"
* Diff mode: Python-file count, the non-Python early-stop line, and warning ordering
* Dependency / container / tests-only warnings, including the diff-mode-only gate
* Exit code: 0 for a reviewable Python target, 1 for a missing path or a target without Python (the caller stops on it)
* ``--committed``: diff mode reads ``HEAD~1..HEAD`` so a fresh worktree is not reviewed as an empty diff
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from _develop_text import load_module

_mod = load_module(
    Path(__file__).resolve().parents[2] / "bin" / "resolve_review_target.py", "develop_resolve_review_target"
)

collect_warnings = _mod.collect_warnings
has_python_under = _mod.has_python_under
main = _mod.main

_NO_PYTHON_PREFIX = "! Diff contains non-Python files only."


@pytest.fixture
def set_diff(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], None]:
    """Return a setter that replaces the working-tree changed-file list the script would read from git."""

    def _set(files: list[str]) -> None:
        monkeypatch.setattr(_mod, "changed_files", lambda timeout=5, committed=False: list(files))

    return _set


class TestHasPythonUnder:
    """``has_python_under`` decides whether an explicit target holds anything reviewable."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            # ``find <file.py> -name '*.py'`` prints the file; ``rglob`` alone would miss it
            pytest.param("mod.py", True, id="python-file-target"),
            pytest.param("notes.md", False, id="non-python-file-target"),
        ],
    )
    def test_file_target(self, tmp_path: Path, filename: str, expected: bool) -> None:
        """A single file target counts as holding Python only when it is a ``.py`` file."""
        target = tmp_path / filename
        target.write_text("x", encoding="utf-8")
        assert has_python_under(str(target)) is expected

    def test_directory_target_finds_nested_python(self, tmp_path: Path) -> None:
        """A directory counts as holding Python when a ``.py`` file sits anywhere below it."""
        nested = tmp_path / "src" / "pkg"
        nested.mkdir(parents=True)
        (nested / "mod.py").write_text("x", encoding="utf-8")
        assert has_python_under(str(tmp_path)) is True

    def test_missing_path_has_no_python(self) -> None:
        """A path that does not exist holds no Python and does not raise."""
        assert has_python_under("definitely/not/here") is False


@pytest.mark.parametrize(
    ("files", "diff_mode", "expected_count"),
    [
        pytest.param(["pyproject.toml"], False, 1, id="deps"),
        pytest.param(["requirements-dev.txt"], False, 1, id="reqs"),
        pytest.param(["setup.cfg", "Dockerfile"], False, 2, id="deps+container"),
        pytest.param(["docker-compose.prod.yml"], False, 1, id="compose"),
        pytest.param(["tests/test_a.py"], True, 1, id="tests-only"),
        pytest.param(["tests/test_a.py"], False, 0, id="tests-only-path-mode"),
        pytest.param(["tests/test_a.py", "src/a.py"], True, 0, id="mixed"),
        pytest.param(["README.md"], True, 0, id="none"),
    ],
)
def test_collect_warnings(files: list[str], diff_mode: bool, expected_count: int) -> None:
    """Dependency, container and tests-only changes each raise one warning; the tests-only one is diff-mode only.

    A tests-only diff warns that nothing under ``src/`` changed, but an explicit ``tests/`` path is deliberate and must
    stay quiet.
    """
    assert len(collect_warnings(files, diff_mode)) == expected_count


class TestMain:
    """``main`` prints the review header and exits 0 for a reviewable target, 1 when the caller must stop."""

    def test_diff_mode_reports_python_count(
        self, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A mixed diff prints its changed Python files and the Python-file count.

        Non-Python files such as ``README.md`` are listed in the diff but must not be counted as reviewable.
        """
        set_diff(["src/a.py", "src/b.py", "README.md"])
        assert main(["--", ""]) == 0
        out = capsys.readouterr().out
        assert "src/a.py" in out
        assert "Reviewing: working-tree diff (2 Python files)" in out
        assert _NO_PYTHON_PREFIX not in out

    def test_diff_mode_without_python_prints_stop_line_then_warnings(
        self, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A diff with no Python exits 1 with the stop line first, then the dependency and container warnings.

        The caller stops on the exit code, so the warnings must still follow the stop line in a stable order.
        """
        set_diff(["pyproject.toml", "Dockerfile"])
        assert main(["--", ""]) == 1
        lines = [line for line in capsys.readouterr().out.splitlines() if line]
        assert lines[-3].startswith(_NO_PYTHON_PREFIX)
        assert lines[-2].startswith("⚠ dependency changes detected")
        assert lines[-1].startswith("⚠ container config changes detected")

    def test_path_mode_prints_target(
        self, tmp_path: Path, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An explicit directory with Python files prints ``Reviewing: <path>`` and exits 0."""
        set_diff([])
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("x", encoding="utf-8")
        assert main(["--", str(pkg)]) == 0
        out = capsys.readouterr().out
        assert f"Reviewing: {pkg}" in out
        assert _NO_PYTHON_PREFIX not in out

    def test_path_mode_tests_directory_is_never_warned(
        self, tmp_path: Path, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An explicit tests/ target is deliberate; the tests-only warning is diff-mode only."""
        set_diff(["tests/test_a.py"])
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("x", encoding="utf-8")
        assert main(["--", str(tests)]) == 0
        assert "no src/ changes" not in capsys.readouterr().out

    def test_leading_dash_target_does_not_break_parsing(
        self, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A target spelled like an option is parsed as a path after ``--`` and reported as not found.

        The call site always passes ``--`` first, so a path such as ``--weird-path`` must not be read as a flag.
        """
        set_diff([])
        assert main(["--", "--weird-path"]) == 1
        assert "! Review target not found: --weird-path" in capsys.readouterr().out

    def test_missing_path_stops_with_specific_message(
        self, tmp_path: Path, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A nonexistent explicit path is its own stop, not the misleading non-Python message."""
        set_diff([])
        missing = tmp_path / "nope.py"
        assert main(["--", str(missing)]) == 1
        out = capsys.readouterr().out
        assert f"! Review target not found: {missing}" in out
        assert _NO_PYTHON_PREFIX not in out

    def test_existing_non_python_path_stops(
        self, tmp_path: Path, set_diff: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An existing path without Python exits 1 with the non-Python stop line."""
        set_diff([])
        notes = tmp_path / "notes.md"
        notes.write_text("x", encoding="utf-8")
        assert main(["--", str(notes)]) == 1
        assert _NO_PYTHON_PREFIX in capsys.readouterr().out

    @mock.patch.object(_mod, "changed_files", autospec=True, return_value=["src/a.py"])
    def test_committed_flag_reads_last_commit(
        self, changed_files: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--committed`` asks for the last commit's files and labels the review ``HEAD~1..HEAD``.

        A fresh worktree has a clean working tree, so without this flag its review would see an empty diff.
        """
        assert main(["--committed", "--", ""]) == 0
        changed_files.assert_called_once_with(timeout=mock.ANY, committed=True)
        assert "Reviewing: last commit (HEAD~1..HEAD) (1 Python files)" in capsys.readouterr().out


class TestChangedFiles:
    """``changed_files`` shells out to git; the subprocess boundary is mocked because git itself is not under test."""

    @mock.patch.object(_mod.subprocess, "run", autospec=True, side_effect=OSError("git not found"))
    def test_survives_missing_git(self, run: mock.MagicMock) -> None:
        """A machine without git yields an empty change list instead of raising."""
        assert _mod.changed_files() == []
        run.assert_called_once()

    @mock.patch.object(_mod.subprocess, "run", autospec=True, return_value=SimpleNamespace(stdout="a.py\n"))
    def test_committed_diffs_head_parent(self, run: mock.MagicMock) -> None:
        """``committed=True`` diffs ``HEAD~1`` against ``HEAD`` and returns the printed file names.

        The exact argv is the contract: it is what makes a fresh worktree reviewable.
        """
        assert _mod.changed_files(committed=True) == ["a.py"]
        assert run.call_args.args[0] == ["git", "diff", "--name-only", "HEAD~1", "HEAD"]
