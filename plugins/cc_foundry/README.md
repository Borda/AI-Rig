# 🏭 foundry — Claude Code Plugin

OSS Claude Code configuration for Python/ML projects: 10 specialist agents, 11 skills, event-driven hooks, and repeatable audit and calibration workflows.

> Optional integrations: `/oss:review` and `/oss:release` add release/review workflows (requires the `oss` plugin); `/develop:feature` and `/develop:fix` add implementation workflows (requires the `develop` plugin); `/research:run` and `/research:topic` add ML research workflows (requires the `research` plugin).

______________________________________________________________________

<details>

<summary><strong>📋 Contents</strong></summary>

- [What is foundry?](#-what-is-foundry)
- [Why foundry?](#-why-foundry)
- [Install](#-install)
- [Quick start](#-quick-start)
- [Current boundaries](#-current-boundaries)
- [Skills reference](#-skills-reference)
  - [`/foundry:setup`](#foundrysetup)
  - [`/foundry:audit`](#foundryaudit)
  - [`/foundry:calibrate`](#foundrycalibrate)
  - [`/foundry:manage`](#foundrymanage)
  - [`/foundry:brainstorm`](#foundrybrainstorm)
  - [`/foundry:investigate`](#foundryinvestigate)
  - [`/foundry:profile`](#foundryprofile)
  - [`/foundry:distill`](#foundrydistill)
  - [`/foundry:session`](#foundrysession)
  - [`/foundry:create`](#foundrycreate)
  - [`/foundry:humanizer`](#foundryhumanizer)
- [Agents reference](#-agents-reference)
  - [foundry:sw-engineer](#foundrysw-engineer)
  - [foundry:solution-architect](#foundrysolution-architect)
  - [foundry:qa-specialist](#foundryqa-specialist)
  - [foundry:linting-expert](#foundrylinting-expert)
  - [foundry:perf-optimizer](#foundryperf-optimizer)
  - [foundry:doc-scribe](#foundrydoc-scribe)
  - [foundry:web-explorer](#foundryweb-explorer)
  - [foundry:curator](#foundrycurator)
  - [foundry:challenger](#foundrychallenger)
  - [foundry:creator](#foundrycreator)
- [Agent relationships](#-agent-relationships)
- [Rules installed](#-rules-installed)
- [Configuration](#-configuration)
- [Troubleshooting](#-troubleshooting)
- [Plugin structure](#-plugin-structure)
- [Bin helper inventory](#bin-helper-inventory)
- [Upgrade](#upgrade)
- [Uninstall](#uninstall)
- [Development / testing](#-development--testing)
- [Contributing / feedback](#-contributing--feedback)

</details>

______________________________________________________________________

## 🤔 What is foundry?

foundry is the configuration and workflow layer for Claude Code on Python/ML OSS projects. It gives Claude Code ten specialist agents, eleven namespaced skills, rules, and event-driven hooks for routing work, checking configuration, measuring instruction quality, and recording lessons for review.

The problem it solves is workflow drift: a generalist session can mix architecture, implementation, tests, docs, and tooling without clear ownership, while corrections disappear between sessions. Foundry provides explicit role boundaries, repeatable audits and calibration runs, and project-local state for the parts it supports. It does not replace application-code, release, or ML-research plugins.

______________________________________________________________________

## 🎯 Why foundry?

**Without**: one model handles architecture, implementation, docs, linting, testing, performance — no boundary enforcement. Corrections evaporate per session. No way to know if agent accuracy drifted.

**With**:

- `/foundry:audit` catches config drift before it becomes debugging session
- `/foundry:calibrate` measures recall vs stated confidence — know exactly where agents fall short
- `/foundry:manage` creates, renames, deletes agents with full cross-reference propagation, one command
- `/foundry:brainstorm` turns vague idea into approved spec before any code written
- `/foundry:distill` converts accumulated corrections into durable rules + agent instruction updates
- Hooks keep lint, task tracking, teammate quality gates, and report checks running around Claude tool events (including Write/Edit operations)

Use `/foundry:audit` for structural checks, `/foundry:calibrate` for measured routing and agent behavior, and `/foundry:distill` to turn recurring corrections into proposed instruction updates.

______________________________________________________________________

## 📦 Install

**Prerequisites**: Claude Code with plugin support; Python 3.10+ and `git` (setup runs from a project repository); `jq` on PATH for setup/audit JSON work; and Node.js on PATH for the plugin's hooks.

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install foundry@borda-ai-rig
```

Companion plugins for full workflow suite:

```bash
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

**One-time setup** — run inside Claude Code after install:

```text
/foundry:setup
```

Merges `statusLine`, `permissions.allow`, `permissions.deny`, `enabledPlugins`, and (when pinned by the project) `advisorModel` into `~/.claude/settings.json`; symlinks all rule files into `~/.claude/rules/` as `foundry-<name>.md` and `TEAM_PROTOCOL.md` into `~/.claude/`. Run it from the project repository root. Idempotent — safe to re-run.

**After any plugin upgrade**, re-run `/foundry:setup` — auto-replaces stale foundry symlinks, removes rules gone from new version. No prompt for old-version symlinks.

______________________________________________________________________

## ⚡ Quick start

Run setup once, then check the installation from the project repository root:

```text
/foundry:setup
```

The first useful workflow is:

```text
/foundry:audit setup
```

It produces a structured report of system config checks (hooks, `settings.json`, plugin integration, and symlinks). Treat any critical findings as blockers and resolve the report's suggested next action.

Follow with:

```text
/foundry:calibrate routing --fast
```

Quick routing benchmark — measures whether Claude Code dispatches synthetic tasks to the right agent. The calibration threshold is 90%; use the measured result to identify routing gaps rather than treating it as a guarantee.

______________________________________________________________________

## 🧭 Current boundaries

- Foundry manages Claude Code configuration and workflow guidance; it does not implement application code or provide release-management and ML-research workflows. For those workflows, install `develop` (requires the `develop` plugin), `oss` (requires the `oss` plugin), or `research` (requires the `research` plugin).
- Calibration uses synthetic problems with quasi-ground-truth. It is an instruction-quality signal, not a production benchmark or guarantee of agent correctness.
- Hooks are event-driven and depend on Claude Code loading the enabled plugin plus Node.js. Most report or nudge; selected guards intentionally gate unsafe pushes or incomplete follow-up prompts.
- Setup intentionally leaves some state behind on uninstall: merged settings, `foundry-*.md` rule links, and `TEAM_PROTOCOL.md` need manual review or removal; see [Uninstall](#uninstall).
- Profile cost output is best-effort and uses public list rates, not an account billing statement; when transcripts are unavailable, the clock report can still run without the cost section.

These are current boundaries of the shipped plugin, not promises about future support.

______________________________________________________________________

## 🔧 Skills reference

### `/foundry:setup`

Post-install setup. Merges settings, creates symlinks. Run once after install, again after any upgrade.

```text
/foundry:setup
/foundry:setup --approve      # non-interactive; auto-accepts all recommended choices
```

What it does:

- Detects Python 3.10+ (`python` / `py -3` / `python3`); installs `~/.local/bin/python` shim when `python` absent or resolves to Windows Store stub
- Backs up `~/.claude/settings.json` before touching
- Merges `statusLine`, `permissions.allow`, `permissions.deny`, `enabledPlugins`, `advisorModel` (copied from project `.claude/settings.json` when pinned)
- Copies `permissions-guide.md` to `.claude/` (only if absent — preserves project-local edits)
- Symlinks all `plugins/cc_foundry/rules/*.md` into `~/.claude/rules/` as `foundry-<name>.md`, plus `TEAM_PROTOCOL.md` into `~/.claude/`; on upgrade, auto-replaces stale foundry symlinks, removes rules gone from the current version, and migrates pre-namespace unprefixed links it provably owns.
- Removes stale `hooks` block from settings if present (hooks now register via plugin manifest)

Hooks (`hooks.json`) register automatically when plugin enabled — `/foundry:setup` never touches them directly.

Codemap integration metadata uses the `codemap-py.integration.v2` managed-block body while retaining the `v1` sentinel format; use `codemap-py integrate audit` for read-only integration evidence.

______________________________________________________________________

### `/foundry:audit`

Full-sweep quality audit of `.claude/` config + all `plugins/*/` agent and skill files. Catches broken cross-references, inventory drift, model-tier mismatches, description overlap, doc staleness. Reports findings by severity; fix level chosen from always-fire follow-up gate after report. Adversarial mode challenges every claim via `foundry:challenger` + Codex.

```text
/foundry:audit                      # full sweep, report only — gate offers fix options
/foundry:audit --upgrade            # fetch latest Claude Code docs, apply improvements with A/B testing
/foundry:audit --adversarial        # adversarial review with foundry:challenger + Codex
/foundry:audit --efficiency         # cost and efficiency sweep: model tiers, effort levels, unbounded spawn patterns, token bloat, boilerplate duplication; outputs P1–4 plan + code-block extraction candidates; extraction gate offers EXTRACT / EXTRACT+RECOMMENDED / skip

# Tier 1 — group scopes
/foundry:audit agents               # all agents
/foundry:audit skills               # all skills
/foundry:audit rules                # all rules
/foundry:audit communication        # communication governance files
/foundry:audit setup                # system config: settings.json, hooks, plugin integration
/foundry:audit plugin               # foundry plugin integration checks only
/foundry:audit plugins              # deep audit of all installed plugins

# Tier 2 — plugin name (shorthand for 'plugins <name>')
/foundry:audit oss                  # oss plugin agents + skills only
/foundry:audit foundry              # foundry plugin only (same as 'plugins foundry')
/foundry:audit oss research         # oss + research plugins

# Tier 3 — specific agent or skill name
/foundry:audit shepherd             # single agent
/foundry:audit curator challenger   # two agents
/foundry:audit review resolve       # two skills

# Combine scope + flags
/foundry:audit oss --adversarial           # oss plugin, adversarial review
/foundry:audit agents --adversarial        # all agents, adversarial review

# Preserve key context across session compaction
/foundry:audit --keep "task-123, .reports/audit/2026-07-03T10-00-00Z"
```

`fix` and `upgrade` mutually exclusive — never combine.

**Behaviour change (0.40.1)**: the dead-model-spec check and Check 21 (skill frontmatter conflicts) were removed, and `--efficiency`'s missing-model check no longer exempts skills declaring `disable-model-invocation: true`. All three rested on the premise that `disable-model-invocation: true` nullifies a co-declared `model:` or `context: fork`; it does not — the flag blocks Claude-automatic invocation, subagent preloading, and scheduled dispatch, while a user-typed `/skill-name` still runs and still honours `model:`. The removed checks would have recommended deleting a field that has real effect. Dropping the exemption widens missing-model detection from 14 to 35 skills, so expect more findings under `--efficiency` than before.

**Sweep checks (30 checks)**:

- Inventory drift: MEMORY.md roster vs files on disk
- Broken cross-references between agents and skills
- Hardcoded absolute user paths
- Model-tier appropriateness (reasoning vs execution roles)
- Agent description routing overlap (40%+ consecutive step overlap flagged)
- settings.json permissions vs Bash calls in skills
- Hook event names vs documented schema
- Claude Code docs freshness (spawns `foundry:web-explorer` for live docs)
- Plugin integration correctness (codex plugin, foundry plugin)
- File length, heading hierarchy, LLM context minimality
- Config token overhead: total always-loaded config >100 KB, single rules file >10 KB (rules/ loads at session start; agents/skills lazy-loaded)
- Codemap index guards: every file resolving a codemap index path must be MANIFEST-managed, declared in the guard registry with its shape, or call the provider CLI (which never spells the path); registry entries whose file no longer holds a guard fail too, so the inventory cannot rot

Outputs structured report. With fix level: delegates fixes to sub-agents (never edits inline), re-audits modified files to confirm fixes held. Convergence loop up to 5 passes.

______________________________________________________________________

### `/foundry:calibrate`

Benchmarks agents and skills against synthetic problems with defined ground truth. Primary signal = calibration bias — gap between self-reported confidence and actual recall. Well-calibrated agent reports 0.9 when finds ~90% of issues.

```text
/foundry:calibrate all --fast              # quick benchmark across all modes (3 problems each)
/foundry:calibrate all --full              # thorough benchmark (10 problems each)
/foundry:calibrate routing --fast          # routing accuracy only — run after any agent description change
/foundry:calibrate agents --full --ab-test # agents + general-purpose baseline comparison
/foundry:calibrate all --fast --apply      # benchmark then immediately apply improvement proposals
/foundry:calibrate --apply                 # apply proposals from the most recent past run
/foundry:calibrate foundry:sw-engineer --fast  # single agent (tier 3 by full name)

# Tier 2 — plugin name
/foundry:calibrate oss --fast              # all oss plugin agents + calibratable skills
/foundry:calibrate oss research --fast     # oss + research plugins

# Tier 3 — specific agent or skill (bare name or plugin-prefixed)
/foundry:calibrate curator --fast          # single agent by bare name
/foundry:calibrate curator shepherd        # two agents (default --fast)

# Multiple targets
/foundry:calibrate agents skills --fast    # agents + skills in one run

# Preserve key context across session compaction
/foundry:calibrate --keep "task-456, .reports/calibrate/2026-07-03T10-00-00Z"
```

**Large fan-out gate**: broad scopes (`all`, `agents`, `skills`, `plugins`, `<plugin-name>`) always confirm via `AskUserQuestion` before spawning — expand to dozens of agent/skill pipelines only inside Step 2, no exact count upfront. Narrow scopes (single agent/skill) confirm only when exact spawn count exceeds 50. `--apply` no longer bypasses gate alone; only `--skip-gate` does.

**Thresholds**:

- Routing accuracy: 90% (hard-problem accuracy: 80%)
- Recall per agent: 0.70 (below = instruction improvement needed)
- Calibration bias: within +/-0.15 (beyond = confidence decoupled from quality)

**Modes**:

- `agents` — all specialist agents
- `skills` — `/foundry:audit` and `/oss:review`
- `routing` — orchestrator dispatch accuracy for synthetic task prompts
- `communication` — team protocol compliance, file-handoff protocol violations
- `rules` — rule adherence across global + path-scoped rule files
- `plugins` / `<plugin-name>` — all agents + calibratable skills, one or all plugins
- `<agent-name>` / `<skill-name>` — single target, bare or plugin-prefixed name
- `all` — all above

Results saved to `.reports/calibrate/<timestamp>/<target>/`. Improvement proposals written to `proposal.md` per target dir, applied with `--apply`.

Agents and skills modes use dual-source evaluation: Claude + Codex generate problems and score responses independently, Claude 51% tiebreaker.

______________________________________________________________________

### `/foundry:manage`

Create, update, or delete agents, skills, and rules; update or delete hooks. Full cross-reference propagation. Keeps MEMORY.md, README, `settings.json` in sync automatically.

```text
/foundry:manage create agent security-auditor "Vulnerability scanning specialist for OWASP Top 10 and supply chain threats"
/foundry:manage create skill benchmark "Benchmark orchestrator for measuring performance across commits"
/foundry:manage create rule torch-patterns "PyTorch coding patterns — compile, AMP, distributed"

/foundry:manage update my-agent "add a section on error handling patterns"
/foundry:manage update my-agent new-agent-name        # rename
/foundry:manage update my-agent docs/spec.md          # apply spec file as change directive

/foundry:manage delete old-agent-name

/foundry:manage add perm "Bash(jq:*)" "Parse and filter JSON" "Extract fields from REST API responses"
/foundry:manage remove perm "Bash(jq:*)"
```

**Create**: fetches latest Claude Code agent/skill frontmatter schema, picks unused color, assigns model tier by role complexity, delegates content generation to `foundry:curator`. Checks overlap with existing agents first.

**Update**: auto-detects type from disk. Rename atomic (write-before-delete). Content edits delegated to `foundry:curator` (agents/skills) or `foundry:sw-engineer` (hooks). Propagates description changes to cross-references when more than 3 files affected.

**Delete**: removes file, cleans broken references across `.claude/`, updates MEMORY.md and README.

**Permissions**: `add perm` / `remove perm` update both `settings.json` and `permissions-guide.md` atomically — never one without other.

After any create or update: `/foundry:calibrate routing --fast` to confirm routing accuracy unaffected.

______________________________________________________________________

### `/foundry:brainstorm`

Turns fuzzy idea into approved exploration tree, then spec, then ordered action plan. Nothing implemented until user approves design.

```text
/foundry:brainstorm "add caching layer to the data pipeline"
/foundry:brainstorm "add caching layer to the data pipeline" --tight    # fewer questions and operations
/foundry:brainstorm "add caching layer to the data pipeline" --deep     # more exploration
/foundry:brainstorm "add caching layer to the data pipeline" --type workflow

/foundry:brainstorm breakdown .plans/blueprint/2026-04-01-caching-layer.md       # tree -> spec
/foundry:brainstorm breakdown .plans/blueprint/2026-04-01-caching-layer-spec.md  # spec -> action plan

# Preserve key context across session compaction
/foundry:brainstorm "add caching layer to the data pipeline" --keep "task-789, .plans/blueprint/2026-04-01-caching-layer.md"
```

**Idea mode** (default):

1. Scans codebase for relevant existing code + constraints
2. Asks up to 10 clarifying questions (5 with `--tight`, 15 with `--deep`) — the first call batches 2-3 orientation questions, the rest follow one at a time so each can adapt to the prior answer
3. Presents 3-5 initial branches: core idea, tension resolved, what it trades away
4. Interactive operations loop: deepen, reject, resolve, merge, add — up to 10 rounds
5. Saves tree to `.plans/blueprint/YYYY-MM-DD-<slug>.md` with `Status: tree`
6. Live tree viewer at URL printed during Step 1 (serve project root with `python -m http.server 8000`)

**Breakdown mode** (`breakdown <file>`):

- `Status: tree` file: distillation questions, then section-by-section spec, saved `Status: draft`
- `Status: draft` file: resolves blocking open questions, produces ordered action plan with tagged invocations

`--type` hint (`application`, `workflow`, `utility`, `config`, `research`) shapes question framing + codebase scan patterns in idea mode.

______________________________________________________________________

### `/foundry:investigate`

Systematic diagnosis for unknown failures. Gathers signals, ranks hypotheses, probes top candidates, reports confirmed root cause + recommended next action.

```text
/foundry:investigate "hooks not firing on Save"
/foundry:investigate "CI fails but passes locally"
/foundry:investigate "codex agent exits 127 on this machine"
/foundry:investigate "/calibrate times out every run"
/foundry:investigate "hooks not firing" --keep "task-999"  # preserve across compaction
```

**Covers**: broken local setup, environment mismatches, tool misconfigurations, hook misbehavior, CI vs local divergence, permission errors, runtime anomalies.

**Not for**: known Python test failures with traceback (use `/develop:debug`); `.claude/` config quality sweep (use `/foundry:audit`).

Workflow: parse symptom -> gather signals in parallel (tool versions, PATH, recent git changes, config state, logs) -> rank hypotheses -> optional Codex adversarial review for ambiguous cases -> probe top hypotheses -> report root cause + recommended next skill.

Output always includes: confirmed root cause (or narrowed suspects), key evidence, what was ruled out, single recommended next action.

**Auto-invokes when (MAYBE):** unknown failure, no Python traceback — hook not firing, CI passes locally but fails remotely, behavior inconsistent with config; "not working but config looks right", "hook not triggering", "why isn't X running".

______________________________________________________________________

### `/foundry:profile`

Buckets session **clock time** from `~/.claude/logs/{timings,invocations}.jsonl` into local-tool, agent-spawn, Skill, AskUserQuestion idle, main-loop reasoning residual, **and** session **tokens/cost** from Claude Code transcripts (`~/.claude/projects/**`, main-loop + subagent files) into main vs subagent spend by model tier — merged into one report. Pure log/transcript read — no instrumentation, no LLM calls, no skill edits.

```text
/foundry:profile                          # last 24h, top 5 slowest calls + cost ranking
/foundry:profile --since 7d               # last 7 days
/foundry:profile --session-id 9c1bded7    # drill one session: clock breakdown + cost/agent-roster/cache-rebuild detail
/foundry:profile --top-n 20               # 20 longest single calls
```

**Covers**: per-session clock breakdown (local% / agent% / skill% / reasoning% / idle), per-skill clock rollup (runs, total, mean, median, p90), top-N longest single calls, headline split over window; `## Tokens & cost` section — sessions ranked by cost (main $ / subagent $ / total $), per-command rollup, or (with `--session-id`) cost by main/sidechain × model tier, agent roster, top cache-rebuild calls, cold-start share. Subagent spend is read directly from each session's `subagents/agent-*.jsonl` transcripts, not inferred.

**Not for**: per-line Python perf (use `foundry:perf-optimizer`); known failure diagnosis (use `/foundry:investigate`); a real billing statement (prices are public list rates, not effective plan rates).

Reads JSONL logs foundry `task-log.js` hook already writes for the clock side (`bin/timing_analyzer.py`) and Claude Code transcripts for the cost side (`bin/cost_analyzer.py`) — answers "where did wall clock go in `/oss:resolve`?" and "what did this session cost?" alike. Background agents (`run_in_background=true`) with spawn-only false-zero `duration_ms` recovered by joining matching `started→completed` pair in `invocations.jsonl`. The cost side is best-effort: if no transcripts fall in the window, the `## Tokens & cost` section is omitted and the clock report still ships.

**Auto-invokes when (MAYBE):** user asks where wall-clock time or tokens/cost go, why a skill is slow or expensive, what dominates session runtime or spend; "where does time go", "why so slow", "what did this cost", "token spend", "profile last session", "clock breakdown", "session timing", "which skill burns tokens".

______________________________________________________________________

### `/foundry:distill`

Extracts patterns from work history + corrections, distills into durable improvements — new agent/skill suggestions, roster quality review, memory pruning, promoting lessons into rules, or analysing external plugins/agentic resources for adoption.

```text
/foundry:distill                                     # analyze project patterns, suggest new agents/skills
/foundry:distill review                              # review existing roster for quality and gaps (no new suggestions)
/foundry:distill prune                               # trim stale/redundant entries from all project MEMORY.md files
/foundry:distill prune --project                     # interactive picker: select which project(s) to prune
/foundry:distill memory                              # promote patterns from all projects into rules/agents/skills
/foundry:distill memory --project                    # interactive picker: select which project(s) to distill from
/foundry:distill "external https://..."              # analyse external plugin/skill/agent resource, produce adoption proposal
/foundry:distill "external ./path/to/plugin"         # same — local path or directory
/foundry:distill "I keep doing X manually"           # use description as context for suggestions
/foundry:distill --keep "task-111"                       # preserve key context across compaction
```

`--project` triggers interactive project picker — lists all slugs under `~/.claude/projects/*/memory/` with MEMORY.md size in tokens; select one or more. Without `--project`, both modes run across **all** projects automatically. Both `prune` and `memory` run parallel across selected projects (one agent per project), then consolidate into single confirmation step. `memory` mode also enriches each project's feedback with project-level context (CLAUDE.md, recent git log, active plans) for better classification accuracy.

**`lessons` mode** = primary post-correction consolidation path. Reads `.notes/lessons.md` and `feedback_*.md` memory files, clusters by domain, classifies each entry as `→ rule`, `→ agent update`, `→ skill update`, `→ already covered`, or `→ too narrow`, generates proposals. Before applying: conflict pre-check — greps each target file for section delta lands in, flags cross-proposal collisions with ⚠. Confirmed changes applied, followed by `git diff` gate — inspect or revert before committing.

**`external` mode** does fast + slow read of source (URL, file, directory), extracts mental model + standout implementation details, compares against live local setup, splits candidates into two groups: *Align + improve* (maps cleanly onto existing agents/skills/rules) and *Differentiated highlights* (novel, structurally different — interesting but larger work). Each candidate scored, assigned adoption lane: adopt-as-is / tweak / discuss / skip. When Group A thin or cumulative edit effort large, recommends installing source as standalone plugin with justification, not cherry-picking. Nothing written until confirmed.

After applying: run `/foundry:setup` to propagate new rule files to `~/.claude/`.

Run monthly or after any correction burst.

______________________________________________________________________

### `/foundry:session`

Session state that outlives a context reset — a handover document plus a parking lot for open loops. `dump` sweeps the live conversation and composes goal, decisions + why, lessons, standing instructions, a files-touched table, outstanding items and an artifacts table, writes it to `.claude/state/session/<slug>.md`, and ends by printing `/clear` as the last line of the reply. The `session-restore.js` hook injects that document back on the other side, so restore costs nothing.

```text
/foundry:session dump [name]     # sweep, write the handover doc, then print /clear
/foundry:session recall [name]   # print a stored handover back into context (named, or latest)
/foundry:session list            # handovers (slug / age / consumed) + open parked items
/foundry:session park <idea>     # stash one open loop without derailing the current task
/foundry:session sweep           # audit the conversation for unlanded ideas and questions
/foundry:session drop <item>     # close a parked item and log the closure
```

**Auto-invokes when:** user says "dump the session", "handover before clear", "save state before clearing", "carry this over", "park this for later", "what did we defer", "anything unfinished before I close".

Runs **inline, not forked** — the conversation history is the authoritative source for what changed, what was decided, and what never landed. Implementation detail (diffs, tool output, exploration transcript, abandoned approaches) is dropped on purpose.

The hook auto-restores only an **unconsumed** handover **under 30 minutes old**, so an old dump never ambushes an unrelated session; anything outside that window is reachable via `recall`, which has no age gate. Over ~8000 chars the hook injects `## Goal` + files table + `## Next step` + a pointer instead of the whole doc. Handover docs older than 14 days list as `⚠ stale`; older than 30 days are swept during `list`.

Parked items live in `.claude/state/session/PARKED.md`, are copied into every dump rather than consumed by it, and have **no TTL** — only `drop` removes one, appending an audit line to `dropped.jsonl`. Parking is an explicit command, not an automatic behaviour.

The mode is `recall`, not `resume`, to stay clear of Claude Code's native `/resume` — which revives a whole past conversation and is the better tool whenever it applies.

**Not** auto-compact survival (the skill contract in `.temp/state/skill-contract.md`, per `rules/compaction.md`) — the two are distinguished in a boundary table in the skill.

______________________________________________________________________

### `/foundry:create`

Interactive outline co-creation for developer advocacy content. Collects format, audience profile, four-beat arc, voice/tone via structured questions; detects out-of-scope requests; surfaces editorial conflicts; writes approved outline for `foundry:creator` to execute.

```text
/foundry:create "tracing Python microservices with OpenTelemetry"
/foundry:create "why your CI pipeline is lying to you"
/foundry:create                   # no topic — skill asks interactively
```

**Supported formats**: blog post, Marp slide deck (conference/meetup talk), social thread (Twitter/LinkedIn), talk abstract (CFP submission), lightning talk (5–10 min).

**Out-of-scope detection**: refuses FAQs, comparison tables, reference docs at Step 1, redirects to `foundry:doc-scribe`.

**Editorial conflict detection**: brief implies expert-level topic for beginner audience (or reverse) — skill surfaces mismatch explicitly before writing.

Writes `.plans/content/<slug>-outline.md`. Hand off to `foundry:creator` after approval:

```text
@foundry:creator
```

Max 5 `AskUserQuestion` interactions for well-specified brief (format, audience, arc, voice). Skips interactive steps if all choices in initial brief.

______________________________________________________________________

### `/foundry:humanizer`

Strips AI-writing tells from human-facing prose — docs, PR/commit bodies, reports, release notes, blog posts. Removes LLM-vocabulary clichés, banned constructions (rule-of-three triads, "not just X but Y"), formatting tells (title-case headings, em-dash overuse). Final pass — preserves meaning + structure; no rewrite from scratch.

```text
/foundry:humanizer <text or file path>    # humanize in place
/foundry:humanizer check <file>           # report tells without editing
```

Primarily model-initiated self-review pass: foundry may invoke before finalizing substantial prose artifact drafted during task. Best-effort, not guaranteed intercept. Skips code, config, machine-parsed envelopes, inter-agent handover files.

______________________________________________________________________

## 🤖 Agents reference

All ten agents available by full plugin-prefixed name. In spawn directives and `subagent_type` values, always use full prefix (`foundry:sw-engineer`, not `sw-engineer`).

### foundry:sw-engineer

**Role**: senior software engineer — writing/refactoring Python.

**Use for**: implementing features, fixing bugs, TDD/test-first, SOLID, type safety, production-quality Python for OSS libraries.

**Model**: `opus`

**Not for**: docstrings (`foundry:doc-scribe`), ruff/mypy config (`foundry:linting-expert`), system design (`foundry:solution-architect`), test quality analysis (`foundry:qa-specialist`), perf profiling (`foundry:perf-optimizer`), ML paper implementations (`research:scientist`), `.claude/` config edits (`foundry:curator`), CI/CD pipeline config — GitHub Actions, pre-commit hooks, CI YAML (`oss:cicd-steward` — requires `oss` plugin).

**Auto-invokes when:** user asks to implement, build, write, modify, fix code across 3+ files; phrases: "implement", "build", "write the code for", "add feature", "fix this bug". Runs in isolated worktree.

Runs in isolated worktree by default — changes sandboxed until review.

______________________________________________________________________

### foundry:solution-architect

**Role**: system design specialist — ADRs, API surface design, interface specs, migration plans, coupling analysis.

**Use for**: architectural trade-offs, public API contracts, deprecation strategies, assessing architectural feasibility of AI-generated hypotheses against codebase constraints.

**Model**: `opusplan`

**Not for**: implementation code (`foundry:sw-engineer`), release management (`oss:shepherd`), perf profiling or DataLoader throughput tuning (`foundry:perf-optimizer`).

**Auto-invokes when (MAYBE):** architecture questions involving 3+ components; "how should I structure this", "ADR for", "migration plan for". Not for simple design questions.

Produces documentation — ADRs, interface contracts, migration plans, component diagrams — not production code. Hands off to `foundry:sw-engineer` for execution. Spec with multiple unresolved decision branches: resolves one at a time via `AskUserQuestion` with recommended answer each, not one bulk ask.

______________________________________________________________________

### foundry:qa-specialist

**Role**: QA specialist — writing, reviewing, fixing tests. Rigorous black-box end-user tester: public API surface by default, expectations from docs/type hints — not implementation, tests represent realistic user workflows.

**Use for**: new pytest tests, public-API coverage gaps, edge-case matrices, fixing failing tests, integration test design. Auto-embeds OWASP Top 10 security review when task scope includes auth, payment flows, or PII — all modes.

**Model**: `sonnet`

**Not for**: linting, type checking, annotation fixes (`foundry:linting-expert`), production implementation (`foundry:sw-engineer`), slow test suite profiling (`foundry:perf-optimizer`), testing private/internal methods or mocking internals **by default** — opt-in only when the caller explicitly asks, or when a bug is unreachable through any public path.

**Auto-invokes when:** user asks to write tests, assess coverage, define test strategy; "write tests for", "add unit tests", "test coverage for".

Writes deterministic, parametrized, behavior-focused tests. Progression: happy path → edge cases → error cases → boundary values → adversarial inputs. Applies public-API coverage checklist before done.

______________________________________________________________________

### foundry:linting-expert

**Role**: static analysis + tooling specialist for Python.

**Use for**: ruff rules, mypy strictness, pre-commit hooks, fixing lint/type violations, missing type annotations, lint/type content of quality gates. Final code sanitization before handover.

**Model**: `sonnet` (effort: medium — haiku doesn't accept an `effort` key)

**Not for**: CI pipeline structure or runner strategy (`oss:cicd-steward`), test logic (`foundry:qa-specialist`), implementation beyond annotation/style (`foundry:sw-engineer`), docstrings or API reference (`foundry:doc-scribe`).

**Auto-invokes when:** after code edits when user asks "is this clean", "any lint issues", "check formatting", "check types"; lint or type errors visible in output.

Always downstream of `foundry:sw-engineer` — never lints unimplemented code.

______________________________________________________________________

### foundry:perf-optimizer

**Role**: performance engineer — profiling + optimizing CPU, GPU, memory, I/O bottlenecks.

**Use for**: profiling Python/ML workloads, DataLoader bottlenecks, mixed precision, vectorizing loops, PyTorch throughput tuning.

**Model**: `opus`

**Not for**: general refactoring (`foundry:sw-engineer`), architectural redesign (`foundry:solution-architect`), DataLoader correctness or reproducibility audits (`research:data-steward` — requires `research` plugin), doc writing (`foundry:doc-scribe`), annotation fixes (`foundry:linting-expert`).

**Auto-invokes when:** user asks to profile, benchmark, optimize Python/ML workload; mentions slow training, GPU underutilization, DataLoader bottleneck; phrases: "why is this slow", "profile this", "optimize training speed". Not for general code changes (`foundry:sw-engineer`).

Strictly profile-first: measure before change, change one thing, measure again. Optimization order: algorithm -> data structure -> I/O -> memory -> concurrency -> vectorization -> compute -> caching. Never GPU tuning before checking I/O.

______________________________________________________________________

### foundry:doc-scribe

**Role**: documentation specialist — docstrings, API references, README files.

**Use for**: auditing missing docstrings, writing Google-style (Napoleon) docstrings from code, README content, doc/code inconsistencies.

**Model**: `sonnet`

**Not for**: CHANGELOG entries or release notes (`oss:shepherd` for lifecycle/format decisions, `/oss:release` for automated generation), linting code examples (`foundry:linting-expert`), implementation (`foundry:sw-engineer`), outward-facing narrative artifacts — blog posts, talk slides, social threads (`foundry:creator`).

**Auto-invokes when:** user asks for documentation; "write docs for", "add docstrings to", "update the README", "document this function".

Always downstream — documents finalized code, never shapes design. After `foundry:doc-scribe` produces content, follow with `foundry:linting-expert` to sanitize code examples.

______________________________________________________________________

### foundry:web-explorer

**Role**: web fetch + content extraction specialist.

**Use for**: fetching live library docs, API references, changelogs, migration guides, package version lookups, GitHub release extraction. Used internally by `/foundry:audit upgrade` and `/foundry:manage create`.

**Model**: `sonnet`

**Not for**: code analysis or implementation (`foundry:sw-engineer`), ML paper analysis (`research:scientist`), README/docstring writing (`foundry:doc-scribe`), dependency upgrade lifecycle decisions (`oss:shepherd`), perf profiling or benchmarking recommendations (`foundry:perf-optimizer`).

**Auto-invokes when:** user asks about library docs, external API, URL content; "what does X docs say", "look up", "find the docs for", "latest version of"; user pastes URL + asks question about it.

Feeds `research:scientist` — fetches current docs + papers; scientist interprets.

______________________________________________________________________

### foundry:curator

**Role**: quality guardian of Claude config markdown files — agents, skills, rules.

**Use for**: auditing `.claude/` config files — verbosity creep, cross-agent duplication, broken cross-references, structural violations, outdated content, roster overlap. Used internally by `/foundry:audit` and `/foundry:manage`.

**Model**: `opusplan`

**Not for**: hook files (`*.js`) — belong to `foundry:sw-engineer`. Not for creating/scaffolding new agents or skills (`/foundry:manage create`). Not for routing new tasks to other agents.

**Auto-invokes when (MAYBE):** user explicitly references specific agent/skill config file by path; "audit this agent", "check this skill file", "is this config valid". Narrow trigger — see description.

Generally not invoked directly. `/foundry:audit` spawns it in batches across all config files; `/foundry:manage` delegates content generation + editing to it.

______________________________________________________________________

### foundry:challenger

**Role**: adversarial reviewer — implementation plans, architecture proposals, significant code reviews.

**Use for**: red-teaming plan before committing, challenging architectural decisions before ship, adversarial code review on security-sensitive or irreversible ops. Every claim unproven until backed by evidence. Attacks across 6 dimensions (Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep, Root Cause) — drills to bedrock per standing challenge (keeps asking "why?" until root cause, not surface symptom). Mandatory refutation step keeps it objective: accepts refutation when evidence warrants.

When `bridge@borda-ai-rig` is installed and enabled, challenger calls the read-only Codex adversarial review track for the same target and aggregates its findings. Pass `--no-codex` to skip. An absent or disabled bridge is surfaced rather than silently falling back.

**Model**: `opus`

**Not for**: designing plans or ADRs (`foundry:solution-architect`), writing tests or coverage review (`foundry:qa-specialist`), config file quality review (`foundry:curator`).

**Auto-invokes when:** user asks to challenge, stress-test, critique design or plan; "challenge this", "what are the weaknesses", "devil's advocate", "poke holes in", "second opinion on".

Read-only — never writes or edits files. Runs by default in all `/develop:*` skills and `/oss:review` — skip with `--no-challenge`.

______________________________________________________________________

### foundry:creator

**Role**: developer advocacy content specialist — outward-facing narrative artifacts.

**Use for**: complete blog posts, Marp slide decks, social threads, talk abstracts, lightning talk outlines in one autonomous pass. Imagines ideal reader experience first, works backwards to structure + form — questions status-quo conventions, pushes for fresh angles. Reads approved outline file (`.plans/content/<slug>-outline.md`) from `/foundry:create`. Applies four-beat story arc (Problem→Journey→Insight→Action) calibrated to target audience level.

**Model**: `sonnet`

**Not for**: in-code documentation, docstrings, API references (`foundry:doc-scribe`), release notes/changelogs (`oss:shepherd` — requires `oss` plugin), structured reference content — FAQs, comparison tables (redirect to `foundry:doc-scribe`).

**Auto-invokes when:** outline file `.plans/content/<slug>-outline.md` approved; user asks for blog post, Marp slide deck, social thread, talk abstract, lightning talk outline. Skip when: outline file not found (run `/foundry:create` first); code documentation (`foundry:doc-scribe`); release notes (`oss:shepherd`).

Always downstream of `/foundry:create` — reads approved outline, generates full artifact. Two-phase system: `/foundry:create` (interactive intake → outline) then `foundry:creator` (autonomous generation → artifact).

______________________________________________________________________

## 🔗 Agent relationships

Agents = directed pipeline, not flat pool:

- `foundry:linting-expert` always **downstream** of `foundry:sw-engineer` — never lints unimplemented code
- `foundry:doc-scribe` always **downstream** — documents finalized code, never shapes design
- `foundry:qa-specialist` runs **parallel** to `foundry:sw-engineer` during review, or downstream after implementation
- `foundry:challenger` is **pre-implementation** — challenges plans + proposals before any code; use before `foundry:sw-engineer`
- `foundry:curator` is **orthogonal** — audits `.claude/` config files, not user code
- `foundry:web-explorer` **feeds** `research:scientist` — fetches current docs + papers; scientist interprets
- `foundry:creator` always **downstream** of `/foundry:create` — reads approved outline file; never generates without prior outline

**Model tiering**: reasoning agents (`foundry:sw-engineer`, `foundry:perf-optimizer`) use `opus`; adversarial reasoning (`foundry:challenger`) uses `opus`; plan-gated roles (`foundry:solution-architect`, `foundry:curator`) use `opusplan`; execution agents (`foundry:doc-scribe`, `foundry:web-explorer`, `foundry:creator`, `foundry:qa-specialist`, `foundry:linting-expert`) use `sonnet`. Aliases resolve to the 5 family (`opus`→Opus 5, `sonnet`→Sonnet 5, `opusplan`→Opus in plan mode, Sonnet in execution); `fable` is the reserve tier for demanding long-horizon reasoning, with no standing agent assignment. Within the 5 family the tiers sit closer together than the 4.x tiers did, so **effort is the primary cost knob and tier the blunt one** — tune effort before moving an agent's tier, and prove any downgrade with `/foundry:calibrate <agent> --full --ab-test` rather than a generation-level parity claim. `/foundry:audit`'s fix dispatch (`audit/modes/fix.md`, `audit/templates/audit-fix-prompt.md`) overrides per-call to `haiku` for `PARALLEL_SAFE_CATEGORIES` findings (pure transcription of an already-known replacement) regardless of the spawned agent's own frontmatter tier — judgment-bearing and cross-file fixes keep the agent's default.

______________________________________________________________________

## 📋 Rules installed

`/foundry:setup` symlinks all 14 rule files from `plugins/cc_foundry/rules/` into `~/.claude/rules/`, each renamed to `foundry-<source-name>.md`. They govern Claude behavior globally, in all sessions after install.

`~/.claude/rules/` is one flat directory shared by every installed plugin, and four of this marketplace's plugins ship a `rules/quality-gates.md`, so source basenames would silently overwrite one another. Each plugin therefore namespaces its own rules with its plugin name: `quality-gates.md` installs as `foundry-quality-gates.md`, alongside `develop-quality-gates.md`, `oss-quality-gates.md`, and `research-quality-gates.md` from the sibling plugins' own setup skills. The prefix is inert — verified against Claude Code 2.1.220 that it changes neither unconditional rule loading nor `paths:` frontmatter matching. Re-running setup after an upgrade migrates any pre-namespace unprefixed link, but only when that link provably belongs to foundry; a link into another marketplace, a source checkout, or a dotfiles tree is preserved and reported as a conflict instead.

> **Stub + on-demand split (token diet):** `git-commit.md`, `debugging.md`, `external-data.md`, `artifact-lifecycle.md`, `quality-gates.md`, `communication.md`, `claude-config.md`, `public-github.md`, `task-lifecycle.md` = thin always-loaded stubs with hard constraints only; full procedural bodies live in `rules/_full/` (resolved from plugin cache, NOT symlinked or injected), Read on demand at the trigger point named in each stub — drafting a commit, multi-file fix, multi-page fetch, defining new output dirs, writing a report, building an `AskUserQuestion` call, setting a Bash timeout, a GitHub write, a subagent spawn. Zero constraint loss: every named rule survives in the stub or its `_full/` body, verified by heading-set diff.
>
> **Measured (2026-08-07)**: the always-injected preamble is ~20.5K tokens per agent and is re-sent every turn — a 56-turn agent re-sends ~1.15M tokens of it. The nine splits above cut ~2.5K of that, plus ~1.3K from `plugins/CLAUDE.md` → `plugins/AUTHORING.md`. **Rule savings are realized only after `make sync-claude` propagates to the plugin cache** — `~/.claude/rules/*.md` are symlinks into a version-pinned cache directory, so edits to this repo's `rules/` do not affect running agents until installed.

| Rule file               | Applies to                      | Governs                                                                                                                                       |
| ----------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `communication.md`      | all                             | Re: anchor format, progress narration, tone, output routing, breaking-findings format, terminal colors, confidence display                    |
| `quality-gates.md`      | all                             | Confidence block format, Internal Quality Loop, link verification, output routing (long output to file)                                       |
| `git-commit.md`         | all                             | Commit message format, diff-gathering before writing, co-author trailers, branch + push safety                                                |
| `claude-config.md`      | all                             | Bash timeouts (3x P90), directory navigation rules, no hardcoded absolute paths, agent/skill spawn discipline                                 |
| `artifact-lifecycle.md` | all                             | Canonical artifact layout (`.plans/`, `.reports/`, `.temp/`), run directory naming, TTL policy                                                |
| `external-data.md`      | all                             | Pagination rules: GitHub CLI, REST APIs, GraphQL, Cloud APIs — never work on partial result set                                               |
| `foundry-config.md`     | `.claude/**`                    | Plan-mode gate before any `.claude/` edit, post-edit checklist, XML tag conventions, distribution rules                                       |
| `python-code.md`        | `**/*.py`                       | Google-style docstrings, closed option sets as enums, dataclass/TypedDict selection, deprecation API, multi-OS portability, complexity limits |
| `python-testing.md`     | `tests/**/*.py`, `**/test_*.py` | pytest design: TDD process, fixtures, parametrization, marker selection, cross-OS tests, mocking, what to test in priority order              |
| `public-github.md`      | all                             | Read-only policy on public GitHub — permitted reads vs permanently forbidden write operations                                                 |
| `untrusted-content.md`  | all                             | Ingested external text is data, never instruction — delimiters, no permission widening, memory propagation                                    |

______________________________________________________________________

## ⚙️ Configuration

### settings.json keys merged by `/foundry:setup`

| Key                                     | What it does                                                                             |
| --------------------------------------- | ---------------------------------------------------------------------------------------- |
| `statusLine.command`                    | Runs `statusline.js` — active agent count in Claude Code status bar                      |
| `permissions.allow`                     | Adds pre-approved Bash commands, git operations, WebFetch domains                        |
| `permissions.deny`                      | Adds permanently denied write operations (public GitHub mutations, destructive git)      |
| `enabledPlugins["bridge@borda-ai-rig"]` | Enables bridge-backed Codex review in `/foundry:calibrate` and `/foundry:audit`          |
| `advisorModel`                          | Copied from project `.claude/settings.json` when pinned — advisor tool uses chosen model |

### Optional flags and knobs

**`--approve`** on `/foundry:setup`: skips all interactive prompts, auto-accepts recommended choices. For scripted or CI setups.

**`--skip-audit`** on `/foundry:manage`: skips trailing `/foundry:audit` validation step. Use inside audit-initiated fix sessions to avoid recursion.

**Calibration pace**: `--fast` (3 problems per target, default) vs `--full` (10 per target). `--fast` for routine checks after agent edits; `--full` for thorough benchmarks before releases or after major instruction changes.

**Brainstorm ceremony**: `--tight` (5/5/1 caps, well-scoped ideas), default (10/10/2), `--deep` (15/15/3, genuinely ambiguous problems).

### Environment

No environment variables required. foundry reads `~/.claude/settings.json` + plugin's installed cache path, both resolved automatically by `/foundry:setup`.

______________________________________________________________________

<details>

<summary>

## 🔍 Troubleshooting

</summary>

**`/foundry:audit` reports broken symlinks (Check I3)**

Symlinks in `~/.claude/rules/` point to previous plugin cache path after upgrade. Re-run `/foundry:setup` — Step 10 detects stale symlinks as conflicts, offers replacement.

**Hooks not firing**

Run `/foundry:investigate "hooks not firing on Save"`. Most common cause: `hooks` block still in `~/.claude/settings.json` from pre-plugin-migration install (hooks now register via plugin manifest, not `hooks` key). `/foundry:setup` Step 3 detects + removes stale block.

**`/foundry:calibrate` times out**

Each pipeline subagent has 10-minute hard cutoff (15 minutes when Codex active). Target consistently times out: run in isolation: `/foundry:calibrate foundry:sw-engineer --fast`. Persistent: `/foundry:investigate "/calibrate times out every run"`.

**`/foundry:manage create` picks wrong model tier**

Model tier chosen by role complexity at creation: `opusplan` for plan-gated quality review (e.g. `foundry:curator`, `foundry:solution-architect`), `opus` for complex reasoning (e.g. `foundry:sw-engineer`), `sonnet` for focused execution, `haiku` for high-frequency diagnostics. Fix after creation: `/foundry:manage update <name> "change model to sonnet"`.

**`foundry:curator` returns low confidence during `/foundry:audit`**

Audit re-runs agent with specific gap named in `Gaps:` field. Confidence still below 0.7 after one retry: gap surfaced with warning in final report for manual review. Recurring gaps: add to `foundry:curator` antipatterns section: `/foundry:manage update foundry-curator "add gap X to antipatterns-to-flag"`.

**`jq` not found warning during `/foundry:audit setup`**

Check 4 (permissions-guide drift) requires `jq`. Install: `brew install jq` on macOS, `apt install jq` on Linux. Audit continues without — only Check 4 skipped.

**`/foundry:audit`'s fix-level question is denied**

`enforce-audit-header.js` denied the follow-up gate because `.reports/audit/<timestamp>/summary.jsonl` does not exist — the audit reached agent launch but never consolidated its findings, so the severity counts in the question have no source. Finish Step 5 (spawn the `foundry:curator` consolidator, let it write `aggregate.md` and `summary.jsonl`); the question then goes through. Only the follow-up gate is affected — `! BREAKING` acknowledgments and the unsupported-flag prompt fire before Step 5 by design and are never blocked. The gate deactivates four hours after a run starts, so an aborted audit never blocks later questions permanently. Once the aggregate exists, the same hook additionally checks (via `report-header-table.js`) whether the reply actually rendered the report header as a table — a missing table never blocks the question, but rides along as an `additionalContext` reminder naming Step 11b.

**`/foundry:profile`'s drill-down question is denied**

`enforce-profile-header.js` denied the Step 5 follow-up gate because `.reports/profile/<timestamp>/report.md` does not exist, so Step 4 had no header to print and the question would rest on an ad-hoc summary. Re-run Step 2's `timing_analyzer.py`, print the report's `---` header block plus the `→ <path>` line, then ask. If the analyzer exited 1 because no session falls in the window, that path authorises no question at all — report it and stop, or re-invoke with a wider `--since`. The gate deactivates two hours after a run starts, so an aborted profile never blocks later questions permanently. Same table-format nudge as `enforce-audit-header.js` once the report exists.

______________________________________________________________________

</details>

<details>

<summary>

## 🏗️ Plugin structure

</summary>

```text
plugins/cc_foundry/
├── .claude-plugin/
│   ├── plugin.json              version + metadata
│   ├── permissions-allow.json   allow-list merged by /foundry:setup
│   └── permissions-deny.json    deny-list merged by /foundry:setup
├── agents/                      10 specialist agent files (flat — a nested `agents/<x>/y.md` would register as a dispatchable `foundry:<x>:y` agent)
├── references/                  agent sidecar fragments (`references/<agent>/*.md`), `cat`-loaded on demand; deliberately outside `agents/` so they are never scanned as agents
├── skills/                      11 skill directories (audit, brainstorm, calibrate, create, distill, humanizer, investigate, manage, profile, session, setup)
├── rules/                       14 rule files symlinked to ~/.claude/rules/foundry-*.md by /foundry:setup (+ 10 on-demand bodies in rules/_full/)
├── CLAUDE.src.md                workflow rules; /foundry:setup Step 10 copies → ~/.claude/CLAUDE.md
├── TEAM_PROTOCOL.md             AgentSpeak v2 inter-agent protocol
├── permissions-guide.md         annotated allow/deny reference (copied to .claude/ by /foundry:setup)
└── hooks/
    ├── hooks.json               hook registrations (${CLAUDE_PLUGIN_ROOT} paths)
    ├── task-log.js              SubagentStart/Stop tracking to /tmp/claude-state-<session>/
    ├── statusline.js            status bar agent counts
    ├── teammate-quality.js      TaskCompleted/TeammateIdle teammate output quality gate
    ├── lint-on-save.js          runs pre-commit after every Write/Edit; async + cross-session lock; 15s timeout; skips .temp/
    ├── artifact-guard.js        PostToolUse Write/Edit on .reports/**/*.md, .temp/**/*.md; soft 10K-token cap + .temp/ ultra-caveman article-density heuristic; feedback only, never blocks
    ├── rtk-rewrite.js           transparently rewrites CLI calls for token compression
    ├── agent-router.js          PreToolUse Agent hook; 3-tier routing fallback (worktree → cache → local)
    ├── commit-guard.js          PreToolUse Bash guard; git commit is prompt-discipline only, not hook-gated; git push --force blocked unconditionally on any branch, regular push gated by a sentinel requiring AskUserQuestion each time (no auto-arm)
    ├── allow-dispatch.js        PreToolUse Bash; the only registered auto-allow hook — calls the two decision modules below as libraries in rank order (provenance, then shape), writes the first allow's payload, and appends one audit record naming the effective verdict plus both modules' own verdicts; a module that throws contributes "none" and never suppresses the other; exits 0 on every path, because an allow-only hook that crashes grants nothing and falls back to normal permission handling; propagated to every plugin except codemap-py
    ├── sentinel-read-allow.js   decision module (shape lane), no longer registered as a hook of its own; auto-allows blueprint idioms ($(cat "${TMPDIR:-/tmp}/…") sentinel reads, $(date -u +FMT) stamps, substitution-free `IFS= read -r VAR < sentinel` form) when every segment is read-only — kills "Contains expansion" prompts for pre-canned skill code; whole-line `#` comments are skipped in segment validation (a separator after one still validates as live, and a quoted `#` stays data), and `..` rejects only as a real path component so `...`/`v1.2..v1.3` are not read as traversal; verdicts are invariant under runtime value substitution, which is what this module covers that `blueprint-allow.js` structurally cannot; keeps its standalone entry point, which `bin/audit_hook_coverage.py` still subprocesses; everything else falls through to normal permission checks; propagated to every plugin except codemap-py
    ├── blueprint-allow.js       decision module (provenance lane, rank 1), no longer registered as a hook of its own; normalizes the command, hashes it, and exact-matches it against the plugin's committed `blueprint-manifest.json` (generated by `bin/build_blueprint_manifest.py` from every verbatim bash block/command shipped in skills/agents/rules); a hit plus an independent danger re-check allows without a prompt, covering `$(python …)`/`$(git …)` capture-substitutions the shape-based module cannot reach; any deviation from the blueprint text misses and falls through to a normal prompt; propagated to every plugin except codemap-py
    ├── audit-close.js           PostToolUse/PostToolUseFailure (Bash) + SessionStart/SessionEnd; appends one record observing that a Bash call completed, and one per session boundary; writes nothing to stdout on any event (SessionStart stdout is injected into the model's context); every installed plugin writes its own row with nothing coordinating them, so four rows for one completion are corroboration rather than duplicates; propagated to every plugin except codemap-py
    ├── lib/audit-log.js         shared record writer for the two hooks above: canonical serialization (a cross-language contract with `bin/verify_blueprint_audit.py`), self-hash, and a single append into `~/.claude/logs/audit/s-<key>.jsonl`; append-only by design — it holds no lock, marker or state directory and has no code path that deletes or rewrites a file; never throws, so a logging failure cannot change a permission decision; `RIG_AUDIT=0` disables writing only; propagated to every plugin except codemap-py
    ├── session-restore.js       SessionStart matcher `clear`; reads `<cwd>/.claude/state/session/LATEST` from the hook payload's own `cwd`, and injects that handover doc as raw stdout (the documented SessionStart context channel) when it is unconsumed and under 30 min old — then marks it consumed and unlinks the pointer so a second `/clear` never re-injects; over ~8000 chars only `## Goal` + files table + `## Next step` + a `/foundry:session recall` pointer go in; every other path is a silent exit 0, so it can never block a session start
    ├── write-guard.js           PreToolUse Edit/Write/NotebookEdit; guard-only inverse of the two allow hooks above — grants nothing, only forces a confirmation (`permissionDecision: "ask"`) on writes to a small repository-independent protected set: `.github/**`, `CLAUDE.md`/`AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml`, `CHANGELOG.md`, dependency lockfiles, `pyproject.toml`; source and tests are deliberately unprotected, so pairing with `acceptEdits` removes routine friction while these stay gated; patterns are anchored on a path-separator boundary (`src/my_changelog_helper.py` passes through), matched after POSIX normalization so Windows backslashes cannot disable it, and case-insensitive because macOS/Windows filesystems case-fold (`changelog.md` writes the real `CHANGELOG.md`); everything else is silent passthrough; propagated to every plugin except codemap-py
    ├── md-compress.js           normalizes markdown whitespace/table-padding in place on Edit
    ├── batch-nudge.js           PreToolUse/PostToolUse/UserPromptSubmit; tracks a streak of sequential batchable calls (Read/Grep/Glob, read-only Bash prefixes) separated by model-round-trip-sized gaps (≥1.5s); at streak 4, nudges via PostToolUse exit 2 (stderr feedback, never blocks — PreToolUse always exits 0)
    ├── enforce-audit-header.js  PreToolUse AskUserQuestion; denies `/foundry:audit`'s follow-up gate until Step 5 has written `$RUN_DIR/summary.jsonl`, so the fix-level question is never asked from an ad-hoc summary; recognises the gate by its fixed option labels, so `! BREAKING` acknowledgments and flag prompts pass through; silent unless an audit run is in flight; once the aggregate exists, additionally nudges (never blocks) via `additionalContext` if the reply never rendered the report header as a table — see `report-header-table.js`
    ├── enforce-profile-header.js PreToolUse AskUserQuestion; denies `/foundry:profile`'s Step 5 follow-up gate until Step 2 has written `$REPORT_DIR/report.md`, so the drill-down question is never asked without a report behind it; reads `REPORT_DIR` line-wise out of the Step 1 `KEY=VALUE` state file (never executes it); silent unless a profile run is in flight; same table-format nudge as above once the report exists
    └── report-header-table.js  shared by both hooks above plus `enforce-review-header.js`/`enforce-analyse-header.js` (cc_oss), `enforce-review-header.js` (cc_develop), `enforce-topic-header.js` (cc_research, byte-identical copies via `propagate_shared.py`): reads the session transcript to check whether the assistant's own reply, since the last human turn, rendered the report's `---` header as a `| Field | Value |` table (or the documented `·`-fallback line) — catches the PR #1303 incident (raw YAML fields printed instead of a table) that the file-existence gate alone could not
```

______________________________________________________________________

</details>

<a id="bin-helper-inventory"></a>

<details>

<summary><strong>🧰 Bin helper inventory (37 shipped deterministic helpers)</strong></summary>

These Python helpers are installed workflow support and maintainer surfaces, not additional slash-command skills. They resolve paths, validate plugin documents, prepare reports, or perform bounded deterministic transforms; invoke them through the owning skill or with the installed plugin root.

`propagate_shared.py` and `check_orphaned_bin.py` use ASCII status labels so success, no-op propagation, and orphan warnings work with legacy console encodings such as Windows cp1252.

#### Audit, consistency, and document checks

| Helper                         | Purpose                                                                       |
| ------------------------------ | ----------------------------------------------------------------------------- |
| `audit_churn.py`               | Emit git-history recurrence signals for `/foundry:audit`.                     |
| `audit_static.py`              | Run and aggregate the deterministic Layer-1 audit checks.                     |
| `check_bash_persistence.py`    | Detect shell variables referenced across separate Bash tool calls.            |
| `check_cli_flag_drift.py`      | Detect drift between documented flags and `argparse` options.                 |
| `check_codemap_guard.py`       | Detect unmanaged codemap index-guard copies.                                  |
| `check_bridge.py`              | Detect whether the bridge plugin is installed and enabled.                    |
| `check_fence_symmetry.py`      | Validate Markdown code-fence pairing and nesting.                             |
| `check_gitignored_refs.py`     | Reject tracked-file references to this repo's gitignored documents.           |
| `check_mode_dispatch.py`       | Detect dangling mode-dispatch references in skill files.                      |
| `check_orphaned_bin.py`        | Find bin scripts not referenced by plugin Markdown.                           |
| `check_plugin_module_docs.py`  | Require a module docstring on every shipped plugin module.                    |
| `check_plugin_version_sync.py` | Require matching versions in `.claude-plugin`/`.codex-plugin` manifest pairs. |
| `check_readme_drift.py`        | Detect README version and bin-reference drift from disk.                      |
| `check_routing_links.py`       | Validate computed paths in skills and agent files.                            |
| `check_spawn_prompt_vars.py`   | Detect unresolved variables in Markdown spawn prompts.                        |
| `check_tag_symmetry.py`        | Check XML-tag symmetry in agent and skill Markdown.                           |
| `classify_resolver_sites.py`   | Classify resolver call sites as inline-required or cat-only.                  |

#### Session, install, and state helpers

| Helper                         | Purpose                                                       |
| ------------------------------ | ------------------------------------------------------------- |
| `find-polluter.py`             | Binary-search a test suite for test-isolation contamination.  |
| `get_plugin_install_path.py`   | Resolve the active plugin path from Claude's registry.        |
| `heal_git_artifacts.py`        | Reclaim stale skill locks and orphaned git worktrees.         |
| `health_sentinel.py`           | Create and inspect health-monitoring sentinels.               |
| `jq_write.py`                  | Apply an atomic `jq` JSON edit through temp-file replacement. |
| `load_mode.py`                 | Resolve a skill mode/template directory and emit one file.    |
| `load_shared_doc.py`           | Resolve the plugin shared-doc directory and emit one file.    |
| `make_run_dir.py`              | Create a UTC-timestamped run directory.                       |
| `purge_plugin_cache.py`        | Remove orphaned versions from the plugin cache.               |
| `remove_hook_from_registry.py` | Remove matching hook commands from a JSON registry.           |
| `resolve_memory_dir.py`        | Resolve the project-scoped Claude memory directory.           |
| `resolve_plugin_root.py`       | Resolve and validate the current installed plugin root.       |
| `resolve_shared_path.py`       | Resolve the plugin's shared directory portably.               |
| `resolve_skill_subdir.py`      | Resolve a skill `modes/` or `templates/` subdirectory.        |
| `state.py`                     | Persist small shell values across Bash tool-call boundaries.  |
| `symlink_with_guard.py`        | Scan and safely manage foundry-init symlink conflicts.        |

#### Reporting, synchronization, and transforms

| Helper                        | Purpose                                                                 |
| ----------------------------- | ----------------------------------------------------------------------- |
| `audit_hook_coverage.py`      | Measure how often the auto-allow hooks fire across session transcripts. |
| `build_blueprint_manifest.py` | Hash shipped bash blueprints into the per-plugin allow manifest.        |
| `cost_analyzer.py`            | Bucket Claude session token usage and estimated cost.                   |
| `extract_code_blocks.py`      | Extract and classify fenced Markdown code blocks.                       |
| `propagate_shared.py`         | Keep byte-identical cross-plugin shared files synchronized.             |
| `resolve_centrality.py`       | Convert codemap centrality output into a resolver map.                  |
| `timing_analyzer.py`          | Bucket Claude session clock time into a Markdown report.                |
| `verify_blueprint_audit.py`   | Verify and prune the auto-allow audit log in `~/.claude/logs/audit/`.   |
| `verify_perm.py`              | Verify permission presence across settings and the permissions guide.   |

</details>

<a id="upgrade"></a>

<details>

<summary><strong>🔄 Upgrade</strong></summary>

```bash
claude plugin install foundry@borda-ai-rig
```

Then, inside Claude Code:

```text
/foundry:setup
```

Re-run `/foundry:setup` after upgrade required — symlinks point to versioned cache path, go stale after reinstall.

</details>

______________________________________________________________________

<a id="uninstall"></a>

<details>

<summary><strong>🗑️ Uninstall</strong></summary>

```bash
claude plugin uninstall foundry
```

Claude Code runs no cleanup hook on uninstall, so nothing `/foundry:setup` created is removed by `claude plugin uninstall` or by `make clear-all`. Settings keys merged into `~/.claude/settings.json` (`statusLine`, `permissions.allow`, `permissions.deny`, `enabledPlugins`, `advisorModel`) remain — remove manually if desired. The `~/.claude/rules/foundry-*.md` symlinks and `~/.claude/TEAM_PROTOCOL.md` also persist and dangle once the plugin cache version is gone; delete them by hand.

</details>

______________________________________________________________________

#### Auto-allow audit log

`allow-dispatch.js` and `audit-close.js` append one JSON line each per Bash call to `~/.claude/logs/audit/`: one file per session (`s-<key>.jsonl`, the key being a hash of the session id) plus a shared `_no-session.jsonl` for calls that arrive without one. A record names the deciding plugin and hook, the session and tool-call ids, the working directory, the effective decision with its lane and provenance, and both decision modules' own verdicts. Command text is never written — only a digest, and for a provenance allow the manifest `src` it matched. Note the limit honestly: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`, so the guarantee is about this file rather than about the log directory.

With several plugins installed each writes its own rows and nothing coordinates them, so one completed call yields one row per plugin at each end. That is corroboration, not duplication.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" verify
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" prune --older-than 30 --dry-run
```

`verify` exits 1 only when a record's own hash fails to verify; torn framing from concurrent appends and every classification are warnings. `record_hash` detects a record that changed after it was written — it is not tamper-evidence, because the same user owns the log, the hooks and the checker. Read the classifications as observations rather than proof: a call with a decision row and no completion row is reported as `completion-unobserved` when the session later ended and `in-flight-or-hard-kill` when it did not, and neither distinguishes a refusal at the prompt from a deny rule elsewhere or a closer that died. Equally, that a tool ran never establishes that a human approved it. `prune` is the only component that deletes a log file, is never run by a hook, and never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id. Nothing schedules `prune`. `RIG_AUDIT=0` disables audit writing without changing any permission decision.

Records follow the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), an active individual-submission Internet-Draft that is not endorsed by the IETF and has no standing in its standards process. Deliberate deviations: `outcome` adds `pending`, because a decision that has not executed yet has no outcome in the draft's vocabulary; `trust_level` is `plugin`/`unknown` rather than the draft's `L0`–`L4`, because what is observed is which component proposed an allow, not an authentication level; `agent_id` is `<plugin>/<hook>` rather than a URI; `session_id` is the host's id verbatim rather than a UUID; and `parent_record_id` and `prev_hash` are always null, because this log is not chained — lineage is derived on read instead.

______________________________________________________________________

## 🧪 Development / testing

One runner (pytest) covers both Python `bin/` scripts and JS hooks. Current test count: `grep -rc 'def test_' plugins/cc_foundry/tests/*.py | awk -F: '{s+=$2} END{print s}'`.

Apply pytest markers and conditional skips directly to test functions or existing test classes, as described in the [bin authoring guide](https://github.com/Borda/AI-Rig/blob/main/plugins/cc_foundry/skills/_shared/bin-authoring-guide.md#bash-script-testing).

**Run locally** (requires Python ≥ 3.10 and Node ≥ 18 — run from repo root):

```bash
pip install pytest pytest-cov
pytest plugins/cc_foundry/tests/ -v
```

JS hook tests only:

```bash
pytest plugins/cc_foundry/tests/ -v -k "js"
```

### JS hook tests (subprocess, black-box)

Each test spawns `node <hook>.js` with JSON payload on stdin, asserts filesystem side-effects under `/tmp/claude-state-<session_id>/` + exit codes.

| File                                | Hook                        | Tests | Covered                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------------- | --------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_task_log_js.py`               | `task-log.js`               | 22    | Agent lifecycle (Pre/PostToolUse, SubagentStart/Stop), tool_use_id→agent_id re-key on SubagentStart (direct + agent_type-fallback match, including named agents matched by name) so SubagentStop's unlink actually lands, background agents kept past dispatch-time PostToolUse, pending/ race condition, worktree `last_active` refresh on tool activity, codex skill tracking, per-tool counters                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `test_statusline_js.py`             | `statusline.js`             | 11    | `🤖` segment: empty/active/stale agents, non-worktree agent stays visible past 10 min (60-min backstop) and drops past 60 min, long-running agent kept visible by fresh `last_active`, codex agent type label (via `agents/` and the `codex/` dir), missing-`agents/`-dir tolerance                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `test_commit_guard_js.py`           | `commit-guard.js`           | 8     | Commit always passes through (prompt-discipline only, not hook-gated), non-push Bash passthrough, SessionStart wipes push sentinel, force-push blocked unconditionally (even with sentinel), push sentinel gate (no auto-arm)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `test_agent_router_js.py`           | `agent-router.js`           | 4     | Builtin/plugin-agent passthrough (tier 1), unknown-agent reroute to `general-purpose` (tier 3), SessionStart index build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `test_batch_nudge_js.py`            | `batch-nudge.js`            | 9     | PreToolUse never blocks regardless of streak; no nudge below threshold; nudge (PostToolUse exit 2 + stderr) fires exactly on the call crossing threshold; small-gap calls (same-message dispatch) never accumulate; non-batchable tool (Edit) resets streak; UserPromptSubmit resets streak; read-only Bash prefix (`git log`) counted batchable, compound and mutating Bash commands are not                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `test_sentinel_read_allow_js.py`    | `sentinel-read-allow.js`    | 146   | Blueprint sentinel reads + date stamps + substitution-free `IFS= read -r` form allowed (observed prompt commands, unquoted variant, defaults, pipes into whitelisted tokens); passthrough on non-whitelisted tokens, non-TMPDIR paths, nested/second substitutions, backticks, process subst, heredocs, write redirects, quote smuggling, Codex bypass PoCs; whole-line comments skipped without letting a separator hide a live command, and a quoted `#` stays data; `..` matched as a path component so `...`/`v1.2..v1.3`/`file..txt` allow while leading/mid/trailing/bare/`${V:-..}` traversal rejects; verdict invariant across runtime value substitution (sentinel basename, var name, CSID form, target filename, subdir path) but flips on injection through the varied slot; no `updatedInput` emitted; non-Bash + malformed JSON hygiene; requiring the module attaches no stdin listener and does not exit, and `evaluate` labels allow / passthrough(`shape-mismatch`) / none(`not-applicable`) without changing the payload `decide` produces |
| `test_blueprint_allow_js.py`        | `blueprint-allow.js`        | 119   | Verbatim block/command/trailing-comment/trailing-whitespace/CRLF variants allowed with `src` provenance, no `updatedInput`; one-char deviations and recombined halves of two manifest entries passthrough; missing manifest, malformed JSON, wrong-typed entries, non-Bash tool, malformed stdin fail open; independent danger re-check refuses even a tampered danger-token entry the generator would never emit; normalization vectors shared with the generator via `blueprint_normalization_vectors.json` (digests agree across JS/Python); `evaluate` labels each decline by the check that made it (`danger`, `manifest-unavailable`, `bailout`, `no-match`) and carries a digest exactly when the command was normalized                                                                                                                                                                                                                                                                                                                               |
| `test_write_guard_js.py`            | `write-guard.js`            | 30    | Every protected class asks with its labelled reason (CI, agent instructions, permission config, lint gate, changelog, lockfile, package metadata); absolute paths, Windows backslash separators and case variants (`changelog.md`, `UV.LOCK` — case-folding filesystems) match; `Write` and `NotebookEdit` (via `notebook_path`) guarded alongside `Edit`; routine source/test edits and anchored near-misses (`src/my_changelog_helper.py`, `src/github/client.py`) pass through; unguarded tools, missing path and malformed stdin fail open; the only `permissionDecision` in the source is `ask`, and no `updatedInput` is emitted                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `test_enforce_audit_header_js.py`   | `enforce-audit-header.js`   | 30    | Follow-up gate denied while `summary.jsonl` missing/empty, passes once written; relative sentinel resolved against payload `cwd` (absolute honoured too); non-gate questions (`! BREAKING` ack, flag prompt) never blocked, gate wording in a description does not count; fail-open on no sentinel, stale sentinel, vanished run dir, implausible sentinel content, unexpected `tool_input` shape, unsafe CSID, malformed JSON; table-format nudge fires/skips per `report-header-table.js` reply-scan result once the aggregate exists                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `test_enforce_profile_header_js.py` | `enforce-profile-header.js` | 37    | Follow-up gate denied while `report.md` missing/empty, passes once written; `REPORT_DIR` read out of the `KEY=VALUE` state file across every form a shell binds (quoted, indented, `export`, CRLF, trailing space, last-assignment-wins); relative value resolved against payload `cwd` (absolute honoured too); fail-open on no sentinel, stale sentinel, vanished report dir, absent/malformed/empty `REPORT_DIR` line, non-profile path, unsafe CSID, malformed JSON; same table-format nudge coverage as above                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `test_report_header_table_js.py`    | `report-header-table.js`    | 12    | `hasHeaderTable`: pipe table with enough rows detected, raw YAML fields (the PR #1303 shape) rejected, below-`MIN_TABLE_ROWS` rejected, `·`-fallback line detected, empty text rejected; `assistantTextSinceLastUserTurn`: collects current-turn assistant text, `tool_result`-only rows are not a turn boundary, non-turn transcript rows (`queue-operation`/`attachment`/`mode`) skipped, sidechain (subagent) text excluded, missing/unreadable `transcript_path` returns `""`; `tableReminder` names skill + print step                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

`run_hook` and `state_dir` = pytest fixtures in `conftest.py`; test methods receive as parameters — no `from conftest import` needed.

**Why Python tests for JS hooks?** Black-box contract tests: each spawns real `node` process, sends JSON payload on stdin, asserts filesystem side-effects + exit codes. Python = orchestrator only — JS runs for real. One runner (pytest) covers all plugin tests (Python `bin/` scripts + JS hooks), no `package.json` or `node_modules` required. Tradeoff: covers hook *interface* contract, not individual JS function internals. Jest = right addition if internal logic ever warrants unit testing.

### Python `bin/` script tests

| File                               | Script                                      | Tests | Covered                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------------- | ------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_check_routing_links.py`      | `check_routing_links.py`                    | 42    | Computed path resolution, orphan-risk detection (R2), bin-ref integrity (R3), security path guard                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `test_check_codemap_guard.py`      | `check_codemap_guard.py`                    | 42    | MANIFEST-managed vs registry-declared vs provider-CLI classification, registry rot detection, git-root anchoring and raw-project-name invariants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `test_build_blueprint_manifest.py` | `build_blueprint_manifest.py`               | 37    | Normalization (CRLF, quote-aware comment strip, blank-run collapse, spacing variants never collide), logical-command splitting (never on `;`/`&&`, continuation-safe, escaped-backslash-aware), heredoc/quote-span bail-out to whole-block-only, danger-token detection (deferred + block-level, dropped count reported), block+command dual hashing with `src` at the opening fence, cross-OS path posix-ification (no backslash ever in emitted JSON), `--update`/`--check` drift + determinism, byte-identical two-run reproducibility                                                                                                                                                                                                                                                                                                                                                                                  |
| `test_symlink_with_guard.py`       | `symlink_with_guard.py`                     | 35    | Create/update/remove symlinks, guard against stale links, unconditional purge of `~/.claude/skills/` links                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `test_extract_code_blocks.py`      | `extract_code_blocks.py`                    | 30    | Fence parsing, lang normalisation, heuristic code/prose classification, token filtering                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `test_check_bash_persistence.py`   | `check_bash_persistence.py`                 | 28    | Cross-block variable reference detection, env-var filtering, multi-block files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `test_find_polluter.py`            | `find_polluter.py`                          | 30    | Safe/unsafe node-id validation, isolation test runner, bisect loop, path-traversal + report guidance                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `test_check_cli_flag_drift.py`     | `check_cli_flag_drift.py`                   | 35    | AST flag extraction, invocation-scoped matching, docstring `Usage:`-block scan vs own argparse surface, REMAINDER passthrough, no-exec guarantee                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `test_audit_log_js.py`             | `hooks/lib/audit-log.js`                    | 62    | Canonical serialization and record hash matched against committed cross-language vectors (key order, explicit null versus absence, non-ASCII and above-BMP characters, integers only, ill-formed strings rejected); filesystem keys hashed so `a/b` and `a?b` cannot merge and lone-surrogate ids are rejected before hashing; directory 0700 and file 0600; append never rewrites earlier bytes; disabled, unwritable, symlinked and unserializable cases return `_unwritten` rather than throwing; `_no-session` soft cutoff refuses and leaves the file byte-identical; identity read from `plugin.json`, not the version-named directory; the export surface contains no file-removing API                                                                                                                                                                                                                             |
| `test_hook_stdout_captures.py`     | both decision modules + `allow-dispatch.js` | 37    | Replays `fixtures/hook_stdout_captures.json`, the raw stdout bytes both modules wrote before the refactor, across the whole command corpus and six manifest scenarios: each module's standalone driver is byte-unchanged, and the dispatcher writes \`blueprint                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `test_allow_dispatch_js.py`        | `allow-dispatch.js`                         | 19    | One row per call carrying the effective verdict plus both lanes' verdicts; blueprint outranks shape when both allow; whitespace splits the lanes (passthrough versus none); both-none writes no row; a module that throws or fails to load leaves the other deciding and is recorded `module-error`; a stalled shape lane does not delay an already-written blueprint allow; `RIG_AUDIT=0` never loads the library, and a broken library leaves stdout and exit code untouched; raw command text never reaches the log and nothing outside the audit directory is created; `integration`-marked concurrent writers all verify their own hashes                                                                                                                                                                                                                                                                             |
| `test_audit_close_js.py`           | `audit-close.js`                            | 32    | Success and failure rows with the same status words `timings.jsonl` uses; both events write both rows; authority is never inferred from execution; non-Bash and unhandled events write nothing; lifecycle rows are skipped without a usable session id while completion rows still land in the shared stream; four simulated plugins produce four retained rows naming their own versions; stdout empty on every event; nothing created outside the log                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `test_verify_blueprint_audit.py`   | `bin/verify_blueprint_audit.py`             | 89    | Table-driven against `fixtures/audit/` — a corpus and its expectations built before the verifier existed. Exit 1 only on a failed record hash; torn framing and every classification stay warnings. Covers the classifications that were wrong in earlier designs: a healthy four-plugin file is clean, four closers are corroboration while a missing one is `incomplete-closure`, a four-plugin shape allow is clean apart from `multi-allow`, abstention requires a real passthrough, and `none`/`module-error` are counted apart from it. Plus the reader's own robustness: a stray directory or dead symlink in the log dir must not raise, since an uncaught raise exits 1 and that code means `corrupt-record`. Plus schema rejection of values this system never writes, both limits, the JSON output shape and totals, and prune's dry run, age cutoff, `--keep-session` and refusal to prune `_no-session.jsonl` |
| `test_audit_registration.py`       | hooks.json + propagation + docs             | 38    | Every plugin registers the dispatcher, registers neither decision module directly, and registers the closer on all four events with the right matcher; the four new files are in the propagation manifest with all three copies and the check-only gate passes; the verifier is named in all four READMEs (a gap `check_orphaned_bin.py` does not cover) and no document still claims a registration order; the shape module still runs as a subprocess for `audit_hook_coverage.py`; a `claude-state-<session>` teardown cannot touch the audit log                                                                                                                                                                                                                                                                                                                                                                       |
| `test_audit_hook_coverage.py`      | `audit_hook_coverage.py`                    | 22    | Manifest union across plugins (single-plugin probe under-reports), newest-version selection by numeric key at any nesting depth, mechanism attribution with blueprint winning over shape, memoization, shape-hook argv/payload dispatch, transcript parsing (Bash-only, malformed-line and vanished-file tolerance), `--since`/`--project`/`--skills-only` filters                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `test_verify_perm.py`              | `verify_perm.py`                            | 21    | Settings allow-entry detection, missing/malformed JSON, CLI exit codes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `test_check_orphaned_bin.py`       | `check_orphaned_bin.py`                     | 21    | Orphaned bin/ script detection, consumer-reference parsing, multi-plugin scan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `test_cost_analyzer.py`            | `cost_analyzer.py`                          | 21    | Message-id dedupe (3x-inflation guard), tier/cost pricing, main/sidechain bucketing, subagent-transcript merge, session discovery no-double-count, CLI session-id + window modes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `test_jq_write.py`                 | `jq_write.py`                               | 18    | Arg parsing, JSON path writes, merge semantics                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `test_resolve_shared_path.py`      | `resolve_shared_path.py`                    | 21    | Plugin/subdir validation, path traversal rejection, tier-1/2/3 resolution cascade                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `test_heal_git_artifacts.py`       | `heal_git_artifacts.py`                     | 67    | PID liveness on both platform branches (Windows simulated on every host, POSIX probe patched so signal 0 is asserted rather than sent), lock verdicts, worktree tiering, CLI exit codes, and a guard that no doctest calls the POSIX probe — signal 0 is `CTRL_C_EVENT` on Windows, so one did abort every Windows CI leg with a `KeyboardInterrupt`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `test_health_sentinel.py`          | `health_sentinel.py`                        | 18    | Sentinel creation, age computation, stale detection, new-file polling                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `test_check_fence_symmetry.py`     | `check_fence_symmetry.py`                   | 17    | Unclosed fences, nested/interleaved fences, multi-file scan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `test_check_gitignored_refs.py`    | `check_gitignored_refs.py`                  | 12    | Token extraction, existence+check-ignore classification in a real tmp checkout, waiver marker, placeholder templates, CLI exit codes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `test_check_bridge.py`             | `check_bridge.py`                           | 9     | nested registry parsing, `installPath` requirement, stale-cache rejection, settings opt-out                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `test_make_run_dir.py`             | `make_run_dir.py`                           | 16    | Portability invariants (no `/tmp` literals, no `utcnow`), timestamp format                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `test_check_spawn_prompt_vars.py`  | `check_spawn_prompt_vars.py`                | 16    | `$VAR` in markdown code blocks, caller-substituted-var whitelist, multi-file scan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `test_check_tag_symmetry.py`       | `check_tag_symmetry.py`                     | 14    | Empty/whitespace XML blocks, unbalanced open/close tags, multi-file scan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `test_resolve_skill_subdir.py`     | `resolve_skill_subdir.py`                   | 11    | Tier-1/2/3 subdir resolution cascade, local-override flag, fallback ordering                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `test_purge_plugin_cache.py`       | `purge_plugin_cache.py`                     | 17    | Report-vs-apply contract, seven deletion guards, `--expect-count` abort, argument rejection                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `test_resolve_memory_dir.py`       | `resolve_memory_dir.py`                     | 10    | Path slugification, `PROJECT_ROOT` override, git fallback, missing-git fallback                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `test_resolve_plugin_root.py`      | `resolve_plugin_root.py`                    | 9     | Registry lookup, cache-scan fallback, orphaned-version skip, security gates (cache-dir + manifest-name)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `test_get_plugin_install_path.py`  | `get_plugin_install_path.py`                | 7     | Registry lookup, multiple-entry tie-breaking, missing plugin exit code                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `test_classify_resolver_sites.py`  | `classify_resolver_sites.py`                | 5     | Directory scan for extractable/non-extractable resolver sites, CLI report + `--list-extractable` — maintainer-only, ad hoc invocation, not wired into any skill                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `test_c33_dir_resolution.py`       | _(C33 dir-resolution logic)_                | 4     | Latest-version selection, older-version exclusion, no-cache fallback                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

CI runs full test suite on every push to `main` and on PRs touching `plugins/` (see `.github/workflows/ci-tests.yml`).

______________________________________________________________________

## 🙏 Contributing / feedback

foundry = part of Borda-AI-Rig repository. Suggest improvement or report bug:

1. Run `/foundry:brainstorm "your idea"` to develop idea before filing
2. File issue on repository — include output of `/foundry:audit setup` + Claude Code version
3. Plugin updates propagate to users via `claude plugin install foundry@borda-ai-rig` + `/foundry:setup`

New agent or skill: use `/foundry:manage create` — handles scaffolding, README sync, MEMORY.md updates automatically.
