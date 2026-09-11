---
name: plan
description: Interactive wizard that scans the codebase, proposes a metric/guard/agent config, and writes a program.md run spec. Also runs cProfile on a file path to surface bottlenecks before prompting for optimization goal.
argument-hint: <goal> | <file.py> [out.md] [--team]
effort: medium
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Wizard: scans codebase, proposes metric/guard/agent config, writes `program.md` run spec. Also runs cProfile on file path to surface bottlenecks before prompting for optimization goal.

NOT for: running experiments (use `/research:run`); methodology validation (use `/research:judge`); full pipeline from goal to result (use `/research:sweep`); benchmarking or microbenchmark design (use `foundry:perf-optimizer` — plan's scope is ML metric optimization loops, not raw latency/throughput benchmarking).

</objective>

<workflow>

<!-- Agent resolution: see _RESEARCH_SHARED/agent-resolution.md -->

## Agent Resolution

**Environment precondition** — `CLAUDE_PLUGIN_ROOT` set automatically when skill runs via plugin manager. Fallback `plugins/cc_research` resolves only from project root. Neither resolves (bare `.claude/` copy invoked from subdirectory) → bin/ scripts return empty strings silently.

**bin/ scripts this skill depends on** (deployed inside `${CLAUDE_PLUGIN_ROOT}/bin/`): `resolve_shared.py`, `make_run_dir.py`. Each call below followed by explicit empty-result guard — silent failure surfaces as fail-fast error, never empty-string path.

**Agent resolution**: load and follow the protocol below. Contains: foundry check + fallback table. Foundry not installed → substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:solution-architect`, `foundry:perf-optimizer`, `research:scientist`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_RESEARCH_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" 2>/dev/null)  # timeout: 5000
[ -z "$_RESEARCH_SHARED" ] && { echo "! Plugin path resolution failed — ensure research plugin installed and CLAUDE_PLUGIN_ROOT set, or invoke /research:plan from project root."; exit 1; }
echo "$_RESEARCH_SHARED" > "${TMPDIR:-/tmp}/research-shared-${CSID}"  # cold resolve — every later site reads this sentinel instead of re-running python
cat "$_RESEARCH_SHARED/agent-resolution.md"
```

## Plan Mode (Steps P-P0–P-P4)

<!-- P-P prefix = Plan-mode steps; R-prefix = Run-mode steps; these labels appear in task-tracking instructions -->

Triggered by `plan <goal|file>`. Wizard configures run.

**Task tracking**: create tasks for P-P0, P-P1, P-P2, P-P2b, P-P3 at start; add P-P4 only if `--team` detected in arguments.

**Unsupported flag check**: load and follow the protocol below. Supported flags for this skill: `--team`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

### Step P-P0: Detect input type

Parse `<input>` from arguments. Determine: **file path** or **goal string**:

Extract first positional token (strip all `--<flag>` tokens from `$ARGUMENTS`, take first remaining token as `FILE_ARG`). Then:

**Disambiguation guard** — treat `FILE_ARG` as file path only if it exists on disk. Multi-token strings ($ARGUMENTS containing spaces beyond `FILE_ARG`) always goal text — never run `test -f` on first token of multi-token goal:

**Quoting note**: `$ARGUMENTS` raw string (not shell-tokenized). User-supplied quotes (e.g. `plan "reduce training loss"`) appear as literal characters. Before token counting, strip surrounding matched quotes from `$ARGUMENTS` so quoted multi-word goals correctly recognised as multi-token:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_STRIPPED=$(echo "$ARGUMENTS" | sed -E 's/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')  # timeout: 5000
NONFLAG_TOKEN_COUNT=$(echo "$_STRIPPED" | tr ' ' '\n' | grep -v '^--' | grep -v '^$' | wc -l | tr -d ' ')  # timeout: 5000
FILE_ARG=$(echo "$_STRIPPED" | tr ' ' '\n' | grep -v '^--' | grep -v '^$' | head -1)  # timeout: 5000
echo "$FILE_ARG" > "${TMPDIR:-/tmp}/research-plan-file-arg-${CSID}"
```

1. `NONFLAG_TOKEN_COUNT == 1` AND `test -f "$FILE_ARG"` succeeds → **file path**. `FILE_ARG` is script to profile. Enter profiling flow.
2. Otherwise (multi-token, or single token not on disk) → **goal string**. Use full `$ARGUMENTS` (minus flags) as `<goal>`. Skip to Step P-P1.

**Profiling flow** (file path detected):

Run baseline profiling using `FILE_ARG` only — never raw `$ARGUMENTS` in cProfile command.

**Module-file guard**: `python -m cProfile` requires executable script (has `if __name__ == "__main__":` guard or runs directly). Library/module files without entry point produce empty cProfile output. Pre-check:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FILE_ARG < "${TMPDIR:-/tmp}/research-plan-file-arg-${CSID}" 2>/dev/null || FILE_ARG=""   # re-derive — bash state lost between Bash() calls
grep -q '__main__' "$FILE_ARG" 2>/dev/null || { echo "⚠ File has no __main__ guard — cProfile will produce empty output. Falling back to goal-string path."; PROFILE_AVAILABLE=false; }
```

If `PROFILE_AVAILABLE=false` from above guard, skip the cProfile block below. Otherwise:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FILE_ARG < "${TMPDIR:-/tmp}/research-plan-file-arg-${CSID}" 2>/dev/null || FILE_ARG=""   # re-derive — bash state lost between Bash() calls
CPROFILE_OUT=$(mktemp -t research-plan-XXXX)  # timeout: 3000
python -m cProfile -s cumtime "$FILE_ARG" > "$CPROFILE_OUT" 2>&1  # timeout: 600000
PROFILE_EXIT=$?
if [ $PROFILE_EXIT -ne 0 ]; then
    echo "⚠ cProfile failed (exit $PROFILE_EXIT) — continuing without profile data. Wizard will prompt for goal string instead."
    PROFILE_AVAILABLE=false
else
    PROFILE_AVAILABLE=true
    head -40 "$CPROFILE_OUT"  # timeout: 5000
    # wall-clock number comes from cProfile's own header ("N function calls in X.XXX seconds") — label it "(measured under profiler)"; a second bare `time python` run costs up to 10 min for a number the run skill re-measures via metric_cmd anyway
fi
```

**Fallback path** — ONLY when `PROFILE_AVAILABLE=false`: skip bottleneck selection menu. Invoke `AskUserQuestion` with options: (a) **Provide goal** — enter optimization goal string directly (cProfile unavailable — note: cProfile requires self-contained runnable script with `if __name__ == '__main__'` guard; modules and test files not supported); (b) **Abort** — stop. Use user's response as `<goal>`. Proceed directly to P-P1. Skip profile-available path entirely — do not read or execute following block.

**Profile-available path** — ONLY when `PROFILE_AVAILABLE=true` (skip entirely if fallback path taken above), present top up to 5 bottleneck functions (skip rows with no data available):

```markdown
Top bottleneck functions:
1. <function> — <cumtime>s (<percentage>%)
2. <function> — <cumtime>s (<percentage>%)
...
```

Invoke `AskUserQuestion` — "What would you like to optimize?", options: (a) Overall execution time · (b) Memory usage · (c) Specific function: `<top function name>` (currently `<time>`s) · (d) Custom goal (describe).

Construct goal string from selection:

- (a) → `"Reduce wall-clock execution time of <file>"`
- (b) → `"Reduce peak memory usage of <file>"`
- (c) → `"Optimize <function> in <file> (currently <time>s)"`
- (d) → user's text

Set as `<goal>`, proceed to P-P1.

### Step P-P1: Parse and scan

**Scope guard (first action)**: Before scanning, check `<goal>` is optimization goal. Input clearly not optimization goal (code question, regex/algo explanation, debug question, any prompt without measurable improvement target) → invoke `AskUserQuestion`:

- question: "This input does not look like an optimization goal (`/research:plan` expects 'Reduce X' / 'Increase Y' / 'Improve Z metric'). How to proceed?"
- (a) label: `rephrase as optimization goal` — description: provide revised goal with measurable improvement target
- (b) label: `abort` — description: stop; use `/research` for explanatory questions

Stop if user selects (b). Never proceed to P-P2 or P-P3 without valid optimization goal.

Parse `<goal>`. Scan codebase to detect:

- Language and framework (Python, PyTorch, pytest, etc.)
- Available test runners or benchmark scripts
- Candidate metric commands (pytest coverage, benchmark scripts, eval scripts)
- Candidate guard commands (test suite, lint, type check)
- Files relevant to goal (scope files)

### Step P-P2: Present proposed config

Present config as code block for review. Include:

```yaml
metric_cmd:      [command that prints a single numeric result]
metric_direction: higher | lower
guard_cmd:       [command that must pass (exit 0) on every kept commit]
max_iterations:  [default 20]
agent_strategy:  [auto | perf | code | ml | arch]
scope_files:     [files the ideation agent may modify]
compute:         local | colab | docker
```

Dry-run both commands before presenting (add `# timeout: 60000` to timed bash calls — user commands may run minutes; ML pipeline data-loading steps may exceed 60s — increase timeout or use guard_cmd dry-run only when metric dry-run slow). Failure → flag error, propose corrections, then invoke `AskUserQuestion` — (a) **I fixed the command — re-run dry-run** · (b) **Proceed anyway (I know this command is correct)** · (c) **Abort**. Never proceed to P-P3 without user confirmation after failure.

### Step P-P2b: Agent validation (pre-write)

After user confirms, run expert agent review before writing `program.md`. Dispatch conditional on goal type — run whichever apply in parallel.

**Foundry availability check** — before dispatching any `foundry:*` agent: run `find ~/.claude/plugins/cache -path "*/foundry*" -name "solution-architect.md" 2>/dev/null | head -1`. Result empty: skip architecture and perf reviews entirely; print `⚠ foundry plugin not installed — skipping foundry:solution-architect and foundry:perf-optimizer reviews. Continuing without architecture/perf advisory.`; record gap in advisory block as `architect: skipped (foundry absent)`. Proceed to P-P3 with available advisor output (scientist only if ML keywords matched).

**Pre-spawn — create plan run dir** (review files share single timestamped dir):

```bash
PLAN_RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "plan" ".experiments" 2>/dev/null)  # timeout: 5000
[ -z "$PLAN_RUN_DIR" ] && { echo "! make_run_dir.py returned empty — research plugin path resolution failed"; exit 1; }
```

**Spawn note**: P-P2 advisors (architect, scientist, perf) run in the background — issue the batch, then end the turn; no filler call, no "waiting" line, no sleep (CLAUDE.md §6). Timeout handled post-hoc — on the completion notifications, check the review file of each dimension ACTUALLY dispatched this run (subset of `plan-review-architect.md`, `plan-review-scientist.md`, `plan-review-perf.md` — derive the expected list from the launch batch, never from the gates' full menu). Any expected file missing or empty = that dimension timed out: surface with ⏱ and continue to P-P3 with remaining advisor output.

**Architect gate** — spawn `foundry:solution-architect` only when `scope_files` contains >1 file OR `agent_strategy = arch`. Single-file optimization goals skip architect (no architectural surface to validate; saves ~5–10 min opus-tier compute). Record skip reason in advisory block as `architect: skipped (single-file scope)`.

When gate fires, before constructing the Agent() call, substitute the actual computed value of `$PLAN_RUN_DIR` into the prompt string (e.g. `.experiments/plan-2026-05-13T10-00-00Z`):

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

````text
Agent(subagent_type="foundry:solution-architect", prompt="Review a proposed research experiment scope.\n\nGoal: <goal>\nScope files (newline-separated paths in a markdown code block):\n```\n<scope_files — one path per line>\n```\nMetric command: <metric_cmd>\n\nCheck: (1) Do scope_files cover the components relevant to the goal? List architectural dependencies outside scope that the ideation agent would need to touch. (2) Are there shared abstractions (base classes, imports, shared state) outside scope required for changes within it?\n\nWrite your full review to `<PLAN_RUN_DIR>/plan-review-architect.md` using the Write tool.\nReturn ONLY: {\"ok\":true|false,\"gaps\":[\"...\"],\"suggestions\":[\"...\"],\"file\":\"<PLAN_RUN_DIR>/plan-review-architect.md\",\"confidence\":0.N}")
````

**Scientist/perf advisory — merged spawn unit.** Two keyword gates decide which dimensions are active: ML gate (`agent_strategy = ml` or goal contains accuracy, loss, model, training, inference, classification, regression) and perf gate (`agent_strategy = perf` or goal contains latency, throughput, wall-clock, speed, memory, FPS). When BOTH fire, spawn ONE agent covering both question sets — never two (their prompts overlap on metric_cmd validity, and each extra spawn pays ~120,851 tok of fixed overhead): agent type `research:scientist` when the ML gate fired, else `foundry:perf-optimizer`. When only one fires, spawn that single dimension as before. The merged spawn writes ONE FILE PER DIMENSION (own Confidence block each) and returns a JSON array, one element per dimension file. Substitute computed `$PLAN_RUN_DIR` before spawning:

```text
Agent(subagent_type="<research:scientist | foundry:perf-optimizer per rule above>", prompt="Review a proposed experiment configuration across the listed dimensions.\n\nGoal: <goal>\nMetric command: <metric_cmd>\nGuard command: <guard_cmd>\nAgent strategy: <agent_strategy>\n\n[ML dimension — include only when ML gate fired] Check: (1) Is the goal a well-formed ML hypothesis — falsifiable, with a concrete success criterion? (2) Could metric_cmd improve while the real goal is not achieved (Goodhart's Law)? (3) Is agent_strategy appropriate for this goal type? Write this dimension's full review to `<PLAN_RUN_DIR>/plan-review-scientist.md` using the Write tool.\n\n[Perf dimension — include only when perf gate fired] Check: (1) Does metric_cmd measure the right performance characteristic for this goal? (2) Is guard_cmd comprehensive enough to catch regressions an ideation agent might introduce? Write this dimension's full review to `<PLAN_RUN_DIR>/plan-review-perf.md` using the Write tool.\n\nEach review file carries its own Confidence block — never blend the dimensions into one file.\nReturn ONLY a JSON array with one element per dimension file: [{\"dim\":\"scientist|perf\",\"ok\":true|false,\"issues\":[\"...\"],\"suggestions\":[\"...\"],\"file\":\"<PLAN_RUN_DIR>/plan-review-<dim>.md\",\"confidence\":0.N}, ...]")
```

Print advisory block below config:

```text
Advisory review:
  architect: <gaps or "scope looks complete">
  scientist:  <issues or "hypothesis is well-formed">   [only if dispatched]
  perf:       <issues or "metric/guard look valid">      [only if dispatched]
```

**Pre-check output path** before presenting advisor results: resolve output path (second argument after `<goal>` if provided, else `program.md` at project root). Check if file exists: `test -f <output_path> && echo "EXISTS"`. Record result as `OUTPUT_EXISTS`.

Any agent returns `ok: false` → surface suggestions, then invoke `AskUserQuestion` combining advisor feedback and (if `OUTPUT_EXISTS`) overwrite decision in one call:

- question: "Advisor flagged issues (listed above). How to proceed?"
- (a) **Revise config** → re-present P-P2 config block (re-enter P-P2; max 3 re-entries before forcing proceed-or-abort)
- (b) **Proceed with current config** — if `OUTPUT_EXISTS`: warn "will overwrite `<output_path>`"; if not: proceed silently
- (c) **Abort** — stop

All advisors return `ok: true` AND `OUTPUT_EXISTS`: invoke `AskUserQuestion` — (a) Overwrite `<output_path>` — proceed; (b) Abort — stop. All advisors return `ok: true` AND NOT `OUTPUT_EXISTS`: proceed directly to writing — no AskUserQuestion needed.

### Step P-P3: Write program.md

Output path: resolved above in P-P2b pre-check.

Write file using canonical template, pre-populated from wizard findings:

````markdown
# Program: <title from goal>

## Goal
<one-paragraph description of what to improve and why>

## Metric
```yaml
command: <metric_cmd from wizard>
direction: higher | lower
target: <optional numeric goal — campaign stops when crossed>
```

## Guard
```yaml
command: <guard_cmd from wizard>
```

## Config
```yaml
max_iterations: 20
agent_strategy: auto | perf | code | ml | arch
scope_files:
  - <path or glob>
compute: local | colab | docker
colab_hw: # optional: H100 | L4 | T4 | A100 (used when compute: colab)
sandbox_network: none | bridge  # ⚠ not validated by judge.md C-checks — manually verify before running
```

## Notes
<optional free-form text — strategy hints, context, known constraints — ignored by the skill>
````

Print:

```text
✓ Program saved to <OUTPUT_PATH>

Next steps:
  /research:judge <OUTPUT_PATH>   ← validate plan before running (recommended)
  /research:run <OUTPUT_PATH>     ← start iteration loop directly
```

### Step P-P4: --team flag

`--team` detected in `$ARGUMENTS`:

**Precondition** — P-P3 must have completed successfully (file written, no overwrite-abort). P-P3 aborted (user chose Abort at overwrite check, or any prior P-P step aborted): mark P-P4 task `deleted`, do NOT execute steps 2–4 below, and do NOT append to any pre-existing file. P-P4 owns only the append; P-P3 owns file existence and base template.

1. Complete Steps P-P0–P-P3 as normal — produce `program.md` with full single-researcher structure.
2. Append `## Team Mode Notes` section to the `program.md` just written by P-P3 (never to a pre-existing file untouched by P-P3):
   - Number of distinct method families found (determines team size at run step)
   - Whether SOTA consensus exists — if clear winner, note team mode may not add value
3. Tell user: "`--team` applies at run step, not plan step. Run: `/research:run <program.md> --team` to execute with parallel researchers."
4. Resolve run-modes dir, read team protocol — include one-line summary in Team Mode Notes:
   ```bash
   _RESEARCH_RUN_MODES=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/research/*/skills/run/modes 2>/dev/null | head -1)
   [ -d "$_RESEARCH_RUN_MODES" ] || _RESEARCH_RUN_MODES="$(git rev-parse --show-toplevel 2>/dev/null)/plugins/cc_research/skills/run/modes"
   [ -f "$_RESEARCH_RUN_MODES/team.md" ] || { echo "⚠ team.md not found at $_RESEARCH_RUN_MODES"; }
   cat "$_RESEARCH_RUN_MODES/team.md"
   ```

</workflow>

<notes>

- **Scope boundary**: plan writes `program.md` only — methodology validation = `/research:judge`; execution = `/research:run`; full pipeline = `/research:sweep`.
- **`--team` note**: `--team` applies at run step, not plan step. Plan produces standard `program.md`; pass flag when invoking `/research:run <program.md> --team`.
- **TTL exemption**: plan run dirs (`.experiments/plan-<timestamp>/`) don't write `result.jsonl` — exempt from 30-day TTL cleanup per `.claude/rules/foundry-artifact-lifecycle.md` (installed via `/foundry:setup` — requires `foundry` plugin); remove manually when no longer needed.

</notes>
