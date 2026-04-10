---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads the full thread, linked issues, and PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out the PR branch (fork-aware via gh pr checkout), merges BASE into it, and resolves conflicts semantically using the contributor's intent as the priority lens; (3) implements each action item as a separate attributed commit via Codex, then pushes back to the contributor's fork. Supports three source modes: pr (live GitHub comments only), report (latest /review report findings as action items, no GitHub re-fetch), and pr + report (both sources aggregated and deduplicated in one pass). Also accepts bare comment text for single-comment dispatch."
argument-hint: <PR number or URL> [report] | report | <review comment text>
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Agent, TaskCreate, TaskUpdate, AskUserQuestion
---

<objective>

OSS maintainer fast-close workflow. Given a PR number, three phases fire automatically:

1. **PR intelligence** — synthesize contribution motivation from the PR body, linked issues, and full discussion thread; classify every comment into action items
2. **Conflict resolution** — check out the PR branch (fork-aware), merge `BASE_REF` into it, resolve conflicts semantically with the contributor's intent as the priority lens
3. **Action item implementation** — implement each action item as a separate commit attributed to the review comment, then push back to the contributor's fork

The result: a conflict-free, review-addressed PR branch pushed to the fork, ready for the maintainer to merge on GitHub — all without touching the GitHub UI.

**Core invariant — transparent and reversible**: every action produces a visible, named git object (merge commit, fix commit) that can be inspected and reverted individually. This is why all conflict resolution goes forward via `git merge` (creates a new commit with two parents) and never via `git rebase` (rewrites SHA history, destroys the ability to revert or cherry-pick individual steps). Each action item becomes its own commit for the same reason — granular revert is always possible.

When given bare comment text, skip straight to Codex dispatch (Step 12).

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - Omitted → **review-handoff mode**: auto-detect PR from the most recent `.temp/output-review-*.md` file
  - A PR number (e.g. `42`) or GitHub PR URL → **pr mode**
  - `report` (bare word) → **report mode**: use latest review report findings as action items; no GitHub re-fetch
  - `42 report` or `<URL> report` → **pr + report mode**: aggregate live GitHub comments + review report findings, deduplicated in one pass
  - Bare review comment text → **comment dispatch mode** (jumps to Step 12)

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

## Step 1: Pre-flight

```bash
# From _shared/preflight-helpers.md — TTL 4 hours, keyed per binary
preflight_ok() {
    local f=".claude/state/preflight/$1.ok"
    [ -f "$f" ] && [ $(($(date +%s) - $(cat "$f"))) -lt 14400 ]
}
preflight_pass() {
    mkdir -p .claude/state/preflight
    date +%s >".claude/state/preflight/$1.ok"
}

# codex — optional; intelligence + conflict resolution work without it
CODEX_AVAILABLE=false
if preflight_ok codex; then
    CODEX_AVAILABLE=true && echo "codex (openai-codex): ok (cached)"
elif claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'; then # timeout: 15000
    preflight_pass codex && CODEX_AVAILABLE=true && echo "codex (openai-codex): ok"
else
    echo "codex (openai-codex): missing — action item implementation (Step 8) will be skipped"
fi

# gh binary + auth — required; cached for 4h (auth won't change within a session)
if preflight_ok gh; then
    echo "gh: ok (cached)"
elif which gh &>/dev/null && gh auth status &>/dev/null; then
    preflight_pass gh && echo "gh: ok ($(gh auth status 2>&1 | grep 'Logged in' | head -1 | xargs))"
elif which gh &>/dev/null; then
    echo "Pre-flight failed: gh found but not authenticated — run: gh auth login" && exit 1
else
    echo "Pre-flight failed: gh not found — install: brew install gh" && exit 1
fi

# Show current remotes — confirms we are in the right repo and surfaces any existing fork remotes
git remote -v # timeout: 3000

# Sync with remote tracking branch before any git work.
# When local is 1 commit ahead and remote is also 1 commit ahead, git pull merges cleanly.
# This prevents the downstream `git merge --continue --no-edit` from being called out of state.
UPSTREAM=$(git rev-parse --abbrev-ref @{u} 2>/dev/null)
if [ -n "$UPSTREAM" ]; then
    git fetch origin 2>/dev/null || true # timeout: 6000
    REMOTE_AHEAD=$(git log HEAD..@{u} --oneline 2>/dev/null | wc -l | tr -d ' ')
    if [ "$REMOTE_AHEAD" -gt 0 ]; then
        echo "Remote is $REMOTE_AHEAD commit(s) ahead — running git pull..."
        git pull || {
            echo "Pre-flight failed: git pull had conflicts — resolve manually before running /resolve"
            exit 1
        } # timeout: 6000
        echo "✓ git pull: merged"
    else
        echo "✓ git: up to date"
    fi
fi
```

If gh is missing or not authenticated: stop (error printed above)

If codex is missing: set `CODEX_AVAILABLE=false` and continue — Steps 3–7 (intelligence + conflict resolution) work without Codex; Step 8 (action items) will be skipped with a notice: `⚠ codex not found — skipping action items. Install: npm install -g @openai/codex`

### Review-handoff auto-detect (when $ARGUMENTS is empty)

If `$ARGUMENTS` is empty:

```bash
# Find most recent review output (written by /review to .temp/)
REVIEW_FILE=$(ls -t .temp/output-review-*.md 2>/dev/null | head -1)
if [ -z "$REVIEW_FILE" ]; then
    echo "No review output found in .temp/ — run /review <PR#> first, or provide a PR number"
    exit 1
fi
echo "→ Using: $REVIEW_FILE"
```

Read `$REVIEW_FILE` with the Read tool. Extract the PR number from the header line:

- Pattern: `## Code Review: PR #<N>` or `## Code Review: <N>` (where N is a number)
- Grep: `grep -oE '(PR #|#)?[0-9]+' "$REVIEW_FILE" | head -1 | grep -oE '[0-9]+'`

If a PR number is found, set `$ARGUMENTS = <extracted number>` and proceed in PR mode (Step 2 onwards). Print: `→ Resolved PR #<N> from review output.`

If no PR number is extractable (review was run on a local path, not a PR), print: "Review output does not reference a PR — provide a PR number explicitly: `/resolve <PR#>`" and exit 1.

Parse $ARGUMENTS:

- If it matches `<number> report` or `<URL> report` (number/URL followed by the word `report`) → **pr + report mode**: strip `report` suffix, set PR# from the remaining token; also find the latest review report using `ls -t .temp/output-review-*.md 2>/dev/null | head -1`; if no report found print a warning but continue in pr mode
- If it equals the bare word `report` → **report mode**: find the latest review report using `ls -t .temp/output-review-*.md 2>/dev/null | head -1`; if no report found stop with: "No review report found in .temp/ — run /review \<PR#> first, or provide a PR number"; extract PR# from header if present
- If it is a number or matches a GitHub PR URL pattern → **pr mode** (continue from Step 2)
- Otherwise → **comment dispatch mode** (jump to Step 12)

### Sources block

Print immediately after mode is resolved, before any GitHub API calls:

```
## Resolve — sources

Mode    : <pr | report | pr + report>
PR      : #<N>  (or "n/a — working on current branch" when report mode found no PR#)
GitHub  : fetching live comments · reviews · inline code comments  (or "not fetched" in report mode)
Report  : <path to report file>  (or "not used" in pr mode / "not found — run /review first" if missing)

Proceeding…
```

## Step 2: Create task

```
TaskCreate(
  subject="Resolve PR #<number>",
  description="OSS fast-close: intelligence → conflicts → action items for PR #<number>",
  activeForm="Resolving PR #<number>"
)
```

Mark it `in_progress` immediately.

## Step 3a: Report intelligence (report mode only)

*Skip to Step 3b (PR intelligence) when in pr mode or pr + report mode.*

When mode == **report**:

Read the review report file. Parse structured findings from each `###` section header (`### [blocking] Critical`, `### Architecture & Quality`, `### Test Coverage Gaps`, `### Performance Concerns`, `### Documentation Gaps`, `### Static Analysis`, `### API Design`, `### Codex Co-Review`). Skip `### OSS Checks`, `### Recommended Next Steps`, `### Review Confidence`, and `### Issue Root Cause Alignment`.

Map each finding bullet to the action item schema:

| Severity in report       | `type`                                 |
| ------------------------ | -------------------------------------- |
| CRITICAL or `[blocking]` | `[req]`                                |
| HIGH                     | `[req]`                                |
| MEDIUM                   | `[suggest]`                            |
| LOW                      | `[suggest]` (omit if total items > 10) |

- `author`: the section owner agent (e.g., `sw-engineer` for Architecture, `qa-specialist` for Test Coverage)
- `file` / `line`: extract from `file:line` notation in the finding bullet; leave blank if absent
- `full_comment_text`: the full finding bullet text
- All items carry the tag `[report]` as a prefix to `type` (e.g., `[report][req]`, `[report][suggest]`)

If PR# was found in the report header (`## Code Review: PR #<N>` or similar):

- Set `$ARGUMENTS = <N>` and proceed to Step 4 (checkout); skip Step 3b (PR intelligence) entirely
- After checkout, skip directly to Step 8 with the report-derived action item list

If no PR# was found in the header:

- Skip Step 3b and Step 4 entirely; work on the current branch as-is
- Skip directly to Step 8 with the report-derived action item list

## Step 3b: PR intelligence

Fetch full PR metadata in one call:

```bash
gh pr view \
    number,title,body,author,labels,isDraft,state, headRefName,baseRefName, headRepositoryOwner,headRepository,isCrossRepository,url, closingIssuesReferences <PR# >--json
```

Extract and record:

- `HEAD_REF` — source branch name (`.headRefName`)
- `BASE_REF` — target branch name (`.baseRefName`, e.g. `main`, `develop`)
- `PR_AUTHOR` — contributor's GitHub login (`.author.login`)
- `HEAD_REPO_OWNER` — owner of the fork/head repository (`.headRepositoryOwner.login`)
- `BASE_REPO_OWNER` — owner of the base repository; extract from `.url` via `split("/")[3]` or run `gh repo view --json owner -q .owner.login` in the project root
- `IS_FORK` — use `.isCrossRepository` directly (`true` = fork PR, `false` = same-repo branch)
- `CLOSING_ISSUES` — list of linked issue numbers (`.closingIssuesReferences[].number`)

Fetch the full discussion:

```bash
gh pr view <PR# >--comments                        # PR-level comments + timeline
gh api repos/{owner}/{repo}/pulls/ <PR# >/reviews  # formal reviews (Approve / Request Changes)
gh api repos/{owner}/{repo}/pulls/ <PR# >/comments # inline code comments with file + line
```

If `CLOSING_ISSUES` is non-empty, fetch each linked issue for motivation context:

```bash
gh issue view title,body <issue# >--json
```

### Synthesize contribution motivation

Read the PR title, PR body, linked issue descriptions, and commit messages together. Produce a 2–3 sentence paragraph:

- What problem or gap the contributor is solving (from linked issues or PR description)
- Why they chose this particular approach (from the PR body, any design notes in commits)
- What the expected user-visible outcome is

This motivation summary is the **priority lens for conflict resolution** in Step 7 — it tells you whose logic should win when both sides touched the same area.

### Classify action items

Read every comment, review, and inline code comment. Classify each:

| Code            | Meaning                                                                                        |
| --------------- | ---------------------------------------------------------------------------------------------- |
| `[req]`         | Change **required** before merge — requested by a reviewer with write access or the maintainer |
| `[suggest]`     | Improvement suggested — nice-to-have, non-blocking                                             |
| `[question]`    | Open question that needs an answer before deciding what code to write                          |
| `[done]`        | A subsequent commit or reply already addressed this — skip                                     |
| `[info]`        | Praise, acknowledgement, emoji-only — skip                                                     |
| `[self-review]` | Finding from the `/review` report — not a GitHub commenter; author = agent name                |

Build `ACTION_ITEMS`: `[{id, type, author, summary, file, line, full_comment_text}]`

Print the action item table:

```
### Action Items — PR #<number>

| Type | Author | Status | Summary | File:Line |
|------|--------|--------|---------|-----------|
| [req] | @reviewer | pending | rename param `x` to `count` | src/foo.py:42 |
| [suggest] | @maintainer | pending | add docstring | — |
| [question] | @reviewer | pending | why not use X instead? | — |
```

> **Guard**: if `[req]` items > 15, print the full list and use `AskUserQuestion` to ask which subset to implement, listing up to 4 grouped options drawn from the items table (mark the first/smallest group as "(Recommended)"), before continuing.

Answer any `[question]` items that can be resolved from reading the code — if the answer is clear, reclassify to `[req]` or `[suggest]`; if it requires maintainer judgement, surface and pause. A question answered by the **contributor** (not the maintainer) is not automatically closed — if the contributor's answer reveals a known limitation or deferred work (e.g., "currently per-process, Redis is a follow-up"), keep it as `[question]` and surface it for the maintainer to explicitly accept or reject before proceeding.

## Step 3c: Merge report findings (pr + report mode only)

*Skip when in pr mode.*

When mode == **pr + report**:

Find and read the latest review report (`ls -t .temp/output-review-*.md 2>/dev/null | head -1`). Parse structured findings using the same logic as Step 3a (report mode) above.

**Deduplication**:

- For each report finding, check if a GitHub action item already targets the same `file:line`:
  - **Match found** → drop the report item; annotate the GitHub item's summary with `(also flagged by /review)`
  - **Semantic match** (same file, no exact line, similar description) → drop the report item; same annotation
  - **No match** → append the report finding to the action item list as a `[report]` item

**Re-prefix GitHub items**: once deduplication is complete, add `[gh]` as a source prefix to all GitHub-sourced items — `[req]` → `[gh][req]`, `[suggest]` → `[gh][suggest]`, `[question]` → `[gh][question]`. This matches the `[report]` source prefix and makes source unambiguous in the merged table. This re-prefixing applies only in `pr + report` mode; single-source `pr` mode items remain plain `[req]`/`[suggest]`.

**Result**: a single merged `ACTION_ITEMS` list. GitHub-sourced items appear first (maintaining `[gh][req]`/`[gh][suggest]` order), followed by surviving `[report]` items. Print a merge summary before the action item table:

```
Report merged: <N> findings from /review · <M> deduplicated against GitHub comments · <K> added as [report] items
```

## Step 4: Checkout PR branch

```bash
SAVED_BRANCH=$(git rev-parse --abbrev-ref HEAD)  # timeout: 3000
gh pr checkout <PR#>   # fetches HEAD_REF; for forks, adds the contributor's remote + sets up tracking  # timeout: 15000
```

`gh pr checkout` handles forks automatically — it adds a remote named after the contributor's GitHub login and configures tracking. Verify:

```bash
git remote -v | grep -v fetch | grep -v push | head -10 # timeout: 3000
git status                                              # confirm we are on HEAD_REF  # timeout: 3000
```

Record `FORK_REMOTE`: for fork PRs it is the contributor's login (e.g. `alice`); for same-repo PRs it is `origin`. The push command in Step 9 is always `git push` (tracking is configured correctly by `gh pr checkout`).

## Step 5: Conflict detection

```bash
# Detect in-progress merge via MERGE_HEAD sentinel — git status --porcelain does not expose this reliably
MERGE_HEAD_FILE="$(git rev-parse --git-dir)/MERGE_HEAD" # timeout: 3000
test -f "$MERGE_HEAD_FILE" && echo "MERGING" || echo "clean"
```

**Case A — MERGING state** (`MERGE_HEAD` present — a previous `git merge` left markers in the PR branch):

Work directly with the existing markers. Skip to Step 6, substep 6c.

**Case B — not MERGING**:

Merge `BASE_REF` into the PR branch (this updates the PR with the latest base changes — the merge direction is BASE → HEAD_REF, not the reverse):

```bash
git fetch origin "$BASE_REF"                     # ensure origin/$BASE_REF is current  # timeout: 6000
git merge "origin/$BASE_REF" --no-commit --no-ff # timeout: 6000
```

Check for conflicted files:

```bash
git diff --name-only --diff-filter=U # timeout: 3000
```

If no conflicts → complete the merge and skip to Step 8:

```bash
git merge --continue --no-edit
```

Report a clean merge, skip Steps 6–7, continue from Step 8.

If more than 20 conflicted files → abort and stop:

```bash
git merge --abort
```

Report the count and file list; use `AskUserQuestion` to ask whether to continue or re-scope, with options: "Continue (Recommended)" (proceed with all conflicted files), "Re-scope" (abort and narrow the merge target).

## Step 6: Distill conflict context

Run before touching any conflict markers.

### 6a: Source-branch intent

Use the contribution motivation from Step 3b as the primary lens. Additionally:

```bash
MERGE_BASE=$(git merge-base "origin/$BASE_REF" "$HEAD_REF") # timeout: 3000
git log $MERGE_BASE..$HEAD_REF --oneline --no-merges        # timeout: 3000
git diff $MERGE_BASE $HEAD_REF --stat                       # timeout: 3000
```

One-sentence summary: which files/modules this PR owns and what it changes about them.

### 6b: Target-branch drift (the "surprises")

```bash
git log $MERGE_BASE..origin/$BASE_REF --oneline --no-merges    # timeout: 3000
SOURCE_LAST_TIME=$(git log "$HEAD_REF" -1 --format="%ci")      # timeout: 3000
git log origin/$BASE_REF --after="$SOURCE_LAST_TIME" --oneline # commits the contributor never saw  # timeout: 3000
```

One-sentence summary: what independent changes landed on base after the contributor's last commit — these must be preserved unconditionally.

## Step 7: Resolve per conflicted file

Delegate per-file conflict edits to `sw-engineer`. Build the spawn prompt with all three context sources, then check the result before completing the merge.

### 7a: Spawn sw-engineer

Spawn `sw-engineer` with this prompt (fill in the bracketed sections from the steps indicated):

```
Agent(sw-engineer, prompt="
You are resolving merge conflicts in a checked-out PR branch.

## Conflicted files
<list every file from Step 5 `git diff --name-only --diff-filter=U` output, one per line>

## Contribution motivation (whose intent wins)
<2–3 sentence motivation summary from Step 3b>

## Merge context
### What HEAD_REF added (merge-base log)
<git log $MERGE_BASE..$HEAD_REF --oneline --no-merges output from Step 6a>

### Files changed by this PR (diff stat)
<git diff $MERGE_BASE $HEAD_REF --stat output from Step 6a>

## Instructions
For each conflicted file:
1. Use the Read tool to inspect the full file and locate all conflict markers
2. Determine the correct resolution using the contribution motivation above as the priority lens:
   - Contributor's new functionality takes priority for files the PR owns (introduced or substantially rewrote)
   - Base's independent refactors and config updates are always preserved
   - When both sides changed the same logic, blend: keep the PR's semantic change while incorporating the base's structural update
3. Use the Edit tool to apply targeted replacements that remove all conflict markers and produce the correct resolved content — do NOT rewrite the whole file; use Edit for minimal targeted replacements
4. After resolving each file, stage it with: git add -- <file>  (timeout: 3000)

Return ONLY a compact JSON envelope — no prose, no explanation:
{\"status\":\"done\",\"resolved\":N,\"staged\":N,\"confidence\":0.N}
")
```

> **Health monitoring**: Agent call is synchronous; Claude awaits response natively. If no response within ~15 min, surface partial results with ⏱ and proceed to merge with whatever files were staged.

### 7b: Verify and complete merge

Parse the JSON envelope returned by sw-engineer. Check that `resolved == staged` — if they differ, surface the discrepancy before proceeding (a mismatch means at least one file was resolved but not staged, which would leave the merge incomplete).

Complete the merge:

```bash
git merge --continue --no-edit # timeout: 3000
```

Print conflict report:

```
### Conflict Resolution

| File | Strategy | Notes |
|------|----------|-------|
| src/foo.py | Blended | kept PR's new param, adopted base's renamed import |
| config.yaml | Target | unrelated config change from base, PR had no opinion |

**Result**: N files resolved. Merge commit created.
```

## Step 8: Implement action items

If `CODEX_AVAILABLE=false`: mark all items `⚠ skipped — codex not installed` and skip to Step 9.

Process `[req]` items first, then `[suggest]` items. **Each item gets its own commit.**

> **Guard**: process at most 10 items, then pause and use `AskUserQuestion` to ask whether to continue with the remaining items, with options: "Continue (Recommended)" (process next batch of items), "Stop here" (finish report with items processed so far).

For each action item:

```bash
# Guard: ensure clean state before each item
test -z "$(git status --porcelain)" || { echo "⚠ dirty tree before item #<id> — stashing"; git stash push -m "resolve-pre-item-<id>"; }  # timeout: 3000

# Snapshot before
git diff HEAD --stat  # timeout: 3000

# Dispatch to Codex
Agent(subagent_type="codex:codex-rescue", prompt="Apply this review feedback to the codebase. Implement exactly what is requested and nothing more. If the change is already present or there is nothing actionable, make no changes and explain why. Feedback from @<author>: <full_comment_text>")

# Check whether code changed
git diff HEAD --stat  # timeout: 3000
```

If code changed → commit:

```bash
# Prerequisite: working tree must be clean before Step 7 Codex calls; verify with git diff --stat HEAD before proceeding.
# Stage tracked modifications + new files from Codex (never git add -A)
git add $(git diff HEAD --name-only)                                                     # timeout: 3000
git ls-files --others --exclude-standard | grep . | xargs git add -- 2>/dev/null || true # grep . filters empty output (macOS-portable; xargs -r is GNU-only); permission matcher sees 'git ls-files' as first token  # timeout: 3000
# timeout: 3000 — git commit (local operation)
git commit -m "$(
	cat <<'EOF'
<imperative short summary of the change>

[resolve #<item_id>] Review comment by @<author> (PR #<PR_NUMBER>):
"<first 72 chars of full_comment_text>..."
EOF
)"
```

If no code changed (already done or non-actionable) → record Codex's reason; do NOT create an empty commit.

Record per-item: `committed <SHA>` or `skipped — <Codex reason>`.

## Step 9: Lint and QA gate

```bash
RUN_DIR=".reports/resolve/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR" # timeout: 5000
```

Spawn both agents in parallel:

```
Agent(linting-expert): "Review all files changed in the current branch since origin/<BASE_REF>. List every lint/type violation. Apply inline fixes for any that are auto-fixable. Write your full findings to $RUN_DIR/linting-expert-step9.md using the Write tool, then return ONLY a compact JSON envelope: {fixed: N, remaining: N, files: [...]}."

Agent(qa-specialist, maxTurns: 15): "Review all files changed in the current branch since origin/<BASE_REF> for correctness, edge cases, and regressions. Flag any blocking issues (bugs, broken contracts, missing test coverage for the changed logic). Write your full findings to $RUN_DIR/qa-specialist-step9.md using the Write tool, then return ONLY a compact JSON envelope: {blocking: N, warnings: N, issues: [...]}."
```

> **Health monitoring**: Agent calls are synchronous; Claude awaits responses natively. If no response within ~15 min, surface partial results from `$RUN_DIR` with ⏱.

Wait for both. Then:

- If `linting-expert` made file changes → commit them:

```bash
git add $(git diff HEAD --name-only)                          # timeout: 3000
git commit -m "lint: auto-fix violations after resolve cycle" # timeout: 3000
```

- If `qa-specialist` reports **blocking** issues → fix each one (via Codex if `CODEX_AVAILABLE=true`, otherwise inline edit), then re-run qa-specialist once to confirm resolution; if issues remain after one fix pass, surface them in the final report and continue (do not loop indefinitely)
- Warnings (non-blocking) → record in the final report; do not block push

## Step 10: Push

*Skip this step entirely when in report mode with no PR# (variables `$FORK_REMOTE`, `$HEAD_REF`, `$BASE_REF` were never set — there is no fork branch to push to; the workflow ends at Step 11).*

```bash
# Ensure fork remote is present (gh pr checkout may not have added it for all setups)
if ! git remote get-url "$FORK_REMOTE" &>/dev/null; then # timeout: 3000
    REPO_NAME=$(git remote get-url origin | sed 's|.*/||' | sed 's|\.git$||')
    git remote add "$FORK_REMOTE" "https://github.com/$FORK_REMOTE/$REPO_NAME.git" # timeout: 3000
    echo "→ Added remote $FORK_REMOTE → https://github.com/$FORK_REMOTE/$REPO_NAME.git"
fi

# Configure tracking if not already set
git branch --set-upstream-to="$FORK_REMOTE/$HEAD_REF" 2>/dev/null || true # timeout: 3000

# Count commits ready to push and announce — user must approve the toolbar permission prompt
PUSH_COUNT=$(git rev-list "$FORK_REMOTE/$HEAD_REF..HEAD" --count 2>/dev/null || git rev-list "origin/$BASE_REF..HEAD" --count) # timeout: 3000
echo "→ $PUSH_COUNT commits ready to push to $FORK_REMOTE/$HEAD_REF — approve the git push request in the toolbar ↑ to complete"

git push # timeout: 30000
# gh pr checkout configured tracking to the fork branch — git push targets it automatically
```

If push is rejected (fork protection or stale tracking):

```bash
git push "$FORK_REMOTE" HEAD:"$HEAD_REF" # timeout: 30000
```

Verify the push reached GitHub:

```bash
gh pr view headRefOid,commits --jq '.commits[-3:] | .[].messageHeadline' <PR# >--json # timeout: 6000
```

Confirm the latest commit headlines match what was just committed.

## Step 11: Final report

Mark the task `completed`, then print:

```
## Resolve Report — PR #<number>

### Contribution
<2–3 sentence motivation summary from Step 3b>

### Conflicts
<conflict table from Step 7, or "No conflicts detected">

### Action Items

| Type | Author | Status | Summary | File:Line |
|------|--------|--------|---------|-----------|
| [req] | @reviewer | ✓ resolved | rename param x → count | src/foo.py:42 |
| [suggest] | @maintainer | ✓ resolved | add docstring | — |
| [question] | @reviewer | ⊘ answered inline — existing approach is correct per linked issue #42 | why not use X? | — |

### Lint + QA
<linting-expert summary: N fixes applied | or "no violations"> / <qa-specialist summary: N blocking fixed, N warnings | or "clean">

### Push
✓ Pushed to <remote>/<HEAD_REF> — N new commits

**Next**:
- `gh pr merge <PR#> --merge` to merge now (preserves all commits)
- `gh pr review <PR#> --approve` to approve first
- Reply to any `[question]` items with the rationale before merging if the reviewer is external

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. conflict strategy ambiguity, action items skipped at guard, Codex partial completion]
**Refinements**: N passes. — omit if 0 passes
```

______________________________________________________________________

# Independent entry point for comment-dispatch mode — not a continuation of the main PR workflow

## Step 12: Comment dispatch + Codex review loop

Reached when $ARGUMENTS is bare comment text (not a PR number or URL).

Create a task:

```
TaskCreate(
  subject="Resolve: <60-char summary of $ARGUMENTS>",
  description="<full $ARGUMENTS>",
  activeForm="Resolving comment"
)
```

If `CODEX_AVAILABLE=false`: stop with `⚠ codex plugin not found — install: /plugin marketplace add openai/codex-plugin-cc && /plugin install codex@openai-codex && /reload-plugins` and mark the task completed.

### 12a: Dispatch

```bash
Agent(subagent_type="codex:codex-rescue", prompt="Apply this review comment to the codebase. If the change is already present, or the comment has no actionable code change, make no changes and briefly explain why. Comment: $ARGUMENTS")
```

Record the initial dispatch outcome (code changed or no change + reason).

### 12b: Codex review loop (max 5 passes)

```bash
git diff HEAD --stat # timeout: 3000 — confirm there are changes to review
```

If no changes: skip the loop; set `CODEX_REVIEW_FINDINGS=""`.

Otherwise:

```pseudocode
for REVIEW_PASS in 1 2 3 4 5; do

  # Review phase — Agent() is a Claude Code tool call, not a shell command
  CODEX_OUT = Agent(subagent_type="codex:codex-rescue",
                    prompt="Review working-tree changes. End output with ISSUES_FOUND=N.")
  ISSUES_FOUND = parse CODEX_OUT for ISSUES_FOUND=N (default 0)

  if ISSUES_FOUND == 0: break

  # Fix phase
  Agent(subagent_type="codex:codex-rescue",
        prompt="Apply this fix: <issue description from review>")

done

if REVIEW_PASS == 5 and ISSUES_FOUND > 0:
  echo "⚠ Review loop hit 5-pass cap — $ISSUES_FOUND issues remain; surface to user"
```

### 12c: Lint and QA gate

If code was changed (dispatch or review loop produced commits):

```bash
RUN_DIR=".reports/resolve/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR" # timeout: 5000
```

Spawn both agents in parallel:

```
Agent(linting-expert): "Review all files changed in HEAD (git diff HEAD~N..HEAD where N = number of commits just made). List every lint/type violation. Apply inline fixes for any that are auto-fixable. Write your full findings to $RUN_DIR/linting-expert-step12c.md using the Write tool, then return ONLY a compact JSON envelope: {fixed: N, remaining: N, files: [...]}."

Agent(qa-specialist, maxTurns: 15): "Review all files changed in the most recent commits for correctness, edge cases, and regressions. Flag any blocking issues. Write your full findings to $RUN_DIR/qa-specialist-step12c.md using the Write tool, then return ONLY a compact JSON envelope: {blocking: N, warnings: N, issues: [...]}."
```

> **Health monitoring**: Agent calls are synchronous; Claude awaits responses natively. If no response within ~15 min, surface partial results from `$RUN_DIR` with ⏱.

- If `linting-expert` made changes → commit:

```bash
git add $(git diff HEAD --name-only)                          # timeout: 3000
git commit -m "lint: auto-fix violations after resolve cycle" # timeout: 3000
```

- If `qa-specialist` reports blocking issues → fix inline, then re-run once; surface any unresolved issues in the report

Mark the task `completed`, then print:

```
## Resolve Report

**Verdict**: ✓ resolved | ⊘ no change — <Codex's reason>

### Codex Review
<findings across passes, or "No issues found" / "Skipped — no changes">

### Lint + QA
<linting-expert summary: N fixes applied | or "no violations"> / <qa-specialist summary: N blocking fixed, N warnings | or "clean">

**Next**: review diff and commit | reply to reviewer with Codex's explanation

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. Codex partial completion, ambiguous comment intent]
**Refinements**: N passes. — omit if 0 passes
```

</workflow>

<notes>

- **Pre-flight git pull** — Step 1 fetches the current branch's remote tracking ref and pulls if the remote is ahead; the common 1-local-ahead / 1-remote-ahead divergence merges cleanly without user intervention; if `git pull` has conflicts the skill exits with a clear message to resolve manually first — this prevents `git merge --continue` being called with no in-progress merge
- **Branch safety** — `gh pr checkout <PR#>` always switches to the PR's HEAD branch, never to `main`/`master`; all commits land on the PR branch by design. Never push directly to the default branch — if for any reason the PR branch turns out to be the default branch, abort and surface the issue to the user
- **OSS fork support** — `gh pr checkout <PR#>` works identically for same-repo branches and forks; for forks it adds the contributor's remote (named after their login) and configures tracking; plain `git push` then targets the fork branch correctly with no manual remote setup
- **Merge direction** — the skill merges `origin/BASE_REF` INTO `HEAD_REF` (updating the PR branch), NOT the reverse; this preserves the PR branch as the source of truth and keeps the GitHub merge clean; the maintainer still clicks Merge (or runs `gh pr merge`) after reviewing
- **Never rebase** — all conflict resolution uses `git merge`; rebase rewrites commit SHAs and breaks cherry-pick / revert; Step 5 uses `git merge --continue --no-edit` to complete a merge after conflict resolution
- **Contribution motivation before code** — Step 3a must happen before any file is read or edited; it provides the "whose intent wins" lens for conflict decisions; reading the PR body and linked issues often reveals design constraints that are invisible from git diff alone
- **Separate commits per action item** — each `[req]` and `[suggest]` item must produce exactly one atomic commit; the `[resolve #N]` tag in the message lets `git log --grep='resolve #3'` find the exact commit for any action item; this makes the history reviewable, the diff bisectable, and each change independently revertable; an empty commit is never created when Codex makes no changes
- **`[question]` items** — answer first (in a PR comment or inline reasoning), then reclassify before implementing; never silently implement a response to an unanswered question
- **Case A (already MERGING)** — if a prior `git merge origin/$BASE_REF` left markers, the skill skips Steps 5 detection and 6 context-distill, jumps directly to Step 7a (resolve per file), uses the existing markers; no new merge is started
- **Push verification** — always confirm via `gh pr view --json commits` that new commits appear on GitHub before reporting success; exit code 0 from `git push` is necessary but not sufficient (branch protection rules can silently reject)
- **`gh pr merge` flags**: `--merge` preserves all commits and history; `--squash` collapses to one (loses individual action-item commits); never suggest `--rebase` (rewrites SHAs); default recommendation is `--merge` unless project convention says otherwise
- **Escape hatch**: `git merge --abort` undoes the entire conflict state and returns the PR branch to pre-merge state; use `git push --force-with-lease` (never plain `--force`) if push is rejected after local amending
- **Codex agent health**: `Agent(subagent_type="codex:codex-rescue", ...)` calls are background agents subject to CLAUDE.md §8 health monitoring — 15-min hard cutoff, ⏱ marker on timeout; surface partial results via `tail -100` on the output file if the agent stalls
- **Worktree cleanup safety net**: `SessionEnd` hook runs `git worktree prune` — catches any orphaned worktrees from prior sessions
- **Mode routing and source-selection logic**: see Steps 3a–3c and `<inputs>` for full mode definitions, source routing, and action-item derivation per mode.
- **`[gh]` items** (pr + report mode only): commit messages use: `[resolve #<id>] @<reviewer> (gh):` — same as plain `[req]`/`[suggest]` in pr mode, plus the `(gh)` source annotation.
- **`[report]` items**: commit messages for these items should attribute the finding to the agent, not a GitHub commenter: `[resolve #<id>] /review finding by <agent-name> (report: <report-path>):` — this distinguishes automated findings from human reviewer requests in git history.
- **Sources block**: always printed after mode resolution and before any GitHub API calls — gives the user a clear "abort if wrong source" moment with zero cost.
- **Step 7 delegation** — Step 7 delegates per-file conflict edits to `sw-engineer`; resolve owns workflow orchestration and context (conflict list, motivation, merge-base log, diff stat); sw-engineer owns code-level semantic resolution (Read → Edit → stage); resolve retains the conflict report block and the `git merge --continue` call.
- Follow-up chains:
  - After push → `gh pr review <PR#> --approve` if satisfied; for substantial maintainer changes, comment on the PR explaining what was done and why — don't silently push to a contributor's fork
  - For `[question]` items left unanswered → post a PR comment with the rationale before merging; gives the contributor context and closes the thread
  - After merge → linked issues close automatically when the PR is merged, provided the PR body contains `Closes #<issue#>` or `Fixes #<issue#>`; if `CLOSING_ISSUES` were found in Step 3b but the PR body lacks those keywords, add them to the PR description: `gh pr edit <PR#> --body "$(gh pr view <PR#> --json body -q .body)\n\nCloses #<issue#>"`

</notes>
