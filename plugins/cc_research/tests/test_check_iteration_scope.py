"""Tests for ``bin/check_iteration_scope.py`` — research:run Phase 3 scope and evaluator check.

Each test builds a throwaway git repository with a program file, so the check runs against real ``git status`` output
rather than a canned porcelain string.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import check_iteration_scope as scope
import pytest

_GIT_ENV = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]

_PROGRAM = """# Program: speed up sort

## Metric

```
command: python bench/measure.py --n 1000
direction: lower
```

## Guard

```
command: python -m pytest tests/
```

## Config

```
scope_files:
  - src/**/*.py
  - tests/
```
"""


def _git(repo: Path, *args: str) -> None:
    """Run git in ``repo``."""
    subprocess.run(["git", *_GIT_ENV, *args], cwd=repo, capture_output=True, text=True, check=True)


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    """Create ``rel`` under ``repo`` with parent directories."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Clean repository holding a program file, an evaluator, source and tests."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "program.md", _PROGRAM)
    _write(tmp_path, "bench/measure.py")
    _write(tmp_path, "src/pkg/sort.py")
    _write(tmp_path, "tests/test_sort.py")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    state = tmp_path / ".experiments" / "state" / "r1" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"program_file": str(tmp_path / "program.md")}), encoding="utf-8")
    return tmp_path


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    """Run the check against ``repo`` and return its exit code and parsed JSON line."""
    code = scope.main(["--state-json", str(repo / ".experiments/state/r1/state.json"), "--root", str(repo)])
    return code, json.loads(capsys.readouterr().out)


def test_in_scope_edit_and_new_test_file_pass(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A source edit plus a new test file inside ``tests/`` is allowed; run artifacts are ignored."""
    _write(repo, "src/pkg/sort.py", "y\n")
    _write(repo, "tests/test_new.py")
    _write(repo, ".experiments/state/r1/diary.md")
    code, result = _run(repo, capsys)
    assert code == 0
    assert sorted(result["changed"]) == ["src/pkg/sort.py", "tests/test_new.py"]
    assert result["untracked"] == ["tests/test_new.py"]
    assert result["violations"] == []


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param("bench/measure.py", id="evaluator-edit"),
        pytest.param("README.md", id="out-of-scope-file"),
        pytest.param("src/notes.txt", id="glob-miss"),
    ],
)
def test_violation_exits_3(repo: Path, capsys: pytest.CaptureFixture[str], rel: str) -> None:
    """Edits to the metric script or files outside ``scope_files`` are violations."""
    _write(repo, rel, "changed\n")
    code, result = _run(repo, capsys)
    assert code == 3
    assert result["violations"] == [rel]


def test_no_program_file_is_unchecked(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A text-goal run (``program_file`` null) reports changes but cannot check scope."""
    (repo / ".experiments/state/r1/state.json").write_text(json.dumps({"program_file": None}), encoding="utf-8")
    _write(repo, "README.md", "changed\n")
    code, result = _run(repo, capsys)
    assert code == 0
    assert result["checked"] is False
    assert result["changed"] == ["README.md"]


def test_missing_state_json_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unreadable state file fails closed with exit 1."""
    _git(tmp_path, "init", "-q")
    assert scope.main(["--state-json", str(tmp_path / "missing.json"), "--root", str(tmp_path)]) == 1
    assert "BLOCKED" in capsys.readouterr().err
