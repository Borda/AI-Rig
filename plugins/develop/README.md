# 🛠️ develop — Claude Code Plugin

Six slash-command skills — `plan`, `feature`, `fix`, `refactor`, `debug`, `review` — built on a single principle: validate the problem exists before writing a single line of solution. Every code-changing skill forces you to prove you understand what you're building or breaking before you touch production code.

> Works standalone — `foundry` is not required. Without it, agent dispatches fall back to `general-purpose` with role descriptions (lower quality). Installing `foundry` unlocks specialized agents (`foundry:sw-engineer`, `foundry:qa-specialist`, etc.) and is strongly recommended.

______________________________________________________________________

<details>
<summary><strong>📋 Contents</strong></summary>

- [What is develop?](#what-is-develop)
- [Why develop?](#why-develop)
- [Install](#install)
- [Quick start](#quick-start)
- [Skills reference](#skills-reference)
  - [`/develop:plan`](#developplan)
  - [`/develop:feature`](#developfeature)
  - [`/develop:fix`](#developfix)
  - [`/develop:refactor`](#developrefactor)
  - [`/develop:debug`](#developdebug)
  - [`/develop:review`](#developreview)
- [Workflow overview](#workflow-overview)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Contributing / feedback](#contributing--feedback)

</details>

______________________________________________________________________

## 🤔 What is develop?

`develop` is a development workflow plugin for Claude Code that enforces a validate-first discipline across the full implementation lifecycle. It covers scoping, feature development, bug fixing, refactoring, debugging, and local code review — all as structured, reproducible workflows rather than freeform requests.

It is for developers who want AI assistance that follows engineering discipline rather than one that guesses and charges ahead. Every skill has explicit gates that prevent moving forward on shaky ground.

______________________________________________________________________

## 🎯 Why develop?

Without it, AI-assisted development tends to:

- Implement features before the API contract is pinned — then discover the design is wrong after the implementation
- Fix bugs by guessing the root cause, producing patches that pass tests but don't fix the actual problem
- Refactor without a safety net, breaking behavior silently
- Apply multi-file changes without knowing which downstream callers are affected

With `develop`, each workflow enforces the same discipline a rigorous engineer applies manually:

- **feature**: write a failing demo test first — if you cannot write the test, the feature is underspecified
- **fix**: reproduce the bug with a failing regression test first — if you cannot reproduce it, you cannot verify the fix
- **refactor**: audit test coverage and lock in characterization tests before moving a single line
- **debug**: gather all evidence and state one confirmed hypothesis before proposing any fix
- **plan**: scope complexity, identify blast radius, and get agent feasibility review before committing to implementation
- **review**: run six specialist agents across architecture, tests, performance, docs, lint, and API design against your local diff — no GitHub PR required

______________________________________________________________________

## 📦 Install

**Prerequisites**

You need Claude Code installed and access to the `Borda-AI-Rig` repository.

```bash
# Verify Claude Code is available
claude --version
```

**Install develop**

Run from the directory that **contains** your `Borda-AI-Rig` clone (not from inside it):

```bash
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install develop@borda-ai-rig
```

<details>
<summary><strong>Install the full suite (recommended)</strong></summary>

```bash
claude plugin install foundry@borda-ai-rig   # specialized agents — strongly recommended
claude plugin install oss@borda-ai-rig        # progressive review loop integration
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

</details>

Installing `foundry` gives `develop` access to `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, and others. Without it, all agent dispatches fall back to `general-purpose` with role-description prompts — functional but lower quality.

<details>
<summary><strong>Verify installation</strong></summary>

```bash
claude plugin list | grep -F 'develop@borda-ai-rig'
```

You should see an enabled entry like `develop@borda-ai-rig` in the output.

</details>

______________________________________________________________________

## ⚡ Quick start

The fastest way to get immediate value: scope your next task before starting it.

```text
/develop:plan "extract data loading into a dedicated DataLoader class"
```

`plan` reads your codebase, classifies the task, identifies affected files, estimates complexity, runs a parallel feasibility review with specialist agents, and writes a structured plan to `.plans/active/`. It then tells you exactly which skill to run next:

```text
Plan -> .plans/active/plan_extract-data-loading-dataloader.md

Classification : refactor
Complexity     : medium
Affected files : 4 files across 2 modules
Key risks      : Public API changes in dataset.py — 3 callers
Agent review   : ✓ agents ready (1 correction incorporated)

-> /develop:refactor "extract data loading into a dedicated DataLoader class" when ready
```

______________________________________________________________________

## 🔧 Skills reference

All skills are invoked with the `develop:` prefix.

______________________________________________________________________

### `/develop:plan`

**Purpose**: Scope a task before committing to it. Produces a structured plan with classification, complexity estimate, affected files, risks, and a suggested implementation approach. No code is written.

**When to use**: before any non-trivial feature, fix, or refactor; when you are unsure of the blast radius or complexity of a change; when you want agent-validated feasibility before starting.

**Invocation**:

```text
/develop:plan "<goal>"
```

**Flags**: none. Pass the goal as free text.

**What happens**:

1. Spawns `foundry:sw-engineer` to classify the task (feature / fix / refactor), map affected files, estimate complexity (small / medium / large), and list risks
2. Writes a structured plan to `.plans/active/<slug>.md`
3. Spawns parallel feasibility agents matching the classification — they flag blockers, open questions, and concerns
4. Attempts to resolve blockers autonomously (codebase search, WebFetch for docs); escalates only what genuinely requires your input
5. Annotates the plan with resolved/unresolved status and writes a Brief summary

**Output to terminal**:

```markdown
Plan -> .plans/active/plan_<slug>.md

Classification : feature | fix | refactor
Complexity     : small | medium | large
Affected files : N files across M modules
Key risks      : <one-liner>
Agent review   : ✓ agents ready (N corrections incorporated)

-> /develop:feature|fix|refactor "<goal>" when ready
```

**Passing the plan to downstream skills**: every code-changing skill accepts `--plan <path>`. When provided, the skill reads classification, affected files, risks, and suggested approach from the plan — skipping cold codebase exploration and inheriting feasibility verdicts already validated.

```text
/develop:plan "add streaming response support"
/develop:feature "add streaming response support" --plan .plans/active/plan_add-streaming-response-support.md
```

**What plan does NOT do**: write any code or tests. It is analysis-only.

______________________________________________________________________

### `/develop:feature`

**Purpose**: TDD-first feature development. Crystallises the API as a failing demo test, drives implementation to pass it, then closes quality gaps via a review loop and documentation update.

**When to use**: adding new behavior to the codebase.

**Not for**: bug fixes — use `/develop:fix`.

**Invocation**:

```text
/develop:feature "<goal>"
/develop:feature "<goal>" --plan <path>    # skip cold analysis, use existing plan
/develop:feature "<goal>" --team           # parallel agents for complex/cross-module features
```

**Flags**:

| Flag            | Description                                                                                                                                                                                |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--plan <path>` | Read classification, scope, and approach from an existing plan file                                                                                                                        |
| `--team`        | Spawn parallel `foundry:sw-engineer` + `foundry:qa-specialist` + `foundry:doc-scribe` teammates. Use when feature spans 3+ modules, changes public API, or touches auth/payment/data scope |

**Workflow**:

1. **Scope analysis** (`foundry:sw-engineer`): understand existing patterns, reuse opportunities, affected files, and compatibility concerns. If a GitHub issue number is provided, fetches the full issue with comments.
2. **Source verification** (conditional): if the feature calls an external library API, detects the installed version from `pyproject.toml`, fetches the official docs page via WebFetch, and cites the relevant passage in code comments.
3. **Demo use-case**: crystallises the API contract as either an inline doctest (simple functions) or an example script (complex features with setup). The demo must fail against current code before proceeding. Gate enforced via exit code — not output text.
4. **TDD implementation loop** (`foundry:sw-engineer`): makes tests pass one at a time, running the full suite after each change to catch regressions.
5. **Review and close gaps**: 5-axis quality scan (correctness, readability, architecture, security, performance) → fix loop, max 3 cycles.
6. **Documentation** (`foundry:doc-scribe`): Google-style docstrings, CHANGELOG entry, README updates if public API changed.
7. **Quality stack**: lint/format (ruff) → type check (mypy) → full test suite → blast-radius check (codemap) → Codex pre-pass → progressive review loop.

**Realistic example**:

```text
/develop:plan "add CSV export to the results API"
/develop:feature "add CSV export to the results API" --plan .plans/active/plan_add-csv-export-results-api.md
```

**Team mode coordination**: Lead broadcasts Step 1 analysis. `foundry:qa-specialist` challenges the API design before implementation starts. `foundry:sw-engineer` implements while `foundry:qa-specialist` writes TDD tests in parallel. `foundry:doc-scribe` prepares documentation structure concurrently.

______________________________________________________________________

### `/develop:fix`

**Purpose**: Reproduce-first bug resolution. Captures the bug in a failing regression test before applying any fix.

**When to use**: fixing a known bug with a traceback, failing test, or GitHub issue.

**Not for**: unknown failures without a traceback or reproduction path — use `/foundry:investigate` for triage; `.claude/` config issues — use `/foundry:audit`.

**Invocation**:

```text
/develop:fix "<symptom description>"
/develop:fix 88                              # GitHub issue number — fetches full issue + comments
/develop:fix "<symptom>" --plan <path>       # use existing plan
/develop:fix "<symptom>" --diagnosis <path>  # skip root cause analysis; use debug output
/develop:fix "<symptom>" --team              # parallel root-cause investigation
```

**Flags**:

| Flag                 | Description                                                                                                 |
| -------------------- | ----------------------------------------------------------------------------------------------------------- |
| `--plan <path>`      | Read scope and approach from an existing plan file                                                          |
| `--diagnosis <path>` | Read confirmed root cause from a `/develop:debug` output file; skips Step 1 analysis entirely               |
| `--team`             | Spawn 2-3 `foundry:sw-engineer` teammates each investigating a distinct root-cause hypothesis independently |

**Workflow**:

1. **Understand the problem** (`foundry:sw-engineer`): reads the full traceback, searches for the failing code path, traces call graph, identifies root cause, state mutation, and blast radius. If the argument is a positive integer, fetches the GitHub issue.
2. **Reproduce the bug** (`foundry:qa-specialist`): writes a regression test that fails on unfixed code. Gate: test must exit non-zero before proceeding.
3. **Apply the fix** (`foundry:sw-engineer`): minimal change — only what is necessary to make the regression test pass.
4. **Review and close gaps**: 5-axis quality scan → fix loop, max 3 cycles. Adjacent bugs are documented as observations and handled in a separate session — never fixed in the same pass.
5. **Quality stack**: ruff → mypy → full test suite → blast-radius check → Codex pre-pass → progressive review loop.

**Realistic example**:

```text
/develop:fix "KeyError in transform pipeline when input has null values"
/develop:fix 124   # fix GitHub issue #124
```

**Using debug output**:

```text
/develop:debug "intermittent timeout on /api/predict under load"
# After debug session writes .plans/active/debug_intermittent-timeout.md:
/develop:fix "intermittent timeout on /api/predict under load" --diagnosis .plans/active/debug_intermittent-timeout.md
```

**Scope gate**: if root cause spans 3+ modules, you are asked whether to narrow scope or proceed — prevents large unfocused fixes.

______________________________________________________________________

### `/develop:refactor`

**Purpose**: Test-first refactoring. Audits existing test coverage, adds characterization tests for gaps, then restructures the code with a safety net that catches any behavior change.

**When to use**: restructuring existing code — extracting classes, simplifying logic, cleaning API, removing dead code — without changing observed behavior.

**Not for**: bug fixes — use `/develop:fix`; new features — use `/develop:feature`.

**Invocation**:

```text
/develop:refactor "<target file or directory> <goal>"
/develop:refactor "<goal>" --plan <path>
/develop:refactor "<goal>" --team            # parallel: foundry:sw-engineer refactors + foundry:qa-specialist writes tests simultaneously
```

**Flags**:

| Flag            | Description                                                                                                                                                          |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--plan <path>` | Read scope and approach from an existing plan file                                                                                                                   |
| `--team`        | Spawn `foundry:sw-engineer` (refactoring) and `foundry:qa-specialist` (characterization tests) in parallel. Use when target is a directory or spans multiple modules |

**Workflow**:

1. **Scope and understand** (`foundry:sw-engineer`): reads the target code, maps the public API surface, identifies complexity hotspots and coupling. Uses codemap for blast-radius analysis when available. Scope gate: if target is directory-wide (10+ files), asks whether to narrow or proceed.
2. **Audit test coverage**: classifies each public function as covered / partially covered / uncovered. Falls back to "all uncovered" conservatively if `pytest-cov` is not installed.
3. **Add characterization tests** (`foundry:qa-specialist`): for every uncovered or partially covered public API, generates tests that assert *current* behavior (not desired behavior). Gate: all characterization tests must pass on unmodified code before proceeding.
4. **Refactor with safety net**: one focused change per cycle, run tests after each. Safety break: max 5 change-test cycles per inner session; max 10 total across all outer review cycles.
5. **Review and close gaps**: checks behavior preservation, goal achievement, no new smells, no unintended API surface changes. Max 3 outer review cycles.
6. **Quality stack**: ruff → mypy → full test suite → blast-radius check → Codex pre-pass → progressive review loop.

**Refactoring categories the skill handles**:

- Logic simplification: replace complex conditionals, flatten nesting, extract helpers
- API cleanup: rename for clarity, consolidate parameters, add type annotations
- Structural: extract classes or modules, reduce coupling, apply design patterns
- Performance: replace loops with vectorized ops, reduce allocations, batch I/O
- Dead code removal: unused imports, unreachable branches, unexported public methods

**Realistic example**:

```text
/develop:plan "extract data loading into a dedicated DataLoader class"
/develop:refactor "extract data loading into a dedicated DataLoader class" --plan .plans/active/plan_extract-data-loading-dataloader.md
```

**Checkpoint and resume**: creates `.developments/<timestamp>/checkpoint.md` after each step. If the session is interrupted, re-running the skill offers to resume from the last completed step.

______________________________________________________________________

### `/develop:debug`

**Purpose**: Investigation-first debugging. Gathers all available signals, traces the failure path, forms a single confirmed root-cause hypothesis, writes a diagnosis file, and hands off to `/develop:fix`.

**When to use**: when you have a symptom but not a confirmed root cause; when a bug is mysterious enough to warrant structured investigation before fixing.

**Not for**: production incidents without local reproduction — use `/foundry:investigate`; `.claude/` config issues — use `/foundry:audit`.

**Invocation**:

```text
/develop:debug "<symptom description>"
/develop:debug 88                       # GitHub issue number
/develop:debug "<symptom>" --team       # parallel hypothesis investigation
```

**Flags**:

| Flag     | Description                                                                                                                                                                                      |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--team` | Spawn 2-3 `foundry:sw-engineer` teammates, each investigating a distinct root-cause hypothesis independently. Use when root cause is unclear after initial analysis, or failure spans 3+ modules |

**Workflow**:

1. **Understand the symptom** (`foundry:sw-engineer`): reads full tracebacks, recent git changes near the failing code, and traces the call path from entry point to failure site.
2. **Pattern analysis**: finds 2-3 similar working code paths and compares them exhaustively against the broken path — across input, environment, call order, conditional branches, and None/empty guards.
3. **Hypothesis and gate**: states root cause explicitly with supporting and contradicting evidence and a confidence level (high / medium / low). Presents hypothesis to you and waits for confirmation before proceeding. Low confidence triggers a targeted probe (minimal script, added assertion) to gather missing signal.
4. **Hand off to fix**: writes a diagnosis file to `.plans/active/debug_<slug>.md` and emits `-> /develop:fix --diagnosis <path>`. Fix's Step 1 analysis is pre-answered by the diagnosis.

**Debug is investigation-only** — no code changes. The fix happens in a separate, auditable session with its own regression test gate.

**Realistic example**:

```text
/develop:debug "intermittent timeout on /api/predict under load"
# -> /develop:fix --diagnosis .plans/active/debug_intermittent-timeout-api-predict.md
```

**Team mode**: teammates independently investigate competing hypotheses, lead facilitates cross-challenge, then synthesises consensus before handing off to fix.

______________________________________________________________________

### `/develop:review`

**Purpose**: Comprehensive local code review via six specialist agents in parallel — covering architecture, tests, performance, documentation, static analysis, and API design. Works against local files or the current git diff. No GitHub PR required.

**When to use**: reviewing your own changes before committing; getting structured feedback on local files; closing quality gaps before opening a PR.

**Not for**: GitHub PR review — use `/oss:review <PR#>`; implementation work — use `/develop:feature` or `/develop:fix`.

**Scope**: Python source files only. Non-Python files (YAML, Dockerfile, JSON, shell scripts) are flagged in the report header as "not reviewed" but their presence is noted because dependency or config changes can silently break reviewed Python code.

**Invocation**:

```text
/develop:review                          # review current git diff (staged + unstaged vs HEAD)
/develop:review src/mypackage/module.py  # review a specific file
/develop:review src/mypackage/           # review all Python files in a directory
```

**Flags**: none. Pass an optional file or directory path as the argument.

**Workflow**:

1. **Identify scope**: collects Python files from the path or `git diff HEAD`. Classifies the diff as FIX / REFACTOR / FEATURE / MIXED — skips optional agents for smaller diffs (e.g., FIX skips `foundry:perf-optimizer` and `foundry:solution-architect`).
2. **Codex co-review** (if `codex` plugin installed): adversarial diff review to seed a pre-flagged issues list for the specialist agents.
3. **Six parallel agents** (file-based handoff — each writes findings to `.reports/review/<timestamp>/`):
   - `foundry:sw-engineer`: architecture, SOLID adherence, type safety, error handling, Python anti-patterns, security for touched auth/input/data paths
   - `foundry:qa-specialist`: test coverage gaps, missing edge cases, ML non-determinism, seed pinning, boundary conditions
   - `foundry:perf-optimizer`: algorithmic complexity, loops that should be NumPy/torch ops, unnecessary I/O, ML DataLoader config (skipped for FIX diffs)
   - `foundry:doc-scribe`: public APIs without docstrings, Google-style section gaps, CHANGELOG entries, deprecated stdlib usage
   - `foundry:linting-expert`: ruff violations, mypy errors, type annotation gaps on public API, suppressed violations
   - `foundry:solution-architect`: API design quality, coupling, backward compatibility (only for changes touching public API boundaries; skipped for REFACTOR and FIX)
4. **Cross-validate** critical and blocking findings using the same agent type that raised each finding.
5. **Consolidate** (`foundry:sw-engineer`): reads all agent findings, deduplicates, ranks by impact, writes full report to `.temp/output-review-<branch>-<date>.md`. Applies signal-to-noise gate: small modules do not get padded with low-severity findings.
6. **Codex delegation** (optional): delegates mechanical tasks — docstrings, missing tests for concrete scenarios, consistent renames — to Codex when a precise brief can be written.

**Report structure**:

```text
Critical (must fix)
Architecture & Quality
Test Coverage Gaps
Performance Concerns
Documentation Gaps
Static Analysis
API Design
Codex Co-Review
Recommended Next Steps
Review Confidence (per-agent scores)
```

**Realistic example**:

```text
git add src/mypackage/trainer.py tests/test_trainer.py
/develop:review src/mypackage/trainer.py
```

**Follow-up from review findings**:

- Blocking bugs or regressions → `/develop:fix`
- Structural or quality issues → `/develop:refactor`
- Security findings → address via `/develop:fix`; run `pip-audit` if dependency files changed
- Mechanical issues (docstrings, missing tests) → `/codex:codex-rescue <task>` if Codex available
- GitHub PR review for a contributor → `/oss:review <PR#>` instead

______________________________________________________________________

## 🗺️ Workflow overview

Skills chain together naturally. A typical development session looks like this:

### New feature

```text
# 1. Scope — understand what you're building before building it
/develop:plan "add rate limiting to the API gateway"

# 2. Implement — TDD contract pins the API, then implementation follows tests
/develop:feature "add rate limiting to the API gateway" --plan .plans/active/plan_add-rate-limiting-api-gateway.md

# 3. Review before committing (optional — quality stack already ran, but useful for a final check)
/develop:review src/gateway/
```

### Bug fix

```text
# Option A: symptom is clear enough — go straight to fix
/develop:fix "RateLimiter raises AttributeError when Redis connection fails"

# Option B: mysterious failure — investigate first
/develop:debug "API gateway returns 200 on every request under high load"
# Debug writes: .plans/active/debug_api-gateway-200-high-load.md
/develop:fix "API gateway returns 200 on every request under high load" --diagnosis .plans/active/debug_api-gateway-200-high-load.md
```

### Safe refactor

```text
/develop:plan "extract request parsing into a dedicated middleware layer"
/develop:refactor "extract request parsing into a dedicated middleware layer" --plan .plans/active/plan_extract-request-parsing-middleware.md
```

### Review before a PR

```text
/develop:review    # reviews the full current diff (staged + unstaged vs HEAD)
```

### Complex or high-stakes work

Add `--team` to any code-changing skill. It spawns parallel specialist agents exploring the implementation space independently. Significantly higher token cost — reserve for changes spanning multiple modules, public API additions, or work in auth/payment/data scope.

```text
/develop:feature "add streaming response support" --team
/develop:fix "memory leak in batch inference" --team
```

______________________________________________________________________

## ⚙️ Configuration

### Dependencies by capability

| Dependency       | Required    | Unlocks                                                                                                                                                                                                                          |
| ---------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `foundry` plugin | recommended | `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, and others; quality stack shared file. Without foundry, all agents fall back to `general-purpose` with role-description prompts. |
| `oss` plugin     | optional    | `/oss:review` used in the progressive review loop (quality stack); `oss:review` checklist used by `develop:review` Agent 1. Absent — review loop step skipped gracefully.                                                        |
| `codex` plugin   | optional    | Codex pre-pass in quality stack; Codex adversarial co-review in `develop:review`; mechanical delegation in Step 6. Gracefully skipped if absent.                                                                                 |
| `codemap`        | optional    | `scan-query` for blast-radius check in quality stack; structural context for refactor, plan, and review. Silently skipped if absent or index missing.                                                                            |
| `gh` CLI         | optional    | Used in `fix` and `debug` when argument is a GitHub issue number (`gh issue view`).                                                                                                                                              |

### Python tooling

The quality stack auto-detects your project's tooling at skill start via the shared runner-detection file. No configuration needed — it finds `uv`, `ruff`, `mypy`, and `pytest` if they are on the path. If a tool is absent, that stack step is skipped with a note in the final report.

### Artifact directories

Skills write to these directories at project root (all gitignored):

| Directory                      | Contents                                                               |
| ------------------------------ | ---------------------------------------------------------------------- |
| `.plans/active/`               | Plan files from `/develop:plan`, diagnosis files from `/develop:debug` |
| `.developments/<timestamp>/`   | Checkpoint files for resumable feature/fix/refactor sessions           |
| `.reports/review/<timestamp>/` | Per-agent finding files from `/develop:review`                         |
| `.temp/`                       | Consolidated review reports                                            |

Completed runs are cleaned up after 30 days. Interrupted runs (no `result.jsonl`) are kept for debugging.

______________________________________________________________________

<details>
<summary>

## 🔍 Troubleshooting

</summary>

## 🔍 Troubleshooting

### "foundry plugin not installed — quality stack skipped"

The quality stack reads a shared file from the `foundry` plugin. If `foundry` is not installed, the lint/type/test/blast-radius steps are skipped entirely and the final report notes this.

```bash
claude plugin install foundry@borda-ai-rig
```

### Demo gate passes (exit 0) when it should fail

`/develop:feature` Step 2 confirms the demo fails before implementation. If the gate exits 0, the feature may already be implemented, or the test is testing the wrong thing. The skill stops and asks you to revisit Step 1. Do not force past this gate — it means either the feature exists already or the demo is not testing the contract you intend.

### Regression test gate passes when it should fail

Same pattern in `/develop:fix` Step 2. If the regression test passes on unfixed code, the test is not capturing the bug. Revisit Step 1 — either the symptom description is not pointing at the actual failure site, or the test exercises a different code path.

### Characterization test fails on unmodified code

In `/develop:refactor` Step 3, characterization tests must pass before refactoring begins. If a characterization test fails, the test is wrong — it must assert *current* behavior, not desired behavior. Fix the test to match what the code actually does now.

### Session interrupted mid-skill

`feature`, `fix`, and `refactor` write a checkpoint file to `.developments/<timestamp>/checkpoint.md` after each major step. Re-running the same skill command offers to resume from the last completed step.

### scan-query warnings appearing in output

`codemap` is optional. If `scan-query` is not on your PATH or the index file is missing, all codemap steps are silently skipped — no blast-radius check, no structural context for analysis agents. The skill works fully without it. To enable codemap context, install the `codemap` plugin and run `/codemap:scan`.

______________________________________________________________________

</details>

## 🙏 Contributing / feedback

This plugin is part of the `borda-ai-rig` plugin suite. The canonical source is in `plugins/develop/` within the repository.

To report a bug or suggest an improvement, open an issue in the repository. Include the skill name, the invocation you used, and what the actual vs expected behavior was.

**To update the plugin after a repository pull**:

```bash
cd Borda-AI-Rig
git pull
claude plugin install develop@borda-ai-rig
```

**To uninstall**:

```bash
claude plugin uninstall develop
```

**Plugin structure**:

```text
plugins/develop/
├── .claude-plugin/
│   └── plugin.json          -- manifest (name, version, author)
└── skills/
    ├── plan/
    │   └── SKILL.md
    ├── feature/
    │   └── SKILL.md
    ├── fix/
    │   └── SKILL.md
    ├── refactor/
    │   └── SKILL.md
    ├── debug/
    │   └── SKILL.md
    └── review/
        └── SKILL.md
```

If you modify any skill, update this README before finishing — an unsynced change is an incomplete change.
