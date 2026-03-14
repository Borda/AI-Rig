---
name: fix
description: Bug-fixing workflow — diagnose the problem, reproduce it with a regression test, apply a targeted fix, then verify with linting, quality checks, and optional optimization.
argument-hint: <bug description, issue #, error message, or failing test>
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
---

<objective>

Fix software bugs with a disciplined reproduce-first workflow. Before touching any code, understand the root cause and capture the bug in a regression test. Then apply the minimal fix, verify all tests pass, and finish with linting and quality checks. The regression test stays in the codebase to prevent re-introduction.

</objective>

<inputs>

- **$ARGUMENTS**: required — one of:
  - A bug description in plain text (e.g., `"TypeError when passing None to transform()"`)
  - A GitHub issue number (e.g., `123` — fetched via `gh issue view`)
  - An error message or traceback snippet
  - A failing test name (e.g., `tests/test_transforms.py::test_none_input`)

</inputs>

<workflow>

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create a new task.

## Step 1: Understand the problem

Gather all available context about the bug:

```bash
# If issue number: fetch the full issue with comments
gh issue view <number> --comments
```

If an error message or pattern was provided: use the Grep tool (pattern `<error_pattern>`, path `.`) to search the codebase for the failing code path. Adjust to `src/`, `lib/`, or `app/` as appropriate for the project layout.

```bash

# If failing test: run it to capture the exact failure
python -m pytest <test_path> -v --tb=long 2>&1 | tail -40
```

Spawn a **sw-engineer** agent to analyze the failing code path and identify:

- The root cause (not just the symptom)
- The minimal code surface that needs to change
- Any related code that might be affected by the fix

## Step 2: Reproduce the bug

Create or identify a test that demonstrates the failure:

```bash
# If a failing test already exists — run it to confirm it fails
python -m pytest <test_file>::<test_name> -v --tb=short

# If no test exists — write a regression test that captures the bug
# Name it: test_<function>_<bug_description> (e.g., test_transform_none_input)
```

Spawn a **qa-specialist** agent to write the regression test if one doesn't exist:

- The test must **fail** against the current code (proving the bug exists)
- Use `pytest.mark.parametrize` if the bug affects multiple input patterns
- Keep the test minimal — exercise exactly the broken behavior
- Add a brief comment linking to the issue if applicable (e.g., `# Regression test for #123`)

**Gate**: the regression test must fail before proceeding. If it passes, the bug isn't properly captured — revisit Step 1.

## Step 3: Apply the fix

Make the minimal change to fix the root cause:

1. Edit only the code necessary to resolve the bug
2. Run the regression test to confirm it now passes:
   ```bash
   python -m pytest <test_file>::<test_name> -v --tb=short
   ```
3. Run the full test suite for the affected module to check for regressions:
   ```bash
   python -m pytest <test_dir> -v --tb=short
   ```
4. If any existing tests break: the fix has side effects — reconsider the approach

## Step 4: Linting and quality

Spawn a **linting-expert** agent (or run directly) to ensure the fix meets code quality standards:

```bash
# Run ruff for linting and formatting
uv run ruff check <changed_files> --fix
uv run ruff format <changed_files>

# Run mypy for type checking if configured
uv run mypy <changed_files> --no-error-summary 2>&1 | head -20

python -m pytest <test_dir> -v --tb=short
```

## Step 5: Verify and report

Output a structured report:

```
## Fix Report: <bug summary>

### Root Cause
[1-2 sentence explanation of what was wrong and why]

### Regression Test
- File: <test_file>
- Test: <test_name>
- Confirms: [what behavior the test locks in]

### Changes Made
| File | Change | Lines |
|------|--------|-------|
| path/to/file.py | description of fix | -N/+M |

### Test Results
- Regression test: PASS
- Full suite: PASS (N tests)
- Lint: clean

### Follow-up
- [any related issues or code that should be reviewed]

## Confidence
**Score**: [0.N]
**Gaps**: [e.g., could not reproduce locally, partial traceback only, fix not runtime-tested]
```

## Team Mode (--team)

Use when the bug has competing root-cause hypotheses or spans multiple modules. Skip for single-file bugs — use the default workflow above.

When to trigger: root cause is unclear after Step 1, OR the bug manifests across 3+ modules.

**Workflow with --team:**

1. Lead spawns 2–3 **sw-engineer** teammates, each investigating a distinct hypothesis
2. Broadcast current evidence to all teammates: `broadcast {bug: <description>, traceback: <key lines>}`
3. Each teammate investigates independently — announces with `alpha PROTO:v2.0` and claims a hypothesis
4. Teammates report findings via lead (hub-and-spoke); lead facilitates cross-challenge between competing analyses
5. Lead synthesizes the consensus root cause, then proceeds with Steps 2–5 above (regression test, fix, lint, report) — all in lead context

**Spawn prompt template:**

```
You are a sw-engineer teammate debugging: [bug description].
Read .claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your hypothesis: [hypothesis N]. Investigate ONLY this root cause.
Report findings to @lead using deltaT# or epsilonT# codes.
Compact Instructions: preserve file paths, errors, line numbers. Discard verbose tool output.
```

</workflow>

<notes>

- **Reproduce first**: never fix a bug you can't demonstrate with a test — the test is the proof
- **Minimal fix**: change only what's necessary to resolve the root cause — avoid incidental refactoring
- The regression test is a permanent contribution — it prevents the bug from recurring
- If the bug is in `.claude/` config files: run `self-mentor` audit + `/sync` after fixing
- Related agents: `sw-engineer` (root cause analysis), `qa-specialist` (regression test), `linting-expert` (quality)
- Follow-up chains:
  - Fix involves structural improvements beyond the bug → `/refactor` for test-first code quality pass
  - Fix touches non-trivial code paths → `/review` for full multi-agent quality validation
  - Fix required consistent renames or annotation changes across many files → `/codex` to delegate the mechanical sweep

</notes>
