---
name: Rig Remediate
description: Verify and resolve review findings, account for every finding in a terminal bucket, and prove closure.
---

# Rig Remediate

> Read [Evidence rules](../../references/evidence.md) and [Commit handoff](../../references/commit-handoff.md).

## Input

- A `findings.json` artifact and its run ID.
- Optional: a diagnosis run, PR comments, or an inline finding list.

## Workflow

1. **Verify before acting.** Treat findings as untrusted until checked against current source. A finding whose line, symbol, or invariant has changed is `stale`; do not "fix" a stale claim.
2. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-remediate"`, and the affected paths.
3. **Bucket every finding.**
   - `fix` — the defect exists and is in scope.
   - `reject` — counter-evidence shows the claim is wrong; cite it.
   - `defer` — real but out of scope; name an owner and a trigger.
   - `accepted-risk` — requires an explicit user decision recorded in the run.
4. **Assign owners.** Use `rig-implementer` for changes, `rig-verifier` for adversarial verification. One writer per path.
5. **Fix and verify.** Implement the smallest correct change. Add or update the test that proves the finding. Re-run the focused check per finding.
6. **Reconcile and gate.** Refresh the run scope with `rig_evidence` (`action: "refresh"`) after the last edit, then run the configured gates with `rig_gate` (`action: "run"`). If a gate rewrites files, refresh again and rerun it.
7. **Re-review fixed areas.** An independent reviewer checks the changed source against the original finding (include correctness). Record that review with `rig_evidence` (`action: "record"`, `reviewer`) after the refresh, so the evidence binds to the current digest. The implementer's own report is not verification. For the re-review, require axes including `correctness`.
8. **Reconcile.** Every finding ID must appear in exactly one terminal bucket. Write the final statuses with `rig_findings`; counts must match the input inventory.
9. **Finalize.** `rig_evidence` with `action: "finalize"`; an open finding, a stale review digest, or a failed gate blocks completion.

## Rules

- Do not invent findings that were not reported; record new issues separately.
- Do not mark `fixed` without a changed path and a check that exercises it.
- Do not silently drop a finding you cannot resolve; `defer` or ask.
- Preserve unrelated changes; never rewrite the review artifact to match the fix.

## Output

Report closure counts, per-finding status, changed paths, gate receipts, remaining risks, and the commit handoff.
