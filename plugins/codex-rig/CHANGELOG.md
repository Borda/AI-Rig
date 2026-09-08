# Changelog

## 0.14.6

- Require the canonical finding-records marker on every new schema-v2 assessed review candidate. Omitting `finding_records_version` no longer falls back to the bare id/severity record shape, so a new producer cannot skip the canonical title, summary, required change, evidence, and closure fields. The final-handoff validator applies the same requirement and keeps the grouped/concise layout mandatory for those records. Schema-v1 historical results stay readable and exempt.

## 0.14.5

- Separate read-only specialist findings from parent-owned report persistence and authorized executable probes. Keep investigation productive through safe serial probes without treating unexecuted requests as evidence, scratch directories as sandboxes, or diagnosis as source-edit authorization.
- Reject observed filesystem write grants and unsupported filesystem-control records in portable read-only runtime validation across review, implementation, and management. Preserve mandatory isolation admission, independent review gates, and explicit limits: no new launcher support or live isolation proof.

## 0.14.4

- Consolidate review/remediation handoffs: concrete resolutions in overview rows, ID-only supporting details, descriptive gate blockers, and compact source-kind counts in remediation selection. Preserve canonical evidence bindings and historical rendering.
- Apply launcher compatibility admission across review, implementation and management before parallel reads. Automatic mode retains serial fallback where independent passes are not mandatory; explicit parallel reads reject unavailable controls. Preserve authoritative runtime checks and high-risk review independence; no new host capability or live-runtime support is claimed.

## 0.14.3

- Align active Codemap consumer guidance with once-only launcher/context resolution, persisted evidence reuse, direct known syntax, and independent stable read-only queries. Preserve optional-provider fallback and distinguish settled graph facts from separate implementation or coverage questions.

- Clarify that integration metadata is not active query policy and that static package checks do not establish live activation or token savings.

- Expand Python utility and test-helper docstrings with executable examples; make ordinary test helpers and fixture implementations private while preserving pytest injection names and required interfaces. Keep docstring lines within 120 characters and regenerate package hashes for the documented payload.

## 0.14.2

- Read generated selection Markdown as UTF-8 in the packaged presentation regression so Windows locale defaults do not corrupt Unicode assertions or fail the installed-package gate.

- Replace external commit-message drafts with chat-reviewed literal message arguments: remove draft-file creation/cleanup approvals, preserve shell-safe quoting and exact-message checks, and stop on unsupported transport or failed commits without automatic repair.

- Keep all-closed selection and grouped review-gate intake compatible with final validation, make byte-bound test fixtures portable, and align fail-fast instructions with grouped layouts.

- Preserve selection inputs against output aliases, scope finding identity to report files, bind gate counts to item types, and require complete actions for new local reviews with findings. Shared closure evidence alone never merges distinct findings.

- Fix repeated report mentions being treated as independent findings: enriched canonical records bind titles, actions, evidence and closure criteria while preserving legacy record reads.

- Validate source ownership, canonical finding identity, counts and confirmed indexes before presenting remediation selection; bind the unchanged inventory to final outcomes.

- Render short review/remediation overviews with named detail groups and full references; separate pending selection from deliberate deferral. Keep historical handoff bytes unchanged.

## 0.14.1

- Preserve incomplete/unpromoted review barriers across later collection failures; reject approving recommendations with failed or incomplete quality gates and reject non-string finding severities without a traceback.

- Gate review completion through both validators and exact downstream discovery before emitting final text. Detect identified notes-only reviews as incomplete and block stale fallback after newer incomplete/malformed results; preserve preliminary evidence and show blocked-first diagnostics instead of a normal verdict with a promotion disclaimer.

- Reject review dispatch preflight when the current launcher cannot supply the declared read-only/never child controls; keep authoritative post-run validation and existing role permissions unchanged.

- Require stable schema-v2 assessed finding IDs/severities with exact count and notes/final-action coverage, plus separately declared operational blockers; retain historical schema-v1 reading.

- Bind assessed review approval/minor recommendations to finding severity, require unique action-table identities, and enforce canonical final outcome wording that agrees with the structured recommendation.

- Bind schema-v2 artifact paths to the run's final `result.json` through candidate promotion; reject blank/duplicate confidence gaps and missing, duplicate, or undeclared closures while retaining schema-v1 read compatibility.

- Separate unchanged-baseline command/output-boundary preflight from passing postimplementation verification in the production parallel remediation route, preserving exact-command approval and parent integration gates.

- Load optional parallel lifecycle mechanics only when evaluating or executing that route; retain common obligations in the remediation entrypoint, with contract/calibration and independent followability coverage. This reduces ordinary-route instruction bytes; live token or latency savings are not established.

## 0.14.0

- Store assessed PR reviews under stable PR-number namespaces with monotonic numeric run indexes: collection starts in a timestamped temporary run until authoritative `pr.json` identifies the PR, then promotes the complete run to `.reports/codex/code-review/pr-<number>/run-<NNN>/` and uses that path for every later artifact. Keep local reviews timestamped, retain pre-identity failures as unavailable diagnostics, and preserve discovery of legacy flat review artifacts without migration.
- Add a self-contained canonical G0–G8 execution-flow schema with narrow boxed terminal nodes, true two-column gate cells with full-height separators and centered content, continuous connector geometry, center-aligned horizontal `✓ YES` / `✗ NO` forks, compact gate definitions, explicit terminal/join/derivation sub-gates, stop/re-plan/fail/accept endpoints, and absolute cross-references from the README plus Code Review, Implement, and Manage contracts. This documentation clarifies synchronization boundaries without enabling generic parallel writes and keeps snippet-included strict MkDocs builds resolvable.
- Promote the code-remediate-local production route after native Linux and Windows lifecycle evidence, while keeping generic parallel writes disabled and every serial or parallel write bound to a frozen plan plus exact digest approval.
- Promote Implement and Manage portable read-only routes through executable consumer-bound runtime matrices. Their installed preflight derives promotion from a closed allowlist, binds the exact consumer and write policies, validates any required exact-digest parent-write approval before dispatch and again before mutation, and requires a consumer-bound post-join runtime result; unbound generic evidence is promotion-ineligible. `auto` returns only concrete `parallel-read` for a promoted route and otherwise resolves safely to serial.
- Make the GA default execution mode `auto` without allowing it, an environment value, a flag, or a child request to bypass consumer promotion, parent-serial mutation authority, canonical gates, or write approval.
- Add durable diagnostic-expiry enforcement: append path-free JSONL evidence to fixed `expiry-audit.jsonl` before eligible deletion and after outcomes, delete only the exact HMAC diagnostic, and retain unresolved diagnostics until resolution.
- Record matched live telemetry observations of `1.6091x` and `1.0791x` speedups with `1.0024x` and `0.9976x` token multipliers. Raw prompts and responses are not retained; the current host has no provider-enforced usage cap, so actual context may exceed pre-dispatch reservations and is reported as an overrun.

## 0.13.0

- Keep code-remediate scope choices attached to the complete visible selection ledger in one assistant message, and render grouped report/online source pointers with plain spaces instead of terminal-visible `<br>` tags.
- Make code-remediate source application and rollback compare Git clean-filtered object identities across integration and authoritative worktrees, while retaining raw source SHA-256 evidence and failing closed on unknown content. This preserves the known-state rollback gate when native Windows checkout filters represent the same Git content with CRLF bytes.
- Raise the installed-package lifecycle timeout from 60 to a bounded 180 seconds for native Windows Git setup, and test mode-only patch rejection from Git raw metadata without depending on host `chmod` or `core.filemode` support.
- Pin every third-party GitHub Action to an upstream-resolved full commit SHA with an adjacent readable version comment, and add a repository regression that rejects mutable, malformed, or comment-missing references without duplicating the reviewed mapping in shipped configuration.
- Add a hard parent-owned pre-dispatch token-admission gate with stable-prefix reservations, completed/active preservation, same-gate serial re-planning, and an explicit boundary that the current host cannot enforce actual per-child provider usage; bind current schema-v2 acceptance to exact-plan budgets while retaining an acceptance-blocked, promotion-ineligible reader for earlier schema-v2 evidence without token budgets.
- Add compact retained wave proof with HMAC identity, strict field allowlisting, durable digests, observed token overruns, bounded diagnostic expiry, and unresolved-failure retention; document and test equal-gate per-skill rollback.
- Add explicit disabled portable read-only adoption declarations to `implement` and `manage`: define only future read-only evidence or inventory fan-out, require immutable freeze and complete join barriers, keep every mutation and canonical gate parent-serial, preserve equal-gate fallback, and block flags, environment values, `auto`, or natural-language requests from enabling the skills before native code-remediate lifecycle evidence, separate promotion, and per-consumer matrix acceptance. Add a failure-first contract test and document that no registry, scheduler, shared-runtime change, generic write path, or quality-gate concurrency is introduced.
- Add an approval-bound generated-fixture worktree scaffold using a simple parent-authoritative handover: freeze exactly two disjoint packages at one clean source identity, create separate detached worktrees, join two completed child reports with concise summaries, exact changed paths, and canonical patch SHA-256, verify both reports against actual Git changes, derive and integrate patches in the parent, and record non-force cleanup evidence. Provide `create_completed_child_handover` so children hash the lifecycle's raw Git subprocess bytes instead of shell- or RTK-rendered diff output; normalize conventional string or path-like lifecycle-state paths at this boundary and reject unsupported objects with stable `PilotError`. Remove the obsolete fixture-history parser, dynamic validator import, timestamp reconstruction, and App Server attestation dependency while retaining authority rederivation, inherited `GIT_*` sanitization, retained-attempt fingerprints, portable path/alias/symlink checks, commit/index/untracked/delete/rename/mode/type rejection, deterministic integration, and failure retention. The record is operational evidence under parent authority rather than cryptographic host provenance; the generated-fixture route remains production-write ineligible and default-serial, while the code-remediate-local production lifecycle remains separately gated.
- Document the complete parallel-execution architecture, including frozen split/freeze/manifest schemas, DAG and wave barriers, ownership and worktree isolation, approval allowlists, read/write promotion, authoritative joins, truthful overlap labels, serial fallback, retries, privacy-minimized telemetry, consumer gates, and rollback boundaries.
- Accept current host terminal records whose whole-second endpoints and millisecond duration differ by less than one second while rejecting larger inconsistencies, and bind every joined child delivery to the authoritative parent collaboration path.
- Add one installed-package-safe staged execution manifest validator that derives `parallel`, `independent-spawned`, and serial labels from recorded substantive intervals; validates serial stage barriers, joins, hashes, controls, bounded retries, cancellation, resource locks, Windows path aliases, and digest-approved parallel writes; rejects common credential material in context packs; and requires consumers to bind recorded observations to host evidence before making runtime-proof claims.
- Bind the read-only pilot to the current parent spawn/start/result-delivery and child lineage/control/terminal/output rollout shapes, fail closed on drift or false overlap labels, and keep generic resolver parallel writes ineligible because its declared controls do not prove per-command enforcement; code-remediate-local uses a separate parent-authoritative lifecycle.
- Promote only schema-v2 portable-read-restricted runtime evidence for non-sensitive read-only waves; bind task classification to the frozen parent plan and restricted network plus approval `never` to observed host records, scan context/output records for common secrets, leave filesystem credential isolation unverified, and keep host-isolated and generic resolver writes unavailable.
- Add the accepted code-remediate-local production lifecycle: bind an exact schema-v2 plan and approval to a clean source `HEAD`/tree, two to four disjoint buckets, context hashes, resource locks, detached worktrees outside the authoritative checkout, verification commands, rollback policy, and non-force cleanup; have children edit only owned paths without commits and return canonical terminal handovers; re-derive and lexically integrate patches in a separate worktree; apply one parent bundle only after exact preimage checks and durable reverse-patch storage; verify postimages; restore only known states; retain evidence and stop on ambiguity; and bind the completed schema-v2 lifecycle digest into the remediation result. Containment is parent-authoritative operational postcondition containment with `capability_sandbox_verified=false`, not per-child capability isolation, hostile-child security isolation, or a globally atomic source transaction. This local route leaves generic `write_parallel_promoted=false`, the shipped default `serial`, and makes no native or live completion claim.
- Keep code-remediate plan, approval, state, patch, rollback, and lifecycle evidence in the authoritative repository's normal `.reports/codex/code-remediate/...` run directory, place plan-bound worktrees only under the external sibling root `.codex-rig-worktrees/<run-id>`, and expose the lifecycle through the thin argparse sequence `prepare`, `create-handover`, `join`, `collect`, `integrate`, `apply-source`, and `cleanup`; this is not a scheduler, registry, global promotion, or native/live proof.
- Close the code-remediate lifecycle challenge boundary: bind a fixed new state basename and output names, actual context-pack paths and hashes rehashed at preparation and every authority transition, and the fixed `code-remediate-shared-quality-gates` reference; record integration as `structurally-verified` without executing arbitrary plan commands, leaving shared gates as executable result authority. Recompute rollback preimages and record `rollback-ambiguous` on unknown or unverifiable states; require the artifact validator to independently re-hash every child patch, forward source bundle, and rollback patch under the exact run root; and reject symlinked source, worktree, evidence, state, output, or patch path components. The boundary remains operational, not capability isolation, and makes no live/native completion claim.
- Run the complete parallel-worktree lifecycle suite from the manifest-declared installed package in the existing Linux, macOS, and native Windows full-test matrix. Require green native Linux and Windows runner evidence before promoting the code-remediate-local production route; CI configuration and a successful local macOS proof are not substitutes.
- Require every production-write child context to name exact no-cache/no-output verification commands that preserve the zero ignored/untracked handover invariant; never delete generated verification output to manufacture a clean handover, and keep buckets parent-owned or sequential when a required check cannot satisfy the boundary.
- Preflight every exact production-write child verification command in a disposable clean worktree containing the planned postimages before hashing the plan; freeze only byte-identical passing command text, and require a new digest and approval after any command change.
- Advance new code-review specialist artifacts to schema 3 with packaged role-card hashes, unique context/output paths, strict trigger reasons, explicit Sol-selection provenance, and shared runtime evidence; retain schema 2 only for bounded historical reads.
- Add a shared execution-mode resolver for `--execution=serial|parallel-read|parallel-write|auto` with explicit flag over `CODEX_RIG_EXECUTION` over shipped-default precedence; staged releases remain serial by default, and no mode grants digest-bound write approval.
- Add privacy-minimized telemetry helpers for HMAC identifiers, cumulative token accounting, explicit dispatch-to-final-join timing, and matched workload comparisons; never derive savings from child-duration proxies or retain prompts, messages, paths, credentials, or raw runtime identifiers.
- Render remediation tables with compact ordered report file-and-line, report JSON-and-finding-ID, or online stable-ID pointers plus under-table symbols for longer summaries, resolutions, evidence, and next actions, while retaining complete source details in metadata and expanded ledger records.
- Reject assessed PR handoffs whose compact snapshot omits or replaces the `Suggestion` row, and bind its `approve`, `minor changes`, `needs work`, `reject`, or `not aligned` value to the validated structured review decision.

## 0.12.1

- Allow bare pull-request remediation targets to collect current online PR items and a verified local checkout without requiring a prior assessed review artifact; explicit `+review`, report aliases, and report paths retain report-plus-online intake.
- Recover newer same-session `code-review` candidates for `+review` only after review-specific and shared validation, preserving exact validator failures and preventing stale-report fallback; preflight specialist manifest attempt cardinality before candidate creation.
- Make Claude/Codex selection affect only host scope: Codex sync now removes managed plugins before reinstalling by default, always refreshes the marketplace by upgrading Git registrations or replacing non-Git registrations with the canonical Git source, and treats `--no-clean` only as the uninstall opt-out.

## 0.12.0

- Add a deterministic post-gate final-handoff renderer and validator for all 13 artifact workflows, with exact per-skill table schemas, complete source coverage, gate/confidence reconciliation, remaining-owner actions, caller-controlled exact output, and digest-bound final bytes.
- Render final-handoff section and table labels as compact portable Markdown bold text; omit heading syntax and ANSI color so terminal, saved-Markdown, and log output remain consistent.
- Keep commit verification sections concise and change-specific; omit exploratory, repeated, superseded, diagnostic, and unrelated gate history while retaining final acceptance evidence.
- Make new result candidates schema v2 and block promotion when final-handoff files, digests, branches, workflow rows, remediation provenance, review terminal rules, or rendered bytes disagree; retain read compatibility for historical schema-v1 results.
- Migrate every workflow result template, skill output contract, shared lifecycle document, calibration helper roster/selftest, package metadata, and acceptance test to the executable handoff lifecycle; retain `agent-shims` as the explicit non-artifact manager exception and disclose that chat transport itself has no host transcript hook.

## 0.11.0

- Centralize generic final-chat and network-approval mechanics in their existing shared contracts while every skill retains its outcome vocabulary, exact result schema, terminal branches, five operation-specific approval values, and recovery exceptions.
- Dispatch approved independent specialist workstreams in one wave only after routes and immutable narrow context packs are fixed; join every handoff before parent acceptance, preserve serial fallback with equal gates, and forbid a second wave, added fan-out, overlapping ownership, approval bypass, or premature dependent work.
- Add regression and behavioral-calibration coverage for the bounded specialist wave and shared network-contract ownership, retain review/remediation scope and exact-plan approval boundaries, and document the whole-plugin efficiency contract.

## 0.10.1

- Restructure dense README policy, sync, PR workflow, and verification passages into labeled lists, a comparison table, and explicit safety blockquotes while preserving commands, caveats, and remote-mutation boundaries.
- Reformat the code-review and code-remediation approval, evidence, fallback, failure, findings-intake, and specialist-routing contracts into atomic steps with concise workflow headings; retain validator-facing literals, retry limits, and workflow behavior.

## 0.10.0

- Standardize every workflow and the agent-shim lifecycle helper's final chat as an outcome-first, structured handoff with skill-specific result tables, exact verification, remaining obligations, prioritized recommendations/next steps, confidence limits, and supplemental artifact links.
- Make code-remediation work buckets mechanically bounded and complete: at most five selected items per bucket, one agent scope for five or fewer items, exact non-overlapping ownership, supported role/context evidence, no one-specialist-per-finding fan-out, and explicit plan-digest-bound approval before useful parallel execution.
- Reconcile the durable code-remediation table against an ordered per-item machine ledger so aggregate counts cannot conceal an omitted or changed disposition; grouped duplicates retain every `report|online` source ID, location, complete body, and evidence path in both durable and final-chat tables.
- Give each code-remediation interaction one rendering owner: visible scope and work-plan messages contain context only, while the selection or approval control exclusively presents its question and choices; runtimes without controls use one plain-text fallback instead of both channels.
- Add regression and behavioral-calibration coverage for duplicate scope-selection and parallel-approval prompts.
- Add focused artifact-validator, output-contract, behavioral-calibration, and package-contract coverage for the new reporting, source provenance, and batching behavior.

## 0.9.2

- Keep ordinary Claude synchronization from invoking Bridge's newly state-capable setup skill or supplying an approval token; other managed plugins retain their existing headless setup dispatch, and executable regression coverage pins the separation.

## 0.9.1

- Add a recurring `audit` value-per-token axis with matched cost measurement, loaded-reference accounting, obligation mapping, deterministic and paired-live value guards, adversarial review, and fail-closed acceptance when evidence or material savings are missing.
- Validate the existing audit ledger plus the new prompt-efficiency artifact, calibrate four overcompression/cost-evidence failure modes, and make specialist-policy loading explicit only on triggered non-PR paths.
- Artifact validation now fails closed for audit run directories missing `audit-ledger.md` or `prompt-efficiency.md` with their named sections; run directories produced before this release no longer re-validate.
- Package-manifest generation excludes runtime `.reports/` artifacts, so a local calibration run followed by a manifest refresh can no longer sweep machine-local report paths into the tracked manifest.
- Deduplicate run-directory boilerplate against the existing helper contract without changing literal commands, artifact paths, PR terminal workflows, or fail-fast vocabularies.
- Separate detailed pre-briefs from compact runtime reasons for every intentional approval request: ask only about the outcome or material effect, never duplicate command syntax or detailed context, use only short categorical safe prefixes when justified, and omit persistent prefixes for one-time or high-risk commands. Multiline commits apply the rule with a reviewed private temporary message file and `rtk git commit --cleanup=verbatim -F <file>`.
- Run Bridge's installed free static doctor during Codex sync, failing closed when the MCP `python` launcher is older than 3.10 or the Claude CLI help contract is incompatible; never invoke a model, authenticate, or treat the check as per-session MCP inventory proof.

## 0.9.0

- Replace the legacy external Claude Codex plugin lifecycle with the managed `bridge` plugin, including install, enabled-version verification, teardown, one-time setup dispatch from its `claude-skills/setup/SKILL.md` entrypoint, and success-gated cleanup of the retired plugin.

## 0.8.3

- Update the live Codemap-py consumer reference to the `codemap-py.integration.v2` managed-block body protocol and `integrate audit` command. The separate structural-context adapter remains `codemap-py.integration.v1`.

## 0.8.2

- Add a compact, artifact-refreshed PR snapshot to every assessed `code-review` PR report and chat handoff: number/link, author, GitHub check state, verified-intent type, and mapped merge suggestion. The collector now retains `statusCheckRollup`; unavailable check evidence remains explicit rather than being presented as passing.
- Specify that `code-review` routing evidence and triggered-role reasons must be non-empty JSON string arrays, preventing valid-looking string values from failing artifact validation and stranding a review as `result.candidate.json`.

## 0.8.1

- kaggle skill: add a mechanical scan step for bare `#`/`##`/... heading-spacer lines inside `# %% [markdown]` cells (style-rules.md rule 08) — prose compliance alone proved insufficient in practice.

## 0.8.0

- Rename the `develop` workflow to `implement` and `analyse` to `change-analysis` across skill identities, invocation names, artifact namespaces, routing, calibration, package metadata, and documentation. This release intentionally provides no aliases or compatibility paths for the former names.
- Apply one canonical five-field approval brief to every shipped networked shell-CLI boundary; denial stops the current tool call without running the external command, equivalent reprompt, or broader fallback, requires a new user message to continue, and keeps separate capabilities as separate approvals.
- Add a fail-closed, standard-library-only App Server denial transcript validator and focused protocol tests covering callback correlation, exact request/lifecycle command identity, declined primary terminal states, no output or fallback execution, fresh-turn recovery, and atomic success or bounded sanitized failure evidence only after the process cleanup attempt, without contacting a host or network service. A separately authorized live matrix runs text-only and installed-skill-input controls before denial, requires isolated non-overlapping roots plus one independently recorded full-package manifest digest across every row, verifies every declared payload before launch, stops on the first failure, and retains only sorted allowlisted error categories, the first specific category, whether any retry occurred, and the final retry state. Prior Codex-home use remains an explicit operator precondition because the probe cannot infer it safely from contents.
- Add an installed-package-safe acceptance gate that copies only manifest-declared payload into a disposable cache and runs explicit package-safe tests without source-checkout context; the complete suite exercises these gates across Linux, macOS, and Windows with Python 3.10–3.13, while live App Server candidate binding remains a separately authorized manual probe and does not claim desktop-UI equivalence.

## 0.7.6

- Skip Git-only `codex plugin marketplace upgrade` for an existing local marketplace, then reconcile the managed plugin set from that configured local snapshot. Git marketplace refresh behavior is unchanged.

## 0.7.5

- Spawn every shared calibration helper through the running Python interpreter instead of executing the `.py` file directly, so the offline harness runs on Windows: `CreateProcess` cannot execute a script by shebang, and the write-result, find-review-report, and validate-artifacts selftests aborted the whole run with `OSError: [WinError 193]` before any result artifact was written.
- Bind a fixture gate command that starts with a bare `python` or `python3` token to the interpreter already running the calibration, removing the dependency on a `python3` entry in the child process PATH.
- Point the Windows spelling of the isolated harness home at the same temporary directory as `HOME`. `ntpath.expanduser` reads `USERPROFILE`, then `HOMEDRIVE` plus `HOMEPATH`, and never `HOME`, so `env -i` left `~` unresolvable and the code-review validator's `--help` exited non-zero on `Path.home()` alone; without the isolated spelling a helper expanding `~` would have reached the real user profile instead.
- Resolve the code-review validator's Codex-home fallback only when `CODEX_HOME` is unset, matching the sync and role-manager helpers. The argparse default previously called `Path.home()` on every invocation, including those that supplied the variable.
- Two Windows gaps stay open and are deliberately out of this change. The paid live A/B gate runner still splits a bare `python3` token and terminates through `os.killpg`, both POSIX-only; that path is blocked in CI and unreachable offline. The offline harness writes its command blockers as extensionless shell scripts, which `CreateProcess` skips, so on Windows the harness isolates by empty credentials rather than by blocked commands.
- Record which index file answered each structural-context query, and report a disagreement between that path and the one the health probe resolved as evidence in the artifact rather than reconciling it. A run whose answers were complete and fresh keeps its `available` status; both paths are retained so the disagreement is diagnosable. A Codemap that reports no path yields `null` and no claim, so an older provider still works. Structural-context artifacts are now schema version 3.
- Report an honest structural-context status when no `--target` is supplied: a standard category batch now omits its target-requiring queries instead of failing them, so a targetless `analysis` or `develop` probe can reach `available` rather than always reporting `degraded`. A category whose every query requires a target keeps its bounded error, and an explicit `--query-kind` fact route still degrades on a missing or malformed target.
- Compose coexisting structural-context caveats into the single new status `stale+degraded` instead of letting a stale index mask a coverage gap; `stale` and `degraded` keep their existing meanings when only one condition holds, and per-query completeness flags are unchanged.
- Record each skill's Codemap route selection in the contract so the default category batch reads as a deliberate per-workflow choice, and pin that record against the skills themselves with a drift test.

## 0.7.4

- Accept canonical `Review Findings and Merge Blocks` sections by using regex control sequences as regex tokens instead of matching their backslash spellings literally.
- Derive mechanical review tier and evidence through one shipped routing helper shared by the code-review producer and validator, eliminating model-authored file/line arithmetic drift.
- Keep shell network access blocked by default while requiring scoped external-network approval for the complete command owning every intentional `gh` or `kaggle` call, GitHub collector fetch/HTTPS path, Codex Git marketplace add/upgrade, and paid live `codex exec` calibration.
- Keep missing Kaggle CLI installation and authentication user-owned: Codex Rig reports the prerequisite and never runs or authorizes an installer.
- Require the complete PR collector command to own approval for its nested GitHub CLI, HTTPS fallback, checkout, and Git fetch traffic instead of approving a standalone `gh` command.
- Retry one agent-caused, pre-approval sandbox-shaped `github-network` collection failure through the runtime approval mechanism before reporting review or remediation evidence unavailable; a user denial always stops the turn and forbids that retry.

## 0.7.3

- Require abstractions to reduce reader-visible concepts, keep ordinary Python imports at module scope, and prefer concrete fixture state plus ordinary helpers over nested fixture factories or meaningless aliases.
- Calibrate nested fixture builders, redundant fixture aliases, unjustified local imports, and incomplete helper extractions that hide scenario inputs or leave sibling behavior duplicated.
- Keep Terra as the normal parent/session and require explicit user selection before either Sol-pinned architecture or security role runs; Sol passes are bounded read-only advisories that return evidence and final acceptance to Terra.

## 0.7.2

- Add `$code-remediate review` for current-session report-mode remediation: reuse the latest assessed `code-review` artifact without refreshing PR evidence or online comments, and fail closed when that artifact is unavailable.
- Add an evidence-backed PR `close` gate before detailed code review, with eight explicit reason codes, false-positive safeguards, blocking-default guidance, validator-enforced terminal artifacts, and remediation rejection for closed results.
- Require every proposed or created Codex commit to include complete `Changes`, `Impact`, `Verification`, and `Residual limits` sections so the commit itself preserves behavioral scope, concrete effects, executed evidence, and remaining uncertainty.
- Restore calibration coverage for the shipped escalation-ledger CLI by registering it in the shared helper self-test roster and restoring its executable package mode.

## 0.7.1

- Add a bounded public HTTPS PR metadata fallback through the allowlisted, read-only `github_read.py` boundary for `github-network`, `github-auth`, `github-rate-limit`, and `command-timeout` failures; raw GitHub CLI stderr remains unpersisted and terminal diagnostics may include a safe `failure_reason` enum.
- Distinguish GitHub GraphQL object-resolution failures from DNS errors, and recover an available system CA bundle when Python's default HTTPS trust store is empty.
- Require canonical PR URLs to match a configured GitHub remote and numeric targets to resolve to one distinct configured GitHub repository identity; ambiguous, unsafe, permission, not-found, and unclassified cases remain fail-closed.
- Normalize limited public PR metadata, verify the `refs/pull/<number>/head` detached checkout and local diff, and list unavailable evidence in `online-review-summary.json` while preserving private-PR and open-only merge/remediation constraints.
- Require review/remediation online triage and action evidence to list sorted fallback evidence IDs, add the exact public-fallback confidence gap, and cap final confidence at `0.89`.

## 0.7.0

- Add task-neutral adaptive Codemap routing to the shared structural-context adapter: localized edits can record a zero-query `skip`, one unresolved structural fact selects one compact query, and broad or unknown scope retains the legacy `standard` batch.
- Persist `query_kind` and `artifact_schema_version: 2` while retaining the provider protocol `codemap-py.integration.v1`; add truthful `skipped` status and explicit target normalization rules.
- Keep Codemap optional and persist each decision once so specialist passes consume one artifact without re-querying.

## 0.6.1

- Ground the `kaggle` workflow through the authenticated Kaggle CLI: probe availability and credentials separately from rules acceptance, then read the real file listing, leaderboard range, and sample submission instead of a login-walled competition page.
- Rank CLI evidence above the fetched page for file names, data schema, and submission format while the page stays authoritative for problem narrative and metric definition.
- Suggest user-owned CLI installation and token setup instead of failing when the CLI is absent or unauthorized, and record degraded grounding as a residual limit.
- Fail any run that downloads a full competition or dataset archive without first listing file sizes and asking.

## 0.6.0

- Detect two consecutive work cycles with no material progress and require a ledger rather than subjective model-stall judgments.
- Escalate after three evidence-backed attempts when they still leave one closure condition unmet, so incremental progress cannot conceal an unresolved task.
- Escalate once for higher-capability advice under existing model/authority boundaries, permit one bounded recovery action, then stop for a user decision when progress still does not occur.
- Keep advisory escalation distinct from repeated-obstacle handling: it never resets recurrence counts, bypasses root-cause evidence, transfers acceptance authority, or routes bounded support to Sol.
- Persist and validate the bounded escalation ledger before any post-trigger cycle; reject incomplete state, repeated retries, and unsuccessful recovery without a human handoff.
- Require an observed read-only sandbox and no state changes for advice-only routing; unverified or unavailable routes now hand off directly to the user.
- Calibrate advisory escalation, post-escalation user handoff, user-directed material progress, and advisor-route safety with scored fixture observations.

## 0.5.1

- Classify a refreshed target branch as `advanced` when the PR-recorded base remains its ancestor, then continue reviewing the exact verified PR head; only genuine target divergence remains a collection failure.
- Validate the same ancestry evidence in code-review and PR code-remediation artifacts so target advancement is operational context, never a PR finding or false merge blocker.
- Restore the intended PR evidence hierarchy: retain PR title/body and current-attempt diagnostics, use numbered fork-aware checkout or exact-HEAD reuse, and derive the authoritative review patch from the verified local checkout.
- Degrade unavailable GraphQL review-thread resolution status to explicit empty artifacts plus a confidence gap instead of aborting source review; keep core identity, target, checkout, and local-diff failures terminal.
- Report terminal collection failures as plain process diagnostic/recovery/evidence prose with source findings not assessed and no merge decision; forbid all Markdown tables so integration failures cannot look like PR issues.

## 0.5.0

- Add `shared/github_read.py` as the plugin-wide GitHub data boundary: authenticated `gh` is primary; only audited built-in view groups (`gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, `workflow`) are permitted; REST API calls are GET-only; and GraphQL accepts queries but rejects mutations.
- Route PR collection through that shared boundary while retaining `gh pr diff` and local-only `gh pr checkout` for PR workflow completeness.
- Add a last-resort unauthenticated `urllib` GET fallback restricted to public `https://api.github.com/repos/...` resources; it never reads tokens/keychain state and private-only evidence still fails closed.
- Keep GitHub CLI diagnostics credential-opaque: never run `gh auth` or persist CLI stdout/stderr on failure; artifacts retain only command label, failure class, and exit code.
- Clear prior collector evidence and terminal failure markers before each retry so attempts never mix source evidence; keep rate-limit recovery user-timed because opaque artifacts intentionally retain no server interval.
- Report failed PR collection as `PR Review Availability: unavailable`, with no source finding or merge decision, rather than misclassifying an unperformed review as `needs-more-work`.
- Make the unavailable-result writer omit normal recommendations and follow-up fields, preserve conservative `checkout-state.json` evidence when a local checkout command fails, and validate that end-to-end artifact branch.
- Keep assessed merge-review and remediation strictly open-PR-only: an advanced or diverged base remains fail-closed for open PRs, while merged or closed PRs may be collected only as raw diagnostic evidence through GitHub's pull ref, exact SHA verification, and detached local checkout.
- Bound production `gh` command memory use with spooled output buffers, reject oversized responses before exposing them to callers, and keep calibration fixtures aligned with the stricter result and PR-identity contracts.
- Keep Codex Git marketplace add/upgrade as its explicit non-`gh` lifecycle exception because it refreshes the snapshot used to manage local Codex plugins.

## 0.4.8

- Require every `code-review` `needs-more-work` result to include a validated `Review Findings and Merge Blocks` table with the affected area, exact pre-merge change, evidence, and actionable status; reproduce that table in the final review summary.
- Require the same table when PR evidence collection fails before source review, while explicitly marking source findings as not assessed rather than inventing a code finding.
- Require the table for every non-`accept-as-is` PR decision, including minor changes, rejection, and not-aligned outcomes; each row names a finding or operational blocker.

## 0.4.7

- Make repository Codex sync install, verify, and remove Codemap alongside Codex Rig while keeping Codex Rig as the sole owner of the managed global instructions block.

## 0.4.6

- Require evidence-based model-difficulty routing: use Luna only for bounded support, Terra for behavior and executable verification, and Sol only for architecture or security; record concrete escalation or de-escalation evidence and never route on cost alone.

## 0.4.5

- Replace bare option strings with named `(str, Enum)` types: `SyncAction` in `sync_codex.py`, and `ResultStatus`, `ClosureStatus`, and `RecoveryStatus` in `write-result.py`. `argparse` now derives `choices=` from the enum instead of repeating the literals, so the CLI surface and the accepted values cannot drift apart. Accepted CLI values and emitted output are unchanged.

## 0.4.4

- Prefer maintained standard-library, native-platform, and already-installed package functionality over duplicating custom code; reject complexity justified only by hypothetical future states, risks, scale, reuse, or edge cases; preserve trust-boundary, data-loss, security, accessibility, and explicit-contract safeguards; record a deliberately bounded simplification's present ceiling and observable revisit trigger.
- Require descriptive user-facing commit handoffs with each hash and title, behavioral impact, affected surfaces, exact verification evidence, residual limits, and the rationale for multiple-commit boundaries.

## 0.4.3

- Keep compact `investigate` and `sync` routing descriptions aligned with the offline calibration contract.

## 0.4.2

- Name the review, test, and toolchain owners in the `oss-shepherd` role card and state that its handover drafts stay advisory text rather than applied changes.
- Record in `shared/native-skill-contract.md` that `agent-shims` is absent from the calibration skill roster, so required-section, `result-template.json`, and canonical result-artifact checks do not run against it.
- Assert manifest identity relationally in `test_installed_cache_scaffold.py` — both shipped manifests must agree and the release must appear in this file — instead of pinning a version literal that broke on every bump.

## 0.4.1

- Require root-cause investigation when the same or plausibly shared obstacle occurs a second time, even when its surface symptom changes.
- Stop after a third occurrence and ask the human with attempted actions, current hypotheses and evidence, and a concise description of the recurring obstacle.
- Enforce recurrence-policy references only at recurrence-owning workflows (`develop`, `code-remediate`, `investigate`, and `delegation-lead`) with calibrated behavioral cases.

## 0.4.0

- Add optional codemap-py structural-context integration: `shared/codemap_adapter.py` probes the public `codemap-py doctor --json`/`query` CLI once per decision point in `analyse`, `audit`, `code-review`, `code-remediate`, `develop`, `investigate`, `optimize`, `release`, and `research`, and persists the result to the run artifact instead of re-querying per specialist.
- Document the `codemap-py.integration.v1` protocol, named status vocabulary (`available`/`absent`/`stale`/`incompatible`/`degraded`), category-to-query map, and the five not-applicable skills (`manage`, `sync`, `agent-shims`, `calibrate`, `kaggle`) in `shared/codemap-contract.md`.
- Keep the integration symmetric and optional: Codex Rig never imports `codemap_py` or requires it installed; absence/incompatibility falls back to normal bounded file inspection.

## 0.3.0

- Add native Windows package verification, read-only shim diagnostics, SessionStart execution, and explicit CI acceptance.
- Replace Bash-only workflow execution with canonical Python diff, PR, gate, run-directory, and Codex sync entrypoints; remove redundant POSIX compatibility wrappers.
- Preserve exact POSIX mode enforcement and authenticated shim cleanup while treating modes and shim mutation as explicitly not applicable on Windows.
- Freeze the audited Windows skip surface and reject private Windows user-profile paths from published package bytes.
- Keep extensionless package identity files LF-stable and resolve validated Windows batch launchers during Codex sync.

## 0.2.4

- Accept protected current-user Codex agent directories without changing their permissions, while keeping lifecycle state private.
- Align executable validation with the package-wide 512 MiB bound and report exact failed invariants.
- Make SessionStart and `agent-shims` diagnostics explain the first cause, confirm zero writes, and provide safe next steps.

## 0.2.3

- Add intent-first target-merge conflict resolution to PR remediation, with explicit merge-commit authorization and fail-closed completion evidence.
- Add scoped `sync.sh clear` teardown for Claude and Codex plugins plus authenticated removal of the managed global-instructions block.
- Keep package identity, release documentation, and acceptance checks synchronized with the plugin version.

## 0.2.2

- Make Codex Rig the canonical source for workflows, role cards, lifecycle contracts, calibration, and public product documentation.
- Document exact blank-agent role injection, inline fallback, model-control limits, lifecycle behavior, and lessons learned from the original named-agent design.
- Replace repository-to-home `.codex` copying with public GitHub plugin installation.
- Follow the GitHub default branch by default while retaining an optional immutable release-tag pin.
- Ship generic Codex guidance as inert `assets/AGENTS.md`; repository sync installs or updates its backup-protected managed block by default whenever Codex scope is active, with `--no-codex-global-agents` opt-out. Direct plugin installation and Claude-only sync leave global and project instructions untouched.
- Require exact, explicit authorization before any amend, rebase, reset, squash, fixup, or equivalent history rewrite.

## 0.2.1

- Package 13 Codex-native workflow skills, one experimental shim manager, and 15 canonical specialist role cards.
- Support parallel blank-agent role-card injection with inline fallback when spawning is unavailable.
- Preserve transactional, exact-approval diagnosis and cleanup for prior thin user-agent shims on supported POSIX local filesystems; block new installation until runtime selection is verifiable.
- Add a trust-gated, read-only SessionStart shim-health diagnostic.
- Keep MCP and native plugin-bundled agent registration out of scope.

Known limit: standalone shim installation proves ownership and link integrity, not selection by the active collaboration interface. Runtimes without an explicit custom-agent selector use blank-agent role injection.

## 0.1.0

- Establish the Apache-2.0 Codex plugin package, deterministic inventory, portable workflows, and canonical role cards.
- Define the thin-shim safety contract, authenticated installed state, and reversible transaction foundation.
- Introduce role-card fallback routing while native custom-agent selection remained unverified.
