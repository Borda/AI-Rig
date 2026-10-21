## Agent Resolution

> **Foundry plugin check**: run `{ find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md" 2>/dev/null; ls plugins/cc_foundry/agents/sw-engineer.md 2>/dev/null; } | head -1` — non-empty output = foundry available (installed cache or local source tree). Two prior forms WRONG: `ls ~/.claude/plugins/cache/ | grep -q foundry` (cache lists marketplace dirs like `borda-ai-rig/`, not plugin names → always misses); hardcoding marketplace slug `cache/borda-ai-rig/foundry/*` (re-breaks under non-standard marketplace name). `find … -path "*/foundry/*"` form is marketplace-agnostic. Uncertain → proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

Foundry **not** installed: substitute `foundry:X` with `general-purpose`, prepend role description + `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| -- | -- | -- | -- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `sonnet` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:perf-optimizer` | `general-purpose` | `opus` | `You are a performance engineer. Profile before changing. Focus on CPU/GPU/memory/IO bottlenecks in Python/ML workloads.` |
| `foundry:doc-scribe` | `general-purpose` | `sonnet` | `You are a documentation specialist. Write Google-style docstrings and keep README content accurate and concise.` |
| `foundry:linting-expert` | `general-purpose` | `sonnet` | `You are a static analysis specialist. Fix ruff/mypy violations, add missing type annotations, configure pre-commit hooks.` |
| `foundry:solution-architect` | `general-purpose` | `opus` | `You are a system design specialist. Evaluate scope, architectural dependencies, and feasibility. Return structured JSON only.` |
| `foundry:web-explorer` | `general-purpose` | `sonnet` | `You are a web research specialist. Fetch and extract structured information from URLs; summarize documentation, changelogs, and API references.` |

`research:scientist` same plugin — no fallback if research plugin installed. Skills with `--team` mode: fallback agents work, lower quality. Apply fallback only for agents skill dispatches to.

## Agent waits — no polling

<!-- policy-sibling: plugins/cc_develop/skills/_shared/agent-resolution.md (§Agent waits — no polling), plugins/cc_oss/skills/review/SKILL.md (background-spawn wait line) — same no-polling wait contract -->

Every background `Agent()` spawn: issue the batch in one response, end that turn with one line naming each agent in flight and the file it will write, and resume on its completion notification — the user never has to ask whether a run is still waiting.

- Never `Bash(true)`, a "waiting" line, or a sleep to hold the turn open — and never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for agents: the notification is the only resume signal. No fixed-interval polling and no liveness probe (`find -newer` or similar) — the deadline file below is the only liveness mechanism.
- **Arm a deadline per agent.** Before the first spawn of the run, the arm block below creates this run's watch directory (it can ride with any earlier real call). In the same response as each spawn batch, write `<WATCH_DIR>/agent-watch-<batch>.tsv` with the Write tool — one row per agent, `<name>\t<deliverable path, or - for an envelope-only agent>\t<deadline seconds>`. The file's write time is the spawn time, so no clock value is ever typed. A re-spawn of a batch rewrites its file.
- **Check once per wake-up.** On every completion or idle notification — and whenever the user writes while agents are open — run the check block once before acting on any agent output, and act on every row: `done` → consume it · `timed_out` → ⏱ `timed_out` now · a row whose notification arrived but which is still `pending`/`awaiting-envelope` (finished or idle without its deliverable) → ⏱ `timed_out` now, never wait for it further · rows still open with no notification → end the turn, printing one status line per name in the check's top-level `pending` list (agent, batch, `elapsed_s`, `seconds_left`). The skill's own timeout handling then applies.
- A ⏱ only informs. It never answers, skips, or defaults a user question.

| Batch | Spawn sites | Deadline (s) |
| -- | -- | -- |
| `verify` | `/research:verify` `research:scientist` audit (`HARD_CUTOFF`) | 900 |
| `oracle` | `/research:run` R0 hypothesis oracles (`modes/hypothesis-pipeline.md`) | 1800 |
| `team` | `/research:run` and `/research:topic` team-mode teammates and consolidator | 1800 |
| `iteration` | `/research:run` R5 Phase 2 specialist | 1800 |
| `fortify` | `/research:fortify` `research:scientist` spawns (its 15-min deadline) | 900 |
| `other` | any other spawn | 900 |

Arm — once per run, before the first spawn (prints the absolute `WATCH_DIR` the Write tool needs):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WATCH_DIR < "${TMPDIR:-/tmp}/research-agent-watch-dir-${CSID}" 2>/dev/null || WATCH_DIR=""
[ -n "$WATCH_DIR" ] || { WATCH_DIR="$PWD/.temp/research/agent-watch-$(date -u +%Y-%m-%dT%H-%M-%SZ)"; echo "$WATCH_DIR" > "${TMPDIR:-/tmp}/research-agent-watch-dir-${CSID}"; }
mkdir -p "$WATCH_DIR" && echo "WATCH_DIR=$WATCH_DIR"  # timeout: 5000
```

Check — once per wake-up:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WATCH_DIR < "${TMPDIR:-/tmp}/research-agent-watch-dir-${CSID}" 2>/dev/null || WATCH_DIR=""
[ -n "$WATCH_DIR" ] || { echo "! BLOCKED — agent-watch dir sentinel missing; run the arm block before spawning"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/agent_watch.py" --state-dir "$WATCH_DIR"  # timeout: 5000
```

A deadline is enforced at the next wake-up — another agent's notification or a user message — since nothing else can wake a waiting run; the check then reports every agent past its deadline at once.
