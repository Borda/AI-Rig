---
name: release
description: Prepare release communication from git history, PRs, or a diff. Adapts output to context — user-facing release notes, CHANGELOG entry, internal release summary, or migration guide. Groups changes by type, filters noise, writes in plain language for the audience.
argument-hint: '[range] [release-notes|changelog|summary|migration] | prep <version>'
allowed-tools: Read, Write, Bash, Grep, Glob, Agent
---

<objective>

Prepare release communication based on what changed. The output format adapts to the audience and context — user-facing release notes, a CHANGELOG entry, an internal release summary, or a migration guide for breaking changes.

</objective>

<inputs>

- **$ARGUMENTS**: git tag, branch, or commit range (e.g. `v1.2.0..HEAD`, `main..release/1.3`).
  If omitted, uses the range from the last tag to HEAD.
- Optionally append the desired format: `release-notes`, `changelog`, `summary`, or `migration`.
  If not specified, infer from context (public repo → release notes, internal tool → summary).
- **Or**: `prep <version>` (e.g. `prep v1.3.0`) — skip to Mode: prep to write CHANGELOG and RELEASE_NOTES.md artifacts to disk.

</inputs>

<workflow>

## Mode Detection

If `$ARGUMENTS` starts with `prep`, skip to **Mode: prep** (at the bottom of this workflow).
Otherwise, run Steps 1–5 as normal.

## Step 1: Gather changes

```bash
# Determine range: use $ARGUMENTS or fall back to last-tag..HEAD
RANGE="${ARGUMENTS:-$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)..HEAD}"

# One-liner overview (navigation index)
git log $RANGE --oneline --no-merges

# Full commit messages — read these to catch BREAKING CHANGE footers,
# co-authors, and details omitted from the subject line
git log $RANGE --no-merges --format="--- %H%n%B"

# File-level diff stat — confirms what areas actually changed
git diff --stat $(echo "$RANGE" | sed 's/\.\./\ /')

# PR titles, bodies, and labels for merged PRs (richer context than commits)
gh pr list --state merged --base main --limit 100 \
  --json number,title,body,labels,mergedAt 2>/dev/null
```

Cross-reference commit bodies against PR descriptions — the canonical source of
truth for *why* a change was made. If a commit footer contains `BREAKING CHANGE:`,
it is a breaking change regardless of how it was labelled in the PR.

## Step 2: Classify each change

| Category             | Output section         | What goes here                                       |
| -------------------- | ---------------------- | ---------------------------------------------------- |
| **Breaking Changes** | ⚠️ Breaking Changes    | Requires callers to change code, config, or behavior |
| **New Features**     | 🚀 Added               | User-visible additions                               |
| **Improvements**     | 🚀 Added or 🌱 Changed | Enhancements to existing behavior                    |
| **Bug Fixes**        | 🔧 Fixed               | Correctness fixes                                    |
| **Performance**      | 🚀 Added or 🔧 Fixed   | Speed or memory improvements                         |
| **Deprecations**     | 🗑️ Deprecated          | Still works, scheduled for removal                   |
| **Removals**         | ❌ Removed             | Previously deprecated API now gone                   |
| **Internal**         | *(omit)*               | Refactors, CI, deps — omit unless user-impacting     |

Filter out: merge commits, minor dep bumps, CI config, comment typos.
Always include: any breaking change, any behavior change, any new API surface.

## Step 3: Choose output format

### Release Notes (user-facing, public)

Omit any section that has no content.

````markdown
## 🚀 Added

- **Feature Name.** One-sentence description of what it does and why it matters. (#PR)

# Minimal real-usage example showing the new surface

```python
# example usage here
```

| Option | Best for |
| ------ | -------- |
| `NAME` | ...      |

- `new_param` added to `SomeConfig`, allowing X. (#PR)

## 🌱 Changed

- [Behaviour change]: old behaviour → new behaviour. (#PR)

## 🗑️ Deprecated

- `OLD_NAME` deprecated in favour of `NEW_NAME`. (#PR)

## ❌ Removed

- `OLD_API` removed (deprecated since vX.Y). Migrate to `NEW_API`. (#PR)

## 🔧 Fixed

- Fixed [what was broken] when [condition]. (#PR)

## ⚠️ Breaking Changes

- **[Area]**: [what changed and what callers must do to migrate]. (#PR)

---

## 🏆 Contributors

A special welcome to our new contributors and a big thank you to everyone who helped with this release:

* **Full Name** (@handle) ([LinkedIn](url)) – *What they built or fixed*
* **Full Name** (@handle) – *What they built or fixed*

---

**Full changelog**: https://github.com/[org]/[repo]/compare/vPREV...vNEXT
````

### CHANGELOG Entry (Keep a Changelog format)

```markdown
## [version] — [date]
### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security
```

### Internal Release Summary

```markdown
## Release [version]
**What shipped**: [2-3 sentence summary of the most important changes]
**Impact**: [who is affected and how]
**Action required**: [anything ops/support/consumers need to do]
**Rollback**: [safe to roll back? any caveats?]
```

### Migration Guide (breaking changes only)

```markdown
## Migrating from [old] to [new]
### [Breaking change name]
**Before**: [snippet]
**After**: [snippet]
**Why**: [reason]
```

## Step 4: Writing guidelines

Write for the reader, not the commit author.

| Element          | Rule                                                              |
| ---------------- | ----------------------------------------------------------------- |
| Feature heading  | Bold title, period, then plain-English description — no jargon    |
| PR numbers       | Always at line end: `(#N)` or `(#N, #M)` — never omit             |
| Code examples    | Real usage showing the new surface; not pseudocode                |
| Tables           | Use for option/preset comparisons; skip for single-item features  |
| Breaking changes | State exactly what breaks and the migration path                  |
| Fix items        | Say what was broken and under what condition — not just "fixed X" |
| Changed items    | Behaviour changes only — old behaviour → new behaviour            |
| Deprecated items | Name old API and its replacement; omit removal version if unknown |
| Removed items    | State deprecated-since version and migration target               |

Bad/good examples:

- Bad: `"refactor: extract UserService from monolith"` → Good: `"User management is now ~40% faster"`
- Bad: `"Fix auth bug"` → Good: `"Fixed login failure for email addresses containing special characters"`

**Contributors rules:**

- List every external contributor, even for a one-liner fix
- Format: `* **Full Name** (@handle) ([LinkedIn](url)) – *noun phrase of what they built/fixed*`
- LinkedIn is optional — include only if known; never guess
- New contributors get a welcome sentence above the list
- Maintainer always listed last with infra / CI / docs scope

After applying the guidelines above to polish the output, write the full content to `tasks/output-release-$(date +%Y-%m-%d).md` using the Write tool and notify: `→ saved to tasks/output-release-$(date +%Y-%m-%d).md`

## Step 5: Publish (after writing notes)

Use project-level tooling to build, publish, and create the GitHub release. Refer to the project's CLAUDE.md or `oss-maintainer` agent for the specific commands.

```bash
# example only — check project CLAUDE.md or oss-maintainer agent for actual release process
gh release create v<version> --title "v<version>" \
  --notes "$(cat RELEASE_NOTES.md)"
```

## Mode: prep

**Trigger**: `/release prep <version>` (e.g., `prep v1.3.0` or `prep 1.3.0`)

**Purpose**: Write release artifacts to disk, ready for the manual bump → commit → push → PR workflow.

```bash
VERSION=$(echo "$ARGUMENTS" | awk '{print $2}')
[[ "$VERSION" != v* ]] && VERSION="v$VERSION"
DATE=$(date +%Y-%m-%d)
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
RANGE="$LAST_TAG..HEAD"
```

Run **Steps 1–2** to gather and classify all changes in `$RANGE`. Then write two artifacts:

### 1. Prepend to `CHANGELOG.md`

Generate the entry in Keep a Changelog format, omitting empty sections. Then:

- If `CHANGELOG.md` exists: insert the new entry after the first `# Changelog` heading line
- If it does not exist: create it with a `# Changelog` header followed by the new entry

### 2. Write `RELEASE_NOTES.md`

Write the user-facing release notes (Step 3 "Release Notes" format) to `RELEASE_NOTES.md` at the repo root. Ready to paste directly into the GitHub release body.

### Output

```
## Release prep: $VERSION

### Written
- `CHANGELOG.md` — $VERSION entry prepended (N changes across M categories)
- `RELEASE_NOTES.md` — user-facing notes ready to paste into GitHub release

### Next steps
1. Review both files
2. Bump version in the project manifest
3. Commit, push, open PR
4. On merge: create GitHub release from RELEASE_NOTES.md
```

End your response with a `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- Filter noise (CI config, dep bumps, typos) unless they are user-impacting
- Follow-up chains:
  - Notes look good → `/release prep <version>` to write artifacts to disk
  - Release includes breaking changes → `/analyse` for downstream ecosystem impact assessment
  - Pre-release audit → `/security` for dependency vulnerability scan before publishing

</notes>
