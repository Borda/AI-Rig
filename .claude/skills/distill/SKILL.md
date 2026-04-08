---
name: distill
description: One-time snapshot that extracts patterns from work history and accumulated lessons, then distills them into concrete improvements — new agent/skill suggestions, roster quality review, memory pruning, or consolidating lessons and feedback into rules and agent/skill updates.
argument-hint: '[review | prune | lessons | "<recurring task description>"]'
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Glob, Grep, Write, AskUserQuestion
effort: high
---

<objective>

Analyze how Claude Code is being used in this project and surface concrete improvements — either by suggesting new agents or skills that would reduce repetition, or by consolidating accumulated lessons and feedback into governance files (rules, agent instructions, skill updates) — without duplicating what already exists.

</objective>

<inputs>

- **$ARGUMENTS**: optional. Four modes:
  - Omitted — analyze the project's existing patterns and agents to generate suggestions proactively.
  - `review` — review the existing agent/skill roster for quality and gaps without suggesting new additions.
  - `prune` — evaluate the project memory file for stale, redundant, or verbose entries and apply a trimmed version.
  - `lessons` — read `.notes/lessons.md` and memory feedback files, then distill recurring patterns into proposed rule files, agent instruction updates, and skill workflow changes.
  - Description of a recurring task — use the description as context when generating suggestions (e.g. "I keep doing X manually").

</inputs>

<workflow>

## Step 1: Inventory existing agents and skills

Use the Glob tool to enumerate agents (pattern `agents/*.md`, path `.claude/`) and skills (pattern `skills/*/SKILL.md`, path `.claude/`).

For each agent/skill found, extract: name, description, tools, purpose.

If OpenSpace is installed, also check for any synthesized skill patterns (`~/.claude/openspace/skills.db`) that could be consolidated with the new rule — the presence of that file signals OpenSpace is active; store the result conceptually (active vs not) for use in Step 2.

## Step 2: Analyze work patterns

**If `$ARGUMENTS` is `prune`**: skip Steps 2–5 entirely and go to "Mode: Memory Pruning" below.

**If `$ARGUMENTS` is `lessons`**: skip Steps 2–5 entirely and go to "Mode: Lessons Distillation" below.

**If `$ARGUMENTS` is `review`**: skip the git analysis below and go directly to Step 3 (Gap analysis). Use the agent/skill descriptions from Step 1 as the sole input — the goal is to assess quality and coverage of the existing roster, not to look for new patterns in recent work. In Step 5, suppress all "Recommend: New Agent/Skill" sections and output only "Existing Coverage", "Recommend: Enhance Existing", and "No Action Needed" entries.

Otherwise, look for signals of repetitive or specialist work. The first three git commands are independent — run them in parallel:

```bash
# timeout: 3000
# --- run these three in parallel ---

# Recent git history — what kinds of changes are common?
git log --oneline -50

# What file types are being worked on?
git log --name-only --pretty="" -30 | sort | uniq -c | sort -rn | head -20

# Commit message patterns — what verbs appear most?
git log --oneline -100 | awk '{print $2}' | sort | uniq -c | sort -rn | head -15
```

Then use the Read tool on `.plans/active/todo.md` and `.notes/lessons.md` (if they exist) for task history and conversation hints.

If `$ARGUMENTS` was provided, use it as additional context for the pattern analysis.

### OpenSpace evolution drift (if active)

If the Step 1 check showed OpenSpace active, run:

```bash
# timeout: 5000
HOME_SKILLS="$HOME/.claude/skills/"
diff -rq "$HOME_SKILLS" .claude/skills/ 2>/dev/null | grep "^Files" | sed "s|Files ${HOME_SKILLS}||;s|\.claude/skills/||" | head -20
```

Each line names a skill file that differs between the home dir (where OpenSpace writes evolved versions) and the project dir (source of truth). Collect these as "graduated candidates" for the Step 5 report. If no differences, note "No drift — project and home skills are in sync."

### Frequency Heuristics

- **3+ occurrences** of a pattern in recent history → candidate for automation
- **2+ different projects** using the same manual process → cross-project skill
- **significant manual effort** per occurrence (subjective — use git history context) → high-value automation target
- **Domain-specific knowledge** required → candidate for a specialist agent (not just a skill)

## Step 3: Gap analysis

For each identified pattern, check:

1. **Is it already covered?** — search existing agent/skill descriptions for overlap
2. **Is it frequent enough?** — recurring ≥ 3 times or clearly domain-specialized (See Step 2 heuristics — combine ≥3 occurrences with the effort/frequency signals from Steps 1–2)
3. **Would a specialist add quality?** — does it require deep domain knowledge?
4. **Is it too narrow?** — a single-use task doesn't warrant a persistent agent

Thresholds for recommendation:

- **New agent**: recurring specialist role, complex decision-making, 5+ distinct capabilities
- **New skill**: workflow orchestration, multi-step process with fixed structure
- **No new file needed**: one-off or already covered by existing agent

## Step 4: Check for duplication

Before recommending anything, run through both the overlap check and the anti-pattern checklist:

```
For each candidate agent/skill:
- Does any existing agent cover >50% of its scope? → enhance existing instead
- Is the name/description confusingly similar to an existing one? → rename existing
```

Anti-pattern checklist — reject the candidate if any apply:

1. **Role vs task confusion**: agents are roles, not tasks. Do not create an agent for every different topic.
2. **Near-duplicate**: the candidate duplicates an existing agent with a slightly different name. Enhance the existing one instead.
3. **Thin wrapper**: the candidate skill just calls one agent with fixed args. That is not enough value to justify a new skill file. Exception: skills that add measure-first/measure-after bookends, multi-mode dispatch across 3+ agents, or safety breaks (retry limits, validation gates) justify the wrapper even if only one agent executes for a given invocation.

## Step 5: Report

```
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

## Mode: Memory Pruning (prune)

Locate, evaluate, and trim the project memory file.

**Find the memory file:**

<!-- Note: this slug derivation is also used in audit/SKILL.md Check 11. If the auto-memory path convention changes, update both files. -->

```bash
# timeout: 3000
PROJECT="$(git rev-parse --show-toplevel)"
MEMORY_FILE="$HOME/.claude/projects/$(echo "$PROJECT" | sed 's|[/.]|-|g')/memory/MEMORY.md"
echo "Memory file located."
```

Read the memory file with the Read tool. Also read `.claude/CLAUDE.md` to identify overlap — anything already covered in CLAUDE.md does not need to live in memory.

**Evaluate each section against these criteria:**

- **Drop**: content that is no longer accurate (removed features, resolved one-time issues, superseded decisions), or fully duplicated in CLAUDE.md
- **Trim**: sections still accurate but containing implementation history or rationale no longer needed day-to-day — keep operational facts (what/where), drop the why-it-was-built backstory
- **Keep**: rules actively applied every session; project-specific facts absent from CLAUDE.md; anything the model needs to act correctly

Before applying edits, print a brief summary of what will be trimmed to terminal so the user can review before any changes are made.

Apply changes with the Edit tool — targeted replacements for trimmed sections, full section removal for dropped ones.

Print a compact summary:

```
Pruned MEMORY.md — <date>
  Dropped: N sections — [names]
  Trimmed: N sections — [names]
  Kept:    N sections unchanged
  Saved:   ~N lines
```

End your response with a `## Confidence` block per CLAUDE.md output standards.

## Mode: Lessons Distillation (lessons)

Read accumulated lessons and feedback, then identify patterns that should be promoted into durable governance — rule files, agent instruction updates, or skill workflow changes.

**Step L1: Collect raw material**

Find and read all source material in parallel:

```bash
# timeout: 5000
# .notes/lessons.md (if it exists)
ls .notes/lessons.md 2>/dev/null && echo "found" || echo "not found"

# Memory feedback files
PROJECT="$(git rev-parse --show-toplevel)"
MEMORY_DIR="$HOME/.claude/projects/$(echo "$PROJECT" | sed 's|[/.]|-|g')/memory"  # slug derivation: git rev-parse --show-toplevel | sed 's|[/.]|-|g'
ls "$MEMORY_DIR"/feedback_*.md 2>/dev/null || echo "no feedback files"
```

Read each found file with the Read tool. Also read `.claude/rules/` (Glob `rules/*.md`, path `.claude/`) to understand what's already captured as a rule.

**Step L2: Cluster and classify**

Group all lessons/feedback entries by domain. Use model reasoning to identify clusters of related items:

- **Git & commit discipline** (staging, branching, commit messages, push safety)
- **Testing & QA** (test patterns, mocking rules, coverage gaps)
- **Agent & skill config** (agent instructions, skill workflow, CLAUDE.md additions)
- **Communication & output** (tone, format, reporting)
- **Tool & permission use** (Bash vs native tools, settings.json)
- **Other** (project-specific, one-off)

For each lesson entry, classify its disposition:

| Disposition         | Meaning                                                                              |
| ------------------- | ------------------------------------------------------------------------------------ |
| `→ rule`            | Recurring enough to warrant a standalone `.claude/rules/<name>.md` file              |
| `→ agent update`    | Specific to one agent's instructions — edit that agent's `.md` file                  |
| `→ skill update`    | Specific to one skill's workflow — edit that skill's `SKILL.md`                      |
| `→ already covered` | Already present verbatim (or near-verbatim) in an existing rule, agent, or CLAUDE.md |
| `→ too narrow`      | One-off, project-specific, or not generalizable — keep in memory only                |

Thresholds:

- **`→ rule`**: 2+ distinct lessons on the same topic, or a single lesson that applies across ≥3 agents/skills
- **`→ agent/skill update`**: lesson applies specifically to one file's behavior and is not yet there
- **`→ already covered`**: exact principle already in the target file — mark and skip

**Step L3: Generate proposals**

Produce a structured proposal table. Do not apply anything yet — report first.

````
## Lessons Distillation Proposals

### Summary
- Source files read: N (.notes/lessons.md + N feedback files)
- Total lessons: N
- Clusters: N domains

### Proposals

| # | Cluster | Lesson (condensed) | Disposition | Target |
|---|---------|-------------------|-------------|--------|
| 1 | Git | Never use git add -A; stage specific files | → already covered | rules/git-commit.md |
| 2 | Agent config | Agent description must include NOT-for clause | → rule | new: rules/agent-descriptions.md |
| 3 | Communication | Flag blockers before starting, not mid-task | → already covered | rules/communication.md |

### New Rule Files Proposed (N)

#### rules/<name>.md
**Cluster**: [domain]
**Lessons consolidated**: [list lesson IDs, e.g., L1, L3, L7]
**Draft content**:
```

______________________________________________________________________

## description: [one-line]

## [Rule heading]

[content distilled from the lessons]

```
**Why a rule file**: [applies broadly across agents/skills, not specific to one]

### Agent Instruction Updates Proposed (N)

#### agents/<name>.md
**Change**: [what to add/modify in the agent's instructions]
**Lesson source**: [which lesson(s) justify this]

### Skill Workflow Updates Proposed (N)

#### skills/<name>/SKILL.md
**Change**: [what step/note to add or modify]
**Lesson source**: [which lesson(s) justify this]

### Already Covered (N) — no action needed
- L2: [lesson] → already in [file]

### Too Narrow (N) — keep in memory
- L5: [lesson] → one-off, not generalizable
````

**Step L4: Apply (with confirmation)**

Print the proposal table. Then use AskUserQuestion:

> "Apply the distillation proposals?"
> (a) Apply — write all `→ rule` and `→ agent/skill update` changes now
> (b) Review first — show a diff of each proposed change before writing
> (c) Skip — discard proposals and exit without changes

If the user selects (a), apply changes:

- **New rule files**: Write tool to create `.claude/rules/<name>.md` with the drafted content
- **Agent updates**: Edit tool to insert the new instruction into the appropriate section of the agent file
- **Skill updates**: Edit tool to insert the new step/note in the skill file

After applying:

1. Run cross-reference checks — use Grep to verify new rule files are referenced from `CLAUDE.md` or the agent files that govern them (any rule with project-wide applicability should appear as a `See .claude/rules/<name>.md` reference in `CLAUDE.md`; agent-scoped rules should appear in the relevant agent file)
2. Print a compact apply summary:

```
Applied N changes — <date>
  New rules:      N files — [names]
  Agent updates:  N files — [names]
  Skill updates:  N files — [names]
  Skipped:        N (already covered or too narrow)
```

3. Remind the user: "Run `/sync apply` to propagate rule changes to `~/.claude/`"

End your response with a `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- This skill is introspective: it looks at the tooling itself, not just the code

- Invoke periodically (e.g., monthly) or after a burst of correction/feedback; this is a one-time snapshot, not a continuous monitor

- Suggestions are proposals — always review before creating new files

- After creating a new agent/skill based on a suggestion, re-run this skill once to confirm the gap is resolved, then stop

- **`lessons` mode is the primary consolidation path** — run after any session with significant corrections to prevent lesson drift back into MEMORY.md noise

- **Agent Teams signal tracking**: when reviewing patterns, also look for:

  - Skills using `--team` or team-mode heuristics more/less than expected → flag over/under-use relative to the decision matrix in `CLAUDE.md § Agent Teams`
  - Security findings appearing in reviews for non-auth code → suggests qa-specialist teammate scope is too broad; narrow it
  - Model tier mismatches (e.g., heavy analysis assigned to `sonnet` teammates) → flag for tier adjustment

- Follow-up chains:

  - Suggestion accepted for new agent/skill → `/manage create` to scaffold and register it
  - Suggestion to enhance existing → edit the agent/skill directly, then `/sync`
  - `lessons` proposals applied → `/sync apply` to propagate; `/audit rules` to verify new rule files are structurally sound

- **OpenSpace integration**: when OpenSpace MCP is active (`~/.claude/openspace/skills.db` exists), distill detects evolved skill variants by diffing `~/.claude/skills/` against `.claude/skills/`. Graduation = manual `cp -r ~/.claude/skills/<name> .claude/skills/<name>` + git commit; discard evolved variants that don't meet quality bar. See `docs/specs/2026-03-31-openspace-mcp-integration.md` for the full graduation flow. <!-- path is project-local (Borda.local/docs/specs/) — not synced to ~/.claude/ -->

</notes>
