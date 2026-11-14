## Parallel Adoption (Portable read-only)

<!-- policy-sibling: skills/manage/SKILL.md (Parallel Adoption section) — near-duplicate, invariant text in shared/portable-parallel-read.md; check siblings before editing this section alone -->

> Only when Execution Route Selection resolves to `parallel-read`, read [portable-parallel-read.md](../../../shared/portable-parallel-read.md) before any child preparation; `serial`, `auto`, and native routes do not load it. The reference preserves the shared route gate, write-policy and approval rules, host-compatibility barrier, preflight and runtime-validation qualifiers, resource-lock declaration, fallback, acceptance, and stop rule; the consumer values below stay authoritative for this skill.

This portable section permits only its promoted portable read-only route; its restrictions do not govern the separate Native Bounded Delegation route.

Follow [portable-parallel-read.md](../../../shared/portable-parallel-read.md) for shared gate order and fork outcomes; the [G0–G8 execution diagram](../../../assets/images/parallel-execution-architecture.svg) is illustrative only. This consumer's read-only evidence passes are bounded by G0–G5; parent owns deterministic G6 integration, G7 verification, and G8 verdict/promotion.

### Safe parallel work

The promoted route permits read-only evidence, acceptance, and documentation-impact passes with immutable disjoint context packs and separate outputs. Source, test, documentation, configuration, calibration, cache, generated-output, artifact, and result writes are outside this portable route.

### Required barrier

Before any dispatch, freeze goal, mode, `done_when`, baseline, ownership DAG, context packs, role-card hashes, checks, resource locks, and plan digest. Dispatch at most one fixed dependency-ready wave, then join every terminal handoff before implementation, integration, gates, or acceptance; changed scope requires new plan.

The frozen `<run-directory>/execution-plan.json` must include exact `consumer_policy` values `consumer_id=implement`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial`.

The plan must also freeze schema-3 `dispatch_choices` for each node as specified in [portable-parallel-read.md](../../../shared/portable-parallel-read.md#required-barrier); omitted spawn settings are represented by `null` and inherit observed parent controls.

Bootstrap exception: run-directory creation (Step 01), baseline diff/branch persistence (Step 02), and the `write-approval.json` write itself are exempt from requiring a prior approved plan digest — these precursor writes must exist before a plan digest can be computed or approved.

Before dispatch, run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/parallel_execution.py preflight --consumer implement --plan <run-directory>/execution-plan.json`; add `--approval <run-directory>/write-approval.json` only when `write_policy.parent_writes=planned`, and omit it when `write_policy.parent_writes=none`; pass `--execution=parallel-read` for the selected portable route, including an environment-selected value.

After every spawned child reaches a terminal handoff, run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/parallel_execution.py validate-runtime --consumer implement --manifest <run-directory>/execution-manifest.json --plan <run-directory>/execution-plan.json --parent-rollout <authoritative-parent-rollout> --sessions-dir <authoritative-sessions-directory> --run-dir <run-directory> --roles-dir PLUGIN_ROOT/roles`. Require `runtime_promotion_eligible=true`, `consumer_id=implement`, and `write_parallel_eligible=false`.

### Serial parent decisions

On this portable route, parent owns all implementation, test, and documentation writes; shared files and dependency chains; integration and conflict handling; calibration, artifacts, and result writes; canonical quality gates; verdict; and promotion. Never expand wave dynamically or dispatch dependent work early.

### Resource conflicts

Shared paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.

### Stop rule

Generic portable parallel writes remain disabled.
