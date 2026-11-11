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

1. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-investigate"`, and candidate paths. Record `git status --porcelain` and a SHA-256 digest of the raw `git diff` output as the working-tree baseline before reproduction.
2. **Reproduce with real inputs.** Run the failing command. If it does not reproduce, stop experiments rather than guessing; record the exact difference and incomplete diagnosis in step 7, then finalize in step 8.
3. **Shrink the case.** Create a user-only scratch copy of only the minimal failing inputs, including relevant uncommitted changes, in this run directory under the project's ignored `.opencode-rig/` artifact root. Never copy credential files (`.env`, key and token files, credential stores); record their absence as a limit or reference them in place read-only. Verify the same failure there and prove that the command executes the scratch bytes before any reduction or experiment: a canary edit in the copy must change the output (then undo it), or print a resolved import or executable path inside the copy. Check resolution through editable installs, `.pth` files, virtualenv launchers with absolute shebangs, workspace symlinks, and absolute configuration paths rather than assuming a copied launcher executes copied code. If the copy does not reproduce the failure or does not isolate execution, record that as a limit, stop experiments, record the incomplete diagnosis in step 7, and finalize per step 8. Once verified, remove unrelated configuration and files until the smallest failing case remains; never remove files from the user's working tree. Record each removal and the scratch location.
4. **List hypotheses.** Write at least two plausible causes. For each, name the observation that would disprove it.
5. **Test one at a time.** Change one variable per run in the scratch copy; reset only that experiment's changes before testing the next hypothesis. Record the command, output digest, and conclusion. Optional: dispatch read-only `rig-investigator` children for independent hypotheses against an untouched source snapshot, separate from the scratch copy being changed; they must not edit files.
6. **Confirm the cause.** In the scratch copy, show the failing behavior disappearing when the cause is removed and returning when that change is undone, or explain why that experiment is unavailable and mark the cause unconfirmed.
7. **Record the diagnosis.** After experiments and any recovery, call `rig_evidence` with `action: "refresh"` and a `message` explaining the final source state, then `action: "record"` with `decision` for reproduction command, root cause or unconfirmed hypothesis, evidence, eliminated hypotheses, and smallest next change; include `limits` and `confidence`. If the source changed during investigation, repeat the reproduction and reassess the diagnosis against the refreshed bytes before recording.
8. **Finalize.** Delete the scratch copy whatever the outcome, including failure or interruption; if cleanup is unavailable, record its retained path as a limit before finalizing. Call `rig_evidence` with `action: "finalize"` and the run ID. If step 7 detects source drift or finalization returns `stale`, call `rig_evidence` with `action: "refresh"`, the run ID and a `message` giving the reason, rerun the affected evidence and required checks, and finalize again; a completed or failed run cannot be refreshed, so start one replacement run with the old run ID in its start `message`. If drift recurs, stop and report incomplete. A finalized investigation records the outcome; it does not turn an incomplete diagnosis or unconfirmed cause into a verified root cause.

## Rules

- Correlation is not causation; if the evidence only supports a correlation, say so.
- Do not weaken a test to make it pass, and do not apply production fixes before the cause is confirmed. Temporary diagnostic edits are confined to the scratch copy.
- Preserve the user's working tree. Use step 1's baseline to distinguish pre-existing and concurrent changes; undo only experiment-owned edits in the scratch copy. Never fall back to in-tree experiments. If reproduction unexpectedly changes source, stop when those edits cannot be separated safely; preserve pre-existing and concurrent changes and verify the original reproduction before refreshing evidence.
- Missing environment or credentials are an explicit limit, not a reason to simulate a pass.

## Output

Report run ID, finalization status, reproduction status, root cause or unconfirmed hypothesis, evidence references, confidence, limits, and the recommended next action. Route a confirmed fix to `rig-plan` before `rig-implement`; a clearly `TRIVIAL` fix may use `rig-implement`'s inline mini-plan. Do not present the fix as already verified.
