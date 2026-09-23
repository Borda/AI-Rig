---
name: test-impact
description: 'Identify which tests need rerunning after a code change — traces static call graph (function-level) or import graph (module-level) to find affected test files, then emits a ready-to-run pytest command. TRIGGER when: user asks which tests are affected by a change; phrases: "which tests are affected", "what tests cover this", "test impact of", "what tests to rerun".'
argument-hint: <module::symbol | module> [--no-mocks]
allowed-tools: Bash, Write, AskUserQuestion
model: haiku
effort: low
---

<objective>

Find minimal tests affected by function/module change via Codemap static analysis; run no tests. Return ready `pytest` command for impacted tests only.

Two input modes:

- **Function-level** (`module::symbol`) — reverse-call-graph BFS; all direct/transitive test callers + symbol mocks (`mock_patches`).
- **Module-level** (bare `module`) — reverse-import-graph BFS; all tests importing through any chain + any module-symbol mocks.

`not_covered`: dynamic dispatch, hook callbacks, string-dispatch callers; same blind spot as `fn-blast`. Surface caveat; log gap.

NOT for: finding all callers of a function (use `/codemap-py:query-code fn-rdeps <module::symbol> --exclude-tests`); querying module deps or blast radius (use `/codemap-py:query-code`); running/executing tests (identified here, not executed).

</objective>

<inputs>

- **$ARGUMENTS**: `<qname> [--no-mocks]`
  - `qname` — `module::symbol` (function-level) or bare dotted module (module-level)
  - `--no-mocks` — exclude mock-only test files (no call/import path)
  - Omitted → AskUserQuestion in Step 1

</inputs>

<workflow>

## Step 0 — Ensure index

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")

# dispatcher, not scan-query/scan-index aliases — aliases lease in-engine too but skip dispatcher's interpreter probe (exit 127), deprecated shims removed no earlier than 1.0.0
# PATH-literal invocation everywhere below — expansion-bearing form matches no bare-name allow prefix; plugin's absolute bin/codemap-py stays interactive fallback
command -v codemap-py >/dev/null 2>&1 || { echo "codemap-py not on PATH — install the codemap-py plugin, or invoke its bin/codemap-py launcher as one standalone command"; exit 1; }

# git-root-anchored, not CWD-relative — resolve_proj_index.py resolves the same pair the engine uses (index_paths.py); a hand-rolled path here disagreed from a subdirectory
python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" --output-prefix "codemap-${_CM_PROJ}" 2>/dev/null
IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""
[ -n "$INDEX" ] || { echo "! resolve_index_env.py failed — check Python availability and CLAUDE_PLUGIN_ROOT"; exit 1; }

[ ! -f "$INDEX" ] && echo "No index found — will build via codemap-py index"
```

`SCAN_NO_AUTOBUILD=1` opts out: use index unchanged; no refresh/full build. Echo build wall-time when run, separating build/query cost.

If `$INDEX` missing:

- With `SCAN_NO_AUTOBUILD=1`: print `! codemap index missing and SCAN_NO_AUTOBUILD=1 — refusing to auto-build. Build it manually first: /codemap-py:scan-codebase`; exit 1.
- Otherwise run foreground `codemap-py index`; wait, then continue. Never invoke `codemap-py:scan-codebase`: `disable-model-invocation:true`, user-slash-only. Build through gated `codemap-py index` dispatcher.

```bash
# timeout: 400000
_CM_BUILD_T0=$(date +%s)
# forward only when user already overrode CODEMAP_INDEX_DIR — an unconditional export turns the
# engine's own git-root default into a flat-convention override (index_paths.py), same trap fixed
# below for the incremental-refresh branch
[ -n "${CODEMAP_INDEX_DIR:-}" ] && export CODEMAP_INDEX_DIR
codemap-py index \
    && echo "[codemap] index built in $(( $(date +%s) - _CM_BUILD_T0 ))s" \
    || { printf "! codemap-py index failed — cannot proceed without an index\n"; exit 1; }
```

If index already exists:

```bash
# timeout: 30000
if [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then
    echo "[codemap] SCAN_NO_AUTOBUILD=1 — using existing index as-is (no refresh)"
else
    _CM_BUILD_T0=$(date +%s)
    # forward only when user already overrode CODEMAP_INDEX_DIR — an unconditional export turns the
    # engine's own git-root default into a flat-convention override (index_paths.py), materializing
    # a second index when this ran from a subdirectory
    [ -n "${CODEMAP_INDEX_DIR:-}" ] && export CODEMAP_INDEX_DIR
    codemap-py index --incremental \
        && echo "[codemap] index built in $(( $(date +%s) - _CM_BUILD_T0 ))s" \
        || printf "⚠ codemap-py index --incremental failed — index may be stale; continuing\n"
fi
```

After build/refresh, re-verify index:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" --output-prefix "codemap-${_CM_PROJ}" 2>/dev/null
IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""
[ -n "$INDEX" ] && [ -f "$INDEX" ] || { printf "! Index not found after refresh at %s — check CODEMAP_INDEX_DIR or re-run /codemap-py:scan-codebase\n" "${INDEX:-<unresolved>}"; exit 1; }
```

## Step 1 — Parse arguments

Extract `QNAME` + `NO_MOCKS` from `$ARGUMENTS`.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
ARGS="${ARGUMENTS:-}"
QNAME=$(echo "$ARGS" | awk '{print $1}')
MOCKS_FLAG=$(echo "$ARGS" | grep -q -- "--no-mocks" && echo "--no-mocks" || echo "")
echo "$QNAME" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-qname-${CSID}"
echo "$MOCKS_FLAG" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-mocks-${CSID}"
```

If `$ARGUMENTS` empty, use `AskUserQuestion`: "Which function or module changed?" Options: (a) Enter `module::symbol` for function-level; (b) Enter bare module for module-level; (c) Cancel, exit without analysis. After answer, set `QNAME`; write tmpfile before Step 2:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
QNAME="<answer from AskUserQuestion>"
echo "$QNAME" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-qname-${CSID}"
```

**Multi-symbol guard**: `$ARGUMENTS` may contain multiple tokens (e.g. `mypackage.auth::validate mypackage.auth::parse`); `awk '{print $1}'` silently keeps first. If more than one after removing `--no-mocks`, print `⚠ test-impact accepts one symbol at a time — using first token only: $QNAME. Run separately for each remaining symbol.`

## Step 2 — Run test-impact query

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
IFS= read -r QNAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-qname-${CSID}" 2>/dev/null || QNAME=""
IFS= read -r MOCKS_FLAG < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-mocks-${CSID}" 2>/dev/null || MOCKS_FLAG=""
_TI_ERR="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-stderr-${CSID}"
# capture stderr to file, never 2>/dev/null — swallowed diagnostic rendered a broken index as "no affected tests" before
RESULT=$(codemap-py query test-impact "$QNAME" $MOCKS_FLAG 2>"$_TI_ERR")
_TI_RC=$?
if [ "$_TI_RC" -ne 0 ]; then
    printf "! test-impact query failed (exit %d) — this is NOT a 'no affected tests' result; do not report an empty test set\n" "$_TI_RC" >&2
    [ -s "$_TI_ERR" ] && sed -n '1,5p' "$_TI_ERR" >&2
    printf "Rebuild with /codemap-py:scan-codebase, then re-run.\n" >&2
    exit 1
fi
# one parse, no per-field `|| echo` defaults — default here forges total=0 from unparsable payload
printf '%s' "$RESULT" | python3 -c "
import json, sys
base, suf = sys.argv[1], sys.argv[2]
try:
    payload = json.loads(sys.stdin.read())
except ValueError:
    sys.stderr.write('! test-impact returned non-JSON output — NOT an empty result; rebuild via /codemap-py:scan-codebase and re-run\n')
    raise SystemExit(1)
index = payload.get('index') or {}
fields = {
    'not-covered': json.dumps(index.get('not_covered') or []),
    'hint': index.get('hint') or '',
    'total': str(len(payload.get('test_files') or [])),
    'pytest-cmd': payload.get('pytest_cmd') or '',
    # engine emits query_complete/stale in every mode (query.py _cmd_coverage) — a soft-failed
    # --incremental refresh above can leave these false/true with zero query failure to catch it
    'query-complete': json.dumps(index.get('query_complete')),
    'stale': json.dumps(index.get('stale')),
    # absent-by-design on a compact+complete answer — not a gap, never defaulted to a fake reason
    'completeness-reason': index.get('completeness_reason') or '',
}
for name, value in fields.items():
    with open(base + name + suf, 'w') as handle:
        handle.write(value + '\n')
" "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-" "-${CSID}" || exit 1
```

Parse `$RESULT` JSON:

- `test_files` — list of test file paths
- `pytest_cmd` — ready-to-run command
- `via_call` / `via_mock` — breakdown of how tests were found
- `index.not_covered` — surface as caveat if non-empty
- `index.hint` — include as suggestion
- `index.query_complete` / `index.stale` — completeness signals, always emitted; `query_complete: false` or `stale: true` means the query ran against an incomplete/stale index (e.g. after a soft-failed `--incremental` refresh, Step 0) — surface as caveat on **both** Step 3 branches, independent of `total` or `not_covered`
- `index.completeness_reason` — human-readable reason when present; absent on a compact+complete answer by design, never treated as a gap or defaulted

**Loud-failure contract**: query failure ≠ empty result. Shell enforces it three ways: nonzero exits skill `1` before field reads; unparsable payload exits parser `1`; `query_complete`/`stale` carry through the same parse so a soft-failed refresh with a clean exit can still be caveated. None of the three may become an uncaveated "no affected tests". False empty set from broken index is worst possible output.

**haiku JSON guard**: output may include log/warning prefix/suffix. Always pipe stdin into `python3 -c "import json, sys; ..."`; never assume raw valid JSON. Never add per-field `|| echo "0"` / `|| echo "[]"`: failed parse must not manufacture benign defaults — `query-complete`/`stale` may legitimately read `null` when the engine omits them; treat `null` as "signal absent", never as false/true.

## Step 3 — Output

**Completeness caveat (both branches below)**: read `query-complete` and `stale` tmpfiles first. `query-complete` reads `false` or `stale` reads `true` → prepend `⚠ Index completeness: <completeness-reason, or "index incomplete/stale — see Step 0 refresh output above" when reason is empty>` before the branch's own message. This fires independently of `not_covered` — a soft-failed `--incremental` refresh (Step 0) produces exactly this with a clean query exit.

**When `total == 0`**: reachable only after Step 2 exit `0` + parsed payload. Genuine empty only when the completeness caveat above is also absent — with the caveat present, "no affected tests" means "the index couldn't prove it", not "there are none". Report "No tests found via static analysis. Try full suite or check with `grep -rn <symbol_name> tests/`." — prefixed with the completeness caveat when present.

**When `total > 0`**:

````
## Test impact: <qname>

**Affected tests** (<total> files, <via_call> via call/import graph, <via_mock> via mocks):
<test_files as bullet list>

**Run:**
```
<pytest_cmd>
```

<if not_covered non-empty>
**Caveat:** dynamic-dispatch / hook-callback callers are not in the static graph — <hint>.
</if>
<if completeness caveat present>
**Caveat:** index completeness — <completeness-reason, or generic incomplete/stale message above>.
</if>
````

Output routing: if `total >= 5`, derive free non-colliding path; write report there:

```bash
# timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-'); BRANCH="${BRANCH:-main}"
# never overwrite — same-day re-run on another target would replace unrelated report
TI_OUT=".temp/output-test-impact-${BRANCH}-$(date +%Y-%m-%d).md"; _n=1
while [ -e "$TI_OUT" ]; do _n=$((_n+1)); TI_OUT=".temp/output-test-impact-${BRANCH}-$(date +%Y-%m-%d)-${_n}.md"; done
printf '%s\n' "$TI_OUT"
```

</workflow>
