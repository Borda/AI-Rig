<!-- file: codemap-context.md — consumers: codemap-py `integrate apply` (managed-block host, CONSUMER_MANAGED_FILE["foundry"]); foundry agents run the equivalent pre-flight inline in their own <codemap-context> block and do not read this file yet -->

**Structural context (codemap-py) — foundry wrapper.** Provider ships shared mechanics; this file adds only foundry-specific dimension. Run only when caller sets `CODEMAP_ENABLED=true`; skip if flag absent.

**Wrapper** — target derivation, query mechanics, evidence-line contract, completeness/staleness semantics, effort tiers all live in codemap-shipped contract. Resolve the active `codemap-py` install (registry first, never a newer orphaned cache dir) and read its contract — no local copy, so the text always matches the CLI actually installed:

```bash
# gate before resolve: absent CLI spawns nothing; && chain keeps set -e off resolver exit 1
command -v codemap-py >/dev/null 2>&1 && _CODEMAP_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" codemap-py claude-skills/_shared 2>/dev/null) && cat "$_CODEMAP_SHARED/codemap-context.md" 2>/dev/null || echo "codemap contract absent — use fallback below"
```

Contract (version as loaded) — follow §Target derivation, §Core query map, §Evidence-line contract, §Effort-tier guidance.

## Per-agent query map (foundry dimension)

Extends contract core map. Each entry is the dimension that agent's own pre-flight already runs — keep both in sync on change:

- `foundry:sw-engineer` — `central --top 5`, `rdeps`, `fn-rdeps`, `fn-blast`, `symbol`
- `foundry:qa-specialist` — `uncovered --top 20`, `coverage-gap --threshold 0.8`, `mock-rdeps`, `fixture-rdeps`, `fixture-graph`
- `foundry:doc-scribe` — `undocumented`, `xrefs --broken`
- `foundry:solution-architect` — `central --top 5`, `rdeps` (fan-in), `deps` (fan-out), `xrefs`
- `foundry:perf-optimizer` — `central --top 5`, `subprocess-deps`, `fn-blast`, `fixture-rdeps`, `fixture-graph`
- `foundry:challenger` — `central --top 5`, `rdeps`, `fn-blast`

**Bounded call budget + hard stop** — symbol not covered by pre-flight above → up to 3 additional `codemap-py query` calls this task. Any result carrying `query_complete: true` (or legacy `exhaustive: true`) is final for that direction: no follow-up Grep/Read/query to re-confirm.

**Fallback when codemap plugin absent**: run only `codemap-py query --timeout 5 central --top 5 2>/dev/null`; treat non-empty output as usable, skip evidence-line/completeness logic, proceed with file reads. Never break the load.

## Managed-block host

This file is `CONSUMER_MANAGED_FILE["foundry"]` — consumer-owned host for the `codemap-py.integration.v2` managed-block body. Ships **without** one: `codemap-py integrate plan` records a first-time insert and `integrate apply` appends the `<!-- codemap-py:integration:begin v1 sha256=... -->` … `end` block at EOF, per install. Sentinel schema remains `v1`; body declares protocol `codemap-py.integration.v2`. Never hand-author that block — engine refuses any block whose body hash doesn't match its marker.
