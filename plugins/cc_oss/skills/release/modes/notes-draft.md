<!-- file: notes-draft.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:release -->

## Generate release demo

**Only for feature releases** (≥1 🚀 Added items). Skip bug-fix-only releases.

Self-contained Python script in jupytext percent (`# %%`) format. Full story: install → setup → demonstrate each highlight → verify output.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload Shared-setup vars (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
DEMO_OUT=".temp/release-demo-$BRANCH-$DATE.py"
mkdir -p .temp  # timeout: 5000
echo "${DEMO_OUT:-}" > "${TMPDIR:-/tmp}/release-demo-out-${CSID}"
```

Write demo to `$DEMO_OUT`. (`prepare` mode: `releases/$VERSION/demo.py` — see Phase 4.)

**Gate: demo must execute to completion before proceeding to Draft executive summary.**

<!-- branch: demo-approval — only in demo/prepare mode; isolated from notes/changelog path; ≤1 call -->

Invoke `AskUserQuestion` — "Ready to run demo script `$DEMO_OUT`?" Options: (a) Run now · (b) Review first · (c) Skip and **exclude from release artifacts**.

On option (c): mark demo excluded, skip to Draft executive summary — do NOT invoke the failure-path AskUserQuestion below.

On (a) or (b) confirmed:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r DEMO_OUT < "${TMPDIR:-/tmp}/release-demo-out-${CSID}" 2>/dev/null || DEMO_OUT=""
python "$DEMO_OUT"  # timeout: 600000
DEMO_EXIT=$?
echo "${DEMO_EXIT}" > "${TMPDIR:-/tmp}/release-demo-exit-${CSID}"
```

<!-- policy-sibling: plugins/cc_oss/skills/release/modes/prepare.md §Phase 4a execution gate (demo retry bound) -->

**Guard**: only proceed to failure handling when `$DEMO_EXIT -ne 0` after attempting a fix. Success (`$DEMO_EXIT = 0`) → proceed directly to Draft executive summary — no AskUserQuestion. Failure → fix and re-run (max 3 iterations total). Only after 3 failed attempts invoke `AskUserQuestion` ("Demo still failing after 3 attempts. Exclude from release and continue, or abort?"). Self-contained: package installed in current env; no live API calls or network deps; deterministic synthetic data; `# !pip install` lines are Python comments — interpreter skips.

## Draft executive summary

1–2 paragraph executive summary: what release is, why it matters, who benefits. Based on Identify highlights. Save `.temp/output-release-summary-$BRANCH-$DATE.md`. This text stays internal (standalone `SUMMARY.md`); it is only the fact source for DRAFT.md's `## 📋 Summary`, which Write release draft re-renders into the pitch shape in `templates/release-draft.md` — never paste these paragraphs there.

## Write release draft

Pre-flight — verify all templates present before proceeding:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload Shared-setup vars (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
[ -z "$SKILL_DIR" ] && echo "Error: could not locate release skill directory" && exit 1
for tmpl in release-draft.md audit-checks.md gather-prompt.md; do # timeout: 5000
    [ -f "$SKILL_DIR/templates/$tmpl" ] || {
        echo "Missing template: $tmpl — aborting"
        exit 1
    }
done
```

Before writing, fetch last 2–3 releases to check formatting conventions:

```bash
gh release list --limit 5                                                  # timeout: 30000
LATEST_TAG=$(gh release list --limit 100 --json tagName --jq '[.[] | select(.tagName | test("rc|dev|alpha|beta"; "i") | not)] | .[0].tagName // empty') # timeout: 30000
[ -z "$LATEST_TAG" ] || [ "$LATEST_TAG" = "null" ] && echo "No releases found — using template defaults" || gh release view "$LATEST_TAG"  # timeout: 15000
```

Existing releases deviate from templates → match tone and prose style only. **Never** use `# Changelog` structure for DRAFT.md — always use `release-draft.md` structure. `gh release list` empty → use template defaults.

Fetch origin URL for full changelog link:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
ORIGIN_URL=$(git remote get-url origin 2>/dev/null || echo "")  # timeout: 3000
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/templates/release-draft.md"  # timeout: 5000
```

**DRAFT.md format guard**: must NOT start with `# Changelog`, must NOT use CHANGELOG section structure. CHANGELOG-format classification = internal working doc only — derive sections from it, don't copy verbatim.

Template (loaded above). Fill `[Full changelog URL]` as `<https form of $ORIGIN_URL>/compare/<LAST_TAG>...<new tag>` (no previous tag → link the release tag's commit history). Omit empty sections.

Key difference from `prepare`: `notes` runs these phases in the main context, spawning subagents only where a step says so (over-50-commit gather under the Size guard, Adversarial review, shepherd voice review), and publishes `DRAFT.md` and the resolved `$CHANGELOG_FILE` from the staged candidate (`modes/release-draft-template.md`); `prepare` writes `releases/$VERSION/DRAFT.md` (`modes/prepare.md` Phase 5).

### Adversarial review

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/adversarial-review.md"  # timeout: 5000
```

Follow above and execute.

<!-- loads: modes/release-draft-template.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$SKILL_DIR/modes/release-draft-template.md"  # timeout: 5000
```

Follow above and execute (format templates, semantic consistency review, polish, shepherd spawn, write to disk).
