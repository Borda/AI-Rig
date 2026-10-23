---
name: optimize
description: Optimize a measurable metric with bounded iterations, resumable numeric campaigns, isolated ablations, and descriptive retrospectives.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md). Waits, plan updates, and test runs follow [Agent Waits](../../shared/native-skill-contract.md#agent-waits), [Plan Updates](../../shared/native-skill-contract.md#plan-updates), and [Sandboxed Test Runs](../../shared/native-skill-contract.md#sandboxed-test-runs).

# Optimize

> Task-size routing follows [Proportional Execution](../../shared/native-skill-contract.md#proportional-execution); this skill's specialized lifecycle and acceptance checks remain authoritative.

Metric-driven optimization with explicit guards, rollback criteria, experiment log.

## Input Schema

```json
{
  "goal": "required measurable improvement objective",
  "mode": "single|campaign|resume|ablation|retrospective",
  "checkpoint": "optional boolean for a resumable numeric campaign; default false",
  "source_run": "required existing managed run directory for resume, ablation, or retrospective",
  "variants": "ablation only: bounded named component-removal patches and their rationale",
  "metric_cmd": "required for single and campaign; for resume, ablation and retrospective recovered from source_run: command that emits or validates the target metric",
  "metric_direction": "higher|lower",
  "guard_cmd": "required for single and campaign; recovered from source_run otherwise: command that must continue to pass",
  "max_iterations": "optional integer, default 1",
  "min_delta": "optional practical significance threshold",
  "scope_files": [
    "paths the optimization may edit"
  ],
  "done_when": "metric improves without guard regression"
}
```

For `resume`, recover goal, commands, scope, direction and total budget from `source_run`; supplied replacements must match the frozen configuration. `ablation` and `retrospective` derive measurement configuration from that source rather than requiring duplicate inputs. Existing `single` and non-checkpointed `campaign` accept numeric or pass/fail metrics unchanged. The checkpoint helper and new numerical analyses require finite numeric values; explain unsupported historical/pass/fail input instead of inventing a metric or migrating a log silently.

## Mode Dispatch

- `single` or `campaign` without `checkpoint`: follow the existing workflow below.
- `campaign` with `checkpoint`, or `resume`: read [Managed campaigns](campaign.md). It wraps steps 02–07 with durable checkpoints, then returns to steps 08–09. Resume keeps the original run directory; never initialize or truncate it again.
- `ablation`: read [Component ablations](ablation.md), create a separate result run in step 01, then complete steps 08–09 with the mode's retained evidence.
- `retrospective`: read [Experiment retrospective](retrospective.md), create a separate result run in step 01, then complete steps 08–09. No source mutation or measurement execution is required.

The local [campaign.py](campaign.py) helper performs deterministic state/receipt checks and summaries, not metric execution or automatic source restoration. Invoke it through `PLUGIN_ROOT/bin/python` (native Windows: `PLUGIN_ROOT/bin/python.cmd`) and inspect `--help`. The agent owns actual command execution, measurement provenance, safe rollback, and worktree orchestration. A digest check establishes consistency, not authenticity of execution.

## Workflow

### 01: Create run directory

Run `create_run.py --skill optimize` per `../../shared/helper-cli-contract.md`.

### 02: Validate metric and guard commands

Require:

- Repeatable `metric_cmd` producing comparable value or pass/fail.
- Known `metric_direction`.
- `guard_cmd` fails on unacceptable regressions.
- Bounded `scope_files`.
- Explicit, bounded `max_iterations` for `campaign`.
- Protect files/scripts used by `metric_cmd`/`guard_cmd` unless user explicitly scopes them and accepts measurement-integrity risk.

Dry-run both before edit:

Execute configured `metric_cmd` and `guard_cmd` separately with host-native command runner. Write complete combined output to `<run-directory>/metric-baseline.txt` and `<run-directory>/guard-baseline.txt`; retain both exit codes. If a missing declared project dependency prevents a command from running, apply [Project Dependency Preparation](../../shared/native-skill-contract.md#project-dependency-preparation), preserve the earlier attempts separately, and rerun both baseline commands in the resulting environment before accepting the baseline or editing source. Stop before editing while either baseline remains unavailable.

Dependency preparation changes a measurement input: invalidate earlier measurements from the previous environment, including when preparation occurs during an iteration or resume. Recapture the baseline on its original source under the owning workflow's source-state and measurement-integrity rules before comparing results. If equivalent recapture is unavailable, retain an inconclusive result with no improvement claim; never attribute an environment change to a source optimization.

### 03: Record baseline and hypothesis

Write `<run-directory>/hypothesis.md`:

- metric to improve
- expected mechanism
- files allowed to change
- guard risk
- rollback condition

For `campaign`, noisy metrics, GPU/ML performance, or correctness-sensitive code, read and apply `../../shared/specialist-orchestration.md`; otherwise do not load it. Write `<run-directory>/specialist-optimization-plan.md` with narrow context packs for:

- `squeezer`: profiling mechanism, bottleneck hypothesis, measurement plan.
- `qa-specialist`: guard coverage and regression risk.
- `data-steward`: data pipeline or reproducibility impact.
- `scientist`: metric validity, ablation design, statistical noise.
- `challenger`: overfitting to metric or weakening guard checks.

No fan-out for one small measured change with stable metric/guard. Never let specialist change metric/guard scripts unless explicitly in `scope_files` and measurement-integrity risk recorded.

**Structural context (optional)**: when `scope_files` resolves to Python module/symbol, select one task-neutral route and probe codemap-py once before first iteration: `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/codemap_adapter.py context --category implementation --query-kind <kind> [--target <qname>] --out <run-directory>/codemap-context.json`. Use `skip` for exact localized optimization with no unresolved structural fact, matching single route (`central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling`) for one unresolved fact, `standard` for broad or unknown scope. Map direct, all, or production caller questions to `callers`; use `blast` only for explicitly transitive caller questions. An explicit user or tool request for structural evidence overrides `skip`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with hypothesis above. Persist result once here, before step 04 applies any change; any triggered specialist consumes `<run-directory>/codemap-context.json`; added queries only under the contract's bounded `Specialist follow-up queries` rule.

Machine-readable iteration log, `single` and non-checkpointed `campaign` only: `<run-directory>/experiments.jsonl` is created once, after step 07 decides keep or revert for the final iteration. Do not create it here; a checkpointed campaign's `campaign.py init` creates its own ledger and fails if the file exists.

### 04: Apply one minimal optimization change per iteration

One independent hypothesis per iteration. Never optimize unmeasured paths. Before each, write `<run-directory>/iteration-<n>-before.patch` with scoped-file diff; after the change, write `<run-directory>/iteration-<n>-after.patch` the same way. The iteration's own change is after minus before, so earlier kept iterations and unrelated user edits stay out of it. If iteration fails and only its own change is present, reverse exactly that change, as in [campaign.md](campaign.md) restore rule; otherwise fail run when clean reversal cannot be proven. Never use `git reset --hard`.

### 05: Re-measure

Re-run same retained `metric_cmd` and `guard_cmd` separately with host-native command runner. Write complete combined output to `<run-directory>/iteration-<n>-metric-after.txt` and `<run-directory>/iteration-<n>-guard-after.txt`; retain both exit codes.

### 06: Compare baseline and after results in `<run-directory>/comparison.md`

Required fields:

- baseline value
- after value
- delta
- guard status
- confidence
- noise caveats

Write one JSON record per iteration to `<run-directory>/iteration-<n>-record.json` with the filesystem tool, once, never edited. Once the final iteration is decided (step 07), create `<run-directory>/experiments.jsonl` exclusively with the filesystem tool from those record files, one object per line in iteration order: a new derived log, as in [Component ablations](ablation.md), never a ledger appended to mid-run. Checkpointed campaigns skip this; `campaign.py finish` is their only writer. Record shape:

```json
{
  "iteration": 1,
  "hypothesis": "one-line mechanism",
  "metric_before": 0.0,
  "metric_after": 0.0,
  "delta": 0.0,
  "guard": "pass|fail",
  "decision": "kept|reverted|inconclusive|failed",
  "rollback_evidence": "path or reason"
}
```

### 07: Decide keep/revert

- Keep only with intended metric movement and passing guards.
- With `min_delta`, keep only if delta meets/exceeds practical-significance threshold.
- Revert or fail on guard regression.
- For noisy measurement, repeat or mark inconclusive.
- In `campaign`, stop at first kept result unless user asked continued exploration; otherwise continue only while `max_iterations` remains and each rejected iteration has rollback evidence.

### 08: Run shared quality gates

Inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gates.py --help`. Tests runs configured test or guard command; give real commands or explicit reasons for other gates.

### 09: Write and validate the mandatory result artifact

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write `OPTIMIZE_METADATA`, validate as `optimize`, promote only validated candidate.

For managed campaigns/resume, retrospective, and ablation, add `metadata.optimize_lifecycle` with `schema_version=1`, matching `mode`, and `bindings` mapping every required retained filename to its exact SHA-256. Campaign/resume/retrospective bind `campaign.json` and `experiments.jsonl`; ablation binds `ablation-input.json`, whose measurement receipts bind their outputs. Shared artifact validation calls `campaign.py admit` before promotion and rejects missing metadata when managed-state markers exist, corrupted history, pending iterations, stale active source, or mismatched receipts. Marker-free historical artifacts retain their existing reader. Final completed campaigns can validate after exhausting their total iteration budget; only scheduling another iteration is prohibited.

Managed structured Results cells must match the bound attempts or measurements, using the exact row conventions in the selected mode document. Admission rejects missing, extra or contradictory rows; narrative summaries do not replace this reconciliation.

## Fail-Fast Rules

01. Missing metric or guard command => fail.
02. Baseline cannot be captured => fail.
03. Scope is unbounded => fail.
04. Guard regression after change => fail unless reverted.
05. Metric/guard script changed without explicit scope and measurement-integrity note => fail.
06. Campaign iteration rejected without rollback evidence or unresolved-risk note => fail.
07. Claimed improvement below `min_delta` without explicit inconclusive status => fail.
08. Result artifact validator failure => fail.
09. Result artifact missing => fail.
10. Managed resume with changed configuration/source, invalid progression, unresolved pending iteration or exhausted scheduling budget => stop that scheduling attempt; preserve evidence, never replay or reset.
11. Ablation reference differs from the frozen best source, measurement source changes, guard fails, or evidence is missing => no contribution claim for that measurement.
12. Adaptive iteration history alone never supports a statistical significance claim.

## Quality Gates

Required:

- `tests`: guard command or impacted tests.
- `review`: metric comparison, rollback decision, relevant campaign ledger, `git diff --check`.
- `artifact`: shared validator confirms comparison, experiments JSONL, gate logs, result JSON shape.

Recommended:

- `lint`, `format`, `types`: run for any code edits.

## Calibration Hooks

On metric/guard-policy change, update calibration:

- behavioral cases: baseline missing, guard regression, noisy metric overclaim, campaign rollback evidence, below-threshold improvement, artifact validator bypass
- benchmark patterns: `optimize`
- managed lifecycle cases: dirty snapshot reconstruction, omitted lifecycle metadata, stale resume source/configuration, append-before-marker-removal recovery, exhausted-budget completion, ablation source/receipt drift, retrospective overclaim.

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared frame with `Next steps`. `Outcome`: `kept|reverted|inconclusive|failed`. `Results`: exactly `Iteration | Baseline | After | Delta | Guard | Decision`, one row/material iteration or campaign. Include method, guards, uncertainty, deferred experiments, noise limits.

Minimum artifact payload template: `result-template.json`.
