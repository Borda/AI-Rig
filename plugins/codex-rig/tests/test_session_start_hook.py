"""Acceptance checks for the optional read-only SessionStart health hook."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from _platform import POSIX_FILE_MODES_AVAILABLE

_posix_doctor_only = pytest.mark.skipif(
    not POSIX_FILE_MODES_AVAILABLE,
    reason="filesystem does not preserve POSIX permission modes",
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOK_CONFIG = PLUGIN_ROOT / "hooks" / "hooks.json"
HOOK_SCRIPT = PLUGIN_ROOT / "hooks" / "session_start.py"


def _snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture bytes and mutation-relevant metadata while excluding atime."""
    rows = []
    for path in [root, *sorted(root.rglob("*"))]:
        metadata = path.lstat()
        rows.append(
            (
                str(path.relative_to(root)),
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_ino,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
            )
        )
    return tuple(rows)


def _hook_input() -> bytes:
    """Return one minimal valid SessionStart event."""
    return json.dumps(
        {
            "session_id": "fixture",
            "transcript_path": None,
            "cwd": "/fixture",
            "hook_event_name": "SessionStart",
            "model": "fixture",
            "permission_mode": "default",
            "source": "startup",
        }
    ).encode()


def test_default_hook_config_is_exact_and_diagnostic_only() -> None:
    """Declare one trusted-by-choice SessionStart command and no mutating event."""
    value = json.loads(HOOK_CONFIG.read_text())
    assert set(value) == {"description", "hooks"}
    assert set(value["hooks"]) == {"SessionStart"}
    group = value["hooks"]["SessionStart"][0]
    assert group["matcher"] == "startup|resume"
    assert group["hooks"] == [
        {
            "type": "command",
            "command": 'python3 "$PLUGIN_ROOT/hooks/session_start.py"',
            "commandWindows": 'python "$env:PLUGIN_ROOT\\hooks\\session_start.py"',
            "timeout": 30,
            "statusMessage": "Checking Codex Rig shim health",
        }
    ]
    plugin = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
    assert "hooks" not in plugin


def test_hook_reuses_manager_doctor_and_preserves_real_home(tmp_path: Path) -> None:
    """Surface degraded health without creating state in the real Codex home."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    codex = tmp_path / ("codex.cmd" if sys.platform == "win32" else "codex")
    codex.write_bytes(b"@exit /b 0\r\n" if sys.platform == "win32" else b"#!/bin/sh\nexit 0\n")
    codex.chmod(0o700)
    environment = os.environ.copy()
    environment["PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    environment["CODEX_HOME"] = str(home)
    environment["PATH"] = f"{tmp_path}{os.pathsep}{environment.get('PATH', '')}"
    before = _snapshot(tmp_path)

    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=_hook_input(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    assert result["continue"] is True
    assert "active_package: plugin root is not the selected cache-version path" in result["systemMessage"]
    assert "No files changed" in result["systemMessage"]
    assert "Run $codex-rig:agent-shims status" in result["systemMessage"]
    assert _snapshot(tmp_path) == before


@_posix_doctor_only
def test_hook_surfaces_one_bounded_block_reason(tmp_path: Path) -> None:
    """Explain the first failed invariant instead of repeating only blocked."""
    home = tmp_path / ("home-" + "x" * 180)
    home.mkdir(mode=0o700)
    agents = home / "agents"
    agents.mkdir(mode=0o700)
    agents.chmod(0o775)
    environment = os.environ.copy()
    environment["PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    environment["CODEX_HOME"] = str(home)
    environment["PATH"] = str(tmp_path)
    before = _snapshot(tmp_path)

    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=_hook_input(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    message = result["systemMessage"]
    assert "filesystem: unsafe protected directory mode" in message
    assert "observed 0775" in message
    assert "executables:" not in message
    assert "Codex executable was not found" not in message
    assert "No files changed" in message
    assert _snapshot(tmp_path) == before


def test_invalid_hook_input_fails_open_without_traceback(tmp_path: Path) -> None:
    """Keep session startup available when the diagnostic envelope is invalid."""
    environment = os.environ.copy()
    environment["PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=b"{}",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    assert result["continue"] is True
    assert "health check unavailable" in result["systemMessage"]
    assert "Run $codex-rig:agent-shims doctor" in result["systemMessage"]
