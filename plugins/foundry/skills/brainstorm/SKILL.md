---
name: brainstorm
description: Iterative brainstorming skill for turning fuzzy ideas into approved tree documents. Diverges into branches, deepens and prunes them over many rounds, saves a tree doc. Run breakdown on the tree to distill it into a spec via guided questions.
argument-hint: <fuzzy idea or feature goal> [--tight|--deep] [--type <type>] | breakdown <tree-or-spec-file>
disable-model-invocation: true
allowed-tools: Read, Write, Bash, Grep, Agent, TaskCreate, TaskUpdate, AskUserQuestion
effort: high
---

<objective>

Turn unformed idea into branching exploration tree, then distill into spec. Idea mode = pure divergence — grow tree of directions, deepen promising branches, prune others, save result. No premature convergence. Run `breakdown` on tree when ready: asks distillation questions, writes spec section-by-section.

</objective>

<inputs>

- **$ARGUMENTS**: required — fuzzy idea, goal, or feature request in any form; one sentence enough

Examples:

- `/brainstorm I want users to be able to export results as CSV`

- **`--tight`** — reduced-ceremony mode: cap clarifying questions at 5 (not 10), cap tree operations at 5 (not 10), require only 1 closed branch (not 2) before saving. Good for well-scoped ideas where problem already understood.

- **`--deep`** — extended-ceremony mode: cap clarifying questions at 15, cap tree operations at 15, require 3+ closed branches before saving. Good for genuinely ambiguous problems where more exploration valuable.

- Default (no flag): behaviour unchanged — 10/10/2 bounds.

- **`--type <type>`** — optional type hint for idea mode. One of: `application` (app/service with users/endpoints), `workflow` (automation, pipeline, script), `utility` (helper library, tool, CLI), `config` (`.claude/` agents/skills/rules), `research` (investigation, survey, experiment design). Affects Step 1 scan patterns and Step 2 question framing. Omit if unsure — skill works without it.

- **`breakdown <tree-or-spec-file>`** — breakdown mode: read already-saved tree (`Status: tree`) or spec (`Status: draft`). For tree: ask distillation questions, write spec section-by-section. For spec: scan for blocking open questions then generate ordered action plan. Skips Steps 1–6 entirely.

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: Before Step 1, create TaskCreate entries for all 6 steps (context scan, clarifying questions, build tree, save tree, tree review, present + gate). Then print session plan to user:

> **Brainstorming: \<goal from $ARGUMENTS>** Plan: context scan → clarifying questions → build tree → save tree doc → review → approval gate. Starting with a codebase scan...

## Step 1: Context scan

Gather project context before asking anything:

- Read `README.md` and relevant files under `docs/`
- Grep for keywords from `$ARGUMENTS` across `src/` or project root
- Identify: related code that already exists, stated non-goals in docs, prior design decisions

**Type-aware scan patterns** (when `--type` declared):

- `application`: look for existing routes, controllers, components, API endpoints, auth middleware
- `workflow`: look for existing scripts, pipelines, CI configs, scheduled jobs, automation files
- `utility`: look for existing utils/, helpers/, lib/ directories and similar functions
- `config`: look for `.claude/` agents, skills, rules, and `settings.json` entries
- `research`: look for existing notes, benchmarks, prior experiment results, and related papers/tickets

When no `--type` declared, perform generic scan as before.

Goal: understand constraints so questions targeted, not generic. If idea already exists or clearly out of scope, say so immediately and stop.

## Step 2: Clarifying questions

Use `AskUserQuestion` for every clarifying question — renders interactive prompt inline, not plain text.

Rules:

- Ask **one question at a time** — call `AskUserQuestion` once, wait for answer, then decide if another question needed
- Always use **multiple-choice** options in `AskUserQuestion`: list lettered choices so user can reply with just "a", "b", or "c"; mark recommended option with **★** (e.g., `a) Option A ★ recommended`) so user has sensible default
- Maximum **10 questions** (5 in `--tight` mode, 15 in `--deep` mode) — after limit, proceed to Step 3 with what you have
- After question 3 (and every subsequent question), always include **escape hatch option**: `x) Enough questions — let's start building the tree` so user can move on if problem already well-defined
- No solution proposals during this step — only gather information
- After each answer, briefly restate updated problem understanding in 1–2 sentences before asking next question or proceeding — simple acknowledgment ("Got it", "Understood") does not count; restatement must name what is now known about problem (e.g., "So the goal is X and the constraint is Y.")
- After restatement, add skill's own perspective in blockquote labelled **Skill's read:** — 1–2 sentences on what directions this answer opens up, what it makes more or less likely, or what tension it surfaces. Active hypothesis, not neutral summary (e.g., `> **Skill's read:** This makes me think the core challenge is X, which points toward approaches like Y`). Write as skill speaking.

**Gate**: do not proceed to Step 3 until problem well-defined or maximum question count reached. Aim for at least 3 questions to build enough context for rich tree.

**Type-aware question framing** (when `--type` declared): lead with type-appropriate questions first:

- `application`: ask about users (who uses it?), scale, and integration points before general questions
- `workflow`: ask about triggers (what starts it?), inputs, outputs, and failure handling first
- `utility`: ask about callers (who uses this library/tool?), interface shape, and scope of responsibility
- `config`: ask whether this targets existing agent/skill or is new, and what gap it fills in current setup
- `research`: ask about hypothesis or question being investigated, and what constitutes useful finding

## Step 3: Build the tree

Full creative session — grow, deepen, and prune tree of directions. Tree is output; convergence happens later in `breakdown`. Runs as loop of **tree operations**.

### Seeding the tree

Present **3–5 initial branches** (top-level directions). For each include:

- **Name**: short label
- **Core idea**: 2–3 sentences — what makes this branch distinct
- **Tension it resolves**: which aspect of problem this branch prioritises
- **What it trades away**: what gets harder or left unsolved
- **Skill's lean**: short honest opinion — what makes branch interesting or worth exploring, and any reservation skill has about it (e.g., "Interesting because it sidesteps the auth problem entirely, but risky if the data model isn't flexible.")

After presenting all initial branches, write **Opening framing** paragraph (2–3 sentences) sharing skill's initial read on problem space: what it sees as core tension, which branch(es) it finds most promising and why, and one thing it's uncertain about. Not recommendation to converge — divergence still goal — but honest perspective to spark reaction.

Then call `AskUserQuestion` with one letter per branch (labelled by name), plus:

- f) None of these — describe what you're thinking
- g) Add more initial branches — I want more angles

On **(f)**: ask what direction user is thinking, then generate 2–3 new branches incorporating it. On **(g)**: generate 2–3 fresh branches with genuinely different framing.

User may select **1–3 branches** to mark as initial focus. All other branches start as [open] too — not closed yet, just not initial focus.

### Tree operations loop

After seeding, enter operations loop. Each iteration:

1. Show current **tree summary** (see format below)
2. Write **Skill's moment** — 2–3 sentences of skill's current read: which open branches look most interesting and why, what closed branches revealed about problem, and what skill would explore next if it had a vote. Make specific to current tree state (refer to actual branch names by their labels). Gives user something to react to before choosing operation.
3. Call `AskUserQuestion` with available operations:
   - a) Deepen a branch — add sub-branches to [branch name]
   - b) Close a branch — mark [branch name] as closed with a reason
   - c) Merge two branches — combine [branch name] + [branch name]
   - d) Add a new top-level branch — explore a different angle
   - e) Reopen a closed branch — reconsider [branch name]
   - f) Ready — save tree and proceed

**Operations**:

- **Deepen**: generate 2–3 sub-branches under named branch. Sub-branches use same format as top-level branches. Ask which one(s) to focus on. After executing, write 1–2 sentences reacting to what deepening this branch opens up — what new tensions or opportunities sub-branches reveal.
- **Close**: mark named branch as ⛔ (closed) with user's reason shown after `—`. Add one-line entry to pruning log. Ask if reason captures it correctly before proceeding. After executing, write 1–2 sentences reacting to what closing this branch reveals — what it tells us about where exploration is actually headed.
- **Merge**: synthesise two named branches into single hybrid branch; present merged description; mark originals as 🔗 with `[merged -> <number>: <new-branch-name>]` immediately in tree summary shown after merge, and in all subsequent tree summaries. After executing, write 1–2 sentences on what merge suggests about where idea is heading — what synthesis makes clearer or harder.
- **Add**: generate 1–2 fresh top-level branches with directions not yet represented in tree. After executing, write 1–2 sentences on why new angle matters — what gap it fills or what it challenges in existing branches.
- **Reopen**: change ⛔ (closed) to 💭 (open) on named branch; note re-opening reason. After executing, write 1–2 sentences on what reopening this branch might change — what it puts back on table.
- **Ready**: exit loop, proceed to Step 4.

### Tree summary format

Always show tree summary **before** calling `AskUserQuestion`:

```
Tree: <title>
├─ ▶️ Branch 1: <name>
│  ├─ 💭 1.1: <name>
│  └─ ⛔ 1.2: <name> — <reason>
├─ 💭 Branch 2: <name>
│  ├─ ▶️ 2.1: <name>
│  │  ├─ 💭 2.1.1: <name>
│  │  └─ ⛔ 2.1.2: <name> — <reason>
│  └─ ⛔ 2.2: <name> — <reason>
└─ 🔗 Branch 3: <name> [merged -> <number>: <new-branch-name>]
Open: N · Closed: N · Merged: N
Legend: ▶️ active focus · 💭 open · ⛔ closed · 🔗 merged
```

Use `├─`, `│  ├─`, `└─` for tree rendering. Show sub-branches indented one level per depth. Sub-branches use hierarchical dot notation: branch 2 splits into 2.1, 2.2, …; those split further into 2.1.1, 2.1.2, … Prefix each branch with status emoji: ▶️ for branch currently operated on (most recently deepened, or selected as initial focus during seeding), 💭 for all other open branches, ⛔ for closed branches (show reason after `—`), 🔗 for merged branches (show merge target as `[merged -> <number>: <new-branch-name>]`). Legend line always last.

### Loop bounds

- Maximum **10 operations** (5 in `--tight` mode, 15 in `--deep` mode) (round = one operation)
- After limit: show tree state, call `AskUserQuestion` with: a) Save tree as-is ★ recommended / b) Do 2 more operations then save
- **Gate**: do not proceed to Step 4 until user selects "Ready" or max reached with at least 2 closed branches (1 in `--tight`, 3 in `--deep`); if fewer than required closed branches exist, prompt: "The tree has few closed branches — consider closing 1–2 that are clearly not the right direction before saving."

## Step 4: Save tree

Assemble tree state and write to `.plans/blueprint/YYYY-MM-DD-<slug>.md` using Write tool (creates directory if absent). Slug derived from title (kebab-case, max 5 words). If file already exists at target path (e.g., same day, same slug after restart), append counter suffix (`-2`, `-3`, etc.) rather than overwriting.

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

**Gate**: do not proceed to Step 5 until file written and path confirmed.

## Step 5: Tree review

Before spawning, pre-compute output path:

```bash
# timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')
OUTPUT_PATH=".temp/output-brainstorm-review-$BRANCH-$(date +%Y-%m-%d).md"
```

Spawn **foundry:self-mentor** with tree-focused prompt (inject pre-computed `$OUTPUT_PATH` in place of `<output-path>`):

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

**Passive health monitoring**: Agent tool is synchronous — Claude awaits self-mentor's response natively. If self-mentor does not return within 15 min, surface any partial output already written to `.temp/` with ⏱ marker and continue to Step 6 with incomplete review noted.

> Note: synchronous Agent calls do not support mid-call extensions per CLAUDE.md §8 — simplified monitoring is intentional for synchronous spawns.

If `findings > 0`: add missing details, improve closure reasons, or add open threads as needed — loop back to Step 5 (max 2 revision cycles). After 2 cycles with remaining findings, surface unresolved issues to user and proceed to Step 6 anyway.

**Gate**: do not proceed to Step 6 until `findings == 0` or 2 revision cycles exhausted.

## Step 6: Present and gate

Show tree file path and compact tree summary (same format as Step 3). Then call `AskUserQuestion` with:

- (a) Tree looks good — ready to distill ★ recommended
- (b) Needs more exploration — [describe what to add or close]
- (c) Start over — back to clarifying questions

**Gate**: do not exit until user approves. On (b): return to Step 3 with existing tree state — add requested branches or close specified ones, then loop back to Step 5. Use reduced cap of **3 additional operations** for this re-entry (not fresh full budget reset); cap resets only at start of Step 3, not on re-entry. On (c): loop back to Step 2. (Max 3 approval cycles — after 3 (b) responses with no convergence, surface unresolved concerns to user and stop.)

On approval, suggest: `/brainstorm breakdown .plans/blueprint/<file>` to distill tree into spec.

## Mode: Breakdown

Triggered when `$ARGUMENTS` starts with `breakdown ` followed by file path.

Read file at given path. Check `**Status**:` field:

- `Status: tree` → **Distillation mode** (Steps D1–D4 below)
- `Status: draft` → **Action plan mode** (Steps B1–B3 below)

______________________________________________________________________

### Distillation mode (Status: tree)

#### Step D1: Present tree summary

Read all open branches from file. Show compact tree summary (same format as Step 3) and one-sentence description of each open branch. State count of open and closed branches.

#### Step D2: Distillation questions

Ask up to **5 distillation questions**, one at a time via `AskUserQuestion`, to narrow open branches into single direction:

Start with these (adapt based on tree content):

1. "Which open branch best captures the core direction you want to pursue?" — list each open branch as lettered option
2. "Should any of the remaining open branches be combined with the chosen direction, or are they separate concerns?"
3. "What is the single most important success criterion for this idea?"
4–5. Ask additional questions based on gaps in open threads section or unresolved tensions between branches

After questions, briefly restate distilled direction in 2–3 sentences — synthesis of what was just decided.

#### Step D3: Write spec

Build spec section by section, showing each section inline and asking for feedback before moving on. Write nothing to disk until full draft assembled.

For each section, write inline then call `AskUserQuestion`:

- a) Looks good — next section ★ recommended
- b) Change this — [describe what to revise]
- c) This section sparks a new thought — [add context]

On **(b)**: revise inline, show updated version, re-offer. Max 2 revisions per section. On **(c)**: incorporate context, revise if needed, re-offer.

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

- **Spec targets `.claude/` config**: `/manage update <name> .plans/blueprint/<spec-file>` or `/manage create <type> <name> "description"`
- **Spec targets application code or mixed changes**: `/brainstorm breakdown .plans/blueprint/<spec-file>` to generate action plan

______________________________________________________________________

### Action plan mode (Status: draft)

#### Step B1: Scan for blocking open questions

Read spec's "Open questions" section. For each question, determine whether **blocking** (no recommended option stated, answer genuinely unknown) or **non-blocking** (spec states recommended option or answer inferable).

For each blocking question: call `AskUserQuestion` — one at a time, in order. Non-blocking questions go into plan table footnote.

#### Step B2: Generate the action plan

1. Parse spec into discrete action items from "Proposed design" and "Success criteria"
2. For each item, write ready-to-run invocation:
   - `.claude/` config change → `/manage create <type> <name> "description"` or `/manage update <name> <spec-file>`
   - System install or shell setup → full shell command
   - Application code change → `/develop:feature "<goal>"` or `/develop:fix "<symptom>"`
   - Documentation → `/develop:feature "<doc goal>"`
   - Verification/testing → `/develop:feature "<test goal>"` or manual check command
3. Output ordered task table:

```
## Action Plan: <spec title>

Spec: <file path>

| # | Task | Invocation |
|---|------|------------|
| 1 | [first action item] | `/develop:feature "<goal>"` |

### Non-blocking open questions (resolve during implementation)
- [list, or "None"]
```

#### Step B3: Post-plan prompt

Call `AskUserQuestion` with:

- (a) Start task 1 now ★ recommended
- (b) Copy plan and pass to another agent
- (c) Revise spec first

On **(a)**: proceed immediately with invocation from task 1. On **(b)**: output plan table as clean markdown block, then stop. On **(c)**: stop and tell user to revise spec and re-run `/brainstorm breakdown <spec>`.

End with `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- **No code at any point** — skill produces tree documents and specs only; implementation out of scope
- **`disable-model-invocation: true`** — skill is conversational; parent model drives all steps turn by turn
- **self-mentor scope in Step 5** — spawn prompt must constrain scope to tree quality explicitly; do not let it audit `.claude/` config files
- **.plans/blueprint/ directory** — created if absent; filenames use `YYYY-MM-DD-<kebab-slug>.md` format; tree files use base slug; spec files append `-spec` to slug to avoid collision
- **Status field**: tree documents use `Status: tree`; spec documents use `Status: draft`; breakdown auto-detects which path to take
- **Breakdown heading convention**: distillation mode uses D-prefix steps (D1–D4); action plan mode uses B-prefix steps (B1–B3)
- **Exploration notes in spec**: Section 6 derived from tree's Pruning log — intentional context for future readers; do not remove in self-mentor review
- **Interaction budget**: idea mode — worst case: 13 (`--tight`) / 23 (default) / 33 (`--deep`) questions + operations + 3 approval cycles; breakdown distillation — max 5 questions + 6 section drafts ≈ 11; typical sessions use ~8–15 total AskUserQuestion calls across both
- **Flag modes**: `--tight` / `--deep` scale question and operation caps (5/15 vs default 10); `--type` enables type-aware scan and question framing in Steps 1–2; flags apply to idea mode only, ignored in breakdown
- **Follow-up**: after spec approval in distillation mode → if targeting `.claude/` config: `/manage update <name> <spec-file>`; for application or mixed changes: `/brainstorm breakdown .plans/blueprint/<spec-file>` for action plan

</notes>
