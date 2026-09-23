---
name: fix
description: 'Reproduce-first bug resolution — capture bug in failing regression test, apply minimal fix, run quality stack and review loop. TRIGGER when: user reports a bug, regression, or unexpected behaviour in Python code with a traceback, failing test, or issue number; phrases: "fix this bug", "repair X", "broken since Y", "test failing". SKIP when: CI-only failures without local traceback (use `/develop:debug` first); new features (use `/develop:feature`); `.claude/` config issues (use `/foundry:audit`); non-Python projects.'
argument-hint: '<symptom or issue # (plain 123 or #123)> [--repo <owner/repo>] [--plan <path>] [--diagnosis <path>] [--no-challenge] [--challenge] [--codemap] [--no-codemap] [--accept-no-plan] [--team] [--worktree] [--keep "<items>"]'
effort: medium
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, EnterWorktree, ExitWorktree
disable-model-invocation: true
---

<objective>

Reproduce-first bug resolution. Capture bug in failing regression test, apply minimal fix, verify via quality stack and review loop.

NOT for:

- CI-only failures with no local traceback — use `/develop:debug` first (`--ci-run <run-id>` for GitHub Actions logs)
- production incidents with no CI run or traceback (use `/foundry:investigate` (requires foundry plugin))
- `.claude/` config issues (use `/foundry:audit` (requires foundry plugin))
- non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest; use language-native toolchain instead
- CSS/JS-only frontend changes (no Python source touched) — use `/develop:feature` for new frontend work, or direct editing for surgical CSS/JS fixes; this skill's regression-test gate assumes pytest

</objective>

<compaction>

- Key boundary: end of Step 2 — reproduction test written and failing, before Step 3 code edits.
- Second boundary: end of Step 3 — fix applied and regression test passing, before Step 4 review stack.
- Preserve at boundary 1: dev-dir, regression test path, root cause summary, plan-file, --keep items.
- Preserve at boundary 2: dev-dir, changed files list, test outcomes, regression test path.

</compaction>

<workflow>

<!-- Agent Resolution: resolved at runtime via $_DEV_SHARED; source at plugins/cc_develop/skills/_shared/agent-resolution.md -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_DEV_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" 2>/dev/null)  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
echo "$_DEV_SHARED" > "${TMPDIR:-/tmp}/dev-shared-${CSID}"  # cold resolve — every later block warm-reads this
# loads: compaction-contract.md
cat "$_DEV_SHARED/agent-resolution.md"
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist` (conditional — outcome C only), `foundry:challenger`.

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

**Optional `--plan <path>`**: `$ARGUMENTS` contains `--plan <path>` (any position) → read plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — populate Step 1 analysis instead of cold codebase exploration. Skip agent feasibility re-check (already done in `/develop:plan`). Store plan path as `PLAN_FILE`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/preflight-helpers.md"
```

Execute --plan path extraction; sets `$PLAN_FILE`.

**Checkpoint init**: creates `.developments/<TS>/`, captures path. Write `checkpoint.md` inside `$DEV_DIR`. After each major step (1, 2, 3, 4), append `step: N — completed` to `$DEV_DIR/checkpoint.md`. On skill start, check for existing `.developments/*/checkpoint.md` — offer resume from last completed step if found.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DEV_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_dir.py" 2>/dev/null)
echo "$DEV_DIR" > "${TMPDIR:-/tmp}/dev-fix-dev-dir-${CSID}"
```

## Fix Mode

**Optional `--diagnosis <path>`**: provided (from preceding `/develop:debug` session) → read diagnosis file first. Skip Step 1 codebase analysis — root cause, suspect files, evidence pre-populated from diagnosis file. Challenger gate still applies: proceed from pre-populated root cause through challenger gate, then Step 2. Do NOT skip challenger gate — it reviews fix approach, not just root cause discovery. Skip only Step 1's **codebase analysis** (the sw-engineer spawn and its codemap queries) — every gate nested in Step 1 still runs against the pre-populated root cause: premise grounding, scope gate, inline plan generation, and the `## Challenger gate`. The cannot-reproduce gate is satisfied by the diagnosis file's Root Cause and Evidence sections; empty or absent → the gate fires as normal.

```bash
DIAG_FILE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/diagnosis_parse.py" "$ARGUMENTS" 2>&1) || { echo "$DIAG_FILE"; exit 1; }  # timeout: 5000
```

Diagnosis file format: see `/develop:debug` Final Report section for canonical field definitions (Root Cause, Suspect Files, Evidence).

## Flag parsing

Parse flags into shell variables (not prose) so downstream blocks see correct values. Persist to temp files for cross-block access (bash resets between calls):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-fix "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
```

```bash
# timeout: 10000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill fix --write-files "$ARGUMENTS"
```

Downstream blocks read back, e.g. `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`.

## Worktree isolation

> loads: worktree-isolation.md

`--worktree` set → offload the whole run into an isolated git worktree — **before** codemap resolve or any edit, so codemap scans + all mutations land in the worktree (per-worktree ephemeral index; parallel runs never share one index).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-fix-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/worktree-isolation.md"
```

`WORKTREE_ENABLED=true` → follow §Enter (call `EnterWorktree`, warm-start codemap). Else skip — run in main tree. Remember the branch for §Exit at Final Report.

**Codemap resolve** — `CODEMAP_RAW` already written to `${TMPDIR:-/tmp}/dev-fix-codemap-${CSID}` by flag-parsing block above (via `dev_parse_args.py --skill fix --write-files`). Read it back, then normalize via `codemap_resolve.py`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" fix) || exit 1
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

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens not in the supported list below. Found → print `` ! Unknown flag(s): `--<token>`. Supported: `--plan`, `--team`, `--worktree`, `--diagnosis`, `--no-challenge`, `--challenge`, `--codemap`, `--no-codemap`, `--accept-no-plan`, `--repo`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Preflight** — if `CODEMAP_ENABLED=true`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/preflight-helpers.md"
```

Execute codemap preflight if the flag is set.

<!-- Only active when --team flag passed (~10% of invocations) -->

## Team Mode Branch

**If `TEAM_MODE=true`**: execute team workflow now — do not proceed to Step 1.

> loads: team-mode.md — gated; ~90% of runs (`--team` absent) skip the load entirely

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false
[ "$TEAM_MODE" = "true" ] && cat "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/fix/modes/team-mode.md"
```

`TEAM_MODE=true` → execute the loaded protocol now, then re-enter the main workflow at §Premise Grounding Gate (the `premise-grounding.md` block above Step 2): run premise-grounding, inline plan generation, and the `## Challenger gate` against the **consensus root cause**, then continue at Step 2 (regression test). Do not re-enter at Step 1 — its sw-engineer analysis is what team mode replaced. `TEAM_MODE=false` → nothing was loaded; skip to Step 1.

## Step 1: Understand the problem

Gather all available context about bug:

> **Argument type detection**: if `$ARGUMENTS` is positive integer (or prefixed with `#`, e.g. `#123`), treat as GitHub issue number and fetch with `gh issue view`. If text (contains spaces, letters, or special chars), treat as symptom description.

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_issue_fetch_wrap.py" fix "$ARGUMENTS"
```

**Cross-repo adaptation** (`REPO_NAME` set) — issue filed against a different codebase. After fetching issue, analysis must:

1. Understand bug's root cause intent from issue body — not just symptoms or described fix (which may reference upstream structure)
2. Locate equivalent bug in LOCAL codebase — run Grep for relevant symbols/patterns; code paths may differ due to divergence
3. Treat upstream issue as context, not prescription — implement fix appropriate to local structure

If error message or pattern provided: use Grep tool (pattern `<error_pattern>`, path `.`) to search codebase for failing code path.

`<test_path>` is a **substitution token** — resolve failing test file/node (from `$ARGUMENTS` or fetched issue) into `TEST_PATH` before running; bash reads a literal `<...>` as stdin redirect. Redirect order is `>file 2>&1` (stdout to file, then stderr onto stdout) — reversed (`2>&1 >file`) loses stderr to terminal.

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
TEST_PATH=""   # REPLACE with the resolved failing test file/node before running this block — do not leave empty
if [ -z "$PYTEST_CMD" ] || [ -z "$TEST_PATH" ]; then
    echo "! Cannot run reproduction: PYTEST_CMD or TEST_PATH unresolved — resolve TEST_PATH from \$ARGUMENTS/issue before running"
else
    $PYTEST_CMD --tb=long "$TEST_PATH" -v >"${TMPDIR:-/tmp}/pytest-out.txt-${CSID}" 2>&1; PYTEST_EXIT=$?; tail -40 "${TMPDIR:-/tmp}/pytest-out.txt-${CSID}"; [ $PYTEST_EXIT -ne 0 ] && echo "PYTEST FAILED (exit $PYTEST_EXIT)"
fi
```

**Codemap route and target derivation** — resolve `CODEMAP_QUERY_KIND` before loading `codemap-context.md`. Use `skip` when request supplies exact file/symbol for a localized edit and no caller, dependency, blast-radius, test-impact, or coupling fact remains unresolved. Use `callers`, `blast`, `dependencies`, `test-impact`, `coupling`, or `central` for one matching unresolved fact; use `standard` when affected surface isn't yet bounded or needs symbol/import context. An explicit structural/tool request overrides `skip`. User may pass an explicit suspect as `module.path::function`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse_target_qname.py" --query-kind "" -- "$ARGUMENTS"  # REPLACE --query-kind "" with the route decided above; empty fails safe to standard
```

**If `CODEMAP_ENABLED=true`**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codemap-context.md"
```

Follow the codemap block. Skip entirely if the flag is false.

Spawn **foundry:sw-engineer** agent to analyze failing code path and identify:

- Root cause — what wrong and why (not just symptom)
- Entry point to failure — which modules does call cross?
- State mutation — what state changed along way?
- Invariant violated — what condition broke at failure point?
- Minimal code surface needing change — exact files and functions
- Related code possibly affected by fix — blast radius
- Recent commits touching this path (from git log output, if provided)

**Direct-caller impact** — `CODEMAP_ENABLED=true` and `TARGET_FN` NOT supplied via `$ARGUMENTS` → derive suspect qualified name from sw-engineer Step 1 finding (module/function named as minimal code surface), run `fn-rdeps` for direct callers — benchmarked far cheaper than a plain caller walk (94k vs 1M+ tokens, +40pp accuracy). Fires only when `TARGET_FN` NOT pre-set from args — pre-set case already covered by `fn-rdeps`/`fn-blast` queries inside shared `codemap-context.md`, which ran earlier using persisted `TARGET_MODULE`/`TARGET_FN`:

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED  < "${TMPDIR:-/tmp}/dev-fix-codemap-enabled-${CSID}"  2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r TARGET_FN        < "${TMPDIR:-/tmp}/dev-fix-target-fn-${CSID}"        2>/dev/null || TARGET_FN=""
IFS= read -r TARGET_MODULE    < "${TMPDIR:-/tmp}/dev-fix-target-module-${CSID}"    2>/dev/null || TARGET_MODULE=""
IFS= read -r TARGET_QUALIFIED < "${TMPDIR:-/tmp}/dev-fix-target-qualified-${CSID}" 2>/dev/null || TARGET_QUALIFIED=""
IFS= read -r DEV_DIR          < "${TMPDIR:-/tmp}/dev-fix-dev-dir-${CSID}"          2>/dev/null || DEV_DIR=""
if [ "$CODEMAP_ENABLED" = "true" ] && [ -z "$TARGET_FN" ] && command -v codemap-py >/dev/null 2>&1; then
    DERIVED_FN=$(grep -oE '[A-Za-z_][A-Za-z0-9_.]*::[A-Za-z_][A-Za-z0-9_]*' "$DEV_DIR/checkpoint.md" 2>/dev/null | head -1)
    if [ -n "$DERIVED_FN" ]; then
        TARGET_QUALIFIED="$DERIVED_FN"
        TARGET_MODULE="${DERIVED_FN%%::*}"
        TARGET_FN="${DERIVED_FN##*::}"    # bare fn — keep consistent with the arg-supplied path
        export TARGET_FN TARGET_MODULE TARGET_QUALIFIED
        codemap-py query --timeout 5 fn-rdeps "$TARGET_QUALIFIED" --exclude-tests 2>/dev/null \
            | tee "$DEV_DIR/fn-rdeps-output.txt" || true
    fi
fi
```

> Derived qualified name comes from whatever Step 1 recorded in `$DEV_DIR/checkpoint.md` (write suspect there as `module::function` when appending `step: 1 — completed`). No suspect in `module::function` form recorded → skip silently; `central` baseline already ran.

**Cannot-reproduce gate**: sw-engineer unable to identify root cause, traceback, or any failing test → invoke `AskUserQuestion` — do NOT proceed to Step 2 with no reproduction path:

- question: "Cannot confirm root cause from available information. How to proceed?"
- (a) Use `/develop:debug` — investigate interactively first
- (b) Provide additional context — user pastes traceback, logs, or minimal reproduction; after reply, re-run Step 1 analysis with new context same session (DMI: can't wait for next invocation; apply additional context inline)
- (c) Use `/foundry:investigate` (requires foundry plugin) — for production incidents with no CI trace Stop until user provides option (b) context or selects a redirect.

If root cause not definitively established after analysis, surface assumptions before proceeding:

> ASSUMPTIONS I'M MAKING:
>
> 1. [assumption about root cause]
> 2. [assumption about affected scope] -> Correct me now or I'll proceed with these.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/premise-grounding.md"
```

§Premise Grounding Gate. Apply using **fix** context from Skill contexts table.

**Scope gate**: root cause spans 3+ modules → flag complexity smell — options: "Narrow scope (Recommended)" / "Proceed anyway". Plan-inline gate below will also fire (medium/large classification) → do NOT open a separate window: defer this question, ask it in the SAME `AskUserQuestion` call as plan-inline's Proceed/Stop/Abort menu (both menus verbatim, two questions, one call — the two gates test overlapping "this is big" conditions back-to-back). Plan-inline not firing → single-question call here as usual.

**Non-Python surface check**: sw-engineer's "minimal code surface" names no `.py` file (CSS/JS/template only) → invoke `AskUserQuestion` — "No Python source in the identified change surface; this skill's regression-test gate assumes pytest. How to proceed?" · (a) **Abort** — edit directly, or use `/develop:feature` for new frontend work · (b) **Continue** — a Python-side test can still capture this. On Abort: stop.

**Complexity classification**: classify the fix as `small` (≤3 files, single concern), `medium` (4–7 files, or a cross-module change), or `large` (8+ files, or a public-API/behaviour change). The plan-inline gate below reads this classification.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/plan-inline.md"
```

§Inline Plan Generation Protocol. Apply using **fix** context from Skill contexts table. On proceed: set `PLAN_FILE=<path>`; continue to Step 2. On small complexity or `ACCEPT_NO_PLAN=true`: skip and continue to Step 2.

## Challenger gate

**Decision — three states** (default is NOT "skip": runs on substantial fixes, auto-skips only small ones):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**, any size.
2. else `--challenge` (`IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED=false` = `true`) → **always run**, even on a small fix.
3. else **default** → **run when fix is substantial** (multi-file, ≳50 lines, or touches public API); **auto-skip when small** (single file, ≲50 lines, no API change) — challenger adds little on trivial fixes.

Both flags exist for opposite regimes: `--no-challenge` suppresses gate on substantial fixes where it would otherwise fire; `--challenge` forces it on small fixes where it would otherwise auto-skip.

Spawn `foundry:challenger` with root cause analysis from Step 1 (root cause, blast radius, assumptions, approach):

> "Review root cause analysis and proposed fix approach. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:

- **Blockers found** → STOP. Present findings, then invoke `AskUserQuestion` — "Challenger raised N blocker(s) on the fix approach. How to proceed?" · (a) **Revise approach** — return to Step 1 analysis with the blockers as input · (b) **Accept risk** — proceed to Step 2 with each blocker documented in the Final Report Follow-up · (c) **Abort**. On Abort: stop. Never proceed to Step 2 on prose alone.
- **Concerns only** → surface as advisory; continue.
- **No findings / all refuted** → proceed.

## Step 2: Reproduce the bug

(Use Glob tool — `pattern: **/test_*.py` — to discover test directories if `<test_dir>` unknown; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` first)

### Part A — Test archaeology (before writing anything new)

1. Search for existing tests covering broken behavior:

   ```bash
   grep -r "<broken_symbol_or_error>" tests/ --include="*.py" -l
   grep -r "#<issue_number>" tests/ --include="*.py" -l
   ```

   Run any candidate tests found to see if they currently pass or fail:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/pytest_gate.py" "$PYTEST_CMD" <candidate_test_file>::<candidate_test_name>  # timeout: 120000
   ```

2. For each candidate test found — critically assess coverage quality:

   - Exercises exact failing path (correct inputs, correct assertions)?
   - Or a weak test — broad mocking, trivially happy-path, partial assertion — that deflected the problem rather than caught it?

3. Three outcomes from archaeology:

   - **A: Existing test fails already** → captures bug; use as-is; proceed to Step 3
   - **B: Existing test passes but is weak** (deflected problem) → fix existing test to properly reproduce; do NOT write new test; gate: test must fail after fix
   - **C: No relevant test found** → write new test (proceed to Part B)

Surface archaeology verdict before any writing:

> Found: `[test path or "none"]` — verdict: `[captures / weak-deflected / no test]`

### Part B — Write new reproduction test (only when outcome C)

Spawn **foundry:qa-specialist** agent (outcome C only — no existing tests found) to write two reproduction tests:

Spawn with context:

- Bug description: [symptom from $ARGUMENTS or issue]
- Failing output: [exact error/traceback captured in Step 1]
- Suspect files: [files identified by sw-engineer in Step 1]
- Expected behaviour: [what should happen]
- Actual behaviour: [what currently happens]

**Path 1 — Full user flow (integration demo)**

- Exercises complete user-reported scenario end-to-end
- No mocking of broken subsystem — real execution
- Confirms user-reported problem fully resolved
- Name: `test_<bug>_user_flow` or `test_<bug>_integration`
- Lives in `tests/integration/` or alongside existing integration tests

**Path 2 — Targeted unit test (fast iteration)**

- Minimal scope: isolates root cause directly
- Mock external dependencies; only broken unit under test is real
- Designed for quick re-run during fix iteration (sub-second)
- Name: `test_<bug>_unit` or `test_<bug>_regression`
- Lives next to broken module's existing unit tests
- Use `pytest.mark.parametrize` if bug affects multiple input patterns
- Add brief comment linking to issue if applicable (e.g., `# Regression test for #123`)

**When to skip Path 1**: bug purely internal (no user-facing flow exists) → document why, proceed with Path 2 only.

Both tests must **fail** against current code before proceeding. Check exit codes for each independently:

```bash
# timeout: 600000
$PYTEST_CMD --tb=short tests/integration/<test_file>::test_<bug>_user_flow -v
GATE_P1=$?
[ $GATE_P1 -eq 0 ] && echo "GATE FAIL (Path 1): test passed — bug not captured" || echo "GATE OK (Path 1): failed as expected (exit $GATE_P1)"

$PYTEST_CMD --tb=short <unit_test_file>::test_<bug>_unit -v
GATE_P2=$?
[ $GATE_P2 -eq 0 ] && echo "GATE FAIL (Path 2): test passed — bug not captured" || echo "GATE OK (Path 2): failed as expected (exit $GATE_P2)"
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "${GATE_P1:-1}" > ${TMPDIR:-/tmp}/dev-fix-gate-p1-${CSID}  # 1 = failed as expected; a skipped Path 1 must not read as a pass
echo "${GATE_P2:-1}" > ${TMPDIR:-/tmp}/dev-fix-gate-p2-${CSID}
```

Either gate exit is 0 → stop. Bug not reproduced on that path. Do not apply fix. DMI skill — stop enforced via bash gate check:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r GATE_P1 < "${TMPDIR:-/tmp}/dev-fix-gate-p1-${CSID}" 2>/dev/null || GATE_P1=1
IFS= read -r GATE_P2 < "${TMPDIR:-/tmp}/dev-fix-gate-p2-${CSID}" 2>/dev/null || GATE_P2=1
if [ "$GATE_P1" -eq 0 ] || [ "$GATE_P2" -eq 0 ]; then
    echo "! GATE FAIL: one or more reproduction tests passed — bug not captured; cannot apply fix against unverified bug"
    exit 1
fi
```

**Outcome B gate** (weak test fixed path): after fixing existing test, run it to confirm it now fails:

```bash
$PYTEST_CMD --tb=long <existing_test_file>::<existing_test_name> -v 2>&1 | tail -30; GATE_EXIT=${PIPESTATUS[0]}  # timeout: 30000
[ $GATE_EXIT -eq 0 ] && echo "GATE FAIL: fixed test still passes — weak test not corrected; revisit" || echo "GATE OK: fixed test fails as expected (exit $GATE_EXIT)"
```

**Outcome B failure-mode verification**: scan traceback output above for expected error string from reported symptom. Traceback lacks a recognizable match to reported bug symptom → surface: `⚠ Test fails but failure mode may differ from reported symptom — verify the test captures the actual bug before proceeding.`

### Review: Validate the reproduction

Before applying fix, critically evaluate reproduction test(s):

1. **Correct failure mode**: fails for right reason (actual bug), not setup issue?
2. **Isolation**: exercises exactly broken behavior, not too broadly?
3. **Minimal reproduction**: smallest test demonstrating failure?
4. **Parametrization**: key variants covered if bug spans multiple input patterns?
5. **Archaeology honesty**: if outcome B (weak test fixed), is test now harder to pass? Does it catch actual failure mode?

Issue found → revise test(s) before applying fix. Flawed reproduction = fix validated against wrong criteria.

```bash
# boundary 1: after reproduction, before edit (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-fix-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PLAN_FILE < "${TMPDIR:-/tmp}/dev-plan-file-${CSID}" 2>/dev/null || _PLAN_FILE=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/dev-fix-keep-items-${CSID}" 2>/dev/null || _KEEP=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_PRESERVE="dev-dir=$_DEV_DIR, plan-file=${_PLAN_FILE:-none}, pytest-cmd=$_PYTEST_CMD"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:fix" "edit (after reproduction test written)" "$_DEV_DIR" "$_PRESERVE" "apply minimal fix (Step 3) → review+quality stack (Step 4)"  # timeout: 5000
```

## Step 3: Apply the fix

**Breaking change gate**: before applying fix, assess whether fix introduces a breaking change.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/semver-rules.md" 2>/dev/null || echo "semver-rules.md unavailable — use standard SemVer rules"  # timeout: 5000
```

`semver-rules.md` loaded → use it for semver classification; else standard SemVer rules (BREAKING = major bump, new feature = minor, fix = patch). Breaking change definition: worked before → fails/behaves differently now → no prior warning/shim. Yes → stop, call `AskUserQuestion` before any edit. State: what worked before, what will break, why this fix approach needed. Proceed only on explicit user confirmation. One question per breaking change; group only when logically one atomic change. Prose question does NOT count — `AskUserQuestion` mandatory.

Make minimal change to fix root cause:

1. Edit only code necessary to resolve bug

2. Run regression test to confirm now passes:

   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <test_file>::<test_name> -v
   ```

3. Run affected tests (prefer targeted over full suite):

   **Test impact (codemap-py)** — derive minimal test set before running anything.

   **Reuse from diagnosis handoff first** — when invoked with `--diagnosis`, `/develop:debug` may have already run this query and written it into diagnosis file under `## Test Impact (codemap-py)`. Reuse it (one query total across debug→fix) only when still fresh — not `stale` and not older than current index:

   ````bash
   # timeout: 6000
   DIAG_FILE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/diagnosis_parse.py" "$ARGUMENTS" 2>/dev/null)  # re-derive — bash resets between calls
   REUSED_PYTEST_CMD=""
   if [ -n "$DIAG_FILE" ] && grep -q '^## Test Impact (codemap-py)' "$DIAG_FILE" 2>/dev/null; then
       # extract the fenced JSON block that follows the marker heading
       _TI_JSON=$(awk '/^## Test Impact \(codemap-py\)/{f=1} f&&/^```json/{g=1;next} f&&/^```/{g=0} g' "$DIAG_FILE")
       _HANDOFF_AT=$(grep -m1 'index_scanned_at:' "$DIAG_FILE" | sed 's/.*index_scanned_at:[[:space:]]*//')
       _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
       PROJ=$(basename "$_ROOT")   # raw basename — scanner writes it verbatim, never sanitized
       _LIVE_AT=$(grep -o '"scanned_at"[[:space:]]*:[[:space:]]*"[^"]*"' "${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}/${PROJ}.json" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
       case "$_TI_JSON" in *'"stale": true'*|*'"stale":true'*) _TI_STALE=1;; *) _TI_STALE=0;; esac
       if [ "$_TI_STALE" -eq 0 ] && [ -n "$_HANDOFF_AT" ] && [ "$_HANDOFF_AT" = "$_LIVE_AT" ]; then
           REUSED_PYTEST_CMD=$(printf '%s' "$_TI_JSON" | grep -o '"pytest_cmd"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/')
           echo "→ reusing test-impact from diagnosis handoff (index unchanged): $REUSED_PYTEST_CMD"
       else
           echo "→ diagnosis test-impact stale or index moved — re-querying live"
       fi
   fi
   ````

   **Live query** — run only when no fresh handoff result was reused (`REUSED_PYTEST_CMD` empty):

   ```bash
   codemap-py query test-impact "<changed_module::function or bare module>" 2>/dev/null
   ```

   - Reused `REUSED_PYTEST_CMD` non-empty, OR live result non-empty `pytest_cmd` → use it instead of full `<test_dir>` run; surface `not_covered` caveat if present
   - Result empty or `codemap-py query` absent → fall back to full directory below

   **Full suite fallback** (only when impact query returns empty or unavailable):

   ```bash
   # timeout: 600000
   $PYTEST_CMD --tb=short <test_dir> -v
   ```

   **If `<test_dir>` does not exist or has no tests beyond regression test**: run only regression test (already verified in Step 2). Note in Final Report: "No pre-existing test suite found — regression test is sole verification."

4. If existing tests break: fix has side effects — reconsider approach

```bash
# boundary 2: after fix applied, before review stack (compaction-contract.md)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-fix-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || _PYTEST_CMD=""
_CHANGED=$(git diff --name-only HEAD 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:fix" "review+quality (after fix applied)" "$_DEV_DIR" "dev-dir=$_DEV_DIR, changed-files=$_CHANGED, pytest-cmd=$_PYTEST_CMD" "review and close gaps (Step 4) → Final Report"  # timeout: 5000
```

## Step 4: Review and close gaps

Full review of fix. **Loop** — review -> fix -> re-review until only nits remain. Max 3 cycles.

**Each cycle:**

**5-axis quality scan** — before full criteria evaluation, assess fix on each axis:

- **Correctness**: addresses root cause (not symptom)? Edge cases covered?
- **Readability**: comprehensible without surrounding bug context?
- **Architecture**: fits existing patterns? New coupling introduced?
- **Security**: bug path touch input handling, auth, or data? If yes, addressed?
- **Performance**: fix introduce loops, queries, or calls in hot path?

Use scan to prioritize which criteria below get deepest scrutiny.

1. Evaluate against all criteria:

   - **Root cause**: fix addresses actual root cause, not just symptom
   - **Minimality**: smallest change resolving bug; no collateral edits
   - **Regression test quality**: test precisely isolates bug (fails before fix, passes after)
   - **Side effects**: full suite passes without new failures or unexpected warnings

2. Every gap found → implement fix immediately — tighten patch, remove collateral edits, adjust test. Return to Step 3 for gap requiring re-examined fix approach.

3. Re-run test suite:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/run_pytest_short.py" "$PYTEST_CMD" <test_dir>; PYTEST_EXIT=$?; [ $PYTEST_EXIT -ne 0 ] && echo "PYTEST FAILED (exit $PYTEST_EXIT)"  # timeout: 600000
   ```

4. **Adjacent bugs** (observation only): scan for similar patterns; document in Follow-up — don't fix here, avoids scope creep.

5. **Objective convergence check**: findings this cycle identical to previous (same locations, same issues) → declare convergence, exit — further cycles won't resolve; surface to user instead.

6. **Only nits remain**: document in Follow-up, exit loop.

7. **Substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface to user before proceeding.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
_SHARED="$_DEV_SHARED"  # foundry--quality-stack.md loads its siblings from $_SHARED — this plugin's own _shared
cat "$_DEV_SHARED/foundry--quality-stack.md"
```

Execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps. `foundry--quality-stack.md` ships in this plugin's own `_shared` (propagated foundry canonical, source-plugin prefix), so it is always present — absence means a broken install, not a missing optional dependency.

## Final Report

```markdown
## Fix Report: <bug summary>

### Root Cause
[1-2 sentence explanation of what was wrong and why]

### Regression Test
- File: <test_file>
- Test: <test_name>
- Confirms: [what behavior the test locks in]
- Disposition: keep if a test runner auto-discovers this file; otherwise add to Follow-up as a cleanup candidate

### Changes Made
| File | Change | Lines |
| --- | --- | --- |
| path/to/file.py | description of fix | -N/+M |

### Test Results
- Regression test: PASS
- Full suite: PASS (N tests)
- Lint: clean

### Follow-up
- [any related issues or code that should be reviewed]
- [no test runner → `rm <test_file>` — no test suite will re-execute it; it served the gate, now expendable. **Exception**: test introduced this session and definitively wrong → delete it. Never delete pre-existing regression tests — they represent captured behavior predating this session.]

## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [e.g., could not reproduce locally, partial traceback only, fix not runtime-tested]

**Refinements**: N passes.
```

**Worktree exit** — if `WORKTREE_ENABLED=true`: follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block (path · branch · merge hint) to the report. Never auto-merge, never `remove`.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

<!-- Team branching logic is inline above at ## Team Mode Branch — executed immediately when TEAM_MODE=true, before Step 1. When to pass --team: the user already expects an unclear root cause or a bug spanning 3+ modules. The branch runs before triage, so these are the user's expectations at invocation time, not conditions this skill evaluates. -->

</workflow>

<notes>

<!-- Reference only — execution-dead at runtime; included for agent behavioral context -->

## Anti-Rationalizations

| Temptation | Reality |
| -- | -- |
| "I already know root cause from symptom" | Unverified assumptions fix the wrong bug. Read code path first. |
| "Regression test can wait — add after fix" | Fix without failing test = unverifiable. Test proves bug existed. |
| "Clean up nearby code while here" | Scope creep produces side effects, obscures fix. Touch only root cause. |
| "Targeted test passes — sufficient" | Targeted test shows bug fixed; full suite shows nothing else broke. Both required. |
| "Fix obvious — Step 1 analysis overkill" | Obvious causes are often symptoms. Analysis reveals actual root cause, blast radius. |

</notes>
