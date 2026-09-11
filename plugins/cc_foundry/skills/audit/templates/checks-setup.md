# Setup Checks — 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11

## Check 1 — Inventory drift (MEMORY.md vs disk)

Use Glob (`agents/*.md`, path `.claude/`) to list agent files; extract basenames, sort, write to `/tmp/agents_disk.txt` via Bash:

```bash
ls .claude/agents/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.md$//' | sort >/tmp/agents_disk.txt || true # timeout: 5000
```

Read `- Agents:` and `- Skills:` roster lines from MEMORY.md content injected in conversation context (auto-memory at session start). Don't Grep file path — MEMORY.md not under `.claude/` but in Claude Code's auto-memory system. Repeat with Glob (`skills/*/`, path `.claude/`) for skills on disk — write to `/tmp/skills_disk.txt`.

**macOS caution**: BSD grep treats args starting with `-` as option flags. When building bash comparison from MEMORY.md roster via grep, use `grep -E 'Agents:'` (no leading `- `) or `grep -- '- Agents:'` not `grep '- Agents:'` — latter exits 2 on macOS, silently produces empty result. Safest: use Read tool (not grep) for MEMORY.md.

## Check 2 — README vs disk

Use Grep tool (pattern `^\| \*\*`, file `README.md`, output mode `content`) to extract agent/skill table rows.

## Check 3 — settings.json permissions

Use Grep tool (pattern `gh |python -m|ruff|mypy|pytest`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to collect bash commands used in skills.

## Check 4 — permissions-guide.md drift

Every allow entry must appear in guide, and vice versa.

```bash
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then  # timeout: 5000
    printf "⚠ SKIPPED: Check 4 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
    printf "⚠ SKIPPED: Check 4 — .claude/settings.json not found\n"
elif [ ! -f ".claude/permissions-guide.md" ]; then
    printf "⚠ SKIPPED: Check 4 — .claude/permissions-guide.md not found\n"
else
    jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | \  # timeout: 5000
    while IFS= read -r perm; do
        grep -qF "\`$perm\`" .claude/permissions-guide.md 2>/dev/null \
            || printf "⚠ MISSING from guide: %s\n" "$perm"
    done

    grep '^| `' .claude/permissions-guide.md 2>/dev/null | awk -F'`' '{print $2}' | \  # timeout: 5000
    while IFS= read -r perm; do
        jq -e --arg p "$perm" '(.permissions.allow // []) + (.permissions.deny // []) | contains([$p])' .claude/settings.json > /dev/null 2>&1 \  # timeout: 5000
        || printf "⚠ ORPHANED in guide: %s\n" "$perm"
    done
fi
```

## Check 5 — Permission safety audit

Every `allow` entry must be non-destructive, reversible, local-only.

Read `.claude/settings.json` with Read tool, extract `permissions.allow` list. For each entry, use model reasoning to evaluate against three criteria:

- **Non-destructive**: no permanent delete/overwrite (no `rm -rf`, `git push --force`, `DROP TABLE`)
- **Reversible**: effect undoable without data loss (local file edits, test runs, read-only queries)
- **Local-only**: no effect outside working directory, no external data transmission

Flag destructive patterns as **critical** (auto-approved destructive commands always = breaking safety failure). Flag external-state mutations as **high**, raise to user — some (e.g., `gh release create`) may be intentional but must be explicitly acknowledged.

## Check 6 — Stale settings.json allow entries

```bash
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then # timeout: 5000
    printf "⚠ SKIPPED: Check 6 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
    printf "⚠ SKIPPED: Check 6 — .claude/settings.json not found\n"
else
    printf "=== Check 6: Stale allow entries ===\n"
    jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | while IFS= read -r entry; do # timeout: 5000
        cmd=$(echo "$entry" | sed 's/^[A-Za-z]*(\(.*\))$/\1/' | sed 's/^"\(.*\)"$/\1/')
        hits=$(grep -rl "$cmd" .claude/agents/ .claude/skills/ .claude/rules/ .claude/hooks/ .claude/CLAUDE.md 2>/dev/null | wc -l | tr -d ' ') # timeout: 5000
        if [ "$hits" -eq 0 ]; then
            printf "⚠ STALE allow: %s — no usage found in .claude/ files\n" "$entry"
        fi
    done
    printf "✓: Check 6 scan complete\n"
fi
```

**Severity**: **low** per stale entry. Fix: remove stale entry from `settings.json` (report only — `settings.json` never auto-edited per audit policy).

**Important**: some allow entries intentionally grant broad patterns (e.g., `Bash(mkdir -p .reports/audit/*)`) not appearing verbatim in config files — exercised at runtime. Flag only entries whose command fragment appears nowhere in any `.claude/` file.

## Check 7 — bridge-to-Codex plugin integration check

Skip when `bridge@borda-ai-rig` is absent.

```bash
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent") # timeout: 5000
if [ "$CODEX_STATUS" = "absent" ]; then
    printf "⚠ SKIPPED: Check 7 — bridge@borda-ai-rig not installed\n"
elif [ "$CODEX_STATUS" = "disabled" ]; then
    printf "⚠ WARN: Check 7 — bridge@borda-ai-rig installed but DISABLED\n"
    printf "  Fix: run \`claude plugin enable bridge@borda-ai-rig\` then \`/reload-plugins\`\n"
else
    printf "✓ OK: Check 7 — bridge@borda-ai-rig present and enabled\n"
fi
```

- Plugin installed but **disabled** → **medium** (fix: `claude plugin enable bridge@borda-ai-rig` + `/reload-plugins`)
- Plugin present but dispatches fail → **high** (verify with `/calibrate skills`)

## Check 8 — foundry plugin correctness

Verify repo's `foundry` plugin structure at `plugins/cc_foundry/`. Skip if not found.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_plugin_layout.py" --plugin-dir plugins/cc_foundry --expect-name foundry  # timeout: 60000
```

**Severity**: manifest missing/invalid JSON → **critical**; broken symlink, hooks.json invalid, hooks.json references missing file, or `claude plugin validate` fails → **high**; .js plugin file is symlink (not real file) → **medium**; 8f permissions-allow.json entries missing from settings.json → **medium**; settings.json entries missing from permissions-allow.json → **low**; setup-foundry SKILL.md missing → **high**; missing required keyword coverage → **medium**. **Report only** — never auto-fix.

## Check 9 — Agent color drift (statusline COLOR_MAP vs frontmatter)

```bash
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    color=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^color:/{sub(/^color: */,""); print}' "$f")
    [ -n "$color" ] && printf "%s: %s\n" "$name" "$color"
done
```

Use model reasoning to cross-reference each extracted color name against `COLOR_MAP` keys in `.claude/hooks/statusline.js`. Flag:

- Color in agent frontmatter but **not a key in `COLOR_MAP`** → **medium** (agent appears uncolored)
- Color in `COLOR_MAP` not declared by any agent → **low** (dead mapping, no functional impact)

## Check 10 — RTK hook alignment

Verify prefix list in `.claude/hooks/rtk-rewrite.js` (`RTK_PREFIXES` array) consistent with commands installed RTK binary supports.

Skip if RTK not installed (`rtk --version` fails) or `.claude/hooks/rtk-rewrite.js` not found.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_rtk_alignment.py"  # timeout: 30000
```

Severity: invalid prefix entries = **high**; missing filterable commands = **medium**. **Report only** — never auto-fix.

## Check 11 — Memory health (MEMORY.md noise accumulation)

MEMORY.md has 200-line truncation limit. Three sub-checks:

**Check 11a — Duplicate with CLAUDE.md**: Read both MEMORY.md and CLAUDE.md. For each MEMORY.md section, check if same rule or directive exists verbatim or near-verbatim in CLAUDE.md. Flag duplicates **low**.

**11b — Stale version pins**:

```bash
MEMORY_FILE="$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g')/memory/MEMORY.md" # timeout: 3000
if [ -f "$MEMORY_FILE" ]; then
    grep -nE '(v[0-9]+\.[0-9]+\.[0-9]+|as of [A-Z][a-z]+ 20[0-9]{2})' "$MEMORY_FILE" || echo "no stale pins found" # timeout: 5000
else
    printf "⚠ SKIPPED: Check 11b — MEMORY.md not found at derived path: %s\n" "$MEMORY_FILE"
fi
```

**11c — Absorbed feedback files**:

```bash
MEMORY_DIR="$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g')/memory" # timeout: 3000
if [ -d "$MEMORY_DIR" ]; then
    ls "$MEMORY_DIR"/feedback_*.md 2>/dev/null || echo "no feedback files" # timeout: 5000
else
    printf "⚠ SKIPPED: Check 11c — memory dir not found: %s\n" "$MEMORY_DIR"
fi
```

All three sub-checks produce only **low** findings — auto-fixed when user picks "Fix all" from follow-up gate. Fix: remove duplicate section, drop version pin, delete absorbed feedback file.

## Check 34 — Config token overhead

Rules files in `.claude/rules/` load **entirely at session start**, regardless of relevance. Agents and skills lazy-loaded (zero cost until invoked). Measures always-loaded byte count, flags oversized components.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/measure_config_size.py" --mode overhead  # timeout: 10000
```

Severity: > 100 KB total or > 10 KB single file = **medium**; 50–100 KB total or 5–10 KB single file = **low**. **Report only** — fix = split or remove content from rules files; never auto-collapse.

Note: `agents/` and `skills/` lazy-loaded — never flag for token overhead.

Note: the thresholds were calibrated against a total that counted the global `CLAUDE.md` twice. The total is now lower for the same tree, so a threshold can only fire later, never sooner — recalibrate downward if 50 KB stops discriminating.

## Check 39 — Plugin version freeze

`plugins/CLAUDE.md` versioning policy requires bumping `plugin.json` version in every commit that modifies plugin files. A frozen version misrepresents what changed and defeats changelog reconstruction.

Skip if `LOCAL_MODE != true` (no git history accessible).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 39: Plugin version freeze ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check 39 skipped in non-local mode\n"
else
    for plugin_dir in plugins/*/; do
        plugin_name=$(basename "$plugin_dir")
        plugin_json="$plugin_dir.claude-plugin/plugin.json"
        [ -f "$plugin_json" ] || continue
        disk_ver=$(python -c "import json; print(json.load(open('$plugin_json'))['version'])" 2>/dev/null)
        head_ver=$(git show HEAD:"$plugin_json" 2>/dev/null | python -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null)
        [ -z "$head_ver" ] && continue  # new plugin, no HEAD yet
        if [ "$disk_ver" = "$head_ver" ]; then
            changed=$(git diff --name-only HEAD -- "$plugin_dir" 2>/dev/null | wc -l | tr -d ' ')
            if [ "$changed" -gt 0 ]; then
                printf "C39-MEDIUM: plugin '%s' has %s modified file(s) but version unchanged (%s)\n" \
                    "$plugin_name" "$changed" "$disk_ver"
            fi
        fi
    done  # timeout: 10000
fi
```

**Severity**: medium — commit mislabeling; no runtime breakage, but release notes and changelog reconstruction are unreliable. Fix: bump `plugin.json` patch or minor version per `plugins/CLAUDE.md` versioning policy before committing.
