# 🤖 Claude Code — Deep Reference

← [Back to root README](../README.md) · [Codex deep reference](../.codex/README.md)

Configuration for [Claude Code](https://claude.ai/code) (Anthropic's AI coding CLI). This file covers agent relationships, skill orchestration flows, implementation architecture, and operational internals. For the high-level overview and workflow sequences, see the root README.

<details>
<summary><strong>Contents</strong></summary>

- [🔄 Config Sync](#-config-sync)
- [🔌 MCP Servers](#-mcp-servers)
- [🧩 Agents](#-agents)
  - [Reference table](#reference-table)
  - [Agent relationship map](#agent-relationship-map)
- [⚡ Skills](#-skills)
  - [Reference table](#reference-table-1)
  - [Orchestration flow by skill](#orchestration-flow-by-skill)
  - [Skill usage examples](#skill-usage-examples)
- [📐 Rules](#-rules)
  - [Reference table](#reference-table-2)
  - [How rules are auto-loaded](#how-rules-are-auto-loaded)
- [🏗️ Architecture](#-architecture)
  - [File-based handoff protocol](#file-based-handoff-protocol)
  - [Tiered review pipeline](#tiered-review-pipeline)
  - [Hook state machine](#hook-state-machine)
  - [Agent Teams](#agent-teams)
- [📊 Status Line](#-status-line)
- [🤝 Integration with Codex](#-integration-with-codex)
- [📂 Artifact Layout](#-artifact-layout)

</details>

## 🔄 Config Sync

This repo is the **source of truth** for all `.claude/` configuration. Home (`~/.claude/`) is a downstream copy kept in sync via the `/sync` skill.

```
.claude/   (source)       →   ~/.claude/   (downstream)
  agents/                       agents/
  skills/                       skills/
  rules/                        rules/
  hooks/statusline.js           hooks/statusline.js
  settings.json                 settings.json  (statusLine path rewritten to absolute)
  CLAUDE.md                     CLAUDE.md
```

**What is NOT synced:** `settings.local.json` (machine-local overrides — API keys, MCP server activation, local permissions).

**Workflow:**

```bash
/sync          # dry-run: show drift report (MISSING / DIFFERS / IDENTICAL per file)
/sync apply    # apply: copy all differing files and verify outcome
```

Run `/sync` after editing any agent, skill, hook, or `settings.json` in this repo to propagate the change to home config.

**Path rewriting:** `statusLine` and hook paths in home `settings.json` use `$HOME` prefix (`node $HOME/.claude/hooks/statusline.js`) — portable, avoids hardcoded usernames. The `/sync` skill applies this rewrite automatically.

## 🔌 MCP Servers

Two optional MCP servers are defined in `.mcp.json` at the repo root (synced to `~/.claude/.mcp.json` via `/sync apply`). Both are **disabled by default** and must be enabled per-machine.

### openspace

Connects [HKUDS/OpenSpace](https://github.com/HKUDS/OpenSpace) as a local MCP server. Exposes four tools — `execute_task`, `search_skills`, `fix_skill`, `upload_skill` — that let Claude delegate tasks to OpenSpace's skill-evolving runtime. Skills auto-improve through use; the benchmark reports ~46% fewer tokens on warm reruns.

**New-machine setup:**

```bash
# 1. Install OpenSpace globally via pipx (Python ≥ 3.12 required)
brew install pipx
pipx install https://github.com/HKUDS/OpenSpace/archive/refs/heads/main.zip --python python3.12
~/.local/bin/openspace-mcp --help   # smoke test

# 2. Update the command path in .mcp.json if your username differs:
#    "/Users/<you>/.local/bin/openspace-mcp"

# 3. Make the server available globally (user-level config):
cp .mcp.json ~/.claude/.mcp.json
# Note: /sync apply syncs .claude/ contents only; .mcp.json at the repo root
# must be copied manually. The project-level .mcp.json already loads when
# Claude Code runs inside this repo.

# 4. Enable for the current session
# Add "openspace" to enabledMcpjsonServers in .claude/settings.local.json:
#   "enabledMcpjsonServers": ["openspace"]

# 5. Restart Claude Code — openspace tools appear in the MCP tool list
```

**Runtime data** lives at `~/.claude/openspace/` (SQLite lineage DB + execution recordings). Not version-controlled, not synced. Persists across sessions.

**Conflict policy:** existing hand-crafted `SKILL.md` files in `~/.claude/skills/` are protected with `chmod 444` after setup — OpenSpace cannot overwrite them. Remove the protection with `chmod 644 ~/.claude/skills/<name>/SKILL.md` only when you intend to let OpenSpace evolve that skill.

**Disable:** remove `"openspace"` from `enabledMcpjsonServers` in `settings.local.json`.

### colab-mcp

Used by `/optimize run --colab` for GPU workloads via Google Colab. See the `/optimize` skill examples for usage. Enable by adding `"colab-mcp"` to `enabledMcpjsonServers`.

## 🧩 Agents

### Reference table

| Agent                  | Purpose                                       | Key Capabilities                                                                                                 |
| ---------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| **sw-engineer**        | Architecture and implementation               | SOLID principles, type safety, clean architecture, doctest-driven dev                                            |
| **solution-architect** | System design and API planning                | ADRs, interface specs, migration plans, coupling analysis, API surface audit                                     |
| **oss-shepherd**       | Project lifecycle management                  | Issue triage, PR review, SemVer, pyDeprecate, trusted publishing                                                 |
| **ai-researcher**      | ML research and implementation                | Paper analysis, experiment design, LLM evaluation, inference optimization                                        |
| **qa-specialist**      | Testing and validation                        | pytest, hypothesis, mutation testing, snapshot tests, ML test patterns; auto-includes OWASP Top 10 in teams      |
| **linting-expert**     | Code quality and static analysis              | ruff, mypy, pre-commit, rule selection strategy, CI quality gates; runs autonomously (`permissionMode: dontAsk`) |
| **perf-optimizer**     | Performance engineering                       | Profile-first workflow, CPU/GPU/memory/I/O, torch.compile, mixed precision                                       |
| **ci-guardian**        | CI/CD reliability                             | GitHub Actions, reusable workflows, trusted publishing, flaky test detection                                     |
| **data-steward**       | Data lifecycle — acquisition and ML pipelines | API completeness, dataset versioning, split validation, leakage detection, data contracts                        |
| **doc-scribe**         | Documentation                                 | Google/Napoleon docstrings (no type duplication), Sphinx/mkdocs, API references                                  |
| **web-explorer**       | Web and docs research                         | API version comparison, migration guides, PyPI tracking, ecosystem compat                                        |
| **self-mentor**        | Config quality reviewer                       | Agent/skill auditing, duplication detection, cross-ref validation, line budgets                                  |

### Agent relationship map

Agents are picked in two ways: **by name** (you write "use the qa-specialist to…") or **automatically** when Claude Code spawns subagents via the Task/Agent tool. The selection heuristic matches the task description against each agent's `description:` frontmatter — `/calibrate routing` benchmarks this accuracy.

Key relationships:

- `linting-expert` is always downstream of `sw-engineer` — never lints code that hasn't been implemented yet
- `qa-specialist` is often parallel to `sw-engineer` (reviews) or downstream (validates implementation)
- `doc-scribe` is always downstream — documents finalized code; never shapes design
- `self-mentor` is orthogonal — audits config files, not user code; spawned by `/distill`, `/audit`, `/brainstorm`
- `web-explorer` feeds `ai-researcher` — fetches current docs/papers; researcher interprets and designs experiments
- `oss-shepherd` is the external interface — PR replies, releases, contributor communication; no code implementation

**Model tiering**: reasoning agents (`sw-engineer`, `qa-specialist`, `perf-optimizer`, `ai-researcher`, `solution-architect`, `oss-shepherd`) default to `opus`; execution agents (`doc-scribe`, `linting-expert`, `ci-guardian`, `data-steward`, `web-explorer`) default to `sonnet`; `self-mentor` uses `opusplan` (plan-gated opus).

## ⚡ Skills

### Reference table

| Skill           | Command                                                  | What It Does                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **review**      | `/review [file\|PR#] [--reply]`                          | Parallel review across arch, tests, perf, docs, lint, security, API; `--reply` drafts contributor comment                                                                                                                                                                                                                                                                                                                                                          |
| **analyse**     | `/analyse <N\|health\|ecosystem> [--reply]`              | GitHub thread analysis (auto-detects issue/PR/discussion); `health` = repo overview + duplicate clustering                                                                                                                                                                                                                                                                                                                                                         |
| **brainstorm**  | `/brainstorm <idea> \| breakdown <tree-or-spec>`         | Two modes: (1) **idea** — clarifying questions → build divergent branch tree (deepen, close, merge, up to 10 ops) → save tree doc → self-mentor review → gate; (2) **breakdown** — auto-detects input: tree (`Status: tree`) → distillation questions → section-by-section spec; spec (`Status: draft`) → ordered action plan                                                                                                                                      |
| **develop**     | `/develop feature\|fix\|refactor\|plan\|debug <goal>`    | TDD-first feature dev, reproduce-first bug fixing, test-first refactor, scope analysis (`plan`), or investigation-first debugging (`debug`)                                                                                                                                                                                                                                                                                                                        |
| **resolve**     | `/resolve <PR#\|URL> [report] \| report \| <comment>`    | OSS fast-close: conflicts + review comments via Codex; three source modes: `pr` (live GitHub), `report` (/review findings), `pr + report` (aggregated + deduplicated in one pass)                                                                                                                                                                                                                                                                                  |
| **calibrate**   | `/calibrate [target] [fast\|full] [apply]`               | Synthetic benchmarks measuring recall vs confidence bias; `routing` and `communication` modes available                                                                                                                                                                                                                                                                                                                                                            |
| **audit**       | `/audit [scope] fix [high\|medium\|all] \| upgrade`      | Config audit: broken refs, inventory drift, docs freshness; `fix` auto-fixes at the requested severity level; `upgrade` applies docs-sourced improvements (mutually exclusive with `fix`)                                                                                                                                                                                                                                                                          |
| **release**     | `/release <mode> [range]`                                | Notes, changelog, migration, full prepare pipeline, or readiness `audit`                                                                                                                                                                                                                                                                                                                                                                                           |
| **research**    | `/research <topic> \| plan [path]`                       | SOTA literature research with implementation plan; `plan` mode produces a phased, codebase-mapped implementation plan (auto-detects latest research output)                                                                                                                                                                                                                                                                                                        |
| **optimize**    | `/optimize plan\|judge\|run\|resume\|sweep <goal\|file>` | Five modes: `plan` = config wizard (or `plan <file.py>` for profile-first bottleneck discovery) → `program.md`; `judge` = research-supervisor review of experimental methodology (hypothesis, measurement, controls, scope, strategy fit → APPROVED/NEEDS-REVISION/BLOCKED); `run` = metric-driven iteration loop; `resume` = continue after crash/stop; `sweep` = non-interactive pipeline (auto-plan → judge gate → run); `--team` and `--colab` (GPU) supported |
| **manage**      | `/manage <op> <type>`                                    | Create, update, delete agents/skills/rules; manage `settings.json` permissions (`add perm`/`remove perm`); auto type-detection and cross-ref propagation                                                                                                                                                                                                                                                                                                           |
| **sync**        | `/sync [apply]`                                          | Drift-detect and sync project `.claude/` and `.codex/` → home `~/.claude/` and `~/.codex/`                                                                                                                                                                                                                                                                                                                                                                         |
| **investigate** | `/investigate <symptom>`                                 | Systematic diagnosis for unknown failures — env, tools, hooks, CI divergence; ranks hypotheses and hands off to the right skill                                                                                                                                                                                                                                                                                                                                    |
| **session**     | `/session [resume\|archive\|summary]`                    | Parking lot for diverging ideas — auto-parks unanswered questions and deferred threads; `resume` shows pending, `archive` closes, `summary` digests the session                                                                                                                                                                                                                                                                                                    |
| **distill**     | `/distill`                                               | One-time snapshot: suggest new agents/skills, review roster, prune memory, or consolidate lessons                                                                                                                                                                                                                                                                                                                                                                  |

### Orchestration flow by skill

Each skill follows a defined topology for how it composes agents:

**`/review`** — parallel fan-out, then consolidation:

```
Tier 0: git diff --stat (mechanical gate — skips trivial diffs)
Tier 1: Codex pre-pass (independent diff review, ~60s)
Tier 2: 6 parallel agents — sw-engineer, qa-specialist, perf-optimizer,
        doc-scribe, solution-architect, linting-expert
→ consolidator reads all findings → final report
→ oss-shepherd writes --reply output (if flag present)
```

**`/develop feature`** — sequential with inner loops:

```
Step 1: sw-engineer (codebase analysis)
Step 2: sw-engineer (demo test — TDD contract)
Step 2 review: in-context validation gate
Step 3: sw-engineer (implementation) + qa-specialist (parallel)
Step 4: review+fix loop (max 3 cycles): sw-engineer → qa-specialist → linting-expert
Step 5: doc-scribe (docs update)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

**`/develop fix`** — reproduce-first:

```
Step 1: sw-engineer (root cause analysis)
Step 2: sw-engineer (regression test that fails)
Step 2 review: in-context validation gate
Step 3: sw-engineer (minimal fix)
Step 4: review+fix loop (max 3 cycles)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

**`/develop refactor`** — test-first:

```
Step 1: sw-engineer + linting-expert (coverage audit, parallel)
Step 2: qa-specialist (characterization tests)
Step 2 review: in-context validation gate
Step 3: sw-engineer (refactor)
Step 5: review+fix loop (max 3 cycles)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

**`/research`** — research-first:

```
web-explorer (fetch current papers/docs) → ai-researcher (deep analysis, writes to file)
→ consolidator reads findings → implementation plan
(--team: multiple ai-researcher instances on competing method families)
```

**`/brainstorm`** — conversational spec, then task breakdown:

```
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

**`/audit`** — self-mentor per file, then consolidation:

```
per-config-file: self-mentor (reads file, writes findings to /tmp/audit-<ts>/<file>.md)
→ consolidator reads all finding files → ranked report with upgrade proposals
(upgrade mode: web-explorer fetches latest Claude Code docs first)
```

### Skill usage examples

<details>
<summary><strong>`/optimize` — Profile-first bottleneck discovery and run mode</strong></summary>

```bash
# plan mode — interactive config wizard → program.md
/optimize plan "increase test coverage to 90%"
/optimize plan src/mypackage/train.py           # profile-first: cProfile → ask what to optimize → wizard
/optimize plan "improve F1 from 0.82 to 0.87" coverage.md  # write to custom path

# judge mode — pre-flight quality gate before the expensive run loop
/optimize judge                    # review program.md methodology → APPROVED / NEEDS-REVISION / BLOCKED
/optimize judge coverage.md        # audit a specific program file
/optimize judge --skip-validation  # skip local metric/guard validation (cross-machine workflows)

# run mode — sustained metric-improvement loop
/optimize run "increase test coverage to 90%"        # run from text goal (20-iteration loop; auto-rollback on regression)
/optimize run coverage.md                            # run from program.md config file

# resume mode — continue after crash or manual stop
/optimize resume                                         # reads program_file from state.json
/optimize resume coverage.md                            # resume specific run

# sweep mode — non-interactive pipeline: auto-plan → judge gate → run
/optimize sweep "increase test coverage to 90%"         # automated end-to-end; no user gates
/optimize sweep coverage.md                             # sweep from program.md config

# flags (plan/run/resume/sweep)
/optimize run "reduce training time by 20%" --team   # parallel exploration across axes
/optimize run "improve validation accuracy" --colab  # GPU workloads via Colab MCP (opt-in)
```

> **Colab MCP is opt-in.** `.mcp.json` defines the server but does not start it. To enable: add `"colab-mcp"` to `enabledMcpjsonServers` in `.claude/settings.local.json`, then restart Claude Code.

</details>

<details>
<summary><strong>`/review` — Parallel code review</strong></summary>

```bash
/review 42          # review PR by number
/review src/mypackage/transforms.py
/review             # review latest commit
/review 42 --reply  # review + draft contributor-facing comment
```

</details>

<details>
<summary><strong>`/analyse` — Issue, PR, Discussion and repo health</strong></summary>

```bash
/analyse 123           # auto-detects issue/PR/discussion; wide-net related search
/analyse health        # repo health overview with duplicate clustering
/analyse ecosystem     # downstream consumer impact analysis
/analyse 123 --reply   # analyse + draft contributor reply
```

</details>

<details>
<summary><strong>`/release` — Release notes, changelog, readiness checks</strong></summary>

```bash
/release notes v1.2.0..HEAD
/release changelog v1.2.0..HEAD
/release prepare v2.0.0
/release audit
```

</details>

<details>
<summary><strong>`/sync` — Config drift detection</strong></summary>

```bash
/sync          # dry-run: show what differs between project and home .claude/
/sync apply    # apply: copy differing files to ~/.claude/
```

</details>

<details>
<summary><strong>`/manage` — Agent/skill lifecycle</strong></summary>

```bash
/manage create agent security-auditor "Security specialist for vulnerability scanning"
/manage update skill optimize perf-audit
/manage delete agent web-explorer
```

</details>

<details>
<summary><strong>`/audit` — Config health sweep + upgrade</strong></summary>

```bash
/audit            # full sweep — report only, includes upgrade proposals table
/audit fix        # auto-fix critical and high findings
/audit upgrade    # apply docs-sourced improvements
/audit agents     # agents only, report only
/audit skills fix # skills only, with auto-fix
```

</details>

<details>
<summary><strong>`/develop` — Unified development orchestrator</strong></summary>

Each mode enforces a validation gate *before* writing implementation code:

- `plan` — scope analysis; produces structured plan in `.plans/active/plan_<slug>.md`
- `feature` — TDD demo validation before writing code
- `fix` — reproduction test before touching anything
- `refactor` — coverage audit before changing structure
- `debug` — investigation-first; evidence gathering → hypothesis gate → minimal fix

```bash
/develop feature add batched predict() method to Classifier
/develop fix TypeError when passing None to transform()
/develop refactor src/mypackage/transforms.py
/develop plan improve caching in the data loader
/develop debug why does the validation loss spike at epoch 3?
```

</details>

<details>
<summary><strong>`/resolve` — Resolve a PR end-to-end</strong></summary>

```bash
/resolve 42                                              # pr mode: live GitHub comments → conflict check → semantic resolution → action items
/resolve https://github.com/org/repo/pull/42             # same as above, URL form
/resolve report                                          # report mode: latest /review findings as action items; no GitHub re-fetch
/resolve 42 report                                       # pr + report mode: GitHub comments + /review findings, aggregated and deduplicated
/resolve "rename foo to bar throughout the auth module"  # single-comment fast path (comment dispatch mode)
```

</details>

<details>
<summary><strong>`/investigate` — Systematic failure diagnosis</strong></summary>

```bash
/investigate "hooks not firing on Save"
/investigate "codex exec exits 127 on this machine"
/investigate "CI fails but passes locally"
/investigate "/calibrate times out every run"
/investigate "uv run pytest can't find conftest.py"
```

</details>

<details>
<summary><strong>`/session` — Session parking lot</strong></summary>

```bash
/session            # auto-parks current diverging ideas and open questions
/session resume     # show all pending parked items
/session archive    # close all pending items
/session summary    # digest of what happened this session
```

</details>

## 📐 Rules

### Reference table

| Rule file                         | Applies to                                      | What it governs                                                                                                                               |
| --------------------------------- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `artifact-lifecycle.md`           | `.claude/**`                                    | Canonical dot-prefixed artifact layout, run-dir naming, TTL policy, SessionEnd cleanup hook, settings.json entries                            |
| `ci-workflows.md`                 | `.github/workflows/**/*.yml`                    | Semantic version tags preferred over SHA pins; Python matrix ≥3.10; fail-fast rules                                                           |
| `claude-config.md`                | `.claude/**`                                    | Checklist for editing `.claude/` files: cross-refs, MEMORY.md roster, README, sync                                                            |
| `communication.md`                | (global)                                        | Re: anchor format, progress narration, tone, output routing, and terminal color conventions                                                   |
| `external-data.md`                | (global)                                        | Pagination and completeness rules for REST, GraphQL, and the `gh` CLI — never work on partial result sets                                     |
| `git-commit.md`                   | (global)                                        | Commit message format, push safety (explicit confirmation required), branch safety                                                            |
| `hooks-js.md`                     | `.claude/hooks/*.js`                            | Hook writing standards: state files, age-out patterns, tool activity tracking                                                                 |
| `pre-commit-config.md`            | `.pre-commit-config.yaml`                       | Version pinning rules, hook ordering, CI integration via pre-commit.ci                                                                        |
| `python-code.md`                  | `**/*.py`                                       | Python style: docstrings, deprecation (pyDeprecate), library API freshness checks, version policy, PyTorch AMP                                |
| `quality-gates.md`                | (global)                                        | Confidence blocks on all analysis tasks, internal quality loop, output routing rules                                                          |
| `release-notes.md`                | `CHANGELOG.md`, `PUBLIC-NOTES.md`               | Release note structure, SemVer decision criteria, deprecation notice format                                                                   |
| `optimize-hypothesis-protocol.md` | `.experiments/**`, `.claude/skills/optimize/**` | JSONL schema for `hypotheses.jsonl` and `checkpoint.json`; `diary.md` entry format; feasibility filter rules for `/optimize run --researcher` |
| `testing.md`                      | `tests/**/*.py`, `**/test_*.py`                 | pytest AAA structure, parametrize standards, doctest location (source files, not tests)                                                       |

### How rules are auto-loaded

Each rule file has `paths:` frontmatter listing glob patterns. Claude Code loads matching rule files automatically when you open or edit a file that matches — no explicit invocation needed. Global rules (no `paths:` restriction, or `paths: "*"`) load in every session. Rules are additive: multiple rules can apply to the same file.

Example: editing `tests/test_transforms.py` auto-loads `testing.md` (matches `tests/**/*.py`) and `python-code.md` (matches `**/*.py`). Editing `.claude/agents/sw-engineer.md` loads `claude-config.md` (matches `.claude/**`).

## 🏗️ Architecture

### File-based handoff protocol

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

**Reference implementations:** `/calibrate` is canonical; `/audit` Step 3 (`self-mentor` per file → consolidator); `/review` Steps 3–6.

______________________________________________________________________

### Tiered review pipeline

Every skill that reviews or validates code uses a three-tier pipeline. Cheaper tiers gate the expensive ones:

| Tier                          | What                                                                   | Cost | When                               |
| ----------------------------- | ---------------------------------------------------------------------- | ---- | ---------------------------------- |
| **0 — Mechanical gate**       | `git diff --stat` — skip trivial diffs                                 | Zero | Always (built into codex-prepass)  |
| **1 — codex:review pre-pass** | Diff-focused review (~60s) — flags bugs, edge cases, logic errors      | Low  | Before expensive agent spawns      |
| **2 — Claude agents**         | Specialized parallel agents (opus for reasoning, sonnet for execution) | High | Full review, audit, implementation |

| Skill                                  | Tier 0 | Tier 1 (codex:review) | Tier 2 (Claude agents) |
| -------------------------------------- | :----: | :-------------------: | :--------------------: |
| `/develop` (feature/fix/refactor/plan) |   ✓    |           ✓           |           ✓            |
| `/review`                              |   ✓    |          ✓ †          |           ✓            |
| `/optimize`                            |   ✓    |           ✓           |           ✓            |
| `/audit fix`                           |   ✓    |           ✓           |           ✓            |
| `/resolve`                             |   —    |           —           |           ✓            |

† For `/review`, the codex plugin runs as a full **co-reviewer** alongside Tier 2 agents — its findings are independently consolidated, not used to seed agent prompts (unbiased review).

______________________________________________________________________

### Hook state machine

`task-log.js` is the central event handler. It handles six Claude Code hook events and maintains runtime state read by `statusline.js`:

**Event → action mapping:**

| Event           | Action                                                                                                                                  |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `PreToolUse`    | Logs Task/Agent and Skill invocations to `logs/invocations.jsonl`; opens codex plugin session file; increments per-tool-type state file |
| `PostToolUse`   | Closes codex plugin session file when any Skill(codex:\*) completes                                                                     |
| `SubagentStart` | Creates `state/agents/<id>.json` with agent type, model, color, start timestamp — one file per agent (no race)                          |
| `SubagentStop`  | Deletes per-agent file; appends completion entry to `invocations.jsonl`                                                                 |
| `PreCompact`    | Appends to `logs/compactions.jsonl`; extracts modified file paths from transcript; writes `state/session-context.md`                    |
| `Stop`          | Clears `state/tools/` — resets the 🔧 row between turns (agents intentionally NOT cleared — may still be running)                       |
| `SessionEnd`    | Clears `state/agents/`, `state/tools/`, `state/codex/`; runs `git worktree prune`; removes orphaned worktrees >2h                       |

**State files layout:**

```
.claude/state/
├── agents/<id>.json        # one per active subagent (created at start, deleted at stop)
├── codex/<id>.json         # one per active codex plugin session
├── tools/<tool>.json       # one per tool type fired this turn (cleared at Stop)
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

______________________________________________________________________

### Agent Teams

Agent Teams is Claude Code's experimental multi-agent feature. Teams are always **user-invoked** — nothing auto-spawns. Enabled via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `settings.json`.

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
| `/develop fix --team`     | Bug spans modules; competing root-cause hypotheses                        |
| `/develop feature --team` | Cross-layer feature needing impl + QA + docs in parallel                  |
| `/research --team`        | Multiple competing method families to evaluate                            |
| `/optimize run --team`    | Goal spans multiple optimization axes (speed = arch + pipeline + compute) |
| `/optimize plan --team`   | Wizard + parallel exploration: teammates each own a different axis        |
| `/optimize`               | Directory or system-wide scope → Claude proposes team (heuristic)         |
| `/develop refactor`       | Directory or system-wide scope → Claude proposes team (heuristic)         |

**Model tiering:** Lead uses `opusplan`/`opus`. Deep reasoning teammates (`sw-engineer`, `qa-specialist`, `ai-researcher`, `perf-optimizer`) use `opus`. Execution teammates (`doc-scribe`, `linting-expert`, `ci-guardian`) use `sonnet`. Keep teams to 3–5 teammates (~7× token cost vs single session).

**Communication protocol:** Inter-agent messages use AgentSpeak v2 (defined in `TEAM_PROTOCOL.md`) — ~60% token savings vs natural language. Status codes (`alpha`/`beta`/`gamma`/`delta`/`epsilon`/`omega`), action symbols (`+`/`-`/`~`/`!`), file locking (`+lock`/`-lock`), and priority prefixes (`!!` urgent, `..` FYI). Lead-to-human communication uses normal English.

**Security in teams:** No standalone security agent. `qa-specialist` automatically embeds OWASP Top 10 security checks when the task touches auth, payment flows, or user data.

**Quality hooks:** `hooks/teammate-quality.js` handles `TeammateIdle` (redirects to pending tasks) and `TaskCompleted` (reserved for future quality gates).

## 📊 Status Line

A lightweight hook (`hooks/statusline.js`) adds a persistent two-row status bar to every Claude Code session:

```
Row 1:  claude-sonnet-4-6 │ Borda.ai-home │ Pro ~$1.20 │ ████░░░░░░ 38% │ 💬
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

### Setup

Install the Codex plugin in Claude Code — not an MCP server, a local plugin:

```bash
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
```

Skills check availability at runtime: `claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'`. If the plugin is absent, each skill skips its Codex step gracefully rather than failing.

**Invocation map** — every place Claude dispatches to Codex and why:

| Skill                              | Site                          | Purpose                                                                              | Plugin command                            |
| ---------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------ | ----------------------------------------- |
| `/develop fix`, `/develop feature` | `_shared/codex-prepass.md`    | Tier 1 pre-pass: review staged diff for bugs before Claude's review cycle            | `codex:review --wait`                     |
| `/review`                          | Step 2 co-review              | Adversarial diff review seeding agent prompts with pre-flagged issues                | `codex:adversarial-review --wait <focus>` |
| `/review`, `/optimize run`         | `_shared/codex-delegation.md` | Delegate mechanical follow-up: docstrings, type annotations, test stubs              | `codex:codex-rescue` (agent)              |
| `/resolve`                         | Step 8 action items           | Apply PR review feedback to the codebase                                             | `codex:codex-rescue` (agent)              |
| `/resolve`                         | Step 12a comment dispatch     | Apply a specific review comment                                                      | `codex:codex-rescue` (agent)              |
| `/resolve`                         | Step 12 review loop           | Review applied changes for issues before committing                                  | `codex:review --wait`                     |
| `/optimize run --codex`            | Phase 2b ideation             | Fallback: generate + apply one atomic optimization when Claude's change was reverted | `codex:codex-rescue` (agent)              |
| `/calibrate`                       | Phase 1a problem gen          | Generate synthetic calibration problems (JSON array written to run dir)              | `codex:codex-rescue` (agent)              |
| `/calibrate`                       | Phase 2 scoring               | Score calibration responses against ground truth (JSON written to run dir)           | `codex:codex-rescue` (agent)              |

**What Claude retains:**

- Long-horizon planning and research (`/research`, `/optimize run`, `/develop plan`)
- Orchestration of multiple agents in defined topologies
- Judgment calls: design decisions, spec approval, test validity assessment
- Final validation: Claude always verifies Codex output via `git diff HEAD` before accepting changes

**Why the division works:** Claude has a mental model of which files are "in scope" for a task; Codex reads the diff and codebase independently, without that context. Their blind spots are complementary — the union of both passes catches more than either alone.

## 📂 Artifact Layout

Runtime artifacts live at the project root in dot-prefixed dirs — separate from versioned config in `.claude/`. The dot-prefix signals "generated output, not source".

```
.plans/blueprint/        ← /brainstorm spec and tree files
.plans/active/           ← todo_*.md, plan_*.md
.plans/closed/           ← completed plans
.notes/                  ← lessons.md, diary, guides
.reports/calibrate/      ← /calibrate benchmark runs
.reports/resolve/        ← /resolve lint+QA gate outputs
.reports/audit/          ← /audit analysis runs
.reports/review/         ← /review multi-agent outputs
.experiments/            ← /optimize skill runs (improve mode)
.developments/           ← /develop review-cycle handoffs
.temp/                   ← long output from any skill (quality-gates rule)
```

Each skill creates a timestamped run dir: `.reports/<skill>/YYYY-MM-DDTHH-MM-SSZ/`. Completed runs contain `result.jsonl`; the `SessionEnd` hook deletes completed runs older than 30 days automatically. Incomplete runs (crashed/timed-out) are kept for debugging. All dot-prefixed dirs are gitignored — see `.claude/rules/artifact-lifecycle.md` for TTL policy and full details.
