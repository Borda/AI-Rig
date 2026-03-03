---
name: refactor
description: Test-first refactoring orchestrator. Ensures test coverage exists before changing code — adds characterization tests if missing, then applies logic improvements, API cleanup, and structural changes with verified input/output consistency.
argument-hint: <file, directory, or module to refactor> ["goal description"]
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent
---

<objective>

Safely refactor code by enforcing a test-first discipline. Before any logic change, verify that the target code has tests covering its current behavior. If tests are missing, generate characterization tests that capture existing input/output contracts. Only then proceed with the refactoring — running the test suite after each change to confirm nothing broke.

</objective>

<inputs>

- **$ARGUMENTS**: required
  - First token: file path, directory, or module to refactor
  - Remaining tokens (optional): quoted goal description — what to improve (e.g., `"replace manual loops with vectorized ops"`, `"simplify error handling"`, `"extract common logic into shared util"`)
  - If no goal given: perform a general quality pass (dead code, complexity, naming, structure)

</inputs>

<workflow>

## Step 1: Scope and understand

Read the target code and build a mental model before touching anything:

If `<target>` is a directory: use the Glob tool (pattern `**/*.py`, path `<target>`) to enumerate Python files (excludes `__pycache__` automatically).

```bash
# Measure current state
wc -l <target>/**/*.py 2>/dev/null || wc -l <target>
```

Spawn a **sw-engineer** agent to analyze the code and identify:

- Public API surface (functions, classes, methods that external code calls)
- Internal complexity hotspots (cyclomatic complexity, deep nesting, long functions)
- Code smells relevant to the stated goal
- Dependencies and coupling between modules

End your response with a `## Confidence` block per CLAUDE.md output standards.

## Step 2: Audit test coverage

Find existing tests for the target code:

Locate test files: use the Glob tool (pattern `**/test_*.py` or `**/*_test.py`) to find candidates, then use the Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those that reference the target module.

```bash
# Check if pytest is available and run coverage on the target
python -m pytest --co -q 2>/dev/null | grep -i "<module_name>" || echo "No tests found"

# If coverage tool available
python -m pytest --cov=<target_module> --cov-report=term-missing -q 2>/dev/null
```

Classify each public function/method as:

- **Covered**: has at least one test exercising its happy path and one edge case
- **Partially covered**: has a test but missing edge cases or failure paths
- **Uncovered**: no test at all

## Step 3: Add characterization tests (if needed)

For every **uncovered** or **partially covered** public API, spawn a **qa-specialist** agent to generate characterization tests:

- Import the function, call it with representative inputs, and assert the **current** output
- These tests document existing behavior — they are not aspirational, they capture reality
- Use `pytest.mark.parametrize` for multiple input/output pairs
- For side-effectful code: mock external dependencies, assert call patterns
- Name tests `test_<function>_characterization_*` so they're easy to identify later

End your response with a `## Confidence` block per CLAUDE.md output standards.

```bash
# Run the new tests to confirm they pass against current code
python -m pytest <test_file> -v
```

**Gate**: all characterization tests must pass before proceeding. If any fail, the test is wrong — fix the test, not the code.

## Step 4: Refactor with safety net

Now apply the refactoring changes. For each change:

1. Make one focused change (single responsibility per edit)
2. Run the full test suite for the target:
   ```bash
   python -m pytest <test_files> -v --tb=short
   ```
3. If tests pass: proceed to the next change
4. If tests fail: the refactoring broke behavior — revert and try a different approach

**Refactoring categories** (apply what matches the goal):

- **Logic simplification**: replace complex conditionals, flatten nesting, extract helper functions
- **API cleanup**: rename for clarity, consolidate overloaded parameters, add type annotations
- **Structural**: extract classes/modules, reduce coupling, apply design patterns
- **Performance**: replace loops with vectorized ops, reduce allocations, batch I/O
- **Dead code removal**: remove unused imports, unreachable branches, commented-out code

## Step 5: Delegate implementation follow-up (optional)

Inspect the refactoring changes (`git diff HEAD --stat`) and identify real implementation tasks that Codex can complete — not style violations (those are handled by pre-commit hooks), but work that requires understanding the restructured code.

**Delegate to Codex when you can write an accurate, specific brief:**

- Renamed or moved functions whose docstrings now describe the wrong context — read the new implementation, then write a precise update brief
- Extracted helpers that have no documentation — describe what the helper does and what invariants it relies on
- Restructured public APIs where the old type annotations no longer reflect the actual contract

**Do not delegate:**

- Style or lint violations — run pre-commit hooks instead
- Any task where you cannot write a precise description without guessing

For each task, read the target code, form an accurate brief, then spawn:

```
Task(
  subagent_type="general-purpose",
  prompt="Read .claude/skills/codex/SKILL.md and follow its workflow exactly.
Task: use the <agent> to <specific task with accurate description of what the code does>.
Target: <file>."
)
```

Example prompt: `"use the doc-scribe to rewrite the docstring for DataPipeline.transform() in src/pipeline.py — the method was refactored to accept a list of transforms instead of a single callable; it now applies them sequentially and returns early on the first None result"`

The subagent handles pre-flight, dispatch, validation, and patch capture. If Codex is unavailable it reports gracefully — do not block on this step.

Include a `### Codex Delegation` section in the Step 6 report only if this step ran.

## Step 6: Verify and report

Run the complete test suite one final time:

```bash
# Full test run
python -m pytest <test_files> -v

# If coverage available — compare before/after
python -m pytest --cov=<target_module> --cov-report=term-missing -q
```

Output a structured report:

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
**Gaps**: [e.g., characterization tests incomplete, edge cases not covered, coverage tool unavailable]
```

</workflow>

<notes>

- **Never refactor without tests**: this is the core invariant — if tests don't exist, add them first
- Characterization tests capture *current* behavior, not *desired* behavior — they're a safety net, not a spec
- One change at a time: each edit should be independently verifiable by the test suite
- If the refactoring goal conflicts with existing tests, that's a signal to discuss with the user — don't silently change test expectations
- Related agents: `sw-engineer` (code analysis), `qa-specialist` (test generation), `linting-expert` (post-refactor cleanup)
- Follow-up chains:
  - Refactored code needs quality validation → `/review` for full multi-agent code review
  - Cleaned-up module is ready to extend → `/feature` to add new capability on the improved foundation
  - Refactoring touched `.claude/` config files → run `self-mentor` on changed files, then `/sync` to propagate
  - Mechanical cleanup needed beyond what Step 5 handled → `/codex` to delegate additional tasks

</notes>
