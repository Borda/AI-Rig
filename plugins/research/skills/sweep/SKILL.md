---
name: sweep
description: Non-interactive end-to-end pipeline — auto-configure program.md (accept defaults), run judge+refine loop (up to 3 iterations), then run the campaign. Single command from goal to result.
argument-hint: '"<goal>" [--team] [--compute=local|colab|docker] [--colab[=H100|L4|T4|A100]] [--codex] [--researcher] [--architect] [--skip-validation] [--out <path>]'
effort: high
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Non-interactive end-to-end research pipeline: auto-plan → judge gate → run. Single command from goal to result. Accepts goal string, passes through all run/colab/team flags.

NOT for: interactive planning (use `/research:plan`); methodology review only (use `/research:judge`); running already-approved plan (use `/research:run`).

</objective>

<workflow>

## Agent Resolution

> **Foundry plugin check**: run `Glob(pattern="foundry*", path="$HOME/.claude/plugins/cache/")` returning results = installed. If check fails, proceed as if foundry available — common case; only fall back if agent dispatch explicitly fails.

Sweep delegates to plan (S2), judge (S3), and run (S5) skill steps — see each skill's Agent Resolution section for fallback handling.

## Steps S1–S5

Triggered by `sweep "goal" [--flags]`. Non-interactive end-to-end: auto-plan → judge gate → run.

**Task tracking**: create tasks for S1–S5 at start.

### Step S1: Parse arguments

Extract `<goal>` — first positional argument (quoted or unquoted string describing optimization target).

Extract flags:

- `--colab[=HW]` — passed to plan (Config.compute) and run; if `=HW` present, extract `colab_hw`
- `--compute=local|colab|docker` — passed through
- `--team` — passed through to run
- `--codex` — passed through to run
- `--researcher` — passed through to run; combine with `--architect` for dual-agent SOTA + architectural hypothesis pipeline (`--journal` and `--hypothesis` not available in sweep mode)
- `--architect` — passed through to run; enables architectural hypothesis pass via `foundry:solution-architect`
- `--skip-validation` — passed to judge step (S3)
- `--out <path>` — optional: write program.md to this path instead of project root

If `<goal>` missing or empty, stop:

```text
⚠ sweep requires a goal prompt.
Usage: /research:sweep "goal description" [--flags]
```

### Step S2: Non-interactive plan

Run plan mode steps P-P2 and P-P3 from `plugins/research/skills/plan/SKILL.md` (P-P0 skipped — `<goal>` always text string; P-P1 skipped — goal provided explicitly) with overrides:

- **P-P2 (config presentation)**: Accept all auto-detected defaults without prompting. Print proposed config as informational block prefixed `sweep: auto-config →` — do NOT wait for confirmation.
- If `--colab[=HW]` or `--compute=colab` passed, write `compute: colab` (and `colab_hw: <HW>` if provided) into Config block.
- **P-P3 (write program.md)**: Write to `<--out path>` if provided; else `program.md` at project root.
  - If output path exists: rename to `<path>.bak` (overwrite existing `.bak`), proceed — no confirmation in sweep mode.

Print on completion:

```text
sweep: plan → <output path> ✓
```

### Step S3: Judge + refinement loop

Initialize `REFINE_ITER = 0`, `MAX_REFINE = 3`.

Repeat up to `MAX_REFINE` times:

1. Increment `REFINE_ITER`. Run judge mode (J1–J6 from `plugins/research/skills/judge/SKILL.md`) against program file.

   - Pass `--skip-validation` if user provided it; else include validation (J4).
   - Capture J6 verdict and judge report path (`JUDGE_REPORT`).

2. Print: `` sweep: judge iteration `REFINE_ITER`/`MAX_REFINE` → `VERDICT`  ``

3. **If `APPROVED`** — exit loop, outcome `approved`.

4. **If `BLOCKED`** — exit loop, outcome `blocked`. No fix attempt — BLOCKED = fundamental design flaw requiring human redesign.

5. **If `NEEDS-REVISION`**:

   - If `REFINE_ITER < MAX_REFINE`:
     - Read `JUDGE_REPORT`. Extract `### Required Changes` section.
     - Apply each fix to program file via Edit tool. Count as `N_FIXES`.
     - Print: `sweep: applied N_FIXES fix(es) to <program path> — re-judging`
     - Continue next iteration.
   - If `REFINE_ITER == MAX_REFINE` — exit loop, outcome `unresolved`.

> **Safety net**: `.bak` from S2 is undo path — loop edits modify `program.md` in place.

### Step S4: Gate on loop outcome

| Outcome | Action |
| --- | --- |
| `approved` | Print `sweep: plan approved (REFINE_ITER/MAX_REFINE iteration(s)) ✓` → proceed to S5 |
| `blocked` | Print `sweep: judge → BLOCKED ✗`; show all critical findings from the report; print follow-up hint; stop |
| `unresolved` | Print `sweep: judge unresolved after MAX_REFINE iterations ✗`; show remaining Required Changes from the last report; call `AskUserQuestion` tool — do NOT write options as plain text: question "Unresolved — how to proceed?", (a) label `proceed to run anyway`, (b) label `fix manually then re-run`, (c) label `abort` — if `a`, proceed to S5; if `b` or `c`, print follow-up hint and stop |

Follow-up hint (blocked or unresolved):

```text
Fix the issues above in <program path>, then:
  /research:judge <program path>          ← re-validate
  /research:run <program path>            ← run when approved
  /research:sweep "revised goal" [flags]  ← re-sweep from scratch
```

### Step S5: Run

Run Default Mode (R1–R7 from `plugins/research/skills/run/SKILL.md`) against program file from S2, passing all flags:

- `--colab[=HW]` / `--compute`
- `--team`
- `--codex`
- `--researcher` / `--architect` (combine for dual-agent pipeline)

> Note: `--journal` and `--hypothesis` not available in sweep mode (see S1).

> **`--team` and interactivity**: when `--team` passed, sweep semi-interactive — run mode Phase B presents user confirmation gate before Phase C. Gate cannot be bypassed from sweep context; sweep pauses and waits. Expected behavior.

On completion, standard R6 terminal summary printed. Also prepend:

```text
sweep: complete — plan → judge → run pipeline finished
```

</workflow>
