---
name: resolve
description: "OSS maintainer fast-close workflow for GitHub PRs. Three phases: (1) PR intelligence — reads the full thread, linked issues, and PR body to synthesize contribution motivation and classify every comment into action items; (2) conflict resolution — checks out the PR branch (fork-aware via gh pr checkout), merges BASE into it, and resolves conflicts semantically using the contributor's intent as the priority lens; (3) implements each action item as a separate attributed commit via Codex, then pushes back to the contributor's fork. Also accepts bare comment text for single-comment dispatch."
argument-hint: <PR number or URL> | <review comment text>
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Grep, Glob, TaskCreate, TaskUpdate
---

<objective>

OSS maintainer fast-close workflow. Given a PR number, three phases fire automatically:

1. **PR intelligence** — synthesize contribution motivation from the PR body, linked issues, and full discussion thread; classify every comment into action items
2. **Conflict resolution** — check out the PR branch (fork-aware), merge `BASE_REF` into it, resolve conflicts semantically with the contributor's intent as the priority lens
3. **Action item implementation** — implement each action item as a separate commit attributed to the review comment, then push back to the contributor's fork

The result: a conflict-free, review-addressed PR branch pushed to the fork, ready for the maintainer to merge on GitHub — all without touching the GitHub UI.

**Core invariant — transparent and reversible**: every action produces a visible, named git object (merge commit, fix commit) that can be inspected and reverted individually. This is why all conflict resolution goes forward via `git merge` (creates a new commit with two parents) and never via `git rebase` (rewrites SHA history, destroys the ability to revert or cherry-pick individual steps). Each action item becomes its own commit for the same reason — granular revert is always possible.

When given bare comment text, skip straight to Codex dispatch (Step 10).

</objective>

<inputs>

- **$ARGUMENTS**: a PR number (e.g. `42`), a GitHub PR URL, or bare review comment text

</inputs>

<workflow>

## Step 1: Pre-flight

```bash
# From _shared/preflight-helpers.md — TTL 4 hours, keyed per binary
preflight_ok()  { local f=".claude/state/preflight/$1.ok"; [ -f "$f" ] && [ $(( $(date +%s) - $(cat "$f") )) -lt 14400 ]; }
preflight_pass(){ mkdir -p .claude/state/preflight; date +%s > ".claude/state/preflight/$1.ok"; }

# codex — optional; intelligence + conflict resolution work without it
if preflight_ok codex; then
  echo "codex: ok (cached)"
elif which codex &>/dev/null; then
  preflight_pass codex && echo "codex: ok"
else
  echo "codex: missing — action item implementation (Step 8) will be skipped"
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
git remote -v
```

<!-- codex package: npm show @openai/codex version to check latest before pinning -->

If gh is missing or not authenticated: stop (error printed above)

If codex is missing: set `CODEX_AVAILABLE=false` and continue — Steps 3–7 (intelligence + conflict resolution) work without Codex; Step 8 (action items) will be skipped with a notice: `⚠ codex not found — skipping action items. Install: npm install -g @openai/codex`

Parse $ARGUMENTS:

- If it is a number or matches a GitHub PR URL pattern → **PR mode** (continue from Step 2)
- Otherwise → **comment dispatch mode** (jump to Step 10)

## Step 2: Create task

```
TaskCreate(
  subject="Resolve PR #<number>",
  description="OSS fast-close: intelligence → conflicts → action items for PR #<number>",
  activeForm="Resolving PR #<number>"
)
```

Mark it `in_progress` immediately.

## Step 3: PR intelligence

Fetch full PR metadata in one call:

```bash
gh pr view <PR#> --json \
  number,title,body,author,labels,isDraft,state,\
  headRefName,baseRefName,\
  headRepositoryOwner,headRepository,baseRepository,\
  closingIssuesReferences
```

Extract and record:

- `HEAD_REF` — source branch name
- `BASE_REF` — target branch name (e.g. `main`, `develop`)
- `PR_AUTHOR` — contributor's GitHub login
- `HEAD_REPO_OWNER` — owner of the head (PR) repository
- `BASE_REPO_OWNER` — owner of the base repository
- `IS_FORK` — `true` when `HEAD_REPO_OWNER != BASE_REPO_OWNER`
- `CLOSING_ISSUES` — list of linked issue numbers

Fetch the full discussion:

```bash
gh pr view <PR#> --comments                                          # PR-level comments + timeline
gh api repos/{owner}/{repo}/pulls/<PR#>/reviews                      # formal reviews (Approve / Request Changes)
gh api repos/{owner}/{repo}/pulls/<PR#>/comments                     # inline code comments with file + line
```

If `CLOSING_ISSUES` is non-empty, fetch each linked issue for motivation context:

```bash
gh issue view <issue#> --json title,body
```

### 3a: Synthesize contribution motivation

Read the PR title, PR body, linked issue descriptions, and commit messages together. Produce a 2–3 sentence paragraph:

- What problem or gap the contributor is solving (from linked issues or PR description)
- Why they chose this particular approach (from the PR body, any design notes in commits)
- What the expected user-visible outcome is

This motivation summary is the **priority lens for conflict resolution** in Step 7 — it tells you whose logic should win when both sides touched the same area.

### 3b: Classify action items

Read every comment, review, and inline code comment. Classify each:

| Code         | Meaning                                                                                        |
| ------------ | ---------------------------------------------------------------------------------------------- |
| `[req]`      | Change **required** before merge — requested by a reviewer with write access or the maintainer |
| `[suggest]`  | Improvement suggested — nice-to-have, non-blocking                                             |
| `[question]` | Open question that needs an answer before deciding what code to write                          |
| `[done]`     | A subsequent commit or reply already addressed this — skip                                     |
| `[info]`     | Praise, acknowledgement, emoji-only — skip                                                     |

Build `ACTION_ITEMS`: `[{id, type, author, summary, file, line, full_comment_text}]`

Print the action item table:

```
### Action Items — PR #<number>

| # | Type | Author | Summary | File:Line |
|---|------|--------|---------|-----------|
| 1 | [req] | @reviewer | rename param `x` to `count` | src/foo.py:42 |
| 2 | [suggest] | @maintainer | add docstring | — |
| 3 | [question] | @reviewer | why not use X instead? | — |
```

> **Guard**: if `[req]` items > 15, print the full list and ask the user which subset to implement before continuing.

Answer any `[question]` items that can be resolved from reading the code — if the answer is clear, reclassify to `[req]` or `[suggest]`; if it requires maintainer judgement, surface and pause. A question answered by the **contributor** (not the maintainer) is not automatically closed — if the contributor's answer reveals a known limitation or deferred work (e.g., "currently per-process, Redis is a follow-up"), keep it as `[question]` and surface it for the maintainer to explicitly accept or reject before proceeding.

## Step 4: Checkout PR branch

```bash
SAVED_BRANCH=$(git rev-parse --abbrev-ref HEAD)
gh pr checkout <PR#>   # fetches HEAD_REF; for forks, adds the contributor's remote + sets up tracking
```

`gh pr checkout` handles forks automatically — it adds a remote named after the contributor's GitHub login and configures tracking. Verify:

```bash
git remote -v | grep -v fetch | grep -v push | head -10
git status  # confirm we are on HEAD_REF
```

Record `FORK_REMOTE`: for fork PRs it is the contributor's login (e.g. `alice`); for same-repo PRs it is `origin`. The push command in Step 9 is always `git push` (tracking is configured correctly by `gh pr checkout`).

## Step 5: Conflict detection

```bash
# Detect in-progress merge via MERGE_HEAD sentinel — git status --porcelain does not expose this reliably
MERGE_HEAD_FILE="$(git rev-parse --git-dir)/MERGE_HEAD"
test -f "$MERGE_HEAD_FILE" && echo "MERGING" || echo "clean"
```

**Case A — MERGING state** (`MERGE_HEAD` present — a previous `git merge` left markers in the PR branch):

Work directly with the existing markers. Skip to Step 6, substep 6c.

**Case B — not MERGING**:

Merge `BASE_REF` into the PR branch (this updates the PR with the latest base changes — the merge direction is BASE → HEAD_REF, not the reverse):

```bash
git fetch origin "$BASE_REF"          # ensure origin/$BASE_REF is current
git merge "origin/$BASE_REF" --no-commit --no-ff
```

Check for conflicted files:

```bash
git diff --name-only --diff-filter=U
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

Report the count and file list; ask the user whether to continue or re-scope.

## Step 6: Distill conflict context

Run before touching any conflict markers.

### 6a: Source-branch intent

Use the contribution motivation from Step 3a as the primary lens. Additionally:

```bash
MERGE_BASE=$(git merge-base "origin/$BASE_REF" "$HEAD_REF")
git log $MERGE_BASE..$HEAD_REF --oneline --no-merges
git diff $MERGE_BASE $HEAD_REF --stat
```

One-sentence summary: which files/modules this PR owns and what it changes about them.

### 6b: Target-branch drift (the "surprises")

```bash
git log $MERGE_BASE..origin/$BASE_REF --oneline --no-merges
SOURCE_LAST_TIME=$(git log "$HEAD_REF" -1 --format="%ci")
git log origin/$BASE_REF --after="$SOURCE_LAST_TIME" --oneline    # commits the contributor never saw
```

One-sentence summary: what independent changes landed on base after the contributor's last commit — these must be preserved unconditionally.

## Step 7: Resolve per conflicted file

For each conflicted file (work in the current PR branch checkout):

a. **Read** the file — examine `<<<<<<<`, `=======`, `>>>>>>>` markers and surrounding context

b. **Determine resolution** using the contribution motivation (Step 3a) and drift (Step 6b):

- Contributor's new functionality takes priority for files the PR owns (introduced or substantially rewrote)
- Base's independent refactors and config updates are always preserved
- When both sides changed the same logic, blend: keep the PR's semantic change while incorporating the base's structural update

c. **Edit** to remove all conflict markers and produce the correct resolved content

d. **Stage**:

```bash
git add <file>
```

### 7e: Complete the merge

```bash
git merge --continue --no-edit
```

Print interim conflict report:

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

> **Guard**: process at most 10 items, then pause and ask the user whether to continue with the remaining items.

For each action item:

```bash
# Snapshot before
git diff HEAD --stat

# Dispatch to Codex
codex exec "Apply this review feedback to the codebase. Implement exactly what is requested and nothing more. If the change is already present or there is nothing actionable, make no changes and explain why. Feedback from @<author>: <full_comment_text>" --sandbox workspace-write

# Check whether code changed
git diff HEAD --stat
```

If code changed → commit:

```bash
git add -A
git commit -m "$(cat <<'EOF'
<imperative short summary of the change>

Addresses review comment by @<author> (PR #<PR_NUMBER>)
<optional: 1-line note on approach if non-obvious>
EOF
)"
```

If no code changed (already done or non-actionable) → record Codex's reason; do NOT create an empty commit.

Record per-item: `committed <SHA>` or `skipped — <Codex reason>`.

## Step 9: Push

```bash
git push
# gh pr checkout configured tracking to the fork branch — git push targets it automatically
```

If push is rejected (fork protection or stale tracking):

```bash
git push "$FORK_REMOTE" HEAD:"$HEAD_REF"
```

Verify the push reached GitHub:

```bash
gh pr view <PR#> --json headRefOid,commits --jq '.commits[-3:] | .[].messageHeadline'
```

Confirm the latest commit headlines match what was just committed.

## Step 9b: Final report

Mark the task `completed`, then print:

```
## Resolve Report — PR #<number>

### Contribution
<2–3 sentence motivation summary from Step 3a>

### Conflicts
<conflict table from Step 7, or "No conflicts detected">

### Action Items

| # | Type | Summary | Result | Commit |
|---|------|---------|--------|--------|
| 1 | [req] | rename param x → count | ✓ committed | abc1234 |
| 2 | [suggest] | add docstring | ✓ committed | def5678 |
| 3 | [question] | why not use X? | ⊘ answered inline — existing approach is correct per linked issue #42 | — |

### Push
✓ Pushed to <remote>/<HEAD_REF> — N new commits

**Next**:
- `gh pr merge <PR#> --merge` to merge now (preserves all commits)
- `gh pr review <PR#> --approve` to approve first
- Reply to any `[question]` items with the rationale before merging if the reviewer is external

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. conflict strategy ambiguity, action items skipped at guard, Codex partial completion]
```

______________________________________________________________________

## Step 10: Comment dispatch + Codex review loop

Reached when $ARGUMENTS is bare comment text (not a PR number or URL).

Create a task:

```
TaskCreate(
  subject="Resolve: <60-char summary of $ARGUMENTS>",
  description="<full $ARGUMENTS>",
  activeForm="Resolving comment"
)
```

If `CODEX_AVAILABLE=false`: stop with `⚠ codex not found — install: npm install -g @openai/codex` and mark the task completed.

### 10a: Dispatch

```bash
codex exec "Apply this review comment to the codebase. If the change is already present, or the comment has no actionable code change, make no changes and briefly explain why. Comment: $ARGUMENTS" --sandbox workspace-write
```

Record the initial dispatch outcome (code changed or no change + reason).

### 10b: Codex review loop (max 5 passes)

```bash
git diff HEAD --stat  # confirm there are changes to review
```

If no changes: skip the loop; set `CODEX_REVIEW_FINDINGS=""`.

Otherwise:

```
for REVIEW_PASS in 1..5:

  # Review phase
  codex exec "Review all changes in git diff HEAD. List every non-cosmetic issue (bug, logic error, regression, missed edge case) as a numbered list. Do NOT list cosmetic nits. End with: ISSUES_FOUND=<count>." --sandbox workspace-write

  if ISSUES_FOUND == 0: break

  # Fix phase
  for each issue in the list:
    codex exec "Apply this fix to the codebase: <issue description>" --sandbox workspace-write

if REVIEW_PASS reached 5 and ISSUES_FOUND > 0:
  note "⚠ Review loop hit 5-pass cap — N issues remain; surface to user"
```

Mark the task `completed`, then print:

```
## Resolve Report

**Verdict**: ✓ resolved | ⊘ no change — <Codex's reason>

### Codex Review
<findings across passes, or "No issues found" / "Skipped — no changes">

**Next**: review diff and commit | reply to reviewer with Codex's explanation

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. Codex partial completion, ambiguous comment intent]
```

</workflow>

<notes>

- **OSS fork support** — `gh pr checkout <PR#>` works identically for same-repo branches and forks; for forks it adds the contributor's remote (named after their login) and configures tracking; plain `git push` then targets the fork branch correctly with no manual remote setup
- **Merge direction** — the skill merges `origin/BASE_REF` INTO `HEAD_REF` (updating the PR branch), NOT the reverse; this preserves the PR branch as the source of truth and keeps the GitHub merge clean; the maintainer still clicks Merge (or runs `gh pr merge`) after reviewing
- **Never rebase** — all conflict resolution uses `git merge`; rebase rewrites commit SHAs and breaks cherry-pick / revert; `--ff-only` is used only for clean no-conflict merges
- **Contribution motivation before code** — Step 3a must happen before any file is read or edited; it provides the "whose intent wins" lens for conflict decisions; reading the PR body and linked issues often reveals design constraints that are invisible from git diff alone
- **Separate commits per action item** — each `[req]` and `[suggest]` item becomes its own commit attributing the review comment author; this makes the change history reviewable, the diff bisectable, and each change independently revertable
- **`[question]` items** — answer first (in a PR comment or inline reasoning), then reclassify before implementing; never silently implement a response to an unanswered question
- **Case A (already MERGING)** — if a prior `git merge origin/$BASE_REF` left markers, the skill skips Steps 5 detection and 6 context-distill, jumps directly to Step 7c (resolve per file), uses the existing markers; no new merge is started
- **Push verification** — always confirm via `gh pr view --json commits` that new commits appear on GitHub before reporting success; exit code 0 from `git push` is necessary but not sufficient (branch protection rules can silently reject)
- **`gh pr merge` flags**: `--merge` preserves all commits and history; `--squash` collapses to one (loses individual action-item commits); never suggest `--rebase` (rewrites SHAs); default recommendation is `--merge` unless project convention says otherwise
- **Escape hatch**: `git merge --abort` undoes the entire conflict state and returns the PR branch to pre-merge state; use `git push --force-with-lease` (never plain `--force`) if push is rejected after local amending
- **5-iteration cap** on the Step 10 Codex review loop overrides the global 3-iteration default — skill-declared bounds take precedence (CLAUDE.md §3 "Safety breaks for loops")
- **`codex exec` timeout**: allow up to 2 minutes per call; background health monitoring (CLAUDE.md §8) does not apply because Codex runs sequentially, not as a spawned background agent
- **Worktree cleanup safety net**: `SessionEnd` hook runs `git worktree prune` — catches any orphaned worktrees from prior sessions
- Follow-up chains:
  - After push → `gh pr review <PR#> --approve` if satisfied; for substantial maintainer changes, comment on the PR explaining what was done and why — don't silently push to a contributor's fork
  - For `[question]` items left unanswered → post a PR comment with the rationale before merging; gives the contributor context and closes the thread
  - After merge → linked issues close automatically when the PR is merged, provided the PR body contains `Closes #<issue#>` or `Fixes #<issue#>`; if `CLOSING_ISSUES` were found in Step 3 but the PR body lacks those keywords, add them to the PR description: `gh pr edit <PR#> --body "$(gh pr view <PR#> --json body -q .body)\n\nCloses #<issue#>"`

</notes>
