Before cycle 1 of review loop, run Codex pre-pass if diff meaningful:

```bash
# canonical check — target selector must be installed and enabled. Installed: own plugin's check_bridge.py.
# Dev tree (CLAUDE_PLUGIN_ROOT unset): every consumer ships a byte-identical manifested copy, so the first one
# found is the right one — no sibling plugin named, no variable borrowed from another fence
_CB="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/bin/check_bridge.py}"
[ -n "$_CB" ] || _CB=$(ls plugins/cc_*/bin/check_bridge.py 2>/dev/null | head -1)
# -f guard: an empty path would make python run a __main__.py in the cwd instead of failing
[ -f "$_CB" ] && CODEX_STATUS=$(python "$_CB" --status 2>/dev/null) || CODEX_STATUS="absent"
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
