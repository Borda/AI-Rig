---
name: setup
description: "Post-install setup for the oss plugin. Run once after installing on a new machine, or after a plugin version upgrade, to deliver this plugin's rules/*.md into ~/.claude/rules/ as namespaced symlinks and merge its own permissions.allow and permissions.deny entries into ~/.claude/settings.json. TRIGGER when: user installed or upgraded the oss plugin and its rules are not loading, or its deny rules are not blocking; phrases: 'set up oss', 'oss rules not loading', 'merge oss permissions', 'after upgrading oss'. SKIP: statusLine/TEAM_PROTOCOL/plugin-cache setup (use /foundry:setup (requires `foundry` plugin)); editing rule content (edit the plugin source)."
argument-hint: '[--approve]'
allowed-tools: Bash, AskUserQuestion
effort: low
---

<objective>

Deliver oss's rules to Claude's user-level rule namespace, and its own permission rules to Claude's settings.

| Action | What happens |
| -- | -- |
| `rules/*.md` → `~/.claude/rules/oss-<name>.md` | symlink |
| Stale link from an older oss version | refreshed silently |
| Link whose source left the plugin | removed |
| Real file or foreign link at a destination | preserved, reported as conflict |
| Anything outside `~/.claude/rules/` | never touched |
| `.claude-plugin/permissions-{allow,deny}.json` → `~/.claude/settings.json` | merged additively except for the exact shipped retired allow entries |
| Any other key of `~/.claude/settings.json` | never touched |

**Why namespaced?** Claude loads user rules from one flat directory. Four plugins ship a `rules/quality-gates.md`; installing source basenames would collide. Every rule installs as `<plugin>-<source-name>.md`, so `quality-gates.md` becomes `oss-quality-gates.md`. The prefix is inert — verified against Claude Code 2.1.220 that a filename prefix changes neither unconditional loading nor `paths:` frontmatter matching.

**Why symlink, not copy?** Rules load at session start. A symlink serves the installed version after every upgrade; a copy silently serves stale content forever.

**Why does oss deliver only its own rules?** Each plugin installs independently. A plugin that shipped a sibling's rules would break standalone installation and couple releases. Still true with the delta variant: `oss-quality-gates.md` links either `rules/quality-gates-delta.md` (when a foundry-owned `foundry-quality-gates.md` is already delivered) or the complete `rules/quality-gates.md` (when it is not) — both sources belong to this plugin, and the delta only *points* at foundry's copy for the shared obligations rather than delivering it. Standalone install loses no rule; all four `quality-gates.md` copies loading at once was 51,274 B of context on every turn.

NOT for: statusLine, `TEAM_PROTOCOL.md`, or plugin-cache purging — those are `/foundry:setup` (requires `foundry` plugin). Of `~/.claude/settings.json` only `permissions.allow` and `permissions.deny` arrays are touched, additively except for removing exactly the retired allow entries listed in `permissions-allow-retired.json`. Writes nothing under `~/.codex/`.

</objective>

<inputs>

- **No arguments** — interactive; prompts before replacing a conflicting destination.
- **`--approve`** — non-interactive; replaces conflicting destinations without asking. Used by `make sync-claude`.

</inputs>

<workflow>

## Step 0: Flags

Parse `$ARGUMENTS` for `--approve` (case-insensitive) → `APPROVE_ALL=true`, else `false`. Any other `--<token>` → print `` ! Unknown flag(s): `--<token>`. Supported: `--approve`. `` and stop.

## Step 1: Python

```bash
# later steps run bare `python` (plugin bin/python shim on PATH) — probe that exact command
python --version 2>&1 | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])" || { printf "! Python 3.10+ not found as 'python' — install it and re-run /oss:setup\n"; exit 1; }
printf "  Python: %s\n" "$(python --version 2>&1)"
```

## Step 2: Dry run — see what would change

`$CLAUDE_PLUGIN_ROOT` is the installed plugin version. `sync_rules.py` re-validates it (manifest exists, parses, declares `oss`; `rules/` is a real directory holding at least one non-empty regular `*.md`) and aborts before touching anything if any check fails.

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}"
python "$PLUGIN_ROOT/bin/sync_rules.py" --plugin-name oss --plugin-root "$PLUGIN_ROOT" --dry-run  # timeout: 15000
```

Non-zero exit → print stderr verbatim and stop; nothing was modified.

## Step 3: Apply

Run without `--dry-run`. Add `--approve` only when `APPROVE_ALL=true`:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}"
python "$PLUGIN_ROOT/bin/sync_rules.py" --plugin-name oss --plugin-root "$PLUGIN_ROOT"  # timeout: 15000
```

Output lines, one per destination: `linked:` · `unchanged:` · `replaced (--approve):` · `removed obsolete:` · `conflict, kept as-is:` · `FAILED:`.

Ownership proved before any replace or remove: existing link must resolve under current plugin root, or under same `~/.claude/plugins/cache/<marketplace>/oss/` lineage as current install. A link into another marketplace, another plugin, a source checkout, or a dotfiles tree is never adopted — path substrings are not evidence of ownership.

## Step 4: Conflicts

No `conflict, kept as-is:` lines → skip to Step 5.

`APPROVE_ALL=true` → conflicts were already replaced in Step 3; skip to Step 5.

Otherwise invoke `AskUserQuestion`, listing each conflicting destination and its current state:

- (a) **Keep them** — those rules stay undelivered
- (b) **Replace all with plugin links** ★ recommended — existing content is overwritten
- (c) **Abort** — leave everything as it is

On **(b)**, re-run Step 3's command with `--approve` appended and report the resulting `replaced (--approve):` lines. On (a) or (c), report which rules remain undelivered.

## Step 5: Merge permissions.allow and permissions.deny

This plugin ships its own `permissions-allow.json` and `permissions-deny.json`. Claude Code doesn't read them from plugin manifest, so without this step they're inert files — allow entries never suppress a prompt, deny entries never block anything.

Merge is idempotent: existing entries never duplicate. Each plugin merges only its own pair; the allow merge removes exactly the entries in `permissions-allow-retired.json` and preserves every other entry.

The merge removes exactly the retired allow entries listed in `permissions-allow-retired.json`, each shipped by an earlier oss version:

- `Bash(gh api repos/*:*)` — nothing replaces it; the broad repository API allow entry is no longer shipped.
- `Bash(uv pip install:*)` — nothing replaces it. It auto-approved every `uv pip install`, including the review overlay's support check and wheel fetch, which must each reach their own permission prompt, and project-environment installs or source builds the overlay forbids.
- The broad `Bash(claude:*)`, `Bash(find:*)`, `Bash(node:*)`, `Bash(sed:*)` and `Bash(eval:*)` shell grants — nothing replaces them.

Preserve every other user allow entry, all deny entries, and unrelated settings.

Deny wins over allow in Claude Code, so merging both in either order yields the same effective policy.

One block runs the whole step: create the file on first install (a standalone install may reach this step with no `~/.claude/settings.json` at all), back it up before any write, merge, then validate. `merge_settings_permissions.py` writes the merged `permissions.allow` and `permissions.deny` arrays in one atomic write, so allow never lands without deny; a missing or invalid shipped file aborts the block with settings unchanged. settings.json that no longer parses is restored from the backup taken at the top of the block:

```bash
SETUP_BAK_TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
[ -f ~/.claude/settings.json ] || printf '{}\n' > ~/.claude/settings.json  # created in-bash — no Write-tool prompt, headless-safe
cp ~/.claude/settings.json "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}"  # timeout: 5000
printf "Backed up ~/.claude/settings.json → ~/.claude/settings.json.bak-%s\n" "$SETUP_BAK_TS"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/merge_settings_permissions.py" --allow "$PLUGIN_ROOT/.claude-plugin/permissions-allow.json" --drop-allow-file "$PLUGIN_ROOT/.claude-plugin/permissions-allow-retired.json" --deny "$PLUGIN_ROOT/.claude-plugin/permissions-deny.json" || { printf "! permissions merge failed — settings.json unchanged\n"; exit 1; }  # timeout: 5000
python -m json.tool ~/.claude/settings.json > /dev/null || { cp "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}" ~/.claude/settings.json; printf "! settings.json no longer parses — restored from backup\n"; exit 1; }  # timeout: 5000 — Step 1 guarantees python; jq may be absent (native Windows), and its exit 127 would restore the backup over a good merge
```

Report the backup path the block printed. From the script's two output lines report "Added N new permissions.allow entries (M already present)." plus the total retired allow entries removed (`dropped`), and "Added N new permissions.deny entries (M already present)." Zero exit → continue to Step 6. Non-zero exit → the block already restored (validation failure) or left settings unchanged (merge failure); report the failure and stop.

## Step 6: Report

- Rules linked: N → `~/.claude/rules/oss-*.md`
- Unchanged: N · Obsolete removed: N · Conflicts kept: N (name each)
- Permissions: N allow entries added, N deny entries added
- stale gh deny entries from older versions: run /foundry:setup (requires `foundry` plugin)
- Failures: N (name each; a failure means the platform refused the symlink — rules are never copied as a fallback)

</workflow>

<notes>

**Upgrade path**: `claude plugin install oss@borda-ai-rig` then `/oss:setup`. Links from the previous version share the install-cache lineage, so they refresh without prompting; a rule dropped in the new version has its link removed. `make sync-claude` runs `/oss:setup --approve` headlessly for every installed managed plugin that ships a setup skill, so a normal sync needs no manual step.

**Uninstall leaves state behind**: Claude Code runs no cleanup hook on uninstall, neither `claude plugin uninstall` nor `make clear-all` removes what setup created. After removing the plugin, delete `~/.claude/rules/oss-*.md` by hand — they become dangling symlinks once plugin cache version is gone.

**Testing**: setup is reachable only as `/oss:setup` after the plugin is installed. To exercise it locally, bump `version` in `plugins/cc_oss/.claude-plugin/plugin.json`, run `claude plugin install oss@borda-ai-rig` from the repo root to refresh the cache, then invoke the skill. `bin/sync_rules.py` is a byte-identical propagated copy of the canonical helper; its regression suite lives beside that canonical copy in the AI-Rig repository.

**Follow-up gate omitted** — setup is one-shot; Step 6 is terminal output.

</notes>
