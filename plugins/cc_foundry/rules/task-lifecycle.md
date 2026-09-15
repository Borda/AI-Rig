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

**The principle: a label's only job is to distinguish this row from its siblings.** Text repeated on every row of a batch carries zero information no matter how true or well-written it is — the reader already knows it, and it consumes the same display budget as text that would have told them something. A rendered row has room for one line; every character of shared text spends that room on nothing and pushes the distinguishing text past the truncation.

This is a property of the batch, not of any single spawn. The same string can be the perfect label for one agent and worthless for five, and nothing visible in one spawn's own arguments reveals which case it is. So the test is never "is this label accurate?" — it is "does this label differ from what the other rows will print?". Enumerated cases below (PR, repo, verb, role word) are the recurring instances, not the rule: anything shared is waste, including shared text these examples never name.

`Agent()` takes three label-bearing slots: `name`, `description`, prompt line 1. Each holds different content. Filling all three from one string wastes two of them.

**The rendered label is prompt line 1, not `description`.** Observed in a real FleetView pane: each row printed `name` plus the leading chars of prompt line 1; the `description` strings set on those same spawns (`arch + SOLID audit`, `coverage + OWASP scan`) did not appear in that pane at all. Treat prompt line 1 as the slot the user actually reads and the one truncation cuts. `description` may surface in other views — keep it correct — but never rely on it to differentiate rows.

| Slot | Role | Carries | Cap |
| -- | -- | -- | -- |
| `name` | address | delta as a unique kebab-case handle — what `SendMessage` types: `review-arch`, `fix-auth-module` | one line |
| `description` | scope | what work this row does that its siblings do not — adds detail `name` lacks | 3–5 words |
| prompt line 1 | task + visible label | task statement, **delta first**; **only** slot allowed to carry the batch-shared target (PR, repo, branch, run) — and only after the delta, never leading | ≤12 words |

`name` necessarily encodes the delta — it must be unique. `description` shares that stem and expands it; it never merely restates it.

Order every slot **delta-first**: whatever differs across this batch — dimension, directory, module, plugin, task ID, issue number. FleetView truncates the tail, never the head, so a batch differing only past the cut has no labels at all. Never buy room by dropping the delta. One spawn in the batch → nothing is shared, so the target *is* the delta and leads every slot.

**Compose the batch's labels in one pass, never one spawn at a time.** A per-spawn author cannot see what its siblings will say, so each one independently reaches for the same framing and the batch converges on an identical prefix. Write all `name`/`description`/prompt-line-1 triples together, before issuing any `Agent()` call, in three steps:

1. **Name the shared context once** — the PR, repo, branch, run, role word, and verb that every row in this batch would carry.
2. **Strike it from every label.** It belongs in the prompt body, which each agent reads in full; it never belongs in a slot that gets truncated.
3. **Keep only the delta** — the dimension, module, directory, finding ID, or issue number that answers "which of these rows is this one?". Read the finished triples as a column, top to bottom: if two rows share their opening words, the strike in step 2 was incomplete.

**Pre-spawn check** (mandatory, cheap, on the composed batch): read the three strings of every spawn side by side. Three fail conditions — `description` adds nothing `name` did not already say; any slot carries a value every sibling also carries (the PR, the repo, the role word); or prompt line 1 opens with anything shared across the batch, including the verb. Any of them → rewrite before spawning.

```text
✗ name: review-sw-engineer  description: sw-engineer review — PR #1424 roboflow/rf-detr  prompt: First run Bash export CSID=…
✗ name: review-arch         description: arch + SOLID audit   prompt: Review PR #1424 roboflow/rf-detr — architecture, SOLID, error paths
✓ name: review-arch         description: arch + SOLID audit   prompt: Architecture, SOLID, error paths — PR #1424 roboflow/rf-detr
```

The second line fails for the reason the first does, one slot over: `Review PR #1424 roboflow/rf-detr — ` is 34 chars every sibling row also prints, so the rendered column shows the same text on every row and the dimension falls past the cut.

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
