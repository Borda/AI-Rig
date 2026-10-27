---
name: Rig Review
description: Review the current diff, a commit, or a path with independent read-only reviewers and a validated findings artifact.
---

# Rig Review

> Read [Evidence rules](../../references/evidence.md). This workflow is read-only; it never writes to GitHub.

## Input

- `workspace` (default): staged and unstaged changes.
- `path <path>`: one file or directory.
- `commit <rev>`: a committed range.
- `pr <number|url>`: optional PR mode when `gh` is authenticated.

## Workflow

1. **Classify.** `TRIVIAL`, `LOCAL`, `BROAD`, or `HIGH_RISK`. Risk categories set depth; they do not grant execution authority.
2. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-review"`, and the exact target paths.
3. **Collect exact source.** Record the diff and the complete bytes of every reviewed file. For `workspace`, prefer a detached review worktree so later edits cannot silently change the evidence. A digest change invalidates the review.
4. **Select axes.** `correctness` is mandatory and must be run (cannot be omitted). Add `security` for auth, credentials, deserialization, or network input; `tests` when behavior changes; `api` for public interfaces; `performance`, `docs`, and `data` when the diff warrants. Cap at four axes below `BROAD`. Also ensure coverage records what was actually inspected for correctness.
5. **Dispatch independent reviewers.** Use `rig-reviewer` (or `rig_dispatch`). Give each only the task, role card, exact source digests, and assigned axis. Reviewers are read-only and return JSON findings; the parent never counts its own reading as reviewer evidence.
6. **Validate findings.** Reject any finding without a concrete file and observed evidence. De-duplicate by signature (file + invariant), not by wording. Use `rig_findings` with `action: "write"`, the axes, the coverage, and `reviewer` naming the independent reviewer(s). That write records the review evidence bound to the scope digest.
7. **Report.** Order by severity. State the axes that ran, the axes that did not, and why. An honest partial review beats a fabricated complete one.
8. **Finalize.** `rig_evidence` with `action: "finalize"`. Open findings are a valid review outcome; they are not a completion failure. Source drift is: start a new review instead of certifying changed bytes.

## Rules

- Never write to the repository or GitHub during review.
- Do not fix findings here; hand them to `rig-remediate`.
- A finding must cite source that was actually read; pattern-matched suspicion is labeled as suspicion.
- If an independent reviewer is unavailable, say so and cap confidence; do not substitute the parent's own analysis.

## Output

Report run ID, scope digest, axes run and skipped, findings by severity, coverage limits, and confidence.
