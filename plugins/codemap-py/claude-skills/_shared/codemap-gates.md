<!-- file: codemap-gates.md — consumers: plugin-local codemap-gates.md wrappers (develop, oss, research) read this file from the active codemap-py install via `resolve_shared_path.py codemap-py claude-skills/_shared` -->

# Codemap gates contract — v3

Plugin-agnostic Gate A / Gate B machinery for missing-index and stale-index decisions. Consumer wrappers read this file live from the active `codemap-py` install (no local copies — the text always matches the CLI actually installed), supplying only their **skip flag** (per-plugin flag disabling gates, e.g. `CODEMAP_RAW=auto` for develop, `CODEMAP_FORCE_OFF=false` for oss).

Read currency first: `IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="no_index"` (consumers may point at own currency file; `CSID` exported by caller per `claude-config.md` TMPDIR Sentinel Scoping).

## Gate A — missing index

Fires when `CODEMAP_ENABLED=false`, consumer's skip flag **not** set to off. Invoke `AskUserQuestion`:

- Question: "No codemap index for this project — structural dependency context unavailable. How to proceed?"
- (a) Continue without codemap — proceed with file-read context only
- (b) Build index now — run `codemap-py index` in the foreground (wait until it finishes), then set `CODEMAP_ENABLED=true` and continue
- (c) Abort — stop; build index manually then re-invoke this skill

On (b): run `codemap-py index` in the foreground (wait until it finishes); set `CODEMAP_ENABLED=true`; continue. (Never model-invoke the `codemap-py:scan-codebase` skill — it is `disable-model-invocation:true`, user-slash-only; the model builds through the gated `codemap-py` dispatcher, never the `scan-index` alias — a compatibility shim removed no earlier than `1.0.0`.) On (c): stop.

## Gate B — stale index

Fires when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

**`LAZY_CODEMAP` unset (default)**: rebuild without asking — rebuild cost varies by repo size, and this default trades that variable pause for staying current automatically. Run `codemap-py index` in the foreground (wait until it finishes). If it exits `index_busy` (an ambient hook already holds the index's exclusive writer lease via background refresh; this waits out the gate timeout — 30 s default, `CODEMAP_GATE_TIMEOUT` overrides — before exiting), print `! codemap index busy — continuing with stale data` and continue with the stale index rather than blocking or looping; a query still surfaces its own staleness via `query_complete`/`completeness_reason`. Otherwise continue with the fresh index. No `AskUserQuestion` in either case.

**`LAZY_CODEMAP` set** (any non-empty value): ask before rebuilding — for a repo where rebuild cost is high, or to keep the confirm-first behavior — invoke `AskUserQuestion`:

- Question: "Codemap index is stale — source files changed since last scan; context may miss recent changes. How to proceed?"
- (a) Rebuild now — run `codemap-py index` in the foreground (wait until it finishes), then continue with fresh index (`index_busy` → continue with stale, same as the default path above)
- (b) Continue with stale data — proceed; results may miss recent changes
- (c) Skip codemap — set `CODEMAP_ENABLED=false`; proceed without structural context

On (a): run `codemap-py index` in the foreground (wait until it finishes; `index_busy` → continue with stale). On (c): set `CODEMAP_ENABLED=false`.
