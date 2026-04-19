---
name: fix
description: Reproduce-first bug resolution — capture bug in failing regression test, apply minimal fix, run quality stack and review loop.
argument-hint: <symptom or issue #>
effort: medium
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Reproduce-first bug resolution. Capture bug in failing regression test, apply minimal fix, verify via quality stack and review loop.

NOT for: unknown failures without traceback (use `/foundry:investigate`); `.claude/` config issues (use `/audit`).

</objective>

<workflow>

<!-- Agent Resolution: identical across all develop skills -->

## Agent Resolution

> **Foundry plugin check**: run `ls ~/.claude/plugins/cache/ 2>/dev/null | grep -q foundry` (exit 0 = installed). If check fails or uncertain, proceed as if foundry available — common case; only fall back if agent dispatch explicitly fails.

When foundry **not** installed, substitute `foundry:X` with `general-purpose`, prepend role description plus `model: <model>`:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |

Skills with `--team` mode: team spawning with fallback agents works but lower-quality output.

## Anti-Rationalizations

| Temptation | Reality |
| --- | --- |
| "I already know the root cause from the symptom" | Assumptions without verification produce fixes for the wrong bug. Read the code path first. |
| "The regression test can wait — I'll add it after the fix" | A fix without a failing test is unverifiable. The test is the proof the bug existed. |
| "I'll clean up nearby code while I'm here" | Scope creep produces side effects and obscures the actual fix. Touch only the root cause. |
| "The targeted test passes — that's sufficient" | The targeted test shows the bug is fixed; the full suite shows nothing else broke. Both are required. |
| "The fix is obvious — Step 1 analysis is overkill" | Obvious causes are often symptoms. Analysis reveals the actual root cause and blast radius. |

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), TaskCreate for all steps before any other work. Mark each step in_progress when starting, completed when done.

# Fix Mode

## Step 1: Understand the problem

Gather all available context about bug:

```bash
# If issue number: fetch the full issue with comments
gh issue view <number >--comments
```

If error message or pattern provided: use Grep tool (pattern `<error_pattern>`, path `.`) to search codebase for failing code path.

```bash
# If failing test: run it to capture the exact failure
python -m pytest --tb=long <test_path >-v 2>&1 | tail -40
```

**Structural context** (codemap, if installed) — soft PATH check, silently skip if `scan-query` not found:

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 5
fi
```

If results returned: prepend `## Structural Context (codemap)` block to foundry:sw-engineer spawn prompt with hotspot JSON. If `scan-query` not found or index missing: proceed silently — do not mention codemap to user.

Spawn **foundry:sw-engineer** agent to analyze failing code path and identify:

- Root cause (not just symptom)
- Minimal code surface needing change
- Related code possibly affected by fix

If root cause not definitively established after analysis, surface assumptions before proceeding:

> ASSUMPTIONS I'M MAKING:
>
> 1. [assumption about root cause]
> 2. [assumption about affected scope] → Correct me now or I'll proceed with these.

**Scope gate**: if root cause spans 3+ modules, flag complexity smell. Use `AskUserQuestion` to present scope concern before proceeding, with options: "Narrow scope (Recommended)" / "Proceed anyway".

## Step 2: Reproduce the bug

Create or identify test demonstrating failure:

```bash
# If a failing test already exists — run it to confirm it fails
python -m pytest --tb=short <test_file >:: <test_name >-v

# If no test exists — write a regression test that captures the bug
```

Spawn **foundry:qa-specialist** agent to write regression test if none exists:

- Test must **fail** against current code (proving bug exists)
- Use `pytest.mark.parametrize` if bug affects multiple input patterns
- Keep test minimal — exercise exactly broken behavior
- Add brief comment linking to issue if applicable (e.g., `# Regression test for #123`)

**Gate**: regression test must fail before proceeding. If passes, bug not properly captured — revisit Step 1.

### Review: Validate the reproduction

Before applying fix, critically evaluate regression test:

1. **Correct failure mode**: fails for right reason (actual bug), not setup issue?
2. **Isolation**: exercises exactly broken behavior, not too broadly?
3. **Minimal reproduction**: smallest test demonstrating failure?
4. **Parametrization**: key variants covered if bug spans multiple input patterns?

If any issue found: revise regression test before applying fix. Flawed reproduction = fix validated against wrong criteria.

## Step 3: Apply the fix

Make minimal change to fix root cause:

1. Edit only code necessary to resolve bug
2. Run regression test to confirm now passes:
   ```bash
   python -m pytest --tb=short <test_file >:: <test_name >-v
   ```
3. Run full test suite for affected module:
   ```bash
   python -m pytest --tb=short <test_dir >-v
   ```
4. If existing tests break: fix has side effects — reconsider approach

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

2. For every gap found: implement fix immediately — tighten patch, remove collateral edits, adjust test. Return to Step 3 for any gap requiring re-examining fix approach.

3. Re-run test suite:

   ```bash
   python -m pytest --tb=short -q <test_dir >-v 2>&1 | tail -20
   ```

4. **Adjacent bugs** (observation only): scan for similar patterns; document in Follow-up — do not fix here, avoids scope creep.

5. **Only nits remain**: document in Follow-up, exit loop.

6. **Substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface to user before proceeding.

Read `.claude/skills/_shared/quality-stack.md` and execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps.

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
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
**Gaps**: [e.g., could not reproduce locally, partial traceback only, fix not runtime-tested]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: root cause unclear after Step 1, OR bug spans 3+ modules.

- **Teammate 1-3 (foundry:sw-engineer x 2-3, model=opus)**: each investigates distinct root-cause hypothesis independently

**Coordination:**

1. Lead broadcasts current evidence: `{bug: <description>, traceback: <key lines>}`
2. Each teammate investigates independently — claims hypothesis
3. Lead facilitates cross-challenge between competing analyses
4. Lead synthesizes consensus root cause, then proceeds with Steps 2-4 (regression test, fix, review loop) alone

**Spawn prompt template:**

```
You are a foundry:sw-engineer teammate debugging: [bug description].
Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your hypothesis: [hypothesis N]. Investigate ONLY this root cause.
Report findings to @lead using deltaT# or epsilonT# codes.
Compact Instructions: preserve file paths, errors, line numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
```

</workflow>
