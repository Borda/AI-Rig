---
name: refactor
description: 'Test-first refactoring — audit coverage, add characterization tests, apply changes with safety net, run quality stack and review loop. TRIGGER when: user wants to restructure existing Python code without changing behaviour; phrases: "refactor X", "clean up Y", "extract Z", "restructure this module", "improve code quality". SKIP when: bug fixes (use `/develop:fix`); new features (use `/develop:feature`); mixed refactor+feature — run `/develop:refactor` first, then `/develop:feature`; non-Python projects.'
argument-hint: <target file or directory> <goal> [--repo <owner/repo>] [--plan <path>] [--no-challenge] [--challenge] [--codemap] [--no-codemap] [--accept-no-plan] [--team] [--worktree] [--no-batch] [--keep "<items>"]
effort: xhigh
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, EnterWorktree, ExitWorktree
disable-model-invocation: true
---

<objective>

Test-first refactoring. Audit coverage, add characterization tests if missing, apply changes with safety net.

NOT for:

- bug fixes (use `/develop:fix`)
- new features (use `/develop:feature`)
- `.claude/` config changes (use `/foundry:manage` (requires foundry plugin))
- non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest; use language-native toolchain instead
- mixed refactor+feature tasks — run /develop:refactor first, then /develop:feature; never both in one run

Quality stack (Branch Safety Guard, Codex Pre-pass, Progressive Review) ships in this plugin's own `_shared` (`foundry--quality-stack.md`); missing → Step 5 quality stack skipped with visible warning — lower output quality but workflow still completes.

</objective>

<constants>

- MAX_INNER_CYCLES: 5 (change-test cycles per outer session — Step 4 safety break; mirrored via dev-max-inner-cycles/dev-max-wall-seconds sentinels in Step 4)

</constants>

<compaction>

- Key boundary: end of Step 2 — coverage audit complete, before characterization test writing in Step 3.
- Second boundary: end of Step 4 — refactor edits applied, before review stack in Step 5.
- Preserve at boundary 1: dev-dir, target path, coverage audit summary, plan-file, --keep items.
- Preserve at boundary 2: dev-dir, changed files list, test outcomes.

</compaction>

<workflow>

<!-- Agent Resolution: resolved at runtime via $_DEV_SHARED; source at develop/skills/_shared/agent-resolution.md (installed path) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # a prior run's dir must not outlive a failed --init; agent-resolution.md §Agent waits recreates it when empty
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" --init  # timeout: 5000 — cold resolve: writes dev-shared (every later block warm-reads it) and a fresh dev-agent-watch-dir per run (agent-resolution.md §Agent waits)
# loads: compaction-contract.md
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" agent-resolution.md
```

Contains: foundry check + fallback table, and §Agent waits — no polling (applies to every spawn below). If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:challenger`.

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Turn budget** — measured refactor runs made one tool call per turn throughout and spent up to 16 turns on task bookkeeping alone — the cost was the standalone turns, not the task count; every turn re-reads the whole context.

- **One task per step, never a bookkeeping-only turn.** Create one task per step — Step 1 Scope · Challenger gate · Step 2 Coverage audit · Step 3 Characterization tests · Step 4 Refactor · Step 5 Review and quality stack · Final Report — all in the same response as the first Project Detection call, so step-level progress stays visible. Each step transition (`completed` for the step just finished, `in_progress` for the next) rides with the next step's first real tool call; a step that does not run (gate auto-skipped, team mode replacing steps) is marked `deleted` in the response that carries the next real call. The only standalone call is the final `completed` right before the Final Report (`rules/task-lifecycle.md`).

- **Checkpoint appends ride along.** Each `step: N — completed` line is staged to `$DEV_DIR/checkpoint.md.rec` (Write tool) and appended to `$DEV_DIR/checkpoint.md` by `append_ledger.py` inside the next real Bash block (a boundary contract block or the next step's first command), never as a call of its own.

- **Independent calls share one response — never across a question.** After Agent Resolution: Project Detection, the preflight-helpers load and checkpoint init together; both flag-parsing blocks together in the next response (after the resume offer, when a prior checkpoint exists). Every later preamble block keeps its order: each sits behind a question (unsupported flag, codemap gates) or the worktree entry, which must precede the codemap gate. Step 1 discovery — Reads of the target files, Glob/Grep for its tests and callers — in one response. Consecutive Edits to the same file in one response, unless an edit depends on the previous one's result.

## Project Detection

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" runner-detection.md  # timeout: 5000
```

Sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

**Optional `--plan <path>`**: `$ARGUMENTS` contains `--plan <path>` (any position) → read plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — informs Step 1 scope analysis. Skip redundant codebase exploration for already-classified files. Store plan path as `PLAN_FILE`. The extraction block also records the plan's hash (`.plans/` is gitignored, so edits leave no diff); Final Report re-checks it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" preflight-helpers.md  # timeout: 5000
```

Execute --plan path extraction; sets `$PLAN_FILE`.

**Checkpoint init**: create `.developments/<TS>/` run directory, capture path in `$DEV_DIR` (assigned below). Write `checkpoint.md` inside `$DEV_DIR`. After each major step (1, 2, 3, 4, 5), stage `step: N — completed` as a record in `$DEV_DIR/checkpoint.md.rec` (Write tool) and append it with `append_ledger.py` (Growing Ledgers `.rec` staging). On skill start, check for existing `.developments/*/checkpoint.md` — offer resume from last completed step if found.

```bash
# persist DEV_DIR for compaction recovery — bash resets between calls
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DEV_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_dir.py" --persist refactor)  # timeout: 5000 — also writes the dev-refactor-dev-dir sentinel
: > "${TMPDIR:-/tmp}/dev-start-time-${CSID}"  # fresh wall clock per run; Step 4 init keeps it across Step 5→4 returns
```

## Flag parsing

Parse flags into shell variables (not prose) so downstream blocks see correct values. Persist to temp files for cross-block access (bash resets between calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-refactor "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
```

```bash
# timeout: 10000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill refactor --write-files "$ARGUMENTS"
```

Downstream blocks read back, e.g. `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`.

**Codemap flag parsing** — no separate step: `dev_parse_args.py` above already resolves `--codemap`/`--no-codemap` into `dev-refactor-codemap-${CSID}`, the skill-specific file `dev_codemap_gate.py` reads (same as feature/fix/debug) — stale values from a prior run of another skill can't leak in.

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. Found → invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--plan`, `--team`, `--worktree`, `--no-batch`, `--no-challenge`, `--challenge`, `--codemap`, `--no-codemap`, `--accept-no-plan`, `--repo`, `--keep`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

`--worktree` set → offload the whole run into an isolated git worktree — **before** codemap detection or any edit, so codemap scans + all mutations land in the worktree (per-worktree ephemeral index; parallel runs never share one index).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-refactor-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
if [ "$WORKTREE_ENABLED" = "true" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" worktree-isolation.md; fi
```

`WORKTREE_ENABLED=true` → follow §Enter (call `EnterWorktree`, warm-start codemap). Else skip — run in main tree (doc not loaded). Remember the branch for §Exit at Final Report.

**Codemap auto-detection** — run after flag parsing. Behaviour differs by mode: `strict` (user explicitly passed `--codemap`) hard-fails when codemap unavailable; `auto` and `off` soft-degrade to `false` (don't abort skill):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" refactor) || exit 1
# codemap: integrated-via-shared
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-gates.md
```

> loads: codemap-gates.md

Follow Gate A and Gate B.

**Preflight** — if `CODEMAP_ENABLED=true`: execute codemap preflight from `preflight-helpers.md` (already loaded in Project Detection).

## Step 1: Scope and understand

Read target code, build mental model before touching anything.

If `<target>` is directory: use Glob tool (pattern `**/*.py`, path `<target>`) to enumerate Python files.

```bash
find <target> -name '*.py' -exec wc -l {} + 2>/dev/null | tail -1
```

**If `CODEMAP_ENABLED=true`**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-refactor-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
if [ "$CODEMAP_ENABLED" = "true" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-context.md; fi  # timeout: 5000
```

Follow the codemap block. Flag false → nothing loaded; skip.

**Multi-file / API-change scope — extended codemap scan** (only when `CODEMAP_ENABLED=true`): target is directory, spans multiple files, or goal mentions renaming/restructuring public API (i.e. refactoring NOT limited to internals of a single function/class with unchanged public interface):

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"  # timeout: 3000
PROJ=$(basename "$_ROOT")   # raw basename — scanner writes it verbatim, never sanitized
REFACTOR_FILES=$(find <target> -name '*.py' -type f 2>/dev/null)
AFFECTED_MODULES=$(echo "$REFACTOR_FILES" | sed 's|^\./||;s|^src/||;s|\.py$||;s|/|.|g' | grep . || echo "")
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"   # root-anchored: skill may run from a subdir
if command -v codemap-py >/dev/null 2>&1 && [ -f "${_IDX}/${PROJ}.json" ] && [ -n "$AFFECTED_MODULES" ]; then
    # one batch process for all module rdeps — a per-module query loop pays process spawn + coverage cost N times
    _BATCH_REQ="${TMPDIR:-/tmp}/dev-refactor-rdeps-batch-${CSID:-$PPID}.json"
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/build_codemap_batch.py" "$_BATCH_REQ" --modules "$(echo $AFFECTED_MODULES)" --queries rdeps
    codemap-py query batch "$_BATCH_REQ" 2>/dev/null
    codemap-py query coupled --top 10
fi
```

Include `## Scope & Reusability (codemap-py)` block in foundry:sw-engineer spawn prompt. `rdeps` returns callers **outside** refactoring scope → flag explicitly — those callers must update or refactoring silently breaks public contract. `CODEMAP_ENABLED=false` and scope is multi-file → skip silently.

Spawn **foundry:sw-engineer** agent to analyze code and identify — Arm batch `scope` in the spawn response (`agent-resolution.md` §Agent waits — no polling, per-agent deadlines). It must identify:

- Public API surface (functions, classes, methods external code calls)
- Internal complexity hotspots (cyclomatic complexity, deep nesting, long functions)
- Code smells relevant to stated goal
- Dependencies and coupling between modules
- **Complexity smell**: directory or cross-module scope — flag it; consider team mode

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" premise-grounding.md  # timeout: 5000
```

§Premise Grounding Gate. Apply using **refactor** context from Skill contexts table.

**Goal classification gate**: after sw-engineer analysis completes, scan goal text for mixed signals — goal contains both refactor keywords (rename, extract, restructure, decouple, consolidate) AND feature keywords (add, implement, new, support) naming new behaviour — "add type annotations/hints" is a Step 4 refactoring category, never a feature signal → ask: "Goal mixes refactoring and feature work — split into two runs." · (a) Abort — run refactor first, then feature · (b) Continue as refactor-only — treat feature additions as out of scope.

**Scope gate**: target spans 3+ modules OR 5+ files OR goal mentions any public-API rename → flag complexity smell. Ask: "Narrow scope (Recommended)" / "Proceed anyway".

Both gates evaluate after the same sw-engineer analysis — when BOTH fire, invoke `AskUserQuestion` ONCE with both questions in the same call (menus stay distinct verbatim; a second sequential window costs another human-idle round trip). Only one fires → single-question call as usual.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" plan-inline.md  # timeout: 5000
```

§Inline Plan Generation Protocol. Apply using **refactor** context from Skill contexts table. On proceed: set `PLAN_FILE=<path>`; continue to Step 2. On small complexity or `ACCEPT_NO_PLAN=true`: skip and continue to Step 2.

## Challenger gate

**Decision** (default runs at any size — a small refactor of a main-path helper can still change behaviour; size never skips the gate):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**.
2. else → **always run**. `--challenge` (`CHALLENGE_FORCED`) is accepted for compatibility and changes nothing.

Arm batch `challenge` in the spawn response (`agent-resolution.md` §Agent waits). Spawn `foundry:challenger` with scope analysis from Step 1 (affected files, dependencies, coupling, risks):

> "Review the refactoring scope and approach. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:

- **Blockers found** → STOP. Write the blocker findings (one line each: claim · file:line · evidence) to `$DEV_DIR/blockers.md` first; print them once as the last reply text before invoking `AskUserQuestion`, as normal Markdown. The question text carries the blocker count, headline and `→ $DEV_DIR/blockers.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview` — "Challenger raised N blocker(s) on the refactoring approach. How to proceed?" · (a) **Revise scope** — return to Step 1 with the blocker as a constraint · (b) **Accept risk** — proceed, record acceptance in `$DEV_DIR/checkpoint.md` (staged `.rec` record, appended by `append_ledger.py`) · (c) **Abort**. On Abort: stop. Never proceed on prose alone.
- **Concerns only** → surface as advisory before coverage audit; continue.
- **No findings / all refuted** → proceed.

## Step 2: Audit test coverage

Find existing tests for target code:

Use Glob tool (pattern `**/test_*.py` or `**/*_test.py`), then Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those referencing target.

> (Use Glob tool — `pattern: **/test_*.py` — to discover test files; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` for configured paths)

`<module_name>`, `<target_module>` (below) and `<test_file>` (Step 3) are **substitution tokens** — replace each with the real value from Step 1 scope before running; an unquoted, unsubstituted `<…>` parses as a shell redirect, not text.

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
[ -n "$PYTEST_CMD" ] || { echo "! BLOCKED — PYTEST_CMD unresolved; re-run Project Detection (an empty runner would classify every function uncovered)"; exit 1; }
# ONE collection pass feeds head-5 sanity print, cov-plugin probe, and module grep — three separate --co runs re-collect the whole suite each time
_CO_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest -- --co -q --cov=. 2>&1)
[ $? -ne 64 ] || { echo "! BLOCKED — test runner refused to start (exit 64): $_CO_OUT"; exit 1; }  # 64 = no test command ran, never "No tests found"
SKIP_COV=0
if echo "$_CO_OUT" | grep -q "ModuleNotFoundError\|No module named.*cov\|unrecognized arguments.*--cov"; then
    echo "⚠ coverage tool not found — coverage gate skipped"
    SKIP_COV=1
    _CO_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest -- --co -q 2>&1)   # cov-less re-collect — the probe run errored before listing tests
    [ $? -ne 64 ] || { echo "! BLOCKED — test runner refused to start (exit 64): $_CO_OUT"; exit 1; }
fi
echo "$_CO_OUT" | head -5
echo "$_CO_OUT" | grep -i "<module_name>" || echo "No tests found for <module_name>"

[ "${SKIP_COV}" -eq 0 ] && { python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest -- --cov=<target_module> -q --cov-report=term-missing || true; }
```

`SKIP_COV=1` → skip coverage classification entirely — don't classify any function as UNCOVERED; note "coverage tool absent — coverage audit skipped" in audit output. **Step 3 qa-specialist spawn behavior when `SKIP_COV=1`**: spawn qa-specialist with all public functions listed as `coverage: unknown`, instructed to write characterization tests for every public function (can't prioritize uncovered functions when coverage unknown — test all for safety net). Proceed to Step 3 with unknown coverage state.

Classify each public function/method (only when `SKIP_COV=0`):

- **Covered**: at least one test for happy path + one edge case
- **Partially covered**: test exists but missing edge cases or failure paths
- **Uncovered**: no test

### Review: Validate the coverage audit

Before writing characterization tests, evaluate audit output critically:

1. **Completeness**: all public functions, methods, classes identified — including complex call paths?
2. **Classification accuracy**: each item correctly classified? Partial-covered often misclassified as covered.
3. **Refactor relevance**: uncovered/partial items in code paths refactoring will touch?
4. **Hidden dependencies**: integration points or cross-module calls audit may have missed?

Audit incomplete → re-examine before Step 3. Gaps found mid-refactoring (Step 4) are costly.

<!-- Only active when --team flag passed (~10% of invocations) -->

**Team mode branch** — if `TEAM_MODE=true`: Steps 1–2 complete solo (teammates need scope + coverage context). Spawn both teammates now; skip Steps 3–4 and Step 5's review loop (sw-engineer runs them); after results run Step 5's quality stack and final gate, then the Final Report.

> loads: team-mode.md — gated; ~90% of runs (`--team` absent) skip the load entirely

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false
[ "$TEAM_MODE" = "true" ] && cat "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/refactor/modes/team-mode.md"
```

Continue to Step 3 only when `TEAM_MODE=false`.

```bash
# boundary 1: after coverage audit, before characterization tests (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_PRESERVE="dev-dir=$_DEV_DIR, plan-file=${_PLAN_FILE:-none}, pytest-cmd=$_PYTEST_CMD"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" --keep-sentinel dev-refactor-keep-items "develop:refactor" "characterize+edit (after coverage audit)" "$_DEV_DIR" "$_PRESERVE" "add characterization tests (Step 3) → refactor with safety net (Step 4)"  # timeout: 5000
```

## Step 3: Add characterization tests (if needed)

For every **uncovered** or **partially covered** public API, arm batch `tests` (`agent-resolution.md` §Agent waits) and spawn **foundry:qa-specialist** to generate characterization tests:

- Import function, call with representative inputs, assert **current** output
- Use `pytest.mark.parametrize` for multiple input/output pairs
- Name tests `test_<function>_characterization_*`

Spawn with context:

- Target module: `<module_path>`
- Coverage audit results: [paste coverage-audit output showing uncovered/partial functions]
- Uncovered public APIs to test: [list from audit]
- Current code (read target file before writing tests — tests must assert CURRENT behaviour, not desired)
- Test file target: `tests/test_<module>_characterization.py`
- Test naming: `test_<function>_characterization_<scenario>`

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest -- <test_file> -v; GATE_EXIT=$?
echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-refactor-gate-exit-${CSID}
# Gate: all characterization tests must pass before proceeding — verdict in the same call as the run
if [ "${GATE_EXIT}" -eq 5 ]; then
    echo "GATE FAIL: no tests collected (exit 5) — characterization test file missing or not detected by pytest; cannot proceed to Step 4 without a safety net"
elif [ "$GATE_EXIT" -eq 64 ]; then
    echo "! GATE ERROR: test runner refused to start (exit 64) — no test ran; re-run Project Detection, do not fix the test"
elif [ "$GATE_EXIT" -ne 0 ]; then
    echo "GATE FAIL: characterization test(s) failed (exit $GATE_EXIT) — fix the test, not the code"
else
    echo "GATE OK: all characterization tests pass on unmodified code"
fi
```

`GATE_EXIT -ne 0` (including exit 5) → characterization tests missing or wrong — **cannot proceed to Step 4 without a passing safety net**. Invoke `AskUserQuestion` — "Characterization test gate failed (exit `$GATE_EXIT`). How to proceed?" · (a) **Fix test collection path / fix test assertions** (recommended — re-spawn qa-specialist with corrected path or assertions) · (b) **Proceed without safety net** (accept risk — record decision in `$DEV_DIR/checkpoint.md`) · (c) **Abort**. On (b): document explicit acceptance in `checkpoint.md` (`step: 3 — gate exit $GATE_EXIT — proceed without safety net (user accepted)`) before continuing — stage the line in `$DEV_DIR/checkpoint.md.rec` (Write tool) and append it with `append_ledger.py` in the next real Bash block.

## Step 4: Refactor with safety net

**Test-run policy** — inside the Step 4 refactor loop and the Step 5 review loop, tests run targeted only, never widened to the full suite. The full suite runs at the final gate once the loops are clean, and again after any fix to a full-suite failure (§Final gate).

**Batch mode gate** — default on, `--no-batch` opts out; groups non-overlapping change-test edits into fewer test runs (`_shared/batch-mode.md` §Batch mode: non-overlap predicate, per-edit snapshot/bisect design, cap accounting):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
if python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_batch_gate.py" refactor; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" batch-mode.md; fi  # timeout: 5000 — batch mode on: writes dev-batch-skill, which batch-mode.md's own fences read back (never a literal <skill> placeholder); off: prints the verdict, loads nothing — gate exits 1, `if` keeps the call at 0 so the opt-out never renders as an error
echo "0"             > ${TMPDIR:-/tmp}/dev-inner-cycle-${CSID}
[ -s "${TMPDIR:-/tmp}/dev-start-time-${CSID}" ] || echo "$(date +%s)"   > ${TMPDIR:-/tmp}/dev-start-time-${CSID}  # once per run (Flag parsing truncates); cycle count resets per Step 4 pass
echo 5    > ${TMPDIR:-/tmp}/dev-max-inner-cycles-${CSID}
echo 1800 > ${TMPDIR:-/tmp}/dev-max-wall-seconds-${CSID}  # 30 min wall cap per run
```

Gate prints `batch mode: on` (default) and loads batch-mode.md → follow the loaded §Batch mode procedure (refactor context) for this loop instead of running steps 1-4 below once per change — group non-overlapping focused changes into a batch, one test run per batch, bisect on failure. Scope stays this loop only; Step 5's review loop keeps its own unbatched per-cycle re-run. Gate prints `batch mode: off` (`--no-batch`; the call still exits 0) → continue below unchanged.

For each change (or, in batch mode, per non-overlapping batch member):

1. One focused change (single responsibility per edit)
2. Run the tests this change touches — targeted only (Test-run policy):
   ```bash
   # timeout: 600000
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_test_targets.py" --skill refactor --run
   ```
   - Selects per changed module via `codemap-py query test-impact` when available, path heuristics otherwise (name-matched tests plus tests importing the module); Step 3's characterization tests are selected as changed files. The first output line is the selection JSON — surface any `codemap.partial`/`codemap.fell_back` count and every `not_covered` entry (`⚠ not_covered — some changed code has no mapped test`) as a coverage caveat; the selection and full log are recorded under `$DEV_DIR/test-targets.jsonl`.
   - Empty selection (`note` set) → nothing ran; say so (Test-run policy).
3. Tests pass: proceed to next change
4. Tests fail: revert, try different approach

**Safety break**: track cycle count and wall time via temp files (bash resets between calls — `$INNER_CYCLE`/`$START_TIME` inline don't survive to later blocks; persistence mandatory). The Batch mode gate block above already initialised them (cycle `0` on every Step 4 pass, start time once per run, caps 5 cycles / 1800 s).

At each inner iteration start, read back, increment, check. With batch mode on, the loop counts **edits processed**, not passes through the loop: `BATCH_SIZE` is the number of non-overlapping members the just-closed batch contained (1 when batch mode is off or no batch formed — cap accounting then matches today's per-edit behavior exactly):

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r INNER_CYCLE < "${TMPDIR:-/tmp}/dev-inner-cycle-${CSID}" 2>/dev/null || INNER_CYCLE="0"
IFS= read -r START_TIME < "${TMPDIR:-/tmp}/dev-start-time-${CSID}" 2>/dev/null || START_TIME=$(date +%s)
# BATCH_SIZE: written by batch-mode.md on batch close; read+reset to "1" in same fence — a separate
# clear fence is skipped by a cap/abort exit, leaving a stale value to double-count the next cycle.
IFS= read -r BATCH_SIZE < "${TMPDIR:-/tmp}/dev-refactor-batch-size-${CSID}" 2>/dev/null || BATCH_SIZE=""
INNER_CYCLE=$((INNER_CYCLE + ${BATCH_SIZE:-1}))
echo "$INNER_CYCLE" > ${TMPDIR:-/tmp}/dev-inner-cycle-${CSID}
echo "1" > "${TMPDIR:-/tmp}/dev-refactor-batch-size-${CSID}"
IFS= read -r MAX_INNER_CYCLES < "${TMPDIR:-/tmp}/dev-max-inner-cycles-${CSID}" 2>/dev/null || MAX_INNER_CYCLES=5
IFS= read -r MAX_WALL_SECONDS < "${TMPDIR:-/tmp}/dev-max-wall-seconds-${CSID}" 2>/dev/null || MAX_WALL_SECONDS=1800
if [ "$INNER_CYCLE" -gt $MAX_INNER_CYCLES ]; then
    echo "⚠ MAX_INNER_CYCLES ($MAX_INNER_CYCLES) reached — stopping refactor loop; report what succeeded, what broke, what remains"
fi
ELAPSED=$(( $(date +%s) - START_TIME ))
if [ "$ELAPSED" -ge $MAX_WALL_SECONDS ]; then
    echo "⚠ wall-time cap reached (30 min) — stopping refactor loop"
fi
```

The block above is the only increment site — run it once per change-test pair (batch mode: once per closed batch), never a second increment after the pair. Stop at `INNER_CYCLE > MAX_INNER_CYCLES` or elapsed ≥ `MAX_WALL_SECONDS`.

**Refactoring categories:**

- **Logic simplification**: replace complex conditionals, flatten nesting, extract helpers
- **API cleanup**: rename for clarity, consolidate parameters, add type annotations
- **Structural**: extract classes/modules, reduce coupling, apply design patterns
- **Performance**: replace loops with vectorized ops, reduce allocations, batch I/O
- **Dead code removal**: remove unused imports, unreachable branches, commented-out code; scan `_`-prefixed functions with no call sites; flag public methods absent from `__init__.py` exports

```bash
# boundary 2: after refactor edits, before review stack (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_CHANGED=$(git diff --name-only HEAD 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:refactor" "review+quality (after refactor edits applied)" "$_DEV_DIR" "dev-dir=$_DEV_DIR, changed-files=$_CHANGED, pytest-cmd=$_PYTEST_CMD" "review and close gaps (Step 5) → Final Report"  # timeout: 5000
```

## Step 5: Review and close gaps

Full review of refactored code. **Loop** — review -> targeted refactoring (return to Step 4) -> re-review until only nits remain. Max 3 outer cycles. (Step 4's "max 5 change-test cycles" bound applies within each pass through Step 4, independent of outer loop.)

**Each cycle:**

1. Evaluate against all criteria:

   - **Behavior preservation**: all characterization tests and pre-existing tests pass with identical outputs
   - **Goal achieved**: stated refactoring goal actually accomplished (not just partial)
   - **No new smells**: no new coupling, complexity, or duplication introduced
   - **API surface**: no unintended public API changes (signature, return type, raised exceptions)
   - **Dead code**: unreachable code after refactor was removed

2. For every gap: return to Step 4, apply targeted fix — one focused change per gap.

3. Re-run the tests this cycle's fixes touch — targeted, same block as Step 4 item 2. Measured runs re-ran the whole suite every cycle (up to seven times per session) — hence the Test-run policy:

   ```bash
   # timeout: 600000
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_test_targets.py" --skill refactor --run
   ```

4. **Objective convergence check**: findings this cycle identical to previous (same locations, same issues) → declare convergence, exit — further cycles won't resolve; surface to user.

5. **Only nits remain** (variable naming, comment clarity, minor formatting): document in Follow-up, exit loop.

6. **Substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: substantive issues remain → stop, surface to user.

**Foundry availability check** before quality stack:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""   # re-derive — bash resets between calls
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
[ -f "$_DEV_SHARED/foundry--quality-stack.md" ] || echo "⚠ foundry--quality-stack.md missing from this plugin's _shared — broken install; quality stack skipped"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
_SHARED="$_DEV_SHARED"  # foundry--quality-stack.md loads its siblings from $_SHARED — this plugin's own _shared
echo "$_SHARED" > "${TMPDIR:-/tmp}/qs-shared-dir-${CSID}"  # quality-stack fences reload it; missing = BLOCKED
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" foundry--quality-stack.md  # timeout: 5000
```

Not found → skip quality stack entirely, note the message above in Final Report, and run the final gate below instead. Otherwise execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps — the quality stack's directory-wide pytest run counts as the final gate's full-suite run only when `$TEST_CMD` is that same pytest invocation (`pytest`, `python -m pytest`, `uv run pytest`, `poetry run pytest`); a `tox -q` or `make test` repository never ran its own command in the stack, so run the final gate once even when no code changed. Otherwise do not run it again unless code changes after it (§Final gate).

**Final gate — full suite with the repository's own command.** Runs when the quality stack was skipped, when `$TEST_CMD` is not the stack's pytest invocation, and again after any code change that follows the quality stack's wide run — a Progressive Review fix, or any fix to a full-suite failure (the stack halting on a genuine failure, or this block failing) — a fix can break something outside the targeted set, and the stack's own re-checks are scoped. A suite that may outlive the ~10 min foreground cap runs as a background Bash call (`run_in_background: true`): end the turn, its exit re-invokes you, no polling. Non-zero → fix (rerun the failing node ids while fixing — never the verification), then rerun this block in full. Max 3 gate iterations; at the cap stop, surface the remaining failures, and never report the suite green. A green full suite is the last test evidence before the Final Report:

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" suite
```

## Final Report

**Plan drift check** — run first; any output means lead the report with that `! BLOCKED` line and let the user decide (detection only, never auto-revert):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/plan_hash.py" check  # timeout: 5000
```

```markdown
## Refactor Report: [target]

### Goal
[stated goal or "general quality pass"]

### Test Coverage Before
- Covered: N functions | Partially: N | Uncovered: N
- Characterization tests added: N

### Changes Made
| File | Change | Lines |
|------|--------|-------|
| path/to/file.py | extracted helper function | -12/+8 |

### Test Results
- All tests passing: yes/no
- Loop runs: targeted (N runs, selection source codemap/heuristic); full suite: final gate, rerun after any fix to a full-suite failure; the last run is green
- Coverage: before% -> after%

### Follow-up
- [any remaining items that need manual review]

## Confidence
**Score**: 0.NN — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- (-0.NN) [e.g., coverage tool unavailable, some tests skipped]

**Refinements**: N passes.
```

**Worktree exit** — if `WORKTREE_ENABLED=true`: follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block (path · branch · merge hint) to the report. Never auto-merge, never `remove`.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

<!-- Reference only — execution-dead at runtime; included for agent behavioral context -->

## Anti-Rationalizations

| Temptation | Reality |
| -- | -- |
| "The code is simple enough — I can skip characterization tests" | No safety net = no proof behavior unchanged. Characterization tests are the only proof. |
| "I'll fix this adjacent bug while I'm in here" | Scope creep conflates history. Adjacent bugs go in Follow-up, not this session. |
| "The tests are too brittle — I'll refactor them as well" | Refactoring tests + prod code simultaneously makes regressions unattributable. Fix tests first, separate pass. |
| "I know the codebase — no need for coverage audit" | Untested edge cases are the most common refactoring breakage. Audit finds what you don't know you don't know. |
| "This is a small change — Step 4's max-5 cycles are overkill" | Simple changes = simple test loops. Guard costs nothing when unneeded; prevents runaway sessions when needed. |

</notes>
