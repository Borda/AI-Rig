---
name: Rig Plan
description: Produce a frozen implementation plan with owned paths, configured gates, risks, and acceptance criteria before code changes. Use for a multi-file or ambiguous change. NOT for a one-line edit or a read-only question.
---

# Rig Plan

> Read [Evidence rules](../../references/evidence.md) before writing any factual claim. Follow [Role dispatch](../../references/evidence.md#role-dispatch) for native-agent fallback and stop on reported scope violations.

Produce one plan that implementation can execute without re-deciding scope.

## Input

- Goal or issue.
- Target paths, PR, or area, when known.
- Constraints: compatibility, deadlines, review requirements.

## Workflow

1. **Classify scope.** `TRIVIAL` under 3 files and 50 changed lines; `LOCAL` one subsystem; `BROAD` cross-subsystem or 8+ files; `HIGH_RISK` when auth, credentials, data, release, CI, or migrations are touched. State the classification and why.
2. **Start the run.** Detect gates with `rig_gate` and `action: "detect"`. Call `rig_evidence` with `action: "start"`, `skill: "rig-plan"`, and the target paths; if no configured gate exists, include `gate_exemption` describing the manual check that replaces it. Keep the returned `run_id`.
3. **Collect evidence.** Read the actual source, tests, configuration, and consumers. Do not plan from filenames alone. Record each authoritative file with `rig_evidence`, `action: "record"`, the returned `run_id`, and `inputs`.
4. **Decide every surface.** Put each candidate surface in one verdict table with exactly one verdict: `in-scope`, `deferred` with its unblock condition, or `out-of-scope` with its reason. No surface stays unclassified.
5. **Reconcile owned paths.** For each `in-scope` row, name its owned path, its consumer, and the check that proves it still works. The table and the owned-path list must reconcile both ways: every owned path has an `in-scope` row, and every `in-scope` row names a path.
6. **Build the task tree.** Derive it from the `in-scope` rows, never hand-write a second scope. The tree is depth-first: a node splits only when its goal is not a coherent, independently verifiable unit; `split_reason` says why. Every leaf has one owner, one owned path set and exactly one acceptance check. Leaves must be a subset of the `in-scope` rows; a shared path between siblings becomes a `depends_on` edge, never parallel siblings. Caps: depth 4, 32 leaves.
7. **Write the plan** to `<run-dir>/plan.md` with: a `revision` counter, the verdict table, a "Verdict changes since the previous revision" section, a `## Task tree` JSON block, the derived workstreams, goal, scope classification, numbered steps, owned paths, gates, risks, rollback, acceptance criteria, and explicit non-goals.
8. **Challenge until the plan is ready.** Compute the plan file's SHA-256 and dispatch `rig-challenger` (native subagent when installed, otherwise `rig_dispatch`) with the exact plan bytes and digest, the verdict table, the task tree, source digests and acceptance criteria. Accept only findings that cite source. Resolve every blocking finding, write the round into the plan's `## Challenge rounds` section (round, reviewer, open/fixed signatures, stop reason), and re-challenge the revised bytes. Repeat until the challenger reports no blocking findings (`converged`), or record `round-limit` at `options.maxLoopRounds` (an instruction budget) or `authority-required` for a decision only the user can make. `converged` is the only outcome that freezes; a `round-limit`/`authority-required` plan records its residue and is not ready.
9. **Freeze it.** Record the plan with `rig_evidence` (`action: "record"`, `plan_path`, `plan_digest` containing the exact digest assessed by the independent challenger, and `reviewer` naming that challenger) and finalize. Finalization rejects a digest mismatch between reviewed, bound, and current plan bytes; never replace the reviewed digest with one from unreviewed edits.

## Rules

- A frozen plan is read-only once implementation starts. If source contradicts the plan, stop and report the contradiction; never rewrite the plan to match what was built.
- The verdict table defines scope, not the prose: a surface marked `deferred` or `out-of-scope` is not worked on, whatever the steps imply.
- Every revision lists each changed verdict under "Verdict changes since the previous revision"; a surface moving to `deferred` or `out-of-scope` must appear there, never only inside rewritten prose.
- Every step names its owned path. Two steps may not own the same path, and no path enters a workstream unless the table marks it `in-scope`.
- Gates come from `rig_gate` detection, never from invented commands.
- If no configured gate exists, pass `gate_exemption` at `action: "start"` and say what manual check replaces it.
- Plans stay in the run directory. Do not ship plan text or private evidence as product content.

## Output

> Lead with the [handoff table](../../references/handoff.md): verdict, alignment, findings, next action, confidence. Then the evidence below.

Report the plan path, revision, scope classification, digest, verdict counts, task-tree leaves/owners, plan-challenge rounds and outcome (`converged`, `round-limit`, `authority-required`), gates, top risks, and any unresolved decision. Do not claim the change is implemented.
