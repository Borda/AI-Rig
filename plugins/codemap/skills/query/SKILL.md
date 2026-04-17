---
name: query
description: Query the codemap structural index — central, coupled, deps, rdeps, or import path between modules.
argument-hint: <central [--top N] | coupled [--top N] | deps <module> | rdeps <module> | path <from> <to>>
---

<objective>

Query the codemap structural index for import-graph analysis. `scan-query` is at `${CLAUDE_PLUGIN_ROOT}/bin/scan-query` — use this exact path.

Queries:

- `central [--top N]` — most-imported modules (highest blast radius, default top 10)
- `coupled [--top N]` — modules with most imports (highest coupling, default top 10)
- `deps <module>` — what does this module import?
- `rdeps <module>` — what imports this module?
- `path <from> <to>` — shortest import path between two modules

NOT for: building or rebuilding the index (use `/codemap:scan`).

</objective>

<workflow>

## Step 1: Run the query

Run `scan-query` via Bash with the appropriate arguments:

```bash
# timeout: 20000
scan-query <QUERY_ARGS>
```

Replace `<QUERY_ARGS>` with the appropriate command:

| Goal                          | Command            |
| ----------------------------- | ------------------ |
| what imports X (reverse deps) | `rdeps <module>`   |
| what X imports (direct deps)  | `deps <module>`    |
| most-imported modules         | `central --top 10` |
| most-coupled modules          | `coupled --top 10` |
| path between A and B          | `path <from> <to>` |

`scan-query` is on PATH and locates the index automatically via git root — no setup required. If the index is missing it prints a clear error.

## Step 2: Format and return

For `rdeps` / `deps`: list the modules, one per line.

For `central` / `coupled`: list top modules by count with a brief note.

For `path`: show the import chain as `A → B → C → D`.

If the result is `{"error": "..."}`: surface the error and suggest re-running `/codemap:scan`.

</workflow>
