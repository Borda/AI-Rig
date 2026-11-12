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
6. **Derive the delegation brief.** Never hand-write it. Disjoint `in-scope` rows become parallel workstreams with one owner per path; rows that share a path are serialised as prerequisites.
7. **Write the plan** to `<run-dir>/plan.md` with: a `revision` counter, the verdict table, a "Verdict changes since the previous revision" section, the derived workstreams, goal, scope classification, numbered steps, owned paths, gates, risks, rollback, acceptance criteria, and explicit non-goals.
8. **Challenge the plan once.** Compute the current plan file's SHA-256 and dispatch `rig-challenger` (native subagent when installed, otherwise `rig_dispatch`) with those exact plan bytes and digest, verdict table, source digests, and acceptance criteria. Accept only findings that cite source. If the plan changes after the challenge, the revised bytes require an independent challenge before freezing.
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

Report the plan path, revision, scope classification, digest, verdict counts, workstreams, step count, gates, top risks, and any unresolved decision. Do not claim the change is implemented.
