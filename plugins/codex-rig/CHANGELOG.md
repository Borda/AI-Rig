# Changelog

## 0.34.1

- Read the review checkout once, not twice, when Code Review prepares source batches: `review_batches.validate_inventory` takes an optional `verified_source`, and only `prepare_source_batches` passes the snapshot it verified moments earlier (about 84 → 42 Git calls per `prepare --batches`). Every other caller, including later phases and the independent validator, still re-reads the checkout in full, so a change made after preparation is still rejected there; a change made during preparation's own context build is now caught by the next phase instead of by preparation itself.

## 0.34.0

- Ship generic engineering policy in the global `assets/AGENTS.md` template instead of leaving it in a repository-only root file: internal record types, docstring opening line, Markdown authoring, multi-OS executables, notebook authoring (pointer to `shared/notebook-style.md`), pytest parametrization, marker and xdist isolation rules, pre-commit-over-bare-tool, benchmark and plan isolation, the lossless instruction-compression gate, and a pointer to `shared/adversarial-loop.md` for review → fix cycles. Plugin-only installs previously never received these rules.
- Cut the always-loaded global `assets/AGENTS.md` from 58,078 to 38,818 bytes by moving activity-specific detail verbatim into the new on-demand `shared/global-baseline-details.md` (instruction-compression gate, reasoning-progress escalation, runtime effort assignments, Markdown authoring, multi-OS executables, module documentation, test authoring, networked CLI and GitHub read routing, pytest sandbox rules, docstring resolution, delegation criteria, team patterns, patch handovers). Each moved section keeps a trigger line in the template naming the section to read first; approval, remote-mutation, commit, untrusted-content, confidence and human-in-the-loop rules stay inline. A packaging test pins the template byte budget and that every pointer target ships and every details section is referenced.
- Make the global block's packaged `shared/<file>` pointers resolvable outside this repository: `install_global_agents.py` renders the absolute installed Codex Rig root into one new template line defining `PLUGIN_ROOT` (new `--plugin-root`, default the package containing `--source`; sync passes the installed cache root and omits it for older templates), and the template byte budget rises by that line's 217 bytes.
- Flag a global block a direct plugin update left behind: the SessionStart hook reads `CODEX_HOME/AGENTS.md` through the installer's new read-only `authenticated_managed_body`/`rendered_plugin_root` parser and warns, without writing, when the authenticated block names another Codex Rig root than the running plugin or no root at all (every block rendered before this release), showing only each root's last component and the re-render command. The README's direct `codex plugin add` update path now re-renders the block from the installed package, and the sync skill's global-instruction `--check` names the installed package as `--source`, since a marketplace-checkout template misreports a current block as `stale-managed-template`.
- Prefer structured terminal summaries with short bullets, counts and evidence links. New remediation handoffs show every finding in a compact Selected/Outcome table; preserve detailed reasons in the full report and historical report rendering.
- Keep remediation selection compact in the terminal: complete table, available PR Relevance Summary, then report link. Preserve per-item context, closure criteria and evidence in saved reports without echoing their detail blocks.
- Make rule 43a an automatic finding-fix sequencing guard. Explicit user commit requests can proceed after required checks before final report validation, then resume the report; earlier checked target integration retains its own sequence.
- Validate report obligation occurrences before selection and frozen remediation plans before edits. Permit coherent small sequential multi-role plans; derive final status counts from folded items while retaining independent final validation.
- Derive duplicated review action content from canonical findings/blockers and retain structured finalizer diagnostics with actionable recovery. Continue source-confirmed remediation when requested report proof is incomplete, unless completed review was explicitly required before edits; keep failed admission and independent coverage limits visible.
- Treat a local merge as task-owned only when the run wrote its `status=in-progress` merge record before starting it and that record still matches `HEAD` and `MERGE_HEAD`; a merge the user started stays a user decision, and continuing the same task-owned merge reuses the authorization recorded when it started. Review the entire merge index, including clean merged-in paths, and verify merge parents, the index tree captured at review time before the commit command, and all first-parent changed paths.
- Complete an approved fresh review as a nested workflow when PR remediation requests `+review` without a report; return the exact validated report and continue the same remediation through its remaining gates, without a review-only final answer or another resume request. A nested review that completes but cannot be admitted keeps its terminal status: `closed` ends remediation, while `unavailable` or a supersession rejection offers one continue-or-stop choice and never reruns the review.
- Route proved reviewer dispatch-order failures to the existing source-verified parent fallback without redundant GitHub collection; preserve approved nested-review continuation, explicit independence, denial and retry limits. Remediation that admits the resulting `serial-fallback` report carries its coverage limit as a named residual limit, never as independent review coverage.
- Require the Codemap structural probe whenever `code-review`, `code-remediate`, or `implement` changes or targets Python (`.py`) files the provider can map; untracked-only and stub-only changes are outside the reviewed diff file. The provider stays optional: an `absent` or `incompatible` probe still persists `codemap-context.json` with its status and reason, and the workflow continues with bounded inspection. Code Review's validator now rejects a fresh Python-diff candidate whose artifact is missing, invalid, or `skipped`; promoted historical results remain readable.
- Replace the rule that specialists never query Codemap with a bounded one: a specialist may receive at most three targeted follow-ups (`callers`, `dependencies`, `test-impact`) after a usable probe, written as `codemap-followups/<role>-<NN>.json` and cited in its own evidence. Read-only reviewers have the parent run them before their context is prepared. Code Review's validator binds each follow-up to a role the review's routing triggered and enforces the naming, routes, category, target, provider precondition, and a status re-derived from the recorded query. The adapter exposes the status vocabulary and follow-up limits as public constants.
- Fix hollow review structural evidence: the review batch's `diff-impact` now reads the reviewed `--diff-file` instead of diffing a detached review worktree against itself, the adapter runs Codemap from `--root` so a nested worktree no longer opens the parent repository's index, and relative `--root`/`--diff-file` resolve against the caller's directory first. Context artifacts move to schema 4: each query keeps the provider's bounded answer (for `diff-impact`: changed modules with risk, unmapped files, test impact; lists capped with original lengths recorded) plus its `completeness_reason`, `root_mismatch`, and `note`, and `status_reasons` names every gap. A change set whose Python files all map to no module, or a diff that deletes Python modules (including a rename's old path), is `degraded` with a reason naming them, never `available`; a deletion-only diff previously persisted `available` with `LOW` risk. A provider failure keeps the provider's own error text; input the provider itself diagnoses (exit 2 with its JSON `error`) degrades and records `input_rejected`, while a bare exit-2 usage error marks the provider `incompatible`. Code Review's validator requires schema 4 for new candidates and re-derives status, reasons, and gap from the recorded query and the run's own `diff.patch`, which the query must have read; a Python diff answered with zero changed files and no reason fails. A run restored by native-provenance recovery, proven by both recovery files, keeps its schema-3 artifact or records `codemap_context: "not-required-historical"` instead of a retroactive probe.
- Record a Codemap follow-up whose target the provider refused — not found, ambiguous, a module where a function is required, or a module not indexed — as `degraded` with `input_rejected`, keeping the provider's `candidates`, `suggestions`, and `rejected_target` in the query's `answer`. Such follow-ups were recorded as an `incompatible` provider, which ends every further follow-up, and the candidate list was dropped. Provider-side failures sharing those exits stay `incompatible`, and an index that failed to load now keeps the provider's own error text instead of "target not indexed".
- Keep the provider's zero-caller method `hint` in the follow-up answer and name it in `status_reasons`; it was stripped, so `called_by: []` for a method reached only through instances read as unused. A `test-impact` follow-up now carries the same hint when the provider's walk found no caller, so zero test files no longer reads as "untested".
- Document the review probe's `diff-impact` disclosure for a changed method or constructor with `caller_count: 0`: the provider's per-entry and top-level `hint` reach `status_reasons`, and its `not_covered` makes that artifact `degraded`; such a change previously persisted `available` with no reason. A change touching only test methods carries no hint and keeps its status. Contract wording now notes that a protocol (dunder) method's hint names no reference search, and that each hint opens with its action so the 300-character answer bound keeps the search or the do-not-delete instruction.
- Re-derive each recorded query's `input_rejected` flag in Code Review's validator from its exit code and recorded `answer` (`provider_rejected_input`), failing `codemap-context-invalid:input_rejected` or `codemap-followup-invalid:<name>:input_rejected` on a mismatch. The validator trusted the recorded flag, so a provider-side failure hand-marked as rejected input passed as `degraded` and admitted follow-ups after an unusable probe. A failed query whose JSON error held only its text now records `answer: {}` instead of `null`, which is what tells a provider-diagnosed exit 2 from an argument-parser usage exit.
- Pass dotted and bare `callers`/`blast` targets to the provider, which resolves a unique one and lists candidates for an ambiguous one. A supplied but malformed target (an empty side around `::`, or more than one `::`) is recorded as `target malformed`, never as "target required, none supplied".
- Unquote git C-quoted paths (`"pkg/mod\303\251.py"`) in the review's `files.txt`, diff headers, and `rename from` lines, so a non-ASCII Python path no longer skips the required probe or hides a deleted module.
- Shrink PR worktree preflight records (`source-worktree-context.json`, `worktree-preflight.json`) from every local dirty path — about 20,000 ignored environment files and 1.6 MB per observed review — to the overlapping paths plus at most 50 sampled others, with per-class counts (`tracked`, `staged`, `untracked_unignored`, `ignored`, `total`) and an omitted count. The overlap check still compares every path, including ignored files, so an ignored file colliding with a PR path still blocks checkout. Records carry `schema_version: 1`; unversioned historical records remain readable.
- Add behavioral calibration cases for a Python-diff review that omits the probe and for one that records an absent provider with its reason.
- Close "package not installed" review gaps instead of documenting them: when a claim's verification run is blocked by a Python package absent or outside the reviewed head's declared specifier, Code Review unpacks the matching wheels into a run-local `dep-overlay` directory (`uv pip install --target … --no-deps`) and runs the selected tests with that overlay first on `PYTHONPATH`. Only a package name the review base already declares qualifies, with the reviewed head's specifier; a name the reviewed head newly adds becomes a `new dependency <name> — verify provenance` finding, never an overlay, and a name from PR prose or code never qualifies. A wheel-only dry-run on the default index (`--only-binary :all: --no-config --no-cache`) checks support first and lists the overlay set, sources are never built, the project environment must be unchanged before and after, and the evidence line records package, version, wheel, base and head declaring `file:line`, fetch and run commands, result and import origin. No wheel, an unreachable index, a timeout or a denied approval keeps the gap with that concrete reason. The fetch executes no code and is the only step that may receive a one-time network approval for its exact command; the test run never shares it and, like a non-test script, runs only in the sandbox, spelled `-B -m pytest` so the reusable pytest approval never covers downloaded wheel code. The head's specifier must be a plain PEP 440 version range: a direct reference, URL, path or quote becomes a `non-index dependency reference <name>` finding with gap reason `head specifier not a version range`, never a fetch. Both `uv` commands pin `--default-index https://pypi.org/simple`, and the overlay fails closed on any other routing: a name the review base routes elsewhere (`[tool.uv.sources]`, a `[[tool.uv.index]]` entry not marked explicit, `[tool.uv]` or requirements index options, a lock-file registry other than the default, another tool's source table, a direct reference or path) or the user routes elsewhere (index environment variables, a user- or system-level `uv.toml` index, a `pip config list` index), or whose routing cannot be determined, is never fetched and keeps the gap `non-default index dependency <name>`, checked for the requested name before the support check and for every other overlay-set name before the fetch. Routing is read from every place it can live, not a fixed file list: a uv workspace root's `pyproject.toml`, every requirements-format file and nested `-r`/`-c` include, `Pipfile` and `Pipfile.lock` sources, and every user, global and environment pip configuration file even when the environment has no pip; a content scan of the whole review base then treats any index setting outside those parsed sources, such as `UV_INDEX_URL` set only in a CI workflow or a Dockerfile, as undetermined routing. A user's index setting is never cleared to reach the default index instead, since a public project with the same name would then be downloaded and executed. Any `UV_*` environment variable outside an allowed set of cache, display, linking and interpreter-discovery variables keeps the gap `uv environment overrides set: <names>`, because `--no-config` does not stop variables such as `UV_OVERRIDE`, `UV_CONSTRAINT` or `UV_TORCH_BACKEND` from changing resolution. Neither step is preapproved recipe or dependency-download work, and neither receives a reusable prefix.
- Add behavioral calibration cases for a review that documents a declared-package gap without closing it, mutates the project environment, and installs a package named only in a PR comment; for an overlay escalated as one download-and-test command under an index-only disclosure and approved as a dependency download; for a dependency the reviewed head newly adds, overlaid without a provenance finding; for a head direct-reference specifier copied into the fetch without a finding; and for a name the review base routes to a private index, fetched from the default index with the user's index variable unset and no gap, with a negative control that keeps the `non-default index dependency` gap without any index read; and for a name whose private index is set only in a CI workflow, fetched after a dependency-file-only check, with a negative control whose content scan keeps the gap.
- Score a behavioral case that expects no findings as correct (F1 1.0) when nothing is reported. Each clean negative-control case previously counted its whole confidence as overconfidence, so `mean_overconfidence` rose toward its threshold whenever a clean case was added; the shipped set moves from 0.147 to 0.001 with existing thresholds unchanged. Add a `min_negative_specificity` gate (0.9) on the share of negative-control observations answered with no finding, reported as `negative_specificity`: a reporter that flagged every negative control at high confidence still passed precision (0.94) and mean overconfidence (0.149), because positive cases dilute both means, and now fails `behavioral-negative-specificity`. A case file without the threshold keeps the permissive default. The same threshold also gates each observation source on its own, failing `behavioral-source-negative-specificity` and naming the source in `negative_specificity_failing_sources`: a partial campaign of five or six false alarms beside the shipped fixture negatives otherwise passed at 0% specificity for its own source.

## 0.33.1

- Overlap independent read-only Git queries in source-snapshot capture, review-worktree verification, and the local-source diff check, so a snapshot waits for its slowest query instead of the sum of all of them. Outcomes are settled in the original check order, so the first reported error and every race-guard comparison are unchanged; a measured local review-batch assembly test drops from 14.2 s to 8.1 s on macOS, with the larger gain expected where process creation is slow.
- Spawn fewer Git processes on hot paths without changing results or errors: release scope validation, parallel-worktree operation and HEAD/tree probes, and the source-snapshot top-level/HEAD lookup each batch read-only queries into one call and fall back to the original per-question calls on any failure or unexpected output.
- The test suite now requires pytest 9 or newer for its built-in `subtests` fixture.

## 0.33.0

- Split review execution validation and preparation tests into focused modules below the repository file-size limit, preserving validation behavior and existing helper imports.
- Route narrow implementation and management work directly; guide broader implementation through independent plan challenges, bounded native delegation, and verification against the accepted plan while keeping portable parallel-read and management-mutation boundaries distinct.
- Bind source-dependent local `working-tree` and `path` review gates to the existing verified collector mirror receipt and a fresh runtime snapshot, including admitted added files; keep clean PR and release source checks strict and historical gate logs unpromoted.
- Show each Codex confidence gap/limit contribution, reconcile deductions with the score, disclose zero-impact limits and unexplained residuals, and preserve historical artifact formats. Carry the rule in every standalone role card and enforce it during calibration. Generated calibration reports account for every existing score branch, including absent live-route evidence, without raising scores.
- Align generated native review calls with hosts that omit the agent selector, while preserving strict model, context, lineage and historical default checks. Validate opaque capacity refusals through exact controls, no-child evidence and joined capacity release rather than plaintext message equality.
- Recover a proved duplicated-interpreter page-read error with one complete independent replacement; bind opaque host delivery, retain original evidence and reject incomplete coverage. Current paged reads tolerate one proved missing-plan copy failure followed immediately by the exact same-page read; unchanged commands may omit the optional outer output-budget comment.
- Admit a single malformed outer JSON comment only with proved host rejection before execution, unchanged reader code, immediate canonical same-page correction and complete source receipts. Share the existing single-failure bound; retain raw evidence and reject altered or incomplete execution.
- Recognize a proved missing reader-file launch within that same bounded completed-read recovery. Require exact remaining arguments, matching exit-2 diagnostic, immediate canonical retry and complete source evidence; reject mixed or repeated failures.
- Align partial missing-reader recovery with native receipts that place the exact diagnostic wholly on stdout or stderr. Preserve strict command/error matching and require a complete independent replacement; reject mixed or extra output.
- Align both batched assessment consumers with unambiguous inline rating/rationale output while making future prompts explicit about separate lines. Reject malformed duplicate fields in all text assessments; preserve raw responses, scores and strict non-batch formats.
- Restore ordinary/main text assessment composition around one blank-line-bounded separate Rating/Rationale pair, preserving surrounding content while rejecting duplicate, malformed, fenced or ambiguous declarations.
- Extend bounded native representation recovery to an explicit rating and em-dash explanation missing the required rationale label. Preserve the original response and complete source evidence; require one exact tool-free correction without changing claims or relaxing ordinary validation.
- Specify reviewer-local finding IDs separately from qualified origin witnesses. Permit one proven namespace correction through the existing bounded native replacement, retaining original raw evidence, exact finding content and strict identity validation.
- Bound final review consolidation across serial native parts, retaining every report, origin witness and cross-group comparison. Validate the complete schedule and preserve all parts' findings, confidence gaps and adverse ratings; keep historical fitting aggregates readable.
- Allow schema-nine confidence above the immutable per-part minimum only through complete accounting of carried nonclosed gaps and residuals, admitted closure/reduction evidence, current unresolved deductions and score reconciliation. Preserve every constituent judgment, gap and actionable finding; validation checks references and accounting while the parent judges semantic closure.
- Version new batch generation and restore the full rating scale in interaction and final prompts. Reconstruct historical runs with source-proven templates, retaining frozen evidence and rejecting unknown templates or altered payloads.
- Give unissued interaction and fitting-final work in historical schema-one runs the complete current rating legend and confidence instructions, then recognize that exact prefix during reconstruction while preserving the proved issued templates.
- Freeze new bounded batch reviews as source-only before dispatch: each role owns one source responsibility, with ordered capacity parts when necessary and no new interaction or consolidation review waves. Preserve strict historical interaction/consolidation routes, immutable source origins, ordinary remediation evidence lookup, and each part's raw assessment and minimum confidence.
- Use private `paged-context-v8` store/load frames for unissued native page reads; prompts remain marker-free, while schema-eight native-wave batch admission accepts an absent or one exact derived leading marker view for the current reader and paged v7/v8, preserving raw output and strict standalone/repair parsers. Preserve issued v6/v7 recipes. Admit only one source-proved literal duplicate `--plan` in a complete known historical v7 execution; do not generalize argument equivalence or alter retained evidence.
- Normalize Git directory terminators during checkout collision checks, preserving ignored worktrees and real ancestor collisions.
- Keep accepted report discussion before source-mutation prerequisites and correct the review action-table divider recipe and diagnostic.
- Require cited evidence and representative real cases alongside TDD; preserve user-authorized behavior and reserve mocks for later polishing.

## 0.32.7

- Resolve missing runtime access through owning-helper approval before terminal review reporting; distinguish network failure from actual denial and preserve safe diagnostics when public fallback fails identically.
- Preserve unrelated TOML tables inserted before the managed permission profile closing comment during setup, migration, and removal; continue rejecting changed or extended permission grants.

## 0.32.6

- Validate large frozen contexts with literal comparisons instead of payload-sized regular expressions, preserving exact evidence boundaries and reducing memory overhead.
- Present remediation scope as All, Required, Suggestions, and Custom selection; keep custom indexes in a separate follow-up and commit/work-plan decisions in separate answer fields.
- Require blocked remediation headlines to explain the current cause, evidence, and next owner/action.
- Align review recommendations with remediation actions: distinguish code fixes from missing review evidence, recover current independent coverage, and preserve historical limitations and source-bound closures.
- Bind recipe preapproval to the three project plugin identities and correct setup guidance for the generated local-workflow default.
- Classify Git operations by remote effects: allow authorized local operations and remote reads, including guarded pull, while keeping push and remote service mutations prohibited.
- Apply repository-wide native automatic approval policy to invoked Codex plugin recipes and their populated helper/gate commands, preserving existing base policy and actual restrictions.
- Deliver remediation scope questions once; retain required independent closure review under the selected scope.
- Run authorized local skill commands under existing sandbox grants; reuse commit consent and saved allowances instead of requesting escalation for ordinary Git writes, helpers, or checks.
- Validate multiline commit quoting before execution and recover proven parse failures with one state-checked correction; resume remaining workflow gates without a new request to continue.
- Install a network-disabled `local-workflow` permission profile for workspace Git writes. Setup state advances from 3 to 4, migrates earlier generated defaults, preserves explicit user defaults and later edits, and retains reversible removal.
- Distinguish prerequisite failures from code-check failures and keep unaffected authorized work moving.

## 0.32.5

- Run helper recipes through the packaged launch boundary, preserving `python` command text while probing eligible runtimes on Linux, macOS and native Windows.
- Keep one native user-question control responsible for each live question and choices; inspect directly exposed and deferred tools, and require verified async rendering and lifetime before fallback.
- Ask once when a requested PR review is missing: run a fresh review before remediation or continue with available findings while preserving the open review obligation; unanswered decisions keep dependent work pending.
- Handle Windows batch launchers safely during Codex sync and preserve native subprocess exit behavior.

## 0.32.4

- Supply a workspace default when installing opt-in permission profiles, repair verified older installations, and preserve explicit user defaults and reversible cleanup.
- Report an unsupported Python interpreter by its actual version before the TOML parser check; sync no longer misreports Python 3.9 as Python 3.10 missing tomli.
- Install writes a `~/.local/bin/python` shim to a Python 3.10+ `python3` when no `python` exists, so the `python`-launched Codex MCP servers start on stock macOS; an existing `python` is never replaced and Windows gets a warning only.

## 0.32.3

- Check global-policy installability before removing or installing plugins and updating the GitHub profile; preserve legitimate managed-template upgrades and explicit prefix migration safeguards.
- Admit complete authentic fast native waves without claiming overlap; reject interrupted free-capacity dispatch and retain strict historical routes.
- Preserve producer session and selected evidence home through completed local/native report intake.
- Ask only for missing decisions or protected effects; keep authorized internal work moving and preserve scope, native-answer and runtime boundaries.
- Recover proved reader-path failures after exact page prefixes with one complete independent replacement; preserve original and sibling evidence.
- Offer eligible prior-review reuse before creating a new run; complete disclosed parent nonapproval fallback and preserve failed configured checks.
- Reject unmet explicit independence and copied candidates masquerading as completed report intake; add failing-before composition regressions and remove unconditional historical repair-question expectations.

## 0.32.2

- Give bounded local implementation and management tasks a parent-only path with relevant project checks.
- Resolve serial fallback before digest consent, preserve supplied denials, and retain delegated-write safeguards.
- Count failed primary attempts rather than productive diagnostics; allow useful recovery and later terminal states without rewriting its evidence.
- Treat any review-score decrease as convergence and align feasible structural-fix/recurrence rules across plugins.
- Detect overlapping/stale global instructions and known legacy skills; provide explicit digest-bound prefix migration with full backup and custom suffix preservation.
- Reject pre-GPT-6 live routes before authentication or execution; preserve historical scoring archives.
- Add outcome and composition regressions to calibration and clarify scoped follow-up decisions.
- Preserve calibration failure reports for importable helpers missing required APIs, and distinguish historical policy quotes from active summary instructions.
- Prepare and assemble current native challenges through a dedicated single-challenger route, preserving exact frozen context, terminal output, identity and historical evidence checks.

## 0.32.1

- Block PR checkout when ignored local files overlap incoming paths, including filesystem aliases and file/directory replacements; preserve unrelated ignored files and distinct link names.
- Retain actionable unavailable-review handoffs for blocked checkout receipts, including ancestor and filesystem-alias collisions.
- Classify known local Git checkout failures into safe diagnostic reasons without retaining raw stderr.
- Align calibration and README with detached review isolation and independently authorized remediation when assessed reports remain incomplete.
- Keep native specialist context files producer-owned; parent-authored focused briefs use separate input paths.
- Derive batched reviewer axes from frozen briefs, matching single-wave assembly; specify integer blocker counts.
- Validate opaque native launch delivery without redundant plaintext comparison, retain host timestamp precision, and reject boolean blocker counts.
- Evaluate native refill opportunities within the active wave while retaining historical wait and spanning-launch validation.
- Supply every literal context-page call in native dispatch; retain known historical reader recipes and reject altered readers or extra page reads.
- Execute current context-page reads independently of the caller's working directory; preserve exact historical working-directory recipes and verify actual generated commands.
- Add executable review completion-to-report-intake checks, documented batch-assessment inputs, and real-Git preservation regressions.

## 0.32.0

- Code-remediate reads each finding's review evidence (specialist sections and review notes named by the finding) before asking the user, and writes per-finding outcomes to `resolution.jsonl` in the review run; a later code-review reads the prior run's outcomes.
- Keep roles apart: a review-only run hands an in-progress merge to remediation instead of finishing it, and remediation validates the review result read-only and stops with a re-run message instead of rewriting it. Review reports are keyed by run, not by file path.
- Closure log and action-item/workplan status are appended as events (`resolution-events.jsonl`, schema 1) and the tables are rendered from them.
- Adversarial-loop ledger rounds move to an append-only `loop-rounds.jsonl` beside a small header (ledger schema 2; schema-1 ledgers stay readable), and reasoning-progress cycles move to `reasoning-cycles.jsonl` (schema 3; schema 2 validated with `--historical`).
- Growing ledgers are appended through the helper that owns their schema, never rewritten (`native-skill-contract.md` §Append-Only Ledgers).
- Discover deferred native question tools before fallback; treat unverified async rendering as unsuitable and preserve text-delivered questions without duplicate submission.
- Recover one evidence-bound native review dispatch or lossless closure-evidence representation failure within the retained run; preserve rejected attempts and frozen source, then resume remaining review gates.

## 0.31.0

- Run targeted tests inside remediation, challenge and implementation fix loops: the new `test_targets.py` selects tests from codemap-py test impact when its index is fresh and complete, falls back to name and import heuristics, and records each selection in the run directory. The full suite runs once at the final gate with the repository's own settings; the code-review gate always runs the full selection.
- Keep targeted loop test runs inside the sandbox with `-p no:xdist`, request one reusable approval for the pinned pytest prefix up front, and stage and commit reviewed paths with one approval guarded by a staged-scope and file-set check.
- Ask every predictable remediation decision in one upfront packet after scope, defaulting the commit choice to decide after verification; every earlier question and approval still happens.
- Finalize code-review and remediation in one helper call that derives handoff fields, renders, writes the result and runs both validators; validators gain an opt-in `--all-errors` mode while their default output is unchanged.
- Wait for child agents with a blocking wait and a per-agent deadline instead of polling; progress names pending and timed-out agents, and plan updates ride with real work.

## 0.30.1

- Validate native review evidence once per admission request, reducing repeated source and receipt checks while preserving fresh validation for later consumers.

## 0.30.0

- Retain every required native reviewer in a largest-context-first frozen queue with at most four allocated child slots; bind real launches, task completion, joins and parent wait/results to refill decisions, recover proved no-child capacity refusals after actual state change, and admit complete smaller-pool native reviews without falsely claiming parallelism. Preserve dependent-wave barriers and strict historical reader gates.
- Fast-forward the maintainer's local target branch to the fetched target before an authorized remediation merge, without a second network fetch; a diverged or checked-out branch is kept and reported, and the merge always uses the fetched target object.
- Require the complete nested boolean review-routing signals mapping before preparation and report exact missing source coverage paths for bounded input repair.
- Split large source and interaction reviews into serial bounded waves, retaining exact coverage, original findings and native provenance through consolidation instead of repeatedly redistributing an oversized context.
- Account for each individual finding in new batched reviewer outputs, including minor findings, and retain unresolved findings when independent source-backed dismissal is unavailable; bind source evidence in code instead of asking reviewers to copy digests.
- Derive native reviewer provenance from verified runtime records instead of model-copied digest headers; recover eligible historical responses into a separate validated candidate while preserving their original evidence.
- Keep remediation focused on selected, source-confirmed fixes when requested review evidence is unfinished; `+review` consumes existing evidence and never silently starts another review or makes reviewer setup a prerequisite for authorized code changes.
- Ship an independently installed local question tool using Codex's native terminal form: one scope-bound request, an explicit correlated answer, and no asynchronous question-text duplication. Unsupported clients and cancelled or malformed replies grant no consent.
- Accept explicit conversational approval or denial for one unchanged, unambiguous pending decision without demanding its displayed key again; retain ambiguity, supersession, exact-token/digest, and runtime-permission barriers.
- Make release-draft placeholders readable as short square-bracket fields, separating writing and verification instructions from public content.
- Keep remediation questions to the supported async fields and concrete selection presets; forbid repeated submissions and even empty final messages while a scope answer remains pending. Client rendering and click delivery remain host capabilities.
- Render the release draft Summary as a short pitch a reader can take in at a glance: one hook line, one to five win bullets, and one upgrade-call line that carries breaking or removed items. Split or distill long lines, and re-rank the wins over the whole release on every incremental cycle.

## 0.29.0

- Admit native reviewer contexts through 256 KiB using the existing ordered, provenance-checked page reader; keep a hard preparation stop for larger contexts and require complete-file chunk planning for them.
- Name the optional paid review route “local reviewer wave” in its guide, helper, tests, and current instructions. Preserve existing evidence fields and diagnostic codes so earlier review runs remain readable.
- Route active Sol parent, review, and specialist work to GPT-6.1 Sol; keep Luna on GPT-6 Luna and Astra as an explicit escalation without a standing role. Align calibration, policy, and package checks with the current three-tier lineup.
- Yield immediately after an accepted required review question so a later status handoff cannot displace the pending answer control.
- Preserve frozen review context bytes across dispatch, pagination, native UTF-8 stdout, and receipt validation on Windows, including CRLF source and non-ASCII text.
- Use portable native-shell invocation in audit acceptance tests and explicit UTF-8 artifact reads; exercise locale and newline regressions on every host.

## 0.28.2

- Bind native reviewer contexts to focused source from verified local, PR, or commit checkouts, including committed deletions, renamed and quoted paths, and preexisting unchanged callers; reject prose-only evidence and duplicate assessments.
- Reject incomplete nested PR comment pages, derive routing file inventories from verified Git comparisons rather than truncated remote metadata, and reject pytest evidence selecting tests outside the reviewed checkout; retain provenance from local xdist workers for parallel source-bound tests.
- Preserve canonical report findings and evidence obligations through remediation; reconcile selected outcome totals and forbid passing results with required work open.
- Keep the user's primary goal fixed through auxiliary setup and report repairs; schema-two progress ledgers reject auxiliary work claimed as primary progress, count evidence-backed attempts independently of progress flags, require recorded authority for condition replacement, and bound setup recovery to one repair.
- Reject unsupported audit optimization acceptance and successful audits without an executed review; require paired per-task completion-quality and tool/check failure guards.
- Load only applicable final handoff supplements while preserving existing workflow obligations.
- Diagnose overlapping unmanaged global instructions without deleting user content.
- Surface Windows startup and analogous assertion failures directly, remove redundant CI package checks, and clarify Sol advisory-role scope.

## 0.28.1

- Route audited GitHub helpers from current network and filesystem grants when the runtime omits the active profile name. Preserve destination restrictions and report/index/checkout write boundaries; failed profile lookup no longer implies disabled access across review, remediation, assessment, and release.
- Preserve exact changed-file bytes in local review worktrees when Git converts line endings, including native Windows autocrlf checkouts. Keep caller state, patch equality, and source verification intact.

## 0.28.0

- Prepare complete native reviewer waves and assemble manifests from observed runtime evidence. Deliver frozen context through bounded audited page reads per reviewer, fixing encrypted-dispatch and self-referential provenance failures while preserving historical schema-five readers and quality gates.
- Isolate local review source in detached worktrees, preserve caller edits/index, and reject changed review snapshots before acceptance.
- Reuse explicitly selected existing review environments and installed Codemap provider roots; fetch material referenced GitHub evidence through the audited reader instead of treating browser-cache misses as unavailable content.
- Prevent unnecessary approval prompts for audited GitHub reads when `github-read` is active: all four consuming Skills omit escalation fields and the approval brief, even when the stored default is `:workspace`. Preserve ordinary-session approval and stop on host denial.
- Permit independent remediation evidence preparation alongside verified-target conflict analysis, without duplicate fetches, concurrent Git mutations, or bypassing merge authorization.
- Default eligible remediation buckets to isolated parallel work; continue authorized parent-owned or sequential work without a fallback approval prompt, preserve unrelated source changes, and accept source-local ignored worktree roots with path-scoped safety checks.

## 0.27.0

- Preserve user-saved collector approvals with literal launchers during GitHub profile setup and removal; retain rejection of modified managed collector grants.
- Require `sw-engineer` as the primary assessed reviewer for changed production Python source in Code Review. Bind its routing, retained rating, and first report position; keep tests-only review with QA and disclose parent substitutes when reviewer capacity is reached.
- Add offline routing calibration for all fifteen agent roles: each role must retain one distinct related task cue in its trigger contract, with missing or overlapping routes failing the gate.
- Show the complete `questions` wrapper for Codex async input, yield without a status handoff while a required answer is pending, and reject unkeyed approval for a keyed local merge decision.

## 0.26.0

- Recover a user-reported dismissed Codex question at its frozen decision checkpoint; use a final plain-chat question when no native control remains suitable and avoid the dismissed control in the same host until delivery is verified again.
- Add `shared/notebook-style.md`, the canonical notebook-authoring standard (cell granularity, markdown narrative depth, plot framing, magics, fail-fast main path) for every notebook this rig produces, propagated byte-identical into `cc_foundry`, `cc_research`, and `cc_oss`. The kaggle skill's `references/style-rules.md` now carries only the Kaggle-only delta on top of it (was style-rules rule 08).

## 0.25.3

- Require a verified blocker before refusing an explicitly requested reversible local action; reuse existing authorization.
- Honor an explicit local remediation commit when all open selected obligations are external environment verification or independent review and canonical gates pass; retain the failed result and disclose each gap in the commit plan and message. Bind the exception to open review or confidence gate items so a mislabeled local finding cannot qualify.
- Distinguish accepted async questions from visible choice forms; preserve typed-answer selection when a client renders the question as text and stop claiming a control is visible.
- Keep the legacy workspace migration regex stable under Ruff and docformatter; restore single-quoted TOML migration.

## 0.25.2

- Split the largest artifact-validation, local reviewer wave review, PR collection, and parallel-execution functions into named single-concern helpers. Validation order, error codes, and evidence records are unchanged.
- Carry the rejected-event diagnostic on `ReviewRouteError` itself instead of a caller-local variable, so failure evidence still records why an local reviewer wave event was refused after the event loop moved into its own helper.

## 0.25.1

- Migrate a sole root `sandbox_mode = "workspace-write"` to `default_permissions = ":workspace"` during GitHub-read profile setup, with a byte-exact backup and reversible clear; retain fail-closed checks for other legacy sandbox settings.

## 0.25.0

- Replace workflow-specific GitHub approval flags with an opt-in `github-read` permission profile. Setup or repository sync installs it, while direct plugin installation does not; select it for a fresh session with `codex -c 'default_permissions="github-read"'`. The profile extends `:workspace` and routes GitHub-domain traffic through the network proxy; host restrictions still apply, and the profile does not enforce HTTP methods or executable identity.
- Validate GitHub-read profile configuration before writing it, preserve unrelated user settings, and recover verified interrupted installs without accepting ambiguous TOML or unsafe permission changes.
- Resolve bare PR numbers against GitHub `origin` when fork remotes exist; explicit PR URLs continue to select their named repository.
- Check review handoffs against their captured source, diff, and supporting evidence before a paid review or clean result. Reject incomplete or mixed PR/local snapshots, keep historical artifacts readable, and render collected PR titles safely.
- Make challenge-resolve carry earlier findings into independent review rounds, require evidence that fixes address earlier counterexamples, and report unresolved findings explicitly. Split large changes into measured chunks and stop when source coverage or convergence cannot be verified.
- Keep remediation chat summaries concise while retaining detailed evidence in artifacts. Permit an explicitly requested local remediation commit when only environment verification is blocked, with the remaining obligation disclosed.
- Classify rejected local reviewer wave notifications by recognized public-schema name without exposing arbitrary methods or payloads.

## 0.24.1

- Fixed ACR2: shepherd's breaking-change `AskUserQuestion` gate was structurally unreachable (agent has no foreground mode) — now unconditionally emits a report block instead, with `semver-rules.md` and README updated to match.
- Added a per-model concurrency ceiling table (Luna 20, Sol 5, Terra 10 — deprecated pending GPT-6 rollout, Astra 2) to `shared/specialist-orchestration.md`, bounding how many nodes of a given model may run at once across a session, separate from the existing four-node per-wave schema-v2 ceiling.
- Fixed 21 audit findings, re-verified against disk state after 2 external commits landed mid-audit (32/32 re-checked findings still valid). See `.reports/audit/2026-09-22T22-18-03Z/fix-summary-codexrig.md` for full detail.

## 0.24.0

- Rename the `adversarial-loop` skill invocation to `challenge-resolve` while retaining the shared procedure and reviewer wire format; remove the unprovable archive-validation opt-out so final results require the same bound action record as candidates.

## 0.23.0

- Run bounded challenges as collect → report old/new once → fix feasible findings → escalate unresolved severe findings → repeat until clean, three rounds or plateau; validate one parent action per open finding, avoid placeholder progress tables, and distinguish advisory reviewer token targets from enforced caps. Table delivery count remains an instruction-level limit without a host transcript receipt.
- Route the normal Codex parent and all fifteen specialist roles to explicit GPT-6 Sol/Luna model-and-effort assignments, keep architecture/security advisors opt-in, and mark GPT-5.6 paid calibration as historical rather than GPT-6 acceptance evidence.
- Resolve numeric PR targets to a unique canonical GitHub URL before proposing reusable collector approval; keep workflow consent separate from loaded host permission and reject ambiguous repository identity.
- Review verified PR commits in detached isolated worktrees, preserving the invoking checkout and binding source-dependent gates to the recorded worktree. Remediation retains its attached-branch checkout contract.
- Reject malformed review risk tiers before specialist work and require explicit role IDs on new parent-only substitute outputs while preserving historical artifact compatibility.

## 0.22.0

- Require reviewer attribution and the aggregate summary when promoting new assessed review candidates without retroactively rejecting historical reports. Non-PR assessed candidates must now carry a `Review Snapshot` table, which earlier releases did not require — a breaking change for that promotion path only; stored artifacts still read unchanged. Require convergence tables in challenge updates, approval/remediation questions and pauses, including unvalidated runs.
- Add scoped reviewer ratings in review header tables, a five-value legend below, and finding authors retaining all deduplicated contributors. Preserve aggregate prose summaries, existing verdicts and evidence; bind rendered attribution to canonical records and retain historical report rendering.

## 0.21.4

- Keep optional GitHub preapproval flags separate from conversational consent across Code Review, Code Remediate, Assess, and Release. Reuse scoped natural-language authorization and matching direct-helper host rules, prohibit flag-reply and reinvocation demands, and preserve native consent controls, runtime permission, and denial boundaries. Add neutral positive and negative calibration scenarios for every consumer without revealing expected verdicts.
- Add an explicit cumulative adversarial-loop progress transcript after every completed challenge-resolve round. Keep JSON stdout and the final canonical results table unchanged; render the stderr table as `Iteration | Critical | High | Medium | Low | Nits | Weighted score` with literal `old + new` cells, reopened-signature history, fixed-pending-verification counting, preserved severity weights, and truthful empty-ledger `not-run` plus `N/A` cells.

## 0.21.3

- Enforce required-work closure, a local commit plan, and valid declared evidence before accepting commit readiness; allow explicitly user-deferred work outside the plan without hiding required blockers. Cover collection prebrief native consent separately from runtime permission and existing preapproval.
- Require an explicit remediation commit disposition before final output; preserve the native commit-mode choice after validation and explain blocked/no-change closeouts. Reject new reports that omit the checkpoint or claim commit readiness with failed verification while keeping historical reports readable.
- Share concise root-owned question guidance across all Codex skills; load detailed approval and recovery rules conditionally and check identical plugin-local copies.
- Specify native remediation selection with concrete all/severity presets and built-in custom index/range input; omit duplicate presets and preserve pending answer binding. Correct README prompt ownership and explain host-required plain-text fallback without promising a UI override.

## 0.21.2

- Recover failed scope-question controls without repeating the report: use eligible async after synchronous rejection, honor explicit host plain-text requirements, and retain accurate capability evidence and pending selection.

## 0.21.1

- Accept current and historical collector checkout diagnostics in unavailable PR reviews, while rejecting malformed fields and keeping raw command details out of the handoff.
- Revalidate PR report intake with both artifact validators, using the recorded producer thread across sessions; reject metadata-only and altered-evidence reports.
- Archive same-directory gate attempts before reruns and reject failed-to-skipped recovery without changing the prior evidence.
- Make review artifact closure explicit at the execution checkpoint, including failed reviews and resumed report intake; emit final output only after the completion lookup.
- Preserve applicable gate failures and their logs instead of replacing launcher failures with skipped checks or direct-check claims.

## 0.21.0

- Add a pre-diff blind blueprint for nontrivial behavior or public-API reviews: derive a one-page proposed solution from the problem statement before opening the diff or changed source. Skip unsuitable changes or missing problem statements with a recorded reason.
- Compare the blueprint with the implementation; record concrete defects as findings and explainable design divergences as author questions under residual risks. Disclose that same-thread ordering reduces anchoring without providing context isolation.

## 0.20.3

- Fall back to permitted synchronous input for optional questions when async is unsuitable; reuse existing explicit commit-mode authorization when it still matches the complete verified plan, asking again only for missing or materially changed decisions.
- Require permitted native Codex question controls for user choices, including generated scope expansions, repair approvals, finding selection, and commit modes. Use async when sync is unavailable or unsuitable, even without independent work; keep required answers pending and use plain chat only when neither control is suitable.
- Present complete actionable options or native free text, preserving existing authorization, exact-digest syntax, and separate runtime permissions.

## 0.20.2

- Compress skill and shared-contract prose to the ultra-caveman tier; behavior, test-pinned contract sentences, role definitions, and structural literals are unchanged.

## 0.20.1

- Track embedded suggestions in collapsed review bodies individually across review and remediation, preserving parent provenance, separate source identities, advertised-count reconciliation, and per-finding dispositions.

## 0.20.0

- Preserve native command exit codes in Windows gate receipts without masking PowerShell errors or successful recovery.
- Decode release-evidence Git metadata as UTF-8 so Unicode paths and contributor identities remain consistent across host locales.
- Accept local working-tree, path, and commit reviews at explicit remediation intake only after both existing artifact validators pass; reject metadata-only, draft, altered-evidence, and noncanonical local files. Keep PR discovery PR-only and preserve terminal-report rejection.
- Add capability-aware native Codex questions across every Codex skill: prefer permitted synchronous controls for required decisions, asynchronous controls for independent follow-ups or lossless fallback, and plain chat when unsupported.
- Use Approve/Deny for conversational authorization while preserving exact-digest and runtime permission boundaries. Bind delayed answers to immutable scope, reject superseded/duplicate replies, retain all feasible choices, and avoid duplicate live prompts.
- Ship plugin-local question guidance and regression coverage; preserve existing authorization and host-specific behavior without changing installed settings or requiring a Codex upgrade.
- Label one evidence-backed first choice `(Recommended)` in option-based questions; preserve canonical answers, exact confirmation syntax, and explicit consent.

## 0.19.0

- Enforce raw JSON findings on every local reviewer wave review turn with a source-bound output schema and fail-closed response validation; preserve original bytes and historical fenced loop reports without automatic retries.
- Reconcile corroborating independent findings without discarding their evidence; reject conflicting verdicts and incomplete ledger unions.
- Require explicit-file review scopes to match every source record. Reconcile Git and PR bot identities in a separate evidence-bound inventory and one aggregate credit instead of silently dropping automation or treating it as human credit.
- Require schema-2 local reviewer wave plans to bind complete source/diff bytes and per-reviewer capacity evidence with a recomputed conservative UTF-8 byte bound and instruction/output headroom. Reject unverified proxy counts, missing, insufficient or changed admission evidence before paid turns; preserve direct inspection of historical plans without allowing them to authorize new dispatch.
- Bind release receipts to exact Git candidates, ancestor scope, surviving published tree entries, visible human credits, complete canonical changelog excerpts and final output destinations. Require summary/migration structure and retained exclusion evidence; reject placeholder secondary files and historical detail loss. Add explicit local demo recording with before/after script digests, output binding and failed/timeout evidence; validation stays read-only.
- Admit complete local reviewer wave review contexts up to 2 MiB without relaxing metadata, role-card, or output limits. Preserve exact inline source validation and bounded JSON transport; support a plan-bound per-thread context-window request with explicit capacity checks and no global configuration changes.
- Respect the separate CLI character ceiling by loading oversized complete contexts into the review thread's history before any paid turn, with validated delivery acknowledgements. Preserve direct input for smaller contexts and retain safe RPC rejection categories without raw error payloads.
- Restore native inspection provenance on hosts emitting task-path spawn receipts instead of activity events. Bind call IDs, exact context, unique child lineage and creation timing; retain legacy validation and reject stale, ambiguous or mismatched evidence.
- Restore complete release communication: structured drafts, full contributor accounting, executive summaries, migration guides, changelog excerpts, and preservation of canonical changelog history and material detail. Trace release-line ancestry, released patch equivalence and final tree state; reconcile incremental drafts without losing hand edits or retaining reverted claims.
- Require a readiness table with check status, evidence and closure action in release reports and final handoffs. Validate selected deliverables and communication evidence before a passing result; retain readable historical reports and honest failed preparations.
- Require both change and readiness tables in new release handoffs. Bind executable release gates to the exact clean release commit with before/after source receipts; reject wrong-checkout results and source drift while preserving opt-in compatibility for other gate-runner callers.

## 0.18.2

- Validate truthful PR collector checkout receipts in review and remediation: native checkout, verified review fallback or existing head, and attached same-repository remediation fallback. Preserve legacy receipts and reject inconsistent command, method, mode, and source evidence.
- Dispatch selected independent read-only reviewers concurrently, allowing shared source reads while the parent preserves the snapshot and coordinates writes and gates. Retain actual concurrency and unavailable-capacity limits.

## 0.18.1

- Restore commit continuation for legacy PR remediation runs already on the original PR branch. Verify retained source identity, recorded revision, ancestry and live destination; preserve historical receipts and Git state, write separate recovery evidence, and resume the authorized commit mode without repeating checkout or mode selection.
- Remove stale review-comment triage, resolution, and skip routes: outdated anchors and conflict-resolution drift require reassessment against current code. Preserve every other disposition and historical artifact compatibility.
- Require PR remediation to try `gh pr checkout <canonical PR URL>` through collector remediation mode, even when HEAD already matches. After failure, allow only a verified same-repository direct checkout of the actual PR branch; a guarded local update from the already fetched, verified head may populate an explicitly selected remote-tracking ref for native tracking creation while preserving divergent or concurrently changed refs. Forks use the shared bounded adversarial recovery route and return to successful attached `gh` checkout; exact-commit detached checkout remains review-only. Make `remediation_branch.py prepare` read-only schema-2 receipt verification, require local branch/head/merge/destination identity, and preserve legacy receipts and local commits through verified recovery. Show observed branch plus original PR destination while remote updates remain human-owned.
- Make audits and adversarial reviews follow producer/consumer handoffs through the next ordinary user action, question green tests and approved assumptions, and retain counterexamples, positive cases, and untested coverage.
- Preserve the user-authored Kaggle preference for single inline shell commands and dedicated multi-command Bash cells.

## 0.18.0

- Preserve exact reviewer-context line endings when checking frozen loop evidence. Accept intact CRLF diffs and reject newline-altered context; exercise LF and CRLF snapshots and local reviewer wave review evidence on every host.
- Bind loop closure to the active host owner, existing validated reviewer execution, one complete structured response, exact returned findings, complete source snapshots, and fresh local source; reject self-declared independence, ignored finding prose, stale source, and passing review gates on stopped loops.
- Add `adversarial-loop` for bounded independent review-and-fix convergence with stable finding identities, weighted stop decisions, retained snapshot/report evidence, and the normal validated result lifecycle. Keep root guardrails short and one detailed shared procedure; existing workflows retain their own gates and authorization.
- Make reply openings identify the topic or question being answered instead of relying on unanchored references to previous turns.

## 0.17.1

- Make remediation outcomes distinguish finding-specific implementation, verified closure without code changes, rejection, deferral, and concrete blockers. Preserve honest unresolved counts, keep passing checks separate from incomplete remediation, and require usable text-review context and permitted missing-coverage investigation before stopping.
- Repair PR source preparation and recovery evidence: verify fetched commits without forced cached-ref updates, distinguish PR-file changes and unresolved index entries from unrelated local edits, preserve safe Git failure causes without raw stderr, and clear stale PR identity between collector attempts.
- Reject passing PR-check claims in new unavailable-review results when collection stopped before verification. Require explicit not-applicable gates and generated handoff consistency; preserve historical artifact reading and ordinary command exit-code behavior.
- Make blocked review and remediation handoffs explain the failed check in plain English, recommend concrete recovery, and show approval/decline consequences without inventing causes or repeating existing authorization. Preserve fresh sequential review as a disclosed alternative to rejected specialist evidence; missing current PR source still prevents edits.
- Explain existing merge conflicts separately from fetch failures, with evidence-backed finish/abort/defer choices and preservation requirements. After authorized recovery succeeds, resume the active review or remediation from its first unmet checkpoint through normal completion gates instead of asking the user to rerun it.

## 0.17.0

- Fix managed runtime preapproval for every `--approve-gh` consumer: retain existing reader grants for Assess/Release and add explicit canonical-PR collector grants for Code Review, Code Remediate, and PR-mode Assess. Preserve approved targets across verified setup/sync upgrades, reject modified rules before writes, and back up/remove owned grants through the existing lifecycle. Loaded allow rules avoid another prompt; setup/restart and stricter host policies remain explicit requirements. Keep skill invocation separate from permission installation.
- Define `--approve-gh` in Code Review and Code Remediate as completed user authorization for the required GitHub collection, so the workflow does not re-ask for consent. Preserve the reusable direct collector prefix of the actual Python executable, installed `collect_pr.py`, `--target`, and canonical repository-qualified PR URL; keep dynamic report paths outside it and do not wrap it in `rtk`. Runtime permission remains separate: the host may still prompt or deny, and the flag cannot bypass prompts or denials, create or modify saved rules, or authorize remote mutation. Remediation finding selection remains separate.
- Support the same completed-user-authorization meaning for `--approve-gh` in Assess and Release. Required generic GitHub reads use the direct actual-Python-plus-installed-`github_read.py` prefix, with reader-wide scope across repositories and its output-file and allowlisted local-checkout capabilities disclosed; PR analysis retains PR-scoped collection. Preserve local-only workflows, runtime prompt and denial controls, and the prohibition on publication or permission-rule changes.
- Rename `change-analysis` to `assess` across discovery, routing, templates, metadata, calibration, and documentation. New reports use `.reports/codex/assess/`; existing `change-analysis` report artifacts remain readable, but the former skill name is no longer registered.
- Reject repeated PR collector targets and abbreviated options so trailing arguments cannot override the target bound by a reusable runtime prefix. The flag supplies completed user authorization for the workflow; runtime rules remain host-owned and the workflow cannot install, change, or bypass them.

## 0.16.3

- Reject contradictory local reviewer wave turn identities, malformed lifecycle items, and events for completed reviewers while sibling reviews remain active. Preserve already completed output on failure and report a specific reason for duplicate terminal events.
- Record final local reviewer wave evidence-validation failures as failed results after cleanup, preserving the original error reason and existing reviewer files. Cover duplicate identities and changed final output; strengthen the native-approval prohibition regression with a full negated assertion.
- Simplify target classification and source/rollback patch validation while preserving mixed-state precedence, symlink rejection, and stable error codes. Isolate input-echo lifecycle validation from the review event loop; add regression and characterization coverage.
- Declare internal reviewer state as a same-module slotted dataclass with keyword-only construction, preserving mutable lifecycle transitions and explicit JSON validation and evidence formats.
- Show accepted choices or input formats in workflow questions, including `(yes / no)` for binary text confirmations. Preserve native-control options, exact-digest approval, and existing authorization without duplicate prompts.
- Compress instruction and documentation prose while retaining literals, structure, executable contract wording, and reviewed policy semantics.

## 0.16.2

- Preserve every skill's standard closing gate and output structure after user intervention, recovery, or repeated invocation. Reuse valid evidence from the first unmet checkpoint; never treat a resumed notes-only run as a completed result or substitute an informal recap for the required final report.
- Start all user-facing communication with a plain-English explanation. New handoffs remove empty result sections, collapse unrun checks, and avoid repeating recovery actions while retaining complete machine evidence and historical rendering compatibility.
- Fetch fork PR commits before checkout-overlap checks while keeping GitHub CLI metadata and `gh pr checkout <number>` primary. Make fresh PR/target source and safe failure diagnosis agent-owned; report the actual failed operation, classified error, and available checkout evidence instead of generic repair instructions.
- Fix local reviewer wave rejection of documented planning and warning events; retain safe rejection classification and recovery guidance without storing raw event data. Repeated launcher failures stop that launcher, not permitted source inspection; retain recurrence evidence and use an available review route without redundant approval.
- Fix blocked source reviews by allowing supplied-context inspection for native parallel reviewers without requiring child read-only/never attestation. Validate frozen source contexts before dispatch and bind reviewer lineage, tool-free activity, terminal responses, and actual overlap without claiming enforced isolation. Keep repository execution parent-owned and preserve existing strict execution routes.
- Stop classifying public API changes as automatically HIGH_RISK. Let review depth follow actual behavior and permit honestly disclosed serial inspection when parallel reviewers are unavailable; an explicit user requirement for independent review still gates completion.
- Require every workflow pause to identify the stopped action, concrete cause and evidence, governing rule, continuing work, next action and owner, and exact resume condition. Reuse existing authorization and keep unsafe execution pauses separate from available source review.

## 0.16.1

- Let PR collection continue when tracked local edits are unrelated to the requested checkout or the current HEAD already exactly matches the PR head. Retain a deterministic `worktree-preflight.json` and block only the dirty paths checkout would overwrite.
- Make terminal PR-review unavailability state its exact reason immediately. Dirty-worktree overlap reports name the paths from `worktree-preflight.json` instead of a generic collector failure.
- After validated remediation, offer an explicit local commit choice: all resolved remediation changes, coherent recorded topics, or safely disjoint findings. A selected mode is the commit authorization; the default remains unstaged.
- Record a pre-edit worktree baseline and require an empty index, exact owned-path staging, and exact staged-scope validation. Unrelated cache/lockfile leftovers remain untouched, while pre-existing, disputed, overlapping, or unprovable paths block only the affected commit unit rather than being included or reset.

## 0.16.0

- Manage reusable GitHub-reader approval through explicit plugin setup and sync. Install a dedicated user rules file for the installed wrapper, regenerate its path after plugin upgrades, and remove it during managed teardown. Direct plugin installation remains inert.
- Back up changed rule files, reject modified or unowned managed content and linked paths, and migrate only canonical two-token legacy reader approvals while preserving unrelated rules. The existing wrapper scope includes GitHub reads, local PR checkout, and output writes; arbitrary Python and direct GitHub CLI commands receive no new approval.
- Run managed home cleanup before removing the plugins that provide its helpers. Cover install, upgrade, idempotence, legacy migration, removal, and Windows argument spelling with isolated-home regressions.
- Preflight all migration backups before rules mutation, report completed updates on partial failure, and verify package hashes before granting approval. Validate complete selected source packages before plugin removal and reject configured unsupported pins before marketplace mutation while retaining normal default-branch upgrades; document the trusted-cache lifetime boundary.

## 0.15.1

- Accept the durable code-remediation table's full workflow column list instead of requiring exactly the nine columns the validator names. `Required table columns` in the skill lists sixteen, so a run that followed the workflow rendered a wider table and was rejected as `code-remediate-final-table-markdown-columns-mismatch`, leaving a complete result stuck as `result.candidate.json` with no way to promote it. The check now reports which required columns are absent and ignores additional ones, matching how the same required set is already checked against the metadata column list and the surrounding prose.
- Name the durable table's closure-evidence column `evidence` in the skill's required-column list, matching the column name the same skill file already declares in `final_resolution_table.required_columns`. The previous heading described the field rather than naming it, so a run that used it literally omitted a required column.
- Compare durable-table detail and expanded-source lines against the stored ledger with inline code spans normalized on both sides. A detail line that marked a path or command as code carried the same text the ledger held but failed `code-remediate-final-table-symbol-detail-missing`, so a complete run could not promote its result. Wording, ordering, and every other difference are still rejected exactly as before.
- Give `shared/app_server_review.py` the module documentation headings the shipped-module check requires. The sections were already present as prose labels; only their form changed.

## 0.15.0

- Add an explicitly approved, bounded local reviewer wave reviewer route for hosts without native child permission controls. Verify invocation-scoped capability restrictions and effective read-only/never thread controls; preserve opaque authentication and parent-owned report persistence.
- Bind separate schema-4 review evidence to frozen context, canonical roles, independent terminal responses, and cleanup without fabricating native lineage. Keep canonical completion/discovery, non-sensitive scope, write-remediation boundaries, and disclosed credential-isolation limits.
- Add adapter and canonical review regression coverage plus fixture-only calibration for inherited MCP configuration, evidence tampering, required independence, and incomplete completion paths.

## 0.14.7

- Anchor recorded artifact paths on the run directory instead of the caller's working directory. Validation used to probe the reader's own directory when a recorded path was relative, so whether a finished run was valid depended on where the validator happened to be invoked from — a review that passed inside its workspace failed when `--complete-run` revalidated it from elsewhere. Gate logs, final-handoff paths, declared result paths, and code-review artifact paths now all resolve from the run directory, falling back to its ancestors for runs written before this convention; every accepted path must still resolve inside the run directory, so widening where a name may resolve does not widen what counts as evidence. Existing artifacts keep validating.
- Record gate logs as POSIX paths relative to `--out` (`checks/<id>.<kind>.txt`) in `run_gates.py`, derived from the file actually written so the recorded name cannot describe a different location. The previous form embedded the producer's own directory and, on Windows, host-native separators that no other operating system could read.

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
- Add an approval-bound generated-fixture worktree scaffold using a simple parent-authoritative handover: freeze exactly two disjoint packages at one clean source identity, create separate detached worktrees, join two completed child reports with concise summaries, exact changed paths, and canonical patch SHA-256, verify both reports against actual Git changes, derive and integrate patches in the parent, and record non-force cleanup evidence. Provide `create_completed_child_handover` so children hash the lifecycle's raw Git subprocess bytes instead of shell- or RTK-rendered diff output; normalize conventional string or path-like lifecycle-state paths at this boundary and reject unsupported objects with stable `PilotError`. Remove the obsolete fixture-history parser, dynamic validator import, timestamp reconstruction, and local reviewer wave attestation dependency while retaining authority rederivation, inherited `GIT_*` sanitization, retained-attempt fingerprints, portable path/alias/symlink checks, commit/index/untracked/delete/rename/mode/type rejection, deterministic integration, and failure retention. The record is operational evidence under parent authority rather than cryptographic host provenance; the generated-fixture route remains production-write ineligible and default-serial, while the code-remediate-local production lifecycle remains separately gated.
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
- Add a fail-closed, standard-library-only local reviewer wave denial transcript validator and focused protocol tests covering callback correlation, exact request/lifecycle command identity, declined primary terminal states, no output or fallback execution, fresh-turn recovery, and atomic success or bounded sanitized failure evidence only after the process cleanup attempt, without contacting a host or network service. A separately authorized live matrix runs text-only and installed-skill-input controls before denial, requires isolated non-overlapping roots plus one independently recorded full-package manifest digest across every row, verifies every declared payload before launch, stops on the first failure, and retains only sorted allowlisted error categories, the first specific category, whether any retry occurred, and the final retry state. Prior Codex-home use remains an explicit operator precondition because the probe cannot infer it safely from contents.
- Add an installed-package-safe acceptance gate that copies only manifest-declared payload into a disposable cache and runs explicit package-safe tests without source-checkout context; the complete suite exercises these gates across Linux, macOS, and Windows with Python 3.10–3.13, while live local reviewer wave candidate binding remains a separately authorized manual probe and does not claim desktop-UI equivalence.

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

## 0.2.0

> Historical development builds: `0.2.0+codex.20260718221820`, `0.2.0+codex.20260718223537`, and `0.2.0+codex.20260719085017`.

- Add the `agent-shims` manager for whole-roster diagnosis, status, installation, removal, and interrupted-transaction recovery, with exact-digest approval for mutations and fresh-session guidance. This describes the historical lifecycle; version 0.2.1 subsequently blocked new shim installation pending runtime-selection verification.
- Add an optional read-only SessionStart shim-health diagnostic for startup and resume, separate from installation and lifecycle authorization.
- Document public GitHub installation and guarded update/uninstall recovery; add installed-package, process-death recovery, and platform-boundary acceptance checks while retaining the then-POSIX-only shim-management limit.

## 0.1.0

- Establish the Apache-2.0 Codex plugin package, deterministic inventory, portable workflows, and canonical role cards.
- Define the thin-shim safety contract, authenticated installed state, and reversible transaction foundation.
- Introduce role-card fallback routing while native custom-agent selection remained unverified.
