<!-- file: codemap-context.md — consumers: review/SKILL.md -->

<!-- oss:review Step 1 — executed via: > loads: modes/codemap-context.md; gated on CODEMAP_ENABLED=true -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md Step 1 -->

<!-- Input: CHANGED_FILES, CLEAN_ARGS, CICD_ONLY_MODE, DOCS_ONLY_MODE, DOCS_CICD_MODE (reload guards default false — missing sentinel never causes a false skip, only extra queries); Codemap enabled/currency are reloaded from review-owned sentinels below -->

<!-- Output: codemap_available, $CODEMAP_CONTEXT_STAGE; persists both to TMPDIR for Step 2 -->

### Structural context + review pre-flight (codemap-py — only if `CODEMAP_ENABLED=true`)

**Skip entire section if `CODEMAP_ENABLED=false`** — sets `codemap_available=false` for downstream agent prompts; agents fall back to file reads.

**Change-set impact at the reviewed head.** `codemap-py context` (codemap-py `structural-context-contract.md`) builds or reuses a per-head index under `${TMPDIR:-/tmp}/codemap-context`, outside every checkout, refuses a root not at the snapshot head (`--expect-head`), and writes one record with a closed-set `status`/`cause`. The impact tier comes from that record (`review_impact_tier.py --context-record`); an `unavailable` record names its cause in `Impact:`, and its text-search fallback goes into the staged context as low-confidence evidence only. Step 2 copies the record to `$RUN_DIR/codemap-structural-context.json`. Launcher exit 127 → `codemap-interpreter-unsupported`; only a launcher without the `context` command (usage exit 2, `unknown command 'context'`) keeps the earlier `diff-impact` query on this checkout's index when its full `HEAD` matches the snapshot head (otherwise `head-mismatch`); a request the command refused (usage exit 2, `invalid_context_request`) → `context-request-invalid`; any other missing record (unwritten `--out`, crash) → `query-failed`; neither ever queries this checkout's index. A freeze the caller inherited (`SCAN_NO_AUTOBUILD=1`) also forbids building the per-head index; the stale-currency freeze covers only this checkout's index.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
codemap_available=false
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
# caller's own freeze binds the per-head context index too; the stale-derived export below covers only this checkout's index
_INHERITED_FREEZE="${SCAN_NO_AUTOBUILD:-0}"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
_CODEMAP_NO_HEAL=""
if [ "$CODEMAP_CURRENCY" = "stale" ] || [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then _CODEMAP_NO_HEAL="--no-heal"; fi
# $RUN_DIR made in Step2; stage to TMPDIR, copied later
CODEMAP_CONTEXT_STAGE="${TMPDIR:-/tmp}/oss-review-codemap-context-${CLEAN_ARGS}-${CSID}.md"
# reload mode flags (scope-detection.md pattern) — gates test/docs sub-batteries behind consuming agent; missing sentinel → all flags false, run everything
[ -f "${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in PR_TYPE) PR_TYPE=$_SV ;; DOCS_TYPING_MODE) DOCS_TYPING_MODE=$_SV ;; TESTS_CI_MODE) TESTS_CI_MODE=$_SV ;; CICD_ONLY_MODE) CICD_ONLY_MODE=$_SV ;; DOCS_ONLY_MODE) DOCS_ONLY_MODE=$_SV ;; DOCS_CICD_MODE) DOCS_CICD_MODE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}"
CICD_ONLY_MODE="${CICD_ONLY_MODE:-false}"; DOCS_ONLY_MODE="${DOCS_ONLY_MODE:-false}"; DOCS_CICD_MODE="${DOCS_CICD_MODE:-false}"
if [ "$CODEMAP_ENABLED" = "true" ] && command -v codemap-py >/dev/null 2>&1; then
    codemap_available=true
    # module names from index's own `name` field, never sed transform: `pkg/__init__.py` is `pkg`, not `pkg.__init__` — old `grep -v '__init__$'` dropped it entirely, an __init__-only PR got zero structural context. Files index doesn't know resolve to nothing, not a guessed name.
    _CHANGED_PY=$(printf '%s\n' "$CHANGED_FILES" | grep '\.py$' | paste -sd, -)
    CHANGED_MODS=$(codemap-py query --timeout 10 $_CODEMAP_NO_HEAL central --top 100000 2>/dev/null | python "$PLUGIN_ROOT/bin/resolve_centrality.py" --files "$_CHANGED_PY" --modules-only 2>/dev/null)
    {
        echo "## Structural Context (codemap-py)"
        echo
        echo "### Global blast-radius baseline"
        codemap-py query --timeout 5 $_CODEMAP_NO_HEAL central --top 5 2>/dev/null
        echo
        echo "### Change-set blast radius (diff-impact)"
        # PR diff not in local git objects — feed the Step-0 snapshot (no re-fetch); captured (not streamed) so fn-rdeps/fn-blast loop below reuses its qname derivation
        IFS= read -r _SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || _SNAP_DIR=""
        # PR context requires the committed Python source at the snapshot head; Step 2 copies the staged record to $RUN_DIR
        _CONTEXT_JSON="${TMPDIR:-/tmp}/oss-review-codemap-structural-${CLEAN_ARGS}-${CSID}.json"
        _CONTEXT_ERR="${TMPDIR:-/tmp}/oss-review-codemap-structural-${CLEAN_ARGS}-${CSID}.err"
        : > "$_CONTEXT_JSON"
        _PR_HEAD_SHA=$(jq -r '.headRefOid // empty' "$_SNAP_DIR/pr-meta.json" 2>/dev/null)
        SCAN_NO_AUTOBUILD="$_INHERITED_FREEZE" codemap-py context --root "$(git rev-parse --show-toplevel)" --diff-file "$_SNAP_DIR/pr.diff" --expect-head "$_PR_HEAD_SHA" --index-dir "${TMPDIR:-/tmp}/codemap-context" --timeout 60 --out "$_CONTEXT_JSON" >/dev/null 2>"$_CONTEXT_ERR"  # timeout: 450000 — whole battery: context build+query ≤2×60 s + fn-rdeps/fn-blast loop + ≤12 tier fn-blast queries
        _CONTEXT_RC=$?
        # review depth from code path, not diff size — overrides Step 1's fail-safe FULL default; stdout to sentinel, not stage. Unavailable record, error answer, stale/root-mismatch index → FULL with that cause named; text-search fallback is evidence only, never the tier
        if [ -s "$_CONTEXT_JSON" ]; then
            _DIFF_IMPACT_JSON=$(jq -c '.diff_impact // empty' "$_CONTEXT_JSON" 2>/dev/null)
            printf '%s\n' "$_DIFF_IMPACT_JSON"
            jq -c '{context: .status, cause, detail, index: .index.action, degraded_reasons}' "$_CONTEXT_JSON" 2>/dev/null
            jq -e '.fallback != null' "$_CONTEXT_JSON" >/dev/null 2>&1 && echo "#### Text-search fallback (method: text-search, confidence: low — candidate files, not resolved edges; zero hits = unknown, never no impact)" && jq -c '.fallback | {modules: [(.modules // [])[] | {path, importer_files, importer_truncated}], symbols: [(.symbols // [])[] | {name, path, reference_files, reference_truncated}], not_covered}' "$_CONTEXT_JSON" 2>/dev/null
            python "$PLUGIN_ROOT/bin/review_impact_tier.py" --context-record "$_CONTEXT_JSON" > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}" 2>/dev/null
        elif [ "$_CONTEXT_RC" = 127 ]; then
            _DIFF_IMPACT_JSON=""
            python "$PLUGIN_ROOT/bin/review_impact_tier.py" --unknown codemap-interpreter-unsupported > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}"
        elif [ "$_CONTEXT_RC" = 2 ] && grep -q -e "unknown command 'context'" -e "invalid choice: 'context'" "$_CONTEXT_ERR" 2>/dev/null; then
            # The older provider cannot verify the source revision for us.
            if [ -n "$_PR_HEAD_SHA" ] && _CHECKOUT_HEAD=$(git rev-parse --verify HEAD 2>/dev/null) && [ "$_CHECKOUT_HEAD" = "$_PR_HEAD_SHA" ]; then
                _DIFF_IMPACT_JSON=$(codemap-py query --timeout 15 $_CODEMAP_NO_HEAL diff-impact --diff-file "$_SNAP_DIR/pr.diff" 2>/dev/null)
                printf '%s\n' "$_DIFF_IMPACT_JSON"
                printf '%s\n' "$_DIFF_IMPACT_JSON" | python "$PLUGIN_ROOT/bin/review_impact_tier.py" > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}" 2>/dev/null
            else
                _DIFF_IMPACT_JSON=""
                python "$PLUGIN_ROOT/bin/review_impact_tier.py" --unknown head-mismatch > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}"
            fi
        elif [ "$_CONTEXT_RC" = 2 ] && grep -q -e '"invalid_context_request"' "$_CONTEXT_ERR" 2>/dev/null; then
            _DIFF_IMPACT_JSON=""
            python "$PLUGIN_ROOT/bin/review_impact_tier.py" --unknown context-request-invalid > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}"
        else
            # provider has `context` but wrote no record (unwritten --out, crash): name it; this checkout's index is not the reviewed head's
            _DIFF_IMPACT_JSON=""
            python "$PLUGIN_ROOT/bin/review_impact_tier.py" --unknown query-failed > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}"
        fi
        echo
        echo "### Changed-function callers (fn-rdeps/fn-blast)"
        # fn-rdeps/fn-blast need module::fn qnames — bare-module calls failed 100% in prod. diff-impact derives qnames but only exposes caller_count not list (0.177x tokens, e.g. enumerate subclass overrides pre-signature-edit).
        # reuse diff-impact's qname derivation via extract_diff_impact_qnames.py — no separate hunk-parsing
        printf '%s\n' "$_DIFF_IMPACT_JSON" | python "$PLUGIN_ROOT/bin/extract_diff_impact_qnames.py" --cap 12 2>/dev/null | while IFS= read -r qn; do
            [ -n "$qn" ] || continue
            echo "#### $qn"
            _FN_RDEPS_OUT=$(codemap-py query --timeout 5 $_CODEMAP_NO_HEAL fn-rdeps "$qn" 2>/dev/null)
            printf '%s\n' "$_FN_RDEPS_OUT"
            # fn-blast (costlier) only if fn-rdeps found >=1 caller — nothing to blast at zero callers
            echo "$_FN_RDEPS_OUT" | grep -q '"count": *[1-9]' && codemap-py query --timeout 8 $_CODEMAP_NO_HEAL fn-blast "$qn" 2>/dev/null
            echo
        done
        # while-read, NOT `for mod in $CHANGED_MODS` — zsh doesn't word-split unquoted vars, for-loop passed whole list as ONE arg → every battery call failed "module not indexed" (~all CLI errors across 4 projects)
        # cap 10 modules — bounds battery wall time on wide PRs; truncation logged, never silent
        _MOD_TOTAL=$(printf '%s\n' "$CHANGED_MODS" | grep -c .)
        [ "$_MOD_TOTAL" -gt 10 ] && echo "⚠ module battery capped at 10 of $_MOD_TOTAL changed modules"
        printf '%s\n' "$CHANGED_MODS" | head -10 | while IFS= read -r mod; do
            [ -n "$mod" ] || continue
            echo "### Module: $mod"
            codemap-py query --timeout 5 $_CODEMAP_NO_HEAL rdeps "$mod" 2>/dev/null  # importer count → risk tier; unconditional, every agent's blast-radius ref
            echo
        done
        # mock-rdeps/uncovered/xrefs/undocumented moved to the post-ranking supplement below — 57% of query volume, consumed only by qa-specialist/doc-scribe; run only when the final lineup spawns them
    } > "$CODEMAP_CONTEXT_STAGE"
    printf '%s\n' "$CHANGED_MODS" | head -10 > "${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}"
elif [ "$CODEMAP_ENABLED" = "true" ]; then
    # enabled but no binary: name that cause instead of keeping the generic `codemap unavailable` default
    python "$PLUGIN_ROOT/bin/review_impact_tier.py" --unknown codemap-not-installed > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}"
fi
echo "$codemap_available"      > "${TMPDIR:-/tmp}/oss-review-codemap-available-${CLEAN_ARGS}-${CSID}"
echo "$CODEMAP_CONTEXT_STAGE"  > "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}"
```

## Dimension-gated supplement — Step 2, AFTER lineup ranking, BEFORE the `$RUN_DIR` copy

These query families feed exactly one dimension each; under default `FANOUT_MAX` cap that dimension is often not spawned — running them for every PR was 57% of query volume with no benchmarked win. Gate on **final ranked lineup**, not PR mode flags:

- `foundry:qa-specialist` in the final lineup → run the qa block
- documentation dimension in the final lineup (merged lint+docs unit led by `foundry:linting-expert`, or standalone `foundry:doc-scribe`) → run the docs block
- neither → skip both; the staged context is already complete

qa block (mock coverage v4.1 + test gaps v4.2):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
[ "$CODEMAP_ENABLED" = "true" ] || exit 0
_CODEMAP_NO_HEAL=""
if [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then _CODEMAP_NO_HEAL="--no-heal"; fi
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
_MODS="${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}"
[ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ] && [ -s "$_MODS" ] || exit 0
# one codemap-py process for every module x query; --timeout is a whole-process alarm, scaled 5 s x 2 queries x N modules. Alarm/failure → batch prints nothing → `render --fallback` reruns each unanswered query alone, --timeout 5 as before: one slow query loses only its own line
_N=$(wc -l < "$_MODS")
python "$PLUGIN_ROOT/bin/codemap_batch.py" build qa "$_MODS" | codemap-py query --timeout $((_N * 10)) $_CODEMAP_NO_HEAL batch - 2>/dev/null | python "$PLUGIN_ROOT/bin/codemap_batch.py" render --fallback qa "$_MODS" >> "$CODEMAP_CONTEXT_STAGE"  # timeout: 330000 — per module ≤10 s batch alarm + ≤20 s fallback (2 queries x 10 s kill bound), 300 s at the 10-module cap
```

docs block (stale doc refs v4.5 + doc coverage v4.4):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/review-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
[ "$CODEMAP_ENABLED" = "true" ] || exit 0
_CODEMAP_NO_HEAL=""
if [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then _CODEMAP_NO_HEAL="--no-heal"; fi
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
_MODS="${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}"
[ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ] && [ -s "$_MODS" ] || exit 0
_N=$(wc -l < "$_MODS")  # whole-process alarm and --fallback as in the qa block
python "$PLUGIN_ROOT/bin/codemap_batch.py" build docs "$_MODS" | codemap-py query --timeout $((_N * 10)) $_CODEMAP_NO_HEAL batch - 2>/dev/null | python "$PLUGIN_ROOT/bin/codemap_batch.py" render --fallback docs "$_MODS" >> "$CODEMAP_CONTEXT_STAGE"  # timeout: 330000 — per module ≤10 s batch alarm + ≤20 s fallback (2 queries x 10 s kill bound), 300 s at the 10-module cap
```

`codemap_available=true`: Step 2 copies `$CODEMAP_CONTEXT_STAGE` to `$RUN_DIR/codemap-context.md` after `$RUN_DIR` created. Every dimension-agent spawn prompt in Step 2 must then include a literal block (substituted from `$RUN_DIR/codemap-context.md`):

```text
## Structural Context (codemap-py, codemap_available=true)
<content of $RUN_DIR/codemap-context.md>

**Codemap-first protocol** (`codemap_substitution_contract` — availability without enforcement measured a 13.4:1 logged-reads-to-queries ratio; this is the fix, verbatim from codemap-py README's three-part contract):
> Reuse gate: reuse a supplied answer only for the same project, current index, target, query and flags; skip its duplicate pre-flight call. Require success and direction-complete metadata. For batch children require `ok: true` and inspect `result.index`; `ok: false` is a failure, never an empty answer. Missing metadata, `stale`, root mismatch, degraded or incomplete results need targeted fallback. Use legacy `exhaustive: true` only when `query_complete` is absent. A valid empty list settles that scoped query; truncation does not enumerate all matches. Necessary source-body reads, test-quality checks, dynamic behavior and required independent verification remain allowed.

1. **Skill-first**: consult structural context above BEFORE any Grep/Glob/Read aimed at imports, callers, test coverage, or doc coverage for a symbol already listed there — never re-derive what's already answered.
2. **Query choice inheritance**: the orchestrator supplies the invocation’s exact enabled/currency sentinel paths in this prompt. Before every fresh shell that queries Codemap or runs a querying helper, reload those files; disabled means no queries, stale means `export SCAN_NO_AUTOBUILD=1` before the command. Retain an inherited freeze. This also applies to fallback and impact/test-selection helpers. **Bounded call budget**: context above insufficient for a symbol not listed → may run codemap-py queries directly, max 3 additional queries this task.
3. **Hard stop on `query_complete: true`**: a result passing the reuse gate and carrying `query_complete: true` (legacy `exhaustive: true` only when `query_complete` is absent) is final for that query direction — write the answer immediately, no follow-up Grep/Read/query to re-confirm it.

Reuse listed answers only under the reuse gate. `uncovered` reports missing static test callers and mocks, not measured line coverage; mock relationships do not prove implementation execution. Missing measurements are unknown, not zero. Module scope is exact; enumerate children for package-wide questions. Source and test reads remain necessary for behavioral review.
```

`codemap_available=false`: omit the block; agents proceed with current file-read behaviour.

Tier annotation for Agent 1 (sw-engineer) only: label each module's `imported_by` count — **high risk** (>20), **moderate** (5–20), **low** (\<5) — for blast-radius reference.
