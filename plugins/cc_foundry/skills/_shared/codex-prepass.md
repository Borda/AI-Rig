When the caller selects Codex pre-pass, check bridge availability and the diff:

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
- changes are formatting/comments/whitespace only and the pre-pass is optional — never skip on line count: a 1-line logic change on the main path is as risky as a large one. When selected as the required independent implementation challenge, the formatting-only skip does not apply.

**Run** when selected as the required challenge, or when an optional pre-pass covers new logic, functions, conditionals, error paths, or restructured code (requires `bridge@borda-ai-rig`):

```text
Skill(skill="bridge:review", args="Read-only adversarial review of the current working-tree changes. Identify bugs, missed edge cases, and inconsistencies; do not apply fixes.")
```

**Oversized diff** (optional; needs a bridge whose review accepts `--partition-file`): a diff too large for one 230 s call (`git diff HEAD --stat` lists over ~40 files or ~600 changed lines) is partitioned instead of sent whole. Write `{"task": "<the brief above>", "against": "HEAD"}` (the working-tree diff; a committed target uses `"range": "<A>..<B>"`) to a file inside the workspace (under the run dir), call `Skill(skill="bridge:review", args="--partition-file <path> --wave 1 --effort medium --timeout-seconds 230")`, then each later wave up to the envelope's `waves`, one at a time, adding `--expect-digest <wave 1 partition_digest>`. Run at most 3 waves in all, a restart's included, and report the groups left as unreviewed scope; apply no fix until the last wave returned, because a changed diff refuses every later wave, and a digest refusal restarts at wave 1 once before the rest is reported unreviewed. Keep each group's findings with its group number. The pre-pass is complete only when every wave's groups completed; `skipped` paths and any failed group are unreviewed scope, never covered. A bridge that rejects the flag → send the single call above; any other `error` status is reported, never retried as the single call.

**Fallback**: status `absent` or `disabled` → skip bridge dispatch. If an independent challenge is required, use another permitted independent route; if unavailable, report missing coverage and do not claim it passed.

Validate Codex findings and apply feasible authorized fixes. Seed cycle 1 with those findings only when the caller explicitly invokes an iterative loop; a clean or skipped pre-pass does not itself start one.
