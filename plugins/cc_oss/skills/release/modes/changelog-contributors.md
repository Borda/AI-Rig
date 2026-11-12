<!-- file: changelog-contributors.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:release -->

## Audit changelog

**`prepare`/`audit` modes**: delegated in parallel (see Delegation strategy). Reload paths:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CHANGELOG_AUDIT_FILE < "${TMPDIR:-/tmp}/release-changelog-audit-${CSID}" 2>/dev/null || CHANGELOG_AUDIT_FILE=""
IFS= read -r CHANGELOG_FILE < "${TMPDIR:-/tmp}/release-changelog-file-${CSID}" 2>/dev/null || CHANGELOG_FILE=""
```

Read `$CHANGELOG_AUDIT_FILE` for audit findings; report added/flagged/unsourced counts from delegation envelope. If file missing (delegation skipped), fall back to inline below.

**`notes` mode or delegation fallback**:

`bin/resolve_changelog_file.py` (called in the fence below) resolves `$CHANGELOG_FILE`: `CHANGELOG.md` at repo root, `docs/CHANGELOG.md`, any `CHANGELOG*` one level deep (excluding `node_modules/`, `.venv/`, `vendor/`); none found → `CHANGELOG.md`. `prepare` and `audit` use the same resolver.

Before the first changelog edit in every `notes` run, stage the complete artifact set, including plain notes and the invalid-marker append fallback. If no changelog exists, select `CHANGELOG.md` as its destination. This begin step copies the live draft, changelog, selected dated plain summary/migration outputs, existing standalone summary/migration files, provenance, and marker; it does not publish any bytes. A pending promotion is recovered during setup before a new candidate begins. Keep `$CHANGELOG_FILE` as the live path for reporting and provenance, and use `$EDIT_CHANGELOG_FILE` for every changelog Read/Edit/Write from this point through the final truth gate:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE=""
IFS= read -r DO_SUMMARY < "${TMPDIR:-/tmp}/release-do-summary-${CSID}" 2>/dev/null || DO_SUMMARY="false"
IFS= read -r DO_MIGRATION < "${TMPDIR:-/tmp}/release-do-migration-${CSID}" 2>/dev/null || DO_MIGRATION="false"
IFS= read -r BRANCH_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_REF" 2>/dev/null || BRANCH_REF=""
IFS= read -r BRANCH_KEY < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_KEY" 2>/dev/null || BRANCH_KEY=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r FROZEN_HEAD < "${TMPDIR:-/tmp}/release-append-head-${CSID}" 2>/dev/null || FROZEN_HEAD=""
IFS= read -r FROZEN_START < "${TMPDIR:-/tmp}/release-append-start-${CSID}" 2>/dev/null || FROZEN_START=""
IFS= read -r FROZEN_END < "${TMPDIR:-/tmp}/release-append-end-${CSID}" 2>/dev/null || FROZEN_END=""
FROZEN_END="${FROZEN_END:-$FROZEN_HEAD}"
IFS= read -r FROZEN_BRANCH < "${TMPDIR:-/tmp}/release-append-branch-${CSID}" 2>/dev/null || FROZEN_BRANCH=""
FROZEN_TAGS_FILE="${TMPDIR:-/tmp}/release-append-tags-${CSID}"
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
[ -n "${CHANGELOG_FILE:-}" ] || CHANGELOG_FILE=$(python "$PLUGIN_ROOT/bin/resolve_changelog_file.py" --repo-root "${REPO_ROOT:-.}") || { echo "Error: changelog path unresolved" >&2; exit 1; }  # timeout: 5000
printf '%s\n' "$CHANGELOG_FILE" > "${TMPDIR:-/tmp}/release-changelog-file-${CSID}"
EDIT_CHANGELOG_FILE="$CHANGELOG_FILE"
: > "${TMPDIR:-/tmp}/release-append-stage-${CSID}"
if [ "$RELEASE_MODE" = notes ]; then
    [ -n "$FROZEN_HEAD" ] && [ -n "$FROZEN_BRANCH" ] && [ -f "$FROZEN_TAGS_FILE" ] || { echo "Error: frozen append source missing" >&2; exit 1; }
    python "$PLUGIN_ROOT/bin/release_append_publish.py" check --branch "$BRANCH" --head-sha "$FROZEN_HEAD" --branch-ref "$FROZEN_BRANCH" --tag-state-file "$FROZEN_TAGS_FILE" || exit 1
fi
if [ "$RELEASE_MODE" = notes ]; then
    [ -n "$FROZEN_START" ] || { echo "Error: frozen append interval missing" >&2; exit 1; }
    START_ARG=()
    if [ "$(python "$PLUGIN_ROOT/bin/release_append_marker.py" is-valid --branch "$FROZEN_BRANCH" --last-tag "$LAST_TAG")" = true ]; then
        IFS= read -r SAVED_MARKER < ".temp/release-state-v2/$BRANCH_KEY/marker" || exit 1
        START_ARG=(--start-sha "$SAVED_MARKER")
    fi
    EXTRA_ARGS=()
    if [ "$DO_APPEND" != true ]; then
        [ "$DO_SUMMARY" != true ] || EXTRA_ARGS+=(--output ".temp/output-release-summary-$BRANCH-$DATE.md")
        [ "$DO_MIGRATION" != true ] || EXTRA_ARGS+=(--output ".temp/output-release-migration-$BRANCH-$DATE.md")
    fi
    APPEND_STAGE=$(python "$PLUGIN_ROOT/bin/release_append_publish.py" begin --branch "$BRANCH" --changelog "$CHANGELOG_FILE" --head-sha "$FROZEN_HEAD" --marker-sha "$FROZEN_END" --range-start "$FROZEN_START" --last-tag "$LAST_TAG" "${START_ARG[@]}" "${EXTRA_ARGS[@]}" --branch-ref "$FROZEN_BRANCH" --tag-state-file "$FROZEN_TAGS_FILE") || exit 1
    printf '%s\n' "$APPEND_STAGE" > "${TMPDIR:-/tmp}/release-append-stage-${CSID}"
    EDIT_CHANGELOG_FILE="$APPEND_STAGE/$CHANGELOG_FILE"
fi
```

Fresh shells must reload `$APPEND_STAGE` from `release-append-stage-${CSID}` and derive `$EDIT_CHANGELOG_FILE="$APPEND_STAGE/$CHANGELOG_FILE"` before any notes changelog Read/Edit/Write; a missing candidate is a hard stop. The live `$CHANGELOG_FILE` is read only for earlier cross-cycle detection and is not changed by this audit.

If `$EDIT_CHANGELOG_FILE` exists: cross-check its target section — the declared Unreleased section; none declared and the top section a released version (tagged, or named by `$LAST_TAG`) → create `## [Unreleased]` above it; never append to the top released section or any older one, whatever the surrounding text. Items absent → add (same emoji format) to `$EDIT_CHANGELOG_FILE`. Items in CHANGELOG not matching classified → flag for review (no auto-delete). For each REVERT_SET pair: add `🔄 Reverted: <original change description> (introduced and reverted in this release)`. Preserve historical entries; remove only the exact matching Unreleased entry for an original change that has not shipped, when that stale claim is confirmed. Reverted items never in highlights or migration guide.

**Mandatory evidence link** (`modes/classify-truth-check.md` "Mandatory evidence link"): every entry added carries its source in the artifact's own form — CHANGELOG `([#N](url))` per `guidelines/writing-rules.md`, or `(sha7)` when no PR exists — never bare. No PR/sha available for a classified item → flag it `high`, "no PR/commit evidence: <description>", instead of writing it unsourced. Existing Unreleased bullet with no PR number and no sha in any form (`(#N)`, `([#N](url))`, `(sha7)`) → flag `medium`, "unsourced entry" (no auto-backfill, no auto-delete).

For each `CROSS_CYCLE_MATCH` targeting `$CHANGELOG_FILE` (from Gather changes' cross-cycle detection — original predates `$RANGE`, matched text found in Unreleased from a prior `--append` cycle): strike/remove the matched entry the same way — do not add a redundant `🔄 Reverted` bullet for something the reader never saw shipped in this visible cycle.

If missing: create `$EDIT_CHANGELOG_FILE` (inside the candidate for notes, or the selected path for a delegation fallback); populate with `# Changelog` header and `## [Unreleased]` from Classify.

After resolving or creating `$CHANGELOG_FILE`, persist its exact path for fresh-shell append validation:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -n "$CHANGELOG_FILE" ] || IFS= read -r CHANGELOG_FILE < "${TMPDIR:-/tmp}/release-changelog-file-${CSID}" 2>/dev/null || CHANGELOG_FILE=""
IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE=""
IFS= read -r APPEND_STAGE < "${TMPDIR:-/tmp}/release-append-stage-${CSID}" 2>/dev/null || APPEND_STAGE=""
EDIT_CHANGELOG_FILE="$CHANGELOG_FILE"
if [ "$RELEASE_MODE" = notes ]; then
    [ -n "$APPEND_STAGE" ] && [ -d "$APPEND_STAGE" ] || { echo "Error: changelog candidate missing" >&2; exit 1; }
    EDIT_CHANGELOG_FILE="$APPEND_STAGE/$CHANGELOG_FILE"
fi
[ -n "$CHANGELOG_FILE" ] && [ -f "$EDIT_CHANGELOG_FILE" ] || { echo "Error: changelog path unresolved" >&2; exit 1; }
printf '%s\n' "$CHANGELOG_FILE" > "${TMPDIR:-/tmp}/release-changelog-file-${CSID}"
```

**Scope check** (same rule as delegated Agent A — see `modes/changelog-audit-prompt.md`): `git log --merges --pretty='%H %P %s' --end-of-options "$RANGE"`, keep rows whose subject doesn't match `merge pull request #[0-9]+|\(#[0-9]+\)` — a raw branch merge landed inside `$RANGE`, not a real PR merge. Diff each such commit's two parents to list what it pulled in; any pulled-in commit already shipped in a prior CHANGELOG section is `critical` — it's about to appear a second time in the wrong release under this PR by accident. A pulled-in commit with no prior CHANGELOG entry is `medium`, "unreviewed branch merge" — confirm it belongs to this release before merging the PR. Report as its own line, never folded into the add/flag count silently.

Always report: "N items added, M flagged for review, K scope-flagged (non-PR branch merge), U unsourced (no PR/commit evidence)." This phase owns CHANGELOG-format classification; Write release draft reads from it — does NOT copy. DRAFT.md uses different format.

## Extract contributors

**`prepare`/`audit` modes**: delegated in parallel (see Delegation strategy). Reload path:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CONTRIBUTORS_FILE < "${TMPDIR:-/tmp}/release-contributors-${CSID}" 2>/dev/null || CONTRIBUTORS_FILE=""
```

Read `$CONTRIBUTORS_FILE` for formatted contributors list. If file missing (delegation skipped), fall back to inline below.

**`notes` mode or delegation fallback**:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload $RANGE (Check 41)
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
python "$PLUGIN_ROOT/bin/extract_contributors.py" --range "$RANGE" --exclude-file "${TMPDIR:-/tmp}/release-shipped-${CSID}" --include-bots  # timeout: 5000
```

`extract_contributors.py --include-bots` emits one `Name <email>` line per contributor, already deduplicated by email. Keep GitHub privacy-email humans; separate bot identities into the single automated-contributions line required by `guidelines/writing-rules.md`. Every commit counts, including docs and typo fixes.

Build the contributor inventory as the union of Git authors/coauthors and every PR author verified to belong to this release range. Use Gather changes' commit-to-PR associations, not the entire merged-PR list or merge dates alone. Include PR-only authors even when a maintainer authored the squash commit and no coauthor trailer exists. Preserve the PR-to-commit evidence in the gathered change table passed to the delegated path. If association or PR-author coverage is unavailable, flag incomplete contributor coverage; never claim completeness from Git alone.

Reconcile duplicates only through verified login/email linkage, including GitHub privacy-email handles; display-name similarity alone is not identity proof. Keep unmatched Git people and PR-only people; use the known login when a PR author's display name is unavailable, never invent an email or name. Deleted/unavailable PR identities remain an explicit coverage gap. Aggregate bot identities before individual formatting or profile lookup; never send bots through those human-only steps.

For each human contributor, inspect commits in range (`git log --no-merges --author="<email>" --oneline --end-of-options "$RANGE"`) when their email is known; for PR-only authors, use their verified in-range PR changes instead. Pick up to 3 most significant contributions. Rank: new public API > major UX improvement > significant fix > internal change > docs/typo. No PR numbers, no issue links, no `(#N)` references in public credit prose.

Resolve GitHub handles from the verified PR author data (`author.login`) or confirmed Git identity linkage. If unresolved, omit the handle.

For each resolved human handle, find a LinkedIn link via this ordered chain — stop at first hit, never guess/infer/search-by-name at any step. Contributor **name is never a matching key anywhere in this chain** — only the resolved GitHub handle (steps 1, 2, 4 below) or an anchor href read directly from fetched page content (step 3):

1. **Primary — Social Accounts API** (strongest signal: explicitly added by the person to their own GitHub profile):

   ```bash
   gh api "/users/<login>/social_accounts" --jq '.[] | select(.provider=="linkedin") | .url' 2>/dev/null  # timeout: 6000
   ```

   Non-empty output → use directly, done.

2. **Fallback A — `.blog` field**:

   ```bash
   gh api /users/<login> --jq '{blog: .blog, twitter: .twitter_username}' 2>/dev/null  # timeout: 6000
   ```

   `.blog` contains `linkedin.com` → use directly, done.

3. **Fallback B — personal-page exception**: `.blog` is a non-empty URL, NOT a `linkedin.com` URL → `WebFetch` that page, scan its actual returned content for `linkedin.com/in/...` anchor links (real hrefs read from the page, never inferred from surrounding text). Exactly one distinct such link found → use it, done. Zero or multiple distinct links found → do not guess; omit LinkedIn for this contributor.

4. **Fallback C — past releases**: search for a Contributors entry, keyed by this EXACT GitHub handle, that a prior release already resolved and credited with a `[LinkedIn](...)` link:

   ```bash
   grep -rn "@<login>" CHANGELOG.md docs/CHANGELOG.md releases/*/SUMMARY.md releases/*/DRAFT.md 2>/dev/null | grep -m1 '\[LinkedIn\]('  # timeout: 5000
   # only when the above finds nothing — scan published release bodies (stop at first match):
   for tag in $(gh release list --limit 100 --json tagName --jq '.[].tagName' 2>/dev/null); do  # timeout: 15000
       gh release view "$tag" --json body --jq '.body' 2>/dev/null | grep -m1 "@<login>.*\[LinkedIn\]("  # timeout: 6000
   done | head -1
   ```

   Found → reuse that URL verbatim, done. Not found → omit.

5. No step produced a link → omit LinkedIn, same as today's no-match behavior.

Canonical Contributors format (the template's Contributors line points here): `- **Name** (@github_handle, [LinkedIn](https://linkedin.com/in/handle)) — <brief what they did>`. Omit `@handle` when unresolvable. `<brief what they did>` picks up to 3 most significant contributions per the Extract contributors ranking and describes the contribution itself (feature/fix/area) — never a PR number, issue link, or `(#N)` reference.
