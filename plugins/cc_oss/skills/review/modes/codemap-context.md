<!-- file: codemap-context.md — consumers: review/SKILL.md -->

<!-- oss:review Step 1 — executed via: > loads: modes/codemap-context.md; gated on CODEMAP_ENABLED=true -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md Step 1 -->

<!-- Input: CODEMAP_ENABLED, CHANGED_FILES, CLEAN_ARGS, _IDX, CICD_ONLY_MODE, DOCS_ONLY_MODE, DOCS_CICD_MODE (reload guards default false — missing sentinel never causes a false skip, only extra queries) -->

<!-- Output: codemap_available, $CODEMAP_CONTEXT_STAGE; persists both to TMPDIR for Step 2 -->

### Structural context + review pre-flight (codemap-py — only if `CODEMAP_ENABLED=true`)

**Skip entire section if `CODEMAP_ENABLED=false`** — sets `codemap_available=false` for downstream agent prompts; agents fall back to file reads.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
codemap_available=false
# index dir anchors at git root, not cwd — subdir invocation otherwise reports no_index. PROJ = raw basename, unsanitized (space/+/non-ASCII survive). `[ -n ]` not `||`: `basename ""` exits 0 — old fallback was dead, non-git project got PROJ="".
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
PROJ=$(basename "$_ROOT")
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
# $RUN_DIR made in Step2; stage to TMPDIR, copied later
CODEMAP_CONTEXT_STAGE="${TMPDIR:-/tmp}/oss-review-codemap-context-${CLEAN_ARGS}-${CSID}.md"
# reload mode flags (scope-detection.md pattern) — gates test/docs sub-batteries behind consuming agent; missing sentinel → all flags false, run everything
[ -f "${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}" ] && . "${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}"
CICD_ONLY_MODE="${CICD_ONLY_MODE:-false}"; DOCS_ONLY_MODE="${DOCS_ONLY_MODE:-false}"; DOCS_CICD_MODE="${DOCS_CICD_MODE:-false}"
if [ "$CODEMAP_ENABLED" = "true" ] && command -v codemap-py >/dev/null 2>&1 && [ -f "${_IDX}/${PROJ}.json" ]; then
    codemap_available=true
    # module names from index's own `name` field, never sed transform: `pkg/__init__.py` is `pkg`, not `pkg.__init__` — old `grep -v '__init__$'` dropped it entirely, an __init__-only PR got zero structural context. Files index doesn't know resolve to nothing, not a guessed name.
    _CHANGED_PY=$(printf '%s\n' "$CHANGED_FILES" | grep '\.py$' | paste -sd, -)
    CHANGED_MODS=$(codemap-py query --timeout 10 central --top 100000 2>/dev/null | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_centrality.py" --files "$_CHANGED_PY" --modules-only 2>/dev/null)
    {
        echo "## Structural Context (codemap-py)"
        echo
        echo "### Global blast-radius baseline"
        codemap-py query --timeout 5 central --top 5 2>/dev/null
        echo
        echo "### Change-set blast radius (diff-impact)"
        # PR diff not in local git objects — feed the Step-0 snapshot (no re-fetch); captured (not streamed) so fn-rdeps/fn-blast loop below reuses its qname derivation
        IFS= read -r _SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || _SNAP_DIR=""
        _DIFF_IMPACT_JSON=$(codemap-py query --timeout 15 diff-impact --diff-file "$_SNAP_DIR/pr.diff" 2>/dev/null)
        printf '%s\n' "$_DIFF_IMPACT_JSON"
        echo
        echo "### Changed-function callers (fn-rdeps/fn-blast)"
        # fn-rdeps/fn-blast need module::fn qnames — bare-module calls failed 100% in prod. diff-impact derives qnames but only exposes caller_count not list (0.177x tokens, e.g. enumerate subclass overrides pre-signature-edit).
        # reuse diff-impact's qname derivation via extract_diff_impact_qnames.py — no separate hunk-parsing
        printf '%s\n' "$_DIFF_IMPACT_JSON" | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/extract_diff_impact_qnames.py" --cap 12 2>/dev/null | while IFS= read -r qn; do
            [ -n "$qn" ] || continue
            echo "#### $qn"
            _FN_RDEPS_OUT=$(codemap-py query --timeout 5 fn-rdeps "$qn" 2>/dev/null)
            printf '%s\n' "$_FN_RDEPS_OUT"
            # fn-blast (costlier) only if fn-rdeps found >=1 caller — nothing to blast at zero callers
            echo "$_FN_RDEPS_OUT" | grep -q '"count": *[1-9]' && codemap-py query --timeout 8 fn-blast "$qn" 2>/dev/null
            echo
        done
        # while-read, NOT `for mod in $CHANGED_MODS` — zsh doesn't word-split unquoted vars, for-loop passed whole list as ONE arg → every battery call failed "module not indexed" (~all CLI errors across 4 projects)
        # cap 10 modules — bounds battery wall time on wide PRs; truncation logged, never silent
        _MOD_TOTAL=$(printf '%s\n' "$CHANGED_MODS" | grep -c .)
        [ "$_MOD_TOTAL" -gt 10 ] && echo "⚠ module battery capped at 10 of $_MOD_TOTAL changed modules"
        printf '%s\n' "$CHANGED_MODS" | head -10 | while IFS= read -r mod; do
            [ -n "$mod" ] || continue
            echo "### Module: $mod"
            codemap-py query --timeout 5 rdeps "$mod" 2>/dev/null  # importer count → risk tier; unconditional, every agent's blast-radius ref
            echo
        done
        # mock-rdeps/uncovered/xrefs/undocumented moved to the post-ranking supplement below — 57% of query volume, consumed only by qa-specialist/doc-scribe; run only when the final lineup spawns them
    } > "$CODEMAP_CONTEXT_STAGE"
    printf '%s\n' "$CHANGED_MODS" | head -10 > "${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}"
fi
echo "$codemap_available"      > "${TMPDIR:-/tmp}/oss-review-codemap-available-${CLEAN_ARGS}-${CSID}"
echo "$CODEMAP_CONTEXT_STAGE"  > "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}"
```

## Dimension-gated supplement — Step 2, AFTER lineup ranking, BEFORE the `$RUN_DIR` copy

These query families feed exactly one dimension each; under default `FANOUT_MAX` cap that dimension is often not spawned — running them for every PR was 57% of query volume with no benchmarked win. Gate on **final ranked lineup**, not PR mode flags:

- `foundry:qa-specialist` in the final lineup → run the qa block
- `foundry:doc-scribe` in the final lineup → run the docs block
- neither → skip both; the staged context is already complete

qa block (mock coverage v4.1 + test gaps v4.2):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
[ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ] && while IFS= read -r mod; do
    [ -n "$mod" ] || continue
    echo "### QA queries: $mod"
    codemap-py query --timeout 5 mock-rdeps "$mod" 2>/dev/null
    codemap-py query --timeout 5 uncovered --top 20 "$mod" 2>/dev/null
    echo
done < "${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}" >> "$CODEMAP_CONTEXT_STAGE"
```

docs block (stale doc refs v4.5 + doc coverage v4.4):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${CLEAN_ARGS}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
[ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ] && while IFS= read -r mod; do
    [ -n "$mod" ] || continue
    echo "### Docs queries: $mod"
    codemap-py query --timeout 5 xrefs --broken "$mod" 2>/dev/null
    codemap-py query --timeout 5 undocumented "$mod" 2>/dev/null
    echo
done < "${TMPDIR:-/tmp}/oss-review-changed-mods-${CLEAN_ARGS}-${CSID}" >> "$CODEMAP_CONTEXT_STAGE"
```

`codemap_available=true`: Step 2 copies `$CODEMAP_CONTEXT_STAGE` to `$RUN_DIR/codemap-context.md` after `$RUN_DIR` created. Every dimension-agent spawn prompt in Step 2 must then include a literal block (substituted from `$RUN_DIR/codemap-context.md`):

```text
## Structural Context (codemap-py, codemap_available=true)
<content of $RUN_DIR/codemap-context.md>

**Codemap-first protocol** (`codemap_substitution_contract` — availability without enforcement measured a 13.4:1 logged-reads-to-queries ratio; this is the fix, verbatim from codemap-py README's three-part contract):
1. **Skill-first**: consult structural context above BEFORE any Grep/Glob/Read aimed at imports, callers, test coverage, or doc coverage for a symbol already listed there — never re-derive what's already answered.
2. **Bounded call budget**: context above insufficient for a symbol not listed → may run codemap-py queries directly, max 3 additional queries this task.
3. **Hard stop on `query_complete: true`**: any codemap-py result carrying `query_complete: true` (or legacy `exhaustive: true`) is final for that query direction — write the answer immediately, no follow-up Grep/Read/query to re-confirm it.

For symbols listed in `uncovered`/`mock-rdeps`/`undocumented`/`xrefs --broken`/`fn-rdeps`/`fn-blast` above: trust codemap-py output as-is. Fall back to file reads only when codemap-py output empty for a symbol needed, or a result shows `query_complete: false`/`degraded` and you must confirm by hand.
```

`codemap_available=false`: omit the block; agents proceed with current file-read behaviour.

Tier annotation for Agent 1 (sw-engineer) only: label each module's `imported_by` count — **high risk** (>20), **moderate** (5–20), **low** (\<5) — for blast-radius reference.
