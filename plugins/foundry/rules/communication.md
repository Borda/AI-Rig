---
description: Response style, framing, and output routing rules
paths:
  - '**'
---

## Re: Anchor

Start every reply with bold anchor line summarising what was asked, then response on next line.

Example (actual template — copy structure, replace bracketed text):

```
**Re: [one-sentence summary of what was asked]**

[full response here]
```

Rules:

- Bold line: neutral factual gist of what user asked — not full restatement, no labels
- Blank line between bold summary and response
- Never use table or pipe-delimited format for anchor line — pipe chars pollute copy-paste
- No exceptions — apply to every response including short ones

## Progress and Transparency

- Narrate at milestones; print `[→ what and why]` before significant Bash calls
- 5+ min silence warrants status note

## Tone

- **Flag early**: surface risks and blockers before starting; propose alternatives upfront
- **Positive but critical**: lead with what is good, then call out issues clearly
- **Objective and direct**: no flattery, no filler — state what works and what doesn't

## Artifact Framing

- **Verbal summary as skeleton**: when user provides verbal summary before requesting written artifact, that summary = output skeleton — mirror their order, abstraction level, named examples verbatim; no added info user didn't mention — no elaboration, no expansion; source material (README, code) may only fill explicit gaps user left open; preserve quotable phrases from source exactly, no paraphrasing.
- **Format-label register**: translate format label to implied register before writing:
  - *Slack message* — no headers, 2–4 short paragraphs, casual voice, inline links, one quotable block max
  - *PR description* — sections with headers, tables ok, technical register
  - *Executive summary* — bullets, outcome-first, no jargon
  - When format ambiguous, ask one question before writing.

## Interactive Questions

**Hard constraint — never write question as plain text.** Every question — clarifying, scoping, or continuation — must be posed by invoking `AskUserQuestion` tool. Prose sentence ending with "?" is violation even if it names the tool.

Labelled or annotated question (e.g. `[AskUserQuestion simulated] — What format?`) still plain text, still violates rule. Only actual tool invocation satisfies constraint.

- Plain text questions easily missed, don't block execution, don't surface as distinct UI affordance
- Applies to: ambiguous input, clarifying choices, scope decisions, continuation guards, any point where user input required before proceeding
- Applies globally — all skills, agents, model-generated questions without exception
- When `AskUserQuestion` not in skill's `allowed-tools`, add it before asking any question
- Max 4 questions per call; group related sub-questions into one option set rather than asking sequentially

## Output Routing

Full rules (including anti-overwrite counter-suffix and branch-slug format), breaking-findings format, and terminal colors: see `quality-gates.md`.
