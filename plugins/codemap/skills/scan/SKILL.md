---
name: scan
description: Scan the Python codebase and build a structural JSON index (import graph + blast-radius metrics).
argument-hint: [--root <path>]
effort: medium
allowed-tools: Bash
disable-model-invocation: false
---

<objective>

Build a structural index of the Python codebase. Uses `ast.parse` to extract the import graph across all Python files, writes `.cache/scan/<project>.json`. No external dependencies required.

Agents and develop skills query this index via `scan-query` to understand module dependencies, blast radius, and coupling before editing code.

NOT for: querying an existing index (use `/codemap:query`).

</objective>

<workflow>

## Step 1: Run the scanner

```bash
# timeout: 360000
${CLAUDE_PLUGIN_ROOT}/bin/scan-index
```

If `--root` was passed as an argument, forward it:

```bash
# timeout: 360000
${CLAUDE_PLUGIN_ROOT}/bin/scan-index --root <path>
```

The scanner writes to `.cache/scan/<project>.json` and prints a summary line:

```
[codemap] ✓ .cache/scan/<project>.json
[codemap]   N modules indexed, M degraded
```

## Step 2: Report

After the scan completes, read the index and report a compact summary:

```bash
python3 -c "
import json, sys
with open('.cache/scan/\$(basename \$(git rev-parse --show-toplevel)).json') as f:
    d = json.load(f)
ok = [m for m in d['modules'] if m.get('status') == 'ok']
deg = [m for m in d['modules'] if m.get('status') == 'degraded']
top = sorted(ok, key=lambda m: m.get('rdep_count', 0), reverse=True)[:5]
print(f\"Modules: {len(ok)} indexed, {len(deg)} degraded\")
print(f\"Most central (by rdep_count):\")
for m in top:
    print(f\"  {m.get('rdep_count', 0):>3}  {m['name']}\")
"
```

If degraded files exist: list them with their reason. Do not treat degraded files as a failure — the index is still useful.

## Step 3: Suggest next step

```
Index ready. Query it with:
  /codemap:query central --top 10
  /codemap:query deps <module>
  /codemap:query rdeps <module>
  /codemap:query coupled --top 10
```

</workflow>
