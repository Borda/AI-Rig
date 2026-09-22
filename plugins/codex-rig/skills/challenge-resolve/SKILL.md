---
name: challenge-resolve
description: Independently review and fix a scoped diff through bounded convergence rounds, with evidence-backed closure and explicit stop/recovery decisions. Use for requested adversarial review-and-fix loops, not a single read-only review.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Challenge and Resolve

Read, apply `../../shared/adversarial-loop.md` before dispatch or edits. That shared procedure owns the algorithm; this entrypoint owns Codex artifacts, the closing gate. Read `evidence-contract.md` before source capture or reviewer dispatch. Also read `../../shared/native-skill-contract.md` and `../../shared/specialist-orchestration.md` for authority, recurrence, reviewer admission, evidence limits.

## Input Schema

```json
{
  "goal": "required review-and-fix objective",
  "scope_files": ["bounded files or diff scope"],
  "specification": "required behavior or acceptance criteria",
  "symptom": "reported failure, or explicit preventive review",
  "caller_run": "optional existing workflow run to resume",
  "done_when": "independent final review is clean and normal checks pass"
}
```

Use default max 3 review rounds, including initial W_0. In-scope feasible fixes are authorized by a review-and-fix request; a structural finding does not authorize scope expansion, public API changes, commits, installs, network access or publication. A caller's stricter scope or admission still applies.

## Workflow

### 01: Establish scope and evidence ownership

Read `../../shared/helper-cli-contract.md`; create a run with `create_run.py --skill challenge-resolve`. Record `caller_run` when supplied, without overwriting its artifacts. Retain baseline source and acceptance evidence. Write `loop-report.md` with `Scope`, `Rounds`, `Findings`, `Recovery`, and `Verification` sections. Identify implementation author and allowed reviewer route before dispatch.

### 02: Run the shared bounded procedure

Follow the shared five-step procedure: challenge and collect findings; report the old/new table once after each validated round; resolve every feasible in-scope finding; escalate unresolved `security`, `critical` or `high`; repeat until clean, three rounds, plateau or a non-converging score. Retain `loop-ledger.json`, `loop-actions.json`, `round-<index>.diff`, current snapshot `current.diff` and each independent report. Read `adversarial_loop.py --help`, validate the ledger after each round, and invoke `adversarial_loop.py --ledger <run-directory>/loop-ledger.json --progress` only after a newly completed challenge, before the next fix, review or stop. Show its full cumulative stderr table with `Iteration | Critical | High | Medium | Low | Nits | Weighted score` and literal `old + new` cells. Never show a placeholder progress table before a validated round or repeat the table in unrelated status updates. Keep the final canonical `Results` table unchanged. Structural or repeated findings alone do not stop an authorized feasible fix; scope, authority, recurrence, plateau, missing independence, stale evidence and exhausted rounds retain their shared stop rules. A `fixed-pending-verification` finding remains open. After resolution/escalation, bind every open finding to an action and run `adversarial_loop.py --ledger <run-directory>/loop-ledger.json --actions <run-directory>/loop-actions.json` before another challenge or final handoff. The helper validates recorded actions, not their truth; the parent must inspect feasibility and fix evidence.

Collect scoped source snapshots with the existing `collect_diff.py` snapshot mode, retain `loop-evidence.json` per `evidence-contract.md`. Use existing Code Review routing, frozen contexts, specialist manifests for reviewer provenance; never manufacture a second runtime evidence format. Every participating reviewer receives the exact snapshot contents, diff, response contract, returns one structured response containing every finding, with only the route-required provenance header outside it. Preserve original reports and runtime evidence.

This explicitly requested loop uses the shared orchestration policy's bounded serial-review exception, not additional parallel waves or write delegation. Set a 1,500-output-token hard reviewer limit when the runtime supports it; otherwise request that target, record its advisory status, and never truncate findings. Re-plan and request a decision only when the next round needs new scope, authority, or a caller-specific approval. There is no fake independence fallback: parent-serial inspection stays labeled non-independent and cannot satisfy this skill's clean outcome.

### 03: Verify closure and run normal gates

Require independent final current-diff coverage before reporting clean. Run `adversarial_loop.py --ledger <run-directory>/loop-ledger.json` for the computed stop decision, then this skill's `validate_evidence.py` for existing reviewer provenance, returned findings, dispatched source contents, freshly recaptured current source. Derive the runtime log root and active `CODEX_THREAD_ID` from the observed host configuration, never from review input or the ledger. A declared identity or saved hash alone is insufficient. Never turn rejected evidence into accepted coverage.

Use `run_gates.py` for actual project lint, format, types, tests, review, with explicit reasons for genuinely inapplicable checks. Include `adversarial_loop.py --ledger <run-directory>/loop-ledger.json --require-clean` in the review gate, along with checks of independent coverage, current source, request conformance. The flag exits nonzero for a valid but non-clean loop, so a stopped ledger cannot masquerade as a passing review gate. Report `status=fail` with the concrete reason when the loop is not clean. For a supplied caller, resume its first unmet checkpoint, complete its ordinary gates and artifact contract too; neither result substitutes for the other.

### 04: Validate and hand off

Store the checker's exact JSON summary in `ADVERSARIAL_LOOP_METADATA.adversarial_loop`, and keep `action_contract_version: 1` in result metadata. Follow the shared helper lifecycle: render bound handoff, write candidate with `write-result.py`, validate as `challenge-resolve`, promote only validated artifacts. Include confidence evidence, gaps, recovery, residual limits. Default validation requires the action contract for candidate and final results; older artifacts lacking it can be read as data but cannot be validated under the current contract. Shared validation reruns action and evidence validation as well as binding the computed decision and visible output. Existing native or App Server evidence retains its actual trust level; neither becomes cryptographic proof of source correctness. The host has no table-delivery receipt: report the once-per-round chat timing as an instruction-level limit, not a machine-verified guarantee.

## Fail-Fast Rules

- Missing scope, specification, source, or required independent route prevents clean completion.
- Shared hard stops take precedence over improvement and passing tests; a structural fix needs explicit authority only when it expands scope or crosses another approval boundary.
- A malformed ledger, missing report, snapshot mismatch, or unverified closure blocks acceptance. Preserve the failed evidence and give the specific recovery, not a bare unresolved status.
- A clean loop does not waive caller completion checks, authorization, or recurrence rules.

## Quality Gates

Use `../../shared/quality-gates.md`. Required review evidence includes the validated ledger, original independent reports, current snapshot binding, finding dispositions, caller completion when applicable. No finding disappears merely because it was not selected or a check became green.

## Calibration Hooks

Cover clean review, pending fixes, repeated signatures with improving score, feasible structural fixes, authority stops, score boundaries, round cap, source changes after review, unavailable independent coverage, table timing, severe-finding escalation, reviewer budget fidelity and self-contained reply topic. Exercise both recovery and truthful failed handoffs; never weaken independent coverage to pass calibration.

## Output Contract

Follow `../../shared/final-handoff-contract.md`; after validation and promotion emit `final.md` verbatim. Use `result-template.json` for schema-v2 metadata.

### Final chat

- `Outcome`: identify the reviewed topic and `clean` or the specific stop reason.
- `Results`: exactly `Iteration | Open findings | Weighted score | Decision | Evidence`, one row per independent round. Use one-based index, counts in `security=0, critical=0, high=0, medium=0, low=0, nit=0` order with actual values, computed score, exact checker decision, and the retained report path. For no independent round use `not-run | Not assessed | N/A | independence-unavailable | loop-report.md`.
- During execution, show the cumulative `--progress` table once after each newly completed validated round, before fixes or the next review. This in-turn transcript is separate from the canonical final `Results` table.
- With no validated round, report the missing evidence in prose, not a `not-run` / `N/A` progress table. A status update without a new round does not repeat the table; never invent a clean row or omit a table due after an actual round.
- Show the score series and each remaining finding's disposition, reason, owner, and next action. Report actual implemented fixes separately from evidence-only closure; no code fixes means say so. The common result counts fold security into critical and nit into low; the ledger retains all six tiers.
- `Next steps`: recommended recovery and relevant alternatives on stops; ask only for the missing decision.
- Confidence includes independence/provenance and execution limits, not just a number. Artifact: `.reports/codex/challenge-resolve/<timestamp>/result.json`.
