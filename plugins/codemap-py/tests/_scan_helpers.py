"""Shared scan-index / scan-query / git subprocess and fixture-tree helpers for the codemap test suite.

Importable from every test module because ``conftest.py`` puts this directory on ``sys.path``. The helpers run the real
``bin/`` scripts as subprocesses, so tests that need a built index or a throwaway git repository share one definition
instead of repeating it per file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# The graph pool forks from runner threads started by pytest-rerunfailures or xdist.
# Apply only to tests that scan in-process, including scans in their fixtures.
IGNORE_RUNNER_THREAD_FORK_WARNING = pytest.mark.filterwarnings(
    r"ignore:This process \(pid=\d+\) is multi-threaded, use of fork\(\) may lead to deadlocks:DeprecationWarning"
)


def run_git(root: Path, *args: str) -> None:
    """Run a git command inside *root*, asserting success."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def init_repo(root: Path) -> None:
    """Initialise *root* as a git repository with a throwaway identity."""
    run_git(root, "init", "-q")
    run_git(root, "config", "user.email", "t@t.t")
    run_git(root, "config", "user.name", "t")


def index_path_for(root: Path) -> Path:
    """Return the codemap index path scan-index writes for *root*."""
    return root / ".cache" / "codemap" / f"{root.name}.json"


def run_scan(scan_index: Path, root: Path, *extra: str) -> None:
    """Run scan-index over *root*, asserting success."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr


def run_query(scan_query: Path, root: Path, index_path: Path, *args: str) -> dict:
    """Run scan-query against *index_path* under *root* and return the parsed JSON."""
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def write_tree(root: Path, files: dict[str, str]) -> None:
    """Write each ``relative path -> text`` entry under *root*, creating parent directories."""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Capture fixture-file bytes keyed by root-relative POSIX paths.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory); _ = (root / "a.txt").write_bytes(b"x")
    ...     sorted(tree_snapshot(root))
    ['a.txt']
    """
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
