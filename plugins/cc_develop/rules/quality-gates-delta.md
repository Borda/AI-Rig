---
description: develop quality-gate deltas — review-loop scope and follow-up-gate examples; shared body in foundry-quality-gates.md
paths:
  - '**'
---

## Adversarial Convergence Loop (any review → fix cycle)

Governs `/develop:review` → `/develop:fix`, `/develop:debug` root-cause loops, pre-commit review. Permitted independent pass: dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` if available; never `subagent_type: "fork"`. Give reviewer diff, spec, symptom — never implementation narrative. Challenger round with no deliverable or a `blocked` envelope = agent failure, never silent `not-run`: record cause from transcript (safety-classifier stop, refused report write, background stall, malformed envelope, reply truncated before its required closing block), re-dispatch once with brief removing it. Security-filter review: leave Codex on (no `--no-codex`); challenger runs open-ended bypass search via its Codex cross-check, statically classifies Codex candidates + any caller probe generator, counts reproduced ones in its own six-tier row, never authors bypass strings; no separate Codex row.

> **Shared body lives in `foundry-quality-gates.md`.** This file states only what is specific to the `develop` plugin: the review-loop scope above and the follow-up-gate routing below. Every other quality-gate obligation — Evidence Grounding, Evidence and real-case acceptance, Adversarial Pass, Confidence Block, Internal Quality Loop, Pre-Handover Check, Link Verification, Report File Format, Reporting Findings — is delivered once by foundry and is not restated here. `/develop:setup` delivers this delta only when a foundry-owned `foundry-quality-gates.md` is already installed, and delivers the plugin's complete `quality-gates.md` otherwise, so a standalone install loses no rule.

## Output Routing

> Long-output routing (Write, then print: header table, path, executive summary), the conditional follow-up gate and its precedence/follow-through, short-status, copy-intent and no-hollow-question rules are foundry §Output Routing in `foundry-quality-gates.md` — not restated here. Plugin additions:

- `<branch>` in the `.temp/output-*` filename = `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. A next-step skill with `disable-model-invocation` cannot be a selectable option — the skill prints it as plain text instead. Canonical examples by skill:
  - `develop:review` → (a) walk through findings · (b) skip; `/develop:fix` and `/develop:refactor` printed as plain-text next steps
  - `develop:debug` → no gate; `/develop:fix --diagnosis <file>` printed as a plain-text next step
  - `develop:plan` → (a) Proceed — print the `/develop:feature --plan <file>` or `/develop:fix --plan <file>` handoff line · (b) Revise plan
