---
name: query-code
description: Query Codemap.
---

> Before asking, read ../../shared/codex-user-questions.md.

NOT for: $codemap-py:scan-codebase, $codemap-py:rename-refs, $codemap-py:test-impact.

Test-impact split: one-off structural fact → `test-impact <target>` here; full workflow → $codemap-py:test-impact.

## Runtime note

No plugin-root variable or shell persistence. Resolve the installed launcher once as `PLUGIN_ROOT/bin/codemap-py`; retain its literal.

## Workflow

Exact file+symbol local edit: skip Codemap only with no unresolved caller/dependency/blast-radius/test-impact/import/source slice. Lifecycle boundary (callback/hook, cancellation/exception, scheduling/cleanup, state transfer): inspect source + named test/oracle; `fn-rdeps` for caller, `fn-deps` for callee. Explicit structural/tool requirement overrides. Choose the smallest complete query set.

| Need | Query |
| -- | -- |
| production module importers / blast radius | `rdeps <module> --exclude-tests` |
| production centrality / highest in-degree | `central --top N --exclude-tests` |
| rank/threshold a known candidate set | `central --among <a,b,c> --exclude-tests` |
| internal-import coupling (not centrality) | `coupled --top N` |
| direct production callers | `fn-rdeps <module::symbol> --exclude-tests` |
| callers plus test-module importers | `fn-rdeps <module::symbol> --exclude-tests`, then `rdeps <module>` |
| transitive callers / function blast | `fn-blast <module::symbol>` |
| broken Sphinx cross-references | `xrefs --broken <module>` |
| test/doc gaps; package inventory | `coverage-gap [module]` · `undocumented [module]` · `packages` · `list --limit 0` |

Routing shortlist, not the parser's full surface. Known syntax: no preliminary help/doctor/scan/freshness. Unknown argument: `query <subcommand> --help`; unknown operation: `query --help`. Never guess. Direct/every/all/production/blast-radius callers → `fn-rdeps <module::symbol> --exclude-tests`; `fn-blast <module::symbol>` only for explicit transitive, closure, hops, or all-levels requests. `test-impact <target>` selects transitive tests; direct test-module imports: `rdeps <module>`, then filter/report tests.

`symbol <name>` accepts `authenticate` or `MyClass.method`; imports: `symbol <name> --with-imports`; `module::symbol` belongs to `fn-*` call-graph queries (dotted/unique bare resolve, else `candidates`; `X.__init__` adds only `X()` sites). Chain `module`+`qualified_name` → `<module>::<qualified_name>` (`mypackage.module::MyClass.method`). Requested qualified extension method: `symbol MyClass.add_feature`, not nearby `symbol MyClass`/`symbols <module>` listing.

`find-symbol '<ClassSuffix>\.<method>$' --exclude-tests --limit 0` finds same-name override candidates, not inheritance proof; verify ancestry/package boundaries in source.

Run each compact query alone: `PLUGIN_ROOT/bin/codemap-py query --compact <subcommand> [arguments]`. An ordinary query prepares the canonical project index before answering, including when no index exists yet; run queries serially while that preparation may occur. With inherited `SCAN_NO_AUTOBUILD=1`, queries leave the existing index unchanged and independent read-only queries may run concurrently against that frozen index. Dependent queries wait for their inputs. To query another project's canonical index, pass `--root <root>`. To select a specific index file, pass `--index <path>`; add `--root <same-root>` when it also defines the query root and accepted custom-index boundary.

`fn-blast`: never `--depth`; never invent flags. A complete, untruncated result settles its own graph fact, not distinct sibling queries; do not re-query/read/grep that same fact. Complete-query paths are caller-repo-relative, never Skill-relative. Ordinary repository reads remain allowed for a distinct independent AST/oracle view or source-body implementation/runtime detail. Else name/target only the gap. If frozen mode reports a missing index, report the structured error and continue without structural context; the user may explicitly run `$codemap-py:scan-codebase` if they want to create it. Never invoke that skill to repair an ordinary query.

Counts and scoped rankings come from the query, never hand work on its output: no counting a returned list, no subtracting one call from another, no eyeballing repo-wide `central` against a candidate set. Rank a set from a prior result with `central --among`; `unmatched` names what it skipped. `rdeps --exclude-tests` reports `importer_count` and `excluded_test_importer_count` together.

Truncation at 20 items is a real cap (`symbol`/`find-symbol` default); `--limit 0` removes it. `rdeps --limit N` previews static `imported_by`; default/`--limit 0` is exhaustive. `dynamic_imported_by`/`config_refs` stay exhaustive. `query_complete` is graph coverage only: true may mean 20-of-N. `index.confidence`: `exact` whole set, `partial` capped/stale; `index.truncated`+`index.total_available` give N. Truncated `rdeps` never settles exhaustive callers.

No arbitrary total-call cap for needed facts. Bounded targeted correction retries per query; if the same correction failure recurs, stop and report the unfinished fact. Never retry a completed fact.

Coverage: exact-module, not descendants. Package-wide: enumerate indexed modules, include package itself, batch once; `--all` = all non-test modules. Keep `selection`/`measurement`: empty/unavailable/partial/available differ; no findings ≠ measured coverage. `uncovered` = static test-call/mock links, not line coverage. Preserve untracked/degraded warnings; no static callers ≠ unused; method `hint` → grep `\.method\b`; dunder `hint`: implicit call, no search.
