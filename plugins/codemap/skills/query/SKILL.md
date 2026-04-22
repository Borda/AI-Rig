---
name: query
description: Query the codemap structural index — central, coupled, deps, rdeps, import path, symbol-level source extraction, and function-level call graph (fn-deps, fn-rdeps, fn-central, fn-blast).
argument-hint: <central [--top N] | coupled [--top N] | deps <module> | rdeps <module> | path <from> <to> | symbol <name> | symbols <module> | find-symbol <pattern> | list | fn-deps <qname> | fn-rdeps <qname> | fn-central [--top N] | fn-blast <qname>>
allowed-tools: Bash
effort: low
---

<objective>

Query codemap structural index for import-graph analysis, symbol-level source extraction, and function-level call graph traversal. **Python projects only** — index covers `.py` files; queries on non-Python projects return empty or error. `scan-query` on PATH (installed by the codemap plugin).

**Module-level queries** (import graph):
- `central [--top N]` — most-imported modules (highest blast radius, default top 10)
- `coupled [--top N]` — modules with most imports (highest coupling, default top 10)
- `deps <module>` — what module imports
- `rdeps <module>` — what imports module
- `path <from> <to>` — shortest import path between two modules

**Symbol-level queries** (use instead of reading full files — ~94% token reduction):
- `symbol <name>` — get source of a function/class/method by name
- `symbols <module>` — list all symbols in a module (no file I/O)
- `find-symbol <pattern>` — regex search across all symbol names in index

**Function-level call graph queries** (v3 index — requires `/codemap:scan` with call graph):
- `fn-deps <qname>` — what does this function/method call? (outgoing edges)
- `fn-rdeps <qname>` — what functions call this one? (incoming edges)
- `fn-central [--top N]` — most-called functions globally (default top 10)
- `fn-blast <qname>` — transitive reverse-call BFS with depth levels

Use `module::function` format for qname, e.g. `mypackage.auth::validate_token`. Requires v3 index — if index is v2, commands return a clear upgrade prompt.

NOT for: building or rebuilding index (use `/codemap:scan`).

</objective>

<workflow>

## Step 1: Run the query

Run `scan-query` via Bash:

```bash
# timeout: 20000
scan-query <QUERY_ARGS>
```

Replace `<QUERY_ARGS>`:

| Goal | Command |
| --- | --- |
| reverse deps | `rdeps <module>` |
| forward deps | `deps <module>` |
| central modules | `central --top 10` |
| coupling rank | `coupled --top 10` |
| import path | `path <from> <to>` |
| symbol source | `symbol <name>` |
| module symbols | `symbols <module>` |
| symbol search | `find-symbol <pattern>` |
| list modules | `list` |
| outgoing calls | `fn-deps module::function` |
| incoming calls | `fn-rdeps module::function` |
| most-called functions | `fn-central --top 10` |
| transitive callers | `fn-blast module::function` |

`scan-query` on PATH, locates index via git root — no setup. Missing index prints clear error.

Symbol names accept: bare name (`authenticate`), qualified name (`MyClass.authenticate`), or case-insensitive substring fallback. Function qnames use `module::function` format (e.g. `mypackage.auth::validate_token`). Index must be current — re-run `/codemap:scan` if stale warning appears.

## Step 2: Format and return

`rdeps` / `deps`: list modules, one per line — never space-separated on a single line.
```text
myapp.api
myapp.middleware
myapp.tests.test_auth
```
NOT: `myapp.api myapp.middleware myapp.tests.test_auth`

`central` / `coupled`: list top modules by count with brief note.

`path`: show chain as `A → B → C → D`.

`symbol`: print `source` field as fenced code block; include module + line range as caption.

`symbols`: list as `type name (lines start–end)`, one per line.

`find-symbol`: list matches as `module:qualified_name (type)`, one per line.

`list`: list all modules as `module (path)`, one per line.
```text
myapp.views (src/myapp/views.py)
myapp.middleware (src/myapp/middleware.py)
```

`fn-deps` / `fn-rdeps`: list as `module::function (resolution)`, one per line.

`fn-central`: list as `count  module::function`, one per line.

`fn-blast`: list as `depth  module::function`, one per line, sorted by depth then name.

`{"error": "..."}`: surface error, suggest re-running `/codemap:scan`.

</workflow>
