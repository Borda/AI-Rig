---
description: Output quality standards — Confidence block, link verification, output routing
paths:
  - '**'
---

## Confidence Block (required on all analysis tasks)

Every analysis agent **must** end with:

```markdown
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
**Gaps**:
- [specific limitation]
                          ← blank line required; Refinements is a peer field, not a sub-bullet
**Refinements**: N passes.
- Pass 1: [what gap was addressed — must name the gap, not just say "re-checked"]
```

> **Never skip** — missing Confidence block = rule violation.

- Omit **Refinements** if 0 passes (don't write "0 passes") — omit individual **Gaps** bullets if none, but keep **Gaps** header
- **Score**, **Gaps**, **Refinements** = peer top-level fields — never nest Refinements under Gaps; blank line before **Refinements** required
- Score < 0.8 → ⚠ on score line AND next line: "orchestrator may re-run with the specific gap addressed"
- Gaps = primary signal — surfaces implicit limitations for re-run decisions

## Internal Quality Loop (analysis tasks only)

Before returning, self-review:

1. Draft → self-evaluate (missed issues, unsupported claims, coverage gaps) → score
2. Score < 0.9: name highest-impact gap concretely, address what you can — even info-access limits: document + add inferences/caveats; re-score; cap 2 passes
3. Score rises only when **named, specific gap** addressed — generic phrases ("re-checked, looks fine", "reviewed for completeness") don't count; pass must name gap (e.g. "Added versioning section missing from initial draft")
4. After 2 passes, report real score — never inflate

## Pre-Handover Check

Confidence < 0.9 and `codex` plugin available → spawn `Agent(subagent_type="codex:codex-rescue")` naming low-confidence area for adversarial review — incorporate before handover. Codex unavailable → state gap and score explicitly so user can decide to re-run.

## Link Verification

**Never add URL without all three steps:**

1. **Fetch** — call WebFetch (or equivalent); URL must return non-error (not 4xx/5xx)

2. **Read** — read actual page content; don't rely on URL structure or HTTP status alone

3. **Match** — confirm content matches intended description; no match = don't add link

4. **Independent** — every URL needs own Fetch+Read+Match pass; verified URL on same domain doesn't exempt others; skipping any step (including inferring validity from URL structure or HTTP status alone) is violation

- Applies to: agent files, skill files, CLAUDE.md, any markdown

## Output Routing

- **Long output** (multi-item analysis, 5+ findings — including lists of 5+ items: module names, issues, files —, or prose >~10 lines) → two mandatory steps in order:

1. Call **Write tool** to create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` where `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` (new file — never overwrite; append counter suffix if slug exists, e.g. `-2.md`); file gets **full content**
2. Print to terminal in this order:
   1. **Header** — plain ASCII verdict line; no Unicode box-drawing chars (`─`, `═`, `│`, `┌` etc.); use `·` as separator: `verdict: NEEDS_WORK · findings: 8 · critical: 0 · high: 2 · medium: 4 · low: 2 · confidence: 0.88`
   2. **Report path** — `→ <filepath>`
   3. **Executive summary** — prose: 2–3 sentence overview + each critical/high finding listed individually; omit medium/low detail unless ≤2 total findings
   4. **Follow-up gate** — invoke `AskUserQuestion` as final step; skip when running as background agent or inside another skill's pipeline

- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create file
- Prose paragraphs: no hard line breaks at column width
- **Follow-up gate options**: skill-defined; minimum: (a) primary action · (b) skip. Canonical examples by skill:
  - `develop:review` → (a) `/develop:fix` · (b) `/develop:refactor` · (c) walk through findings · (d) skip
  - `develop:debug` → (a) `/develop:fix --diagnosis <file>` · (b) skip
  - `develop:plan` → (a) `/develop:feature --plan <file>` · (b) `/develop:fix --plan <file>` · (c) skip
- **Follow-up gate follow-through**: when `AskUserQuestion` returns with a skill-invocation option selected — call `Skill(skill=..., args=...)` in the same response turn; never narrate the intent as prose ("Invoke that next.", "Will now run /skill") and stop without acting

## Reporting Findings

- **Report before fixing**: state every finding before any fix — never silently mutate
- **Per-fix narration**: before each file edit or tool call, state what changes and why
- **! BREAKING format**: breaking findings = standalone block — never inline or buried in table row:

```text
! BREAKING — <one-line impact: what breaks and who is affected>
Fix: <concrete action to resolve>
```

- Terminal colors: RED = critical · YELLOW = warnings · GREEN = pass · CYAN = fix hint
