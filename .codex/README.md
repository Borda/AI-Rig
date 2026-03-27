# 🤖 Codex CLI — Deep Reference

← [Back to root README](../README.md) · [Claude deep reference](../.claude/README.md)

Multi-agent configuration for [OpenAI Codex CLI](https://github.com/openai/codex) (Rust implementation). This file covers agent spawn rules, model strategy, runtime profiles, execution architecture, and Claude integration internals. For the high-level overview and cross-tool workflow sequences, see the root README.

<details>
<summary><strong>Contents</strong></summary>

- [🔄 Config Sync](#-config-sync)
- [🧩 Agents](#-agents)
  - [Reference table](#reference-table)
  - [Spawn rules](#spawn-rules)
- [🧠 Model Strategy & Profiles](#-model-strategy--profiles)
  - [Model Strategy](#model-strategy)
  - [Profiles](#profiles)
- [🏗️ Architecture](#-architecture)
  - [Multi-agent execution model](#multi-agent-execution-model)
  - [AGENTS.md layering](#agentsmd-layering)
  - [MCP server](#mcp-server)
- [🤝 Integration with Claude](#-integration-with-claude)

</details>

## 🔄 Config Sync

This repo (`.codex/`) is the source of truth — home (`~/.codex/`) is a downstream copy:

```bash
cp -r .codex/ ~/.codex/    # activate globally (config_file paths are relative — no rewriting needed)
```

Run after editing any agent config, `config.toml`, or `AGENTS.md`. Unlike `.claude/`, there is no drift-detection command — copy directly.

A project-local `AGENTS.md` at the repo root extends the global `~/.codex/AGENTS.md` automatically — no additional config needed.

<details>
<summary><strong>Install</strong></summary>

```bash
npm install -g @openai/codex    # install Codex CLI
cp -r .codex/ ~/.codex/         # activate globally (config_file paths are relative — no rewriting needed)
```

</details>

## 🧩 Agents

### Reference table

All agents run on `gpt-5.3-codex` — differentiation is via reasoning effort only (see [Model Strategy & Profiles](#-model-strategy--profiles)).

| Agent                | Effort | Purpose                                                                 |
| -------------------- | ------ | ----------------------------------------------------------------------- |
| **sw-engineer**      | high   | SOLID implementation, doctest-driven dev, ML pipeline architecture      |
| **qa-specialist**    | xhigh  | Edge-case matrix, The Borda Standard, adversarial test review           |
| **squeezer**         | high   | Profile-first optimization, GPU throughput, memory efficiency           |
| **doc-scribe**       | medium | 6-point Google/Napoleon docstrings, README stewardship, CHANGELOG       |
| **security-auditor** | xhigh  | OWASP Python, ML supply chain, secrets, CI/CD hygiene *(read-only)*     |
| **data-steward**     | high   | Split leakage, DataLoader reproducibility, augmentation correctness     |
| **ci-guardian**      | medium | GitHub Actions, trusted PyPI publishing, pre-commit, flaky tests        |
| **linting-expert**   | medium | ruff, mypy, pre-commit config, rule progression, suppression discipline |
| **oss-shepherd**     | high   | Issue triage, PR review, SemVer, pyDeprecate, release checklist         |

### Spawn rules

Codex selects agents autonomously based on task type (defined in `AGENTS.md`). You can also address agents by name in your prompt.

**Automatic spawn patterns** (from `AGENTS.md`):

- `sw-engineer` handles core implementation; on completion Codex fans out to `qa-specialist` + `doc-scribe` concurrently
- `security-auditor` is spawned whenever the task touches auth, credentials, external APIs, model weights, or pickle loading
- `data-steward` is spawned when the task touches data pipelines, splits, augmentation, or DataLoaders
- `squeezer` is spawned for profiling, throughput, and memory optimization tasks
- `ci-guardian` is spawned for CI workflow and publishing tasks

**When to address by name** vs letting Codex decide:

- Use by name when you want a specific perspective that task-type detection might not trigger (e.g., `"use the security-auditor to review src/api/auth.py"` even if the task description looks like a feature)
- Let Codex decide for broad tasks — multi-agent fan-out happens automatically per the spawn matrix in `AGENTS.md`

**Fan-out example:**

```
sw-engineer (implement) → qa-specialist + doc-scribe (concurrent)
                       ↘ security-auditor (if auth or secrets scope detected)
```

## 🧠 Model Strategy & Profiles

### Model Strategy

All agents use `gpt-5.3-codex`. Differentiation is via `model_reasoning_effort`:

| Effort     | Roles                                             | Why                                                    |
| ---------- | ------------------------------------------------- | ------------------------------------------------------ |
| **xhigh**  | qa-specialist, security-auditor                   | Adversarial: exhaustive search for what could go wrong |
| **high**   | sw-engineer, squeezer, data-steward, oss-shepherd | Analytical: depth without unbounded budget             |
| **medium** | doc-scribe, ci-guardian, linting-expert           | Writing/config: quality over deductive intensity       |

The review model (`gpt-5.4`, set via `review_model` in `config.toml`) is used for Codex's own internal review pass — separate from agent effort levels.

### Profiles

Four runtime profiles in `config.toml` cover common mode switches. Activate with `--profile <name>`:

```bash
codex --profile deep-review "full security audit of src/api/"
codex --profile fast-edit "fix the typo in the docstring"
```

| Profile       | What changes                                                          | When to use                                                  |
| ------------- | --------------------------------------------------------------------- | ------------------------------------------------------------ |
| `cautious`    | `approval_policy = "untrusted"` — every command requires confirmation | Unfamiliar codebases, production systems, destructive ops    |
| `fast-edit`   | `gpt-5-codex` model, medium reasoning, concise summary, 2 max threads | Narrow mechanical edits where speed > depth                  |
| `fresh-docs`  | `web_search = "live"`, concise summary                                | Questions about volatile docs, library versions, API changes |
| `deep-review` | `gpt-5.4` model, xhigh reasoning, live web search, concise summary    | Broad or high-risk changes needing maximum review depth      |

The default profile (no flag) is `approval_policy = "on-request"` with `gpt-5.3-codex` at medium reasoning effort — optimized for everyday agentic coding.

**When to use a profile vs editing effort directly:** Use a profile for session-level mode switches; edit `agents/*.toml` only for permanent per-agent changes. Profiles override the base config without touching the files.

## 🏗️ Architecture

### Multi-agent execution model

Configured in `config.toml`:

```toml
max_threads = 4                # max concurrent agents
max_depth = 2                  # max spawn depth (orchestrator → agent → sub-agent)
job_max_runtime_seconds = 3600 # hard 1-hour cutoff per Codex session
```

**How Codex schedules agents:**

1. The lead agent (or the base Codex session) classifies the task and determines which specialist agents to spawn
2. Agents spawn concurrently up to `max_threads`; each runs in its own sandboxed context
3. Agents at depth 2 (spawned by a spawned agent) cannot themselves spawn further — `max_depth = 2` prevents unbounded recursion
4. If a sub-agent's work exceeds `job_max_runtime_seconds`, it is killed and its partial output is surfaced to the orchestrator

`sandbox_mode = "workspace-write"` — agents can read/write within the workspace but cannot make outbound network calls or run arbitrary system commands without approval (unless the profile relaxes this).

### AGENTS.md layering

Codex loads agent instructions in layers, with more specific layers overriding the global baseline:

**Layer 1 — Global baseline** (`~/.codex/AGENTS.md` or `.codex/AGENTS.md` in this repo):

- The Borda Standard: coding quality rules, Python 3.10+ baseline, doctest-driven development, naming conventions
- Freshness policy: prefer live docs over cached assumptions for volatile tooling
- Runtime profile descriptions
- Spawn rules: which agent types to invoke for which task categories

**Layer 2 — Project-local** (`AGENTS.md` at the repo root):

- Environment bootstrap commands (how to install deps, activate env)
- Lint/type-check/test/build commands for this specific project
- Package manager (uv, pip, poetry)
- Release entrypoint and acceptance criteria
- Any project-specific overrides to the global Borda Standard

When project-local `AGENTS.md` exists, it extends and overrides the global baseline — Codex merges both. Project-local guidance takes precedence for all overlapping rules.

**What project-local AGENTS.md must define** (per Borda Standard):

- Environment bootstrap command
- Lint + type-check + test + build commands
- Package manager
- Release entrypoint
- Task completion criteria

### MCP server

`config.toml` configures one MCP server:

```toml
[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
```

**Purpose:** Gives Codex and its agents live access to OpenAI's developer documentation — API references, model capabilities, SDK changelogs. Used by the freshness policy: "For OpenAI and Codex-specific questions, prefer the configured OpenAI developer docs MCP server when available, then fall back to primary web sources."

**Activation:** The MCP server is always-on for Codex sessions (no opt-in required, unlike Claude's `colab-mcp`). Codex queries it automatically when the task touches OpenAI APIs or Codex-specific behavior.

## 🤝 Integration with Claude

→ Claude's perspective on this integration: [`.claude/README.md` — Integration with Codex](../.claude/README.md#-integration-with-codex) · Full architecture: [root README](../README.md#-claude--codex-integration)

**What Claude delegates to Codex:**

- Mechanical, diff-scoped tasks: add docstrings, rename symbols, add type annotations across a module
- PR review comment application (via `/resolve`)
- Codex pre-pass in the tiered review pipeline (Tier 1) — diff-focused review before Claude's parallel agents

**What Claude retains:**

- Long-horizon planning and research (`/survey`, `/research`, `/develop plan`)
- Orchestration of multiple agents in defined topologies
- Judgment calls: design decisions, spec approval, test validity assessment
- Final validation: Claude always reviews Codex output with lint + tests before marking work complete

**Why the division works:** Claude has a mental model of which files are "in scope" for a task; Codex reads the diff and codebase independently, without that context. Their blind spots are complementary — the union of both passes catches more than either alone.
