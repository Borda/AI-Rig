---
name: audit
description: Full-sweep quality audit of .claude/ config — cross-references, permissions, inventory drift, model tiers, docs freshness. Reports by severity; auto-fixes at the requested level — 'fix high' (critical+high), 'fix medium' (critical+high+medium), 'fix all' (all findings including low).
argument-hint: '[agents|skills] [fix [high|medium|all]]'
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
---

<objective>

Run a full-sweep quality audit of the `.claude/` configuration: every agent file, every skill file, settings.json, and hooks. Spawns `self-mentor` for per-file analysis, then aggregates findings system-wide to catch issues that only surface across files — infinite loops, inventory drift, missing permissions, and cross-file interoperability breaks. Reports all findings and auto-fixes at the requested level: `fix high` (critical+high only), `fix medium` (critical+high+medium, default fix level), or `fix all` (all findings including low).

</objective>

<inputs>

- **$ARGUMENTS**: optional
  - No argument: full sweep, report only — lists all findings, no changes made (default)
  - `fix high` — fix `critical` and `high` findings; `medium` and `low` reported only
  - `fix medium` — fix `critical`, `high`, and `medium` findings; `low` reported only
  - `fix all` — fix all findings including `low`
  - `fix` (no level) — alias for `fix medium` (backward compatible)
  - `agents` — restrict sweep to agent files only, report only
  - `skills` — restrict sweep to skill files only, report only
  - Scope and fix level can be combined: `agents fix medium`, `skills fix all` — scope always precedes `fix`

</inputs>

<workflow>

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase and mark status live so the user can see progress in real time:

- Phase 1: setup + collect (Pre-flight + Steps 1–2) → mark in_progress when starting, completed when file list is ready
- Phase 2: per-file audit (Step 3) → mark in_progress when agents launch, completed when all reports received
- Phase 3: system-wide checks (Step 4) → mark in_progress when checks start, completed when all checks done
- Phase 4: aggregate + fix (Steps 5–9) → mark in_progress, then completed when fixes land
- Phase 5: final report (Step 10) → mark in_progress, then completed before output
- On loop retry or scope change → create a new task; do not reuse the completed task

Surface progress to the user at natural milestones: after system-wide checks ("✓ Checks 1-9 complete, N findings so far — spawning per-file audits"), after agent reports ("Agent reports received — N medium, N low findings"), and before each fix batch ("Fixing N medium findings in parallel").

## Pre-flight checks

```bash
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'

# .claude/ directory must exist
if [ ! -d ".claude" ]; then
  printf "${RED}! BREAKING${NC}: .claude/ directory not found — nothing to audit\n"
  exit 1
fi

# jq availability — Check 6 depends on it
if ! command -v jq &>/dev/null; then
  printf "${YEL}⚠ MISSING${NC}: jq not found — Check 6 (permissions-guide drift) will be skipped\n"
  JQ_AVAILABLE=false
else
  JQ_AVAILABLE=true
fi

# git availability — used in path portability check and baseline context
if ! command -v git &>/dev/null; then
  printf "${YEL}⚠ MISSING${NC}: git not found — path portability check may miss repo-root references\n"
fi
```

If `.claude/` is missing, abort immediately. Missing `jq` is a warning — the audit continues with Check 6 skipped.

## Step 1: Run pre-commit (if configured)

```bash
# Check whether pre-commit is installed and a config exists
if command -v pre-commit &>/dev/null && [ -f .pre-commit-config.yaml ]; then
  pre-commit run --all-files
fi
```

Any files auto-corrected by pre-commit hooks (formatters, linters, whitespace fixers) are now clean before the structural audit begins. Note which files were modified — include them in the audit scope even if they were not originally targeted.

If pre-commit is not configured, skip this step silently.

## Step 2: Collect all config files

Enumerate everything in scope using built-in tools:

- **Agents**: Glob tool, pattern `agents/*.md`, path `.claude/`
- **Skills**: Glob tool, pattern `skills/*/SKILL.md`, path `.claude/`
- **Settings**: Read tool on `.claude/settings.json`
- **Hooks**: Glob tool, pattern `hooks/*`, path `.claude/`

Record the full file list — this becomes the audit scope for Steps 3–4. Cross-reference checks in Step 3 depend on this inventory being current. If MEMORY.md has not been updated since the last agent or skill was added or removed, run a live disk scan now rather than relying on the cached roster. Stale inventory is the primary cause of false-negative cross-reference findings.

## Step 3: Per-file audit via self-mentor

Spawn one **self-mentor** agent per file (or batch into groups of up to 10 for efficiency). Each invocation prompt must end with:

> "End your response with a `## Confidence` block per CLAUDE.md output standards."

Read the self-mentor prompt template from ${CLAUDE_SKILL_DIR}/templates/self-mentor-prompt.md and include its content in each self-mentor invocation prompt.

Collect all findings from each self-mentor response into a structured list keyed by file path.

## Step 4: System-wide checks

Beyond per-file analysis, run cross-file checks that self-mentor cannot do alone:

Run the following checks. For file-listing steps use Glob; for content-search steps use Grep. Bash is only needed for the pipeline comparisons and the `printf`/`jq` blocks below.

```bash
# Redeclare color helpers per Bash tool call — shell env does not persist across calls; CYN (agent name/fix hint) is added here and absent from the pre-flight block.
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; CYN='\033[0;36m'; NC='\033[0m'
# RED → breaking / critical  |  YELLOW → warning / medium  |  GREEN → pass  |  CYAN → agent name / source
```

**Check 1 — Inventory drift (MEMORY.md vs disk)**
Use Glob (`agents/*.md`, path `.claude/`) to list agent files; extract basenames and sort, then write to `/tmp/agents_disk.txt` via Bash:

```bash
ls .claude/agents/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.md$//' | sort > /tmp/agents_disk.txt || true
```

Read the `- Agents:` and `- Skills:` roster lines from the MEMORY.md content injected in the conversation context (available as auto-memory at session start). Do not attempt to Grep a file path — the MEMORY.md is not stored under `.claude/` but in Claude Code's auto-memory system. Repeat with Glob (`skills/*/`, path `.claude/`) for skills on disk — write to `/tmp/skills_disk.txt`.

**Check 2 — README vs disk**
Use Grep tool (pattern `^\| \*\*`, file `README.md`, output mode `content`) to extract agent/skill table rows.

**Check 3 — settings.json permissions**
Use Grep tool (pattern `gh |python -m|ruff|mypy|pytest`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to collect bash commands used in skills.

**Check 4 — Orphaned follow-up references**
Use Grep tool (pattern `` `/[a-z-]*` ``, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to find skill-name references; compare against disk inventory.

**Check 5 — Hardcoded user paths**
Use Grep tool (pattern `/Users/|/home/`, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`) to flag non-portable paths.

**Important**: run this check on every file regardless of whether critical or high findings were already found — path portability issues are orthogonal to other severity classes and must not be deprioritized due to presence of more serious findings in the same file.

**Check 6 — permissions-guide.md drift** — every allow entry must appear in the guide, and vice versa

```bash
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then
  printf "${YEL}⚠ SKIPPED${NC}: Check 6 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
  printf "${YEL}⚠ SKIPPED${NC}: Check 6 — .claude/settings.json not found\n"
elif [ ! -f ".claude/permissions-guide.md" ]; then
  printf "${YEL}⚠ SKIPPED${NC}: Check 6 — .claude/permissions-guide.md not found\n"
else
  # Allow entries missing from guide
  jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | \
  while IFS= read -r perm; do
    grep -qF "\`$perm\`" .claude/permissions-guide.md 2>/dev/null \
      || printf "${YEL}⚠ MISSING from guide${NC}: %s\n" "$perm"
  done

  # Guide entries orphaned (not in allow list)
  grep '^| `' .claude/permissions-guide.md 2>/dev/null | awk -F'`' '{print $2}' | \
  while IFS= read -r perm; do
    jq -e --arg p "$perm" '(.permissions.allow // []) + (.permissions.deny // []) | contains([$p])' .claude/settings.json > /dev/null 2>&1 \
      || printf "${YEL}⚠ ORPHANED in guide${NC}: %s\n" "$perm"
  done
fi
```

**Check 7 — Skill frontmatter conflicts** — `context:fork + disable-model-invocation:true` is a broken combination: a forked skill has no model to coordinate agents or synthesize results.

```bash
for f in .claude/skills/*/SKILL.md; do
  name=$(basename "$(dirname "$f")")
  if awk '/^---$/{c++} c<2' "$f" 2>/dev/null | grep -q 'context: fork' && \
     awk '/^---$/{c++} c<2' "$f" 2>/dev/null | grep -q 'disable-model-invocation: true'; then
    printf "${RED}! BREAKING${NC} skills/%s: context:fork + disable-model-invocation:true\n" "$name"
    printf "  ${RED}→${NC} forked skill has no model to coordinate agents or synthesize results\n"
    printf "  ${CYN}fix${NC}: remove disable-model-invocation:true (or remove context:fork if purely tool-only)\n"
  fi
done
```

Flag any drift between MEMORY.md, README.md, settings.json, and actual disk state. Flag any hardcoded `/Users/` or `/home/` paths — these should be `.claude/`, `~/`, or `$(git rev-parse --show-toplevel)/` style. Flag any permissions-guide.md entries not in the allow list (orphaned docs) or allow entries without a guide row (undocumented permissions).

**Check 8 — Model tier appropriateness**

Three capability tiers define the expected model assignment for each agent:

| Tier                | Profile                                                     | Current mapping     |
| ------------------- | ----------------------------------------------------------- | ------------------- |
| `plan-gated`        | Long-horizon reasoning, plan mode, governance               | `opusplan`          |
| `deep-reasoning`    | Complex implementation, multi-file code gen, judgment calls | `opus`              |
| `focused-execution` | Pattern matching, structured output, rule application       | `sonnet` or `haiku` |

Extract declared models with Bash:

```bash
printf "%-30s %s\n" "AGENT" "MODEL"
for f in .claude/agents/*.md; do
  name=$(basename "$f" .md)
  model=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^model:/{sub(/^model: /,""); print}' "$f")
  printf "%-30s %s\n" "$name" "${model:-(inherit)}"
done
```

Using model reasoning, classify each agent into a tier based on its `<role>`, `description`, and `<workflow>` content. Cross-reference the classified tier against the declared model:

- `focused-execution` agent using `opus` or `opusplan` → **medium** (potential overkill — may increase latency and cost without quality gain)
- `deep-reasoning` agent using `sonnet` → **high** (likely underpowered for multi-file code gen or complex judgment)
- `plan-gated` agent using `sonnet` → **high** (plan mode requires strong long-horizon reasoning)
- `focused-execution` agent using `haiku` → **not a finding** — haiku is acceptable and economical for narrow/rule-based tasks

**Important**: CLAUDE.md's `## Agent Teams` section specifies models for team-mode spawn instructions to the lead — it is NOT a mandate for agent frontmatter. Frontmatter `model:` governs standalone use. Do NOT flag frontmatter models as violations because they differ from CLAUDE.md's team-mode model spec.

**Report only** — never auto-fix. Model assignments may be intentional trade-offs (e.g., cost sensitivity, latency constraints). Flag mismatches with rationale so the user can decide.

**Time-resilience note**: when new model tiers or model aliases arrive, update only the tier-to-model mapping table above. The tier classification heuristic (what each agent does) is model-agnostic.

### Tool efficiency

For each agent and skill, validate that declared tools match actual usage — no unnecessary permissions, no missing tools.

**Mechanical check** — for each skill, cross-reference `allowed-tools:` frontmatter against tool names referenced in the workflow body:

```bash
for f in .claude/skills/*/SKILL.md; do
  name=$(basename "$(dirname "$f")")
  declared=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^allowed-tools:/{sub(/^allowed-tools: /,""); print}' "$f")
  body=$(awk '/^---$/{c++} c>=2{print}' "$f")
  for tool in Read Write Edit Bash Grep Glob TaskCreate TaskUpdate WebFetch WebSearch; do
    in_body=$(echo "$body" | grep -cw "$tool" || true)
    in_decl=$(echo "$declared" | grep -cw "$tool" || true)
    if [ "$in_body" -gt 0 ] && [ "$in_decl" -eq 0 ]; then
      printf "${YEL}⚠ MISSING tool${NC}: skills/%s references %s but not in allowed-tools\n" "$name" "$tool"
    fi
    if [ "$in_body" -eq 0 ] && [ "$in_decl" -gt 0 ]; then
      printf "${YEL}⚠ UNUSED tool${NC}:  skills/%s declares %s but workflow never references it\n" "$name" "$tool"
    fi
  done
done
```

**Semantic check** (model reasoning) — review each agent's `tools:` frontmatter against its declared domain and workflow:

- `WebFetch`/`WebSearch` declared for an agent whose domain has no web-research component → **medium** (unnecessary permission surface)
- `Write`/`Edit` declared for a read-only agent (e.g., `solution-architect`) but not used in practice → **medium**
- `Bash` absent for an agent whose domain involves running code (linting, Continuous Integration (CI) validation, performance profiling) → **high** (silent failure when workflow invokes shell commands)
- `Task` absent for an orchestrating agent that needs to spawn subagents → **high**
- `tools:` is `*` (wildcard) for a focused domain agent — prefer an explicit list → **low**

Report missing necessary tools as **high**; declared-but-unused tools as **medium**.

### Purpose overlap review

Read all agent/skill descriptions together and flag pairs where:

- Two agents have substantially overlapping domains (risk: users don't know which to pick)
- A skill's workflow duplicates logic already owned by an agent it could simply spawn
- An agent has grown so broad its scope is unclear (candidate for splitting)

### CLAUDE.md consistency

`.claude/CLAUDE.md` is the master governance file; agent and skill instructions must not contradict it.

Read `.claude/CLAUDE.md` and extract its governance directives (Workflow Orchestration, Task Management, Self-Setup Maintenance, Communication, Core Principles). For each agent and skill file, check whether any instruction contradicts or undermines a CLAUDE.md directive:

- **Direct contradiction**: file says the opposite of what CLAUDE.md mandates (e.g., "skip planning" vs "enter plan mode for non-trivial tasks")
- **Missing required behavior**: file performs an action governed by Self-Setup Maintenance rules but omits the required steps (e.g., modifies `.claude/` files without mentioning cross-reference updates)
- **Tone/style mismatch**: file's communication guidance conflicts with the Communication section (e.g., "apologize to the user" vs "flag early, not late")

Major contradictions → **high** severity, raised to user (CLAUDE.md takes precedence — the agent/skill needs updating, but the user decides how).
Minor drift (slightly different wording of the same idea, or missing but not contradicting) → **low**.

- **Direct contradiction** includes explicit instructions to skip a required behavior (e.g., "Do not create task tracking entries" contradicts the Task Management directive as directly as saying "never track tasks"). The test is whether a reasonable reader would interpret the instruction as overriding the CLAUDE.md mandate — if yes, it is high; if the file simply does not mention the behavior, it is low (omission).

### Claude Code docs freshness

Spawn a **web-explorer** agent to fetch the current Claude Code documentation. Try the direct paths below; if they don't resolve, navigate from the Claude Code homepage (`code.claude.com`) to find the current schema pages:

- Hook event names, types, and schemas — `code.claude.com/docs/en/hooks`
- Agent frontmatter schema — `code.claude.com/docs/en/sub-agents`
- Skill frontmatter schema — `code.claude.com/docs/en/skills`

With the fetched docs, validate the local config:

**Hook validation** (`settings.json`):

- Every hook event name (e.g. `SubagentStart`) exists in the documented event list
- Every hook `type` is one of `command`, `http`, `prompt`, `agent`
- No deprecated top-level `decision:`/`reason:` fields in PreToolUse hooks
  (correct form is `hookSpecificOutput.permissionDecision`)

**Agent frontmatter validation** (`.claude/agents/*.md`):

- All frontmatter fields are in the documented schema
  (`name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`,
  `maxTurns`, `skills`, `mcpServers`, `hooks`, `memory`, `background`, `isolation`,
  `color` — note: `color` is a Claude Code UI extension not in the public schema)
- `model` values are recognized short-names (`sonnet`, `opus`, `haiku`, `inherit`,
  or project-level aliases like `opusplan`)

**Skill frontmatter validation** (`.claude/skills/*/SKILL.md`):

- All frontmatter fields are in the documented schema
  (`name`, `description`, `argument-hint`, `disable-model-invocation`,
  `user-invocable`, `allowed-tools`, `model`, `context`, `agent`, `hooks`)

**Improvement opportunities** — collect documented features not yet in use:

- New hook events that could add value (e.g. `PreCompact`, `SessionEnd`, `Stop`)
- New agent frontmatter fields (e.g. `memory`, `isolation`, `maxTurns`, `background`)
- New skill frontmatter fields (e.g. `context: fork`, `model`, `hooks`)
- New settings keys (e.g. `sandbox`, `plansDirectory`, `alwaysThinkingEnabled`)

Findings classification:

- Deprecated/invalid hook event name or type in use → **high**
- Deprecated frontmatter field, deprecated settings key, unrecognized model ID → **medium**
- New documented feature not yet used → **low** (prefix with 💡)

**Check 9 — Example value vs. token cost**

First, detect whether the project has local context files that reduce the need for generic examples:

```bash
for f in AGENTS.md CONTRIBUTING.md .claude/CLAUDE.md; do
  [ -f "$f" ] && printf "✓ found: %s\n" "$f"
done
```

Then scan agent and skill files for inline examples:

````bash
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do
  count=$(grep -cE '^```|^## Example|^### Example' "$f" 2>/dev/null || true)
  lines=$(wc -l < "$f" | tr -d ' ')
  [ "$count" -gt 0 ] && printf "%s: %d example blocks, %d total lines\n" "$f" "$count" "$lines"
done
````

Using model reasoning, evaluate each file's examples against two criteria:

1. **Necessity**: does the example demonstrate something that prose alone cannot — a nuanced judgment call, a non-obvious output format, a complex multi-step pattern? Or does it just restate the surrounding paragraph in code?
2. **Project fit**: if the project has `AGENTS.md` or `CONTRIBUTING.md`, project-specific examples in agent files compete with or duplicate that local context — flag as low-value unless the example is domain-specific to the agent's specialty.

Classify each example block:

- **High-value**: non-obvious pattern, nuanced judgment, or output-format spec that prose cannot convey → keep
- **Low-value**: restates prose, trivial, or superseded by project-local docs → **low** finding: suggest removing or replacing with a pointer to the local doc

Report per-file: `N examples total, K high-value, M low-value (est. ~X tokens wasted)`.

## Step 5: Aggregate and classify findings

**Antipatterns that indicate severity under-classification** (common failure modes from calibration):

- Classifying `context:fork + disable-model-invocation:true` as "possibly contradictory" (low) rather than **critical** — this combination is always breaking; no conditional language
- Classifying inventory drift (MEMORY.md vs disk) as "out of sync" (medium) rather than **critical** — any cross-reference using the stale roster will fail at runtime
- Classifying a `deep-reasoning` agent on `sonnet` as "possibly underpowered" (medium) rather than **high** — the tier classification table provides the authority for this judgment; use it
- Classifying a direct CLAUDE.md contradiction as a "best practice concern" (medium) rather than **high** — any instruction that explicitly overrides a CLAUDE.md directive is high severity per the governance hierarchy

Group all findings from Steps 1–4 into a severity table. Read the severity classification table from ${CLAUDE_SKILL_DIR}/severity-table.md.

## Step 6: Cross-validate critical findings

Read and follow the cross-validation protocol from `.claude/skills/_shared/cross-validation-protocol.md`.

**Skill-specific**: the verifier agent is always **self-mentor**.

## Step 7: Report findings

Output a structured audit report before fixing anything:

```
## Audit Report — .claude/ config

### Scope
- Agents audited: N
- Skills audited: N
- System-wide checks: inventory drift, README sync, permissions, infinite loops, hardcoded paths, CLAUDE.md consistency, docs freshness, permissions-guide drift, model tier appropriateness

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
```

If no fix level was passed, stop here and present the report.

## Step 8: Delegate fixes to subagents

Spawn one **sw-engineer** subagent per affected file, batching all findings for that file into a single subagent prompt. Issue **all spawns in a single response** for parallelism.

Each subagent prompt template: Read the fix prompt template from ${CLAUDE_SKILL_DIR}/templates/fix-prompt.md and use it, filling in `<file path>` and the list of findings.

**Exceptions — handle inline without subagents (note in report):**

- **settings.json permission missing**: report only — structural JSON edits are risky to delegate
- **CLAUDE.md contradiction**: raise to user — do not auto-fix (CLAUDE.md takes precedence)
- **Dead loop**: flag for user review — requires human judgment on which link to break
- **Model tier mismatch**: report only — model assignments may be intentional for cost/latency trade-offs; user decides whether to adjust

After all subagents complete, collect their results and proceed to Step 9.

**Low findings** (nits): fix only when `fix all` was passed — otherwise collect in the final report for optional manual cleanup.

## Step 9: Re-audit modified files + confidence check

For every file changed in Step 8, spawn **self-mentor** again to confirm:

- The fix resolved the finding
- No new issues were introduced by the edit

```bash
# Spot-check: confirm the previously broken reference no longer appears
grep -n "<broken-name>" <fixed-file>
```

**Confidence re-run**: after collecting all self-mentor responses from Steps 3 and 9, parse each confidence score. For any file where **Score < 0.7**:

1. Re-spawn self-mentor on that file with the specific gap from the `Gaps:` field addressed in the prompt (e.g., "pay special attention to async error paths — previous pass flagged this as a gap")
2. If confidence is still < 0.7 after one retry: flag to user with ⚠ and include the gap in the final report — do not silently drop it
3. Recurring low-confidence gaps (same gap on same file across multiple audit runs) → candidate for adding to self-mentor's `\<antipatterns_to_flag>` or the agent's own instructions

```bash
# Parse confidence scores from self-mentor outputs (regex on task result text)
# Score: 0.82  → extract 0.82
# Flag any < 0.7 for targeted re-run
```

If re-audit surfaces new issues, loop back to Step 8 for those findings only (max 2 re-audit cycles — escalate to user if still unresolved).

## Step 10: Final report

Output the complete audit summary:

```
## Audit Complete — .claude/ config

### Files Audited
- Agents: N | Skills: N | Settings: 1 | Hooks: N

### Findings
| Severity | Found | Fixed | Remaining |
|---|---|---|---|
| critical | N | N | 0 |
| high | N | N | 0 |
| medium | N | N | 0 |
| low | N | N (fix all only) | N |

### Fixes Applied
| File | Change |
|---|---|
| agents/foo.md | Replaced broken ref `old-agent` → `correct-agent` |

### Remaining (low/nits — auto-fixed only with 'fix all'; otherwise manual review optional)
- [low findings that were not auto-fixed]
- [any infinite loops flagged for user decision]

### Agent Confidence
| File | Score | Label | Gaps |
|------|-------|-------|------|
| agents/foo.md | 0.92 | high | — |
| skills/bar/SKILL.md | 0.64 | ⚠ low | no runtime data for bash validation |

Low-confidence files re-audited: N | Still uncertain after retry: N (see gaps above)

### Next Step
Run `/sync apply` to propagate clean config to ~/.claude/
```

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
- **Max 2 re-audit cycles**: if fixes don't converge after 2 loops, surface the remaining issues to the user rather than spinning
- **Relationship to self-mentor**: `self-mentor` is a single-file reactive audit; `/audit` is the system-wide sweep that runs self-mentor at scale and adds cross-file checks
- **Paths must be portable**: `.claude/` for project-relative paths, `~/` for home paths — never `/Users/<name>/` or `/home/<name>/`; this rule applies to ALL skill and agent files
- Pre-flight for `/sync` — run clean before `/sync apply`.
- **Bash error logging**: if a bash block in Pre-flight checks or Step 4 fails unexpectedly, append a JSONL line to `.claude/logs/audit-errors.jsonl` (`{"ts":"<ISO>","check":"<N>","error":"<message>"}`) for post-mortem — do not swallow errors silently.
- **Execution order tip**: Steps 1–2 and Step 4 bash checks are fast (seconds); Step 3 (self-mentor spawns) is expensive (seconds per file). For early signal on system-wide issues, run Steps 1–2 + Step 4 first, then spawn Step 3 agents in parallel with any Step 4 analysis that doesn't depend on per-file results.
- **Token cost**: Step 3 (self-mentor spawns) is the most expensive part of the audit. For a quick structural scan where you mainly need cross-reference and inventory validation, the system-wide checks in Step 4 are often sufficient on their own. Consider running `/audit agents` or `/audit skills` to scope the sweep, or skip Step 3 entirely for a fast pass when you already trust per-file quality.
- **Skill-creator complement**: for testing whether skill trigger descriptions fire correctly (trigger accuracy, A/B description testing), see the official `skill-creator` from `github.com/anthropics/skills` <!-- verify at use time -->. `/audit` checks structural quality; `skill-creator` validates that the right skill is selected by Claude Code's dispatcher when the user types a command.
- Follow-up chains:
  - Audit clean → `/sync apply` to propagate verified config to `~/.claude/`
  - Audit found structural issues → review flagged files manually before syncing
  - Audit found many low items → run `/audit fix all` to auto-fix them, or run `/refactor` for a targeted cleanup pass
  - After fixing agent instructions (from audit findings) → `/calibrate <agent>` to verify the fix improved recall and confidence calibration

</notes>
