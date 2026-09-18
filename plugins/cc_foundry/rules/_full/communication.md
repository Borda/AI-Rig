---
description: Response style, framing, and output routing rules
paths:
  - '**'
---

## Reply Visibility — Exemption Detail

Box `╔═╗`/`╚═╝` zone creates header boundary. `▓▓▓` footer creates end boundary. Together frame response against surrounding tool call output and hook logs. Unicode box-drawing only — no ANSI escape codes.

**Exemption — machine-parsed responses**: omit box header and footer when response prompt contains `Return ONLY:` or `compact JSON envelope` — output parsed by parent orchestrator. Either keyword alone triggers exemption; both present also triggers.

**Exemption — quality-gates report headers**: when reply leads with quality-gates `---` metadata block (skill's Output Routing report header — YAML between `---` delimiters converted to a two-column Markdown table per `quality-gates.md` Universal terminal-print rule, e.g. `/oss:review`, `/oss:resolve`, `/foundry:audit`, `/foundry:calibrate`), omit `╔═╗` box header — that table IS reply header, box would shadow it. Print the table as first block of reply; keep `▓` footer. Never emit both box header and metadata table in same reply.

## Artifact Framing — Format-Label Register Table

*Format-label register*: translate format label to implied register before writing:

- *Slack message* — no headers, 2–4 short paragraphs, casual voice, inline links, one quotable block max
- *PR description* — sections with headers, tables ok, technical register
- *Executive summary* — bullets, outcome-first, no jargon

When format ambiguous, ask one question before writing.

## Interactive Questions — Non-Compliant Forms Catalogue

Labelled or annotated question (e.g. `[AskUserQuestion simulated] — What format?`) still plain text, still violates rule. Only actual tool invocation satisfies constraint.

Describing, simulating, or annotating a tool call in any form — parenthetical ("AskUserQuestion would be invoked here"), bracket notation (`[Invoking AskUserQuestion: ...]`), intent narration ("I would ask...") — is plain text, a violation. Call tool directly; emit no prose description of intent before or instead of the call.

Compliant example — only valid form:

> Call `AskUserQuestion(questions=["What format is the data in? (JSON, CSV, XML)"])` — no prose question in response body.

- Plain text questions easily missed, don't block execution, don't surface as distinct UI affordance
- Bracketed, annotated, or narrated tool calls are plain text and violate this constraint — violations: `[AskUserQuestion: ...]`, `"I would ask..."`, `"AskUserQuestion would be invoked here"`, `(AskUserQuestion simulated)`
- Only actual tool invocation (tool call block in response) satisfies this constraint

## Confidence Display — Full Specification

For every `AskUserQuestion` multiple-choice call with a genuine model leaning: embed plain-text markers **inside each option's own `description` field** — never as a separate legend before the tool call. A standalone legend needs a label scheme (A/B/C or 1/2/3) mapped back to option order; that mapping silently breaks whenever the legend's item count or order drifts from the actual options (observed failure — legend keyed 3 letters against a 5-option call, no shared referent). Marker-in-description skips the mapping step: reader sees the scores on the exact option they score.

Format — two axes prefixed to `description`, `·` separator; `←` marks the recommended (highest-`fit`) option:

```
description: "fit: 55% · conf: 65% ← recommended — <rest of trade-off explanation>"
```

Rules:

- **Plain text only** — `fit: N%` · `conf: N%`, exact integers, `·` separator. No emoji bar, no ANSI. (Emoji bars dropped: hand-drawn glyphs malformed — stray digits like `🟩⬜⬜⬜⬜⬜2⬜`, coarse 20% buckets, width misalign per terminal. Plain numbers precise + unbreakable.)
- **Two distinct axes — never conflate**:
  - `fit: N%` = how well this option **addresses the problem** — comparative across options, spans them (need not sum to 100). Highest `fit` = the pick.
  - `conf: N%` = model's **self-confidence that its `fit` read is reliable** — epistemic, per-option, absolute (not comparative). Independent of fit: a high-`fit` pick may carry low `conf` when evidence is thin.
- Mark highest-`fit` option with trailing `←` (aligns with second-slot recommended option per placement rule in stub). Near-tie on `fit` → higher `conf` breaks it.
- Marker goes in `description` (always rendered), **not** `preview` (only shown when focused / side-by-side layout) — `description` is the only field guaranteed visible
- **Genuine-recommendation gate**: show markers only when the choice is a real, open decision **and** the model has a real leaning (a correct/better answer exists). Omit markers entirely when: (a) pure user-taste, no right answer (theme, naming preference); (b) the crossroad is fixed/given/forced — one viable path, outcome predetermined, or the option only confirms a decision already made. A marker on a non-decision is noise — never fake a recommendation or a spread. (If the choice is fully forced, prefer not asking at all — see AskUserQuestion "genuinely the user's to make".)
- Applies globally — all skills, agents, model-generated questions
