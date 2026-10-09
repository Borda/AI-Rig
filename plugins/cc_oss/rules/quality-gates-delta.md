---
description: oss quality-gate deltas — review-loop scope and follow-up-gate examples; shared body in foundry-quality-gates.md
paths:
  - '**'
---

## Adversarial Convergence Loop (any review → fix cycle)

Governs `/oss:review` → `/oss:resolve`, pre-commit review, and contributor-PR fix rounds. For a permitted independent pass, dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` when the bridge plugin is available; never use `subagent_type: "fork"`. Give the reviewer the diff, specification, and symptom, never the implementation narrative.

> **Shared body lives in `foundry-quality-gates.md`.** This file states only what is specific to the `oss` plugin: the review-loop scope above and the follow-up-gate routing below. Every other quality-gate obligation — Evidence Grounding, Evidence and real-case acceptance, Adversarial Pass, Confidence Block, Internal Quality Loop, Pre-Handover Check, Link Verification, Report File Format, Reporting Findings — is delivered once by foundry and is not restated here. `/oss:setup` delivers this delta only when a foundry-owned `foundry-quality-gates.md` is already installed, and delivers the plugin's complete `quality-gates.md` otherwise, so a standalone install loses no rule.

## Output Routing

> Long-output routing (Write, then print: header table, path, executive summary), the conditional follow-up gate and its precedence/follow-through, short-status, copy-intent and no-hollow-question rules are foundry §Output Routing in `foundry-quality-gates.md` — not restated here. Plugin additions:

- `<branch>` in the `.temp/output-*` filename = `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`
- ASCII verdict fallback line (no YAML block): `·` separator, no Unicode box-drawing chars (`─`, `═`, `│`, `┌` etc.)
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples:
  - `oss:review N` → (a) `/oss:resolve N` · (b) `/oss:resolve report` · (c) `/oss:resolve N report` · (d) walk findings · (e) skip
  - `oss:analyse N` → (a) `/develop:fix` · (b) `/develop:feature` · (c) `/oss:review N` · (d) draft reply · (e) skip
