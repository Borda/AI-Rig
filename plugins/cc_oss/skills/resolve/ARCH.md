<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/oss:resolve` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking a 1200-line skill.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. A schema that disagrees with `SKILL.md` is worse than none — `SKILL.md` wins every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

```
SETUP
  flags · gh auth · codemap detect · workflow tasks
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
INTEL  ▣ 1 agent                        BRANCH + TRIAL MERGE
  fetch PR thread                         checkout PR branch
  classify every comment                  merge base --no-commit
  synthesize motivation                   detect conflicted files
  |                  |                    create per-conflict tasks
  |          needed by conflict resolve              |
  +--------------------- JOIN -----------------------+
  |
MERGE FINDINGS
  dedup report against GitHub · print ACTION_ITEMS table
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
CONFLICT RESOLVE  ▣ 1 per file            ◆ SELECTION GATE
  distill intent + base drift               which items
  resolve markers, stage                    commit mode
                                            topic group
                                            dispatch width
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
COMMIT MERGE
  verify nothing unmerged · commit · create per-item tasks
  |
IMPLEMENT
  ▣ challenge      parallel by domain, read-only
  ▣ specialists    parallel, one git worktree each
    merge-back     sequential cherry-pick, most-central first
  |
VERIFY  ▣ qa-specialist ‖ ▣ linting-expert
  |
◆ PUSH GATE  authorize push + post-PR action
  |
SHIP  push · final report · ▣ comment dispatch
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

Navigation only; the step numbers carry no meaning at this level.

| Block | Steps |
| -- | -- |
| SETUP | 1, 2 |
| INTEL | 3a, 3b |
| BRANCH + TRIAL MERGE | 4, 5 |
| MERGE FINDINGS | 3c |
| CONFLICT RESOLVE | 6, 7a |
| SELECTION GATE | 3d |
| COMMIT MERGE | 7b join, 3e |
| IMPLEMENT | 8 |
| VERIFY | 9 |
| PUSH GATE · SHIP | 10, 11, 12 |
