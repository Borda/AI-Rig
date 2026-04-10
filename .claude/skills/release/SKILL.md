---
name: release
description: 'Prepare release communication and check release readiness. Modes — notes (writes PUBLIC-NOTES.md), changelog (prepends CHANGELOG.md), summary (internal brief), migration (breaking-changes guide), prepare (full pipeline: audit → notes + changelog + summary + migration if breaking changes), audit (pre-release readiness check: blockers, docs alignment, version consistency, Common Vulnerabilities and Exposures (CVEs)). Use whenever the user says "prepare release", "write changelog", "what changed since v1.x", "prepare v2.0", "write release notes", "am I ready to release", "check release readiness", or wants to announce a version to users.'
argument-hint: <mode> [range] | migration <from> <to> | prepare <version> | audit [version]
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, TaskCreate, TaskUpdate
model: opus
---

<objective>

Prepare release communication based on what changed. The output format adapts to the audience and context — user-facing release notes, a CHANGELOG entry, an internal release summary, or a migration guide for breaking changes.

</objective>

<inputs>

Mode comes **first**; range or version follows:

| Invocation                       | Arguments                                    | Writes to disk                                                                              |
| -------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `/release notes [range]`         | optional git range (default: last-tag..HEAD) | `PUBLIC-NOTES.md`                                                                           |
| `/release changelog [range]`     | optional git range                           | Prepends `CHANGELOG.md`                                                                     |
| `/release summary [range]`       | optional git range                           | `.temp/output-release-summary-<branch>-<date>.md`                                           |
| `/release migration <from> <to>` | two version tags, e.g. `v1.2 v2.0`           | Terminal only                                                                               |
| `/release prepare <version>`     | version to stamp, e.g. `v1.3.0`              | All artifacts: audit → `PUBLIC-NOTES.md` + `CHANGELOG.md` + summary + migration if breaking |
| `/release audit [version]`       | optional target version                      | Terminal readiness report                                                                   |

If no mode is given, defaults to `notes`. `prepare` is the full release pipeline — it runs audit first, then generates all artifacts for the version; use it when you are ready to cut a release rather than drafting individual documents.

</inputs>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create a new task.

## Mode Detection

Parse `$ARGUMENTS` by the first token:

```bash
read FIRST REST <<<"$ARGUMENTS"
```

| First token                     | Mode      | Routing                                                                                                                                |
| ------------------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `prepare`                       | prepare   | Skip to **Mode: prepare**                                                                                                              |
| `audit`                         | audit     | Skip to **Mode: audit**                                                                                                                |
| `migration`                     | migration | `read MIGRATION_FROM MIGRATION_TO <<< "$REST"`, set `RANGE="$MIGRATION_FROM..$MIGRATION_TO"`, continue Steps 1–5 with migration format |
| `notes`, `changelog`, `summary` | as named  | Set `RANGE="$REST"` (empty = default); continue Steps 1–5                                                                              |
| *(none or bare range)*          | notes     | Set `RANGE="$ARGUMENTS"`; continue Steps 1–5                                                                                           |

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
TRUNK=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}')
gh pr list --state merged --base "${TRUNK:-main}" --limit 100 \  # timeout: 15000
--json number,title,body,labels,mergedAt,author 2>/dev/null
```

Cross-reference commit bodies against Pull Request (PR) descriptions — the canonical source of truth for *why* a change was made. If a commit footer contains `BREAKING CHANGE:`, it is a breaking change regardless of how it was labelled in the PR.

## Step 2: Classify each change

Section order (fixed — never reorder): 🚀 Added → ⚠️ Breaking Changes → 🌱 Changed → 🗑️ Deprecated → ❌ Removed → 🔧 Fixed

| Category             | Output section         | What goes here                                                                                                                                                                       |
| -------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **New Features**     | 🚀 Added               | User-visible additions                                                                                                                                                               |
| **Breaking Changes** | ⚠️ Breaking Changes    | Existing code **stops working immediately** after upgrade — API removed, signature changed incompatibly, behavior changed with no fallback. Must be 100% certain it no longer works. |
| **Improvements**     | 🚀 Added or 🌱 Changed | Enhancements to existing behavior                                                                                                                                                    |
| **Performance**      | 🚀 Added or 🔧 Fixed   | Speed or memory improvements                                                                                                                                                         |
| **Deprecations**     | 🗑️ Deprecated          | Old API **still works** this release but is scheduled for removal — emits a warning, replacement exists                                                                              |
| **Removals**         | ❌ Removed             | Previously deprecated API now gone (this is what becomes a Breaking Change in the next cycle)                                                                                        |
| **Bug Fixes**        | 🔧 Fixed               | Correctness fixes                                                                                                                                                                    |
| **Internal**         | *(omit)*               | Refactors, CI/tooling, deps, code cleanup, developer-facing housekeeping — omit unless directly user-impacting                                                                       |

**Breaking vs Deprecated**: if the old call still works (even with a warning), it is **Deprecated** — never Breaking Changes. Breaking Changes are strictly for changes where upgrading causes immediate failures with no compatibility period.

Filter out: merge commits, minor dep bumps, CI/tooling config, comment typos, internal refactors, code cleanup, internal-only dependency bumps, developer-facing housekeeping, and any change with no user-visible impact. **Never include internal staff names or internal maintenance details in public-facing output** (release notes, changelogs, migration guides). Always include: any breaking change, any behavior change, any new API surface.

## Step 3: Explore interesting changes

For the top 3–5 most significant classified changes (features, breaking changes, major behaviour changes), read the actual diff or changed files:

```bash
git diff $RANGE -- <file>    # timeout: 3000
git show <commit>:<file>     # timeout: 3000
```

Goal: understand what the change actually does at the implementation level — new APIs, new parameters, new behaviour — so notes and changelog describe real functionality, not just commit subject lines.

Skip this for trivial changes (typos, dep bumps, CI config).

## Step 4: Choose output format

Pre-flight — verify all templates are present before proceeding:

```bash
for tmpl in PUBLIC-NOTES.tmpl.md CHANGELOG.tmpl.md SUMMARY.tmpl.md MIGRATION.tmpl.md; do # timeout: 5000
    [ -f ".claude/skills/release/templates/$tmpl" ] || {
        echo "Missing template: $tmpl — aborting"
        exit 1
    }
done
```

Before writing, fetch the last 2–3 releases from the repo to check for project-specific formatting conventions:

```bash
gh release list --limit 3                                                  # timeout: 30000
LATEST_TAG=$(gh release list --limit 1 --json tagName --jq '.[0].tagName') # timeout: 30000
gh release view "$LATEST_TAG"                                              # timeout: 15000
```

If the existing releases deviate significantly from the templates below (e.g., no emoji sections, different heading levels, prose-style entries), match their style. The templates below are the default — project conventions take precedence.

### Notes — user-facing, public (`notes`)

Omit any section that has no content.

For `notes` mode: first produce a CHANGELOG-format classification (Step 2 output in changelog structure). Then derive the user-facing notes FROM that classification, expanding interesting features with implementation insights from Step 3. The changelog classification is a working document — do not write it to disk in `notes` mode, but use it as the structural backbone for the notes.

Read the PUBLIC-NOTES template from .claude/skills/release/templates/PUBLIC-NOTES.tmpl.md and use it as the format for the notes output.

### CHANGELOG Entry (`changelog`)

Read the CHANGELOG entry template from .claude/skills/release/templates/CHANGELOG.tmpl.md and use it as the format.

### Internal Release Summary (`summary`)

Read the internal release summary template from .claude/skills/release/templates/SUMMARY.tmpl.md and use it as the format.

### Migration Guide (`migration`)

Read the migration guide template from .claude/skills/release/templates/MIGRATION.tmpl.md and use it as the format.

## Step 5: Writing guidelines

Read the writing guidelines from .claude/skills/release/guidelines/writing-rules.md and follow them.

After applying the guidelines above to polish the output, write to disk per mode:

- **`notes`**: write to `PUBLIC-NOTES.md` at the repo root. Notify: `→ written to PUBLIC-NOTES.md`
- **`changelog`**: prepend the entry to `CHANGELOG.md` after the `# Changelog` heading (create the file with that heading if it does not exist). Notify: `→ prepended to CHANGELOG.md`
- **`summary`**: extract branch first — `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` — then save to `.temp/output-release-summary-$BRANCH-$(date +%Y-%m-%d).md`. Notify: `→ saved to .temp/output-release-summary-<branch>-<date>.md`
- **`migration`**: print to terminal only

## Step 6: Publish (after writing notes)

**Human gate** — stop here and hand off to the user: the GitHub release must be created with project-level tooling (e.g. `gh release create`). Refer to the project's CLAUDE.md or `oss-shepherd` agent (see `<release_workflow>` section) for the exact command.

## Mode: prepare

**Trigger**: `/release prepare <version>` (e.g., `prepare v1.3.0` or `prepare 1.3.0`)

**Purpose**: Full release preparation pipeline — audit readiness first, then generate and write all artifacts. Use this when cutting a release; use individual modes (`notes`, `changelog`, `summary`) for drafting.

```bash
VERSION="${REST%% *}"
[[ "$VERSION" != v* ]] && VERSION="v$VERSION"
DATE=$(date +%Y-%m-%d)
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
RANGE="$LAST_TAG..HEAD"
```

### Phase 1: Readiness audit

Run all checks from **Mode: audit** with `$VERSION` as the target. Present the readiness table.

**If verdict is BLOCKED**: stop here. List the blockers and instruct the user to resolve them before re-running `/release prepare $VERSION`. Do not write any artifacts.

**If verdict is READY or NEEDS ATTENTION**: surface any warnings, then continue to Phase 2.

### Phase 2: Gather and classify changes

Run **Steps 1–2** to gather and classify all commits in `$RANGE`.

Note whether any **Breaking Changes** were classified — this gates Phase 3d.

### Phase 3: Write all artifacts

```bash
RELEASE_DIR="releases/$VERSION"
mkdir -p "$RELEASE_DIR"
```

Write each artifact in sequence:

**a. `releases/$VERSION/PUBLIC-NOTES.md`** — user-facing notes (Step 3 `notes` format).

**b. `CHANGELOG.md`** — prepend entry stamped `$VERSION — $DATE` (Step 3 `changelog` format) to the root `CHANGELOG.md`. This file is cumulative — it is not versioned per release. Create it with a `# Changelog` header if it does not exist.

**c. `releases/$VERSION/SUMMARY.md`** — internal summary (Step 3 `summary` format).

**d. `releases/$VERSION/MIGRATION.md`** — always written. If breaking changes were classified in Phase 2, use the Step 3 `migration` format. If no breaking changes, write a single line: `No breaking changes in this release.`

### Output

```
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

**Purpose**: Pre-release readiness check — surfaces outstanding work, alignment gaps, and blocking issues before cutting a release.

Read and execute all checks from `.claude/skills/release/templates/audit-checks.md`. Checks cover: version consistency across manifests, docs/CHANGELOG alignment, open blocking issues, dependency CVE scan, and unreleased commits since last tag.

After the readiness table, if any issues were found, append a **Findings summary** table with one row per issue:

| #   | Issue           | Location          | Severity                 |
| --- | --------------- | ----------------- | ------------------------ |
| 1   | <what is wrong> | <section or file> | critical/high/medium/low |

This ensures every finding has an explicit location reference, severity label, and action — matching the structured output format used by `notes` and `changelog` modes.

End your response with a `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- Filter noise (CI config, dep bumps, typos) unless they are user-impacting
- **Public-facing content policy**: release notes, changelogs, and migration guides must contain only user-visible changes, fixes, and improvements. Never include: internal staff names, internal maintenance details, internal refactors, CI/tooling-only changes, internal-only dependency bumps, code cleanup, or developer-facing housekeeping with no user-visible impact.
- Public-facing output (release notes, changelogs, migration guides) is co-authored with `oss-shepherd` — follow its `<voice>` guidelines for human, direct tone
- Follow-up chains:
  - Readiness check → `/release prepare <version>` runs a built-in audit first; use standalone `/release audit [version]` only when you want a readiness check without cutting the release
  - Release includes breaking changes → `/analyse` for downstream ecosystem impact assessment
  - Notes/changelog written → See Step 5 for the release-create gate (`gh release create` must be run by the user via project tooling)
  - `migration` content written → add to project docs and link from the CHANGELOG entry

</notes>
