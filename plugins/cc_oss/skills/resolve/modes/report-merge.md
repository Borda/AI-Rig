<!-- oss:resolve Step 3c — executed inline: cat $_OSS_RESOLVE/modes/report-merge.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 3c) -->

## Step 3c: Merge report findings (pr + report mode only)

*Skip when in pr mode.*

! NO user input in this step — deterministic merge only; Step 3d handles all user selection.

When mode == **pr + report**:

Use the report already resolved by **Report source resolution** (Step 1) — `IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"`. Never re-glob here: a second newest-of-any-PR lookup can hand this step another PR's findings. Empty sentinel means that block never ran — run it now and honour its gate. Findings source follows **Review findings source** in `modes/report-intelligence.md`, Step 3a's file, which this mode never loads: the review's `findings.jsonl` beside the report is used as-is. Only for an older report without it, run Step 3a's load block above and that file's taxonomy `cat` block, then apply only its **Review findings source** parse rules to write `$IMPL_DIR/report-findings.jsonl` once with the Write tool; never run its `mktemp` block, which would replace this run's `IMPL_DIR` sentinel.

**Merge, never rewrite by hand.** GitHub comments are often terse; the review's local finding for the same code carries the detail and the evidence paths. `merge_action_items.py` folds the review findings into Step 3b's `action-items.jsonl` deterministically:

- **Same `file:line` as a pending GitHub item** → same target, including the survivor's location or any root-cause-merged row's location; match the GitHub item to the survivor. The GitHub item keeps its id and inherits `finding_id`, the full finding text (appended under the comment), `source_file` and `verify_file`. Author becomes `@login + <owner-agent>`, Summary gains `(also flagged by /review — <owner-agent>)`. It never inherits `verify_verdict`: the verifier confirmed the review's claim, not the GitHub comment's, so Step 8 Phase 1 still tests the item in full, reading the reviewer's evidence first.
- **Same target at another line** (semantic match — your judgment, listed by `--candidates`) → same annotation and evidence paths.
- **One finding per item.** A separate finding on the same line, or a link to an item that is closed or already holds a finding, is not folded; that finding is appended as its own item. The summary counts ignored links as `links_ignored`.
- **Root-cause merge** → a `merged_into` row naming a single new survivor's `candidate_id` adds no item of its own. The survivor's item lists `merged_finding_ids`, appends each merged row's text, takes the group's highest severity, and, for a `[report]` item, becomes `[req]` when any member alone would be. A target that is missing, ambiguous, chained, self-referencing or already placed by an earlier merge keeps the row as its own item. Relay each resulting `⚠` stderr line to the user; the summary JSON reports the `folded` count and the `fold_orphans` list of `{finding_id, merged_into}` records.
- **No match** → appended as a `[report]` item with the next free id (`G+1`, `G+2`, …). GitHub ids `1..G` never change, so no renumbering, and table IDs, Step 3d `SELECTED_ITEMS`, Step 3e task IDs and Step 8 lookups always name the same item.
- **Unmatched pending GitHub item with a short comment** (not a `[question]`) → marked `thin: true`; Step 8 Phase 1 reassesses it from scratch.
- Re-running the merge is safe: findings already present by `finding_id` or `merged_finding_ids` are skipped.
- When any GitHub row is annotated, the script replaces the file in one atomic rename instead of appending; ids and rows are never retyped.

Step 1 — choose the findings file and list same-file pairs that did not match exactly:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] && [ -f "$IMPL_DIR/action-items.jsonl" ] || { echo "! BLOCKED — Step 3b action-items.jsonl missing"; exit 1; }
[ -f "$REPORT_FILE" ] || { echo "! BLOCKED — report sentinel empty; run Report source resolution (Step 1) first"; exit 1; }
IFS= read -r INTAKE_DEGRADED < "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" 2>/dev/null || INTAKE_DEGRADED=""
# the script picks findings.jsonl beside the report (else mints the parsed $IMPL_DIR copy) and persists its path;
# --intake-accepted admits only the report accepted at Gap triage, compared by file identity (relative vs absolute spelling)
python "$PLUGIN_ROOT/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" --report-file "$REPORT_FILE" --impl-dir "$IMPL_DIR" --persist-findings "${TMPDIR:-/tmp}/resolve-findings-file-${CSID}" --intake-accepted "$INTAKE_DEGRADED" --candidates  # timeout: 5000
```

Step 2 — for each printed pair, decide whether the finding and the GitHub item describe the same problem (similar description, not merely the same file). Write the accepted pairs with the Write tool to `$IMPL_DIR/report-links.jsonl`, one `{"finding_id": "<id>", "item_id": <N>}` per line; write an empty file when none match. Then merge:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r FINDINGS < "${TMPDIR:-/tmp}/resolve-findings-file-${CSID}" 2>/dev/null || FINDINGS=""
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r INTAKE_DEGRADED < "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" 2>/dev/null || INTAKE_DEGRADED=""
[ -f "$IMPL_DIR/report-links.jsonl" ] || { echo "! BLOCKED — write report-links.jsonl (empty when no semantic match) before merging"; exit 1; }
[ -n "$INTAKE_DEGRADED" ] && [ "$INTAKE_DEGRADED" -ef "$REPORT_FILE" ] && ACCEPT_DEGRADED="--accept-degraded" || ACCEPT_DEGRADED=""
python "$PLUGIN_ROOT/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" --findings "$FINDINGS" --links "$IMPL_DIR/report-links.jsonl" ${ACCEPT_DEGRADED:+"$ACCEPT_DEGRADED"}  # timeout: 5000
```

- The printed summary's `deduped_exact + deduped_semantic` is the `<M>` and `appended` is the `<K>` of the merge summary line below. These count survivor and standalone items, not folded rows: `findings = already_merged + <M> + <K> + folded`. `fold_orphans` rows remain included in `<M>` or `<K>`, never in `folded`.
- A non-zero exit stops the run before the merged table or Step 3d; never let Step 8 consume a half-merged file.
- Keep a zero-item file empty. Render the merged table from `action-items.jsonl` itself, so displayed IDs are stored IDs.

**GitHub prefixes**: Step 3b already writes `[gh][req]` / `[gh][suggest]` / `[gh][question]`, and the merge never changes an item's `type`. Never edit `action-items.jsonl` by hand to add a prefix.

### Sources confirmation

Sources block (same format as Step 3a template; Mode=pr + report · PR=#<N> · GitHub=Read — PR body · <N> comments · <N> reviews · <N> inline code comments · <N> recurring findings merged · Report=Read <path>), then the Step 3b contribution motivation, head `$IMPL_DIR/action-items-table.md`, above the merge summary and the action item table. Print that headed table once as the last text of the reply message that issues Step 3d’s picker, never in an option’s `preview`. This merged block replaces `pr-intelligence.md`’s own Sources block; Q1 lists pending items as plain lines (**Items in the question text** below).

Result: single merged `ACTION_ITEMS`. Storage order is append order (GitHub items `1..G`, then `[report]` items `G+1..`); the table below displays rows severity descending and always shows each row's stored id in `#`. Merge summary — heads the saved table and the pre-picker reply:

```text
Report merged: <N> findings from /review · <M> deduplicated against GitHub comments · <K> added as [report] items
```

**MANDATORY — render merged ACTION_ITEMS as markdown table** (severity descending; same columns as pr-intelligence.md table), write it to `$IMPL_DIR/action-items-table.md` before the call, and print the headed table exactly once as the last text of the reply message that issues Step 3d’s picker — never in an option’s `preview`, never Bash/tool stdout. The Sources block, motivation and merge summary head the table, with every row below them. Q1 lists the pending items as plain lines (**Items in the question text** below). This table is selection-driving data, not a decorative table — render it in full under every compression mode and every communication style active this session (caveman included), never replace it with a prose count or summary line. A reply that references "the table above" without the table in that reply is a defect.

```markdown
### Action Items — PR #<N> (merged)

| # | Type | Change | Severity | Author | Status | Summary | Notes |
|---|------|--------|----------|--------|--------|---------|-------|
| 1 | [gh][req] | code | 4 | @reviewer | pending | rename param x to count | — |
| 2 | [gh][suggest] | docs | 2 | @reviewer + foundry:doc-scribe | pending | add docstring (also flagged by /review — foundry:doc-scribe) | — |
| 3 | [report][suggest] | docs | 2 | foundry:doc-scribe | pending | add docstring to Foo.bar | — |
```

**Author field rules** — Author = who owns fixing this item:

- `[gh]` items (no dedup): GitHub reviewer's `@login`
- `[gh]` items (dedup collision with report): `@login + <owner-agent>` (e.g. `@reviewer + foundry:doc-scribe`) — both authors preserved
- `[report]` items (no collision): Owner agent from taxonomy (e.g. `foundry:doc-scribe`, `foundry:qa-specialist`) — **never** the skill name `review` or `/review`

Columns exactly as above — never add `File`, `Sev`, `Loc` or any other column, and never rename one. Summary ≤60 chars. Notes = `—` when empty; carries commit SHA for `addressed` rows and classification verdicts — never `file:line`, which the `file`/`line` fields already hold. Status = item `status` (`pending`/`resolved`/`addressed`, pr-intelligence.md rule); Type never carries resolution. Print only when merged ACTION_ITEMS has ≥1 row.

`location` is a field, not a column — stays in `action-items.jsonl`, drives resolve routing, gets no column here: `[report]` origin already carried by `Type` and `Author`. One non-redundant bit is resolvability, so preserve it the same way every other table in this skill does — **append `· thread (no GH resolve)` to Status for `location: discussion` rows** (same rule as Step 11's table and the Step 3d picker). Never reintroduce a `Loc` column to restate what `Type`, `Author`, and that suffix already say. Merged table is authoritative set for Step 3d selection — supersedes Step 3b's pre-merge table, which is never printed in this mode.
