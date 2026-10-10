"""Verify shipped permission rules and execute setup migrations against isolated user settings.

Two migrations run against isolated settings: the allow merge removes each plugin's exact retired allow rules, and the
deny merge removes the exact retired gh write deny entries (``permissions-deny-retired.json``), which would otherwise
defeat the gh write guard's one-time approval, since a settings deny rule still applies after a hook allows a call.
"""

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
#: Allow rule an earlier oss shipped that auto-approved the review overlay's support check and wheel fetch.
OVERLAY_INSTALL_RULE = "Bash(uv pip install:*)"
PLUGINS = ["cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py"]
#: Exact allow rules each plugin's setup removes from user settings before merging its own list.
RETIRED_RULES = {plugin: {OLD_RULE} for plugin in PLUGINS} | {"cc_oss": {OLD_RULE, OVERLAY_INSTALL_RULE}}
#: oss review template whose overlay support check and fetch must each reach their own harness permission prompt.
OVERLAY_TEMPLATE = ROOT / "plugins/cc_oss/skills/review/templates/agent-prompts.md"
#: Interpreter the overlay procedure names as ``<env-python>``.
ENV_PYTHON = ".venv/bin/python"
#: Command families no shipped allow rule may cover: each pre-approves arbitrary package downloads or installs.
FORBIDDEN_COMMAND_FAMILIES = [
    "uv pip install example-package",
    "uv run --with example-package python -m pytest",
    "pip install example-package",
    "pip download example-package",
]


def _overlay_commands() -> list[str]:
    """Return every overlay command the oss review template prescribes, as Claude Code would match it.

    A leading environment assignment is kept and also stripped: Claude Code strips assignments of certain known-safe
    variables before matching allow rules, so the check covers the command with and without its prefix.
    """
    text = OVERLAY_TEMPLATE.read_text(encoding="utf-8")
    spans = re.findall(r"`((?:uv pip install|PIP_CONFIG_FILE=)[^`]* --[^`]*)`", text)
    commands = [span.replace("<env-python>", ENV_PYTHON) for span in spans]
    return commands + [re.sub(r"^[A-Z_]+=\S+ ", "", command) for command in commands]


def _bash_rule_matches(rule: str, command: str) -> bool:
    """Return whether one shipped ``Bash(...)`` allow rule would match a command under Claude Code wildcard semantics.

    ``*`` matches any character sequence; a trailing ``:*`` or `` *`` also matches the bare command; no other
    wildcard exists. Compound-command splitting and wrapper stripping are irrelevant to single overlay commands.

    Example:
        >>> _bash_rule_matches("Bash(uv pip install:*)", "uv pip install --dry-run x")
        True
        >>> _bash_rule_matches("Bash(uv pip list:*)", "uv pip install x")
        False
    """
    if not (rule.startswith("Bash(") and rule.endswith(")")):
        return False
    pattern = rule[len("Bash(") : -1]
    bare_allowed = pattern.endswith((":*", " *"))
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, command, flags=re.DOTALL) is not None or (bare_allowed and command == pattern[:-2])


@pytest.mark.parametrize("plugin", PLUGINS)
def test_shipped_repository_api_rule_uses_wildcard_syntax(plugin: str) -> None:
    """Repository API permissions must avoid mixing wildcard and legacy prefix syntax."""
    rules = json.loads((ROOT / "plugins" / plugin / ".claude-plugin/permissions-allow.json").read_text())

    assert NEW_RULE in rules
    assert OLD_RULE not in rules
    assert not [rule for rule in rules if rule.endswith(":*)") and "*" in rule[:-3]]


def test_overlay_command_extraction_finds_every_fetch_step() -> None:
    """The template still spells the support check, fetch and both pip fallback commands where the scan finds them.

    Without this guard a reworded template would leave the allow-list cross-check below scanning nothing and passing.
    """
    commands = _overlay_commands()
    assert [command.split(" --", 1)[0] for command in commands[:4]] == [
        "uv pip install",
        "uv pip install",
        f"PIP_CONFIG_FILE=/dev/null {ENV_PYTHON} -m pip download",
        f"PIP_CONFIG_FILE=/dev/null {ENV_PYTHON} -m pip install",
    ]


@pytest.mark.parametrize(
    ("rule", "command_index", "matches"),
    [
        pytest.param(OVERLAY_INSTALL_RULE, 0, True, id="retired-install-rule"),
        pytest.param("Bash(uv pip *)", 0, True, id="space-wildcard"),
        pytest.param("Bash(uv*)", 0, True, id="glued-wildcard"),
        pytest.param("Bash(uv pip list:*)", 0, False, id="read-only-sibling"),
        pytest.param(f"Bash({ENV_PYTHON} -m pip:*)", 6, True, id="env-stripped-pip-fallback-is-checked"),
        pytest.param("Bash(python:*)", 6, False, id="interpreter-prefix-misses-venv-python"),
    ],
)
def test_allow_rule_matcher_catches_the_retired_rule(rule: str, command_index: int, matches: bool) -> None:
    """The matcher behind the cross-check reports rules that cover a shipped overlay command, and only those.

    Index 0 is the support check; index 6 is the pip-fallback download with its environment prefix stripped. A matcher
    that never matched would let the cross-check pass even with the retired rule still shipped.
    """
    assert _bash_rule_matches(rule, _overlay_commands()[command_index]) is matches


@pytest.mark.parametrize("plugin", PLUGINS)
def test_no_shipped_allow_rule_preapproves_overlay_fetch(plugin: str) -> None:
    """No plugin's shipped allow list may auto-approve the review overlay's download commands or their families.

    The oss review contract makes each overlay command's permission prompt the user's approval; a merged ``Bash(uv pip
    install:*)`` rule ran the support check and the wheel fetch with no prompt, and also covered installs into the
    project environment and source builds the overlay forbids.
    """
    rules = json.loads((ROOT / "plugins" / plugin / ".claude-plugin/permissions-allow.json").read_text())
    commands = _overlay_commands() + FORBIDDEN_COMMAND_FAMILIES

    assert [(rule, command) for rule in rules for command in commands if _bash_rule_matches(rule, command)] == []


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
        pytest.param(
            {"permissions": {"allow": [OVERLAY_INSTALL_RULE, "Bash(uv pip list:*)", OLD_RULE]}},
            id="retired-overlay-install-rule",
        ),
    ],
)
def test_setup_migrates_only_obsolete_rule(plugin: str, settings: dict[str, object]) -> None:
    """Setup must remove only the exact rules that plugin retired, preserve user settings, and remain idempotent.

    Only oss shipped ``Bash(uv pip install:*)``, so only oss setup removes it; every other plugin's setup keeps a user's
    own copy of that rule untouched.
    """
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
    assert migrated["permissions"]["allow"] == sorted(set(original_allow + shipped) - RETIRED_RULES[plugin])
    assert NEW_RULE in migrated["permissions"]["allow"]
    assert json.loads(second.stdout) == migrated
    expected = json.loads(json.dumps(settings))
    expected.setdefault("permissions", {})["allow"] = migrated["permissions"]["allow"]
    assert migrated == expected


#: A gh write deny entry every hook plugin and codemap-py shipped before the gh write guard's one-time approval.
RETIRED_GH_DENY = "Bash(gh pr merge:*)"
#: Local branch and tag deletion denies every hook plugin and codemap-py shipped before local Git ran freely.
RETIRED_LOCAL_DENY = ("Bash(git branch -D:*)", "Bash(git branch -d:*)", "Bash(git tag -d:*)")
#: Force and mirror push denies every plugin keeps: no approval ever covers a force push.
KEPT_FORCE_DENY = (
    "Bash(git push --force:*)",
    "Bash(git push --force-with-lease:*)",
    "Bash(git push -f:*)",
    "Bash(git push --force-if-includes:*)",
    "Bash(git push --mirror:*)",
)
KEPT_DENY = KEPT_FORCE_DENY[0]


def _plugin_json(plugin: str, name: str) -> list[str]:
    """Return one of a plugin's shipped ``.claude-plugin`` permission lists."""
    return json.loads((ROOT / "plugins" / plugin / ".claude-plugin" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("plugin", PLUGINS)
def test_shipped_deny_list_holds_no_retired_entry(plugin: str) -> None:
    """No shipped deny entry names a gh command or a local branch/tag deletion; every force push deny stays.

    A settings deny rule still applies after a hook allows a call, so a shipped gh write deny would make every one-time
    ``gh-write`` approval spend its token and still be denied; local Git, deletions included, runs freely. The retired
    list holds exactly those two kinds and nothing the list still ships.
    """
    deny = _plugin_json(plugin, "permissions-deny.json")
    retired = _plugin_json(plugin, "permissions-deny-retired.json")

    assert [rule for rule in deny if rule.startswith("Bash(gh ") or rule in RETIRED_LOCAL_DENY] == []
    assert [rule for rule in KEPT_FORCE_DENY if rule not in deny] == []
    assert {RETIRED_GH_DENY, *RETIRED_LOCAL_DENY} <= set(retired)
    assert [
        rule for rule in retired if rule in deny or not (rule.startswith("Bash(gh ") or rule in RETIRED_LOCAL_DENY)
    ] == []


@pytest.mark.integration
@pytest.mark.skipif(JQ is None, reason="jq is required to execute the shipped setup migration")
@pytest.mark.parametrize("plugin", PLUGINS)
@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="first-install"),
        pytest.param({"permissions": {"deny": [RETIRED_GH_DENY, RETIRED_GH_DENY, KEPT_DENY]}}, id="retired-and-kept"),
        pytest.param({"permissions": {"deny": [*RETIRED_LOCAL_DENY, KEPT_DENY]}}, id="retired-local-deletion"),
        pytest.param(
            {
                "permissions": {
                    "allow": ["Bash(custom-tool:*)"],
                    "deny": [RETIRED_GH_DENY, "Bash(gh pr merge --admin:*)", "Bash(custom-deny:*)"],
                },
                "env": {"CUSTOM_SETTING": "preserved"},
            },
            id="preserve-user-settings",
        ),
    ],
)
def test_setup_retires_only_the_shipped_gh_write_denies(plugin: str, settings: dict[str, object]) -> None:
    """Setup removes exactly the retired gh write denies, adds the shipped ones, keeps the rest, and is idempotent.

    A user's own deny entry that only resembles a retired one (``Bash(gh pr merge --admin:*)``) stays: removal is an
    exact string match against the plugin's retired list.
    """
    root = ROOT / "plugins" / plugin
    source = root / ("README.md" if plugin == "codemap-py" else "skills/setup/SKILL.md")
    expressions = re.findall(r"'([^'\n]*\.permissions\.deny = [^'\n]*)'", source.read_text(encoding="utf-8"))
    assert len(expressions) == 1, "setup must expose one executable deny merge"
    meta = root / ".claude-plugin"
    command = [
        JQ,
        "--slurpfile",
        "deny",
        str(meta / "permissions-deny.json"),
        "--slurpfile",
        "retired",
        str(meta / "permissions-deny-retired.json"),
        expressions[0],
    ]

    first = subprocess.run(command, input=json.dumps(settings), capture_output=True, text=True, check=True)
    migrated = json.loads(first.stdout)
    second = subprocess.run(command, input=first.stdout, capture_output=True, text=True, check=True)

    original_deny = settings.get("permissions", {}).get("deny", [])
    shipped = _plugin_json(plugin, "permissions-deny.json")
    retired = set(_plugin_json(plugin, "permissions-deny-retired.json"))
    assert migrated["permissions"]["deny"] == sorted(set(original_deny + shipped) - retired)
    assert json.loads(second.stdout) == migrated
    expected = json.loads(json.dumps(settings))
    expected.setdefault("permissions", {})["deny"] = migrated["permissions"]["deny"]
    assert migrated == expected
