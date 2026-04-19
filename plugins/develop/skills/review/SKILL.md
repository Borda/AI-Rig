---
name: review
description: Multi-agent code review of local files, directories, or the current git diff covering architecture, tests, performance, docs, lint, security, and API design.
argument-hint: '[file|dir]'
allowed-tools: Read, Write, Edit, Bash, Grep, Agent, TaskCreate, TaskUpdate
context: fork
model: opus
effort: high
---

<objective>

Comprehensive code review of local files or working-tree diff. Spawn specialized sub-agents in parallel, consolidate findings into structured feedback with severity levels.

</objective>

<inputs>

- **$ARGUMENTS**: optional file path or directory to review.
  - Path given: review those files
  - Omitted: review current git diff (`git diff HEAD` — staged + unstaged vs HEAD)
  - **Scope**: reviews Python source only. Non-Python file (YAML, JSON, shell script, etc.) → state out of scope, suggest appropriate tool. No findings.

</inputs>

<constants>
<!-- Background agent health monitoring (CLAUDE.md §8) — applies to Step 3 parallel agent spawns -->
MONITOR_INTERVAL=300   # 5 minutes between polls
HARD_CUTOFF=900        # 15 minutes of no file activity → declare timed out
EXTENSION=300          # one +5 min extension if output file explains delay
</constants>

<workflow>

<!-- Structural pattern shared with oss:review — coordinate changes between develop:review and oss:review when modifying agent spawn logic, file-handoff protocol, or consolidation steps -->

<!-- Agent Resolution: identical across all develop skills -->

## Agent Resolution

> **Foundry plugin check**: run `ls ~/.claude/plugins/cache/ 2>/dev/null | grep -q foundry` (exit 0 = installed). Uncertain → proceed as if foundry available — common case; fall back only if agent dispatch explicitly fails.

Foundry **not** installed → substitute `foundry:X` with `general-purpose`, prepend role description + `model: <model>` to spawn call:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:sw-engineer` | `general-purpose` | `opus` | `You are a senior Python software engineer. Write production-quality, type-safe code following SOLID principles.` |
| `foundry:qa-specialist` | `general-purpose` | `opus` | `You are a QA specialist. Write deterministic, parametrized pytest tests covering edge cases and regressions.` |
| `foundry:perf-optimizer` | `general-purpose` | `opus` | `You are a performance engineer. Profile before changing. Focus on CPU/GPU/memory/IO bottlenecks in Python/ML workloads.` |
| `foundry:doc-scribe` | `general-purpose` | `sonnet` | `You are a documentation specialist. Write Google-style docstrings and keep README content accurate and concise.` |
| `foundry:linting-expert` | `general-purpose` | `haiku` | `You are a static analysis specialist. Fix ruff/mypy violations, add missing type annotations, configure pre-commit hooks.` |
| `foundry:solution-architect` | `general-purpose` | `opus` | `You are a system design specialist. Produce ADRs, interface specs, and API contracts — read code, produce specs only.` |

Skills with `--team` mode: team spawning with fallback agents works but produces lower-quality output.

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- `completed` if work clearly done
- `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

**Task tracking**: per CLAUDE.md, TaskCreate for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create new task.

## Step 1: Identify scope

```bash
if [ -n "$ARGUMENTS" ]; then
    # Path given directly — collect Python files under it
    TARGET="$ARGUMENTS"
    echo "Reviewing: $TARGET"
else
    # No argument — review current working-tree diff vs HEAD
    git diff HEAD --name-only  # timeout: 3000
fi
```

Filter to Python files only. No Python files found → report "no Python files to review" and stop.

### Scope pre-check

Before spawning agents, classify diff:

- Count files changed, lines added/removed, new classes/modules introduced
- Classify: **FIX** (\<3 files, \<50 lines), **REFACTOR** (no new public API), **FEATURE** (new public API or module), **MIXED**
- **Complexity smell**: 8+ files changed → note in report header

Use classification to skip optional agents:

- FIX → skip Agent 3 (perf-optimizer) and Agent 6 (solution-architect)
- REFACTOR → skip Agent 6 (solution-architect)
- FEATURE/MIXED → spawn all agents

### Structural context (codemap, if installed)

```bash
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    CHANGED_MODS=$(git diff HEAD --name-only | grep '\.py$' | sed 's|^src/||;s|\.py$||;s|/|.|g' | grep -v '__init__$')  # timeout: 3000
    scan-query central --top 5 2>/dev/null  # timeout: 5000
    for mod in $CHANGED_MODS; do scan-query rdeps "$mod" 2>/dev/null; done  # timeout: 5000
fi
```

Codemap returns results → prepend `## Structural Context (codemap)` block to **Agent 1 (foundry:sw-engineer)** spawn prompt. Include:

- Each changed module's `rdep_count` — label **high risk** (>20), **moderate** (5–20), **low** (\<5)
- `central --top 5` for project-wide blast-radius reference

Agent 1 uses this to prioritize: high `rdep_count` modules warrant deeper scrutiny on API compatibility, error handling, behavioural correctness — downstream callers outside diff not otherwise visible. Codemap absent or index missing → skip silently.

## Step 2: Codex co-review

Set up run directory:

```bash
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".reports/review/$TIMESTAMP"
mkdir -p "$RUN_DIR"  # timeout: 5000
```

Check availability:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex' && echo "codex (openai-codex) available" || echo "⚠ codex (openai-codex) not found — skipping co-review"  # timeout: 15000
```

If Codex available:

```bash
CODEX_OUT="$RUN_DIR/codex.md"
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review of $TARGET: look for bugs, missed edge cases, incorrect logic, and inconsistencies with existing code patterns. Read-only: do not apply fixes. Write findings to $RUN_DIR/codex.md.")
```

After Codex writes `$RUN_DIR/codex.md`, extract compact seed list (≤10 items, `[{"loc":"file:line","note":"..."}]`) to inject into agent prompts in Step 3 as pre-flagged issues to verify or dismiss. Codex skipped or found nothing → proceed with empty seed.

## Step 3: Spawn sub-agents in parallel

**File-based handoff**: read `.claude/skills/_shared/file-handoff-protocol.md`. Run directory created in Step 2 (`$RUN_DIR`).

<!-- Note: $RUN_DIR must be pre-expanded before inserting into spawn prompts — replace with the literal path string computed in Step 2 setup. -->

Replace `$RUN_DIR` in spawn prompt below with actual path from Step 2.

Resolve oss:review checklist path (version-agnostic):

```bash
OSS_ROOT=$(jq -r 'to_entries[] | select(.key | test("oss@")) | .value.installPath' ~/.claude/plugins/installed_plugins.json 2>/dev/null | head -1)  # timeout: 5000
REVIEW_CHECKLIST="${OSS_ROOT}/skills/review/checklist.md"
if [ ! -f "$REVIEW_CHECKLIST" ]; then
    echo "⚠ oss:review checklist not found at $REVIEW_CHECKLIST — Agent 1 will skip checklist patterns; continuing without it"  # timeout: 5000
    REVIEW_CHECKLIST=""
else
    echo "Checklist: $REVIEW_CHECKLIST"
fi
```

Replace `$REVIEW_CHECKLIST` in Agent 1 and consolidator spawn prompts below with resolved path. **If empty, omit the checklist instruction from those prompts entirely** — do not pass an empty path.

<!-- Note: $REVIEW_CHECKLIST must be pre-expanded before inserting into spawn prompts — replace with the literal path string from the bash block above, same as $RUN_DIR. -->

Launch agents simultaneously with Agent tool (security augmentation folded into Agent 1 — not separate spawn; Agent 6 optional). Every agent prompt must end with:

> "Write your FULL findings (all sections, Confidence block) to `$RUN_DIR/<agent-name>.md` using the Write tool — where `<agent-name>` is e.g. `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:perf-optimizer`, `foundry:doc-scribe`, `foundry:linting-expert`, `foundry:solution-architect`. Then return to the caller ONLY a compact JSON envelope on your final line — nothing else after it: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":1,\"medium\":2},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.88}`"

**Agent 1 — foundry:sw-engineer**: Review architecture, SOLID adherence, type safety, error handling, code structure. Check Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking issues vs suggestions.

**Error path analysis** (new/changed code in diff): For each error-handling path introduced or modified, produce table:

| Location | Exception/Error | Caught? | Action if caught | User-visible? |
| --- | --- | --- | --- | --- |

Flag rules:

- Caught=No + User-visible=Silent → **HIGH** (unhandled error path)
- Caught=Yes + Action=`pass` or bare `except` → **MEDIUM** (swallowed error)
- Cap at 15 rows. New/changed paths only, not entire codebase.

Read review checklist (Read tool → `$REVIEW_CHECKLIST`) — apply CRITICAL/HIGH patterns as severity anchors. Respect suppressions list.

**Agent 2 — foundry:qa-specialist**: Audit test coverage. Identify untested paths, missing edge cases, test quality issues. Check ML-specific issues (non-deterministic tests, missing seed pinning). List top 5 missing tests. Check explicitly for missing tests in these patterns (GT-level findings, not afterthoughts):

- Concurrent access to shared state (locks or shared variables present)
- Error paths: calling methods in wrong order (e.g., `log()` before `start()`)
- Resource cleanup on exception (file handles, database connections)
- Boundary conditions for division, empty collections, zero-count inputs
- Type-coercion boundary inputs: functions parsing/converting strings to typed values (`int()`, `float()`, `datetime`) — test near-valid inputs (float strings for int parsers, empty strings, very large values, `None`) — common omissions.

**Consolidation rule**: Each test gap = one finding with concise list of test scenarios, not separate findings per scenario. Format: "Missing tests for `parse_numeric()`: empty string, None, very large integers, float-string for int parser." Keeps test coverage section actionable, prevents exceeding 5 items.

**Agent 3 — foundry:perf-optimizer**: Analyze performance issues. Algorithmic complexity, Python loops that should be NumPy/torch ops, repeated computation, unnecessary I/O. ML code: check DataLoader config, mixed precision. Prioritize by impact.

**Agent 4 — foundry:doc-scribe**: Check documentation completeness. Public APIs without docstrings, missing Google style sections, outdated README, CHANGELOG gaps. Verify examples run.

- **Algorithmic accuracy check**: Functions computing mathematical results (moving averages, statistics, transforms, distances) — verify docstring behavioral claims match implementation. Deviation from conventional definition → MEDIUM; docstring must document deviation, not state standard definition. **Deprecation check**: Always check whether datetime, os.path, or other stdlib functions are deprecated in Python 3.10+ (e.g., `datetime.utcnow()` deprecated in 3.12, `os.path` vs `pathlib`). Flag deprecated stdlib as MEDIUM with replacement.

**Agent 5 — foundry:linting-expert**: Static analysis audit. Check ruff and mypy pass. Type annotation gaps on public APIs, suppressed violations without explanation, missing pre-commit hooks. Flag mismatched target Python version.

**Security augmentation (conditional — fold into Agent 1 prompt, not separate spawn)**: Target touches authentication, user input handling, dependency updates, or serialization → add to foundry:sw-engineer prompt (Agent 1): check SQL injection, XSS, insecure deserialization, hardcoded secrets, missing input validation. Run `pip-audit` if dependency files changed. Skip for purely internal refactoring.

**Agent 6 — foundry:solution-architect (optional, for changes touching public API boundaries)**: Target touches `__init__.py` exports, adds/modifies Protocols or ABCs, changes module structure, or introduces new public classes → evaluate API design quality, coupling impact, backward compatibility. Skip for internal implementation changes.

**Health monitoring** (CLAUDE.md §8): Agent calls synchronous — Claude awaits each response natively; no Bash checkpoint polling available. Agent doesn't return within `$HARD_CUTOFF` seconds → use Read tool to surface partial results in `$RUN_DIR`, continue with what found; mark timed-out agents with ⏱ in final report. Grant one `$EXTENSION` if output file tail explains delay. Never silently omit timed-out agents.

## Step 4: Cross-validate critical/blocking findings

Read and follow cross-validation protocol from `.claude/skills/_shared/cross-validation-protocol.md`. File absent → skip Step 4.

**Skill-specific**: use **same agent type** that raised finding as verifier (e.g., foundry:sw-engineer verifies foundry:sw-engineer's critical finding).

## Step 5: Consolidate findings

Before constructing output path, extract branch and date: `BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')` `DATE=$(date +%Y-%m-%d)`

Spawn **foundry:sw-engineer** consolidator with prompt:

> "Read all finding files in `$RUN_DIR/` (agent files: `sw-engineer.md`, `qa-specialist.md`, `perf-optimizer.md`, `doc-scribe.md`, `linting-expert.md`, `solution-architect.md`, and `codex.md` if present — skip missing). Read `$REVIEW_CHECKLIST` using Read tool and apply consolidation rules (signal-to-noise filter, annotation completeness, section caps). Apply precision gate: only include findings with concrete, actionable location (function, line range, or variable name). Apply finding density rule: modules under 100 lines → aim ≤10 total findings. Rank findings within each section by impact (blocking > critical > high > medium > low). For `codex.md`: include unique findings under `### Codex Co-Review` section; deduplicate against agent findings (same file:line raised by both → keep agent version, mark 'also flagged by Codex'). Parse each agent's `confidence` from its envelope; assign `codex` fixed confidence of 0.75. Write consolidated report to `.temp/output-review-$BRANCH-$DATE.md` using Write tool. Return ONLY one-line summary: `verdict=<APPROVE|REQUEST_CHANGES|NEEDS_WORK> | findings=N | critical=N | high=N | file=.temp/output-review-$BRANCH-$DATE.md`"

Main context receives only one-liner verdict.

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
<!-- Replace with actual agent scores for this review -->

**Aggregate**: min 0.N / median 0.N
```

After parsing confidence scores: any agent scored < 0.7 → prepend **⚠ LOW CONFIDENCE** to that agent's findings section, explicitly state gap. Never silently drop uncertain findings.

<!-- Extended Fields live in .claude/skills/_shared/terminal-summaries.md — if that file is absent, omit the extended fields block -->

Read compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use **PR Summary** template. Replace `[entity-line]` with `Review — [target]`, replace `[skill-specific path]` with `.temp/output-review-$BRANCH-$DATE.md`. Print block to terminal.

After printing to terminal, prepend same compact block to top of report file using Edit tool.

## Step 6: Delegate implementation follow-up (optional)

After consolidating, identify tasks Codex can implement directly — not style violations (pre-commit handles those), but work requiring meaningful code or documentation grounded in actual implementation.

**Delegate to Codex when you can write accurate, specific brief:**

- Public functions with no docstrings — read implementation first, describe what each does so Codex writes real 6-section docstring, not placeholder
- Missing test coverage for concrete, well-defined behaviour — describe exact scenario to test
- Consistent rename across multiple files — name old and new symbol and why flagged

**Do not delegate — require human judgment:**

- Architectural issues, logic errors, security vulnerabilities, behavioural changes
- Any task where accurate description requires guessing

Read `.claude/skills/_shared/codex-delegation.md`, apply delegation criteria defined there.

Print `### Codex Delegation` section to terminal only when tasks actually delegated — omit entirely if nothing delegated.

End response with `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- Critical issues always surfaced regardless of scope
- Skip sections with no issues — no padding with "looks good". Reviewing isolated code without git context → skip Performance Concerns unless code itself shows performance issues.
- **Signal-to-noise gate**: Function or class ≤50 lines with only 1–2 ground-level issues (critical/high) → no more than 2 medium/low findings beyond them. Remainder as `[nit]` in dedicated "Minor Observations" section — not elevated to same tier as high-severity findings.
- **Follow-up chains**:
  - `[blocking]` bugs or regressions → `/develop:fix` to reproduce with test and apply targeted fix
  - Structural or quality issues → `/develop:refactor` for test-first improvements
  - Security findings in auth/input/deps → run `pip-audit` for dependency CVEs; address OWASP issues inline via `/develop:fix`
  - Mechanical issues beyond Step 5 findings → `/codex:codex-rescue <task>` to delegate
  - Contributor-facing review of GitHub PR → use `/oss:review <PR#>` instead

</notes>
