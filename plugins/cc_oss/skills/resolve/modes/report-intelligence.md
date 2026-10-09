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
# case + prefix strip, not [[ =~ ]]: zsh (the Bash tool's shell on macOS) never sets BASH_REMATCH
case "$_REPORT_PR_FIELD" in
    n/a) PR_NUMBER="n/a" ;;
    '#'[1-9]*) PR_NUMBER="${_REPORT_PR_FIELD#?}" ;;
    *) PR_NUMBER="" ;;
esac
case "$PR_NUMBER" in
    n/a) ;;
    ''|*[!0-9]*) echo "⛔ Report PR field is not a PR number or n/a; refusing an ambiguous checkout"; exit 1 ;;
esac
_HEADING_PR=$(sed -nE 's/^## Code Review: (PR #|#)?([1-9][0-9]*)([[:space:]].*)?$/\2/p' "$REPORT_FILE" | head -1)
[ -z "$_HEADING_PR" ] || [ "$_HEADING_PR" = "$PR_NUMBER" ] || { echo "⛔ Report PR heading conflicts with frontmatter"; exit 1; }
printf '%s\n' "$PR_NUMBER" > "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}"
echo "→ Report PR: $PR_NUMBER"
```

Sources block — it heads the `preview` of every bulk-action option of Step 3d's AskUserQuestion, above the action items table, same as `pr-intelligence.md`'s. Never as reply text before the parsing calls below: 5.5-family models may return reply text written before a tool call as an empty progress update.

```markdown
## Resolve — sources

Mode   : report
PR     : #<N>  (extracted from report header, or "n/a — working on current branch")
GitHub : not fetched
Report : Read <path to report file>
```

<!-- loads: review-section-taxonomy.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 41)
cat "$_OSS_SHARED/review-section-taxonomy.md"  # timeout: 5000
```

### Review findings source

The review's own structured findings are the handoff. Never re-derive what the review already recorded:

- **`findings.jsonl` beside the report** (written by the review consolidator, stable `id` per finding): use it as-is. No taxonomy parse, no retyping.
- **Older report without it**: parse the report once into the same shape. Use the Write tool to write `$IMPL_DIR/report-findings.jsonl`, then mint ids with the block below. Taxonomy (loaded above):
  - **Grep pattern** row for header matching (contains-match; headers may carry `⚠ LOW CONFIDENCE — ` prefix); skip sections where Grep key is `— skip`.
  - One record per finding bullet: `section` (canonical header), `severity` (`critical`/`high`/`medium`/`low`/`cosmetic`), `title`, `full_text` (the full bullet), `file`/`line` (from `file:line` notation, `""`/`null` if absent), `author` (Owner agent column), `change` (resolve `change` column — drives Step 8 Phase 2 specialist routing; never default every item to `code`). Never write `verify_verdict` or `verify_file` from report text: only the review's own `findings.jsonl` carries a verifier verdict, and the merge drops any verdict in a findings file resolve wrote itself.

Step 3b does not run in report mode, so Step 3a owns the item directory and its session sentinel:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IMPL_DIR=$(mktemp -d)
printf '%s\n' "$IMPL_DIR" > "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}"  # timeout: 3000
```

Pick the findings file, mint ids for a parsed legacy report, then build `$IMPL_DIR/action-items.jsonl` deterministically. `merge_action_items.py` assigns ids 1.. in findings order, maps severity to `[report][req]`/`[report][suggest]` per the taxonomy's **Severity → Resolve Type** table, and carries `finding_id`, `source_file` and `verify_file` through for Step 8, plus `verify_verdict: CONFIRMED` only for findings from the review's own `findings.jsonl` that name a verifier file. Re-running it skips findings already present:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing"; exit 1; }
[ -f "$REPORT_FILE" ] || { echo "! BLOCKED — report sentinel empty; run Report source resolution (SKILL.md Step 1) first"; exit 1; }
FINDINGS="$(dirname "$REPORT_FILE")/findings.jsonl"
if [ ! -s "$FINDINGS" ]; then
    FINDINGS="$IMPL_DIR/report-findings.jsonl"
    [ -f "$FINDINGS" ] || { echo "! BLOCKED — no findings.jsonl beside the report; write $FINDINGS from the parsed report first"; exit 1; }
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/mint_finding_ids.py" "$FINDINGS" || exit 1  # timeout: 5000
fi
printf '%s\n' "$FINDINGS" > "${TMPDIR:-/tmp}/resolve-findings-file-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" --findings "$FINDINGS"  # timeout: 5000
```

- Zero findings leave an empty `action-items.jsonl`.
- Render the table below from those records with the same IDs. Stop before Step 3d if the merge exits non-zero.
- A compaction or Step 8 may reload only `action-items.jsonl`.
- Stored items stay one per finding, so each keeps its `finding_id` and evidence paths, and the displayed table keeps one row per item id at every pending count — LOW items are never clustered into composite rows (taxonomy **LOW Grouping Rule**); past 18 pending, SKILL.md Step 3d's compressed table still lists every row, which the selection gate checks id by id.

Render ACTION_ITEMS as a markdown table (severity descending), below the Sources block in the same preview:

```markdown
### Action Items — report

| # | Type | Change | Severity | Author | Status | Summary | Notes |
|---|------|--------|----------|--------|--------|---------|-------|
| 1 | [report][req] | code | 4 | foundry:sw-engineer | pending | rename param x to count | — |
```

Columns exactly as above — never add `File`, `Sev`, `Loc` or any other column. Summary ≤60 chars. Notes = `—` when empty; carries commit SHA for `addressed` rows and classification verdicts (e.g. deprecation filter output) — never `file:line`, which the `file`/`line` fields already hold. Render it exactly once, as the `preview` of every bulk-action option of Step 3d's AskUserQuestion (SKILL.md Step 3d **Table in the picker preview**) — or, past the preview cap (2000 chars, 12 lines), in `$IMPL_DIR/action-items-table.md` with the Sources block on top, Q1's question text naming that file with the line `→ Full item table: <path>`, and every preview carrying the same capped summary naming it (SKILL.md Step 3d **Preview cap**) — never also as reply text before the picker, never Bash/tool stdout, never here and again at the gate.

PR# found in report header → use the persisted `PR_NUMBER` from the block above, set `$ARGUMENTS = <N>`, go to Step 3d, skip Step 3e, then Step 4; skip Step 3b. Step 3d chooses `SELECTED_ITEMS` and commit mode before checkout. Continue through the normal post-checkout steps to Step 8.

No PR# in header → skip Steps 3b and 4; work on current branch as-is. Set fallback values for variables Step 8 reads: `HEAD_REF=$(git branch --show-current 2>/dev/null || echo "")` and `IS_FORK=false` (no cross-repo context). Run the local report commit reference block below, go to Step 3d, skip Step 3e, then use its selected IDs and commit mode in Step 8.

### Local report commit reference

On the no-PR path only, publish an explicit local-report marker before Step 8. It keeps per-item, grouped, and all-at-once commit messages truthful when Step 4's PR reference writer is skipped. The Step 1 reset and the Step 8 checks prevent an earlier PR's reference from being reused in this session.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
[ "$PR_NUMBER" = "n/a" ] || { echo "⛔ Local report reference requires no PR number"; exit 1; }
printf '%s\n' 'n/a (local report)' > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"
```

**Report mode — Step 8 behavior**: use only the `SELECTED_ITEMS` and commit mode produced by Step 3d. If `SELECTED_ITEMS` is empty after the user choice, skip Step 8 and jump to Step 9. Otherwise implement exactly those IDs, including explicitly selected resolved/addressed IDs; never replace the selection with the pending set.

**Challenge Log — Phase 1 not skippable in report mode.** Report-mode items reach Step 8 with `SELECTED_ITEMS` set above, same as any other mode — `action-item-dispatch.md`'s Phase 1 then runs unconditionally; only sanctioned skip is `--no-challenge` (SKILL.md), which omits Challenge Log section entirely. Do not shortcut Phase 1 by reusing a source report's own verdicts or `Recommendation` text as if it were Phase 1 output, even when that source is itself a prior `oss:review` report — a reviewer's own recommendation is exactly the unproven claim Phase 1 exists to independently re-verify (`action-item-dispatch.md`'s Part 1/Part 2 challenge contract). Reusing source verdicts instead of dispatching challenge agents is a spec violation to self-correct on, not a documented report-mode behavior.

One narrow, different evidence class is admissible: a `verify_verdict: CONFIRMED` carried in `action-items.jsonl`. That verdict was produced by the review's Step 4 cross-validation — a separate, independently spawned verifier that read the code, not the reviewer's own recommendation. It reaches only a `[report]` item built from that same finding: GitHub items never inherit it, and findings resolve parsed from an older report never carry it. Right before Phase 1, `merge_action_items.py --recheck-verdicts` drops it again unless the PR head still equals the head the review recorded and the verifier's file is still on disk. Phase 1 still dispatches for such an item. Its challenger skips re-proving existence (Part 1), re-checks only that the code still matches, and always runs Part 2 (is the fix right). Everything else keeps the full Part 1.

**`BASE_REF` derivation (no-PR path)** — report mode without PR# skips Step 4, so it must publish the local default branch to the same sentinel Step 4 writes for a PR. Step 9 reloads this state in its own shell; an unknown base blocks QA rather than selecting an empty diff range. Comment dispatch jumps to Step 12 and does not run Step 9.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
: > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"
BASE_REF=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
BASE_REF="${BASE_REF#origin/}"
[ -n "$BASE_REF" ] && git check-ref-format --branch "$BASE_REF" >/dev/null 2>&1 || { echo "⛔ Cannot determine a valid origin default branch for report QA"; exit 1; }
printf '%s\n' "$BASE_REF" > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"
```
