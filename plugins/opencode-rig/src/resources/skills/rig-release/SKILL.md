---
name: Rig Release
description: Assess release readiness with a version baseline from HEAD, full gates, and packaged file verification.
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
6. **Verify the package.** Run the package build or `npm pack --dry-run` (or the project equivalent) and check the file list. No tests, plans, credentials, or private evidence may ship.
7. **Compare versions.** Declared version must equal `HEAD` plus exactly one SemVer step unless the package is new. Record the comparison.
8. **Finalize.** Produce a readiness verdict with blockers and a human checklist.

## Rules

- A dirty working tree is not a blocker for assessment, but it must be disclosed; the released commit must be clean.
- Never bump a version skips a step or down from `HEAD`.
- Do not publish, tag, or push, even when every check passes.
- Report unavailable evidence as a blocker, not a pass.

## Output

Readiness verdict, proposed version, gate results, documentation state, package file list, blockers, and the remaining human steps.
