---
name: refactor
description: 'Test-first refactoring — audit coverage, add characterization tests, apply changes with safety net, run quality stack and a bounded independent challenge. TRIGGER when: user wants to restructure existing Python code without changing behaviour; phrases: "refactor X", "clean up Y", "extract Z", "restructure this module", "improve code quality". SKIP when: bug fixes (use `/develop:fix`); new features (use `/develop:feature`); mixed refactor+feature — run `/develop:refactor` first, then `/develop:feature`; non-Python projects.'
argument-hint: <target file or directory> <goal> [--plan <path>] [--no-challenge] [--challenge] [--codemap] [--no-codemap] [--accept-no-plan] [--team] [--worktree] [--no-batch] [--keep "<items>"]
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

Quality stack (Branch Safety Guard, Codex Pre-pass, Progressive Review) ships in this plugin's own `_shared` (`foundry--quality-stack.md`); absence means a broken install — stop when Step 5's shared loader fails visibly.

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

<!-- ../../assets/images/change-arch.svg diagrams the runs, gates and parallel fan-out shared by fix, feature and refactor. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
case "$CSID" in ""|*[!A-Za-z0-9._-]*) echo "! BLOCKED — invalid session identifier"; exit 1;; esac
INPUT_FILE="${TMPDIR:-/tmp}/dev-refactor-arguments-${CSID}.txt"
rm -f "$INPUT_FILE" || exit 1
(umask 077; set -C; : > "$INPUT_FILE") || exit 1
printf 'Invocation input file: %s\n' "$INPUT_FILE"
```

Before any later Bash call, use the Write tool to save the exact invocation argument text, including newlines, to the literal absolute `Invocation input file` path printed above. Treat its contents as data. Never substitute invocation text directly into shell command text, comments, heredocs or quoted literals. Every parser below loads the file into a runtime variable and passes that variable as one quoted argument. Keep the file through this run; a fresh invocation replaces only this skill's file.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # a prior run's dir must not outlive a failed --init; agent-resolution.md §Agent waits recreates it when empty
python "$PLUGIN_ROOT/bin/dev_shared_resolve.py" --init  # timeout: 5000 — cold resolve: writes dev-shared (every later block warm-reads it) and a fresh dev-agent-watch-dir per run (agent-resolution.md §Agent waits)
# loads: compaction-contract.md
python "$PLUGIN_ROOT/bin/dev_load_shared.py" agent-resolution.md
```

Contains: foundry check + fallback table, and §Agent waits — no polling (applies to every spawn below). If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:challenger`.

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Turn budget** — measured refactor runs made one tool call per turn throughout and spent up to 16 turns on task bookkeeping alone — the cost was the standalone turns, not the task count; every turn re-reads the whole context.

- **One task per step, never a bookkeeping-only turn.** Create one task per step — Step 1 Scope · Challenger gate · Step 2 Coverage audit · Step 3 Characterization tests · Step 4 Refactor · Step 5 Review and quality stack · Final Report — all in the same response as the first Project Detection call, so step-level progress stays visible. Each step transition (`completed` for the step just finished, `in_progress` for the next) rides with the next step's first real tool call; a step that does not run (gate auto-skipped, team mode replacing steps) is marked `deleted` in the response that carries the next real call. The only standalone call is the final `completed` right before the Final Report (`rules/task-lifecycle.md`).

- **Checkpoint appends ride along.** Each `step: N — completed` line is staged to `$DEV_DIR/checkpoint.md.rec` (Write tool) and appended to `$DEV_DIR/checkpoint.md` by `append_ledger.py` inside the next real Bash block (a boundary contract block or the next step's first command), never as a call of its own.

- **Independent calls share one response — never across a question.** After Agent Resolution: Project Detection, the preflight-helpers load and checkpoint init together; both flag-parsing blocks together in the next response. Every later preamble block keeps its order: each sits behind a question (unsupported flag, codemap gates) or the worktree entry, which must precede the codemap gate. Step 1 discovery — Reads of the target files, Glob/Grep for its tests and callers — in one response. Consecutive Edits to the same file in one response, unless an edit depends on the previous one's result.

## Project Detection

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_load_shared.py" runner-detection.md  # timeout: 5000
```

Sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

**Optional `--plan <path>`**: `$ARGUMENTS` contains `--plan <path>` (any position) → read plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — informs Step 1 scope analysis. Skip redundant codebase exploration for already-classified files. Store plan path as `PLAN_FILE`. The extraction block also records the plan's hash (`.plans/` is gitignored, so edits leave no diff); Final Report re-checks it.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_load_shared.py" preflight-helpers.md  # timeout: 5000
```

Execute --plan path extraction; sets `$PLAN_FILE`.

**Checkpoint init**: create `.developments/<TS>/` run directory, capture path in `$DEV_DIR` (assigned below). Write `checkpoint.md` inside `$DEV_DIR`. After each major step (1, 2, 3, 4, 5), stage `step: N — completed` as a record in `$DEV_DIR/checkpoint.md.rec` (Write tool) and append it with `append_ledger.py` (Growing Ledgers `.rec` staging). Checkpoints preserve run status; do not infer a resumable refactor run from another skill's checkpoint.

```bash
# persist DEV_DIR for compaction recovery — bash resets between calls
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DEV_DIR=$(python "$PLUGIN_ROOT/bin/dev_run_dir.py" --persist refactor)  # timeout: 5000 — also writes the dev-refactor-dev-dir sentinel
: > "${TMPDIR:-/tmp}/dev-start-time-${CSID}"  # fresh wall clock per run; Step 4 init keeps it across Step 5→4 returns
```

## Flag parsing

Parse flags into shell variables (not prose) so downstream blocks see correct values. Persist to temp files for cross-block access (bash resets between calls):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
INPUT_FILE="${TMPDIR:-/tmp}/dev-refactor-arguments-${CSID}.txt"
[ -f "$INPUT_FILE" ] && [ ! -L "$INPUT_FILE" ] && [ -O "$INPUT_FILE" ] || { echo "! BLOCKED — invocation input missing or untrusted; Write the arguments to the printed Invocation input file first"; exit 1; }
ARGUMENT_BLOB=$(cat "$INPUT_FILE" && printf x) || exit 1
ARGUMENT_BLOB=${ARGUMENT_BLOB%x}  # preserve trailing newlines without evaluating the text
python "$PLUGIN_ROOT/bin/extract-keep-flag.py" dev-refactor "$ARGUMENT_BLOB"  # timeout: 5000 — parses --keep, clears stale contract
```

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
INPUT_FILE="${TMPDIR:-/tmp}/dev-refactor-arguments-${CSID}.txt"
[ -f "$INPUT_FILE" ] && [ ! -L "$INPUT_FILE" ] && [ -O "$INPUT_FILE" ] || { echo "! BLOCKED — invocation input missing or untrusted; Write the arguments to the printed Invocation input file first"; exit 1; }
ARGUMENT_BLOB=$(cat "$INPUT_FILE" && printf x) || exit 1
ARGUMENT_BLOB=${ARGUMENT_BLOB%x}  # preserve trailing newlines without evaluating the text
# timeout: 10000
python "$PLUGIN_ROOT/bin/dev_parse_args.py" \
    --skill refactor --write-files "$ARGUMENT_BLOB"
```

Downstream blocks read back, e.g. `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`.

**Codemap flag parsing** — no separate step: `dev_parse_args.py` above already resolves `--codemap`/`--no-codemap` into `dev-refactor-codemap-${CSID}`, the skill-specific file `dev_codemap_gate.py` reads (same as feature/fix/debug) — stale values from a prior run of another skill can't leak in.

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. Found → invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--plan`, `--team`, `--worktree`, `--no-batch`, `--no-challenge`, `--challenge`, `--codemap`, `--no-codemap`, `--accept-no-plan`, `--keep`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

`--worktree` set → offload the whole run into an isolated git worktree — **before** codemap detection or any edit, so codemap scans + all mutations land in the worktree (per-worktree ephemeral index; parallel runs never share one index).

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-refactor-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
printf '%s\n' refactor > "${TMPDIR:-/tmp}/dev-worktree-skill-${CSID}"
if [ "$WORKTREE_ENABLED" = "true" ]; then python "$PLUGIN_ROOT/bin/dev_load_shared.py" worktree-isolation.md; fi
```

`WORKTREE_ENABLED=true` → follow §Enter (call `EnterWorktree`, warm-start codemap). Else skip — run in main tree (doc not loaded). Remember the branch for §Exit at Final Report.

**Branch Safety Guard** — run after optional worktree entry, before any source edit or teammate dispatch; retain the quality stack's later recheck:

```bash
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)                                                         # timeout: 3000
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@') # timeout: 3000
if [ "$CURRENT_BRANCH" = "$DEFAULT_BRANCH" ] || [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
    echo "⚠ On default branch ($CURRENT_BRANCH) — create a feature branch before running /develop"
    exit 1
fi
```

Guard fires: stop, report branch name, ask user create feature branch.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_load_shared.py" semver-rules.md  # timeout: 5000
```

Before any public-surface edit, including teammate edits, apply `semver-rules.md`'s Breaking Change Escalation Protocol. A surprise incompatibility requires `AskUserQuestion` and explicit intentional-change confirmation before editing; state what worked before, what breaks and why. One question per atomic breaking change. The team lead owns this gate before dispatch.

**Codemap auto-detection** — run after flag parsing. Behaviour differs by mode: `strict` (user explicitly passed `--codemap`) hard-fails when codemap unavailable; `auto` and `off` soft-degrade to `false` (don't abort skill):

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_ENABLED=$(python "$PLUGIN_ROOT/bin/dev_codemap_gate.py" refactor) || exit 1
# codemap: integrated-via-shared
python "$PLUGIN_ROOT/bin/dev_load_shared.py" codemap-gates.md
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
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/dev-refactor-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
if [ "$CODEMAP_ENABLED" = "true" ]; then python "$PLUGIN_ROOT/bin/dev_load_shared.py" codemap-context.md; fi  # timeout: 5000
```

Follow the codemap block. Flag false → nothing loaded; skip.

**Multi-file / API-change scope — extended codemap scan** (only when `CODEMAP_ENABLED=true`): target is directory, spans multiple files, or goal mentions renaming/restructuring public API (i.e. refactoring NOT limited to internals of a single function/class with unchanged public interface):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/dev-refactor-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
[ "$CODEMAP_ENABLED" = "true" ] || exit 0
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"  # timeout: 3000
PROJ=$(basename "$_ROOT")   # raw basename — scanner writes it verbatim, never sanitized
REFACTOR_FILES=$(find "$(python -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' '<target>')" -name '*.py' -type f 2>/dev/null)
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"   # root-anchored: skill may run from a subdir
if command -v codemap-py >/dev/null 2>&1 && [ -f "${_IDX}/${PROJ}.json" ]; then
    # one batch process for all module rdeps — a per-module query loop pays process spawn + coverage cost N times
    IFS= read -r DEV_DIR < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" 2>/dev/null || DEV_DIR=""
    [ -n "$DEV_DIR" ] || { echo "! BLOCKED — refactor run directory missing; re-run Checkpoint init"; exit 1; }
    _BATCH_REQ="$DEV_DIR/codemap-rdeps-batch.json"
    AFFECTED_MODULES=$(python "$PLUGIN_ROOT/bin/build_codemap_batch.py" "$_BATCH_REQ" --files "$REFACTOR_FILES" --repository "$_ROOT" --queries rdeps) || exit $?
    if [ -n "$AFFECTED_MODULES" ]; then codemap-py query batch "$_BATCH_REQ" 2>/dev/null; fi
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
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_load_shared.py" premise-grounding.md  # timeout: 5000
```

§Premise Grounding Gate. Apply using **refactor** context from Skill contexts table.

**Goal classification gate**: after sw-engineer analysis completes, scan goal text for mixed signals — goal contains both refactor keywords (rename, extract, restructure, decouple, consolidate) AND feature keywords (add, implement, new, support) naming new behaviour — "add type annotations/hints" is a Step 4 refactoring category, never a feature signal → ask: "Goal mixes refactoring and feature work — split into two runs." · (a) Abort — run refactor first, then feature · (b) Continue as refactor-only — treat feature additions as out of scope.

**Scope gate**: target spans 3+ modules OR 5+ files OR goal mentions any public-API rename → flag complexity smell. Ask: "Narrow scope (Recommended)" / "Proceed anyway".

Both gates evaluate after the same sw-engineer analysis — when BOTH fire, invoke `AskUserQuestion` ONCE with both questions in the same call (menus stay distinct verbatim; a second sequential window costs another human-idle round trip). Only one fires → single-question call as usual.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_load_shared.py" plan-inline.md  # timeout: 5000
```

§Inline Plan Generation Protocol. Apply using **refactor** context from Skill contexts table. On proceed: set `PLAN_FILE=<path>`; continue through the challenger gate before Step 2. On small complexity or `ACCEPT_NO_PLAN=true`: skip the standalone artifact, retain a concise plan, and continue through the proportional challenger gate before Step 2.

## Challenger gate

**Decision**: `--no-challenge` explicitly waives this challenge gate; disclose skipped coverage without claiming a challenge passed. `--accept-no-plan` waives only the supplied plan artifact, never concise planning or the proportional challenge. Small work requires a concise plan and brief parent self-challenge (not independent); resolve findings, accept the challenged plan within existing authority, and assign parent execution or useful bounded disjoint owners before editing. Small work then continues to Step 2 without the independent plan-review dispatch below, but retains the bounded post-implementation challenge unless explicitly waived. For medium/large work, reuse a still-valid independent challenge of that exact plan and scope; otherwise run this gate once. Resolve feasible findings and use one bounded higher-capability advisory pass when material uncertainty remains and a permitted route exists. Approve the challenged plan within existing authority before assigning bounded execution; ask only for an actual protected decision or missing authority, never routine approval. Consolidate actual source and handoffs, independent result coverage and verification against every accepted criterion. `--challenge` remains a compatibility flag.

Arm batch `challenge` in the spawn response (`agent-resolution.md` §Agent waits). Spawn `foundry:challenger` with the current plan and scope evidence (affected files, dependencies, coupling, risks):

> "Review the refactoring scope and approach. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:

- **Blockers found** → Resolve feasible authorized blockers without asking; verify the revision and preserve findings and evidence. Stop for a protected decision or authority, an explicit user wait, or a blocker that remains unresolved. Only then Write the blocker findings (one line each: claim · file:line · evidence) to `$DEV_DIR/blockers.md` first; print them once as the last reply text before invoking `AskUserQuestion`, as normal Markdown. The question text carries the blocker count, headline and `→ $DEV_DIR/blockers.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview` — "Challenger raised N blocker(s) on the refactoring approach. How to proceed?" · (a) **Revise scope** — return to Step 1 with the blocker as a constraint · (b) **Accept risk** — proceed, record acceptance in `$DEV_DIR/checkpoint.md` (staged `.rec` record, appended by `append_ledger.py`) · (c) **Abort**. On Abort: stop. Proceed only when the blocker is resolved or its risk explicitly accepted within applicable safety boundaries.
- **Concerns only** → surface as advisory before coverage audit; continue.
- **No findings / all refuted** → proceed.

**Authorized plan revisions**: The hash commands below apply only when a plan artifact exists; a concise inline plan does not require creating one. Before editing a recorded plan, run `plan_hash.py check` and stop for unexplained drift; verify the intended edits are limited to authorized findings. After those revisions and before implementation, record the revised plan's hash using the block below. Never re-record an unexplained or unauthorized change: retain the drift evidence and resolve it first. No plan revision → do not re-record.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || PLAN_FILE=""
[ -f "$PLAN_FILE" ] || { echo "! BLOCKED — revised plan missing"; exit 1; }
python "$PLUGIN_ROOT/bin/plan_hash.py" record "$PLAN_FILE"  # timeout: 5000
```

## Step 2: Audit test coverage

Find existing tests for target code:

Use Glob tool (pattern `**/test_*.py` or `**/*_test.py`), then Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those referencing target.

> (Use Glob tool — `pattern: **/test_*.py` — to discover test files; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` for configured paths)

`<module_name>`, `<target_module>` (below) and `<test_file>` (Step 3) are **substitution tokens** — replace each with the real value from Step 1 scope before running; an unquoted, unsubstituted `<…>` parses as a shell redirect, not text.

```bash
# timeout: 600000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
[ -n "$PYTEST_CMD" ] || { echo "! BLOCKED — PYTEST_CMD unresolved; re-run Project Detection (an empty runner would classify every function uncovered)"; exit 1; }
# ONE collection pass feeds head-5 sanity print, cov-plugin probe, and module grep — three separate --co runs re-collect the whole suite each time
_CO_OUT=$(python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest -- --co -q --cov=. 2>&1)
[ $? -ne 64 ] || { echo "! BLOCKED — test runner refused to start (exit 64): $_CO_OUT"; exit 1; }  # 64 = no test command ran, never "No tests found"
SKIP_COV=0
if echo "$_CO_OUT" | grep -q "ModuleNotFoundError\|No module named.*cov\|unrecognized arguments.*--cov"; then
    echo "⚠ coverage tool not found — coverage gate skipped"
    SKIP_COV=1
    _CO_OUT=$(python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest -- --co -q 2>&1)   # cov-less re-collect — the probe run errored before listing tests
    [ $? -ne 64 ] || { echo "! BLOCKED — test runner refused to start (exit 64): $_CO_OUT"; exit 1; }
fi
echo "$_CO_OUT" | head -5
echo "$_CO_OUT" | grep -i "<module_name>" || echo "No tests found for <module_name>"

[ "${SKIP_COV}" -eq 0 ] && { python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest -- --cov=<target_module> -q --cov-report=term-missing || true; }
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

**Team mode branch** — if `TEAM_MODE=true`: Steps 1–2 complete solo (teammates need scope + coverage context). Follow the serialized teammate protocol now; skip Steps 3–4 and Step 5's local review (sw-engineer runs them); parent runs Step 5's bounded independent implementation challenge; after results run Step 5's quality stack and final gate, then the Final Report.

> loads: team-mode.md — gated; ~90% of runs (`--team` absent) skip the load entirely

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false
[ "$TEAM_MODE" = "true" ] && python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$PLUGIN_ROOT/skills/refactor/modes/team-mode.md"
```

Continue to Step 3 only when `TEAM_MODE=false`.

```bash
# boundary 1: after coverage audit, before characterization tests (compaction-contract.md)
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_PRESERVE="dev-dir=$_DEV_DIR, plan-file=${_PLAN_FILE:-none}, pytest-cmd=$_PYTEST_CMD"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-sentinel dev-refactor-keep-items "develop:refactor" "characterize+edit (after coverage audit)" "$_DEV_DIR" "$_PRESERVE" "add characterization tests (Step 3) → refactor with safety net (Step 4)" -- calls_ahead=30 "drop=scope exploration and coverage analysis"  # timeout: 5000
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
python "$PLUGIN_ROOT/bin/compaction_boundary.py" "$_DEV_SHARED"
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
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_run_tests.py" pytest -- <test_file> -v; GATE_EXIT=$?
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

On accepted (b) after a red characterization run, use the Write tool to save its exact repository-relative test path as one line in `$DEV_DIR/accepted-red-characterization.txt`. This file records only that explicitly accepted failing characterization file; exclude it from the Pre-edit baseline and Gate T2, and retain the missing safety net in the Final Report. A runner error is not a red characterization run; recover the runner first.

## Step 4: Refactor with safety net

**Pre-edit baseline** — after characterization tests and before the first source edit, run the Step 4 selector once against the Python files identified in Step 1. Team mode's parent runs this same block after T1 and before spawning the implementation teammate. Replace the entire `'<scoped_python_files>'` token with the explicit, individually shell-quoted repository-relative paths from that scope, not the current dirty diff. Include newly added characterization test paths so an initially untested module has a runnable baseline; the accepted-red file, when recorded above, is filtered out of the selected tests before execution. A coverage run is not this baseline. A nonempty completed green run of the remaining scoped tests is required for both solo and team mode:

```bash
# timeout: 600000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r DEV_DIR < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" 2>/dev/null || DEV_DIR=""
[ -n "$DEV_DIR" ] || { echo "! BLOCKED — refactor run directory missing; re-run Checkpoint init"; exit 1; }
set -- '<scoped_python_files>'
case "$*" in *'<'*'>'*|"") echo "! BLOCKED — scoped file list contains an unresolved placeholder; substitute the Step 1 paths before running the baseline"; exit 1 ;; esac
IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r ACCEPTED_RED_CHAR 2>/dev/null < "$DEV_DIR/accepted-red-characterization.txt" || ACCEPTED_RED_CHAR=""
ACCEPTED_RED_CHAR=${ACCEPTED_RED_CHAR%$'\r'}
if [ -n "$ACCEPTED_RED_CHAR" ]; then
    echo "BASELINE: accepted-red characterization test excluded: $ACCEPTED_RED_CHAR; remaining source tests must pass"
    python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill refactor --files "$@" > "$DEV_DIR/baseline-selection.json" || { echo "! BLOCKED — baseline selection failed; recover the runner before editing"; exit 1; }
    python - "$DEV_DIR/baseline-selection.json" "$ACCEPTED_RED_CHAR" "$DEV_DIR/baseline-tests.json" <<'PY' || { echo "! BLOCKED — baseline filtering failed; recover test selection before editing"; exit 1; }
import json
import sys
from pathlib import Path

selection, excluded, output = sys.argv[1:]
tests = json.loads(Path(selection).read_text(encoding="utf-8"))["tests"]
remaining = [test for test in tests if Path(test).resolve() != Path(excluded).resolve()]
Path(output).write_text(json.dumps(remaining) + "\n", encoding="utf-8")
PY
    python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill refactor --tests-from "$DEV_DIR/baseline-tests.json" --record-dir "$DEV_DIR" --run
else
    python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill refactor --files "$@" --record-dir "$DEV_DIR" --run
fi
BASELINE_EXIT=$?
case "$BASELINE_EXIT" in
    0) python "$PLUGIN_ROOT/bin/dev_test_targets.py" --check-record "$DEV_DIR/test-targets.jsonl" --expect-exit 0 || { echo "! BLOCKED — no completed nonempty baseline; resolve test selection before editing"; exit 1; } ;;
    1) python "$PLUGIN_ROOT/bin/dev_test_targets.py" --check-record "$DEV_DIR/test-targets.jsonl" --expect-exit 1 || { echo "! BLOCKED — baseline runner failed without a completed red run; recover the runner before editing"; exit 1; }
       echo "! BLOCKED — pre-existing test failure in the remaining scoped tests; stop refactoring and recommend /develop:fix before any source edit"; exit 1 ;;
    *) echo "! BLOCKED — baseline runner or collection failed (exit $BASELINE_EXIT); recover the runner before editing"; exit "$BASELINE_EXIT" ;;
esac
echo "BASELINE OK: scoped tests pass before source edits"
```

Any `! BLOCKED` result → stop before source edits or the implementation teammate spawn, and do not revert pre-existing failures.

**Test-run policy** — inside the Step 4 refactor loop and the Step 5 review loop, tests run targeted only, never widened to the full suite. The full suite runs at the final gate once the loops are clean, and again after any fix to a full-suite failure (§Final gate).

**Batch mode gate** — default on, `--no-batch` opts out; groups non-overlapping change-test edits into fewer test runs (`_shared/batch-mode.md` §Batch mode: non-overlap predicate, per-edit snapshot/bisect design, cap accounting):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
if python "$PLUGIN_ROOT/bin/dev_batch_gate.py" refactor; then python "$PLUGIN_ROOT/bin/dev_load_shared.py" batch-mode.md; fi  # timeout: 5000 — batch mode on: writes dev-batch-skill, which batch-mode.md's own fences read back (never a literal <skill> placeholder); off: prints the verdict, loads nothing — gate exits 1, `if` keeps the call at 0 so the opt-out never renders as an error
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
   PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
   if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
   python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill refactor --run
   ```
   - Selects per changed module via `codemap-py query test-impact` when available, path heuristics otherwise (name-matched tests plus tests importing the module); Step 3's characterization tests are selected as changed files. The first output line is the selection JSON — surface any `codemap.partial`/`codemap.fell_back` count and every `not_covered` entry (`⚠ not_covered — some changed code has no mapped test`) as a coverage caveat; the selection and full log are recorded under `$DEV_DIR/test-targets.jsonl`.
   - Empty selection (`note` set) → nothing ran; say so (Test-run policy).
3. Tests pass: proceed to next change
4. Tests fail: reverse only your own Edit against the current file, preserving pre-existing and concurrent edits; never use `git checkout` or `git restore` to revert a Step 4 change. If the current region no longer matches your edit, stop and reconcile ownership before undoing anything. Then try a different approach.

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
    exit 1
fi
ELAPSED=$(( $(date +%s) - START_TIME ))
if [ "$ELAPSED" -ge $MAX_WALL_SECONDS ]; then
    echo "⚠ wall-time cap reached (30 min) — stopping refactor loop"
    exit 1
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
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-refactor-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_CHANGED=$({ git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard; } | sort -u | tr '\n' ' ' | sed 's/ *$//')
python "$PLUGIN_ROOT/bin/write_skill_contract.py" "develop:refactor" "review+quality (after refactor edits applied)" "$_DEV_DIR" "dev-dir=$_DEV_DIR, changed-files=$_CHANGED, pytest-cmd=$_PYTEST_CMD" "review and close gaps (Step 5) → Final Report" -- calls_ahead=40 "drop=refactor exploration and intermediate test output"  # timeout: 5000
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
python "$PLUGIN_ROOT/bin/compaction_boundary.py" "$_DEV_SHARED"
```

## Step 5: Review and close gaps

Review the refactored code once and resolve feasible gaps. Only an explicitly requested iterative review-and-fix loop may repeat the steps below, up to 3 outer cycles. (Step 4's "max 5 change-test cycles" bound applies within each pass through Step 4, independent of outer loop.)

A request to repeat review and fix until a reviewer reports no findings or no security findings, or to resolve all findings; a second independent review round about to be dispatched for the same change; or a skill workflow repeating its review then fix cycle counts as an explicitly invoked loop from that moment: before the next reviewer dispatch, start ledger; follow `../../rules/_full/adversarial-loop.md` (helper output, stop/plateau rules, user's round budget).

**First pass (repeat only for an explicitly requested iterative loop):**

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
   PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
   if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
   python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill refactor --run
   ```

4. **Objective convergence check**: findings this cycle identical to previous (same locations, same issues) → declare convergence, exit — further cycles won't resolve; surface to user.

5. **Only nits remain** (variable naming, comment clarity, minor formatting): document in Follow-up, exit loop.

6. **Substantive gaps remain**: report unresolved gaps; start another cycle only for an explicitly requested loop (max 3 total).

**After 3 cycles**: substantive issues remain → stop, surface to user.

**Quality-stack load** — the shared loader resolves warm state or the installed cache and fails visibly when the required file is missing. Missing file → stop the skill; do not run the final gate or report completion until the installation is repaired.

```bash
# timeout: 5000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_load_shared.py" --qs-dir foundry--quality-stack.md  # timeout: 5000
```

**Bounded independent implementation challenge**: After implementation and before the final gate, unless the user explicitly passed `--no-challenge`, dispatch one independent reviewer through the permitted `foundry:challenger` or available `bridge:review` route; never fork the implementation conversation. Supply only the current scoped diff, task acceptance and relevant evidence, allowing targeted source inspection. Reuse still-valid independent coverage for this exact result instead of dispatching again. Validate findings, apply feasible authorized fixes, rerun affected checks and report unresolved findings or missing independent coverage. Do not start another challenge or convergence loop automatically; after fixes, honestly report that the revised result lacks independent coverage. If no independent route is available, report the gap and do not claim the challenge passed.

Execute Branch Safety Guard, Quality Stack, the bounded implementation challenge above, and Codex Mechanical Delegation; use Codex Pre-pass as that challenge only when selected, and run Progressive Review Loop only when explicitly requested — the quality stack's directory-wide pytest run counts as the final gate's full-suite run only when `$TEST_CMD` is that same pytest invocation (`pytest`, `python -m pytest`, `uv run pytest`, `poetry run pytest`); a `tox -q` or `make test` repository never ran its own command in the stack, so run the final gate once even when no code changed. Otherwise do not run it again unless code changes after it (§Final gate).

**Final gate — full suite with the repository's own command.** Runs when `$TEST_CMD` is not the stack's pytest invocation, and again after any code change that follows the quality stack's wide run — a Progressive Review fix, or any fix to a full-suite failure (the stack halting on a genuine failure, or this block failing) — a fix can break something outside the targeted set, and the stack's own re-checks are scoped. A suite that may outlive the ~10 min foreground cap runs as a background Bash call (`run_in_background: true`): end the turn, its exit re-invokes you, no polling. Non-zero → fix (rerun the failing node ids while fixing — never the verification), then rerun this block in full. Max 3 gate iterations; at the cap stop, surface the remaining failures, and never report the suite green. A green full suite is the last test evidence before the Final Report:

```bash
# timeout: 600000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/dev_run_tests.py" suite
```

## Final Report

**Plan drift check** — run first; any output means lead the report with that `! BLOCKED` line and let the user decide (detection only, never auto-revert):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/plan_hash.py" check  # timeout: 5000
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

Clear the contract in the current worktree before calling `ExitWorktree`. The contract reader in `rule-inject.js` resolves `.temp/state` from `process.cwd()`.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

**Worktree exit** — if `WORKTREE_ENABLED=true`: follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block (path · branch · merge hint) to the report. Never auto-merge, never `remove`.

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
