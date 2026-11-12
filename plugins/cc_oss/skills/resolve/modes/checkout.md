<!-- file: checkout.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:resolve -->

## Step 4: Checkout PR branch

> **Run 1** — entered from Step 3b's overlap directive, in the same turn that spawned `INTEL_AGENT`, not after Step 3e. Needs only `PR_NUMBER`.

**Worktree isolation (opt-in `--worktree`)** — run FIRST, before `gh` check + checkout below, so checkout, Phase-2 specialist worktrees, cherry-picks, and push all happen off an isolated worktree and caller's main tree/branch never change. Skip when `WT_ENABLED != true` or `MODE = report` with no PR#.

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED="$(python "$PLUGIN_ROOT/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)"
[ "$WT_ENABLED" = "true" ] && [ -f "$_OSS_SHARED/worktree-isolation.md" ] && python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_SHARED/worktree-isolation.md"  # timeout: 5000
```

`WT_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`) + §resolve (do NOT alter checkout/mutex/fingerprint/push — Enter is the only addition; the mutex path is worktree-invariant, Step 11 restore becomes a harmless no-op, and the push still targets the fork). Then continue Step 4 below inside the worktree.

*Skip only when `MODE = report` with no PR# (`$PR_NUMBER` unset — no remote branch to check out). In pr mode, runs unconditionally regardless of `SELECTED_ITEMS` — conflict resolution must happen even when 0 action items selected.*

When skipping: `TaskUpdate(task_id=TASK_CHECKOUT, status="deleted")`, `TaskUpdate(task_id=TASK_CONFLICT, status="deleted")`. Otherwise: `TaskUpdate(task_id=TASK_CHECKOUT, status="in_progress")`, then `TaskUpdate(task_id=TASK_CHECKOUT, status="completed")`. Timing: see the **Zero bookkeeping-only turns** table.

**Branch-safety pre-check** — must run BEFORE `gh pr checkout` so a wrong-branch commit is impossible (per `git-commit.md` Gate 2). Verify PR's `headRefName` isn't repo's default branch — `gh pr checkout` of a same-repo PR whose HEAD = default branch would land on default; any later commit (Step 8) would violate Gate 2. One block, in order: the pre-check (`resolve_pr_refs.py`), the `gh` check + checkout + same-branch verify, then the `FORK_REMOTE`/`PR_REF` derivation for Step 10. Every hard `exit 1` ends the whole block, so a failed stage never reaches the next:

```bash
# timeout: 45000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "! BLOCKED — Step 4 PR number sentinel missing or invalid; refusing checkout"; exit 1 ;; esac
python "$PLUGIN_ROOT/bin/resolve_pr_refs.py" --pr "$PR_NUMBER" || exit $?  # timeout: 15000
# gh = hard prereq: gh pr checkout has no fallback path
command -v gh >/dev/null 2>&1 || { echo "! BLOCKED — gh CLI required; install: https://cli.github.com"; exit 1; }
# resolve_pr_refs.py persisted these sentinels; a child process cannot export them
IFS= read -r PR_HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || PR_HEAD_REF=""
IFS= read -r PR_HEAD_OID < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || PR_HEAD_OID=""
# SHA-first: skip if at PR head — avoids worktree conflict (gh pr checkout aliases pr-N-slug if branch active elsewhere)
IFS= read -r LOCAL_SHA < "${TMPDIR:-/tmp}/resolve-local-sha-${CSID}" 2>/dev/null || LOCAL_SHA=""
if [ -n "$PR_HEAD_OID" ] && [ "$LOCAL_SHA" = "$PR_HEAD_OID" ]; then
    echo "→ Already at PR head ($LOCAL_SHA) — skipping gh pr checkout"
    # SHA match, diff branch (e.g. pr<N> alias) — force-align to PR_HEAD_REF so Step8/10 land correct branch
    CURRENT=$(git branch --show-current 2>/dev/null)
    if [ -n "$PR_HEAD_REF" ] && [ "$CURRENT" != "$PR_HEAD_REF" ]; then
        echo "→ Re-aligning local branch: $CURRENT → $PR_HEAD_REF (same SHA $LOCAL_SHA)"
        if git show-ref --verify --quiet "refs/heads/$PR_HEAD_REF"; then
            [ "$(git rev-parse "refs/heads/$PR_HEAD_REF")" = "$PR_HEAD_OID" ] || { echo "! BLOCKED — existing branch $PR_HEAD_REF is not at PR head $PR_HEAD_OID; preserve its history and reconcile before retrying"; exit 1; }
            git switch --end-of-options "$PR_HEAD_REF" \
                || { echo "! BLOCKED — Cannot switch to $PR_HEAD_REF — aborting (branch active in another worktree?)"; exit 1; }
        else
            git switch -c "$PR_HEAD_REF" --end-of-options "$PR_HEAD_OID" \
            || { echo "! BLOCKED — Cannot switch to $PR_HEAD_REF — aborting (branch active in another worktree?)"; exit 1; }
        fi
    fi
else
    # hard-exit on failure — else HEAD_REF set but git stuck on caller branch, Step8 commits land wrong branch
    # --branch required: w/o it gh CLI v2.93+ falls back to pr<N> alias on collision → Step10 push makes unrelated branch (CRITICAL bug pyDeprecate 2026-06-13T08:33Z)
    gh pr checkout "$PR_NUMBER" --branch="$PR_HEAD_REF" \
        || { echo "! BLOCKED — gh pr checkout failed — aborting (network, branch deleted, auth expired, or local conflicts)"; exit 1; }   # timeout: 15000
fi
# verify in the same call — checkout must land on the expected branch, else abort before Step 8 can commit
HEAD_REF="$PR_HEAD_REF"
IFS= read -r IS_CROSS_REPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || IS_CROSS_REPO=""
[ -n "$HEAD_REF" ] && [ -n "$IS_CROSS_REPO" ] || { echo "! BLOCKED — Step 4 verify: HEAD_REF/IS_CROSS_REPO sentinels missing — checkout state unverifiable, aborting before Step 8 can commit"; exit 1; }
git remote -v | grep '(fetch)' | head -10 # timeout: 3000
git status  # timeout: 3000
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null)  # timeout: 3000
# same-repo: branch must equal PR_HEAD_REF, no alias — gh falls back to pr<N> on collision; assert as hard gate
if [ "$IS_CROSS_REPO" = "false" ] && [ "$CURRENT_BRANCH" != "$PR_HEAD_REF" ]; then
    echo "! BLOCKED — SAME-REPO RULE VIOLATION: on '$CURRENT_BRANCH' but PR headRefName='$PR_HEAD_REF' — branch alias (pr<N>) created instead of using original branch. Aborting to prevent push to wrong branch."
    exit 1
fi
[ "$CURRENT_BRANCH" = "$HEAD_REF" ] || { echo "! BLOCKED — checkout did not land on $HEAD_REF (current: $CURRENT_BRANCH) — aborting before Step 8 can commit to wrong branch"; exit 1; }  # timeout: 3000
CURRENT_SHA=$(git rev-parse HEAD 2>/dev/null)  # timeout: 3000
if [ -n "$PR_HEAD_OID" ] && [ "$CURRENT_SHA" != "$PR_HEAD_OID" ] && git merge-base --is-ancestor "$PR_HEAD_OID" HEAD; then
    echo "! BLOCKED — '$CURRENT_BRANCH' is ahead of PR head $PR_HEAD_OID (unpushed local commits or a newer push) — push or move those commits, then re-run"
    exit 1
fi
[ -n "$PR_HEAD_OID" ] && [ "$CURRENT_SHA" = "$PR_HEAD_OID" ] || { echo "! BLOCKED — checkout HEAD '$CURRENT_SHA' differs from PR head '$PR_HEAD_OID' — aborting before Step 8 can commit"; exit 1; }
# FORK_REMOTE for the Step 10 push; truncate first so a failed derivation leaves no stale value
: > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"
: > "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}"
if [ "$IS_CROSS_REPO" = "true" ]; then
    IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-head-repo-owner-${CSID}" 2>/dev/null || FORK_REMOTE=""
    [ -n "$FORK_REMOTE" ] || FORK_REMOTE=$(gh pr view "$PR_NUMBER" --json headRepositoryOwner --jq .headRepositoryOwner.login) # sentinel-miss fallback only # timeout: 6000
    PR_URL=$(gh pr view "$PR_NUMBER" --json url --jq .url) || { echo "! BLOCKED — Could not resolve fork PR URL"; exit 1; } # timeout: 6000
    [[ "$PR_URL" =~ ^https://[^/]+/[^/]+/[^/]+/pull/${PR_NUMBER}$ ]] || { echo "! BLOCKED — Fork PR URL does not match PR number"; exit 1; }
    PR_REF="$PR_URL"
else
    FORK_REMOTE="origin"
    PR_REF="#$PR_NUMBER"
fi
echo "$PR_REF" > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"  # timeout: 3000
echo "$FORK_REMOTE" > "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}"  # read by Step 3d push question + Step 10 push
# soft-verify — layouts vary across gh versions
git remote get-url "$FORK_REMOTE" >/dev/null 2>&1 \
    || echo "⚠ Remote $FORK_REMOTE not registered — Step 10 will add it before push" # timeout: 3000
```

`gh pr checkout` auto-handles forks — adds contributor's remote, configures tracking. `FORK_REMOTE`: contributor login (e.g. `alice`) for forks, `origin` for same-repo. `resolve-head-ref` is the local checkout branch; `resolve-remote-head-ref` is the contributor's destination branch. A fork whose head has the upstream default-branch name uses local `pr-<N>-<head>`; it never checks out the upstream default branch. Step 10 hands over an explicit destination refspec when the local alias differs from the remote head; ordinary branches retain the confirmed `git push` route.

`PR_REF`: the token Step 8's commit messages embed for this PR — `#<N>` when the commit lands same-repo (`FORK_REMOTE=origin`), or the full `PR_URL` when it lands in the contributor's fork (bare `#N` there would resolve against the fork's own issues, not this repo's PR — a cross-repo false link). Persisted to `${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}` for Step 8 to read.

**Before any merge or action-item commit:** follow `modes/dispatch-prelude.md` §Repository hook approval, using this run's `IMPL_DIR` sentinel. Show the inspector's exact hook commands/configuration and record the user's choice before the first hook execution; commit permission alone never approves repository hooks. A new run uses its new intelligence directory, never an earlier approval. Both merge commits use `resolve_commit_hooks.py --commit-merge`; an absent approval keeps hooks disabled. Report-only mode reaches the same approval contract in Step 8.
