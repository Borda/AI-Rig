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
  GitHub ids kept · report items appended · render ACTION_ITEMS table (shown once: picker preview, or table file past the preview cap)
  |
◆ SELECTION GATE  (step 3d)   single stream — no agent in flight beside the picker
  bulk page first (table in previews or file) · which items · commit mode
  topic group + typed labels · dispatch width · push intent + post-PR action
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
STREAM A: CONFLICT RESOLVE + COMMIT MERGE  STREAM B: CHALLENGE, EARLY WAVE  (step 8 phase 1)
  distill intent + base drift  (step 6)     verdict re-check  stale head or missing verifier file → confirmation dropped
                                            ▣ challenge      parallel by domain, read-only
  ▣ 1 agent, all files  (step 7a)                            ≤12 items per chunk · one file past it → its own ordered chunks, still parallel
  resolve markers, stage                                     reviewer evidence first · verifier-confirmed → fix check only
  verify nothing unmerged · commit          pinned PR-head reads while files conflict, never disk
    · re-point HEAD fingerprint  (step 7b)  C1 candidates held out (medium effort + bridge)
                                                             ▣ caucus: origin agent type, one item, after a failed retry
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
IMPLEMENT  (step 8, rest)
  C1 Codex-first for medium items (clean tree, one item per call)
  ▣ challenge, late wave  C1 fall-throughs only
  ▣ specialists    parallel, one git worktree each  (step 8 phase 2)
                   every worktree pinned at step 0 to the PR head captured right before the first wave (phase2-base-sha)
                   group tags + per-link items fixed once in phase2-groups.tsv, read on resume
                   ≤5 items per spawn (8 under per-specialist) · one file past it → chained links, one at a time
                   sw-engineer without an xhigh item (max effort high or medium) → sonnet
    merge-back     sequential cherry-pick, most-central first  (step 8 phase 3)
  |
VERIFY  (step 9)  target drift check → re-merge on drift, unattended, ≤2  (step 9.0)
                  ▣ qa-specialist ‖ ▣ linting-expert  (targeted tests)
  full suite once, background run  (step 9)
  |
◆ PUSH CONFIRMATION  (step 10)  diff stat + commit count + target drift — skipped on an explicit "don't push" intent
  |
SHIP  push (step 10) · final report + resolution.jsonl → review dir (step 11) · ▣ comment dispatch (step 12)
```

Between the selection gate and the push confirmation the run is unattended on the normal path: every decision that can be made with the same information is answered at the selection gate and persisted. Push authorization needs the diff stat, which exists only after implementation, so it stays at Step 10.

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| INTEL ‖ BRANCH + TRIAL MERGE | 2 | fixed | MERGE FINDINGS |
| CONFLICT RESOLVE ‖ IMPLEMENT challenge, early wave | 2 batches | one conflict agent beside the challenge chunks below (it holds one opus slot) | JOIN: merge committed and every challenge chunk, retry and caucus settled |
| IMPLEMENT challenge | at least `Σ ceil(n_d/12)` | ≤12 items per chunk over 3 domains, a file past 12 cut into its own ordered chunks, all concurrent (read-only); pool caps: opus 5, sonnet 8 | early wave with the fan's join, late wave (C1 fall-throughs) before specialists |
| IMPLEMENT specialists | `DISPATCH_MODE` | ≤5 items per spawn (8 under `per-specialist`), a chain holds one slot; pool caps by effective model: opus 5, sonnet 8 | merge-back |
| VERIFY | 2 | fixed | ship |
| SHIP comment dispatch | 3 | `BATCH_SIZE`, waves of 3 | end |

Every `▣` lane carries a deadline armed in its spawn turn (`agent-watch-<batch>.tsv`) and checked by one `agent_watch.py` call at each wake-up; a join never polls, and a lane that stops without its deliverable joins as ⏱ timed out.

Both fans overlap work with no data dependency between the lanes. The first works because checkout and the trial merge need only the PR number, so it adds no spawn. The second works because conflict resolution does not depend on which items get selected (mandatory even at zero selected items) and the challenge only reads; it adds no wall-clock beyond the slower lane.

**Finding collection is one stream.** No agent runs beside the SELECTION GATE: a conflict agent there once raced the picker and the table `enforce-resolve-table.js` requires failed to land often. Conflict resolution therefore starts after the gate, not beside it, trading the human-idle overlap for a reliable table.

**What bounds the first fan.** CONFLICT RESOLVE reads the contribution motivation INTEL synthesizes, where thread consensus outranks the PR body. It therefore cannot join the first fan, and never substitutes a git-log-only reading of intent. **What bounds the second fan.** C1 and Phase 2 need a committed merge and a clean tree, so they wait for the join; C1 candidates (medium effort with the bridge available) are held out of the early challenge wave and challenged late only if C1 hands them back. Challengers read conflicted files from the PR head, never the half-merged disk copy.

`DISPATCH_MODE` sets specialist width only: `auto` (≤5 items per spawn, pool-capped waves) · `sequential` (one worktree at a time) · `per-specialist` (≤8 items per spawn, usually one worktree per specialist) · `preview` (shown as "Custom" — ask again, with the formed groups as every option's preview). Specialist routing, the file-ownership tiebreak and the import-coupling merge never change with it, and no mode lifts the per-spawn cap.

**Chained links are not a fan.** One file holding more items than the cap becomes one group whose links run strictly one after another: link k+1 spawns after link k's envelope lands, in its own worktree pinned to link k's tip as recorded in `chain-<group_tag>.tsv`; link 1 pins to `phase2-base-sha`. The group's tag and per-link items are written once to `phase2-groups.tsv` before the first spawn, so a resumed run continues the same lineage instead of re-deriving a tag; every spawned link is recorded in `phase2-spawned.tsv`, so a resume never puts a second agent on a link still in flight, and a recorded link with no watch row (never launched) is armed as timed out and joins as ⏱ instead of being waited on. A link that reports a base mismatch, or whose tip does not descend from its base, ends the chain; its remaining items stay pending. The chain holds one pool slot for its whole life, and merge-back sees it as one group with its commit order intact.

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown flag · missing report source · codemap index | conditional | SETUP |
| more than 20 conflicted files | conditional | trial merge, and aborts it |
| **SELECTION** (items, commit mode, grouping + labels, dispatch width, push intent + post-PR action; `enforce-resolve-table.js` denies it until one table holds every pending and every resolved/addressed item, in the first question's text or every option preview within the host's 2000-char / 12-line preview cap (a line over 86 chars counts as wrapped; an option description never counts) — the skill puts it in the Q1 bulk options' `preview`, the table's only copy, or past the cap in `action-items-table.md`, named in Q1's text with a capped summary in every preview) | **always** | everything past it, including the second fan |
| group preview (`DISPATCH_MODE=preview`, elected at SELECTION) | conditional | challenge → specialists |
| challenge timed out twice (batched per wave) | conditional, error recovery | challenge → specialists |
| unresolved item status | conditional, error recovery | final report |
| **PUSH CONFIRMATION** (target, diff stat, commit count, last subject, target-branch drift + re-sync option; post-PR too when no intent was recorded) | **always**, unless SELECTION recorded an explicit "don't push" | ship |
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

- **Zero conflicted files** — the trial merge commits itself; the CONFLICT RESOLVE lane and its join are no-ops, leaving the challenge early wave alone in the second fan.
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
| COMMIT MERGE (stream A, before the join) | 7b join (3e tasks open the fan) |
| IMPLEMENT | 8 |
| VERIFY | 9 |
| SHIP | 10, 11, 12 |
