<!-- file: breakdown.md — consumers: brainstorm/SKILL.md -->

## Mode: Breakdown

Triggered when `$ARGUMENTS` starts with `breakdown ` followed by file path.

Read file at given path. Check `**Status**:` field:

- `Status: tree` → **Distillation mode** (Steps D1–D4 below)
- `Status: draft` → **Action plan mode** (Steps B1–B3 below)

### Distillation mode (Status: tree)

#### Step D1: Present tree summary

Read all open branches from file. Build compact tree summary (same format as Step 3) and one-sentence description of each open branch, with count of open and closed branches — shown only as every option's `preview` on D2's single-select questions, never also as reply text (SKILL.md §Tree summary format).

#### Step D2: Distillation questions

Ask up to **5 distillation questions** to narrow open branches into single direction — batch into `AskUserQuestion` calls of up to 3 questions each (max 2 calls):

Start with these (adapt based on tree content):

1. "Which open branch best captures the core direction you want to pursue?" — list each open branch as lettered option. Note: if tree was saved and this branch doesn't already have ✓ status in file, update it to `resolved — chosen in distillation` in tree file; do not re-save file here — spec file written in D3 will reflect accepted direction.
2. "Should any remaining open branches be combined with chosen direction, or are they separate concerns?"
3. "What is the single most important success criterion for this idea?" 4–5. Ask additional questions based on gaps in open threads section or unresolved tensions between branches

After questions, briefly restate distilled direction in 2–3 sentences — synthesis of what was just decided.

#### Step D3: Write spec

Build spec section by section into one assembled draft — it reaches the user only as the `preview` of the D3 question below, never as reply text (text before a tool call can arrive as an empty progress update). Write nothing to disk until full draft assembled. **Preview cap**: ≤2000 chars and ≤12 lines per preview, every line counted — Claude Code withholds a longer preview and clips a taller one, no scroll; a 6-section spec nearly always breaks it. Over the cap: Write the assembled draft to `.reports/brainstorm/<slug>-spec-draft.md` first — a scratch copy for reading, not the spec — every D3 option's `preview` is a compact summary (the Goal sentence, then one line per section) ending `→ full draft: <path>`, and the D3 question text names that same path.

Once all 6 sections are assembled, invoke a single single-select `AskUserQuestion` for the full spec, the assembled draft (all 6 sections) as the `preview` of every option — or, over the preview cap, the compact summary naming the draft file:

- a) Spec looks good — write to disk ★ recommended
- b) Revise [section name(s)] — [describe what to change]
- c) A section sparks a new thought — [add context]

On **(b)**: revise named sections, re-offer with the revised draft as the `preview` of every option (over the cap: rewrite the draft file, refresh the summary). Max 2 revisions per section. On **(c)**: incorporate context, revise if needed, re-offer the same way.

**Sections**:

**Section 1 — Goal** (1 paragraph: what problem this solves and for whom) Derive from distilled direction from D2. Reference open branches that fed into it.

**Section 2 — Non-goals** (explicit list) Derive from closed branches and open branches not chosen in D2.

**Section 3 — Proposed design** (distilled direction with enough detail to implement) Break into sub-points. Describe *what*, not *how*. If direction is merge of multiple open branches, name each part.

**Section 4 — Open questions** (unresolved decisions) Seed from "Open threads" section of tree. For each, note blocking vs non-blocking and recommended default if possible.

**Section 5 — Success criteria** (observable, testable outcomes) Include criterion identified in D2 question 3. Each criterion must be concrete enough to write pass/fail check.

**Section 6 — Exploration notes** (summary of closed branches and why) Draw from Pruning log in tree. Context for future readers — what was considered and rejected.

**Gate**: do not write the spec to `.plans/blueprint/` until all 6 sections drafted and individually approved — the over-cap `.reports/` draft copy is only the preview's full text, never the spec.

**Graduation checklist** — verify before writing to disk:

- [ ] Goal (Section 1) is concrete and names who benefits
- [ ] Proposed design (Section 3) has at least 3 distinct sub-points
- [ ] Success criteria (Section 5) are observable/testable — not vague ("it works") but checkable ("running X produces Y")
- [ ] At least one non-goal stated (Section 2 not empty)

If any item fails, call `AskUserQuestion`, its question text naming each failing item, with:

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

**Idempotency pre-check**: before generating plan, call `TaskList` and scan for active `/develop:feature` tasks naming this spec's slug. Found → `AskUserQuestion`, its question text naming the existing task (subject + status): re-generate plan (will not re-dispatch — see Step B3) or skip; do not silently double-dispatch.

1. Parse spec into discrete action items from "Proposed design" and "Success criteria"
2. For each item, write ready-to-run invocation:
   - `.claude/` config change → `/foundry:manage create <type> <name> "description"` or `/foundry:manage update <name> <spec-file>`
   - System install or shell setup → full shell command
   - Application code change → `/develop:feature "<goal>"` or `/develop:fix "<symptom>"` (requires `develop` plugin)
   - Documentation → `/develop:feature "<doc goal>"` (requires `develop` plugin)
   - Verification/testing → `/develop:feature "<test goal>"` (requires `develop` plugin) or manual check command
3. Build ordered task table — shown only as the `preview` of every option of Step B3's question, never also as reply text before the call (it can arrive as an empty progress update). Over the preview cap (≤2000 chars, ≤12 lines; a plan past ~6 tasks breaks it): Write it to `.reports/brainstorm/<slug>-plan.md` first and make every B3 preview a compact summary — task count, task 1's invocation — ending `→ full plan: <path>`, the B3 question text naming that same path:

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

**Model-invocability check**: inspect task 1's invocation from the action plan table (Step B2). If it resolves to `/foundry:manage create ...` / `/foundry:manage update ...` (or any other skill with `disable-model-invocation: true`), task 1 is **not** model-invocable — carry task 1's invocation in the question text, and omit the "Start task 1 now" option below. Otherwise task 1 is model-invocable — offer it normally.

Both variants below are single-select, and the action plan from Step B2 is the `preview` of every option — or, over the preview cap, B2's compact summary naming the plan file, which the question text then names too.

**When task 1 is model-invocable**, call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments, the action plan table as the `preview` of every option:

- question: "Plan ready. What next?"
- (a) label: `Start task 1 now` — description: proceed immediately with task 1 invocation (★ recommended)
- (b) label: `Copy plan` — description: output plan table as clean markdown block, then stop
- (c) label: `Revise spec first` — description: stop; revise spec and re-run `/brainstorm breakdown <spec>`

On **(a)** (requires `develop` plugin): before dispatching, verify no active `/develop:feature` task for this spec exists in TaskList — call `TaskList` and scan for tasks naming the spec slug or referencing `/develop:feature` against same spec file; if found, surface existing task to user and skip dispatch (prevents double-dispatch on re-entry). Otherwise proceed immediately with invocation from task 1. On **(b)**: output plan table as clean markdown block, then stop. On **(c)**: stop and tell user to revise spec and re-run `/brainstorm breakdown <spec>`.

**When task 1 is NOT model-invocable**, call `AskUserQuestion` with task 1's invocation in the question text and the action plan table as the `preview` of every option — neither printed as reply text before the call, which can arrive as an empty progress update:

- question: "Plan ready. Task 1 requires manual invocation: `<task 1 invocation>`. What next?"
- (a) label: `Copy plan` — description: output plan table as clean markdown block, then stop (★ recommended)
- (b) label: `Revise spec first` — description: stop; revise spec and re-run `/brainstorm breakdown <spec>`

On **(a)**: output plan table as clean markdown block, then stop. On **(b)**: stop and tell user to revise spec and re-run `/brainstorm breakdown <spec>`. Either way, end the final reply (after the last tool call) with task 1's invocation as a copy-pasteable command.

End with `## Confidence` block per CLAUDE.md output standards.
