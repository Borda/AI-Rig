---
name: Rig Implement
description: Execute a frozen plan or bounded change, run configured gates, and produce an evidence-backed commit handoff.
---

# Rig Implement

> Read [Evidence rules](../../references/evidence.md) and [Commit handoff](../../references/commit-handoff.md).

## Input

- A frozen `plan.md` with its digest, or a bounded change with explicit scope.
- Optional: `kind` = `feature`, `fix`, `refactor`, or `perf`.

## Workflow

1. **Revalidate the plan.** Read current source and compare it with the plan's assumptions. If source contradicts the plan, stop and report; do not rewrite the plan.
2. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-implement"`, target paths, and the plan path. Record the plan digest.
3. **Establish a baseline.** Run the narrowest relevant test or reproduction before editing. For a refactor, capture characterization tests first.
4. **Implement in batches.** Group non-overlapping edits and their tests into one batch. Run the focused check after each batch. When a batch fails, bisect to the smallest edit so attribution is not lost.
5. **Reconcile the scope.** After the last edit and any formatter, call `rig_evidence` with `action: "refresh"` and a `message` naming what changed. This is the only way the run adopts edited source.
6. **Run configured gates.** Call `rig_gate` with `action: "run"` and the run ID. Fix failures and rerun the same gate. Never retry a failing gate unchanged, and never substitute a manual claim for a failed check. If a gate rewrites files, refresh the scope and rerun that gate.
7. **Record outputs.** `rig_evidence` with `action: "record"` for changed paths, limits, and any decision that future readers need.
8. **Finalize.** `rig_evidence` with `action: "finalize"`. A failed gate or source drift blocks completion; drift means refresh and rerun, never editing the record.
9. **Commit handoff.** Propose the exact commit message and paths. A plain commit runs when the user asked for it or a local Git grant covers it; otherwise ask once. Never push.

## Rules

- Do not touch paths outside the owned set without saying so and getting scope approval.
- Do not delete or weaken tests to reach green.
- Preserve unrelated working-tree changes.
- Keep the change minimal: no opportunistic refactors outside the plan.
- Report incomplete work as incomplete; a partial fix with a clear stop is valid.

## Output

Report run ID, changed paths with digests, gate receipts, failed or skipped checks, remaining risks, and the commit handoff. Do not claim completion without a finalized run.
