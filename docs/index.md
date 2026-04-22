---
description: Five Claude Code plugins — foundry, oss, develop, research, codemap — that enforce specialist agents, validate-first workflows, and calibrated quality gates for Python/ML OSS projects.
---

# 🤖 Borda's AI-Rig

> Five plugins that turn Claude Code into a disciplined development partner

Claude Code is a capable generalist. Serious Python and ML OSS work needs something more: an agent that enforces your SemVer, benchmarks its own accuracy drift, validates a feature with a demo test before writing production code, and reviews a PR through six specialist lenses in one command. This suite exists because that gap is real, and the usual workarounds — copy-pasted prompts, ad-hoc checklists, hoping the model remembers your conventions — do not scale.

Five composable plugins. Each one targets a hard part of the practitioner loop. Together they cover the full cycle from idea to shipped release.

______________________________________________________________________

## 🤔 Why this exists

**Before**: one generalist handles architecture, implementation, documentation, linting, testing, and performance with no boundary enforcement between them. Corrections made in one session evaporate. ML experiments run without a judge gate and silently fail to improve anything. PR reviews miss security issues because no one ran the right checklist. Releases get wrong SemVer because nobody counted the breaking changes.

**After**: each part of the loop has a dedicated skill backed by a calibrated specialist agent. The agents know your conventions, enforce discipline at every gate, and feed corrections back into their own instructions. The feedback loop is closed.

______________________________________________________________________

## 🔌 The five plugins

### 🏭 foundry — base infrastructure

foundry is the foundation the other plugins build on. It packages eight non-overlapping specialist agents — software engineer, QA specialist, performance optimizer, solution architect, doc scribe, linting expert, web explorer, and self mentor — along with the lifecycle tools that keep them reliable over time. Without foundry, every other plugin falls back to a generic agent with a role-description prompt. With it, every dispatch lands on a calibrated specialist.

**Best for:**

- Keeping agent configuration healthy: `/foundry:audit` catches config drift before it becomes a debugging session
- Measuring accuracy: `/foundry:calibrate` benchmarks recall versus stated confidence so you know exactly where agents fall short
- Closing the self-improvement loop: `/foundry:distill` converts accumulated corrections into durable rule updates

[Full documentation →](foundry.md)

______________________________________________________________________

### 🌱 oss — open-source maintainer workflows

oss removes the context-switch tax of maintaining a public project. Four slash-command skills cover the recurring expensive parts of the maintainer loop — issue triage, PR review, resolving feedback, and releasing — backed by two specialist agents that own contributor communication and CI health.

**Best for:**

- Reviewing PRs fast: `/oss:review` runs a Codex pre-pass in ~60 seconds, then fans six specialist agents across architecture, tests, performance, docs, linting, and security in parallel
- Closing review feedback completely: `/oss:resolve` reads live PR comments, deduplicates across sources, resolves conflicts semantically, and tags every fix so you can trace it
- Shipping releases correctly: `oss:shepherd` enforces SemVer, writes the changelog, generates migration guides for breaking changes, and runs a readiness audit before any tag lands

[Full documentation →](oss.md)

______________________________________________________________________

### 🛠️ develop — validate-first implementation

develop enforces a single discipline across the full implementation lifecycle: prove you understand the problem before you touch production code. Six structured workflows — plan, feature, fix, refactor, debug, review — each have explicit validation gates that prevent moving forward on shaky ground.

**Best for:**

- Building features safely: `/develop:feature` requires a failing demo test before any implementation — if you cannot write the test, the feature is underspecified
- Fixing bugs correctly: `/develop:fix` requires a failing regression test that reproduces the bug — you cannot verify the fix until you can reproduce the failure
- Refactoring without breakage: `/develop:refactor` audits coverage and locks in characterization tests before moving a single line

[Full documentation →](develop.md)

______________________________________________________________________

### 🔬 research — structured ML improvement

research turns the messy, iterative cycle of ML improvement into a structured pipeline. You start with evidence from the literature, write a machine-readable experiment spec, get a methodology review before spending any GPU time, and run an automated improvement loop that commits every change atomically and rolls back anything that regresses your target metric.

**Best for:**

- Grounding experiments in the literature: `/research:topic` runs a SOTA search before you write a single line of code
- Catching flawed designs early: `/research:judge` reviews the experiment spec for methodology problems — in minutes, not after 20 GPU-hours
- Automated improvement loops: `/research:run` proposes changes, commits them, measures the metric, and rolls back regressions without you watching

[Full documentation →](research.md)

______________________________________________________________________

### 🗂️ codemap — instant structural answers

codemap scans your Python project once and builds a structural index — an import graph with blast-radius metrics, symbol locations, and a function-level call graph. Every Claude Code session that follows can answer structural questions in a single tool call instead of 20 Glob/Grep passes. On pytorch-lightning (646 modules), plain-arm agents hit the 300-second timeout on three out of eight benchmark tasks; with codemap, zero timeouts.

**Best for:**

- Knowing blast radius before refactoring: `scan-query rdeps mypackage.auth` returns every module that imports `auth` instantly
- Identifying coupling hotspots: `scan-query central --top 5` surfaces the five modules with the widest blast radius
- Eliminating cold-start exploration: agents start every session with full structural context, not a Glob/Grep marathon

[Full documentation →](codemap.md)

______________________________________________________________________

## 📦 Install everything

Run from the directory that **contains** your `Borda-AI-Rig` clone (not from inside it):

```bash
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
claude plugin install codemap@borda-ai-rig
```

One-time setup — run inside Claude Code after installing:

```text
/foundry:init
```

This merges settings, symlinks rule files, and confirms everything is wired correctly. It is idempotent — safe to re-run after any upgrade.

Verify the installation worked:

```text
/foundry:audit setup
```

Zero critical findings means you are ready.

______________________________________________________________________

## 🧭 Where to start

| If you want to…                                 | Start with                                                    |
| ----------------------------------------------- | ------------------------------------------------------------- |
| Set up the agent team and verify configuration  | `/foundry:audit setup` then `/foundry:calibrate routing fast` |
| Review a PR with expert coverage                | `/oss:review`                                                 |
| Build a new feature with a validation gate      | `/develop:feature`                                            |
| Fix a bug and prove the fix                     | `/develop:fix`                                                |
| Run a structured ML experiment                  | `/research:plan` then `/research:judge` then `/research:run`  |
| Cut a release with correct SemVer and changelog | `/oss:release`                                                |
| Understand blast radius before a refactor       | `/codemap:scan` then `scan-query rdeps <module>`              |
| Measure whether agents are drifting in accuracy | `/foundry:calibrate`                                          |

______________________________________________________________________

## 🔗 How the plugins work together

The plugins are designed to be composed. Here are three workflows that span the full suite:

**Research → Develop → OSS: shipping an ML improvement**

1. `/research:topic` — search the literature and identify the promising approach
2. `/research:plan` + `/research:judge` — write the experiment spec and get methodology review
3. `/research:run` — automated improvement loop with metric tracking and auto-rollback
4. `/develop:feature` — implement the winning approach with a demo test gate
5. `/oss:review` — six-agent parallel review before the PR merges
6. `/oss:release` — SemVer-correct release with changelog and migration guide

**Codemap → Develop → OSS: safe refactoring**

1. `/codemap:scan` — build the structural index
2. `scan-query central --top 5` — identify the riskiest modules to touch
3. `/develop:refactor` — lock in characterization tests, then refactor with full blast-radius awareness
4. `/oss:resolve` — close any review feedback in one pass before merge

**Foundry → everything else: keeping the system honest**

1. `/foundry:audit` — weekly config health check; catches drift in hooks, settings, and agent routing
2. `/foundry:calibrate` — benchmarks recall and confidence across all agents; surfaces where quality has drifted
3. `/foundry:distill` — converts the week's corrections into updated agent instructions and rules
4. Repeat — the self-improvement loop runs continuously alongside your normal workflow

______________________________________________________________________

## Frequently Asked Questions

??? question "What is Borda's AI-Rig?"

    Borda's AI-Rig is a suite of five Claude Code plugins — foundry, oss, develop, research, and codemap — that enforce specialist routing, validate-first discipline, and calibrated quality gates across the full Python/ML OSS development lifecycle. Each plugin targets a distinct part of the practitioner loop and is designed to compose with the others.

??? question "How is this different from just prompting Claude?"

    Without AI-Rig, one generalist model handles architecture, implementation, documentation, linting, testing, and performance with no boundary enforcement. Corrections made in one session evaporate. AI-Rig closes the feedback loop: each part of the loop has a dedicated skill backed by a calibrated specialist agent that enforces discipline at every gate and feeds corrections back into its own instructions automatically via `/foundry:distill`.

??? question "What does 'validate-first' mean?"

    Validate-first means you must prove you understand the problem before writing production code. `/develop:feature` requires a failing demo test before implementation. `/develop:fix` requires a failing regression test that reproduces the bug before applying any fix. If you cannot write the test, the problem is underspecified.

??? question "What is calibration bias?"

    Calibration bias measures the gap between an agent's stated confidence and its actual recall on synthetic test problems. A bias of +0.15 means the agent says it is 90% confident but is only right 75% of the time. `/foundry:calibrate` benchmarks this for every agent and surfaces agents that are systematically overconfident or underconfident.

??? question "What is a blast-radius score?"

    Blast-radius score (from the codemap plugin) measures how many modules would be affected if a given module changed. `scan-query rdeps mypackage.auth` returns every module that imports `auth`. High blast-radius modules are the riskiest to refactor — codemap surfaces these before you touch anything.

??? question "Does this work without all five plugins?"

    Yes. Each plugin installs independently. foundry is strongly recommended as a base since it provides the specialist agents the other plugins dispatch to. Without foundry, the other plugins fall back to a generic agent with a role-description prompt.

??? question "How do I know if my agent configuration has drifted?"

    Run `/foundry:audit` — it runs 29 checks across hooks, settings, agent routing, and rule files. Zero critical findings means the configuration is healthy. Run it weekly or after any Claude Code update.

______________________________________________________________________

## 🤝 Contributing

The agent and skill source lives in `plugins/` — one directory per plugin, each with its own `skills/`, `agents/`, and `rules/` subdirectories. If you find a gap in agent behavior, the right fix is usually a targeted edit to the agent's instruction file, not a new skill. If you find a workflow that should be a skill, open an issue describing the before/after and the command you wish existed.

The `foundry` plugin's self-improvement loop (`/foundry:distill`) is specifically designed to absorb corrections: run it after a session where you caught the agent doing something wrong and it will propose instruction updates automatically.

We are glad you are here.
