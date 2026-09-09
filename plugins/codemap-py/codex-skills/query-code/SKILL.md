---
name: query-code
description: Query Codemap.
---

NOT for: $codemap-py:scan-codebase, $codemap-py:rename-refs, $codemap-py:test-impact.

Test-impact split: one-off structural fact → `test-impact <target>` here; full workflow → $codemap-py:test-impact.

## Runtime note

No Codex plugin-root variable or shell persistence. Resolve the installed launcher once as `PLUGIN_ROOT/bin/codemap-py`; retain its literal in reasoning.

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

Routing shortlist, not the parser's full surface. Known syntax: no preliminary help/doctor/scan/freshness. Unknown argument: `query <subcommand> --help`; unknown operation: `query --help`. Never guess. Direct/every/all/production/blast-radius callers → `fn-rdeps <module::symbol> --exclude-tests`; `fn-blast <module::symbol>` only for explicit transitive, closure, hops, or all-levels requests. `test-impact <target>` selects transitive tests; direct test-module imports: `rdeps <module>`, then filter/report tests.

`symbol <name>` accepts `authenticate` or `MyClass.method`; imports: `symbol <name> --with-imports`; `module::symbol` belongs to `fn-*` call-graph queries. Chain `module`+`qualified_name` → `<module>::<qualified_name>` (`mypackage.module::MyClass.method`). Requested qualified extension method: `symbol MyClass.add_feature`, not nearby `symbol MyClass`/`symbols <module>` listing.

`find-symbol '<ClassSuffix>\.<method>$' --exclude-tests --limit 0` finds same-name override candidates, not inheritance proof; verify ancestry/package boundaries in source.

Run each compact query alone: `PLUGIN_ROOT/bin/codemap-py query --compact <subcommand> [arguments]`. Independent read-only queries may run concurrently as separate commands on a prepared stable index with self-heal disabled (`SCAN_NO_AUTOBUILD=1`); dependent queries wait. Refresh/self-heal/index writes run serially. Custom-root index: `--index <emitted-index-path> --root <same-root>`; `--root` is path resolution only.

`fn-blast`: never `--depth`; never invent flags. A complete, untruncated result settles its own graph fact, not distinct sibling queries; do not re-query/read/grep that same fact. Complete-query paths are caller-repo-relative, never Skill-relative. Ordinary repository reads remain allowed for a distinct independent AST/oracle view or source-body implementation/runtime detail. Else name/target only the gap. Missing index: request $codemap-py:scan-codebase.

Counts and scoped rankings come from the query, never hand work on its output: no counting a returned list, no subtracting one call from another, no eyeballing repo-wide `central` against a candidate set. Rank a set from a prior result with `central --among`; `unmatched` names what it skipped. `rdeps --exclude-tests` reports `importer_count` and `excluded_test_importer_count` together.

Truncation at 20 items is a real cap (`symbol`/`find-symbol` default); `--limit 0` removes it. `rdeps --limit N` previews static `imported_by`; default/`--limit 0` is exhaustive. `dynamic_imported_by`/`config_refs` stay exhaustive. `query_complete` is graph coverage only: true may mean 20-of-N. `index.confidence`: `exact` whole set, `partial` capped/stale; `index.truncated`+`index.total_available` give N. Re-run capped lists with `--limit 0`; truncated `rdeps` never settles exhaustive callers.

No arbitrary total-call cap for needed facts. Bounded targeted correction retries per query; if the same correction failure recurs, stop and report the unfinished fact. Never retry a completed fact.
