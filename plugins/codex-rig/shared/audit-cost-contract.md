# Audit Optimization Evidence

> Required only before declaring `metadata.value_per_token.status=accepted`; [Audit](../skills/audit/SKILL.md) loads this contract at that decision. Other comparison states retain their honest evidence limits.

Acceptance uses `value_per_token.schema_version=2`. Version 1 records remain archived evidence; their old acceptance assertions cannot certify a current optimization. An assessed audit may pass while its optimization remains `not-run`, `insufficient-evidence`, or `rejected`. A passing audit requires its actual `review` gate to pass; generic all-skipped diagnostic accounting alone is insufficient.

## Required records

- `scope_roots`, `conditional_load_trace`, and `residual_limits`: nonempty string lists. Declare the complete source set, exercised paths, every loaded reference, and remaining evidence limits.
- `baseline_sha256`, `candidate_sha256`: distinct lowercase SHA-256 snapshot identities, equal across static measurements and retained comparisons. Workflow owner verifies complete source coverage and snapshot provenance.
- `static_measurements`: nonempty records with `source=provider-native|tiktoken:o200k_base`, both snapshot identities, and finite positive `baseline_tokens` / `candidate_tokens`. Byte or word proxies cannot accept a candidate.
- `obligation_map_path` and `obligation_map_sha256`: run-relative POSIX JSON path and exact retained-byte digest. Its nonempty list maps every original obligation to `baseline`, `candidate`, and `evidence` strings with `preserved=true`. Workflow owner and independent reviewer verify completeness, modal strength, ordering, exceptions, approval, and stop conditions; a self-declared map does not prove these facts.
- `static_gates`: exactly one record per `package`, `tests`, `calibration`, `contract-markers`, and `adversarial-review`; each has `id`, `status=pass`, and `evidence`. Retained guard JSON also requires `status=pass` and both matching snapshot identities. Keep exact checks, source identities, reviewer evidence and results in those records.
- `behavioral_comparison` and `live_comparison`: retained evidence records. Each evidence record has exactly `path` and `sha256`, binding existing JSON bytes inside the run directory. No symlink components, traversal, or external paths are accepted.
- `decision`: nonempty evidence-backed explanation. List each remaining limit once in `residual_limits`.

## Comparison bodies

Behavioral JSON carries both snapshot identities, finite nonnegative `critical_regressions`, `baseline_failures`, and `candidate_failures`. Acceptance requires zero critical regressions and no increased failures. Its nonempty `tasks` list retains each unique nonempty `id` and paired `baseline` / `candidate` assessments. Each assessment requires finite `completion_quality` in `[0, 1]`, finite nonnegative `tool_failures` / `check_failures`, and a nonempty `evidence` string list identifying the retained task results and assessment. Every candidate task must retain or improve completion quality and must not increase either failure count. Parent review verifies the paired task coverage, common assessment rubric and full retained evidence; numerical declarations do not establish truthful results.

Live JSON carries both snapshot identities, `source=provider-native`, matching `baseline_identity` and `candidate_identity` objects with exactly `model`, `effort`, `task_contract_sha256`, and `prompt_sha256`. It records finite positive `baseline_cost`, `candidate_cost`, and `min_cost_reduction` strictly between 0 and 1. Use normalized costs in the same unit; acceptance requires `(baseline_cost - candidate_cost) / baseline_cost >= min_cost_reduction`. Retain paired native observations and provider provenance; missing paired evidence stays `insufficient-evidence`.

Validation establishes structure, retained digest integrity, identity matching, declared guard outcomes, and arithmetic. It does not authenticate provider telemetry, reviewer independence, source completeness, or truthful task results. Parent acceptance still checks those facts; never substitute a passing validator for the required review or live observations.
