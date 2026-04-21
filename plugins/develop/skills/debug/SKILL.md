---
name: debug
description: Investigation-first debugging — gather evidence, form confirmed root-cause hypothesis, write regression test, apply minimal fix via fix mode handoff.
argument-hint: <symptom or failing test>
effort: medium
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Investigation-first debugging. Gather evidence, trace data flow, form confirmed root-cause hypothesis, write regression test, hand off to fix mode.

NOT for: production incidents without local reproduction (use `/foundry:investigate` for triage); `.claude/` config issues (use `/audit`).

</objective>

<workflow>

<!-- Agent Resolution: skill-specific subset — update only agents used by this skill -->

## Agent Resolution

> **Foundry plugin check**: run `ls ~/.claude/plugins/cache/ 2>/dev/null | grep -q foundry` (exit 0 = installed). If check fails or uncertain, proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

When foundry **not** installed, substitute `foundry:X` with `general-purpose`, prepend role description plus `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |

Skills with `--team` mode: team spawning with fallback agents still works but produces lower-quality output.

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), TaskCreate all steps before any other work. Mark each step `in_progress` when starting, `completed` when done.

## Anti-Rationalizations

| Temptation | Reality |
| --- | --- |
| "I already know the root cause from the traceback" | Tracebacks show where, not why. Assumptions without code-path verification produce fixes for the wrong bug. |
| "The fix is obvious — Step 2 pattern analysis is overkill" | Obvious causes are often symptoms. Pattern comparison reveals ordering, timing, or environment differences invisible in the traceback. |
| "I'll just apply the fix here instead of handing off to `/develop:fix`" | Debug is investigation-only. Mixing investigation and implementation conflates history and skips the regression test gate. |
| "Low confidence is fine — I'll just try the fix and see" | A fix without a confirmed hypothesis is a guess. Guesses produce fixes that pass tests but don't resolve the underlying problem. |

## Project Detection

Detect test runner once — debug runs pytest in Step 1:

```bash
if [ -f "uv.lock" ] || grep -q '\[tool\.uv\]' pyproject.toml 2>/dev/null; then TEST_CMD="uv run pytest"
elif [ -f "poetry.lock" ] || grep -q '\[tool\.poetry\]' pyproject.toml 2>/dev/null; then TEST_CMD="poetry run pytest"
elif [ -f "tox.ini" ]; then TEST_CMD="tox"
elif [ -f "Makefile" ] && grep -q '^test:' Makefile 2>/dev/null; then TEST_CMD="make test"
else TEST_CMD="python -m pytest"; fi
```

```bash
# Derive PYTEST_CMD for commands needing pytest-specific flags
case "$TEST_CMD" in
    tox|"make test")
        if command -v uv >/dev/null 2>&1; then PYTEST_CMD="uv run pytest"
        else PYTEST_CMD="python -m pytest"; fi ;;
    *) PYTEST_CMD="$TEST_CMD" ;;
esac
```

Use `$TEST_CMD` in place of `python -m pytest` in this workflow.

**Checkpoint**: debug is investigation-only — no code changes. `.plans/active/debug_<slug>.md` (written in Step 4) serves as implicit session state. No `.developments/` checkpoint needed.

# Debug Mode

> **Argument type detection**: if `$ARGUMENTS` is a positive integer, treat as GitHub issue number and fetch with `gh issue view`. If text (contains spaces, letters, or special chars), treat as symptom description.

## Step 1: Understand the symptom

Collect all signals before forming any hypothesis:

```bash
# Read the full traceback — never just the last line
$PYTEST_CMD --tb=long <test_path> -v 2>&1 | tail -60
```

```bash
# What changed recently near the failing code?
git log --oneline -20
COMMIT_COUNT=$(git rev-list --count HEAD 2>/dev/null || echo 1)
LOOKBACK=$(( COMMIT_COUNT < 5 ? COMMIT_COUNT : 5 ))
[ "$LOOKBACK" -gt 1 ] && git diff HEAD~${LOOKBACK}..HEAD -- <suspect_file>
```

If GitHub issue number provided:

```bash
gh issue view <number> --comments
```

Use Grep (pattern: failing symbol, class, or error keyword) to trace call path from entry point to failure site. Path hint: use `src/` if that directory exists, otherwise search from project root (`.`).

Spawn **foundry:sw-engineer** agent to map execution path and produce:

- Entry point to failure: which modules does call cross?
- What state mutated along the way?
- What invariant violated at failure point?
- Any recent commit touching this path (from git log output)

**Scope gate**: if root cause spans 3+ modules, flag complexity smell. Use `AskUserQuestion` to present scope concern before proceeding, with options: "Narrow scope (Recommended)" / "Proceed anyway".

Present agent's analysis summary before proceeding.

## Step 2: Pattern analysis

Find nearest similar working code path, compare exhaustively:

1. Locate 2-3 code paths handling similar input or similar work *successfully*
2. List **every** difference between working path and broken one — not just obvious one
3. Check across axes:
   - Same input, different environment (versions, config, data shape)?
   - Same logic, different call order or timing?
   - Conditionals taking different branches on different inputs?
   - None/empty guards present in working path but absent in broken one?

Step catches non-obvious causes — ordering dependency, environment-specific state, type coercion silently changing behaviour.

## Step 3: Hypothesis and gate

State root cause hypothesis explicitly before writing any code:

```
Root cause: <one sentence — what is wrong and why>
Evidence for: [signals that support this]
Evidence against: [anything that contradicts or remains unexplained]
Confidence: high / medium / low
```

**Gate**: present hypothesis to user and wait for confirmation or challenge before proceeding to Step 4. Wrong hypothesis produces fix that passes tests but doesn't resolve underlying problem.

**Autonomous-mode fallback** (when running as a subagent with no direct user interaction):
- Confidence **high**: proceed automatically to Step 4; note "auto-confirmed (subagent mode)" in Final Report
- Confidence **medium**: return hypothesis + evidence to parent agent as structured JSON; let parent decide: `{"hypothesis":"<root cause>","evidence":["<s1>","<s2>"],"confidence":"medium","action_required":"confirm_before_fix"}`
- Confidence **low**: run targeted probe (minimal script, added assertion) to gather more signal before returning to parent

If confidence low: propose targeted probe (minimal script, added log statement, single assertion) to gather missing signal — run it before committing to fix.

## Step 4: Hand off to fix

Root cause confirmed. Transition to fix mode with diagnosis as input — fix's Step 1 pre-answered.

Emit handoff block:

```
Root cause: <confirmed hypothesis from Step 3>
Suspect file(s): <files identified in Steps 1-2>
Evidence: <key signals that confirmed the hypothesis>
```

**Write diagnosis to file** before handing off — enables `/develop:fix` to skip Step 1 analysis via `--diagnosis <path>`:

```bash
SLUG=$(echo "<symptom first 4 words>" | tr ' ' '-' | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-')
DIAG_FILE=".plans/active/debug_${SLUG}.md"
mkdir -p .plans/active
```

Write `$DIAG_FILE` with this structure:
```markdown
# Debug Diagnosis: <symptom>

## Root Cause
<one sentence — confirmed hypothesis>

## Suspect Files
- path/to/file.py — <reason>

## Evidence
- <signal 1 that confirmed hypothesis>
- <signal 2>

## Confidence
<high|medium|low>
```

Hand off: `-> /develop:fix --diagnosis $DIAG_FILE` from Step 2 (regression test). Root cause already known — fix's Step 1 analysis is complete.

## Final Report

After root cause confirmed and handoff to `/develop:fix` complete, emit terminal summary:

```
Root Cause: <one sentence>
File(s): <suspect files>
Evidence: <key signals>
→ Handed off to /develop:fix from Step 2

## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
**Gaps**: [e.g., unverified alternative hypotheses, hypothesis only — not confirmed via test reproduction]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: root cause unclear after Step 2, OR failure spans 3+ modules.

- **Teammate 1-3 (foundry:sw-engineer x 2-3, model=opus)**: each investigates distinct root-cause hypothesis independently

**Coordination:**

1. Lead broadcasts current evidence: `{symptom: <description>, traceback: <key lines>}`
2. Each teammate claims one hypothesis, investigates independently — no overlap
3. Lead facilitates cross-challenge between competing analyses
4. **Convergence deadline**: after cross-challenge, if teammates still disagree on root cause, lead selects the hypothesis with the most direct evidence (observable in code or logs). If truly tied, use `AskUserQuestion` to present the top 2 competing hypotheses and ask user to guide.
5. Lead synthesises consensus root cause, then executes Steps 3-4 (hypothesis gate, hand off to fix) alone

**Spawn prompt template:**

```
You are a foundry:sw-engineer teammate debugging: [symptom].
Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your hypothesis: [hypothesis N]. Investigate ONLY this root cause.
Report findings to @lead using deltaT# or epsilonT# codes.
Compact Instructions: preserve file paths, errors, line numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal completion in your final delta message: "Status: complete | blocked — <reason>".
```

</workflow>
