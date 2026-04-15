---
name: feature
description: TDD-first feature development — crystallise API as a demo test, drive implementation to pass it, run quality stack and progressive review loop.
argument-hint: <goal>
effort: high
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

TDD-first feature development. Crystallise the API as a demo use-case test, drive implementation to pass it, then close quality gaps with review, documentation, and the quality stack.

NOT for: bug fixes (use `/develop:fix`); `.claude/` config changes (use `/manage`).

</objective>

<workflow>

## Anti-Rationalizations

| Temptation                                                           | Reality                                                                                      |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| "The feature is clear — I can skip the demo and go straight to code" | Without a crystallized API contract, implementation drifts. The demo is the spec.            |
| "I know this library — no need to check docs"                        | Training data contains deprecated patterns. One fetch prevents hours of rework.              |
| "I'll write tests after the implementation is stable"                | Tests drive design. Writing them first reveals API problems before they are baked in.        |
| "The existing suite still passes — the feature is good"              | The existing suite doesn't cover the new feature. The demo and edge-case tests do.           |
| "Step 1 analysis is unnecessary for a small addition"                | Scope analysis reveals reuse opportunities and blast radius. Small additions regularly grow. |

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope is known), create TaskCreate entries for all steps of this workflow before doing any other work. Mark each step in_progress when starting it, completed when done.

# Feature Mode

TDD-first feature development. Crystallise the API as a demo use-case test, drive implementation to pass it, then document.

## Step 1: Understand purpose and scope

Gather full context before writing any code:

```bash
# If issue number: fetch the full issue with comments
gh issue view <number >--comments
```

If a free-text description was provided: use the Grep tool (pattern `<keyword>`, glob `**/*.py`, path `src/`) to search for related code before spawning the analysis agent.

Spawn a **foundry:sw-engineer** agent to analyse the codebase and produce:

- **Purpose**: what problem does this feature solve, and for which users?
- **Scope**: which files and modules are likely to change (entry points, data models, tests)?
- **Compatibility**: does the feature touch public API? Will it require deprecation? Does it need backward-compat shims?
- **Reuse opportunities**: existing utilities, base classes, patterns, or abstractions that the new code can extend rather than duplicate
- **Risks**: edge cases, performance implications, or integration points that need careful handling
- **Scope challenge**: Is this the right problem? Are there simpler alternatives? What already exists that could be extended instead of built from scratch?
- **Complexity smell**: If the proposed change touches 8+ files or introduces 2+ new classes/modules, flag it explicitly — the scope may need narrowing before proceeding

**Gate**: If complexity smell was flagged, present the scope concern to the user before proceeding to Step 2.

Present the analysis summary before proceeding.

## Step 1.5: Source verification (when using external APIs or version-sensitive libraries)

Skip if the feature is purely internal to the project's own code.

**Trigger**: the feature calls an external library API — a new framework feature, a third-party SDK method, or a stdlib function that has changed in a recent Python version.

**DETECT → FETCH → CITE pipeline:**

1. **DETECT** — read `pyproject.toml` or `requirements*.txt` for the exact version and output:

   ```
   STACK DETECTED:
   - <library> <exact-version> (from pyproject.toml)
   → Fetching official docs for the relevant API.
   ```

2. **FETCH** — use WebFetch to retrieve the **specific relevant docs page** (not the homepage). Source priority: official docs > official changelog/migration guide > web standards (MDN). Never cite Stack Overflow, blog posts, or AI training data.

3. **CITE** — when implementing, embed a comment with the source URL and the key quoted passage:

   ```python
   # Docs: https://docs.example.com/v2/api/method
   # "The recommended pattern for X is Y" (v2.1 docs)
   ```

4. **Conflict** — if docs describe a pattern that conflicts with how the codebase currently uses the library:

   ```
   CONFLICT DETECTED:
   Existing code uses <old pattern>.
   <library> <version> docs recommend <new pattern> for this use case.
   Options:
   A) Use the documented pattern (may require updating existing call sites)
   B) Match existing code (works but not idiomatic for this version)
   → Which approach?
   ```

## Step 2: Write a demo use-case

Before crystallising the API, surface any non-obvious design decisions:

> ASSUMPTIONS I'M MAKING:
>
> 1. [assumption about API shape, e.g. "returning a list not a generator"]
> 2. [assumption about caller context, e.g. "called once per batch, not per item"] → Correct me now or I'll proceed with these.

Do not proceed to the demo if any assumption would materially change the API shape.

Crystallise the intended API contract before any implementation exists. Choose the form based on scope:

**Unit function / simple API** -> inline doctest:

```python
def predict(self, x: Tensor) -> Tensor:
    """
    >>> model = Classifier()
    >>> model.predict(torch.zeros(1, 3))
    tensor([0])
    """
```

**Complex feature** (setup required, side effects, multi-step flow) -> minimal example script:

```python
# examples/demo_<feature>.py  — throwaway script, run manually
from mypackage import Classifier

model = Classifier.from_pretrained("tiny")
result = model.predict_batch(["hello", "world"])
print(result)  # expected: [label, label]
```

The example script captures what the feature should feel like to use. It becomes a formal pytest test once the implementation is complete and the API is stable (end of Step 3).

Both forms must:

- Use the **exact API** the feature will expose (function name, signature, return type)
- Show the happy-path end-to-end flow a user would first reach for
- **Fail or error** against current code (the feature doesn't exist yet)

```bash
# Doctest form: confirm it fails
python -m pytest --doctest-modules src/ -v <module >.py 2>&1 | tail -10

# Script form: confirm it errors (ImportError, AttributeError, NotImplementedError)
python examples/demo_ <feature >.py 2>&1 | tail -5
```

**Gate**: demo must fail or error. If it passes, the feature may already exist — revisit Step 1.

### Review: Validate the demo

Before proceeding to implementation, critically evaluate the demo itself:

1. **Goal alignment**: does this demo address the user's stated goal, or does it solve a related but slightly different problem?
2. **API design**: is the proposed API minimal? Does it follow existing conventions in the codebase (naming, parameter order, return types)?
3. **Missing scenarios**: are there obvious happy-path variants or important failure modes the demo doesn't cover?
4. **Testability**: can this demo be automatically verified — not just `print`-and-inspect?

If any issue is found: revise the demo and re-run the gate. Do not proceed to Step 3 with a flawed API contract — the entire TDD loop is anchored to this.

## Step 3: TDD implementation loop

Drive the implementation by making tests pass, one cycle at a time:

```bash
# Baseline: confirm existing suite is green before adding any new code
python -m pytest --tb=short -q <target_test_dir >-v 2>&1 | tail -20
```

**Gate**: all existing tests must pass before proceeding. If any fail, stop — do not add new code on a broken baseline. Use `/develop:fix` to address pre-existing failures first, then return here.

Start from the Step 2 demo — it is already failing and becomes the first target. For each piece of functionality:

1. **Target the demo or write the next focused test** — first iteration uses the Step 2 demo directly; subsequent iterations add one new test per piece of new behaviour
2. **Run the existing suite — confirm all pass**:
   ```bash
   python -m pytest --tb=short -q <target_test_dir >-v 2>&1 | tail -20
   ```
3. **Run the new demo/test — confirm it fails**:
   ```bash
   # doctest form
   python -m pytest --doctest-modules src/ -v --tb=short <module >.py 2>&1 | tail -10
   # pytest form
   python -m pytest --tb=short <test_file >:: <test_name >-v
   # script form
   python examples/demo_ <feature >.py 2>&1 | tail -5
   ```
4. **Implement the minimal code** (spawn **foundry:sw-engineer** agent for non-trivial logic):
   - Reuse or extend existing code identified in Step 1 — prefer subclassing or composing over parallel reimplementation
   - Match the project's existing patterns (naming, error handling, type annotations)
5. **Run the demo/test — confirm it passes**
6. **Run the full suite** to catch regressions:
   ```bash
   python -m pytest --tb=short -q <target_test_dir >-v
   ```
7. If regressions appear: fix them before moving on — never carry forward a broken suite

Repeat until all feature tests pass and the Step 2 demo passes.

If Step 2 produced an example script: promote it into a formal pytest test now that the API is stable. Delete the script once the test is in place.

## Step 4: Review and close gaps

Read `.claude/skills/_shared/codex-prepass.md` and run the Codex pre-pass before cycle 1.

Full review of the implementation. This is a **loop** — review -> fix -> re-review until only nits remain. Maximum 3 cycles.

**Each cycle:**

**5-axis quality scan** — before the full criteria evaluation, assess the implementation on each axis:

- **Correctness**: does it match the exact API from Step 2? Edge cases and error paths covered?
- **Readability**: can another engineer understand the feature without reading the issue or demo?
- **Architecture**: does it fit established patterns? Is the abstraction level appropriate?
- **Security**: if the feature touches input handling, auth, or data storage — are those paths hardened?
- **Performance**: any N+1 patterns, unbounded collections, or unnecessary computation introduced?

Use this scan to prioritize which criteria below get deepest scrutiny.

1. Evaluate against all criteria:

   - **API match**: implementation matches the exact API from Step 2 (name, signature, return type)
   - **Scope discipline**: only Step-1-identified files changed; no drive-by fixes or unrelated edits
   - **Edge cases**: error paths, boundary inputs, None/empty handling are exercised by tests
   - **Test quality**: tests verify behavior (not implementation internals); parametrized where inputs vary
   - **Simplicity**: no dead code, no unnecessary abstractions, no over-engineering

2. For every gap found: implement the fix immediately — add missing tests, remove dead code, revert out-of-scope edits. Return to Step 3 for any substantive implementation gap that needs a new TDD cycle.

3. Re-run the full suite to confirm nothing regressed:

   ```bash
   python -m pytest --tb=short -q <target_test_dir >-v 2>&1 | tail -20
   ```

4. **If only nits remain** (style, cosmetic naming, minor formatting): document in Follow-up and exit the loop.

5. **If substantive gaps remain**: start the next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface them to the user before proceeding to Step 5.

## Step 5: Documentation

Spawn a **foundry:doc-scribe** agent to update all affected documentation:

- Add or update **docstrings** on new/modified functions and classes (Google style — Napoleon)
- Update the module-level docstring if the feature adds a significant capability
- Add the demo from Step 2 as a doctest if not already embedded
- Update `CHANGELOG.md` with a one-line entry under `Unreleased`
- If the feature changes a public API: update `README.md` usage examples

```bash
# Verify doctests pass after doc updates
python -m pytest --doctest-modules <target_module >-v 2>&1 | tail -20
```

Read `.claude/skills/_shared/quality-stack.md` and execute the Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps.

## Final Report

```
## Feature Report: <feature name>

### Purpose
[1-2 sentence description of what was built and why]

### Codebase Analysis
- Reused: [list of existing utilities/patterns leveraged]
- Modified: [files changed and why]
- New files: [list]

### Demo Use-Case
- Location: <file>::<test or doctest>
- API: [the function/class signature exposed]

### TDD Cycle
- Tests written: N
- Tests passing: N/N
- Regressions introduced: 0

### Quality
- Lint: clean / N issues fixed
- Types: clean / N issues fixed
- Doctests: passing
- Review: pass / N issues fixed (N cycles)

### Follow-up
- [any deferred items, known limitations, or suggested next steps]

## Confidence
**Score**: [0.N]
**Gaps**: [e.g., review cycle incomplete, edge cases not fully explored]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: feature spans 3+ modules, OR changes a public API, OR involves auth/payment/data scope.

- **Teammate 1 (foundry:sw-engineer, model=opus)**: implements the feature (Steps 2-3)
- **Teammate 2 (foundry:qa-specialist, model=opus)**: writes TDD tests in parallel + security checks for auth/payment/data scope
- **Teammate 3 (foundry:doc-scribe, model=sonnet)**: prepares documentation structure in parallel (Step 5)

**Coordination:**

1. Lead broadcasts Step 1 analysis: `{feature: <desc>, scope: <modules>, API: <proposed signature>}`
2. QA challenges SW's API design first — lead routes challenge back to SW before implementation starts
3. SW shares implementation details with QA so tests stay accurate
4. Lead synthesizes outputs in Steps 5 onward as normal

**Spawn prompt template:**

```
You are a [role] teammate implementing: [feature].
Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your task: [specific responsibility].
[If QA]: include security checks for any auth/payment/data-handling code.
Compact Instructions: preserve file paths, test results, API signatures. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
```

</workflow>
