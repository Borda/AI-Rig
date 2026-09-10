---
description: Evidence-first Claude Code and Codex plugins for Python, ML, and open-source maintenance, with complete capability inventories and honest runtime boundaries.
---

# Borda's AI-Rig

AI-Rig packages recurring Python, ML, and open-source work as explicit Claude Code and Codex workflows. It helps teams establish scope before editing, preserve evidence across specialist handoffs, and review the result against measurable gates.

The plugins are used daily in active development and maintenance, which gives their workflow contracts and recovery paths sustained operational exercise. Foundry and Codex Rig calibration use synthetic instruction cases; operational use and synthetic calibration are complementary strengths, not guarantees that every model output is correct.

Seven independently installable packages serve two runtimes: six marketplace plugins for Claude Code, three for Codex, with Codemap-py and bridge_CC-Codex shared by both. Install the smallest set that solves the current task.

## Choose a package

| Need                                                                           | Package                                  | What it contributes                                                                                                   |
| ------------------------------------------------------------------------------ | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Maintain Claude configuration, routing, reusable guidance, and session lessons | [🏭 Foundry](cc_foundry.md)              | 11 skills, 10 specialist agents, 13 rules, and event-driven hooks                                                     |
| Triage issues, review PRs, resolve feedback, and assess release readiness      | [🌱 OSS](cc_oss.md)                      | 5 maintainer skills and 4 agents, with human-owned public actions                                                     |
| Plan, build, fix, refactor, debug, or review Python changes                    | [🛠️ Develop](cc_develop.md)              | 7 validate-first skills with explicit reproduction and acceptance gates                                               |
| Ground and run reviewable ML experiments                                       | [🔬 Research](cc_research.md)            | 10 skills and 2 agents covering literature, design, execution, verification, ablation, retrospective, and Kaggle work |
| Answer structural Python questions from a local index                          | [🗂️ Codemap-py](codemap-py.md)           | 6 skills shared by Claude Code and Codex                                                                              |
| Run evidence-first engineering workflows in Codex                              | [🤖 Codex Rig](codex-rig.md)             | 13 workflow skills, 1 lifecycle manager, and 15 role cards                                                            |
| Hand bounded implement, advise, and review calls between Claude Code and Codex | [🌉 bridge_CC-Codex](bridge_cc-codex.md) | 3 bridge verbs in both directions with explicit models, budgets, compact envelopes, and recursion safety              |

## Complete skill inventory

| Package            | Shipped skills                                                                                                                                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 🏭 Foundry         | `setup`, `audit`, `calibrate`, `manage`, `brainstorm`, `investigate`, `profile`, `distill`, `session`, `create`, `humanizer`                                              |
| 🌱 OSS             | `analyse`, `review`, `resolve`, `release`, `setup`                                                                                                                        |
| 🛠️ Develop         | `plan`, `feature`, `fix`, `refactor`, `debug`, `review`, `setup`                                                                                                          |
| 🔬 Research        | `topic`, `plan`, `judge`, `run`, `sweep`, `verify`, `fortify`, `retro`, `kaggle`, `setup`                                                                                 |
| 🗂️ Codemap-py      | `scan-codebase`, `query-code`, `test-impact`, `rename-refs`, `integration`, `debrief-coding`                                                                              |
| 🤖 Codex Rig       | `assess`, `audit`, `calibrate`, `code-remediate`, `code-review`, `implement`, `investigate`, `kaggle`, `manage`, `optimize`, `release`, `research`, `sync`, `agent-shims` |
| 🌉 bridge_CC-Codex | `implement`, `advise`, `review`, `setup`, plus Claude-side detached-job `status`, `result`, `cancel`                                                                      |

The package pages document every skill's arguments, prerequisites, outputs, stopping conditions, fallbacks, and known boundaries. The repository's [Claude guide](https://github.com/Borda/AI-Rig/blob/main/.claude/README.md) also inventories all Claude agents, rules, and hooks; the [Codex Rig role-card reference](https://github.com/Borda/AI-Rig/blob/main/plugins/codex-rig/roles/README.md) inventories all Codex specialist roles.

## Install for Claude Code

Use a current Claude Code release with plugin support. Register the marketplace once, then install only the packages you need:

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
claude plugin install codemap-py@borda-ai-rig
claude plugin install bridge@borda-ai-rig
```

Start a new session or run `/reload-plugins`. Then run setup for each installed plugin that ships rules:

```text
/foundry:setup
/oss:setup
/develop:setup
/research:setup
```

Codemap-py needs no setup skill. Build its first project index with `/codemap-py:scan-codebase`. bridge_CC-Codex ships no rules either; its `/bridge:setup` is a free static check of the local `codex` and `claude` CLIs.

Useful first commands:

```text
/foundry:audit setup
/develop:plan "describe the next change"
/oss:analyse 42
/research:plan "state a measurable ML goal"
/codemap-py:query-code rdeps mypackage.auth
```

## Install for Codex

Use a current Codex release with plugin support:

```bash
codex plugin marketplace add Borda/AI-Rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
codex plugin add bridge@borda-ai-rig
codex plugin list
```

Codemap-py is optional. Install it when Python structure is unresolved; skip it when the edit surface is already known. To pin reproducible marketplace bytes, add the marketplace with `--ref <release-tag>`.

Try the investigation-to-review loop:

```text
$codex-rig:agent-shims doctor
$codex-rig:investigate find the root cause of the failing test
$codex-rig:implement apply the verified fix
$codex-rig:code-review review the current diff
```

Quote `$codex-rig:...` and `$codemap-py:...` when passing an invocation through a shell so `$` is not expanded.

## What the packages solve

### 🏭 Foundry

Foundry makes Claude configuration and specialist ownership inspectable. Audit and calibration surface stale references, weak routing, and confidence gaps; management and distillation turn reviewed corrections into deliberate changes. It does not guarantee that a model follows every instruction, and calibration is a synthetic instruction-quality signal rather than production correctness. [Read the full Foundry reference.](cc_foundry.md)

### 🌱 OSS

OSS keeps maintainer work tied to current GitHub and repository evidence. It can prepare contributor-facing text and release artifacts, but the maintainer reviews and performs public actions. The release skill assesses readiness; it does not edit versions, create tags, push, or publish. [Read the full OSS reference.](cc_oss.md)

### 🛠️ Develop

Develop requires the proof appropriate to the change: an acceptance demo for a feature, a reproduction for a fix, characterization for a refactor, or root-cause evidence for an unknown failure. It targets Python 3.10+ projects with executable verification and is not a general non-Python migration framework. [Read the full Develop reference.](cc_develop.md)

### 🔬 Research

Research keeps literature, experiment intent, methodology review, measured changes, and retrospective evidence in the project. It cannot guarantee improvement or repair weak data, metrics, splits, baselines, compute, or credentials. Optional Colab, Docker, Codex, Kaggle, Foundry, and Codemap routes have their own preconditions and fallbacks. [Read the full Research reference.](cc_research.md)

### 🗂️ Codemap-py

Codemap-py accelerates import, symbol, call, test-impact, and rename questions from a local static Python index. Static AST evidence can miss dynamic dispatch, callbacks, string imports, inheritance, generated code, and external consumers; it never substitutes for runtime or test evidence. [Read the full Codemap-py reference.](codemap-py.md)

### 🤖 Codex Rig

Codex Rig gives native Codex workflows a shared input, gate, artifact, and confidence contract. Role cards can guide runtime blank agents when that route exists; otherwise the parent performs a disclosed inline pass. Direct plugin installation does not claim persistent named-agent registration, silently enable network access, mutate remote GitHub state, or copy repository instructions into a user's Codex home; the separate source-checkout `make sync-*` flow can deliberately project managed instructions when its Codex scope is selected. [Read the full Codex Rig reference.](codex-rig.md)

### 🌉 bridge_CC-Codex

bridge_CC-Codex lets each host hand the other a bounded implement, advise, or review request with an explicit model, effort, and wall-clock budget, returning a compact validated envelope instead of a raw transcript. It does not install or authenticate either CLI, retry write-capable work automatically, or allow recursive cross-host loops; provider cost, credentials, and permissions stay operator-owned. [Read the full bridge_CC-Codex reference.](bridge_cc-codex.md)

## Practical sequences

Use the shortest sequence that closes the real uncertainty.

Bug report to verified fix in Claude Code:

```text
/oss:analyse 42
/develop:fix 42
/develop:review
```

ML idea to reviewable result:

```text
/research:topic "candidate method"
/research:plan "state the measurable goal"
/research:judge program.md
/research:run program.md
/research:retro
```

Native Codex investigation and remediation:

```text
$codex-rig:investigate diagnose the failure
$codex-rig:implement apply the verified fix
$codex-rig:code-review review the resulting diff
$codex-rig:code-remediate close selected findings
```

## Current boundaries and possible future work

- AI-Rig makes process, evidence, and uncertainty visible; it does not make generated code or model conclusions correct by construction.
- Every package installs independently. Optional integrations add depth and must degrade or stop explicitly when absent.
- Network-backed workflows depend on user-managed credentials and runtime approval. The plugins do not install credentials or silently broaden access.
- Public GitHub actions and release publication remain human-owned.
- Setup-created rule links and Foundry-managed settings survive plugin uninstall and require the manual cleanup documented by each plugin.
- Core workflows target Windows, macOS, and Linux, while named optional tools and a few cleanup or timeout paths have narrower documented contracts.
- Broader language support, richer dynamic analysis, automatic uninstall cleanup, and deeper native runtime integration are possible future directions, not roadmap promises.

## Update and remove

Use `claude plugin update <plugin>@borda-ai-rig` and rerun that plugin's setup skill after updating Foundry, OSS, Develop, or Research. Use `claude plugin uninstall <plugin>@borda-ai-rig` to uninstall, then follow the package's manual cleanup instructions if you also want setup-created files removed.

For Codex, use `codex plugin marketplace upgrade borda-ai-rig` to refresh a configured Git marketplace snapshot, then `codex plugin add <plugin>@borda-ai-rig` to install the refreshed package. A configured local or other non-Git marketplace keeps its current snapshot and skips the refresh. Use `codex plugin remove <plugin>@borda-ai-rig` to remove a plugin. If pre-release Codex Rig shims exist, run `$codex-rig:agent-shims remove` before removing Codex Rig.

## Contributing

The source of truth lives under `plugins/`, one package per directory. A public skill, agent, role, rule, hook, flag, prerequisite, output, or limitation change is incomplete until the owning README and relevant host/site guides agree. Keep benchmark-specific task evidence out of product copy, and run the owning tests, package checks, Markdown formatter, link checks, and site build before proposing a release.

Questions, bug reports, and design proposals are welcome in [GitHub Issues](https://github.com/Borda/AI-Rig/issues).
