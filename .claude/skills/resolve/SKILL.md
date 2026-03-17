---
name: resolve
description: 'Given a Pull Request (PR) number, resolves in two phases: (1) automatically detects and semantically resolves merge conflicts — distills source-branch intent and target-branch drift before touching any markers; (2) processes review comments via Codex. Also accepts bare comment text for single-comment dispatch.'
argument-hint: <PR number or URL> | <review comment text>
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Grep, Glob, TaskCreate, TaskUpdate
---

<objective>

Two phases when given a PR number:

1. **Conflict resolution** (if any) — understand what the source branch does and what "surprise" commits landed on the target, then resolve conflict markers with full semantic context rather than textual guessing.
2. **Review comment resolution** — dispatch each actionable comment to Codex and verify code changed.

When given bare comment text, skip straight to Codex dispatch.

</objective>

<inputs>

- **$ARGUMENTS**: a PR number (e.g. `42`), a GitHub PR URL, or bare review comment text

</inputs>

<workflow>

## Step 1: Pre-flight

```bash
which codex
which gh
```

If codex is missing: stop with `Pre-flight failed: codex not found. Install: npm install -g @openai/codex`

<!-- verify at use time — check npm for current @openai/codex package name -->

If gh is missing: stop with `Pre-flight failed: gh not found. Install: brew install gh`

Parse $ARGUMENTS:

- If it is a number or matches a GitHub PR URL pattern → **PR mode** (continue from Step 2)
- Otherwise → **comment dispatch mode** (jump to Step 8)

## Step 2: Create task

```
TaskCreate(
  subject="Resolve PR #<number>",
  description="Conflict check + review comment resolution for PR #<number>",
  activeForm="Resolving PR #<number>"
)
```

Mark it `in_progress` immediately.

## Step 3: Fetch PR metadata

```bash
gh pr view <PR#> --json headRefName,baseRefName,body,comments
```

Extract:

- `HEAD_REF` — the source branch name
- `BASE_REF` — the target branch name (typically `main`)
- `COMMENTS` — the review comment list (saved for Step 7)

## Step 4: Conflict detection

```bash
# Detect MERGING state via MERGE_HEAD sentinel file — git status --porcelain does not expose this reliably
MERGE_HEAD_FILE="$(git rev-parse --git-dir)/MERGE_HEAD"
test -f "$MERGE_HEAD_FILE" && echo "MERGING" || echo "clean"
```

Two cases:

**Case A — MERGING state** (`MERGE_HEAD` file present — previous merge attempt left conflict markers in the main working tree):

- Work directly in the main working tree. Skip to Step 5, using the existing conflict markers.

**Case B — not MERGING** (clean or dirty working tree, no `MERGE_HEAD`):

- Create an isolated worktree in `/tmp` so the main working tree is never touched:

```bash
WORKTREE_DIR="/tmp/resolve-pr-${PR_NUMBER}-$(git rev-parse --short HEAD)"
TEMP_BRANCH="resolve/pr-${PR_NUMBER}-tmp"
git worktree add "$WORKTREE_DIR" -b "$TEMP_BRANCH" HEAD
```

Then attempt the merge inside the worktree:

```bash
cd "$WORKTREE_DIR"
git merge $HEAD_REF --no-commit --no-ff
```

Check for actual conflict files:

```bash
git diff --name-only --diff-filter=U
```

If no conflicts → fast-forward and clean up:

```bash
cd -
git merge "$TEMP_BRANCH" --ff-only
git worktree remove "$WORKTREE_DIR"
git branch -d "$TEMP_BRANCH"
```

Report a clean merge, skip to Step 6.

If more than 20 conflicted files → clean up and stop:

```bash
cd -
git worktree remove --force "$WORKTREE_DIR"
git branch -d "$TEMP_BRANCH"
```

Report the count and file list, ask the user whether to continue or re-scope.

## Step 5: Resolve conflicts

All edits in this step happen inside `$WORKTREE_DIR` (Case B) or the main working tree (Case A).

### 5a: Distill source-branch intent

Find the common ancestor and examine what the PR branch contributed:

```bash
MERGE_BASE=$(git merge-base $BASE_REF $HEAD_REF)
git log $MERGE_BASE..$HEAD_REF --oneline --no-merges
git log $MERGE_BASE..$HEAD_REF --format="%s%n%b" --no-merges
git diff $MERGE_BASE $HEAD_REF --stat
```

Synthesize a 1-paragraph summary: what problem this branch solves, which files/areas it owns, and the author's apparent intent.

### 5b: Distill target-branch drift

Understand what landed on the target that the PR author never saw:

```bash
git log $MERGE_BASE..$BASE_REF --oneline --no-merges
git diff $MERGE_BASE $BASE_REF --stat

SOURCE_LAST_TIME=$(git log $HEAD_REF -1 --format="%ci")
git log $BASE_REF --until="$SOURCE_LAST_TIME" -1 --oneline   # contemporary state
git log $BASE_REF --after="$SOURCE_LAST_TIME" --oneline       # "surprise" commits
```

Synthesize: what changed on the target after the PR author's last commit — these are the surprises that must be respected.

### 5c: Resolve per conflicted file

For each conflicted file (paths relative to the active working tree):

a. **Read** the file — examine the `<<<<<<<`, `=======`, `>>>>>>>` markers and surrounding context
b. **Determine resolution** using the synthesized context from 5a and 5b:

- Source intent takes priority for files the PR branch owns (introduced or substantially rewrote)
- Target changes are preserved when independent of the PR's work (e.g., unrelated refactors, config updates)
- When both sides changed the same logic, blend: keep the PR's semantic change while incorporating the target's structural update

c. **Edit** the file to remove all conflict markers and produce the correct resolved content
d. **Stage** the file:

```bash
git add <file>
```

### 5d: Complete the merge

**Case B (worktree)**:

```bash
git merge --continue --no-edit   # creates the merge commit (--no-edit uses auto-generated message)
cd -                             # back to main working tree
# Guard: confirm we are on BASE_REF before advancing its pointer
[[ "$(git rev-parse --abbrev-ref HEAD)" == "$BASE_REF" ]] || { echo "Error: main tree is not on $BASE_REF — aborting ff-only"; exit 1; }
git merge "$TEMP_BRANCH" --ff-only   # advance main's pointer; no extra commit
git worktree remove "$WORKTREE_DIR"
git branch -d "$TEMP_BRANCH"
```

**Case A (MERGING state)**:
The merge commit will be created when the user runs `git commit` — do not auto-commit.

## Step 6: Conflict resolution verdict

```bash
# Case B only: merge commit exists — show what the merge brought in
git diff HEAD~1 HEAD --stat
# Case A: no merge commit yet — user must run `git commit` first; then inspect with git diff HEAD~1 HEAD
```

Print an interim report:

```
### Conflict Resolution

| File | Strategy | Notes |
|------|----------|-------|
| ...  | Source / Target / Blended | ... |

**Result**: N files resolved and staged. Clean to proceed.
```

## Step 7: Process review comments

```bash
gh pr view <PR#> --comments
```

Classify each comment thread:

- **Already resolved on GitHub** (`state: RESOLVED`) — include in table with `✓ Already resolved` marker; skip Codex dispatch
- **Non-actionable** ("LGTM", "nice catch", emoji-only) — omit from table entirely
- **Actionable** — dispatch to Codex

> **Guard**: If actionable (unresolved) comments > 10: process the first 10, report the remaining count, and ask the user whether to continue. This prevents runaway execution on large PRs.

For each actionable comment:

```bash
git diff HEAD --stat  # snapshot before
codex exec "Apply this review comment to the codebase. If the change is already present or has no actionable code change, make no changes and briefly explain why. Comment: <comment text>" --sandbox workspace-write
git diff HEAD --stat  # snapshot after
```

Record per-comment: resolved (code changed) or no change + Codex's reason.

Mark the task `completed`, then print:

```
## Resolve Report — PR #<number>

### Conflicts
<conflict table from Step 6, or "No conflicts detected">

### Review Comments

| # | Comment | Status | Action | Result |
|---|---------|--------|--------|--------|
| 1 | <30-char summary> | ✓ Resolved | — | — |
| 2 | <30-char summary> | Open | Applied / No change | <Codex reason if no change> |

**Next**: `git diff HEAD~1 HEAD` for merge changes + `git diff HEAD` for comment changes → commit when satisfied; or `git merge --abort` to undo the merge.

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. conflict strategy ambiguity, unresolved comments, Codex partial completion]
```

______________________________________________________________________

## Step 8: Comment dispatch mode (fast path)

Reached when $ARGUMENTS is bare comment text (not a PR number or URL).

Create a task:

```
TaskCreate(
  subject="Resolve: <60-char summary of $ARGUMENTS>",
  description="<full $ARGUMENTS>",
  activeForm="Resolving comment"
)
```

Snapshot pre-Codex state:

```bash
git diff HEAD --stat
```

Dispatch:

```bash
codex exec "Apply this review comment to the codebase. If the change is already present, or the comment has no actionable code change, make no changes and briefly explain why. Comment: $ARGUMENTS" --sandbox workspace-write
```

Check outcome:

```bash
git diff HEAD --stat
git diff HEAD
```

Mark the task `completed`, then print:

```
## Resolve Report

| # | Comment | Codex Action | Pre-existing |
|---|---------|--------------|--------------|
| 1 | <30-char summary> | <what Codex did or its explanation> | ✓ / ✗ |

**Verdict**: ✓ resolved | ⊘ no change — <Codex's reason>

**Next**: review diff and commit | reply to reviewer: <Codex's reason>

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. Codex partial completion, ambiguous comment intent]
```

</workflow>

<notes>

- **Conflict resolution is automatic** — no flag required; whenever a PR has conflicts, Steps 4–6 fire before review comments are touched
- **Worktree isolation** — the merge runs in `/tmp/resolve-pr-<N>-<hash>` via a transient branch (`resolve/pr-<N>-tmp`); the user's main working tree and uncommitted changes are never touched; the temp branch is deleted on cleanup so there is no lasting pollution
- **Case A (already MERGING)** — if a prior merge attempt left markers, the main working tree is used directly; no worktree is created
- **Escape hatch**: `git merge --abort` (in the worktree or main tree) undoes the entire merge; `git worktree remove --force` + `git branch -d` cleans up if needed
- **Verdict from git state** — `git diff HEAD~1 HEAD --stat` (merge) and `git diff HEAD --stat` (comment changes) are the authoritative signals, not prose output
- **Codex does comment resolution; Claude does conflict resolution** — the two are complementary; Claude has the distilled branch context for conflict decisions that Codex lacks
- **`codex exec` timeout**: each call is a synchronous foreground process — allow up to 2 minutes per comment before considering it stalled. Background health monitoring (CLAUDE.md §8) does not apply here because Codex runs sequentially, not as a spawned background agent
- Follow-up chains:
  - After PR resolve → review `git diff HEAD~1 HEAD` (merge) + `git diff HEAD` (comments), then commit; optionally `/review` for a quality pass
  - Comment no-change → reply to reviewer with Codex's explanation; once clarified, run `/resolve <comment>` again

</notes>
