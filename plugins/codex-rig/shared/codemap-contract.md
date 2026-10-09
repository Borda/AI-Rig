<!-- file: codemap-contract.md — consumers: skills/{assess,audit,code-review,code-remediate,implement,investigate,optimize,release,research}/SKILL.md, skills/code-review/validate_artifacts.py, shared/codemap_adapter.py -->

# Codemap-py structural-context contract — codex-rig

Protocol: `codemap-py.integration.v1`. Codex Rig is **consumer**, never provider — it reads only public `codemap-py` CLI/JSON surface (`doctor --json`, `query <subcommand>`) via `../../shared/codemap_adapter.py`. It never imports `codemap_py`, never reads codemap-py cache internals or source-tree paths, and never depends on `codemap-py` being installed.

## Launcher resolution

The adapter's launcher contract is explicit: when `CODEMAP_BIN` is non-empty, use that launcher first and fail closed if it cannot be executed or inspected; do not fall back to another launcher. When `CODEMAP_BIN` is unset or empty, an explicit `--provider-root` selected from the active installed Codemap skill path/install record resolves that provider’s shipped launcher; an invalid explicit root fails closed. Otherwise the adapter resolves `codemap-py` through `PATH`. Plugin presence outside PATH is not provider absence; pass the observed active root without scanning caches. Use the provider’s existing `CODEMAP_PYTHON` setting with an available compatible interpreter when required; do not install one. Resolve it once at workflow decision point and reuse validated literal for probe and every query. The fallback must not guess cache version or inspect Codemap's installation internals. Managed queries use compact public form, `query --compact ...`, and record resolved launcher and `doctor --json` result in persisted context evidence.

## Active consumer guidance and integration metadata

`shared/codemap-contract.md` is active Codex Rig consumer contract for launcher validation, query routing, and context-artifact reuse. The provider-managed `shared/codemap-py-integration.md` file is metadata-only: its identity, protocol, and timestamp do not wire launcher, install provider, or prove that active consumer can recognize Codemap. Provider integration must not borrow another plugin's shared script or edit installed caches. Audit checks provider identity and active consumer guidance reachability/content separately; missing, unreachable, or outdated guidance is bounded source-maintenance finding (or existing approved `plan_sync` remediation), not proof of active wiring. Matching installed bytes or native plugin listings cannot prove current-session activation, and matching source hashes alone cannot prove that guidance is semantically current.

## Persist-once rule

Each required workflow probes **once**, at its bounded decision point, and persists result to its own run artifact (e.g. `<run-directory>/codemap-context.json`). Specialists consume that artifact from context pack first. Re-running workflow's probe or standard batch per child specialist defeats token-saving purpose of shared structural index and is contract violation; the only per-specialist query is bounded follow-up below.

If caller already has context artifact that answers decision, it must reuse that artifact rather than invoke `query-code` or adapter again. A new query is permitted only for unresolved fact or identified completeness gap; after query returns complete and untruncated evidence, that same graph fact is settled, but sibling queries answering distinct dimensions still run when required. Structural evidence comes from compact CLI JSON, never from index/cache files or raw runtime logs.

Independent read-only queries may run concurrently as separate standalone commands only against prepared stable index with self-heal disabled (`SCAN_NO_AUTOBUILD=1` or equivalent frozen contract). Dependent queries wait for their inputs. Refresh, self-heal, and index writes are always serialized. There is no arbitrary total-call cap for workflow facts required to finish; per-specialist follow-ups carry their own limit below. Correction retries for one started query stay bounded and stop when same correction failure recurs.

Invocation:

```
PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/codemap_adapter.py context --category <analysis|implementation|review|audit> \
  [--query-kind <skip|central|callers|blast|dependencies|test-impact|coupling|standard>] \
  [--target <qname>] [--root <path>] [--diff-file <path>] --out <run-directory>/codemap-context.json
```

`query-kind` defaults to `standard`, preserving existing category batch for callers that do not opt into adaptive routing. `skip` records truthful zero-query decision and does not resolve launcher, run `doctor`, or start Codemap subprocess. For non-standard fact kind with valid required target, adapter runs health probe and exactly one compact query; missing or malformed required target records bounded degraded outcome without starting that query. `standard` retains category's established batch. The adapter persists `artifact_schema_version: 4` and selected `query_kind` while keeping `protocol_version: codemap-py.integration.v1` unchanged. Follow-up artifacts below share that schema.

`--root` names checkout whose index answers: adapter runs `doctor` and every query with that root as working directory, because Codemap discovers its index from working directory and only compares `--root` with that index's `scan_root`. Without that, caller inside nested review worktree opens parent repository's index and gets `root_mismatch`. A relative `--root` or `--diff-file` resolves against caller's working directory first and reaches provider absolute.

Schema 4 over schema 3:

- top-level `diff_file`: absolute unified diff change-set query read; `null` when none.
- top-level `status_reasons`: one `<subcommand>: <cause>` line per query gap, except one query's adapter-detected gaps share one line joined with `; `; non-empty for every `degraded`, `stale`, `stale+degraded`, or post-query `incompatible` status.
- per query `answer`: provider's answer itself minus `index` — for `diff-impact`, changed modules with risk and changed symbols, unmapped files, test impact, highest risk. Failed query: provider's structured error fields besides `error`/`detail` (an ambiguous target's `candidates` and `candidate_count`, a missing module's `suggestions`, `rejected_target`), so a rejected target keeps the alternatives offered; `{}` when provider's JSON error held only its text, `null` when provider printed no JSON object. Lists not nested in another list keep at most 20 items (`ANSWER_LIST_LIMIT`), nested lists 5, strings 300 characters; `answer_truncated` maps every cut path to its original length.
- Zero-caller method or constructor (`fn-rdeps`, `fn-blast`, function-level `test-impact`): provider's `hint` stays in `answer` and adds `<subcommand>: hint: <text>` to `status_reasons`. Provider keeps `query_complete`, so the hint is the only sign `called_by: []` or zero test files means unresolved statically (instance calls, property reads, bound-method references, overrides, subclass constructors, implicit protocol calls), never unused or untested. Its search is reference-shaped (`\.method\b`, `\bClass\b`), never a call-only `.method(`; a protocol (dunder) method's hint names no search, since Python calls it implicitly (`len(x)`, `x == y`), and opens with "Never delete". Each hint opens with its action, so the 300-character bound keeps the search or the keep instruction. A `test*` method in a test module gets no hint: the test runner calls it. Advisory: status unchanged.
- `diff-impact` changed method or constructor with `caller_count: 0`: provider copies the same hint onto that `fn_rdeps` entry, adds a top-level `hint` counting such symbols and naming up to five (one `diff-impact: hint: <text>` reason), and lists the call graph's `not_covered` slugs, which make the artifact `degraded` like any other `not_covered` gap. A `diff-impact` answer with no such entry carries neither and keeps its status; a change touching only test methods is such an answer.
- per query: provider's `completeness_reason`, `root_mismatch`, and `note` verbatim, change set's `changed_files`, `adapter_gap` for gap adapter detected itself, and `input_rejected` when provider itself diagnosed the failure as rejected input.
- `root_mismatch` is provider's own completeness verdict, so it degrades status with its reason; contrast `index_path_divergence` below.
- Provider reads only post-image `.py` paths and still calls these answers complete, so adapter reads same diff back and records each as `degraded` with its reason, never `available` (several gaps join with `; `):
  - changed Python files all map to no module (`changed_files > 0`, empty `changed_modules`): `all N changed Python files unmapped by the index` — mismatched index, or change that only adds new modules. Provider counts every changed `.py` file it reads, mapped or not, so only empty mapped-module list reveals this.
  - diff deletes Python modules, including rename's old path or empty/binary file with no `---`/`+++` pair: `N deleted Python modules not analysed (importers unchecked): <paths>` — at most 5 paths named, count covers all. Applies beside other changes too.
  - provider read no Python file (`changed_files` 0) because every remaining Python change lacks text hunk (mode, binary, or empty-file change): `N Python files changed without a text hunk not analysed: <paths>`. Beside a read change, such file adds no structure and no gap.
- Only diff naming no `.py` path at all may map zero changed files and stay `available`.
- Git C-quotes a non-ASCII path (`"pkg/mod\303\251.py"`) in diff headers, `rename from` lines, and `files.txt`; adapter and validator unquote every one before classifying it, so such a path neither skips required probe nor hides a deleted module.
- Provider failure keeps provider's own `error`/`detail` text. Input provider itself diagnoses and rejects sets `input_rejected` and degrades context as caller input. Flag is `provider_rejected_input(exit_code, answer)` over recorded fields alone, so readers re-derive it, never trust it:
  - exit 2 with its JSON `error` object (recorded `answer` an object, possibly `{}`), such as unreadable diff file;
  - target it could not resolve — not found, ambiguous (`candidates`), module where function required, or module not indexed — whose JSON error carries `rejected_target` (exit 1 or 3), or, from provider predating that key, not-indexed exit 3 naming `module`.
- Provider-side failures share those exits: bare exit 2 without JSON `error` is argument-parser usage error (adapter/provider flag skew); exit 1 without `rejected_target` is invalid index or disabled feature; exit 3 with root `path` is index that failed to load (path recorded as query `index_path`). Only provider-side failure of every query is `incompatible`.

A skipped artifact retains same top-level contract and makes decision auditable without fabricating provider evidence:

```json
{
  "protocol_version": "codemap-py.integration.v1",
  "artifact_schema_version": 4,
  "category": "implementation",
  "query_kind": "skip",
  "target": "pkg.module::symbol",
  "diff_file": null,
  "status": "skipped",
  "status_reasons": [],
  "probe": {"status": "skipped", "detail": "query kind skip: no Codemap subprocess requested", "launcher": null, "doctor": null},
  "queries": [],
  "index_path_divergence": []
}
```

## Required probe for Python scope

Required means adapter runs and persists `<run-directory>/codemap-context.json`; it never means provider must be installed.

| Skill | Required when | Recorded outcome |
| -- | -- | -- |
| `code-review` | collected `files.txt` lists case-sensitive `.py` path | standard `review` batch; `skipped` invalid, since category has no skip decision |
| `code-remediate` | `target_scope` or normalized findings name `.py` path | standard `review` batch |
| `implement` | target or planned files include `.py` path | adaptive route; `skip` still runs adapter and persists `skipped` artifact with its reason |

Provider maps only case-sensitive `.py` post-image paths from diff file, and review `diff.patch` excludes untracked files, so stub-only (`.pyi`) or untracked-only Python changes do not require probe whose answer could only be empty.

- `review` batch reads change set from `--diff-file`, never by diffing working tree against `HEAD`: in detached review worktree at reviewed head that compares tree with itself and reports zero changed files as complete answer. Missing diff file records bounded degraded outcome without starting `diff-impact`.
- Run review batch from invoking checkout that owns index: `--root` is that checkout, never nested review worktree. Result describes that indexed tree's structure for reviewed change set, not reviewed-head source; modules added by change appear as unmapped files. Any residual `root_mismatch` stays `degraded` with its reason.
- `absent` and `incompatible` stay non-fatal: artifact records `status` plus `probe.detail` reason, and workflow continues with bounded file inspection.
- Skipping adapter without artifact is contract failure. Code Review's validator (`skills/code-review/validate_artifacts.py`) fails closed on every fresh candidate: `codemap-context-missing-for-python-diff`, `codemap-context-skipped-for-python-diff`, or `codemap-context-invalid:<field>`. Promoted historical results predating rule stay readable.
- Current artifact must be internally true: `query_kind` `standard`; exactly one `diff-impact` record when provider probe was usable, none otherwise; `diff_file` the run's own absolute `<run-directory>/diff.patch` once query ran (any other run file, such as `files.txt`, reads as diff with no Python change); each query's `input_rejected` equal to `provider_rejected_input` over its recorded exit code and `answer` (`codemap-context-invalid:input_rejected` otherwise; follow-ups `codemap-followup-invalid:<name>:input_rejected`), so a provider-side failure hand-marked as rejected input cannot pass as `degraded` and admit follow-ups; `adapter_gap`, `status`, and `status_reasons` equal to what adapter's own reduction derives from recorded query outcomes and that `diff.patch` (`reduce_status`, `gap_reasons`, `change_set_gap`, `read_diff_text`). Python diff whose query answered zero changed files with empty `status_reasons` fails as `codemap-context-invalid:changed_files`. Hand-written `available` over empty, failed, mismatched, all-unmapped, or deletion-only query fails closed.
- Recovered pre-rule run: run restored by `review_prepare.py recover-native-provenance` (which leaves `native-recovery/inspection-summary.json` and `specialist-manifest.native-recovery.candidate.json`) never re-runs adapter retroactively. It keeps its existing schema-3 artifact, or, with none, records `codemap_context: "not-required-historical"` in result metadata. Validator requires both recovery files, marker as JSON object and candidate carrying recovery identity (`schema_version` 8, `manifest_kind` `native-wave`, `dispatch_protocol` `paged-context-v6`), and rejects exemption beside real probe artifact or any other value.
- `code-remediate` and `implement` persist no canonical changed-file listing for validator to classify, so their requirement is instruction-level; their own notes still name artifact path and status.
- `assess`, `investigate`, `optimize`, `research`, `audit`, and `release` keep optional decision-point probe.

## Specialist follow-up queries

When persisted artifact leaves one structural fact open for one specialist's axis, that specialist may receive at most 3 targeted follow-up queries per workflow run (`FOLLOW_UP_QUERY_LIMIT` in adapter).

- Routes: `callers` (`fn-rdeps`), `dependencies` (`rdeps`), or `test-impact`, each with resolved `--target`. Never `standard`, `central`, `blast`, `coupling`, or `skip`.
- Precondition: primary artifact status is `available`, `stale`, `degraded`, or `stale+degraded`. After `absent`, `incompatible`, or `skipped`, no follow-up runs; incompatible provider is never retried within same run. Follow-up whose own artifact is `incompatible` ends that run's follow-ups the same way. Follow-up `degraded` with `input_rejected` means provider works and refused target: next follow-up may use one candidate from its `answer`, within per-role limit.
- Executor: whoever holds command capability for that specialist. Read-only or tool-free reviewers never run commands, so parent runs their follow-ups before their context is prepared and frozen; native bounded owners with command capability run their own. Reuse launcher literal validated for primary probe.
- Each query writes new file, never overwrites, numbered `01` to `03` per role; cite its path, route, target, and status in that specialist's own evidence (Code Review: its `review-briefs.json` evidence brief; other workflows: its context pack or handoff).
- A fourth open structural question becomes recorded coverage gap in that specialist's evidence, never fourth query.
- Code Review's validator enforces file naming, role in `review-routing.json` `triggered_roles` (so renamed roles cannot multiply limit), route and its recorded subcommand, matching category, non-empty target, provider precondition, and `input_rejected` and status re-derived from recorded query.

```
PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/codemap_adapter.py context --category <primary category> \
  --query-kind <callers|dependencies|test-impact> --target <qname> [--root <path>] [--provider-root <path>] \
  --out <run-directory>/codemap-followups/<role>-<NN>.json
```

## Index-path provenance

Schema 3 adds two provenance fields. Each record in `queries[]` carries `index_path`: index file provider reported for that one query — path it actually opened, or, on not-indexed exit raised by failed load, path it addressed and could not open. A provider that reports no path yields `null`, which is expected shape for Codemap predating field and for not-indexed exit raised after index loaded successfully. The adapter never substitutes probe's path for missing one.

`index_path_divergence` lists every query whose reported path differs from `probe.doctor.index_path`, each record naming subcommand and both paths. The two are worth comparing because they are produced by separate processes: `doctor` derives its path from Codemap's resolver, while query reports file it opened. A disagreement means two processes resolved different indexes — stale index-directory override, different git root, or self-heal that rewrote elsewhere — so probe's path is not provenance for answers actually returned.

The divergence is recorded, never reconciled, and never folded into `status` — unlike provider-reported `root_mismatch`, which is provider's own verdict on its answer. The adapter has no basis for electing one path as correct, and status token would cost reader two paths that make disagreement diagnosable; run with divergent paths and complete, fresh answers therefore stays `available`. The paths are compared verbatim rather than normalized, since normalizing would absorb exactly symlink and relative-root differences worth reporting. A path missing on either side produces no record — absence is not disagreement.

## Named status vocabulary

| Status | Meaning | Workflow action |
| -- | -- | -- |
| `available` | `codemap-py` present, index healthy, evidence exhaustive for queries run | consume persisted evidence as authoritative |
| `absent` | `codemap-py` not installed / not on PATH | fall back to Codex Rig's own bounded file inspection; do not treat this as error |
| `stale` | index older than source for at least one query's target | note caveat, still use returned evidence, do not silently treat it as exhaustive |
| `incompatible` | interpreter unsupported, `doctor` payload malformed, or every mapped query failed provider-side (including bare exit-2 usage error) | fall back to bounded file inspection; record incompatibility, never retry adapter within same run |
| `degraded` | some evidence returned but `query_complete`/`not_covered`/`degraded` flags gap | use evidence, surface gap as caveat, never silently present it as exhaustive |
| `stale+degraded` | batch is stale **and** gap-flagged, whether both conditions come from one query or from different queries | surface both caveats: re-indexing alone does not make this evidence exhaustive |
| `skipped` | adaptive routing deliberately selected zero Codemap queries | continue with bounded local inspection; retain persisted route decision and do not claim structural evidence |

`stale+degraded` is vocabulary's only composed value, and no further composition is possible: `absent` and `skipped` are decided before any query runs, `incompatible` either before any query runs or only when no query succeeded, and `stale` is only ever read off query that parsed successfully. It exists because ranking one condition above other would drop other's caveat — `stale` alone invites false conclusion that re-indexing restores exhaustiveness, and `degraded` alone hides that evidence describes older tree. A consumer testing for single condition splits value on `+`; per-query `stale`, `query_complete`, `not_covered`, and `degraded` fields remain in `queries[]` either way.

Absence and incompatibility are non-fatal: workflow proceeds with its normal bounded file inspection. A run must never claim structural evidence it did not actually receive.

## Category → query map

| Category | Consuming skills | Queries (`codemap-py query <subcommand>`) |
| -- | -- | -- |
| `analysis` | assess, research | `central` (no target) + `deps <target>` |
| `implementation` | implement, investigate, optimize | `rdeps <target>` + `coupled` (no target) + `test-impact <target>` |
| `review` | code-review, code-remediate | `diff-impact --diff-file <diff>` (no target — reads reviewed change set once) |
| `audit` | audit, release | `undocumented --all` + `dead-modules` (no target) |

Adaptive route kinds are closed consumer vocabulary: `central` (`central`), `callers` (`fn-rdeps <module::symbol> --exclude-tests`), `blast` (`fn-blast <module::symbol>`), `dependencies` (`rdeps <module>`), `test-impact` (`test-impact <module-or-qname>`), and `coupling` (`coupled`). `standard` selects category table above; `skip` selects no query. A workflow chooses `skip` for exact localized edit with no unresolved structural fact, matching single route for one unresolved fact, and `standard` for broad or unknown scope. An explicit user or tool request for structural evidence overrides `skip`. The decision is made once and resulting artifact is consumed by all specialists; only bounded specialist follow-ups above may add queries.

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

`target` is dotted module or `module::symbol` qname when skill has resolved one at its decision point; it is optional for every consuming skill. In a `standard` batch with no target, queries marked `requires_target=True` in `codemap_adapter.CATEGORY_QUERIES` are dropped from plan instead of being run and failed, so `analysis` runs `central` alone and `implementation` runs `coupled` alone. The artifact then lists only queries actually attempted and `available` keeps its documented "exhaustive for the queries run" meaning; `degraded` stays reserved for real gap in provider evidence rather than for question caller never asked. A category whose every query requires target keeps its bounded error, because reporting `available` off zero executed queries would claim evidence never received. An explicit fact route still records bounded degraded outcome for missing or malformed required target — there target is caller's own required input, not optional refinement: `target required, none supplied` when none (or only whitespace) was given, `target malformed: …` when one was given with empty side around `::` or more than one `::`. Route normalization uses module portion of `module::symbol` for `dependencies`; `callers`, `blast`, and `test-impact` pass `module::symbol`, dotted `module.symbol`, or bare name unchanged, and provider resolves unique one (`normalized_from`) or rejects ambiguous one with `candidates`. A malformed or missing required target is never guessed or queried.

## Not-applicable skills (recorded reason, no forced query)

Five skills have no Python structural-query subject and stay not-applicable rather than being forced to integrate:

| Skill | Reason |
| -- | -- |
| `manage` | operates on plugin config artifacts (`.md`/`.toml`/`.json`) — config-reference propagation, not Python call graph |
| `sync` | pure package-lifecycle (install/refresh via Codex CLI) — zero source analysis; must stay single-product owner, never circular installer |
| `agent-shims` | manages authenticated agent shim lifecycle/filesystem state — no source-code subject |
| `calibrate` | consumes fixed `runtime/calibration/*` fixtures — self-measurement of plugin, not consuming-project Python structure |
| `kaggle` | output is Jupytext notebook; `*.ipynb` is outside frozen `py311-ast-v1` module/package contract |

## Symmetric optionality

`codemap_adapter.py` has zero import-time or startup-time dependency on `codemap-py` being installed — every subprocess call is lazy, inside function called only when wired skill reaches its decision point. Codex Rig's own skill discovery, packaging, and startup never probe or require `codemap-py`.

A required probe for Python scope keeps this optionality: it obliges workflow to run adapter and persist outcome, never to have provider installed. This optionality is default runtime contract. A benchmark or deployment profile may make Codemap availability explicit admission requirement for packaged-integration arm, but it must install and version-lock provider and still validate launcher recognition through adapter; installation alone does not change runtime contract.
