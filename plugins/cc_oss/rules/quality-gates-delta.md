---
description: oss quality-gate deltas — review-loop scope and output routing; shared body in foundry-quality-gates.md
paths:
  - '**'
---

## Adversarial Convergence Loop (any review → fix cycle)

Governs `/oss:review` → `/oss:resolve`, pre-commit review, and contributor-PR fix rounds. For a permitted independent pass, dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` when the bridge plugin is available; never use `subagent_type: "fork"`. Give the reviewer the diff, specification, and symptom, never the implementation narrative.

> **Shared body lives in `foundry-quality-gates.md`.** This file states only what is specific to the `oss` plugin: the review-loop scope above and the follow-up-gate routing below. Every other quality-gate obligation — Evidence Grounding, Adversarial Pass, Confidence Block, Internal Quality Loop, Pre-Handover Check, Link Verification, Report File Format, Reporting Findings — is delivered once by foundry and is not restated here; a `§` or `**Report File Format**` reference below resolves against that file. `/oss:setup` delivers this delta only when a foundry-owned `foundry-quality-gates.md` is already installed, and delivers the plugin's complete `quality-gates.md` otherwise, so a standalone install loses no rule.

## Output Routing

- **Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps in order:

1. Call **Write tool** to create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` where `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` (new file — never overwrite; append counter suffix if slug exists, e.g. `-2.md`); file gets **full content**
2. Print to terminal in order:
   1. **YAML header table** — render `---` metadata block from top of report file as simple two-column Markdown table (`Field | Value`, one row per key, each value single physical line ≤100 chars — never wrap a value inside a cell: wrapped continuation line loses leading `|`, breaks GFM table parsing from that row down) — never print raw YAML verbatim (see **Report File Format** below); if skill has no YAML block in file, fall back to plain ASCII verdict line; no Unicode box-drawing chars (`─`, `═`, `│`, `┌` etc.); use `·` as separator: `verdict: ⚠ NEEDS_WORK · findings: 8 · critical: 0 · high: 2 · medium: 4 · low: 2 · confidence: 0.88` (verdict word prefixed with its symbol — see §Reporting Findings)
   2. **Report path** — `→ <filepath>`
   3. **Executive summary** — prose: 2–3 sentence overview + each critical/high finding listed; omit medium/low detail unless ≤2 total findings
   4. **Follow-up gate** — invoke `AskUserQuestion` as final step; skip when background agent or inside another skill's pipeline

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create file
- **Copy-intent override**: output destined for an external artifact (PR body, release notes, report to share) → write to file regardless of length; output read in-context and acted on immediately (audit findings, calibration result, code review) → terminal only even if long
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples:
  - `oss:review N` → (a) `/oss:resolve N` · (b) `/oss:resolve report` · (c) `/oss:resolve N report` · (d) walk findings · (e) skip
  - `oss:analyse N` → (a) `/develop:fix` · (b) `/develop:feature` · (c) `/oss:review N` · (d) draft reply · (e) skip
- **Follow-up gate follow-through**: when `AskUserQuestion` returns with skill-invocation option — call `Skill(skill=..., args=...)` same turn; never narrate intent as prose and stop without acting
- **Don't ask what you can't honor**: selected option can't trigger automatic action (`disable-model-invocation: true`, or output is intermediate with a downstream AskUserQuestion coming anyway) → print the suggestion as plain text instead of asking a hollow question
