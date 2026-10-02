<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/oss:resolve` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking a 1200-line skill.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. Every schema block carries a `(step N)` tag naming its `SKILL.md` step; renumbering a step updates the tag and the index at the end. A schema that disagrees with `SKILL.md` is worse than none — `SKILL.md` wins every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

```
SETUP  (steps 1, 2)
  flags · gh auth · codemap detect · workflow tasks
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
INTEL  (steps 3a, 3b)                   BRANCH + TRIAL MERGE  (steps 4, 5)
  ▣ 1 agent                               checkout PR branch  (step 4)
  fetch PR thread                         merge base --no-commit
  classify every comment                  detect conflicted files  (step 5)
  synthesize motivation                   create per-conflict tasks  (step 5a)
  |                  |                               |
  |          needed by conflict resolve              |
  +--------------------- JOIN -----------------------+
  |
MERGE FINDINGS  (step 3c)
  review findings.jsonl folded in by merge_action_items.py
  GitHub ids kept · report items appended · print ACTION_ITEMS table
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
CONFLICT RESOLVE  (steps 6, 7)            ◆ SELECTION GATE  (step 3d)
  ▣ 1 per file  (step 7a)                   which items · over-20 cap
  distill intent + base drift  (step 6)     commit mode
  resolve markers, stage                    topic group + typed labels
                                            dispatch width
                                            push intent + post-PR action
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
COMMIT MERGE  (step 7b join, step 3e)
  verify nothing unmerged · commit · create per-item tasks  (step 3e)
  |
IMPLEMENT  (step 8)
  verdict re-check  stale head or missing verifier file → confirmation dropped
  ▣ challenge      parallel by domain, read-only  (step 8 phase 1)
                   reviewer evidence first · verifier-confirmed → fix check only
                   ▣ caucus: origin agent type, one item, after a failed retry
  ▣ specialists    parallel, one git worktree each  (step 8 phase 2)
    merge-back     sequential cherry-pick, most-central first  (step 8 phase 3)
  |
VERIFY  (step 9)  ▣ qa-specialist ‖ ▣ linting-expert  (targeted tests)
  full suite once, background run  (step 9)
  |
◆ PUSH CONFIRMATION  (step 10)  diff stat + commit count — skipped on an explicit "don't push" intent
  |
SHIP  push (step 10) · final report + resolution.jsonl → review dir (step 11) · ▣ comment dispatch (step 12)
```

Between the selection gate and the push confirmation the run is unattended on the normal path: every decision that can be made with the same information is answered at the selection gate and persisted. Push authorization needs the diff stat, which exists only after implementation, so it stays at Step 10.

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| INTEL ‖ BRANCH + TRIAL MERGE | 2 | fixed | MERGE FINDINGS |
| CONFLICT RESOLVE ‖ SELECTION GATE | 2 | conflicted-file count on one side, one gate on the other | COMMIT MERGE |
| IMPLEMENT challenge | ≤3 | challenger roster (3 domains) | before specialists |
| IMPLEMENT specialists | `DISPATCH_MODE` | pool caps: opus 5, sonnet 8 | merge-back |
| VERIFY | 2 | fixed | ship |
| SHIP comment dispatch | 3 | `BATCH_SIZE`, waves of 3 | end |

Every `▣` lane carries a deadline armed in its spawn turn (`agent-watch-<batch>.tsv`) and checked by one `agent_watch.py` call at each wake-up; a join never polls, and a lane that stops without its deliverable joins as ⏱ timed out.

Both top-level fans are free — each rides an idle window the orchestrator already had, so neither adds a spawn. The first works because checkout and the trial merge need only the PR number. The second works because conflict resolution does not depend on which items get selected; it is mandatory even at zero selected items.

**What bounds the first fan.** CONFLICT RESOLVE reads the contribution motivation INTEL synthesizes, where thread consensus outranks the PR body. It therefore cannot join the first fan, and never substitutes a git-log-only reading of intent.

`DISPATCH_MODE` sets specialist width only: `auto` (≤5 items per worktree, pool-capped waves) · `sequential` (one worktree at a time) · `per-specialist` (no split, one worktree per specialist) · `preview` (shown as "Custom" — print the formed groups, ask again). Specialist routing, the file-ownership tiebreak and the import-coupling merge never change with it.

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown flag · missing report source · codemap index | conditional | SETUP |
| more than 20 conflicted files | conditional | trial merge, and aborts it |
| **SELECTION** (items, commit mode, grouping + labels, dispatch width, over-20 cap, push intent + post-PR action; `enforce-resolve-table.js` denies it until every pending item has a table row in the reply) | **always** | everything past the second join |
| group preview (`DISPATCH_MODE=preview`, elected at SELECTION) | conditional | challenge → specialists |
| challenge timed out twice (batched per wave) | conditional, error recovery | challenge → specialists |
| unresolved item status | conditional, error recovery | final report |
| **PUSH CONFIRMATION** (target, diff stat, commit count, last subject; post-PR too when no intent was recorded) | **always**, unless SELECTION recorded an explicit "don't push" | ship |
| typed-labels file lost | conditional, error recovery | implement commit |

Normal action-item path costs at most 3 `AskUserQuestion` calls at the selection gate plus the push confirmation. Between them, only the user-elected group preview and the error-recovery gates can ask. The push itself can still stop for a push guard or permission prompt — deliberate user safety controls the skill never bypasses; it records the push status, saves the guard's exact unblock lines, continues to the final report, and ends that report with those lines.

## Mode branches

| Mode | Schema |
| -- | -- |
| `pr` | full schema above; MERGE FINDINGS is a pass-through |
| `pr + report` | full schema; MERGE FINDINGS dedups the saved review report against GitHub comments |
| `report`, no PR number | both fans collapse. No branch, no merge, no conflicts: gather findings from the report, SELECTION GATE (no push question), implement, verify. No push. |

## Degenerate cases

All collapse to a straight line with no special handling:

- **Zero conflicted files** — the trial merge commits itself; the CONFLICT RESOLVE lane and its join are no-ops, leaving the selection gate alone in the second fan.
- **`--worktree`** — the worktree is entered inside the BRANCH lane, so an INTEL agent spawned moments earlier keeps writing to the absolute work directory it was handed.
- **Work under the inline threshold** (~73 tool calls; a typical one-to-three item PR) — nothing spawns at all, so no width applies and the reply says the dispatch answer changed nothing.
- **`--no-challenge`** — IMPLEMENT drops its challenge fan and goes straight to specialists.

## Where this lives in `SKILL.md`

Index of the `(step N)` tags in the schema above, one row per block. Text order in `SKILL.md` is not execution order — its "Run structure" table maps steps to runs.

| Block | Steps |
| -- | -- |
| SETUP | 1, 2 |
| INTEL | 3a, 3b |
| BRANCH + TRIAL MERGE | 4, 5 (5a per-conflict tasks) |
| MERGE FINDINGS | 3c |
| CONFLICT RESOLVE | 6, 7 (7a spawn) |
| SELECTION GATE | 3d |
| COMMIT MERGE | 7b join, 3e |
| IMPLEMENT | 8 |
| VERIFY | 9 |
| SHIP | 10, 11, 12 |
