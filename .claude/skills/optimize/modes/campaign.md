# Campaign Mode

______________________________________________________________________

## Plan Mode (Steps C-P1–C-P3)

Triggered by `plan <goal>`. Interactive wizard to configure a campaign run.

**Task tracking**: create tasks for C-P1, C-P2, C-P3 at start.

### Step C-P1: Parse and scan

**Scope guard (first action)**: Before scanning, check whether `<goal>` is an optimization goal. If the input is clearly not an optimization goal — e.g., a question about code semantics, a regex or algorithm explanation request, a debugging question, or any prompt that does not describe a measurable improvement target — print:

```
⚠ This input does not look like an optimization goal.
/optimize plan expects: "Reduce X" / "Increase Y" / "Improve Z metric".
Use /ask or /research for explanatory questions.
```

Then stop. Do not proceed to C-P2 or C-P3.

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
compute:         local | colab | docker
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
compute: local | colab | docker
sandbox_network: none | bridge
```

## Notes
<optional free-form text — strategy hints, context, known constraints — ignored by the skill>
````

Print:

```
✓ Program saved to program.md

Next steps:
  /optimize judge program.md   ← validate plan before running (recommended)
  /optimize campaign program.md ← start iteration loop directly
```

______________________________________________________________________

## Default Mode (Steps C1–C7)

Triggered by `campaign <goal>` or `campaign <file.md>`.

**Task tracking**: create tasks for C1–C7 at start. If `--codex` is active, also create task `C5b: Codex co-pilot (iter ?/max)` with status `pending`.

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
.experiments/state/<run-id>/
  state.json         ← iteration count, best metric, status
  experiments.jsonl  ← one line per iteration
  diary.md           ← human-readable research diary (hypothesis → outcome → decision)
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

Print: `Baseline: <metric_cmd key> = <value>`.

Write initial diary header to `.experiments/state/<run-id>/diary.md`:

```markdown
# Research Diary — <goal>

**Run**: <run-id>
**Started**: <ISO timestamp>
**Baseline**: <metric_key> = <baseline value>

---
```

Then proceed to Step C5.

### Step C5: Iteration loop

For each iteration `i` from 1 to `max_iterations`:

**Phase overview** (all phases run per iteration):

| Phase | Name             | Trigger / description                                                                                                        |
| ----- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 0     | Print header     | Always — print `[→ Iter N/max · starting]`; TaskUpdate C5 subject with current iteration                                     |
| 1     | Build context    | Always — build compact context from git log, JSONL history, and recent diff                                                  |
| 2     | Propose change   | Always — spawn specialist agent to read code, research, investigate, and generate a hypothesis with optional sandbox scripts |
| 2a    | Sandbox validate | `compute: docker` only — run agent's exploratory scripts in Docker sandbox (read-only mount); skip if sandbox unavailable    |
| 2b    | Apply change     | Always — agent applies the (validated) proposal to real codebase using Write/Edit tools only; no Bash on codebase            |
| 2c    | Codex co-pilot   | `--codex` only — **MANDATORY every iteration**; Codex second pass after Phase 2b; must not be skipped                        |
| 3     | Verify files     | Always — check `git diff --stat`; skip to Phase 8 if no files changed (no-op)                                                |
| 4     | Commit change    | Always — stage modified files and commit before verifying metric                                                             |
| 5     | Verify metric    | Always — run `metric_cmd` via `compute` mode (local/colab/docker); revert on timeout                                         |
| 6     | Run guard        | Always — run `guard_cmd` via `compute` mode; record pass or fail                                                             |
| 7     | Evaluate outcome | Always — keep, rework, or revert based on metric + guard result                                                              |
| 7a    | Write diary      | Always — append one structured entry to `diary.md` recording hypothesis, outcome, and decision rationale                     |
| 8     | Write log        | Always — append JSONL record, update `state.json`, print iteration summary, TaskUpdate C5 with result                        |
| 9     | Progress checks  | Always — summary every SUMMARY_INTERVAL, stuck detection, diminishing-returns warn, early-stop check                         |

> **Phase 2a and 2b are Track 2 (Docker sandbox) features**. Until Track 2 is implemented, Phase 2 continues to propose AND implement in one step as before. The table above shows the target architecture.

**Command execution rules** (apply to ALL phases that run external commands):

1. **No compound commands**: Never `cd /path && command`. Always two separate Bash calls — CWD persists between calls.
2. **Use Bash tool `timeout` parameter**: Never the `timeout` shell command wrapper. Pass `timeout: <ms>` on the Bash tool call itself.
3. **No inline multi-line Python**: For Python logic longer than 3 lines, always write the script to `.experiments/state/<run-id>/scripts/script-<i>.py` using the Write tool, then execute with `python3 <path>` or `uv run python <path>`. Two triggers that Claude Code always flags regardless of allow list: (a) patterns like `=([0-9.]+)` inside `-c "..."` (false Zsh substitution match); (b) multi-line `-c "..."` containing `#`-prefixed comment lines ("quoted newline followed by #-prefixed line" safety check). Writing to a file sidesteps both.
4. **No Zsh constructs**: Never use `=()`, `<()`, `>()` in Bash commands — even inside quoted strings; Claude Code scans raw command text.
5. **Local exploratory scripts that write to real files** (e.g., scanning config combos, patching JSON, running experiments with temp overrides): write to `.experiments/state/<run-id>/scripts/`, execute locally with `python3 <path>`. These scripts legitimately modify project files and must run locally — NOT in Docker sandbox.
6. **Docker sandbox** (when available — see Phase 2a): Phases 4–6 route `metric_cmd`/`guard_cmd` through Docker when `compute: docker`. Phase 2a runs read-only hypothesis scripts in sandbox. Scripts that write to project files always run locally regardless of compute mode.
7. **One change per iteration, no batch loops**: Never write a script that iterates over multiple config variants, parameter combos, or implementations in a single Bash/Python call. Each variant is one campaign iteration — the loop, measurement, and comparison are the campaign framework's job, not the ideation agent's.

#### Phase 0 — Print header

Before any phase work, print the iteration header and update the C5 task subject:

```
[→ Iter N/max_iterations — best so far: <best_metric> (Δ<best_delta_pct>% vs baseline)]
```

TaskUpdate C5 subject: `C5: Iteration N/max_iterations — running`

#### Phase 1 — Build context

Build context for the ideation agent and write it to a file — do NOT accumulate this inline in the main context:

```bash
# Collect signals
git log --oneline -10 > .experiments/state/<run-id>/context-<i>.md
tail -10 .experiments/state/<run-id>/experiments.jsonl >> .experiments/state/<run-id>/context-<i>.md
git diff --stat HEAD~5 HEAD >> .experiments/state/<run-id>/context-<i>.md
```

Prepend a header block to `context-<i>.md` with: goal, current metric vs baseline, delta trend (last 5 kept deltas), and the iteration number. The ideation agent in Phase 2 reads this file directly — the content is never echoed back to the main context.

#### Phase 2 — Propose change

Spawn the selected specialist agent with `maxTurns: 15` and this prompt (adapt as needed):

```
Goal: <goal>
Current metric: <metric_cmd key> = <current value> (baseline: <baseline>, direction: <higher|lower>)
Experiment history: read `.experiments/state/<run-id>/context-<i>.md` for the full context block.
Scope files (read and modify only these): <scope_files>
Program constraints: read `<program_file>` — especially `## Notes`, `## Config`, and any named subsections
  (e.g., "Hard boundaries", "Optuna's role", "What the agent is free to change"). These take precedence
  over general campaign rules. If program_file is null, skip this step.

Read `context-<i>.md`, the scope files, and the program constraints. Propose and implement ONE atomic change most likely to improve the metric.
The change must not break <guard_cmd>.
ONE means ONE: do NOT write a script that loops over multiple config variants or implementations to find the best one —
each variant is a separate iteration; the campaign loop handles comparison and selection.
Pick the single most promising hypothesis and apply it directly to the source files.
Write your full analysis (reasoning, alternatives considered, Confidence block) to
`.experiments/state/<run-id>/ideation-<i>.md` using the Write tool.
Return ONLY the JSON result line — nothing else after it:
{"description":"...","files_modified":[...],"confidence":0.N}
```

For `--colab` runs: the ideation agent (especially `ai-researcher`) may call `mcp__colab-mcp__runtime_execute_code` during this phase to prototype GPU code before committing.

<!-- MCP tool call — invoked via MCP protocol, not Bash; requires colab-mcp server enabled in settings.local.json -->

If the Agent tool is unavailable (nested subagent context), implement the change inline and construct the JSON result manually.

#### Phase 2c — Codex co-pilot (`--codex` only)

> **MANDATORY — do not skip.** When `--codex` was active at Step C2 and not cleared, this phase MUST run on every iteration regardless of Phase 2 outcome. Print the narration line and update the C5b task before calling Agent.

Print:

```
[→ Iter N/max · Phase 2c: Codex co-pilot — running]
```

TaskUpdate C5b subject: `C5b: Codex co-pilot — iter N/max_iterations running`, status: `in_progress`

Run Phase 2c on **every iteration** when `--codex` is active. Claude-first co-pilot — Codex always gets a second turn; keep whichever of the two proposals produces a better net metric improvement (or keep Claude's if Codex produces no additional improvement).

- If Claude's Phase 2 change was **kept**: Codex runs a second pass on the current state — building on Claude's work, trying an additional improvement.
- If Claude's Phase 2 change was **reverted or no-op**: the working tree has already been restored to the pre-Phase-2 state; Codex gets a fresh attempt on the clean tree.

Run Codex ideation:

```
Agent(
  subagent_type="codex:codex-rescue",
  prompt="Goal: <goal>. Current metric: <metric_key>=<current_value> (baseline: <baseline>, direction: <higher|lower>). Scope files: <scope_files>. Read context from .experiments/state/<run-id>/context-<i>.md. Starting state: Claude's change was [kept|reverted|no-op]. [If kept: try to improve further from the current state. If reverted/no-op: propose a fresh approach.] Propose and implement ONE atomic optimization change most likely to improve the metric without breaking <guard_cmd>. Write your full reasoning to .experiments/state/<run-id>/codex-ideation-<i>.md."
)
```

- If Claude's change was **kept** AND Codex proposes additional changes: proceed through Phases 3–7 exactly as for a standard ideation (commit, verify metric, run guard, decide keep/revert). Codex wins only if delta ≥ 0.1% AND guard passes.
- If Claude's change was **kept** AND Codex produces no changes (no-op on top of Claude's kept change): append a `codex-no-op` record and continue — Claude's kept result stands.
- If Claude's change was **reverted or no-op** AND Codex proposes changes: proceed through Phases 3–7 exactly as for a Claude ideation — commit, verify metric, run guard, decide keep/revert.
- If Claude's change was **reverted or no-op** AND Codex returns no file changes: append `status: codex-no-op` to JSONL with `ideation_source: "codex"`, continue loop.
- Set `"ideation_source": "codex"` in the Phase 8 JSONL record for any Codex-proposed change.

After Codex completes (any outcome — kept, reverted, no-op):

TaskUpdate C5b subject: `C5b: Codex co-pilot — iter N done (<outcome>)`

**Stuck escalation with `--codex`**: when Phase 9 detects `STUCK_THRESHOLD` consecutive discards and `--codex` is active, increase Codex ideation effort — add this hint to the Codex prompt for the next iteration: "Previous N attempts were all reverted. Focus on a fundamentally different approach (different file, different algorithm, different abstraction)."

#### Phase 3 — Verify files changed

`git diff --stat`. If no files changed (no-op): append to JSONL with `status: no-op`, skip to Phase 8 (log), continue loop.

#### Phase 4 — Commit change

Stage only the modified files (never `git add -A`):

```bash
git add <files_modified from agent JSON>
git commit -m "experiment(optimize/i<N>): <description>"
```

If pre-commit hooks fail:

- Delegate to `linting-expert` agent: provide the failing hook output and the modified files; ask it to fix the issues. Max 2 attempts.
- If still failing after 2 attempts: `git restore --staged .` + `git checkout -- .` to clean up, append `status: hook-blocked`, continue loop.

#### Phase 5 — Verify metric

**If `sandbox_mode = "docker"`** (Phase 2a set this):

```bash
docker run --rm --network <sandbox_network> \
  -v "$(pwd):/workspace:ro" \
  -v "$(pwd)/.experiments:/workspace/.experiments:rw" \
  --tmpfs /tmp:rw,size=256m \
  campaign-<run-id> \
  sh -c '<metric_cmd>'
```

No resource limits — the container may use all available CPU and memory. Use the Bash tool `timeout` parameter (not the `timeout` command): `timeout: <VERIFY_TIMEOUT_SEC * 1000>`.

**If `sandbox_mode = "local"`** (Docker unavailable or `--sandbox=local`):
Run `metric_cmd` directly using the Bash tool with `timeout: <VERIFY_TIMEOUT_SEC * 1000>` (milliseconds). Do NOT use the `timeout` shell command wrapper.
If the command requires a different working directory, issue a separate `cd <path>` Bash call first (CWD persists).
If metric parsing requires complex Python logic (regex, JSON), write a parser script to `.experiments/state/<run-id>/scripts/parse-metric-<i>.py` using the Write tool and execute it with `python3 <path>` instead of an inline `python3 -c "..."` one-liner.
If the local command triggers a user approval prompt, include in the approval description: "⚠ Docker sandbox unavailable — running metric_cmd uncontained. Start Docker Desktop to enable sandboxed execution."

**If `--colab` active**: `--colab` and `--sandbox` are mutually exclusive. Colab routes all execution through the remote GPU runtime via `mcp__colab-mcp__runtime_execute_code`; Docker sandbox is not used. If both flags are present, `--colab` takes precedence and `sandbox_mode` is ignored for Phases 5/6.

If timeout expires: append `status: timeout`, revert via `git revert HEAD --no-edit`, continue loop.

#### Phase 6 — Run guard

**If `sandbox_mode = "docker"`**: run `guard_cmd` inside the same Docker container as Phase 5 (same flags: `--network <sandbox_network>`, `:ro` project mount, `:rw` `.experiments/` mount, `--tmpfs /tmp`; no resource limits). Check exit code only. `--colab` flag: same mutual-exclusion rule as Phase 5 — colab takes precedence, sandbox is ignored.

**If `sandbox_mode = "local"`**: run `guard_cmd` directly. If it triggers a user approval prompt, include: "⚠ Docker sandbox unavailable — running guard_cmd uncontained. Start Docker Desktop to enable sandboxed execution."

Record pass (exit 0) or fail (non-zero).

#### Phase 7 — Evaluate outcome

| Condition                                             | Action                                                                                                           |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| metric improved AND guard pass                        | Keep commit. Update `state.json`: `best_metric`, `best_commit`.                                                  |
| metric improved AND guard fail                        | Rework: re-spawn agent with guard failure output. Max `GUARD_REWORK_MAX` (2) attempts. If still failing: revert. |
| metric improved AND gain < 0.1% AND change > 50 lines | Discard (simplicity override): `git revert HEAD --no-edit`.                                                      |
| no improvement                                        | Revert: `git revert HEAD --no-edit`.                                                                             |

`git revert HEAD --no-edit` — never `git reset --hard` (preserves history, not in deny list).

#### Phase 7a — Write diary

After the Phase 7 decision, append one entry to `diary.md`:

```markdown
## Iteration N — <ISO timestamp>

**Hypothesis**: <agent's description from Phase 2 JSON — the proposed change and expected improvement>

**Outcome**: <metric_key> = <value> (Δ<delta>% vs baseline) — <kept|reverted|rework|no-op|hook-blocked|timeout>

**Decision**: <one sentence: why the outcome was accepted or rejected — e.g. "Metric improved 1.2% with guard passing" or "Reverted: metric regressed by 0.5%" or "Guard failed after 2 rework attempts">

---
```

For `no-op` iterations (agent made no file changes), write:

```markdown
## Iteration N — <ISO timestamp>

**Hypothesis**: <description> — no files modified

**Outcome**: no-op

**Decision**: Skipped (no changes made)

---
```

#### Phase 8 — Write log

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

`ideation_source` is `"claude"` when the Claude specialist agent proposed the change, `"codex"` when Phase 2c (Codex fallback) proposed it.

Update `state.json`: `iteration = i`, `status = running`.

Print iteration summary:

```
[✓ Iter N/max — <kept|reverted|no-op|...> · metric=<value> (Δ<delta>%) · agent=<agent_type>]
```

TaskUpdate C5 subject: `C5: Iter N/max — last: <status>, best: <best_metric>`

#### Phase 9 — Progress checks

- **Summary every SUMMARY_INTERVAL iterations**: print compact table (iteration, metric, delta, status) for the last N iterations.
- **Stuck detection**: if last `STUCK_THRESHOLD` entries all have `status: reverted|no-op|hook-blocked`, trigger escalation (see `<constants>` in SKILL.md). Log escalation action.
- **Diminishing returns**: if last `DIMINISHING_RETURNS_WINDOW` kept entries each improved < 0.5%, print a warning and suggest stopping. Do not auto-stop — let the user decide.
- **Early stop**: if `target` is set in the program file (or config), stop when the metric crosses it (`direction: higher` → metric ≥ target; `direction: lower` → metric ≤ target). Mark `state.json` `status: goal-achieved`.
- **Context compaction** (every SUMMARY_INTERVAL iterations): write a full iteration summary table to `.experiments/state/<run-id>/progress-<i>.md` and actively discard verbose per-iteration details from working memory. Retain in working memory only: current metric value, iteration count, JSONL file path, and `best_commit`. This prevents linear context growth in long campaigns — full history is always recoverable from `experiments.jsonl` and `ideation-<i>.md` files on disk.

### Step C6: Results report

Pre-compute the branch before writing: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`

Write full report to `.temp/output-optimize-campaign-$BRANCH-$(date +%Y-%m-%d).md` using the Write tool. Do not print the full report to terminal.

**Report structure:**

```markdown
## Campaign Run: <goal>

**Run ID**: <run-id>
**Date**: <date>
**Iterations**: <total> (<kept> kept, <reverted> reverted, <other> other)
**Baseline**: <metric> = <baseline value>
**Best**: <metric> = <best value> (<delta>% improvement)
**Best commit**: <sha>
**Diary**: ".experiments/state/<run-id>/diary.md"
**Codex co-pilot**: active (ran every iteration) — <N> Codex passes run (omit line if --codex not used)
**Codex wins**: <N> Codex proposals kept vs <N> Claude proposals kept

### Experiment History

| #   | Metric | Delta  | Status   | Description | Agent | Confidence |
| --- | ------ | ------ | -------- | ----------- | ----- | ---------- |
| N   | value  | +X.X%  | status   | desc        | agent | 0.N        |

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
→ saved to .temp/output-optimize-campaign-<date>.md
→ diary: .experiments/state/<run-id>/diary.md
---
```

Update `state.json`: `status = completed`.

### Step C7: Codex delegation (optional)

After confirming results, inspect applied changes (`git diff <baseline_commit>...<best_commit> --stat`) and identify tasks Codex can complete (inline comments on non-obvious changes, docstring updates for modified functions, test coverage for the modified path). Read `.claude/skills/_shared/codex-delegation.md` and apply the criteria defined there.

______________________________________________________________________

## Resume Mode

Triggered by `resume` or `resume <file.md>`.

**Locating the run**:

- `resume` (no argument): scan all run dirs in `.experiments/state/`, select the one with the latest `started_at` that has `status: running`.
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
5. Continue the iteration loop from `state.json.iteration + 1`. The `diary.md` file is NOT re-initialized on resume — iteration entries continue appending to the existing file.

______________________________________________________________________

## Team Mode (`--team`)

**When to trigger**: goal spans multiple optimization axes (e.g., "improve training speed" = model architecture + data pipeline + compute efficiency), OR user explicitly passes `--team`.

**Workflow:**

1. Lead completes Steps C1–C4 (config, preconditions, baseline) solo.

2. Lead identifies 2–3 distinct optimization axes from the goal + codebase analysis.

3. Lead defines the run output directory and spawns 2–3 teammates (reasoning agents at `opus` per CLAUDE.md §Agent Teams), each assigned a different axis and a matching ideation agent type. Each teammate runs in an isolated worktree (`isolation: worktree`).

   ```bash
   RUN_DIR=".experiments/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
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

5. **Consolidation**: after all teammates complete (or reach `TeammateIdle`), spawn a single `sw-engineer` consolidator agent. Provide it the file paths of all teammate output files (`$RUN_DIR/teammate-<axis>.md` for each axis). Prompt:

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
