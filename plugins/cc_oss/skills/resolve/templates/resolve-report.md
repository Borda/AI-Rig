<!-- oss:resolve final report template — read by Step 11 for output format reference -->

> Replace square-bracket placeholders with observed values before delivery. Preserve status flags and evidence labels; those are report notation, not placeholders. Omit template instructions from the finished report.

## Resolve Report — PR #[PR number]

**Link**: [PR_HTML_URL, bare https URL; omit this line when the run has no PR]

[One-line delivery summary with the linked PR number and whether this run's pushed commit was verified on that PR. For other outcomes, state the push status and where the work is available.]

### Contribution

[Contribution summary]

> Use the two- to three-sentence motivation summary from Step 3b.

### Conflicts

[Conflict table]

> Use the conflict table from Step 7, or “No conflicts detected.”

### Action Items

<!-- One row per SELECTED item. Columns: # | Type | Change | Status | Resolution | Commit -->

<!-- Status: ✓ implemented / ⊘ skipped / ✗ challenge-rejected -->

<!-- Resolution: implemented / self-resolved / skipped / challenge-rejected -->

<!-- Change: code / test / docs / config / ci / style / refactor / perf / architecture -->

<!-- Commit: short SHA or "—" when COMMIT_MODE=stage -->

| # | Type | Change | Status | Resolution | Commit |
| -- | -- | -- | -- | -- | -- |
| 1 | [gh][req] | code | ✓ | implemented | `abc1234` |

### Challenge Log

<!-- One row per surviving/rejected item. Verdicts render as bracketed flags [VALID]/[REJECT] with a mandatory few-word reason — never a bare verdict word. Every cell self-contained — no cross-row lookups needed. Omit section when --no-challenge. -->

| # | Finding | Evidence | Resolution |
| -- | -- | -- | -- |
| 1 | Off-by-one in pagination cursor at api.py:88 | [VALID] — cursor increments before bounds check, confirmed in code | as-suggested: moved bounds check before cursor increment (`abc1234`) |
| 9 | Use `cv2.INTER_AREA` for all resizes | [VALID] — current code uses fixed interpolation regardless of scale direction | self-resolved: use INTER_AREA only when both target dims < source, else INTER_LINEAR |

<!-- No Suggestion column: Resolution is the outcome and already names what was done with the suggestion; suggestion_why stays in challenge-log.txt -->

<!-- ✗ wrong — bare verdict, no reason: | 3 | ... | VALID | ... | -->

<!-- ✓ right — the Evidence cell carries flag + reason, always the Phase 1 challenge agent's actual rationale — never a filler string standing in for a missing one -->

### Open Concerns

[Retain review-validation.json open_concerns and resolver UNVERIFIED items with stable ID, reason, owner, next_check and observable closure. Separate ALREADY_FIXED history with original/current revision and fix evidence. These are not rejected defects or implemented changes.]

### Review Intake

⚠ Review intake degraded ([report path]) — [critical count] critical · [degraded count] degraded gaps

| Rule | Severity | Subject | Detail |
| -- | -- | -- | -- |
| [rule] | [severity] | [role, candidate id, or -] | [detail] |

> Only when Step 11 printed `INTAKE=degraded`; omit the section otherwise. One row per `gaps[]` entry of the gap JSON printed beside it. `report` / `pr + report` mode: state that the confirmed findings were consumed and the open concerns carried non-actionable; `pr` mode: state that report findings were not merged (PR comments only). Either way, state that the review's report, sidecars and findings were left unchanged.

### Lint + QA

[Lint summary] / [QA summary]

> Report the linting-expert fix count or “no violations,” and the foundry:qa-specialist blocking-fix and warning counts or “clean.”

**PR code**: \[one line from `$IMPL_DIR/pr-code-decision.txt`\]

> Only for a fork PR. `auto`: `sandbox approved without asking — review and this run's scan both risk none at head <first 12 chars of PR_HEAD_OID>`; `sandbox`: `sandbox approved`; `unsandboxed`: `run WITHOUT sandbox approved (your choice)`; `deny`, `stale` or any other content: `tests not run — PR-code execution not approved`. The line states the approval only, never that code ran: whether tests ran comes from the execution-gap lines (a guard refusal or a skipped selection still reports `tests not run`). Omit the line for a same-repo PR and when the file is absent (a `report` run with no PR number records none). This line is the only place the user sees why the question was skipped, since Bash output is not reliably shown.

**Repository hooks**: `disabled from the merge on — the approved hooks changed (base merge or conflict resolution); commits ran without hooks`

> Only when `$IMPL_DIR/hooks-decision.txt` reads `stale` (the merge-commit refresh in `conflict-resolution.md` set the approval aside); omit the line otherwise.

### Push

[Push status and PR-head verification]

> Exactly one status line, chosen by the recorded `PUSH_STATUS`, with the key shown in backticks after it:
>
> | `PUSH_STATUS` | Line |
> | -- | -- |
> | `pushed` | Pushed to [Remote]/[Branch]. State the verified PR head when it matches, or “PR update unverified” when the heads differ or evidence is missing. |
> | `blocked-guard` | `⚠ Push blocked by push guard — run the lines under Unblock push at the end of this report` |
> | `blocked-permission` | `⚠ Push blocked — git push permission not granted; run the command under Unblock push` |
> | `blocked-needs-manual-push` | `⚠ Push needs your shell — no upstream tracking, and no approval covers the explicit-refspec push; run the line under Unblock push` |
> | `rejected-non-ff` | `✗ Push rejected (non-fast-forward)` |
> | `rejected-remote` | `✗ Push approved but failed — the remote refused it: [git's error line, verbatim from push-error.txt]` |
> | `skipped-by-user` | `⊘ Push skipped by you (Step 3d "don't push" or Step 10 "Hold") — run git push when ready` |
> | `not-attempted` | `✗ Push not attempted — push scope could not be computed` |
> | `none` | `⊘ No push this run (no PR number, or all items skipped)` |
> | `unknown` | Push status unavailable. |
>
> `skipped-by-user` is the user's decision, never a failure or a Confidence gap.

**Remote branch**: \[what the remote PR branch holds, from `REMOTE_STATE` and `REMOTE_DETAIL`\]

> Only when Step 11 printed a `REMOTE_STATE` other than `n/a`. Describe what the branch holds (`unchanged`, `pushed-externally`, `diverged`, `moved`, `absent`, `unknown`); never call it a safety problem and never name who moved it.

### GitHub writes for you

| # | Item | Write |
| -- | -- | -- |
| [id] | [item gist] | [what the write does] |

`gh api graphql -f query='mutation { [aliased replies and resolves for every row] }'`

> Only when an item needs a GitHub write this run did not make (PR description edit, thread resolution, alert dismissal, reply); omit otherwise. One combined command for every GraphQL-expressible row, so the user approves once; only text needing `--body-file` (a file in the run dir) gets its own command line.

### Run leftovers

\[One line per branch, worktree and stash entry, plus any `LEFTOVER_CHECK=incomplete` reasons\]

```text
[every CLEANUP= command, verbatim, in printed order]
```

> Include this section when Step 11 lists entries or reports incomplete checks. Include the command block only if there are `CLEANUP=` lines. Follow it with the printed `STASH_DROP=` text when present. The user decides whether to run the commands.

**Next**:

- [For a verified push, review the linked PR for merge. Otherwise give the action appropriate to the recorded status.]

## Confidence

<!-- format per quality-gates.md: Score 0.N, Gaps bullets, Refinements N passes (omit if 0); Step 9 DOCS_ONLY=true adds `- (-0.00) full test suite not run — documentation-only change` -->

**Score**: 0.NN — [high ≥0.9 | moderate 0.85–0.9 | low \<0.85 ⚠]

**Gaps**:

- (-0.NN) [specific limitation]

**Refinements**: N passes.

- Pass 1: [what gap was addressed]

## Unblock push

> `blocked-guard` / `blocked-permission` / `blocked-needs-manual-push` only, plus `rejected-remote` when the remote comparison shows missing run commits or delivery stays unverified, and always the last section of the report — after **Next**, the Challenge Log and Confidence. Repeat the lines of `push-unblock.txt` verbatim in one fenced block; omit the section for every other status.
