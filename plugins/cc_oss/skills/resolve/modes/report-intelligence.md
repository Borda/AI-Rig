<!-- oss:resolve Step 3a — executed inline: cat $_OSS_RESOLVE/modes/report-intelligence.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 3a) -->

## Step 3a: Report intelligence (report mode only)

*Skip to Step 3b (PR intelligence) when in pr mode or pr + report mode.*

<!-- Sources block template (used in 3a/3b/3c): fields GitHub and Report vary by mode -->

When mode == **report**:

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

Print ACTION_ITEMS as markdown table to terminal (severity descending):

```markdown
### Action Items — report

| # | Type | Change | Severity | Author | Status | Summary | Notes |
|---|------|--------|----------|--------|--------|---------|-------|
| 1 | [report][req] | code | 4 | foundry:sw-engineer | pending | rename param x to count | — |
```

Summary ≤60 chars. Notes = `—` when empty; carries commit SHA for `[done]` rows and classification verdicts (e.g. deprecation filter output) — never `file:line`, which the `file`/`line` fields already hold. Print before branching on PR# presence so user sees all items that will be executed (report mode skips Step 3d — no picker).

PR# found in report header → set `$ARGUMENTS = <N>`, go to Step 4; skip Step 3b entirely. After checkout, set `SELECTED_ITEMS` = all report-derived ACTION_ITEMS IDs (report mode executes all findings; no user selection step); skip to Step 8.

No PR# in header → skip Steps 3b and 4; work on current branch as-is. Before skipping, set fallback values for variables Step 8 reads: `HEAD_REF=$(git branch --show-current 2>/dev/null || echo "")` and `IS_FORK=false` (no cross-repo context). Set `SELECTED_ITEMS` = all report-derived ACTION_ITEMS IDs; skip to Step 8.

**Report mode — Step 8 behavior**: `SELECTED_ITEMS` initialized above; Step 3d (user selection) is skipped; Step 8 proceeds with all report-derived items. If report produces zero action items: `SELECTED_ITEMS=[]` → Step 8 skipped, jump to Step 9.

**`BASE_REF` derivation (no-PR path)** — when Step 3b skipped (report mode without PR#, or comment-dispatch mode), Step 9's lint-qa gate still needs `BASE_REF` for `git merge-base HEAD "origin/$BASE_REF"`. Without this, `BASE_REF` expands empty → `origin/` invalid ref → linting sees no changes → workflow pushes silently with vacuous QA gate. Set from local default-branch symbolic-ref before Step 8, guard downstream `git merge-base` against shallow-clone empty output (CI checkouts frequently use `--depth=1`, `merge-base` returns nothing — linting again sees no changes):

```bash
BASE_REF=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main")  # timeout: 3000
# shallow-clone fallback: git merge-base returns empty in --depth=1 clones
# and git diff <empty>..HEAD shows entire branch history (or nothing)
MERGE_BASE=$(git merge-base HEAD "origin/$BASE_REF" 2>/dev/null)  # timeout: 3000
if [ -z "$MERGE_BASE" ]; then
    MERGE_BASE=$(git rev-list --max-parents=0 HEAD 2>/dev/null | head -1)  # timeout: 3000
fi
```
