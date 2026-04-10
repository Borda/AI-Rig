---
name: research
description: Research State of the Art (SOTA) literature for an Artificial Intelligence / Machine Learning (AI/ML) topic, method, or architecture. Finds relevant papers, builds a comparison table, recommends the best implementation strategy for the current codebase, and optionally produces a phased implementation plan mapped to the codebase. Delegates deep analysis to the ai-researcher agent and codebase mapping to solution-architect.
argument-hint: <topic> | plan [path-to-output.md] | --team
allowed-tools: Read, Write, Grep, Glob, Agent, WebSearch, WebFetch, TaskCreate, TaskUpdate
context: fork
effort: high
model: opus
---

<objective>

Research the literature on an AI/ML topic and return actionable findings: what SOTA methods exist, which fits best for the current use case, and a concrete implementation plan. This skill is an orchestrator — it gathers codebase context, delegates literature search and analysis to the ai-researcher agent, and packages results into a structured report.

This skill is NOT for deep single-paper analysis or experiment design — use the `ai-researcher` agent directly for hypothesis generation, ablation design, and experiment validation.

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - `<topic>` — topic, method name, or problem description (e.g. "object detection for small objects", "efficient transformers", "self-supervised pretraining for medical images")
  - `plan` — produce a phased implementation plan from the most recent research output (auto-detected from `.temp/`)
  - `plan <path-to-output.md>` — produce a plan from a specific existing research output file
  - `--team` — multi-agent mode; spawns 2–3 ai-researcher teammates for topics with 3+ competing method families and no SOTA consensus; ~7× token cost vs single-agent mode

</inputs>

<constants>
HARD_CUTOFF: 900   # 15 min — if ai-researcher does not return, surface partial results from .temp/
# Agent calls are synchronous — timeout is handled by Claude Code's native call timeout; no manual extension possible.
# Deviation from §8: Agent tool is synchronous; no file-activity poll available; timeout enforced by HARD_CUTOFF only
</constants>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase — paper collection, ai-researcher analysis, and report generation. Mark in_progress/completed throughout.

## Step 1: Understand the codebase context

Before searching, read the current project to extract constraints:

- Framework in use (PyTorch, JAX, TensorFlow, scikit-learn)?
- Task being solved (classification, detection, generation, regression)?
- Constraints (latency, memory, dataset size, compute budget)?

## Step 2: Research & codebase check (run in parallel)

### 2a: Spawn ai-researcher agent (issue with 2b simultaneously in one response)

Task the ai-researcher with a single objective: find the top 5 papers for `$ARGUMENTS`, produce a comparison table (method, key idea, benchmark results, compute, code availability), and recommend the single best method given the codebase constraints in Step 1 — with a brief implementation plan. The agent's own workflow handles the research and experiment design details.

Use this prompt scaffold (adapt the constraints from Step 1):

Note: pre-compute output paths before spawning — the orchestrator must extract the branch and evaluate date expressions, then substitute concrete paths into all spawn prompts:

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main') # timeout: 3000
DATE=$(date +%Y-%m-%d)
```

**Note**: Substitute pre-computed values — do not pass raw $(date) expressions into spawn prompts.

```
Research the literature on: <$ARGUMENTS>
Codebase constraints: <framework, Python version, compute budget, existing dependencies from Step 1>
Deliver: comparison table (method, key idea, benchmarks, compute, code available), recommendation for best method, a 3-step implementation plan for this codebase, key hyperparameters (name, typical range, what it controls) for the recommended method, and common gotchas (failure modes and how to avoid them).
Write your full findings (comparison table, paper analysis, recommendation, implementation plan, Confidence block) to `.temp/output-research-agent-$BRANCH-$DATE.md` using the Write tool.
Then return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","papers":N,"recommendation":"<method name>","file":".temp/output-research-agent-$BRANCH-$DATE.md","confidence":0.N}
```

**Health monitoring** — the Agent tool is synchronous; Claude awaits the ai-researcher response natively (no Bash checkpoint available in this skill). If ai-researcher does not return within `$HARD_CUTOFF` seconds (~15 min), use the Read tool to surface any partial results already written to `.temp/` and continue with what was found; mark timed-out agents with ⏱ in the report. # Agent calls are synchronous — timeout is handled by Claude Code's native call timeout; no manual extension possible.

**If the Agent tool is unavailable** (running as a subagent where nested agent spawning is blocked), skip the Agent call and conduct the research inline: use WebSearch and WebFetch to find the top 5 papers, then synthesize the comparison table yourself. Notify the user: "Note: ai-researcher agent could not be spawned in this context — conducting research inline."

### 2b: Check for existing implementations (main context)

Use the Grep tool to search the codebase for any existing related code:

- Pattern: `$ARGUMENTS` (literal)
- Glob: `**/*.py`
- Output mode: `files_with_matches`
- Limit to 10 results

## Step 3: Report

```
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
<!-- One row per spawned agent; team mode: 2–3 rows -->
| Agent | Score | Gaps |
|---|---|---|
| ai-researcher | [score] | [gaps] |
```

Write the full report to `.temp/output-research-$BRANCH-$DATE.md` using the Write tool — **do not print the full report to terminal**.

Then print a compact terminal summary:

```
---
Research — [topic]
SOTA:        [1–2 sentence summary of current landscape]
Best method: [recommended approach / architecture]
Key papers:  [top 2–3 papers with year]
Gaps:        [what the research couldn't cover or needs runtime validation]
Confidence:  [aggregate score] — [key gaps]
→ saved to .temp/output-research-[date].md
---
```

End your response with a `## Confidence` block per CLAUDE.md output standards.

## Team Mode

Use when the topic warrants exploring multiple competing method families with adversarial cross-evaluation.

When to trigger: 3+ distinct method families exist for the topic AND the field has no clear leading method (benchmark spread \<5% between top methods, or no SOTA consensus in the past 12 months). Skip for topics with a clear dominant approach — the default single ai-researcher is sufficient.

**Workflow with team:**

1. Lead completes Step 1 (codebase context) as normal
2. Spawn 2–3 **ai-researcher** teammates, each assigned a distinct method cluster
3. Broadcast constraints to all: `broadcast {topic: <topic>, constraints: <framework/compute/dataset from Step 1>}`
4. Each teammate researches independently, reports with `deltaT# HOOK:verify` (AgentSpeak v2 completion signal — see TEAM_PROTOCOL.md) and a compressed comparison table
5. Lead routes key findings from one researcher to others for cross-challenge: `@AR2: AR1 found [finding] — does it hold under [condition]?`
6. Lead synthesizes into the Step 3 report, noting where researchers agreed or diverged

**Note on CLAUDE.md §8 (background agent monitoring)**: Team mode spawns in-process teammates via TeamCreate — not background agents writing to a run directory. In-process teammates send TeammateIdle notifications on completion, which serve as synchronous completion signals. The file-activity polling protocol (§8) does not apply here; TeammateIdle is the equivalent liveness signal.

**Spawn prompt template:**

```
# Substitute pre-computed values — do not pass raw $(date) expressions into spawn prompts
You are an ai-researcher teammate researching: [topic].
Read .claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your cluster: [method family N] (e.g., "attention-free architectures" vs "linear attention variants").
Research the top 3 methods in your cluster: comparison table + recommendation given constraints.
Write your full findings (comparison table, analysis, Confidence block) to `.temp/output-research-<teammate-name>-$BRANCH-<date>.md` using the Write tool.
Report completion with deltaT# HOOK:verify and include: papers=N recommendation="<method>" confidence=0.N file=.temp/output-research-<teammate-name>-<date>.md
Compact Instructions: preserve paper titles, benchmarks, code links. Discard protocol handshakes.
Task tracking: call TaskUpdate(in_progress) when you start your assigned task; call TaskUpdate(completed) when done, before sending your delta message.
```

Lead synthesizes by reading teammate file paths from their delta messages. For 3 teammates, spawn a consolidator ai-researcher agent: "Read the research files at [paths from deltas]. Synthesize into the Step 3 unified report structure. Write to `.temp/output-research-$BRANCH-<date>.md`. Return ONLY: `papers=N best_method=<name> confidence=0.N file=<path>`"

## Plan Mode

Produce a sequenced, dependency-ordered implementation plan from SOTA research findings, mapped against the current codebase. Use after a research run has identified a recommended method and you need a phased plan before starting `/develop feature`.

**Input detection** (parse the argument after `plan`):

- No argument → **auto-detect**: use Glob (pattern `**/output-research-*.md`, path `.temp/`) to find recent research outputs; exclude any path containing `-plan-` or `-codebase-`; sort by modification time descending; pick the most recent. Print `→ Using: <path>` to terminal before proceeding. If no file found, stop with: "No recent research output found — run `/research <topic>` first."
- Ends in `.md` → treat as path to an existing research output file; skip to Step R1-B

### Step R1: Gather research findings

**R1-A — From fresh research**: After Steps 1–3 complete, read the generated `.temp/output-research-<date>.md`. Extract: Recommendation section, Implementation Plan, Key Hyperparameters, Gotchas, and Integration with Current Codebase.

**R1-B — From existing output**: Read the file at the given path directly. Extract the same sections.

**Validation**: the file must contain a clear **Recommendation** section naming a specific method. If missing or ambiguous, stop and report: "Research output does not contain a clear method recommendation — run `/research <topic>` first, then pass the output path."

Before spawning in Steps R2–R3, pre-compute the output path components: `YYYY=$(date +%Y); MM=$(date +%m); DATE=$(date +%Y-%m-%d)` `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` <!-- same pattern as Step 2a date/branch block -->

### Step R2: Codebase analysis

<!-- Pre-compute DATE and BRANCH before spawning: DATE=$(date +%Y-%m-%d); BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main') -->

Spawn a **solution-architect** agent with this prompt:

```
Read the research findings file at <path from R1>.
Analyze the current codebase to map the recommended method against existing code:
1. Identify all files and modules relevant to the recommended method's domain
2. Map existing abstractions: interfaces, base classes, patterns the codebase already uses
3. Identify integration points: where does the new method plug in?
4. Flag conflicts: existing patterns that would need to change
5. Estimate complexity per integration point (low/medium/high)

Write your full analysis to `.temp/output-research-codebase-$BRANCH-$DATE.md` using the Write tool.
Return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","integration_points":N,"conflicts":N,"file":".temp/output-research-codebase-<date>.md","confidence":0.N,"summary":"N integration points, N conflicts"}
```

### Step R3: Synthesize plan

Read both files (research findings from R1 + codebase analysis from R2). Produce a phased plan and write it to `.temp/output-research-plan-$BRANCH-$DATE.md`:

```
## Implementation Roadmap: [method name]
Topic: [original $ARGUMENTS]

### Prerequisites
- [dependency, environment requirement, or data prerequisite]

### Phase 1: Foundation — [description]
**Goal**: [what this phase achieves and why it must come first]
| Task | Files | Depends On | Complexity | Verification |
|------|-------|------------|------------|--------------|
| ...  | ...   | —          | low/med/hi | [how to verify done] |

### Phase 2: Core Implementation — [description]
**Goal**: [what this phase achieves]
| Task | Files | Depends On | Complexity | Verification |
|------|-------|------------|------------|--------------|
| ...  | ...   | Phase 1    | ...        | ...          |

### Phase 3: Integration & Validation — [description]
**Goal**: wire into existing pipeline, validate end-to-end
[same table format]

### Risks
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| ...  | low/med/hi | ...    | ...        |

### Estimated Effort
| Phase | Tasks | Complexity Mix | Estimate |
|-------|-------|----------------|----------|
| 1     | N     | N low, M med   | X days   |

### Next Steps
- Phase 1 ready → `/develop feature <first task from Phase 1>`
- Full plan approved → create `.plans/active/todo_<method>.md` with phases as task groups
```

Print a compact terminal summary:

```
---
Research Plan — [method name]
Phases:      [N] phases, [M] tasks total
Complexity:  [N low / M medium / K high]
Top risk:    [one-line from risks table]
Confidence:  [score] — [key gaps]
→ saved to .temp/output-research-plan-[date].md
---
```

</workflow>

<notes>

- This skill orchestrates — it gathers context and delegates research to `ai-researcher` and codebase mapping to `solution-architect` (plan mode). For direct hypothesis/experiment work, use the `ai-researcher` agent directly.
- **Team Mode dependency**: `--team` mode requires `.claude/TEAM_PROTOCOL.md` to exist — each teammate spawn prompt includes `Read .claude/TEAM_PROTOCOL.md and use AgentSpeak v2`; verify the file is present before launching team mode.
- **Link integrity**: All URLs cited in the research report must be fetched and verified before inclusion. Use WebFetch to confirm each URL exists and says what you claim.
- Follow-up chains:
  - Research recommends a method for implementation → `/research plan` to produce a sequenced plan (auto-detects latest output), then `/develop feature` for TDD-first implementation
  - Research integrates into existing code → `/develop refactor` first to prepare the module, then `/develop feature`
  - Research reveals security concerns with a dependency → run `pip-audit` or `uv run pip-audit` for a Common Vulnerabilities and Exposures (CVE) scan
  - Plan approved → create `.plans/active/todo_<method>.md` with phases as task groups; start with `/develop feature <first task from Phase 1>`

</notes>
