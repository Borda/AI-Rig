---
name: audit
description: "Full-sweep quality audit of .claude/ config — cross-references, permissions, inventory drift, model tiers, docs freshness. Two mutually exclusive action modes: 'fix [high|medium|all]' auto-fixes at the requested severity level; 'upgrade' applies docs-sourced improvements with correctness verification and calibrate A/B testing for capability changes."
argument-hint: '[agents|skills|rules|communication|setup|plugin] fix [high|medium|all] | upgrade'
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
effort: high
---

<objective>

Run a full-sweep quality audit of the `.claude/` configuration: every agent file, every skill file, every rule file, settings.json, and hooks. Spawns `foundry:self-mentor` for per-file analysis, then aggregates findings system-wide to catch issues that only surface across files — infinite loops, inventory drift, missing permissions, and cross-file interoperability breaks. Reports all findings and auto-fixes at the requested level: `fix high` (critical+high only), `fix medium` (critical+high+medium, default fix level), or `fix all` (all findings including low).

</objective>

<inputs>

- **$ARGUMENTS**: optional — **`fix` and `upgrade` are mutually exclusive; never combine them**
  - No argument: full sweep, report only — lists all findings, no changes made (default)
  - `fix high` — fix `critical` and `high` findings; `medium` and `low` reported only
  - `fix medium` — fix `critical`, `high`, and `medium` findings; `low` reported only
  - `fix all` — fix all findings including `low`
  - `fix` (no level) — alias for `fix medium` (backward compatible)
  - `upgrade` — fetch latest Claude Code docs, filter new features by genuine value, then apply: **config** changes (apply + correctness check), **capability** changes (calibrate before → apply → calibrate after → accept if Δrecall ≥ 0 and ΔF1 ≥ 0). Skip to **Mode: upgrade**.
  - `agents` — restrict sweep to agent files only, report only
  - `skills` — restrict sweep to skill files only, report only
  - `rules` — restrict sweep to rule files only, report only
  - `communication` — restrict sweep to communication governance files: `rules/communication.md`, `rules/quality-gates.md`, `TEAM_PROTOCOL.md`, `skills/_shared/file-handoff-protocol.md`
  - `setup` — restrict sweep to system-configuration files: `settings.json`, `permissions-guide.md`, hooks, `MEMORY.md`, `README.md`, plugin integration, and post-install user state (Checks 1–11, I1, I2, I3); Step 3 runs for `init` SKILL.md only (one foundry:self-mentor spawn); Checks I1–I3 read `~/.claude/` not `.claude/`
  - `plugin` — restrict sweep to plugin integration only: codex plugin (Check 7), foundry plugin + init validation (Check 8, including 8g); Step 3 runs for `init` SKILL.md only (one foundry:self-mentor spawn)
  - Scope and fix level can be combined: `agents fix medium`, `rules fix all` — scope always precedes `fix`
  - **Invalid combinations** (report error and stop): `fix upgrade`, `upgrade fix`, `upgrade agents`, combining any scope/fix flag with `upgrade`

</inputs>

<constants>
<!-- Background agent health monitoring (CLAUDE.md §8) — applies to Step 3 foundry:self-mentor spawns -->
MONITOR_INTERVAL=300   # 5 minutes between polls
HARD_CUTOFF=900        # 15 minutes of no file activity → declare timed out
EXTENSION=300          # one +5 min extension if output file explains delay
</constants>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Orchestration contract**: the audit orchestrator is a thin coordinator — it issues Glob/Grep calls for inventory, spawns agents, reads JSON envelopes, and aggregates findings. It must NOT read agent/skill/rule file bodies directly. Any inline read of a non-template file is a protocol violation and will cause context overflow at scale.

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase and mark status live so the user can see progress in real time:

- Phase 1: setup + collect (Pre-flight + Steps 1–2) → mark in_progress when starting, completed when file list is ready
- Phase 2: per-file audit (Step 3) → mark in_progress when agents launch, completed when all reports received
- Phase 3: system-wide checks (Step 4) → mark in_progress when checks start, completed when all checks done
- **Phases 2 and 3 launch simultaneously** — mark both in_progress in the same update; they are independent and must not be serialized
- Phase 4: aggregate + fix (Steps 5–10) → mark in_progress, then completed when fixes land
- Phase 5: final report (Step 11) → mark in_progress, then completed before output
- On loop retry or scope change → create a new task; do not reuse the completed task

Surface progress to the user at natural milestones: after system-wide checks ("✓ Checks 1-21 complete, N findings so far — spawning per-file audits"), after agent reports ("Agent reports received — N medium, N low findings"), and before each fix batch ("Fixing N medium findings in parallel").

## Pre-flight checks

**Context budget**: the full audit (12+ agents, 14+ skills, 12 system checks) runs close to context limits. Strict file-based handoff is mandatory — every sub-agent writes its full output to a file and returns only a compact JSON envelope. Any sub-agent that echoes findings back to context will cause compaction before the audit completes.

```bash
RED='\033[1;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'

# From _shared/preflight-helpers.md — TTL 4 hours, keyed per binary
preflight_ok() {
    local f=".claude/state/preflight/$1.ok"
    [ -f "$f" ] && [ $(($(date +%s) - $(cat "$f"))) -lt 14400 ]
} # timeout: 5000
preflight_pass() {
    mkdir -p .claude/state/preflight
    date +%s >".claude/state/preflight/$1.ok"
} # timeout: 5000

# .claude/ directory must exist (not cached — filesystem state)
if [ ! -d ".claude" ]; then
    printf "${RED}! BREAKING${NC}: .claude/ directory not found — nothing to audit\n"
    exit 1
fi

# jq availability — Check 4 depends on it
if preflight_ok jq; then
    JQ_AVAILABLE=true
elif command -v jq &>/dev/null; then # timeout: 5000
    preflight_pass jq
    JQ_AVAILABLE=true
else
    printf "${YEL}⚠ MISSING${NC}: jq not found — Check 4 (permissions-guide drift) will be skipped\n"
    JQ_AVAILABLE=false
fi

# git availability — used in path portability check and baseline context
if ! preflight_ok git && ! command -v git &>/dev/null; then # timeout: 5000
    printf "${YEL}⚠ MISSING${NC}: git not found — path portability check may miss repo-root references\n"
else
    preflight_ok git || preflight_pass git
fi

# node availability — Check 10 (RTK prefix parsing) and upgrade mode (hook syntax check) depend on it
if preflight_ok node; then
    NODE_AVAILABLE=true
elif command -v node &>/dev/null; then # timeout: 5000
    preflight_pass node
    NODE_AVAILABLE=true
else
    printf "${YEL}⚠ MISSING${NC}: node not found — Check 10 (RTK hook parsing) and upgrade hook syntax check will be skipped\n"
    NODE_AVAILABLE=false
fi
```

If `.claude/` is missing, abort immediately. Missing `jq` is a warning — the audit continues with Check 4 skipped.

## Step 1: Run pre-commit (if configured)

```bash
# Check whether pre-commit is installed and a config exists
if (preflight_ok pre-commit || { command -v pre-commit &>/dev/null && preflight_pass pre-commit; }) &&
[ -f .pre-commit-config.yaml ]; then
    pre-commit run --all-files # timeout: 600000
fi
```

Any files auto-corrected by pre-commit hooks (formatters, linters, whitespace fixers) are now clean before the structural audit begins. Note which files were modified — include them in the audit scope even if they were not originally targeted.

If pre-commit is not configured, skip this step silently.

## Step 2: Collect all config files

Enumerate everything in scope using built-in tools:

- **Agents**: Glob tool, pattern `agents/*.md`, path `.claude/`
- **Skills**: Glob tool, pattern `skills/*/SKILL.md`, path `.claude/`
- **Rules**: Glob tool, pattern `rules/*.md`, path `.claude/`
- **Communication**: Read tool on `rules/communication.md`, `rules/quality-gates.md`, `TEAM_PROTOCOL.md`, `skills/_shared/file-handoff-protocol.md`
- **Settings**: Read tool on `.claude/settings.json`
- **Hooks**: Glob tool, pattern `hooks/*`, path `.claude/`

Record the full file list — this becomes the audit scope for Steps 3–4. Cross-reference checks in Step 3 depend on this inventory being current. If MEMORY.md has not been updated since the last agent or skill was added or removed, run a live disk scan now rather than relying on the cached roster. Stale inventory is the primary cause of false-negative cross-reference findings.

**Setup scope**: when `$SCOPE` is `setup`, also collect `plugins/foundry/skills/init/SKILL.md` for the Step 3 foundry:self-mentor spawn — this is the only per-file spawn in setup scope. Checks I1–I3 (from `checks-install.md`) run in Step 4 against `~/.claude/` to validate post-install user state.

## Step 3: Per-file audit via foundry:self-mentor

**Context management** — with 12+ agents and 14+ skills, accumulating full foundry:self-mentor responses in context causes overflow before aggregation. Use file-based findings to keep the main context lean.

**Hard rule — no pre-reading**: Never call Read on an agent or skill file before spawning foundry:self-mentor on it. The spawned agent does the reading. The orchestrator only reads the returned JSON envelope. Pre-reading 41 KB agent/skill files into main context before spawning defeats the entire purpose of delegation and will cause context overflow at scale.

**Batching rule**: For scopes with >5 files, always batch into groups of up to 10 — never spawn one agent per file at scale, as this creates N parallel agents each inflating the coordinator context with their JSON envelope. Batching is the default for any scope larger than 5 files; one-per-file spawning is only acceptable for ≤5 files.

**Scope-restricted runs**: for a scoped run (e.g. `/audit skills`, `/audit agents`) targeting fewer than 5 files, spawn one batched foundry:self-mentor for ALL files in scope — not one agent per file. Read only the one relevant template file for the active scope (not all 4 template files).

Set up the run directory once before spawning any agents:

```bash
RUN_DIR=".reports/audit/$(date -u +%Y-%m-%dT%H-%M-%SZ)" # timeout: 5000
mkdir -p "$RUN_DIR"                                     # timeout: 5000
echo "Run dir: $RUN_DIR"
```

Spawn **foundry:self-mentor** agents in batches of up to 10 files per agent (default) — or one batch for all files if scope ≤5 files. The spawn prompt for each agent must:

1. Include the content from `.claude/skills/audit/templates/self-mentor-prompt.md`
2. Include the disk inventory from Step 2 (agent/skill list for cross-reference validation)
3. End with:

> "Write your FULL findings (all severity levels, Confidence block) to `<RUN_DIR>/<file-basename>.md` using the Write tool — where `<file-basename>` is the filename only (e.g. `shepherd.md`, `audit-SKILL.md`). Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<RUN_DIR>/<file-basename>.md\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"<filename>: N critical, N high, N medium, N low\"}`"

Replace `<RUN_DIR>` with the actual directory path and `<file-basename>` with just the filename.

**Critical context discipline**: do NOT include any other text, tool output summaries, or findings in the response body — only the JSON envelope on the final line. All content goes to the file.

> The template file is canonical for the per-file audit criteria. The disk inventory and RUN_DIR path injected here are runtime values added to each agent spawn.

After all spawns complete, you will have a list of short summaries in context. Use these to identify which files have findings. The full content is in the run directory files.

**Health monitoring** (CLAUDE.md §8): after spawning all batches, create a checkpoint:

```bash
AUDIT_CHECKPOINT="/tmp/audit-check-$(date +%s)" # timeout: 5000
touch "$AUDIT_CHECKPOINT"                       # timeout: 5000
```

Every `$MONITOR_INTERVAL` seconds, run `find $RUN_DIR -newer "$AUDIT_CHECKPOINT" -type f | wc -l` — new files = agents alive; zero new files for `$HARD_CUTOFF` seconds = stalled. Grant one `$EXTENSION` extension if the output file tail explains the delay. On timeout: read partial output from the stalled agent's file; surface it with ⏱ in the final report. Never silently omit timed-out agents.

## Step 4: System-wide checks

> **Full implementation instructions** are split across 4 scope files in `.claude/skills/audit/templates/`. Read only the file(s) for the active scope at the start of this step — do not read all 4 files unless running a full sweep.
>
> | Scope | File(s) to read |
> | --- | --- |
> | `setup` | `checks-setup.md` + `checks-install.md` |
> | `plugin` | `checks-setup.md` (Checks 7, 8 only) |
> | `agents` | `checks-agents.md` + `checks-shared.md` (run only: 14, 15, 17, 12, 13, 25, 29) + `checks-skills.md` (Check 22 only) |
> | `skills` | `checks-skills.md` + `checks-shared.md` (run only: 14, 15, 17, 12, 13, 25, 29) |
> | `rules` | `checks-shared.md` (run only: 18, 12, 13, 29) |
> | `communication` | `checks-shared.md` (run only: 15, 16, 12, 13, 29) |
> | No scope (full) | all 4 files |

**Delegation for full-sweep runs**: for full-sweep runs (no scope restriction), spawn a dedicated `foundry:self-mentor` agent to execute Step 4 checks for each scope group, passing the relevant template file path and RUN_DIR. Use one agent per scope group: agents-checks (reads `checks-agents.md` + relevant `checks-shared.md` entries), skills-checks (reads `checks-skills.md` + relevant `checks-shared.md` entries), shared-checks (reads `checks-shared.md`), and setup-checks (reads `checks-setup.md` + `checks-install.md`). Each agent writes its findings to `<RUN_DIR>/system-checks-<scope>.md` and returns only a JSON envelope. The orchestrator does NOT read the template files itself in this case — it passes only the file path to the spawned agent.

Run the following checks. Use native tools first (Glob, Grep, Read); Bash only for pipeline operations the native tools cannot do.

**Agent roster consistency policy**: evaluate the agent system as a set of capabilities, not just files. For every overlap surfaced in checks 20 or 17, make an explicit judgment:

- **keep** when both roles own meaningfully different acceptance criteria
- **sharpen** when both roles are justified but one or both descriptions/handoffs are too fuzzy
- **merge/prune** when the roles differ mostly by tone or examples rather than by decision surface

Do not leave overlap findings as vague "potential duplication" notes. The audit must say which of the three outcomes applies and why.

**Context discipline for Step 4**: write all check findings to `$RUN_DIR/system-checks.md` (using Write tool after all checks complete), not to the main conversation context. Keep only a one-line status per check in context:

- `✓ Check N — <one-line result>` (pass)
- `⚠ Check N — N findings` (issues)

**Scope filter**: when `$SCOPE` is set, run only the checks listed for that scope; skip all others silently.

- `agents` — Checks 14, 15, 19, 20, 17, 12, 13, 25, 22, 26, 29
- `skills` — Checks 14, 15, 21, 17, 12, 23, 22, 13, 24, 25, 26, 27, 28, 29
- `rules` — Checks 18, 12, 13, 29
- `communication` — Checks 15, 16, 12, 13, 29
- `setup` — Checks 1, 2, 3, 4, 5, 9, 10, 11, 7, 6, 8, I1, I2, I3 (Step 3: one foundry:self-mentor spawn for `init` SKILL.md only; I1–I3 read `~/.claude/`)
- `plugin` — Checks 7, 8 (Step 3: one foundry:self-mentor spawn for `init` SKILL.md only)
- No scope argument — run all checks

### Check summary
<!-- Full check implementations in `audit/templates/checks-*.md` — this table is the quick-reference index only. -->

| # | Name | Severity | Scope | Notes |
| --- | --- | --- | --- | --- |
| 1 | Inventory drift (MEMORY.md vs disk) | medium | setup | Agents + skills on disk vs MEMORY.md roster |
| 2 | README vs disk | medium | setup | Agent/skill table rows in README vs disk |
| 3 | settings.json permissions | medium | setup | Bash commands in skills vs allow list |
| 4 | permissions-guide.md drift | medium | setup | Every allow entry must have a guide row, and vice versa |
| 5 | Permission safety audit | critical/high | setup | Allow entries must be non-destructive, reversible, local-only |
| 6 | Stale settings.json allow entries | low | setup | Allow entries with no usage in any .claude/ file |
| 7 | codex plugin integration | medium | setup | Plugin installed and enabled; dispatches work |
| 8 | foundry plugin correctness | critical/high/med | setup | 8a manifest, 8b symlinks, 8c hook scripts, 8d hooks.json, 8e dry-run validate, 8f perms drift |
| 9 | Agent color drift | medium | setup | statusline COLOR_MAP vs agent frontmatter color: |
| 10 | RTK hook alignment | high/medium | setup | RTK_PREFIXES vs installed RTK subcommands - skip if rtk absent |
| 11 | Memory health | low | setup | 11a duplicate rules, 11b stale version pins, 11c absorbed feedback files |
| I1 | Plugin cache intact | high | setup | foundry in ~/.claude/plugins/installed_plugins.json; installPath exists |
| I2 | Settings merge complete | medium | setup | statusLine, permissions.allow, enabledPlugins.codex in ~/.claude/settings.json |
| I3 | Link health (conditional) | high | setup | Symlinks in ~/.claude/rules/ and ~/.claude/TEAM_PROTOCOL.md resolve; fix: /foundry:init |
| 12 | File length | medium | all | Agents >300, skills >600, rules >200 lines - report only |
| 13 | Heading hierarchy continuity | medium | all | Heading level jumps >1 (e.g. ## to ####) |
| 14 | Orphaned follow-up references | medium | agents/skills | Skill-name refs in SKILL.md vs disk inventory |
| 15 | Hardcoded user paths | high | agents/skills | /Users/ and /home/ in config files + settings.json |
| 16 | Example value vs. token cost | low | agents/skills | Inline examples: high-value vs. low-value (prose restatement) |
| 17 | Cross-file content duplication | medium | agents/skills | 40%+ consecutive step overlap; recommend canonical owner or merge path |
| 18 | Rules integrity | high/medium | rules | 18a inventory, 18b frontmatter, 18c redundancy, 18d cross-ref integrity |
| 19 | Model tier appropriateness | medium/high | agents | Tier policy: opusplan/opus/sonnet/haiku - report only |
| 20 | Agent description routing | medium/low | agents | 20a overlap pairs, 20b NOT-for coverage, 20c trigger specificity, 20d keep/sharpen/prune |
| 21 | Skill frontmatter conflicts | critical | skills | context:fork + disable-model-invocation:true is broken |
| 22 | Calibration coverage gap | medium/low | agents/skills | Unregistered calibratable skills/agents; stale domain table entries |
| 23 | Bash misuse / native tool substitution | medium | agents/skills | cat/grep/find/echo>/sed replaceable by native tools |
| 24 | Skill sequence compatibility | high/medium | skills | 24a target skill not on disk; 24b argument absent from argument-hint; scans skills, agents, READMEs |
| 25 | Implicit agent references | high | agents/skills | subagent_type without plugin prefix (e.g. "sw-engineer" instead of "foundry:sw-engineer"); exempt: built-in types |
| 26 | Symbol and shortcut consistency | medium/low | agents/skills | 26a same-concept emoji conflict, 26b slash notation mixed, 26c body contradicts legend |
| 27 | Cross-plugin shared-file ref integrity | critical/high/med | skills | 27a absent from foundry/\_shared/; 27b catch-22 (fallback needs foundry); 27c plugin-local \_shared/ unmounted |
| 28 | Cross-plugin agent dispatch fallback | high/medium | skills | 28a no fallback for cross-plugin dispatch; 28b fallback present but incomplete |
| 29 | LLM context minimality | medium/low | agents/skills/rules | Within-file repetition, prose inflation, obvious-consequence restatement — report only |

### Claude Code docs freshness (within Step 4)

Spawn a **foundry:web-explorer** agent to fetch current Claude Code documentation. **File-based handoff**: foundry:web-explorer writes full findings to `$RUN_DIR/docs-freshness.md` using the Write tool. Return ONLY a compact JSON envelope: `{"status":"done","file":"$RUN_DIR/docs-freshness.md","findings":N,"deprecated":N,"new_features":N,"confidence":0.N,"summary":"N findings, N deprecated, N new features"}`

Validate the local config against fetched docs:

- **Hook validation**: every hook event name and `type` exists in documented schema; no deprecated `decision:`/`reason:` fields
- **Agent frontmatter validation**: all fields in documented schema; `model` values are recognized short-names
- **Skill frontmatter validation**: all fields in documented schema
- **Improvement opportunities**: new features passing the genuine-value filter → **Upgrade Proposals** table (max 5; classify as `config` or `capability`)

Findings: deprecated/invalid = **high**; deprecated frontmatter field = **medium**; new feature not used = **Upgrade Proposals** (not a LOW finding).

<!-- URLs fetched live by web-explorer at runtime; graceful degradation: if any 404, instruct navigation from code.claude.com homepage. -->

After all checks complete: collect all `⚠` lines, write the full details to `$RUN_DIR/system-checks.md`, and include only the summary table in the conversation context.

## Step 5: Aggregate and classify findings

**Delegate aggregation to a consolidator agent** to avoid flooding the main context with all agent findings. Spawn a **foundry:self-mentor** consolidator agent with this prompt:

> "Read all finding files in `<RUN_DIR>/` (\*.md files from Steps 3–4, including `docs-freshness.md` if present). Apply the severity classification from `.claude/skills/audit/severity-table.md`. Antipatterns that indicate severity under-classification are also in that file. Group all findings by severity (critical, high, medium, low). Apply the one-finding-per-issue rule: when a single location has multiple distinct problems at different severities, emit one finding entry per problem. Write the aggregated severity table to `<RUN_DIR>/aggregate.md` using the Write tool. Also write `<RUN_DIR>/summary.jsonl` — one compact JSON object per line, one line per finding: `{"file":"<basename>","sev":"high|medium|low","id":"H1","one_line":"<finding description>"}`. This file is what the orchestrator will read; aggregate.md is for human review only. Return ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"file\":\"<RUN_DIR>/aggregate.md\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"confidence\":0.N,\"summary\":\"N findings total: C critical, H high, M medium, L low\"}`"

Main context receives only that one-liner. The orchestrator MUST NOT read `aggregate.md` in full — it is 200–600 lines and would overflow context on large audits. Instead, use `$RUN_DIR/summary.jsonl` for all dispatch decisions in Steps 7 and 8.

## Step 6: Cross-validate critical findings

Read and follow the cross-validation protocol from `.claude/skills/_shared/cross-validation-protocol.md`.

**Skill-specific**: the verifier agent is always **foundry:self-mentor**.

## Step 7: Report findings

Output a structured audit report before fixing anything:

```
## Audit Report — .claude/ config

### Scope
- Agents audited: N
- Skills audited: N
- Rules audited: N
- System-wide checks: inventory drift, README sync, permissions, infinite loops, hardcoded paths, CLAUDE.md consistency, docs freshness, permissions-guide drift, model tier appropriateness, agent color drift, RTK hook alignment, memory health, agent routing alignment, codex plugin integration check, rules integrity, cross-file content duplication, file length, Bash misuse / native tool substitution, stale allow entries, calibration coverage gap, heading hierarchy continuity, skill sequence compatibility, LLM context minimality

### Findings by Severity

#### Critical (N)
| File | Line | Issue | Category |
|---|---|---|---|
| agents/foo.md | 42 | References `bar-agent` which does not exist on disk | broken cross-ref |

#### High (N)
...

#### Medium (N)
...

#### Low (N) — auto-fixed only with 'fix all'; otherwise reported only
...

### Summary
- Total findings: N (C critical, H high, M medium, L low)
- Auto-fix eligible: N per fix level — `fix high`: C+H | `fix medium`: C+H+M | `fix all`: C+H+M+L

### Upgrade Proposals (N — run `/audit upgrade` to apply)
| # | Feature | Type | Rationale |
|---|---------|------|-----------|
| 1 | ... | config | ... |

(omit this section entirely if no proposals passed the genuine-value filter)
```

If no fix level was passed, stop here and present the report.

## Step 8: Delegate fixes to subagents

> **HARD RULE — No inline fixes**: The orchestrator MUST NOT apply any fix directly using Edit or Write tools — not even single-line edits. Every fix at every severity level goes through a sub-agent. This is not optional. The overhead of spawning is always lower than the context cost of 40+ inline Edit calls accumulated across a `fix all` run.

**Fix Action Hierarchy** — before applying any fix, reason through this order:

1. **Reason** — is the finding actually correct? Is the flagged content genuinely wrong, or just in the wrong place? A misidentified finding should be discarded, not acted on.
2. **Relocate** — if the content is correct but in the wrong location, move it rather than removing it.
3. **Consolidate** — if the content is redundant with something nearby, merge into one clearer location.
4. **Minimize** — if the content is too long but otherwise valid, compress it (tighten wording, remove restatements).
5. **Remove** — only if none of the above apply. Never remove solely because something was flagged as verbose.

Apply this hierarchy to every fix action at all severity levels.

Choose the fix agent based on file type:

- **`.claude/agents/*.md` and `.claude/skills/*/SKILL.md`** → spawn **foundry:self-mentor** — it has domain expertise in config quality and has `Write`/`Edit` tools
- **Code files** (`.py`, `.js`, `.ts`, etc.) → spawn **foundry:sw-engineer**

**Phase 4 delegation rule**: fix-phase edits that touch >3 files should be delegated to a `foundry:sw-engineer` agent rather than applied inline — pass it the list of findings and target file paths; it applies Edit calls and returns a compact status JSON.

Spawn one agent per affected file, batching all findings for that file into a single subagent prompt. Issue **all spawns in a single response** for parallelism.

Each subagent prompt template: Read the fix prompt template from .claude/skills/audit/templates/fix-prompt.md and use it, filling in `<file path>` and the list of findings.

**Preferred orchestration pattern — audit-fix sub-agent**

When the finding count exceeds 10 or `fix all` was passed, spawn a dedicated **audit-fix** sub-agent that handles all of Steps 8–10 in isolation:

```
Read `<RUN_DIR>/summary.jsonl` — this is the findings list (one JSON object per line).
Read `.claude/skills/audit/templates/fix-prompt.md` for the per-file fix prompt template.
For each unique file in the findings list, spawn one fix agent (foundry:self-mentor for .md files, foundry:sw-engineer for .js/.py files) with all findings for that file batched into a single prompt.
Issue all fix spawns in a single response for parallelism.
After all fix agents complete, spawn foundry:self-mentor re-audit agents (one per changed file) to confirm fixes held.
Write a completion summary to `<RUN_DIR>/fix-summary.md`:
  - findings_total: N
  - fixed: N
  - failed: N
  - re_audit_clean: true|false
Return ONLY: {"status":"done","file":"<RUN_DIR>/fix-summary.md","fixed":N,"failed":N,"re_audit_clean":true|false,"confidence":0.N}
```

The orchestrator (main context) then reads only the compact JSON envelope. It does NOT read fix-summary.md unless `re_audit_clean: false` or `failed > 0`.

When finding count ≤ 10 and fix level is `fix high` or `fix medium` (not `fix all`), the inline batched pattern (one fix-agent per file, all spawned in parallel) is acceptable without the dedicated orchestrator sub-agent.

**Exceptions — handle inline without subagents (note in report):**

- **settings.json permission missing**: report only — structural JSON edits are risky to delegate
- **CLAUDE.md contradiction**: raise to user — do not auto-fix (CLAUDE.md takes precedence)
- **Dead loop**: flag for user review — requires human judgment on which link to break
- **Model tier mismatch**: report only — model assignments may be intentional for cost/latency trade-offs; user decides whether to adjust

After all subagents complete, collect their results and proceed to Step 10.

**Low findings** (nits): fix only when `fix all` was passed — otherwise collect in the final report for optional manual cleanup.

## Step 9: Codex cross-file check

After all Step 8 fix agents complete and before foundry:self-mentor re-audit:

Read `.claude/skills/_shared/codex-prepass.md` and run the Codex pre-pass on the combined diff of all fixes.

Treat any findings as additional issues entering Step 10's re-audit scope. Skip if Step 8 touched only 1 file.

## Step 10: Re-audit modified files + confidence check

For every file changed in Step 8, spawn **foundry:self-mentor** again to confirm the fix resolved the finding and no new issues were introduced. Use the same file-based approach as Step 3 — write full re-audit findings to `<RUN_DIR>/<file-basename>-reaudit.md` and return ONLY a compact JSON envelope: `{"status":"done","file":"<RUN_DIR>/<file-basename>-reaudit.md","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"confidence":0.N,"summary":"<filename>: fix confirmed, N residual findings"}`

```bash
# Spot-check: confirm the previously broken reference no longer appears
grep -n "<broken-name>" <fixed-file>
```

**Confidence re-run**: parse each confidence score from the one-line summaries (Step 3) and re-audit summaries (Step 10). For any file where **Score < 0.7**:

1. Re-spawn foundry:self-mentor on that file with the specific gap from the `Gaps:` field addressed in the prompt (e.g., "pay special attention to async error paths — previous pass flagged this as a gap")
2. If confidence is still < 0.7 after one retry: flag to user with ⚠ and include the gap in the final report — do not silently drop it
3. Recurring low-confidence gaps (same gap on same file across multiple audit runs) → candidate for adding to foundry:self-mentor's `\<antipatterns_to_flag>` or the agent's own instructions

```bash
# Parse confidence scores from foundry:self-mentor outputs (regex on task result text)
# Score: 0.82  → extract 0.82
# Flag any < 0.7 for targeted re-run
```

**Convergence loop**: if the re-audit surfaces new fixable findings at the active fix level, loop back to Step 8 with those findings. Repeat until:

- Zero fixable findings remain (convergence achieved) → mark fix pass complete, or
- Hard limit: **5 total fix passes** (including the initial Step 8 pass) — if still not converged, surface all remaining fixable findings to the user with a `⚠ CONVERGENCE LIMIT` warning explaining which issues resisted fixing.

Track pass count with a counter initialized to 1 at the first Step 8 execution. Increment before each re-entry into Step 8. Never suppress findings to make the counter appear clean.

The audit-fix sub-agent (when used) must also apply this loop internally — its prompt should instruct it to keep spawning fix agents and re-audit agents until clean or 5-pass limit reached.

**Cross-file re-validation**: after per-file re-audit completes for this pass, re-run the subset of system-wide checks (Step 4) that are sensitive to the files modified in this pass:

- Check 1 (inventory drift) — if any agent or skill file was modified
- Check 2 (README vs disk) — if any agent or skill was added, renamed, or deleted
- Check 14 (orphaned follow-up references) — if any skill file was modified
- Check 17 (cross-file content duplication) — if two or more files were modified
- Check 25 (implicit agent references) — if any agent or skill file was modified
- Check 27 (cross-plugin shared-file ref integrity) — if any skill file was modified

Write cross-file re-validation findings to `<RUN_DIR>/crossfile-revalidation-pass<N>.md` where N is the current pass count. Include any new findings in the convergence loop input for the next Step 8 iteration.

## Step 11: Final report

Output the complete audit summary: List each audited file by name in the `### Files Audited` section — names are drawn from the Step 2 inventory; counts alone are insufficient.

```
## Audit Complete — .claude/ config

### Files Audited
- **Agents** (N): name-1, name-2, ...
- **Skills** (N): name-1, name-2, ...
- **Rules** (N): name-1, name-2, ...
- **Hooks** (N): file-1.js, file-2.js, ...
- **Settings**: settings.json
- **Communication** (if in scope): communication.md, quality-gates.md, TEAM_PROTOCOL.md, file-handoff-protocol.md

### Findings
| Severity | Found | Fixed | Remaining |
|---|---|---|---|
| critical | N | N | 0 |
| high | N | N | 0 |
| medium | N | N | 0 |
| low | N | N (fix all only) | N |

**Fix convergence**: Converged in N pass(es) — 0 fixable findings remain.
```

Or if limit hit:

```
**Fix convergence**: ⚠ CONVERGENCE LIMIT reached (5 passes) — N fixable findings remain (see Remaining section).
```

(Omit the fix convergence line for report-only runs — only shown when a fix level was passed.)

### Fixes Applied

| File | Change |
| --- | --- |
| agents/foo.md | Replaced broken ref `old-agent` → `correct-agent` |

### Remaining (low/nits — auto-fixed only with 'fix all'; otherwise manual review optional)

- [low findings that were not auto-fixed]
- [any infinite loops flagged for user decision]

### Agent Confidence

| File | Score | Label | Gaps |
| --- | --- | --- | --- |
| agents/foo.md | 0.92 | high | — |
| skills/bar/SKILL.md | 0.64 | ⚠ low | no runtime data for bash validation |

Low-confidence files re-audited: N | Still uncertain after retry: N (see gaps above)

### Next Step

Run `/foundry:init` to propagate clean config to ~/.claude/

```

## Mode: upgrade

**Trigger**: `/audit upgrade`

Read and execute `plugins/foundry/skills/audit/modes/upgrade.md`.

</workflow>

<notes>

- **`!` Breaking findings**: when a skill or agent is completely non-functional (check #7, broken cross-refs, invalid hook events), prefix the finding with `!` and state the impact + fix in one place — don't bury it as a table row. These surface as **`! BREAKING`** in bash output and as prominent callouts in the final report.
- **Terminal color conventions** (used in Step 4 bash output):
  - `RED` (`\033[1;31m`) — breaking/critical: `! BREAKING`, `ERROR`
  - `YELLOW` (`\033[1;33m`) — warnings/medium: `⚠ MISSING`, `⚠ ORPHANED`, `⚠ DIFFERS`
  - `GREEN` (`\033[0;32m`) — pass status: `✓ OK`, `✓ IDENTICAL`
  - `CYAN` (`\033[0;36m`) — source agent name or fix hint
- **Report before fix**: never silently mutate files — always present the findings report first (Step 7), then fix
- **settings.json is hands-off**: missing permissions are always reported, never auto-edited — structural JSON edits risk breaking Claude Code's config loading
- **Dead loops need human judgment**: a cycle in follow-up chains might be intentional (e.g., refactor → review → fix → refactor) — flag and explain, don't auto-remove
- **Convergence loop replaces cycle cap**: the fix loop runs until zero fixable findings remain or the 5-pass hard limit is hit — see Step 10 for the full protocol
- **Relationship to self-mentor**: `foundry:self-mentor` is a single-file reactive audit; `/audit` is the system-wide sweep that runs foundry:self-mentor at scale and adds cross-file checks
- `general-purpose` is a built-in Claude Code agent type (no `.claude/agents/general-purpose.md` file needed); no custom system prompt, all tools available.
- **Paths must be portable**: `.claude/` for project-relative paths, `~/` or `$HOME/` for home paths — never a literal `/Users/<name>/` or `/home/<name>/` path (shown here as anti-examples only); this rule applies to ALL config files including `settings.json`
- **Bash error logging**: if a bash block in Pre-flight checks or Step 4 fails unexpectedly, append a JSONL line to `.claude/logs/audit-errors.jsonl` (`{"ts":"<ISO>","check":"<N>","error":"<message>"}`) for post-mortem — do not swallow errors silently.
- **Parallel execution rule**: After Step 2 (file collection), launch Steps 3 and 4 in the same response — all foundry:self-mentor agent spawns AND all system-wide bash checks must be issued together. Do NOT run Step 3 first and Step 4 second. Aggregation (Step 5) waits for both to complete. The docs-freshness web-explorer (within Step 4) also launches in that same parallel batch.
- **Token cost**: Step 3 (foundry:self-mentor spawns) is the most expensive part of the audit. For a quick structural scan where you mainly need cross-reference and inventory validation, the system-wide checks in Step 4 are often sufficient on their own. Consider running `/audit agents` or `/audit skills` to scope the sweep, or skip Step 3 entirely for a fast pass when you already trust per-file quality.
- **Skill-creator complement**: For testing whether skill trigger descriptions fire correctly (trigger accuracy, A/B description testing), see the official skill-creator utility from Anthropic. `/audit` checks structural quality; `skill-creator` validates that the right skill is selected by Claude Code's dispatcher when the user types a command.
- Follow-up chains:
  - Audit clean → `/foundry:init` to propagate verified config to `~/.claude/`
  - Audit found structural issues → review flagged files manually before syncing
  - Audit found many low items → run `/audit fix all` to auto-fix them, or run `/develop:refactor` for a targeted cleanup pass
  - After fixing agent instructions (from audit findings) → `/calibrate <agent>` to verify the fix improved recall and confidence calibration
  - Audit Check 20 found description overlap → `/calibrate routing` to verify behavioral routing impact; update descriptions for confused pairs based on the routing report
  - Audit surfaced upgrade proposals → `/audit upgrade` to apply with correctness checks and calibrate A/B evidence for capability changes
  - `/audit upgrade` reverted a capability change → run `/calibrate <agent> full` for deeper signal (N=10 vs N=3 used in upgrade mode)
  - Audit Check 22 found unregistered calibratable mode → update `calibrate/modes/skills.md` domain table and run `/calibrate skills` to verify the new target works
  - Audit Check 22 found stale domain table entry → remove it from `calibrate/modes/skills.md`

</notes>
```
