"""Acceptance checks for the optional read-only SessionStart health hook.

Every behaviour test runs the packaged hook script in a real subprocess against a private plugin copy and a disposable
Codex home, because the hook's end-to-end output and its read-only guarantee are the contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _fixture_files import tree_snapshot
from _platform import POSIX_FILE_MODES_AVAILABLE

_posix_doctor_only = pytest.mark.skipif(
    not POSIX_FILE_MODES_AVAILABLE, reason="filesystem does not preserve POSIX permission modes"
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HOOK_CONFIG = PLUGIN_ROOT / "hooks" / "hooks.json"
HOOK_SCRIPT = PLUGIN_ROOT / "hooks" / "session_start.py"
#: Diagnostic the shim-health check emits for a plugin root outside the selected cache-version path.
_NOT_CACHE_PATH = "active_package: plugin root is not the selected cache-version path"


@pytest.fixture(scope="module")
def isolated_plugin_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy the plugin payload once so unrelated repo-tree writes cannot race package verification.

    The doctor subprocess opens no-follow directory handles and snapshots parent-directory identity before/after each
    package file read (see ``_safe_package_io.py``), failing closed on any mismatch. Pointing it at the live
    ``PLUGIN_ROOT`` exposes that guard to any concurrent write near the repo tree (another xdist worker, git, an editor,
    AV/indexer) during the scan, which raises ``SafePackageIOError`` and fails the assertion on the expected reason
    string. A private copy removes every writer but this fixture itself.
    """
    destination = tmp_path_factory.mktemp("codex-rig-plugin") / "codex-rig"
    shutil.copytree(PLUGIN_ROOT, destination)
    return destination


@pytest.fixture
def path_with_codex(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``PATH`` at ``tmp_path`` alone, holding a fake ``codex`` launcher when the indirect parameter is true."""
    monkeypatch.setenv("PATH", str(tmp_path))
    if request.param:
        _write_fake_codex(tmp_path)


def _write_fake_codex(directory: Path) -> Path:
    """Write an executable ``codex`` stub that exits 0, using the launcher form the host OS resolves."""
    codex = directory / ("codex.cmd" if sys.platform == "win32" else "codex")
    codex.write_bytes(b"@exit /b 0\r\n" if sys.platform == "win32" else b"#!/bin/sh\nexit 0\n")
    codex.chmod(0o700)
    return codex


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


def _run_hook(plugin_root: Path, home: Path, **environment: str) -> subprocess.CompletedProcess[bytes]:
    """Run the hook script of ``plugin_root`` against ``home`` with a valid event; ``environment`` overrides the env."""
    env = {**os.environ, "PLUGIN_ROOT": str(plugin_root), "CODEX_HOME": str(home), **environment}
    return subprocess.run(
        [sys.executable, str(plugin_root / "hooks" / "session_start.py")],
        input=_hook_input(),
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )


def _install_global_block(plugin_root: Path, home: Path, source: Path, *options: str) -> None:
    """Write the managed global block with the packaged installer, as sync or a direct re-render would."""
    home.mkdir(mode=0o700)
    command = [sys.executable, str(plugin_root / "scripts" / "install_global_agents.py"), "--source", str(source)]
    completed = subprocess.run(
        [*command, "--codex-home", str(home), *options], capture_output=True, text=True, check=False, timeout=30
    )
    assert completed.returncode == 0, completed.stderr


def _read_only_hook_message(home: Path, plugin_root: Path) -> str:
    """Run the hook from ``plugin_root`` against ``home`` and return its system message.

    Also asserts the read-only contract shared by every case: exit 0 without stderr, no change under ``home``, and no
    installer bytecode written into the plugin root while the hook loads the installer's parser.
    """
    shutil.rmtree(plugin_root / "scripts" / "__pycache__", ignore_errors=True)
    before = tree_snapshot(home)

    completed = _run_hook(plugin_root, home)

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert tree_snapshot(home) == before
    assert not list((plugin_root / "scripts").glob("__pycache__/install_global_agents.*"))
    return json.loads(completed.stdout).get("systemMessage", "")


def test_default_hook_config_is_exact_and_diagnostic_only() -> None:
    """Declare trusted-by-choice startup and one-shot review/remediation diagnostics only.

    The shipped ``hooks.json`` is compared byte-for-field: one SessionStart health check on startup/resume and one Stop
    check for unfinished review work, with the plugin manifest declaring no hooks of its own.
    """
    value = json.loads(HOOK_CONFIG.read_text())
    assert set(value) == {"description", "hooks"}
    assert set(value["hooks"]) == {"SessionStart", "Stop"}
    group = value["hooks"]["SessionStart"][0]
    assert group["matcher"] == "startup|resume"
    assert group["hooks"] == [
        {
            "type": "command",
            "command": '"$PLUGIN_ROOT/bin/python" "$PLUGIN_ROOT/hooks/session_start.py"',
            "commandWindows": '& "$env:PLUGIN_ROOT\\bin\\python.cmd" "$env:PLUGIN_ROOT\\hooks\\session_start.py"; exit $LASTEXITCODE',
            "timeout": 30,
            "statusMessage": "Checking Codex Rig shim health",
        }
    ]
    assert value["hooks"]["Stop"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": '"$PLUGIN_ROOT/bin/python" "$PLUGIN_ROOT/hooks/review_stop.py"',
                    "commandWindows": '& "$env:PLUGIN_ROOT\\bin\\python.cmd" "$env:PLUGIN_ROOT\\hooks\\review_stop.py"; exit $LASTEXITCODE',
                    "timeout": 5,
                    "statusMessage": "Checking unfinished review or remediation work",
                }
            ]
        }
    ]
    plugin = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
    assert "hooks" not in plugin


def test_hook_reuses_manager_doctor_and_preserves_real_home(tmp_path: Path, isolated_plugin_root: Path) -> None:
    """Surface degraded health without creating state in the real Codex home.

    A fake ``codex`` launcher is on ``PATH`` but the plugin root is not a cache-version path, so the manager doctor
    reports a degraded package; the hook must relay that in its system message and leave the filesystem untouched.
    """
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _write_fake_codex(tmp_path)
    before = tree_snapshot(tmp_path)

    completed = _run_hook(isolated_plugin_root, home, PATH=f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    assert completed.returncode == 0
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    assert result["continue"] is True
    assert _NOT_CACHE_PATH in result["systemMessage"]
    assert "No files changed" in result["systemMessage"]
    assert "Run $codex-rig:agent-shims status" in result["systemMessage"]
    assert tree_snapshot(tmp_path) == before


@_posix_doctor_only
def test_hook_surfaces_one_bounded_block_reason(tmp_path: Path, isolated_plugin_root: Path) -> None:
    """Explain the first failed invariant instead of repeating only blocked.

    A Codex home whose ``agents`` directory is group-writable fails the filesystem invariant first; the message must
    name that mode and stay bounded to it rather than also reporting the missing executable.
    """
    home = tmp_path / ("home-" + "x" * 180)
    home.mkdir(mode=0o700)
    agents = home / "agents"
    agents.mkdir(mode=0o700)
    agents.chmod(0o775)
    before = tree_snapshot(tmp_path)

    completed = _run_hook(isolated_plugin_root, home, PATH=str(tmp_path))

    assert completed.returncode == 0
    message = json.loads(completed.stdout)["systemMessage"]
    assert "filesystem: unsafe protected directory mode" in message
    assert "observed 0775" in message
    assert "executables:" not in message
    assert "Codex executable was not found" not in message
    assert "No files changed" in message
    assert tree_snapshot(tmp_path) == before


def test_hook_warns_when_global_block_names_previous_plugin_root(tmp_path: Path, isolated_plugin_root: Path) -> None:
    """Flag a global block a direct plugin update left on the previous version's root, naming no private path.

    ``codex plugin add`` installs a new versioned cache directory but never re-renders the block, so every packaged
    ``shared/<file>`` pointer keeps reading the old package (or a removed one) until the installer runs again.
    """
    home = tmp_path / "home"
    previous_root = tmp_path / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.33.1"
    template = isolated_plugin_root / "assets" / "AGENTS.md"
    _install_global_block(isolated_plugin_root, home, template, "--plugin-root", str(previous_root))

    message = _read_only_hook_message(home, isolated_plugin_root)

    assert (
        "stale PLUGIN_ROOT — the managed CODEX_HOME/AGENTS.md block names …/0.33.1, "
        f"not this session's …/{isolated_plugin_root.name}, so its shared/ pointers do not read this package"
    ) in message
    assert "No files changed" in message
    assert "install_global_agents.py --source <installed-package>/assets/AGENTS.md" in message
    assert str(tmp_path) not in message
    assert str(isolated_plugin_root) not in message


def test_hook_warns_when_global_block_predates_the_plugin_root_line(tmp_path: Path, isolated_plugin_root: Path) -> None:
    """Flag the block every 0.33.1 install carries after a direct update: it names no root at all.

    The template shipped beside the hook always defines ``PLUGIN_ROOT``, so a rootless block resolves none of its
    packaged ``shared/<file>`` pointers; a root-only comparison would stay silent on the most common upgrade.
    """
    home = tmp_path / "home"
    older_template = tmp_path / "older" / "AGENTS.md"
    older_template.parent.mkdir()
    current = (isolated_plugin_root / "assets" / "AGENTS.md").read_bytes()
    older_template.write_bytes(re.sub(rb"^.*\{\{CODEX_RIG_PLUGIN_ROOT\}\}.*\n", b"", current, flags=re.MULTILINE))
    _install_global_block(isolated_plugin_root, home, older_template)

    message = _read_only_hook_message(home, isolated_plugin_root)

    assert "block names no PLUGIN_ROOT (rendered from an older template)" in message
    assert "No files changed" in message
    assert str(tmp_path) not in message


@pytest.mark.parametrize(
    ("path_with_codex", "present", "absent"),
    [
        pytest.param(True, _NOT_CACHE_PATH, "executables:", id="codex-available"),
        pytest.param(
            False, "executables: Codex executable is unavailable on PATH", "active_package:", id="codex-missing"
        ),
    ],
    indirect=["path_with_codex"],
)
def test_hook_is_silent_when_global_block_names_its_own_root(
    tmp_path: Path, isolated_plugin_root: Path, path_with_codex: None, present: str, absent: str
) -> None:
    """Add no root warning when the block was rendered for the plugin root running the hook.

    Codex availability is controlled so the unrelated shim-health diagnostic is deterministic on development and CI
    hosts. Neither a non-cache fixture root nor a missing executable should produce a stale global-root warning.
    """
    home = tmp_path / "home"
    template = isolated_plugin_root / "assets" / "AGENTS.md"
    _install_global_block(isolated_plugin_root, home, template, "--plugin-root", str(isolated_plugin_root))

    message = _read_only_hook_message(home, isolated_plugin_root)

    assert "PLUGIN_ROOT" not in message
    assert present in message
    assert absent not in message


def test_invalid_hook_input_fails_open_without_traceback() -> None:
    """Keep session startup available when the diagnostic envelope is invalid.

    An empty JSON object is not a SessionStart event; the hook must still exit 0 with no traceback and tell the user the
    health check was unavailable, so a malformed host payload never blocks the session.
    """
    completed = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=b"{}",
        capture_output=True,
        env={**os.environ, "PLUGIN_ROOT": str(PLUGIN_ROOT)},
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    assert result["continue"] is True
    assert "health check unavailable" in result["systemMessage"]
    assert "Run $codex-rig:agent-shims doctor" in result["systemMessage"]
