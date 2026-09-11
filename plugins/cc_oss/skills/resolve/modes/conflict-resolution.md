<!-- oss:resolve Steps 5-7 — executed via: cat $_OSS_RESOLVE/modes/conflict-resolution.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md -->

<!-- Input: PR branch checked out (Step 4 complete), $MERGE_BASE, $HEAD_REF, $BASE_REF, $BASE_REPO_OWNER -->

<!-- Output: conflicts resolved or NO_CONFLICTS_FOUND=true set -->

## Step 5: Conflict detection

```bash
# MERGE_HEAD sentinel — git status --porcelain does not expose in-progress merge reliably
MERGE_HEAD_FILE="$(git rev-parse --git-dir)/MERGE_HEAD" # timeout: 3000
test -f "$MERGE_HEAD_FILE" && echo "MERGING" || echo "clean"
```

**Case A — MERGING** (`MERGE_HEAD` present — prior `git merge` left markers): work with existing markers. Skip to Step 7a.

**Case B — not MERGING**:

Pull latest state for both branches before merging:

```bash
# 1. update source branch (ff-only; non-ff = force-pushed, use local)
git pull "${FORK_REMOTE:-origin}" "$HEAD_REF" --ff-only 2>/dev/null \
    || echo "⚠ PR branch not fast-forwardable — proceeding with local state"  # timeout: 6000
git fetch origin "$BASE_REF" || { echo "⛔ fetch origin/$BASE_REF failed — cannot guarantee base is current; check network/auth and retry"; exit 1; }  # timeout: 6000
# 3. merge — no-commit to inspect conflicts before finalizing
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

> **Invariant**: all conflict tasks `completed` before Step 8. Upfront creation keeps each conflict scoped, independently reversible.

No conflicts → complete merge, skip to Step 8:

```bash
git commit --no-edit # timeout: 6000
```

Report clean merge, skip Steps 6–7, continue Step 8.

⛔ More than 20 conflicted files → abort and stop:

```bash
git merge --abort
```

Report count + file list; `AskUserQuestion` with options:

- (a) "Retry with base only — merge origin/$BASE_REF in batches (manual)" — re-attempt merge in chunks outside this workflow
- (b) "Open PR in browser for manual resolution" — `gh pr view <PR#> --web`
- (c) "Stop — merge aborted" — workflow complete; branch left on $SAVED_BRANCH

## Step 6: Distill conflict context

### 6a: Source-branch intent

Use Step 3b motivation as primary lens. Additionally:

```bash
MERGE_BASE=$(git merge-base "origin/$BASE_REF" "$HEAD_REF") # timeout: 3000
git log $MERGE_BASE..$HEAD_REF --oneline --no-merges        # timeout: 3000
git diff $MERGE_BASE $HEAD_REF --stat                       # timeout: 3000
```

One-sentence summary: which files/modules PR owns, what it changes.

### 6b: Target-branch drift (the "surprises")

```bash
git log $MERGE_BASE..origin/$BASE_REF --oneline --no-merges    # timeout: 3000
SOURCE_LAST_TIME=$(git log "$HEAD_REF" -1 --format="%ci")      # timeout: 3000
git log origin/$BASE_REF --after="$SOURCE_LAST_TIME" --oneline # commits the contributor never saw  # timeout: 3000
```

One-sentence summary: independent base changes after contributor's last commit — preserve unconditionally

## Step 7: Resolve per conflicted file

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

> **Health monitoring**: the spawn runs in the background — spawn, end the turn, resume on the completion notification; no filler call, no "waiting" line, no sleep. Nothing after ~15 min → surface partial results ⏱, proceed with staged files.

### 7b: Verify and complete merge

Parse JSON from sw-engineer. Check `resolved == staged` — mismatch = file resolved but not staged → surface before proceeding.

Verify no conflict markers remain and all resolved files staged:

```bash
STILL_CONFLICTED=$(git diff --name-only --diff-filter=U 2>/dev/null)
[ -z "$STILL_CONFLICTED" ] || { echo "⛔ Unmerged files remain — resolve before continuing: $STILL_CONFLICTED"; exit 1; }  # timeout: 3000
# residual conflict markers in staged content? (--cached = index, not worktree)
git diff --cached --check 2>&1 | grep -qE 'conflict marker' && { echo "⛔ Conflict markers still present in staged files — re-inspect and re-stage"; exit 1; } || true  # timeout: 3000
# only conflicted files — avoid pulling in unrelated tracked changes
RESOLVED_FILES=$(git diff --cached --name-only 2>/dev/null)
[ -n "$RESOLVED_FILES" ] || { echo "⛔ No staged files found — sw-engineer may not have staged resolutions"; exit 1; }
```

Complete merge (editor-safe, produces proper 2-parent merge commit):

```bash
git commit --no-edit # timeout: 6000
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
