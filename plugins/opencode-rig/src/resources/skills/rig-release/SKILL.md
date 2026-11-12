---
name: Rig Release
description: Assess release readiness with a version baseline from the last release tag, full configured gates, changelog and README synchronization, packaged file verification, and read-only release-note, migration, and contributor-reply drafts. Use before tagging or publishing. NOT for performing the publication itself.
---

# Rig Release

> Read [Evidence rules](../../references/evidence.md).

> This workflow never publishes, tags, pushes, or mutates a remote. Publication stays human-owned.

## Workflow

1. **Read the baseline from the last release tag.** Identify the project's version manifest and release-tag convention for the package being released. Read its version from the last release tag for that package. If no release tag matches, use a user-declared baseline from the request or the project's declared release policy, which takes precedence over `HEAD`; without one, use `HEAD` and carry "no release tag found, using HEAD as the first-release baseline" as a limit. State that a package that shipped before without tags needs the user's declared baseline. Do not use an uncommitted version as the baseline.
2. **Classify the change.** Fix or wording: patch. New capability or surface: minor. Breaking contract: major, and only with explicit approval.
3. **Start the run.** Call `rig_evidence` with `action: "start"`, `skill: "rig-release"`, and the package root. Keep the returned `run_id`, then call `rig_evidence` with `action: "record"`, that `run_id`, and a `decision` naming the baseline ref and version, plus any `limits` identified in step 1.
4. **Run the full gate set.** `rig_gate` with `action: "run"` and no name filter. A single failure blocks readiness; do not retry unchanged.
5. **Synchronize documentation.** Verify the changelog bullet describes user-visible impact, the README version marker matches the manifest, and changed agents, skills, tools, and commands are documented.
6. **Draft the release pack.** Record the exact commit range and its resolved endpoint SHAs with `rig_evidence` `action: "record"` and `decision`, then produce the three drafts below from that evidence. Every line must trace to a commit, finding, or changelog entry; an empty section is better than an invented one.
7. **Verify the package.** If a configured package build or dry-run gate exists, prefer it through `rig_gate`; otherwise run `npm pack --dry-run` (or the project equivalent) directly. Default Makefile/package.json gate detection covers lint, format, typecheck, and test, so it does not supply a package gate. Check the file list, save it in the run directory, and bind it with `rig_evidence`, `action: "record"`, the `run_id`, and `outputs`. No tests, plans, credentials, or private evidence may ship.
8. **Compare versions.** Declared version must equal the recorded baseline plus exactly one SemVer step unless the package is new. Record the comparison.
9. **Finalize.** Call `rig_evidence` with `action: "finalize"`. If finalization fails, report its blockers and do not claim readiness. Produce a readiness verdict with blockers, the drafts, and a human checklist.

## Release pack

- **Release notes.** Group by Added, Changed, Fixed, Security, Deprecated, and Removed. Each entry names the user-visible effect, not the internal refactor, and credits the contributor when the commit or issue records one.
- **Migration notes.** List breaking changes, renamed options, changed defaults, and required user actions, each with before/after snippets and a rollback path. Say explicitly when no action is required.
- **Contributor replies.** Draft a short factual reply per linked issue or pull request: what shipped, what did not, and what happens next. Never promise a date, never claim a fix that is not in the range, and never post it.

## Rules

- A dirty working tree is not a blocker for assessment, but it must be disclosed; the released commit must be clean.
- Never skip a SemVer step or move the version below the baseline.
- Do not publish, tag, push, comment, or open a pull request, even when every check passes. Drafts stay in the report.
- Never invent a changelog entry, contributor, issue link, or migration step. Missing evidence is a blocker, not a blank to fill.
- Report unavailable evidence as a blocker, not a pass.

## Output

Readiness verdict, proposed version, gate results, documentation state, package file list, release notes, migration notes, contributor reply drafts, blockers, and the remaining human steps.
