# 🏠 Borda's .ai-home

Personal AI coding assistant configuration for Python/ML OSS development. Version-controlled, opinionated, continuously improved.

<details>
<summary><strong>Contents</strong></summary>

- [🎯 Why](#-why)
- [💡 Design Principles](#-design-principles)
- [⚡ Quick Start](#-quick-start)
- [📦 What's Here](#-whats-here)
- [🧩 Agents](#-agents)
- [🤖 Claude Code](#-claude-code)
- [🤖 Codex CLI](#-codex-cli)
- [🤝 Claude + Codex Integration](#-claude--codex-integration)
- [🪙 Token Savings (RTK)](#-token-savings-rtk)
- [🔄 Config Sync](#-config-sync)

</details>

## 🎯 Why

Managing AI coding workflows for Python/ML OSS is complex — you need domain-aware agents, not generic chat. This config packages 12 specialist agents and 15 slash-command skill workflows in a version-controlled, continuously benchmarked setup optimized for:

- Python/ML OSS libraries requiring SemVer discipline and deprecation cycles
- ML training and inference codebases needing GPU profiling and data pipeline validation
- Multi-contributor projects with CI/CD, pre-commit hooks, and automated releases

## 💡 Design Principles

- **Agents are roles, skills are workflows** — agents carry domain expertise, skills orchestrate multi-step processes
- **No duplication** — agents reference each other instead of repeating content
- **Profile-first, measure-last** — performance skills always bracket changes with measurements
- **Link integrity** — never cite a URL without fetching it first (enforced in all research agents)
- **Python 3.10+ baseline** — all configs target py310 minimum (3.9 EOL was Oct 2025)
- **Modern toolchain** — uv, ruff, mypy, pytest, GitHub Actions with trusted publishing

## ⚡ Quick Start

```bash
# Install Claude Code and Codex CLI
npm install -g @anthropic-ai/claude-code && npm install -g @openai/codex

# Activate config globally
cp -r .claude/ ~/.claude/    # Claude Code agents, skills, hooks
cp -r .codex/ ~/.codex/      # Codex CLI agents and profiles

# Optional: install RTK for 60–99% token savings on CLI output
# See the Token Savings section below for install instructions
```

→ See [Token Savings (RTK)](#-token-savings-rtk) for install and details.

## 📦 What's Here

```
borda.ai-home/
├── .claude/                # Claude Code (Claude by Anthropic)
│   ├── README.md           # full reference: skills, rules, hooks, architecture
│   ├── CLAUDE.md           # workflow rules and core principles
│   ├── settings.json       # permissions and model preferences
│   ├── agents/             # specialist agents
│   ├── skills/             # workflow skills (slash commands)
│   ├── rules/              # per-topic coding and config standards (auto-loaded by Claude Code)
│   └── hooks/              # UI extensions
├── .mcp.json               # MCP server definitions (source of truth; synced to ~/.claude/.mcp.json)
├── .codex/                 # OpenAI Codex CLI
│   ├── README.md           # full reference: agents, profiles, Claude integration
│   ├── AGENTS.md           # global instructions and subagent spawn rules
│   ├── config.toml         # multi-agent config (gpt-5.3-codex baseline)
│   └── agents/             # per-agent model and instruction overrides
├── .pre-commit-config.yaml
├── .gitignore
└── README.md
```

## 🧩 Agents

Specialist roles with deep domain knowledge — requested by name, or auto-selected by Claude Code and Codex CLI.

| Agent                  | Claude | Codex | Purpose                                                       |
| ---------------------- | ------ | ----- | ------------------------------------------------------------- |
| **ai-researcher**      | ✓      | —     | Paper analysis, hypothesis generation, experiment design      |
| **ci-guardian**        | ✓      | ✓     | GitHub Actions, test matrices, flaky test detection, caching  |
| **data-steward**       | ✓      | ✓     | Dataset versioning, split validation, leakage detection       |
| **doc-scribe**         | ✓      | ✓     | Google/Napoleon docstrings, Sphinx/mkdocs, API references     |
| **linting-expert**     | ✓      | ✓     | ruff, mypy, pre-commit, type annotations                      |
| **oss-shepherd**       | ✓      | ✓     | Issue triage, PR review, SemVer, releases, trusted publishing |
| **perf-optimizer**     | ✓      | —     | Profile-first CPU/GPU/memory/I/O, torch.compile               |
| **qa-specialist**      | ✓      | ✓     | pytest, hypothesis, mutation testing, ML test patterns        |
| **self-mentor**        | ✓      | —     | Config quality review, duplication detection, cross-ref audit |
| **solution-architect** | ✓      | —     | System design, ADRs, API surface, migration plans             |
| **sw-engineer**        | ✓      | ✓     | Architecture, implementation, SOLID principles, type safety   |
| **web-explorer**       | ✓      | —     | API version comparison, migration guides, PyPI tracking       |

## 🤖 Claude Code

Agents and skills for [Claude Code](https://claude.ai/code) (Anthropic's AI coding CLI).

### Skills

Skills are multi-agent workflows invoked via slash commands. Each skill composes several agents in a defined topology.

| Skill           | What It Does                                                                                                                                                                                                                                                                                                                |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **review**      | Parallel review across arch, tests, perf, docs, lint, security, API; `--reply` drafts comment                                                                                                                                                                                                                               |
| **analyse**     | GitHub thread analysis; `health` = repo overview + duplicate clustering                                                                                                                                                                                                                                                     |
| **brainstorm**  | `/brainstorm <idea>` — clarifying questions → approaches → spec (saved to `.plans/blueprint/`) → self-mentor review → approval gate; `/brainstorm breakdown <spec>` — read approved spec → ordered task table with per-task skill/command tags                                                                              |
| **develop**     | TDD-first features, reproduce-first fixes, test-first refactors, scope analysis, debugging                                                                                                                                                                                                                                  |
| **resolve**     | OSS fast-close: conflicts + review comments via codex-plugin-cc; three source modes: `pr` (live GitHub), `report` (/review findings), `pr + report` (aggregated + deduplicated in one pass)                                                                                                                                 |
| **calibrate**   | Synthetic benchmarks measuring recall vs confidence bias                                                                                                                                                                                                                                                                    |
| **audit**       | Config audit: broken refs, inventory drift, docs freshness; `fix [high\|medium\|all]` auto-fixes by severity; `upgrade` applies docs-sourced improvements (mutually exclusive)                                                                                                                                              |
| **release**     | Notes, changelog, summary, migration, full prepare pipeline, or readiness `audit`                                                                                                                                                                                                                                           |
| **research**    | SOTA literature research with implementation plan; `plan` mode produces a phased, codebase-mapped implementation plan (auto-detects latest research output)                                                                                                                                                                 |
| **optimize**    | Four modes: `plan` = config wizard (or `plan <file.py>` for profile-first bottleneck discovery) → `program.md`; `judge` = research-supervisor review of experimental methodology (APPROVED/NEEDS-REVISION/BLOCKED); `run` = metric-driven iteration loop; `resume` = continue after crash; `--team` and `--colab` supported |
| **manage**      | Create, update, delete agents/skills/rules; manage `settings.json` permissions; auto type-detection and cross-ref propagation                                                                                                                                                                                               |
| **sync**        | Drift-detect and sync project `.claude/` and `.codex/` → home `~/.claude/` and `~/.codex/`                                                                                                                                                                                                                                  |
| **investigate** | Systematic diagnosis for unknown failures — env, tools, hooks, CI divergence; ranks hypotheses and hands off to the right skill                                                                                                                                                                                             |
| **session**     | Parking lot for diverging ideas — auto-parks unanswered questions and deferred threads; `resume` shows pending, `archive` closes, `summary` digests the session                                                                                                                                                             |
| **distill**     | Suggest new agents/skills, prune memory, consolidate lessons into rules                                                                                                                                                                                                                                                     |

→ Full command reference, orchestration flows, rules (13 auto-loaded rule files), architecture internals, status line — see [`.claude/README.md` → Skills](.claude/README.md#-skills)

### Common Workflow Sequences

Skills chain naturally — the output of one becomes the input for the next.

<details>
<summary><strong>Bug report → fix → validate</strong></summary>

```
/analyse 42            # understand the issue, extract root cause hypotheses
/develop fix 42        # reproduce with test, apply targeted fix
/review                # validate the fix meets quality standards
```

</details>

<details>
<summary><strong>Performance investigation → optimize → refactor</strong></summary>

```
/optimize plan src/mypackage/dataloader.py     # profile-first: cProfile → pick goal → wizard
/develop refactor src/mypackage/dataloader.py  # extract caching layer
/review                                        # full quality pass on changes
```

</details>

<details>
<summary><strong>Code review → fix blocking issues</strong></summary>

```
/review 55                                           # 7 agent dimensions + Codex co-review
/develop fix "race condition in cache invalidation"  # fix blocking issue from review
/review 55                                           # re-review after fix
```

</details>

<details>
<summary><strong>New feature → implement → release</strong></summary>

```
/analyse 87            # understand the issue, clarify acceptance criteria
/develop feature 87    # codebase analysis, demo test, TDD, docs, review
/release               # generate CHANGELOG entry and release notes
```

</details>

<details>
<summary><strong>New capability → research → implement</strong></summary>

```
/research "efficient attention for long sequences"        # find SOTA methods
/develop feature "implement FlashAttention in encoder"    # TDD-first implementation
/review                                                   # validate implementation
```

</details>

<details>
<summary><strong>Autonomous metric improvement campaign</strong></summary>

```
/optimize plan "increase test coverage to 90%"      # interactive config wizard → program.md
/optimize run "increase test coverage to 90%"   # run 20-iteration loop; auto-rollback on regression
/optimize resume                                    # resume after crash or manual stop
/review                                             # validate kept commits
```

</details>

<details>
<summary><strong>Fuzzy idea → spec → breakdown → implement</strong></summary>

```
/brainstorm "integrate OpenSpace MCP for skill evolution"
# clarifying questions → 2–3 approaches → spec saved to .plans/blueprint/ → self-mentor review → approval

/brainstorm breakdown .plans/blueprint/2026-03-31-openspace-mcp-integration.md
# reads spec → ordered task table with per-task skill/command tags:
#   | 1 | Install OpenSpace venv         | bash                  |
#   | 2 | Add .mcp.json config entry      | /manage update / Write |
#   | 3 | Copy bootstrap skills           | bash + /manage        |
#   | 4 | Enable in settings.local.json   | /manage update        |

# then execute each row in the breakdown table using its tagged skill
```

</details>

<details>
<summary><strong>Research SOTA → optimize toward metric</strong></summary>

```
/research "knowledge distillation for small models"           # find best approach
/optimize plan "improve F1 from 0.82 to 0.87"                 # configure metric + guard + agent
/optimize run --team                                      # parallel exploration across axes
/review                                                       # quality pass on kept changes
```

</details>

<details>
<summary><strong>Distill → create → audit → sync</strong></summary>

```
/distill                             # analyze work patterns, suggest new agents/skills
/manage create agent my-agent "..."  # scaffold suggested agent
/audit                               # verify config integrity — catch broken refs, dead loops
/calibrate routing                   # confirm new agent description doesn't confuse routing
/sync apply                          # propagate clean config to ~/.claude/
```

</details>

<details>
<summary><strong>PR review feedback → resolve → verify</strong></summary>

```
/resolve 42   # auto-detect conflicts → resolve semantically → apply review comments via codex-plugin-cc
/review       # full quality pass on all applied changes
```

</details>

<details>
<summary><strong>OSS contributor PR triage → review → reply</strong></summary>

Preferred flow for maintainers responding to external contributions:

```
/analyse 42 --reply      # assess PR readiness + draft contributor reply in one step

# or if you need the full deep review first:
/review 42 --reply        # 7-agent + Codex co-review + draft overall comment + inline comments table
                          # output: .temp/output-reply-pr-42-dev-<date>.md

# post when ready:
gh pr comment 42 --body "$(cat .temp/output-reply-pr-42-dev-<date>.md)"
```

Both `--reply` flags produce the same two-part oss-shepherd output: an overall PR comment (prose, warm, decisive) and an inline comments table (file | line | 1–2 sentence fix). The `/analyse` path is faster for routine triage; `/review` path gives deeper findings for complex PRs.

</details>

<details>
<summary><strong>Agent self-improvement loop</strong></summary>

```
/distill                        # analyze work patterns, surface what agents are missing or miscalibrated
/calibrate all fast ab apply    # benchmark all agents vs general-purpose baseline, apply improvement proposals
/audit fix                      # structural sweep after calibrate changed instruction files
/sync apply                     # propagate improved config to ~/.claude/
```

</details>

<details>
<summary><strong>Agent description drift → routing alignment check</strong></summary>

After editing agent descriptions (manually or via `/audit fix`), verify that routing accuracy hasn't degraded:

```
/audit                      # Check 12 flags description overlap pairs (static, fast)
/calibrate routing fast     # behavioral test: generates task prompts, measures routing accuracy
```

Run `/calibrate routing fast` after any agent description change. Thresholds: routing accuracy ≥90%, hard-problem accuracy ≥80%.

</details>

<details>
<summary><strong>Config maintenance — periodic health check</strong></summary>

```
/audit                 # inspect findings + docs-sourced upgrade proposals — report only, no changes
/audit upgrade         # apply upgrade proposals: config changes verified, capability changes A/B tested
/audit fix             # full sweep + auto-fix critical and high findings
/sync apply            # propagate verified config to ~/.claude/
```

</details>

<details>
<summary><strong>Memory hygiene — monthly or after a burst of corrections</strong></summary>

MEMORY.md is injected into every message in every session. As it grows, so does the per-message token cost — compounding across every turn. Keep it lean.

```
/distill lessons    # promote recurring corrections into durable rules/agents/skills
/distill prune      # trim MEMORY.md — drop entries now covered by rules, stale facts, or superseded decisions
/sync apply         # propagate rule changes to ~/.claude/
```

Run after any session with significant corrections, or monthly as routine hygiene.

</details>

<details>
<summary><strong>Keep config current after Claude Code releases</strong></summary>

```
/audit                 # fetches latest Claude Code docs, surfaces applicable improvements as upgrade proposals
/audit upgrade         # applies config proposals (correctness check) and capability proposals (calibrate A/B)
/calibrate all fast    # re-benchmark all agents to confirm no regression from applied changes
/sync apply            # propagate clean, calibrated config to ~/.claude/
```

</details>

<details>
<summary><strong>Release preparation</strong></summary>

```
/release notes v1.2.0..HEAD  # generate release notes from git history
```

</details>

## 🤖 Codex CLI

Multi-agent configuration for [OpenAI Codex CLI](https://github.com/openai/codex) (Rust implementation). Nine specialist roles on `gpt-5.3-codex`, auto-selected by task type or addressed by name. See the Agents table above for the full roster with Claude/Codex availability.

### Usage

```bash
codex                                                          # interactive — auto-selects agents
codex "use the qa-specialist to review src/api/auth.py"        # address agent by name
codex --profile deep-review "full security audit of src/api/" # activate a profile
```

### Install

```bash
npm install -g @openai/codex    # install Codex CLI
cp -r .codex/ ~/.codex/         # activate globally
```

### Files

| File            | Purpose                                                     |
| --------------- | ----------------------------------------------------------- |
| `AGENTS.md`     | Global agent instructions, The Borda Standard, spawn rules  |
| `config.toml`   | Multi-agent config: 4 runtime profiles, MCP server, sandbox |
| `agents/*.toml` | Per-agent model and reasoning effort overrides              |

→ Deep reference: spawn rules, profiles, architecture, and Claude integration — see [`.codex/README.md` → Agents](.codex/README.md#-agents)

## 🤝 Claude + Codex Integration

Claude and Codex complement each other — Claude handles long-horizon reasoning, orchestration, and judgment calls; Codex handles focused, mechanical in-repo coding tasks with direct shell access.

Every skill that reviews or validates code uses a three-tier pipeline: **Tier 0** (mechanical `git diff --stat` gate), **Tier 1** (codex:review pre-pass, ~60s, diff-focused), **Tier 2** (specialized Claude agents). Cheaper tiers gate the expensive ones — this keeps full agent spawns reserved for diffs that actually need them. → Full architecture with skill-tier matrix: [`.claude/README.md` → Tiered review pipeline](.claude/README.md#tiered-review-pipeline)

**Why unbiased review matters / Real example**: Claude makes targeted changes with intentionality — it has a mental model of which files are "in scope". Codex has no such context: it reads the diff and the codebase independently. During one session, Claude applied a docstring-style mandate across 6 files and scored its own confidence at 0.88. The Codex pre-pass then found `skills/develop/modes/feature.md` still referencing the old style — a direct miss. The union of both passes is more complete than either alone.

### Two integration patterns make this pairing practical

1. **Offloading mechanical tasks from Claude to Codex**

   Claude identifies what needs to change and delegates execution to the plugin agent. Claude keeps its context clean and validates the output via `git diff HEAD`.

   Dispatched automatically by `/review`, `/resolve`, `/calibrate`, and `/optimize run` via `codex-delegation.md`. The plugin agent has full working-tree access.

2. **Codex reviewing staged work**

   After Claude stages changes, `codex:review --wait` serves as a second pass — examining the diff, applying review comments, or resolving PR conflicts. The `/resolve` skill automates this: it resolves conflicts semantically (Claude) then applies review comments (plugin agent).

   ```bash
   /resolve 42   # Claude resolves conflicts → plugin agent applies review comments
   /resolve "rename the `fit` method to `train` throughout the module"
   ```

<details>
<summary><strong>Setup requirement</strong></summary>

Install the Codex plugin in Claude Code:

```bash
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
```

Without the plugin: pre-pass review is skipped gracefully (skills check with `claude plugin list | grep 'codex@openai-codex'`); `/resolve`'s review-comment step is skipped (conflict resolution works with Claude alone).

</details>

## 🔌 MCP Servers

Two optional MCP servers are defined in `.mcp.json` (synced to `~/.claude/.mcp.json` by `/sync apply`). Both are **disabled by default** — enable per-machine in `settings.local.json`.

| Server        | Purpose                                                                                                                                   | Enable                                       |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| **openspace** | Skill auto-evolution — reduces token consumption on repeated tasks via `execute_task`, `search_skills`, `fix_skill`, `upload_skill` tools | add `"openspace"` to `enabledMcpjsonServers` |
| **colab-mcp** | GPU workloads via Google Colab (used by `/optimize run --colab`)                                                                          | add `"colab-mcp"` to `enabledMcpjsonServers` |

→ New-machine setup and full reference: [`.claude/README.md` → MCP Servers](.claude/README.md#-mcp-servers)

## 🪙 Token Savings (RTK)

RTK is an optional CLI proxy that compresses build, test, and git output before it reaches Claude — saving 60–99% of tokens on common operations with no change to your workflow.

**Install** — see [rtk-ai/rtk](https://github.com/rtk-ai/rtk) for platform-specific instructions. Quick options:

```bash
curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/master/install.sh | sh  # Linux / macOS
cargo install --git https://github.com/rtk-ai/rtk                                 # via Cargo
```

> ⚠️ There are two projects named `rtk` on crates.io — always install from `rtk-ai/rtk`, not `reachingforthejack/rtk` (Rust Type Kit). Verify with `rtk gain` after install.

**Verify it's working:**

```bash
rtk gain            # shows actual token savings from this session
rtk gain --history  # per-command savings history
```

**How it integrates with this config:**
A pre-configured `PreToolUse` hook (`.claude/hooks/rtk-rewrite.js`) transparently rewrites supported CLI calls — `git status` becomes `rtk git status` — without needing duplicate entries in `settings.json`. The hook is a no-op when RTK is not installed, so the config stays portable across machines.

| Category     | Commands                               | Typical Savings |
| ------------ | -------------------------------------- | --------------- |
| Tests        | vitest, playwright, cargo test, pytest | 90–99%          |
| Build        | next, tsc, lint, prettier, ruff        | 70–87%          |
| Git / GitHub | git, gh pr, gh run, gh issue           | 26–80%          |
| Packages     | pnpm, npm, pip                         | 70–90%          |
| Files        | ls, grep, find, diff                   | 60–75%          |

**Scope**: RTK only compresses **Bash tool output** — shell commands like `git`, `cargo`, `pytest`, etc. It does not affect Claude Code's native tools (Read, Grep, Glob, Edit, Write), which run inside Claude's own engine and are already token-efficient by design.

**Context reset between heavy skills**: large skills (`/audit`, `/resolve`, `/review`) are loaded into context on invocation and stay there for every subsequent message in that session — `/audit` alone adds ~19K tokens. Use `/clear` between heavy skill invocations or before switching topics. Unlike terminating the session, `/clear` is instant and keeps all config (CLAUDE.md, rules, hooks) intact.

RTK is optional — removing it leaves all functionality intact.

## 🔄 Config Sync

This repo (`.claude/`) is the source of truth — home (`~/.claude/`) is a downstream copy:

```bash
/sync          # show what differs between project and home .claude/
/sync apply    # copy all differing files to ~/.claude/
```

Run after editing any agent, skill, hook, or `settings.json`. `settings.local.json` is never synced.
