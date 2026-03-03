---
name: observe
description: Analyzes ongoing work patterns and the existing agent/skill roster to suggest creating new agents or skills for specialized or repetitive tasks. Continuously monitors what tasks are being done repeatedly or where specialist knowledge would help. Avoids recommending duplicates.
argument-hint: '[review | "<recurring task description>"]'
disable-model-invocation: true
allowed-tools: Read, Bash, Grep, Glob
---

<objective>

Analyze how Claude Code is being used in this project and suggest new agents or skills that would reduce repetition, improve quality, or handle specialized domains — without duplicating what already exists.

</objective>

<inputs>

- **$ARGUMENTS**: optional. Three modes:
  - Omitted — analyze the project's existing patterns and agents to generate suggestions proactively.
  - `review` — review the existing agent/skill roster for quality and gaps without suggesting new additions.
  - Description of a recurring task — use the description as context when generating suggestions (e.g. "I keep doing X manually").

</inputs>

<workflow>

## Step 1: Inventory existing agents and skills

Use the Glob tool to enumerate agents (pattern `agents/*.md`, path `.claude/`) and skills (pattern `skills/*/SKILL.md`, path `.claude/`).

For each agent/skill found, extract: name, description, tools, purpose.

## Step 2: Analyze work patterns

**If `$ARGUMENTS` is `review`**: skip the git analysis below and go directly to Step 3 (Gap analysis). Use the agent/skill descriptions from Step 1 as the sole input — the goal is to assess quality and coverage of the existing roster, not to look for new patterns in recent work. In Step 5, suppress all "Recommend: New Agent/Skill" sections and output only "Existing Coverage", "Recommend: Enhance Existing", and "No Action Needed" entries.

Otherwise, look for signals of repetitive or specialist work. The first three git commands are independent — run them in parallel:

```bash
# --- run these three in parallel ---

# Recent git history — what kinds of changes are common?
git log --oneline -50

# What file types are being worked on?
git log --name-only --pretty="" -30 | sort | uniq -c | sort -rn | head -20

# Commit message patterns — what verbs appear most?
git log --oneline -100 | awk '{print $2}' | sort | uniq -c | sort -rn | head -15
```

Then use the Read tool on `tasks/todo.md` and `tasks/lessons.md` (if they exist) for task history and conversation hints.

If `$ARGUMENTS` was provided, use it as additional context for the pattern analysis.

### Frequency Heuristics

- **3+ occurrences** of a pattern in recent history → candidate for automation
- **2+ different projects** using the same manual process → cross-project skill
- **> 10 minutes** of manual work per occurrence → high-value automation target
- **Domain-specific knowledge** required → candidate for a specialist agent (not just a skill)

## Step 3: Gap analysis

For each identified pattern, check:

1. **Is it already covered?** — search existing agent/skill descriptions for overlap
2. **Is it frequent enough?** — recurring ≥ 3 times or clearly domain-specialized
3. **Would a specialist add quality?** — does it require deep domain knowledge?
4. **Is it too narrow?** — a single-use task doesn't warrant a persistent agent

Thresholds for recommendation:

- **New agent**: recurring specialist role, complex decision-making, 5+ distinct capabilities
- **New skill**: workflow orchestration, multi-step process with fixed structure
- **No new file needed**: one-off or already covered by existing agent

## Step 4: Check for duplication

Before recommending anything:

```
For each candidate agent/skill:
- Does any existing agent cover >50% of its scope? → enhance existing instead
- Is the name/description confusingly similar to an existing one? → rename existing
- Does it overlap with a GitHub Copilot agent? → acceptable if serving different tool
```

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
```

## Decision Criteria

### When to Create a New Agent (specialist role)

- Has a distinct persona, expertise, or decision-making style
- Would be invoked for a class of tasks, not a single workflow
- Benefits from deep domain knowledge in its system prompt
- Examples: `migration-guide`, `dependency-auditor` (hypothetical — use abstract names, not agent names that don't exist on disk)

### When to Create a New Skill (workflow)

- Has a fixed multi-step process that spawns sub-agents or runs commands
- Is invoked ad-hoc for a specific task type
- Benefits from structured output format
- Examples: `analyse`, `review`, `release`

### When to Do Nothing

- Task is handled well by an existing agent with slight prompt adjustment
- Too project-specific to be reusable
- One-off task that won't recur

### Anti-patterns to Avoid

- Creating an agent for every different topic (agents are roles, not tasks)
- Duplicating an existing agent with a slightly different name
- Creating a skill that just calls one agent with fixed args (not enough value)

</workflow>

<notes>

- This skill is introspective: it looks at the tooling itself, not just the code
- Run periodically (e.g., monthly) or after noticing repetitive manual work
- Suggestions are proposals — always review before creating new files
- After creating a new agent/skill based on a suggestion, re-run this skill once to confirm the gap is resolved, then stop
- Follow-up chains:
  - Suggestion accepted for new agent/skill → `/manage create` to scaffold and register it
  - Suggestion to enhance existing → edit the agent/skill directly, then `/sync`

</notes>
