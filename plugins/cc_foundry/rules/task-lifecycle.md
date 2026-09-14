---
description: Task lifecycle sequencing — TaskUpdate ordering, frozen plan, subagent task prohibition, spawn slots, end-turn-after-spawn
paths:
  - '**'
---

## Task lifecycle sequencing

### TaskUpdate before long output

`TaskUpdate(status="completed")` fires **before** any long output block (audit report, calibration summary, release notes, multi-item list). A tool call placed after the block may never execute — compaction can fire mid-response, leaving the task "in_progress" forever.

Sequence: `TaskUpdate(completed)` → emit output. Never the reverse.

### Frozen plan during build

An approved `.plans/active/todo_*.md` or `plan_*.md` is read-only once implementation starts. Only a post-build sync step (move to `.plans/closed/results_*.md`) or explicit user-directed revision may edit it. An implementer that rewrites the spec to match what it built destroys the spec's value as an independent verification target. Build reveals the plan is wrong → stop, surface via `AskUserQuestion`; never edit the plan to fit.

### Subagent task prohibition

Tasks created inside a subagent are session-local — invisible in the parent `TaskList`, so useless for tracking.

Subagents never call `TaskCreate` or `TaskUpdate`. The orchestrator creates all tasks before the first `Agent()` spawn, and marks each teammate's task `completed` as its delta arrives — never batched at session end.

Every subagent spawn prompt includes:

```text
Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state.
```

### Spawn slots — three fields, no repeats

FleetView renders three columns per agent: `name`, `description`, prompt leading chars. Each holds different content. Filling all three from one string wastes two columns.

| Slot | Role | Carries | Cap |
| -- | -- | -- | -- |
| `name` | address | delta as a unique kebab-case handle — what `SendMessage` types: `review-arch`, `fix-auth-module` | one line |
| `description` | scope | what work this row does that its siblings do not — adds detail `name` lacks | 3–5 words |
| prompt line 1 | task | task statement; **only** slot carrying batch-shared target (PR, repo, branch, run) | ≤12 words |

`name` necessarily encodes the delta — it must be unique. `description` shares that stem and expands it; it never merely restates it.

Order every slot **delta-first**: whatever differs across this batch — dimension, directory, module, plugin, task ID, issue number. FleetView truncates the tail, never the head, so a batch differing only past the cut has no labels at all. Never buy room by dropping the delta. One spawn in the batch → nothing is shared, so the target *is* the delta and leads every slot.

**Pre-spawn check** (mandatory, cheap, per spawn): read the three strings side by side. Two fail conditions — `description` adds nothing `name` did not already say, or any slot carries a value every sibling also carries (the PR, the repo, the role word). Either → rewrite before spawning.

```text
✗ name: review-sw-engineer  description: sw-engineer review — PR #1424 roboflow/rf-detr  prompt: First run Bash export CSID=…
✓ name: review-arch         description: arch + SOLID audit   prompt: Review PR #1424 roboflow/rf-detr — architecture, SOLID, error paths
```

`description` is required — the tool rejects the call without it. Omitted `name` → harness assigns one and `SendMessage` cannot address that agent.

Boilerplate (`Task tracking:`, `Compact Instructions:`, TEAM_PROTOCOL read, run-dir preamble, envelope spec) goes **after** prompt line 1 — including a preamble a template calls "prepend to every prompt". Task line still first.

Slots and caps come from the live `Agent()` schema in context, not this table alone. Schema carries a field this rule omits → follow the schema, fix the rule. Never drop a field because the rule predates it.

### After spawning: end the turn

`Agent()` never blocks. Every spawn runs in the background; the harness re-invokes the orchestrator on completion. No `run_in_background` parameter, no blocking mode.

Nothing to wait through. **Spawn, finish the turn, stop.** The notification is the resume signal.

Forbidden while agents are in flight, every skill:

- No-op calls held open only to keep the turn alive — `Bash(true)`, `Bash(:)`, an `echo` nobody reads, a re-`ls` of a listed directory.
- Text-only waiting turns — "Waiting.", "Standing by.", "Still waiting.", "Waiting on the consolidator."
- Any `sleep`, foreground or backgrounded, and any `while`/`until` poll loop. Foreground `sleep` is harness-blocked; the loop never runs.
- Fixed-interval polling prose ("poll every 5 minutes", "check every `$MONITOR_INTERVAL` seconds") — an interval needs a clock the orchestrator does not have.

Permitted when a real signal is needed: **one** liveness probe per turn — a single `find <run-dir> -newer <sentinel>`, or one bounded `Monitor` call. Then end the turn again.

On the notification: read each spawned agent's output file. Empty or missing → `timed_out`, surface with ⏱, never silently omit.

> Full detail (worked ✓/✗ slot, spawn-prompt, and FleetView examples) in `_full/task-lifecycle.md`. Read before composing multi-agent spawn prompts:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/task-lifecycle.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/task-lifecycle.md"  # timeout: 5000
> ```
