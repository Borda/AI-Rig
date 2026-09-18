<!-- file: breakdown.md — consumers: brainstorm/SKILL.md -->

## Mode: Breakdown

Triggered when `$ARGUMENTS` starts with `breakdown ` followed by file path.

Read file at given path. Check `**Status**:` field:

- `Status: tree` → **Distillation mode** (Steps D1–D4 below)
- `Status: draft` → **Action plan mode** (Steps B1–B3 below)

### Distillation mode (Status: tree)

#### Step D1: Present tree summary

Read all open branches from file. Show compact tree summary (same format as Step 3) and one-sentence description of each open branch. State count of open and closed branches.

#### Step D2: Distillation questions

Ask up to **5 distillation questions** to narrow open branches into single direction — batch into `AskUserQuestion` calls of up to 3 questions each (max 2 calls):

Start with these (adapt based on tree content):

1. "Which open branch best captures the core direction you want to pursue?" — list each open branch as lettered option. Note: if tree was saved and this branch doesn't already have ✓ status in file, update it to `resolved — chosen in distillation` in tree file; do not re-save file here — spec file written in D3 will reflect accepted direction.
2. "Should any remaining open branches be combined with chosen direction, or are they separate concerns?"
3. "What is the single most important success criterion for this idea?" 4–5. Ask additional questions based on gaps in open threads section or unresolved tensions between branches

After questions, briefly restate distilled direction in 2–3 sentences — synthesis of what was just decided.

#### Step D3: Write spec

Build spec section by section, showing each section inline. Write nothing to disk until full draft assembled.

Write all 6 sections inline, then invoke a single `AskUserQuestion` for the full spec:

- a) Spec looks good — write to disk ★ recommended
- b) Revise [section name(s)] — [describe what to change]
- c) A section sparks a new thought — [add context]

On **(b)**: revise named sections inline, re-present those sections, re-offer. Max 2 revisions per section. On **(c)**: incorporate context, revise if needed, re-offer.

**Sections**:

**Section 1 — Goal** (1 paragraph: what problem this solves and for whom) Derive from distilled direction from D2. Reference open branches that fed into it.

**Section 2 — Non-goals** (explicit list) Derive from closed branches and open branches not chosen in D2.

**Section 3 — Proposed design** (distilled direction with enough detail to implement) Break into sub-points. Describe *what*, not *how*. If direction is merge of multiple open branches, name each part.

**Section 4 — Open questions** (unresolved decisions) Seed from "Open threads" section of tree. For each, note blocking vs non-blocking and recommended default if possible.

**Section 5 — Success criteria** (observable, testable outcomes) Include criterion identified in D2 question 3. Each criterion must be concrete enough to write pass/fail check.

**Section 6 — Exploration notes** (summary of closed branches and why) Draw from Pruning log in tree. Context for future readers — what was considered and rejected.

**Gate**: do not write to disk until all 6 sections drafted and individually approved.

**Graduation checklist** — verify before writing to disk:

- [ ] Goal (Section 1) is concrete and names who benefits
- [ ] Proposed design (Section 3) has at least 3 distinct sub-points
- [ ] Success criteria (Section 5) are observable/testable — not vague ("it works") but checkable ("running X produces Y")
- [ ] At least one non-goal stated (Section 2 not empty)

If any item fails, call `AskUserQuestion` with:

- a) Revise failing section(s) now — return to that section in D3 ★ recommended
- b) Proceed anyway — I accept spec may be underspecified

On **(a)**: jump back to failing section in D3 (max 1 extra revision per section). On **(b)**: proceed to write.

After all sections approved: write to `.plans/blueprint/YYYY-MM-DD-<slug>.md` (new file; use tree's slug with `-spec` suffix if writing alongside tree):

```markdown
# <title>

**Date**: YYYY-MM-DD
**Status**: draft

## Goal
[Section 1]

## Non-goals
[Section 2]

## Proposed design
[Section 3]

## Open questions
[Section 4]

## Success criteria
[Section 5]

## Exploration notes
[Section 6]
```

#### Step D4: Suggest next step

After writing spec, suggest:

- **Spec targets `.claude/` config**: `/foundry:manage update <name> .plans/blueprint/<spec-file>` or `/foundry:manage create <type> <name> "description"`
- **Spec targets application code or mixed changes**: `/brainstorm breakdown .plans/blueprint/<spec-file>` to generate action plan (action plan may emit `/develop:feature` and `/develop:fix` invocations — these require the `develop` plugin)

### Action plan mode (Status: draft)

#### Step B1: Scan for blocking open questions

Read spec's "Open questions" section. For each question, determine whether **blocking** (no recommended option stated, answer genuinely unknown) or **non-blocking** (spec states recommended option or answer inferable).

For each blocking question: call `AskUserQuestion` — one at a time, in order. Non-blocking questions go into plan table footnote.

#### Step B2: Generate the action plan

**Idempotency pre-check**: before generating plan, call `TaskList` and scan for active `/develop:feature` tasks naming this spec's slug. Found → surface existing task to user, ask whether to re-generate plan (will not re-dispatch — see Step B3) or skip; do not silently double-dispatch.

1. Parse spec into discrete action items from "Proposed design" and "Success criteria"
2. For each item, write ready-to-run invocation:
   - `.claude/` config change → `/foundry:manage create <type> <name> "description"` or `/foundry:manage update <name> <spec-file>`
   - System install or shell setup → full shell command
   - Application code change → `/develop:feature "<goal>"` or `/develop:fix "<symptom>"` (requires `develop` plugin)
   - Documentation → `/develop:feature "<doc goal>"` (requires `develop` plugin)
   - Verification/testing → `/develop:feature "<test goal>"` (requires `develop` plugin) or manual check command
3. Output ordered task table:

> *Note: `/develop:feature` and `/develop:fix` require the `develop` plugin. If not installed, replace those commands with appropriate manual workflow.*

```markdown
## Action Plan: <spec title>

Spec: <file path>

| # | Task | Invocation |
|---|------|------------|
| 1 | [first action item] | `/develop:feature "<goal>"` |

### Non-blocking open questions (resolve during implementation)
- [list, or "None"]
```

#### Step B3: Post-plan prompt

**Model-invocability check**: inspect task 1's invocation from the action plan table (Step B2). If it resolves to `/foundry:manage create ...` / `/foundry:manage update ...` (or any other skill with `disable-model-invocation: true`), task 1 is **not** model-invocable — print task 1's invocation as a copy-pasteable plain-text command above the question, and omit the "Start task 1 now" option below. Otherwise task 1 is model-invocable — offer it normally.

**When task 1 is model-invocable**, call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "Plan ready. What next?"
- (a) label: `Start task 1 now` — description: proceed immediately with task 1 invocation (★ recommended)
- (b) label: `Copy plan` — description: output plan table as clean markdown block, then stop
- (c) label: `Revise spec first` — description: stop; revise spec and re-run `/brainstorm breakdown <spec>`

On **(a)** (requires `develop` plugin): before dispatching, verify no active `/develop:feature` task for this spec exists in TaskList — call `TaskList` and scan for tasks naming the spec slug or referencing `/develop:feature` against same spec file; if found, surface existing task to user and skip dispatch (prevents double-dispatch on re-entry). Otherwise proceed immediately with invocation from task 1. On **(b)**: output plan table as clean markdown block, then stop. On **(c)**: stop and tell user to revise spec and re-run `/brainstorm breakdown <spec>`.

**When task 1 is NOT model-invocable**, print task 1's invocation as a copy-pasteable command, then call `AskUserQuestion`:

- question: "Plan ready. Task 1 requires manual invocation (shown above). What next?"
- (a) label: `Copy plan` — description: output plan table as clean markdown block, then stop (★ recommended)
- (b) label: `Revise spec first` — description: stop; revise spec and re-run `/brainstorm breakdown <spec>`

On **(a)**: output plan table as clean markdown block, then stop. On **(b)**: stop and tell user to revise spec and re-run `/brainstorm breakdown <spec>`.

End with `## Confidence` block per CLAUDE.md output standards.
