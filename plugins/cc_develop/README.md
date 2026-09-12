# 🛠️ develop — Claude Code Plugin

Six development workflows — `plan`, `feature`, `fix`, `refactor`, `debug`, and `review` — help Claude Code understand a Python change before it edits production code. `/develop:setup` is the separate post-install command that delivers this plugin's rule symlinks.

The gates narrow the failure surface — they do not replace developer judgment on whether a generated change is correct or production-safe.

Optional Codemap context and index-gate guidance ship with develop, so loading them does not depend on another plugin's private shared directory. The host-provided active installation takes precedence over other cached versions. Structural queries still require the `codemap-py` plugin; an unavailable CLI or local contract retains the file-read fallback.

> Works standalone — `foundry` is not required. Without it, agent dispatches fall back to `general-purpose` with role descriptions; with it, the same workflows can route to named specialists such as `foundry:sw-engineer` and `foundry:qa-specialist`.

______________________________________________________________________

<details>

<summary><strong>📋 Contents</strong></summary>

- [What is develop?](#-what-is-develop)
- [Why develop?](#-why-develop)
- [Install](#-install)
- [Quick start](#-quick-start)
- [Skills reference](#-skills-reference)
  - [`/develop:plan`](#developplan)
  - [`/develop:feature`](#developfeature)
  - [`/develop:fix`](#developfix)
  - [`/develop:refactor`](#developrefactor)
  - [`/develop:debug`](#developdebug)
  - [`/develop:review`](#developreview)
  - [`/develop:setup`](#developsetup)
- [Workflow overview](#-workflow-overview)
- [Configuration](#-configuration)
- [Bin helper inventory](#bin-helper-inventory)
- [Troubleshooting](#-troubleshooting)
- [Contributing / feedback](#-contributing--feedback)

</details>

______________________________________________________________________

## 🤔 What is develop?

`develop` is a Claude Code plugin for validate-first Python development: scope work, pin a feature contract, reproduce a bug, preserve behavior during refactoring, investigate failures, and review local Python changes with explicit evidence and handoff artifacts.

Each workflow has gates that pause when the contract, reproduction, evidence, or safety net is missing.

> Current boundaries: code-changing and review workflows target Python projects with pytest-style tooling; `/develop:plan` can analyze a broader task, but downstream implementation still assumes pytest. Dependency migrations, data migrations, codebase onboarding, and Python-free changes remain outside this plugin's current scope; use the project's native workflow or another tool. Broader language support could be added later, but is not promised here.

______________________________________________________________________

## 🎯 Why develop?

Without it, AI-assisted development tends to:

- Implement before API contract pinned — discover wrong design after implementation
- Fix bugs by guessing root cause — patches pass tests, don't fix actual problem
- Refactor without safety net — break behavior silently
- Apply multi-file changes blind to affected downstream callers

These workflows address common maintenance failures with concrete gates:

- **feature**: failing demo test first — cannot write test = feature underspecified
- **fix**: reproduce bug with failing regression test first — cannot reproduce = cannot verify fix
- **refactor**: audit test coverage, lock characterization tests before moving one line
- **debug**: gather all evidence, state one confirmed hypothesis before any fix
- **plan**: scope complexity, identify blast radius, agent feasibility review before committing
- **review**: local Python review across architecture, tests, performance, docs, lint, security, and API design. It groups the relevant dimensions into spawn units (performance+architecture and docs+lint each share one agent) and runs the top three units by default, or all selected units with `--full`; no GitHub PR is required.

When Codemap is enabled, `fix` selects a route before retrieval: a fully localized file-and-symbol edit can use the explicit zero-query path, while unresolved callers, dependencies, blast radius, imports, or source scope receive only the matching compact query.

______________________________________________________________________

## 📦 Install

**Prerequisites**: Claude Code installed (`claude --version`) and Python 3.10+ with a project test runner for code-changing/review workflows. The plugin itself is installed from the `borda-ai-rig` marketplace; project dependencies remain your responsibility.

**Install develop**

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install develop@borda-ai-rig
```

Then run `/develop:setup` once to link this plugin's rules into `~/.claude/rules/`. Re-run it after every upgrade. A repository checkout may also run `make sync-claude`, but that is not required for a marketplace install.

<details>

<summary><strong>Optional integrations</strong></summary>

```bash
claude plugin install foundry@borda-ai-rig   # named specialist agents
claude plugin install oss@borda-ai-rig        # optional PR review and severity checklist integration
claude plugin install research@borda-ai-rig
```

</details>

`foundry` gives `develop` access to named specialist agents such as `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, and `foundry:doc-scribe`. Without it, dispatches fall back to `general-purpose` with role descriptions. The `develop` plugin ships its own quality-stack and shared workflow files; `foundry` is not required for those files to load.

<details>

<summary><strong>Verify installation</strong></summary>

```bash
claude plugin list | grep -F 'develop@borda-ai-rig'
```

Expect enabled entry like `develop@borda-ai-rig` in output.

</details>

______________________________________________________________________

## ⚡ Quick start

Fastest value: scope next task before starting.

```text
/develop:plan "extract data loading into a dedicated DataLoader class"
```

`plan` reads codebase, classifies task, identifies affected files, estimates complexity, runs parallel feasibility review with specialist agents, writes structured plan to `.plans/active/`. Then tells exactly which skill to run next:

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

All skills invoked with `develop:` prefix.

______________________________________________________________________

### `/develop:plan`

**Purpose**: Scope task before committing. Produces structured plan: classification, complexity estimate, affected files, risks, suggested approach. No code written.

**When to use**: before non-trivial feature/fix/refactor; blast radius or complexity unclear; want agent-validated feasibility first.

**Invocation**:

```text
/develop:plan "<goal>"
```

**Flags**:

| Flag              | Description                                          |
| ----------------- | ---------------------------------------------------- |
| `--no-challenge`  | Skip challenger adversarial gate                     |
| `--codemap`       | Require Codemap and an index; stop when unavailable  |
| `--no-codemap`    | Disable Codemap even if available                    |
| `--semble`        | Enable the optional Semble semantic-search preflight |
| `--max-depth <N>` | Limit plan → debug → plan cycles (default: `3`)      |

**What happens**:

1. Spawns `foundry:sw-engineer` to classify task (feature / fix / refactor), map affected files, estimate complexity (small / medium / large), list risks. With codemap: effort sizing structural, not file-count-based — reverse-dependency counts per affected module set blast tier (≥5 rdeps HIGH, 1–4 MODERATE, 0 LOW), co-change coupled pairs surface as risks. HIGH module or 3+ affected modules push complexity to `large`
2. Writes structured plan to `.plans/active/<slug>.md`
3. Spawns parallel feasibility agents matching classification — flag blockers, open questions, concerns
4. Resolves blockers autonomously (codebase search, WebFetch for docs); escalates only what genuinely needs your input
5. Annotates plan with resolved/unresolved status, writes Brief summary

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

**Passing plan to downstream skills**: every code-changing skill accepts `--plan <path>`. Skill reads classification, affected files, risks, suggested approach from plan — skips cold codebase exploration, inherits validated feasibility verdicts.

```text
/develop:plan "add streaming response support"
/develop:feature "add streaming response support" --plan .plans/active/plan_add-streaming-response-support.md
```

**What plan does NOT do**: write code or tests. Analysis-only.

______________________________________________________________________

### `/develop:feature`

**Purpose**: TDD-first feature development. Crystallises API as failing demo test, drives implementation to pass, closes quality gaps via review loop + docs update.

**When to use**: adding new behavior.

**Not for**: bug fixes — use `/develop:fix`.

**Invocation**:

```text
/develop:feature "<goal>"
/develop:feature "<goal>" --plan <path>                     # skip cold analysis, use existing plan
/develop:feature 123 --repo owner/upstream-repo         # implement issue from upstream repo (fork workflow)
/develop:feature "<goal>" --team                            # parallel agents for complex/cross-module features
```

**Flags**:

| Flag                  | Description                                                                                                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--issue <N>`         | Fetch issue `<N>` as the feature request; numeric positional arguments are also recognized as issue references                                                                                                                 |
| `--repo <owner/repo>` | Route issue fetch to upstream repo. Use when working in fork and issue on original repo (e.g. `--repo owner/my-project`).                                                                                                      |
| `--plan <path>`       | Read classification, scope, approach from existing plan file                                                                                                                                                                   |
| `--team`              | Spawn parallel `foundry:sw-engineer` + `foundry:qa-specialist` + `foundry:doc-scribe` teammates. Use when feature spans 3+ modules, changes public API, or touches auth/payment/data scope                                     |
| `--worktree`          | Run the whole skill in an isolated git worktree (`.claude/worktrees/`) on a new branch — you review + merge (never auto-merged). Codemap index is per-worktree, so parallel runs never race one index. Composes with `--team`. |
| `--no-codemap`        | Disable codemap even if available                                                                                                                                                                                              |
| `--codemap`           | Require Codemap and an index; stop when unavailable                                                                                                                                                                            |
| `--accept-no-plan`    | Skip inline plan generation for medium/large scope (trust own scoping)                                                                                                                                                         |
| `--no-challenge`      | Skip challenger adversarial gate                                                                                                                                                                                               |
| `--challenge`         | Force challenger gate even on small change auto-skip would otherwise skip                                                                                                                                                      |
| `--keep "<items>"`    | Append items to compaction contract preserve field — keeps key context if auto-compaction fires mid-skill                                                                                                                      |

**Workflow**:

1. **Scope analysis** (`foundry:sw-engineer`): existing patterns, reuse opportunities, affected files, compatibility concerns. GitHub issue number given → fetches full issue + comments (upstream if `--repo`).
2. **Source verification** (conditional): feature calls external library API → detects installed version from `pyproject.toml`, fetches official docs via WebFetch, cites relevant passage in code comments.
3. **Demo use-case**: crystallises API contract as inline doctest (simple functions) or example script (complex features with setup). Demo must fail against current code before proceeding. Gate enforced via exit code — not output text.
4. **TDD implementation loop** (`foundry:sw-engineer`): tests pass one at a time, full suite after each change to catch regressions.
5. **Review and close gaps**: 5-axis quality scan (correctness, readability, architecture, security, performance) → fix loop, max 3 cycles.
6. **Documentation** (`foundry:doc-scribe`): updates docstrings and README content when the implementation changes a public API; a separate changelog step is used only when the project has a changelog convention.
7. **Quality stack**: available ruff/mypy checks → full test suite → optional Codemap blast-radius check → optional Codex pre-pass → progressive review loop.

**Realistic example**:

```text
/develop:plan "add CSV export to the results API"
/develop:feature "add CSV export to the results API" --plan .plans/active/plan_add-csv-export-results-api.md
```

**Team mode coordination**: Lead broadcasts Step 1 analysis. `foundry:qa-specialist` challenges API design before implementation. `foundry:sw-engineer` implements while `foundry:qa-specialist` writes TDD tests parallel. `foundry:doc-scribe` prepares docs structure concurrently.

______________________________________________________________________

### `/develop:fix`

**Purpose**: Reproduce-first bug resolution. Captures bug in failing regression test before any fix.

**When to use**: fixing known bug with traceback, failing test, or GitHub issue.

**Not for**: CI-only failures — use `/develop:debug --ci-run <run-id>` first; production incidents without CI run or traceback — use `/foundry:investigate` (requires `foundry` plugin); `.claude/` config issues — use `/foundry:audit` (requires `foundry` plugin).

**Invocation**:

```text
/develop:fix "<symptom description>"
/develop:fix 88                                     # GitHub issue number — fetches full issue + comments
/develop:fix 88 --repo owner/upstream-repo          # issue on upstream repo (fork workflow)
/develop:fix "<symptom>" --plan <path>              # use existing plan
/develop:fix "<symptom>" --diagnosis <path>         # skip root cause analysis; use debug output
/develop:fix "<symptom>" --team                     # parallel root-cause investigation
```

**Flags**:

| Flag                  | Description                                                                                                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--repo <owner/repo>` | Route issue fetch to upstream repo. Use when working in fork and issue on original repo (e.g. `--repo owner/my-project`).                                                                                                      |
| `--plan <path>`       | Read scope and approach from existing plan file                                                                                                                                                                                |
| `--diagnosis <path>`  | Read confirmed root cause from `/develop:debug` output file; skips Step 1 analysis entirely                                                                                                                                    |
| `--team`              | Spawn 2-3 `foundry:sw-engineer` teammates, each investigating distinct root-cause hypothesis independently                                                                                                                     |
| `--worktree`          | Run the whole skill in an isolated git worktree (`.claude/worktrees/`) on a new branch — you review + merge (never auto-merged). Codemap index is per-worktree, so parallel runs never race one index. Composes with `--team`. |
| `--no-challenge`      | Skip challenger adversarial gate entirely                                                                                                                                                                                      |
| `--challenge`         | Force challenger gate even on small change auto-skip would otherwise skip                                                                                                                                                      |
| `--keep "<items>"`    | Append items to compaction contract preserve field — keeps key context if auto-compaction fires mid-skill                                                                                                                      |

**Workflow**:

1. **Understand the problem** (`foundry:sw-engineer`): reads full traceback, searches failing code path, traces call graph, identifies root cause, state mutation, blast radius. Argument = positive integer → fetches GitHub issue (upstream if `--repo`).
2. **Reproduce the bug** (`foundry:qa-specialist`): writes regression test failing on unfixed code. Gate: test must exit non-zero before proceeding.
3. **Apply the fix** (`foundry:sw-engineer`): minimal change — only what makes regression test pass.
4. **Review and close gaps**: 5-axis quality scan → fix loop, max 3 cycles. Adjacent bugs documented as observations, handled in separate session — never fixed same pass.
5. **Quality stack**: available ruff/mypy checks → full test suite → optional Codemap blast-radius check → optional Codex pre-pass → progressive review loop.

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

**Scope gate**: root cause spans 3+ modules → asked narrow scope or proceed — prevents large unfocused fixes.

______________________________________________________________________

### `/develop:refactor`

**Purpose**: Test-first refactoring. Audits test coverage, adds characterization tests for gaps, restructures code with safety net catching any behavior change.

**When to use**: restructuring existing code — extracting classes, simplifying logic, cleaning API, removing dead code — without changing observed behavior.

**Not for**: bug fixes — use `/develop:fix`; new features — use `/develop:feature`.

**Invocation**:

```text
/develop:refactor "<target file or directory> <goal>"
/develop:refactor "<goal>" --plan <path>
/develop:refactor "<goal>" --repo owner/upstream-repo   # context from upstream issue (fork workflow)
/develop:refactor "<goal>" --team                       # parallel: foundry:sw-engineer refactors + foundry:qa-specialist writes tests simultaneously
```

**Flags**:

| Flag                  | Description                                                                                                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--repo <owner/repo>` | Route issue fetch to upstream repo. Use when refactor context tied to upstream issue in fork. Parsed for consistency; refactor has no issue-fetch step by default.                                                             |
| `--plan <path>`       | Read scope and approach from existing plan file                                                                                                                                                                                |
| `--team`              | Spawn `foundry:sw-engineer` (refactoring) + `foundry:qa-specialist` (characterization tests) parallel. Use when target is directory or spans multiple modules                                                                  |
| `--worktree`          | Run the whole skill in an isolated git worktree (`.claude/worktrees/`) on a new branch — you review + merge (never auto-merged). Codemap index is per-worktree, so parallel runs never race one index. Composes with `--team`. |
| `--no-codemap`        | Disable codemap even if available                                                                                                                                                                                              |
| `--codemap`           | Require Codemap and an index; stop when unavailable                                                                                                                                                                            |
| `--accept-no-plan`    | Skip inline plan generation for medium/large scope                                                                                                                                                                             |
| `--no-challenge`      | Skip challenger adversarial gate                                                                                                                                                                                               |
| `--challenge`         | Force challenger gate even on small change auto-skip would otherwise skip                                                                                                                                                      |
| `--keep "<items>"`    | Append items to compaction contract preserve field — keeps key context if auto-compaction fires mid-skill                                                                                                                      |

**Workflow**:

1. **Scope and understand** (`foundry:sw-engineer`): reads target code, maps public API surface, identifies complexity hotspots + coupling. Codemap for blast-radius when available. Scope gate: target spans 3+ modules, 5+ files, or any public-API rename → asks narrow or proceed.
2. **Audit test coverage**: classifies each public function covered / partially covered / uncovered. No `pytest-cov` installed → falls back to "all uncovered" conservatively.
3. **Add characterization tests** (`foundry:qa-specialist`): every uncovered/partial public API gets tests asserting *current* behavior (not desired). Gate: all characterization tests must pass on unmodified code before proceeding.
4. **Refactor with safety net**: one focused change per cycle, tests after each. Safety break: max 5 change-test cycles per inner session; max 10 total across all outer review cycles.
5. **Review and close gaps**: behavior preservation, goal achievement, no new smells, no unintended API surface changes. Max 3 outer review cycles.
6. **Quality stack**: available ruff/mypy checks → full test suite → optional Codemap blast-radius check → optional Codex pre-pass → progressive review loop.

**Refactoring categories skill handles**:

- Logic simplification: replace complex conditionals, flatten nesting, extract helpers
- API cleanup: rename for clarity, consolidate parameters, add type annotations
- Structural: extract classes/modules, reduce coupling, apply design patterns
- Performance: replace loops with vectorized ops, reduce allocations, batch I/O
- Dead code removal: unused imports, unreachable branches, unexported public methods

**Realistic example**:

```text
/develop:plan "extract data loading into a dedicated DataLoader class"
/develop:refactor "extract data loading into a dedicated DataLoader class" --plan .plans/active/plan_extract-data-loading-dataloader.md
```

**Checkpoint and resume**: creates `.developments/<timestamp>/checkpoint.md` after each step. Session interrupted → re-run offers resume from last completed step.

______________________________________________________________________

### `/develop:debug`

**Purpose**: Investigation-first debugging. Gathers all signals, traces failure path, forms single confirmed root-cause hypothesis, writes diagnosis file, hands off to `/develop:fix`.

**When to use**: symptom without confirmed root cause; bug mysterious enough to warrant structured investigation before fixing.

**Not for**: production incidents without CI run ID or traceback — use `/foundry:investigate` (requires `foundry` plugin); `.claude/` config issues — use `/foundry:audit` (requires `foundry` plugin). CI-only failures ARE supported — pass `--ci-run <run-id or URL>`.

**Invocation**:

```text
/develop:debug "<symptom description>"
/develop:debug 88                                   # GitHub issue number
/develop:debug 88 --repo owner/upstream-repo        # issue on upstream repo (fork workflow)
/develop:debug "<symptom>" --team                   # parallel hypothesis investigation
```

**Flags**:

| Flag                   | Description                                                                                                                                                                                                                                     |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--issue <N>`          | Force issue mode — fetch GitHub issue `<N>` instead of inferring mode from a bare numeric argument. Value form only; `--issue=123` is not supported by design (mode-detect matches the bare token).                                             |
| `--repo <owner/repo>`  | Route issue fetch to upstream repo. Use when working in fork and issue on original repo (e.g. `--repo owner/my-project`).                                                                                                                       |
| `--team`               | Spawn 2-3 `foundry:sw-engineer` teammates, each investigating distinct root-cause hypothesis independently. Use when root cause unclear after initial analysis, or failure spans 3+ modules                                                     |
| `--worktree`           | Run the investigation in an isolated git worktree (base: HEAD) so reproduction attempts never touch main sources. Diagnosis file is written to the **main tree** so `/develop:fix` can read it.                                                 |
| `--ci-run <id-or-url>` | Fetch CI failure logs via `gh run view <id> --log-failed` instead of running pytest locally. Accepts bare run ID or any GitHub Actions URL (`/actions/runs/<id>` or `/actions/runs/<id>/jobs/<job>`). Use for CI-only failures, no local repro. |
| `--codemap`            | Require Codemap and an index; stop when unavailable                                                                                                                                                                                             |
| `--no-codemap`         | Disable codemap even if available                                                                                                                                                                                                               |
| `--no-challenge`       | Skip challenger adversarial gate entirely                                                                                                                                                                                                       |
| `--challenge`          | Force challenger gate even on small change auto-skip would otherwise skip                                                                                                                                                                       |
| `--keep "<items>"`     | Append items to compaction contract preserve field — keeps key context if auto-compaction fires mid-skill                                                                                                                                       |

**Workflow**:

1. **Understand the symptom** (`foundry:sw-engineer`): reads full tracebacks, recent git changes near failing code, traces call path entry point → failure site. GitHub issue number → fetches full issue + comments (upstream if `--repo`).
2. **Pattern analysis**: finds 2-3 similar working code paths, compares exhaustively against broken path — input, environment, call order, conditional branches, None/empty guards.
3. **Hypothesis and gate**: states root cause explicitly with supporting + contradicting evidence and confidence (high / medium / low). Presents hypothesis, waits for confirmation before proceeding. Low confidence → targeted probe (minimal script, added assertion) for missing signal. Codemap enabled → one-time `test-impact` query on confirmed suspect module/function.
4. **Hand off to fix**: writes diagnosis file to `.plans/active/debug_<slug>.md`, emits `-> /develop:fix --diagnosis <path>`. Fix's Step 1 pre-answered by diagnosis. Test-impact result (with index timestamp) written under `## Test Impact (codemap-py)` section — `/develop:fix` reuses it (test-impact runs once across debug→fix flow), re-queries only if index moved or result stale.

**Debug is investigation-only** — no code changes. Fix happens in separate auditable session with own regression test gate.

**Realistic example**:

```text
/develop:debug "intermittent timeout on /api/predict under load"
# -> /develop:fix --diagnosis .plans/active/debug_intermittent-timeout-api-predict.md
```

**Team mode**: teammates independently investigate competing hypotheses, lead facilitates cross-challenge, synthesises consensus before handing off to fix.

______________________________________________________________________

### `/develop:review`

**Purpose**: Review local Python files or the current git diff across architecture, tests, performance, docs, static analysis, security, and API design. The classifier selects relevant dimensions, which fold into spawn units (performance+architecture share one agent, docs+lint share another; each merged unit still writes one findings file per dimension); the default fan-out is capped at three units, and `--full` runs every selected unit. No GitHub PR is required.

**When to use**: reviewing own changes before committing; structured feedback on local files; closing quality gaps before PR.

**Not for**: GitHub PR review — use `/oss:review <PR#>` (requires `oss` plugin); implementation work — use `/develop:feature` or `/develop:fix`.

**Scope**: Python source only. Non-Python files (YAML, Dockerfile, JSON, shell scripts) flagged in report header as "not reviewed" but presence noted — dependency/config changes can silently break reviewed Python code.

**Invocation**:

```text
/develop:review                          # review current git diff (staged + unstaged vs HEAD)
/develop:review src/mypackage/module.py  # review a specific file
/develop:review src/mypackage/           # review all Python files in a directory
```

**Flags**:

| Flag               | Description                                                                                                                                                                                    |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--no-challenge`   | Skip challenger adversarial gate                                                                                                                                                               |
| `--challenge`      | Force challenger gate even on small change auto-skip would otherwise skip                                                                                                                      |
| `--codemap`        | Require Codemap and an index; stop when unavailable                                                                                                                                            |
| `--no-codemap`     | Disable codemap even if available                                                                                                                                                              |
| `--semble`         | Enable semble semantic search context                                                                                                                                                          |
| `--worktree`       | Run the review in an isolated git worktree (base: HEAD) so no agent can mutate main sources. Report is written to the **main tree**; reviews committed HEAD (uncommitted changes not visible). |
| `--full`           | Run every spawn unit selected by classification instead of the default top-three-unit cap                                                                                                      |
| `--keep "<items>"` | Append items to compaction contract preserve field — keeps key context if auto-compaction fires mid-skill                                                                                      |

**Workflow**:

1. **Identify scope**: collects Python files from path or `git diff HEAD`. Classifies diff FIX / REFACTOR / FEATURE / CHORE / MIXED — skips optional agents for smaller diffs (FIX skips `foundry:perf-optimizer` + `foundry:solution-architect`; CHORE skips `foundry:qa-specialist`, `foundry:perf-optimizer`, `foundry:solution-architect`). Small diff (single file, \<50 lines, no new public API) also auto-skips `foundry:challenger` gate unless `--challenge` passed.
2. **Codex co-review** (if `bridge@borda-ai-rig` is installed and enabled): adversarial diff review seeds pre-flagged issues list for specialist agents.
3. **Selected parallel agents** (file-based handoff — each writes handover files to `.temp/review/<timestamp>/`; the default cap is three spawn units, `--full` removes that cap; merged units write one findings file per dimension):
   - `foundry:sw-engineer`: architecture, SOLID, type safety, error handling, Python anti-patterns, security for touched auth/input/data paths
   - `foundry:qa-specialist`: test coverage gaps, missing edge cases, ML non-determinism, seed pinning, boundary conditions
   - `foundry:perf-optimizer`: algorithmic complexity, loops that should be NumPy/torch ops, unnecessary I/O, ML DataLoader config (skipped for FIX diffs)
   - `foundry:doc-scribe`: public APIs without docstrings, Google-style section gaps, CHANGELOG entries, deprecated stdlib usage
   - `foundry:linting-expert`: ruff violations, mypy errors, type annotation gaps on public API, suppressed violations
   - `foundry:solution-architect`: API design quality, coupling, backward compatibility (only for public API boundary changes; skipped for REFACTOR and FIX)
4. **Cross-validate** critical + blocking findings using same agent type that raised each finding.
5. **Consolidate** (`foundry:sw-engineer`): reads all findings, deduplicates, ranks by impact, writes full report to `.reports/review/<timestamp>/review-report.md`. Signal-to-noise gate: small modules not padded with low-severity findings.
6. **Codex delegation** (optional): mechanical tasks — docstrings, missing tests for concrete scenarios, consistent renames — delegated to Codex when precise brief writable.

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
- Security findings → `/develop:fix`; run `pip-audit` if dependency files changed
- Mechanical issues (docstrings, missing tests) → when `bridge@borda-ai-rig` is available, construct an `implement` brief that states the exact finding, target paths, current evidence, permitted edits, required result, stop condition, and verification command.
- GitHub PR review for contributor → `/oss:review <PR#>` (requires `oss` plugin) instead

______________________________________________________________________

### `/develop:setup`

**Purpose**: Deliver this plugin's `rules/*.md` into Claude's user-level rule namespace. Maintenance command, not part of any development workflow.

**When to use**: after installing develop on a new machine, or after upgrading it. `make sync-claude` runs it automatically for every installed managed plugin that ships a setup skill, so a normal sync needs no manual step.

**Invocation**:

```text
/develop:setup            # interactive — asks before replacing anything it does not own
/develop:setup --approve  # non-interactive — used by make sync-claude
```

Each rule installs as a symlink at `~/.claude/rules/develop-<source-name>.md`. The `develop-` prefix keeps the flat rule namespace collision-free — four plugins ship a `rules/quality-gates.md`. A filename prefix does not change how Claude loads a rule or how its `paths:` frontmatter matches.

Only links this plugin provably owns are replaced or removed: the existing target must resolve under the current plugin root or under the same install-cache lineage. A real file, a link into another marketplace, a source checkout, or a dotfiles tree is reported as a conflict and left alone unless you approve replacing it.

______________________________________________________________________

## 🗺️ Workflow overview

Skills chain naturally. Typical session:

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

### Fork workflow (upstream issue)

Forked repo, want to fix/implement issue reported on original upstream. Pass `--repo <owner/repo>` to route issue fetch to upstream instead of fork:

```text
# Debug an upstream issue in your fork
/develop:debug 88 --repo owner/my-project

# Fix it — skip debug if root cause is clear
/develop:fix 88 --repo owner/my-project

# Implement a feature request from upstream
/develop:feature 123 --repo owner/my-project
```

`--repo` accepted by `fix`, `feature`, `debug`, `refactor`. Affects only `gh issue view` call fetching issue body + comments — rest of workflow operates on local fork as normal.

### Complex or high-stakes work

Add `--team` to any code-changing skill. Spawns parallel specialist agents exploring implementation space independently. Significantly higher token cost — reserve for multi-module changes, public API additions, auth/payment/data scope.

```text
/develop:feature "add streaming response support" --team
/develop:fix "memory leak in batch inference" --team
```

### Isolated runs — `--worktree`

Add `--worktree` to run the **entire** skill inside a fresh git worktree under `.claude/worktrees/` on a new branch **based off your current `HEAD`** (via `git worktree add HEAD` + the harness `EnterWorktree`/`ExitWorktree` tools — not `origin/<default>`). The main working tree is never touched; on completion the skill leaves the worktree + branch on disk and reports the path/branch — **you** review and merge (never auto-merged). Uncommitted working-tree changes do not transfer into a worktree — commit or stash first if the run must see them.

On entry a `--worktree` run also **reports** any leaked worktrees it could reclaim — clean, ≥14-day-old `agent-*`/`oss-*` trees, including directories git no longer has registered (`git worktree prune` cannot see those; it removes the inverse case). Nothing is deleted without an explicit answer to the prompt that follows. Trees holding uncommitted work are listed and kept at any age, and your own `dev-*` trees are never candidates.

Available on: `feature`, `fix`, `refactor` (all work stays in the worktree); and opt-in on the read-only `debug` + `review` (isolation guards sources, but the diagnosis/report deliverable is written to the **main tree** so downstream skills + your review can reach it). Not on `plan` (analysis-only, never edits).

```text
/develop:fix "off-by-one in token expiry" --worktree
/develop:refactor src/loader.py "extract batching" --worktree --team
```

- **Codemap alignment** — the index path is anchored to the git top-level (`<root>/.cache/codemap/<project>.json`), not to the session CWD, and a linked worktree is its own top-level, so the index still resolves per-worktree (`<worktree>/.cache/codemap/…`) — and resolves identically from any subdirectory inside it. Each run owns its own ephemeral index, so any number of parallel `--worktree` runs never share or race one index; `.cache/` is gitignored so a worktree index never merges back. After you merge the branch, the main index is flagged stale on the next prompt and refreshes once.
- **Composes with `--team`** — the orchestrator worktree is the integration point; `--team` teammates keep their own per-agent isolation and merge into the orchestrator's worktree branch.
- Not offered on `plan` because planning is analysis-only; `debug` and `review` also support `--worktree`, with their report/diagnosis deliverables written to the main tree as documented above.

______________________________________________________________________

## ⚙️ Configuration

### Dependencies by capability

| Dependency            | Required    | Unlocks                                                                                                                                                                                      |
| --------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `foundry` plugin      | recommended | Named specialist agents. Without it, agent-router falls back to `general-purpose` with role-description prompts; develop's own quality-stack files still load.                               |
| `oss` plugin          | optional    | Severity checklist for local review when available; use `/oss:review <PR#>` for contributor-facing GitHub PR review (requires `oss`).                                                        |
| `bridge@borda-ai-rig` | optional    | Read-only adversarial pre-pass and bounded mechanical follow-up when installed and enabled; skipped with an explicit status when absent or disabled.                                         |
| `codemap-py`          | optional    | Structural context such as callers, imports, test impact, and blast radius. Auto mode uses an available index; absent/stale indexes follow the Codemap gate, and `--no-codemap` disables it. |
| `semble` MCP          | optional    | Semantic-search companion enabled explicitly with `--semble`; preflight stops if the MCP server is not configured.                                                                           |
| `gh` CLI              | optional    | Fetches issue bodies for numeric issue arguments and GitHub Actions logs for `/develop:debug --ci-run`; required only for those paths.                                                       |

### Codemap-py behavior

In auto mode, Codemap-py is used when the plugin and a project index are available; no flag is needed for that default experience.

| State                                 | Behavior                                   |
| ------------------------------------- | ------------------------------------------ |
| Installed + current index + auto mode | Use Codemap context                        |
| Installed + no index + auto mode      | Gate A asks whether to build or continue   |
| Installed + stale index + auto mode   | Gate B asks whether to refresh or continue |
| Not installed + auto mode             | Continue without Codemap                   |
| Any unavailable state + `--codemap`   | Stop and report strict-mode failure        |
| Any state + `--no-codemap`            | Always disabled                            |

`--codemap` = **strict assertion** — useful in CI or to guarantee structural context always applied. `--no-codemap` skips codemap for specific run (e.g. non-Python submodule).

### Python tooling

The quality stack is shipped by `develop` and auto-detects the project's runner (`uv`, Poetry, tox, Make, or `python -m pytest`). Missing optional tools such as ruff or mypy are reported and skipped; the test runner remains the decisive verification step.

### Hooks

Hooks register automatically from `hooks/hooks.json` when the plugin is enabled — no `settings.json` edits needed:

| Hook                       | Event                                                                                                     | Behaviour                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| -------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `agent-router.js`          | `PreToolUse` (`Agent`)                                                                                    | Reroutes `Agent()` calls when the requested agent is not installed: exact match → semantic match → `general-purpose`.                                                                                                                                                                                                                                                                                                                                  |
| `sentinel-read-allow.js`   | (decision module, no longer registered as a hook of its own; called by `allow-dispatch.js` in rank order) | Auto-allows the pre-canned TMPDIR sentinel-read and `$(date +FMT)` idioms inside read-only commands, so skill bash blocks stop raising "Contains expansion" prompts. Everything else falls through to normal checks. Whole-line `# …` comments inside a block are skipped rather than blocking it, and `..` is matched as a path component so ellipsis and version ranges are not mistaken for traversal.                                              |
| `blueprint-allow.js`       | (decision module, no longer registered as a hook of its own; called by `allow-dispatch.js` in rank order) | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); normalizes the command, hashes it, and exact-matches it against this plugin's committed `blueprint-manifest.json`, so verbatim bash shipped in its own skills runs without a prompt. Any deviation from the blueprint text misses and falls through to a normal prompt.                                                                                        |
| `allow-dispatch.js`        | `PreToolUse` (`Bash`)                                                                                     | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); the only registered Bash auto-allow hook. Calls `blueprint-allow.js` then `sentinel-read-allow.js` as libraries, emits the first allow, and appends one audit record naming the effective verdict and both modules' own verdicts. Exits 0 on every path: an allow-only hook that crashes grants nothing and the call falls back to normal permission handling. |
| `audit-close.js`           | `PostToolUse`, `PostToolUseFailure` (`Bash`), `SessionStart`, `SessionEnd`                                | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); appends one record observing that a Bash call completed, and one per session boundary. Writes nothing to stdout on any event. Every installed plugin writes its own rows with nothing coordinating them.                                                                                                                                                       |
| `lib/audit-log.js`         | (shared module, not a hook)                                                                               | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); the shared record writer behind the two hooks above. Append-only — no lock, marker, state directory or deletion path — and never throws, so a logging failure cannot change a permission decision. `RIG_AUDIT=0` disables writing only.                                                                                                                        |
| `write-guard.js`           | `PreToolUse` (`Edit`, `Write`, `NotebookEdit`)                                                            | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); grants nothing — forces a confirmation on writes to `.github/**`, `CLAUDE.md`/`AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml`, `CHANGELOG.md`, dependency lockfiles and `pyproject.toml`. Source and tests are deliberately unprotected; everything else is silent passthrough.                                                               |
| `enforce-review-header.js` | `PreToolUse` (`AskUserQuestion`)                                                                          | Denies `/develop:review`'s follow-up question until the consolidated `review-report.md` exists, so the report header always reaches the terminal first. Silent unless a review run is in flight. Once the report exists, additionally nudges (never blocks) via `additionalContext` when the reply never rendered the header as a table — see `report-header-table.js`.                                                                                |
| `report-header-table.js`   | (shared module, not a hook)                                                                               | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); reads the session transcript to check whether the assistant's own reply, since the last human turn, rendered the report's `---` header as a `\| Field \| Value \|` table (or the documented `·`-fallback line) — catches the PR #1303 incident (raw YAML fields printed instead of a table) that the file-existence gate alone could not.                      |

### Artifact directories

Skills write to these dirs at project root (all gitignored):

| Directory                      | Contents                                                               |
| ------------------------------ | ---------------------------------------------------------------------- |
| `.plans/active/`               | Plan files from `/develop:plan`, diagnosis files from `/develop:debug` |
| `.developments/<timestamp>/`   | Checkpoint files for resumable feature/fix/refactor sessions           |
| `.temp/review/<timestamp>/`    | Per-agent handover files (intermediate) from `/develop:review`         |
| `.reports/review/<timestamp>/` | Consolidated final report from `/develop:review`                       |

Completed runs cleaned after 30 days. Interrupted runs (no `result.jsonl`) kept for debugging.

______________________________________________________________________

<details>

<summary>

## 🔍 Troubleshooting

</summary>

### "foundry plugin not installed — named-agent fallback"

Not installed → named-agent dispatch falls back to `general-purpose`; the develop-owned quality stack still runs. Install `foundry` when you want its specialist agents.

### A question is blocked with "develop:review report gate"

`enforce-review-header.js` denied an `AskUserQuestion` call because `.reports/review/<timestamp>/review-report.md` does not exist — the review reached agent launch but never consolidated its findings into a report. Finish the consolidation step and print the report `---` header; the question then goes through. The gate deactivates two hours after a run starts, so an aborted review never blocks later questions permanently. When no review is actually in flight and an aborted run simply left its sentinel behind, there is no need to wait out that window: the denial message names the sentinel file, so `rm -f`-ing that path and re-issuing the question clears the block immediately. Once the report exists, the hook also checks (via `report-header-table.js`) whether the printed reply actually rendered the header as a table — a missing table never blocks the question, but rides along as an `additionalContext` reminder naming Step 5b.

### Demo gate passes (exit 0) when it should fail

`/develop:feature` Step 2 confirms demo fails before implementation. Gate exits 0 → feature may already exist, or test tests wrong thing. Skill stops, asks revisit Step 1. Do not force past gate — means feature exists already or demo not testing intended contract.

### Regression test gate passes when it should fail

Same pattern in `/develop:fix` Step 2. Regression test passes on unfixed code → test not capturing bug. Revisit Step 1 — symptom description not pointing at actual failure site, or test exercises different code path.

### Characterization test fails on unmodified code

`/develop:refactor` Step 3: characterization tests must pass before refactoring begins. Characterization test fails → test wrong — must assert *current* behavior, not desired. Fix test to match what code actually does now.

### Session interrupted mid-skill

`feature`, `fix`, `refactor` write checkpoint file to `.developments/<timestamp>/checkpoint.md` after each major step. Re-running same skill command offers resume from last completed step.

### codemap-py query warnings appearing in output

`codemap-py` optional. `codemap-py` not on PATH → all codemap-py steps silently skipped. Plugin installed but index missing/stale → default (auto) mode prompts build/rebuild (Gate A/B); `--no-codemap` skips silently. Skill works fully without it. To enable codemap-py context, install the `codemap-py` plugin, then run `/codemap-py:scan-codebase` (requires `codemap-py` plugin).

______________________________________________________________________

</details>

## 🙏 Contributing / feedback

Plugin part of `borda-ai-rig` suite. Canonical source in `plugins/cc_develop/` within repository.

Report bug or suggest improvement: open issue in repository. Include skill name, invocation used, actual vs expected behavior.

**To update the plugin**:

```bash
claude plugin install develop@borda-ai-rig
```

**To uninstall**:

```bash
claude plugin uninstall develop
```

**Plugin structure**:

```text
plugins/cc_develop/
├── .claude-plugin/
│   ├── plugin.json              -- manifest (name, version, author)
│   ├── permissions-allow.json   -- tool calls the skills expect to be pre-approved
│   └── permissions-deny.json    -- operations that must stay denied (see below)
├── bin/
│   └── sync_rules.py            -- installs rules/*.md into ~/.claude/rules/
├── rules/
│   └── quality-gates.md         -- delivered as ~/.claude/rules/develop-quality-gates.md
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
    ├── review/
    │   └── SKILL.md
    └── setup/
        └── SKILL.md
```

**Permission manifests**: `.claude-plugin/permissions-allow.json` lists the tool calls the skills expect to be pre-approved. `.claude-plugin/permissions-deny.json` is its counterpart — the operations that must stay denied no matter how broad the allow list becomes: destructive shell and git commands (`rm -rf`, `sudo`, `ssh`, `chmod 777`, branch and tag deletion, force-push, `claude --dangerously-skip-permissions`) plus every public-GitHub write (`gh issue`/`pr`/`release`/`gist` create, edit, merge, delete, and `gh api` with `POST`, `PATCH`, `PUT` or `DELETE`). Both files are merged into `~/.claude/settings.json` by `/develop:setup` (Step 5) — additive and idempotent, nothing is ever removed. Deny entries are prefix matches, so they stop the documented command forms rather than every possible flag ordering.

<a id="bin-helper-inventory"></a>

<details>

<summary><strong>🧰 Bin helper inventory (22 shipped deterministic helpers)</strong></summary>

These helpers are installed workflow support and maintainer surfaces, not additional slash-command skills. The skills own the development workflow; the helpers handle bounded flag parsing, Codemap context, test execution, worktree setup, path resolution, and state extraction.

| Helper                       | Purpose                                                                      |
| ---------------------------- | ---------------------------------------------------------------------------- |
| `build_codemap_batch.py`     | Build one Codemap pre-flight batch for changed modules.                      |
| `codemap_resolve.py`         | Resolve Codemap auto, strict, or disabled mode.                              |
| `codemap_scan.py`            | Derive affected modules and emit structural Codemap queries.                 |
| `derive_codemap_target.py`   | Derive TARGET_MODULE/TARGET_FN from a skill goal.                            |
| `dev_codemap_gate.py`        | Normalize and persist Codemap mode for all six workflows.                    |
| `dev_issue_fetch_wrap.py`    | Fetch and persist upstream issue context for development skills.             |
| `dev_parse_args.py`          | Parse development-skill arguments into shell-safe assignments.               |
| `dev_run_dir.py`             | Create a timestamped `.developments/` run directory and optional sentinel.   |
| `dev_setup_worktree_wrap.py` | Set up team-mode worktree run directories and state.                         |
| `dev_shared_resolve.py`      | Resolve develop's own shared directory portably.                             |
| `diagnosis_parse.py`         | Parse and validate a `--diagnosis` path from arguments.                      |
| `extract-keep-flag.py`       | Parse `--keep "<items>"` and clear a stale compaction contract.              |
| `extract_json_field.py`      | Recover a JSON object from text and print a selected field.                  |
| `find-polluter.py`           | Binary-search test isolation contamination.                                  |
| `heal_git_artifacts.py`      | Reclaim stale skill locks and orphaned git worktrees.                        |
| `issue_fetch.py`             | Validate an issue argument and fetch it through `gh`.                        |
| `parse-skill-flags.py`       | Parse boolean and value skill flags into shell assignments.                  |
| `parse_target_qname.py`      | Split a `module::function` suspect out of a skill's arguments.               |
| `pytest_gate.py`             | Run an allow-listed pytest command with full output.                         |
| `resolve_review_target.py`   | Resolve a review target and its changed Python files.                        |
| `run_pytest_short.py`        | Run an allow-listed pytest command and show its final output lines.          |
| `setup_worktree.py`          | Create a team-mode `.temp/develop/` run directory and optional sentinel.     |
| `sync_rules.py`              | Install namespaced rule symlinks into `~/.claude/rules/`.                    |
| `verify_blueprint_audit.py`  | Verify and prune the auto-allow audit log.                                   |
| `write_skill_contract.py`    | Write the compaction-boundary contract the PreCompact hook appends verbatim. |

</details>

#### Auto-allow audit log

`allow-dispatch.js` and `audit-close.js` append one JSON line each per Bash call to `~/.claude/logs/audit/`: one file per session (`s-<key>.jsonl`, the key being a hash of the session id) plus a shared `_no-session.jsonl` for calls that arrive without one. A record names the deciding plugin and hook, the session and tool-call ids, the working directory, the effective decision with its lane and provenance, and both decision modules' own verdicts. Command text is never written — only a digest, and for a provenance allow the manifest `src` it matched. Note the limit honestly: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`, so the guarantee is about this file rather than about the log directory.

With several plugins installed each writes its own rows and nothing coordinates them, so one completed call yields one row per plugin at each end. That is corroboration, not duplication.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" verify
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" prune --older-than 30 --dry-run
```

`verify` exits 1 only when a record's own hash fails to verify; torn framing from concurrent appends and every classification are warnings. `record_hash` detects a record that changed after it was written — it is not tamper-evidence, because the same user owns the log, the hooks and the checker. Read the classifications as observations rather than proof: a call with a decision row and no completion row is reported as `completion-unobserved` when the session later ended and `in-flight-or-hard-kill` when it did not, and neither distinguishes a refusal at the prompt from a deny rule elsewhere or a closer that died. Equally, that a tool ran never establishes that a human approved it. `prune` is the only component that deletes a log file, is never run by a hook, and never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id. Nothing schedules `prune`. `RIG_AUDIT=0` disables audit writing without changing any permission decision.

Records follow the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), an active individual-submission Internet-Draft that is not endorsed by the IETF and has no standing in its standards process. Deliberate deviations: `outcome` adds `pending`, because a decision that has not executed yet has no outcome in the draft's vocabulary; `trust_level` is `plugin`/`unknown` rather than the draft's `L0`–`L4`, because what is observed is which component proposed an allow, not an authentication level; `agent_id` is `<plugin>/<hook>` rather than a URI; `session_id` is the host's id verbatim rather than a UUID; and `parent_record_id` and `prev_hash` are always null, because this log is not chained — lineage is derived on read instead.

**Uninstall leaves rule links behind**: Claude Code runs no cleanup hook on uninstall, so `~/.claude/rules/develop-*.md` survives both `claude plugin uninstall` and `make clear-all`. Delete those symlinks by hand — once the plugin cache version is gone they dangle.

Modify any skill → update this README before finishing — unsynced change = incomplete change.
