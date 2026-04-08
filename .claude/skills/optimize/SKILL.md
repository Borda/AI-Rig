---
name: optimize
description: Performance orchestrator with five modes. `plan` = interactive wizard → writes `program.md` config; also accepts a file path to auto-detect profiling targets via cProfile. `judge` = research-supervisor review of `program.md` — validates experimental methodology (hypothesis clarity, measurement validity, control adequacy, scope, strategy fit) and emits APPROVED / NEEDS-REVISION / BLOCKED verdict before the expensive run loop. `run` = sustained metric-improvement loop with atomic commits, auto-rollback, and experiment logging; accepts a `program.md` file and an optional clarification prompt. `resume` = continue a crashed or stopped run. `sweep` = non-interactive end-to-end pipeline: plan (auto-config) → judge+refine loop (up to 3 iterations, auto-applies Required Changes) → run. Supports --team, --colab[=H100|L4|T4|A100], --codex, and --compute=local|colab|docker in run/resume/sweep (--colab and --docker are mutually exclusive).
argument-hint: plan <goal|file> [out.md] | judge [file.md] [--skip-validation] | run <file.md> [clarification] [--compute=local|colab|docker] [--team] [--colab[=H100|L4|T4|A100]] [--codex] [--researcher] [--architect] [--journal (req: --researcher|--architect)] [--hypothesis <path> (req: --researcher|--architect)] | resume [file.md] [--team] [--colab[=H100|L4|T4|A100]] [--codex] [--compute=local|colab|docker] | sweep "goal" [--team] [--colab[=H100|L4|T4|A100]] [--codex] [--compute=local|colab|docker] [--researcher] [--architect] [--out <path>] [--skip-validation]
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
effort: high
---

<objective>

Five complementary modes under one skill. `plan` mode runs an interactive wizard: scans the codebase, proposes a metric/guard/agent config, and writes a `program.md` human-readable run spec. If given a file path instead of a goal, it first runs cProfile to surface bottlenecks, presents the top findings, and asks the user what to optimize — then proceeds as the normal wizard. `judge` mode is a read-only pre-flight gate — like a research supervisor reviewing a PhD student's experimental protocol before approving expensive lab time: audits `program.md` for structural completeness (J2), reviews the experimental methodology for design soundness via solution-architect (J3), optionally validates metric and guard commands locally to confirm they execute (J4), and requests a Codex adversarial review of any identified gaps (J5). Emits a deterministic verdict: APPROVED / NEEDS-REVISION / BLOCKED. `run` mode runs a sustained improvement campaign: iterate with specialist agents, commit atomically, auto-rollback on regression, and log every experiment to JSONL — until the metric goal is reached or the iteration limit is hit. `resume` mode continues a run that was stopped or crashed, re-reading the program file to pick up any edits. `sweep` mode is a non-interactive end-to-end pipeline: auto-configure `program.md` (accept defaults, no wizard), then run a judge+refine loop — up to 3 iterations of judge → apply Required Changes → re-judge — before proceeding to run. BLOCKED exits immediately; still unresolved after 3 iterations escalates to the user. Single command from goal to result.

</objective>

<inputs>

- `plan <goal>` — interactive wizard: scan codebase, propose config, write `program.md` at project root
- `plan <file.py>` — profiling flow: run cProfile on the file, show top bottlenecks, ask what to optimize, then run wizard
- `plan <goal> output.md` — same wizard, write to specified path
- `judge` — research-supervisor review of `program.md`; emit APPROVED / NEEDS-REVISION / BLOCKED verdict
- `judge path/to/plan.md` — audit the specified program file
- `judge --skip-validation` — skip local metric/guard validation (cross-machine workflows)
- `run program.md [clarification]` — program file with an optional per-run direction for the ideation agent (bare word or quoted string)
- `sweep "goal"` — non-interactive pipeline: auto-configure plan, judge (halt if not APPROVED), then run
- `sweep "goal" --skip-validation` — skip local metric/guard validation during the judge step
- `sweep "goal" --compute=local|colab|docker` — override compute environment (also accepts `--colab[=HW]`)
- `resume` — resume most recent run (reads `program_file` from `state.json`)
- `resume program.md` — resume the run started from that file
- `resume` also accepts `--team`, `--colab[=HW]`, `--codex`, and `--compute=local|colab|docker` — see respective flag entries above

**Auto-detect rule** (for `run`): argument must end in `.md` — treated as the program file path.

- `--team` flag (plan/run/resume/sweep) — parallel strategy exploration: 2–3 teammates each own a different optimization axis
- `--colab[=HW]` flag (plan/run/resume/sweep) — alias for `--compute=colab`; optionally specify GPU hardware: `--colab=H100`, `--colab=L4`, `--colab=T4`, `--colab=A100`. If omitted, Colab picks the default GPU. Unknown hardware values warn but do not block. Advisory at precondition check; GPU identity assertion in Phase 5.
- `--compute=local|colab|docker` flag (run/resume/sweep only) — override the `compute` field in program.md: `local` = run on host (default), `colab` = Colab MCP GPU runtime, `docker` = Docker sandbox (read-only project mount, ephemeral `/tmp`); `--colab` and `--compute=colab` are equivalent; `--docker` and `--compute=docker` are equivalent; if Docker daemon is not running when `docker` is selected, the run stops with an error; `--colab=HW` sets both `compute=colab` and `colab_hw=HW`; `--colab` and `--docker` are mutually exclusive — passing both stops with an error
- `--codex` flag (plan/run/resume only) — offload ideation to Codex: each iteration, Codex proposes and implements an optimization as a fallback when the Claude specialist agent's change is reverted or a no-op; Claude orchestrates the loop, compares metric results, and keeps the winner; run stops with an error if `codex` plugin is not installed when `--codex` is passed
- `--researcher` flag (run/sweep only) — enables autonomous research pipeline: `ai-researcher` generates hypotheses from SOTA literature, `solution-architect` filters for architectural feasibility, campaign loop consumes `hypotheses.jsonl`; see `.claude/rules/optimize-hypothesis-protocol.md` for the full JSONL schema; combine with `--architect` for dual-agent hypothesis generation
- `--journal` flag (run only) — record ALL iteration outcomes (kept and reverted) to `journal.md`; last 5 entries fed back to the ideation agent to prevent repeating failed approaches; not available in sweep mode; requires `--researcher` or `--architect`
- `--hypothesis <path>` flag (run only) — skip the oracle hypothesis-generation phase and use a pre-generated `hypotheses.jsonl` from the specified path; requires `--researcher` or `--architect`; not available in sweep mode
- `--architect` flag (run/sweep only) — enables architectural hypothesis pipeline: `solution-architect` generates hypotheses by analyzing the codebase architecture and coupling, `ai-researcher` filters for SOTA grounding. Combine with `--researcher` to run both oracle (SOTA literature) and architectural passes, filling the queue from both agents. Without `--researcher`, only architectural analysis hypotheses are generated.

</inputs>

<constants>

Campaign mode only:

```
MAX_ITERATIONS:             20 (ceiling: 50 — never exceed without explicit user override)
STUCK_THRESHOLD:            5 consecutive discards → escalation
GUARD_REWORK_MAX:           2 attempts before revert
VERIFY_TIMEOUT_SEC:         120 (local), 300 (--colab)
COLAB_KNOWN_HW:             H100, L4, T4, A100
SUMMARY_INTERVAL:           10 iterations
DIMINISHING_RETURNS_WINDOW: 5 iterations < 0.5% each → warn user and suggest stopping
STATE_DIR:                  .experiments/<run-id>/      (timestamped dir per run — see .claude/rules/artifact-lifecycle.md)
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

Extract the first token from arguments. Valid values: `plan`, `judge`, `run`, `resume`, `sweep`.

If the first token is not a valid mode, stop and present:

```
Usage: /optimize plan <goal|file> [out.md]
       /optimize judge [file.md] [--skip-validation]
       /optimize run <file.md> [clarification] [--team] [--colab[=HW]] [--codex] [--compute=local|colab|docker] [--researcher] [--architect] [--journal] [--hypothesis <path>]
       /optimize resume [file.md] [--team] [--colab[=HW]] [--codex] [--compute=local|colab|docker]
       /optimize sweep "goal" [--team] [--colab[=HW]] [--codex] [--compute=local|colab|docker] [--researcher] [--skip-validation]
```

## Step 2: Dispatch to mode file

**If mode is `judge`**: Read `.claude/skills/optimize/modes/judge.md` and execute its steps (J1–J6) in order, passing the remaining arguments (optional `file.md` and `--skip-validation` flag).

**If mode is `run`**: Read `.claude/skills/optimize/modes/run.md` and execute its Default Mode steps (R0–R7) in order, passing the remaining arguments along with any flags (`--team`, `--colab`, `--codex`, `--researcher`, `--journal`, `--hypothesis`, `--architect`). When `--team` is active, `run.md` delegates to `.claude/skills/optimize/modes/team.md`.

**If mode is `plan`**: Read `.claude/skills/optimize/modes/plan.md` and execute its Plan Mode steps (P-P0–P-P3), passing the remaining arguments as `<goal|file> [out.md]`.

**If mode is `resume`**: Read `.claude/skills/optimize/modes/run.md` and execute its Resume Mode steps, passing the optional `file.md` argument along with any flags (`--team`, `--colab`, `--codex`, `--compute`).

**If mode is `sweep`**: Read `.claude/skills/optimize/modes/sweep.md` and execute its steps (S1–S5) in order, passing the remaining arguments (goal prompt, optional `--skip-validation`) and flags (`--team`, `--colab[=HW]`, `--codex`, `--researcher`, `--compute`).

</workflow>

<notes>

**Cross-mode follow-up chains:**

- Quick perf investigation → `/optimize plan <file.py>` to profile first, then decide the goal
- Wizard produces `program.md` → run `/optimize judge` before starting the expensive run loop
- Judge emits NEEDS-REVISION or BLOCKED → fix the flagged items, re-run `/optimize judge` to confirm, then proceed to `/optimize run`
- Run improves metric → `/review` for quality validation of kept commits
- Run metric plateauing → `/research` for SOTA comparison — maybe a fundamentally different approach is needed
- Run kept commits accumulate technical debt or bottleneck is architectural (not just a hot loop) → `/develop refactor` for structural cleanup and architectural changes with test safety net
- Quick end-to-end without interactive wizard → `/optimize sweep "goal"` to auto-plan, validate, and run in one shot

**Mode file locations**: `plan` lives in `modes/plan.md` (P-P0–P-P3). `run` and `resume` live in `modes/run.md` (R0–R7 + Resume Mode). `--team` extension lives in `modes/team.md` (Phases A–D). `judge` lives in `modes/judge.md`. `sweep` lives in `modes/sweep.md` (S1–S5).

**Research pipeline**: `--researcher` activates pre-phase hypothesis generation (R0) before the campaign loop — see `.claude/rules/optimize-hypothesis-protocol.md` for `hypotheses.jsonl` schema, `checkpoint.json`, and entry format. `--journal` records all outcomes (kept and reverted) to `journal.md` for failure feedback. `--architect` enables a parallel architectural hypothesis pass via `solution-architect`.

</notes>
