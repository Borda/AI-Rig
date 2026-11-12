"""Run registered hook commands at their permission and file-mode boundaries."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

PLUGIN = Path(__file__).resolve().parents[2]
_bash_required = pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires a working POSIX bash")
_node_required = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")


def _private_modes_supported() -> bool:
    """Probe whether this filesystem enforces POSIX owner-only file modes."""
    with tempfile.TemporaryDirectory() as directory:
        probe = Path(directory) / "mode"
        probe.touch()
        probe.chmod(0o600)
        return stat.S_IMODE(probe.stat().st_mode) == 0o600


def _registered_commands(event: str, tool: str, fragment: str) -> list[str]:
    """Select the commands the hook host dispatches for one tool and event."""
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    return [
        hook["command"]
        for entry in hooks.get(event, [])
        if re.fullmatch(entry.get("matcher", ".*"), tool)
        for hook in entry["hooks"]
        if fragment in hook.get("command", "")
    ]


def _dispatch_markdown(event: str, document: Path) -> list[subprocess.CompletedProcess[str]]:
    """Run the registered Markdown hook exactly as a shell-based host would."""
    payload = json.dumps(
        {"hook_event_name": event, "tool_name": "Edit", "tool_input": {"file_path": str(document), "old_string": "old"}}
    )
    return [
        subprocess.run(
            ["bash", "-c", command],
            input=payload,
            cwd=document.parent,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN)},
            capture_output=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        for command in _registered_commands(event, "Edit", "/md-compress.js")
    ]


@_bash_required
@_node_required
def test_markdown_registration_leaves_pending_edit_untouched(tmp_path: Path) -> None:
    """PreToolUse dispatch cannot normalize the pending edit or emit permission approval."""
    document = tmp_path / "report.md"
    original = "| key  | value  |\n"
    document.write_text(original, encoding="utf-8")
    results = _dispatch_markdown("PreToolUse", document)
    assert document.read_text(encoding="utf-8") == original
    assert results == []


@_bash_required
@_node_required
def test_markdown_registration_normalizes_only_completed_edit(tmp_path: Path) -> None:
    """PostToolUse dispatch normalizes an edited Markdown file without a permission envelope."""
    document = tmp_path / "report.md"
    document.write_text("| key  | value  |\n", encoding="utf-8")
    results = _dispatch_markdown("PostToolUse", document)
    assert len(results) == 1
    assert document.read_text(encoding="utf-8") == "| key | value |\n"
    assert results[0].stdout == ""


@_bash_required
@_node_required
def test_registered_multiedit_protects_untracked_instructions(tmp_path: Path) -> None:
    """Host dispatch reaches the legacy multi-edit guard before an untracked instruction write."""
    commands = _registered_commands("PreToolUse", "MultiEdit", "/write-guard.js")
    assert len(commands) == 1
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "MultiEdit", "tool_input": {"file_path": "CLAUDE.md"}}
    )
    result = subprocess.run(
        ["bash", "-c", commands[0]],
        input=payload,
        cwd=tmp_path,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@_bash_required
@pytest.mark.skipif(not _private_modes_supported(), reason="requires POSIX private file modes")
def test_subscription_status_is_created_private_under_permissive_umask(tmp_path: Path) -> None:
    """SessionStart account status remains owner-readable only even with umask 022."""
    executable = tmp_path / "bin" / "claude"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '{\"account\":\"private\"}'\n", encoding="utf-8")
    executable.chmod(0o700)
    commands = _registered_commands("SessionStart", "", "claude auth status")
    assert len(commands) == 1
    result = subprocess.run(
        ["bash", "-c", "umask 022\n" + commands[0]],
        env={**os.environ, "HOME": str(tmp_path), "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    status = tmp_path / ".claude" / "state" / "subscription.json"
    assert json.loads(status.read_text(encoding="utf-8")) == {"account": "private"}
    assert stat.S_IMODE(status.stat().st_mode) == 0o600
