<!-- oss:release Mode: prepare — executed via: cat "$SKILL_DIR/modes/prepare.md"; execute -->

<!-- Variables available: $SKILL_DIR, $_OSS_SHARED, $LAST_TAG, $BRANCH, $DATE, $RANGE, $VERSION, $REPO_ROOT, $GATHER_FILE, $CHANGELOG_FILE -->

**Trigger**: `/release prepare <version>` (e.g., `prepare v1.3.0` or `prepare 1.3.0`)

**Purpose**: Full release pipeline — audit first, generate all artifacts. Use when cutting release; individual modes for drafting.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell loses vars; Mode Detection persists REST to tmpdir
IFS= read -r REST < "${TMPDIR:-/tmp}/release-rest-${CSID}" 2>/dev/null || REST=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
VERSION="${REST%% *}"
[[ "$VERSION" != v* ]] && VERSION="v$VERSION"
RANGE="${RANGE:-$LAST_TAG..HEAD}"
echo "$VERSION" > "${TMPDIR:-/tmp}/release-prepare-version-${CSID}"  # persist for later blocks (Check 41)
echo "$RANGE" > "${TMPDIR:-/tmp}/release-range-${CSID}"  # reuses the shared sentinel written by release/SKILL.md:256, read at :125,:442 and classify-truth-check.md:84
echo "range: $RANGE"
# BRANCH, DATE, REPO_ROOT, SKILL_DIR from Shared setup above
```

### Phase 1: Readiness audit

Run all checks from **Mode: audit** with `$VERSION` as target. `| Check | Status | Detail |` readiness table must appear inline in terminal before proceeding — audit-checks.md requires this even in sub-phase context. If table absent from response after running audit, re-execute terminal output step from audit-checks.md before continuing.

**If verdict is BLOCKED**: stop. List blockers, tell user to resolve before re-running `/release prepare $VERSION`. Write no artifacts.

**If verdict is READY or NEEDS_ATTENTION**: surface warnings, continue to Phase 2.

### Phase 2: Gather, classify, and changelog

**a. Gather and classify** — spawn gather subagent per **Delegation strategy** for `$RANGE`; write findings to `GATHER_FILE`. Read returned JSON envelope; pass file path downstream. Don't read gather file into main context. Keep qualified ⚠️ Breaking Changes and ❌ Removed rows with unresolved baselines in the classified table; their "(not baseline-verified)" status follows the item into downstream artifacts and review, never treated as verified or moved to the waived ledger. Note `breaking` count from envelope — gates Phase 3b (migration guide). After envelope validation, apply the Delegation strategy's item-evidence review gate when `unconfirmed_breaking > 0` before proceeding to 2b.

**b. Audit changelog** — Agent A must preflight its selected changelog path before its first read or write, as specified in `modes/changelog-audit-prompt.md`. Recheck the resolved `$CHANGELOG_FILE` and every existing release artifact path before this inline edit; a failure stops this phase without further artifact writes:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}" 2>/dev/null || VERSION=""
[ -n "$VERSION" ] && [ -n "$CHANGELOG_FILE" ] || { echo "Error: release version or changelog path missing" >&2; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/setup_release_dir.py" --validate-only "releases/$VERSION" "$CHANGELOG_FILE" || exit 1
```

Then apply **Audit changelog** logic inline: cross-check classified changes from `$GATHER_FILE`, add missing entries, stamp unreleased section as `## [$VERSION] — $DATE`. Report: "N items added, M flagged."

Persist the resolved `$CHANGELOG_FILE` through the Audit changelog path sentinel in `SKILL.md` before the next shell; do not assume the path remains in memory.

### Phase 3: Highlights and migration

Set up release directory, back up existing artifacts:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}" 2>/dev/null || VERSION=""  # reload (Check 41)
IFS= read -r CHANGELOG_FILE < "${TMPDIR:-/tmp}/release-changelog-file-${CSID}" 2>/dev/null || CHANGELOG_FILE=""  # written by Phase 2b's changelog-audit envelope
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT="."
[ -n "$CHANGELOG_FILE" ] || CHANGELOG_FILE=$(find "$REPO_ROOT" -maxdepth 2 -name "CHANGELOG.md" 2>/dev/null | head -1)  # audit.md search order
RELEASE_DIR="releases/$VERSION"
# Hard-fail: this script's .bak backup loop is the only thing standing between a re-run and silent
# loss of hand-edited HIGHLIGHTS/MIGRATION/SUMMARY/DRAFT, which Phases 3a-5 overwrite unconditionally.
# The helper refuses release-directory symlink traversal before unlinking or backing up files.
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/setup_release_dir.py" "$RELEASE_DIR" "$CHANGELOG_FILE" \
    || { echo "! BLOCKED — setup_release_dir failed (RELEASE_DIR='$RELEASE_DIR' CHANGELOG_FILE='$CHANGELOG_FILE'); artifact backups did not run — refusing to continue into the overwriting Write phases"; exit 1; }  # timeout: 5000
```

**a. Identify highlights** — apply **Identify highlights** logic using classified changes from `$GATHER_FILE`: rank top 3–5 most significant changes (breaking > new public API > major UX > notable fixes), pull one concrete code example per highlight from diff. Write to `releases/$VERSION/HIGHLIGHTS.md`. Source of truth for demo, executive summary, release draft spotlights

**b. Draft migration guide** — apply **Draft migration guide** logic using breaking/deprecated changes from `$GATHER_FILE`. No breaking changes → single line: `No breaking changes in this release.` Shepherd voice review applies. Write to `releases/$VERSION/MIGRATION.md`.

### Phase 4: Demo and summary

**a. Demo notebook** — reuse `$GATHER_FILE` and `releases/$VERSION/HIGHLIGHTS.md` from Phase 3. Apply demo generation logic from **Mode: demo**, Phase 2 (Generate demo script). Output path:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}" 2>/dev/null || VERSION=""  # reload (Check 41)
DEMO_OUT="releases/$VERSION/demo.py"
echo "$DEMO_OUT" > "${TMPDIR:-/tmp}/release-prepare-demo-out-${CSID}"  # persist for next block (Check 41)
```

Write generated script to `$DEMO_OUT` using Write tool. **Execution gate** — run:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r DEMO_OUT < "${TMPDIR:-/tmp}/release-prepare-demo-out-${CSID}" 2>/dev/null || DEMO_OUT=""  # reload (Check 41)
python "$DEMO_OUT"  # timeout: 600000
```

<!-- policy-sibling: plugins/cc_oss/skills/release/SKILL.md §Generate release demo (demo retry bound) -->

Fix and re-run until exits 0 with expected output — **max 3 attempts total**. Don't proceed to 4b until gate passes. Still failing after the 3rd attempt: stop retrying, invoke `AskUserQuestion` ("Demo still failing after 3 attempts. Exclude from release and continue, or abort?") — (a) **Exclude and continue**: mark demo excluded, drop the `demo.py` bullet from the Written list and the `jupytext` item from Next steps, note the exclusion + last failure reason in the output, and run 4b from `HIGHLIGHTS.md` alone · (b) **Abort**: stop, report the 3 failed attempts, write no further artifacts.

**b. Executive summary** — apply **Draft executive summary** logic using `releases/$VERSION/HIGHLIGHTS.md` and demo output (demo excluded → HIGHLIGHTS.md alone). Write to `releases/$VERSION/SUMMARY.md`.

### Phase 5: Write release draft

`releases/$VERSION/DRAFT.md` — final assembly. Source: `releases/$VERSION/HIGHLIGHTS.md` (spotlights), `releases/$VERSION/MIGRATION.md`, `releases/$VERSION/SUMMARY.md`. Apply **Write release draft** logic (release-draft.md format). Adversarial review applies (use `$GATHER_FILE` from Phase 2a as gather context). Shepherd voice review applies.

### Phase 6: Consolidate waived changes

Every ⚠️ Breaking/❌ Removed/rename claim that Truth check, Gather changes' cross-cycle detection, or Post-merge re-validation excluded during this run lives in this invocation's `$WAIVED_FILE` (`.temp/release-waived-$BRANCH-$DATE-<unique suffix>` — see `modes/classify-truth-check.md` "Waived changes ledger"), never in a public artifact. Consolidate it into the version directory so the audit trail survives past `.temp/` cleanup:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}" 2>/dev/null || VERSION=""  # reload (Check 41)
IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
IFS= read -r UNCONFIRMED < "${TMPDIR:-/tmp}/release-unconfirmed-${CSID}" 2>/dev/null || UNCONFIRMED=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
OUT="releases/$VERSION/waived-changes.md"
[ -n "$VERSION" ] && [ -n "$WAIVED_FILE" ] && [ -f "$WAIVED_FILE" ] || { echo "Error: waiver ledger or release version missing — cannot claim no waived changes" >&2; exit 1; }
case "$UNCONFIRMED" in ''|*[!0-9]*) echo "Error: verified gather waiver count missing" >&2; exit 1;; esac
LEDGER_UNCONFIRMED=$(grep -Ec '^(REMOVED|NET-STATE-ADD):' "$WAIVED_FILE" || true)
[ "$LEDGER_UNCONFIRMED" -eq "$UNCONFIRMED" ] || { echo "Error: waiver ledger changed after gather validation" >&2; exit 1; }
{
  echo "# Waived changes — $VERSION"
  echo
  echo "Changes classified during release prep against ${LAST_TAG:-no prior tag} that did not survive into this release — never shipped under the claimed name, reverted before shipping, or superseded across append cycles. Audit trail only; not part of the public changelog."
  echo
  if [ -s "$WAIVED_FILE" ]; then
    sed 's/^/- /' "$WAIVED_FILE"
  else
    echo "No changes were waived in this release."
  fi
} > "$OUT"  # timeout: 5000
echo "→ wrote $OUT"
```

### Output

```markdown
## Release prepare: $VERSION

### Audit
Reproduce the full Phase-1 readiness table verbatim — the `| Check | Status | Detail |` markdown table from audit-checks.md with ALL check rows (Working tree, CI, Blocking issues, Open PRs, README aligned, CHANGELOG entry, Version consistent, Dependency CVEs, Scheduled removals, Doc proportionality) and their Status glyphs (`✓`/`⚠`/`✗`). "Condensed" applies to the Detail column only (trim verbose detail) — never to row count. Do NOT replace this table with a finding-bullet digest, and do NOT substitute a different table (e.g. a `File | Status` artifacts box).
[any warnings carried forward]

### Written (documentation artifacts — complementary to the release, not the release itself)
Render as the markdown bullet list below — NOT a box-drawing (`┌─┬─┐`) `File | Status` table.
- `$CHANGELOG_FILE` — $VERSION entry stamped (Phase 2b); `releases/$VERSION/CHANGELOG.md` symlinks here
- `releases/$VERSION/HIGHLIGHTS.md` — top 3–5 spotlights with code examples (Phase 3a)
- `releases/$VERSION/MIGRATION.md` — migration guide (N breaking changes, or "No breaking changes") (Phase 3b)
- `releases/$VERSION/demo.py` — story-telling jupytext notebook (Phase 4a; omit if demo excluded)
- `releases/$VERSION/SUMMARY.md` — internal executive summary (Phase 4b)
- `releases/$VERSION/DRAFT.md` — user-facing release notes, final assembly (Phase 5)
- `releases/$VERSION/waived-changes.md` — Breaking/Removed/rename claims excluded against `$LAST_TAG` (N waived, or "No changes were waived") (Phase 6)

### Next steps
1. Review all written files
2. Bump version in the project manifest
3. Commit, push, open PR
4. On merge: publish the release — `gh release create $VERSION --notes-file releases/$VERSION/DRAFT.md` (user-run; DRAFT.md is source for release notes, not the release itself)
5. Upload package to PyPI (or relevant registry) — separate step after GitHub release
6. Convert demo: `jupytext --to notebook releases/$VERSION/demo.py` (skip if demo excluded)
```

End terminal response (not written artifacts) with `## Confidence` block per CLAUDE.md output standards: `**Score**: 0.0–1.0 — [label]`; omit Refinements if 0 passes.
