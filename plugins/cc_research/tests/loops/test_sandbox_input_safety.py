"""Sandbox input checks preserve a finite deadline and the intended writable mount."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import docker_sandbox_run as ds
import pytest


def _symlinks_supported() -> bool:
    """Probe whether the host permits directory symlinks without elevated privileges."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "link").symlink_to(Path(tmp) / "target", target_is_directory=True)
    except OSError:
        return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create directory symlinks")


@pytest.mark.parametrize("raw", ["inf", "1e999", "86401", "1e300"])
@mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
def test_timeout_override_keeps_a_bounded_deadline(mock_run: mock.MagicMock, tmp_path: Path, raw: str) -> None:
    """Infinite or excessive overrides fall back to the existing ten-minute deadline."""
    mock_run.return_value = subprocess.CompletedProcess([], 0)

    assert ds.main(["--mode", "explore", "metric.py"], env={"SANDBOX_TIMEOUT_SEC": raw}, cwd=str(tmp_path)) == 0

    assert mock_run.call_args.kwargs["timeout"] == 600.0


def test_timeout_accepts_the_maximum_finite_deadline() -> None:
    """A finite one-day override remains available for long-running verification."""
    assert ds._resolve_timeout("86400") == 86400.0


@_skip_no_symlink
@mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
def test_verify_mounts_the_resolved_experiments_directory(mock_run: mock.MagicMock, tmp_path: Path) -> None:
    """A supported experiments symlink binds its resolved directory as the writable mount."""
    mock_run.return_value = subprocess.CompletedProcess([], 0)
    target = tmp_path / "outside"
    target.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".experiments").symlink_to(target, target_is_directory=True)

    assert ds.main(["--mode", "verify", "pytest -q"], env={}, cwd=str(project)) == 0
    assert ds.bind_mount(target.resolve(), "/workspace/.experiments", readonly=False) in mock_run.call_args.args[0]


@mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
def test_verify_rejects_a_regular_file_experiments_mount(
    mock_run: mock.MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file in place of the writable directory gives an actionable exit without traceback."""
    (tmp_path / ".experiments").write_text("retain me", encoding="utf-8")

    assert ds.main(["--mode", "verify", "pytest -q"], env={}, cwd=str(tmp_path)) == 2

    mock_run.assert_not_called()
    assert ".experiments" in capsys.readouterr().err
    assert (tmp_path / ".experiments").read_text(encoding="utf-8") == "retain me"


@_skip_no_symlink
def test_verify_builder_rejects_a_symlinked_experiments_mount(tmp_path: Path) -> None:
    """The argv builder itself cannot render a writable bind mount through a symlink."""
    (tmp_path / ".experiments").symlink_to(tmp_path / "absent", target_is_directory=True)

    with pytest.raises(ValueError, match=r"\.experiments"):
        ds.build_verify_command("pytest -q", "none", str(tmp_path))


@pytest.mark.parametrize("mode", ["explore", "verify"])
@mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
def test_bridge_network_warns_about_readable_project_secrets(
    mock_run: mock.MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    """Bridge opt-in names outbound access and the secret-bearing project files the container can read."""
    mock_run.return_value = subprocess.CompletedProcess([], 0)

    assert ds.main(["--mode", mode, "metric.py"], env={"SANDBOX_NETWORK": "bridge"}, cwd=str(tmp_path)) == 0

    warning = capsys.readouterr().err
    assert "warning" in warning
    assert "outbound" in warning
    assert ".env" in warning
    assert ".git/config" in warning
    assert "read" in warning


@pytest.mark.parametrize("command", ["'r''m' -rf data", '"r"m -rf data', "/bin/'r''m' -rf data"])
@mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
def test_verify_rejects_shell_concatenated_destructive_commands(
    mock_run: mock.MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """The filter sees the destructive command name that the container shell actually receives."""
    assert ds.main(["--mode", "verify", command], env={}, cwd=str(tmp_path)) == 2

    mock_run.assert_not_called()
    assert "destructive binaries" in capsys.readouterr().err


@mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
def test_verify_rejects_unterminated_shell_quoting(
    mock_run: mock.MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed shell quoting produces a clean argument error before Docker runs."""
    assert ds.main(["--mode", "verify", "pytest 'metric.py"], env={}, cwd=str(tmp_path)) == 2

    mock_run.assert_not_called()
    assert "quoting" in capsys.readouterr().err
