---
name: Rig Optimize
description: Improve a measured performance metric in guarded rounds, keeping only changes that beat the baseline noise and pass every correctness gate.
---

# Rig Optimize

> Read [Evidence rules](../../references/evidence.md).

> This workflow changes code, one measured variable at a time. It never commits, pushes, or rewrites history; a human or `rig-implement` owns the commit.

## Preconditions

- The benchmark is an already-configured gate. Detect it with `rig_gate` and `action: "detect"`.
- The benchmark prints a stable metric and exits non-zero on failure.
- There is no benchmark gate? Stop and ask the user to configure one. Never invent an ad-hoc benchmark command, and never use a paid or network-dependent benchmark without explicit approval.

## Workflow

1. **Declare the contract.** Metric, unit, direction (lower or higher is better), target, minimum improvement (default 5%), rounds budget (default 3), and the exact gate id. Start the run with `rig_evidence` and bind the benchmark script and fixtures as `inputs`.
2. **Measure the baseline.** Run the benchmark gate at least three times from a clean start. Record every receipt, the median, the spread, and the environment (hardware, runtime version, cache state). Statistical noise is evidence, not an obstacle.
3. **Profile before editing.** Use the project's profiler or timing instrumentation to name the hotspot. Profile output is an `output`, not a claim.
4. **Change one variable.** State the hypothesis, the expected effect, the files to touch, and the rollback. Do not combine a refactor with a measurement round.
5. **Re-measure the same way.** Rerun the benchmark gate at least three times under the same conditions. A win must exceed the baseline spread and the declared minimum improvement. Also run every non-benchmark gate: a faster build that breaks correctness is a regression.
6. **Decide and record.** Keep the round when it wins and everything passes; otherwise restore the touched files exactly and record the failed hypothesis with `rig_evidence` `action: "record"` and a `decision`. Never rewrite or delete the measurement history.
7. **Stop honestly.** Stop on target reached, improvement below noise after the round budget, a correctness regression, or an exhausted budget. A non-improvement is a valid result.
8. **Review independently.** Dispatch `rig-optimizer` to check the methodology: sample count, variance, warmup, environment, and whether the benchmark still measures the intended behavior. Record it with `reviewer`.
9. **Finalize.** `rig_gate` for the full set, then `rig_evidence` with `action: "finalize"`.

## Rules

- Never change the benchmark, its fixtures, or its inputs in a round that claims a win. A benchmark change is its own round with its own baseline.
- Never skip a correctness gate because the benchmark passed.
- Never claim an improvement from a single run, from an unmeasured change, or from a different machine.
- Never upgrade a dependency, change a compiler flag, or alter a fixture without explicit approval.
- Never commit, push, tag, or rewrite history in this workflow.
- If the metric is unstable across identical runs, report that first and stop; do not optimize against noise.

## Output

The metric contract, baseline median and spread, per-round hypothesis and measurement, kept and reverted rounds with reasons, the final measured delta, correctness gate results, environment, limits, and the proposed commit scope for a human.
