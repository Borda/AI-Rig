<!-- file: codemap-gates.md — consumers: oss/skills/review, resolve -->

**Wrapper** — the Gate A / Gate B machinery lives in the codemap-shipped gates contract. Resolve this plugin's local propagated copy and read it:

```bash
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)
[ -z "$_OSS_SHARED" ] && _OSS_SHARED="plugins/cc_oss/skills/_shared"
if ! command -v codemap-py >/dev/null 2>&1 || ! cat "$_OSS_SHARED/codemap-py--codemap-gates.md" 2>/dev/null; then
    echo "codemap gates contract absent — use fallback below"
fi
```

`CODEMAP_CURRENCY` is set by the calling skill (`oss:review`, `oss:resolve`) before reading this file.

Contract `v2` (loaded above, when present) — follow both gates with oss's skip flag:

- **Gate A — missing index**: fire when `CODEMAP_ENABLED=false` and `CODEMAP_FORCE_OFF=false`.
- **Gate B — stale index**: fire when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

Each gate's `AskUserQuestion` prompt, options, and on-choice actions (build, continue, abort/skip) live in the contract — apply them as written, no consumer override: the contract's v2 build/rebuild action is the gated `codemap-py index` launcher.

**Fallback when the codemap plugin is absent**: skip both gates and proceed with `CODEMAP_ENABLED` as-is — no structural gating, file-read context only. Never break the load.
