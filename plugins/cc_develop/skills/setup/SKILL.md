---
name: setup
description: "Post-install setup for the develop plugin. Run once after installing on a new machine, or after a plugin version upgrade, to deliver this plugin's rules/*.md into ~/.claude/rules/ as namespaced symlinks and merge its own permissions.allow and permissions.deny entries into ~/.claude/settings.json. TRIGGER when: user installed or upgraded the develop plugin and its rules are not loading, or its deny rules are not blocking; phrases: 'set up develop', 'develop rules not loading', 'merge develop permissions', 'after upgrading develop'. SKIP: statusLine/TEAM_PROTOCOL/plugin-cache setup (use /foundry:setup (requires `foundry` plugin)); editing rule content (edit the plugin source)."
argument-hint: '[--approve]'
allowed-tools: Bash, AskUserQuestion
effort: low
model: sonnet
---

<objective>

Deliver develop's rules to Claude's user-level rule namespace, and its own permission rules to Claude's settings.

| Action | What happens |
| -- | -- |
| `rules/*.md` → `~/.claude/rules/develop-<name>.md` | symlink |
| Stale link from an older develop version | refreshed silently |
| Link whose source left the plugin | removed |
| Real file or foreign link at a destination | preserved, reported as conflict |
| Anything outside `~/.claude/rules/` | never touched |
| `.claude-plugin/permissions-{allow,deny}.json` → `~/.claude/settings.json` | merged additively; nothing removed |
| Any other key of `~/.claude/settings.json` | never touched |

**Why namespaced?** Claude loads user rules from one flat directory. Four plugins ship a `rules/quality-gates.md`; installing source basenames would collide. Every rule installs as `<plugin>-<source-name>.md` — `quality-gates.md` becomes `develop-quality-gates.md`. Prefix is inert — verified against Claude Code 2.1.220: a filename prefix changes neither unconditional loading nor `paths:` frontmatter matching.

**Why symlink, not copy?** Rules load at session start. A symlink serves the installed version after every upgrade; a copy silently serves stale content forever.

**Why does develop deliver only its own rules?** Each plugin installs independently. A plugin shipping a sibling's rules would break standalone installation, couple releases.

NOT for: statusLine, `TEAM_PROTOCOL.md`, or plugin-cache purging — those are `/foundry:setup` (requires `foundry` plugin). Of `~/.claude/settings.json` only the `permissions.allow` and `permissions.deny` arrays are touched, and only additively. Writes nothing under `~/.codex/`.

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
PYTHON_CMD=""
for c in python python3; do
    command -v "$c" >/dev/null 2>&1 && "$c" --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])" && PYTHON_CMD="$c" && break
done
if [ -z "$PYTHON_CMD" ] && command -v py >/dev/null 2>&1 && py -3 --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="py -3"
fi
[ -z "$PYTHON_CMD" ] && { printf "! Python 3.10+ not found — install it and re-run /develop:setup\n"; exit 1; }
printf "  Python: %s\n" "$PYTHON_CMD"
```

## Step 2: Dry run — see what would change

`$CLAUDE_PLUGIN_ROOT` is the installed plugin version. `sync_rules.py` re-validates it (manifest exists, parses, declares `develop`; `rules/` is a real directory holding ≥1 non-empty regular `*.md`) and aborts before touching anything on any check failure.

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}"
python "$PLUGIN_ROOT/bin/sync_rules.py" --plugin-name develop --plugin-root "$PLUGIN_ROOT" --dry-run  # timeout: 15000
```

Non-zero exit → print stderr verbatim and stop; nothing was modified.

## Step 3: Apply

Run without `--dry-run`. Add `--approve` only when `APPROVE_ALL=true`:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}"
python "$PLUGIN_ROOT/bin/sync_rules.py" --plugin-name develop --plugin-root "$PLUGIN_ROOT"  # timeout: 15000
```

Output lines, one per destination: `linked:` · `unchanged:` · `replaced (--approve):` · `removed obsolete:` · `conflict, kept as-is:` · `FAILED:`.

Ownership proved before any replace or remove: existing link must resolve under current plugin root, or the same `~/.claude/plugins/cache/<marketplace>/develop/` lineage as current install. A link into another marketplace, another plugin, a source checkout, or a dotfiles tree never adopted — path substrings aren't evidence of ownership.

## Step 4: Conflicts

No `conflict, kept as-is:` lines → skip to Step 5.

`APPROVE_ALL=true` → conflicts were already replaced in Step 3; skip to Step 5.

Otherwise invoke `AskUserQuestion`, listing each conflicting destination and its current state:

- (a) **Keep them** — those rules stay undelivered
- (b) **Replace all with plugin links** ★ recommended — existing content is overwritten
- (c) **Abort** — leave everything as it is

On **(b)**, re-run Step 3's command with `--approve` appended and report the resulting `replaced (--approve):` lines. On (a) or (c), report which rules remain undelivered.

## Step 5: Merge permissions.allow and permissions.deny

This plugin ships its own `permissions-allow.json` and `permissions-deny.json`. Claude Code doesn't read them from the plugin manifest, so without this step they're inert files — allow entries never suppress a prompt, deny entries never block anything.

Merge is additive and idempotent: `unique` keeps existing entries from duplicating; no entry ever removed. Each plugin merges only its own pair.

Create the file on first install, back it up before any write — a standalone install may reach this step with no `~/.claude/settings.json` at all:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
SETUP_BAK_TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
echo "$SETUP_BAK_TS" > "${TMPDIR:-/tmp}/develop-setup-bak-ts-${CSID}"
[ -f ~/.claude/settings.json ] || printf '{}\n' > ~/.claude/settings.json  # created in-bash — no Write-tool prompt, headless-safe
cp ~/.claude/settings.json "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}"  # timeout: 5000
```

Report: "Backed up ~/.claude/settings.json → ~/.claude/settings.json.bak-<timestamp>"

Writes merged `permissions.allow` array:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}"
_jq_result=$(jq --slurpfile perms "$PLUGIN_ROOT/.claude-plugin/permissions-allow.json" \
    '.permissions.allow = ((.permissions.allow // []) + $perms[0] | unique)' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/develop_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/develop_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed merging permissions.allow — settings.json unchanged\n"; exit 1; }
```

Report: "Added N new permissions.allow entries (M already present)."

Writes merged `permissions.deny` array:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}"
_jq_result=$(jq --slurpfile deny "$PLUGIN_ROOT/.claude-plugin/permissions-deny.json" \
    '.permissions.deny = ((.permissions.deny // []) + $deny[0] | unique)' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/develop_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/develop_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed merging permissions.deny — settings.json unchanged\n"; exit 1; }
```

Report: "Added N new permissions.deny entries (M already present)."

Deny wins over allow in Claude Code — merging both in either order yields the same effective policy.

Validate what was written; settings.json that no longer parses is restored from the backup taken above:

```bash
jq empty ~/.claude/settings.json  # timeout: 5000
```

Zero exit → continue to Step 6. Non-zero exit → restore, report the failure, and stop:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SETUP_BAK_TS < "${TMPDIR:-/tmp}/develop-setup-bak-ts-${CSID}" 2>/dev/null || SETUP_BAK_TS=$(ls -t "$HOME/.claude/settings.json.bak-"* 2>/dev/null | head -1 | sed 's/.*\.bak-//')
cp "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}" ~/.claude/settings.json  # timeout: 5000
```

## Step 6: Report

- Rules linked: N → `~/.claude/rules/develop-*.md`
- Unchanged: N · Obsolete removed: N · Conflicts kept: N (name each)
- Permissions: N allow entries added, N deny entries added
- Failures: N (name each; a failure means the platform refused the symlink — rules are never copied as a fallback)

</workflow>

<notes>

**Upgrade path**: `claude plugin install develop@borda-ai-rig` then `/develop:setup`. Links from the previous version share the install-cache lineage, so they refresh without prompting; a rule dropped in the new version has its link removed. `make sync-claude` runs `/develop:setup --approve` headlessly for every installed managed plugin shipping a setup skill — normal sync needs no manual step.

**Uninstall leaves state behind**: Claude Code runs no cleanup hook on uninstall; neither `claude plugin uninstall` nor `make clear-all` removes what setup created. After removing the plugin, delete `~/.claude/rules/develop-*.md` by hand — they become dangling symlinks once the plugin cache version is gone.

**Testing**: setup reachable only as `/develop:setup` after plugin install. To exercise locally: bump `version` in `plugins/cc_develop/.claude-plugin/plugin.json`, run `claude plugin install develop@borda-ai-rig` from repo root to refresh cache, then invoke the skill. `bin/sync_rules.py` itself covered by `plugins/cc_develop/tests/test_sync_rules.py` against disposable home directories.

**Follow-up gate omitted** — setup is one-shot; Step 6 is terminal output.

</notes>
