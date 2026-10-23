<!-- file: codemap-gates.md — consumers: research/skills/run, verify -->

**Wrapper** — Gate A/B machinery lives in codemap-shipped gates contract. Resolve the active `codemap-py` install (registry first, never a newer orphaned cache dir) and read its contract — no local copy, so the text always matches the CLI actually installed:

```bash
# gate before resolve: absent CLI spawns nothing; && chain keeps set -e off resolver exit 1
command -v codemap-py >/dev/null 2>&1 && _CODEMAP_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared_path.py" codemap-py claude-skills/_shared 2>/dev/null) && cat "$_CODEMAP_SHARED/codemap-gates.md" 2>/dev/null || echo "codemap gates contract absent — use fallback below"
```

Read currency: `IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/research-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="no_index"`.

Contract (version as loaded) — follow both gates with research's skip flag:

- **Gate A — missing index**: fire when `CODEMAP_ENABLED=false` and `CODEMAP_RAW=auto`.
- **Gate B — stale index**: fire when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

Gate A prepares automatically by default; `LAZY_CODEMAP` is the explicit confirm-first preference. Gate B follows the same default/opt-out preference in the loaded contract. Inherited `SCAN_NO_AUTOBUILD=1` freezes both gates and must be carried to every later query. The contract's own build/rebuild action is the gated `codemap-py index` launcher. After successful preparation, rerun `codemap-flag.py <current-skill-sentinel-slug> "$ARGUMENTS"` to persist enabled/current state. On Gate B's skip branch, rerun `codemap-flag.py <current-skill-sentinel-slug> '--no-codemap'` to persist disabled state. On its stale-data continuation branch, retain the stale currency sentinel and pass `--no-heal` to every later query, including later tool calls. Every prompt, option, preparation action, and failure outcome lives in that provider contract; apply it without consumer overrides.

**Fallback when codemap-py plugin absent**: skip both gates, proceed with `CODEMAP_ENABLED` as-is — no structural gating, file-read context only. Never break load.

> Query-state handoff: after completing the gate, every fresh query shell reloads the same invocation’s enabled and currency sentinels. Skip queries when disabled. For `stale`, export `SCAN_NO_AUTOBUILD=1` in that query shell before direct commands or Python helpers, so fallback queries and nested subprocesses inherit the choice. Never clear an inherited freeze. Do not export this derived freeze in the preparation shell: affirmative refresh reruns the owning resolver and persists `current`, so later fresh query shells resume normal healing. Include the exact sentinel paths and this rule in every delegated agent prompt; tool shells do not inherit another tool call’s local variables.
