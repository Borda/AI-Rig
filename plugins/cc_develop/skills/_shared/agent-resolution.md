## Agent Resolution

> **Foundry plugin check**: run `{ find ~/.claude/plugins/cache -maxdepth 5 -path "*/foundry/*/agents/sw-engineer.md" 2>/dev/null; ls plugins/cc_foundry/agents/sw-engineer.md 2>/dev/null; } | head -1` — non-empty output = foundry available (installed cache or local source tree). Two prior forms WRONG: `ls ~/.claude/plugins/cache/ | grep -q foundry` (cache lists marketplace dirs like `borda-ai-rig/`, not plugin names → always misses); hardcoding marketplace slug `cache/borda-ai-rig/foundry/*` (re-breaks under non-standard marketplace name). `find … -path "*/foundry/*"` form is marketplace-agnostic. Uncertain → proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

Foundry **not** installed → substitute `foundry:X` with `general-purpose`, prepend role + `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| -- | -- | -- | -- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:perf-optimizer` | `general-purpose` | `opus` | `You are a performance engineer. Profile before changing. Focus on CPU/GPU/memory/IO bottlenecks in Python/ML workloads.` |
| `foundry:doc-scribe` | `general-purpose` | `sonnet` | `You are a documentation specialist. Write Google-style docstrings and keep README content accurate and concise.` |
| `foundry:linting-expert` | `general-purpose` | `haiku` | `You are a static analysis specialist. Fix ruff/mypy violations, add missing type annotations, configure pre-commit hooks.` |
| `foundry:solution-architect` | `general-purpose` | `opus` | `You are a system design specialist. Produce ADRs, interface specs, and API contracts — read code, produce specs only.` |
| `foundry:challenger` | `general-purpose` | `opus` | `You are an adversarial reviewer. Challenge the proposed plan or design across 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply a refutation step — try to disprove each challenge before keeping it. Report only challenges that survive refutation.` |

Skills with `--team` mode: team spawning works with fallback agents, output quality lower. Apply fallback only for agents skill actually dispatches.

**Model aliases on fallback**: challenger + solution-architect → `opus`; doc-scribe → `sonnet`; linting-expert → `haiku`. Substituting `general-purpose`: prepend role + target model to spawn prompt: `"Act as <role>. Use <model> quality reasoning."` — else inherits session model.

## Agent waits — no polling, per-agent deadlines

<!-- policy-sibling: plugins/cc_oss/skills/resolve/SKILL.md (§Agent wait discipline — same agent_watch.py contract), plugins/cc_research/skills/_shared/agent-resolution.md (§Agent waits — no polling) -->

Every `Agent()` spawn runs in the background. Waiting on one is **never** a tool call:

- **Never** `Bash(true)`, a "waiting" line, or a sleep to hold the turn open — and never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for agents: the notification is the only resume signal. No fixed-interval polling; a skill that defines a liveness probe runs it at most once per wake-up.
- **Arm a deadline per agent.** Before the first spawn of the run, the arm block below creates this run's watch directory (it can ride with any earlier real call). In the same response as each spawn batch, write `<WATCH_DIR>/agent-watch-<batch>.tsv` with the Write tool — one row per agent, `<name>\t<deliverable path, or - for an envelope-only agent>\t<deadline seconds>`. The file's write time is the spawn time, so no clock value is ever typed. A re-spawn of a batch rewrites its file.
- **Spawn turn names what is in flight.** End the spawning turn with one line naming each agent in flight and its deliverable — the user never has to ask whether a run is still waiting.
- **Check once per wake-up.** On every completion or idle notification — and whenever the user writes while agents are open — run the check block once before acting on any agent output, and act on every row: `done` → consume it · `timed_out` → ⏱ `timed_out` now · a row whose notification arrived but which is still `pending`/`awaiting-envelope` (finished or idle without its deliverable) → ⏱ `timed_out` now, never wait for it further · rows still open with no notification → end the turn. Before ending it, print one status line per name in the check's top-level `pending` list — agent, batch, `elapsed_s` since spawn and `seconds_left` to its deadline — so in-flight progress stays visible. Never leave a stalled agent for the user to notice: a run the user returns to already shows every ⏱.
- **A ⏱ only informs.** It never answers, skips, or defaults a user question, and never counts as a clean result. A timed-out analysis or test-writing agent's deliverable is produced inline by the lead. A timed-out `foundry:challenger` is never read as "no findings": invoke `AskUserQuestion` once — (a) retry the challenge once · (b) proceed unchallenged, ⏱ recorded in the Final Report · (c) abort.

| Batch | Spawn sites | Deadline (s) |
| -- | -- | -- |
| `scope` | refactor/fix/debug Step 1 `foundry:sw-engineer` analysis | 1800 |
| `challenge` | `## Challenger gate` `foundry:challenger` (refactor, fix, debug) | 900 |
| `tests` | refactor Step 3 characterization / fix Step 2 Part B reproduction `foundry:qa-specialist` | 1200 |
| `team` | team-mode teammates (refactor, fix, debug, feature) | 1800 |
| `review` | `/develop:review` Step 3 spawn units and Agent 0 | 900 |
| `verify` | `/develop:review` Step 4 cross-validation verifiers | 900 |
| `consolidate` | `/develop:review` Step 5 consolidator | 900 |
| `other` | any other spawn | 900 |

Arm — once per run, before the first spawn (prints the absolute `WATCH_DIR` the Write tool needs):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WATCH_DIR < "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}" 2>/dev/null || WATCH_DIR=""
[ -n "$WATCH_DIR" ] || { WATCH_DIR="$PWD/.temp/develop/agent-watch-$(date -u +%Y-%m-%dT%H-%M-%SZ)"; echo "$WATCH_DIR" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"; }
mkdir -p "$WATCH_DIR" && echo "WATCH_DIR=$WATCH_DIR"  # timeout: 5000
```

Check — once per wake-up:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WATCH_DIR < "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}" 2>/dev/null || WATCH_DIR=""
[ -n "$WATCH_DIR" ] || { echo "! BLOCKED — agent-watch dir sentinel missing; run the arm block before spawning"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/agent_watch.py" --state-dir "$WATCH_DIR"  # timeout: 5000
```

A deadline is enforced at the next wake-up — another agent's notification or a user message — since nothing else can wake a waiting run; the check then reports every agent past its deadline at once.
