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
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codex_state.py" read --state ".experiments/state/${RUN_ID}/codex-state"  # timeout: 5000 — prints CODEX_ITER, CODEX_NOOP_STREAK, CODEX_DISABLED (true once the streak reaches 2); missing file reads 0/0
```

Gate fail (`CODEX_DISABLED=true` or `CODEX_ITER >= MAX_CODEX_RUNS`): skip Phase 2c, continue to Phase 3.

Else print narration, update R5b before Agent call:

```text
[→ Iter N/max · Phase 2c: Codex co-pilot — running (CODEX_ITER/MAX_CODEX_RUNS)]
```

TaskUpdate R5b subject: `R5b: Codex co-pilot — iter N/max_iterations running`, status: `in_progress`

Codex runs a second pass in the same iteration, on the working tree Phase 2 left. Claude's edit is uncommitted and **undecided** here — Phases 3–7 have not run — so nothing is "kept" or "reverted" yet. Both passes form one change set: Phase 3 reconciles the changed paths of both against `scope_files` (a Codex path outside scope fails the combined scope check like any other), Phase 4 commits them as one experiment commit, and Phase 7 compares the single result against `best_metric`. "delta ≥ 0.1%" = delta against `best_metric`, not the previous Claude iteration. The combined change is kept only if delta ≥ 0.1% AND guard passes; one keep/revert decision covers both passes, so per-pass attribution comes from the diary entry, not from the delta.

- Claude Phase 2 **edited** the tree: Codex builds on that uncommitted work.
- Claude Phase 2 **no-op** (no edit): Codex attempts a fresh change on the clean tree.

Run Codex ideation:

```text
Skill(skill="bridge:implement", args="Goal: <goal>. Run clarification: <clarification_prompt> when present. Current metric: <metric_key>=<current_value> (baseline: <baseline>, direction: <higher|lower>). Scope files: <scope_files>. Compute: <compute>. Colab hardware: <colab_hw> when active. Read .experiments/state/<run-id>/context-<i>.md. Starting state: Claude's change is [present in the working tree, uncommitted and not yet evaluated | absent (Phase 2 was a no-op)]. Propose and implement one atomic optimization most likely to improve the metric without breaking <guard_cmd>. Write full reasoning to .experiments/state/<run-id>/codex-ideation-<i>.md.")
```

- Codex proposes a change: proceed Phases 3–7 on the combined change set (verify files, commit, metric, guard, decide). Diary entry (Phase 7a) names the paths each pass touched.
- Codex makes no change: Claude's edit (or the Phase 3 no-op) stands alone; note `codex-no-op` in the diary entry and continue to Phase 3.
- Set `"ideation_source": "codex"` in the Phase 8 JSONL record whenever Codex changed any path in the committed change set (the schema has no combined value); `"claude"` otherwise.

After Codex completes (any outcome):

**Persist the counters** — run exactly one of the two blocks below, chosen by the outcome just recorded. Both increment `CODEX_ITER`; they differ only in what happens to the no-op streak. Never edit the numbers by hand: the blocks re-read the file so the increment survives a compaction that landed mid-iteration.

Codex produced **no code changes** (`codex-no-op`) — streak grows:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codex_state.py" bump --state ".experiments/state/${RUN_ID}/codex-state" --outcome noop  # timeout: 5000 — re-reads the file, codex_iter + 1, noop_streak + 1; prints the new counters
```

Codex **proposed a change** (kept, reverted, or still under evaluation) — streak resets:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codex_state.py" bump --state ".experiments/state/${RUN_ID}/codex-state" --outcome productive  # timeout: 5000 — re-reads the file, codex_iter + 1, noop_streak reset to 0; prints the new counters
```

Streak reaching 2 is what disables Codex for the rest of the run — the gate check above derives that, so nothing else needs writing.

TaskUpdate R5b subject: `R5b: Codex co-pilot — iter N done (<outcome>)`

**Stuck escalation with `--codex`**: Phase 9 detects `STUCK_THRESHOLD` discards and `--codex` active → run the single stuck outcome of run `<constants>` step 3 (escalation iterations; any kept → continue, both discarded → stop). Codex boost applies inside those escalation iterations only, never replaces the stop — add to the Codex prompt: "Previous N attempts all reverted. Focus on fundamentally different approach (different file, different algorithm, different abstraction)."
