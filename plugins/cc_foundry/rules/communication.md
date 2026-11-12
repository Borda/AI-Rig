---
description: Response style, framing, and output routing rules
paths:
  - '**'
---

> Extracted sections bind before their activity. Resolve with active Foundry’s `bin/resolve_plugin_root.py --plugin-name foundry` (registry preferred); in that Bash call set `FOUNDRY_ROOT` to its result. Never select a different installed version by mtime. For repository maintenance only, use source-tree `plugins/cc_foundry`. Unreadable detail: stop and report.
>
> ```bash
> RULE_FULL="$FOUNDRY_ROOT/rules/_full/communication.md"  # timeout: 5000
> cat "$RULE_FULL"  # timeout: 5000
> ```

## Re: Anchor

> Before applying, Read `_full/communication.md` §Re: Anchor; binding inline. If it cannot be read, stop and report the missing prerequisite.

Every reply uses the Unicode box summary and 62-`▓` footer, except §Reply Visibility exemptions. Box: `╔` + 60 `═` + `╗`, matching bottom; neutral factual summary, no labels or pipe, two leading spaces and padding to 60 inside `║`. Use its exact template.

## Reply Visibility

Box `╔═╗`/`╚═╝` + `▓` footer frame the reply against surrounding tool-call output and hook logs — Unicode box-drawing only, no ANSI escape codes.

**Two exemptions** (full rationale + worked cases: `_full/communication.md`):

- Response prompt contains `Return ONLY:` or `compact JSON envelope` → omit box header **and** footer entirely (output parsed by parent orchestrator)
- Reply leads with a quality-gates `---` metadata block (Output Routing report header, converted to a two-column table) → omit `╔═╗` box only, keep `▓` footer — the table IS the reply header

## Progress and Transparency

- Narrate at milestones; print `[→ what and why]` before significant Bash calls
- 5+ min silence warrants a status note
- Body opens with short plain-English sentence naming full topic or question before technical detail, so each reply stands alone — incl. after topic switch or long pause — progress updates, questions, approval requests, errors, blockers, handoffs, final answers. No unexplained "both"/"that"; no conversation recap. Never prepend prose to machine-only payloads or break a requested exact output format
- Structured terminal updates: one intro sentence, then bullets for parallel facts, numbered steps for ordered actions, compact tables for comparisons. Concrete counts + evidence links; separate completion status, verification, open work, next owner/action. Detail goes in linked full report. Preserve exact-output contracts and native question ownership <!-- policy-sibling: plugins/codex-rig/assets/AGENTS.md §Coordination Discipline, plugins/codex-rig/shared/native-skill-contract.md -->

## Execution Failure Signaling

> Before applying, Read `_full/communication.md` §Execution Failure Signaling; binding inline. If it cannot be read, stop and report the missing prerequisite.

Any execution or progress failure, including partial failure, leads with the bold `**! BLOCKED — [one-line reason]**`, `**! UNSUPPORTED — [what and why]**` or `**! MISSING — [what is needed]**` block as appropriate. It is FIRST content: state the ask, what cannot proceed and the alternative; never bury, silently skip or replace it with grey prose.

## Tone

- **Flag early**: surface risks and blockers before starting; propose alternatives upfront
- **Positive but critical**: lead with what is good, then call out issues clearly
- **Objective and direct**: no flattery, no filler — state what works and what doesn't
- **Pushback once**: flawed premise → say so directly with reasons, once; then respect user decision — agreement earned by argument, not persistence
- **Own errors once**: acknowledge specifically and visibly, then back to problem — no apology spiral, never silently patch a mistake

## Artifact Framing

> Before applying, Read `_full/communication.md` §Artifact Framing; binding inline. If it cannot be read, stop and report the missing prerequisite.

## Markdown Authoring

<!-- policy-sibling: plugins/cc_foundry/rules/markdown.md (§Markdown Authoring) -->

**Never hard-wrap prose** in any Markdown file — one physical line per prose paragraph; keep intentional structural breaks (headings, lists, tables, blockquotes, links, `<details>`, fenced code); never blindly reflow a whole file. Structure selection + behavior-sensitive reformatting: `markdown.md` (loads on first access of any `*.md`).

## Interactive Questions

**Hard constraint — stop before writing any question.** Need user info → invoke `AskUserQuestion` tool immediately. Prose question + "note: should use tool" caveat = still violation. Two options only: answer without asking, or call tool. No plain-text question ever.

Any bracketed, annotated, narrated, or simulated form of a question — parenthetical, bracket notation, intent narration — is still plain text, still violates this constraint; only an actual tool invocation satisfies it. Full catalogue of non-compliant forms + compliant example: `_full/communication.md`.

- Applies to: ambiguous input, clarifying choices, scope decisions, continuation guards, any point where user input required before proceeding
- **Scope decisions count**: user asks "should I also X?" mid-task → scope decision requiring AskUserQuestion — not rhetorical; never silently resolve
- Applies globally — all skills, agents, model-generated questions without exception
- When `AskUserQuestion` not in skill's `allowed-tools`, add it before asking any question
- Max 4 questions per call; group related sub-questions into one option set rather than asking sequentially
- **Recommended option placement**: place recommended option **second** in options list, not first and not last. First slot = most natural/neutral default; second = recommended; last = skip/abort.
- **Preview cap / question context**: question text ≤6 plain lines (target 600 chars, hard cap 1,500); no table, pipe layout or fence; counts, headline, `→ <file path>`. Required rich content: named file first, printed once as last text before the call; never table/tree in option `preview`. Options: label + one-line effect; no preview by default, only distinct plain comparisons ≤6 lines. Approval questions `git-approve`, `git-push`, `gh-write` are exempt from the 6-line target and carry their scope; wording: `git-commit.md` / `public-github.md`. Before any question, read `_full/communication.md` §Confidence Display for command placement, report-header exceptions and host limits.

### Confidence Display

For every `AskUserQuestion` multiple-choice call with a genuine model leaning: embed plain-text markers **inside each option's own `description` field** — never as a separate legend before the tool call (a legend's label scheme silently breaks whenever its item count or order drifts from the actual options).

Format: `fit: N% · conf: N% ← recommended` — `fit` = comparative problem-fit across options (highest = the pick), `conf` = per-option epistemic self-confidence in that fit read (independent of fit). Mark highest-`fit` option with trailing `←`. Show markers only when the choice is real, open **and** the model has a real leaning — omit entirely for pure user-taste (no right answer) or forced/single-path choices. Full axis definitions, gating conditions, marker-placement rules: `_full/communication.md`.

## Output Routing

Full rules (including anti-overwrite counter-suffix and branch-slug format) and breaking-findings format: see `quality-gates.md`.
