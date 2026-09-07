<!-- file: codemap-contract.md — consumers: skills/{change-analysis,audit,code-review,code-remediate,implement,investigate,optimize,release,research}/SKILL.md, shared/codemap_adapter.py -->

# Codemap-py structural-context contract — codex-rig

Protocol: `codemap-py.integration.v1`. Codex Rig is a **consumer**, never a provider — it reads only the public `codemap-py` CLI/JSON surface (`doctor --json`, `query <subcommand>`) via `../../shared/codemap_adapter.py`. It never imports `codemap_py`, never reads codemap-py cache internals or source-tree paths, and never depends on `codemap-py` being installed.

## Launcher resolution

The adapter's launcher contract is explicit: when `CODEMAP_BIN` is non-empty, use that launcher first and fail closed if it cannot be executed or inspected; do not fall back to another launcher. Only when `CODEMAP_BIN` is unset or empty may the adapter resolve `codemap-py` through `PATH`. Resolve it once at the workflow decision point and reuse the validated literal for the probe and every query. The fallback must not guess a cache version or inspect Codemap's installation internals. Managed queries use the compact public form, `query --compact ...`, and record the resolved launcher and `doctor --json` result in the persisted context evidence.

## Active consumer guidance and integration metadata

`shared/codemap-contract.md` is the active Codex Rig consumer contract for launcher validation, query routing, and context-artifact reuse. The provider-managed `shared/codemap-py-integration.md` file is metadata-only: its identity, protocol, and timestamp do not wire a launcher, install a provider, or prove that the active consumer can recognize Codemap. Provider integration must not borrow another plugin's shared script or edit installed caches. Audit checks provider identity and active consumer guidance reachability/content separately; missing, unreachable, or outdated guidance is a bounded source-maintenance finding (or an existing approved `plan_sync` remediation), not proof of active wiring. Matching installed bytes or native plugin listings cannot prove current-session activation, and matching source hashes alone cannot prove that the guidance is semantically current.

## Persist-once rule

Each required workflow probes **once**, at its bounded decision point, and persists the result to its own run artifact (e.g. `<run-directory>/codemap-context.json`). Specialists consume that artifact from the context pack; they never re-run the adapter. Re-querying per child specialist defeats the token-saving purpose of a shared structural index and is a contract violation.

If a caller already has a context artifact that answers the decision, it must reuse that artifact rather than invoke `query-code` or the adapter again. A new query is permitted only for an unresolved fact or identified completeness gap; after a query returns complete and untruncated evidence, that same graph fact is settled, but sibling queries answering distinct dimensions still run when required. Structural evidence comes from the compact CLI JSON, never from index/cache files or raw runtime logs.

Independent read-only queries may run concurrently as separate standalone commands only against a prepared stable index with self-heal disabled (`SCAN_NO_AUTOBUILD=1` or an equivalent frozen contract). Dependent queries wait for their inputs. Refresh, self-heal, and index writes are always serialized. There is no arbitrary total-call cap for facts required to finish; correction retries for one started query stay bounded and stop when the same correction failure recurs.

Invocation:

```
python PLUGIN_ROOT/shared/codemap_adapter.py context --category <analysis|implementation|review|audit> \
  [--query-kind <skip|central|callers|blast|dependencies|test-impact|coupling|standard>] \
  [--target <qname>] [--root <path>] --out <run-directory>/codemap-context.json
```

`query-kind` defaults to `standard`, preserving the existing category batch for callers that do not opt into adaptive routing. `skip` records a truthful zero-query decision and does not resolve a launcher, run `doctor`, or start a Codemap subprocess. For a non-standard fact kind with a valid required target, the adapter runs the health probe and exactly one compact query; a missing or malformed required target records a bounded degraded outcome without starting that query. `standard` retains the category's established batch. The adapter persists `artifact_schema_version: 3` and the selected `query_kind` while keeping `protocol_version: codemap-py.integration.v1` unchanged.

A skipped artifact retains the same top-level contract and makes the decision auditable without fabricating provider evidence:

```json
{
  "protocol_version": "codemap-py.integration.v1",
  "artifact_schema_version": 3,
  "category": "implementation",
  "query_kind": "skip",
  "target": "pkg.module::symbol",
  "status": "skipped",
  "probe": {"status": "skipped", "detail": "query kind skip: no Codemap subprocess requested", "launcher": null, "doctor": null},
  "queries": [],
  "index_path_divergence": []
}
```

## Index-path provenance

Schema 3 adds two provenance fields. Each record in `queries[]` carries `index_path`: the index file the provider reported for that one query — the path it actually opened, or, on a not-indexed exit raised by a failed load, the path it addressed and could not open. A provider that reports no path yields `null`, which is the expected shape for a Codemap predating the field and for a not-indexed exit raised after the index loaded successfully. The adapter never substitutes the probe's path for a missing one.

`index_path_divergence` lists every query whose reported path differs from `probe.doctor.index_path`, each record naming the subcommand and both paths. The two are worth comparing because they are produced by separate processes: `doctor` derives its path from Codemap's resolver, while a query reports the file it opened. A disagreement means the two processes resolved different indexes — a stale index-directory override, a different git root, or a self-heal that rewrote elsewhere — so the probe's path is not provenance for the answers actually returned.

The divergence is recorded, never reconciled, and never folded into `status`. The adapter has no basis for electing one path as correct, and a status token would cost the reader the two paths that make the disagreement diagnosable; a run with divergent paths and complete, fresh answers therefore stays `available`. The paths are compared verbatim rather than normalized, since normalizing would absorb exactly the symlink and relative-root differences worth reporting. A path missing on either side produces no record — absence is not disagreement.

## Named status vocabulary

| Status | Meaning | Workflow action |
| -- | -- | -- |
| `available` | `codemap-py` present, index healthy, evidence exhaustive for the queries run | consume the persisted evidence as authoritative |
| `absent` | `codemap-py` not installed / not on PATH | fall back to Codex Rig's own bounded file inspection; do not treat this as an error |
| `stale` | index older than source for at least one query's target | note the caveat, still use the returned evidence, do not silently treat it as exhaustive |
| `incompatible` | interpreter unsupported, `doctor` payload malformed, or every mapped query failed | fall back to bounded file inspection; record the incompatibility, never retry the adapter within the same run |
| `degraded` | some evidence returned but `query_complete`/`not_covered`/`degraded` flags a gap | use the evidence, surface the gap as a caveat, never silently present it as exhaustive |
| `stale+degraded` | the batch is stale **and** gap-flagged, whether both conditions come from one query or from different queries | surface both caveats: re-indexing alone does not make this evidence exhaustive |
| `skipped` | adaptive routing deliberately selected zero Codemap queries | continue with bounded local inspection; retain the persisted route decision and do not claim structural evidence |

`stale+degraded` is the vocabulary's only composed value, and no further composition is possible: `absent`, `incompatible`, and `skipped` are decided before any query runs, and `stale` is only ever read off a query that parsed successfully. It exists because ranking one condition above the other would drop the other's caveat — `stale` alone invites the false conclusion that re-indexing restores exhaustiveness, and `degraded` alone hides that the evidence describes an older tree. A consumer testing for a single condition splits the value on `+`; the per-query `stale`, `query_complete`, `not_covered`, and `degraded` fields remain in `queries[]` either way.

Absence and incompatibility are non-fatal: the workflow proceeds with its normal bounded file inspection. A run must never claim structural evidence it did not actually receive.

## Category → query map (plan §8.4)

| Category | Consuming skills | Queries (`codemap-py query <subcommand>`) |
| -- | -- | -- |
| `analysis` | change-analysis, research | `central` (no target) + `deps <target>` |
| `implementation` | implement, investigate, optimize | `rdeps <target>` + `coupled` (no target) + `test-impact <target>` |
| `review` | code-review, code-remediate | `diff-impact` (no target — reads the working-tree/PR diff once) |
| `audit` | audit, release | `undocumented --all` + `dead-modules` (no target) |

Adaptive route kinds are a closed consumer vocabulary: `central` (`central`), `callers` (`fn-rdeps <module::symbol> --exclude-tests`), `blast` (`fn-blast <module::symbol>`), `dependencies` (`rdeps <module>`), `test-impact` (`test-impact <module-or-qname>`), and `coupling` (`coupled`). `standard` selects the category table above; `skip` selects no query. A workflow chooses `skip` for an exact localized edit with no unresolved structural fact, the matching single route for one unresolved fact, and `standard` for broad or unknown scope. An explicit user or tool request for structural evidence overrides `skip`. The decision is made once and the resulting artifact is consumed by all specialists without re-querying.

## Route selection per skill

Passing `--query-kind` is a per-workflow decision, not a migration every consumer owes. The default keeps the category batch, so a skill that does not select a route is fully specified rather than unfinished. This table is the record of each skill's choice; a skill that starts or stops selecting routes must move rows here in the same change.

| Skill | Category | Route selection | Why |
| -- | -- | -- | -- |
| `implement` | `implementation` | adaptive — passes `--query-kind` | resolves one module/symbol at its decision point, so a single fact usually settles the open structural question |
| `investigate` | `implementation` | adaptive — passes `--query-kind` | same decision point, reached only when `scope` names a Python module/symbol |
| `optimize` | `implementation` | adaptive — passes `--query-kind` | same decision point, reached only when `scope_files` resolves to a Python module/symbol |
| `change-analysis` | `analysis` | standard batch — no `--query-kind` | its decision point is broad or unknown scope, which the routing rule above already assigns to `standard` |
| `research` | `analysis` | standard batch — no `--query-kind` | same broad-scope decision point |
| `code-review` | `review` | standard batch — no `--query-kind` | the category's only query is `diff-impact`, which has no equivalent in the closed fact-kind vocabulary; the sole alternative kind would be `skip` |
| `code-remediate` | `review` | standard batch — no `--query-kind` | same single-query category |
| `audit` | `audit` | standard batch — no `--query-kind` | the category's `undocumented --all` and `dead-modules` have no fact-kind equivalents |
| `release` | `audit` | standard batch — no `--query-kind` | same category |

The five not-applicable skills below select no category at all and are absent from this table by design.

`target` is a dotted module or `module::symbol` qname when the skill has resolved one at its decision point; it is optional for every consuming skill. In a `standard` batch with no target, the queries marked `requires_target=True` in `codemap_adapter.CATEGORY_QUERIES` are dropped from the plan instead of being run and failed, so `analysis` runs `central` alone and `implementation` runs `coupled` alone. The artifact then lists only the queries actually attempted and `available` keeps its documented "exhaustive for the queries run" meaning; `degraded` stays reserved for a real gap in provider evidence rather than for a question the caller never asked. A category whose every query requires a target keeps its bounded error, because reporting `available` off zero executed queries would claim evidence never received. An explicit fact route still records a bounded degraded outcome for a missing or malformed required target — there the target is the caller's own required input, not an optional refinement. Route normalization uses the module portion for `dependencies`, the full qname for `callers` and `blast`, and either form for `test-impact`. A malformed or missing required target is never guessed or queried.

## Not-applicable skills (recorded reason, no forced query)

Five skills have no Python structural-query subject and stay not-applicable rather than being forced to integrate:

| Skill | Reason |
| -- | -- |
| `manage` | operates on plugin config artifacts (`.md`/`.toml`/`.json`) — config-reference propagation, not a Python call graph |
| `sync` | pure package-lifecycle (install/refresh via Codex CLI) — zero source analysis; must stay the single-product owner, never a circular installer |
| `agent-shims` | manages authenticated agent shim lifecycle/filesystem state — no source-code subject |
| `calibrate` | consumes fixed `runtime/calibration/*` fixtures — self-measurement of the plugin, not consuming-project Python structure |
| `kaggle` | output is a Jupytext notebook; `*.ipynb` is outside the frozen `py311-ast-v1` module/package contract |

## Symmetric optionality (plan §8.5)

`codemap_adapter.py` has zero import-time or startup-time dependency on `codemap-py` being installed — every subprocess call is lazy, inside a function called only when a wired skill reaches its decision point. Codex Rig's own skill discovery, packaging, and startup never probe or require `codemap-py`.

This optionality is the default runtime contract. A benchmark or deployment profile may make Codemap availability an explicit admission requirement for a packaged-integration arm, but it must install and version-lock the provider and still validate launcher recognition through the adapter; installation alone does not change the runtime contract.
