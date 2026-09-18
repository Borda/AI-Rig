<!-- Codex co-pilot mode include: loaded by research:run when --codex flag is set -->

<!-- Implements Phase 2c of the R5 iteration loop -->

## Phase 2c — Codex co-pilot (`--codex` only)

> **Cost-bounded gate.** Run when `--codex` confirmed at R2 AND both gates pass:
>
> 1. **Cost ceiling** — `CODEX_ITER < MAX_CODEX_RUNS` (default `MAX_CODEX_RUNS=10`; even with `MAX_ITERATIONS=20`, Codex runs max 10 times).
> 2. **Diminishing returns** — last 2 Codex passes both produced no code changes → skip Codex remaining iterations, append note to `diary.md`: `"Codex skipped from iter N — 2 consecutive no-ops"`.

**Counters live in a file, never in prose.** `CODEX_ITER` and `CODEX_NOOP_STREAK` are persisted to `.experiments/state/<run-id>/codex-state` as JSON and re-read at every gate check. Prose-tracked counters are lost to mid-run compaction — silently re-opens whole Codex budget. `CODEX_DISABLED` is **derived** at read time (`CODEX_NOOP_STREAK >= 2`), never stored — one less value that can go stale.

Run at the **first** Phase 2c of the run, ahead of the gate check. The write is guarded on the file's absence, so re-running it after a resume or a compaction cannot reset a budget that is already part-spent:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
[ -z "$RUN_ID" ] && { echo "! BLOCKED — run-id sentinel missing; R2 must run before Phase 2c"; exit 1; }
mkdir -p ".experiments/state/${RUN_ID}"  # timeout: 3000
[ -f ".experiments/state/${RUN_ID}/codex-state" ] || printf '{"codex_iter": 0, "noop_streak": 0}\n' > ".experiments/state/${RUN_ID}/codex-state"
```

**Gate check** — reload the counters at the top of every Phase 2c, before deciding anything (bash state dies between calls; context memory dies at compaction). A missing file reads as `0/0`, so an interrupted run resumes with a fresh budget rather than a hard stop:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
_CODEX_STATE=".experiments/state/${RUN_ID}/codex-state"
CODEX_ITER=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/read_state_field.py" "$_CODEX_STATE" codex_iter --default 0 2>/dev/null || echo 0)  # timeout: 5000
CODEX_NOOP_STREAK=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/read_state_field.py" "$_CODEX_STATE" noop_streak --default 0 2>/dev/null || echo 0)  # timeout: 5000
CODEX_DISABLED=false; [ "$CODEX_NOOP_STREAK" -ge 2 ] 2>/dev/null && CODEX_DISABLED=true
echo "CODEX_ITER=$CODEX_ITER CODEX_NOOP_STREAK=$CODEX_NOOP_STREAK CODEX_DISABLED=$CODEX_DISABLED"
```

Gate fail (`CODEX_DISABLED=true` or `CODEX_ITER >= MAX_CODEX_RUNS`): skip Phase 2c, continue to Phase 3.

Else print narration, update R5b before Agent call:

```text
[→ Iter N/max · Phase 2c: Codex co-pilot — running (CODEX_ITER/MAX_CODEX_RUNS)]
```

TaskUpdate R5b subject: `R5b: Codex co-pilot — iter N/max_iterations running`, status: `in_progress`

Codex runs second pass when active — builds on Claude's kept change or fresh attempt after revert/no-op. Codex commit evaluated by Phase 7 against `best_metric` (same rule as any iteration); "delta ≥ 0.1%" = delta against `best_metric`, not previous Claude iteration. Codex wins only if delta ≥ 0.1% AND guard passes.

- Claude Phase 2 **kept**: Codex second pass on current state — builds on Claude's work.
- Claude Phase 2 **reverted/no-op**: working tree restored; Codex fresh attempt on clean tree.

Run Codex ideation:

```text
Skill(skill="bridge:implement", args="Goal: <goal>. Run clarification: <clarification_prompt> when present. Current metric: <metric_key>=<current_value> (baseline: <baseline>, direction: <higher|lower>). Scope files: <scope_files>. Compute: <compute>. Colab hardware: <colab_hw> when active. Read .experiments/state/<run-id>/context-<i>.md. Starting state: Claude's change was [kept|reverted|no-op]. Propose and implement one atomic optimization most likely to improve the metric without breaking <guard_cmd>. Write full reasoning to .experiments/state/<run-id>/codex-ideation-<i>.md.")
```

- Claude **kept** + Codex proposes: proceed Phases 3–7 (commit, verify, guard, decide). Codex wins only if delta ≥ 0.1% AND guard passes.
- Claude **kept** + Codex no-op: append `codex-no-op` record, continue — Claude's result stands.
- Claude **reverted/no-op** + Codex proposes: proceed Phases 3–7.
- Claude **reverted/no-op** + Codex no changes: append `status: codex-no-op` (`ideation_source: "codex"`), continue.
- Set `"ideation_source": "codex"` in Phase 8 JSONL record for any Codex-proposed change.

After Codex completes (any outcome):

**Persist the counters** — run exactly one of the two blocks below, chosen by the outcome just recorded. Both increment `CODEX_ITER`; they differ only in what happens to the no-op streak. Never edit the numbers by hand: the blocks re-read the file so the increment survives a compaction that landed mid-iteration.

Codex produced **no code changes** (`codex-no-op`) — streak grows:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
_CODEX_STATE=".experiments/state/${RUN_ID}/codex-state"
_ITER=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/read_state_field.py" "$_CODEX_STATE" codex_iter --default 0 2>/dev/null || echo 0)  # timeout: 5000
_STREAK=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/read_state_field.py" "$_CODEX_STATE" noop_streak --default 0 2>/dev/null || echo 0)  # timeout: 5000
printf '{"codex_iter": %d, "noop_streak": %d}\n' "$((_ITER + 1))" "$((_STREAK + 1))" > "$_CODEX_STATE"
```

Codex **proposed a change** (kept, reverted, or still under evaluation) — streak resets:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
_CODEX_STATE=".experiments/state/${RUN_ID}/codex-state"
_ITER=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/read_state_field.py" "$_CODEX_STATE" codex_iter --default 0 2>/dev/null || echo 0)  # timeout: 5000
printf '{"codex_iter": %d, "noop_streak": 0}\n' "$((_ITER + 1))" > "$_CODEX_STATE"
```

Streak reaching 2 is what disables Codex for the rest of the run — the gate check above derives that, so nothing else needs writing.

TaskUpdate R5b subject: `R5b: Codex co-pilot — iter N done (<outcome>)`

**Stuck escalation with `--codex`**: Phase 9 detects `STUCK_THRESHOLD` discards and `--codex` active → increase Codex effort — add to prompt: "Previous N attempts all reverted. Focus on fundamentally different approach (different file, different algorithm, different abstraction)."
