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
from collections.abc import Callable
from functools import cache
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
OTHER_DENY_PLUGINS = [plugin for plugin in PLUGINS if plugin not in RETIRED_DENY_PLUGINS]
TRIMMED_ALLOW_PLUGINS = ["cc_oss", "cc_develop", "cc_research"]
UNTRIMMED_ALLOW_PLUGINS = [plugin for plugin in PLUGINS if plugin not in TRIMMED_ALLOW_PLUGINS]
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
#: Shared script every setup runs to merge its shipped permission lists; its call line is the executable contract.
MERGE_SCRIPT = "bin/merge_settings_permissions.py"
#: Flags of that call belonging to each array, so one merge can be executed without touching the other.
MERGE_FLAGS = {"allow": ("--allow", "--drop-allow", "--drop-allow-file"), "deny": ("--deny", "--retire-deny-file")}
#: PreToolUse hook that blocks a ``python`` Bash call whose arguments name ``gh`` with ``api`` or a write verb.
GH_WRITE_GUARD = ROOT / "plugins" / "cc_foundry" / "hooks" / "gh-write-guard.js"
#: Plugins whose setup skill runs the shared permissions merge script.
MERGE_SCRIPT_PLUGINS = ["cc_foundry", "cc_oss", "cc_develop", "cc_research"]
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

_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to run the gh-write guard")
_requires_jq = pytest.mark.skipif(JQ is None, reason="jq is required to execute the shipped setup migration")
_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _plugin_json(plugin: str, name: str) -> list[str]:
    """Return one of a plugin's shipped ``.claude-plugin`` permission lists."""
    return json.loads((ROOT / "plugins" / plugin / ".claude-plugin" / name).read_text(encoding="utf-8"))


@cache
def _overlay_commands() -> tuple[str, ...]:
    """Return every overlay command the oss review template prescribes, as Claude Code would match it.

    A leading environment assignment is kept and also stripped: Claude Code strips assignments of certain known-safe
    variables before matching allow rules, so the check covers the command with and without its prefix.
    """
    text = OVERLAY_TEMPLATE.read_text(encoding="utf-8")
    spans = re.findall(r"`((?:uv pip install|PIP_CONFIG_FILE=)[^`]* --[^`]*)`", text)
    commands = [span.replace("<env-python>", ENV_PYTHON) for span in spans]
    return tuple(commands + [re.sub(r"^[A-Z_]+=\S+ ", "", command) for command in commands])


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


def _fenced_blocks(text: str) -> list[str]:
    """Return the body of every fenced code block in ``text``, one string per block, each line newline-terminated.

    A fence opens with three or more backticks or tildes (an info string may follow) and closes on a line holding only
    that character, at least as many times as it opened.

    Examples:
        >>> _fenced_blocks("a\\n```bash\\necho hi\\n```\\nb\\n~~~~\\nx\\n~~~~~\\n")
        ['echo hi\\n', 'x\\n']
    """
    blocks: list[str] = []
    body: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            fence = opened.group(1) if opened else None
            body = []
        elif len(stripped) >= len(fence) and stripped == fence[0] * len(stripped):
            blocks.append("".join(f"{row}\n" for row in body))
            fence = None
        else:
            body.append(line)
    return blocks


def _setup_source(plugin: str) -> tuple[Path, str]:
    """Return a plugin's root and the text of the file documenting its permission merge."""
    root = ROOT / "plugins" / plugin
    source = root / ("README.md" if plugin == "codemap-py" else "skills/setup/SKILL.md")
    return root, source.read_text(encoding="utf-8")


def _merge_blocks(plugin: str) -> tuple[Path, str, list[str]]:
    """Return a plugin's root, its setup text, and the fenced blocks of that text that run the merge script."""
    root, text = _setup_source(plugin)
    return root, text, [block for block in _fenced_blocks(text) if MERGE_SCRIPT in block]


def _merge_call_flags(text: str, root: Path) -> dict[str, list[str]]:
    """Return ``{flag: values}`` from the ``python …/merge_settings_permissions.py`` lines of a setup; empty if none.

    Flags are read from the shipped call itself, so a lost ``--drop-allow-file`` or ``--retire-deny-file`` changes the
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


def _guard_exit_code(command: str) -> tuple[int, str]:
    """Return the gh-write guard's exit code and stderr for ``command`` sent as a Bash PreToolUse payload."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(ROOT)})
    result = subprocess.run(
        ["node", str(GH_WRITE_GUARD)], input=payload, capture_output=True, encoding="utf-8", check=False
    )
    return result.returncode, result.stderr


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
    runs = []
    for _ in range(2):
        subprocess.run(command, env=env, capture_output=True, encoding="utf-8", check=True)
        runs.append(json.loads(target.read_text(encoding="utf-8")))
    return runs[0], runs[1]


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
    first = subprocess.run(command, input=json.dumps(settings), capture_output=True, encoding="utf-8", check=True)
    second = subprocess.run(command, input=first.stdout, capture_output=True, encoding="utf-8", check=True)
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


class TestShippedAllowRules:
    """The allow lists each plugin ships keep scoped commands, wildcard syntax and the Foundry-only guard boundary."""

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_repository_api_rule_uses_wildcard_syntax(self, plugin: str) -> None:
        """Repository API permissions avoid mixing wildcard and legacy prefix syntax.

        Trimmed sibling lists omit the ``gh api repos`` allow entirely; the others ship only the wildcard spelling and
        never a ``:*`` rule that also contains a mid-pattern ``*``.
        """
        rules = _plugin_json(plugin, "permissions-allow.json")

        assert (NEW_RULE in rules) is (plugin not in TRIMMED_ALLOW_PLUGINS)
        assert OLD_RULE not in rules
        assert [rule for rule in rules if rule.endswith(":*)") and "*" in rule[:-3]] == []

    @pytest.mark.parametrize("plugin", TRIMMED_ALLOW_PLUGINS)
    def test_trimmed_siblings_omit_write_capable_allows(self, plugin: str) -> None:
        """Sibling allow lists carry none of the write-capable gh, branch and tag allows.

        Only Foundry's gh write guard and Codemap own those allows; a sibling shipping one would bypass the guard.
        """
        rules = set(_plugin_json(plugin, "permissions-allow.json"))

        assert rules.isdisjoint(GUARDED_ALLOW_RULES)

    @pytest.mark.parametrize("plugin", UNTRIMMED_ALLOW_PLUGINS)
    def test_foundry_and_codemap_keep_accepted_allows(self, plugin: str) -> None:
        """Foundry and Codemap retain the write-capable allows their guard boundary accepts."""
        rules = set(_plugin_json(plugin, "permissions-allow.json"))

        assert GUARDED_ALLOW_RULES <= rules

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_shell_permissions_keep_only_scoped_commands(self, plugin: str) -> None:
        """Retired command families, eval included, prompt while Python and scoped utilities stay available.

        The ``find``, ``node``, ``claude`` and ``sed`` rules must equal the plugin's narrowed list exactly, and none may
        permit in-place edits, deletion or ``-exec``.
        """
        rules = set(_plugin_json(plugin, "permissions-allow.json"))
        scoped = {rule for rule in rules if rule.startswith(("Bash(find", "Bash(node", "Bash(claude", "Bash(sed"))}
        mutating = [
            rule for rule in rules if "sed -i" in rule or ("find " in rule and ("-delete" in rule or "-exec" in rule))
        ]

        assert rules.isdisjoint(RETIRED_SHELL_RULES)
        assert scoped == set(NARROWED_SHELL_RULES[plugin])
        assert {"Bash(python:*)", "Bash(node --check:*)", "Bash(find .temp*)"} <= rules
        assert mutating == []

    def test_no_plugin_ships_retired_shell_allows(self) -> None:
        """Installing any plugin must not silently restore the retired broad command grants.

        Every shipped allow list under ``plugins/`` is scanned, including plugins outside the setup-merge set.
        """
        sources = sorted((ROOT / "plugins").glob("*/.claude-plugin/permissions-allow.json"))
        shipped_retired = {
            str(path.relative_to(ROOT)): sorted(RETIRED_SHELL_RULES & set(json.loads(path.read_text(encoding="utf-8"))))
            for path in sources
        }

        assert sources
        assert {path: rules for path, rules in shipped_retired.items() if rules} == {}


class TestOverlayFetchRules:
    """No shipped allow rule pre-approves the oss review overlay's download and install commands."""

    def test_command_extraction_finds_every_fetch_step(self) -> None:
        """The template still spells the support check, fetch and both pip fallback commands where the scan finds them.

        Without this guard a reworded template would leave the allow-list cross-check below scanning nothing and
        passing.
        """
        commands = _overlay_commands()

        assert [command.split(" --", 1)[0] for command in commands[:4]] == [
            "uv pip install",
            "uv pip install",
            f"PIP_CONFIG_FILE=/dev/null {ENV_PYTHON} -m pip download",
            f"PIP_CONFIG_FILE=/dev/null {ENV_PYTHON} -m pip install",
        ]

    @pytest.mark.parametrize(
        ("rule", "command_prefix", "matches"),
        [
            pytest.param(OVERLAY_INSTALL_RULE, "uv pip install", True, id="retired-install-rule"),
            pytest.param("Bash(uv pip *)", "uv pip install", True, id="space-wildcard"),
            pytest.param("Bash(uv*)", "uv pip install", True, id="glued-wildcard"),
            pytest.param("Bash(uv pip list:*)", "uv pip install", False, id="read-only-sibling"),
            pytest.param(
                f"Bash({ENV_PYTHON} -m pip:*)",
                f"{ENV_PYTHON} -m pip download",
                True,
                id="env-stripped-pip-fallback-is-checked",
            ),
            pytest.param(
                "Bash(python:*)", f"{ENV_PYTHON} -m pip download", False, id="interpreter-prefix-misses-venv-python"
            ),
        ],
    )
    def test_matcher_reports_rules_covering_an_overlay_command(
        self, rule: str, command_prefix: str, matches: bool
    ) -> None:
        """The matcher behind the cross-check reports rules that cover a shipped overlay command, and only those.

        The first command starting with ``uv pip install`` is the support check; the one starting with the bare
        environment interpreter is the pip-fallback download with its environment prefix stripped. A matcher that never
        matched would let the cross-check pass even with the retired rule still shipped.
        """
        command = next(command for command in _overlay_commands() if command.startswith(command_prefix))

        assert _bash_rule_matches(rule, command) is matches

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_no_shipped_allow_rule_preapproves_overlay_fetch(self, plugin: str) -> None:
        """No plugin's shipped allow list auto-approves the review overlay's download commands or their families.

        The oss review contract makes each overlay command's permission prompt the user's approval; a merged ``Bash(uv
        pip install:*)`` rule ran the support check and the wheel fetch with no prompt, and also covered installs into
        the project environment and source builds the overlay forbids.
        """
        rules = _plugin_json(plugin, "permissions-allow.json")
        commands = [*_overlay_commands(), *FORBIDDEN_COMMAND_FAMILIES]

        assert [(rule, command) for rule in rules for command in commands if _bash_rule_matches(rule, command)] == []


class TestSetupMergeGuard:
    """Each shipped setup merge block reaches ``settings.json`` past the gh-write guard."""

    @pytest.mark.parametrize("plugin", MERGE_SCRIPT_PLUGINS)
    def test_setup_documents_one_merge_block(self, plugin: str) -> None:
        """Each setup skill holds exactly one fenced block running the merge script, with every call inside it.

        The guard test below runs that single block; a second block or a call outside it would go unexercised.
        """
        _, text, blocks = _merge_blocks(plugin)
        calls = [line.strip() for line in text.splitlines() if MERGE_SCRIPT in line]

        assert len(blocks) == 1
        assert [call for call in calls if call not in blocks[0]] == []

    @pytest.mark.integration
    @_requires_node
    @pytest.mark.parametrize(
        "expand",
        [
            pytest.param(lambda root: "$PLUGIN_ROOT", id="unexpanded"),
            pytest.param(lambda root: root.as_posix(), id="expanded"),
        ],
    )
    @pytest.mark.parametrize("plugin", MERGE_SCRIPT_PLUGINS)
    def test_merge_call_passes_gh_write_guard(self, plugin: str, expand: Callable[[Path], str]) -> None:
        """Each shipped setup merge block, as the Bash tool receives it, is not blocked by the gh-write guard.

        The guard reads every argument of a ``python`` call as interpreter code and blocks one that names ``gh api``, so
        a retired ``Bash(gh api …)`` rule on the command line stops setup before ``settings.json`` is merged. The whole
        fenced block, continuation lines included, is run through the real guard with ``$PLUGIN_ROOT`` left as written
        and expanded, so the symptom cannot return in one plugin while another still passes.
        """
        root, _, blocks = _merge_blocks(plugin)
        command = blocks[0].replace("$PLUGIN_ROOT", expand(root))

        code, stderr = _guard_exit_code(command)

        assert (code, stderr) == (0, "")

    @pytest.mark.integration
    @_requires_node
    def test_guard_blocks_a_python_call_naming_gh_api(self) -> None:
        """The guard still blocks a ``python`` call carrying ``gh api`` text, so the shipped-call test can fail.

        Without this control a guard that stopped inspecting ``python`` arguments would make the shipped-call test pass
        for any command line and the retired-rule workaround would look verified when it is not needed.
        """
        command = f"python bin/merge_settings_permissions.py --allow a.json --drop-allow '{OLD_RULE}'"

        code, _ = _guard_exit_code(command)

        assert code == 2


class TestAllowMigration:
    """Setup removes each plugin's exact retired allow rules while preserving everything else."""

    @pytest.mark.integration
    @_requires_jq
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
    def test_migrates_only_obsolete_rule(self, plugin: str, settings: dict[str, object], tmp_path: Path) -> None:
        """Setup removes only the exact rules that plugin retired, preserves user settings, and is idempotent.

        Only oss shipped ``Bash(uv pip install:*)``, so only oss setup removes it; every other plugin's setup keeps a
        user's own copy of that rule untouched. The shipped merge is executed for real, twice, on isolated settings.
        """
        migrated, again = _apply_setup_merge(plugin, settings, "allow", tmp_path)

        shipped = _plugin_json(plugin, "permissions-allow.json")
        original_allow = settings.get("permissions", {}).get("allow", [])
        expected = json.loads(json.dumps(settings))
        expected.setdefault("permissions", {})["allow"] = migrated["permissions"]["allow"]
        assert migrated["permissions"]["allow"] == _expected_merge(original_allow + shipped, RETIRED_RULES[plugin])
        assert (NEW_RULE in migrated["permissions"]["allow"]) is (
            plugin not in TRIMMED_ALLOW_PLUGINS or NEW_RULE in original_allow
        )
        assert again == migrated
        assert migrated == expected


class TestDenyLists:
    """Shipped deny lists keep every force-push deny, drop gh write denies, and migrate through Foundry's scrub."""

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_shipped_list_holds_no_retired_entry(self, plugin: str) -> None:
        """No shipped deny entry names a gh command or a local branch/tag deletion; every force push deny stays.

        A settings deny rule still applies after a hook allows a call, so a shipped gh write deny would make every one-
        time ``gh-write`` approval spend its token and still be denied. Sibling lists omit the corresponding broad
        allows.
        """
        deny = _plugin_json(plugin, "permissions-deny.json")

        assert [rule for rule in deny if rule.startswith("Bash(gh ") or rule in RETIRED_LOCAL_DENY] == []
        assert [rule for rule in KEPT_FORCE_DENY if rule not in deny] == []

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_only_foundry_ships_the_retired_list(self, plugin: str) -> None:
        """Foundry is the sole scrub point for stale gh and branch/tag deletion denies."""
        path = ROOT / "plugins" / plugin / ".claude-plugin/permissions-deny-retired.json"

        assert path.exists() is (plugin in RETIRED_DENY_PLUGINS)

    @pytest.mark.parametrize("plugin", RETIRED_DENY_PLUGINS)
    def test_retired_list_names_only_gh_and_local_deletion_denies(self, plugin: str) -> None:
        """The retired list covers the known gh and deletion denies and never a deny the plugin still ships.

        Removal is an exact string match, so a retired entry that is also shipped would be added and removed in the same
        merge, and a non-gh, non-deletion entry would scrub a rule that should stay.
        """
        deny = _plugin_json(plugin, "permissions-deny.json")
        retired = _plugin_json(plugin, "permissions-deny-retired.json")

        assert {RETIRED_GH_DENY, *RETIRED_LOCAL_DENY} <= set(retired)
        assert [
            rule for rule in retired if rule in deny or not (rule.startswith("Bash(gh ") or rule in RETIRED_LOCAL_DENY)
        ] == []

    @pytest.mark.integration
    @_requires_jq
    @pytest.mark.parametrize("plugin", RETIRED_DENY_PLUGINS)
    @pytest.mark.parametrize(
        "settings",
        [
            pytest.param({}, id="first-install"),
            pytest.param(
                {"permissions": {"deny": [RETIRED_GH_DENY, RETIRED_GH_DENY, KEPT_DENY]}}, id="retired-and-kept"
            ),
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
        self, plugin: str, settings: dict[str, object], tmp_path: Path
    ) -> None:
        """Setup removes exactly the retired gh write denies, adds the shipped ones, keeps the rest, and is idempotent.

        A user's own deny entry that only resembles a retired one (``Bash(gh pr merge --admin:*)``) stays: removal is an
        exact string match against the plugin's retired list.
        """
        migrated, again = _apply_setup_merge(plugin, settings, "deny", tmp_path)

        original_deny = settings.get("permissions", {}).get("deny", [])
        shipped = _plugin_json(plugin, "permissions-deny.json")
        retired = set(_plugin_json(plugin, "permissions-deny-retired.json"))
        expected = json.loads(json.dumps(settings))
        expected.setdefault("permissions", {})["deny"] = migrated["permissions"]["deny"]
        assert migrated["permissions"]["deny"] == _expected_merge(original_deny + shipped, retired)
        assert again == migrated
        assert migrated == expected

    @pytest.mark.integration
    @_requires_jq
    @pytest.mark.parametrize("plugin", OTHER_DENY_PLUGINS)
    def test_other_plugins_merge_denies_additively(self, plugin: str, tmp_path: Path) -> None:
        """Sibling setup and Codemap's manual merge preserve stale and custom denies for Foundry to scrub."""
        settings = {
            "permissions": {"deny": [RETIRED_GH_DENY, *RETIRED_LOCAL_DENY, "Bash(custom-deny:*)"]},
            "env": {"CUSTOM_SETTING": "preserved"},
        }
        expected_deny = _expected_merge(
            settings["permissions"]["deny"] + _plugin_json(plugin, "permissions-deny.json"), set()
        )

        migrated, again = _apply_setup_merge(plugin, settings, "deny", tmp_path)

        expected = {**settings, "permissions": {"deny": expected_deny}}
        assert migrated == expected
        assert again == expected
