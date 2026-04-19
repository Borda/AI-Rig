---
name: feature
description: TDD-first feature development — crystallise API as a demo test, drive implementation to pass it, run quality stack and progressive review loop.
argument-hint: <goal>
effort: high
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

TDD-first feature development. Crystallise API as demo use-case test, drive implementation to pass it, close quality gaps with review, docs, quality stack.

NOT for: bug fixes (use `/develop:fix`); `.claude/` config changes (use `/manage`).

</objective>

<workflow>

<!-- Agent Resolution: identical across all develop skills -->

## Agent Resolution

> **Foundry plugin check**: run `ls ~/.claude/plugins/cache/ 2>/dev/null | grep -q foundry` (exit 0 = installed). If check fails or uncertain, proceed as if foundry available — it is common case; only fall back if agent dispatch explicitly fails.

When foundry **not** installed, substitute `foundry:X` references with `general-purpose` and prepend role description plus `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:doc-scribe` | `general-purpose` | `sonnet` | `You are a documentation specialist. Write Google-style docstrings and keep README content accurate and concise.` |

Skills with `--team` mode: team spawning with fallback agents still works but lower-quality output.

## Anti-Rationalizations

| Temptation | Reality |
| --- | --- |
| "The feature is clear — I can skip the demo and go straight to code" | Without a crystallized API contract, implementation drifts. The demo is the spec. |
| "I know this library — no need to check docs" | Training data contains deprecated patterns. One fetch prevents hours of rework. |
| "I'll write tests after the implementation is stable" | Tests drive design. Writing them first reveals API problems before they are baked in. |
| "The existing suite still passes — the feature is good" | The existing suite doesn't cover the new feature. The demo and edge-case tests do. |
| "Step 1 analysis is unnecessary for a small addition" | Scope analysis reveals reuse opportunities and blast radius. Small additions regularly grow. |

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: immediately after Step 1 (scope known), create TaskCreate entries for all steps before any other work. Mark each step in_progress when starting, completed when done.

# Feature Mode

## Step 1: Understand purpose and scope

Gather full context before writing any code:

```bash
# If issue number: fetch the full issue with comments
gh issue view <number >--comments
```

If free-text description provided: use Grep tool (pattern `<keyword>`, glob `**/*.py`, path `src/`) to search related code before spawning analysis agent.

**Structural context** (codemap, if installed) — soft PATH check, silently skip if `scan-query` not found:

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 5
fi
```

If results returned: prepend `## Structural Context (codemap)` block to foundry:sw-engineer spawn prompt with hotspot JSON. Gives analysis agent immediate complexity awareness without cold Glob/Grep exploration. If `scan-query` not found or index missing: proceed silently — don't mention codemap to user.

Spawn **foundry:sw-engineer** agent to analyse codebase and produce:

- **Purpose**: what problem does feature solve, and for which users?
- **Scope**: which files and modules likely change (entry points, data models, tests)?
- **Compatibility**: does feature touch public API? Require deprecation? Need backward-compat shims?
- **Reuse opportunities**: existing utilities, base classes, patterns, abstractions new code can extend instead of duplicate
- **Risks**: edge cases, performance implications, integration points needing careful handling
- **Scope challenge**: Right problem? Simpler alternatives? What already exists that could extend instead of build from scratch?
- **Complexity smell**: If proposed change touches 8+ files or introduces 2+ new classes/modules, flag explicitly — scope may need narrowing before proceeding

**Gate**: If complexity smell flagged, present scope concern to user before proceeding to Step 2.

Present analysis summary before proceeding.

## Step 1.5: Source verification (when using external APIs or version-sensitive libraries)

Skip if feature calls no external library APIs — no new framework features, no third-party SDK methods, no stdlib functions changed in recent Python version.

**Trigger**: feature calls external library API — new framework feature, third-party SDK method, or stdlib function changed in recent Python version.

**DETECT → FETCH → CITE pipeline:**

1. **DETECT** — read `pyproject.toml` or `requirements*.txt` for exact version and output:

   ```
   STACK DETECTED:
   - <library> <exact-version> (from pyproject.toml)
   → Fetching official docs for the relevant API.
   ```

2. **FETCH** — use WebFetch to retrieve **specific relevant docs page** (not homepage). Source priority: official docs > official changelog/migration guide > web standards (MDN). Never cite Stack Overflow, blog posts, or AI training data.

3. **CITE** — when implementing, embed comment with source URL and key quoted passage:

   ```python
   # Docs: https://docs.example.com/v2/api/method
   # "The recommended pattern for X is Y" (v2.1 docs)
   ```

4. **Conflict** — if docs describe pattern conflicting with how codebase currently uses library:

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

Before crystallising API, surface non-obvious design decisions:

> ASSUMPTIONS I'M MAKING:
>
> 1. [assumption about API shape, e.g. "returning a list not a generator"]
> 2. [assumption about caller context, e.g. "called once per batch, not per item"] → Correct me now or I'll proceed with these.

Don't proceed to demo if any assumption would materially change API shape.

Crystallise intended API contract before any implementation. Choose form based on scope:

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

Example script captures what feature should feel like to use. Becomes formal pytest test once implementation complete and API stable (end of Step 3).

Both forms must:

- Use **exact API** feature will expose (function name, signature, return type)
- Show happy-path end-to-end flow user would first reach for
- **Fail or error** against current code (feature doesn't exist yet)

```bash
# Doctest form: confirm it fails
python -m pytest --doctest-modules src/ -v <module >.py 2>&1 | tail -10

# Script form: confirm it errors (ImportError, AttributeError, NotImplementedError)
python examples/demo_ <feature >.py 2>&1 | tail -5
```

**Gate**: demo must fail or error. If passes, feature may already exist — revisit Step 1.

### Review: Validate the demo

Before proceeding to implementation, critically evaluate demo:

1. **Goal alignment**: does demo address user's stated goal, or slightly different problem?
2. **API design**: is proposed API minimal? Follows existing codebase conventions (naming, parameter order, return types)?
3. **Missing scenarios**: obvious happy-path variants or important failure modes demo doesn't cover?
4. **Testability**: can demo be automatically verified — not just `print`-and-inspect?

If issue found: revise demo and re-run gate. Don't proceed to Step 3 with flawed API contract — entire TDD loop anchored to this.

## Step 3: TDD implementation loop

Drive implementation by making tests pass, one cycle at a time:

```bash
# Baseline: confirm existing suite is green before adding any new code
python -m pytest --tb=short -q <target_test_dir >-v 2>&1 | tail -20
```

**Gate**: all existing tests must pass before proceeding. If any fail, stop — don't add new code on broken baseline. Use `/develop:fix` to address pre-existing failures first, then return here.

Start from Step 2 demo — already failing, becomes first target. For each piece of functionality:

1. **Target demo or write next focused test** — first iteration uses Step 2 demo directly; subsequent iterations add one new test per piece of new behaviour
2. **Run existing suite — confirm all pass**:
   ```bash
   python -m pytest --tb=short -q <target_test_dir >-v 2>&1 | tail -20
   ```
3. **Run new demo/test — confirm it fails**:
   ```bash
   # doctest form
   python -m pytest --doctest-modules src/ -v --tb=short <module >.py 2>&1 | tail -10
   # pytest form
   python -m pytest --tb=short <test_file >:: <test_name >-v
   # script form
   python examples/demo_ <feature >.py 2>&1 | tail -5
   ```
4. **Implement minimal code** (spawn **foundry:sw-engineer** agent for non-trivial logic):
   - Reuse or extend existing code identified in Step 1 — prefer subclassing or composing over parallel reimplementation
   - Match project's existing patterns (naming, error handling, type annotations)
5. **Run demo/test — confirm it passes**
6. **Run full suite** to catch regressions:
   ```bash
   python -m pytest --tb=short -q <target_test_dir >-v
   ```
7. If regressions appear: fix before moving on — never carry forward broken suite

Repeat until all feature tests pass and Step 2 demo passes.

If Step 2 produced example script: promote into formal pytest test now that API is stable. Delete script once test in place.

## Step 4: Review and close gaps

Full review of implementation. **Loop** — review -> fix -> re-review until only nits remain. Maximum 3 cycles.

**Each cycle:**

**5-axis quality scan** — before full criteria evaluation, assess implementation on each axis:

- **Correctness**: matches exact API from Step 2? Edge cases and error paths covered?
- **Readability**: can another engineer understand feature without reading issue or demo?
- **Architecture**: fits established patterns? Abstraction level appropriate?
- **Security**: if feature touches input handling, auth, or data storage — are those paths hardened?
- **Performance**: N+1 patterns, unbounded collections, unnecessary computation introduced?

Use scan to prioritize which criteria below get deepest scrutiny.

1. Evaluate against all criteria:

   - **API match**: implementation matches exact API from Step 2 (name, signature, return type)
   - **Scope discipline**: only Step-1-identified files changed; no drive-by fixes or unrelated edits
   - **Edge cases**: error paths, boundary inputs, None/empty handling exercised by tests
   - **Test quality**: tests verify behavior (not implementation internals); parametrized where inputs vary
   - **Simplicity**: no dead code, unnecessary abstractions, over-engineering

2. For every gap found: implement fix immediately — add missing tests, remove dead code, revert out-of-scope edits. Return to Step 3 for substantive implementation gap needing new TDD cycle.

3. Re-run full suite to confirm nothing regressed:

   ```bash
   python -m pytest --tb=short -q <target_test_dir >-v 2>&1 | tail -20
   ```

4. **If only nits remain** (style, cosmetic naming, minor formatting): document in Follow-up and exit loop.

5. **If substantive gaps remain**: start next cycle (max 3 total).

**After 3 cycles**: if substantive issues remain, stop — surface to user before proceeding to Step 5.

## Step 5: Documentation

Spawn **foundry:doc-scribe** agent to update all affected docs:

- Add or update **docstrings** on new/modified functions and classes (Google style — Napoleon)
- Update module-level docstring if feature adds significant capability
- Add demo from Step 2 as doctest if not already embedded
- Update `CHANGELOG.md` with one-line entry under `Unreleased`
- If feature changes public API: update `README.md` usage examples

```bash
# Verify doctests pass after doc updates
python -m pytest --doctest-modules <target_module >-v 2>&1 | tail -20
```

Read `.claude/skills/_shared/quality-stack.md` and execute Branch Safety Guard, Quality Stack, Codex Pre-pass, Progressive Review Loop, and Codex Mechanical Delegation steps.

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
**Score**: 0.N — [high ≥0.9 | moderate 0.8–0.9 | low <0.8 ⚠]
**Gaps**: [e.g., review cycle incomplete, edge cases not fully explored]
**Refinements**: N passes.
```

## Team Assignments

**When to use team mode**: feature spans 3+ modules, OR changes public API, OR involves auth/payment/data scope.

- **Teammate 1 (foundry:sw-engineer, model=opus)**: implements feature (Steps 2-3)
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
