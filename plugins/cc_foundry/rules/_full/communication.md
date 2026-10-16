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
- Marker goes in `description` (always rendered), **not** `preview` (only shown when focused / side-by-side layout) — `description` is the only field guaranteed visible per option. Options carry a label + one-line effect describing what the choice does with the content. An option `description` is no place for multi-line content: Claude Code renders it as one line, its line breaks replaced.
- **Question text**: at most 6 plain lines (target 600 chars, hard cap 1,500); no table, pipe layout or fence. Short facts carry counts, headline, what each choice does, and `→ <file path>`. The host cuts question text after 2,000 characters; keep this summary even when content was printed before the call, because pre-call reply text can arrive as an empty progress update on some models.
- **No restating**: question text never repeats rows, items or findings that the last printed text (a table, list or report) already shows, nor items its own options offer — the options' labels are the list. It keeps the short summary for the pre-call-text-lost case: counts, headline, `→ <file path>`, never the rows themselves. Exception: content printed nowhere else, such as a conflicted-path list no table carries.
- **Required rich content**: content the user must read before choosing (item tables, plans, trees, queues, specs, diffs, lists) goes to a named file first and is printed once as last text before the call, rendered as normal Markdown. The question text names the file and carries the short plain summary; never table/tree in option `preview`. Report-header follow-ups use the exception below.
- **Option previews**: no preview by default; allow only short, distinct plain comparisons of at most 6 lines, no table, pipe layout or fence. Never give several options the same preview. Keep lines short to avoid wrapped rows.
- **Command approval**: A command the user must approve stays in the question text as an inline code span (for example the `gh-write` command).
- **Report-header follow-ups** — `oss:review`, `develop:review`, `oss:analyse`, `foundry:audit`, `foundry:profile`, `research:topic`: the first question's text carries a short plain summary with report path, `Title`, `Outcome`/`Verdict`, and finding-count facts; audit instead names the `summary.jsonl` path and the Audit Report headline. No table; question text stays within 1,500 characters. Print the full header table (audit: the full findings report) in the final reply after the answer; Stop still requires it there. Do not print this content before the question or put it in option previews. The workflow gate hooks enforce the question summary and final delivery (`report-header-table.js`).
- **Approval questions**: `git-approve`, `git-push`, `gh-write` are exempt from the 6-line target and carry their scope; their longer wording is owned by `git-commit.md` / `public-github.md`. They retain the hard cap of 1,500 characters and the no-table, no-pipe-layout, no-fence rule.
- **Host preview limits**: Claude Code (2.1.294) shows "(preview cannot be shown in full …)" instead of any preview over 2000 chars, clips a taller one to terminal rows − 26 lines with no scroll, and cuts option descriptions after 2000 chars. The legacy host ceiling is 12 lines, every line counted; a line over 86 chars counts once per 86 chars it spans (the box is terminal columns − 34 wide, 86 on the 120 × 38 terminal the 12-line height assumes). The stricter 6-line comparison contract above applies; an over-cap preview is never the content's only copy. HTML-preview hosts (`previewFormat: "html"`) render host-side with unverified limits, so the same contract applies.
- **Genuine-recommendation gate**: show markers only when the choice is a real, open decision **and** the model has a real leaning (a correct/better answer exists). Omit markers entirely when: (a) pure user-taste, no right answer (theme, naming preference); (b) the crossroad is fixed/given/forced — one viable path, outcome predetermined, or the option only confirms a decision already made. A marker on a non-decision is noise — never fake a recommendation or a spread. (If the choice is fully forced, prefer not asking at all — see AskUserQuestion "genuinely the user's to make".)
- **Widening gate**: before marking an option that widens a permission, sandbox, network or approval grant as recommended, read what the grant actually admits (profile definition, installer refusals, enforcement docs) and name its side effects in that option's `description`. A recommendation marker on an unread widening is an ungrounded premise.
- Applies globally — all skills, agents, model-generated questions
