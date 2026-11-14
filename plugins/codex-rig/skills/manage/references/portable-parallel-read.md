## Parallel Adoption (Portable read-only)

<!-- policy-sibling: skills/implement/SKILL.md (Parallel Adoption section) — near-duplicate, invariant text in shared/portable-parallel-read.md; check siblings before editing this section alone -->

> Only when Execution Route Selection resolves to `parallel-read`, read [portable-parallel-read.md](../../../shared/portable-parallel-read.md) before any child preparation; `serial`, `auto`, and native routes do not load it. The reference preserves the shared route gate, write-policy and approval rules, host-compatibility barrier, preflight and runtime-validation qualifiers, resource-lock declaration, fallback, acceptance, and stop rule; the consumer values below stay authoritative for this skill.

This portable section permits only its promoted portable read-only route. Native disjoint read-only support may follow Proportional Execution; protected-target, approval, and propagation rules stay unchanged.

Follow [portable-parallel-read.md](../../../shared/portable-parallel-read.md) for shared gate order and fork outcomes; the [G0–G8 execution diagram](../../../assets/images/parallel-execution-architecture.svg) is illustrative only. This consumer's read-only inventory passes are bounded by G0–G5; parent owns deterministic G6 integration, G7 verification, and G8 verdict/promotion.

### Safe parallel work

The promoted route permits read-only inventory, reference, ownership, and policy-impact scans over immutable disjoint targets with separate outputs. Disjoint propagation is not enabled; config, policy, documentation, calibration, cache, generated-output, artifact, and result writes remain outside this portable route.

### Required barrier

Before any dispatch, freeze intent, target, baseline, ownership map, exact references, calibration and routing impact, context packs, role-card hashes, checks, resource locks, and plan digest. Dispatch at most one fixed dependency-ready wave, then join every terminal scan before edits, propagation, gates, or acceptance; changed scope requires new plan.

The frozen `<run-directory>/execution-plan.json` must include exact `consumer_policy` values `consumer_id=manage`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial`.

The plan must also freeze schema-3 `dispatch_choices` for each node as specified in [portable-parallel-read.md](../../../shared/portable-parallel-read.md#required-barrier); omitted spawn settings are represented by `null` and inherit observed parent controls.

Bootstrap exception: run-directory creation (Step 01), the `inventory.txt`/`references.txt`/`ownership.md` writes from Step 03, and the `write-approval.json` write itself are exempt from requiring a prior approved plan digest — these precursor writes must exist before a plan digest can be computed or approved.

Before dispatch, run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/parallel_execution.py preflight --consumer manage --plan <run-directory>/execution-plan.json`; add `--approval <run-directory>/write-approval.json` only when `write_policy.parent_writes=planned`, and omit it when `write_policy.parent_writes=none`; pass `--execution=parallel-read` for the selected portable route, including an environment-selected value.

After every spawned child reaches a terminal handoff, run `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/parallel_execution.py validate-runtime --consumer manage --manifest <run-directory>/execution-manifest.json --plan <run-directory>/execution-plan.json --parent-rollout <authoritative-parent-rollout> --sessions-dir <authoritative-sessions-directory> --run-dir <run-directory> --roles-dir PLUGIN_ROOT/roles`. Require `runtime_promotion_eligible=true`, `consumer_id=manage`, and `write_parallel_eligible=false`.

### Serial parent decisions

Parent owns all create, update, delete, rename, and permission mutations; same-file policy or config changes; shared `AGENTS.md`, README, and config edits; calibration version advancement; propagation; artifacts and result writes; canonical quality gates; verdict; and promotion. Installed plugin-root and generated `codex-rig-*.toml` safety decisions remain serial and unchanged.

### Resource conflicts

Shared targets, paths, indexes, caches, generated outputs, test environments, ports, devices, or undeclared resources force serial execution or re-planning.

### Stop rule

Generic parallel writes remain disabled.
