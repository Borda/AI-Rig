<!-- oss:resolve final report template — read by Step 11 for output format reference -->

<!-- Placeholders: $PR_NUMBER, $PR_URL, $BRANCH, $REPO, $ACTION_ITEMS count, per-item status -->

## Resolve Report — PR #<number>

### Contribution

\<2–3 sentence motivation summary from Step 3b>

### Conflicts

\<conflict table from Step 7, or "No conflicts detected">

### Action Items

<!-- One row per SELECTED item. Columns: # | Type | Change | Status | Resolution | Commit -->

<!-- Status: ✓ implemented / ⊘ skipped / ✗ rejected by challenge -->

<!-- Resolution: implemented / self-resolved / skipped / challenge-rejected -->

<!-- Change: code / test / docs / config / ci / style / refactor -->

<!-- Commit: short SHA or "—" when COMMIT_MODE=stage -->

| # | Type | Change | Status | Resolution | Commit |
| -- | -- | -- | -- | -- | -- |
| 1 | [gh][req] | code | ✓ | implemented | `abc1234` |

### Challenge Log

<!-- One row per surviving/rejected item. Verdicts render as bracketed flags [VALID]/[REJECT] with a mandatory few-word reason — never a bare verdict word. Every cell self-contained — no cross-row lookups needed. Omit section when --no-challenge. -->

| # | Finding | Evidence | Suggestion | Resolution |
| -- | -- | -- | -- | -- |
| 1 | Off-by-one in pagination cursor at api.py:88 | [VALID] — cursor increments before bounds check, confirmed in code | [VALID] — fix matches existing guard pattern used elsewhere in file | as-suggested: moved bounds check before cursor increment (`abc1234`) |
| 9 | Use `cv2.INTER_AREA` for all resizes | [VALID] — current code uses fixed interpolation regardless of scale direction | [REJECT] — unconditional INTER_AREA degrades quality on upscale | self-resolved: use INTER_AREA only when both target dims < source, else INTER_LINEAR |

<!-- ✗ wrong — bare verdict, no reason: | 3 | ... | VALID | VALID | ... | -->

<!-- ✓ right — every verdict cell carries flag + reason, always the Phase 1 challenge agent's actual rationale — never a filler string standing in for a missing one -->

### Lint + QA

\<linting-expert summary: N fixes applied | or "no violations"> / \<foundry:qa-specialist summary: N blocking fixed, N warnings | or "clean">

### Push

✓ Pushed to <remote>/\<HEAD_REF> — N new commits

**Next**:

- Maintainer reviews, clicks Merge in GitHub UI — merge commit keeps per-item commits; squash collapses them

## Confidence

<!-- format per quality-gates.md: Score 0.N, Gaps bullets, Refinements N passes (omit if 0) -->

**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low \<0.85 ⚠]

**Gaps**:

- [specific limitation]

**Refinements**: N passes.

- Pass 1: [what gap was addressed]
