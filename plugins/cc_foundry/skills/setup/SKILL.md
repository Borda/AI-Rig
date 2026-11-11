---
name: setup
description: Post-install setup for foundry plugin. Run once after installing on a new machine, or after a plugin version upgrade to sync settings and symlinks. Merges statusLine, permissions.allow, enabledPlugins, advisorModel, and env defaults (CLAUDE_CODE_ENABLE_TODO_TOOLS) into ~/.claude/settings.json; symlinks rules and TEAM_PROTOCOL.md into ~/.claude/; offers the shipped CLAUDE.md while preserving existing user content by default; purges orphaned plugin cache versions.
argument-hint: '[--approve]'
allowed-tools: Read, Write, Bash, AskUserQuestion
effort: low
---

<objective>

Set up foundry on new machine:

| Action | What happens |
| -- | -- |
| Detect Python 3.10+ (`python` / `py -3` / `python3`); use the shipped launcher for subsequent commands | ✓ |
| Merge `statusLine`, `permissions.allow`, `enabledPlugins`, `advisorModel`, `env` defaults → `~/.claude/settings.json` | ✓ |
| `rules/<name>.md` → `~/.claude/rules/foundry-<name>.md` | symlink |
| `TEAM_PROTOCOL.md` → `~/.claude/` | symlink |
| `CLAUDE.src.md` → `~/.claude/CLAUDE.md` (Step 12) | copy if absent; keep diverged file unless replacement explicitly chosen, back up before replacement |
| Purge orphaned plugin cache versions (`.orphaned_at`, age-gated, confirm-gated) | ✓ |
| `hooks/hooks.json` | auto — plugin system |
| Conflict review before overwriting existing user files | ✓ |

**Why is every rule renamed `foundry-<name>.md`?** `~/.claude/rules/` is one flat directory shared by every installed plugin, and four of them ship a `rules/quality-gates.md`. Installing source basenames would have them overwrite each other, so each plugin namespaces its own rules with its plugin name. The prefix is inert — verified against Claude Code 2.1.220 that it changes neither unconditional loading nor `paths:` frontmatter matching. Phase 1 migrates a pre-namespace unprefixed link (`quality-gates.md` → `foundry-quality-gates.md`) by removing the old one, but only when that old link provably belongs to foundry.

**Why symlink rules and TEAM_PROTOCOL.md (not copy)?** Both load at session startup. Symlinks = every session gets plugin's current version — no stale copies, no re-run after upgrades. Broken symlink after upgrade = obvious error; stale copy silently serves old content.

**Why copy `CLAUDE.md` instead (the one exception)?** `~/.claude/CLAUDE.md` is user-owned and routinely hand-edited. A symlink would point it at a read-only, version-pinned cache dir — user edits would fail or leak into the cache, and Step 11's purge would dangle it. So Step 12 copies, and pays for the copy with what the symlinks get for free: it detects divergence, gates the overwrite, and keeps a timestamped backup rather than assuming the destination is plugin-owned.

**Why NOT symlink skills?** A directory carrying a `SKILL.md` under `~/.claude/skills/` registers as a **user-level** skill, and user-level skills silently shadow Claude Code's bundled skill of the same name. That already cost bare `/review` (hit CC's bundled reviewer instead of `oss:review`). Foundry skills dispatch as `/foundry:<name>` — no `~/.claude/skills/` entry needed, ever. `skills/_shared` excluded too: no plugin may depend on a global `_shared` path — each resolves its own via `bin/resolve_shared_path.py`. Phase 1 purges any such symlink unconditionally, including ones pointing at the current version.

**Why not symlink agents?** Agents must use full plugin prefix (`foundry:sw-engineer`, not `sw-engineer`) for unambiguous dispatch. Plugin system exposes agents at `foundry:` namespace — no `~/.claude/agents/` symlinks needed. (Stale agent symlinks from prior installs removed by setup's Phase 1 cleanup.)

**Why hooks need no action?** `hooks/hooks.json` inside plugin registers automatically when plugin enabled. Setup's only hook-adjacent step: write `statusLine.command` path (Step 4) — `statusLine` is top-level settings key, not part of `hooks.json`.

NOT for: editing project `.claude/settings.json` (Step 8 READS it to offer advisorModel promotion, never writes it).

</objective>

<inputs>

- **No arguments** — interactive mode; prompts on conflicts.
- **`--approve`** — non-interactive mode; auto-accepts all recommended answers. Use for scripted or CI setups.

</inputs>

<workflow>

## Flag detection

`--approve` (case-insensitive) sets `APPROVE_ALL=true`; else `APPROVE_ALL=false`. The setup block below parses it in-bash, persists it to a sentinel and prints `APPROVE_ALL=<value>` — carry that stdout value as model context for every gate below.

**Unsupported flag check** — run before the setup block: scan `$ARGUMENTS` for `--<token>` tokens other than `--approve`. Found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--approve`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

`APPROVE_ALL=true`: every `AskUserQuestion` below **skipped** — ★ recommended option applied automatically. Print `[--approve] auto-accepting recommended option` in place of question.

## Setup block — flag, Python, plugin root, backup (Steps 1–2)

One block, no decision between its parts. Probe Python 3.10+ — required before any `bin/*.py` calls (Windows Store stub returns exit 9009 when given args; caught by `2>/dev/null`) — then run Step 1 and Step 2. A git repository is needed only for Step 6; global setup continues outside one.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
PYTHON_CMD=""
if command -v python >/dev/null 2>&1 && python --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="python"
elif command -v py >/dev/null 2>&1 && py -3 --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="py -3"
elif command -v python3 >/dev/null 2>&1 && python3 --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])"; then
    PYTHON_CMD="python3"
else
    printf "! Python 3.10+ not found — install Python 3.10+ and re-run /foundry:setup\n"
    exit 1
fi
python --version 2>/dev/null | grep -qE "Python 3\.(1[0-9]|[2-9][0-9])" || { printf "! BLOCKED — later steps call bare python; %s works but python does not — install python 3.10+ on PATH (bin/python py -3 fallback pending)\n" "$PYTHON_CMD"; exit 1; }
printf "  Python: %s\n" "$PYTHON_CMD"

APPROVE_ALL=false
printf '%s\n' "$ARGUMENTS" | grep -qiE '(^|[[:space:]])--approve([[:space:]]|$)' && APPROVE_ALL=true
echo "$APPROVE_ALL" > "${TMPDIR:-/tmp}/foundry-setup-approve-${CSID}"
echo "APPROVE_ALL=$APPROVE_ALL"

PLUGIN_ROOT=$($PYTHON_CMD "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_plugin_root.py" --plugin-name foundry 2>/dev/null)  # timeout: 15000
case $? in
    0) ;;
    2) echo "! SECURITY: resolve_plugin_root.py rejected the candidate root — aborting setup"; exit 1 ;;
    *) PLUGIN_ROOT="" ;;  # not found; handled by empty-check below
esac
echo "$PLUGIN_ROOT" > "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}"  # persist for later blocks (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! foundry plugin not found — install it first with: claude plugin marketplace add Borda/AI-Rig && claude plugin install foundry@borda-ai-rig\n"; exit 1; }
[ -f "$PLUGIN_ROOT/hooks/statusline.js" ] || { printf "! %s/hooks/statusline.js missing — aborting setup\n" "$PLUGIN_ROOT"; exit 1; }
echo "PLUGIN_ROOT=$PLUGIN_ROOT"

SETUP_BAK_TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$HOME/.claude" || exit 1
[ -f ~/.claude/settings.json ] || { printf '{}\n' > ~/.claude/settings.json || exit 1; }  # create empty in-bash if absent — no Write-tool permission prompt (headless-safe)
cp ~/.claude/settings.json "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}" || exit 1  # timeout: 5000
echo "$SETUP_BAK_TS" > "${TMPDIR:-/tmp}/foundry-setup-bak-ts-${CSID}"  # persist for restore in Step 9
```

No user Python shim is written.

## Step 1: Locate the installed plugin

Runs inside the setup block above. Resolve validated install root via canonical resolver — registry lookup, cache-scan fallback (skips `.orphaned_at`, newest by semver), and both security gates (under cache dir + `plugin.json` name match) live in the script; do not re-implement inline.

`$PLUGIN_ROOT` empty after both attempts: the block stops and reports: "foundry plugin not found — install it first with: `claude plugin marketplace add Borda/AI-Rig && claude plugin install foundry@borda-ai-rig`"

`$PLUGIN_ROOT/hooks/statusline.js` missing: the block stops and reports.

## Step 2: Back up settings.json

Runs inside the setup block above. Report: "Backed up ~/.claude/settings.json → ~/.claude/settings.json.bak-<timestamp>"

## Step 3: Check for stale hooks block

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo --dry-run > "${TMPDIR:-/tmp}/foundry-setup-hooks-mode-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" has-hooks ~/.claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

`hooks` key exists: run the classifier below in `--dry-run` mode before the gate; it prints `foundry: N, user: M` without writing settings. Only commands executing a shipped foundry hook from the current plugin root, its installed-cache lineage, `${CLAUDE_PLUGIN_ROOT}/hooks/`, or the legacy `~/.claude/hooks/` directory (expanding `$HOME` and `~`) are stale foundry entries. Other commands are user-owned; the mere presence of `hooks` does not establish stale configuration. Unrecognized settings shapes are preserved and reported as user-owned.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
IFS= read -r HOOK_FILTER_MODE < "${TMPDIR:-/tmp}/foundry-setup-hooks-mode-${CSID}" 2>/dev/null || HOOK_FILTER_MODE=--dry-run
python - "$PLUGIN_ROOT" "$HOOK_FILTER_MODE" <<'PY' || { printf "! failed stripping hooks — settings.json unchanged\n"; exit 1; }  # timeout: 5000
import json
import os
import shlex
import sys
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
if mode not in {"--dry-run", "--apply"}:
    raise SystemExit("invalid hook filter mode")
sys.path.insert(0, str(root / "bin"))
from settings_json import _atomic_write, _load

target = (Path(os.environ["HOME"]) / ".claude/settings.json").resolve()
settings = _load(target)
names = {path.name for path in (root / "hooks").glob("*.js")}
manifest = json.loads((root / "hooks/hooks.json").read_text(encoding="utf-8"))
shipped_commands = {
    hook["command"]
    for groups in manifest["hooks"].values()
    for group in groups
    for hook in group.get("hooks", [])
    if "command" in hook
}
cache = Path(os.environ["HOME"]) / ".claude/plugins/cache"
lineage = root.parent if root.parent.name == "foundry" and root.parent.parent.parent == cache else None


def foundry_hook(hook: object) -> bool:
    """Identify an executed hook with a shipped basename and foundry-owned path."""
    if not isinstance(hook, dict):
        return False
    command = hook.get("command", "")
    if not isinstance(command, str):
        return False
    if command in shipped_commands:
        return True
    try:
        tokens = shlex.split(command.replace("\\", "/"))
    except ValueError:
        return False
    if tokens[:1] == ["exec"]:
        tokens = tokens[1:]
    if len(tokens) < 2 or tokens[0].rsplit("/", 1)[-1] not in {"node", "node.exe", "python", "python3"}:
        return False
    path = Path(os.path.expandvars(os.path.expanduser(tokens[1])))
    owned_paths = {root / "hooks", Path.home() / ".claude/hooks"}
    if lineage is not None:
        owned_paths.add(lineage / path.parent.parent.name / "hooks")
    owned = path.parent in owned_paths or tokens[1] == "${CLAUDE_PLUGIN_ROOT}/hooks/" + path.name
    return owned and path.name in names


events = settings.get("hooks", {})
if not isinstance(events, dict):
    print("hooks: skipped (expected an object; kept user-owned value)")
    print("foundry: 0, user: 1" if mode == "--dry-run" else "removed: 0, kept: 1")
    raise SystemExit(0)
foundry_count = user_count = 0
for event, groups in list(events.items()):
    if not isinstance(groups, list):
        user_count += 1
        continue
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            user_count += 1
            kept.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            user_count += 1
            kept.append(group)
            continue
        filtered = [hook for hook in hooks if not foundry_hook(hook)]
        foundry_count += len(hooks) - len(filtered)
        user_count += len(filtered)
        if filtered or not hooks:
            kept.append({**group, "hooks": filtered})
    if kept:
        events[event] = kept
    else:
        del events[event]
if not events:
    settings.pop("hooks", None)
if mode == "--dry-run":
    print(f"foundry: {foundry_count}, user: {user_count}")
else:
    _atomic_write(target, settings)
    print(f"removed: {foundry_count}, kept: {user_count}")
PY
```

`APPROVE_ALL=true`: when the dry-run output reports `user: M` with M > 0, print `[--approve] auto-accepting: keep existing hooks`, apply option (b). Otherwise remove only stale foundry entries (option a); report the printed counts and choice.

Otherwise, use `AskUserQuestion`:

(a) Remove only stale foundry hook entries (backup in place from Step 2; recommended only when the dry-run reports `user: 0`) (b) Skip — keep existing hooks ★ recommended when the dry-run reports M > 0

On **(a)**: select apply mode below, then re-run the classifier block above verbatim. It prints `removed: N, kept: M` and filters individual foundry commands, preserving other commands, event groups, matcher metadata and unrelated settings; remove only groups left empty by the filter:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo --apply > "${TMPDIR:-/tmp}/foundry-setup-hooks-mode-${CSID}"
```

On **(b)**: warn "Double-firing risk: existing hooks block will fire alongside plugin-registered hooks." Continue.

## Step 4: Merge statusLine

Check if statusLine already points to the **current** plugin's statusline.js (filename match alone is insufficient — a stale entry from an older plugin version survives upgrades, silently runs the previous hook). Verify both that command contains `statusline.js` AND that `$PLUGIN_ROOT` path (with its version segment) appears in the command string:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }  # empty root makes contains("") true → stale entry kept
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" statusline-current ~/.claude/settings.json --root "$PLUGIN_ROOT" >/dev/null 2>&1  # timeout: 5000
```

Already set to current `$PLUGIN_ROOT`: report "statusLine already set to current plugin version — skipping." Otherwise Read the existing `statusLine`. An absent entry or a stale foundry command whose path proves the same installed-cache lineage may be updated below. A command executing a shipped hook basename from the legacy `~/.claude/hooks/` directory (expanding `$HOME` and `~`) is also stale foundry. A filename match alone does not prove ownership; treat any other existing value as custom.

`APPROVE_ALL=true`: keep a custom statusLine and report `[--approve] keeping custom statusLine`. In interactive mode use `AskUserQuestion`: (a) **Keep mine** ★ recommended · (b) **Replace with foundry statusLine**. Only explicit option (b) permits replacing a custom value.

Writes `statusLine` key to `~/.claude/settings.json`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" set-statusline ~/.claude/settings.json --command "node \"$PLUGIN_ROOT/hooks/statusline.js\"" || { printf "! failed updating statusLine — settings.json unchanged\n"; exit 1; }  # timeout: 5000
```

Writeback happens in-bash above (`settings_json.py` atomic rename — no Write-tool permission prompt, headless-safe). Report: `  statusLine: set to current plugin version`.

## Step 5: Merge permissions.allow and permissions.deny

Merge `$PLUGIN_ROOT/.claude-plugin/permissions-allow.json` and `permissions-deny.json` into `~/.claude/settings.json` via `merge_settings_permissions.py` below — one atomic write covers both arrays. A missing or invalid shipped file aborts the block with settings unchanged.

- **allow**: add only entries not already present (exact string match). Before merging, remove exactly the entries in `permissions-allow-retired.json`: the obsolete `Bash(gh api repos/*:*)` entry and the broad `Bash(claude:*)`, `Bash(find:*)`, `Bash(node:*)`, `Bash(sed:*)` and `Bash(eval:*)` shell grants. The shipped `Bash(gh api repos/*)` replaces only the obsolete repository API form; nothing replaces the retired shell grants.
- **deny**: first remove exactly the retired entries listed in `permissions-deny-retired.json` (exact string match): the deny entries earlier versions shipped for `gh` writes — a settings deny rule still applies after a hook `allow`, so they would defeat the user's one-time `gh-write` approval, and `hooks/gh-write-guard.js` now gates every gh write — and for local `git branch -D`/`-d` and `git tag -d`, since local Git runs freely. Then add only entries not already present.
- Preserve every other user allow and deny entry, and unrelated settings.

Writes merged `permissions.allow` and `permissions.deny` arrays:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
python "$PLUGIN_ROOT/bin/merge_settings_permissions.py" --allow "$PLUGIN_ROOT/.claude-plugin/permissions-allow.json" --drop-allow-file "$PLUGIN_ROOT/.claude-plugin/permissions-allow-retired.json" --deny "$PLUGIN_ROOT/.claude-plugin/permissions-deny.json" --retire-deny-file "$PLUGIN_ROOT/.claude-plugin/permissions-deny-retired.json" || { printf "! permissions merge failed — settings.json unchanged\n"; exit 1; }  # timeout: 5000
```

Writeback happens in-bash above (atomic rename). Report from the script's two output lines: "Added N new permissions.allow entries (M already present)." plus the total retired allow entries removed (`dropped`), and "Added N new permissions.deny entries (M already present)." plus how many retired deny entries were removed (`retired`).

## Step 6: Copy permissions-guide.md

This step writes to `.claude/permissions-guide.md` relative to current working directory only at a git repository root. Outside a repository root, skip this project-local copy with a note and continue global setup.

Copy `$PLUGIN_ROOT/permissions-guide.md` to `.claude/permissions-guide.md` — only if destination absent (preserves project-local edits via `/manage`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
if [ ! -e ".git" ]; then
    printf "  permissions-guide.md: skipped (not at a git repository root)\n"
elif [ ! -f ".claude/permissions-guide.md" ]; then  # timeout: 5000
    mkdir -p .claude && cp "$PLUGIN_ROOT/permissions-guide.md" ".claude/permissions-guide.md" && printf "  copied: permissions-guide.md\n" || { printf "! permissions-guide.md copy failed — check the project .claude directory\n"; exit 1; }
else
    printf "  permissions-guide.md already present — skipping\n"
fi
```

## Step 7: Merge enabledPlugins and env defaults

Bridge is optional. Resolve its installed path from the registry before offering enablement; an absent installation skips only bridge enablement, then continues to env defaults:

```bash
BRIDGE_ROOT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/get_plugin_install_path.py" borda-ai-rig bridge 2>/dev/null) || BRIDGE_ROOT=""  # timeout: 5000
[ -n "$BRIDGE_ROOT" ] && [ -d "$BRIDGE_ROOT" ] || { printf "  bridge: skipped (not installed)\n"; exit 0; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" plugin-enabled ~/.claude/settings.json --name "bridge@borda-ai-rig" >/dev/null 2>&1  # timeout: 5000
```

Printed `bridge: skipped (not installed)`: report skipped, continue to env defaults. Otherwise rc 0: report "enabledPlugins already set — skipping." Installed but not enabled: use `AskUserQuestion`: (a) **Skip bridge enablement** ★ recommended · (b) **Enable installed bridge plugin**. Only explicit option (b) runs the write below.

`APPROVE_ALL=true`: skip bridge enablement and report `[--approve] bridge enablement skipped (opt-in required)`; continue to env defaults.

Writes `enabledPlugins["bridge@borda-ai-rig"]` key:

```bash
BRIDGE_ROOT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/get_plugin_install_path.py" borda-ai-rig bridge 2>/dev/null) || BRIDGE_ROOT=""  # timeout: 5000
[ -n "$BRIDGE_ROOT" ] && [ -d "$BRIDGE_ROOT" ] || { printf "  bridge: skipped (not installed)\n"; exit 0; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" enable-plugin ~/.claude/settings.json --name "bridge@borda-ai-rig" || { printf "! failed updating enabledPlugins — settings.json unchanged\n"; exit 1; }  # timeout: 5000
```

Writeback happens in-bash above (`settings_json.py` atomic rename).

Then merge `$PLUGIN_ROOT/.claude-plugin/env-defaults.json` into the `env` object in `~/.claude/settings.json`. Claude Code ships task tools (`TaskCreate`/`TaskList`/`TaskUpdate`/`TaskGet`) **disabled** unless `CLAUDE_CODE_ENABLE_TODO_TOOLS` is set, so without this key every skill mandating task tracking silently no-ops outside a project that sets the var itself:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
# A user who deliberately set "0" keeps it; only absent keys get the shipped default
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" merge-env ~/.claude/settings.json --defaults "$PLUGIN_ROOT/.claude-plugin/env-defaults.json" || { printf "! failed merging env defaults — settings.json unchanged\n"; exit 1; }  # timeout: 5000
```

Writeback happens in-bash above (`settings_json.py` atomic rename). Report: `  env: N default(s) added (M already set)`. Existing values never overwritten — a key already present keeps user's value, including a deliberate `"0"`.

## Step 8: Merge advisorModel (from project settings)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
ADV=""
if [ -f ".claude/settings.json" ]; then
    ADV=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" advisor-model .claude/settings.json 2>/dev/null)  # timeout: 5000
fi
echo "$ADV" > "${TMPDIR:-/tmp}/foundry-setup-adv-${CSID}"
echo "ADV=$ADV"
```

`$ADV` empty: report `  advisorModel: skipped (not pinned in project .claude/settings.json)`, continue.

Check if global already equals it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r ADV < "${TMPDIR:-/tmp}/foundry-setup-adv-${CSID}" 2>/dev/null || ADV=""  # reload: fresh shell
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" advisor-matches ~/.claude/settings.json --model "$ADV" >/dev/null 2>&1  # timeout: 5000
```

Already equal: report `  advisorModel already set to <value> — skipping.`

Otherwise use `AskUserQuestion` before promoting a project value to global settings: (a) **Keep global advisorModel** ★ recommended · (b) **Use this project's value globally**. State the project value and that this changes the default for other projects, including when this repository is only being analysed. Only explicit option (b) runs the write below.

`APPROVE_ALL=true`: keep the global advisorModel and report `[--approve] project advisorModel not promoted (opt-in required)`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r ADV < "${TMPDIR:-/tmp}/foundry-setup-adv-${CSID}" 2>/dev/null || ADV=""  # reload: fresh shell
[ -n "$ADV" ] || { printf "! advisorModel sentinel empty — settings.json unchanged\n"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/settings_json.py" set-advisor ~/.claude/settings.json --model "$ADV" || { printf "! failed updating advisorModel — settings.json unchanged\n"; exit 1; }  # timeout: 5000
```

Writeback happens in-bash above (`settings_json.py` atomic rename — no Write-tool permission prompt). Report `  advisorModel: set to <value>`.

## Step 9: Validate

After all writes, confirm file parses as valid JSON:

```bash
python -m json.tool ~/.claude/settings.json > /dev/null  # timeout: 5000 — Step 1 guarantees Python for every JSON operation
```

`python -m json.tool` exits non-zero: restore from backup: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r SETUP_BAK_TS < "${TMPDIR:-/tmp}/foundry-setup-bak-ts-${CSID}" 2>/dev/null || SETUP_BAK_TS=$(ls -t "$HOME/.claude/settings.json.bak-"* 2>/dev/null | head -1 | sed 's/.*\.bak-//'); cp "$HOME/.claude/settings.json.bak-${SETUP_BAK_TS}" ~/.claude/settings.json || exit 1`, report error, stop. Valid: continue.

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

The script walks `~/.claude/rules/` and `TEAM_PROTOCOL.md`, removes every link the current version doesn't provide — an obsolete rule, a dangling link left by a source rename, or a pre-namespace unprefixed link now superseded by `foundry-<name>.md`. Each removal prints `  removed obsolete: <name>`.

Removal requires proof of ownership: link's target must resolve under `$PLUGIN_ROOT` or under the same `~/.claude/plugins/cache/<marketplace>/foundry/` lineage. A path *substring* is never accepted as proof — an earlier implementation used one and deleted a user's `dotfiles/plugins/cc_foundry/rules/…` link. A link into another marketplace, a sibling plugin's namespace, an arbitrary source checkout, or a dotfiles tree is left untouched even when its name collides.

Cleanup also purges two dest dirs unconditionally — both skills and agents are served from the plugin namespace, never via `~/.claude/` entries:

- `~/.claude/skills/` — every foundry-managed symlink removed, **including ones pointing at the current version**, because a current-version link is the defect itself (a `SKILL.md` dir there registers as a user-level skill, shadows CC's bundled skill of that name). `_shared` gets no exemption. Prints `  removed user-level skill link: <name>`.
- `~/.claude/agents/` — foundry-managed symlinks removed, except current-version ones, kept as a signal that something outside setup is staging them. Prints `  removed obsolete agent: <name>`.

**Phase 2 — Conflict scan** — identify entries needing user confirmation. Stale foundry symlinks (old version → current) are auto-replaced in Phase 4 without prompt:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
# reset Phase 3 choice sentinels, approved list and scan marker — a prior run's answer or scan must not leak into Phase 4
echo false > "${TMPDIR:-/tmp}/foundry-setup-skip-conflicts-${CSID}"; echo false > "${TMPDIR:-/tmp}/foundry-setup-replace-all-${CSID}"; echo false > "${TMPDIR:-/tmp}/foundry-setup-per-item-${CSID}"; echo false > "${TMPDIR:-/tmp}/foundry-setup-scan-ok-${CSID}"; : > "${TMPDIR:-/tmp}/foundry-setup-approved-${CSID}.txt"
# file + exit status, never process substitution (its status is lost: a failed scan would read as zero conflicts and skip consent); no mapfile — absent in zsh
python "$PLUGIN_ROOT/bin/symlink_with_guard.py" scan --plugin-root "$PLUGIN_ROOT" > "${TMPDIR:-/tmp}/foundry-setup-conflicts-${CSID}.txt" || { printf "! BLOCKED — conflict scan failed (exit %s); nothing linked — fix the error above, re-run /foundry:setup\n" "$?"; exit 1; }  # timeout: 30000
echo true > "${TMPDIR:-/tmp}/foundry-setup-scan-ok-${CSID}"
echo "--- LINK_CONFLICTS ---"; cat "${TMPDIR:-/tmp}/foundry-setup-conflicts-${CSID}.txt"; echo "--- end LINK_CONFLICTS ---"
```

The `scan` mode walks the same two patterns (rules `*.md`, `TEAM_PROTOCOL.md`), prints one conflict per line. Entries surface only when dest is a real file or a symlink failing the same ownership proof Phase 1 uses. Each line names the *destination* file: `rules/foundry-<name>.md → <target>` · `rules/foundry-<name>.md  (real file)` · `TEAM_PROTOCOL.md → <target>`.

**Phase 3 — Handle remaining conflicts** (real files or symlinks to non-foundry paths):

Phase 2 printed no line between the `LINK_CONFLICTS` markers: skip to Phase 4. Phase 2 stopped with `! BLOCKED`: stop setup here — never run Phase 4.

`APPROVE_ALL=true`: print `[--approve] auto-accepting: keep existing conflict entries`, keep all (apply option b below) and report the skipped entries. # --approve mode: keep user files; AskUserQuestion skipped

Otherwise, use `AskUserQuestion`:

```markdown
These entries in ~/.claude/ would be replaced with symlinks to the foundry plugin:
  - <name>  (<current state>)
  - …
```

Options:

(a) Replace all — back up real files first (b) Skip all conflicts — keep existing files unchanged ★ recommended (c) Choose per entry

- On **(a)**: set `REPLACE_ALL_MODE=true` — persist it: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; echo true > "${TMPDIR:-/tmp}/foundry-setup-replace-all-${CSID}"`.
- On **(b)**: set `SKIP_CONFLICTS_MODE=true` — persist it: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; echo true > "${TMPDIR:-/tmp}/foundry-setup-skip-conflicts-${CSID}"`.
- On **(c)**: set `PER_ITEM_REVIEW_MODE=true` — persist the flag: `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; echo true > "${TMPDIR:-/tmp}/foundry-setup-per-item-${CSID}"`.
  - **Cap**: N = conflict lines Phase 2 printed; N > 10: put "⚠ N conflicts found — per-item review capped at 10; showing first 10. Run again for the rest." in the per-entry question text below (not reply text before the call), process only first 10.
  - Collect per-entry consent in **ONE** `AskUserQuestion` call: build `ceil(N/4)` questions, each `multiSelect: true` with up to 4 options (harness cap), one option per conflicting entry, labelled with entry name and its current state; header "Replace these?".
  - A ticked option = approve replacing that entry; unticked = keep existing. Cap 10 conflicts → 3 questions in one call. Do not iterate one call per entry — per-entry consent preserved by the per-entry option, not by a serial window.
  - For each ticked entry: append entry's identifier — the destination path exactly as the Phase 2 conflict line names it, i.e. `rules/foundry-<name>.md` for rules, or `TEAM_PROTOCOL.md` — to the approved-entries file (`foundry-setup-approved-${CSID}.txt`, emptied in Phase 2); unticked entries left out. Phase 4 matches this string verbatim against `rules/$base`, so a bare `foundry-<name>.md` never matches and silently skips an approved rule.
  - After answers return, persist the ticked identifiers, replacing `'<entry>'` with one single-quoted word per ticked identifier (none ticked: skip — the file is already empty, so Phase 4 skips every conflict; a model-context list does not survive into the next Bash call): `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; for ENTRY in '<entry>'; do case "$ENTRY" in *'<'*'>'*|"") echo "! BLOCKED — approved-entry placeholder unsubstituted"; exit 1 ;; esac; printf '%s\n' "$ENTRY" >> "${TMPDIR:-/tmp}/foundry-setup-approved-${CSID}.txt"; done`.

Items not on the Phase 2 conflict list (`foundry-setup-conflicts-${CSID}.txt`: current, stale foundry, absent) bypass this gate — handled silently in Phase 4.

**Phase 4 — Symlink** — for each approved, auto-replaced, or absent entry, `symlink_with_guard.py create` creates a native symlink or a tracked copy when symlinks are unavailable. Back up each real destination before an explicitly approved replacement; a failed backup stops before unlinking. Stale foundry symlinks from Phase 2 included here (auto-replaced silently). Conflict guard depends on which Phase 3 branch fired:

- `SKIP_CONFLICTS_MODE=true` (option b or the approve sentinel is `true`): skip every entry that is a real file or non-foundry symlink — those are conflicts the user declined.
- `PER_ITEM_REVIEW_MODE=true` (option c): for entries listed in the Phase 2 conflict file (`foundry-setup-conflicts-${CSID}.txt`), only replace when the entry's identifier is in the approved-entries file; otherwise skip. Entries not in the conflict file (current / stale foundry / absent) always replace.
- `REPLACE_ALL_MODE=true` (explicit option a): replace listed conflicts, backing up real files first. Without explicit replacement consent, keep listed conflicts. Entries not in the conflict file always replace.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Phase 2/3 lists stay files — Bash calls don't share state; no mapfile (absent in zsh)
_CONF="${TMPDIR:-/tmp}/foundry-setup-conflicts-${CSID}.txt"; _APPR="${TMPDIR:-/tmp}/foundry-setup-approved-${CSID}.txt"
IFS= read -r _SCAN_OK < "${TMPDIR:-/tmp}/foundry-setup-scan-ok-${CSID}" 2>/dev/null || _SCAN_OK=false
[ "$_SCAN_OK" = true ] || { printf "! BLOCKED — Phase 2 conflict scan did not complete; nothing linked — re-run /foundry:setup\n"; exit 1; }
IFS= read -r SKIP_CONFLICTS_MODE < "${TMPDIR:-/tmp}/foundry-setup-skip-conflicts-${CSID}" 2>/dev/null || SKIP_CONFLICTS_MODE=false
IFS= read -r _APPROVE < "${TMPDIR:-/tmp}/foundry-setup-approve-${CSID}" 2>/dev/null || _APPROVE=false
[ "$_APPROVE" = true ] && SKIP_CONFLICTS_MODE=true
IFS= read -r REPLACE_ALL_MODE < "${TMPDIR:-/tmp}/foundry-setup-replace-all-${CSID}" 2>/dev/null || REPLACE_ALL_MODE=false
IFS= read -r PER_ITEM_REVIEW_MODE < "${TMPDIR:-/tmp}/foundry-setup-per-item-${CSID}" 2>/dev/null || PER_ITEM_REVIEW_MODE=false
IFS= read -r _PR < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null && PLUGIN_ROOT="$_PR"  # reload: fresh shell (Check 43); caller-exported PLUGIN_ROOT kept when sentinel absent
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }

_approved() {
    [ -f "$_APPR" ] && grep -qxF -- "$1" "$_APPR"
}
_in_conflicts() {
    [ -f "$_CONF" ] || return 1
    while IFS= read -r c; do
        # conflict lines start with "rules/<base>" or "TEAM_PROTOCOL.md"
        case "$c" in "$1"*) return 0 ;; esac
    done < "$_CONF"
    return 1
}

for src in "$PLUGIN_ROOT/rules/"*.md; do
    # foundry- prefix must match _RULE_PREFIX in symlink_with_guard.py — scan output and this loop key on same dest name, else Phase 3 answers apply to wrong file
    base="foundry-$(basename "$src")"
    dest="$HOME/.claude/rules/$base"
    if [ "${SKIP_CONFLICTS_MODE:-false}" = "true" ] && _in_conflicts "rules/$base"; then
        echo "  skipped (user choice b): $base"; continue
    fi
    if [ "${PER_ITEM_REVIEW_MODE:-false}" = "true" ] && _in_conflicts "rules/$base" && ! _approved "rules/$base"; then
        echo "  skipped (user choice c — not approved): $base"; continue
    fi
    if _in_conflicts "rules/$base" && [ "$REPLACE_ALL_MODE" != true ] && ! { [ "$PER_ITEM_REVIEW_MODE" = true ] && _approved "rules/$base"; }; then
        echo "  skipped (replacement not approved): $base"; continue
    fi
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
        backup="${dest}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
        cp -p "$dest" "$backup" || exit 1  # timeout: 5000
        printf "  backup: %s\n" "$backup"
    fi
    sidecar="$HOME/.claude/rules/.$base.sourced_from"
    if [ -e "$sidecar" ] || [ -L "$sidecar" ]; then
        mv "$sidecar" "${sidecar}.bak-$(date -u +%Y%m%dT%H%M%SZ)" || exit 1
    fi
    if [ -e "$dest" ] || [ -L "$dest" ]; then unlink "$dest" || exit 1; fi
    tier=$(python "$PLUGIN_ROOT/bin/symlink_with_guard.py" create --plugin-root "$PLUGIN_ROOT" --src "$src" --dest "$dest") || exit 1  # timeout: 5000
    printf "  linked (%s): %s\n" "$tier" "$base"
done  # timeout: 10000
dest="$HOME/.claude/TEAM_PROTOCOL.md"
if [ "${SKIP_CONFLICTS_MODE:-false}" = "true" ] && _in_conflicts "TEAM_PROTOCOL.md"; then
    echo "  skipped (user choice b): TEAM_PROTOCOL.md"
elif [ "${PER_ITEM_REVIEW_MODE:-false}" = "true" ] && _in_conflicts "TEAM_PROTOCOL.md" && ! _approved "TEAM_PROTOCOL.md"; then
    echo "  skipped (user choice c — not approved): TEAM_PROTOCOL.md"
elif _in_conflicts "TEAM_PROTOCOL.md" && [ "$REPLACE_ALL_MODE" != true ] && ! { [ "$PER_ITEM_REVIEW_MODE" = true ] && _approved "TEAM_PROTOCOL.md"; }; then
    echo "  skipped (replacement not approved): TEAM_PROTOCOL.md"
else
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
        backup="${dest}.bak-$(date -u +%Y%m%dT%H%M%SZ)"
        cp -p "$dest" "$backup" || exit 1  # timeout: 5000
        printf "  backup: %s\n" "$backup"
    fi
    sidecar="$HOME/.claude/.TEAM_PROTOCOL.md.sourced_from"
    if [ -e "$sidecar" ] || [ -L "$sidecar" ]; then
        mv "$sidecar" "${sidecar}.bak-$(date -u +%Y%m%dT%H%M%SZ)" || exit 1
    fi
    if [ -e "$dest" ] || [ -L "$dest" ]; then unlink "$dest" || exit 1; fi
    tier=$(python "$PLUGIN_ROOT/bin/symlink_with_guard.py" create --plugin-root "$PLUGIN_ROOT" --src "$PLUGIN_ROOT/TEAM_PROTOCOL.md" --dest "$dest") || exit 1  # timeout: 5000
    printf "  linked (%s): TEAM_PROTOCOL.md\n" "$tier"
fi
# NO skills loop — deliberate: SKILL.md dir under ~/.claude/skills/ becomes user-level skill, shadows CC's bundled skill; _shared excluded too (own-plugin resolver). Phase 1 purges leftover links
```

## Step 11: Purge orphaned plugin cache versions

**Must run AFTER Step 10 Phase 4** — Phase 4 re-points every `~/.claude/` symlink at the current version. Purging first would delete a cache dir that surviving links still target, turning stale-but-readable links into broken ones. Don't reorder.

Report first — deletes nothing:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
python "$PLUGIN_ROOT/bin/purge_plugin_cache.py" --protect "$PLUGIN_ROOT" --protect "${CLAUDE_PLUGIN_ROOT:-}"  # timeout: 30000
```

Output `nothing to purge …`: print it, skip to Step 12 (no prompt).

Otherwise the report lists `<plugin>/<version>  <size>  orphaned <N>d ago  leases:N` plus a total. Deletion is irreversible, so gate it. `APPROVE_ALL=true`: print `[--approve] auto-accepting: purge all listed cache versions`, take option (a) without prompting. Else Write the full list to `.temp/setup/cache-purge.md` first (a prior run's file exists: Read it before the Write, which refuses an unread existing file); it is printed once, rendered as normal Markdown, as the last reply text before `AskUserQuestion` — Bash output is not reply text the user reads. The question text carries the purge headline, candidate count, total reclaimable size and `→ .temp/setup/cache-purge.md`. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options carry labels and one-line action descriptions, no `preview`; place the recommended option second without changing labels or answer mapping.

- (a) **Purge all listed** — reclaim every listed version
- (b) **Skip** — keep everything, proceed to Step 12
- (c) **Raise the age floor** — re-run report with a larger `--min-orphan-age-hours` (e.g. 168 = keep the last week)

On (a), re-invoke with the candidate count from the report as `--expect-count` — mismatch means the cache changed since the user saw the list, and nothing is deleted:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
EXPECT_COUNT='<N>'
case "$EXPECT_COUNT" in ''|*[!0-9]*) printf "! BLOCKED — cache count placeholder must be replaced with the reported integer\n"; exit 1 ;; esac
python "$PLUGIN_ROOT/bin/purge_plugin_cache.py" --apply --expect-count "$EXPECT_COUNT" --protect "$PLUGIN_ROOT" --protect "${CLAUDE_PLUGIN_ROOT:-}"  # timeout: 60000
```

Substitute `<N>` with count from report line before running. Versions newer than age floor are deferred by design, clear on a later run — that's not a failure.

## Step 12: Write CLAUDE.src.md → ~/.claude/CLAUDE.md

`~/.claude/CLAUDE.md` is a user-owned file that may be hand-authored — unlike every other destination in this skill, its prior contents cannot be reconstructed from the plugin. Detect divergence first, back up with a timestamp, and gate the overwrite.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""  # reload: fresh shell (Check 43)
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
[ -f "$PLUGIN_ROOT/CLAUDE.src.md" ] || { printf "! CLAUDE.src.md missing — live file unchanged\n"; exit 1; }
CLAUDE_MD_STATE=absent
if [ -f "$HOME/.claude/CLAUDE.md" ]; then
    cmp -s "$HOME/.claude/CLAUDE.md" "$PLUGIN_ROOT/CLAUDE.src.md" && CLAUDE_MD_STATE=identical || CLAUDE_MD_STATE=diverged
fi
printf "  CLAUDE.md: %s\n" "$CLAUDE_MD_STATE"  # timeout: 5000
```

`identical`: report `  CLAUDE.md already current — skipping.`, skip to Step 13 (no backup, no prompt, no write).

`absent`: write it directly — nothing can be lost:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
cp "$PLUGIN_ROOT/CLAUDE.src.md" "$HOME/.claude/CLAUDE.md" || exit 1  # timeout: 5000
printf "  wrote: CLAUDE.src.md → ~/.claude/CLAUDE.md (created)\n"
```

`diverged`: the existing file carries content this plugin did not write. `APPROVE_ALL=true`: print `[--approve] auto-accepting: keep existing ~/.claude/CLAUDE.md`, apply option (a) and report that the shipped version was not applied. Otherwise invoke `AskUserQuestion`:

- (a) **Keep mine — skip the write** ★ recommended — leave `~/.claude/CLAUDE.md` untouched; foundry rules still load via the Step 10 symlinks
- (b) **Replace with the shipped version** — timestamped backup written first, nothing overwritten in place
- (c) **Write side by side** — ship to `~/.claude/CLAUDE.src.md` for manual merge; leave the live file untouched

On (a): report `  CLAUDE.md: kept existing (user choice) — shipped version not applied.`, continue to Step 13.

On (b):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
[ -f "$PLUGIN_ROOT/CLAUDE.src.md" ] || { printf "! CLAUDE.src.md missing — live file unchanged\n"; exit 1; }
CLAUDE_BAK_TS=$(date -u +%Y%m%dT%H%M%SZ)
CLAUDE_MD_BAK="$HOME/.claude/CLAUDE.md.bak-${CLAUDE_BAK_TS}"
cp "$HOME/.claude/CLAUDE.md" "$CLAUDE_MD_BAK" || exit 1  # timeout: 5000
cp "$PLUGIN_ROOT/CLAUDE.src.md" "$HOME/.claude/CLAUDE.md" || exit 1  # timeout: 5000
printf "  wrote: CLAUDE.src.md → ~/.claude/CLAUDE.md (backup: %s)\n" "$CLAUDE_MD_BAK"
```

On (c):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLUGIN_ROOT < "${TMPDIR:-/tmp}/setup-plugin-root-${CSID}" 2>/dev/null || PLUGIN_ROOT=""
[ -n "$PLUGIN_ROOT" ] || { printf "! PLUGIN_ROOT sentinel missing — re-run /foundry:setup\n"; exit 1; }
cp "$PLUGIN_ROOT/CLAUDE.src.md" "$HOME/.claude/CLAUDE.src.md" || exit 1  # timeout: 5000
printf "  wrote: ~/.claude/CLAUDE.src.md — merge by hand into CLAUDE.md\n"
```

## Step 13: Final report

Print summary:

- Python: `<PYTHON_CMD>` (shipped launcher available for subsequent calls)
- statusLine: set / kept custom / skipped
- hooks: removed N stale / kept (user-owned)
- permissions.allow: N entries added
- permissions.deny: N entries added, M retired entries removed
- enabledPlugins: set / skipped
- env defaults: N added / all already set
- advisorModel: set / skipped
- Rules removed obsolete: N (files no longer in current plugin version)
- User-level skill links removed: N (foundry skills invoke as `/foundry:<name>`)
- Agent symlinks removed from ~/.claude/agents/: N (stale foundry-managed symlinks purged)
- Rules linked: N → ~/.claude/rules/foundry-\*.md
- Rule conflicts kept: N
- TEAM_PROTOCOL.md: linked → ~/.claude/TEAM_PROTOCOL.md / kept existing
- Conflict backups: actual timestamped paths printed during Phase 4 (when replaced)
- Cache purged: N orphaned version(s), M MB (or `skipped` / `nothing to purge`)
- CLAUDE.md: written / skipped (already current) / kept existing (user choice) / side-by-side → ~/.claude/CLAUDE.src.md
- CLAUDE.md backup: ~/.claude/CLAUDE.md.bak-<timestamp> (when replaced)
- Backup at: ~/.claude/settings.json.bak-\<SETUP_BAK_TS> (substitute the actual Step 2 timestamp)

</workflow>

<notes>

**Uninstall leaves state behind**: Claude Code runs no cleanup hook on uninstall, and neither `claude plugin uninstall` nor `make clear-all` removes what setup created. After removing foundry, delete `~/.claude/rules/foundry-*.md` and `~/.claude/TEAM_PROTOCOL.md` by hand — they dangle once plugin cache version is gone — review `statusLine`, `permissions`, `enabledPlugins`, `advisorModel`, `env` keys setup merged into `~/.claude/settings.json`, which also survive.

**Follow-up gate omitted** — setup is one-shot; no iterative follow-up action applies. Step 13 Final report is terminal output; no `AskUserQuestion` gate required. (Step 11 has its own confirm gate before deleting cache dirs — that's a safety prompt, not a follow-up gate.)

**Testing setup changes**: Setup skill has no `.claude/skills/setup` entry — only reachable as `/foundry:setup` after plugin installed. To test: bump `version` in `plugins/cc_foundry/.claude-plugin/plugin.json`, run `claude plugin install foundry@borda-ai-rig` from repo root to refresh cache, invoke `/foundry:setup`.

**Upgrade path**: after `claude plugin install foundry@borda-ai-rig` upgrades version, re-run `/foundry:setup` — Step 10 Phase 1 removes rules symlinks no longer in new version, purges every `~/.claude/skills/` foundry link; Phase 2–4 auto-replaces stale foundry rules symlinks without prompting; real-file and non-foundry-path conflicts still surfaced for user review.

Note: `make sync-claude` calls `/foundry:setup` headlessly at end — rules symlinks updated automatically on every sync run.

</notes>
