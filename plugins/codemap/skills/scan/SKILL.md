---
name: scan
description: Scan the Python codebase and build a structural JSON index (import graph + blast-radius metrics).
argument-hint: [--root <path>]
effort: medium
allowed-tools: Bash
---

<objective>

**Python only** — uses `ast.parse` to extract import graph across all `.py` files; non-Python files not indexed. Writes `.cache/scan/<project>.json`. No external deps required.

Agents and develop skills query index via `scan-query` to understand module dependencies, blast radius, coupling before editing code.

NOT for: querying existing index (use `/codemap:query`).

</objective>

<workflow>

## Step 1: Run the scanner

```bash
# timeout: 360000
${CLAUDE_PLUGIN_ROOT}/bin/scan-index
```

If `--root` passed as argument, forward it:

```bash
# timeout: 360000
${CLAUDE_PLUGIN_ROOT}/bin/scan-index --root <path>
```

Scanner writes to `.cache/scan/<project>.json` and prints summary line:

```
[codemap] ✓ .cache/scan/<project>.json
[codemap]   N modules indexed, M degraded
```

## Step 2: Report

After scan completes, read index and report compact summary:

```bash
# Note: $(...) inside the double-quoted python3 -c "..." string is shell-expanded before Python sees it.
# basename/git rev-parse resolve the project name at call time — intentional shell substitution.
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

Degraded files exist: list with reason. Not failure — index still useful.

## Step 3: Suggest next step

```
Index ready. Query it with:
  /codemap:query central --top 10
  /codemap:query deps <module>
  /codemap:query rdeps <module>
  /codemap:query coupled --top 10
```

</workflow>
