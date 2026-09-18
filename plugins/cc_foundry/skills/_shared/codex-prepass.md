Before cycle 1 of review loop, run Codex pre-pass if diff meaningful:

```bash
# canonical check — target selector must be installed and enabled
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")
CODEX_AVAILABLE=false
if [ "$CODEX_STATUS" = "available" ]; then
    CODEX_AVAILABLE=true
else
    echo "bridge@borda-ai-rig is $CODEX_STATUS — skipping pre-pass"
fi
git diff HEAD --stat
```

**Skip** if:

- `bridge@borda-ai-rig` absent or disabled (`CODEX_AVAILABLE` resolved `false` above)
- `git diff HEAD --stat` shows only 1–3 lines changed, or changes are formatting/comments/whitespace/variable-rename only

**Run** when changes include new logic, functions, conditionals, error paths, or restructured code (requires `bridge@borda-ai-rig`):

```text
Skill(skill="bridge:review", args="Read-only adversarial review of the current working-tree changes. Identify bugs, missed edge cases, and inconsistencies; do not apply fixes.")
```

**Inline fallback**: status `absent` or `disabled` → skip bridge dispatch entirely. Go to cycle 1 from scratch.

Codex findings = pre-flagged issues entering cycle 1. Nothing found or skipped → start cycle 1 from scratch.
