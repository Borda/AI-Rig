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

Per module: import graph, blast-radius metrics, **symbol list** (classes/functions/methods + line ranges). Symbols let `codemap-py query symbol` / `find-symbol` return target source, avoiding full-file reads.

Agents/develop skills query via `codemap-py query` for deps, blast radius, coupling, symbol source before edits.

NOT for: querying existing index (use `/codemap-py:query-code`); integration health checks or wiring consumer integration (use `/codemap-py:integration` — `audit`/`plan`/`apply`); non-code input (contracts, prose, config-only review). On out-of-scope input: state scope mismatch and stop — never answer "for reference only" with partial findings.

</objective>

<workflow>

## Step 1: Run the scanner

Build invocation from `$ARGUMENTS`. Pass supplied `--root <path>` and/or `--incremental`; never literal placeholders.

**Unknown-flag check**: `setup_scan_env.py` validates `$ARGUMENTS` with the same tokenizer that extracts `--root` (quoted paths stay one token). Any `--` token except `--root <path>` and `--incremental` → it prints `! Unknown flag(s): <tokens>` then `Supported: --root <path>, --incremental` and exits 5; the block prints that stderr unchanged and exits 1. Never AskUserQuestion: disable-model-invocation:true makes it unreachable. Rosters + shell must use exact `Unknown flag(s)` wording, no synonym. Preflight exit `1` is skill-local shortcut accepted in `shared/capability-contract.md`; CLI syntax errors remain exit `2` per its §Shared exit-code contract. `--root` with no value (would silently fall back to default-root scan): `! --root requires a value` + same `Supported:` line; exit 1 — distinct message, never the `Unknown flag(s)` wording.

Step 1 exits non-zero → report its error and stop; never run Step 2 or Step 3.

```bash
# timeout: 400000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ_SLUG=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
# success marker for Step 2 — cleared first so a failed run never inherits an earlier success
rm -f "${TMPDIR:-/tmp}/codemap-scan-ok-${_CM_PROJ_SLUG}-${CSID}"
SETUP_STDERR="${TMPDIR:-/tmp}/codemap-setup-err-$$-${CSID}"
SCAN_STATE_FILE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/setup_scan_env.py" --arguments "$ARGUMENTS" 2>"$SETUP_STDERR")
_SETUP_RC=$?
# 5 = $ARGUMENTS rejected — stderr already holds the exact pinned wording; a prefix would bury it
[ "$_SETUP_RC" -eq 5 ] && { cat "$SETUP_STDERR" >&2; exit 1; }
if [ "$_SETUP_RC" -ne 0 ] || [ -z "$SCAN_STATE_FILE" ]; then
  printf "! setup_scan_env.py failed"; [ -s "$SETUP_STDERR" ] && printf ": %s" "$(cat "$SETUP_STDERR")"; printf "\n"; exit 1
fi
# project-scoped — bare CSID collides across concurrent repos in one session
printf '%s\n' "$SCAN_STATE_FILE" > "${TMPDIR:-/tmp}/codemap-state-ref-${_CM_PROJ_SLUG}-${CSID}"  # Step 2 reads without knowing PID
# -O owned-by-uid, ! -L not-symlink — defense-in-depth on mktemp; last check before sourcing (executed), fails closed on shared-TMPDIR collision
[ -O "$SCAN_STATE_FILE" ] && [ ! -L "$SCAN_STATE_FILE" ] || { printf "! codemap state file failed ownership/symlink check — aborting\n" >&2; exit 1; }
# shellcheck source=/dev/null
. "$SCAN_STATE_FILE"
# NUL-delimited — avoids eval; written by parse_scan_args.py
_ARGS_FILE="${TMPDIR:-/tmp}/codemap-scan-args-nul-$$-${CSID}"
# unchecked failure left SCAN_ARGS empty → silent default git-root full scan reported as success
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_scan_args.py" "$SCAN_ARGS_RAW" --nul-output "$_ARGS_FILE" && [ -f "$_ARGS_FILE" ] || { printf "! parse_scan_args.py failed — scan not started\n" >&2; exit 1; }
SCAN_ARGS=()
while IFS= read -r -d '' _arg; do
  SCAN_ARGS+=("$_arg")
done < "$_ARGS_FILE"
rm -f "$_ARGS_FILE"
# dispatcher, not deprecated scan-index alias (removed ≥1.0.0): alias leases in-engine too (graph.main wraps build+publish in rwgate.write_index) but skips interpreter probe (exit 127, no eligible CPython)
# SCAN_BIN stays setup_scan_env.py's existence preflight — dispatcher needs same binary
# PATH-literal first token — expansion-bearing form matches no bare-name allow prefix; absolute launcher = interactive fallback
command -v codemap-py >/dev/null 2>&1 || { printf "! codemap-py not on PATH — run \"\${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py\" index as one standalone command instead\n" >&2; exit 1; }
codemap-py index --timeout 360 "${SCAN_ARGS[@]}"
# capture rc BEFORE branching — in `if ! cmd; then`, $? = negated compound status (always 0), never scanner's
_SCAN_RC=$?
if [ "$_SCAN_RC" -ne 0 ]; then
    printf "! codemap-py index failed (exit %d) — index may be stale or incomplete\n" "$_SCAN_RC"
    # rm sentinel on failure — stale one misleads Step 2
    rm -f "${TMPDIR:-/tmp}/codemap-incremental-noop-${PROJ_SLUG}-${CSID}"
    exit 1
fi
printf 'ok\n' > "${TMPDIR:-/tmp}/codemap-scan-ok-${_CM_PROJ_SLUG}-${CSID}"
```

**`--root` naming**: index uses `basename(<path>)`, unlike default git-root basename. After a custom-root scan, retain exact path printed by scanner as `<emitted-index-path>`. A later query may pass `--root <same-root>` to select that root's canonical index; pass `--index <emitted-index-path> --root <same-root>` to pin the emitted file. `--root` also sets query path resolution and the accepted custom-index boundary. After custom-root scan, verify path via `resolve_index_env.py`. `--root .` handled specially — `setup_scan_env.py` falls back to git-root basename, same as omitting `--root`.

Writes `<root>/.cache/codemap/<project>.json`, or `$CODEMAP_INDEX_DIR/<project>.json` when set; prints:

```text
[codemap] ✓ .cache/codemap/<project>.json
[codemap]   N modules indexed, M degraded
```

## Step 2: Report

After scan, read index and report compact summary. Step 2 exits non-zero → report its error and stop; never run Step 3 or print `Index ready`.

```bash
# timeout: 15000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
_CM_PROJ_SLUG=$(basename "$_GIT_ROOT")
# Step 1 failed → no success marker → stop; index may not exist
IFS= read -r _SCAN_OK < "${TMPDIR:-/tmp}/codemap-scan-ok-${_CM_PROJ_SLUG}-${CSID}" 2>/dev/null || _SCAN_OK=""
[ "$_SCAN_OK" = "ok" ] || { printf "! Step 1 scan did not succeed — not reporting stats\n"; exit 1; }
IFS= read -r SCAN_STATE_FILE < "${TMPDIR:-/tmp}/codemap-state-ref-${_CM_PROJ_SLUG}-${CSID}" 2>/dev/null || SCAN_STATE_FILE=""
[ -n "$SCAN_STATE_FILE" ] && [ -f "$SCAN_STATE_FILE" ] || { printf "! codemap state missing — re-run /codemap-py:scan-codebase\n"; exit 1; }
# shellcheck source=/dev/null
. "$SCAN_STATE_FILE"
PROJ_NAME="${PROJ_NAME:-$(basename "$_GIT_ROOT")}"
# scanner writes <scan-root>/.cache/codemap (graph._resolve_out_path); no --root (or ".") = git root
# sentinel, not $(…) capture — `>` truncates first, so a failed call never leaves an earlier root
_ROOT_FILE="${TMPDIR:-/tmp}/codemap-scan-root-${_CM_PROJ_SLUG}-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_scan_args.py" "$SCAN_ARGS_RAW" --print-root > "$_ROOT_FILE" 2>/dev/null && IFS= read -r _SCAN_ROOT < "$_ROOT_FILE" || _SCAN_ROOT="."
[ "$_SCAN_ROOT" = "." ] && _SCAN_ROOT="$_GIT_ROOT"
_IDX="${CODEMAP_INDEX_DIR:-${_SCAN_ROOT}/.cache/codemap}"
if [ -f "${_IDX}/${PROJ_NAME}.json" ]; then
    # scan-stats.py reads SCAN_ARGS env (e.g. --root src/mypackage) for project root
    SCAN_ARGS="$SCAN_ARGS_RAW" python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/scan-stats.py"
    # rc before noop block — exit 2 (CODEMAP_INDEX_DIR outside allowed roots) previously reported as success
    _STATS_RC=$?
    # --incremental noop check — sentinel set by Step 1 on fallback to full scan
    if [ -f "${TMPDIR:-/tmp}/codemap-incremental-noop-${PROJ_SLUG}-${CSID}" ]; then
        echo "[codemap] Note: --incremental had no prior index — full scan ran instead"
        rm -f "${TMPDIR:-/tmp}/codemap-incremental-noop-${PROJ_SLUG}-${CSID}"
    fi
    [ "$_STATS_RC" -eq 0 ] || { printf "! scan-stats.py failed (exit %d) — index written by Step 1, stats not reported\n" "$_STATS_RC"; exit 1; }
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
