# Agent Spawn Protocol — Health Monitoring (CLAUDE.md §6)

Reference from any skill that spawns agents — resolve `$_FOUNDRY_SHARED` and load in the same bash block:

```bash
cat "$_FOUNDRY_SHARED/agent-spawn-protocol.md"
```

Apply monitoring for `<skill-name>` run.

Claude Code harness runs one Bash call at a time (max ~10 min per call, foreground `sleep` blocked). Skill therefore **cannot** sit in a `while true; do sleep … done` poll loop waiting on an agent — that loop never runs. Monitoring is event-driven and post-hoc, not a busy-wait.

## Every spawn is a background spawn

`Agent()` does not block. There is no `run_in_background` parameter and no synchronous mode — the call returns immediately and the harness re-invokes the orchestrator with a **completion notification** when the agent finishes.

1. Spawn, finish the turn, **stop**. The notification is the resume signal; there is nothing to wait through.
2. Never hold the turn open with no-op calls (`Bash(true)`, `Bash(:)`, a re-`ls`), text-only "Waiting."/"Standing by." turns, any `sleep`, or a fixed-interval poll. Each one is a full model turn that re-reads the whole live context to produce nothing.
3. Need a real signal between turns? **One** probe per turn — the `Monitor` tool, or a single `health_sentinel.py`/`find` call (§8b) — then end the turn again.
4. On the notification: read the agent's output file. Empty or missing → mark `timed_out` with ⏱ and record `{"verdict":"timed_out"}`; never silently omit a stalled agent.

Skill prose still claiming spawns are "synchronous" or that "the framework awaits each response natively" describes a harness that no longer exists; this protocol is current. Canonical rule: `rules/task-lifecycle.md` §After spawning: end the turn.

## §8b health_sentinel.py liveness helper (optional)

For a single between-turn liveness probe on a background run, `health_sentinel.py` validates run dir and emits a quoted sentinel path:

```bash
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/health_sentinel.py" start <SKILL>-<ID> 2>/dev/null)"  # timeout: 5000
[ -n "$SENTINEL" ] || printf "⚠ health monitoring disabled — health_sentinel.py missing or failed\n"
```

Later (a separate Bash call — e.g. after a completion notification), probe progress: `find <output-dir> -newer "$SENTINEL" -name "<glob>" | wc -l` — new files since sentinel = progress. Shell state does not persist across Bash calls, so persist the sentinel path to a file if a later turn needs it.

## Resume vs fresh spawn — context cost discipline

`SendMessage({to: <agentId>})` resumes an agent from its **full prior transcript** — every follow-up round re-sends everything the agent ever read/wrote, so cost grows with the transcript, not the new task (observed: a 4th follow-up round on an accumulated agent cost ~650K subagent tokens vs ~200K for the same-scope task run as a fresh spawn). It also risks the agent reasoning off stale context from earlier rounds instead of current on-disk state.

**Default: one task, one spawn.** When a spawned agent finishes and reports its result, treat it as done — do not keep it around "in case." A new, independent follow-up task (even on the same files, even moments later) gets a **fresh** `Agent()` call with a self-contained prompt: current file paths + the specific task, not a reference to "what you just did." The fresh agent re-reads current on-disk state itself, which is also strictly more correct than trusting a stale in-context copy.

**When resume is actually right**: genuinely sequential/incremental work where the agent's own accumulated reasoning state is the point (e.g. an iterative refinement loop the agent is mid-way through, or a multi-part task deliberately split into hand-offs to keep each turn's prompt small). Even then, each hand-off must compact hard — send only the delta/new instruction, never re-paste prior findings the agent already has in its transcript — so the resumed context doesn't balloon with repeated, increasingly outdated material across rounds.

- If unsure whether a follow-up is "the same task continuing" or "a new independent task" — default to fresh. A fresh spawn that re-derives something the old agent already knew is far cheaper than an old agent quietly reasoning off page-3 assumptions that page-40 already invalidated.

## Delegation cost discipline — batch trivial work, don't spawn per finding

Every `Agent()` call pays fixed overhead (context load, model inference) regardless of how small the task is — a one-line typo fix costs nearly the same spawn overhead as a real logic fix. Two failure modes to avoid:

1. **One spawn per trivial finding.** A batch of independent, low-risk findings (typo, hardcoded path, missing frontmatter field, stale version ref, duplicate lines — the mechanical categories a skill's own `PARALLEL_SAFE_CATEGORIES` list already names) does not need one agent call per finding, or even one per file, when several land in the same or related files. Batch them into a single spawn prompt covering the whole set; let the agent apply all of them in one pass.
2. **Full adversarial review overhead on work with no real ambiguity.** A dual-agent challenge-and-validate gate (e.g. `foundry:challenger` + `foundry:curator` both reviewing before a fix lands) exists to catch fixes that might silently remove load-bearing content or misjudge intent — real risk on CRITICAL/HIGH or cross-file-dependent findings. Applying that same two-agent gate to a mechanical, unambiguous, single-file substitution is cost without signal. Skills defining a fix-apply gate should carve out a fast path: findings in the mechanical/parallel-safe category class skip the full gate and go straight to one fix agent (still never inline-edited by the orchestrator — §Fix Action Hierarchy in `audit/modes/fix.md` still applies); reserve the full adversarial gate for findings with real correctness or scope risk.

**Model/agent tier**: match the agent to the task, not the task to the agent. A narrow, well-specified, single-file mechanical fix is a good fit for `bridge:implement` when `bridge@borda-ai-rig` is available and the brief names the exact finding, target file, current evidence, permitted edit, expected result, stop condition, and verification command. Reserve the expensive agent for findings that actually need judgment (ambiguous intent, cross-file reconciliation, behavioral-risk calls).

Where this doesn't apply: genuinely cross-file-dependent fixes (must read another file to get the fix right), or findings flagged CRITICAL/HIGH where a wrong fix has real cost — these keep the full gate and appropriate agent tier regardless of how "small" the diff looks.

## Rules

- Never omit the timed-out signal (⏱) — surface partial results always
- Rely on the harness completion notification, not a busy-wait loop
- New independent follow-up task → fresh `Agent()` spawn, not `SendMessage`-resume (see above)
- Batch independent trivial/mechanical findings into one spawn; don't pay per-finding agent overhead for unambiguous, low-risk fixes (see §Delegation cost discipline)
- Canonical reference: CLAUDE.md §6
