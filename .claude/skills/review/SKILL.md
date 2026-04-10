---
name: review
description: Multi-agent code review covering architecture, tests, performance, docs, lint, security, and Application Programming Interface (API) design.
argument-hint: '[PR number|file|dir|path/to/report.md] [--reply]'
allowed-tools: Read, Write, Bash, Grep, Agent, TaskCreate, TaskUpdate
context: fork
model: opus
effort: high
---

<objective>

Perform a comprehensive code review by spawning specialized sub-agents in parallel and consolidating their findings into structured feedback with severity levels.

</objective>

<inputs>

- **$ARGUMENTS**: optional file path, directory, or Pull Request (PR) number to review.
  - If a number is given (e.g. `42`): review the PR diff
  - If a path is given: review those files
  - If omitted: review recently changed files
  - `--reply`: after review, spawn oss-shepherd to draft a contributor-facing PR comment from the findings. When the argument is a path ending in `.md`, spawns oss-shepherd directly from that report without running a new review.
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

```bash
# Parse --reply flag — must run before any gh calls
REPLY_MODE=false
CLEAN_ARGS=$ARGUMENTS
if echo "$ARGUMENTS" | grep -q -- '--reply'; then
    REPLY_MODE=true
    CLEAN_ARGS=$(echo "$ARGUMENTS" | sed 's/--reply//g' | xargs)
fi
```

```bash
DIRECT_PATH_MODE=false
if echo "$CLEAN_ARGS" | grep -qE '\.md$'; then
    DIRECT_PATH_MODE=true
    REVIEW_FILE="$CLEAN_ARGS"
fi
```

```bash
# If $CLEAN_ARGS is a PR number — run all four in parallel:
gh pr diff $CLEAN_ARGS --name-only                     # files changed in PR                    # timeout: 6000
gh pr view $CLEAN_ARGS                                 # PR description and metadata             # timeout: 6000
gh pr checks $CLEAN_ARGS                               # CI status — don't review if CI is red  # timeout: 15000
gh pr view $CLEAN_ARGS --json reviews,labels,milestone # timeout: 6000

# If $CLEAN_ARGS is a path: use it directly

# If no argument: find recently changed files
git diff --name-only HEAD~1 HEAD # timeout: 3000
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

Parse the PR body (from `gh pr view $CLEAN_ARGS`) for issue references (`Closes #N`, `Fixes #N`, `Resolves #N`, `refs #N` — case-insensitive). Extract all referenced issue numbers into `ISSUE_NUMS` (list). Cap at 3 issues maximum.

If `ISSUE_NUMS` is non-empty, spawn one **sw-engineer** agent per issue at the start of Step 2 (in parallel with Codex co-review — both are independent of each other). Each issue agent should:

- Fetch issue: `gh issue view <N> --json title,body,comments,state,labels`
- Fetch comments: `gh issue view <N> --comments`
- Produce `/analyse`-style output: Summary, Root Cause Hypotheses table (top 3), Code Evidence for top hypothesis
- Write full analysis to `$RUN_DIR/issue-<N>.md` (file-handoff protocol)
- Return compact JSON envelope only: `{"status":"done","issue":N,"root_cause":"<one-line summary>","file":"$RUN_DIR/issue-<N>.md","confidence":0.N}`

If `ISSUE_NUMS` is empty, skip all issue-related checks in downstream steps.

### Direct report fast-path

If `DIRECT_PATH_MODE=true`:

- `REPLY_MODE=false` → print `Error: --reply is required when passing a .md report path` and stop.
- `REPLY_MODE=true` and `[ ! -f "$REVIEW_FILE" ]` → print `Error: report not found: $REVIEW_FILE` and stop.
- `REPLY_MODE=true` and file exists → print `[direct] using $REVIEW_FILE` → **skip immediately to Step 9**. Do not run Steps 2–8.

## Step 2: Codex co-review

Set up the run directory (shared by Codex and all agent spawns in Step 3):

```bash
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".reports/review/$TIMESTAMP"
mkdir -p "$RUN_DIR" # timeout: 5000
```

Check availability:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex' && echo "codex (openai-codex) available" || echo "⚠ codex (openai-codex) not found — skipping co-review" # timeout: 15000
```

If Codex is available, run a comprehensive review on the diff:

```bash
CODEX_OUT="$RUN_DIR/codex.md"
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review: look for bugs, missed edge cases, incorrect logic, and inconsistencies with existing code patterns. Read-only: do not apply fixes. Write findings to $RUN_DIR/codex.md.")
```

After Codex writes `$RUN_DIR/codex.md`, extract a compact seed list (≤10 items, `[{"loc":"file:line","note":"..."}]`) to inject into agent prompts in Step 3 as pre-flagged issues to verify or dismiss. If Codex was skipped or found nothing, proceed with an empty seed.

## Step 3: Spawn sub-agents in parallel

**File-based handoff**: read `.claude/skills/_shared/file-handoff-protocol.md`. The run directory was created in Step 2 (`$RUN_DIR`).

<!-- Note: $RUN_DIR must be pre-expanded before inserting into spawn prompts — replace with the literal path string computed in Step 2 setup. -->

Replace `$RUN_DIR` in the spawn prompt below with the actual path from Step 2.

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

**Health monitoring** (CLAUDE.md §8): Agent calls are synchronous — Claude awaits each response natively; no Bash checkpoint polling is available. If any agent does not return within `$HARD_CUTOFF` seconds, use the Read tool to surface any partial results already written to `$RUN_DIR` and continue with what was found; mark timed-out agents with ⏱ in the final report. Grant one `$EXTENSION` extension if the output file tail explains the delay. Never silently omit timed-out agents.

## Step 4: Post-agent checks (run in parallel)

While agents from Step 3 are completing, run these two independent checks simultaneously:

```bash
TRUNK=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}') # timeout: 6000  # shared by 4a and 4b
```

### 4a: Ecosystem impact check (for libraries with downstream users)

```bash
# Check if changed APIs are used by downstream projects
# Rate-limit guard: if gh api returns HTTP 429, wait 10 seconds and retry once.
# If still rate-limited, log "rate-limited — downstream search may be incomplete" and continue.
# --paginate is available for large result sets but increases rate-limit exposure; omit unless completeness is critical.
CHANGED_EXPORTS=$(git diff $(git merge-base HEAD origin/${TRUNK:-main}) HEAD -- "src/**/__init__.py" | grep "^[-+]" | grep -v "^[-+][-+]" | grep -oP '\w+' | sort -u) # timeout: 3000
for export in $CHANGED_EXPORTS; do
    echo "=== $export ==="
    gh api "search/code" --field "q=$export language:python" --jq '.items[:5] | .[].repository.full_name' 2>/dev/null # timeout: 30000
    # Note: GitHub code search API is rate-limited (~30 req/min); empty results may indicate rate limiting, not absence of usage
done

# Check if deprecated APIs have migration guides
git diff $(git merge-base HEAD origin/${TRUNK:-main}) HEAD | grep -A2 "deprecated" # timeout: 3000
```

### 4b: Open Source Software (OSS) checks

```bash
# Check for new dependencies — license compatibility
git diff $(git merge-base HEAD origin/${TRUNK:-main}) HEAD -- pyproject.toml requirements*.txt # timeout: 3000

# Check for secrets accidentally committed
git diff $(git merge-base HEAD origin/${TRUNK:-main}) HEAD | grep -iE "(password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}" # timeout: 3000

# Check for API stability: are public APIs being removed without deprecation?
git diff $(git merge-base HEAD origin/${TRUNK:-main}) HEAD -- "src/**/__init__.py" # timeout: 3000

# Check CHANGELOG was updated
git diff $(git merge-base HEAD origin/${TRUNK:-main}) HEAD -- CHANGELOG.md CHANGES.md # timeout: 3000
```

## Step 5: Cross-validate critical/blocking findings

Read and follow the cross-validation protocol from `.claude/skills/_shared/cross-validation-protocol.md`. If `.claude/skills/_shared/cross-validation-protocol.md` is not present, skip Step 5.

**Skill-specific**: use the **same agent type** that raised the finding as the verifier (e.g., sw-engineer verifies sw-engineer's critical finding).

## Step 6: Consolidate findings

Before constructing the output path, extract the current branch and date components: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` `YYYY=$(date +%Y); MM=$(date +%m); DATE=$(date +%Y-%m-%d)`

Spawn a **sw-engineer** consolidator agent with this prompt:

> "Read all finding files in `$RUN_DIR/` (agent files: `sw-engineer.md`, `qa-specialist.md`, `perf-optimizer.md`, `doc-scribe.md`, `linting-expert.md`, `solution-architect.md`, and `codex.md` if present — skip any that are missing). Read `.claude/skills/review/checklist.md` using the Read tool and apply the consolidation rules (signal-to-noise filter, annotation completeness, section caps). Apply the precision gate: only include findings with a concrete, actionable location (function, line range, or variable name). Apply the finding density rule: for modules under 100 lines, aim for ≤10 total findings. Rank findings within each section by impact (blocking > critical > high > medium > low). For `codex.md`: include its unique findings under a `### Codex Co-Review` section; deduplicate against agent findings (same file:line raised by both → keep the agent version, mark as 'also flagged by Codex'). If `issue-*.md` files exist in `$RUN_DIR`, include a `### Issue Root Cause Alignment` section placed immediately after `### [blocking] Critical`. For each linked issue: state the root cause hypothesis, whether the PR addresses it (yes / partially / no), whether the PR description diverges from the issue's stated problem, and whether the reproduction scenario is tested. Any `root cause misalignment` or `scope divergence` finding is at least HIGH severity. Parse each agent's `confidence` from its envelope; assign `codex` a fixed confidence of 0.75 (moderate — static analysis, no runtime context). Write the consolidated report to `.temp/output-review-$BRANCH-$DATE.md` using the Write tool. Return ONLY a one-line summary: `verdict=<APPROVE|REQUEST_CHANGES|NEEDS_WORK> | findings=N | critical=N | high=N | file=.temp/output-review-$BRANCH-$DATE.md`"

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

<!-- Extended Fields live in .claude/skills/_shared/terminal-summaries.md — if that file is absent, omit the extended fields block -->

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **PR Summary** template with the **Extended Fields (review only)** addendum. Replace `[entity-line]` with `Review — [target]` and replace `[skill-specific path]` with `.temp/output-review-$BRANCH-$DATE.md`. The rendered terminal block must follow this exact structure: opening `---` on its own line, followed by the entity line on the next line (never concatenated as `---Review...`); the `→ saved to .temp/output-review-$BRANCH-$DATE.md` line must be present after `Confidence:`; closing `---` must follow the `→ saved to` line. Print this block to the terminal.

After printing to the terminal, also prepend the same compact block to the top of the report file using the Edit tool — insert it at line 1 so the file begins with the compact summary followed by a blank line, then the existing `## Code Review: [target]` content.

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

Example prompt: `"Add a test for StreamReader.read_chunk() in tests/test_reader.py — the method should raise ValueError when called after close(), currently no test covers this path."`

Only print a `### Codex Delegation` section to the terminal when tasks were actually delegated — omit entirely if nothing was delegated. (do not re-write the output file).

## Step 8: Reply gate — STOP CHECK

**Run this step before the Confidence block regardless of `--reply` mode.**

If `REPLY_MODE=true`: your response is **incomplete** until you have executed Step 9 below and written the reply file. Do not add a Confidence block or end your response here — proceed to Step 9 immediately.

If `REPLY_MODE=false`: skip Step 9 and end with the Confidence block now.

## Step 9: Draft contributor reply (only when --reply)

If `REPLY_MODE` is not set, skip this step.

Spawn the **oss-shepherd** agent with:

- The review output file path from Step 6
- The PR number and contributor handle (if known from Step 1)
- Prompt: "Read the review report at `<path>`. Write a two-part contributor reply: **Part 1 — Reply summary** (always present, always complete on its own): (a) acknowledgement + praise naming what is genuinely good — technique, structural decisions, test quality — 1–3 concrete observations, not generic; (b) thematic areas needing improvement — no counts, no itemisation, no 'see below'; name the concern areas concretely enough that the contributor knows what to look at without Part 2; (c) optional closing sentence only when Part 2 follows (e.g. 'I've left inline suggestions with specifics.'). **Part 2 — Inline suggestions** (optional; single unified table, all findings in one place — no separate prose paragraphs): `| Importance | Confidence | File | Line | Comment |` — Importance and Confidence as the two leftmost columns; high → medium → low, then most confident first within tier; 1–2 sentences per row for high items; include all high/medium/low findings in one table. No column-width line-wrapping in prose. Write your full output to `.temp/output-reply-<PR#>-$(date +%Y-%m-%d).md` using the Write tool. Return ONLY a one-line summary: `part1=done | part2=N_rows | → .temp/output-reply-<PR#>-<date>.md`"

Print compact terminal summary:

```
  Part 1  — reply summary (complete standalone)
  Part 2  — N inline suggestions

  Reply:  .temp/output-reply-<PR#>-<date>.md
```

End your response with a `## Confidence` block per CLAUDE.md output standards. For static analysis of complete, self-contained code (no missing imports needed to reason about the findings), a baseline confidence of 0.88+ is appropriate; reserve scores below 0.80 for cases where runtime behaviour, external dependencies, or execution traces are genuinely needed to validate a finding. This is always the very last thing, whether or not `--reply` was used.

</workflow>

<notes>

- Critical issues are always surfaced regardless of scope
- Skip sections where no issues were found — don't pad with "looks good". When reviewing isolated code without git context (no PR diff, no repo history available), skip OSS Checks and Performance Concerns sections entirely unless the code itself contains evidence of performance issues (e.g., nested loops over large collections, I/O in tight loops) or OSS concerns (e.g., hardcoded secrets, new dependency strings).
- **Signal-to-noise gate**: When a function or class has ≤50 lines and only 1–2 ground-level issues (critical/high), do not add more than 2 medium/low findings beyond them. Surface the remainder as `[nit]` in a dedicated "Minor Observations" section rather than elevating them to the same tier as high-severity findings. The goal is that the first 3 findings a reader sees are always the most impactful.
- In PR mode: check CI status first — if red, report that without full review
- Blocking issues require explicit `[blocking]` prefix so author knows what must change
- Follow-up chains:
  - `[blocking]` bugs or regressions → `/develop fix` to reproduce with test and apply targeted fix
  - Structural or quality issues → `/develop refactor` for test-first improvements
  - Security findings in auth/input/deps → run `pip-audit` for dependency Common Vulnerabilities and Exposures (CVEs); address Open Web Application Security Project (OWASP) issues inline via `/develop fix`
  - Mechanical issues beyond what Step 6 auto-fixed → `/codex:codex-rescue <task>` to delegate additional tasks
  - Docstrings, type annotations, renames, and other mechanical findings → `/codex:codex-rescue <task description>` per finding to delegate to Codex
  - PR feedback to be shared directly with a contributor → use `--reply` to auto-draft via oss-shepherd; or invoke oss-shepherd manually for custom framing

</notes>
