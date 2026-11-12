"""Verify refactor batch modules follow explicit repository files rather than diff heuristics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import build_codemap_batch
import pytest
from _develop_text import nested_bash_fences


@pytest.fixture
def output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the batch artifact in the standalone run directory."""
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / ".developments"
    run_dir.mkdir()
    return run_dir / "batch.json"


@pytest.mark.parametrize(
    "file_name",
    ["src/pkg/__init__.py", "./src/pkg/__init__.py", "src\\pkg\\__init__.py"],
)
@mock.patch("build_codemap_batch._git_diff_files", autospec=True)
def test_scoped_package_initializer_queries_package(mock_diff, output: Path, file_name: str) -> None:
    """Every supported separator spelling maps an initializer to its package without reading the diff."""
    assert build_codemap_batch.main([str(output), "--files", file_name, "--queries", "rdeps"]) == 0
    assert json.loads(output.read_text()) == [{"cmd": "rdeps", "args": ["pkg"]}]
    mock_diff.assert_not_called()


def test_absolute_scope_is_relative_to_repository(output: Path) -> None:
    """Absolute paths keep the module after removing the explicit repository and source roots."""
    repository = output.parent.parent
    source = str(repository / "src/pkg/mod.py")
    assert (
        build_codemap_batch.main(
            [str(output), "--files", source, "--repository", str(repository), "--queries", "rdeps"]
        )
        == 0
    )
    assert json.loads(output.read_text()) == [{"cmd": "rdeps", "args": ["pkg.mod"]}]


def test_scope_drops_invalid_paths_with_warning(output: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Outside paths and unrepresentable module names cannot silently erase caller coverage."""
    files = "src/pkg/mod.py\n../outside.py\nnot-python.txt\nsrc/bad-name.py\nsrc/pkg/mod.py"
    assert build_codemap_batch.main([str(output), "--files", files, "--queries", "rdeps"]) == 0
    assert json.loads(output.read_text()) == [{"cmd": "rdeps", "args": ["pkg.mod"]}]
    captured = capsys.readouterr()
    assert captured.out == "pkg.mod\n"
    assert "../outside.py" in captured.err
    assert "not-python.txt" in captured.err
    assert "src/bad-name.py" in captured.err


def test_empty_explicit_scope_does_not_fall_back_to_diff(output: Path) -> None:
    """An empty requested scope produces no caller requests instead of querying unrelated changes."""
    assert build_codemap_batch.main([str(output), "--files", "", "--queries", "rdeps"]) == 0
    assert json.loads(output.read_text()) == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")
def test_refactor_subdirectory_scope_keeps_the_package_module(tmp_path: Path) -> None:
    """The shipped file enumeration preserves repository coordinates from a nested invocation directory."""
    source = tmp_path / "src/pkg/module.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    skill = Path(__file__).resolve().parents[2] / "skills/refactor/SKILL.md"
    block = next(block for block in nested_bash_fences(skill.read_text()) if "REFACTOR_FILES=" in block)
    enumeration = next(line for line in block.splitlines() if line.startswith("REFACTOR_FILES="))
    result = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            enumeration.replace("<target>", "module.py") + '\nprintf "%s\\n" "$REFACTOR_FILES"',
        ],
        cwd=source.parent,
        env={**os.environ, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert build_codemap_batch.derive_scoped_modules(result.stdout, tmp_path) == ["pkg.module"]
