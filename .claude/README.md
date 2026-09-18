# 🤖 Claude Code plugins in Borda's AI-Rig

← [Back to root README](../README.md) · [Codex Rig deep reference](../plugins/codex-rig/README.md)

AI-Rig gives Claude Code five independently installable plugins for Python, ML, and open-source maintenance. Each one packages a bounded set of skills and, where useful, agents, rules, and hooks. Install the smallest set that solves today's problem; add the others when their workflow earns its place.

<details>
<summary><strong>Contents</strong></summary>

- [What this setup enables](#-what-this-setup-enables)
- [Install](#-install)
- [First useful session](#-first-useful-session)
- [Restore and setup](#-restore-this-setup)
- [Distribution](#-distribution)
- [Plugin architecture](#-plugin-architecture)
- [Optional integrations](#-recommended-add-ons)
- [Skills](#-skills)
- [Agents](#-agents)
- [Rules and hooks](#-rules-and-hooks)
- [Plugin composition](#-how-the-plugins-compose)
- [Orchestration flows and examples](#orchestration-flow-by-skill)
- [Native Claude Code skills](#-native-claude-code-skills)
- [Dependency matrices](#-plugin-dependency-matrix)
- [Rules](#-rules-and-hooks)
- [Architecture and teams](#-architecture)
- [Hooks and state](#-hooks)
- [Status line](#-status-line)
- [Codex integration](#-integration-with-codex)
- [Artifacts](#-artifact-layout)
- [Boundaries and future work](#-current-boundaries-and-possible-future-work)
- [Update, remove, and source checkouts](#-update-remove-and-source-checkouts)
- [Source of truth](#-source-of-truth)

</details>

## ⚡ What this setup enables

- **A bounded route from uncertainty to evidence.** Foundry audits and routes configuration work; Develop gates implementation with executable proof; OSS organizes maintainer and release work; Research keeps experiments reviewable; Codemap-py supplies static Python structure.
- **A practical installed-all experience.** Plugins remain independently installable, but Foundry specialists are available to sibling workflows when installed; absent optional agents and integrations follow each skill's documented fallback or stop condition.
- **Operational context without silent authority.** Setup can link namespaced rules and merge the documented local settings, while hooks track session state and enforce narrow boundaries. Network access, credentials, releases, and remote GitHub writes remain explicit user decisions.

| Plugin                                          | Problem it solves                                                                              | Shipped surface                                                             |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| [`foundry`](../plugins/cc_foundry/README.md)    | Configuration drift, unclear specialist ownership, and lessons that disappear between sessions | 11 skills, 10 agents, 13 rules, and 15 JavaScript hook modules              |
| [`oss`](../plugins/cc_oss/README.md)            | Repeated issue, PR, feedback-resolution, and release-readiness work                            | 5 skills, 4 agents, 1 rule, and 4 active hook modules plus 1 shared helper  |
| [`develop`](../plugins/cc_develop/README.md)    | Implementation that starts before scope, reproduction, or acceptance is clear                  | 7 skills, 1 rule, and 3 active hook modules plus 1 shared helper            |
| [`research`](../plugins/cc_research/README.md)  | ML experiments that lack literature grounding, a methodology gate, or reviewable state         | 10 skills, 2 agents, 1 rule, and 3 active hook modules plus 1 shared helper |
| [`codemap-py`](../plugins/codemap-py/README.md) | Expensive or incomplete structural exploration of Python repositories                          | 6 skills, 6 optional registered Python hooks, and 1 shared hook helper      |

The counts above are source inventories, not marketing estimates. The complete names appear below so a missing README row is visible during review.

## 📦 Install

Prerequisites: a current Claude Code release with plugin support, `git`, and the runtime requirements documented by each plugin. Foundry's setup and hooks additionally use Python 3.10+, `jq`, and Node.js; codemap-py's dispatcher requires CPython `>=3.11,<3.15`.

Register the marketplace once, then install only the plugins you need:

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
claude plugin install codemap-py@borda-ai-rig
```

Start a fresh Claude session or run `/reload-plugins`. Then run the setup skill for every installed plugin that ships rules:

```text
/foundry:setup
/oss:setup
/develop:setup
/research:setup
```

Setup is per plugin, not a Foundry-only umbrella step. Foundry performs the broader documented settings merge, links its namespaced rules, and installs `TEAM_PROTOCOL.md`; OSS, Develop, and Research link only their own namespaced rules. Codemap-py has no setup skill.

## ⚡ First useful session

Verify the configuration and choose one real task:

```text
/foundry:audit setup
/develop:plan "describe the next change"
/oss:analyse 42
/research:plan "state a measurable ML goal"
/codemap-py:scan-codebase
/codemap-py:query-code rdeps mypackage.auth
```

You do not need all five plugins. A bug fix can use Develop alone; a maintainer can install OSS without Research; Codemap-py is useful only while Python structure is unresolved. Companion workflows detect optional integrations and either use them, fall back transparently, or stop when the missing capability is essential.

## ♻️ Restore This Setup

The public install path restores plugin files through Claude Code. Setup is the separate step that delivers rules and selected project-wide settings; it is safe to rerun after an upgrade.

**Step 1 — install the plugins you need:**

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
claude plugin install codemap-py@borda-ai-rig
```

**Step 2 — start a fresh Claude session or reload plugins, then run setup for every installed plugin that ships rules:**

```text
/foundry:setup
/oss:setup
/develop:setup
/research:setup
```

/oss:setup, /develop:setup, and /research:setup deliver only that plugin's namespaced quality-gates rule. /foundry:setup also backs up and merges its documented statusLine, permissions.allow, permissions.deny, enabledPlugins, and project-pinned advisorModel values into ~/.claude/settings.json, links TEAM_PROTOCOL.md, and purges orphaned Foundry cache versions. Codemap-py has no setup skill.

Rules share the flat ~/.claude/rules/ namespace, so setup prefixes source names (foundry-\*.md, oss-quality-gates.md, develop-quality-gates.md, research-quality-gates.md). Setup replaces an existing destination only when ownership is provable; conflicts are reported and can be explicitly approved. Agents, skills, hooks, and plugin CLAUDE.md content are exposed by Claude Code's plugin loader rather than copied into ~/.claude/skills/ or ~/.claude/agents/.

A plugin upgrade can invalidate versioned rule links. Rerun the corresponding setup skill after upgrading; make sync-claude is the repository maintainer path that installs from the pushed remote and dispatches managed setup skills, not a preview of uncommitted local edits.

**Uninstall leaves setup state behind.** Claude Code provides no plugin cleanup hook. After uninstall, review or remove that plugin's namespaced rule links; Foundry also leaves ~/.claude/TEAM_PROTOCOL.md and the settings keys it merged. Preserve unrelated user settings and delete only entries you can attribute to the plugin.

## 🔄 Distribution

The checked-in plugins/ directories are source of truth; the marketplace package is the user-facing distribution. Claude Code natively exposes each installed plugin's agents, skills, hooks, and CLAUDE.md. Setup creates only the documented local projections:

```text
plugins/cc_foundry/              ← source of truth
    agents/*.md                  ← plugin loader → foundry:<agent>
    skills/*/SKILL.md            ← plugin loader → /foundry:<skill>
    hooks/hooks.json + hooks/*.js← plugin loader registers hooks
    rules/*.md                   ← /foundry:setup → ~/.claude/rules/foundry-*.md
    TEAM_PROTOCOL.md             ← /foundry:setup → ~/.claude/TEAM_PROTOCOL.md
    CLAUDE.src.md                ← /foundry:setup → ~/.claude/CLAUDE.md
    .claude-plugin/*.json        ← manifest and setup metadata
```

OSS, Develop, and Research follow the same plugin-loader pattern and link only their own namespaced rules through their setup skills. settings.local.json remains machine-local and is never distributed by this guide.

## 📦 Plugin Architecture

The six Claude plugins are peers with closed, documented responsibilities:

| Plugin          | Owns                                                                                                                                             | Optional relationship                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| Foundry         | 11 configuration, calibration, routing, session, profile, content, and maintenance skills; 10 specialist agents; 13 rules; 15 JavaScript modules | Supplies named specialists to sibling workflows when installed; absent specialists use the owning skill's fallback or stop rule    |
| OSS             | 5 maintainer and release skills; 4 agents; one quality-gates rule; 4 active hook handlers plus a shared helper                                   | Uses Foundry reviewers, bridge_CC-Codex, and gh only when available and required by the selected mode                              |
| Develop         | 7 validate-first Python workflow skills; one quality-gates rule; 3 active hook handlers plus a shared helper                                     | Can use Foundry agents, Codemap-py structural context, and bridge_CC-Codex                                                         |
| Research        | 10 experiment and evidence skills; 2 agents; one quality-gates rule; 3 active hook handlers plus a shared helper                                 | Requires explicit compute, Colab, Docker, bridge_CC-Codex, and Kaggle prerequisites for those paths; Foundry is required by Kaggle |
| Codemap-py      | 6 shared Claude/Codex structural skills; 6 registered Python hooks plus one helper                                                               | Supplies static structure to Foundry, OSS, Develop, Research, and Codex Rig; it does not prove runtime behavior                    |
| bridge_CC-Codex | 7 Claude-side bridge skills for implement, advice, review, setup, and detached-job lifecycle; no hooks or loaded rules                           | Calls Codex directly with explicit model, effort, budget, compact-envelope, and recursion contracts                                |

Install independently. Foundry is a quality upgrade, not a prerequisite for the other plugins unless a specific skill says so. No plugin silently installs credentials, enables network access, publishes releases, or mutates remote GitHub state.

## 🔌 Recommended Add-ons

Optional integrations are disabled or absent unless the user installs and enables their own toolchain. Each consuming workflow checks its own preconditions.

### bridge_CC-Codex

bridge_CC-Codex replaces the retired external rescue plugin for review pre-passes, bounded mechanical work, and read-only advice in selected Develop, OSS, Foundry, and Research workflows. Install it from this repository's marketplace when those paths are useful:

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install bridge@borda-ai-rig
```

Start a fresh session or run `/reload-plugins`, then run `/bridge:setup`. Foundry setup may enable an installed bridge but does not install it or either provider credential. Missing bridge support is generally an explicit graceful skip, while a user-requested bridge path stops and reports the missing prerequisite.

### Colab, Docker, Kaggle, and Codemap

/research:run --colab requires the colab-mcp runtime tool and a connected runtime. /research:run --compute=docker requires a reachable Docker daemon. /research:kaggle requires the authenticated Kaggle CLI and the Foundry plugin's foundry:sw-engineer; it stops when those prerequisites are missing. Develop can use Codemap-py for structural context. These integrations remain user-managed; this repository does not provide credentials, GPU capacity, or hosted runtimes.

## ⚡ Skills

<details>
<summary><strong>Complete 46-skill roster</strong></summary>

### Foundry: configuration and reusable practice

| Skill                  | What it does                                                                                                                                         |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/foundry:setup`       | Merge the documented Claude settings, link Foundry rules, install `TEAM_PROTOCOL.md`, and verify the installed layout.                               |
| `/foundry:audit`       | Check configuration, routing, hooks, references, and instruction health; consolidate findings into a reviewable report.                              |
| `/foundry:calibrate`   | Exercise fixed and behavioral cases and score recall, precision, confidence accuracy, and routing gaps.                                              |
| `/foundry:manage`      | Create, update, or remove agents, skills, and rules; update or remove existing hooks while maintaining references. Hook creation is not implemented. |
| `/foundry:brainstorm`  | Structure an idea through deliberate perspectives before committing to implementation.                                                               |
| `/foundry:investigate` | Diagnose configuration, environment, hook, permission, and runtime anomalies that lack a normal Python traceback.                                    |
| `/foundry:profile`     | Summarize wall-clock activity from hook logs and, when transcripts are available, estimate token and cost data.                                      |
| `/foundry:distill`     | Turn reviewed session corrections into proposed durable instruction changes; it does not make model behavior self-correcting by itself.              |
| `/foundry:session`     | Dump, restore, list, recall, and clean bounded project-local handover state across context resets.                                                   |
| `/foundry:create`      | Co-create and write an artifact outline, then optionally delegate generation to `foundry:creator`.                                                   |
| `/foundry:humanizer`   | Review prose for mechanical or synthetic patterns and propose a more natural edit without bypassing factual review.                                  |

[Foundry's README](../plugins/cc_foundry/README.md) is the source for modes, flags, outputs, setup mutations, retention, troubleshooting, and cleanup.

### OSS: maintainer work

| Skill          | What it does                                                                                                                      |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `/oss:analyse` | Triage an issue, PR, repository, or vitality question before implementation and prepare evidence-backed maintainer communication. |
| `/oss:review`  | Review a GitHub PR or local change with mandatory and scope-selected quality dimensions, then consolidate actionable findings.    |
| `/oss:resolve` | Re-read current feedback, select valid findings, apply authorized fixes, and preserve unresolved items with rationale.            |
| `/oss:release` | Assess SemVer and release readiness and prepare local release artifacts; it does not edit versions, tag, push, or publish.        |
| `/oss:setup`   | Link the plugin's namespaced quality-gate rule and verify delivery.                                                               |

GitHub-backed modes require an authenticated `gh` CLI. Replies, reviews, merges, tags, pushes, and publication remain maintainer decisions. [The OSS README](../plugins/cc_oss/README.md) documents scopes, modes, reports, fallbacks, and release boundaries.

### Develop: validate-first Python changes

| Skill               | What it does                                                                                                            |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `/develop:plan`     | Resolve uncertain scope and acceptance before code changes, with optional deeper decomposition.                         |
| `/develop:feature`  | Require an executable failing demo or equivalent acceptance proof before implementation.                                |
| `/develop:fix`      | Reproduce a reported defect with a failing regression check before changing the implementation.                         |
| `/develop:refactor` | Establish characterization and safety-net evidence before behavior-preserving structural work.                          |
| `/develop:debug`    | Narrow an unknown failure to a supported root cause before proposing a fix.                                             |
| `/develop:review`   | Review local Python work across the top scope-selected dimensions by default, or all selected dimensions with `--full`. |
| `/develop:setup`    | Link the plugin's namespaced quality-gate rule and verify delivery.                                                     |

Develop assumes Python 3.10+ and a project with executable verification, usually pytest. It is not a general non-Python migration or onboarding framework. [The Develop README](../plugins/cc_develop/README.md) documents exact gates, flags, worktrees, fork modes, and recovery paths.

### Research: reviewable ML iteration

| Skill               | What it does                                                                                                                |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `/research:topic`   | Survey current literature and implementation evidence for a bounded research question.                                      |
| `/research:plan`    | Turn a measurable goal, dataset, metric, constraints, and guardrails into `program.md`.                                     |
| `/research:judge`   | Review an experiment plan before expensive execution and identify methodology blockers.                                     |
| `/research:run`     | Execute a bounded metric-improvement campaign with recorded state, comparisons, and guardrails.                             |
| `/research:sweep`   | Run the plan-to-judge-to-experiment route; existing output, `--team`, or unresolved judge results may require confirmation. |
| `/research:verify`  | Compare a paper's stated method with an implementation and report supported, missing, or divergent behavior.                |
| `/research:fortify` | Design and assess ablations that test whether a claimed improvement survives controlled alternatives.                       |
| `/research:retro`   | Explain what a completed campaign established, failed to establish, and should change next.                                 |
| `/research:kaggle`  | Create or extend grounded Jupytext Kaggle notebooks using the authenticated Kaggle CLI.                                     |
| `/research:setup`   | Link the plugin's namespaced quality-gate rule and verify delivery.                                                         |

Research keeps plans, evidence, and state reviewable; it cannot guarantee a metric improvement or repair a weak dataset, metric, split, baseline, or compute budget. [The Research README](../plugins/cc_research/README.md) documents artifacts, optional Colab/Docker/Codex/Kaggle paths, stopping conditions, and current limitations.

### Codemap-py: structural Python evidence

| Skill                        | What it does                                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `/codemap-py:scan-codebase`  | Build or refresh the local Python structural index.                                                                  |
| `/codemap-py:query-code`     | Query imports, symbols, call relationships, documentation, coverage context, diffs, and selected structural signals. |
| `/codemap-py:test-impact`    | Find indexed tests related to a qualified Python symbol, with optional mock exclusion.                               |
| `/codemap-py:rename-refs`    | Rename indexed Python references with explicit review boundaries for dynamic and external consumers.                 |
| `/codemap-py:integration`    | Check, plan, apply, synchronize, or demonstrate Codemap integration without rebuilding the index.                    |
| `/codemap-py:debrief-coding` | Report coding-session telemetry without running integration, indexing, or queries.                                   |

Codemap is static AST evidence, not runtime proof. Dynamic dispatch, callbacks, string imports, inheritance, generated code, external consumers, and test outcomes still require source inspection or execution. [The Codemap-py README](../plugins/codemap-py/README.md) documents query grammar, index freshness, platform behavior, and safe fallbacks.

### bridge_CC-Codex: bounded cross-host work

| Skill               | What it does                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `/bridge:implement` | Ask Codex to make one bounded write-capable change and return a compact result envelope.                           |
| `/bridge:advise`    | Ask Codex a bounded read-only question with explicit model, effort, and timeout controls.                          |
| `/bridge:review`    | Ask Codex for an independent read-only adversarial review of named evidence.                                       |
| `/bridge:setup`     | Check both local CLIs and bridge configuration without spending provider credits unless a live probe is requested. |
| `/bridge:status`    | Read the state of one detached bridge implementation job.                                                          |
| `/bridge:result`    | Retrieve one completed detached job's compact result and transcript reference.                                     |
| `/bridge:cancel`    | Request cooperative cancellation of one detached job and preserve any work already reported.                       |

The bridge invokes the installed `codex` CLI directly; it does not require the retired rescue plugin. [The bridge_CC-Codex README](../plugins/bridge_cc-codex/README.md) documents envelopes, lifecycle records, model and budget controls, and cross-host recursion safety.

</details>

## 🧩 Agents

Agents provide narrow ownership; they are not a claim that every command always launches every agent. Skills select only relevant roles, and optional-agent routes disclose their fallback when a named specialist is unavailable.

<details>
<summary><strong>Complete 16-agent roster and ownership map</strong></summary>

### Foundry agents

| Agent                        | Primary ownership                                                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `foundry:sw-engineer`        | Maintainable Python/ML implementation, APIs, types, and reproducibility                                                                            |
| `foundry:solution-architect` | System design, public contracts, coupling, and migration planning                                                                                  |
| `foundry:qa-specialist`      | Regression evidence, suspicious tests, edge cases, and acceptance                                                                                  |
| `foundry:linting-expert`     | Ruff, mypy, pre-commit, formatting, and suppression discipline                                                                                     |
| `foundry:perf-optimizer`     | Profile-first performance and resource analysis                                                                                                    |
| `foundry:doc-scribe`         | Public docs, docstrings, examples, README content, and migration guidance; changelogs and release notes belong to `oss:shepherd` or `/oss:release` |
| `foundry:web-explorer`       | Current primary documentation, changelogs, and external evidence                                                                                   |
| `foundry:curator`            | Agent, skill, rule, configuration, and cross-reference hygiene                                                                                     |
| `foundry:challenger`         | Adversarial review of plans, risky changes, and unsupported conclusions                                                                            |
| `foundry:creator`            | Structure and validation of new Claude artifacts                                                                                                   |

### OSS and Research agents

| Agent                   | Primary ownership                                                                |
| ----------------------- | -------------------------------------------------------------------------------- |
| `oss:shepherd`          | Contributor experience, SemVer, deprecations, changelogs, and release readiness  |
| `oss:cicd-steward`      | CI/CD, matrices, caching, trusted publishing, and flaky-run diagnosis            |
| `oss:gh-scraper`        | Internal bounded collection of GitHub issue, PR, review, and repository evidence |
| `oss:repo-warden`       | Internal repository policy and health evidence used by maintainer workflows      |
| `research:scientist`    | Papers, hypotheses, methodology, metrics, and ablation design                    |
| `research:data-steward` | Dataset provenance, split integrity, leakage, imbalance, and loader behavior     |

`gh-scraper` and `repo-warden` are workflow support agents, not standalone promises to mutate GitHub or repository policy.

### Agent relationship map

Skills choose only the roles relevant to the selected scope; the table is a routing map, not a promise that every run launches every agent.

- foundry:sw-engineer owns Python and hook implementation; foundry:qa-specialist validates public behavior; foundry:linting-expert owns static-analysis rules; foundry:solution-architect produces design and migration artifacts; foundry:perf-optimizer measures bottlenecks before tuning; foundry:doc-scribe owns technical docs; foundry:creator owns outward-facing artifacts from approved outlines; foundry:web-explorer fetches current external evidence; foundry:curator audits configuration; and foundry:challenger attacks plans and unsupported conclusions.
- oss:gh-scraper fetches raw GitHub vitality data for /oss:analyse; three oss:repo-warden instances score assigned axis groups; oss:shepherd owns contributor and release communication; oss:cicd-steward owns GitHub Actions reliability. These internal collection/scoring agents are not standalone user commands.
- research:scientist handles named papers, hypotheses, and experiments. research:data-steward is manual-use data expertise and can delegate external collection to foundry:web-explorer when both are available.
- Sibling plugins disclose fallback routing when Foundry agents are absent. A missing required integration or specialist stops only where that workflow says it must.

</details>

## 📐 Rules and hooks

Rules are delivered by setup and remain namespaced so plugins can install independently. Hooks register from each enabled plugin's manifest; users should not copy hook entries into `settings.json`.

### Rule inventory

Foundry ships 13 rules: `artifact-lifecycle`, `claude-config`, `communication`, `compaction`, `debugging`, `external-data`, `foundry-config`, `git-commit`, `public-github`, `python-code`, `python-testing`, `quality-gates`, and `task-lifecycle`.

OSS, Develop, and Research each ship their own namespaced `quality-gates` rule. Similar filenames are deliberate independent copies, not an undeclared installation dependency.

<details>
<summary><strong>Rule reference</strong></summary>

| Rule file               | Applies to                      | What it governs                                                                        |
| ----------------------- | ------------------------------- | -------------------------------------------------------------------------------------- |
| `artifact-lifecycle.md` | Global                          | Dot-prefixed artifact layout, run-directory naming, and retention policy               |
| `claude-config.md`      | Global                          | Portable paths, bounded Bash execution, and navigation conventions                     |
| `communication.md`      | Global                          | Progress narration, tone, output routing, and confidence reporting                     |
| `compaction.md`         | Global                          | Context-compaction contract and durable skill state in `.temp/state/skill-contract.md` |
| `debugging.md`          | Global                          | Root-cause diagnosis, evidence before fixes, and post-fix validation                   |
| `external-data.md`      | Global                          | Completeness and pagination for REST, GraphQL, and GitHub CLI reads                    |
| `foundry-config.md`     | `.claude/**`                    | Plan gates, post-edit checks, XML conventions, cleanup, and settings allow entries     |
| `git-commit.md`         | Global                          | Commit format and push/branch safety                                                   |
| `public-github.md`      | Global                          | Permitted read-only public GitHub operations and forbidden writes                      |
| `python-code.md`        | `**/*.py`                       | Python style, APIs, deprecations, and type/design conventions                          |
| `python-testing.md`     | `tests/**/*.py`, `**/test_*.py` | pytest structure, parametrization, mocking, and doctest placement                      |
| `quality-gates.md`      | Global                          | Confidence blocks, quality loops, and output routing                                   |
| `task-lifecycle.md`     | Global                          | Task sequencing, subagent conventions, and lifecycle handoffs                          |

</details>

### Hook inventory

Foundry ships 15 JavaScript modules: `agent-router`, `artifact-guard`, `batch-nudge`, `commit-guard`, `enforce-audit-header`, `enforce-profile-header`, `lint-on-save`, `md-compress`, `report-header-table`, `rtk-rewrite`, `sentinel-read-allow`, `session-restore`, `statusline`, `task-log`, and `teammate-quality`. Together they provide routing context, report/artifact gates, bounded safety checks, optional command rewriting, session handover, status display, timing logs, and teammate-quality reminders. `report-header-table` is a shared helper used by report gates rather than a separately registered event handler.

OSS ships `agent-router`, `enforce-analyse-header`, `enforce-review-header`, `report-header-table`, and `sentinel-read-allow`. Develop ships `agent-router`, `enforce-review-header`, `report-header-table`, and `sentinel-read-allow`. Research ships `agent-router`, `enforce-topic-header`, `report-header-table`, and `sentinel-read-allow`.

Codemap-py registers six optional hooks: `guard-redundant-scan.py`, `inject-preamble.py`, `log-skill-start.py`, `log-tool-use.py`, `record-exhausted.py`, and `seed-session.py`; `_hookutil.py` is their shared non-executable helper. The hooks add ambient index status and session-sharded telemetry and narrowly discourage redundant structural scans. They fail open and are not required for scanning or querying.

Hooks can validate known boundaries and provide context; they cannot guarantee that every model response follows every instruction. The plugin READMEs document event bindings, sentinels, timeouts, logs, and recovery steps.

### How rules are auto-loaded

A rule file may carry `paths:` frontmatter listing glob patterns. Claude Code loads matching rule files automatically when you open or edit a file matching it — no explicit invocation needed. Global rules (no frontmatter at all, no `paths:` restriction, or `paths: "*"`) load every session. Rules additive: multiple rules can apply to same file.

Example: editing `tests/test_transforms.py` auto-loads `python-testing.md` (matches `tests/**/*.py`) and `python-code.md` (matches `**/*.py`). Editing `.claude/agents/sw-engineer.md` loads `foundry-config.md` (matches `.claude/**`).

### Orchestration flow by skill

Each skill follows defined topology for how it composes agents:

<details>
<summary><strong>`/oss:review`</strong> — parallel fan-out, then consolidation</summary>

```text
Tier 0: git diff --stat (mechanical gate — skips trivial diffs)
Tier 1: Codex pre-pass (independent diff review, ~60s)
Tier 2: scope-selected agents (default capped at four; --full runs every selected dimension)
→ consolidator reads all findings → final report
→ shepherd writes --reply output (if flag present)
```

</details>

<details>
<summary><strong>`/develop:feature`</strong> — sequential with inner loops</summary>

```text
Step 1: sw-engineer (codebase analysis)
Step 2: sw-engineer (demo test — TDD contract)
Step 2 review: in-context validation gate
Step 3: sw-engineer (implementation) + qa-specialist (parallel)
Step 4: review+fix loop (max 3 cycles): sw-engineer → qa-specialist → linting-expert
Step 5: doc-scribe (docs update)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

</details>

<details>
<summary><strong>`/develop:fix`</strong> — reproduce-first</summary>

```text
Step 1: sw-engineer (root cause analysis)
Step 2: sw-engineer (regression test that fails)
Step 2 review: in-context validation gate
Step 3: sw-engineer (minimal fix)
Step 4: review+fix loop (max 3 cycles)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

</details>

<details>
<summary><strong>`/develop:refactor`</strong> — test-first</summary>

```text
Step 1: sw-engineer + linting-expert (coverage audit, parallel)
Step 2: qa-specialist (characterization tests)
Step 2 review: in-context validation gate
Step 3: sw-engineer (refactor)
Step 5: review+fix loop (max 3 cycles)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

</details>

<details>
<summary><strong>`/research:topic`</strong> — research-first</summary>

```text
web-explorer (fetch current papers/docs) → scientist (deep analysis, writes to file)
→ consolidator reads findings → implementation plan
(--team: multiple scientist instances on competing method families)
```

</details>

<details>
<summary><strong>`/foundry:brainstorm`</strong> — conversational spec, then task breakdown</summary>

```text
idea mode:
  Step 1: context scan (Read README, Grep keywords)
  Step 2: AskUserQuestion (clarify, one at a time, max 10)
  Step 3: build tree loop (seed 3–5 branches → deepen/close/merge/add, max 10 ops)
  Step 4: Write tree doc → .plans/blueprint/YYYY-MM-DD-<slug>.md (Status: tree)
  Step 5: curator (tree quality audit — coverage, closure quality, open threads)
  Step 6: AskUserQuestion (approval gate) → suggest /foundry:brainstorm breakdown <tree>

breakdown mode (triggered by "breakdown <tree-or-spec>"):
  Auto-detects Status field:
  Status: tree → D1 present summary → D2 distillation questions (max 5)
           → D3 write spec section-by-section → D4 suggest next step
  Status: draft → B1 blocking questions → B2 action plan table → B3 post-plan prompt
```

</details>

<details>
<summary><strong>`/foundry:audit`</strong> — curator per file, then consolidation</summary>

```text
per-config-file: curator (reads file, writes findings to /tmp/audit-<ts>/<file>.md)
→ consolidator reads all finding files → ranked report with upgrade proposals
(upgrade mode: web-explorer fetches latest Claude Code docs first)
```

</details>

### Skill usage examples

<details>
<summary><strong>Workflow examples and command sequences</strong></summary>

**`/research:plan`, `/research:run`, `/research:judge`, `/research:sweep` — Profile-first bottleneck discovery and metric-improvement loop**

```text
# plan mode — interactive config wizard → program.md
/research:plan "increase test coverage to 90%"
/research:plan src/mypackage/train.py           # profile-first: cProfile → ask what to optimize → wizard
/research:plan "improve F1 from 0.82 to 0.87" coverage.md  # write to custom path

# judge mode — pre-flight quality gate before the expensive run loop
/research:judge                    # review program.md methodology → APPROVED / NEEDS-REVISION / BLOCKED
/research:judge coverage.md        # audit a specific program file
/research:judge --skip-validation  # skip local metric/guard validation (cross-machine workflows)

# run mode — sustained metric-improvement loop
/research:run "increase test coverage to 90%"        # run from text goal (20-iteration loop; auto-rollback on regression)
/research:run coverage.md                            # run from program.md config file

# resume mode — continue after crash or manual stop
/research:run --resume                               # reads program_file from state.json
/research:run coverage.md --resume                   # resume specific run

# sweep mode — automated pipeline: auto-plan → judge gate → run
/research:sweep "increase test coverage to 90%"      # may prompt for existing output, --team, or unresolved judge results
/research:sweep coverage.md                          # sweep from program.md config

# flags (run/sweep)
/research:run "reduce training time by 20%" --team   # parallel exploration across axes
/research:run "improve validation accuracy" --colab  # GPU workloads via Colab MCP (opt-in)
```

> **Colab MCP is opt-in.** `.mcp.json` defines the server but does not start it. To enable: add `"colab-mcp"` to `enabledMcpjsonServers` in `.claude/settings.local.json`, then restart Claude Code.

**`/oss:review` — Parallel PR review; `/develop:review` — local file/diff review**

```text
# PR review (GitHub)
/oss:review 42          # review PR by number
/oss:review 42 --reply  # review + draft contributor-facing comment

# Local diff or file review (no GitHub PR needed)
/develop:review src/mypackage/transforms.py
/develop:review             # review current git diff
```

**`/oss:analyse` — Issue, PR, Discussion and repo health**

```text
/oss:analyse 123           # auto-detects issue/PR/discussion; wide-net related search
/oss:analyse health        # repo health overview with duplicate clustering
/oss:analyse ecosystem     # downstream consumer impact analysis
/oss:analyse 123 --reply   # analyse + draft contributor reply
```

**`/oss:release` — Release notes, changelog, readiness checks**

```text
/oss:release notes v1.2.0..HEAD
/oss:release changelog v1.2.0..HEAD
/oss:release prepare v2.0.0
/oss:release audit
```

**`/foundry:manage` — Agent/skill lifecycle**

```text
/foundry:manage create agent security-auditor "Security specialist for vulnerability scanning"
/foundry:manage update optimize perf-audit
/foundry:manage delete web-explorer
```

**`/foundry:audit` — Config health sweep + upgrade**

```text
/foundry:audit                        # full sweep — report, then gate offers fix levels
/foundry:audit --upgrade              # apply docs-sourced improvements
/foundry:audit --adversarial          # challenger + Codex adversarial review
/foundry:audit agents                 # agents scope only
/foundry:audit skills                 # skills scope only
/foundry:audit skills --skip-gate     # skills scope, suppress follow-up gate (programmatic)
```

**`/develop:feature`, `/develop:fix`, `/develop:refactor`, `/develop:plan`, `/develop:debug` — Development workflows**

Each mode enforces validation gate *before* writing implementation code:

- `/develop:plan` — scope analysis; produces structured plan in `.plans/active/plan_<slug>.md`
- `/develop:feature` — TDD demo validation before writing code
- `/develop:fix` — reproduction test before touching anything
- `/develop:refactor` — coverage audit before changing structure
- `/develop:debug` — investigation-first; evidence gathering → hypothesis gate → minimal fix

```text
/develop:feature add batched predict() method to Classifier
/develop:fix TypeError when passing None to transform()
/develop:refactor src/mypackage/transforms.py
/develop:plan improve caching in the data loader
/develop:debug why does the validation loss spike at epoch 3?
```

**`/oss:resolve` — Resolve a PR end-to-end**

```text
/oss:resolve 42                                              # pr mode: live GitHub comments → conflict check → semantic resolution → action items
/oss:resolve https://github.com/org/repo/pull/42             # same as above, URL form
/oss:resolve report                                          # report mode: latest /oss:review findings as action items; no GitHub re-fetch
/oss:resolve 42 report                                       # pr + report mode: GitHub comments + /oss:review findings, aggregated and deduplicated
/oss:resolve "rename foo to bar throughout the auth module"  # single-comment fast path (comment dispatch mode)
```

**`/foundry:investigate` — Systematic failure diagnosis**

```text
/foundry:investigate "hooks not firing on Save"
/foundry:investigate "codex exec exits 127 on this machine"
/foundry:investigate "CI fails but passes locally"
/foundry:investigate "/foundry:calibrate times out every run"
/foundry:investigate "uv run pytest can't find conftest.py"
```

**`/foundry:session` — Session handover + parking lot**

```text
/foundry:session dump       # sweep the conversation, write the handover doc, print /clear
/foundry:session recall     # print a stored handover back into context (manual fallback)
/foundry:session list       # stored handovers + open parked items
/foundry:session park <idea>  # stash one open loop without derailing the current task
/foundry:session sweep      # audit the conversation for unlanded ideas and questions
/foundry:session drop <item>  # close a parked item
```

</details>

## 🧭 Native Claude Code skills

Two capabilities below ship **natively with Claude Code** — not part of this repo's plugins (foundry / oss / develop / research / codemap-py). Work in any project, including this one, alongside plugin skills above.

### Skill auto-selection (the "skills advisor")

Every skill — native or plugin — carries a `description` with `TRIGGER` / `SKIP` routing signals. Claude Code reads those descriptions and **auto-selects matching skill** when request lines up with one, so no need to remember exact slash command. `description` field is what does advising: routing signal deciding which skill (if any) fits task. Always overridable by invoking skill explicitly with `/<skill-name>`.

**When to rely on it**

- **Discovery / "which skill fits this?"** — describe task in plain language, let auto-selection surface the right skill (e.g. "review this PR" -> `/oss:review`, "why is CI failing but local passes" -> `/foundry:investigate`). Intended path when exact command unknown.
- **Explicit control** — when you already know which skill you want, type slash command directly (`/develop:fix ...`); explicit invocation always wins over auto-selection.
- **Caveat** — auto-selection only fires when description clearly matches; vague requests may match nothing (Claude answers inline) or wrong skill. Sharpen request, or invoke explicitly, if wrong thing triggers. Auto-selection never runs a skill silently — invocation is visible.

### `/deep-research` — multi-source, fact-checked research report

Native research harness for questions needing real sourcing rather than single answer. **Fans out parallel web searches, fetches sources, adversarially verifies each claim, synthesizes cited report** — verification pass separates it from plain search: claims that can't be corroborated dropped or flagged rather than repeated.

**Invocation**

```text
/deep-research <question>
```

**When to use it**

- **Use it** for deep, multi-source, fact-checked write-up on a topic — comparisons, state-of-the-art surveys, "what's actually true about X" where citations and cross-checking wanted, not quick recall.
- **Don't use it** for single-fact lookup (normal web search or inline answer faster) or anything answerable from codebase (use `/codemap-py:query-code`, grep, or direct read).
- **Scope first** — if question underspecified (e.g. "what car should I buy" with no budget / use-case / region), asks 2-3 clarifying questions before spending search budget. Giving constraints up front produces sharper report.

## 🗺️ Plugin dependency matrix

<details>
<summary><strong>Install and capability matrix</strong></summary>

| Consumer   | Standalone install | Setup projection | Optional companion                                                         | Hard prerequisite called out by source                                                  |
| ---------- | ------------------ | ---------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| foundry    | Yes                | /foundry:setup   | bridge_CC-Codex for selected audits/calibration; Node.js, Python, jq       | Claude Code plugin support; project repository for setup                                |
| oss        | Yes                | /oss:setup       | Foundry agents, bridge_CC-Codex, Codemap-py, authenticated gh              | gh only for GitHub-backed modes                                                         |
| develop    | Yes                | /develop:setup   | Foundry agents, Codemap-py, bridge_CC-Codex                                | Python project and executable verification for code workflows                           |
| research   | Yes                | /research:setup  | Foundry agents, bridge_CC-Codex, Colab MCP, Docker, Kaggle CLI, Codemap-py | Clean Git worktree for /research:run; explicit runtime/credential requirements per flag |
| codemap-py | Yes                | None             | Claude or Codex host                                                       | CPython >=3.11,\<3.15 for its dispatcher; optional coverage>=7.4 for coverage indexing  |
| bridge     | Yes                | /bridge:setup    | Installed Codex and Claude Code CLIs                                       | Provider authentication, permission, budget, and workspace authority remain user-owned  |

</details>

<details>
<summary><strong>Routing and fallback matrix</strong></summary>

| Workflow                                | Primary roles                                              | Optional roles/tools                       | Fallback or stop behavior                                                                      |
| --------------------------------------- | ---------------------------------------------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| /foundry:calibrate                      | Foundry specialists plus curator                           | bridge_CC-Codex                            | Reports missing optional routes; calibration measures instruction behavior, not correctness    |
| /oss:review                             | Foundry review roles when installed                        | bridge_CC-Codex, gh for PR scope           | Uses general-purpose fallback for absent specialists; GitHub operations remain user-owned      |
| /oss:analyse vitality                   | gh-scraper → three repo-warden scorers → shepherd assembly | gh                                         | Only vitality mode uses this internal pipeline; no direct user invocation                      |
| /develop:feature, fix, refactor, review | sw-engineer, QA, linting, plus scope-selected roles        | bridge_CC-Codex, Codemap-py                | Missing optional context is reported or skipped; executable verification remains decisive      |
| /research:topic, run, judge, sweep      | scientist and scope-selected Foundry roles                 | bridge_CC-Codex, Codemap-py, Colab, Docker | Requested unavailable integrations stop; absent optional specialists follow the skill contract |
| /research:kaggle                        | foundry:sw-engineer                                        | Authenticated Kaggle CLI                   | Stops without Foundry or required Kaggle grounding                                             |
| /codemap-py:\*                          | Static index/query engine                                  | Six optional Python hooks                  | Dynamic behavior and runtime correctness require source/tests/execution                        |

</details>

<details>
<summary><strong>Agent short names and dispatch boundaries</strong></summary>

Use full names in prompts (foundry:qa-specialist, oss:shepherd, research:scientist). Short-name routing is an implementation convenience, not a second public API. gh-scraper and repo-warden are internal /oss:analyse vitality stages; research:data-steward is manual-use; foundry:creator is reached by /foundry:create after outline approval.

</details>

## 🏗️ Architecture

<details>
<summary><strong>Handoffs, review tiers, and teams</strong></summary>

### File-based handoff protocol

*When multiple analysis agents return findings inline, orchestrator's context window fills with intermediate output it never uses directly — file-based handoff keeps orchestrator clean for decision-making.*

**When it applies:**

- Any skill spawning **2+ agents in parallel** for analysis or review
- Any **single agent** expected to produce >500 tokens of findings
- Exception: implementation agents (writing code) return inline — output is the deliverable
- Exception: single-agent single-question spawns where output inherently short (\<200 tokens)

**Agent contract** — spawned agent must:

1. Write full output to `<RUN_DIR>/<agent-name>.md` using Write tool
2. Return to orchestrator **only** compact JSON envelope on final line:

```json
{
  "status": "done",
  "findings": 3,
  "severity": {
    "critical": 0,
    "high": 1,
    "medium": 2
  },
  "file": "<path>",
  "confidence": 0.88,
  "summary": "1 high (missing tool), 2 medium (unused tools)"
}
```

**Orchestrator contract:**

1. Do NOT read agent files back into main context — delegate to consolidator agent instead
2. Collect compact envelopes (tiny — stay in context)
3. Spawn consolidator to read all `<RUN_DIR>/*.md` files and write final report

**Threshold:** 4+ agent files → mandatory consolidator; 2–3 files → orchestrator may read directly if total content \<2K tokens.

**RUN_DIR convention:**

- Ephemeral (per-run): `/tmp/<skill>-<timestamp>/` — created once before any spawns
- Persistent (final reports): `.temp/`

**Reference implementations:** `/foundry:calibrate` is canonical; `/foundry:audit` Step 3 (`curator` per file → consolidator); `/oss:review` Steps 3–6.

______________________________________________________________________

### Tiered review pipeline

Every review skill gates cheap work before spawning expensive agents — cheaper tiers short-circuit pipeline when diff trivial or issues already clear:

| Tier                     | What it does                                                          | Cost |
| ------------------------ | --------------------------------------------------------------------- | ---- |
| **T0 — Mechanical gate** | `git diff --stat` — skips trivial or empty diffs before any AI work   | Zero |
| **T1 — Codex pre-pass**  | Focused diff review (~60 s); flags bugs, edge cases, and logic errors | Low  |
| **T2 — Claude agents**   | Specialized parallel agents selected by scope and role frontmatter    | High |

Which tiers each skill uses:

| Skill                                                   | T0  | T1  | T2  |
| ------------------------------------------------------- | :-: | :-: | :-: |
| `/develop:feature`, `/develop:fix`, `/develop:refactor` |  ✓  |  ✓  |  ✓  |
| `/oss:review`                                           |  ✓  | ✓ ‡ |  ✓  |
| `/research:run`                                         |  ✓  |  ✓  |  ✓  |
| `/foundry:audit` (fix via gate)                         |  ✓  |  ✓  |  ✓  |
| `/oss:resolve`                                          |     |     |  ✓  |

‡ For `/oss:review`, Codex runs as full **co-reviewer** alongside T2 agents — findings independently consolidated rather than seeding agent prompts (unbiased review).

______________________________________________________________________

### Agent Teams

Agent Teams is Claude Code's experimental multi-agent feature. Teams always **user-invoked** — nothing auto-spawns. Auto-spawning teams would multiply token costs 5-10x on routine tasks; explicit invocation lets you make cost/benefit call per run. Enabled via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `settings.json`.

**When to use teams vs subagents:**

| Signal                                              | Team | Subagents |
| --------------------------------------------------- | :--: | :-------: |
| Competing root-cause hypotheses                     |  ✓   |           |
| Cross-layer feature: impl + QA + docs in parallel   |  ✓   |           |
| SOTA research: multiple competing method clusters   |  ✓   |           |
| Adversarial review (teammates challenge each other) |  ✓   |           |
| Sequential pipeline (fix → test → lint)             |      |     ✓     |
| Independent parallel review dimensions              |      |     ✓     |
| Single file / single module scope                   |      |     ✓     |
| Routine tasks (sync, distill, release)              |      |     ✓     |

**Skills with team support:**

| Skill                     | When to use                                                               |
| ------------------------- | ------------------------------------------------------------------------- |
| `/develop:fix --team`     | Bug spans modules; competing root-cause hypotheses                        |
| `/develop:feature --team` | Cross-layer feature needing impl + QA + docs in parallel                  |
| `/research:topic --team`  | Multiple competing method families to evaluate                            |
| `/research:run --team`    | Goal spans multiple optimization axes (speed = arch + pipeline + compute) |
| `/research:plan --team`   | Wizard + parallel exploration: teammates each own a different axis        |
| `/develop:refactor`       | Directory or system-wide scope → Claude proposes team (heuristic)         |

**Model and effort settings:** Agent frontmatter is the source of truth for each role's model and effort; these values can change with the shipped plugin. Teams are user-invoked and should stay small enough for the task and budget.

**Communication protocol:** Inter-agent messages use AgentSpeak v2 (defined in `TEAM_PROTOCOL.md`) — ~60% token savings vs natural language. Status codes (`alpha`/`beta`/`gamma`/`delta`/`epsilon`/`omega`), action symbols (`+`/`-`/`~`/`!`), file locking (`+lock`/`-lock`), priority prefixes (`!!` urgent, `..` FYI). Lead-to-human communication uses normal English.

**Security in teams:** No standalone security agent. `qa-specialist` automatically embeds OWASP Top 10 security checks when task touches auth, payment flows, or user data.

**Quality hooks:** `hooks/teammate-quality.js` handles `TeammateIdle` (redirects to pending tasks) and `TaskCompleted` (reserved for future quality gates).

</details>

## 🪝 Hooks

<details>
<summary><strong>Hook inventory, state machine, and recovery behavior</strong></summary>

### Hooks inventory

Foundry has 15 JavaScript files; report-header-table.js is a shared helper and not a separately registered handler. OSS registers four active handlers plus the shared report helper; Develop and Research each register three active handlers plus the helper. Codemap-py registers six Python hooks plus \_hookutil.py, a shared helper.

| Hook                | Event                       | Matcher     | Purpose                |
| ------------------- | --------------------------- | ----------- | ---------------------- |
| task-log.js         | lifecycle events            | all         | Session state tracking |
| lint-on-save.js     | PostToolUse                 | Write, Edit | Lint on save           |
| md-compress.js      | PreToolUse                  | Edit (.md)  | Token compression      |
| rtk-rewrite.js      | PreToolUse                  | Bash        | CLI output compression |
| teammate-quality.js | TeammateIdle, TaskCompleted | all         | Team quality gate      |
| statusline.js       | (statusLine)                | n/a         | Status bar             |

### task-log.js state machine

`task-log.js` is the central event handler. It handles lifecycle events and maintains runtime state read by `statusline.js`:

**Event → action mapping:**

| Event                | Action                                                                                                                                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PreToolUse`         | Logs Task/Agent and Skill invocations to `logs/invocations.jsonl`; opens codex plugin session file; increments per-tool-type state file                                                                                                      |
| `PostToolUse`        | Closes Codex plugin session files when Skill(codex:\*) or Agent(codex:\*) completes; computes wall-clock timing delta from PreToolUse start marker and appends to `~/.claude/logs/timings.jsonl`                                             |
| `PostToolUseFailure` | Records timing with error status to timings.jsonl (same timing path as PostToolUse)                                                                                                                                                          |
| `UserPromptSubmit`   | Writes queue marker to `state/queue/` to light processing badge 💬 in statusline                                                                                                                                                             |
| `SubagentStart`      | Creates `state/agents/<id>.json` with agent type, model, color, start timestamp — one file per agent (no race)                                                                                                                               |
| `SubagentStop`       | Deletes per-agent file; appends completion entry to `invocations.jsonl`                                                                                                                                                                      |
| `PreCompact`         | Appends to `logs/compactions.jsonl`; extracts modified file paths from transcript; writes `state/session-context.md`                                                                                                                         |
| `Stop`               | Clears `state/tools/` — resets 🔧 row between turns (agents intentionally NOT cleared — may still be running); clears `state/queue/` processing markers (dismisses 💬 badge) and removes orphaned timing start markers from `state/timings/` |
| `SessionEnd`         | Deletes entire `/tmp/claude-state-<session>/` directory (agents, tools, codex, queue, timings, dedup locks); runs `git worktree prune`; removes orphaned worktrees >2h                                                                       |

**State files layout:**

```markdown
/tmp/claude-state-<session>/
├── agents/<id>.json        # one per active subagent (created at start, deleted at stop)
├── codex/<id>.json         # one per active codex plugin session
├── skills/<id>.json        # one per in-flight Skill() call
├── tools/<tool>.json       # one per tool type fired this turn (cleared at Stop)
├── timings/<tool_use_id>.json   # in-flight timing start markers (PreToolUse → PostToolUse)
├── queue/<timestamp>.json       # processing badge markers (UserPromptSubmit → Stop)
└── pending/<tool_use_id>.json   # agent type cache for SubagentStart resolution

.claude/state/
└── session-context.md      # modified-file breadcrumb (survives compaction)

.claude/logs/               # skill-specific logs (project-scoped)
# Hook audit logs are global — written to ~/.claude/logs/:
#   invocations.jsonl       append-only: agent launches, skill invocations, completions (includes project field)
#   compactions.jsonl       append-only: compaction events (includes project field)
#   timings.jsonl           append-only: per-tool wall-clock timing (includes project field)
```

**Age-out rules:**

- Agents: 10-minute safety-net — files older than 10 min with no corresponding Stop event indicate crashed agent; statusline excludes them
- Codex activity uses the same session-scoped state and active-agent safety filters; the renderer does not promise a separate Codex timeout row
- Worktrees: 2-hour cutoff in SessionEnd cleanup

**Inline `SessionStart` hook** (shell command, not a JavaScript file): `claude auth status > ~/.claude/state/subscription.json` snapshots billing plan data for the status line asynchronously. The plugin also registers `session-restore.js` for the `clear` matcher.

### Supplementary hooks

Registered by the Foundry hook manifest alongside `task-log.js`:

**`lint-on-save.js`** (PostToolUse — Write, Edit) — closes gap between "Claude edits a file" and "a human runs pre-commit" by linting every file the moment it's written. Runs `pre-commit run --files <path>` on each Write/Edit, exits 2 on failure so Claude sees diagnostics and applies fix immediately. No-op when `.pre-commit-config.yaml` absent or pre-commit not installed.

**`md-compress.js`** (PreToolUse — Edit only, `.md` files only) — normalizes token-wasteful whitespace in file being edited, in place, right before edit runs, and normalizes `old_string` same way so pre-normalization match still finds its target. Collapses table column padding (2+ spaces → 1), consecutive blank lines, trailing whitespace — all outside fenced code blocks; write is atomic (write-then-rename). Deliberately does *not* run on Read: earlier version did, to save Read-time tokens, but normalizing on every Read silently rewrote table alignment in any `.md` file Claude merely looked at, including files nobody asked to touch — dropped in favor of Edit-only scope, which only touches files someone is actually editing.

**`rtk-rewrite.js`** (PreToolUse — Bash) — rewrites supported CLI calls through the optional RTK proxy (`git status` → `rtk git status`). RTK performs format-aware compression for Git, pytest, and build output; the savings depend on the command and output, and compressed output still requires normal verification. The hook is a no-op when RTK is not installed — see root [README → Optional integrations and add-ons](../README.md#optional-integrations-and-add-ons).

</details>

## 📊 Status Line

Foundry's statusline.js is configured by /foundry:setup through the top-level statusLine setting. It renders two lines from the current hook payload and session-scoped state:

```text
Line 1: model (effort) · project · billing · context bar · 💬 while processing
Line 2: ⚡ active skill · 🤖 active agents (including codex:*) · 🛠️ recent tool counts
```

The renderer reads agents/, skills/, and tools/ from /tmp/claude-state-<session>/, plus the queue marker. task-log.js writes those files; the renderer never mutates them. Agent activity uses a 10-minute safety filter for worktree agents and a longer backstop for non-worktree agents, while tool activity expires after 30 seconds. SessionEnd removes the session subtree and stale crash remnants.

Billing is presentation only: API-key mode shows actual token-rate spend from the payload; OAuth/subscription mode shows the plan and a theoretical API-rate estimate, not a statement charge. The context bar, model, and effort come from Claude Code's statusline payload. The hook never guarantees correctness or replaces task-level verification.

## 🤝 Integration with Codex

→ Full architecture: [root README → How the packages compose](../README.md#how-the-packages-compose)

→ Install: see [Recommended Add-ons → bridge_CC-Codex](#-recommended-add-ons)

### Skills digestion

Skills check availability at runtime using the exact installed selector `bridge@borda-ai-rig` and honor an explicit disabled entry. If the bridge is absent or disabled, optional calls are skipped and reported rather than silently falling back to the removed original plugin.

**Invocation contract:**

| Operation   | Use                                                                                                                         | Invocation                                                           |
| ----------- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `implement` | One bounded write-capable change whose task, files, evidence, stopping condition, and required checks are stated completely | `Skill(skill="bridge:implement", args="<self-contained task>")`      |
| `advise`    | A read-only question or decision that must not edit the workspace                                                           | `Skill(skill="bridge:advise", args="<self-contained question>")`     |
| `review`    | An independent read-only adversarial pass over named files, artifacts, or the current diff                                  | `Skill(skill="bridge:review", args="<self-contained review brief>")` |

The task argument must stand alone: name the objective, relevant paths or evidence, permission boundary, expected result, and stop condition. Do not pass internal plan labels, step numbers, private shorthand, or references that require the callee to read unavailable context.

**What Claude retains:**

- Long-horizon planning and research (`/research:topic`, `/research:run`, `/develop:plan`)
- Orchestration of multiple agents in defined topologies
- Judgment calls: design decisions, spec approval, test validity assessment
- Final validation: Claude always verifies Codex output via `git diff HEAD` before accepting changes

**Why the division can help:** Claude supplies task context and orchestration; Codex can provide an independent diff or mechanical pass. Treat both outputs as review input and keep Claude's source-backed validation as the acceptance gate.

## 📂 Artifact Layout

<details>
<summary><strong>Runtime artifacts and retention</strong></summary>

Runtime artifacts live at project root in dot-prefixed dirs — separate from versioned config in `.claude/`. Dot-prefix signals "generated output, not source".

```text
.plans/blueprint/        ← /foundry:brainstorm spec and tree files
.plans/active/           ← todo_*.md, plan_*.md
.plans/closed/           ← completed plans
.notes/                  ← lessons.md, diary, guides
.reports/calibrate/      ← /foundry:calibrate benchmark runs
.reports/resolve/        ← /oss:resolve lint+QA gate outputs
.reports/audit/          ← /foundry:audit analysis runs
.reports/review/         ← /oss:review multi-agent outputs
.experiments/            ← /research:run skill runs (improve mode)
.developments/           ← /develop:* review-cycle handoffs
.temp/                   ← long output from any skill (quality-gates rule)
```

Artifact locations and filenames are skill-specific: plan artifacts use `.plans/...`, research artifacts use `.experiments/...`, development handoffs use `.developments/...`, and report-producing workflows use selected `.reports/<name>/...` directories. Do not assume every run creates a timestamped `.reports/<skill>/.../result.jsonl`; inspect the invoked skill's output contract for exact paths and files. Foundry's artifact-lifecycle rule describes age-based cleanup, so verify that rule before relying on automatic deletion. Incomplete runs (crashed/timed-out) are kept for debugging. All dot-prefixed dirs are gitignored — see `.claude/rules/artifact-lifecycle.md` for TTL policy and full details.

</details>

## 🔗 How the plugins compose

The packages are peers, not a hidden dependency chain:

- Foundry adds the richest Claude specialist roster and configuration lifecycle. OSS, Develop, and Research can use those specialists when available and disclose a general-purpose fallback when they are not.
- Codemap-py adds structural evidence when the affected Python surface is uncertain. A complete Codemap query still needs runtime or test evidence for behavior.
- OSS owns maintainer and release-readiness work; Develop owns implementation discipline; Research owns the experiment lifecycle. A handoff should preserve evidence rather than silently changing ownership.
- Optional bridge_CC-Codex, Colab, Docker, and Kaggle routes require their own installed tools, credentials, runtime permissions, and task-specific preconditions.

A practical feature path is `/develop:plan` → `/develop:feature` → `/develop:review`. A public contribution can continue with `/oss:review` → `/oss:resolve`. An ML path can start with `/research:topic` → `/research:plan` → `/research:judge` → `/research:run` → `/research:retro`. Use fewer steps when the evidence is already available.

## 🧭 Current boundaries and possible future work

- Skills make decisions and evidence visible; they do not make generated code, review findings, or research conclusions correct by construction.
- Setup-created rule links, Foundry's `TEAM_PROTOCOL.md`, and Foundry-managed settings survive plugin uninstall because Claude Code provides no plugin cleanup hook. Follow each README's manual cleanup list.
- Network-backed work depends on user-managed authentication and runtime approval. No plugin installs credentials or silently broadens access.
- Release workflows prepare and assess artifacts but do not edit release versions, tag, push, upload, or publish.
- Project-local reports and state need an owner and retention policy. Some Foundry state has documented age-based cleanup; other artifacts remain until the project removes them.
- Core scripts and hooks are designed for Windows, macOS, and Linux, but optional tools and individual integrations have narrower contracts documented by their owning README.
- Broader language support, richer dynamic-analysis evidence, automatic uninstall cleanup, and deeper native runtime integration are possible future directions, not committed roadmap items.

## ⬆️ Update, remove, and source checkouts

Update installed plugins with the current Claude Code plugin commands:

```bash
claude plugin update foundry@borda-ai-rig
claude plugin update oss@borda-ai-rig
claude plugin update develop@borda-ai-rig
claude plugin update research@borda-ai-rig
claude plugin update codemap-py@borda-ai-rig
```

Run `/foundry:setup`, `/oss:setup`, `/develop:setup`, or `/research:setup` again after updating the corresponding plugin. Remove a plugin with `claude plugin uninstall <plugin>` (for example, `claude plugin uninstall foundry`), then follow its README if you also want to remove setup-created links or settings.

Marketplace installation is the public path. A repository checkout also provides `make sync-claude`, but that command installs from the pushed GitHub remote rather than the dirty local worktree. It is a deliberate maintainer restore path, not a local preview command.

## 🏗️ Source of truth

- [Foundry product, skills, agents, rules, hooks, and setup](../plugins/cc_foundry/README.md)
- [OSS maintainer workflows, agents, reports, and boundaries](../plugins/cc_oss/README.md)
- [Develop workflow gates, flags, artifacts, and recovery](../plugins/cc_develop/README.md)
- [Research experiment lifecycle, integrations, and limitations](../plugins/cc_research/README.md)
- [Codemap-py query, indexing, runtime, and packaging reference](../plugins/codemap-py/README.md)
- [Anthropic's plugin marketplace documentation](https://code.claude.com/docs/en/discover-plugins)

License: [Apache-2.0](../LICENSE).
