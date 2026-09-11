---
name: topic
description: Research State of the Art (SOTA) literature for an Artificial Intelligence / Machine Learning (AI/ML) topic, method, or architecture. Finds relevant papers, builds a comparison table, recommends the best implementation strategy for the current codebase, and optionally produces a phased implementation plan mapped to the codebase. Owns broad SOTA search end-to-end via foundry:web-explorer; delegates codebase mapping to foundry:solution-architect.
argument-hint: <topic> [--team] | plan [<output.md>] [--keep "<items>"]
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, WebSearch, WebFetch, TaskCreate, TaskUpdate, AskUserQuestion, TaskList
disable-model-invocation: true
effort: medium
---

<objective>

Research AI/ML topic literature. Return actionable findings: SOTA methods, best fit, concrete implementation plan. Skill = orchestrator — gathers codebase context, delegates literature search to researcher agent, packages results into structured report.

NOT for deep single-paper analysis or experiment design — use `research:scientist` directly for hypothesis generation, ablation design, experiment validation.

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - `<topic>` — topic, method name, or problem description (e.g. "object detection for small objects", "efficient transformers", "self-supervised pretraining for medical images")
  - `plan` — produce phased implementation plan from most recent research output (auto-detected from `.temp/`)
  - `plan <path-to-output.md>` — produce plan from specific existing research output file
  - `--team` — multi-agent mode; spawns 2–3 researcher teammates for topics with 3+ competing method families and no SOTA consensus; ~7× token cost vs single-agent mode

</inputs>

<compaction>

- Key boundary: end of Step 2 — SOTA literature gathered and written to AGENT_OUT; before Step 3 report synthesis.
- Preserve: AGENT_OUT path (TMPDIR key), BRANCH (TMPDIR key), DATE (TMPDIR key), REPORT_OUT target path, topic string from ARGUMENTS.
- Clear at Step 1 start (stale prior run) and at follow-up gate (terminal action).

</compaction>

<workflow>

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

## Agent Resolution

**Agent resolution**: load and follow the protocol below. Contains: foundry check + fallback table. Foundry not installed → substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:web-explorer`, `foundry:solution-architect`.

```bash
# loads: compaction-contract.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_RESEARCH_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" 2>/dev/null)  # timeout: 5000
[ -z "$_RESEARCH_SHARED" ] && { echo "! Plugin path resolution failed — ensure research plugin installed and CLAUDE_PLUGIN_ROOT set, or invoke from project root."; exit 1; }
echo "$_RESEARCH_SHARED" > "${TMPDIR:-/tmp}/research-shared-${CSID}"  # cold resolve — every later site reads this sentinel instead of re-running python
cat "$_RESEARCH_SHARED/agent-resolution.md"
```

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase — paper collection, researcher analysis, report generation. Mark in_progress/completed throughout. Always create **"Print report header"** as its own task (all paths — single-agent Step 3, `--team`, `plan`) — `in_progress` right after the report file is written (by the lead directly, or by a spawned consolidator's returned envelope); `completed` only once the `---` header has actually appeared in this response. This task exists because a sibling skill (oss:review) had an incident where a report was written correctly but the terminal print step got silently skipped while the hard-enforced `AskUserQuestion` fired anyway — tracking the print as its own task makes it as trackable as the tool calls around it. The shared `## Follow-up gate` below must not fire while this task is `pending`/`in_progress`.

## Step 1: Understand the codebase context

Read current project before searching, extract constraints:

- Framework (PyTorch, JAX, TensorFlow, scikit-learn)?
- Task (classification, detection, generation, regression)?
- Constraints (latency, memory, dataset size, compute budget)?

**Case-insensitive flag/mode normalization** — normalize before parsing so `--PLAN`, `--Team`, `Plan`, etc. accepted. Each Bash tool call runs fresh shell, so lowercased copy does NOT persist across blocks — re-derive inline from `$ARGUMENTS` (harness-substituted every block) wherever dispatch check needs it, e.g. `echo "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | …`. Preserve original `$ARGUMENTS` only where literal substitution into prompts required (e.g. topic string).

**Unsupported flag check** (runs BEFORE any mode dispatch to catch unknown flags in all modes): load and follow the protocol below. Supported flags for this skill: `--team`, `--keep`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read of the Agent Resolution cold resolve (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" topic "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears a stale contract, persists for Step 2
```

```bash
UNKNOWN_FLAGS=$(echo "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | grep -oE -- '--[a-z][a-z0-9-]+' | grep -v -E -- '--(team|keep)' || true)  # timeout: 5000
```

**Early dispatch for `--team` and `plan` modes** — check BEFORE Steps 2-3. Priority: `--team` wins over `plan` (`plan --team` → Team Mode, topic string = "plan"):

```bash
ARGUMENTS_LOWER=$(echo "$ARGUMENTS" | tr '[:upper:]' '[:lower:]')  # timeout: 5000
FIRST_WORD=$(echo "$ARGUMENTS_LOWER" | awk '{print $1}')  # timeout: 5000
```

- `$ARGUMENTS_LOWER` contains `--team` flag → skip Steps 2-3; jump directly to **Team Mode** section below.
- Else `$FIRST_WORD` equals exactly `plan` → skip Steps 2-3; jump directly to **Plan Mode** section below.

Steps 2-3 execute only when neither `--team` nor `plan` mode is detected.

## Step 2: Research & codebase check (run in parallel)

> **Parallelism scope**: 2a (Agent spawn) and 2b (Grep) issue in one response. Any WebSearch/WebFetch calls inside the researcher agent are issued sequentially — invoke all searches before synthesizing results. No mechanism exists to parallelize prose-driven searches across calls.

### 2a: SOTA literature search (issue with 2b simultaneously in one response)

**One owner for the search — decide first, never both**: `foundry:web-explorer` available (check below) → the AGENT owns the entire SOTA search and writes `$AGENT_OUT`; the orchestrator issues NO WebSearch/WebFetch of its own (a second inline pass re-fetches the same 5 papers and bills the full page text twice). Web-explorer unavailable → orchestrator conducts the search inline. Either way: find top 5 papers for `$ARGUMENTS`, produce comparison table (method, key idea, benchmark results, compute, code availability), recommend single best method given codebase constraints from Step 1.

**Note**: never dispatch to `research:scientist` for broad SOTA surveys — scientist scoped to deep single-paper analysis with named paper anchor. Use `research:scientist` directly only when: (a) specific paper identified and needs deep analysis, (b) hypothesis generation for identified method, or (c) experiment design for concrete approach. Broad SOTA = web-explorer territory.

Pre-compute output paths before searching:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
DATE=$(date +%Y-%m-%d)  # timeout: 3000
mkdir -p .temp .reports/research  # timeout: 3000
# anti-overwrite counter-suffix (quality-gates.md §Output Routing) — resolved by resolve-anti-overwrite-path.py
# Step 3's report path is resolved HERE, not at Step 3: the hook gate below must exist from the
# moment the run is committed to producing a report, not from the moment it remembers to.
AGENT_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .temp "output-research-agent-$BRANCH-$DATE")  # timeout: 5000
REPORT_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "topic-$BRANCH-$DATE")  # timeout: 5000
echo "$BRANCH" > "${TMPDIR:-/tmp}/topic-branch-${CSID}"
echo "$DATE" > "${TMPDIR:-/tmp}/topic-date-${CSID}"
echo "$AGENT_OUT" > "${TMPDIR:-/tmp}/topic-agent-out-${CSID}"
echo "$REPORT_OUT" > "${TMPDIR:-/tmp}/topic-report-out-${CSID}"
# Absolute path — hooks/enforce-topic-header.js reads this to gate the follow-up question
echo "$PWD/$REPORT_OUT" > "${TMPDIR:-/tmp}/research-topic-report-file-${CSID}"
```

Search targets (for whichever owner runs the search): arXiv, Papers With Code, Semantic Scholar, HuggingFace Hub. For each of top 5 papers: extract method, key idea, benchmark results, compute cost, code availability. The owner writes full findings (comparison table, paper analysis, recommendation, implementation plan, Confidence block) to `$AGENT_OUT`.

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

**Availability check** (`ls ~/.claude/plugins/cache/borda-ai-rig/foundry/*/agents/web-explorer.md 2>/dev/null`): present → spawn `Agent(subagent_type="foundry:web-explorer", prompt="...")` as the sole search owner per the rule above — its prompt carries the search targets, per-paper extraction fields, and the `$AGENT_OUT` write (resolved literal path). Absent → conduct the search inline using WebSearch and WebFetch directly.

### 2b: Check for existing implementations (main context)

Use Grep tool to search codebase for existing related code:

- Pattern: `$ARGUMENTS` (treat as literal string — if `$ARGUMENTS` contains regex metacharacters like `.`, `*`, `+`, `?`, `(`, `)`, `[`, `]`, `\`, escape them via `grep -F` semantics, OR escape each metachar with `\\` before passing to Grep tool)
- Glob: `**/*.py`
- Output mode: `files_with_matches`
- Limit to 1000 results (per external-data.md — never cap at default 10)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary: after Step 2 literature gathered (compaction-contract.md)
IFS= read -r _AGENT_OUT < "${TMPDIR:-/tmp}/topic-agent-out-${CSID}" 2>/dev/null || _AGENT_OUT=""
IFS= read -r _BRANCH < "${TMPDIR:-/tmp}/topic-branch-${CSID}" 2>/dev/null || _BRANCH=""
IFS= read -r _DATE < "${TMPDIR:-/tmp}/topic-date-${CSID}" 2>/dev/null || _DATE=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/topic-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _REPORT_OUT < "${TMPDIR:-/tmp}/topic-report-out-${CSID}" 2>/dev/null || _REPORT_OUT=".reports/research/topic-${_BRANCH}-${_DATE}.md"
_KEEP_APPEND=""; [ -n "$_KEEP" ] && _KEEP_APPEND="; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/write_skill_contract.py" "research:topic" "synthesis (after Step 2 literature gathered)" "n/a" "agent-out=${_AGENT_OUT}, report-out=${_REPORT_OUT}, branch=${_BRANCH}${_KEEP_APPEND}" "Step 3 synthesize agent findings into report → follow-up gate"  # timeout: 5000
```

## Step 3: Report

```markdown
---
Title:       Research — [topic]
Date:        [YYYY-MM-DD]
Scope:       [topic / research question]
Focus:       SOTA literature research
Agents:      [agents actually dispatched this run — e.g. foundry:web-explorer when it ran Step 2a; solution-architect only on plan-mode runs; never the full menu]
Outcome:     EXPLORATORY | PROMISING | CONSENSUS
Best method: [recommended approach / architecture]
Papers:      [N papers analyzed]
Confidence:  [aggregate score] — [key gaps]
Next steps:  /research:topic plan → /develop:feature (requires `develop` plugin)
Path:        → .reports/research/topic-<branch>-<date>.md
---

## Research: $ARGUMENTS

### SOTA Overview
[2-3 sentence summary of the current state of the field]

### Method Comparison
| Method | Key Idea | SOTA Result | Compute | Code Available |
|--------|----------|-------------|---------|----------------|
| ...    | ...      | ...         | ...     | Yes/No + link  |

### Recommendation
**Use [method]** because [specific reason matching the current codebase constraints].

### Implementation Plan
1. [step with file/component to change]
2. [step]
3. [step]

### Key Hyperparameters
- [param]: [typical range] — [what it controls]

### Gotchas
- [common failure mode and how to avoid it]

### Integration with Current Codebase
- Files to modify: [list with file:line references]
- New dependencies needed: [package versions]
- Estimated effort: [hours/days]
- Risk assessment: [what could go wrong during integration]

### References
- [Paper title] ([year]) — [link]

### Agent Confidence
<!-- Rows come from the actual launch batch: one row per agent this run really spawned, named as dispatched. -->
<!-- No agent spawned (orchestrator ran the search inline): single row, agent `orchestrator (inline)`. -->
<!-- The rows below are shape examples, never emitted verbatim — a fixed researcher-1/2/3 lineup reports agents that never ran. -->
| Agent | Score | Gaps |
|---|---|---|
| [agent as dispatched, e.g. foundry:web-explorer] | [score] | [gaps] |
```

```bash
mkdir -p .reports/research  # timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload from Step 2a bash block (Check 41: fresh shell per call)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/topic-branch-${CSID}" 2>/dev/null || BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')
IFS= read -r DATE < "${TMPDIR:-/tmp}/topic-date-${CSID}" 2>/dev/null || DATE=$(date +%Y-%m-%d)
# report path (anti-overwrite suffix, quality-gates.md) resolved at Step 2a — reuse
# verbatim; re-resolving here would drift from the path the hook gate is watching
IFS= read -r REPORT_OUT < "${TMPDIR:-/tmp}/topic-report-out-${CSID}" 2>/dev/null || REPORT_OUT=""
if [ -z "$REPORT_OUT" ]; then
    REPORT_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "topic-$BRANCH-$DATE")  # timeout: 5000
    echo "$REPORT_OUT" > "${TMPDIR:-/tmp}/topic-report-out-${CSID}"
    echo "$PWD/$REPORT_OUT" > "${TMPDIR:-/tmp}/research-topic-report-file-${CSID}"
fi
```

Write full report to `$REPORT_OUT` using Write tool (resolved by counter-suffix loop above) — **do not print full report to terminal**.

TaskUpdate "Print report header" → `in_progress`.

Print compact terminal summary — MANDATORY, do this in the same turn as the write above, then TaskUpdate "Print report header" → `completed` only once it has actually appeared in this response:

```text
---
Research — [topic]
SOTA:        [1–2 sentence summary of current landscape]
Best method: [recommended approach / architecture]
Key papers:  [top 2–3 papers with year]
Gaps:        [what the research couldn't cover or needs runtime validation]
Confidence:  [aggregate score] — [key gaps]
→ saved to .reports/research/topic-$BRANCH-$DATE.md
---
```

**Hook-enforced**: `hooks/enforce-topic-header.js` (PreToolUse on `AskUserQuestion`) denies the `## Follow-up gate` call while the report file named by the `research-topic-report-file` sentinel (written at Step 2a) is missing or empty. A denial reading `research:topic report gate` means the report was never written — write it to that exact path, print its `---` header, then re-issue the question. The hook sees only whether the report exists, not whether the print happened; the "Print report header" task remains the check for the print itself.

End response with `## Confidence` block per CLAUDE.md output standards.

## Team Mode — only when `--team` flag present

> loads: modes/team.md # also loads: modes/plan.md **Mode-file existence check** — verify before reading:

```bash
_TEAM_MODE="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills/topic/modes/team.md"
[ -f "$_TEAM_MODE" ] || { echo "! MISSING — modes/team.md not found at $_TEAM_MODE. Plugin may not be fully installed. Falling back to single-agent mode."; exit 1; }
[ -f "$HOME/.claude/TEAM_PROTOCOL.md" ] || { echo "! MISSING — ~/.claude/TEAM_PROTOCOL.md not found. Run /foundry:setup (requires foundry plugin) to install. Falling back to single-agent mode."; exit 1; }
cat "$_TEAM_MODE"  # timeout: 5000
```

Follow `modes/team.md` (loaded above) and execute its workflow.

**Mandatory termination gate**: after `modes/team.md` returns (consolidation complete, report written, header printed per its own mandatory print step, "Print report header" task `completed`), continue to `## Follow-up gate` section below — do NOT exit early. `AskUserQuestion` call in `## Follow-up gate` is only authorized terminal action for team mode; reaching end of team workflow without invoking it is protocol violation.

## Plan Mode — only when first token of `$ARGUMENTS` is exactly `plan` (not a prefix match — "planning algorithms" must NOT trigger this mode)

> loads: modes/plan.md **Mode-file existence check** — verify before reading:

```bash
_PLAN_MODE="${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/skills/topic/modes/plan.md"
[ -f "$_PLAN_MODE" ] || { echo "! MISSING — modes/plan.md not found at $_PLAN_MODE. Plugin may not be fully installed."; exit 1; }
cat "$_PLAN_MODE"  # timeout: 5000
```

Follow `modes/plan.md` (loaded above) and execute its workflow.

**Mandatory termination gate**: after `modes/plan.md` returns (phased plan emitted, report written, compact terminal summary printed per its own `Print compact terminal summary` step, "Print report header" task `completed`), continue to `## Follow-up gate` section below — do NOT exit early. `AskUserQuestion` call in `## Follow-up gate` is only authorized terminal action for plan mode; reaching end of plan workflow without invoking it is protocol violation.

## Follow-up gate

**Hard gate**: check "Print report header" task status before anything else here. Not `completed` → the report header has not actually been printed yet — go back and do it now (Step 3 / team.md / plan.md, whichever path ran), then mark the task `completed`, before calling `AskUserQuestion` below. `hooks/enforce-topic-header.js` backs this gate structurally — the `AskUserQuestion` below is denied outright while the run's report file is absent or empty on disk.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — topic research complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

Call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "What next?"
- (a) label: `/research:plan` — description: design a research program from these findings
- (b) label: `/develop:feature` — description: implement based on findings (requires `develop` plugin)
- (c) label: `skip` — description: no action

</workflow>

<notes>

- Skill orchestrates — owns broad SOTA literature search end-to-end via `foundry:web-explorer`, delegates codebase mapping to `foundry:solution-architect` (plan mode). For direct hypothesis/experiment work on named paper, use `research:scientist` directly.
- **Team Mode dependency**: `--team` requires `~/.claude/TEAM_PROTOCOL.md` to exist — each teammate spawn prompt includes `Read $HOME/.claude/TEAM_PROTOCOL.md and use AgentSpeak v2`; verify file present before launching team mode.
- **Link integrity**: all URLs cited in research report must be fetched and verified before inclusion. Use WebFetch to confirm each URL exists and says what claimed.
- Follow-up chains:
  - Research recommends method → `/research:plan` for sequenced plan (auto-detects latest output), then `/develop:feature` (requires `develop` plugin) for TDD-first implementation
  - Research integrates into existing code → `/develop:refactor` (requires `develop` plugin) first to prepare module, then `/develop:feature` (requires `develop` plugin)
  - Research reveals security concerns with dependency → run `pip-audit` or `uv run pip-audit` for Common Vulnerabilities and Exposures (CVE) scan
  - Plan approved → create `.plans/active/todo_<method>.md` with phases as task groups; start with `/develop:feature <first task from Phase 1>` (requires `develop` plugin)

</notes>
