---
name: release
description: Draft release notes, changelogs, contributor credits, migration guides, and summaries from traced unreleased changes; assess SemVer readiness without publishing.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Release

Prepare substantial, source-grounded release communication and SemVer readiness evidence. Never tag, publish, upload, or force-push. Release documents serve users; the final workflow report serves maintainers and does not replace the release draft.

## Input Schema

```json
{
  "mode": "optional notes|prepare|audit|demo; default notes",
  "range": "optional base..head or base->head; not a target version",
  "target_version": "optional SemVer version",
  "changelog": "optional boolean; --changelog updates the existing changelog without pruning history",
  "summary": "optional boolean; --summary writes a standalone executive summary",
  "migration": "optional boolean; --migration writes standalone upgrade guidance",
  "append": "optional boolean; --append reconciles an existing draft with newly landed changes",
  "done_when": "release blockers, warnings, and required artifacts are explicit"
}
```

## Workflow

<!-- policy-sibling: skills/assess/SKILL.md, skills/code-remediate/SKILL.md, skills/code-review/SKILL.md -->

For allowed GitHub reads, run the direct reader under an active opted-in `github-read` profile or request runtime approval for the complete owning command. No separate workflow consent is needed. Apply [GitHub Read Execution](../../shared/native-skill-contract.md#github-read-execution); an unexpected runtime restriction or denial stops the attempt.

### 01: Create run directory

Run `create_run.py --skill release` per `../../shared/helper-cli-contract.md`.

### 02: Determine mode, range, and target version

Select the release mode from the direct request. Local-only notes and checks remain local.

- Default `notes`: write `DRAFT.md`; optional `--changelog`, `--summary`, `--migration`, `--append` map to the boolean fields above.
- `prepare <version>`: readiness audit plus `DRAFT.md`, `CHANGELOG.md`, `SUMMARY.md`, `MIGRATION.md` in the established release directory (otherwise `releases/<version>/`); update the canonical changelog without replacing its history. A feature demo is optional and must be executed before inclusion.
- `audit [version]`: evidence and readiness verdict only; no product or release-document edits. Infer an omitted version only from verified project metadata.
- `demo [range]`: write and execute a release demonstration using the project environment; a plan alone is incomplete. Never required for non-feature releases.

Normalize `base->head` to `base..head`; retain both literal input and resolved commit IDs. Reject unknown flags, ambiguous ranges, symmetric `...` ranges, or incompatible mode/flag combinations before writing release docs. `--append` applies to notes only; other artifact flags apply to notes (prepare already includes them). A bare version supplies `target_version`, not a Git range. Read [release-evidence.md](release-evidence.md) for every mode; read [release-writing.md](release-writing.md) for notes, prepare, append, or demo.

Record mode, authorized output paths, target version, working-tree baseline, and acceptance in the run plan. Existing user content is input to preserve. Preparation does not independently authorize source/API/dependency edits or version stamping; change project version files only when requested or required by the user's authorized project release workflow.

### 03: Collect release evidence

Use [GitHub Reader Runtime Boundary](../../shared/native-skill-contract.md#github-reader-runtime-boundary) when the normal workflow calls `github_read.py`. Do not create or modify runtime approval rules files. Runtime denial stops the current attempt under the existing recovery policy. Remote publication, tagging, uploading, and other remote mutation remain forbidden.

Follow `release-evidence.md`: pin the release head, select a channel-appropriate baseline, inventory the release branch, and subtract changes already shipped to that release line using ancestry and patch evidence. A nearest tag, same subject, PR number, merge label, or default-branch listing alone cannot establish membership. Save the full candidate log to `<run-directory>/commits.txt` and the scope decision to `release-scope.md`. Initial releases include the root commit; empty or unavailable evidence is never a successful empty release.

When current GitHub release metadata is required, use `python PLUGIN_ROOT/shared/github_read.py --out <run-directory>/github-release.json -- gh release view <tag-or-url> --json <fields>` directly under an active opted-in `github-read` profile or with runtime approval for the complete reader. It prefers `gh`; public HTTPS fallback is only for public REST resources and cannot supply private evidence. Never invoke `gh` directly. An unexpected restriction or denial stops the attempt; record current release metadata as unavailable evidence.

Inspect `python PLUGIN_ROOT/shared/collect_diff.py --help`; collect `commit` scope for retained release range into `<run-directory>/range`. Collection failure is evidence gap, not empty release.

Write `<run-directory>/change-table.md`: every candidate SHA, PR/source evidence, disposition and reason, release section, user impact, breaking status, docs need, verification. Group related changes in prose while retaining every supporting commit/PR. Write `contributors.md` with complete human attribution and exclusions, and `changelog-audit.md` with scope, preserved history/detail, additions and unresolved discrepancies. These are required evidence even when changelog writing is not selected; audit mode records findings without editing it.

### 04: Verify release readiness

Required checks:

- SemVer classification matches observed API/user-visible changes.
- Breaking changes have migration guidance.
- Deprecations follow project policy and released before removal.
- CHANGELOG/release notes mention user-visible changes.
- Do not advertise reverted changes as live features.
- Call out security/dependency changes with source evidence.
- Verify claims against the pinned release head, including merged-in work and public examples; classification follows actual behavior, not commit prefixes or known local callers alone.
- Reconcile every candidate commit, all human contributors, and existing changelog content before declaring coverage complete. Missing contributor notes block communication completion; unresolved handles alone do not when verified names are credited.

**Structural context (optional)**: for Python package release, also probe codemap-py once for undocumented public surface and externally-uncalled modules: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category audit --out <run-directory>/codemap-context.json`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with checks above, using persisted evidence as additional readiness signal.

Write `<run-directory>/release-readiness.md` with:

- `SemVer`
- `Migration`
- `Checks`
- `Blockers`

Under `## Checks`, require a readiness table with exactly `Check | Status | Evidence | Blocker / next action`. Include rows `SemVer`, `Migration`, `Release scope`, `Contributors`, `Changelog`, `Documentation`, `Verification`, and `Artifacts`; add project-specific checks as needed. Status is `pass`, `fail`, `warning`, `not-applicable`, or `unavailable`. Every row needs concrete evidence and either `None` or the owner and closure action; not-applicable rows explain why. Missing required evidence is unavailable/blocked, never pass. Retain this table even in a failed run.

For `prepare`/`audit`, read and apply `../../shared/specialist-orchestration.md` only for public API changes, CI/release automation, security/dependency changes, docs/migration work, or broad verification risk; otherwise do not load it. Write `<run-directory>/specialist-release-plan.md` with narrow context packs for:

- `oss-shepherd`: SemVer, deprecation policy, maintainer readiness.
- `cicd-steward`: release workflow, publishing, CI status, artifact gates.
- `doc-scribe`: changelog, migration guide, README/examples.
- `qa-specialist`: verification matrix and test evidence.
- `security-auditor`: only when user expressly requests that advisory pass or selects the role for security/dependency-sensitive changes; it returns a bounded read-only evidence artifact to the Sol parent/session for release acceptance.
- `challenger`: release-blocker downgrade or no-blocker conclusion.

Single-agent for `notes` on narrow low-risk range unless SemVer/migration impact ambiguous.

### 05: Produce and review communication

Apply `release-writing.md` and [release-draft.md](release-draft.md). Preserve substantial changelog detail, historical versions, existing contributor credits, and hand-authored additions. Produce each selected artifact; revalidate the final merged contents on append, including stale summaries and examples. Write `draft-review.md` with claim traceability, contributor coverage, changelog preservation, example validation, artifact paths, and unresolved limitations. Audit records these checks without creating a draft. A failed readiness gate may leave clearly labeled reviewable drafts, never a release-ready claim.

Before selecting a demo as complete, use the explicit `shared/release_evidence.py` recorder described in `release-evidence.md` after execution authorization. Bind its actual output and before/after script digests. Syntax checks, headings, arbitrary credit labels, and nonempty secondary files cannot replace the receipt's source/content/execution checks.

### 06: Run required checks from `../../shared/quality-gates.md`

Inspect `python PLUGIN_ROOT/shared/run_gates.py --help`; run every project-required release gate with explicit commands/skip reasons and `--expected-head <release-head-full-sha>`. Execute from a verified clean checkout of that exact commit; when the caller's checkout differs or contains edits, use a separate clean detached worktree/snapshot without switching, resetting, or discarding caller work. Keep the absolute run directory outside the tested source or Git-ignored. Verify commands, imports and environment resolve to this snapshot rather than an installed copy or another checkout.

The runner records each executable check's expected head and observed Git head/status before and after execution. A mismatch, dirty state or failed inspection blocks the gate, even if its command exits zero. Do not remove the flag, substitute a fabricated receipt, or downgrade this failure to a warning. Skipped checks retain explicit reasons and need no execution receipt. Record source location, environment/import verification and observed receipts in `draft-review.md`; unavailable execution remains blocked where required.

### 07: Classify blockers and warnings

- `critical`: publish would ship known security/data-loss/API breakage without mitigation.
- `high`: SemVer, release-membership, attribution coverage, changelog preservation, migration, or required-check gap blocks readiness.
- `medium`: incomplete optional docs, unavailable optional profile links, uncertain compatibility requiring a named check.
- `low`: wording, formatting, or optional artifact polish.

### 08: Decide gate result, write `result.candidate.json`, validate artifacts, and publish `.reports/codex/release/<timestamp>/result.json`

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write `RELEASE_METADATA` with `release_contract_version=1`, `mode`, `target_version`, resolved range, `release_head` (full lowercase 40- or 64-character commit SHA), `requested_artifacts` (names among `DRAFT.md`, `CHANGELOG.md`, `SUMMARY.md`, `MIGRATION.md`, `demo.py`), and the `release_evidence` receipt defined in `release-evidence.md`. Passing results require every executable gate's clean before/after receipt to match `release_head`. Save exact final copies of selected deliverables in `<run-directory>/deliverables/`; bind their actual project destinations and SHA-256 hashes in the receipt. Notes always requires the draft; prepare requires all four Markdown files (the zero-breaking-change carve-out for `MIGRATION.md` content, not the file itself, is in `release-writing.md`); demo requires the executed script and receipt. Audit has an empty artifact list but still binds scope, contributor, and changelog evidence. On fail/timeout retain the requested list and explain unfinished outputs; do not fabricate them or unavailable source receipts. Validate as `release`, promote only validated candidate. Old reports lacking the version remain readable; new runs must not omit it to bypass communication checks.

## Fail-Fast Rules

01. Missing or invalid target range for notes/prepare/demo => fail.
02. Invalid SemVer target for prepare/audit => fail.
03. Breaking change without migration decision => fail.
04. Release blocker presented as warning => fail.
05. Publish/tag/upload action attempted by this skill => fail.
06. Missing `release-readiness.md` SemVer, Migration, Checks, or Blockers evidence => fail.
07. Result artifact validator failure => fail.
08. Result artifact missing => fail.
09. Passing notes/prepare result without selected deliverables, complete human credits, claim traceability, or changelog preservation evidence => fail.
10. Prior-release subtraction inferred from subjects alone, missing release-line decision, or claims about code absent from the pinned release head => fail.
11. Append overwrites user content, drops history/detail without a verified correction, or advances its checkpoint before final validation => fail.

## Quality Gates

Release readiness requires all five shared gates + shared artifact validator unless project has no executable package; record any skipped executable check as gap.

Notes-only work may mark unrelated build/type/test gates not applicable with explicit reasons; communication review and artifact validation remain mandatory. Separate successfully drafted notes from release readiness. Hosted checks must bind to their exact commit; they do not verify later local edits. The artifact validator binds local source, artifact bytes, human credits and aggregate bot accounting; it cannot prove PR-discovery completeness, classification truth or prose accuracy. The semantic review must establish those properties using `release-evidence.md` and `release-writing.md`.

## Calibration Hooks

On SemVer, deprecation, changelog, or release-blocker policy change, update calibration:

- behavioral cases: missing migration, wrong SemVer, unreleased API removal, artifact validator bypass, networked CLI owning-command approval, diverged release branch, released patch equivalence, same-title unrelated commits, contributor omissions, human noreply/coauthor attribution, changelog detail/history pruning, first release, append revert/pivot, draft versus readiness
- benchmark patterns: `release`

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared ordered frame. `Outcome` is `release-ready`, `blocked`, or `warning-only`. `Results` has one material change or blocker per row and exactly `Change | SemVer impact | Status / blocker | Evidence`. Apply shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules; include release gates and every blocker/warning with owner and closure action.

Set `outcome.title` exactly: failed/timed-out results use `blocked`; passing notes/demo or any readiness warning uses `warning-only`; only passing prepare/audit with cleared readiness uses `release-ready`. The summary explains completed draft work and remaining readiness scope. New communication-contract results require schema v2 and its validated final handoff. The shared explicit caller-contract exception still honors a user's exact requested format; never select it merely to omit the readiness table.

Add a `Readiness` table with exactly `Check | Status | Evidence | Blocker / next action`, copying the rows from `release-readiness.md` without shortening away evidence or recovery. Bind separate source IDs for change rows and readiness checks. New release handoffs therefore have the changes table plus readiness table; the shared renderer retains historical one-table compatibility. For notes-only completion, state draft completion and unassessed release readiness explicitly; do not use `release-ready` when required release checks were outside scope.

Minimum artifact payload template: `result-template.json`.
