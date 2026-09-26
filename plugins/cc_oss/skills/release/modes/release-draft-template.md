<!-- file: release-draft-template.md — consumers: release/SKILL.md (## Write release draft section) -->

### CHANGELOG Entry (`--changelog` flag)

Use this format:

```markdown
## [version] — [date]
### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security
```

### Internal Release Summary (`--summary` flag)

Use this format:

```markdown
## Release [version]
**What shipped**: [2-3 sentence summary of the most important changes]
**Impact**: [who is affected and how]
**Action required**: [anything ops/support/consumers need to do]
**Rollback**: [safe to roll back? any caveats?]
```

### Semantic consistency review

Runs on full draft after adversarial review, before writing to disk. Check for each:

| Check | What to look for | Flag format |
| -- | -- | -- |
| **Double-mention** | Same concept named twice under different labels (e.g. "async functions" and "async generators" as separate entries for same change) | `DUPLICATE: "<A>" and "<B>" describe the same change — merge or drop one` |
| **Impossible fix** | 🔧 Fixed entry whose subject was introduced in this same release (can't fix what was never shipped) | `IMPOSSIBLE-FIX: "<entry>" — feature added this release, can't be a fix` |
| **Causation non sequitur** | "X: Y" where Y doesn't explain or follow from X | `NON-SEQUITUR: "<X>: <Y>" — Y doesn't explain X` |
| **Contradictory claim** | Headline or first sentence asserts X; immediate caveat or next sentence denies X | `CONTRADICTION: "<headline>" contradicted by "<caveat>"` |
| **Verbatim duplication** | Identical or near-identical sentence appearing in ≥2 sections (Summary, Spotlight, Notable changes, Migration guide) | `VERBATIM-DUP: "<sentence>" appears in <section A> and <section B>` |
| **Misclassified scope** | Internal-only change (dead code removal, doc reformat, test-only, CI config) appearing in user-facing section | `SCOPE: "<entry>" is internal-only — move to Internal or remove` |

For each finding: emit one flag line with location (`§<section-name>`, item text). Collect all findings before taking action — don't fix inline during scan.

**After scan**: zero findings → proceed to Polish. Findings present → list all; fix each; re-scan once; proceed only when clean.

### Polish and write to disk

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload SKILL_DIR (Check 41: fresh shell)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/guidelines/writing-rules.md" ] && cat "$SKILL_DIR/guidelines/writing-rules.md"  # timeout: 5000
```

Follow above (if present). If absent, proceed without style guidelines.

Dispatch shepherd for public-facing voice/tone review before writing to disk. Check availability first:

```bash
SHEPHERD_AVAILABLE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/check_agent.py" oss shepherd 2>/dev/null)  # timeout: 5000
# expand to literal value before spawning
SHEPHERD_DIR=".temp/release-shepherd-$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')-$(date +%Y-%m-%d)"
mkdir -p "$SHEPHERD_DIR"  # timeout: 5000
```

If `$SHEPHERD_AVAILABLE` equals `true`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/release-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
[ -f "$_OSS_SHARED/shepherd-voice.md" ] || { echo "⚠ shepherd-voice.md not found — falling back"; SHEPHERD_AVAILABLE=false; }  # timeout: 5000
```

If still `true`, write draft to `$SHEPHERD_DIR/draft.md`, then spawn:

```text
Agent(subagent_type="oss:shepherd", prompt="Review the full release draft at <$SHEPHERD_DIR/draft.md> for public-facing voice and tone. Apply shepherd voice guidelines: human and direct, no internal jargon, no staff names, no internal maintenance details. Write the revised content to <$SHEPHERD_DIR/shepherd-revised.md>. Return ONLY: {\"status\":\"done\",\"changes\":N,\"file\":\"<$SHEPHERD_DIR/shepherd-revised.md>\"}")
```

If `oss:shepherd` not available, use draft content directly — skip shepherd review. Humanizer is independent of shepherd availability: still run a `foundry:humanizer` pass (requires `foundry` plugin) on the draft before writing to disk on this branch — shepherd's absence doesn't exempt the draft from it.

Read `$SHEPHERD_DIR/shepherd-revised.md` → validate: `if [ -s "$SHEPHERD_DIR/shepherd-revised.md" ]; then SHEPHERD_REVISED_PATH="$SHEPHERD_DIR/shepherd-revised.md"; else echo "⚠ shepherd output empty or missing — using original draft"; SHEPHERD_REVISED_PATH="$SHEPHERD_DIR/draft.md"; fi`. Shepherd runs once per invocation.

Write to disk:

Shepherd review policy (applies when `$SHEPHERD_AVAILABLE == true`):

<!-- branch: draft-exists — only when DRAFT.md non-empty (notes/prepare path); call 1 of ≤2 on notes+changelog path -->

- **notes** (always): shepherd review → write to `$APPEND_STAGE/DRAFT.md` for both plain and append notes; the candidate is published only after final validation.
  - **Append merge** — gate computed once, folding all three preconditions (`--append` set, DRAFT.md present, marker valid) into `$MARKER_VALID`:

    ```bash
    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    # Reload raw branch ref, DO_APPEND, LAST_TAG (Check 41: fresh shell)
    IFS= read -r BRANCH_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_REF" 2>/dev/null || BRANCH_REF=""
    IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
    IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
    if [ "$DO_APPEND" = "true" ] && [ -s DRAFT.md ]; then
        # is-valid also rejects a marker superseded by a later release tag (see release_append_marker.py) —
        # keeps this gate in agreement with what Gather changes' `resolve` actually used for $RANGE
        MARKER_VALID=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_marker.py" is-valid --branch "$BRANCH_REF" --last-tag "$LAST_TAG")  # timeout: 5000
    else
        MARKER_VALID=false
    fi
    printf '%s\n' "$MARKER_VALID" > "${TMPDIR:-/tmp}/release-marker-valid-${CSID}"
    IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
    if [ -n "$APPEND_STAGE" ]; then
        [ -n "$APPEND_STAGE" ] && [ -f "$APPEND_STAGE/candidate.json" ] || { echo "Error: append stage missing; refusing live edits" >&2; exit 1; }
    fi
    ```

    When `$MARKER_VALID == true`: skip the Overwrite guard below entirely. Every Read/Edit/Write of `DRAFT.md` in this append merge and post-merge validation targets `$APPEND_STAGE/DRAFT.md`; use `$APPEND_STAGE/SUMMARY.md`, `$APPEND_STAGE/MIGRATION.md`, and `$APPEND_STAGE/$CHANGELOG_FILE` for the corresponding append artifacts. These are candidate copies, not live files. Report the user-visible paths without the stage prefix. The incremental range and candidate staging together prevent replay after an aborted or interrupted write.

    Build a merge plan — `$APPEND_ITEMS_FILE` (`.temp/release-append-items-$BRANCH-$DATE.json`), a JSON object mapping section header (exact key text, no markdown prefix) to `{"add": [...], "remove": [...]}`:

    - `"🚀 Added"` / `"⚠️ Breaking Changes"` / `"🌱 Changed"` / `"🗑️ Deprecated"` / `"❌ Removed"` / `"🔧 Fixed"` — `add` from Classify each change (this run's incremental classification)
    - `"🏆 Contributors"` — `add` from Extract contributors
    - `"✨ Spotlights / highlights"` — `add` from Identify highlights (one string per spotlight, its own `### <Feature>` sub-heading + write-up)
    - `"🔄 Migration guide"` — `add` from Draft migration guide (one string per breaking/deprecated symbol's before→after write-up)
    - `"📋 Summary"` — `add`: a single short paragraph from Draft executive summary describing just this increment (lands under a "### Since last draft" subheading; routine additions preserve the original summary paragraph, subject to the final truth gate below)
    - `remove` on any of the above: the `matched_text` values from Gather changes' `CROSS_CYCLE_MATCH` list targeting DRAFT.md — a revert/pivot superseding a bullet/block a PRIOR cycle already wrote. Omit or empty-list `add`/`remove` on any section with nothing new/stale.

    This plan file has two consumers: shepherd (voice review, one artifact to review in a single pass — below) and the Apply step directly below it — **no script ingests this file**; the model reads it and applies it itself.

    If `$SHEPHERD_AVAILABLE`, dispatch shepherd on this JSON directly (same availability check as above): `Agent(subagent_type="oss:shepherd", prompt="Review wording of every string in each section's \"add\" array in the JSON at <$APPEND_ITEMS_FILE> for public-facing voice/tone (same guidelines as full release notes — human and direct, no internal jargon, no staff names). Never touch \"remove\" arrays — those are match substrings, not published prose. Preserve every key exactly; write the same JSON shape with only \"add\" string values reworded to <$SHEPHERD_DIR/append-items-revised.json>. Return ONLY: {\"status\":\"done\",\"file\":\"<$SHEPHERD_DIR/append-items-revised.json>\"}")`; on success use the revised file as `$APPEND_ITEMS_FILE`, else keep the unrevised one.

    **Apply the plan to `$APPEND_STAGE/DRAFT.md` — Read + Edit tool, never a parsing script:**

    1. Read `$APPEND_STAGE/DRAFT.md` (Read tool) — get its actual current section structure; never assume the canonical template order, use whatever heading text is genuinely present (tolerant of a hand-edit dropping an emoji variation selector, changing case, or extra whitespace — still the same section).
    2. For each section key in `$APPEND_ITEMS_FILE` with a non-empty `remove`: locate that section. For each `remove` value (an exact bullet/block/line copied verbatim from `CROSS_CYCLE_MATCH`): Edit tool with `old_string` = that exact text plus enough surrounding context (its own blank-line neighbours, or its full `### <name>` sub-heading for a block item) to make the match unique — never `replace_all`. A section emptied down to nothing by removal (all items struck, nothing added) → drop its header line too, no dangling empty stub.
    3. For each section key with a non-empty `add`: same located section. No matching heading exists yet → add one in a sensible position near thematically-similar existing content (`templates/release-draft.md`'s section order is the tie-breaker when nothing nearby suggests a better spot — e.g. a new `### 🌱 Changed` heading belongs near `### 🚀 Added` / `### 🗑️ Deprecated`, not at a random spot). Edit tool to insert each new bullet/block into the section body — same unique-context discipline as removal.
    4. **Contributors**: dedup by bolded `**Name**` — an existing entry with the same bolded name is never duplicated; a genuinely new name is inserted as a new bullet.
    5. **Summary**: routine incremental content never rewrites the original paragraph. Add it under a `### Since last draft` subheading — search inside `## 📋 Summary`'s own body (don't assume a fixed line offset); create it on first use, append on later cycles. The final cross-artifact truth gate may correct an existing paragraph when source evidence shows its claim is stale.
    6. **Spotlights re-ranking** (when Identify highlights recomputes the top 3–5 over the union of surviving old + newly classified candidates — the winning set can drop or reorder entries, not just add): Edit the ENTIRE section body (every `### <Feature>` block between the heading and the next boundary) to the freshly computed final set, in final order. Carried-over entries keep their existing write-up verbatim; newly promoted ones get a fresh write-up.

    Same Read-locate-Edit pattern applies uniformly across every mergeable DRAFT.md section — prose (Summary), block (Spotlights, Migration guide), list (Notable-changes subsections, Contributors) — the model adapts each edit to whatever shape the section's actual content has; no separate code path per section kind.

    Report per-section add/remove counts from what was actually applied. Notify: `→ merged N new item(s), struck M stale item(s) from DRAFT.md`.

  - **Post-merge re-validation** (only after the Merge above succeeds): the 4 gates below ran scoped to just this cycle's incremental classify output, earlier in the pipeline — re-running them against the FINAL merged candidate `$APPEND_STAGE/DRAFT.md` catches drift a prior cycle's content develops from THIS cycle's changes without being a clean, detected `CROSS_CYCLE_MATCH`. Input for every check below is "current merged candidate content", not "`$RANGE` diff":

    1. **Truth check re-run** — apply `modes/classify-truth-check.md`'s category-specific predicates to every named symbol in merged DRAFT.md's 🚀 Added/⚠️ Breaking Changes/🌱 Changed/**❌ Removed** bullets and every Spotlights entry (old survivors + new), not only this cycle's entries. Added/Changed or a Spotlight final name absent at HEAD → strike that claim. A ❌ Removed old name present at `$LAST_TAG` and absent at HEAD → **keep the removal**; still present at HEAD or absent at baseline → strike it. Breaking/rename claims follow the original baseline review gate before any relabeling. Inconclusive probes keep a qualified claim. Capture each claim to strike by exact bullet/spotlight text in a section-scoped `POST_MERGE_REMOVE` list (same exact-text discipline as `CROSS_CYCLE_MATCH` — never a bare symbol), and append it to the waived-changes ledger (`modes/classify-truth-check.md` "Waived changes ledger"):
       ```bash
       export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
       IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
       [ -n "$WAIVED_FILE" ] && echo "POST_MERGE_REMOVE: $ANCHOR_TEXT — no longer verifiable in merged HEAD, struck from $ARTIFACT" >> "$WAIVED_FILE"  # timeout: 3000
       ```
    2. **Identify highlights re-rank** — re-run SKILL.md's Identify highlights ranking rule (breaking > new public API > major UX > notable fix) over the union of: current Spotlights entries (post-merge, minus anything `POST_MERGE_REMOVE` struck) + this cycle's own newly classified changes (already in context). Pick top 3–5. Resulting set or order differs from what's currently in DRAFT.md's Spotlights → build `updated_spotlights`: the complete ranked list, verbatim `### <Feature>` write-up for every carried-over entry, freshly drafted for anything newly promoted.
    3. **Validate migration docs re-run** — gather every ⚠ Breaking Changes / 🗑️ Deprecated / ❌ Removed bullet now in the merged DRAFT.md (old + new); re-run SKILL.md's Validate migration docs coverage check (same `$MIGRATION_DOC` detection + grep-based outcome table) against all of them, not just this cycle's new items — catches a gap that reopened (e.g. cross-cycle removal struck a bullet but not the migration doc's now-orphaned instructions).
    4. **Validate docs re-run** — re-run SKILL.md's Validate docs doc-alignment + doc-weight proportionality check over the full accumulated 🚀 Added set (old + new) in the merged DRAFT.md, same formula and UNDERTREATED threshold.
    5. **Audit changelog** — its earlier pass edits `$APPEND_STAGE/$CHANGELOG_FILE`, but may precede this DRAFT.md strike. Reconcile the candidate's current release section in the final cross-artifact gate below.

    **Apply** — same Read+Edit tool mechanism as Append merge above, scoped to just what these gates found: skip entirely when `$POST_MERGE_REMOVE` is empty and the re-rank produced no change to the Spotlights set (true no-op case). Otherwise: strike each `POST_MERGE_REMOVE` entry from its Notable-changes section (exact-text Edit, same discipline as any cross-cycle removal); if the re-rank changed the Spotlights set or order, Edit the whole Spotlights section body to `updated_spotlights` in final order (drop-or-reorder — not an add/remove delta). Report what changed.

    Surface migration/doc-alignment gaps as warnings in the final report — same non-blocking, self-correcting pattern as the upstream single-pass gates; never silently ship a draft known to reference a removed symbol or a stale spotlight.

    **Worked example** (stale spotlight superseded by a later revert): Cycle 1's Identify highlights picks "Async batch API" as a top-3 spotlight (from commit `abc123`); the Provenance record step (see "Post-write bookkeeping" below) computes `abc123`'s patch-id (`patchid1`) and records `{patch_id: patchid1, sha: abc123, artifact: DRAFT.md, anchor_text: "- **Async batch API** — new bulk-processing endpoint. (#10)"}` and a matching entry for the Spotlights block. Cycle 2's incremental range includes `Revert "feat: add async batch API"` — Gather changes' cross-cycle detection reads that revert commit's own `This reverts commit abc123...` trailer, computes `abc123`'s current patch-id (still `patchid1` even if `abc123` was reworded or cherry-picked since Cycle 1 — the diff, not the sha, is what's tracked), looks up `patchid1` in the provenance store, gets both matches back directly (no grep, no semantic confirmation needed) and strikes the Notable-changes bullet via `CROSS_CYCLE_MATCH`. Post-merge Truth check re-run finds the async-batch symbol no longer in HEAD → the "Async batch API" Spotlight is now also unverifiable → added to `POST_MERGE_REMOVE`. Identify highlights re-rank recomputes top 3–5 over the surviving candidates + this cycle's new changes — "Async batch API" drops out; the next-best candidate (say, a previously 4th-ranked fix now promoted to 3rd) takes its place. `updated_spotlights` replaces the whole section in one Edit — the stale entry is gone, never left sitting beside the new set.

  - **Normal write** — `$MARKER_VALID == false` (plain notes, or `--append` with no valid marker): write only `$APPEND_STAGE/DRAFT.md`; the overwrite guard examines that candidate copy, and publication below updates the live file only after final source validation. A missing candidate is a hard stop. No notes-mode Write/Edit may target live `DRAFT.md`, including overwrite, append, and an initially empty file. Recheck frozen source before editing the candidate:

    ```bash
    # Recheck source immediately before fallback draft write.
    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
    IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE="notes"
    if [ "$RELEASE_MODE" = notes ]; then
        IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
        [ -n "$APPEND_STAGE" ] && [ -f "$APPEND_STAGE/candidate.json" ] || { echo "Error: append candidate missing before fallback draft write" >&2; exit 1; }
        IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
        IFS= read -r FROZEN_HEAD < "${TMPDIR:-/tmp}/release-append-head-${CSID}" 2>/dev/null || FROZEN_HEAD=""
        IFS= read -r FROZEN_BRANCH < "${TMPDIR:-/tmp}/release-append-branch-${CSID}" 2>/dev/null || FROZEN_BRANCH=""
        FROZEN_TAGS_FILE="${TMPDIR:-/tmp}/release-append-tags-${CSID}"
        [ -n "$BRANCH" ] && [ -n "$FROZEN_HEAD" ] && [ -n "$FROZEN_BRANCH" ] && [ -f "$FROZEN_TAGS_FILE" ] || { echo "Error: frozen append source missing before draft write" >&2; exit 1; }
        python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_publish.py" check --branch "$BRANCH" --head-sha "$FROZEN_HEAD" --branch-ref "$FROZEN_BRANCH" --tag-state-file "$FROZEN_TAGS_FILE" || exit 1
    fi
    ```

    **Overwrite guard** — if `$APPEND_STAGE/DRAFT.md` is non-empty, invoke `AskUserQuestion` ("DRAFT.md already exists — overwrite, append, or abort?") with: (a) **Overwrite** · (b) **Append** (after `---` separator) · (c) **Abort**. Skip prompt only when the candidate draft is empty or missing. Notify: `→ written to DRAFT.md` / `→ appended to DRAFT.md` / `→ DRAFT.md unchanged — aborted`.

<!-- branch: changelog-confirm — only with --changelog flag; call 2 of ≤2 on notes+changelog path; max 4 total on prepare+changelog+draft path -->

- **`--changelog`** (if set): no shepherd (structured, internal) → invoke `AskUserQuestion`: "Ready to prepend to `$CHANGELOG_FILE`?" Options: (a) Proceed · (b) Preview only. On (b): display content, stop. On (a): derive `VERSION=$(git describe --tags --abbrev=0 2>/dev/null || echo "")` and `VERSION_BARE="${VERSION#v}"`. **Idempotency check**: inspect `$APPEND_STAGE/$CHANGELOG_FILE` for every notes run for a version header in any supported form (`## [1.2.0]`, `## [v1.2.0]`, `## v1.2.0`, `## 1.2.0`). Skip if present; otherwise prepend after `# Changelog` in that candidate file. Notify using `$CHANGELOG_FILE`, never the stage path.

**Collapse guard** (used by the `SUMMARY.md`/`MIGRATION.md` merges below — the two whole-file artifacts with no section structure of their own to sanity-check against; `$ARTIFACT` = whichever of the two is being merged this cycle). A cheap, mechanical byte-count trip-wire, not a parser — the one destructive-irreversible failure mode a purely LLM-driven merge can't self-correct from (the actual historical bug here was a whole-file wipe of MIGRATION.md), kept deliberately minimal so it never reintroduces the schema-brittleness this redesign retires. DRAFT.md's own sections are NOT guarded this way — a section legitimately emptying down to a dropped header (all items struck, nothing added) is intended behavior, not corruption, since the rest of the file is still there to sanity-check against.

Before this cycle's Read+Edit merge against `$ARTIFACT` begins:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_BEFORE=$(wc -c < "$ARTIFACT" 2>/dev/null || echo 0)
echo "${_BEFORE:-0}" > "${TMPDIR:-/tmp}/release-collapse-guard-${CSID}"  # timeout: 3000
```

After every Edit-tool operation against `$ARTIFACT` this cycle completes:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _BEFORE < "${TMPDIR:-/tmp}/release-collapse-guard-${CSID}" 2>/dev/null || _BEFORE=0
_AFTER=$(wc -c < "$ARTIFACT" 2>/dev/null || echo 0)
if [ "${_BEFORE:-0}" -gt 200 ] && [ "${_AFTER:-0}" -lt 20 ]; then
    echo "⚠ $ARTIFACT content collapsed from ${_BEFORE}B to ${_AFTER}B during merge"
fi  # timeout: 3000
```

Tripped → restore `$ARTIFACT` to the exact content read via the Read tool at the start of this merge cycle (Write tool — the model still holds it in context; no git dependency), stop, surface `⚠ $ARTIFACT merge refused — content would collapse from a substantial file to near-empty; restored pre-merge content, review the cross-cycle strike list manually.` Never leave a collapsed file in place. The 200B/20B threshold is a coarse "was this substantively non-empty before, is it now essentially gone" check — not a markdown-structure inspection.

- **`--summary`** (if set): no shepherd (internal).
- `$MARKER_VALID == true`: stable user-visible path `SUMMARY.md` at repo root; edit only `$APPEND_STAGE/SUMMARY.md`. Candidate missing (first append) → `cp "<executive-summary-draft>" "$APPEND_STAGE/SUMMARY.md"`; candidate exists → Read it (Read tool), locate `### Since last draft` inside it (create on first use, append to it on later cycles — same pattern as DRAFT.md's own Summary section, Append-merge step 5 above), Edit tool in this increment's paragraph, guarded by **Collapse guard** above (`$ARTIFACT=$APPEND_STAGE/SUMMARY.md`). A merge failure must never silently fall through to an overwrite. Source-backed corrections to stale prose belong to the final cross-artifact truth gate. Notify using `SUMMARY.md`, never the stage path.
- Otherwise: with `--append`, write the full fallback summary to `$APPEND_STAGE/SUMMARY.md`; for plain notes, write it to `$APPEND_STAGE/.temp/output-release-summary-$BRANCH-$DATE.md`, creating its parent directory inside the candidate. Publish to `.temp/output-release-summary-$BRANCH-$DATE.md` with the draft and marker. Notify using the user-visible path.
- **`--migration`** (if set): shepherd review (public-facing).
  - `$MARKER_VALID == true`: stable user-visible path `MIGRATION.md` at repo root; edit only `$APPEND_STAGE/MIGRATION.md`. Candidate missing (first append) → `cp "<migration-guide-draft>" "$APPEND_STAGE/MIGRATION.md"`; candidate exists → Read it (Read tool); for each new symbol write-up (Draft migration guide), Edit tool to append a new `### <symbol>` block; for each `CROSS_CYCLE_MATCH` targeting `MIGRATION.md`, Edit tool to strike the exact matched block (unique surrounding context, same discipline as DRAFT.md). Guarded by **Collapse guard** above (`$ARTIFACT=$APPEND_STAGE/MIGRATION.md`) — this is the artifact whose whole-file wipe was the actual historical bug; the guard remains a mechanical backstop. Notify using `MIGRATION.md`, never the stage path.
  - Otherwise: with `--append`, write the full fallback migration guide to `$APPEND_STAGE/MIGRATION.md`; for plain notes, write it to `$APPEND_STAGE/.temp/output-release-migration-$BRANCH-$DATE.md`, creating its parent directory inside the candidate. Publish to `.temp/output-release-migration-$BRANCH-$DATE.md` with the draft and marker. Notify using the user-visible path.

**Candidate validation and publication** (runs once after the artifact edits above; live notes artifacts remain untouched until publication):

- **Final cross-artifact truth gate**: after candidate edits, inventory every existing target, including selected dated plain outputs and unchanged standalone files; an existing file can carry claims from earlier cycles. The inventory is the set to inspect, not a promise to create missing optional files:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
  IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE="notes"
  if [ "$RELEASE_MODE" = notes ]; then
      IFS= read -r STORED_CHANGELOG_FILE < "${TMPDIR:-/tmp}/release-changelog-file-${CSID}" 2>/dev/null || STORED_CHANGELOG_FILE=""
      CHANGELOG_FILE="${STORED_CHANGELOG_FILE:-${CHANGELOG_FILE:-}}"
      IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
      [ -n "$APPEND_STAGE" ] && [ -f "$APPEND_STAGE/candidate.json" ] || { echo "Error: append candidate missing during validation" >&2; exit 1; }
      [ -n "$CHANGELOG_FILE" ] && [ -f "$APPEND_STAGE/$CHANGELOG_FILE" ] || { echo "Error: staged changelog missing during append validation" >&2; exit 1; }
      IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
      IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
      for ARTIFACT in DRAFT.md "${CHANGELOG_FILE:-}" SUMMARY.md MIGRATION.md ".temp/output-release-summary-$BRANCH-$DATE.md" ".temp/output-release-migration-$BRANCH-$DATE.md"; do
          [ -n "$ARTIFACT" ] && [ -f "$APPEND_STAGE/$ARTIFACT" ] && printf '%s\n' "$APPEND_STAGE/$ARTIFACT"
      done
      true
  fi  # release-artifacts: inspect every existing notes target regardless of flags
  ```
  Independently truth-check every current-release claim in each inventoried candidate artifact against `$LAST_TAG` and HEAD using the category-specific predicates in `modes/classify-truth-check.md`: DRAFT.md bullets, Spotlights, Summary, and Migration guide; the current release section of `$CHANGELOG_FILE`; standalone `SUMMARY.md`; and standalone `MIGRATION.md`. A stale standalone migration claim must be found and reconciled **without a DRAFT.md match** or a `POST_MERGE_REMOVE`/`CROSS_CYCLE_MATCH` seed. Use those lists as additional exact anchors, never as the complete input set. Match the same symbol/claim and PR or commit evidence, then remove or correct each stale bullet, block, or prose reference with Read+Edit and unique context **inside `$APPEND_STAGE` only**. Source-backed correction of stale DRAFT.md Summary or standalone SUMMARY.md prose is required here even though the routine append merge preserves earlier paragraphs; do not use a patch-id strike for prose. Keep unrelated historical changelog versions and migration entries. If source evidence is inconclusive or a correspondence ambiguous, **abort without publishing** and report exact user-visible files/claims. Re-read **all inventoried candidate artifacts** after edits; if a stale claim survives, abort before provenance or publication.
- **Provenance record**: for every bullet/block just written or merged this cycle that traces to specific commit(s) — DRAFT.md's Notable-changes subsections, Spotlights, Migration guide; `$CHANGELOG_FILE`'s Unreleased entries; standalone `MIGRATION.md` blocks — record its provenance. **Excluded from patch-id provenance**: Summary paragraphs (DRAFT.md's own `## 📋 Summary` section and standalone `SUMMARY.md`), because their blended prose has no reliable one-commit anchor; routine appends preserve earlier paragraphs, while the final truth gate above may correct stale claims using source evidence. Contributors remain excluded because credit is per person, not per commit revert.
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
  IFS= read -r BRANCH_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_REF" 2>/dev/null || BRANCH_REF=""
  IFS= read -r BRANCH_KEY < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_KEY" 2>/dev/null || BRANCH_KEY=""
  IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
  IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
  IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE="notes"
  IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
  [ "$RELEASE_MODE" != notes ] || { [ -n "$APPEND_STAGE" ] && [ -f "$APPEND_STAGE/candidate.json" ]; } || { echo "Error: notes candidate missing before provenance write" >&2; exit 1; }
  python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_marker.py" guard --branch "$BRANCH_REF" || exit 1
  PROVENANCE_FILE=".temp/release-state-v2/$BRANCH_KEY/provenance.json"
  [ "$RELEASE_MODE" != notes ] || PROVENANCE_FILE="$APPEND_STAGE/$PROVENANCE_FILE"
  mkdir -p "$(dirname "$PROVENANCE_FILE")" || exit 1
  [ -f "$PROVENANCE_FILE" ] || echo "[]" > "$PROVENANCE_FILE"  # timeout: 3000
  ```
  Read `$PROVENANCE_FILE` (Read tool). For each qualifying bullet/block, build one record per contributing commit sha (from Classify each change's sha tracking — see `modes/classify-truth-check.md` "PR accumulation") sharing that bullet's `anchor_text`. Compute each sha's content-stable identity first — this, not the sha, is the record's matching key:
  ```bash
  PATCH_ID=$(git show "<full-40-char-sha>" | git patch-id --stable | awk '{print $1}')  # timeout: 3000
  # empty for a merge commit shown without -m, or a genuinely empty commit — rare here since a
  # qualifying bullet traces to a real user-facing diff; record patch_id: null when this happens,
  # the entry then can only ever be struck via the semantic path (documented gap, not a bug)
  ```
  `{"patch_id": "<40-hex patch-id, or null>", "sha": "<full 40-char sha — debug metadata only, never a matching key>", "subject": "<original commit subject — human debugging only, never a matching key>", "artifact": "DRAFT.md"|"CHANGELOG.md"|"MIGRATION.md", "anchor_text": "<exact text just written, verbatim>", "written_at": "$DATE"}`. Append every new record to the array just read; write the complete updated array back to `$PROVENANCE_FILE` (Write tool). A `CROSS_CYCLE_MATCH`-struck bullet from THIS cycle is never re-recorded — it was removed, not written.
- **Marker refresh**: persists the baseline for future `--append` runs.
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
  IFS= read -r BRANCH_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_REF" 2>/dev/null || BRANCH_REF=""
  IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
  IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
  IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE="notes"
  IFS= read -r FROZEN_HEAD < "${TMPDIR:-/tmp}/release-append-head-${CSID}" 2>/dev/null || FROZEN_HEAD=""
  IFS= read -r FROZEN_END < "${TMPDIR:-/tmp}/release-append-end-${CSID}" 2>/dev/null || FROZEN_END=""
  IFS= read -r FROZEN_BRANCH < "${TMPDIR:-/tmp}/release-append-branch-${CSID}" 2>/dev/null || FROZEN_BRANCH=""
  IFS= read -r COMPLETED_RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || COMPLETED_RANGE=""
  FROZEN_TAGS_FILE="${TMPDIR:-/tmp}/release-append-tags-${CSID}"
  HEAD_SHA=$(git rev-parse --verify 'HEAD^{commit}' 2>/dev/null) || { echo "Error: release HEAD unresolved" >&2; exit 1; }  # timeout: 3000
  if [ "$RELEASE_MODE" = notes ]; then
      [ -n "$FROZEN_HEAD" ] && [ "$HEAD_SHA" = "$FROZEN_HEAD" ] || { echo "Error: HEAD changed after append gather; refusing marker advancement" >&2; exit 1; }
      [ -n "$FROZEN_BRANCH" ] && [ -f "$FROZEN_TAGS_FILE" ] || { echo "Error: frozen append source missing before marker refresh" >&2; exit 1; }
      python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_publish.py" check --branch "$BRANCH" --head-sha "$FROZEN_HEAD" --branch-ref "$FROZEN_BRANCH" --tag-state-file "$FROZEN_TAGS_FILE" || exit 1
  fi
  case "$COMPLETED_RANGE" in *...*|*..*..*) echo "Error: completed notes range must have one two-dot endpoint" >&2; exit 1;; *..*) ;; *) echo "Error: completed notes range missing" >&2; exit 1;; esac
  RANGE_END="${COMPLETED_RANGE##*..}"
  MARKER_SHA=$(git rev-parse --verify "$RANGE_END^{commit}" 2>/dev/null) || { echo "Error: completed notes endpoint unresolved" >&2; exit 1; }
  [ "$RELEASE_MODE" != notes ] || [ "$MARKER_SHA" = "$FROZEN_END" ] || { echo "Error: marker differs from completed notes endpoint" >&2; exit 1; }
  LIVE_BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || { echo "Error: release marker requires an attached branch" >&2; exit 1; }
  [ -n "$BRANCH_REF" ] && [ "$LIVE_BRANCH" = "$BRANCH_REF" ] || { echo "Error: release branch changed before marker refresh" >&2; exit 1; }
  IFS= read -r MARKER_VALID < "${TMPDIR:-/tmp}/release-marker-valid-${CSID}" 2>/dev/null || MARKER_VALID="false"
  IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
  [ "$RELEASE_MODE" != notes ] || { [ -n "$APPEND_STAGE" ] && [ -f "$APPEND_STAGE/candidate.json" ]; } || { echo "Error: notes candidate missing before marker refresh" >&2; exit 1; }
  RECEIPT_FILE=$(mktemp "${TMPDIR:-/tmp}/release-completed-notes-${CSID}-XXXXXXXX") || exit 1
  if [ -n "$MARKER_SHA" ] && [ "$RELEASE_MODE" = notes ]; then
      python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_marker.py" receipt --branch "$BRANCH_REF" --last-tag "$LAST_TAG" --range-file "${TMPDIR:-/tmp}/release-range-${CSID}" --draft "$APPEND_STAGE/DRAFT.md" --output "$RECEIPT_FILE" || exit 1
      python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_marker.py" write --branch "$BRANCH_REF" --last-tag "$LAST_TAG" --sha "$MARKER_SHA" --receipt "$RECEIPT_FILE" --draft "$APPEND_STAGE/DRAFT.md" --marker-dir "$APPEND_STAGE/.temp" || exit 1
      python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_publish.py" seal --branch "$BRANCH" --stage "$APPEND_STAGE" || exit 1
  fi  # timeout: 5000
  ```

For all notes, independently repeat the **Final cross-artifact truth gate** on every inventoried candidate artifact after `seal`, including provenance and marker consistency. The semantic review must read the sealed candidate. If a correction changes any candidate bytes, run `seal` again and repeat the full review; publish only the seal that passed without another edit. A candidate byte change after that review makes `publish` fail closed. Keep the journal for recovery if a live replacement was interrupted.

After the sealed candidate passes the full truth gate, publish it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
if [ -n "$APPEND_STAGE" ]; then
    [ -n "$APPEND_STAGE" ] && [ -f "$APPEND_STAGE/candidate.json" ] || { echo "Error: sealed append candidate missing" >&2; exit 1; }
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_publish.py" publish --branch "$BRANCH" --stage "$APPEND_STAGE" || exit 1
fi
```

**Human gate** — stop, hand off after writing files. GitHub release must be created with project-level tooling (`gh release create`). Exact release steps:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/release-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
cat "$_OSS_SHARED/release-checklist.md"  # timeout: 5000
```

> Confidence block — notes mode: end response here with `## Confidence` block per CLAUDE.md output standards.
