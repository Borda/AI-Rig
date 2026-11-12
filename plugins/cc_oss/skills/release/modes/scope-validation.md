<!-- file: scope-validation.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:release -->

## Explore codebase

For top 3–5 significant changes (features, breaking, major behavior), read actual diff or changed files:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
git diff --end-of-options "$RANGE" -- <file>    # timeout: 3000
git show --end-of-options <commit>:<file>       # timeout: 3000
```

Goal: understand new APIs, parameters, behavior — notes describe real functionality, not just commit subjects. Skip trivial changes (typos, dep bumps, CI config).

## Validate docs

Check public API surface in docs/ (or README) matches diff. Flag public symbol added/renamed/removed in Gather changes but absent from docs. Report: `- [MISSING/STALE] <symbol> in <doc-file>`. Empty list = docs aligned.

**Doc weight check** — for each 🚀 Added change identifying significant new entity (new public skill, new command, new agent, new submodule, new mode): compute **doc weight** for that feature and 2–3 comparable existing features of same nature in relevant README or docs file.

Doc weight = `header_score + coverage_score + example_score`:

- `header_score`: H2 = 3, H3 = 2, H4/deeper = 1, no heading = 0
- `coverage_score`: `min(non_blank_lines_in_section / 5, 5)` — lines from feature heading to next same-or-higher heading
- `example_score`: fenced code blocks in section, capped at 3

Weight ratio = `new_feature_weight / mean(comparable_weights)`. Flag UNDERTREATED when ratio < 0.5. Report: `- [UNDERTREATED] <feature> in <doc-file> — weight N vs peers M1/M2 (ratio R)`. Collect as `doc_proportionality` list in findings.

## Classify each change

<!-- loads: modes/classify-truth-check.md (single load — Classify/Truth check/Breaking-change classification below run sequentially in this same path, no branch skips an earlier phase while running a later one; file content stays in context for the two "Follow above" refs below) -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$SKILL_DIR/modes/classify-truth-check.md"  # timeout: 5000
```

Follow above and execute. Contains: category table, PR accumulation rules, dedup rules, OMIT-INTERNAL body-signal override, cherry-pick annotation.

## Truth check

Follow `modes/classify-truth-check.md` (Truth check section, loaded above) and execute. Gate: runs after Classify, before Audit changelog. Verify 🚀 Added / 🌱 Changed final names at HEAD; verify ❌ Removed old names exist at `$LAST_TAG` and are absent at HEAD; check ⚠️ Breaking Changes against the baseline before relabeling. Max 3 loop iterations.

Inline runs must derive and persist waiver counts from this invocation's ledger immediately after Truth check:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
[ -n "$WAIVED_FILE" ] && [ -f "$WAIVED_FILE" ] || { echo "Error: waiver ledger missing after Truth check" >&2; exit 1; }
UNCONFIRMED=$(grep -Ec '^(REMOVED|NET-STATE-ADD):' "$WAIVED_FILE" || true)
UNCONFIRMED_BREAKING=$(grep -Ec '^(REMOVED|NET-STATE-ADD): ⚠️ Breaking Changes:' "$WAIVED_FILE" || true)
printf '%s\n' "$UNCONFIRMED" > "${TMPDIR:-/tmp}/release-unconfirmed-${CSID}"
printf '%s\n' "$UNCONFIRMED_BREAKING" > "${TMPDIR:-/tmp}/release-unconfirmed-breaking-${CSID}"
printf 'unconfirmed=%s unconfirmed_breaking=%s\n' "$UNCONFIRMED" "$UNCONFIRMED_BREAKING"
```

Apply the Delegation strategy's breaking-waiver item-approval gate using these counts before proceeding. Fresh shells reload `$UNCONFIRMED_BREAKING` from `release-unconfirmed-breaking-${CSID}`; no artifact may be written before every required approval completes.

## Breaking-change classification

Follow `modes/classify-truth-check.md` (Breaking-change classification section, loaded above) and execute. Codemap-gated (skips without a v3 index). For each diff-derived public symbol, `fn-rdeps --exclude-tests` labels it Breaking (caller outside its own package) or internal; Breaking symbols move to ⚠️ Breaking Changes with caller evidence, and `migration_lines` feed the Draft migration guide as `breaking_callers` findings.

Apply the **post-promotion baseline and item-approval gate** in that section after codemap output and before Audit changelog or any artifact write. Earlier Truth check and the delegated gather envelope cannot approve a later Added/Changed → Breaking promotion.

## Validate migration docs

Gate — runs after Truth check. Only when project has migration docs page.

**Detect** — migration doc OR any alternative describing API changes between versions:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload REPO_ROOT (Check 41)
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
MIGRATION_DOC=$(find "$REPO_ROOT" -maxdepth 3 \( \
  -iname "MIGRATION*" -o -iname "UPGRADING*" -o \
  -iname "migration.md" -o -iname "upgrading.md" -o \
  -iname "CHANGELOG*" -o -iname "BREAKING*" -o \
  -iname "api-changes*" -o -iname "release-notes*" \
\) -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/.git/*" \
| head -1)  # timeout: 5000
[ -z "$MIGRATION_DOC" ] && MIGRATION_DOC=$(find "$REPO_ROOT/docs" -maxdepth 2 \( \
  -iname "migration*" -o -iname "upgrade*" -o -iname "breaking*" -o -iname "api-changes*" \
\) 2>/dev/null | head -1)  # timeout: 5000
printf '%s\n' "$MIGRATION_DOC" > "${TMPDIR:-/tmp}/release-migration-doc-${CSID}"
```

Skip entirely when `$MIGRATION_DOC` empty — no migration/upgrade docs exist in project.

**When found**: for every classified item in ⚠️ Breaking Changes, 🗑️ Deprecated, and ❌ Removed — verify present and described in `$MIGRATION_DOC`. "Present" = migration doc contains symbol name or semantically equivalent reference with upgrade instructions.

Check each item:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r MIGRATION_DOC < "${TMPDIR:-/tmp}/release-migration-doc-${CSID}" 2>/dev/null || MIGRATION_DOC=""
grep -i "<symbol_or_key>" "$MIGRATION_DOC" 2>/dev/null  # timeout: 3000
```

**Outcomes**:

- Found with upgrade path → `✓ <symbol> covered`
- Found but no upgrade path → `[SHALLOW] <symbol> in <doc> — present but missing upgrade instructions`
- Not found → `[MISSING-MIGRATION] <symbol> — ⚠️ Breaking/🗑️ Deprecated but absent from <doc>`

Collect all findings as `migration_gaps` list. Zero findings → migration doc complete. Report them in the run's final reply, not before the next tool call (text written before a tool call can arrive as an empty progress update).

**Do not block** on `[SHALLOW]` findings — flag and continue. `[MISSING-MIGRATION]` findings surface as warnings; Draft migration guide phase must fill the gaps.
