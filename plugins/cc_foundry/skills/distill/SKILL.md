---
name: distill
description: One-time snapshot extracting patterns from work history and accumulated lessons, distills into concrete improvements — new agent/skill suggestions, memory pruning, consolidating lessons into rules/agent updates, or performing bin/ extraction from /audit --efficiency candidates. Roster boundary analysis → /foundry:audit agents (Check 34).
argument-hint: '[prune | memory | executables [<run-dir-or-report-path>] | "external <url-or-path>" | "<recurring task description>"] [--project] [--eager] [--keep "<items>"]'
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Glob, Grep, Write, AskUserQuestion, Agent, WebFetch, TaskCreate, TaskUpdate, TaskList
effort: low
---

<objective>

Analyze how Claude Code is used and surface concrete improvements — new agents/skills to reduce repetition, or consolidate lessons into governance files (rules, agent instructions, skill updates) — without duplicating what exists.

NOT for single-file edits or quality checks — use `/foundry:audit` for config quality checks. NOT for audit-only scan for extraction candidates (use `/foundry:audit --efficiency` instead of `distill executables` for detection-only).

</objective>

<inputs>

- **$ARGUMENTS**: optional. Modes:
  - Omitted — analyze existing patterns and agents; generate suggestions proactively.
  - `prune [--eager]` — evaluate project memory file for stale, redundant, or verbose entries. Default: advisory diff + apply prompt. `--eager`: score every entry (Usage likelihood × Impact → Tier P0/P1/P2), print full scored table with `#` column, let user select by tier or item numbers, delegate edits to `foundry:curator`.
  - `memory [--eager]` — read `.notes/lessons.md` and memory feedback files, distill recurring patterns into proposed rule files, agent instruction updates, and skill workflow changes. `--eager`: include Pattern count, Strength, and Tier columns in proposal table; let user select clusters to promote by tier or item numbers; delegate writes to `foundry:curator`.
  - `external <source> [--eager]` — analyse external plugin, skill, or agentic resource and produce structured adoption proposal. `<source>` is URL, file path, or local directory. `--eager`: lower adoption bar — recommend partial adoption even for single useful components.
  - `executables [--eager] [<run-dir-or-report-path>]` — perform bin/ extraction from `/foundry:audit --efficiency` Check 33 candidates. Auto-detects latest run dir under `.reports/audit/`; pass optional path to target a specific run dir or report file. Runs inline Check 33 scan when no report exists. Default gates on HIGH/MEDIUM verdict. `--eager`: also surface LOW verdict clusters as extraction candidates. Spawns `foundry:sw-engineer` per cluster. Skip to **Mode: Executables Extraction** below.
  - `[--eager] <recurring task description>` — use description as context when generating suggestions. `--eager`: lower frequency threshold from 3+ to 2+ occurrences; single high-effort occurrence also qualifies.
  - `--project` — in `prune` and `memory` modes, show an interactive project picker: enumerate all slugs under `~/.claude/projects/*/memory/` with MEMORY.md size in tokens, then let user select which project(s) to operate on. Omit to operate across **all** projects automatically. Has no effect on other modes.

</inputs>

<compaction>
- Key boundary 1: end of Step 2 frequency heuristics (default mode only — prune/memory/external/executables exit early), before Step 3 gap analysis.
- Preserve at boundary 1: EAGER flag, ARGUMENTS (stripped), no run-dir (default mode is stateless).
- Terminal paths: end of Step 5 report (default mode); end of Memory Pruning mode (both eager and standard branches).
</compaction>

<workflow>

**Task hygiene**: load and follow the protocol below.

```bash
# loads: compaction-contract.md
# audit-skip: resilience-replication
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_shared_doc.py" foundry skills/_shared task-hygiene.md  # timeout: 5000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
KEEP_ITEMS=""
if [[ "$ARGUMENTS" =~ --keep[[:space:]]\"([^\"]+)\" ]]; then
    KEEP_ITEMS="${BASH_REMATCH[1]}"
fi
ARGUMENTS=$(echo "$ARGUMENTS" | sed 's/--keep "[^"]*"//g')
rm -f .temp/state/skill-contract.md  # clear stale contract (compaction-contract.md §Lifecycle)  # timeout: 5000
mkdir -p "${TMPDIR:-/tmp}/distill-state-${CSID}"
echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/distill-state-${CSID}/keep-items"
EAGER=false
[[ "$ARGUMENTS" == *"--eager"* ]] && EAGER=true
ARGUMENTS=$(echo "$ARGUMENTS" | sed 's/--eager//g' | xargs)  # timeout: 3000
echo "EAGER=$EAGER"  # shell vars don't persist across Bash calls — read from stdout
echo "ARGUMENTS_STRIPPED=$ARGUMENTS"
```

> **Note**: `EAGER` and stripped `ARGUMENTS` are set by this Bash block, but shell variable state does **not** persist across separate Bash() tool calls. After this block runs, read its stdout (`EAGER=true/false`, `ARGUMENTS_STRIPPED=...`) and carry those values as model-context references for all subsequent mode dispatch and threshold decisions. Do not rely on `$EAGER` as a live shell variable in later steps — substitute the literal boolean value read from stdout.

```bash
PROJECT_FLAG=false
if echo "$ARGUMENTS" | grep -qE -- "--project"; then
    PROJECT_FLAG=true
    ARGUMENTS=$(echo "$ARGUMENTS" | sed 's/--project//' | xargs)
fi
echo "PROJECT_FLAG=$PROJECT_FLAG"
echo "ARGUMENTS_FINAL=$ARGUMENTS"
```

> **Note**: `PROJECT_FLAG` does not persist across Bash calls. Read its value from the stdout line `PROJECT_FLAG=true/false` and carry as model-context reference. When `true`, the mode must run the interactive picker before operating.

## Step 1: Inventory existing agents and skills

Use Glob tool to enumerate agents and skills across all sources — project-local AND plugin-namespaced — to avoid false-gap findings when candidate already exists in plugin:

- **Project-local**: pattern `agents/*.md`, path `.claude/`; pattern `skills/*/SKILL.md`, path `.claude/`
- **Plugin source** (workspace): pattern `*/agents/*.md`, path `plugins/`; pattern `*/skills/*/SKILL.md`, path `plugins/`
- **Installed plugin cache** (if accessible): resolve cache root — `PLUGIN_CACHE="${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}"` — then use Glob tool on `$PLUGIN_CACHE` for pattern `*/agents/*.md` and `*/skills/*/SKILL.md`

For each agent/skill found, extract: name, description, tools, purpose. Tag each entry with plugin namespace (e.g. `foundry:sw-engineer`, `oss:resolve`) — used in Step 3 gap analysis to prevent recommending duplicates of plugin-namespaced agents/skills.

## Step 2: Analyze work patterns

> **Mode-token normalization** — all mode dispatches below compare against the **first whitespace-delimited token** of the stripped `ARGUMENTS` (after `--eager` removal). Use this single rule consistently; do not rely on exact equality of the full `$ARGUMENTS` string, since trailing flags/spaces from prior parsing may differ.

**If first token equals `executables`** (i.e. `executables` alone or `executables <path>`, NOT a path or word that merely starts with the string `executables`): skip Steps 2–5 entirely and go to "Mode: Executables Extraction" below.

**If first token equals `prune`**: skip Steps 2–5 entirely and go to "Mode: Memory Pruning" below.

**If first token equals `memory`**: skip Steps 2–5 entirely and go to "Mode: Memory Distillation" below.

**If first token equals `external`** (i.e. `external <source>`, NOT a word that merely starts with the string `external`): skip Steps 2–5 entirely and go to "Mode: External Distillation" below.

Otherwise, look for signals of repetitive or specialist work. First three git commands are independent — run in parallel:

```bash
# timeout: 3000
# --- run these three in parallel ---
git log --oneline -50

git log --name-only --pretty="" -30 | sort | uniq -c | sort -rn | head -20

git log --oneline -100 | cut -d' ' -f2 | sort | uniq -c | sort -rn | head -15
```

Then use Glob tool (pattern `todo_*.md`, path `.plans/active/`) to list active task files; read each with Read tool. Also read `.notes/lessons.md` (if exists) for task history and conversation hints.

If `$ARGUMENTS` provided, use as additional context for pattern analysis.

### Frequency Heuristics

- **3+ occurrences** of pattern in recent history → candidate for automation
- **2+ different projects** using same manual process → cross-project skill
- **significant manual effort** per occurrence (subjective — use git history context) → high-value automation target
- **Domain-specific knowledge** required → candidate for specialist agent (not just skill)

With `--eager` (lower thresholds):

- **2+ occurrences** → candidate for automation
- **1 occurrence** with significant manual effort → qualifies as high-value candidate
- Domain-specific threshold unchanged

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/distill-state-${CSID}/keep-items" 2>/dev/null || _KEEP=""
_PRESERVE="run-dir=n/a"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:distill" "gap-analysis (after work-pattern scan)" "n/a" "$_PRESERVE" "gap analysis (Step 3) → duplication check (Step 4) → report (Step 5)"  # timeout: 5000
```

## Step 3: Gap analysis

For each identified pattern, check:

1. **Already covered?** — search existing agent/skill descriptions for overlap
2. **Frequent enough?** — recurring ≥ 3 times or clearly domain-specialized (See Step 2 heuristics — combine ≥3 occurrences with effort/frequency signals from Steps 1–2)
3. **Would specialist add quality?** — does it require deep domain knowledge?
4. **Too narrow?** — single-use task doesn't warrant persistent agent

Thresholds for recommendation:

- **New agent**: recurring specialist role, complex decision-making, 5+ distinct capabilities
- **New skill**: workflow orchestration, multi-step process with fixed structure
- **No new file needed**: one-off or already covered by existing agent

## Step 4: Check for duplication

Before recommending anything, run overlap check and anti-pattern checklist:

```markdown
For each candidate agent/skill:
- Does any existing agent cover >50% of its scope? → enhance existing instead
  (with --eager: lower to >30%; any shared single named capability → flag as boundary issue)
- Is the name/description confusingly similar to an existing one? → rename existing
```

Anti-pattern checklist — reject candidate if any apply:

1. **Role vs task confusion**: agents are roles, not tasks. Do not create agent for every different topic.
2. **Near-duplicate**: candidate duplicates existing agent with slightly different name. Enhance existing instead.
3. **Thin wrapper**: candidate skill just calls one agent with fixed args. Not enough value to justify new skill file. Exception: skills that add measure-first/measure-after bookends, multi-mode dispatch across 3+ agents, or safety breaks (retry limits, validation gates) justify wrapper even if only one agent executes for given invocation.

## Step 5: Report

```markdown
## Agent/Skill Suggestions

### Existing Coverage (no gaps found)
- [agent/skill]: covers [pattern] well — no new file needed

### Recommend: New Agent — [name]
**Trigger**: [what recurring pattern or gap justifies this]
**Gap**: [what existing agents don't cover]
**Scope**: [what it would do — 3-5 bullet points]
**Suggested tools**: [Read, Write, Edit, Bash, etc.]
**Draft description**: "[one-line description for frontmatter]"

### Recommend: New Skill — [name]
**Trigger**: [what repetitive workflow justifies this]
**Gap**: [why existing skills don't cover it]
**Scope**: [what workflow steps it would orchestrate]
**Draft description**: "[one-line description for frontmatter]"

### Recommend: Enhance Existing — [agent/skill name]
**Add**: [specific capability missing from current version]
**Why**: [what recurring task would benefit]

### No Action Needed
[pattern]: already handled by [existing agent/skill]

## Confidence
**Score**: [0.N]
**Gaps**: [e.g., git history too shallow, task files not present, descriptions too generic to compare]

**Refinements**: N passes. [Pass 1: <what improved>. Pass 2: <what improved>.] — omit if 0 passes
```

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

## Mode: Memory Pruning — only when `$ARGUMENTS == "prune"`

```bash
DISTILL_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" distill modes 2>/dev/null || echo "plugins/cc_foundry/skills/distill/modes")  # timeout: 5000
cat "$DISTILL_MODES/prune.md"
```

Execute the mode loaded above.

## Mode: Memory Distillation — only when first token is `memory`

```bash
DISTILL_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" distill modes 2>/dev/null || echo "plugins/cc_foundry/skills/distill/modes")  # timeout: 5000
cat "$DISTILL_MODES/memory.md"
```

Execute the mode loaded above.

## Mode: External Distillation — only when `$ARGUMENTS` begins with `external`

```bash
DISTILL_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" distill modes 2>/dev/null || echo "plugins/cc_foundry/skills/distill/modes")  # timeout: 5000
cat "$DISTILL_MODES/external.md"
```

Execute the mode loaded above.

## Mode: Executables Extraction — only when `$ARGUMENTS` begins with `executables`

```bash
DISTILL_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" distill modes 2>/dev/null || echo "plugins/cc_foundry/skills/distill/modes")  # timeout: 5000
cat "$DISTILL_MODES/executables.md"
```

Execute the mode loaded above.

</workflow>

<notes>

- Skill is introspective: looks at tooling itself, not just code

- Invoke periodically (e.g., monthly) or after burst of correction/feedback; one-time snapshot, not continuous monitor

- Suggestions are proposals — review before creating new files

- After creating new agent/skill from suggestion, re-run skill once to confirm gap resolved, then stop

- **`memory` mode is primary consolidation path** — run after any session with significant corrections to prevent lesson drift into MEMORY.md noise

- **Agent Teams signal tracking**: when reviewing patterns, also look for:

  - Skills using `--team` or team-mode heuristics more/less than expected → flag over/under-use relative to decision matrix in `CLAUDE.md § Agent Teams`
  - Security findings in reviews for non-auth code → foundry:qa-specialist teammate scope too broad; narrow it
  - Model tier mismatches (e.g., heavy analysis assigned to `sonnet` teammates) → flag for tier adjustment

- **`external` mode calibration**: two concrete GT fixture cases defined in calibrate skills mode file — find via `find "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}" -maxdepth 5 -path "*/calibrate/modes/skills.md" 2>/dev/null | head -1` with fallback to `plugins/cc_foundry/skills/calibrate/modes/skills.md`:

  - **caveman plugin** — narrow, self-contained communication mode, no local structural overlap → GT: install-as-is recommended, Group A empty or thin
  - **Karpathy autoresearch** — research automation tool, strong overlap with `research:` plugin structure → GT: Group A candidates map to research plugin, digest recommended, install-as-is not triggered
  - Ground truth = static snapshot of each tool's agent/skill/rule files (no live fetch needed); score adoption-table lane assignments against GT outcomes

- Follow-up chains:

  - Suggestion accepted for new agent/skill → `/foundry:manage create` to scaffold and register it
  - Suggestion to enhance existing → edit agent/skill directly, then `/foundry:setup`
  - `memory` proposals applied → `/foundry:setup` to propagate; `/foundry:audit rules` to verify new rule files structurally sound
  - `executables` extraction complete → `/foundry:setup` to propagate bin/ scripts; run `/foundry:audit --efficiency` to confirm `clusters == 0`

</notes>
