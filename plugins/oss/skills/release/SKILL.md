---
name: release
description: 'Prepare release communication and check release readiness. Modes — notes (writes PUBLIC-NOTES.md), changelog (prepends CHANGELOG.md), summary (internal brief), migration (breaking-changes guide), prepare (full pipeline: audit → notes + changelog + summary + migration if breaking changes), audit (pre-release readiness check: blockers, docs alignment, version consistency, Common Vulnerabilities and Exposures (CVEs)). Use whenever the user says "prepare release", "write changelog", "what changed since v1.x", "prepare v2.0", "write release notes", "am I ready to release", "check release readiness", or wants to announce a version to users.'
argument-hint: <mode> [range] | migration <from> <to> | prepare <version> | audit [version]
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, TaskCreate, TaskUpdate, Agent
model: opus
---

<objective>

Prepare release communication from what changed. Output adapts to audience — user-facing notes, CHANGELOG entry, internal summary, or migration guide.

</objective>

<inputs>

Mode comes **first**; range or version follows:

| Invocation | Arguments | Writes to disk |
| --- | --- | --- |
| `/release notes [range]` | optional git range (default: last-tag..HEAD) | `PUBLIC-NOTES.md` |
| `/release changelog [range]` | optional git range | Prepends `CHANGELOG.md` |
| `/release summary [range]` | optional git range | `.temp/output-release-summary-<branch>-<date>.md` |
| `/release migration <from> <to>` | two version tags, e.g. `v1.2 v2.0` | Terminal only |
| `/release prepare <version>` | version to stamp, e.g. `v1.3.0` | All artifacts: audit → `PUBLIC-NOTES.md` + `CHANGELOG.md` + summary + migration if breaking |
| `/release audit [version]` | optional target version | Terminal readiness report |

No mode given → defaults to `notes`. `prepare` = full pipeline — runs audit first, then all artifacts; use when cutting release, not drafting.

</inputs>

<workflow>

**Task hygiene**: Call `TaskList` before creating tasks. Per found task:

- `completed` if work clearly done
- `deleted` if orphaned / irrelevant
- `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, TaskCreate for each major phase. Mark in_progress/completed throughout. On retry or scope change, new task.

## Mode Detection

Parse `$ARGUMENTS` by first token:

```bash
read FIRST REST <<<"$ARGUMENTS"
```

| First token | Mode | Routing |
| --- | --- | --- |
| `prepare` | prepare | Skip to **Mode: prepare** |
| `audit` | audit | Skip to **Mode: audit** |
| `migration` | migration | `read MIGRATION_FROM MIGRATION_TO <<< "$REST"`, set `RANGE="$MIGRATION_FROM..$MIGRATION_TO"`, continue Steps 1–5 with migration format |
| `notes`, `changelog`, `summary` | as named | Set `RANGE="$REST"` (empty = default); continue Steps 1–5 |
| *(none or bare range)* | notes | Set `RANGE="$ARGUMENTS"`; continue Steps 1–5 |

## Step 1: Gather changes

```bash
# Use $RANGE from Mode Detection, or fall back to last-tag..HEAD
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
RANGE="${RANGE:-$LAST_TAG..HEAD}"

# One-liner overview (navigation index)
git log $RANGE --oneline --no-merges # timeout: 3000

# Full commit messages — read these to catch BREAKING CHANGE footers,
# co-authors, and details omitted from the subject line
git log $RANGE --no-merges --format="--- %H%n%B" # timeout: 3000

# File-level diff stat — confirms what areas actually changed
git diff --stat $(echo "$RANGE" | sed 's/\.\./\ /') # timeout: 3000

# PR titles, bodies, and labels for merged PRs (richer context than commits)
TRUNK=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | { read -r _ _ val; echo "${val:-main}"; })
# timeout: 15000
gh pr list --state merged --base "${TRUNK:-main}" --limit 100 \
    --json number,title,body,labels,mergedAt,author 2>/dev/null
```

Cross-reference commit bodies against Pull Request (PR) descriptions — canonical source of truth for *why* change was made. `BREAKING CHANGE:` footer = breaking change regardless of PR label.

## Step 2: Classify each change

Section order (fixed — never reorder): 🚀 Added → ⚠️ Breaking Changes → 🌱 Changed → 🗑️ Deprecated → ❌ Removed → 🔧 Fixed

| Category | Output section | What goes here |
| --- | --- | --- |
| **New Features** | 🚀 Added | User-visible additions |
| **Breaking Changes** | ⚠️ Breaking Changes | Existing code **stops working immediately** after upgrade — API removed, signature changed incompatibly, behavior changed with no fallback. Must be 100% certain it no longer works. |
| **Improvements** | 🚀 Added or 🌱 Changed | Enhancements to existing behavior |
| **Performance** | 🚀 Added or 🔧 Fixed | Speed or memory improvements |
| **Deprecations** | 🗑️ Deprecated | Old API **still works** this release but is scheduled for removal — emits a warning, replacement exists |
| **Removals** | ❌ Removed | Previously deprecated API now gone (this is what becomes a Breaking Change in the next cycle) |
| **Bug Fixes** | 🔧 Fixed | Correctness fixes |
| **Internal** | *(omit)* | Refactors, CI/tooling, deps, code cleanup, developer-facing housekeeping — omit unless directly user-impacting |

**Breaking vs Deprecated**: old call still works (even with warning) → Deprecated, never Breaking. Breaking = upgrade causes immediate failures, no compat period.

Filter out: merge commits, minor dep bumps, CI/tooling config, comment typos, internal refactors, code cleanup, internal-only dep bumps, developer housekeeping, no-user-impact changes. **Never include internal staff names or internal maintenance details in public-facing output.** Always include: breaking changes, behavior changes, new API surface.

## Step 3: Explore interesting changes

For top 3–5 most significant changes (features, breaking, major behavior), read actual diff or changed files:

```bash
git diff $RANGE -- <file>    # timeout: 3000
git show <commit>:<file>     # timeout: 3000
```

Goal: understand what change actually does at implementation level — new APIs, parameters, behavior — so notes describe real functionality, not just commit subjects.

Skip for trivial changes (typos, dep bumps, CI config).

## Step 4: Choose output format

Pre-flight — verify all templates present before proceeding:

```bash
# Resolve skill directory portably — works in developer repo and after plugin install
SKILL_DIR="$(find ~/.claude/plugins -path "*/oss/skills/release" -type d 2>/dev/null | head -1)"
[ -z "$SKILL_DIR" ] && SKILL_DIR="plugins/oss/skills/release"
for tmpl in PUBLIC-NOTES.tmpl.md CHANGELOG.tmpl.md SUMMARY.tmpl.md MIGRATION.tmpl.md; do # timeout: 5000
    [ -f "$SKILL_DIR/templates/$tmpl" ] || {
        echo "Missing template: $tmpl — aborting"
        exit 1
    }
done
```

Before writing, fetch last 2–3 releases to check project-specific formatting conventions:

```bash
gh release list --limit 3                                                  # timeout: 30000
LATEST_TAG=$(gh release list --limit 1 --json tagName --jq '.[0].tagName') # timeout: 30000
gh release view "$LATEST_TAG"                                              # timeout: 15000
```

Existing releases deviate from templates → match their style. Templates below = default; project conventions take precedence.

### Notes — user-facing, public (`notes`)

Omit sections with no content.

For `notes` mode: first produce CHANGELOG-format classification (Step 2 output). Derive user-facing notes FROM that classification, expanding interesting features with Step 3 insights. Classification = working document — don't write to disk in `notes` mode, use as structural backbone.

Read PUBLIC-NOTES template from $SKILL_DIR/templates/PUBLIC-NOTES.tmpl.md and use as format.

### CHANGELOG Entry (`changelog`)

Read CHANGELOG entry template from $SKILL_DIR/templates/CHANGELOG.tmpl.md and use as format.

### Internal Release Summary (`summary`)

Read internal release summary template from $SKILL_DIR/templates/SUMMARY.tmpl.md and use as format.

### Migration Guide (`migration`)

Read migration guide template from $SKILL_DIR/templates/MIGRATION.tmpl.md and use as format.

## Step 5: Writing guidelines

Read writing guidelines from $SKILL_DIR/guidelines/writing-rules.md and follow them.

After polishing, for `notes` and `changelog` modes dispatch shepherd for public-facing voice/tone review before writing to disk:

```bash
# Pre-compute shepherd run dir (file-handoff protocol)
SHEPHERD_DIR=".temp/release-shepherd-$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')-$(date +%Y-%m-%d)"
mkdir -p "$SHEPHERD_DIR"
# Write the generated draft content to: $SHEPHERD_DIR/draft.md before dispatching
```

```text
Agent(subagent_type="oss:shepherd", prompt="Review the draft release content at <$SHEPHERD_DIR/draft.md> for public-facing voice and tone. Apply shepherd voice guidelines: human and direct, no internal jargon, no staff names, no internal maintenance details. Write the revised content to <$SHEPHERD_DIR/shepherd-revised.md>. Return ONLY: {\"status\":\"done\",\"changes\":N,\"file\":\"<$SHEPHERD_DIR/shepherd-revised.md>\"}")
```

Read `$SHEPHERD_DIR/shepherd-revised.md` → use as final content for disk write. For `summary` and `migration` modes, skip shepherd, write directly.

Write to disk per mode:

- **`notes`**: write to `PUBLIC-NOTES.md` at repo root. Notify: `→ written to PUBLIC-NOTES.md`
- **`changelog`**: prepend entry to `CHANGELOG.md` after `# Changelog` heading (create file with that heading if missing). Notify: `→ prepended to CHANGELOG.md`
- **`summary`**: extract branch — `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` — save to `.temp/output-release-summary-$BRANCH-$(date +%Y-%m-%d).md`. Notify: `→ saved to .temp/output-release-summary-<branch>-<date>.md`
- **`migration`**: print to terminal only

## Step 6: Publish (after writing notes)

**Human gate** — stop and hand off to user: GitHub release must be created with project-level tooling (e.g. `gh release create`). See project's CLAUDE.md or `shepherd` agent (`<release_checklist>` section) for exact command.

## Mode: prepare

**Trigger**: `/release prepare <version>` (e.g., `prepare v1.3.0` or `prepare 1.3.0`)

**Purpose**: Full release pipeline — audit first, then generate all artifacts. Use when cutting release; use individual modes for drafting.

```bash
VERSION="${REST%% *}"
[[ "$VERSION" != v* ]] && VERSION="v$VERSION"
DATE=$(date +%Y-%m-%d)
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
RANGE="$LAST_TAG..HEAD"
# Resolve skill directory — required by Phase 1 audit and Phase 3 template reads
SKILL_DIR="$(find ~/.claude/plugins -path "*/oss/skills/release" -type d 2>/dev/null | head -1)"  # timeout: 5000
[ -z "$SKILL_DIR" ] && SKILL_DIR="plugins/oss/skills/release"
```

### Phase 1: Readiness audit

Run all checks from **Mode: audit** with `$VERSION` as target. Present readiness table.

**If verdict is BLOCKED**: stop. List blockers, instruct user to resolve before re-running `/release prepare $VERSION`. Write no artifacts.

**If verdict is READY or NEEDS ATTENTION**: surface warnings, continue to Phase 2.

### Phase 2: Gather and classify changes

Run **Steps 1–2** to gather and classify all commits in `$RANGE`.

Note whether **Breaking Changes** classified — gates Phase 3d.

### Phase 3: Write all artifacts

```bash
RELEASE_DIR="releases/$VERSION"
mkdir -p "$RELEASE_DIR"
```

Write each artifact in sequence:

**a. `releases/$VERSION/PUBLIC-NOTES.md`** — user-facing notes (Step 3 `notes` format). Shepherd voice review applies per Step 5.

**b. `CHANGELOG.md`** — prepend entry stamped `$VERSION — $DATE` (Step 3 `changelog` format) to root `CHANGELOG.md`. Cumulative file — not versioned per release. Create with `# Changelog` header if missing.

**c. `releases/$VERSION/SUMMARY.md`** — internal summary (Step 3 `summary` format).

**d. `releases/$VERSION/MIGRATION.md`** — always written. Breaking changes classified → use Step 3 `migration` format. No breaking changes → single line: `No breaking changes in this release.`

### Output

```markdown
## Release prepare: $VERSION

### Audit
[readiness table from Phase 1, condensed]
[any warnings carried forward]

### Written
- `releases/$VERSION/PUBLIC-NOTES.md` — user-facing notes (N features, N fixes, N breaking changes)
- `CHANGELOG.md` — $VERSION entry prepended (root, cumulative)
- `releases/$VERSION/SUMMARY.md` — internal summary
- `releases/$VERSION/MIGRATION.md` — migration guide (N breaking changes, or "No breaking changes")

### Next steps
1. Review all written files
2. Bump version in the project manifest
3. Commit, push, open PR
4. On merge: create GitHub release from PUBLIC-NOTES.md
```

## Mode: audit

**Trigger**: `/release audit [version]`

**Purpose**: Pre-release readiness check — surfaces outstanding work, alignment gaps, and blockers before cutting release.

Read and execute all checks from `$SKILL_DIR/templates/audit-checks.md`. Checks cover: version consistency across manifests, docs/CHANGELOG alignment, open blocking issues, dependency CVE scan, unreleased commits since last tag.

After readiness table, if issues found, append **Findings summary** table with one row per issue:

| # | Issue | Location | Severity |
| --- | --- | --- | --- |
| 1 | <what is wrong> | <section or file> | critical/high/medium/low |

Ensures every finding has explicit location, severity, and action — matching structured output format of `notes` and `changelog` modes.

End response with `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- Filter noise (CI config, dep bumps, typos) unless user-impacting
- **Public-facing content policy**: release notes, changelogs, migration guides = user-visible changes only. Never include: internal staff names, internal maintenance, internal refactors, CI/tooling changes, internal dep bumps, code cleanup, developer housekeeping with no user impact.
- Public-facing output co-authored with `shepherd` — follow its `<voice>` guidelines for human, direct tone
- Follow-up chains:
  - Readiness check → `/release prepare <version>` runs built-in audit first; use standalone `/release audit [version]` only for readiness check without cutting release
  - Release includes breaking changes → `/oss:analyse` for downstream ecosystem impact
  - Notes/changelog written → see Step 5 for release-create gate (`gh release create` must be user-run via project tooling)
  - `migration` content written → add to project docs and link from CHANGELOG entry

</notes>
