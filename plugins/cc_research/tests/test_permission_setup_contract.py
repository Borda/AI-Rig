"""Verify the standalone hook payload and execute setup's real permission merges."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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


def test_standalone_allow_list_omits_broad_write_matches() -> None:
    """Fresh installs must not pre-approve API writes or local branch/tag deletion."""
    allow = json.loads((_PLUGIN / ".claude-plugin/permissions-allow.json").read_text(encoding="utf-8"))
    assert _TRIMMED_ALLOWS.isdisjoint(allow)
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
@pytest.mark.skipif(not _BASH or not shutil.which("jq"), reason="setup requires Bash and jq")
@pytest.mark.parametrize("existing_settings", [False, True])
def test_setup_executes_additive_idempotent_merges(tmp_path: Path, existing_settings: bool) -> None:
    """Real jq merges retain shared allows and user denies while removing only the obsolete allow."""
    assert _BASH is not None
    user_allow = _TRIMMED_ALLOWS | {"Bash(custom-reader:*)"} if existing_settings else set()
    user_deny = (
        {"Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Bash(custom-writer:*)"} if existing_settings else set()
    )
    settings = (
        {
            "theme": "dark",
            "permissions": {
                "allow": sorted(user_allow | {"Bash(gh api repos/*:*)"}),
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
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
    }
    setup = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", setup, re.DOTALL)
    for _ in range(2):
        for kind, original in (("allow", user_allow), ("deny", user_deny)):
            candidates = [block for block in blocks if f".permissions.{kind} =" in block]
            assert len(candidates) == 1
            # A bare name lets Windows select System32's WSL launcher ahead of the PATH shell.
            result = subprocess.run(
                [_BASH, "-c", candidates[0]],
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
            shipped = json.loads((_PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))
            assert merged["permissions"][kind] == sorted(original | set(shipped))
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
