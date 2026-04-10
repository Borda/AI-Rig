---
name: analyse
description: Analyze GitHub issues, Pull Requests (PRs), Discussions, and repo health for an Open Source Software (OSS) project. For any specific item, casts a wide net — finds and lists all related open and closed issues/PRs/discussions, explicitly flags duplicates. Also summarizes long threads, assesses PR readiness, extracts reproduction steps, and generates repo health stats. Uses gh Command Line Interface (CLI) for GitHub Application Programming Interface (API) access. Complements oss-shepherd agent.
argument-hint: <N|health|ecosystem|path/to/report.md> [--reply]
allowed-tools: Read, Bash, Write, Agent
context: fork
model: opus
effort: high
---

<objective>

Analyze GitHub threads and repo health to help maintainers triage, respond, and decide quickly. Produces actionable, structured output — not just summaries.

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - `N` (a number) — any GitHub thread: issue, PR, or discussion; auto-detects the type
  - `health` — repo issue/PR/discussion health overview with duplicate detection
  - `ecosystem` — downstream consumer impact analysis for library maintainers
  - `--reply` — only valid with `N`; spawns oss-shepherd to draft a contributor-facing reply after the thread analysis. Silently ignored for `health` and `ecosystem`.
  - `path/to/report.md` — path to an existing report file; only valid combined with `--reply`; skips all analysis and spawns oss-shepherd directly using the provided file

</inputs>

<constants>
<!-- Background agent health monitoring (CLAUDE.md §8) — applies to Step 7 oss-shepherd spawn -->
MONITOR_INTERVAL=300   # 5 minutes between polls
HARD_CUTOFF=900        # 15 minutes of no file activity → declare timed out
EXTENSION=300          # one +5 min extension if output file explains delay
</constants>

<workflow>

## Step 1: Flag parsing

```bash
REPLY_MODE=false
CLEAN_ARGS=$ARGUMENTS
if echo "$ARGUMENTS" | grep -q -- '--reply'; then
    REPLY_MODE=true
    CLEAN_ARGS=$(echo "$ARGUMENTS" | sed 's/--reply//g' | xargs)
fi # timeout: 5000
```

`REPLY_MODE` is only meaningful when `$CLEAN_ARGS` is a number — silently ignored for `health` and `ecosystem`.

```bash
DIRECT_PATH_MODE=false
if echo "$CLEAN_ARGS" | grep -qE '\.md$'; then
    DIRECT_PATH_MODE=true
    REPORT_FILE="$CLEAN_ARGS"
fi # timeout: 5000
TODAY=$(date +%Y-%m-%d)
```

`DIRECT_PATH_MODE=true` is only valid when `REPLY_MODE=true` — if combined without `--reply`, Step 2 will print an error and stop.

## Step 2: Reply-mode fast-path (only when `REPLY_MODE=true`)

Skip this step when `REPLY_MODE=false` and `DIRECT_PATH_MODE=false`.

**Direct report path** (`DIRECT_PATH_MODE=true` — checked first):

- `REPLY_MODE=false` → print `Error: --reply is required when passing a .md report path` and stop.
- `REPLY_MODE=true` and file does not exist (`[ ! -f "$REPORT_FILE" ]`) → print `Error: report not found: $REPORT_FILE` and stop.
- `REPLY_MODE=true` and file exists → print `[direct] using $REPORT_FILE` → skip immediately to Step 7. Do not run the auto-detection fast-path below.

The remaining fast-path logic (TODAY, REPORT_FILE auto-construction, drift check) only runs when `DIRECT_PATH_MODE=false`.

When `REPLY_MODE=true`, check whether a fresh report already exists before making any API calls — if it does and the item has had no new activity, skip straight to Step 7:

```bash
REPORT_FILE=".reports/analyse/thread/output-analyse-thread-$CLEAN_ARGS-$TODAY.md"
DRIFT=false
FAST_PATH=false

if [ -f "$REPORT_FILE" ]; then
    REPORT_MTIME=$(stat -f %m "$REPORT_FILE" 2>/dev/null || stat -c %Y "$REPORT_FILE")                                   # timeout: 5000
    UPDATED_AT=$(gh api "repos/{owner}/{repo}/issues/$CLEAN_ARGS" --jq '.updated_at' 2>/dev/null)                        # timeout: 6000
    UPDATED_TS=$(date -d "$UPDATED_AT" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$UPDATED_AT" +%s 2>/dev/null) # timeout: 5000
    [ "$UPDATED_TS" -gt "$REPORT_MTIME" ] && DRIFT=true
    [ "$DRIFT" = "false" ] && FAST_PATH=true
fi
```

- `FAST_PATH=true` → print `[resume] reusing existing report for #$CLEAN_ARGS` and jump **directly to Step 7**. Skip Steps 3–6 entirely.
- `FAST_PATH=false` (report missing or drift detected) → continue to Step 3.

## Step 3: Cache layer (numeric arguments only)

Check for a local cache file before making API calls — prevents redundant fetches and avoids GitHub rate limits when re-analysing the same item in the same day.

```bash
CACHE_DIR=".cache/gh"
CACHE_FILE="$CACHE_DIR/$CLEAN_ARGS-$TODAY.json"
mkdir -p "$CACHE_DIR" # timeout: 5000
```

**Cache hit** — if `$CACHE_FILE` exists:

- Read `type`, `item`, and `comments` fields from the JSON
- Skip all primary `gh` item fetches in `modes/thread.md`
- Print `[cache] #$CLEAN_ARGS ($TODAY)` as a one-line status note
- Still run wide-net searches (dynamic — never cached)

**Cache miss** — after fetching in `modes/thread.md`, write:

```bash
jq -n \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg type "$TYPE" \
    --argjson number "$CLEAN_ARGS" \
    --argjson item "$ITEM" \
    --arg comments "$COMMENTS" \
    '{"ts":$ts,"type":$type,"number":$number,"item":$item,"comments":$comments}' \
    >"$CACHE_FILE" # timeout: 5000
```

**Stale cache** — a file for the same number but an earlier date is ignored. Old files are left in place — they are small and provide audit history. Prune files older than 30 days: `find .cache/gh -mtime +30 -delete`

Cache applies to: issue/PR/discussion primary fetch and comments. Cache does NOT apply to: `gh issue list`, `gh pr list`, `gh pr checks`, `gh pr diff`, discussion list queries, health/ecosystem modes.

## Step 4: Auto-Detection (numeric arguments only)

Issues, PRs, and discussions share a unified running index — a given number is exactly one type. If cache hit: read `TYPE` and `ITEM` from `$CACHE_FILE` — skip the `gh` calls below.

If cache miss:

```bash
# 4a: try the issues API (covers both issues and PRs)
ITEM=$(gh api "repos/{owner}/{repo}/issues/$CLEAN_ARGS" 2>/dev/null) # timeout: 6000

if [ -n "$ITEM" ]; then
	TYPE=$(echo "$ITEM" | jq -r 'if .pull_request then "pr" else "issue" end') # timeout: 5000
else
	# 4b: try discussions via GraphQL
	DISC=$(gh api graphql -f query='
    query($owner:String!,$repo:String!,$number:Int!){
      repository(owner:$owner,name:$repo){
        discussion(number:$number){ title }
      }
    }' -f owner='{owner}' -f repo='{repo}' -F number=$CLEAN_ARGS \
		--jq '.data.repository.discussion.title' 2>/dev/null) # timeout: 6000
	[ -n "$DISC" ] && TYPE="discussion" || TYPE="unknown"
fi
# unknown → print "Item #N not found" and stop
```

## Step 5: Mode dispatch

Read `.claude/skills/analyse/modes/<mode>.md` and execute all steps defined there.

| Argument          | Mode file            |
| ----------------- | -------------------- |
| number (any type) | `modes/thread.md`    |
| `health`          | `modes/health.md`    |
| `ecosystem`       | `modes/ecosystem.md` |

## Step 6: Reply gate — STOP CHECK

**Run this step before the Confidence block regardless of `--reply` mode.**

If `REPLY_MODE=true`: your response is **incomplete** until you have executed Step 7 below and written the reply file. Do not add a Confidence block or end your response here — proceed to Step 7 immediately.

If `REPLY_MODE=false`: skip Step 7 and end with the Confidence block now.

## Step 7: Draft contributor reply (only when --reply, thread mode only)

The report at `$REPORT_FILE` is guaranteed to exist at this point — either reused via the fast-path (Step 2, `FAST_PATH=true`) or freshly written by Step 5. `$DRIFT` is set by Step 2 (`true` if new activity was detected, `false` otherwise).

**Spawn oss-shepherd** with the report path, the item number, and this prompt (note: oss-shepherd runs in a forked context — all required context must be self-contained in the prompt):

"Write your full output to `.reports/analyse/thread/output-reply-thread-<number>-$(date +%Y-%m-%d).md` using the Write tool. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\".reports/analyse/thread/output-reply-thread-<number>-<date>.md\",\"sentences\":N,\"resolved\":\"yes|no|partial\",\"confidence\":0.N,\"summary\":\"Reply: N sentences, resolved: yes|no|partial\"}` Read the report at `<path>` for context. If the item is an issue or discussion, also fetch the full thread (`gh issue view <number> --comments` or equivalent GraphQL for discussions) and read every comment."

**Health monitoring**: Agent spawns are synchronous — Claude awaits the response natively. If oss-shepherd does not return within `$HARD_CUTOFF` seconds, surface any partial output found at the expected reply path and mark with ⏱ in the terminal summary. Never silently omit.

Print compact terminal summary:

```
  Reply — N sentences  |  resolved: yes|no|partial
  [analysis refreshed — new activity since last report]  ← only if drift detected

  Reply:  .reports/analyse/thread/output-reply-thread-<number>-<date>.md
```

End your response with a `## Confidence` block per CLAUDE.md output standards — this is always the **absolute last thing**. If `REPLY_MODE=true`, place this block **after** completing the reply step above, never after the analysis alone.

</workflow>

<notes>

- Mode files live in `.claude/skills/analyse/modes/` — one file per mode, fully self-contained
- `modes/thread.md` handles all three thread types (issue, PR, discussion) via internal branching
- Always use `gh` CLI — never hardcode repo URLs
- Run `gh auth status` first if commands fail; user may need to authenticate
- For closed items, note the resolution so history is useful
- Don't post responses without explicit user instruction — only draft them
- **Forked context**: this skill runs with `context: fork` — it operates without access to the current conversation history. All required context must be provided as the skill argument or in your prompt.
- Follow-up chains:
  - Issue with confirmed bug → `/develop fix` to diagnose, reproduce with test, and apply targeted fix
  - Issue is a feature request → `/develop feature` for TDD-first implementation
  - PR with quality concerns → `/review` for comprehensive multi-agent code review
  - Draft responses → use `--reply` to auto-draft via oss-shepherd; or invoke oss-shepherd manually

</notes>
