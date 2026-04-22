---
name: review
description: Multi-agent code review of GitHub Pull Requests covering architecture, tests, performance, docs, lint, security, and API design.
argument-hint: '[PR number|path/to/report.md] [--reply]'
allowed-tools: Read, Write, Edit, Bash, Grep, Agent, TaskCreate, TaskUpdate
context: fork
model: opus
effort: high
---

<objective>

Spawn specialized sub-agents in parallel. Consolidate findings into structured feedback with severity levels.

</objective>

<inputs>

- **$ARGUMENTS**: PR number or report path.
  - If a number given (e.g. `42`): review PR diff
  - `--reply`: spawn oss:shepherd to draft contributor-facing PR comment. Path ending in `.md` → spawn oss:shepherd from that report, skip new review.
  - **Scope**: Python source only. Non-Python file → state out of scope, suggest tool, no findings.
  - **Local files**: use `/develop:review` for local files or current git diff.

</inputs>

<constants>
<!-- Background agent health monitoring (CLAUDE.md §8) — applies to Step 3 parallel agent spawns -->
MONITOR_INTERVAL=300   # 5 minutes between polls
HARD_CUTOFF=900        # 15 minutes of no file activity → declare timed out
EXTENSION=300          # one +5 min extension if output file explains delay
</constants>

<workflow>

<!-- Agent Resolution: canonical table at plugins/oss/skills/_shared/agent-resolution.md -->

## Agent Resolution

```bash
# Locate oss plugin shared dir — installed first, local workspace fallback
_OSS_SHARED=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/_shared 2>/dev/null | head -1)
[ -z "_OSS_SHARED" ] && _OSS_SHARED="plugins/oss/skills/_shared"
```

Read `$_OSS_SHARED/agent-resolution.md`. Contains: foundry check + fallback table. If foundry not installed: use table to substitute each `foundry:X` with `general-purpose`. Agents this skill uses: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:perf-optimizer`, `foundry:doc-scribe`, `foundry:linting-expert`, `foundry:solution-architect`.

**Task hygiene**: Before creating tasks, call `TaskList`. Each found task:

- `completed` if work done
- `deleted` if orphaned / irrelevant
- `in_progress` only if genuinely continuing

**Task tracking**: Per CLAUDE.md, TaskCreate for each major phase. Mark in_progress/completed throughout. Loop retry or scope change → new task.

## Step 1: Identify scope and context (run in parallel for PR mode)

```bash
# Parse --reply flag — must run before any gh calls
REPLY_MODE=false
CLEAN_ARGS=$ARGUMENTS
if [[ "$ARGUMENTS" == *"--reply"* ]]; then
    REPLY_MODE=true
    CLEAN_ARGS="${ARGUMENTS//--reply/}"
    CLEAN_ARGS="${CLEAN_ARGS#"${CLEAN_ARGS%%[![:space:]]*}"}"
fi
```

```bash
DIRECT_PATH_MODE=false
if [[ "$CLEAN_ARGS" == *.md ]]; then
    DIRECT_PATH_MODE=true
    REVIEW_FILE="$CLEAN_ARGS"
fi
```

```bash
# $CLEAN_ARGS must be a PR number — run all four in parallel:
gh pr diff $CLEAN_ARGS --name-only                     # files changed in PR                    # timeout: 6000
gh pr view $CLEAN_ARGS                                 # PR description and metadata             # timeout: 6000
gh pr checks $CLEAN_ARGS                               # CI status — don't review if CI is red  # timeout: 15000
gh pr view $CLEAN_ARGS --json reviews,labels,milestone # timeout: 6000
```

CI red → report without full review.

### Scope pre-check

Before spawning agents, classify diff:

- Count files changed, lines added/removed, new classes/modules
- Classify as: **FIX** (\<3 files, \<50 lines), **REFACTOR** (no new public API), **FEATURE** (new public API or module), or **MIXED**
- **Complexity smell**: if 8+ files changed, note in report header

Use classification to skip optional agents:

- FIX scope → skip Agent 3 (perf-optimizer) and Agent 6 (solution-architect)
- REFACTOR scope → skip Agent 6 (solution-architect)
- FEATURE/MIXED → spawn all agents

### Structural context (codemap, if installed)

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    # Use file list from the gh pr diff already fetched above
    CHANGED_MODS=$(gh pr diff $CLEAN_ARGS --name-only 2>/dev/null | grep '\.py$' | sed 's|^src/||;s|\.py$||;s|/|.|g' | grep -v '__init__$')  # timeout: 6000
    scan-query central --top 5 2>/dev/null  # timeout: 5000
    for mod in $CHANGED_MODS; do scan-query rdeps "$mod" 2>/dev/null; done  # timeout: 5000
fi
```

Codemap returns results: prepend `## Structural Context (codemap)` block to **Agent 1 (foundry:sw-engineer)** spawn prompt. Include:

- Each changed module's `rdep_count` — label as **high risk** (>20), **moderate** (5–20), or **low** (\<5)
- `central --top 5` for project-wide blast-radius reference

Agent 1 uses this to prioritize: high `rdep_count` modules warrant deeper scrutiny on API compat, error handling, correctness — downstream callers outside diff not otherwise visible. Codemap absent → skip silently.

### Linked issue analysis (PR mode only)

Parse PR body (`gh pr view $CLEAN_ARGS`) for issue refs (`Closes #N`, `Fixes #N`, `Resolves #N`, `refs #N` — case-insensitive). Extract to `ISSUE_NUMS`. Cap 3.

`ISSUE_NUMS` non-empty: spawn one **foundry:sw-engineer** per issue **at Step 2, after `$RUN_DIR` is initialized** (parallel with Codex co-review). Each issue agent:

- Fetch issue: `gh issue view <N> --json title,body,comments,state,labels`
- Fetch comments: `gh issue view <N> --comments`
- Produce `/oss:analyse`-style output: Summary, Root Cause Hypotheses table (top 3), Code Evidence for top hypothesis
- Write full analysis to `$RUN_DIR/issue-<N>.md` (file-handoff protocol)
- Return compact JSON envelope only: `{"status":"done","issue":N,"root_cause":"<one-line summary>","file":"$RUN_DIR/issue-<N>.md","confidence":0.N}`

`ISSUE_NUMS` empty → skip issue checks downstream.

### Direct report fast-path

If `DIRECT_PATH_MODE=true`:

- `REPLY_MODE=false` → print `Error: --reply is required when passing a .md report path` and stop.
- `REPLY_MODE=true` and `[ ! -f "$REVIEW_FILE" ]` → print `Error: report not found: $REVIEW_FILE` and stop.
- `REPLY_MODE=true` and file exists → print `[direct] using $REVIEW_FILE` → **skip to Step 9**. Skip Steps 2–8.

## Step 2: Codex co-review

Set up run directory (shared by Codex and Step 3 agents):

```bash
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".reports/review/$TIMESTAMP"
mkdir -p "$RUN_DIR" # timeout: 5000
```

Check availability:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex' && echo "codex (openai-codex) available" || echo "⚠ codex (openai-codex) not found — skipping co-review" # timeout: 15000
```

Codex available → run review on diff:

```bash
CODEX_OUT="$RUN_DIR/codex.md"
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review: look for bugs, missed edge cases, incorrect logic, and inconsistencies with existing code patterns. Read-only: do not apply fixes. Write findings to $RUN_DIR/codex.md.")
```

After Codex writes `$RUN_DIR/codex.md`, extract seed list (≤10 items, `[{"loc":"file:line","note":"..."}]`) to inject into Step 3 agent prompts as pre-flagged issues. Codex skipped or empty → proceed with empty seed.

## Step 3: Spawn sub-agents in parallel

**File-based handoff**: read `.claude/skills/_shared/file-handoff-protocol.md`. File absent → warn the user: "file-handoff protocol not found — verify foundry plugin installed (`claude plugin list`); continuing without it." Then continue without it. Run dir from Step 2 (`$RUN_DIR`).

<!-- Note: $RUN_DIR must be pre-expanded before inserting into spawn prompts — replace with the literal path string computed in Step 2 setup. -->

Replace `$RUN_DIR` below with actual path from Step 2.

Launch agents simultaneously. Security augmentation folded into Agent 1. Agent 6 optional. Every agent prompt must end with:

> "Write your FULL findings (all sections, Confidence block) to `$RUN_DIR/<agent-slug>.md` using the Write tool — where `<agent-slug>` uses hyphen separator (no colon), e.g. `foundry--sw-engineer.md`, `foundry--qa-specialist.md`, `foundry--perf-optimizer.md`, `foundry--doc-scribe.md`, `foundry--linting-expert.md`, `foundry--solution-architect.md`. Colons are invalid in macOS filenames. Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":1,\"medium\":2},\"file\":\"$RUN_DIR/<agent-slug>.md\",\"confidence\":0.88}`"

**Agent 1 — foundry:sw-engineer**: Review architecture, SOLID, type safety, error handling, code structure. Check Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking vs suggestions.

**Error path analysis** (new/changed code): For each error-handling path introduced or modified, produce table:

| Location | Exception/Error | Caught? | Action if caught | User-visible? |
| --- | --- | --- | --- | --- |

Flag rules:

- Caught=No + User-visible=Silent → **HIGH** (unhandled error path)
- Caught=Yes + Action=`pass` or bare `except` → **MEDIUM** (swallowed error)
- Cap 15 rows. New/changed paths only.

Read `plugins/oss/skills/review/checklist.md` — apply CRITICAL/HIGH patterns as severity anchors. Respect suppressions.

`ISSUE_NUMS` non-empty: read `$RUN_DIR/issue-*.md`. Evaluate whether changes address root cause, not just symptom. PR addresses symptom only → `[blocking] HIGH — root cause misalignment`. PR description diverges from issue problem → `HIGH — PR/issue scope divergence`.

**Agent 2 — foundry:qa-specialist**: Audit test coverage. Find untested paths, missing edge cases, test quality issues. Check ML-specific issues (non-deterministic tests, missing seed pinning). List top 5 missing tests. Also check explicitly (GT-level findings, not afterthoughts):

- Concurrent access to shared state (when locks or shared variables are present)
- Error paths: calling methods in wrong order (e.g., `log()` before `start()`)
- Resource cleanup on exception (file handles, database connections)
- Boundary conditions for division, empty collections, and zero-count inputs
- Type-coercion boundary inputs: `int()`, `float()`, `datetime` parsers — test near-valid inputs (float strings for int parsers, empty strings, very large values, None)

**Consolidation rule**: One finding per test gap with concise scenario list, not separate findings. Format: "Missing tests for `parse_numeric()`: empty string, None, very large integers, float-string for int parser." Keeps section actionable, ≤5 items.

`ISSUE_NUMS` non-empty: read `$RUN_DIR/issue-*.md`. Check tests cover linked issue reproduction scenario. Issue has minimal repro/trace not covered by tests → `HIGH — issue reproduction not tested`.

**Agent 3 — foundry:perf-optimizer**: Find perf issues. Algorithmic complexity, Python loops that should be NumPy/torch ops, repeated computation, unnecessary I/O. ML code: DataLoader config, mixed precision. Prioritize by impact.

**Agent 4 — foundry:doc-scribe**: Check doc completeness. Public APIs without docstrings, missing Google style sections, outdated README, CHANGELOG gaps. Verify examples run.

- **Algorithmic accuracy check**: Functions computing math results — verify docstring claims match implementation. Output shape/length match? Standard name (e.g. "moving average") match behavior (expanding vs sliding window)? Deviates from convention → MEDIUM (docstring must document deviation). **Deprecation check**: Check stdlib deprecated in Python 3.10+ (e.g., `datetime.utcnow()` deprecated in 3.12, `os.path` vs `pathlib`). Flag deprecated usage as MEDIUM with replacement.

**Agent 5 — foundry:linting-expert**: Static analysis. Check ruff/mypy pass. Type annotation gaps on public APIs, suppressed violations without explanation, missing pre-commit hooks. Flag mismatched Python version.

**Security augmentation (conditional — fold into Agent 1, not separate spawn)**: Diff touches auth, user input, deps, or serialization → add to Agent 1 prompt: check SQL injection, XSS, insecure deserialization, hardcoded secrets, missing input validation. Run `pip-audit` if dep files changed. Skip if purely internal refactoring.

**Agent 6 — foundry:solution-architect (optional, PRs touching public API boundaries)**: Diff touches `__init__.py` exports, adds/modifies Protocols/ABCs, changes module structure, or new public classes → evaluate API design, coupling, backward compat. Skip if internal only.

**Health monitoring** (CLAUDE.md §8): Agents synchronous — Claude awaits natively; no Bash checkpoint polling. Agent doesn't return within `$HARD_CUTOFF`s → Read partial results from `$RUN_DIR`, continue; mark ⏱ in report. One `$EXTENSION` if output file explains delay. Never omit timed-out agents.

## Step 4: Post-agent checks (run in parallel)

Run these two checks simultaneously (while Step 3 agents complete):

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

### 4b: OSS checks

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

Read and follow `.claude/skills/_shared/cross-validation-protocol.md`. File absent → warn the user: "cross-validation protocol not found — verify foundry plugin installed (`claude plugin list`); skipping Step 5." Then skip Step 5.

**Skill-specific**: same agent type that raised finding = verifier (e.g., foundry:sw-engineer verifies foundry:sw-engineer critical finding).

## Step 6: Consolidate findings

Before output path, extract: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` `YYYY=$(date +%Y); MM=$(date +%m); DATE=$(date +%Y-%m-%d)`

Spawn a **foundry:sw-engineer** consolidator agent with this prompt:

> **Task:** Read all finding files in `$RUN_DIR/` (agent files: `sw-engineer.md`, `qa-specialist.md`, `perf-optimizer.md`, `doc-scribe.md`, `linting-expert.md`, `solution-architect.md`, and `codex.md` if present — skip any that are missing). Read `plugins/oss/skills/review/checklist.md` using the Read tool and apply the consolidation rules (signal-to-noise filter, annotation completeness, section caps).
>
> **Filtering rules:**
> - Precision gate: only include findings with a concrete, actionable location (function, line range, or variable name).
> - Finding density: for modules under 100 lines, aim for ≤10 total findings.
> - Ranking: within each section, order by impact (blocking > critical > high > medium > low).
> - Codex deduplication: include `codex.md` unique findings under `### Codex Co-Review`; same file:line raised by both agent and Codex → keep agent version, mark as 'also flagged by Codex'.
>
> **Issue alignment (when `issue-*.md` files exist in `$RUN_DIR`):** Include a `### Issue Root Cause Alignment` section placed immediately after `### [blocking] Critical`. For each linked issue: state the root cause hypothesis, whether the PR addresses it (yes / partially / no), whether the PR description diverges from the issue's stated problem, and whether the reproduction scenario is tested. Any `root cause misalignment` or `scope divergence` finding is at least HIGH severity.
>
> **Confidence parsing:** Parse each agent's `confidence` from its JSON envelope. Assign `codex` a fixed confidence of 0.75 (moderate — static analysis, no runtime context).
>
> **Write to:** `.temp/output-review-$BRANCH-$DATE.md` using the Write tool.
>
> **Return ONLY** a one-liner summary: `verdict=<APPROVE|REQUEST_CHANGES|NEEDS_WORK> | findings=N | critical=N | high=N | file=.temp/output-review-$BRANCH-$DATE.md`

Main context receives only the one-liner verdict. Proceed with that summary for terminal output.

```markdown
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
| Agent | Score | Label | Gaps |
|-------|-------|-------|------|

**Aggregate**: min 0.65 / median 0.N
[⚠ LOW CONFIDENCE: qa-specialist could not verify test execution — treat coverage findings as indicative, not conclusive]
```

After parsing confidence: agent < 0.7 → prepend **⚠ LOW CONFIDENCE** to findings section, state gap explicitly. Never drop uncertain findings.

<!-- Extended Fields live in .claude/skills/_shared/terminal-summaries.md -->

Read `.claude/skills/_shared/terminal-summaries.md`. File absent → warn the user: "foundry:init required: `.claude/skills/_shared/terminal-summaries.md` not found; install foundry plugin and run `foundry:init` to activate terminal summary templates. Printing plain terminal output instead." Then print a plain summary without the template. When file is present — use **PR Summary** template with **Extended Fields (review only)**. Replace `[entity-line]` with `Review — [target]`, `[skill-specific path]` with `.temp/output-review-$BRANCH-$DATE.md`. Terminal block structure: opening `---` on own line, entity line next (never `---Review...`), `→ saved to .temp/output-review-$BRANCH-$DATE.md` after `Confidence:`, closing `---` after `→ saved to`. Print to terminal.

After printing, also prepend compact block to report file top via Edit — line 1, followed by blank line, then `## Code Review: [target]`.

## Step 7: Delegate implementation follow-up (optional)

After consolidating, identify tasks Codex can implement — not style violations (pre-commit handles those), but meaningful code/doc work grounded in actual implementation.

**Delegate to Codex when you can write an accurate, specific brief:**

- Public functions with no docstrings — read implementation first, describe what each does so Codex writes real 6-section docstring
- Missing test coverage for concrete, well-defined behavior — describe exact scenario
- Consistent rename across files — name old/new symbol and reason

**Do not delegate — these require human judgment:**

- Architectural issues, logic errors, security vulnerabilities, or behavioural changes
- Any task where you cannot write a precise description without guessing

Read `.claude/skills/_shared/codex-delegation.md`. File absent → warn the user: "codex-delegation criteria not found — verify foundry plugin installed (`claude plugin list`); skipping Step 7 delegation." Then skip Step 7.

Example prompt: `"Add a test for StreamReader.read_chunk() in tests/test_reader.py — the method should raise ValueError when called after close(), currently no test covers this path."`

Print `### Codex Delegation` only when tasks delegated — omit if nothing delegated. Don't rewrite output file.

## Step 8: Reply gate — STOP CHECK

**Run this step before the Confidence block regardless of `--reply` mode.**

`REPLY_MODE=true`: response incomplete until Step 9 done and reply file written. No Confidence block — proceed to Step 9.

`REPLY_MODE=false`: skip Step 9, end with Confidence block.

## Step 9: Draft contributor reply (only when --reply)

`REPLY_MODE` not set → skip.

Pre-compute the reply date: `SPAWN_DATE="$(date -u +%Y-%m-%d)"`

Call `Agent(subagent_type="oss:shepherd", prompt=...)` with:

- Review output file path from Step 6
- PR number and contributor handle (if known from Step 1)
- Prompt: "Read the review report at `<path>`. Write a two-part contributor reply: **Part 1 — Reply summary** (always present, always complete on its own): (a) acknowledgement + praise naming what is genuinely good — technique, structural decisions, test quality — 1–3 concrete observations, not generic; (b) thematic areas needing improvement — no counts, no itemisation, no 'see below'; name the concern areas concretely enough that the contributor knows what to look at without Part 2; (c) optional closing sentence only when Part 2 follows (e.g. 'I've left inline suggestions with specifics.'). **Part 2 — Inline suggestions** (optional; single unified table, all findings in one place — no separate prose paragraphs): `| Importance | Confidence | File | Line | Comment |` — Importance and Confidence as the two leftmost columns; high → medium → low, then most confident first within tier; 1–2 sentences per row for high items; include all high/medium/low findings in one table. No column-width line-wrapping in prose. Write your full output to `.temp/output-reply-<PR#>-$SPAWN_DATE.md` using the Write tool. Return ONLY a one-line summary: `part1=done | part2=N_rows | → .temp/output-reply-<PR#>-<date>.md`"

Print compact terminal summary:

```text
  Part 1  — reply summary (complete standalone)
  Part 2  — N inline suggestions

  Reply:  .temp/output-reply-<PR#>-<date>.md
```

End with `## Confidence` block per CLAUDE.md. Static analysis of complete, self-contained code → baseline 0.88+; below 0.80 only when runtime behavior, external deps, or execution traces needed. Always last thing, regardless of `--reply`.

</workflow>

<notes>

- Critical issues always surfaced regardless of scope
- Skip sections with no issues — no padding. Isolated code without git context → skip OSS Checks and Performance Concerns unless code has evidence of perf issues (nested loops, I/O in tight loops) or OSS concerns (hardcoded secrets, new deps).
- **Signal-to-noise gate**: Function/class ≤50 lines with 1–2 critical/high issues → max 2 additional medium/low findings. Rest as `[nit]` in "Minor Observations". First 3 findings reader sees = most impactful.
- PR mode: check CI first — red → report without full review
- Blocking issues need explicit `[blocking]` prefix
- Follow-up chains:
  - `[blocking]` bugs or regressions → `/develop:fix` to reproduce with test and apply targeted fix
  - Structural or quality issues → `/develop:refactor` for test-first improvements
  - Security findings in auth/input/deps → run `pip-audit` for dep CVEs; address OWASP issues via `/develop:fix`
  - Mechanical issues beyond Step 6 → `/codex:codex-rescue <task>`
  - Docstrings, type annotations, renames → `/codex:codex-rescue <task description>` per finding
  - PR feedback for contributor → `--reply` to auto-draft via oss:shepherd, or invoke oss:shepherd manually for custom framing

</notes>
