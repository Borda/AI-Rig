<!-- file: codemap-context.md — consumers: plugins/cc_develop/skills/{plan,fix,feature,refactor,review}/SKILL.md, plugins/cc_oss/skills/review/SKILL.md -->

**Structural context (codemap-py)** — run only when caller sets `CODEMAP_ENABLED=true`; skip if flag absent. Callers pre-set `TARGET_MODULE` (dotted), `TARGET_FN` (bare function name), and `CODEMAP_QUERY_KIND`. Use `skip` for a fully localized edit, a task-fit kind for one unresolved structural fact, and `standard` only when broader context is justified.

**Wrapper** — query mechanics, batch pre-flight bash, evidence-line contract, completeness/staleness semantics, coverage-metadata rules, targeted-edit pattern, and effort tiers live in codemap-shipped contract. Resolve this plugin's local propagated copy and read it:

```bash
_DEV_SHARED="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/_shared}"
[ -z "$_DEV_SHARED" ] && _DEV_SHARED=$(python "plugins/cc_develop/bin/dev_shared_resolve.py" 2>/dev/null)
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
if ! command -v codemap-py >/dev/null 2>&1 || ! cat "$_DEV_SHARED/codemap-py--codemap-context.md" 2>/dev/null; then
    echo "codemap contract absent — use fallback below"
fi
```

Contract (`v3`) — follow §Batch pre-flight pattern (run with `TARGET_MODULE`/`TARGET_FN`/`CODEMAP_QUERY_KIND`), §Evidence-line contract, §Coverage metadata, §Targeted-edit pattern, §Effort-tier guidance.

**Fallback when codemap plugin absent**: when `CODEMAP_QUERY_KIND=skip`, run no Codemap command. Otherwise run only the task-fit query when known, falling back to `codemap-py query --timeout 5 central --top 5 2>/dev/null`; treat any non-empty output as usable, skip evidence-line/completeness logic, and proceed with file reads for the rest. Never break load.

## Per-agent query map (develop dimension)

Extends contract core map with develop's dimension queries:

- `central --top 5` — global blast-radius baseline (sw-engineer, architect)
- `fn-rdeps --exclude-tests` — direct callers; benchmarked (94k vs 1M+ tokens, +40pp accuracy); run first (sw-engineer)
- `fn-blast` — transitive caller impact when depth > 1 needed (sw-engineer)
- `uncovered --top 20` — test gaps (qa-specialist)
- `mock-rdeps` — test mock coverage; prevents false "untested" on mocked symbols (qa-specialist)
- `undocumented` — docstring gaps (doc-scribe)
- `symbol --with-imports` — contract reading without re-reading file (all agents)

Results returned: prepend `## Structural Context (codemap-py)` block to foundry:sw-engineer spawn prompt with hotspot JSON and per-query output, followed by this **codemap-first protocol** (own copy — self-contained, no cross-plugin reference): (1) **Skill-first** — use the block above before any Grep/Glob/Read aimed at imports, callers, or symbol contracts for a symbol already listed there. (2) **Bounded call budget** — symbol not listed → up to 3 additional `codemap-py query` calls this task. (3) **Hard stop on `query_complete: true`** (or legacy `exhaustive: true`) — that result is final for its direction, no follow-up Grep/Read/query to re-confirm it. `codemap-py` not found or index missing: emit ⚠ warning to stderr (`>&2 echo "⚠ codemap-py: codemap-py unavailable or index missing — context reduced to central --top 5"`), omit the protocol paragraph, then proceed.

Note: `foundry:sw-engineer` also carries its own `<codemap_context>` pre-flight (workflow step 00) that runs independently of this wrapper — the protocol above governs the query output this wrapper hands the agent inline; sw-engineer's own step 00 governs what it queries itself on spawn regardless of caller.

## Extended scan — multi-file / API changes (develop batch producer)

Contract §Extended scan defines risk tiers (`>=5` HIGH, `1–4` MODERATE, `0` LOW). Develop's batch producer:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/codemap_scan.py" --source=diff  # timeout: 15000
```

> Interpret: any `rdeps` output = external callers affected; `coupled` output = co-change pairs.
>
> Fallback (specific known module): `codemap-py query rdeps <mod> --top 10 2>/dev/null || true`.

## Review-pipeline injection (oss:review, develop:review)

Review orchestrators run v4 pre-flight queries **per changed module** before spawning dimension agents, persist structured output, pass to each agent as `CODEMAP_CONTEXT` so agent skips redundant file reads. Pre-flight queries (module-level only — a name-only changed-files list cannot supply the `module::fn` qnames `fn-rdeps`/`fn-blast` require; bare-module fn-\* calls failed 100% in production, 2026-07 usage audit F1; fn-level returns once diff-hunk qname derivation lands — audit plan P1.2b):

```bash
codemap-py query --timeout 5 rdeps       "$MODULE"            2>/dev/null  # importer count → risk tier
codemap-py query --timeout 5 mock-rdeps  "$MODULE"            2>/dev/null  # mock coverage (v4.1)
codemap-py query --timeout 5 uncovered   --top 20 "$MODULE"   2>/dev/null  # test gaps (v4.2)
codemap-py query --timeout 5 xrefs --broken      "$MODULE"    2>/dev/null  # stale doc refs (v4.5)
codemap-py query --timeout 5 undocumented "$MODULE"  2>/dev/null  # doc coverage (v4.4)
```

> Per-agent consumption:
>
> - `qa-specialist` — read `uncovered` + `mock-rdeps` first; skip manual test-file grep for symbols codemap already classifies; fall back to Read only when codemap context absent or insufficient
> - `doc-scribe` — read `undocumented` + `xrefs --broken` first; skip docstring-scan reads on listed symbols
> - `sw-engineer` — read `rdeps` first (importers per changed module); `fn-rdeps`/`fn-blast` only when a concrete `module::fn` qname is known (dimension queries above)
> - `challenger` — unchanged; always reads source directly
>
> **Bounded call budget + hard stop** (mirrors `review/SKILL.md` Step 1 spawn-prompt block — update both together): symbol not covered by the pre-flight batch above → up to 3 additional `codemap-py query` calls this review pass. Any result carrying `query_complete: true` (or legacy `exhaustive: true`) is final for that direction — no follow-up Grep/Read/query to re-confirm it.

## Review→resolve pre-flight cache (persisted artifact)

Review runs per-changed-module pre-flight batch once (§Review-pipeline injection). Follow-on skill on same PR — `oss:resolve` after `/review` — otherwise re-issues identical `rdeps`/`mock-rdeps`/`uncovered`/`xrefs`/`undocumented` queries for same modules. Persisted cache lets follow-on **reuse** those answers instead.

**Artifact shape (report §5.3)** — one file per module at `.temp/<run>/codemap-context/<module>.json`, split into stable *prefix* (index-derived, content-hashed + git-sha stamped) and volatile *delta* (touched files, exhausted queries, notes) so a later cross-skill handoff generalizes without rework:

```json
{"module": "pkg.mod",
 "prefix": {"git_sha": "<index git_sha>", "scanned_at": "<index ISO ts>", "index_stamp": "<size>:<mtime_ns> of index file", "content_hash": "<sha256>", "answers": {"rdeps": {...}, "fn-rdeps": {...}}},
 "delta": {"touched_files": [], "exhausted_queries": [], "notes": []}}
```

**Freshness rule** (fail-closed, three conditions — all must hold): `prefix.git_sha` matches current index `git_sha`; `prefix.scanned_at` not older than current index `scanned_at` (rebuilt index — newer `scanned_at` — invalidates every artifact); `prefix.index_stamp` still equals the index file's `<size>:<mtime_ns>`. The stamp is what makes the rule fail closed without trusting index-declared metadata: an `--incremental` re-scan, a restored backup, or a manual edit can leave `git_sha`/`scanned_at` untouched and is invisible to the first two checks alone. An artifact written before the stamp field existed carries none and is re-queried rather than trusted. Verdict reasons from `codemap_cache.py read`: `fresh` · `git_sha_mismatch` · `index_rebuilt` · `index_stamp_mismatch` · `content_hash_mismatch`. Health metric: `reuse_ratio` = reused answers / total persisted.

**Writer/reader contract** (oss plugin ships `bin/codemap_cache.py`; gate on `oss` availability — consumer without it simply re-queries):

```bash
# write — split a codemap-py query batch result into per-module artifacts (batch-producer side)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/codemap_cache.py" write --batch "$BATCH_OUT" --index "$IDX" --cache-dir "$CACHE_DIR"
# read — reuse verdict + cached answers for one module (consumer side)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/codemap_cache.py" read  --module "$MOD" --index "$IDX" --cache-dir "$CACHE_DIR"
# report — aggregate reuse_ratio for telemetry
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/codemap_cache.py" report --cache-dir "$CACHE_DIR"
```

> Review-side wiring (optional, not yet in review/SKILL.md): review may call `codemap_cache.py write` on its `$RUN_DIR` batch output to seed `$RUN_DIR/codemap-context/` — until then, resolve materializes cache from review's persisted `$RUN_DIR/codemap-context.md` batch blob on first use, so no review change required for reuse to work.

**Semble companion** — include in agent spawn prompt only when caller sets `SEMBLE_ENABLED=true`; skip if flag absent:

> `mcp__semble__search` available and codemap direction-incomplete (`"query_complete": false`, or legacy `"exhaustive": false`) or no index found: call `mcp__semble__search` with varied queries (e.g. `"<module> import"`, `"from <module> import"`, `"<module> usage"`) and `repo=<git_root>`, `top_k=20`. Stop when two consecutive queries return no new modules. Merge all results into final rdep set — union of codemap + all semble calls. Codemap `query_complete: true`: skip semble.
