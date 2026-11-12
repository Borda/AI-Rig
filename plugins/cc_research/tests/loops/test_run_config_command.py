"""Configured command execution preserves shell behavior and enforces bounded metric runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import run_config_command as runner

_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "run_config_command.py"


def _run(tmp_path: Path, config: object, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Execute the real runner with a private configuration and working directory."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"config": config}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--state-json", str(state), *arguments],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_metric_preserves_pipeline_output_and_nonzero_status(tmp_path: Path) -> None:
    """A metric pipeline can print a value without hiding its nonzero command exit."""
    result = _run(tmp_path, {"metric_cmd": "printf '1.25\\n' | cat; exit 7"}, "--kind", "metric")

    assert result.returncode == 7
    assert result.stdout == "1.25\n"


def test_guard_executes_from_inherited_worktree(tmp_path: Path) -> None:
    """Relative commands resolve within the caller's variant worktree."""
    (tmp_path / "marker").write_text("variant\n", encoding="utf-8")
    result = _run(tmp_path, {"guard_cmd": "cat marker"}, "--kind", "guard")

    assert result.returncode == 0
    assert result.stdout == "variant\n"


def test_command_override_is_shell_data_to_the_runner(tmp_path: Path) -> None:
    """The explicit override replaces the configured command without a host-shell expansion."""
    result = _run(
        tmp_path,
        {"metric_cmd": "exit 9"},
        "--kind",
        "metric",
        "--command-override",
        "printf 'override\\n'",
    )

    assert result.returncode == 0
    assert result.stdout == "override\n"


@pytest.mark.parametrize("kind", ["metric", "guard"])
def test_legacy_state_executes_explicit_override(tmp_path: Path, kind: str) -> None:
    """A completed legacy run without configuration retains its explicit command recovery path."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--state-json",
            str(state),
            "--kind",
            kind,
            "--command-override",
            "printf 'legacy override\\n'; exit 7",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 7
    assert result.stdout == "legacy override\n"
    assert result.stderr == ""


@pytest.mark.parametrize("kind", ["metric", "guard"])
def test_legacy_state_without_override_is_rejected(tmp_path: Path, kind: str) -> None:
    """An absent legacy configuration alone does not authorize a default command."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--state-json", str(state), "--kind", kind],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "state.config must be an object" in result.stderr


@pytest.mark.parametrize("config", [None, pytest.param([], id="list"), "invalid"])
@pytest.mark.parametrize("kind", ["metric", "guard"])
def test_malformed_config_with_override_is_rejected(tmp_path: Path, config: object, kind: str) -> None:
    """Explicit overrides do not excuse null or malformed configuration values."""
    result = _run(tmp_path, config, "--kind", kind, "--command-override", "printf 'must not run'")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "state.config must be an object" in result.stderr


@pytest.mark.parametrize("timeout", [0, -1, True, "2", 1.5])
def test_invalid_metric_timeout_is_rejected(tmp_path: Path, timeout: object) -> None:
    """Invalid configured timeouts cannot silently disable the execution bound."""
    result = _run(tmp_path, {"metric_cmd": "printf 'ran'", "metric_timeout_sec": timeout}, "--kind", "metric")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "positive integer" in result.stderr


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="host lacks process-group termination")
def test_metric_timeout_returns_124_and_stops_child(tmp_path: Path) -> None:
    """A process-group timeout prevents a background child from producing later output."""
    result = _run(
        tmp_path,
        {"metric_cmd": "(sleep 4; printf 'survived') & wait", "metric_timeout_sec": 1},
        "--kind",
        "metric",
    )

    assert result.returncode == 124
    assert "timed out after 1 seconds" in result.stderr
    assert result.stdout == ""


def test_timeout_environment_override_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed environment override is an error rather than an unbounded or default run."""
    monkeypatch.setenv("VERIFY_TIMEOUT_SEC", "invalid")
    result = _run(tmp_path, {"metric_cmd": "printf 'ran'"}, "--kind", "metric")

    assert result.returncode == 2
    assert "positive integer" in result.stderr


def test_guard_does_not_use_metric_timeout_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Metric-specific environment configuration does not invalidate the guard's established timeout."""
    monkeypatch.setenv("VERIFY_TIMEOUT_SEC", "invalid")
    result = _run(tmp_path, {"guard_cmd": "printf 'guard'"}, "--kind", "guard")

    assert result.returncode == 0
    assert result.stdout == "guard"


def test_missing_command_is_rejected(tmp_path: Path) -> None:
    """A missing configured command fails before spawning Bash."""
    result = _run(tmp_path, {}, "--kind", "metric")

    assert result.returncode == 2
    assert "metric_cmd" in result.stderr


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param({}, 120, id="local-default"),
        pytest.param({"compute": "colab"}, 300, id="colab-default"),
        pytest.param({"metric_timeout_sec": 180}, 180, id="program-timeout"),
    ],
)
def test_metric_timeout_uses_route_default_or_program_setting(
    config: dict, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One configured timeout replaces the route default across execution stages."""
    monkeypatch.delenv("VERIFY_TIMEOUT_SEC", raising=False)

    assert runner.command_timeout(config, "metric") == expected


def test_positive_timeout_environment_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit positive environment limit overrides the program value."""
    monkeypatch.setenv("VERIFY_TIMEOUT_SEC", "15")

    assert runner.command_timeout({"metric_timeout_sec": 180}, "metric") == 15


@mock.patch("run_config_command.subprocess.Popen", autospec=True)
def test_timeout_without_process_groups_kills_direct_child(
    popen: mock.MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented fallback terminates its direct child when process groups are unavailable."""
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runner.shutil, "which", lambda executable: "resolved-bash")
    popen.return_value.wait.side_effect = [subprocess.TimeoutExpired("command", 1), -9]

    assert runner.execute("command", 1) == 124
    popen.assert_called_once_with(["resolved-bash", "-c", "command"], start_new_session=False)
    popen.return_value.kill.assert_called_once_with()
    assert "timed out after 1 seconds" in capsys.readouterr().err


@mock.patch("run_config_command.subprocess.Popen", autospec=True)
def test_timeout_cleanup_wait_is_bounded(
    popen: mock.MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unconfirmed process cleanup emits a diagnostic and cannot hang the runner indefinitely."""
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runner.shutil, "which", lambda executable: "resolved-bash")
    popen.return_value.wait.side_effect = subprocess.TimeoutExpired("command", 1)

    assert runner.execute("command", 1) == 124
    assert popen.return_value.wait.call_args_list == [mock.call(timeout=1), mock.call(timeout=5)]
    assert "cleanup did not confirm termination within 5 seconds" in capsys.readouterr().err
