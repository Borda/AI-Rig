<!-- file: worktree-isolation.md — consumers: plugins/cc_develop/skills/{feature,fix,refactor,debug,review}/SKILL.md (cc_oss ships its own copy) -->

# Worktree isolation (`--worktree`)

Offload whole skill run into isolated git worktree. Gated on `WORKTREE_ENABLED=true`. Flag absent → skip entire file, run in main tree as before.

Base = current `HEAD` (deterministic). Do NOT rely on `EnterWorktree(name=…)` alone — its default `worktree.baseRef` is `fresh` (branches from `origin/<default-branch>`), discarding local commits. Instead create the worktree explicitly off `HEAD`, then enter it by `path`.

> **Uncommitted-changes caveat** — a git worktree starts from a commit; uncommitted working-tree edits do NOT transfer. Commit (or stash→apply) local work first if the run must see it. Warn the user when `git status --porcelain` is non-empty at §Enter.

## §Enter — before any mutation

Run right after flag parse, **before** codemap detection + Step 1. Steps:

0. Read gate (values written without trailing newline → explicit equality, not `|| default` which clobbers `read`'s partial assignment):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-<skill>-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
```

`WORKTREE_ENABLED` != `true` → skip rest of file, run in main tree.

1. Guard — already in a worktree (`git rev-parse --git-common-dir` ≠ `.git`)? → skip Enter, warn `⚠ already in worktree — --worktree no-op`, continue in current tree.
2. Create + enter (deterministic HEAD base). Slug = first ~4 words of goal, lowercased, non-`[A-Za-z0-9._-]`→`-`, ≤48 chars; empty goal → `dev-<skill>`. Persist `_ORIG_ROOT` (main tree) for §Deliverable + §Exit — capture **before** entering:

```bash
# timeout: 30000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/heal_git_artifacts.py" worktrees
```

> Heal before creating, not after — the run that leaks a worktree is by definition the one that never reaches its own cleanup. This call is **report-only**; deletes nothing.
>
> Exit 0 (nothing reclaimable) → say nothing, continue. Exit 1 → print the tool's list verbatim, then `AskUserQuestion`: (a) **Skip** — leave them, continue the run · (b) **Remove them** — run the block below in this turn, continue · (c) **Abort**. Removing a worktree deletes a directory tree, so it never happens without this answer — never run the `--apply` form unprompted.

```bash
# timeout: 30000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/heal_git_artifacts.py" worktrees --apply
```

> Candidates are only clean, registered-or-orphaned, ≥14-day-old `agent-*`/`oss-*` trees — subagent-isolation and oss-skill trees, not yours. **`dev-*` is never a candidate** — §Exit contracts those as deliverables you review and merge yourself. Uncommitted work is reported, kept at any age. Never abort the run because healing was skipped.
>
> Scope note: this whole file is gated on `--worktree`, so worktree healing runs only on isolated runs.

```bash
# timeout: 15000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
git rev-parse --show-toplevel > "${TMPDIR:-/tmp}/dev-<skill>-orig-root-${CSID}"   # main-tree root, read later
git status --porcelain 2>/dev/null | head -1 | grep -q . && echo "⚠ uncommitted changes will NOT appear in the worktree — commit/stash first if needed"
WT=".claude/worktrees/dev-<skill>-<slug>"
git worktree add -b "dev-<skill>-<slug>" "$WT" HEAD   # branch off current HEAD, not origin/default
```

> Branch/path name collision (`add` fails `already exists`) → append a short disambiguator and retry once.

3. `EnterWorktree(path=".claude/worktrees/dev-<skill>-<slug>")` — switches session CWD into the worktree. All later edits, tests, codemap scans land there.
4. Warm-start codemap (optional, cheap) — copy main index in so first query is `current`, not cold scan:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _ORIG < "${TMPDIR:-/tmp}/dev-<skill>-orig-root-${CSID}" 2>/dev/null || _ORIG=""
_PROJ="$(basename "$(pwd)")"
[ -n "$_ORIG" ] && [ -f "$_ORIG/.cache/codemap/$_PROJ.json" ] && { mkdir -p .cache/codemap && cp "$_ORIG/.cache/codemap/$_PROJ.json" ".cache/codemap/$_PROJ.json" 2>/dev/null; } || true
```

> Silent no-op if main index absent or basename differs — worktree cold-builds its own. Never block on copy failure.

## §Codemap alignment — automatic, no coordination

Index path is anchored to the git toplevel (`<root>/.cache/codemap/<project>.json`, project = raw basename of that root), not the CWD; under `CODEMAP_INDEX_DIR` it is flat (`<override>/<project>.json`). A linked worktree is its own git toplevel, so root differs either way → **each worktree owns its own index**. Parallel runs never share/race one index. `.cache/` gitignored → worktree index never merges back → main index untouched. After user merges branch, `inject-preamble.py` currency check flags main index stale next prompt → one standard refresh.

Do NOT point `CODEMAP_INDEX_DIR` at a shared path inside worktree mode — breaks isolation.

## §Deliverable — read-only skills only (review, debug)

Code-editing skills (feature, fix, refactor) want everything in the worktree — skip this section. Read-only skills produce a report/diagnosis that is the deliverable, must stay reachable from the main tree (a later `fix` reads the diagnosis; the user reads the report). Write the **final** deliverable to the main tree, not the worktree:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _ORIG_ROOT < "${TMPDIR:-/tmp}/dev-<skill>-orig-root-${CSID}" 2>/dev/null || _ORIG_ROOT="$(pwd)"
```

Prefix final report/diagnosis path with `$_ORIG_ROOT` (absolute). Intermediate `.temp/` handoffs may stay in the worktree (ephemeral). Report main-tree deliverable path to user.

## §Exit + report — end of run

After quality/review gates, before final summary:

1. Capture branch — `git branch --show-current` (from worktree CWD).
2. `ExitWorktree(action="keep")` — returns session to original dir, leaves worktree + branch on disk. **Never** `remove`; **never** auto-merge. (A `path`-entered worktree is never auto-removed by `ExitWorktree` — keep is the only sane action.)
3. In Final Report, surface `Worktree` block:

```
Worktree — isolated run (base: HEAD)
  path:   .claude/worktrees/dev-<skill>-<slug>/
  branch: <branch>
  merge:  review, then `git merge <branch>` (or open PR from it)
```

## §Team interaction

`--worktree` + `--team` compose. Orchestrator worktree = integration point. `--team` teammates keep own `isolation:worktree`; branches merge into orchestrator worktree branch as today. No change to team flow — runs one level deeper.
