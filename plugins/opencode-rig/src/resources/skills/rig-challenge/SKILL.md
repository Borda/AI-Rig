---
name: Rig Challenge
description: Run a bounded adversarial convergence loop with an independent challenger, validated findings, fixes, independent re-assessment, and a stop reason. Use for risky or security-sensitive changes. NOT for routine edits.
---

# Rig Challenge

> Read [Evidence rules](../../references/evidence.md) and [Convergence](../../references/convergence.md). Follow [Role dispatch](../../references/evidence.md#role-dispatch) for native-agent fallback and stop on reported scope violations.

## Input

- Bounded task, exact source paths, and acceptance criteria.
- Optional: a prior ledger and its run ID.

## Workflow

1. **Freeze the baseline.** Detect gates using `rig_gate` with `action: "detect"`. If there is no configured gate, pass `gate_exemption` naming the reason and manual substitute at start. Start a run with `rig_evidence` (`action: "start"`, `skill: "rig-challenge"`, and `paths` naming the exact target source files) and record the exact source and acceptance criteria.
2. **Round 1 — independent challenge.** Dispatch `rig-challenger` with the complete source and criteria. The challenger returns structured findings: invariant, counterexample, severity, and file reference. The parent does not challenge its own work. Ensure correctness is covered (the challenger must examine logic/control flow).
3. **Validate.** Check each finding against the source. Record a stable signature: file plus normalized invariant. Use `rig_loop` with `action: "start"` and `reviewer` naming the independent challenger; the baseline round's weighted total is `W0`. If `W0` is zero, print the returned progress table and proceed directly to step 8.
4. **Fix.** Address validated findings only. One writer per path. Refresh the run scope with `rig_evidence` (`action: "refresh"`, `message` giving the reason) after the last fix, then run configured gates.
5. **Re-assess.** A fresh challenger receives the prior findings, the complete required source, and the changed paths. It marks resolved signatures `fixed` or `rejected`, retains unresolved signatures as `open`, and may report new signatures.
6. **Score and decide.** Append the round with `rig_loop` (`action: "append"`, `reviewer`, and every prior signature assessed). Score per [Convergence](../../references/convergence.md), using the tool's decision.
   - Strictly decreasing total: continue or stop as converged.
   - Equal total: plateau; stop.
   - Increasing total: non-converging; stop.
7. **Print progress.** After each validated round, print the cumulative table returned by `rig_loop` per [Convergence](../../references/convergence.md).
8. **Run configured gates.** Use `rig_gate` (`action: "detect"`), then run all configured gates with `action: "run"`, even when the baseline has no findings and no fixes were made. When no gate is configured, perform the recorded manual substitute. If there is neither a configured gate nor a recorded exemption, report the missing gate and stop; do not claim completion. If any gate fails, do not finalize the loop; treat the failure as a finding, fix it, refresh the scope with `rig_evidence` (`action: "refresh"`, `message` giving the reason), and return to step 5 for an independent reassessment before rerunning gates.
9. **Finalize.** Record the terminal stop reason with `rig_loop` (`action: "finalize"`, `reviewer`, `stop_reason`, and a `note` for authority, drift, or unavailable-reviewer stops); `reviewer` may be omitted for an unavailable-reviewer stop when no independent reviewer was available. Recording a stop does not complete the run. Call `rig_evidence` with `action: "finalize"` only for convergence backed by the required review and gates. Security or critical residue blocks completion; any stop other than convergence leaves the run blocked and requires a replacement run rather than refresh to resume work.

## Rules

- For every challenger dispatch, use a native subagent when installed, otherwise `rig_dispatch` with `role: "rig-challenger"`, the source paths, task, and `read_only: true`.
- Use only configured `maxLoopRounds` for round-limit stops per [Convergence](../../references/convergence.md).
- A new finding never resets prior signatures or history.
- Do not reclassify a finding as fixed without independent assessment.
- Scope changes retain the parent stop and mark unassessed prior findings as unassessed.

## Output

Report the run ID, weighted series, stop reason, unresolved signatures, gate receipts, and remaining risks.
