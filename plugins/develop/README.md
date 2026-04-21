# 🛠️ develop — Claude Code Plugin

Development workflow plugin: six slash-command skills for scope planning, feature development, bug fixing, refactoring, debugging, and code review — all built on a validate-first principle that proves the problem exists before writing a single line of solution. All code-changing skills include Anti-Rationalizations tables — common shortcuts that produce wrong outcomes.

> [!NOTE]
>
> Works standalone — foundry is not required. Without it, agent dispatches fall back to `general-purpose` with role descriptions (lower quality). Installing foundry unlocks specialized agents (`foundry:sw-engineer`, `foundry:qa-specialist`, etc.) and is strongly recommended for production use.

<details>
<summary><strong>📋 Contents</strong></summary>

- [Why](#-why)
- [Key Principles](#-key-principles)
- [Quick start](#-quick-start)
- [How to Use](#-how-to-use)
- [Overview](#-overview)
- [Dependencies](#dependencies)
- [Plugin details](#-plugin-details)

</details>

## 🎯 Why

Most development mistakes happen before the first keystroke: implementing a feature nobody verified is needed, fixing a bug by guessing rather than reproducing it, refactoring without a safety net that catches regressions. The cost is not the wrong code — it's the downstream review, the revert, the second PR.

`develop` enforces a validation gate before every code change:

- **Plan mode** scopes the work before committing to it — analyses the codebase, estimates complexity, surfaces hidden dependencies, produces a structured plan validated by Codex
- **Feature mode** writes a failing demo test first — a TDD contract that pins the expected behaviour before any implementation begins; if you cannot write the test, the feature is underspecified
- **Fix mode** reproduces the bug with a failing regression test first — if you cannot reproduce it, you cannot verify the fix; the test becomes permanent protection against the same regression
- **Refactor mode** builds a characterization test suite first — tests that document what the code already does, creating a safety net that catches any behaviour change during restructuring
- **Debug mode** investigates before proposing — reads logs, traces call paths, forms a single hypothesis with confidence level (high/medium/low); no blind guesses
- **Review mode** runs six specialist agents across architecture, tests, performance, docs, lint, and security — against local files or the current git diff; Python-only (non-Python files flagged but not reviewed); no GitHub PR required

> [!IMPORTANT]
>
> Every code-changing mode (feature, fix, refactor) closes with the same quality stack: tool detection → lint/format (ruff) → type check (mypy) → test suite with flaky retry → blast-radius check (codemap) → Codex pre-pass → progressive review loop → Codex delegation. `/develop:review` is the quality gate itself — use it to review the current diff before committing.

## 💡 Key Principles

- **Validate before implementing** — demo test (feature), regression test (fix), characterization test (refactor); no production code before the validation artifact exists
- **Reproduce before fixing** — a fix without a failing test is a guess; the test is the proof the fix is correct and stays correct
- **Minimal change** — fix mode applies the smallest change that makes the regression test pass; no opportunistic cleanup, no adjacent improvements
- **No adjacent bug fixing** — if a different bug is discovered during a fix, it is documented as an observation and handled in a separate session; one fix per session prevents conflated history
- **Quality stack is non-negotiable** — lint/format → type check → test suite → blast-radius → Codex pre-pass → review loop runs on every code-changing mode completion; cannot be skipped

## ⚡ Quick start

```bash
# Run from the directory that CONTAINS your Borda-AI-Rig clone
claude plugin marketplace add ./Borda-AI-Rig
claude plugin install develop@borda-ai-rig
```

<details>
<summary>Install the full suite</summary>

```bash
claude plugin install foundry@borda-ai-rig   # base agents — strongly recommended
claude plugin install oss@borda-ai-rig
claude plugin install develop@borda-ai-rig
claude plugin install research@borda-ai-rig
```

</details>

> [!NOTE]
>
> Skills are always invoked with the `develop:` prefix: `/develop:plan`, `/develop:feature`, `/develop:fix`, `/develop:refactor`, `/develop:debug`, `/develop:review`.

## 🔁 How to Use

### Scope a large change before committing

```bash
/develop:plan "migrate auth from session tokens to JWTs"
```

### Implement a new feature

```bash
/develop:plan "add CSV export to the results API"   # scope it first
/develop:feature "add CSV export to the results API"
```

### Fix a reported bug

```bash
/develop:fix "KeyError in transform pipeline when input has null values"
/develop:fix 88                    # fix by GitHub issue number
```

### Refactor safely

```bash
/develop:refactor "extract data loading into a dedicated DataLoader class"
```

### Investigate a mystery failure

```bash
/develop:debug "intermittent timeout on /api/predict under load"
```

### Review current changes

```bash
/develop:review                          # review current git diff (staged + unstaged)
/develop:review src/mypackage/module.py  # review a specific file
/develop:review src/mypackage/           # review all Python files in a directory
```

### Team mode (parallel agents on the same task)

```bash
/develop:feature "add streaming response support" --team
/develop:fix "memory leak in batch inference" --team
/develop:refactor "extract data loading into DataLoader class" --team
/develop:debug "intermittent timeout on /api/predict under load" --team
```

> [!NOTE]
>
> `--team` spawns multiple `sw-engineer` + `qa-specialist` instances exploring the implementation space in parallel. Significantly higher token cost — use for complex, high-stakes tasks only.

## 🗺️ Overview

### 6 Development Modes

| Mode         | What It Solves                                                                               |
| ------------ | -------------------------------------------------------------------------------------------- |
| **plan**     | Scope analysis — codebase mapping, complexity estimate, hidden dependencies, structured plan |
| **feature**  | TDD-first implementation with review+fix loop — builds demo test before any production code  |
| **fix**      | Reproduce-first bug fixing — regression test must fail before the fix is written             |
| **refactor** | Test-first restructuring — characterization tests locked in before any production code moves |
| **debug**    | Investigation-first diagnosis — single hypothesis with confidence gate before fix handoff    |
| **review**   | Six-agent parallel review of local Python files or current git diff; no GitHub PR needed     |

### Orchestration Flows

> [!NOTE]
>
> These flows document the skill implementations. If any divergence exists between this section and the skill files, the skill files are authoritative.

<details>
<summary><strong>`/develop:plan`</strong> — scope before commit</summary>

```
Step 1: sw-engineer (codebase analysis — classify task type, map affected files, assess complexity, list risks)
Step 2: structured plan written to .plans/active/
Step 3: parallel feasibility review — sw-engineer + qa-specialist + linting-expert (per classification);
        internal resolution loop — agents assess feasibility (JSON parse-failure fallback);
        marks resolved/unresolved in plan file; escalates only what cannot be resolved autonomously
→ Structured plan with phases, estimated effort, identified blockers, and agent-validated design
No quality stack — plan makes no code changes.
```

</details>

<details>
<summary><strong>`/develop:feature`</strong> — TDD contract first</summary>

```
Step 1: sw-engineer (codebase analysis — understand existing patterns, reuse opportunities, risks)
Step 1.5: verify assumptions via WebFetch — library docs, API specs
          (WebFetch failure → skip, note in report)
Step 2: sw-engineer (demo test — failing test that defines feature contract)
        assumption gate — exit-code check; if already passing, feature may already exist
Step 2 review: in-context validation gate — test must be runnable and meaningful
Step 3: sw-engineer (TDD implementation loop; in --team mode, qa-specialist runs parallel review)
Step 4: review+fix loop (max 3 cycles) — 5-axis quality scan
Step 5: doc-scribe (docs update — docstrings, API references)
Quality stack: tool detection → ruff → mypy → test suite → blast-radius → Codex → review loop
```

</details>

<details>
<summary><strong>`/develop:fix`</strong> — reproduce before fixing</summary>

```
Step 1: sw-engineer (root cause analysis — read logs, trace failure path, identify blast radius)
Step 2: qa-specialist (regression test that fails on unfixed code — proves bug exists)
Step 2 review: in-context validation gate — test must actually fail
Step 3: sw-engineer (minimal fix — smallest change that makes regression test pass)
Step 4: review+fix loop (max 3 cycles) — 5-axis quality scan
Quality stack: tool detection → ruff → mypy → test suite → blast-radius → Codex → review loop
NOT for: unknown failures without reproduction test.
```

</details>

<details>
<summary><strong>`/develop:refactor`</strong> — safety net first</summary>

```
Step 1: sw-engineer (scope and understand — read target code, map public API, identify complexity hotspots)
Step 2: sw-engineer (audit test coverage — classify each public function: covered/partial/uncovered)
Step 2 review: validate coverage audit — completeness, classification accuracy, refactor relevance
Step 3: qa-specialist (characterization tests for uncovered/partial APIs — must pass on unmodified code)
Step 4: sw-engineer (refactor with safety net — max 5 change-test cycles; all tests must stay green)
Step 5: review+fix loop (max 3 outer cycles, return to Step 4 for targeted fixes)
Quality stack: tool detection → ruff → mypy → test suite → blast-radius → Codex → review loop
```

</details>

<details>
<summary><strong>`/develop:debug`</strong> — investigation before prescription</summary>

```
Step 1: sw-engineer (gather signals — logs, tracebacks, failing tests, recent git changes)
Step 2: sw-engineer (pattern analysis — compare broken path against 2-3 similar working paths)
Step 3: sw-engineer (form SINGLE hypothesis with confidence: high/medium/low)
        Gate: present hypothesis to user; low confidence → targeted probe before proceeding
Step 4: write diagnosis to .plans/active/ → hand off to /develop:fix --diagnosis <path>
No quality stack — investigation-only, no code changes.
NOT for: production incidents without local reproduction (use /foundry:investigate).
```

In team mode, 2-3 sw-engineer teammates each investigate distinct hypotheses independently; lead facilitates cross-challenge, synthesises consensus root cause.

</details>

<details>
<summary><strong>`/develop:review`</strong> — six-agent parallel review</summary>

```
Step 1: identify scope (path given → use it; omitted → git diff HEAD — staged + unstaged)
        Python-only. Non-Python files (Dockerfile, YAML, pyproject.toml) flagged but not reviewed.
        Classify diff: FIX / REFACTOR / FEATURE / MIXED — skip optional agents per classification.
Step 2: Codex co-review (adversarial diff review — seed list of pre-flagged issues for agents)
Step 3: 6 parallel agents via file-based handoff:
        sw-engineer (architecture, SOLID, error paths, security augmentation)
        qa-specialist (test coverage, missing edge cases, ML non-determinism)
        perf-optimizer (algorithmic complexity, Python→NumPy, I/O)
        doc-scribe (docstrings, README, CHANGELOG)
        linting-expert (ruff, mypy, type annotations)
        solution-architect (optional — API design, coupling, backward compat)
Step 4: cross-validate critical/blocking findings
Step 5: sw-engineer consolidator reads all agent findings → ranked report
Step 6: optional Codex delegation for mechanical fixes (docstrings, missing tests)
NOT for: GitHub PR review (use /oss:review <PR#>); implementation (use /develop:feature or /develop:fix).
```

</details>

### Quality Stack (code-changing modes only: feature, fix, refactor)

NOT applied to: plan (no code changes), debug (investigation-only), review (read-only gate itself).

Runs after all mode-specific steps complete:

1. **Tool detection** — detect uv/ruff/mypy availability once; reuse throughout
2. **Lint/format** — `ruff check --fix` + `ruff format` on changed files
3. **Type check** — `mypy` on changed files
4. **Test suite** — full pytest run with flaky retry (fail → 2 retries; pass on retry → flag flaky, do not block)
5. **Blast-radius check** — codemap `scan-query rdeps` on modified public functions (if codemap installed)
6. **Codex pre-pass** — independent adversarial diff review (mandatory if codex installed; gracefully skipped if absent)
7. **Progressive review loop** — max 3 cycles: full `/oss:review` → targeted re-check → minimal verification
8. **Codex mechanical delegation** — delegate docstrings, missing tests, consistent renames to Codex

Requires foundry plugin. Falls back gracefully if uv/ruff/mypy unavailable.

Skills create and track tasks during execution — visible in task list as live progress feed.

## Dependencies

| Dependency         | Required    | Used for                                                                                                                                                                                                                                                            |
| ------------------ | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **foundry plugin** | recommended | `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe` agents + `quality-stack.md` shared file. Without foundry, skills fall back to `general-purpose` agents with role-description prompts — functional but lower quality. |
| **oss plugin**     | optional    | `/oss:review` used in Progressive Review Loop (quality stack); `oss:review` checklist used by `/develop:review` Agent 1. Absent = review loop step skipped gracefully.                                                                                              |
| **codex plugin**   | optional    | `codex:review` for Codex pre-pass; `codex:codex-rescue` for plan design review and mechanical delegation. Gracefully skipped if absent.                                                                                                                             |
| **codemap**        | optional    | `scan-query` for blast-radius check (quality stack) and structural context in refactor/plan/review. Silently skipped if absent.                                                                                                                                     |
| **gh CLI**         | optional    | Used in debug/fix for `gh issue view` when argument is a GitHub issue number.                                                                                                                                                                                       |

## 📦 Plugin details

### Upgrade

```bash
cd Borda-AI-Rig
git pull
claude plugin install develop@borda-ai-rig
```

### Uninstall

```bash
claude plugin uninstall develop
```

### Structure

```
plugins/develop/
├── .claude-plugin/
│   └── plugin.json          ← manifest
└── skills/
    ├── plan/
    ├── feature/
    ├── fix/
    ├── refactor/
    ├── debug/
    └── review/
```
