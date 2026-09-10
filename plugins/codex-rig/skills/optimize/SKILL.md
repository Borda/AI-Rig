---
name: optimize
description: Optimize a measurable metric with bounded iterations, guardrails, and regression gates.
---

# Optimize

Metric-driven optimization with explicit guards, rollback criteria, experiment log.

## Input Schema

```json
{
  "goal": "required measurable improvement objective",
  "mode": "single|campaign",
  "metric_cmd": "required command that emits or validates the target metric",
  "metric_direction": "higher|lower",
  "guard_cmd": "required command that must continue to pass",
  "max_iterations": "optional integer, default 1",
  "min_delta": "optional practical significance threshold",
  "scope_files": [
    "paths the optimization may edit"
  ],
  "done_when": "metric improves without guard regression"
}
```

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

Execute configured `metric_cmd` and `guard_cmd` separately with host-native command runner. Write complete combined output to `<run-directory>/metric-baseline.txt` and `<run-directory>/guard-baseline.txt`; retain both exit codes and stop before editing if either command cannot run.

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

**Structural context (optional)**: when `scope_files` resolves to Python module/symbol, select one task-neutral route and probe codemap-py once before first iteration: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category implementation --query-kind <kind> [--target <qname>] --out <run-directory>/codemap-context.json`. Use `skip` for exact localized optimization with no unresolved structural fact, matching single route (`central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling`) for one unresolved fact, and `standard` for broad or unknown scope. Map direct, all, or production caller questions to `callers`; use `blast` only for explicitly transitive caller questions. An explicit user or tool request for structural evidence overrides `skip`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with hypothesis above. Persist result once here, before step 04 applies any change; any triggered specialist consumes `<run-directory>/codemap-context.json`, never fresh query.

Initialize machine-readable iteration log:

Create empty `<run-directory>/experiments.jsonl` with filesystem tool before first iteration.

### 04: Apply one minimal optimization change per iteration

One independent hypothesis per iteration. Do not optimize unmeasured paths. Before each, write `<run-directory>/iteration-<n>-before.patch` with scoped-file diff. If iteration fails and only its patch is present, revert with `git apply -R` against iteration diff; otherwise fail run when clean reversal cannot be proven. Never use `git reset --hard`.

### 05: Re-measure

Re-run same retained `metric_cmd` and `guard_cmd` separately with host-native command runner. Write complete combined output to `<run-directory>/metric-after.txt` and `<run-directory>/guard-after.txt`; retain both exit codes.

### 06: Compare baseline and after results in `<run-directory>/comparison.md`

Required fields:

- baseline value
- after value
- delta
- guard status
- confidence
- noise caveats

Append one JSON object/iteration to `<run-directory>/experiments.jsonl`:

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

Inspect `python PLUGIN_ROOT/shared/run_gates.py --help`. Tests runs configured test or guard command; give real commands or explicit reasons for other gates.

### 09: Write and validate the mandatory result artifact

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write `OPTIMIZE_METADATA`, validate as `optimize`, promote only validated candidate.

## Fail-Fast Rules

1. Missing metric or guard command => fail.
2. Baseline cannot be captured => fail.
3. Scope is unbounded => fail.
4. Guard regression after change => fail unless reverted.
5. Metric/guard script changed without explicit scope and measurement-integrity note => fail.
6. Campaign iteration rejected without rollback evidence or unresolved-risk note => fail.
7. Claimed improvement below `min_delta` without explicit inconclusive status => fail.
8. Result artifact validator failure => fail.
9. Result artifact missing => fail.

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

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared frame with `Next steps`. `Outcome`: `kept|reverted|inconclusive|failed`. `Results`: exactly `Iteration | Baseline | After | Delta | Guard | Decision`, one row/material iteration or campaign. Include method, guards, uncertainty, deferred experiments, noise limits.

Minimum artifact payload template: `result-template.json`.
