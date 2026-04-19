---
name: init
description: Post-install setup for foundry plugin. Merges statusLine, permissions.allow, and enabledPlugins into ~/.claude/settings.json; symlinks rules and TEAM_PROTOCOL.md into ~/.claude/.
allowed-tools: Read, Write, Bash, AskUserQuestion
effort: low
model: sonnet
argument-hint: '[--approve]'
---

<objective>

Set up foundry on new machine:

| Action | What happens |
| --- | --- |
| Merge `statusLine`, `permissions.allow`, `enabledPlugins` → `~/.claude/settings.json` | ✓ |
| `rules/*.md` → `~/.claude/rules/` | symlink |
| `TEAM_PROTOCOL.md` → `~/.claude/` | symlink |
| `hooks/hooks.json` | auto — plugin system |
| Conflict review before overwriting existing user files | ✓ |

**Why symlink rules (not copy)?** Rules and TEAM_PROTOCOL.md load at session startup. Symlinks = every session gets plugin's current version — no stale copies, no re-run after upgrades. Broken symlink after upgrade = obvious error; stale copy = silently serves old content.

**Why not symlink agents and skills?** Claude Code plugin system already exposes all plugin skills and agents at root namespace. Agents must always use full plugin prefix (`foundry:sw-engineer`, not `sw-engineer`) for unambiguous dispatch regardless of symlinks. Init creates no agent or skill symlinks.

**Why hooks need no action?** `hooks/hooks.json` inside plugin registers automatically when plugin enabled. Init's only hook-adjacent step: write `statusLine.command` path (Step 3) — `statusLine` is top-level settings key, not part of `hooks.json`.

NOT for: editing project `.claude/settings.json`.

</objective>

<inputs>

- **No arguments** — interactive mode; prompts on conflicts.
- **`--approve`** — non-interactive mode; auto-accepts all recommended answers. Use for scripted or CI setups.

</inputs>

<workflow>

## Flag detection

Parse `$ARGUMENTS` for `--approve` (case-insensitive). If found, set `APPROVE_ALL=true`; else `APPROVE_ALL=false`.

When `APPROVE_ALL=true`, every `AskUserQuestion` below **skipped** — ★ recommended option applied automatically. Print `[--approve] auto-accepting recommended option` in place of question.

## Step 1: Locate the installed plugin

Read `~/.claude/plugins/installed_plugins.json` using Read tool. Find entry whose key contains `foundry` (case-insensitive). Extract its `installPath`. If file missing or no foundry entry, fall back to filesystem scan:

```bash
PLUGIN_ROOT=$(jq -r 'to_entries[] | select(.key | ascii_downcase | contains("foundry")) | .value.installPath // empty' \
    "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null | head -1)  # timeout: 5000

# Fallback when registry entry is absent (manual cache copies, partial installs)
if [ -z "$PLUGIN_ROOT" ]; then
    PLUGIN_ROOT=$(find ~/.claude/plugins/cache -maxdepth 5 -name "plugin.json" 2>/dev/null \
            | xargs grep -l 'foundry' 2>/dev/null \
            | head -1 \
        | xargs -I{} dirname {})  # timeout: 10000
    [ -n "$PLUGIN_ROOT" ] && printf "  Note: foundry not in installed_plugins.json — using cache scan result; consider reinstalling\n"
fi
```

If `$PLUGIN_ROOT` empty after both attempts, stop and report: "foundry plugin not found — install it first with: `claude plugin marketplace add /path/to/Borda-AI-Rig && claude plugin install foundry@borda-ai-rig`"

Confirm `$PLUGIN_ROOT/hooks/statusline.js` exists. If not, stop and report.

## Step 2: Back up settings.json

```bash
[ ! -f ~/.claude/settings.json ] && echo '{}' > ~/.claude/settings.json  # timeout: 5000
cp ~/.claude/settings.json ~/.claude/settings.json.bak  # timeout: 5000
```

Report: "Backed up ~/.claude/settings.json → ~/.claude/settings.json.bak"

## Step 2b: Check for stale hooks block

```bash
jq -e 'has("hooks")' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If `hooks` key exists, user has pre-plugin-migration settings block — hooks fire twice.

If `APPROVE_ALL=true`: print `[--approve] auto-accepting: remove stale hooks block` and proceed to remove (apply option a below).

Otherwise, use `AskUserQuestion`:

- a) Remove stale `hooks` block now ★ recommended (backup in place from Step 2)
- b) Skip — I'll handle manually

On **(a)**: use jq to strip `hooks` key, write back with Write tool, continue. On **(b)**: warn "Double-firing risk: existing hooks block will fire alongside plugin-registered hooks." Continue.

## Step 3: Merge statusLine

Check if statusLine already points to statusline.js:

```bash
jq -e '(.statusLine.command // "") | contains("statusline.js")' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If already set: report "statusLine already set — skipping." Otherwise:

```bash
jq --arg cmd "node \"$PLUGIN_ROOT/hooks/statusline.js\"" \
    '.statusLine = {"async":true,"command":$cmd,"type":"command"}' \
    ~/.claude/settings.json > /tmp/foundry_init_tmp.json  # timeout: 5000
```

Write `/tmp/foundry_init_tmp.json` back to `~/.claude/settings.json` using Write tool.

## Step 4: Merge permissions.allow and permissions.deny

Read `$PLUGIN_ROOT/.claude-plugin/permissions-allow.json` using Read tool. Merge into `~/.claude/settings.json` — add only entries not already present (exact string match):

```bash
jq --slurpfile perms "$PLUGIN_ROOT/.claude-plugin/permissions-allow.json" \
    '.permissions.allow = ((.permissions.allow // []) + $perms[0] | unique)' \
    ~/.claude/settings.json > /tmp/foundry_init_tmp.json  # timeout: 5000
```

Write back with Write tool. Report: "Added N new permissions.allow entries (M already present)."

Check whether `$PLUGIN_ROOT/.claude-plugin/permissions-deny.json` exists. If so, read with Read tool and merge — add only entries not already present:

```bash
jq --slurpfile deny "$PLUGIN_ROOT/.claude-plugin/permissions-deny.json" \
    '.permissions.deny = ((.permissions.deny // []) + $deny[0] | unique)' \
    ~/.claude/settings.json > /tmp/foundry_init_tmp.json  # timeout: 5000
```

Write back with Write tool. Report: "Added N new permissions.deny entries (M already present)."

## Step 4b: Copy permissions-guide.md

Copy `$PLUGIN_ROOT/permissions-guide.md` to `.claude/permissions-guide.md` — only if destination absent (preserves project-local edits via `/manage`):

```bash
if [ ! -f ".claude/permissions-guide.md" ]; then  # timeout: 5000
    cp "$PLUGIN_ROOT/permissions-guide.md" ".claude/permissions-guide.md"
    printf "  copied: permissions-guide.md\n"
else
    printf "  permissions-guide.md already present — skipping\n"
fi
```

## Step 5: Merge enabledPlugins

```bash
jq -e '.enabledPlugins["codex@openai-codex"] == true' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If already `true`: report "enabledPlugins already set — skipping." Otherwise:

```bash
jq '.enabledPlugins["codex@openai-codex"] = true' \
    ~/.claude/settings.json > /tmp/foundry_init_tmp.json  # timeout: 5000
```

Write back with Write tool.

## Step 6: Validate

After all writes, confirm file parses as valid JSON:

```bash
jq empty ~/.claude/settings.json  # timeout: 5000
```

If `jq` exits non-zero: restore from backup (`cp ~/.claude/settings.json.bak ~/.claude/settings.json`), report error, stop. If valid: continue.

## Step 7: Symlink rules and TEAM_PROTOCOL.md

Ensure target dir exists:

```bash
mkdir -p ~/.claude/rules  # timeout: 5000
```

**Conflict scan** — identify rule files and TEAM_PROTOCOL.md existing in `~/.claude/` as real files or symlinks pointing elsewhere:

```bash
LINK_CONFLICTS=()
for src in "$PLUGIN_ROOT/rules/"*.md; do
    dest="$HOME/.claude/rules/$(basename "$src")"
    if [ -L "$dest" ]; then
        target=$(readlink "$dest")
        echo "$target" | grep -q "$PLUGIN_ROOT" || LINK_CONFLICTS+=("rules/$(basename "$src") → $target")
    elif [ -f "$dest" ]; then
        LINK_CONFLICTS+=("rules/$(basename "$src")  (real file)")
    fi
done  # timeout: 5000
src="$PLUGIN_ROOT/TEAM_PROTOCOL.md"; dest="$HOME/.claude/TEAM_PROTOCOL.md"
if [ -L "$dest" ]; then
    target=$(readlink "$dest")
    echo "$target" | grep -q "$PLUGIN_ROOT" || LINK_CONFLICTS+=("TEAM_PROTOCOL.md → $target")
elif [ -f "$dest" ]; then
    LINK_CONFLICTS+=("TEAM_PROTOCOL.md  (real file)")
fi  # timeout: 5000
```

If conflicts exist:

If `APPROVE_ALL=true`: print `[--approve] auto-accepting: replace all symlink conflicts` and replace all (apply option a below).

Otherwise, use `AskUserQuestion`:

```
These entries in ~/.claude/ would be replaced with symlinks to the foundry plugin:
  - <name>  (<current state>)
  - …
```

Options:

- a) Replace all ★ recommended
- b) Skip all conflicts — keep existing files unchanged
- c) Review one by one

On **c**: loop with `AskUserQuestion` — "Replace `<name>`? (y) Yes / (n) Skip".

**Symlink** — for each approved or absent entry, `ln -sf` atomically replaces:

```bash
for src in "$PLUGIN_ROOT/rules/"*.md; do
    ln -sf "$src" "$HOME/.claude/rules/$(basename "$src")"  # timeout: 5000
    echo "  linked: $(basename "$src")"
done  # timeout: 10000
ln -sf "$PLUGIN_ROOT/TEAM_PROTOCOL.md" ~/.claude/TEAM_PROTOCOL.md  # timeout: 5000
echo "  linked: TEAM_PROTOCOL.md"
```

## Step 8: Final report

Print summary:

- statusLine: set / skipped
- permissions.allow: N entries added
- enabledPlugins: set / skipped
- Rules linked: N → ~/.claude/rules/
- TEAM_PROTOCOL.md linked → ~/.claude/TEAM_PROTOCOL.md
- Backup at: ~/.claude/settings.json.bak

Suggest: "Re-run `/foundry:init` after any plugin upgrade to refresh symlinks to new cache path."

</workflow>

<notes>

**Testing init changes**: Init skill has no `.claude/skills/init` entry — only reachable as `/foundry:init` after plugin installed. To test: bump `version` in `plugins/foundry/.claude-plugin/plugin.json`, run `claude plugin install foundry@borda-ai-rig` from repo root to refresh cache, invoke `/foundry:init`. **Upgrade path**: After `claude plugin install foundry@borda-ai-rig` upgrades version, symlinks point to old cache path. Re-run `/foundry:init` — Step 7 detects stale symlinks as conflicts and replaces them.

</notes>
