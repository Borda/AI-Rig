---
name: brainstorm
description: Interactive design-first skill for turning fuzzy ideas into approved specs. Asks clarifying questions one at a time, proposes 2–3 approaches with trade-offs, writes a structured spec to _brainstorming/, runs a self-mentor review for gaps and ambiguity, then gates on explicit user approval before any implementation begins.
argument-hint: <fuzzy idea or feature goal> | breakdown <spec-file>
disable-model-invocation: true
allowed-tools: Read, Write, Glob, Grep, Agent, TaskCreate, TaskUpdate, AskUserQuestion
---

<objective>

Turn an unformed idea into a concrete, approved spec before any code is written. The session is conversational — clarifying questions narrow the problem; a proposal crystallises the solution; a written spec locks in scope. `self-mentor` reviews the spec for gaps and ambiguity before you see it. Nothing advances to implementation until you give explicit approval.

</objective>

<inputs>

- **$ARGUMENTS**: required — a fuzzy idea, goal, or feature request in any form; a single sentence is enough

Examples:

- `/brainstorm add a caching layer to the pipeline`

- `/brainstorm redesign how agents hand off to each other`

- `/brainstorm I want users to be able to export results as CSV`

- **`breakdown <spec-file>`** — breakdown mode: read an already-approved spec (must be a path to a `_brainstorming/*.md` file), ask for clarification on blocking questions before generating the plan, then produce a structured, ordered action plan with discrete tasks each tagged with a ready-to-run invocation. Skips Steps 1–6 entirely.

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: Before Step 1, create TaskCreate entries for all 6 steps
(context scan, clarifying questions, propose approaches, write spec, spec review,
present + gate). Then print a session plan to the user:

> **Brainstorming: \<goal from $ARGUMENTS>**
> Plan: context scan → clarifying questions → propose approaches → write spec → review → approval gate.
> Starting with a codebase scan...

## Step 1: Context scan

Gather project context before asking anything:

- Read `README.md` and any relevant files under `docs/`
- Grep for keywords from `$ARGUMENTS` across `src/` or the project root
- Identify: related code that already exists, stated non-goals in docs, prior design decisions

Goal: understand constraints so questions are targeted, not generic. If the idea already exists or is clearly out of scope, say so immediately and stop.

## Step 2: Clarifying questions

Use `AskUserQuestion` for every clarifying question — this renders an interactive prompt inline, not plain text.

Rules:

- Ask **one question at a time** — call `AskUserQuestion` once, wait for the answer, then decide whether another question is needed
- Always use **multiple-choice** options in `AskUserQuestion`: list lettered choices so the user can reply with just "a", "b", or "c"; mark the option you recommend with **★** (e.g., `a) Option A ★ recommended`) so the user has a sensible default
- Maximum **3 questions** — after 3, call `AskUserQuestion` with two framings and ask which is closer
- No solution proposals during this step — only gather information

**Gate**: do not proceed to Step 3 until the problem is well-defined.

## Step 3: Propose approaches

Present 2–3 candidate approaches based on the clarified problem in your text response. Write the full approach table — including Name, Summary, Pros, Cons, and When to prefer for every option — **before** making any tool call. Only after all approaches are written in prose, call `AskUserQuestion` to ask the user to pick one:

For each approach in the text include:

- **Name**: short label (e.g. "Lazy cache", "Eager precompute")
- **Summary**: one sentence
- **Pros**: 1–3 bullets
- **Cons**: 1–3 bullets
- **When to prefer**: one sentence

After presenting, call `AskUserQuestion` with the lettered options so the user can reply with a single letter. Do not proceed until a direction is chosen.

## Step 4: Write spec

Write to `_brainstorming/YYYY-MM-DD-<slug>.md` using the Write tool (creates the directory if absent):

Use this structure:

```markdown
# <title>

**Date**: YYYY-MM-DD
**Status**: draft

## Goal
[One paragraph: what problem this solves and for whom]

## Non-goals
[Explicit list of what this does NOT cover]

## Proposed design
[The chosen approach from Step 3, with enough detail to implement]

## Open questions
[Unresolved decisions that need answers before or during implementation]

## Success criteria
[Observable, testable outcomes that confirm the design is working]
```

Describe *what*, not *how*. No implementation details.

## Step 5: Spec review

Before spawning, pre-compute the output path:
`BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`
`OUTPUT_PATH="_outputs/$(date +%Y)/$(date +%m)/output-brainstorm-review-$BRANCH-$(date +%s).md"`

Spawn **self-mentor** with a spec-focused prompt scoped to spec quality only — not a full config audit (inject the pre-computed `$OUTPUT_PATH` into the prompt in place of `<output-path>`):

```
Read _brainstorming/<spec-file>. Audit for spec quality only:
- Completeness: are all five sections present and non-empty?
- Ambiguity: any section vague enough that two implementers would build different things?
- Scope creep: does the Proposed design exceed the stated Goal?
- Placeholders: any "[TBD]" or "[TODO]" that must be filled before approval?
Write your full findings to <output-path> using the Write tool.
Return ONLY a compact JSON envelope: {"status":"done","findings":N,"file":"<path>","confidence":0.N,"summary":"<one-line>"}
```

**Passive health monitoring**: the Agent tool is synchronous — Claude awaits self-mentor's response natively. If self-mentor does not return within 15 min (Claude Code's internal timeout), surface any partial output already written to `_outputs/` with a ⏱ marker and continue to Step 6 with an incomplete review noted.

If `findings > 0`: revise the spec to address the findings and loop back to Step 5 (max 2 revision cycles total). After 2 cycles with remaining findings, surface unresolved issues to the user and proceed to Step 6 anyway.

## Step 6: Present and gate

Show the spec path and a one-paragraph summary of the spec. Then call `AskUserQuestion` with:

- (a) Approved — proceed to planning
- (b) Needs changes — describe what to revise
- (c) Start over — back to clarifying questions

**Gate**: do not exit until the user approves. On (b): revise the spec and loop back to Step 5. On (c): loop back to Step 2. (max 3 approval cycles — after 3 (b) rejection responses with no convergence, surface unresolved concerns to user and stop rather than looping again.)

On approval suggest the natural next step based on what the spec targets:

- **Spec targets `.claude/` config** (an agent, skill, or rule described in `agents/`, `skills/`, or `rules/`): suggest `/manage update <name> _brainstorming/<file>` to apply the spec inline, or `/manage create <type> <name> "description"` for a new entity.
- **Spec targets application code, system setup, or mixed changes**: suggest `/brainstorm breakdown _brainstorming/<file>` to generate a structured action plan with per-task skill/command tags. Use `/develop plan _brainstorming/<file>` only if the spec is purely a code implementation task with no setup or config steps.

## Mode: Breakdown

Triggered when `$ARGUMENTS` starts with `breakdown ` followed by a file path.

Read the spec file at the given path. Then:

### Step B1: Scan for blocking open questions

Read the spec's "Open questions" section. For each question, determine whether it is **blocking** (no recommended option stated and the answer is genuinely unknown — two implementers would make different assumptions) or **non-blocking** (spec already states a recommended option or the answer is inferable from context).

For each blocking question found: call `AskUserQuestion` with the question as a multiple-choice prompt — one question at a time, in order — before proceeding to Step B2. Non-blocking questions must not trigger a question; list them in the plan table footnote instead.

### Step B2: Generate the action plan

1. Parse the spec into discrete action items from the "Proposed design" and "Success criteria" sections
2. For each action item, classify and tag with the recommended downstream skill or command, and write a ready-to-run invocation string — not just a label:
   - `.claude/` config change (agent/skill/rule) → full `/manage create <type> <name> "description"` or `/manage update <name> <spec-file>` string
   - System install or shell setup → full shell command (e.g., `python3 -m venv ~/tools/my-venv && pip install package`)
   - Application code change → full `/develop feature "<goal>"` or `/develop fix "<symptom>"` string
   - Documentation → full `/develop feature "<doc goal>" --mode doc` string
   - Verification/testing → full `/develop feature "<test goal>"` string or manual check command
3. Output an ordered task table:

```
## Action Plan: <spec title>

Spec: <file path>

| # | Task | Invocation |
|---|------|------------|
| 1 | Install venv | `bash` — `python3 -m venv ~/tools/my-venv && pip install package` |
| 2 | Add `.mcp.json` entry | `/manage update .mcp.json "add my-server config"` |
| … | … | … |

### Non-blocking open questions (resolve during implementation)
- [Any non-blocking open questions from the spec, or "None" if absent]
```

### Step B3: Post-plan prompt

After presenting the plan table, call `AskUserQuestion` with:

- (a) Start task 1 now ★ recommended
- (b) Copy plan and pass to another agent
- (c) Revise spec first

On **(a)**: proceed immediately with the invocation string from task 1 — run the bash command, invoke the skill, or call the tool as specified.
On **(b)**: output the full plan table as a clean markdown block (no prose wrapper) suitable for pasting into another agent's prompt, then stop.
On **(c)**: stop and tell the user to revise the spec and re-run `/brainstorm breakdown <spec>`.

End with a `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- **No code at any point** — this skill produces a spec document only; implementation is out of scope
- **`disable-model-invocation: true`** — the skill is conversational; the parent model drives all steps turn by turn
- **self-mentor scope in Step 5** — the spawn prompt must constrain scope to spec quality explicitly; do not let it audit `.claude/` config files
- **\_brainstorming/ directory** — created if absent; spec filenames use `YYYY-MM-DD-<kebab-slug>.md` format
- **Follow-up**: on spec approval → if targeting `.claude/` config (agent/skill/rule): `/manage update <name> <spec-file>` (type auto-detected) or `/manage create <type> <name> "desc"`; for mixed or system-level specs: `/brainstorm breakdown _brainstorming/<file>` to generate a per-task action plan; for pure code implementation: `/develop plan <spec-file>` then `/develop feature`

</notes>
