---
name: setup
description: Post-install setup for foundry plugin. Run once after installing on a new machine, or after a plugin version upgrade to sync settings and symlinks. Merges statusLine, permissions.allow, enabledPlugins, advisorModel, and env defaults (CLAUDE_CODE_ENABLE_TODO_TOOLS) into ~/.claude/settings.json; symlinks rules and TEAM_PROTOCOL.md into ~/.claude/; purges orphaned plugin cache versions.
argument-hint: '[--approve]'
allowed-tools: Read, Write, Bash, AskUserQuestion
effort: low
model: sonnet
---

<objective>

Set up foundry on new machine:

| Action | What happens |
| -- | -- |
| Detect Python 3.10+ (`python` / `py -3` / `python3`); install `~/.local/bin/python` shim if needed | ✓ |
| Merge `statusLine`, `permissions.allow`, `enabledPlugins`, `advisorModel`, `env` defaults → `~/.claude/settings.json` | ✓ |
| `rules/<name>.md` → `~/.claude/rules/foundry-<name>.md` | symlink |
| `TEAM_PROTOCOL.md` → `~/.claude/` | symlink |
| Purge orphaned plugin cache versions (`.orphaned_at`, age-gated, confirm-gated) | ✓ |
| `hooks/hooks.json` | auto — plugin system |
| Conflict review before overwriting existing user files | ✓ |

**Why is every rule renamed `foundry-<name>.md`?** `~/.claude/rules/` is one flat directory shared by every installed plugin, and four of them ship a `rules/quality-gates.md`. Installing source basenames would have them overwrite each other, so each plugin namespaces its own rules with its plugin name. The prefix is inert — verified against Claude Code 2.1.220 that it changes neither unconditional loading nor `paths:` frontmatter matching. Phase 1 migrates a pre-namespace unprefixed link (`quality-gates.md` → `foundry-quality-gates.md`) by removing the old one, but only when that old link provably belongs to foundry.

**Why symlink rules and TEAM_PROTOCOL.md (not copy)?** Both load at session startup. Symlinks = every session gets plugin's current version — no stale copies, no re-run after upgrades. Broken symlink after upgrade = obvious error; stale copy silently serves old content.

**Why NOT symlink skills?** A directory carrying a `SKILL.md` under `~/.claude/skills/` registers as a **user-level** skill, and user-level skills silently shadow Claude Code's bundled skill of the same name. That already cost bare `/review` (hit CC's bundled reviewer instead of `oss:review`). Foundry skills dispatch as `/foundry:<name>` — no `~/.claude/skills/` entry needed, ever. `skills/_shared` is excluded too: no plugin may depend on a global `_shared` path — each resolves its own via `bin/resolve_shared_path.py`. Phase 1 purges any such symlink unconditionally, including ones pointing at the current version.

**Why not symlink agents?** Agents must use full plugin prefix (`foundry:sw-engineer`, not `sw-engineer`) for unambiguous dispatch. Plugin system exposes agents at `foundry:` namespace — no `~/.claude/agents/` symlinks needed. (Stale agent symlinks from prior installs removed by setup's Phase 1 cleanup.)

**Why hooks need no action?** `hooks/hooks.json` inside plugin registers automatically when plugin enabled. Setup's only hook-adjacent step: write `statusLine.command` path (Step 4) — `statusLine` is top-level settings key, not part of `hooks.json`.

NOT for: editing project `.claude/settings.json` (Step 8 READS it to propagate advisorModel, never writes it).

</objective>

<inputs>

- **No arguments** — interactive mode; prompts on conflicts.
- **`--approve`** — non-interactive mode; auto-accepts all recommended answers. Use for scripted or CI setups.

</inputs>

<workflow>

## Flag detection

Parse `$ARGUMENTS` for `--approve` (case-insensitive). If found, set `APPROVE_ALL=true`; else `APPROVE_ALL=false`.

**Early git repository check** — Step 6 requires a git repository. In `--approve` mode there is no interactive fallback, so check immediately before Step 1:

```bash
if [ "$APPROVE_ALL" = "true" ] && [ ! -e ".git" ]; then
    printf "! --approve requires git repository — run from project root\n"
    exit 1
fi
```

When `APPROVE_ALL=true`, every `AskUserQuestion` below **skipped** — ★ recommended option applied automatically. Print `[--approve] auto-accepting recommended option` in place of question.

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--approve`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Python detection

Probe Python 3.10+ — required before any `bin/*.py` calls. Windows Store stub returns exit 9009 when given args; caught by `2>/dev/null`:

```bash
PYTHON_CMD=""
SHIM_DIR="$HOME/.local/bin"
if command -v python >/dev/null 2>&1 && python --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="python"
elif command -v py >/dev/null 2>&1 && py -3 --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="py -3"
    mkdir -p "$SHIM_DIR"
    printf '#!/usr/bin/env bash\npy -3 "$@"\n' > "$SHIM_DIR/python"
    chmod +x "$SHIM_DIR/python"
    printf "  Python shim installed: %s/python → py -3\n" "$SHIM_DIR"
elif command -v python3 >/dev/null 2>&1 && python3 --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="python3"
    mkdir -p "$SHIM_DIR"
    printf '#!/usr/bin/env bash\npython3 "$@"\n' > "$SHIM_DIR/python"
    chmod +x "$SHIM_DIR/python"
    printf "  Python shim installed: %s/python → python3\n" "$SHIM_DIR"
else
    printf "! Python 3.10+ not found — install Python 3.10+ and re-run /foundry:setup\n"
    exit 1
fi
printf "  Python: %s\n" "$PYTHON_CMD"

# ~/.local/bin is XDG-standard but not always on PATH
if [ -f "$SHIM_DIR/python" ] && ! echo ":$PATH:" | grep -q ":$SHIM_DIR:"; then
    printf "  ⚠ %s not on PATH — add to shell rc:\n      export PATH=\"\$HOME/.local/bin:\$PATH\"\n" "$SHIM_DIR"
fi
```

`~/.local/bin` is XDG-standard user-bin directory on modern macOS/Linux. Shim created only when `python` absent or resolves to Store stub. Idempotent — re-running setup overwrites shim with same content. If `~/.local/bin` not yet on `$PATH`, setup prints `export PATH="$HOME/.local/bin:$PATH"` line for user's shell rc.

## Step 1: Locate the installed plugin

Resolve validated install root via canonical resolver — registry lookup, cache-scan fallback (skips `.orphaned_at`, newest by semver), and both security gates (under cache dir + `plugin.json` name match) live in the script; do not re-implement inline:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_plugin_root.py" --plugin-name foundry 2>/dev/null)  # timeout: 15000
case $? in
    0) ;;
    2) echo "! SECURITY: resolve_plugin_root.py rejected the candidate root — aborting setup"; exit 1 ;;
    *) PLUGIN_ROOT="" ;;  # not found; handled by empty-check below
esac
echo "$PLUGIN_ROOT" > "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}"  # persist for later blocks (Check 41)
```

If `$PLUGIN_ROOT` empty after both attempts, stop and report: "foundry plugin not found — install it first with: `claude plugin marketplace add Borda/AI-Rig && claude plugin install foundry@borda-ai-rig`"

Confirm `$PLUGIN_ROOT/hooks/statusline.js` exists. If not, stop and report.

## Step 2: Back up settings.json

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
SETUP_BAK_TS=$(date -u +%Y%m%dT%H%M%SZ)
[ -f ~/.claude/settings.json ] || printf '{}\n' > ~/.claude/settings.json  # create empty in-bash if absent — no Write-tool permission prompt (headless-safe)
cp ~/.claude/settings.json "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}"  # timeout: 5000
echo "$SETUP_BAK_TS" > "${TMPDIR:-/tmp}/foundry-setup-bak-ts-${CSID}"  # persist for restore in Step 9
```

Report: "Backed up ~/.claude/settings.json → ~/.claude/settings.json.bak-<timestamp>"

## Step 3: Check for stale hooks block

```bash
jq -e 'has("hooks")' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If `hooks` key exists, user has pre-plugin-migration settings block — hooks fire twice.

If `APPROVE_ALL=true`: print `[--approve] auto-accepting: remove stale hooks block` and proceed to remove (apply option a below).

Otherwise, use `AskUserQuestion`:

(a) Remove stale `hooks` block now ★ recommended (backup in place from Step 2) (b) Skip — I'll handle manually

On **(a)**: strip `hooks` key in-bash (no Write tool), continue:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_jq_result=$(jq 'del(.hooks)' ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed stripping hooks — settings.json unchanged\n"; exit 1; }
```

On **(b)**: warn "Double-firing risk: existing hooks block will fire alongside plugin-registered hooks." Continue.

## Step 4: Merge statusLine

Check if statusLine already points to the **current** plugin's statusline.js (filename match alone is insufficient — a stale entry from an older plugin version survives upgrades and silently runs the previous hook). Verify both that the command contains `statusline.js` AND that the `$PLUGIN_ROOT` path (with its version segment) appears in the command string:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload (Check 41)
jq --arg root "$PLUGIN_ROOT" -e '
    (.statusLine.command // "") as $cmd
    | ($cmd | contains("statusline.js")) and ($cmd | contains($root))
' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If already set to the current `$PLUGIN_ROOT`: report "statusLine already set to current plugin version — skipping." If a stale entry exists (statusline.js present but `$PLUGIN_ROOT` does not match), the check returns non-zero and the merge below overwrites with the current path. Otherwise:

Writes `statusLine` key to `~/.claude/settings.json`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_jq_result=$(jq --arg cmd "node \"$PLUGIN_ROOT/hooks/statusline.js\"" \
    '.statusLine = {"async":true,"command":$cmd,"type":"command"}' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed updating statusLine — settings.json unchanged\n"; exit 1; }
```

Writeback happens in-bash above (`mv` — no Write-tool permission prompt, headless-safe). Report: `  statusLine: set to current plugin version`.

## Step 5: Merge permissions.allow and permissions.deny

Merge `$PLUGIN_ROOT/.claude-plugin/permissions-allow.json` into `~/.claude/settings.json` via jq below — add only entries not already present (exact string match):

Writes merged `permissions.allow` array:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_jq_result=$(jq --slurpfile perms "$PLUGIN_ROOT/.claude-plugin/permissions-allow.json" \
    '.permissions.allow = ((.permissions.allow // []) + $perms[0] | unique)' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed merging permissions.allow — settings.json unchanged\n"; exit 1; }
```

Writeback happens in-bash above (`mv`). Report: "Added N new permissions.allow entries (M already present)."

Check whether `$PLUGIN_ROOT/.claude-plugin/permissions-deny.json` exists. If so, merge via jq below — add only entries not already present:

Writes merged `permissions.deny` array:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_jq_result=$(jq --slurpfile deny "$PLUGIN_ROOT/.claude-plugin/permissions-deny.json" \
    '.permissions.deny = ((.permissions.deny // []) + $deny[0] | unique)' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed merging permissions.deny — settings.json unchanged\n"; exit 1; }
```

Writeback happens in-bash above (`mv`). Report: "Added N new permissions.deny entries (M already present)."

## Step 6: Copy permissions-guide.md

Note: this step writes to `.claude/permissions-guide.md` relative to the current working directory — setup must be run from project root (a git repository root). Guard:

```bash
[ -e ".git" ] || { printf "! BLOCKED — /foundry:setup must run from project root (git repository root)\n"; exit 1; }
```

Copy `$PLUGIN_ROOT/permissions-guide.md` to `.claude/permissions-guide.md` — only if destination absent (preserves project-local edits via `/manage`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 41)
if [ ! -f ".claude/permissions-guide.md" ]; then  # timeout: 5000
    cp "$PLUGIN_ROOT/permissions-guide.md" ".claude/permissions-guide.md"
    printf "  copied: permissions-guide.md\n"
else
    printf "  permissions-guide.md already present — skipping\n"
fi
```

## Step 7: Merge enabledPlugins and env defaults

```bash
jq -e '.enabledPlugins["bridge@borda-ai-rig"] == true' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If already `true`: report "enabledPlugins already set — skipping." Otherwise:

Writes `enabledPlugins["bridge@borda-ai-rig"]` key:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_jq_result=$(jq '.enabledPlugins["bridge@borda-ai-rig"] = true' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed updating enabledPlugins — settings.json unchanged\n"; exit 1; }
```

Writeback happens in-bash above (`mv`).

Then merge `$PLUGIN_ROOT/.claude-plugin/env-defaults.json` into `.env`. Claude Code ships the task tools (`TaskCreate`/`TaskList`/`TaskUpdate`/`TaskGet`) **disabled** unless `CLAUDE_CODE_ENABLE_TODO_TOOLS` is set, so without this key every skill mandating task tracking silently no-ops outside a project that sets the var itself:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 41)
# //= per key — a user who deliberately set "0" keeps it; only absent keys get the shipped default
_jq_result=$(jq --slurpfile envd "$PLUGIN_ROOT/.claude-plugin/env-defaults.json" \
    '.env = ((.env // {}) as $cur | reduce ($envd[0] | to_entries[]) as $e ($cur; .[$e.key] //= $e.value))' \
    ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed merging env defaults — settings.json unchanged\n"; exit 1; }
```

Writeback happens in-bash above (`mv`). Report: `  env: N default(s) added (M already set)`. Existing values are never overwritten — a key already present keeps the user's value, including a deliberate `"0"`.

## Step 8: Merge advisorModel (from project settings)

```bash
ADV=""
if [ -f ".claude/settings.json" ]; then
    ADV=$(jq -r '.advisorModel // empty' .claude/settings.json 2>/dev/null)  # timeout: 5000
fi
```

If `$ADV` empty: report `  advisorModel: skipped (not pinned in project .claude/settings.json)` and continue.

Check if global already equals it:

```bash
jq --arg m "$ADV" -e '.advisorModel == $m' ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

If already equal: report `  advisorModel already set to <value> — skipping.`

Otherwise:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_jq_result=$(jq --arg m "$ADV" '.advisorModel = $m' ~/.claude/settings.json)  # timeout: 5000
[ $? -eq 0 ] && [ -n "$_jq_result" ] && printf '%s\n' "$_jq_result" > "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" && mv "${TMPDIR:-/tmp}/foundry_setup_tmp.json-${CSID}" ~/.claude/settings.json || { printf "! jq failed updating advisorModel — settings.json unchanged\n"; exit 1; }
```

Writeback happens in-bash above (`mv` — no Write-tool permission prompt). Report `  advisorModel: set to <value>`.

## Step 9: Validate

After all writes, confirm file parses as valid JSON:

```bash
jq empty ~/.claude/settings.json  # timeout: 5000
```

If `jq` exits non-zero: restore from backup: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r SETUP_BAK_TS < "${TMPDIR:-/tmp}/foundry-setup-bak-ts-${CSID}" 2>/dev/null || SETUP_BAK_TS=$(ls -t "$HOME/.claude/settings.json.bak-"* 2>/dev/null | head -1 | sed 's/.*\.bak-//'); cp "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}" ~/.claude/settings.json`, report error, stop. If valid: continue.

## Step 10: Symlink rules and TEAM_PROTOCOL.md

Ensure target dir exists:

```bash
mkdir -p ~/.claude/rules  # timeout: 5000
```

**Phase 1 — Remove obsolete foundry-managed symlinks** (file/dir removed from current plugin version, or dangling target):

```bash
# re-resolve — state doesn't persist across steps; use installed cache path, not local fallback
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_plugin_root.py" --plugin-name foundry 2>/dev/null)  # timeout: 15000
[ -z "$PLUGIN_ROOT" ] && { printf "! setup Phase 1 — could not resolve PLUGIN_ROOT; run claude plugin install foundry@borda-ai-rig first\n"; exit 1; }
python "$PLUGIN_ROOT/bin/symlink_with_guard.py" cleanup --plugin-root "$PLUGIN_ROOT"  # timeout: 15000
```

The script walks `~/.claude/rules/` and `TEAM_PROTOCOL.md` and removes every link the current version does not provide — an obsolete rule, a dangling link left by a source rename, or a pre-namespace unprefixed link now superseded by `foundry-<name>.md`. Each removal prints `  removed obsolete: <name>`.

Removal requires proof of ownership: the link's target must resolve under `$PLUGIN_ROOT` or under the same `~/.claude/plugins/cache/<marketplace>/foundry/` lineage. A path *substring* is never accepted as proof — an earlier implementation used one and deleted a user's `dotfiles/plugins/cc_foundry/rules/…` link. A link into another marketplace, a sibling plugin's namespace, an arbitrary source checkout, or a dotfiles tree is left untouched even when its name collides.

Cleanup also purges two dest dirs unconditionally — both skills and agents are served from the plugin namespace, never via `~/.claude/` entries:

- `~/.claude/skills/` — every foundry-managed symlink removed, **including ones pointing at the current version**, because a current-version link is the defect itself (a `SKILL.md` dir there registers as a user-level skill and shadows CC's bundled skill of that name). `_shared` gets no exemption. Prints `  removed user-level skill link: <name>`.
- `~/.claude/agents/` — foundry-managed symlinks removed, except current-version ones, which are kept as a signal that something outside setup is staging them. Prints `  removed obsolete agent: <name>`.

**Phase 2 — Conflict scan** — identify entries needing user confirmation. Stale foundry symlinks (old version → current) are auto-replaced in Phase 4 without prompt:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
mapfile -t LINK_CONFLICTS < <(python "$PLUGIN_ROOT/bin/symlink_with_guard.py" scan --plugin-root "$PLUGIN_ROOT")  # timeout: 30000
printf '%s\n' "${LINK_CONFLICTS[@]}" > "${TMPDIR:-/tmp}/foundry-setup-conflicts-${CSID}.txt"  # timeout: 3000 — persist for Phase 4, calls don't share state
```

The `scan` mode walks the same two patterns (rules `*.md`, `TEAM_PROTOCOL.md`) and prints one conflict per line. Entries surface only when the dest is a real file or a symlink failing the same ownership proof Phase 1 uses. Each line names the *destination* file: `rules/foundry-<name>.md → <target>` · `rules/foundry-<name>.md  (real file)` · `TEAM_PROTOCOL.md → <target>`.

**Phase 3 — Handle remaining conflicts** (real files or symlinks to non-foundry paths):

If `$LINK_CONFLICTS` empty: skip to Phase 4.

If `APPROVE_ALL=true`: print `[--approve] auto-accepting: replace all symlink conflicts` and replace all (apply option a below). # --approve mode: auto-accept all conflicts; AskUserQuestion skipped

Otherwise, use `AskUserQuestion`:

```markdown
These entries in ~/.claude/ would be replaced with symlinks to the foundry plugin:
  - <name>  (<current state>)
  - …
```

Options:

(a) Replace all ★ recommended (b) Skip all conflicts — keep existing files unchanged (c) Choose per entry

On **(b)**: set `SKIP_CONFLICTS_MODE=true`. On **(c)**: initialize `APPROVED_CONFLICT_ENTRIES=()` and `PER_ITEM_REVIEW_MODE=true`. **Cap**: if `${#LINK_CONFLICTS[@]} > 10`, emit warning "⚠ ${#LINK_CONFLICTS[@]} conflicts found — per-item review capped at 10; showing first 10. Run again for the rest." and process only the first 10. Collect per-entry consent in **ONE** `AskUserQuestion` call: build `ceil(N/4)` questions, each `multiSelect: true` with up to 4 options (harness cap), one option per conflicting entry, labelled with the entry name and its current state; header "Replace these?". A ticked option = approve replacing that entry; unticked = keep existing. Cap 10 conflicts → 3 questions in one call. Do not iterate one call per entry — per-entry consent is preserved by the per-entry option, not by a serial window. For each ticked entry: append the entry's identifier — the destination basename, i.e. `foundry-<name>.md` for rules, or `TEAM_PROTOCOL.md` — to `APPROVED_CONFLICT_ENTRIES`; unticked entries are left out. After the answers return, persist: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; printf '%s\n' "${APPROVED_CONFLICT_ENTRIES[@]}" > ${TMPDIR:-/tmp}/foundry-setup-approved-${CSID}.txt`. Items not in `$LINK_CONFLICTS` (current, stale foundry, absent) bypass this gate — handled silently in Phase 4.

**Phase 4 — Symlink** — for each approved, auto-replaced, or absent entry, `ln -sf` creates/replaces. Stale foundry symlinks from Phase 2 are included here (auto-replaced silently). Conflict guard depends on which Phase 3 branch fired:

- `SKIP_CONFLICTS_MODE=true` (option b): skip every entry that is a real file or non-foundry symlink — those are conflicts the user declined.
- `PER_ITEM_REVIEW_MODE=true` (option c): for entries that appear in `$LINK_CONFLICTS`, only replace when the entry's identifier is in `APPROVED_CONFLICT_ENTRIES`; otherwise skip. Entries not in `$LINK_CONFLICTS` (current / stale foundry / absent) always replace.
- Neither flag (option a or no conflicts): replace unconditionally.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# restore arrays from Phase 2/3; Bash calls don't share state
mapfile -t LINK_CONFLICTS < ${TMPDIR:-/tmp}/foundry-setup-conflicts-${CSID}.txt 2>/dev/null || LINK_CONFLICTS=()
mapfile -t APPROVED_CONFLICT_ENTRIES < ${TMPDIR:-/tmp}/foundry-setup-approved-${CSID}.txt 2>/dev/null || APPROVED_CONFLICT_ENTRIES=()

_approved() {
    local needle="$1"
    for e in "${APPROVED_CONFLICT_ENTRIES[@]:-}"; do
        [ "$e" = "$needle" ] && return 0
    done
    return 1
}
_in_conflicts() {
    local needle="$1"
    for c in "${LINK_CONFLICTS[@]:-}"; do
        # LINK_CONFLICTS entries start with "rules/<base>" or "TEAM_PROTOCOL.md"
        case "$c" in "$needle"*) return 0 ;; esac
    done
    return 1
}

for src in "$PLUGIN_ROOT/rules/"*.md; do
    # foundry- prefix must match _RULE_PREFIX in symlink_with_guard.py — scan output and this loop key on same dest name, else Phase 3 answers apply to wrong file
    base="foundry-$(basename "$src")"
    dest="$HOME/.claude/rules/$base"
    if [ "${SKIP_CONFLICTS_MODE:-false}" = "true" ] && [ -e "$dest" ] && [ ! -L "$dest" ]; then
        echo "  skipped (user choice b): $base"; continue
    fi
    if [ "${PER_ITEM_REVIEW_MODE:-false}" = "true" ] && _in_conflicts "rules/$base" && ! _approved "rules/$base"; then
        echo "  skipped (user choice c — not approved): $base"; continue
    fi
    unlink "$dest" 2>/dev/null || true; ln -sf "$src" "$dest"  # timeout: 5000
    echo "  linked: $base"
done  # timeout: 10000
dest="$HOME/.claude/TEAM_PROTOCOL.md"
if [ "${SKIP_CONFLICTS_MODE:-false}" = "true" ] && [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "  skipped (user choice b): TEAM_PROTOCOL.md"
elif [ "${PER_ITEM_REVIEW_MODE:-false}" = "true" ] && _in_conflicts "TEAM_PROTOCOL.md" && ! _approved "TEAM_PROTOCOL.md"; then
    echo "  skipped (user choice c — not approved): TEAM_PROTOCOL.md"
else
    unlink "$dest" 2>/dev/null || true; ln -sf "$PLUGIN_ROOT/TEAM_PROTOCOL.md" "$dest"  # timeout: 5000
    echo "  linked: TEAM_PROTOCOL.md"
fi
# NO skills loop — deliberate: SKILL.md dir under ~/.claude/skills/ becomes user-level skill, shadows CC's bundled skill; _shared excluded too (own-plugin resolver). Phase 1 purges leftover links
```

## Step 11: Purge orphaned plugin cache versions

**Must run AFTER Step 10 Phase 4** — Phase 4 re-points every `~/.claude/` symlink at the current version. Purging first would delete a cache dir that surviving links still target, turning stale-but-readable links into broken ones. Do not reorder.

Report first — deletes nothing:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 41)
python "$PLUGIN_ROOT/bin/purge_plugin_cache.py" --protect "$PLUGIN_ROOT" --protect "${CLAUDE_PLUGIN_ROOT:-}"  # timeout: 30000
```

Output `nothing to purge …` → print it, skip to Step 12 (no prompt).

Otherwise the report lists `<plugin>/<version>  <size>  orphaned <N>d ago  leases:N` plus a total. Deletion is irreversible, so gate it. If `APPROVE_ALL=true`: print `[--approve] auto-accepting: purge all listed cache versions` and take option (a) without prompting. Else invoke `AskUserQuestion`:

- (a) **Purge all listed** — reclaim every listed version
- (b) **Skip** — keep everything, proceed to Step 12
- (c) **Raise the age floor** — re-run report with a larger `--min-orphan-age-hours` (e.g. 168 = keep the last week)

On (a), re-invoke with the candidate count from the report as `--expect-count` — mismatch means the cache changed since the user saw the list, and nothing is deleted:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""
python "$PLUGIN_ROOT/bin/purge_plugin_cache.py" --apply --expect-count <N> --protect "$PLUGIN_ROOT" --protect "${CLAUDE_PLUGIN_ROOT:-}"  # timeout: 60000
```

Substitute `<N>` with the count from the report line before running. Versions newer than the age floor are deferred by design and clear on a later run — that is not a failure.

## Step 12: Write CLAUDE.src.md → ~/.claude/CLAUDE.md

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 41)
[ -f "$HOME/.claude/CLAUDE.md" ] && cp "$HOME/.claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md.bak"  # timeout: 5000
cp "$PLUGIN_ROOT/CLAUDE.src.md" "$HOME/.claude/CLAUDE.md"  # timeout: 5000
printf "  wrote: CLAUDE.src.md → ~/.claude/CLAUDE.md\n"
```

## Step 13: Final report

Print summary:

- Python: `<PYTHON_CMD>` (shim installed at ~/.local/bin/python / already on PATH / n/a)
- statusLine: set / skipped
- permissions.allow: N entries added
- enabledPlugins: set / skipped
- env defaults: N added / all already set
- advisorModel: set / skipped
- Rules removed obsolete: N (files no longer in current plugin version)
- User-level skill links removed: N (foundry skills invoke as `/foundry:<name>`)
- Agent symlinks removed from ~/.claude/agents/: N (stale foundry-managed symlinks purged)
- Rules linked: N → ~/.claude/rules/foundry-\*.md
- TEAM_PROTOCOL.md linked → ~/.claude/TEAM_PROTOCOL.md
- Cache purged: N orphaned version(s), M MB (or `skipped` / `nothing to purge`)
- CLAUDE.md written → ~/.claude/CLAUDE.md
- Backup at: ~/.claude/settings.json.bak

</workflow>

<notes>

**Uninstall leaves state behind**: Claude Code runs no cleanup hook on uninstall, and neither `claude plugin uninstall` nor `make clear-all` removes what setup created. After removing foundry, delete `~/.claude/rules/foundry-*.md` and `~/.claude/TEAM_PROTOCOL.md` by hand — they dangle once the plugin cache version is gone — and review the `statusLine`, `permissions`, `enabledPlugins`, `advisorModel`, and `env` keys setup merged into `~/.claude/settings.json`, which also survive.

**Follow-up gate omitted** — setup is one-shot; no iterative follow-up action applies. Step 13 Final report is terminal output; no `AskUserQuestion` gate required. (Step 11 has its own confirm gate before deleting cache dirs — that is a safety prompt, not a follow-up gate.)

**Testing setup changes**: Setup skill has no `.claude/skills/setup` entry — only reachable as `/foundry:setup` after plugin installed. To test: bump `version` in `plugins/cc_foundry/.claude-plugin/plugin.json`, run `claude plugin install foundry@borda-ai-rig` from repo root to refresh cache, invoke `/foundry:setup`. **Upgrade path**: After `claude plugin install foundry@borda-ai-rig` upgrades version, re-run `/foundry:setup` — Step 10 Phase 1 removes rules symlinks no longer in new version and purges every `~/.claude/skills/` foundry link; Phase 2–4 auto-replaces stale foundry rules symlinks without prompting; real-file and non-foundry-path conflicts still surfaced for user review. Note: `make sync-claude` calls `/foundry:setup` headlessly at end — rules symlinks updated automatically on every sync run.

</notes>
