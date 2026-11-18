---
name: Rig Implement
description: Execute a frozen plan or a trivial change with an inline mini-plan, run configured project gates, and produce an evidence-backed commit handoff. Use after planning or for a clear scoped fix under 3 files and 50 changed lines. NOT for review-only or investigation-only requests.
---

# Rig Implement

> Read [Evidence rules](../../references/evidence.md) and [Commit handoff](../../references/commit-handoff.md). Follow [Role dispatch](../../references/evidence.md#role-dispatch) for delegated work and the shared [Optional command compression](../../references/evidence.md#optional-command-compression) guidance for read-heavy commands.

## Input

- A frozen `plan.md` from a completed, independently reviewed `rig-plan` run, with its run ID, digest, `revision`, and scope verdict table; or
- a prompt plus a structured mini-plan for a `TRIVIAL` change (under 3 files and 50 changed lines).

The mini-plan is the object the tool validates at start; a prompt alone is not one:

```json
{
  "goal": "one sentence stating the outcome",
  "owned_paths": ["src/example.ts"],
  "acceptance_check": "the exact focused command or observation that proves it",
  "feasibility": "why this is understood, coherent and safe to start now"
}
```

- Optional: `kind` = `feature`, `fix`, or `refactor`. Performance optimization routes to `rig-optimize`.

> **No plan, no implementation.** Without a frozen plan or a valid mini-plan, stop and use `rig-plan`. Never start an unclear change to find out whether it is small: that is the wasted effort this gate exists for. Ambiguity, `HIGH_RISK` scope (auth, credentials, data, release, CI, migrations), 3+ files, or no honest feasibility statement means `rig-plan` first. The tool rejects a malformed, directory-valued, VCS-internal (`.git`), or scope-widening mini-plan, normalizes the owned paths, and scopes the run to exactly those files; a declared file may not exist yet, so a small new file starts normally and must exist by refresh. The size guideline is accounting: the run reports the change it measured from its own start bytes, and a change above 50 lines or two files is recorded as a limit that points later work at `rig-plan`, not as a failure.

## Workflow

01. **Start the run.** Detect gates using `rig_gate` with `action: "detect"`. If there is no configured gate, pass `gate_exemption` naming the reason and manual substitute at start. Call `rig_evidence` with `action: "start"`, `skill: "rig-implement"`, target paths, and both `plan_path` and `plan_run_id` for a frozen plan; keep the returned run ID. For a trivial change, omit `plan_path` and `plan_run_id` and pass the mini-plan JSON as `trivial_plan`; its `owned_paths` become the scope.
02. **Verify the plan.** For a frozen plan, use `rig_evidence` with `action: "inspect"` and explicit `run_id` for both this run and the completed, independently reviewed `rig-plan` run. Compare the normalized project-relative `plan.path` values against the project root; equivalent spellings must not cause a false stop, and paths outside the project are rejected. Require digest equality between both `plan.digest` values, the plan run's `review.plan_digest`, and the current plan file's SHA-256: use `shasum -a 256` or `sha256sum` on POSIX, or `Get-FileHash -Algorithm SHA256` on Windows, with the plan file as the argument; compare lowercase hex of the raw bytes without rebinding. Missing provenance, missing review, different normalized paths, or a digest mismatch stops implementation. The tool checks reviewed-plan provenance and unchanged plan bytes at start and finalization. Read the plan's `revision` and confirm the work stays inside its `in-scope` rows. For a mini-plan, confirm the change is still the smaller case it claimed: same owned paths, no new requirement, no `HIGH_RISK` surface. If source contradicts either plan, stop and report; do not rewrite the plan.
03. **Establish a baseline.** Run the narrowest relevant test or reproduction before editing. For a refactor, capture characterization tests first.
04. **Implement the task tree depth-first.** Walk leaves in tree order; each leaf is a batch: one owner, one acceptance check, and a focused run before the next leaf. A failed leaf check blocks its subtree. Disjoint leaves must run as bounded subagents (one owner per path); serialise only rows that share a path, and never run a shared path in parallel. A leaf whose check cannot run is an explicit block, not a pass. A mini-plan is a single leaf.
05. **Reconcile the scope.** After the last edit and any formatter, call `rig_evidence` with `action: "refresh"` and a `message` naming what changed. This is the only way the run adopts edited source; it clears the previous challenge and findings, which must then be re-recorded against the new bytes. An open challenge ledger keeps its rounds and continues with `action: "append"`; a closed one is archived, so the next challenge starts a fresh ledger.
06. **Run configured gates.** Call `rig_gate` with `action: "run"` and the run ID. If no configured gate exists, execute and record the manual substitute named in the start-time exemption instead. Fix failures and rerun the same gate. Never retry a failing gate unchanged, and never substitute a manual claim for a failed check. If a gate rewrites files, refresh the scope and rerun that gate.
07. **Record outputs.** `rig_evidence` with `action: "record"` for changed paths, limits, and any decision that future readers need. Map every changed path to an `in-scope` row; a path outside the table stops the handoff, which waits for the user to approve a plan revision.
08. **Run the final independent challenge.** After the gates pass and before finalizing, dispatch `rig-challenger` (native subagent when installed, otherwise `rig_dispatch`) with the changed paths, the plan tree or mini-plan and acceptance criteria, and the gate receipts. Always write its result with `rig_findings` (`reviewer` names the challenger, evidence and coverage included; every finding carries a severity and a status). An empty inventory is the required record when the challenge found nothing; finalize rejects a missing one, and a trivial run is exempt. The record binds the current scope digest: any later edit invalidates it and requires a fresh challenge. The parent never counts its own analysis as the challenge.
09. **Resolve every finding.** Closure is the default, not a negotiation: on an in-scope path, fix it. `rejected` needs counter-evidence from source, not preference; `deferred` is only for a finding outside the plan's owned paths and needs an owner and a trigger; `accepted-risk` requires an explicit user decision recorded in the run. When the challenge reported a `security`, `critical`, or `high` finding that is not rejected, converge with the loop:
    - `rig_loop` with `action: "start"`, the implementation run ID, `reviewer`, and the challenge findings, using each finding ID as its stable signature. Start the ledger before fixing, so it shows the finding open and then fixed; every security, critical and high finding must appear, and finalization rejects a ledger that skipped one.
    - Fix the validated findings, `rig_evidence` `action: "refresh"`, rerun the affected gates, and re-challenge the new bytes with `rig-challenger`.
    - `rig_loop` with `action: "append"` re-assessing every prior signature as `open` or `fixed`, then `action: "finalize"` with `converged` when the last round has no open security, critical or high finding. The tool rejects convergence on an open high finding for an implementation run.
    - Repeat while a security, critical or high finding remains open, up to `options.maxLoopRounds`. At the limit, finalize the loop with `round-limit` and report the run as blocked with the residue; never claim convergence.
    - No security, critical or high finding: skip the loop entirely (early exit) and close the remaining low findings by disposition.
10. **Finalize.** For a frozen plan, recheck the plan's SHA-256 against both bound digests using a raw-bytes command from step 2; stop on any mismatch and never rebind an edited plan to bypass the check. For a mini-plan, read the measured change the tool reports; a measurement above the 50-line or 2-file guideline, an unmeasured file, ambiguity, or `HIGH_RISK` scope belongs in `rig-plan` next time, and this run records it as a limit. A declared file that is missing is still a block, and every recorded output must stay inside the declared paths. After the applicable check passes, call `rig_evidence` with `action: "finalize"`. It requires passing gates, a challenge bound to the current scope, a findings inventory with no open finding, and a converged loop whenever the challenge reported a security, critical or high finding. A failed gate or source drift blocks completion; never edit the record to bypass it. If step 7 detects source drift or finalization returns `stale`, call `rig_evidence` with `action: "refresh"`, the run ID and a `message` giving the reason, rerun the affected evidence and required checks, and finalize again; a completed or failed run cannot be refreshed, so start one replacement run with the old run ID in its start `message`. If drift recurs, stop and report incomplete.
11. **Commit handoff.** Propose the exact commit message and the paths grouped by plan row. Follow [Commit handoff](../../references/commit-handoff.md): honor explicit restrictions; a user-requested commit or valid existing grant needs no new conversational approval, and proposing this handoff alone does not authorize a commit. Without a grant, use the runtime shell permission prompt for the one-time **Approve** or **Deny** decision on that exact commit; do not ask for a conversational **Approve** first. **Approve** authorizes only the proposed commit without a persistent grant; **Deny** leaves changes unstaged. If the user wants persistent approval, offer **Approve always** separately: its option text states its repository-wide scope across worktrees, the 30-day lifetime, the exclusions, and how to revoke with `rig_git_grant` with `action: "revoke"` before the user answers. Only **Approve always** records `rig_git_grant` with `action: "approve"`, the user's literal `Approve always` answer in `confirm`, and `scope_acknowledged: true` only after that answer accepts the stated scope. Never push.

## Rules

- Do not touch paths outside the owned set; a path the verdict table does not mark `in-scope` stops work until the user approves a plan revision.
- Do not delete or weaken tests to reach green.
- Preserve unrelated working-tree changes.
- Never revert a path that is not in an `in-scope` row. Before reverting, discarding, or overwriting a change you did not author, establish whether it is intended; ask when the tree is shared.
- Keep the change minimal: no opportunistic refactors outside the plan. A recorded output outside the owned set is refused; the path check runs at start, refresh, record and gate time and is a snapshot: a symlink created and removed between calls cannot be observed, and the shell/script boundary remains the real write gate. The size measurement certifies what this run measured, not that the working tree holds nothing else. A path the run does not record is outside its evidence: the tool cannot observe an undeclared working-tree edit, and the handoff states that limit instead of claiming otherwise. Record every path you changed and stop the handoff rather than completing around one you cannot account for.
- Write ad-hoc probes as files under the run's `scripts/` directory and run them with `rig_script` (`source: "run"`, fixed interpreter, `argv` list); a project tool under `scripts/`, `bin/` or `tools/` uses `source: "project"`. Do not inline a `-c` body, a heredoc or a `mktemp` script: an inline body has no file provenance and asks.
- This loop is the automatic, severity-driven convergence step of an implementation. `rig-challenge` is the same ledger entered on request for risky work, and `code-remediate` remains the route for a review inventory or a finding outside this plan's scope.
- Report incomplete work as incomplete; a partial fix with a clear stop is valid.

## Output

> Lead with the [handoff table](../../references/handoff.md): verdict, alignment, findings, next action, confidence. Then the evidence below.

Report run ID, changed paths grouped by plan row with digests, task-tree leaves and delegated owners, the final-challenge reviewer and verdict, loop rounds and stop reason when the loop ran, gate receipts, failed or skipped checks, remaining risks, and the commit handoff. Do not claim completion without a finalized run.
