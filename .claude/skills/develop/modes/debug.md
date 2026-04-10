# Debug Mode

Systematic investigation-first debugging. Gather evidence and trace the data flow before writing any test or fix — form a confirmed root-cause hypothesis, then hand off to fix mode for the reproduce-fix-verify cycle.

## Step 1: Understand the symptom

Collect all available signals before forming any hypothesis:

```bash
# Read the full traceback — never just the last line
python -m pytest --tb=long <test_path >-v 2>&1 | tail -60
```

```bash
# What changed recently near the failing code?
git log --oneline -20
git diff HEAD~5..HEAD -- <suspect_file>
```

If a GitHub issue number was provided:

```bash
gh issue view <number >--comments
```

Use Grep (pattern: failing symbol, class, or error keyword; path: `src/`) to trace the call path from entry point to failure site.

Spawn a **sw-engineer** agent to map the execution path and produce:

- Entry point to failure: which modules does the call cross?
- What state is mutated along the way?
- What invariant is violated at the point of failure?
- Any recent commit that touched this path (from git log output)

**Complexity gate**: if the root cause spans 3+ modules or requires a design change, flag it and use `AskUserQuestion` to ask whether to proceed, with options: "Continue debug (Recommended)" (fix the root cause in this session), "Open /develop feature" (open a wider feature track instead).

Present the agent's analysis summary before proceeding.

## Step 2: Pattern analysis

Find the nearest similar working code path and compare exhaustively:

1. Locate 2–3 code paths that handle similar input or perform similar work *successfully*
2. List **every** difference between the working path and the broken one — not just the obvious one
3. Check across axes:
   - Same input, different environment (versions, config, data shape)?
   - Same logic, different call order or timing?
   - Conditionals that take different branches on different inputs?
   - None/empty guards present in the working path but absent in the broken one?

This step catches non-obvious causes — an ordering dependency, environment-specific state, a type coercion that silently changes behaviour.

## Step 3: Hypothesis and gate

State the root cause hypothesis explicitly before writing any code:

```
Root cause: <one sentence — what is wrong and why>
Evidence for: [signals that support this]
Evidence against: [anything that contradicts or remains unexplained]
Confidence: high / medium / low
```

**Gate**: present this to the user and wait for confirmation or challenge before proceeding to Step 4. A wrong hypothesis produces a fix that passes tests but does not resolve the underlying problem.

If confidence is low: propose a targeted probe (a minimal script, an added log statement, a single assertion) to gather the missing signal — run it before committing to a fix.

## Step 4: Hand off to fix

Root cause confirmed. Transition to fix mode with the diagnosis as input — fix's Step 1 is pre-answered.

Emit this handoff block:

```
Root cause: <confirmed hypothesis from Step 3>
Suspect file(s): <files identified in Steps 1–2>
Evidence: <key signals that confirmed the hypothesis>
```

→ Proceed with `/develop fix` from **Step 2** (regression test). The root cause is already known — fix's Step 1 analysis is complete.

## Team Assignments

**When to use team mode**: root cause unclear after Step 2, OR failure spans 3+ modules.

- **Teammate 1–3 (sw-engineer × 2–3, model=opus)**: each investigates a distinct root-cause hypothesis independently

**Coordination:**

1. Lead broadcasts current evidence: `{symptom: <description>, traceback: <key lines>}`
2. Each teammate claims one hypothesis and investigates it independently — no overlap
3. Lead facilitates cross-challenge between competing analyses
4. Lead synthesises the consensus root cause, then executes Steps 3–4 (hypothesis gate, hand off to fix) alone

**Spawn prompt template:**

```
You are a sw-engineer teammate debugging: [symptom].
Read .claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your hypothesis: [hypothesis N]. Investigate ONLY this root cause.
Report findings to @lead using deltaT# or epsilonT# codes.
Compact Instructions: preserve file paths, errors, line numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal completion in your final delta message: "Status: complete | blocked — <reason>".
```
