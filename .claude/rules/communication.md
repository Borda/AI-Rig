---
description: Response style, framing, and output routing rules
---

## Re: Anchor

Start every reply with a Unicode box, left-aligned content, output as literal text
(no code fences, no blockquote prefix). Width = 100 chars (98 inner + 2 border).

Example (the actual template — copy this structure, replace bracketed text):

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ [one-sentence summary of what was asked]                                                         │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ [opening line of response]                                                                       │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Padding rule — left-align only, never right-align:

- Cell content = `│ ` + text + trailing spaces + `│`
- Keep text under 90 chars; trailing spaces fill the remainder to 98 inner chars
- Never exceed 98 inner chars — overflow pushes the right `│` out

Rules:

- Top cell: neutral factual gist of what the user asked — not a full restatement, no labels
- Bottom cell: opening sentence only; full response continues as normal text below the box
- No exceptions — apply to every response including short ones

## Progress and Transparency

- Narrate at milestones; print `[→ what and why]` before significant Bash calls
- 5+ min silence warrants a status note

## Tone

- **Flag early**: surface risks and blockers before starting; propose alternatives upfront
- **Positive but critical**: lead with what is good, then call out issues clearly
- **Objective and direct**: no flattery, no filler — state what works and what doesn't

## Output Routing

See `.claude/rules/quality-gates.md` for output routing rules, breaking-findings format, and terminal colors.
