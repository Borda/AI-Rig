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
  dedup report against GitHub · print ACTION_ITEMS table
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
CONFLICT RESOLVE  (steps 6, 7)            ◆ SELECTION GATE  (step 3d)
  ▣ 1 per file  (step 7a)                   which items
  distill intent + base drift  (step 6)     commit mode
  resolve markers, stage                    topic group
                                            dispatch width
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
COMMIT MERGE  (step 7b join, step 3e)
  verify nothing unmerged · commit · create per-item tasks  (step 3e)
  |
IMPLEMENT  (step 8)
  ▣ challenge      parallel by domain, read-only  (step 8 phase 1)
  ▣ specialists    parallel, one git worktree each  (step 8 phase 2)
    merge-back     sequential cherry-pick, most-central first  (step 8 phase 3)
  |
VERIFY  (step 9)  ▣ qa-specialist ‖ ▣ linting-expert
  |
◆ PUSH GATE  (step 10)  authorize push + post-PR action
  |
SHIP  push (step 10) · final report (step 11) · ▣ comment dispatch (step 12)
```

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| INTEL ‖ BRANCH + TRIAL MERGE | 2 | fixed | MERGE FINDINGS |
| CONFLICT RESOLVE ‖ SELECTION GATE | 2 | conflicted-file count on one side, one gate on the other | COMMIT MERGE |
| IMPLEMENT challenge | ≤3 | challenger roster (3 domains) | before specialists |
| IMPLEMENT specialists | `DISPATCH_MODE` | pool caps: opus 5, sonnet 8 | merge-back |
| VERIFY | 2 | fixed | push gate |
| SHIP comment dispatch | 3 | `BATCH_SIZE`, waves of 3 | end |

Both top-level fans are free — each rides an idle window the orchestrator already had, so neither adds a spawn. The first works because checkout and the trial merge need only the PR number. The second works because conflict resolution does not depend on which items get selected; it is mandatory even at zero selected items.

**What bounds the first fan.** CONFLICT RESOLVE reads the contribution motivation INTEL synthesizes, where thread consensus outranks the PR body. It therefore cannot join the first fan, and never substitutes a git-log-only reading of intent.

`DISPATCH_MODE` sets specialist width only: `auto` (≤5 items per worktree, pool-capped waves) · `sequential` (one worktree at a time) · `per-specialist` (no split, one worktree per specialist) · `preview` (shown as "Custom" — print the formed groups, ask again). Specialist routing, the file-ownership tiebreak and the import-coupling merge never change with it.

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown flag · missing report source | conditional | SETUP |
| more than 20 conflicted files | conditional | trial merge, and aborts it |
| **SELECTION** | **always** | everything past the second join |
| more than 20 items selected | conditional | per-item task creation |
| group preview (`DISPATCH_MODE=preview`) | conditional | challenge → specialists |
| topic labels (`GROUP_STRATEGY=labels`) | conditional | implement commit |
| **PUSH** | **always** | ship |
| unresolved item status | conditional | final report |

Normal action-item path costs at most 5 `AskUserQuestion` calls.

## Mode branches

| Mode | Schema |
| -- | -- |
| `pr` | full schema above; MERGE FINDINGS is a pass-through |
| `pr + report` | full schema; MERGE FINDINGS dedups the saved review report against GitHub comments |
| `report`, no PR number | both fans collapse. No branch, no merge, no conflicts: gather findings from the report, SELECTION GATE, implement, verify. No push. |

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
| PUSH GATE · SHIP | 10, 11, 12 |
