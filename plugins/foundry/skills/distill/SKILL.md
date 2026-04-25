---
name: distill
description: One-time snapshot that extracts patterns from work history and accumulated lessons, then distills them into concrete improvements — new agent/skill suggestions, roster quality review, memory pruning, or consolidating lessons and feedback into rules and agent/skill updates.
argument-hint: '[review | prune | lessons | "external <url-or-path>" | "<recurring task description>"]'
disable-model-invocation: true
allowed-tools: Read, Edit, Bash, Glob, Grep, Write, AskUserQuestion, Agent
effort: high
---

<objective>

Analyze how Claude Code is used and surface concrete improvements — new agents/skills to reduce repetition, or consolidate lessons into governance files (rules, agent instructions, skill updates) — without duplicating what exists.

</objective>

<inputs>

- **$ARGUMENTS**: optional. Four modes:
  - Omitted — analyze existing patterns and agents; generate suggestions proactively.
  - `review` — review the existing agent/skill roster for quality and gaps without suggesting new additions.
  - `prune` — evaluate the project memory file for stale, redundant, or verbose entries and apply a trimmed version.
  - `lessons` — read `.notes/lessons.md` and memory feedback files, then distill recurring patterns into proposed rule files, agent instruction updates, and skill workflow changes.
  - `external <source>` — analyse an external plugin, skill, or agentic resource and produce a structured adoption proposal. `<source>` is a URL, file path, or local directory.
  - Description of a recurring task — use the description as context when generating suggestions (e.g. "I keep doing X manually").

</inputs>

<constants>
MEMORY_DIR=".claude/agent-memory"
</constants>

<workflow>

## Step 1: Inventory existing agents and skills

Use the Glob tool to enumerate agents (pattern `agents/*.md`, path `.claude/`) and skills (pattern `skills/*/SKILL.md`, path `.claude/`).

For each agent/skill found, extract: name, description, tools, purpose.

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
git log --oneline -100 | cut -d' ' -f2 | sort | uniq -c | sort -rn | head -15
```

Then use the Glob tool (pattern `todo_*.md`, path `.plans/active/`) to list active task files; read each with the Read tool. Also read `.notes/lessons.md` (if it exists) for task history and conversation hints.

If `$ARGUMENTS` was provided, use it as additional context for the pattern analysis.

### Frequency Heuristics

- **3+ occurrences** of a pattern in recent history → candidate for automation
- **2+ different projects** using the same manual process → cross-project skill
- **significant manual effort** per occurrence (subjective — use git history context) → high-value automation target
- **Domain-specific knowledge** required → candidate for a specialist agent (not just a skill)

## Step 3: Gap analysis

> **`review` mode**: focus on agent/skill quality and coverage gaps — skip "Recommend: New Agent/Skill" analysis and focus on "Existing Coverage" and "Recommend: Enhance Existing".

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

> **`review` mode**: duplication checks still apply — review mode does not skip this step.

Before recommending anything, run through both the overlap check and the anti-pattern checklist:

```markdown
For each candidate agent/skill:
- Does any existing agent cover >50% of its scope? → enhance existing instead
- Is the name/description confusingly similar to an existing one? → rename existing
```

Anti-pattern checklist — reject the candidate if any apply:

1. **Role vs task confusion**: agents are roles, not tasks. Do not create an agent for every different topic.
2. **Near-duplicate**: the candidate duplicates an existing agent with a slightly different name. Enhance the existing one instead.
3. **Thin wrapper**: the candidate skill just calls one agent with fixed args. That is not enough value to justify a new skill file. Exception: skills that add measure-first/measure-after bookends, multi-mode dispatch across 3+ agents, or safety breaks (retry limits, validation gates) justify the wrapper even if only one agent executes for a given invocation.

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

## Mode: Memory Pruning (prune)

Locate, evaluate, and trim the project memory file.

**Find the memory file:**

<!-- Note: this slug derivation is also used in audit/SKILL.md Check 11. If the auto-memory path convention changes, update both files. -->

```bash
# timeout: 3000
PROJECT="$(git rev-parse --show-toplevel)"
MEMORY_FILE="$HOME/.claude/projects/$(echo "$PROJECT" | sed 's|[/.]|-|g' | sed 's|^-||')/memory/MEMORY.md"
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

```text
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

Use Read tool on `.notes/lessons.md` (skip if file not found). Derive MEMORY_DIR via canonical snippet from `<constants>`, then use Glob tool with pattern `feedback_*.md` in MEMORY_DIR to list feedback files; read each with Read tool. Also read `.claude/rules/` (Glob `rules/*.md`, path `.claude/`) to understand what's already captured as a rule.

**Step L2: Cluster and classify**

Group all lessons/feedback entries by domain. Use model reasoning to identify clusters of related items:

- **Git & commit discipline** (staging, branching, commit messages, push safety)
- **Testing & QA** (test patterns, mocking rules, coverage gaps)
- **Agent & skill config** (agent instructions, skill workflow, CLAUDE.md additions)
- **Communication & output** (tone, format, reporting)
- **Tool & permission use** (Bash vs native tools, settings.json)
- **Other** (project-specific, one-off)

For each lesson entry, classify its disposition:

| Disposition | Meaning |
| --- | --- |
| `→ rule` | Recurring enough to warrant a standalone `.claude/rules/<name>.md` file |
| `→ agent update` | Specific to one agent's instructions — edit that agent's `.md` file |
| `→ skill update` | Specific to one skill's workflow — edit that skill's `SKILL.md` |
| `→ already covered` | Already present verbatim (or near-verbatim) in an existing rule, agent, or CLAUDE.md |
| `→ too narrow` | One-off, project-specific, or not generalizable — keep in memory only |

Thresholds:

- **`→ rule`**: 2+ distinct lessons on the same topic, or a single lesson that applies across ≥3 agents/skills
- **`→ agent/skill update`**: lesson applies specifically to one file's behavior and is not yet there
- **`→ already covered`**: exact principle already in the target file — mark and skip

**Step L3: Generate proposals**

Produce a structured proposal table. Do not apply anything yet — report first.

````markdown
## Lessons Distillation Proposals

### Summary
- Source files read: N (.notes/lessons.md + N feedback files)
- Total lessons: N
- Clusters: N domains

### Proposals

| # | Cluster | Lesson (condensed) | Disposition | Target |
|---|---------|-------------------|-------------|--------|
| 1 | Git | Never use git add -A; stage specific files | → already covered | rules/git-commit.md |
| 2 | Agent config | Agent description must include NOT-for clause | → rule | add to existing rule: rules/foundry-config.md |
| 3 | Communication | Flag blockers before starting, not mid-task | → already covered | rules/communication.md |

### New Rule Files Proposed (N)

#### rules/<name>.md
**Cluster**: [domain]
**Lessons consolidated**: [list lesson IDs, e.g., L1, L3, L7]
**Draft content**:
```markdown

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

**Conflict pre-check** — before presenting the question, run in parallel for every `→ rule` and `→ agent/skill update` proposal:

1. **Existing content grep**: use Grep to search the target file (if it already exists) for the section heading or key phrase the delta would insert near. A hit = potential collision with existing content.
2. **Cross-proposal collision**: if two proposals both target the same file and same section heading, mark both ⚠ CONFLICT.

Annotate each conflicting proposal row with ⚠. If any conflicts found, print above the question:

```text
⚠ Conflicts detected:
  - Proposal #N conflicts with existing content in <file>:<section> — both modify <topic>
  - Proposals #N and #M both target <file>:<section>
Review conflicts manually or select (b) to inspect each change before writing.
```

Print the (annotated) proposal table. Then call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into the tool call arguments:
- question: "Apply proposals?"
- (a) label: `Apply non-conflicting` — description: write all `→ rule` and `→ agent/skill update` changes except ⚠ flagged proposals
- (b) label: `Review first` — description: show a diff of each proposed change before writing
- (c) label: `Skip` — description: discard proposals and exit without changes

If the user selects (a), apply changes:

- **New rule files**: Write tool to create `.claude/rules/<name>.md` with the drafted content
- **Agent updates**: Edit tool to insert the new instruction into the appropriate section of the agent file
- **Skill updates**: Edit tool to insert the new step/note in the skill file

After applying:

1. Run cross-reference checks — use Grep to verify new rule files are referenced from `CLAUDE.md` or the agent files that govern them (any rule with project-wide applicability should appear as a `See .claude/rules/<name>.md` reference in `CLAUDE.md`; agent-scoped rules should appear in the relevant agent file)
2. Print a compact apply summary:

```text
Applied N changes — <date>
  New rules:      N files — [names]
  Agent updates:  N files — [names]
  Skill updates:  N files — [names]
  Skipped:        N (already covered or too narrow)
```

3. Remind the user: "Run `/foundry:init` to propagate rule changes to `~/.claude/`"

4. **Git diff gate** — run after all writes complete:

```bash
git diff HEAD -- <space-separated list of changed files>  # timeout: 5000
```

Print the diff. If anything unexpected appears, revert individual files before proceeding: `git checkout HEAD -- <file>`. This is the final safety net — changes are recoverable until committed.

**Step L5: curator review** — after applying changes, dispatch curator to audit the created and modified config files:

```text
Agent(subagent_type="foundry:curator", prompt="Review the following Claude config files just created or modified by /distill:lessons: <list new rule files and updated agent/skill files from Step L4>. Check: (1) quality — rules are concrete, not vague; (2) duplication — no overlap with existing files; (3) NOT-for boundary clarity; (4) structural consistency. Return a prioritized report of issues; note advisory vs. blocking.")
```

Surface curator findings as an advisory block in terminal output. Do not block on curator findings — they are quality recommendations, not release gates.

End your response with a `## Confidence` block per CLAUDE.md output standards.

## Mode: External Distillation (external)

Analyse an external plugin, skill, or agentic resource and produce a structured adoption proposal for the local Claude Code setup.

**E1: Classify and plan**

Identify source type:
- URL → `WebFetch` (skim landing page + follow key links: README, docs, manifests, agent/skill files)
- File path → `Read`
- Directory → `Glob` `*.md`, `*.js`, `*.json` then prioritise: manifests, README, agent/skill/rule/hook files

**E2: Fast read — structure and intent**

Skim headings, frontmatter, filenames, top-level examples. Extract: purpose, target user, top-level architecture, routing logic. ≤ 2 reads per top-level file.

**E3: Slow read — full content**

Read all agent/skill/rule/hook files end to end. For large sources prioritise: prompts, rules, validation gates, templates, docs. Use Glob + Read in parallel.

**E4: Extract mental model**

Record in working notes: source intent, architecture, routing, safety model, expected outputs, key design decisions.

**E5: Identify standout implementation details**

Use Grep for: hooks, validation gates, must/never constraints, fallback paths, scoring rubrics, unusual prompt patterns. Flag anything absent in local setup.

**E6: Source report**

Produce inline:

```text
## Source Report — <source>
Intent:       [one line]
Architecture: [one line]
Notable hacks: [bullets]
Risks / unclear assumptions: [bullets]
Candidate artifacts: [comma list]
```

**E7: Read live local setup**

Run in parallel:
- Glob + Read on `.claude/agents/*.md`, `.claude/skills/**/SKILL.md`, `.claude/rules/*.md`
- Glob on `plugins/*/` for installed plugins

**E8: Build local capability map**

Group local agents/skills/rules by responsibility, trigger conditions, gates, output formats. Note coverage gaps.

**E9–E10: Compare and split**

For each candidate from E6, compare against local capability map. Assign to group:

- **Group A — Align + improve**: maps onto existing local agent/skill/rule, improves it without structural change
- **Group B — Differentiated highlights**: novel pattern or design philosophy, doesn't map natively — interesting but requires larger structural work or conflicts with existing design

**E11: Score candidates**

Rate each on: impact (H/M/L) · fit (H/M/L) · duplication risk (none/low/high) · effort (S/M/L) · safety risk.

**E12: Adoption brainstorm table**

Exactly one of Adopt/Tweak/Discuss/Skip per item. "Local target" = specific file or directory.

- `source/hooks/task-log.js` — Group A · **Adopt as-is** · Local target: `.claude/hooks/task-log.js` · No local equivalent; identical purpose
- `source/skills/audit/SKILL.md` — Group A · **Tweak** · Local target: `.claude/skills/audit/SKILL.md` · Adapt trigger keyword to local naming
- `source/agents/doc-scribe.md` — Group B · **Discuss** · Local target: — · Overlaps existing agent; scope TBD
- `source/skills/old-util/SKILL.md` — Group B · **Skip** · Local target: — · Covered by local equivalent

**Install-as-is recommendation**

After scoring, apply this judgement:

- **Recommend install-as-is** when: (a) Group A has ≤ 2 candidates AND source has coherent standalone design, OR (b) cumulative edit effort is L (large) for ≥ 3 candidates
- If recommending: state justification — what the source provides that local setup lacks, why cherry-picking would dilute the value
- Present as an explicit option in E13 (option b); omit if not recommended

**E13: Gate — AskUserQuestion**

Present source report + adoption table + install-as-is recommendation (when applicable). Then call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into the tool call arguments:
- question: "Apply external source candidates?"
- (a) label: `Apply Group A candidates` — description: adopt-as-is and tweak items only
- (b) label: `Install as standalone plugin` — description: install external source as standalone plugin (include only when install-as-is recommended)
- (c) label: `Review first` — description: walk through each candidate interactively
- (d) label: `Skip` — description: exit without changes

**E14: Apply**

- Option (a): reuse existing distill apply path — conflict pre-check + AskUserQuestion gate + Edit + git diff safety net (per Step L4). Limit edits to confirmed Group A targets only.
- Option (b): print install command or path; do not apply automatically — plugin installation requires user action.

**E15: Verify and report**

Print changed files. Run `git diff HEAD -- <files>` and show output. Surface unresolved Group B items as open questions for future distill runs. End with `## Confidence` block per CLAUDE.md output standards.

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

- **`external` mode calibration**: two concrete GT fixture cases defined in `calibrate/modes/skills.md`:
  - **caveman plugin** — narrow, self-contained communication mode, no local structural overlap → GT: install-as-is recommended, Group A empty or thin
  - **Karpathy autoresearch** — research automation tool, strong overlap with `research:` plugin structure → GT: Group A candidates map to research plugin, digest recommended, install-as-is not triggered
  - Ground truth = static snapshot of each tool's agent/skill/rule files (no live fetch needed); score adoption-table lane assignments against GT outcomes

- Follow-up chains:

  - Suggestion accepted for new agent/skill → `/manage create` to scaffold and register it
  - Suggestion to enhance existing → edit the agent/skill directly, then `/foundry:init`
  - `lessons` proposals applied → `/foundry:init` to propagate; `/audit rules` to verify new rule files are structurally sound

</notes>
