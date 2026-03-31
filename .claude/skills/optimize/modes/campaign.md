# Campaign Mode

______________________________________________________________________

## Plan Mode (Steps C-P1–C-P3)

Triggered by `plan <goal>`. Interactive wizard to configure a campaign run.

**Task tracking**: create tasks for C-P1, C-P2, C-P3 at start.

### Step C-P1: Parse and scan

Parse `<goal>` from arguments. Scan the codebase to detect:

- Language and framework (Python, PyTorch, pytest, etc.)
- Available test runners or benchmark scripts
- Candidate metric commands (pytest coverage, benchmark scripts, eval scripts)
- Candidate guard commands (test suite, lint, type check)
- Files relevant to the goal (scope files)

### Step C-P2: Present proposed config

Present the proposed config as a code block for user review. Include:

```
metric_cmd:      [command that prints a single numeric result]
metric_direction: higher | lower
guard_cmd:       [command that must pass (exit 0) on every kept commit]
max_iterations:  [default 20]
agent_strategy:  [auto | perf | code | ml | arch]
scope_files:     [files the ideation agent may modify]
compute:         local | colab
```

Dry-run both commands before presenting. If either fails, flag the error and propose corrections. Do not proceed to C-P3 until the user confirms or edits the config.

### Step C-P3: Write program.md

Determine the output path: if the user provided a second argument after `<goal>`, use that path; otherwise use `program.md` at the project root.

**Overwrite check**: if the output path already exists, print a one-line warning and use `AskUserQuestion` to ask: (a) Overwrite — proceed; (b) Abort — stop. No silent overwrite.

Write the file using this canonical template, pre-populated from the wizard's findings:

````markdown
# Campaign: <title from goal>

## Goal
<one-paragraph description of what to improve and why>

## Metric
```
command: <metric_cmd from wizard>
direction: higher | lower
target: <optional numeric goal — campaign stops when crossed>
```

## Guard
```
command: <guard_cmd from wizard>
```

## Config
```
max_iterations: 20
agent_strategy: auto | perf | code | ml | arch
scope_files:
  - <path or glob>
compute: local | colab
```

## Notes
<optional free-form text — strategy hints, context, known constraints — ignored by the skill>
````

Print:

```
✓ Program saved to program.md
Run /optimize campaign program.md to start the iteration loop.
```

______________________________________________________________________

## Default Mode (Steps C1–C7)

Triggered by `campaign <goal>` or `campaign <file.md>`.

**Task tracking**: create tasks for steps C1–C7 at start.

### Step C1: Load / build config

**Auto-detect**: if the first non-flag argument ends in `.md`, treat it as a program file path and parse it. Otherwise, treat it as a text goal (existing behavior).

**If argument is a `.md` file** — read and parse with these rules:

1. Find each `## <Section>` heading (case-insensitive).
2. Extract the first fenced code block following that heading.
3. Parse the block as `key: value` lines; multi-value fields use indented `  - value` list items. Path values containing spaces must be wrapped in double quotes.
4. Missing required fields (`command` under `## Metric` and `## Guard`) → stop with a clear error.
5. `agent_strategy: auto` (or omitted) → apply keyword heuristics from `<constants>` in SKILL.md to `## Goal` text and metric command.
6. `target` under `## Metric`: `direction: higher` → stop when metric ≥ target; `direction: lower` → stop when metric ≤ target. If `target` is omitted, run until `max_iterations`.
7. Unrecognized keys and section headings → warn once, then ignore.
8. `## Notes` and the `# Campaign:` title are never parsed — human-only.

**If argument is text** — attempt auto-detection of `metric_cmd` and `guard_cmd` from the goal string and codebase scan (same logic as Plan C-P1, but non-interactive — infer reasonable defaults). `config.json` is not read.

Generate a `run-id` = `$(date +%Y%m%d-%H%M%S)`. Create the run directory:

```
.claude/state/optimize/<run-id>/
  state.json      ← iteration count, best metric, status
  experiments.jsonl  ← one line per iteration
```

Write initial `state.json` (`program_file` is the absolute path to the `.md` file, or `null` when launching from a text goal):

```json
{
  "run_id": "<run-id>",
  "goal": "<goal>",
  "config": {},
  "program_file": "<absolute path to program.md, or null>",
  "iteration": 0,
  "best_metric": null,
  "best_commit": null,
  "status": "running",
  "started_at": "<ISO timestamp>"
}
```

### Step C2: Precondition checks

Run all checks before touching code. Fail fast with a clear message if any fail:

1. **Clean git**: `git status --porcelain` → must be empty. If dirty: print the dirty files and stop.
2. **Not detached HEAD**: `git rev-parse --abbrev-ref HEAD` → must not be `HEAD`.
3. **Metric command produces numeric output**: run `metric_cmd` once; parse stdout for a float. If no float found: show the output and stop.
4. **Guard command passes**: run `guard_cmd` once; must exit 0. If it fails: show the output and stop.
5. **`--colab` check** (if flag present): verify Colab MCP tools are available by checking for `mcp__colab-mcp__runtime_execute_code`. If unavailable, print setup instructions (see Colab MCP section) and stop.
6. **`--codex` check** (if flag present): verify `claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'`. If unavailable: print "⚠ codex plugin not found — proceeding without Codex ideation (Claude-only mode)" and clear the `--codex` flag for this run. Graceful degradation — not a hard stop.

### Step C3: Select ideation agent

Apply the `agent_strategy` mapping from `<constants>` in SKILL.md. If `auto`, apply keyword heuristics to `metric_cmd`. Log selected agent to `state.json`.

### Step C4: Establish baseline (iteration 0)

Run `metric_cmd` and `guard_cmd`. Parse the metric value. Append to `experiments.jsonl`:

```json
{
  "iteration": 0,
  "commit": "<HEAD sha>",
  "metric": 0.0,
  "delta": 0.0,
  "guard": "pass",
  "status": "baseline",
  "description": "baseline",
  "agent": null,
  "confidence": null,
  "timestamp": "<ISO>",
  "files": []
}
```

Update `state.json`: `best_metric = <baseline>`, `best_commit = <HEAD sha>`.

Print: `Baseline: <metric_cmd key> = <value>`. Then proceed to Step C5.

### Step C5: Iteration loop

For each iteration `i` from 1 to `max_iterations`:

**Phase overview** (all phases run per iteration):

| Phase | Name            | Trigger / description                                                                                |
| ----- | --------------- | ---------------------------------------------------------------------------------------------------- |
| 1     | Review          | Always — build compact context from git log, JSONL history, and recent diff                          |
| 2     | Ideate          | Always — spawn specialist agent to propose and implement ONE atomic change                           |
| 3     | Verify files    | Always — check `git diff --stat`; skip to Phase 8 if no files changed (no-op)                        |
| 4     | Commit          | Always — stage modified files and commit before verifying metric                                     |
| 5     | Verify metric   | Always — run `metric_cmd` with timeout; revert on timeout                                            |
| 6     | Guard           | Always — run `guard_cmd`; record pass or fail                                                        |
| 7     | Decide          | Always — keep, rework, or revert based on metric + guard result                                      |
| 8     | Log             | Always — append JSONL record and update `state.json`                                                 |
| 9     | Progress checks | Always — summary every SUMMARY_INTERVAL, stuck detection, diminishing-returns warn, early-stop check |

#### Phase 1 — Review

Build context for the ideation agent and write it to a file — do NOT accumulate this inline in the main context:

```bash
# Collect signals
git log --oneline -10 > .claude/state/optimize/<run-id>/context-<i>.md
tail -10 .claude/state/optimize/<run-id>/experiments.jsonl >> .claude/state/optimize/<run-id>/context-<i>.md
git diff --stat HEAD~5 HEAD >> .claude/state/optimize/<run-id>/context-<i>.md
```

Prepend a header block to `context-<i>.md` with: goal, current metric vs baseline, delta trend (last 5 kept deltas), and the iteration number. The ideation agent in Phase 2 reads this file directly — the content is never echoed back to the main context.

#### Phase 2 — Ideate

Spawn the selected specialist agent with this prompt (adapt as needed):

```
Goal: <goal>
Current metric: <metric_cmd key> = <current value> (baseline: <baseline>, direction: <higher|lower>)
Experiment history: read `.claude/state/optimize/<run-id>/context-<i>.md` for the full context block.
Scope files (read and modify only these): <scope_files>

Read `context-<i>.md` and the scope files. Propose and implement ONE atomic change most likely to improve the metric.
The change must not break <guard_cmd>.
Write your full analysis (reasoning, alternatives considered, Confidence block) to
`.claude/state/optimize/<run-id>/ideation-<i>.md` using the Write tool.
Return ONLY the JSON result line — nothing else after it:
{"description":"...","files_modified":[...],"confidence":0.N}
```

For `--colab` runs: the ideation agent (especially `ai-researcher`) may call `mcp__colab-mcp__runtime_execute_code` during this phase to prototype GPU code before committing.

<!-- MCP tool call — invoked via MCP protocol, not Bash; requires colab-mcp server enabled in settings.local.json -->

If the Agent tool is unavailable (nested subagent context), implement the change inline and construct the JSON result manually.

#### Phase 2b — Codex co-pilot (`--codex` only)

Run Phase 2b on **every iteration** when `--codex` is active. Claude-first co-pilot — Codex always gets a second turn; keep whichever of the two proposals produces a better net metric improvement (or keep Claude's if Codex produces no additional improvement).

- If Claude's Phase 2 change was **kept**: Codex runs a second pass on the current state — building on Claude's work, trying an additional improvement.
- If Claude's Phase 2 change was **reverted or no-op**: the working tree has already been restored to the pre-Phase-2 state; Codex gets a fresh attempt on the clean tree.

Run Codex ideation:

```
Agent(
  subagent_type="codex:codex-rescue",
  prompt="Goal: <goal>. Current metric: <metric_key>=<current_value> (baseline: <baseline>, direction: <higher|lower>). Scope files: <scope_files>. Read context from .claude/state/optimize/<run-id>/context-<i>.md. Starting state: Claude's change was [kept|reverted|no-op]. [If kept: try to improve further from the current state. If reverted/no-op: propose a fresh approach.] Propose and implement ONE atomic optimization change most likely to improve the metric without breaking <guard_cmd>. Write your full reasoning to .claude/state/optimize/<run-id>/codex-ideation-<i>.md."
)
```

- If Claude's change was **kept** AND Codex proposes additional changes: proceed through Phases 3–7 exactly as for a standard ideation (commit, verify metric, run guard, decide keep/revert). Codex wins only if delta ≥ 0.1% AND guard passes.
- If Claude's change was **kept** AND Codex produces no changes (no-op on top of Claude's kept change): append a `codex-no-op` record and continue — Claude's kept result stands.
- If Claude's change was **reverted or no-op** AND Codex proposes changes: proceed through Phases 3–7 exactly as for a Claude ideation — commit, verify metric, run guard, decide keep/revert.
- If Claude's change was **reverted or no-op** AND Codex returns no file changes: append `status: codex-no-op` to JSONL with `ideation_source: "codex"`, continue loop.
- Set `"ideation_source": "codex"` in the Phase 8 JSONL record for any Codex-proposed change.

**Stuck escalation with `--codex`**: when Phase 9 detects `STUCK_THRESHOLD` consecutive discards and `--codex` is active, increase Codex ideation effort — add this hint to the Codex prompt for the next iteration: "Previous N attempts were all reverted. Focus on a fundamentally different approach (different file, different algorithm, different abstraction)."

#### Phase 3 — Verify files changed

`git diff --stat`. If no files changed (no-op): append to JSONL with `status: no-op`, skip to Phase 8 (log), continue loop.

#### Phase 4 — Commit

Stage only the modified files (never `git add -A`):

```bash
git add <files_modified from agent JSON>
git commit -m "experiment(optimize/i<N>): <description>"
```

If pre-commit hooks fail:

- Delegate to `linting-expert` agent: provide the failing hook output and the modified files; ask it to fix the issues. Max 2 attempts.
- If still failing after 2 attempts: `git restore --staged .` + `git checkout -- .` to clean up, append `status: hook-blocked`, continue loop.

#### Phase 5 — Verify metric

Run `metric_cmd` with timeout:

```bash
timeout <VERIFY_TIMEOUT_SEC> <metric_cmd>
```

For `--colab`: route through `mcp__colab-mcp__runtime_execute_code` instead of local Bash. Parse numeric result from output.

If timeout expires: append `status: timeout`, revert via `git revert HEAD --no-edit`, continue loop.

#### Phase 6 — Guard

Run `guard_cmd` (exit-code check only). Record pass or fail.

#### Phase 7 — Decide

| Condition                                             | Action                                                                                                           |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| metric improved AND guard pass                        | Keep commit. Update `state.json`: `best_metric`, `best_commit`.                                                  |
| metric improved AND guard fail                        | Rework: re-spawn agent with guard failure output. Max `GUARD_REWORK_MAX` (2) attempts. If still failing: revert. |
| metric improved AND gain < 0.1% AND change > 50 lines | Discard (simplicity override): `git revert HEAD --no-edit`.                                                      |
| no improvement                                        | Revert: `git revert HEAD --no-edit`.                                                                             |

`git revert HEAD --no-edit` — never `git reset --hard` (preserves history, not in deny list).

#### Phase 8 — Log

Append one JSONL record to `experiments.jsonl`:

```json
{
  "iteration": 1,
  "commit": "<sha of experiment commit or revert>",
  "metric": 0.0,
  "delta": 0.0,
  "guard": "pass|fail",
  "status": "kept|reverted|rework|no-op|hook-blocked|timeout",
  "description": "<agent description>",
  "agent": "<agent type>",
  "confidence": 0.0,
  "timestamp": "<ISO>",
  "files": [],
  "ideation_source": "claude"
}
```

`ideation_source` is `"claude"` when the Claude specialist agent proposed the change, `"codex"` when Phase 2b (Codex fallback) proposed it.

Update `state.json`: `iteration = i`, `status = running`.

#### Phase 9 — Progress checks

- **Summary every SUMMARY_INTERVAL iterations**: print compact table (iteration, metric, delta, status) for the last N iterations.
- **Stuck detection**: if last `STUCK_THRESHOLD` entries all have `status: reverted|no-op|hook-blocked`, trigger escalation (see `<constants>` in SKILL.md). Log escalation action.
- **Diminishing returns**: if last `DIMINISHING_RETURNS_WINDOW` kept entries each improved < 0.5%, print a warning and suggest stopping. Do not auto-stop — let the user decide.
- **Early stop**: if `target` is set in the program file (or config), stop when the metric crosses it (`direction: higher` → metric ≥ target; `direction: lower` → metric ≤ target). Mark `state.json` `status: goal-achieved`.
- **Context compaction** (every SUMMARY_INTERVAL iterations): write a full iteration summary table to `.claude/state/optimize/<run-id>/progress-<i>.md` and actively discard verbose per-iteration details from working memory. Retain in working memory only: current metric value, iteration count, JSONL file path, and `best_commit`. This prevents linear context growth in long campaigns — full history is always recoverable from `experiments.jsonl` and `ideation-<i>.md` files on disk.

### Step C6: Results report

Pre-compute the branch before writing: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`

Write full report to `_outputs/$(date +%Y)/$(date +%m)/output-optimize-campaign-$BRANCH-$(date +%Y-%m-%d).md` using the Write tool. Do not print the full report to terminal.

**Report structure:**

```markdown
## Campaign Run: <goal>

**Run ID**: <run-id>
**Date**: <date>
**Iterations**: <total> (<kept> kept, <reverted> reverted, <other> other)
**Baseline**: <metric> = <baseline value>
**Best**: <metric> = <best value> (<delta>% improvement)
**Best commit**: <sha>
**Codex co-pilot**: active (ran every iteration) — <N> Codex passes run (omit line if --codex not used)
**Codex wins**: <N> Codex proposals kept vs <N> Claude proposals kept

### Experiment History
| # | Metric | Delta | Status | Description | Agent | Confidence |
|---|--------|-------|--------|-------------|-------|------------|
| ... |

### Summary
[2-3 sentences on what strategies worked, what didn't, what to try next]

### Recommended Follow-ups
- [next action]
```

Print compact terminal summary:

```
---
Campaign — <goal>
Iterations: <total>  Kept: <kept>  Reverted: <reverted>
Baseline:   <metric_key> = <baseline>
Best:       <metric_key> = <best> (<delta>% improvement, commit <sha>)
Agent:      <agent type used>
→ saved to _outputs/YYYY/MM/output-optimize-campaign-<date>.md
---
```

Update `state.json`: `status = completed`.

### Step C7: Codex delegation (optional)

After confirming results, inspect applied changes (`git diff <baseline_commit>...<best_commit> --stat`) and identify tasks Codex can complete (inline comments on non-obvious changes, docstring updates for modified functions, test coverage for the modified path). Read `.claude/skills/_shared/codex-delegation.md` and apply the criteria defined there.

______________________________________________________________________

## Resume Mode

Triggered by `resume` or `resume <file.md>`.

**Locating the run**:

- `resume` (no argument): scan all run dirs in `.claude/state/optimize/`, select the one with the latest `started_at` that has `status: running`.
- `resume <file.md>`: resolve the path to absolute. Scan all run dirs, filter for those whose `state.json` has `"program_file"` matching that absolute path. Pick the one with the latest `started_at`. If no match: stop with a clear error.

1. Read `state.json` from the located run dir.
2. **Re-parse program file**: if `program_file` is non-null, re-read and re-parse that file (Step C1 parsing rules) and update config in memory. This applies any edits the user made between runs. Note: edits to `program.md` while a campaign loop is actively running do not take effect until the next `resume`.
3. **Validate `experiments.jsonl` integrity**: read the last line of `experiments.jsonl` and attempt to parse it as JSON. If the last line is truncated or not valid JSON, warn the user:
   ```
   ⚠ experiments.jsonl last line appears corrupt (truncated or invalid JSON).
   Offer to truncate the corrupt entry (y/n)?
   ```
   If the user confirms, remove the last line before resuming. If they decline, stop and let the user fix it manually.
4. Validate git HEAD: if the current HEAD has diverged from `state.json.best_commit` in an unexpected direction, warn and ask before continuing.
5. Continue the iteration loop from `state.json.iteration + 1`.

______________________________________________________________________

## Team Mode (`--team`)

**When to trigger**: goal spans multiple optimization axes (e.g., "improve training speed" = model architecture + data pipeline + compute efficiency), OR user explicitly passes `--team`.

**Workflow:**

1. Lead completes Steps C1–C4 (config, preconditions, baseline) solo.
2. Lead identifies 2–3 distinct optimization axes from the goal + codebase analysis.
3. Lead defines the run output directory and spawns 2–3 teammates (reasoning agents at `opus` per CLAUDE.md §Agent Teams), each assigned a different axis and a matching ideation agent type. Each teammate runs in an isolated worktree (`isolation: worktree`).

```bash
RUN_DIR="_optimizations/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"
```

Example axis assignment for "reduce training time":

- teammate-A = `ai-researcher` axis: model architecture changes
- teammate-B = `perf-optimizer` axis: data pipeline and GPU utilization
- teammate-C = `sw-engineer` axis: code-level optimizations (batching, caching)

Each teammate's spawn prompt must include:

```
Read .claude/TEAM_PROTOCOL.md and use AgentSpeak v2.
You are a campaign teammate. Your axis: <axis description>.
Ideation agent: <agent type>.
Run 3–5 independent iterations of the Review→Ideate→Modify→Commit→Verify→Guard→Log loop.
Baseline metric: <metric_cmd key> = <baseline>. Direction: <higher|lower>.
Scope files: <scope_files>.
Write your full iteration log (all runs, metrics, reasoning) to `$RUN_DIR/teammate-<axis>.md` using the Write tool before returning.
Return ONLY a compact JSON envelope: {axis, iterations_run, kept, best_metric, best_commit, description}
Call TaskUpdate(in_progress) when starting; TaskUpdate(completed) when done.
```

4. Each teammate runs their iterations independently and reports results.

5. **Consolidation**: after all teammates complete (or reach `TeammateIdle`), spawn a single `general-purpose` consolidator agent. Provide it the file paths of all teammate output files (`$RUN_DIR/teammate-<axis>.md` for each axis). Prompt:

   ```
   Read the teammate output files at the following paths: <paths>.
   Synthesize findings into a single consolidated report: per-axis summary, metric improvements, best commits, and recommended cherry-pick order.
   Write the full consolidated report to `$RUN_DIR/consolidated.md` using the Write tool.
   Return ONLY: {"status":"done","axes_summarized":<N>,"file":"$RUN_DIR/consolidated.md"}
   ```

   Read `$RUN_DIR/consolidated.md` for the cherry-pick plan before proceeding.

   **Context discipline**: the lead MUST NOT inline-read teammate output files directly — the consolidator agent owns reading `teammate-<axis>.md` files and synthesizing them. The lead's context receives only the compact JSON envelope from the consolidator and then reads `consolidated.md` for the cherry-pick plan. This prevents the lead's context from growing by O(axes × iterations).

6. Lead cherry-picks the winning commits from each axis into the main branch, tests for compatibility, runs `guard_cmd`.

7. Lead measures combined metric, resolves conflicts if needed, writes the Step C6 report with per-axis breakdown and combined result.

8. Shutdown teammates.

**Note on CLAUDE.md §8**: team mode uses in-process teammates that send `TeammateIdle` notifications on completion — the file-activity polling protocol does not apply; `TeammateIdle` is the liveness signal.

______________________________________________________________________

## Colab MCP Integration (`--colab`)

**Purpose**: route metric verification and GPU code testing to a Colab notebook runtime instead of local execution. Essential for ML training metrics, CUDA benchmarks, and any workload requiring a GPU.

**Setup** (user must complete before running `--colab`):

1. Add `"colab-mcp"` to `enabledMcpjsonServers` in `settings.local.json`:
   ```json
   {
     "enabledMcpjsonServers": [
       "colab-mcp"
     ]
   }
   ```
2. Ensure `colab-mcp` server is defined in `.mcp.json` under `mcpServers` (see project `.mcp.json`).
3. Open a Colab notebook with the runtime connected and execute the MCP connection cell.

**How it works during a run:**

- Step C2 (preconditions): checks for `mcp__colab-mcp__runtime_execute_code` availability.
- Phase 5 (verify metric): calls `mcp__colab-mcp__runtime_execute_code` with `metric_cmd` instead of local `timeout <cmd>`.
- Phase 2 (ideate): `ai-researcher` agent can call `mcp__colab-mcp__runtime_execute_code` to prototype GPU code before committing.
- `VERIFY_TIMEOUT_SEC` = 300 (vs 120 local) to account for network + GPU startup latency.

If Colab MCP is unavailable at Step C2, print:

```
⚠ Colab MCP not available. To enable:
  1. Add "colab-mcp" to enabledMcpjsonServers in settings.local.json
  2. Open a Colab notebook and connect the runtime
  3. Execute the MCP connection cell in the notebook
Then re-run with --colab.
```

______________________________________________________________________

## Notes

- **Commit before verify** is the foundational pattern — it enables a clean `git revert HEAD` if the metric does not improve. Never verify before committing.
- **`git revert` over `git reset --hard`** — preserves experiment history, is not in the deny list.
- **Never `git add -A`** — always stage specific files returned by the agent JSON.
- **Never `--no-verify`** — if a pre-commit hook blocks, delegate to `linting-expert` and fix.
- **Guard ≠ Verify** — guard checks for regressions (tests, lint); verify checks the target metric. Both must pass to keep a commit.
- **Scope files are read-only for guard/test files** — the ideation agent must not modify test files or the metric/guard scripts themselves.
- **JSONL over TSV** — richer structured fields, `jq`-parseable, no delimiter ambiguity; query with `jq -c 'select(.status == "kept")' experiments.jsonl`.
- **State persistence enables resume** — if the loop crashes or times out, `resume` picks up exactly where it stopped.
- **Safety break**: max iterations default is 20; the skill never exceeds MAX_ITERATIONS without a user override in config.
