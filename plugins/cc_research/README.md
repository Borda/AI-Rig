# 🔬 research — Claude Code Plugin

`research` turns a vague ML improvement idea into a reviewable path: gather literature, write a measurable experiment contract, check the methodology, run bounded iterations, and inspect what changed. Experiment state and reports stay in the project so the next maintainer can see what was tried.

Optional Codemap index-gate guidance ships with research, so loading it does not depend on another plugin's private shared directory. The host-provided active installation takes precedence over other cached versions. Structural queries still require the `codemap-py` plugin; an unavailable CLI or local contract retains the file-read fallback.

> Value at a glance: research connects literature, code, metrics, guards, commits, ablations, and retrospective evidence in one namespaced plugin while leaving datasets, compute, credentials, and scientific judgment with the project owner.

> Current limits at a glance: the plugin does not provide data, GPUs, credentials, or companion plugins; `/research:run --codex` requires the installed and enabled `bridge@borda-ai-rig` plugin; unavailable explicit integrations stop the requested path rather than silently degrading; metric proxies still require human validation.

<details open>
<summary><strong>Contents</strong></summary>

- [What research solves](#-what-research-solves)
- [Install](#-install)
- [Quick start](#-quick-start)
- [Workflow index](#-workflow-index)
  - [`/research:topic`](#researchtopic)
  - [`/research:plan`](#researchplan)
  - [`/research:judge`](#researchjudge)
  - [`/research:run`](#researchrun)
  - [`/research:sweep`](#researchsweep)
  - [`/research:verify`](#researchverify)
  - [`/research:fortify`](#researchfortify)
  - [`/research:retro`](#researchretro)
  - [`/research:kaggle`](#researchkaggle)
  - [`/research:setup`](#researchsetup)
- [Experiment contract](#-experiment-contract)
- [Workflow overview](#-workflow-overview)
- [Agents and optional integrations](#-agents-and-optional-integrations)
- [Hooks, rules, artifacts, and bin tools](#-hooks-rules-artifacts-and-bin-tools)
- [Current boundaries](#-current-boundaries)
- [Troubleshooting](#-troubleshooting)
- [Contributing and maintenance](#-contributing-and-maintenance)
- [Acknowledgments and license](#-acknowledgments-and-license)

</details>

## 🎯 What research solves

Without a contract, ML work often becomes intuition → experiment → unclear result → repeated effort. Baselines drift, proxy metrics go unquestioned, paper details are misimplemented, and GPU hours can be spent before a guard or split audit catches the design flaw.

With research, the evidence path is explicit:

1. `/research:topic` gathers literature and maps a recommendation to the current codebase.
2. `/research:plan` records the goal, metric, guard, scope, strategy, and budget in `program.md`.
3. `/research:judge` checks methodology, scientific rigor, and command executability before expensive work.
4. `/research:run` proposes one scoped change per iteration, commits before measurement, keeps guarded improvements, and reverts regressions.
5. `/research:retro` analyzes significance, dead iterations, suspicious jumps, and next hypotheses.
6. `/research:verify` checks whether an implementation matches a named paper across formulas, hyperparameters, evaluation, notation, and citations.
7. `/research:fortify` isolates components in worktrees to test which changes mattered.

Every step is explicit and reviewable. The chain is not a guarantee of scientific validity, and a user still decides whether a result is worth adopting.

## 📦 Install

Prerequisites are Claude Code with plugin support and Python 3.10+ for setup. `/research:run` also requires Git and starts only from a clean worktree. Datasets, project dependencies, compute, credentials, and optional companion plugins remain user-managed.

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install research@borda-ai-rig
```

After installation, run `/research:setup` once to deliver this plugin's `rules/quality-gates.md` into the user rule directory. Run it again after an upgrade if the rule link is stale. The plugin is independently installable; most optional specialist paths degrade to `general-purpose` with a role prompt when Foundry is absent.

For companion development and release workflows:

```bash
claude plugin install foundry@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install oss@borda-ai-rig
```

`/research:kaggle` requires `foundry:sw-engineer` from the `foundry` plugin and has no fallback. Other skills that request `foundry:*` agents use `general-purpose` with a role description when Foundry is unavailable, so review and implementation quality can be lower.

<details>
<summary><strong>Upgrade and uninstall</strong></summary>

Upgrade from the marketplace and refresh delivered rules:

```bash
claude plugin install research@borda-ai-rig
```

```text
/research:setup
```

Uninstall the plugin with the Claude Code plugin manager:

```bash
claude plugin uninstall research
```

Uninstall does not remove created rule links. Delete only dangling `~/.claude/rules/research-*.md` links after confirming they target this plugin's former cache.

</details>

## ⚡ Quick start

Start with a measurable optimization goal:

```text
/research:plan "improve validation F1 from 0.82 to 0.87"
/research:judge program.md
/research:run program.md
```

`/research:plan` scans the project, proposes metric and guard commands, and writes `program.md`. `/research:judge` checks completeness, methodology, scientific rigor, and (unless skipped) runs the commands once. `/research:run` asks a specialist agent for one scoped change per iteration, measures the configured metric, runs the guard, and keeps or reverts the change.

The metric and guard commands are supplied by you. Research can check that a metric emits a number and a guard exits successfully, but it cannot prove that a proxy metric represents the real goal.

## 🔧 Workflow index

| Need                                        | Command                                                        | Result                                                                                 |
| ------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Compare current AI/ML methods               | `/research:topic "<topic>"`                                    | Literature report and codebase-mapped recommendation in `.reports/research/`           |
| Convert a goal into an experiment contract  | `/research:plan "<goal>"`                                      | `program.md` or a requested output path                                                |
| Check a contract before spending compute    | `/research:judge [<program.md>]`                               | `APPROVED`, `NEEDS-REVISION`, or `BLOCKED` report                                      |
| Run a bounded improvement campaign          | `/research:run <program.md>`                                   | State, iteration log, diary, and report under `.experiments/` and `.reports/research/` |
| Run plan → judge → campaign in one command  | `/research:sweep "<goal>"`                                     | Contract, up to three judge/refinement passes, then a run                              |
| Check an implementation against a paper     | `/research:verify <paper>`                                     | Formula, hyperparameter, evaluation, notation, and citation-chain audit                |
| Test which changes mattered                 | `/research:fortify <run-id>` or `/research:fortify program.md` | Isolated-worktree ablations and component-importance report                            |
| Analyze a completed campaign                | `/research:retro [<run-id>]`                                   | Significance/descriptive statistics, dead-iteration analysis, and next hypotheses      |
| Generate a Kaggle notebook script           | `/research:kaggle <competition-name>`                          | Jupytext `# %%` Python script under `.experiments/kaggle/`                             |
| Install or refresh this plugin's rule links | `/research:setup [--approve]`                                  | Namespaced symlinks under `~/.claude/rules/`                                           |

All ten commands are Claude Code skills under the `research:` namespace. Their `SKILL.md` files are the source of truth for validation rules, output schemas, flags, and edge cases.

### `/research:topic`

<details>
<summary><strong>Literature search, report gating, and plan follow-up</strong></summary>

Searches AI/ML literature, compares methods, recommends an approach for the current codebase, and can turn the recommendation into a phased plan. A broad survey uses the workflow; a named-paper deep dive belongs to `research:scientist`.

```text
/research:topic "efficient fine-tuning methods"
/research:topic "efficient fine-tuning methods" --team
/research:topic plan
/research:topic plan path/to/topic-report.md
```

`--team` runs competing method-family researchers in parallel and costs more context. `--keep "<items>"` preserves named context through compaction. `plan` consumes the most recent or supplied topic report and writes a plan report under `.reports/research/topic-<branch>-<date>.md`.

The `enforce-topic-header.js` hook blocks the follow-up question until the report exists, so the report header can reach the terminal first. Once the report exists, the companion header check may add a reminder when the header was not rendered as the expected table; it does not block the question.

</details>

### `/research:plan`

<details>
<summary><strong>Experiment wizard and profile-first mode</strong></summary>

Builds a `program.md` contract from a measurable goal. A runnable Python file can be supplied instead to run `cProfile` before the wizard asks what to optimize.

```text
/research:plan "reduce inference latency by 30%"
/research:plan src/train.py
/research:plan "reduce inference latency by 30%" out/program.md
```

The wizard scans the codebase, proposes metric and guard commands, chooses a strategy, and asks before overwriting an existing output. It uses `foundry:solution-architect`, `foundry:perf-optimizer`, and `research:scientist` when available, with documented fallbacks.

The output records `Goal`, `Metric`, `Guard`, `Config`, and optional `Notes`. `scope_files` constrain ideation, `max_iterations` is capped at 50, and `agent_strategy` accepts `auto`, `perf`, `code`, `ml`, or `arch`.

</details>

### `/research:judge`

<details>
<summary><strong>Methodology gate and verdict semantics</strong></summary>

Reviews a contract before a campaign. It checks required fields, scope adequacy, metric/goal alignment, methodology, scientific rigor, and command execution.

```text
/research:judge
/research:judge path/to/program.md
/research:judge path/to/program.md --skip-validation
```

The review covers hypothesis clarity, measurement validity, control adequacy, experimental scope, strategy fit, protocol consistency, stopping criteria, reproducibility, falsifiability, Goodhart risk, baseline quality, and missing variance evidence. A dry run checks that the metric emits a numeric value and the guard exits successfully.

| Verdict          | Meaning                                                       |
| ---------------- | ------------------------------------------------------------- |
| `APPROVED`       | Protocol is sufficiently sound for the configured run.        |
| `NEEDS-REVISION` | Fixable gaps remain; read the Required Changes section.       |
| `BLOCKED`        | A fundamental design or execution condition must be repaired. |

A `NEEDS-REVISION` verdict names the gap and the concrete repair:

```text
Verdict: NEEDS-REVISION
Finding: target not set — the campaign will run to max_iterations
Finding: measurement validity — metric_cmd measures a proxy, not the stated metric
Required changes: (1) add `target:` under ## Metric  (2) replace metric_cmd
```

`--skip-validation` is for cross-machine planning. It leaves metric and guard executability unverified and therefore prevents an `APPROVED` verdict. Reports are written to `.reports/research/judge-<branch>-<date>.md`.

</details>

### `/research:run`

<details>
<summary><strong>Bounded metric-improvement loop, flags, and state</strong></summary>

Runs the core loop. Each iteration builds bounded context, proposes one scoped change, verifies that files changed, commits before measuring, runs the metric and guard, keeps a guarded improvement, or reverts the change. A campaign defaults to 20 iterations and is capped at 50. Five consecutive discards trigger strategy escalation and then stop rather than looping blindly.

```text
/research:run program.md
/research:run program.md "focus on data augmentation"
/research:run program.md --resume
/research:run program.md --team --researcher --journal
```

What the loop prints:

```text
Baseline: f1_score = 0.820
[→ Iter 1/20 — best so far: 0.820 (Δ0.0% vs baseline)]
[✓ Iter 1/20 — kept · metric=0.831 (Δ1.3%) · agent=research:scientist]
[✓ Iter 2/20 — reverted · metric=0.818 (Δ-0.2%) · agent=codex]
```

Supported flags are `--resume`, `--team`, `--compute=local|colab|docker`, `--colab[=H100|L4|T4|A100]`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis <path>`, `--scientist`, `--codemap`, `--no-codemap`, and `--keep "<items>"`.

| Strategy | Agent                        | Typical goal signals              |
| -------- | ---------------------------- | --------------------------------- |
| `perf`   | `foundry:perf-optimizer`     | latency, throughput, memory       |
| `code`   | `foundry:sw-engineer`        | coverage, complexity, coupling    |
| `ml`     | `research:scientist`         | accuracy, loss, F1, AUC           |
| `arch`   | `foundry:solution-architect` | modularity, cohesion              |
| `auto`   | inferred from keywords       | default when no explicit strategy |

The default execution is local. `--compute=docker` requires a reachable Docker daemon and routes metric/guard verification through the sandbox. `--colab` requires the `colab-mcp` runtime tool and a connected runtime; an optional hardware value is checked against the requested GPU. `--codex` requires `bridge@borda-ai-rig` installed and enabled plus a working `claude` CLI. Explicit integrations stop when unavailable rather than silently degrading.

`--researcher` generates hypotheses with `research:scientist`; `--architect` adds architectural hypotheses from `foundry:solution-architect`; both can run together, and every oracle annotates its own hypotheses' feasibility (no separate annotation pass). `--hypothesis <path>` consumes a pre-built JSONL queue, and `--journal` requires one of the hypothesis flags and records every outcome. `--team` uses the team mode for parallel hypothesis exploration. `--keep "<items>"` preserves named context through compaction.

Keep logic is metric-direction aware: an improved metric with a passing guard is kept, a regression is reverted, a guard failure may be reworked up to the configured limit, and a large low-value change can be discarded under the simplicity guard. Every revert is a `git revert`, preserving history rather than deleting evidence.

Run state lives under `.experiments/state/<run-id>/` with `state.json`, `experiments.jsonl`, diary, context, progress, scripts, and resume data. Hypothesis queues, checkpoints, and optional journals live under `.experiments/<run-id>/`. The final report is `.reports/research/run-<branch>-<date>.md`.

The run starts from a clean Git worktree and can create commits or reverts. Review scope files, commands, diffs, and the final report before accepting a campaign result.

Illustrative terminal shape (values and agent names are examples, not a benchmark):

```text
Baseline: f1_score = 0.820
[-> Iter 1/20 — best so far: 0.820]
[✓ Iter 1/20 — kept · metric=0.831 · guard=passed]
[✓ Iter 2/20 — reverted · metric=0.818 · guard=passed]
```

</details>

### `/research:sweep`

<details>
<summary><strong>Plan → judge/refine → run pipeline</strong></summary>

Runs the non-interactive plan → judge/refine → run pipeline from a goal. It accepts the run's compute, team, Codex, researcher, architect, journal, and hypothesis options, plus `--skip-validation`, `--out <path>`, and `--keep "<items>"`. It asks before overwriting an existing contract.

```text
/research:sweep "increase validation F1 to 0.87"
/research:sweep "reduce test runtime" --out .experiments/program.md
/research:sweep "improve recall" --researcher --journal
```

Judge refinement runs at most three times, applying Required Changes between passes. `APPROVED` starts the run, `BLOCKED` stops with critical findings, and unresolved `NEEDS-REVISION` remains a user decision. Sweep-specific options are `--skip-validation`, `--out <path>`, and `--keep "<items>"`; use the `/research:run` section for forwarded run flags.

Use separate `/research:plan` and `/research:judge` when you need to inspect or tune the contract before spending compute.

</details>

### `/research:verify`

<details>
<summary><strong>Paper-to-code fidelity audit</strong></summary>

Audits whether code matches a named paper; it does not judge whether the paper's claims are valid. Input may be a PDF path, arXiv/PDF URL, or pasted paper text.

```text
/research:verify paper.pdf
/research:verify paper.pdf --scope "src/model/**/*.py"
/research:verify paper.pdf --program program.md --strict
/research:verify paper.pdf --dim F,H --no-codemap
```

The five dimensions are formula (`F`), hyperparameter (`H`), evaluation (`E`), notation (`N`), and citation chain (`C`). Fidelity is `(MATCH + 0.5 * PARTIAL) / total_verified_claims`; unverifiable claims are excluded from the denominator and documented. `--strict` stops on HIGH-severity formula or evaluation mismatches. `--codemap` requires a usable index, while `--no-codemap` opts out; missing or stale structural context otherwise degrades the audit with an explicit gap.

Reports are written to `.reports/research/verify-<branch>-<date>.md`, with the detailed scientist audit retained under `.experiments/verify-<timestamp>/`.

Illustrative finding shape:

```text
Fidelity: MODERATE (0.74)
BREAKING — HIGH severity mismatch in F (formula)
Fix: src/model.py:42 — reduction differs from the paper specification
```

</details>

### `/research:fortify`

<details>
<summary><strong>Isolated ablations and optional reviewer Q&A</strong></summary>

Runs one-component-at-a-time ablations after a completed run and an `APPROVED` judge report for the same program. It identifies candidates from the diff and diary, creates an isolated Git worktree for each variant, runs metric and guard commands locally, ranks importance, and can generate venue-specific reviewer Q&A.

```text
/research:fortify
/research:fortify <run-id>
/research:fortify program.md --max-ablations 5 --skip-run
/research:fortify <run-id> --venue NeurIPS
```

Supported options are `--venue CVPR|NeurIPS|ICML|workshop`, `--max-ablations <N>`, `--skip-run`, and `--keep "<items>"`. `--skip-run` identifies candidates without executing ablations. `--compute` and `--colab` are not implemented for fortify; metric and guard commands run locally in each worktree.

| Class         | Metric loss after component removal |
| ------------- | ----------------------------------- |
| `CRITICAL`    | More than 50% of the full metric    |
| `SIGNIFICANT` | 10–50% of the full metric           |
| `MARGINAL`    | Less than 10% of the full metric    |

The full variant is a sanity check and should reproduce the best metric within 2%; divergence is reported as possible nondeterminism or environment drift. The main worktree is never modified by ablation execution. Reports are written to `.reports/research/fortify-<branch>-<date>.md`.

Illustrative report shape:

```text
Components: 4 identified, 4 ablations completed
Top: learning-rate-warmup (importance: 62.3%, CRITICAL)
Reviewer Q&A: generated only when --venue was supplied
```

</details>

### `/research:retro`

<details>
<summary><strong>Retrospective statistics and next-hypothesis queue</strong></summary>

Reads a completed run's JSONL and diary without changing code or experiment state. It computes a one-sided one-sample Wilcoxon comparison of kept iterations against the baseline when at least six kept iterations and `scipy` are available; otherwise it reports descriptive statistics.

```text
/research:retro
/research:retro <run-id>
/research:retro <run-id> --compare <run-id-2>
/research:retro <run-id> --threshold 0.005 --alpha 0.01
```

The report covers direction-aware significance, dead plateaus and churn, suspicious jumps above two standard deviations, strategy effectiveness, failure patterns, diminishing returns, and three to five next hypotheses compatible with `/research:run --hypothesis`. `--compare` requires matching program and metric. Reports are written to `.reports/research/retro-<branch>-<date>.md`.

Illustrative report shape:

```text
Significance: p=0.031 (interpret with the recorded sample and assumptions)
Dead iterations: 4/20
Suspicious jumps: 1 (investigate)
Next: /research:run program.md --hypothesis .experiments/retro-<ts>/hypotheses.jsonl
```

</details>

### `/research:kaggle`

<details>
<summary><strong>Grounded Jupytext notebook generation</strong></summary>

Generates a Kaggle competition notebook as a Jupytext `# %%` Python script. It grounds schema and submission format through the authenticated Kaggle CLI, then produces an EDA → baseline → training → inference pipeline where the selected mode requires it. It requires `foundry:sw-engineer` and stops when that required agent is unavailable.

```text
/research:kaggle competition-name
/research:kaggle competition-name "problem description"
/research:kaggle competition-name --type classification
/research:kaggle competition-name --eda-only
/research:kaggle competition-name --inference-only
/research:kaggle competition-name --resume .experiments/kaggle/existing.py
```

Supported options are `--type classification|regression|segmentation|detection|tabular`, `--eda-only`, `--inference-only`, `--offline-setup`, `--resume <path>`, and `--keep "<items>"`. `--eda-only` is always online and omits training; `--inference-only` is offline, uses the frozen-package pattern, and writes an `-inference.py` suffix; `--offline-setup` adds frozen package setup and is ignored for EDA-only mode.

Generated notebooks use small single-purpose cells, a why for each meaningful cell, visual EDA, leakage-safe evaluation, PTL plus torchmetrics for DNN training, and separate checkpoint load/inference. Credentials are not written to the notebook. Output is `.experiments/kaggle/<competition-name>.py` or the inference suffix.

</details>

### `/research:setup`

<details>
<summary><strong>Rule delivery, ownership checks, and conflicts</strong></summary>

Delivers this plugin's `rules/*.md` into Claude's flat user-rule namespace with a `research-` prefix, avoiding collisions with other plugins' `quality-gates.md` files.

```text
/research:setup
/research:setup --approve
```

The default mode previews changes and asks before replacing a conflicting destination. `--approve` is the non-interactive mode used by synchronization. Only links whose targets resolve under this plugin's current cache or install-cache lineage are replaced or removed; a real file, another marketplace, a source checkout, or a dotfiles tree remains a conflict unless explicitly approved.

Each rule becomes `~/.claude/rules/research-<source-name>.md`. Claude Code does not run cleanup on uninstall, so dangling links must be removed manually after confirming ownership. An upgrade refreshes links from the new cache lineage and removes links for rules no longer shipped.

</details>

## 🧾 Experiment contract

<details open>
<summary><strong><code>program.md</code> fields and validation rules</strong></summary>

`program.md` is the boundary between planning and execution. Write it with `/research:plan` or by hand:

````markdown
## Goal
One measurable improvement target.

## Metric
```yaml
command: python eval.py
direction: higher
target: 0.87
```

## Guard
```yaml
command: python -m pytest tests/
```

## Config
```yaml
max_iterations: 20
agent_strategy: auto
scope_files:
  - src/
compute: local
```
````

| Field             | Values                               | Default  | Contract                                                     |
| ----------------- | ------------------------------------ | -------- | ------------------------------------------------------------ |
| `max_iterations`  | 1–50                                 | 20       | Hard ceiling; set deliberately before a campaign.            |
| `agent_strategy`  | `auto`, `perf`, `code`, `ml`, `arch` | `auto`   | Auto infers from goal/metric keywords and warns on fallback. |
| `scope_files`     | paths or globs                       | required | Bounds what ideation may inspect and modify.                 |
| `compute`         | `local`, `colab`, `docker`           | `local`  | Routes metric and guard execution.                           |
| `colab_hw`        | `H100`, `L4`, `T4`, `A100`           | none     | Hardware preference for Colab runs.                          |
| `sandbox_network` | `none`, `bridge`                     | `none`   | Network isolation for Docker sandbox execution.              |

The metric command must emit a numeric value, and the guard must exit successfully for a kept iteration. A target is recommended because running only to the iteration ceiling can waste compute. Scope files, direction, baseline, controls, and stopping criteria should be reviewable before `/research:run`.

</details>

## 📊 Workflow outputs

<details open>
<summary><strong>Reports, state, and handoff files</strong></summary>

| Workflow  | Primary report                                 | Supporting state or output                                                                |
| --------- | ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `topic`   | `.reports/research/topic-<branch>-<date>.md`   | Optional topic plan report and gated follow-up context.                                   |
| `judge`   | `.reports/research/judge-<branch>-<date>.md`   | Methodology and scientific review evidence under `.experiments/judge-<timestamp>/`.       |
| `run`     | `.reports/research/run-<branch>-<date>.md`     | `.experiments/state/<run-id>/` plus hypothesis artifacts under `.experiments/<run-id>/`.  |
| `sweep`   | Delegated `judge` and `run` reports            | Generated contract at the requested `--out` path or project root.                         |
| `verify`  | `.reports/research/verify-<branch>-<date>.md`  | Scientist audit under `.experiments/verify-<timestamp>/`.                                 |
| `fortify` | `.reports/research/fortify-<branch>-<date>.md` | Candidate list, worktrees, results, and dropped variants under `.experiments/fortify-*/`. |
| `retro`   | `.reports/research/retro-<branch>-<date>.md`   | Analysis state and compatible `hypotheses.jsonl` under `.experiments/retro-*/`.           |
| `kaggle`  | N/A                                            | `.experiments/kaggle/<competition-name>.py` and optional downloaded data.                 |
| `setup`   | Terminal summary                               | Namespaced links under `~/.claude/rules/`.                                                |

Reports should disclose the metric, baseline, commands, changed scope, gate results, confidence, unresolved limitations, and a concrete next action. Generated state is project-rooted and intended to be inspectable or cleaned according to the owning workflow's contract.

</details>

<details>
<summary><strong>Colab MCP setup</strong></summary>

`--colab` routes metric verification and GPU testing to a connected Colab runtime through `colab-mcp`. Before invoking it:

1. Enable `colab-mcp` in `settings.local.json`.
2. Ensure `colab-mcp` is defined in `.mcp.json` under `mcpServers`.
3. Open a Colab notebook with a connected runtime and execute its MCP connection cell.

`--colab=H100` requests a specific hardware class; the run checks the observed GPU and reports a mismatch rather than silently treating another GPU as equivalent. `--colab` and `--compute=docker` are mutually exclusive.

</details>

## 🗺️ Workflow overview

<details open>
<summary><strong>Common paths and run internals</strong></summary>

Standard evidence path:

```text
1. /research:topic "<method>"       understand current methods before coding
2. /research:plan "<goal>"          write program.md
3. /research:judge                   validate methodology cheaply
4. /research:run program.md          run bounded improvement loop
5. /research:retro                   analyze results and next hypotheses
6. /research:verify paper.pdf        confirm paper-to-code fidelity
7. /research:fortify                 isolate component importance
```

Fast iteration:

```text
/research:plan "reduce inference latency by 30%"
/research:judge
/research:run program.md
```

Paper implementation:

```text
/research:topic "flash attention variants"
/research:plan "reduce training step time by 20%"
/research:judge
/research:run program.md --researcher
/research:verify paper.pdf --strict
/research:retro
```

Conference preparation:

```text
/research:fortify --venue NeurIPS
```

Resumption after interruption:

```text
/research:run --resume
```

Inside `/research:run`, the fixed sequence is: build context from Git and JSONL history; spawn a bounded specialist; verify files changed; commit before measuring; run the metric; run the guard; keep, rework, or revert; write the diary and JSONL record; then check stuck runs, diminishing returns, and early-stop conditions. The commit-before-measure choice makes every rollback an auditable `git revert`.

The workflow is user-invoked and bounded. Chaining `/research:retro`, `/research:run --hypothesis`, or `/research:fortify` remains an explicit next action; no unattended campaign is promised.

</details>

## 🔗 Agents and optional integrations

The plugin ships exactly two manually invocable agents:

| Agent                   | Purpose                                                                                         | Not for                                                                      |
| ----------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `research:scientist`    | Named-paper analysis, falsifiable hypotheses, experiment design, and result interpretation.     | Broad SOTA survey, data acquisition, or general Python.                      |
| `research:data-steward` | Dataset acquisition, provenance, split integrity, leakage checks, and data-pipeline validation. | ML hypothesis design, throughput optimization, or undocumented web scraping. |

`research:scientist` expects a named paper, author, or arXiv anchor for paper implementation. It enforces one hypothesis per experiment, seed averaging, baselines, ablations, and mean ± standard deviation rather than best-run reporting. `research:data-steward` audits pagination completeness, schema, boundaries, deduplication, split isolation, stateful transforms, augmentations, and DataLoader configuration; it delegates unknown URL discovery to `foundry:web-explorer` when Foundry is available.

Optional integrations are capability-gated:

- `foundry` (requires `foundry` plugin) supplies software, performance, architecture, and web-research agents. Most skills fall back to `general-purpose` role prompts when it is absent; Kaggle stops.
- `codemap-py` (requires `codemap-py` plugin) supplies structural context to `run` and `verify` when enabled and indexed. `--no-codemap` opts out; `--codemap` makes a usable index mandatory.
- `bridge@borda-ai-rig` enables `/research:run --codex`; it is never silently substituted when requested.
- A connected `colab-mcp` runtime enables `--colab` for `run` and `sweep`.
- `scipy` enables Wilcoxon significance in `retro`; without it, the report uses descriptive statistics.
- The authenticated Kaggle CLI is required for online competition grounding.

<details>
<summary><strong>Agent operating boundaries</strong></summary>

`research:scientist` is for a named paper, publication-backed method, falsifiable hypothesis, or experiment design. It separates paper claims from evidence, checks baselines and variance, identifies one central idea, audits attribution and contribution claims, plans one-variable-at-a-time experiments, estimates compute, and interprets results as confirmed, refuted, or partially supported. It should report mean ± standard deviation over at least three seeds when stochastic results are being compared and should flag cherry-picked results, missing confidence intervals, test-set reuse, and leakage concerns.

`research:data-steward` is for acquisition, provenance, DVC or version tracking, split integrity, leakage detection, schema validation, and DataLoader configuration. Its checklist includes pagination count/schema/boundary/dedup verification; mutually exclusive or group-aware splits; train-only fitting for stateful transforms; train-only augmentation and oversampling; temporal window direction; NaN/Inf, shape, dtype, and range checks; and `shuffle=False` for validation/test loaders. It delegates unknown URL discovery or scraping to `foundry:web-explorer` and validates the returned data itself.

Useful data-steward search patterns include `fit_transform(` for pre-split normalization, `Random*` transforms for validation contamination, `train_test_split(` without group awareness, `patient_id` or `subject_id` split gaps, `random_split(` shared-transform risks, and augmentation calls before the split. These are investigation prompts, not automatic findings; the agent must confirm the surrounding data flow.

Scientist handoffs should state the paper or method, core idea, actual contribution, mechanics, evidence, limitations, relevance, falsifiable prediction, variables, controls, success criterion, ablations, compute estimate, and expected outcome. A paper summary is not a benchmark claim unless the source and protocol are retained.

</details>

## 📐 Hooks, rules, artifacts, and bin tools

<details open>
<summary><strong>Registered hooks and shared helper behavior</strong></summary>

Hooks register from `hooks/hooks.json` when the plugin is enabled; no settings edit is needed for registration:

| Hook                      | Event                                                                         | Behavior                                                                                                                                                                                                                                                                                   |
| ------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `agent-router.js`         | `PreToolUse(Agent)`                                                           | Exact agent route, semantic fallback, then `general-purpose`.                                                                                                                                                                                                                              |
| `sentinel-read-allow.js`  | (decision module, not registered)                                             | Allows only pre-canned read-only temporary-state idioms; other commands use normal permission checks. Whole-line `# …` comments inside a block are skipped rather than blocking it, and `..` is matched as a path component so ellipsis and version ranges are not mistaken for traversal. |
| `blueprint-allow.js`      | (decision module, not registered)                                             | Exact-matches the normalized command against this plugin's committed `blueprint-manifest.json`; any deviation falls through to a normal prompt.                                                                                                                                            |
| `allow-dispatch.js`       | `PreToolUse(Bash)`                                                            | The only registered Bash auto-allow hook. Calls the two decision modules above in rank order, emits the first allow, and appends one audit record naming the effective verdict and both modules' own verdicts. Exits 0 on every path.                                                      |
| `audit-close.js`          | `PostToolUse(Bash)`, `PostToolUseFailure(Bash)`, `SessionStart`, `SessionEnd` | Appends one record observing that a Bash call completed, and one per session boundary. Writes nothing to stdout on any event.                                                                                                                                                              |
| `write-guard.js`          | `PreToolUse(Edit, Write, NotebookEdit)`                                       | Grants nothing; forces confirmation on writes to CI definitions, agent instructions, permission config, lockfiles and release metadata. Source and tests stay unprotected.                                                                                                                 |
| `enforce-topic-header.js` | `PreToolUse(AskUserQuestion)`                                                 | Gates topic follow-up until its report exists; header-table checking remains additive and non-blocking.                                                                                                                                                                                    |

`lib/audit-log.js` and `report-header-table.js` are shared hook modules rather than registered hooks. `lib/audit-log.js` is the append-only record writer behind the two audit hooks: it holds no lock, marker or state directory, has no code path that deletes or rewrites a file, and never throws, so a logging failure cannot change a permission decision. `RIG_AUDIT=0` disables writing only. `report-header-table.js` checks whether a topic report header was rendered as the required table or documented fallback line.

#### Auto-allow audit log

`allow-dispatch.js` and `audit-close.js` append one JSON line each per Bash call to `~/.claude/logs/audit/`: one file per session (`s-<key>.jsonl`, the key being a hash of the session id) plus a shared `_no-session.jsonl` for calls that arrive without one. A record names the deciding plugin and hook, the session and tool-call ids, the working directory, the effective decision with its lane and provenance, and both decision modules' own verdicts. Command text is never written — only a digest, and for a provenance allow the manifest `src` it matched. Note the limit honestly: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`, so the guarantee is about this file rather than about the log directory.

With several plugins installed each writes its own rows and nothing coordinates them, so one completed call yields one row per plugin at each end. That is corroboration, not duplication.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" verify
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" prune --older-than 30 --dry-run
```

`verify` exits 1 only when a record's own hash fails to verify; torn framing from concurrent appends and every classification are warnings. `record_hash` detects a record that changed after it was written — it is not tamper-evidence, because the same user owns the log, the hooks and the checker. Read the classifications as observations rather than proof: a call with a decision row and no completion row is reported as `completion-unobserved` when the session later ended and `in-flight-or-hard-kill` when it did not, and neither distinguishes a refusal at the prompt from a deny rule elsewhere or a closer that died. Equally, that a tool ran never establishes that a human approved it. `prune` is the only component that deletes a log file, is never run by a hook, and never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id. Nothing schedules `prune`. `RIG_AUDIT=0` disables audit writing without changing any permission decision.

Records follow the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), an active individual-submission Internet-Draft that is not endorsed by the IETF and has no standing in its standards process. Deliberate deviations: `outcome` adds `pending`, because a decision that has not executed yet has no outcome in the draft's vocabulary; `trust_level` is `plugin`/`unknown` rather than the draft's `L0`–`L4`, because what is observed is which component proposed an allow, not an authentication level; `agent_id` is `<plugin>/<hook>` rather than a URI; `session_id` is the host's id verbatim rather than a UUID; and `parent_record_id` and `prev_hash` are always null, because this log is not chained — lineage is derived on read instead.

`rules/quality-gates.md` requires confidence blocks on analysis output and defines report-file and terminal-header formatting. `/research:setup` delivers it as a namespaced user rule instead of editing global configuration.

`.claude-plugin/permissions-allow.json` lists the tool calls the skills expect to be pre-approved. `.claude-plugin/permissions-deny.json` is its counterpart — the operations that must stay denied no matter how broad the allow list becomes: destructive shell and git commands (`rm -rf`, `sudo`, `ssh`, `chmod 777`, branch and tag deletion, force-push, `claude --dangerously-skip-permissions`) plus every public-GitHub write (`gh issue`/`pr`/`release`/`gist` create, edit, merge, delete, and `gh api` with `POST`, `PATCH`, `PUT` or `DELETE`). Both files are merged into `~/.claude/settings.json` by `/research:setup` (Step 5) — additive and idempotent, nothing is ever removed. Deny entries are prefix matches, so they stop the documented command forms rather than every possible flag ordering.

Generated files remain at the project root:

```text
.experiments/state/<run-id>/              run state, JSONL log, diary, context, scripts
.experiments/<run-id>/                    hypothesis queue, checkpoint, optional journal
.experiments/{judge,verify,fortify,retro}-* intermediate analysis artifacts
.experiments/kaggle/                       generated Jupytext scripts and downloaded data
.reports/research/                         topic, judge, run, verify, fortify, retro reports
.temp/state/                               short-lived cross-phase skill contracts
```

`bin/verify_blueprint_audit.py` reads and prunes the auto-allow audit log described under Hooks. `bin/health_monitor_start.py` creates cross-platform health-monitor sentinel coordinates; current skills describe the monitoring sequence in workflow prose rather than exposing this helper as a standalone user command. That consolidation is possible future cleanup, not a current requirement.

The packaged bin inventory is:

`check_output_within_root.py`, `codemap-flag.py`, `codemap_resolve.py`, `compute_effect_size.py`, `detect-complexity.py`, `docker_sandbox_run.py`, `extract-keep-flag.py`, `find_judge_verdict.py`, `find_run_id.py`, `fortify_next_variant.py`, `gate-on-sentinel.py`, `git_slugs.sh`, `heal_git_artifacts.py`, `health_monitor_start.py`, `load-agent-reference.py`, `make_run_dir.py`, `parse-skill-flags.py`, `parse_kaggle_args.py`, `read_state_field.py`, `require-vars.py`, `resolve-anti-overwrite-path.py`, `resolve-quality-gates.sh`, `resolve_shared.py`, `retro_analyze.py`, `sync_rules.py`, `verify_patient_split.py`, and `write_skill_contract.py`.

</details>

## 🧭 Current boundaries

These are current constraints, not promises about future releases:

- `/research:run` operates on the current Git worktree and can create commits or reverts. Review `scope_files`, metric commands, guard commands, and the resulting diff before accepting a campaign result.
- Research does not provide datasets, GPU capacity, Kaggle credentials, Colab runtimes, Codex, or specialist companion plugins; those remain user-managed prerequisites.
- `fortify` executes ablations locally. Worktree isolation protects the main worktree, but it does not make arbitrary metric or guard commands safe.
- Statistical conclusions are limited by run history, baseline design, data quality, and independence assumptions. `retro` reports what it computed; it does not establish causal validity.
- A missing or stale Codemap index reduces structural context, and an unavailable optional specialist falls back or stops according to the specific skill contract.
- `--colab`, `--compute=docker`, `--codex`, `--researcher`, and `--architect` are explicit requirements when requested; the workflow reports missing capability instead of pretending it ran.
- Rule links can outlive plugin uninstall because Claude Code has no uninstall cleanup hook; ownership must be checked before manual removal.

Potential future work includes richer native agent selection, broader compute backends for fortify, and more shared health-monitor orchestration. None is required for the current plugin contract.

## 🔍 Troubleshooting

<details open>
<summary><strong>Common failure messages and recovery</strong></summary>

**`No program.md found`**: run `/research:plan "<measurable goal>"` or pass an existing contract path to `/research:judge` and `/research:run`.

**`APPROVED` is unavailable with `--skip-validation`**: this is intentional because metric and guard executability was not tested on the current machine.

**A topic question is blocked by the report gate**: ensure `.reports/research/topic-<branch>-<date>.md` exists and its `---` header is rendered before the follow-up question. The gate is scoped to an active topic run and expires after its resolution window.

**Metric command failed or emitted no number**: run `metric_cmd` directly and make it print one parseable float, optionally with a label such as `F1: 0.82`. Wrap table or structured output in a command that extracts one number.

**Guard command exited non-zero during judge**: fix the underlying current-code failure first, or use `--skip-validation` only when planning on one machine for execution on another; the resulting judge cannot be `APPROVED`.

**`--colab` stops**: enable `colab-mcp`, connect a Colab runtime, execute its connection cell, and use only one of `--colab` or `--compute=docker`.

**`--compute=docker` stops**: verify that a Docker daemon is reachable and that the metric/guard commands are valid inside the configured image.

**`--codex` stops**: put the `claude` executable on `PATH` and install and enable `bridge@borda-ai-rig`; the requested co-pilot is not silently replaced.

**Run stops after five consecutive discards**: inspect `.experiments/state/<run-id>/diary.md`, then adjust the goal, scope, strategy, or hypothesis queue before retrying. The stop prevents blind looping.

**`fortify: BLOCKED`**: complete `/research:run` and obtain an `APPROVED` `/research:judge` report for the same program before running ablations.

**`/research:kaggle` stops before generation**: install Foundry so `foundry:sw-engineer` is available and authenticate the Kaggle CLI for online grounding. Use `--inference-only` only with a checkpoint path available to the generated script.

**`retro` reports no significance value**: install `scipy`, or interpret the descriptive statistics when fewer than six kept iterations are available.

**Rule setup reports a conflict**: inspect the destination target. `/research:setup` replaces only links proven to belong to this plugin or its cache lineage; use `--approve` only after reviewing a real conflicting target.

</details>

## 🙏 Contributing and maintenance

The canonical sources are the ten `skills/*/SKILL.md` files, the two `agents/*.md` files, `rules/*.md`, registered hooks, sidecar references, and `bin/*`. Keep this README synchronized when a public skill, flag, trigger, prerequisite, output path, hook, or boundary changes.

The plugin version is currently `0.19.0`. This bridge integration is a designed capability change and therefore uses a minor version bump.

When editing a skill, update its README entry, flags, NOT-for boundaries, output paths, fallback behavior, and relevant troubleshooting guidance. Verify that references loaded from `skills/_shared/`, `skills/*/modes/`, agent sidecars, or `bin/` remain installed-path safe and do not assume a source checkout.

The plugin's tests cover path safety, Codemap resolution, effect size, Docker sandboxing, hook contracts, run directories, rule resolution, shared-file resolution, retro analysis, and patient split validation. Run focused tests while editing and the full `plugins/cc_research` suite before release.

## 🙏 Acknowledgments and license

Research automation design draws on [fcakyon/phd-skills](https://github.com/fcakyon/claude-skills) for hook-first guardrails and [karpathy/autoresearch](https://github.com/karpathy/autoresearch) for metric-driven, commit-preserving iteration contracts. Those influences inform the design; this plugin's current behavior is defined by its shipped skills, agents, hooks, rules, and tests.

Research is licensed under Apache-2.0.
