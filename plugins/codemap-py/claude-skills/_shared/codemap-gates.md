<!-- file: codemap-gates.md — consumers: plugin-local codemap-gates.md wrappers read manifested codemap-py--codemap-gates.md copies -->

# Codemap gates contract — v2

Plugin-agnostic Gate A / Gate B machinery for missing-index and stale-index decisions. Consumer wrappers read byte-identical manifested copies, synced from this canonical source, supplying only their **skip flag** (per-plugin flag disabling gates, e.g. `CODEMAP_RAW=auto` for develop, `CODEMAP_FORCE_OFF=false` for oss).

Read currency first: `IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="no_index"` (consumers may point at own currency file; `CSID` exported by caller per `claude-config.md` TMPDIR Sentinel Scoping).

## Gate A — missing index

Fires when `CODEMAP_ENABLED=false`, consumer's skip flag **not** set to off. Invoke `AskUserQuestion`:

- Question: "No codemap index for this project — structural dependency context unavailable. How to proceed?"
- (a) Continue without codemap — proceed with file-read context only
- (b) Build index now — run `codemap-py index` in the foreground (wait until it finishes), then set `CODEMAP_ENABLED=true` and continue
- (c) Abort — stop; build index manually then re-invoke this skill

On (b): run `codemap-py index` in the foreground (wait until it finishes); set `CODEMAP_ENABLED=true`; continue. (Never model-invoke the `codemap-py:scan-codebase` skill — it is `disable-model-invocation:true`, user-slash-only; the model builds through the gated `codemap-py` dispatcher, never the `scan-index` alias — a compatibility shim removed no earlier than `1.0.0`.) On (c): stop.

## Gate B — stale index

Fires when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`. Invoke `AskUserQuestion`:

- Question: "Codemap index is stale — source files changed since last scan; context may miss recent changes. How to proceed?"
- (a) Rebuild now — run `codemap-py index` in the foreground (wait until it finishes), then continue with fresh index (note: ambient hook may already hold index's exclusive writer lease via background refresh; rebuild waits out gate timeout — 30 s default, `CODEMAP_GATE_TIMEOUT` overrides — then exits `index_busy`; re-run once that refresh lands)
- (b) Continue with stale data — proceed; results may miss recent changes
- (c) Skip codemap — set `CODEMAP_ENABLED=false`; proceed without structural context

On (a): run `codemap-py index` in the foreground (wait until it finishes); continue. On (c): set `CODEMAP_ENABLED=false`.
