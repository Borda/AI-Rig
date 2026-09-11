---
name: retro
description: 'Post-run retrospective: reads .experiments/ JSONL, computes Wilcoxon significance, detects dead iterations, flags suspicious jumps, generates next-hypothesis queue for --hypothesis flag.'
argument-hint: '[<run-id>] [--compare <run-id-2>] [--threshold <delta>] [--alpha <significance>]'
effort: medium
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Post-run retrospective analysis. After `/research:run` completes, reads `.experiments/state/<run-id>/experiments.jsonl`, computes statistical significance, detects dead iterations, flags suspicious metric jumps, generates learning summary with next-hypothesis queue.

NOT for: running experiments (use `/research:run`); designing experiments (use `/research:plan`); validating methodology (use `/research:judge`); verifying paper implementation (use `/research:verify`); comparing runs from different programs/goals — `--compare` valid only for same-program, same-metric runs. Read-only — never modifies code, commits, or experiment state.

</objective>

<workflow>

## Agent Resolution

**Agent resolution**: load and follow the protocol below. Contains: foundry check + fallback table. `research:scientist` in same plugin — no fallback needed if research plugin installed.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_RESEARCH_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" 2>/dev/null)  # timeout: 5000
[ -z "$_RESEARCH_SHARED" ] && { echo "! Plugin path resolution failed — ensure research plugin installed and CLAUDE_PLUGIN_ROOT set, or invoke /research:retro from project root."; exit 1; }
echo "$_RESEARCH_SHARED" > "${TMPDIR:-/tmp}/research-shared-${CSID}"  # cold resolve — every later site reads this sentinel instead of re-running python
cat "$_RESEARCH_SHARED/agent-resolution.md"
```

## Retro Mode (Steps T1–T7)

Triggered by `retro`, `retro <run-id>`, or `retro <run-id> --compare <run-id-2>`.

**Defaults**: `--threshold 0.001`, `--alpha 0.05`.

**Unsupported flag check**: load and follow the protocol below. Supported flags for this skill: `--compare`, `--threshold`, `--alpha`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

**Task tracking**: create tasks for T1–T7 at start — before any tool calls.

### Step T1: Locate and load run data

**Input resolution** (priority order):

1. Explicit `<run-id>` arg → read `.experiments/state/<run-id>/`
2. No arg → scan `.experiments/state/`, pick latest dir where `state.json` has `status: completed` or `status: goal-achieved`
3. None found → stop with error:
   ```text
   No completed run found. Run /research:run first, or provide: /research:retro <run-id>
   ```

**Newer-in-progress check** (only when path 2 used — no explicit run-id given): after selecting completed run, scan `.experiments/state/` for any dir with `status: running` and mtime newer than selected dir. If found, surface warning but don't stop — user may intentionally retro prior completed run:

```text
⚠ Newer in-progress run found: <newer-run-id> (status: running, started <ISO timestamp>). Retro will analyse <selected-run-id> instead. Use /research:retro <run-id> to override.
```

**Load files** from `.experiments/state/<run-id>/`:

- `state.json`: extract `goal`, `best_metric`, `config` (incl. `metric.direction`), `iteration` count, `best_commit`. Compute `baseline_metric` from iteration 0 in `experiments.jsonl`.
- `experiments.jsonl`: full iteration history — validate each line parses as JSON. If last line truncated, warn and **rewrite sanitized copy to `$RUN_DIR/experiments-clean.jsonl`** (skip truncated last line). All downstream steps (T2 retro_analyze.py, T3 dead-iter scan, T5 scientist) must read sanitized copy — never raw file — so every step sees same iteration set. Persist sanitized path: `echo "$RUN_DIR/experiments-clean.jsonl" > "${TMPDIR:-/tmp}/retro-jsonl-path-${CSID}"` (consumers re-hydrate from this file). If JSONL untruncated, sanitized copy byte-identical to raw file.
- `diary.md`: if present, read for qualitative context in T5.

If `--compare <run-id-2>` present: load second run identically from `.experiments/state/<run-id-2>/`. If not found, stop: `"Compare target not found: .experiments/state/<run-id-2>/. Check run ID and retry."`

**Assign `RUN_ID_ARG`** from `$ARGUMENTS` — first positional non-flag token, empty if absent (ADV-H17):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_REMAINDER=$(echo "$ARGUMENTS" | sed -E 's/--compare[= ]+[^ ]+//g; s/--threshold[= ]+[^ ]+//g; s/--alpha[= ]+[^ ]+//g')
RUN_ID_ARG=$(echo "$_REMAINDER" | awk '{for (i=1; i<=NF; i++) if ($i !~ /^--/) { print $i; exit }}')
RUN_ID_ARG="${RUN_ID_ARG:-}"
echo "$RUN_ID_ARG" > "${TMPDIR:-/tmp}/retro-run-id-${CSID}"  # persist for T3 (vars lost between Bash calls)
```

**Pre-compute run directory** — also fix `$RUN_ID` (resolved from input resolution above), persist `$RUN_DIR` for T3 (ADV-H18 + ADV-L16):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_ID="${RUN_ID_ARG:-$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/find_run_id.py" .experiments/state 2>/dev/null)}"  # loads: find_run_id.py
# T-G2: find_run_id.py errors suppressed 2>/dev/null; empty case surfaced to avoid double-slash path
[ -z "$RUN_ID" ] && { echo "! Failed to resolve run ID — no completed run found or bin/find_run_id.py unavailable; check research plugin install."; exit 1; }
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
echo "$RUN_ID" > "${TMPDIR:-/tmp}/retro-run-id-resolved-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "retro" ".experiments" 2>/dev/null)  # timeout: 5000
mkdir -p "$RUN_DIR/scripts"  # timeout: 3000
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/retro-run-dir-${CSID}"  # T3 + fallback path reload from temp file
```

### Step T2: Statistical significance analysis

Run the Wilcoxon signed-rank test via the bundled bin/ script — pure Python with scipy.stats:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/retro-run-id-resolved-${CSID}" 2>/dev/null || RUN_ID=""  # re-hydrate RUN_ID from T1 (Check 41: fresh shell)
ALPHA="${ALPHA:-0.05}"
METRIC_DIRECTION=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/read_state_field.py" ".experiments/state/$RUN_ID/state.json" "config.metric.direction" --default "higher" 2>/dev/null || echo "higher")  # loads: read_state_field.py
IFS= read -r RETRO_JSONL < "${TMPDIR:-/tmp}/retro-jsonl-path-${CSID}" 2>/dev/null || RETRO_JSONL=".experiments/state/$RUN_ID/experiments-clean.jsonl"  # re-hydrate sanitized path from T1 (Check 41: fresh shell)
RETRO_RESULT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/retro_analyze.py" --jsonl "$RETRO_JSONL" --baseline "baseline" --alpha "$ALPHA" --direction "$METRIC_DIRECTION")  # timeout: 30000
RETRO_EXIT=$?
echo "$RETRO_RESULT" > "${TMPDIR:-/tmp}/retro-result-${CSID}"  # persist for effect-size block (Check 41: fresh shell)
[ "$RETRO_EXIT" -eq 2 ] && { echo "retro: Input error (exit 2) — run-id '$RUN_ID' missing, malformed, or has no baseline record; re-run /research:run to create baseline"; exit 1; }
```

**Contract** — script reads JSONL, extracts metric values for ALL iterations with `status == "kept"`, runs one-sided **one-sample** Wilcoxon signed-rank test of "kept iterations vs single baseline metric" (`status == "baseline"`). Not a paired test — run records one baseline metric, no per-iteration matched baseline; baseline scalar compared against each kept value. Prints single line of JSON to stdout:

- `{"significant": bool, "p_value": float, "statistic": float, "n": int}` on success
- `{"significant": false, "p_value": null, "statistic": null, "n": <N>, "reason": "<msg>"}` when `N < 6` or scipy missing
- `{"error": "<msg>"}` on input error (exit 2 — missing file, malformed JSON, no baseline record)

Exit codes: `0` = significant · `1` = not significant (or insufficient data) · `2` = input error.

**Direction handling** — script branches on `--direction`:

- `higher` → `alternative = "greater"` (improvement = candidate > baseline)
- `lower` → `alternative = "less"` (improvement = candidate < baseline — for loss, latency, error)

Read `direction` from `state.json` config (or infer from goal text), pass via `$METRIC_DIRECTION`.

**Effect size** — script does not return rank-biserial `r` directly. Compute via the bundled bin/ script:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RETRO_RESULT < "${TMPDIR:-/tmp}/retro-result-${CSID}" 2>/dev/null || RETRO_RESULT=""  # re-hydrate from T2 (Check 41: fresh shell)
EFFECT_R=$(echo "$RETRO_RESULT" | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/compute_effect_size.py")  # timeout: 5000
```

**If `--compare`**: invoke script second time on second run's `experiments.jsonl`; downstream report renders second row.

Write the combined results (parsed JSON plus computed `r`) to `$RUN_DIR/stats-results.json` via Write tool.

### Step T3: Dead iteration detection

**Definition**: dead iteration window = 3+ consecutive iterations (any status) where `abs(metric_delta) < threshold` (default `--threshold 0.001`).

**Scale check** (after loading baseline_metric in T1): if `baseline_metric > 100 * threshold`, print:

```text
! Threshold advisory: baseline_metric=[value] is >100x the default threshold (0.001).
  For this metric scale, consider: --threshold [baseline_metric * 0.0001:.4f]
  Proceeding with --threshold [threshold] — override with: /research:retro <run-id> --threshold <value>
```

Apply advisory threshold automatically only when `--threshold` not explicitly provided by user.

**Timeout detection**: when scanning reverted iterations, check `status` field. If `status == "timeout"`: classify as `timeout-as-revert` (see Notes). Else: flag any reverted iteration where `delta` is in correct improvement direction (metric moved toward goal) as "possible timeout — verify commit [sha]"; don't count delta as valid.

Scan `experiments.jsonl` sequentially, skipping iteration 0 (baseline). For each window of 3+ consecutive iterations where `abs(delta) < threshold`:

- Record: `start_iter`, `end_iter`, `count`
- Classify type: `dead-plateau` if all iterations in window have `status: kept`; `dead-churn` if mixed `kept`/`reverted`/other
- Compute `wasted_iters` = total iterations in all dead windows

Re-hydrate cross-Bash state at the start of every separate Bash invocation in T3 (each Bash call is a fresh shell — `$RUN_DIR` / `$RUN_ID_ARG` lost across calls; ADV-H18 / ADV-L16):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/retro-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
IFS= read -r RUN_ID_ARG < "${TMPDIR:-/tmp}/retro-run-id-${CSID}" 2>/dev/null || RUN_ID_ARG=""
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/retro-run-id-resolved-${CSID}" 2>/dev/null || RUN_ID=""
IFS= read -r RETRO_JSONL < "${TMPDIR:-/tmp}/retro-jsonl-path-${CSID}" 2>/dev/null || RETRO_JSONL=".experiments/state/$RUN_ID/experiments-clean.jsonl"
# T-C1: separate guards — `|| ... &&` has subtle precedence. `exit 1` terminates the Bash
# subprocess only — orchestrator must treat non-zero exit as hard stop, not proceed to T4.
# One call reports both missing values; a trailing `[ -z ] && { …; }` guard would also
# leave the block's exit status at 1 whenever the value IS present (T-C1).
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/require-vars.py" "$RUN_DIR" "retro T3: RUN_DIR missing — T1 must run first" "$RUN_ID" "retro T3: RUN_ID missing — T1 must run first" || exit 1
```

Write summary to `$RUN_DIR/dead-iters.json` via Write tool. Format:

```json
{
  "windows": [{"start": 5, "end": 8, "count": 4, "type": "dead-churn"}],
  "total_dead": 4,
  "total_iterations": 20,
  "dead_pct": 20.0
}
```

Write dead-iteration scan script to `$RUN_DIR/scripts/dead-iter-scan.py` via Write tool, then execute in a separate Bash call. Never inline Python in the Bash command. (Different from T2: T3 writes a fresh dynamic script per invocation; T2 invokes a static bin/ script.)

### Step T4: Suspicious jump detection

Compute per-iteration absolute metric deltas for kept iterations only. Build sliding window of 5 kept iterations to compute running mean and std of deltas.

Flag any single-step improvement where `abs(delta) > running_mean + 2 * running_std`:

| Severity | Condition |
| -- | -- |
| HIGH | `abs(delta) > running_mean + 3 * running_std` |
| MEDIUM | `abs(delta) > running_mean + 2 * running_std` (and not HIGH) |

For each flagged jump, record:

- `iteration`, `delta`, `sigma` (how many std above mean), `commit` SHA, `files` changed (from experiments.jsonl `files` field)
- Label: `"suspicious — investigate"` — NEVER auto-label `"data leakage"` or imply causation
- Include corresponding `diary.md` entry for that iteration if present

**Minimum data**: require ≥6 kept iterations before flagging (need 5 for window + 1 to test). Fewer → skip suspicious-jump detection entirely, write `"⚠ Insufficient data for trend analysis (need ≥6 data points, have <N>)"` in Suspicious Metric Jumps section of report.

Write to `$RUN_DIR/suspicious-jumps.json` via Write tool.

### Step T5: Scientist learning summary

Pre-compute all file paths before spawning, and substitute the actual computed values for every `<RUN_DIR>`/`<path>` placeholder in the prompt below before constructing the Agent() call — an unsubstituted placeholder reaches the agent as literal text, output lands in a wrongly-named directory, and the post-call check reports a false timeout. Verify `$RUN_DIR/stats-results.json`, `$RUN_DIR/dead-iters.json`, `$RUN_DIR/suspicious-jumps.json` exist (T2–T4 must complete first).

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")`:

```markdown
Act as a research retrospective analyst.

Read:
- experiments-clean.jsonl at <RETRO_JSONL path — the sanitized copy written by T1; fall back to experiments.jsonl if clean copy absent> (full iteration history)
- diary.md at <path> (if exists — for qualitative context)
- stats results at <RUN_DIR>/stats-results.json
- dead iteration summary at <RUN_DIR>/dead-iters.json
- suspicious jumps at <RUN_DIR>/suspicious-jumps.json

Produce a retrospective analysis covering:

1. **Strategy effectiveness**: which agent types (perf/code/ml/arch) had highest kept-rate and average delta? Rank them. Include per-agent iteration count, kept count, and mean delta.
2. **Failure pattern analysis**: what approaches were repeatedly tried and reverted? Common failure modes? Group by pattern, not individual iteration.
3. **Diminishing returns**: at which iteration did improvement rate drop below 0.5% per iteration? Was the stopping point appropriate?
4. **Next hypotheses**: based on what worked and failed, generate 3–5 concrete next hypotheses. Write them as a hypotheses.jsonl-compatible file to <RUN_DIR>/hypotheses.jsonl — one JSON object per line with fields: hypothesis (str), rationale (str), confidence (float 0–1), expected_delta (str like "+2%"), priority (int 1=highest), source: "retro". Do NOT include feasible/blocker/codebase_mapping — feasibility annotation is optional in this context; /research:run treats absent feasibility fields as feasible:true. Note: full feasibility-annotation workflow is defined in research:scientist — see that agent for complete annotation spec.
5. **Cross-run insights** (only if compare data present in stats-results.json): which run's strategy was more effective and why?

Write full retrospective to <RUN_DIR>/retrospective.md using Write tool.
Include ## Confidence block per quality-gates rules.
Return ONLY: {"status":"done","hypotheses":N,"file":"<RUN_DIR>/retrospective.md","confidence":0.N}
```

**Health monitoring note** (CLAUDE.md §6): the research:scientist agent runs in the background — spawn, then end the turn; no filler call, no "waiting" line, no sleep. If the completion notification arrives with no output file, or nothing appears for >15 min, treat as timed out.

**Post-call timeout check**: after Agent() returns, verify:

- File `$RUN_DIR/retrospective.md` exists and has content → success
- File missing or empty → set `scientist_status = "timed_out"`, continue to T6; surface with ⏱ in report

Parse returned JSON envelope. Record `hypotheses` count and `confidence` for T6.

### Step T6: Write retro report

```bash
mkdir -p .reports/research  # timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
```

Write full report to `.reports/research/retro-$BRANCH-$(date +%Y-%m-%d).md` via Write tool. Anti-overwrite: `BASE=".reports/research/retro-$BRANCH-$(date +%Y-%m-%d).md"; OUT="$BASE"; COUNT=2; while [ -f "$OUT" ]; do OUT="${BASE%.md}-${COUNT}.md"; COUNT=$((COUNT+1)); done`

```markdown
---
Title:         Retro — [goal]
Date:          [YYYY-MM-DD]
Scope:         [run-id] / [total] iterations
Focus:         retrospective analysis of ML optimization run
Agents:        research:scientist (T5)
Outcome:       IMPROVED | STALLED | PLATEAU | DIVERGED
Significance:  p=[value] ([significant|not significant] at alpha=[alpha])
Hypotheses:    [N] next steps generated
Confidence:    [score] — [key gaps]
Next steps:    /research:run … --hypothesis | /research:fortify
Path:          → .reports/research/retro-<branch>-<date>.md
---

## Retrospective: <goal>

**Run**: <run-id>
**Date**: <date>
**Iterations**: <total> (<kept> kept, <reverted> reverted, <other> other)
**Baseline**: <metric_key> = <baseline>
**Best**: <metric_key> = <best> (<delta>% improvement)

### Statistical Significance

| Test | N | Statistic | p-value | Significant? | Effect size |
| --- | --- | --- | --- | --- | --- |
| Wilcoxon vs baseline | N | ... | ... | YES/NO (alpha=<alpha>) | r=... (<small/medium/large>) |
| Wilcoxon run-1 vs run-2 | N | ... | ... | YES/NO | r=... |

(Second row only if `--compare` used. If N < 6: replace table with descriptive stats table — mean, median, min, max, std — and note "Insufficient data for significance testing (N=<N>)".)

**Effect size interpretation**: |r| < 0.3 = small, 0.3–0.5 = medium, > 0.5 = large.

> **Independence caveat** — Wilcoxon assumes independent samples. Sequential optimization iterations are typically autocorrelated; p-value is indicative only, not formally valid. If `dead_pct > 30%` from the Dead Iterations section, escalate caveat to HIGH: "p-value unreliable — high autocorrelation from dead-plateau windows."

### Dead Iterations

| Start | End | Count | Type | Notes |
| --- | --- | --- | --- | --- |
| ... | ... | ... | dead-plateau / dead-churn | ... |

Total dead: <N> of <total> (<pct>% of compute)

(If no dead windows: "No dead iteration windows detected (threshold=<threshold>)")

### Suspicious Metric Jumps

| Iteration | Delta | Sigma | Severity | Commit | Files Changed |
| --- | --- | --- | --- | --- | --- |
| ... | ... | ... | HIGH/MEDIUM | <sha> | <files> |

(If none: "No suspicious jumps detected")
(If insufficient data: "Insufficient data for jump detection (N=<N>)")

### Strategy Effectiveness

| Strategy | Kept | Tried | Keep-rate | Avg Delta | Best Delta |
| --- | --- | --- | --- | --- | --- |
| ... | ... | ... | ...% | ... | ... |

(From scientist retrospective. If scientist timed out: "Scientist agent timed out — strategy analysis unavailable")

### Failure Patterns
<From scientist retrospective — grouped failure modes>

### Diminishing Returns
<Iteration where improvement rate dropped below 0.5% per iteration, or "not applicable">

### Suggested Next Hypotheses

| # | Hypothesis | Rationale | Expected Delta | Confidence |
| --- | --- | --- | --- | --- |
| 1 | ... | ... | ... | 0.N |

Full retrospective: <RUN_DIR>/retrospective.md
Next hypotheses queue: <RUN_DIR>/hypotheses.jsonl

## Confidence
**Score**: 0.N — [high|moderate|low]
**Gaps**:
- Finding confidence (dead windows, suspicious jumps, classification errors, pattern detection): [high|moderate|low] — independent of statistical test availability
- Statistical confidence (Wilcoxon p-value): [available: p=X | unavailable: scipy not installed — descriptive stats only]
- [other specific limitations]
```

### Step T7: Terminal summary and follow-up gate

Print compact summary to terminal only — do NOT repeat full report:

```text
---
Retro — <goal>
Run:           <run-id> (<total> iterations, <kept> kept)
Significance:  p=<value> (<significant|not significant> at alpha=<alpha>)  [or: N=<N> insufficient]
Effect size:   r=<value> (<small|medium|large>)  [or: n/a]
Dead iters:    <N>/<total> (<pct>%)  [or: none]
Suspicious:    <N> jumps (<severity> — investigate: <sha1>, <sha2>)  [or: none]
Hypotheses:    <N> next steps generated
-> saved to .reports/research/retro-<branch>-<date>.md
---
Next: /research:run <program.md> --hypothesis <RUN_DIR>/hypotheses.jsonl    [only if scientist_status != "timed_out" AND <RUN_DIR>/hypotheses.jsonl exists]
     /research:fortify <run-id>    ← stress-test top hypothesis before full re-run
```

If `scientist_status == "timed_out"` or `<RUN_DIR>/hypotheses.jsonl` does not exist on disk, omit the `--hypothesis` Next line entirely and replace with: `Next: /research:fortify <run-id>    ← scientist analysis unavailable; no hypotheses queue generated`.

</workflow>

<notes>

- Retro read-only — never modifies code, commits, or writes to `.experiments/state/<run-id>/`
- `.experiments/retro-<timestamp>/` stores analysis scripts, intermediate JSON, scientist output, hypotheses.jsonl
- Retro run dirs don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (exempt per `.claude/rules/foundry-artifact-lifecycle.md` — no `result.jsonl` = cleanup skipped); remove manually when done (`rm -rf .experiments/retro-*/`)
- `hypotheses.jsonl` uses `source: "retro"` — compatible with `--hypothesis` flag of `/research:run`; `"retro"` extends oracle schema (see `protocol.md`); feasibility fields omitted, treated as feasible:true by run
- `--compare` requires both runs use same metric; if metric names differ, stop: `"Cannot compare runs with different metrics: <metric-1> vs <metric-2>"`
- Dead iteration threshold (`--threshold`) should match metric's noise floor — default 0.001 for normalized metrics; adjust for raw values (e.g. `--threshold 0.1` for loss in hundreds)
- Statistical tests assume metric values are independent samples — if iterations highly correlated (e.g. cumulative optimization), note limitation in report
- **Requires `scipy` in active Python environment** (`pip install scipy`) — `retro_analyze.py` runs Wilcoxon signed-rank test via `scipy.stats`. Without scipy, test skipped and `retro_analyze.py` returns `{"significant": false, "p_value": null, "reason": "scipy not installed"}`; report includes descriptive stats only (mean/median/min/max/std). Install: `pip install scipy` or `uv add scipy`.
- **Named anomaly patterns** (use consistently across reports):
  - `kept-regression`: kept iteration where metric moved in wrong direction (positive delta for higher-is-better, negative delta for lower-is-better)
  - `reverted-improvement`: reverted iteration where metric moved in correct direction — reverted for non-metric reasons (performance, OOM, instability); flag as "improvement-when-reverted — consider revisiting with adjusted constraints"
  - `timeout-as-revert`: reverted iteration with `status: "timeout"` — metric value unreliable; never count delta as valid improvement
  - `config-repetition`: same agent + same file(s) attempted 3+ times without crossing threshold — flag as "repeated-failure pattern"

</notes>
