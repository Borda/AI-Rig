---
name: feature
description: TDD-first feature development orchestrator. Analyses purpose, scope, and codebase compatibility before writing a single line of implementation — starts with a demo use-case doctest/test to nail the API contract, then drives implementation through TDD, and finishes with doc updates, QA, linting, and a full review pass.
argument-hint: <feature description or issue #> ["target module or directory"]
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent
---

<objective>

Implement new features with a TDD-first discipline that mirrors how a senior software developer thinks: understand the codebase deeply before touching it, crystallise the intended API as a demo doctest or test, drive the implementation to make that test pass, then complete the cycle with documentation, QA, linting, and code review. If any issue is found in the review pass, fix it and repeat the cycle until the feature is clean.

</objective>

<inputs>

- **$ARGUMENTS**: required
  - First token(s): feature description in plain text (e.g., `"add batched predict() method to Classifier"`) OR a GitHub issue number (e.g., `42` — fetched via `gh issue view`)
  - Optional second quoted token: target module or directory to scope the analysis (e.g., `"src/classifier"`)
  - If no target given: let the codebase analysis in Step 1 identify the right location

</inputs>

<workflow>

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create a new task.

## Step 1: Understand purpose and scope

Gather full context before writing any code:

```bash
# If issue number: fetch the full issue with comments
gh issue view <number> --comments
```

If a free-text description was provided: use the Grep tool (pattern `<keyword>`, glob `**/*.py`, path `src/`) to search for related code before spawning the analysis agent.

Spawn a **sw-engineer** agent to analyse the codebase and produce:

- **Purpose**: what problem does this feature solve, and for which users?
- **Scope**: which files and modules are likely to change (entry points, data models, tests)?
- **Compatibility**: does the feature touch public API? Will it require deprecation? Does it need backward-compat shims?
- **Reuse opportunities**: existing utilities, base classes, patterns, or abstractions that the new code can extend rather than duplicate
- **Risks**: edge cases, performance implications, or integration points that need careful handling

Present the analysis summary before proceeding.

## Step 2: Write a demo use-case

Crystallise the intended API contract before any implementation exists. Choose the form based on scope:

**Unit function / simple API** → inline doctest:

```python
def predict(self, x: Tensor) -> Tensor:
    """
    >>> model = Classifier()
    >>> model.predict(torch.zeros(1, 3))
    tensor([0])
    """
```

**Complex feature** (setup required, side effects, multi-step flow) → minimal example script:

```python
# examples/demo_<feature>.py  — throwaway script, run manually
from mypackage import Classifier

model = Classifier.from_pretrained("tiny")
result = model.predict_batch(["hello", "world"])
print(result)  # expected: [label, label]
```

The example script is not a test yet — it just captures what the feature should feel like to use. It becomes a formal pytest test once the implementation is complete and the API is stable (end of Step 3).

Both forms must:

- Use the **exact API** the feature will expose (function name, signature, return type)
- Show the happy-path end-to-end flow a user would first reach for
- **Fail or error** against current code (the feature doesn't exist yet)

```bash
# Doctest form: confirm it fails
python -m pytest --doctest-modules src/<module>.py -v 2>&1 | tail -10

# Script form: confirm it errors (ImportError, AttributeError, NotImplementedError)
python examples/demo_<feature>.py 2>&1 | tail -5
```

**Gate**: demo must fail or error. If it passes, the feature may already exist — revisit Step 1.

## Step 3: TDD implementation loop

Drive the implementation by making tests pass, one cycle at a time:

```bash
# Baseline: confirm existing suite is green before adding any new code
python -m pytest <target_test_dir> -v --tb=short -q 2>&1 | tail -20
```

**Gate**: all existing tests must pass before proceeding. If any fail, stop — do not add new code on a broken baseline. The failures are a pre-existing bug, not part of this feature; use `/fix` to address them first, then return here.

Start from the Step 2 demo — it is already failing and becomes the first target. For each piece of functionality:

1. **Target the demo or write the next focused test** — first iteration uses the Step 2 demo directly; subsequent iterations add one new test per piece of new behaviour
2. **Run the existing suite — confirm all pass**:
   ```bash
   python -m pytest <target_test_dir> -v --tb=short -q 2>&1 | tail -20
   ```
   If any existing test fails: stop, fix it before proceeding — unrelated failures pollute the signal.
3. **Run the new demo/test — confirm it fails** (proves the feature is not yet implemented):
   ```bash
   # doctest form
   python -m pytest --doctest-modules src/<module>.py -v --tb=short 2>&1 | tail -10
   # pytest form
   python -m pytest <test_file>::<test_name> -v --tb=short
   # script form
   python examples/demo_<feature>.py 2>&1 | tail -5
   ```
4. **Implement the minimal code** to make it pass (spawn **sw-engineer** agent for non-trivial logic):
   - Reuse or extend existing code identified in Step 1 — prefer subclassing or composing over parallel reimplementation; if new code would share substantial logic (>~30%) with existing code, the design likely needs inheritance or delegation rather than duplication
   - Match the project's existing patterns (naming, error handling, type annotations)
   - Keep each change small and focused
5. **Run the demo/test — confirm it passes** (same command as sub-step 3 above)
6. **Run the full suite** to catch regressions:
   ```bash
   python -m pytest <target_test_dir> -v --tb=short -q
   ```
7. If regressions appear: fix them before moving on — never carry forward a broken suite

Repeat until all feature tests pass and the demo use-case from Step 2 passes.

If Step 2 produced an example script: promote it into a formal pytest test now that the API is stable. Delete the script once the test is in place.

## Step 4: Documentation

Spawn a **doc-scribe** agent to update all affected documentation:

- Add or update **docstrings** on new/modified functions and classes (NumPy style)
- Update the module-level docstring if the feature adds a significant capability
- Add the demo from Step 2 as a doctest if not already embedded
- Update `CHANGELOG.md` with a one-line entry under `Unreleased`
- If the feature changes a public API: update `README.md` usage examples

```bash
# Verify doctests pass after doc updates
python -m pytest --doctest-modules <target_module> -v 2>&1 | tail -20
```

## Step 5: QA, linting, and review

Run the full quality stack:

```bash
# Linting and formatting
ruff check <changed_files> --fix
ruff format <changed_files>

# Type checking
mypy <changed_files> --no-error-summary 2>&1 | head -30

# Full test suite
python -m pytest <test_dir> -v --tb=short -q

# Doctests
python -m pytest --doctest-modules <target_module> -v 2>&1 | tail -20
```

Spawn a **linting-expert** agent if mypy or ruff issues require non-trivial fixes.

Then invoke the **`/review`** skill for a full multi-agent code review:

```
/review <changed_files_or_PR>
```

The review covers: architecture, test coverage, performance, documentation, lint, security, and API design.

## Step 6: Fix and repeat (if issues found)

If the review (or any earlier step) surfaces issues:

1. Triage findings by severity — fix `critical` and `high` before considering the feature done
2. For each finding, apply the minimal targeted fix
3. Re-run the affected step (e.g., if a test was wrong: back to Step 3; if docs are incomplete: back to Step 4)
4. Re-run the full quality stack from Step 5
5. Repeat until `/review` returns no `critical` or `high` findings

## Step 7: Delegate implementation follow-up (optional)

Inspect what was built (`git diff HEAD --stat`) and identify real implementation tasks that Codex can complete — not style violations (those are handled by pre-commit hooks), but work that requires understanding the code and writing meaningful content.

**Delegate to Codex when you can write an accurate, specific brief:**

- New public functions/classes need full 6-section docstrings — read the implementation first, then describe what each one does, its arguments, return value, and any invariants
- New functionality needs tests beyond what qa-specialist already wrote — describe the exact behaviour to be tested
- New module or class needs a usage example that demonstrates the intended API contract

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
  # Path is project-relative; if the codex skill moves, update this path.
)
```

Example prompt: `"use the doc-scribe to add a 6-section NumPy-style docstring to BatchTransform.apply() in src/transforms.py — the method applies per-sample normalization using a precomputed mean/std tensor and returns a tensor of the same shape as input"`

The subagent handles pre-flight, dispatch, validation, and patch capture. If Codex is unavailable it reports gracefully — do not block on this step.

Include a `### Codex Delegation` line in the Step 8 report only if this step ran.

## Step 8: Final report

Output a structured summary:

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
**Gaps**: [e.g., review cycle incomplete, edge cases not fully explored, integration tests not run]
```

</workflow>

<notes>

- **Green baseline before adding**: existing tests must pass before any new code is written — this is the primary assumption that distinguishes `/feature` from `/fix`. `/fix` starts with a failing test to prove the bug; `/feature` starts with a passing suite to prove the foundation is sound.
- **Demo first, implement second**: the demo use-case is not a throwaway — it survives as living documentation and a regression guard
- **Reuse over reinvent**: Step 1 analysis is mandatory to find existing patterns; duplicating code is a review failure
- **Never skip the review cycle**: `/review` is not optional — it catches what TDD misses (API design, security, performance)
- **Fix loop is bounded**: after 3 cycles without reaching clean review, pause and re-scope with the user — the feature may need architectural rethinking
- **`disable-model-invocation: true`**: Claude will not auto-invoke this skill; you must type `/feature <description>` explicitly. Once invoked, the parent model executes all workflow steps — this flag only prevents automatic background triggering.
- Related agents: `sw-engineer` (analysis + implementation), `doc-scribe` (documentation), `linting-expert` (type safety + style) — `qa-specialist` is invoked indirectly via `/review`, not spawned directly by this skill
- Follow-up chains:
  - Feature changes public API → `/release` to prepare CHANGELOG entry and migration guide
  - Feature is performance-sensitive → `/optimize` for baseline + bottleneck analysis
  - Feature touches `.claude/` config files → spawn a `self-mentor` agent for the modified files, then `/sync` to propagate
  - Mechanical follow-up needed beyond what Step 7 handled → `/codex` to delegate additional tasks

</notes>
