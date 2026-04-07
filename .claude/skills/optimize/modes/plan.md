<!-- Mode-file include: loaded by .claude/skills/optimize/SKILL.md — not a standalone skill -->

<!-- Implements one mode: plan (P-P0–P-P3) -->

## Plan Mode (Steps P-P0–P-P3)

<!-- P-P prefix = Plan-mode steps; R-prefix = Run-mode steps; these labels appear in task-tracking instructions -->

Triggered by `plan <goal|file>`. Interactive wizard to configure a run.

**Task tracking**: create tasks for P-P0, P-P1, P-P2, P-P3 at start.

### Step P-P0: Detect input type

Parse `<input>` from arguments. Determine whether it is a **file path** or a **goal string**:

1. If the argument contains no spaces AND `test -f <argument>` succeeds → **file path**. Enter profiling flow below.
2. Otherwise → **goal string**. Skip to Step P-P1.

**Profiling flow** (file path detected):

Run baseline profiling:

```bash
python3 -m cProfile -s cumtime "$ARGUMENTS" 2>&1 | head -40
time python3 "$ARGUMENTS"
```

Present the top 5 bottleneck functions. Then ask:

```
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

Construct a goal string from the user's selection:

- (a) → `"Reduce wall-clock execution time of <file>"`
- (b) → `"Reduce peak memory usage of <file>"`
- (c) → `"Optimize <function> in <file> (currently <time>s)"`
- (d) → user's text

Set the constructed string as `<goal>` and proceed to Step P-P1.

### Step P-P1: Parse and scan

**Scope guard (first action)**: Before scanning, check whether `<goal>` is an optimization goal. If the input is clearly not an optimization goal — e.g., a question about code semantics, a regex or algorithm explanation request, a debugging question, or any prompt that does not describe a measurable improvement target — print:

```
⚠ This input does not look like an optimization goal.
/optimize plan expects: "Reduce X" / "Increase Y" / "Improve Z metric".
Use /research for explanatory questions.
```

Then stop. Do not proceed to P-P2 or P-P3.

Parse `<goal>` from arguments. Scan the codebase to detect:

- Language and framework (Python, PyTorch, pytest, etc.)
- Available test runners or benchmark scripts
- Candidate metric commands (pytest coverage, benchmark scripts, eval scripts)
- Candidate guard commands (test suite, lint, type check)
- Files relevant to the goal (scope files)

### Step P-P2: Present proposed config

Present the proposed config as a code block for user review. Include:

```
metric_cmd:      [command that prints a single numeric result]
metric_direction: higher | lower
guard_cmd:       [command that must pass (exit 0) on every kept commit]
max_iterations:  [default 20]
agent_strategy:  [auto | perf | code | ml | arch]
scope_files:     [files the ideation agent may modify]
compute:         local | colab | docker
```

Dry-run both commands before presenting. If either fails, flag the error and propose corrections. Do not proceed to P-P3 until the user confirms or edits the config.

### Step P-P3: Write program.md

Determine the output path: if the user provided a second argument after `<goal>`, use that path; otherwise use `program.md` at the project root.

**Overwrite check**: if the output path already exists, print a one-line warning and use `AskUserQuestion` to ask: (a) Overwrite — proceed; (b) Abort — stop. No silent overwrite.

Write the file using this canonical template, pre-populated from the wizard's findings:

````markdown
# Program: <title from goal>

## Goal
<one-paragraph description of what to improve and why>

## Metric
```
command: <metric_cmd from wizard>
direction: higher | lower
target: <optional numeric goal — campaign stops when crossed>
```

## Guard
```
command: <guard_cmd from wizard>
```

## Config
```
max_iterations: 20
agent_strategy: auto | perf | code | ml | arch
scope_files:
  - <path or glob>
compute: local | colab | docker
colab_hw:                         # optional: H100 | L4 | T4 | A100 (used when compute: colab)
sandbox_network: none | bridge
```

## Notes
<optional free-form text — strategy hints, context, known constraints — ignored by the skill>
````

Print:

```
✓ Program saved to program.md

Next steps:
  /optimize judge program.md   ← validate plan before running (recommended)
  /optimize run program.md     ← start iteration loop directly
```
