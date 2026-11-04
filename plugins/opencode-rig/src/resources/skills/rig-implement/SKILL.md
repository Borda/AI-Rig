---
name: Rig Implement
description: Execute a frozen plan or bounded change, run configured gates, and produce an evidence-backed commit handoff.
---

# Rig Implement

> Read [Evidence rules](../../references/evidence.md) and [Commit handoff](../../references/commit-handoff.md).

## Input

- A frozen `plan.md` bound to the run by digest, with its `revision` and scope verdict table.
- Optional: `kind` = `feature`, `fix`, `refactor`, or `perf`.

> No plan, no implementation: a bounded change still needs a frozen plan. Without one, stop and run `rig-plan` first.

## Workflow

1. **Bind the plan.** Refuse to start without a frozen plan bound to the run. Read its digest and `revision`, and confirm the work stays inside its `in-scope` rows. If source contradicts the plan, stop and report; do not rewrite the plan.
2. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-implement"`, target paths, and the plan path. Record the plan digest.
3. **Establish a baseline.** Run the narrowest relevant test or reproduction before editing. For a refactor, capture characterization tests first.
4. **Implement in batches.** Group non-overlapping edits and their tests into one batch. Run the focused check after each batch. When a batch fails, bisect to the smallest edit so attribution is not lost. When the `in-scope` rows decompose into disjoint ownership, run the pieces in parallel as bounded subagents: one owner per path, and serialise only rows that share a path.
5. **Reconcile the scope.** After the last edit and any formatter, call `rig_evidence` with `action: "refresh"` and a `message` naming what changed. This is the only way the run adopts edited source.
6. **Run configured gates.** Call `rig_gate` with `action: "run"` and the run ID. Fix failures and rerun the same gate. Never retry a failing gate unchanged, and never substitute a manual claim for a failed check. If a gate rewrites files, refresh the scope and rerun that gate.
7. **Record outputs.** `rig_evidence` with `action: "record"` for changed paths, limits, and any decision that future readers need. Map every changed path to an `in-scope` row; a path outside the table stops the handoff, which waits for the user to approve a plan revision.
8. **Finalize.** `rig_evidence` with `action: "finalize"`. A failed gate or source drift blocks completion; drift means refresh and rerun, never editing the record.
9. **Commit handoff.** Propose the exact commit message and the paths grouped by plan row. Before any local commit, ask the user and record `rig_git_grant` with `action: "approve"`, their own words in `confirm`, and `scope_acknowledged: true`. Never commit on an absent grant. Never push.

## Rules

- Do not touch paths outside the owned set; a path the verdict table does not mark `in-scope` stops work until the user approves a plan revision.
- Do not delete or weaken tests to reach green.
- Preserve unrelated working-tree changes.
- Never revert a path that is not in an `in-scope` row. Before reverting, discarding, or overwriting a change you did not author, establish whether it is intended; ask when the tree is shared.
- Keep the change minimal: no opportunistic refactors outside the plan.
- Report incomplete work as incomplete; a partial fix with a clear stop is valid.

## Tooling

> Guidance, never enforcement: a missing RTK changes nothing.

- When `command -v rtk` succeeds, prefix read-heavy shell commands with `rtk`; it rewrites them to `rtk <cmd>` for roughly 60–99% fewer input tokens.
- RTK must already be installed; never install it during a run. A command RTK cannot filter runs unchanged.

## Output

Report run ID, changed paths grouped by plan row with digests, gate receipts, failed or skipped checks, remaining risks, and the commit handoff. Do not claim completion without a finalized run.
