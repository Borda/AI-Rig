# Refactor Mode

Test-first refactoring. Audit test coverage, add characterization tests if missing, then apply changes with a safety net.

## Step 1: Scope and understand

Read the target code and build a mental model before touching anything.

If `<target>` is a directory: use the Glob tool (pattern `**/*.py`, path `<target>`) to enumerate Python files.

```bash
# Measure current state
wc -l <target>/**/*.py 2>/dev/null || wc -l <target>
```

Spawn a **sw-engineer** agent to analyze the code and identify:

- Public API surface (functions, classes, methods that external code calls)
- Internal complexity hotspots (cyclomatic complexity, deep nesting, long functions)
- Code smells relevant to the stated goal
- Dependencies and coupling between modules
- **Complexity smell**: Directory or cross-module scope — flag it; consider team mode

## Step 2: Audit test coverage

Find existing tests for the target code:

Use the Glob tool (pattern `**/test_*.py` or `**/*_test.py`) to find candidates, then the Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those that reference the target.

```bash
# Check coverage
python -m pytest --co -q 2>/dev/null | grep -i "<module_name>" || echo "No tests found"
python -m pytest --cov= -q <target_module >--cov-report=term-missing 2>/dev/null
```

Classify each public function/method as:

- **Covered**: has at least one test exercising happy path and one edge case
- **Partially covered**: has a test but missing edge cases or failure paths
- **Uncovered**: no test at all

### Review: Validate the coverage audit

Before writing characterization tests, critically evaluate the audit output itself:

1. **Completeness**: were all public functions, methods, and classes identified — including those with complex call paths?
2. **Classification accuracy**: is each item correctly classified? Partially-covered functions are frequently misclassified as covered.
3. **Refactor relevance**: are the uncovered/partial items in the code paths the refactoring will actually touch?
4. **Hidden dependencies**: are there integration points or cross-module calls the audit may have missed?

If the audit seems incomplete: re-examine before proceeding to Step 3. Gaps in the safety net discovered mid-refactoring (Step 4) are costly.

## Step 3: Add characterization tests (if needed)

For every **uncovered** or **partially covered** public API, spawn a **qa-specialist** agent to generate characterization tests:

- Import the function, call it with representative inputs, assert the **current** output
- Use `pytest.mark.parametrize` for multiple input/output pairs
- Name tests `test_<function>_characterization_*`

```bash
# Run to confirm they pass against current code
python -m pytest <test_file >-v
```

**Gate**: all characterization tests must pass before proceeding. If any fail, fix the test, not the code.

## Step 4: Refactor with safety net

For each change:

1. Make one focused change (single responsibility per edit)
2. Run the test suite:
   ```bash
   python -m pytest --tb=short <test_files >-v
   ```
3. If tests pass: proceed to the next change
4. If tests fail: revert and try a different approach

**Safety break**: max 5 change-test cycles per session. After 5, stop and report which succeeded, which broke, and what remains.

**Refactoring categories:**

- **Logic simplification**: replace complex conditionals, flatten nesting, extract helpers
- **API cleanup**: rename for clarity, consolidate parameters, add type annotations
- **Structural**: extract classes/modules, reduce coupling, apply design patterns
- **Performance**: replace loops with vectorized ops, reduce allocations, batch I/O
- **Dead code removal**: remove unused imports, unreachable branches, commented-out code; scan `_`-prefixed functions with no call sites; flag public methods absent from `__init__.py` exports

## Step 5: Review and close gaps

Read `.claude/skills/_shared/codex-prepass.md` and run the Codex pre-pass before cycle 1.

Full review of the refactored code. This is a **loop** — review → targeted refactoring (return to Step 4) → re-review until only nits remain. Maximum 3 outer cycles. (Step 4's "max 5 change-test cycles" bound applies within each individual pass through Step 4, independently of this outer loop.)

**Each cycle:**

1. Evaluate against all criteria:

   - **Behavior preservation**: all characterization tests and pre-existing tests pass with identical outputs
   - **Goal achieved**: the stated refactoring goal was actually accomplished (not just partially)
   - **No new smells**: no new coupling, complexity, or duplication introduced
   - **API surface**: no unintended public API changes (signature, return type, raised exceptions)
   - **Dead code**: any code that became unreachable after the refactor was removed

2. For every gap found: return to Step 4 and apply a targeted fix — one focused change per gap.

3. Re-run the full test suite:

   ```bash
   python -m pytest --tb=short <test_files >-v 2>&1 | tail -20
   ```

4. **If only nits remain** (variable naming, comment clarity, minor formatting): document in Follow-up and exit the loop.

5. **If substantive gaps remain**: start the next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface them to the user before proceeding.

## Final Report

```
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
- Coverage: before% → after%

### Follow-up
- [any remaining items that need manual review]

## Confidence
**Score**: [0.N]
**Gaps**: [e.g., coverage tool unavailable, some tests skipped]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: target is a directory OR cross-module scope.

- **Teammate 1 (sw-engineer, model=opus)**: performs the refactoring (Step 4)
- **Teammate 2 (qa-specialist, model=opus)**: writes characterization tests (Step 3) in parallel

**Coordination:**

1. Lead broadcasts Step 1+2 analysis: `{target: <path>, coverage: <summary>, goal: <stated goal>}`
2. QA writes characterization tests while SW prepares the refactoring plan
3. **File locking**: teammates coordinate via TEAM_PROTOCOL.md to avoid editing the same file simultaneously
4. Lead synthesizes outputs and runs quality stack

**Spawn prompt template:**

```
You are a [sw-engineer|qa-specialist] teammate refactoring: [target].
Read .claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Apply file locking protocol for concurrent edits.
Your task: [refactoring steps 4 | characterization tests step 3].
Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
```
