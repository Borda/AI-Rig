# Changelog

`codemap-py` is the renamed, direct successor to the `codemap` plugin. The maintained product and its SemVer history continue across the rename; only the plugin identity, repository directory, and skill namespace change. Pre-`0.25.0` history was recorded as `codemap` under `plugins/codemap/` — see the repository git history for that line; it is not reproduced here.

## 0.34.0

- Preserve standalone coverage, completeness, truncation, and totals in each batch item's `result.index`; summarize only common fields and conservative completion at the batch level. A complete first item no longer masks partial or failed siblings.

- Add read-only source/native consumer query-guidance evidence and missing, unreferenced, or drift findings to integration audit. Static references do not prove fresh-session activation; managed metadata remains separate from operational guidance.

- Align both query and integration skills around once-only launcher resolution, direct known syntax, stable read-only concurrency, bounded recovery, and reuse of settled graph facts without suppressing distinct follow-up questions. Keep self-heal and writes serial.

- Describe integration demo accurately as an audit plus structural smoke query, with explicit unavailable token measurement and no paired-comparison claim.

- Accept the request array itself as the `query batch` argument, alongside the existing file path and `-` for stdin, and name every accepted form in the unreadable-input error instead of reporting only the filesystem failure.

- Return usage and exit 0 from `index --help` when the `scan-index` launcher is absent, instead of a `missing_executable` error and exit 1 that also aborted `index --help && query --help` chains before the second command ran.

## 0.33.1

- Expand Python helper and hook contracts with executable examples, document actual hook failure boundaries, and remove skipped doctest blocks. Make ordinary test helpers and fixture implementations private while preserving fixture injection names.

## 0.32.0

- Add opt-in `rdeps --limit N` static-importer previews with explicit truncation and total metadata; keep the default and `--limit 0` exhaustive, leave dynamic/config results uncapped, and prevent previews from arming the exhausted-query sentinel.
- Carry the exact emitted `--index` path after custom-root scans in both host skills; with an explicit root, permit only its exact default or configured index-directory target when it resides outside the caller project.
- Align prompt-hook freshness with indexed Python, stub, RST, and nested Markdown changes through a tested writer/query/hook pathspec contract, including a nested prompt session detecting a root-level dirty stub.

## 0.31.2

- Restructure dense README integration, index-lifecycle, troubleshooting, query, rename, telemetry, and cross-plugin guidance into scannable lists and decision sequences while preserving every command, flag, exit/status meaning, benchmark caveat, completeness rule, and Claude/Codex boundary.

## 0.31.1

- Compress all six Codex and six Claude skill contracts without changing command syntax, routing, stop rules, safety gates, installed-root requirements, output guarantees, or Claude executable fences. Replace stale private-plan section references with self-contained constraints, including the shared contract documents' authority framing; the § labels remain as stable in-file anchors.

## 0.31.0

- **BREAKING:** replace `codemap-py integrate check` with the read-only `codemap-py integrate audit`; there is no compatibility alias. Update `/codemap-py:integration check` and `$codemap-py:integration check` invocations to `audit`, and consume `schema_version: 2` / `codemap-py.integration.v2` reports. `check` now exits `2` as an unknown subcommand. Unlike `check` (always exit `0`), `audit` exits `1` when any finding fails — and an existing v1 managed block reports `managed_block_invalid` until `plan` + `apply` rewrite it, so scripts treating nonzero as an error must be updated together with the invocation.
- Add audit JSON/text evidence for observed provider, consumer, managed-block, index, runtime-log, and usage state, stable findings, and non-executable remediation. Audit never invokes a mutation, refresh, query self-heal, or plugin-manager mutation; its only subprocesses are read-only probes (`claude|codex plugin list --json`, `git rev-parse`).
- Scope new telemetry to `logs/{claude,codex,direct}/`, stamp runtime/version and refresh provenance, and retain legacy flat records as unattributed input. The session marker moves from `current-session` to `current-session-<runtime>.json`; a mid-session mixed-version state (old hook with new CLI, or the inverse) loses the session join until both layers run the same version. Update debrief and avoidance analysis to recurse across flat and runtime trees, report per-runtime/unattributed metrics, and preserve topology during directory anonymization.
- Keep incremental documentation refreshes in freshness and documentation-reference evidence without creating degraded module records; the next bounded refresh self-heals the index. Audit now reports observed provider content identity, same-version content drift, an unobservable session catalog when native provenance is absent, Codex CLI/tool shards without a skill-start hook, per-runtime usage summaries, and unavailable token measurement because host hooks expose no tokens. These reports make no live fresh-session activation or token-savings claim.
- Align query guidance with the utilization guard: query first without a pre-scan, stop re-query/read/grep after a complete untruncated structural result, allow source-body reads for distinct implementation/runtime details, use only targeted fallbacks for named incomplete/degraded gaps, and route test-choice questions to `test-impact`.
- Keep the managed sentinel schema at `v1` while the managed-block body protocol becomes `codemap-py.integration.v2`. Apply remains source-only and sync remains runtime-only; one approved plan may serve both, since each command executes only its own operation kind and sync re-validates the installed runtime state immediately before every native operation.

## 0.30.1

- Rewrite the public README around the problem solved, Claude Code and Codex installation, first query, adaptive query selection, prerequisites, runtime differences, and static-analysis limits; remove duplicated run-specific benchmark tables while retaining links to the canonical benchmark record.
- Align `bin/README.md`, `scripts/README.md`, and the documentation-site wrapper with the current dispatcher, six-skill dual-runtime package, Python version gate, package validation, and install-probe contracts.

## 0.30.0

Staleness reporting and telemetry anchoring: the currency probe joins the read gate, and every staleness question — and every log shard — is now resolved from the repository root rather than from the process working directory.

- Anchor the telemetry log root at the project root through one resolver, `runtime_log.log_root()`, now used by the CLI layer, the query engine, and the runtime-scoped writer alike; `runtime_log.LOG_DIR_ENV` names the override key once instead of each layer repeating the literal. A session whose hooks fired at the repository root while a query ran from a subdirectory previously wrote the two halves of one session into `<root>/.cache/codemap/logs` and `<subdir>/.cache/codemap/logs`; neither half was an error, so the join simply returned nothing and `debrief-coding` reported the missing half as absent. The import-time `_LOG_DIR` constant in `query.py` is removed rather than repointed — a constant frozen at import could never see a `CODEMAP_LOG_DIR` exported afterwards.

- Anchor a **relative** `CODEMAP_LOG_DIR` to the project root as well. An absolute override is unchanged and still honoured verbatim; a relative one used to resolve against whatever directory the process started in, which reintroduced the same split the default suffered from. Set an absolute path to keep the previous behaviour.

- Report the index file a query actually loaded as `index.index_path`, captured at load time rather than recomputed from the resolver when the block is emitted. A consumer that compared its own probe path against a resolver-derived answer was comparing two runs of one function; this is the only value that can disagree with the resolver — a stale `CODEMAP_INDEX_DIR` in the querying process, a different git root, a self-heal that rewrote elsewhere — which is what makes it worth reporting. The field survives the coverage-block diet, since a consumer that only ever sees compacted blocks would otherwise never see it.

- Raise the index-size ceiling in `bin/check-index-currency`, `bin/scan-stats.py` and `bin/smoke_test_index.py` from 50 MB to the query engine's own 512 MB. A helper ceiling below the engine's does not fail safe: `check-index-currency` answered `no_index` — the same answer a project with no index at all gives — for any index above 50 MB, so the staleness gate silently stopped firing on exactly the large repositories it exists for. Measured on a real index of 131 MB.

- Ship `hooks/_hookutil.py`, holding the project anchor, the log-directory resolution, the project key and the session-key sanitizer the logging and sentinel hooks must agree on. All five hooks now import it instead of carrying their own copy: `project_name()` was duplicated three ways and `session_key()` twice, and a divergence between copies never raised — it wrote one file and read another, so the cross-layer join simply returned nothing. It is a deliberate copy of the rules in `runtime_log`, not an import: the hooks fire on every Grep/Read/Glob/Bash call and must stay free of package imports and subprocesses, so the two layers are held in agreement by test instead.

- Read the index under a shared read lease in `check-index-currency` instead of a bare `json.load`, so the launcher obeys the same gate as every other consumer. A live writer holding the index now answers `stale` (the honest verdict while a rebuild is in flight) and an unusable coordination root answers `no_index`; the 2 s lease covers the index parse only, never the Tier 2 source-tree walk.

- Anchor every staleness-related git subprocess at the git top-level rather than the process working directory. A query issued from a subdirectory previously compared subdirectory-relative paths against root-relative index entries, so it read every indexed file as deleted: the index reported permanently stale, self-healed on every call, and answered `query_complete: false` for the rest of the session. The same anchoring applies to the untracked-file scan and to the mtime check on indexed-but-untracked files, which silently found nothing from a subdirectory.

- Watch the writer's own file set in the timestamp fallback used for a pre-`file_shas` index — `*.py`, `*.pyi`, `*.rst`, `docs/**/*.md`, spelled once and shared with the SHA path. The previous hand-written pathspec (`*.py` minus `docs/`, `*.md`, `*.rst`) covered none of a changed `.pyi`, `.rst`, doc file, or a `.py` under `docs/`, each of which left the check reporting a fresh index.

- Report `stale_undetermined` when git fails inside a repository rather than presenting the resulting empty answer as proof of freshness. The coverage block gains the flag only in that case, `query_complete` returns false with a `stale_undetermined` reason, and stderr names the failure. Being outside a repository stays silent — staleness was never knowable there.

- Query git once per invocation for tracked blob SHAs instead of twice; the self-heal decision and the coverage block now read one memoized, self-consistent answer.

- Correct the README's `CODEMAP_INDEX_DIR` description, which still documented the root-keyed `<canonical-root-sha256>/<project>.json` layout that `0.29.4` retired in favour of the flat `<override>/<project>.json` convention.

- Correct the README's managed-block claims: consumer plugins ship the host file the block lands in, not a pre-applied block, so `/codemap-py:integration check` reporting `missing` before the first `apply` is the expected state and not a packaging defect.

- Port `bin/setup_scan_env.sh` to stdlib-only `bin/setup_scan_env.py`; the `.sh` remains as a deprecated `exec` shim (removal no earlier than `1.0.0`) so existing call sites keep working, and an integration test pins shim/port equivalence on every scenario. The port removes the `python3`-on-PATH dependency by loading `parse_scan_args` via `importlib`, and `format_scan_args()` is extracted so both consumers share one quoting rule.

- Convert the claude-skills dispatcher invocations to bare PATH-literal `codemap-py` (the `0.29.1` pattern) in `scan-codebase`, `test-impact` and `rename-refs`, retiring the `CM=`/`"$CM"` re-resolution ritual; codex-skills keep explicit plugin-root paths deliberately, since the Codex runtime has no `bin/` `PATH` entry.

- Correct four skill-prose sites that claimed the `scan-index`/`scan-query` aliases take no writer lease: every route leases inside the engine, so the prose now gives the real reasons to prefer the dispatcher — the interpreter probe (exit `127`) and the aliases' deprecated-shim status. The `inject-preamble` hook's stale comment claiming its detached scan was ungated is likewise corrected, and the hook's model-facing directive now names `codemap-py index`.

- Gate new code on cyclomatic-complexity and size limits (C901, PLR0911/0912/0915), scoped to this plugin from the repository-root ruff config via a negated per-file-ignore, with the six current offenders enumerated per file as explicit accepted debt.

## 0.29.4

Audit remediation across the index gate, the hook roster, and both skill rosters.

Index and RW gate:

- Retire the root-keyed `CODEMAP_INDEX_DIR` layout in favour of the flat `<override>/<project>.json` convention every writer already used, so the leased, written, loaded, and `doctor`-reported paths are one path.
- Take the read and write leases inside the engines (`query.main`, `graph.main`) rather than in their launchers, so the `codemap-py` dispatcher, `scan-query`/`scan-index`, the self-heal spawn, and hook background refreshes are all gated by construction. Callers must not wrap an engine invocation in a second lease.
- Report a corrupt index through the `codemap-py query` dispatcher as a bounded diagnosable error instead of a raw traceback, matching what standalone `scan-query` already did, and stop parsing the index twice per dispatched query.
- Implement the previously advertised version-skew refusal: a writer refuses to overwrite an index written by a newer schema generation instead of silently downgrading it.
- Align the writer's temp-file name with the orphan cleaner so a crashed writer's temp is reclaimed, and `fsync` the payload before the atomic rename.

! BREAKING — Two projects with the same directory name sharing one `CODEMAP_INDEX_DIR` no longer receive independent indexes; they resolve to the same file. Fix: give colliding projects separate override directories. The collision is reported as an `index_root_collision` diagnostic rather than silently serving another project's index.

! BREAKING — An unwritable index directory now fails with a structured `{"error": "index_coordination_unavailable"}` on stderr instead of the previous `[codemap] ERROR: …` text. Exit code and stdout are unchanged.

Hooks and telemetry:

- Invalidate the exhausted-query sentinel on `Edit`/`Write`/`MultiEdit`/`NotebookEdit` and expire it after 30 minutes, so a post-edit grep is no longer denied on stale authority.
- Anchor the redundant-scan guard's pattern to `grep`/`rg` so unrelated commands containing `import` are not denied, and scope its fallback session key per project and session.
- Route the `intent` and `target` fields through token-level scrubbing in `anonymize.py` — a dot-free command such as a plain `grep` was previously exported verbatim — and pseudonymize the session id in both the record and the exported filename.
- Bound the repeated-read telemetry check to a trailing window instead of re-reading the whole shard on every matched tool call.
- Scope recorded query completeness to the module actually queried rather than to any `query_complete` appearing anywhere in a combined tool response.
- Agree on one session key across the hooks (filesystem git-root walk), fixing telemetry joins that silently read zero from a subdirectory.

Skills and docs:

- Move the Claude `scan-codebase`, `test-impact`, and `rename-refs` skills onto the gated `codemap-py index|query` dispatcher, so both rosters share one execution path and one concurrency contract.
- Disclose the 20-item result cap, `--limit 0`, and the `confidence` field in both `query-code` rosters, which previously instructed agents to stop querying on a flag that reports graph coverage, not display truncation.
- Stop the Claude `test-impact` skill from rendering a failed query as "no affected tests"; a query failure now exits non-zero instead of falling back to empty defaults.
- Read anonymized copies in Claude `debrief-coding --anonymize` mode and delete the false claim that tool-use logs are never anonymized.
- Replace the undefined `$CODEMAP_BIN` in the Codex `query-code` skill, correct its `module::symbol` qname claim to match the source, drop the Claude `rename-refs` hard dependency on `jq`, and make report output paths collision-safe in both rosters.
- Extend the parity gate with execution-surface checks (CLI surface, exit codes, qname grammar, result caps, undefined launcher variables, output paths, error suppression) — it was previously blind to all of them.

Also: reconcile the `.claude-plugin` and `.codex-plugin` manifests, which had drifted to `0.29.3` and `0.29.2`. No `0.29.3` entry was ever recorded; that bump shipped in the Claude manifest alone.

## 0.29.2

- Clarify that complete structural lookup resolves only its graph fact: lifecycle-boundary edits must inspect source plus the named test/oracle and use one directional caller/callee query only when that responsibility remains unresolved.

## 0.29.1

- Keep structural queries in the caller's current repository, define returned relative paths against that repository rather than the installed Skill directory, and stop redundant query/source verification after `query_complete: true`; Claude prefers the literal PATH-resolved `codemap-py query` command so headless permission matching does not reject environment expansion, while the installed absolute launcher remains an interactive fallback and query-code permits only those query commands plus the documented Read/Write rendering path.

## 0.29.0

- Route direct callers and same-name implementation candidates to task-fit compact queries, while requiring source verification for inheritance claims.
- Skip structural retrieval for fully localized edits with no unresolved graph or source-slice fact, while preserving explicit tool requirements and the smallest relevant query for uncertain scope.

## 0.28.8

- Repair structural query routing for multi-query diff-impact evidence and broken Sphinx cross-references while preserving compact managed-run evidence boundaries.

## 0.28.7

- Replace closed integration, query, schema, scanner, and wrapper option strings with Python 3.10-compatible string enums without changing CLI or JSON wire values.

## 0.28.6

- Preserve static reverse-import edges for relative imports and known `from package import submodule` forms without adding false module edges for symbol imports.

## 0.28.5

- Harden cross-platform test isolation and publish synchronized structural and agentic benchmark evidence.

## 0.28.4

- Harden runtime state handling, temporary-file safety, path containment, and cross-runtime skill guidance after the plugin audit.

## 0.28.3

- Add actionable hints for invalid query commands and align mirrored production-importer, feature-scaffolding, and symbol-routing guidance with the supported CLI contracts.

## 0.28.2

- Improve structural query fidelity for source-root aliases, centrality, direct-import routes, and compact coverage results.

## 0.28.1

- Correct Codex and Claude query-skill guidance for direct-import routes used by structural validation.

## 0.28.0

- Add alias-aware import and symbol resolution across scanning, graph construction, structural queries, and integration probes.

## 0.27.3

- Clarify query command contracts and improve structural-query discoverability for Codex.

## 0.27.2

- Streamline all six Codex skills around the native plugin-root integration contract.

## 0.27.1

- Harden frozen structural queries and compact coverage output while preserving complete-result metadata.

## 0.27.0

- Port all six Claude hooks to stdlib-only Python: session seeding, exhaustive-query recording, redundant-scan guard, preamble injection, skill-start telemetry, and tool-use telemetry. The guard and recorder retain their shared per-session sentinel contract. The preamble uses an atomic exclusive refresh lock and a platform-specific detached spawn; its Windows process-group branch is acceptance-tested. Claude hook wiring now launches Python helpers; Codex still declares no hook.
- Remove the retired installed-cache injection implementation and its audit/tests. The integration engine is the sole source-wiring path: `build_plan`/`apply_plan` write authenticated blocks only at finalized checked-in targets. Migration utilities for plugin-root and dual-identity detection now live in `codemap_py.index_paths`; the legacy post-commit installer is removed, while `resolve_proj_index.py` and `smoke_test_index.py` remain live skill dependencies.
- Finalize the engine target map: oss writes `skills/_shared/codemap-gates.md`; foundry, develop, and research write `skills/_shared/codemap-context.md`; codex-rig writes `shared/codemap-py-integration.md`. Root `sync.sh` stays the whole-repo aggregate installer; retiring it to a `codemap-py integrate` exec adapter is deferred to a follow-up, since `integrate` covers only the codemap-integration set, not sync.sh's full install scope.

## 0.26.0

- Make the `integrate apply` engine byte-exact on Windows: `_atomic_write` now writes the managed block in binary mode so the on-disk bytes stay LF-only on every OS, matching the plan's expected post-state hash and the block's own embedded SHA-256 stamp (text-mode writes translated `\n` to `\r\n` on Windows, corrupting the self-authenticating marker and failing the post-write hash check); source-write rollback likewise restores the exact original bytes.
- Decode every `git`/native-CLI subprocess with `encoding="utf-8"` (`canonical_root`, `_git_dirty`, `_run_native`, and the JSON probe) so a project path containing non-ASCII characters resolves correctly on Windows instead of being mangled by the console code page and misrouting the dirty-overlap and identity checks.
- Seed the integration test fixtures LF-only and disable `core.autocrlf` in the disposable fixture repos so the suite is byte-deterministic across platforms; add a test asserting a freshly applied managed block contains no `\r\n`.
- **Dual-runtime skill parity.** `codemap-py` now ships a Codex skill roster under `codex-skills/` alongside the existing Claude roster under `claude-skills/`, and the Codex manifest points `skills` at `./codex-skills/`. Both runtimes expose the same six skills — `scan-codebase`, `query-code`, `test-impact`, `rename-refs`, `integration`, and `debrief-coding` — with identical truth-claims (inputs, outputs, exit codes, completeness metadata, caveats), differing only in runtime-specific invocation (Codex resolves an explicit plugin-root path and has no `bin/` PATH or `AskUserQuestion` tool). A shared capability contract (`shared/capability-contract.md`) is the single source of those truth-claims, and a parity test rejects missing skills, stale command names, unsupported cache paths, or contradictory limits. Codex still ships no hooks in this release — a documented automation limitation, not hidden parity.
- **Native `integrate` control plane** (`codemap-py integrate check|plan|apply|sync|demo`, surfaced as `/codemap-py:integration` and `$codemap-py:integration`). `check` is a read-only health report; `plan` persists an inspectable artifact (exact targets, before-state hashes, argv arrays, ordered operations, rollback identities) bound by a SHA-256; `apply` updates only sentinel-bounded managed blocks inside allowlisted consumer source files (marker `<!-- codemap-py:integration:begin v1 sha256=… -->`), preserving everything outside the block byte-for-byte and refusing foreign/modified markers, path escapes, symlinks, installed-cache roots, or dirty overlap; `sync` runs only the approved native plugin-manager argv and never mutates source or global instructions; `demo` records disposable evidence. Every mutation is dry-run-first, hash-approved, journaled with before-images, and rolled back on partial failure, ending `recovery-required` if a rollback cannot be verified. This replaces the retired installed-cache `init` injection model.
- The Claude `integration` skill was rewritten from the retired `check|init|demo` model to `check|plan|apply|sync|demo`; the legacy demo A/B measurement helper was removed. The package builder now ships `codex-skills/` and `shared/` and records the Codex skill roster in the manifest, and the package validator now enforces Codex six-skill parity in place of the former zero-roster rule. Both plugin manifests move to `0.26.0`.

## 0.25.1

- Extracted the two monolithic `bin/` executables into an importable `src/codemap_py/` package: `scanner` (Python-file discovery and single-file AST parsing), `graph` (import/call/test/fixture/docstring graph, coverage, and test-impact construction plus scan orchestration), `query` (query dispatch and rendering), `cli` (the shared dispatcher), and the schema/index-path/read-write-gate/ logging/telemetry cores. `bin/scan-index` and `bin/scan-query` are now thin launchers over the package; `python -m codemap_py` reaches the same dispatcher. The legacy `bin/_*.py` module names remain as compatibility shims, so existing imports and monkeypatches keep working unchanged. No CLI behavior, output bytes, or exit codes changed — the move is byte-for-byte parity-tested against the pre-extraction bytes.
- **`.pyi` type stubs now participate in analysis** (plan §2.1 scope extension). A sibling `module.py` stays authoritative and its `module.pyi` is recorded as a shadowed stub rather than indexed twice; a `module.pyi` with no implementation is indexed once as a stub-only module contributing declarations and imports but no call edges; `package/__init__.pyi` follows the same precedence rule; case-fold path collisions fail closed identically on every OS. Editing a `.pyi` now invalidates the index. The first scan after upgrading rebuilds each index once (new discovery set), then reuse is stable; the on-disk index schema and `.cache/codemap/` path are unchanged.
- Reorganized the test suite into subsystem subfolders with shared fixtures under `tests/data/` (test-only; nothing ships in the package).

## 0.25.0

- **Renamed the product and plugin identity**: `codemap` → `codemap-py`, directory `plugins/codemap/` → `plugins/codemap-py/`. **! BREAKING**: the Claude skill namespace changed from `/codemap:*` to `/codemap-py:*` — renaming one plugin manifest cannot keep a second namespace alive, so every saved prompt, alias, or automation invoking the old triggers must move to the new namespace. `scan-index`/`scan-query` compatibility aliases, the `.cache/codemap/` project cache, and every `CODEMAP_*` environment variable are unaffected and keep working exactly as before.
- Added a dual-runtime package layout: a Claude Code manifest (`.claude-plugin/plugin.json`) alongside a new Codex manifest (`.codex-plugin/plugin.json`). The Codex manifest ships with **no skill roster yet** — Codex skill parity lands in a later `0.25.x` release. Both manifests share the same product identity and SemVer version.
- Carried forward the `codemap-py` CLI (`codemap-py index` / `codemap-py query` / `codemap-py doctor`), the process-safe shared-index read/write gate, and the runtime-neutral index-identity resolver introduced under `codemap` `0.24.1`, now packaged under the renamed identity.
- Removed `hooks/sentinel-read-allow.js` from the shipped payload. That shared auto-allow hook is canonical to, and now lives only in, the `cc_foundry` plugin; installs without `cc_foundry` see an ordinary Bash permission prompt for sentinel-read compounds instead of auto-allow — a UX difference only, never a correctness one.
- Added `LICENSE` (Apache-2.0) and `NOTICE` to the package payload.

## Predecessor

Continuity note: `codemap-py` `0.25.0` succeeds `codemap` `0.24.0`/`0.24.1` directly — same maintained product and release train, not a new `0.1.0` line and not yet a `1.0.0` stable line. See [Upgrading from codemap](README.md#upgrading-from-codemap) for the migration and rollback procedure.
