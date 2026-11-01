"""Check standalone permissions and execute the setup skill's real settings merges.

The oss plugin ships without a git/gh guard, so its allow list, hook inventory and setup merge are contracts: broad
write matches stay out of fresh installs, the hook set stays oss-only, and the setup skill's own bash block merges
shipped permissions into user settings additively and idempotently.
"""

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

RETIRED_SHELL_ALLOWS = {"Bash(find:*)", "Bash(node:*)", "Bash(claude:*)", "Bash(sed:*)", "Bash(eval:*)"}

_EXPECTED_HOOKS = {
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

_LEGACY_ALLOW = {"Bash(gh api repos/*:*)", "Bash(uv pip install:*)"} | RETIRED_SHELL_ALLOWS
_USER_ALLOW = _TRIMMED_ALLOWS | {"Bash(custom-reader:*)"}
_USER_DENY = {"Bash(gh pr merge:*)", "Bash(git branch -D:*)", "Bash(custom-writer:*)"}
_EXISTING_SETTINGS = {
    "theme": "dark",
    "permissions": {"allow": sorted(_USER_ALLOW | _LEGACY_ALLOW), "deny": sorted(_USER_DENY)},
}
# The merge keeps user order (legacy spellings dropped) and appends shipped rules once each, in shipped-file order.
_KEPT_ALLOW = [rule for rule in sorted(_USER_ALLOW | _LEGACY_ALLOW) if rule not in _LEGACY_ALLOW]


def _shipped(kind: str) -> list[str]:
    """Return the rules the plugin ships in ``permissions-<kind>.json``."""
    return json.loads((_PLUGIN / f".claude-plugin/permissions-{kind}.json").read_text(encoding="utf-8"))


def _setup_merge_block() -> str:
    """Return the setup skill's permission-merge bash block, with the bare ``python`` bound to this runner."""
    text = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
    candidates = [block for block in blocks if "merge_settings_permissions.py" in block]
    assert len(candidates) == 1
    # The block's bare `python` may not be on PATH (or may be a different interpreter) under the test runner.
    return re.sub(r"^python ", f'"{Path(sys.executable).as_posix()}" ', candidates[0], count=1, flags=re.MULTILINE)


def _run_merge(bash: str, merge: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the setup merge block in bash and require a clean exit."""
    # A bare name lets Windows select System32's WSL launcher ahead of the PATH shell.
    result = subprocess.run(
        [bash, "-c", merge], env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=False
    )
    assert result.returncode == 0, (
        f"{bash} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _merged_settings(config: Path) -> dict:
    """Read the settings file the merge wrote."""
    return json.loads(config.read_text(encoding="utf-8"))


def _search_commands() -> list[list[str]]:
    """Return the argument vectors of the ``gh api`` examples in the ecosystem mode file."""
    text = (_PLUGIN / "skills/analyse/modes/ecosystem.md").read_text(encoding="utf-8")
    return [shlex.split(line.removesuffix("\\")) for line in text.splitlines() if line.startswith("gh api ")]


class TestStandaloneAllowList:
    """The allow list a fresh install receives from the oss plugin."""

    def test_omits_broad_write_matches(self) -> None:
        """Fresh OSS installs must not pre-approve API writes or local branch/tag deletion.

        The trimmed entries (``gh api repos/*``, branch and tag matches) and the retired shell allows would let a prompt
        injection write through ``gh`` or delete refs without a question, so none may reappear in the shipped list.
        """
        allow = _shipped("allow")
        assert (_TRIMMED_ALLOWS | RETIRED_SHELL_ALLOWS).isdisjoint(allow)
        assert {"Bash(python:*)", "Bash(node --check:*)", "Bash(find .temp*)"} <= set(allow)
        assert "Bash(gh api search/*)" in allow


class TestHookInventory:
    """The standalone payload keeps only oss-owned hooks and registrations."""

    def test_single_bash_auto_allow_registration(self) -> None:
        """Exactly one Bash ``PreToolUse`` registration exists and it points at ``allow-dispatch.js``.

        Two Bash auto-allow hooks would each emit a decision; the dispatcher is the one place that ranks them.
        """
        hooks = json.loads((_PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        bash_hooks = [hook for hook in hooks["PreToolUse"] if "Bash" in hook["matcher"].split("|")]
        assert len(bash_hooks) == 1
        assert "/hooks/allow-dispatch.js" in bash_hooks[0]["hooks"][0]["command"]

    def test_registrations_reference_only_shipped_hooks(self) -> None:
        """Every hook command registered in ``hooks.json`` names a module in the oss hook inventory."""
        hooks = json.loads((_PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        referenced = {
            name
            for event in hooks.values()
            for registration in event
            for hook in registration["hooks"]
            for name in re.findall(r"/hooks/([^\"/]+\.js)", hook["command"])
        }
        assert referenced <= _EXPECTED_HOOKS


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="setup requires Bash")
class TestSetupMerge:
    """The setup skill's real merge call against a temporary home."""

    @pytest.mark.parametrize(
        ("settings", "existing", "extra"),
        [
            pytest.param({}, {"allow": [], "deny": []}, {}, id="no-settings"),
            pytest.param(
                _EXISTING_SETTINGS,
                {"allow": _KEPT_ALLOW, "deny": sorted(_USER_DENY)},
                {"theme": "dark"},
                id="existing-settings",
            ),
        ],
    )
    def test_is_additive_and_idempotent(self, tmp_path: Path, settings: dict, existing: dict, extra: dict) -> None:
        """The shipped merge call preserves user denies and shared allows, retiring obsolete OSS and shell grants.

        Scenario: setup runs twice against either no settings file or one holding user rules, legacy spellings and an
        unrelated key. After each run the allow and deny lists must be the user's kept rules plus the shipped rules
        once each, so a second run changes nothing and the unrelated key survives.
        """
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
        merge = _setup_merge_block()
        expected = {kind: list(dict.fromkeys(existing[kind] + _shipped(kind))) for kind in ("allow", "deny")}
        first = _run_merge(_BASH, merge, env)
        after_first = _merged_settings(config)
        second = _run_merge(_BASH, merge, env)
        after_second = _merged_settings(config)
        assert all(f"permissions.{kind}: added" in run.stdout for run in (first, second) for kind in ("allow", "deny"))
        assert after_first["permissions"] == expected
        assert after_second["permissions"] == expected
        assert extra.items() <= after_second.items()


class TestUpgradeGuidance:
    """Documentation the upgrade path depends on."""

    def test_readme_and_setup_explain_the_upgrade_boundary(self) -> None:
        """Upgrade guidance must distinguish the shipped trim from preserved user settings and foundry cleanup.

        Users upgrading keep their four trimmed allow entries; the README and setup skill have to say so, and point at
        foundry setup for stale gh deny entries, or the trim reads as a silent regression.
        """
        readme = (_PLUGIN / "README.md").read_text(encoding="utf-8")
        setup = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
        assert "oss ships no git/gh guard" in readme
        assert "Existing settings keep these four allow entries" in readme
        assert all(f"`{entry}`" in readme for entry in _TRIMMED_ALLOWS)
        assert "stale gh deny entries from older versions: run /foundry:setup" in setup
        assert "nothing replaces it" in setup


class TestEcosystemSearch:
    """The ecosystem mode's ``gh api`` search examples."""

    def test_uses_path_first_explicit_get(self) -> None:
        """Search examples keep fields read-only while matching the standalone search API permission prefix.

        Both examples must start ``gh api search/code --method GET`` and only then add ``--field``, so the shipped
        ``Bash(gh api search/*)`` allow entry matches them and the explicit GET stops ``--field`` turning them into a
        POST.
        """
        commands = _search_commands()
        assert len(commands) == 2
        assert all(argv[:5] == ["gh", "api", "search/code", "--method", "GET"] for argv in commands)
        assert all(argv.index("--field") > argv.index("--method") for argv in commands)
