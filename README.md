# 🏠 Borda's AI-Rig

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude_Code-plugins-orange)](https://code.claude.com/docs/en/discover-plugins) [![Codex](https://img.shields.io/badge/Codex-plugins-green)](https://developers.openai.com/codex/cli/reference)

Practical agent workflows for Python, ML, and open-source maintenance. AI-Rig turns recurring work—scoping a change, reproducing a bug, reviewing a pull request, running an experiment, or checking release readiness—into explicit workflows with specialist ownership, evidence gates, and reviewable artifacts.

Seven packages serve two runtimes: six marketplace plugins for Claude Code, three for Codex, with `codemap-py` and `bridge_CC-Codex` shared by both.

<details>
<summary><strong>Contents</strong></summary>

- [Start here](#-start-here)
- [What this setup enables](#-what-this-setup-enables)
- [Complete capability map](#-complete-capability-map)
- [Installed-all blueprint](#-installed-all-blueprint)
- [Install for Claude Code](#-install-for-claude-code)
- [Install for Codex](#-install-for-codex)
- [What each plugin solves](#-what-each-plugin-solves)
- [Practical workflow sequences](#-practical-workflow-sequences)
- [How the packages compose](#-how-the-packages-compose)
- [Optional integrations and add-ons](#-optional-integrations-and-add-ons)
- [Artifacts and evidence](#-artifacts-and-evidence)
- [Current boundaries](#-current-boundaries)
- [Repository checkout and synchronization](#-repository-checkout-and-synchronization)
- [Upgrade and remove](#-upgrade-and-remove)
- [Troubleshooting](#-troubleshooting)
- [Documentation map](#-documentation-map)
- [Contributing](#-contributing)

</details>

## ⚡ Start here

Choose the smallest plugin that solves the job in front of you. Every package installs independently; optional integrations add specialist depth or structural context without becoming hidden prerequisites.

| Need                                                                    | Start with                                                | What it gives you                                                                                                         |
| ----------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Keep Claude agent configuration, routing, and reusable guidance healthy | [🏭 `foundry`](plugins/cc_foundry/README.md)              | 10 specialist agents, 11 configuration/content skills, rules, hooks, audit, and calibration                               |
| Triage GitHub work, review PRs, resolve feedback, and prepare releases  | [🌱 `oss`](plugins/cc_oss/README.md)                      | 4 agents and 5 maintainer skills with human-reviewed replies and no automatic publish step                                |
| Plan, build, fix, refactor, debug, or review Python changes             | [🛠️ `develop`](plugins/cc_develop/README.md)              | 6 validate-first development workflows plus setup, with reproduction/demo/safety-net gates                                |
| Plan and run literature-grounded ML experiments                         | [🔬 `research`](plugins/cc_research/README.md)            | 2 research agents and 10 skills for planning, judging, running, verifying, ablation, retrospectives, and Kaggle notebooks |
| Answer import, caller, coupling, rename, and affected-test questions    | [🗂️ `codemap-py`](plugins/codemap-py/README.md)           | The same 6 structural-analysis skills for Claude Code and Codex, backed by a local static Python index                    |
| Use evidence-first workflows and specialist role cards in Codex         | [🤖 `Codex Rig`](plugins/codex-rig/README.md)             | 13 workflow skills, 1 lifecycle manager, 15 role cards, shared gates, calibration, and artifact contracts                 |
| Hand bounded implement, advise, and review calls between the two hosts  | [🌉 `bridge_CC-Codex`](plugins/bridge_cc-codex/README.md) | 3 bridge verbs in both directions with explicit models, effort, budgets, compact envelopes, and recursion safety          |

If you use Claude Code, read the [Claude guide](.claude/README.md). If you use Codex, read the [Codex guide](.codex/README.md).

## 🎯 What this setup enables

<details>
<summary><strong>Show the evidence-first value of the full suite</strong></summary>

- A bounded route from uncertainty to evidence: Foundry audits configuration and routing, Develop gates Python changes, OSS organizes maintainer work, Research keeps experiments reviewable, Codemap-py supplies static structure, and Codex Rig provides the parallel host-native workflow.
- Scope-selected specialist review: OSS and Codex Rig use narrow context packs and mandatory QA/challenge coverage where their contracts require it; smaller or lower-risk work can stay on the parent path.
- Review-to-remediation continuity: OSS and Codex Rig save findings as artifacts, ask which work to select, group related findings, and verify the selected closure rather than silently applying every comment.
- Validate-first development: Develop's feature path requires a failing demo, its fix path requires a failing regression test, and its refactor path protects current behavior before editing.
- Metric-guarded research: Research proposes one scoped change, measures the configured metric and guard, keeps improvements, and reverts regressions as auditable Git history.
- Calibration and recovery: Foundry and Codex Rig measure workflow/instruction quality, confidence, and known leaks; those results improve the process but do not guarantee model correctness.

These are workflow contracts, not promises that every run launches every specialist, finds every dynamic dependency, or publishes a correct change. The linked plugin READMEs define each route's prerequisites, fallback, and stop behavior.

</details>

## 🔧 Complete capability map

This is the literal shipped skill inventory. The host guides list every agent, role, rule, and hook; the plugin READMEs document arguments, outputs, prerequisites, recovery, and edge cases.

<details>
<summary><strong>Show complete capability map</strong></summary>

| Package            | Complete skill roster                                                                                                                                                                                                                                                                                                               |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 🏭 Foundry         | `/foundry:setup`, `/foundry:audit`, `/foundry:calibrate`, `/foundry:manage`, `/foundry:brainstorm`, `/foundry:investigate`, `/foundry:profile`, `/foundry:distill`, `/foundry:session`, `/foundry:create`, `/foundry:humanizer`                                                                                                     |
| 🌱 OSS             | `/oss:analyse`, `/oss:review`, `/oss:resolve`, `/oss:release`, `/oss:setup`                                                                                                                                                                                                                                                         |
| 🛠️ Develop         | `/develop:plan`, `/develop:feature`, `/develop:fix`, `/develop:refactor`, `/develop:debug`, `/develop:review`, `/develop:setup`                                                                                                                                                                                                     |
| 🔬 Research        | `/research:topic`, `/research:plan`, `/research:judge`, `/research:run`, `/research:sweep`, `/research:verify`, `/research:fortify`, `/research:retro`, `/research:kaggle`, `/research:setup`                                                                                                                                       |
| 🗂️ Codemap-py      | `scan-codebase`, `query-code`, `test-impact`, `rename-refs`, `integration`, `debrief-coding`, namespaced as `/codemap-py:...` in Claude Code and `$codemap-py:...` in Codex                                                                                                                                                         |
| 🤖 Codex Rig       | `$codex-rig:assess`, `$codex-rig:audit`, `$codex-rig:calibrate`, `$codex-rig:code-remediate`, `$codex-rig:code-review`, `$codex-rig:implement`, `$codex-rig:investigate`, `$codex-rig:kaggle`, `$codex-rig:manage`, `$codex-rig:optimize`, `$codex-rig:release`, `$codex-rig:research`, `$codex-rig:sync`, `$codex-rig:agent-shims` |
| 🌉 bridge_CC-Codex | `implement`, `advise`, `review`, `setup`, plus Claude-side detached-job `status`, `result`, `cancel`; namespaced as `/bridge:...` in Claude Code and `$bridge:...` in Codex                                                                                                                                                         |

</details>

## 🏗️ Installed-all blueprint

<details>
<summary><strong>Show the complete two-runtime inventory and design boundary</strong></summary>

The installed-all Claude surface is 16 agents across Foundry (10), OSS (4), and Research (2), plus 46 skills across Foundry (11), OSS (5), Develop (7), Research (10), Codemap-py (6), and bridge_CC-Codex (7). It also includes 16 namespaced rules (13 Foundry rules plus one quality-gates rule for each companion that ships rules) and host hooks whose exact active-module counts are maintained in the [Claude guide](.claude/README.md). Codex Rig adds 14 skills, 15 role cards, shared gates, calibration, and one optional read-only health hook; Codemap-py contributes six skills to both runtimes and no Codex hook manifest; the bridge_CC-Codex plugin contributes four Codex-side skills and the reverse MCP transport.

Specialist roles are requested by name or auto-selected. Claude agents dispatch as `<plugin>:<agent>`; the Codex column marks roles that also ship as a Codex Rig role card.

| Role                          | Claude plugin | Codex | Owns                                                                           |
| ----------------------------- | ------------- | ----- | ------------------------------------------------------------------------------ |
| **sw-engineer**               | 🟠 foundry    | ✓     | Implementation, refactors, SOLID, type safety; also authors hook JS            |
| **solution-architect**        | 🟠 foundry    | ✓     | ADRs, API surface, migration plans, component design — specs only, no code     |
| **qa-specialist**             | 🟠 foundry    | ✓     | pytest, hypothesis, mutation testing, ML test patterns; carries OWASP coverage |
| **linting-expert**            | 🟠 foundry    | ✓     | ruff, mypy, pre-commit, type annotations                                       |
| **perf-optimizer / squeezer** | 🟠 foundry    | ✓     | Profile-first CPU/GPU/memory/I/O work; the Codex role card is named `squeezer` |
| **doc-scribe**                | 🟠 foundry    | ✓     | Docstrings, API references, README, Sphinx/MkDocs                              |
| **web-explorer**              | 🟠 foundry    | ✓     | External docs, release notes, version and migration lookups                    |
| **curator**                   | 🟠 foundry    | ✓     | Config quality: agent/skill/rule verbosity, duplication, roster overlap        |
| **challenger**                | 🟠 foundry    | ✓     | Adversarial review — treats claims as unproven until evidence                  |
| **creator**                   | 🟠 foundry    | —     | Blog posts, slide decks, threads, talk abstracts from an approved outline      |
| **shepherd / oss-shepherd**   | 🟢 oss        | ✓     | Contributor communication, triage, SemVer, release coordination                |
| **cicd-steward**              | 🟢 oss        | ✓     | GitHub Actions health, test matrices, caching, SHA pinning                     |
| **gh-scraper**                | 🟢 oss        | —     | Internal: bulk GitHub REST/GraphQL fetch for vitality scoring                  |
| **repo-warden**               | 🟢 oss        | —     | Internal: scores vitality axes from pre-fetched data                           |
| **scientist**                 | 🟣 research   | ✓     | Paper analysis, hypothesis generation, experiment design                       |
| **data-steward**              | 🟣 research   | ✓     | Dataset versioning, split validation, leakage detection                        |
| **delegation-lead**           | —             | ✓     | Codex-only: routes disjoint workstreams, enforces a parent handover gate       |
| **security-auditor**          | —             | ✓     | Codex-only: read-only trust-boundary, secrets, dependency, supply-chain audit  |

Sixteen Claude agents, fifteen Codex role cards. `delegation-lead` and `security-auditor` are Codex-only — on the Claude side, security review is folded into `foundry:qa-specialist`. The [Codex guide](.codex/README.md) maps each role card to its ownership and requested model tier.

| Runtime         | Source inventory                                                                           | Distribution and trust boundary                                                                                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code     | Six peer plugins: Foundry, OSS, Develop, Research, Codemap-py, and bridge_CC-Codex.        | Marketplace packages expose plugin-loader agents, skills, hooks, and `CLAUDE.md`; setup projects only documented rules/settings and does not install credentials or remote authority. |
| Codex           | Codex Rig plus optional Codemap-py and bridge_CC-Codex.                                    | Marketplace packages expose namespaced skills and role cards; blank-agent injection or inline fallback is disclosed, while persistent named-agent selection remains unverified.       |
| Source checkout | `plugins/` is the source of truth; `.claude/` and `.codex/` are host guides/configuration. | Direct marketplace install uses published bytes; `Makefile` is a deliberate restore path that consumes the pushed remote and may project selected local policy.                       |

The design principle is simple: make uncertainty, ownership, evidence, and recovery visible while keeping packages independently installable. A workflow may route to a specialist, but a role card, task name, artifact, or green gate is not proof that a model followed it or that the resulting change is correct. Human review remains the acceptance boundary for consequential work.

</details>

## 📦 Install for Claude Code

Prerequisite: a current Claude Code release with plugin support. The commands below follow Anthropic's [marketplace and plugin installation flow](https://code.claude.com/docs/en/discover-plugins).

Register the marketplace once, then install only what you need:

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
claude plugin install codemap-py@borda-ai-rig
claude plugin install bridge@borda-ai-rig
```

Start a fresh Claude session or run `/reload-plugins`. For each installed plugin that ships rules, run its setup skill once and again after an upgrade:

```text
/foundry:setup
/oss:setup
/develop:setup
/research:setup
```

The setup skills create namespaced rule links. Only Foundry performs the broader documented settings merge and installs `TEAM_PROTOCOL.md`; the other setup skills deliver their own rules. Codemap-py needs no setup skill—build its first project index with `/codemap-py:scan-codebase`. bridge_CC-Codex ships no rules either; `/bridge:setup` is a free static check of the local `codex` and `claude` CLIs.

First useful commands:

```text
/foundry:audit setup
/develop:plan "describe the next change"
/oss:analyse vitality
/research:plan "state a measurable ML goal"
/codemap-py:query-code rdeps mypackage.auth
```

## 📦 Install for Codex

Prerequisite: a current Codex release with stable plugin commands. The commands below match the [official Codex developer command reference](https://developers.openai.com/codex/cli/reference#codex-plugin).

```bash
codex plugin marketplace add Borda/AI-Rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
codex plugin add bridge@borda-ai-rig
codex plugin list
```

`codemap-py` is optional. Install it when unresolved Python structure is part of the task; skip it for a fully localized edit. To pin reproducible marketplace bytes, add the marketplace with `--ref <release-tag>` rather than copying an old example tag.

Start a fresh Codex session, then try:

```text
$codex-rig:agent-shims doctor
$codex-rig:investigate find the root cause of the failing test
$codex-rig:implement apply the verified fix
$codex-rig:code-review review the current diff
$codemap-py:scan-codebase
$codemap-py:query-code rdeps mypackage.auth
```

From a shell, quote `$codex-rig:...` and `$codemap-py:...` invocations so the shell does not expand `$`.

## 🎯 What each plugin solves

### 🏭 Foundry: keep the agent system maintainable

Foundry addresses workflow and configuration drift. Its named agents separate architecture, implementation, testing, documentation, performance, research, and configuration review; its audit and calibration skills make routing gaps and stale instructions visible.

Install it when you want the full Claude specialist roster. Companion plugins can fall back to general-purpose role prompts when Foundry is absent, but those fallbacks are less specialized. Calibration is a synthetic instruction-quality signal, not a guarantee of production correctness. [Read the Foundry reference.](plugins/cc_foundry/README.md)

### 🌱 OSS: keep contributor and release work traceable

OSS turns GitHub threads and PR evidence into structured maintainer work. It can draft contributor-facing replies, but the maintainer reviews and posts them. It can prepare and audit release artifacts, but it does not change package versions, create tags, or publish releases.

GitHub-backed workflows require an authenticated `gh` CLI. Foundry, bridge_CC-Codex, and codemap-py are optional integrations. [Read the OSS reference.](plugins/cc_oss/README.md)

### 🛠️ Develop: prove the change before trusting the edit

Develop gives Python work a validate-first path: plan uncertain scope, require a failing demo for features, reproduce bugs before fixing them, lock behavior before refactoring, investigate before implementing, and review local Python changes across scope-selected dimensions.

The code-changing workflows assume Python 3.10+ and pytest-style project verification. They are not general migration, onboarding, or non-Python workflows. [Read the Develop reference.](plugins/cc_develop/README.md)

### 🔬 Research: make ML iteration reviewable

Research links literature, a measurable `program.md`, methodology review, bounded experiment campaigns, paper-to-code verification, ablations, and post-run analysis. State and reports stay in the project so another maintainer can understand what was tried.

Metrics, guards, datasets, compute, credentials, and experimental validity remain user-owned. Optional Colab, Docker, Codex, Kaggle, Foundry, and Codemap paths stop or degrade according to the specific skill contract. [Read the Research reference.](plugins/cc_research/README.md)

### 🗂️ Codemap-py: answer structural questions without pretending they are runtime proof

Codemap-py indexes Python imports, symbols, call edges, test relationships, and selected documentation references. Use the smallest query that resolves the open structural question; skip the tool when the edit surface is already known.

The index is static AST evidence. Dynamic dispatch, callbacks, string imports, inheritance relationships, external consumers, and test outcomes still require source inspection or execution. The dispatcher currently requires CPython `>=3.11,<3.15`; Codex receives the six skills but not Claude's optional ambient hooks. [Read the Codemap-py reference.](plugins/codemap-py/README.md)

### 🤖 Codex Rig: make Codex workflows comparable and auditable

Codex Rig provides investigation, development, review, remediation, research, optimization, release-readiness, management, audit, calibration, sync, and Kaggle workflows that share gate and artifact contracts. Specialist role cards can be injected into runtime blank agents when that route exists; otherwise the parent performs a disclosed inline pass.

The plugin does not claim persistent named-agent registration, silently enable network access, or mutate remote GitHub state. Direct plugin installation also leaves global and project instructions unchanged. [Read the Codex Rig reference.](plugins/codex-rig/README.md)

## 🗺️ Practical workflow sequences

### Bug report to verified fix

```text
/oss:analyse 42
/develop:fix 42
/develop:review
```

In Codex:

```text
$codex-rig:investigate diagnose the reported failure
$codex-rig:implement apply the verified fix
$codex-rig:code-review review the resulting diff
```

### ML idea to reviewable result

```text
/research:topic "candidate method"
/research:plan "state the measurable goal"
/research:judge program.md
/research:run program.md
/research:retro
```

### Structural question to safer refactor

```text
/codemap-py:scan-codebase
/codemap-py:query-code rdeps mypackage.auth
/develop:refactor "change the authenticated request boundary"
```

### PR review to selected remediation

```text
/oss:review 123
/oss:resolve 123 report
```

In Codex:

```text
$codex-rig:code-review #123
$codex-rig:code-remediate #123 +review
```

### Fuzzy idea to scoped implementation

<details>
<summary><strong>Show the clarify → plan → implement path</strong></summary>

```text
/foundry:brainstorm "add caching to the data pipeline"
/develop:plan "turn the approved outline into a measurable change"
/develop:feature "implement the accepted contract"
/develop:review
```

Keep the approved outline, acceptance statement, demo or reproduction evidence, and review result together. If the task is still ambiguous, stop at planning rather than presenting a speculative implementation as complete.

</details>

## 🔗 How the packages compose

<details>
<summary><strong>Show the installed-all topology and end-to-end paths</strong></summary>

The packages remain independently installable, but their boundaries are intentional when the full suite is present. Foundry supplies Claude configuration, reusable rules, hooks, and specialist agents; Develop owns validate-first Python changes; OSS owns GitHub triage, review, and release preparation; Research owns literature-grounded ML iteration; Codemap-py answers unresolved Python structure for Claude and Codex; Codex Rig provides the Codex-native workflow, role-card, gate, and artifact layer.

Claude companion workflows use Foundry specialists when available and follow each plugin's documented fallback or stop condition when a companion is absent. Develop, Research, and Codex Rig can use Codemap-py as optional structural context; a missing or stale index is reported and does not become a claim that no callers or tests exist. Network-backed paths keep authentication and approval user-owned.

For a new change, use the Develop plugin or Codex Rig's `implement` skill after the scope is understood. For a GitHub issue or pull request, use OSS or Codex Rig's review/remediation pair. For an ML hypothesis, use Research's topic → plan → judge → run → retro sequence. Add Codemap-py only while imports, callers, coupling, or test impact remain unresolved.

Two Claude–Codex integration patterns are supported when their optional plugins are installed: Claude can delegate a bounded mechanical task or pre-review to Codex and then inspect the local diff; Codex can independently review Claude's staged/local work and leave an artifact for the next remediation step. Both are local, evidence-backed handoffs; neither grants remote mutation or turns one model's result into proof.

For a daily maintainer pass, start with `/oss:analyse vitality` or the Codex Rig `assess` route, review selected PRs with `/oss:review` or `$codex-rig:code-review`, reproduce and fix one high-value issue with the Develop plugin or `investigate` → `implement`, then assess release readiness with `/oss:release` or `release`. Keep the scope and evidence artifacts for the next session rather than treating the sequence as an unattended campaign.

</details>

## 🔗 Optional integrations and add-ons

<details>
<summary><strong>Show supported optional capabilities and prerequisites</strong></summary>

| Capability             | Adds                                                                       | User-owned prerequisite and boundary                                                                                                                                      |
| ---------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Codemap-py             | Static imports, call edges, coupling, test-impact, and integration context | Install the plugin and run its scan; stale, incompatible, or missing indexes degrade to bounded source inspection.                                                        |
| GitHub CLI (`gh`)      | OSS issue/PR workflows and Codex PR evidence collection                    | Authenticate `gh` for private or complete evidence; workflows do not install credentials or mutate remote state.                                                          |
| Kaggle CLI             | Research and Codex Rig grounded Kaggle notebook workflows                  | Install and authenticate the CLI; missing credentials stop the requested online grounding path.                                                                           |
| Docker                 | Isolated Research metric and guard execution via `--compute=docker`        | A reachable Docker daemon and valid commands inside the configured image are required.                                                                                    |
| Colab MCP              | Research GPU metric verification via `--colab`                             | Enable [`colab-mcp`](https://github.com/googlecolab/colab-mcp), connect a Colab runtime, and use it instead of Docker; unavailable capability stops the explicit request. |
| Claude–Codex co-review | Optional adversarial or mechanical Codex pass from Claude workflows        | Install `bridge@borda-ai-rig` and keep the `claude`/`codex` CLIs available; the request is never silently replaced.                                                       |
| Foundry RTK hook       | Optional shell-output compression for supported Claude commands            | Install/configure [RTK](https://github.com/rtk-ai/rtk) if desired; the hook is a Foundry convenience and is not a correctness or security boundary.                       |
| cc-Lens                | Optional local Claude Code token, cost, and tool-use analytics             | Install [cc-Lens](https://github.com/Arindam200/cc-lens) separately; it reads local Claude state and is not part of AI-Rig's workflow or correctness evidence.            |
| Caveman                | Optional compressed response mode for Claude conversations                 | Install [Caveman](https://github.com/JuliusBrussee/caveman) separately; it changes presentation, not plugin behavior, validation, or safety guarantees.                   |

External add-ons are not hidden installation prerequisites. The owning plugin README remains authoritative for flags, supported versions, and recovery when an integration is requested.

RTK is an output-compression convenience for supported shell commands, not a replacement for Claude's native file tools or a correctness gate. Codex's repository policy routes eligible commands explicitly because the current Codex path does not rewrite commands in place.

</details>

## 📊 Artifacts and evidence

<details>
<summary><strong>Show where workflows write state and how to read it</strong></summary>

Codex Rig writes `.reports/codex/<skill>/<timestamp>/` with a validated `result.json`, gate records/logs, and skill-specific evidence. Research writes run state under `.experiments/` and final reports under `.reports/research/`. Codemap-py writes its local index under `.cache/codemap/` and includes freshness, coverage, truncation, and blind-spot metadata in query results.

Artifacts are reviewable evidence, not authority. Read the commands, source, tests, gate status, confidence, and unresolved limits before accepting consequential work. Generated state is project-local; follow the owning plugin's retention and cleanup guidance before deleting or automating it.

</details>

## 🧭 Current boundaries

- AI-Rig makes process and evidence visible; it does not make model output correct by construction. Review diffs, commands, reports, and confidence limits before accepting consequential work.
- Every package is independently installable. Cross-plugin features are capability-gated and must degrade or stop explicitly when the optional dependency is absent.
- Network-backed workflows depend on user-managed authentication and runtime approval. The plugins do not install credentials or silently broaden network access.
- Release workflows prepare and assess local artifacts. Remote publication, pushes, comments, merges, tags, and package uploads remain human-owned unless a future workflow explicitly documents a different contract.
- Generated reports, plans, and experiment state are project-local. Read the owning plugin's artifact and cleanup sections before automating retention.
- Platform support is capability-specific. Core workflows target Windows, macOS, and Linux; Codex Rig's authenticated legacy-shim cleanup remains POSIX-only, and Codemap's scan timeout flag uses a Unix-only signal where available.
- Possible future work is described as an opportunity, not a roadmap promise. The shipped source and tests define the current contract.

## 🏗️ Repository checkout and synchronization

Direct marketplace installation is the normal public path. A source checkout adds maintainer tooling and the broader, deliberate `Makefile` restore flow:

```bash
make sync-all         # Claude + Codex scopes (default target)
make sync-claude      # Claude scope only
make sync-codex       # Codex scope only
```

The `Makefile` installs from the pushed GitHub remote, not uncommitted local files. Commit and push first if you intentionally want a checkout change to become installable; never use sync as a preview of a dirty worktree.

Each external Claude marketplace and plugin add, update, uninstall, or install command has a 120-second timeout. Use the `EXTERNAL_PLUGIN_TIMEOUT_SECONDS` environment variable to select another positive-integer deadline; managed AI-Rig plugin and setup commands retain their existing behavior.

<details>
<summary><strong>Show repository layout, distribution paths, and session-only development</strong></summary>

The checked-in `plugins/` directories are source of truth. Claude Code loads each plugin's manifest, agents, skills, hooks, and plugin instructions from its marketplace cache; setup projects only the documented namespaced rules and Foundry settings. Codex loads Codex Rig's namespaced skills and role cards from its package; direct installation leaves `.codex/config.toml`, personal policy, and global instructions untouched. Codemap-py is the shared structural product, not a runtime dependency that silently changes either host.

```text
AI-Rig/
├── plugins/                  # Claude and Codex package source
│   ├── cc_foundry/            # Claude configuration, agents, rules, hooks
│   ├── cc_oss/                # maintainer and release workflows
│   ├── cc_develop/            # validate-first Python workflows
│   ├── cc_research/           # literature-grounded ML workflows
│   ├── codemap-py/             # shared Python structure skills and indexer
│   └── codex-rig/              # Codex workflows, roles, gates, hooks, scripts
├── .claude/README.md          # Claude host blueprint
├── .codex/README.md           # Codex host blueprint
├── .codex/config.toml         # project-local Codex defaults
└── Makefile                   # deliberate pushed-remote restore entry point
```

For local development without installation, use the host's supported plugin-directory loading against a checkout and treat it as a session-only test path. It does not publish bytes, update the marketplace, or make `make sync-all` consume uncommitted files. Package build/manifest checks and the owning plugin README define the release-grade verification path.

For Claude Code, a checked-out plugin can be loaded for one session with `claude --plugin-dir ./plugins/cc_foundry` (repeat with the target plugin when testing another package). Use the Codex plugin manager for Codex package tests; do not treat a source checkout as a marketplace release.

</details>

## ⬆️ Upgrade and remove

Claude Code:

```bash
claude plugin update foundry@borda-ai-rig
claude plugin update oss@borda-ai-rig
claude plugin update develop@borda-ai-rig
claude plugin update research@borda-ai-rig
claude plugin update codemap-py@borda-ai-rig
```

Run the installed plugin's setup skill again after upgrading Foundry, OSS, Develop, or Research. Uninstall with `claude plugin uninstall <plugin>@borda-ai-rig`. Setup-created rule links and Foundry-managed settings require the manual cleanup documented in each plugin README.

Codex:

`marketplace upgrade` refreshes configured Git marketplace sources. A configured local or other non-Git marketplace keeps its current snapshot and skips this step.

```bash
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
```

Remove with `codex plugin remove <plugin>@borda-ai-rig`. If pre-release Codex Rig shims exist, run `$codex-rig:agent-shims remove` before removing the plugin.

<details>
<summary><strong>Show cleanup and recovery notes</strong></summary>

Claude setup merges settings additively and creates namespaced rule links, so uninstall does not automatically revoke those entries; follow the owning plugin README for exact manual cleanup. Codex Rig shims should be removed while the plugin is still available, then the plugin can be removed. A sync-managed global instruction block must be removed only after checking its authenticated markers and preserving user-owned content.

Claude cleanup normally means reviewing `~/.claude/settings.json`, the matching `~/.claude/rules/<plugin>-<source-name>.md` links, and Foundry's `~/.claude/TEAM_PROTOCOL.md`; remove only entries whose ownership is attributable to this suite and preserve unrelated user settings.

If a cleanup check blocks, do not force-delete files. Reinstall the relevant plugin, start a fresh session, run its doctor or setup check, and repeat the documented guarded cleanup. Marketplace registrations and unrelated plugins remain outside this repository's removal scope.

</details>

## 🔍 Troubleshooting

<details>
<summary><strong>Show first recovery checks</strong></summary>

- Skill names are missing: start a fresh Claude/Codex session or reload plugins, then run the host's install/doctor check.
- Codemap queries report stale or absent: run the explicit scan from the project root, then inspect the returned freshness and coverage block.
- GitHub or Kaggle work stops: authenticate the user-owned CLI and approve the complete owning network command when the runtime requests it.
- `Makefile` installs an older state: remember that it reads the pushed GitHub remote, so commit and push intentionally before syncing.
- Codex Rig shim cleanup is blocked: preserve the diagnostic evidence, reinstall the plugin if needed, and rerun `agent-shims doctor` before an approved `remove`.

</details>

## 📚 Documentation map

| Document                                                      | Audience                                                                                  |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| [Documentation site](https://borda.github.io/AI-Rig/)         | Public overview and rendered plugin pages                                                 |
| [Claude guide](.claude/README.md)                             | Claude installation, plugin selection, setup, architecture, hooks, and runtime boundaries |
| [Codex guide](.codex/README.md)                               | Codex installation, product selection, first workflows, artifacts, and runtime boundaries |
| [Foundry - README](plugins/cc_foundry/README.md)              | Complete Foundry skill, agent, rule, hook, and setup reference                            |
| [OSS - README](plugins/cc_oss/README.md)                      | Complete maintainer workflow and agent reference                                          |
| [Develop - README](plugins/cc_develop/README.md)              | Complete validate-first development reference                                             |
| [Research - README](plugins/cc_research/README.md)            | Complete ML research workflow reference                                                   |
| [Codemap-py - README](plugins/codemap-py/README.md)           | Complete dual-runtime structural-analysis reference                                       |
| [Codex Rig - README](plugins/codex-rig/README.md)             | Complete Codex workflow, role, lifecycle, and calibration reference                       |
| [Bridge_CC-Codex - README](plugins/bridge_cc-codex/README.md) | Complete bidirectional bridge verb, budget, and envelope reference                        |

## 🙏 Contributing

The source of truth lives under `plugins/`, one directory per package. Keep a plugin README synchronized with every public skill, agent, rule, hook, flag, prerequisite, output, or limitation change. Keep benchmark task details in benchmark evidence, not product copy. Run the owning plugin's tests, package validation, Markdown formatter, and link/site checks before proposing a release.

Questions, bug reports, and design proposals are welcome in [GitHub Issues](https://github.com/Borda/AI-Rig/issues).

License: [Apache-2.0](LICENSE).
