# 🏠 Borda's AI-Rig

![Borda's AI-Rig banner](docs/assets/images/banner.webp)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE) [![Claude Code](https://img.shields.io/badge/Claude_Code-plugins-D97757)](https://code.claude.com/docs/en/discover-plugins) [![Codex](https://img.shields.io/badge/Codex-plugins-10A37F)](https://developers.openai.com/codex/cli/reference) [![OpenCode](https://img.shields.io/badge/OpenCode-plugin-111827)](https://opencode.ai) [![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](pyproject.toml) ![Platforms](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey)

[![CI manifests](https://github.com/Borda/AI-Rig/actions/workflows/ci-manifests.yml/badge.svg?event=push)](https://github.com/Borda/AI-Rig/actions/workflows/ci-manifests.yml) [![CI executables](https://github.com/Borda/AI-Rig/actions/workflows/ci-tests.yml/badge.svg?event=push)](https://github.com/Borda/AI-Rig/actions/workflows/ci-tests.yml) [![CI harness](https://github.com/Borda/AI-Rig/actions/workflows/ci-harness.yml/badge.svg)](https://github.com/Borda/AI-Rig/actions/workflows/ci-harness.yml) [![codecov](https://codecov.io/gh/Borda/AI-Rig/graph/badge.svg?token=NTJKY4o4es)](https://codecov.io/gh/Borda/AI-Rig) [![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Borda/AI-Rig/main.svg)](https://results.pre-commit.ci/latest/github/Borda/AI-Rig/main)

Practical agent workflows for Python, ML, and open-source maintenance. AI-Rig turns recurring work—scoping a change, reproducing a bug, reviewing a pull request, running an experiment, or checking release readiness—into explicit workflows with specialist ownership, evidence gates, and reviewable artifacts.

Eight packages serve three runtimes: six marketplace plugins for Claude Code, three for Codex, and one npm plugin for OpenCode, with `codemap-py` and `bridge_CC-Codex` shared by two hosts each.

Design principles:

- **Simple and reliable:** favor workflows that complete the user's goal, recover from routine problems, and reuse valid evidence.
- **No silent blockers:** routine incomplete handoffs and self-created blockers are defects to fix. Genuine permission, prerequisite, and user-decision boundaries stay explicit, with safety and verification intact.
- **Root cause, not symptom:** establish the cause with evidence and a falsification check before changing behavior. If it is unproven, keep investigating.
- **Verified fixes:** a fix is complete only after the original failure and affected sibling workflows are verified.

<details>
<summary><strong>Contents</strong></summary>

- [Start here](#-start-here)
- [What this setup enables](#-what-this-setup-enables)
- [Complete capability map](#-complete-capability-map)
- [How it fits together](#-how-it-fits-together)
- [Install for Claude Code](#-install-for-claude-code)
- [Install for Codex](#-install-for-codex)
- [Install for OpenCode](#-install-for-opencode)
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
| Run evidence-first workflows, gates, and Git guardrails in OpenCode     | [⚡ `OpenCode Rig`](plugins/opencode-rig/README.md)       | 11 workflow skills, 7 evidence tools, 7 role agents, an adversarial loop, and an npm-installable TypeScript package       |
| Hand bounded implement, advise, and review calls between the two hosts  | [🌉 `bridge_CC-Codex`](plugins/bridge_cc-codex/README.md) | 3 bridge verbs in both directions with explicit models, effort, budgets, compact envelopes, and recursion safety          |

If you use Claude Code, read the [Claude guide](.claude/README.md). If you use Codex, read the [Codex guide](.codex/README.md). OpenCode installs the npm package directly; its [README](plugins/opencode-rig/README.md) is the host guide.

## 🎯 What this setup enables

<details>
<summary><strong>Show the evidence-first value of the full suite</strong></summary>

- A bounded route from uncertainty to evidence: Foundry audits configuration and routing, Develop gates Python changes, OSS organizes maintainer work, Research keeps experiments reviewable, Codemap-py supplies static structure, and Codex Rig and OpenCode Rig provide the host-native workflow layer for Codex and OpenCode.
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
| ⚡ OpenCode Rig    | `/rig-plan`, `/rig-investigate`, `/rig-research`, `/rig-implement`, `/rig-review`, `/rig-remediate`, `/rig-challenge`, `/rig-optimize`, `/rig-distill`, `/rig-release`, `/rig-doctor`                                                                                                                                               |
| 🌉 bridge_CC-Codex | `implement`, `advise`, `review`, `setup`, plus Claude-side detached-job `status`, `result`, `cancel`; namespaced as `/bridge:...` in Claude Code and `$bridge:...` in Codex                                                                                                                                                         |

</details>

## 🏗️ How it fits together

| Runtime     | Packages                                                        | What you get                                                        |
| ----------- | --------------------------------------------------------------- | ------------------------------------------------------------------- |
| Claude Code | `foundry`, `oss`, `develop`, `research`, `codemap-py`, `bridge` | Specialist agents, skills, rules, and hooks loaded from marketplace |
| Codex       | `codex-rig`, `codemap-py`, `bridge`                             | Namespaced skills and role cards                                    |
| OpenCode    | `opencode-rig`                                                  | npm package: tools, skills, and role agents                         |

- `plugins/` is the source of truth; `.claude/` and `.codex/` are host guides and configuration.
- Specialist roles are requested by name or auto-selected. Claude agents dispatch as `<plugin>:<agent>`; the [Claude guide](.claude/README.md) and [Codex guide](.codex/README.md) list every agent, role, rule, hook, and skill.
- A role card, task name, artifact, or green gate is not proof that a model followed it or that the change is correct. Human review stays the acceptance boundary for consequential work.

### Specialist roles

Each role links to its own definition file. Claude agents dispatch as `<plugin>:<agent>`; Codex role cards are injected into blank agents or run as a disclosed inline pass.

| Role                          | Plugin      | Owns                                                                | Definition                                                                                                                  |
| ----------------------------- | ----------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `sw-engineer`                 | 🏭 foundry  | Implementation, refactors, SOLID, type safety; also authors hook JS | [agent](plugins/cc_foundry/agents/sw-engineer.md) · [Codex card](plugins/codex-rig/roles/sw-engineer/ROLE.md)               |
| `solution-architect`          | 🏭 foundry  | ADRs, API surface, migration plans — specs only                     | [agent](plugins/cc_foundry/agents/solution-architect.md) · [Codex card](plugins/codex-rig/roles/solution-architect/ROLE.md) |
| `qa-specialist`               | 🏭 foundry  | pytest, hypothesis, mutation testing; OWASP coverage                | [agent](plugins/cc_foundry/agents/qa-specialist.md) · [Codex card](plugins/codex-rig/roles/qa-specialist/ROLE.md)           |
| `linting-expert`              | 🏭 foundry  | ruff, mypy, pre-commit, type annotations                            | [agent](plugins/cc_foundry/agents/linting-expert.md) · [Codex card](plugins/codex-rig/roles/linting-expert/ROLE.md)         |
| `perf-optimizer` / `squeezer` | 🏭 foundry  | Profile-first CPU/GPU/memory/I/O work                               | [agent](plugins/cc_foundry/agents/perf-optimizer.md) · [Codex card](plugins/codex-rig/roles/squeezer/ROLE.md)               |
| `doc-scribe`                  | 🏭 foundry  | Docstrings, API references, README                                  | [agent](plugins/cc_foundry/agents/doc-scribe.md) · [Codex card](plugins/codex-rig/roles/doc-scribe/ROLE.md)                 |
| `web-explorer`                | 🏭 foundry  | External docs, release notes, version lookups                       | [agent](plugins/cc_foundry/agents/web-explorer.md) · [Codex card](plugins/codex-rig/roles/web-explorer/ROLE.md)             |
| `curator`                     | 🏭 foundry  | Config quality: verbosity, duplication, roster overlap              | [agent](plugins/cc_foundry/agents/curator.md) · [Codex card](plugins/codex-rig/roles/curator/ROLE.md)                       |
| `challenger`                  | 🏭 foundry  | Adversarial review; claims unproven until evidence                  | [agent](plugins/cc_foundry/agents/challenger.md) · [Codex card](plugins/codex-rig/roles/challenger/ROLE.md)                 |
| `creator`                     | 🏭 foundry  | Blog posts, slide decks, threads from an approved outline           | [agent](plugins/cc_foundry/agents/creator.md)                                                                               |
| `shepherd` / `oss-shepherd`   | 🌱 oss      | Contributor communication, triage, SemVer, releases                 | [agent](plugins/cc_oss/agents/shepherd.md) · [Codex card](plugins/codex-rig/roles/oss-shepherd/ROLE.md)                     |
| `cicd-steward`                | 🌱 oss      | GitHub Actions health, matrices, caching, SHA pinning               | [agent](plugins/cc_oss/agents/cicd-steward.md) · [Codex card](plugins/codex-rig/roles/cicd-steward/ROLE.md)                 |
| `gh-scraper`                  | 🌱 oss      | Internal: bulk GitHub fetch for vitality scoring                    | [agent](plugins/cc_oss/agents/gh-scraper.md)                                                                                |
| `repo-warden`                 | 🌱 oss      | Internal: scores vitality axes from fetched data                    | [agent](plugins/cc_oss/agents/repo-warden.md)                                                                               |
| `scientist`                   | 🔬 research | Paper analysis, hypotheses, experiment design                       | [agent](plugins/cc_research/agents/scientist.md) · [Codex card](plugins/codex-rig/roles/scientist/ROLE.md)                  |
| `data-steward`                | 🔬 research | Dataset versioning, split validation, leakage detection             | [agent](plugins/cc_research/agents/data-steward.md) · [Codex card](plugins/codex-rig/roles/data-steward/ROLE.md)            |
| `delegation-lead`             | Codex only  | Routes disjoint workstreams, parent handover gate                   | [Codex card](plugins/codex-rig/roles/delegation-lead/ROLE.md)                                                               |
| `security-auditor`            | Codex only  | Read-only trust-boundary, secrets, supply-chain audit               | [Codex card](plugins/codex-rig/roles/security-auditor/ROLE.md)                                                              |

16 Claude agents, 15 Codex role cards. On the Claude side, security review is folded into `foundry:qa-specialist`. The [Codex role index](plugins/codex-rig/roles/README.md) maps each card to its ownership and model tier.

### Rules

Foundry ships the shared rules; each companion ships its own `quality-gates` rule. `/foundry:setup`, `/oss:setup`, `/develop:setup` and `/research:setup` link them into `~/.claude/rules/`.

- Foundry: [`plugins/cc_foundry/rules/`](plugins/cc_foundry/rules/) — e.g. [`quality-gates.md`](plugins/cc_foundry/rules/quality-gates.md), [`git-commit.md`](plugins/cc_foundry/rules/git-commit.md), [`debugging.md`](plugins/cc_foundry/rules/debugging.md), [`python-code.md`](plugins/cc_foundry/rules/python-code.md).
- Companions: [oss](plugins/cc_oss/rules/), [develop](plugins/cc_develop/rules/), [research](plugins/cc_research/rules/).
- bridge_CC-Codex ships its own policy files in [`plugins/bridge_cc-codex/rules/`](plugins/bridge_cc-codex/rules/).

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

## 📦 Install for OpenCode

Prerequisite: OpenCode `2.1.0` or newer and Node.js `20` or newer. OpenCode Rig is an npm package with no Python runtime.

Published package, when available:

```bash
opencode plugin add opencode-rig@0.2.0
npx opencode-rig@0.2.0 init
```

Before the first npm release, install the package straight from this repository:

```bash
opencode plugin add 'github:Borda/AI-Rig#main::path:plugins/opencode-rig'
node plugins/opencode-rig/bin/opencode-rig.mjs init
```

This repository already does the first step: [`opencode.jsonc`](opencode.jsonc) registers the same Git specifier for the project. `make sync-opencode` registers it in the global config and installs the role agents, following the same pushed-remote rule as the other sync targets.

`opencode plugin add` installs the package and adds it to the OpenCode configuration. `init` writes the seven `rig-*` role agents that a plugin package cannot register: `~/.config/opencode/agents/` by default, or `.opencode/agents/` with `--project`. `opencode plugin add` does not link the package's CLI on `PATH`, so run `init` with `npx` or the checked-out script. Plain local commits ask until you record an explicit grant with `rig_git_grant`; push and every other remote mutation keep asking.

Start a fresh session, then try:

```text
/rig-research compare the two caching designs against the official docs
/rig-investigate find the root cause of the failing test
/rig-implement apply the verified fix
/rig-review review the current diff
/rig-challenge challenge the change and iterate to convergence
/rig-optimize cut the indexing benchmark median by 20%
/rig-distill which failures kept repeating across the last runs?
```

Ignore the run artifact directory in projects that use the plugin; it holds scope digests, gate logs, findings, and reports:

```bash
printf '\n.opencode-rig/\n' >> .gitignore
```

`opencode-rig print-config` prints the recommended permission rules, and `opencode-rig doctor` reports install and environment health.

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

### ⚡ OpenCode Rig: keep OpenCode delivery evidence-first

OpenCode Rig ports the suite's evidence discipline to OpenCode without the DAG, freeze, and approval machinery. Each run binds its scope bytes, records real gate exit codes, requires a named independent reviewer, and fails closed on source drift. Plan, investigate, research, implement, review, remediate, challenge, optimize, distill, release-readiness, and doctor workflows share those contracts.

The package is TypeScript distributed through npm.

- It cannot register agents by itself: `opencode-rig init` installs seven hash-stamped role agents and refuses to overwrite one you edited.
- `rig-scientist` grades research claims against their sources; `rig-optimizer` recomputes benchmark rounds.
- `rig-distill` proposes assets from recorded runs without installing them; `rig-release` drafts notes, migration steps, and contributor replies without posting.
- The permission hook escalates remote mutations, destructive operations, history rewrites, and ungranted local commits. It never downgrades a configured `ask` and never publishes anything.

[Read the OpenCode Rig reference.](plugins/opencode-rig/README.md)

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

In OpenCode:

```text
/rig-investigate diagnose the reported failure
/rig-implement apply the verified fix
/rig-review review the resulting diff
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

The packages remain independently installable; their boundaries are intentional when the full suite is present.

**Ownership**

- Foundry: Claude configuration, reusable rules, hooks, and specialist agents.
- Develop: validate-first Python changes.
- OSS: GitHub triage, review, and release preparation.
- Research: literature-grounded ML iteration.
- Codemap-py: unresolved Python structure for Claude and Codex.
- Codex Rig and OpenCode Rig: the host-native workflow, gate, and artifact layer for Codex and OpenCode.

**Companions**

- Claude workflows use Foundry specialists when available and follow each plugin's documented fallback or stop condition when a companion is absent.
- Develop, Research, and Codex Rig can use Codemap-py as optional structural context. A missing or stale index is reported; it never becomes a claim that no callers or tests exist.
- Network-backed paths keep authentication and approval user-owned.

**Which workflow for which job**

- New change: Develop, or Codex Rig `implement`, once scope is understood.
- GitHub issue or PR: OSS, or Codex Rig review/remediation.
- ML hypothesis: Research `topic` → `plan` → `judge` → `run` → `retro`.
- Unresolved imports, callers, coupling, or test impact: add Codemap-py only while those remain open.

**Claude–Codex handoffs** (with the optional plugins installed)

- Claude delegates a bounded mechanical task or pre-review to Codex, then inspects the local diff.
- Codex independently reviews Claude's staged or local work and leaves an artifact for the next remediation step.
- Both are local, evidence-backed; neither grants remote mutation or turns one model's result into proof.

**Daily maintainer pass**

1. `/oss:analyse vitality` or Codex Rig `assess`.
2. Review selected PRs with `/oss:review` or `$codex-rig:code-review`.
3. Reproduce and fix one high-value issue with Develop, or `investigate` → `implement`.
4. Assess readiness with `/oss:release` or `release`.

Keep scope and evidence artifacts for the next session; this is not an unattended campaign.

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

External add-ons are not hidden installation prerequisites. The owning plugin README is authoritative for flags, supported versions, and recovery.

RTK is an output-compression convenience for supported shell commands, not a correctness gate or a replacement for Claude's native file tools. Codex's repository policy routes eligible commands explicitly because the current Codex path does not rewrite commands in place.

</details>

## 📊 Artifacts and evidence

<details>
<summary><strong>Show where workflows write state and how to read it</strong></summary>

| Package      | Where it writes                                                                                                 |
| ------------ | --------------------------------------------------------------------------------------------------------------- |
| Codex Rig    | `.reports/codex/<skill>/<timestamp>/`: validated `result.json`, gate records/logs, skill-specific evidence      |
| Research     | Run state in `.experiments/`; final reports in `.reports/research/`                                             |
| Codemap-py   | Local index in `.cache/codemap/`; query results carry freshness, coverage, truncation, and blind-spot metadata  |
| OpenCode Rig | `.opencode-rig/runs/<skill>/<run-id>/`: `run.json`, gate receipts/logs, findings inventory, loop ledger, report |

Artifacts are reviewable evidence, not authority. Read the commands, source, tests, gate status, confidence, and unresolved limits before accepting consequential work. Generated state is project-local; follow the owning plugin's retention and cleanup guidance before deleting or automating it.

</details>

## 🧭 Current boundaries

- AI-Rig makes process and evidence visible; it does not make model output correct by construction. Review diffs, commands, reports, and confidence limits before accepting consequential work.
- Every package is independently installable. Cross-plugin features are capability-gated and must degrade or stop explicitly when the optional dependency is absent.
- Network-backed workflows depend on user-managed authentication and runtime approval. The plugins do not install credentials or silently broaden network access.
- Release workflows prepare and assess local artifacts. Remote publication, pushes, comments, merges, tags, and package uploads remain human-owned unless a future workflow explicitly documents a different contract.
- Generated reports, plans, and experiment state are project-local. Read the owning plugin's artifact and cleanup sections before automating retention.
- Platform support is capability-specific. Core workflows target Windows, macOS, and Linux; Codex Rig's authenticated legacy-shim cleanup remains POSIX-only, and Codemap's scan timeout flag uses a Unix-only signal where available.
- OpenCode Rig is an npm package for OpenCode `2.1.0+` on Node `20+`. It registers tools, skills, and one permission hook; role agents exist only after `opencode-rig init`, and its Git grant covers plain local commits only.
- Possible future work is described as an opportunity, not a roadmap promise. The shipped source and tests define the current contract.

## 🏗️ Repository checkout and synchronization

Direct marketplace installation is the normal public path. A source checkout adds maintainer tooling and the deliberate `Makefile` restore flow. Checkout sync requires [RTK](https://github.com/rtk-ai/rtk) on `PATH` (check with `make preflight-sync`, which changes nothing); ordinary plugin use does not.

```bash
make sync-all         # Claude + Codex + OpenCode, plus standing Claude consent when operator proof exists (default target)
make sync-claude      # Claude scope only
make sync-codex       # Codex scope only
make sync-opencode    # OpenCode scope only: GitHub registration + role agents
make rig-check        # OpenCode Rig typecheck, formatting, tests, version, and package checks
```

> The `Makefile` installs from the pushed GitHub remote, not uncommitted local files. Commit and push first if you want a checkout change to become installable; never use sync as a preview of a dirty worktree.

Restart open Claude Code sessions after a sync: a session keeps the plugin versions it loaded at start.

Consent handling, sync internals, repository layout, and local development without installing: [DEVELOPMENT.md](DEVELOPMENT.md).

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

OpenCode:

```bash
opencode plugin update opencode-rig
opencode-rig init --upgrade
```

Update the package, then refresh the role agents. `init --upgrade` replaces stale or locally edited stamped agents; without it, `init` and `check` stop on a conflict instead of overwriting your edits. Remove with:

```bash
opencode-rig remove
opencode plugin remove opencode-rig
```

<details>
<summary><strong>Show cleanup and recovery notes</strong></summary>

- **Claude:** setup merges settings additively and creates namespaced rule links, so uninstall does not revoke them. Review `~/.claude/settings.json`, the matching `~/.claude/rules/<plugin>-<source-name>.md` links, and Foundry's `~/.claude/TEAM_PROTOCOL.md`; remove only entries attributable to this suite and keep unrelated user settings. Each plugin README has the exact steps.
- **Codex:** remove Codex Rig shims while the plugin is still available, then remove the plugin. Remove a sync-managed global instruction block only after checking its authenticated markers and preserving user-owned content.
- **OpenCode:** `remove` deletes only stamped agent files; `.opencode-rig/` run artifacts stay until you delete them.
- **Blocked cleanup:** do not force-delete. Reinstall the plugin, start a fresh session, run its doctor or setup check, and repeat the guarded cleanup. Marketplace registrations and unrelated plugins are outside this repository's removal scope.

</details>

## 🔍 Troubleshooting

<details>
<summary><strong>Show first recovery checks</strong></summary>

- Skill names are missing: start a fresh Claude/Codex session or reload plugins, then run the host's install/doctor check.
- Codemap queries report stale or absent: run the explicit scan from the project root, then inspect the returned freshness and coverage block.
- GitHub or Kaggle work stops: authenticate the user-owned CLI and approve the complete owning network command when the runtime requests it.
- `Makefile` installs an older state: remember that it reads the pushed GitHub remote, so commit and push intentionally before syncing.
- Codex Rig shim cleanup is blocked: preserve the diagnostic evidence, reinstall the plugin if needed, and rerun `agent-shims doctor` before an approved `remove`.
- OpenCode Rig tools or skills are missing: rerun `opencode-rig check` to compare installed role agents with the package, then `opencode-rig doctor` for version, grant, ignore, and gate state.

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
| [OpenCode Rig - README](plugins/opencode-rig/README.md)       | Complete OpenCode install, tool, skill, agent, guardrail, and artifact reference          |
| [Bridge_CC-Codex - README](plugins/bridge_cc-codex/README.md) | Complete bidirectional bridge verb, budget, and envelope reference                        |

## 🙏 Contributing

- Contributor and agent technical detail: [DEVELOPMENT.md](DEVELOPMENT.md).
- Source of truth is `plugins/`, one directory per package.
- Keep each plugin README synchronized with every public skill, agent, rule, hook, flag, prerequisite, output, or limitation change.
- Keep benchmark task details in benchmark evidence, not product copy.
- Before proposing a release, run the owning plugin's tests, package validation, Markdown formatter, and link/site checks.
- `plugins/opencode-rig/` runs `npm run check` (typecheck, formatting, Vitest, version sources, tarball contract) instead of pytest.

Questions, bug reports, and design proposals are welcome in [GitHub Issues](https://github.com/Borda/AI-Rig/issues).

License: [Apache-2.0](LICENSE).
