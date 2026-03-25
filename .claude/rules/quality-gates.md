---
description: Output quality standards — Confidence block, link verification, output routing
---

## Confidence Block (required on all analysis tasks)

Every agent completing an analysis task **must** end with:

```
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8]
**Gaps**:
- [specific limitation]

**Refinements**: N passes.
- Pass 1: [what gap was addressed]
```

- Omit **Refinements** entirely if 0 passes (do not write "0 passes") — omit individual **Gaps** bullets if none apply, but always include the **Gaps** header
- **Score**, **Gaps**, and **Refinements** are peer top-level fields — never nest Refinements as a sub-bullet under Gaps; the blank line before **Refinements** is required
- Score < 0.8 → append ⚠ to the score line AND add a note "orchestrator may re-run with the specific gap addressed"
- Gaps field is the primary signal — surfaces implicit limitations for re-run decisions

## Internal Quality Loop (analysis tasks only)

Before returning, self-review:

1. Draft → self-evaluate (missed issues, unsupported claims, coverage gaps) → score
2. If score < 0.9: address highest-impact gap, name it concretely, re-score; cap at 2 passes
3. Score rises only when a named gap was addressed — "re-checked, looks fine" doesn't count
4. After 2 passes, report the real score — never inflate; `/calibrate` catches bias

## Link Verification

**Never add a URL to any file without completing all three steps for that URL:**

1. **Fetch** — call WebFetch (or equivalent); the URL must return a non-error response (not 4xx/5xx)
2. **Read** — read the actual page content returned; do not rely on the URL structure or HTTP status alone
3. **Match** — confirm the content matches the intended description; if it does not match, do not add the link

- Every URL is verified independently — a verified URL on the same domain does not cover other URLs
- Applies to: agent files, skill files, CLAUDE.md, any markdown

## Output Routing

- **Long output** → write to `tasks/output-<slug>-<YYYY-MM-DD>.md` (new file, never overwrite); print compact terminal summary (verdict · 2–3 sentences · critical points · confidence · `→ file`)
- **Short inline status** → terminal only
- Prose paragraphs: no hard line breaks at column width

## Reporting Findings

- **Report before fixing**: state every finding before applying any fix — never silently mutate
- **Per-fix narration**: before each file edit or tool call that applies a fix, state what is being changed and why
- **! BREAKING format**: breaking findings must appear as a standalone block — never inline mid-sentence or buried in a table row:

```
! BREAKING — <one-line impact: what breaks and who is affected>
Fix: <concrete action to resolve>
```

- Terminal colors: RED = critical · YELLOW = warnings · GREEN = pass · CYAN = fix hint
