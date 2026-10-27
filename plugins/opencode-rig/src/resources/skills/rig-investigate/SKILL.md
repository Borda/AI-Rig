---
name: Rig Investigate
description: Reproduce a failure and narrow it to an evidence-backed root cause before fixing.
---

# Rig Investigate

> Read [Evidence rules](../../references/evidence.md). A plausible explanation is not a root cause.

## Input

- Symptom, failing command, stack trace, or behavior.
- Known environment and versions.
- Paths or subsystem hints.

## Workflow

1. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-investigate"`, and candidate paths.
2. **Reproduce with real inputs.** Run the failing command. If it does not reproduce, record the exact difference and stop rather than guessing.
3. **Shrink the case.** Remove unrelated configuration and files until the smallest failing case remains. Record each removal.
4. **List hypotheses.** Write at least two plausible causes. For each, name the observation that would disprove it.
5. **Test one at a time.** Change one variable per run. Record the command, output digest, and conclusion. Optional: dispatch read-only `rig-investigator` children for independent hypotheses; they share the tree, so they must not edit files.
6. **Confirm the cause.** Show the failing behavior disappearing when the cause is removed, or explain why that experiment is unavailable.
7. **Record the diagnosis** with reproduction command, root cause, evidence, eliminated hypotheses, confidence, and the smallest next change.

## Rules

- Correlation is not causation; if the evidence only supports a correlation, say so.
- Do not weaken a test to make it pass, and do not change production code before the cause is confirmed.
- Missing environment or credentials are an explicit limit, not a reason to simulate a pass.
- Python scopes may probe codemap when available (one probe); absence is recorded, not fatal.

## Output

Report reproduction status, root cause, evidence references, confidence, limits, and the recommended next action. Do not present the fix as already verified.
