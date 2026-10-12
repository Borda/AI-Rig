# Changelog

## 0.40.1

- Render reviewer attribution with readable role names and square-bracketed ratings, omit execution-origin labels, and display null judgments as “no source judgment.”

## 0.40.0

- Gate existing compaction boundaries with persisted metadata and optional Foundry `compact_gate.py`; append the fix checkpoint before writing its contract. Follow-up questions carry the report path and key outcome, while the final reply preserves the full report header.
- Route skill questions through saved-content and plain-summary delivery without previews, using persisted session/digest ownership metadata and atomic contract writes.

## 0.39.2

- Validate each review candidate against allowed source evidence before publishing findings; retain revision, checks, counterevidence, disposition and durable open concerns through remediation.
- Define source ratings as 1 approve, 2 minor edits, 3 major revision, 4 insufficient-but-promising, and 5 reject only for an evidenced conflict with binding constraints. A completed undecidable assessment (`null`) differs from a missing reviewer; neither implies rejection or approval, and ratings never replace the aggregate verdict.
- Breaking report-intake change: add versioned `review-validation.json` with `source-5-v1` ratings and `REVIEW_V1_PASS` / `REVIEW_V1_BLOCK` / `REVIEW_V1_REJECT_<GROUND>` gate headers. These prefixes deliberately block legacy resolver intake: upgrade the resolver and rerun its report gate rather than stripping the prefix.

## 0.39.1

- Remove shipped repository-API, GraphQL, branch and tag allow grants; new installs use normal permission handling, including routine-read prompts when no other grant matches. Remove shipped GitHub-write and local branch/tag-deletion denies while retaining force-push and mirror-push denies.
- Develop setup preserves existing repository-API, GraphQL, branch and tag allow entries because Foundry may also have installed them; standalone users remove them manually. Foundry setup owns stale GitHub-deny cleanup.
- Add an optional `gh-read` decision lane after blueprint and shape; a missing optional module falls through.

## 0.39.0

- Require the report header in the first follow-up question text or in every eligible option preview before `develop:review` proceeds; record delivery only after the question is shown. When the full header exceeds preview limits, use a compact summary and saved-report path.
- Warn about stale session state and offer a choice; debug/fix hypothesis teammates are read-only, while team edits share the orchestrator worktree and need no separate teammate merges.

## 0.38.7

- Add `--force-if-includes` and mirror pushes to Develop’s explicit force-push deny list. The agent router resolves the newest Haiku model dynamically from environment/cache/API and falls back with diagnostics.

## 0.38.6

- Require evidence-backed findings and completion claims in review and fix; retain representative real-case acceptance, use mocks only as supplementary checks, and do not weaken assertions or requested behavior.

## 0.38.5

- Retry transcript reads for up to two seconds before denying review-header delivery; report the visible-reply character count and distinguish thinking/reasoning and tool output from user-visible delivery.

## 0.38.4

- Keep the report-print exception available and enforce Stop when `develop:review` follow-up is skipped; return a short terminal summary while retaining full findings in the report.
- Account for confidence with two-decimal gap deductions summing to `1 - score`; use `(-0.00)` for nonreducing limits and one residual entry for unexplained shortfall.

## 0.38.3

- Add a native Windows Python launcher and runtime probes, preserving workload arguments and exit status; use portable launcher commands in the existing adversarial-log instructions.

## 0.38.2

- Add a portable `python` fallback for hosts that only provide `python3`; Develop hooks also probe for Node and report once when hooks cannot run.

## 0.38.1

- Require strict score descent for unchanged scope; plateau, increased score, stale evidence or a stricter caller budget stops the loop. Completion questions are conditional on missing decisions or authority, and authorized next actions continue without an unconditional skill menu.
- Remove the arbitrary three-review cap: strict integer score descent bounds reviews while stricter caller budgets still apply.

## 0.38.0

- Record append-only adversarial round logs with a split header and staged-append protocol while retaining historical-reader compatibility.
- Make debug’s hypothesis ledger append-only and select review depth from the Codemap code path rather than diff size. Run the challenger at every diff size; `--challenge` becomes a compatibility no-op and `--no-challenge` disables it.
- List Codex-eligible mechanical findings under Recommended Next Steps; review names candidates for the fix workflow or user and never invokes `bridge:implement` or edits code itself.

## 0.37.1

- Correct Develop’s GitHub repository API allow rule and migrate only the exact obsolete entry in setup.

## 0.37.0

- Add `dev_test_targets.py` to select focused tests from fresh, complete Codemap impact data or fall back to name/import heuristics; record selections in `test-targets.jsonl`. Feature, fix and refactor loops run targeted tests, then require a fresh full-suite gate after any later fix. Agent-watch notifications/deadlines have an explicit timeout fallback.

## 0.36.1

- Correct a stale step reference in `develop:debug` and split long Markdown prose without changing its instructions.

## 0.36.0

- Create worktrees before asking about existing trees, and report a leaked worktree only when creation retries fail with a matching path or branch.
- Warn when loaded plugin paths are stale and clean up report-template attribution.

## 0.35.0

- Deliver Develop’s quality-gate deltas only when Foundry ownership is verified; tests compare the full rule with its reduced delta.

## 0.34.0

- Batch non-overlapping edits and tests into fewer test runs in `develop:feature` and `develop:refactor`; failures bisect the batch, and `--no-batch` opts out. Probe parallel runners, merge doctest collection, retry focused failing nodes and classify isolation failures.
- Breaking report change: use the Final Report Doctests values `pass` / `fail` / `not-merged`, replacing the earlier pass/fail contract; failed merged collection uses a separate doctest pass and reports `not-merged`.
- Repair zsh multi-token runner expansion and pipeline-exit capture across workflows.

## 0.33.4

- Split long helper functions in Develop’s `bin/` modules and add `main()` docstrings; refresh its blueprint manifest.

## 0.33.3

- Move six Codemap test files into `tests/codemap/` and update affected repository-root anchors.
- Expand adversarial review coverage to first-pass inventories, invariant/original/consumer/sibling evidence, finding-origin accounting and the loop-actions schema 2.

## 0.33.2

- Reject CLI-option-shaped Codemap module names before subprocess calls, contain Git-artifact cleanup within the selected repository, atomically replace propagated rules, and handle oversized audit lines without crashing the verifier.
- Make `develop:review` fail closed when its run directory is missing, correct challenger-severity and checklist handling, and surface breaking changes explicitly for background callers without a foreground question path.

## 0.33.1

- Record challenge progress cumulatively and require a later independent review after fixes; keep completion bound to validated rounds and the caller’s source, role and evidence gates.

## 0.33.0

- Add scoped reviewer ratings and a finding-author column to `develop:review`, preserving the aggregate verdict without averaging ratings. Ratings distinguish approval, minor changes, changes required, insufficient evidence and block/reject; missing reviewers remain unrated, and the legend, summary and findings overview travel with the header.

## 0.32.3

- Show cumulative adversarial-loop progress after each completed review while keeping unresolved and unavailable reviews distinct from clean results.

## 0.32.2

- Preserve Codemap package-initializer queries across mixed diffs and Windows/source-layout paths; keep incomplete preflight data partial and legitimate source reads available.

## 0.32.1

- Block the review follow-up until the report exists and its complete header is visibly delivered.

## 0.32.0

- Add an isolated blind-solve reviewer to `develop:review` for feature or mixed changes only when an active plan names an affected module; compare its blueprint with the implementation and report explainable divergences as questions.
- Refresh the review compaction contract and hint before follow-up.

## 0.31.0

- Read Codemap’s shared context live from its active install, remove `--semble`, and retain the standalone path fallback. Add bounded shutdown timeouts to Develop session hooks.

## 0.30.6

- Compress prose and nonfunctional code comments across Develop skills, templates, shared files and rules while preserving frontmatter, headings, code, load paths and functional comments.

## 0.30.5

- Preload deferred task-tool schemas before task-hygiene calls. Clarify OSS PR-scoped versus Develop timestamp-scoped report paths and repair README rendering and anchors.

## 0.30.4

- Include unchanged consumers and the next ordinary user action in workflow reviews; require outcome probes for identity, destination, lifetime, retry and resume assumptions.

## 0.30.3

- Adopt the bounded adversarial convergence procedure for review-and-fix cycles, with validated independent evidence, fresh source snapshots and explicit stop or recovery decisions.

## 0.30.2

- Restore four quality-gate obligations in the Develop rule copy: route files by copy intent, avoid option questions whose actions cannot be executed, merge instruction blocks without losing rules, and record deferred work in the delivered artifact.

## 0.30.1

- Parse skill flags in shared helpers, avoiding zsh’s empty `BASH_REMATCH`; derive feature and plan Codemap targets in `derive_codemap_target.py`.
- Use TSV only for supported query shapes and feature-probe it, falling back to JSON with older Codemap CLI installs.

## 0.30.0

- Add a weighted loop capped at three independent review/fix iterations; stop on plateau or non-convergence, and leave structural findings unapplied until explicit approval for separately scoped work.
- Dispatch reviewers in the background, end the turn and resume on completion notifications; bound inactivity handling to about 15 minutes and retain partial/error outputs instead of silently omitting failed reviewers.

## 0.29.2

- Extract argument-target parsing, review-target resolution and skill-compaction contract writing into tested Python helpers; persist the tried-candidate ledger so resumed debugging uses state that was actually written.

## 0.29.1

- Ship Develop-local Codemap context and gate contracts so standalone installs do not depend on another plugin’s private shared directory; prefer the active install and fall back to file reads when Codemap is unavailable.

## 0.29.0

- Register the shared Bash allow-audit hook in Develop, preserving the existing command decisions while adding an audit trail.
- Record ranked allow-hook decisions and completion/session events, storing command digests rather than raw commands; audit can be disabled, and verification/pruning does not make the log tamper-evident.

## 0.28.1

- Clarify bridge detection, diagnosis containment and JSON recovery contracts, and strengthen helper tests and test-marker metadata.

## 0.28.0

- Add a shipped deny list and permission-merge setup step for Develop, with backup, validation and rollback if settings are malformed.

## 0.27.1

- Replace routine self-review and score-triggered adversarial passes with named-gap triggers; retain findings before presentation filtering and raise feature/refactor effort to xhigh.

## 0.27.0

- Merge paired review dimensions into one spawn, enforce fan-out limits and bound feature iteration by cycle and wall-time caps.

## 0.26.0

- Replace the legacy Codex integration with explicit bridge calls for implementation, advice and review; preserve unavailable behavior and local review fallbacks.

## 0.25.1

- Close sentinel-read hook allow bypasses found by adversarial review while retaining the reviewed read-only command cases.

## 0.25.0

- Add a protected-file write guard to Develop and document the propagated blueprint allow hook in its README.

## 0.24.0

- Add shared stale-lock and worktree healing with repository-root containment; reduce review fan-out to keep total agents bounded.

## 0.23.0

- Add the blueprint-manifest auto-allow hook to Develop while preserving the existing read-only command policy.

## 0.22.2

- Replace runtime test skips with collection-time capability markers and synchronize Develop documentation with current commands and skill roster.

## 0.22.1

- Anchor Codemap index and logs at the Git root, and resolve module names from the index so Develop queries work from subdirectories and do not guess from paths.

## 0.22.0

- Choose a task-fit Codemap route before fix retrieval; use no query for a fully localized edit unless a structural fact or explicit tool request remains unresolved.

## 0.21.1

- Trim overlong comments in Develop workflow scripts without changing commands or functional rationale.

## 0.21.0

- Rank only review dimensions selected by scope and evidence, cap default fan-out at four, and let `--full` run every eligible dimension without widening the scope.

## 0.20.0

- Extract Codemap gating, issue fetching and worktree setup into tested Python helpers, and split team-mode prompts into loadable files. Add a language preflight before pytest runs in projects without Python markers.
- Check review-header delivery in the transcript as a nudge before follow-up; the hard delivery gate arrives later.

## 0.19.1

- Add shared-path fallback guards at every resolver call so zsh and broken installs cannot silently use an empty path.

## 0.19.0

- Add `/develop:setup` to install and clean up only Develop’s namespaced rules without editing settings or another plugin’s rules.

## 0.18.5

- Use closed option sets for Develop’s scan-source and spec-type values, deriving command choices from the accepted values.

## 0.18.4

- Resolve shared files from Develop’s own installed plugin rather than Foundry’s cache or user-level skill links, allowing standalone use.
- Ship Develop-local quality-stack, Codex and validation/handoff instructions so standalone workflows retain those protocols.

## 0.18.3

- Repair shared-shell state and sentinel handling so Develop workflows retain paths and flags between calls.
- Add explicit `--issue N` mode and improve report/header and sentinel recovery paths.

## 0.18.2

- Add a fail-open, expiring hook that blocks an active review follow-up until its report file exists; printing and delivering the header remains a skill obligation.

## 0.18.1

- Keep report header table cells on one physical line so long values cannot break the rendered table.

## 0.18.0

- Add `--worktree` to feature, fix, debug, refactor and review; edits run in a HEAD-based worktree and report its branch/path for manual integration. Plan remains read-only and does not accept the flag.
- Worktrees start from committed `HEAD`; debug diagnoses and review reports return to the main tree for manual integration.

## 0.17.4

- Require Develop review to print its report header before asking the follow-up question.

## 0.17.3

- Label injected structural context as `codemap-py` throughout Develop and keep debug-to-fix result matching in sync.

## 0.17.2

- Migrate Develop’s structural context and test-impact calls to the `codemap-py` CLI while preserving existing preflight behavior.

## 0.17.1

- Point Develop’s Codemap wrappers and fallback paths at the dual-runtime `codemap-py` package.

## 0.17.0

- Propagate the sentinel-read allow hook to Develop so pre-canned read-only state reads no longer trigger avoidable permission prompts.

## 0.16.2

- Use Codemap `diff-impact` for function-level review context; remove bare-module calls to `fn-rdeps`/`fn-blast` while retaining module-level `rdeps` queries.

## 0.16.1

- Render YAML report metadata as a two-column Markdown table while retaining machine-readable YAML in the saved report.

## 0.16.0

- Add the session ID to temporary sentinels so parallel sessions and projects do not share state.

## 0.15.8

- Move the source directory to `plugins/cc_develop/` while preserving the installed `develop:` namespace.

## 0.15.7

- Review public API changes against neighboring symbols and established naming, placement, parameter and return-shape patterns.

## 0.15.6

- Make Codemap helper loads version-proof and correct build/rebuild wording across Develop’s shared gates.

## 0.15.5

- Use `cat` for shared-document loads so documented shell permissions survive plugin updates, remove a superseded team-prompt copy and ship a Develop permission-allow manifest.

## 0.15.4

- Compress Develop’s Markdown prose while preserving commands, code, frontmatter and functional annotations.

## 0.15.3

- Align Develop’s issue-ID routing and remove a review flag step that the parser did not support.
- Recover the final top-level JSON object from helper output and repair router timeout handling.

## 0.15.2

- Add explicit `--help` and argument validation to Develop’s bin scripts.

## 0.15.1

- Move shared Codemap context and currency gates into the provider contract while keeping Develop’s flags and graceful fallback in its own wrapper.
- Change Develop’s plugin license to Apache-2.0.

## 0.15.0

- Use indexed symbol spans for targeted edits in larger files; fall back to full reads when spans drift or replacement text is ambiguous.

## 0.14.0

- Reuse test-impact results from debug in fix while the index remains current; size plans from structural dependency and coupling data.

## 0.13.0

- Batch Codemap preflight queries for Develop review in one process and extract the query builder into a tested Python helper.

## 0.12.2

- Prioritize Codemap results in reviewer prompts; recognize direction-scoped `query_complete` with the legacy `exhaustive` alias and invoke the Semble companion when available for incomplete context or a missing index.

## 0.12.1

- Document that Develop never auto-invokes OSS skills and the shared quality loop escalates to `develop:review` only when a concern signal warrants it.

## 0.12.0

- Add per-skill compaction contracts to Develop review, feature, fix, refactor and debug so the workflow can resume from persisted state after context compaction.
- Add `--keep` context preservation and harden argument parsing for tokenized and quoted values.

## 0.11.7

- Skip the default challenger on small single-file changes while allowing `--challenge` to force it; `--no-challenge` still disables it.

## 0.11.6

- Fix cross-platform runner handling, test ordering and flag spelling across feature, fix, refactor and debug.
- Gate characterization tests on unmodified source before refactoring, then rerun the same tests afterward. Failed or no-test prechecks require recovery, explicit acceptance of proceeding without a safety net, or abort.

## 0.11.5

- Persist shell state between calls so Develop review retains its selected target and shared path, while feature keeps its Codemap module target.

## 0.11.4

- Synchronize Develop’s agent router with the canonical implementation, enforce source-drift checks and add SessionStart agent indexing with optional OpenAI-embedding/Anthropic matching and fallback.

## 0.11.3

- Correct Foundry detection in Develop’s agent fallback so installed specialist agents are no longer silently replaced by general-purpose agents.

## 0.11.2

- Verify the user’s premise before debugging, and use a synthesis-only general agent to consolidate local review findings.

## 0.11.1

- Remove self-evident shell comments from Develop skill bodies while preserving comments that explain constraints.

## 0.11.0

- Restore missing/stale Codemap index gates in debug, feature, fix, plan and refactor, and document automatic build/rebuild prompts.

## 0.10.9

- Require resolved literal paths in reviewer prompts and preserve agent descriptions when the router rewrites dispatches.

## 0.10.8

- Clean feature/refactor team sentinels on exit, move trigger/skip guidance into skill descriptions and surface strict Codemap failures.

## 0.10.7

- Add two-tier Codemap currency checks and wire missing/stale-index gates into all six Develop workflows.

## 0.10.6

- Query direct reverse dependencies before blast-radius results in Develop review, and warn when Codemap context is unavailable.

## 0.10.5

- Query direct function callers before transitive impact and move feature team/report plus review consolidator prompts into loadable templates.

## 0.10.4

- Consume Codemap `test-impact` to select affected tests in feature, fix and refactor, falling back to the full suite; record query counts, completeness, coverage caveats and gaps.

## 0.10.3

- Add Codemap preflight checks to Develop review so index materialization and per-agent queries are explicit before findings are written.
- Refresh changed Codemap files incrementally and query richer function, test and documentation context before review findings are written.

## 0.10.2

- Remove comments that merely restate Develop workflow commands while preserving rationale and behavior.
- Add a `Title` field to report metadata.

## 0.10.1

- Classify changes by intent, including chore work, and align report metadata with explicit cosmetic-through-critical severity sections.

## 0.10.0

- Add premise verification before Develop workflows form a diagnosis or design; review findings must cite file and line evidence and meet the source threshold.

## 0.9.4

- Honor `CODEMAP_INDEX_DIR` in Develop’s Codemap consumers, allowing indexes outside the default project cache.

## 0.9.3

- Migrate debug, feature, fix and refactor to the shared skill-driven argument parser; persist parsed flags in per-skill and compatible legacy temp files instead of caller-side eval blocks.

## 0.9.2

- Add trigger and skip metadata to Develop skills, and clarify Python-project eligibility and CI-only failure handoffs.

## 0.9.1

- Persist run-directory and parsed-argument values across fresh shell calls, and derive Codemap targets from the active traceback or symptom.

## 0.9.0

- Add the shared argument parser, automatic Codemap context with strict/off overrides, and safer issue/test helper argument handling.

## 0.8.1

- Replace the pinned Haiku model ID with the `haiku` alias in Develop’s agent router.

## 0.8.0

- Add `--repo` to feature, fix and debug issue fetching and parse it in refactor for consistency; add two-wave feature teams and per-agent debug health sentinels. Persist plan flags across calls, bound plan/debug dispatch depth and distinguish public breaking changes from internal signature-change risks.

## 0.7.5

- Pass the resolved diagnosis path directly from debug’s follow-up question into `/develop:fix`.

## 0.7.4

- Update Develop’s runtime and documentation references to the consolidated Foundry setup entrypoint.

## 0.7.3

- Update Develop’s Codemap references to the unambiguous `scan-codebase` and `query-code` skill names.

## 0.7.2

- Remove a trivial Codemap flag helper in favor of direct skill prose and extend the plugin’s hook and workflow regression coverage.
- Use inline doctests for simple feature contracts and example scripts for complex ones before converting them to formal pytest tests.

## 0.7.1

- Persist and reload the Codex output path across shell calls, and distinguish failed reviewer outputs from roles that were legitimately skipped.

## 0.7.0

- Replace Develop shell utilities with cross-platform Python CLIs and update skill callers; keep only permitted shell wrappers.

## 0.6.2

- Narrow debug tool permissions, remove debug/feature effort overrides and update test/configuration support.

## 0.6.1

- Restrict Codemap scans to the project tree and cap them at 2,000 Python files; sanitize project and sentinel names, validate issue IDs and pytest commands, parse diagnosis paths without shell evaluation, and reject missing diagnosis files.
- Add a non-Python preflight to feature; feature's lead or software engineer owns primary red-green tests, while QA adds edge-case and regression tests after implementation. In fix, retain the challenger on diagnosis handoff, stop for a decision without a reproduction path, and preserve pre-existing regression tests.
- Gate mixed feature/refactor goals and broad refactors spanning 3+ modules, 5+ files or any public-API rename; require a scope decision before proceeding.

## 0.6.0

- Extract structural scan logic into `codemap_scan.py`; add pytest-gate, short-run and worktree shell helpers and connect their skill callers.

## 0.5.3

- Add plugin-owned helpers for issue fetching, shared-path resolution, run directories and Codemap flags; reduce repeated shell parsing in Develop skills.

## 0.5.2

- When review receives a PR-like argument, detect whether OSS is installed before routing and ask explicitly when its PR workflow is unavailable. Treat pytest exit 5 as a no-tests/collection condition, cap refactor cycles, and author new QA reproduction tests only when no relevant existing test is found; strengthen weak existing tests instead.

## 0.5.1

- Fix Develop’s shared-path guard so SemVer rules resolve correctly before a fix workflow uses them.

## 0.5.0

- Standardize report YAML headers, add the report path, list challenger in review fallback roles and adjust shared-path fallback handling.

## 0.4.0

- Accept full GitHub Actions URLs in `--ci-run`, extract CI log handling and inline plan generation into shared helpers, and repair feature team-mode dispatch and fix-plan initialization.

## 0.3.15

- Remove the explicit `opusplan` model override from Develop review so runtime model selection applies.

## 0.3.14

- Limit deprecation checks in Develop review to public API surfaces.

## 0.3.13

- Separate temporary reviewer handoffs from final reports under `.reports/review/`; the consolidated Develop review names the source handoff files.

## 0.3.12

- Add the breaking-change gate to fix and plan work, and preserve the confirmed diagnosis in debug/fix handoffs.

## 0.3.11

- Add `--team` modes with explicit reviewer/teammate dispatch and keep the shared quality-stack and Codemap contracts aligned with their callers.

## 0.3.10

- Keep issue-ID normalization and fetching in one shell call, repair Foundry fallback paths, recognize team flags, and distinguish workflow routing metadata.

## 0.3.9

- Extract reusable preflight handling; detect unsupported flags and ask whether to abort or continue ignoring them.

## 0.3.8

- Make Codemap and Semble opt-in with explicit flags; strict preflight stops with a clear status when a requested tool is unavailable.

## 0.3.7

- Extract task-hygiene instructions for reuse across Develop workflows and bound agent-router matching with a five-second timeout.
- Parse `--plan` and `--diagnosis` handoffs in fix/refactor and fail fast when the referenced file is missing. Offer Codemap’s optional Semble companion query only when structural coverage is incomplete, then combine reverse-dependency results.

## 0.3.6

- Add a three-tier agent-router fallback: exact installed agent, optional LLM-selected local role, then `general-purpose`. Require a selected follow-up skill to run in the same turn.

## 0.3.5

- Require analysis agents to self-check claims and coverage, and route low-confidence work for adversarial review when available. Debug issue mode fetches the issue first and runs a referenced test before falling back to the full suite when the path is absent or invalid.

## 0.3.4

- Centralize Codemap context and task hygiene in shared Develop files; add challenger coverage and reliable `--plan`/`--diagnosis` parsing across applicable modes.
- Protect existing plans with a counter suffix instead of silently overwriting them.

## 0.3.3

- Reconcile existing tasks before creating new ones and correct the plugin-prefixed workflow dispatch and scope boundaries.

## 0.3.2

- Run a read-only adversarial challenger across Develop’s six workflows by default; `--no-challenge` skips the extra review.

## 0.3.1

- Accept issue identifiers with or without a leading `#` in feature, fix and debug commands.

## 0.3.0

- Keep local review in `develop:review` and reserve `oss:review` for pull requests; integer input to local review now redirects to the PR workflow.

## 0.2.1

- Extract agent fallback and test-runner detection into shared sidecars, add a bounded refactor cycle cap, and reuse Codemap blast-radius context for multi-file work. Missing Codemap indexes now return a failing status so callers can gate correctly.

## 0.2.0

- Separate `PYTEST_CMD` from the project test runner and detect the repository’s runner before testing. Add `--plan` and `--diagnosis` handoffs, early exit-code gates, and persisted checkpoints to feature, fix and debug flows. Review loops now stop when findings stabilize.
- Persist structured plans with feasibility review/resolution and Codex validation before implementation.

## 0.1.0

- Establish the standalone Develop plugin with feature, fix, refactor, plan and debug workflows under its own namespace.
- Extract shared branch-safety, quality-gate, review-loop and Codex-delegation instructions for Develop workflows to reuse.
- Remove the former Foundry Develop skill copy after moving its workflows into the plugin.
- Add `develop:review` for local Python files, directories and diffs, with six scoped specialist roles consolidated into a report.
- Add external-API detection/fetch/citation checks, surfaced assumptions and five-axis feature/fix quality scans.
- Inject optional Codemap hotspots and dependency/reverse-dependency context into reviews, with risk tiers.
- Remove Foundry as a required dependency; absent named agents fall back to `general-purpose` with role descriptions.
