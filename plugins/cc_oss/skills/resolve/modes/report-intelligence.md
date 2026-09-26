<!-- oss:resolve Step 3a — executed inline: cat $_OSS_RESOLVE/modes/report-intelligence.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 3a) -->

## Step 3a: Report intelligence (report mode only)

*Skip to Step 3b (PR intelligence) when in pr mode or pr + report mode.*

<!-- Sources block template (used in 3a/3b/3c): fields GitHub and Report vary by mode -->

When mode == **report**:

Source file = `REPORT_FILE`, already resolved and gated by SKILL.md Step 1's **Report source resolution** block: `IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"`. Never glob for it again here, and never conclude "no report" from an empty sentinel — empty means that block has not run yet; run it, including its `AskUserQuestion` gate when nothing is found. Starting a fresh `oss:review` without that gate is the documented failure this path exists to prevent.

### Report header state

Read the validated report's frontmatter `PR:` field once and publish the exact PR number for every later shell. A bare `report` invocation left the Step 1 sentinel at `n/a`; changing only `$ARGUMENTS` here did not change what Step 4 and later blocks read. An absent PR is explicitly `n/a`; malformed or conflicting PR fields block the route.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
[ -f "$REPORT_FILE" ] || { echo "⛔ Report source missing; run the Step 1 report gate"; exit 1; }
: > "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}"
_REPORT_PR_FIELD=$(awk 'NR == 1 { next } { sub(/\015$/, ""); if ($0 == "---") exit; if ($0 ~ /^PR:[[:space:]]*/) { sub(/^PR:[[:space:]]*/, ""); print } }' "$REPORT_FILE")
if [[ "$_REPORT_PR_FIELD" =~ ^#([1-9][0-9]*)$ ]]; then
    PR_NUMBER="${BASH_REMATCH[1]}"
elif [ "$_REPORT_PR_FIELD" = "n/a" ]; then
    PR_NUMBER="n/a"
else
    echo "⛔ Report PR field is not a PR number or n/a; refusing an ambiguous checkout"
    exit 1
fi
_HEADING_PR=$(sed -nE 's/^## Code Review: (PR #|#)?([1-9][0-9]*)([[:space:]].*)?$/\2/p' "$REPORT_FILE" | head -1)
[ -z "$_HEADING_PR" ] || [ "$_HEADING_PR" = "$PR_NUMBER" ] || { echo "⛔ Report PR heading conflicts with frontmatter"; exit 1; }
printf '%s\n' "$PR_NUMBER" > "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}"
echo "→ Report PR: $PR_NUMBER"
```

Print Sources block before parsing findings:

```markdown
## Resolve — sources

Mode   : report
PR     : #<N>  (extracted from report header, or "n/a — working on current branch")
GitHub : not fetched
Report : Read <path to report file>

Building action items…
```

<!-- loads: review-section-taxonomy.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 41)
cat "$_OSS_SHARED/review-section-taxonomy.md"  # timeout: 5000
```

Taxonomy (loaded above) — use **Grep pattern** row for header matching (contains-match; headers may carry `⚠ LOW CONFIDENCE — ` prefix), **Severity → Resolve Type** table for `type` assignment, **LOW Grouping Rule** for composite rows, **Owner agent** column for `author` field, and **resolve `change`** column for `change` field. Skip sections where Grep key is `— skip`.

- `author`: Owner agent column from taxonomy
- `change`: resolve `change` column from taxonomy — drives Step 8 Phase 2 specialist routing; do NOT default every report item to `code`, the taxonomy row already names the right value per section
- `file`/`line`: extract from `file:line` notation; blank if absent or grouped composite
- `full_comment_text`: full finding bullet (or concatenated bullets for composites)
- All items get `[report]` prefix on `type` (e.g., `[report][req]`, `[report][suggest]`)

Before printing the table, create and persist the same item source that Step 8 reads. Step 3b does not run in report mode, so Step 3a owns this directory and its session sentinel:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IMPL_DIR=$(mktemp -d)
printf '%s\n' "$IMPL_DIR" > "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}"  # timeout: 3000
```

Use the Write tool to write `$IMPL_DIR/action-items.jsonl` before Step 3d: one compact JSON object per classified ACTION_ITEM, including every displayed pending row. Use Step 3b's exact fields (`id`, `type`, `change`, `severity`, `author`, `summary`, `file`, `line`, `url`, `full_comment_text`, `location`, `origin`). Assign sequential numeric IDs starting at 1; use `location: "report"`, `origin: "posted"`, and an empty `url`. Preserve the full finding text and taxonomy-derived `change` and `author`; the shortened table summary is not a substitute for `full_comment_text`. Write an empty file when there are zero findings. The table below must be rendered from those same records with the same IDs; stop before Step 3d if the file cannot be written or differs from the displayed items. A compaction or Step 8 may reload only this file.

Print ACTION_ITEMS as a user-facing markdown table (severity descending):

```markdown
### Action Items — report

| # | Type | Change | Severity | Author | Status | Summary | Notes |
|---|------|--------|----------|--------|--------|---------|-------|
| 1 | [report][req] | code | 4 | foundry:sw-engineer | pending | rename param x to count | — |
```

Summary ≤60 chars. Notes = `—` when empty; carries commit SHA for `[done]` rows and classification verdicts (e.g. deprecation filter output) — never `file:line`, which the `file`/`line` fields already hold. Print before branching on PR# presence so the user can select from the full report in Step 3d.

PR# found in report header → use the persisted `PR_NUMBER` from the block above, set `$ARGUMENTS = <N>`, go to Step 3d, skip Step 3e, then Step 4; skip Step 3b. Step 3d chooses `SELECTED_ITEMS`, commit mode, and any over-20 batch before checkout. Continue through the normal post-checkout steps to Step 8.

No PR# in header → skip Steps 3b and 4; work on current branch as-is. Set fallback values for variables Step 8 reads: `HEAD_REF=$(git branch --show-current 2>/dev/null || echo "")` and `IS_FORK=false` (no cross-repo context). Run the local report commit reference block below, go to Step 3d, skip Step 3e, then use its selected IDs and commit mode in Step 8.

### Local report commit reference

On the no-PR path only, publish an explicit local-report marker before Step 8. It keeps per-item, grouped, and all-at-once commit messages truthful when Step 4's PR reference writer is skipped. The Step 1 reset and the Step 8 checks prevent an earlier PR's reference from being reused in this session.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
[ "$PR_NUMBER" = "n/a" ] || { echo "⛔ Local report reference requires no PR number"; exit 1; }
printf '%s\n' 'n/a (local report)' > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"
```

**Report mode — Step 8 behavior**: use only the `SELECTED_ITEMS` and commit mode produced by Step 3d. If the report produces zero pending action items, Step 3d sets `SELECTED_ITEMS=[]`; skip Step 8 and jump to Step 9.

**Challenge Log — Phase 1 not skippable in report mode.** Report-mode items reach Step 8 with `SELECTED_ITEMS` set above, same as any other mode — `action-item-dispatch.md`'s Phase 1 then runs unconditionally; only sanctioned skip is `--no-challenge` (SKILL.md), which omits Challenge Log section entirely. Do not shortcut Phase 1 by reusing a source report's own verdicts or `Recommendation` text as if it were Phase 1 output, even when that source is itself a prior `oss:review` report — a reviewer's own recommendation is exactly the unproven claim Phase 1 exists to independently re-verify (`action-item-dispatch.md`'s Part 1/Part 2 challenge contract). Reusing source verdicts instead of dispatching challenge agents is a spec violation to self-correct on, not a documented report-mode behavior.

**`BASE_REF` derivation (no-PR path)** — report mode without PR# skips Step 4, so it must publish the local default branch to the same sentinel Step 4 writes for a PR. Step 9 reloads this state in its own shell; an unknown base blocks QA rather than selecting an empty diff range. Comment dispatch jumps to Step 12 and does not run Step 9.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
: > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"
BASE_REF=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
BASE_REF="${BASE_REF#origin/}"
[ -n "$BASE_REF" ] && git check-ref-format --branch "$BASE_REF" >/dev/null 2>&1 || { echo "⛔ Cannot determine a valid origin default branch for report QA"; exit 1; }
printf '%s\n' "$BASE_REF" > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"
```
