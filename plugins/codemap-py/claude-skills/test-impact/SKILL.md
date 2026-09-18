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
_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"
INDEX="${_IDX}/${_CM_PROJ}.json"

# dispatcher, not scan-query/scan-index aliases — aliases lease in-engine too but skip dispatcher's interpreter probe (exit 127), deprecated shims removed no earlier than 1.0.0
# PATH-literal invocation everywhere below — expansion-bearing form matches no bare-name allow prefix; plugin's absolute bin/codemap-py stays interactive fallback
command -v codemap-py >/dev/null 2>&1 || { echo "codemap-py not on PATH — install the codemap-py plugin, or invoke its bin/codemap-py launcher as one standalone command"; exit 1; }

[ ! -f "$INDEX" ] && echo "No index found — will build via codemap-py index"
```

`SCAN_NO_AUTOBUILD=1` opts out: use index unchanged; no refresh/full build. Echo build wall-time when run, separating build/query cost.

If `$INDEX` missing:

- With `SCAN_NO_AUTOBUILD=1`: print `! codemap index missing and SCAN_NO_AUTOBUILD=1 — refusing to auto-build. Build it manually first: /codemap-py:scan-codebase`; exit 1.
- Otherwise run foreground `codemap-py index`; wait, then continue. Never invoke `codemap-py:scan-codebase`: `disable-model-invocation:true`, user-slash-only. Build through gated `codemap-py index` dispatcher.

If index already exists:

```bash
# timeout: 30000
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"
if [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then
    echo "[codemap] SCAN_NO_AUTOBUILD=1 — using existing index as-is (no refresh)"
else
    _CM_BUILD_T0=$(date +%s)
    # export not inline env prefix — prefix puts expansion ahead of binary, command no longer matches bare-name allow prefix
    export CODEMAP_INDEX_DIR="${_IDX}"   # forward to the build; ensures it writes to same path as INDEX
    codemap-py index --incremental \
        && echo "[codemap] index built in $(( $(date +%s) - _CM_BUILD_T0 ))s" \
        || printf "⚠ codemap-py index --incremental failed — index may be stale; continuing\n"
fi
```

After build/refresh, re-verify index:

```bash
# timeout: 5000
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"
INDEX="${_IDX}/${_CM_PROJ}.json"
[ -f "$INDEX" ] || { printf "! Index not found after refresh at %s — check CODEMAP_INDEX_DIR or re-run /codemap-py:scan-codebase\n" "$INDEX"; exit 1; }
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

**Loud-failure contract**: query failure ≠ empty result. Shell enforces it: nonzero exits skill `1` before field reads; unparsable payload exits parser `1`. Neither may become "no affected tests". False empty set from broken index is worst possible output.

**haiku JSON guard**: output may include log/warning prefix/suffix. Always pipe stdin into `python3 -c "import json, sys; ..."`; never assume raw valid JSON. Never add per-field `|| echo "0"` / `|| echo "[]"`: failed parse must not manufacture benign defaults.

## Step 3 — Output

**When `total == 0`**: reachable only after Step 2 exit `0` + parsed payload: genuine empty. Report "No tests found via static analysis. Try full suite or check with `grep -rn <symbol_name> tests/`."

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
