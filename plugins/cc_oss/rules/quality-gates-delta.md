---
description: oss quality-gate deltas — review-loop scope and follow-up-gate examples; shared body in foundry-quality-gates.md
paths:
  - '**'
---

## Adversarial Convergence Loop (explicitly invoked)

A request to repeat review and fix until a reviewer reports no findings or no security findings, or to resolve all findings; a second independent review round about to be dispatched for the same change; or a skill workflow repeating its review then fix cycle counts as an explicitly invoked loop from that moment: before the next reviewer dispatch, start ledger; follow `_full/adversarial-loop.md` (helper output, stop/plateau rules, user's round budget).

For permitted independent review in `/oss:review` → `/oss:resolve`, pre-commit review, and contributor-PR fix rounds. For a permitted independent pass, dispatch `foundry:challenger` via `Agent()` (requires `foundry` plugin), or `bridge:review` when the bridge plugin is available; never use `subagent_type: "fork"`. Give the reviewer the diff, specification, and symptom, never the implementation narrative. A challenger round that returns no deliverable or a `blocked` envelope is an agent failure, never a silent `not-run`: record its cause from the transcript (safety-classifier stop, refused report write, background stall, malformed envelope, reply truncated before its required closing block) and re-dispatch once with a brief that removes it. For a security-filter review, leave Codex enabled (no `--no-codex`): the challenger runs the open-ended bypass search through its Codex cross-check, statically classifies Codex's candidates and any probe generator the caller supplies, counts the reproduced ones in its own six-tier row, and never authors bypass strings itself, so the round needs no separate Codex row.

Codex quota stop: a challenger reply whose envelope carries `"codex_blocked": "quota"` means its Codex cross-check stopped on a harness-classified `quota-exhausted` bridge fault; for the rest of that loop:

- Pass `--no-codex` to every later `foundry:challenger` dispatch, and never switch the loop to a `bridge:review` route: the peer is out of credits, so another Codex call cannot succeed.
- Dispatch no later security-filter review: the challenger ignores `--no-codex` for one, so it would spend a doomed Codex call. Report that review `blocked` with the quota cause and the options (wait for the quota reset, or the user decides), never PASS.
- A `blocked` envelope carrying the key is not re-dispatched, since no brief removes a quota stop.
- Record the quota stop under the reviewer route limits in `loop-report.md`.
- An envelope, when present, decides alone through its top-level `"codex_blocked": "quota"` key. The `⚠ codex-blocked:quota` line counts only when the dispatch demanded no envelope, and only as the first non-blank line under `## Codex Cross-Check`, read at the first such heading outside any code fence; other report text never counts.
- Field absent and no such line → the loop rules apply unchanged.

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md (canonical), plugins/cc_develop/rules/quality-gates.md, plugins/cc_develop/rules/quality-gates-delta.md, plugins/cc_oss/rules/quality-gates.md, plugins/cc_oss/rules/quality-gates-delta.md, plugins/cc_research/rules/quality-gates.md, plugins/cc_research/rules/quality-gates-delta.md — Codex quota stop, byte-identical -->

> **Shared body lives in `foundry-quality-gates.md`.** This file states only what is specific to the `oss` plugin: the review-loop scope above and the follow-up-gate routing below. Every other quality-gate obligation — Evidence Grounding, Evidence and real-case acceptance, Adversarial Pass, Confidence Block, Internal Quality Loop, Pre-Handover Check, Link Verification, Report File Format, Reporting Findings — is delivered once by foundry and is not restated here. `/oss:setup` delivers this delta only when a foundry-owned `foundry-quality-gates.md` is already installed, and delivers the plugin's complete `quality-gates.md` otherwise, so a standalone install loses no rule.

## Output Routing

> Long-output routing (Write, then print: header table, path, executive summary), the conditional follow-up gate and its precedence/follow-through, short-status, copy-intent and no-hollow-question rules are foundry §Output Routing in `foundry-quality-gates.md` — not restated here. Plugin additions:

- `<branch>` in the `.temp/output-*` filename = `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`
- ASCII verdict fallback line (no YAML block): `·` separator, no Unicode box-drawing chars (`─`, `═`, `│`, `┌` etc.)
- Prose paragraphs: no hard line breaks at column width
- **Manual follow-ups**: when the chosen skill has `disable-model-invocation: true`, print its exact command with the target for the user to run; never call `Skill()` for it. Missing optional plugin: name the prerequisite and print the command.
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples:
  - `/oss:review N` → (a) `/oss:resolve N` · (b) `/oss:resolve report` · (c) `/oss:resolve N report` · (d) walk findings · (e) skip
  - `/oss:analyse N` → (a) `/develop:fix` · (b) `/develop:feature` · (c) `/oss:review N` · (d) draft reply · (e) skip
