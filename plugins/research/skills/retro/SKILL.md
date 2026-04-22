---
name: retro
description: Post-run retrospective analysis of experiment history. Reads .experiments/ JSONL, computes statistical significance of improvements (Wilcoxon signed-rank), detects dead iterations, flags suspicious metric jumps, and generates a learning summary with next-hypothesis queue compatible with --hypothesis flag of research:run.
argument-hint: '[<run-id>] [--compare <run-id-2>] [--threshold <delta>] [--alpha <significance>]'
effort: medium
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
disable-model-invocation: true
---

<objective>

Post-run retrospective analysis. After `/research:run` completes, reads `.experiments/state/<run-id>/experiments.jsonl`, computes statistical significance, detects dead iterations, flags suspicious metric jumps, and generates a learning summary with a next-hypothesis queue.

NOT for: running experiments (use `/research:run`); designing experiments (use `/research:plan`); validating methodology (use `/research:judge`); verifying paper implementation (use `/research:verify`). Read-only analysis only — never modifies code, commits, or experiment state.

</objective>

<workflow>

## Agent Resolution

> **Foundry plugin check**: run `Glob(pattern="foundry*", path="$HOME/.claude/plugins/cache/")` returning results = installed. If check fails, proceed as if foundry available — common case; only fall back if agent dispatch explicitly fails.

`research:scientist` in same plugin as this skill — no fallback needed if research plugin installed.

# Retro Mode (Steps T1–T7)

Triggered by `retro`, `retro <run-id>`, or `retro <run-id> --compare <run-id-2>`.

**Defaults**: `--threshold 0.001`, `--alpha 0.05`.

**Task tracking**: create tasks for T1, T2, T3, T4, T5, T6, T7 at start — before any tool calls.

## Step T1: Locate and load run data

**Input resolution** (priority order):

1. Explicit `<run-id>` argument → read `.experiments/state/<run-id>/`
2. No argument → scan `.experiments/state/`, pick latest dir where `state.json` has `status: completed` or `status: goal-achieved`
3. None found → stop with error:
   ```text
   No completed run found. Run /research:run first, or provide: /research:retro <run-id>
   ```

**Load files** from `.experiments/state/<run-id>/`:

- `state.json`: extract `goal`, `best_metric`, `config` (including `metric.direction`), `iteration` count, `best_commit`. Compute `baseline_metric` from iteration 0 in `experiments.jsonl`.
- `experiments.jsonl`: full iteration history — validate each line parses as JSON. If last line truncated, warn and skip it.
- `diary.md`: if present, read for qualitative context in T5.

If `--compare <run-id-2>` present: load second run identically from `.experiments/state/<run-id-2>/`. If not found, stop: `"Compare target not found: .experiments/state/<run-id-2>/. Check run ID and retry."`

**Pre-compute run directory**:

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
```

```bash
RUN_DIR=".experiments/retro-$(date -u +%Y-%m-%dT%H-%M-%SZ)"  # timeout: 3000
mkdir -p "$RUN_DIR/scripts"
```

## Step T2: Statistical significance analysis

Write analysis script to `$RUN_DIR/scripts/analyze.py` via Write tool, then execute in two separate Bash calls (cd first if needed). Never inline Python.

**Script requirements**:

1. Load `experiments.jsonl`, filter iterations where `status == "kept"`, extract metric values into `kept_metrics` list.
2. Read `baseline_metric` from iteration 0 entry (`status == "baseline"`).
3. Read `direction` from `state.json` config (or infer from goal text: "higher"/"lower").

**If `len(kept_metrics) < 6`**: compute descriptive stats only (mean, median, min, max, std of kept_metrics). Print `"insufficient data for significance testing (N=<N>, minimum 6 required)"`. Do NOT compute or report a p-value.

**If N >= 6**: run Wilcoxon signed-rank test:

```python
from scipy.stats import wilcoxon
# Compare kept metrics against baseline repeated N times
baseline_repeated = [baseline_metric] * len(kept_metrics)
alternative = "greater" if direction == "higher" else "less"
stat, pvalue = wilcoxon(kept_metrics, baseline_repeated, alternative=alternative)
# Effect size: rank-biserial correlation r = 1 - (2W / (n*(n+1)))
n = len(kept_metrics)
r = 1 - (2 * stat) / (n * (n + 1))
```

**If `--compare`**: also run Wilcoxon between kept metrics of run-1 vs run-2 (paired if same length; warn if different lengths — use min length with truncation note).

**Error handling**: if `scipy` not installed, print `"pip install scipy required for significance testing, reporting descriptive stats only"` and fall back to descriptive stats. Handle empty JSONL (0 kept iterations) gracefully — report `"no kept iterations found"`.

Write results JSON to `$RUN_DIR/stats-results.json`. Execute script with `python3 $RUN_DIR/scripts/analyze.py` via Bash (`timeout: 30000`).

## Step T3: Dead iteration detection

**Definition**: dead iteration window = 3+ consecutive iterations (any status) where `abs(metric_delta) < threshold` (default `--threshold 0.001`).

Scan `experiments.jsonl` sequentially, skipping iteration 0 (baseline). For each window of 3+ consecutive iterations where `abs(delta) < threshold`:

- Record: `start_iter`, `end_iter`, `count`
- Classify type: `dead-plateau` if all iterations in window have `status: kept`; `dead-churn` if mixed `kept`/`reverted`/other
- Compute `wasted_iters` = total iterations in all dead windows

Write summary to `$RUN_DIR/dead-iters.json` via Write tool. Format:

```json
{
  "windows": [{"start": 5, "end": 8, "count": 4, "type": "dead-churn"}],
  "total_dead": 4,
  "total_iterations": 20,
  "dead_pct": 20.0
}
```

This analysis can be computed inline or via a script in `$RUN_DIR/scripts/` — use script if logic exceeds 3 lines.

## Step T4: Suspicious jump detection

Compute per-iteration absolute metric deltas for kept iterations only. Build a sliding window of 5 kept iterations to compute running mean and standard deviation of deltas.

Flag any single-step improvement where `abs(delta) > running_mean + 2 * running_std`:

| Severity | Condition |
| --- | --- |
| HIGH | `abs(delta) > running_mean + 3 * running_std` |
| MEDIUM | `abs(delta) > running_mean + 2 * running_std` (and not HIGH) |

For each flagged jump, record:

- `iteration`, `delta`, `sigma` (how many std above mean), `commit` SHA, `files` changed (from experiments.jsonl `files` field)
- Label: `"suspicious — investigate"` — NEVER auto-label `"data leakage"` or imply causation
- Include corresponding `diary.md` entry for that iteration if present

**Minimum data**: require at least 6 kept iterations before flagging (need 5 for window + 1 to test). Fewer → write `"insufficient data for jump detection (N=<N>)"`.

Write to `$RUN_DIR/suspicious-jumps.json` via Write tool.

## Step T5: Scientist learning summary

Pre-compute all file paths before spawning. Verify `$RUN_DIR/stats-results.json`, `$RUN_DIR/dead-iters.json`, and `$RUN_DIR/suspicious-jumps.json` exist (T2–T4 must complete first).

**Health monitoring setup**:

```bash
LAUNCH_AT=$(date +%s)
touch /tmp/retro-check-$LAUNCH_AT
```

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")`:

```markdown
Act as a research retrospective analyst.

Read:
- experiments.jsonl at <path> (full iteration history)
- diary.md at <path> (if exists — for qualitative context)
- stats results at <RUN_DIR>/stats-results.json
- dead iteration summary at <RUN_DIR>/dead-iters.json
- suspicious jumps at <RUN_DIR>/suspicious-jumps.json

Produce a retrospective analysis covering:

1. **Strategy effectiveness**: which agent types (perf/code/ml/arch) had highest kept-rate and average delta? Rank them. Include per-agent iteration count, kept count, and mean delta.
2. **Failure pattern analysis**: what approaches were repeatedly tried and reverted? Common failure modes? Group by pattern, not individual iteration.
3. **Diminishing returns**: at which iteration did improvement rate drop below 0.5% per iteration? Was the stopping point appropriate?
4. **Next hypotheses**: based on what worked and failed, generate 3–5 concrete next hypotheses. Write them as a hypotheses.jsonl-compatible file to <RUN_DIR>/hypotheses.jsonl — one JSON object per line with fields: hypothesis (str), rationale (str), confidence (float 0–1), expected_delta (str like "+2%"), priority (int 1=highest), source: "retro". Do NOT include feasible/blocker/codebase_mapping — retro entries skip the feasibility-annotation pass; /research:run treats absent feasibility fields as feasible:true.
5. **Cross-run insights** (only if compare data present in stats-results.json): which run's strategy was more effective and why?

Write full retrospective to <RUN_DIR>/retrospective.md using Write tool.
Include ## Confidence block per quality-gates rules.
Return ONLY: {"status":"done","hypotheses":N,"file":"<RUN_DIR>/retrospective.md","confidence":0.N}
```

**Health monitoring** (CLAUDE.md §8):

Poll every 5 min: `find $RUN_DIR -newer /tmp/retro-check-$LAUNCH_AT -type f | wc -l` — new files = alive; zero = stalled.

- **Hard cutoff: 15 min** no file activity → timed out
- **One extension (+5 min)**: if `tail -20 $RUN_DIR/retrospective.md` shows active progress (partial content written), grant one extension; second stall = hard cutoff
- **On timeout**: read `tail -100 $RUN_DIR/retrospective.md`; if file missing or empty, set `scientist_status = "timed_out"`, continue to T6. Surface with ⏱ in report.

Parse returned JSON envelope. Record `hypotheses` count and `confidence` for T6.

## Step T6: Write retro report

Pre-compute branch (already done in T1). Write full report to `.temp/output-retro-$BRANCH-$(date +%Y-%m-%d).md` via Write tool (never overwrite — append counter suffix if file exists):

```markdown
## Retrospective: <goal>

**Run**: <run-id>
**Date**: <date>
**Iterations**: <total> (<kept> kept, <reverted> reverted, <other> other)
**Baseline**: <metric_key> = <baseline>
**Best**: <metric_key> = <best> (<delta>% improvement)

### Statistical Significance

| Test | N | Statistic | p-value | Significant? | Effect size |
|------|---|-----------|---------|--------------|-------------|
| Wilcoxon vs baseline | N | ... | ... | YES/NO (alpha=<alpha>) | r=... (<small/medium/large>) |
| Wilcoxon run-1 vs run-2 | N | ... | ... | YES/NO | r=... |

(Second row only if `--compare` used. If N < 6: replace table with descriptive stats table — mean, median, min, max, std — and note "Insufficient data for significance testing (N=<N>)".)

**Effect size interpretation**: |r| < 0.3 = small, 0.3–0.5 = medium, > 0.5 = large.

### Dead Iterations

| Start | End | Count | Type | Notes |
|-------|-----|-------|------|-------|
| ... | ... | ... | dead-plateau / dead-churn | ... |

Total dead: <N> of <total> (<pct>% of compute)

(If no dead windows: "No dead iteration windows detected (threshold=<threshold>)")

### Suspicious Metric Jumps

| Iteration | Delta | Sigma | Severity | Commit | Files Changed |
|-----------|-------|-------|----------|--------|---------------|
| ... | ... | ... | HIGH/MEDIUM | <sha> | <files> |

(If none: "No suspicious jumps detected")
(If insufficient data: "Insufficient data for jump detection (N=<N>)")

### Strategy Effectiveness

| Strategy | Kept | Tried | Keep-rate | Avg Delta | Best Delta |
|----------|------|-------|-----------|-----------|------------|
| ... | ... | ... | ...% | ... | ... |

(From scientist retrospective. If scientist timed out: "Scientist agent timed out — strategy analysis unavailable")

### Failure Patterns
<From scientist retrospective — grouped failure modes>

### Diminishing Returns
<Iteration where improvement rate dropped below 0.5% per iteration, or "not applicable">

### Suggested Next Hypotheses

| # | Hypothesis | Rationale | Expected Delta | Confidence |
|---|-----------|-----------|----------------|------------|
| 1 | ... | ... | ... | 0.N |

Full retrospective: <RUN_DIR>/retrospective.md
Next hypotheses queue: <RUN_DIR>/hypotheses.jsonl

## Confidence
**Score**: 0.N — [high|moderate|low]
**Gaps**:
- [specific limitation]
```

## Step T7: Terminal summary

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
-> saved to .temp/output-retro-<branch>-<date>.md
---
Next: /research:run <program.md> --hypothesis <RUN_DIR>/hypotheses.jsonl
     /research:fortify <commit>    ← stress-test top hypothesis before full re-run
```

## Notes

- Retro is read-only — never modifies code, commits, or writes to `.experiments/state/<run-id>/`
- `.experiments/retro-<timestamp>/` stores analysis scripts, intermediate JSON, scientist output, and hypotheses.jsonl
- Retro run directories don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (exempt per `.claude/rules/artifact-lifecycle.md` — no `result.jsonl` = cleanup skipped); remove manually when no longer needed (`rm -rf .experiments/retro-*/`)
- `hypotheses.jsonl` output uses `source: "retro"` — compatible with `--hypothesis` flag of `/research:run`; `"retro"` is an additional declared source value extending the oracle schema (see `protocol.md`); feasibility fields omitted, treated as feasible:true by run
- `--compare` requires both runs to use the same metric; if metric names differ, stop with error: `"Cannot compare runs with different metrics: <metric-1> vs <metric-2>"`
- Dead iteration threshold (`--threshold`) should match the metric's noise floor — default 0.001 works for normalized metrics; adjust for raw values (e.g. `--threshold 0.1` for loss values in the hundreds)
- Statistical tests assume metric values are independent samples — if iterations are highly correlated (e.g. cumulative optimization), note this limitation in the report

</workflow>
