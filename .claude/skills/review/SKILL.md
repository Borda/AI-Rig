---
name: review
description: Multi-agent code review covering architecture, tests, performance, docs, lint, security, and Application Programming Interface (API) design.
argument-hint: '[file, directory, or PR number] [--reply]'
allowed-tools: Read, Write, Bash, Grep, Agent, TaskCreate, TaskUpdate
context: fork
model: opus
---

<objective>

Perform a comprehensive code review by spawning specialized sub-agents in parallel and consolidating their findings into structured feedback with severity levels.

</objective>

<inputs>

- **$ARGUMENTS**: optional file path, directory, or Pull Request (PR) number to review.
  - If a number is given (e.g. `42`): review the PR diff
  - If a path is given: review those files
  - If omitted: review recently changed files
  - `--reply`: after review, spawn oss-shepherd to draft a contributor-facing PR comment from the findings
  - **Scope**: this skill reviews Python source code only. If the input is a non-Python file (YAML, JSON, shell script, etc.), state that it is out of scope and suggest the appropriate tool — do not produce findings.

</inputs>

<constants>
<!-- Background agent health monitoring (CLAUDE.md §8) — applies to Step 3 parallel agent spawns -->
MONITOR_INTERVAL=300   # 5 minutes between polls
HARD_CUTOFF=900        # 15 minutes of no file activity → declare timed out
EXTENSION=300          # one +5 min extension if output file explains delay
</constants>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create a new task.

## Step 1: Identify scope and context (run in parallel for PR mode)

If `$ARGUMENTS` contains `--reply`, strip it from the arguments and set `REPLY_MODE=true`. Pass the remaining arguments to the rest of the workflow.

```bash
# If $ARGUMENTS is a PR number — run all four in parallel:
gh pr diff $ARGUMENTS --name-only   # files changed in PR                    # timeout: 6000
gh pr view $ARGUMENTS               # PR description and metadata             # timeout: 6000
gh pr checks $ARGUMENTS             # CI status — don't review if CI is red  # timeout: 15000
gh pr view $ARGUMENTS --json reviews,labels,milestone                         # timeout: 6000

# If $ARGUMENTS is a path: use it directly

# If no argument: find recently changed files
git diff --name-only HEAD~1 HEAD  # timeout: 3000
```

If Continuous Integration (CI) is red, report that without full review.

### Scope pre-check

Before spawning agents, classify the diff:

- Count files changed, lines added/removed, new classes/modules introduced
- Classify as: **FIX** (\<3 files, \<50 lines), **REFACTOR** (no new public API), **FEATURE** (new public API or module), or **MIXED**
- **Complexity smell**: if 8+ files changed, note in the report header

Use classification to skip optional agents:

- FIX scope → skip Agent 3 (perf-optimizer) and Agent 6 (solution-architect)
- REFACTOR scope → skip Agent 6 (solution-architect)
- FEATURE/MIXED → spawn all agents

### Linked issue analysis (PR mode only)

Parse the PR body (from `gh pr view $ARGUMENTS`) for issue references (`Closes #N`, `Fixes #N`, `Resolves #N`, `refs #N` — case-insensitive). Extract all referenced issue numbers into `ISSUE_NUMS` (list). Cap at 3 issues maximum.

If `ISSUE_NUMS` is non-empty, spawn one **sw-engineer** agent per issue at the start of Step 2 (in parallel with Codex co-review — both are independent of each other). Each issue agent should:

- Fetch issue: `gh issue view <N> --json title,body,comments,state,labels`
- Fetch comments: `gh issue view <N> --comments`
- Produce `/analyse`-style output: Summary, Root Cause Hypotheses table (top 3), Code Evidence for top hypothesis
- Write full analysis to `$RUN_DIR/issue-<N>.md` (file-handoff protocol)
- Return compact JSON envelope only: `{"status":"done","issue":N,"root_cause":"<one-line summary>","file":"$RUN_DIR/issue-<N>.md","confidence":0.N}`

If `ISSUE_NUMS` is empty, skip all issue-related checks in downstream steps.

## Step 2: Codex co-review

Set up the run directory (shared by Codex and all agent spawns in Step 3):

```bash
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR="_reviews/$TIMESTAMP"
mkdir -p "$RUN_DIR"
```

Check availability:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex' && echo "codex (openai-codex) available" || echo "⚠ codex (openai-codex) not found — skipping co-review"
```

If Codex is available, run a comprehensive review on the diff:

```bash
CODEX_OUT="$RUN_DIR/codex.md"
Skill("codex:adversarial-review", args="--wait look for bugs, missed edge cases, incorrect logic, and inconsistencies with existing code patterns")
```

After Codex writes `$RUN_DIR/codex.md`, extract a compact seed list (≤10 items, `[{"loc":"file:line","note":"..."}]`) to inject into agent prompts in Step 3 as pre-flagged issues to verify or dismiss. If Codex was skipped or found nothing, proceed with an empty seed.

## Step 3: Spawn sub-agents in parallel

**File-based handoff**: read `.claude/skills/_shared/file-handoff-protocol.md`. The run directory was created in Step 2 (`$RUN_DIR`).

Launch agents simultaneously with the Agent tool (security augmentation is folded into Agent 1 — not a separate spawn; Agent 6 is optional). Every agent prompt must end with:

> "Write your FULL findings (all sections, Confidence block) to `$RUN_DIR/<agent-name>.md` using the Write tool — where `<agent-name>` is e.g. `sw-engineer`, `qa-specialist`, `perf-optimizer`, `doc-scribe`, `linting-expert`, `solution-architect`. Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":1,\"medium\":2},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.88}`"

**Agent 1 — sw-engineer**: Review architecture, SOLID adherence, type safety, error handling, and code structure. Check for Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking issues vs suggestions.

**Error path analysis** (for new/changed code in the diff): For each error-handling path introduced or modified, produce a table:

| Location | Exception/Error | Caught? | Action if caught | User-visible? |
| -------- | --------------- | ------- | ---------------- | ------------- |

Flag rules:

- Caught=No + User-visible=Silent → **HIGH** (unhandled error path)
- Caught=Yes + Action=`pass` or bare `except` → **MEDIUM** (swallowed error)
- Cap at 15 rows. Focus on new/changed paths only, not the entire codebase.

Read the review checklist (use the Read tool to read `.claude/skills/review/checklist.md`) — apply CRITICAL/HIGH patterns as severity anchors. Respect the suppressions list.

If `ISSUE_NUMS` is non-empty, linked issue analysis files exist at `$RUN_DIR/issue-*.md`. Read them. Evaluate whether the code changes address the root cause identified in each linked issue — not just the symptom or the PR description. If the PR addresses only a symptom while the root cause remains unfixed, flag as `[blocking] HIGH — root cause misalignment`. If the PR description diverges from the issue's stated problem (solving something different than what was reported), flag as `HIGH — PR/issue scope divergence`.

**Agent 2 — qa-specialist**: Audit test coverage. Identify untested code paths, missing edge cases, and test quality issues. Check for Machine Learning (ML)-specific issues (non-deterministic tests, missing seed pinning). List the top 5 tests that should be added. Also check explicitly for missing tests in these patterns (these are Ground Truth (GT)-level findings, not afterthoughts):

- Concurrent access to shared state (when locks or shared variables are present)
- Error paths: calling methods in wrong order (e.g., `log()` before `start()`)
- Resource cleanup on exception (file handles, database connections)
- Boundary conditions for division, empty collections, and zero-count inputs
- Type-coercion boundary inputs: for functions that parse or convert strings to typed values (int(), float(), datetime), test with inputs that are near-valid (float strings for int parsers, empty strings, very large values, None) — these are common omissions.

**Consolidation rule**: Report each test gap as one finding with a concise list of test scenarios, not as separate findings per scenario. Format: "Missing tests for `parse_numeric()`: empty string, None, very large integers, float-string for int parser." This keeps the test coverage section actionable and prevents the section from exceeding 5 items.

If `ISSUE_NUMS` is non-empty, linked issue analysis files exist at `$RUN_DIR/issue-*.md`. Read them. Check that tests cover the specific reproduction scenario described in the linked issue. If the issue includes a minimal reproduction or error trace that is not covered by new or existing tests, flag as `HIGH — issue reproduction not tested`.

**Agent 3 — perf-optimizer**: Analyze code for performance issues. Look for algorithmic complexity issues, Python loops that should be NumPy/torch ops, repeated computation, unnecessary Input/Output (I/O). For ML code: check DataLoader config, mixed precision usage. Prioritize by impact.

**Agent 4 — doc-scribe**: Check documentation completeness. Find public APIs without docstrings, missing Google style sections, outdated README sections, and CHANGELOG gaps. Verify examples actually run.

- **Algorithmic accuracy check**: For functions that compute mathematical results (moving averages, statistics, transforms, distances), verify that the docstring's behavioral claims match what the implementation actually computes. Specifically: does the described output shape/length match the actual algorithm? Does the standard name (e.g. "moving average") correspond to the actual implementation behavior (expanding-window vs. sliding-window)? If the implementation deviates from the conventional definition, flag as MEDIUM — the docstring must document the deviation, not just state the standard definition. **Deprecation check**: Always check whether datetime, os.path, or other stdlib functions used in the code have been deprecated in Python 3.10+ (e.g., `datetime.utcnow()` deprecated in 3.12, `os.path` vs `pathlib`). Flag deprecated stdlib usage as MEDIUM with the replacement. This is a frequent omission in general review but reliably caught by doc-scribe with this explicit trigger.

**Agent 5 — linting-expert**: Static analysis audit. Check ruff and mypy would pass. Identify type annotation gaps on public APIs, suppressed violations without explanation, and any missing pre-commit hooks. Flag mismatched target Python version.

**Security augmentation (conditional — fold into Agent 1 prompt, not a separate spawn)**: If the diff touches authentication, user input handling, dependency updates, or serialization — add to the sw-engineer agent prompt (Agent 1 above): check for Structured Query Language (SQL) injection, Cross-Site Scripting (XSS), insecure deserialization, hardcoded secrets, and missing input validation. Run `pip-audit` if dependency files changed. Skip if the PR is purely internal refactoring.

**Agent 6 — solution-architect (optional, for PRs touching public API boundaries)**: If the diff touches `__init__.py` exports, adds/modifies Protocols or Abstract Base Classes (ABCs), changes module structure, or introduces new public classes — evaluate API design quality, coupling impact, and backward compatibility. Skip if changes are internal implementation only.

**Health monitoring** (CLAUDE.md §8): after spawning all agents, create a checkpoint:

```bash
REVIEW_CHECKPOINT="/tmp/review-check-$(date +%s)"
touch "$REVIEW_CHECKPOINT"
```

Every `$MONITOR_INTERVAL` seconds, run `find "$RUN_DIR" -newer "$REVIEW_CHECKPOINT" -type f | wc -l` — new files = alive; zero new files for `$HARD_CUTOFF` seconds = stalled. Grant one `$EXTENSION` extension if the output file tail explains the delay. On timeout: read partial output; mark the agent ⏱ in the final report. Never silently omit timed-out agents.

## Step 4: Post-agent checks (run in parallel)

While agents from Step 3 are completing, run these two independent checks simultaneously:

### 4a: Ecosystem impact check (for libraries with downstream users)

```bash
# Check if changed APIs are used by downstream projects
# Rate-limit guard: if gh api returns HTTP 429, wait 10 seconds and retry once.
# If still rate-limited, log "rate-limited — downstream search may be incomplete" and continue.
# --paginate is available for large result sets but increases rate-limit exposure; omit unless completeness is critical.
CHANGED_EXPORTS=$(git diff $(git merge-base HEAD origin/main) HEAD -- "src/**/__init__.py" | grep "^[-+]" | grep -v "^[-+][-+]" | grep -oP '\w+' | sort -u)
for export in $CHANGED_EXPORTS; do
  echo "=== $export ==="
  gh api "search/code" --field "q=$export language:python" --jq '.items[:5] | .[].repository.full_name' 2>/dev/null
  # Note: GitHub code search API is rate-limited (~30 req/min); empty results may indicate rate limiting, not absence of usage
done

# Check if deprecated APIs have migration guides
git diff $(git merge-base HEAD origin/main) HEAD | grep -A2 "deprecated"
```

### 4b: Open Source Software (OSS) checks

```bash
# Check for new dependencies — license compatibility
git diff $(git merge-base HEAD origin/main) HEAD -- pyproject.toml requirements*.txt

# Check for secrets accidentally committed
git diff $(git merge-base HEAD origin/main) HEAD | grep -iE "(password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}"

# Check for API stability: are public APIs being removed without deprecation?
git diff $(git merge-base HEAD origin/main) HEAD -- "src/**/__init__.py"

# Check CHANGELOG was updated
git diff $(git merge-base HEAD origin/main) HEAD -- CHANGELOG.md CHANGES.md
```

## Step 5: Cross-validate critical/blocking findings

Read and follow the cross-validation protocol from `.claude/skills/_shared/cross-validation-protocol.md`.

**Skill-specific**: use the **same agent type** that raised the finding as the verifier (e.g., sw-engineer verifies sw-engineer's critical finding).

## Step 6: Consolidate findings

Spawn a **sw-engineer** consolidator agent with this prompt:

> "Read all finding files in `$RUN_DIR/` (agent files: `sw-engineer.md`, `qa-specialist.md`, `perf-optimizer.md`, `doc-scribe.md`, `linting-expert.md`, `solution-architect.md`, and `codex.md` if present — skip any that are missing). Read `.claude/skills/review/checklist.md` using the Read tool and apply the consolidation rules (signal-to-noise filter, annotation completeness, section caps). Apply the precision gate: only include findings with a concrete, actionable location (function, line range, or variable name). Apply the finding density rule: for modules under 100 lines, aim for ≤10 total findings. Rank findings within each section by impact (blocking > critical > high > medium > low). For `codex.md`: include its unique findings under a `### Codex Co-Review` section; deduplicate against agent findings (same file:line raised by both → keep the agent version, mark as 'also flagged by Codex'). If `issue-*.md` files exist in `$RUN_DIR`, include a `### Issue Root Cause Alignment` section placed immediately after `### [blocking] Critical`. For each linked issue: state the root cause hypothesis, whether the PR addresses it (yes / partially / no), whether the PR description diverges from the issue's stated problem, and whether the reproduction scenario is tested. Any `root cause misalignment` or `scope divergence` finding is at least HIGH severity. Parse each agent's `confidence` from its envelope; assign `codex` a fixed confidence of 0.75 (moderate — static analysis, no runtime context). Write the consolidated report to `_outputs/$(date +%Y)/$(date +%m)/output-review-$(date +%Y-%m-%d).md` using the Write tool. Return ONLY a one-line summary: `verdict=<APPROVE|REQUEST_CHANGES|NEEDS_WORK> | findings=N | critical=N | high=N | file=_outputs/$(date +%Y)/$(date +%m)/output-review-$(date +%Y-%m-%d).md`"

Main context receives only the one-liner verdict. Proceed with that summary for terminal output.

```
## Code Review: [target]

### [blocking] Critical (must fix before merge)
- [bugs, security issues, data corruption risks]
- Severity: CRITICAL / HIGH

### Issue Root Cause Alignment
(omit if no linked issues)
- Issue #N: [title] — [root cause hypothesis from analysis]
- Root cause addressed: [yes / partially / no — explanation]
- PR/issue scope alignment: [aligned / diverged — what differs]
- Reproduction tested: [yes / no — what's missing]

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

### Codex Co-Review
(omit section if Codex was unavailable or found no unique issues)
- [unique findings from codex.md not already captured by agents above]
- Duplicate findings (same location as agent finding): omitted — see agent section

### Recommended Next Steps
1. [most important action]
2. [second most important]
3. [third]

### Review Confidence
<!-- Replace placeholder rows with actual agent scores for this review -->
| Agent | Score | Label | Gaps |
|-------|-------|-------|------|
<!-- | sw-engineer | 0.88 | high | — | -->
<!-- | qa-specialist | 0.65 | ⚠ low | no test execution; coverage unverifiable without running suite | -->
<!-- | perf-optimizer | 0.72 | moderate | no profiling data; estimates from static analysis only | -->
<!-- | codex | 0.75 | moderate | static analysis only; no type inference or runtime context | -->

**Aggregate**: min 0.65 / median 0.N
[⚠ LOW CONFIDENCE: qa-specialist could not verify test execution — treat coverage findings as indicative, not conclusive]
```

After parsing confidence scores: if any agent scored < 0.7, prepend **⚠ LOW CONFIDENCE** to that agent's findings section and explicitly state the gap. Do not silently drop uncertain findings — flag them so the reviewer can decide whether to investigate further.

Before constructing the output path, extract the current branch:
`BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')`

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **PR Summary** template with the **Extended Fields (review only)** addendum. Replace `[entity-line]` with `Review — [target]` and replace `[skill-specific path]` with `_outputs/$(date +%Y)/$(date +%m)/output-review-$BRANCH-$(date +%Y-%m-%d).md`.

## Step 7: Delegate implementation follow-up (optional)

After consolidating findings, identify tasks from the review that Codex can implement directly — not style violations (those are handled by pre-commit hooks), but work that requires writing meaningful code or documentation grounded in the actual implementation.

**Delegate to Codex when you can write an accurate, specific brief:**

- Public functions with no docstrings — read the implementation first, then describe what each one does so Codex can write a real 6-section docstring, not a placeholder
- Missing test coverage for a concrete, well-defined behaviour — describe the exact scenario to test
- A consistent rename identified across multiple files — name both the old and new symbol and why it was flagged

**Do not delegate — these require human judgment:**

- Architectural issues, logic errors, security vulnerabilities, or behavioural changes
- Any task where you cannot write a precise description without guessing

Read `.claude/skills/_shared/codex-delegation.md` and apply the delegation criteria defined there.

Example prompt: `"use the qa-specialist to add a test for StreamReader.read_chunk() in tests/test_reader.py — the method should raise ValueError when called after close(), currently there is no test for this path"`

Only print a `### Codex Delegation` section to the terminal when tasks were actually delegated — omit entirely if nothing was delegated. (do not re-write the output file).

**STOP CHECK — `REPLY_MODE=true`**: your response is **incomplete** until you have executed Step 8 below and written the reply file. Do not add a Confidence block or end your response here.

## Step 8: Draft contributor reply (only when --reply)

If `REPLY_MODE` is not set, skip this step.

Spawn the **oss-shepherd** agent with:

- The review output file path from Step 6
- The PR number and contributor handle (if known from Step 1)
- Prompt: "Read the review report at `<path>`. Produce the standard two-part contributor reply per your `<voice>` block: (1) overall PR comment in GitHub Markdown (full MD: headers, bullets, code blocks, `> blockquotes`, links) — one prose paragraph per blocking/high issue; items also in the inline table get one clause only, not a full paragraph; nit/low items bundled as a single 'Minor:' line; decisive close; (2) inline comments table with columns `| Importance | Confidence | File | Line | Comment |` — Importance and Confidence as the two leftmost columns; ordered high → medium → low, then most confident first within each tier; nit/low items omitted from the table entirely. Use all blocking and high findings. No column-width line-wrapping in prose. Write your full output to `_outputs/$(date +%Y)/$(date +%m)/output-reply-<PR#>-$(date +%Y-%m-%d).md` using the Write tool. Return ONLY a one-line summary: `overall=N_issues blocking=N | inline=N_rows | → _outputs/<YYYY>/<MM>/output-reply-<PR#>-<date>.md`"

Print compact terminal summary:

```
  Overall comment  — N issues (M blocking, K minor)
  Inline comments  — N rows

  Reply:  _outputs/<YYYY>/<MM>/output-reply-<PR#>-<date>.md
```

End your response with a `## Confidence` block per CLAUDE.md output standards. For static analysis of complete, self-contained code (no missing imports needed to reason about the findings), a baseline confidence of 0.88+ is appropriate; reserve scores below 0.80 for cases where runtime behaviour, external dependencies, or execution traces are genuinely needed to validate a finding. This is always the very last thing, whether or not `--reply` was used.

</workflow>

<notes>

- Critical issues are always surfaced regardless of scope
- Skip sections where no issues were found — don't pad with "looks good". When reviewing isolated code without git context (no PR diff, no repo history available), skip OSS Checks and Performance Concerns sections entirely unless the code itself contains evidence of performance issues (e.g., nested loops over large collections, I/O in tight loops) or OSS concerns (e.g., hardcoded secrets, new dependency strings).
- In PR mode: check CI status first — if red, report that without full review
- Blocking issues require explicit `[blocking]` prefix so author knows what must change
- Follow-up chains:
  - `[blocking]` bugs or regressions → `/develop fix` to reproduce with test and apply targeted fix
  - Structural or quality issues → `/develop refactor` for test-first improvements
  - Security findings in auth/input/deps → run `pip-audit` for dependency Common Vulnerabilities and Exposures (CVEs); address Open Web Application Security Project (OWASP) issues inline via `/develop fix`
  - Mechanical issues beyond what Step 6 auto-fixed → `/codex:rescue <task>` to delegate additional tasks
  - Docstrings, type annotations, renames, and other mechanical findings → `/codex:rescue <task description>` per finding to delegate to Codex
  - PR feedback to be shared directly with a contributor → use `--reply` to auto-draft via oss-shepherd; or invoke oss-shepherd manually for custom framing

</notes>
