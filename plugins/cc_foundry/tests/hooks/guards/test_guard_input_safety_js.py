"""Exercise hook guards against ambiguous text, failed inspection, and filesystem aliases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _symlinks_supported

HOOKS = Path(__file__).resolve().parents[3] / "hooks"
_node_required = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node.js")
_symlinks_required = pytest.mark.skipif(not _symlinks_supported(), reason="requires symbolic links")


def _guard(hook: Path, tool: str, inputs: dict, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a real guard with one isolated tool call."""
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inputs, "cwd": str(cwd)}
    return subprocess.run(
        ["node", str(hook)],
        input=json.dumps(payload),
        cwd=cwd,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent), "TMPDIR": str(cwd)},
        capture_output=True,
        encoding="utf-8",
        timeout=5,
        check=False,
    )


@_node_required
@pytest.mark.parametrize(
    "command",
    [
        'rg "(.*)" src',
        'printf "(.*)"',
        'echo "approval-.*"',
        "echo plugins/cc_foundry/hooks/*.js",
    ],
)
def test_read_and_print_patterns_are_not_write_targets(tmp_path: Path, command: str) -> None:
    """Regex text and printed globs do not execute a writer or write a git directory."""
    result = _guard(HOOKS / "approval-guard.js", "Bash", {"command": command}, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@_node_required
@pytest.mark.parametrize(
    "command",
    [
        "node plugins/cc_foundry/hooks/*.js < payload.json",
        "cp payload.json .gi?",
        "printf x 12>>.git/claude-git-approval.json",
        "printf x 9<>.git/claude-git-approval.json",
        "printf x &>.git/claude-git-approval.json",
        "printf x &>>.git/claude-git-approval.json",
        "printf x >|.git/claude-git-approval.json",
        r"printf x >.\g\i?",
        "python3 -c \"print('.git')\"",
        "node -e \"console.log('.git')\"",
    ],
)
def test_writer_operands_and_redirects_remain_blocked(tmp_path: Path, command: str) -> None:
    """Executable globs, write destinations, and fd-prefixed redirects still deny self-grants."""
    result = _guard(HOOKS / "approval-guard.js", "Bash", {"command": command}, tmp_path)
    assert result.returncode == 2
    assert "approval record guard" in result.stderr


@_node_required
def test_long_numeric_argument_finishes_without_redirect_backtracking(tmp_path: Path) -> None:
    """A numeric argument without a redirect finishes within the subprocess deadline."""
    result = _guard(HOOKS / "approval-guard.js", "Bash", {"command": "echo " + "1" * 200_000}, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@_node_required
@pytest.mark.parametrize(
    ("tool", "inputs", "expected"),
    [
        pytest.param("Bash", {"command": "cp x .git/claude-git-approval.json"}, 2, id="record-command"),
        pytest.param("Write", {"file_path": ".git/claude-push-approval.json"}, 2, id="record-file"),
        pytest.param("Bash", {"command": "ls src"}, 0, id="unrelated-command"),
    ],
)
def test_inspection_exception_fails_closed_on_record_names(
    tmp_path: Path, tool: str, inputs: dict, expected: int
) -> None:
    """A broken inspection module cannot turn an explicit approval-record write into a pass."""
    hook = tmp_path / "approval-guard.js"
    hook.write_bytes((HOOKS / hook.name).read_bytes())
    library = tmp_path / "lib" / "approval-grants.js"
    library.parent.mkdir()
    library.write_text(
        'exports.selfGrantProblem = () => { throw new Error("inspection failed"); };\n', encoding="utf-8"
    )
    result = _guard(hook, tool, inputs, tmp_path)
    assert result.returncode == expected


@_node_required
def test_multiedit_of_untracked_instructions_asks(tmp_path: Path) -> None:
    """Legacy multi-edit writes retain the same protection as ordinary edits."""
    result = _guard(HOOKS / "write-guard.js", "MultiEdit", {"file_path": "CLAUDE.md", "edits": []}, tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@_node_required
@_symlinks_required
@pytest.mark.parametrize("target", ["CLAUDE.md", ".claude/settings.local.json"])
def test_alias_to_untracked_protected_file_asks(tmp_path: Path, target: str) -> None:
    """An innocuous alias cannot hide the protected name of its existing destination."""
    destination = tmp_path / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("original\n", encoding="utf-8")
    alias = tmp_path / "notes.txt"
    alias.symlink_to(destination)
    result = _guard(HOOKS / "write-guard.js", "Edit", {"file_path": str(alias)}, tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@_node_required
@_symlinks_required
def test_dangling_alias_to_protected_file_asks(tmp_path: Path) -> None:
    """An alias targeting a missing protected file cannot bypass the first creation prompt."""
    alias = tmp_path / "notes.txt"
    alias.symlink_to(tmp_path / "CLAUDE.md")
    result = _guard(HOOKS / "write-guard.js", "Write", {"file_path": str(alias)}, tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@_node_required
@_symlinks_required
def test_creation_under_directory_alias_asks(tmp_path: Path) -> None:
    """A new file below a directory alias inherits the protected path of its real parent."""
    protected = tmp_path / ".claude"
    protected.mkdir()
    alias = tmp_path / "config"
    alias.symlink_to(protected, target_is_directory=True)
    result = _guard(HOOKS / "write-guard.js", "Write", {"file_path": str(alias / "settings.local.json")}, tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
