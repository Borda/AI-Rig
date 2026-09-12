---
name: feature
description: 'TDD-first feature development — crystallise API as a demo test, drive implementation to pass it, run quality stack and progressive review loop. TRIGGER when: user asks to build new functionality, add a capability, or implement a feature in a Python project; phrases: "add X", "implement Y", "build Z feature", "create a new module for". SKIP when: bug fixes (use `/develop:fix`); refactoring without new behaviour (use `/develop:refactor`); non-Python projects; `.claude/` config changes (use `/foundry:manage`).'
argument-hint: <goal> [--issue <N>] [--repo <owner/repo>] [--plan <path>] [--no-challenge] [--challenge] [--no-codemap] [--codemap] [--semble] [--team] [--worktree] [--accept-no-plan] [--keep "<items>"]
effort: xhigh
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, WebFetch, EnterWorktree, ExitWorktree
disable-model-invocation: true
---

<objective>

TDD-first feature development. Crystallise API as demo use-case test, drive implementation to pass it, close quality gaps with review, docs, quality stack.

NOT for:

- bug fixes (use `/develop:fix`)
- `.claude/` config changes (use `/foundry:manage` (requires foundry plugin))
- non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest; use language-native toolchain instead
- mixed refactor+feature tasks — run /develop:refactor first, then /develop:feature

</objective>

<compaction>

- Key boundary: end of Step 1 — scope analysis and plan complete, before Step 2 demo test writing.
- Second boundary: end of Step 3 — TDD loop complete, before Step 4 review/close gaps.
- Preserve at boundary 1: dev-dir (checkpoint.md), plan-file, scope from sw-engineer analysis, PYTEST_CMD, --keep items.
- Mid-loop refresh: after each Step 3 TDD cycle contract is rewritten with changed-files + checkpoint.md path — so mid-loop compaction resumes loop (re-run suite for green state) instead of restarting Step 2 demo.
- Preserve at boundary 2: dev-dir, changed files list, test outcomes, PYTEST_CMD.

</compaction>

<workflow>

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (mounted by develop plugin init) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_DEV_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" 2>/dev/null)  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
echo "$_DEV_SHARED" > "${TMPDIR:-/tmp}/dev-shared-${CSID}"  # cold resolve — every later block warm-reads this
# loads: compaction-contract.md
cat "$_DEV_SHARED/agent-resolution.md"
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:doc-scribe`, `foundry:linting-expert`, `foundry:challenger`.

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

**Optional `--plan <path>`**: if `$ARGUMENTS` contains `--plan <path>` (at any position), read plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — use to populate Step 1 analysis instead of cold codebase exploration. Skip agent feasibility re-check (already done in `/develop:plan`). Store plan path as `PLAN_FILE`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/preflight-helpers.md"
```

Execute --plan path extraction; sets `$PLAN_FILE`.

**Checkpoint init**: run block below to create `.developments/<TS>/` and capture path in `$DEV_DIR`. Write `checkpoint.md` inside `$DEV_DIR`. After each major step (1, 2, 3, 4, 5), append `step: N — completed` to `$DEV_DIR/checkpoint.md`. On skill start, check for existing `.developments/*/checkpoint.md` — if found, offer to resume from last completed step.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DEV_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_dir.py" 2>/dev/null)
echo "$DEV_DIR" > "${TMPDIR:-/tmp}/dev-feature-dev-dir-${CSID}"
```

## Flag parsing

Parse flags into actual shell variables (not prose) so downstream blocks see correct values. Persist to temp files for cross-block access (bash state lost between Bash() calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-feature "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
```

```bash
# timeout: 10000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill feature --write-files "$ARGUMENTS"
```

Downstream blocks read back, e.g. `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`.

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags worktree --value-flags issue "$ARGUMENTS")"  # timeout: 5000
ISSUE_REF="$VALUE_ISSUE"
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

If `ISSUE_REF` non-empty and issue fetch succeeded: include issue title, body, and labels in Step 1 scope analysis as pre-populated requirements context.

**Cross-repo adaptation** (when `REPO_NAME` set) — issue filed against different codebase. After fetching issue, Step 1 scope analysis must also:

1. Extract intent from issue — what problem does it solve in abstract terms, not just described implementation details (which assume upstream's structure)
2. Check local divergences: run `git log --oneline -10` and grep for symbols mentioned in issue; identify where local codebase differs structurally from what issue assumes
3. Produce adaptation plan: upstream intent → local implementation using local conventions, existing abstractions, and current code structure — never assume upstream approach ports directly

**Unsupported flag check** — after ALL supported flags extracted (including `--issue` from block above), scan `$ARGUMENTS` for remaining `--<token>` tokens not in supported list. Do NOT include `--issue` in "unknown" set — it is consumed in second parse block above. Supported: `--plan`, `--team`, `--worktree`, `--no-challenge`, `--challenge`, `--no-codemap`, `--codemap`, `--semble`, `--accept-no-plan`, `--issue`, `--repo`, `--keep`. If truly unknown token found: print `` ! Unknown flag(s): `--<token>`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

## Worktree isolation

> loads: worktree-isolation.md

When `--worktree` set, offload the whole run into an isolated git worktree — **before** codemap detection or any edit, so codemap scans + all mutations land in the worktree (per-worktree ephemeral index; parallel runs never share one index).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-feature-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/worktree-isolation.md"
```

`WORKTREE_ENABLED=true` → follow §Enter (call `EnterWorktree`, warm-start codemap). Else skip — run in main tree. Remember the branch for §Exit at Final Report.

**Codemap auto-detection** — run after flag parsing; reads raw value, normalizes to `true`/`false`, writes normalized result so downstream blocks see post-normalization state:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" feature) || exit 1
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

**Semble preflight** — if `SEMBLE_ENABLED=true`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/preflight-helpers.md"
```

Execute semble preflight if flag set.

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

`TEAM_MODE=true` → execute the loaded protocol now, then exit; do not continue to solo Steps 1-5. `TEAM_MODE=false` → nothing was loaded; skip to Step 1.

## Step 1: Understand purpose and scope

Gather full context before writing any code:

> **Argument type detection**: if `$ARGUMENTS` is positive integer (or prefixed with `#`, e.g. `#123`), treat as GitHub issue number and fetch with `gh issue view`. If text, treat as feature description.
>
> **Issue ID parsing rule**: `$ARGUMENTS` with all supported flags stripped is treated as a GitHub issue number only when the *entire* remaining string is digits (optionally prefixed with `#`, e.g. `123` or `#123`) — matched via `^#?[0-9]+$` against the full flag-stripped argument, not a leading digit run. A goal like `500 error handling` correctly stays descriptive text, not issue `#500`, because the full stripped string isn't digits-only.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# CLEAN_ARGS is the blob with every declared flag and its value removed — same strip as debug
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags no-challenge,challenge,team,worktree,no-codemap,codemap,semble,accept-no-plan --value-flags issue,repo,plan "$ARGUMENTS")"  # timeout: 5000
if [[ "$CLEAN_ARGS" =~ ^#?[0-9]+$ ]]; then
  python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_issue_fetch_wrap.py" feature "$ARGUMENTS"  # timeout: 6000
  ISSUE_FETCH_EXIT=$?
  [ "$ISSUE_FETCH_EXIT" -ne 0 ] && echo "⚠ issue_fetch.py failed (exit $ISSUE_FETCH_EXIT) — proceeding without issue context"
fi
```

If free-text description provided: use Grep tool (pattern `<keyword>`, glob `**/*.py`) to search related code. Path hint: use `src/` if that directory exists, otherwise search from project root (`.`).

**Codemap target derivation** — when feature extends an existing module or modifies an existing function, pre-set `TARGET_MODULE`/`TARGET_FN` so `codemap-context.md` runs caller-impact queries (`rdeps` module importers, `fn-rdeps` function callers) before implementation, surfacing who breaks if existing surface changes. Goal may name extension point as `module.path` or `module.path::function`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags worktree --value-flags plan,issue,repo "$ARGUMENTS")"  # timeout: 5000 — CLEAN_ARGS only; a flag value like `--plan x.md` would otherwise outrank the goal's module
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/derive_codemap_target.py" "$CLEAN_ARGS")"  # timeout: 5000 — module.path or module.path::fn; both empty for net-new, so only central baseline runs
export TARGET_MODULE TARGET_FN
echo "$TARGET_MODULE" > ${TMPDIR:-/tmp}/dev-feature-target-module-${CSID}   # persist — reloaded by rdeps block (bash state lost between Bash() calls)
echo "$TARGET_FN"     > ${TMPDIR:-/tmp}/dev-feature-target-fn-${CSID}
```

> Pure net-new feature (no existing module/function named) → both empty → only `central` baseline runs, which is correct: nothing to compute caller impact against yet.

**Module-importer impact** — when `CODEMAP_ENABLED=true` and `TARGET_MODULE` set, run `rdeps` for modules that import extension target, so implementation accounts for downstream importers before changing surface:

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-feature-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r TARGET_MODULE < "${TMPDIR:-/tmp}/dev-feature-target-module-${CSID}" 2>/dev/null || TARGET_MODULE=""   # re-derive — bash state lost between Bash() calls
if [ "$CODEMAP_ENABLED" = "true" ] && [ -n "$TARGET_MODULE" ] && command -v codemap-py >/dev/null 2>&1; then
    codemap-py query --timeout 5 rdeps "$TARGET_MODULE" --top 10 --exclude-tests 2>/dev/null || true
fi
```

**If `CODEMAP_ENABLED=true` or `SEMBLE_ENABLED=true`** (codemap normalized by `bin/codemap_resolve.py`; semble verified by `preflight-helpers.md` §Semble preflight):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codemap-context.md"
```

Follow enabled sections (codemap block if `CODEMAP_ENABLED`, semble companion if `SEMBLE_ENABLED`). Skip entirely if both flags false.

Spawn **foundry:sw-engineer** agent to analyse codebase and produce:

- **Purpose**: what problem does feature solve, and for which users?
- **Scope**: which files and modules likely change (entry points, data models, tests)?
- **Compatibility**: does feature touch public API? Require deprecation? Need backward-compat shims?
- **Reuse opportunities**: existing utilities, base classes, patterns, abstractions new code can extend instead of duplicate
- **Risks**: edge cases, performance implications, integration points needing careful handling
- **Scope challenge**: Right problem? Simpler alternatives? What already exists that could extend instead of build from scratch?
- **Complexity smell**: if proposed change touches 8+ files or introduces 2+ new classes/modules, flag explicitly — scope may need narrowing before proceeding

**Complexity classification**: classify as `small` (≤3 files, single concern), `medium` (4–7 files, or 1 new module), or `large` (8+ files, 2+ new modules, or public API change).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/plan-inline.md"
```

§Inline Plan Generation Protocol. Apply using **feature** context from Skill contexts table. On proceed: set `PLAN_FILE=<path>`; continue to Step 2. On small complexity or `ACCEPT_NO_PLAN=true`: skip and continue to Step 2.

Present analysis summary before proceeding.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/premise-grounding.md"
```

§Premise Grounding Gate. Apply using **feature** context from Skill contexts table.

### Source Verification (optional — when using external APIs or version-sensitive libraries)

Skip if feature calls no external library APIs — no new framework features, no third-party SDK methods, no stdlib functions changed in recent Python version.

**Trigger**: feature calls external library API — new framework feature, third-party SDK method, or stdlib function changed in recent Python version.

**DETECT → FETCH → CITE pipeline:**

1. **DETECT** — read `pyproject.toml` or `requirements*.txt` for exact version and output:

   ```markdown
   STACK DETECTED:
   - <library> <exact-version> (from pyproject.toml)
   → Fetching official docs for the relevant API.
   ```

2. **FETCH** — use WebFetch to retrieve **specific relevant docs page** (not homepage). Source priority: official docs > official changelog/migration guide > web standards (MDN). Never cite Stack Overflow, blog posts, or AI training data.

   If WebFetch fails (network unavailable, site down): skip source verification entirely. Proceed to Step 2. Note in Final Report: "Source verification skipped — WebFetch unavailable."

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

**Decision — three states** (default is NOT "skip": it runs on substantial features and auto-skips only small ones):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**, any size.
2. else `--challenge` (`IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED=false` = `true`) → **always run**, even on a small feature.
3. else **default** → **run when feature is substantial** (multi-file, ≳50 lines, or adds any new public API — common case for a feature); **auto-skip when small** (single file, ≲50 lines, no new public API).

Both flags exist because they cover opposite regimes: `--no-challenge` suppresses gate on substantial features where it would otherwise fire; `--challenge` forces it on small features where it would otherwise auto-skip.

Spawn `foundry:challenger` with scope analysis from Step 1 (purpose, scope, risks, approach):

> "Review implementation approach and scope identified in Step 1. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:

- **Blockers found** → STOP. Present findings. Don't proceed to Step 2 until user resolves each blocker or explicitly accepts risk.
- **Concerns only** → surface as advisory section before demo test; continue.
- **No findings / all refuted** → proceed.

```bash
# boundary 1: after scope analysis, before demo/edit (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-feature-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/dev-feature-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_PRESERVE="dev-dir=$_DEV_DIR, plan-file=${_PLAN_FILE:-none}, pytest-cmd=$_PYTEST_CMD"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:feature" "demo+edit (after scope analysis and plan)" "$_DEV_DIR" "$_PRESERVE" "write demo test (Step 2) → TDD loop (Step 3) → review (Step 4)"  # timeout: 5000
```

## Step 2: Write a demo use-case

Before crystallising API, surface non-obvious design decisions:

> ASSUMPTIONS I'M MAKING:
>
> 1. [assumption about API shape, e.g. "returning a list not a generator"]
> 2. [assumption about caller context, e.g. "called once per batch, not per item"] → Correct me now or I'll proceed with these.

Don't proceed to demo if any assumption would materially change API shape.

Crystallise intended API contract before any implementation. Choose form based on scope:

> **Choosing demo form**: use inline doctest for simple functions/methods with minimal setup; use example script for features requiring external state, multiple steps, or side effects.

**Unit function / simple API** -> inline doctest (doctest in method docstring; must fail against current code).

**Complex feature** (setup required, side effects, multi-step flow) -> minimal example script `examples/demo_<feature>.py`; shows intended API end-to-end; becomes formal pytest test once implementation complete and API stable (end of Step 3).

Both forms must:

- Use **exact API** feature will expose (function name, signature, return type)
- Show happy-path end-to-end flow user would first reach for
- **Fail or error** against current code (feature doesn't exist yet)

**Gate**: demo must fail or error.

`<module>` is a **substitution token** — resolve actual module file path (e.g. `src/mypackage/feature.py`) into shell variable `$MODULE_PATH` before executing these blocks. Do NOT execute with literal `<module>.py` string — bash would interpret `<` as stdin redirect from a file named `module>.py`.

```bash
# Resolve MODULE_PATH before this block — e.g.:
# MODULE_PATH=$(find src/ -name '*.py' | head -1)
# timeout: 30000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
$PYTEST_CMD --collect-only --doctest-modules $MODULE_PATH -q 2>&1 | tail -5; COLLECT_EXIT=${PIPESTATUS[0]}
if [ "$COLLECT_EXIT" -eq 5 ]; then
    echo "⚠ GATE FAIL: no demo tests collected — demo file missing or doctest malformed"
    GATE_EXIT=1
elif [ "$COLLECT_EXIT" -ne 0 ]; then
    echo "⚠ Cannot collect doctests — check module for import errors (collect exit $COLLECT_EXIT)"
    GATE_EXIT=1
fi
echo "${GATE_EXIT:-0}" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
echo "$COLLECT_EXIT"   > ${TMPDIR:-/tmp}/dev-feature-collect-exit-${CSID}
```

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r COLLECT_EXIT < "${TMPDIR:-/tmp}/dev-feature-collect-exit-${CSID}" 2>/dev/null || COLLECT_EXIT="1"
IFS= read -r GATE_EXIT < "${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}" 2>/dev/null || GATE_EXIT="1"
# doctest form — MODULE_PATH resolved above
if [ "${COLLECT_EXIT:-1}" -eq 0 ]; then
    $PYTEST_CMD --doctest-modules $MODULE_PATH -v 2>&1 | tail -10; GATE_EXIT=${PIPESTATUS[0]}
    if [ "${GATE_EXIT:-0}" -eq 0 ]; then
        echo "⚠ GATE FAIL: demo passed (exit 0) — feature may already exist; revisit Step 1"
    else
        echo "✓ GATE OK: demo failed as expected (exit $GATE_EXIT)"
    fi
    echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
fi

# python examples/demo_<feature>.py 2>&1 | tail -5; GATE_EXIT=$?
# echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-feature-gate-exit-${CSID}
```

If `COLLECT_EXIT -ne 0`: stop — collection failed, gate skipped (GATE_EXIT=1). If `GATE_EXIT -eq 0`: invoke `AskUserQuestion` — do not silently proceed past a gate failure with prose alone: "Demo passed against current code — feature may already exist. How to proceed?" · (a) **Stop** — revisit Step 1 scope (recommended; feature likely already implemented) · (b) **Continue anyway** — proceed with TDD loop (gate explicitly overridden). On Stop: exit; do not advance to Step 3.

### Review: Validate the demo

Before proceeding to implementation, critically evaluate demo:

1. **Goal alignment**: does demo address user's stated goal, or slightly different problem?
2. **API design**: is proposed API minimal? Follows existing codebase conventions (naming, parameter order, return types)?
3. **Missing scenarios**: obvious happy-path variants or important failure modes demo doesn't cover?
4. **Testability**: can demo be automatically verified — not just `print`-and-inspect?

If issue found: revise demo and re-run gate. Don't proceed to Step 3 with flawed API contract — entire TDD loop anchored to this.

## Step 3: TDD implementation loop

**TDD test ownership**: lead (or foundry:sw-engineer if delegated) writes all red-green demo and TDD tests in Steps 2–3. foundry:qa-specialist must NOT write primary demo or red-green tests in any mode — qa-specialist adds edge-case, boundary, and regression tests after implementation complete (Step 4). Rule applies in both solo and team mode.

Drive implementation by making tests pass, one cycle at a time:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/run_pytest_short.py" "$PYTEST_CMD" <target_test_dir>  # timeout: 600000
GATE_EXIT=$?
```

**Gate**: all existing tests must pass before proceeding. If any fail, stop — don't add new code on broken baseline. Use `/develop:fix` to address pre-existing failures first, then return here.

> **Note on exit code 5**: `pytest` returns exit code 5 when no tests collected. Exit code 5 acceptable here — means no pre-existing tests exist yet, valid baseline for new feature. Proceed with TDD loop. Only exit codes 1, 2, 3, 4 indicate actual test failures.

(Use Glob tool — `pattern: **/test_*.py` — to discover test directories if `<target_test_dir>` unknown; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` first)

**Safety break** (mirrors refactor's guard — the only other bounded loop in this plugin): initialize cycle counter + wall clock via temp files (bash state lost between Bash() calls):

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "0"           > ${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}
echo "$(date +%s)" > ${TMPDIR:-/tmp}/dev-feature-tdd-start-${CSID}
```

Start from Step 2 demo — already failing, becomes first target. For each piece of functionality:

1. **Target demo or write next focused test** — first iteration uses Step 2 demo directly; subsequent iterations add one new test per piece of new behaviour

2. **Run existing suite — confirm all pass**:

   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <target_test_dir> -v 2>&1 | tail -20
   GATE_EXIT=${PIPESTATUS[0]}
   ```

3. **Run new demo/test — confirm it fails**:

   ```bash
   # timeout: 600000
   $PYTEST_CMD --doctest-modules <module>.py -v --tb=short 2>&1 | tail -10
   GATE_EXIT=${PIPESTATUS[0]}
   $PYTEST_CMD --tb=short <test_file>::<test_name> -v
   python examples/demo_<feature>.py 2>&1 | tail -5
   ```

4. **Implement minimal code** (spawn **foundry:sw-engineer** agent for non-trivial logic):

   - Reuse or extend existing code identified in Step 1 — prefer subclassing or composing over parallel reimplementation
   - Match project's existing patterns (naming, error handling, type annotations)

5. **Run demo/test — confirm it passes**

6. **Run affected tests** (prefer targeted over full suite):

   **Test impact (codemap-py)** — identify minimal test set first:

   ```bash
   codemap-py query test-impact "<changed_module>" 2>/dev/null
   ```

   - Non-empty `pytest_cmd` → run those tests first; surface `not_covered` caveat if present
   - Empty or `codemap-py query` absent → fall back to full suite below

   **Full suite fallback**:

   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <target_test_dir> -v
   ```

7. If regressions appear: fix before moving on — never carry forward broken suite

After each cycle, refresh compaction contract so a mid-loop compaction resumes TDD loop instead of restarting Step 2 demo:

```bash
# WHY: boundary-1 (Step 1) says next=Step 2 demo; skip this, mid-Step-3 compaction restarts demo — idempotent but wastes spawns+tests. checkpoint.md lists completed steps for resume.
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-feature-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/dev-feature-keep-items-${CSID}" 2>/dev/null || _KEEP=""
# tracked mods AND untracked new files — new TDD files untracked until staged; git diff alone drops them
_CHANGED=$( { git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } | sort -u | tr '\n' ' ' | sed 's/ *$//')
_PRESERVE="dev-dir=$_DEV_DIR, changed-files=$_CHANGED, pytest-cmd=$_PYTEST_CMD, plan-file=${_PLAN_FILE:-none}, checkpoint=$_DEV_DIR/checkpoint.md"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:feature" "TDD loop in progress (Step 3)" "$_DEV_DIR" "$_PRESERVE" "re-run suite to see current green state, then continue TDD for remaining behaviour — do NOT restart the Step 2 demo. checkpoint.md lists completed steps."  # timeout: 5000
```

At each cycle start, read back, increment, check — stop at `MAX_INNER_CYCLES=5` or 30-min wall cap; on trip: stop the loop, report what passed/failed/remains, invoke `AskUserQuestion` — (a) continue N more cycles · (b) re-scope · (c) stop here:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TDD_CYCLE < "${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}" 2>/dev/null || TDD_CYCLE="0"
IFS= read -r TDD_START < "${TMPDIR:-/tmp}/dev-feature-tdd-start-${CSID}" 2>/dev/null || TDD_START=$(date +%s)
TDD_CYCLE=$((TDD_CYCLE+1))
echo "$TDD_CYCLE" > ${TMPDIR:-/tmp}/dev-feature-tdd-cycle-${CSID}
MAX_INNER_CYCLES=5  # returns from Step 4 to Step 3 count as a cycle too
[ "$TDD_CYCLE" -gt $MAX_INNER_CYCLES ] && echo "⚠ MAX_INNER_CYCLES ($MAX_INNER_CYCLES) reached — stop TDD loop; surface state to user"
[ $(( $(date +%s) - TDD_START )) -ge 1800 ] && echo "⚠ wall-time cap reached (30 min) — stop TDD loop; surface state to user"
```

Repeat until all feature tests pass and Step 2 demo passes (or the safety break trips — a Step 4 return to Step 3 also increments the counter).

If Step 2 produced example script: promote into formal pytest test now that API is stable. Delete script once test in place.

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

- **Correctness**: matches exact API from Step 2? Edge cases and error paths covered?
- **Readability**: can another engineer understand feature without reading issue or demo?
- **Architecture**: fits established patterns? Abstraction level appropriate?
- **Security**: if feature touches input handling, auth, or data storage — are those paths hardened?
- **Performance**: N+1 patterns, unbounded collections, unnecessary computation introduced?

Use scan to prioritize which criteria below get deepest scrutiny.

1. Evaluate against all criteria:

   - **API match**: implementation matches exact API from Step 2 (name, signature, return type)
   - **Scope discipline**: only Step-1-identified files changed; no drive-by fixes or unrelated edits
   - **Edge cases**: error paths, boundary inputs, None/empty handling exercised by tests
   - **Test quality**: tests verify behavior (not implementation internals); parametrized where inputs vary
   - **Simplicity**: no dead code, unnecessary abstractions, over-engineering

2. For every gap found: implement fix immediately — add missing tests, remove dead code, revert out-of-scope edits. Return to Step 3 for substantive implementation gap needing new TDD cycle.

3. Re-run full suite to confirm nothing regressed:

   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <target_test_dir> -v 2>&1 | tail -20
   GATE_EXIT=${PIPESTATUS[0]}
   ```

   > **Objective convergence check**: if findings in this cycle identical to previous cycle (same locations, same issues), declare convergence and exit loop — further cycles won't resolve; surface to user.

4. **If only nits remain** (style, cosmetic naming, minor formatting): document in Follow-up and exit loop.

5. **If substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface to user before proceeding to Step 5.

When stopping with unresolved issues, use the **Incomplete Report Variant** from `${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/feature/templates/report-templates.md`.

## Step 5: Documentation

Spawn **foundry:doc-scribe** agent to update docstrings and README only (doc-scribe NOT-for: CHANGELOG — route separately):

- Add or update **docstrings** on new/modified functions and classes (Google style — Napoleon)
- Update module-level docstring if feature adds significant capability
- Add demo from Step 2 as doctest if not already embedded
- If feature changes public API: update `README.md` usage examples

Spawn doc-scribe with context:

- Affected files: [list from Step 1 scope analysis]
- New/modified public API: [function names, signatures from Step 3]
- Demo location: [Step 2 demo file path and function name]

Agent must Read each affected source file before writing docstrings — do not write placeholder content.

**CHANGELOG update** (separate from doc-scribe): after doc-scribe completes, lead appends the one-line entry to `CHANGELOG.md` under `Unreleased` directly via the Edit tool — feature name plus one-line description of the new capability. Never spawn an agent for this: a spawn costs ~120,851 tok of fixed overhead to write one line.

```bash
# timeout: 600000
$PYTEST_CMD --doctest-modules <target_module> -v 2>&1 | tail -20
GATE_EXIT=${PIPESTATUS[0]}
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
_SHARED="$_DEV_SHARED"  # foundry--quality-stack.md loads its siblings from $_SHARED — this plugin's own _shared
cat "$_DEV_SHARED/foundry--quality-stack.md"
```

Execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps. `foundry--quality-stack.md` ships in this plugin's own `_shared` (propagated foundry canonical, source-plugin prefix), so it is always present — absence means a broken install, not a missing optional dependency.

**Branch Safety Guard — no test suite**: if no test suite found (pytest collects 0 tests or `$TEST_CMD` not set), log `⚠ No test suite detected — Branch Safety Guard weakened` and require explicit user confirmation before proceeding past guard.

## Final Report

```bash
# loads: report-templates.md
_TPL="${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/feature/templates/report-templates.md"
cat "$_TPL"
```

§Standard Final Report — use as output structure.

**Worktree exit** — if `WORKTREE_ENABLED=true`: follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block (path · branch · merge hint) to the report. Never auto-merge, never `remove`.

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
| "The existing suite still passes — the feature is good" | Existing suite doesn't cover new feature. Demo and edge-case tests do. |
| "Step 1 analysis is unnecessary for a small addition" | Scope analysis reveals reuse opportunities and blast radius. Small additions regularly grow. |

</notes>
