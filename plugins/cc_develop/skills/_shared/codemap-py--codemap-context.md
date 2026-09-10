<!-- file: codemap-context.md — consumers: plugin-local codemap-context.md wrappers read manifested codemap-py--codemap-context.md copies; provider→consumer wiring uses the codemap-py.integration.v2 managed-block body with a v1 sentinel schema -->

# Codemap context contract — v3

Plugin-agnostic structural-context contract. Consumer plugins read their own byte-identical manifested copies, synchronized from this canonical source. Wrappers add only per-agent query maps + flag surfaces + plugin-local batch/cache paths; query mechanics, evidence-line contract, completeness/staleness semantics, batch pre-flight, effort tiers are maintained here.

> Contract version `v3` is the context-contract doc version — bump it when the query set or evidence contract changes. The provider→consumer wiring uses the `codemap-py.integration.v2` managed-block body with a `v1` sentinel schema (see `shared/integration-contract.md`), independent of this doc version.

## Target derivation — pluggable (consumer supplies)

`TARGET_MODULE` (dotted), `TARGET_FN` (bare name), and `CODEMAP_QUERY_KIND` are **consumer-supplied inputs** — contract doesn't derive them. Consumer wrapper/SKILL sets them from `$ARGUMENTS`, review diff, or finding before reading this file. `CODEMAP_QUERY_KIND=skip` is the executable zero-query route for a fully localized edit. The safe adaptive vocabulary is `skip`, `central`, `callers`, `blast`, `dependencies`, `test-impact`, `coupling`, and `standard`; an unset or unknown value preserves the legacy `standard` batch.

- explicit `module.path` or `module.path::function` in args → split into `TARGET_MODULE` / `TARGET_FN`
- module-only known → set `TARGET_MODULE`, leave `TARGET_FN` empty
- both empty → only global `central` baseline runs (correct when affected surface unknown until agent searches)

Normalize file path to dotted module: strip leading `./` and `src/`, strip trailing `.py`, replace `/` with `.`.

## Core query map

- `central --top 5` — global blast-radius baseline; always safe, runs with no target.
- `fn-rdeps <mod>::<fn> --exclude-tests` — direct callers of function; benchmarked 94k vs 1M+ tokens, +40pp accuracy; run first when symbol known.
- `fn-blast <mod>::<fn>` — transitive caller impact (depth > 1).
- `rdeps <mod>` — reverse module dependencies; run when only module (no function) known.
- `test-impact <mod | mod::fn>` — transitive affected-test selection for a known changed target.
- `coupled` — internal co-change coupling; does not take a target.
- `symbol --with-imports <fn>` — read symbol's contract without re-reading file (all agents).

> Consumer wrappers extend this map with per-agent dimensions (test gaps, doc gaps, mock coverage, etc.). Keep additions in wrapper — not here.

## Batch pre-flight pattern

Reference bash for single-target run. Consumers may inline it or call plugin-local batch producer; completeness/evidence logic below invariant.

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _ROOT="$PWD"
PROJ=$(basename "$_ROOT")
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
_CM_ROUTE="${CODEMAP_QUERY_KIND:-standard}"
if [ "$_CM_ROUTE" != "skip" ] && command -v scan-query >/dev/null 2>&1 && [ -f "${_IDX}/${PROJ}.json" ]; then
    if [ "${SCAN_NO_AUTOBUILD:-0}" != "1" ]; then
        scan-index --incremental 2>/dev/null || true   # refresh SHA-changed files only; never full-build mid-task
    fi
    _CM_N=0 _CM_H=0 _CM_STALE=0 _CM_NONEXH=0
    _cq() {
        local out; _CM_N=$((_CM_N+1))
        out=$(scan-query --timeout 5 "$@" 2>/dev/null)
        case "$out" in
            *'"error"'*|'') ;;
            *) _CM_H=$((_CM_H+1)); printf '%s\n' "$out"
               case "$out" in *'"stale":true'*|*'"stale": true'*) _CM_STALE=1;; esac
               # query_complete is the forward field (direction-scoped); exhaustive is its legacy alias for one cycle.
               case "$out" in *'"query_complete":false'*|*'"query_complete": false'*|*'"exhaustive":false'*|*'"exhaustive": false'*) _CM_NONEXH=1;; esac ;;
        esac
    }
    case "$_CM_ROUTE" in
        central) _cq central --top 5 ;;
        callers) [ -n "$TARGET_MODULE" ] && [ -n "$TARGET_FN" ] && _cq fn-rdeps "${TARGET_MODULE}::${TARGET_FN}" --exclude-tests ;;
        blast) [ -n "$TARGET_MODULE" ] && [ -n "$TARGET_FN" ] && _cq fn-blast "${TARGET_MODULE}::${TARGET_FN}" ;;
        dependencies) [ -n "$TARGET_MODULE" ] && _cq rdeps "$TARGET_MODULE" ;;
        test-impact) _CM_TARGET="${TARGET_QUALIFIED:-$TARGET_MODULE}"; [ -n "$_CM_TARGET" ] && _cq test-impact "$_CM_TARGET" ;;
        coupling) _cq coupled ;;
        *)
            _cq central --top 5
            [ -n "$TARGET_FN" ] && _cq fn-rdeps "${TARGET_MODULE}::${TARGET_FN}" --exclude-tests
            [ -n "$TARGET_FN" ] && _cq fn-blast "${TARGET_MODULE}::${TARGET_FN}"
            [ -z "$TARGET_FN" ] && [ -n "$TARGET_MODULE" ] && _cq rdeps "$TARGET_MODULE"
            [ -n "$TARGET_FN" ] && _cq symbol --with-imports "$TARGET_FN"
            ;;
    esac
    _IDX_MTIME=$(date -r "${_IDX}/${PROJ}.json" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "?")
    if [ "$_CM_STALE" -eq 1 ]; then _CM_COMPL="stale"
    elif [ "$_CM_NONEXH" -eq 1 ]; then _CM_COMPL="partial"
    elif [ "$_CM_H" -eq 0 ]; then _CM_COMPL="unknown"
    else _CM_COMPL="exhaustive"
    fi
    echo "codemap_evidence: queries_run=${_CM_N} hits=${_CM_H} completeness=${_CM_COMPL} index_mtime=${_IDX_MTIME}"
fi
```

> Consumer wrappers run this batch pattern (or a shorter inline variant — `central --top 3` + one derived query — for quick tasks), pointing here for the full map. `scan-query` not found or index missing → the pre-flight emits nothing, callers fall back to normal exploration path.

## Evidence-line contract

Every run emits one `codemap_evidence:` line summarising retrieval reliability:

```
codemap_evidence: queries_run=<n> hits=<h> completeness=<exhaustive|partial|stale|unknown> index_mtime=<iso|?>
```

Completeness semantics:

- `exhaustive` — all queries hit, none stale, none direction-incomplete → consumers may **skip** re-querying (grep/read) for what codemap returned.
- `partial` — at least one result `query_complete:false` (direction-incomplete) → fill gaps via consumer's fallback (semble, grep), not by re-running codemap.
- `stale` — index older than source (`stale:true`) → rebuild or accept reduced currency; see gates contract.
- `unknown` — no query hit → index empty or target absent; fall back to file reads.

Consumers may skip re-querying **only** when `completeness=exhaustive`.

## Coverage metadata in output

Each `scan-query` result carries `index` block with per-command coverage fields:

- `index.method` — analysis technique used (`static-ast`, `import-graph`, `index-lookup`, `ast-flags`).
- `index.not_covered` — what method structurally misses (list); non-empty → surface as scope caveat in response; do NOT grep to fill gap — gaps structurally unresolvable by static analysis.
- `index.hint` — actionable alternative if deeper coverage needed (e.g. grep pattern for hook-registered callers).
- `index.confidence: "exact"` — result authoritative; omit verification caveats.

**Codemap = primary codebase navigation tool.** Do NOT grep/bash to re-verify what codemap already returned. When `not_covered` non-empty: (1) include one-line caveat — "Note: callers via [not_covered items] not included — structurally invisible to static AST"; (2) log gap:

```bash
mkdir -p .cache/codemap
printf '{"ts":"%s","cmd":"%s","target":"%s","not_covered":%s,"hint":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<subcommand>" "<target>" '<not_covered_json>' "<hint_or_empty>" \
    >> .cache/codemap/gaps.jsonl 2>/dev/null || true
```

(3) Continue achieving goal — do NOT abandon task because of structural gap.

When `method=index-lookup` + `confidence=exact`: result authoritative, skip verification caveats.

## Effort-tier guidance

Scale query set to task blast-radius; more queries cost more tokens.

Set `CODEMAP_QUERY_KIND=skip` and skip Codemap when an exact file and symbol are supplied for a localized edit and no caller, dependency, blast-radius, test-impact, or coupling fact remains unresolved. An explicit structural query or tool requirement overrides this skip; otherwise set the kind for the smallest complete query.

- **quick** (one unresolved structural fact): run only `central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling` as matches that fact. Do not add centrality or a transitive walk by default.
- **standard** (feature/fix touching one module): add `fn-blast` + `symbol --with-imports`; add wrapper's per-agent dimensions.
- **deep** (multi-module / public-API change): run per-affected-module reverse-dependency batch (below), tier blast radius.

## Extended scan — multi-file / API changes

Task touches multiple modules or changes public-API surface → run per-affected-module reverse-dependency scan. Interpret: any `rdeps` output = external callers affected; `coupled` output = co-change pairs. Fallback for one known module: `scan-query rdeps <mod> --top 10 2>/dev/null || true`.

Risk tier by `rdep_count`:

- `>= 5` → HIGH blast radius — flag before proceeding.
- `1–4` → MODERATE — note in plan/report.
- `0` → LOW — proceed normally.

> Batch producer (plugin `bin/` script or inline per-module loop) and any persisted-cache artifact are plugin-local — those paths live in wrapper, not here.

## Targeted-edit pattern (known symbol, large file)

Known symbol + file >~300 lines: `symbol <mod::name>` → take line span → `Read(offset=span_start−10, limit=span_len+20)` → Edit. Slice Read suffices — Edit needs only slice containing target, not whole file. Spans come from index; file changed since scan → spans may drift (self-heal usually covers it). Edit errors "Found N matches" (`old_string` not file-wide unique) or no-match (drifted) → full `Read`, then Edit with larger unique `old_string`.

## Result-prepend contract

Prepend returned results as `## Structural Context (codemap)` section to any agent spawn prompt, with hotspot JSON + per-query output. `codemap_evidence:` line at end of block reports retrieval reliability; agents may skip re-querying only when `completeness=exhaustive`. `scan-query` unavailable or index missing → emit one-line ⚠ to stderr, proceed with file-read context.
