### 06: Build And Approve The Work Bucket Plan

> Only when evaluating or executing `parallel-specialists`, read [parallel-lifecycle.md](parallel-lifecycle.md) before freezing its plan; parent-owned and sequential routes do not load it. The reference preserves production lifecycle's containment, dispatch-record, verification, reconciliation, rollback, and cleanup obligations.

Before any selected-scope edit or specialist spawn, write `<run-directory>/resolution-workplan.md` sections:

- `## Work Bucket Plan`: one row per bucket with selected indexes, role, owned files/evidence, dependencies, and checks; identify intentionally shared files.
- `## Parallel Approval`: parallel feasibility, exact plan digest, dispatch source/status, and actual execution mode. For an ineligible parallel plan using the workflow-default parent-only route, include exactly one nonempty `Ineligibility reason: <reason>` line tied to this plan and available runtime. The validator rejects common placeholders; the parent must verify the reason against available evidence. Do not ask the user to approve parent-owned or sequential execution.
- `## Execution Order`: dependency-aware bucket order and `parent-owned|sequential-specialists|parallel-specialists` mode.
- `## Ungrouped Items`: always `none`; every selected item belongs to exactly one bucket, including parent-owned work.

Before the first selected-scope edit, record `<run-directory>/commit-baseline.json`: current `HEAD`, the parsed path/status inventory from raw `git status --porcelain=v1 -z`, and the exact paths planned for each work bucket. This is an ownership boundary for an optional later commit, not a requirement for remediation. Unrelated tracked or untracked changes, including a lockfile such as `uv.lock`, do not block remediation or PR collection unless checkout would overwrite that exact path. A path already changed at the baseline is never automatically commit-eligible; retain it unstaged and do not restore, stash, reset, or otherwise hide it. Exclude run artifacts under `.reports/` from every commit unit.

Late baseline capture is an agent process error. Current dirtiness alone proves neither pre-existing user edits nor agent ownership. Preserve the late capture unchanged and record `commit-ownership-recovery.md`: identify the first edit, retained pre-edit status and source snapshots, exact baseline HEAD/branch, affected paths, and the complete agent edit sequence through current bytes. Recover a separate pre-edit `commit-baseline.json` from those contemporaneous records only when they prove each candidate path was initially clean and every subsequent edit is task-owned; cite the preserved late capture and recovery in the commit plan. If that name already holds the late capture, retain it byte-exact under a distinct evidence path before writing the recovered baseline. Never infer ownership from memory, current `HEAD` or dirty status; never fabricate an empty baseline. Absent or disputed evidence leaves affected paths unstaged with the precise ownership gap, while other authorized checks and closeout continue. This recovery supplies ownership proof only; it grants no commit authorization and waives no verification or destination check.

Also write `<run-directory>/work-bucket-plan.json` with exact `work_buckets` array used to render user-visible table. Use `schema_version=1` for parent-owned, sequential, and planning-only work. Use `schema_version=2` from outset when proposing production `parallel-specialists`; include consumer, source repository relative to its workspace parent, a supported isolated worktree root, exact baseline HEAD/tree, rollback and cleanup policies, context hashes, resource locks, output paths, and verification commands required by loaded production lifecycle reference. Use an existing sibling `.codex-rig-worktrees/<run-id>` root or prefer source-local `.reports/codex/code-remediate-worktrees/<run-id>` when it is Git-ignored and resolves to the source repository or one child directory. Reject symlinked or escaping roots and pre-existing run roots. Keep plan, dispatch record, state, patches, reconciliation, rollback material, and lifecycle projection in source-local `<run-directory>`. Hash exact plan bytes with SHA-256 and record digest in workplan before dispatch. The table, JSON, metadata, and dispatch record must describe and bind the same plan bytes; never upgrade an already used schema-v1 plan in place.

Group per capable specialist/domain when it reduces duplicated context or preserves one root cause. Valid keys:

- shared affected files/modules/tests/docs/CI workflows
- same closure type: code, tests, docs, CI, security, typing, performance, review-gate, merge/conflict
- same root cause/expected fix
- same verification command/closure evidence
- same merge/conflict collision risk

Group by coherent fix and capable role; do not manufacture a second task or spawn one specialist per finding merely to claim parallelism. A work bucket contains at most five selected items and each selected item appears in exactly one bucket. Parallel buckets may name the same exact repo-relative file when their roles have independent work in isolated worktrees. Reject absolute paths, globs, `..`, duplicate aliases, and ancestor/descendant path overlap. Identify every shared file and the parent reconciliation owner before dispatch. A collision discovered after join stays in the integration worktree until the parent reconciles it and verifies the final postimage; never silently choose a child's version.

Plan parallel dispatch by default after selection:

- Create the fewest coherent role/domain buckets that cover selected items, whether the selection has fewer or more than five items; never split one coherent fix only to reach two buckets.
- Use `parallel-specialists` whenever at least two buckets can work independently in isolated worktrees, including when they share an exact file that the parent can reconcile after join. Do not convert eligible work to parent-owned or sequential because handoff overhead is higher.
- If selected work is indivisible, dependent, or runtime capacity is unavailable, continue with the available parent-owned or sequential route and record the concrete reason, actual mode, and applicable plan status. Put exactly one nonempty `Ineligibility reason: <reason>` line in `## Parallel Approval`, tied to this plan and available runtime. The validator rejects common placeholders; the parent verifies whether evidence supports the reason. Do not fabricate independent writers from dependent implementation, QA, or read tasks. If a production precondition fails, identify the specific unsafe operation and path, evidence of possible damage, and safe isolation alternatives; unrelated dirty paths, a sibling-root inconvenience, handoff overhead, and reconcilable shared-file edits alone are not blockers. Missing parallel capacity is not a reason to stop authorized work.

For an eligible parallel plan, show the complete `## Work Bucket Plan` table and SHA-256 digest before dispatch. Record `source=workflow-default`, `response=approve`, and `prompt_presented=false` against the exact frozen plan bytes; this is a workflow dispatch decision, not a claim of user approval for that specific digest. An existing explicit user choice for the exact plan may instead use `source=explicit-input` or `source=user-prompt`. Only when the upfront packet answer is `show the plan and wait for my approval`, ask `Approve` / `Deny` for the shown digest through User Questions and record `source=user-prompt` with `prompt_presented=true`; this opted-in review is the one plan question allowed mid-run. Do not spawn until the plan, context packs, baseline, and verification preflight pass. For an ineligible plan, record why parallel execution is unavailable and continue authorized work in the available mode without an approval prompt. An explicit user denial still stops the denied route; missing authorization for an unsafe operation still stops that operation.

Write `<run-directory>/parallel-approval.json` with exactly `plan_sha256`, `source=workflow-default|explicit-input|user-prompt|not-required`, `response=approve|parent-only|not-required`, and `prompt_presented`. Parallel dispatch requires `response=approve`, `parallel_approval_status=approved`, and `approved_plan_sha256` equal to current bucket-plan digest. For an ineligible plan, the four-field approval artifact records `response=parent-only`, `source=workflow-default`, and `prompt_presented=false`. In `CODE_REMEDIATE_METADATA.resolution_workplan`, also record `parallel_eligible=false`, `parallel_approval_required=false`, `parallel_approval_status=parent-only`, and `approved_plan_sha256=null`; this records the parent-only route and is not approval for a different or unsafe operation. Historical `not-required` records remain readable through the historical result reader. New `result.candidate.json` validation rejects that legacy dispatch source for selected work; emit the current default or explicit-choice dispatch record before promotion.

For a selection of at most five items, multiple sequential buckets are permitted when `execution_mode=sequential-specialists`, owners include distinct capable roles, and every bucket declares nonempty `grouping_rationale`, `expected_closure`, and `dependencies`. Dependencies name earlier bucket IDs only; use `[]` for no prerequisite. Keep the concrete parallel-ineligibility evidence above. One coherent parent bucket remains the default for indivisible work; artificial same-owner per-finding fan-out remains invalid.

After writing `work-bucket-plan.json` and `parallel-approval.json`, inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/remediation_finalize.py workplan --help` and run its `workplan` action. It writes the `## Work Bucket Plan`, `## Parallel Approval`, `## Execution Order`, and `## Ungrouped Items` sections of `resolution-workplan.md` from those two files and keeps every other section, such as `## Upfront Decisions`; pass `--ineligibility-reason` for a workflow-default parent-only plan. Do not hand-write those four sections or copy the plan, its digest, approval fields, or group counts into metadata: the helper derives them. Author only `parallel_eligible` and `parallel_approval_required` in the workplan metadata. Then run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/remediation_finalize.py preflight --run <run-directory> --stage plan --metadata <metadata-draft.json>` before any selected-scope edit or dispatch. It derives plan copies and checks ownership, exact selection coverage, dependencies, approval digest and rendered plan together. No completed outcome is needed. Parallel work still requires its existing production preflight before dispatch and complete runtime evidence at finalization; the planning check never replaces either gate.

Every bucket row records:

- bucket id
- selected indexes
- severity range
- grouping rationale
- primary owner: `parent|sw-engineer|qa-specialist|doc-scribe|cicd-steward|linting-expert|data-steward|scientist|squeezer|oss-shepherd`
- verifier: `parent|qa-specialist|security-auditor|linting-expert|cicd-steward|challenger|solution-architect|none`
- context pack path
- expected closure evidence
- dependencies or `none`
- owned files/evidence; identify exact files shared by parallel buckets for parent reconciliation
- execution mode: `parent|sequential|parallel`
- execution status: `planned|in-progress|fixed|verified|deferred|unresolved`, recorded only as `bucket` status events and rendered in the table's `Status` column (default `planned`)

When a selected scope has distinct independent role/domain work, use separate buckets even with five or fewer items. A single coherent, indivisible, or dependency-bound bucket continues parent-owned or sequentially with the concrete reason recorded; the parent retains acceptance and source application.

Owner assignment rules:

- implementation/refactor/API: `sw-engineer` primary, `qa-specialist` verifier. A public API/migration concern stays with the parent/session unless the user expressly requests architecture advice or selects `solution-architect`; then it is a bounded read-only verifier/context artifact, never primary, returned to the parent for acceptance.
- test gap/regression proof: `qa-specialist` primary, `parent` verifier.
- docs/changelog/examples/docstrings: `doc-scribe` primary, `parent` or `qa-specialist` verifier for executable docs.
- CI/workflow/release gate: `cicd-steward` primary, `parent` verifier. Permission/secret work stays with the parent/session unless the user expressly requests security advice or selects `security-auditor`; then it is a bounded read-only evidence artifact returned to the parent for acceptance.
- SemVer/compatibility classification, deprecation-cycle correctness, or release-readiness/blocker impact: `oss-shepherd` primary, `parent` verifier; changelog/migration prose stays `doc-scribe`, release automation stays `cicd-steward`.
- lint/type/pre-commit/suppression: `linting-expert` primary, `parent` or `qa-specialist` verifier when runtime could change.
- security/dependency/permission/data exposure: parent/session primary, `challenger` verifier for high/critical/non-obvious closure. Use `security-auditor` only on user-explicit advisory request or role selection, as bounded read-only evidence artifact; it never becomes primary or accepts closure.
- data/ML/research/performance: `data-steward`, `scientist`, or `squeezer` primary; `qa-specialist` verifies tensor/data boundaries.
- review-gate/follow-up: primary role matching missing evidence, `parent` verifier; unresolved when evidence needs unavailable CI, maintainer review, user-deferred external step.
- merge/conflict collision: `sw-engineer` primary; `challenger` verifies critical/high/non-obvious; cite `<run-directory>/merge-prestage.md`.

For each non-parent bucket write narrow `<run-directory>/specialists/<bucket-id>-context.md`: selected bucket only, relevant files/hunks/logs, closure question, stop rule, expected evidence. Omit unrelated findings, full PR discussion, and full review report by default.

Every owner/verifier must be one of enums above. Parent buckets use `execution_mode=parent`; specialist buckets use `sequential|parallel`. Every specialist context-pack path must resolve inside run directory and exist before dispatch.

Record `CODE_REMEDIATE_METADATA.resolution_workplan`: existing group/owner/verifier counts and path; `max_items_per_bucket=5`; `execution_mode`; `bucket_plan_path`, `bucket_plan_sha256`, `parallel_approval_path`, eligibility, approval requirement/status/source/response, prompt flag, `approved_plan_sha256`; plus `work_buckets` with bucket id, selected indexes, owner, owned paths/evidence, execution mode, and any singleton rationale.

### Production Parallel Lifecycle

For `parallel-specialists`, follow loaded lifecycle reference through verification, join, any parent reconciliation, source application, and cleanup before claiming completion. Parent-owned and sequential execution continues with step 07 after recording its ineligibility reason and actual mode.

### 07: Apply Fixes In Selected Scope

Fix selected scope in priority: `critical` -> `high` -> `medium` -> `low`.

In `mode=pr`, complete target-merge conflict resolution and its authorized merge commit before selected report/online-review findings. For parent-owned or sequential execution, fix one selected valid group at a time. For approved production parallel execution, follow the loaded production lifecycle reference and treat the parent join as the barrier before source application. `<run-directory>/resolution-workplan.md` remains the execution ledger. Never edit unselected findings or a selected item outside its assigned group unless the workplan is updated first with the reason and affected closure evidence. After each group, append its closure record to the append-only `<run-directory>/closure-log.md`: write one `### <bucket-id>` block with changed files, verification, and evidence to `<run-directory>/closure-log.md.rec` with the filesystem tool, then run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/remediation_finalize.py append --run <run-directory> --ledger closure-log.md`, which appends it, creates the `## Closure Evidence` heading on first use, and removes the `.rec` file. Never rewrite an earlier closure entry; a correction is a new block. Record the group's status change as a `bucket` status event.

Targeted pytest runs during implementation and verification in steps 07 and 08 follow [Sandboxed Test Runs](../../../shared/native-skill-contract.md#sandboxed-test-runs): add `-p no:xdist` when its conditions hold, so they run inside the sandbox without escalation. After each group's edit, inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/test_targets.py --help`, run it for that group's changed files and run its `pytest_args`, or the group's targeted acceptance check for non-Python scope; never run the full suite per finding or per group. The full suite runs exactly once, at the step 09 gate, with the repository's own settings; a later file change reruns that complete bundle under step 09's rerun rule.

### 08: Challenge Closure Before Full Gates

For every selected fixed finding answer:

- Does original failure still reproduce?
- Could it pass review but remain functionally wrong?
- Which regression check protects it now?
- What risk remains?

Missing closure evidence keeps item unresolved. When the original failure does not reproduce or contradicts the finding, consult [reviewer evidence](selection.md#reviewer-evidence-before-asking) before closing it as rejected.

Append the item's closure evidence to `closure-log.md` through the step 07 append before recording its fixed status event.

Classify how closure happened, not just whether the item is open. An `implemented` item requires this run's finding-specific change and verification evidence. A target integration is not finding implementation: record its commit and upstream files separately, and count a finding fixed by integration only when its exact required behavior and regression evidence are demonstrated. Fresh passing CI may close the corresponding CI obligation without any source change; it cannot close an independent-review or code-behavior obligation. Existing correct behavior, signed agreements, stale notices, and duplicate findings need their own evidence-backed dispositions, not implied patches.

Apply `../../../shared/specialist-orchestration.md` when selected findings cross specialist domain. `<run-directory>/resolution-workplan.md` is source of truth for closure ownership; write `<run-directory>/specialist-closure-plan.md` only for post-fix verification beyond workplan owner/verifier. Context packs stay group-local: selected group, changed files, closure evidence, exact verification question; omit unrelated review items/PR discussion.

Execute already-authorized workplan owner/verifier assignments without another review choice; do not retroactively add an optional reviewer to those assignments. The selected remediation scope authorizes its required local checks. For an implemented change, obtain one bounded independent challenge of the changed result under [Proportional Execution](../../../shared/native-skill-contract.md#proportional-execution), preserving actual host and paid-execution boundaries; this required pass is not an iterative loop. An additional independent review requires the specific user decision in [Review limitations and additional review](../../../shared/quality-gates.md#review-limitations-and-additional-review); `all`, a confidence gap or a failed reviewer launch alone does not authorize it. Joined `+review` authorizes its intake review step; the separate implementation challenge follows the shared proportional contract. An explicitly approved bounded closure review permits at most one closure-verification pass for its stated expectation. After corrections rerun affected executable checks, but do not dispatch another reviewer by default. If the user declines or dismisses another pass, finish with the factual verification limitation and no synthetic finding, blocker, open review obligation or repeated request. Do not claim independent coverage was obtained when it was not. Explicitly requested convergence workflows retain their own authorized loop.

After that authority check, select closure roles by domain below; these triggers alone never authorize dispatch. Before freezing inputs or dispatching an authorized pass, complete step 09's format preparation. These roles do not start a convergence loop:

- `qa-specialist`: bug fix, test gap, regression proof, or behavior `already-applied` claim.
- `security-auditor`: only when user expressly requests that advisory pass or selects that role for auth, credentials, deserialization, dependency/supply-chain, permissions, or data exposure; return its bounded read-only evidence artifact to the parent/session for closure acceptance.
- `cicd-steward`: GitHub Actions, release automation, flaky CI, gate-environment failures.
- `linting-expert`: ruff, mypy, pre-commit, type/lint config, suppression changes.
- `doc-scribe`: public docs, changelog, examples, migration text, public docstrings.
- `data-steward`, `scientist`, or `squeezer`: data/ML/research/performance findings.
- `challenger`: critical/high, conflict resolution, or closure based on non-obvious assumption.

For required or already-authorized verification, if its specialist is unavailable, write labeled in-main substitute evidence in `<run-directory>/specialists`; lower confidence when independence mattered. A substitute cannot satisfy an explicit independence requirement. Never mark selected high/critical resolved only from parent claim when closure evidence depends on required specialist verification. A declined optional pass creates no substitute-review obligation.

Before authorized dispatch, check that the selected reviewer can inspect every required input under its actual route. A no-tools reviewer must receive the relevant source, diff, and evidence inline; artifact paths alone are not readable context. A context-delivery failure is not a substantive review and does not close the item. Preserve the failed attempt, repair the context, and resume within existing authorization, dispatch-wave and recurrence rules; re-plan before another independent pass when required. If no independent route is available for required or already-authorized work, explain the failed proof and offer the permitted parent-sequential alternative without pretending it meets explicitly required independence. Ask only for a genuinely missing route or scope decision.

Missing coverage details are an investigation checkpoint, not proof that no fix is possible. Inspect existing coverage artifacts and the project's configured focused coverage command; run authorized reproducible checks and compare missing branches with the selected finding before proposing tests. Obtain external details only through the approved read boundary. Do not add speculative tests or declare missing coverage resolved from green CI alone. If usable evidence remains unavailable after permitted recovery, record the attempted commands, specific missing capability or data, owner, and smallest next action; resume the selected finding when that checkpoint is repaired instead of restarting or silently abandoning it.

### 09: Run Shared Quality Gates

Before freezing selected changed-file inputs, issuing mutation receipts, or dispatching independent closure review, run the configured format hook on every selected changed file, including tests. If formatting changes a file or the hook fails, resolve it and rerun that same hook until clean, then freeze the inputs and receipts. Run the canonical gate bundle afterward; this preparation does not replace its format result. Any later file change requires refreshed review inputs and a complete bundle rerun, with no skipped applicable checks or rewritten receipts.

Inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gates.py --help`; run every project-relevant closure gate with explicit command/skip reason.

Configured-hook failures follow [Hook Failure Recovery](../../../shared/commit-response-template.md#hook-failure-recovery), including equivalent PR/target checks, root-cause evidence, authorized repair and revalidation. A known failing hook blocks a commit, not permitted investigation and repair; retain scope, recurrence and actual capability boundaries.

Independent closure dispatch follows step 08's authority boundary; these gates do not authorize another review pass.

Keep the repository's configured test command unchanged; the targeted-run `-p no:xdist` rule never applies to this gate. The pinned test-runner approval does not cover the gate runner: when the test gate needs escalated pytest, request a one-time escalation for the complete `run_gates.py` command without `prefix_rule`, as [Sandboxed Test Runs](../../../shared/native-skill-contract.md#sandboxed-test-runs) defines.

### 10: Write Unresolved Findings

Write unresolved findings to `<run-directory>/unresolved.txt`.

When selected items remain unresolved, distinguish fixed work from still-needed process/environment/CI/external-review action. Include:

- `Unresolved Work Summary`: selected total/resolved/unresolved; local actionable unresolved; process/gate unresolved; environment blocked; external-owner blocked; whether all local code/doc findings closed.
- `Why Selected Items Remain Unresolved`: one row/unresolved selected item or reason group: selected indexes, severity, closure class, status, reason, attempted evidence, next owner/action.
- `Next Action`: smallest concrete action closing each unresolved reason group.

Use closure classes: `local-code-or-doc`, `process-gate`, `independent-review`, `environment-blocked`, `external-ci`, `user-deferred`, `already-closed`, `other`. With `remediation_scope=all`, never say "resolved all" while selected item unresolved. Say "all local actionable findings are closed" only when local code/doc closed; separately list remaining selected gate/process obligations.
