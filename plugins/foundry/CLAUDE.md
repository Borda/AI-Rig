**Re: Compress markdown to caveman format**

## Workflow Orchestration

### 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- Use plan mode for verification steps, not just building
- Goes sideways → STOP, re-plan immediately
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy

- Use sub-agents liberally to keep main context clean
- Prefer specialised agents over general-purpose; offload research and exploration
- Run independent subtasks in parallel, not serially; one tack per sub-agent
- **Context discipline**: share only task-relevant context in spawn prompts — no unrelated history, no out-of-scope details
- Complex problems → throw more compute via sub-agents
- **File-based handoff**: 2+ analysis agents each write full output to file, return only compact JSON envelope — see `.claude/skills/_shared/file-handoff-protocol.md`

### 3. Self-Improvement Loop

- After ANY correction: update `.notes/lessons.md` with preventative rule
- Write rules that prevent same mistake; iterate until mistake rate drops
- Review memory and lessons at session start

### 4. Verification Before Done

- Never mark complete without proving it works — run tests, check logs, diff against main
- Ask "would a staff engineer approve this?"
- **Confidence scores**: request `## Confidence` block from every analysis agent (protocol in Output Standards); surface low confidence — never drop uncertain findings

### 5. Demand Elegance (Balanced)

- Non-trivial changes: pause, ask "is there a more elegant way?"
- Fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip for simple, obvious fixes — don't over-engineer

### 6. Autonomous Bug Fixing

Just fix it — use logs, errors, failing tests; no hand-holding, including CI

### 7. Agent Teamwork

- **Delegate to the right agent** — task has designated owner → hand it off; don't attempt yourself
- **No territorial behaviour** — never contradict or redo another agent's output; build on it or flag concern constructively
- **One voice per domain** — orchestrator picks one agent; others stay silent rather than competing

### 8. Background Agent Health Monitoring

Any orchestrator spawning background agents writing to run directory **must** monitor them — `/foundry:calibrate` is canonical; skills customize defaults via `<constants>` block.

**Protocol**:

1. Create per-agent checkpoint and record launch time: `LAUNCH_AT=$(date +%s); touch /tmp/<skill>-check-<id>`
2. Every **5 min**: `find <run-dir> -newer /tmp/<checkpoint> -type f | wc -l` — new files = alive; zero = stalled
3. **Hard cutoff: 15 min** of no file activity → timed out
4. **One extension (+5 min)** if `tail -20 <output_file>` explains delay — second unexplained stall = cutoff
5. On timeout: read `tail -100 <output_file>` for partial results; if none use `{"verdict":"timed_out"}`; surface with ⏱ — never omit

Skills may tighten (not loosen) these defaults in their own `<constants>` block.

## Pre-Authorized Operations

Operations in `settings.json` are pre-approved — execute directly. Operation not covered → restructure to match existing allow entry before requesting new permission; batch missing permissions into one ask.

**Tool efficiency rule** — native Claude tools (Read, Grep, Glob, Write, Edit, and others) always available, never need `settings.json` approval; use them first:

- Native tools purpose-built and auditable; Bash for operations they cannot do (run tests, git, system commands)
- Prefer N sequential native tool calls over one script; loop of 10 Reads beats heredoc needing approval
- Avoid `python3 << 'EOF' ... EOF` heredocs; use `python3 -c "..."` one-liners only when native tools cannot write back (e.g. JSON transforms)

## Agent Teams

Teams always user-invoked:

- **Models**: lead = session model; reasoning teammates (foundry:sw-engineer, foundry:qa-specialist, foundry:perf-optimizer, research:scientist) = `opus`; execution teammates (foundry:doc-scribe, foundry:linting-expert, oss:ci-guardian, research:data-steward, foundry:web-explorer) = `sonnet`; max 3–5
- **Protocol**: every spawn prompt must include `Read ~/.claude/TEAM_PROTOCOL.md and use AgentSpeak v2`; preserve file paths, errors, test results, task IDs; discard verbose output
- **Security**: `foundry:qa-specialist` auto-includes OWASP Top 10 — no separate security agent
- **File-based handoff applies in teams**: teammates writing parallel analysis still follow §2 file-handoff protocol — compact JSON envelope back to lead, full output to file

## Task Management

### File-based tracking

1. Plan in `.plans/active/todo_<name>.md`; check in before starting
2. On approval → TaskCreate for each phase; mark complete as you go
3. Document results in `.plans/closed/results_<name>.md`; capture lessons → see §3 Self-Improvement Loop

### Session-start hygiene

**First action of every interaction**: call `TaskList`, triage all found tasks before any work:

- Work clearly done → `TaskUpdate` status `completed`
- Orphaned / no longer relevant → `TaskUpdate` status `deleted`
- Genuinely continuing from prior session → keep, mark `in_progress`

Prevents zombie tasks accumulating across sessions and showing false progress.

### In-session task tracking

- **Skills with predefined workflow**: TaskCreate all steps at start — before any tool calls; keep list current as work evolves
- **Multi-step work** (3+ tool calls or 2+ distinct instructions) → TaskCreate before first tool call, including on plan-mode exit
- On pivot → new task for new work; TaskUpdate existing if scope changed
- Mark complete before final output; keep statuses current — it's a live feed
- Skip for: single-task actions, simple skills (sync, distill), transient subagents

### Safety breaks for loops

- Default max 3 iterations
- At limit: stop, report progress, ask user whether to continue or re-scope
- Skill-declared bounds take precedence

## Self-Setup Maintenance

See `.claude/rules/foundry-config.md` for `.claude/` editing checklist (plan mode gate, post-edit steps, XML conventions, sync). See `.claude/rules/claude-config.md` for universal Bash timeout and directory navigation rules.

## Communication

See `.claude/rules/communication.md` for Re: anchor format, progress narration, tone, output routing, breaking findings, and terminal colors.

## External Data & APIs

See `.claude/rules/external-data.md` for pagination rules, completeness requirements, and `gh` CLI usage.

## Output Standards

See `.claude/rules/quality-gates.md` for Confidence block format, Internal Quality Loop, link verification, and output routing rules.

## Compact Instructions

When context compacted, preserve in summary:

1. Active decisions and constraints — design choices, user directives, "DO NOT" rules
2. Current task state — phase/step active, what remains
3. File modification history — which files changed and why
4. Pending follow-ups — deferred items, open questions, next steps

After compaction, re-read `.claude/state/session-context.md` if it exists.

## Core Principles

- **Simplicity First**: touch only what's necessary; smallest change that works
- **No Laziness**: find root causes; no temporary fixes; senior developer standards
- **Reversibility check**: before any action that cannot restore to pre-session state (deleting pre-existing files, pushing, dropping tables, external messages), pause — confirm scope matches what was asked; prefer reversible alternatives
- **Tool-first**: use declared tools fully and creatively — if tool can do job indirectly, use it
