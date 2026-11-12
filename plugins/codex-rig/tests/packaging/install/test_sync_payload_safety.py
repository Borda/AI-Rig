"""Verify marketplace helper bytes and Windows command resolution before execution."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _bridge_fixture(root: Path) -> tuple[Path, str]:
    """Write one bridge file and return its independent Git blob identity."""
    doctor = root / "plugins" / "bridge_cc-codex" / "bin" / "bridge_diagnose.py"
    doctor.parent.mkdir(parents=True)
    payload = b"# verified helper\n"
    doctor.write_bytes(payload)
    digest = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload, usedforsecurity=False).hexdigest()
    return doctor, digest


@pytest.mark.parametrize("tamper", ["source", "untracked", "bytecode"])
def test_bridge_rejects_unverified_payload_before_execution(tmp_path: Path, tamper: str) -> None:
    """Changed tracked files and extra importable bytes must stop the doctor before any execution."""
    sync = load_module("sync_bridge_payload_regression", SCRIPTS / "sync_codex.py")
    doctor, blob = _bridge_fixture(tmp_path)
    target = {
        "source": doctor,
        "untracked": doctor.parent / "helper.py",
        "bytecode": doctor.parent / "__pycache__" / "helper.pyc",
    }[tamper]
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"unverified bytes")
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a clean committed tree while recording any attempted execution."""
        calls.append(command)
        output = json_tree(blob) if "ls-tree" in command else "a" * 40 + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(sync.SyncError, match=r"Bridge.*verif"):
        sync._run_bridge_static_diagnosis(tmp_path, run, io.StringIO())

    assert all(command[0] == "git" for command in calls)


def json_tree(blob: str) -> str:
    """Return the Git tree protocol record for the independently hashed helper."""
    return f"100644 blob {blob}\tplugins/bridge_cc-codex/bin/bridge_diagnose.py\0"


@pytest.mark.parametrize("relative", ["__pycache__/x.pyc", "nested/__pycache__/x.pyc", "x.pyc", "nested/x.pyc"])
def test_strict_inventory_rejects_bytecode_files(tmp_path: Path, relative: str) -> None:
    """Strict inventories reject cache files and standalone bytecode at every payload depth."""
    sync = load_module("sync_strict_bytecode_inventory", SCRIPTS / "sync_codex.py")
    payload = tmp_path / relative
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"unverified bytecode")

    with pytest.raises(sync.PackageIdentityError, match="bytecode"):
        sync.inventory_package_files(
            tmp_path, excluded_parts=frozenset(), excluded_files=frozenset(), reject_bytecode=True
        )


@pytest.mark.parametrize("relative", ["__pycache__", "nested/__pycache__"])
def test_bridge_rejects_empty_cache_directory_before_execution(tmp_path: Path, relative: str) -> None:
    """Even an empty cache directory stops Bridge verification before the doctor runs."""
    sync = load_module("sync_empty_cache_regression", SCRIPTS / "sync_codex.py")
    doctor, blob = _bridge_fixture(tmp_path)
    (doctor.parent / relative).mkdir(parents=True)
    calls = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Report the verified source tree and record any attempted doctor execution."""
        calls.append(command)
        output = json_tree(blob) if "ls-tree" in command else "a" * 40 + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(sync.SyncError, match=r"Bridge.*verif"):
        sync._run_bridge_static_diagnosis(tmp_path, run, io.StringIO())

    assert all(command[0] == "git" for command in calls)


@pytest.mark.parametrize("relative", ["__pycache__/x.pyc", "nested/__pycache__/x.pyc"])
def test_default_inventory_accepts_plain_bytecode_cache(tmp_path: Path, relative: str) -> None:
    """Default inventories preserve verified sources while omitting plain bytecode caches."""
    sync = load_module("sync_tolerant_bytecode_inventory", SCRIPTS / "sync_codex.py")
    (tmp_path / "helper.py").write_bytes(b"value = 'source'\n")
    payload = tmp_path / relative
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"unverified bytecode")

    assert sync.inventory_package_files(tmp_path, excluded_parts=frozenset(), excluded_files=frozenset()) == (
        "helper.py",
    )


def test_bridge_uses_current_interpreter_and_reports_commit(tmp_path: Path) -> None:
    """Run only verified Bridge bytes with the current interpreter and bytecode writing disabled."""
    sync = load_module("sync_bridge_interpreter_regression", SCRIPTS / "sync_codex.py")
    doctor, blob = _bridge_fixture(tmp_path)
    calls = []
    output = io.StringIO()

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Serve the committed tree and one healthy static diagnosis."""
        calls.append(command)
        if "ls-tree" in command:
            payload = json_tree(blob)
        elif command[0] == "git":
            payload = "a" * 40 + "\n"
        else:
            payload = '{"ok":true,"live":false}'
        return subprocess.CompletedProcess(command, 0, payload, "")

    sync._run_bridge_static_diagnosis(tmp_path, run, output)

    assert calls[-1] == [sys.executable, "-B", str(doctor), "--direction", "claude"]
    assert "a" * 40 in output.getvalue()


def test_windows_commands_ignore_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve Codex, Git and Python only from PATH directories outside the current directory."""
    sync = load_module("sync_path_regression", SCRIPTS / "sync_codex.py")
    current = tmp_path / "current"
    trusted = tmp_path / "trusted"
    current.mkdir()
    trusted.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", os.pathsep.join(("", str(current), str(trusted))))
    monkeypatch.setattr(
        sync.shutil, "which", lambda command: command if Path(command).is_absolute() else str(current / command)
    )

    assert sync._resolve_system_command(["codex", "--version"], windows=True) == (
        [str(trusted / "codex"), "--version"],
        False,
    )
    assert sync._resolve_system_command(["git", "--version"], windows=True) == (
        [str(trusted / "git"), "--version"],
        False,
    )
    assert sync._resolve_system_command(["python", "--version"], windows=True) == (
        [str(trusted / "python"), "--version"],
        False,
    )
