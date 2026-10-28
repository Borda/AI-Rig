# Findings, gates and completion

### 05: Validate every candidate before publishing a finding

Validate every severity and category, including lint, documentation, architecture, and new or strengthened consolidation claims. Split compound claims. Inspect exact source, callers, current contracts, tests, configuration, and relevant CI counterevidence; green CI closes a claim only when its revision, backend, path, and assertions settle it. Static evidence may settle a claim. Run a bounded real reproducer when behavior needs execution, then faithful simulation where it answers the represented property; retain hardware/OS boundaries. No source fixes occur during review.

For every package-dependent behavioral claim, including favorable claims and proposed refutations, check the exact reviewed declaration and runtime before assigning a disposition. If an absent or incompatible Python package prevents the relevant execution, the parent owns dependency preparation through [Ephemeral Dependency Evidence](../../../shared/native-skill-contract.md#ephemeral-dependency-evidence): install eligible declared wheels into the run-local overlay, then execute the bounded affected check against the reviewed source and retain import-origin proof. Reuse matching preparation across claims and pass its evidence to read-only reviewers; do not ask each reviewer to install independently. Package absence alone does not exhaust feasible checks. A permitted installation or check still unperformed is incomplete work; a concrete routing/provenance refusal, missing compatible wheel, denied capability or failed preparation remains explicit unavailable evidence. Never hand remediation a bare missing-package limit that Code Review could have tested, count an import failure as confirmation of the behavioral claim, or claim a skipped backend passed. Static proof may close a claim only when it establishes the claimed behavior without the missing runtime; cite that proof and its boundary. Preserve the dependency contract's refusal conditions and separate fetch/run permissions; this rule authorizes no project/global environment mutation or automatic execution of newly introduced dependencies.

Record each candidate once in `review_validation.candidates`, with the exact fields below. `confirmed` alone enters `review_findings`; retain `refuted`, `already-fixed`, `optional-out-of-scope`, and `unverified` separately. Rejected repair advice does not refute the defect. Already-fixed retains original/current identities and fix evidence in its check/result. Refutation requires affirmative counterevidence; failed reproduction or unavailable retrieval alone is insufficient. Retained authoritative evidence remains usable while its revision/contract binding holds.

Use `unverified` only after feasible checks are exhausted and an actual unavailable prerequisite prevents closure; name that prerequisite, owner, next check, and observable closure. Available unperformed work uses null disposition and `incomplete`, including time/token pressure. Preserve every immutable source candidate in `source_findings` and map it to its parent candidate with `source_finding_mapping`; only confirmed mappings become fix requests. Without reconciliation, every nonconfirmed mapping retains the original severity, summary as candidate claim, and closure evidence as candidate closure condition; distinct originals sharing a candidate require the same complete `source_finding_reconciliation` and strongest severity as confirmed actions. Exact duplicates retain `source_finding_duplicates` accounting. Ordinary remediation intake retains every mapped immutable original and its evidence inside the existing open candidate obligation, preserving one occurrence per pending or unverified candidate; refuted, already-fixed, and optional-out-of-scope dispositions remain archived validation records without new edit obligations. Records markers 1 and 2 require the same canonical review-notes action table. In final handoff, every open candidate has a `remaining` entry whose `row_id` is its ID and whose `owner` matches; `item` includes its exact claim and missing prerequisite, and `next_action` includes the exact next check and observable closure condition. These evidence obligations appear once in remaining work, outside the confirmed finding table. Existing critical/blocking independent checks remain additive under current route/cap rules.

### 06: Write `<run-directory>/review-notes.md`

Set `CODE_REVIEW_METADATA.finding_records_version=2` for every new assessed review. This existing version gate identifies the required reader semantics; never downgrade the marker to bypass intake. An incompatible reader requires capability diagnosis and an authorized compatible reader or package-source repair, not an automatic review rerun. Apply [Report compatibility and recovery](report-recovery.md); ask about fresh review only when substantive evidence requires new work. Add `review_validation={schema_version:1, source_rating_version:1, source_verdict:<recommendation or null>, verification_readiness:<complete|blocked|incomplete>, candidates:[...]}`. Each candidate has exactly `id`, `severity`, `claim`, `source_revision`, `contract`, `trigger`, `confirmation_evidence`, `falsification_evidence`, `counterevidence` (string list), `check`, `result`, `environment`, `limits` (string list), `disposition`, `reason`, `feasible_checks_complete` (boolean), `missing_prerequisite`, `owner`, `next_check`, and `closure_condition`. Text fields are nonempty except `missing_prerequisite` may be empty for closed candidates. IDs are stable; confirmed IDs and severities exactly equal canonical finding records. Bind `source_revision` to `head:<verified head_oid>` from this run’s PR routing, or `sha256:<exact file digest>` of this run’s retained `local-source/source-snapshot.json`, `batch-inventory.json`, `inspection-plan.json`, or `direct-plan.json`; these source-bearing artifacts bind the actual inspected bytes. Unrelated revision text fails validation. Open verification cannot have passing result status or unconditional approval. Historical marker 1 and absent source-rating family retain the original five-value interpretation, where 4 means insufficient evidence and 5 means block/reject. New assessed results use result schema 3, and the validator requires this marker under both candidate and promoted filenames. Historical schema-2 results and schema-3 results lacking retained role-card proof remain readable through `find-review-report.py --archive-result <path>` as explicitly unverified data; they cannot certify completion or feed remediation directly; only producer recovery may reuse evidence the current validator authenticates ([Report compatibility and recovery](report-recovery.md)). Earlier strict portable or substituted runs without full retained card bytes cannot be certified after installed-card drift. Direct legacy validator calls may check fields and retained assessments, but do not authenticate archive provenance after role-card upgrades.

Define each finding once in `CODE_REVIEW_METADATA.review_findings` with stable `id`, `severity`, `title`, `summary`, `required_change`, nonempty ordered `evidence` strings, and `closure_evidence`. These enriched records are canonical; counts, notes and final actions are views, never separately ingested findings. In `Findings`, reference canonical IDs instead of repeating complete finding text. Decision summaries and confidence gaps sharing finding's closure cross-reference that ID; independent operational obligations remain distinct. Keep genuine code/test/online evidence in canonical record, not merely repeated report-line mentions.

For every new assessed review, also record `CODE_REVIEW_METADATA.reviewer_assessments`: one ordered `{role, rating, evidence}` record per actual reviewer, using a readable role name (with the internal provenance suffix specified below), an integer from 1 through 5 or explicit null for a completed undecidable assessment, and the retained assessment's evidence pointer. For production Python, place the triggered `Software engineer` assessment first, using `Software engineer (parent substitute)` only when its pass was actually substituted; a generic main-reviewer entry does not replace this required pass. Ask each reviewer to state its scoped rating and rationale; never infer approval from silence or confidence. For `mode=app-server`, copy `rating` from the retained JSON `assessment`; for other specialist passes, point to the retained response containing `## Reviewer Assessment`, `Source rating version: 1`, `Rating: <1-5|null>`, `Verification readiness: <complete|blocked|incomplete>`, and `Rationale: <nonblank explanation>`. In-main coverage uses the covered role name with ` (parent substitute)`, such as `Software engineer (parent substitute)`, and writes that same section in its retained substitute response. Parent-only reviews name the main reviewer, point to `review-notes.md`, and add `## Main Reviewer Assessment` with the same Rating and Rationale lines. The number of internal labels marked as parent substitutes must cover every pass with `mode=substituted`. In new presentation-7 reviewer lists and finding authors, use role names only: omit the internal ` (parent substitute)` suffix and render scores as `Role [rating]` (`Role [no source judgment]` for null). Keep exact internal labels in machine metadata and handoff payloads so evidence bindings remain intact. Historical presentation profiles retain their original names, parenthesized scores and exact rendered bytes. Do not invent skipped participants. Ratings are scoped judgments, not severity or confidence scores and never averaged into the overall verdict. Preserve disagreements for parent reconciliation. Every canonical finding and operational blocker carries a nonempty `authors` list of matching reviewer labels; deduplication retains every contributing author.

Derive each assessment role label from its manifest role ID: replace hyphens with spaces, capitalize the first word, and use `QA`, `OSS`, `CICD`, and `Software` for initial `qa`, `oss`, `cicd`, and `sw` respectively. For example, `cicd-steward` becomes `CICD steward`, and `qa-specialist` becomes `QA specialist`. Append ` (parent substitute)` only for a substituted pass; `Main reviewer` is an optional separate assessment. Match finding and blocker authors to the corrected assessment labels, retaining the internal substitute suffix in machine records.

Required sections:

- `Decision Summary`
- `PR Snapshot` for every assessed `scope=pr` review
- `Scope`
- `Risk Tier`
- `Files Inspected`
- `Specialist Passes`
- `Specialist Manifest`
- `Findings`
- `Review Findings and Merge Blocks` when canonical actions exist or the source recommendation requires changes; omit it for a null recommendation with no canonical actions.
- `No-Finding Residual Risks`
- `Confidence Gaps`
- `Confidence Calibration`
- `Online Review Triage` for `scope=pr`

When `online-review-summary.json` reports `pr_metadata_transport=public-https-fallback`, `Online Review Triage` must list sorted `unavailable_evidence` IDs `github_provided_file_list`, `mergeability`, `review_decision`, `reviews`, and `top_level_comments`, and add exact confidence gap `Public HTTPS PR metadata fallback omitted evidence: <sorted IDs>.` Substitute that sorted list into `<sorted IDs>`. The final review confidence is capped at `0.89`; preserve gap and its closure state in confidence metadata.

### 07: Run shared quality gates

Inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gates.py --help`; run every project-relevant review gate with explicit command/skip reason.

For PR review, pass the absolute `local-checkout.json.worktree` as gate runner `--worktree`, an absolute `--out` path in the source repository's retained run directory, and `--expected-head` from the checkout receipt. Explicitly set `--review 'git diff --check <verified-base-oid>...<verified-head-oid>'` with both full OIDs from `pr-routing.json`; the default plain `git diff --check` examines an empty diff in a clean detached worktree and cannot verify PR changes. The runner binds its before/after source receipts and executes every source-dependent gate in that worktree; do not use a shell `cd` wrapper or rely on the invoking checkout's current HEAD. For Python project tests, use the runner's import-bound mode: `--pytest-python <absolute-project-python> --pytest-import <project-module> --pytest-args-json '["-q", "tests/"]'` instead of free-form `--tests`. Name each project module whose source the tests must exercise; the runner executes pytest and inspects actual imports in every executing process, then requires origins in tracked review-worktree files. An editable install targeting the invoking checkout or an unimported declared module cannot establish a source-bound pass. Bootstrap a project environment when required under normal dependency and network approval rules. If test source origin cannot be proved, retain the failure or inconclusive evidence and do not claim tests exercised the PR source; a free-form passing test command alone does not satisfy the new PR source-bound test gate.

For `working-tree` and `path` review mirrors, pass the absolute recorded `review_worktree` as `--worktree` and an absolute `--out` inside the retained run directory. Omit `--expected-head` only when the output already contains the valid `local-source/review-worktree.json` receipt matching that exact mirror; the runner also verifies its retained source snapshot and rejects missing, mismatched, ambiguous, or stale mirror evidence. For source-bound Python tests, use `--pytest-python <absolute-project-python> --pytest-import <project-module> --pytest-args-json '<JSON array of project pytest arguments>'` and repeat `--pytest-import` for every required module; preserve the project's selection and parallel options. The frozen local runtime snapshot must prove each observed test and module origin is a current mirror member; an admitted added file may retain `tracked=false` only with matching snapshot membership and bytes. Do not invent a PR expected head or use free-form `--tests` as source proof. Clean PR mode still requires its expected head and tracked origins; release gates retain their clean-commit contract.

This test gate is the review's final gate: pass the project's full test selection with its own settings, never a changed-file subset, and record the exact pytest arguments in `review-notes.md`. Collected PR CI results for the verified head may be cited as additional verification evidence, but never replace or skip the local gate. Do not run pytest outside this gate except for a concrete finding's reproduction, which follows [Sandboxed Test Runs](../../../shared/native-skill-contract.md#sandboxed-test-runs), including its recorded `test_targets.py --out` selection.

Before the full run, compare the selected environment with the reviewed project's declared test groups and CI extras, including optional dependencies used by unconditionally collected tests. Prepare the complete selected test environment before retrying, following [Project Dependency Preparation](../../../shared/native-skill-contract.md#project-dependency-preparation). In nested remediation the remediation parent owns preparation; standalone review batches eligible roots under its ephemeral dependency contract. After its routing, provenance and wheel-only preparation checks pass, the full source-bound canonical gate may run with that overlay under the same sandbox and without execution escalation; a focused overlay check remains claim evidence and never replaces the full gate. Preserve any previous failed gate attempt. If a failed test proves that its child interpreter cannot import the verified checkout while the parent did, inspect the project's layout and test contract. For a confirmed `src` layout, pass the verified worktree's `src` directory in the gate process's `PYTHONPATH` so child processes inherit it, preserving existing entries and any overlay precedence; record the setting and rerun the equivalent source-bound gate. Do not apply this recovery when the test intentionally checks installed-package behavior, and do not use it to hide an import-origin mismatch.

Import-bound pytest may use project or CLI xdist options. The gate verifies imports and selected test files in every started worker, retains each worker receipt, and fails source proof when a worker crashes or lacks a valid receipt. A module absent in one worker is acceptable only when another executing worker proves its tracked origin; every observed import is checked. Keep the project's usual parallel test selection when useful.

A failed quality check does not cancel artifact closure. Inspect the recorded command, stdout, and stderr; direct-check receipts do not replace `gates.json`. If a borrowed environment autoloads an unrelated pytest plugin that fails before collection, inspect the project configuration and available existing environments; retain the failure, use an equivalent source-bound full-suite run only when its configured test behavior is preserved, and do not mistake the plugin failure for a source defect. When no suitable environment is available after permitted preparation, record the failed gate and continue all feasible review coverage and artifact closure rather than stopping at the test failure. Preserve the failed attempt before any evidence-backed rerun with the project's existing environment and equivalent check scope. A local launcher failure, including an `uv` subprocess failure, is process evidence, not a source finding. If checks remain failed, retain them in the canonical gate/result evidence (`status=fail`, or `timeout` when applicable), reconcile the decision and handoff, and continue through step 12. If valid gate evidence cannot be produced, use the blocked-handoff output with the exact unmet checkpoint; never substitute an informal review verdict.

### 08: Classify findings using `../../../shared/severity-map.md`

### 09: Compute the structured review decision and update `Decision Summary`

Skip this step after T0 PR collection failure: write terminal availability/recovery output instead, with no recommendation or merge decision.

Use one established source verdict, independent of severity and verification readiness:

- `accept-as-is` (Approve): no required source changes; optional preferences alone may accompany approval. Unconditional approval requires complete verification and passing required gates. Completed source inspection may support this judgment with blocked verification when an actual prerequisite remains unavailable, provided no source findings, open concerns or undecidable reviewer scope remain. Keep the failed result and state the missing check; this is not merge approval. An executed behavioral-check failure must be investigated and validated as a source candidate before selecting a judgment.
- `minor-changes` (Minor changes): confirmed required lint, nit, formatting, or genuinely minor documentation corrections only.
- `needs-more-work` (Major revision): valuable aligned contribution with fixable correctness, design, architecture, tests, or substantive documentation defects. Even a fixable critical bug belongs here; explain concrete closure.
- `insufficient-but-promising` (Insufficient, but a good initiative): promising project-aligned contribution with substantially incomplete implementation. Explain the missing source implementation and useful initiative. Missing hardware, unavailable checks, or reviewer/process gaps never independently imply this stage.
- `reject` (Block): evidenced incompatibility with documented project direction or binding constraints. Cite the requirement, explain why the current scope cannot fit and whether a different scope could fit. Preference alone cannot reject.
- null: completed assessment cannot establish a source judgment after feasible checks. Return the inspected coverage/rationale with blocked readiness; a missing response remains a coverage limitation, never a null assessment or source finding.

Set `review_validation.source_verdict` equal to `review_decision.recommendation`. Keep `verification_readiness` separate: complete, blocked by named unavailable capability, or incomplete because feasible work remains. Missing hardware, missing protocol, reviewer launch failure, or missing evidence never independently justify rejection. Say `No source changes identified; verification blocked/incomplete` where appropriate, or qualify a known source judgment. Required gates and provenance retain their force. Historical `not-aligned` stays readable but is not a new verdict. `Decision Summary` must include:

- `Recommendation`: exact value above
- `Summary`: 1-3 sentences covering outcome
- `Rationale`: why recommendation follows from findings, gates, scope
- `Blocking findings`: critical/high items or `none`
- `Minor changes`: medium/low items or `none`
- `Required next work`: pre-merge work or `none`
- `Confidence`: score plus key gaps

Apply [Review limitations and additional review](../../../shared/quality-gates.md#review-limitations-and-additional-review). Use `needs-more-work` only for supported source defects. If no source defect was found, say `No source changes identified; verification limited` and explain the actual coverage. Keep missing reviewer coverage, context isolation, provenance and unavailable checks in confidence/readiness disclosures, never canonical findings, operational blockers or required-next-work requests. Report all supported findings and material limitations even when optional review is declined; do not delay delivery for another review or bookkeeping perfection. Preserve validated evidence and explicitly label any unvalidated report.

For missing independent coverage, name the uncovered axes as factual limitations. For unproven historical blueprint isolation, disclose the historical limit without requesting impossible retroactive proof. A further pass requires the specific decision defined above; declining or dismissing it removes that request from final findings, blockers and next steps. Never claim a fresh assessment proves the original run was isolated, or count parent substitutes as independent coverage.

For assessed `scope=pr` review, immediately before user-facing output, rebuild `PR Snapshot` from current run's `pr.json`, `pr-routing.json`, and `gates.json`; never reuse PR number, author, CI state, or recommendation from invocation or earlier chat. This is refreshed presentation of exact evidence reviewed, not new network fetch after review.

`PR Snapshot` must use this compact Markdown table in `review-notes.md` and reproduce it before findings in final chat:

| Field | Value |
| -- | -- |
| PR | `[#<number> — <title>](<url>)` |
| Author | `@<pr.json author.login>` |
| CI | `passing`, `failing — <check names>`, `pending — <check names>`, or `unavailable` |
| Type | `fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed` |
| Commit | full reviewed commit SHA; local/path reviews label it as the working-tree base |
| Suggestion | `approve`, `minor changes`, `major revision`, `insufficient, but a good initiative`, `block`, or `no source judgment` |
| Reviewers | `<readable role> [<rating>], <readable role> [<rating>].` |
| Report | `[Markdown report](<absolute path to this run’s review-notes.md>)` |

Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.

The Reviewers and Report rows and legend are additive. Preserve the aggregate text summary, all existing header fields, verdict, findings, evidence and confidence. In `final-handoff.json`, attach `reviewers` copied from `reviewer_assessments` and `summary` copied from `review_decision.summary` to the snapshot table; the renderer appends the Reviewers row, Report link and legend, so do not duplicate these derived rows in machine `rows`. The Report link targets the owning run’s full nonempty `review-notes.md`, preserving all per-finding details; never use compact `final.md` as that target. Attach `source_rating_version:1` and `verification_readiness` copied from `review_validation` to that snapshot. The renderer selects the versioned five-stage legend only for that explicit family. A source rejection does not itself close a PR. Terminal unassessed branches retain their current exceptions.

Null means an actual reviewer returned an inspected but undecidable scope, with blocked readiness and a rationale naming the unavailable prerequisite and next check. Retain the completed response and its coverage. A reviewer that never returned remains a disclosed coverage limitation, outside the source findings and action table. Never relabel a historical raw 4 as rejection. For app-server plans, set `source_rating_version:1`; returned JSON assessment has exactly `source_rating_version`, `rating`, `verification_readiness`, and `rationale`. Challenge-only adversarial-loop assessments retain their separate historical contract. Ratings record scoped judgments only. No validator derives, cross-checks or overrides `review_decision.recommendation` from them, so a header that disagrees with the verdict passes every automated gate: keeping the two coherent, and never averaging ratings into the verdict, is a workflow obligation of this skill.

**Every new assessed review needs one snapshot table, whatever its scope.** `scope=pr` uses `PR Snapshot` with the fields above. Every other scope uses `Review Snapshot`, with `layout` omitted (legacy), columns `Field | Value`, and these rows:

| Field | Value |
| -- | -- |
| Scope | the reviewed target — working tree, branch, commit range, or path set |
| Revision | reviewed commit SHA, or the exact declared working-tree state |
| CI | `passing`, `failing — <check names>`, `pending — <check names>`, or `unavailable` — never fabricate a run that did not happen |
| Type | `fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed` |
| Commit | full reviewed commit SHA; local/path reviews label it as the working-tree base |
| Suggestion | `approve`, `minor changes`, `major revision`, `insufficient, but a good initiative`, `block`, or `no source judgment` |

Attach `reviewers` and `summary` to it exactly as for `PR Snapshot`; its rendered table also includes the derived Report link to the full `review-notes.md`. A new assessed candidate without this table fails promotion with `code-review-final-handoff-candidate-attribution-missing`. Validation binds the rows themselves: a different field set or order fails with `code-review-final-handoff-review-snapshot-fields-mismatch`, and a `Suggestion` disagreeing with `review_decision.recommendation` fails with `code-review-final-handoff-review-snapshot-suggestion-mismatch`.

Read PR CI from `pr.json.statusCheckRollup`: failing completed check makes CI `failing`; otherwise incomplete check makes it `pending`; otherwise completed successful/neutral/skipped checks make it `passing`. An absent or empty rollup is `unavailable`, never `passing`; name known non-passing checks. Classify `Type` from verified change intent and diff, not title or file count. Map `Suggestion` from `review_decision.recommendation` exactly as the validator does:

| `recommendation` | `Suggestion` with `review_validation` (all new assessed reviews) | `Suggestion` without it (historical) |
| -- | -- | -- |
| `accept-as-is` | `approve` | `approve` |
| `minor-changes` | `minor changes` | `minor changes` |
| `needs-more-work` | `major revision` | `needs work` |
| `insufficient-but-promising` | `insufficient, but a good initiative` | `insufficient, but a good initiative` |
| `reject` | `block` | `reject` |
| `not-aligned` | `not aligned` | `not aligned` |
| null | `no source judgment` | `no source judgment` |

The snapshot applies only after successful source assessment: terminal unavailable and close outputs use their compact display-only status table, without assessed findings.

For every new assessed review with findings or operational blockers, regardless of scope or recommendation, add a `## Review Findings and Merge Blocks` section immediately after `Decision Summary` and include its grouped table in final handoff. For an undecidable source judgment with no canonical actions, omit the table and disclose the review limitations. It is canonical pre-merge handoff and must use this exact Markdown header and column order:

```markdown
| Finding / area | Author | Required change | Evidence | Status |
| --- | --- | --- | --- | --- |
| Exact finding ID or declared operational-blocker ID | Canonical author roles without execution-origin suffixes, joined with comma-space | Canonical required_change | Canonical evidence joined with semicolon-space | Required, Minor change, Verify, Implemented; verify, Required verification, Reject, or Not aligned |
```

Include one non-empty row for every confirmed source finding and genuine project action. Failed or missing review gates and reviewer-process limitations remain in verification and confidence disclosures; they do not create table rows. Schema-v2 first cells use exact declared finding or operational-blocker IDs; validator checks unique, complete identity coverage as specified below. Historical schema-v1 has only count-based coverage; parent must cross-check its source identities.

For new assessed final handoffs, set `presentation_version=12` and findings table `layout=concise`, `overview_only=true`; keep its four machine columns unchanged. Each finding row additionally carries `title`, `summary`, and `closure_evidence` copied exactly from its canonical record. Its Required change/Evidence cells must equal that record's required_change and semicolon-space-joined evidence. Write required_change as one short, concrete resolution proposal; put rationale and failure mechanism in summary, and verification in closure_evidence. Use `Finding` for named problem; do not create synonymous `Issue` field. Operational blockers carry canonical `id`, descriptive `title`, short `required_change`, and nonempty `evidence`; omit summary/closure. Keep runner or host limitations distinct from source defects.

Copy each canonical `authors` list to its final finding row without changing the four machine cells. The renderer shows `ID | Author | Finding | Resolution proposal | Status` and omits per-finding detail groups from terminal output. Keep full context, evidence and closure instructions in saved `review-notes.md` and all canonical machine fields. Do not repeat titles, proposals or status below the overview, and do not restate title as context. Render each confidence limit and gap as a separate bullet, retaining exact text, status, evidence/rationale and deduction labels. Human-readable titles never replace stable IDs in machine cells. Historical handoffs without reviewer attribution retain exact rendering; ID-only historical blockers remain readable.

`Status` must distinguish required, minor, verification-only, rejected, or not aligned; `Implemented` alone is not open action. Do not collapse distinct findings into generic row. This table is mandatory when canonical actions exist; a null source judgment without actions needs only the factual limitation, never an invented row. For other source-change decisions, missing, malformed, empty, or non-actionable rows fail validation. Terminal review-unavailable output forbids assessed machine tables and renders its display-only status table plus the process diagnostic.

### 10: Record confidence evidence before the final handoff

Before final chat/`result.json`, write `Confidence Calibration` in `review-notes.md`; mirror in `CODE_REVIEW_METADATA.confidence_recovery`.

First execute [Agent-owned confidence recovery](../../../shared/quality-gates.md#agent-owned-confidence-recovery). Missing child-join provenance requires inspecting authentic receipts and completing eligible bounded recovery through [Reviewer validation recovery](validation-recovery.md) before choosing a parent fallback. Do not hand the user recoverable agent preparation or unexecuted checks merely as confidence deductions. If required independent coverage remains unavailable after permitted recovery, preserve that failure and the exact blocker; parent inspection cannot close it.

Required confidence calibration content:

- `Initial Confidence`: starting score and concrete uncertainty sources.
- `Objective Evidence`: inspected changed files; PR/local artifacts; tests/checks; specialist outputs; pattern cross-checks.
- `Confidence Gaps`: missing checks, substituted specialists, unresolved PR evidence, unverified assumptions, unavailable source context.
- `Recovery Actions`: loops to raise confidence: read more code, check nearby patterns, run focused commands, add specialists, narrow claims, downgrade unsupported findings.
- `Recomputed Confidence`: final score and supporting evidence.
- `Remaining Limits`: residual uncertainty; acceptable or blocking.

Shared confidence policy:

Apply shared confidence band policy from `../../../shared/quality-gates.md`. Record required evidence in `Confidence Calibration`; mirror it in `CODE_REVIEW_METADATA.confidence_recovery` before output.

Confidence must be honest/objectively verifiable. Never raise it to pass gate; improve evidence, narrow claims, or fail with named gap.

### 11: Declare no findings and residual risk

When no confirmed finding remains, state that explicitly in `Findings` and record every unchecked path, unavailable evidence, and untested risk in `No-Finding Residual Risks` of `review-notes.md`; never imply coverage the review did not perform. With findings, still list there each risk not tied to a finding.

### 12: Write and validate the mandatory result artifact

Run review validator with `--manifest-only` against completed run directory and current parent thread before writing `result.candidate.json`. It preflights routed specialist manifest shape and provenance, including one or two sequential attempts for every spawned pass. If it fails, preserve manifest and completed specialist evidence, repair only deterministic bookkeeping that has evidence, then rerun this preflight once; never invent missing attempt provenance or create candidate before preflight passes.

Preflight success is not review completion. Follow `../../../shared/helper-cli-contract.md` and authoritative help; finish this ordered sequence before reporting the assessed verdict, including `needs-more-work`:

1. Follow `../../../shared/final-handoff-contract.md` to prepare the branch-appropriate handoff, then run `final_handoff.py render` to bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`.
2. Run `write-result.py` with `CODE_REVIEW_METADATA` and `FOLLOW_UP` to write `result.candidate.json`, retaining canonical failed checks and final-handoff binding.
3. Run the review-specific validator against that candidate and this run's actual parent-thread provenance.
4. Run the shared validator for `code-review` against the same candidate and run.
5. Only after both validators pass, promote that candidate to `result.json`.
6. Run `find-review-report.py --complete-run <run-directory>` through the Output Contract's completion checkpoint. Emit only successful completion stdout verbatim for standalone review; approved nested review instead returns through [Nested review delivery](../../../shared/final-handoff-contract.md#nested-review-delivery). Rendering and promotion alone never authorize either delivery.

Run steps 1–5 as one command: inspect `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/remediation_finalize.py finalize --help` and call it with `--skill code-review`, the actual `--parent-thread-id`, `--codex-home` when not the default, your metadata draft, your `final-handoff.json` draft, the result fields, and `--promote`. It derives the handoff's verification, confidence, and result artifact from `gates.json` and metadata, plus canonical finding/blocker content in the existing handoff action rows and notes table. Author stable row/source IDs and statuses once; ambiguous identity coverage or conflicting notes/handoff statuses requires reconciliation. The snapshot, sources, outcome, remaining work, and next steps stay agent-authored. Canonical records, provenance, assessments and reviewer output paths are never inferred or repaired by this derivation. It renders, writes the candidate, runs the review-specific validator and then the shared validator in all-errors mode, and promotes only when both pass, returning one JSON summary. Use the returned structured errors, not-run checks and next action to distinguish copied presentation drift from missing source/runtime proof. Repair every listed error from its hint and rerun. If validation then exposes a different error, check whether it shares a cause with the earlier failure. When evidence shows the earlier issue was resolved and the new cause is distinct, continue the new checkpoint under the same authorization; otherwise apply the shared recurrence policy. If the same error remains after its own repair, investigate its cause and follow the bounded recovery below with the failing check and evidence. A failed quality gate is not failed report production: after validation, promotion and `--complete-run`, report review completion separately from change readiness. Stop only the failed finalization route; already-authorized source-confirmed remediation may proceed through its preliminary intake with review proof open. Never require another specialist launch solely to repair a copied table. Step 6 still runs separately.

Resume the first unmet checkpoint under existing authorization; reuse only still-valid source, gate, and specialist evidence. Missing artifacts require completing this sequence, not another source review or specialist launch merely for bookkeeping. Failed validation follows the bounded recovery below; stop only for an actual unresolved blocker, with preliminary evidence labeled and no assessed verdict.

`CODE_REVIEW_METADATA.specialist_passes` mirrors every triggered specialist entry; `review_run_id`/`review_input_sha256` mirror top-level values. Strict portable spawned passes mirror their validated runtime summary, while instruction-bounded native inspection route uses validator-defined inspection evidence and must not claim portable sandbox or approval controls. `CODE_REVIEW_METADATA.scope` matches normalized scope. For assessed reviews, `CODE_REVIEW_METADATA.review_decision` mirrors `Decision Summary` recommendation, summary, rationale. A terminal collection failure records `review_status=unavailable` with source findings not assessed and merge decision not made. A terminal close records `review_status=closed` plus validated `close_decision`, with source findings not assessed and detailed review skipped. Both terminal shapes use exactly zero `critical`, `high`, `medium`, and `low` findings and omit normal recommendations/follow-up and assessed-review metadata. Every assessed source-change decision or review with canonical actions carries the validated `Review Findings and Merge Blocks` table in `review-notes.md`; a null judgment without actions omits it; terminal unavailable and closed results use their canonical prose and a display-only status table, with no assessed finding rows. An assessed review with unavailable thread-resolution evidence includes canonical thread confidence gap and unresolved/deferred closure rationale. `CODE_REVIEW_METADATA.confidence_recovery` mirrors `Confidence Calibration` and includes `initial_confidence`, `final_confidence`, `status`, `evidence`, `recovery_actions`, `remaining_limits`. `CODE_REVIEW_METADATA.confidence_gap_closures` has one closure per non-empty `confidence_gaps`, with `status=closed|unresolved|deferred` and matching evidence/rationale.

## Fail-fast Rules

01. Empty `files.txt` and `untracked.txt` with no explicit target => fail.
02. Shared gate or diff collection script missing => fail.
03. Result artifact missing => fail.
04. Assessed review that skips changed-file inspection => fail; terminal close instead requires minimal verified diff evidence at T0.
05. Blocking finding without local evidence or pattern check => fail.
06. Missing T0 scope classification => fail.
07. Detailed-review routing signals, triggered roles, manifest roles, and specialist files disagree => fail.
08. Detailed review triggers an axis without actual spawned or explicit substitute output, or lacks complete required role and assigned-path coverage => fail, except the validated fully inspected `TRIVIAL` or `LOCAL` parent-only route with no required independence. Direct schema-ten runs require authentic child and join evidence; record actual execution without claiming parallel overlap that did not occur. Historical overlap and scheduling gates remain unchanged for their respective routes.
09. Detailed review is missing review routing or validator-required specialist/inspection evidence => fail.
10. `BROAD` or `HIGH_RISK` review labels parent-serial substitute as independent, or reports completion when an unwaived expressly user-required independent pass remains unavailable => fail. An explicit waiver requires a separate revised-scope plan and normal completion checks; it never certifies the original scope.
11. Result artifact validator failure => fail.
12. An assessed review is missing required `review-notes.md` sections, or terminal result violates its exact prose shape => fail.
13. PR scope without PR body metadata, authoritative remote/target evidence, exact `local-checkout.json`, locally derived `diff.patch`, comments/reviews, normalized thread artifacts, and `online-review-summary.json` => fail.
14. PR scope ignores unresolved online reviews without triage, or claims complete thread triage when `review_threads_status=unavailable` => fail.
15. An assessed review has missing structured review decision summary or invalid recommendation => fail.
16. PR scope uses `curl`, `raw.githubusercontent.com`, or copied `head-files/` snapshots for source inspection instead of local checkout => fail.
17. PR scope runs `git`/`gh` with `--force` before explicit user confirmation and overwrite-risk explanation => fail.
18. Missing `Confidence Calibration`, `metadata.confidence_recovery`, or `metadata.confidence_gap_closures` => fail.
19. Shared confidence policy violation from `../../../shared/quality-gates.md` => fail.
20. Spawned specialist lacks validated parent/child rollout provenance, hashes, or exact output binding => fail.
21. More than two attempts, a generic retry without an authenticated host failure receipt, or native correction without the exact eligible evidence in [Reviewer validation recovery](validation-recovery.md) => fail. The current manifest validator has no authenticated generic timeout, transport-error, or rate-limit receipt; retain that evidence gap and use a supported recovery or disclosed parent fallback instead of asserting the failure type.
22. An assessed source-change decision or review with canonical actions is missing complete actionable `Review Findings and Merge Blocks` table => fail.
23. A terminal core T0 PR collection failure emits an assessed findings table or merge recommendation, omits its process diagnostic/recovery/evidence or required display-only status table, or does not explicitly mark source findings `not assessed` and merge decision `not made` => fail.
24. A terminal close lacks one valid close code, two distinct evidence sources, counterevidence check, verified-head binding, `confidence >= 0.90`, or advisory-only/no-mutation state => fail.
25. A strict portable spawned pass lacks frozen shared execution plan/manifest, exact role/context/output correspondence, host-bound terminal evidence, truthful execution label, or parent runtime metadata mirror; instruction-bounded native inspection route instead must satisfy its validator-defined inspection evidence and text-only output contract => fail.
26. A terminal close emits findings, normal recommendation/follow-up, an assessed findings table, review routing, specialist artifacts, or detailed-review claims, or omits its required display-only status table => fail.
27. A review-only run finishes, aborts, or resolves an existing merge or conflict, or asks for that authorization, instead of reporting it and handing it to `code-remediate` => fail.
28. A PR review whose earlier run carries `resolution.jsonl` dispatches reviewers without the prior resolutions, or re-reports a `rejected` finding without citing new source evidence => fail.

## Quality Gates

Required checks:

- `review`: T0 files, risk tier, either validated terminal close evidence or local changed-file inspection, simplicity/readability/reproducibility inspection, project docstring-style detection, docstring/comment policy inspection for changed code, PR body/target/checkout/local-diff evidence when relevant, specialist manifest/notes when detailed review proceeds, structured assessed decision summary or terminal unavailable/closed result, non-approval PR findings/action table for assessed reviews, plain terminal diagnostics, explicit supplemental-thread degradation, confidence calibration/recovery, PR online-review triage when assessed, severity map when assessed, `git diff --check`.

Conditional checks:

- `lint`/`format`/`types`/`tests`: run/inspect available results when needed to validate finding.
- `calibration`: run when reviewing native skill/agent/config behavior.

## Calibration Hooks

- regression scenario: every package-dependent behavioral claim, including favorable claims and refutations, prepares eligible declared dependencies and performs affected execution before disposition; unavailable preparation retains its concrete failure rather than package absence alone

Update calibration when review routing, severity discipline, decision vocabulary, or output shape changes:

- benchmark patterns: `code-review`
- behavioral cases: false blocker, target-advance false blocker, unrelated invoking-worktree edits preserved, isolated review-worktree obstruction diagnosed, supplemental-thread degradation, sandboxed collector network approval, each terminal close code plus its false-positive fall-through, close-versus-reject separation, closed-report remediation rejection, non-approval PR findings/action table missing reported finding, missing `needs-more-work` table, T0 PR collection failure with merge recommendation/assessed findings table or without process diagnostic/display-only status table/source findings `not assessed`/merge decision `not made`, malformed finding-table row, missing specialist pass, no-finding residual risk, substituted fan-out confidence, PR online review triage, missing project docstring-style detection, missing code self-documentation, long code blocks, deep branching, docstrings masking poor structure, low-confidence recovery loop, objective confidence evidence, review-only conflict handoff without finish or abort, prior remediation feedback confirmed or re-reported with new evidence
- PR routing cases: target-branch refresh required, isolated exact-head detached review worktree, verified local diff required, raw-file snapshot rejection, remediation attached checkout, same-repository fallback and fork-recovery route separation

## Output Contract

Complete step 12 before final output. Follow `../../../shared/final-handoff-contract.md` with branch `assessed`, `unavailable`, or `closed` exactly as review result requires. Terminal `unavailable` and `closed` branches forbid assessed machine tables and use the presentation-12 display-only status table; explicitly requested exact caller format uses `caller-contract` and retains the same artifact closure and completion checkpoint.

Only after the completion checkpoint succeeds, emit `final.md` verbatim through that command's successful stdout for standalone review. An approved review nested in remediation follows [Nested review delivery](../../../shared/final-handoff-contract.md#nested-review-delivery) and returns to its waiting caller instead.

Use `../../../shared/quality-gates.md`.

Completion checkpoint:

This checkpoint is mandatory on first run and after every resume, repeated invocation, or user-assisted recovery. Follow shared Resume And Re-entry contract; notes-only findings and passing local tests are not completed review. Keep plain-English opening and required assessed summary/tables rather than replacing them with informal verdict and bullets.

Unstarted specialist passes, pending triage, unrun gates or unwritten artifacts are unfinished work, not a blocker by themselves. Continue the authorized review and its first unmet checkpoint in this turn; do not emit a blocked handoff merely because the work remains. A failed operation is evidence to diagnose, not permission to stop the review. A terminal pause requires an unresolved controlling stop after permitted recovery and independent work are exhausted: a denied or unavailable required capability, a genuinely missing user decision, unverified source, or an exhausted applicable retry limit. Cite that condition and its evidence; never substitute a list of pending tasks.

1. Run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/find-review-report.py --complete-run <run-directory>`; inspect `--help` for explicit parent-thread/rollout arguments. It requires promoted canonical result, reruns both validators, and validates that run independently of newer-run discovery; default discovery and explicit remediation intake still enforce newest-run eligibility. Emit only successful stdout verbatim for standalone review; nested delivery retains these bound final bytes and returns the exact result path.
2. On preflight, validation, promotion, or lookup failure, explain in commentary what could not finish, why the check rejected it, and what work continues. Select the next action in this order; failure alone never selects a final handoff.
   - First perform permitted recovery and continue all independent authorized work in this turn. Missing declared dependencies require [Project Dependency Preparation](../../../shared/native-skill-contract.md#project-dependency-preparation) and the review's run-local overlay boundary; record an ineligible package as that check's limitation, then continue eligible preparation, source inspection, specialist passes, and report production. Uninspected checker configuration remains pending, never `not-applicable`. Failed checks remain evidence in the completed review rather than a reason to abandon review production.
   - Read and apply [Reviewer validation recovery](validation-recovery.md) and shared Actionable Pauses before handing off; runs that validate cleanly do not load it. The reference preserves the dispatch-order, context-not-sent, reader-command and bounded-repair rules, the repair-route stops, and the question branches. Recommend recovery, give any necessary decision with its consequences, and name the resume condition.
   - If a required async question is accepted, yield immediately with the decision pending. Do not sleep, poll, or send a final or status handoff. Resume this checkpoint after an explicit answer.
   - Only when an unresolved controlling stop still prevents closure after the preceding recovery and independent work, state `Review handoff blocked`, the exact process error and retained run path, and `Review not complete`. Deliver all supported findings as explicitly preliminary evidence, with inspected scope and factual limitations; link the retained full notes. Identify the exhausted recovery and the condition requiring another actor. Do not repeat a declined additional review in next actions or turn the process error into a source finding. Do not end with only a diagnostic or a generic request to rerun. Use the shared unvalidated-process table with the blocked status before it, not buried below it. Preserve the supported `PR Snapshot` or `Review Snapshot` as an explicitly preliminary, unvalidated table even with zero findings; mark unavailable fields `unverified`. Never emit a normal assessed verdict or imply promotion.
   - Before a genuinely blocked handoff that needs a user decision, apply [Incomplete review decision](report-recovery.md#incomplete-review-decision): ask `New review`, `Resume last one`, or `Stop` through the native question tool, not hidden prose or a request to send another message. The remediation parent owns this single prompt for nested review. Preserve pending-question, denied-capability, closed-PR and retry-limit rules.
   - For a genuinely blocked standalone handoff, identify the specific blocker and what must change, then provide a copyable request: `Resume the code review of <target> from <exact run path> and finish the missing checks.` Resume preserves valid evidence and completes the first unmet checkpoint. When a fresh review is feasible, also provide `Run a fresh code review of <target>.` Explain that fresh review recollects and rechecks evidence. Replace placeholders with the actual target and path, recommend the feasible route, and explain why an unavailable route cannot be used. These requests never replace continuing authorized work now, a required native decision, or the waiting remediation's nested delivery.
3. Preserve preliminary notes without treating them as validated `+review` input. Do not claim findings were lost, fabricate runtime evidence, relabel spawned work as inline, or offer online-only intake as equivalent recovery. A blocked process message does not claim canonical terminal result or misuse pre-assessment `unavailable` branch.

### Final review presentation

Final chat follows shared ordered frame with these review-specific branches.

For assessed review:

- New assessed schema-3 results use enriched canonical `CODE_REVIEW_METADATA.review_findings` records from step 06, or `[]` for none. Historical exact `{"id":"<stable finding ID>","severity":"critical|high|medium|low"}` records remain accepted as archive data; do not rewrite historical artifacts. Derive IDs from assessed source findings before writing action rows, never reconstruct them from table. Per-severity totals must equal `findings`.
- Optional `operational_blockers` contains canonical `id`, descriptive `title`, short `required_change`, and nonempty ordered `evidence` for non-finding actions, separate from severity totals. Historical exact `{"id":"<stable blocker ID>"}` records remain readable. IDs must be nonblank, unique and disjoint. Every required notes action table and final findings table uses those exact IDs as its first cell and covers complete declared set once—no missing, duplicate or unknown action.
- Historical schema-v1 count-only artifacts remain readable; new reviews must not downgrade to v1 to avoid identity checks. Terminal unavailable/closed results omit both assessed identity lists.
- For versioned assessed handoffs, `outcome` is exactly `{"title": "Review Decision", "summary": "Recommendation: <recommendation>."}` with recommendation copied from `CODE_REVIEW_METADATA.review_decision.recommendation`. Keep rationale, blockers, and required next work in decision summary and result rows; never replace canonical outcome with unbound approval statement.
- `Results` reproduces fresh `PR Snapshot` immediately after summary and before any findings for every assessed PR, including zero findings and all checks passing. Non-PR assessed reviews retain their `Review Snapshot` too. It also reproduces the canonical `Review Findings and Merge Blocks` table when canonical actions exist or the source recommendation requires changes. A null recommendation without canonical actions omits this table; never fabricate a row to represent limited coverage.
- For every new assessed Git review, run the normal finalizer to derive `metadata.reviewed_commit_sha` and the snapshot `Commit` row from frozen source receipts. Ordinary native preparation freezes `reviewed-commit.txt`; batches retain `source_snapshot.revision` in `batch-inventory.json`; PR and local/path reviews retain their existing checkout/snapshot receipts. Commit routes without native receipts use the gate runner’s matching clean before/after source observations, with or without an explicit `--worktree`; an expected SHA without observations never qualifies. Preserve the full SHA, never the invoking checkout HEAD or a shortened hash. PR/commit reviews identify the reviewed tip; working-tree/path reviews label it `working-tree base` and retain `Revision: diff sha256:<digest>` from `diff.patch`. The finalizer adds the same identity to the full report. New assessed candidates require the bound SHA; historical promoted reports without it retain their original rendering.
- Apply shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules; name reviewed evidence, checks, unresolved blocks, owners, and material limits.

For terminal core T0 PR collection failure:

- Start with artifact-bound plain-English explanation, then `PR Review Availability: unavailable` and `Reason: <specific cause>.` before verification details. For an isolated review-worktree obstruction, name that obstruction and link `worktree-preflight.json` or `checkout-state.json` when present; preserve final-handoff path redaction. Otherwise name classified failure from `pr-error.txt`. Include available safe command/checkout diagnostics and mark unknown causes explicitly.
- Use the compact status table plus plain prose for classified process diagnostic, `Next steps` recovery, source findings `not assessed`, merge decision `not made`, confidence/material limits, evidence, and artifact path.
- Do not use an assessed findings table or normal recommendation; retain the terminal status table.

For terminal close:

- Start with plain-English explanation of close disposition, then state `Review Decision: close` and name close code.
- State source findings were not assessed, detailed review was skipped, decisive evidence, counterevidence checked, and `GitHub mutation: not performed`.
- Provide the terminal status table, confidence/material limits and artifact path without a normal recommendation.
- Omit separate `Next steps` recommendation because close code is terminal. A completed close disposition is not paused review.

Keep full routing, recovery, and closure evidence in artifact. New assessed source recommendations are `accept-as-is`, `minor-changes`, `needs-more-work`, `insufficient-but-promising`, `reject`, or explicit null; historical `not-aligned` remains readable.

Minimum artifact payload template: `result-template.json`.
