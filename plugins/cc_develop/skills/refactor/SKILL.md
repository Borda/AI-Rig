---
name: refactor
description: 'Test-first refactoring — audit coverage, add characterization tests, apply changes with safety net, run quality stack and review loop. TRIGGER when: user wants to restructure existing Python code without changing behaviour; phrases: "refactor X", "clean up Y", "extract Z", "restructure this module", "improve code quality". SKIP when: bug fixes (use `/develop:fix`); new features (use `/develop:feature`); mixed refactor+feature — run `/develop:refactor` first, then `/develop:feature`; non-Python projects.'
argument-hint: <target file or directory> <goal> [--repo <owner/repo>] [--plan <path>] [--no-challenge] [--challenge] [--codemap] [--no-codemap] [--accept-no-plan] [--semble] [--team] [--worktree] [--keep "<items>"]
effort: xhigh
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, EnterWorktree, ExitWorktree
disable-model-invocation: true
---

<objective>

Test-first refactoring. Audit coverage, add characterization tests if missing, apply changes with safety net.

NOT for:

- bug fixes (use `/develop:fix`)
- new features (use `/develop:feature`)
- `.claude/` config changes (use `/foundry:manage` (requires foundry plugin))
- non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest; use language-native toolchain instead
- mixed refactor+feature tasks — run /develop:refactor first, then /develop:feature; do not attempt both in single skill run

Quality stack (Branch Safety Guard, Codex Pre-pass, Progressive Review) requires `foundry` plugin; when absent, Step 5 quality stack skipped with a visible warning — output lower quality but workflow still completes.

</objective>

<constants>

- MAX_INNER_CYCLES: 5 (change-test cycles per outer session — Step 4 safety break)

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
_DEV_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" 2>/dev/null)  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
echo "$_DEV_SHARED" > "${TMPDIR:-/tmp}/dev-shared-${CSID}"  # cold resolve — every later block warm-reads this
# loads: compaction-contract.md
cat "$_DEV_SHARED/agent-resolution.md"
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:challenger`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/task-hygiene.md"
```

## Project Detection

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/runner-detection.md"
```

Sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

**Optional `--plan <path>`**: if `$ARGUMENTS` contains `--plan <path>` (at any position), read plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — use to inform Step 1 scope analysis. Skip redundant codebase exploration for already-classified files. Store plan path as `PLAN_FILE`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/preflight-helpers.md"
```

Execute --plan path extraction; sets `$PLAN_FILE`.

**Checkpoint init**: create `.developments/<TS>/` run directory, capture path in `$DEV_DIR` (assigned in the block below). Write `checkpoint.md` inside `$DEV_DIR`. After each major step (1, 2, 3, 4, 5), append `step: N — completed` to `$DEV_DIR/checkpoint.md`. On skill start, check for existing `.developments/*/checkpoint.md` — offer resume from last completed step if found.

```bash
# persist DEV_DIR for compaction recovery — bash state lost between Bash() calls
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DEV_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_dir.py" 2>/dev/null)  # timeout: 5000
echo "$DEV_DIR" > "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}"
```

## Flag parsing

Parse flags into actual shell variables (not prose) so downstream blocks see correct values. Persist to temp files for cross-block access (bash state lost between Bash() calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
KEEP_ITEMS=""
if [[ "$ARGUMENTS" =~ --keep[[:space:]]\"([^\"]+)\" ]]; then
    KEEP_ITEMS="${BASH_REMATCH[1]}"
fi
echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/dev-refactor-keep-items-${CSID}"
rm -f .temp/state/skill-contract.md  # timeout: 5000
```

```bash
# timeout: 10000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill refactor --write-files "$ARGUMENTS"
```

Downstream blocks read back, e.g. `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`.

**Codemap flag parsing** — derive raw flag into a real shell variable, then normalize via `codemap_resolve.py`. Uses skill-specific temp file (`dev-refactor-codemap-raw-${CSID}`) to avoid reading stale values from prior feature/debug runs:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_RAW=auto
[[ " $ARGUMENTS " == *" --no-codemap "* ]] && CODEMAP_RAW=off
[[ " $ARGUMENTS " == *" --codemap "* ]] && [[ " $ARGUMENTS " != *" --no-codemap "* ]] && CODEMAP_RAW=strict
echo "$CODEMAP_RAW" > ${TMPDIR:-/tmp}/dev-refactor-codemap-raw-${CSID}
```

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--plan`, `--team`, `--worktree`, `--no-challenge`, `--challenge`, `--codemap`, `--no-codemap`, `--accept-no-plan`, `--semble`, `--repo`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

When `--worktree` set, offload the whole run into an isolated git worktree — **before** codemap detection or any edit, so codemap scans + all mutations land in the worktree (per-worktree ephemeral index; parallel runs never share one index).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-refactor-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/worktree-isolation.md"
```

`WORKTREE_ENABLED=true` → follow §Enter (call `EnterWorktree`, warm-start codemap). Else skip — run in main tree. Remember the branch for §Exit at Final Report.

**Codemap auto-detection** — run after flag parsing. Behaviour differs by mode: `strict` (user explicitly passed `--codemap`) hard-fails when codemap unavailable; `auto` and `off` soft-degrade to `false` (do not abort skill):

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" refactor) || exit 1
# codemap: integrated-via-shared
```

> loads: codemap-gates.md

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codemap-gates.md"
```

Follow Gate A and Gate B.

**Preflight** — if `CODEMAP_ENABLED=true`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/preflight-helpers.md"
```

Execute codemap + semble preflight if respective flags set.

## Step 1: Scope and understand

Read target code, build mental model before touching anything.

If `<target>` is directory: use Glob tool (pattern `**/*.py`, path `<target>`) to enumerate Python files.

```bash
find <target> -name '*.py' -exec wc -l {} + 2>/dev/null | tail -1
```

**If `CODEMAP_ENABLED=true` or `SEMBLE_ENABLED=true`**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codemap-context.md"
```

Follow enabled sections (codemap block if `CODEMAP_ENABLED`, semble companion if `SEMBLE_ENABLED`). Skip if both false.

**Multi-file / API-change scope — extended codemap scan** (only when `CODEMAP_ENABLED=true`): if target is directory, spans multiple files, or goal mentions renaming/restructuring public API (i.e., refactoring NOT limited to internals of single function or class with unchanged public interface):

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

Include `## Scope & Reusability (codemap-py)` block in foundry:sw-engineer spawn prompt. If `rdeps` returns callers **outside** refactoring scope: flag explicitly — those callers must update or refactoring silently breaks public contract. If `CODEMAP_ENABLED=false` and scope is multi-file: skip silently.

Spawn **foundry:sw-engineer** agent to analyze code and identify:

- Public API surface (functions, classes, methods external code calls)
- Internal complexity hotspots (cyclomatic complexity, deep nesting, long functions)
- Code smells relevant to stated goal
- Dependencies and coupling between modules
- **Complexity smell**: directory or cross-module scope — flag it; consider team mode

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/premise-grounding.md"
```

§Premise Grounding Gate. Apply using **refactor** context from Skill contexts table.

**Goal classification gate**: after sw-engineer analysis completes, scan goal text for mixed signals — if goal contains both refactor keywords (rename, extract, restructure, decouple, consolidate) AND feature keywords (add, implement, new, support), ask: "Goal mixes refactoring and feature work — split into two runs." · (a) Abort — run refactor first, then feature · (b) Continue as refactor-only — treat feature additions as out of scope.

**Scope gate**: if target spans 3+ modules OR 5+ files OR goal mentions any public-API rename — flag complexity smell. Ask: "Narrow scope (Recommended)" / "Proceed anyway".

Both gates evaluate after the same sw-engineer analysis — when BOTH fire, invoke `AskUserQuestion` ONCE with both questions in the same call (menus stay distinct verbatim; a second sequential window costs another human-idle round trip). Only one fires → single-question call as usual.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/plan-inline.md"
```

§Inline Plan Generation Protocol. Apply using **refactor** context from Skill contexts table. On proceed: set `PLAN_FILE=<path>`; continue to Step 2. On small complexity or `ACCEPT_NO_PLAN=true`: skip and continue to Step 2.

## Challenger gate

**Decision — three states** (default is NOT "skip": it runs on substantial refactors and auto-skips only small contained ones):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**, any size.
2. else `--challenge` (`IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED=false` = `true`) → **always run**, even on a small change.
3. else **default** → **run when refactor is substantial** (spans multiple files, ≳50 lines, or changes public API / an exported symbol); **auto-skip when small** (single file, ≲50 lines, no API change) — a contained refactor has little design surface to challenge.

Two flags are opposites for two regimes, which is why both exist: `--no-challenge` suppresses gate on *substantial* changes where it would otherwise fire; `--challenge` forces it on *small* changes where it would otherwise auto-skip.

Spawn `foundry:challenger` with scope analysis from Step 1 (affected files, dependencies, coupling, risks):

> "Review the refactoring scope and approach. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:

- **Blockers found** → STOP. Present findings. Don't proceed to Step 2 until user resolves each blocker or explicitly accepts risk.
- **Concerns only** → surface as advisory before coverage audit; continue.
- **No findings / all refuted** → proceed.

## Step 2: Audit test coverage

Find existing tests for target code:

Use Glob tool (pattern `**/test_*.py` or `**/*_test.py`), then Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those referencing target.

> (Use Glob tool — `pattern: **/test_*.py` — to discover test files; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` for configured paths)

```bash
# timeout: 600000
# ONE collection pass feeds head-5 sanity print, cov-plugin probe, and module grep — three separate --co runs re-collect the whole suite each time
_CO_OUT=$($PYTEST_CMD --co -q --cov=. 2>&1)
SKIP_COV=0
if echo "$_CO_OUT" | grep -q "ModuleNotFoundError\|No module named.*cov\|unrecognized arguments.*--cov"; then
    echo "⚠ coverage tool not found — coverage gate skipped"
    SKIP_COV=1
    _CO_OUT=$($PYTEST_CMD --co -q 2>&1)   # cov-less re-collect — the probe run errored before listing tests
fi
echo "$_CO_OUT" | head -5
echo "$_CO_OUT" | grep -i "<module_name>" || echo "No tests found for <module_name>"

[ "${SKIP_COV}" -eq 0 ] && { $PYTEST_CMD --cov=<target_module> -q --cov-report=term-missing || true; }
```

If `SKIP_COV=1`: skip coverage classification entirely — do not classify any function as UNCOVERED; note "coverage tool absent — coverage audit skipped" in audit output. **Step 3 qa-specialist spawn behavior when `SKIP_COV=1`**: spawn qa-specialist with all public functions listed as `coverage: unknown` and instruction to write characterization tests for every public function (cannot prioritize uncovered functions when coverage unknown — test all to ensure safety net). Proceed to Step 3 with unknown coverage state.

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

If audit incomplete: re-examine before Step 3. Gaps found mid-refactoring (Step 4) costly.

<!-- Only active when --team flag passed (~10% of invocations) -->

**Team mode branch** — if `TEAM_MODE=true`: Steps 1–2 complete solo (teammates need scope + coverage context). Spawn both teammates now; skip Steps 3–5, proceed to Final Report after results received.

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
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/dev-refactor-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_PRESERVE="dev-dir=$_DEV_DIR, plan-file=${_PLAN_FILE:-none}, pytest-cmd=$_PYTEST_CMD"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:refactor" "characterize+edit (after coverage audit)" "$_DEV_DIR" "$_PRESERVE" "add characterization tests (Step 3) → refactor with safety net (Step 4)"  # timeout: 5000
```

## Step 3: Add characterization tests (if needed)

For every **uncovered** or **partially covered** public API, spawn **foundry:qa-specialist** to generate characterization tests:

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
$PYTEST_CMD <test_file> -v; GATE_EXIT=$?
echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-gate-exit-${CSID}
```

**Gate**: all characterization tests must pass before proceeding. Check exit code from persisted file (`$?` in a fresh shell is unrelated to prior pytest run):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r GATE_EXIT < "${TMPDIR:-/tmp}/dev-gate-exit-${CSID}" 2>/dev/null || GATE_EXIT="1"
if [ "${GATE_EXIT}" -eq 5 ]; then
    echo "GATE FAIL: no tests collected (exit 5) — characterization test file missing or not detected by pytest; cannot proceed to Step 4 without a safety net"
elif [ "$GATE_EXIT" -ne 0 ]; then
    echo "GATE FAIL: characterization test(s) failed (exit $GATE_EXIT) — fix the test, not the code"
else
    echo "GATE OK: all characterization tests pass on unmodified code"
fi
```

If `GATE_EXIT -ne 0` (including exit 5): characterization tests missing or wrong — **cannot proceed to Step 4 without a passing safety net**. Invoke `AskUserQuestion` — "Characterization test gate failed (exit `$GATE_EXIT`). How to proceed?" · (a) **Fix test collection path / fix test assertions** (recommended — re-spawn qa-specialist with corrected path or assertions) · (b) **Proceed without safety net** (accept risk — record decision in `$DEV_DIR/checkpoint.md`) · (c) **Abort**. On (b): document explicit acceptance in `checkpoint.md` (`step: 3 — gate exit $GATE_EXIT — proceed without safety net (user accepted)`) before continuing.

## Step 4: Refactor with safety net

For each change:

1. One focused change (single responsibility per edit)
2. Run affected tests (prefer targeted over full characterization suite):
   ```bash
   codemap-py query test-impact "<changed_module>" 2>/dev/null
   ```
   - Non-empty `pytest_cmd` → run those tests; surface `not_covered` caveat if present; fall back to full suite if all tests pass but feel incomplete
   - Empty or unavailable → full suite:
   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <test_files> -v
   ```
3. Tests pass: proceed to next change
4. Tests fail: revert, try different approach

**Safety break**: track cycle count and wall time via temp files (bash state lost between Bash() calls — `$INNER_CYCLE` and `$START_TIME` declared inline are unavailable in subsequent Bash blocks; persistence is mandatory):

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "0"             > ${TMPDIR:-/tmp}/dev-inner-cycle-${CSID}
echo "$(date +%s)"   > ${TMPDIR:-/tmp}/dev-start-time-${CSID}
MAX_WALL_SECONDS=1800  # 30 min cap (5 outer × MAX_INNER_CYCLES worst case)
```

At each inner iteration start, read back, increment, check:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r INNER_CYCLE < "${TMPDIR:-/tmp}/dev-inner-cycle-${CSID}" 2>/dev/null || INNER_CYCLE="0"
IFS= read -r START_TIME < "${TMPDIR:-/tmp}/dev-start-time-${CSID}" 2>/dev/null || START_TIME=$(date +%s)
INNER_CYCLE=$((INNER_CYCLE+1))
echo "$INNER_CYCLE" > ${TMPDIR:-/tmp}/dev-inner-cycle-${CSID}
MAX_INNER_CYCLES=5  # must match constants block — bash can't ref it directly
if [ "$INNER_CYCLE" -gt $MAX_INNER_CYCLES ]; then
    echo "⚠ MAX_INNER_CYCLES ($MAX_INNER_CYCLES) reached — stopping refactor loop; report what succeeded, what broke, what remains"
fi
ELAPSED=$(( $(date +%s) - START_TIME ))
if [ "$ELAPSED" -ge 1800 ]; then
    echo "⚠ wall-time cap reached (30 min) — stopping refactor loop"
fi
```

After each change-test pair: re-read counter from temp file, increment, write back. Stop when `INNER_CYCLE > MAX_INNER_CYCLES` or elapsed ≥ `MAX_WALL_SECONDS`.

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

3. Re-run full test suite:

   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <test_files> -v 2>&1 | tail -20
   GATE_EXIT=${PIPESTATUS[0]}
   ```

4. **Objective convergence check**: if findings this cycle identical to previous cycle (same locations, same issues), declare convergence and exit — further cycles won't resolve; surface to user.

5. **Only nits remain** (variable naming, comment clarity, minor formatting): document in Follow-up, exit loop.

6. **Substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: substantive issues remain → stop, surface to user.

**Foundry availability check** before quality stack:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""   # re-derive — bash state lost between Bash() calls
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
[ -f "$_DEV_SHARED/foundry--quality-stack.md" ] || echo "⚠ foundry--quality-stack.md missing from this plugin's _shared — broken install; quality stack skipped"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
_SHARED="$_DEV_SHARED"  # foundry--quality-stack.md loads its siblings from $_SHARED — this plugin's own _shared
cat "$_DEV_SHARED/foundry--quality-stack.md"
```

If not found → skip quality stack entirely, note the message above in Final Report. Otherwise execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps.

## Final Report

```markdown
## Refactor Report: <target>

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
- Coverage: before% -> after%

### Follow-up
- [any remaining items that need manual review]

## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [e.g., coverage tool unavailable, some tests skipped]

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
| "The code is simple enough — I can skip characterization tests" | No safety net = no proof behavior unchanged. Characterization tests only proof. |
| "I'll fix this adjacent bug while I'm in here" | Scope creep conflates history. Adjacent bugs go in Follow-up, not this session. |
| "The tests are too brittle — I'll refactor them as well" | Refactoring tests + prod code simultaneously makes regressions unattributable. Fix tests first, separate pass. |
| "I know the codebase — no need for coverage audit" | Untested edge cases = most common refactoring breakage. Audit finds what you don't know you don't know. |
| "This is a small change — Step 4's max-5 cycles are overkill" | Simple changes = simple test loops. Guard costs nothing when unneeded; prevents runaway sessions when it is. |

</notes>
