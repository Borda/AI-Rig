<!-- oss:release Mode: demo — executed via: cat "$SKILL_DIR/modes/demo.md"; execute -->

<!-- Variables available: $SKILL_DIR, $_OSS_SHARED, $LAST_TAG, $BRANCH, $DATE, $RANGE, $VERSION, $REPO_ROOT, $GATHER_FILE -->

**Trigger**: `/release demo [range]`

**Purpose**: Story-telling release notebook — self-contained Python script in jupytext percent (`# %%`) format. Highlights 2–3 most significant contributions with narrative prose, runnable code cells. Suitable for Colab, local Jupyter, or blog embeds.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload vars from Shared setup — fresh shell (Check 41)
IFS= read -r REST < "${TMPDIR:-/tmp}/release-rest-${CSID}" 2>/dev/null || REST=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
RANGE="${REST:+${REST/->/../}}"
RANGE="${RANGE:-$LAST_TAG..HEAD}"
echo "$RANGE" > "${TMPDIR:-/tmp}/release-demo-range-${CSID}"  # persist for later blocks (Check 41)
```

### Phase 1: Gather and pick headline features

Run gather/explore/validate inline for `$RANGE` (no delegation — demo single-pass like `notes` mode). Use same commands as **Gather changes** section above (git log, gh pr list). For diff stat, prefer three-dot range:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-demo-range-${CSID}" 2>/dev/null || RANGE=""  # reload (Check 41)
git diff --stat "$(echo "$RANGE" | sed 's/\.\./.../')"  # three-dot range preferred; timeout: 3000
```

From gathered commits and diffs, select 2–3 headline features:

- Prefer: new public API, breaking changes, significant performance wins, major UX improvements
- Exclude: internal refactors, CI/tooling, dep bumps, doc-only changes

For each headline feature, read actual diff or changed source file to understand before/after interface — demo cells must show real API, not paraphrase.

### Phase 2: Generate demo script

**Real-world data constraint**: demo must use actual project artifacts, real API calls against package under release, or genuine example data already in repo. Fabricated/synthetic inputs not acceptable by default. Sources in priority order: repo test fixtures, example scripts shipped with package, public datasets referenced in project docs, real CLI invocations against installed package.

**Fallback protocol — if real demo cannot be assembled** (no usable fixtures, installed package not functional, API requires live credentials): before writing any synthetic script, execute these steps in order:

1. **Document each failed attempt**: output `## Demo attempts` block to terminal listing every approach tried and specific reason rejected (e.g. "test fixtures require database connection", "example script imports non-installable C extension"). Minimum one entry per attempt.
2. **Ask Codex (if available)**:
   ```bash
   CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")  # timeout: 5000
   [ "$CODEX_STATUS" = "available" ] && CODEX_OK="available" || CODEX_OK=""
   ```
   If `$CODEX_OK` non-empty, call `Skill(skill="bridge:advise", args="Read the complete ## Demo attempts log printed above and inspect <REPO_ROOT>. Locate a real-world demo using existing project artifacts, tests, examples, or documented APIs. Return one viable approach with exact source paths and commands; do not modify files.")`. If Codex returns a viable approach, use it — stop, skip steps 3–4.
3. **Ask user**: invoke `AskUserQuestion` with `## Demo attempts` log (and Codex outcome if attempted), asking user to either provide real-world assets or explicitly approve synthetic demo.
4. **Synthetic demo only on explicit approval**: proceed with synthetic/fabricated demo content only if step 3 `AskUserQuestion` response explicitly authorises it.

Write Python script in jupytext percent format, following the repo-wide notebook standard:

```bash
cat "$_OSS_SHARED/notebook-style.md"  # timeout: 5000
```

Structure in order:

1. **Jupytext header** — prepend verbatim as first block of generated script:

   ```python
   # ---
   # jupyter:
   #   jupytext:
   #     cell_metadata_filter: -all
   #     formats: ipynb,py:percent
   #     text_representation:
   #       extension: .py
   #       format_name: percent
   #       format_version: '1.3'
   #       jupytext_version: 1.16.0
   # ---
   ```

2. **Title cell** (`# %% [markdown]`):

   - `# <PackageName> <VERSION>: <tagline — one clause per headline feature>`
   - Colab badge placeholder: `[![Open In Colab](...)](<repo-url>/blob/main/releases/<VERSION>/demo.ipynb)`
   - `**What you'll learn:**` — bullet per headline feature
   - `**Sections:**` — numbered TOC with anchor links
   - 2–3 narrative paragraphs: what release adds, why it matters; `> **Breaking change:**` blockquote if breaking changes present

3. **Install cell** (`# %%`): `# !pip install <package>==<VERSION>`

4. **Config cell** (`# %%`): all notebook-level constants (`OUTPUT_DIR`, `BATCH_SIZE`, etc.); `num_workers` pattern for macOS/Windows safety if training involved

5. **One section per headline feature** — for each:

   - Markdown cell: `## N. <Feature name>` + prose (before/after, motivation, API shape)
   - Code cell(s): demonstrate feature; if showing old→new migration, old API in commented block above
   - Verification cell where output confirms feature works (e.g. print, assertion, plot)

6. **Next steps cell** (`# %% [markdown]`):

   - `## <N+1>. Next steps` header
   - Bullet list: docs link, changelog link (GitHub compare URL), migration guide link if breaking changes, links to prior release demos; use `<placeholder-url>` format — never invent real URLs

Demo-only content rules (on top of `notebook-style.md` above — its compact-docstring and narrative-depth anchors still apply):

- All code must be syntactically valid Python
- Placeholder URLs use `<repo-url>`, `<docs-url>` — never invent real URLs
- Narrative cells explain WHY, not just what — write for developer who hasn't seen release
- Breaking changes get both `> **Breaking change:**` callout in title cell AND comparison cell in relevant section

### Phase 3: Write output

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload vars from Shared setup — fresh shell (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
# LAST_TAG = previous release (range lower bound) — not release being drafted
# always .temp/; prepare mode uses releases/$VERSION/
DEMO_OUT=".temp/release-demo-$BRANCH-$DATE.py"
mkdir -p .temp  # timeout: 5000
```

Write generated script to `$DEMO_OUT` using Write tool.

Notify: `→ written to $DEMO_OUT`

> Convert `.py` → `.ipynb` with `jupytext --to notebook $DEMO_OUT` — user runs this; skill does not execute it.
