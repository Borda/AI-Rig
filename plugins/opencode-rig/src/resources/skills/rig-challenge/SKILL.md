---
name: Rig Challenge
description: Run a bounded adversarial convergence loop with independent challengers and a strict-decrease stop rule.
---

# Rig Challenge

> Read [Evidence rules](../../references/evidence.md) and [Convergence](../../references/convergence.md).

## Input

- Bounded task, exact source paths, and acceptance criteria.
- Optional: a prior ledger and its run ID.

## Workflow

1. **Freeze the baseline.** Start a run with `rig_evidence` (`skill: "rig-challenge"`) and record the exact source and acceptance criteria.
2. **Round 1 — independent challenge.** Dispatch `rig-challenger` with the complete source and criteria. The challenger returns structured findings: invariant, counterexample, severity, and file reference. The parent does not challenge its own work. Ensure correctness is covered (the challenger must examine logic/control flow).
3. **Validate.** Check each finding against the source. Record a stable signature: file plus normalized invariant. Use `rig_loop` with `action: "start"` and `reviewer` naming the independent challenger; the baseline round's weighted total is `W0`.
4. **Fix.** Address validated findings only. One writer per path. Refresh the run scope with `rig_evidence` (`action: "refresh"`) after the last fix, then run configured gates.
5. **Re-assess.** A fresh challenger receives the prior findings, the complete required source, and the changed paths. It marks each signature `verified-fixed` or `rejected` and may report new signatures.
6. **Score and decide.** Append the round with `rig_loop` (`action: "append"`, `reviewer`, and every prior signature assessed). Compute the open weighted total using security 20, critical 10, high 6, medium 4, low 2, nit 1.
   - Strictly decreasing total: continue or stop as converged.
   - Equal total: plateau; stop.
   - Increasing total: non-converging; stop.
7. **Print progress.** After each validated round, print the cumulative table: `Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score`, with every numeric cell as `old + new`.
8. **Finalize.** Record the terminal stop reason with `rig_loop` (`action: "finalize"`, `reviewer`, `stop_reason`, and a `note` for authority, drift, or unavailable-reviewer stops) and finalize the run. Security or critical residue blocks finalization; any stop other than convergence leaves the run blocked.

## Rules

- No arbitrary round cap replaces the stop rule; `maxLoopRounds` is a safety ceiling, not a target.
- A new finding never resets prior signatures or history.
- Do not reclassify a finding as fixed without independent assessment.
- Scope changes retain the parent stop and mark unassessed prior findings as unassessed.

## Output

Report the run ID, weighted series, stop reason, unresolved signatures, gate receipts, and remaining risks.
