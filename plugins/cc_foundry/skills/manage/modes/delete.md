<!-- file: delete.md — consumers: SKILL.md (Step 4 dispatch) -->

### Mode: Delete Agent

```bash
rm .claude/agents/<name>.md # timeout: 5000
```

### Mode: Delete Skill

```bash
rm -r .claude/skills/<name>  # timeout: 5000
```

### Mode: Delete Rule

```bash
rm .claude/rules/<name>.md # timeout: 5000
```

### Mode: Delete Hook

```bash
rm .claude/hooks/<name>.js  # timeout: 5000
```

After deleting the hook file, also remove its entry from `.claude/settings.json` so Claude Code does not invoke a missing script. Identify the hook's matcher pattern (the entry's `matcher` field, or `command` substring containing the deleted filename) and run jq to strip every block referencing it. Substitute `<name>` with the deleted hook's basename (no `.js` suffix). The one block below also verifies the entry is gone (expected output `0`) and **updates the plugin `hooks.json` registry**: if the deleted hook was registered in the foundry plugin's own `hooks.json`, `/foundry:setup` would re-add it to `settings.json` on the next sync, resurrecting a broken reference, so the matching entry is stripped there too:

```bash
# timeout: 5000
HOOK_NAME="<name>"        # e.g. "rtk-rewrite" — no .js suffix
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/remove_hook_from_registry.py" \
    --json-file .claude/settings.json \
    --hook-name "$HOOK_NAME" \
    --match path
jq --arg hook "$HOOK_NAME" '[.. | objects | select(.command? // "" | test($hook + "\\.js"))] | length' .claude/settings.json  # timeout: 5000
# Expected output: 0
PLUGIN_HOOKS_JSON="plugins/cc_foundry/hooks/hooks.json"  # source tree only: installed cache is replaced on plugin update
if [ -f "$PLUGIN_HOOKS_JSON" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/remove_hook_from_registry.py" \
        --json-file "$PLUGIN_HOOKS_JSON" \
        --hook-name "$HOOK_NAME" \
        --match basename
    jq --arg hook "$HOOK_NAME" '[.. | objects | select(.command? // "" | test($hook + "\\.js"))] | length' "$PLUGIN_HOOKS_JSON"  # expected: 0
else
    printf "  (no plugin hooks.json at %s — skipping registry cleanup)\n" "$PLUGIN_HOOKS_JSON"
fi
```
