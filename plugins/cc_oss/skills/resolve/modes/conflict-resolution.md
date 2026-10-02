<!-- oss:resolve Steps 5-7 — executed via: cat $_OSS_RESOLVE/modes/conflict-resolution.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md -->

<!-- Input: PR branch checked out (Step 4 complete), $MERGE_BASE, $HEAD_REF, $BASE_REF, $BASE_REPO_OWNER -->

<!-- Output: conflicts resolved or NO_CONFLICTS_FOUND=true set -->

> **Three dispatch points, not one pass** (SKILL.md §Run structure). Step 5 runs in Run 1 right after Step 4, in the turn that spawned `INTEL_AGENT` — it needs only the checked-out branch. Steps 6–7a run later in Run 1, dispatched in the same turn as the Step 3d selection question, because Step 6a needs the motivation that agent synthesized. Step 7b is collected at the join opening Run 2, before any item task exists. Execute each part when its point is reached; never run all three back to back.

## Step 5: Conflict detection

> Run 1, beside the intel agent.

```bash
# MERGE_HEAD sentinel — git status --porcelain does not expose in-progress merge reliably
MERGE_HEAD_FILE="$(git rev-parse --git-dir)/MERGE_HEAD" # timeout: 3000
test -f "$MERGE_HEAD_FILE" && echo "MERGING" || echo "clean"
```

**Case A — MERGING** (`MERGE_HEAD` present — prior `git merge` left markers): work with existing markers, but first check the target branch has not moved since that merge started:

```bash
git fetch origin "$BASE_REF" || echo "⚠ fetch origin/$BASE_REF failed — cannot tell whether the target moved"  # timeout: 6000
[ "$(git rev-parse MERGE_HEAD)" = "$(git rev-parse "origin/$BASE_REF" 2>/dev/null)" ] \
    || echo "⚠ origin/$BASE_REF moved since this merge started — finish it, then re-run /oss:resolve to merge the newer target"  # timeout: 3000
```

Skip to Step 7a.

**Case B — not MERGING**:

Pull latest state, both branches, before merging — the source (PR) branch **and** the target branch:

```bash
# 1. update source branch (ff-only; non-ff = force-pushed, use local)
git pull "${FORK_REMOTE:-origin}" "$HEAD_REF" --ff-only 2>/dev/null \
    || echo "⚠ PR branch not fast-forwardable — proceeding with local state"  # timeout: 6000
# 2. update target branch — remote-tracking ref (required), then the local branch (ff-only, best effort)
git fetch origin "$BASE_REF" || { echo "⛔ fetch origin/$BASE_REF failed — cannot guarantee base is current; check network/auth and retry"; exit 1; }  # timeout: 6000
if git show-ref --verify --quiet "refs/heads/$BASE_REF"; then
    git fetch . "origin/$BASE_REF:$BASE_REF" 2>/dev/null \
        || echo "⚠ local $BASE_REF not updated (diverged, or checked out in another worktree) — merging origin/$BASE_REF, which is current"
fi  # timeout: 3000
# 3. merge — no-commit to inspect conflicts before finalizing
git merge "origin/$BASE_REF" --no-commit --no-ff # timeout: 6000
# 4. conflicted files, same call — merge exits 1 on conflict, this line still runs
git diff --name-only --diff-filter=U  # timeout: 3000
```

The merge always uses the freshly fetched `origin/$BASE_REF`, never the local `$BASE_REF`, so a stale or diverged local target branch never leaks into the PR. The local update keeps the maintainer's own target branch in step with what was merged; `git fetch .` is fast-forward only and never touches a branch that is checked out, so it cannot rewrite local work.

The block's last line lists the conflicted files — no second call.

### 5a: Create per-conflict tasks

For each conflicted file, create task **before touching any file** — every file's `TaskCreate` in **one response**, riding with the next real tool call:

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

No conflicts → complete merge here:

```bash
git commit --no-edit # timeout: 6000
```

Report clean merge. Steps 6–7 and the Step 7b join become no-ops — return to Run 1 (await the `INTEL_AGENT` envelope, then Step 3c).

⛔ More than 20 conflicted files → abort and stop:

```bash
git merge --abort
```

Report count + file list; `AskUserQuestion` with options:

- (a) "Retry with base only — merge origin/$BASE_REF in batches (manual)" — re-attempt merge in chunks outside this workflow
- (b) "Open PR in browser for manual resolution" — `gh pr view <PR#> --web`
- (c) "Stop — merge aborted" — workflow complete; branch left on $SAVED_BRANCH

## Step 6: Distill conflict context

> Run 1, dispatched in the Step 3d gate turn — after `INTEL_AGENT` returns, before the `AskUserQuestion` call. Runs through the user's idle window.

### 6a: Source-branch intent

Use Step 3b motivation as primary lens — the 2–3 sentence synthesis `INTEL_AGENT` wrote, where thread consensus outranks the PR body. This is the dependency that keeps Steps 6–7 out of the earlier overlap; never substitute a git-log-only reading of intent for it. Additionally, one call collects both 6a's source-branch intent and 6b's target-branch drift:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell (Check 41) — reload refs Step 4 persisted; bare $BASE_REF/$HEAD_REF are unbound in a new Bash call
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
[ -n "$BASE_REF" ] && [ -n "$HEAD_REF" ] || { echo "⛔ Step 6: BASE_REF/HEAD_REF sentinels missing — cannot distill conflict context"; exit 1; }
MERGE_BASE=$(git merge-base "origin/$BASE_REF" "$HEAD_REF") # timeout: 3000
echo "## 6a: what HEAD_REF added"
git log "$MERGE_BASE..$HEAD_REF" --oneline --no-merges
git diff "$MERGE_BASE" "$HEAD_REF" --stat
echo "## 6b: target drift since merge-base"
git log "$MERGE_BASE..origin/$BASE_REF" --oneline --no-merges
SOURCE_LAST_TIME=$(git log "$HEAD_REF" -1 --format="%ci")
echo "## 6b: commits the contributor never saw"
git log "origin/$BASE_REF" --after="$SOURCE_LAST_TIME" --oneline  # timeout: 9000
```

6a one-sentence summary: which files/modules PR owns, what it changes.

### 6b: Target-branch drift (the "surprises")

From the same call's `## 6b` sections. One-sentence summary: independent base changes after contributor's last commit — preserve unconditionally

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
1. Read tool: inspect full file, locate all conflict markers
2. Determine correct resolution using contribution motivation above as priority lens:
   - Contributor's new functionality takes priority for files PR owns (introduced or substantially rewrote)
   - Base's independent refactors and config updates always preserved
   - When both sides changed same logic, blend: keep PR's semantic change while incorporating base's structural update
3. Edit tool: apply targeted replacements removing all conflict markers, producing correct resolved content — do NOT rewrite whole file; minimal targeted replacements only
4. After resolving each file, stage it: git add -- <file>  (timeout: 3000)

Return ONLY a compact JSON envelope — no prose, no explanation:
{\"status\":\"done\",\"resolved\":N,\"staged\":N,\"confidence\":0.N}
")
```

> **Health monitoring** — SKILL.md §Agent wait discipline: in the spawn response, write `$IMPL_DIR/agent-watch-conflict.tsv` with the row `conflict-resolver<TAB>-<TAB>900` (envelope-only agent, 15-min deadline). Never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop; no filler call, no "waiting" line, no sleep. At the Step 7b join run the watch check first: `timed_out`, or a notification that arrived without the JSON envelope → ⏱ `timed_out` now, surface partial results, proceed with staged files.

> **Turn placement**: this spawn is followed in the same response by Step 3d's `AskUserQuestion`, not by an ended turn — the selection question is substantive work, so the no-filler rule above is satisfied. The completion notification and the user's answer arrive independently; whichever lands second opens the join below.

### 7b: Verify and complete merge

> Run 2's first work — the Step 7b join, after both the agent envelope and the user's Step 3d answer are in hand, before Step 3e creates any item task.

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

Mark all conflict tasks completed — in one response, riding with the merge commit call:

```text
for each (filepath, conflict_task_id) pair from Step 5a: TaskUpdate(task_id=\<conflict_task_id>, status="completed")  — all in one response, beside the merge commit call
```
