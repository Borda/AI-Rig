<!-- source: plugins/cc_foundry/CLAUDE.src.md → ~/.claude/CLAUDE.md via /foundry:setup Step 10 | NOT auto-loaded from cache (non-CLAUDE.md name intentional) -->

## Workflow Orchestration

### 1. Plan Mode Default

- Plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- Plan mode for verification steps too, not just building
- Goes sideways → STOP, re-plan now
- Detailed specs upfront cut ambiguity

### 2. Subagent Strategy

- Spawn sub-agents for isolation-motivated work (distinct role/system-prompt, adversarial check, model tier, worktree) regardless of size, and for work-displacement past the measured ~73-tool-call break-even — see `rules/claude-config.md` §Agent/Skill Spawn Discipline. Below that threshold with no isolation need: do it inline, spawn nothing
- Prefer specialised agents over general-purpose once a spawn is warranted; offload research + exploration when it clears the threshold above
- **Always pass explicit `subagent_type` matching the task**, even when the right specialist is already named in your own spawn prompt — §Agent Teams (below) is a separate, narrower gate (formal multi-agent Team protocol only); it does NOT restrict picking a specialist for an ordinary single-agent spawn, ad-hoc or background included. Omitting `subagent_type` defaults to `general-purpose` and hides the spawn from 🤖 status tracking. (Telemetry evidence + worked failure example: `rules/_full/CLAUDE-full.md` §Subagent Strategy.)
- Independent subtasks run parallel, not serial; one tack per sub-agent
- **Context discipline**: spawn prompt = task inputs + instructions only. Include: working dir · input paths/vars · output target · return envelope format. Exclude: session history · prior-phase reasoning · inline file contents (pass path)
- Complex problem → more compute via sub-agents, but only on genuinely independent, sizeable tracks. Never delegate work finishable in a handful of tool calls, never spawn several where one suffices, and **never spawn a subagent to verify or double-check own work** — current models self-verify natively; a verifier spawn compounds that behaviour instead of adding signal. Deterministic caps: `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` (Claude Code ≥ 2.1.217)
- **File-based handoff**: 2+ analysis agents each write full output to file, return only compact JSON envelope — protocol in foundry's own shared dir, resolved via `bin/resolve_shared_path.py foundry skills/_shared` then `cat "$_FS/file-handoff-protocol.md"`

### 3. Self-Improvement Loop

- After ANY correction: update `.notes/lessons.md` with preventative rule
- Rules must prevent same mistake; iterate until mistake rate drops

### 4. Verification Before Done

- Never mark complete without proof — run tests, check logs, diff against main. Proof means **external evidence** (command output, disk state, test result), never a re-read of own reasoning
- Ask "would staff engineer approve?"
- **Diff against ask**: before done, diff output against literal request — every constraint honored, nothing silently dropped
- **Confidence scores**: request `## Confidence` block from every analysis agent (protocol in Output Standards); surface low confidence — never drop uncertain findings
- **No added self-recheck passes, except one gated on a named, checkable trigger** (e.g. a scope-boundary re-scan against a stated rule — targeted verification, not blanket re-verify). Current models mostly catch and fix their own mistakes without an unscoped re-verify prompt; instructions like "double-check your answer" or "re-verify before responding" compound with that behaviour and cost tokens without reliably improving results. Run the external proof above once, then report

### 5. Autonomous Bug Fixing

Trivial/mechanical (typo, single-file): fix it — logs, errors, failing tests; no hand-holding. Multi-file or behaviour-changing: Root Cause protocol (`rules/debugging.md`).

### 6. Agent Health Monitoring

Harness runs one Bash call at a time (foreground `sleep` blocked, ~10 min per-call cap) — skill **cannot** busy-wait polling an agent. Monitoring event-driven, post-hoc.

**Protocol**:

- **Every spawn is background.** `Agent()` never blocks; no `run_in_background` parameter exists. Spawn, **end the turn**, resume on the harness completion notification.
- **Never hold the turn open**: no no-op call (`Bash(true)`, `Bash(:)`, re-`ls`), no text-only "Waiting."/"Standing by." turn, no `sleep`, no fixed-interval poll. Each costs a full model turn and returns nothing.
- **Optional between-turn liveness**: `Monitor` tool, or a single `find <run-dir> -newer <sentinel>` probe per turn — one probe, then end the turn again.
- **On notification**: read agent output file; empty or missing → mark `timed_out`, record `{"verdict":"timed_out"}`, surface with ⏱ — never omit stalled agent.

Canonical helper: `_FOUNDRY_SHARED/agent-spawn-protocol.md`. Skills may tighten timeouts in own `<constants>` block.

### 7. Context Cost Discipline

- Cache-read cost = live context size × turn count — dominant cost line; keep both small
- Multi-phase skill runs (e.g. review → resolve): after phase report file written, `/compact` before the next phase; resume from report file, not transcript
- **Never suggest `/clear`.** It discards the prompt cache, so the next call re-writes the full context at the cache-write rate — ~12.5× the read rate — and buys nothing back, because the rebuilt context is the same size. Measured 2026-08-08 on a `/oss:review`: one mid-run `/clear` cost 179,545 write tokens in a single call, 46% of that session's entire cache-write spend. **`/compact` is the tool for every case** — it pays the same rebuild once, then shrinks what is re-sent on every remaining turn. Measured 2026-08-08 across 41 real compactions in 5 sessions (`bin/cost_analyzer.py`, drop-detection method — a compaction is any call whose post-call context falls below 70% of the prior call's): median context shrink 70%, median rebuild cost $0.87, median break-even **~2 turns**, worst observed 14 turns. All 41 repaid before their session ended. Break-even scales with pre-compaction context size, not a fixed dollar figure — it is worth checking only in the closing turns of a session, never mid-run
- Live context past ~40% of the model's window = smell — wrap phase, persist state to file, restart lean. Current 5-family models carry a 1M window (Haiku 4.5: 200K), and instruction-following holds across it, so the smell is **cost**, not capability: cache-read scales linearly with live size, and `autoCompactThreshold: 0.7` only fires at ~700K
- Batch tool calls: create all tasks in ONE response (parallel calls); pair `TaskUpdate` with next substantive tool call — never emit response with only task bookkeeping

## Pre-Authorized Operations

Operations in `settings.json` pre-approved — execute direct. Not covered → restructure to match existing allow entry before requesting new permission; batch missing permissions into one ask.

- **Plugin binary via `${CLAUDE_PLUGIN_ROOT}/bin/X`** (quoted, resolves to absolute versioned cache path) never matches bare `Bash(X:*)` allow entry — matcher compares literal command string, not basename. Re-prompts every call despite an existing bare-name allow entry for `X`. Fix at plugin's `permissions-allow.json`: path-scoped glob (`Bash(*/<plugin>/*/bin/*:*)`), not bare command name — survives version bumps too. Confirmed 2026-08-08 (codemap-py `scan-index`).

**Tool efficiency rule** — native Claude tools (Read, Grep, Glob, Write, Edit, others) always available, never need `settings.json` approval; use first:

- Native tools purpose-built, auditable; Bash for what they cannot do (run tests, git, system commands)
- Prefer N sequential native tool calls over one script; loop of 10 Reads beats heredoc needing approval
- Avoid `python << 'EOF' ... EOF` heredocs; `python -c "..."` one-liners only when native tools cannot write back (e.g. JSON transforms)

## Agent Teams

Teams always user-invoked — this gate is scoped to the formal multi-agent Team protocol below (model tiering, TEAM_PROTOCOL.md, AgentSpeak v2); it does not apply to picking a specialist for an ordinary single-agent spawn — that's §2 Subagent Strategy, always in effect:

- **Models**: lead = session model; reasoning (foundry:sw-engineer, foundry:perf-optimizer, research:scientist) = `opus`; execution (foundry:qa-specialist, foundry:doc-scribe, foundry:linting-expert, oss:cicd-steward, research:data-steward, foundry:web-explorer) = `sonnet`; max 3–5
- **Protocol**: every spawn prompt must include `Read ~/.claude/TEAM_PROTOCOL.md and use AgentSpeak v2`; preserve file paths, errors, test results, task IDs; discard verbose output
- **Security**: `foundry:qa-specialist` auto-includes OWASP Top 10 — no separate security agent
- **File-based handoff in teams**: teammates writing parallel analysis follow §2 file-handoff protocol — compact JSON envelope back to lead, full output to file

## Task Management

### File-based tracking

1. Plan in `.plans/active/todo_<name>.md`; check in before start
2. On approval → TaskCreate each phase; mark complete as you go
3. Document results in `.plans/closed/results_<name>.md`; capture lessons → §3 Self-Improvement Loop

### Session-start hygiene

**First action every interaction**: call `TaskList`, triage all found tasks before work:

- Work clearly done → `TaskUpdate` status `completed`
- Orphaned / irrelevant → `TaskUpdate` status `deleted`
- Genuinely continuing prior session → keep, mark `in_progress`

Stops zombie tasks piling up across sessions, showing false progress.

### In-session task tracking

- **Skills with predefined workflow**: TaskCreate all steps at start — before any tool calls; keep list current
- **Multi-step work** (3+ tool calls or 2+ distinct instructions) → TaskCreate before first tool call, including on plan-mode exit
- On pivot → new task for new work; TaskUpdate existing if scope changed
- Mark complete before final output; keep statuses current — live feed
- Skip for: single-task actions, simple skills (sync, distill), transient subagents

### Safety breaks for loops

- Default max 3 iterations
- At limit: stop, report progress, ask user continue or re-scope
- Skill-declared bounds win

## Self-Setup Maintenance

`.claude/rules/foundry-foundry-config.md` = `.claude/` editing checklist (plan mode gate, post-edit steps, XML conventions, sync). `.claude/rules/foundry-claude-config.md` = universal Bash timeout + directory navigation rules.

## Compact Instructions

When context compacted, preserve in summary:

1. Active decisions + constraints — design choices, user directives, "DO NOT" rules
2. Current task state — active phase/step, what remains
3. File modification history — which files changed, why
4. Pending follow-ups — deferred items, open questions, next steps

After compaction, re-read `.claude/state/session-context.md` if exists. `## Skill Compaction Contract` section present → verbatim skill hand-off from PreCompact hook (hook reads the live contract from `.temp/state/skill-contract.md`, written by the active skill at phase boundaries) — treat `preserve:`, `run-dir`, `next` fields as authoritative for resuming active skill; never paraphrase or summarize away.

## Core Principles

- **Simplicity First**: touch only necessary; smallest change that works
- **No Laziness**: find root causes; no temp fixes; senior developer standards
- **Root Cause**: post-fix verify all symptoms resolved; symptoms remain → root cause incomplete; loop (max 3, then AskUser). `foundry:challenger` is **stakes-gated, not routine** — dispatch for user-visible or hard-to-reverse changes, or when the fix rests on a premise still unproven; skip it for ordinary multi-file work the fix's own tests already cover. Protocol: `rules/debugging.md`.
- **Proportionality**: reasoning depth, length, tool count scale to stakes — over-delivery = failure mode equal to under-delivery (buries signal, wastes tokens); trivial ask → direct answer, zero ceremony
- **Legible deviation**: every filled assumption, bent instruction, corrected error stated explicit — user never discovers changes by diffing output against request
- **Reversibility check**: before action that cannot restore pre-session state (deleting pre-existing files, pushing, dropping tables, external messages), pause — confirm scope matches ask; prefer reversible alternatives
- **Tool-first**: use declared tools fully, creatively — tool can do job indirectly → use it
