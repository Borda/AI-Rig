# Candidate validation

Before consolidation, the parent validates **every candidate at every severity**, including lint, documentation, architecture, Codex, and compound claims. Split atomic claims; establish the actual violated current contract and realistic trigger. Source, callers, configuration, and tests may conclusively settle a claim without execution. Inspect relevant counterevidence before publishing; generic green CI or an unrelated passing input cannot refute a claim.

Use bounded real execution when behavior requires it and the current workflow permits it. Bind CI/reproduction evidence to the reviewed revision, backend, path, environment, and assertions. When the real environment is unavailable, a faithful simulation may establish represented control flow or mathematics; state the unrepresented hardware/OS behavior. Exhaust feasible authorized checks; feasible undone checks mean **incomplete**, never an environmental limitation. A child inspection-only restriction does not prove the parent lacks an authorized check. Do not install dependencies unless the owning workflow explicitly permits bounded ephemeral verification; preserve its exact approval/provenance/environment controls. Never broaden permissions or fix product code inside review. Inspection-only workflows retain their execution restrictions.

Record each candidate in the durable report's `review-validation.json` envelope before publication: stable `id`, atomic `claim`, exact `revision`/snapshot, affected `contract`, realistic `trigger`, `counterevidence` (callers/tests/configuration/CI inspected), `checks` (actual check, result, revision/environment binding, limits), `disposition`, `reason`, `limits` (list), and boolean `feasible_checks_complete`. Each check is an object with nonempty `method`, `result`, `revision`, `environment`, and a `limits` list. `counterevidence` is a list (including inspected absent tests/CI when relevant). `feasible_checks_complete` is true for completed dispositions and false for pending null. Checks may be source inspection. Retain the original/current identities and evidenced change for later fixes.

| Disposition | Meaning |
| -- | -- |
| `CONFIRMED` | Evidence establishes the defect/current requirement violation; eligible for actionable findings. |
| `REFUTED` | Affirmative counterevidence falsifies this exact claim; one failed reproducer is insufficient unless its scope settles the claim. |
| `ALREADY_FIXED` | Valid earlier concern resolved by an evidenced later change; retain fix evidence, never relabel false positive. |
| `UNVERIFIED` | Feasible checks exhausted; named genuinely unavailable prerequisite prevents closure. Record `missing_capability`. |
| `OPTIONAL` | No violated current requirement or demonstrated in-scope defect; label advice separately. |
| `null` | Feasible check remains undone; readiness is `incomplete`. |

Rejecting a proposed repair does not refute the defect. Preserve confirmed portions of compound claims. Retained authoritative evidence remains valid when its source/revision/contract binding holds despite failed fresh retrieval. Stale evidence plus unavailable retrieval leaves `UNVERIFIED`, never `REFUTED`.

Every `UNVERIFIED` or pending (`null`) candidate has one durable `open_concerns` entry in the same envelope: `candidate_id`, `owner`, `next_check`, observable `closure`. These obligations appear separately in the report and remediation handoff, never in `findings.jsonl` or automatic fix requests. Missing protocol/reviewer coverage is a process gap with owner/recovery, not a source finding or automatic request for another review. Any review beyond the invoked workflow's authorized scope requires one explicit user decision naming the exact gap, expected evidence/output, and stop condition. If declined or dismissed, skip it without asking again or carrying its request into findings/action items; preserve only the factual limitation. Late or materially strengthened consolidator claims return to the parent for validation before publication.

## Independent critical verification

Parent validation is additive to independent verification of `CRITICAL`/`[blocking]` candidates. Spawn a separate agent of the originating type, preserving the caller's inspection-only and provenance restrictions:

```text
Independently inspect <exact revision and file/scope> for this atomic candidate: <claim, contract, realistic trigger>.
Do NOT read prior agent conclusions. Inspect source and relevant callers/tests/configuration/CI counterevidence.
Return CONFIRMED, REFUTED, ALREADY_FIXED, or UNVERIFIED with actual checks, revision/environment binding, limits and rationale. Unavailable evidence is not refutation. Include confidence per the workflow output contract.
```

**Spawn cap: max 3 verifier agents per run.** Group at most two same-origin candidates per verifier, retaining a separate verdict per ID. Candidates beyond available coverage remain required verification obligations; never bypass the cap or downgrade uncertainty into an actionable high finding. A verifier disagreement requires parent evidence resolution; uncertainty stays separate. Missing required protocol or feasible verifier work is incomplete; observed unavailable verifier capability is blocked. Preserve actual route availability and inspection-only boundaries.

## Version-one publication

Write one `review-validation.json` envelope beside `review-report.md` and `findings.jsonl`:

- `contract_version: 1`, `rating_scale: "source-5-v1"`.
- Aggregate `source_outcome`: `APPROVE`, `MINOR_EDITS`, `NEEDS_WORK`, `INSUFFICIENT_BUT_PROMISING`, `REJECT`, or JSON `null`; matching `source_rating`: 1, 2, 3, 4, 5, or JSON `null`. Never average reviewer ratings.
- `readiness`: `complete`, `blocked`, or `incomplete`; `readiness_reason` names evidence, missing capability/owner/recovery, or feasible outstanding work. Required gates retain their force. No unconditional approval or merge-readiness claim with an open required check.
- `assessments`: one entry per actual or missing required reviewer with `role`, `presence` (`completed` or `missing`), `source_outcome`, `source_rating`, `readiness`, `coverage`, `rationale`, `evidence`, and `limits`; `evidence` and `limits` are JSON arrays of strings even for one item. A completed undecidable assessment has null judgment/rating, blocked readiness, returned rationale and inspected coverage. Missing output has `presence: "missing"`, null judgment/rating and an explicit coverage limitation outside source findings; it never counts as completed inspection. Skipped roles are omitted. Keep execution provenance in evidence and limits; display role names only, with scores in square brackets (`Role [rating]`).
- `candidates` and `open_concerns`: the records above; retain all candidates. `open_concerns` holds exactly one entry for each `UNVERIFIED` or `null` candidate and none for any other disposition. Any such open candidate makes the aggregate and each affected assessment `readiness` `blocked` (`UNVERIFIED`) or `incomplete` (`null`), never `complete`. Aggregate `readiness: complete` requires every assessment `readiness: complete` and an empty `open_concerns`; one blocked or incomplete assessment makes the aggregate blocked or incomplete. A missing required role has `readiness: incomplete`; it, like any `null` candidate, makes the aggregate `incomplete`, which overrides `blocked`, also under a null aggregate outcome. Every CONFIRMED candidate appears exactly once in findings.jsonl; presentation caps may group display but never discard confirmed obligations.

Null is absence of source judgment, never a sixth rating or rejection. A completed undecidable aggregate has blocked readiness and completed undecidable assessment evidence. An incomplete aggregate remains an incomplete publication, not a completed no-judgment result. Known partial defects may justify `NEEDS_WORK` with other scope unresolved.

`APPROVE`: no required source changes; optional advice may accompany it. `MINOR_EDITS`: only confirmed lint/nits/formatting/genuinely minor docs. `NEEDS_WORK` (Major revision): aligned, valuable contribution with fixable correctness/design/architecture/tests/substantive docs issues, including fixable severe defects. `INSUFFICIENT_BUT_PROMISING`: a good initiative with a substantially incomplete source implementation; cite the missing current requirements and observable completion. This source judgment never means missing hardware, reviewer output, or process evidence. `REJECT` (Block): proven conflict with documented direction/binding constraints, citing the requirement and why current scope cannot fit; unavailable hardware, reviewer failure, evidence gaps, architecture preferences, or severity alone never imply rejection. Preserve existing licensing/provenance safeguards.

New headers carry `Review contract: 1`, `Rating scale: source-5-v1`, `Readiness: <value>`, `Outcome: <source_outcome or literal null>`, and **`Gate: REVIEW_V1_PASS`, `REVIEW_V1_BLOCK`, or `REVIEW_V1_REJECT_<GROUND> @<sha>`**. The prefix deliberately blocks legacy resolver intake; upgrade the resolver and rerun its report gate, never strip the prefix. New readers normalize only after validating the envelope. Existing terminal grounds and source SHA safeguards retain force. Historical unversioned reports preserve `REQUEST_CHANGES`, terminal `REJECT_*`/`N/A`, and the original five-rating legend (old 4 = insufficient evidence, old 5 = block/reject).

Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.
