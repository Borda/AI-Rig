---
name: create
description: 'Interactive outline co-creation for developer advocacy content — collects format, audience profile, story arc (Problem→Journey→Insight→Action), and voice/tone; detects out-of-scope requests (FAQs, comparison tables); surfaces conflicts between user brief and audience needs. Writes approved outline to .plans/content/<slug>-outline.md for foundry:creator to execute. Use when starting a blog post, Marp slide deck, social thread, talk abstract, or lightning talk. SKIP: idea generation before an outline exists (use foundry:brainstorm — brainstorm explores directions into a tree; create co-creates the outline, foundry:creator writes the finished artifact).'
argument-hint: '[topic]'
disable-model-invocation: true
allowed-tools: Write, Bash, TaskCreate, TaskUpdate, TaskList, AskUserQuestion, Agent
effort: medium
---

<objective>

Story arc four-beat: Problem → Journey → Insight → Action.

NOT for: implementation, code gen, README writing (use `foundry:doc-scribe`), structured ref docs (FAQs, comparison tables — use `foundry:doc-scribe`).

</objective>

<inputs>

- **$ARGUMENTS**: optional — topic or goal, any form; one sentence enough. Format hints accepted ("a blog post about…", "talk abstract for…").

</inputs>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: TaskCreate all steps in the same response as the first real tool call — never a response of bookkeeping alone.

## Step 1 — Parse topic and out-of-scope detection

- $ARGUMENTS provided: extract topic; note embedded format hint.
- No $ARGUMENTS: AskUserQuestion — "What are you trying to write about, and for whom?" (free text). After answer, re-check against out-of-scope conditions: answer describes FAQs, comparison tables, feature matrices, README content, or docstrings: stop. Respond: "This format doesn't fit a narrative arc — use `foundry:doc-scribe` for structured reference content." No further steps.
- Out-of-scope gate (when $ARGUMENTS provided): brief describes FAQs, comparison tables, feature matrices, or ref docs: stop. Respond: "This format doesn't fit a narrative arc — use `foundry:doc-scribe` for structured reference content." No further steps.

## Step 2 — Format and audience (1 AskUserQuestion call — 2 questions; +1 follow-up call when Q1 = (d))

**Format + audience** (single AskUserQuestion call with 2 questions; AskUserQuestion allows at most 4 options per question, so the two short talk formats share slot (d)):

- Q1 "What content format?": (a) blog post · (b) conference / meetup talk with Marp slide deck ★ · (c) social thread (X/LinkedIn) · (d) short talk — talk abstract (CFP submission) or lightning talk (5–10 min)
- Q2 "Who is the audience?": (a) beginners — new to problem space ★ · (b) intermediate — familiar with basics, seeking depth · (c) expert — know landscape, want novel insight · (d) describe your own profile

Q1 = (d) → one follow-up AskUserQuestion call, 1 question "Which short talk format?": (a) talk abstract (CFP submission) · (b) lightning talk (5–10 min). No ★ — pure user choice. All five formats stay reachable.

After answer: restate one sentence covering format + audience ("Got it — a [format] for [audience description].").

## Step 3 — Arc construction and conflict check

Propose four-beat arc from topic + audience:

- **Problem**: concrete opening hook — specific pain or question, not generic
- **Journey**: 3–5 key points (what tried, what failed, what arc covers)
- **Insight**: core "aha" framed for stated audience level — name directly
- **Action**: specific next step for audience

**Out-of-scope re-check (re-apply the Step 1 test to the constructed arc)**: the Step 1 gate tests the brief; this one tests what the arc turned out to be. If the Journey beat resolves mainly to a feature-by-feature comparison, an options matrix, a question-and-answer list, or reference material with no narrative through-line — i.e. the beats only hold together as a table or list — stop here, before writing any outline. Respond: "The arc for this topic resolves to structured reference content rather than a narrative — use `foundry:doc-scribe` instead." Do not write the outline file and do not offer the Step 4 generation gate. A topic that merely *mentions* alternatives still passes: the test is whether the Insight beat names a single transferable idea, or just summarises the comparison.

**Editorial conflict check**: brief implies expert audience but topic introductory, or vice versa: surface before continuing:

> "Your brief suggests [X] but audience profile is [Y] — recommend adjusting [Z]. Proceed as-is or adjust?"

**Arc approval + voice** (single AskUserQuestion call): derive a topic slug (kebab-case, max 5 words, per Step 4), Write the proposed arc (Hook · Journey · Insight · Action) to `.temp/create/<slug>-arc.md` first, keeping each beat to one or two lines. The full arc is printed once, rendered as normal Markdown, as the last reply text before the question call. The question text carries the arc headline, four-beat count and `→ .temp/create/<slug>-arc.md`, then asks the voice choice — option (d) redirects to arc adjustment. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options carry labels and one-line action descriptions, no `preview`; place the recommended option second without changing labels or answer mapping.

Options:

- (a) Approve arc — neutral developer advocate (balanced, educational) ★
- (b) Approve arc — opinionated / direct first-person, no hedging
- (c) Approve arc — conversational / approachable, informal
- (d) Adjust the arc first (free text — describe what to change)

On (d): revise arc, rewrite `.temp/create/<slug>-arc.md`, print the revised arc once immediately before re-invoking this question and refresh its plain summary. After (a)/(b)/(c): restate confirmed arc and voice in two sentences.

## Step 4 — Write outline file

- Derive slug from topic: kebab-case, max 5 words (e.g. `tracing-python-services-otel`).
- Write creates `.plans/content/` if absent — no separate mkdir needed.
- **Anti-overwrite check before writing outline**: list existing files matching `.plans/content/<slug>-outline*.md` (Bash `ls -1 .plans/content/<slug>-outline*.md 2>/dev/null || true`). If `.plans/content/<slug>-outline.md` already exists, append smallest available counter suffix (`-2`, `-3`, …) per quality-gates.md output routing convention. Resulting path becomes new `<outline-path>`; use in Write call AND in Step 4 gate spawn prompt below. Print resolved path before writing.
- Write `<outline-path>` with this structure:

```md
---
topic: <topic from brief>
created: YYYY-MM-DD
---

## Audience
[who they are, experience level, what they've likely seen, what they need]

## Format
[blog post | conference talk (N min) | social thread (x|linkedin) | talk abstract | lightning talk (N min)]

## Voice
[tone brief: e.g., "direct and opinionated, first-person, no hedging"]

## Arc

### Problem
[concrete opening hook — the pain or question]

### Journey
[key points to explore: what was tried, what failed, what the arc covers]

### Insight
[the core "aha" — what was learned or built; name it directly]

### Action
[call to action — specific, what audience should do next]

## Constraints
[length target, things to avoid, format-specific constraints]
```

- Confirm file path to user.

- Artifact extension `<ext>` = `md` for every supported Step 2 format (Marp deck included) — substitute it literally into the spawn prompt before invoking `Agent()`, so the path on disk is `.plans/content/<slug>.md`, never `.plans/content/<slug>.<ext>`. A future format with a different extension must add its own mapping here.

> Creator spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

- End with `AskUserQuestion` gate, two options: (a) **Generate the full artifact now** — spawn `foundry:creator` via `Agent(subagent_type='foundry:creator', name='creator-<slug>', description='write <format> artifact', prompt='Read <outline-path> and generate the complete <format> artifact. Output file path: .plans/content/<slug>.<ext>. Return ONLY a JSON envelope: status, file (path actually written, counter suffix included), partial (true on exhaustion, plus what was finished), confidence (your Confidence score).')` where `<outline-path>` is resolved path from anti-overwrite step above, and `<slug>`, `<format>`, `<ext>` substituted from generated outline (`<ext>` per the extension rule above) before the call — never pass literal angle-bracket placeholders to spawned agent. (b) **Stop here** — I'll invoke `foundry:creator` manually when ready.

  **Substitution verification before issuing Agent call (mandatory)**:

  1. Construct final prompt, `name` and `description` strings with all placeholders replaced
  2. Scan all three constructed strings for remaining `<` or `>` characters; either present means substitution incomplete — resolve missing value(s) before spawning
  3. Confirm outline file path in prompt matches `<outline-path>` exactly (resolved path including any counter suffix, not a guess)

  If user selects (a), issue Agent() call in same response turn AFTER verification above passes. Do not narrate intent — call the tool. Same response: Write `.temp/create/agent-watch-creator.tsv` = `creator-<slug>\t-\t900` (`_shared/agent-spawn-protocol.md` §Deadlines) — envelope-only row: creator's anti-overwrite may write `<slug>-2.md`, so a fixed deliverable path could match a stale prior artifact and mark `done` at once. End the turn — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. On its notification run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir ".temp/create"` once; notification carried the envelope → done, report its `file` path; no envelope → report the artifact as ⏱ `timed_out`.

- End with `## Confidence` block per quality-gates.md protocol, score based on outline coverage of topic, arc, audience.

</workflow>

<notes>

- **Execution model**: `disable-model-invocation: true` — Claude itself follows this SKILL.md as workflow template directly in main context (no autonomous sub-agent dispatch during outline phase). Step 4 gate selects (a): exactly one sub-agent spawned, `foundry:creator` (executes outline, writes full artifact). No other sub-agent invocations by this skill.

- Question budget: **3 `AskUserQuestion` calls / 4 questions** when a topic is supplied (step 2 = 1 call carrying 2 questions; steps 3 and 4 = 1 question each). Step 2 short-talk follow-up (Q1 = (d)) adds 1 call / 1 question. Topic absent adds step 1's opener → 4 calls / 5 questions. Each arc-conflict round in step 3 re-invokes that call → up to 7 calls / 8 questions worst case.

- Each AskUserQuestion uses lettered options with one ★ recommended default, except the step 2 short-talk follow-up (pure user choice, no ★).

- After each answer, restate understanding in 1–2 sentences before proceeding.

- Never silently adjust arc to match audience — always surface conflicts explicitly (Step 3).

- Refuse FAQs / comparison tables / ref docs at Step 1 gate; name `foundry:doc-scribe` as redirect.

- Write outline exactly once after approval — no second draft unless user requests.

- `foundry:creator` reads output outline file, generates full artifact autonomously.

- Outline spec files written to `.plans/content/` — `.plans/` is manual lifecycle, never auto-deleted (`artifact-lifecycle.md`).

</notes>
