"""Subprocess tests for ``hooks/rtk-rewrite.js``.

The hook is a ``PreToolUse`` gate that rewrites read-heavy Bash commands to
their ``rtk <cmd>`` equivalents and auto-approves them. Its security contract:

* **Read-only rewrite** — only commands the hook can prove read-only are
  rewritten with ``permissionDecision:"allow"``; everything else passes
  through unchanged (empty stdout, exit 0) so the real allow/deny matcher sees
  the original string.
* **No deny bypass** — because a rewrite mutates the command string (and thus
  dodges the settings.json deny list), mutating subcommands on mixed CLIs
  (``git push``, ``gh pr comment``, ``gh api -X POST``, ``docker rm`` …) must
  NEVER be rewritten. They passthrough to real permission checking.
* **No result corruption** — ``diff`` is never rewritten (rtk alters its exit
  status / "identical" summary).

These tests require ``rtk`` to be resolvable on ``PATH``; when it is not, the
hook is a deliberate no-op and the behavioural contract cannot be exercised, so
the suite is skipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "rtk-rewrite.js"

NODE_UNAVAILABLE = shutil.which("node") is None
RTK_UNAVAILABLE = shutil.which("rtk") is None


def _run(command: str) -> dict:
    """Invoke the hook with a Bash tool payload and return parsed stdout (or {})."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    proc = subprocess.run(
        ["node", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _rewritten_to(result: dict) -> str | None:
    """Extract the rewritten command from a hook result, or None on passthrough.

    Examples:
        >>> _rewritten_to({"hookSpecificOutput": {"updatedInput": {"command": "rtk git status"}}})
        'rtk git status'
        >>> _rewritten_to({}) is None
        True
    """
    try:
        return result["hookSpecificOutput"]["updatedInput"]["command"]
    except (KeyError, TypeError):
        return None


# ── Read-only commands are rewritten + auto-allowed ───────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the hook",
)


_skip_rtk_unavailable = pytest.mark.skipif(
    RTK_UNAVAILABLE,
    reason="hook is a no-op without rtk on PATH — contract not exercisable",
)


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff HEAD",
        "git log --oneline -5",
        "git show HEAD",
        "gh pr view 42",
        "gh issue list",
        "gh api repos/owner/repo",
        "docker ps",
        "kubectl get pods",
        "aws ec2 describe-instances",
        "aws s3 ls",
        "pytest tests/",
        "ruff check .",
        "grep -r foo .",
        "ls -la",
    ],
)
def test_readonly_commands_are_rewritten_and_allowed(command: str) -> None:
    """Provably read-only commands get an rtk rewrite with allow decision."""
    result = _run(command)
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert _rewritten_to(result) == f"rtk {command}"


# ── Mutating / dangerous commands must NEVER be rewritten (no deny bypass) ─────


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git branch -D feature",
        "git reset --hard HEAD",
        "git commit -m x",
        "gh pr comment 42 --body hi",
        "gh pr create --title x",
        "gh issue close 42",
        "gh release create v1",
        "gh api -X POST repos/o/r/issues",
        "gh api --method DELETE repos/o/r/x",
        "docker rm -f box",
        "kubectl delete pod x",
        "aws s3 rm s3://bucket/key",
    ],
)
def test_mutating_commands_passthrough_unchanged(command: str) -> None:
    """Mutating subcommands are never rewritten; they passthrough to real checks."""
    result = _run(command)
    assert result == {}, f"{command!r} was rewritten — deny bypass risk: {result}"


# ── Compound commands must never be rewritten (chaining deny-bypass) ───────────


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        "git status && git push origin main",
        "git status; git push",
        "git status $(git push)",
        "git diff HEAD `git push`",
        "git status && rm -rf /tmp/x",
        "gh pr view 42 && gh pr merge 42",
        "aws ec2 describe-instances && aws s3 rm s3://b/k",
        "pytest tests/ && rm -rf build",
        "git log | tee /tmp/out",
        "git log > /tmp/out",
    ],
)
def test_compound_commands_are_never_rewritten(command: str) -> None:
    """A read-only prefix followed by any shell operator must passthrough whole."""
    result = _run(command)
    assert result == {}, f"{command!r} was rewritten — chaining bypass: {result}"


# ── git branch create must not be rewritten ───────────────────────────────────


@_skip_rtk_unavailable
@_skip_node_unavailable
def test_git_branch_create_passthrough() -> None:
    """Create a branch (mutation) — never rewritten."""
    assert _run("git branch newfeature") == {}


# ── find is never rewritten (destructive flags carry no shell metacharacter) ──


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.py' -delete",
        "find . -type f -exec rm {} +",
        "find . -fprintf /etc/target payload",
        "find . -name '*.py'",
    ],
)
def test_find_is_never_rewritten(command: str) -> None:
    """``find`` passes through whole.

    ``-delete``, ``-exec ... {} +`` and ``-fprintf`` mutate the filesystem while carrying no character that
    ``SHELL_META`` catches, so a prefix match would auto-approve them. The read-only spelling passes through too — the
    prefix is excluded outright rather than filtered.
    """
    result = _run(command)
    assert result == {}, f"{command!r} was rewritten — destructive find bypass: {result}"


# ── cargo / next: inspection rewritten, execution passthrough ─────────────────


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize("command", ["cargo tree", "cargo metadata --no-deps", "next info"])
def test_guarded_build_tool_inspection_is_rewritten(command: str) -> None:
    """Inspection subcommands of cargo/next still earn the rewrite."""
    result = _run(command)
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert _rewritten_to(result) == f"rtk {command}"


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    ["cargo install ripgrep", "cargo run --release", "cargo check", "next build", "next dev"],
)
def test_guarded_build_tool_execution_passthrough(command: str) -> None:
    """Subcommands that execute arbitrary project code are never rewritten.

    ``cargo check`` is included: it runs ``build.rs``, so it executes project code even though it produces no binary.
    """
    result = _run(command)
    assert result == {}, f"{command!r} was rewritten — arbitrary execution: {result}"


# ── Result-corrupting commands are excluded ───────────────────────────────────


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        "diff a.txt b.txt",
        "curl -X POST https://api.example.com",
        "wget --post-data=x https://example.com",
        "psql -c 'DROP TABLE users'",
    ],
)
def test_excluded_commands_passthrough(command: str) -> None:
    """Diff (result corruption) and curl/wget/psql (unprovable intent) passthrough."""
    result = _run(command)
    assert result == {}, f"{command!r} should passthrough, got: {result}"


# ── Basic hook hygiene ────────────────────────────────────────────────────────


@_skip_rtk_unavailable
@_skip_node_unavailable
def test_already_prefixed_command_passthrough() -> None:
    """A command already starting with 'rtk ' is left untouched (no double wrap)."""
    assert _run("rtk git status") == {}


@_skip_rtk_unavailable
@_skip_node_unavailable
def test_non_bash_tool_passthrough() -> None:
    """Non-Bash tool payloads are ignored."""
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
    proc = subprocess.run(["node", str(HOOK)], input=payload, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
