# 🔬 research — Claude Code Plugin

ML research plugin: two specialist agents and eight slash-command skills for literature search, experiment design, methodology review, metric-driven improvement loops, and automated research sweeps — built on a profile-first, judge-gated pipeline that spends compute only on experiments worth running.

> Works standalone — `foundry` is not required. Without it, agent dispatches fall back to `general-purpose` with role descriptions (lower quality). Installing `foundry` unlocks specialized agents (`foundry:sw-engineer`, `foundry:perf-optimizer`, etc.) and is strongly recommended.

______________________________________________________________________

<details>
<summary><strong>📋 Contents</strong></summary>

- [What is research?](#what-is-research)
- [Why research?](#why-research)
- [Install](#install)
- [Quick start](#quick-start)
- [Skills reference](#skills-reference)
  - [`/research:topic`](#researchtopic--sota-literature-search)
  - [`/research:plan`](#researchplan--experiment-configuration-wizard)
  - [`/research:judge`](#researchjudge--methodology-gate)
  - [`/research:run`](#researchrun--metric-improvement-loop)
  - [`/research:sweep`](#researchsweep--non-interactive-end-to-end-pipeline)
  - [`/research:verify`](#researchverify--paper-vs-code-consistency-audit)
  - [`/research:fortify`](#researchfortify--ablation-study-runner)
  - [`/research:retro`](#researchretro--post-run-retrospective)
- [Agents reference](#agents-reference)
  - [`research:scientist`](#researchscientist)
  - [`research:data-steward`](#researchdata-steward)
- [Workflow overview](#workflow-overview)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Contributing and feedback](#contributing-and-feedback)
- [Acknowledgments](#acknowledgments)

</details>

______________________________________________________________________

## 🤔 What is research?

`research` turns the messy, iterative cycle of ML improvement into a structured pipeline. You start with evidence from the literature, write a machine-readable experiment spec, get a methodology review before you spend any GPU time, and run an automated improvement loop that commits every change atomically and rolls back anything that regresses your target metric.

It is for ML engineers and researchers who are tired of ad-hoc experiment management — running things that were never properly scoped, losing track of what was tried, or discovering after 20 GPU-hours that the experiment design had a flaw that could have been caught in 5 minutes.

______________________________________________________________________

## 🎯 Why research?

Without it, a typical improvement cycle looks like this: you have an intuition, you run an experiment, it does not help, you are not sure why, and the next person on the team does not know what was tried. Baselines drift. GPU hours disappear. Papers get implemented with subtle hyperparameter mismatches that invalidate the results.

With `research`, the loop looks like this instead:

1. You search the literature before writing a single line of code (`/research:topic`).
2. You write down the hypothesis, metric, and success criterion in a single file (`/research:plan`).
3. A methodology reviewer checks whether the experiment is well-formed before it runs (`/research:judge`).
4. An automated loop proposes changes, commits them, measures the metric, and rolls back regressions — without you watching it (`/research:run`).
5. After the run, you get statistical significance, dead iteration detection, and a queue of next hypotheses (`/research:retro`).
6. You verify that your implementation actually matches the paper it came from (`/research:verify`).
7. You run ablations to find out which components actually mattered (`/research:fortify`).

Nothing is lost. Every iteration is logged. Every rollback is a reversible `git revert`. You can stop, resume, hand off to a teammate, and the full history is in `.experiments/`.

______________________________________________________________________

## 📦 Install

**Prerequisite**: Claude Code with plugin support. The plugin lives in the Borda-AI-Rig repository.

Run these commands from the directory that *contains* your `Borda-AI-Rig` clone (not from inside it):

```bash
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install research@borda-ai-rig
```

Install the full suite for best results (`foundry` unlocks specialist agents):

```bash
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

<details>
<summary><strong>Upgrade</strong></summary>

```bash
cd Borda-AI-Rig && git pull
claude plugin install research@borda-ai-rig
```

</details>

<details>
<summary><strong>Uninstall</strong></summary>

```bash
claude plugin uninstall research
```

</details>

All skills are invoked with the `research:` prefix: `/research:topic`, `/research:plan`, `/research:judge`, `/research:run`, `/research:sweep`, `/research:verify`, `/research:fortify`, `/research:retro`.

______________________________________________________________________

## ⚡ Quick start

The one command that gives you immediate value on an existing project:

```text
/research:plan "improve F1 from 0.82 to 0.87"
```

This runs an interactive wizard that scans your codebase, proposes a metric command, guard command, and iteration budget, and writes `program.md` at your project root. From there:

```text
/research:judge          # validate methodology before spending compute
/research:run program.md # run the improvement loop
```

You will see output like this as the run progresses:

```text
Baseline: f1_score = 0.820
[-> Iter 1/20 — best so far: 0.820 (D0.0% vs baseline)]
[✓ Iter 1/20 — kept · metric=0.831 (D1.3%) · agent=research:scientist]
[-> Iter 2/20 — best so far: 0.831 (D1.3% vs baseline)]
[✓ Iter 2/20 — reverted · metric=0.818 (D-0.2%) · agent=research:scientist]
```

Every kept iteration is a commit. Every reverted iteration is a `git revert` — the history is preserved and the baseline is never damaged.

______________________________________________________________________

## 🔧 Skills reference

### `/research:topic` — SOTA literature search

Searches the AI/ML literature for a topic, builds a comparison table of methods, and produces a recommendation with an implementation plan mapped to your codebase. Delegates literature search to `research:scientist` and codebase mapping to `foundry:solution-architect`.

**Invocation**:

```text
/research:topic "<topic>"
/research:topic "<topic>" --team
/research:topic plan                       # produce implementation plan from most recent output
/research:topic plan <path/to/output.md>   # produce plan from a specific output file
```

**Flags**:

- `--team`: spawn 2–3 researcher instances on competing method families in parallel. Use when 3+ distinct method families exist and there is no clear SOTA consensus. Expect roughly 7x token cost versus single-agent mode.

**Output**: full report written to `.temp/output-research-<branch>-<date>.md`; compact summary printed to terminal.

**Plan mode**: after running `/research:topic`, run `/research:topic plan` to produce a phased implementation roadmap (written to `.temp/output-research-plan-<branch>-<date>.md`) ready to hand off to `/develop:feature`.

**Realistic example**:

```text
/research:topic "efficient fine-tuning methods for LLMs"
# comparison table: LoRA, IA3, prefix tuning, full fine-tune
# recommendation: LoRA given your single-GPU budget
# 3-phase implementation plan with file-level tasks

/research:topic plan   # convert recommendation into phased roadmap
```

______________________________________________________________________

### `/research:plan` — experiment configuration wizard

Interactive wizard that scans your codebase, proposes a `metric_cmd`, `guard_cmd`, and experiment config, and writes `program.md`. Also accepts a Python file path to profile first — it runs `cProfile`, shows the top bottlenecks, and asks what you want to optimize.

**Invocation**:

```text
/research:plan "<goal>"          # interactive wizard from a goal string
/research:plan src/train.py      # profile-first: run cProfile, then wizard
/research:plan "<goal>" out.md   # write to a specific output path
```

**What it writes** (`program.md`):

```markdown
# Program: <title>

## Goal
## Metric      <- metric_cmd, direction, optional target
## Guard       <- guard_cmd (must exit 0 to keep a commit)
## Config      <- max_iterations, agent_strategy, scope_files, compute
## Notes       <- human-readable hints for the ideation agent
```

The wizard dry-runs both commands before writing the file and dispatches expert agents (architect, scientist, perf-optimizer depending on goal type) to review the config before it is written.

After writing, the wizard suggests:

```text
Next steps:
  /research:judge program.md   <- validate plan before running (recommended)
  /research:run program.md     <- start iteration loop directly
```

**Profile-first example**:

```text
/research:plan src/train.py
# runs cProfile, shows top 5 bottleneck functions
# "What would you like to optimize?"
# wizard continues from your selection
```

______________________________________________________________________

### `/research:judge` — methodology gate

Validates `program.md` before the expensive run. Acts as a research supervisor reviewing an experimental protocol across seven dimensions. Never modifies code or state — read-only.

**Invocation**:

```text
/research:judge                    # auto-detect program.md at project root
/research:judge path/to/plan.md    # review a specific file
/research:judge --skip-validation  # skip dry-run of metric/guard commands
```

Use `--skip-validation` when writing `program.md` on one machine but planning to run on a remote GPU where the commands are not locally executable.

**What it checks**:

- **Completeness audit** (12 items): Goal present, Metric has command and direction, Guard has command, scope_files exist on disk, max_iterations in bounds, and more.
- **Methodology review** (7 dimensions via `foundry:solution-architect`): hypothesis clarity, measurement validity, control adequacy, experimental scope, protocol consistency, stopping criteria, reproducibility.
- **Scientific rigor** (4 dimensions via `research:scientist`): hypothesis falsifiability, Goodhart's Law risk, missing baselines, reproducibility risks.
- **Dry-run validation**: executes `metric_cmd` and `guard_cmd` once to confirm they produce numeric output and exit 0.
- **Codex adversarial review**: if the `codex` plugin is installed, runs a second adversarial pass on the top findings.

**Verdicts**:

| Verdict          | Meaning                                             |
| ---------------- | --------------------------------------------------- |
| `APPROVED`       | Protocol is sound — proceed to `/research:run`      |
| `NEEDS-REVISION` | Fixable issues found — see Required Changes section |
| `BLOCKED`        | Fundamental design flaw — redesign before running   |

The verdict is deterministic: computed from finding counts and methodology rating, not inferred from prose.

**Output**: full report to `.temp/output-judge-<branch>-<date>.md`.

**Example**:

```text
/research:judge program.md
# Verdict: NEEDS-REVISION
# Finding: C7 — target not set; campaign will run to max_iterations
# Finding: measurement validity — metric_cmd measures proxy, not actual F1
# Required Changes: (1) add target: 0.87 to ## Metric  (2) replace metric_cmd
```

______________________________________________________________________

### `/research:run` — metric-improvement loop

The core loop. Reads `program.md`, establishes a baseline, then iterates: spawn ideation agent, implement change, commit, measure metric, run guard, keep or revert. All changes are atomic git commits. Regressions are `git revert`ed automatically — the history is preserved and the baseline is never damaged.

**Invocation**:

```text
/research:run program.md
/research:run program.md "focus on attention layers"   # clarification hint to ideation agent
/research:run program.md --team                        # parallel hypothesis exploration
/research:run program.md --compute=docker              # run metric/guard in Docker sandbox
/research:run program.md --colab                       # route metric verification to Colab
/research:run program.md --colab=H100                  # request specific GPU type
/research:run program.md --codex                       # Codex co-pilot every iteration
/research:run program.md --researcher                  # pre-generate hypotheses via scientist
/research:run program.md --architect                   # pre-generate hypotheses via solution-architect
/research:run program.md --researcher --journal        # also log every iteration to journal
/research:run program.md --hypothesis path/to/hypotheses.jsonl  # consume a pre-built queue
/research:run --resume                                 # resume latest interrupted run
/research:run program.md --resume                      # resume a specific run
```

**Agent strategy** (set via `agent_strategy` in `program.md` or auto-detected from goal/metric keywords):

| Strategy | Agent                        | Use when goal contains         |
| -------- | ---------------------------- | ------------------------------ |
| `perf`   | `foundry:perf-optimizer`     | latency, throughput, memory    |
| `code`   | `foundry:sw-engineer`        | coverage, complexity, coupling |
| `ml`     | `research:scientist`         | accuracy, loss, F1, AUC        |
| `arch`   | `foundry:solution-architect` | modularity, cohesion           |
| `auto`   | inferred from keywords       | default                        |

**Keep/revert logic per iteration**:

| Condition                                | Action                                 |
| ---------------------------------------- | -------------------------------------- |
| Metric improved AND guard passes         | Keep commit                            |
| Metric improved AND guard fails          | Rework (up to 2 attempts), then revert |
| Improvement < 0.1% AND change > 50 lines | Discard (simplicity override)          |
| No improvement                           | Revert                                 |

**Stuck detection**: after 5 consecutive discards, the skill rotates to a different agent type automatically. If still stuck after two rotations, it surfaces to you and stops — no blind looping.

**State**: default run state goes into `.experiments/state/<run-id>/` — `state.json`, `experiments.jsonl` (one JSONL record per iteration), and `diary.md` (human-readable hypothesis-outcome log). Hypothesis-pipeline artifacts are separate: when `--researcher`, `--architect`, `--hypothesis`, or `--journal` is active, queue and learning files live in `.experiments/<run-id>/` as `hypotheses.jsonl`, `checkpoint.json`, and optionally `journal.md`. Resume uses both locations: state for iteration progress, checkpoint entries to skip already-tested hypotheses.

**Hypothesis pipeline** (`--researcher`, `--architect`, `--hypothesis`, `--journal`):

- `--researcher`: spawns `research:scientist` to write 5-10 ML experiment hypotheses grounded in SOTA literature and the metric goal.
- `--architect`: spawns `foundry:solution-architect` to write 5-10 architecture/refactoring hypotheses; when used alone, feasibility is considered already validated.
- `--researcher --architect`: runs both generators, merges their JSONL queues by priority, then runs a feasibility annotation pass.
- `--hypothesis <path>`: reads a pre-built `hypotheses.jsonl` queue and skips oracle generation.
- `--journal`: requires `--researcher` or `--architect`; appends every kept and reverted iteration to `.experiments/<run-id>/journal.md` so future ideation can avoid repeating failed approaches.

**Limits**: default 20 iterations; maximum 50 (never exceeded without explicit override in `program.md`).

**Example**:

```text
/research:run program.md --codex
# Baseline: f1_score = 0.820
# [-> Iter 1/20 — best so far: 0.820 (D0.0% vs baseline)]
# [✓ Iter 1/20 — kept · metric=0.831 (D1.3%) · agent=research:scientist]
# [✓ Iter 2/20 — reverted · metric=0.818 (D-0.2%) · agent=codex]
```

______________________________________________________________________

### `/research:sweep` — non-interactive end-to-end pipeline

Chains plan, judge (with auto-refinement), and run into a single non-interactive command. Designed for unattended runs — safe to kick off overnight.

**Invocation**:

```text
/research:sweep "<goal>"
/research:sweep "<goal>" --team
/research:sweep "<goal>" --compute=docker
/research:sweep "<goal>" --colab=H100
/research:sweep "<goal>" --codex --researcher
/research:sweep "<goal>" --skip-validation --out path/to/program.md
```

**Flags**: sweep passes through the run flags that are supported in sweep mode: `--team`, `--colab[=HW]`, `--compute`, `--codex`, `--researcher`, and `--architect`. `--journal` and `--hypothesis` are run-only flags; use `/research:run` directly when you need them. Additional sweep-specific flags:

- `--skip-validation`: skip dry-run in judge step (useful for cross-machine workflows)
- `--out <path>`: write `program.md` to a specific path instead of project root

**Judge refinement loop**: sweep runs judge up to 3 times, applying Required Changes between iterations. If the plan reaches `APPROVED`, the run starts automatically. If it hits `BLOCKED`, sweep stops and shows you the critical findings. If it cannot resolve `NEEDS-REVISION` after 3 iterations, it asks whether to proceed anyway or abort.

**When to use sweep vs manual pipeline**: use sweep when you want a single command and are comfortable with auto-configured defaults. Use `/research:plan` + `/research:judge` + `/research:run` when you want to review and tune the config yourself.

**Example**:

```text
/research:sweep "increase test coverage to 90%" --codex
# sweep: auto-config -> program.md
# sweep: judge iteration 1/3 -> NEEDS-REVISION
# sweep: applied 2 fix(es) to program.md — re-judging
# sweep: judge iteration 2/3 -> APPROVED
# sweep: plan approved (2/3 iteration(s))
# [-> Iter 1/20 — ...]
```

______________________________________________________________________

### `/research:verify` — paper-vs-code consistency audit

After implementing a method from a paper, verify that the implementation actually matches the paper's claims. Audits across five dimensions, produces a fidelity score, and flags mismatches with severity and specific fix instructions.

**Invocation**:

```text
/research:verify paper.pdf
/research:verify paper.pdf --scope "src/model/**/*.py"
/research:verify paper.pdf --program program.md         # use scope_files from program.md
/research:verify paper.pdf --strict                     # stop on HIGH severity formula/eval mismatch
/research:verify paper.pdf --dim F,H                    # audit only specific dimensions
```

**Five audit dimensions**:

| Code | Dimension             | What it checks                                                                                 |
| ---- | --------------------- | ---------------------------------------------------------------------------------------------- |
| F    | Formula matching      | Every equation — loss functions, forward passes, reductions (mean vs sum)                      |
| H    | Hyperparameter parity | LR, batch size, weight decay, scheduler, warmup steps — do code defaults match paper values?   |
| E    | Eval protocol         | Same metric (e.g. mAP@0.5 vs mAP@[0.5:0.95]), same test split, same preprocessing at inference |
| N    | Notation consistency  | Variable names in code vs paper notation — confusing mappings flagged                          |
| C    | Citation chain        | Does code implement the cited paper, or a derivative from a different paper?                   |

**Fidelity score**: `(MATCH + 0.5 * PARTIAL) / total_verified_claims`

| Score     | Rating            |
| --------- | ----------------- |
| >= 0.9    | HIGH fidelity     |
| 0.7 – 0.9 | MODERATE fidelity |
| < 0.7     | LOW fidelity      |

**Strict mode** (`--strict`): if any HIGH severity mismatch exists in dimensions F or E, stops immediately with a BREAKING notice. Use before running expensive experiments.

**Output**: full report to `.temp/output-verify-<branch>-<date>.md`.

**Example**:

```text
/research:verify paper.pdf --strict
# Fidelity: MODERATE (0.74)
# ! BREAKING — HIGH severity mismatch in F (formula)
# Fix: src/model.py:42 — loss uses 'mean' reduction but paper specifies 'sum'
```

______________________________________________________________________

### `/research:fortify` — ablation study runner

After `/research:run` finds improvements, fortify identifies which components actually mattered. It detects component candidates from the git diff and run diary, creates an isolated git worktree per ablation (main repo never touched), runs the metric and guard in each worktree, ranks components by importance, and optionally generates reviewer Q&A for a conference submission.

**Invocation**:

```text
/research:fortify                                # auto-detect latest completed run
/research:fortify <run-id>
/research:fortify program.md
/research:fortify --venue NeurIPS               # ablations + reviewer Q&A
/research:fortify --venue CVPR
/research:fortify --max-ablations 5             # cap at N ablation variants
/research:fortify --skip-run                    # identify candidates only, no execution
/research:fortify --compute=colab               # run metric/guard via Colab
```

**Prerequisites**: requires a completed `/research:run` AND an APPROVED `/research:judge` verdict for the same `program.md`. Fortify will refuse to run without both.

**Importance classification**:

| Class       | Condition                                          |
| ----------- | -------------------------------------------------- |
| CRITICAL    | Removing this component costs > 50% of full metric |
| SIGNIFICANT | 10–50% of full metric                              |
| MARGINAL    | < 10% of full metric                               |

Each ablation runs in its own git worktree created from `best_commit`. The main working tree is never modified. If `git revert` conflicts arise (two components touched the same lines), the variant is recorded as `revert-conflict` and reported — not treated as an error.

A `full` variant (all components present) runs as a sanity check and must reproduce `best_metric` within 2%. A divergence warning appears in the report — this catches non-deterministic metrics or environment changes between runs.

**Output**: full report to `.temp/output-fortify-<branch>-<date>.md`.

**Example**:

```text
/research:fortify --venue NeurIPS
# Components: 4 identified, 4 ablations completed
# Top:   learning-rate-warmup (importance: 62.3% CRITICAL)
# Other: label-smoothing (14.1% SIGNIFICANT)
#        dropout-schedule (7.2% MARGINAL)
#        weight-init (3.1% MARGINAL)
# Reviewer Q&A: generated for NeurIPS
```

______________________________________________________________________

### `/research:retro` — post-run retrospective

Analyzes the experiment history after `/research:run` completes. Computes statistical significance (Wilcoxon signed-rank test), detects dead iteration windows, flags suspicious metric jumps, and generates a next-hypothesis queue compatible with the `--hypothesis` flag of `/research:run`.

**Invocation**:

```text
/research:retro                                         # auto-detect latest completed run
/research:retro <run-id>
/research:retro <run-id> --compare <run-id-2>           # statistical comparison of two runs
/research:retro --threshold 0.005                       # dead iteration threshold (default 0.001)
/research:retro --alpha 0.01                            # significance level (default 0.05)
```

**What it produces**:

- **Statistical significance**: Wilcoxon signed-rank test comparing kept iteration metrics against the baseline. Requires N >= 6 kept iterations; falls back to descriptive stats otherwise. Requires `scipy` — install with `pip install scipy` if missing.
- **Dead iteration detection**: windows of 3+ consecutive iterations where `abs(delta) < threshold`. Classified as `dead-plateau` (kept iterations going nowhere) or `dead-churn` (mixed kept/reverted with no progress).
- **Suspicious jump detection**: single-iteration improvements more than 2 standard deviations above the running mean. Flagged as "suspicious — investigate"; never auto-labeled as data leakage.
- **Strategy effectiveness**: which agent type (perf/code/ml/arch) had the highest keep-rate and mean delta.
- **Next hypotheses**: 3–5 concrete hypotheses written to `.experiments/retro-<ts>/hypotheses.jsonl`, compatible with `/research:run program.md --hypothesis <path>`.

**Output**: full report to `.temp/output-retro-<branch>-<date>.md`.

**Example**:

```text
/research:retro
# Significance:  p=0.031 (significant at alpha=0.05)
# Effect size:   r=0.71 (large)
# Dead iters:    4/20 (20% of compute)
# Suspicious:    1 jump (MEDIUM — investigate: abc1234)
# Hypotheses:    4 next steps generated
# Next: /research:run program.md --hypothesis .experiments/retro-<ts>/hypotheses.jsonl
```

______________________________________________________________________

## 🤖 Agents reference

### `research:scientist`

**Role**: AI/ML researcher bridging theory and practice. Reads papers critically, implements methods from descriptions, generates falsifiable hypotheses, designs rigorous experiments, and reasons about whether results support conclusions.

**Model**: `opus`

**When to use directly**:

- Deep analysis of a specific paper — extracting method details, checking reproducibility, finding what the appendix says about hyperparameters
- Generating a falsifiable hypothesis and designing ablations to test it
- Implementing a method from a publication, including non-obvious details (gradient clipping, weight init, EMA decay) that papers often omit
- Reviewing whether a reported result is meaningful — did they report mean ± std over multiple seeds, or just the best run?

**When NOT to use**:

- Broad SOTA landscape survey across multiple methods -> `/research:topic`
- Dataset acquisition, split validation, leakage detection -> `research:data-steward`
- General Python implementation unrelated to a paper -> `foundry:sw-engineer`
- Fetching library docs or web content -> `foundry:web-explorer`

**Example dispatch**:

```text
use scientist to analyze the methodology in this paper and suggest ablations
```

The scientist enforces strict experiment design: every experiment tests exactly one hypothesis, random seed averaging over >= 3 runs, ablation for each component, mean ± std reported (never best run alone). It will flag cherry-picked results, missing confidence intervals, and test set reuse.

______________________________________________________________________

### `research:data-steward`

**Role**: Data lifecycle specialist. Handles everything between "I need this dataset" and "the data feeding the experiment is correct." That includes acquiring datasets from external sources, verifying completeness from paginated APIs, versioning with DVC, auditing train/val/test splits, and detecting data leakage.

**Model**: `sonnet`

**When to use directly**:

- Verifying that your train/val/test splits do not overlap (especially critical for patient-level or session-level grouped data)
- Detecting leakage — normalizer fit on the full dataset before splitting, stochastic augmentation on val/test, SMOTE applied before split
- Acquiring a dataset from an external API with completeness verification (not just the first page)
- Setting up DVC for dataset versioning and provenance tracking
- Auditing a DataLoader for correctness (num_workers seeding, pin_memory, shuffle disabled on val/test)

**When NOT to use**:

- ML hypothesis generation or experiment design -> `research:scientist`
- DataLoader throughput optimization -> `foundry:perf-optimizer`
- URL discovery or web scraping -> `foundry:web-explorer` (data-steward validates what it returns)

**Example dispatch**:

```text
use data-steward to verify train/val split integrity and check for data leakage
```

The data-steward runs six parallel grep patterns against your codebase to surface the top ML data bugs that general code review misses:

| Pattern searched                   | Bug class                                                 |
| ---------------------------------- | --------------------------------------------------------- |
| `fit_transform(`                   | Pre-split normalization leakage                           |
| `Random*` transforms               | Stochastic augmentation on val/test                       |
| `train_test_split(`                | Ungrouped split candidate (checked for missing `groups=`) |
| `patient_id`, `subject_id` columns | Grouped data not split on group ID                        |
| `random_split(`                    | Shared-transform risk (torch Subsets)                     |
| `augment_images(`, `.augment(`     | Pre-split augmentation                                    |

______________________________________________________________________

## 🗺️ Workflow overview

The skills chain naturally. Here is the standard pipeline for a full research session:

```markdown
1. /research:topic "<method>"          <- understand SOTA before coding
2. /research:plan "<goal>"             <- configure experiment, write program.md
3. /research:judge                     <- validate methodology cheaply
4. /research:run program.md            <- run improvement loop with auto-rollback
5. /research:retro                     <- analyze results, generate next hypotheses
6. /research:verify paper.pdf          <- confirm implementation matches the paper
7. /research:fortify                   <- run ablations to find what mattered
```

You do not need all seven steps every time. The most common paths:

**Fast iteration** (you have a clear goal, no paper to verify):

```text
/research:plan "reduce inference latency by 30%"
/research:judge
/research:run program.md
```

**Paper implementation**:

```text
/research:topic "flash attention variants"
/research:plan "reduce training step time by 20%"
/research:judge
/research:run program.md --researcher
/research:verify paper.pdf --strict
/research:retro
```

**Overnight unattended run**:

```text
/research:sweep "increase test coverage to 90%" --codex
```

**Conference submission prep**:

```text
/research:fortify --venue NeurIPS
```

**Resuming after interruption**:

```text
/research:run --resume
```

### How the loop works inside `/research:run`

Each iteration follows this fixed sequence:

1. Build context from git log, JSONL history, and recent diff — written to a file, not accumulated in memory
2. Spawn specialist agent with context, scope files, and program constraints — agent proposes one atomic change
3. Verify that files actually changed (skip no-ops)
4. Commit the change before measuring (enables clean revert)
5. Measure `metric_cmd`
6. Run `guard_cmd` (tests, lint, type check)
7. Keep if metric improved AND guard passes; rework up to 2 times if guard fails; revert otherwise
8. Write diary entry and JSONL record
9. Check for stuck runs, diminishing returns, and early stop

The key design choice is commit before verify. This means every revert is a clean `git revert HEAD --no-edit` that preserves history. You never lose track of what was tried.

______________________________________________________________________

## ⚙️ Configuration

### `program.md` — the research contract

All skills read this file. Write it with `/research:plan`, or by hand. Required sections:

```markdown
## Goal        one paragraph describing what to improve and why
## Metric      command that prints a single float, direction (higher|lower), optional target
## Guard       command that must exit 0 on every kept commit
## Config      max_iterations, agent_strategy, scope_files, compute
## Notes       optional hints for the ideation agent (not parsed by skill, read by agents)
```

<details>
<summary>

Config fields reference

</summary>

Config fields:

| Field             | Values                               | Default  | Notes                                                        |
| ----------------- | ------------------------------------ | -------- | ------------------------------------------------------------ |
| `max_iterations`  | 1–50                                 | 20       | Hard ceiling at 50; never exceeded without explicit override |
| `agent_strategy`  | `auto`, `perf`, `code`, `ml`, `arch` | `auto`   | Auto infers from goal/metric keywords                        |
| `scope_files`     | list of paths/globs                  | required | Ideation agent reads and modifies only these                 |
| `compute`         | `local`, `colab`, `docker`           | `local`  | Routing for metric/guard execution                           |
| `colab_hw`        | `H100`, `L4`, `T4`, `A100`           | none     | Hardware preference for Colab runs                           |
| `sandbox_network` | `none`, `bridge`                     | `none`   | Network isolation in Docker sandbox                          |

</details>

### Colab MCP integration (`--colab`)

Routes metric verification and GPU code testing to a Google Colab runtime via the `colab-mcp` server. Use for ML training metrics, CUDA benchmarks, and any workload that requires a GPU.

Setup (before running `--colab`):

1. Add `"colab-mcp"` to `enabledMcpjsonServers` in `settings.local.json`:
   ```json
   {
     "enabledMcpjsonServers": [
       "colab-mcp"
     ]
   }
   ```
2. Ensure `colab-mcp` is defined in `.mcp.json` under `mcpServers`.
3. Open a Colab notebook with the runtime connected and execute the MCP connection cell.

When `--colab=H100` is specified, the run validates GPU identity via `torch.cuda.get_device_name()` at each iteration and halts if the actual hardware does not match what was requested.

### Artifact layout

All outputs go under `.experiments/` and `.temp/` at your project root. These directories are gitignored.

```text
.experiments/
  state/<run-id>/          <- per-run state, JSONL log, diary (research:run)
  judge-<ts>/              <- methodology review artifacts (research:judge)
  verify-<ts>/             <- scientist audit output (research:verify)
  fortify-<ts>/            <- ablation worktrees and results (research:fortify)
  retro-<ts>/              <- analysis scripts, hypotheses.jsonl (research:retro)
.temp/
  output-research-*.md     <- topic reports
  output-judge-*.md        <- judge reports
  output-optimize-run-*.md <- run final reports
  output-verify-*.md       <- verify reports
  output-fortify-*.md      <- fortify reports
  output-retro-*.md        <- retro reports
```

Directories without `result.jsonl` (judge, verify, fortify, retro run dirs) are exempt from the automated 30-day TTL cleanup. Remove them manually when no longer needed: `rm -rf .experiments/judge-*/`.

______________________________________________________________________

<details>
<summary>

## 🔍 Troubleshooting

</summary>

## 🔍 Troubleshooting

**"No program.md found" when running `/research:judge` or `/research:run`**

Run `/research:plan "<goal>"` first. The plan skill writes `program.md` to your project root by default. If you wrote it manually or saved it elsewhere, pass the path explicitly: `/research:judge path/to/plan.md`.

**"Metric command failed or produced no numeric output" during judge or run**

Your `metric_cmd` must print a single float to stdout. Test it in your terminal first. If the command prints a label alongside the number (e.g., `F1: 0.82`), the skill can parse it. If it prints a table or structured output, you need a wrapper that extracts the number: `python3 eval.py | grep f1 | awk '{print $2}'`.

**"Guard command exited non-zero" in judge**

Your `guard_cmd` is failing on the current codebase before any changes. Fix the underlying issue first. Use `--skip-validation` if you are writing `program.md` on one machine but planning to run on another.

**`--colab` check fails: "Colab MCP not available"**

The Colab MCP server is not enabled. Follow the three-step setup in the Configuration section. The most common miss is forgetting to execute the MCP connection cell in the Colab notebook after connecting the runtime.

**Run stops after 5 consecutive reverts (stuck detection triggered)**

The skill will rotate to a different agent type automatically on the first occurrence, then surface to you if still stuck after two rotations. This is intentional — the skill does not loop indefinitely. Review `.experiments/state/<run-id>/diary.md` to see what has been tried, then consider adjusting `scope_files`, changing `agent_strategy` in `program.md`, or refining the goal.

**"fortify: BLOCKED — no APPROVED judge verdict found"**

Run `/research:judge <program.md>` and get an `APPROVED` verdict before running fortify. This gate exists to prevent ablation studies on methodologically unsound baselines.

**`/research:verify` returns LOW fidelity for a correct implementation**

Some paper claims are unverifiable from code alone — training infrastructure decisions, dataset-specific tuning, details documented only in a supplementary appendix. Unverifiable claims are excluded from the fidelity denominator. If many claims are unverifiable, the score may not be representative. Check the Dimension Summary table in the report for the proportion of unverifiable claims per dimension.

______________________________________________________________________

</details>

## 🙏 Contributing and feedback

This plugin is part of the Borda-AI-Rig project. The skills and agents are in `plugins/research/` in the repository.

The skill files (`plugins/research/skills/*/SKILL.md`) and agent files (`plugins/research/agents/*.md`) are the canonical source of truth — this README must stay in sync with them. Any change to a skill's behavior (flags, NOT-for scope, trigger conditions) requires an update here.

Version bumps follow the project policy: new capability bumps the minor version; fixes, wording, and refactors bump the patch version. Current version: `0.3.0`.

______________________________________________________________________

## 🙏 Acknowledgments

This plugin draws on two open-source research automation projects:

- **fcakyon/phd-skills** — Claude Code plugin built from real PhD mistakes. Its hook-first guardrail philosophy and visual output inspection directly influenced the design of `verify` and `fortify`. The `--venue` reviewer Q&A in fortify is a direct port of its `fortify` command concept.

- **karpathy/autoresearch** — Autonomous overnight ML experiment runner that inverts the human/agent role: agents touch code, humans shape direction via `program.md`. The core loop design of `run` (single metric, atomic commits, wall-clock budgets, `program.md` as the research contract) traces directly to this work.
