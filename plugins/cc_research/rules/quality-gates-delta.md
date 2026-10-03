---
description: research quality-gate deltas — review-loop scope and output routing; shared body in foundry-quality-gates.md
paths:
  - '**'
---

## Adversarial Convergence Loop (any review → fix cycle)

Governs `/research:judge` refinement loops, `/research:verify` fix rounds, and pre-commit review. For a permitted independent pass, dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` when the bridge plugin is available; never use `subagent_type: "fork"`. Give the reviewer the diff, specification, and symptom, never the implementation narrative.

> **Shared body lives in `foundry-quality-gates.md`.** This file states only what is specific to the `research` plugin: the review-loop scope above and the follow-up-gate routing below. Every other quality-gate obligation — Evidence Grounding, Adversarial Pass, Confidence Block, Internal Quality Loop, Pre-Handover Check, Link Verification, Report File Format, Reporting Findings — is delivered once by foundry and is not restated here; a `§` or `**Report File Format**` reference below resolves against that file. `/research:setup` delivers this delta only when a foundry-owned `foundry-quality-gates.md` is already installed, and delivers the plugin's complete `quality-gates.md` otherwise, so a standalone install loses no rule.

## Output Routing

- **Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps in order:

1. Call **Write tool** to create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` where `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` (new file — never overwrite; append counter suffix if slug exists, e.g. `-2.md`); file gets **full content**
2. Print to terminal in this order:
   - a. **YAML header table** — render `---` metadata block from top of report file as simple two-column Markdown table (`Field | Value`, one row per key, each value single physical line ≤100 chars — never wrap a value inside a cell: wrapped continuation line loses leading `|`, breaks GFM table parsing from that row down) — never print raw YAML verbatim (see **Report File Format** below); if skill has no YAML block in file, fall back to plain ASCII verdict line with `·` separator: `verdict: ⚠ NEEDS_WORK · findings: 8 · critical: 0 · high: 2 · medium: 4 · low: 2 · confidence: 0.88` (verdict word prefixed with its symbol — see §Reporting Findings)
   - b. **Report path** — `→ <filepath>`
   - c. **Executive summary** — prose: 2–3 sentence overview + each critical/high finding listed individually; omit medium/low detail unless ≤2 total findings
   - d. **Conditional follow-up gate** — invoke `AskUserQuestion` only when a required decision or authorization is missing; output length never triggers a question. Continue an already-authorized next action in the same turn. Background/pipeline work returns any missing decision to its owner.

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create file
- **Copy-intent override**: output destined for an external artifact (PR body, release notes, report to share) → write to file regardless of length; output read in-context and acted on immediately (audit findings, calibration result, code review) → terminal only even if long
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples by skill:
  - `research:topic` → (a) `/research:plan` · (b) `/develop:feature` · (c) skip
- **Follow-up precedence**: this conditional rule overrides generic skill completion prompts such as `NEVER SKIP` or `Always fires` when no required decision or authorization is missing. Preserve concrete gates for a new fix scope, paid execution, sensitive or destructive actions, remote changes, and other ungranted authority; existing authorization applies only within its granted scope. Do not invent a decision merely to fill a follow-up menu.
- **Follow-up gate follow-through**: `AskUserQuestion` returns with skill-invocation option selected → call `Skill(skill=..., args=...)` same response turn; never narrate intent as prose ("Invoke that next.", "Will now run /skill") and stop without acting
- **Don't ask what you can't honor**: selected option can't trigger automatic action (`disable-model-invocation: true`, or output is intermediate with a downstream AskUserQuestion coming anyway) → print the suggestion as plain text instead of asking a hollow question
