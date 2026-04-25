**Re: Compress setup-checks markdown to caveman format**

# Setup Checks — 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11

## Check 1 — Inventory drift (MEMORY.md vs disk)

Use Glob (`agents/*.md`, path `.claude/`) to list agent files; extract basenames, sort, write to `/tmp/agents_disk.txt` via Bash:

```bash
ls .claude/agents/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.md$//' | sort >/tmp/agents_disk.txt || true # timeout: 5000
```

Read `- Agents:` and `- Skills:` roster lines from MEMORY.md content injected in conversation context (available as auto-memory at session start). Do not Grep a file path — MEMORY.md not stored under `.claude/` but in Claude Code's auto-memory system. Repeat with Glob (`skills/*/`, path `.claude/`) for skills on disk — write to `/tmp/skills_disk.txt`.

**macOS caution**: BSD grep treats arguments starting with `-` as option flags. When constructing bash comparison from MEMORY.md roster via grep, always use `grep -E 'Agents:'` (no leading `- `) or `grep -- '- Agents:'` not `grep '- Agents:'` — latter exits 2 on macOS, silently produces empty result. Safest: use Read tool (not grep) for MEMORY.md as stated above.

## Check 2 — README vs disk

Use Grep tool (pattern `^\| \*\*`, file `README.md`, output mode `content`) to extract agent/skill table rows.

## Check 3 — settings.json permissions

Use Grep tool (pattern `gh |python -m|ruff|mypy|pytest`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to collect bash commands used in skills.

## Check 4 — permissions-guide.md drift

Every allow entry must appear in guide, and vice versa.

```bash
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then  # timeout: 5000
    printf "${YEL}⚠ SKIPPED${NC}: Check 4 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 4 — .claude/settings.json not found\n"
elif [ ! -f ".claude/permissions-guide.md" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 4 — .claude/permissions-guide.md not found\n"
else
    # Allow entries missing from guide
    jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | \  # timeout: 5000
    while IFS= read -r perm; do
        grep -qF "\`$perm\`" .claude/permissions-guide.md 2>/dev/null \
            || printf "${YEL}⚠ MISSING from guide${NC}: %s\n" "$perm"
    done

    # Guide entries orphaned (not in allow list)
    grep '^| `' .claude/permissions-guide.md 2>/dev/null | awk -F'`' '{print $2}' | \  # timeout: 5000
    while IFS= read -r perm; do
        jq -e --arg p "$perm" '(.permissions.allow // []) + (.permissions.deny // []) | contains([$p])' .claude/settings.json > /dev/null 2>&1 \  # timeout: 5000
        || printf "${YEL}⚠ ORPHANED in guide${NC}: %s\n" "$perm"
    done
fi
```

## Check 5 — Permission safety audit

Every `allow` entry must be non-destructive, reversible, local-only.

Read `.claude/settings.json` with Read tool, extract `permissions.allow` list. For each entry, use model reasoning to evaluate against three criteria:

- **Non-destructive**: no permanent delete/overwrite (no `rm -rf`, `git push --force`, `DROP TABLE`)
- **Reversible**: effect undoable without data loss (local file edits, test runs, read-only queries)
- **Local-only**: no effect outside working directory, no external data transmission

Flag destructive patterns as **critical** (auto-approved destructive commands always breaking safety failure). Flag external-state mutations as **high**, raise to user — some (e.g., `gh release create`) may be intentional but must be explicitly acknowledged.

## Check 6 — Stale settings.json allow entries

```bash
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then # timeout: 5000
    printf "${YEL}⚠ SKIPPED${NC}: Check 6 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 6 — .claude/settings.json not found\n"
else
    printf "=== Check 6: Stale allow entries ===\n"
    jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | while IFS= read -r entry; do # timeout: 5000
        cmd=$(echo "$entry" | sed 's/^[A-Za-z]*(\(.*\))$/\1/' | sed 's/^"\(.*\)"$/\1/')
        hits=$(grep -rl "$cmd" .claude/agents/ .claude/skills/ .claude/rules/ .claude/hooks/ .claude/CLAUDE.md 2>/dev/null | wc -l | tr -d ' ') # timeout: 5000
        if [ "$hits" -eq 0 ]; then
            printf "${YEL}⚠ STALE allow${NC}: %s — no usage found in .claude/ files\n" "$entry"
        fi
    done
    printf "${GRN}✓${NC}: Check 6 scan complete\n"
fi
```

**Severity**: **low** per stale entry. Fix: remove stale entry from `settings.json` (report only — `settings.json` never auto-edited per audit policy).

**Important**: some allow entries intentionally grant broad patterns (e.g., `Bash(mkdir -p .reports/audit/*)`) that don't appear verbatim in config files — exercised at runtime. Flag only entries whose command fragment appears nowhere in any `.claude/` file.

## Check 7 — codex plugin integration check

Skip if codex (openai-codex) plugin not installed.

```bash
RED='\033[1;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'
CODEX_LINE=$(claude plugin list 2>/dev/null | grep 'codex@openai-codex') # timeout: 5000
if [ -z "$CODEX_LINE" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 7 — codex (openai-codex) plugin not installed\n"
elif echo "$CODEX_LINE" | grep -q 'disabled'; then
    printf "${YEL}⚠ WARN${NC}: Check 7 — codex (openai-codex) plugin installed but DISABLED\n"
    printf "  Fix: run \`claude plugin enable codex@openai-codex\` then \`/reload-plugins\`\n"
else
    printf "${GRN}✓ OK${NC}: Check 7 — codex (openai-codex) plugin present and enabled\n"
fi
```

- Plugin installed but **disabled** → **medium** (fix: `claude plugin enable codex@openai-codex` + `/reload-plugins`)
- Plugin present but dispatches fail → **high** (verify with `/calibrate skills`)

## Check 8 — foundry plugin correctness

Verify repo's `foundry` plugin structure at `plugins/foundry/`. Skip if `plugins/foundry/` not found.

```bash
RED='\033[1;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
printf "=== Check 8: foundry plugin correctness ===\n"

PLUGIN_DIR="plugins/foundry"

if [ ! -d "$PLUGIN_DIR" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 8 — plugins/foundry/ not found\n"
else
    FAIL=0

    # 8a — Manifest: exists, valid JSON, required fields
    MANIFEST="$PLUGIN_DIR/.claude-plugin/plugin.json"
    if [ ! -f "$MANIFEST" ]; then
        printf "${RED}! CRITICAL${NC}: Check 8a — manifest not found: %s\n" "$MANIFEST"
        FAIL=$((FAIL + 1))
    elif ! python3 -c "import json,sys; d=json.load(open('$MANIFEST')); [sys.exit(1) for k in ('name','version','description') if k not in d]" 2>/dev/null; then # timeout: 5000
        printf "${RED}! CRITICAL${NC}: Check 8a — manifest invalid JSON or missing required fields (name, version, description)\n"
        FAIL=$((FAIL + 1))
    else
        PLUGIN_NAME=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['name'])" 2>/dev/null) # timeout: 5000
        if [ "$PLUGIN_NAME" != "foundry" ]; then
            printf "${RED}! HIGH${NC}: Check 8a — manifest name is '%s', expected 'foundry'\n" "$PLUGIN_NAME"
            FAIL=$((FAIL + 1))
        else
            printf "${GRN}✓${NC}: Check 8a — manifest valid (name: foundry)\n"
        fi
    fi

    # 8b — agents/ and skills/ must be real directories in plugin (canonical source)
    #       .claude/agents/*.md and .claude/skills/*/ must be symlinks pointing INTO plugin
    for dir in agents skills; do
        REAL_PATH="$PLUGIN_DIR/$dir"
        if [ -L "$REAL_PATH" ]; then
            printf "${RED}! HIGH${NC}: Check 8b — %s is a symlink; expected real directory (canonical source)\n" "$REAL_PATH"
            FAIL=$((FAIL + 1))
        elif [ ! -d "$REAL_PATH" ]; then
            printf "${RED}! HIGH${NC}: Check 8b — %s directory not found\n" "$REAL_PATH"
            FAIL=$((FAIL + 1))
        else
            printf "${GRN}✓${NC}: Check 8b — real directory: %s\n" "$REAL_PATH"
        fi
    done
    # Verify .claude/ entries are reverse symlinks into plugin
    for dir in agents skills; do
        LOCAL=".claude/$dir"
        if [ "$dir" = "agents" ]; then
            # agents: individual .md symlinks
            BROKEN=$(find "$LOCAL" -maxdepth 1 -name "*.md" ! -type l 2>/dev/null | wc -l | tr -d " ")
            STALE=$(find "$LOCAL" -maxdepth 1 -name "*.md" -type l ! -readable 2>/dev/null | wc -l | tr -d " ")
            [ "$BROKEN" -gt 0 ] && printf "${YEL}⚠ MEDIUM${NC}: Check 8b — %d non-symlink .md file(s) in .claude/agents/ (expected symlinks → plugin)\n" "$BROKEN"
            [ "$STALE" -gt 0 ] && printf "${RED}! HIGH${NC}: Check 8b — %d broken symlink(s) in .claude/agents/\n" "$STALE"
            [ "$BROKEN" -eq 0 ] && [ "$STALE" -eq 0 ] && printf "${GRN}✓${NC}: Check 8b — .claude/agents/ symlinks valid\n"
        else
            # skills: directory-level symlinks
            BROKEN=$(find "$LOCAL" -maxdepth 1 -mindepth 1 -type d ! -type l 2>/dev/null | wc -l | tr -d " ")
            STALE=$(find "$LOCAL" -maxdepth 1 -mindepth 1 -type l ! -readable 2>/dev/null | wc -l | tr -d " ")
            [ "$BROKEN" -gt 0 ] && printf "${YEL}⚠ MEDIUM${NC}: Check 8b — %d real directory/directories in .claude/skills/ (expected symlinks → plugin)\n" "$BROKEN"
            [ "$STALE" -gt 0 ] && printf "${RED}! HIGH${NC}: Check 8b — %d broken symlink(s) in .claude/skills/\n" "$STALE"
            [ "$BROKEN" -eq 0 ] && [ "$STALE" -eq 0 ] && printf "${GRN}✓${NC}: Check 8b — .claude/skills/ symlinks valid\n"
        fi
    done

    # 8c — Hook scripts: real files in plugin; hooks auto-register via hooks.json + CLAUDE_PLUGIN_ROOT
    HOOKS_DIR="$PLUGIN_DIR/hooks"
    HOOKS_JSON="$HOOKS_DIR/hooks.json"
    if [ ! -d "$HOOKS_DIR" ]; then
        printf "${RED}! HIGH${NC}: Check 8c — hooks/ directory not found in plugin\n"
        FAIL=$((FAIL + 1))
    else
        JS_FAIL=0
        for js in "$HOOKS_DIR"/*.js; do
            [ -e "$js" ] || continue
            name=$(basename "$js")
            # 8c-i: plugin hooks must be real files, not symlinks
            if [ -L "$js" ]; then
                printf "${YEL}⚠ MEDIUM${NC}: Check 8c — %s is a symlink; expected real file in plugin hooks/\n" "$name"
                JS_FAIL=$((JS_FAIL + 1))
            fi
            # 8c-ii: verify every .js referenced in hooks.json exists in plugin hooks/
            # (hooks auto-register via hooks.json + CLAUDE_PLUGIN_ROOT — no .claude/hooks/ symlinks needed)
        done
        [ "$JS_FAIL" -eq 0 ] && printf "${GRN}✓${NC}: Check 8c — plugin hook files valid\n"

        # 8c-iii: hooks.json reference integrity — every referenced .js must exist
        if command -v node &>/dev/null && [ -f "$HOOKS_JSON" ]; then
            REF_FAIL=0
            while IFS= read -r js_name; do
                if [ ! -f "$HOOKS_DIR/$js_name" ]; then
                    printf "${RED}! HIGH${NC}: Check 8c — hooks.json references missing file: hooks/%s\n" "$js_name"
                    REF_FAIL=$((REF_FAIL + 1))
                fi
            done < <(node -e "
                const h = JSON.parse(require('fs').readFileSync('$HOOKS_JSON'));
                const scripts = new Set();
                Object.values(h.hooks || {}).flat().forEach(e => (e.hooks || []).forEach(hook => {
                    const m = (hook.command || '').match(/\\\$\\{CLAUDE_PLUGIN_ROOT\\}\\/hooks\\/([^\"'\\s]+\\.js)/);
                    if (m) scripts.add(m[1]);
                }));
                scripts.forEach(s => console.log(s));
            " 2>/dev/null)
            [ "$REF_FAIL" -eq 0 ] && printf "${GRN}✓${NC}: Check 8c — hooks.json references all resolve to plugin files\n"
            [ "$REF_FAIL" -gt 0 ] && FAIL=$((FAIL + 1))
        else
            printf "${YEL}⚠ SKIPPED${NC}: Check 8c-iii — node not available; hooks.json reference check skipped\n"
        fi
    fi

    # 8d — hooks.json: exists, valid JSON
    if [ ! -f "$HOOKS_JSON" ]; then
        printf "${RED}! HIGH${NC}: Check 8d — hooks/hooks.json not found\n"
        FAIL=$((FAIL + 1))
    elif ! python3 -c "import json; json.load(open('$HOOKS_JSON'))" 2>/dev/null; then # timeout: 5000
        printf "${RED}! HIGH${NC}: Check 8d — hooks/hooks.json is not valid JSON\n"
        FAIL=$((FAIL + 1))
    else
        printf "${GRN}✓${NC}: Check 8d — hooks/hooks.json is valid JSON\n"
    fi

    # 8e — Dry-run validate via claude plugin validate
    if ! command -v claude &>/dev/null; then # timeout: 5000
        printf "${YEL}⚠ SKIPPED${NC}: Check 8e — claude CLI not in PATH\n"
    else
        VALIDATE_OUT=$(claude plugin validate "./$PLUGIN_DIR" 2>&1) # timeout: 15000
        VALIDATE_EXIT=$?
        if [ "$VALIDATE_EXIT" -ne 0 ]; then
            printf "${RED}! HIGH${NC}: Check 8e — \`claude plugin validate\` failed:\n%s\n" "$VALIDATE_OUT"
            FAIL=$((FAIL + 1))
        else
            printf "${GRN}✓${NC}: Check 8e — \`claude plugin validate\` passed\n"
        fi
    fi

    # 8f — permissions-allow.json vs settings.json drift (skipped on marketplace install if no .claude/)
    PERM_JSON="$PLUGIN_DIR/.claude-plugin/permissions-allow.json"
    if [ ! -f "$PERM_JSON" ]; then
        printf "${YEL}\u26a0 SKIPPED${NC}: Check 8f \u2014 permissions-allow.json not found at %s\n" "$PERM_JSON"
    elif ! command -v jq &>/dev/null; then # timeout: 5000
        printf "${YEL}\u26a0 SKIPPED${NC}: Check 8f \u2014 jq not available\n"
    elif [ ! -f ".claude/settings.json" ]; then
        printf "${YEL}\u26a0 SKIPPED${NC}: Check 8f \u2014 no .claude/settings.json (marketplace install; skipping drift check)\n"
    else
        MISSING_FROM_PLUGIN=$(comm -23 \
                <(jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | sort) \
            <(jq -r '.[]' "$PERM_JSON" 2>/dev/null | sort)) # timeout: 10000
        MISSING_FROM_SETTINGS=$(comm -23 \
                <(jq -r '.[]' "$PERM_JSON" 2>/dev/null | sort) \
            <(jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | sort)) # timeout: 10000
        if [ -n "$MISSING_FROM_PLUGIN" ]; then
            COUNT=$(echo "$MISSING_FROM_PLUGIN" | wc -l | tr -d ' ')
            printf "${YEL}\u26a0 MEDIUM${NC}: Check 8f \u2014 %d allow entries in settings.json absent from permissions-allow.json (plugin users won't get them)\n" "$COUNT"
            echo "$MISSING_FROM_PLUGIN" | sed 's/^/    /'
            FAIL=$((FAIL + 1))
        fi
        if [ -n "$MISSING_FROM_SETTINGS" ]; then
            COUNT=$(echo "$MISSING_FROM_SETTINGS" | wc -l | tr -d ' ')
            printf "${YEL}\u26a0 LOW${NC}: Check 8f \u2014 %d permissions-allow.json entries absent from .claude/settings.json\n" "$COUNT"
            echo "$MISSING_FROM_SETTINGS" | sed 's/^/    /'
        fi
        if [ -z "$MISSING_FROM_PLUGIN" ] && [ -z "$MISSING_FROM_SETTINGS" ]; then
            printf "${GRN}\u2713${NC}: Check 8f \u2014 permissions-allow.json and .claude/settings.json in sync\n"
        fi
    fi

    # 8g — init skill: exists in plugin only (not in .claude/skills/), declares required behaviors
    SF_SKILL="$PLUGIN_DIR/skills/init/SKILL.md"
    if [ ! -f "$SF_SKILL" ]; then
        printf "${RED}! HIGH${NC}: Check 8g — init SKILL.md not found at %s\n" "$SF_SKILL"
        FAIL=$((FAIL + 1))
    else
        # Must NOT exist as standalone .claude/skills/init/ (plugin-only skill)
        if [ -e ".claude/skills/init" ]; then
            printf "${YEL}⚠ MEDIUM${NC}: Check 8g — .claude/skills/init/ exists; init skill should live only in the plugin\n"
        fi
        # Must declare all settings it merges and the link subcommand
        MISSING_COVERAGE=0
        for KEYWORD in "statusLine" "permissions.allow" "codex@openai-codex" "link"; do
            if ! grep -qF "$KEYWORD" "$SF_SKILL"; then # timeout: 5000
                printf "${YEL}⚠ MEDIUM${NC}: Check 8g — init SKILL.md does not mention '%s'\n" "$KEYWORD"
                MISSING_COVERAGE=$((MISSING_COVERAGE + 1))
            fi
        done
        if [ "$MISSING_COVERAGE" -eq 0 ]; then
            printf "${GRN}✓${NC}: Check 8g — setup-foundry SKILL.md present and covers required settings\n"
        else
            FAIL=$((FAIL + 1))
        fi
    fi

    if [ "$FAIL" -eq 0 ]; then
        printf "${GRN}✓ OK${NC}: Check 8 — foundry plugin structure valid\n"
    else
        printf "${RED}✗${NC}: Check 8 — %d issue(s) found\n" "$FAIL"
    fi
fi
```

**Severity**: manifest missing/invalid JSON → **critical**; broken symlink, hooks.json invalid, hooks.json references missing file, or `claude plugin validate` fails → **high**; .js plugin file is symlink (not real file) → **medium**; 8f permissions-allow.json entries missing from settings.json → **medium**; settings.json entries missing from permissions-allow.json → **low**; setup-foundry SKILL.md missing → **high**; missing required keyword coverage → **medium**. **Report only** — never auto-fix.

## Check 9 — Agent color drift (statusline COLOR_MAP vs frontmatter)

```bash
# Extract color: values declared in agent frontmatter
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    color=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^color:/{sub(/^color: */,""); print}' "$f")
    [ -n "$color" ] && printf "%s: %s\n" "$name" "$color"
done
```

Using model reasoning, cross-reference each extracted color name against `COLOR_MAP` keys in `.claude/hooks/statusline.js`. Flag:

- Color in agent frontmatter but **not a key in `COLOR_MAP`** → **medium** (agent appears uncolored)
- Color in `COLOR_MAP` not declared by any agent → **low** (dead mapping, no functional impact)

## Check 10 — RTK hook alignment

Verify prefix list in `.claude/hooks/rtk-rewrite.js` (`RTK_PREFIXES` array) consistent with commands installed RTK binary supports.

Skip if RTK not installed (`rtk --version` fails) or `.claude/hooks/rtk-rewrite.js` not found.

```bash
YEL='\033[1;33m'
RED='\033[1;31m'
GRN='\033[0;32m'
CYN='\033[0;36m'
NC='\033[0m'
printf "=== Check 10: RTK hook alignment ===\n"

if ! command -v rtk &>/dev/null; then # timeout: 5000
	printf "${YEL}⚠ SKIPPED${NC}: Check 10 — rtk not installed\n"
elif [ ! -f ".claude/hooks/rtk-rewrite.js" ]; then
	printf "${YEL}⚠ SKIPPED${NC}: Check 10 — .claude/hooks/rtk-rewrite.js not found\n"
else
	if ! command -v node &>/dev/null; then # timeout: 5000
		printf "${YEL}⚠ SKIPPED${NC}: Check 10 RTK parsing — node not in PATH\n"
	else
		RTK_HELP=$(rtk --help 2>&1) # timeout: 5000
		HOOK_PREFIXES=$(node -e "
    const fs = require('fs');
    const src = fs.readFileSync('.claude/hooks/rtk-rewrite.js', 'utf8');
    const m = src.match(/RTK_PREFIXES\s*=\s*\[([^\]]*)\]/s);
    if (!m) { process.exit(1); }
    const entries = m[1].match(/\"[^\"]+\"/g) || [];
    entries.forEach(e => console.log(e.replace(/'/g, '')));
  " 2>/dev/null) # timeout: 5000

		if [ -z "$HOOK_PREFIXES" ]; then
			printf "${YEL}⚠ SKIPPED${NC}: Check 10 — could not parse RTK_PREFIXES from hook file\n"
		else
			INVALID=0
			while IFS= read -r prefix; do
				[ -z "$prefix" ] && continue
				if ! echo "$RTK_HELP" | grep -qw "$prefix"; then
					printf "${RED}! INVALID hook prefix${NC}: '%s' — not a recognized RTK subcommand\n" "$prefix"
					INVALID=$((INVALID + 1))
				fi
			done <<<"$HOOK_PREFIXES"

			META_CMDS="gain discover proxy init version help"
			MISSING=0
			while IFS= read -r rtk_cmd; do
				[ -z "$rtk_cmd" ] && continue
				is_meta=0
				for meta in $META_CMDS; do
					[ "$rtk_cmd" = "$meta" ] && is_meta=1 && break
				done
				[ "$is_meta" -eq 1 ] && continue
				if ! echo "$HOOK_PREFIXES" | grep -qw "$rtk_cmd"; then
					printf "${YEL}⚠ MISSING hook prefix${NC}: '%s' — RTK supports filtering this command but hook does not list it\n" "$rtk_cmd"
					MISSING=$((MISSING + 1))
				fi
			done < <(echo "$RTK_HELP" | grep -oE '^\s{2,4}[a-z][a-z0-9_-]+' | tr -d ' ' | sort -u)

			if [ "$INVALID" -eq 0 ] && [ "$MISSING" -eq 0 ]; then
				printf "${GRN}✓ OK${NC}: Check 10 — RTK hook prefixes aligned with installed RTK version\n"
			fi
		fi
	fi
fi
```

Severity: invalid prefix entries = **high**; missing filterable commands = **medium**. **Report only** — never auto-fix.

## Check 11 — Memory health (MEMORY.md noise accumulation)

MEMORY.md has 200-line truncation limit. Three sub-checks:

**11a — Duplicate with CLAUDE.md**: Read both MEMORY.md and CLAUDE.md. For each MEMORY.md section, check if same rule or directive exists verbatim or near-verbatim in CLAUDE.md. Flag duplicates as **low**.

**11b — Stale version pins**:

```bash
MEMORY_FILE="$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g')/memory/MEMORY.md" # timeout: 3000
if [ -f "$MEMORY_FILE" ]; then
    grep -nE '(v[0-9]+\.[0-9]+\.[0-9]+|as of [A-Z][a-z]+ 20[0-9]{2})' "$MEMORY_FILE" || echo "no stale pins found" # timeout: 5000
else
    printf "${YEL}⚠ SKIPPED${NC}: Check 11b — MEMORY.md not found at derived path: %s\n" "$MEMORY_FILE"
fi
```

**11c — Absorbed feedback files**:

```bash
MEMORY_DIR="$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g')/memory" # timeout: 3000
if [ -d "$MEMORY_DIR" ]; then
    ls "$MEMORY_DIR"/feedback_*.md 2>/dev/null || echo "no feedback files" # timeout: 5000
else
    printf "${YEL}⚠ SKIPPED${NC}: Check 11c — memory dir not found: %s\n" "$MEMORY_DIR"
fi
```

All three sub-checks produce only **low** findings — auto-fixed under `/audit fix all`. Fix: remove duplicate section, drop version pin, delete absorbed feedback file.

## Check 30 — Config token overhead

Rules files in `.claude/rules/` load **entirely at session start**, regardless of relevance. Agents and skills are lazy-loaded (zero cost until invoked). This check measures always-loaded byte count and flags oversized components.

```bash
# timeout: 5000
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'
printf "${GRN}--- Check 30: Config token overhead ---${NC}
"

PROJECT_CLAUDE=$(wc -c < CLAUDE.md 2>/dev/null || echo 0)
RULES_TOTAL=$(find .claude/rules -name "*.md" -print0 2>/dev/null | xargs -0 cat 2>/dev/null | wc -c || echo 0)
GLOBAL_CLAUDE=$(cat ~/.claude/CLAUDE.md ~/.claude/*.md 2>/dev/null | wc -c || echo 0)
TOTAL=$((PROJECT_CLAUDE + RULES_TOTAL + GLOBAL_CLAUDE))

printf "  Project CLAUDE.md:  %d bytes
" "$PROJECT_CLAUDE"
printf "  Rules dir total:    %d bytes
" "$RULES_TOTAL"
printf "  Global ~/.claude/:  %d bytes
" "$GLOBAL_CLAUDE"
printf "  Total always-loaded: %d bytes (~%d tokens)
" "$TOTAL" "$((TOTAL / 4))"

# 30b — single oversized rules file
if [ -d .claude/rules ]; then
    find .claude/rules -name "*.md" | while read -r f; do
        sz=$(wc -c < "$f")
        if [ "$sz" -gt 10240 ]; then
            printf "${RED}! FAIL Check 30b — rules file %s is %d bytes (> 10 KB)
" "$f" "$sz"
        elif [ "$sz" -gt 5120 ]; then
            printf "${YEL}⚠ WARN Check 30b — rules file %s is %d bytes (> 5 KB)
" "$f" "$sz"
        fi
    done
fi

# 30a — total overhead
if [ "$TOTAL" -gt 102400 ]; then
    printf "${RED}! FAIL Check 30a — total always-loaded config %d bytes (> 100 KB)
" "$TOTAL"
elif [ "$TOTAL" -gt 51200 ]; then
    printf "${YEL}⚠ WARN Check 30a — total always-loaded config %d bytes (> 50 KB)
" "$TOTAL"
else
    printf "${GRN}✓ OK Check 30 — config overhead %d bytes (~%d tokens)
" "$TOTAL" "$((TOTAL / 4))"
fi
```

Severity: > 100 KB total or > 10 KB single file = **medium**; 50–100 KB total or 5–10 KB single file = **low**. **Report only** — fix = split or remove content from rules files; never auto-collapse.

Note: agents/ and skills/ are lazy-loaded — never flag them for token overhead.
