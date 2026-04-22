---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads the full thread, linked issues, and PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out the PR branch (fork-aware via gh pr checkout), merges BASE into it, and resolves conflicts semantically using the contributor's intent as the priority lens; (3) implements each action item as a separate attributed commit via Codex, then pushes back to the contributor's fork. Supports three source modes: pr (live GitHub comments only), report (latest /review report findings as action items, no GitHub re-fetch), and pr + report (both sources aggregated and deduplicated in one pass). Also accepts bare comment text for single-comment dispatch."
argument-hint: <PR number or URL> [report] | report | <review comment text>
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Agent, TaskCreate, TaskUpdate, AskUserQuestion
---

<objective>

OSS maintainer fast-close workflow. PR number → three phases fire automatically:

1. **PR intelligence** — synthesize motivation from PR body, linked issues, thread; classify comments into action items
2. **Conflict resolution** — checkout PR branch (fork-aware), merge `BASE_REF`, resolve conflicts with contributor intent as priority lens
3. **Action item implementation** — implement each item as separate commit attributed to review comment, push to contributor's fork

Result: conflict-free PR branch pushed to fork, ready to merge — no GitHub UI.

**Core invariant — transparent and reversible**: every action = visible named git object. Use `git merge` (new commit, two parents), never `git rebase` (rewrites SHA, kills revert/cherry-pick). Each action item = own commit — granular revert always possible.

Bare comment text → skip to Codex dispatch (Step 12).

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - Omitted → **review-handoff mode**: auto-detect PR from most recent `.temp/output-review-*.md`
  - PR number (e.g. `42`) or GitHub PR URL → **pr mode**
  - `report` (bare word) → **report mode**: latest review findings as action items; no GitHub re-fetch
  - `42 report` or `<URL> report` → **pr + report mode**: aggregate live GitHub comments + review report, deduplicated in one pass
  - Bare review comment text → **comment dispatch mode** (jumps to Step 12)

</inputs>

<workflow>

<!-- Agent Resolution: canonical table at plugins/oss/skills/_shared/agent-resolution.md -->

## Agent Resolution

```bash
# Locate oss plugin shared dir — installed first, local workspace fallback
_OSS_SHARED=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/_shared 2>/dev/null | head -1)
[ -z "_OSS_SHARED" ] && _OSS_SHARED="plugins/oss/skills/_shared"
```

Read `$_OSS_SHARED/agent-resolution.md`. Contains: foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`.

**Task hygiene**: Before creating tasks, call `TaskList`. For each task:

- `completed` if done
- `deleted` if orphaned/irrelevant
- `in_progress` only if genuinely continuing

## Step 1: Pre-flight

```bash
# From plugins/foundry/skills/_shared/preflight-helpers.md — TTL 4 hours, keyed per binary
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

If gh missing or not authenticated: stop (error printed above).

Codex missing: set `CODEX_AVAILABLE=false`, continue — Steps 3–7 work without Codex; Step 8 skipped with notice: `⚠ codex not found — skipping action items. Install: npm install -g @openai/codex`

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

Read `$REVIEW_FILE`. Extract PR number from header:

- Pattern: `## Code Review: PR #<N>` or `## Code Review: <N>`
- Grep: `grep -oE '(PR #|#)?[0-9]+' "$REVIEW_FILE" | head -1 | grep -oE '[0-9]+'`

PR found → set `$ARGUMENTS = <N>`, proceed PR mode. Print: `→ Resolved PR #<N> from review output.`

No PR number extractable → print: "Review output does not reference a PR — provide a PR number explicitly: `/resolve <PR#>`" and exit 1.

Parse $ARGUMENTS:

- Matches `<number> report` or `<URL> report` → **pr + report mode**: strip `report` suffix, set PR# from remaining token; find latest review report via `ls -t .temp/output-review-*.md 2>/dev/null | head -1`; no report found → warn but continue in pr mode
- Equals bare word `report` → **report mode**: find latest review report via `ls -t .temp/output-review-*.md 2>/dev/null | head -1`; no report found → stop with: "No review report found in .temp/ — run /review \<PR#> first, or provide a PR number"; extract PR# from header if present
- Number or GitHub PR URL → **pr mode** (continue Step 2)
- Otherwise → **comment dispatch mode** (jump to Step 12)

## Step 2: Create initial task

```text
TaskCreate(
  subject="Resolve PR #<number> — gather action items",
  description="Fetch PR thread, linked issues, and/or review report; classify all comments into ACTION_ITEMS",
  activeForm="Gathering action items for PR #<number>"
)
```

Mark `in_progress` immediately:

```text
TaskUpdate(task_id=<task_id_from_above>, status="in_progress")
```

## Step 3a: Report intelligence (report mode only)

*Skip to Step 3b (PR intelligence) when in pr mode or pr + report mode.*

When mode == **report**:

### Sources confirmation

Print before parsing findings:

```markdown
## Resolve — sources

Mode   : report
PR     : #<N>  (extracted from report header, or "n/a — working on current branch")
GitHub : not fetched
Report : Read <path to report file>

Building action items…
```

Read report. Parse findings from each `###` header (`### [blocking] Critical`, `### Architecture & Quality`, `### Test Coverage Gaps`, `### Performance Concerns`, `### Documentation Gaps`, `### Static Analysis`, `### API Design`, `### Codex Co-Review`). Skip `### OSS Checks`, `### Recommended Next Steps`, `### Review Confidence`, `### Issue Root Cause Alignment`.

Map each finding to action item schema:

| Severity in report | `type` |
| --- | --- |
| CRITICAL or `[blocking]` | `[req]` |
| HIGH | `[req]` |
| MEDIUM | `[suggest]` |
| LOW | `[suggest]` (omit if total items > 10) |

- `author`: section owner agent (e.g., `foundry:sw-engineer` for Architecture, `foundry:qa-specialist` for Test Coverage)
- `file`/`line`: extract from `file:line` notation; blank if absent
- `full_comment_text`: full finding bullet
- All items get `[report]` prefix on `type` (e.g., `[report][req]`, `[report][suggest]`)

PR# found in report header → set `$ARGUMENTS = <N>`, go to Step 4; skip Step 3b entirely. After checkout, skip to Step 8 with report-derived action items.

No PR# in header → skip Steps 3b and 4; work on current branch as-is. Skip to Step 8 with report-derived action items.

## Step 3b: PR intelligence

Fetch full PR metadata in one call:

```bash
gh pr view <PR_NUMBER> \
    --json number,title,body,author,labels,isDraft,state,headRefName,baseRefName,headRepositoryOwner,headRepository,isCrossRepository,url,closingIssuesReferences
```

Extract and record:

- `HEAD_REF` — source branch name (`.headRefName`)
- `BASE_REF` — target branch name (`.baseRefName`, e.g. `main`, `develop`)
- `PR_AUTHOR` — contributor's GitHub login (`.author.login`)
- `HEAD_REPO_OWNER` — owner of fork/head repo (`.headRepositoryOwner.login`)
- `BASE_REPO_OWNER` — owner of base repo; from `.url` via `split("/")[3]` or `gh repo view --json owner -q .owner.login`
- `IS_FORK` — `.isCrossRepository` (`true` = fork PR, `false` = same-repo branch)
- `CLOSING_ISSUES` — linked issue numbers (`.closingIssuesReferences[].number`)

Fetch full discussion:

```bash
gh pr view <PR_NUMBER> --comments                        # PR-level comments + timeline
gh api repos/{owner}/{repo}/pulls/<PR_NUMBER>/reviews  # formal reviews (Approve / Request Changes)
gh api repos/{owner}/{repo}/pulls/<PR_NUMBER>/comments # inline code comments with file + line
```

Non-empty `CLOSING_ISSUES` → fetch each linked issue:

```bash
gh issue view <ISSUE_NUMBER> --json title,body
```

### Synthesize contribution motivation

Read PR title, body, linked issues, commits. Produce 2–3 sentence paragraph:

- What problem/gap contributor is solving (linked issues or PR description)
- Why they chose this approach (PR body, design notes in commits)
- Expected user-visible outcome

Motivation = **priority lens for conflict resolution** in Step 7 — whose logic wins when both sides touched same area.

### Classify action items

Read every comment, review, inline code comment. Classify:

| Code | Meaning |
| --- | --- |
| `[req]` | Change **required** before merge — requested by a reviewer with write access or the maintainer |
| `[suggest]` | Improvement suggested — nice-to-have, non-blocking |
| `[question]` | Open question that needs an answer before deciding what code to write |
| `[done]` | A subsequent commit or reply already addressed this — skip |
| `[info]` | Praise, acknowledgement, emoji-only — skip |
| `[self-review]` | Finding from the `/oss:review` report — not a GitHub commenter; author = agent name |

Build `ACTION_ITEMS`: `[{id, type, author, summary, file, line, full_comment_text}]`

### Sources confirmation

Print right before action item table:

```markdown
## Resolve — sources

Mode   : pr
PR     : #<N>
GitHub : Read — PR body · <N> comments · <N> reviews · <N> inline code comments
Report : not used

Building action items…
```

Print action item table:

```markdown
### Action Items — PR #<number>

| Type | Author | Status | Summary | File:Line |
|------|--------|--------|---------|-----------|
| [req] | @reviewer | pending | rename param `x` to `count` | src/foo.py:42 |
| [suggest] | @maintainer | pending | add docstring | — |
| [question] | @reviewer | pending | why not use X instead? | — |
```

> **Guard**: `[req]` items > 15 → print list, `AskUserQuestion` for subset (up to 4 grouped options, first = "(Recommended)") before continuing.

Answer `[question]` items resolvable from code — clear answer → reclassify `[req]`/`[suggest]`; maintainer judgement needed → surface and pause. Contributor answer ≠ auto-close — answer revealing known limitation/deferred work → keep `[question]`, surface for maintainer to accept/reject.

## Step 3c: Merge report findings (pr + report mode only)

*Skip when in pr mode.*

When mode == **pr + report**:

Find + read latest review report (`ls -t .temp/output-review-*.md 2>/dev/null | head -1`). Parse findings same as Step 3a.

**Deduplication**:

- Report finding matches GitHub item at same `file:line` → drop report item; annotate GitHub item with `(also flagged by /review)`
- Semantic match (same file, no exact line, similar description) → drop report item; same annotation
- No match → append report finding as `[report]` item

**Re-prefix GitHub items** once deduplication done: `[req]` → `[gh][req]`, `[suggest]` → `[gh][suggest]`, `[question]` → `[gh][question]`. Only in `pr + report` mode; single-source `pr` items stay plain.

### Sources confirmation

Print right before merge summary and action item table:

```markdown
## Resolve — sources

Mode   : pr + report
PR     : #<N>
GitHub : Read — PR body · <N> comments · <N> reviews · <N> inline code comments
Report : Read <path to report file>

Building action items…
```

Result: single merged `ACTION_ITEMS`. GitHub items first (`[gh][req]`/`[gh][suggest]`), then `[report]` items. Print merge summary before table:

```text
Report merged: <N> findings from /review · <M> deduplicated against GitHub comments · <K> added as [report] items
```

## Step 3d: Create per-item tasks

Mark Step 2 task `completed`:

```text
TaskUpdate(task_id=<step2_task_id>, status="completed")
```

For each item in `ACTION_ITEMS` create task:

```text
TaskCreate(
  subject="[<type>] <summary> — PR #<number>",
  description="Author: @<author> | File: <file:line or '—'> | <full_comment_text>",
  activeForm="Implementing: <summary>"
)
```

- `<type>` — item's type code: `req`, `suggest`, `question`, `done`, `info`, `self-review`, `report-req`, `report-suggest`, etc.
- `<summary>` — item's `summary` field (truncate to 80 chars if needed)
- Skip `[done]`/`[info]` items — no task needed.

Store returned task ID in each `ACTION_ITEMS` entry as `task_id`.

## Step 4: Checkout PR branch

```bash
SAVED_BRANCH=$(git rev-parse --abbrev-ref HEAD)  # timeout: 3000
gh pr checkout <PR#>   # fetches HEAD_REF; for forks, adds the contributor's remote + sets up tracking  # timeout: 15000
```

`gh pr checkout` auto-handles forks — adds contributor's remote, configures tracking. Verify:

```bash
git remote -v | grep -v fetch | grep -v push | head -10 # timeout: 3000
git status                                              # confirm we are on HEAD_REF  # timeout: 3000
```

`FORK_REMOTE`: contributor login (e.g. `alice`) for forks, `origin` for same-repo. Push always `git push` — tracking configured by `gh pr checkout`.

## Step 5: Conflict detection

```bash
# Detect in-progress merge via MERGE_HEAD sentinel — git status --porcelain does not expose this reliably
MERGE_HEAD_FILE="$(git rev-parse --git-dir)/MERGE_HEAD" # timeout: 3000
test -f "$MERGE_HEAD_FILE" && echo "MERGING" || echo "clean"
```

**Case A — MERGING** (`MERGE_HEAD` present — prior `git merge` left markers): work with existing markers. Skip to Step 6, substep 6c.

**Case B — not MERGING**:

Merge `BASE_REF` into PR branch (BASE → HEAD_REF, not reverse):

```bash
git fetch origin "$BASE_REF"                     # ensure origin/$BASE_REF is current  # timeout: 6000
git merge "origin/$BASE_REF" --no-commit --no-ff # timeout: 6000
```

Check conflicted files:

```bash
git diff --name-only --diff-filter=U # timeout: 3000
```

### 5a: Create per-conflict tasks

For each conflicted file, create task **before touching any file**:

```text
TaskCreate(
  subject="Resolve conflict: <filepath> — PR #<number>",
  description="Merge conflict in <filepath> from merging origin/<BASE_REF> into <HEAD_REF>. Must be completed before action-item implementation begins.",
  activeForm="Resolving conflict: <filepath>"
)
```

Store returned task ID alongside each file path as `conflict_task_id`. Print conflict task table:

```markdown
### Merge Conflicts — PR #<number>

| File | Task | Status |
|------|------|--------|
| src/foo.py | #<task_id> | pending |
| config.yaml | #<task_id> | pending |
```

> **Invariant**: all conflict tasks `completed` before Step 8. Upfront creation keeps each conflict scoped and independently reversible.

No conflicts → complete merge, skip to Step 8:

```bash
git merge --continue --no-edit
```

Report clean merge, skip Steps 6–7, continue Step 8.

More than 20 conflicted files → abort and stop:

```bash
git merge --abort
```

Report count + file list; `AskUserQuestion` with options: "Continue (Recommended)" (all conflicted files), "Re-scope" (abort, narrow merge target).

## Step 6: Distill conflict context

Run before touching any conflict markers.

### 6a: Source-branch intent

Use Step 3b motivation as primary lens. Additionally:

```bash
MERGE_BASE=$(git merge-base "origin/$BASE_REF" "$HEAD_REF") # timeout: 3000
git log $MERGE_BASE..$HEAD_REF --oneline --no-merges        # timeout: 3000
git diff $MERGE_BASE $HEAD_REF --stat                       # timeout: 3000
```

One-sentence summary: which files/modules PR owns and what it changes.

### 6b: Target-branch drift (the "surprises")

```bash
git log $MERGE_BASE..origin/$BASE_REF --oneline --no-merges    # timeout: 3000
SOURCE_LAST_TIME=$(git log "$HEAD_REF" -1 --format="%ci")      # timeout: 3000
git log origin/$BASE_REF --after="$SOURCE_LAST_TIME" --oneline # commits the contributor never saw  # timeout: 3000
```

One-sentence summary: independent base changes after contributor's last commit — preserve unconditionally.

## Step 7: Resolve per conflicted file

Delegate per-file conflict edits to `foundry:sw-engineer`. Build spawn prompt from all three context sources, check result before completing merge.

### 7a: Spawn sw-engineer

Spawn `foundry:sw-engineer` (fill brackets from indicated steps):

```markdown
Agent(subagent_type="foundry:sw-engineer", prompt="
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

> **Health monitoring**: synchronous; Claude awaits natively. No response ~15 min → surface partial results ⏱, proceed with staged files.

### 7b: Verify and complete merge

Parse JSON from sw-engineer. Check `resolved == staged` — mismatch = file resolved but not staged → surface before proceeding.

Complete merge:

```bash
git merge --continue --no-edit # timeout: 3000
```

Print conflict report:

```markdown
### Conflict Resolution

| File | Strategy | Notes |
|------|----------|-------|
| src/foo.py | Blended | kept PR's new param, adopted base's renamed import |
| config.yaml | Target | unrelated config change from base, PR had no opinion |

**Result**: N files resolved. Merge commit created.
```

Mark all conflict tasks completed:

```text
for each (filepath, conflict_task_id) pair from Step 5a: TaskUpdate(task_id=\<conflict_task_id>, status="completed")
```

## Step 8: Implement action items

Authorize commits for this workflow:

```bash
touch /tmp/claude-commit-authorized  # timeout: 3000
```

If `CODEX_AVAILABLE=false`: mark all items `⚠ skipped — codex not installed`, skip to Step 9.

> **Conflict gate**: verify all Step 5a conflict tasks `completed` before any action item. Still `pending`/`in_progress` → stop, surface list, wait. Items on unresolved conflicts compound diff.

Process `[req]` items first, then `[suggest]`. **Each item gets its own commit.**

> **Guard**: process at most 10 items, then pause and `AskUserQuestion` to continue with remaining items, with options: "Continue (Recommended)" (next batch), "Stop here" (report with items processed so far).

For each action item:

```bash
# Guard: ensure clean state before each item
test -z "$(git status --porcelain)" || { echo "⚠ dirty tree before item #<id> — stashing"; git stash push -m "resolve-pre-item-<id>"; }  # timeout: 3000

# Snapshot before
git diff HEAD --stat  # timeout: 3000
```

Mark item's task in_progress:

```text
TaskUpdate(task_id=<item.task_id>, status="in_progress")
```

```bash
# Dispatch to Codex
Agent(subagent_type="codex:codex-rescue", prompt="Apply this review feedback to the codebase. Implement exactly what is requested and nothing more. If the change is already present or there is nothing actionable, make no changes and explain why. Feedback from @<author>: <full_comment_text>")

# Check whether code changed
git diff HEAD --stat  # timeout: 3000
```

Code changed → commit:

```bash
# Prerequisite: working tree must be clean before Step 7 Codex calls; verify with git diff --stat HEAD before proceeding.
# Stage tracked modifications + new files from Codex (never git add -A)
git add $(git diff HEAD --name-only)                                                     # timeout: 3000
git ls-files --others --exclude-standard | grep . | xargs git add -- 2>/dev/null || true # grep . filters empty output (macOS-portable; xargs -r is GNU-only); permission matcher sees 'git ls-files' as first token  # timeout: 3000
# timeout: 3000 — git commit (local operation); include co-author trailer per git-commit.md
git commit -m "$(
	cat <<'EOF'
<imperative short summary of the change>

[resolve #<item_id>] Review comment by @<author> (PR #<PR_NUMBER>):
"<first 72 chars of full_comment_text>..."

---
Co-authored-by: Claude Code <noreply@anthropic.com>
Co-authored-by: OpenAI Codex <codex@openai.com>
EOF
)"
```

No code changed (already done or non-actionable) → record Codex's reason; do NOT create empty commit.

Record per-item: `committed <SHA>` or `skipped — <Codex reason>`.

Mark item's task completed:

```text
TaskUpdate(task_id=<item.task_id>, status="completed")
```

## Step 9: Lint and QA gate

```bash
RUN_DIR=".reports/resolve/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR" # timeout: 5000
```

Spawn both in parallel:

```text
Agent(subagent_type="foundry:linting-expert", prompt="Review all files changed in the current branch since origin/<BASE_REF>. List every lint/type violation. Apply inline fixes for any that are auto-fixable. Write your full findings to $RUN_DIR/linting-expert-step9.md using the Write tool, then return ONLY a compact JSON envelope: {fixed: N, remaining: N, files: [...]}."

Agent(subagent_type="foundry:qa-specialist", maxTurns=15, prompt="Review all files changed in the current branch since origin/<BASE_REF> for correctness, edge cases, and regressions. Flag any blocking issues (bugs, broken contracts, missing test coverage for the changed logic). Write your full findings to $RUN_DIR/qa-specialist-step9.md using the Write tool, then return ONLY a compact JSON envelope: {blocking: N, warnings: N, issues: [...]}."
```

> **Health monitoring**: synchronous. No response ~15 min → surface partial results from `$RUN_DIR` ⏱.

Wait for both. Then:

- `linting-expert` made file changes → commit:

```bash
git add $(git diff HEAD --name-only)                          # timeout: 3000
git commit -m "$(cat <<'EOF'
lint: auto-fix violations after resolve cycle

---
Co-authored-by: Claude Code <noreply@anthropic.com>
EOF
)"  # timeout: 3000
```

- Blocking issues from `foundry:qa-specialist` → fix (via Codex or inline edit), re-run qa-specialist once to confirm; issues after one pass → surface in report, continue (no infinite loop)
- Warnings (non-blocking) → record in report; do not block push

Revoke commit authorization:

```bash
rm -f /tmp/claude-commit-authorized  # timeout: 3000
```

## Step 10: Push

*Skip when report mode with no PR# (`$FORK_REMOTE`, `$HEAD_REF`, `$BASE_REF` unset — no fork branch; workflow ends at Step 11).*

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

Push rejected (fork protection or stale tracking):

```bash
git push "$FORK_REMOTE" HEAD:"$HEAD_REF" # timeout: 30000
```

Verify push reached GitHub:

```bash
gh pr view <PR_NUMBER> --json headRefOid,commits --jq '.commits[-3:] | .[].messageHeadline' # timeout: 6000
```

Confirm latest commit headlines match what was just committed.

## Step 11: Final report

Mark remaining open tasks `completed`. Per-item tasks should be done by Step 8; this closes items skipped (guard paused, question items, codex-not-available).

Then print:

```markdown
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
<linting-expert summary: N fixes applied | or "no violations"> / <foundry:qa-specialist summary: N blocking fixed, N warnings | or "clean">

### Push
✓ Pushed to <remote>/<HEAD_REF> — N new commits

**Next**:
- `gh pr merge <PR#> --merge` to merge now (preserves all commits)

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. conflict strategy ambiguity, action items skipped at guard, Codex partial completion]
**Refinements**: N passes. — omit if 0 passes
```

______________________________________________________________________

## Step 12: Comment dispatch + Codex review loop

Read and execute `plugins/oss/skills/resolve/modes/comment-dispatch.md`.

</workflow>

<notes>

- **Pre-flight git pull** — Step 1 fetches remote tracking ref, pulls if ahead; 1-local/1-remote divergence merges clean; `git pull` conflicts → exit with message to resolve manually — prevents `git merge --continue` with no in-progress merge
- **Branch safety** — `gh pr checkout <PR#>` always lands on PR's HEAD, never `main`/`master`. Never push to default branch — if PR branch = default branch, abort and surface.
- **OSS fork support** — `gh pr checkout <PR#>` works same for branches + forks; forks get contributor remote + tracking; plain `git push` targets fork branch automatically.
- **Merge direction** — `origin/BASE_REF` INTO `HEAD_REF` (not reverse); PR branch = source of truth; maintainer still clicks Merge.
- **Never rebase** — use `git merge`; rebase rewrites SHAs, breaks cherry-pick/revert; Step 5 uses `git merge --continue --no-edit`.
- **Contribution motivation before code** — Step 3a before any file read/edit; provides "whose intent wins" lens; PR body + linked issues reveal constraints invisible in git diff.
- **Separate commits per action item** — each `[req]`/`[suggest]` = one atomic commit; `[resolve #N]` tag = `git log --grep` findable; history reviewable, diff bisectable, changes independently revertable; no empty commits.
- **`[question]` items** — answer inline in resolve report only (never post to PR); reclassify before implementing; never silently implement unanswered question.
- **Case A (already MERGING)** — prior `git merge` left markers → skip Steps 5 detection + 6 context-distill, jump to Step 7a; no new merge.
- **Push verification** — confirm via `gh pr view --json commits` before reporting success; exit 0 from `git push` necessary but not sufficient (branch protection can silently reject).
- **`gh pr merge` flags**: `--merge` = preserves all commits; `--squash` = collapses (loses action-item commits); never `--rebase` (rewrites SHAs); default `--merge`.
- **Escape hatch**: `git merge --abort` = undo all conflict state; `git push --force-with-lease` (never plain `--force`) only when user explicitly requests — if push rejected after local amend.
- **Codex agent health**: subject to CLAUDE.md §8 — 15-min cutoff, ⏱ on timeout; partial results via `tail -100` on output file.
- **Worktree cleanup safety net**: `SessionEnd` runs `git worktree prune` — catches orphaned worktrees.
- **Mode routing**: see Steps 3a–3c and `<inputs>` for mode definitions, source routing, action-item derivation.
- **`[gh]` items** (pr + report mode only): commit messages use: `[resolve #<id>] @<reviewer> (gh):` — same as plain `[req]`/`[suggest]` in pr mode, plus `(gh)` source annotation.
- **`[report]` items**: attribute to agent, not GitHub commenter — distinguishes automated findings in git history. Format: `[resolve #<id>] /review finding by <agent-name> (report: <report-path>):`
- **Sources block**: print after mode resolution, before GitHub API calls — "abort if wrong source" moment.
- **Step 7 delegation** — resolve owns orchestration + context; sw-engineer owns code-level resolution (Read → Edit → stage); resolve retains conflict report + `git merge --continue`.
- Follow-up chains:
  - After push → never approve/comment on PR; maintainer reviews + clicks Merge.
  - Unanswered `[question]` items → record in resolve report only; do NOT post to PR.
  - After merge → linked issues close if PR body has `Closes #<issue#>`/`Fixes #<issue#>`; if `CLOSING_ISSUES` found in Step 3b but body lacks keywords, add: `gh pr edit <PR#> --body "$(gh pr view <PR#> --json body -q .body)\n\nCloses #<issue#>"`

</notes>
