---
name: Rig Plan
description: Produce a frozen implementation plan with owned paths, configured gates, risks, and acceptance criteria.
---

# Rig Plan

> Read [Evidence rules](../../references/evidence.md) before writing any factual claim.

Produce one plan that implementation can execute without re-deciding scope.

## Input

- Goal or issue.
- Target paths, PR, or area, when known.
- Constraints: compatibility, deadlines, review requirements.

## Workflow

1. **Classify scope.** `TRIVIAL` under 3 files and 50 changed lines; `LOCAL` one subsystem; `BROAD` cross-subsystem or 8+ files; `HIGH_RISK` when auth, credentials, data, release, CI, or migrations are touched. State the classification and why.
2. **Collect evidence.** Read the actual source, tests, configuration, and consumers. Do not plan from filenames alone. Record each authoritative file in the run.
3. **Start the run.** Call `rig_evidence` with `action: "start"`, `skill: "rig-plan"`, and the target paths. Keep the returned `run_id`.
4. **Map the change.** List each affected component, its consumer, the owned path, and the check that proves it still works.
5. **Write the plan** to `<run-dir>/plan.md` with: goal, scope classification, numbered steps, owned paths, gates, risks, rollback, acceptance criteria, and explicit non-goals.
6. **Challenge the plan once.** Dispatch `rig-challenger` (native subagent when installed, otherwise `rig_dispatch`) with the plan, source digests, and acceptance criteria. Accept only findings that cite source.
7. **Freeze it.** Record the plan with `rig_evidence` (`action: "record"`, `plan_path`, and `reviewer` naming the independent challenger) and finalize.

## Rules

- A frozen plan is read-only once implementation starts. If source contradicts the plan, stop and report the contradiction; never rewrite the plan to match what was built.
- Every step names its owned path. Two steps may not own the same path.
- Gates come from `rig_gate` detection, never from invented commands.
- If no configured gate exists, record a gate exemption and say what manual check replaces it.
- Plans stay in the run directory. Do not ship plan text or private evidence as product content.

## Output

Report the plan path, scope classification, digest, step count, gates, top risks, and any unresolved decision. Do not claim the change is implemented.
