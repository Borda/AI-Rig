---
name: brainstorm
description: Iterative brainstorming skill for turning fuzzy ideas into approved tree documents. Diverges into branches, deepens and prunes them over many rounds, saves a tree doc. Run breakdown on the tree to distill it into a spec via guided questions.
argument-hint: <fuzzy idea or feature goal> [--tight|--deep] [--type <type>] | breakdown <tree-or-spec-file>
disable-model-invocation: true
allowed-tools: Read, Write, Bash, Grep, Agent, TaskCreate, TaskUpdate, AskUserQuestion
effort: high
---

<objective>

Turn an unformed idea into a branching exploration tree, then distill that tree into a spec. Idea mode is pure divergence — you grow a tree of directions, deepen promising branches, prune others, and save the result. Nothing is forced to converge prematurely. Run `breakdown` on the tree when ready: it asks distillation questions and writes the spec section-by-section.

</objective>

<inputs>

- **$ARGUMENTS**: required — a fuzzy idea, goal, or feature request in any form; a single sentence is enough

Examples:

- `/brainstorm add a caching layer to the pipeline`

- `/brainstorm redesign how agents hand off to each other`

- `/brainstorm I want users to be able to export results as CSV`

- **`--tight`** — reduced-ceremony mode: cap clarifying questions at 5 (not 10), cap tree operations at 5 (not 10), require only 1 closed branch (not 2) before saving. Good for well-scoped ideas where the problem is already understood.

- **`--deep`** — extended-ceremony mode: cap clarifying questions at 15, cap tree operations at 15, require 3+ closed branches before saving. Good for genuinely ambiguous problems where more exploration is valuable.

- Default (no flag): behaviour unchanged — 10/10/2 bounds.

- **`--type <type>`** — optional type hint for idea mode. One of: `application` (app/service with users/endpoints), `workflow` (automation, pipeline, script), `utility` (helper library, tool, CLI), `config` (`.claude/` agents/skills/rules), `research` (investigation, survey, experiment design). Affects Step 1 scan patterns and Step 2 question framing. Omit if unsure — the skill works without it.

- **`breakdown <tree-or-spec-file>`** — breakdown mode: read an already-saved tree (`Status: tree`) or spec (`Status: draft`). For a tree: ask distillation questions and write the spec section-by-section. For a spec: scan for blocking open questions then generate an ordered action plan. Skips Steps 1–6 entirely.

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: Before Step 1, create TaskCreate entries for all 6 steps
(context scan, clarifying questions, build tree, save tree, tree review,
present + gate). Then print a session plan to the user:

> **Brainstorming: \<goal from $ARGUMENTS>**
> Plan: context scan → clarifying questions → build tree → save tree doc → review → approval gate.
> Starting with a codebase scan...

## Step 1: Context scan

Gather project context before asking anything:

- Read `README.md` and any relevant files under `docs/`
- Grep for keywords from `$ARGUMENTS` across `src/` or the project root
- Identify: related code that already exists, stated non-goals in docs, prior design decisions

**Type-aware scan patterns** (when `--type` is declared):

- `application`: look for existing routes, controllers, components, API endpoints, auth middleware
- `workflow`: look for existing scripts, pipelines, CI configs, scheduled jobs, automation files
- `utility`: look for existing utils/, helpers/, lib/ directories and similar functions
- `config`: look for `.claude/` agents, skills, rules, and `settings.json` entries
- `research`: look for existing notes, benchmarks, prior experiment results, and related papers/tickets

When no `--type` is declared, perform the generic scan as before.

Goal: understand constraints so questions are targeted, not generic. If the idea already exists or is clearly out of scope, say so immediately and stop.

## Step 2: Clarifying questions

Use `AskUserQuestion` for every clarifying question — this renders an interactive prompt inline, not plain text.

Rules:

- Ask **one question at a time** — call `AskUserQuestion` once, wait for the answer, then decide whether another question is needed
- Always use **multiple-choice** options in `AskUserQuestion`: list lettered choices so the user can reply with just "a", "b", or "c"; mark the option you recommend with **★** (e.g., `a) Option A ★ recommended`) so the user has a sensible default
- Maximum **10 questions** (5 in `--tight` mode, 15 in `--deep` mode) — after the limit, proceed to Step 3 with what you have
- After question 3 (and on every subsequent question), always include an **escape hatch option**: `x) Enough questions — let's start building the tree` so the user can move on if the problem is already well-defined
- No solution proposals during this step — only gather information
- After each answer, briefly restate the updated problem understanding in 1–2 sentences before asking the next question or proceeding — a simple acknowledgment ("Got it", "Understood") does not count; the restatement must name what is now known about the problem (e.g., "So the goal is X and the constraint is Y.")

**Gate**: do not proceed to Step 3 until the problem is well-defined or the maximum question count is reached. Aim for at least 3 questions to build enough context for a rich tree.

**Type-aware question framing** (when `--type` is declared): lead with type-appropriate questions first:

- `application`: ask about users (who uses it?), scale, and integration points before general questions
- `workflow`: ask about triggers (what starts it?), inputs, outputs, and failure handling first
- `utility`: ask about callers (who uses this library/tool?), interface shape, and scope of responsibility
- `config`: ask whether this targets an existing agent/skill or is new, and what gap it fills in the current setup
- `research`: ask about the hypothesis or question being investigated, and what constitutes a useful finding

## Step 3: Build the tree

This step is the full creative session — grow, deepen, and prune a tree of directions. The tree is the output; convergence happens later in `breakdown`. Runs as a loop of **tree operations**.

### Seeding the tree

Present **3–5 initial branches** (top-level directions). For each include:

- **Name**: short label
- **Core idea**: 2–3 sentences — what makes this branch distinct
- **Tension it resolves**: which aspect of the problem does this branch prioritise?
- **What it trades away**: what gets harder or is left unsolved?

After presenting all initial branches, call `AskUserQuestion` with one letter per branch (labelled by name), plus:

- f) None of these — describe what you're thinking
- g) Add more initial branches — I want more angles

On **(f)**: ask what direction the user is thinking, then generate 2–3 new branches incorporating it.
On **(g)**: generate 2–3 fresh branches with genuinely different framing.

User may select **1–3 branches** to mark as initial focus. All other branches start as [open] too — they are not closed yet, just not the initial focus.

### Tree operations loop

After seeding, enter the operations loop. Each iteration:

1. Show the current **tree summary** (see format below)
2. Call `AskUserQuestion` with the available operations:
   - a) Deepen a branch — add sub-branches to [branch name]
   - b) Close a branch — mark [branch name] as closed with a reason
   - c) Merge two branches — combine [branch name] + [branch name]
   - d) Add a new top-level branch — explore a different angle
   - e) Reopen a closed branch — reconsider [branch name]
   - f) Ready — save tree and proceed

**Operations**:

- **Deepen**: generate 2–3 sub-branches under the named branch. Sub-branches use the same format as top-level branches. Ask which one(s) to focus on.
- **Close**: mark the named branch as `[closed — <user's reason>]`. Add a one-line entry to the pruning log. Ask if the reason captures it correctly before proceeding.
- **Merge**: synthesise two named branches into a single hybrid branch; present the merged description; mark the original two as `[merged into <new name>]` immediately in the tree summary shown after the merge, and in all subsequent tree summaries.
- **Add**: generate 1–2 fresh top-level branches with directions not yet represented in the tree.
- **Reopen**: change `[closed]` to `[open]` on the named branch; note the re-opening reason.
- **Ready**: exit the loop, proceed to Step 4.

### Tree summary format

Always show the tree summary **before** calling `AskUserQuestion`:

```
Tree: <title>
├─ Branch 1: <name> [open]
│  ├─ 1.1: <name> [open]
│  └─ 1.2: <name> [closed — <reason>]
├─ Branch 2: <name> [open]
│  ├─ 2.1: <name> [open]
│  │  ├─ 2.1.1: <name> [open]
│  │  └─ 2.1.2: <name> [closed — <reason>]
│  └─ 2.2: <name> [closed — <reason>]
└─ Branch 3: <name> [closed — <reason>]
Open: N | Closed: N | Merged: N
```

Use `├─`, `│  ├─`, `└─` for tree rendering. Show sub-branches indented one level per depth. Sub-branches use hierarchical dot notation: branch 2 splits into 2.1, 2.2, …; those can further split into 2.1.1, 2.1.2, … Closed branches show their reason inline.

### Loop bounds

- Maximum **10 operations** (5 in `--tight` mode, 15 in `--deep` mode) (a round = one operation)
- After the limit: show tree state, call `AskUserQuestion` with: a) Save tree as-is ★ recommended / b) Do 2 more operations then save
- **Gate**: do not proceed to Step 4 until the user selects "Ready" or the max is reached with at least 2 closed branches (1 in `--tight`, 3 in `--deep`); if fewer than the required closed branches exist, prompt: "The tree has few closed branches — consider closing 1–2 that are clearly not the right direction before saving."

## Step 4: Save tree

Assemble the tree state and write to `.plans/blueprint/YYYY-MM-DD-<slug>.md` using the Write tool (creates the directory if absent). The slug is derived from the title (kebab-case, max 5 words). If a file already exists at the target path (e.g., same day, same slug after a restart), append a counter suffix (`-2`, `-3`, etc.) rather than overwriting.

```markdown
# <title>

**Date**: YYYY-MM-DD
**Status**: tree

## Root idea

[The original user input and the refined problem understanding built up during Step 2 — 2–3 sentences.]

## Branches

[For each branch in the tree, using this structure:]

### Branch N: <name> [open | closed — <reason> | merged into <name>]

**Core idea**: ...
**Tension it resolves**: ...
**What it trades away**: ...

[Sub-branches nested as H4 headings if present:]

#### N.1: <sub-branch name> [open | closed — <reason>]

**Core idea**: ...

[Deeper splits nest as H5 headings, e.g. `##### N.1.1: <name>`]

## Pruning log

- Branch N closed: <reason>
- Sub-branch N.1 closed: <reason>
[One bullet per closed or merged branch, in the order they were closed.]

## Open threads

[Unanswered questions, untested combinations, and constraints that surfaced during Step 3. Each thread is a one-line bullet.]
```

**Gate**: do not proceed to Step 5 until the file is written and the path is confirmed.

## Step 5: Tree review

Before spawning, pre-compute the output path:

```bash
# timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')
OUTPUT_PATH=".temp/output-brainstorm-review-$BRANCH-$(date +%Y-%m-%d).md"
```

Spawn **self-mentor** with a tree-focused prompt (inject the pre-computed `$OUTPUT_PATH` in place of `<output-path>`):

```
Read .plans/blueprint/<tree-file>. Audit for tree quality only (do NOT audit `.claude/` config files — scope is the brainstorm tree only):
- Root idea: is the original problem clearly stated in the "Root idea" section?
- Branch depth: do open branches have enough detail (not just a name)?
- Closure quality: are closure reasons substantive (not just "not chosen" or "skipped")?
- Coverage: are there obvious high-level directions completely missing from the tree?
- Open threads: are there unresolved questions worth capturing?
Write your full findings to <output-path> using the Write tool.
Return ONLY a compact JSON envelope: {"status":"done","findings":N,"file":"<path>","confidence":0.N,"summary":"<one-line>"}
```

**Passive health monitoring**: the Agent tool is synchronous — Claude awaits self-mentor's response natively. If self-mentor does not return within 15 min, surface any partial output already written to `.temp/` with a ⏱ marker and continue to Step 6 with an incomplete review noted.

# Note: synchronous Agent calls do not support mid-call extensions per CLAUDE.md §8 — simplified monitoring is intentional for synchronous spawns.

If `findings > 0`: add missing details, improve closure reasons, or add open threads as needed — loop back to Step 5 (max 2 revision cycles). After 2 cycles with remaining findings, surface unresolved issues to the user and proceed to Step 6 anyway.

**Gate**: do not proceed to Step 6 until `findings == 0` or 2 revision cycles are exhausted.

## Step 6: Present and gate

Show the tree file path and a compact tree summary (same format as Step 3). Then call `AskUserQuestion` with:

- (a) Tree looks good — ready to distill ★ recommended
- (b) Needs more exploration — [describe what to add or close]
- (c) Start over — back to clarifying questions

**Gate**: do not exit until the user approves. On (b): return to Step 3 with the existing tree state — add the requested branches or close the specified ones, then loop back to Step 5. Use a reduced cap of **3 additional operations** for this re-entry (not a fresh full budget reset); the cap resets only at the start of Step 3, not on re-entry. On (c): loop back to Step 2. (Max 3 approval cycles — after 3 (b) responses with no convergence, surface unresolved concerns to user and stop.)

On approval, suggest: `/brainstorm breakdown .plans/blueprint/<file>` to distill the tree into a spec.

## Mode: Breakdown

Triggered when `$ARGUMENTS` starts with `breakdown ` followed by a file path.

Read the file at the given path. Check the `**Status**:` field:

- `Status: tree` → **Distillation mode** (Steps D1–D4 below)
- `Status: draft` → **Action plan mode** (Steps B1–B3 below)

______________________________________________________________________

### Distillation mode (Status: tree)

#### Step D1: Present tree summary

Read all open branches from the file. Show the compact tree summary (same format as Step 3) and a one-sentence description of each open branch. State the count of open and closed branches.

#### Step D2: Distillation questions

Ask up to **5 distillation questions**, one at a time via `AskUserQuestion`, to narrow the open branches into a single direction:

Start with these (adapt based on the tree content):

1. "Which open branch best captures the core direction you want to pursue?" — list each open branch as a lettered option
2. "Should any of the remaining open branches be combined with the chosen direction, or are they separate concerns?"
3. "What is the single most important success criterion for this idea?"
   4–5. Ask additional questions based on gaps in the open threads section or unresolved tensions between branches

After the questions, briefly restate the distilled direction in 2–3 sentences — the synthesis of what was just decided.

#### Step D3: Write spec

Build the spec section by section, showing each section inline and asking for feedback before moving on. Write nothing to disk until the full draft is assembled.

For each section, write it inline then call `AskUserQuestion`:

- a) Looks good — next section ★ recommended
- b) Change this — [describe what to revise]
- c) This section sparks a new thought — [add context]

On **(b)**: revise inline, show updated version, re-offer. Max 2 revisions per section.
On **(c)**: incorporate context, revise if needed, re-offer.

**Sections**:

**Section 1 — Goal** (1 paragraph: what problem this solves and for whom)
Derive from the distilled direction from D2. Reference the open branches that fed into it.

**Section 2 — Non-goals** (explicit list)
Derive from closed branches and the open branches not chosen in D2.

**Section 3 — Proposed design** (the distilled direction with enough detail to implement)
Break into sub-points. Describe *what*, not *how*. If the direction is a merge of multiple open branches, name each part.

**Section 4 — Open questions** (unresolved decisions)
Seed from the "Open threads" section of the tree. For each, note blocking vs non-blocking and a recommended default if possible.

**Section 5 — Success criteria** (observable, testable outcomes)
Include the criterion identified in D2 question 3. Each criterion must be concrete enough to write a pass/fail check.

**Section 6 — Exploration notes** (summary of closed branches and why)
Draw from the Pruning log in the tree. This is context for future readers — what was considered and rejected.

**Gate**: do not write to disk until all 6 sections are drafted and individually approved.

**Graduation checklist** — verify before writing to disk:

- [ ] Goal (Section 1) is concrete and names who benefits
- [ ] Proposed design (Section 3) has at least 3 distinct sub-points
- [ ] Success criteria (Section 5) are observable/testable — not vague ("it works") but checkable ("running X produces Y")
- [ ] At least one non-goal is stated (Section 2 is not empty)

If any item fails, call `AskUserQuestion` with:

- a) Revise the failing section(s) now — return to that section in D3 ★ recommended
- b) Proceed anyway — I accept the spec may be underspecified

On **(a)**: jump back to the failing section in D3 (max 1 extra revision per section).
On **(b)**: proceed to write.

After all sections approved: write to `.plans/blueprint/YYYY-MM-DD-<slug>.md` (new file; use the tree's slug with a `-spec` suffix if writing alongside the tree):

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

After writing the spec, suggest:

- **Spec targets `.claude/` config**: `/manage update <name> .plans/blueprint/<spec-file>` or `/manage create <type> <name> "description"`
- **Spec targets application code or mixed changes**: `/brainstorm breakdown .plans/blueprint/<spec-file>` to generate the action plan

______________________________________________________________________

### Action plan mode (Status: draft)

#### Step B1: Scan for blocking open questions

Read the spec's "Open questions" section. For each question, determine whether it is **blocking** (no recommended option stated and the answer is genuinely unknown) or **non-blocking** (spec states a recommended option or the answer is inferable).

For each blocking question: call `AskUserQuestion` — one at a time, in order. Non-blocking questions go into the plan table footnote.

#### Step B2: Generate the action plan

1. Parse the spec into discrete action items from "Proposed design" and "Success criteria"
2. For each item, write a ready-to-run invocation:
   - `.claude/` config change → `/manage create <type> <name> "description"` or `/manage update <name> <spec-file>`
   - System install or shell setup → full shell command
   - Application code change → `/develop feature "<goal>"` or `/develop fix "<symptom>"`
   - Documentation → `/develop feature "<doc goal>"`
   - Verification/testing → `/develop feature "<test goal>"` or manual check command
3. Output an ordered task table:

```
## Action Plan: <spec title>

Spec: <file path>

| # | Task | Invocation |
|---|------|------------|
| 1 | ... | `...` |

### Non-blocking open questions (resolve during implementation)
- [list, or "None"]
```

#### Step B3: Post-plan prompt

Call `AskUserQuestion` with:

- (a) Start task 1 now ★ recommended
- (b) Copy plan and pass to another agent
- (c) Revise spec first

On **(a)**: proceed immediately with the invocation from task 1.
On **(b)**: output the plan table as a clean markdown block, then stop.
On **(c)**: stop and tell the user to revise the spec and re-run `/brainstorm breakdown <spec>`.

End with a `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- **No code at any point** — this skill produces tree documents and specs only; implementation is out of scope
- **`disable-model-invocation: true`** — the skill is conversational; the parent model drives all steps turn by turn
- **self-mentor scope in Step 5** — the spawn prompt must constrain scope to tree quality explicitly; do not let it audit `.claude/` config files
- **.plans/blueprint/ directory** — created if absent; filenames use `YYYY-MM-DD-<kebab-slug>.md` format; tree files use the base slug; spec files append `-spec` to the slug to avoid collision
- **Status field**: tree documents use `Status: tree`; spec documents use `Status: draft`; breakdown auto-detects which path to take
- **Breakdown heading convention**: distillation mode uses D-prefix steps (D1–D4); action plan mode uses B-prefix steps (B1–B3)
- **Exploration notes in spec**: Section 6 is derived from the tree's Pruning log — it is intentional context for future readers and should not be removed by self-mentor review
- **Interaction budget**: idea mode — worst case: 13 (`--tight`) / 23 (default) / 33 (`--deep`) questions + operations + 3 approval cycles; breakdown distillation — max 5 questions + 6 section drafts ≈ 11; typical sessions use ~8–15 total AskUserQuestion calls across both
- **Flag modes**: `--tight` / `--deep` scale question and operation caps (5/15 vs default 10); `--type` enables type-aware scan and question framing in Steps 1–2; these flags apply to idea mode only and are ignored in breakdown
- **Follow-up**: after spec approval in distillation mode → if targeting `.claude/` config: `/manage update <name> <spec-file>`; for application or mixed changes: `/brainstorm breakdown .plans/blueprint/<spec-file>` for the action plan

</notes>
