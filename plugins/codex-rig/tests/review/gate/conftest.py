"""Fixtures shared by the gate runner acceptance modules.

They provide a committed repository and its head, the run output location, a sealed but uninstalled lint retry and an
independent directory outside the run output. A module-level fixture of the same name still takes precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _gate_runner_support import (
    RUN_GATES,
    _git,
    _initialize_repository,
    _interrupt_before_install,
    _load_run_gates,
    _PendingPublication,
    _python_command,
    _run_lint_gate,
    _skip_gates,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a clean committed repository at ``tmp_path/repository``."""
    path = tmp_path / "repository"
    path.mkdir()
    _initialize_repository(path)
    return path


@pytest.fixture
def head(repository: Path) -> str:
    """Return the commit id at the tip of the ``repository`` fixture."""
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def output(tmp_path: Path) -> Path:
    """Return where a gate run writes its receipts; the runner creates the directory."""
    return tmp_path / "gates"


@pytest.fixture
def pending_lint_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _PendingPublication:
    """Publish one lint attempt, then seal a second attempt whose installation never started."""
    output = tmp_path / "review"
    assert _run_lint_gate(tmp_path, output, None, _python_command("print('first')")).returncode == 0
    prior = (output / "gates.json").read_bytes()
    gates = _load_run_gates(monkeypatch)
    argv = [str(RUN_GATES), "--out", str(output), "--lint", _python_command("print('second')"), *_skip_gates("lint")]
    stage = _interrupt_before_install(gates, monkeypatch, output, argv)
    return _PendingPublication(gates, output, stage, prior)


@pytest.fixture
def outside_directory(tmp_path: Path) -> Path:
    """Return a directory outside the run output holding only an independent sentinel file."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"independent")
    return outside
