<!-- Mode-file include: loaded by .claude/skills/optimize/modes/run.md — not a standalone skill -->

<!-- Implements one mode extension: Team Mode (--team flag, Phases A–D) -->

<!-- Triggered from Step R5 of Default Mode when --team is active -->

## Team Mode (`--team`)

**When to trigger**: goal spans multiple optimization axes (e.g., "improve training speed" = model architecture + data pipeline + compute efficiency), OR user explicitly passes `--team`.

**Architecture**: two-phase pipeline — parallel hypothesis generation (read-only, no code changes) followed by sequential implementation on the live codebase ordered from minimal to largest change scope. This eliminates cross-axis conflicts: no worktrees, no cherry-picking; every implementation step sees the cumulative state of all prior kept changes.

**Team Mode directory layout**:

- `<RUN_DIR>` (`.experiments/<timestamp>/`) — hypothesis artifacts: `hypotheses-<axis-slug>.jsonl`, `hypothesis-analyst-<axis-slug>.md`, `team-queue.jsonl`, `team-results.jsonl`
- `.experiments/state/<run-id>/` — standard iteration artifacts: `ideation-team-<M>.md`, `diary.md`, `experiments.jsonl`

**Workflow:**

### Phase A: Parallel Hypothesis Generation (read-only)

1. Lead completes Steps R1–R4 (config, preconditions, baseline) solo.

2. Lead identifies 2–3 distinct optimization axes from the goal + codebase analysis. Example for "reduce training time": model architecture · data pipeline · compute efficiency.

3. Lead creates the run output directory:

   ```bash
   RUN_DIR=".experiments/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
   mkdir -p "$RUN_DIR"
   ```

   Store `RUN_DIR` as a run-level variable — do not re-evaluate `date` at later phases. All references to `<RUN_DIR>` in Phases B, C, and D use this same value.

   Write `phase: "A"` to `state.json` immediately after creating `RUN_DIR` — this makes Phase A resume reachable:

   ```
   {"team_mode": {"phase": "A", "run_dir": "<RUN_DIR>"}}
   ```

4. Spawn 2–3 hypothesis agents in parallel (reasoning agents at `opus` per CLAUDE.md §Agent Teams). **No worktrees** — agents perform read-only analysis only. Each agent is assigned one axis and a matching specialist type (same `agent_strategy` mapping from SKILL.md constants).

   Each hypothesis agent's spawn prompt:

   ```
   Read .claude/TEAM_PROTOCOL.md and use AgentSpeak v2.
   You are a hypothesis analyst. Your axis: <axis description>.
   Agent type: <agent type>.
   READ-ONLY: do NOT modify source files. You may only write to your designated output files.

   Analyze the codebase through the lens of your axis. Generate 3–5 concrete, implementable hypotheses.

   Baseline metric: <metric_cmd key> = <baseline>. Direction: <higher|lower>.
   Scope files: <scope_files>.
   Run clarification: <clarification_prompt>  ← omit this line entirely if clarification_prompt is null
   Program constraints: read <program_file> — especially ## Notes, ## Config.

   For each hypothesis, produce a JSON object with ALL these fields:
   - hypothesis: concrete description of the change
   - rationale: why this should improve the metric
   - confidence: float 0–1
   - expected_delta: expected metric change (e.g. "+1–3% val_loss")
   - priority: int (1 = highest within this axis)
   - source: "team"
   - axis: "<axis name>"
   - agent_type: "<your agent type>"
   - change_scope: "small" | "medium" | "large"
   - feasible: true | false
   - blocker: null | "<blocker description if feasible=false>"
   - codebase_mapping: "<files, classes, or functions to change>"

   change_scope guide:
   - small: 1–2 files, localized change (parameter tweak, single-function edit)
   - medium: 3–5 files, cross-cutting but bounded (module refactor, data path change)
   - large: 6+ files or architectural restructuring

   Write all hypotheses as JSONL (one JSON object per line) to `<RUN_DIR>/hypotheses-<axis-slug>.jsonl`.
   Write your full analysis, reasoning, and Confidence block to `<RUN_DIR>/hypothesis-analyst-<axis-slug>.md` using the Write tool.
   Return ONLY: {"status":"done","axis":"<axis>","count":N,"file":"<jsonl path>","confidence":0.N}
   Call TaskUpdate(in_progress) when starting; TaskUpdate(completed) when done.
   ```

**Health monitoring** (CLAUDE.md §8): after spawning all agents in step 4, create a checkpoint:

```bash
LAUNCH_AT=$(date +%s)
CHECKPOINT="/tmp/optimize-check-$LAUNCH_AT"
touch "$CHECKPOINT"
```

Poll every 5 min: `find $RUN_DIR -newer "$CHECKPOINT" -type f | wc -l` — new files = alive; zero = stalled.

- **Hard cutoff: 15 min** of no file activity → timed out
- **One extension (+5 min)**: if `tail -20 <output_file>` shows active progress, grant one extension; second stall = hard cutoff
- **On timeout**: read partial output from `<RUN_DIR>/hypotheses-<axis-slug>.jsonl`; surface with ⏱ in Phase D report — never silently omit a timed-out agent

5. Collect compact JSON envelopes from all hypothesis agents. Do not read the `.md` analysis files into lead context directly — they are inputs to Phase B queue assembly only.

   **`--researcher` / `--architect` interaction**: if R0 pre-phase ran before Team Mode, the R0 hypotheses in `<RUN_DIR>/hypotheses.jsonl` are included in Phase B queue assembly alongside the axis hypotheses. R0 entries lacking `axis`/`agent_type`/`change_scope` are backfilled: `axis: "cross-cutting"`, `agent_type` inferred from `source` field, `change_scope` inferred from `codebase_mapping` length (1–2 targets = small, 3–5 = medium, 6+ = large).

### Phase B: Queue Assembly + User Gate

1. Read all `<RUN_DIR>/hypotheses-*.jsonl` files (and `<RUN_DIR>/hypotheses.jsonl` if present from R0).

2. Filter: exclude entries with `feasible: false` (retain in file for audit). Move entries with `confidence < 0.7` to end of queue.

3. Sort the combined queue:

   - Primary: `change_scope` ascending — `small` first, then `medium`, then `large`
   - Secondary: `expected_delta` descending within scope tier (parse the delta string to extract numeric midpoint, e.g., "+1–3%" → 2.0; if `expected_delta` cannot be parsed to a numeric value, treat it as 0 — sort to end of scope tier)
   - Tertiary (tiebreaker): `confidence` descending

4. Assign sequential `queue_position` (1-indexed) to each entry in sorted order.

5. Print the queue as a formatted table:

   ```
   ┌────┬──────────────────────────────────┬────────────────────┬────────┬───────────────┬──────────┬────────────┐
   │ #  │ Hypothesis                       │ Axis               │ Scope  │ Expected Δ    │ Conf.    │ Agent      │
   ├────┼──────────────────────────────────┼────────────────────┼────────┼───────────────┼──────────┼────────────┤
   │ 1  │ Cache embeddings in forward pass │ data pipeline      │ small  │ +2–4% speed   │ 0.90     │ perf-opt   │
   │ 2  │ Fuse batch-norm + conv layers    │ model architecture │ small  │ +1–2% speed   │ 0.85     │ ai-res     │
   │ …  │ …                                │ …                  │ …      │ …             │ …        │ …          │
   └────┴──────────────────────────────────┴────────────────────┴────────┴───────────────┴──────────┴────────────┘

   Total: N hypotheses (N small, N medium, N large) across N axes
   ```

Before presenting the user gate, update `state.json` `team_mode.phase` to `"B"` — ensures resume can re-display the queue if interrupted:

```
{"team_mode": {"phase": "B", "run_dir": "<RUN_DIR>"}}
```

6. Present the user gate using `AskUserQuestion`:

   ```
   Proceed with implementation?
     (a) Run all N hypotheses in order shown
     (b) Select specific hypotheses (enter numbers, e.g. "1,3,5-7")
     (c) Abort
   ```

   - (a) proceeds with full queue
   - (b) filters to selected entries, preserving sort order
   - (c) stops; write partial report noting that hypotheses were generated but not tested

7. Write the final ordered queue to `<RUN_DIR>/team-queue.jsonl` (one JSON object per line, in execution order). Add `team_mode` to `state.json`:

   ```
   {
     "team_mode": {
       "axes": ["<axis-1>", "<axis-2>"],
       "phase": "C",
       "queue_file": "<RUN_DIR>/team-queue.jsonl",
       "current_hypothesis": 0,
       "total_hypotheses": N
     }
   }
   ```

### Phase C: Sequential Implementation + Guard

For each hypothesis in `<RUN_DIR>/team-queue.jsonl` (in sorted order, 1-indexed as M of N):

1. **Print header**: `[→ Team Hyp M/N · axis: <axis> · scope: <change_scope> · "<hypothesis short>"]`

2. **Spawn specialist agent** matching `agent_type` from the hypothesis. **On the real codebase** — no worktree. The spawn prompt follows the R5 Phase 2 ideation template with the hypothesis pre-specified:

   ```
   Goal: <goal>
   Run clarification: <clarification_prompt>  ← omit if null
   Current metric: <metric_cmd key> = <current_value> (baseline: <baseline>, direction: <higher|lower>)
   Scope files: <scope_files>
   Program constraints: read <program_file>

   Focus this iteration on implementing this hypothesis:
   "<hypothesis text>"
   Rationale: <rationale>
   Expected change scope: <change_scope>
   Target files: <codebase_mapping>

   Propose and implement ONE atomic change. Write analysis to
   `.experiments/state/<run-id>/ideation-team-<M>.md` using the Write tool.
   Return ONLY: {"description":"...","files_modified":[...],"scripts":[],"confidence":0.N}
   ```

3. **Run R5 Phases 3–7a identically** (verify changed files → commit → run metric → run guard → keep/rework/rollback → write diary entry). Phase 8 writes to `experiments.jsonl` and `state.json` as in standard mode; Phase 9 progress checks apply as in standard mode (stuck detection, diminishing returns, context compaction). Phase C does not duplicate this logic — it uses the same per-phase steps with the hypothesis-driven ideation output from step 2.

4. **Log outcome** to `<RUN_DIR>/team-results.jsonl` (append, one line per hypothesis):

   ```json
   {
     "queue_position": 1,
     "hypothesis": "<text>",
     "axis": "<axis>",
     "agent_type": "<agent>",
     "change_scope": "<scope>",
     "metric_before": 0.0,
     "metric_after": 0.0,
     "delta_pct": 0.0,
     "status": "kept|reverted|rework|no-op|hook-blocked|timeout",
     "commit": "<sha or null>",
     "timestamp": "<ISO>"
   }
   ```

5. **If kept**: update the running current metric value for the next hypothesis. Each subsequent hypothesis sees the cumulative state of all prior kept changes.

6. Update `state.json` `team_mode.current_hypothesis` after each hypothesis (enables resume).

7. `--codex`, `--colab`, `--journal` flags apply identically to standard R5.

### Phase D: Consolidated Report

After all hypotheses are processed (or the user stops early with Ctrl-C / user abort):

1. Read `<RUN_DIR>/team-results.jsonl`.

2. Write the full report to `.temp/output-optimize-team-<branch>-<YYYY-MM-DD>.md`:

   ```markdown
   ## Team Run: <goal>

   **Run ID**: <run-id>
   **Date**: <date>
   **Axes**: <comma-separated list>
   **Hypotheses tested**: <kept> kept · <reverted> reverted · <other> other (of <total>)
   **Baseline**: <metric> = <baseline>
   **Final**: <metric> = <final> (<total delta>%)

   ### Per-Hypothesis Results

   | #  | Hypothesis            | Axis    | Scope  | Δ%     | Status   | Commit |
   |----|-----------------------|---------|--------|--------|----------|--------|
   | 1  | Cache embeddings …    | data    | small  | +2.1%  | kept     | abc123 |

   ### Per-Axis Summary

   | Axis               | Tested | Kept | Best Δ% | Cumulative Δ% |
   |--------------------|--------|------|---------|----------------|
   | data pipeline      | 3      | 2    | +2.1%   | +2.9%          |

   ### Summary
   [2–3 sentences on what strategies worked, cross-axis interactions observed]

   ### Recommended Follow-ups
   - [next action if metric goal not fully reached]
   ```

3. Print compact terminal summary:

   ```
   ---
   Team Run — <goal>
   Hypotheses tested: <total>  Kept: <kept>  Reverted: <reverted>
   Axes:     <comma-separated list>
   Baseline: <metric_key> = <baseline>
   Final:    <metric_key> = <final> (<total delta>% improvement)
   → saved to .temp/output-optimize-team-<branch>-<date>.md
   ---
   ```

4. No teammates to shut down — hypothesis agents completed in Phase A; Phase C implementation agents are one-shot spawns.

**CLAUDE.md §8**: Health monitoring for Phase A is described in the Health monitoring block above (after step 4). Phase C implementation agents are standard single-iteration spawns — same timeouts as R5.

**Resume support**: `resume` mode reads `state.json.team_mode` to determine phase:

- `phase: "A"` — re-run Phase A from scratch (read-only, cheap to repeat)
- `phase: "B"` — re-display queue, re-prompt user gate
- `phase: "C"` — resume from `current_hypothesis + 1` (completed entries already in `team-results.jsonl`)
- `phase: "D"` — re-generate report
