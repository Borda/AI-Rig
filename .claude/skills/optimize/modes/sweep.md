# Sweep Mode

______________________________________________________________________

## Steps S1–S5

Triggered by `sweep "goal" [--flags]`. Non-interactive end-to-end pipeline: auto-plan → judge gate → run.

**Task tracking**: create tasks for S1–S5 at start.

### Step S1: Parse arguments

Extract `<goal>` — the first positional argument (quoted or unquoted string describing the optimization target).

Extract flags and their values:

- `--colab[=HW]` — passed through to plan (Config.compute) and run; if `=HW` present, extract `colab_hw`
- `--compute=local|colab|docker` — passed through
- `--team` — passed through to run
- `--codex` — passed through to run
- `--researcher` — passed through to run; combine with `--architect` for dual-agent SOTA + architectural hypothesis pipeline (`--journal` and `--hypothesis` are not available in sweep mode)
- `--architect` — passed through to run; enables architectural hypothesis pass via `solution-architect`
- `--skip-validation` — passed to judge step (S3) to skip local metric/guard validation
- `--out <path>` — optional: write program.md to this path instead of project root

If `<goal>` is missing or empty, stop:

```
⚠ sweep requires a goal prompt.
Usage: /optimize sweep "goal description" [--flags]
```

### Step S2: Non-interactive plan

Run plan mode steps P-P2 and P-P3 from `modes/plan.md` (P-P0 profiling flow skipped — `<goal>` is always a text string; P-P1 scope guard skipped — goal was provided explicitly) with these behavioral overrides:

- **P-P2 (config presentation)**: Accept all auto-detected defaults without prompting the user. Print the proposed config as an informational block prefixed `sweep: auto-config →` but do NOT wait for user confirmation.
- If `--colab[=HW]` or `--compute=colab` was passed, write `compute: colab` (and `colab_hw: <HW>` if provided) into the Config block.
- **P-P3 (write program.md)**: Write to `<--out path>` if provided; otherwise to `program.md` at project root.
  - If the output path already exists: rename existing to `<path>.bak` (overwrite any existing `.bak`), then proceed — no interactive confirmation in sweep mode.

Print on completion:

```
sweep: plan → <output path> ✓
```

### Step S3: Judge + refinement loop

Initialize `REFINE_ITER = 0`, `MAX_REFINE = 3`.

Repeat up to `MAX_REFINE` times:

1. Increment `REFINE_ITER`. Run judge mode (J1–J6 from `modes/judge.md`) against the program file.

   - Pass `--skip-validation` if the user provided it; otherwise include validation (J4).
   - Capture the J6 verdict and the judge report path (`JUDGE_REPORT`).

2. Print: `` sweep: judge iteration `REFINE_ITER`/`MAX_REFINE` → `VERDICT`  ``

3. **If `APPROVED`** — exit loop with outcome `approved`.

4. **If `BLOCKED`** — exit loop with outcome `blocked`. Do not attempt to fix — BLOCKED means a fundamental design flaw that requires human redesign.

5. **If `NEEDS-REVISION`**:

   - If `REFINE_ITER < MAX_REFINE`:
     - Read `JUDGE_REPORT`. Extract the `### Required Changes` section.
     - Apply each listed fix to the program file using the Edit tool. Count applied edits as `N_FIXES`.
     - Print: `sweep: applied N_FIXES fix(es) to <program path> — re-judging`
     - Continue to next iteration.
   - If `REFINE_ITER == MAX_REFINE` — exit loop with outcome `unresolved`.

> **Safety net**: the `.bak` file created in S2 is the undo path — edits applied during the loop modify `program.md` in place.

### Step S4: Gate on loop outcome

| Outcome      | Action                                                                                                                                                                                                                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `approved`   | Print `sweep: plan approved (REFINE_ITER/MAX_REFINE iteration(s)) ✓` → proceed to S5                                                                                                                                                                                                       |
| `blocked`    | Print `sweep: judge → BLOCKED ✗`; show all critical findings from the report; print follow-up hint; stop                                                                                                                                                                                   |
| `unresolved` | Print `sweep: judge unresolved after MAX_REFINE iterations ✗`; show remaining Required Changes from the last report; **ask user** with options: `(a) proceed to run anyway  (b) fix manually then re-run  (c) abort` — if `a`, proceed to S5; if `b` or `c`, print follow-up hint and stop |

Follow-up hint (blocked or unresolved):

```
Fix the issues above in <program path>, then:
  /optimize judge <program path>          ← re-validate
  /optimize run <program path>            ← run when approved
  /optimize sweep "revised goal" [flags]  ← re-sweep from scratch
```

### Step S5: Run

Run Default Mode (R1–R7 from `modes/run.md`) against the program file from S2, passing through all flags:

- `--colab[=HW]` / `--compute`
- `--team`
- `--codex`
- `--researcher` / `--architect` (combine both for dual-agent pipeline)

> Note: `--journal` and `--hypothesis` are not available in sweep mode (see S1).

> **`--team` and interactivity**: when `--team` is passed, sweep becomes semi-interactive — run mode's Phase B presents a user confirmation gate before Phase C begins. The gate cannot be bypassed from sweep context; sweep will pause and wait for user input at that point. This is expected behavior.

On completion, the standard R6 terminal summary is printed. Additionally, prepend:

```
sweep: complete — plan → judge → run pipeline finished
```
