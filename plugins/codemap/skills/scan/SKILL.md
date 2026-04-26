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

Parse `$ARGUMENTS` to build the invocation. Pass `--root <path>` if provided; pass `--incremental` if provided. Then run once:

```bash
# timeout: 360000
# Example with both flags: ${CLAUDE_PLUGIN_ROOT}/bin/scan-index --root /path/to/project --incremental
# scan-index handles v2→v3 fallback internally — exits 0 on either path
${CLAUDE_PLUGIN_ROOT}/bin/scan-index [--root <path>] [--incremental]
```

Scanner writes to `<root>/.cache/scan/<project>.json` and prints summary line:

```text
[codemap] ✓ .cache/scan/<project>.json
[codemap]   N modules indexed, M degraded
```

## Step 2: Report

After scan completes, read index and report compact summary:

```bash
# scan-query has no summary mode — inline script required to extract project stats from raw index JSON
# $ARGUMENTS is shell-expanded; handles --root <path> if provided, falls back to git root or cwd
python3 -c "
import json, sys, subprocess, os, shlex
try:
    args = shlex.split('$ARGUMENTS') if '$ARGUMENTS' else []
except Exception:
    args = []
try:
    i = args.index('--root')
    root = os.path.abspath(args[i + 1])
except (ValueError, IndexError):
    try:
        root = subprocess.check_output(['git','rev-parse','--show-toplevel'], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        root = os.getcwd()
proj = os.path.basename(root)
index_path = os.path.join(root, '.cache', 'scan', f'{proj}.json')
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
