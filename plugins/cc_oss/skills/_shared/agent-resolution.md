## Agent Resolution

> **Foundry plugin check**: run `{ find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md" 2>/dev/null; ls plugins/cc_foundry/agents/sw-engineer.md 2>/dev/null; } | head -1` — non-empty output = foundry available (installed cache or local source tree). Two prior forms WRONG: `ls ~/.claude/plugins/cache/ | grep -q foundry` (cache lists marketplace dirs like `borda-ai-rig/`, not plugin names, always misses); hardcoding marketplace slug `cache/borda-ai-rig/foundry/*` (re-breaks under non-standard marketplace name). `find … -path "*/foundry/*"` form is marketplace-agnostic. Uncertain: proceed as if foundry available, common case; fall back only if agent dispatch explicitly fails.

Foundry **not** installed: substitute `foundry:X` with `general-purpose`, prepend role description + `model: <model>` to spawn call.

| foundry agent | Fallback | Model | Role description prefix |
| -- | -- | -- | -- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:perf-optimizer` | `general-purpose` | `opus` | `You are a performance engineer. Profile before changing. Focus on CPU/GPU/memory/IO bottlenecks in Python/ML workloads.` |
| `foundry:doc-scribe` | `general-purpose` | `sonnet` | `You are a documentation specialist. Write Google-style docstrings and keep README content accurate and concise.` |
| `foundry:linting-expert` | `general-purpose` | `haiku` | `You are a static analysis specialist. Fix ruff/mypy violations, add missing type annotations, configure pre-commit hooks.` |
| `foundry:solution-architect` | `general-purpose` | `opus` | `You are a system design specialist. Produce ADRs, interface specs, and API contracts — read code, produce specs only.` |
| `foundry:challenger` | `general-purpose` | `opus` | `You are an adversarial reviewer. Challenge the proposed plan or design across 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply a refutation step — try to disprove each challenge before keeping it. Report only challenges that survive refutation.` |

Skills with `--team` mode: fallback agents work, lower quality. Apply fallback only to agents the skill actually dispatches.

## Task hygiene

Task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone: every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block. Workflow-specific task names and timing remain with the consuming skill.

## Agent budget

Each spawn costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call. Measured on a real PR review: 11 agents, ~55% of the whole bill. Work under ~73 calls is cheaper done inline: spawn nothing for work displacement alone; isolation-motivated spawns (adversarial reviewer, distinct specialist role, model tier, worktree) run regardless of size. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion: `partial: true` plus what was finished.
