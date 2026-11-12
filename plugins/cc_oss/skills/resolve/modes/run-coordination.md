<!-- file: run-coordination.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:resolve -->

## Run structure — text order is not execution order

Two blocking user gates cut this workflow into three **runs**; an explicit "don't push" at Step 3d removes the second gate, leaving two. Within a run, work that shares no data dependency is dispatched in the same response turn so it overlaps instead of queueing. Sections below stay in step-number order for reading, except the Step 7b join coordination placed after Step 3d; the run marker on each heading says when it actually executes.

| Run | Executes | Ends at |
| -- | -- | -- |
| 1 | Step 1 · 2 · 3a · 3b ‖ **Step 4 + Step 5** · 3c · gate | Step 3d selection gate |
| 2 | Step 3e · 6 · **Step 7a ‖ Step 8 Phase 1 early wave** · 7b join · C1 · Phase 1 late wave · rest of 8 · 9 | Step 10 push confirmation (skipped on an explicit Step 3d "don't push") |
| 3 | Step 10 push · 11 · 12 | workflow end |

**Run 2 is unattended on the normal path.** Step 3d collects every decision a later step needs and can predict up front with the same information — item scope, commit mode, grouping strategy and typed labels, dispatch width, push intent and the post-PR action — and persists each to a sentinel or `$IMPL_DIR` file. Later steps never re-ask a Step 3d decision. Push authorization is the one decision that cannot move: its required scope (diff stat, commit count, last subject) exists only after implementation, so Step 10 asks it with that scope unless the user explicitly chose "don't push" at Step 3d. Otherwise Run 2 asks only for error recovery (an unresolved item status, a challenge that timed out twice, a lost typed-labels file) or for the group preview the user explicitly chose at Step 3d.

Two overlaps, each riding work that shares no data dependency with its partner:

- **Step 4 + Step 5 beside the Step 3b intel agent.** Checkout and the `--no-commit` merge need only `PR_NUMBER`, never any intelligence output, so they run in the same turn that spawns `INTEL_AGENT` instead of waiting for its envelope.
- **Step 7a beside the Step 8 Phase 1 challenge, after the gate.** Conflict resolution does not depend on which items get selected, and the challenge only reads, so both fire in one response once Step 3d has answered. While files conflict, Phase 1 reads every file from the pinned PR head (`git show <PR head oid>:<file>`), never the half-merged disk copy — one consistent snapshot whenever 7b commits (`dispatch-phase1.md`).

**Finding collection is one stream.** Everything up to the Step 3d picker (intel, merge findings, table) runs with no conflict agent in flight, so no agent notification or tool output interleaves with the `AskUserQuestion` call that `enforce-resolve-table.js` checks. Conflict agents once ran beside that call and the required table failed to land often enough to cost the picker a retry; the overlap moved behind the gate, where the user is not waiting on a table.

Two dependencies bound the Run 2 fan. **Step 6a consumes the contribution motivation that `INTEL_AGENT` synthesizes** (`pr-intelligence.md`: PR body is stated intent, thread is the authoritative record) — its envelope is long in by Run 2, and a git-log-only lens is never a fallback. **C1 and Phase 2 need a committed merge and a clean tree**, so they wait for the Step 7b join; items C1 may take (`medium` effort with the bridge available) are held out of the early challenge wave and challenged late only if C1 hands them back.

Degenerate cases, all reducing to the old serial order with no special handling: `report` mode with no PR# skips Steps 4–7 entirely; zero conflicted files means Step 5 commits the merge itself, Step 7a spawns nothing and Phase 1 runs alone; `--no-challenge` or zero selected items leaves Step 7a alone in the fan; `--worktree` enters the worktree inside Step 4, so an `INTEL_AGENT` spawned moments earlier keeps writing to the absolute `IMPL_DIR` it was handed.

## Agent wait discipline — no polling, per-agent deadlines

<!-- policy-sibling: plugins/cc_develop/skills/_shared/agent-resolution.md (§Agent waits — no polling), plugins/cc_research/skills/_shared/agent-resolution.md (§Agent waits — no polling) — same agent_watch.py no-polling wait contract -->

Every spawned agent (intel, conflict, Phase 1 challenge, Phase 2 specialists, Step 9 QA/lint) runs in the background. Waiting on one is **never** a tool call:

- **Never** call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for a spawned agent — and no `sleep`, no poll loop, no no-op call, no "waiting" turn. Spawn, end the turn, resume on the completion notification (`rules/task-lifecycle.md` §After spawning).
- **Arm a deadline per agent.** In the same response as each spawn batch, write `$IMPL_DIR/agent-watch-<batch>.tsv` with the Write tool — one row per agent, `<name>\t<deliverable path, or - for an envelope-only agent>\t<deadline seconds>`. The file's write time is the spawn time, so no clock value is ever typed. Batch names and deadlines: `intel` 300 · `conflict` 900 · `challenge` 300 · `challenge-retry` 300 · `impl` 900 (rewrite per wave) · `impl-<group_tag>.link<k>` 900 (one file per chain link k≥2, never a rewrite; the spawn-base block also writes one, deadline 0 and any k, for a recorded link with no watch row — never launched, so it reads `timed_out` at once) · `qa` 900.
- **Check once per wake-up.** On every completion or idle notification, first persist any envelope the notification carried (where a step says to write it to a file), then run the block below once, before acting on any agent output, and act on every row: `done` → consume it · `timed_out` → ⏱ `timed_out` now and take the step's documented fallback · an agent whose notification arrived but whose row is still `pending`/`awaiting-envelope` (idle or finished without its deliverable) → ⏱ `timed_out` now, never wait for it further · rows still open with no notification → end the turn.
- Never ask the user whether to keep waiting, and never leave a stalled agent for the user to notice — a run the user returns to must already show every ⏱.
- A ⏱ only informs. It never answers, skips, or defaults a user question: every gate the step defines still fires on the timed-out path.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot check agent deadlines"; exit 1; }
python "$PLUGIN_ROOT/bin/agent_watch.py" --state-dir "$IMPL_DIR"  # timeout: 5000
```
