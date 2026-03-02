---
name: sync
description: Drift-detect and sync git-tracked .claude/ and .codex/ config from project to home. Default shows what would change; "apply" performs the sync.
argument-hint: '[apply]'
allowed-tools: Read, Bash
---

<objective>

Sync git-tracked files from project `.claude/` and `.codex/` to their home equivalents (`~/.claude/` and `~/.codex/`). Uses `rsync` driven by `git ls-files` as the file manifest — only git-tracked files are ever synced; runtime state, secrets, and temp files are excluded automatically.

Project always wins: files in home that have no counterpart in the project are left untouched. No deletion, ever.

</objective>

<inputs>

- **$ARGUMENTS**: optional
  - Omitted → dry-run: show what would change, no files written
  - `apply` → apply sync: rsync files and report outcome

</inputs>

<constants>

- PROJECT root: `$(git rev-parse --show-toplevel)`
- HOME_EXPANDED: `$(eval echo ~)`
- File manifest: `git ls-files .claude/` and `git ls-files .codex/` — git-tracked only; gitignored paths (state/, logs/, settings.local.json, auth.json) excluded automatically
- settings.json transform: replace `node .claude/hooks/` with `node $HOME_EXPANDED/.claude/hooks/` (absolute). Covers all hook entries — statusLine, task-log, and future additions.
- Never synced: `settings.local.json`, `.codex/auth.json` — not git-tracked, excluded automatically
- No `context: fork` — sync reads files from both project and home then writes decisions; forking would give the skill an isolated context with no access to the home directory state it needs to compare against

</constants>

<workflow>

## Step 1: Resolve paths

```bash
PROJECT="$(git rev-parse --show-toplevel)"
HOME_EXPANDED="$(eval echo ~)"
HOME_CLAUDE="$HOME_EXPANDED/.claude"
HOME_CODEX="$HOME_EXPANDED/.codex"
```

## Step 2: Dry-run — show what would change

Run all three checks and collect output before printing anything.

**`.claude/` files** (excluding `settings.json`):

```bash
git -C "$PROJECT" ls-files .claude/ \
  | grep -vE 'settings\.json$|settings\.local\.json$' \
  | sed 's|^\.claude/||' \
  | rsync -av --dry-run --files-from=- "$PROJECT/.claude/" "$HOME_CLAUDE/"
```

rsync prints only files that would change; identical files are silently skipped.

**`settings.json`** — transform then compare:

```bash
SETTINGS_TMP="$(mktemp "${TMPDIR:-/tmp}/settings_xfm.XXXXXX.json")"
trap 'rm -f "$SETTINGS_TMP"' EXIT
sed "s|node .claude/hooks/|node $HOME_EXPANDED/.claude/hooks/|g" \
  "$PROJECT/.claude/settings.json" > "$SETTINGS_TMP"

diff -q "$SETTINGS_TMP" "$HOME_CLAUDE/settings.json" > /dev/null 2>&1
```

- **Identical** → emit `✓ IDENTICAL settings.json`
- **Differs** → read both `"$SETTINGS_TMP"` and `~/.claude/settings.json`; produce a human-readable semantic summary: which permissions were added, which removed, any model/hook/config key changes. Do not output raw diff as the final report.

**`.codex/` files**:

```bash
git -C "$PROJECT" ls-files .codex/ \
  | sed 's|^\.codex/||' \
  | rsync -av --dry-run --files-from=- "$PROJECT/.codex/" "$HOME_CODEX/"
```

If `$ARGUMENTS` is empty: print the combined dry-run output and offer `/sync apply`. Stop here.

## Step 3: Apply (only when $ARGUMENTS == "apply")

**`.claude/` files** (excluding `settings.json`):

```bash
git -C "$PROJECT" ls-files .claude/ \
  | grep -vE 'settings\.json$|settings\.local\.json$' \
  | sed 's|^\.claude/||' \
  | rsync -av --files-from=- "$PROJECT/.claude/" "$HOME_CLAUDE/"
```

**`settings.json`** — write only if different:

```bash
SETTINGS_TMP="$(mktemp "${TMPDIR:-/tmp}/settings_xfm.XXXXXX.json")"
trap 'rm -f "$SETTINGS_TMP"' EXIT
sed "s|node .claude/hooks/|node $HOME_EXPANDED/.claude/hooks/|g" \
  "$PROJECT/.claude/settings.json" > "$SETTINGS_TMP"

if ! diff -q "$SETTINGS_TMP" "$HOME_CLAUDE/settings.json" > /dev/null 2>&1; then
  cp "$SETTINGS_TMP" "$HOME_CLAUDE/settings.json"
  echo "merged   settings.json"
else
  echo "✓ unchanged settings.json"
fi
```

**`.codex/` files**:

```bash
git -C "$PROJECT" ls-files .codex/ \
  | sed 's|^\.codex/||' \
  | rsync -av --files-from=- "$PROJECT/.codex/" "$HOME_CODEX/"
```

## Step 4: Verify and report outcome

```bash
# JSON validity
jq empty "$HOME_EXPANDED/.claude/settings.json" && echo "settings.json: valid"

# Counts
echo ".claude files: $(git -C "$PROJECT" ls-files .claude/ | wc -l | tr -d ' ')"
echo ".codex files:  $(git -C "$PROJECT" ls-files .codex/  | wc -l | tr -d ' ')"
```

```
## Sync Outcome — <date>

| Target          | Result             |
|-----------------|--------------------|
| .claude/ files  | N transferred      |
| settings.json   | merged / unchanged |
| .codex/ files   | N transferred      |
| JSON validity   | valid              |
```

</workflow>

<notes>

- File manifest from `git ls-files` — adding a new agent, skill, hook, or codex agent to git automatically includes it in future syncs; no skill edits needed
- `settings.local.json` and `.codex/auth.json` are never synced — gitignored, excluded automatically
- All `node .claude/hooks/` paths in settings.json are rewritten to absolute — relative paths fail when hooks fire outside this project directory
- Project owns what it tracks; home-only files are never touched or deleted
- rsync skips identical files — only changed files are transferred, making repeated runs cheap
- Run `/sync` (dry-run) first, then `/sync apply` once the report looks correct
- Follow-up: after `/sync apply`, run `/audit` to confirm the propagated config is structurally sound

</notes>
