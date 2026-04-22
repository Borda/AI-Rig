---
name: scan
description: Scan the Python codebase and build a structural JSON index (import graph + blast-radius metrics).
argument-hint: [--root <path>] [--incremental]
effort: medium
allowed-tools: Bash
---

<objective>

**Python only** — uses `ast.parse` to extract import graph and symbol metadata across all `.py` files; non-Python files not indexed. Writes `.cache/scan/<project>.json`. No external deps required.

Index captures per module: import graph, blast-radius metrics, and **symbol list** (classes, functions, methods with line ranges). Symbol data enables `scan-query symbol` / `find-symbol` to return just the target function source instead of full file reads.

Agents and develop skills query index via `scan-query` to understand module dependencies, blast radius, coupling, and individual symbol source before editing code.

NOT for: querying existing index (use `/codemap:query`).

</objective>

<workflow>

## Step 1: Run the scanner

```bash
# timeout: 360000
${CLAUDE_PLUGIN_ROOT}/bin/scan-index  # add --root <path> if provided
```

If `--incremental` passed: re-parse only files changed since last scan (git SHA comparison), then recompute global metrics. Falls back to full scan when no v3 index exists.

```bash
# timeout: 60000
# scan-index handles v2→v3 fallback internally — exits 0 on either path
${CLAUDE_PLUGIN_ROOT}/bin/scan-index --incremental
```

Scanner writes to `.cache/scan/<project>.json` and prints summary line:

```text
[codemap] ✓ .cache/scan/<project>.json
[codemap]   N modules indexed, M degraded
```

## Step 2: Report

After scan completes, read index and report compact summary:

```bash
# scan-query has no summary mode — inline script required to extract project stats from raw index JSON
python3 -c "
import json, sys, subprocess, os
try:
    proj = os.path.basename(subprocess.check_output(['git','rev-parse','--show-toplevel'], stderr=subprocess.DEVNULL).decode().strip())
except Exception:
    proj = os.path.basename(os.getcwd())
index_path = f'.cache/scan/{proj}.json'
try:
    with open(index_path) as f:
        d = json.load(f)
except FileNotFoundError:
    print(f'Index not found: {index_path} — run /codemap:scan first')
    sys.exit(1)
ok = [m for m in d['modules'] if m.get('status') == 'ok']
deg = [m for m in d['modules'] if m.get('status') == 'degraded']
if not ok:
    print('No modules indexed.')
    sys.exit(0)
top = sorted(ok, key=lambda m: m.get('rdep_count', 0), reverse=True)[:5]
total_syms = sum(len(m.get('symbols', [])) for m in ok)
total_calls = sum(len(s.get('calls', [])) for m in ok for s in m.get('symbols', []))
print(f\"Modules: {len(ok)} indexed, {len(deg)} degraded\")
print(f\"Symbols: {total_syms} (functions, classes, methods)\")
if total_calls:
    print(f\"Calls:   {total_calls} resolved call edges (v3 index)\")
print(f\"Most central (by rdep_count):\")
for m in top:
    print(f\"  {m.get('rdep_count', 0):>3}  {m['name']}\")
"
```

Degraded files exist: list with reason. Not failure — index still useful.

## Step 3: Suggest next step

```text
Index ready. Query it with:
  /codemap:query central --top 10
  /codemap:query deps <module>
  /codemap:query rdeps <module>
  /codemap:query coupled --top 10
  # see /codemap:query for full list of subcommands
```

</workflow>
