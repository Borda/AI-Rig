<!-- file: team-mode.md — consumers: plugins/cc_develop/skills/refactor/SKILL.md (Team mode branch, gated on TEAM_MODE=true) -->

# Refactor — Team Mode Protocol

> **Agent budget** — each teammate costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each teammate near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt requires an envelope even on exhaustion — `partial: true` plus what was finished.

Loaded only when `TEAM_MODE=true`. Steps 1–2 complete solo (teammates need scope + coverage context). Spawn both teammates now; skip Steps 3–5, proceed to Final Report after results.

Compute run directory and create health sentinel:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_setup_worktree_wrap.py" refactor
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-refactor-team-ts-${CSID}" 2>/dev/null || TS=""
IFS= read -r RUN_DIR_LITERAL < "${TMPDIR:-/tmp}/dev-refactor-run-dir-${CSID}" 2>/dev/null || RUN_DIR_LITERAL=""
# no EXIT trap here — fires when THIS Bash call's shell exits (immediately), deleting the sentinel setup_worktree.py just created; pre-spawn touch below re-arms it, completion block below cleans up
```

**IMPORTANT**: in spawn prompts below, substitute `$RUN_DIR_LITERAL` with actual resolved path before constructing each Agent call — agents receive literal resolved strings, not shell variable references. Same for `$TS`.

**Note on `model=` assignments**: `model=opus`/`model=sonnet` in spawn prompts below are advisory hints — effective only when actual foundry agents installed. Falling back to `general-purpose` (foundry absent): prompt-prepend `model=` doesn't reliably override agent-resolution fallback tier; effective model set by `agent-resolution.md`'s fallback table, not spawn prompt. Intentional — sonnet sufficient for qa-specialist characterization tests, opus for sw-engineer refactor implementation; on fallback, expect tier degradation noted in Final Report.

Serialize teammates — qa-specialist writes and gates characterization tests against **pre-refactor** source first, then sw-engineer applies refactor. Spawning sw-engineer first inverts safety net: characterization tests would be written against already-mutated code, so any behaviour change refactor introduces becomes undetectable (tests pin new behaviour instead of original). Mirrors solo Step 3 gate (`GATE OK: all characterization tests pass on unmodified code`).

**Step T1 — Spawn foundry:qa-specialist (model=sonnet) against the pre-refactor source and wait for completion**. Prompt: "You are a foundry:qa-specialist teammate refactoring: [target]. Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Your task: write characterization tests (Step 3) to build a safety net BEFORE any refactor — test the CURRENT (unmodified, pre-refactor) source and assert its existing behaviour. Scope constraint: only create/edit files under `tests/`. Do NOT edit source files. Broadcast context: {target: <path>, coverage: <summary>, goal: <stated goal>}. Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output. Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal completion in final delta message: 'Status: complete | blocked — <reason>'. Write your full analysis to $RUN_DIR_LITERAL/refactor-qa-specialist.md using the Write tool. Return ONLY compact JSON: {"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line>"}."

**Gate T1 — characterization tests must pass on unmodified code before spawning sw-engineer**. Run qa-specialist's tests (`$PYTEST_CMD <char_test_file> -v`; check exit via persisted-exit pattern in Step 3). Exit 0 → safety net green; proceed to T2. Exit ≠ 0 (including 5 — no tests collected) → no valid safety net; do NOT refactor. Invoke `AskUserQuestion` — (a) re-spawn qa-specialist with corrected assertions/path (recommended) · (b) proceed without safety net (record acceptance in `checkpoint.md`) · (c) abort.

**Step T2 — Only after Gate T1 is green, spawn foundry:sw-engineer (model=opus) to apply the refactor**. Prompt: "You are a foundry:sw-engineer teammate refactoring: [target]. Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Your task: apply the refactoring steps (Steps 4–5: change with safety net, review). Scope constraint: only edit source files (not under `tests/`) — do NOT modify the characterization tests in `$RUN_DIR_LITERAL/refactor-qa-specialist.md`'s test file. Broadcast context: {target: <path>, coverage: <summary>, goal: <stated goal>, safety_net: $RUN_DIR_LITERAL/refactor-qa-specialist.md}. Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output. Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal completion in final delta message: 'Status: complete | blocked — <reason>'. Write your full analysis to $RUN_DIR_LITERAL/refactor-sw-engineer.md using the Write tool. Return ONLY compact JSON: {"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line>"}."

**Gate T2 — re-run the same characterization tests against the post-refactor source**. Green→green proves refactor preserved behaviour. Any test now failing means refactor changed observable behaviour — surface with ⚠ and do NOT accept refactor until reconciled (fix source, or confirm behaviour change intended and update test deliberately).

Health monitoring (CLAUDE.md §6): re-derive `$TS` and `$RUN_DIR` at block start (bash resets between calls — read back from temp files the spawn block persisted):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-refactor-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-refactor-run-dir-${CSID}" 2>/dev/null || RUN_DIR=".temp/develop/$TS"
```

Apply to each teammate independently — create sentinel `touch ${TMPDIR:-/tmp}/refactor-team-check-$TS` before each spawn (tmpdir-exempt: sentinel written by setup_worktree.py's `_sentinel_dir()`, which resolves ${TMPDIR:-/tmp} semantics — $TS run-timestamp already provides uniqueness in place of a CSID suffix); every 5 min: `find $RUN_DIR -newer ${TMPDIR:-/tmp}/refactor-team-check-$TS -type f | wc -l` — new files = alive; zero = stalled. Hard cutoff: 15 min no file activity → timed out. One extension (+5 min) if `tail -20` of output file explains delay; second unexplained stall = hard cutoff. On timeout: read `tail -100` of stalled file; surface partial results with ⏱; never omit.

After both complete: read output files from `$RUN_DIR/`, synthesize outputs, run quality stack, produce Final Report. Clean up health sentinel (exact filename, never a glob — another session's sentinel may share the base name):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-refactor-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] && rm -f "${TMPDIR:-/tmp}/refactor-team-check-$TS"
```

Exit — do not continue to Steps 3–5.
