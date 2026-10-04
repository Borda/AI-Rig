# Final Handoff Contract

> Load this common contract plus the matching required supplement for every workflow disposition, including terminal and exact caller output. Other workflows need no supplement.

| Workflow | Required supplement |
| -- | -- |
| `code-review` | [Code Review handoff](final-handoff-code-review.md) |
| `code-remediate` | [Remediation handoff](final-handoff-code-remediate.md) |
| `challenge-resolve` | [Challenge handoff](final-handoff-challenge-resolve.md) |
| `release` | [Release handoff](final-handoff-release.md) |

The post-gate presentation checkpoint makes the workflow's final user-facing structure executable. Artifact workflows must write `<run-directory>/final-handoff.json`, render it with `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/final_handoff.py render --handoff <run-directory>/final-handoff.json --out-final <run-directory>/final.md --out-validation <run-directory>/final-handoff.validation.json`, bind returned digests under `result.metadata.final_handoff`, pass all skill-specific and shared artifact validators, promote the candidate, then emit validated `final.md` bytes verbatim. Never manually reconstruct or summarize the response after validation.

Rendered section and table labels use portable Markdown bold text such as `**Outcome**` and `**Verification**`. Never emit ATX/Setext headings, ANSI color escapes, or renderer-specific HTML styling; final reports must stay compact and readable in monochrome terminals, saved Markdown, plain logs.

This is a post-gate checkpoint, not a sixth quality gate. Order is fixed: five gates; final-handoff creation and rendering; result candidate using schema 3 for every current Code Review disposition or schema 2 for other skills; skill validator; shared validator including `final_handoff.py check`; candidate promotion; verbatim final output. A failure in the checkpoint blocks promotion and completion.

## Schema

`final-handoff.json` has exactly these top-level fields: `schema_version`, `skill`, `branch`, `outcome`, `tables`, `source_records`, `source_coverage`, `verification`, `remaining`, `next_steps`, `confidence`, `artifacts`, `caller_contract`. Schema version is `1`. The helper's executable validation is authoritative for nested types, closed vocabularies, exact columns, row/source uniqueness, completeness counts, confidence bands, closure evidence, branch-specific table rules.

New workflow handoffs set `presentation_version=3`; historical v2 retains its exact rendered bytes and omitted version retains the v1 compatibility path. Both v2 and v3 start with plain-English outcome prose, omit an empty Results section, render one concise checks-not-run line when all gates are not applicable, and render each recovery action once with its owner. V3 also prefixes remaining next steps with their row IDs; v2 keeps its original unprefixed text. The complete machine `outcome`, `verification`, `remaining`, `next_steps` records stay unchanged. Exact caller-contract output stays exact.

Normal artifact workflows use `branch=standard`; `code-review` uses `assessed`, `unavailable`, or `closed`. An explicitly requested exact caller output uses `caller-contract`, records requested format and evidence, forbids normal tables, emits only `caller_contract.output`. Never infer this override merely because concise output would be convenient.

For normal table branches, every material finding, decision, changed surface, experiment, recommendation, or source-owned remediation item must have one stable row ID. `source_records` is the complete source ledger; each table row names its represented source IDs; `source_coverage.omitted_source_records_total` must be zero. A table may carry ordered `details` entries with exactly `id` and `text`; every detail ID must be unique, referenced as `[<id>]` by cell in that table, with the renderer placing definitions immediately below the table. Remaining work names the related row ID, item, owner, next action; `next_steps` contains exactly the remaining row IDs. Terminal `code-review` branches contain no tables or source records.

`verification` preserves all five gate records in canonical order, uses the corresponding gate `stdout` artifact path as evidence. `confidence.score`, gaps, gap closure status/evidence/rationale, limits must exactly reconcile with the result's confidence metadata. `artifacts` must include the candidate/final result path.

Every pause follows [Actionable Pauses](native-skill-contract.md#actionable-pauses), including terminal review unavailability and assessed review with a withheld executable check. Use existing fields: `outcome.summary` states the stopped action, concrete cause/evidence, governing rule; the related `remaining` entry names continuing work in `item`, the responsible actor in `owner`, feasible recovery plus exact resume condition in `next_action`. Keep that entry in `next_steps`. For assessed code review, whose outcome is a fixed recommendation, place the complete explanation in the related remaining item and canonical operational blocker when applicable. An exact caller contract must preserve the same explanation within its allowed output. Never reduce handoff to an error code, generic approval request, or instruction to retry unchanged after deterministic failure.

Use the existing `next_action` text for recommended recovery, pending decision identity, consequences, resume condition. When a decision is needed, follow [User Questions](native-skill-contract.md#user-questions): show required context first, then let one native control own the live question and choices; only the plain-chat fallback includes them in `next_action`. Keep the exact original question, accepted answers, scope binding, actual response in existing durable workflow notes without extending closed schemas. Never duplicate a native question in rendered final Markdown. Distinguish continuing work from work paused until a specific condition; never imply a temporary process failure permanently blocks the goal. Keep canonical branch wording, schemas, evidence bindings, historical bytes unchanged. An unvalidated process pause follows the same guidance without claiming a validated final handoff.

## Exact table columns

Use the active skill's `Output Contract` columns, enforced by the handoff helper. Supplements add their workflow's table rules.

## Result compatibility

`write-result.py` creates schema-2 results for other skills and schema-3 results for every current Code Review disposition. It requires `metadata.final_handoff` with schema version, three sibling paths, two SHA-256 digests, and branch. `validate-artifacts.py` checks those paths, exact rendered bytes, digests, gates, confidence, result artifact reference, and workflow-specific reconciliation. Historical results with no `schema_version` are read as schema 1 and remain valid without final-handoff artifacts; new results cannot opt into that compatibility path because the writer emits schema 2 or 3.

## Transport limit and manager exception

The checkpoint proves the complete response bytes the workflow must emit, but the current host has no post-send transcript hook that can prove chat transport reproduced them. Treat any later manual rewrite as a contract violation; a future host transcript hook may close this residual transport gap without changing artifact schema.

`agent-shims` remains a documented lifecycle exception because it has no canonical `.reports` result artifact, is excluded from the artifact/calibration roster. Its final chat contract remains advisory until that manager gains a canonical run/result lifecycle; never claim executable final-response validation for it.
