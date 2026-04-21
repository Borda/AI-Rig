---
name: plan
description: Analysis-only planning — classify and scope a task without writing code; outputs a structured plan to .plans/active/.
argument-hint: <goal>
effort: medium
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion, WebFetch
disable-model-invocation: true
---

<objective>

Analysis-only mode. Produces structured plan, no code. Use to understand scope, risks, effort before `/develop:feature`, `/develop:fix`, `/develop:refactor`.

NOT for: writing code/tests (use develop mode); `.claude/` config changes (use `/manage`).

</objective>

<workflow>

## Agent Resolution
<!-- Agent Resolution: skill-specific subset — update only agents used by this skill -->

> **Foundry plugin check**: run `ls ~/.claude/plugins/cache/ 2>/dev/null | grep -q foundry` (exit 0 = installed). If check fails or uncertain, proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

When foundry **not** installed, substitute `foundry:X` with `general-purpose`, prepend role description plus `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:linting-expert` | `general-purpose` | `haiku` | `You are a static analysis specialist. Fix ruff/mypy violations, add missing type annotations, configure pre-commit hooks.` |

**Checkpoint**: plan is single-pass — `.plans/active/<slug>` file existence serves as implicit resume signal. No `.developments/` checkpoint needed; if skill interrupted, re-run `/develop:plan` to regenerate (plan makes no code changes).

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), TaskCreate all steps before any other work. Mark each step in_progress when starting, completed when done.

## Anti-Rationalizations

| Temptation | Reality |
| --- | --- |
| "The plan is obvious — no need for agent feasibility review" | Feasibility review catches domain-specific blockers (missing test infrastructure, incompatible library constraints, API changes) that seem obvious in hindsight. |
| "Codex design review is optional for small tasks" | Small tasks regularly reveal large hidden dependencies. Codex catches architectural anti-patterns before they are baked into an implementation plan. |
| "I can scope this during implementation — no need to plan first" | Scope discovered during implementation inflates PRs and obscures intent. Plan mode exists to prevent exactly this. |

# Plan Mode

## Step 1: Classify and scope

Determine task type and affected surface.

**Structural context** (codemap, if installed) — soft PATH check, silently skip if `scan-query` not found:

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 5
fi
```

If results returned: prepend `## Structural Context (codemap)` block to foundry:sw-engineer spawn prompt with hotspot JSON. Gives agent complexity picture for sizing effort and identifying risky modules before codebase exploration. If `scan-query` not found or index missing: proceed silently — do not mention codemap to user.

Spawn **foundry:sw-engineer** agent with full goal text from `$ARGUMENTS`. Agent should:

- Classify task as `feature`, `fix`, or `refactor`
- Identify affected files and modules (search codebase — no guessing)
- Assess complexity: small (1-3 files, self-contained), medium (4-8 files or 1-2 modules), large (cross-module, API changes, or 3+ modules)
- List risks: breaking changes, missing tests, unclear requirements, external dependencies
- Note complexity smells: ambiguous goal, scope creep risk, missing reproduction case, directory-wide refactor without explicit goal

Agent returns findings inline (no file handoff — output short).

## Step 2: Structured plan

Derive filename slug from goal: first 4-5 meaningful words, lowercase, hyphen-separated (e.g. `"improve caching in data loader"` -> `plan_improve-caching-data-loader.md`). Write plan to `.plans/active/<slug>` (create or overwrite). Store full path as `PLAN_FILE` — used in Steps 3 and Final output.

```markdown
# Plan: <goal>

## Brief

*[Generated after agent review — see below]*

---

## Full Plan

**Classification**: feature | fix | refactor
**Complexity**: small | medium | large
**Date**: <YYYY-MM-DD>

### Goal

<One-paragraph restatement of the goal in concrete terms — what changes, what doesn't.>

### Affected files

- `path/to/file.py` — reason
- `path/to/other.py` — reason

### Risks

- <risk 1>
- <risk 2>

### Suggested approach

1. <Step 1>
2. <Step 2>
3. <Step 3>
...

### Follow-up command

/develop <classification> <original goal text>
```

## Step 3: Agent feasibility review

Spawn execution agents for classification in parallel. Each reads `<PLAN_FILE>`, returns **only** compact JSON — no prose, no analysis:

- **feature**: foundry:sw-engineer, foundry:qa-specialist, foundry:linting-expert
- **fix**: foundry:sw-engineer, foundry:qa-specialist
- **refactor**: foundry:sw-engineer, foundry:linting-expert, foundry:qa-specialist

Each agent receives only plan file path and role — no conversation history, no unrelated context. Prompt (substitute `<ROLE>` and `<PLAN_FILE>`):

> "Read `<PLAN_FILE>`. Review the plan from your perspective as `<ROLE>`. Flag any domain-specific concerns, risks, or blockers you see. Can you execute your part autonomously without further user input? Return only: `{\"a\":\"<ROLE>\",\"ok\":true|false,\"blockers\":[\"...\"],\"q\":[\"...\"],\"concerns\":[\"...\"]}`"

**Parse-failure handling**: agent responses may not be valid JSON (especially fallback `general-purpose` agents that wrap JSON in prose). Before processing:

1. Attempt to extract JSON object using pattern `\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}` from response
2. If extraction succeeds: use extracted object
3. If extraction fails entirely: treat response as `{"a":"<ROLE>","ok":false,"blockers":["agent returned non-JSON response"],"q":[],"concerns":[]}` and enter resolution loop with re-query

Agents return inline (verdicts ~150 bytes — no file handoff). Collect all results:

- All `ok: true`, empty `blockers`, `q`, `concerns` -> note `✓ agents ready` in final output and proceed
- Any `ok: false`, non-empty `blockers` or `q` -> enter **internal resolution loop** below before surfacing to user
- Non-empty `concerns` with `ok: true` -> surface as advisory notes in final output (not blockers, domain-specific flags user should know before starting)

### Internal resolution loop (max 3 iterations)

For each blocker or open question:

1. **Attempt autonomous resolution** — search codebase, read relevant files, re-read goal. Fetch primary-source documentation for relevant issues (official docs, RFCs, library changelogs, migration guides) via WebFetch — known URLs only; WebFetch fetches a specific URL, it does not search. If answer determinable from any source, update `<PLAN_FILE>` and mark item resolved.
2. **Re-query raising agent** — send only resolved item: `{"a":"<ROLE>","resolved":"<item>","answer":"<resolution>"}`. If agent returns `ok: true` -> resolved; remove from blockers list.
3. After all resolvable items cleared, re-check: if all agents `ok: true` -> `✓ agents ready`.

**Plan file coherence**: after the resolution loop exits (regardless of outcome), annotate `<PLAN_FILE>`:
- Each resolved blocker: add `(resolved ✓)` inline
- Each unresolved blocker: add `(unresolved — requires user input)`
- Update the Brief (once it exists): note "N of M blockers resolved autonomously; N require user input"
This ensures the plan file is coherent even after partial resolution.

**Escalate to user only what cannot be resolved autonomously** — blocker requires user input when: depends on business decision, undocumented external constraint, missing credential/secret, or genuine goal ambiguity with two equally valid interpretations.

For each escalated item:

- **Issue**: one sentence — what blocks or is unclear
- **Alternatives**: 2-3 concrete options with trade-offs
- **Recommendation**: which option and why

Do not escalate: items resolvable from codebase, items that are risks (not blockers), items already addressed in plan.

## Final output

Compose brief — compact human-readable plan summary after all agent input incorporated:

```
<One-sentence summary of what the plan achieves and the main approach.>

Classification : <feature|fix|refactor>
Complexity     : <small|medium|large>
Affected files : N files across M modules
Key risks      : <one-liner or "none">
Agent review   : ✓ agents ready (<N> corrections incorporated)  |  ⚠ see below

<Steps table — use the format that best fits the complexity:>
- Simple: | # | Step |
- Staged/large: | # | Stage | What changes | Stop condition |
- Fix: | # | Action | Target | Verification |

Advisory notes from agents (omit table if none):

| Agent | Note |
|-------|------|
| <role> | <concern> |

Co-review corrections applied (<N> agents, omit table if none):

| Agent | Location | Change |
|-------|----------|--------|
| <agent> | <file or step> | <what changed> |
```

**Write brief into `<PLAN_FILE>`**: replace `*[Generated after agent review — see below]*` placeholder in `## Brief` with composed brief. File now contains both brief and full plan.

**Print to terminal**:

```
Plan -> <PLAN_FILE>

<brief content exactly as written to the file>

-> /develop <classification> <goal> when ready
```

If unresolved items escalated, print each after brief:

```
⚠ Issue: <one sentence>
  Alternatives: (a) ... (b) ... (c) ...
  Recommendation: <option> — <reason>
```

Wait for user input before printing `-> /develop ...`.

**Handoff contract**: the plan file at `<PLAN_FILE>` is consumable by downstream skills. Pass it via `--plan <PLAN_FILE>` when invoking `/develop:feature`, `/develop:fix`, or `/develop:refactor`. When a skill receives `--plan <path>`, it reads the plan file at Step 1 and:
- Extracts `Classification`, `Affected files`, `Risks`, `Suggested approach` — skipping cold codebase exploration
- Inherits agent feasibility verdicts and Codex corrections already applied
- Uses `Suggested approach` as implementation roadmap

No quality stack, no Codex pre-pass, no review loop. Exit after printing summary.

End the plan document with:

```
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
**Gaps**:
- [specific limitation or unverified assumption]

**Refinements**: N passes.
- Pass 1: [what was addressed]
```

</workflow>
