<!-- oss:resolve final report template — read by Step 11 for output format reference -->

> Replace square-bracket placeholders with observed values before delivery. Preserve status flags and evidence labels; those are report notation, not placeholders. Omit template instructions from the finished report.

## Resolve Report — PR #[PR number]

### Contribution

[Contribution summary]

> Use the two- to three-sentence motivation summary from Step 3b.

### Conflicts

[Conflict table]

> Use the conflict table from Step 7, or “No conflicts detected.”

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

[Lint summary] / [QA summary]

> Report the linting-expert fix count or “no violations,” and the foundry:qa-specialist blocking-fix and warning counts or “clean.”

### Push

✓ Pushed to [Remote]/[Branch] — N new commits

> Exactly one status line, chosen by the recorded `PUSH_STATUS`, with the key shown in backticks after it:
>
> | `PUSH_STATUS` | Line |
> | -- | -- |
> | `pushed` | `✓ Pushed to [Remote]/[Branch] — N new commits` |
> | `blocked-guard` | `⚠ Push blocked by push guard — run the lines under Unblock push at the end of this report` |
> | `blocked-permission` | `⚠ Push blocked — git push permission not granted; run the command under Unblock push` |
> | `blocked-needs-manual-push` | `⚠ Push needs your shell — no upstream tracking, and no approval covers the explicit-refspec push; run the line under Unblock push` |
> | `rejected-non-ff` | `✗ Push rejected (non-fast-forward) — merge the remote branch, then push manually` |
> | `skipped-by-user` | `⊘ Push skipped by you (Step 3d "don't push" or Step 10 "Deny") — run git push when ready` |
> | `not-attempted` | `✗ Push not attempted — push scope could not be computed` |
> | `none` | `⊘ No push this run (no PR number, or all items skipped)` |

**Next**:

- Maintainer reviews, clicks Merge in GitHub UI — merge commit keeps per-item commits; squash collapses them

## Confidence

<!-- format per quality-gates.md: Score 0.N, Gaps bullets, Refinements N passes (omit if 0) -->

**Score**: 0.NN — [high ≥0.9 | moderate 0.85–0.9 | low \<0.85 ⚠]

**Gaps**:

- (-0.NN) [specific limitation]

**Refinements**: N passes.

- Pass 1: [what gap was addressed]

## Unblock push

> `blocked-guard` / `blocked-permission` / `blocked-needs-manual-push` only, and always the last section of the report — after **Next**, the Challenge Log and Confidence. Repeat the lines of `push-unblock.txt` verbatim in one fenced block; omit the section for every other status.
