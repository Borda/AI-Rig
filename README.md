# Borda's .local

Personal AI coding assistant configuration for Python/ML OSS development. Version-controlled, opinionated, continuously improved.

## 📦 What's Here

```
borda.local/
├── .claude/                # Claude Code (Claude by Anthropic)
│   ├── CLAUDE.md           # workflow rules and core principles
│   ├── settings.json       # permissions and model preferences
│   ├── agents/             # specialist agents
│   ├── skills/             # workflow skills (slash commands)
│   └── hooks/              # UI extensions
├── .codex/                 # OpenAI Codex CLI
│   ├── AGENTS.md           # global instructions and subagent spawn rules
│   ├── config.toml         # multi-agent config (gpt-5.3-codex baseline)
│   └── agents/             # per-agent model and instruction overrides
├── .pre-commit-config.yaml
├── .gitignore
└── README.md
```

## 🤖 Claude Code

Agents and skills for [Claude Code](https://claude.ai/code) (Anthropic's AI coding CLI).

### Agents

Specialist roles with deep domain knowledge. You can request a specific agent by name in your prompt (e.g., *"use the qa-specialist to write tests for this module"*). Claude Code also selects agents automatically when spawning subagents via the Task tool.

| Agent                  | Purpose                          | Key Capabilities                                                                                                 |
| ---------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| **sw-engineer**        | Architecture and implementation  | SOLID principles, type safety, clean architecture, doctest-driven dev                                            |
| **solution-architect** | System design and API planning   | ADRs, interface specs, migration plans, coupling analysis, API surface audit                                     |
| **oss-maintainer**     | Project lifecycle management     | Issue triage, PR review, SemVer, pyDeprecate, trusted publishing                                                 |
| **ai-researcher**      | ML research and implementation   | Paper analysis, experiment design, LLM evaluation, inference optimization                                        |
| **qa-specialist**      | Testing and validation           | pytest, hypothesis, mutation testing, snapshot tests, ML test patterns                                           |
| **linting-expert**     | Code quality and static analysis | ruff, mypy, pre-commit, rule selection strategy, CI quality gates; runs autonomously (`permissionMode: dontAsk`) |
| **perf-optimizer**     | Performance engineering          | Profile-first workflow, CPU/GPU/memory/I/O, torch.compile, mixed precision                                       |
| **ci-guardian**        | CI/CD reliability                | GitHub Actions, reusable workflows, trusted publishing, flaky test detection                                     |
| **data-steward**       | ML data pipeline integrity       | Split validation, leakage detection, data contracts, class imbalance                                             |
| **doc-scribe**         | Documentation                    | Google/Napoleon docstrings (no type duplication), Sphinx/mkdocs, changelog                                       |
| **web-explorer**       | Web and docs research            | API version comparison, migration guides, PyPI tracking, ecosystem compat                                        |
| **self-mentor**        | Config quality reviewer (Opus)   | Agent/skill auditing, duplication detection, cross-ref validation, line budgets                                  |

### Skills

Skills are orchestrations of agents — invoked via slash commands (`/review`, `/develop fix`, etc.). A single skill typically composes multiple agents in parallel and consolidates their output. Think of agents as specialists you can talk to, and skills as predefined workflows that coordinate them.

| Skill         | Command                                              | What It Does                                                                                                                                                                                              |
| ------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **review**    | `/review [file\|PR#] [--reply]`                      | Parallel code review across 7 dimensions (arch, tests, perf, docs, lint, security, API design); `--reply` drafts a contributor-facing comment via oss-maintainer                                          |
| **optimize**  | `/optimize [target]`                                 | Measure-change-measure performance loop                                                                                                                                                                   |
| **release**   | `/release <mode> [range]`                            | Notes, changelog, summary, migration, or full prepare pipeline; `audit` checks release readiness                                                                                                          |
| **survey**    | `/survey [topic]`                                    | SOTA literature survey with implementation plan                                                                                                                                                           |
| **analyse**   | `/analyse [#\|health\|dupes\|...] [--reply]`         | Issue/PR/Discussion analysis by number (unified index, auto-detected), repo health, duplicate detection; `--reply` drafts a contributor-facing reply, reusing today's report if the item hasn't drifted   |
| **observe**   | `/observe`                                           | Meta-skill: analyze work patterns and suggest new agents or skills                                                                                                                                        |
| **audit**     | `/audit [fix [high\|medium\|all]\|upgrade]`          | Full-sweep config audit: broken refs, dead loops, inventory drift, docs freshness + upgrade proposals; `upgrade` applies docs-sourced improvements (config: correctness check, capability: calibrate A/B) |
| **sync**      | `/sync [apply]`                                      | Drift-detect project `.claude/` vs home `~/.claude/`; `apply` performs the sync                                                                                                                           |
| **manage**    | `/manage <op> <type>`                                | Create, update, or delete agents/skills with cross-ref propagation                                                                                                                                        |
| **develop**   | `/develop feature\|fix\|refactor`                    | Unified development orchestrator: TDD-first feature dev, reproduce-first bug fixing, or test-first refactoring                                                                                            |
| **calibrate** | `/calibrate [target] [fast\|full]`                   | Agent calibration: synthetic problems with known outcomes, measures recall vs confidence bias                                                                                                             |
| **codex**     | `/codex <task> [target]`                             | Delegate mechanical coding tasks to Codex CLI — Claude orchestrates, Codex executes                                                                                                                       |
| **resolve**   | `/resolve <PR#\|comment>`                            | Resolve a PR: auto-detects merge conflicts first (semantic resolution with branch intent), then applies review comments via Codex                                                                         |
| **research**  | `/research [plan\|resume] <goal> [--team] [--colab]` | Autonomous goal-directed iteration loop: define metric + guard, iterate with specialist agents (perf-optimizer, sw-engineer, ai-researcher), auto-rollback on regression; GPU workloads via Colab MCP     |

<details>
<summary><strong>Skill usage examples</strong></summary>

- **`/optimize` — Performance deep-dive**

  ```bash
  # Profile a specific Python module
  /optimize src/mypackage/dataloader.py
  # Profile a whole package entry point
  /optimize src/mypackage/train.py
  # Target a slow test suite
  /optimize tests/test_heavy_integration.py
  ```

- **`/review` — Parallel code review**

  ```bash
  # Review a PR by number
  /review 42
  # Review specific files
  /review src/mypackage/transforms.py
  # Review latest commit (no argument)
  /review
  # Review + draft contributor-facing comment (overall + inline table)
  /review 42 --reply
  ```

- **`/analyse` — Issue, PR, Discussion and repo health**

  ```bash
  # Analyze by number — type auto-detected (issue / PR / discussion share a unified index)
  /analyse 123
  # Repo health overview
  /analyse health
  # Find duplicate issues
  /analyse dupes memory leak
  # Analyze + draft contributor reply (reuses today's report if no new activity since it was written)
  /analyse 123 --reply
  ```

- **`/survey` — SOTA literature search**

  ```bash
  # Survey a topic
  /survey efficient transformers for long sequences
  # Survey a specific method
  /survey knowledge distillation for object detection
  ```

- **`/release` — Release notes, changelog, and readiness checks**

  ```bash
  /release notes v1.2.0..HEAD   # write PUBLIC-NOTES.md
  /release changelog v1.2.0..HEAD  # prepend CHANGELOG.md
  /release prepare v2.0.0        # full pipeline: audit + notes + changelog + migration
  /release audit                 # pre-release readiness check (blockers, CVEs, version consistency)
  ```

- **`/sync` — Config drift detection**

  ```bash
  # Dry-run: show what differs between project and home .claude/
  /sync
  # Apply: copy differing files to ~/.claude/
  /sync apply
  ```

- **`/manage` — Agent/skill lifecycle**

  ```bash
  # Create a new agent
  /manage create agent security-auditor "Security specialist for vulnerability scanning"
  # Rename a skill (updates all cross-references)
  /manage update skill optimize perf-audit
  # Delete an agent (cleans broken refs)
  /manage delete agent web-explorer
  ```

- **`/audit` — Config health sweep + upgrade**

  ```bash
  # Full sweep — report only, includes upgrade proposals table
  /audit
  # Auto-fix critical and high findings
  /audit fix
  # Apply docs-sourced improvements: config changes verified, capability changes A/B tested via calibrate
  /audit upgrade
  # Agents only, report only
  /audit agents
  # Skills only, with auto-fix
  /audit skills fix
  ```

- **`/develop` — Unified development orchestrator**

  ```bash
  # TDD-first feature development
  /develop feature 87
  /develop feature "add batched predict() method to Classifier"
  /develop feature "add batched predict() method to Classifier" "src/classifier"

  # Reproduce-first bug fixing
  /develop fix 42
  /develop fix "TypeError when passing None to transform()"
  /develop fix tests/test_transforms.py::test_none_input

  # Test-first refactoring
  /develop refactor src/mypackage/transforms.py "replace manual loops with vectorized ops"
  /develop refactor src/mypackage/utils/
  ```

- **`/codex` — Delegate mechanical work to Codex**

  ```bash
  # Add docstrings to all undocumented public functions in a module
  /codex "add Google-style docstrings to all undocumented public functions" "src/mypackage/transforms.py"
  # Rename a symbol consistently across a directory
  /codex "rename BatchLoader to DataBatcher throughout the package" "src/mypackage/"
  # Add type annotations to a well-typed module
  /codex "add return type annotations to all functions missing them" "src/mypackage/utils.py"
  ```

- **`/resolve` — Resolve a PR end-to-end**

  ```bash
  # Full PR resolution: conflict check → semantic resolution → review comments
  /resolve 42
  # Also accepts a full GitHub PR URL
  /resolve https://github.com/org/repo/pull/42

  # Single-comment fast path (no PR number)
  /resolve "rename foo to bar throughout the auth module"
  ```

- **`/research` — Autonomous metric improvement loop**

  ```bash
  # Interactive wizard: scan codebase, propose config, dry-run commands
  /research plan "increase test coverage to 90%"
  # Run iteration loop (uses existing config or auto-detects)
  /research "improve test coverage"
  # Resume a previous run by ID (or auto-picks most recent running run)
  /research resume
  /research resume 20240318-143022
  # Team mode: parallel agents on different optimization axes
  /research "reduce training time by 20%" --team
  # GPU workloads via Colab MCP (opt-in — see note below)
  /research "improve validation accuracy" --colab
  ```

> **Colab MCP is opt-in and has no overhead when inactive.** `.mcp.json` defines the server but does not start it. To enable: add `"colab-mcp"` to `enabledMcpjsonServers` in `.claude/settings.local.json`, then restart Claude Code. MCP servers load at session start, so a restart is required before `--colab` will work.

</details>

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
/optimize src/mypackage/dataloader.py   # profile and fix top bottleneck
/develop refactor src/mypackage/dataloader.py "extract caching layer"  # structural improvement
/review                                 # full quality pass on changes
```

</details>

<details>
<summary><strong>Code review → fix blocking issues</strong></summary>

```
/review 55             # parallel review across 7 dimensions
/develop fix "race condition in cache invalidation"  # fix blocking issue from review
/review 55             # re-review after fix
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
<summary><strong>New capability → survey → implement</strong></summary>

```
/survey "efficient attention for long sequences"  # find SOTA methods
/develop feature "implement FlashAttention in encoder"    # TDD-first implementation
/review                                           # validate implementation
```

</details>

<details>
<summary><strong>Autonomous metric improvement campaign</strong></summary>

```
/research plan "increase test coverage to 90%"   # interactive config wizard
/research "increase test coverage to 90%"        # run 20-iteration loop; auto-rollback on regression
/research resume                                  # resume after crash or manual stop
/review                                           # validate kept commits
```

</details>

<details>
<summary><strong>Survey SOTA → research toward metric</strong></summary>

```
/survey "knowledge distillation for small models"   # find best approach
/research plan "improve F1 from 0.82 to 0.87"       # configure metric + guard + agent
/research "improve F1 from 0.82 to 0.87" --team     # parallel exploration across axes
/review                                              # quality pass on kept changes
```

</details>

<details>
<summary><strong>Observe → create → audit → sync</strong></summary>

```
/observe               # analyze work patterns, suggest new agents/skills
/manage create agent security-auditor "..."  # scaffold suggested agent
/audit                 # verify config integrity — catch broken refs, dead loops
/sync apply            # propagate clean config to ~/.claude/
```

</details>

<details>
<summary><strong>Delegate mechanical work to Codex</strong></summary>

```
/codex "add Google-style docstrings to all undocumented public functions" "src/mypackage/"
# Codex executes; Claude validates with lint + tests
/review                                   # full quality pass on Codex output
```

</details>

<details>
<summary><strong>PR review feedback → resolve → verify</strong></summary>

```
/resolve 42   # auto-detect conflicts → resolve semantically → apply review comments via Codex
/review       # full quality pass on all applied changes
```

</details>

<details>
<summary><strong>OSS contributor PR triage → review → reply</strong></summary>

Preferred flow for maintainers responding to external contributions:

```
/analyse 42 --reply      # assess PR readiness + draft contributor reply in one step
                          # reuses today's analysis if no new activity since last run

# or if you need the full deep review first:
/review 42 --reply        # 7-dimension review + draft overall comment + inline comments table
                          # output: tasks/output-reply-pr-42-<date>.md

# post when ready:
gh pr comment 42 --body "$(cat tasks/output-reply-pr-42-<date>.md)"
```

Both `--reply` flags produce the same two-part oss-maintainer output: an overall PR comment (prose, warm, decisive) and an inline comments table (file | line | 1–2 sentence fix). The `/analyse` path is faster for routine triage; `/review` path gives deeper findings for complex PRs. Issue/discussion numbers are auto-detected from the unified GitHub index — no type prefix needed.

</details>

<details>
<summary><strong>Agent self-improvement loop</strong></summary>

```
/observe                        # analyze work patterns, surface what agents are missing or miscalibrated
/calibrate all fast ab apply    # benchmark all agents vs general-purpose baseline, apply improvement proposals
/audit fix                      # structural sweep after calibrate changed instruction files
/sync apply                     # propagate improved config to ~/.claude/
```

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
/release v1.2.0..HEAD  # generate release notes from git history
```

</details>

### Agent Teams

Claude Code's experimental Agent Teams feature is enabled. Teams are always **user-invoked** — nothing auto-spawns. You use `/develop fix --team`, `/develop feature --team`, etc. explicitly.

**Enable**: Already active via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `settings.json`.

#### When to use teams vs subagents

| Signal                                              | Use Team | Use Subagents |
| --------------------------------------------------- | -------- | ------------- |
| Competing root-cause hypotheses                     | ✓        |               |
| Cross-layer feature: impl + QA + docs in parallel   | ✓        |               |
| SOTA survey: multiple competing method clusters     | ✓        |               |
| Adversarial review (teammates challenge each other) | ✓        |               |
| Sequential pipeline (fix → test → lint)             |          | ✓             |
| Independent parallel review dimensions              |          | ✓             |
| Single file / single module scope                   |          | ✓             |
| Routine tasks (sync, observe, release)              |          | ✓             |

#### Skills with team support

| Skill                     | Mode          | When to use                                                                     |
| ------------------------- | ------------- | ------------------------------------------------------------------------------- |
| `/develop fix --team`     | `--team` flag | Bug spans modules; competing root-cause hypotheses                              |
| `/develop feature --team` | `--team` flag | Cross-layer feature needing impl + QA + docs in parallel                        |
| `/survey --team`          | `--team` flag | Multiple competing method families to evaluate                                  |
| `/research --team`        | `--team` flag | Goal spans multiple optimization axes (e.g., speed = arch + pipeline + compute) |
| `/optimize`               | heuristic     | Directory or system-wide scope → Claude proposes team                           |
| `/develop refactor`       | heuristic     | Directory or system-wide scope → Claude proposes team                           |

**Model tiering**: Lead uses `opusplan`/`opus`. Deep reasoning teammates (`sw-engineer`, `qa-specialist`, `ai-researcher`, `perf-optimizer`) use `opus`. Execution teammates (`doc-scribe`, `linting-expert`, `ci-guardian`) use `sonnet`. Keep teams to 3–5 teammates (~7× token cost vs single session).

**Communication protocol**: Inter-agent messages use the AgentSpeak v2 compressed syntax defined in `.claude/TEAM_PROTOCOL.md` (~60% token savings vs natural language). Lead-to-human communication uses normal English.

**Security in teams**: No standalone security agent. `qa-specialist` automatically embeds OWASP Top 10 security checks when operating as a teammate on code touching auth, payment flows, or user data.

**Quality hooks**: `hooks/teammate-quality.js` handles `TeammateIdle` (redirects to pending tasks if any exist) and `TaskCompleted` (reserved for future quality gates).

### Status Line

A lightweight hook (`hooks/statusline.js`) adds a persistent multi-row status bar to every Claude Code session:

```
Row 1 (always):    claude-sonnet-4-6 │ Borda.local │ Pro ~$1.20 │ ████░░░░░░ 38%
Row 2 (agents):    ⚡ 5 agents (self-mentor ×3, opus, sw-engineer) │ codex ×2
Row 3 (tools):     🔧 (Bash ×3 | Edit | Read ×12)
```

Row 1 shows the active model name, current project directory, billing indicator, and a 10-segment context usage bar (green → yellow → red). Rows 2 and 3 appear only when there is something to show.

**Agent row** — groups running agents by display name, then appends active Codex sessions:

- *Specialized agents* (have a `.claude/agents/` file) → shown by type name in their declared `color:` from frontmatter (e.g. `sw-engineer` in blue, `self-mentor` in pink)
- *General-purpose agents* or agents without a pinned model → shown by model name in gray (e.g. `opus`, `sonnet`)
- Agents of the same type are grouped with a `×N` count
- *Codex sessions* — appended after agents as `codex ×N` in yellow when `/codex` is running (tracked via `PreToolUse`/`PostToolUse` hooks on both Skill and Bash paths; covers `/codex`, `/resolve`, and any direct `codex exec` or `timeout N codex …` Bash call)

**Tool row** — shows tools called in the last 30 seconds, each in a unique fixed color:
`Read` (blue) · `Write` (bright green) · `Edit` (green) · `Bash` (yellow) · `Grep` (cyan) · `Glob` (bright cyan) · `WebFetch` (magenta) · `WebSearch` (bright magenta) · `Task`/`Agent` (bright blue) · `Skill` (bright yellow)

<details>
<summary><strong>Billing indicator explained</strong></summary>

- **Subscription (Pro/Max)**: `Max/Pro/Sub ~$X.XX` in cyan — plan name read from `~/.claude/state/subscription.json` (written at session start); `~$X.XX` is the session's theoretical API-rate cost (tokens × list price), not an actual charge. Use `/status` for real monthly quota.
- **API key**: `API $X.XX` in yellow — actual spend at pay-per-token rates.

`cost.total_cost_usd` (the source of `$X.XX`) is tokens × published API rates. For subscription users this is an estimate only — Anthropic's subscription quota uses internal accounting that doesn't map 1:1 to API list prices.

</details>

The agent row is powered by `hooks/task-log.js`, which handles: `SubagentStart`/`SubagentStop` (one file per active agent in `.claude/state/agents/<id>.json`), `PreToolUse`/`PostToolUse` (audit log + tool activity state in `.claude/state/tools/<tool>.json`; Codex session lifecycle in `.claude/state/codex/<id>.json`), `PreCompact` (context snapshot in `.claude/state/session-context.md`), and `Stop`/`SessionEnd` (clear all state so the status line resets cleanly). A 10-minute safety-net age-out handles crashed or hung agents; a 30-minute cutoff handles stalled Codex sessions.

Configured via `statusLine` in `settings.json`. Zero external dependencies — stdlib `path` and `fs` only.

### Config Sync

This repo is the **source of truth** for all `.claude/` configuration. Home (`~/.claude/`) is a downstream copy kept in sync via the `/sync` skill.

```
Borda.local/.claude/   →   ~/.claude/
  agents/                    agents/
  skills/                    skills/
  hooks/statusline.js        hooks/statusline.js
  settings.json              settings.json  (statusLine path rewritten to absolute)
```

One file is intentionally **not synced**: `settings.local.json` (machine-local overrides). `CLAUDE.md` is synced as part of the standard propagation.

**Workflow:**

```bash
/sync          # dry-run: show drift report (MISSING / DIFFERS / IDENTICAL per file)
/sync apply    # apply: copy all differing files and verify outcome
```

Run `/sync` after editing any agent, skill, hook, or `settings.json` in this repo to propagate the change to the home config.

## 🤝 Claude + Codex Integration

Claude and Codex complement each other — Claude handles long-horizon reasoning, orchestration, and judgment calls; Codex handles focused, mechanical in-repo coding tasks with direct shell access.

#### Tiered Review Architecture

Every skill that reviews or validates code uses a three-tier pipeline, where cheaper tiers gate the expensive ones:

| Tier                    | What                                                                   | Cost | When                               |
| ----------------------- | ---------------------------------------------------------------------- | ---- | ---------------------------------- |
| **0 — Mechanical gate** | `git diff --stat` — skip trivial diffs                                 | Zero | Always (built into codex-prepass)  |
| **1 — Codex pre-pass**  | Diff-focused review (~60s) — flags bugs, edge cases, logic errors      | Low  | Before expensive agent spawns      |
| **2 — Claude agents**   | Specialized parallel agents (opus for reasoning, sonnet for execution) | High | Full review, audit, implementation |

| Skill                             | Tier 0 (gate) | Tier 1 (Codex pre-pass) | Tier 2 (Claude agents) |
| --------------------------------- | :-----------: | :---------------------: | :--------------------: |
| `/develop` (feature/fix/refactor) |       ✓       |            ✓            |           ✓            |
| `/review`                         |       ✓       |            ✓            |           ✓            |
| `/optimize`                       |       ✓       |            ✓            |           ✓            |
| `/audit fix`                      |       ✓       |            ✓            |           ✓            |
| `/resolve`                        |       —       |            —            |           ✓            |
| `/codex`                          |       —       |            ✓            |           —            |
| `/research`                       |       ✓       |            —            |           ✓            |

**Why unbiased review matters / Real example**: Claude makes targeted changes with intentionality — it has a mental model of which files are "in scope" for a task. Codex has no such context: it reads the diff and the codebase independently. During one session, Claude applied a docstring-style mandate across 6 files, reported the work done, and scored its own confidence at 0.88. The Codex pre-pass then found `skills/develop/modes/feature.md` still referencing the old style — a direct miss from the batch fix. That file simply wasn't on Claude's mental scope list, so it was never checked. The union of both passes is more complete than either alone.

### Two integration patterns make this pairing practical

1. **Offloading mechanical tasks from Claude to Codex**

   Claude identifies what needs to change (rename a symbol, fill in docstrings, add type annotations across a module) and delegates execution to Codex. Claude keeps its context clean and validates the output.

   ```bash
   # Claude orchestrates, Codex executes
   /codex "add Google-style docstrings to all undocumented public functions" "src/mypackage/"
   /codex "rename BatchLoader to DataBatcher throughout the package" "src/mypackage/"
   /codex "add return type annotations to all functions missing them" "src/mypackage/utils.py"
   # Claude then reviews with lint + tests
   /review
   ```

2. **Codex reviewing staged work**

   After Claude (or you) stages changes, Codex can serve as a second pass — examining the diff, applying review comments, or resolving PR conflicts. The `/resolve` skill automates this: it resolves conflicts semantically (Claude) then applies review comments (Codex).

   ```bash
   # Stage changes, then let Codex process review feedback
   /resolve 42   # Claude resolves conflicts → Codex applies review comments
   /resolve "rename the `fit` method to `train` throughout the module"  # single-comment fast path
   ```

<details>
<summary><strong>Pre-flight requirements</strong></summary>

The `/codex` and `/resolve` skills require both tools to be installed:

```bash
# Install Claude Code (if not already)
npm install -g @anthropic-ai/claude-code

# Install Codex CLI
npm install -g @openai/codex    # npm show @openai/codex version for latest

# Verify
which claude && which codex
```

If `codex` is not found, `/codex` will fail at the pre-flight check and `/resolve`'s review-comment step will be skipped with a clear error. Conflict resolution in `/resolve` (Step 4–5) runs through Claude only and does not require Codex.

The active Codex session count appears in the statusline Row 2 next to the agent count (`⚡ 3 agents │ codex ×2`), driven by `PreToolUse`/`PostToolUse` hooks in `task-log.js`.

</details>

## 🤖 Codex CLI

Multi-agent configuration for [OpenAI Codex CLI](https://github.com/openai/codex) (Rust implementation). Where Claude Code excels at long-horizon planning and research, Codex CLI is optimized for focused, in-repo agentic coding — running shell commands, editing files, and spawning parallel sub-agents directly in your terminal.

### Agents

Nine specialist roles wired into the multi-agent system. Codex can spawn them autonomously based on task type (see `AGENTS.md` for the full spawn-rule matrix) or you can address them by name in your prompt.

| Agent                | Model         | Effort | Purpose                                                                 |
| -------------------- | ------------- | ------ | ----------------------------------------------------------------------- |
| **sw-engineer**      | gpt-5.3-codex | high   | SOLID implementation, doctest-driven dev, ML pipeline architecture      |
| **qa-specialist**    | gpt-5.3-codex | xhigh  | Edge-case matrix, The Borda Standard, adversarial test review           |
| **squeezer**         | gpt-5.3-codex | high   | Profile-first optimization, GPU throughput, memory efficiency           |
| **doc-scribe**       | gpt-5.3-codex | medium | 6-point Google/Napoleon docstrings, README stewardship, CHANGELOG       |
| **security-auditor** | gpt-5.3-codex | xhigh  | OWASP Python, ML supply chain, secrets, CI/CD hygiene *(read-only)*     |
| **data-steward**     | gpt-5.3-codex | high   | Split leakage, DataLoader reproducibility, augmentation correctness     |
| **ci-guardian**      | gpt-5.3-codex | medium | GitHub Actions, trusted PyPI publishing, pre-commit, flaky tests        |
| **linting-expert**   | gpt-5.3-codex | medium | ruff, mypy, pre-commit config, rule progression, suppression discipline |
| **oss-maintainer**   | gpt-5.3-codex | high   | Issue triage, PR review, SemVer, pyDeprecate, release checklist         |

### Model Strategy

All agents in this repo are configured to use `gpt-5.3-codex`. Differentiation is via reasoning effort:

- **xhigh** — adversarial roles (qa-specialist, security-auditor): exhaustive search for what could go wrong
- **high** — analytical roles (sw-engineer, squeezer, data-steward, oss-maintainer): depth without unbounded budget
- **medium** — writing/config roles (doc-scribe, ci-guardian, linting-expert): quality over deductive intensity

### Usage

```bash
# Interactive session — Codex selects agents automatically
codex

# Address a specific agent by name in your prompt
codex "use the security-auditor to review src/api/auth.py"
codex "spawn data-steward to validate the train/val split in data/splits/"

# Parallel fan-out (Codex orchestrates automatically per AGENTS.md rules)
# e.g. after sw-engineer finishes → qa-specialist + doc-scribe run concurrently
```

### Install / Port to Home

This repo is the authoring location. To activate globally, copy the entire `.codex/` directory to `~/.codex/`:

```bash
cp -r .codex/ ~/.codex/
```

`config_file` paths in `config.toml` are **relative to that file** — no substitution needed. The `AGENTS.md` at `~/.codex/AGENTS.md` is read by Codex for every project; a project-local `AGENTS.md` at the repo root extends it.

### Files

| File            | Purpose                                                                   |
| --------------- | ------------------------------------------------------------------------- |
| `config.toml`   | Global model, sandbox, features flags, and `[agents]` registry            |
| `AGENTS.md`     | Borda Standard, 6-point docstring structure, spawn rules for all 9 agents |
| `agents/*.toml` | Per-agent `model`, `model_reasoning_effort`, and `developer_instructions` |

## 💡 Design Principles

- **Agents are roles, skills are workflows** — agents carry domain expertise, skills orchestrate multi-step processes
- **No duplication** — agents reference each other instead of repeating content (e.g., sw-engineer references linting-expert for config)
- **Profile-first, measure-last** — performance skills always bracket changes with measurements
- **Link integrity** — never cite a URL without fetching it first (enforced in all research agents)
- **Python 3.10+ baseline** — all configs target py310 minimum (3.9 EOL was Oct 2025)
- **Modern toolchain** — uv, ruff, mypy, pytest, GitHub Actions with trusted publishing

## 🎯 Tailored For

This setup is optimized for maintaining Python/ML OSS projects in the PyTorch ecosystem:

- Libraries with public APIs requiring SemVer discipline and deprecation cycles
- ML training and inference codebases needing GPU profiling and data pipeline validation
- Multi-contributor projects with CI/CD, pre-commit hooks, and automated releases
