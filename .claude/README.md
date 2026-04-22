# 🤖 Claude Code — Deep Reference

← [Back to root README](../README.md) · [Codex deep reference](../.codex/README.md)

Configuration for [Claude Code](https://claude.ai/code) (Anthropic's AI coding CLI). This file covers agent relationships, skill orchestration flows, implementation architecture, and operational internals. For the high-level overview and workflow sequences, see the root README.

<details>
<summary><strong>Contents</strong></summary>

- [♻️ Restore This Setup](#%EF%B8%8F-restore-this-setup)
- [🔄 Distribution](#-distribution)
- [📦 Plugin Architecture](#-plugin-architecture)
- [🔌 Recommended Add-ons](#-recommended-add-ons)
- [🧩 Agents](#-agents)
  - [Reference table](#reference-table)
  - [Agent relationship map](#agent-relationship-map)
- [⚡ Skills](#-skills)
  - [Reference table](#reference-table-1)
  - [Orchestration flow by skill](#orchestration-flow-by-skill)
  - [Skill usage examples](#skill-usage-examples)
- [🗺️ Plugin dependency matrix](#%EF%B8%8F-plugin-dependency-matrix)
- [📐 Rules](#-rules)
  - [Reference table](#reference-table-2)
  - [How rules are auto-loaded](#how-rules-are-auto-loaded)
- [🏗️ Architecture](#-architecture)
  - [File-based handoff protocol](#file-based-handoff-protocol)
  - [Tiered review pipeline](#tiered-review-pipeline)
  - [Agent Teams](#agent-teams)
- [🪝 Hooks](#-hooks)
  - [Hooks inventory](#hooks-inventory)
  - [task-log.js state machine](#task-logjs-state-machine)
  - [Supplementary hooks](#supplementary-hooks)
- [📊 Status Line](#-status-line)
- [🤝 Integration with Codex](#-integration-with-codex)
- [📂 Artifact Layout](#-artifact-layout)

</details>

## ♻️ Restore This Setup

`.claude/` is entirely restored from the installed plugins — there is nothing to manually copy or edit. After a fresh clone or machine setup:

**Step 1** — install the plugins (run from the directory containing your clone):

```bash
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install foundry@borda-ai-rig
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

**Step 2** — run inside Claude Code:

```text
/foundry:init
```

This merges `statusLine`, `permissions.allow`, and `enabledPlugins` (codex plugin) into `~/.claude/settings.json`; symlinks `rules/*.md` and `TEAM_PROTOCOL.md` into `~/.claude/`. Agents, skills, and hooks are exposed natively by the Claude Code plugin system — no symlinks needed.

**What is restored:** `~/.claude/rules/*.md` and `~/.claude/TEAM_PROTOCOL.md` become symlinks into the installed foundry plugin. `~/.claude/settings.json` is updated in-place. All other plugin files (agents, skills, hooks, CLAUDE.md) are served directly by the plugin system. The only local-machine files are `settings.local.json` and `settings.json` (project prefs + permissions).

Re-run `/foundry:init` after any plugin upgrade — rule symlinks point to versioned cache paths and go stale after reinstall.

## 🔄 Distribution

`plugins/foundry/` is the **source of truth** for all foundry configuration. The Claude Code plugin system natively exposes agents and skills; `/foundry:init` symlinks rules and `TEAM_PROTOCOL.md` into `~/.claude/` so they load on every session.

```text
plugins/foundry/           ← source of truth
    rules/*.md             ←── symlinked ──→  ~/.claude/rules/*.md          (init: ln -sf)
    TEAM_PROTOCOL.md       ←── symlinked ──→  ~/.claude/TEAM_PROTOCOL.md    (init: ln -sf)
    agents/*.md            ← plugin system exposes as  foundry:<agent>
    skills/*/SKILL.md      ← plugin system exposes as  foundry:<skill>  and  /<skill>
    hooks/*.js             ← auto-registered via hooks.json  (no init action)
    CLAUDE.md              ← loaded by plugin system per session  (no init action)
    permissions-guide.md   ← in plugin cache; not distributed elsewhere
```

**Distributing to `~/.claude/`** — run after install or upgrade:

```text
/foundry:init   # symlink rules/*.md + TEAM_PROTOCOL.md → ~/.claude/;
                # merge statusLine, permissions.allow, enabledPlugins → ~/.claude/settings.json
                # (re-run after plugin upgrade to refresh stale rule symlinks)
```

**What is NOT distributed:** `settings.local.json` (machine-local overrides — API keys, MCP server activation, local permissions).

**statusLine path:** home `settings.json` uses `$HOME` prefix (`node $HOME/.claude/hooks/statusline.js`) — `/foundry:init` sets this automatically.

## 📦 Plugin Architecture

```text
   ╔══════════════════════════════════╗
   ║  🟠 foundry  [OPTIONAL]          ║
   ╟────────────────────┬─────────────╢
   ║  agents            │  skills     ║
   ║  sw-engineer       │  audit      ║
   ║  qa-specialist     │  calibrate  ║
   ║  linting-expert    │  manage     ║
   ║  perf-optimizer    │  brainstorm ║
   ║  solution-architect│  investigate║
   ║  doc-scribe        │  session    ║
   ║  web-explorer      │  distill    ║
   ║  self-mentor       │             ║
   ╚════════════════════╨═════════════╝
                :
                :····························:····························:
                :                            :                            :
   ╔═════════════════════════╗  ╔═════════════════════════╗  ╔═════════════════════════╗
   ║       🟡 develop        ║  ║        🟢 oss           ║  ║     🟣 research         ║
   ║  agents    │  skills    ║  ║  agents    │  skills    ║  ║  agents    │  skills    ║
   ╟────────────┬────────────╢  ╟────────────┬────────────╢  ╟────────────┬────────────╢
   ║ {sw-eng}   │  feature   ║  ║ ci-guard   │  analyse   ║  ║ scientist  │  topic     ║
   ║ {qa-spec}  │  fix       ║  ║ shepherd   │  review    ║  ║ data-stew  │  plan      ║
   ║ {linting}  │  refactor  ║  ║ {sw-eng}   │  resolve   ║  ║ {sw-eng}   │  judge     ║
   ║ {doc}      │  plan      ║  ║ {qa-spec}  │  release   ║  ║ {linting}  │  run       ║
   ║            │  debug     ║  ║ {linting}  │            ║  ║ {perf-opt} │  sweep     ║
   ║            │  review    ║  ║ {perf-opt} │            ║  ║ {web-exp}  │            ║
   ╚════════════╧════════════╝  ║ {sol-arch} │            ║  ╚════════════╧════════════╝
                                ╚════════════╧════════════╝
   {name} = uses foundry agent (not defined in plugin)
```

Each plugin is fully self-contained and can be installed in any order or combination — every `SKILL.md` carries inline agent fallback tables so that, without foundry, agent dispatches resolve to a `general-purpose` model prefixed with a role description. Installing foundry replaces those fallbacks with purpose-built specialized agents (each tuned to the right model tier and domain constraints), which is the recommended setup. The four plugins are composable: install only what you need, and add foundry whenever you want the quality upgrade.

## 🔌 Recommended Add-ons

Optional external tools that integrate with this setup. All are **disabled by default** and must be enabled per-machine.

### Codex plugin

The [Codex plugin](https://github.com/openai/codex-plugin-cc) adds a local OpenAI Codex agent as a Tier 1 pre-pass reviewer and autonomous executor. Used by `/develop:fix`, `/develop:feature`, `/oss:review`, `/oss:resolve`, `/calibrate`, and `/research:run`.

Install inside Claude Code:

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
```

→ Full invocation map and architecture: [Integration with Codex](#-integration-with-codex)

### OpenSpace (MCP)

[HKUDS/OpenSpace](https://github.com/HKUDS/OpenSpace) is a local MCP server exposing skill-evolving tools (`execute_task`, `search_skills`, `fix_skill`, `upload_skill`) — skills auto-improve through use (~46% fewer tokens on warm reruns). Enable by adding `"openspace"` to `enabledMcpjsonServers` in `settings.local.json`.

### Colab MCP

Used by `/research:run --colab` for GPU workloads via Google Colab. Enable by adding `"colab-mcp"` to `enabledMcpjsonServers` in `settings.local.json`.

MCP servers are defined in `.mcp.json` at the repo root — copy to home: `cp .mcp.json ~/.claude/.mcp.json`.

## 🧩 Agents

### Reference table

| Agent                     | Purpose                                       | Key Capabilities                                                                                                 |
| ------------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| **🟠 sw-engineer**        | Architecture and implementation               | SOLID principles, type safety, clean architecture, doctest-driven dev                                            |
| **🟠 solution-architect** | System design and API planning                | ADRs, interface specs, migration plans, coupling analysis, API surface audit                                     |
| **🟠 qa-specialist**      | Testing and validation                        | pytest, hypothesis, mutation testing, snapshot tests, ML test patterns; auto-includes OWASP Top 10 in teams      |
| **🟠 linting-expert**     | Code quality and static analysis              | ruff, mypy, pre-commit, rule selection strategy, CI quality gates; runs autonomously (`permissionMode: dontAsk`) |
| **🟠 perf-optimizer**     | Performance engineering                       | Profile-first workflow, CPU/GPU/memory/I/O, torch.compile, mixed precision                                       |
| **🟠 doc-scribe**         | Documentation                                 | Google/Napoleon docstrings (no type duplication), Sphinx/mkdocs, API references                                  |
| **🟠 web-explorer**       | Web and docs research                         | API version comparison, migration guides, PyPI tracking, ecosystem compat                                        |
| **🟠 self-mentor**        | Config quality reviewer                       | Agent/skill auditing, duplication detection, cross-ref validation, line budgets                                  |
| **🟢 shepherd**           | Project lifecycle management                  | Issue triage, PR review, SemVer, pyDeprecate, trusted publishing                                                 |
| **🟢 ci-guardian**        | CI/CD reliability                             | GitHub Actions, reusable workflows, trusted publishing, flaky test detection                                     |
| **🟣 scientist**          | ML research and implementation                | Paper analysis, experiment design, LLM evaluation, inference optimization                                        |
| **🟣 data-steward**       | Data lifecycle — acquisition and ML pipelines | API completeness, dataset versioning, split validation, leakage detection, data contracts                        |

### Agent relationship map

Agents are picked in two ways: **by name** (you write "use the qa-specialist to…") or **automatically** when Claude Code spawns subagents via the Task/Agent tool. The selection heuristic matches the task description against each agent's `description:` frontmatter — `/calibrate routing` benchmarks this accuracy.

Key relationships:

- `linting-expert` is always downstream of `sw-engineer` — never lints code that hasn't been implemented yet
- `qa-specialist` is often parallel to `sw-engineer` (reviews) or downstream (validates implementation)
- `doc-scribe` is always downstream — documents finalized code; never shapes design
- `self-mentor` is orthogonal — audits config files, not user code; spawned by `/audit` and `/brainstorm`
- `web-explorer` feeds `scientist` — fetches current docs/papers; scientist interprets and designs experiments
- `shepherd` is the external interface — PR replies, releases, contributor communication; no code implementation

**Model tiering**: reasoning agents (`sw-engineer`, `qa-specialist`, `perf-optimizer`, `scientist`) default to `opus`; plan-gated agents (`solution-architect`, `shepherd`, `self-mentor`) use `opusplan` (plan-gated Opus — pays for reasoning only when the task warrants it); execution agents (`doc-scribe`, `linting-expert`, `ci-guardian`, `data-steward`, `web-explorer`) default to `sonnet`.

## ⚡ Skills

### Reference table

| Skill                | Plugin      | Command                                                            | What It Does                                                                                                                                                                                                                                                                                                                  |
| -------------------- | ----------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **audit**            | 🟠 foundry  | `/audit [scope] fix [high\|medium\|all] \| upgrade`                | Config audit: broken refs, inventory drift, docs freshness; `fix` auto-fixes at the requested severity level; `upgrade` applies docs-sourced improvements (mutually exclusive with `fix`)                                                                                                                                     |
| **manage**           | 🟠 foundry  | `/manage <op> <type>`                                              | Create, update, delete agents/skills/rules; manage `settings.json` permissions (`add perm`/`remove perm`); auto type-detection and cross-ref propagation                                                                                                                                                                      |
| **calibrate**        | 🟠 foundry  | `/calibrate [target] [fast\|full] [apply]`                         | Synthetic benchmarks measuring recall vs confidence bias; `routing` and `communication` modes available                                                                                                                                                                                                                       |
| **brainstorm**       | 🟠 foundry  | `/brainstorm <idea> \| breakdown <tree-or-spec>`                   | Two modes: (1) **idea** — clarifying questions → build divergent branch tree (deepen, close, merge, up to 10 ops) → save tree doc → self-mentor review → gate; (2) **breakdown** — auto-detects input: tree (`Status: tree`) → distillation questions → section-by-section spec; spec (`Status: draft`) → ordered action plan |
| **investigate**      | 🟠 foundry  | `/investigate <symptom>`                                           | Systematic diagnosis for unknown failures — env, tools, hooks, CI divergence; ranks hypotheses and hands off to the right skill                                                                                                                                                                                               |
| **session**          | 🟠 foundry  | `/session [resume\|archive\|summary]`                              | Parking lot for diverging ideas — auto-parks unanswered questions and deferred threads; `resume` shows pending, `archive` closes, `summary` digests the session                                                                                                                                                               |
| **distill**          | 🟠 foundry  | `/distill`                                                         | One-time snapshot: suggest new agents/skills, review roster, prune memory, or consolidate lessons                                                                                                                                                                                                                             |
| **oss:review**       | 🟢 oss      | `/oss:review [file\|PR#] [--reply]`                                | Parallel review across arch, tests, perf, docs, lint, security, API; `--reply` drafts contributor comment                                                                                                                                                                                                                     |
| **oss:analyse**      | 🟢 oss      | `/oss:analyse <N\|health\|ecosystem\|path/to/report.md> [--reply]` | GitHub thread analysis (auto-detects issue/PR/discussion); `health` = repo overview + duplicate clustering                                                                                                                                                                                                                    |
| **oss:resolve**      | 🟢 oss      | `/oss:resolve <PR#\|URL> [report] \| report \| <comment>`          | OSS fast-close: conflicts + review comments via Codex; three source modes: `pr` (live GitHub), `report` (/oss:review findings), `pr + report` (aggregated + deduplicated in one pass)                                                                                                                                         |
| **oss:release**      | 🟢 oss      | `/oss:release <mode> [range]`                                      | Notes, changelog, migration, full prepare pipeline, or readiness `audit`                                                                                                                                                                                                                                                      |
| **develop:feature**  | 🟡 develop  | `/develop:feature <goal>`                                          | TDD-first feature dev: codebase analysis, demo test, TDD loop, docs, review                                                                                                                                                                                                                                                   |
| **develop:fix**      | 🟡 develop  | `/develop:fix <goal>`                                              | Reproduce-first bug fixing: regression test, minimal fix, quality stack                                                                                                                                                                                                                                                       |
| **develop:refactor** | 🟡 develop  | `/develop:refactor <goal>`                                         | Test-first refactor with coverage audit before changing structure                                                                                                                                                                                                                                                             |
| **develop:plan**     | 🟡 develop  | `/develop:plan <goal>`                                             | Scope analysis — produces structured plan without writing implementation code                                                                                                                                                                                                                                                 |
| **develop:debug**    | 🟡 develop  | `/develop:debug <goal>`                                            | Investigation-first debugging: evidence gathering → hypothesis gate → minimal fix                                                                                                                                                                                                                                             |
| **develop:review**   | 🟡 develop  | `/develop:review`                                                  | Six-agent parallel review of local files or current git diff; no GitHub PR needed                                                                                                                                                                                                                                             |
| **research:topic**   | 🟣 research | `/research:topic <topic>`                                          | SOTA literature research with codebase-mapped implementation plan                                                                                                                                                                                                                                                             |
| **research:plan**    | 🟣 research | `/research:plan <goal\|file.py>`                                   | Config wizard: interactive goal → `program.md`; `plan <file.py>` for profile-first bottleneck discovery                                                                                                                                                                                                                       |
| **research:judge**   | 🟣 research | `/research:judge [file]`                                           | Research-supervisor review of experimental methodology (hypothesis, measurement, controls, scope, strategy fit → APPROVED/NEEDS-REVISION/BLOCKED)                                                                                                                                                                             |
| **research:run**     | 🟣 research | `/research:run <goal\|file> [--resume] [--team] [--colab]`         | Metric-driven iteration loop; `--resume` continues after crash; `--team` for parallel exploration; `--colab` for GPU workloads                                                                                                                                                                                                |
| **research:sweep**   | 🟣 research | `/research:sweep <goal\|file>`                                     | Non-interactive pipeline: auto-plan → judge gate → run                                                                                                                                                                                                                                                                        |

### Orchestration flow by skill

Each skill follows a defined topology for how it composes agents:

<details>
<summary><strong>`/oss:review`</strong> — parallel fan-out, then consolidation</summary>

```text
Tier 0: git diff --stat (mechanical gate — skips trivial diffs)
Tier 1: Codex pre-pass (independent diff review, ~60s)
Tier 2: 6 parallel agents — sw-engineer, qa-specialist, perf-optimizer,
        doc-scribe, solution-architect, linting-expert
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
<summary><strong>`/brainstorm`</strong> — conversational spec, then task breakdown</summary>

```text
idea mode:
  Step 1: context scan (Read README, Grep keywords)
  Step 2: AskUserQuestion (clarify, one at a time, max 10)
  Step 3: build tree loop (seed 3–5 branches → deepen/close/merge/add, max 10 ops)
  Step 4: Write tree doc → .plans/blueprint/YYYY-MM-DD-<slug>.md (Status: tree)
  Step 5: self-mentor (tree quality audit — coverage, closure quality, open threads)
  Step 6: AskUserQuestion (approval gate) → suggest /brainstorm breakdown <tree>

breakdown mode (triggered by "breakdown <tree-or-spec>"):
  Auto-detects Status field:
  Status: tree → D1 present summary → D2 distillation questions (max 5)
           → D3 write spec section-by-section → D4 suggest next step
  Status: draft → B1 blocking questions → B2 action plan table → B3 post-plan prompt
```

</details>

<details>
<summary><strong>`/audit`</strong> — self-mentor per file, then consolidation</summary>

```text
per-config-file: self-mentor (reads file, writes findings to /tmp/audit-<ts>/<file>.md)
→ consolidator reads all finding files → ranked report with upgrade proposals
(upgrade mode: web-explorer fetches latest Claude Code docs first)
```

</details>

### Skill usage examples

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

# sweep mode — non-interactive pipeline: auto-plan → judge gate → run
/research:sweep "increase test coverage to 90%"      # automated end-to-end; no user gates
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

**`/manage` — Agent/skill lifecycle**

```text
/manage create agent security-auditor "Security specialist for vulnerability scanning"
/manage update skill optimize perf-audit
/manage delete agent web-explorer
```

**`/audit` — Config health sweep + upgrade**

```text
/audit            # full sweep — report only, includes upgrade proposals table
/audit fix        # auto-fix critical and high findings
/audit upgrade    # apply docs-sourced improvements
/audit agents     # agents only, report only
/audit skills fix # skills only, with auto-fix
```

**`/develop:feature`, `/develop:fix`, `/develop:refactor`, `/develop:plan`, `/develop:debug` — Development workflows**

Each mode enforces a validation gate *before* writing implementation code:

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

**`/investigate` — Systematic failure diagnosis**

```text
/investigate "hooks not firing on Save"
/investigate "codex exec exits 127 on this machine"
/investigate "CI fails but passes locally"
/investigate "/calibrate times out every run"
/investigate "uv run pytest can't find conftest.py"
```

**`/session` — Session parking lot**

```text
/session            # auto-parks current diverging ideas and open questions
/session resume     # show all pending parked items
/session archive    # close all pending items
/session summary    # digest of what happened this session
```

## 🗺️ Plugin dependency matrix

<details>
<summary><strong>Agent short names</strong></summary>

**foundry** 🟠

- `🟠sm` — self-mentor
- `🟠sw` — sw-engineer
- `🟠qa` — qa-specialist
- `🟠lint` — linting-expert
- `🟠arch` — solution-architect
- `🟠perf` — perf-optimizer
- `🟠doc` — doc-scribe
- `🟠web` — web-explorer

**oss** 🟢

- `🟢cig` — ci-guardian
- `🟢shep` — shepherd

**research** 🟣

- `🟣sci` — scientist
- `🟣ds` — data-steward

**ext** 🔷

- `🔷cx` — codex-rescue

</details>

### Agents (inter-agent dependencies)

| Caller ↓ / Called →       | 🟠sm | 🟠sw | 🟠qa | 🟠lint | 🟠arch | 🟠perf | 🟠doc | 🟠web | 🟢cig | 🟢shep | 🟣sci | 🟣ds | 🔷cx |
| ------------------------- | ---- | ---- | ---- | ------ | ------ | ------ | ----- | ----- | ----- | ------ | ----- | ---- | ---- |
| 🟠 **self-mentor**        |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟠 **sw-engineer**        |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟠 **qa-specialist**      |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟠 **linting-expert**     |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟠 **solution-architect** |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟠 **perf-optimizer**     |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟠 **doc-scribe**         |      | °    |      | °      |        |        |       |       |       | °      |       |      |      |
| 🟠 **web-explorer**       |      |      |      |        |        |        |       |       |       |        |       |      |      |
| 🟢 **ci-guardian**        |      |      |      | °      |        |        |       |       |       | °      |       |      |      |
| 🟢 **shepherd**           |      |      |      |        |        |        | °     |       | °     |        |       |      |      |
| 🟣 **scientist**          |      | °    | °    |        |        | °      |       | °     |       |        |       | °    |      |
| 🟣 **data-steward**       |      |      |      |        |        |        |       | →     |       |        | °     |      |      |
| 🔷 **codex-rescue**       |      |      |      |        |        |        |       |       |       |        |       |      |      |

<details>
<summary><strong>Legend</strong></summary>

- **✓** — skill actively spawns this agent (primary expected call)
- **→** — agent actively spawns another agent at runtime (real sub-task delegation)
- **°** — scope boundary: description says "use this agent for X", never spawns directly
- **?** — conditional spawn: which agent is selected depends on runtime strategy
- _(empty cell)_ — no dependency

</details>

### Skills

_Empty rows = no direct agent dispatches (intentional, not an omission). ✓ = always spawned · ? = conditional spawn_

| Skill              | 🟠sm | 🟠sw | 🟠qa | 🟠lint | 🟠arch | 🟠perf | 🟠doc | 🟠web | 🟢shep | 🟣sci | 🟣ds | 🔷cx |
| ------------------ | ---- | ---- | ---- | ------ | ------ | ------ | ----- | ----- | ------ | ----- | ---- | ---- |
| 🟠 **brainstorm**  | ✓    |      |      |        |        |        |       |       |        |       |      |      |
| 🟠 **investigate** |      |      |      |        |        |        |       |       |        |       |      | ✓    |
| 🟠 **audit**       | ✓    | ✓    |      |        |        |        |       | ✓     |        |       |      | ✓    |
| 🟠 **calibrate**   | ✓    | ✓    | ✓    | ✓      | ✓      | ✓      | ✓     | ✓     | ✓      | ✓     | ✓    | ✓    |
| 🟠 **manage**      | ✓    | ✓    |      |        |        |        |       | ✓     |        |       |      |      |
| 🟠 **init**        |      |      |      |        |        |        |       |       |        |       |      |      |
| 🟠 **distill**     | ✓    |      |      |        |        |        |       |       |        |       |      |      |
| 🟠 **session**     |      |      |      |        |        |        |       |       |        |       |      |      |
| 🟢 **review**      |      | ✓    | ✓    | ✓      | ✓      | ✓      | ✓     |       | ✓      |       |      | ✓    |
| 🟢 **analyse**     |      |      |      |        |        |        |       |       | ✓      |       |      |      |
| 🟢 **release**     |      |      |      |        |        |        |       |       | ✓      |       |      |      |
| 🟢 **resolve**     |      | ✓    | ✓    | ✓      |        |        |       |       |        |       |      | ✓    |
| 🟡 **review**      |      | ✓    | ✓    | ✓      | ?      | ✓      | ✓     |       |        |       |      | ✓    |
| 🟡 **feature**     |      | ✓    | ✓    | ✓      |        |        | ✓     |       |        |       |      | ✓    |
| 🟡 **fix**         |      | ✓    | ✓    | ✓      |        |        |       |       |        |       |      | ✓    |
| 🟡 **refactor**    |      | ✓    | ✓    | ✓      |        |        |       |       |        |       |      | ✓    |
| 🟡 **plan**        |      | ✓    | ✓    | ✓      |        |        |       |       |        |       |      |      |
| 🟡 **debug**       |      | ✓    |      |        |        |        |       |       |        |       |      |      |
| 🟣 **topic**       |      |      |      |        | ?      |        |       |       |        | ✓     |      |      |
| 🟣 **run**         |      | ✓    |      | ✓      | ?      | ✓      |       |       |        | ✓     |      | ✓    |
| 🟣 **judge**       |      |      |      |        | ✓      |        |       |       |        | ✓     |      | ✓    |
| 🟣 **plan**        |      |      |      |        | ✓      | ?      |       |       |        | ?     |      |      |
| 🟣 **sweep**       |      | ✓    |      | ✓      | ✓      | ✓      |       |       |        | ✓     |      | ✓    |

## 📐 Rules

### Reference table

| Rule file               | Applies to                      | What it governs                                                                                                          |
| ----------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `artifact-lifecycle.md` | (global)                        | Canonical dot-prefixed artifact layout, run-dir naming, TTL policy                                                       |
| `claude-config.md`      | (global)                        | Universal ops rules: no hardcoded paths, Bash timeouts, two-separate-calls navigation pattern                            |
| `communication.md`      | (global)                        | Re: anchor format, progress narration, tone, output routing, and terminal color conventions                              |
| `external-data.md`      | (global)                        | Pagination and completeness rules for REST, GraphQL, and the `gh` CLI — never work on partial result sets                |
| `foundry-config.md`     | `.claude/**`                    | Plan mode gate for `.claude/` edits, post-edit checklist, XML tag conventions, cleanup hook, settings.json allow entries |
| `git-commit.md`         | (global)                        | Commit message format, push safety (explicit confirmation required), branch safety                                       |
| `python-code.md`        | `**/*.py`                       | Python style: docstrings, deprecation (pyDeprecate), library API freshness checks, version policy, PyTorch AMP           |
| `quality-gates.md`      | (global)                        | Confidence blocks on all analysis tasks, internal quality loop, output routing rules                                     |
| `testing.md`            | `tests/**/*.py`, `**/test_*.py` | pytest AAA structure, parametrize standards, doctest location (source files, not tests)                                  |
| `public-github.md`      | (global)                        | Read-only policy for public GitHub operations — permitted reads and forbidden writes                                     |

### How rules are auto-loaded

Each rule file has `paths:` frontmatter listing glob patterns. Claude Code loads matching rule files automatically when you open or edit a file that matches — no explicit invocation needed. Global rules (no `paths:` restriction, or `paths: "*"`) load in every session. Rules are additive: multiple rules can apply to the same file.

Example: editing `tests/test_transforms.py` auto-loads `testing.md` (matches `tests/**/*.py`) and `python-code.md` (matches `**/*.py`). Editing `.claude/agents/sw-engineer.md` loads `foundry-config.md` (matches `.claude/**`).

## 🏗️ Architecture

### File-based handoff protocol

*When multiple analysis agents return findings inline, the orchestrator's context window fills with intermediate output it never uses directly — file-based handoff keeps the orchestrator clean for decision-making.*

**When it applies:**

- Any skill spawning **2+ agents in parallel** for analysis or review
- Any **single agent** expected to produce >500 tokens of findings
- Exception: implementation agents (writing code) return inline — their output is the deliverable
- Exception: single-agent single-question spawns where output is inherently short (\<200 tokens)

**Agent contract** — the spawned agent must:

1. Write full output to `<RUN_DIR>/<agent-name>.md` using the Write tool
2. Return to the orchestrator **only** a compact JSON envelope on the final line:

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

1. Do NOT read agent files back into main context — delegate to a consolidator agent instead
2. Collect the compact envelopes (tiny — stay in context)
3. Spawn a consolidator to read all `<RUN_DIR>/*.md` files and write the final report

**Threshold:** 4+ agent files → mandatory consolidator; 2–3 files → orchestrator may read directly if total content \<2K tokens.

**RUN_DIR convention:**

- Ephemeral (per-run): `/tmp/<skill>-<timestamp>/` — created once before any spawns
- Persistent (final reports): `.temp/`

**Reference implementations:** `/calibrate` is canonical; `/audit` Step 3 (`self-mentor` per file → consolidator); `/oss:review` Steps 3–6.

______________________________________________________________________

### Tiered review pipeline

Every review skill gates cheap work before spawning expensive agents — cheaper tiers short-circuit the pipeline when the diff is trivial or issues are already clear:

| Tier                     | What it does                                                           | Cost |
| ------------------------ | ---------------------------------------------------------------------- | ---- |
| **T0 — Mechanical gate** | `git diff --stat` — skips trivial or empty diffs before any AI work    | Zero |
| **T1 — Codex pre-pass**  | Focused diff review (~60 s); flags bugs, edge cases, and logic errors  | Low  |
| **T2 — Claude agents**   | Specialized parallel agents (opus for reasoning, sonnet for execution) | High |

Which tiers each skill uses:

| Skill                                                   | T0  | T1  | T2  |
| ------------------------------------------------------- | :-: | :-: | :-: |
| `/develop:feature`, `/develop:fix`, `/develop:refactor` |  ✓  |  ✓  |  ✓  |
| `/oss:review`                                           |  ✓  | ✓ ‡ |  ✓  |
| `/research:run`                                         |  ✓  |  ✓  |  ✓  |
| `/audit fix`                                            |  ✓  |  ✓  |  ✓  |
| `/oss:resolve`                                          |     |     |  ✓  |

‡ For `/oss:review`, Codex runs as a full **co-reviewer** alongside T2 agents — its findings are independently consolidated rather than seeding agent prompts (unbiased review).

______________________________________________________________________

### Agent Teams

Agent Teams is Claude Code's experimental multi-agent feature. Teams are always **user-invoked** — nothing auto-spawns. Auto-spawning teams would multiply token costs 5-10x on routine tasks; explicit invocation lets you make the cost/benefit call per run. Enabled via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `settings.json`.

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

**Model tiering:** Lead uses `opusplan`/`opus`. Deep reasoning teammates (`sw-engineer`, `qa-specialist`, `scientist`, `perf-optimizer`) use `opus`. Execution teammates (`doc-scribe`, `linting-expert`, `ci-guardian`) use `sonnet`. Keep teams to 3–5 teammates (~7× token cost vs single session).

**Communication protocol:** Inter-agent messages use AgentSpeak v2 (defined in `TEAM_PROTOCOL.md`) — ~60% token savings vs natural language. Status codes (`alpha`/`beta`/`gamma`/`delta`/`epsilon`/`omega`), action symbols (`+`/`-`/`~`/`!`), file locking (`+lock`/`-lock`), and priority prefixes (`!!` urgent, `..` FYI). Lead-to-human communication uses normal English.

**Security in teams:** No standalone security agent. `qa-specialist` automatically embeds OWASP Top 10 security checks when the task touches auth, payment flows, or user data.

**Quality hooks:** `hooks/teammate-quality.js` handles `TeammateIdle` (redirects to pending tasks) and `TaskCompleted` (reserved for future quality gates).

## 🪝 Hooks

### Hooks inventory

| Hook                | Event                       | Matcher     | Purpose                |
| ------------------- | --------------------------- | ----------- | ---------------------- |
| task-log.js         | 9 events                    | all         | Session state tracking |
| lint-on-save.js     | PostToolUse                 | Write, Edit | Lint on save           |
| md-compress.js      | PreToolUse                  | Read (.md)  | Token compression      |
| rtk-rewrite.js      | PreToolUse                  | Bash        | CLI output compression |
| teammate-quality.js | TeammateIdle, TaskCompleted | all         | Team quality gate      |
| stats-reader.js     | (standalone)                | n/a         | Session stats          |
| statusline.js       | (statusLine)                | n/a         | Status bar             |

### task-log.js state machine

`task-log.js` is the central event handler. It handles nine Claude Code hook events and maintains runtime state read by `statusline.js`:

**Event → action mapping:**

| Event                | Action                                                                                                                                                                                                                                           |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PreToolUse`         | Logs Task/Agent and Skill invocations to `logs/invocations.jsonl`; opens codex plugin session file; increments per-tool-type state file                                                                                                          |
| `PostToolUse`        | Closes codex plugin session file when any Skill(codex:\*) completes; computes wall-clock timing delta from the PreToolUse start marker and appends to `~/.claude/logs/timings.jsonl`                                                             |
| `PostToolUseFailure` | Records timing with error status to timings.jsonl (same timing path as PostToolUse)                                                                                                                                                              |
| `UserPromptSubmit`   | Writes a queue marker to `state/queue/` to light the processing badge 💬 in statusline                                                                                                                                                           |
| `SubagentStart`      | Creates `state/agents/<id>.json` with agent type, model, color, start timestamp — one file per agent (no race)                                                                                                                                   |
| `SubagentStop`       | Deletes per-agent file; appends completion entry to `invocations.jsonl`                                                                                                                                                                          |
| `PreCompact`         | Appends to `logs/compactions.jsonl`; extracts modified file paths from transcript; writes `state/session-context.md`                                                                                                                             |
| `Stop`               | Clears `state/tools/` — resets the 🔧 row between turns (agents intentionally NOT cleared — may still be running); clears `state/queue/` processing markers (dismisses 💬 badge) and removes orphaned timing start markers from `state/timings/` |
| `SessionEnd`         | Deletes entire `/tmp/claude-state-<session>/` directory (agents, tools, codex, queue, timings, dedup locks); runs `git worktree prune`; removes orphaned worktrees >2h                                                                           |

**State files layout:**

```markdown
/tmp/claude-state-<session>/
├── agents/<id>.json        # one per active subagent (created at start, deleted at stop)
├── codex/<id>.json         # one per active codex plugin session
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

- Agents: 10-minute safety-net — files older than 10 min with no corresponding Stop event indicate a crashed agent; statusline excludes them
- Codex plugin sessions: 30-minute cutoff — stalled plugin sessions are treated as timed out
- Worktrees: 2-hour cutoff in SessionEnd cleanup

**`PostCompact` over-registration:** `PostCompact` is registered in `settings.json` for `task-log.js` but is handled as a no-op — the code handles `PreCompact` instead.

**Inline `SessionStart` hooks** (shell commands, not JS files): (1) `claude auth status > ~/.claude/state/subscription.json` — snapshots billing plan for the status line billing indicator, async; (2) `rm -f .claude/state/session-context.md` — clears last session's breadcrumb on fresh startup.

### Supplementary hooks

Registered alongside `task-log.js` in `settings.json`:

**`lint-on-save.js`** (PostToolUse — Write, Edit) — closes the gap between "Claude edits a file" and "a human runs pre-commit" by linting every file the moment it is written. Runs `pre-commit run --files <path>` on each Write/Edit, exits 2 on failure so Claude sees the diagnostics and applies a fix immediately. No-op when `.pre-commit-config.yaml` is absent or pre-commit is not installed.

**`md-compress.js`** (PreToolUse — Read, `.md` files only) — transparently compresses token-wasteful whitespace in Markdown files before Claude reads them, reducing context consumption without altering content. Collapses table column padding (2+ spaces → 1), consecutive blank lines, and trailing whitespace — all outside fenced code blocks. Writes to a stable temp file keyed by source-path hash; reused within a session when the source is unchanged.

**`rtk-rewrite.js`** (PreToolUse — Bash) — rewrites supported CLI calls to go through the RTK proxy (`git status` → `rtk git status`) for 60–99% token savings on build/test/git output. RTK is a *structural* compressor — it understands git diff, pytest, and build-log formats and removes tokens that are visually useful to humans but informationally redundant for an LLM, unlike generic truncation which can drop the relevant parts. No-op when RTK is not installed — see root [README → Token Savings](../README.md#-token-savings-rtk).

**Session stats utility** — `hooks/stats-reader.js` is a standalone script (not a hook event) for inspecting session token and tool usage from JSONL history. Run directly:

```bash
node .claude/hooks/stats-reader.js --latest              # most recent session
node .claude/hooks/stats-reader.js --latest --timings    # + per-tool wall-clock stats from timings.jsonl
node .claude/hooks/stats-reader.js --date 2026-04-08     # all sessions on a date
node .claude/hooks/stats-reader.js <session-uuid>        # specific session by UUID prefix
```

Output: JSON with token usage by model (input/output/cache), tool call counts, turn count, duration, and optional timing percentiles (`count`, `mean_ms`, `p95_ms`) per tool.

## 📊 Status Line

A lightweight hook (`hooks/statusline.js`) adds a persistent two-row status bar to every Claude Code session:

```text
Row 1:  claude-sonnet-4-6 │ Borda.AI-Rig │ Pro ~$1.20 │ ████░░░░░░ 38% │ 💬
Row 2:  🕵 2 agents (self-mentor, sw-engineer) │ 🤖 codex-rescue │ 🔧 Bash ×3 · Edit · Read ×12
```

**Row 1** — model name · project directory · billing indicator · 10-segment context bar (green → yellow → red) · processing badge `💬` (cyan; shown while Claude is handling the current turn; disappears when done)

**Row 2** — native agent count · Codex sessions (separate) · active tools (last 30 seconds)

**Agent row (`🕵`) details:**

- Specialized agents (have a `.claude/agents/` file) → shown by type name in their declared `color:` from frontmatter
- General-purpose agents → shown by model name in gray (`opus`, `sonnet`)
- Same-type agents grouped with `×N` count
- **`codex:*` subagents are excluded here** — they appear in `🤖` instead

**Codex row (`🤖`) details:**

- Shows the short name of each active codex session, without the `codex:` prefix (e.g., `codex-rescue`, `review`, `adversarial-review`)
- Sources: both `Skill(codex:*)` invocations and `Agent(subagent_type="codex:*")` subagents
- Multiple sessions of the same type grouped as `<name> ×N`
- Safety-net: sessions older than 30 min are treated as timed out and excluded

**Tool row colors:** `Read` (blue) · `Write` (bright green) · `Edit` (green) · `Bash` (yellow) · `Grep` (cyan) · `Glob` (bright cyan) · `WebFetch` (magenta) · `WebSearch` (bright magenta) · `Task`/`Agent` (bright blue) · `Skill` (bright yellow)

**Billing indicator:**

- **Subscription (Pro/Max):** `Max/Pro/Sub ~$X.XX` in cyan — plan from `~/.claude/state/subscription.json`; `~$X.XX` is theoretical API-rate cost (tokens × list price), not an actual charge
- **API key:** `API $X.XX` in yellow — actual spend at pay-per-token rates

**Hook mechanics:** `statusline.js` reads `state/agents/`, `state/codex/`, `state/tools/`, and `state/queue/` on each render. `task-log.js` writes those files (including `UserPromptSubmit` → queue markers, `Stop` → queue drain); `statusline.js` only reads. Configured via `statusLine` in `settings.json`. Zero external dependencies — stdlib `path` and `fs` only.

## 🤝 Integration with Codex

→ Full architecture: [root README → Claude + Codex integration](../README.md#-claude--codex-integration)

→ Install: see [Recommended Add-ons → Codex plugin](#-recommended-add-ons)

### Skills digestion

Skills check availability at runtime: `claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'`. If the plugin is absent, each skill skips its Codex step gracefully rather than failing.

**Invocation map** — every place Claude dispatches to Codex and why:

| Skill                              | Site                          | Purpose                                                                              | Plugin command                            |
| ---------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------ | ----------------------------------------- |
| `/develop:fix`, `/develop:feature` | `_shared/codex-prepass.md`    | Tier 1 pre-pass: review staged diff for bugs before Claude's review cycle            | `codex:review --wait`                     |
| `/oss:review`                      | Step 2 co-review              | Adversarial diff review seeding agent prompts with pre-flagged issues                | `codex:adversarial-review --wait <focus>` |
| `/oss:review`, `/research:run`     | `_shared/codex-delegation.md` | Delegate mechanical follow-up: docstrings, type annotations, test stubs              | `codex:codex-rescue` (agent)              |
| `/oss:resolve`                     | Step 8 action items           | Apply PR review feedback to the codebase                                             | `codex:codex-rescue` (agent)              |
| `/oss:resolve`                     | Step 12a comment dispatch     | Apply a specific review comment                                                      | `codex:codex-rescue` (agent)              |
| `/oss:resolve`                     | Step 12 review loop           | Review applied changes for issues before committing                                  | `codex:review --wait`                     |
| `/research:run --codex`            | Phase 2b ideation             | Fallback: generate + apply one atomic optimization when Claude's change was reverted | `codex:codex-rescue` (agent)              |
| `/calibrate`                       | Phase 1a problem gen          | Generate synthetic calibration problems (JSON array written to run dir)              | `codex:codex-rescue` (agent)              |
| `/calibrate`                       | Phase 2 scoring               | Score calibration responses against ground truth (JSON written to run dir)           | `codex:codex-rescue` (agent)              |

**What Claude retains:**

- Long-horizon planning and research (`/research:topic`, `/research:run`, `/develop:plan`)
- Orchestration of multiple agents in defined topologies
- Judgment calls: design decisions, spec approval, test validity assessment
- Final validation: Claude always verifies Codex output via `git diff HEAD` before accepting changes

**Why the division works:** Claude has a mental model of which files are "in scope" for a task; Codex reads the diff and codebase independently, without that context. Their blind spots are complementary — the union of both passes catches more than either alone.

## 📂 Artifact Layout

Runtime artifacts live at the project root in dot-prefixed dirs — separate from versioned config in `.claude/`. The dot-prefix signals "generated output, not source".

```text
.plans/blueprint/        ← /brainstorm spec and tree files
.plans/active/           ← todo_*.md, plan_*.md
.plans/closed/           ← completed plans
.notes/                  ← lessons.md, diary, guides
.reports/calibrate/      ← /calibrate benchmark runs
.reports/resolve/        ← /oss:resolve lint+QA gate outputs
.reports/audit/          ← /audit analysis runs
.reports/review/         ← /oss:review multi-agent outputs
.experiments/            ← /research:run skill runs (improve mode)
.developments/           ← /develop:* review-cycle handoffs
.temp/                   ← long output from any skill (quality-gates rule)
```

Each skill creates a timestamped run dir: `.reports/<skill>/YYYY-MM-DDTHH-MM-SSZ/`. Completed runs contain `result.jsonl`; the `SessionEnd` hook deletes completed runs older than 30 days automatically. Incomplete runs (crashed/timed-out) are kept for debugging. All dot-prefixed dirs are gitignored — see `.claude/rules/artifact-lifecycle.md` for TTL policy and full details.
