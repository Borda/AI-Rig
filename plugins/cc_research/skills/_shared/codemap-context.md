<!-- file: codemap-context.md — consumers: research/skills/run, verify -->

**Structural context (codemap-py)** — re-read the caller's persisted enabled state and currency before querying; skip if disabled.

Callers may pre-set `TARGET_MODULE` (dotted) and `TARGET_FN` (bare function name) before reading this file — typically module/function the experiment or verification edits. Both empty → only global `central` baseline runs.

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)   # `basename ""` exits 0, so `||` never fired
[ -n "$_ROOT" ] || _ROOT="$PWD"
PROJ=$(basename "$_ROOT")   # raw basename — scanner writes it verbatim, never sanitized
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"   # root-anchored: skill may run from a subdir
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/${CODEMAP_SENTINEL_SLUG}-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/research-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
_CM_NO_HEAL=""
if [ "$CODEMAP_CURRENCY" = "stale" ] || [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then _CM_NO_HEAL="--no-heal"; fi
if [ "$CODEMAP_ENABLED" = "true" ] && command -v codemap-py >/dev/null 2>&1; then
    _CM_N=0 _CM_H=0
    _cq() {
        local out; _CM_N=$((_CM_N+1))
        if ! out=$(codemap-py query --timeout 5 $_CM_NO_HEAL "$@" 2>/dev/null); then
            printf 'codemap query unavailable: %s\n' "$1" >&2
            return 0
        fi
        case "$out" in
            *'"error"'*|'') printf 'codemap query unavailable: %s\n' "$1" >&2 ;;
            *) _CM_H=$((_CM_H+1)); printf '%s\n' "$out" ;;
        esac
    }
    _cq central --top 5
    [ -n "$TARGET_FN" ]     && _cq fn-rdeps "${TARGET_MODULE}::${TARGET_FN}" --exclude-tests  # direct callers
    [ -n "$TARGET_MODULE" ] && _cq rdeps "$TARGET_MODULE" --limit 10  # importer blast-radius
    [ -n "$TARGET_MODULE" ] && _cq uncovered --top 20 "$TARGET_MODULE"  # test gaps
fi
```

> Query map:
>
> - `central --top 5` — global blast-radius baseline
> - `fn-rdeps --exclude-tests` — direct callers of the edited function (skip redundant caller-walk reads)
> - `rdeps --limit 10` — modules importing the edited target; risk tier by count: `>=5` HIGH, `1–4` MODERATE, `0` LOW
> - `uncovered --top 20` — public symbols in the exact module without static test callers or mocks; not measured line coverage. Missing measurements are unknown, not zero. Package-wide checks explicitly enumerate child modules; an empty package initializer does not describe its descendants

Prepend `## Structural Context (codemap-py)` block with this output to relevant agent spawn prompt, followed by this **codemap-first protocol** (own copy — self-contained, no cross-plugin reference):

> Reuse gate: reuse a supplied answer only for the same project, current index, target, query and flags; skip its duplicate pre-flight call. Require success and direction-complete metadata. For batch children require `ok: true` and inspect `result.index`; `ok: false` is a failure, never an empty answer. Missing metadata, `stale`, root mismatch, degraded or incomplete results need targeted fallback. Use legacy `exhaustive: true` only when `query_complete` is absent. A valid empty list settles that scoped query; truncation does not enumerate all matches. Necessary source-body reads, test-quality checks, dynamic behavior and required independent verification remain allowed.

1. **Skill-first**: consult the structural context above BEFORE any Grep/Glob/Read aimed at imports, callers, or test coverage for a symbol already listed there — never re-derive what's already answered.
2. **Query choice inheritance**: the orchestrator supplies the invocation’s exact enabled/currency sentinel paths in this prompt. Before every fresh shell that queries Codemap or runs a querying helper, reload those files; disabled means no queries, stale means `export SCAN_NO_AUTOBUILD=1` before the command. Retain an inherited freeze. This also applies to fallback and impact/test-selection helpers. **Bounded call budget**: context above insufficient for a symbol not listed → agent may run `codemap-py query` directly, max 3 additional queries this task.
3. **Hard stop on `query_complete: true`**: a result passing the reuse gate and carrying `query_complete: true` (legacy `exhaustive: true` only when `query_complete` is absent) is final for that query direction — write the answer immediately, no follow-up Grep/Read/query to re-confirm it.

For listed symbols, reuse only answers passing the reuse gate. Static mock relationships do not establish runtime execution or assertion quality. Read source and tests when the research question requires implementation, formula or behavioral verification.

`codemap-py` not found, persisted `CODEMAP_ENABLED=false`, index missing under an explicit freeze, or block above produced no output: omit the protocol paragraph entirely — agent proceeds with normal file-read behaviour. The first ordinary query prepares a missing index unless inherited `SCAN_NO_AUTOBUILD=1` freezes preparation. A `stale` currency sentinel is retained after Gate B's continue-with-stale choice, so this block and later queries carry `--no-heal` until a successful rebuild updates the sentinel.
