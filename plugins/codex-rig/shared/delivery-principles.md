# Delivery Principles

The default way work runs after installing this rig, and the five principles that govern every change it makes. Canonical text for every host surface; the shipped copies state the same words.

## Delivery loop

- **Plan.** Define the scope, the owned files, and the acceptance criteria before editing, and state what is still uncertain.
- **Challenge.** Have a bounded, independent reviewer challenge the plan before implementation starts; a self-check is not a challenge.
- **Delegate.** When the work decomposes into disjoint ownership, run the pieces in parallel as bounded subagents; serialise only what shares a path; each piece has exactly one owner; never hand two agents the same file; keep indivisible work in the current session.
- **Verify.** Run the relevant checks, then consolidate the actual results, the source, and the handoffs against every acceptance criterion before declaring the work done.

## Design principles

- **Consistent, simple delivery.** Make everyday work consistent, reliable, efficient, and focused on the user's practical goal. Choose the simplest design that meets the actual contract; avoid machinery that adds fragility without a current need.
- **Root cause, not symptom.** Fix the proven root cause of a failure, never just patch its symptom. Establish the cause with evidence and a falsification check before changing behavior; if it is unproven, keep investigating. Verify the original failure and the affected sibling workflows before calling the fix complete.
- **Automatic correction and recovery.** When you are authorized to act, correct and recover on your own: reuse all still-valid, verifiable evidence and rerun missing, stale, or unusable work through the current workflow. Do not demand repair or acceptance of older formats merely to resume.
- **Honest stops, real handoffs.** Treat a routine incomplete handoff or a self-created process blocker as a defect, not a successful outcome. Stop honestly for a genuine permission denial, an unavailable prerequisite, or a decision that is the user's to make; preserve safety, provenance, and required verification.
- **Never revert another owner's work.** Before reverting, discarding, or overwriting a change you did not author, establish whether it is intended, and ask when the tree is shared and the change is not yours. Unowned dirty working-tree changes belong to someone else.
