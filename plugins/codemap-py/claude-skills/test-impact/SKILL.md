---
name: test-impact
description: 'Identify which tests need rerunning after a code change — traces static call graph (function-level) or import graph (module-level) to find affected test files, then emits a ready-to-run pytest command. TRIGGER when: user asks which tests are affected by a change; phrases: "which tests are affected", "what tests cover this", "test impact of", "what tests to rerun".'
argument-hint: <module::symbol | module> [--no-mocks]
allowed-tools: Bash, Write, AskUserQuestion
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
  - `qname` — `module::symbol` (function-level; a dotted `module.symbol` or a bare name defined once also resolves, while a module-name suffix or bare method name exits with candidates) or bare dotted module (module-level; an indexed module name always wins)
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
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" --output-prefix "codemap-${_CM_PROJ}" 2>/dev/null
IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""
[ -n "$INDEX" ] || { echo "! resolve_index_env.py failed — check Python availability and CLAUDE_PLUGIN_ROOT"; exit 1; }

[ -f "$INDEX" ] || echo "No index found — will build via codemap-py index"
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
codemap-py index --timeout 360 \
    && echo "[codemap] index built in $(( $(date +%s) - _CM_BUILD_T0 ))s" \
    || { printf "! codemap-py index failed — cannot proceed without an index\n"; exit 1; }
```

If index already exists:

```bash
# timeout: 400000
if [ "${SCAN_NO_AUTOBUILD:-0}" = "1" ]; then
    echo "[codemap] SCAN_NO_AUTOBUILD=1 — using existing index as-is (no refresh)"
else
    _CM_BUILD_T0=$(date +%s)
    # forward only when user already overrode CODEMAP_INDEX_DIR — an unconditional export turns the
    # engine's own git-root default into a flat-convention override (index_paths.py), materializing
    # a second index when this ran from a subdirectory
    [ -n "${CODEMAP_INDEX_DIR:-}" ] && export CODEMAP_INDEX_DIR
    codemap-py index --incremental --timeout 360 \
        && echo "[codemap] index built in $(( $(date +%s) - _CM_BUILD_T0 ))s" \
        || printf "⚠ codemap-py index --incremental failed — index may be stale; continuing\n"
fi
```

After build/refresh, re-verify index; the block's last command is Step 1's argument parse:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" --output-prefix "codemap-${_CM_PROJ}" 2>/dev/null
IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""
[ -n "$INDEX" ] && [ -f "$INDEX" ] || { printf "! Index not found after refresh at %s — check CODEMAP_INDEX_DIR or re-run /codemap-py:scan-codebase\n" "${INDEX:-<unresolved>}"; exit 1; }
# shlex tokens + exact-token flag match: awk/grep split sent a leading --no-mocks as the target
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_test_impact_args.py" --output-prefix "codemap-${_CM_PROJ}" --arguments="$ARGUMENTS"
```

## Step 1 — Parse arguments

Extract `QNAME` + `NO_MOCKS` from `$ARGUMENTS` — the parse ran as the last command of the Step 0 re-verify block above. Exit `1` → index not found (its `!` line printed); stop. Exit `0` → target recorded (`ti-qname`/`ti-mocks` sentinels). Exit `2` → relay its `!` line (unknown flag, unbalanced quote, flag-shaped target); stop. Exit `3` → no target given:

- Use `AskUserQuestion`: "Which function or module changed? Type `module::symbol` (function-level) or a bare dotted module (module-level) in the free-text field." Options: up to 3 concrete targets grounded in changed `.py` files (`git diff --name-only HEAD`, as bare dotted modules), plus `Cancel — exit without analysis`. Never offer a format name as an option — choosing one yields no target.
- No grounded target → skip the question; print `Usage: /codemap-py:test-impact <module::symbol | module> [--no-mocks]`; stop.

After an answer, record it before Step 2:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
QNAME="<answer from AskUserQuestion>"
case "$QNAME" in *'<'*'>'*|"") echo "! BLOCKED — target placeholder not substituted; ask again"; exit 1;; esac
# answer + original flags: re-parsing the answer alone rewrites ti-mocks empty, dropping --no-mocks
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_test_impact_args.py" --output-prefix "codemap-${_CM_PROJ}" --arguments="$QNAME $ARGUMENTS"
```

**Multi-symbol guard**: `$ARGUMENTS` may contain multiple targets (e.g. `mypackage.auth::validate mypackage.auth::parse`). Helper keeps first non-flag token; when more remain it prints `⚠ test-impact accepts one symbol at a time — using first token only: $QNAME. Run separately for each remaining symbol.` — relay verbatim.

## Step 2 — Run test-impact query

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || basename "$PWD")
IFS= read -r QNAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-qname-${CSID}" 2>/dev/null || QNAME=""
IFS= read -r MOCKS_FLAG < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-mocks-${CSID}" 2>/dev/null || MOCKS_FLAG=""
[ -n "$QNAME" ] || { echo "! no test-impact target recorded — finish Step 1 first"; exit 1; }
_TI_ERR="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-ti-stderr-${CSID}"
# capture stderr to file, never 2>/dev/null — swallowed diagnostic rendered a broken index as "no affected tests" before
RESULT=$(codemap-py query test-impact "$QNAME" $MOCKS_FLAG 2>"$_TI_ERR")
_TI_RC=$?
[ "$_TI_RC" -eq 0 ] || printf "! test-impact query failed (exit %d) — this is NOT a 'no affected tests' result; do not report an empty test set\n" "$_TI_RC" >&2
# one parse, no per-field `|| echo` defaults (forged total=0); error JSON is on stdout, so routing reads it there
printf '%s' "$RESULT" | python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/summarize_test_impact.py" --rc "$_TI_RC" --stderr-file "$_TI_ERR" --output-prefix "codemap-${_CM_PROJ}" || exit 1
```

On success the helper prints a summary — Step 3 renders from it only:

- `test_files` — list of test file paths
- `pytest_cmd` — ready-to-run command for POSIX shells on POSIX hosts or PowerShell on native Windows. `test_files` is the authoritative argv list; for another shell, rebuild with its quoting rather than reuse incompatible escaping.
- `via_call` / `via_mock` — breakdown of how tests were found
- `index.not_covered` — surface as caveat if non-empty
- `index.hint` — include as suggestion
- `index.query_complete` / `index.stale` — completeness signals, always emitted; `query_complete: false` or `stale: true` means the query ran against an incomplete/stale index (e.g. after a soft-failed `--incremental` refresh, Step 0) — surface as caveat on **both** Step 3 branches, independent of `total` or `not_covered`
- `index.completeness_reason` — human-readable reason when present; absent on a compact+complete answer by design, never treated as a gap or defaulted

**Loud-failure contract**: query failure ≠ empty result. Shell enforces it three ways: nonzero exits skill `1` before field reads; unparsable payload exits helper `1`; `query_complete`/`stale` carry through the same parse so a soft-failed refresh with a clean exit can still be caveated. None of the three may become an uncaveated "no affected tests". False empty set from broken index is worst possible output.

**Failure routing** (nonzero exit; helper reads the engine's stdout error JSON, then stderr head):

- `rejected_target` present (typo, ambiguous name, module not indexed) → prints `error` plus each `candidates`/`suggestions` entry; no rebuild advice. Relay the candidates; re-run with one exact candidate.
- Exit `2` without it → rejected argument; no rebuild advice.
- Anything else (missing/oversized/corrupt index, self-check failure) → rebuild advice.

**haiku JSON guard**: output may include log/warning prefix/suffix; helper tolerates it — never assume raw valid JSON. Never add per-field `|| echo "0"` / `|| echo "[]"`: failed parse must not manufacture benign defaults — `query_complete`/`stale` may legitimately read `null` when the engine omits them; treat `null` as "signal absent", never as false/true.

## Step 3 — Output

Render every value from Step 2's printed summary — never estimate counts or re-run the query.

**Completeness caveat (both branches below)**: read `query_complete` and `stale` from that summary first. `query_complete` reads `false` or `stale` reads `true` → prepend `⚠ Index completeness: <completeness_reason, or "index incomplete/stale — see Step 0 refresh output above" when reason is empty>` before the branch's own message. This fires independently of `not_covered` — a soft-failed `--incremental` refresh (Step 0) produces exactly this with a clean query exit.

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
**Caveat:** index completeness — <completeness_reason, or generic incomplete/stale message above>.
</if>
````

Output routing: if `total >= 5`, print free non-colliding path (`<TI_OUT>` below); write report there:

```bash
# timeout: 3000
# never overwrite — same-day re-run on another target would replace unrelated report; helper appends -2, -3, … until free
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/anti_overwrite_path.py" .temp output-test-impact --branch-date
```

Write the report with Write: a YAML `---` block first (`Title: Test impact — <qname>`, `Date`, `Scope: <qname>`, `Focus: static test impact`, `Agents: none`, `Outcome: <total> affected test files`, `Confidence: complete` or `incomplete/stale` per the caveat, `Next steps: run the printed pytest command`, `Path: <TI_OUT>`), then the template above.

</workflow>
