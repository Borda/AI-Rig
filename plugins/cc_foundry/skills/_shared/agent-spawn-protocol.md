# Agent Spawn Protocol — Health Monitoring (CLAUDE.md §6)

Reference for any spawning skill — resolve `$_FOUNDRY_SHARED`, load in same bash block:

```bash
cat "$_FOUNDRY_SHARED/agent-spawn-protocol.md"
```

Apply monitoring for `<skill-name>` run.

Harness runs one Bash call at a time (max ~10 min/call, foreground `sleep` blocked). Skill therefore **cannot** sit in a `while true; do sleep … done` poll loop waiting on an agent — that loop never runs. Monitoring event-driven and post-hoc, not busy-wait.

## Every spawn is a background spawn

`Agent()` never blocks. No `run_in_background` parameter, no synchronous mode — call returns immediately; harness re-invokes orchestrator with a **completion notification** when agent finishes (or an idle notification when a teammate stops).

1. Spawn, arm the batch's deadlines (§Deadlines) in the **same response**, finish turn, **stop**. Notification is the resume signal — nothing to wait through.
2. **Never wait with a tool.** No `ScheduleWakeup`, `ListAgents` or `Monitor` to wait on a spawned agent; no no-op calls (`Bash(true)`, `Bash(:)`, re-`ls`), text-only "Waiting."/"Standing by." turns, `sleep`, poll loop or fixed-interval poll. Each burns a full model turn re-reading whole live context for nothing — measured: 399 `ScheduleWakeup` calls across `/oss:resolve` runs in one month, while users typed "check on the agents".
3. On every completion or idle notification: one `agent_watch.py` call (§Deadlines) **before** acting on any agent output; act on every row it prints.
4. Notification arrived, deliverable missing (empty/missing output file, or idle without its envelope) → ⏱ `timed_out` **at once**, record `{"verdict":"timed_out"}`; never wait for it further, never silently omit it.
5. A ⏱ only informs. It never answers, skips or defaults a user question — every gate the skill defines still fires on the timed-out path.
6. Never ask the user whether to keep waiting; never leave a stalled agent for the user to notice — a run the user returns to already shows every ⏱.
7. Task bookkeeping rides with real work: the batch's `TaskUpdate(in_progress)` ships in the spawn response, each `TaskUpdate(completed)` in the response that consumes that agent's result — zero bookkeeping-only turns (`rules/task-lifecycle.md`).

Skill prose still claiming spawns are "synchronous" or "the framework awaits each response natively" describes a harness that no longer exists; this protocol is current. Canonical rule: `rules/task-lifecycle.md` §After spawning: end the turn.

## Deadlines — `agent_watch.py`

One deadline per agent, armed in the spawn response, checked at each wake-up — the orchestrator never needs a clock, a timer, or a poll.

- **Arm**: in the same response as the spawn batch, Write `<RUN_DIR>/agent-watch-<batch>.tsv` with the Write tool — one row per agent: `<name>\t<deliverable path, or - for an envelope-only agent>\t<deadline seconds>`. The file's write time is the batch's spawn time, so no clock value is ever typed; a write deferred to a later turn silently shifts every deadline — never defer it. Deadline seconds come from the skill's `<constants>` (default 900).
- **Check**: on each wake-up, re-read the run dir from the skill's own sentinel and run once, in the same block:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir "$RUN_DIR"  # timeout: 5000
```

- **Act on every row**: `done` → consume · `timed_out` → ⏱ now, take the step's documented fallback · `pending`/`awaiting-envelope` for an agent whose notification already arrived → ⏱ `timed_out` now (an envelope-only agent whose notification carried its envelope is done — persist the envelope first when the step says to) · rows still open with no notification → end the turn.
- A re-spawned agent gets a fresh row in a new batch file (`agent-watch-<batch>-retry.tsv`), never an edited row — editing rewrites the mtime of every sibling's deadline.

## Resume vs fresh spawn — context cost discipline

`SendMessage({to: <agentId>})` resumes an agent from its **full prior transcript** — every follow-up round re-sends everything the agent ever read/wrote, so cost grows with the transcript, not the new task (observed: 4th follow-up round on an accumulated agent cost ~650K subagent tokens vs ~200K for the same-scope task run as a fresh spawn). Also risks the agent reasoning off stale context from earlier rounds instead of current on-disk state.

**Default: one task, one spawn.** Spawned agent finishes and reports result → treat as done, don't keep it around "in case." A new, independent follow-up task (even same files, moments later) gets a **fresh** `Agent()` call with a self-contained prompt: current file paths + the specific task, not a reference to "what you just did." Fresh agent re-reads current on-disk state itself — strictly more correct than trusting a stale in-context copy.

**When resume is actually right**: genuinely sequential/incremental work where the agent's own accumulated reasoning state is the point (e.g. an iterative refinement loop the agent is mid-way through, or a multi-part task deliberately split into hand-offs to keep each turn's prompt small). Even then, each hand-off must compact hard — send only the delta/new instruction, never re-paste prior findings the agent already has in its transcript — so resumed context doesn't balloon with repeated, increasingly outdated material across rounds.

- Unsure whether a follow-up is "the same task continuing" or "a new independent task" — default to fresh. A fresh spawn that re-derives something the old agent already knew is far cheaper than an old agent quietly reasoning off page-3 assumptions that page-40 already invalidated.

## Delegation cost discipline — batch trivial work, don't spawn per finding

Every `Agent()` call pays fixed overhead (context load, model inference) regardless of task size — a one-line typo fix costs nearly the same spawn overhead as a real logic fix. Two failure modes to avoid:

1. **One spawn per trivial finding.** A batch of independent, low-risk findings (typo, hardcoded path, missing frontmatter field, stale version ref, duplicate lines — the mechanical categories a skill's own `PARALLEL_SAFE_CATEGORIES` list already names) does not need one agent call per finding, or even one per file, when several land in the same or related files. Batch into a single spawn prompt covering the whole set; let the agent apply all of them in one pass.
2. **Full adversarial review overhead on work with no real ambiguity.** A dual-agent challenge-and-validate gate (e.g. `foundry:challenger` + `foundry:curator` both reviewing before a fix lands) exists to catch fixes that might silently remove load-bearing content or misjudge intent — real risk on CRITICAL/HIGH or cross-file-dependent findings.
   - Applying the same two-agent gate to a mechanical, unambiguous, single-file substitution is cost without signal.
   - Skills defining a fix-apply gate should carve out a fast path: findings in the mechanical/parallel-safe category class skip the full gate and go straight to one fix agent (still never inline-edited by the orchestrator — §Fix Action Hierarchy in `audit/modes/fix.md` still applies); reserve the full adversarial gate for findings with real correctness or scope risk.

**Model/agent tier**: match the agent to the task, not the task to the agent. A narrow, well-specified, single-file mechanical fix fits `bridge:implement` when `bridge@borda-ai-rig` is available and the brief names the exact finding, target file, current evidence, permitted edit, expected result, stop condition, and verification command. Reserve the expensive agent for findings that actually need judgment (ambiguous intent, cross-file reconciliation, behavioral-risk calls).

Doesn't apply: genuinely cross-file-dependent fixes (must read another file to get the fix right), or findings flagged CRITICAL/HIGH where a wrong fix has real cost — these keep the full gate and appropriate agent tier regardless of how "small" the diff looks.

## Rules

- Never omit the timed-out signal (⏱) — surface partial results always
- Rely on the harness completion notification plus the per-agent deadline — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a busy-wait loop, or a no-op call
- New independent follow-up task → fresh `Agent()` spawn, not `SendMessage`-resume (see above)
- Batch independent trivial/mechanical findings into one spawn; don't pay per-finding agent overhead for unambiguous, low-risk fixes (see §Delegation cost discipline)
- Canonical reference: CLAUDE.md §6
