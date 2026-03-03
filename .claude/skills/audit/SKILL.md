---
name: audit
description: Comprehensive config audit for the entire .claude/ directory. Orchestrates self-mentor across all agents, skills, settings, and hooks to detect correctness issues, broken cross-references, interoperability problems, infinite loops, redundancy, and inefficiency. Reports findings by severity and auto-fixes critical, high, and medium findings; CLAUDE.md contradictions and missing permissions are always reported but never auto-fixed.
argument-hint: '[agents|skills] [fix]'
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task
---

<objective>

Run a full-sweep quality audit of the `.claude/` configuration: every agent file, every skill file, settings.json, and hooks. Spawns `self-mentor` for per-file analysis, then aggregates findings system-wide to catch issues that only surface across files — infinite loops, inventory drift, missing permissions, and cross-file interoperability breaks. Reports all findings and auto-fixes critical, high, and medium issues (low/nit findings are reported only).

</objective>

<inputs>

- **$ARGUMENTS**: optional
  - No argument: full sweep, report only — lists all findings, no changes made (default)
  - `fix` — full sweep + auto-fix `critical`, `high`, and `medium` findings; `low` findings reported only
  - `agents` — restrict sweep to agent files only, report only
  - `skills` — restrict sweep to skill files only, report only
  - `agents fix` / `skills fix` — restricted scope + auto-fix (scope always precedes `fix`)

</inputs>

<workflow>

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

Record the full file list — this becomes the audit scope for Steps 3–4.

## Step 3: Per-file audit via self-mentor

Spawn one **self-mentor** agent per file (or batch into groups of 4–5 for efficiency). Each invocation prompt must end with:

> "Include a `### Confidence` block at the end of your report: **Score**: 0.N (high ≥0.9 / moderate 0.7–0.9 / low \<0.7) and **Gaps**: what limited thoroughness."

Each invocation should ask self-mentor to check:

- **Purpose and logical coherence**: is the agent's/skill's role clearly defined? Does its scope make sense — not too broad, not too narrow? Would a new user understand when to reach for it vs a similar one?
- **Structural completeness**: required sections present, tags balanced, step numbering sequential
- **Cross-reference validity**: every agent/skill name mentioned must exist on disk
- **Verbosity and duplication**: bloated steps, repeated instructions, copy-paste between files
- **Content freshness**: outdated model names, stale version pins, deprecated API references
- **Hardcoded user paths**: any `/Users/<name>/` or `/home/<name>/` absolute path — must be `.claude/`, `~/`, or derived from `git rev-parse --show-toplevel`
- **Infinite loops**: does file A's follow-up chain reference file B which references A creating a cycle? (flag, don't auto-fix)

Collect all findings from each self-mentor response into a structured list keyed by file path.

## Step 4: System-wide checks

Beyond per-file analysis, run cross-file checks that self-mentor cannot do alone:

Run the following checks. For file-listing steps use Glob; for content-search steps use Grep. Bash is only needed for the pipeline comparisons and the `printf`/`jq` blocks below.

```bash
# Color helpers — makes severity levels and source agents scannable in terminal output
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; CYN='\033[0;36m'; NC='\033[0m'
# RED → breaking / critical  |  YELLOW → warning / medium  |  GREEN → pass  |  CYAN → agent name / source
```

**Check 1 — Inventory drift (MEMORY.md vs disk)**
Use Glob (`agents/*.md`, path `.claude/`) to list agent files; extract basenames and sort, then write to `/tmp/agents_disk.txt` via Bash:

```bash
ls .claude/agents/*.md | xargs -n1 basename | sed 's/\.md$//' | sort > /tmp/agents_disk.txt
```

Use Grep tool (pattern `^\- Agents:`, file `.claude/memory/MEMORY.md`) to read the roster line. Repeat with Glob (`skills/*/`, path `.claude/`) for skills — write to `/tmp/skills_disk.txt` — and Grep (`^\- Skills:`) for the MEMORY.md line.

**Check 2 — README vs disk**
Use Grep tool (pattern `^\| \*\*`, file `README.md`, output mode `content`) to extract agent/skill table rows.

**Check 3 — settings.json permissions**
Use Grep tool (pattern `gh |python -m|ruff|mypy|pytest`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to collect bash commands used in skills.

**Check 4 — Orphaned follow-up references**
Use Grep tool (pattern `` `/[a-z-]*` ``, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to find skill-name references; compare against disk inventory.

**Check 5 — Hardcoded user paths**
Use Grep tool (pattern `/Users/|/home/`, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`) to flag non-portable paths.

**Check 6 — permissions-guide.md drift** — every allow entry must appear in the guide, and vice versa

```bash
# Allow entries missing from guide
jq -r '.permissions.allow[]' .claude/settings.json | \
while IFS= read -r perm; do
  grep -qF "\`$perm\`" .claude/permissions-guide.md 2>/dev/null \
    || printf "${YEL}⚠ MISSING from guide${NC}: %s\n" "$perm"
done

# Guide entries orphaned (not in allow list)
grep '^| `' .claude/permissions-guide.md | awk -F'`' '{print $2}' | \
while IFS= read -r perm; do
  jq -e --arg p "$perm" '.permissions.allow | contains([$p])' .claude/settings.json > /dev/null 2>&1 \
    || printf "${YEL}⚠ ORPHANED in guide${NC}: %s\n" "$perm"
done
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

### Tool efficiency

For each agent and skill, validate that declared tools match actual usage — no unnecessary permissions, no missing tools.

**Mechanical check** — for each skill, cross-reference `allowed-tools:` frontmatter against tool names referenced in the workflow body:

```bash
for f in .claude/skills/*/SKILL.md; do
  name=$(basename "$(dirname "$f")")
  declared=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^allowed-tools:/{sub(/^allowed-tools: /,""); print}' "$f")
  body=$(awk '/^---$/{c++} c>=2{print}' "$f")
  for tool in Read Write Edit Bash Grep Glob Task WebFetch WebSearch; do
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
- `Bash` absent for an agent whose domain involves running code (linting, CI validation, performance profiling) → **high** (silent failure when workflow invokes shell commands)
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

## Step 5: Aggregate and classify findings

Group all findings from Steps 1–4 into a severity table:

| Severity     | Examples                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **critical** | Broken cross-reference (agent/skill does not exist on disk), MEMORY.md inventory wrong, relative path that silently falls back to wrong directory                                                                                                                                                                                                                                                                                                                    |
| **high**     | Dead loop in follow-up chain, missing settings.json permission for a tool in use, broken code example (undefined variable, wrong command syntax), agent/skill instruction directly contradicts a `.claude/CLAUDE.md` directive, deprecated/invalid hook event name or type in use, `context:fork + disable-model-invocation:true` on the same skill (skill cannot run), tool declared in `tools:`/`allowed-tools:` that is needed but absent causing silent failures |
| **medium**   | Duplication across files, stale model name, README row missing for existing skill, hardcoded `/Users/<name>/` path, undocumented modes in inputs, deprecated frontmatter field or settings key, permissions-guide.md missing row for an allow entry or containing an orphaned row, declared tool not referenced anywhere in the workflow (unnecessary permission surface)                                                                                            |
| **low**      | Verbosity, minor formatting, incomplete follow-up chain, outdated version pin with "autoupdate" note, agent/skill omits a CLAUDE.md principle but doesn't contradict it, 💡 new documented CC feature not yet used                                                                                                                                                                                                                                                   |

## Step 6: Cross-validate critical findings

Before surfacing any `critical` finding in the report, spawn a second **self-mentor** agent targeting only that file with a prompt that names the specific finding:

```
Independently review <file> for the following specific issue: "<finding description>".
Do NOT read any prior self-mentor report on this file.
Confirm: is this a real critical issue, a false positive, or something lower severity?
Explain your reasoning. Include your ## Confidence block.
```

Classify the outcome:

- **Both agree it is critical** → include as critical in the report ✓
- **Second pass disagrees or downgrades** → downgrade to `high` with a note: "unconfirmed critical — one of two independent passes flagged this"
- **Both agree it is NOT critical** → remove from critical list; re-classify at the lower severity both agree on

This cross-validation adds one extra spawn per critical finding — it is worth it to avoid false-positive blocking issues reaching the user.

## Step 7: Report findings

Output a structured audit report before fixing anything:

```
## Audit Report — .claude/ config

### Scope
- Agents audited: N
- Skills audited: N
- System-wide checks: inventory drift, README sync, permissions, infinite loops, hardcoded paths, CLAUDE.md consistency, docs freshness, permissions-guide drift

### Findings by Severity

#### Critical (N)
| File | Line | Issue | Category |
|---|---|---|---|
| agents/foo.md | 42 | References `bar-agent` which does not exist on disk | broken cross-ref |

#### High (N)
...

#### Medium (N)
...

#### Low (N) — reported only, not auto-fixed
...

### Summary
- Total findings: N (C critical, H high, M medium, L low)
- Auto-fix eligible: N (critical + high + medium)
```

If `fix` was not passed, stop here and present the report.

## Step 8: Fix critical, high, and medium findings

For each `critical`, `high`, and `medium` finding, apply a targeted fix:

- **Broken cross-reference**: remove or replace with the correct name (check disk to find the right target)
- **Inventory drift in MEMORY.md**: regenerate the agents/skills lines from disk
- **README row missing**: add the row with description from the file's `description:` frontmatter
- **Dead loop**: break the cycle by removing or rephrasing one of the follow-up references (flag for user review before changing)
- **Missing settings.json permission**: note it in the report — do NOT auto-edit settings.json (structural JSON edits are risky)
- **Hardcoded `/Users/<name>/` path**: replace with `.claude/` (project-relative), `~/` (home-relative), or `$(git rev-parse --show-toplevel)/` as appropriate
- **Broken code example**: fix the code directly (undefined variables, wrong API, wrong shell syntax)
- **Undocumented modes**: add the mode to `<inputs>` block and `argument-hint` frontmatter
- **`! BREAKING` context:fork + disable-model-invocation:true**: remove `disable-model-invocation: true` from the skill frontmatter. If the skill truly needs no model (pure tool pipeline), remove `context: fork` instead — but any skill using `Task` to spawn agents and reading their results needs the model.
- **permissions-guide.md missing row**: add the table row to the correct section using `/manage add perm <rule> "description" "use case"` or manually insert it
- **permissions-guide.md orphaned row**: remove the row (the allow entry was already removed from settings.json; the guide row is stale)
- **CLAUDE.md contradiction**: do NOT auto-fix — raise to user with the specific contradiction (quote both the CLAUDE.md directive and the conflicting line in the agent/skill). CLAUDE.md takes precedence; the user decides whether to update the agent/skill or revise CLAUDE.md.

After each fix, note the file and change in a running fix log.

**Low findings** (nits): collect them in the final report but do not auto-fix — present them for optional manual cleanup.

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
| low | N | — | N |

### Fixes Applied
| File | Change |
|---|---|
| agents/foo.md | Replaced broken ref `bar-agent` → `baz-agent` |

### Remaining (low/nits — manual review optional)
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
- This skill is the correct pre-flight before `/sync` — run `/audit` to confirm config is clean, then `/sync apply` to propagate
- Follow-up chains:
  - Audit clean → `/sync apply` to propagate verified config to `~/.claude/`
  - Audit found structural issues → review flagged files manually before syncing
  - Audit found many low items → schedule a dedicated `/refactor`-style cleanup pass
  - After fixing agent instructions (from audit findings) → `/calibrate <agent>` to verify the fix improved recall and confidence calibration

</notes>
