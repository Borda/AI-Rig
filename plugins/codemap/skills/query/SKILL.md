---
name: query
description: Query the codemap structural index — central, coupled, deps, rdeps, or import path between modules.
argument-hint: <central [--top N] | coupled [--top N] | deps <module> | rdeps <module> | path <from> <to>>
allowed-tools: Read, Bash
effort: low
---

<objective>

Query codemap structural index for import-graph analysis. **Python projects only** — index covers `.py` files; queries on non-Python projects return empty or error. `scan-query` on PATH (installed by `/codemap:scan`).

Queries:

- `central [--top N]` — most-imported modules (highest blast radius, default top 10)
- `coupled [--top N]` — modules with most imports (highest coupling, default top 10)
- `deps <module>` — what module imports
- `rdeps <module>` — what imports module
- `path <from> <to>` — shortest import path between two modules

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
| what imports X (reverse deps) | `rdeps <module>` |
| what X imports (direct deps) | `deps <module>` |
| most-imported modules | `central --top 10` |
| most-coupled modules | `coupled --top 10` |
| path between A and B | `path <from> <to>` |

`scan-query` on PATH, locates index via git root — no setup. Missing index prints clear error.

## Step 2: Format and return

`rdeps` / `deps`: list modules, one per line.

`central` / `coupled`: list top modules by count with brief note.

`path`: show chain as `A → B → C → D`.

`{"error": "..."}`: surface error, suggest re-running `/codemap:scan`.

</workflow>
