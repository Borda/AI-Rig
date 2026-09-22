<!-- file: codemap-context.md — consumers: plugin-local codemap-context.md wrappers (foundry, develop) read this file from the active codemap-py install via `resolve_shared_path.py codemap-py claude-skills/_shared`; provider→consumer wiring uses the codemap-py.integration.v2 managed-block body with a v1 sentinel schema -->

# Codemap context contract — v4

Plugin-agnostic structural-context contract. Consumers read this file live from the active `codemap-py` install (no local copies — the text always matches the CLI actually installed). Wrappers add only per-agent query maps + flag surfaces + plugin-local batch/cache paths; query mechanics, evidence-line contract, completeness/staleness semantics, batch pre-flight, effort tiers stay maintained here.

> `v4` = context-contract doc version — bump on query-set or evidence-contract change. Provider→consumer wiring uses `codemap-py.integration.v2` managed-block body with `v1` sentinel schema (see `shared/integration-contract.md`), independent of this doc version.

## Target derivation — pluggable (consumer supplies)

`TARGET_MODULE` (dotted), `TARGET_FN` (bare name), `CODEMAP_QUERY_KIND` = **consumer-supplied inputs** — contract doesn't derive them. Consumer wrapper/SKILL sets them from `$ARGUMENTS`, review diff, or finding before reading this file. `CODEMAP_QUERY_KIND=skip` = executable zero-query route for a fully localized edit. Safe adaptive vocabulary: `skip`, `central`, `callers`, `blast`, `dependencies`, `test-impact`, `coupling`, `standard`; unset/unknown value preserves legacy `standard` batch.

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

Reference bash, single-target run. Consumers inline it or call plugin-local batch producer; completeness/evidence logic below stays invariant.

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || _ROOT="$PWD"
PROJ=$(basename "$_ROOT")
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
_CM_ROUTE="${CODEMAP_QUERY_KIND:-standard}"
if [ "$_CM_ROUTE" != "skip" ] && command -v scan-query >/dev/null 2>&1 && [ -f "${_IDX}/${PROJ}.json" ]; then
    # refresh SHA-changed files only; never full-build mid-task
    [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ] || scan-index --incremental 2>/dev/null || true
    _CM_N=0 _CM_H=0 _CM_STALE=0 _CM_NONEXH=0
    # tsv only for flat wide tables: rdeps/test-impact exit 1 "format_not_tabular", symbol = 1 row.
    # --format is codemap-py 0.37.0+; older builds exit 2 with empty stdout, so probe once.
    _CM_TSV=0
    case "$(scan-query --help 2>&1)" in *--format*) _CM_TSV=1 ;; esac
    _cq() {
        local out err envelope fmt=json rc=0; _CM_N=$((_CM_N+1))
        err=$(mktemp "${TMPDIR:-/tmp}/codemap-envelope-XXXXXX")
        case "${_CM_TSV}:$1" in
            1:central|1:coupled|1:fn-rdeps|1:fn-blast) fmt=tsv; out=$(scan-query --timeout 5 --format tsv "$@" 2>"$err") || rc=$? ;;
            *)                                        out=$(scan-query --timeout 5 "$@" 2>"$err") || rc=$? ;;
        esac
        envelope=$(cat "$err" 2>/dev/null); rm -f "$err"
        if [ "$rc" -ne 0 ]; then printf 'codemap query unavailable: %s\n' "$1" >&2; return 0; fi
        # Errors stay JSON on stdout under any --format, so this test is format-independent.
        case "$out" in *'"error"'*) return 0 ;; esac
        # Empty tsv writes no rows but still an envelope — judge emptiness on the envelope's stream.
        case "$fmt" in
            tsv) case "$envelope" in '') return 0 ;; esac ;;
            *)   case "$out" in '') return 0 ;; esac ;;
        esac
        _CM_H=$((_CM_H+1))
        [ -n "$out" ] && printf '%s\n' "$out"
        # tsv envelope on stderr, JSON envelope in $out — scan both or a stale index reads exhaustive.
        case "$out$envelope" in *'"stale":true'*|*'"stale": true'*) _CM_STALE=1 ;; esac
        # Missing metadata never proves completeness; the forward field takes precedence.
        case "$out$envelope" in
            *'"query_complete"'*) case "$out$envelope" in *'"query_complete":true'*|*'"query_complete": true'*) ;; *) _CM_NONEXH=1 ;; esac ;;
            *) case "$out$envelope" in *'"exhaustive":true'*|*'"exhaustive": true'*) ;; *) _CM_NONEXH=1 ;; esac ;;
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
    elif [ "$_CM_H" -eq 0 ]; then _CM_COMPL="unknown"
    elif [ "$_CM_NONEXH" -eq 1 ] || [ "$_CM_H" -lt "$_CM_N" ]; then _CM_COMPL="partial"
    else _CM_COMPL="exhaustive"
    fi
    echo "codemap_evidence: queries_run=${_CM_N} hits=${_CM_H} completeness=${_CM_COMPL} index_mtime=${_IDX_MTIME}"
fi
```

> Consumer wrappers run this batch pattern (or a shorter inline variant — `central --top 3` + one derived query — for quick tasks), pointing here for full map. `scan-query` not found or index missing → pre-flight emits nothing, callers fall back to normal exploration path.

## Evidence-line contract

Every run emits one `codemap_evidence:` line summarising retrieval reliability:

```
codemap_evidence: queries_run=<n> hits=<h> completeness=<exhaustive|partial|stale|unknown> index_mtime=<iso|?>
```

Completeness semantics:

- `exhaustive` — all queries hit, none stale, none direction-incomplete → consumers may **skip** re-querying (grep/read) for what codemap returned.
- `partial` — at least one query failed, missed, lacked completeness metadata or returned `query_complete:false` → fill gaps via consumer's fallback (grep, targeted file reads), not by re-running identical codemap queries.
- `stale` — index older than source (`stale:true`) → rebuild or accept reduced currency; see gates contract.
- `unknown` — no query hit → index empty or target absent; fall back to file reads.

Consumers may skip re-querying **only** when `completeness=exhaustive`.

> Reuse only successful answers for the same project, current index, target, query and flags. Inspect each batch child's `ok` and `result.index`; outer success never clears a failed child. Missing, stale, root-mismatched or degraded evidence cannot settle the question. A valid empty list is an answer, not a miss. Completeness is direction-scoped, not an untruncated enumeration. Necessary source-body, test-quality, dynamic-behavior and independent-review reads remain allowed.

`uncovered` reports absent static test callers and mocks, not measured line coverage; mock relationships do not prove implementation execution. Missing measurements are unknown, not zero. Module scope is exact: enumerate child modules explicitly for package-wide questions.

## Coverage metadata in output

Each `scan-query` result carries `index` block with per-command coverage fields:

- `index.method` — analysis technique used (`static-ast`, `import-graph`, `index-lookup`, `ast-flags`).
- `index.not_covered` — what method structurally misses (list); non-empty → surface as scope caveat in response. Targeted source/runtime evidence may answer a different dynamic-behavior question; do not claim it makes the static graph complete.
- `index.hint` — actionable alternative if deeper coverage needed (e.g. grep pattern for hook-registered callers).
- `index.confidence: "exact"` — exact within the reported method and scope; does not override freshness, completeness or truncation limits.

**Codemap = primary codebase navigation tool.** Do NOT grep/bash to re-verify what codemap already returned. When `not_covered` non-empty: (1) include one-line caveat — "Note: callers via [not_covered items] not included — structurally invisible to static AST"; (2) log gap:

```bash
mkdir -p .cache/codemap
printf '{"ts":"%s","cmd":"%s","target":"%s","not_covered":%s,"hint":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<subcommand>" "<target>" '<not_covered_json>' "<hint_or_empty>" \
    >> .cache/codemap/gaps.jsonl 2>/dev/null || true
```

(3) Continue achieving goal — do NOT abandon task because of structural gap.

When `method=index-lookup` + `confidence=exact`: reuse the lookup subject to the same freshness and scope limits; avoid redundant verification of that fact.

## Effort-tier guidance

Scale query set to task blast-radius; more queries cost more tokens.

Set `CODEMAP_QUERY_KIND=skip`, skip Codemap when exact file+symbol supplied for a localized edit and no caller, dependency, blast-radius, test-impact, or coupling fact remains unresolved. Explicit structural query or tool requirement overrides this skip; otherwise set kind for smallest complete query.

- **quick** (one unresolved structural fact): run only `central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling` matching that fact. Do not add centrality or transitive walk by default.
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

Known symbol + file >~300 lines: `symbol <mod::name>` → take line span → `Read(offset=span_start−10, limit=span_len+20)` → Edit. Slice Read suffices — Edit needs only target's slice, not whole file. Spans come from index; file changed since scan → spans may drift (self-heal usually covers it). Edit errors "Found N matches" (`old_string` not file-wide unique) or no-match (drifted) → full `Read`, then Edit with larger unique `old_string`.

## Result-prepend contract

Prepend returned results as `## Structural Context (codemap)` section to any agent spawn prompt, with hotspot JSON + per-query output. `codemap_evidence:` line at end of block reports retrieval reliability; agents may skip re-querying only when `completeness=exhaustive`. `scan-query` unavailable or index missing → emit one-line ⚠ to stderr, proceed with file-read context.
