---
name: Rig Implement
description: Execute a frozen plan or a trivial change with an inline mini-plan, run configured project gates, and produce an evidence-backed commit handoff. Use after planning or for a clear scoped fix under 3 files and 50 changed lines. NOT for review-only or investigation-only requests.
---

# Rig Implement

> Read [Evidence rules](../../references/evidence.md) and [Commit handoff](../../references/commit-handoff.md). Follow [Role dispatch](../../references/evidence.md#role-dispatch) for delegated work and the shared [Optional command compression](../../references/evidence.md#optional-command-compression) guidance for read-heavy commands.

## Input

- A frozen `plan.md` from a completed, independently reviewed `rig-plan` run, with its run ID, digest, `revision`, and scope verdict table; or an inline mini-plan for a `TRIVIAL` change (under 3 files and 50 changed lines) with goal, owned paths, verdict table, steps, and acceptance checks.
- Optional: `kind` = `feature`, `fix`, or `refactor`. Performance optimization routes to `rig-optimize`.

> No plan, no implementation: a `TRIVIAL` change may use the inline mini-plan. Larger, ambiguous, or `HIGH_RISK` work (auth, credentials, data, release, CI, or migrations) requires `rig-plan` first.

## Workflow

1. **Start the run.** Detect gates using `rig_gate` with `action: "detect"`. If there is no configured gate, pass `gate_exemption` naming the reason and manual substitute at start. Call `rig_evidence` with `action: "start"`, `skill: "rig-implement"`, target paths, and both `plan_path` and `plan_run_id` for a frozen plan; keep the returned run ID. For an inline mini-plan, omit `plan_path` and `plan_run_id` and pass its complete text as `trivial_plan` at start, before editing.
2. **Verify the plan.** For a frozen plan, use `rig_evidence` with `action: "inspect"` and explicit `run_id` for both this run and the completed, independently reviewed `rig-plan` run. Compare the normalized project-relative `plan.path` values against the project root; equivalent spellings must not cause a false stop, and paths outside the project are rejected. Require digest equality between both `plan.digest` values, the plan run's `review.plan_digest`, and the current plan file's SHA-256: use `shasum -a 256` or `sha256sum` on POSIX, or `Get-FileHash -Algorithm SHA256` on Windows, with the plan file as the argument; compare lowercase hex of the raw bytes without rebinding. Missing provenance, missing review, different normalized paths, or a digest mismatch stops implementation. The tool checks reviewed-plan provenance and unchanged plan bytes at start and finalization. Read the plan's `revision` and confirm the work stays inside its `in-scope` rows. For an inline mini-plan, confirm `TRIVIAL` eligibility and its `in-scope` rows; the tool persists its text, while eligibility and semantic scope remain instruction-enforced. If source contradicts either plan, stop and report; do not rewrite the plan.
3. **Establish a baseline.** Run the narrowest relevant test or reproduction before editing. For a refactor, capture characterization tests first.
4. **Implement in batches.** Group non-overlapping edits and their tests into one batch. Run the focused check after each batch. When a batch fails, bisect to the smallest edit so attribution is not lost. When the `in-scope` rows decompose into disjoint ownership, run the pieces in parallel as bounded subagents: one owner per path, and serialise only rows that share a path.
5. **Reconcile the scope.** After the last edit and any formatter, call `rig_evidence` with `action: "refresh"` and a `message` naming what changed. This is the only way the run adopts edited source.
6. **Run configured gates.** Call `rig_gate` with `action: "run"` and the run ID. If no configured gate exists, execute and record the manual substitute named in the start-time exemption instead. Fix failures and rerun the same gate. Never retry a failing gate unchanged, and never substitute a manual claim for a failed check. If a gate rewrites files, refresh the scope and rerun that gate.
7. **Record outputs.** `rig_evidence` with `action: "record"` for changed paths, limits, and any decision that future readers need. Map every changed path to an `in-scope` row; a path outside the table stops the handoff, which waits for the user to approve a plan revision.
8. **Finalize.** For a frozen plan, recheck the plan's SHA-256 against both bound digests using a raw-bytes command from step 2; stop on any mismatch and never rebind an edited plan to bypass the check. For an inline mini-plan, recheck the actual task-owned diff: 3+ files, 50+ changed lines, ambiguity, or `HIGH_RISK` scope stops the handoff and routes to `rig-plan`. After the applicable check passes, call `rig_evidence` with `action: "finalize"`. A failed gate or source drift blocks completion; never edit the record to bypass it. If step 7 detects source drift or finalization returns `stale`, call `rig_evidence` with `action: "refresh"`, the run ID and a `message` giving the reason, rerun the affected evidence and required checks, and finalize again; a completed or failed run cannot be refreshed, so start one replacement run with the old run ID in its start `message`. If drift recurs, stop and report incomplete.
9. **Commit handoff.** Propose the exact commit message and the paths grouped by plan row. Follow [Commit handoff](../../references/commit-handoff.md): honor explicit restrictions; a user-requested commit or valid existing grant needs no new conversational approval, and proposing this handoff alone does not authorize a commit. Without a grant, use the runtime shell permission prompt for the one-time **Approve** or **Deny** decision on that exact commit; do not ask for a conversational **Approve** first. **Approve** authorizes only the proposed commit without a persistent grant; **Deny** leaves changes unstaged. If the user wants persistent approval, offer **Approve always** separately: its option text states its repository-wide scope across worktrees, the 30-day lifetime, the exclusions, and how to revoke with `rig_git_grant` with `action: "revoke"` before the user answers. Only **Approve always** records `rig_git_grant` with `action: "approve"`, the user's literal `Approve always` answer in `confirm`, and `scope_acknowledged: true` only after that answer accepts the stated scope. Never push.

## Rules

- Do not touch paths outside the owned set; a path the verdict table does not mark `in-scope` stops work until the user approves a plan revision.
- Do not delete or weaken tests to reach green.
- Preserve unrelated working-tree changes.
- Never revert a path that is not in an `in-scope` row. Before reverting, discarding, or overwriting a change you did not author, establish whether it is intended; ask when the tree is shared.
- Keep the change minimal: no opportunistic refactors outside the plan.
- Report incomplete work as incomplete; a partial fix with a clear stop is valid.

## Output

Report run ID, changed paths grouped by plan row with digests, gate receipts, failed or skipped checks, remaining risks, and the commit handoff. Do not claim completion without a finalized run.
