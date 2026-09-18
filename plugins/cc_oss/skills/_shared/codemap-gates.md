<!-- file: codemap-gates.md — consumers: oss/skills/review, resolve -->

**Wrapper** — Gate A / Gate B machinery lives in the codemap-shipped gates contract. Resolve the active `codemap-py` install (registry first, never a newer orphaned cache dir) and read its contract — no local copy, so the text always matches the CLI actually installed:

```bash
# gate before resolve: absent CLI spawns nothing; && chain keeps set -e off resolver exit 1
command -v codemap-py >/dev/null 2>&1 && _CODEMAP_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" codemap-py claude-skills/_shared 2>/dev/null) && cat "$_CODEMAP_SHARED/codemap-gates.md" 2>/dev/null || echo "codemap gates contract absent — use fallback below"
```

Calling skill (`oss:review`, `oss:resolve`) sets `CODEMAP_CURRENCY` before reading this file.

Contract (version as loaded above, when present): follow both gates with oss's skip flag.

- **Gate A — missing index**: fire when `CODEMAP_ENABLED=false` and `CODEMAP_FORCE_OFF=false`.
- **Gate B — stale index**: fire when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

Gate A always asks (`AskUserQuestion`). Gate B's rebuild-vs-ask default is whatever the loaded contract states — `LAZY_CODEMAP` applies only where that contract documents it. Every prompt, option, and on-choice action lives in the contract. Apply as written, no consumer override: contract's own build/rebuild action is the gated `codemap-py index` launcher.

**Fallback, codemap plugin absent**: skip both gates, proceed with `CODEMAP_ENABLED` as-is — no structural gating, file-read context only. Never break the load.
