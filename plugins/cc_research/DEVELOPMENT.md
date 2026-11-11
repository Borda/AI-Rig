# research — Development Notes

Technical detail for contributors, maintainers and agents. User-facing documentation lives in [README.md](README.md).

## Contents

- [Sources and maintenance](#sources-and-maintenance)
- [Review-loop and quality-gate behavior](#review-loop-and-quality-gate-behavior)
- [Codemap integration](#codemap-integration)
- [Report and journal templates](#report-and-journal-templates)
- [Planning and execution policy](#planning-and-execution-policy)
- [Agent operating boundaries](#agent-operating-boundaries)
- [Hooks in detail](#hooks-in-detail)
- [Auto-allow audit log](#auto-allow-audit-log)
- [Quality-gates rule delivery](#quality-gates-rule-delivery)
- [Permissions files](#permissions-files)
- [Generated files](#generated-files)
- [bin tools](#bin-tools)
- [Kaggle notebook style](#kaggle-notebook-style)
- [Tests](#tests)
- [Future work](#future-work)

## Sources and maintenance

- Canonical sources: the ten `skills/*/SKILL.md` files, the two `agents/*.md` files, `rules/*.md`, registered hooks, sidecar references, and `bin/*`.
- Keep the README synchronized when a public skill, flag, trigger, prerequisite, output path, hook, or boundary changes.
- The plugin version is recorded in `.claude-plugin/plugin.json`; use that manifest as the source of truth when preparing a release.
- When editing a skill, update its README entry, flags, NOT-for boundaries, output paths, fallback behavior, and relevant troubleshooting guidance.
- Verify that references loaded from `skills/_shared/`, `skills/*/modes/`, agent sidecars, or `bin/` remain installed-path safe and do not assume a source checkout.
- Run focused tests while editing and the full `plugins/cc_research` suite before release.

## Review-loop and quality-gate behavior

- Review gaps remain factual limitations. Any additional review beyond authorized scope requires one explicit, bounded user decision; declined requests do not return as findings or final action items.
- Review loops fix feasible authorized findings, investigate repeated signatures, and continue whenever the weighted score decreases; structural labels alone do not stop remediation.
- Completion prompts ask only for a missing decision or authorization, regardless of report length, and already-authorized next steps continue. Concrete gates for fresh scope, paid execution, sensitive or destructive actions, and remote changes remain in force.
- Independent challenge loops show the cumulative old/new table once after each newly validated challenge, before fixes or another review. Pending reviews and unrelated status updates do not repeat it; final handoffs retain their canonical Results table.
- Each open finding needs a recorded fix, escalation, or justified deferral; chat table timing has no machine-readable delivery receipt.
- Adversarial workflow reviews include unchanged downstream consumers and the next ordinary user action, checking whether local success establishes the promised outcome and recording untested handoffs.

Round procedure:

1. Each round challenges and collects findings.
2. It reports the cumulative old/new table.
3. It resolves feasible authorized findings.
4. It escalates unfixable `security`/`critical`/`high` findings.
5. It repeats until clean or plateau/non-convergence, subject to authority, evidence, recurrence and stricter caller budgets.

Table and scoring:

- The table is `Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score`.
- Open and fixed-pending-verification signatures split into old and new; verified-fixed and rejected findings stay excluded.
- Security and critical have separate columns; score weights remain `20/10/6/4/2/1`.
- Empty rounds print no progress table.
- A 1,500-output-token reviewer target is hard only when the runtime supports it; findings are never dropped to meet it.
- Before the baseline review of a diff the parent or its resolver wrote, the author records an attestation (invariant, sibling routes, consumer, failing-before/passing-after check) and fixes the gaps it exposes. The reviewer never sees it and it never counts toward scoring.
- Each round's cost lands in an advisory `loop-metrics.jsonl`.

## Codemap integration

- Optional Codemap index-gate guidance ships with research, so loading it does not depend on another plugin's private shared directory. The host-provided active installation takes precedence over other cached versions.
- Structural queries still require the `codemap-py` plugin; an unavailable CLI or local contract retains the file-read fallback.
- Enabled missing/stale-index preparation follows the active provider contract automatically by default; explicit `LAZY_CODEMAP` confirm-first and `SCAN_NO_AUTOBUILD=1` frozen preferences remain controlling.
- Later query blocks reload saved gate choices and preserve the stale-data freeze in their subprocesses.
- Codemap context distinguishes static test links from measured line coverage: missing measurements are unknown, not zero, and package queries select exact modules.
- Failed queries report unavailable context rather than successful empty results.
- Run, verify and scientist prompts reuse only matching fresh answers while preserving source, formula and test-quality verification.

## Report and journal templates

- Research-topic and run reports and experiment journals use short descriptive fields, with writing rules kept outside the output shape.
- Completed artifacts replace those fields with observed values and omit template instructions.

## Planning and execution policy

Work-size policy for agent-driven implementation:

- Small implementation uses a concise plan, brief parent self-challenge (not independent), acceptance within existing authority, useful bounded execution ownership or indivisible parent execution, and consolidation with independent result challenge and relevant verification.
- Medium/large work retains independent plan challenge before acceptance.
- Approval prompts only for actual missing authority or protected decisions; reuse accepted parent plans and establish core acceptance before optional expansion.
- Validate a coherent bounded outcome or split it into smaller outcomes, then complete the active branch depth-first using a compact task map and context limited to the active piece, dependencies, and interfaces.
- Complete required leaf challenge and verification before dependent work; dependency-ready siblings with agreed stable interfaces and disjoint owners may proceed.
- Unknown causes or design choices first become bounded investigation or decision prerequisites; parent consolidation retains required gates.
- This replaces broadly open workstreams and far-ahead detail to reduce context, coordination, and rework; that benefit is expected, not measured.

Evidence and real-case acceptance: shipped `rules/quality-gates.md` requires cited proof for factual, causal and completion claims, a representative real case alongside TDD, and unchanged user-authorized behavior. Mocks are supplementary polishing after real behavior is validated; unavailable real acceptance stays explicit.

## Agent operating boundaries

### `research:scientist`

- For a named paper, publication-backed method, falsifiable hypothesis, or experiment design.
- Separates paper claims from evidence, checks baselines and variance, identifies one central idea, and audits attribution and contribution claims.
- Plans one-variable-at-a-time experiments, estimates compute, and interprets results as confirmed, refuted, or partially supported.
- Reports mean ± standard deviation over at least three seeds when stochastic results are compared.
- Flags cherry-picked results, missing confidence intervals, test-set reuse, and leakage concerns.
- Handoffs state the paper or method, core idea, actual contribution, mechanics, evidence, limitations, relevance, falsifiable prediction, variables, controls, success criterion, ablations, compute estimate, and expected outcome. A paper summary is not a benchmark claim unless the source and protocol are retained.

### `research:data-steward`

- For acquisition, provenance, DVC or version tracking, split integrity, leakage detection, schema validation, and DataLoader configuration.
- Checklist:
  - pagination count/schema/boundary/dedup verification;
  - mutually exclusive or group-aware splits;
  - train-only fitting for stateful transforms;
  - train-only augmentation and oversampling;
  - temporal window direction;
  - NaN/Inf, shape, dtype, and range checks;
  - `shuffle=False` for validation/test loaders.
- Delegates unknown URL discovery or scraping to `foundry:web-explorer` and validates the returned data itself.
- Engineering hygiene is reported at Info severity; silent record loss or duplication is High even when snapshot consistency is involved.
- An unrelated request receives one or two scope/redirect lines plus the required Confidence block.
- Useful search patterns (investigation prompts, not automatic findings; confirm the surrounding data flow):
  - `fit_transform(` for pre-split normalization;
  - `Random*` transforms for validation contamination;
  - `train_test_split(` without group awareness;
  - `patient_id` or `subject_id` split gaps;
  - `random_split(` shared-transform risks;
  - augmentation calls before the split.

## Hooks in detail

Hooks register from `hooks/hooks.json`.

| Hook | Event | Behavior |
| -- | -- | -- |
| `agent-router.js` | `PreToolUse(Agent)` | Exact agent route, semantic fallback, then `general-purpose`. |
| `sentinel-read-allow.js` | decision module, not registered | Allows only pre-canned read-only temporary-state idioms. Whole-line `# …` comments inside a block are skipped, and `..` is matched as a path component. |
| `blueprint-allow.js` | decision module, not registered | Exact-matches the normalized command against the committed `blueprint-manifest.json`; any deviation falls through to a normal prompt. |
| `allow-dispatch.js` | `PreToolUse(Bash)` | The only registered Bash auto-allow hook. Calls the two decision modules in rank order, emits the first allow, and appends one audit record. Exits 0 on every path. |
| `audit-close.js` | `PostToolUse(Bash)`, `PostToolUseFailure(Bash)`, `SessionStart`, `SessionEnd` | Appends one record observing that a Bash call completed, and one per session boundary. Writes nothing to stdout. |
| `write-guard.js` | `PreToolUse(Edit, Write, NotebookEdit)` | Grants nothing; see write-guard rules below. |
| `stale-plugin-check.js` | `SessionStart` | See stale-plugin check below. |
| `enforce-topic-header.js` | `PreToolUse(AskUserQuestion)`, `PostToolUse(AskUserQuestion)`, `Stop` | See topic-header gate below. |

Runtime lookup:

- Each hook runs `node` from PATH, then `/opt/homebrew/bin/node` or `/usr/local/bin/node`; when none exists, hooks skip without blocking and one `SessionStart` check reports once per session that they are inactive.
- The plugin's `bin/python` and native Windows `bin/python.cmd` preserve the shipped `python` command and probe candidate runtimes for Python 3.10+ before executing the workload once.
- Plugin `bin/` follows system directories on PATH, so an eligible system `python` takes precedence; missing eligible runtimes produce a clear diagnostic.

### `write-guard.js`

- Forces confirmation on writes to untracked files in the protected set: agent instructions, permission config, the pre-commit gate, package metadata.
- A tracked protected file never asks, since every write to it is diffable and revertible.
- A protected file the session created asks once on creation, then passes for that session.
- Source, tests, CI files, changelogs and lockfiles stay unprotected.

### `stale-plugin-check.js`

- Warns the user when this session still runs a replaced research version, naming the loaded and installed versions and the restart step.
- Tells the model not to run research workflows on the stale copy until the user chooses, through `AskUserQuestion`, to restart or continue.
- Fires once per session and version: after that ask, a compact/clear warning drops it (a first detection at compact/clear still asks).
- Silent when current; never blocks.

### `enforce-topic-header.js` and `report-header-table.js`

- Blocks only the owning workflow follow-up until its report exists and the follow-up call's first question text is a short plain summary: report path, `Title`, `Outcome`/`Verdict`; at most 1,500 characters, no table.
- Option descriptions and previews never supply the summary, and a reply-text table before the call does not count.
- Diagnostic/recovery questions remain available; unreadable delivery evidence is not success.
- On `Stop`: keeps a turn going once per report when its final reply (the text after the turn's last tool call) lacks the full header table; a shown follow-up does not satisfy it.
- `report-header-table.js` verifies current topic-report header delivery in the call's first question only — its text or every single-select option's preview. Never counting: a reply-text table before the call, an option description rendered as one line, some previews only, a later question, or multiSelect previews.
- A preview over the cap (2000 chars or 12 lines, a line over 86 chars counting once per 86 it spans; Claude Code withholds or clips it) never delivers. Over-cap content passes as the report file named in the question text with a compact summary naming it in every option preview.
- A denial says reply text does not count, names over-cap previews or where a misplaced copy sits, and gives the fix.
- The `Stop` check reads the turn's visible reply text instead, in one read with no wait.
- HTML-format previews (`previewFormat: "html"` hosts) are matched with their `<pre>`/`<div>` wrapper tags removed and pipe entities decoded (an entity pipe inside a cell bordered by bare pipes stays cell content); a denial tells those hosts to wrap the table in `<pre>`.
- Existing sentinel expiry is unchanged; it is not a completion receipt.

### `lib/audit-log.js`

- The append-only record writer behind the two audit hooks.
- Holds no lock, marker or state directory, has no code path that deletes or rewrites a file, and never throws, so a logging failure cannot change a permission decision.
- `RIG_AUDIT=0` disables writing only.

## Auto-allow audit log

`allow-dispatch.js` and `audit-close.js` append one JSON line each per Bash call to `~/.claude/logs/audit/`.

- One file per session (`s-<key>.jsonl`, the key being a hash of the session id) plus a shared `_no-session.jsonl` for calls that arrive without one.
- A record names the deciding plugin and hook, the session and tool-call ids, the working directory, the effective decision with its lane and provenance, and both decision modules' own verdicts.
- Command text is never written — only a digest, and for a provenance allow the manifest `src` it matched.
- Limit: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`, so the guarantee is about this file rather than about the log directory. That text is masked first (env assignments, credential-named values and flags, URL credentials, Bearer values, well-known token prefixes), and `task-log.js` tightens the log directory to `0700` and log files to `0600`.
- With several plugins installed each writes its own rows and nothing coordinates them, so one completed call yields one row per plugin at each end. That is corroboration, not duplication.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" verify
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" prune --older-than 30 --dry-run
```

- `verify` exits 1 only when a record's own hash fails to verify; torn framing from concurrent appends and every classification are warnings.
- `record_hash` detects a record that changed after it was written — it is not tamper-evidence, because the same user owns the log, the hooks and the checker.
- Classifications are observations rather than proof: a call with a decision row and no completion row is `completion-unobserved` when the session later ended and `in-flight-or-hard-kill` when it did not. Neither distinguishes a refusal at the prompt from a deny rule elsewhere or a closer that died, and a tool having run never establishes that a human approved it.
- `prune` is the only component that deletes a log file, is never run by a hook, and never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id. Nothing schedules `prune`.
- `RIG_AUDIT=0` disables audit writing without changing any permission decision.

Records follow the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), an active individual-submission Internet-Draft that is not endorsed by the IETF and has no standing in its standards process. Deliberate deviations:

- `outcome` adds `pending`, because a decision that has not executed yet has no outcome in the draft's vocabulary.
- `trust_level` is `plugin`/`unknown` rather than the draft's `L0`–`L4`, because what is observed is which component proposed an allow, not an authentication level.
- `agent_id` is `<plugin>/<hook>` rather than a URI.
- `session_id` is the host's id verbatim rather than a UUID.
- `parent_record_id` and `prev_hash` are always null, because this log is not chained — lineage is derived on read instead.

## Quality-gates rule delivery

- `rules/quality-gates.md` requires confidence blocks on analysis output, defines report-file and terminal-header formatting, and requires its local `_full/adversarial-loop.md` procedure for independent review/fix cycles, including first-pass coverage, invariant and sibling-route closure checks, and separate carried/new finding weight.
- Confidence describes the correctness of the delivered result and how it was produced and checked within scope. Report completion separately; a high score does not pass an unmet gate.
- `/research:setup` delivers it as a namespaced user rule instead of editing global configuration, as one variant at `research-quality-gates.md`:
  - when a foundry-owned `foundry-quality-gates.md` is already delivered, it links `rules/quality-gates-delta.md` — this plugin's scope and output-routing sections only, with every shared obligation read from foundry's copy;
  - otherwise it links the complete `rules/quality-gates.md`, so a standalone install loses no rule.

## Permissions files

- `.claude-plugin/permissions-allow.json` lists the tool calls the skills expect to be pre-approved.
- `.claude-plugin/permissions-allow-retired.json` lists allow rules earlier versions shipped; `/research:setup` removes exactly those from `settings.json` through `--drop-allow-file`, keeping the rule text out of the command line. The setup summary counts removals from this complete retirement set.
- `.claude-plugin/permissions-deny.json` lists operations that stay denied however broad the allow list becomes: destructive shell and git commands (`rm -rf`, `sudo`, `ssh`, `chmod 777`, force-push, `claude --dangerously-skip-permissions`).
- Both allow and deny files are merged into `~/.claude/settings.json` by `/research:setup` (Step 4); the merge is otherwise additive and idempotent.
- Deny entries are prefix matches, so they stop the documented command forms rather than every possible flag ordering.

## Generated files

Generated files remain at the project root:

```text
.experiments/state/<run-id>/              run state, JSONL log, diary, context, scripts
.experiments/<run-id>/                    hypothesis queue, checkpoint, optional journal
.experiments/{judge,verify,fortify,retro}-* intermediate analysis artifacts
.experiments/kaggle/                       generated Jupytext scripts and downloaded data
.reports/research/                         topic, judge, run, verify, fortify, retro reports
.temp/state/                               short-lived cross-phase skill contracts
```

Opt-in Kaggle Step 4 gate also writes `src/<package>/<module>.py`, `tests/test_<module>.py`, and `notebooks/01_<name>_pkg.py`.

## bin tools

Invoked by skills, not user commands.

| Script | Role |
| -- | -- |
| `bin/verify_blueprint_audit.py` | Reads and prunes the auto-allow audit log. |
| `bin/resolve_shared.py` | Resolves the plugin's `skills/_shared`; with `--persist` / `--emit` writes the per-session sentinel and prints the requested docs in one call. |
| `bin/load-agent-reference.py` | Loads agent sidecar references. Without an installed plugin, both it and `resolve_shared.py` read the cwd-relative `plugins/cc_research` tree only inside the research source repository (its `plugins/cc_research/.claude-plugin/plugin.json` names `research`); elsewhere `resolve_shared.py` exits 1 and `load-agent-reference.py` falls back to the installed cache or blocks. |
| `bin/compaction_boundary.py` | Prints a skill's compaction Boundary step and the foundry gate decision (`continue` when foundry is absent). |
| `bin/check_iteration_scope.py` | Phase 3 scope check. |
| `bin/fortify_metric_stats.py` | Fortify's repeated-run mean, sigma and classification. |
| `bin/fence_untrusted.py` | Wraps untrusted paper text for `verify`, following the `skills/_shared/untrusted-content.md` protocol. |
| `bin/run_metric_stats.py` | `run`'s repeated-measurement cursor and noise-aware keep decision when `metric_repeats` > 1. |
| `bin/revert_iteration_commit.py` | Double-revert guard `run` calls from Phase 5 and Phase 7; refuses another iteration's experiment commit at `HEAD`. |
| `bin/codex_state.py` | Reads and bumps the `--codex` co-pilot counters. |
| `bin/merge_settings_permissions.py` | Merges shipped `permissions-allow.json` / `permissions-deny.json` into `~/.claude/settings.json` in one atomic write for `/research:setup`. |
| `bin/write_skill_contract.py` | `--keep-sentinel <name>` appends the session's `--keep` items to a compaction contract's `preserve:` line. |
| `bin/health_monitor_start.py` | Creates cross-platform health-monitor sentinel coordinates; skills describe the monitoring sequence in workflow prose rather than exposing it as a user command. Consolidation is possible future cleanup, not a current requirement. |

Full packaged inventory:

`adversarial_loop.py`, `agent_watch.py`, `append_ledger.py`, `check_iteration_scope.py`, `check_output_within_root.py`, `codemap-flag.py`, `codemap_resolve.py`, `codex_state.py`, `compaction_boundary.py`, `compute_effect_size.py`, `detect-complexity.py`, `docker_sandbox_run.py`, `extract-keep-flag.py`, `fence_untrusted.py`, `find_judge_verdict.py`, `find_run_id.py`, `fortify_metric_stats.py`, `fortify_next_variant.py`, `gate-on-sentinel.py`, `get_plugin_install_path.py`, `heal_git_artifacts.py`, `health_monitor_start.py`, `load-agent-reference.py`, `make_run_dir.py`, `merge_settings_permissions.py`, `parse-skill-flags.py`, `parse_kaggle_args.py`, `read_state_field.py`, `require-vars.py`, `resolve-anti-overwrite-path.py`, `resolve_shared.py`, `resolve_shared_path.py`, `retro_analyze.py`, `revert_iteration_commit.py`, `run_metric_stats.py`, `sync_rules.py`, `verify_patient_split.py`, `write_skill_contract.py`.

The private module `_sentinel_io.py` is imported by the sentinel writers to create each `${TMPDIR}` sentinel fresh with mode 0600 instead of writing through a planted symlink.

## Kaggle notebook style

Generated notebooks follow the repo-wide notebook standard (`skills/_shared/notebook-style.md`, propagated from `codex-rig/shared/notebook-style.md`) plus a small Kaggle-only delta in `skills/kaggle/modes/style-rules.md`.

## Tests

The plugin's tests cover path safety, Codemap resolution, effect size, Docker sandboxing, hook contracts, run directories, rule resolution, shared-file resolution, retro analysis, and patient split validation.

## Future work

Potential future work includes richer native agent selection, broader compute backends for fortify, and more shared health-monitor orchestration. None is required for the current plugin contract.
