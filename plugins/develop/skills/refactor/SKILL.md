---
name: refactor
description: Test-first refactoring — audit coverage, add characterization tests, apply changes with safety net, run quality stack and review loop.
argument-hint: <target file or directory> <goal> [--no-challenge]
effort: high
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Test-first refactoring. Audit coverage, add characterization tests if missing, apply changes with safety net.

NOT for: bug fixes (use `/develop:fix`); new features (use `/develop:feature`); `.claude/` config changes (use `/foundry:manage`).

</objective>

<constants>
- MAX_INNER_CYCLES: 5 (change-test cycles per outer session — Step 4 safety break)
- MAX_AGGREGATE_CYCLES: 10 (total change-test cycles across all outer cycles combined — Step 4 + Step 5)
</constants>

<workflow>

<!-- Agent Resolution: canonical table at plugins/develop/skills/_shared/agent-resolution.md -->

## Agent Resolution

```bash
# Locate develop plugin shared dir — installed first, local workspace fallback
_DEV_SHARED=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/develop/*/skills/_shared 2>/dev/null | head -1)
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/develop/skills/_shared"
```

Read `$_DEV_SHARED/agent-resolution.md`. Contains: foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`.

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), TaskCreate all steps before any other work. Mark each step in_progress when starting, completed when done.

## Anti-Rationalizations

| Temptation | Reality |
| --- | --- |
| "The code is simple enough — I can skip characterization tests" | Without a safety net, you cannot detect behaviour change. Characterization tests are the only proof nothing broke. |
| "I'll fix this adjacent bug while I'm in here" | Scope creep conflates history. Adjacent bugs go in Follow-up, not this session. |
| "The tests are too brittle — I'll refactor them as well" | Refactoring tests and production code simultaneously makes regressions unattributable. Fix tests first in a separate pass. |
| "I know the codebase — no need for coverage audit" | Untested edge cases are the most common refactoring breakage. The audit finds what you don't know you don't know. |
| "This is a small change — Step 4's max-5 cycles are overkill" | Simple changes have simple test loops. The guard costs nothing when not needed and prevents runaway sessions when it is. |

## Project Detection

Read `$_DEV_SHARED/runner-detection.md` — sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

**Optional `--plan <path>`**: if `$ARGUMENTS` ends with `--plan <path>`, read the plan file first. Extract `Affected files`, `Risks`, `Suggested approach` — use these to inform Step 1 scope analysis. Skip redundant codebase exploration for already-classified files. Store plan path as `PLAN_FILE`.

**Checkpoint init**: create `.developments/<TS>/checkpoint.md` (where `TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)`). After each major step (1, 2, 3, 4, 5), append `step: N — completed`. On skill start, check for existing `.developments/*/checkpoint.md` — offer resume from last completed step if found.

## Refactor Mode

## Flag parsing

**Set `CHALLENGE_ENABLED=true`**. If `--no-challenge` present in `$ARGUMENTS`, set `CHALLENGE_ENABLED=false`.

## Step 1: Scope and understand

Read target code, build mental model before touching anything.

If `<target>` is directory: use Glob tool (pattern `**/*.py`, path `<target>`) to enumerate Python files.

```bash
# Measure current state
find <target> -name '*.py' -exec wc -l {} + 2>/dev/null | tail -1
```

**Structural context** (codemap, if installed) — soft PATH check, silently skip if `scan-query` not found:

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 5
fi
```

If results returned: prepend `## Structural Context (codemap)` block to foundry:sw-engineer spawn prompt with hotspot JSON. If target maps to module in index, also include `scan-query deps <target_module>` (coupling) and `scan-query rdeps <target_module>` (blast radius). Derive `<target_module>` from target path: strip project root prefix, replace `/` with `.`, drop `.py` extension. If `scan-query` not found or index missing: proceed silently — don't mention codemap to user.


**Multi-file / API-change scope — extended codemap scan**: if target is a directory, spans multiple files, or the goal mentions renaming/restructuring public API (i.e., refactoring is NOT limited to internals of a single function or class with unchanged public interface):

```bash
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    # Reusability: who calls each affected module outside the refactoring scope
    for mod in <affected_modules>; do
        scan-query rdeps "$mod"
    done
    # Tightest coupling pairs — determines refactor sequence and what must change together
    scan-query coupled --top 10
fi
```

Include `## Scope & Reusability (codemap)` block in foundry:sw-engineer spawn prompt. If `rdeps` returns callers **outside** the refactoring scope: flag them explicitly — those callers must be updated or the refactoring silently breaks the public contract. If codemap not installed / index missing **and** scope is multi-file: warn user: "codemap unavailable — blast radius unknown; run `/codemap:scan` before proceeding with cross-module refactoring".

Spawn **foundry:sw-engineer** agent to analyze code and identify:

- Public API surface (functions, classes, methods external code calls)
- Internal complexity hotspots (cyclomatic complexity, deep nesting, long functions)
- Code smells relevant to stated goal
- Dependencies and coupling between modules
- **Complexity smell**: directory or cross-module scope — flag it; consider team mode

**Scope gate**: if target is directory-wide scope (10+ files) regardless of goal, flag complexity smell. Use `AskUserQuestion` with options: "Narrow scope (Recommended)" / "Proceed anyway".

## Challenger gate

**Skip if `CHALLENGE_ENABLED=false`.**

Spawn `foundry:challenger` with the scope analysis from Step 1 (affected files, dependencies, coupling, risks):

> "Review the refactoring scope and approach. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result:
- **Blockers found** → STOP. Present findings. Do not proceed to Step 2 until user resolves each blocker or explicitly accepts the risk.
- **Concerns only** → surface as advisory before coverage audit; continue.
- **No findings / all refuted** → proceed.

## Step 2: Audit test coverage

Find existing tests for target code:

Use Glob tool (pattern `**/test_*.py` or `**/*_test.py`), then Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those referencing target.

> (Use Glob tool — `pattern: **/test_*.py` — to discover test files; check `pyproject.toml` `[tool.pytest.ini_options] testpaths` for configured paths)

```bash
# Check pytest available
$PYTEST_CMD --co -q 2>&1 | head -5

# Check pytest-cov available
SKIP_COV=0
if ! python3 -c "import pytest_cov" 2>/dev/null; then
    echo "WARNING: pytest-cov not installed — coverage data unavailable; classifying all public functions as UNCOVERED (conservative)"
    SKIP_COV=1
fi

# Collect tests for target module
$PYTEST_CMD --co -q 2>&1 | grep -i "<module_name>" || echo "No tests found for <module_name>"

# Run coverage (only if pytest-cov available)
[ "${SKIP_COV}" -eq 0 ] && $PYTEST_CMD --cov=<target_module> -q --cov-report=term-missing
```

Classify each public function/method:

- **Covered**: at least one test for happy path + one edge case
- **Partially covered**: test exists but missing edge cases or failure paths
- **Uncovered**: no test

### Review: Validate the coverage audit

Before writing characterization tests, critically evaluate audit output:

1. **Completeness**: all public functions, methods, classes identified — including complex call paths?
2. **Classification accuracy**: each item correctly classified? Partial-covered often misclassified as covered.
3. **Refactor relevance**: uncovered/partial items in code paths refactoring will touch?
4. **Hidden dependencies**: integration points or cross-module calls audit may have missed?

If audit incomplete: re-examine before Step 3. Gaps found mid-refactoring (Step 4) costly.

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
# Run to confirm they pass against current code
$PYTEST_CMD <test_file> -v
```

**Gate**: all characterization tests must pass before proceeding. Check exit code:

```bash
GATE_EXIT=$?
if [ $GATE_EXIT -ne 0 ]; then
    echo "GATE FAIL: characterization test(s) failed (exit $GATE_EXIT) — fix the test, not the code"
    # The test is wrong if it fails on unmodified code
fi
echo "GATE OK: all characterization tests pass on unmodified code"
```

If `GATE_EXIT -ne 0`: the characterization test is wrong — it must document *current* behavior, not desired behavior. Fix the test to match what the code actually does, then re-run.

## Step 4: Refactor with safety net

For each change:

1. One focused change (single responsibility per edit)
2. Run test suite:
   ```bash
   $PYTEST_CMD --tb=short <test_files> -v
   ```
3. Tests pass: proceed to next change
4. Tests fail: revert, try different approach

**Safety break**: max 5 change-test cycles per session. After 5, stop — report what succeeded, what broke, what remains.

> **Aggregate ceiling**: max 10 total change-test cycles across all outer cycles combined (inner Step 4 + outer Step 5). After 10 total cycles, stop — report what succeeded, what remains, ask user whether to continue or accept current state.

**Refactoring categories:**

- **Logic simplification**: replace complex conditionals, flatten nesting, extract helpers
- **API cleanup**: rename for clarity, consolidate parameters, add type annotations
- **Structural**: extract classes/modules, reduce coupling, apply design patterns
- **Performance**: replace loops with vectorized ops, reduce allocations, batch I/O
- **Dead code removal**: remove unused imports, unreachable branches, commented-out code; scan `_`-prefixed functions with no call sites; flag public methods absent from `__init__.py` exports

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
   $PYTEST_CMD --tb=short <test_files> -v 2>&1 | tail -20
   ```

4. **Objective convergence check**: if findings this cycle are identical to the previous cycle (same locations, same issues), declare convergence and exit — further cycles will not resolve the issue; surface to user.

5. **Only nits remain** (variable naming, comment clarity, minor formatting): document in Follow-up, exit loop.

6. **Substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: substantive issues remain → stop, surface to user.

Read `.claude/skills/_shared/quality-stack.md` (if file not found → skip quality stack entirely, note "foundry plugin not installed — quality stack skipped" in Final Report) and execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps.

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
**Score**: 0.N — [high >=0.9 | moderate 0.8-0.9 | low <0.8]
**Gaps**: [e.g., coverage tool unavailable, some tests skipped]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: target is directory OR cross-module scope.

- **Teammate 1 (foundry:sw-engineer, model=opus)**: performs refactoring (Step 4)
- **Teammate 2 (foundry:qa-specialist, model=opus)**: writes characterization tests (Step 3) in parallel

**Coordination:**

1. Lead broadcasts Step 1+2 analysis: `{target: <path>, coverage: <summary>, goal: <stated goal>}`
2. QA writes characterization tests while SW prepares refactoring plan
3. **File locking**: teammates coordinate via TEAM_PROTOCOL.md to avoid editing same file simultaneously
4. Lead synthesizes outputs, runs quality stack

**Spawn prompt template:**

```markdown
You are a [foundry:sw-engineer|foundry:qa-specialist] teammate refactoring: [target].
Read ${HOME}/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Apply file locking protocol for concurrent edits.
Your task: [refactoring steps 4 | characterization tests step 3].
Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
```

</workflow>
