<!-- file: codemap-gates.md — consumers: research/skills/run, verify -->

**Wrapper** — Gate A / Gate B machinery lives in codemap-shipped gates contract. Resolve this plugin's local propagated copy and read it:

```bash
_RESEARCH_SHARED="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/_shared}"
[ -z "$_RESEARCH_SHARED" ] && _RESEARCH_SHARED=$(python "plugins/cc_research/bin/resolve_shared.py" 2>/dev/null)
[ -z "$_RESEARCH_SHARED" ] && _RESEARCH_SHARED="plugins/cc_research/skills/_shared"
if ! command -v codemap-py >/dev/null 2>&1 || ! cat "$_RESEARCH_SHARED/codemap-py--codemap-gates.md" 2>/dev/null; then
    echo "codemap gates contract absent — use fallback below"
fi
```

Read currency: `IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/research-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="no_index"`.

Contract (`v2`) — follow both gates with research's skip flag:

- **Gate A — missing index**: fire when `CODEMAP_ENABLED=false` and `CODEMAP_RAW=auto`.
- **Gate B — stale index**: fire when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

Each gate's `AskUserQuestion` prompt, options, and on-choice actions (continue, abort/skip) in contract — apply as written, no consumer override: the contract's own build/rebuild action is the gated `codemap-py index` dispatcher.

**Fallback when codemap-py plugin absent**: skip both gates, proceed with `CODEMAP_ENABLED` as-is — no structural gating, file-read context only. Never break load.
