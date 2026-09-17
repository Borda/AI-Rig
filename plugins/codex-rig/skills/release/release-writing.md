# Release writing and reconciliation

Read after `release-evidence.md`. Use `release-draft.md` for the public draft, with project tone and verified version naming. Prior releases guide tone/detail, not permission to omit contributor or migration coverage. Use available local prior releases first; retrieve current published examples through the approved GitHub reader only when required. Do not import another plugin's tools or permissions.

## Deliverables

| Mode | Required product artifacts | Evidence and limits |
| -- | -- | -- |
| notes | `DRAFT.md`; selected changelog, summary, migration flags add those outputs | Draft-complete is not release-ready; record checks outside notes scope. |
| prepare | Release-directory `DRAFT.md`, `CHANGELOG.md`, `SUMMARY.md`, `MIGRATION.md`; preserve/update canonical changelog | Full readiness checks; draft may remain when checks fail, labeled blocked. |
| audit | No product writes | Audit existing artifacts or record missing ones; no mutation to satisfy a finding. |
| demo | Executed `demo.py` in established release location or run deliverables directory | Explicitly report execution/exclusion; a planning note cannot satisfy demo mode. |

Resolve existing output paths before writing, record them in the plan, and preserve unrelated user changes. Optional requested outputs are still mandatory once selected. Store final artifact copies under the run's `deliverables/` for validation; this evidence copy does not replace the actual project deliverable. `requested_artifacts` lists exactly selected filenames, even on a blocked run.

## Public draft quality

- Summary: two to four sentences explaining the release, benefit and affected users. Avoid unsupported superlatives or claims broader than the evidence.
- Highlights: rank up to three to five substantial changes by user impact; do not pad small releases. Show practical examples for APIs/features where useful. Bug-fix releases can use a concrete trigger and before/after outcome rather than artificial code demos.
- Migration: concrete old/new usage, changed defaults, outputs, dtypes, configuration, removal timing and dependency/runtime constraints where relevant. With no migration, state that explicitly. Do not call a release non-breaking solely because it lacks removed symbols or local downstream callers. Guidance must agree in draft, changelog and standalone migration file.
- Notable changes: classify Added, Breaking Changes, Changed, Deprecated, Removed, Fixed and Security as applicable; omit empty subsections. Group related improvements, explain actual effects, preserve all supporting PR/commit links, and filter internal noise without dropping its contributor credit. Document dependency changes when users are affected.
- Contributors: complete verified human credits from `contributors.md`, not only headline authors. Include meaningful contributions and optional verified handles/profile links. If there truly are no human contributors, state that with evidence instead of inventing names.
- Automation: preserve verified bot authors/coauthors/PR authors in a separate inventory before human formatting or profile enrichment. End the contributor section with exactly one `*Automated contributions: <sorted labels>*` line when bots contributed; use verified `@handle` labels or the verified Git name when a handle is unavailable. Never render individual human-style bot credits or drop bots when filtering change prose. No bots means no automation line. Audit/demo-only runs retain this line in `contributors.md`.
- Full changelog: use the observed repository identity and exact base/head. For an uncreated target tag, compare to the pinned head SHA rather than publish a dead target-version link. Initial releases explain the absence of a previous version and link to the verified head/history. An off-branch tag comparison can show already-released material: label it as the raw comparison and link the filtered change table rather than implying it equals the release set. Never invent a repository URL.

`SUMMARY.md` uses nonempty `## Overview` and `## Benefits` sections without repeating the technical list. `MIGRATION.md` uses a nonempty `## Actions` section or `## No migration required` with rationale. Release-local `CHANGELOG.md` keeps the complete canonical target section and references using the exact extraction contract in `release-evidence.md`. Draft text may be shorter; shortening the draft never authorizes pruning the canonical changelog. Section validation is a structural backstop, not proof of prose accuracy.

Check every named API and example against the pinned release source. Execute examples when meaningful and authorized; unavailable optional extras/data are named limitations, not proof of success. Demo code uses the project's existing environment and real local fixtures where available, deterministic inputs, and no implicit installation/download/paid calls. Synthetic fixtures require the project's/user's permission when applicable. Exclude an optional failing demo from prepare outputs with disclosure; a requested demo remains incomplete until it executes or the user changes scope.

For a selected demo, use the explicit `record-demo` entrypoint in `release-evidence.md` with the existing project interpreter after reviewing and authorizing the script. Retain failed and timed-out evidence; do not manufacture success from syntax checks or copied receipts. Demo-only credits belong in `contributors.md`, not executable code.

## Incremental and repeated runs

`--append` reuses the existing release identity, baseline, last validated head and artifact provenance from the previous validated run. Do not infer completion from a branch name, current files or an unvalidated timestamp marker. Validate that the previous head is an ancestor of the new head and that repository, channel and target version agree. Explicit input conflicting with retained scope requires resolution before edits.

Missing checkpoint, rewritten history, changed release identity or changed artifact bytes invalidates incremental assumptions. Preserve current artifacts; rebuild the full evidence inventory, then reconcile with them. Never fall back to destructive overwrite. With no new commits, still check manual edits and current claims before declaring a no-op.

Run scope collection, classification, contributor accounting, migration and truth checks for the delta, then reconcile the entire draft against the complete release set. Keep a per-claim provenance table in `draft-review.md`: artifact/section, exact text, contributing SHAs, stable patch IDs when available, and current disposition. Patch IDs help recover changed commit identities; no match is not a deletion decision.

Preserve unaffected prose and hand edits. A verified revert or pivot may remove/supersede the exact stale claim and related examples, summaries and migration guidance; record the evidence and replacement. Do not leave contradictory Summary/Spotlight text because it came from an earlier cycle. Recompute contributor coverage for the whole release, deduplicate credits/links, and avoid accumulating repeated “since last draft” paragraphs. Re-audit historical changelog bytes and material detail after integration.

Selected artifacts define write authority: `--append` alone updates the draft, not every standalone file from earlier runs. Inspect existing counterparts for contradictions; flag stale unselected files with the exact required correction and owner, and do not claim the whole release set is synchronized. A request to update the complete release set authorizes selecting those existing artifacts without another permission question.

Validate final outputs and retained provenance before advancing the last-processed head. Partial writes or failed validation retain the prior checkpoint and require reconciliation on retry. The validated result, pinned head and artifact hashes are the checkpoint; no separate branch-global mutable marker is required.

## Final semantic review

Write `draft-review.md` sections `Claims`, `Contributors`, `Changelog preservation`, `Examples`, `Deliverables`, `Limits`. Check bidirectionally: every public claim maps to evidence; every included user-visible change maps to a claim or justified grouping; every eligible human maps to credit. Audit requested artifact presence, final version/range consistency, links, migration coherence, stale/reverted claims, preserved changelog history/detail and append content. Report unavailable checks and exact owners/actions. A title or section-presence check cannot establish completeness.

Use independent review when breadth or risk warrants it under the shared specialist policy; otherwise record the parent-owned semantic review. A reviewer examines the final artifact copies, retained evidence and actual project destinations, not just a readiness summary. Any subsequent edit invalidates the reviewed snapshot. The shared artifact validator is a structural backstop; it does not replace this semantic gate.
