"""Tests for ``bin/resolve_review_target.py`` — resolve a review target and its Python files.

Covers:
* Explicit-path mode: a file target and a directory target both count as "has Python"
* Diff mode: Python-file count, the non-Python early-stop line, and warning ordering
* Dependency / container / tests-only warnings, including the diff-mode-only gate
* Exit code: 0 for a reviewable Python target, 1 for a missing path or a target without Python (the caller stops on it)
* ``--committed``: diff mode reads ``HEAD~1..HEAD`` so a fresh worktree is not reviewed as an empty diff
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "resolve_review_target.py"
_spec = importlib.util.spec_from_file_location("develop_resolve_review_target", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

collect_warnings = _mod.collect_warnings
has_python_under = _mod.has_python_under
main = _mod.main

_NO_PYTHON_PREFIX = "! Diff contains non-Python files only."


def _patch_diff(monkeypatch: pytest.MonkeyPatch, files: list[str]) -> None:
    monkeypatch.setattr(_mod, "changed_files", lambda timeout=5, committed=False: list(files))


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # ``find <file.py> -name '*.py'`` prints the file; ``rglob`` alone would miss it
        pytest.param("mod.py", True, id="python-file-target"),
        pytest.param("notes.md", False, id="non-python-file-target"),
    ],
)
def test_has_python_under_file_target(tmp_path: Path, filename: str, expected: bool) -> None:
    """A single file target counts as holding Python only when it is a ``.py`` file."""
    target = tmp_path / filename
    target.write_text("x", encoding="utf-8")
    assert has_python_under(str(target)) is expected


def test_has_python_under_directory(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "mod.py").write_text("x", encoding="utf-8")
    assert has_python_under(str(tmp_path)) is True


def test_has_python_under_missing_path() -> None:
    assert has_python_under("definitely/not/here") is False


@pytest.mark.parametrize(
    ("files", "diff_mode", "expected_count"),
    [
        (["pyproject.toml"], False, 1),
        (["requirements-dev.txt"], False, 1),
        (["setup.cfg", "Dockerfile"], False, 2),
        (["docker-compose.prod.yml"], False, 1),
        (["tests/test_a.py"], True, 1),
        (["tests/test_a.py"], False, 0),
        (["tests/test_a.py", "src/a.py"], True, 0),
        (["README.md"], True, 0),
    ],
    ids=["deps", "reqs", "deps+container", "compose", "tests-only", "tests-only-path-mode", "mixed", "none"],
)
def test_collect_warnings(files: list[str], diff_mode: bool, expected_count: int) -> None:
    assert len(collect_warnings(files, diff_mode)) == expected_count


def test_diff_mode_reports_python_count(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_diff(monkeypatch, ["src/a.py", "src/b.py", "README.md"])
    assert main(["--", ""]) == 0
    out = capsys.readouterr().out
    assert "src/a.py" in out
    assert "Reviewing: working-tree diff (2 Python files)" in out
    assert _NO_PYTHON_PREFIX not in out


def test_diff_mode_without_python_prints_stop_line_then_warnings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_diff(monkeypatch, ["pyproject.toml", "Dockerfile"])
    assert main(["--", ""]) == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines[-3].startswith(_NO_PYTHON_PREFIX)
    assert lines[-2].startswith("⚠ dependency changes detected")
    assert lines[-1].startswith("⚠ container config changes detected")


def test_path_mode_prints_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_diff(monkeypatch, [])
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x", encoding="utf-8")
    assert main(["--", str(pkg)]) == 0
    out = capsys.readouterr().out
    assert f"Reviewing: {pkg}" in out
    assert _NO_PYTHON_PREFIX not in out


def test_path_mode_tests_directory_is_never_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An explicit tests/ target is deliberate; the tests-only warning is diff-mode only."""
    _patch_diff(monkeypatch, ["tests/test_a.py"])
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("x", encoding="utf-8")
    assert main(["--", str(tests)]) == 0
    assert "no src/ changes" not in capsys.readouterr().out


def test_leading_dash_target_does_not_break_parsing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_diff(monkeypatch, [])
    assert main(["--", "--weird-path"]) == 1  # parsed as a path (not an option), which does not exist
    assert "! Review target not found: --weird-path" in capsys.readouterr().out


def test_changed_files_survives_missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("git not found")

    monkeypatch.setattr(_mod.subprocess, "run", _boom)
    assert _mod.changed_files() == []


def test_missing_path_stops_with_specific_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A nonexistent explicit path is its own stop, not the misleading non-Python message."""
    _patch_diff(monkeypatch, [])
    missing = tmp_path / "nope.py"
    assert main(["--", str(missing)]) == 1
    out = capsys.readouterr().out
    assert f"! Review target not found: {missing}" in out
    assert _NO_PYTHON_PREFIX not in out


def test_existing_non_python_path_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_diff(monkeypatch, [])
    notes = tmp_path / "notes.md"
    notes.write_text("x", encoding="utf-8")
    assert main(["--", str(notes)]) == 1
    assert _NO_PYTHON_PREFIX in capsys.readouterr().out


def test_committed_flag_reads_last_commit(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[bool] = []

    def _fake(timeout: int = 5, committed: bool = False) -> list[str]:
        seen.append(committed)
        return ["src/a.py"]

    monkeypatch.setattr(_mod, "changed_files", _fake)
    assert main(["--committed", "--", ""]) == 0
    assert seen == [True]
    assert "Reviewing: last commit (HEAD~1..HEAD) (1 Python files)" in capsys.readouterr().out


def test_changed_files_committed_diffs_head_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Done:
        stdout = "a.py\n"

    def _run(cmd: list[str], **_kwargs: object) -> _Done:
        calls.append(cmd)
        return _Done()

    monkeypatch.setattr(_mod.subprocess, "run", _run)
    assert _mod.changed_files(committed=True) == ["a.py"]
    assert calls == [["git", "diff", "--name-only", "HEAD~1", "HEAD"]]
