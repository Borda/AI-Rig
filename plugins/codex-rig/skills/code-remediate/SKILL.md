---
name: code-remediate
description: Collect and resolve selected findings in one pass, then verify; bare PR targets use online items, while PR +review authorizes matching review evidence and any needed review step.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Code Remediate

Apply the shared [Proportional Execution](../../shared/native-skill-contract.md#proportional-execution) policy to bounded analysis or implementation delegated within this workflow; specialized source selection, protected-state, lifecycle, and verification gates remain authoritative.

Code Remediate is a single pass: collect findings, resolve the selected work, obtain one bounded independent challenge of any implemented change, verify, and report. No convergence score, loop ledger, or score-based stop applies to ordinary remediation. Single pass limits workflow expansion, not necessary debugging, regression repair, or rerunning a failed configured check within the authorized scope.

Challenge Resolve is opt-in: a Code Remediate invocation alone does not authorize the `challenge-resolve` skill or an iterative adversarial review-and-fix loop. The bounded independent plan or implementation challenge required by Proportional Execution is not that loop. Enter the separate iterative workflow only when the user explicitly requests it. Do not propose or ask approval for an unsolicited loop. Existing explicit approval for the same scope remains valid; do not ask again. Only an explicitly requested loop reads `../../shared/adversarial-loop.md` and applies its convergence and stop rules; afterward resume remediation's selection, implementation evidence, and normal completion gates. An unavailable recovery route remains explicitly blocked rather than bypassed.

See the [fixed recurrence and root-cause policy](../../shared/native-skill-contract.md#recurrence-and-root-cause-policy) and [reasoning-progress escalation policy](../../shared/native-skill-contract.md#reasoning-progress-escalation) for repeated-obstacle handling; record and validate `reasoning-progress.json` before another cycle after escalation trigger.

Newly discovered in-scope defects do not trigger another review wave or score-based permission request. Resolve feasible work within the authorized scope using source evidence and focused checks; record genuinely separate or unfinished work with evidence, owner, and next action. Ask only for a concrete missing decision or material scope expansion. Never claim unresolved selected findings are closed.

Apply [Worktree lifecycle](../../shared/native-skill-contract.md#worktree-lifecycle) before collection: capture the original invoking checkout and announce any temporary worktree and intended return destination. Review isolation does not make remediation worktree-only; absent an explicit worktree-only request, finished changes belong in the invoking checkout.

Keep report and reviewer setup subordinate to selected finding closure under the primary-goal rules in Reasoning-Progress Escalation. After selection and root-cause evidence establish an authorized fix, implement it and its regression before another review-preparation cycle. Diagnose an agent-owned setup failure once and attempt one bounded repair; if it fails, stop that auxiliary route and continue safe primary work. Preserve missing review coverage as a factual limitation; do not ask the user to debug reviewer names, commands, or metadata.

## Input Schema

```json
{
  "findings_source": "optional path, explicit list, or review for the current-session assessed review; +review uses the newest matching PR review and performs any needed review recovery or fresh review; +report/report/latest request existing evidence only; omit with a bare PR target to use current online review items",
  "mode": "optional report|pr|auto; default auto: pr for bare number, #number, PR URL, or pr_target; otherwise report, which also carries local supplied findings",
  "target": "optional shorthand target number, issue/PR URL, path, or current branch",
  "pr_target": "optional PR number, PR URL, or current branch PR when mode=pr",
  "remediation_scope": "optional all|required|suggestions|critical|high|medium|low|comma-separated severities|comma-separated selection indexes; ask before editing when omitted",
  "target_scope": "required path/module",
  "done_when": "selected findings are fixed/resolved and unselected critical/high findings are explicitly deferred"
}
```

## Workflow

Follow these phases in order. Load only the current phase and triggered reference sections; do not concatenate all phase references. If a read is truncated, retrieve the missing applicable section before acting on it. Resume at the first unmet checkpoint, retaining valid evidence and prior authorization. Before phase 01, read [Fail-fast Rules](references/completion.md#fail-fast-rules) and [Quality Gates](references/completion.md#quality-gates); these rules apply throughout. Read [Output Contract](references/completion.md#output-contract) before any status or final response and [Calibration Hooks](references/completion.md#calibration-hooks) when changing the corresponding behavior. A genuine stop or denial remains authoritative.

### 01: Admit target and evidence

Read [intake.md](references/intake.md). Preflight the PR before creating a run or looking up evidence; normalize input and admit exact requested evidence under the documented source and freshness rules. For `+review` with missing, incomplete, stale, or unstarted review evidence, invoke the owning Code Review workflow using the already authorized update, resume, or fresh review route, subject to its actual permission, independence, retry, and recovery boundaries. Admit its exact returned result through the normal validator, then continue this remediation. An incomplete checkpoint, successful collection, or completion of Code Review alone is not a terminal remediation outcome. `+report` and explicit report paths do not authorize a new review; preserve their decision and stop boundaries.

### 02: Collect and select findings

Read [selection.md](references/selection.md). Collect verified source and online items as applicable, establish target intent and resolve authorized integration, normalize findings, and obtain the required resolution scope before edits. Bare PR remediation checks online claims against current source even when their recorded revision is older; age alone requires no review-choice question. Preserve the documented source and selection decisions, including the explicit available-findings choice where required.

### 03: Implement and verify

Read [implementation.md](references/implementation.md). Build the work bucket plan, implement selected source-confirmed work, verify closure and run the required shared quality gates. Record unresolved findings and retain evidence for each outcome. Do not treat report or reviewer preparation as a substitute for selected fixes and their regression checks.

### 04: Finalize, commit, and deliver

After the preceding applicable execution and verification checkpoints, or a genuine documented stop, read the finalization and delivery procedures in [completion.md](references/completion.md). Validate the result artifact, apply the selected commit decision and worktree delivery requirements, and report actual outcomes and limits. Its throughout rules remain active from entry. Incomplete reporting cannot replace an available authorized execution step.
