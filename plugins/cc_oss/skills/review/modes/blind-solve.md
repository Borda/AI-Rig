<!-- Consumer: plugins/cc_oss/skills/review/SKILL.md (Agent 0); fragment executed inline. -->

### Agent 0 — blind-solve (FEATURE/MIXED only, general-purpose)

Anti-anchoring pre-step: before any agent reads the diff, spawn a standalone agent that derives its own **blueprint-level** solution to the problem the PR solves — approach + key data structures + edge cases, not full implementation. Bounded: ~10 tool calls, output ≤1 page. This spawn is isolation-motivated, not work displacement — the blind agent must not share the orchestrator's context, so the "under ~73 calls → inline" rule does not apply; it still costs the fixed per-spawn overhead, which is why it is gated to FEATURE/MIXED.

Problem statement, in priority order: linked issue body (the original ask — outranks the PR's own framing when both exist) → PR title + body → changed file **names**. Gather it with this block, in the Step 2 launch turn **after** Step 2's run-dir allocation block (the sentinel it reads is written there); nothing here touches diff content:

```bash
# timeout: 15000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUM < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || PR_NUM=""
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$PR_NUM" ] && [ -n "$RUN_DIR" ] || { echo "! BLOCKED — PR tag or run-dir sentinel empty; run Step 2's allocation block first"; exit 1; }
BASE_REF=$(gh pr view "$PR_NUM" --json baseRefName --jq .baseRefName 2>/dev/null)
PR_BODY_TXT=$(gh pr view "$PR_NUM" --json body --jq .body 2>/dev/null)
# issue bodies fetched raw here — the Step 2 issue agent runs in the same batch as Agent 0, its issue-<N>.md files do not exist yet
ISSUE_REFS=$(printf '%s' "$PR_BODY_TXT" | grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?) #[0-9]+' | grep -oE '[0-9]+' | sort -u | head -3)
{
  for n in $ISSUE_REFS; do echo "## Linked issue #$n"; gh issue view "$n" --json title,body --jq '"\(.title)\n\n\(.body)"' 2>/dev/null; echo; done
  echo "## PR"; gh pr view "$PR_NUM" --json title,body --jq '"\(.title)\n\n\(.body)"' 2>/dev/null
  echo; echo "## Changed files (names only)"; gh pr diff "$PR_NUM" --name-only 2>/dev/null
  echo; echo "BASE_REF=$BASE_REF"
} > "$RUN_DIR/blind-solve-input.md"
echo "blind-solve input: $RUN_DIR/blind-solve-input.md base=$BASE_REF"
```

Spawn prompt: the contents of `$RUN_DIR/blind-solve-input.md` verbatim, then: "Your only source for pre-change code is `git show origin/<BASE_REF>:<path>` for files listed above. Never use Read, `cat`, `gh pr diff`, `git diff`, or any working-tree path — the working tree may already contain the change. Sketch your own solution to the stated problem — approach, key data structures/functions, edge cases — in ≤1 page. Do not write full code." Write to `$RUN_DIR/foundry--blind-solve.md`, return `{"status":"done","file":"$RUN_DIR/foundry--blind-solve.md","confidence":0.N}`. `BASE_REF` is the PR's own `baseRefName`, not the local default branch. Launch in the same batch as the Step 2 agents.
