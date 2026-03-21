# Plan Mode

Analysis-only mode that produces a structured plan without writing any code. Use this to understand scope, risks, and effort before committing to a full `/develop feature|fix|refactor`.

## Step 1: Classify and scope

Determine the task type and affected surface.

Spawn a **sw-engineer** agent with the full goal text from `$ARGUMENTS`. The agent should:

- Classify the task as `feature`, `fix`, or `refactor`
- Identify affected files and modules (search the codebase — do not guess)
- Assess complexity: small (1–3 files, self-contained), medium (4–8 files or 1–2 modules), large (cross-module, API changes, or 3+ modules)
- List risks: breaking changes, missing tests, unclear requirements, external dependencies
- Note any complexity smells: ambiguous goal, scope creep risk, missing reproduction case, directory-wide refactor without explicit goal

The agent returns its findings inline (no file handoff needed — output is short).

## Step 2: Structured plan

Write the plan to `tasks/todo.md` (overwrite if the file is a stub; append a new section if it contains active work):

```markdown
# Plan: <goal>

**Classification**: feature | fix | refactor
**Complexity**: small | medium | large
**Date**: <YYYY-MM-DD>

## Goal

<One-paragraph restatement of the goal in concrete terms — what changes, what doesn't.>

## Affected files

- `path/to/file.py` — reason
- `path/to/other.py` — reason

## Risks

- <risk 1>
- <risk 2>

## Suggested approach

1. <Step 1>
2. <Step 2>
3. <Step 3>
...

## Follow-up command

```

/develop <classification> <original goal text>

```
```

## Final output

Print a compact terminal summary (not a full report file):

```
Plan written to tasks/todo.md

Classification : <feature|fix|refactor>
Complexity     : <small|medium|large>
Affected files : N files across M modules
Key risks      : <one-liner or "none">

→ /develop <classification> <goal> when ready
```

No quality stack, no Codex pre-pass, no review loop. Exit after printing the summary.
