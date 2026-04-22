# 🏠 Borda's AI-Rig

Personal AI coding assistant configuration for Python/ML OSS development. Version-controlled, opinionated, continuously improved.

<details>
<summary><strong>Contents</strong></summary>

- [🎯 Why](#-why)
- [💡 Design Principles](#-design-principles)
- [⚡ Quick Start](#-quick-start)
- [🔁 Daily OSS Workflow](#-daily-oss-workflow)
- [📦 What's Here](#-whats-here)
- [🧩 Agents](#-agents)
- [🤖 Claude Code](#-claude-code)
- [🤖 Codex CLI](#-codex-cli)
- [🤝 Claude + Codex Integration](#-claude--codex-integration)
- [🛠 Recommended Add-ons](#-recommended-add-ons)
- [🔌 Plugin Management](#-plugin-management)

</details>

## 🎯 Why

Managing AI coding workflows for Python/ML OSS is complex — you need domain-aware agents, not generic chat. This config packages 12 specialist agents and 20+ slash-command skill workflows across four focused plugins, in a version-controlled, continuously benchmarked setup optimized for:

- Python/ML OSS libraries requiring SemVer discipline and deprecation cycles
- ML training and inference codebases needing GPU profiling and data pipeline validation
- Multi-contributor projects with CI/CD, pre-commit hooks, and automated releases

> [!NOTE]
>
> **What this adds over vanilla Claude Code:** With defaults, Claude reviews code as a generalist. With this config, it reviews as 6 specialists in parallel, with a Codex pre-pass for unbiased coverage, file-based handoff to prevent context flooding, automatic lint-on-save, and token compression via RTK — all orchestrated by slash commands that chain into complete workflows.

## 💡 Design Principles

- **Agents are roles, skills are workflows** — agents carry domain expertise, skills orchestrate multi-step processes
- **No duplication** — agents reference each other instead of repeating content
- **Profile-first, measure-last** — performance skills always bracket changes with measurements
- **Link integrity** — never cite a URL without fetching it first (enforced in all research agents)
- **Python 3.10+ baseline** — all configs target py310 minimum (3.9 EOL was Oct 2025)
- **Modern toolchain** — uv, ruff, mypy, pytest, GitHub Actions with trusted publishing

## ⚡ Quick Start

```bash
# Install Claude Code
npm install -g @anthropic-ai/claude-code

# 1. Clone (run from the directory that will CONTAIN the clone)
git clone https://github.com/Borda/AI-Rig Borda-AI-Rig

# 2. Register as a local marketplace
claude plugin marketplace add ./Borda-AI-Rig

# 3. Install all four plugins
claude plugin install foundry@borda-ai-rig   # base agents + audit, manage, calibrate, brainstorm, …
claude plugin install oss@borda-ai-rig       # OSS workflow: analyse, review, resolve, release
claude plugin install develop@borda-ai-rig   # development: feature, fix, refactor, plan, debug
claude plugin install research@borda-ai-rig  # ML research: topic, plan, judge, run, sweep
```

> [!NOTE]
>
> **Safe to install alongside any existing Claude Code setup.** Plugins live in a private cache (`~/.claude/plugins/cache/<plugin>/`) under their own namespace. Your existing `~/.claude/agents/`, `~/.claude/skills/`, and `settings.json` are never modified or overwritten — custom agents and skills you have created remain fully independent. See the [Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference) for details.

**4. One-time settings merge** — run inside Claude Code:

```text
/foundry:init link
```

`link` symlinks foundry agents and skills into `~/.claude/` so you can type `/audit`, `/manage`, `/brainstorm`, etc. without a `foundry:` prefix. OSS, develop, and research skills always use their plugin prefix (`/oss:review`, `/develop:fix`, `/research:run`). Safe to re-run.

> [!IMPORTANT]
>
> **Codex CLI** — optional companion; the plugins install Claude Code agents and skills only:
>
> ```bash
> npm install -g @openai/codex
> cp -r Borda-AI-Rig/.codex/ ~/.codex/   # Codex agents and profiles
> ```

→ See [Token Savings (RTK)](#-token-savings-rtk) for RTK install details.

## 🔁 Daily OSS Workflow

A typical maintainer morning — 15 new issues, 3 PRs waiting, a release due:

```text
# 1. Morning triage — what needs attention?
/oss:analyse health                # repo overview, duplicate issue clustering, stale PR detection

# 2. Review incoming PRs
/oss:review 55 --reply             # 7-agent review + welcoming contributor comment

# — or: full review first, then apply every finding in one automated pass
/oss:review 21                     # 7-agent review → saved findings report
/oss:resolve 21 report             # Codex reads the report and applies every comment

# 3. Fix the critical bug from overnight
/oss:analyse 42                    # understand the issue
/develop:fix 42                    # reproduce → regression test → minimal fix → quality stack

# 4. Ship the release
/oss:release prepare v2.1.0        # changelog, notes, migration guide, readiness audit
```

Each command chains agents in a defined topology — see [Common Workflow Sequences](#common-workflow-sequences) below for more patterns.

## 📦 What's Here

```text
AI-Rig/
├── plugins/
│   ├── foundry/            # Base plugin: agents, hooks, audit/manage/calibrate/brainstorm/…
│   │   ├── .claude-plugin/
│   │   │   └── plugin.json # plugin manifest
│   │   ├── agents/         # 8 foundry agents (canonical source)
│   │   ├── skills/         # foundry skills (canonical source)
│   │   ├── rules/          # rule files (canonical source; symlinked from .claude/rules/)
│   │   ├── CLAUDE.md       # workflow rules (symlinked from .claude/CLAUDE.md)
│   │   ├── TEAM_PROTOCOL.md # AgentSpeak v2 protocol (symlinked from .claude/TEAM_PROTOCOL.md)
│   │   ├── permissions-guide.md # allow-entry reference (symlinked from .claude/permissions-guide.md)
│   │   └── hooks/
│   │       └── hooks.json  # task tracking, quality gates, preprocessing
│   ├── oss/                # OSS plugin: shepherd, ci-guardian + analyse/review/resolve/release
│   ├── develop/            # Develop plugin: feature/fix/refactor/plan/debug
│   └── research/           # Research plugin: scientist, data-steward + topic/plan/judge/run/sweep
├── .claude/                # Claude Code source of truth
│   ├── README.md           # full reference: restore, skills, rules, hooks, architecture (real file)
│   ├── CLAUDE.md           # workflow rules and core principles (symlink → plugins/foundry/)
│   ├── TEAM_PROTOCOL.md    # AgentSpeak v2 inter-agent protocol (symlink → plugins/foundry/)
│   ├── permissions-guide.md # allow-entry reference (symlink → plugins/foundry/)
│   ├── settings.json       # deny list + project preferences (real file)
│   ├── agents/             # symlinks → plugins/foundry/agents/
│   ├── skills/             # symlinks → plugins/foundry/skills/
│   ├── rules/              # per-topic coding and config standards (symlinks → plugins/foundry/rules/)
│   └── hooks/              # symlinks → plugins/foundry/hooks/
├── .mcp.json               # MCP server definitions
├── .codex/                 # OpenAI Codex CLI
│   ├── README.md           # full reference: agents, profiles, Claude integration
│   ├── AGENTS.md           # global instructions and subagent spawn rules
│   ├── config.toml         # multi-agent config (gpt-5.4 baseline)
│   ├── agents/             # per-agent model and instruction overrides
│   ├── calibration/        # self-calibration harness + fixed task set
│   └── skills/             # codex-native workflow skills
├── .pre-commit-config.yaml
├── .gitignore
└── README.md
```

## 🧩 Agents

Specialist roles with deep domain knowledge — requested by name, or auto-selected by Claude Code and Codex CLI.

| Agent                  | Claude [plugins] | Codex | Purpose                                                       |
| ---------------------- | ---------------- | ----- | ------------------------------------------------------------- |
| **doc-scribe**         | 🟠 foundry       | ✓     | Google/Napoleon docstrings, Sphinx/mkdocs, API references     |
| **linting-expert**     | 🟠 foundry       | ✓     | ruff, mypy, pre-commit, type annotations                      |
| **perf-optimizer**     | 🟠 foundry       | —     | Profile-first CPU/GPU/memory/I/O, torch.compile               |
| **qa-specialist**      | 🟠 foundry       | ✓     | pytest, hypothesis, mutation testing, ML test patterns        |
| **self-mentor**        | 🟠 foundry       | ✓     | Config quality review, duplication detection, cross-ref audit |
| **solution-architect** | 🟠 foundry       | ✓     | System design, ADRs, API surface, migration plans             |
| **sw-engineer**        | 🟠 foundry       | ✓     | Architecture, implementation, SOLID principles, type safety   |
| **web-explorer**       | 🟠 foundry       | ✓     | API version comparison, migration guides, PyPI tracking       |
| **ci-guardian**        | 🟢 oss           | ✓     | GitHub Actions, test matrices, flaky test detection, caching  |
| **shepherd**           | 🟢 oss           | ✓     | Issue triage, PR review, SemVer, releases, trusted publishing |
| **data-steward**       | 🟣 research      | ✓     | Dataset versioning, split validation, leakage detection       |
| **scientist**          | 🟣 research      | —     | Paper analysis, hypothesis generation, experiment design      |

## 🤖 Claude Code

Agents and skills for [Claude Code](https://claude.ai/code) (Anthropic's AI coding CLI).

### Skills

Skills are multi-agent workflows invoked via slash commands. Each skill composes several agents in a defined topology.

After running `/foundry:init link`, foundry skills are available without a prefix. OSS, develop, and research skills always use their plugin prefix.

| Skill                  | What It Does                                                                                                                                                           |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 🟠 `/brainstorm`       | `/brainstorm <idea>` — clarifying questions → approaches → spec → self-mentor review → approval gate; `breakdown <spec>` — ordered task table with per-task skill tags |
| 🟠 `/manage`           | Create, update, delete agents/skills/rules; manage `settings.json` permissions; auto type-detection and cross-ref propagation                                          |
| 🟠 `/investigate`      | Systematic diagnosis for unknown failures — env, tools, hooks, CI divergence; ranks hypotheses and hands off to the right skill                                        |
| 🟠 `/session`          | Parking lot for diverging ideas — auto-parks unanswered questions and deferred threads; `resume` shows pending, `archive` closes, `summary` digests the session        |
| 🟠 `/audit`            | Config audit: broken refs, inventory drift, docs freshness; `fix [high\|medium\|all]` auto-fixes by severity; `upgrade` applies docs-sourced improvements              |
| 🟠 `/calibrate`        | Synthetic benchmarks measuring recall vs confidence bias                                                                                                               |
| 🟠 `/distill`          | Suggest new agents/skills, prune memory, consolidate lessons into rules                                                                                                |
| 🔵 `/develop:plan`     | Scope analysis and implementation planning without code changes                                                                                                        |
| 🔵 `/develop:feature`  | TDD-first feature implementation: codebase analysis, demo test, TDD loop, docs, review                                                                                 |
| 🔵 `/develop:fix`      | Reproduce-first bug fixes: regression test, minimal fix, quality stack                                                                                                 |
| 🔵 `/develop:debug`    | Systematic debugging for known test failures                                                                                                                           |
| 🔵 `/develop:refactor` | Test-first refactors with scope analysis                                                                                                                               |
| 🔵 `/develop:review`   | Six-agent parallel review of local files or current git diff; no GitHub PR needed                                                                                      |
| 🟢 `/oss:analyse`      | GitHub thread analysis; `health` = repo overview + duplicate issue clustering                                                                                          |
| 🟢 `/oss:review`       | Tiered parallel review of GitHub PRs; `--reply` drafts welcoming contributor comments                                                                                  |
| 🟢 `/oss:resolve`      | OSS fast-close: resolving conflicts + applying review comments via codex-plugin-cc; three source modes: `pr`, `report`, `pr + report`                                  |
| 🟢 `/oss:release`      | SemVer-disciplined release pipeline: notes, changelog with deprecation tracking, migration guides, full prepare pipeline                                               |
| 🟣 `/research:topic`   | SOTA literature research with codebase-mapped implementation plan                                                                                                      |
| 🟣 `/research:plan`    | Config wizard: profile-first bottleneck discovery → `program.md`                                                                                                       |
| 🟣 `/research:judge`   | Research-supervisor review of experimental methodology (APPROVED/NEEDS-REVISION/BLOCKED)                                                                               |
| 🟣 `/research:run`     | Metric-driven iteration loop; `--resume` continues after crash; `--team` for parallel exploration; `--colab` for GPU workloads                                         |
| 🟣 `/research:sweep`   | Non-interactive pipeline: auto-plan → judge gate → run                                                                                                                 |

→ Full command reference, orchestration flows, rules (13 auto-loaded rule files), architecture internals, status line — see [`.claude/README.md` → Skills](.claude/README.md#-skills)

### Common Workflow Sequences

Skills chain naturally — the output of one becomes the input for the next.

<details>
<summary><strong>Bug report → fix → validate</strong></summary>

```text
/oss:analyse 42            # understand the issue, extract root cause hypotheses
/develop:fix 42            # reproduce with test, apply targeted fix
/oss:review                # validate the fix meets quality standards
```

</details>

<details>
<summary><strong>Performance investigation → optimize → refactor</strong></summary>

```text
/research:plan src/mypackage/dataloader.py      # profile-first: cProfile → pick goal → wizard
/develop:refactor src/mypackage/dataloader.py  # extract caching layer
/develop:review                                # review diff before commit
```

</details>

<details>
<summary><strong>Code review → fix blocking issues</strong></summary>

```text
/oss:review 55                                           # 7 agent dimensions + Codex co-review
/develop:fix "race condition in cache invalidation"      # fix blocking issue from review
/oss:review 55                                           # re-review after fix
```

</details>

<details>
<summary><strong>New feature → implement → release</strong></summary>

```text
/oss:analyse 87            # understand the issue, clarify acceptance criteria
/develop:feature 87        # codebase analysis, demo test, TDD, docs, review
/oss:release               # generate CHANGELOG entry and release notes
```

</details>

<details>
<summary><strong>New OSS capability → research → implement → review</strong></summary>

```text
/research:topic "efficient attention for long sequences"  # find SOTA methods
/develop:feature "implement FlashAttention in encoder"    # TDD-first implementation
/develop:review                                           # review diff before commit
# push + open PR → /oss:review 42 for PR review
```

</details>

<details>
<summary><strong>Autonomous metric improvement campaign</strong></summary>

```text
/research:plan "increase test coverage to 90%"      # interactive config wizard → program.md
/research:run "increase test coverage to 90%"       # run 20-iteration loop; auto-rollback on regression
/research:run --resume                              # resume after crash or manual stop
```

</details>

<details>
<summary><strong>Fuzzy idea → spec → breakdown → implement</strong></summary>

```text
/brainstorm "integrate OpenSpace MCP for skill evolution"
# clarifying questions → 2–3 approaches → spec saved to .plans/blueprint/ → self-mentor review → approval

/brainstorm breakdown .plans/blueprint/2026-03-31-openspace-mcp-integration.md
# reads spec → ordered task table with per-task skill/command tags:
#   | 1 | Install OpenSpace venv         | bash                    |
#   | 2 | Add .mcp.json config entry      | /manage update / Write  |
#   | 3 | Copy bootstrap skills           | bash + /manage          |
#   | 4 | Enable in settings.local.json   | /manage update          |

# then execute each row in the breakdown table using its tagged skill
```

</details>

<details>
<summary><strong>Research SOTA → optimize toward metric</strong></summary>

```text
/research:topic "knowledge distillation for small models"  # find best approach
/research:plan "improve F1 from 0.82 to 0.87"              # configure metric + guard + agent
/research:run --team                                       # parallel exploration across axes
```

</details>

<details>
<summary><strong>Distill → create → audit → calibrate</strong></summary>

```text
/distill                             # analyze work patterns, suggest new agents/skills
/manage create agent my-agent "..."  # scaffold suggested agent
/audit                               # verify config integrity — catch broken refs, dead loops
/calibrate routing                   # confirm new agent description doesn't confuse routing
```

</details>

<details>
<summary><strong>PR review feedback → resolve → verify</strong></summary>

```text
/oss:resolve 42   # auto-detect conflicts → resolve semantically → apply review comments via codex-plugin-cc
/develop:review   # full quality pass on all applied changes
```

</details>

<details>
<summary><strong>OSS contributor PR triage → review → reply</strong></summary>

Preferred flow for maintainers responding to external contributions:

```text
/oss:analyse 42 --reply      # assess PR readiness + draft contributor reply in one step

# or if you need the full deep review first:
/oss:review 42 --reply        # 7-agent + Codex co-review + draft overall comment + inline comments table
                              # output: .temp/output-reply-pr-42-dev-<date>.md

# post when ready:
gh pr comment 42 --body "$(cat .temp/output-reply-pr-42-dev-<date>.md)"
```

Both `--reply` flags produce the same two-part shepherd output: an overall PR comment (prose, warm, decisive) and an inline comments table (file | line | 1–2 sentence fix). The `/oss:analyse` path is faster for routine triage; `/oss:review` path gives deeper findings for complex PRs.

</details>

<details>
<summary><strong>Agent self-improvement loop</strong></summary>

```text
/distill                        # analyze work patterns, surface what agents are missing or miscalibrated
/calibrate all fast ab apply    # benchmark all agents vs general-purpose baseline, apply improvement proposals
/audit fix                      # structural sweep after calibrate changed instruction files
```

</details>

<details>
<summary><strong>Agent description drift → routing alignment check</strong></summary>

After editing agent descriptions (manually or via `/audit fix`), verify that routing accuracy hasn't degraded:

```text
/audit                      # Check 20 flags description overlap pairs (static, fast)
/calibrate routing fast     # behavioral test: generates task prompts, measures routing accuracy
```

Run `/calibrate routing fast` after any agent description change. Thresholds: routing accuracy ≥90%, hard-problem accuracy ≥80%.

</details>

<details>
<summary><strong>Config maintenance — periodic health check</strong></summary>

```text
/audit                 # inspect findings + docs-sourced upgrade proposals — report only, no changes
/audit upgrade         # apply upgrade proposals: config changes verified, capability changes A/B tested
/audit fix             # full sweep + auto-fix critical and high findings
```

</details>

<details>
<summary><strong>Memory hygiene — monthly or after a burst of corrections</strong></summary>

MEMORY.md is injected into every message in every session. As it grows, so does the per-message token cost — compounding across every turn. Keep it lean.

```text
/distill lessons    # promote recurring corrections into durable rules/agents/skills
/distill prune      # trim MEMORY.md — drop entries now covered by rules, stale facts, or superseded decisions
```

Run after any session with significant corrections, or monthly as routine hygiene.

</details>

<details>
<summary><strong>Keep config current after Claude Code releases</strong></summary>

```text
/audit                 # fetches latest Claude Code docs, surfaces applicable improvements as upgrade proposals
/audit upgrade         # applies config proposals (correctness check) and capability proposals (calibrate A/B)
/calibrate all fast    # re-benchmark all agents to confirm no regression from applied changes
```

</details>

<details>
<summary><strong>Release preparation</strong></summary>

```text
/oss:release notes v1.2.0..HEAD  # generate release notes from git history
```

</details>

## 🤖 Codex CLI

Multi-agent configuration for [OpenAI Codex CLI](https://github.com/openai/codex) (Rust implementation). Default session model is `gpt-5.4`, with 12 specialist roles and a mirrored codex-native skill backbone (`review/develop/resolve/audit` + `calibrate/release/investigate/manage/analyse/research`).

### Usage

```bash
codex                                                         # interactive — auto-selects agents
codex "use the qa-specialist to review src/api/auth.py"       # address agent by name
codex --profile deep-review "full security audit of src/api/" # activate a profile
```

### Codex Skill Invocation (Important)

Codex does not expose your mirrored skills as slash commands. In this setup:

- `/fast` works (built-in Codex command).
- `/investigate`, `/resolve`, `/review` do not work as slash commands.

Use prompt-based invocation instead:

```text
run investigate on this branch and find root cause of failing CI
run resolve for the current working tree and fix high-severity findings
run review, then develop, then audit for issue #42
```

One-shot examples:

```bash
codex "run investigate on current diff and produce investigation findings"
codex "run resolve on this repo and apply required quality gates"
```

### Install

```bash
npm install -g @openai/codex          # install Codex CLI
cp -r Borda-AI-Rig/.codex/ ~/.codex/ # activate globally (run from parent of clone)
```

### Sync / Update

After pulling the repo, re-apply to `~/.codex/`:

```bash
# ⚠ Overwrites ~/.codex/ completely — use rsync below if you have local customizations
cp -r Borda-AI-Rig/.codex/ ~/.codex/

# Incremental sync (preserves any local-only files you added)
rsync -av Borda-AI-Rig/.codex/ ~/.codex/
```

Use `rsync` when you have local customizations (extra agents, personal profiles) that you don't want overwritten.

### Files

| File                | Purpose                                                                    |
| ------------------- | -------------------------------------------------------------------------- |
| `AGENTS.md`         | Global agent instructions, The Borda Standard, spawn rules                 |
| `config.toml`       | Multi-agent config: profiles, MCP server, sandbox, skills                  |
| `agents/*.toml`     | Per-agent model and reasoning effort overrides                             |
| `skills/*/SKILL.md` | Codex-native workflow skills (core skills are execution-ready)             |
| `skills/_shared/*`  | Shared execution helpers (`run-gates.sh`, `write-result.sh`, severity map) |
| `calibration/*`     | Self-calibration harness (`run.sh`, `tasks.json`, `benchmarks.json`)       |

→ Deep reference: skills, quality gates, calibration, and architecture — see [`.codex/README.md`](.codex/README.md)

## 🤝 Claude + Codex Integration

Claude and Codex complement each other — Claude handles long-horizon reasoning, orchestration, and judgment calls; Codex handles focused, mechanical in-repo coding tasks with direct shell access.

Every skill that reviews or validates code uses a three-tier pipeline:

- **Tier 0** (mechanical `git diff --stat` gate)
- **Tier 1** (codex:review pre-pass, ~60s, diff-focused)
- **Tier 2** (specialized Claude agents).

Cheaper tiers gate the expensive ones — this keeps full agent spawns reserved for diffs that actually need them. → Full architecture with skill-tier matrix: [`.claude/README.md` → Tiered review pipeline](.claude/README.md#tiered-review-pipeline)

**Why unbiased review matters / Real example**: Claude makes targeted changes with intentionality — it has a mental model of which files are "in scope". Codex has no such context: it reads the diff and the codebase independently. During one session, Claude applied a docstring-style mandate across 6 files and scored its own confidence at 0.88. The Codex pre-pass then found `skills/develop/modes/feature.md` still referencing the old style — a direct miss. The union of both passes is more complete than either alone.

### Two integration patterns make this pairing practical

1. **Offloading mechanical tasks from Claude to Codex**

   Claude identifies what needs to change and delegates execution to the plugin agent. Claude keeps its context clean and validates the output via `git diff HEAD`.

   Dispatched automatically by `/oss:review`, `/oss:resolve`, `/calibrate`, and `/research:run` via `codex-delegation.md`. The plugin agent has full working-tree access.

2. **Codex reviewing staged work**

   After Claude stages changes, `codex:review --wait` serves as a second pass — examining the diff, applying review comments, or resolving PR conflicts. The `/oss:resolve` skill automates this: it resolves conflicts semantically (Claude) then applies review comments (plugin agent).

   ```text
   /oss:resolve 42   # Claude resolves conflicts → plugin agent applies review comments
   /oss:resolve "rename the `fit` method to `train` throughout the module"
   ```

<details>
<summary><strong>Setup requirement</strong></summary>

Install the Codex plugin in Claude Code:

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
```

Without the plugin: pre-pass review is skipped gracefully (skills check with `claude plugin list | grep 'codex@openai-codex'`); `/oss:resolve`'s review-comment step is skipped (conflict resolution works with Claude alone).

</details>

## 🛠 Recommended Add-ons

### Token Savings (RTK)

[RTK](https://github.com/rtk-ai/rtk) is an optional CLI proxy that compresses Bash output (git, pytest, build tools) before it reaches Claude — 60–99% token savings with no workflow changes. A `PreToolUse` hook (`plugins/foundry/hooks/rtk-rewrite.js`) transparently rewrites supported commands across all Claude skills; Codex runs get the same treatment via `.codex/hooks/rtk-enforce.js`. The hook is a no-op when RTK is not installed, so the config stays portable.

→ Install instructions: [rtk-ai/rtk](https://github.com/rtk-ai/rtk)

### Codex CLI plugin

[openai/codex-plugin-cc](https://github.com/openai/codex-plugin-cc) connects the Codex CLI to Claude Code as a local plugin — enabling the cross-validation, mechanical delegation, and diff pre-pass described in [Claude + Codex Integration](#-claude--codex-integration).

→ Install: `/plugin marketplace add openai/codex-plugin-cc` → `/plugin install codex@openai-codex` → `/reload-plugins`

> [!NOTE]
>
> RTK only compresses **Bash tool output** — shell commands like `git`, `cargo`, `pytest`, etc. It does not affect Claude Code's native tools (Read, Grep, Glob, Edit, Write), which run inside Claude's own engine and are already token-efficient by design.

### cc-Lens

[cc-Lens](https://github.com/Arindam200/cc-lens) is a local analytics dashboard for Claude Code — token/cost trends, tool usage breakdowns, session replay. Reads `~/.claude/` directly, no cloud, no data leaves the machine.

→ Run: `npx cc-lens` — no install required

### Colab-MCP

[colab-mcp](https://github.com/googlecolab/colab-mcp) connects Google Colab as a remote GPU executor. Pre-configured in `.mcp.json` (disabled by default) — used by `/research:run --colab` to offload metric-improvement iterations to a cloud GPU without a local CUDA setup. Supports hardware selection: `--colab=H100`, `--colab=L4`, `--colab=T4`, `--colab=A100`.

→ Enable: add `"colab-mcp"` to `enabledMcpjsonServers` in `settings.local.json`

### Caveman

[caveman](https://github.com/JuliusBrussee/caveman) makes Claude respond in compressed "caveman speak" — cutting ~75% of output tokens while retaining full technical accuracy. Adjustable intensity levels (lite → full → ultra → 文言文) and a compression tool that also cuts ~46% of input tokens per session.

→ Install: `claude plugin marketplace add JuliusBrussee/caveman && claude plugin install caveman@caveman`

## 🔌 Plugin Management

### Upgrade

```bash
cd Borda-AI-Rig && git pull
claude plugin install foundry@borda-ai-rig   # reinstalls from updated source
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

Re-run `/foundry:init` only if permissions or `enabledPlugins` changed. Re-run `/foundry:init link` if you previously used the link mode — symlinks point to the old plugin cache after an upgrade.

### Session-only (no install, for development)

```bash
claude --plugin-dir ./Borda-AI-Rig/plugins/foundry
```

### Uninstall

```bash
claude plugin uninstall foundry
claude plugin uninstall oss
claude plugin uninstall develop
claude plugin uninstall research
```

Settings added by `/foundry:init` remain in `~/.claude/settings.json`; remove manually if desired. If `/foundry:init link` was run, symlinks in `~/.claude/agents/` and `~/.claude/skills/` also persist and will be broken after uninstall — remove with `rm ~/.claude/agents/<name>.md` and `rm -rf ~/.claude/skills/<name>` for each.

______________________________________________________________________

<div align="center">

**Questions?** Open an [issue](https://github.com/Borda/AI-Rig/issues) or start a [discussion](https://github.com/Borda/AI-Rig/discussions).

Made with 💙 by the Borda et al.

</div>
