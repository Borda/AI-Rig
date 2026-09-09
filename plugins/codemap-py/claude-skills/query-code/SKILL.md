---
name: query-code
description: >-
  Query Codemap's Python structural index for dependencies, callers, paths, symbols, blast radius, test impact,
  mocks, fixtures, subprocesses, and static gaps. Trigger for "what depends on", "who calls", "imports of",
  "dependency graph", or "blast radius". Skip for renames, text search, non-Python repositories, or index rebuilds.
argument-hint: <rdeps|deps|path|central|coupled|symbol|symbols|find-symbol|fn-rdeps|fn-deps|fn-blast|diff-impact|test-impact|mock-rdeps|fixture-rdeps|fixture-graph|subprocess-deps|subprocess-rdeps|coverage|coverage-gap|undocumented> ...
allowed-tools: Bash(codemap-py query:*), Bash(*/bin/codemap-py* query:*), Read, Write
model: haiku
effort: low
---

<objective>
Answer structural Python questions via unified `codemap-py query` CLI.

NOT for: rebuilding the index (use `/codemap-py:scan-codebase`), renaming symbols (use `/codemap-py:rename-refs`), or which tests cover or are affected by a change (use `/codemap-py:test-impact`). </objective>

Test-impact split: a one-off structural fact ("which tests would this touch?") uses table subcommand `test-impact <target>`; full affected-test workflow (index ensure, JSON parse, `pytest` command, `not_covered` caveat) uses `/codemap-py:test-impact`. NOT-for defers workflow, not subcommand.

Skip Codemap when exact file + symbol localize edit and no caller, dependency, blast-radius, test-impact, import, or source-slice fact remains open. Lifecycle boundary—callback/hook, cancellation/exception, scheduling/cleanup, state transfer—keeps scope open: inspect source + named test/oracle, then query `fn-rdeps` for caller or `fn-deps` for callee responsibility. Explicit structural query/tool requirement overrides skip. Otherwise use smallest complete query.

<workflow>

## Choose the smallest complete query set

"Affected if X changes" = reverse dependencies. Run every query from the caller's current repository; working directory selects project index. Do not `cd` into `$CLAUDE_PLUGIN_ROOT` or plugin directory.

```bash
codemap-py query --compact <subcommand> [arguments]
```

For a custom-root index, pass `--index <emitted-index-path> --root <same-root>`; `--root` is path resolution only and does not select the index.

Enabled plugin adds version-matched `bin/` to Bash `PATH`. If unavailable interactively, invoke installed plugin's absolute `bin/codemap-py` launcher as one standalone command and accept normal host permission prompt. Prepend no `cd`, `export`, or other shell command.

| Goal | Query subcommand |
| -- | -- |
| production module importers / blast radius | `rdeps <module> --exclude-tests` |
| direct test-module importers | `rdeps <module>` then filter/report test modules |
| module imports | `deps <module>` |
| shortest import chain | `path <from> <to>` |
| production centrality / highest in-degree | `central --top N --exclude-tests` |
| rank / threshold a known candidate set | `central --among <a,b,c> --exclude-tests` |
| production vs test importer counts | `rdeps <module> --exclude-tests` (`importer_count`, `excluded_test_importer_count`) |
| internal-import coupling (not centrality) | `coupled --top N` |
| symbol source including module imports or module symbols | `symbol <name> --with-imports` · `symbols <module>` |
| regex symbol search | `find-symbol <pattern>` |
| direct production callers | `fn-rdeps <module::symbol> --exclude-tests` |
| callers plus test-module importers | `fn-rdeps <module::symbol> --exclude-tests`, then `rdeps <module>` |
| direct imports / callees | `fn-deps <module::symbol>` |
| transitive callers / function blast | `fn-blast <module::symbol>` |
| broken Sphinx cross-references | `xrefs --broken <module>` |
| changed-code blast radius | `diff-impact [--base REF]` |
| transitive affected tests / mocks | `test-impact <target>` · `mock-rdeps <target>` |
| pytest fixtures | `fixture-rdeps <name>` · `fixture-graph <test-file>` |
| subprocess relationships | `subprocess-deps <module>` · `subprocess-rdeps <module>` |
| coverage / documentation gaps | `coverage <target>` · `coverage-gap [module]` · `undocumented [module]` |

Direct/every/all/production/blast-radius callers → `fn-rdeps <module::symbol> --exclude-tests`; `fn-blast <module::symbol>` only for explicit transitive, closure, hops, or all-levels requests.

Test modules directly importing module: use `rdeps <module>`, then filter/report tests. Reserve `test-impact <target>` for transitive affected-test selection. For the counts alone, `rdeps <module> --exclude-tests` reports `importer_count` and `excluded_test_importer_count` together; never derive one by subtracting a second call from the first.

Counts and scoped rankings come from the query, not from hand work on its output: do not count a returned list by eye, and do not intersect a repository-wide `central` ranking against a candidate set by hand. To order or threshold modules a previous query returned, pass them to `central --among <a,b,c>`; its `unmatched` field names every requested module the ranking did not cover, so nothing drops silently.

`symbol <name>` accepts bare function (for example `authenticate`) or qualified method (for example `MyClass.method`); `module::symbol` belongs to `fn-*` call-graph queries. To chain `symbol` into `fn-*`, compose returned `module` + `qualified_name` exactly as `<module>::<qualified_name>`; example `mypackage.module::MyClass.method`. For feature scaffolding, query requested qualified extension method (for example, `symbol MyClass.add_feature`), not nearby `symbol MyClass` or `symbols <module>` listing unless broader scope requested.

For method changes possibly affecting overrides, use `find-symbol '<ClassSuffix>\.<method>$' --exclude-tests --limit 0` for same-name override candidates. Name match discovers candidates, not inheritance; inspect each source to verify ancestry + package boundaries before treating as override.

Source request naming imports: use `symbol <name> --with-imports`. `query_complete: true` confirms index coverage, not requested optional fields.

Table is a routing shortlist, not the parser's full surface. If the operation is known but an argument is unknown, read `codemap-py query <subcommand> --help`; if the operation is unknown, read `codemap-py query --help`; never guess a subcommand. Known syntax needs no preliminary help, doctor, scan, or freshness call. For exploratory module importer questions, `rdeps <module> --limit N` returns an explicit bounded preview of static `imported_by`; `dynamic_imported_by` and `config_refs` remain exhaustive. Default `rdeps <module>` and `rdeps <module> --limit 0` return every static importer. A truncated preview never settles exhaustive callers.

## Index and completeness contract

Resolve the installed launcher once and retain its literal in reasoning; shell variables do not persist between tool calls. Run selected queries first; no unconditional pre-scan/freshness call. Independent read-only queries may run concurrently as separate tool commands only against a prepared stable index with no self-heal (`SCAN_NO_AUTOBUILD=1`); dependent queries wait for their inputs, and refresh, self-heal, or index writes run serially. Keep independent queries standalone, not `batch`, until its per-item completeness contract is verified. Use `test-impact` for test choice, not direct test-module import.

- Normal mode may perform the CLI's bounded incremental self-heal.
- With `SCAN_NO_AUTOBUILD=1`, never run freshness query, incremental refresh, or automatic full build. Query existing index unchanged.
- Missing frozen index = hard stop. Report structured error; ask for `/codemap-py:scan-codebase`.
- Explicit user-requested `codemap-py index` remains allowed; flag blocks only implicit writes.

Interpret `index`:

- Complete, untruncated `query_complete: true` settles its own answered structural fact, not sibling standard queries answering other dimensions. Complete-query paths are caller-repo-relative, never Skill-relative. Do not re-query/read/grep that same graph fact.
- Ordinary repository reads remain allowed for task-requested distinct independent AST/oracle view or source-body implementation/runtime. Label separately, never as rechecking complete Codemap result.
- `query_complete: false`: name `completeness_reason`; use only a targeted fallback for gaps named by `degraded`, `not_covered`, `root_mismatch`, or `stale`.
- `compact: true` changes only coverage metadata; findings/counts remain complete.

Truncation ≠ incompleteness. Truncation at 20 items is a real cap, not exhaustive unless `--limit 0` (`symbol` and `find-symbol` default). `query_complete` scores graph coverage only—staleness, degraded/untracked files, root mismatch, name collisions—not cap. Thus capped `query_complete: true` is still 20-of-N; never stop before missing items.

Before treating list as whole, read `index.confidence`: `"exact"` = all matches; `"partial"` = capped/stale. When capped, `index.truncated: true` + `index.total_available: <N>` give total. Before claiming complete, re-run with `--limit 0` or `--top`/`--limit` above `total_available`. This is a targeted correction for that fact, not a new sibling question.

`fn-blast` takes one qualified name, never `--depth`; `coupled` ≠ `central`. Never invent flags or retry a completed structural query. Continue distinct queries or exhaustive facts required to finish; there is no arbitrary total-call cap. For one started query, allow only bounded targeted correction retries for a syntax/argument error; if the same correction failure recurs, stop and report the failure and unfinished fact. Tool-routing failure with no CLI execution does not count.

## Render

Use JSON primary array: `imported_by` / `direct_imports`, `called_by` / `calls`, `path`, `symbols`, `central` / `coupled`, `blast_radius`, or `changed_modules` + `test_impact`. Preserve qualified names exactly. Include present stale, degraded, root-mismatch, and `not_covered` caveats.

Output routing (the only use of `Write`): if the rendered result set is 5+ items, write it to `.temp/output-query-code-<branch>-<YYYY-MM-DD>.md`.

</workflow>
