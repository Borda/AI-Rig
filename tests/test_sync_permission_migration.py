"""Verify shipped permission rules and execute setup migrations against isolated user settings.

Two migrations run against isolated settings: the allow merge removes each plugin's exact retired allow rules, and the
Foundry deny merge removes the exact retired gh write deny entries (``permissions-deny-retired.json``), which would
otherwise defeat its gh write guard's one-time approval. Other plugins merge denies additively.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JQ = shutil.which("jq")
OLD_RULE = "Bash(gh api repos/*:*)"
NEW_RULE = "Bash(gh api repos/*)"
#: Allow rule an earlier oss shipped that auto-approved the review overlay's support check and wheel fetch.
OVERLAY_INSTALL_RULE = "Bash(uv pip install:*)"
PLUGINS = ["cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py"]
RETIRED_DENY_PLUGINS = ["cc_foundry"]
TRIMMED_ALLOW_PLUGINS = ["cc_oss", "cc_develop", "cc_research"]
GUARDED_ALLOW_RULES = {NEW_RULE, "Bash(gh api graphql:*)", "Bash(git branch:*)", "Bash(git tag:*)"}
#: Exact allow rules each plugin's setup removes from user settings before merging its own list.
RETIRED_SHELL_RULES = {"Bash(find:*)", "Bash(node:*)", "Bash(claude:*)", "Bash(sed:*)", "Bash(eval:*)"}
RETIRED_RULES = {plugin: {OLD_RULE, *RETIRED_SHELL_RULES} for plugin in PLUGINS}
RETIRED_RULES["cc_oss"].add(OVERLAY_INSTALL_RULE)
NARROWED_SHELL_RULES = {
    "cc_foundry": [
        "Bash(claude plugin validate)",
        'Bash(find "$INDEX" -mmin +60 -print)',
        'Bash(find "$_ROOT" -path "*/agents/*.md")',
        'Bash(find "$_ROOT" -path "*/rules/*.md")',
        'Bash(find "$_ROOT" -path "*skills/*/SKILL.md")',
        'Bash(find "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}" -maxdepth 5 -path "*/calibrate/modes/skills.md")',
        'Bash(find -path "*/agents/*.md")',
        "Bash(find .cache*)",
        "Bash(find .developments*)",
        "Bash(find .experiments*)",
        "Bash(find .notes*)",
        "Bash(find .plans*)",
        "Bash(find .reports*)",
        "Bash(find .temp*)",
        'Bash(find plugins -path "*/skills/*/SKILL.md")',
        'Bash(find ~/.claude/plugins/cache -name "*" -type d)',
        "Bash(node --check:*)",
        "Bash(sed 's/.*\\.bak-//')",
    ],
    "cc_develop": [
        "Bash(claude --version)",
        "Bash(claude plugin list)",
        "Bash(find .cache*)",
        "Bash(find .notes*)",
        "Bash(find .plans*)",
        "Bash(find .reports*)",
        "Bash(find .temp*)",
        'Bash(find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md")',
        "Bash(node --check:*)",
    ],
    "cc_oss": [
        "Bash(find .cache*)",
        "Bash(find .notes*)",
        "Bash(find .plans*)",
        "Bash(find .reports*)",
        "Bash(find .temp*)",
        'Bash(find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md")',
        "Bash(node --check:*)",
        "Bash(sed 's/-\\+/-/g; s/^-//; s/-$//')",
        "Bash(sed 's/\\.[^.]*$//')",
        "Bash(sed 's|refs/remotes/origin/||')",
        "Bash(sed -nE 's/^(PR:[[:space:]]*|## Code Review: (PR )?)#?([1-9][0-9]*)([[:space:]].*)?$/\\3/p' \"$REVIEW_FILE\")",
    ],
    "cc_research": [
        "Bash(find .cache*)",
        "Bash(find .experiments*)",
        "Bash(find .notes*)",
        "Bash(find .plans*)",
        "Bash(find .reports*)",
        "Bash(find .temp*)",
        'Bash(find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md")',
        'Bash(find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/web-explorer.md")',
        'Bash(find ~/.claude/plugins/cache -path "*/foundry*" -name "solution-architect.md")',
        "Bash(node --check:*)",
    ],
    "codemap-py": [
        "Bash(find .cache*)",
        "Bash(find .notes*)",
        "Bash(find .plans*)",
        "Bash(find .reports*)",
        "Bash(find .temp*)",
        "Bash(node --check:*)",
        "Bash(sed 's/\\./\\\\./g')",
    ],
}
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

    assert (NEW_RULE in rules) is (plugin not in TRIMMED_ALLOW_PLUGINS)
    assert OLD_RULE not in rules
    assert not [rule for rule in rules if rule.endswith(":*)") and "*" in rule[:-3]]


@pytest.mark.parametrize("plugin", PLUGINS)
def test_shipped_allow_rules_preserve_the_foundry_only_guard_boundary(plugin: str) -> None:
    """Sibling lists omit write-capable allows; Foundry and Codemap retain their accepted allows."""
    rules = set(_plugin_json(plugin, "permissions-allow.json"))
    if plugin in TRIMMED_ALLOW_PLUGINS:
        assert rules.isdisjoint(GUARDED_ALLOW_RULES)
    else:
        assert GUARDED_ALLOW_RULES <= rules


@pytest.mark.parametrize("plugin", PLUGINS)
def test_shell_permissions_keep_only_scoped_commands(plugin: str) -> None:
    """Retired command families, eval included, must prompt while Python and scoped utilities stay available."""
    rules = set(_plugin_json(plugin, "permissions-allow.json"))
    assert rules.isdisjoint(RETIRED_SHELL_RULES)
    assert {rule for rule in rules if rule.startswith(("Bash(find", "Bash(node", "Bash(claude", "Bash(sed"))} == set(
        NARROWED_SHELL_RULES[plugin]
    )
    assert {"Bash(python:*)", "Bash(node --check:*)", "Bash(find .temp*)"} <= rules
    assert not any(
        "sed -i" in rule or ("find " in rule and any(flag in rule for flag in ["-delete", "-exec"])) for rule in rules
    )


def test_no_plugin_ships_retired_shell_allows() -> None:
    """Installing any plugin must not silently restore the retired broad command grants."""
    sources = list((ROOT / "plugins").glob("*/.claude-plugin/permissions-allow.json"))
    assert sources
    assert {
        str(path): sorted(RETIRED_SHELL_RULES.intersection(json.loads(path.read_text())))
        for path in sources
        if RETIRED_SHELL_RULES.intersection(json.loads(path.read_text()))
    } == {}


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


#: Shared script every setup runs to merge its shipped permission lists; its call line is the executable contract.
MERGE_SCRIPT = "bin/merge_settings_permissions.py"
#: Flags of that call belonging to each array, so one merge can be executed without touching the other.
MERGE_FLAGS = {"allow": ("--allow", "--drop-allow"), "deny": ("--deny", "--retire-deny-file")}


def _setup_source(plugin: str) -> tuple[Path, str]:
    """Return a plugin's root and the text of the file documenting its permission merge."""
    root = ROOT / "plugins" / plugin
    source = root / ("README.md" if plugin == "codemap-py" else "skills/setup/SKILL.md")
    return root, source.read_text(encoding="utf-8")


def _merge_call_flags(text: str, root: Path) -> dict[str, list[str]]:
    """Return ``{flag: values}`` from the ``python …/merge_settings_permissions.py`` lines of a setup; empty if none.

    Flags are read from the shipped call itself, so a lost ``--drop-allow`` or ``--retire-deny-file`` changes the
    executed migration and fails the assertions below. ``$PLUGIN_ROOT`` expands to the plugin under test.
    """
    flags: dict[str, list[str]] = {}
    for line in text.replace("\\\n", " ").splitlines():
        if not (line.lstrip().startswith("python ") and MERGE_SCRIPT in line):
            continue
        tokens = shlex.split(line.split(" || ", 1)[0].replace("$PLUGIN_ROOT", root.as_posix()))
        for flag, value in pairwise(tokens):
            if flag.startswith("--"):
                flags.setdefault(flag, []).append(value)
    return flags


def _run_merge_script(
    root: Path, flags: dict[str, list[str]], kind: str, settings: dict[str, object], tmp_path: Path
) -> tuple[dict[str, object], dict[str, object]]:
    """Execute the shipped ``kind`` ("allow" or "deny") merge twice on isolated settings under a temporary home."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    target = home / ".claude" / "settings.json"
    target.write_text(json.dumps(settings), encoding="utf-8")
    command = [sys.executable, str(root / MERGE_SCRIPT), "--settings", str(target)]
    for flag in MERGE_FLAGS[kind]:
        for value in flags.get(flag, []):
            command += [flag, value]
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    first = json.loads(target.read_text(encoding="utf-8"))
    subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    return first, json.loads(target.read_text(encoding="utf-8"))


def _run_merge_jq(
    root: Path, text: str, kind: str, settings: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """Execute the inline jq ``kind`` merge a setup still documents (Codemap's manual merge) twice."""
    expressions = re.findall(rf"'([^'\n]*\.permissions\.{kind} = [^'\n]*)'", text)
    assert len(expressions) == 1, f"setup must expose one executable {kind} merge"
    meta = root / ".claude-plugin"
    command = [JQ, "--slurpfile", "perms" if kind == "allow" else kind, str(meta / f"permissions-{kind}.json")]
    retired = meta / "permissions-deny-retired.json"
    if kind == "deny" and retired.exists():
        command += ["--slurpfile", "retired", str(retired)]
    command.append(expressions[0])
    first = subprocess.run(command, input=json.dumps(settings), capture_output=True, text=True, check=True)
    second = subprocess.run(command, input=first.stdout, capture_output=True, text=True, check=True)
    return json.loads(first.stdout), json.loads(second.stdout)


def _expected_merge(rules: list[str], removed: set[str]) -> list[str]:
    """Return the array a shipped merge should produce from ``rules`` (existing then shipped) minus ``removed``.

    ``merge_settings_permissions.py`` and Codemap's documented inline jq merge both keep first occurrences in order, so
    user rules keep their order.
    """
    return [rule for rule in dict.fromkeys(rules) if rule not in removed]


def _apply_setup_merge(
    plugin: str, settings: dict[str, object], kind: str, tmp_path: Path
) -> tuple[dict[str, object], dict[str, object]]:
    """Run one plugin's shipped ``kind`` merge twice and return the settings after each run.

    A setup that calls ``merge_settings_permissions.py`` has that call executed with only the flags of ``kind``; one
    that still documents an inline jq filter (Codemap) has the filter executed. Dispatch follows the source text, so a
    plugin switches paths the moment its setup adopts the script.
    """
    root, text = _setup_source(plugin)
    flags = _merge_call_flags(text, root)
    if flags:
        return _run_merge_script(root, flags, kind, settings, tmp_path)
    return _run_merge_jq(root, text, kind, settings)


@pytest.mark.integration
@pytest.mark.skipif(JQ is None, reason="jq is required to execute the shipped setup migration")
@pytest.mark.parametrize("plugin", PLUGINS)
@pytest.mark.parametrize(
    "settings",
    [
        pytest.param({}, id="first-install"),
        pytest.param(
            {
                "permissions": {
                    "allow": [
                        *sorted(RETIRED_SHELL_RULES),
                        *sorted(RETIRED_SHELL_RULES),
                        "Bash(find .temp*)",
                        "Bash(node --check:*)",
                        "Bash(sed -n custom)",
                        "Bash(claude plugin list)",
                        "Bash(python:*)",
                    ]
                },
                "theme": "dark",
            },
            id="retired-shell-rules-including-identical-user-grants",
        ),
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
def test_setup_migrates_only_obsolete_rule(plugin: str, settings: dict[str, object], tmp_path: Path) -> None:
    """Setup must remove only the exact rules that plugin retired, preserve user settings, and remain idempotent.

    Only oss shipped ``Bash(uv pip install:*)``, so only oss setup removes it; every other plugin's setup keeps a user's
    own copy of that rule untouched.
    """
    root = ROOT / "plugins" / plugin
    migrated, again = _apply_setup_merge(plugin, settings, "allow", tmp_path)

    shipped = json.loads((root / ".claude-plugin/permissions-allow.json").read_text())
    original_permissions = settings.get("permissions", {})
    original_allow = original_permissions.get("allow", [])
    assert migrated["permissions"]["allow"] == _expected_merge(original_allow + shipped, RETIRED_RULES[plugin])
    assert (NEW_RULE in migrated["permissions"]["allow"]) is (
        plugin not in TRIMMED_ALLOW_PLUGINS or NEW_RULE in original_allow
    )
    assert again == migrated
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
    ``gh-write`` approval spend its token and still be denied. Sibling lists omit the corresponding broad allows.
    """
    deny = _plugin_json(plugin, "permissions-deny.json")
    assert [rule for rule in deny if rule.startswith("Bash(gh ") or rule in RETIRED_LOCAL_DENY] == []
    assert [rule for rule in KEPT_FORCE_DENY if rule not in deny] == []


@pytest.mark.parametrize("plugin", PLUGINS)
def test_only_foundry_ships_the_retired_deny_list(plugin: str) -> None:
    """Foundry is the sole scrub point for stale gh and branch/tag deletion denies."""
    path = ROOT / "plugins" / plugin / ".claude-plugin/permissions-deny-retired.json"
    assert path.exists() is (plugin in RETIRED_DENY_PLUGINS)
    if plugin not in RETIRED_DENY_PLUGINS:
        return
    deny = _plugin_json(plugin, "permissions-deny.json")
    retired = _plugin_json(plugin, "permissions-deny-retired.json")
    assert {RETIRED_GH_DENY, *RETIRED_LOCAL_DENY} <= set(retired)
    assert [
        rule for rule in retired if rule in deny or not (rule.startswith("Bash(gh ") or rule in RETIRED_LOCAL_DENY)
    ] == []


@pytest.mark.integration
@pytest.mark.skipif(JQ is None, reason="jq is required to execute the shipped setup migration")
@pytest.mark.parametrize("plugin", RETIRED_DENY_PLUGINS)
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
def test_setup_retires_only_the_shipped_gh_write_denies(
    plugin: str, settings: dict[str, object], tmp_path: Path
) -> None:
    """Setup removes exactly the retired gh write denies, adds the shipped ones, keeps the rest, and is idempotent.

    A user's own deny entry that only resembles a retired one (``Bash(gh pr merge --admin:*)``) stays: removal is an
    exact string match against the plugin's retired list.
    """
    migrated, again = _apply_setup_merge(plugin, settings, "deny", tmp_path)

    original_deny = settings.get("permissions", {}).get("deny", [])
    shipped = _plugin_json(plugin, "permissions-deny.json")
    retired = set(_plugin_json(plugin, "permissions-deny-retired.json"))
    assert migrated["permissions"]["deny"] == _expected_merge(original_deny + shipped, retired)
    assert again == migrated
    expected = json.loads(json.dumps(settings))
    expected.setdefault("permissions", {})["deny"] = migrated["permissions"]["deny"]
    assert migrated == expected


@pytest.mark.integration
@pytest.mark.skipif(JQ is None, reason="jq is required to execute the shipped setup migration")
@pytest.mark.parametrize("plugin", [plugin for plugin in PLUGINS if plugin not in RETIRED_DENY_PLUGINS])
def test_other_plugins_merge_denies_additively(plugin: str, tmp_path: Path) -> None:
    """Sibling setup and Codemap's manual merge preserve stale and custom denies for Foundry to scrub."""
    settings = {
        "permissions": {"deny": [RETIRED_GH_DENY, *RETIRED_LOCAL_DENY, "Bash(custom-deny:*)"]},
        "env": {"CUSTOM_SETTING": "preserved"},
    }
    migrated, again = _apply_setup_merge(plugin, settings, "deny", tmp_path)
    expected = {
        **settings,
        "permissions": {
            "deny": _expected_merge(
                settings["permissions"]["deny"] + _plugin_json(plugin, "permissions-deny.json"), set()
            )
        },
    }
    assert migrated == expected
    assert again == expected
