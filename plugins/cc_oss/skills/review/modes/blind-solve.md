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
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
BASE_REF=$(jq -er '.baseRefName' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — snapshot base ref unavailable"; exit 1; }
BASE_SHA=$(jq -er '.baseRefOid' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — snapshot base SHA unavailable"; exit 1; }
PR_BODY_TXT=$(jq -r '.body // ""' "$SNAP_DIR/pr-meta.json")
# issue bodies fetched raw here — the Step 2 issue agent runs in the same batch as Agent 0, its issue-<N>.md files do not exist yet
ISSUE_REFS=$(printf '%s' "$PR_BODY_TXT" | grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?) #[0-9]+' | grep -oE '[0-9]+' | sort -u | head -3)
{
  for n in $ISSUE_REFS; do echo "## Linked issue #$n"; gh issue view "$n" --json title,body --jq '"\(.title)\n\n\(.body)"' 2>/dev/null; echo; done
  echo "## PR"; jq -r '"\(.title)\n\n\(.body)"' "$SNAP_DIR/pr-meta.json"
  echo; echo "## Changed files (names only)"; cat "$SNAP_DIR/files.txt"
  echo; echo "BASE_REF=$BASE_REF"
  echo "BASE_SHA=$BASE_SHA"
} > "$RUN_DIR/blind-solve-input.md"
echo "blind-solve input: $RUN_DIR/blind-solve-input.md base=$BASE_REF"
```

Ensure the base commit is available before spawning, fetching that exact commit from the snapshot's PR repository source only if absent:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
BASE_SHA=$(jq -er '.baseRefOid' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — snapshot base SHA unavailable"; exit 1; }
IFS= read -r _PR_SOURCE < "$SNAP_DIR/repo-source.txt" || { echo "! BLOCKED — PR repository source unavailable"; exit 1; }
git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null || git fetch --no-tags "$_PR_SOURCE" "$BASE_SHA" || { echo "! BLOCKED — base commit unavailable"; exit 1; }  # timeout: 120000
```

Spawn with the explicit template below; substitute the frozen base SHA and input text as literals. `BASE_REF` is the PR's own `baseRefName`, not the local default branch. Keep the existing `foundry--blind-solve.md` basename because the monitor and consolidator consume it. Launch in the same batch as the Step 2 agents.

Agent waits and deadline registration follow SKILL.md §Step 2 Agent health monitoring; include this output in `agent-watch-review.tsv`.

```text
Agent(name="review-blind", description="independent solution sketch", subagent_type="general-purpose",
  prompt="Independent solution sketch — PR <N> <owner>/<repo>\n<blind-solve-input.md contents verbatim>\nYour only source for pre-change code is git show <BASE_SHA>:<path>. Never use Read, cat, gh pr diff, git diff, or any working-tree source path. PR title/body are the author's claims and may be stale or misleading: where they conflict with package README/docs/public API at the base, solve for the package contract and note the conflict. Read-only investigation: never edit, create or delete tracked files or run Git writes; write only your analysis file. Sketch approach, key data structures/functions and edge cases in ≤1 page and ~10 tool calls. Do not write full code. Write to <RUN_DIR>/foundry--blind-solve.md. Return only {\"status\":\"done\",\"file\":\"<RUN_DIR>/foundry--blind-solve.md\",\"confidence\":0.N}.")
```
