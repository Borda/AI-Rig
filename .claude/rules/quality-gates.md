---
description: Output quality standards — Confidence block, link verification, output routing
---

## Confidence Block (required on all analysis tasks)

Every agent completing an analysis task **must** end with:

```
## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
**Gaps**:
- [specific limitation]
                          ← blank line required; Refinements is a peer field, not a sub-bullet
**Refinements**: N passes.
- Pass 1: [what gap was addressed — must name the gap, not just say "re-checked"]
```

> **Never skip this block** — a missing Confidence block is a rule violation regardless of how short or simple the analysis is.

- Omit **Refinements** entirely if 0 passes (do not write "0 passes") — omit individual **Gaps** bullets if none apply, but always include the **Gaps** header
- **Score**, **Gaps**, and **Refinements** are peer top-level fields — never nest Refinements as a sub-bullet under Gaps; the blank line before **Refinements** is required
- Score < 0.8 → the ⚠ is part of the score line AND the next line must read: "orchestrator may re-run with the specific gap addressed"
- Gaps field is the primary signal — surfaces implicit limitations for re-run decisions

## Internal Quality Loop (analysis tasks only)

Before returning, self-review:

1. Draft → self-evaluate (missed issues, unsupported claims, coverage gaps) → score
2. If score < 0.9: name the highest-impact gap concretely and address what you can — even if the gap is an information-access limitation, document it and add any inferences or caveats that reduce uncertainty; re-score; cap at 2 passes
3. Score rises only when a **named, specific gap** was addressed — generic phrases like "re-checked, looks fine", "reviewed for completeness", or "checked all findings" do not count; the pass description must name the gap (e.g. "Added versioning section missing from initial draft")
4. After 2 passes, report the real score — never inflate; `/calibrate` catches bias

## Pre-Handover Check

If confidence score < 0.9 and the `codex` plugin is available, spawn `Agent(subagent_type="codex:codex-rescue")` naming the specific low-confidence area for adversarial review — incorporate findings before handover. If Codex unavailable, explicitly state the gap and score so the user can decide to re-run.

## Link Verification

**Never add a URL to any file without completing all three steps for that URL:**

1. **Fetch** — call WebFetch (or equivalent); the URL must return a non-error response (not 4xx/5xx)
2. **Read** — read the actual page content returned; do not rely on the URL structure or HTTP status alone
3. **Match** — confirm the content matches the intended description; if it does not match, do not add the link

- Every URL is verified independently — a verified URL on the same domain does not cover other URLs
- Applies to: agent files, skill files, CLAUDE.md, any markdown

## Output Routing

- **Long output** (multi-item analysis, 5+ findings, or any prose exceeding ~10 lines) → two mandatory steps in order:
  1. Call the **Write tool** to create `.temp/output-<slug>-<branch>-<YYYY-MM-DD>.md` where `<branch>` is `$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` (new file — never overwrite; append counter suffix if slug exists, e.g. `-2.md`); the file receives the **full content**
  2. Print to terminal only: compact summary — verdict · 2–3 sentences · critical points · confidence score · `→ <filepath>`; **do not repeat the full content in the terminal**
- **Short inline status** (single result, pass/fail, one-sentence finding) → terminal only; do **not** create a file
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
