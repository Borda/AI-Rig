# Advisory Consolidation

Use only for `axis=consolidate`. Find instruction sections expressing the same intent in different words, then recommend `keep`, `sharpen` or `merge-prune`. This is advisory discovery: write run artifacts only, never instruction sources, installation state or a fix-selection gate. This branch takes precedence over `mode=upgrade|adversarial`; it does not run either mode's ordinary audit steps. Preserve unrelated dirty files. Applying a proposal requires a separately authorized edit and the existing lossless instruction-compression gate.

## Inventory and scope

Use the audit inventory and explicit target. Discover actual Codex instruction owners: global, project and nested instruction layers; skill entrypoints and conditional references; role cards. Read applicable declarations and consumers before inferring loading. Never equate directory proximity, identical text or two `lazy` labels with shared activation. Do not require another plugin or a repository named `plugins/`; audit installed packages and consuming-project targets through their supplied locations.

Write `<run-directory>/consolidation.json` using schema 1 below. Sources include every instruction examined and every citing document used as evidence. Record absolute canonical regular-file paths and SHA-256 hashes of exact bytes; linked or unreadable sources go under `excluded` with reasons. Retain source snapshots in the run for review history. Record every discovered section's owner, applicability, loading evidence, dependencies and inclusive line range; never overlap item ranges within a file. Read whole files to establish that the ranges are real instruction sections rather than text inside examples or fences.

`group` names proven identical authority, hierarchy, scope and activation. Keep global/project/nested boundaries separate. Mark unresolved ownership, shared-file copies, policy siblings, frontmatter/routing metadata and unsupported structure in `blocked`. List exact original heading lines protected by inbound citations, including headings outside the candidate ranges. Include citing files in `sources`, even when they supply no items. Unknown loading uses `load=unknown`; never guess a merge scope.

The helper deliberately does not parse Markdown or discover citations. Parent and independent reviewer establish inventory and citation completeness, rendered structure and actual loading. Set `syntax_supported=false` when those properties cannot be established. Initial supported edits preserve cited headings and avoid container, fence, HTML and setext changes; proposed renames/repointing or complex structural edits remain unvalidated. This conservative ceiling should be revisited only when a concrete excluded candidate warrants a tested parser extension.

## Bounded discovery

Rank groups by likely semantic overlap and loaded instruction cost. Examine at most 12 groups and judge at most 20 candidates; record all others as `not-examined`, with reason. No minimum size forces a failed run: empty or wholly blocked scopes produce honest no-candidate advice. Use existing curator routing when batching saves work; parent-only discovery is valid for a small target. Do not automatically spawn a fixed team.

For each examined item, state its intent: situation plus directive. Cluster two or more sections from different files only when they concern the same situation and directive. An item belongs to at most one candidate. Literal duplicates, within-file repetition and agent roster overlap remain with their existing audit owners; record `elsewhere`, never silently drop or double-count them. Account for every item exactly once in `coverage`, including blocked and unclustered (`keep`) items.

- `keep`: distinct trigger, exception, incident condition, actor, literal, threshold, ordering or acceptance criterion makes separate rules useful.
- `sharpen`: retain local ownership while clarifying wording or boundaries. A lazy skill or role may only be sharpened locally or kept; never move its obligation into another file, even under different words.
- `merge-prune`: identical proven loading group and identical `always` or `scoped` load class; every original obligation remains explicit, modal strength and scope remain unchanged, and replacement text is smaller. No lazy, unknown, blocked or cross-layer merge is reviewed as eligible.

Write exact whole-section replacements in `edits`. A merge lists every member, using an empty replacement for a removed section. A sharpen lists its changed members. Map every member's obligations to exact original and resulting quotes; unchanged members still have mappings. Preserve commands, paths, numbers, exceptions, actors, order, approval and stop conditions. A map declaring `preserved=true` is evidence to challenge, not proof of semantic completeness.

## Artifact schema

The report has exactly `schema_version`, `sources`, `items`, `coverage`, `candidates`, `excluded`. Lists may be empty. Record fields:

| Record | Fields |
| -- | -- |
| source | `id`, `path` (absolute canonical file), `sha256` |
| item | `id`, `source` (source ID), `start_line`, `end_line`, `load` (`always`, `scoped`, `lazy`, `unknown`), `group`, `load_evidence`, `blocked` (strings), `protected_headings` (exact lines), `syntax_supported` (boolean) |
| coverage | `id` (item ID), `disposition` (`candidate`, `keep`, `blocked`, `elsewhere`, `not-examined`), `reason` |
| candidate | `id`, `members` (item IDs), `decision` (`keep`, `sharpen`, `merge-prune`), `reason`, `edits`, `obligations`, `review` |
| edit | `item` (member ID), `replacement` (text, including empty text) |
| obligation | `from` (member ID), `original` (source quote), `to_item` (member ID), `to` (resulting quote), `preserved` (`true`) |
| review | `digest`, `verdict` (`holds`, `weakens`, `loses`), `evidence`; initially `null` |
| exclusion | `path`, `reason` |

Keep candidates have empty edits and obligations. For sharpen, `from` equals `to_item`; for merge it may name the survivor. Retain all original obligations, not just one quote per member. Structural validation establishes record consistency, not that the map is exhaustive or the selected source set complete.

## Independent challenge and settlement

Inspect packaged `shared/audit_consolidation.py --help`, then run it through the packaged launcher with `--report <run-directory>/consolidation.json`. It reads sources and prints `outcomes` plus `review_inputs`; it writes nothing. Save stdout inside the run. Each review input binds all source identities, including citations, member inventory, decision, edits and obligation map. The digest is generated by the helper; never hand-invent one.

For each structurally eligible proposal, dispatch a fresh independent challenger with the exact `review_inputs` entry, original files/snapshots and citing documents. Do not pass the judge's `reason` or persuasive discovery narrative. Use native bounded dispatch and the normal agent-wait/deadline rules. Batch only when it preserves complete review coverage. Missing independent capacity leaves proposals unvalidated; it does not authorize calling the parent an independent reviewer.

Ask the reviewer to falsify preservation: lost or softened obligation, broader trigger, erased exception/incident, changed literal/actor/order/threshold, broken cited heading, hidden text after Markdown structure changes, or an obligation moved across lazy files. Require exact source/proposal quotes and `holds|weakens|loses` with the supplied digest. Verify genuine independent provenance before retaining a review; the helper authenticates neither the reviewer nor semantic completeness.

Rerun the helper after joining reviews. It derives `keep`, `unvalidated` (blocked/unsupported, missing or stale review), `reviewed` (`holds` with eligible structure), or `rejected` (`weakens|loses`). Changed source or citing-file bytes invalidate the run; preserve prior evidence and rebuild the affected inventory/reviews. Never edit hashes to carry forward a stale verdict. A malformed artifact must be corrected, not reported as empty success. Source references and proposal bytes must still match at final validation.

## Report and closing gates

Retain the ordinary audit files and their required headings: `audit-ledger.md`, `workflow-exploration.md`, `prompt-efficiency.md`. Populate them from this branch's scope, transitions, overlap decisions, obligation maps, load evidence and independent reviews. Mark ordinary runtime/config checks and live cost experiments unassessed with reasons rather than claiming a full standard audit. Include every exclusion, blocked item, not-examined item, other-owner disposition and review timeout. Preserve all proposal states in the final results.

Separate static byte deltas from token counts; separate always-loaded and conditional/per-invocation opportunities; never sum unvalidated proposals with reviewed ones. The helper's `byte_delta` is a proxy, not verified savings. No live cost or behavior improvement is established here. Set `value_per_token.status=not-run`; a later value-per-token comparison must satisfy the existing cost contract before acceptance. Proposals are individual alternatives, not an automatically composable patch set.

Use `shared/run_gates.py` with the helper's report validation as the executed `review` command. Mark lint, format, types and tests not applicable: advisory analysis makes no source change or executable behavior claim. Do not run autoformatters, calibration, installation or application commands on the target. Verify unchanged source bytes and preserved dirty state. Parent checks coverage, authentic review provenance, citation completeness and the final report against derived settlement.

Rejoin audit step 12. Bind the exact report bytes and copy helper `outcomes` into `AUDIT_METADATA.consolidation`; the shared artifact validator recomputes settlement and rejects mismatches. Preserve normal final-handoff rendering, validation and promotion. Report “advisory assessment completed” separately from proposal state, with the next owner/action for any recommended application. Do not ask for fix selection or apply changes as part of this audit.
