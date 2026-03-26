---
name: brainstorm
description: Interactive design-first skill for turning fuzzy ideas into approved specs. Asks clarifying questions one at a time, proposes 2–3 approaches with trade-offs, writes a structured spec to docs/specs/, runs a self-mentor review for gaps and ambiguity, then gates on explicit user approval before any implementation begins.
argument-hint: <fuzzy idea or feature goal>
disable-model-invocation: true
allowed-tools: Read, Write, Glob, Grep, Agent, TaskCreate, TaskUpdate
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

</inputs>

<workflow>

**Task tracking**: create tasks for all steps before any tool calls.

## Step 1: Context scan

Gather project context before asking anything:

- Read `README.md` and any relevant files under `docs/`
- Grep for keywords from `$ARGUMENTS` across `src/` or the project root
- Identify: related code that already exists, stated non-goals in docs, prior design decisions

Goal: understand constraints so questions are targeted, not generic. If the idea already exists or is clearly out of scope, say so immediately and stop.

## Step 2: Clarifying questions

Ask questions to sharpen the problem definition:

- Ask **one question at a time** — wait for the answer before asking the next
- Prefer **multiple-choice** format: "Is the goal (a) X, (b) Y, or (c) something else?"
- Maximum **3 questions** — after 3, propose two framings and ask which is closer
- No solution proposals during this step — only gather information

**Gate**: do not proceed to Step 3 until the problem is well-defined.

## Step 3: Propose approaches

Present 2–3 candidate approaches based on the clarified problem:

For each approach include:

- **Name**: short label (e.g. "Lazy cache", "Eager precompute")
- **Summary**: one sentence
- **Pros**: 1–3 bullets
- **Cons**: 1–3 bullets
- **When to prefer**: one sentence

Ask the user to pick one (or a mix). Do not proceed until a direction is chosen.

## Step 4: Write spec

Write to `docs/specs/YYYY-MM-DD-<slug>.md` using the Write tool (creates the directory if absent):

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

Spawn **self-mentor** with a spec-focused prompt scoped to spec quality only — not a full config audit:

```
Read docs/specs/<spec-file>. Audit for spec quality only:
- Completeness: are all five sections present and non-empty?
- Ambiguity: any section vague enough that two implementers would build different things?
- Scope creep: does the Proposed design exceed the stated Goal?
- Placeholders: any "[TBD]" or "[TODO]" that must be filled before approval?
When scoring confidence: the score reflects capability certainty on the spec as written; document context-dependent assumptions (e.g., project norms) in Gaps rather than deflating the score.
Write your full findings to /tmp/brainstorm-review-<ts>.md using the Write tool.
Return ONLY a compact JSON envelope: {"status":"done","findings":N,"file":"<path>","confidence":0.N,"summary":"<one-line>"}
```

If `findings > 0`: address them (revise the spec, re-run the review) before proceeding to Step 6.

## Step 6: Present and gate

Show the spec path and a summary to the user:

> "Spec written to `docs/specs/<file>`. Does this capture what you had in mind? Approve to proceed to planning, or tell me what to change."

**Gate**: do not exit until the user gives explicit approval. On change requests: revise the spec and loop back to Step 5.

On approval suggest the natural next step:

> "Next: `/develop plan docs/specs/<file>` to break this into an implementation plan."

</workflow>

<notes>

- **No code at any point** — this skill produces a spec document only; implementation is out of scope
- **`disable-model-invocation: true`** — the skill is conversational; the parent model drives all steps turn by turn
- **self-mentor scope in Step 5** — the spawn prompt must constrain scope to spec quality explicitly; do not let it audit `.claude/` config files
- **docs/specs/ directory** — created if absent; spec filenames use `YYYY-MM-DD-<kebab-slug>.md` format
- **Follow-up**: on spec approval → `/develop plan <spec-file>` for task decomposition; `/develop feature` once the plan is in place

</notes>
