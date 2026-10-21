<!-- file: perm.md — consumers: SKILL.md (Step 4 dispatch) -->

### Mode: Add Permission

Adds rule to both `settings.json` and `permissions-guide.md` atomically.

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
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" .claude/settings.json '.permissions.allow += [$rule]' --arg rule "<rule>"
   ```

3. Also append to the plugin **source** `permissions-allow.json` so `/foundry:setup` syncs it to `~/.claude/settings.json` after reinstall. Source tree only: the installed cache copy is replaced on plugin update, and a project-only permission written there would reach every project through setup. Outside the foundry source repo the file is absent and the step is a no-op:

   ```bash
   # timeout: 5000
   PERM_FILE="plugins/cc_foundry/.claude-plugin/permissions-allow.json"
   if [ -f "$PERM_FILE" ]; then
       python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" "$PERM_FILE" '. += [$rule] | unique' --arg rule "<rule>"
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
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/verify_perm.py" "<rule>" .claude/settings.json .claude/permissions-guide.md present
   # Exits 0 if both consistent; prints "settings: OK|MISSING" + "guide: OK|MISSING"
   ```

### Mode: Remove Permission

Removes rule from both `settings.json` and `permissions-guide.md` atomically.

1. Update `settings.json` — atomic jq edit via shared helper:

   ```bash
   # timeout: 15000
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/jq_write.py" .claude/settings.json 'del(.permissions.allow[] | select(. == $rule))' --arg rule "<rule>"
   ```

2. Update `permissions-guide.md` — use Edit tool to remove table row containing `` `<rule>` ``.

3. Verify both files clean:

   ```bash
   # timeout: 5000
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/verify_perm.py" "<rule>" .claude/settings.json .claude/permissions-guide.md absent
   # Exits 0 if both consistent; prints "settings: OK|STILL_PRESENT" + "guide: OK|STILL_PRESENT"
   ```
