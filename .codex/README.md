# 🤖 Codex CLI — Deep Reference

← [Back to root README](../README.md) · [Claude deep reference](../.claude/README.md)

Multi-agent configuration for [OpenAI Codex CLI](https://github.com/openai/codex) (Rust implementation). This file covers agent spawn rules, model strategy, runtime profiles, execution architecture, mirrored skill usage, and Claude integration internals.

<details>
<summary><strong>Contents</strong></summary>

- [🔄 Config Sync](#-config-sync)
- [🧩 Agents](#-agents)
  - [Reference table](#reference-table)
  - [Spawn rules](#spawn-rules)
- [🧠 Model Strategy & Profiles](#-model-strategy--profiles)
  - [Model Strategy](#model-strategy)
  - [Profiles](#profiles)
- [🧭 Skills In Codex](#-skills-in-codex)
  - [Built-in vs mirrored commands](#built-in-vs-mirrored-commands)
  - [Usage examples](#usage-examples)
- [🪙 RTK Integration](#-rtk-integration)
- [🏗️ Architecture](#-architecture)
  - [Multi-agent execution model](#multi-agent-execution-model)
  - [Mirrored skills and gates](#mirrored-skills-and-gates)
  - [AGENTS.md layering](#agentsmd-layering)
  - [MCP server](#mcp-server)
- [🤝 Integration with Claude](#-integration-with-claude)

</details>

## 🔄 Config Sync

This repo (`.codex/`) is the source of truth. Home (`~/.codex/`) is a downstream copy:

```bash
cp -r .codex/ ~/.codex/ # activate globally (config_file paths are relative)
```

Run after editing any agent config, `config.toml`, hooks, or `AGENTS.md`.

<details>
<summary><strong>Install</strong></summary>

```bash
npm install -g @openai/codex # install Codex CLI
cp -r .codex/ ~/.codex/      # activate globally
```

</details>

## 🧩 Agents

### Reference table

All agents are standardized on `gpt-5.4`. Differentiation is via reasoning effort and role instructions.

| Agent                  | Effort | Purpose                                                                 |
| ---------------------- | ------ | ----------------------------------------------------------------------- |
| **sw-engineer**        | high   | SOLID implementation, doctest-driven dev, ML pipeline architecture      |
| **qa-specialist**      | xhigh  | Edge-case matrix, The Borda Standard, adversarial test review           |
| **squeezer**           | high   | Profile-first optimization, GPU throughput, memory efficiency           |
| **doc-scribe**         | medium | 6-point Google/Napoleon docstrings, README stewardship, CHANGELOG       |
| **security-auditor**   | xhigh  | OWASP Python, ML supply chain, secrets, CI/CD hygiene *(read-only)*     |
| **data-steward**       | high   | Split leakage, DataLoader reproducibility, augmentation correctness     |
| **ci-guardian**        | medium | GitHub Actions, trusted PyPI publishing, pre-commit, flaky tests        |
| **linting-expert**     | medium | ruff, mypy, pre-commit config, rule progression, suppression discipline |
| **oss-shepherd**       | high   | Issue triage, PR review, SemVer, pyDeprecate, release checklist         |
| **solution-architect** | high   | System design, ADRs, API compatibility, migration planning              |
| **web-explorer**       | medium | External docs/release-note extraction and evidence gathering            |
| **self-mentor**        | medium | Config quality checks, drift/leak detection, workflow hygiene           |

### Spawn rules

Codex selects agents autonomously based on task type (defined in `AGENTS.md`). You can also address agents by name in your prompt.

Automatic spawn patterns (from `AGENTS.md`):

- `sw-engineer` handles core implementation; on completion Codex can fan out to `qa-specialist` + `doc-scribe`
- `security-auditor` is used when tasks touch auth, credentials, external APIs, model weights, or deserialization
- `data-steward` is used when tasks touch data pipelines, splits, augmentation, or DataLoaders
- `squeezer` is used for profiling, throughput, and memory optimization tasks
- `ci-guardian` is used for CI workflow and publishing tasks

When to address by name vs letting Codex decide:

- Use by name when you want a specific perspective that task-type detection might not trigger
- Let Codex decide for broad tasks; orchestration can fan out automatically

## 🧠 Model Strategy & Profiles

### Model Strategy

Session defaults:

- `model = "gpt-5.4"`
- `review_model = "gpt-5.4"`
- `approval_policy = "on-request"`
- `sandbox_mode = "workspace-write"`

Reasoning allocation:

| Effort     | Roles                                                                 | Why                                                    |
| ---------- | --------------------------------------------------------------------- | ------------------------------------------------------ |
| **xhigh**  | qa-specialist, security-auditor                                       | Adversarial: exhaustive search for what could go wrong |
| **high**   | sw-engineer, squeezer, data-steward, oss-shepherd, solution-architect | Analytical: depth without unbounded budget             |
| **medium** | doc-scribe, ci-guardian, linting-expert, web-explorer, self-mentor    | Writing/config/research balance                        |

### Profiles

Four runtime profiles in `config.toml` cover common mode switches. Activate with `--profile <name>`:

```bash
codex --profile deep-review "full security audit of src/api/"
codex --profile fast-edit "fix the typo in the docstring"
```

| Profile       | What changes                                                        | When to use                                                  |
| ------------- | ------------------------------------------------------------------- | ------------------------------------------------------------ |
| `cautious`    | `approval_policy = "untrusted"`                                     | Unfamiliar codebases, production systems, destructive ops    |
| `fast-edit`   | `model = "gpt-5-codex"`, medium reasoning, low verbosity, 2 threads | Narrow mechanical edits where speed > depth                  |
| `fresh-docs`  | `web_search = "live"`, concise summaries                            | Questions about volatile docs, library versions, API changes |
| `deep-review` | `model = "gpt-5.4"`, `xhigh` reasoning, live web search             | Broad/high-risk changes needing maximum review depth         |

## 🧭 Skills In Codex

### Built-in vs mirrored commands

Codex built-in slash commands (for example `/fast`) work normally.

Mirrored workflow skills in `.codex/skills/*` are instruction assets, not custom slash commands. That means:

- `/investigate`, `/resolve`, `/review` are not recognized as Codex slash commands in this setup
- Use prompt-based invocation instead

### Usage examples

Interactive prompt usage:

```text
run investigate on this branch and find root cause of failing CI
run resolve on the current working tree and fix high-severity findings
run review, then develop, then audit for issue #42
```

One-shot shell usage:

```bash
codex "run investigate for current failing pytest and write findings artifact"
codex "run resolve on this diff and apply required quality gates"
```

Agent targeting examples:

```text
use the qa-specialist to review tests/ for missing edge cases
use the solution-architect to produce a minimal migration plan for this API change
use the self-mentor to review .codex drift and weak gates
```

## 🪙 RTK Integration

Codex hooks are enabled in `config.toml`:

```toml
[features]
codex_hooks = true
```

Configured hook files:

- `.codex/hooks.json`
- `.codex/hooks/rtk-enforce.js`

Behavior:

- If `rtk` is not installed, hook is a no-op
- If command is already `rtk ...`, hook is a no-op
- For known RTK-eligible prefixes, agents should invoke `rtk <cmd>` directly
- The hook is fail-open for eligible commands to avoid turning missed RTK routing into visible tool failures
- For excluded risky patterns (for example `git push`, destructive git deletes), it passes through normal approvals unchanged

Note: current Codex `PreToolUse` parsing does not apply in-place command rewrites via `updatedInput`. RTK routing is therefore documented in `.codex/AGENTS.md` instead of enforced with deny-and-rerun.

## 🏗️ Architecture

### Multi-agent execution model

Configured in `config.toml`:

```toml
max_threads = 4
max_depth = 2
job_max_runtime_seconds = 3600
```

How Codex schedules agents:

1. The lead agent (or base session) classifies the task and decides which specialists to spawn
2. Agents spawn concurrently up to `max_threads`
3. Agents at depth 2 cannot spawn further (`max_depth = 2`)
4. Jobs exceeding `job_max_runtime_seconds` are stopped and surfaced to the orchestrator

### Mirrored skills and gates

Mirrored workflow backbone:

- Core loop: `review`, `develop`, `resolve`, `audit`
- Extended set: `calibrate`, `release`, `investigate`, `sync`, `manage`, `analyse`, `optimize`, `research`

Shared gate references:

- `.codex/skills/_shared/quality-gates.md`
- `.codex/skills/_shared/run-gates.sh`
- `.codex/skills/_shared/write-result.sh`
- `.codex/skills/_shared/severity-map.md`

Artifact contract:

- `.reports/codex/<skill>/<timestamp>/result.json`

Calibration runner:

```bash
.codex/calibration/run.sh
```

### AGENTS.md layering

Codex loads agent instructions in layers, with more specific layers overriding broader ones:

- Global baseline: `~/.codex/AGENTS.md` or project `.codex/AGENTS.md`
- Project-local override: repo root `AGENTS.md`

Project-local instructions take precedence for overlapping rules.

### MCP server

`config.toml` configures:

```toml
[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
```

Purpose: live OpenAI/Codex documentation lookups for freshness-critical guidance.

## 🤝 Integration with Claude

→ Claude-side integration details: [`.claude/README.md` — Integration with Codex](../.claude/README.md#-integration-with-codex) · Full architecture: [root README](../README.md#-claude--codex-integration)

Typical division:

- Codex: focused mechanical implementation, diff-scoped edits, fast in-repo execution
- Claude: long-horizon orchestration, broader review topology, final synthesis

The combined workflow catches blind spots better than either tool alone.
