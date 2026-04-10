# Fix Mode

Reproduce-first bug resolution. Capture the bug in a failing regression test, apply the minimal fix, then verify.

## Step 1: Understand the problem

Gather all available context about the bug:

```bash
# If issue number: fetch the full issue with comments
gh issue view <number >--comments
```

If an error message or pattern was provided: use the Grep tool (pattern `<error_pattern>`, path `.`) to search the codebase for the failing code path.

```bash
# If failing test: run it to capture the exact failure
python -m pytest --tb=long <test_path >-v 2>&1 | tail -40
```

Spawn a **sw-engineer** agent to analyze the failing code path and identify:

- The root cause (not just the symptom)
- The minimal code surface that needs to change
- Any related code that might be affected by the fix

## Step 2: Reproduce the bug

Create or identify a test that demonstrates the failure:

```bash
# If a failing test already exists — run it to confirm it fails
python -m pytest --tb=short <test_file >:: <test_name >-v

# If no test exists — write a regression test that captures the bug
```

Spawn a **qa-specialist** agent to write the regression test if one doesn't exist:

- The test must **fail** against the current code (proving the bug exists)
- Use `pytest.mark.parametrize` if the bug affects multiple input patterns
- Keep the test minimal — exercise exactly the broken behavior
- Add a brief comment linking to the issue if applicable (e.g., `# Regression test for #123`)

**Gate**: the regression test must fail before proceeding. If it passes, the bug isn't properly captured — revisit Step 1.

### Review: Validate the reproduction

Before applying any fix, critically evaluate the regression test itself:

1. **Correct failure mode**: does the test fail for the right reason (the actual bug), not because of a setup issue?
2. **Isolation**: does the test exercise exactly the broken behavior, or does it inadvertently test too broadly?
3. **Minimal reproduction**: is this the smallest test that demonstrates the failure?
4. **Parametrization**: if the bug manifests across multiple input patterns, are the key variants covered?

If any issue is found: revise the regression test before applying the fix. A flawed reproduction means the fix will be validated against the wrong criteria.

## Step 3: Apply the fix

Make the minimal change to fix the root cause:

1. Edit only the code necessary to resolve the bug
2. Run the regression test to confirm it now passes:
   ```bash
   python -m pytest --tb=short <test_file >:: <test_name >-v
   ```
3. Run the full test suite for the affected module:
   ```bash
   python -m pytest --tb=short <test_dir >-v
   ```
4. If any existing tests break: the fix has side effects — reconsider the approach

## Step 4: Review and close gaps

Read `.claude/skills/_shared/codex-prepass.md` and run the Codex pre-pass before cycle 1.

Full review of the fix. This is a **loop** — review → fix → re-review until only nits remain. Maximum 3 cycles.

**Each cycle:**

1. Evaluate against all criteria:

   - **Root cause**: fix addresses the actual root cause, not just the symptom
   - **Minimality**: smallest change that resolves the bug; no collateral edits
   - **Regression test quality**: test precisely isolates the bug (fails before fix, passes after)
   - **Side effects**: full suite passes without new failures or unexpected warnings

2. For every gap found: implement the fix immediately — tighten the patch, remove collateral edits, adjust the test. Return to Step 3 for any gap that requires re-examining the fix approach.

3. Re-run the test suite:

   ```bash
   python -m pytest --tb=short -q <test_dir >-v 2>&1 | tail -20
   ```

4. **Adjacent bugs** (observation only): scan for similar patterns in the codebase; document in Follow-up — do not fix here to avoid scope creep.

5. **If only nits remain**: document in Follow-up and exit the loop.

6. **If substantive gaps remain**: start the next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface them to the user before proceeding.

## Final Report

```
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
|------|--------|-------|
| path/to/file.py | description of fix | -N/+M |

### Test Results
- Regression test: PASS
- Full suite: PASS (N tests)
- Lint: clean

### Follow-up
- [any related issues or code that should be reviewed]
- [if no test runner: `rm <test_file>` — no test suite will re-execute it; it served the gate, now expendable]

## Confidence
**Score**: [0.N]
**Gaps**: [e.g., could not reproduce locally, partial traceback only, fix not runtime-tested]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: root cause unclear after Step 1, OR bug spans 3+ modules.

- **Teammate 1–3 (sw-engineer x 2–3, model=opus)**: each investigates a distinct root-cause hypothesis independently

**Coordination:**

1. Lead broadcasts current evidence: `{bug: <description>, traceback: <key lines>}`
2. Each teammate investigates independently — claims a hypothesis
3. Lead facilitates cross-challenge between competing analyses
4. Lead synthesizes consensus root cause, then proceeds with Steps 2–4 (regression test, fix, review loop) alone

**Spawn prompt template:**

```
You are a sw-engineer teammate debugging: [bug description].
Read .claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your hypothesis: [hypothesis N]. Investigate ONLY this root cause.
Report findings to @lead using deltaT# or epsilonT# codes.
Compact Instructions: preserve file paths, errors, line numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
```
