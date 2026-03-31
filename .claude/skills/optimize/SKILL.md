---
name: optimize
description: Performance orchestrator with four modes. `plan` = interactive wizard → writes `program.md` config. `campaign` = sustained metric-improvement loop with atomic commits, auto-rollback, and experiment logging; accepts a `program.md` file. `resume` = continue a crashed or stopped campaign. `perf` = single-pass profiling deep-dive (baseline → perf-optimizer → verify → report). Supports --team, --colab, and --codex in plan/campaign/resume.
argument-hint: plan <goal> [out.md] | campaign <goal|file.md> | resume [file.md] | perf <target> [--team] [--colab] [--codex]
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
---

<objective>

Four complementary modes under one skill. `plan` mode runs an interactive wizard: scans the codebase, proposes a metric/guard/agent config, and writes a `program.md` human-readable campaign spec. `campaign` mode runs a sustained improvement campaign: iterate with specialist agents, commit atomically, auto-rollback on regression, and log every experiment to JSONL — until the metric goal is reached or the iteration limit is hit. `resume` mode continues a campaign that was stopped or crashed, re-reading the program file to pick up any edits. `perf` mode orchestrates a single-pass performance investigation: establish a baseline, spawn perf-optimizer to find and fix the top bottleneck, verify the improvement, and report.

</objective>

<inputs>

- `plan <goal>` — interactive wizard: scan codebase, propose config, write `program.md` at project root
- `plan <goal> output.md` — same wizard, write to specified path
- `campaign <goal>` — run the iteration loop directly with a text goal
- `campaign program.md` — parse `program.md`, run campaign
- `resume` — resume most recent running campaign (reads `program_file` from `state.json`)
- `resume program.md` — resume the campaign started from that file
- `perf <target>` — file, module, or directory to optimize (single profiling session)

**Auto-detect rule** (for `campaign`): if the argument ends in `.md` → treat as program file path. Otherwise → treat as text goal.

- `--team` flag (plan/campaign/resume only) — parallel strategy exploration: 2–3 teammates each own a different optimization axis
- `--colab` flag (plan/campaign/resume only) — route metric verification through a Colab MCP GPU runtime
- `--codex` flag (plan/campaign/resume only) — offload ideation to Codex: each iteration, Codex proposes and implements an optimization as a fallback when the Claude specialist agent's change is reverted or a no-op; Claude orchestrates the loop, compares metric results, and keeps the winner; gracefully degrades to Claude-only if `codex` is not installed

</inputs>

<constants>

Campaign mode only:

```
MAX_ITERATIONS:             20 (ceiling: 50 — never exceed without explicit user override)
STUCK_THRESHOLD:            5 consecutive discards → escalation
GUARD_REWORK_MAX:           2 attempts before revert
VERIFY_TIMEOUT_SEC:         120 (local), 300 (--colab)
SUMMARY_INTERVAL:           10 iterations
DIMINISHING_RETURNS_WINDOW: 5 iterations < 0.5% each → warn user and suggest stopping
STATE_DIR:                  _optimizations/state/
```

**Agent strategy mapping** (`agent_strategy` in config → ideation agent to spawn):

| `agent_strategy` | Specialist agent     | When to use                                  |
| ---------------- | -------------------- | -------------------------------------------- |
| `auto`           | heuristic            | Default — infer from metric_cmd keywords     |
| `perf`           | `perf-optimizer`     | latency, throughput, memory, GPU utilization |
| `code`           | `sw-engineer`        | coverage, complexity, lines, coupling        |
| `ml`             | `ai-researcher`      | accuracy, loss, F1, AUC, BLEU                |
| `arch`           | `solution-architect` | coupling, cohesion, modularity metrics       |

> note: solution-architect uses opusplan tier — higher cost per ideation call

**Auto-inference keyword heuristics** (applied when `agent_strategy: auto` or omitted; checked against `## Goal` text AND metric command):

- contains `pytest`, `coverage`, `complexity` → `code` → `sw-engineer`
- contains `time`, `latency`, `bench`, `throughput`, `memory` → `perf` → `perf-optimizer`
- contains `accuracy`, `loss`, `f1`, `auc`, `train`, `val`, `eval` → `ml` → `ai-researcher`
- no keyword match → `perf` (default fallback)

**Stuck escalation sequence** (at STUCK_THRESHOLD consecutive discards):

1. Switch to a different agent type (rotate through: `code` → `ml` → `perf` → `code`; if current is `ml`, next is `perf`; if current is `perf`, next is `code`)
2. Spawn 2 agents in parallel with competing strategies; keep whichever improves metric
3. Stop, report progress, surface to user — do not continue looping blindly

</constants>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, create TaskCreate entries for all known steps immediately at skill start. Mark in_progress when starting each step, completed when done. Keep statuses current throughout.

## Step 1: Parse mode

Extract the first token from arguments. Valid values: `plan`, `campaign`, `resume`, `perf`.

If the first token is not a valid mode, stop and present:

```
Usage: /optimize plan <goal> [out.md]
       /optimize campaign <goal|file.md> [--team] [--colab]
       /optimize resume [file.md] [--team] [--colab]
       /optimize perf <file|module|dir>
```

## Step 2: Dispatch to mode file

**If mode is `perf`**: Read `.claude/skills/optimize/modes/perf.md` and execute its steps (P1–P6) in order, passing the remaining arguments as `$ARGUMENTS`.

**If mode is `campaign`**: Read `.claude/skills/optimize/modes/campaign.md` and execute its Default Mode steps (C1–C7) in order, passing the remaining arguments along with any flags (`--team`, `--colab`).

**If mode is `plan`**: Read `.claude/skills/optimize/modes/campaign.md` and execute its Plan Mode steps (C-P1–C-P3), passing the remaining arguments as `<goal> [out.md]`.

**If mode is `resume`**: Read `.claude/skills/optimize/modes/campaign.md` and execute its Resume Mode steps, passing the optional `file.md` argument along with any flags (`--team`, `--colab`).

</workflow>

<notes>

## Cross-mode follow-up chains

- Perf bottleneck is architectural (not just a hot loop) → `/develop refactor` for structural changes with test safety net
- Perf changes non-trivial code paths → `/review` for quality validation
- Perf optimized code needs documentation updates → Step P6 auto-delegates to Codex
- Campaign improves metric → `/review` for quality validation of kept commits
- Campaign metric plateauing → `/research` for SOTA comparison — maybe a fundamentally different approach is needed
- Campaign kept commits accumulate technical debt → `/develop refactor` for structural cleanup with test safety net
- Campaign exposes a performance ceiling → `/optimize perf` for a deeper profiling pass on the bottleneck
- `/optimize perf` reveals a systemic throughput issue (not a single hot path) → `/optimize campaign <goal>` for a sustained multi-iteration improvement run

</notes>
