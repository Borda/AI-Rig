# 🤖 Claude Code — Deep Reference

← [Back to root README](../README.md) · [Codex deep reference](../.codex/README.md)

Configuration for [Claude Code](https://claude.ai/code) (Anthropic's AI coding CLI). This file covers agent relationships, skill orchestration flows, implementation architecture, and operational internals. For the high-level overview and workflow sequences, see the root README.

<details>
<summary><strong>Contents</strong></summary>

- [🔄 Config Sync](#-config-sync)
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
| **doc-scribe**         | Documentation                                 | Google/Napoleon docstrings (no type duplication), Sphinx/mkdocs, changelog                                       |
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

| Skill          | Command                                                 | What It Does                                                                                                                                                                                             |
| -------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **review**     | `/review [file\|PR#] [--reply]`                         | Parallel review across arch, tests, perf, docs, lint, security, API; `--reply` drafts contributor comment                                                                                                |
| **analyse**    | `/analyse <N\|health\|ecosystem> [--reply]`             | GitHub thread analysis (auto-detects issue/PR/discussion); `health` = repo overview + duplicate clustering                                                                                               |
| **brainstorm** | `/brainstorm <idea>`                                    | Interactive design-first spec: clarifying questions → 2–3 approaches → spec → self-mentor review → approval gate                                                                                         |
| **develop**    | `/develop feature\|fix\|refactor\|plan\|debug <goal>`   | TDD-first feature dev, reproduce-first bug fixing, test-first refactor, scope analysis (`plan`), or investigation-first debugging (`debug`)                                                              |
| **resolve**    | `/resolve <PR#\|comment>`                               | Resolve PR merge conflicts or apply review comments via Codex                                                                                                                                            |
| **calibrate**  | `/calibrate [target] [fast\|full] [apply]`              | Synthetic benchmarks measuring recall vs confidence bias; `routing` and `communication` modes available                                                                                                  |
| **audit**      | `/audit [scope] fix [high\|medium\|all] \| upgrade`     | Config audit: broken refs, inventory drift, docs freshness; `fix` auto-fixes at the requested severity level; `upgrade` applies docs-sourced improvements (mutually exclusive with `fix`)                |
| **release**    | `/release <mode> [range]`                               | Notes, changelog, migration, full prepare pipeline, or readiness `audit`                                                                                                                                 |
| **research**   | `/research <topic> \| plan [path]`                      | SOTA literature research with implementation plan; `plan` mode produces a phased, codebase-mapped implementation plan (auto-detects latest research output)                                              |
| **optimize**   | `/optimize plan\|campaign\|resume\|perf <goal\|target>` | Four modes: `plan` = config wizard → `program.md`; `campaign` = metric-driven iteration loop; `resume` = continue after crash/stop; `perf` = profiling deep-dive; `--team` and `--colab` (GPU) supported |
| **manage**     | `/manage <op> <type>`                                   | Create, rename, or delete agents/skills with cross-ref propagation and routing calibration                                                                                                               |
| **sync**       | `/sync [apply]`                                         | Drift-detect and sync project `.claude/` → home `~/.claude/`                                                                                                                                             |
| **codex**      | `/codex <task> [target]`                                | Delegate mechanical coding tasks to Codex CLI                                                                                                                                                            |
| **distill**    | `/distill`                                              | One-time snapshot: suggest new agents/skills, review roster, prune memory, or consolidate lessons                                                                                                        |

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

**`/audit`** — self-mentor per file, then consolidation:

```
per-config-file: self-mentor (reads file, writes findings to /tmp/audit-<ts>/<file>.md)
→ consolidator reads all finding files → ranked report with upgrade proposals
(upgrade mode: web-explorer fetches latest Claude Code docs first)
```

### Skill usage examples

<details>
<summary><strong>`/optimize` — Performance deep-dive and campaign mode</strong></summary>

```bash
# plan mode — interactive config wizard → program.md
/optimize plan "increase test coverage to 90%"
/optimize plan "improve F1 from 0.82 to 0.87" coverage.md  # write to custom path

# campaign mode — sustained metric-improvement loop
/optimize campaign "increase test coverage to 90%"        # run from text goal (20-iteration loop; auto-rollback on regression)
/optimize campaign coverage.md                            # run from program.md config file

# resume mode — continue after crash or manual stop
/optimize resume                                          # reads program_file from state.json
/optimize resume coverage.md                             # resume specific campaign

# flags (plan/campaign/resume)
/optimize campaign "reduce training time by 20%" --team   # parallel exploration across axes
/optimize campaign "improve validation accuracy" --colab  # GPU workloads via Colab MCP (opt-in)

# perf mode — single profiling session
/optimize perf src/mypackage/dataloader.py
/optimize perf src/mypackage/train.py
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

- `plan` — scope analysis; produces structured plan in `tasks/plan_<slug>.md`
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
<summary><strong>`/codex` — Delegate mechanical work to Codex</strong></summary>

```bash
/codex "add Google-style docstrings to all undocumented public functions" "src/mypackage/"
/codex "rename BatchLoader to DataBatcher throughout the package" "src/mypackage/"
/codex "add return type annotations to all functions missing them" "src/mypackage/utils.py"
```

</details>

<details>
<summary><strong>`/resolve` — Resolve a PR end-to-end</strong></summary>

```bash
/resolve 42                                              # full PR: conflict check → semantic resolution → review comments
/resolve https://github.com/org/repo/pull/42
/resolve "rename foo to bar throughout the auth module"  # single-comment fast path
```

</details>

## 📐 Rules

### Reference table

| Rule file               | Applies to                        | What it governs                                                                                                   |
| ----------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `artifact-lifecycle.md` | `.claude/**`                      | Canonical `_<skill>/` artifact layout, run-dir naming, TTL policy, SessionEnd cleanup hook, settings.json entries |
| `ci-workflows.md`       | `.github/workflows/**/*.yml`      | Semantic version tags preferred over SHA pins; Python matrix ≥3.10; fail-fast rules                               |
| `claude-config.md`      | `.claude/**`                      | Checklist for editing `.claude/` files: cross-refs, MEMORY.md roster, README, sync                                |
| `communication.md`      | (global)                          | Re: anchor format, progress narration, tone, output routing, and terminal color conventions                       |
| `external-data.md`      | (global)                          | Pagination and completeness rules for REST, GraphQL, and the `gh` CLI — never work on partial result sets         |
| `git-commit.md`         | (global)                          | Commit message format, push safety (explicit confirmation required), branch safety                                |
| `hooks-js.md`           | `.claude/hooks/*.js`              | Hook writing standards: state files, age-out patterns, tool activity tracking                                     |
| `pre-commit-config.md`  | `.pre-commit-config.yaml`         | Version pinning rules, hook ordering, CI integration via pre-commit.ci                                            |
| `python-code.md`        | `**/*.py`                         | Modern Python style: type annotations, dataclasses, structural pattern matching                                   |
| `quality-gates.md`      | (global)                          | Confidence blocks on all analysis tasks, internal quality loop, output routing rules                              |
| `release-notes.md`      | `CHANGELOG.md`, `PUBLIC-NOTES.md` | Release note structure, SemVer decision criteria, deprecation notice format                                       |
| `testing.md`            | `tests/**/*.py`, `**/test_*.py`   | pytest AAA structure, parametrize standards, doctest location (source files, not tests)                           |

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
- Persistent (final reports): `tasks/`

**Reference implementations:** `/calibrate` is canonical; `/audit` Step 3 (`self-mentor` per file → consolidator); `/review` Steps 3–6.

______________________________________________________________________

### Tiered review pipeline

Every skill that reviews or validates code uses a three-tier pipeline. Cheaper tiers gate the expensive ones:

| Tier                    | What                                                                   | Cost | When                               |
| ----------------------- | ---------------------------------------------------------------------- | ---- | ---------------------------------- |
| **0 — Mechanical gate** | `git diff --stat` — skip trivial diffs                                 | Zero | Always (built into codex-prepass)  |
| **1 — Codex pre-pass**  | Diff-focused review (~60s) — flags bugs, edge cases, logic errors      | Low  | Before expensive agent spawns      |
| **2 — Claude agents**   | Specialized parallel agents (opus for reasoning, sonnet for execution) | High | Full review, audit, implementation |

| Skill                                  | Tier 0 | Tier 1 (Codex pre-pass) | Tier 2 (Claude agents) |
| -------------------------------------- | :----: | :---------------------: | :--------------------: |
| `/develop` (feature/fix/refactor/plan) |   ✓    |            ✓            |           ✓            |
| `/review`                              |   ✓    |           ✓ †           |           ✓            |
| `/optimize`                            |   ✓    |            ✓            |           ✓            |
| `/audit fix`                           |   ✓    |            ✓            |           ✓            |
| `/resolve`                             |   —    |            —            |           ✓            |
| `/codex`                               |   —    |            ✓            |           —            |

† For `/review`, Codex runs as a full **co-reviewer** alongside Tier 2 agents — its findings are independently consolidated, not used to seed agent prompts (unbiased review).

______________________________________________________________________

### Hook state machine

`task-log.js` is the central event handler. It handles six Claude Code hook events and maintains runtime state read by `statusline.js`:

**Event → action mapping:**

| Event           | Action                                                                                                                           |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `PreToolUse`    | Logs Task/Agent and Skill invocations to `logs/invocations.jsonl`; opens Codex session file; increments per-tool-type state file |
| `PostToolUse`   | Closes Codex session file when Skill(codex) or Bash(codex …) completes                                                           |
| `SubagentStart` | Creates `state/agents/<id>.json` with agent type, model, color, start timestamp — one file per agent (no race)                   |
| `SubagentStop`  | Deletes per-agent file; appends completion entry to `invocations.jsonl`                                                          |
| `PreCompact`    | Appends to `logs/compactions.jsonl`; extracts modified file paths from transcript; writes `state/session-context.md`             |
| `Stop`          | Clears `state/tools/` — resets the 🔧 row between turns (agents intentionally NOT cleared — may still be running)                |
| `SessionEnd`    | Clears `state/agents/`, `state/tools/`, `state/codex/`; runs `git worktree prune`; removes orphaned worktrees >2h                |

**State files layout:**

```
.claude/state/
├── agents/<id>.json        # one per active subagent (created at start, deleted at stop)
├── codex/<id>.json         # one per active /codex session
├── tools/<tool>.json       # one per tool type fired this turn (cleared at Stop)
└── session-context.md      # modified-file breadcrumb (survives compaction)

.claude/logs/
├── invocations.jsonl       # append-only: agent launches, skill invocations, completions
└── compactions.jsonl       # append-only: compaction events
```

**Age-out rules:**

- Agents: 10-minute safety-net — files older than 10 min with no corresponding Stop event indicate a crashed agent; statusline excludes them
- Codex sessions: 30-minute cutoff — stalled Codex processes are treated as timed out
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

| Skill                       | When to use                                                               |
| --------------------------- | ------------------------------------------------------------------------- |
| `/develop fix --team`       | Bug spans modules; competing root-cause hypotheses                        |
| `/develop feature --team`   | Cross-layer feature needing impl + QA + docs in parallel                  |
| `/research --team`          | Multiple competing method families to evaluate                            |
| `/optimize campaign --team` | Goal spans multiple optimization axes (speed = arch + pipeline + compute) |
| `/optimize plan --team`     | Wizard + parallel exploration: teammates each own a different axis        |
| `/optimize`                 | Directory or system-wide scope → Claude proposes team (heuristic)         |
| `/develop refactor`         | Directory or system-wide scope → Claude proposes team (heuristic)         |

**Model tiering:** Lead uses `opusplan`/`opus`. Deep reasoning teammates (`sw-engineer`, `qa-specialist`, `ai-researcher`, `perf-optimizer`) use `opus`. Execution teammates (`doc-scribe`, `linting-expert`, `ci-guardian`) use `sonnet`. Keep teams to 3–5 teammates (~7× token cost vs single session).

**Communication protocol:** Inter-agent messages use AgentSpeak v2 (defined in `TEAM_PROTOCOL.md`) — ~60% token savings vs natural language. Status codes (`alpha`/`beta`/`gamma`/`delta`/`epsilon`/`omega`), action symbols (`+`/`-`/`~`/`!`), file locking (`+lock`/`-lock`), and priority prefixes (`!!` urgent, `..` FYI). Lead-to-human communication uses normal English.

**Security in teams:** No standalone security agent. `qa-specialist` automatically embeds OWASP Top 10 security checks when the task touches auth, payment flows, or user data.

**Quality hooks:** `hooks/teammate-quality.js` handles `TeammateIdle` (redirects to pending tasks) and `TaskCompleted` (reserved for future quality gates).

## 📊 Status Line

A lightweight hook (`hooks/statusline.js`) adds a persistent two-row status bar to every Claude Code session:

```
Row 1:  claude-sonnet-4-6 │ Borda.ai-home │ Pro ~$1.20 │ ████░░░░░░ 38%
Row 2:  🕵 5 agents (self-mentor ×3, opus, sw-engineer) │ 🤖 codex ×2 │ 🔧 Bash ×3 · Edit · Read ×12
```

**Row 1** — model name · project directory · billing indicator · 10-segment context bar (green → yellow → red)

**Row 2** — agent count · Codex sessions · active tools (last 30 seconds)

**Agent row details:**

- Specialized agents (have a `.claude/agents/` file) → shown by type name in their declared `color:` from frontmatter
- General-purpose agents → shown by model name in gray (`opus`, `sonnet`)
- Same-type agents grouped with `×N` count
- Codex sessions appended as `codex ×N` in yellow

**Tool row colors:** `Read` (blue) · `Write` (bright green) · `Edit` (green) · `Bash` (yellow) · `Grep` (cyan) · `Glob` (bright cyan) · `WebFetch` (magenta) · `WebSearch` (bright magenta) · `Task`/`Agent` (bright blue) · `Skill` (bright yellow)

**Billing indicator:**

- **Subscription (Pro/Max):** `Max/Pro/Sub ~$X.XX` in cyan — plan from `~/.claude/state/subscription.json`; `~$X.XX` is theoretical API-rate cost (tokens × list price), not an actual charge
- **API key:** `API $X.XX` in yellow — actual spend at pay-per-token rates

**Hook mechanics:** `statusline.js` reads `state/agents/`, `state/codex/`, and `state/tools/` on each render. `task-log.js` writes those files; `statusline.js` only reads. Configured via `statusLine` in `settings.json`. Zero external dependencies — stdlib `path` and `fs` only.

## 🤝 Integration with Codex

→ Codex's perspective on this integration: [`.codex/README.md` — Integration with Claude](../.codex/README.md#-integration-with-claude) · Full architecture: [root README](../README.md#-claude--codex-integration)

**What Claude delegates to Codex:**

- Mechanical, diff-scoped tasks: add docstrings, rename symbols, add type annotations across a module
- PR review comment application (via `/resolve`)
- Codex pre-pass in the tiered review pipeline (Tier 1) — independent diff review before Claude's parallel agents

**What Claude retains:**

- Long-horizon planning and research (`/research`, `/optimize campaign`, `/develop plan`)
- Orchestration of multiple agents in defined topologies
- Judgment calls: design decisions, spec approval, test validity assessment
- Final validation: Claude always reviews Codex output with lint + tests before marking work complete

**Why the division works:** Claude has a mental model of which files are "in scope" for a task; Codex reads the diff and codebase independently, without that context. Their blind spots are complementary — the union of both passes catches more than either alone.

## 📂 Artifact Layout

Runtime artifacts live at the project root in `_<skill>/` dirs — separate from versioned config in `.claude/`. The `_` prefix sorts them together and signals "generated output, not source".

```
_calibrate/          ← /calibrate benchmark runs
_resolve/            ← /resolve lint+QA gate outputs
_audit/              ← /audit analysis runs
_review/             ← /review multi-agent outputs
_optimize/           ← /optimize skill runs (perf + campaign modes)
_develop/            ← /develop review-cycle handoffs
_out/                ← long output from any skill (quality-gates rule)
  YYYY/MM/
tasks/_plans/        ← active and closed plans (tracked)
tasks/_working/      ← lessons, diary, guides (tracked)
```

Each skill creates a timestamped run dir: `_<skill>/YYYY-MM-DDTHH-MM-SSZ/`. Completed runs contain `result.jsonl`; the `SessionEnd` hook deletes completed runs older than 30 days automatically. Incomplete runs (crashed/timed-out) are kept for debugging. All `_*/` dirs are gitignored — see `.claude/rules/artifact-lifecycle.md` for TTL policy and full details.
