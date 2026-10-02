## Agent Resolution

> **Foundry plugin check**: run `{ find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md" 2>/dev/null; ls plugins/cc_foundry/agents/sw-engineer.md 2>/dev/null; } | head -1` — non-empty output = foundry available (installed cache or local source tree). Two prior forms WRONG: `ls ~/.claude/plugins/cache/ | grep -q foundry` (cache lists marketplace dirs like `borda-ai-rig/`, not plugin names → always misses); hardcoding marketplace slug `cache/borda-ai-rig/foundry/*` (re-breaks under non-standard marketplace name). `find … -path "*/foundry/*"` form is marketplace-agnostic. Uncertain → proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

Foundry **not** installed: substitute `foundry:X` with `general-purpose`, prepend role description + `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| -- | -- | -- | -- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:perf-optimizer` | `general-purpose` | `opus` | `You are a performance engineer. Profile before changing. Focus on CPU/GPU/memory/IO bottlenecks in Python/ML workloads.` |
| `foundry:doc-scribe` | `general-purpose` | `sonnet` | `You are a documentation specialist. Write Google-style docstrings and keep README content accurate and concise.` |
| `foundry:linting-expert` | `general-purpose` | `haiku` | `You are a static analysis specialist. Fix ruff/mypy violations, add missing type annotations, configure pre-commit hooks.` |
| `foundry:solution-architect` | `general-purpose` | `opusplan` | `You are a system design specialist. Evaluate scope, architectural dependencies, and feasibility. Return structured JSON only.` |
| `foundry:web-explorer` | `general-purpose` | `sonnet` | `You are a web research specialist. Fetch and extract structured information from URLs; summarize documentation, changelogs, and API references.` |

`research:scientist` same plugin — no fallback if research plugin installed. Skills with `--team` mode: fallback agents work, lower quality. Apply fallback only for agents skill dispatches to.

## Agent waits — no polling

<!-- policy-sibling: plugins/cc_develop/skills/_shared/agent-resolution.md (§Agent waits — no polling), plugins/cc_oss/skills/review/SKILL.md (background-spawn wait line) — same no-polling wait contract -->

Every background `Agent()` spawn: issue the batch in one response, end that turn with one line naming each agent in flight and the file it will write, and resume on its completion notification — the user never has to ask whether a run is still waiting.

- Never `Bash(true)`, a "waiting" line, or a sleep to hold the turn open — and never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for agents: the notification is the only resume signal. No fixed-interval polling.
- **Deadline**: the skill's stated cutoff, else 15 min from the spawn. A completion notification whose expected output is missing or empty, or any wake-up that finds an agent past its deadline with no output, marks it ⏱ `timed_out` at once — never wait on it further, and never leave it for the user to ask about. The skill's own timeout handling then applies.
- A ⏱ only informs. It never answers, skips, or defaults a user question.
