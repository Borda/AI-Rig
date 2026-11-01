"""Tests for ``bin/check_iteration_scope.py`` — research:run Phase 3 scope and evaluator check.

Each test copies a committed throwaway git repository holding a program file, so the check runs against real ``git
status`` output rather than a canned porcelain string.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import check_iteration_scope as scope
import pytest

# maintenance.auto=false: background maintenance's lock file appears in .git/objects while the baseline repo is copied.
_GIT_ENV = [
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.com",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "maintenance.auto=false",
]

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


@pytest.fixture(scope="module")
def _baseline_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Immutable committed repository holding a program file, an evaluator, source and tests (built once)."""
    root = tmp_path_factory.mktemp("baseline")
    _git(root, "init", "-q")
    _write(root, "program.md", _PROGRAM)
    _write(root, "bench/measure.py")
    _write(root, "src/pkg/sort.py")
    _write(root, "tests/test_sort.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


@pytest.fixture
def repo(_baseline_repo: Path, tmp_path: Path) -> Path:
    """Private clean copy of the baseline repository plus a run state pointing at its program file."""
    work = tmp_path / "work"
    shutil.copytree(_baseline_repo, work)
    state = work / ".experiments" / "state" / "r1" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"program_file": str(work / "program.md")}), encoding="utf-8")
    return work


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    """Run the check against ``repo`` and return its exit code and parsed JSON line."""
    code = scope.main(["--state-json", str(repo / ".experiments/state/r1/state.json"), "--root", str(repo)])
    return code, json.loads(capsys.readouterr().out)


class TestScopeCheck:
    """Scope and evaluator check run after each research:run iteration."""

    def test_in_scope_edits_pass(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A source edit plus a new test file inside ``tests/`` is allowed; run artifacts are ignored.

        The program scopes ``src/**/*.py`` and ``tests/``; the diary under ``.experiments/`` is bookkeeping, so none of
        the three touched paths may be reported as a violation.
        """
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
    def test_violation_exits_3(self, repo: Path, capsys: pytest.CaptureFixture[str], rel: str) -> None:
        """Edits to the metric script or files outside ``scope_files`` are violations.

        Editing the evaluator would let an iteration game its own metric, and a file outside the declared scope (or one
        the ``src/**/*.py`` glob misses) breaks the contract the program was approved under.
        """
        _write(repo, rel, "changed\n")

        code, result = _run(repo, capsys)

        assert code == 3
        assert result["violations"] == [rel]

    def test_text_goal_run_is_unchecked(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A text-goal run (``program_file`` null) reports changes but cannot check scope.

        With no program file there is no declared scope to enforce, so the check lists what changed and exits 0.
        """
        (repo / ".experiments/state/r1/state.json").write_text(json.dumps({"program_file": None}), encoding="utf-8")
        _write(repo, "README.md", "changed\n")

        code, result = _run(repo, capsys)

        assert code == 0
        assert result["checked"] is False
        assert result["changed"] == ["README.md"]

    def test_unreadable_state_blocks(self, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unreadable state file fails closed with exit 1.

        Without the state there is no way to know the scope, so the check must block rather than wave the edit through.
        """
        code = scope.main(["--state-json", str(repo / "missing.json"), "--root", str(repo)])

        assert code == 1
        assert "BLOCKED" in capsys.readouterr().err
