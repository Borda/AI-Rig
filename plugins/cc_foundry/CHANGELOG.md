# Changelog

## 0.67.2

- Add a no-restating rule to the question-text guidance: a question never repeats rows, items or findings that the last printed text already shows or that its own options carry.
- Clarify the version-one review publication in the cross-validation protocol: string-array evidence and limits, open concerns only for UNVERIFIED or pending candidates, no complete readiness beside an open candidate.

## 0.67.1

- Show reviewer roles without execution provenance and render ratings in square brackets, keeping provenance in evidence and limits. Update the shared protocol and Foundry README to match.
- Register Develop’s historical index-directory configuration note as documentation so the Codemap guard check accepts the changelog while retaining executable guard checks.

## 0.67.0

- Add a boundary gate that checks calls-ahead and current context against estimated rebuild cost, verifies the owned skill-contract digest, attempts to log each decision, and prints the contract only when compaction is warranted.
- Add Claude and Codex context-composition reports, including usage deduplication and compaction detection; harden transcript parsing and token bounds in the cost analyzer.
- Extend skill-contract metadata with validated gate parameters and a session-scoped ownership stamp; save verified contracts at PreCompact and restore them after compaction.
- Adopt explicit compaction boundaries in phased workflows, with a safe continuation fallback when Foundry is unavailable and phase-specific preservation of reports and state.
- Build edited-file context from session timings, including MultiEdit and NotebookEdit, instead of relying on a truncated transcript tail.
- Require follow-up questions to name the report path and key outcome fields within the text budget; move concise plans and decision context into question text while retaining full report headers for final replies.
- Clarify compaction authority, question formatting, and approval scope in Foundry rules and CLAUDE.src.md.

## 0.66.1

- Require parent validation of review candidates at every severity before publishing actionable findings, retaining the source revision, contract, trigger, checks, counterevidence, environment, limits, and disposition.
- Separate source judgment from verification readiness; distinguish confirmed, refuted, already-fixed, optional, unfinished, and unavailable concerns, and preserve unresolved obligations with an owner and closure condition.
- Version the review ratings and finding records while preserving historical meanings and archive readers; reject incompatible current consumers and partial contract downgrades.
- Carry validation identity and retained evidence through report merging, dispatch, remediation, and late-wave checks; reuse proof only while it still applies, and keep a rejected repair separate from refuting the underlying defect.
- Align Foundry shared protocols and README guidance with the cross-plugin review contract.

## 0.66.0

- Guard `gh` writes with `gh-write-guard.js`: exact writes require a user-approved single-use grant, and force updates remain denied even with a grant.
- Add approval provenance through `approval-guard.js` and grant records: standing approval applies only to plain local commits; pushes and GitHub writes consume one-use grants, with self-grant attempts rejected.
- Generalize the shell Git lexer for Bash and Zsh invocation differences, and use it when checking commit-guard push commands.
- Add an optional GitHub-read lane to allow dispatch and allow audit verification to accept two- or three-lane verdicts.

## 0.65.0

- Show report headers, plans, findings, trees, and queues once as option previews, moving oversized output to a named file and unknown-flag or blocker prompts into the question text.
- Make `foundry:sw-engineer` spawns choose worktree isolation at the call site; update distill execution clusters to transplant each worktree patch and keep management scaffolds in the caller's tree.

## 0.64.9

- Add `rule-inject.js` to load spawn and commit rules lazily after the first matching tool call, including failed calls and compacted sessions.
- Deny `git push --force-if-includes` and `git push --mirror` as explicit backstops, and update Foundry's commit permission guidance.
- Resolve the current Haiku model at runtime with a 24-hour cache and fallback; disable thinking, raise its token allowance, and report resolver failures instead of treating them as an empty result.
- Put compact and repeated-failure guidance in the actual follow-up question, and update the model-window and cache-write guidance.

## 0.64.8

- Ground completion claims in cited source evidence and a representative real case; retain the real-case check through investigation, implementation, and verification.

## 0.64.7

- Wait briefly for transcript flush before rejecting a just-printed report header; explain visible-text failures and permit one identical follow-up retry without printing the table again.

## 0.64.6

- Make audit and profile delivery hooks check the current report at both the follow-up question and turn end; keep diagnostic and recovery questions available when delivery evidence is missing.
- Standardize confidence gaps as per-gap deductions that sum to the reported shortfall, including in calibration prompts and report templates.

## 0.64.5

- Add POSIX and native-Windows `python` launchers that select an eligible Python 3.10+ runtime, preserve workload arguments and exit status, and leave a real `python` earlier on PATH in control.

## 0.64.4

- Add a SessionStart rule check that detects missing setup-created `foundry-<name>.md` links, names the rule files to load, and injects the commit rule so an incomplete setup does not silently omit commit policy.
- Route registered hooks through Node.js from PATH or supported fixed locations, then exit cleanly when no Node runtime is available.
- Require co-author trailers to replace host-default attribution, with Codex attribution when Codex shaped the work and Claude attribution when Claude shaped the diff.

## 0.64.3

- Continue independent review-and-fix cycles while the same-scope nonnegative integer score strictly decreases; stop on a plateau, increase, lost authority, or exhausted caller budget, with no arbitrary three-review cap.
- Clarify evidence and contract handling in Foundry quality gates and the audit workflow, and make the strict-decrease rule visible in the quality-gate example.

## 0.64.2

- Make audit and calibration histories append-only through `append_ledger.py`: stage audit findings in `.rec` files and append calibration's per-target result files with `--from`, preserving prior history and avoiding duplicate legacy migration.
- Run isolation-motivated agent spawns regardless of task size, while retaining the ordinary budget rules for other spawns.

## 0.64.1

- Correct the GitHub repository API permission pattern and migrate only the exact retired setup entry, so unrelated permission entries remain untouched.

## 0.64.0

- Replace agent-wait polling with per-agent deadlines armed at spawn and checked on each wake; mark a missed deadline or notification without output timed out immediately.

## 0.63.2

- Register propagation of the shared native-question MCP implementation from Codex Rig to Codemap and Bridge.

## 0.63.1

- Record approved setup conflicts using the `rules/foundry-<name>.md` filename that the next phase checks, so approvals are recognized during linking.
- Split long Foundry prose into Markdown lists and shorter paragraphs.

## 0.63.0

- Warn at SessionStart when the loaded Foundry plugin root is marked orphaned or differs from the installed path, and tell the user to restart without blocking the session.
- At SessionEnd, sweep only registered idle `agent-*` and `oss-*` worktrees; retain dirty trees, ignored run output, other prefixes, and orphan directories.
- Shorten report template fields while keeping authoring guidance outside the completed report.

## 0.62.1

- Add audit and distill architecture notes describing phase overlap and ownership, and align the skills with those documented boundaries.

## 0.62.0

- Add audit efficiency checks for removable setup, loader, contract-write, and bookkeeping work while preserving model-decision boundaries.
- Load shared task-hygiene guidance in workflows and gate parallel spawn width using model-specific ceilings.

## 0.61.0

- Add a Foundry notebook-authoring rule covering notebook narrative, cell boundaries, plotting, and shell usage; propagate the canonical shared style to the plugin.

## 0.60.0

- Persist shared quality-stack variables across Bash blocks so batch mode and shell-variable checks use the intended values.
- Probe xdist for wide runs while keeping diagnostic retries serial; check collection before folding doctests into pytest, classify doctest assertion failures as genuine, quote failed node IDs through eval, and explicitly report the degraded unmerged doctest path.

## 0.59.2

- Split oversized Foundry bin helpers into smaller functions with maintainer-facing `main()` docstrings, improving navigation without changing their command contracts.

## 0.59.1

- Cap batched agent spawns at the configured stall bound so a single batch cannot exceed the workflow's wait budget.

## 0.59.0

- Strengthen producer-to-consumer evidence checks and validate host-manifest agreement and changed shipped version or schema continuity against committed HEAD; reject skipped or downgraded values, removed tracked manifests, and ambiguous unsupported selections.

## 0.58.2

- Set model-specific spawn ceilings and use them to batch Foundry audit and adversarial waves.
- Harden Foundry path utilities against destination overwrite, command injection, and traversal; bound file sizes and make hook-registry matching literal rather than regex-based.

## 0.58.1

- Bound independent review to three rounds including baseline, show the cumulative table once after each validated round, and require a parent action record for every unresolved finding.

## 0.58.0

- Add reviewer attribution and current-tool activity to the status line and task log, and clarify scope and handoff rules for adversarial review cycles.

## 0.57.5

- Show the full cumulative old/new findings table after each validated review round, preserving previous rows and excluding verified-fixed and rejected findings.

## 0.57.4

- Tighten Foundry's Codemap consumer contract: reuse only matching fresh answers, check query completeness, cap supplementary queries, and keep direct source reads available when Codemap is absent or incomplete.

## 0.57.3

- Add the short and detailed native-question guides to Foundry's propagation manifest so independently installed Codex-facing plugins can ship synchronized copies.

## 0.57.2

- Gate audit and profile follow-ups on saved report evidence and current-turn delivery of the required summary or matching header table; keep diagnostic and recovery questions available.

## 0.57.1

- Add an idle-compaction contract explaining how long-open questions can expire prompt-cache benefits and what context to preserve before compaction.

## 0.57.0

- Resolve the active Codemap provider's shared gates and context at runtime instead of shipping stale copies; degrade to Foundry's fallback when the optional provider is absent.
- Set a 15-second timeout on SessionEnd task-log and audit-close hooks.

## 0.56.2

- Shorten Foundry agent, skill, rule, and reference prose.

## 0.56.1

- Update SessionEnd retention for PR-scoped numbered OSS review runs: expire individual runs after thirty days and remove empty PR parents while retaining legacy flat-run handling.

## 0.56.0

- Preload the task tools before use and clarify that task-list state is session-scoped, so an empty list does not prove no work is running.

## 0.55.5

- Extend Foundry audit and adversarial guidance to check producer-consumer handoffs and ordinary user actions, keeping evidence and authority gates active across the whole route.

## 0.55.4

- Add a canonical shared-procedure copy and propagation check so Foundry installs the same procedure content that its sibling consumers receive.

## 0.55.3

- Require each agent task to begin with a distinct delta-first name, description, and task-leading line; check duplicate slots and follow the live tool fields rather than stale examples.

## 0.55.2

- Make independent Fetch, Read, and Match verification explicit for every URL, including when another URL on the same domain was verified.

## 0.55.1

- Replace Foundry skill flag parsing based on Bash-only regex capture and ad hoc text matching with shared argument parsers that work under Zsh as well as Bash.

## 0.55.0

- Add severity-weighted independent review and remediation loops with an original default cap of three iterations. Use score ratios to continue or stop; stop on structural findings or repeated failure signatures, and block completion or commits while security or critical findings remain open.
- Replace synchronous or polling assumptions with background-spawn, end-turn, and completion-notification handling; preserve missing-output timeout reporting, update audit checks for the background contract.

## 0.54.2

- Extract repeated Foundry skill shell blocks into Python helpers while leaving judgment steps, agent prompts, and output templates in the skills.
- Fix install-state detection for Claude's nested installed-plugin registry shape, and let investigation contracts record tried and probed candidates for safe post-compaction resume.

## 0.54.1

- Align Foundry Codemap guidance with the provider-owned consumer contract, preserving the active plugin root and the missing-dependency fallback.

## 0.54.0

- Unify auto-allow decisions under ranked provenance and shape dispatch; append decision and completion audit records, and add canonical record hashing with verify/prune commands. Logs are not tamper-evident. Verification exits 1 for record-hash mismatch, while framing and schema findings remain warnings; unusable inputs exit 2.
- Use ASCII status labels for success, no-op propagation, and orphan warnings so Foundry CLI output works on legacy Windows consoles.

## 0.53.0

- Expand the Foundry Python rules for cross-platform executables and semantic test selection, while trimming duplicated repository-level guidance.

## 0.52.1

- Expand maintainer-facing contracts and executable examples for Foundry Python helpers, including bridge checks, resolver-site classification, cost analysis, and timing analysis.

## 0.52.0

- Add explicit denials for dangerous Git operations and Claude's permission-bypass flag to Foundry's plugin permission policy.
- Add an untrusted-content rule: fetched pages, thread bodies, and pull-request text remain data, even when they contain instruction-like text.

## 0.51.3

- Add severity/fix requirements and precision discipline across foundry agents (challenger, creator, curator, doc-scribe, solution-architect, sw-engineer, web-explorer) so every finding states severity, cites a named criterion, and speculative items are labelled rather than dropped
- Reconcile self-recheck-ban premise (CLAUDE.src.md, quality-gates.md, rules/debugging.md, rules/\_full/debugging.md) with named-trigger re-scans already required elsewhere (shepherd.md, challenger.md)
- Consolidate model tiering to the 5-family: foundry:linting-expert haiku to sonnet, synced across curator.md's tiering table and README

## 0.51.2

- debugging.md (+\_full): forbid dispatching foundry:challenger as subagent_type fork; challenger gets diff+symptom+spec only, no implementer reasoning trail
- Stop after the same failure signature twice rather than exhausting the iteration cap; keep approved plans read-only during implementation unless the user explicitly authorizes revision.

## 0.51.1

- Define spawn discipline in Foundry configuration: use work-displacement spawns only when expected work exceeds their overhead, while preserving isolation-motivated review and role spawns.

## 0.51.0

- Propagate Foundry-owned quality-stack and cross-validation guidance into consumer plugins as namespaced files, with shared-file checks preventing drift.

## 0.50.1

- Add a pre-commit guard against references to tracked, repository-private ignored documents, including path traversal and separator variants.
- Tighten Foundry's specialist references while preserving routing rules, commands, thresholds, and execution contracts.

## 0.50.0

- Replace Foundry's legacy Codex rescue integration with explicit bridge implement, advise, and review calls, including unavailable and disabled behavior.
- Resolve the active plugin registry's nested structure, distribute Foundry's availability detector to each independent consumer, and add an audit utility that compares local Bash transcripts with installed blueprint manifests.

## 0.49.2

- Document Foundry's consumer-owned `skills/_shared/codemap-context.md` as the host for the v2 integration body, retaining v1 sentinels and read-only integration audit. It ships without a managed block until integration applies it.

## 0.49.1

- Close sentinel-read auto-allow bypasses by rejecting unsafe shell functions, traversal, write redirection, and loader or lookup assignments; retain only validated read forms.

## 0.49.0

- Guard writes to protected repository files and provide a recovery tool for Git metadata artifacts damaged by hooks.

## 0.48.0

- Add `heal_git_artifacts.py` to safely recover stale skill locks and orphaned Git worktrees, and propagate its canonical Foundry implementation through the shared-file manifest.

## 0.47.0

- Add a blueprint manifest builder and auto-allow hook so exact, predeclared skill commands can run without repeated approval prompts.

## 0.46.2

- Restrict Foundry test skips to collection-time capability probes, and consolidate polluter-finder coverage under one canonical test module.

## 0.46.1

- Anchor Foundry Codemap context and query paths at the repository root, and provide canonical consumer guidance plus audit checks for declared provider routes and CLI flags.

## 0.46.0

- `foundry:session` becomes one inline skill with six modes — `dump`, `recall`, `list`, `park`, `sweep`, `drop`; `park`/`sweep`/`drop` are new, replacing the behavioural parking rule that only ever loaded on invocation and so never fired
- `hooks/carryover-restore.js` renamed `session-restore.js`; store path, injection banner and the oversized-doc pointer now read `/foundry:session recall <slug>`; `hooks.json` SessionStart command updated
- `rules/compaction.md` manual-sibling note, `rules/foundry-config.md` log table, and distill's slug-divergence guard no longer name removed skills or logs; `calibrate/modes/skills.md` had the same skill listed twice, bare and plugin-prefixed — the bare row is gone
- README rows, mode listings, hook description, skill-directory count and test table synced in both `plugins/cc_foundry/README.md` and `.claude/README.md`; version 0.45.0 to 0.46.0

## 0.45.0

- Add `/foundry:carryover` and restoration hooks to preserve selected work context across session handoffs.

## 0.44.0

- Extend `/foundry:profile` with cost analysis and classify hook and resolver activity alongside session timing.

## 0.43.0

- Add a batch-nudge hook and route mechanical, single-file audit fixes to a lower-cost implementation path under explicit gates.

## 0.42.2

- Compress duplicated bin-authoring gate wording across Foundry manage spawn prompts.
- Replace the estimated compaction break-even with measured session observations.
- Convert two Foundry audit/calibrate loader sites to the appropriate resolver calls; require Python variable documentation in `#:` comments where a docstring cannot bind.

## 0.42.1

- Add an agent-budget note to Foundry spawn sites: account for fixed spawn overhead, avoid work-displacement spawns below break-even, and require partial results when an agent exhausts its call budget.

## 0.42.0

- Extract Foundry's shared-document and mode loaders into Python bin helpers, replacing repeated resolve-then-cat blocks in skill bodies.
- Bound supplementary Codemap queries to three and stop enumeration after a complete result while preserving required source reads; budget spawns and require honest partial-output envelopes.

## 0.41.0

- Move subagent-type telemetry evidence and a worked failure example into `rules/_full/CLAUDE-full.md`, leaving the mandatory rule in the always-loaded stub.
- Require evidence before factual claims and explicit disclosure when verification is unavailable; move worked rule detail to on-demand `_full` files while retaining mandatory stubs.

## 0.40.1

- Move agent supplemental references into `references/`; re-key agent records so SubagentStop removes them, use separate worktree and non-worktree age limits, and strengthen audit Bash-correctness and efficiency checks.

## 0.40.0

- Install Foundry rules under namespaced `foundry-<name>.md` links and migrate legacy links during setup.
- Adopt a destination only when its target resolves within this Foundry install or its matching marketplace lineage; preserve links into other plugins, checkouts, or dotfiles as conflicts.

## 0.39.1

- Use named string enums for closed option sets and split compound pre-commit checks into individually selectable subchecks.

## 0.39.0

- Give each plugin its own shared-file resolver and remove Foundry-path reach-ins from consumer workflows, so installed siblings can operate independently.
- Add an orphaned-plugin-cache cleanup helper that reports by default and supports guarded explicit apply. Setup passes the displayed candidate count and aborts cleanup if that count changes.

## 0.38.4

- Preserve Foundry shell variables and sentinel newlines across separate Bash tool calls, preventing fresh-shell workflows from losing state.

## 0.38.3

- foundry:audit — enforce-audit-header.js; also adds the report.md Write step Step 11 was missing entirely (Path: field pointed at a file nothing ever created); gates on summary.jsonl instead of report.md since report.md is written after the gate fires
- foundry:profile — enforce-profile-header.js; parses REPORT_DIR out of the Step 1 KEY=VALUE state file

## 0.38.2

- Add audit Check 41d to detect legacy numeric phase headers and require the current letter-suffixed phase convention.

## 0.38.1

- Use a fresh, narrowly scoped agent for new independent follow-up tasks; resume only when accumulated reasoning is needed to continue the same task.

## 0.38.0

- Add audit Check 45 for dangling and asymmetric policy-sibling references.
- Add policy-reference graph tracing to curator evaluation criteria and the fix workflow.

## 0.37.4

- Give Foundry's review agents WebFetch and WebSearch so they can verify factual reviewer claims against authoritative sources.
- Require each YAML report-header table cell to stay on one physical line so wrapped values do not break the table.

## 0.37.3

- Require review workflows to print the report header before presenting a follow-up question; standardize outcome symbols and report formatting.

## 0.37.2

- Move Foundry's Codemap consumers to the provider's command surface and register Foundry as a host of the shared structural-context contract.

## 0.37.1

- Update Codemap references to the `codemap-py` name and stop propagating the Bash sentinel-read hook into its Python-only hook tree.

## 0.37.0

- Add a Bash pre-tool hook that auto-allows only validated read-only sentinel and timestamp command substitutions; unsafe shell forms continue through normal permission checks.
- Document the safe sentinel-read form and scope it to the active session and temporary directory.

## 0.36.1

- Render report `---` YAML headers as two-column Markdown tables in terminal (never raw YAML — wraps badly); add table template to foundry quality-gates; reword communication.md exemption
- Add foundry task-lifecycle "Fleet-view description: unique-first" note

## 0.36.0

- Scope temporary sentinels to the current Claude session to prevent collisions across sessions and projects; move skill contracts and logs to their intended writable locations.

## 0.35.6

- Move Foundry under `plugins/cc_foundry` and update repository references while preserving the `foundry:` command namespace.

## 0.35.5

- Extract plugin-table trimming into a tested `trim_plugin_tables.py` utility and document it in the Foundry README.

## 0.35.4

- Replace emoji confidence bars with distinct plain `fit` and `conf` scores, add runtime-state write/delete permissions, and put unique task labels before spawn boilerplate.
- Update the Foundry README rule table to match the confidence display.

## 0.35.3

- fixes headless block: sync.sh runs `claude --print /foundry:setup --approve`, where a Write-tool permission prompt on ~/.claude/settings.json cannot be approved and hangs; in-bash writeback needs no tool prompt

## 0.35.2

- Require code-touching agents to check declared dependency APIs before adding utilities and verify public symbols from the installed library rather than relying on memory.

## 0.35.1

- Add post-write artifact-compression feedback, task-log error breadcrumbs, option-local confidence bars, and soft output caps.
- Replace implicit skill loads with explicit Bash loads.

## 0.35.0

- Add a five-step confidence display before multiple-choice questions, with a marked leading option; omit the display when choices are matters of taste.

## 0.34.6

- Shorten Foundry agent, skill, rule, and shared Markdown prose.

## 0.34.5

- Require each commit-message claim to match the final diff before commit, so reverted or never-landed work is not described as part of the change.

## 0.34.4

- Limit Markdown normalization to Edit operations and write atomically; Read no longer rewrites files or redirects the user to normalized copies.

## 0.34.3

- Restrict the hook to push approval: force-push remains blocked, regular push needs a user-armed one-use token, and commits use the prompt authorization rule.

## 0.34.2

- CLAUDE.src.md: add Proportionality and Legible deviation to Core Principles; add diff-against-ask bullet to Verification Before Done

## 0.34.1

- Add a five-second timeout to the Foundry agent-router embedding request so a stalled connection cannot hang PreToolUse or SessionStart.

## 0.34.0

- Add `--help` handling to Foundry bin scripts and audit Check 42 for drift between documented skill flags and argparse definitions.

## 0.33.0

- Add /foundry:humanizer skill — strips AI-writing tells (clichés, triads, weasel-word attribution, formatting tics) from human-facing prose, grounded in Wikipedia's AI-writing-signs checklist
- Sync foundry README and root README skill table plus hook test-coverage counts
- Refresh worktree-agent activity on tool events and measure staleness from last activity so long-running active agents stay visible.

## 0.32.4

- Add a decision gate for when a design needs an ADR, require design branches to be resolved one at a time, and document a write-delegation checklist.

## 0.32.3

- Make the statusline tolerate a missing agents directory and merge Codex subagents into the agent count.

## 0.32.2

- foundry: calibrate now confirms via AskUserQuestion before large fan-outs — mode-category scopes (all/agents/skills/plugins) always confirm since exact spawn count isn't knowable until Step 2; narrow scopes confirm past a 50-spawn threshold; --apply alone no longer bypasses this gate, only --skip-gate does
- foundry: extract audit SKILL.md Steps 4/5/5b/7 into modes/steps-4-5-7.md (52KB to 37.8KB force-loaded on every invocation)
- foundry: extract sw-engineer.md packaging/API-stability guidance into packaging-patterns.md, mirroring the existing hook-authoring.md pattern

## 0.32.1

- Add context-cost discipline, cap low-confidence audit reruns through a consolidated batch, and cache manage schemas for 24 hours.

## 0.32.0

- Add explicit compaction checkpoints and preserve contracts to Foundry's phased workflows, with per-skill checkpoint data, optional keep-items, and cleanup on completion.

## 0.31.6

- Layer 2 (judgment by domain): audit Step 3 now groups all of a plugin's files into one holistic foundry:curator for plugin scope, so cross-file breaks (tool-grant mismatch, contract splits, dead dispatch) that per-file batching misses get caught.
- manage: /foundry:calibrate mandatory only on routing-surface (description/trigger) changes, not every content edit

## 0.31.5

- Remove Foundry's redundant preflight-helper load, prune repeated deterministic curator checks and unused color metadata, and correct README and hook paths.
- Add `state.py` set, load, and clear namespaces to persist small shell values across independent Bash calls.

## 0.31.4

- At SessionEnd, prune completed run directories and temporary artifacts older than 30 days while preserving incomplete runs with no result file.

## 0.31.3

- develop/review: REVIEW_ARGS empty -> always working-tree-diff; \_FOUNDRY_SHARED empty -> cross-validation always SKIPPED; develop/feature: TARGET_MODULE empty -> codemap rdeps skipped
- foundry/profile: SINCE/SESSION_ID/TOP_N + REPORT_DIR (timestamped, not re-derivable) now persisted/reloaded; foundry/session: MEMORY_DIR re-derived; foundry/setup: PLUGIN_ROOT reloaded in 2 blocks

## 0.31.2

- Wire blocking pre-commit checkers for README drift, mode dispatch, shared-file synchronization, and shell-variable persistence; fix false positives and restore the lessons-to-memory dispatch.

## 0.31.1

- Replace RTK command-prefix allowlisting with positive read-only checks for guarded commands; compound shell commands and unproven commands now pass to the normal permission matcher.
- Add GitHub API mutation deny entries as defense in depth.

## 0.31.0

- Remove distill's review mode and point roster-boundary analysis to the Foundry audit workflow.
- Add audit Check 34 for roster-boundary overlap and gaps, and prohibit subagents from mutating task state.

## 0.30.0

- Use the most common case as the default shape for test-data factories, prefer plain functions for pure factory data, and flag newly duplicated code blocks across files.
- Rename distill lessons mode to memory; process all projects by default, support an interactive project picker, and parallelize multi-project analysis and curator writes.

## 0.29.1

- foundry: solution-architect opusplan→opus (removes plan-mode overhead); audit batching capped at 10 batches

## 0.29.0

- `foundry:distill` prune mode: multi-project `--project` flag with interactive picker across all `~/.claude/projects/` slugs

## 0.28.0

- Centralize validated installed-root resolution, retain background agent tracking after dispatch, and introduce health-sentinel boilerplate plus an audit fix hierarchy.
- Extract brainstorm breakdown into a mode file and include required descriptions when the agent router rewrites rerouted tool input.

## 0.27.8

- Remove unused `when_to_use` frontmatter from Foundry skills and move trigger and skip behavior into their active descriptions.
- Add explicit blocked-stop directives to Foundry skills that disable model invocation.

## 0.27.7

- Replace manual path-containment checks in the Foundry helper with `Path.is_relative_to()` for explicit root membership.

## 0.27.6

- foundry curator + audit: curator.md step 7 in §Code Block Authoring adds bash compression as P3 flag — multi-line if/fi with single body → &&/|| one-liner, \_rc capture only when format string contains %d, ;-joined sequential single-assignment lines, WHY-only comment rule; audit/SKILL.md adds verbose-bash-block to PARALLEL_SAFE_CATEGORIES so compression fixes can run in parallel without cross-file reads

## 0.27.5

- Add source-grounding rules to Foundry agent cards: verify behavioral claims against source or tests instead of memory or inference.
- Register Codemap blast-radius calibration and identify it as the calibratable subtask of development workflows.
- Print report YAML metadata first and exempt it from decorative boxes; offer a challenger review after substantive manage changes, with explicit trivial-edit and mode exemptions.

## 0.27.4

- Update Foundry audit checks and agent guidance to use Codemap's expanded index and query surface.

## 0.27.3

- Register OSS analysis vitality as a target that Foundry calibration can evaluate.
- Keep session reasoning and file contents out of spawn prompts; omit human box headers and footers from machine-parsed replies.

## 0.27.2

- Remove redundant explanatory comments from Foundry shell and Python blocks while preserving executable behavior.

## 0.27.1

- foundry/curator.md: added when_to_use to valid skill frontmatter schema (resolves curator↔audit Check 31a contradiction)
- foundry/checks-skills.md: Check 31a changed from presence check to duplication check
- foundry/skill-scaffold.md: when_to_use: documented as optional field

## 0.27.0

- Add /foundry:profile: session clock-time analyzer; timing_analyzer.py (691 lines) + SKILL.md + 9 pytest tests; buckets local-tool/agent-spawn/skill/idle/main-loop-reasoning from ~/.claude/logs/\*.jsonl

## 0.26.0

- foundry: add C901 + PLR rules to linting-expert ruff config template; [tool.ruff.lint.mccabe] max-complexity=12 and [tool.ruff.lint.pylint] max-args=12/branches=12/statements=50/returns=6; PLR2004 in ignore (magic-value too noisy)
- foundry: add Complexity Thresholds section to python-code.md — table with CC≤12, required-args≤7 (style), total-args≤12 (PLR0913), branches≤12, stmts≤50, returns≤6; refactor-first noqa policy
- foundry: add Python Code Complexity section to quality-gates.md — plain-language enforcement rules, required-vs-kwargs arg distinction

## 0.25.3

- Skip `.temp/` linting before lock acquisition, place the recommended option second, and batch challenger/verification for grouped changes.

## 0.25.2

- Extract hook-registry removal into a guarded Foundry Python helper, and add source-evidence requirements to Foundry quality gates.

## 0.25.1

- foundry/skills/brainstorm/SKILL.md — AQQ ≤4 cap compliance: branch selection via free-text reply, tree ops as category-picker with follow-up AQQ per category
- plugins/CLAUDE.md — markdown annotation rule: > for prose comments in plugin .md files; # only inside code fences

## 0.25.0

- Define safe routing for multi-value script output through temporary files, and prohibit `eval` as a data transport mechanism.

## 0.24.3

- foundry statusline: fix 0% bar after /clear vs genuinely full context

## 0.24.2

- Add an audit check-numbering convention using letter suffixes beginning at `a`; add checks 29a/29b and update the check index and related references.

## 0.24.1

- Set Foundry commit guidance to use the `claude[bot]` GitHub noreply address for Claude co-author attribution.

## 0.24.0

- foundry-config.md: add abstraction-first stop rule — when change touches 3+ files with identical content, find shared abstraction before writing
- plugins/CLAUDE.md: add compression tier table + token-first length unit convention (tokens primary, lines secondary)

## 0.23.3

- Keep tables and fenced code outside blockquotes so terminal rendering and copy/paste preserve their structure.

## 0.23.2

- Require both an architectural and a visual-domain signal before applying the CV/ML documentation checklist, and make architecture discovery language aware.
- Persist brainstorm viewer state across Bash calls and prevent create outlines from overwriting existing files.

## 0.23.1

- Add architecture antipattern guidance for oversized or misused interfaces and expand QA checks for shell-injection risks in subprocess usage.

## 0.23.0

- Update Foundry rule references after the shared agent instructions were renumbered, keeping cross-references on the current section.

## 0.22.3

- Harden native-Windows path serialization and containment, reject POSIX absolute paths during run-directory validation, handle home-directory fallback, and prevent duplicate feature dispatch for the same brainstorm specification.

## 0.22.1

- Replace hard-coded `/tmp` paths in Foundry setup and initialization workflows with the platform temporary-directory fallback.

## 0.22.0

- foundry-config.md: curator vs sw-engineer dispatch rule for .claude/ config edits
- comment-dispatch.md: sentinel via compute_commit_sentinel.py; lint+QA gate simplified; fallback → foundry:sw-engineer

## 0.21.2

- Show top-level effort level and active skills in the status line, tracking both Skill calls and user slash commands and clearing completed activity.

## 0.21.1

- Show Claude and Codex agents in one statusline group, include the model effort when available, and simplify agent and tool count labels.

## 0.21.0

- Rename `/foundry:init` to `/foundry:setup` and update Foundry skill references.
- Collect non-auto-fixable audit decisions before changes begin, add high-churn and recency guidance to commit review, and introduce a JSON writer plus health-sentinel updates.

## 0.20.0

- Replace Foundry shell helpers with cross-platform Python executables and add Python helpers for Bash-persistence and spawn-prompt validation.
- Verify that `CLAUDE_PLUGIN_ROOT` resolves to the requested plugin before using a shared-path fallback.

## 0.19.2

- Automatically arm the commit sentinel after an explicit user commit request; retain workflow and confirmation authorization, the separate default-branch question, and an independent authorization signal for push.

## 0.19.1

- extract token-bloat blocks to foundry/skills/\_shared/: design-artifacts.md, pytest-config.md, pytorch-tracking.md

## 0.19.0

- Add a registry-aware helper that resolves the active Foundry install path, plus root-cause debugging guidance and audit checks for executable boundaries and security risks.

## 0.18.0

- Convert Foundry business-logic shell scripts to typed Python helpers, leaving only policy-permitted shell wrappers.

## 0.17.0

- Add `extract_code_blocks.py` and a bin-authoring guide for classifying fenced code and deciding when to extract scripts; use token estimates and explicit gate/score/verdict columns in audit Check 33.

## 0.15.2

- Refactor Foundry skills to delegate repeated mechanics to existing `bin/` helpers while keeping workflow decisions in Markdown.
- Lead execution failures, including partial failures, with a visible bold Markdown failure block.

## 0.15.1

- adversarial.md: Phase A-prime — unconstrained foundry:curator pass, parallel with challenger; catches structural/operational gaps (confirmed failure with no exit 1, undocumented implicit deps, non-happy-path silent failures); Phase C deduplicates against standard audit

## 0.15.0

- Make audit Check 17 and efficiency Check 33 group code blocks by functional purpose, using syntactic similarity as a secondary signal; keep efficiency tables out of the base audit path.

## 0.14.2

- Route Foundry creator's release-note work to the OSS release role and clarify cross-plugin feature references in Foundry management guidance.
- Remediate per-file audit confidence below 0.80 through reasoning, documentation, and available Codex cross-check passes; consolidate findings, recompute totals, and explicitly report unavailable Codex.

## 0.14.1

- Refresh stale skill links during setup and unlink managed destinations before relinking them.

## 0.14.0

- Add shared-path and audit-mode shell loaders to remove repeated skill preflights, and port the polluter finder to Python.
- Add audit Check 33 to identify similar code blocks and assess extraction opportunities by within-file purpose and syntax.

## 0.13.2

- Strengthen Foundry challenger findings with numbered, evidence-backed required changes; add calibration-only model-tier guidance and scaffold templates for skills and agents.

## 0.13.1

- Normalize YAML frontmatter quoting in Foundry distill and manage skills for consistent metadata parsing.
- Run calibration categories sequentially and pipeline spawns in batches of five, with task state reflecting actual execution.

## 0.13.0

- Define YAML report headers and terminal rendering in the shared quality-gates rule; simplify audit follow-up choices to fit the host limit.
- Add `Skill` to the permitted tools where audit and review workflows dispatch follow-up skills.

## 0.12.2

- Track Agent tool launches at PreToolUse and remove them at completion, so the statusline displays agents reliably even when SubagentStart is missed.

## 0.12.1

- Rank commit subject types by product significance.
- Ask whether to mark-and-continue, fix, or abort when retries establish a flaky test; do not continue silently.

## 0.12.0

- Use the least-certain subfinding when aggregating confidence, and narrow image and tensor documentation guidance to CV/ML projects.

## 0.11.2

- Fix 32 audit findings: XML tag escaping (creator, curator), ls-td→find (audit, brainstorm, calibrate, distill), investigate run dir → .temp/investigate/, health-monitor loop compressed, calibrate general-purpose→foundry:curator for .md edits, manage mode headers clarified, brainstorm/calibrate plugin dependency caveats, init Write-tool for settings bootstrap, doc-scribe comment relocation, challenger/solution-architect XML cleanup, qa-specialist emoji system made display-only aliases, manage jq note corrected

## 0.11.1

- Standardize commit subjects as `type(scope): detail` and limit deprecation checks to public APIs.

## 0.11.0

- Separate final reports from intermediate `.temp/` handovers and require final reports to include Source Files provenance.

## 0.10.5

- Add explicit trigger and skip boundaries to Foundry specialist agents and relevant skills.
- Define the breaking-change gate in the versioning rules and require explicit user acknowledgment before audit continues past each breaking finding.

## 0.10.4

- foundry:manage — plugin-prefixed invocations (/foundry:manage); jq replaces python3 for settings check; DIRECTIVE variable made explicit in content-edit mode
- foundry:investigate — symptom/signals/hypothesis captured in bash vars before Codex spawn; inline Confidence block example; shell var refs replace literal placeholders in prompts
- foundry:calibrate — graceful-exit protocol (safety-net result.jsonl on any crash/exit); CODEX_MODES variable fix; phase timeout hint injected into subagent prompts; Skill added to allowed-tools

## 0.10.3

- Shorten Foundry agent, skill, rule, and shared Markdown prose.

## 0.10.2

- Resolve the default branch dynamically and require its additional five-minute authorization sentinel alongside the ordinary fifteen-minute commit sentinel.

## 0.10.1

- Add Foundry audit checks for orphaned empty structural blocks and align curator formatting and completeness guidance.
- Give a delegated audit fixer ownership of its follow-up gate and skip the parent gate on that route to prevent double-gating.

## 0.10.0

- Ask for clarification when Foundry skills receive unsupported flags instead of silently ignoring them.

## 0.9.6

- Add audit checks for identical Bash blocks repeated across three or more files and for inline Python permission problems.

## 0.9.5

- Require challenger and curator approval of each audit fix before spawning its implementer; retain blocked findings and reasons in the fix summary.
- Add Check 31 to find tool calls in skill bodies that are missing from `allowed-tools` frontmatter.

## 0.9.4

- Reset statusline agent badges immediately on `/clear`; remove per-turn lock files and clean stale session directories without deleting live concurrent sessions.

## 0.9.3

- Clear per-branch commit authorization sentinels at session start and when `/clear` is submitted, so approval cannot leak across sessions.

## 0.9.2

- Correct agent frontmatter names to unprefixed local names, quote skill argument hints for valid YAML, and register OSS analysis and release calibration targets.

## 0.9.1

- Add explicit fallbacks for optional OSS and Research roles, allowing Foundry workflows to skip cleanly when a sibling plugin is unavailable.

## 0.9.0

- Require QA agents to inventory public APIs and engineers to plan before implementation.

## 0.8.2

- Require Foundry quality-gate workflows to invoke a selected follow-up skill in the same turn rather than merely narrating the intended action.

## 0.8.1

- Add a resilience rule that prevents a plugin's recovery mechanism from depending on the plugin it is meant to recover, and raise challenger and creator effort to xhigh.

## 0.8.0

- Move Foundry executable helpers into the plugin's `bin/` directory and consolidate audit/calibration argument syntax.
- Make memory pruning read-only by default and require an explicit memory-write decision.

## 0.7.1

- Add a Codex cross-check to challenger with a `--no-codex` opt-out, convergence reporting, and surfaced failures; register Research verification cases for calibration.

## 0.7.0

- audit: `adversarial` mode (alias: `challenge`) — foundry:challenger Phase A + Codex Phase B + curator consolidation

## 0.6.1

- Classify manage content edits as trivial or substantive before dispatch, and rename the CI guardian role to CI/CD steward throughout routing references.

## 0.6.0

- Rename `foundry:self-mentor` to `foundry:curator`, and add a creator agent plus `/create` workflow for scaffolding agent definitions.
- Add a short pre-seeding exchange to brainstorm before it creates formal branches.

## 0.5.0

- Add a read-only, red-team challenger that tests plans and code across assumptions, missing cases, security, architecture, and complexity, then must attempt to refute each finding.

## 0.4.0

- Add an external-resource mode to `/distill` that analyzes a plugin or skill from a URL or local path, compares it with the current setup, scores adoption candidates, and presents adopt, adapt, discuss, or skip choices.
- Check for lesson conflicts before writing and show the resulting diff for review; start a fast calibration benchmark when the target has no prior run.

## 0.3.0

- Deny sudo, SSH, world-writable permissions, and recursive force deletion in Foundry policy; add plugin audit checks for Bash correctness and dependency cycles.

## 0.2.1

- Add `foundry:linting-expert` to agent-resolution fallback tables and provide an unconstrained deep-plugin audit prompt.

## 0.2.0

- Add a live browser tree viewer with JSON sidecar updates to brainstorm; require an approved design before implementation and distinguish rejected from resolved branches.
- Add a screenshot of the brainstorm tree to the Foundry README.

## 0.1.4

- Add a PreToolUse commit guard with a short-lived authorization sentinel and wire its lifecycle into `/resolve`; clarify report confidence templates and shared-file failure handling.

## 0.1.3

- Make `/foundry:audit` converge through up to five fix and revalidation passes, report remaining findings when the cap is reached, and write per-pass cross-file validation results.

## 0.1.2

- Clarify Foundry audit orchestration, batching, and delegation thresholds, and repair namespaced references among its agents and skills.
- Make sibling plugins able to operate without Foundry by providing role-description fallbacks when optional agents are unavailable.

## 0.1.1

- Add GitHub read-only guidance and a deny policy; merge denials during setup and support `init --approve` for noninteractive recommended choices.
- Remove the misplaced standalone `stats-reader.js` script from the hooks directory.

## 0.1.0

- Establish `plugins/foundry/` as the canonical distribution for agents, skills, hooks, and rules, with `/foundry:init` merging settings and linking or copying configuration into the host environment.
- Move the team protocol, permissions guide, configuration, and shared rules into the plugin; replace former `.claude/` files with links and remove duplicate permission and status-line configuration.
- Namespace Foundry skill and agent dispatch references and repair broken setup links so the split plugin can resolve its own workflows and cross-plugin roles.
