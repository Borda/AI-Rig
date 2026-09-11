---
description: Task lifecycle sequencing — TaskUpdate ordering, subagent task prohibition, spawn-prompt lead line, end-turn-after-spawn
paths:
  - '**'
---

## Task lifecycle sequencing

### TaskUpdate before long output

Call `TaskUpdate(status="completed")` **before** any long output block (audit report, calibration summary, release notes, multi-item list). Tool calls placed after long output block may never execute if context compaction fires mid-response, leaving tasks permanently "in_progress".

Correct sequence: `TaskUpdate(completed)` → emit output. Wrong: emit output → `TaskUpdate(completed)`.

### Frozen plan during build

Once a `.plans/active/todo_*.md` or `plan_*.md` is approved and implementation starts, treat it as read-only for implementation-phase agents — only a post-build sync step (move to `.plans/closed/results_*.md`, or explicit user-directed plan revision) may edit it. An implementer that can silently rewrite the spec to match whatever it built defeats the spec's purpose as an independent verification target. If the build reveals the plan itself is wrong, stop and surface via `AskUserQuestion` rather than quietly editing the plan to fit.

### Subagent task prohibition

Tasks created inside subagents are session-local — invisible in parent `TaskList`. Never useful for tracking.

Subagents must NOT call `TaskCreate` or `TaskUpdate`. Orchestrator creates all tasks before first `Agent()` spawn.

Subagent spawn prompts must include:

```text
Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state.
```

Orchestrator: mark each teammate's task `completed` as its delta arrives — never batch at session end.

### Spawn-prompt lead line

FleetView/agent-list shows leading chars of the `Agent()` prompt as each agent's description — Agent tool has no separate description field. Boilerplate-first spawn prompt → every agent reads identical useless label.

Rule: **first line = concise task label**, ≤10 words, no boilerplate. Place all boilerplate (`Task tracking:`, `Compact Instructions:`, TEAM_PROTOCOL read, run-dir preamble, envelope spec) **after** the task line — including a preamble a template calls "prepend to every prompt"; the label still goes first.

### Fleet-view description: unique-first

FleetView truncates the tail, never the head, and caps each row at one terminal line. So the label is ordered by what distinguishes the row, not by what reads naturally:

1. **Delta first** — whatever differs between the spawns in this batch: the dimension, the directory, the module, the plugin, the task ID.
2. **Shared context after, and only if it fits** — the target N agents have in common (the PR, the repo, the branch, the run) is context, never the label. One spawn in the batch → nothing is shared, so the target *is* the delta and leads.
3. Cut from the tail when over budget. Never buy room by dropping the delta.

A batch whose rows differ only past the truncation point has no labels at all.

### After spawning: end the turn

`Agent()` does not block. Every spawn runs in the background and the harness re-invokes the orchestrator with a completion notification — there is no `run_in_background` parameter to choose otherwise, and no blocking mode to fall back on.

So there is nothing to wait through. **Spawn, finish the turn, stop.** The notification is the resume signal.

Forbidden while agents are in flight, in every skill:

- No-op tool calls issued only to hold the turn open — `Bash(true)`, `Bash(:)`, an `echo` nobody reads, a re-`ls` of a directory already listed.
- Text-only turns that announce waiting — "Waiting.", "Standing by.", "Still waiting.", "Waiting on the consolidator."
- Any `sleep`, foreground or backgrounded, and any `while`/`until` poll loop. Foreground `sleep` is blocked by the harness; the loop never runs.
- Fixed-interval polling prose ("poll every 5 minutes", "check every `$MONITOR_INTERVAL` seconds"). An interval needs a clock the orchestrator does not have.

Permitted between turns, when a real signal is needed: **one** liveness probe per turn — a single `find <run-dir> -newer <sentinel>`, or one bounded `Monitor` call. One probe, then end the turn again.

On the notification: read each spawned agent's output file. Empty or missing → `timed_out`, surface with ⏱, never silently omit.

> Full detail (worked ✓/✗ spawn-prompt and FleetView-label examples) in `_full/task-lifecycle.md`. Read before composing multi-agent spawn prompts:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/task-lifecycle.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/task-lifecycle.md"  # timeout: 5000
> ```
