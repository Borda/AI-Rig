---
name: fortify
description: Systematic ablation study runner. After research:run finds improvements, fortify identifies component candidates from git diff + diary, creates isolated git worktrees per ablation (main repo never modified), runs metric+guard in each worktree, ranks component importance, and optionally generates reviewer Q&A calibrated to a target venue.
argument-hint: '[<run-id>|<program.md>] [--venue <CVPR|NeurIPS|ICML|workshop>] [--max-ablations <N>] [--skip-run] [--compute=local|colab|docker] [--colab[=HW]]'
effort: high
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Systematic ablation study runner — after `/research:run` finds improvements, fortify identifies which components contributed, generates ablation variants (remove one component at a time), runs each variant in **isolated git worktrees** (main repo never modified), ranks component importance, and optionally generates reviewer Q&A material calibrated to a venue.

NOT for: running the initial optimization loop (use `/research:run`); validating methodology before running (use `/research:judge`); verifying paper-vs-code consistency (use `/research:verify`); hypothesis generation (use `research:scientist` directly). Fortify exclusively runs ablation studies on completed runs.

</objective>

<constants>

```yaml
MAX_ABLATION_CANDIDATES:  8 (ceiling — scientist produces 3–8; --max-ablations caps further)
METRIC_TIMEOUT_MS:        360000 (6 min — same as run SKILL.md)
GUARD_TIMEOUT_MS:         360000
GIT_OP_TIMEOUT_MS:        15000
SANITY_DIVERGENCE_PCT:    2.0 (full-variant vs best_metric mismatch threshold)
IMPORTANCE_CLASS_CRITICAL: 50.0 (% of full metric lost)
IMPORTANCE_CLASS_SIGNIFICANT: 10.0
FORTIFY_DIR_BASE:         .experiments
STATE_DIR_BASE:           .experiments/state
```

</constants>

<workflow>

## Agent Resolution

> **Foundry plugin check**: run `Glob(pattern="foundry*", path="$HOME/.claude/plugins/cache/")` returning results = installed. If check fails, proceed as if foundry available — common case; only fall back if agent dispatch explicitly fails.

`research:scientist` in same plugin as this skill — no fallback needed if research plugin installed.

## CRITICAL: Worktree-based isolation

**Do NOT use `git checkout -b <branch>` for ablations** — this dirties the main working tree and corrupts concurrent tool calls. Each ablation gets its own git worktree under `$FORTIFY_DIR/worktrees/<variant>`, created from `best_commit`. Main working tree is NEVER modified. Cleanup: `git worktree remove --force` per variant; `git worktree prune` on interrupt.

# Fortify Mode (Steps F1–F8)

Triggered by `fortify` or `fortify <run-id|program.md>`.

**Task tracking**: create tasks for F1, F2, F3, F4, F5, F6, F7, F8 at start — before any tool calls.

## Step F1: Locate source run and validate judge approval

**Input resolution** (priority order):

1. Explicit `<run-id>` argument → read `.experiments/state/<run-id>/state.json`
2. Explicit `<program.md>` argument → scan `.experiments/state/*/state.json` for matching `program_file`, pick latest with `status: completed` or `status: goal-achieved`
3. No argument → scan `.experiments/state/`, pick latest with `status: completed` or `status: goal-achieved`
4. None found → stop:
   ```text
   fortify: No completed run found. Run /research:run first.
   ```

**Guard: judge approval required.** Scan `.experiments/judge-*/` directories. For each, check if `methodology.md` references the same `program_file`. If no directory with an APPROVED verdict found:

```text
fortify: BLOCKED — no APPROVED judge verdict found for this program.
Ablation studies require an approved baseline. Run: /research:judge <program.md>
```

Read from `state.json`: `goal`, `best_metric`, `best_commit`, `config` (including `metric_cmd`, `guard_cmd`, `compute`), `program_file`.

Also read `baseline_commit` — iteration 0 commit from `experiments.jsonl` (first line, `status: "baseline"`, field `"commit"`).

**Pre-compute run directory** (each in separate Bash call):

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)                                           # timeout: 3000
FORTIFY_DIR=".experiments/fortify-$TS"                                       # timeout: 5000
WORKTREE_BASE="$FORTIFY_DIR/worktrees"
mkdir -p "$FORTIFY_DIR" "$WORKTREE_BASE"
```

## Step F2: Identify ablation candidates via scientist

Gather two inputs for the scientist:

1. **Git diff**: run `git diff <baseline_commit>...<best_commit> --stat` (summary) and full `git diff <baseline_commit>...<best_commit>`. If full diff exceeds ~200 lines, write to `$FORTIFY_DIR/diff.txt` via Write tool; otherwise inline in prompt.
2. **Experiment history**: paths to `experiments.jsonl` and `diary.md` from the source run directory.

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")` with health monitoring (15-min cutoff, one 5-min extension — same pattern as judge J3):

```markdown
Act as an ML ablation study designer.

Read:
- git diff at <FORTIFY_DIR>/diff.txt (or inline if small)
- experiments.jsonl at <path> (filter for entries with status: "kept")
- diary.md at <path> (if exists)

Identify 3–8 distinct logical components that were changed during this run.
A component = a logically independent change that can be removed independently.

For each component produce one JSON line to <FORTIFY_DIR>/ablation-candidates.jsonl:
{
  "component_id": <int>,
  "name": "<descriptive name, e.g. 'learning rate warmup'>",
  "description": "<what it does and why it was introduced>",
  "files": ["<file:line range>"],
  "revert_commits": ["<commit SHA>"],
  "expected_importance": "HIGH|MEDIUM|LOW"
}

Write your analysis to <FORTIFY_DIR>/candidates-analysis.md.
Include ## Confidence block.
Return ONLY: {"status":"done","components":N,"file":"<FORTIFY_DIR>/ablation-candidates.jsonl","confidence":0.N}
```

**Health monitoring** (CLAUDE.md §8):

```bash
LAUNCH_AT_F2=$(date +%s)
CHECKPOINT_F2="/tmp/fortify-check-$LAUNCH_AT_F2"
touch "$CHECKPOINT_F2"  # timeout: 3000
```

Poll every 5 min: `find <FORTIFY_DIR> -newer "$CHECKPOINT_F2" -type f | wc -l` (`timeout: 5000`) — new files = alive; zero = stalled.

- **Hard cutoff: 15 min** no file activity → timed out
- **One extension (+5 min)**: if `tail -20 <FORTIFY_DIR>/candidates-analysis.md` shows active progress, grant one extension; second stall = hard cutoff
- **On timeout**: stop with `"fortify: Scientist timed out. Check <FORTIFY_DIR>/ for partial output."`; surface with ⏱

Read `ablation-candidates.jsonl` after scientist completes. If `--max-ablations <M>` specified and component count + 1 (for full variant) exceeds M: sort by `expected_importance` (HIGH first, then MEDIUM, then LOW), keep top M-1 components plus always include the `full` sanity-check variant.

**`--skip-run` early exit**: if `--skip-run` flag present, print candidate table (component_id, name, description, files, expected_importance) and exit. No ablation execution. Print: `"fortify: --skip-run — <N> candidates identified. Next: /research:fortify without --skip-run"`. Jump to F8 (skip-run variant).

## Step F3: Generate ablation variants

For each component from F2, there is one ablation variant: `no-<component-name>` (slugified — lowercase, spaces replaced with hyphens). Plus one `full` variant (sanity check — should reproduce `best_metric`).

Write variant configs to `$FORTIFY_DIR/variants.jsonl` via Write tool — one JSON line per variant:

```json
{"variant_name": "full", "component_removed": null, "revert_commits": [], "revert_strategy": "none"}
{"variant_name": "no-<name>", "component_removed": "<name>", "revert_commits": ["<sha1>", "<sha2>"], "revert_strategy": "git-revert"}
```

## Step F4: Run ablation variants via worktrees

Run each variant **sequentially** to avoid git worktree conflicts.

**Before loop — store original working directory:**

```bash
ORIG_DIR="$(pwd)"  # timeout: 3000
```

**On interrupt** (user abort or unexpected error mid-loop): `cd "$ORIG_DIR"` first, then run `git worktree prune` (`timeout: 15000`) to clean up any partially created worktrees before exiting.

For each variant in `variants.jsonl`:

**4a. Create isolated worktree at best_commit:**

```bash
git worktree add "$WORKTREE_BASE/<variant_name>" <best_commit>  # timeout: 15000
```

**4b. Navigate into worktree** (two separate Bash calls — cd first, then command):

```bash
cd "$WORKTREE_BASE/<variant_name>"  # timeout: 3000
```

**4c. Apply revert (skip for `full` variant):**

For `full` variant: no changes — proceed directly to 4d.

For `no-<component>` variant: revert the component's commits:

```bash
git revert <commit1> <commit2> --no-edit  # timeout: 15000
```

If revert produces merge conflicts: append `{"variant":"<name>","status":"revert-conflict",...}` to `results.jsonl`, jump to 4f (cleanup).

**4d. Run metric_cmd in worktree:**

```bash
<metric_cmd>  # timeout: 360000
```

Parse stdout for numeric metric value. If command fails or no numeric output: record `status: "metric-failed"`, jump to 4f.

**4e. Run guard_cmd in worktree:**

```bash
<guard_cmd>  # timeout: 360000
```

Record guard result: `"pass"` (exit 0) or `"fail"` (non-zero).

**4f. Cleanup worktree (INVARIANT — must execute even if 4c/4d/4e fail):**

```bash
cd "$ORIG_DIR"  # timeout: 3000
```

```bash
git worktree remove --force "$WORKTREE_BASE/<variant_name>"  # timeout: 15000
```

**4g. Record result** — append one JSON line to `$FORTIFY_DIR/results.jsonl`:

```json
{"variant":"<name>","component_removed":"<name or null>","metric":0.0,"delta_from_full":0.0,"delta_pct":0.0,"guard":"pass|fail","status":"completed|revert-conflict|metric-failed|timeout","timestamp":"<ISO>"}
```

`delta_from_full` and `delta_pct` are placeholders here — computed in post-loop step below.

After all variants processed:

```bash
git worktree prune  # timeout: 15000
```

**Post-loop delta computation**: read `results.jsonl`, find `full` variant metric. For each completed `no-<component>` variant:

- `delta_from_full = ablated_metric - full_metric`
- `delta_pct = (delta_from_full / abs(full_metric)) * 100` (signed — negative means removing the component hurt)

Update `results.jsonl` with computed deltas via Write tool (rewrite full file).

## Step F5: Rank component importance

For each `no-<component>` variant with `status: "completed"`:

- `importance = abs(full_metric - ablated_metric) / abs(full_metric) * 100` (percentage of full metric lost by removing this component)
- Importance class:
  - **CRITICAL**: importance > 50%
  - **SIGNIFICANT**: importance 10–50%
  - **MARGINAL**: importance < 10%

Sort by importance descending. Write ranked results to `$FORTIFY_DIR/importance-ranking.json` via Write tool — JSON array of objects with fields: `rank`, `component`, `full_metric`, `ablated_metric`, `importance_pct`, `class`.

**Sanity check**: compare `full` variant metric against `best_metric` from `state.json`. If divergence exceeds 2%:

```text
Warning: Sanity check failed: full-variant metric=<X> differs from best_metric=<Y> by <Z>%. Results may be unreliable (non-deterministic metric or environment change).
```

Include this warning prominently in the F7 report.

## Step F6: Reviewer Q&A (optional — `--venue` only)

Skip entirely if no `--venue` flag. Supported venues: `CVPR`, `NeurIPS`, `ICML`, `workshop`.

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")` with health monitoring (same 15-min cutoff, one 5-min extension):

```markdown
Act as a peer reviewer for <venue>.

Read:
- ablation results at <FORTIFY_DIR>/results.jsonl
- importance ranking at <FORTIFY_DIR>/importance-ranking.json
- original program.md at <path>

Generate:
1. 5–7 likely reviewer questions calibrated to <venue> standards
   (CVPR/NeurIPS/ICML: expect thorough ablations, statistical significance, compute budget justification; workshop: lighter bar)
2. For each question: a data-backed answer referencing specific ablation results
3. A supplementary material draft section with the ablation table (LaTeX-ready)

Write to <FORTIFY_DIR>/reviewer-qa.md.
Include ## Confidence block.
Return ONLY: {"status":"done","questions":N,"file":"<FORTIFY_DIR>/reviewer-qa.md","confidence":0.N}
```

**Health monitoring**: same as F2 (15-min cutoff, one extension). On timeout: note `"Reviewer Q&A: timed out"` in report, continue to F7.

## Step F7: Write fortify report

Pre-compute branch (if not already set):

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
```

Write full report to `.temp/output-fortify-$BRANCH-$(date +%Y-%m-%d).md` via Write tool (never overwrite — append counter suffix if slug exists, e.g. `-2.md`):

```markdown
## Fortify Report: <goal>

**Source run**: <run-id>
**Date**: <date>
**Baseline commit**: <best_commit>
**Components identified**: <N>
**Ablations run**: <N completed> of <N+1 planned>

### Sanity Check (full variant)
Full metric: <value> (expected from run: <best_metric>) — PASS | Warning MISMATCH (<Z>% divergence)

### Component Importance Ranking

| Rank | Component | Full Metric | Ablated Metric | Delta | Importance | Class |
|------|-----------|-------------|----------------|-------|------------|-------|
| 1    | ...       | ...         | ...            | ...   | X.X%       | CRITICAL |

### Ablation Matrix

| Variant       | Metric | Guard | Status           | Delta from Full |
|---------------|--------|-------|------------------|-----------------|
| full          | ...    | pass  | completed        | baseline        |
| no-component1 | ...    | pass  | completed        | -X.X%           |
| no-component2 | ...    | n/a   | revert-conflict  | n/a             |

### Skipped Variants
<list any revert-conflict, metric-failed, or timeout variants with reason>

### Reviewer Q&A
<section from F6 if --venue was specified; otherwise omit this section entirely>

Full artifacts: <FORTIFY_DIR>/

## Confidence
**Score**: 0.N — [high|moderate|low]
**Gaps**:
- [specific limitation]
```

## Step F8: Terminal summary

Print compact terminal summary:

```text
---
Fortify — <goal>
Source run:   <run-id>
Sanity:       full=<value> (expected <best_metric>) — PASS | Warning MISMATCH
Components:   <N> identified · <N> ablations completed
Top:          <component-name> (importance: X.X% · CRITICAL|SIGNIFICANT|MARGINAL)
Marginal:     <N> components < 10% each
Venue Q&A:    generated for <venue> | n/a
-> saved to .temp/output-fortify-<branch>-<date>.md
-> ablation artifacts: <FORTIFY_DIR>/
---
Next: simplify model by removing marginal components, re-run /research:run
```

If `--skip-run` was used (early exit at F2): replace ablation lines with:

```text
---
Fortify — <goal> (--skip-run)
Source run:   <run-id>
Components:   <N> candidates identified — ablations not executed
-> candidates: <FORTIFY_DIR>/ablation-candidates.jsonl
-> analysis:   <FORTIFY_DIR>/candidates-analysis.md
---
Next: run /research:fortify without --skip-run to execute ablations
```

## Notes

- **Worktree invariant** — cleanup (`git worktree remove --force`) must execute even if metric/guard fails. Never leave stale worktrees. Final `git worktree prune` catches any missed cleanup.
- **Main repo never modified** — all ablation work happens in worktrees. Main working tree stays clean throughout.
- **Sequential execution** — variants run one at a time. Parallel worktrees would require separate detached HEADs and complicate cleanup.
- **No compound Bash commands** — always two separate Bash calls (cd then command). CWD persists between calls.
- **Bash tool `timeout` parameter** — never shell `timeout` wrapper. Pass `timeout: <ms>` on Bash tool call.
- **Judge prerequisite** — fortify refuses to run without an APPROVED judge verdict. This prevents ablation studies on unapproved methodologies.
- **`--skip-run` for planning** — generates candidate list without running ablations. Useful for reviewing what would be ablated before committing compute.
- **`--skip-run` scope**: this flag skips ablation *execution* only — the source run (`research:run`) must already be complete before invoking fortify with `--skip-run`. It does not affect the source run.
- **Fortify run directories** don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (exempt per `.claude/rules/artifact-lifecycle.md` TTL policy — no `result.jsonl` = cleanup skipped); remove manually when no longer needed (`rm -rf .experiments/fortify-*/`)
- **Compute passthrough** — `--compute` and `--colab` flags pass through to metric_cmd/guard_cmd execution. Docker and Colab routing follow the same conventions as `/research:run` Phases 5–6.
- **Revert conflicts expected** — when commits are interleaved (component A's commit touches same lines as component B's), revert may conflict. This is recorded as `revert-conflict` and reported, not treated as an error.

</workflow>
