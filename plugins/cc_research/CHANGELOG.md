# Changelog

## 0.28.2

- Sync the quality-gates rule: after each validated adversarial-loop round, print the progress table exactly, with all rows kept and each cell as `old + new`.

## 0.28.0

- Write Research skill contracts atomically with validated optional gate metadata and a session/digest owner stamp, allowing the continuation path to verify which session produced the contract.
- At main-thread phase boundaries, consult Foundry’s optional compaction gate and continue safely when it is unavailable. A compact decision emits a `/compact` keep payload and waits for continuation; restore saved report and task inputs without repeating the phase, require confirmation for unverified contracts, and skip foreign-session stamps.
- Require topic follow-up questions to name the report path, title and outcome within the 1,500-character question budget. Save longer workflow context to a file and summarize it in question text; retain the full report header in the final response after the answer.

## 0.27.2

- Remove broad GitHub API, branch and tag commands from Research’s shipped allow list. Remove shipped GitHub-write and local branch/tag deletion denies while retaining destructive-operation protections.
- Keep existing user allow and deny entries during Research setup; stale-deny cleanup remains with Foundry setup, so already-installed entries persist until that migration is performed.

## 0.27.1

- Remove the obsolete `resolve-quality-gates.sh` helper and read the scientist’s link-integrity requirements from the loaded quality-gates rule.

## 0.27.0

- Accept the report header in the first follow-up question’s text or eligible option previews; when the delivery budget is exceeded, require a compact summary and saved-report path instead of silently dropping the context.
- Add a SessionStart stale-plugin warning naming loaded and installed versions and restart/resume steps. Ask once per session and loaded version whether to restart or knowingly continue; compaction can repeat the warning without re-asking.

## 0.26.13

- Move shared output-routing policy to Foundry while retaining Research’s follow-up examples in its quality-gate delta; remove the obsolete Git-slug helper and run commit-sentinel steps.
- Resolve the router’s API model through an override, a 24-hour cache, the Models API or a logged fallback. Disable thinking for bounded routing replies and report API or timeout failures before falling back to a general-purpose agent.

## 0.26.12

- Require quality-gate completion claims to cite concrete evidence and stay within the scope of checks actually performed.
- Require a representative real case alongside TDD and recheck it before completion; mocks supplement real validation and unavailable real acceptance remains explicit.

## 0.26.11

- Re-read missing visible report-header delivery for up to two seconds to allow transcript output to flush, and include the observed visible-character count in diagnostics. Reasoning and tool output do not count as user-visible delivery.

## 0.26.10

- Check report delivery again at Stop, track delivery by report version and read the Stop payload’s last assistant message when available.
- Require two-decimal confidence deductions that sum to the reported shortfall, including tier and fetch penalties; separate retrospective finding confidence from statistical-test availability.

## 0.26.9

- Add native Windows Python fallback for Research helper recipes, preserving arguments, exit status and one-time workload execution after runtime validation.

## 0.26.8

- Add a Python fallback for unchanged skill and agent recipes; try known Node locations for hooks and report inactive hooks at SessionStart when Node is unavailable.

## 0.26.7

- Remove the fixed three-review cap from Research’s adversarial loop: continue while the unchanged-scope score strictly decreases, stop on a plateau or caller budget, and resolve feasible authorized findings before requesting a new decision.

## 0.26.6

- Convert research run, team, and fortify checkpoints to append-only JSONL ledgers so later updates do not rewrite earlier records.
- Keep isolation-motivated agent spawns even when their work is small.

## 0.26.5

- Correct the GitHub repository API permission pattern in Research setup and document the exact migration from the obsolete rule.

## 0.26.4

- Select targeted tests during fix loops, then run the full suite once at the final gate; wait for agents through notifications and report timed-out agents.

## 0.26.3

- Restructure long Research prose into readable lists and paragraphs.

## 0.26.2

- Add stale-root detection from installation-registry paths and orphan markers; callers can distinguish a loaded stale root from the active installation.
- Use concise descriptive fields in Research report templates and keep authoring instructions out of completed reports.

## 0.26.1

- Add a block-and-flow architecture map for the topic workflow, including fan/join stages, gates, mode branches, and degenerate cases.

## 0.26.0

- Install Research’s quality-gate delta only when Foundry rule ownership is proven; otherwise deliver the complete Research rule so standalone installations retain every obligation.
- Discover deferred task tools before calling them, combine bookkeeping with substantive work, and batch related cleanup, contract and context-loading calls.

## 0.25.5

- Adopt the shared notebook-authoring contract in Kaggle workflows while keeping Kaggle-specific guidance as a small extension; update the Jupytext blank-cell helper to use the named Markdown rule.

## 0.25.4

- Split long Research Python helpers into smaller functions and add `main()` docstrings; regenerate the blueprint manifest.

## 0.25.3

- Inventory review coverage and finding origins before repair. Require each fix to name its invariant, original counterexample, downstream consumer and sibling route, with failing-before and passing-after evidence.

## 0.25.2

- Dispatch data-steward modes from caller prompt intent, prioritizing review intent over acquisition keywords; label the judge's merged scientist/architect review accurately.
- Record lower-priority data-integrity and hygiene observations instead of omitting them when more severe findings exist, cap confidence for acknowledged missed findings, and execute compound fortify metric commands through the shell.

## 0.25.1

- Record each finding’s fix, escalation or justified deferral, and require later independent verification before closing a repair.
- Keep every finding when a reviewer exceeds the advisory 1,500-token output target; repeat the convergence table only when a new round has completed.

## 0.25.0

- Show completed convergence rows in status, approval/recovery and pause messages; use not-run/N/A when no round is validated and retain the final Results table.

## 0.24.4

- Show cumulative old/new finding counts and weighted scores after each completed challenge, before the next fix, review or stop. Keep pending reviews distinct from completed clean rounds.

## 0.24.3

- Reject failed Codemap query output and distinguish static test/mock relationships from measured coverage while keeping formula and source verification explicit.

## 0.24.2

- Block topic follow-up until the saved report and matching visible header exist; retain diagnostic questions and add frontmatter to topic plan reports.

## 0.24.1

- Ship the common skill compaction-contract format and boundary lifecycle, recording the active skill, source paths, progress and next steps for continuation after compaction.

## 0.24.0

- Resolve the Codemap gate contract from the active `codemap-py` installation, remove Research’s frozen gate copy and preserve the fallback when the provider is absent. Follow the loaded provider contract’s documented stale-index policy.

## 0.23.5

- Condense Research agent and skill guidance covering test contracts, commands, flags, paths and workflow order.

## 0.23.4

- Extend adversarial review through unchanged downstream consumers and the next ordinary user action; record handoffs that were not verified.
- Use a dedicated `%%bash` cell for consecutive notebook shell commands and `# !` for a single inline command.

## 0.23.3

- Add the shared adversarial review loop to Research quality gates, with evidence-backed convergence scoring and an explicit stop when findings remain open.

## 0.23.2

- Restore quality-gate requirements lost from the Research copies and verify their parity with the canonical rule.

## 0.23.1

- Parse skill flags with Python helpers instead of shell regexes, fixing captures that were empty under zsh. Match `--team` as a complete token and keep flag-shaped words inside `--keep` values from becoming workflow options.

## 0.23.0

- Add the quality-gate convergence loop: track scores across review/fix rounds and ask for a decision when the loop stops with findings open.

## 0.22.2

- Replace zsh-incompatible `BASH_REMATCH` parsing in Kaggle, fortify and sweep with argument parsers.

## 0.22.1

- Ship Research’s propagated Codemap gate contract and load it from the active Research root, retaining the fallback when the optional provider is absent.

## 0.22.0

- Add a Bash auto-allow audit trail so permitted blueprint commands are recorded and verifiable.

## 0.21.1

- Document Research helper behavior and verify their executable examples against the implementation.

## 0.21.0

- Add permission-deny rules to Research and merge its own allow/deny policy during setup with a backup and validation step.
- Add CPU, memory, and process quotas to research sandbox containers, with positive-value overrides and safe defaults.

## 0.20.1

- Validate `program.md` section headers before judging a research proposal, so malformed input stops at the schema gate.
- Trigger additional review for a specific actionable gap or unproven claim instead of routine score thresholds; preserve all discoveries before filtering the presented summary.

## 0.20.0

- Reduce research workflow spawns and repeated work: combine related plan and judge advisors, fetch paper content once, avoid redundant timing runs, and report actual launch batches.
- Persist Codex usage counters in experiment state; make topic SOTA search single-owner and let hypothesis oracles annotate feasibility.
- Reduce fortify’s per-variant shell calls while ensuring skip paths still advance the variant cursor.

## 0.19.0

- Replace the legacy Codex integration with the self-contained Bridge `implement`, `advise`, and `review` calls, including explicit unavailable and disabled behavior.
- Format plugin Markdown through the repository formatter and condense Research ML reference guidance.

## 0.18.1

- Close sentinel-read auto-allow bypasses so command forms outside the read-only contract continue through normal permission checks.

## 0.18.0

- Guard writes to protected files with a shared hook, propagated to Research and registered in its hook configuration.

## 0.17.0

- Add stale Git lock and worktree reporting; require explicit confirmation before removing worktree directories.
- Have fortify sweep its variant root after Git prunes worktrees, including worktrees whose accumulator entry was never saved.

## 0.16.0

- Auto-allow only validated blueprint-manifest commands, with the hook registered after the sentinel-read gate.

## 0.15.4

- Add a tested Jupytext helper that removes heading-spacer lines inside Markdown cells only; Kaggle generation now applies the cell-scoped repair instead of a file-wide regex.

## 0.15.3

- Move test capability skips to collection time and update Research docs to match the shipped agent and skill roster.

## 0.15.2

- Anchor Codemap paths at the Git root so subdirectory invocations select the same index. Preserve UTF-8 paths and raw basenames through Research’s resolver and flag handling.
- Read stale-index currency from the returned JSON even when the provider exits nonzero, so the gate does not lose its stale verdict.
- Normalize Windows extended-length symlink targets so setup recognizes and updates its own rule links.

## 0.15.1

- Ground Kaggle notebooks in the authenticated Kaggle CLI’s competition files, leaderboard, and sample submission instead of guessing the submission schema.
- Keep Kaggle credentials out of prompts, notebooks, and artifacts; gate full archive downloads behind a size-aware user choice.

## 0.14.3

- Remove self-evident shell comments from Research workflows while retaining comments that explain constraints or edge cases.

## 0.14.2

- Resolve the shared Research helper once per skill and reuse a session sentinel in fortify, plan, retro, and verify to cut repeated subprocess calls.

## 0.14.1

- Route Research Python scripts through the interpreter so skill commands work on native Windows, where extensionless shell-style calls are not portable.
- Record an agent-spawn budget and avoid parallel specialist calls when inline work is cheaper.

## 0.14.0

- Replace repeated inline logic with Python helpers and cache resolved shared paths in session-scoped sentinels for workflow reuse.
- Add the Research-specific scientist Codemap preamble and a shared transcript helper that nudges when the visible report-header table is missing.

## 0.13.1

- Move agent sidecar references out of the scanned `agents/` tree and update loaders to use the new `references/` location, preventing sidecars from being registered as agents.
- Fix zero-match `grep -c` handling where the fallback could append a second zero and corrupt numeric output.

## 0.13.0

- Add `/research:setup` to deliver only Research’s own rules into Claude’s user-level rule namespace without modifying settings.
- Add a tested `sync_rules.py` helper that validates source rules before creating, refreshing, pruning, or preserving destination links.

## 0.12.8

- Validate retrospective metric direction with a closed enum, and render topic-team report metadata as a two-column table.

## 0.12.7

- Make Research workflows read shared contracts and helpers from their own plugin installation, so the plugin remains usable without Foundry.

## 0.12.6

- Rehydrate shell variables and sentinel state between separate tool calls, and fix path handling that lost values or a trailing newline.

## 0.12.5

- Require research topic reports to print their report path before follow-up questions and carry that path through single-agent, team, and plan flows.

## 0.12.4

- Replace ordinal `#N` references that looked like GitHub issue numbers with plain step/rank labels in Research instructions.

## 0.12.3

- Enforce printing the research report header before the topic workflow asks a follow-up question.

## 0.12.2

- Label Codemap references as `codemap-py` throughout Research skills so headings and handoff parsing identify the correct provider.

## 0.12.1

- Migrate Research’s Codemap commands, resolver and gate calls to the `codemap-py` CLI and namespace.
- Correct file-permission allow-rule forms so the shipped rules use syntax the host accepts.

## 0.12.0

- Add a hardened sentinel-read allow hook across Research skills so only read-only sentinel substitutions bypass permission prompts; writes, traversal, and environment-changing forms remain denied.
- Replace command-substitution sentinel reads with `IFS= read -r` to avoid triggering broad expansion permissions.

## 0.11.0

- Scope temporary sentinels to the Claude session ID so concurrent sessions cannot overwrite each other’s state.
- Move skill contracts from `.claude/state` into the project’s `.temp/state` directory and audit temporary sentinels for missing session suffixes.

## 0.10.9

- Move the plugin directory to `plugins/cc_research` while retaining the `research:` command namespace.

## 0.10.8

- Require Kaggle notebooks to both optimize the competition metric and teach the workflow top-to-bottom; add an executable check that repairs bare Markdown heading spacers.

## 0.10.7

- Run Codemap index builds through the foreground CLI instead of invoking the slash-only scan skill, which could not be dispatched through the Skill tool.

## 0.10.6

- Load shared documents through shell reads rather than the host Read tool where the latter could not resolve the plugin’s shared-file path.

## 0.10.5

- Compose the existing Kaggle workflow from selected foundation, EDA, training, inference and submission contracts; validate the sections required by full, EDA-only and inference-only modes.
- Require notebook steps to fail promptly on errors and reject mutually exclusive EDA-only and inference-only selections before generation.

## 0.10.4

- Harden Research helpers and workflows: constrain Docker verification commands, make run discovery resilient to concurrent deletion, support detached Git states, and align quality-gate confidence thresholds.

## 0.10.3

- Add consistent `--help` and argument parsing to Research command-line helpers through the shared argparse migration.

## 0.10.2

- Change the declared plugin license from MIT to Apache-2.0 and synchronize the README’s displayed version.

## 0.10.1

- Move judge J3 prompt templates into a shared file, gate the Codex adversarial pass by program complexity, and mark the data-steward agent manual-only.

## 0.10.0

- Add per-skill compaction contracts to Research’s phased workflows so progress, decisions, and next steps survive automatic context compaction.
- Save contract state at workflow boundaries and clear it when the task completes; the first boundary remains the earliest point at which skill-specific context can be preserved.

## 0.9.5

- Wire the Docker sandbox runner into research metric-computation modes; it had been tested but was not used by the workflow.
- Replace unreachable synchronous-agent polling with post-return artifact checks; missing or empty expected artifacts produce timeout markers and partial-result handling.

## 0.9.4

- Let research runs honor `program.md` iteration limits up to 50 and source fortify metric/guard commands from experiment state.
- Normalize program paths and fail closed when Git metric state cannot be verified.

## 0.9.3

- Recompute variables lost between shell blocks across fortify, Kaggle, judge, and retro, restoring correct run paths, selected modes, output filenames, and specialist dispatch.

## 0.9.2

- Re-derive topic arguments before unknown-flag and `plan` mode routing, and rebuild the commit-sentinel path before cleanup.
- Cache local-role descriptions at SessionStart; reroute unavailable roles through semantic matching, using embeddings when configured and API selection otherwise before general-purpose fallback.

## 0.9.1

- Detect Foundry in marketplace-independent cache paths and the source tree instead of searching for plugin names among marketplace directories.

## 0.9.0

- Share Kaggle style rules across notebook modes, keep configuration next to the cells that consume it, and support frozen packages for offline kernels.
- Require competitor-notebook context scans and make source-package extraction an explicit post-validation offer.
- Skip the architect review spawn for narrow judge scopes while marking the methodology rating directly.

## 0.8.0

- Add Research Codemap integration with automatic, strict, and disabled modes, plus shared context and gate contracts for run and paper verification.
- Make Codemap index availability determine whether automatic mode injects blast-radius context.

## 0.7.4

- Extract repeated Git slug generation into a Research helper and fix agent-router handoffs to preserve agent descriptions.

## 0.7.3

- Fix Research agent XML-style instruction delimiters so scientist and data-steward sections are interpreted as intended.

## 0.7.2

- Remove comments that merely restated code across Research agent and skill files.

## 0.7.1

- Remove invalid agent frontmatter from reference sidecars and replace a personal Kaggle example with user-neutral wording.

## 0.7.0

- Add `/research:kaggle` to generate competition notebooks as Jupytext scripts, with full, EDA-only, and inference-only modes and modality-aware data exploration.
- Add an offline-setup path for inference notebooks and shared unsupported-flag handling across research workflows.
- Route broad topic searches to web search and reserve the scientist agent for deep analysis of one paper.

## 0.6.9

- Reuse Research’s run-directory and health-monitor helpers, and fail explicitly when the shared plugin path cannot be resolved.

## 0.6.8

- Move Colab setup and GPU-backed metric verification into a dedicated run mode.
- Move the plan overwrite guard ahead of advisor results and combine related decisions into one question.
- Add agent resolution and calibration guidance to fortify.

## 0.6.7

- Repair Research skill parsing and references during the shared executable-output routing update.

## 0.6.6

- Add fail-fast checks, JSONL sanitization, and stronger Codex detection across Research workflows.
- Document reproducibility and concurrent-storage risks in the research agent patterns.

## 0.6.5

- Persist fortify, judge and topic paths across separate shell calls; distinguish verification sentinels by branch and date so concurrent work does not reuse the wrong state.
- Extract run metric-computation and resume modes into loadable files, and tighten the scientist’s plugin-prefixed agent references.

## 0.6.4

- Update Research references to the current Foundry setup section after the setup workflow was consolidated.

## 0.6.3

- Validate sweep output containment with a shared helper, and correct Docker absolute-path checks and mount spelling for Windows paths.

## 0.6.2

- Fix the judge’s specialist prompt to use resolvable program and run paths.

## 0.6.1

- Add `find_run_id.py` and `read_state_field.py` to replace inline JSON extraction, preventing run-ID injection and correcting fortify state lookup.

## 0.6.0

- Replace Research shell helpers with cross-platform Python equivalents and extract patient-split verification from inline skill code.

## 0.5.2

- Extract effect-size computation into a tested Python helper and move topic planning and team flows into mode files.

## 0.5.1

- Promote judge findings about optimizing a proxy at the expense of the real objective to blocking severity; skipping validation cannot produce an APPROVED outcome.
- Validate sweep output paths and load explicitly supplied hypothesis files. Harden run-directory, health-monitor and Docker helpers so invalid inputs fail before work starts.

## 0.5.0

- Move Research shell business logic into typed, tested Python helpers; keep skill instructions focused on workflow decisions.
- Add a scientist ML-concepts reference specialization for machine-learning research tasks.

## 0.4.3

- Add helpers for run-directory creation, shared-path resolution, and health-monitor startup; restore the documented flat run-directory shape.

## 0.4.2

- Make Research run, retro, and fortify report paths anti-overwrite by selecting the next unused suffix; remove the obsolete retro health-monitor sentinel.

## 0.4.1

- Strengthen fortify’s metric sign and coupled-component checks, judge’s proxy-metric and no-op detection, and retro’s threshold and timeout analysis.
- Use a consistent `python` command for Research Python helpers.

## 0.4.0

- Store research reports under `.reports/research/` and add YAML metadata to report templates.
- Normalize report frontmatter and shared output conventions across Research workflows.

## 0.3.8

- Fix research audit findings: remove an agent-name collision, repair fallback paths and allowed tools, and mark optional cross-plugin references explicitly.
- Condense Research instruction files.

## 0.3.7

- Move plan's unsupported-flag check before input processing and use an interactive goal choice after file profiling; resolve sweep shared paths before its first step.

## 0.3.6

- Fix shared-path resolution, duplicated judge execution, and stale shell-variable references across plan, judge, sweep, run, and verify.
- Remove the redundant polling step from the synchronous verify agent call.
- Ask before silently ignoring unsupported skill flags.

## 0.3.5

- Add paper-verification cutoff constants and refinement-pass reporting, plus run health-monitor checkpoints.

## 0.3.4

- Add agent routing through an exact installed role, optional API-assisted local-role selection, and a general-purpose fallback when the preferred agent is unavailable.
- Require a follow-up skill selected through a quality-gate question to execute in the same turn.

## 0.3.3

- Remove the judge committee mode and create all review checkpoints before dispatching agents.
- Make fortify handle zero-valued baseline metrics and read the judge verdict from its artifact instead of inferring success from a rating.
- Use file-based review handoffs for plan specialists.

## 0.3.2

- Decline nonresearch scientist inputs without returning partial findings, and forward the sweep’s `--architect` option into the run workflow.
- Remove redundant Notes headings while retaining workflow notes.

## 0.3.1

- Add the topic workflow’s standalone agent-resolution fallback and explicit follow-up question; make the planning workflow’s `--team` path a named task-tracked phase.

## 0.3.0

- Share agent-resolution fallback guidance across Research skills instead of duplicating it.
- Add command timeouts and repair paths, directory cleanup, and metric-check behavior across run, plan, judge, fortify, retro, and topic workflows.

## 0.2.0

- Add `research:fortify` for ablation studies, `research:retro` for post-run statistical review, and `research:verify` for paper-to-code fidelity checks.
- Split data-steward and run guidance into mode-loaded sidecars, reducing always-loaded instruction size while retaining specialized details.
- Add the research MkDocs site and GitHub Pages workflow.

## 0.1.0

- Create the standalone Research plugin with scientist and data-steward agents and the `topic`, `plan`, `judge`, `run`, and `sweep` skills.
- Add skill metadata, team/advisory modes, and resume handling; migrate research run constants and shared quality guidance into the new plugin structure.
- Make research commands use their namespaced plugin invocations and document standalone installation with graceful general-purpose agent fallbacks.
- Move the run protocol into the plugin and standardize research report paths and plugin-prefixed agent references.
