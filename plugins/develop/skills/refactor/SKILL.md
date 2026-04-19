---
name: refactor
description: Test-first refactoring — audit coverage, add characterization tests, apply changes with safety net, run quality stack and review loop.
argument-hint: <target file or directory> <goal>
effort: high
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Test-first refactoring. Audit coverage, add characterization tests if missing, apply changes with safety net.

NOT for: bug fixes (use `/develop:fix`); new features (use `/develop:feature`); `.claude/` config changes (use `/manage`).

</objective>

<workflow>

<!-- Agent Resolution: identical across all develop skills -->

## Agent Resolution

> **Foundry plugin check**: run `ls ~/.claude/plugins/cache/ 2>/dev/null | grep -q foundry` (exit 0 = installed). If check fails or uncertain, proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

When foundry **not** installed, substitute `foundry:X` with `general-purpose`, prepend role description plus `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |

Skills with `--team` mode: team spawning with fallback agents still works but lower-quality output.

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), TaskCreate all steps before any other work. Mark each step in_progress when starting, completed when done.

# Refactor Mode

## Step 1: Scope and understand

Read target code, build mental model before touching anything.

If `<target>` is directory: use Glob tool (pattern `**/*.py`, path `<target>`) to enumerate Python files.

```bash
# Measure current state
wc -l <target>/**/*.py 2>/dev/null || wc -l <target>
```

**Structural context** (codemap, if installed) — soft PATH check, silently skip if `scan-query` not found:

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 5
fi
```

If results returned: prepend `## Structural Context (codemap)` block to foundry:sw-engineer spawn prompt with hotspot JSON. If target maps to module in index, also include `scan-query deps <target_module>` (coupling) and `scan-query rdeps <target_module>` (blast radius). Derive `<target_module>` from target path: strip project root prefix, replace `/` with `.`, drop `.py` extension. If `scan-query` not found or index missing: proceed silently — don't mention codemap to user.

Spawn **foundry:sw-engineer** agent to analyze code and identify:

- Public API surface (functions, classes, methods external code calls)
- Internal complexity hotspots (cyclomatic complexity, deep nesting, long functions)
- Code smells relevant to stated goal
- Dependencies and coupling between modules
- **Complexity smell**: directory or cross-module scope — flag it; consider team mode

**Scope gate**: if target is directory-wide scope (10+ files) regardless of goal, flag complexity smell. Use `AskUserQuestion` with options: "Narrow scope (Recommended)" / "Proceed anyway".

## Step 2: Audit test coverage

Find existing tests for target code:

Use Glob tool (pattern `**/test_*.py` or `**/*_test.py`), then Grep tool (pattern `<module_name>`, output mode `files_with_matches`) to narrow to those referencing target.

```bash
# Check coverage
python -m pytest --co -q 2>/dev/null | grep -i "<module_name>" || echo "No tests found"
python -m pytest --cov= -q <target_module >--cov-report=term-missing 2>/dev/null
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

```bash
# Run to confirm they pass against current code
python -m pytest <test_file >-v
```

**Gate**: all characterization tests must pass before proceeding. If any fail, fix test — not code.

## Step 4: Refactor with safety net

For each change:

1. One focused change (single responsibility per edit)
2. Run test suite:
   ```bash
   python -m pytest --tb=short <test_files >-v
   ```
3. Tests pass: proceed to next change
4. Tests fail: revert, try different approach

**Safety break**: max 5 change-test cycles per session. After 5, stop — report what succeeded, what broke, what remains.

**Refactoring categories:**

- **Logic simplification**: replace complex conditionals, flatten nesting, extract helpers
- **API cleanup**: rename for clarity, consolidate parameters, add type annotations
- **Structural**: extract classes/modules, reduce coupling, apply design patterns
- **Performance**: replace loops with vectorized ops, reduce allocations, batch I/O
- **Dead code removal**: remove unused imports, unreachable branches, commented-out code; scan `_`-prefixed functions with no call sites; flag public methods absent from `__init__.py` exports

## Step 5: Review and close gaps

Read `.claude/skills/_shared/codex-prepass.md`, run Codex pre-pass before cycle 1.

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
   python -m pytest --tb=short <test_files >-v 2>&1 | tail -20
   ```

4. **Only nits remain** (variable naming, comment clarity, minor formatting): document in Follow-up, exit loop.

5. **Substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: substantive issues remain → stop, surface to user.

Read `.claude/skills/_shared/quality-stack.md` and execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps.

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
- Coverage: before% -> after%

### Follow-up
- [any remaining items that need manual review]

## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
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

```
You are a [foundry:sw-engineer|foundry:qa-specialist] teammate refactoring: [target].
Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2. Apply file locking protocol for concurrent edits.
Your task: [refactoring steps 4 | characterization tests step 3].
Compact Instructions: preserve file paths, test results, coverage numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
```

</workflow>
