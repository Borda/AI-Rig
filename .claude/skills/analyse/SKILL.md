---
name: analyse
description: Analyze GitHub issues, Pull Requests (PRs), Discussions, and repo health for an Open Source Software (OSS) project. For any specific item, casts a wide net — finds and lists all related open and closed issues/PRs/discussions, explicitly flags duplicates. Also summarizes long threads, assesses PR readiness, extracts reproduction steps, and generates repo health stats. Uses gh Command Line Interface (CLI) for GitHub Application Programming Interface (API) access. Complements oss-shepherd agent.
argument-hint: <N|health|ecosystem> [--reply]
allowed-tools: Read, Bash, Write, Agent
context: fork
model: opus
effort: high
---

<objective>

Analyze GitHub threads and repo health to help maintainers triage, respond, and decide
quickly. Produces actionable, structured output — not just summaries.

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - `N` (a number) — any GitHub thread: issue, PR, or discussion; auto-detects the type
  - `health` — repo issue/PR/discussion health overview with duplicate detection
  - `ecosystem` — downstream consumer impact analysis for library maintainers
  - `--reply` — only valid with `N`; spawns oss-shepherd to draft a contributor-facing
    reply after the thread analysis. Silently ignored for `health` and `ecosystem`.

</inputs>

<workflow>

## Step 1: Flag parsing

If `$ARGUMENTS` contains `--reply`, strip it and set `REPLY_MODE=true`. `REPLY_MODE` is only
meaningful when `$ARGUMENTS` is a number — silently ignored for `health` and `ecosystem`.

## Step 2: Cache layer (numeric arguments only)

Check for a local cache file before making API calls — prevents redundant fetches and avoids
GitHub rate limits when re-analysing the same item in the same day.

```bash
CACHE_DIR="_analyse/cache-gh"
TODAY=$(date +%Y-%m-%d)
CACHE_FILE="$CACHE_DIR/$ARGUMENTS-$TODAY.json"
mkdir -p "$CACHE_DIR"
```

**Cache hit** — if `$CACHE_FILE` exists:

- Read `type`, `item`, and `comments` fields from the JSON
- Skip all primary `gh` item fetches in `modes/thread.md`
- Print `[cache] #$ARGUMENTS ($TODAY)` as a one-line status note
- Still run wide-net searches (dynamic — never cached)

**Cache miss** — after fetching in `modes/thread.md`, write:

```bash
jq -n \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg type "$TYPE" \
  --argjson number "$ARGUMENTS" \
  --argjson item "$ITEM" \
  --arg comments "$COMMENTS" \
  '{"ts":$ts,"type":$type,"number":$number,"item":$item,"comments":$comments}' \
  > "$CACHE_FILE"
```

**Stale cache** — a file for the same number but an earlier date is ignored. Old files are
left in place — they are small and provide audit history. Prune files older than 30 days:
`find _analyse/cache-gh -mtime +30 -delete`

# \_analyse/ dir TTL: clean with `find _analyse/cache-gh -mtime +30 -delete` (run manually — not in SessionEnd hook)

Cache applies to: issue/PR/discussion primary fetch and comments.
Cache does NOT apply to: `gh issue list`, `gh pr list`, `gh pr checks`, `gh pr diff`,
discussion list queries, health/ecosystem modes.

## Step 3: Auto-Detection (numeric arguments only)

Issues, PRs, and discussions share a unified running index — a given number is exactly one
type. If cache hit: read `TYPE` and `ITEM` from `$CACHE_FILE` — skip the `gh` calls below.

If cache miss:

```bash
# Step 1: try the issues API (covers both issues and PRs)
ITEM=$(gh api "repos/{owner}/{repo}/issues/$ARGUMENTS" 2>/dev/null)

if [ -n "$ITEM" ]; then
  TYPE=$(echo "$ITEM" | jq -r 'if .pull_request then "pr" else "issue" end')
else
  # Step 2: try discussions via GraphQL
  DISC=$(gh api graphql -f query='
    query($owner:String!,$repo:String!,$number:Int!){
      repository(owner:$owner,name:$repo){
        discussion(number:$number){ title }
      }
    }' -f owner='{owner}' -f repo='{repo}' -F number=$ARGUMENTS \
    --jq '.data.repository.discussion.title' 2>/dev/null)
  [ -n "$DISC" ] && TYPE="discussion" || TYPE="unknown"
fi
# unknown → print "Item #N not found" and stop
```

## Step 4: Mode dispatch

Read `.claude/skills/analyse/modes/<mode>.md` and execute all steps defined there.

| Argument          | Mode file            |
| ----------------- | -------------------- |
| number (any type) | `modes/thread.md`    |
| `health`          | `modes/health.md`    |
| `ecosystem`       | `modes/ecosystem.md` |

## Step 5: Draft contributor reply (--reply only, thread mode only)

If `REPLY_MODE` is not set, skip this step entirely.

**Reuse vs recreate**: reuse an existing report only if it exists *and* the item hasn't had
new activity since it was written.

```bash
TODAY=$(date +%Y-%m-%d)
mkdir -p _analyse/thread
REPORT_FILE="_analyse/thread/output-analyse-thread-$ARGUMENTS-$TODAY.md"

DRIFT=false
if [ -f "$REPORT_FILE" ]; then
  REPORT_MTIME=$(stat -f %m "$REPORT_FILE" 2>/dev/null || stat -c %Y "$REPORT_FILE")
  UPDATED_AT=$(gh api "repos/{owner}/{repo}/issues/$ARGUMENTS" --jq '.updated_at' 2>/dev/null)
  UPDATED_TS=$(date -d "$UPDATED_AT" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED_AT" +%s 2>/dev/null)
  [ "$UPDATED_TS" -gt "$REPORT_MTIME" ] && DRIFT=true
fi
```

Decision:

- Report exists **and** `DRIFT=false` → reuse it; go straight to the oss-shepherd spawn.
- Report missing **or** `DRIFT=true` → run full thread analysis first (Step 4), then continue.
  Note `[analysis refreshed — new activity since last report]` in the terminal summary.

**Spawn oss-shepherd** with the report path, the item number, and this prompt (note: oss-shepherd runs in a forked context — all required context must be self-contained in the prompt):

"Read the report at `<path>` for context. If the item is an issue or discussion, also fetch
the full thread (`gh issue view <number> --comments` or equivalent GraphQL for discussions)
and read every comment. Apply voice and formatting rules from oss-shepherd's `<voice>`
block — do not embed them inline here. Write your full output to
`_analyse/thread/output-reply-thread-<number>-$(date +%Y-%m-%d).md` using the Write tool. Return ONLY
a compact JSON envelope on your final line — nothing else after it:
`{\"status\":\"done\",\"file\":\"_analyse/thread/output-reply-thread-<number>-<date>.md\",\"sentences\":N,\"resolved\":\"yes|no|partial\",\"confidence\":0.N,\"summary\":\"Reply: N sentences, resolved: yes|no|partial\"}`"

Print compact terminal summary:

```
  Reply — N sentences  |  resolved: yes|no|partial
  [analysis refreshed — new activity since last report]  ← only if drift detected

  Reply:  _analyse/thread/output-reply-thread-<number>-<date>.md
```

End your response with a `## Confidence` block per CLAUDE.md output standards — this is
always the **absolute last thing**. If `REPLY_MODE=true`, place this block **after**
completing the reply step above, never after the analysis alone.

</workflow>

<notes>

- Mode files live in `.claude/skills/analyse/modes/` — one file per mode, fully self-contained
- `modes/thread.md` handles all three thread types (issue, PR, discussion) via internal branching
- Always use `gh` CLI — never hardcode repo URLs
- Run `gh auth status` first if commands fail; user may need to authenticate
- For closed items, note the resolution so history is useful
- Don't post responses without explicit user instruction — only draft them
- **Forked context**: this skill runs with `context: fork` — it operates without access to
  the current conversation history. All required context must be provided as the skill
  argument or in your prompt.
- Follow-up chains:
  - Issue with confirmed bug → `/develop fix` to diagnose, reproduce with test, and apply targeted fix
  - Issue is a feature request → `/develop feature` for TDD-first implementation
  - PR with quality concerns → `/review` for comprehensive multi-agent code review
  - Draft responses → use `--reply` to auto-draft via oss-shepherd; or invoke oss-shepherd manually

</notes>
