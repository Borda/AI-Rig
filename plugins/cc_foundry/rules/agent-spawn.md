---
description: Agent() spawn rules — three-slot delta-first labels, end the turn after spawning with per-agent deadlines, parallel spawn ceilings by model tier; injected by rule-inject.js on the first Agent() call of a session or subagent, loads on first access of a skill or agent file
paths:
  - '**/skills/**/*.md'
  - '**/agents/**/*.md'
---

> Before composing spawn prompts or applying spawn ceilings, read the binding extracted sections. Resolve with active Foundry’s `bin/resolve_plugin_root.py --plugin-name foundry` helper (registry preferred); set `FOUNDRY_ROOT` to the result in the same Bash call. Never select a different installed version by mtime. For repository maintenance only, use source-tree `plugins/cc_foundry`. Unreadable detail → stop and report.
>
> ```bash
> RULE_FULL="$FOUNDRY_ROOT/rules/_full/agent-spawn.md"  # timeout: 5000
> cat "$RULE_FULL"  # timeout: 5000
> ```

## Spawn slots — three fields, no repeats

<!-- policy-sibling: plugins/cc_foundry/rules/task-lifecycle.md (§Spawn slots stub) -->

> Before applying, Read `_full/agent-spawn.md` §Spawn slots — three fields, no repeats; binding inline. If it cannot be read, stop and report the missing prerequisite.

`Agent()` slots: `name` = unique kebab-case delta handle; `description` = required 3–5-word scope adding detail; prompt line 1 = rendered task label, ≤12 words, delta first. Shared context belongs only after the delta in prompt line 1, never in other slots. Compose the entire batch together; mandatory side-by-side pre-spawn check rejects repeated shared text, redundant descriptions or shared opening words. Boilerplate follows line 1. Omitted `name` prevents addressing by `SendMessage`; follow the live schema and fix this rule if it differs. Authored skill/mode templates obey the same discipline at every batch-spawn site. Worked triples remain in `_full/task-lifecycle.md` §Spawn slots.

## After spawning: end the turn

<!-- policy-sibling: plugins/cc_foundry/rules/task-lifecycle.md (§After spawning stub) -->

`Agent()` never blocks. Every spawn runs in the background; the harness re-invokes the orchestrator on completion. No `run_in_background` parameter, no blocking mode.

Nothing to wait through. **Spawn, finish the turn, stop.** The notification is the resume signal.

Forbidden while agents are in flight, every skill:

- No-op calls held open only to keep the turn alive — `Bash(true)`, `Bash(:)`, an `echo` nobody reads, a re-`ls` of a listed directory.
- Text-only waiting turns — "Waiting.", "Standing by.", "Still waiting.", "Waiting on the consolidator."
- Any `sleep`, foreground or backgrounded, and any `while`/`until` poll loop. Foreground `sleep` is harness-blocked; the loop never runs.
- Fixed-interval polling prose ("poll every 5 minutes", "check every `$MONITOR_INTERVAL` seconds") — an interval needs a clock the orchestrator does not have.
- Any waiting tool — `ScheduleWakeup`, `ListAgents`, `Monitor` — and any liveness probe (`find <run-dir> -newer <sentinel>`) used to wait on a spawned agent.

Instead, a **per-agent deadline**: in the spawn response, write the batch's `agent-watch-<batch>.tsv` (one row per agent: name, deliverable, deadline seconds); on every completion or idle notification, run `agent_watch.py` once and act on every row (`_shared/agent-spawn-protocol.md` §Deadlines).

On the notification: an agent whose notification arrived without its deliverable (empty or missing output file, idle without its envelope) is `timed_out` at once — surface with ⏱, never wait for it further, never silently omit. A ⏱ only informs: it never answers, skips or defaults a user question, and never prompts one asking whether to keep waiting.

## Parallel Spawn Ceilings — by Model Tier

<!-- policy-sibling: plugins/cc_foundry/rules/claude-config.md (§Parallel Spawn Ceilings stub) -->

> Before applying, Read `_full/agent-spawn.md` §Parallel Spawn Ceilings — by Model Tier; binding inline. If it cannot be read, stop and report the missing prerequisite.

Limit simultaneous in-flight `Agent()` calls across all due steps/phases by effective model, including overrides: `haiku` 20, `sonnet` 8, `opus` 5; separate pools, never a shared total. Grow toward a ceiling in waves of ≤5. This ceiling is separate from `claude-config.md` §Agent/Skill Spawn Discipline; cap each phase at its effective tier ceiling.
