# Release writing and reconciliation

Read after `release-evidence.md`. Use `release-draft.md` for public draft, with project tone and verified version naming. Prior releases guide tone/detail, not permission to omit contributor or migration coverage. Use available local prior releases first; retrieve current published examples through approved GitHub reader only when required. Never import another plugin's tools or permissions.

## Deliverables

| Mode | Required product artifacts | Evidence and limits |
| -- | -- | -- |
| notes | `DRAFT.md`; selected changelog, summary, migration flags add those outputs | Draft-complete isn't release-ready; record checks outside notes scope. |
| prepare | Release-directory `DRAFT.md`, `CHANGELOG.md`, `SUMMARY.md`, `MIGRATION.md`; preserve/update canonical changelog | Full readiness checks; draft may remain when checks fail, labeled blocked. |
| audit | No product writes | Audit existing artifacts or record missing ones; no mutation to satisfy a finding. |
| demo | Executed `demo.py` in established release location or run deliverables directory | Explicitly report execution/exclusion; a planning note can't satisfy demo mode. |

Resolve existing output paths before writing, record them in plan, preserve unrelated user changes. Optional requested outputs are still mandatory once selected. Store final artifact copies under run's `deliverables/` for validation; this evidence copy doesn't replace actual project deliverable. `requested_artifacts` lists exactly selected filenames, even on a blocked run.

## Public draft quality

- Summary: two to four sentences explaining release, benefit, affected users. Avoid unsupported superlatives or claims broader than evidence.
- Highlights: rank up to three to five substantial changes by user impact; never pad small releases. Show practical examples for APIs/features where useful. Bug-fix releases can use a concrete trigger and before/after outcome rather than artificial code demos.
- Migration: concrete old/new usage, changed defaults, outputs, dtypes, configuration, removal timing, dependency/runtime constraints where relevant. With no migration, state that explicitly. Never call a release non-breaking solely because it lacks removed symbols or local downstream callers. Guidance must agree in draft, changelog, standalone migration file.
- Notable changes: classify Added, Breaking Changes, Changed, Deprecated, Removed, Fixed, Security as applicable; omit empty subsections. Group related improvements, explain actual effects, preserve all supporting PR/commit links, filter internal noise without dropping its contributor credit. Document dependency changes when users are affected.
- Contributors: complete verified human credits from `contributors.md`, not only headline authors. Include meaningful contributions and optional verified handles/profile links. If truly no human contributors, state that with evidence instead of inventing names.
- Automation: preserve verified bot authors/coauthors/PR authors in a separate inventory before human formatting or profile enrichment. End contributor section with exactly one `*Automated contributions: <sorted labels>*` line when bots contributed; use verified `@handle` labels or verified Git name when a handle is unavailable. Never render individual human-style bot credits or drop bots when filtering change prose. No bots means no automation line. Audit/demo-only runs retain this line in `contributors.md`.
- Full changelog: use observed repository identity and exact base/head. For an uncreated target tag, compare to pinned head SHA rather than publish a dead target-version link. Initial releases explain absence of a previous version, link to verified head/history. An off-branch tag comparison can show already-released material: label it as raw comparison, link filtered change table rather than implying it equals release set. Never invent a repository URL.

`SUMMARY.md` uses nonempty `## Overview` and `## Benefits` sections without repeating technical list. `MIGRATION.md` uses nonempty `## Actions` section or `## No migration required` with rationale. Release-local `CHANGELOG.md` keeps complete canonical target section and references using exact extraction contract in `release-evidence.md`. Draft text may be shorter; shortening draft never authorizes pruning canonical changelog. Section validation is a structural backstop, not proof of prose accuracy.

Check every named API and example against pinned release source. Execute examples when meaningful and authorized; unavailable optional extras/data are named limitations, not proof of success. Demo code uses project's existing environment and real local fixtures where available, deterministic inputs, no implicit installation/download/paid calls. Synthetic fixtures require project's/user's permission when applicable. Exclude an optional failing demo from prepare outputs with disclosure; a requested demo stays incomplete until it executes or user changes scope.

For a selected demo, use explicit `record-demo` entrypoint in `release-evidence.md` with existing project interpreter after reviewing and authorizing the script. Retain failed and timed-out evidence; never manufacture success from syntax checks or copied receipts. Demo-only credits belong in `contributors.md`, not executable code.

## Incremental and repeated runs

`--append` reuses existing release identity, baseline, last validated head, artifact provenance from previous validated run. Never infer completion from a branch name, current files, or an unvalidated timestamp marker. Validate previous head is ancestor of new head, and repository, channel, target version agree. Explicit input conflicting with retained scope requires resolution before edits.

Missing checkpoint, rewritten history, changed release identity, or changed artifact bytes invalidates incremental assumptions. Preserve current artifacts; rebuild full evidence inventory, then reconcile with them. Never fall back to destructive overwrite. With no new commits, still check manual edits and current claims before declaring a no-op.

Run scope collection, classification, contributor accounting, migration and truth checks for the delta, then reconcile entire draft against complete release set. Keep a per-claim provenance table in `draft-review.md`: artifact/section, exact text, contributing SHAs, stable patch IDs when available, current disposition. Patch IDs help recover changed commit identities; no match isn't a deletion decision.

Preserve unaffected prose and hand edits. A verified revert or pivot may remove/supersede exact stale claim and related examples, summaries and migration guidance; record evidence and replacement. Never leave contradictory Summary/Spotlight text because it came from an earlier cycle. Recompute contributor coverage for whole release, deduplicate credits/links, avoid accumulating repeated "since last draft" paragraphs. Re-audit historical changelog bytes and material detail after integration.

Selected artifacts define write authority: `--append` alone updates draft, not every standalone file from earlier runs. Inspect existing counterparts for contradictions; flag stale unselected files with exact required correction and owner, never claim whole release set is synchronized. A request to update complete release set authorizes selecting those existing artifacts without another permission question.

Validate final outputs and retained provenance before advancing last-processed head. Partial writes or failed validation retain prior checkpoint, require reconciliation on retry. Validated result, pinned head, artifact hashes are the checkpoint; no separate branch-global mutable marker required.

## Final semantic review

Write `draft-review.md` sections `Claims`, `Contributors`, `Changelog preservation`, `Examples`, `Deliverables`, `Limits`. Check bidirectionally: every public claim maps to evidence; every included user-visible change maps to a claim or justified grouping; every eligible human maps to credit. Audit requested artifact presence, final version/range consistency, links, migration coherence, stale/reverted claims, preserved changelog history/detail, append content. Report unavailable checks and exact owners/actions. A title or section-presence check can't establish completeness.

Use independent review when breadth or risk warrants it under shared specialist policy; otherwise record parent-owned semantic review. A reviewer examines final artifact copies, retained evidence, actual project destinations, not just a readiness summary. Any subsequent edit invalidates reviewed snapshot. Shared artifact validator is a structural backstop; it doesn't replace this semantic gate.
