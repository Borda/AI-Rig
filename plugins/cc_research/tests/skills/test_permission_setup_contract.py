"""Verify the standalone hook payload and execute setup's real permission merges.

The payload tests read shipped JSON and file names; the setup tests extract the fenced ``bash`` blocks from the setup
skill and run them in a real Bash against a sandboxed ``HOME``, because the shipped merge call is the contract.
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
from _research_text import flat

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_TRIMMED_ALLOWS = {
    "Bash(gh api repos/*)",
    "Bash(gh api graphql:*)",
    "Bash(git branch:*)",
    "Bash(git tag:*)",
}

RETIRED_SHELL_ALLOWS = {"Bash(find:*)", "Bash(node:*)", "Bash(claude:*)", "Bash(sed:*)", "Bash(eval:*)"}
_HOOK_FILES = {
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
_skip_without_bash = pytest.mark.skipif(not _BASH, reason="setup requires Bash")


def _read(relative: str) -> str:
    return (_PLUGIN / relative).read_text(encoding="utf-8")


def _flat(relative: str) -> str:
    """File text with whitespace runs collapsed, so a phrase matches wherever the prose is wrapped."""
    return flat(_read(relative))


def _bash_blocks(markdown: str) -> list[str]:
    """Return the body of every fenced ``bash`` block, parsed line by line."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        if current is None and line.strip() == "```bash":
            current = []
        elif current is not None and line.strip() == "```":
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return ["\n".join(block) for block in blocks]


def _setup_env(home: Path) -> dict[str, str]:
    """Environment for a setup run; Bash filesystem operations need forward slashes, including on native Windows."""
    return {
        **os.environ,
        "HOME": home.as_posix(),
        "USERPROFILE": str(home),
        "TMPDIR": home.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
    }


def _run_bash(script: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in the PATH Bash (a bare name lets Windows pick System32's WSL launcher first)."""
    assert _BASH is not None
    return subprocess.run(
        [_BASH, "-c", script],
        env=_setup_env(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _user_settings(existing: bool) -> tuple[set[str], set[str], dict]:
    """Return ``(user_allow, user_deny, settings)`` for a pre-existing user config, or empty ones for a fresh home."""
    if not existing:
        return set(), set(), {}
    allow = _TRIMMED_ALLOWS | {"Bash(custom-reader:*)"}
    deny = {"Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Bash(custom-writer:*)"}
    settings = {
        "theme": "dark",
        "permissions": {
            "allow": sorted(allow | {"Bash(gh api repos/*:*)"} | RETIRED_SHELL_ALLOWS),
            "deny": sorted(deny),
        },
    }
    return allow, deny, settings


class TestShippedPayload:
    """Files and registrations a standalone install ships."""

    def test_allow_list_omits_broad_write_matches(self) -> None:
        """Fresh installs must not pre-approve API writes or local branch/tag deletion.

        The trimmed and retired grants stay out of the shipped allow list while the read-only essentials remain in it.
        """
        allow = json.loads(_read(".claude-plugin/permissions-allow.json"))

        assert (_TRIMMED_ALLOWS | RETIRED_SHELL_ALLOWS).isdisjoint(allow)
        assert {"Bash(python:*)", "Bash(node --check:*)", "Bash(find .temp*)"} <= set(allow)
        assert "Bash(gh api search/*)" in allow

    def test_registers_one_bash_auto_allow_hook(self) -> None:
        """Exactly one ``PreToolUse`` registration matches Bash, and it dispatches through ``allow-dispatch.js``."""
        hooks = json.loads(_read("hooks/hooks.json"))["hooks"]

        bash_hooks = [hook for hook in hooks["PreToolUse"] if "Bash" in hook["matcher"].split("|")]

        assert len(bash_hooks) == 1
        assert "/hooks/allow-dispatch.js" in bash_hooks[0]["hooks"][0]["command"]

    def test_registrations_reference_only_shipped_hooks(self) -> None:
        """Every hook script named by a registration command is one of the shipped research hooks."""
        hooks = json.loads(_read("hooks/hooks.json"))["hooks"]

        referenced = {
            name
            for event in hooks.values()
            for registration in event
            for hook in registration["hooks"]
            for name in re.findall(r"/hooks/([^\"/]+\.js)", hook["command"])
        }

        assert referenced <= _HOOK_FILES

    def test_readme_and_setup_explain_upgrade_boundary(self) -> None:
        """Upgrade guidance distinguishes shipped permissions from preserved settings and foundry cleanup.

        Phrases are matched on whitespace-collapsed text so reflowing the Markdown cannot fail the check.
        """
        readme = _flat("README.md")
        setup = _flat("skills/setup/SKILL.md")

        assert "Research ships no git/gh guard" in readme
        assert "Existing settings keep these four allow entries" in readme
        assert all(f"`{entry}`" in readme for entry in _TRIMMED_ALLOWS)
        assert "stale gh deny entries from older versions: run /foundry:setup" in setup
        assert "Nothing replaces those retired entries." in setup


@pytest.mark.integration
@_skip_without_bash
class TestSetupMerge:
    """The shipped setup blocks executed in a real Bash against a sandboxed ``HOME``."""

    @staticmethod
    def _merge_block() -> str:
        """Return the single setup block that runs ``merge_settings_permissions.py``, with a runnable interpreter."""
        candidates = [
            block for block in _bash_blocks(_read("skills/setup/SKILL.md")) if "merge_settings_permissions.py" in block
        ]
        assert len(candidates) == 1
        # The block's bare `python` may not be on PATH (or may be a different interpreter) under the test runner.
        return re.sub(r"^python ", f'"{Path(sys.executable).as_posix()}" ', candidates[0], count=1, flags=re.MULTILINE)

    @pytest.mark.parametrize("existing_settings", [False, True])
    def test_merges_are_additive_and_idempotent(self, tmp_path: Path, existing_settings: bool) -> None:
        """The shipped merge call retains shared allows and user denies while retiring obsolete shell and API grants.

        Run twice on a fresh home and on a populated one: user order is kept (obsolete rules dropped), shipped rules are
        appended once each in shipped-file order, and the second run changes nothing.
        """
        user_allow, user_deny, settings = _user_settings(existing_settings)
        config = tmp_path / ".claude/settings.json"
        config.parent.mkdir()
        config.write_text(json.dumps(settings), encoding="utf-8")
        block = self._merge_block()

        first = _run_bash(block, tmp_path)
        after_first = json.loads(config.read_text(encoding="utf-8"))
        second = _run_bash(block, tmp_path)
        after_second = json.loads(config.read_text(encoding="utf-8"))

        existing = {"allow": sorted(user_allow), "deny": sorted(user_deny)}
        expected = {
            kind: list(dict.fromkeys(existing[kind] + json.loads(_read(f".claude-plugin/permissions-{kind}.json"))))
            for kind in ("allow", "deny")
        }
        assert (first.returncode, second.returncode) == (0, 0), (first.stderr, second.stderr)
        assert after_first["permissions"] == expected
        assert after_second["permissions"] == expected
        assert all(f"permissions.{kind}: added" in run.stdout for run in (first, second) for kind in ("allow", "deny"))
        assert after_second.get("theme") == settings.get("theme")

    @staticmethod
    def _run_backup_and_merge(home: Path, corruption: str) -> subprocess.CompletedProcess[str]:
        """Run the real backup and merge blocks with ``python`` replaced by a shim and ``jq`` forbidden.

        The shim runs the real interpreter, then executes ``corruption`` (a shell command) after the call, so the
        shipped backup, merge and validation commands stay real while the interpreter boundary is controlled.
        """
        blocks = _bash_blocks(_read("skills/setup/SKILL.md"))
        backup = next(block for block in blocks if "SETUP_BAK_TS=$(date" in block)
        merge = next(block for block in blocks if "merge_settings_permissions.py" in block)
        shell_functions = f"""python() {{ "{Path(sys.executable).as_posix()}" "$@" || return $?; {corruption}; }}
jq() {{ printf 'unexpected jq invocation\\n' >&2; return 127; }}
"""
        return _run_bash(shell_functions + backup + "\n" + merge, home)

    @pytest.fixture
    def original_settings(self, tmp_path: Path) -> str:
        """Write a minimal valid settings file under the sandboxed home and return its exact text."""
        original = '{"theme": "dark", "permissions": {"allow": [], "deny": []}}\n'
        config = tmp_path / ".claude/settings.json"
        config.parent.mkdir()
        config.write_text(original, encoding="utf-8")
        return original

    def test_valid_merge_survives_missing_jq(self, tmp_path: Path, original_settings: str) -> None:
        """Missing jq cannot undo a valid merge: validation uses Python, not jq, and the merged file is kept."""
        result = self._run_backup_and_merge(tmp_path, ":")

        settings = json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))
        shipped = json.loads(_read(".claude-plugin/permissions-allow.json"))
        assert "unexpected jq invocation" not in result.stderr
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert settings["theme"] == "dark"
        assert settings["permissions"]["allow"] == shipped
        assert "restored from" not in result.stdout

    def test_invalid_json_restores_exact_backup(self, tmp_path: Path, original_settings: str) -> None:
        """Genuinely invalid JSON after the merge restores the exact backup and exits 1.

        The shim truncates the settings file after the merge call, simulating a corrupting write; setup must roll back
        rather than leave the user's config unparsable.
        """
        corruption = 'if [ "$1" != "-m" ]; then printf "{" > "$HOME/.claude/settings.json"; fi'

        result = self._run_backup_and_merge(tmp_path, corruption)

        assert "unexpected jq invocation" not in result.stderr
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert (tmp_path / ".claude/settings.json").read_text(encoding="utf-8") == original_settings
        assert "restored from" in result.stdout
