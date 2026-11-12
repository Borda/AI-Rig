<!-- file: team-mode.md — consumers: plugins/cc_develop/skills/refactor/SKILL.md (Team mode branch, gated on TEAM_MODE=true) -->

# Refactor — Team Mode Protocol

> **Agent budget** — each teammate costs ~120,851 tok fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing — work-displacement only; an isolation-motivated spawn (adversarial reviewer, distinct specialist role, model tier, worktree) runs regardless of size. Keep each teammate near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt requires an envelope even on exhaustion — `partial: true` plus what was finished.

Loaded only when `TEAM_MODE=true`. Steps 1–2 complete solo (teammates need scope + coverage context). Follow the serialized teammate protocol below; skip Steps 3–4 and Step 5's local review (sw-engineer runs it); after results run Step 5's quality stack and final gate, then the Final Report.

Compute run directory — its first line is the team-protocol guard shared by every develop team mode: printed `! BLOCKED` → stop the skill and relay that line as the final reply; never fall back to solo, never spawn teammates told to read a missing file:

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
[ -f ~/.claude/TEAM_PROTOCOL.md ] || { echo "! BLOCKED — --team needs ~/.claude/TEAM_PROTOCOL.md, written by /foundry:setup (requires foundry plugin); run /foundry:setup, or re-run without --team"; exit 1; }
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_setup_worktree_wrap.py" refactor
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-refactor-team-ts-${CSID}" 2>/dev/null || TS=""
IFS= read -r RUN_DIR_LITERAL < "${TMPDIR:-/tmp}/dev-refactor-run-dir-${CSID}" 2>/dev/null || RUN_DIR_LITERAL=""
[ -n "$TS" ] && [ -n "$RUN_DIR_LITERAL" ] || { echo "! dev-refactor-team-ts or dev-refactor-run-dir missing — cannot build teammate output paths; aborting team mode"; exit 1; }
: > "${TMPDIR:-/tmp}/dev-refactor-char-test-${CSID}"
```

**IMPORTANT**: in spawn prompts below, substitute `$RUN_DIR_LITERAL` with actual resolved path before constructing each Agent call — agents receive literal resolved strings, not shell variable references. Same for `$TS` and `<REFACTOR_SKILL>`: resolve the latter to this plugin's installed `skills/refactor/SKILL.md` using `${CLAUDE_PLUGIN_ROOT}` before spawning.

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
REFACTOR_SKILL="$PLUGIN_ROOT/skills/refactor/SKILL.md"
[ -f "$REFACTOR_SKILL" ] || { echo "! BLOCKED — refactor skill missing; repair the installation before spawning teammates"; exit 1; }
REFACTOR_SKILL=$(cd "$PLUGIN_ROOT/skills/refactor" && printf '%s/SKILL.md\n' "$PWD") || exit 1
printf '%s\n' "$REFACTOR_SKILL"
```

**Note on `model=` assignments**: `model=opus`/`model=sonnet` in spawn prompts below are advisory hints — effective only when actual foundry agents installed. Falling back to `general-purpose` (foundry absent): prompt-prepend `model=` doesn't reliably override agent-resolution fallback tier; effective model set by `agent-resolution.md`'s fallback table, not spawn prompt. Intentional — sonnet sufficient for qa-specialist characterization tests, opus for sw-engineer refactor implementation; on fallback, expect tier degradation noted in Final Report.

Serialize teammates — qa-specialist writes and gates characterization tests against **pre-refactor** source first, then sw-engineer applies refactor. Spawning sw-engineer first inverts safety net: characterization tests would be written against already-mutated code, so any behaviour change refactor introduces becomes undetectable (tests pin new behaviour instead of original). Mirrors solo Step 3 gate (`GATE OK: all characterization tests pass on unmodified code`).

**Step T1 — Spawn foundry:qa-specialist (model=sonnet) against the pre-refactor source, end the turn, resume on its completion notification**. Each `Agent()` call sets `name` and `description` and opens prompt line 1 with the teammate's delta; the shared target and boilerplate follow line 1. Slots: `name="refactor-qa"` · `description="characterization safety net"`. Prompt: "Write characterization tests before the refactor.

You are a foundry:qa-specialist teammate refactoring: [target]. Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Read `<REFACTOR_SKILL>` §Step 3: Add characterization tests (if needed) before writing tests. Your task: write characterization tests (Step 3) to build a safety net BEFORE any refactor — test the CURRENT (unmodified, pre-refactor) source and assert its existing behaviour. Scope constraint: only create/edit files under `tests/`. Do NOT edit source files. Report the exact characterization test file path in your saved analysis; the lead runs the gate itself and does not trust a self-reported pass count. Broadcast context: {target: <path>, coverage: <summary>, goal: <stated goal>}. Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output. Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal completion in final delta message: 'Status: complete | blocked — <reason>'. Write your full analysis to $RUN_DIR_LITERAL/refactor-qa-specialist.md using the Write tool. Return ONLY compact JSON: {"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line>"}."

**Gate T1 — characterization tests must pass on unmodified code before spawning sw-engineer**. Read qa-specialist's saved analysis, confirm its test file exists under `tests/`, and substitute that exact repository-relative path for the `<char_test_file>` line in the quoted here-document below; its contents are literal, including quotes and shell metacharacters. The same sentinel supplies T1 and T2; do not change it after T1 passes.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CHAR_TEST_FILE <<'CHAR_PATH'
<char_test_file>
CHAR_PATH
case "$CHAR_TEST_FILE" in *'<'*'>'*|*/../*|*/..) echo "! BLOCKED — unresolved or invalid characterization path"; exit 1 ;; tests/*) ;; *) echo "! BLOCKED — characterization path must be under tests/"; exit 1 ;; esac
printf '%s\n' "$CHAR_TEST_FILE" > "${TMPDIR:-/tmp}/dev-refactor-char-test-${CSID}"
```

```bash
# Gate T1  # timeout: 600000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CHAR_TEST_FILE < "${TMPDIR:-/tmp}/dev-refactor-char-test-${CSID}" 2>/dev/null || CHAR_TEST_FILE=""
case "$CHAR_TEST_FILE" in *'<'*'>'*|*/../*|*/..) echo "! BLOCKED — unresolved or invalid characterization path"; exit 1 ;; tests/*) ;; *) echo "! BLOCKED — characterization path must be under tests/"; exit 1 ;; esac
[ -n "$CHAR_TEST_FILE" ] && [ -f "$CHAR_TEST_FILE" ] || { echo "! BLOCKED — characterization test file missing; recover the qa-specialist path before T1"; exit 1; }
python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest -- "$CHAR_TEST_FILE" -v
GATE_EXIT=$?
case "$GATE_EXIT" in
    0) echo "GATE OK: characterization tests pass on unmodified code" ;;
    5) echo "GATE FAIL: no characterization tests collected"; exit 5 ;;
    64) echo "! GATE ERROR: runner refused to start; re-run Project Detection before T1"; exit 64 ;;
    *) echo "GATE FAIL: characterization tests failed (exit $GATE_EXIT); do not refactor"; exit "$GATE_EXIT" ;;
esac
```

Exit 0 → safety net green; proceed to T2. Exit 64 → recover the runner and re-run T1; do not repair assertions for a run that never started. Other nonzero exits (including 5 — no tests collected) → no valid safety net; do NOT refactor. Invoke `AskUserQuestion` — (a) re-spawn qa-specialist with corrected assertions/path (recommended) · (b) proceed without safety net (record acceptance in `checkpoint.md` — staged `checkpoint.md.rec` record, appended by `append_ledger.py`) · (c) abort. Explicit (b) permits T2 without a green T1; retain the missing pre-refactor proof in the Final Report even if T2 later passes.

On accepted (b) after a red T1, also follow `skills/refactor/SKILL.md` §Step 3's accepted-red recording rule: use the Write tool to save the exact characterization path in `$DEV_DIR/accepted-red-characterization.txt` before running the baseline. The baseline and T2 exclude that file; neither exclusion supplies characterization proof.

Before T2, the parent executes `skills/refactor/SKILL.md` §Step 4's **Pre-edit baseline** block against the scoped source and characterization files, excluding any recorded accepted-red characterization file. A red or missing baseline of the remaining tests blocks the implementation spawn even when the user accepted missing characterization coverage at T1; route only completed pre-existing source-test failures to `/develop:fix`.

**Step T2 — Only after Gate T1 is green or its explicit risk-acceptance path is recorded, and the Pre-edit baseline passes, spawn foundry:sw-engineer (model=opus) to apply the refactor**. Slots: `name="refactor-impl"` · `description="apply refactor steps"`. Prompt: "Apply the refactor behind the characterization safety net.

You are a foundry:sw-engineer teammate refactoring: [target]. Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Read `<REFACTOR_SKILL>` (the resolved `skills/refactor/SKILL.md`) §Step 4: Refactor with safety net and §Step 5: Review and close gaps before editing. Your task: apply Step 4 and Step 5's local review, retaining targeted-only loop tests, the 5-edit / 1800-second caps, batch snapshot rules and own-Edit-only rollback. Leave Step 5's quality stack, independent challenge and final gate to the lead. Scope constraint: only edit source files (not under `tests/`) — do NOT modify the characterization tests in `$RUN_DIR_LITERAL/refactor-qa-specialist.md`'s test file. Broadcast context: {target: <path>, coverage: <summary>, goal: <stated goal>, safety_net: $RUN_DIR_LITERAL/refactor-qa-specialist.md}. Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output. Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal completion in final delta message: 'Status: complete | blocked — <reason>'. Write your full analysis to $RUN_DIR_LITERAL/refactor-sw-engineer.md using the Write tool. Return ONLY compact JSON: {"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line>"}."

**Gate T2 — re-run the same characterization tests against the post-refactor source**, except the recorded accepted-red file, which is excluded without a green verdict. Green→green proves refactor preserved behaviour only when T1 passed. Any test now failing means refactor changed observable behaviour — surface with ⚠ and do NOT accept refactor until reconciled (fix source, or confirm behaviour change intended and update test deliberately).

```bash
# Gate T2  # timeout: 600000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CHAR_TEST_FILE < "${TMPDIR:-/tmp}/dev-refactor-char-test-${CSID}" 2>/dev/null || CHAR_TEST_FILE=""
case "$CHAR_TEST_FILE" in *'<'*'>'*|*/../*|*/..) echo "! BLOCKED — unresolved or invalid characterization path"; exit 1 ;; tests/*) ;; *) echo "! BLOCKED — characterization path must be under tests/"; exit 1 ;; esac
IFS= read -r DEV_DIR 2>/dev/null < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" || DEV_DIR=""
ACCEPTED_RED_CHAR=""
if [ -n "$DEV_DIR" ]; then IFS= read -r ACCEPTED_RED_CHAR 2>/dev/null < "$DEV_DIR/accepted-red-characterization.txt" || ACCEPTED_RED_CHAR=""; fi
ACCEPTED_RED_CHAR=${ACCEPTED_RED_CHAR%$'\r'}
if [ "$CHAR_TEST_FILE" = "$ACCEPTED_RED_CHAR" ]; then
    echo "GATE T2: accepted-red characterization test excluded: $CHAR_TEST_FILE; no characterization proof"
    exit 0
fi
[ -n "$CHAR_TEST_FILE" ] && [ -f "$CHAR_TEST_FILE" ] || { echo "! BLOCKED — characterization test file missing; recover the T1 path before T2"; exit 1; }
python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest -- "$CHAR_TEST_FILE" -v
GATE_EXIT=$?
case "$GATE_EXIT" in
    0) echo "GATE OK: characterization tests pass after refactor" ;;
    5) echo "GATE FAIL: no characterization tests collected; do not accept refactor"; exit 5 ;;
    64) echo "! GATE ERROR: runner refused to start; re-run Project Detection before T2"; exit 64 ;;
    *) echo "GATE FAIL: characterization tests failed (exit $GATE_EXIT); reconcile source before acceptance"; exit "$GATE_EXIT" ;;
esac
```

Health monitoring (CLAUDE.md §6): re-derive `$TS` and `$RUN_DIR` at block start (bash resets between calls — read back from temp files the spawn block persisted):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TS < "${TMPDIR:-/tmp}/dev-refactor-team-ts-${CSID}" 2>/dev/null || TS=""
[ -n "$TS" ] || { echo "! dev-refactor-team-ts missing — cannot locate team run dir; aborting team mode"; exit 1; }
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-refactor-run-dir-${CSID}" 2>/dev/null || RUN_DIR=".temp/develop/$TS"
```

Apply to each teammate independently — arm batch `team` in each spawn response (deliverables `$RUN_DIR/refactor-qa-specialist.md`, `$RUN_DIR/refactor-sw-engineer.md`). Spawn, **end the turn**, resume on the completion notification and run the check block (`agent_watch.py`) once (`agent-resolution.md` §Agent waits — no polling, per-agent deadlines) — never a fixed-interval check, never a liveness probe (`find -newer` or similar): the armed deadline (1800 s) is the only liveness mechanism; a `timed_out` row, or a notification without its deliverable, means timed out. On timeout: read `tail -100` of stalled file; surface partial results with ⏱; never omit.

After both complete: read output files from `$RUN_DIR/`, synthesize outputs, run quality stack, then the final gate — Step 5's "Quality-stack load", "Bounded independent implementation challenge" and "Final gate" blocks, executed as written (the quality stack's wide run is the gate's first full-suite run only when `$TEST_CMD` is that same pytest invocation, else run the final gate once; sw-engineer already ran the Step 5 local review; the parent still runs its bounded independent implementation challenge). Produce Final Report only after that; never report the suite green before the gate passes.

Exit after the Final Report — do not continue to Steps 3–4 or Step 5's local review.
