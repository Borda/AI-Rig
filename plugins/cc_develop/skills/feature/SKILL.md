---
name: feature
description: 'TDD-first feature development — crystallise API as a demo test, drive implementation to pass it, run quality stack and progressive review loop. TRIGGER when: user asks to build new functionality, add a capability, or implement a feature in a Python project; phrases: "add X", "implement Y", "build Z feature", "create a new module for". SKIP when: bug fixes (use `/develop:fix`); refactoring without new behaviour (use `/develop:refactor`); non-Python projects; `.claude/` config changes (use `/foundry:manage`).'
argument-hint: <goal> [--issue <N>] [--repo <owner/repo>] [--plan <path>] [--no-challenge] [--challenge] [--no-codemap] [--codemap] [--team] [--worktree] [--no-batch] [--accept-no-plan] [--keep "<items>"]
effort: xhigh
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, WebFetch, EnterWorktree, ExitWorktree
disable-model-invocation: true
---

<objective>

TDD-first feature development. Crystallise API as demo use-case test, drive implementation to pass it, close quality gaps with review, docs, quality stack.

NOT for:

- bug fixes (use `/develop:fix`)
- `.claude/` config changes (use `/foundry:manage` (requires foundry plugin))
- non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest, use language-native toolchain instead
- mixed refactor+feature tasks — run /develop:refactor first, then /develop:feature

</objective>

<compaction>

- Key boundary: end of Step 1 — scope analysis, plan complete, before Step 2 demo test writing.
- Second boundary: end of Step 3 — TDD loop complete, before Step 4 review/close gaps.
- Preserve at boundary 1: dev-dir (checkpoint.md), plan-file, scope from sw-engineer analysis, PYTEST_CMD, --keep items.
- Mid-loop refresh: after each Step 3 TDD cycle, contract rewritten with changed-files + checkpoint.md path — so mid-loop compaction resumes loop (re-run suite for green state) instead of restarting Step 2 demo.
- Preserve at boundary 2: dev-dir, changed files list, test outcomes, PYTEST_CMD.

</compaction>

<workflow>

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (mounted by develop plugin init) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # a prior run's dir must not outlive a failed --init; agent-resolution.md §Agent waits recreates it when empty
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" --init  # timeout: 5000 — cold resolve: writes dev-shared (every later block warm-reads it) and a fresh dev-agent-watch-dir per run (agent-resolution.md §Agent waits)
# loads: compaction-contract.md
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" agent-resolution.md
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:doc-scribe`, `foundry:linting-expert`, `foundry:challenger`.

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

## Project Detection

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" runner-detection.md  # timeout: 5000
```

Sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

**Language preflight gate**: apply §Language preflight gate from `runner-detection.md` (loaded above) — sets `NON_PY` and runs the abort/continue question.

<!--
  NON_PY and MULTI_LANG gates are mutually exclusive — NON_PY fires only when no Python markers exist;
  MULTI_LANG fires only when Python markers AND non-Python markers coexist. Both cannot be true on the
  same repo; never reorder so MULTI_LANG runs before NON_PY.
-->

**Monorepo language-target gate**: if `NON_PY` empty (Python markers found) but non-Python markers also exist, confirm target language:

```bash
# timeout: 5000
MULTI_LANG=false
[ -f "pyproject.toml" ] && [ -f "package.json" ] && MULTI_LANG=true
[ -f "pyproject.toml" ] && [ -f "go.mod" ] && MULTI_LANG=true
[ -f "pyproject.toml" ] && [ -f "Cargo.toml" ] && MULTI_LANG=true
```

If `MULTI_LANG=true`: invoke `AskUserQuestion` — "Monorepo detected (Python + non-Python markers coexist). This skill targets Python/pytest. Is the feature you're building Python-only?" · (a) **Yes — Python only** — proceed · (b) **No — involves non-Python code too** — abort; use a language-native toolchain for the non-Python portion. On (b): stop.

**Optional `--plan <path>`**: `$ARGUMENTS` contains `--plan <path>` (at any position) → read plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — use to populate Step 1 analysis instead of cold codebase exploration. Skip agent feasibility re-check (already done in `/develop:plan`). Store plan path as `PLAN_FILE`. The extraction block also records the plan's hash (`.plans/` is gitignored, so edits leave no diff); Final Report re-checks it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" preflight-helpers.md  # timeout: 5000
```

Execute --plan path extraction; sets `$PLAN_FILE`.

**Checkpoint init**: run block below to create `.developments/<TS>/`, capture path in `$DEV_DIR`. That block is merged into §Flag parsing. Write `checkpoint.md` inside `$DEV_DIR`. After each major step (1, 2, 3, 4, 5), stage `step: N — completed` as a record in `$DEV_DIR/checkpoint.md.rec` (Write tool) and append it with `append_ledger.py` (Growing Ledgers `.rec` staging) inside the next real Bash block, never as a call of its own. On skill start, check for existing `.developments/*/checkpoint.md` — found → offer to resume from last completed step.

## Flag parsing

Parse flags into actual shell variables (not prose) so downstream blocks see correct values. Persist to temp files for cross-block access (bash state lost between Bash() calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DEV_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_dir.py" --persist feature)  # timeout: 5000 — also writes the dev-feature-dev-dir sentinel
: > "${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}"; : > "${TMPDIR:-/tmp}/dev-feature-tdd-start-${CSID}"  # fresh TDD caps per run; Step 3 init keeps them across Step 4→3 returns
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-feature "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill feature --write-files "$ARGUMENTS"  # timeout: 10000
```

Downstream blocks read back, e.g. `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`.

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# --keep-state: no eval; --inlined: Flag parsing already cleared
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags worktree --value-flags issue --keep-state dev-feature-args --inlined "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r ISSUE_REF < "${TMPDIR:-/tmp}/dev-feature-args-issue-${CSID}" 2>/dev/null || ISSUE_REF=""
echo "$ISSUE_REF" > "${TMPDIR:-/tmp}/dev-issue-ref-${CSID}"
if [ -n "$ISSUE_REF" ]; then
    IFS= read -r REPO_NAME < "${TMPDIR:-/tmp}/dev-upstream-${CSID}" 2>/dev/null || REPO_NAME=""
    if [ -n "$REPO_NAME" ]; then
        gh issue view "$ISSUE_REF" --repo "$REPO_NAME" 2>/dev/null || echo "⚠ Could not fetch issue $ISSUE_REF from $REPO_NAME — proceeding without issue context"
    else
        gh issue view "$ISSUE_REF" 2>/dev/null || echo "⚠ Could not fetch issue $ISSUE_REF — proceeding without issue context"
    fi
fi
```

If `ISSUE_REF` non-empty and issue fetch succeeded: include issue title, body, labels in Step 1 scope analysis as pre-populated requirements context.

**Cross-repo adaptation** (when `REPO_NAME` set) — issue filed against different codebase. After fetching issue, Step 1 scope analysis must also:

1. Extract intent from issue — what problem it solves in abstract terms, not just described implementation details (which assume upstream's structure)
2. Check local divergences: run `git log --oneline -10`, grep for symbols mentioned in issue; identify where local codebase differs structurally from what issue assumes
3. Produce adaptation plan: upstream intent → local implementation using local conventions, existing abstractions, current code structure — never assume upstream approach ports directly

**Unsupported flag check** — after ALL supported flags extracted (including `--issue` from block above), scan `$ARGUMENTS` for remaining `--<token>` tokens not in supported list. Do NOT include `--issue` in "unknown" set — it is consumed in second parse block above. Supported: `--plan`, `--team`, `--worktree`, `--no-batch`, `--no-challenge`, `--challenge`, `--no-codemap`, `--codemap`, `--accept-no-plan`, `--issue`, `--repo`, `--keep`. If truly unknown token found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

When `--worktree` set, offload the whole run into an isolated git worktree — **before** codemap detection or any edit, so codemap scans + all mutations land in the worktree (per-worktree ephemeral index; parallel runs never share one index).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-feature-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
if [ "$WORKTREE_ENABLED" = "true" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" worktree-isolation.md; fi
```

`WORKTREE_ENABLED=true` → follow §Enter (call `EnterWorktree`, warm-start codemap). Else skip — run in main tree (doc not loaded). Remember the branch for §Exit at Final Report.

**Codemap auto-detection** — run after flag parsing; reads raw value, normalizes to `true`/`false`, writes normalized result so downstream blocks see post-normalization state:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" feature) || exit 1
# codemap: integrated-via-shared
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-gates.md
```

> loads: codemap-gates.md

Follow Gate A and Gate B.

<!-- Only active when --team flag passed (~10% of invocations) -->

## Team Mode Branch

**Run immediately after flag parsing when `TEAM_MODE=true`. Runs Step 1 inline (teammates need scope context), then spawns parallel teammates for Steps 2-4. Exit after synthesis.**

> loads: team-mode.md — gated; ~90% of runs (`--team` absent) skip the load entirely

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false
[ "$TEAM_MODE" = "true" ] && cat "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/feature/modes/team-mode.md"
```

`TEAM_MODE=true` → execute the loaded protocol now (it runs Step 5's quality stack and final gate, then the Final Report), then exit; do not continue to solo Steps 1-5. `TEAM_MODE=false` → nothing was loaded; skip to Step 1.

## Step 1: Understand purpose and scope

Gather full context before writing any code:

> **Issue ID parsing rule**: `$ARGUMENTS` with all supported flags stripped is treated as a GitHub issue number only when the *entire* remaining string is digits (optionally prefixed with `#`, e.g. `123` or `#123`) — matched via `^#?[0-9]+$` against the full flag-stripped argument, not a leading digit run. A goal like `500 error handling` correctly stays descriptive text, not issue `#500`, because the full stripped string isn't digits-only.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/dev-feature-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""  # every declared flag already stripped by Flag parsing (dev_parse_args owns the flag list); a raw "--repo o/r 42" would fail the digits-only check
if [[ "$CLEAN_ARGS" =~ ^#?[0-9]+$ ]]; then
  python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_issue_fetch_wrap.py" feature "$CLEAN_ARGS"  # timeout: 6000
  ISSUE_FETCH_EXIT=$?
  [ "$ISSUE_FETCH_EXIT" -ne 0 ] && echo "⚠ issue_fetch.py failed (exit $ISSUE_FETCH_EXIT) — proceeding without issue context"
fi
```

If free-text description provided: use Grep tool (pattern `<keyword>`, glob `**/*.py`) to search related code. Path hint: use `src/` if that directory exists, otherwise search from project root (`.`).

**Codemap target derivation** — feature extends an existing module or modifies an existing function → pre-set `TARGET_MODULE`/`TARGET_FN` so `codemap-context.md` runs caller-impact queries (`rdeps` module importers, `fn-rdeps` function callers) before implementation, surfacing who breaks if existing surface changes. Goal may name extension point as `module.path` or `module.path::function`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/dev-feature-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""  # all declared flags stripped — a flag value like `--plan x.md` would otherwise outrank the goal's module
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/derive_codemap_target.py" --keep-state dev-feature -- "$CLEAN_ARGS" || exit $?  # timeout: 5000 — module.path or module.path::fn; both empty for net-new, so only central baseline runs
IFS= read -r TARGET_MODULE < "${TMPDIR:-/tmp}/dev-feature-target-module-${CSID}" 2>/dev/null || TARGET_MODULE=""
IFS= read -r TARGET_FN < "${TMPDIR:-/tmp}/dev-feature-target-fn-${CSID}" 2>/dev/null || TARGET_FN=""
export TARGET_MODULE TARGET_FN
```

> Pure net-new feature (no existing module/function named) → both empty → only `central` baseline runs, correct: nothing to compute caller impact against yet.

**Module-importer impact** — `CODEMAP_ENABLED=true` and `TARGET_MODULE` set → run `rdeps` for modules that import extension target, so implementation accounts for downstream importers before changing surface:

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-feature-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r TARGET_MODULE < "${TMPDIR:-/tmp}/dev-feature-target-module-${CSID}" 2>/dev/null || TARGET_MODULE=""   # re-derive — bash state lost between Bash() calls
if [ "$CODEMAP_ENABLED" = "true" ] && [ -n "$TARGET_MODULE" ] && command -v codemap-py >/dev/null 2>&1; then
    codemap-py query --timeout 5 rdeps "$TARGET_MODULE" --top 10 --exclude-tests 2>/dev/null || true
fi
```

**If `CODEMAP_ENABLED=true`** (normalized by `bin/codemap_resolve.py`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-feature-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
if [ "$CODEMAP_ENABLED" = "true" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-context.md; fi  # timeout: 5000
```

Follow the codemap block. Flag false → nothing loaded; skip entirely.

Arm batch `scope` in the spawn response (`agent-resolution.md` §Agent waits — no polling, per-agent deadlines). Spawn **foundry:sw-engineer** agent to analyse codebase, produce:

- **Purpose**: what problem does feature solve, for which users?
- **Scope**: which files and modules likely change (entry points, data models, tests)?
- **Compatibility**: does feature touch public API? Require deprecation? Need backward-compat shims?
- **Reuse opportunities**: existing utilities, base classes, patterns, abstractions new code can extend instead of duplicate
- **Risks**: edge cases, performance implications, integration points needing careful handling
- **Scope challenge**: right problem? Simpler alternatives? What already exists that could extend instead of build from scratch?
- **Complexity smell**: proposed change touches 8+ files or introduces 2+ new classes/modules → flag explicitly, scope may need narrowing before proceeding

Carry the analysis summary in the Final Report. Write the full analysis to `$DEV_DIR/analysis.md` first; when the next gate fires, print the analysis summary once as the last reply text before the question, as normal Markdown. The question text carries counts, headline and `→ $DEV_DIR/analysis.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview`; do not repeat the same analysis at later gates.

**Complexity classification**: classify as `small` (≤3 files, single concern), `medium` (4–7 files, or 1 new module), or `large` (8+ files, 2+ new modules, or public API change).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" plan-inline.md  # timeout: 5000
```

§Inline Plan Generation Protocol. Apply using **feature** context from Skill contexts table. On proceed: set `PLAN_FILE=<path>`; continue to Step 2. On small complexity or `ACCEPT_NO_PLAN=true`: skip and continue to Step 2.

Plan-inline skipped because `--plan` was supplied or `ACCEPT_NO_PLAN=true`, **and** complexity classified `large` → still surface the smell once: invoke `AskUserQuestion` — "Scope is large (8+ files or 2+ new modules) and the plan gate was bypassed. How to proceed?" · (a) **Narrow scope** (recommended) · (b) **Proceed** — scope accepted as-is.

**Goal classification gate**: after sw-engineer analysis completes, scan the goal text for mixed signals — goal contains both feature keywords (add, implement, new, support) AND refactor keywords (rename, extract, restructure, decouple, consolidate) → invoke `AskUserQuestion`: "Goal mixes feature work and refactoring — split into two runs." · (a) **Abort** — run `/develop:refactor` first, then `/develop:feature` · (b) **Continue as feature-only** — treat refactoring as out of scope.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" premise-grounding.md  # timeout: 5000
```

§Premise Grounding Gate. Apply using **feature** context from Skill contexts table.

### Source Verification (optional — when using external APIs or version-sensitive libraries)

Skip if feature calls no external library APIs — no new framework features, no third-party SDK methods, no stdlib functions changed in recent Python version.

**Trigger**: feature calls external library API — new framework feature, third-party SDK method, stdlib function changed in recent Python version.

**DETECT → FETCH → CITE pipeline:**

1. **DETECT** — read `pyproject.toml` or `requirements*.txt` for exact version and output:

   ```markdown
   STACK DETECTED:
   - <library> <exact-version> (from pyproject.toml)
   → Fetching official docs for the relevant API.
   ```

2. **FETCH** — use WebFetch to retrieve **specific relevant docs page** (not homepage). Source priority: official docs > official changelog/migration guide > web standards (MDN). Never cite Stack Overflow, blog posts, or AI training data.

   WebFetch fails (network unavailable, site down) → skip source verification entirely. Proceed to Step 2. Note in Final Report: "Source verification skipped — WebFetch unavailable."

3. **CITE** — when implementing, embed comment with source URL and key quoted passage:

   ```python
   # Docs: https://docs.example.com/v2/api/method
   # "The recommended pattern for X is Y" (v2.1 docs)
   ```

4. **Conflict** — if docs describe pattern conflicting with how codebase currently uses library:

   ```text
   CONFLICT DETECTED:
   Existing code uses <old pattern>.
   <library> <version> docs recommend <new pattern> for this use case.
   Options:
   A) Use the documented pattern (may require updating existing call sites)
   B) Match existing code (works but not idiomatic for this version)
   → Which approach?
   ```

## Challenger gate

**Decision** (default runs at any size — a small feature on the main path is as risky as a large one; size never skips the gate):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**.
2. else → **always run**. `--challenge` (`CHALLENGE_FORCED`) is accepted for compatibility and changes nothing.

Arm batch `challenge` in the spawn response (`agent-resolution.md` §Agent waits). Spawn `foundry:challenger` with scope analysis from Step 1 (purpose, scope, risks, approach):

> "Review implementation approach and scope identified in Step 1. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:

- **Blockers found** → STOP. Write the blocker findings (one line each: claim · file:line · evidence) to `$DEV_DIR/blockers.md` first; print them once as the last reply text before invoking `AskUserQuestion`, as normal Markdown. The question text carries the blocker count, headline and `→ $DEV_DIR/blockers.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview` — "Challenger raised N blocker(s) on the implementation approach. How to proceed?" · (a) **Revise scope** — return to Step 1 analysis with the blockers as input · (b) **Accept risk** — proceed to Step 2 with each blocker documented in the Final Report Follow-up · (c) **Abort**. On Abort: stop. Never proceed to Step 2 on prose alone.
- **Concerns only** → surface as advisory section before demo test; continue.
- **No findings / all refuted** → proceed.

```bash
# boundary 1: after scope analysis, before demo/edit (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-feature-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_PRESERVE="dev-dir=$_DEV_DIR, plan-file=${_PLAN_FILE:-none}, pytest-cmd=$_PYTEST_CMD"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" --keep-sentinel dev-feature-keep-items "develop:feature" "demo+edit (after scope analysis and plan)" "$_DEV_DIR" "$_PRESERVE" "write demo test (Step 2) → TDD loop (Step 3) → review (Step 4)"  # timeout: 5000
```

## Step 2: Write a demo use-case

Before crystallising API, surface non-obvious design decisions:

> ASSUMPTIONS I'M MAKING:
>
> 1. [assumption about API shape, e.g. "returning a list not a generator"]
> 2. [assumption about caller context, e.g. "called once per batch, not per item"] → Correct me now or I'll proceed with these.

Never proceed to demo if any assumption would materially change API shape.

Crystallise intended API contract before any implementation. Choose form based on scope:

> **Choosing demo form**: use inline doctest for simple functions/methods with minimal setup; use example script for features requiring external state, multiple steps, or side effects.

**Unit function / simple API** -> inline doctest (doctest in method docstring; must fail against current code).

**Complex feature** (setup required, side effects, multi-step flow) -> minimal example script `examples/demo_<feature>.py`; shows intended API end-to-end; becomes formal pytest test once implementation complete, API stable (end of Step 3).

Both forms must:

- Use **exact API** feature will expose (function name, signature, return type)
- Show happy-path end-to-end flow user would first reach for
- **Fail or error** against current code (feature doesn't exist yet)

**Gate**: demo must fail or error.

`<module>` is a **substitution token** — resolve actual module file path (e.g. `src/mypackage/feature.py`) into shell variable `$MODULE_PATH` before executing these blocks. Do NOT execute with literal `<module>.py` string — bash would interpret `<` as stdin redirect from a file named `module>.py`.

```bash
# Resolve MODULE_PATH before this block — e.g.:
# MODULE_PATH=$(find src/ -name '*.py' | head -1)
# DEMO_SCRIPT: examples/demo_<feature>.py for the Complex-feature form above; empty = doctest form
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
DEMO_SCRIPT=""
set -o pipefail  # PIPESTATUS is bash-only, absent under zsh (Claude Code's Bash tool login shell on macOS)
if [ -n "$DEMO_SCRIPT" ]; then
    COLLECT_EXIT=5
    echo "5" > ${TMPDIR:-/tmp}/dev-feature-collect-exit-${CSID}
    echo "0" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
    echo "$DEMO_SCRIPT" > ${TMPDIR:-/tmp}/dev-feature-demo-script-${CSID}
else
    [ -n "$PYTEST_CMD" ] && [ -n "$MODULE_PATH" ] || { echo "! BLOCKED — PYTEST_CMD or MODULE_PATH empty; re-run Project Detection and resolve MODULE_PATH"; exit 1; }
    echo "$MODULE_PATH" > ${TMPDIR:-/tmp}/dev-feature-module-path-${CSID}  # later blocks reload it — bash state is lost between calls
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest --tail 5 -- --collect-only --doctest-modules "$MODULE_PATH" -q; COLLECT_EXIT=$?
    if [ "$COLLECT_EXIT" -eq 5 ]; then
        echo "⚠ GATE FAIL: no demo tests collected — demo file missing or doctest malformed"
        GATE_EXIT=1
    elif [ "$COLLECT_EXIT" -ne 0 ]; then
        [ "$COLLECT_EXIT" -eq 64 ] && echo "! test runner refused to start (exit 64) — no test ran; re-run Project Detection"
        echo "⚠ Cannot collect doctests — check module for import errors (collect exit $COLLECT_EXIT)"
        GATE_EXIT=1
    fi
    echo "${GATE_EXIT:-0}" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
    echo "$COLLECT_EXIT"   > ${TMPDIR:-/tmp}/dev-feature-collect-exit-${CSID}
    : > ${TMPDIR:-/tmp}/dev-feature-demo-script-${CSID}
fi
# run — doctest form needs COLLECT_EXIT=0; example-script form has DEMO_SCRIPT set, COLLECT_EXIT=5 by design
if [ "${COLLECT_EXIT:-1}" -eq 0 ]; then
    VERDICT_FILE="${TMPDIR:-/tmp}/dev-tests-verdict-${CSID}"
    export DEV_TESTS_VERDICT_NONCE="$$.$RANDOM.$RANDOM"
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest --tail 10 -- --doctest-modules "$MODULE_PATH" -v; GATE_EXIT=$?
    VERDICT=""; [ -f "$VERDICT_FILE" ] && [ ! -L "$VERDICT_FILE" ] && [ -O "$VERDICT_FILE" ] && IFS= read -r VERDICT < "$VERDICT_FILE"
    # wrapper writes failed NONCE only for a pytest failure with a summary: exit 1 without it = interpreter died first, 70 = never a red demo
    [ "$GATE_EXIT" -eq 1 ] && [ "$VERDICT" != "failed $DEV_TESTS_VERDICT_NONCE" ] && { echo "! exit 1 but no failed verdict from the wrapper — no test ran"; GATE_EXIT=70; }
    # only 0 (passed) and 1 (failed = red) are verdicts; 64 runner refused, 2/3 interrupted/internal, 4 usage, 5 none collected are not a red demo — fail closed, never fall to GATE OK
    case "$GATE_EXIT" in 0|1) ;; *) echo "! GATE ERROR: runner exit $GATE_EXIT (refused to start, interrupted, internal or usage error) — not a red demo; fix PYTEST_CMD/MODULE_PATH"; echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}; exit 1;; esac
    if [ "${GATE_EXIT:-0}" -eq 0 ]; then
        echo "⚠ GATE FAIL: demo passed (exit 0) — feature may already exist; revisit Step 1"
    else
        echo "✓ GATE OK: demo failed as expected (exit $GATE_EXIT)"
    fi
    echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
elif [ -n "$DEMO_SCRIPT" ]; then
    # a broken interpreter exits 1 before the demo starts — prove python runs a script so exit 1 can only come from the demo
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_dir.py" --help > /dev/null 2>&1 || { echo "! GATE ERROR: python cannot start — demo not run; fix the interpreter"; exit 1; }
    # pipefail (set above) keeps the status of the demo, not of tail, and no log file is written under TMPDIR
    python "$DEMO_SCRIPT" 2>&1 | tail -5; GATE_EXIT=$?
    # only 0 (passed) and 1 (failed = red) are verdicts; a missing script (2), usage error or signal is not a red demo
    case "$GATE_EXIT" in 0|1) ;; *) echo "! GATE ERROR: demo script exit $GATE_EXIT (missing, interrupted or usage error) — not a red demo; fix DEMO_SCRIPT"; echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}; exit 1;; esac
    if [ "${GATE_EXIT:-0}" -eq 0 ]; then
        echo "⚠ GATE FAIL: demo passed (exit 0) — feature may already exist; revisit Step 1"
    else
        echo "✓ GATE OK: demo failed as expected (exit $GATE_EXIT)"
    fi
    echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
fi
if [ "$COLLECT_EXIT" -ne 0 ] && [ "$COLLECT_EXIT" -ne 5 ]; then
    echo "! GATE FAIL: demo collection failed (exit $COLLECT_EXIT) — fix the import error or malformed doctest before Step 3"
    exit 1
fi
```

If `COLLECT_EXIT -ne 0` and `DEMO_SCRIPT` is empty (doctest form): stop — collection failed, gate skipped (GATE_EXIT=1). Example-script form (`DEMO_SCRIPT` set) skips collection by design — `COLLECT_EXIT=5` there is expected, not a failure. If `GATE_EXIT -eq 0`: invoke `AskUserQuestion` — never silently proceed past a gate failure with prose alone: "Demo passed against current code — feature may already exist. How to proceed?" · (a) **Stop** — revisit Step 1 scope (recommended; feature likely already implemented) · (b) **Continue anyway** — proceed with TDD loop (gate explicitly overridden). On Stop: exit; never advance to Step 3.

### Review: Validate the demo

Before proceeding to implementation, critically evaluate demo:

1. **Goal alignment**: does demo address user's stated goal, or slightly different problem?
2. **API design**: is proposed API minimal? Follows existing codebase conventions (naming, parameter order, return types)?
3. **Missing scenarios**: obvious happy-path variants or important failure modes demo doesn't cover?
4. **Testability**: can demo be automatically verified — not just `print`-and-inspect?

If issue found: revise demo, re-run gate. Never proceed to Step 3 with flawed API contract — entire TDD loop anchored to this.

## Step 3: TDD implementation loop

**Test-run policy** — inside the Step 3 TDD loop and the Step 4 review loop, tests run targeted only (`dev_test_targets.py`), never the full directory. The full suite runs at the final gate once the loops are clean, and again after any fix to a full-suite failure (§Final gate).

**TDD test ownership**: lead (or foundry:sw-engineer if delegated) writes all red-green demo and TDD tests in Steps 2–3. foundry:qa-specialist must NOT write primary demo or red-green tests in any mode — qa-specialist adds edge-case, boundary, regression tests only after implementation is complete — team mode Wave 2, or the quality stack's Progressive Review when it flags test gaps; solo Step 4 spawns no qa-specialist. Rule applies in both solo and team mode.

Drive implementation by making tests pass, one cycle at a time:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
[ -n "$PYTEST_CMD" ] || { echo "! BLOCKED — PYTEST_CMD unresolved; re-run Project Detection"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/run_pytest_short.py" "$PYTEST_CMD" <target_test_dir>  # timeout: 600000
GATE_EXIT=$?
```

**Gate**: all existing tests must pass before proceeding. Any fail → stop — never add new code on broken baseline. Use `/develop:fix` to address pre-existing failures first, then return here.

> **Note on exit code 5**: `pytest` returns exit code 5 when no tests collected. Exit code 5 acceptable here — means no pre-existing tests exist yet, valid baseline for new feature. Proceed with TDD loop. Only exit codes 1, 2, 3, 4 indicate actual test failures.

(Use Glob tool — `pattern: **/test_*.py` — to discover test directories if `<target_test_dir>` unknown; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` first)

**Safety break** (mirrors refactor's guard — the only other bounded loop in this plugin): cycle counter + wall clock live in temp files (bash state lost between Bash() calls), initialized by the Batch mode gate block below.

**Batch mode gate** — default on, `--no-batch` opts out; groups non-overlapping edits into fewer test runs (`_shared/batch-mode.md` §Batch mode: non-overlap predicate, per-edit snapshot/bisect design, cap accounting); the same block initializes the Safety-break counters:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
if python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_batch_gate.py" feature; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" batch-mode.md; fi  # timeout: 5000 — batch mode on: writes dev-batch-skill, which batch-mode.md's own fences read back (never a literal <skill> placeholder); off: prints the verdict, loads nothing — gate exits 1, `if` keeps the call at 0 so the opt-out never renders as an error
[ -s "${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}" ] || echo "0"           > ${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}  # once per run (Flag parsing truncates): Step 4→3 returns keep counting
[ -s "${TMPDIR:-/tmp}/dev-feature-tdd-start-${CSID}" ] || echo "$(date +%s)" > ${TMPDIR:-/tmp}/dev-feature-tdd-start-${CSID}
```

Gate prints `batch mode: on` (default) and loads batch-mode.md → follow the loaded §Batch mode procedure (feature context) for this loop instead of running steps 1-7 below once per piece of functionality — group non-overlapping pieces into a batch, one run per batch, bisect on failure. Scope stays this loop only; the Step 4 review loop keeps its own unbatched `test-impact`-scoped re-runs. Gate prints `batch mode: off` (`--no-batch`; the call still exits 0) → continue below unchanged.

Start from Step 2 demo — already failing, becomes first target. For each piece of functionality (or, in batch mode, per non-overlapping batch member):

1. **Existing suite baseline** — the pre-loop gate above already ran `<target_test_dir>` before any edit (the Step 2 demo is a doctest or script, outside that directory); never re-run it here. Every cycle: baseline proven at cycle 0 by that gate; this cycle verifies through step 6's targeted run (Test-run policy).

2. **Target demo or write next focused test** — first iteration uses Step 2 demo directly; subsequent iterations add one new test per piece of new behaviour

3. **Run new demo/test — confirm it fails**:

   ```bash
   # timeout: 600000
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   set -o pipefail  # PIPESTATUS is bash-only, absent under zsh (Claude Code's Bash tool login shell on macOS)
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest --tail 10 -- --doctest-modules <module>.py -v --tb=short
   GATE_EXIT=$?
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest -- --tb=short <test_file>::<test_name> -v
   python examples/demo_<feature>.py 2>&1 | tail -5
   ```

4. **Implement minimal code** (arm batch `other` in the spawn response — `agent-resolution.md` §Agent waits — then spawn **foundry:sw-engineer** agent for non-trivial logic):

   - Reuse or extend existing code identified in Step 1 — prefer subclassing or composing over parallel reimplementation
   - Match project's existing patterns (naming, error handling, type annotations)

5. **Run demo/test — confirm it passes**

6. **Run affected tests** — targeted only (Test-run policy). One call selects and runs: `codemap-py query test-impact` per changed module when available, path heuristics otherwise (name-matched tests plus tests importing the module); this cycle's new tests are selected as changed files:

   ```bash
   # timeout: 600000
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_test_targets.py" --skill feature --run
   ```

   The first output line is the selection JSON — surface any `codemap.partial`/`codemap.fell_back` count and every `not_covered` entry (`⚠ not_covered — some changed code has no mapped test`) as a coverage caveat; the selection and full log are recorded under `$DEV_DIR/test-targets.jsonl`. Empty selection (`note` set) → say nothing ran beyond step 5.

7. Regressions appear → fix before moving on — never carry forward broken suite

After each cycle, refresh compaction contract so a mid-loop compaction resumes TDD loop instead of restarting Step 2 demo, then read back cycle count/start, increment, and check caps — stop at `MAX_INNER_CYCLES=5` or 30-min wall cap; on trip: stop loop, report what passed/failed/remains, invoke `AskUserQuestion` — (a) continue N more cycles · (b) re-scope · (c) stop here. With batch mode on, the loop counts **edits processed**, not passes through the loop: `BATCH_SIZE` is the number of non-overlapping members the just-closed batch contained (1 when batch mode is off or no batch formed — cap accounting then matches today's per-edit behavior exactly):

```bash
# boundary-1 (Step 1) says next=Step 2 demo; skip this and mid-Step-3 compaction restarts demo — idempotent but wastes spawns+tests. checkpoint.md lists completed steps for resume.
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-feature-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
# tracked mods AND untracked new files — new TDD files untracked until staged; git diff alone drops them
_CHANGED=$( { git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } | sort -u | tr '\n' ' ' | sed 's/ *$//')
_PRESERVE="dev-dir=$_DEV_DIR, changed-files=$_CHANGED, pytest-cmd=$_PYTEST_CMD, plan-file=${_PLAN_FILE:-none}, checkpoint=$_DEV_DIR/checkpoint.md"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" --keep-sentinel dev-feature-keep-items "develop:feature" "TDD loop in progress (Step 3)" "$_DEV_DIR" "$_PRESERVE" "re-run suite to see current green state, then continue TDD for remaining behaviour — do NOT restart the Step 2 demo. checkpoint.md lists completed steps."  # timeout: 5000
IFS= read -r TDD_CYCLE < "${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}" 2>/dev/null || TDD_CYCLE="0"
IFS= read -r TDD_START < "${TMPDIR:-/tmp}/dev-feature-tdd-start-${CSID}" 2>/dev/null || TDD_START=$(date +%s)
# BATCH_SIZE: written by batch-mode.md on batch close; read+reset to "1" in same fence — a separate
# clear fence is skipped by a cap/abort exit, leaving a stale value to double-count the next cycle.
IFS= read -r BATCH_SIZE < "${TMPDIR:-/tmp}/dev-feature-batch-size-${CSID}" 2>/dev/null || BATCH_SIZE=""
TDD_CYCLE=$((TDD_CYCLE + ${BATCH_SIZE:-1}))
echo "$TDD_CYCLE" > ${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}
echo "1" > "${TMPDIR:-/tmp}/dev-feature-batch-size-${CSID}"
MAX_INNER_CYCLES=5  # returns from Step 4 to Step 3 count as edits too (1 each, batch or not)
# -ge, not -gt: this fence runs at cycle END (merged into the post-cycle contract write), so the count
# already includes the cycle just finished. -gt here would let a 6th cycle run before tripping a cap of 5.
[ "$TDD_CYCLE" -ge $MAX_INNER_CYCLES ] && echo "⚠ MAX_INNER_CYCLES ($MAX_INNER_CYCLES) reached — stop TDD loop; surface state to user"
[ $(( $(date +%s) - TDD_START )) -ge 1800 ] && echo "⚠ wall-time cap reached (30 min) — stop TDD loop; surface state to user"
```

Repeat until all feature tests pass, Step 2 demo passes (or safety break trips — a Step 4 return to Step 3 also increments the counter).

If Step 2 produced example script: promote into formal pytest test now API is stable. Delete script once test in place.

```bash
# boundary 2: after TDD loop, before review stack (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-feature-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_CHANGED=$(git diff --name-only HEAD 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:feature" "review+quality (after TDD loop complete)" "$_DEV_DIR" "dev-dir=$_DEV_DIR, changed-files=$_CHANGED, pytest-cmd=$_PYTEST_CMD" "review and close gaps (Step 4) → docs (Step 5) → Final Report"  # timeout: 5000
```

## Step 4: Review and close gaps

Full review of implementation. **Loop** — review -> fix -> re-review until only nits remain. Maximum 3 cycles.

**Each cycle:**

**5-axis quality scan** — before full criteria evaluation, assess implementation on each axis:

- **Correctness**: matches exact API from Step 2? Edge cases, error paths covered?
- **Readability**: can another engineer understand feature without reading issue or demo?
- **Architecture**: fits established patterns? Abstraction level appropriate?
- **Security**: feature touches input handling, auth, or data storage → those paths hardened?
- **Performance**: N+1 patterns, unbounded collections, unnecessary computation introduced?

Use scan to prioritize which criteria below get deepest scrutiny.

1. Evaluate against all criteria:

   - **API match**: implementation matches exact API from Step 2 (name, signature, return type)
   - **Scope discipline**: only Step-1-identified files changed; no drive-by fixes, unrelated edits
   - **Edge cases**: error paths, boundary inputs, None/empty handling exercised by tests
   - **Test quality**: tests verify behavior (not implementation internals); parametrized where inputs vary
   - **Simplicity**: no dead code, unnecessary abstractions, over-engineering

2. Every gap found → implement fix immediately — add missing tests, remove dead code, revert out-of-scope edits. Return to Step 3 for substantive implementation gap needing new TDD cycle.

3. Re-run the tests this cycle's fixes touch to confirm nothing regressed — targeted only, same block as Step 3 item 6 (Test-run policy):

   ```bash
   # timeout: 600000
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_test_targets.py" --skill feature --run
   ```

   > **Objective convergence check**: findings in this cycle identical to previous cycle (same locations, same issues) → declare convergence, exit loop — further cycles won't resolve; surface to user.

4. **Only nits remain** (style, cosmetic naming, minor formatting) → document in Follow-up, exit loop.

5. **Substantive gaps remain** → start next cycle (max 3 total).

**After 3 cycles**: substantive issues remain → stop; they surface in the Incomplete Report Variant below (the final reply), not as reply text before Step 5's tool calls (text written before a tool call can arrive as an empty progress update).

When stopping with unresolved issues, use the **Incomplete Report Variant** from `${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/feature/templates/report-templates.md`.

## Step 5: Documentation

Arm batch `other` in the spawn response (`agent-resolution.md` §Agent waits). Spawn **foundry:doc-scribe** agent to update docstrings and README only (doc-scribe NOT-for: CHANGELOG — route separately):

- Add or update **docstrings** on new/modified functions and classes (Google style — Napoleon)
- Update module-level docstring if feature adds significant capability
- Add demo from Step 2 as doctest if not already embedded
- Feature changes public API → update `README.md` usage examples

Spawn doc-scribe with context:

- Affected files: [list from Step 1 scope analysis]
- New/modified public API: [function names, signatures from Step 3]
- Demo location: [Step 2 demo file path and function name]

Agent must Read each affected source file before writing docstrings — never write placeholder content.

**CHANGELOG update** (separate from doc-scribe): after doc-scribe completes, lead appends the one-line entry to `CHANGELOG.md` under `Unreleased` directly via the Edit tool — feature name plus one-line description of new capability. Never spawn an agent for this: a spawn costs ~120,851 tok fixed overhead to write one line.

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
set -o pipefail  # PIPESTATUS is bash-only, absent under zsh (Claude Code's Bash tool login shell on macOS)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" pytest --tail 20 -- --doctest-modules <target_module> -v
GATE_EXIT=$?
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
_SHARED="$_DEV_SHARED"  # foundry--quality-stack.md loads its siblings from $_SHARED — this plugin's own _shared
echo "$_SHARED" > "${TMPDIR:-/tmp}/qs-shared-dir-${CSID}"  # quality-stack fences reload it; missing = BLOCKED
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" foundry--quality-stack.md  # timeout: 5000
```

Execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps. `foundry--quality-stack.md` ships in this plugin's own `_shared` (propagated foundry canonical, source-plugin prefix), so it is always present — absence means a broken install, not a missing optional dependency. Its directory-wide pytest run counts as the final gate's first full-suite run only when `$TEST_CMD` is that same pytest invocation (`pytest`, `python -m pytest`, `uv run pytest`, `poetry run pytest`); a `tox -q` or `make test` repository never ran its own command in the stack, so run the block below once even when no code changed. It runs again after code changes (§Final gate rerun).

**Final gate rerun — full suite with the repository's own command**, after any code change that follows the quality stack's wide run — a Progressive Review fix, or any fix to a full-suite failure (the stack halting on a genuine failure, or this block failing) — a fix can break something outside the targeted set, and the stack's own re-checks are scoped. A suite that may outlive the ~10 min foreground cap runs as a background Bash call (`run_in_background: true`): end the turn, its exit re-invokes you, no polling. Non-zero → fix (rerun the failing node ids while fixing — never the verification), then rerun this block in full. Max 3 gate iterations; at the cap stop, surface the remaining failures, and never report the suite green. A green full suite is the last test evidence before the Final Report:

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" suite
```

**Branch Safety Guard — no test suite**: no test suite found (the full-suite run above exits 5 — pytest collected 0 tests; a missing test-command sentinel produces the unresolved notice above, never this branch) → log `⚠ No test suite detected — Branch Safety Guard weakened`, require explicit user confirmation before proceeding past guard.

## Final Report

**Plan drift check** — run first; any output means lead the report with that `! BLOCKED` line and let the user decide (detection only, never auto-revert):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/plan_hash.py" check  # timeout: 5000
```

```bash
# loads: report-templates.md
_TPL="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/feature/templates/report-templates.md"
cat "$_TPL"
```

§Standard Final Report — use as output structure.

**Worktree exit** — `WORKTREE_ENABLED=true` → follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append `Worktree` block (path · branch · merge hint) to report. Never auto-merge, never `remove`.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

<!-- Team spawn logic: see ## Team Mode Branch above -->

</workflow>

<notes>

<!-- Reference only — execution-dead at runtime; included for agent behavioral context -->

## Anti-Rationalizations

| Temptation | Reality |
| -- | -- |
| "The feature is clear — I can skip the demo and go straight to code" | Without crystallized API contract, implementation drifts. Demo = spec. |
| "I know this library — no need to check docs" | Training data contains deprecated patterns. One fetch prevents hours of rework. |
| "I'll write tests after the implementation is stable" | Tests drive design. Writing first reveals API problems before baked in. |
| "The existing suite still passes — the feature is good" | Existing suite doesn't cover new feature. Demo, edge-case tests do. |
| "Step 1 analysis is unnecessary for a small addition" | Scope analysis reveals reuse opportunities, blast radius. Small additions regularly grow. |

</notes>
