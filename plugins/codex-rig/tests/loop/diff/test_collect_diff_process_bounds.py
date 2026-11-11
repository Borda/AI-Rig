"""Bound Git processes used for local diff evidence and review mirrors."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module_registered

HELPER = Path(__file__).resolve().parents[3] / "shared" / "collect_diff.py"


@pytest.fixture
def collector() -> ModuleType:
    """Load the standalone local evidence collector."""
    return load_module_registered("collect_diff_bounds", HELPER)


@pytest.mark.parametrize(
    ("operation", "argument", "expected"),
    [
        pytest.param("_git_output", ("status", "--short"), b"evidence", id="snapshot"),
        pytest.param("_output_is_ignored", "report.json", True, id="ignored"),
    ],
)
def test_git_processes_disable_fsmonitor_and_set_timeout(
    collector: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    argument: tuple[str, ...] | str,
    expected: bytes | bool,
) -> None:
    """Pass process-level protections to each direct read boundary."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Record the external process contract without launching Git."""
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, b"evidence", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert getattr(collector, operation)(tmp_path, argument) == expected
    assert len(calls) == 1
    assert calls[0][0][:3] == ["git", "-c", "core.fsmonitor=false"]
    assert 0 < calls[0][1]["timeout"] <= 60


@pytest.mark.parametrize(
    ("operation", "argument", "message"),
    [
        pytest.param("_git_output", ("status", "--short"), "Git source snapshot command timed out", id="snapshot-read"),
        pytest.param("_output_is_ignored", "report.json", "Git ignored-path check timed out", id="ignored-path"),
    ],
)
def test_git_read_timeout_becomes_stable_failure(
    collector: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    argument: tuple[str, ...] | str,
    message: str,
) -> None:
    """Expose bounded read failure without leaking a subprocess traceback to the CLI."""

    def timeout(arguments: list[str], **kwargs: Any) -> None:
        """Simulate Git exceeding its subprocess deadline."""
        raise subprocess.TimeoutExpired(arguments, 60)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match=message):
        getattr(collector, operation)(tmp_path, argument)


def test_artifact_timeout_returns_failing_status(
    collector: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timed-out artifact command returns a stable nonzero status with a diagnostic."""

    def timeout(arguments: list[str], **kwargs: Any) -> None:
        """Simulate a subprocess deadline on a local artifact read."""
        assert arguments[:3] == ["git", "-c", "core.fsmonitor=false"]
        assert 0 < kwargs["timeout"] <= 60
        raise subprocess.TimeoutExpired(arguments, 60)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert collector.run_git(("status", "--short"), tmp_path / "status.txt") == 124
    assert "Git command timed out" in capsys.readouterr().err


@pytest.fixture
def changed_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create real changed source with isolated Git configuration."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repository = tmp_path / "source"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True)
    (repository / "source.txt").write_bytes(b"before\n")
    subprocess.run(["git", "-C", str(repository), "add", "source.txt"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    (repository / "source.txt").write_bytes(b"after\n")
    return repository


@pytest.mark.integration
def test_review_mirror_bounds_every_git_process(
    collector: ModuleType, changed_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protect snapshot, gitlink, worktree creation and patch application in the real mirror route."""
    original_run = subprocess.run
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Check process protections while preserving real Git behavior."""
        assert arguments[:3] == ["git", "-c", "core.fsmonitor=false"]
        assert 0 < kwargs["timeout"] <= 60
        calls.append(arguments)
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    collector.collect_review_worktree(changed_repository, tmp_path / "report")
    assert any("worktree" in arguments for arguments in calls)
    assert any("apply" in arguments for arguments in calls)
    assert (tmp_path / "report-review-worktree" / "source.txt").read_bytes() == b"after\n"


@pytest.mark.integration
@pytest.mark.parametrize("command", ["worktree", "apply"])
def test_review_mirror_timeout_becomes_stable_failure(
    collector: ModuleType, changed_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Turn a timed-out mirror creation or apply into a recorded runtime failure."""
    original_run = subprocess.run

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Inject a deadline only at the selected real process boundary."""
        if command in arguments:
            raise subprocess.TimeoutExpired(arguments, 60)
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match=r"Git .* timed out"):
        collector.collect_review_worktree(changed_repository, tmp_path / "report")
