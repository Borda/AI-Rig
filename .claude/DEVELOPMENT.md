# 🛠️ Claude Code plugins — development and internals

← [Back to the Claude guide](README.md) · [Root README](../README.md)

This page is for contributors, maintainers, and agents that want technical detail: skill topologies, the handoff protocol, hook internals, and runtime state. Installation and everyday use live in the [Claude guide](README.md).

<details>
<summary><strong>Contents</strong></summary>

- [Agent relationship map](#agent-relationship-map)
- [Orchestration flow by skill](#orchestration-flow-by-skill)
- [File-based handoff protocol](#file-based-handoff-protocol)
- [Tiered review pipeline notes](#tiered-review-pipeline-notes)
- [Agent Teams internals](#agent-teams-internals)
- [Hook internals](#hook-internals)
- [task-log.js state machine](#task-logjs-state-machine)
- [Supplementary hooks](#supplementary-hooks)

</details>

## Agent relationship map

Skills choose only the roles relevant to the selected scope; this is a routing map, not a promise that every run launches every agent.

- **Foundry roles:**
  - `foundry:sw-engineer` owns Python and hook implementation.
  - `foundry:qa-specialist` validates public behavior.
  - `foundry:linting-expert` owns static-analysis rules.
  - `foundry:solution-architect` produces design and migration artifacts.
  - `foundry:perf-optimizer` measures bottlenecks before tuning.
  - `foundry:doc-scribe` owns technical docs.
  - `foundry:creator` owns outward-facing artifacts from approved outlines.
  - `foundry:web-explorer` fetches current external evidence.
  - `foundry:curator` audits configuration.
  - `foundry:challenger` attacks plans and unsupported conclusions.
- **OSS roles:**
  - `oss:gh-scraper` fetches raw GitHub vitality data for `/oss:analyse`.
  - Three `oss:repo-warden` instances score assigned axis groups.
  - `oss:shepherd` owns contributor and release communication.
  - `oss:cicd-steward` owns GitHub Actions reliability.
  - The collection and scoring agents are internal, not standalone user commands.
- **Research roles:**
  - `research:scientist` handles named papers, hypotheses, and experiments.
  - `research:data-steward` is manual-use data expertise and can delegate external collection to `foundry:web-explorer` when both are available.
- Sibling plugins disclose fallback routing when Foundry agents are absent. A missing required integration or specialist stops only where that workflow says it must.

## Orchestration flow by skill

Each skill follows a defined topology for how it composes agents.

### `/oss:review` — parallel fan-out, then consolidation

```text
Tier 0: git diff --stat (mechanical gate — skips trivial diffs)
Tier 1: Codex pre-pass (independent diff review, ~60s)
Tier 2: scope-selected agents (default capped at four; --full runs every selected dimension)
→ consolidator reads all findings → final report
→ shepherd writes --reply output (if flag present)
```

### `/develop:feature` — sequential with inner loops

```text
Step 1: sw-engineer (codebase analysis)
Step 2: sw-engineer (demo test — TDD contract)
Step 2 review: in-context validation gate
Step 3: sw-engineer (implementation) + qa-specialist (parallel)
Step 4: review+fix loop (max 3 cycles): sw-engineer → qa-specialist → linting-expert
Step 5: doc-scribe (docs update)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

### `/develop:fix` — reproduce-first

```text
Step 1: sw-engineer (root cause analysis)
Step 2: sw-engineer (regression test that fails)
Step 2 review: in-context validation gate
Step 3: sw-engineer (minimal fix)
Step 4: review+fix loop (max 3 cycles)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

### `/develop:refactor` — test-first

```text
Step 1: sw-engineer + linting-expert (coverage audit, parallel)
Step 2: qa-specialist (characterization tests)
Step 2 review: in-context validation gate
Step 3: sw-engineer (refactor)
Step 5: review+fix loop (max 3 cycles)
Quality stack: linting-expert → qa-specialist → Codex pre-pass
```

### `/research:topic` — research-first

```text
web-explorer (fetch current papers/docs) → scientist (deep analysis, writes to file)
→ consolidator reads findings → implementation plan
(--team: multiple scientist instances on competing method families)
```

### `/foundry:brainstorm` — conversational spec, then task breakdown

```text
idea mode:
  Step 1: context scan (Read README, Grep keywords)
  Step 2: AskUserQuestion (clarify, one at a time, max 10)
  Step 3: build tree loop (seed 3–5 branches → deepen/close/merge/add, max 10 ops)
  Step 4: Write tree doc → .plans/blueprint/YYYY-MM-DD-<slug>.md (Status: tree)
  Step 5: curator (tree quality audit — coverage, closure quality, open threads)
  Step 6: AskUserQuestion (approval gate) → suggest /foundry:brainstorm breakdown <tree>

breakdown mode (triggered by "breakdown <tree-or-spec>"):
  Auto-detects Status field:
  Status: tree → D1 present summary → D2 distillation questions (max 5)
           → D3 write spec section-by-section → D4 suggest next step
  Status: draft → B1 blocking questions → B2 action plan table → B3 post-plan prompt
```

### `/foundry:audit` — curator per file, then consolidation

```text
per-config-file: curator (reads file, writes findings to /tmp/audit-<ts>/<file>.md)
→ consolidator reads all finding files → ranked report with upgrade proposals
(upgrade mode: web-explorer fetches latest Claude Code docs first)
```

## File-based handoff protocol

When multiple analysis agents return findings inline, the orchestrator's context window fills with intermediate output it never uses directly. File-based handoff keeps the orchestrator clean for decision-making.

**When it applies:**

- Any skill spawning **2+ agents in parallel** for analysis or review.
- Any **single agent** expected to produce more than 500 tokens of findings.
- Exception: implementation agents (writing code) return inline — output is the deliverable.
- Exception: single-agent single-question spawns where output is inherently short (under 200 tokens).

**Agent contract** — a spawned agent must:

1. Write full output to `<RUN_DIR>/<agent-name>.md` using the Write tool.
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

1. Do NOT read agent files back into main context — delegate to a consolidator agent instead.
2. Collect compact envelopes (tiny — they stay in context).
3. Spawn a consolidator to read all `<RUN_DIR>/*.md` files and write the final report.

**Threshold:** 4+ agent files → mandatory consolidator; 2–3 files → the orchestrator may read directly if total content is under 2K tokens.

**RUN_DIR convention:**

- Ephemeral (per-run): `/tmp/<skill>-<timestamp>/` — created once before any spawns.
- Persistent (final reports): `.temp/`.

**Reference implementations:** `/foundry:calibrate` is canonical; `/foundry:audit` Step 3 (`curator` per file → consolidator); `/oss:review` Steps 3–6.

## Tiered review pipeline notes

- Every review skill gates cheap work before spawning expensive agents, so cheaper tiers short-circuit the pipeline when a diff is trivial or issues are already clear.
- For `/oss:review`, Codex runs as a full **co-reviewer** alongside T2 agents. Findings are independently consolidated rather than seeding agent prompts (unbiased review).

## Agent Teams internals

- Agent Teams is Claude Code's experimental multi-agent feature. Teams are always **user-invoked** — nothing auto-spawns. Auto-spawning teams would multiply token costs 5-10x on routine tasks; explicit invocation lets you make the cost/benefit call per run. Enabled via `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `settings.json`.
- **Model and effort settings:** agent frontmatter is the source of truth for each role's model and effort; these values can change with the shipped plugin.
- **Communication protocol:** inter-agent messages use AgentSpeak v2 (defined in `TEAM_PROTOCOL.md`), roughly 60% token savings versus natural language.
  - Status codes: `alpha`/`beta`/`gamma`/`delta`/`epsilon`/`omega`.
  - Action symbols: `+`/`-`/`~`/`!`.
  - File locking: `+lock`/`-lock`.
  - Priority prefixes: `!!` urgent, `..` FYI.
  - Lead-to-human communication uses normal English.
- **Security in teams:** no standalone security agent. `qa-specialist` automatically embeds OWASP Top 10 security checks when a task touches auth, payment flows, or user data.
- **Quality hooks:** `hooks/teammate-quality.js` handles `TeammateIdle` (redirects to pending tasks) and `TaskCompleted` (reserved for future quality gates).

## Hook internals

Foundry has 25 JavaScript files (21 event handlers, 4 helpers); `report-header-table.js` is a shared helper and not a separately registered handler. OSS registers four active handlers plus the shared report helper; Develop and Research each register three active handlers plus the helper. Codemap-py registers six Python hooks plus `_hookutil.py`, a shared helper.

| Hook                  | Event                       | Matcher     | Purpose                |
| --------------------- | --------------------------- | ----------- | ---------------------- |
| `task-log.js`         | lifecycle events            | all         | Session state tracking |
| `lint-on-save.js`     | PostToolUse                 | Write, Edit | Lint on save           |
| `md-compress.js`      | PreToolUse                  | Edit (.md)  | Token compression      |
| `rtk-rewrite.js`      | PreToolUse                  | Bash        | CLI output compression |
| `teammate-quality.js` | TeammateIdle, TaskCompleted | all         | Team quality gate      |
| `statusline.js`       | (statusLine)                | n/a         | Status bar             |

- `report-header-table` is a shared helper used by report gates.
- `blueprint-allow`, `sentinel-read-allow`, and Foundry's optional `github-read-allow` are decision modules called by `allow-dispatch`; none of the four is a separately registered event handler.
- The `hooks/lib/` libraries are `audit-log` (shared audit record writer), `approval-grants` (Foundry approval records and single-use token claims), and `shell-git` (Foundry shell lexer for `commit-guard`, `rule-inject`, and `gh-write-guard`).
- Each of the OSS, Develop, and Research plugins also ships `hooks/lib/audit-log`.
- Codemap-py hooks add ambient index status and session-sharded telemetry and narrowly discourage redundant structural scans. They fail open and are not required for scanning or querying.
- Together the Foundry hooks provide routing context, Bash auto-allow with an audit trail, report/artifact gates, push and protected-write guards, activity-scoped rule injection, setup and stale-install checks, optional command rewriting, session handover, status display, timing logs, and teammate-quality reminders.

## task-log.js state machine

`task-log.js` is the central event handler. It handles lifecycle events and maintains runtime state read by `statusline.js`.

**Event → action mapping:**

| Event                | Action                                                                                                                                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PreToolUse`         | Logs Task/Agent and Skill invocations to `logs/invocations.jsonl`; opens codex plugin session file; increments per-tool-type state file                                                                                                      |
| `PostToolUse`        | Closes Codex plugin session files when Skill(codex:\*) or Agent(codex:\*) completes; computes wall-clock timing delta from PreToolUse start marker and appends to `~/.claude/logs/timings.jsonl`                                             |
| `PostToolUseFailure` | Records timing with error status to timings.jsonl (same timing path as PostToolUse)                                                                                                                                                          |
| `UserPromptSubmit`   | Writes queue marker to `state/queue/` to light processing badge 💬 in statusline                                                                                                                                                             |
| `SubagentStart`      | Creates `state/agents/<id>.json` with agent type, model, color, start timestamp — one file per agent (no race)                                                                                                                               |
| `SubagentStop`       | Deletes per-agent file; appends completion entry to `invocations.jsonl`                                                                                                                                                                      |
| `PreCompact`         | Appends to `logs/compactions.jsonl`; extracts modified file paths from transcript; writes `state/session-context.md`                                                                                                                         |
| `Stop`               | Clears `state/tools/` — resets 🔧 row between turns (agents intentionally NOT cleared — may still be running); clears `state/queue/` processing markers (dismisses 💬 badge) and removes orphaned timing start markers from `state/timings/` |
| `SessionEnd`         | Deletes entire `/tmp/claude-state-<session>/` directory (agents, tools, codex, queue, timings, dedup locks); runs `git worktree prune`; removes orphaned worktrees >2h                                                                       |

**State files layout:**

```text
/tmp/claude-state-<session>/
├── agents/<id>.json        # one per active subagent (created at start, deleted at stop)
├── codex/<id>.json         # one per active codex plugin session
├── skills/<id>.json        # one per in-flight Skill() call
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

- Agents: 10-minute safety net — files older than 10 min with no corresponding Stop event indicate a crashed agent; the statusline excludes them.
- Codex activity uses the same session-scoped state and active-agent safety filters; the renderer does not promise a separate Codex timeout row.
- Worktrees: 2-hour cutoff in SessionEnd cleanup.

**Inline `SessionStart` hook** (shell command, not a JavaScript file): `claude auth status > ~/.claude/state/subscription.json` snapshots billing plan data for the status line asynchronously. The plugin also registers `session-restore.js` for the `clear` matcher.

## Supplementary hooks

Registered by the Foundry hook manifest alongside `task-log.js`.

### `lint-on-save.js` (PostToolUse — Write, Edit)

- Closes the gap between "Claude edits a file" and "a human runs pre-commit" by linting every file the moment it is written.
- Runs `pre-commit run --files <path>` on each Write/Edit and exits 2 on failure, so Claude sees diagnostics and applies the fix immediately.
- No-op when `.pre-commit-config.yaml` is absent or pre-commit is not installed.

### `md-compress.js` (PreToolUse — Edit only, `.md` files only)

- Normalizes token-wasteful whitespace in the file being edited, in place, right before the edit runs, and normalizes `old_string` the same way so the pre-normalization match still finds its target.
- Collapses table column padding (2+ spaces → 1), consecutive blank lines, and trailing whitespace — all outside fenced code blocks. The write is atomic (write-then-rename).
- Deliberately does *not* run on Read: an earlier version did, to save Read-time tokens, but normalizing on every Read silently rewrote table alignment in any `.md` file Claude merely looked at, including files nobody asked to touch. It was dropped in favor of Edit-only scope, which only touches files someone is actually editing.

### `rtk-rewrite.js` (PreToolUse — Bash)

- Rewrites supported CLI calls through the optional RTK proxy (`git status` → `rtk git status`).
- RTK performs format-aware compression for Git, pytest, and build output; savings depend on the command and output, and compressed output still requires normal verification.
- No-op when RTK is not installed — see root [README → Optional integrations and add-ons](../README.md#optional-integrations-and-add-ons).
