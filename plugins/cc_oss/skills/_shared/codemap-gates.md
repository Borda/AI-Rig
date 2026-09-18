<!-- file: codemap-gates.md — consumers: oss/skills/review, resolve -->

**Wrapper** — Gate A / Gate B machinery lives in the codemap-shipped gates contract. Resolve this plugin's local propagated copy, read it:

```bash
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)
[ -z "$_OSS_SHARED" ] && _OSS_SHARED="plugins/cc_oss/skills/_shared"
if ! command -v codemap-py >/dev/null 2>&1 || ! cat "$_OSS_SHARED/codemap-py--codemap-gates.md" 2>/dev/null; then
    echo "codemap gates contract absent — use fallback below"
fi
```

Calling skill (`oss:review`, `oss:resolve`) sets `CODEMAP_CURRENCY` before reading this file.

Contract `v2` (loaded above, when present): follow both gates with oss's skip flag.

- **Gate A — missing index**: fire when `CODEMAP_ENABLED=false` and `CODEMAP_FORCE_OFF=false`.
- **Gate B — stale index**: fire when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

Each gate's `AskUserQuestion` prompt, options, on-choice actions (build, continue, abort/skip) live in the contract. Apply as written, no consumer override: contract's v2 build/rebuild action is the gated `codemap-py index` launcher.

**Fallback, codemap plugin absent**: skip both gates, proceed with `CODEMAP_ENABLED` as-is — no structural gating, file-read context only. Never break the load.
