<!-- file: codemap-contract.md — consumers: skills/{assess,audit,code-review,code-remediate,implement,investigate,optimize,release,research}/SKILL.md, shared/codemap_adapter.py -->

# Codemap-py structural-context contract — codex-rig

Protocol: `codemap-py.integration.v1`. Codex Rig is **consumer**, never provider — it reads only public `codemap-py` CLI/JSON surface (`doctor --json`, `query <subcommand>`) via `../../shared/codemap_adapter.py`. It never imports `codemap_py`, never reads codemap-py cache internals or source-tree paths, and never depends on `codemap-py` being installed.

## Launcher resolution

The adapter's launcher contract is explicit: when `CODEMAP_BIN` is non-empty, use that launcher first and fail closed if it cannot be executed or inspected; do not fall back to another launcher. Only when `CODEMAP_BIN` is unset or empty may adapter resolve `codemap-py` through `PATH`. Resolve it once at workflow decision point and reuse validated literal for probe and every query. The fallback must not guess cache version or inspect Codemap's installation internals. Managed queries use compact public form, `query --compact ...`, and record resolved launcher and `doctor --json` result in persisted context evidence.

## Active consumer guidance and integration metadata

`shared/codemap-contract.md` is active Codex Rig consumer contract for launcher validation, query routing, and context-artifact reuse. The provider-managed `shared/codemap-py-integration.md` file is metadata-only: its identity, protocol, and timestamp do not wire launcher, install provider, or prove that active consumer can recognize Codemap. Provider integration must not borrow another plugin's shared script or edit installed caches. Audit checks provider identity and active consumer guidance reachability/content separately; missing, unreachable, or outdated guidance is bounded source-maintenance finding (or existing approved `plan_sync` remediation), not proof of active wiring. Matching installed bytes or native plugin listings cannot prove current-session activation, and matching source hashes alone cannot prove that guidance is semantically current.

## Persist-once rule

Each required workflow probes **once**, at its bounded decision point, and persists result to its own run artifact (e.g. `<run-directory>/codemap-context.json`). Specialists consume that artifact from context pack; they never re-run adapter. Re-querying per child specialist defeats token-saving purpose of shared structural index and is contract violation.

If caller already has context artifact that answers decision, it must reuse that artifact rather than invoke `query-code` or adapter again. A new query is permitted only for unresolved fact or identified completeness gap; after query returns complete and untruncated evidence, that same graph fact is settled, but sibling queries answering distinct dimensions still run when required. Structural evidence comes from compact CLI JSON, never from index/cache files or raw runtime logs.

Independent read-only queries may run concurrently as separate standalone commands only against prepared stable index with self-heal disabled (`SCAN_NO_AUTOBUILD=1` or equivalent frozen contract). Dependent queries wait for their inputs. Refresh, self-heal, and index writes are always serialized. There is no arbitrary total-call cap for facts required to finish; correction retries for one started query stay bounded and stop when same correction failure recurs.

Invocation:

```
python PLUGIN_ROOT/shared/codemap_adapter.py context --category <analysis|implementation|review|audit> \
  [--query-kind <skip|central|callers|blast|dependencies|test-impact|coupling|standard>] \
  [--target <qname>] [--root <path>] --out <run-directory>/codemap-context.json
```

`query-kind` defaults to `standard`, preserving existing category batch for callers that do not opt into adaptive routing. `skip` records truthful zero-query decision and does not resolve launcher, run `doctor`, or start Codemap subprocess. For non-standard fact kind with valid required target, adapter runs health probe and exactly one compact query; missing or malformed required target records bounded degraded outcome without starting that query. `standard` retains category's established batch. The adapter persists `artifact_schema_version: 3` and selected `query_kind` while keeping `protocol_version: codemap-py.integration.v1` unchanged.

A skipped artifact retains same top-level contract and makes decision auditable without fabricating provider evidence:

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

Schema 3 adds two provenance fields. Each record in `queries[]` carries `index_path`: index file provider reported for that one query — path it actually opened, or, on not-indexed exit raised by failed load, path it addressed and could not open. A provider that reports no path yields `null`, which is expected shape for Codemap predating field and for not-indexed exit raised after index loaded successfully. The adapter never substitutes probe's path for missing one.

`index_path_divergence` lists every query whose reported path differs from `probe.doctor.index_path`, each record naming subcommand and both paths. The two are worth comparing because they are produced by separate processes: `doctor` derives its path from Codemap's resolver, while query reports file it opened. A disagreement means two processes resolved different indexes — stale index-directory override, different git root, or self-heal that rewrote elsewhere — so probe's path is not provenance for answers actually returned.

The divergence is recorded, never reconciled, and never folded into `status`. The adapter has no basis for electing one path as correct, and status token would cost reader two paths that make disagreement diagnosable; run with divergent paths and complete, fresh answers therefore stays `available`. The paths are compared verbatim rather than normalized, since normalizing would absorb exactly symlink and relative-root differences worth reporting. A path missing on either side produces no record — absence is not disagreement.

## Named status vocabulary

| Status | Meaning | Workflow action |
| -- | -- | -- |
| `available` | `codemap-py` present, index healthy, evidence exhaustive for queries run | consume persisted evidence as authoritative |
| `absent` | `codemap-py` not installed / not on PATH | fall back to Codex Rig's own bounded file inspection; do not treat this as error |
| `stale` | index older than source for at least one query's target | note caveat, still use returned evidence, do not silently treat it as exhaustive |
| `incompatible` | interpreter unsupported, `doctor` payload malformed, or every mapped query failed | fall back to bounded file inspection; record incompatibility, never retry adapter within same run |
| `degraded` | some evidence returned but `query_complete`/`not_covered`/`degraded` flags gap | use evidence, surface gap as caveat, never silently present it as exhaustive |
| `stale+degraded` | batch is stale **and** gap-flagged, whether both conditions come from one query or from different queries | surface both caveats: re-indexing alone does not make this evidence exhaustive |
| `skipped` | adaptive routing deliberately selected zero Codemap queries | continue with bounded local inspection; retain persisted route decision and do not claim structural evidence |

`stale+degraded` is vocabulary's only composed value, and no further composition is possible: `absent`, `incompatible`, and `skipped` are decided before any query runs, and `stale` is only ever read off query that parsed successfully. It exists because ranking one condition above other would drop other's caveat — `stale` alone invites false conclusion that re-indexing restores exhaustiveness, and `degraded` alone hides that evidence describes older tree. A consumer testing for single condition splits value on `+`; per-query `stale`, `query_complete`, `not_covered`, and `degraded` fields remain in `queries[]` either way.

Absence and incompatibility are non-fatal: workflow proceeds with its normal bounded file inspection. A run must never claim structural evidence it did not actually receive.

## Category → query map (plan §8.4)

| Category | Consuming skills | Queries (`codemap-py query <subcommand>`) |
| -- | -- | -- |
| `analysis` | assess, research | `central` (no target) + `deps <target>` |
| `implementation` | implement, investigate, optimize | `rdeps <target>` + `coupled` (no target) + `test-impact <target>` |
| `review` | code-review, code-remediate | `diff-impact` (no target — reads working-tree/PR diff once) |
| `audit` | audit, release | `undocumented --all` + `dead-modules` (no target) |

Adaptive route kinds are closed consumer vocabulary: `central` (`central`), `callers` (`fn-rdeps <module::symbol> --exclude-tests`), `blast` (`fn-blast <module::symbol>`), `dependencies` (`rdeps <module>`), `test-impact` (`test-impact <module-or-qname>`), and `coupling` (`coupled`). `standard` selects category table above; `skip` selects no query. A workflow chooses `skip` for exact localized edit with no unresolved structural fact, matching single route for one unresolved fact, and `standard` for broad or unknown scope. An explicit user or tool request for structural evidence overrides `skip`. The decision is made once and resulting artifact is consumed by all specialists without re-querying.

## Route selection per skill

Passing `--query-kind` is per-workflow decision, not migration every consumer owes. The default keeps category batch, so skill that does not select route is fully specified rather than unfinished. This table is record of each skill's choice; skill that starts or stops selecting routes must move rows here in same change.

| Skill | Category | Route selection | Why |
| -- | -- | -- | -- |
| `implement` | `implementation` | adaptive — passes `--query-kind` | resolves one module/symbol at its decision point, so single fact usually settles open structural question |
| `investigate` | `implementation` | adaptive — passes `--query-kind` | same decision point, reached only when `scope` names Python module/symbol |
| `optimize` | `implementation` | adaptive — passes `--query-kind` | same decision point, reached only when `scope_files` resolves to Python module/symbol |
| `assess` | `analysis` | standard batch — no `--query-kind` | its decision point is broad or unknown scope, which routing rule above already assigns to `standard` |
| `research` | `analysis` | standard batch — no `--query-kind` | same broad-scope decision point |
| `code-review` | `review` | standard batch — no `--query-kind` | category's only query is `diff-impact`, which has no equivalent in closed fact-kind vocabulary; sole alternative kind would be `skip` |
| `code-remediate` | `review` | standard batch — no `--query-kind` | same single-query category |
| `audit` | `audit` | standard batch — no `--query-kind` | category's `undocumented --all` and `dead-modules` have no fact-kind equivalents |
| `release` | `audit` | standard batch — no `--query-kind` | same category |

The five not-applicable skills below select no category at all and are absent from this table by design.

`target` is dotted module or `module::symbol` qname when skill has resolved one at its decision point; it is optional for every consuming skill. In a `standard` batch with no target, queries marked `requires_target=True` in `codemap_adapter.CATEGORY_QUERIES` are dropped from plan instead of being run and failed, so `analysis` runs `central` alone and `implementation` runs `coupled` alone. The artifact then lists only queries actually attempted and `available` keeps its documented "exhaustive for the queries run" meaning; `degraded` stays reserved for real gap in provider evidence rather than for question caller never asked. A category whose every query requires target keeps its bounded error, because reporting `available` off zero executed queries would claim evidence never received. An explicit fact route still records bounded degraded outcome for missing or malformed required target — there target is caller's own required input, not optional refinement. Route normalization uses module portion for `dependencies`, full qname for `callers` and `blast`, and either form for `test-impact`. A malformed or missing required target is never guessed or queried.

## Not-applicable skills (recorded reason, no forced query)

Five skills have no Python structural-query subject and stay not-applicable rather than being forced to integrate:

| Skill | Reason |
| -- | -- |
| `manage` | operates on plugin config artifacts (`.md`/`.toml`/`.json`) — config-reference propagation, not Python call graph |
| `sync` | pure package-lifecycle (install/refresh via Codex CLI) — zero source analysis; must stay single-product owner, never circular installer |
| `agent-shims` | manages authenticated agent shim lifecycle/filesystem state — no source-code subject |
| `calibrate` | consumes fixed `runtime/calibration/*` fixtures — self-measurement of plugin, not consuming-project Python structure |
| `kaggle` | output is Jupytext notebook; `*.ipynb` is outside frozen `py311-ast-v1` module/package contract |

## Symmetric optionality (plan §8.5)

`codemap_adapter.py` has zero import-time or startup-time dependency on `codemap-py` being installed — every subprocess call is lazy, inside function called only when wired skill reaches its decision point. Codex Rig's own skill discovery, packaging, and startup never probe or require `codemap-py`.

This optionality is default runtime contract. A benchmark or deployment profile may make Codemap availability explicit admission requirement for packaged-integration arm, but it must install and version-lock provider and still validate launcher recognition through adapter; installation alone does not change runtime contract.
