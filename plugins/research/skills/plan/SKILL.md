---
name: plan
description: Interactive wizard that scans the codebase, proposes a metric/guard/agent config, and writes a program.md run spec. Also runs cProfile on a file path to surface bottlenecks before prompting for optimization goal.
argument-hint: <goal> | <file.py> [out.md]
effort: medium
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Wizard: scans codebase, proposes metric/guard/agent config, writes `program.md` run spec. Also runs cProfile on file path to surface bottlenecks before prompting for optimization goal.

NOT for: running experiments (use `/research:run`); methodology validation (use `/research:judge`); full pipeline from goal to result (use `/research:sweep`).

</objective>

<workflow>

<!-- Agent Resolution: canonical table at plugins/research/skills/_shared/agent-resolution.md -->

## Agent Resolution

```bash
# Locate research plugin shared dir — installed first, local workspace fallback
_RESEARCH_SHARED=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/research/*/skills/_shared 2>/dev/null | head -1)
[ -z "_RESEARCH_SHARED" ] && _RESEARCH_SHARED="plugins/research/skills/_shared"
```

Read `$_RESEARCH_SHARED/agent-resolution.md`. Contains: foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:solution-architect`, `foundry:perf-optimizer`.

## Plan Mode (Steps P-P0–P-P3)

<!-- P-P prefix = Plan-mode steps; R-prefix = Run-mode steps; these labels appear in task-tracking instructions -->

Triggered by `plan <goal|file>`. Wizard configures run.

**Task tracking**: create tasks for P-P0, P-P1, P-P2, P-P2b, P-P3 at start.

### Step P-P0: Detect input type

Parse `<input>` from arguments. Determine: **file path** or **goal string**:

1. No spaces AND `test -f <argument>` succeeds → **file path**. Enter profiling flow.
2. Otherwise → **goal string**. Skip to Step P-P1.

**Profiling flow** (file path detected):

Run baseline profiling:

```bash
python3 -m cProfile -s cumtime "$ARGUMENTS" 2>&1 | head -40  # timeout: 60000
PROFILE_EXIT=${PIPESTATUS[0]}
[ $PROFILE_EXIT -ne 0 ] && echo "cProfile failed (exit $PROFILE_EXIT)" && exit 1
time python3 "$ARGUMENTS"  # timeout: 60000
```

Present top 5 bottleneck functions. Ask:

```markdown
Top bottleneck functions:
1. <function> — <cumtime>s (<percentage>%)
2. <function> — <cumtime>s (<percentage>%)
...

What would you like to optimize?
  (a) Overall execution time
  (b) Memory usage
  (c) Specific function: <top function name>
  (d) Custom goal: <describe>
```

Construct goal string from selection:

- (a) → `"Reduce wall-clock execution time of <file>"`
- (b) → `"Reduce peak memory usage of <file>"`
- (c) → `"Optimize <function> in <file> (currently <time>s)"`
- (d) → user's text

Set as `<goal>`, proceed to P-P1.

### Step P-P1: Parse and scan

**Scope guard (first action)**: Before scanning, check `<goal>` is optimization goal. Input clearly not optimization goal (code question, regex/algo explanation, debug question, or any prompt without measurable improvement target) → print:

```text
Warning: This input does not look like an optimization goal.
/research:plan expects: "Reduce X" / "Increase Y" / "Improve Z metric".
Use /research for explanatory questions.
```

Stop. Do not proceed to P-P2 or P-P3.

Parse `<goal>`. Scan codebase to detect:

- Language and framework (Python, PyTorch, pytest, etc.)
- Available test runners or benchmark scripts
- Candidate metric commands (pytest coverage, benchmark scripts, eval scripts)
- Candidate guard commands (test suite, lint, type check)
- Files relevant to goal (scope files)

### Step P-P2: Present proposed config

Present config as code block for review. Include:

```yaml
metric_cmd:      [command that prints a single numeric result]
metric_direction: higher | lower
guard_cmd:       [command that must pass (exit 0) on every kept commit]
max_iterations:  [default 20]
agent_strategy:  [auto | perf | code | ml | arch]
scope_files:     [files the ideation agent may modify]
compute:         local | colab | docker
```

Dry-run both commands before presenting (add `# timeout: 60000` to any timed bash calls — user commands may run for minutes). Failure → flag error, propose corrections. Do not proceed to P-P3 until user confirms or edits.

### Step P-P2b: Agent validation (pre-write)

After user confirms, run expert agent review before writing `program.md`. Dispatches conditional on goal type — run whichever apply in parallel:

**Always** — spawn architect to validate scope coverage:

```text
Agent(subagent_type="foundry:solution-architect", prompt="Review a proposed research experiment scope.\n\nGoal: <goal>\nScope files: <scope_files>\nMetric command: <metric_cmd>\n\nCheck: (1) Do scope_files cover the components relevant to the goal? List architectural dependencies outside scope that the ideation agent would need to touch. (2) Are there shared abstractions (base classes, imports, shared state) outside scope required for changes within it?\n\nReturn ONLY: {\"ok\":true|false,\"gaps\":[\"...\"],\"suggestions\":[\"...\"],\"confidence\":0.N}")
```

**If `agent_strategy = ml` or goal contains ML keywords (accuracy, loss, model, training, inference, classification, regression)** — also spawn research:scientist:

```text
Agent(subagent_type="research:scientist", prompt="Review a proposed ML experiment configuration.\n\nGoal: <goal>\nMetric command: <metric_cmd>\nAgent strategy: <agent_strategy>\n\nCheck: (1) Is the goal a well-formed ML hypothesis — falsifiable, with a concrete success criterion? (2) Could metric_cmd improve while the real goal is not achieved (Goodhart's Law)? (3) Is agent_strategy appropriate for this goal type?\n\nReturn ONLY: {\"ok\":true|false,\"issues\":[\"...\"],\"suggestions\":[\"...\"],\"confidence\":0.N}")
```

**If `agent_strategy = perf` or goal contains performance keywords (latency, throughput, wall-clock, speed, memory, FPS)** — also spawn perf:

```text
Agent(subagent_type="foundry:perf-optimizer", prompt="Review a proposed performance experiment configuration.\n\nGoal: <goal>\nMetric command: <metric_cmd>\nGuard command: <guard_cmd>\n\nCheck: (1) Does metric_cmd measure the right performance characteristic for this goal? (2) Is guard_cmd comprehensive enough to catch regressions an ideation agent might introduce?\n\nReturn ONLY: {\"ok\":true|false,\"issues\":[\"...\"],\"suggestions\":[\"...\"],\"confidence\":0.N}")
```

Print advisory block below config:

```text
Advisory review:
  architect: <gaps or "scope looks complete">
  scientist:  <issues or "hypothesis is well-formed">   [only if dispatched]
  perf:       <issues or "metric/guard look valid">      [only if dispatched]
```

Any agent returns `ok: false` → surface suggestions, ask user: revise config (re-enter P-P2) or proceed. Do not block — user decides.

### Step P-P3: Write program.md

Output path: second argument after `<goal>` if provided, else `program.md` at project root.

**Overwrite check**: path exists → print one-line warning, `AskUserQuestion`: (a) Overwrite — proceed; (b) Abort — stop. No silent overwrite.

Write file using canonical template, pre-populated from wizard findings:

````markdown
# Program: <title from goal>

## Goal
<one-paragraph description of what to improve and why>

## Metric
```yaml
command: <metric_cmd from wizard>
direction: higher | lower
target: <optional numeric goal — campaign stops when crossed>
```

## Guard
```yaml
command: <guard_cmd from wizard>
```

## Config
```yaml
max_iterations: 20
agent_strategy: auto | perf | code | ml | arch
scope_files:
  - <path or glob>
compute: local | colab | docker
colab_hw: # optional: H100 | L4 | T4 | A100 (used when compute: colab)
sandbox_network: none | bridge
```

## Notes
<optional free-form text — strategy hints, context, known constraints — ignored by the skill>
````

Print:

```text
✓ Program saved to program.md

Next steps:
  /research:judge program.md   ← validate plan before running (recommended)
  /research:run program.md     ← start iteration loop directly
```

## --team flag

`--team` detected in `$ARGUMENTS`:
1. Complete Steps 1–3 as normal — produce `program.md` with full single-researcher structure.
2. Append a `## Team Mode Notes` section to `program.md`:
   - Number of distinct method families found (used to determine team size at run step)
   - Whether SOTA consensus exists — if clear winner, note team mode may not add value
3. Tell user: "`--team` applies at run step, not plan step. Run: `/research:run <program.md> --team` to execute with parallel researchers."
4. Read `${CLAUDE_SKILL_DIR}/../run/modes/team.md` for team spawn protocol — include a one-line summary of the team protocol in the Team Mode Notes section.

</workflow>
