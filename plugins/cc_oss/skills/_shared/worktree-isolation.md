<!-- file: worktree-isolation.md — consumers: plugins/cc_oss/skills/{review,resolve}/SKILL.md -->

# Worktree isolation (`--worktree`) — oss

Opt-in. Offload the run into an isolated git worktree so the caller's working tree + branch are never touched. Gated on `WT_ENABLED=true`; flag absent → run in main tree as today.

Base = current `HEAD` (deterministic). Do NOT lean on `EnterWorktree(name=…)` alone — its default `worktree.baseRef` is `fresh` (branches from `origin/<default>`), wrong base. Create off `HEAD` explicitly, enter by `path`.

## §Enter

Run right after flag parsing, before any checkout / agent spawn / report dir creation.

1. Gate:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-<skill>-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false
```

`WT_ENABLED` != `true` → skip this file.

2. Guard already-in-worktree (`git rev-parse --git-common-dir` ≠ `.git`) → warn `⚠ already in worktree — --worktree no-op`, continue.
3. Create off HEAD + persist main-tree root (for §Deliverable / restore), then enter:

```bash
# timeout: 15000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
git rev-parse --show-toplevel > "${TMPDIR:-/tmp}/oss-<skill>-orig-root-${CSID}"
WT=".claude/worktrees/oss-<skill>-<slug>"      # slug: PR number, or report basename
git worktree add -b "oss-<skill>-<slug>" "$WT" HEAD   # branch off HEAD, not origin/default
echo "$WT" > "${TMPDIR:-/tmp}/oss-<skill>-wt-${CSID}"   # final path, read by EnterWorktree below
```

> `add` fails `already exists` → append a disambiguator to `<slug>` (changes branch **and** path), retry once. Never check or ask about other worktrees up front — they don't block this run. **Only if the retry still fails**, run the report (deletes nothing):

```bash
# timeout: 30000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/heal_git_artifacts.py" worktrees
```

> Exit 0 (nothing reclaimable) or exit 2 (environment error) → leftovers aren't the cause; print the `git worktree add` error, stop. Exit 1 but the `add` error names none of the listed paths or branches → leftovers aren't the cause; print the error, stop. Exit 1 and the error names a listed path or branch → `AskUserQuestion` with the list and the `add` error as the `preview` of both options, not as reply text before the call (it can arrive as an empty progress update); preview cap ≤2000 chars and ≤12 lines (Claude Code withholds or clips a larger preview, no scroll) — over it, Write both to `.temp/worktree-leftovers.md` first, preview the count, the `add` error and the first entries that fit, ending `→ full list: .temp/worktree-leftovers.md`, and name that path in the question text — never offer removal of an unseen list: (a) **Stop** — keep everything, end the run · (b) **Remove them and retry** — run the block below in this turn, retry the create once. That retry fails too → print the error, stop; never loop back to the report. Removing a worktree deletes a directory tree, so never run the `--apply` form without that answer.

```bash
# timeout: 30000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/heal_git_artifacts.py" worktrees --apply
```

> Candidates are only clean, registered-or-orphaned, ≥14-day-old `agent-*`/`oss-*` trees. Uncommitted work is reported and kept at any age; `dev-*` and hand-made names are never candidates. Lock healing (`oss:resolve` Step 8) is unconditional and unaffected.

4. `EnterWorktree(path=<final $WT from `${TMPDIR:-/tmp}/oss-<skill>-wt-${CSID}`>)` — the possibly disambiguated path, never the original slug — session CWD now the worktree.

## §review — read-only, report to main tree

`oss:review` produces a report; write the **final** report under the main tree so its printed path + follow-up gate stay valid:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _ORIG_ROOT < "${TMPDIR:-/tmp}/oss-review-orig-root-${CSID}" 2>/dev/null || _ORIG_ROOT="$(pwd)"
```

Prefix `REPORT_DIR` with `$_ORIG_ROOT` (absolute). `$RUN_DIR` (`.temp/`) handoffs may stay in the worktree. Review reads the PR diff as usual (`gh pr diff` is remote — CWD-independent).

## §resolve — wrap the whole run above its existing isolation

`oss:resolve` mutates a PR branch and **pushes to the contributor fork** (its real deliverable is remote — reachable regardless of local worktree). Entering the session worktree **before** Step 4 `gh pr checkout` means the checkout, Phase-2 per-specialist `git worktree`s, cherry-pick merge-back, and push all happen off the isolated worktree; the caller's main tree + branch never change.

Composition facts (verified against `resolve/modes/action-item-dispatch.md`):

- **Mutex path** derives from git-common-dir + branch. Worktrees share the common-dir, so the lock is identical inside the session worktree → a second concurrent resolve on the same PR branch is still blocked. No regression.
- **Phase-2 specialist worktrees** register on the same common-dir (siblings, not nested) and pin to `phase2-base-sha` — the PR HEAD captured right before the first spawn wave, after C1's `each` commits — exactly as before; `resolve-base-sha` (the prelude fingerprint) stays the reset fence's anchor.
- **Step 11 caller-branch restore** becomes a harmless no-op — the main tree was never switched.
- **Push** targets the same remote fork; unaffected by CWD.

Enter is the only addition; do not alter checkout, mutex, fingerprint, or push logic.

## §Exit

End of run, after gates/push:

1. `git branch --show-current` (from worktree CWD).
2. `ExitWorktree(action="keep")` — returns session to original dir, leaves worktree + branch on disk. Never `remove`, never auto-merge. (`path`-entered worktrees are never auto-removed.)
3. Surface a `Worktree` block: `path` · `branch` · note (review: `merge if you want the report's companion edits`; resolve: `` commits already pushed to the fork — worktree is disposable, remove with `git worktree remove` when done ``).

## §Isolated spawns

A spawn that itself passes `isolation="worktree"` gets its own worktree and **cannot write into the session worktree** ("This path is in a different worktree"), nor into the main checkout ("Edit the worktree copy of this file instead of the shared-checkout path"). Never give such an agent a deliverable path inside the session worktree — `$RUN_DIR` under `.temp/` included. Hand it a path outside every checkout (`mktemp -d`, as resolve's `IMPL_DIR`), or take its result from its return envelope or its worktree branch (resolve Phase 2 commits there, then cherry-picks). Review dimension agents spawn without `isolation`, so they write `$RUN_DIR` in the session worktree directly.
