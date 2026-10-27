---
name: Rig Release
description: Assess release readiness with a version baseline from HEAD, full gates, packaged file verification, and read-only release-note, migration, and contributor-reply drafts.
---

# Rig Release

> Read [Evidence rules](../../references/evidence.md).

> This workflow never publishes, tags, pushes, or mutates a remote. Publication stays human-owned.

## Workflow

1. **Read the baseline from HEAD.** Do not use an uncommitted version. For this plugin family, `package.json` is the version manifest; confirm the repository's policy for the package being released.
2. **Classify the change.** Fix or wording: patch. New capability or surface: minor. Breaking contract: major, and only with explicit approval.
3. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-release"`, and the package root.
4. **Run the full gate set.** `rig_gate` with `action: "run"` and no name filter. A single failure blocks readiness; do not retry unchanged.
5. **Synchronize documentation.** Verify the changelog bullet describes user-visible impact, the README version marker matches the manifest, and changed agents, skills, tools, and commands are documented.
6. **Draft the release pack.** Bind the commit range with `rig_evidence` `action: "record"` and `commit`, then produce the three drafts below from that evidence. Every line must trace to a commit, finding, or changelog entry; an empty section is better than an invented one.
7. **Verify the package.** Run the package build or `npm pack --dry-run` (or the project equivalent) and check the file list. No tests, plans, credentials, or private evidence may ship.
8. **Compare versions.** Declared version must equal `HEAD` plus exactly one SemVer step unless the package is new. Record the comparison.
9. **Finalize.** Produce a readiness verdict with blockers, the drafts, and a human checklist.

## Release pack

- **Release notes.** Group by Added, Changed, Fixed, Security, Deprecated, and Removed. Each entry names the user-visible effect, not the internal refactor, and credits the contributor when the commit or issue records one.
- **Migration notes.** List breaking changes, renamed options, changed defaults, and required user actions, each with before/after snippets and a rollback path. Say explicitly when no action is required.
- **Contributor replies.** Draft a short factual reply per linked issue or pull request: what shipped, what did not, and what happens next. Never promise a date, never claim a fix that is not in the range, and never post it.

## Rules

- A dirty working tree is not a blocker for assessment, but it must be disclosed; the released commit must be clean.
- Never bump a version skips a step or down from `HEAD`.
- Do not publish, tag, push, comment, or open a pull request, even when every check passes. Drafts stay in the report.
- Never invent a changelog entry, contributor, issue link, or migration step. Missing evidence is a blocker, not a blank to fill.
- Report unavailable evidence as a blocker, not a pass.

## Output

Readiness verdict, proposed version, gate results, documentation state, package file list, release notes, migration notes, contributor reply drafts, blockers, and the remaining human steps.
