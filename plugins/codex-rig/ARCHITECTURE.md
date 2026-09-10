# Codex Rig Parallel Execution Architecture

## Purpose and status

This document is maintainer-facing architecture contract for bounded multi-agent execution in Codex Rig. It explains what may run concurrently, which records make execution verifiable, where synchronization is mandatory, and which claims remain deliberately unavailable. The host schedules agents; plugin validates evidence and consumers retain final acceptance.

The current generic runtime promotion tier is `portable-read-restricted`. New generic runtime promotion requires schema-v2 evidence, non-sensitive task, read-only nodes, restricted network mode, approval policy `never`, passed context/output common-secret scanning, and explicitly unverified filesystem credential isolation. Code Review also has separate instruction-bounded native inspection route: reviewers receive full role card first, then scope inventory and relevant source/diff/evidence as untrusted input, return text only, and are instructed not to use child tools or repository execution; runtime detects and rejects violations without claiming isolation. This route is review evidence, not generic runtime promotion. The generic resolver's parallel-write mode and stronger `host-isolated` tier remain disabled; code-remediate has separate local production lifecycle with its own schema-v2, approval, source-application, and containment contract below.

## Maintainer navigation

- [Shared orchestration policy](shared/specialist-orchestration.md) defines routing, ownership, fallback, handoff, and retry rules.
- [Execution validator](shared/parallel_execution.py) defines executable schema, digest checks, stage barriers, controls, joins, overlap derivation, closed consumer preflight, exact parent-write approval, and post-join runtime binding.
- [Generated worktree lifecycle](shared/parallel_worktrees.py) implements approval-bound generated-fixture proof and separate code-remediate-local schema-v2 lifecycle; its thin argparse sequence is `prepare`, `create-handover`, `join`, `collect`, `integrate`, `apply-source`, and `cleanup`. It is not scheduler, registry, or generic write resolver route.
- [Telemetry helper](shared/parallel_telemetry.py) defines privacy-minimized timing, token accounting, workload matching, and comparison metrics.
- [Code-review contract](skills/code-review/SKILL.md) is reference consumer for specialist manifests and runtime evidence.
- [Implement contract](skills/implement/SKILL.md) declares promoted portable read-only evidence route and parent-serial mutation boundary.
- [Manage contract](skills/manage/SKILL.md) declares promoted portable read-only inventory route and parent-serial mutation boundary.
- [Plugin README](README.md) gives user-facing activation, review, calibration, and rollback boundaries.

## Architectural invariants

1. A plan is frozen before dispatch. Dispatch cannot add nodes, broaden ownership, change dependencies, or change integration baseline.
2. A node is eligible only when its context, role card, output path, ownership, resource locks, controls, and checks are explicit and hash-bound.
3. A downstream stage waits for every dependency node to join. A child response alone never proves completion or acceptance.
4. `parallel` is observed runtime result, not requested mode or planning label. It requires at least two substantive validated child intervals that overlap.
5. Parent reconciliation remains serial and deterministic even when child work overlaps. Parent owns conflicts, final severity, integration, and user-facing conclusions.
6. An unavailable or unsafe parallel route uses same plan and quality gates serially and is recorded as `serial-fallback`; it is not silently called parallel.
7. No environment variable, execution flag, child request, or declared control grants generic write authority. The only accepted write lifecycle is code-remediate-local, where exact schema-v2 consumer plan and approval bind source baseline, buckets, and lifecycle evidence; generic `write_parallel_promoted` remains `false`.
8. Evidence is minimized by audience. Private validation artifacts may retain runtime IDs needed to bind authoritative rollouts; consumer summaries omit raw prompts, messages, credentials, and environments; telemetry HMACs runtime IDs and retains only counters, timing, hashes, enums, and bounded projections.

## Execution request and activation

The shared resolver accepts `--execution=serial`, `--execution=parallel-read`, `--execution=parallel-write`, or `--execution=auto`. Resolution precedence is:

1. Explicit per-invocation `--execution=...`.
2. `CODEX_RIG_EXECUTION`.
3. The shipped default.

The shipped default is `auto`. `auto` selects only already-promoted read-only consumer route and otherwise resolves safely to `serial`. An explicit unpromoted mode fails closed. `parallel-write` remains unavailable and never inherits authorization from `auto`, environment, or read-only approval.

The resolver is request parser and safety gate, not scheduler. `auto` returns concrete effective mode `parallel-read` only when closed consumer boundary derives promoted read route; otherwise it returns `serial`. Each skill must still declare its safe parallel surfaces, barriers, ownership, resources, and consumer checks. Until skill adopts shared runtime contract, resolver value is not universal CLI capability for that skill.

## Portable read-only consumer declarations

Implement and Manage carry explicit portable read-only declarations instead of shared consumer registry or another module. Each declaration names only its non-sensitive read-only route, exact frozen consumer policy, one fixed dependency-ready wave, complete terminal join, serial parent decisions and mutations, validated resource locks, equal-gate `serial-fallback`, acceptance evidence, and fail-closed stop conditions. The frozen policy must bind `consumer_id`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial` to plan digest. The same plan binds `write_policy`: any planned parent mutation requires separate approval record containing only exact plan SHA-256, `response=approve`, and `source=explicit-input|user-prompt`.

Before dispatch, each consumer invokes `parallel_execution.py preflight --consumer <implement|manage>`; command derives promotion from closed allowlist, applies flag/environment/default precedence, validates exact consumer and write policies, and validates required approval without granting parallel-write authority. After terminal join, each invokes `validate-runtime --consumer <implement|manage>` with authoritative runtime paths, then repeats identical preflight before first parent mutation. Generic validation without consumer id remains readable but is explicitly promotion-ineligible. Code Review now binds `consumer_id=code-review` through its existing artifact validator instead of relying on unbound generic evidence.

The declarations enable only validated portable read-only work. Implement keeps source, test, documentation, configuration, calibration, artifact, result, integration, gate, verdict, and promotion work parent-serial. Manage keeps create, update, delete, rename, permission, policy, configuration, documentation, calibration, propagation, artifact, result, gate, verdict, and promotion work parent-serial. Canonical quality gates also remain serial because no isolated resource-compatible gate group has executable adoption evidence.

The code-remediate-local lifecycle and Implement/Manage portable read-only routes are separately promoted through their executable acceptance matrices. Generic parallel writes remain disabled, and no flag, environment value, or natural-language request can activate unpromoted consumer or bypass its plan binding.

## Eligibility and capability tiers

### Portable read-only tier

`portable-read-restricted` is only runtime promotion tier currently available. It accepts only non-sensitive work and requires frozen parent plan to carry `capability_policy.task_sensitivity=non-sensitive`. The v2 manifest must carry portable capability record with restricted network mode, approval policy `never`, no external events, passed context scan, and `filesystem_isolation=unverified`.

Persisted v2 node controls use literal observed values: `sandbox_mode=read-only`, `write_paths=[]`, `network=restricted`, and `credentials=unverified`. The validator may create internal schema-v1-compatible projection to reuse structural checks, but it never promotes legacy `network=false` or `credentials=false` values as runtime evidence.

The tier proves recorded configuration and observed rollout binding required by contract. It does not prove global network denial, universal command inspection, credential isolation, filesystem isolation, or behavior outside retained event records. A response item representing external network, browser, connector, MCP, search, or web capability fails closed.

### Host-isolated tier

`host-isolated` is reserved for future authoritative host evidence. The current validator rejects it with unavailable-evidence failure. Do not infer this tier from read-only sandbox label, restricted network metadata, empty tool list, or child assertion.

### Write tier

Generic write-capable parallel execution is not promoted: v2 manifest for shared resolver containing write node is rejected before structural acceptance. Code-remediate-local production remediation is separately promoted consumer-owned lifecycle, not generic runtime promotion. Its exact schema-v2 plan and approval bind clean authoritative source repository, baseline `HEAD` and tree, two to four disjoint buckets, actual context-pack paths and SHA-256 values, resource locks, detached worktrees under only external sibling root `.codex-rig-worktrees/<run-id>`, fixed new state basename and output names under run root, fixed `code-remediate-shared-quality-gates` verification reference, rollback policy, and non-force cleanup policy. Plan, approval, state, patch, rollback, and lifecycle artifacts remain in authoritative repository's normal `.reports/codex/code-remediate/...` run directory. The lifecycle's completed evidence is hash-bound into remediation result; it does not change generic resolver's `write_parallel_promoted` value or grant authority to another consumer.

## Frozen execution plan

Parent writes `execution-plan.json`, role-specific context packs, and `freeze-record.json` before dispatch. These artifacts separate intent from immutable evidence:

- `execution-plan.json` records run identity, requested mode, capability classification, one predeclared wave, concurrency limit, nodes, ownership/locks, and acceptance requirements.
- `freeze-record.json` records exact plan, context-pack, and role-card SHA-256 values before first spawn. Role-card hashes bind nodes to installed `roles/<role>/ROLE.md` bytes.
- `execution-manifest.json` is constructed after terminal joins. It binds plan digest, capability evidence, structural DAG, attempts, output hashes, observed controls, host lineage, joins, and claimed mode for executable validator.

A skill with multiple static stages must freeze every stage and dependency before dispatch, but current portable pilot plan uses one bounded wave. No later manifest may add node, dependency, owner, lock, or acceptance requirement absent from frozen intent. Paths are relative to run directory, normalized across POSIX and Windows forms, and rejected when absolute, traversing, aliased, duplicated, or pattern-bearing.

Neutral frozen plan:

```json
{
  "schema_version": 2,
  "run_id": "run-example-001",
  "capability_policy": {
    "tier": "portable",
    "task_sensitivity": "non-sensitive"
  },
  "consumer_policy": {
    "consumer_id": "implement",
    "capability": "portable-read-only",
    "promotion_status": "promoted",
    "parent_mutations": "serial",
    "canonical_gates": "serial"
  },
  "write_policy": {
    "parent_writes": "none",
    "approval_requirement": "not-required"
  },
  "requested_mode": "parallel-read",
  "wave": {
    "wave_id": "wave-001",
    "configured_limit": 2,
    "nodes": [
      {
        "node_id": "qa",
        "role_id": "qa-specialist",
        "context_path": "qa-context.md",
        "mutation": "read-only",
        "owned_paths": [],
        "resource_locks": []
      },
      {
        "node_id": "challenge",
        "role_id": "challenger",
        "context_path": "challenger-context.md",
        "mutation": "read-only",
        "owned_paths": [],
        "resource_locks": []
      }
    ]
  },
  "acceptance": {
    "required_evidence_level": "portable-read-restricted",
    "required_actual_mode": "parallel",
    "write_parallel_eligible": false,
    "requires_terminal_join": true,
    "requires_source_backed_output": true
  }
}
```

Neutral pre-dispatch freeze record:

```json
{
  "status": "frozen-before-dispatch",
  "run_id": "run-example-001",
  "execution_plan_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "contexts": {
    "qa": {
      "path": "qa-context.md",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "role_card_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    },
    "challenge": {
      "path": "challenger-context.md",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "role_card_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    }
  },
  "dispatch_status": "not-started",
  "approval_status": "required-when-external"
}
```

After writing plan bytes, parent computes their SHA-256 and stores it in freeze record, later execution manifest, and any write-approval record. The digest cannot be embedded in plan it hashes. Frozen inputs remain unchanged after dispatch; terminal state belongs in separate run record rather than rewriting pre-dispatch evidence.

## DAG, waves, ownership, and resources

Manifest stages form directed acyclic graph. Dependencies must name existing stages, cycles fail closed, and validator computes deterministic lexical topological order. Every stage must declare distinct wave ID and non-empty node list in real manifest. A stage barrier requires every node in preceding topological stage to have joined before any dependent node starts.

Nodes in one wave may run concurrently only when they are independent. Read-only nodes have no owned write paths. Future write nodes must own disjoint normalized paths; equal paths and ancestor/descendant paths conflict. Resource locks use small validated vocabulary `git-index`, `database:<name>`, `port:<number>`, `gpu:<id>`, `cache:<path>`, `generated:<path>`, and `test-env:<name>`. Conflicting locks block wave even when paths differ.

The concurrency ceiling is minimum of configured limit, ready independent nodes, and available resources; default ceiling is four. Parent may overlap only declared read-only or separately owned work and must not mutate bucket path, dependency, resource, or integration baseline while children run.

## Approval allowlist and two-phase promotion

Parallel scheduling never expands authority already granted to parent task. Every write, whether serial or parallel, requires frozen plan and exact digest-bound approval. A read-only child wave inside authorized workflow does not need second write approval, but starting paid or externally networked parent process remains separate external-capability action and follows shared five-field approval contract. A prior approval applies only to its stated command boundary and retry policy; it cannot authorize later paid retry, write wave, external service, broader path, or different plan digest.

| Action | Accepted authority | Never sufficient |
| -- | -- | -- |
| Local non-sensitive read-only fan-out | Existing authorized workflow plus schema-v2 portable policy, observed `read-only`, network `restricted`, and approval `never` | Requested mode, child assertion, missing approval data, or prior unrelated approval |
| Paid/external parent invocation | Explicit user approval after five-field capability brief; one attempt unless brief authorizes bounded retry | Local fan-out eligibility, saved CLI prefix alone, or exhausted earlier approval |
| External network/browser/MCP/connector/app use inside portable wave | None; portable execution forbids it and rollout validator fails closed on such events | Restricted network metadata, empty output, or approval policy `never` |
| Code-remediate-local production remediation | Exact schema-v2 consumer plan plus `approve` from `explicit-input` or `user-prompt`, bound to frozen plan digest and source baseline | Generic `parallel-write`, `auto`, environment variables, read approval, child requests, requested controls, schema-v1 planning evidence, or different consumer |
| Future generic parallel write wave | Explicit `approve` from `explicit-input` or `user-prompt`, bound to exact frozen `plan_sha256`, after generic write-tier promotion | `auto`, environment variables, read approval, paid-call approval, child requests, or requested controls |
| Public API, security, data-deletion, or schema-migration decision | The repository's separate human-in-the-loop policy in addition to execution authority | Any execution mode or plan approval |

Runtime approval prefixes are convenience routing rules, not product authority. The owning workflow must still present required capability brief, preserve one-attempt or retry boundary, and stop safely when approval is denied or exhausted. The manifest records portable policy and host binding so consumer cannot mistake missing evidence for approval.

Write approval is separate and applies to every write. The allowlist is exactly `response=approve`, `source=explicit-input` or `source=user-prompt`, and `plan_sha256` equal to frozen plan digest. A flag, environment value, child request, `auto`, or read-only result never substitutes for this record.

Promotion is therefore two-phase:

1. Read phase: freeze read-only plan, validate node provenance and controls, dispatch one wave, bind authoritative parent/child rollout evidence, join every child, derive actual mode, and promote only portable-read-restricted result.
2. Code-remediate write phase: promoted consumer freezes digest-bound write plan, establishes path/resource/worktree isolation, obtains exact write approval, validates patch-only outputs and verification, then integrates serially in deterministic order. The generic runtime still stops before generic parallel writes; documenting this separate lifecycle does not enable generic write route.

## Canonical G0–G8 execution flow

This is single versioned gate taxonomy for bounded multi-agent execution. G0 through G8 are parent-owned synchronization gates; host may schedule approved read-only wave, but only parent can integrate, verify, issue verdict, or promote evidence. The Code Review instruction-bounded native inspection route is review-only exception to strict child-control admission: it supplies complete role card first, then scope inventory and relevant evidence inline, returns text only, detects prohibited execution, and does not claim sandbox or approval isolation. A route that cannot satisfy gate stops or uses explicitly recorded equal-gate serial fallback.

```text
                             ┌─────┬──────────────────────┐
                             │ G0  │ Authority and intake │
                             └─────┴───────┬──────────────┘
                                           ▼
                              ┌─────┬────────────────────┐
                              │ G1  │ Evidence complete? │
                              └─────┴──────┬─────────────┘
                              ┌────────────┴────────┐
                        ✓ YES │                     │ ✗ NO
                              ▼                     ▼
                ┌─────┬──────────────────────┐  ┌──────────┐
                │ G2  │ Freeze route + plan  │  │   STOP   │
                └─────┴───────┬──────────────┘  └──────────┘
                              └────────────┐
                                           ▼
                              ┌─────┬────────────────────┐
                              │ G3  │ Writes requested?  │
                              └─────┴──────┬─────────────┘
                              ┌────────────┴────────────────────────┐
                         ✗ NO │                                     │ ✓ YES
                              ▼                                     ▼
                    ┌────────────────────┐              ┌────────────────────────┐
                    │    Read route?     │              │  Promoted + approved?  │
                    └─────────┬──────────┘              └───────────┬────────────┘
                  ┌───────────┴───────┐                    ┌────────┴────────┐
                ✗ NO                ✓ YES                ✓ YES             ✗ NO
                  ▼                   ▼                    ▼                 ▼
          ┌─────┬──────────┐  ┌─────┬──────────┐  ┌─────┬────────────┐  ┌──────────┐
          │ G4  │  Serial  │  │ G4  │   Read   │  │ G4  │ Write wave │  │   STOP   │
          └─────┴─┬────────┘  └─────┴─┬────────┘  └─────┴───┬────────┘  └──────────┘
                  └───────────────────┼─────────────────────┘
                                      ▼
                      ┌─────┬────────────────────────┐
                      │ G5  │ Terminal, join, derive │
                      └─────┴─────────┬──────────────┘
                                      ▼
                       ┌─────┬──────────────────────┐
                       │ G5a │   Nodes terminal?    │
                       └─────┴────────┬─────────────┘
                        ┌─────────────┴──────────────┐
                      ✓ YES                        ✗ NO
                        ▼                            ▼
          ┌─────┬──────────────────────┐  ┌──────────────────────┐
          │ G5b │ Join evidence valid? │  │  Preserve + re-plan  │
          └─────┴───────┬──────────────┘  └──────────────────────┘
              ┌─────────┴────────────┐
            ✓ YES                  ✗ NO
              ▼                      ▼
┌─────┬──────────────────────┐  ┌──────────┐
│ G5c │ Derive observed mode │  │   FAIL   │
└─────┴───────┬──────────────┘  └──────────┘
              └───────────────────────┐
                                      ▼
                       ┌─────┬──────────────────────┐
                       │ G6  │  Integrate serially  │
                       └─────┴────────┬─────────────┘
                                      ▼
                       ┌────────────────────────────┐
                       │     Conflict or drift?     │
                       └──────────────┬─────────────┘
                        ┌─────────────┴──────────────┐
                      ✗ NO                         ✓ YES
                        ▼                            ▼
          ┌─────┬──────────────────────┐  ┌──────────────────────┐
          │ G7  │     Gates pass?      │  │  Preserve + re-plan  │
          └─────┴───────┬──────────────┘  └──────────────────────┘
                 ┌──────┴───────────────┐
             ✓ YES                   ✗ NO
                 ▼                      ▼
┌─────┬──────────────────────────┐ ┌──────────┐
│ G8  │ ACCEPT and retain proof  │ │   FAIL   │
└─────┴──────────────────────────┘ └──────────┘
```

The flow's endpoint meanings are fixed: `STOP` means no safe continuation, `re-plan` means parent must freeze changed scope or baseline again, `FAIL` means current result cannot satisfy acceptance, and `ACCEPT` means parent may publish only supported evidence and promotion state. Every question in chart has explicit `✓ YES` and `✗ NO` branches; connector lines and `▼` carry direction, and approval question's `✓ YES` branch is itself validated before dispatch.

- **G0 — Authority and intake**: Normalize request, scope, run identity, permitted mutations, sensitivity, and execution request. Apply precedence `--execution`, then `CODEX_RIG_EXECUTION`, then shipped default. `auto` selects only already-promoted portable read-only consumer route; otherwise it resolves safely to `serial`. Generic parallel writes remain disabled, and `auto` never bypasses consumer promotion or write approval.
- **G1 — Evidence ready**: Collect and persist shared baseline, capability evidence, source-backed context, and required host or rollout records before planning dispatch. Missing, mutable, sensitive, or unverifiable evidence fails closed for strict portable runtime; child assertion or requested control is not evidence. For Code Review, source inspection continues whenever changed source is accessible even when reviewer provenance or optional specialist coverage is unavailable; unavailable source identity remains hard stop.
- **G2 — Route, packs, plan, and digest freeze**: Freeze DAG, one dependency-ready wave, role-specific context packs, owned paths, resource locks, checks, controls, baseline, role-card hashes, and exact plan digest before any spawn. Dispatch cannot add nodes, broaden ownership, change dependencies, or change integration baseline.
- **G3 — Required approvals**: Obtain only approvals required by selected capability and external boundary. The instruction-bounded Code Review inspection route requires no child execution approval; parent handles any safe authorized probe separately. Every write requires frozen plan and exact digest-bound approval with `response=approve` and allowed human source. A missing, stale, broader, or mismatched approval stops affected action.
- **G4 — Dependency-ready dispatch**: Dispatch only fixed ready wave up to validated resource and concurrency limit, recording parent and child identities, route, context hashes, controls, and start events. If wave is unsafe or unavailable, execute same frozen plan serially as `serial-fallback` with equal gates; never add later wave dynamically.
- **G5 — Terminal, join, and derivation**: The parent waits for complete wave before any dependent work, integration, or acceptance. The three G5 sub-gates preserve distinct evidence boundaries.
  - **G5a — Real terminal state**: Require real terminal event for every required node. `cancel_requested` is not terminal; failed, cancelled, missing, or ambiguous terminal evidence blocks acceptance and preserves available evidence for safe recovery.
  - **G5b — Complete join contracts**: Validate every required handoff, output or patch hash, changed path, ownership claim, resource result, verifier status, unresolved item, and parent result-consumption event. A child response alone never proves completion or acceptance.
  - **G5c — Observed execution derivation**: Derive `parallel`, `independent-spawned`, `serial`, or `serial-fallback` from validated substantive child intervals and fixed plan. `parallel` requires at least two validated intervals that overlap; requested mode or spawn count is never sufficient.
- **G6 — Parent-only deterministic integration**: Freeze joined result, then reconcile in topological stage order and stable node order using parent workspace. Conflicts, changed scope, or baseline drift stop integration and require new frozen plan; partial writes are never integrated automatically.
- **G7 — Integration-wide verification**: Run canonical integration-wide checks against frozen integrated result and retain their exact outcomes. Quality gates remain serial unless separately adopted contract proves isolation and equal evidence; any failed or missing gate produces `FAIL`.
- **G8 — Parent verdict, evidence, and promotion**: Parent owns final severity, confidence, residual limits, sanitized artifact retention, user-facing output, and any promotion decision. Publish only evidence level supported by validator; successful completion reaches `ACCEPT`, while unsupported or unresolved evidence remains failed or unavailable. A missing independent QA/challenger pass may leave completion withheld while accessible source inspection and available findings remain reported.

No dependent node starts before its dependency join. No parent acceptance occurs before every required node joins. A user cancellation stops new dispatch, preserves terminal evidence and worktrees, and never integrates partial writes automatically.

## Schema-v2 manifest and authoritative rollout binding

Schema-v1 remains readable for historical structural validation. New runtime promotion requires schema-v2. The v2 capability record has exact shape below:

```json
{
  "tier": "portable",
  "task_sensitivity": "non-sensitive",
  "network": {
    "mode": "restricted",
    "approval_policy": "never",
    "external_events": []
  },
  "credentials": {
    "context_scan": "passed",
    "filesystem_isolation": "unverified"
  }
}
```

The runtime validator binds manifest bytes to recorded manifest digest and plan bytes to `plan_sha256`. It then binds each node to unique parent spawn call, parent-observed start activity, child session metadata, child terminal event, exact output, and delivery to authoritative parent collaboration path. It verifies role model/effort against installed role card and retains only safe projections and hashes in result.

Current child terminal endpoints are whole-second values while `duration_ms` retains sub-second precision. The validator therefore requires positive endpoints and duration plus strict residual below one second; residual of one second or more fails closed. Parent delivery likewise requires response author to match child, recipient to equal authoritative parent path, envelope and output bytes to match, and delivery timestamp to follow child completion.

The current rollout event shape and timestamp units are observed implementation details, not platform guarantee. Missing, ambiguous, drifted, or externally capable response records fail closed. A successful result reports literal `evidence_level=portable-read-restricted`, `network_mode=restricted`, `approval_policy=never`, `filesystem_credential_isolation=unverified`, and `write_parallel_eligible=false`; it does not report `network=false`, `credentials=false`, global network guarantee, universal command inspection, or host-wide filesystem/credential isolation.

## Truthful execution labels and fallback

The structural validator derives `parallel` only when two or more substantive completed nodes in same wave have strictly overlapping start/terminal intervals and validated outputs. Spawn overlap, teardown overlap, empty output, requested-only controls, and unjoined responses do not qualify. Multiple completed children without overlap are `independent-spawned`; one child is `serial`.

`serial-fallback` is valid only when same frozen plan and quality gates were attempted after parallel dispatch was unavailable or unsafe, and observed intervals do not overlap. A fallback is not second fan-out wave. If new work, changed ownership, changed dependencies, or changed approvals are needed, stop, preserve evidence, and create new plan.

## Retry, replan, cancellation, and stop rules

- Allow at most one retry per node, and only for `timeout`, `transport_error`, or `rate_limited` outcomes. The first attempt must be failed and output-free; second attempt starts after its terminal event.
- Do not automatically retry deterministic findings, validation failures, conflicts, ownership overlap, baseline drift, malformed evidence, or completed output.
- Preserve all attempts and diagnostic evidence. Never replace completed output with checkpoint or invent missing provenance.
- On partial failure, block dependents and parent acceptance; preserve successful results and re-plan explicitly if recovery changes graph or plan digest.
- On cancellation, stop new dispatch, allow configured grace period, retain `cancel_requested` until real terminal event, and never integrate partial writes automatically.
- If required gate remains unmet after permitted recovery, stop and return exact failure category and owner/action rather than weakening evidence claim.

## Telemetry and matched-workload analysis

[`shared/parallel_telemetry.py`](shared/parallel_telemetry.py) accepts sanitized rollout rows and emits compact attempt and wave records. It validates cumulative token counters, reconstructs deltas when terminal totals are unavailable, records task timing when present, and HMACs runtime identifiers. It never reads or stores prompts, reasoning, tool arguments, paths, credentials, full environments, raw child messages, or provider prices.

Before wave dispatches, parent calls `admit_wave_token_budget` with frozen positive wave ceiling, stable node order, and positive per-node token reservations. Completed and active reservations must form stable prefix and remain charged; admission stops at first reservation that would exceed ceiling, and that node plus every later unstarted node move to same-gate serial re-planning. Active children are not terminated merely because admission budget is exhausted. Schema-v2 runtime acceptance reads `token_budgets` from exact digest-bound plan and rejects any retained wave whose spawned nodes, wave identity, lexical node order, or reservations were not fully admitted. Earlier schema-v2 evidence without token budgets remains readable only through explicit `historical_unbudgeted=True` path, which returns `acceptance_blocked=true` and `runtime_promotion_eligible=false`; default acceptance path still fails closed. This is hard boundary on pre-dispatch reservations, not provider-enforced cap on actual child consumption: current host exposes no enforceable per-child token limit, so retained telemetry reports `actual_over_budget_tokens` instead of claiming actual usage was capped.

Wave telemetry records `dispatch_to_final_join` wall time, token counters, mode, attempt count, SHA-256 digest of normalized workload key, unavailable fields, and child-duration maximum as diagnostic proxy. The child-duration maximum is not wall-time envelope and cannot support savings claim.

`build_retained_wave_evidence` accepts only exact compact wave schema and rejects unknown fields. It replaces raw wave ID with HMAC, emits durable proof digest and bounded counters/status, records admission ceiling, reservations, and actual overrun, and never projects prompts, messages, reasoning, tool data, paths, environments, credentials, or raw runtime identifiers. Its storage consumer, `enforce_diagnostic_expiry`, appends path-free JSONL evidence to fixed `expiry-audit.jsonl` before eligible deletion and after each outcome. Sanitized diagnostics expire 30 days after success or resolution; unresolved failed, cancelled, or conflicted work remains until resolution. Only exact HMAC diagnostic named by retained record is eligible for deletion.

`compare_parallel_to_serial` reports wall-time savings, speedup, and token multiplier only when serial and parallel waves have same workload-key digest and both use `dispatch_to_final_join` as their wall-time source. Missing or mismatched baselines return null metrics with explicit unavailable fields. A comparison is not price estimate and does not by itself promote execution safety.

Neutral telemetry shape:

```json
{
  "schema_version": 1,
  "wave_id": "wave-example",
  "mode": "parallel",
  "attempt_count": 2,
  "workload_key_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
  "wall_time_ms": 4200,
  "wall_time_source": "dispatch_to_final_join",
  "child_duration_proxy_ms": 3900,
  "input_tokens": 1200,
  "output_tokens": 800,
  "reasoning_output_tokens": 300,
  "cached_input_tokens": 0,
  "cache_write_input_tokens": 0,
  "total_tokens": 2000,
  "unavailable_fields": []
}
```

## Consumer integration

### Generated-fixture proof and code-remediate-local production lifecycle

The generated-fixture proof is deliberately narrower than write promotion and treats active parent session as operational authority. `shared/parallel_worktrees.py` accepts exact approved plan for one generated repository, freezes exactly two disjoint work packages at one clean source `HEAD` and tree, and creates separate detached child worktrees. The parent dispatches both subagents before waiting. Each completed child uses `create_completed_child_handover` to return one bounded report with its node ID, terminal status, concise summary, exact changed paths, and canonical patch SHA-256. The helper reads raw Git subprocess bytes through same lifecycle implementation as parent join; shell-rendered `git diff` output is never digest source.

The parent joins both reports at one barrier. `join_child_handovers` requires both statuses to be `completed`, checks exact report schema, and compares every reported path and patch digest with actual detached worktree before persisting join. The lifecycle strips inherited `GIT_*` redirection overrides, rederives plan, approval, repository, worktree, node, output, and patch authority before transitions, fingerprints declared retained attempt, and rejects symlinked managed roots, Windows aliases, traversal, case-folded or ancestor ownership collisions, dirty source state, child commits, staged changes, untracked or undeclared paths, deletes, renames, mode/type changes, empty patches, changed authority, and changed source baselines.

After join, parent derives both patches itself, rechecks handover paths and hashes, and applies them in stable node order to separate integration worktree. Successful cleanup begins only after integration record is durable; it restores only generated owned paths, invokes non-force `git worktree remove`, and records command results and absent-path postconditions. Any missing, failed, cancelled, malformed, mismatched, conflicted, drifted, or cleanup-uncertain result retains diagnostics and blocks acceptance.

The lifecycle record is compact operational audit trail under parent authority, not cryptographic proof against hostile post-run rewriting. The generated-fixture proof may claim only that parent froze two generated-fixture packages, dispatched two child sessions to isolated worktrees, joined two completed reports, independently verified resulting Git changes and patches, and integrated them deterministically. It does not prove particular child tool, child authorship, edit-time overlap, host attestation, native-Windows Git lifecycle behavior, production eligibility, or general availability. App Server access, brokers, sidecars, session-store discovery, signed receipts, and full-thread filtering are deliberately outside this boundary.

A recorded failure demonstrates boundary: when one child hashed RTK-rendered `git diff` output, parent rejected mismatch before collecting either patch, persisted bounded join-failure diagnostics, and retained both worktrees. The lifecycle allows no retry of that approved plan. Recovery must use canonical helper, new frozen digest, and separate approval.

The generated-fixture proof leaves generic `write_parallel_promoted=false` and `write_parallel_eligible=false`; its approval or evidence cannot authorize code-remediate, another consumer, or generic resolver, and it does not alter shipped `auto` default.

Code-remediate-local has separately accepted production lifecycle. A schema-v2 plan and explicit approval must bind one clean authoritative source repository and exact `HEAD`/tree, two to four disjoint buckets, actual context-pack paths and SHA-256 values, resource locks, detached worktrees under only `.codex-rig-worktrees/<run-id>` outside authoritative checkout, fixed new state basename and output names under source-local run root, fixed `code-remediate-shared-quality-gates` reference, rollback policy, and non-force cleanup policy. Plan, approval, state, patch, rollback, and lifecycle artifacts stay in authoritative repository's normal `.reports/codex/code-remediate/...` run directory. Preparation and every authority transition re-hash each actual context pack and reject drift. The parent invokes thin argparse sequence `prepare`, `create-handover`, `join`, `collect`, `integrate`, `apply-source`, and `cleanup`; these operations are not scheduler, registry, or global promotion mechanism. The parent prepares worktrees; each child edits only its owned paths, does not commit, and returns canonical terminal status, summary, changed paths, and patch SHA-256. The parent re-derives every patch, joins all terminal handovers, integrates in lexical bucket order in separate integration worktree, and records only Git-structural integration as `structurally-verified`; it does not execute arbitrary plan-provided commands. After source application, existing shared quality-gate phase remains executable result authority and its validated `gates.json` is required for passing remediation result.

The strict production handover also rejects ignored child output, so every frozen child context must name verification commands that leave zero ignored or untracked paths. Use tool-native no-cache/no-output controls for disposable test caches, coverage data, bytecode, lint caches, generated reports, and equivalent artifacts; parent-owned integration gate remains responsible for authoritative coverage and full validation. Before plan digest is frozen, execute exact commands in disposable clean worktree containing planned postimages and require zero exit failures, tracked-postimage drift, ignored output, or untracked output. Only byte-identical preflighted command text may enter context pack; later change requires new digest and approval. Do not clean generated output after child command to make failed handover appear clean. If required child check cannot honor zero-output invariant, classify that bucket as parent-owned or sequential before dispatch.

Source application rechecks exact raw integration postimages, captures raw source preimages, stores durable reverse patch, applies one parent-generated forward source bundle, and verifies every expected Git-content postimage through each worktree's clean filters. Raw source SHA-256 postimages remain recorded for cleanup and evidence, while Git clean-filtered object identities allow LF and CRLF worktree bytes to represent same repository content across native platforms. On failure, known states are restored only after recomputing affected identities and confirming each path is at its recorded preimage or expected postimage; mismatch, filter failure, restore error, or failed recomputation records `rollback-ambiguous`, retains worktrees and evidence, and stops without automatic restoration. Non-force cleanup occurs only after durable source application and exact recorded source postconditions; cleanup or lifecycle failures retain evidence. The schema-v2 lifecycle record and digest are bound into code-remediate result, while schema-v1 bucket plans are planning-only and cannot prove completed execution.

Its containment claim is `parent-authoritative operational postcondition containment` with `capability_sandbox_verified=false`. The artifact validator independently re-hashes every child patch, forward source bundle, and rollback patch beneath exact run root before accepting lifecycle evidence. Source, worktree, evidence-root, state, output, and patch path components reject symlinks and path escapes. This is not per-child capability sandbox, hostile-child security boundary, globally atomic source transaction, or security isolation guarantee. Separately sandboxed processes remain future stronger alternative, not current prerequisite.

The code-remediate-local production route completed full lifecycle and rollback proof. Installed-package acceptance executes same lifecycle suite from manifest-declared payload, and repository's full-test matrix records that gate on Linux, macOS, and native Windows. Promotion is limited to this consumer-owned route. Every later live write still requires newly frozen consumer-specific plan and exact digest approval; neither this matrix nor prior proof authorizes another write.

A strict portable consumer such as Implement or Manage must mirror validated runtime summary into its metadata, preserve exact `execution_mode`, `execution_evidence_level`, and `write_parallel_eligible=false`, and keep parent acceptance in consumer. Code Review's instruction-bounded native inspection route records its own validator-defined review evidence and must not project portable sandbox or approval claims into that summary. Every consumer must run its own artifact validator after shared validator, fail on missing or stale plan/manifest correspondence, and never convert `independent-spawned` or planning label into `parallel`.

The consumer should expose compact wave ledger containing stage, node/role, actual mode, join state, blockers, evidence level, and artifact links. Detailed provenance remains in run directory. A consumer may use serial fallback without changing finding semantics, but it must disclose reduced independence or confidence when independent review was required.

## Failure modes and required responses

| Failure | Required response |
| -- | -- |
| Missing or unsupported schema | Keep historical reads on schema-v1; fail new runtime promotion unless schema-v2 is complete. |
| Sensitive task or missing frozen policy | Do not dispatch portable runtime work; classify or re-plan serially. |
| Network mode, approval, external event, or credential-scan mismatch | Fail closed; do not reinterpret restricted as denial or missing evidence as approval. |
| Host-isolated requested without authoritative proof | Report unavailable; remain on portable boundary or serialize. |
| Duplicate/aliased path, ownership overlap, or resource-lock conflict | Do not dispatch concurrently; preserve plan and re-plan or serialize. |
| Missing, non-positive, mismatched, or already-exceeded token reservation budget | Do not dispatch; correct and re-freeze plan instead of inventing capacity. |
| Next reservation exceeds frozen ceiling | Stop new dispatch at that stable-order boundary; preserve completed work, await terminal evidence from active children, and serially re-plan every unstarted node with same gates. |
| Missing terminal, output, verifier, or parent join | Block node and every dependent; do not claim completion or parallelism. Code Review may still report accessible source inspection and withhold completion when only optional independent coverage is missing. |
| Code Review native inspection provenance or optional specialist route unavailable | Continue accessible source inspection and available static review; record unmet independence/provenance requirement and withhold completion only when that requirement is mandatory. Do not relabel parent-serial work as independent. |
| Unsafe or uncertain executable probe | Pause only probe, preserve hypothesis and evidence, and continue static source inspection; resume after separately authorized safe probe or report human-owned alternative. |
| False overlap or serial-fallback claim | Reject manifest and preserve observed intervals for diagnosis. |
| Transient child failure | Use at most one permitted retry; otherwise block acceptance. |
| Deterministic failure, conflict, drift, or changed work | Stop automatic recovery; create new frozen plan. |
| Required child verification emits ignored or untracked output | Preserve failed worktree, correct context to use tool-native no-output controls, and create new frozen plan; if suppression is unavailable, run bucket parent-owned or sequential. |
| Exact child verification command fails preflight or changes afterward | Do not hash or approve plan; correct and rerun disposable-worktree preflight, then freeze only passing byte-identical command text. |
| Cancellation without real terminal event | Retain `cancel_requested`, block joins, and wait or hand off as unresolved. |
| Telemetry baseline mismatch | Report unavailable savings/multiplier metrics; do not infer them from child durations. |
| Retained telemetry contains unknown/raw fields or invalid lifecycle time | Reject compact proof; preserve source diagnostic under its existing restricted policy until valid projection exists. |
| Mutable or malformed third-party workflow action reference, or missing/mismatched readable version comment | Fail repository pin gate; resolve owning upstream ref during review, pin workflow to full commit SHA, and retain reviewed major version in adjacent comment before merge. |

## Worked runtime result

The following is neutral compact consumer projection after valid read-only wave. Private validator evidence may retain raw thread IDs needed for rollout binding, while consumer and telemetry projections remove or pseudonymize them. The example deliberately omits raw prompts, messages, paths outside safe relative artifact links, and host-wide claims:

```json
{
  "actual_mode": "parallel",
  "evidence_level": "portable-read-restricted",
  "network_mode": "restricted",
  "approval_policy": "never",
  "filesystem_credential_isolation": "unverified",
  "write_parallel_eligible": false,
  "manifest_sha256": "1111111111111111111111111111111111111111111111111111111111111111",
  "runtime_nodes": [
    {
      "node_id": "qa",
      "child_rollout_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
      "parent_join": {"event_id": "join-qa", "recipient": "/root", "message_sha256": "3333333333333333333333333333333333333333333333333333333333333333"}
    },
    {
      "node_id": "challenge",
      "child_rollout_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
      "parent_join": {"event_id": "join-challenge", "recipient": "/root", "message_sha256": "5555555555555555555555555555555555555555555555555555555555555555"}
    }
  ]
}
```

This result supports narrow execution claim only after shared validator has inspected retained authoritative rollout records. If those records are absent, stale, ambiguous, or inconsistent with frozen plan, consumer must report unavailable or serial evidence instead.

## Rollback and future extension

Rollback disables per-skill parallel opt-in and runs same versioned work through `serial-fallback` or ordinary serial execution. Keep schemas, artifacts, joins, and quality gates intact; never downgrade new evidence into legacy parallel claim or discard failed/conflicted worktrees automatically.

1. Disable the affected skill's parallel opt-in without changing the frozen plan or its digest.
2. Preserve completed outputs, terminal child evidence, parent joins, and the original quality gates.
3. Serially execute only unfinished work; never replay completed nodes.
4. Retain failed or conflicted worktrees and stop when cleanup or repository state is ambiguous.

Future extension points are deliberately bounded: host-isolated tier may be added only with authoritative complete-path evidence; write promotion may be added only with separate approved isolation and integration lifecycle; and new execution fields require versioned migration with historical readers retained. No registry or provider-specific scheduler belongs in this architecture.
