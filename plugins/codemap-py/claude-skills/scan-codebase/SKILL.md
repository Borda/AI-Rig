---
name: scan-codebase
description: 'Scan the Python codebase and build a structural JSON index (import graph + blast-radius metrics). TRIGGER when: user asks to build, refresh, or rebuild the codemap index; user mentions stale index, missing symbols, or re-indexing after significant project changes; phrases: "build codemap", "scan codebase", "refresh structural index", "rebuild import graph".'
argument-hint: '[--root <path>] [--incremental]'
allowed-tools: Bash
disable-model-invocation: true
effort: low
---

<objective>

**Python only**: `ast.parse` extracts import graph + symbol metadata from all `.py`; non-Python excluded. Writes `.cache/codemap/<project>.json`; no external deps. Zero-Python project still writes empty index; queries return nothing.

Per module: import graph, blast-radius metrics, **symbol list** (classes/functions/methods + line ranges). Symbols let `scan-query symbol` / `find-symbol` return target source, avoiding full-file reads.

Agents/develop skills query via `scan-query` for deps, blast radius, coupling, symbol source before edits.

NOT for: querying existing index (use `/codemap-py:query-code`); integration health checks or wiring consumer integration (use `/codemap-py:integration` — `check`/`plan`/`apply`); non-code input (contracts, prose, config-only review). On out-of-scope input: state scope mismatch and stop — never answer "for reference only" with partial findings.

</objective>

<workflow>

## Step 1: Run the scanner

Build invocation from `$ARGUMENTS`. Pass supplied `--root <path>` and/or `--incremental`; never literal placeholders.

**Unknown-flag check**: before `parse_scan_args.py`, find `$ARGUMENTS` `--` tokens except `--root`, `--incremental`. If any, print `! Unknown flag(s): <tokens>` then `Supported: --root <path>, --incremental`; exit 1. Never AskUserQuestion: disable-model-invocation:true makes it unreachable. Rosters + shell must use exact `Unknown flag(s)` wording, no synonym. Preflight exit `1` is skill-local shortcut accepted in `shared/capability-contract.md`; CLI syntax errors remain §7.5 exit `2`.

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# tr|awk not for-loop — zsh doesn't word-split unquoted vars; for-loop saw whole string as one token, always false-flagged "--root <path>" unsupported
# awk skips token after --root same as old _SKIP_NEXT; parse_scan_args.py handles quoted paths
_ARGS_UNKNOWN=$(printf '%s\n' "$ARGUMENTS" | tr ' ' '\n' \
  | awk '/^--root$/{skip=1;next} skip{skip=0;next} /^--incremental$/{next} /^--/{print}' | tr '\n' ' ')
_ARGS_UNKNOWN="${_ARGS_UNKNOWN% }"
[ -z "$_ARGS_UNKNOWN" ] || { printf "! Unknown flag(s): %s\nSupported: --root <path>, --incremental\n" "$_ARGS_UNKNOWN" >&2; exit 1; }
SETUP_STDERR="${TMPDIR:-/tmp}/codemap-setup-err-$$-${CSID}"
SCAN_STATE_FILE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/setup_scan_env.py" --arguments "$ARGUMENTS" 2>"$SETUP_STDERR")
if [ $? -ne 0 ] || [ -z "$SCAN_STATE_FILE" ]; then
  printf "! setup_scan_env.py failed"; [ -s "$SETUP_STDERR" ] && printf ": %s" "$(cat "$SETUP_STDERR")"; printf "\n"; exit 1
fi
# project-scoped — bare CSID collides across concurrent repos in one session
_CM_PROJ_SLUG=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
printf '%s\n' "$SCAN_STATE_FILE" > "${TMPDIR:-/tmp}/codemap-state-ref-${_CM_PROJ_SLUG}-${CSID}"  # subsequent blocks read without knowing PID
```

```bash
# timeout: 400000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ_SLUG=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
IFS= read -r SCAN_STATE_FILE < "${TMPDIR:-/tmp}/codemap-state-ref-${_CM_PROJ_SLUG}-${CSID}" 2>/dev/null || SCAN_STATE_FILE=""
[ -n "$SCAN_STATE_FILE" ] && [ -f "$SCAN_STATE_FILE" ] || { printf "! codemap state missing — re-run from the beginning\n"; exit 1; }
# -O owned-by-uid, ! -L not-symlink — defense-in-depth on mktemp; last check before sourcing (executed), fails closed on shared-TMPDIR collision
[ -O "$SCAN_STATE_FILE" ] && [ ! -L "$SCAN_STATE_FILE" ] || { printf "! codemap state file failed ownership/symlink check — aborting\n" >&2; exit 1; }
# shellcheck source=/dev/null
. "$SCAN_STATE_FILE"
# NUL-delimited — avoids eval; written by parse_scan_args.py
_ARGS_FILE="${TMPDIR:-/tmp}/codemap-scan-args-nul-$$-${CSID}"
python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_scan_args.py" "$SCAN_ARGS_RAW" --nul-output "$_ARGS_FILE"
SCAN_ARGS=()
while IFS= read -r -d '' _arg; do
  SCAN_ARGS+=("$_arg")
done < "$_ARGS_FILE"
rm -f "$_ARGS_FILE"
# dispatcher, not scan-index alias — alias leases in-engine too (graph.main wraps build+publish in rwgate.write_index) but skips dispatcher's interpreter probe (exit 127 on no eligible CPython) and is a deprecated shim, removed no earlier than 1.0.0. SCAN_BIN stays setup_scan_env.py's existence preflight (dispatcher needs same binary present).
# PATH-literal first token — expansion-bearing form matches no bare-name allow prefix; absolute launcher is interactive fallback
command -v codemap-py >/dev/null 2>&1 || { printf "! codemap-py not on PATH — run \"\${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py\" index as one standalone command instead\n" >&2; exit 1; }
codemap-py index --timeout 360 "${SCAN_ARGS[@]}"
# capture rc BEFORE branching — inside `if ! cmd; then`, $? is the negated compound's status (always 0), never the scanner's
_SCAN_RC=$?
if [ "$_SCAN_RC" -ne 0 ]; then
    printf "! codemap-py index failed (exit %d) — index may be stale or incomplete\n" "$_SCAN_RC"
    # rm sentinel on failure — stale one misleads Step 2
    rm -f "${TMPDIR:-/tmp}/codemap-incremental-noop-${PROJ_SLUG}-${CSID}"
    exit 1
fi
```

**`--root` naming**: index uses `basename(<path>)`, unlike default git-root basename. After a custom-root scan, retain exact path printed by scanner as `<emitted-index-path>`; later query must use `--index <emitted-index-path> --root <same-root>`. `--root` is path resolution only, doesn't select the index. After custom-root scan, verify path via `resolve_index_env.py`. `--root .` handled specially — `setup_scan_env.py` falls back to git-root basename, same as omitting `--root`.

Writes `<root>/.cache/codemap/<project>.json`, or `$CODEMAP_INDEX_DIR/<project>.json` when set; prints:

```text
[codemap] ✓ .cache/codemap/<project>.json
[codemap]   N modules indexed, M degraded
```

## Step 2: Report

After scan, read index and report compact summary:

```bash
# timeout: 15000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# skip if Step 1 failed — index may not exist
_CM_PROJ_SLUG=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
IFS= read -r SCAN_STATE_FILE < "${TMPDIR:-/tmp}/codemap-state-ref-${_CM_PROJ_SLUG}-${CSID}" 2>/dev/null || SCAN_STATE_FILE=""
[ -n "$SCAN_STATE_FILE" ] && [ -f "$SCAN_STATE_FILE" ] || { printf "! codemap state missing — re-run /codemap-py:scan-codebase\n"; exit 1; }
# shellcheck source=/dev/null
. "$SCAN_STATE_FILE"
PROJ_NAME="${PROJ_NAME:-$(basename "$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")")}"
_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"
if [ -f "${_IDX}/${PROJ_NAME}.json" ]; then
    # scan-stats.py reads SCAN_ARGS env (e.g. --root src/mypackage) for project root
    SCAN_ARGS="$SCAN_ARGS_RAW" python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/scan-stats.py"
    # --incremental noop check — sentinel set by Step 1 on fallback to full scan
    if [ -f "${TMPDIR:-/tmp}/codemap-incremental-noop-${PROJ_SLUG}-${CSID}" ]; then
        echo "[codemap] Note: --incremental had no prior index — full scan ran instead"
        rm -f "${TMPDIR:-/tmp}/codemap-incremental-noop-${PROJ_SLUG}-${CSID}"
    fi
else
    echo "[codemap] Skipping stats — no index found (Step 1 may have failed)"
fi
```

Degraded count is informational. `scan-stats.py` reports module counts, no per-file list; index remains useful.

If `--incremental` has no prior index, Step 1 sets `codemap-incremental-noop-${PROJ_SLUG}-${CSID}` before scan. Step 2 detects/removes after stats; logs `--incremental had no prior index — full scan ran instead`. On scan failure, Step 1 removes sentinel to prevent false next-run state.

**Sentinel hostname limit**: `PROJ_SLUG` includes hostname short-name. Dynamic container/cloud hostnames change sentinel key; incremental-noop detection may miss stale sentinel. Only Step 2 message affected, not scan correctness.

Zero-Python project: Step 3 suggestions return nothing; index valid but empty.

## Step 3: Suggest next step

```text
Index ready. Query it with:
  /codemap-py:query-code central --top 10
  /codemap-py:query-code deps <module>
  /codemap-py:query-code rdeps <module>
  /codemap-py:query-code coupled --top 10
  # see /codemap-py:query-code for full list of subcommands
```

</workflow>
