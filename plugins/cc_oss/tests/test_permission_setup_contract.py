"""Check standalone permissions and execute the setup skill's real settings merges."""

from __future__ import annotations

import json
import os
import re
import shlex
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

RETIRED_SHELL_ALLOWS = {"Bash(find:*)", "Bash(node:*)", "Bash(claude:*)", "Bash(sed:*)"}


def test_standalone_allow_list_omits_broad_write_matches() -> None:
    """Fresh OSS installs must not pre-approve API writes or local branch/tag deletion."""
    allow = json.loads((_PLUGIN / ".claude-plugin/permissions-allow.json").read_text(encoding="utf-8"))
    assert (_TRIMMED_ALLOWS | RETIRED_SHELL_ALLOWS).isdisjoint(allow)
    assert {"Bash(eval:*)", "Bash(python:*)", "Bash(node --check:*)", "Bash(find .temp*)"} <= set(allow)
    assert "Bash(gh api search/*)" in allow


def test_hook_inventory_and_registrations_keep_only_oss_modules() -> None:
    """The standalone payload retains its own hooks and only one Bash auto-allow registration."""
    expected = {
        "agent-router.js",
        "allow-dispatch.js",
        "audit-close.js",
        "blueprint-allow.js",
        "enforce-analyse-header.js",
        "enforce-resolve-table.js",
        "enforce-review-header.js",
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
    """The shipped merge call preserves user denies and shared allows, retiring obsolete OSS and shell grants."""
    assert _BASH is not None
    legacy_allow = {"Bash(gh api repos/*:*)", "Bash(uv pip install:*)"} | RETIRED_SHELL_ALLOWS
    user_allow = _TRIMMED_ALLOWS | {"Bash(custom-reader:*)"}
    user_deny = {"Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Bash(custom-writer:*)"}
    settings = {"theme": "dark", "permissions": {"allow": sorted(user_allow | legacy_allow), "deny": sorted(user_deny)}}
    if not existing_settings:
        settings = {}
        user_allow = set()
        user_deny = set()
    # Merge keeps user order (legacy spellings dropped) and appends shipped rules once each, in shipped-file order.
    existing = {
        "allow": [rule for rule in sorted(user_allow | legacy_allow) if rule not in legacy_allow] if user_allow else [],
        "deny": sorted(user_deny),
    }
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
    text = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
    candidates = [block for block in blocks if "merge_settings_permissions.py" in block]
    assert len(candidates) == 1
    # The block's bare `python` may not be on PATH (or may be a different interpreter) under the test runner.
    merge = re.sub(r"^python ", f'"{Path(sys.executable).as_posix()}" ', candidates[0], count=1, flags=re.MULTILINE)
    for _ in range(2):
        # A bare name lets Windows select System32's WSL launcher ahead of the PATH shell.
        result = subprocess.run(
            [_BASH, "-c", merge],
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{_BASH} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        for kind in ("allow", "deny"):
            assert f"permissions.{kind}: added" in result.stdout
            merged = json.loads(config.read_text(encoding="utf-8"))
            shipped = json.loads((_PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))
            assert merged["permissions"][kind] == list(dict.fromkeys(existing[kind] + shipped))
            if existing_settings:
                assert merged["theme"] == "dark"
    final = json.loads(config.read_text(encoding="utf-8"))
    for kind in ("allow", "deny"):
        shipped = json.loads((_PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))
        assert final["permissions"][kind] == list(dict.fromkeys(existing[kind] + shipped))


def test_readme_and_setup_explain_the_upgrade_boundary() -> None:
    """Upgrade guidance must distinguish the shipped trim from preserved user settings and foundry cleanup."""
    readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
    setup = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    assert "oss ships no git/gh guard" in readme
    assert "Existing settings keep these four allow entries" in readme
    assert all(f"`{entry}`" in readme for entry in _TRIMMED_ALLOWS)
    assert "stale gh deny entries from older versions: run /foundry:setup" in setup
    assert "nothing replaces it" in setup


def test_ecosystem_search_uses_path_first_explicit_get() -> None:
    """Search examples keep fields read-only while matching the standalone search API permission prefix."""
    text = (_PLUGIN / "skills/analyse/modes/ecosystem.md").read_text(encoding="utf-8")
    commands = [shlex.split(line.removesuffix("\\")) for line in text.splitlines() if line.startswith("gh api ")]
    assert len(commands) == 2
    for argv in commands:
        assert argv[:5] == ["gh", "api", "search/code", "--method", "GET"]
        assert argv.index("--field") > argv.index("--method")
