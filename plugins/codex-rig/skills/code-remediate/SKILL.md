---
name: code-remediate
description: Collect and resolve selected findings in one pass, then verify; bare PR targets use online items, while PR +review authorizes matching review evidence and any needed review step.
---

<!-- ../../assets/images/code-remediate-arch.svg diagrams the routes, gates and parallel write lifecycle. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, route choice, or child fan-out. -->

<!-- policy-sibling: skills/assess/SKILL.md, skills/release/SKILL.md, skills/code-review/SKILL.md, skills/code-remediate/references/intake.md, skills/code-review/references/source-and-scope.md, skills/code-remediate/references/existing-conflict.md -->

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Code Remediate

Apply the shared [Proportional Execution](../../shared/native-skill-contract.md#proportional-execution) policy to bounded analysis or implementation delegated within this workflow; specialized source selection, protected-state, lifecycle, and verification gates remain authoritative.

Code Remediate is a single pass: collect findings, resolve the selected work, obtain one bounded independent challenge of any implemented change, verify, and report. No convergence score, loop ledger, or score-based stop applies to ordinary remediation. Single pass limits workflow expansion, not necessary debugging, regression repair, or rerunning a failed configured check within the authorized scope.

When a worktree delivery holds a commit the run created, show the final handover first, then open the standard **Approve** / **Deny** push question as the final gate without waiting for a request. When the user asks to publish the PR changes, open the standard **Approve** / **Deny** push question as soon as the [publication gate](../../shared/native-skill-contract.md#pr-worktree-publication-gate) has verified the eligible commit and destination. Honor the request in the initial instructions or a later message. For an already completed delivery, enter the gate directly from its promoted run. Existing PR worktrees use the same question.

Challenge Resolve is opt-in: a Code Remediate invocation alone does not authorize the `challenge-resolve` skill or an iterative adversarial review-and-fix loop. The bounded independent plan or implementation challenge required by Proportional Execution is not that loop. Enter the separate iterative workflow only when the user explicitly requests it. Do not propose or ask approval for an unsolicited loop. Existing explicit approval for the same scope remains valid; do not ask again. Only an explicitly requested loop reads `../../shared/adversarial-loop.md` and applies its convergence and stop rules; afterward resume remediation's selection, implementation evidence, and normal completion gates. An unavailable recovery route remains explicitly blocked rather than bypassed.

Selecting all findings sets the scope of this pass. A joined `+review`, a request for one more review, a gate retry, or a resumed run keeps its own scope. Start Challenge Resolve only when the user names that workflow or asks to repeat independent review and fixes, for example until reviewers find no remaining issues.

See the [fixed recurrence and root-cause policy](../../shared/native-skill-contract.md#recurrence-and-root-cause-policy) and [reasoning-progress escalation policy](../../shared/native-skill-contract.md#reasoning-progress-escalation) for repeated-obstacle handling; record and validate `reasoning-progress.json` before another cycle after escalation trigger.

Newly discovered in-scope defects do not trigger another review wave or score-based permission request. Resolve feasible work within the authorized scope using source evidence and focused checks; record genuinely separate or unfinished work with evidence, owner, and next action. Ask only for a concrete missing decision or material scope expansion. Never claim unresolved selected findings are closed.

Apply [Worktree lifecycle](../../shared/native-skill-contract.md#worktree-lifecycle) before collection. Capture the original invoking checkout. In PR mode the default is `gh pr checkout` in that checkout. When local commits on a fork branch prevent exact-head checkout, retain the completed review, show the local-versus-remote differences, and guide the user through freeing the local name. Follow [Source recovery](references/source-recovery.md). The optional `--worktree` flag requests a separate linked Git worktree, runs the same native PR checkout there, and retains the branch and finished changes there for the user. Announce both paths and the intended destination. Linked worktrees share branch names with the invoking checkout, so a branch collision must be resolved there. Run ordinary project checks under current host grants on every PR origin. Inspect the command and its current executable inputs when review evidence is unavailable or stale. Resolve suspicious code within the selected scope before running it. Carry an explicit `--sandbox` through nested review, direct tests, retries, resume and final gates. Use the enforced host boundary under [Sandboxed Test Runs](../../shared/native-skill-contract.md#sandboxed-test-runs) when it supplies the required confinement. For independent confinement, pass `--sandbox-gate <gate>` for each affected command, or `--sandbox` for an explicit workflow-wide request. A command that cannot run with the requested confinement stays unexecuted. A detached Code Review worktree belongs to its review.

Keep report and reviewer setup subordinate to selected finding closure under the primary-goal rules in Reasoning-Progress Escalation. After selection and root-cause evidence establish an authorized fix, implement it and its regression before another review-preparation cycle. Diagnose an agent-owned setup failure once and attempt one bounded repair; if it fails, stop that auxiliary route and continue safe primary work. Preserve missing review coverage as a factual limitation; do not ask the user to debug reviewer names, commands, or metadata.

## Input Schema

```json
{
  "findings_source": "optional path, explicit list, or review for the current-session assessed review; +review uses the newest matching PR review and performs any needed review recovery or fresh review; +report/report/latest request existing evidence only; omit with a bare PR target to use current online review items",
  "mode": "optional report|pr|auto; default auto: pr for bare number, #number, PR URL, or pr_target; otherwise report, which also carries local supplied findings",
  "target": "optional shorthand target number, issue/PR URL, path, or current branch",
  "pr_target": "optional PR number, PR URL, or current branch PR when mode=pr",
  "sandbox": "optional boolean; --sandbox explicitly requires sandboxed code execution for this invocation, including nested review, targeted tests and final gates",
  "worktree": "optional boolean; --worktree requests linked-worktree delivery for a PR; default false",
  "remediation_scope": "optional all|required|suggestions|critical|high|medium|low|comma-separated severities|comma-separated selection indexes; ask before editing when omitted",
  "target_scope": "optional path/module; derive from the admitted target and selected findings when omitted",
  "done_when": "selected findings are fixed/resolved and unselected critical/high findings are explicitly deferred"
}
```

## Workflow

Follow these phases in order. Load only the current phase and triggered reference sections; do not concatenate all phase references. If a read is truncated, retrieve the missing applicable section before acting on it. Resume at the first unmet checkpoint, retaining valid evidence and prior authorization. Before admitting the target and evidence, read [Fail-fast Rules](references/completion.md#fail-fast-rules) and [Quality Gates](references/completion.md#quality-gates); these rules apply throughout. Read [Output Contract](references/completion.md#output-contract) before any status or final response and [Calibration Hooks](references/completion.md#calibration-hooks) when changing the corresponding behavior. A genuine stop or denial remains authoritative.

### Admit target and evidence

When following a completed PR review, apply the [reviewed execution decision](../../shared/native-skill-contract.md#contributed-code-execution) before executing code. The invocation’s explicit `--sandbox` always wins, including when `metadata.code_safety` says safe at the same SHA. An authenticated `ordinary` decision covers the verified inputs. An `inspect` decision requires fresh inspection of this command. Reassess after a fix or merge and confine a command whose concrete execution risk persists. Honor any retained integrity hold first.

For fresh PR remediation and resumption of ordinary source work, follow [Review checkout scheduling](../../shared/native-skill-contract.md#review-checkout-scheduling) after PR State Preflight and before the first run or checkout write. For in-place delivery, acquire the checkout writer claim before the PR target claim and retain it through delivery. Linked-worktree delivery uses its own checkout. Claim the canonical PR for ordinary work. The resumed source-drift exception acquires the applicable claims before its guarded pull. For protected operations, follow the shared scheduling procedure to exchange the target claim for repository-wide exclusivity and revalidate on admission. Only Code Remediate invoked with the explicit `+review` argument can start or resume Code Review. On resume, retain that same invocation and target. Its nested Code Review inherits the parent’s owner and current claims. Only the acquiring parent transitions or releases them after writers finish or records the required inspection hold.

Read [intake.md](references/intake.md). For an unfinished report from the same verified in-place or linked delivery, apply intake's local report-only recovery before PR State Preflight. Recover checkout ownership and reverify delivery under the slot before any handoff write. Otherwise preflight the PR before creating a run or looking up evidence; normalize input and admit exact requested evidence under the documented source and freshness rules. For `+review` with missing, incomplete, stale, or unstarted review evidence, invoke the owning Code Review workflow using the already authorized update, resume, or fresh review route, subject to its actual permission, independence, retry, and recovery boundaries. Admit its exact returned result through the normal validator, then continue this remediation. An incomplete checkpoint, successful collection, or completion of Code Review alone is not a terminal remediation outcome. `+report` and explicit report paths do not authorize a new review; preserve their decision and stop boundaries.

### Collect and select findings

Read [selection.md](references/selection.md). Collect verified source and online items as applicable, establish target intent and resolve authorized integration, normalize findings, and obtain the required resolution scope before edits. Bare PR remediation checks online claims against current source even when their recorded revision is older; age alone requires no review-choice question. Preserve the documented source and selection decisions, including the explicit available-findings choice where required.

### Implement and verify

Read [implementation.md](references/implementation.md). Build the work bucket plan, implement selected source-confirmed work, verify closure and run the required shared quality gates. Record unresolved findings and retain evidence for each outcome. Do not treat report or reviewer preparation as a substitute for selected fixes and their regression checks.

### Finalize, commit, and deliver

After the preceding applicable execution and verification checkpoints, or a genuine documented stop, read the finalization and delivery procedures in [completion.md](references/completion.md). Validate the result artifact, apply the selected commit decision and worktree delivery requirements, and report actual outcomes and limits. Its throughout rules remain active from entry. Incomplete reporting cannot replace an available authorized execution step.
