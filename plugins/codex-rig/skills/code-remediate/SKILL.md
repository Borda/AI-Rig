---
name: code-remediate
description: Apply selected review fixes; bare PR targets use current online items, while PR +review adds the latest matching artifact.
---

# Code Remediate

See the [fixed recurrence and root-cause policy](../../shared/native-skill-contract.md#recurrence-and-root-cause-policy) and [reasoning-progress escalation policy](../../shared/native-skill-contract.md#reasoning-progress-escalation) for repeated-obstacle handling; record and validate `reasoning-progress.json` before another cycle after an escalation trigger.

Run linear code remediation to close findings.

## Input Schema

```json
{
  "findings_source": "optional path, explicit list, review for the current-session assessed review, or +review/+report/report/latest to auto-select the newest matching PR review report; omit with a bare PR target to use current online review items",
  "mode": "optional report|pr|auto; infer pr for bare number, #number, or PR URL",
  "target": "optional shorthand target number, issue/PR URL, path, or current branch",
  "pr_target": "optional PR number, PR URL, or current branch PR when mode=pr",
  "remediation_scope": "optional all|critical|high|medium|low|comma-separated severities|comma-separated selection indexes; ask before editing when omitted",
  "target_scope": "required path/module",
  "done_when": "selected findings are fixed/resolved and unselected critical/high findings are explicitly deferred"
}
```

## Workflow (Exact Commands)

### 01: Create Run Directory

Run `create_run.py --skill code-remediate` per `../../shared/helper-cli-contract.md`.

### 02: Normalize input and optional report findings

Shorthand rules:

- Canonical in-session report: `$code-remediate review` => `mode=report`, `REQUESTED_REPORT=true`, `FINDINGS_SOURCE=latest-assessed-current-session-review`. It resolves to the latest assessed `code-review` result created in the current session. Reuse the exact prior artifact path recorded in this session; do not scan reports or infer a PR target. Do not collect PR evidence or fetch online review comments. If no assessed current-session review result is available, fail with `current-session-review-report-required` and instruct the user to run `$code-review <target>` first or supply a report path.
- Canonical online-only PR: `$code-remediate #123` => `mode=pr`, `PR_TARGET=123`, `REQUESTED_REPORT=false`, `FINDINGS_SOURCE=none`. Accepted bare PR forms are: bare number, `#number`, PR URL, and natural-language bare PR targets; they collect current online items and verified local checkout without a prior review report.
- Natural-language online-only aliases: `remediate 123`, `remediate #123`, `remediate PR 123`, and `remediate <github-pr-url>` use the same bare-PR route.
- Canonical report-backed PR: `$code-remediate #123 +review` => `mode=pr`, `PR_TARGET=123`, `REQUESTED_REPORT=true`, `FINDINGS_SOURCE=latest-matching-review-report`.
- `matching-review-incomplete:<run-directory>` means an identified review retained notes but never produced a promoted result or candidate. Lead with `Review handoff blocked`, link that retained run, and explain that preliminary evidence exists but the review did not complete. Return to the producer completion checkpoint; do not claim no review was performed, consume notes as a validated result, select an older verdict, or switch to online-only intake. This applies across sessions as well as within one session. A newer malformed result similarly blocks stale assessed fallback.
- Compatibility alias: `$code-remediate #123 +report` => `mode=pr`, `PR_TARGET=123`, `REQUESTED_REPORT=true`, `FINDINGS_SOURCE=latest-matching-review-report`; `$code-remediate #123 +report compatibility alias` has same report lookup.
- Natural-language aliases: `remediate 123 report`, `remediate #123 report`, and `remediate PR 123 report` => `mode=pr`, `PR_TARGET=123`, `REQUESTED_REPORT=true`, `FINDINGS_SOURCE=latest-matching-review-report`.
- `remediate <github-pr-url> report` => `mode=pr`, `PR_TARGET=<github-pr-url>`, `REQUESTED_REPORT=true`, `FINDINGS_SOURCE=latest-matching-review-report`.
- An explicit review result path combined with a PR target sets `REQUESTED_REPORT=true`; a bare PR target has no implicit report path.
- Bare PR and report-backed PR routing are distinct: explicit `+review`, `+report`, report aliases, and report paths retain report-plus-online behavior; absence of a report source selects online-only intake and never falls back to report lookup.
- If `+review`, `+report`, `report`, `latest`, `latest-report`, or `review-report` replaces a path, find the newest matching result across canonical `.reports/codex/code-review/pr-<number>/run-<NNN>/result.json` and legacy flat `.reports/codex/code-review/<timestamp>/result.json` artifacts whose sibling `pr.json` has the same PR number/URL as `PR_TARGET`.
- When `REQUESTED_REPORT=true`, no matching code-review report => fail with direct instruction to run `$code-review <target>` first or supply report path. A `matching-review-unavailable-rerun-code-review` result means PR collection failed before any assessed review; do not use it as findings input, and rerun `$code-review <target>` after resolving the collection failure. A `matching-review-closed-not-remediable` result is a terminal close disposition with no source findings; do not remediate it or fall back to an older assessed report. A `matching-review-candidate-unpromoted:<path>` result requires the bounded same-session recovery below; do not fall back to an older assessed report.
- When canonical matching PR runs exist, select the greatest parsed numeric `run-<NNN>` index. Otherwise select the greatest parsed legacy flat timestamp. Never rely on lexical glob order, modification time, or directory traversal order; record the selected path in `<run-directory>/findings-input.txt`.

When `FINDINGS_SOURCE=latest-matching-review-report`, inspect `python PLUGIN_ROOT/shared/find-review-report.py --help`, resolve `PR_TARGET` against `.reports/codex/code-review`, and assign the printed path to `FINDINGS_SOURCE`. The helper searches explicit canonical nested PR runs plus legacy flat timestamped runs; no migration is required. It filters explicit `review_status=unavailable` diagnostics, so an older assessed review remains eligible when a newer collection failure exists. A newer `review_status=closed` result instead blocks older findings because the close disposition is current and non-remediable. Before accepting an explicit review result path as findings input, invoke the same helper with `--result <path>`; it rejects unavailable results with the rerun instruction, closed results with `matching-review-closed-not-remediable`, and candidate paths with `matching-review-candidate-unpromoted:<path>`. A bare PR target must not run this helper, scan prior review reports, or require a `code-review` artifact.

For `matching-review-candidate-unpromoted:<path>`, recover only when the candidate's `specialist-manifest.json` names the same parent thread as the current remediation session. Run the review-specific validator, then the shared validator, against that exact candidate and its review run directory; promote it to `result.json` only after both validators pass, then rerun the finder and use the promoted result. Never consume `result.candidate.json` directly. If either validator fails, persist its exact stderr code in `<run-directory>/review-candidate-validation.txt`, including `manifest-invalid-attempt-count:<role>` when applicable, and return to the code-review manifest preflight checkpoint for one evidence-preserving repair from retained specialist and rollout records. Never invent missing attempt provenance or retry a specialist for artifact bookkeeping. After a repaired manifest passes `--manifest-only`, rerender/rewrite the candidate as required and retry both validators once. If exact evidence cannot repair the run or either validator still fails, do not promote the candidate, rerun the full review, or fall back to an older assessed report; stop with the exact error and candidate path. This recovery has no waiting loop and makes no remote mutation.

When `FINDINGS_SOURCE` exists, copy its exact bytes to `<run-directory>/findings-input.txt` with the filesystem tool. Do not depend on a shell variable retaining that source path. For bare PR online-only intake, do not create `<run-directory>/findings-input.txt`; set `CODE_REMEDIATE_METADATA.review_report_intake.requested_report=false` and every report-item counter to `0`.

For `mode=pr`, inspect `python PLUGIN_ROOT/shared/collect_pr.py --help`; collect `PR_TARGET` into `<run-directory>/pr` with checkout enabled for current online evidence, target/head refresh, local checkout.

In runtimes with network sandboxing, execute the complete collector command with approved external network access from its first attempt under `../../shared/native-skill-contract.md`. Before requesting it, state:

- `Action and purpose`: collect current PR evidence before remediation.
- `External capability`: read-only GitHub access plus the documented local checkout.
- `Credential behavior`: `gh` is an opaque local credential broker.
- `Filesystem and worktree effects`: write collection artifacts and may update the local checkout.
- `Retry policy and safe denial outcome`: one classified recovery only, otherwise remediation uses its core collection-failure path.
- For Codex exec, set `sandbox_permissions="require_escalated"` on the collector with a narrow read-only GitHub justification; never request a broad `python` approval prefix. Apply the other shared runtime and denial boundaries. A direct approval for `gh pr view` does not cover `gh` spawned by the collector: the outer collector command owns its nested GitHub CLI, HTTPS fallback, checkout, and Git fetch traffic. The PR request authorizes asking, never bypassing runtime approval.
- If an agent-caused unapproved attempt returns `github-network` before any user approval request or denial, rerun that same complete collector command once through the runtime's external-network approval mechanism before treating collection as terminal. This recovery exists only for that pre-denial sandbox mistake; after the user denies approval, the current turn stops and the retry is forbidden. Only after that approved collector attempt fails, external-network approval is unavailable, or the user denies it may remediation apply its core collection-failure path; never repeat more than one approved recovery attempt.

`github_read.py` is the plugin-wide GitHub data boundary: do not invoke `gh` outside it.

- It uses `gh` as an opaque local credential broker, never invokes `gh auth`, reads token/keychain state, or persists GitHub CLI failure output.
- It permits only audited built-in view groups (`gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, `workflow`), REST GET, and GraphQL queries; no remote mutation is permitted.
- Its public HTTPS fallback cannot establish private PR evidence.

Core and supplemental evidence:

- `collect_pr.py` treats PR identity/body plus exact local source as core evidence: it uses numbered fork-aware `gh pr checkout <number>` when needed, verifies the PR head SHA, and derives `diff.patch` locally.
- GraphQL review-thread resolution status is supplemental; if unavailable, the collector writes empty normalized thread arrays plus `review-threads-error.txt` and continues.
- Record that online-triage coverage gap in `action-items.md`, result confidence gaps, and unresolved/deferred closure rationale; never treat it as a code finding or silently claim complete thread triage.
- On core collection failure, use `<run-directory>/pr/pr-error.txt` and `<run-directory>/pr/command-failure.json` when present to distinguish the classified process failure from source-review findings; do not treat it as a merge recommendation.

When `gh pr view` metadata fails, public unauthenticated HTTPS fallback is eligible only when all of these hold:

- The failure is `github-network`, `github-auth`, `github-rate-limit`, or `command-timeout`.
- The checkout target is trusted: a canonical PR URL must match a configured GitHub remote; a numeric target requires exactly one distinct configured GitHub repository identity.

Ambiguous or unsafe targets, permission failures, not-found failures, and unclassified failures remain fail-closed.

Fallback behavior:

- The fallback normalizes limited PR metadata, then uses the verified `refs/pull/<number>/head` ref for a detached checkout and derives the local diff; it never establishes private PR evidence.
- `online-review-summary.json` must list unavailable fallback evidence as sorted IDs.
- Raw GitHub CLI stderr is never persisted; terminal diagnostics may include a safe `failure_reason` enum alongside non-secret classification metadata.

Findings intake:

- For `mode=report`, normalize only the review report after confirming it is assessed. Reject `review_status=unavailable` and `review_status=closed`; the latter is a close disposition without source findings. Do not read, collect, or infer any `<run-directory>/pr/` evidence.
- For `mode=pr`, always normalize `<run-directory>/pr/comments.json`, `<run-directory>/pr/reviews.json`, `<run-directory>/pr/review-threads.json`, and `<run-directory>/pr/unresolved-review-threads.json`.
  - When `REQUESTED_REPORT=false`, those current online records are the complete findings source. Do not read or infer a review report, and do not require a prior assessed artifact. If no online item is actionable after triage, continue through the documented `none-selectable` path instead of requesting `code-review`.
  - When `REQUESTED_REPORT=true`, additionally normalize `<run-directory>/findings-input.txt`. Treat the review report as a closure contract, not only code findings: before editing normalize report findings, failed `checks_failed`, `follow_up`, `review_decision.required_next_work`, confidence gaps, confidence-recovery remaining limits, and no-finding residual risks into report-origin action items.
  - Use local checkout in `<run-directory>/pr/local-checkout.json` as authoritative source for code triage/edits and require its `verified-local-checkout` diff provenance.
  - Require `<run-directory>/pr/target-branch.json` to prove base/target fetch before conflict/review-item resolution; `<run-directory>/pr/pr-head-fetch.json` records same-repo PR refresh or cross-repository skip rationale.
  - Checkout artifacts include `force_policy`; if checkout fails or does not match PR head, record `forced-checkout-not-attempted` and stop before forced retry.
  - If core metadata, target refresh, checkout, or local diff fails, record failure; continue with supplied report only when user accepts stale online-review coverage and no code edits are required, else fail.
  - If only supplemental review-thread resolution status is unavailable, continue with explicit partial-coverage evidence and do not infer that any thread is resolved.
  - Never inspect/edit PR code from `curl`, `raw.githubusercontent.com`, or copied `head-files/` snapshots; raw-file snapshot rejection: snapshots are rejected.

### 03: Understand PR Intent, Then Resolve Merge Conflicts

For `mode=pr`, required before `action-items.md`, `resolution-scope.md`, or report/PR-review code changes. Establish clean PR and latest target implementation before conflict markers make worktree noisy.

Read `remote_ref` from `<run-directory>/pr/target-branch.json` with a JSON parser and retain the exact printed value as `<base-remote-ref>`. Run `git merge-base HEAD <base-remote-ref>` as argv, retain its single printed value as `<merge-base>`, and write that value to `<run-directory>/pr/merge-base.txt`. Run these argv commands separately and write stdout to the named artifacts:

- `git diff --stat <merge-base>..HEAD` → `<run-directory>/pr/pr-intent.diffstat`
- `git diff --name-only <merge-base>..HEAD` → `<run-directory>/pr/pr-intent-files.txt`
- `git diff --stat <merge-base>..<base-remote-ref>` → `<run-directory>/pr/target-since-merge-base.diffstat`
- `git merge-tree <merge-base> HEAD <base-remote-ref>` → `<run-directory>/pr/merge-tree.txt`

Record each command's exit status; unavailable evidence is a gap, never an implied clean result.

Write `<run-directory>/merge-prestage.md` sections before attempting a merge:

- `## PR And Target Refresh`: PR number/head, target branch, fetched target hash, local checkout hash, evidence paths.
- `## Clean PR Implementation Context`: intended change, changed files, key invariants, clean-PR-implied tests/docs.
- `## Target Branch Context`: relevant fetched-target details, especially likely collision files.
- `## Conflict Risk`: mergeability, `merge-tree` signal, both-side changed files, conflicts present/likely/absent.
- `## Resolution Strategy`: reconcile PR intent and target implementation for each conflict/likely collision before review/report findings.
- `## Merge Execution`: conflict decision, authorization state, merge command/status, resolved paths, verification, and evidence path.

Write `<run-directory>/pr/merge-resolution.json` with `schema_version`, `conflicts_detected`, `status`, `authorization`, `base_remote_ref`, `target_oid`, `pre_merge_head`, `post_merge_head`, `merge_commit`, `resolved_paths`, `unmerged_paths`, and `evidence`. Use `status=not-needed` and `authorization=not-required` when fresh evidence proves no conflict. Do not merge the target merely to refresh a conflict-free PR.

If conflicts are present or likely, resolve them as PR integration before normalizing or addressing any report/online-review item:

1. Use the already-recorded clean PR purpose, invariants, target changes, and per-file resolution strategy as primary context. Inspect `git show <base-remote-ref>:<path>` and nearby tests where needed; conflict markers are secondary evidence only.
2. A generic remediation request does not authorize a local merge commit. Show the target ref/OID, intended merge, collision files, resolution strategy, and overwrite/commit effect. Ask for explicit authorization to create the local target-merge commit. Record `authorization=explicit-input|user-confirmed`; if authorization is absent or the runtime cannot ask, stop with `target-merge-authorization-required` before review-item work.
3. After authorization, run `git merge --no-commit --no-ff <base-remote-ref>` with the retained literal ref. Never rebase, force checkout, or rewrite history as a substitute.
4. Resolve only merge collisions, preserving the recorded PR intent atop the fetched target implementation. Do not combine review-comment fixes unless the same lines cannot otherwise form a coherent merge; record unavoidable coupling in `<run-directory>/closure-log.md`.
5. Verify `git diff --name-only --diff-filter=U` is empty, run the smallest collision-relevant tests, then create the authorized merge commit using `../../shared/commit-response-template.md` and the required `Co-authored-by: Codex <codex@openai.com>` trailer. Record the pre/post HEAD, merge commit, resolved paths, tests, and empty unmerged-path list in `merge-resolution.json` and `## Merge Execution`.

Do not create `action-items.md`, `resolution-scope.md`, or edit for a report/online-review finding until `merge-resolution.json` is `not-needed` or `completed`, the worktree has no unmerged paths, and no merge is in progress. If merge resolution or its verification fails, stop; do not hide the conflict behind finding remediation.

If checkout starts dirty, conflicted, or partially merged, fail or ask cleanup before editing. Never use an existing conflicted worktree as primary truth.

### 04: Normalize Findings Before Editing

**Structural context (optional)**: when `target_scope` names a Python module, also probe codemap-py once for changed-symbol/caller impact: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category review [--target <qname>] --out <run-directory>/codemap-context.json`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue normalizing the available report and/or online findings evidence. Persist the result once here; specialist owners assigned in step 06 receive `<run-directory>/codemap-context.json` in their context pack, never a fresh query.

Write `<run-directory>/action-items.md` starting with `## Review Item Resolution Table`, before prose. Normalize by canonical obligation, not by each report mention. For structured reviews, consume each `review_findings` record once using `report [<report-json>#<finding-id>]`; copy its title, summary, required change, evidence and closure evidence. Historical ID/severity-only or Markdown reviews remain readable: identify one primary finding/action record and attach other views of that same finding as `related_mentions`, never independent source records. Consolidate only matching canonical finding IDs within one report or an independently evidenced same obligation; shared closure text, a test command or a source location alone never justifies merging distinct findings. A genuinely independent gate or confidence obligation remains a separate item. Do not weaken report validation or certify historical failed results.

Every source has one owning item. Preserve genuinely independent report and fresh-online evidence with exact references, bodies and evidence paths. An online comment already attached to a finding must not also be ingested as a second duplicate row; record its duplicate/corroborating relationship in the owning item's expanded record. Multiple different sources for the same obligation may share an item; never drop provenance. Each report source carries `finding_id` when known; optional `related_mentions` retains repeated summary/action/confidence locations without increasing source counts. Render primary sources as `report [<report-file>:<line>]`, `report [<report-json>#<finding-id>]`, or `online [<comment|thread|review-id>]`. Keep full source records in metadata and expanded item records: category, stable source ID, location or `general`, complete body, evidence path or `report-only`. No counts, representative sources, ellipses or artifact links may replace required evidence. Before asking for selection, run the executable inventory gate in step 05; handwritten counts are not acceptance evidence.

When `online-review-summary.json` reports `pr_metadata_transport=public-https-fallback`, list the sorted `unavailable_evidence` IDs `github_provided_file_list`, `mergeability`, `review_decision`, `reviews`, and `top_level_comments` in `action-items.md` and the online action evidence, and add the exact confidence gap `Public HTTPS PR metadata fallback omitted evidence: <sorted IDs>.` Substitute that sorted list into `<sorted IDs>`. The final remediation confidence is capped at `0.89`; carry the gap and its closure state through `action-items.md`, result metadata, and unresolved/deferred evidence.

For `mode=pr`, check every report/PR-review item against PR intent and changed diff before triage:

- `direct-diff`: references PR-changed file/hunk/behavior.
- `pr-intent`: connects to PR purpose, acceptance criteria, review decision, requested change, even outside touched hunk.
- `adjacent`: touches nearby code/tests/docs/config/verification needed for safe merge.
- `unknown`: current evidence cannot determine relation.
- `unrelated`: no connection to PR intent, changed files, adjacent verification, or merge readiness after local PR-context inspection.

Write relation in action table and every expanded item. `direct-diff`, `pr-intent`, `adjacent`, `unknown` are never `out-of-scope`; keep `valid`/`needs-clarification` and selectable unless `resolved`, `already-fixed`, or `already-applied` evidence closes them. If current PR cannot close one, record `unresolved`, `deferred`, or required follow-up; never downgrade to `out-of-scope`. User can select, defer, or explicitly rule it into PR.

When `REQUESTED_REPORT=true`, include non-code report-origin review obligations:

- failed `checks_failed`, including missing independence, full gates, lint, type, test, confidence gates
- `follow_up`, especially `needs-independent-review`
- `review_decision.required_next_work` and merge/readiness blockers
- confidence gaps, confidence-recovery remaining limits, no-finding residual risks blocking acceptance

Report-origin obligations default in scope for `+review`, `+report`, `report`, or review-report path. Never mark `out-of-scope` merely because closure needs independent reviewer, installed tool, CI/full-gate run, or unavailable local environment. Mark `valid`/`needs-clarification`, keep selectable, leave `unresolved`/user-deferred until closure evidence. `out-of-scope` only for item proven unrelated to requested report/PR/target after citing evidence; never use it to silence failed gates/follow-up.

After the resolution table, add `## Review Report Intake`: whether a report was requested, total report-origin items, report-origin review-gate/follow-up items, selectable review-gate/follow-up items, and report-origin `out-of-scope` count. When `REQUESTED_REPORT=false`, record `requested report: false` and `0` for every report count. The `out-of-scope` count must be `0` unless an item is proven unrelated to the requested report/PR/target.

Required table columns:

- selection index: numeric selectable; `-` non-selectable
- input item: stable input row id, report id, PR comment id, review id, thread id, source location
- item name: short human-readable finding/review obligation/gate/comment/thread name
- item type: `code|test|docs|review-gate|confidence-gap|pr-comment|pr-review|pr-thread|unresolved-pr-thread|ci|typing|lint|security|performance|process|other`
- sources: ordered compact unique pointers rendered as `report [<report-file>:<line>]`, `report [<report-json>#<finding-id>]`, or `online [<comment|thread|review-id>]`; join multiple records with one plain ASCII space and never append locations, bodies, evidence paths, resolutions, summaries, or online URLs
- item id or source location
- source category: `report|online`; `online` covers PR comments, reviews, threads, and unresolved threads while item type preserves the detailed online subtype
- fetched evidence path, or `report-only`
- PR/diff relation: `direct-diff|pr-intent|adjacent|unknown|unrelated`
- severity
- summary
- triage status: `valid|resolved|duplicate|stale|out-of-scope|already-fixed|already-applied|needs-clarification`
- resolution: `implemented|resolved|rejected|stale|not-applicable|duplicate|already-fixed|already-applied|needs-clarification|unresolved`
- owner/status: `todo|fixed|resolved|deferred|unresolved|not-selected|not-actionable`
- resolved how: `[O<row-position>]`; immediately below the table define `[O<row-position>] <how/why resolved/unresolved/deferred/not applicable>`
- evidence: closure evidence or unresolved rationale as `[E<row-position>]`; immediately below the table define `[E<row-position>] <complete evidence, unresolved rationale, owner action, or next action>`

After table add `## Final Resolution Summary`:

- what was requested
- ingested entries total
- resolved or already-closed entries total
- implemented entries total
- unresolved entries total
- deferred/not-selected entries total
- not-applicable/stale/duplicate/rejected entries total
- one sentence: all selected local actionable items closed or not

Then add `## Final Resolution Table Completeness`:

- ingested entries total
- final table rows total
- omitted entries total: must be `0`
- selectable/non-selectable row totals
- triage status counts
- resolution status counts
- source records total
- represented source records total
- omitted source records total: must be `0`
- grouped items total

`CODE_REMEDIATE_METADATA.final_resolution_table` has the same item and source counts plus `items`, the ordered machine-readable source for the durable and final-chat tables. Each item contains non-empty `input_item_id`, `item_name`, `item_type`, `severity`, `triage_status`, `resolution_status`, `owner_status`, `resolved_how`, and `evidence`, plus boolean `selectable` and a non-empty ordered `sources` list. Each source contains `kind=report|online`, `source_id`, `location`, `body`, and `evidence`; `(kind, source_id)` is unique across items. A report `source_id` is `<report-file>:<line>` or `<report-json>#<finding-id>`; an online `source_id` is its stable comment, thread, or review ID, never a URL. Preserve source order, full bodies, and unique IDs. Render the `Review Item Resolution Table` and `Final Outcome Table` from this list. Their `Sources` cells contain only the ordered compact pointers; full source records remain in metadata and expanded item records. The durable table uses `[O<n>]` and `[E<n>]` cells and defines their complete `resolved_how` and `evidence` text immediately below the table. The final handoff maps cells mechanically as `input_item_id`, `severity`, `item_name`, every compact source reference joined in source order, `resolution_status — [O<n>]`, and `[E<n>] — owner/status: owner_status`; its table `details` list contains the ordered `O<n>`/`E<n>` definitions. No later prose rewrite may change those values. Fail before output if the durable table and items disagree, a compact pointer or detail symbol is missing or changed, expanded source detail is missing, `omitted_source_records_total` is nonzero, source counts disagree, the final table omits or changes an item, counts fail to account for every row, or any row lacks a disposition. `CODE_REMEDIATE_METADATA.final_resolution_table.required_columns` lists `input item`, `item name`, `item type`, `sources`, `triage status`, `resolution`, `owner/status`, `resolved how`, `evidence`.

Closure evidence for report-origin obligation must match type:

- independent review: independent specialist/maintainer output path plus updated metadata proving independence, or unavailable rationale
- full gates: clean full-gate/CI result path, or workspace/environment-prevented rationale
- type/lint/test environment: installed-environment command log, or missing executable/dependency rationale
- confidence gap: closing evidence, or explicit unresolved/deferred record

After table, keep an expanded item record for every remediation item:

- finding id or source location
- severity
- source and fetched evidence path
- every contributing `report|online` source ID, location, complete body, and evidence path
- PR/diff relation and evidence
- summary
- exact affected files
- expected closure evidence
- triage status: `valid|resolved|duplicate|stale|out-of-scope|already-fixed|already-applied|needs-clarification`
- resolution: `implemented|resolved|rejected|stale|not-applicable|duplicate|already-fixed|already-applied|needs-clarification|unresolved`
- owner/status: `todo|fixed|resolved|deferred|unresolved`
- unresolved rationale, when applicable

For ambiguous finding/thread/comment, inspect referenced local/checked-out code; sharpen to action item or `needs-clarification` before edits. If fetched PR evidence marks comment/thread resolved, table it as triage/resolution `resolved`, cite fetched evidence, state current PR marks it resolved; do not create implementation follow-up. If requested change already exists locally, mark triage/resolution `already-applied`, cite code evidence, no follow-up. Never fix duplicate, stale, out-of-scope, already-fixed, already-applied, or resolved comments; record triage evidence.

### 05: Ask For Resolution Scope Before Editing

Before code changes, build `<run-directory>/resolution-scope.md` with `## Resolution Scope Selection`. Selectable list includes every non-closed work-requiring finding; omits fetched online PR comments/threads currently resolved. Omit resolved online PR items from selection; keep them in `action-items.md` only as non-selectable audit rows, selection index `-`; omit-resolved-online rule.

Write `<run-directory>/selection.json` before prompting or accepting explicit scope: `schema_version=1`, `selected_indexes=null` while awaiting input, and `items` in stable ledger order. Each item copies `input_item_id`, `item_name`, `item_type`, `severity`, `selectable`, and complete ordered `sources` from the normalized ledger, plus nonempty `summary` and `closure_evidence`. Classify report-origin non-code gate/follow-up obligations as `review-gate` or `confidence-gap`; intake counts derive from these types, not words in titles. Include nonselectable items for identity/count reconciliation; only selectable items appear as choices. A confirmed selection is a list of numeric selection indexes, not finding IDs. No-selectable uses `[]` without pretending the user selected anything.

Report identity defaults to its source file path, so different reports may reuse finding IDs. When different files are verified views of the same report, give their sources the same explicit `report_id`; do not use a shared directory as identity. Selection input/output must be distinct files; output symlinks and aliases are rejected before writing.

Set `selection.json.presentation_version=3` and add a short, concrete `resolution_proposal` to each selectable item. Inspect `python PLUGIN_ROOT/shared/final_handoff.py selection --help`, then run its `selection` action with `--input <run-directory>/selection.json --out-scope <run-directory>/resolution-scope.md`. Failure blocks the prompt and edits. The helper validates unique item/source/canonical-finding ownership and any declared totals before writing. It renders `# | Severity | Finding | Resolution proposal | Sources`. Sources are derived tags such as `report ×1; online ×2`, counting genuine source records only; never fill the overview with paths, IDs, bodies or repeated mentions. Each ID-only detail group adds `Context`, `Done when`, and every genuine evidence reference once. Use Finding for the named problem, not a synonymous Issue label; context explains the failure without repeating title/proposal. Related mentions are labeled separately. Historical presentations retain their original rendering.

Before selection, say `Awaiting selection`; never imply pending findings were deliberately deferred. After explicit choice, update `selected_indexes`, rerun the helper, and record `CODE_REMEDIATE_METADATA.resolution_scope.presentation_version=3`, matching selection.json. Final validation checks exact rendered bytes, confirmed/deferred indexes and unchanged item/source inventory. Preserve version-2 and legacy scope validation for historical runs.

Selectable items:

- include triage `valid`
- include `needs-clarification` only when next step is clarification/code inspection, not implementation
- include report-origin failed checks, follow-ups, required next work, confidence gaps, residual risks unless cited evidence closes them
- include PR/review items related to PR intent, changed diff, adjacent verification, unknown relation unless cited evidence closes them
- exclude triage/resolution `resolved`, `duplicate`, `stale`, `out-of-scope`, `already-fixed`, `already-applied`
- exclude fetched online PR comments/threads marked resolved in current PR evidence

### Terminal Scope Context Contract

Before accepting an explicit scope or prompting for one, complete the pre-edit `<run-directory>/resolution-scope.md` document. It must contain, in this order:

1. `## Resolution Scope Selection`.
2. Helper-derived pending/confirmed state and actual item/source counts. Keep selection source, exact prompt or explicit-input note, confirmation, severity groups and resolved-online omission counts in `CODE_REMEDIATE_METADATA.resolution_scope` and the durable ledger, not hand-edited generated Markdown.
3. The complete short selection table, followed by a visually separated ID-only detail group for every selectable item. Do not abbreviate supporting context, closure evidence or genuine source references; do not repeat the table's finding name or proposal.

For an omitted `remediation_scope`, record the pending state before prompting: `selection source: user-prompt`, the exact prompt below, `user selection confirmed before editing: false`, and no selected indexes or severity groups. Retain resolved online items as nonselectable inventory entries and the documented omitted count; do not add them to the choice table.

Read the complete `<run-directory>/resolution-scope.md` through the filesystem tool and render it unabridged before any scope prompt or edits. Immediately append `Full report: <run-directory>/action-items.md`; do not use shell output or a persisted path variable to assemble this context.

The `Full report` path must appear immediately after the unabridged scope context and target `<run-directory>/action-items.md`, the complete normalized resolution report. The link supplements the scope context; do not replace the context with a `Selectable items:` summary, shortened numbered list, artifact link, or ellipsis. The rendered table must let the user choose from the full item id/source, severity, summary, and closure evidence without opening another file.

Immediately after the terminal command returns, emit exactly one user-visible assistant message containing, in order, the exact unabridged `resolution-scope.md` content, `Full report: <action-items.md path>`, and this question with its choices:

```text
Which findings should I remediate?
- all
- severity group: critical, high, medium, low, or comma-separated groups such as critical,high
- indexes: comma-separated indexes or ranges such as 1,3,5-7
```

A terminal/tool rendering alone never satisfies this interaction: collapsed output, `Read resolution-scope.md` summaries, status messages, artifact links without the ledger, and announcements that the ledger is rendering do not expose selectable options. Do not open a second scope-selection control after the combined user-visible message; that would duplicate the question and split its choices from their context.

If `remediation_scope` supplied, it is user selection: apply without re-asking; still write and print the complete `<run-directory>/resolution-scope.md` before edits, but omit the question and choices from the user-visible message. If omitted and selectable items exist, stop before edits and ask exactly once with the combined message above. Never infer `all`, silently select only code-editable items, or use default selection. If the runtime cannot ask at all, fail `scope-selection-required` before edit. If none selectable, write and print `none-selectable`, skip implementation, continue gates/artifact.

Record in the durable ledger and `CODE_REMEDIATE_METADATA.resolution_scope`; do not hand-edit generated `resolution-scope.md`:

- selection source: `explicit-input`, `user-prompt`, or `none-selectable`
- prompt presented
- user selection confirmed before editing
- selected indexes/severity groups
- omitted resolved-online count
- deferred/unselected indexes
- unselected critical/high findings

Validate before edit:

- `all` selects every selectable item
- severity group selects every selectable matching severity
- indexes select only selectable rows
- invalid index or attempt to select omitted/resolved item => fail before editing
- selectable items without explicit input or confirmed user prompt => fail before editing
- unselected critical/high recorded as deferred by user selection in `resolution-scope.md` and final output

Each `out-of-scope` item needs user justification/confirmation before removal from selectable list. Record every item in `## Out Of Scope Confirmation`: item id, source, rationale, evidence path, user confirmation. Without confirmation, retain selectable as `valid`/`needs-clarification`.

For `mode=pr`, add `## PR Relevance Summary` to `<run-directory>/action-items.md` and copy `CODE_REMEDIATE_METADATA.pr_relevance` into `selection.json.pr_relevance`; the selection helper renders the same count fields under `## PR Relevance Summary` in the generated scope. Do not append prose to generated Markdown. Connected is `direct-diff`, `pr-intent`, `adjacent`, or `unknown`. `connected items marked out-of-scope` must be `0`. Required-follow-up rows remain final-output-visible so user can rule them into current PR.

### 06: Build And Approve The Work Bucket Plan

> Only when evaluating or executing `parallel-specialists`, read [parallel-lifecycle.md](references/parallel-lifecycle.md) before preparing or approving its plan; parent-owned and sequential routes do not load it. The reference preserves the production lifecycle's containment, approval, verification, rollback, and cleanup obligations.

Before any selected-scope edit or specialist spawn, write `<run-directory>/resolution-workplan.md` sections:

- `## Work Bucket Plan`: one row per non-overlapping bucket with selected indexes, role, owned files/evidence, dependencies, and checks.
- `## Parallel Approval`: eligibility decision, exact proposal when eligible, approval source/status, and user response when prompted.
- `## Execution Order`: dependency-aware bucket order and `parent-owned|sequential-specialists|parallel-specialists` mode.
- `## Ungrouped Items`: always `none`; every selected item belongs to exactly one bucket, including parent-owned work.

Also write `<run-directory>/work-bucket-plan.json` with the exact `work_buckets` array used to render the user-visible table. Use `schema_version=1` for parent-owned, sequential, and planning-only work. Use `schema_version=2` from the outset when proposing production `parallel-specialists`; include the consumer, source repository relative to its workspace parent, a `.codex-rig-worktrees/<run-id>` sibling worktree root outside that repository, exact baseline HEAD/tree, rollback and cleanup policies, context hashes, resource locks, output paths, and verification commands required by the loaded production lifecycle reference. Keep the plan, approval, state, patches, rollback material, and lifecycle projection in the source-local `<run-directory>`. Hash the exact plan bytes with SHA-256 and record the digest in the workplan before asking for approval. The table, JSON, metadata, and approval must describe and bind the same plan bytes; never upgrade an already approved schema-v1 plan in place.

Group per capable specialist/domain when it reduces duplicated context or preserves one root cause. Valid keys:

- shared affected files/modules/tests/docs/CI workflows
- same closure type: code, tests, docs, CI, security, typing, performance, review-gate, merge/conflict
- same root cause/expected fix
- same verification command/closure evidence
- same merge/conflict collision risk

Never split a coherent fix across specialists or group unrelated findings for shared severity. Do not spawn one specialist per finding. A work bucket contains at most five selected items and each selected item appears in exactly one bucket. Parallel ownership uses concrete repo-relative file/evidence paths: no absolute paths, globs, `..`, duplicate aliases, or ancestor/descendant overlap. A collision pauses execution for parent re-planning.

Apply the overhead gate before proposing fan-out:

- With five or fewer selected items, keep one agent scope in one bucket and do not prompt for parallel approval.
- With more than five selected items, create the fewest coherent buckets needed to preserve the five-item limit, specialist/domain boundaries, and file/evidence ownership.
- Propose parallel execution only when at least two buckets are independent and expected elapsed-time or context savings exceed delegation and consolidation overhead.
- Keep parent-owned or sequential execution without a prompt when parallel execution is not useful.

When parallel execution is useful, first emit a user-visible assistant message containing the complete `## Work Bucket Plan` table from `work-bucket-plan.json` and its SHA-256 digest. That context message must not contain the approval question or its choices. Then open exactly one approval control. The approval control is the sole owner of this question and its choices. Configure it with `Approve these parallel work buckets?` and the choices `approve`, `revise`, and `parent-only`. If the runtime cannot open an interactive control but can ask directly, ask once in plain text instead of opening the control; never use both channels for the same question. Do not spawn or edit selected scope until the user confirms that exact digest before parallel dispatch. Explicit user input counts only when it approves the exact rendered-equivalent bucket content and digest; otherwise record a prompt. `revise` is never a final approval: regenerate the JSON/table/digest and ask again until the response is `approve` or `parent-only`. `parent-only` keeps all work parent-owned or sequential.

Write `<run-directory>/parallel-approval.json` after the final response with exactly `plan_sha256`, `source=not-required|explicit-input|user-prompt`, `response=not-required|approve|parent-only`, and `prompt_presented`. Parallel dispatch requires `response=approve`, `parallel_approval_status=approved`, and `approved_plan_sha256` equal to the current bucket-plan digest. A declined eligible plan records `response=parent-only`; an ineligible plan records `not-required` without prompting.

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
- owned files/evidence; parallel buckets cannot overlap
- execution mode: `parent|sequential|parallel`
- execution status: `planned|in-progress|fixed|verified|deferred|unresolved`

Owner assignment rules:

- implementation/refactor/API: `sw-engineer` primary, `qa-specialist` verifier. A public API/migration concern stays with the Terra parent/session unless the user expressly requests Sol or selects `solution-architect`; then it is a bounded read-only verifier/context artifact, never primary, returned to Terra for acceptance.
- test gap/regression proof: `qa-specialist` primary, `parent` verifier.
- docs/changelog/examples/docstrings: `doc-scribe` primary, `parent` or `qa-specialist` verifier for executable docs.
- CI/workflow/release gate: `cicd-steward` primary, `parent` verifier. Permission/secret work stays with the Terra parent/session unless the user expressly requests Sol or selects `security-auditor`; then it is a bounded read-only evidence artifact returned to Terra for acceptance.
- SemVer/compatibility classification, deprecation-cycle correctness, or release-readiness/blocker impact: `oss-shepherd` primary, `parent` verifier; changelog/migration prose stays `doc-scribe`, release automation stays `cicd-steward`.
- lint/type/pre-commit/suppression: `linting-expert` primary, `parent` or `qa-specialist` verifier when runtime could change.
- security/dependency/permission/data exposure: Terra parent/session primary, `challenger` verifier for high/critical/non-obvious closure. Use `security-auditor` only on a user-explicit Sol request or role selection, as a bounded read-only evidence artifact; it never becomes primary or accepts the closure.
- data/ML/research/performance: `data-steward`, `scientist`, or `squeezer` primary; `qa-specialist` verifies tensor/data boundaries.
- review-gate/follow-up: primary role matching missing evidence, `parent` verifier; unresolved when evidence needs unavailable CI, maintainer review, user-deferred external step.
- merge/conflict collision: `sw-engineer` primary; `challenger` verifies critical/high/non-obvious; cite `<run-directory>/merge-prestage.md`.

For each non-parent bucket write narrow `<run-directory>/specialists/<bucket-id>-context.md`: selected bucket only, relevant files/hunks/logs, closure question, stop rule, expected evidence. Omit unrelated findings, full PR discussion, and full review report by default.

Every owner/verifier must be one of the enums above. Parent buckets use `execution_mode=parent`; specialist buckets use `sequential|parallel`. Every specialist context-pack path must resolve inside the run directory and exist before dispatch.

Record `CODE_REMEDIATE_METADATA.resolution_workplan`: existing group/owner/verifier counts and path; `max_items_per_bucket=5`; `execution_mode`; `bucket_plan_path`, `bucket_plan_sha256`, `parallel_approval_path`, eligibility, approval requirement/status/source/response, prompt flag, `approved_plan_sha256`; plus `work_buckets` with bucket id, selected indexes, owner, owned paths/evidence, execution mode, and any singleton rationale.

### Production Parallel Lifecycle

For approved `parallel-specialists`, follow the loaded lifecycle reference through verification, join, source application, and cleanup before claiming completion. Parent-owned and sequential execution continues with step 07.

### 07: Apply Fixes In Selected Scope

Fix selected scope in priority: `critical` -> `high` -> `medium` -> `low`.

In `mode=pr`, complete target-merge conflict resolution and its authorized merge commit before selected report/online-review findings. For parent-owned or sequential execution, fix one selected valid group at a time. For approved production parallel execution, follow the loaded production lifecycle reference and treat the parent join as the barrier before source application. `<run-directory>/resolution-workplan.md` remains the execution ledger. Never edit unselected findings or a selected item outside its assigned group unless the workplan is updated first with the reason and affected closure evidence. After each group, record changed files/evidence in `<run-directory>/closure-log.md`.

### 08: Challenge Closure Before Full Gates

For every selected fixed finding answer:

- Does original failure still reproduce?
- Could it pass review but remain functionally wrong?
- Which regression check protects it now?
- What risk remains?

Missing closure evidence keeps item unresolved.

Write `<run-directory>/closure-log.md` `Closure Evidence` before marking any item fixed.

Apply `../../shared/specialist-orchestration.md` when selected findings cross specialist domain. `<run-directory>/resolution-workplan.md` is source of truth for closure ownership; write `<run-directory>/specialist-closure-plan.md` only for post-fix verification beyond workplan owner/verifier. Context packs stay group-local: selected group, changed files, closure evidence, exact verification question; omit unrelated review items/PR discussion.

Specialist closure triggers:

- `qa-specialist`: bug fix, test gap, regression proof, or behavior `already-applied` claim.
- `security-auditor`: only when the user expressly requests Sol or selects that role for auth, credentials, deserialization, dependency/supply-chain, permissions, or data exposure; return its bounded read-only evidence artifact to the Terra parent/session for closure acceptance.
- `cicd-steward`: GitHub Actions, release automation, flaky CI, gate-environment failures.
- `linting-expert`: ruff, mypy, pre-commit, type/lint config, suppression changes.
- `doc-scribe`: public docs, changelog, examples, migration text, public docstrings.
- `data-steward`, `scientist`, or `squeezer`: data/ML/research/performance findings.
- `challenger`: critical/high, conflict resolution, or closure based on non-obvious assumption.

If triggered specialist unavailable, write labeled in-main substitute in `<run-directory>/specialists`; lower confidence when independence mattered. Never mark selected high/critical resolved only from parent claim when closure evidence depends on triggered specialist domain.

### 09: Run Shared Quality Gates

Inspect `python PLUGIN_ROOT/shared/run_gates.py --help`; run every project-relevant closure gate with explicit command/skip reason.

### 10: Write Unresolved Findings

Write unresolved findings to `<run-directory>/unresolved.txt`.

When selected items remain unresolved, distinguish fixed work from still-needed process/environment/CI/external-review action. Include:

- `Unresolved Work Summary`: selected total/resolved/unresolved; local actionable unresolved; process/gate unresolved; environment blocked; external-owner blocked; whether all local code/doc findings closed.
- `Why Selected Items Remain Unresolved`: one row/unresolved selected item or reason group: selected indexes, severity, closure class, status, reason, attempted evidence, next owner/action.
- `Next Action`: smallest concrete action closing each unresolved reason group.

Use closure classes: `local-code-or-doc`, `process-gate`, `independent-review`, `environment-blocked`, `external-ci`, `user-deferred`, `already-closed`, `other`. With `remediation_scope=all`, never say "resolved all" while selected item unresolved. Say "all local actionable findings are closed" only when local code/doc closed; separately list remaining selected gate/process obligations.

### 11: Write And Validate Result Artifact

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write `CODE_REMEDIATE_METADATA`, validate `code-remediate`, promote only validated candidate.

`CODE_REMEDIATE_METADATA` records:

- normalized `mode`
- `confidence_gaps`, `confidence_recovery`: initial/final score, status, evidence, recovery actions, remaining limits
- `confidence_gap_closures`: one `closed|unresolved|deferred` closure with evidence/rationale per non-empty confidence gap
- `resolution_scope`: requested scope, `selection_source`, prompt, `selection_confirmed_by_user`, selected indexes/severity groups, deferred indexes, omitted resolved-online count
- `resolution_workplan`: `groups_total`, ownership counts, `unassigned_selected_items`, five-item cap, execution mode, parallel eligibility, `parallel_approval_status`, exact bucket membership/owned paths, workplan path, and the path/SHA-256/status of the reconciled production lifecycle when completed parallel specialists are claimed
- `review_report_intake`: requested-report and report/review-gate counts, including `report_items_marked_out_of_scope`
- `final_resolution_table`: ordered per-item machine ledger, ingested/final/omitted/selectability counts, source records and grouped-item counts, required columns, triage/resolution counts
- `out_of_scope_confirmation`: count, `all_confirmed_by_user`, and each item id/source/rationale/evidence path/confirmation
- `pr_relevance`: evaluation, `connected_items_marked_out_of_scope`, and `connected_required_followup_total` plus connected open/selectable counts
- `unresolved_summary`: selected/closure-class counts, `all_local_actionable_items_closed`, and reason/count/owner/next-action/evidence groups
- `merge_resolution`: merge artifact path, conflict decision, status, and authorization

For `mode=pr`, also include selected PR target plus `pr-routing.json`, `target-branch.json`, `local-checkout.json`, `merge-resolution.json`, and `merge-prestage.md` paths under the run directory.

### 12: Commit Attribution When Explicitly Requested

Leave accepted remediation changes unstaged by default. If the user explicitly requests a local commit after gates pass, load `../../shared/commit-response-template.md` and use its exact message shape. Every proposed or created remediation commit must end with:

```text
Co-authored-by: Codex <codex@openai.com>
```

Do not commit for a remediation summary alone or without the user's explicit authorization. Creating a new remediation commit never authorizes rewriting an existing commit. Amend, rebase, reset, squash, fixup, and equivalent history edits require an explicit request for that exact operation.

## Fail-fast Rules

01. Missing findings source in report mode, an explicit report path, or a report alias => fail. A bare PR has current online PR evidence as its findings source and must not fail or request `code-review` merely because no assessed review artifact exists. 01a. `+review`, `+report`, or `report` shorthand without matching target code-review report => fail: "run `$code-review <target>` first or provide a report path".
02. Shared gate script missing => fail.
03. Selected critical unresolved => fail. Unselected critical/high allowed only when user selection explicitly records deferred.
04. Finding marked fixed without closure evidence => fail.
05. Gate fails because of resolution patch => fail unless explicitly unresolved.
06. PR mode without fresh core PR evidence or explicit supplemental-thread/stale-coverage caveat => fail. 06a. PR code edits without `pr/local-checkout.json` proving local checkout matches PR head and `diff_source=verified-local-checkout` => fail.
07. Online thread/comment fixed without valid triage status => fail.
08. Duplicate/stale/out-of-scope/already-fixed review thread/comment edited, not recorded => fail.
09. Result artifact validator failure => fail.
10. Result artifact missing => fail.
11. `<run-directory>/action-items.md` lacks complete review item resolution table => fail.
12. PR mode uses `curl`, `raw.githubusercontent.com`, or copied `head-files/` snapshots for code inspection/edits => fail.
13. `<run-directory>/resolution-scope.md` lacks `Resolution Scope Selection` before edits => fail.
14. Edit finding not selected in `resolution-scope.md` => fail.
15. Selectable list includes fetched online PR resolved comment/thread => fail.
16. PR mode lacks `<run-directory>/pr/target-branch.json`, `<run-directory>/pr/merge-tree.txt`, or `<run-directory>/merge-prestage.md` before review/report finding edits => fail.
17. PR merge conflicts resolved from markers without recorded clean PR/target context => fail.
18. Apply report/online-review findings before PR merge/conflict prestage complete => fail.
19. PR mode runs `git`/`gh` with `--force` before explicit user confirmation and overwrite-risk explanation => fail.
20. Review report `checks_failed`, `follow_up`, required next work, confidence gaps, residual risks omitted from `action-items.md` => fail.
21. Report-origin obligation marked `out-of-scope` before user selection without cited unrelatedness evidence => fail.
22. Selected review gate/follow-up marked resolved without matching closure evidence => fail.
23. Selectable findings and omitted `remediation_scope`, but no recorded user prompt/confirmed selection before edit => fail.
24. Any `out-of-scope` item lacks specific rationale and explicit user confirmation => fail.
25. PR item connected to PR intent, changed diff, adjacent verification, or unknown relation marked `out-of-scope` => fail.
26. Connected PR item omitted from selectable scope/required follow-up without closure evidence => fail.
27. Selected items but missing `<run-directory>/resolution-workplan.md` before edits => fail.
28. Selected item absent from `Work Bucket Plan` or present in more than one bucket => fail.
29. Specialist-owned bucket lacks an existing in-run context pack or supported owner/verifier assignment => fail.
30. Selected unresolved items but `<run-directory>/unresolved.txt` lacks `Unresolved Work Summary`, `Why Selected Items Remain Unresolved`, or `Next Action` => fail.
31. `remediation_scope=all` with selected unresolved, but final output says/implies all selected work resolved => fail.
32. Unresolved selected item lacks closure class, attempted evidence, next owner, or next action => fail.
33. Selected local code/doc unresolved without blocker evidence and status fail/timeout => fail.
34. Final table row count differs from total ingested entries => fail.
35. Final table omits ingested entry or `omitted_entries_total` is not `0` => fail.
36. Final table status counts do not cover every row => fail.
37. Final table lacks `input item`, `item name`, `item type`, `triage status`, `resolution`, `owner/status`, `resolved how`, or `evidence` => fail.
38. Final chat lacks compact resolution summary, unresolved/deferred items, confidence/material limits, or artifact path => fail.
39. A pre-edit scope interaction substitutes a compact `Selectable items:` list for the unabridged `resolution-scope.md` context => fail: `scope-context-not-rendered`.
40. The unabridged scope context is not immediately followed by a `Full report` link/path to `<run-directory>/action-items.md` => fail: `scope-report-link-missing`.
41. The user-visible assistant message does not contain the unabridged scope context, `Full report` path, selection question, and choices together; collapsed tool output does not count => fail: `scope-context-not-visible`.
    - A separate scope-selection control repeats the question after the combined user-visible message => fail: `scope-prompt-duplicated`.
    - The parallel-approval question or choices appear in both a user-visible plan message and the approval control => fail: `parallel-approval-prompt-duplicated`.
42. An explicitly requested remediation commit omits `Co-authored-by: Codex <codex@openai.com>` or the shared commit-response template => fail: `codex-coauthor-trailer-missing`.
43. Existing history would be rewritten without an explicit request for that exact operation => fail: `history-rewrite-not-explicitly-authorized`.
44. Target conflicts are present/likely but `merge-resolution.json` is absent or not `completed` before report/online-review work => fail: `target-merge-not-completed`.
45. Target merge starts or commits without explicit authorization for that local merge commit => fail: `target-merge-authorization-required`.
46. Report/online-review finding work starts while unmerged paths or an in-progress merge remain => fail: `merge-conflicts-unresolved-before-review-remediation`.
47. Work bucket contains more than five selected items => fail: `code-remediate-work-bucket-too-large`.
48. Selected item is absent from all work buckets or appears in more than one => fail: `code-remediate-work-bucket-coverage-mismatch`.
49. Five or fewer selected items are split across multiple execution buckets => fail: `code-remediate-low-volume-fanout`.
50. Parallel specialist execution begins without recorded `approved` confirmation bound to the current plan digest from explicit input or a user prompt => fail: `code-remediate-parallel-approval-missing`.
51. Parallel buckets claim overlapping owned files/evidence => fail: `code-remediate-parallel-ownership-overlap`.
52. Bucket plan JSON, metadata, rendered table, or SHA-256 digest disagree => fail: `code-remediate-work-bucket-plan-content-mismatch`.
53. Final approval evidence does not bind `approve` to the current plan digest => fail: `code-remediate-parallel-approved-plan-not-bound`.
54. `revise` is treated as final approval without a regenerated plan and later `approve|parent-only` response => fail: `code-remediate-parallel-approval-response-invalid`.
55. Bucket owner/verifier is outside the supported role enums or its execution mode contradicts parent/specialist ownership => fail.
56. Specialist context pack is missing or resolves outside the run directory => fail.
57. Parallel owned path is absolute, globbed, contains `..`, aliases another path, or overlaps another bucket by ancestor/descendant => fail.
58. `final_resolution_table.items` is absent, duplicates an input ID, disagrees with declared counts, or does not match the durable Markdown rows => fail.
59. A remediation item has no source record, uses a source category other than `report|online`, duplicates a `(kind, source_id)`, or omits a source ID, location, complete body, or evidence path => fail: `code-remediate-source-provenance-incomplete`.
60. Presentation loses or changes a required source pointer, detail, or expanded source record => fail: `code-remediate-source-presentation-mismatch`. Apply the format-specific contract: concise and historical grouped selection/final output retain ordered pointers and complete labeled supporting details without requiring visible symbols; durable ledgers and historical symbol layouts retain compact source cells and every referenced symbol definition immediately below their table. Expanded records always preserve source location, complete body, and evidence path.
61. Source-record totals disagree or `omitted_source_records_total` is not `0` => fail: `code-remediate-source-coverage-mismatch`.
62. A completed `parallel-specialists` result uses a schema-v1 planning artifact or omits a completed schema-v2 production lifecycle reference => fail: `code-remediate-production-lifecycle-required`.
63. A referenced production lifecycle is absent, escapes the run directory, has the wrong SHA-256, or is not durably readable => fail: `code-remediate-production-lifecycle-evidence-missing`.
64. Production lifecycle evidence names a different approved plan digest => fail: `code-remediate-production-lifecycle-plan-mismatch`.
65. Production lifecycle status, joined nodes, ownership, patch hashes, integration order, source application, rollback material, cleanup, or containment fields do not reconcile with the approved schema-v2 plan and result metadata => fail closed; never downgrade the result to planning evidence or infer completion.

## Quality Gates

Required checks:

- `review`: complete action-item resolution table/item and source-record counts; compact ordered `report|online` references in visible tables plus full provenance in machine metadata and expanded records; review report failed-gate/follow-up intake; PR/diff relevance; indexed scope selection with prompt/confirmation; bounded non-overlapping work buckets with fan-out approval; schema-v2 production lifecycle reconciliation for completed parallel specialists; user-confirmed out-of-scope rationale; PR online-review triage; target refresh; intent-first merge/conflict resolution; merge authorization/completion evidence; relevant local checkout evidence; closure log; unresolved list with closure classes/next owner/attempted evidence/next action; `git diff --check`.
- `tests`: smallest checks proving fixed-finding closure.
- `artifact`: shared validator confirms closure artifacts, gate logs, result JSON shape.

Conditional checks:

- `lint`/`format`/`types`: run configured checks for changed code/config.
- `calibration`: run the owning calibration when findings affect skills, role/agent routing, or gate policy; Codex Rig source uses `runtime/calibration/run.py --layout plugin`.

## Calibration Hooks

Update calibration when resolution policy/output shape changes:

- benchmark patterns: `code-remediate`
- behavioral cases: bare PR online-only intake without a prior review artifact, explicit report-alias boundary, ambiguous findings, false closure, unresolved critical/high handling, missing user-selected resolution scope, missing resolution workplan for selected items, selected item omitted or duplicated across buckets, bucket over five items, low-volume fan-out, one-specialist-per-finding overhead, parallel execution without user approval, overlapping parallel ownership, specialist-owned bucket missing context pack, completed parallel remediation without hash-bound join/integration/source-application/rollback/cleanup evidence, capability-sandbox overclaim, unconfirmed out-of-scope triage, connected PR item marked out-of-scope, missing connected follow-up, code-review-to-remediation gate symmetry, unresolved selected-item closure summary, complete final resolution table, per-item machine ledger reconciled with durable Markdown, compact report/online references with full expanded source records, final chat outcome table for every ingested item and source, gate failure disclosure, artifact validator bypass, PR online review triage, supplemental-thread degradation, sandboxed collector network approval, PR target-branch refresh, PR intent-first merge/conflict completion, fork-aware numbered checkout, verified local diff before edits

## Output Contract

Before writing the result candidate, follow `../../shared/final-handoff-contract.md`: derive the handoff rows and `kind:source_id` references directly from `CODE_REMEDIATE_METADATA.final_resolution_table.items`, render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`, then emit `final.md` verbatim only after both validators and promotion pass. Any item/source coverage mismatch blocks output.

Use `../../shared/quality-gates.md`.

Apply the shared confidence band policy.

Keep the complete, unabridged resolution ledger in `<run-directory>/action-items.md`: every ingested item, validated column/count/status vocabulary, resolved evidence, scope selection, workplan, PR relevance, unresolved class, and confidence recovery.

Record source coverage with exact counters `source_records_total`, `represented_source_records_total`, `omitted_source_records_total`, and `grouped_items_total`; reconcile them against the item source records and require zero omissions.

The pre-edit scope context and one-channel interaction behavior remain exactly as required by the Terminal Scope Context Contract; this output contract does not abridge or replace them.

Final chat follows the shared ordered frame:

- `Outcome`: start `Remediation Summary` with requested scope; ingested/selected/implemented/unresolved/deferred totals; whether all selected local actionable items closed; and gate status.
- `Results`: derive handoff machine cells from `CODE_REMEDIATE_METADATA.final_resolution_table.items`, with one row for every ingested item, including non-selectable, rejected, resolved, and unselected rows. Preserve item and source order. Keep the exact six machine columns `Item | Severity | Finding | Sources | Outcome | Evidence / next action`, and set table `layout=concise`.
  - Keep every genuine source pointer in its bound machine cell, joined with a newline in source order. The renderer shows `ID | Severity | Finding | Resolution | Outcome`, expanding the bound resolution text once in the overview. ID-only detail blocks retain full source and evidence/next-action details; do not repeat findings, resolutions or outcome status.
  - Keep Outcome as `<resolution_status> — [O<n>]`, Evidence / next action as `[E<n>] — owner/status: <owner_status>`, and their exact bound `details` values in JSON. The concise renderer expands the resolution in the overview and evidence below; do not expose an ungrouped O/E list. Historical grouped and legacy handoffs retain their exact rendering.
- Apply the shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules. List every unresolved/deferred item with owner/action; link the result and full ledger; for `mode=pr`, add merge-prestage evidence and remaining collision risk.
- `Item`: exact stable input item ID; keep numeric selection indexes separate and explain them in the selection overview.
- `Outcome`: use `Implemented — <exact change>`, `Rejected — <duplicate, stale, not-applicable, or user-confirmed out-of-scope rationale>`, `Skipped / unselected — <user selection and remaining owner/action>`, `Already closed — <existing closure evidence>`, or `Unresolved — <blocker and next owner/action>`.
- Do not collapse rows sharing an outcome. Group exact duplicate sources only under the source-preservation contract above. Say `resolved all` only when `selected_items_unresolved=0`.

Minimum artifact payload template: `result-template.json`.
