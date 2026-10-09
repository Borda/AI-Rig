---
description: Task lifecycle sequencing — TaskUpdate ordering, frozen plan, subagent task prohibition; spawn-slot and end-turn-after-spawn stubs (full rule in agent-spawn.md)
paths:
  - '**'
---

## Task lifecycle sequencing

### TaskUpdate before long output

`TaskUpdate(status="completed")` fires **before** any long output block (audit report, calibration summary, release notes, multi-item list). A tool call placed after the block may never execute — compaction can fire mid-response, leaving the task "in_progress" forever.

Sequence: `TaskUpdate(completed)` → emit output. Never the reverse.

**Exception — report-print tasks** (e.g. `Step 5b: Print report header`): their `completed` means "the table is visible", so it goes after the table text, in the same response. A lost update costs only a stale status; delivery itself is hook-checked on `Stop`. Marking it first is how an incident shipped a reply with no header table.

This is the **one** sanctioned bookkeeping-only response. Everywhere else, a `TaskCreate`/`TaskUpdate` rides along with the next substantive tool call — zero bookkeeping-only turns (`CLAUDE.md` §Task Management ▸ In-session task tracking, with the measured turn cost). Ordering rule, not a licence.

### Frozen plan during build

An approved `.plans/active/todo_*.md` or `plan_*.md` is read-only once implementation starts. Only a post-build sync step (move to `.plans/closed/results_*.md`) or explicit user-directed revision may edit it. An implementer that rewrites the spec to match what it built destroys the spec's value as an independent verification target. Build reveals the plan is wrong → stop, surface via `AskUserQuestion`; never edit the plan to fit.

### Subagent task prohibition

Tasks created inside a subagent are session-local — invisible in the parent `TaskList`, so useless for tracking.

Subagents never call `TaskCreate` or `TaskUpdate`. The orchestrator creates all tasks before the first `Agent()` spawn, and marks each teammate's task `completed` as its delta arrives, in the same response that consumes the delta — never batched at session end, never a turn of its own.

Every subagent spawn prompt includes:

```text
Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state.
```

### Spawn slots — three fields, no repeats

<!-- policy-sibling: plugins/cc_foundry/rules/agent-spawn.md (§Spawn slots) -->

Every `Agent()` call: `name` = unique kebab-case delta handle; `description` = 3–5-word scope adding what `name` lacks; prompt line 1 = task, delta first, ≤12 words, never opening with batch-shared text (PR, repo, verb, role word). Compose the whole batch's labels in one pass and pre-spawn check them side by side; binds authored `Agent(...)` templates in skill/mode files too. Full rule: `agent-spawn.md` — injected by `rule-inject.js` on the first `Agent()` call of a session or subagent, loaded on first access of a skill or agent file.

### After spawning: end the turn

<!-- policy-sibling: plugins/cc_foundry/rules/agent-spawn.md (§After spawning) -->

Spawn, end the turn, resume on the completion notification — no no-op call, waiting turn, `sleep`, poll loop, or waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`). Per-agent deadline: write `agent-watch-<batch>.tsv` in the spawn response, run `agent_watch.py` once per notification; a notification without its deliverable is `timed_out` at once — surface with ⏱, never wait for it further, never silently omit. Full rule: `agent-spawn.md`.
