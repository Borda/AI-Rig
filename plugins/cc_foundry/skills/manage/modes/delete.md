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

Deregister the project hook from each existing file of `.claude/settings.json` and `.claude/settings.local.json` **before** deleting its file. Skip missing settings files; with neither file present, no project registration needs removal. Substitute `<name>` with the hook's basename (no `.js` suffix). Require successful deregistration and a zero remaining path-match count in every existing file; either failure stops deletion and leaves the hook file intact. This project operation does not edit the plugin's shipped `hooks.json`; same-basename plugin hooks are separate entities.

```bash
# timeout: 5000
HOOK_NAME="<name>"        # e.g. "rtk-rewrite" — no .js suffix
case "$HOOK_NAME" in *'<'*'>'*|"") echo "! BLOCKED — hook name not substituted"; exit 1;; esac
command -v jq >/dev/null || { echo "! BLOCKED — jq is required for hook verification"; exit 1; }
for HOOK_SETTINGS in .claude/settings.json .claude/settings.local.json; do
    [ -e "$HOOK_SETTINGS" ] || [ -L "$HOOK_SETTINGS" ] || continue
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/remove_hook_from_registry.py" \
        --json-file "$HOOK_SETTINGS" \
        --hook-name "$HOOK_NAME" \
        --match path || exit 1
    jq -e --arg hook "$HOOK_NAME" '[.hooks | .. | objects | select(.command? // "" | contains(".claude/hooks/" + $hook + ".js"))] | length | select(. == 0)' "$HOOK_SETTINGS" || exit 1  # timeout: 5000
done
# Expected output: 0
rm ".claude/hooks/$HOOK_NAME.js"  # timeout: 5000
```
