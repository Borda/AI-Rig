**Re: Compress install-checks markdown to caveman format**

# Install Checks — I1, I2, I3

Checks validate post-install state in `~/.claude/`. Operate on home dir, not project `.claude/`. Run via `/foundry:audit setup` (or `/audit setup` after `foundry:init link`).

______________________________________________________________________

## Check I1 — Plugin cache intact

Verify foundry plugin installed and cache dir accessible.

```bash
RED='\033[1;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
printf "=== Check I1: foundry plugin cache ===\n"

REGISTRY="$HOME/.claude/plugins/installed_plugins.json"
if [ ! -f "$REGISTRY" ]; then
    printf "${RED}! HIGH${NC}: Check I1 — installed_plugins.json not found; plugin may not be installed\n"
else
    INSTALL_PATH=$(jq -r 'to_entries[] | select(.key | ascii_downcase | contains("foundry")) | .value.installPath // empty' \
        "$REGISTRY" 2>/dev/null | head -1)  # timeout: 5000
    if [ -z "$INSTALL_PATH" ]; then
        printf "${RED}! HIGH${NC}: Check I1 — foundry not found in installed_plugins.json\n"
        printf "  Fix: claude plugin marketplace add ./Borda-AI-Rig && claude plugin install foundry@borda-ai-rig\n"
    elif [ ! -d "$INSTALL_PATH" ]; then
        printf "${RED}! HIGH${NC}: Check I1 — install cache missing: %s\n" "$INSTALL_PATH"
        printf "  Fix: claude plugin install foundry@borda-ai-rig  (reinstall to rebuild cache)\n"
    else
        VERSION=$(jq -r 'to_entries[] | select(.key | ascii_downcase | contains("foundry")) | .value.version // "unknown"' \
            "$REGISTRY" 2>/dev/null | head -1)  # timeout: 5000
        printf "${GRN}✓${NC}: Check I1 — foundry cache intact at %s (version: %s)\n" "$INSTALL_PATH" "$VERSION"
        echo "$INSTALL_PATH" >/tmp/audit_install_plugin_root  # pass to I2/I3
    fi
fi
```

**Severity**: missing/broken cache → **high** (plugin non-functional).

______________________________________________________________________

## Check I2 — Settings merge complete

Verify `foundry:init` ran: `~/.claude/settings.json` has required entries, no stale hooks block.

```bash
RED='\033[1;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
printf "=== Check I2: ~/.claude/settings.json merge ===\n"

SETTINGS="$HOME/.claude/settings.json"

if [ ! -f "$SETTINGS" ]; then
    printf "${RED}! HIGH${NC}: Check I2 — ~/.claude/settings.json not found\n"
else
    FAIL=0

    # I2a — statusLine: must reference statusline.js (any path)
    if ! jq -e '(.statusLine.command // "") | contains("statusline.js")' "$SETTINGS" >/dev/null 2>&1; then  # timeout: 5000
        printf "${YEL}⚠ MEDIUM${NC}: Check I2a — statusLine not set to statusline.js\n"
        printf "  Fix: run /foundry:init\n"
        FAIL=$((FAIL + 1))
    else
        printf "${GRN}✓${NC}: Check I2a — statusLine set\n"
    fi

    # I2b — permissions.allow: spot-check that foundry entries were merged (>10 entries expected)
    if ! jq -e '(.permissions.allow // []) | length > 10' "$SETTINGS" >/dev/null 2>&1; then  # timeout: 5000
        printf "${YEL}⚠ MEDIUM${NC}: Check I2b — permissions.allow appears empty or very short; foundry entries may not have been merged\n"
        printf "  Fix: run /foundry:init\n"
        FAIL=$((FAIL + 1))
    else
        printf "${GRN}✓${NC}: Check I2b — permissions.allow populated\n"
    fi

    # I2c — enabledPlugins: codex@openai-codex must be true
    if ! jq -e '.enabledPlugins["codex@openai-codex"] == true' "$SETTINGS" >/dev/null 2>&1; then  # timeout: 5000
        printf "${YEL}⚠ MEDIUM${NC}: Check I2c — enabledPlugins.codex@openai-codex not set to true\n"
        printf "  Fix: run /foundry:init\n"
        FAIL=$((FAIL + 1))
    else
        printf "${GRN}✓${NC}: Check I2c — enabledPlugins.codex@openai-codex enabled\n"
    fi

    # I2d — stale hooks block: must be absent (double-fires with plugin hooks.json)
    if jq -e 'has("hooks")' "$SETTINGS" >/dev/null 2>&1; then  # timeout: 5000
        printf "${YEL}⚠ MEDIUM${NC}: Check I2d — 'hooks' key present in ~/.claude/settings.json; stale block from before plugin migration will cause double-firing\n"
        printf "  Fix: run /foundry:init — it will offer to remove the stale hooks block\n"
        FAIL=$((FAIL + 1))
    else
        printf "${GRN}✓${NC}: Check I2d — no stale hooks block\n"
    fi

    [ "$FAIL" -eq 0 ] && printf "${GRN}✓${NC}: Check I2 — settings merge complete\n"
fi
```

**Severity**: missing entry or stale hooks block → **medium** per sub-check (non-blocking, degrades functionality). Fix: re-run `/foundry:init` — idempotent.

______________________________________________________________________

## Check I3 — Link health (conditional)

Runs only if `~/.claude/agents/` or `~/.claude/skills/` contain symlinks pointing to plugin cache path. Checks staleness — symlinks break silently after plugin version upgrade changes cache path.

```bash
RED='\033[1;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
printf "=== Check I3: ~/.claude/ link health ===\n"

INSTALL_PATH=$(cat /tmp/audit_install_plugin_root 2>/dev/null)
LINKED=0
STALE=0

# Agents: check .md symlinks in ~/.claude/agents/
for f in "$HOME/.claude/agents/"*.md; do
    [ -e "$f" ] || continue
    if [ -L "$f" ]; then
        LINKED=$((LINKED + 1))
        [ ! -f "$f" ] && STALE=$((STALE + 1)) && \
            printf "${RED}! HIGH${NC}: Check I3 — broken symlink: %s -> %s\n" "$f" "$(readlink "$f" 2>/dev/null)"
    fi
done

# Skills: check directory symlinks in ~/.claude/skills/
for d in "$HOME/.claude/skills/"/*/; do
    [ -e "$d" ] || continue
    d="${d%/}"
    if [ -L "$d" ]; then
        LINKED=$((LINKED + 1))
        [ ! -d "$d" ] && STALE=$((STALE + 1)) && \
            printf "${RED}! HIGH${NC}: Check I3 — broken symlink: %s -> %s\n" "$d" "$(readlink "$d" 2>/dev/null)"
    fi
done

if [ "$LINKED" -eq 0 ]; then
    printf "${GRN}✓${NC}: Check I3 — no foundry symlinks in ~/.claude/ (foundry:init link not run; skipping)\n"
elif [ "$STALE" -eq 0 ]; then
    printf "${GRN}✓${NC}: Check I3 — %d symlink(s) all resolve correctly\n" "$LINKED"
else
    printf "${RED}! HIGH${NC}: Check I3 — %d of %d symlink(s) broken (likely stale after plugin version upgrade)\n" "$STALE" "$LINKED"
    printf "  Fix: re-run /foundry:init link — it will replace stale symlinks with the current cache path\n"
fi
```

**Severity**: broken symlinks → **high** (agents/skills silently unavailable at root namespace). Fix: re-run `/foundry:init link` — detects and replaces stale symlinks.
