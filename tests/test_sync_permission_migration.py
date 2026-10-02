"""Verify shipped permission rules and execute setup migrations against isolated user settings."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
JQ = shutil.which("jq")
OLD_RULE = "Bash(gh api repos/*:*)"
NEW_RULE = "Bash(gh api repos/*)"
PLUGINS = ["cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py"]


@pytest.mark.parametrize("plugin", PLUGINS)
def test_shipped_repository_api_rule_uses_wildcard_syntax(plugin: str) -> None:
    """Repository API permissions must avoid mixing wildcard and legacy prefix syntax."""
    rules = json.loads((ROOT / "plugins" / plugin / ".claude-plugin/permissions-allow.json").read_text())

    assert NEW_RULE in rules
    assert OLD_RULE not in rules
    assert not [rule for rule in rules if rule.endswith(":*)") and "*" in rule[:-3]]


@pytest.mark.integration
@pytest.mark.skipif(JQ is None, reason="jq is required to execute the shipped setup migration")
@pytest.mark.parametrize("plugin", PLUGINS)
@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="first-install"),
        pytest.param({"permissions": {"allow": [OLD_RULE, OLD_RULE]}}, id="duplicate-stale-rules"),
        pytest.param({"permissions": {"allow": [OLD_RULE, NEW_RULE]}}, id="old-and-new-rules"),
        pytest.param(
            {
                "permissions": {
                    "allow": [OLD_RULE, "Bash(custom-tool:*)", "Bash(gh api repos/custom:*)"],
                    "deny": ["Bash(gh api repos/private/*)"],
                },
                "env": {"CUSTOM_SETTING": "preserved"},
            },
            id="preserve-user-settings",
        ),
    ],
)
def test_setup_migrates_only_obsolete_rule(plugin: str, settings: dict[str, object]) -> None:
    """Setup must remove the exact stale rule, preserve user settings, and remain idempotent."""
    root = ROOT / "plugins" / plugin
    source = root / ("README.md" if plugin == "codemap-py" else "skills/setup/SKILL.md")
    skill = source.read_text(encoding="utf-8")
    expressions = re.findall(r"'([^'\n]*\.permissions\.allow = [^'\n]*)'", skill)
    assert len(expressions) == 1, "setup must expose one executable allow merge"
    command = [JQ, "--slurpfile", "perms", str(root / ".claude-plugin/permissions-allow.json"), expressions[0]]

    first = subprocess.run(command, input=json.dumps(settings), capture_output=True, text=True, check=True)
    migrated = json.loads(first.stdout)
    second = subprocess.run(command, input=first.stdout, capture_output=True, text=True, check=True)

    shipped = json.loads((root / ".claude-plugin/permissions-allow.json").read_text())
    original_permissions = settings.get("permissions", {})
    original_allow = original_permissions.get("allow", [])
    assert migrated["permissions"]["allow"] == sorted(set(original_allow + shipped) - {OLD_RULE})
    assert NEW_RULE in migrated["permissions"]["allow"]
    assert json.loads(second.stdout) == migrated
    expected = json.loads(json.dumps(settings))
    expected.setdefault("permissions", {})["allow"] = migrated["permissions"]["allow"]
    assert migrated == expected
