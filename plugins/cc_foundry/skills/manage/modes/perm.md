<!-- file: perm.md — consumers: SKILL.md (Step 4 dispatch) -->

### Permission validation (add/remove)

Run before either mode below. Substitute the exact rule with shell-safe quoting in every block; never execute an unsubstituted placeholder. `jq` is required. Exit 0 from the lookup means present, exit 1 means absent; a missing or null allow list means absent. Missing jq, malformed settings, a present non-array allow list, or any other exit code is a validation error.

```bash
# timeout: 5000
PERM_RULE="<rule>"
case "$PERM_RULE" in *'<'*'>'*|"") echo "! BLOCKED — <rule> not substituted"; exit 1;; esac
command -v jq >/dev/null || { echo "! BLOCKED — jq is required for permission validation"; exit 1; }
PERM_STATUS=0
jq -e --arg rule "$PERM_RULE" '.permissions.allow as $a | if $a == null then false elif ($a | type) != "array" then error("allow must be an array") else $a | index($rule) != null end' .claude/settings.json >/dev/null || PERM_STATUS=$?
case "$PERM_STATUS" in
    0) echo present ;;
    1) echo absent ;;
    *) echo "! BLOCKED — permission lookup failed"; exit 1 ;;
esac
```

For add: require `absent`, description and use case; for remove: require `present`. Otherwise report validation failure and stop before any edit.

### Mode: Add Permission

Adds rule to both `settings.json` and `permissions-guide.md` atomically.

When `plugins/cc_foundry/.claude-plugin/permissions-allow.json` is absent, skip the question and use `SHIP_PERMISSION=false`; ask only when it exists. Before any write in that source-tree case, invoke `AskUserQuestion`: "Where should this permission apply?" — (a) **Project-only** [default] · (b) **Ship with foundry** (also edits the source allow list and reaches every user after update/reinstall plus setup). Set `SHIP_PERMISSION=true` only after explicit choice (b); otherwise keep it false. Never edit an installed cache. Substitute the chosen boolean into the separate Step 3 block because shell state does not persist between calls.

1. Determine guide category from rule prefix:

   - `WebSearch` → `## Web`
   - `WebFetch(domain:...)` → `## WebFetch — allowed domains`
   - `WebFetch` (bare, no domain) → `## WebFetch — allowed domains`
   - `Agent(subagent_type:*)` → `## Shell utilities` (agent dispatch rules)
   - `mcp__*` → `## Shell utilities` (MCP tool rules)
   - `Bash(gh ...)` → `## GitHub CLI — read-only`
   - `Bash(git log:*)`, `Bash(git show:*)`, `Bash(git diff:*)`, `Bash(git rev-*:*)`, `Bash(git ls-*:*)`, `Bash(git -C:*)`, `Bash(git branch:*)`, `Bash(git tag:*)`, `Bash(git status:*)`, `Bash(git describe:*)`, `Bash(git shortlog:*)` → `## Git — read-only`
   - `Bash(git add:*)`, `Bash(git checkout:*)`, `Bash(git stash:*)`, `Bash(git restore:*)`, `Bash(git clean:*)`, `Bash(git apply:*)` → `## Git — local write`
   - `Bash(pytest:*)`, `Bash(python ...)`, `Bash(ruff:*)`, `Bash(mypy:*)`, `Bash(pip ...)` → `## Python toolchain`
   - `Bash(brew ...)`, `Bash(codex:*)` → `## macOS / ecosystem`
   - All other `Bash(...)` → `## Shell utilities`

2. Update `settings.json` — atomic jq edit via shared helper:

   ```bash
   # timeout: 15000
   PERM_RULE="<rule>"
   case "$PERM_RULE" in *'<'*'>'*|"") echo "! BLOCKED — <rule> not substituted"; exit 1;; esac
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" .claude/settings.json '.permissions.allow += [$rule]' --arg rule "$PERM_RULE" || exit 1
   ```

3. Only for **Ship with foundry**, also append to the plugin **source** `permissions-allow.json` so `/foundry:setup` syncs it to `~/.claude/settings.json` after reinstall. Project-only skips this write even when the source file exists:

   ```bash
   # timeout: 5000
   PERM_RULE="<rule>"
   case "$PERM_RULE" in *'<'*'>'*|"") echo "! BLOCKED — <rule> not substituted"; exit 1;; esac
   SHIP_PERMISSION="<true-or-false>"
   case "$SHIP_PERMISSION" in true|false) ;; *) echo "! BLOCKED — permission scope not substituted"; exit 1;; esac
   PERM_FILE="plugins/cc_foundry/.claude-plugin/permissions-allow.json"
   if [ "$SHIP_PERMISSION" = true ]; then
       [ -f "$PERM_FILE" ] || { echo "! BLOCKED — permission source file missing"; exit 1; }
       python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" "$PERM_FILE" '. += [$rule] | unique' --arg rule "$PERM_RULE" || exit 1
   fi
   ```

4. Update `permissions-guide.md` — append new row to end of correct section (before its trailing `---` separator). New row format:

   ```markdown
   | `<rule>` | <description> | <use case> |
   ```

   Use Edit tool to insert row: find last table row in target section and insert after it.

5. Verify both files updated:

   ```bash
   # timeout: 5000
   PERM_RULE="<rule>"
   case "$PERM_RULE" in *'<'*'>'*|"") echo "! BLOCKED — <rule> not substituted"; exit 1;; esac
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/verify_perm.py" "$PERM_RULE" .claude/settings.json .claude/permissions-guide.md present || exit 1
   # Exits 0 if both consistent; prints "settings: OK|MISSING" + "guide: OK|MISSING"
   ```

### Mode: Remove Permission

Removes rule from both `settings.json` and `permissions-guide.md` atomically.

1. Update `settings.json` — atomic jq edit via shared helper:

   ```bash
   # timeout: 15000
   PERM_RULE="<rule>"
   case "$PERM_RULE" in *'<'*'>'*|"") echo "! BLOCKED — <rule> not substituted"; exit 1;; esac
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" .claude/settings.json 'del(.permissions.allow[] | select(. == $rule))' --arg rule "$PERM_RULE" || exit 1
   ```

2. Update `permissions-guide.md` — use Edit tool to remove table row containing `` `<rule>` ``.

3. Verify both files clean:

   ```bash
   # timeout: 5000
   PERM_RULE="<rule>"
   case "$PERM_RULE" in *'<'*'>'*|"") echo "! BLOCKED — <rule> not substituted"; exit 1;; esac
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/verify_perm.py" "$PERM_RULE" .claude/settings.json .claude/permissions-guide.md absent || exit 1
   # Exits 0 if both consistent; prints "settings: OK|STILL_PRESENT" + "guide: OK|STILL_PRESENT"
   ```
