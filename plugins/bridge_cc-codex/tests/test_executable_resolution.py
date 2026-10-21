"""Exercise executable lookup without Windows current-directory search on every host."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402
import bridge_diagnose  # noqa: E402
import bridge_setup  # noqa: E402


@pytest.mark.parametrize("name", ["claude", "codex", "git", "taskkill", "tasklist"])
def test_windows_lookup_ignores_planted_current_directory_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Use only PATH directories even when cwd and PATH contain implicit cwd entries."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (tmp_path / f"{name}.EXE").touch()
    expected = trusted / f"{name}.EXE"
    expected.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_call.sys, "platform", "win32")
    monkeypatch.setenv("PATH", f';.;.\\;"{trusted}";;')
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")

    assert bridge_call._resolve_executable(name) == str(expected)
    assert bridge_call._resolved_command([name, "--version"]) == [str(expected), "--version"]


@pytest.mark.parametrize("name", ["codex", "codex.CMD"])
def test_windows_lookup_applies_pathext_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Resolve npm shims and preserve an already supplied executable extension."""
    (tmp_path / "codex.EXE").touch()
    expected = tmp_path / "codex.CMD"
    expected.touch()
    monkeypatch.setattr(bridge_call.sys, "platform", "win32")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    assert bridge_call._resolve_executable(name) == str(expected)


def test_missing_windows_tool_never_falls_back_to_a_bare_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cwd-only peer remains missing and retains the OS missing-file diagnostic."""
    (tmp_path / "claude.EXE").touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_call.sys, "platform", "win32")
    monkeypatch.setenv("PATH", ";.;.\\;")
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("missing peer reached subprocess"))

    assert bridge_call._resolve_executable("claude") is None
    with pytest.raises(FileNotFoundError) as error:
        bridge_call._resolved_command(["claude", "--version"])
    assert str(error.value) == str(FileNotFoundError(2, os.strerror(2), "claude"))
    assert bridge_setup._inspect("codex", "claude", "user") == (None, "missing-prerequisite")
    assert bridge_setup._capture(["claude", "--version"]) == (False, "", "probe-failed")
    result = bridge_diagnose._static_result("claude", {"required": ["--help"]})
    assert result["ok"] is False
    assert result["error"] == str(error.value)


def test_windows_setup_and_diagnosis_launch_only_the_path_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every setup probe, mutation, login and static doctor uses the absolute peer path."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (tmp_path / "claude.EXE").touch()
    peer = trusted / "claude.CMD"
    peer.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_call.sys, "platform", "win32")
    monkeypatch.setenv("PATH", f";.;{trusted}")
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")
    calls: list[list[str]] = []

    def capture(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record the exact executable sent to the OS without launching a peer."""
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"loggedIn": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", capture)
    assert bridge_setup._capture(["claude", "--version"])[0] is True
    assert bridge_setup._authenticated("claude") == (True, True)
    assert bridge_setup._run_configuration([{"argv": ["claude", "plugin", "list"]}]) is True
    assert bridge_setup._launch_authentication("claude").ok is True
    assert bridge_diagnose._static_result("claude", {"required": []})["ok"] is True
    assert len(calls) == 5
    assert all(argv[0] == str(peer) for argv in calls)


def test_windows_peer_spawn_uses_the_path_peer_with_child_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve before Popen receives provider credentials and an untrusted working directory."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    (tmp_path / "claude.EXE").touch()
    peer = trusted / "claude.EXE"
    peer.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_call.sys, "platform", "win32")
    monkeypatch.setenv("PATH", f";.;{trusted}")
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-provider-key")
    launches: list[tuple[list[str], dict[str, object]]] = []

    def capture(argv: list[str], **kwargs: object) -> SimpleNamespace:
        """Capture spawn arguments and emulate a completed adapter without output pipes."""
        launches.append((argv, kwargs))
        return SimpleNamespace(returncode=0, communicate=lambda timeout: ("", ""))

    monkeypatch.setattr(subprocess, "Popen", capture)
    result = bridge_call._spawn_child(["claude", "--version"], tmp_path, 1.0)
    assert result.returncode == 0
    assert launches[0][0] == [str(peer), "--version"]
    assert launches[0][1]["cwd"] == tmp_path
    assert launches[0][1]["env"]["ANTHROPIC_API_KEY"] == "test-provider-key"


def test_posix_lookup_returns_absolute_executable_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative PATH entries become absolute and non-executable files are ignored."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    executable = trusted / "peer"
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_call.sys, "platform", "linux")
    monkeypatch.setenv("PATH", f"{os.pathsep}.{os.pathsep}trusted")

    with monkeypatch.context() as posix:
        posix.setattr(bridge_call.os, "name", "posix")
        assert bridge_call._resolve_executable("peer") == str(executable)
        posix.setattr(bridge_call.os, "access", lambda path, mode: False)
        assert bridge_call._resolve_executable("peer") is None
