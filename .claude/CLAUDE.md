## Workflow Orchestration

### 1. Plan Mode Default

- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy

- Use sub-agents liberally to keep main context clean
- Prefer specialised agents (`sw-engineer`, `ai-researcher`, etc.) over general-purpose
- Offload research and exploration to sub-agents
- Run independent subtasks in parallel, not serially
- One tack per sub-agent — no multi-tasking within a single agent
- **File-based handoff**: when spawning 2+ analysis agents, each agent writes full output to a file and returns only a compact JSON envelope (~200 bytes) to the orchestrator — read `.claude/skills/_shared/file-handoff-protocol.md` for the protocol; `/calibrate` is the reference implementation
- For complex problems, throw more compute at it via sub-agents

### 3. Self-Improvement Loop

- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project
- For recurring low-confidence gaps: add the pattern to the agent's `<antipatterns_to_flag>` and run `/calibrate <agent>` to confirm improved recall

### 4. Verification Before Done

- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness
- **Confidence scores**: when spawning analysis agents, request a `## Confidence` block at the end of each response (protocol in Output Standards). Low confidence is signal — surface it; never drop uncertain findings.

### 5. Demand Elegance (Balanced)

- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes – don't over-engineer

### 6. Autonomous Bug Fixing

- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests – then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

### 7. Agent Teamwork

- **Delegate to the right agent** — when a task has a designated owner (e.g. `linting-expert`, `qa-specialist`, `doc-scribe`), hand it off; don't attempt it yourself
- **No territorial behaviour** — never contradict or redo another agent's output to assert ownership; build on it or flag a concern constructively
- **One voice per domain** — if two agents could both handle something, the orchestrator picks one; the other stays silent rather than competing

### 8. Background Agent Health Monitoring

Any orchestrator — main session or skill — that spawns background agents or actions with non-deterministic execution time **must** monitor them actively. Applies to: any orchestrator that spawns background agents writing output into a run directory — `/calibrate` is the canonical implementation; other skills implement the protocol in their own `<constants>` block when applicable.

**Protocol**:

1. Record launch time per agent (`LAUNCH_AT=$(date +%s)`) and create a per-agent file checkpoint (`touch /tmp/calibrate-check-<id>`)
2. Every **5 minutes**: run `find <run-dir> -newer /tmp/<checkpoint> -type f | wc -l` — new files = alive; no files = stalled
3. **Hard cutoff at 15 minutes** of no file activity — declare timed out, do not wait further
4. **One extension (+5 min)** granted if the agent's output file explicitly explains the delay (read via `tail -20 <output_file>`) (use `tail -100` when reading for partial results at final timeout) — a second unexplained stall still triggers the cutoff
5. On timeout: read output file for partial results; if none, construct `{"verdict":"timed_out","gaps":["re-run individually"]}` — always surface timed-out agents with ⏱ in the report; never silently omit them

**Skills with non-deterministic sub-agent execution inherit these defaults** and may tighten (not loosen) the constants in their own `<constants>` block.

## Agent Teams

Teams are always user-invoked. When executing in team mode:

- **Models**: lead = session model; reasoning teammates (sw-engineer, qa-specialist, perf-optimizer, ai-researcher) = `opus`; execution teammates (doc-scribe, linting-expert, ci-guardian, data-steward, web-explorer) = `sonnet`; max 3–5 teammates
- **Protocol**: every spawn prompt must include `Read .claude/TEAM_PROTOCOL.md and use AgentSpeak v2` + compact instructions (preserve: file paths, errors, test results, task IDs; discard: verbose tool output, handshakes)
- **Security**: `qa-specialist` as teammate auto-includes OWASP Top 10 checks for auth/payment/data scope — no separate security agent

## Task Management

### File-based tracking

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Plan approved → create tasks**: once the user approves the plan, convert each major phase into a live progress view (Applies to any agent — main session or spawned subagent)
4. **Track Progress**: Mark items complete as you go
5. **Explain Changes**: High-level summary at each step
6. **Document Results**: Add review section to `tasks/todo.md`
7. **Capture Lessons**: Update `tasks/lessons.md` after corrections

### In-session task tracking

- **Skills with a predefined multi-step workflow**: create TaskCreate entries for **all known steps immediately at skill start** — before any tool calls, before any analysis. The task list is the user's first view of the plan. Steps may be renamed or new ones added as the work evolves; the list must always reflect the current plan.
- Any multi-step main-session work (fix, investigation, debug — 3+ tool calls, or a message with 2+ distinct instructions) → TaskCreate at the start, before the first tool call; don't wait to understand the root cause
- **Plan-mode exit → task-list entry**: when exiting plan mode after user approval, the first action is always TaskCreate for each major phase — never start implementation without the task list in place
- On pivot (unplanned work discovered mid-skill) → create a new task for the new work; rename existing tasks with TaskUpdate if scope changed
- Skip for: single-task actions, simple skills (sync, observe), transient subagents (not team teammates with assigned tasks)
- Mark tasks complete before producing final output — TaskUpdate(completed) must come before the closing report/summary
- **The task list is a live feed for the user** — keep statuses current throughout execution, not just at start and end; the user watches it to know what is happening without asking

### Safety breaks for loops

- Default max: 3 iterations for any retry/loop
- At limit: stop, report progress, ask user whether to continue or re-scope
- If a skill already declares a bound (e.g. "max 2 re-audit cycles"), that bound takes precedence

## Self-Setup Maintenance

When modifying any file under `.claude/` (agents, skills, settings, hooks, this file):

1. **Update all cross-references** — agents reference each other by name (e.g. `sw-engineer` → `linting-expert`); if a name, scope, or capability changes, update every file that mentions it
2. **Update `memory/MEMORY.md`** — the agents/skills inventory line must stay in sync with what actually exists on disk
3. **Cross-check `README.md`** — after ANY change to a `.claude/` file, verify `README.md` reflects the change: agent/skill tables match files on disk, Status Line section matches `hooks/statusline.js` behavior, Config Sync section matches `skills/sync/SKILL.md`; keep descriptions and names accurate (no hardcoded counts — the tables are self-documenting)
4. **Update `settings.json` permissions** — if a skill or agent adds new `gh`, `bash`, or `WebFetch` calls, add the matching permission rule so it doesn't hit a prompt
5. **Keep `</workflow>` tags structural** — all mode sections in skill files must sit inside the `<workflow>` block; the closing tag goes after the last mode, before `<notes>`
6. **No orphaned step numbers** — if steps are added/removed in a skill workflow, renumber sequentially

## Communication

- **Transparent progress**: for multi-step work, narrate at natural milestones ("✓ checks complete — N findings, spawning per-file audits now") so the user knows the current phase without asking; silence for 5+ minutes is always worth a brief status note; before any significant Bash call (tests, logs, linters, builds) print a one-liner `[→ <what and why>]` so the user always knows what is executing
- **Flag early, not late**: surface risks, blockers, and concerns before starting — propose alternatives upfront rather than apologising after the fact
- **Objective and direct**: no flattery, no filler; state what works and what doesn't
- **Positive but critical**: lead with what is good, then call out issues clearly
- **File vs terminal for long output**: when a skill produces a full analysis or report, write it to a **new** file `tasks/output-<slug>-<YYYY-MM-DD>.md` — **do not print the full report to terminal**. Print a compact terminal summary instead covering: status/verdict, 2–3 sentence summary, critical/blocking points, recommendation, confidence score + gaps, `→ saved to <filename>`. If output is a short status the user reads and acts on inline (audit readiness check, calibrate scores), keep it terminal-only. Never overwrite an existing output file — always create a new one to avoid diff noise from unrelated content. Prose paragraphs: no hard line breaks at column width.
- **`!` Breaking findings**: when something is completely non-functional (skill can't run, cross-ref is broken, hook crashes), mark it `!` or `! BREAKING` and state the impact + fix in the same breath — never bury it as a quiet table row. The user should not have to discover it themselves.
- **Terminal color conventions** (for skill bash output and status lines):
  - RED — breaking/critical: `! BREAKING`, errors that prevent execution
  - YELLOW — warnings: `⚠ MISSING`, `⚠ DIFFERS`, medium findings
  - GREEN — pass status: `✓ OK`, `✓ IDENTICAL`
  - CYAN — source agent name or inline fix hint

## Output Standards

Every agent completing an analysis task **must** end its response with a `## Confidence` block:

```
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.7–0.9 | low <0.7]
**Gaps**:
- [specific limitation — e.g., no test execution; coverage findings indicative only]
- [specific limitation — e.g., no profiling data; perf estimates from static analysis]
**Refinements**: N passes.
- Pass 1: [what gap was concretely addressed]
- Pass 2: [what gap was concretely addressed]
```

(omit **Refinements** entirely if 0 passes; omit a **Gaps** bullet if no limitations apply)

- The **Gaps field** is the primary reliable signal: it makes implicit limitations explicit so the orchestrator and user can decide whether a second pass is needed.
- Score < 0.7 → orchestrator flags with ⚠ and may re-run the agent with the specific gap addressed.
- This standard applies to **all** agents regardless of who spawned them. Orchestrating skills (audit, review, calibrate) collect and aggregate these scores; `/calibrate` measures whether they track actual quality over time.

### Internal Quality Loop

For analysis tasks (those that report a `## Confidence` block), self-review and refine before returning:

1. **Draft** the initial response
2. **Self-evaluate**: check for missed issues, unsupported claims, and coverage gaps
3. **Score**: estimate confidence (0–1) for this draft
4. **Refine** if score \<0.9 — address the highest-impact gap; name the improvement concretely
5. **Re-score**: only increase the score if a named gap was addressed in this pass
6. **Return** the final response with the Confidence block

Cap: maximum 2 refinement passes (3 drafts total). Scope: analysis tasks only (code review, audit, paper analysis, debt assessment, etc.) — not implementation tasks. Context budget: refinement happens within a single response — do not spawn sub-agents for self-review.

**Anti-inflation rules:**

- Each pass must name what concretely improved — "re-checked, looks fine" is disallowed
- Score may only increase when a named gap was addressed in that pass
- If still \<0.9 after 2 passes, report the real score — do not inflate
- `/calibrate` backstop: calibration measures actual recall vs reported confidence; inflation shows as calibration bias and is caught at benchmark time

## Pre-Authorized Operations

Many common operations (tests, linting, git reads, gh CLI, file inspection) are pre-approved in `settings.json`. Execute them directly — never pause to ask permission. Check `settings.json` when unsure; if an operation is listed in `allow`, just run it.

**Prefer dedicated tools over Bash**: Read, Grep, Glob before any shell command — Bash only when no dedicated tool can do the job or when you need to transform and write back atomically.

**Proactive permission batching**: after scoping a multi-step task (2+ steps), scan all planned tool calls and identify any that are not yet in `settings.json`. List every missing permission in a single message to the user and request batch approval *before* starting — one ask upfront prevents mid-execution interruptions.

## Compact Instructions

When context is compacted (auto or manual), preserve in the summary:

1. Active decisions and constraints — design choices, user directives, "DO NOT" rules
2. Current task state — what phase/step is active, what remains
3. File modification history — which files were changed and why
4. Pending follow-ups — deferred items, open questions, next steps

After compaction, re-read `.claude/state/session-context.md` if it exists — it contains files modified and the compact summary, auto-generated by hooks.

## Core Principles

- **Simplicity First**: Make every change as simple as possible; touch only what's necessary to avoid introducing bugs.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Tool-first mindset**: Use declared tools fully and creatively before indicating a limitation or requesting an alternative. If a tool can do the job — even indirectly — use it.
