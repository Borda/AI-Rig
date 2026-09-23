---
name: create
description: 'Interactive outline co-creation for developer advocacy content — collects format, audience profile, story arc (Problem→Journey→Insight→Action), and voice/tone; detects out-of-scope requests (FAQs, comparison tables); surfaces conflicts between user brief and audience needs. Writes approved outline to .plans/content/<slug>-outline.md for foundry:creator to execute. Use when starting a blog post, Marp slide deck, social thread, talk abstract, or lightning talk. SKIP: idea generation before an outline exists (use foundry:brainstorm — brainstorm explores directions into a tree; create turns an already-approved outline into finished content).'
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

**Task hygiene**: load and follow the protocol below.

```bash
# audit-skip: resilience-replication
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_shared_doc.py" foundry skills/_shared task-hygiene.md  # timeout: 5000
```

**Task tracking**: TaskCreate all steps before any tool calls.

## Step 1 — Parse topic and out-of-scope detection

- $ARGUMENTS provided: extract topic; note embedded format hint.
- No $ARGUMENTS: AskUserQuestion — "What are you trying to write about, and for whom?" (free text). After answer, re-check against out-of-scope conditions: answer describes FAQs, comparison tables, feature matrices, README content, or docstrings: stop. Respond: "This format doesn't fit a narrative arc — use `foundry:doc-scribe` for structured reference content." No further steps.
- Out-of-scope gate (when $ARGUMENTS provided): brief describes FAQs, comparison tables, feature matrices, or ref docs: stop. Respond: "This format doesn't fit a narrative arc — use `foundry:doc-scribe` for structured reference content." No further steps.

## Step 2 — Format and audience (1 AskUserQuestion call — 2 questions)

**Format + audience** (single AskUserQuestion call with 2 questions):

- Q1 "What content format?": (a) blog post · (b) conference / meetup talk with Marp slide deck ★ · (c) social thread (X/LinkedIn) · (d) talk abstract (CFP submission) · (e) lightning talk (5–10 min)
- Q2 "Who is the audience?": (a) beginners — new to problem space ★ · (b) intermediate — familiar with basics, seeking depth · (c) expert — know landscape, want novel insight · (d) describe your own profile

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

**Arc approval + voice** (single AskUserQuestion call): show proposed arc, then ask voice choice — option (d) redirects to arc adjustment.

Options:

- (a) Approve arc — neutral developer advocate (balanced, educational) ★
- (b) Approve arc — opinionated / direct first-person, no hedging
- (c) Approve arc — conversational / approachable, informal
- (d) Adjust the arc first (free text — describe what to change)

On (d): revise arc, re-present, re-invoke this question. After (a)/(b)/(c): restate confirmed arc and voice in two sentences.

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

- Derive artifact extension `<ext>` from format selected in Step 2 — substitute literal value into spawn prompt before invoking `Agent()`; never pass literal `<ext>` placeholder. Mapping:

  | Format (Step 2 choice) | `<ext>` |
  | -- | -- |
  | a) blog post | `md` |
  | b) conference / meetup talk with Marp slide deck | `md` (Marp markdown) |
  | c) social thread (X/LinkedIn) | `md` |
  | d) talk abstract (CFP submission) | `md` |
  | e) lightning talk | `md` |

  Every supported format currently renders to a markdown source file, so `<ext>` resolves to `md` in every branch — substitution must still happen explicitly so artifact path on disk is `.plans/content/<slug>.md`, not `.plans/content/<slug>.<ext>`. If a future format uses a different extension, extend the table.

> **Agent budget** — each spawn costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

- End with `AskUserQuestion` gate, two options: (a) **Generate the full artifact now** — spawn `foundry:creator` via `Agent(subagent_type='foundry:creator', prompt='Read <outline-path> and generate the complete <format> artifact. Output file path: .plans/content/<slug>.<ext>')` where `<outline-path>` is resolved path from anti-overwrite step above, and `<slug>`, `<format>`, `<ext>` substituted from generated outline (see extension table above) before the call — never pass literal angle-bracket placeholders to spawned agent. (b) **Stop here** — I'll invoke `foundry:creator` manually when ready.

  **Substitution verification before issuing Agent call (mandatory)**:

  1. Construct final prompt string with all placeholders replaced
  2. Scan constructed string for remaining `<` or `>` characters; either present means substitution incomplete — resolve missing value(s) before spawning
  3. Confirm outline file path in prompt matches `<outline-path>` exactly (resolved path including any counter suffix, not a guess)

  If user selects (a), issue Agent() call in same response turn AFTER verification above passes. Do not narrate intent — call the tool.

- End with `## Confidence` block per quality-gates.md protocol, score based on outline coverage of topic, arc, audience.

</workflow>

<notes>

- **Execution model**: `disable-model-invocation: true` — Claude itself follows this SKILL.md as workflow template directly in main context (no autonomous sub-agent dispatch during outline phase). Step 4 gate selects (a): exactly one sub-agent spawned, `foundry:creator` (executes outline, writes full artifact). No other sub-agent invocations by this skill.

- Question budget: **3 `AskUserQuestion` calls / 4 questions** when a topic is supplied (step 2 = 1 call carrying 2 questions; steps 3 and 4 = 1 question each). Topic absent adds step 1's opener → 4 calls / 5 questions. Each arc-conflict round in step 3 re-invokes that call → up to 6 calls / 7 questions worst case.

- Each AskUserQuestion uses lettered options with one ★ recommended default.

- After each answer, restate understanding in 1–2 sentences before proceeding.

- Never silently adjust arc to match audience — always surface conflicts explicitly (Step 3).

- Refuse FAQs / comparison tables / ref docs at Step 1 gate; name `foundry:doc-scribe` as redirect.

- Write outline exactly once after approval — no second draft unless user requests.

- `foundry:creator` reads output outline file, generates full artifact autonomously.

- Outline spec files written to `.plans/content/` — see `artifact-lifecycle.md` for TTL policy (30d).

</notes>
