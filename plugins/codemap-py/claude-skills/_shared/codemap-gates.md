<!-- file: codemap-gates.md — consumers: plugin-local codemap-gates.md wrappers (develop, oss, research) read this file from the active codemap-py install via `resolve_shared_path.py codemap-py claude-skills/_shared` -->

# Codemap gates contract — v4

Plugin-agnostic Gate A / Gate B machinery for missing-index and stale-index decisions. Consumer wrappers read this file live from the active `codemap-py` install (no local copies — the text always matches the CLI actually installed), supplying only their **skip flag** (per-plugin flag that turns the gates off). Gate A fires only while the flag holds its active value — `CODEMAP_RAW=auto` for develop and research, `CODEMAP_FORCE_OFF=false` for oss; `CODEMAP_RAW=off` / `CODEMAP_FORCE_OFF=true` skips both gates.

Read currency first: `IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/<consumer>-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="no_index"` — `<consumer>` = the calling plugin's own sentinel prefix (e.g. `dev` for develop, `research` for research; oss per-skill prefix), as its wrapper states; `CSID` exported by caller per `claude-config.md` TMPDIR Sentinel Scoping.

If inherited `SCAN_NO_AUTOBUILD=1`, do not build, refresh, or ask to override the freeze in either gate. Keep the observed missing/stale state, continue with the consumer's file-read fallback where allowed, and apply its strict-mode failure behavior when structural context is required.

## Gate A — missing index

Fires when the index is missing, the consumer has enabled Codemap by default or explicitly, and its skip flag is not set to off. By default, run `codemap-py index` in the foreground and wait for completion before continuing. Set `CODEMAP_ENABLED=true` only after successful preparation. On failure, report the provider's actual error, keep Codemap unavailable for this run, and continue through the consumer's documented file-read fallback where allowed; never describe a failed build as ready.

When `LAZY_CODEMAP` is set to any non-empty value, preserve confirm-first behavior and ask:

- Question: "No codemap index for this project — structural dependency context unavailable. How to proceed?"
- (a) Continue without codemap — proceed with file-read context only
- (b) Build index now — run `codemap-py index` in the foreground, then continue after it succeeds
- (c) Abort — stop; the user can build the index and re-invoke this skill

Only prepare on the user's affirmative choice; only set `CODEMAP_ENABLED=true` after success. After success, rerun the consumer's normal gate-state helper so its enabled and currency sentinels reflect the prepared index for later reads. A failed preparation reports its actual error and leaves Codemap unavailable. A Gate A continue-without choice leaves the consumer's already-persisted disabled state unchanged. Never model-invoke the `codemap-py:scan-codebase` skill: the scan skill remains explicit user invocation only.

## Gate B — stale index

Fires when `CODEMAP_ENABLED=true` and `CODEMAP_CURRENCY=stale`.

**`LAZY_CODEMAP` unset (default)**: rebuild without asking. Run `codemap-py index` in the foreground and wait for completion. If it exits `index_busy` (an ambient hook already holds the index's exclusive writer lease via background refresh; this waits out the gate timeout — 30 s default, `CODEMAP_GATE_TIMEOUT` overrides — before exiting), print `! codemap index busy — continuing with stale data` and continue with stale results and their staleness caveat. For any other failure, report the actual error and use the consumer's documented fallback where allowed; do not claim fresh data. No `AskUserQuestion` in either case.

**`LAZY_CODEMAP` set** (any non-empty value): ask before rebuilding — for a repo where refresh cost is high, or to keep the confirm-first behavior — invoke `AskUserQuestion`:

- Question: "Codemap index is stale — source files changed since last scan; context may miss recent changes. How to proceed?"
- (a) Rebuild now — run `codemap-py index` in the foreground and wait for completion (`index_busy` → continue with stale, same as the default path above)
- (b) Continue with stale data — proceed; results may miss recent changes
- (c) Skip codemap — set `CODEMAP_ENABLED=false`; proceed without structural context

On (a): run `codemap-py index` in the foreground and wait for completion; after success rerun the consumer's normal gate-state helper so later sentinel reads see the enabled, current index. Report real errors and do not claim fresh data if preparation fails; a failed rebuild keeps the stale currency sentinel. On (b): retain the stale currency sentinel and enabled state. Every later query in this skill must pass `--no-heal` while that sentinel remains `stale`, including queries issued from a later tool call; inherited `SCAN_NO_AUTOBUILD=1` also requires the freeze to be carried to later calls. On (c): use the consumer wrapper's own skip-state helper to persist `CODEMAP_ENABLED=false` for later sentinel reads; do not alter raw user flags. A skip decision is scoped to this skill invocation and does not change global configuration.
