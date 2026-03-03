---
name: review
description: Multi-agent code review covering architecture, tests, performance, docs, lint, security, and API design.
argument-hint: '[file, directory, or PR number to review]'
allowed-tools: Read, Write, Bash, Grep, Glob, Agent
context: fork
---

<objective>

Perform a comprehensive code review by spawning specialized sub-agents in parallel and consolidating their findings into structured feedback with severity levels.

</objective>

<inputs>

- **$ARGUMENTS**: optional file path, directory, or PR number to review.
  - If a number is given (e.g. `42`): review the PR diff
  - If a path is given: review those files
  - If omitted: review recently changed files

</inputs>

<workflow>

## Step 1: Identify scope and context (run in parallel for PR mode)

```bash
# If $ARGUMENTS is a PR number — run all four in parallel:
gh pr diff $ARGUMENTS --name-only   # files changed in PR
gh pr view $ARGUMENTS               # PR description and metadata
gh pr checks $ARGUMENTS             # CI status — don't review if CI is red
gh pr view $ARGUMENTS --json reviews,labels,milestone

# If $ARGUMENTS is a path: use it directly

# If no argument: find recently changed files
git diff --name-only HEAD~1 HEAD
```

If CI is red, report that without full review.

## Step 2: Spawn sub-agents in parallel

Launch agents simultaneously with the Task tool (agents 6 and 7 are conditional). Every agent prompt must end with:

> "End your response with: `## Confidence` / `**Score**: 0.N` (high ≥0.9 / moderate 0.7–0.9 / low \<0.7) / `**Gaps**: what limited your analysis (e.g., no runtime traces, no test execution, partial file read)`."

**Agent 1 — sw-engineer**: Review architecture, SOLID adherence, type safety, error handling, and code structure. Check for Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking issues vs suggestions.

**Agent 2 — qa-specialist**: Audit test coverage. Identify untested code paths, missing edge cases, and test quality issues. Check for ML-specific issues (non-deterministic tests, missing seed pinning). List the top 5 tests that should be added.

**Agent 3 — perf-optimizer**: Analyze code for performance issues. Look for algorithmic complexity issues, Python loops that should be NumPy/torch ops, repeated computation, unnecessary I/O. For ML code: check DataLoader config, mixed precision usage. Prioritize by impact.

**Agent 4 — doc-scribe**: Check documentation completeness. Find public APIs without docstrings, missing NumPy/Google style sections, outdated README sections, and CHANGELOG gaps. Verify examples actually run.

**Agent 5 — linting-expert**: Static analysis audit. Check ruff and mypy would pass. Identify type annotation gaps on public APIs, suppressed violations without explanation, and any missing pre-commit hooks. Flag mismatched target Python version.

**Agent 6 — `/security` skill (optional, for PRs touching auth/input/deps)**: If the diff touches authentication, user input handling, dependency updates, or serialization — spawn a general-purpose subagent that reads and executes the security skill: `Task(subagent_type="general-purpose", prompt="Read .claude/skills/security/SKILL.md and follow its workflow exactly. Scope: <files from Step 1>.")`. Skip if the PR is purely internal refactoring.

**Agent 7 — solution-architect (optional, for PRs touching public API boundaries)**: If the diff touches `__init__.py` exports, adds/modifies Protocols or ABCs, changes module structure, or introduces new public classes — evaluate API design quality, coupling impact, and backward compatibility. Skip if changes are internal implementation only.

## Step 3: Post-agent checks (run in parallel)

While agents from Step 2 are completing, run these two independent checks simultaneously:

### 3a: Ecosystem impact check (for libraries with downstream users)

```bash
# Check if changed APIs are used by downstream projects
CHANGED_EXPORTS=$(git diff HEAD~1 HEAD -- "src/**/__init__.py" | grep "^[-+]" | grep -v "^[-+][-+]" | grep -oP '\w+' | sort -u)
for export in $CHANGED_EXPORTS; do
  echo "=== $export ==="
  gh api "search/code" --field "q=$export language:python" --jq '.items[:5] | .[].repository.full_name' 2>/dev/null
  # Note: GitHub code search API is rate-limited (~30 req/min); empty results may indicate rate limiting, not absence of usage
done

# Check if deprecated APIs have migration guides
git diff HEAD~1 HEAD | grep -A2 "deprecated"
```

### 3b: OSS checks

```bash
# Check for new dependencies — license compatibility
git diff HEAD~1 HEAD -- pyproject.toml requirements*.txt

# Check for secrets accidentally committed
git diff HEAD~1 HEAD | grep -iE "(password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}"

# Check for API stability: are public APIs being removed without deprecation?
git diff HEAD~1 HEAD -- "src/**/__init__.py"

# Check CHANGELOG was updated
git diff HEAD~1 HEAD -- CHANGELOG.md CHANGES.md
```

## Step 4: Cross-validate critical/blocking findings

Before consolidating, for any finding classified as `CRITICAL` or `[blocking]` from Step 2, spawn a second independent agent to verify. Use the **same agent type** that raised the finding (e.g., sw-engineer verifies sw-engineer's critical finding):

```
Independently review <file or diff section> for the following specific issue: "<finding description>".
Do NOT read the previous agent's output.
Is this a real critical/blocking issue? Confirm or refute with reasoning.
Include your ## Confidence block.
```

Classify:

- **Confirmed by both** → include as critical/blocking ✓
- **Second pass disagrees** → downgrade to `high` with note "unconfirmed — one of two passes flagged this"
- **Both agree lower severity** → re-classify accordingly

Only apply cross-validation to `CRITICAL`/`[blocking]` findings — high and lower go directly to Step 5.

## Step 5: Consolidate findings

```
## Code Review: [target]

### [blocking] Critical (must fix before merge)
- [bugs, security issues, data corruption risks]
- Severity: CRITICAL / HIGH

### Architecture & Quality
- [sw-engineer findings]
- [blocking] issues marked explicitly
- [nit] suggestions marked explicitly

### Test Coverage Gaps
- [qa-specialist findings — top 5 missing tests]
- For ML code: non-determinism or missing seed issues

### Performance Concerns
- [perf-optimizer findings — ranked by impact]
- Include: current behavior vs expected improvement

### Documentation Gaps
- [doc-scribe findings]
- Public API without docstrings listed explicitly

### Static Analysis
- [linting-expert findings — ruff violations, mypy errors, annotation gaps]

### API Design (if applicable)
- [solution-architect findings — coupling, API surface, backward compat]
- Public API changes: [intentional / accidental leak]
- Deprecation path: [provided / missing]

### OSS Checks
- New dependencies: [list, license status]
- API stability: [any public API removed without deprecation?]
- CHANGELOG: [updated / not updated]
- Secrets scan: [clean / found: file:line]

### Recommended Next Steps
1. [most important action]
2. [second most important]
3. [third]

### Review Confidence
| Agent | Score | Label | Gaps |
|-------|-------|-------|------|
| sw-engineer | 0.88 | high | — |
| qa-specialist | 0.65 | ⚠ low | no test execution; coverage unverifiable without running suite |
| perf-optimizer | 0.72 | moderate | no profiling data; estimates from static analysis only |

**Aggregate**: min 0.65 / median 0.N
[⚠ LOW CONFIDENCE: qa-specialist could not verify test execution — treat coverage findings as indicative, not conclusive]
```

After parsing confidence scores: if any agent scored < 0.7, prepend **⚠ LOW CONFIDENCE** to that agent's findings section and explicitly state the gap. Do not silently drop uncertain findings — flag them so the reviewer can decide whether to investigate further.

After printing the consolidated report, write the full content to `tasks/output-review-$(date +%Y-%m-%d).md` using the Write tool and notify: `→ saved to tasks/output-review-$(date +%Y-%m-%d).md`

## Step 6: Delegate implementation follow-up (optional)

After consolidating findings, identify tasks from the review that Codex can implement directly — not style violations (those are handled by pre-commit hooks), but work that requires writing meaningful code or documentation grounded in the actual implementation.

**Delegate to Codex when you can write an accurate, specific brief:**

- Public functions with no docstrings — read the implementation first, then describe what each one does so Codex can write a real 6-section docstring, not a placeholder
- Missing test coverage for a concrete, well-defined behaviour — describe the exact scenario to test
- A consistent rename identified across multiple files — name both the old and new symbol and why it was flagged

**Do not delegate — these require human judgment:**

- Architectural issues, logic errors, security vulnerabilities, or behavioural changes
- Any task where you cannot write a precise description without guessing

For each task, read the relevant code, form an accurate brief, then spawn:

```
Task(
  subagent_type="general-purpose",
  prompt="Read .claude/skills/codex/SKILL.md and follow its workflow exactly.
Task: use the <agent> to <specific task with accurate description of what the code does>.
Target: <file>."
)
```

Example prompt: `"use the qa-specialist to add a test for StreamReader.read_chunk() in tests/test_reader.py — the method should raise ValueError when called after close(), currently there is no test for this path"`

The subagent handles pre-flight, dispatch, validation, and patch capture. If Codex is unavailable it reports gracefully — do not block on this step.

Print a `### Codex Delegation` section to the terminal summarizing what was auto-implemented (do not re-write the output file).

</workflow>

<notes>

- Critical issues are always surfaced regardless of scope
- Skip sections where no issues were found — don't pad with "looks good"
- In PR mode: check CI status first — if red, report that without full review
- Blocking issues require explicit `[blocking]` prefix so author knows what must change
- Follow-up chains:
  - `[blocking]` bugs or regressions → `/fix` to reproduce with test and apply targeted fix
  - Structural or quality issues → `/refactor` for test-first improvements
  - Security findings in auth/input/deps → `/security` for a dedicated deep audit
  - Mechanical issues beyond what Step 6 auto-fixed → `/codex` to delegate additional tasks
  - Docstrings, type annotations, renames, and other mechanical findings → `/resolve` (no args) to auto-implement all fixable items via Codex

</notes>
