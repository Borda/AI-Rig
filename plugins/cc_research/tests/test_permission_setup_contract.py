"""Verify the standalone hook payload and execute setup's real permission merges."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_BASH = shutil.which("bash")
_TRIMMED_ALLOWS = {
    "Bash(gh api repos/*)",
    "Bash(gh api graphql:*)",
    "Bash(git branch:*)",
    "Bash(git tag:*)",
}

RETIRED_SHELL_ALLOWS = {"Bash(find:*)", "Bash(node:*)", "Bash(claude:*)", "Bash(sed:*)", "Bash(eval:*)"}


def test_standalone_allow_list_omits_broad_write_matches() -> None:
    """Fresh installs must not pre-approve API writes or local branch/tag deletion."""
    allow = json.loads((_PLUGIN / ".claude-plugin/permissions-allow.json").read_text(encoding="utf-8"))
    assert (_TRIMMED_ALLOWS | RETIRED_SHELL_ALLOWS).isdisjoint(allow)
    assert {"Bash(python:*)", "Bash(node --check:*)", "Bash(find .temp*)"} <= set(allow)
    assert "Bash(gh api search/*)" in allow


def test_hook_payload_and_registrations_keep_only_research_modules() -> None:
    """The payload retains research's hooks and one Bash auto-allow registration."""
    expected = {
        "agent-router.js",
        "allow-dispatch.js",
        "audit-close.js",
        "blueprint-allow.js",
        "enforce-topic-header.js",
        "report-header-table.js",
        "sentinel-read-allow.js",
        "stale-plugin-check.js",
        "write-guard.js",
    }
    assert {path.name for path in (_PLUGIN / "hooks").glob("*.js")} == expected
    assert {path.name for path in (_PLUGIN / "hooks/lib").glob("*.js")} == {"audit-log.js"}
    assert {path.name for path in (_PLUGIN / ".claude-plugin").glob("*.json")} == {
        "plugin.json",
        "permissions-allow.json",
        "permissions-deny.json",
    }
    hooks = json.loads((_PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    bash_hooks = [hook for hook in hooks["PreToolUse"] if "Bash" in hook["matcher"].split("|")]
    assert len(bash_hooks) == 1
    assert "/hooks/allow-dispatch.js" in bash_hooks[0]["hooks"][0]["command"]
    for event in hooks.values():
        for registration in event:
            for hook in registration["hooks"]:
                referenced = re.findall(r"/hooks/([^\"/]+\.js)", hook["command"])
                assert set(referenced) <= expected


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="setup requires Bash")
@pytest.mark.parametrize("existing_settings", [False, True])
def test_setup_executes_additive_idempotent_merges(tmp_path: Path, existing_settings: bool) -> None:
    """The shipped merge call retains shared allows and user denies while retiring obsolete shell and API grants."""
    assert _BASH is not None
    user_allow = _TRIMMED_ALLOWS | {"Bash(custom-reader:*)"} if existing_settings else set()
    user_deny = (
        {"Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Bash(custom-writer:*)"} if existing_settings else set()
    )
    settings = (
        {
            "theme": "dark",
            "permissions": {
                "allow": sorted(user_allow | {"Bash(gh api repos/*:*)"} | RETIRED_SHELL_ALLOWS),
                "deny": sorted(user_deny),
            },
        }
        if existing_settings
        else {}
    )
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    config.write_text(json.dumps(settings), encoding="utf-8")
    # Bash filesystem operations need forward slashes, including on native Windows.
    env = {
        **os.environ,
        "HOME": tmp_path.as_posix(),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
    }
    setup = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", setup, re.DOTALL)
    candidates = [block for block in blocks if "merge_settings_permissions.py" in block]
    assert len(candidates) == 1
    # The block's bare `python` may not be on PATH (or may be a different interpreter) under the test runner.
    block = re.sub(r"^python ", f'"{Path(sys.executable).as_posix()}" ', candidates[0], count=1, flags=re.MULTILINE)
    for _ in range(2):
        # A bare name lets Windows select System32's WSL launcher ahead of the PATH shell.
        result = subprocess.run(
            [_BASH, "-c", block],
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{_BASH} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        merged = json.loads(config.read_text(encoding="utf-8"))
        # User order kept (obsolete rule dropped), shipped rules appended once each in shipped-file order.
        existing = {"allow": sorted(user_allow), "deny": sorted(user_deny)}
        for kind in ("allow", "deny"):
            shipped = json.loads((_PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))
            assert merged["permissions"][kind] == list(dict.fromkeys(existing[kind] + shipped))
            assert f"permissions.{kind}: added" in result.stdout
        if existing_settings:
            assert merged["theme"] == "dark"


def test_readme_and_setup_explain_the_upgrade_boundary() -> None:
    """Upgrade guidance distinguishes shipped permissions from preserved settings and foundry cleanup."""
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    setup = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    assert "Research ships no git/gh guard" in readme
    assert "Existing settings keep these four allow entries" in readme
    assert all(f"`{entry}`" in readme for entry in _TRIMMED_ALLOWS)
    assert "stale gh deny entries from older versions: run /foundry:setup" in setup
    assert "nothing replaces it" in setup


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="setup requires Bash")
@pytest.mark.parametrize("corrupt_merge", [False, True])
def test_setup_validates_without_jq_and_restores_parse_failure(tmp_path: Path, corrupt_merge: bool) -> None:
    """Missing jq cannot undo a valid merge; genuinely invalid JSON restores the exact backup."""
    assert _BASH is not None
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    original = '{"theme": "dark", "permissions": {"allow": [], "deny": []}}\n'
    config.write_text(original, encoding="utf-8")
    setup = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", setup, re.DOTALL)
    backup = next(block for block in blocks if "SETUP_BAK_TS=$(date" in block)
    merge = next(block for block in blocks if "merge_settings_permissions.py" in block)
    # Replace the interpreter boundary, retaining the real shipped backup, merge and validation commands.
    corruption = 'if [ "$1" != "-m" ]; then printf "{" > "$HOME/.claude/settings.json"; fi' if corrupt_merge else ":"
    shell_functions = f"""python() {{ "{Path(sys.executable).as_posix()}" "$@" || return $?; {corruption}; }}
jq() {{ printf 'unexpected jq invocation\\n' >&2; return 127; }}
"""
    result = subprocess.run(
        [_BASH, "-c", shell_functions + backup + "\n" + merge],
        env={
            **os.environ,
            "HOME": tmp_path.as_posix(),
            "USERPROFILE": str(tmp_path),
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "unexpected jq invocation" not in result.stderr
    assert result.returncode == (1 if corrupt_merge else 0), (result.stdout, result.stderr)
    if corrupt_merge:
        assert config.read_text(encoding="utf-8") == original
        assert "restored from" in result.stdout
    else:
        settings = json.loads(config.read_text(encoding="utf-8"))
        assert settings["theme"] == "dark"
        shipped = json.loads((_PLUGIN / ".claude-plugin/permissions-allow.json").read_text(encoding="utf-8"))
        assert settings["permissions"]["allow"] == shipped
        assert "restored from" not in result.stdout
