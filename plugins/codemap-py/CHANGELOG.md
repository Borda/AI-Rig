# Changelog

## 0.39.6

- Security: fixed a decorator-injection bypass and a source-code leak in the telemetry anonymizer (found by this release's own residual adversarial scan, beyond the audit's original scope).
- Fixed 30 audit findings across skills, bin/ scripts, and rules (model-tier mismatches, cross-reference drift, doc/behavior contradictions); see `.reports/audit/2026-09-22T22-18-03Z/fix-summary-codemap.md` for full detail.
- `foundry:audit`'s bin/ test-coverage check (M34) now falls back to a category-nested `tests/<category>/test_<name>.py` layout when the flat path is missing, fixing false positives against this plugin's test layout.

## 0.39.5

- Security: the prompt hook validates the index header's `git_sha` as a hex object name before passing it to `git diff`. A planted `.cache/codemap/<project>.json` with an option-shaped `git_sha` (for example `--output=<path>`) could otherwise make the hook write a file at that path on every prompt that starts a refresh; such a value now yields an unknown `changed_count` without any git call. A count that fails for any other reason leaves the count unknown and the refresh still starts, so the refresh lock can no longer leak.
- `changed_count` on hook-triggered refreshes records the eligible staged-path delta against the index commit (`git diff --cached --name-only -z`), applying built-in and configured scanner exclusions while retaining indexed documentation paths. Unstaged and untracked files are outside this count; it is not a measured count of reparsed files. NUL-delimited paths avoid quoting and space-related miscounts; unavailable or invalid revisions remain unknown.
- Tool telemetry retains Grep/Glob `search_path` and producer-observed `search_scope` beside the pattern `target`. The join classifies single-file searches as `source_read`, directory searches as `structural_search`, and missing or indeterminate legacy scope as `unknown`; `unknown_count` is reported overall and per runtime beside `structural_search_count`. `anonymize.py` scrubs search paths. The classifier also recognizes `egrep -r`/`fgrep -r`, rejects backup-file names as own-file evidence, and serializes `OverlapKind` values. The legacy overlap count remains a proxy, not confirmed misuse or measured savings.
- Recursive-looking Bash searches outside own-file inspection are classified as `unknown`: command spelling alone cannot establish directory scope, even when `rg` or a recursive grep flag appears. Debrief guidance on both hosts explains this conservative classification, unknown legacy search scope, static-analysis blind spots and the 200-character Bash logging limit; it does not use a release-time overlap tally as product behavior.
- Query and index invocations retain their target project — the explicit `--root`, or the index's recorded scan root when none is given — for telemetry placement, project identity and Claude marker lookup, including when launched from another repository. Runtime identity and terminal outcomes remain unchanged.
- Debrief guidance (both hosts): completeness fields live under `result.index`; `completeness_reason` is absent by design on compact complete answers; `not_covered` is a fixed per-method blind-spot list, never a coverage-gap fraction; timing is reported for queries and index refreshes separately; records without `v` are excluded from recent cohorts by default; zero skill starts beside CLI volume is the expected direct-CLI shape.
- Query routing tables (both hosts) list `packages` and `list --limit 0` for repository-shape questions; the Codex table gains the coverage/documentation-gap row it lacked.
- Test isolation clears `CLAUDE_CODE_SESSION_ID`, `CSID` and `CODEX_THREAD_ID` so seeded-session join tests do not inherit the outer host session.

## 0.39.4

- Shared Claude context contract v4 keeps failed/missing pre-flight queries partial, requires explicit completeness metadata, and preserves per-child batch limits, source verification and static-versus-measured coverage distinctions.
- Record handled query/index terminal outcomes once, including argument, gate, and timeout failures; retain exit codes and best-effort opt-out logging. Clear engine-owned timeout alarms and restore caller timers on return.
- Preserve payload-only Codex/Claude session identity in refresh children, honor Claude environment-session fallback in hooks, and retain the Codex background-refresh trigger. Missing Codex identity never borrows a project-global session marker.
- Distinguish unavailable, partial, empty, and measured line coverage; report exact-module versus all-module selection without changing recursive scope.
- Record project identity on new telemetry; require explicit project coordinates and successful CLI outcomes for joins. Count failed/unjoinable batch children separately. Label changed eligibility as `module_overlap_proxy_v3`, never confirmed misuse or token savings; synchronize both hosts' query/debrief guidance. Old logs remain untouched and ineligible legacy records stay visible in raw counts.

## 0.39.3

- Restore the caller's temporary-directory environment and Python temp cache after the resolver doctest, preventing order-dependent failures in downstream plugin tests.
- Share concise root-owned Codex question guidance with conditional approval and recovery details; retain plugin-local payloads and native presets plus custom input.

## 0.39.2

- Recover rejected Codex question controls through an eligible alternate route without repeating report context; distinguish explicit host plain-text requirements from missing tools and preserve pending consent.

## 0.39.1

- Fall back to permitted synchronous input for optional questions when async is unavailable or unsuitable.
- Require permitted native Codex question controls for user choices, including generated scope expansions, repair approvals, finding selection, and commit modes. Use async when sync is unavailable or unsuitable, even without independent work; keep required answers pending and use plain chat only when neither control is suitable.
- Present complete actionable options or native free text, preserving existing authorization, exact-digest syntax, and separate runtime permissions.

## 0.39.0

- Consumers now read `claude-skills/_shared/codemap-gates.md` and `claude-skills/_shared/codemap-context.md` live from the active `codemap-py` install instead of shipping manifested copies. Every Claude consumer (`foundry`, `develop`, `oss`, `research`) resolves the contract through its own `resolve_shared_path.py codemap-py claude-skills/_shared` — the install record picks the version, and the resolver's newest-cache tier (which skips `.orphaned_at` dirs) is reached only when no usable install record exists or the recorded install lacks the subdir — and the five `codemap-py--*.md` copies are gone. A frozen copy drifted silently against whatever `codemap-py` the user actually ran; a live read cannot. Behavior deltas: a `codemap-py` CLI on `PATH` without the plugin installed used to load the consumer's frozen copy and now takes the consumer's fallback line instead, which is the intended degradation since the gates only make sense with the plugin's hooks present; loading the contract now needs `python` on `PATH` in every consumer (develop and research previously read their copy without an interpreter); and a source checkout with `codemap-py` installed reads the installed contract, not the checkout's — edit-and-run against `claude-skills/_shared/*.md` needs a reinstall or an uninstalled provider to hit the source-tree tier. The contract bodies are unchanged by this switch (the `v3` changes below are separate); only the consumer headers and intro lines of both files were rewritten, plus one example in the context contract's `partial` completeness bullet that named a semble fallback no consumer ships any more. The resolver fixes (un-prefixed source-tree fallback, registry lookup under `sys.executable` instead of a `python` PATH search) live in foundry.
- Add `LAZY_CODEMAP` env var to the shared Gate B (stale-index) contract. Default (unset): rebuild the stale index in the foreground without asking, since a confirm-first prompt on every stale scan was pure friction on a fast rebuild — an ambient writer-lease conflict (`index_busy`) now falls back to continuing with the stale index instead of blocking or looping, rather than leaving no fallback. Set `LAZY_CODEMAP` to restore the previous confirm-first `AskUserQuestion` behavior, e.g. on a repo where rebuild cost is high. Bumps the contract to `v3`; every consumer plugin (`cc_develop`, `cc_oss`, `cc_research`) picks up the default flip through the live contract read above once this version is installed — until then an installed `v2` still asks first.

## 0.38.1

- Compress skill and shared-contract prose to the ultra-caveman tier for both hosts; behavior, test-pinned contract sentences, and structural literals are unchanged.

## 0.38.0

- Add capability-aware native Codex questions across every Codex skill: prefer permitted synchronous controls for required decisions, asynchronous controls for independent follow-ups or lossless fallback, and plain chat when unsupported.
- Use Approve/Deny for conversational authorization while preserving exact-digest and runtime permission boundaries. Bind delayed answers to immutable scope, reject superseded/duplicate replies, retain all feasible choices, and avoid duplicate live prompts.
- Ship plugin-local question guidance and regression coverage; preserve existing authorization and host-specific behavior without changing installed settings or requiring a Codex upgrade.
- Label one evidence-backed first choice `(Recommended)` in option-based questions; preserve canonical answers, exact confirmation syntax, and explicit consent.

`codemap-py` is the renamed, direct successor to the `codemap` plugin. The maintained product and its SemVer history continue across the rename; only the plugin identity, repository directory, and skill namespace change. Pre-`0.25.0` history was recorded as `codemap` under `plugins/codemap/` — see the repository git history for that line; it is not reproduced here.

## 0.37.1

- Keep the shared Claude/Codex prompt hook silent and skip refresh when scanner exclusions leave no indexable `.py` or `.pyi` sources. Missing-index guidance retains the bounded `__init__.py`/`pyproject.toml`/`setup.py` marker condition, so marker-free scripts remain manual without imposing a universal `__init__.py` requirement; existing indexes with real source retain normal behavior.

## 0.37.0

- Add `--format {json,tsv}` to `scan-query`. JSON stays the default and is unchanged, so nothing parsing stdout today is affected. `tsv` names the columns once in a header line instead of repeating every key on every row: measured on a 100-row `central` result, 3588 tokens of JSON against 2130 of TSV, a 40.6% reduction on the payload an agent reads into context. Formatting is applied in the single stdout seam rather than at each emitter, so every command that returns a table honours the flag; converting emitters individually left most commands silently answering in JSON while the caller had asked for TSV.
- Refuse `--format tsv` for any result that is not a single table of flat, uniform records — several candidate lists, ragged rows, a nested value in a cell, a bare list of strings, or an empty list. Stringifying a nested value would produce a cell no consumer can parse back, which fails silently; the refusal exits non-zero with a JSON error instead. Flat name lists such as `rdeps` are excluded deliberately: JSON already encodes them within 9% of a newline-separated list, so there is nothing to win.
- Write the metadata envelope to stderr under `--format tsv`, keeping staleness and completeness flags reachable. Dropping it would make a stale or incomplete answer indistinguishable from a good one.
- Keep every error object as JSON on stdout regardless of `--format`, since `{"error": ...}` is the shape callers already parse, and leave batch and `diff-impact` subqueries on JSON: the batch driver owns the one real stdout write and re-parses each captured subquery.
- Write TSV bytes as UTF-8 with explicit `\n` through `sys.stdout.buffer`. Windows text-mode stdout rewrites a newline embedded in a quoted cell to CRLF, and a legacy console encoding raises on a non-ASCII path; JSON escapes both cases and TSV does not.
- Request `--format tsv` from the batch pre-flight in `claude-skills/_shared/codemap-context.md`, for `central`, `coupled`, `fn-rdeps` and `fn-blast` only. The flag was inert before this: every skill call site funnels through the pre-flight's `_cq`, and nothing passed `--format`. The four are the commands whose result is one table wide enough for a header to pay for itself. `rdeps` and `test-impact` exit 1 `format_not_tabular`, which `_cq` reads as a miss and downgrades the run's completeness for. `symbol` does render as a table, but a one-row one whose header roughly equals its payload and whose `source` field — a whole function body — would become a single quoted multi-line cell.
- Read the metadata envelope from stderr in `_cq`, which previously discarded it with `2>/dev/null`. Staleness and completeness are detected by substring-matching the envelope, and the contract grants consumers permission to skip re-querying when the run reports `completeness=exhaustive`; a stale index whose envelope went unread would have earned exactly that verdict. Emptiness is now judged on whichever stream carries the envelope for the format in use, so a TSV query that matched nothing counts as an empty table rather than a failed retrieval.
- Probe `scan-query --help` once per pre-flight for `--format` before using it. The flag is new in this release, and an older `scan-query` on PATH answers an unknown option with argparse exit 2 and an empty stdout — a miss on the four commands worth the most.

## 0.36.0

- Prune excluded directories while detecting the source root, instead of sweeping the whole tree and filtering afterwards. `_detect_src_root_from_init` paired two unbounded `rglob` calls with a post-hoc `SKIP_DIRS` filter, so `.venv`, `.git`, and every other directory the filter would later discard was walked in full — on one repository, 124996 directories where pruning visits 9847 — and the sweep ran twice, once per init-file pattern. Detection there measured 15.3s, falling to 0.26s with no configuration at all; the cost was unpruned traversal and the double sweep, not any one large subtree. A full scan went from 21.4s to 5.9s, and an incremental scan that finds nothing to do from 15.9s to 0.35s. The second figure is the one that mattered — a no-op refresh that took longer than the query engine's own 10s self-heal timeout could never complete, so every self-heal was killed at the cap having healed nothing.
- Detect the source root deterministically. Candidates were collected into a `set` and read back by iteration order, so an unchanged tree resolved to a different root between runs of the same command under a different `PYTHONHASHSEED`. Candidates are now ordered, the shallowest `src` wins, and both the `src` search and the depth fallback break ties on the path. Depth decides before the alphabet does: selecting the first `src` in sorted order is reproducible but arbitrary, letting a vendored `a/src` beat a top-level `src`.
- Consult `[tool.codemap] exclude` and `.codemapignore` when detecting the source root. Detection previously ignored both, so an excluded subtree could be elected the root of the very index it is excluded from — on this repository a snapshot under `benchmarks/results/` won it. This governs which root is selected, not scan speed; the pruning above delivers the speed on its own.
- Add `rwgate.writer_active`, an advisory probe reporting whether a live writer holds intent for an index. It is not a lease and never acquires one: a caller asking it is deciding whether to *start* work, not whether a read is safe.
- Stand down from a self-heal while another writer is already running, rather than spawning a second `scan-index` that can only queue behind the first and then be killed at the heal timeout, having healed nothing. This covers the writer that appears after a query has taken its read lease — including a second query's own heal, which is how parallel queries used to stack scans — and not the writer already running when the query starts: that one the read lease waits out, after which the index is fresh and no heal is attempted. The query answers from the current index, flagged as before.

## 0.35.1

- Make shared guidance independently installable through synchronized consumer-owned copies, and anchor the reference context batch's default index to the repository root.

## 0.35.0

- Add `query central --among <modules>`, which ranks only the named modules by their own in-degree instead of the whole repository. It answers the question left over after an importer query — order or threshold *these* modules — which previously had no query behind it: callers either issued one `rdeps` call per candidate and counted the returned lists, or filtered a repository-wide `central` ranking against their candidate set by hand. Requested modules the ranking does not cover are returned as `unmatched`, and `candidate_count` states the scoped set's size, so neither a typo nor an explicit `--top` can drop a candidate silently. Without `--top`, a scoped ranking returns every candidate rather than the repository-wide default of ten.
- Report `importer_count` from `query rdeps`, and `excluded_test_importer_count` when `--exclude-tests` is set, so the production and test importer totals both come from one call. Deriving the test count by subtracting a filtered call from an unfiltered one put an arithmetic step outside the tool, where an off-by-one is indistinguishable from a wrong graph. `importer_count` is the total before any `--limit` truncation.
- Break ties in the repository-wide `central` ranking by module name, matching the `--exclude-tests` path. Equal-count modules previously came back in index order.
- Add `CODEMAP_COORDINATION_DIR`, which moves the read/write gate's `.index-rw` skeleton to a named directory while leaving the index where it resolved. It serves a deployment whose index directory cannot hold lock state — a read-only or shared mount, or a sandbox that grants write access to the gate alone — where the existing `CODEMAP_INDEX_DIR` was the only lever and moved the index along with the locks. The path resolver and the gate now derive the directory through one shared rule, so the directory a caller leases is always the directory the gate initialises. One index per override directory: two projects pointed at the same one share a registry mutex and serialise against each other.

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
