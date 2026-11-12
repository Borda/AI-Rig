# Skill Checks — Gaps (22, 40)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 22 — Calibration coverage gap

**Step 1 — Read calibrate domain table**: Load calibrate `skills.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof), extract registered target list under `### Domain table`. Build registered-targets set.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "$PLUGIN_ROOT/bin/load_mode.py" calibrate modes skills.md $([ "$LOCAL_MODE" = "true" ] && echo --local) --fallback-source  # timeout: 5000
```

**Step 2 — Scan all skill modes on disk**: In local mode, enumerate `plugins/*/.claude-plugin/plugin.json`, parse each JSON `name` as the routing prefix and inspect that plugin root's `skills/*/SKILL.md`, `claude-skills/*/SKILL.md` and their `modes/*.md`. In non-local mode, read `~/.claude/plugins/installed_plugins.json` for installed marketplace/plugin identities, then invoke the audit plugin's own `bin/get_plugin_install_path.py <marketplace> <plugin-name>` for each identity and inspect the same subdirectories under its resolved active install path. Also include directly installed `.claude/skills/`. Do not infer routing prefixes from directory names or treat an unavailable install record as a missing target. Extract mode names from `argument-hint:` frontmatter and `## Mode:` / `### Mode:` headings.

**Step 3 — Validate registered targets exist on disk**: For each registered target, resolve its routing prefix through the manifest/install map from Step 2, then verify the matching skill/mode file exists. Record unavailable resolution as unverified rather than a stale target. Registered target with no matching file → **medium** (calibrate fails at runtime).

**Step 4 — Identify unregistered calibratable candidates** (model reasoning):

Mode is calibratable when ALL three signals present:

1. **Deterministic structured output**: findings list, completeness checklist, structured table, or machine-readable verdict
2. **Synthetic input feasible**: testable without external services
3. **Ground truth constructable**: known issues injectable and scorable

→ Unregistered mode matching all three: **low** (add to `calibrate/modes/skills.md` domain table)

**Step 5 — Read agents domain table**: Load calibrate `agents.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof), extract all agent names from `### Domain table`. Build registered-agent-names set.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "$PLUGIN_ROOT/bin/load_mode.py" calibrate modes agents.md $([ "$LOCAL_MODE" = "true" ] && echo --local) --fallback-source  # timeout: 5000
```

**Step 6 — Scan all agent files on disk**: Reuse Step 2's Claude manifest/active-install map, scan each resolved root's `agents/*.md` and qualify each basename with that manifest's JSON `name`. Also scan `.claude/agents/*.md` as bare names. In non-local mode, never infer installed agents from a project-local `plugins/` tree. Build the full discovered-agent set.

**Step 7 — Validate registered agents exist on disk**: For each registered agent in domain table, verify it resolves to discovered file. Bare name (e.g. `sw-engineer`) matches `foundry:sw-engineer` when no `.claude/agents/sw-engineer.md` exists — apply model reasoning to resolve bare names against plugin-qualified discoveries. Registered agent with no matching file → **medium** (stale entry causes calibrate to fail at runtime; remove from domain table or correct prefix).

**Step 8 — Identify unregistered agents**: For each discovered agent not in domain table, apply same three-signal calibratability test from Step 4. → Unregistered calibratable agent: **low** (add to `calibrate/modes/agents.md` domain table with appropriate domain string).

## Check 40 — Health monitoring gap

A Claude skill that actually dispatches `Agent(subagent_type=...)` must implement the deadline protocol in `agent-spawn-protocol.md` §Deadlines. Scan the owner's `SKILL.md` and child Markdown executed by that owner, excluding spawn-prompt templates (`templates/*prompt*.md`) and the audit skill's `templates/checks-*.md`; inherited references in that skill's `SKILL.md` apply to its mode and template files. Accept `agent-spawn-protocol`, `agent_watch`, or an explicit `agent-resolution.md` / `Agent waits` reference as protocol routing; inspect the routed contract during review. Mere prose mentioning `Agent()` is not a dispatch.

Local scope is only `skills/` and `claude-skills/` under plugins carrying `.claude-plugin/plugin.json`; non-local scope is directly installed `.claude/skills/`. Codex and OpenCode skills use their own host contracts.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 40: Health monitoring gap ===\n"
while IFS= read -r owner; do  # timeout: 5000
    while IFS= read -r f; do
        tr '\n' ' ' < "$f" | grep -qE 'Agent[[:space:]]*\(.{0,200}.{0,200}subagent_type[[:space:]]*=' || continue
        grep -qiE 'agent-spawn-protocol|agent_watch|agent-resolution\.md|Agent waits' "$owner" "$f" 2>/dev/null || printf "C40-HIGH: agent spawn without deadline protocol: %s (owner %s)\n" "$f" "$owner"
        grep -qE 'health_sentinel|find.*-newer.*wc -l' "$f" 2>/dev/null && printf "C40-MEDIUM: retired liveness poll still prescribed: %s (owner %s)\n" "$f" "$owner"
    done < <(find "$(dirname "$owner")" -type f -name '*.md' ! -path '*/templates/*prompt*.md' ! -path '*/audit/templates/checks-*.md' 2>/dev/null | sort)
done < <(
if [ "$LOCAL_MODE" = "true" ]; then
    while IFS= read -r manifest; do
        [ -f "$manifest" ] || continue
        plugin_root=$(dirname "$(dirname "$manifest")")
        find "$plugin_root/skills" "$plugin_root/claude-skills" -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
    done < <(find plugins -mindepth 3 -maxdepth 3 -type f -path '*/.claude-plugin/plugin.json' 2>/dev/null | sort)
else
    find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
fi | sort
)
```

**Severity**: high — actual dispatch lacks protocol routing; medium — retired liveness polling remains. Fix: load the plugin's own `agent-spawn-protocol.md` §Deadlines, arm `agent-watch-<batch>.tsv` in the spawn response, check `agent_watch.py` once per wake-up and surface ⏱ immediately for missing deliverables. Review routed contracts before concluding coverage; a filename mention alone does not prove execution.
