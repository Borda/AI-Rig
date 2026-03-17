---
name: develop
description: Unified development orchestrator with three modes — feature (TDD-first new capability), fix (reproduce-first bug resolution), refactor (test-first code quality). Each mode includes a built-in self-review gate before the shared quality stack and progressive review loop.
argument-hint: feature|fix|refactor <description or issue #> ["target"]
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate
---

<objective>

Implement software changes with disciplined, test-driven workflows. The mode determines the entry contract:

- **feature**: TDD-first — validate demo → implement → review+fix loop (max 3 cycles) → doc → quality stack + review
- **fix**: reproduce-first — validate reproduction → apply minimal fix → review+fix loop (max 3 cycles) → quality stack + review
- **refactor**: test-first — validate coverage audit → characterization tests → refactor → review+fix loop (max 3 cycles) → quality stack + review

Each mode includes two layers of built-in review before the shared quality stack:

1. **Step 2 review** — validate the artifact (demo, regression test, or coverage audit) before writing any implementation
2. **Step 4/5 review+fix loop** — review the implementation, fix all gaps, loop (max 3 cycles) until only nits remain

Both layers are in-context (no agent spawn). They catch design mismatches, scope creep, and missing edge cases early so `/review` validates rather than discovers.

All modes share a quality stack and a progressive review loop that narrows scope across cycles.

</objective>

<inputs>

- **$ARGUMENTS**: required
  - First token: `feature`, `fix`, or `refactor`
  - Remaining tokens: mode-specific arguments (description, issue #, file path, etc.)
  - `--team` flag (anywhere in arguments): triggers team mode for the active mode

Examples:

- `/develop feature "add batched predict() method to Classifier" "src/classifier"`
- `/develop fix 123`
- `/develop fix "TypeError when passing None to transform()"`
- `/develop refactor src/transforms.py "simplify error handling"`
- `/develop feature --team "add authentication flow"`

</inputs>

<workflow>

**Task tracking**: immediately after Step 0 (mode is known), create TaskCreate entries for **all steps of the chosen mode's workflow** before doing any other work. This gives the user an instant view of the full plan. Mark each step in_progress when starting it, completed when done. On loop retry or scope change, create a new task or rename the existing one with TaskUpdate.

## Step 0: Parse mode

Extract the first token from `$ARGUMENTS` as the mode. Valid values: `feature`, `fix`, `refactor`.

If the first token is not a valid mode, stop and present the usage:

```
Usage: /develop <mode> <description>
Modes: feature, fix, refactor
```

Strip the mode token from arguments — the remainder is passed to mode-specific steps.

Detect `--team` flag: if present anywhere in arguments, enable team mode (see ## Team Mode section).

## Step 1: Scope analysis

Read `.claude/skills/develop/modes/<mode>.md` and run its **## Step 1** instructions.

This step produces a scope analysis that is used by all subsequent steps.

**Gate**: if the mode file's Step 1 flags a complexity smell (feature: 8+ files / 2+ new classes; fix: root cause spans 3+ modules; refactor: directory-wide scope without explicit goal), present the scope concern to the user before proceeding.

## Step 2: Mode-specific steps

Read `.claude/skills/develop/modes/<mode>.md` and execute its steps in order (Step 2 onward).

The mode file defines all steps specific to that workflow (TDD loop, regression test, characterization tests, etc.).

## Shared Step: Quality stack

Run after all mode-specific steps complete:

```bash
# Linting and formatting
uv run ruff check <changed_files> --fix
uv run ruff format <changed_files>

# Type checking
uv run mypy <changed_files> --no-error-summary 2>&1 | head -30

# Full test suite
python -m pytest <test_dir> -v --tb=short -q

# Doctests (if applicable)
python -m pytest --doctest-modules <target_module> -v 2>&1 | tail -20
```

Spawn a **linting-expert** agent if mypy or ruff issues require non-trivial fixes.

## Shared Step: Progressive review loop

Maximum 3 cycles. Applied after the quality stack.

**Cycle 1: Full review**

- Invoke `/review` for a full multi-agent code review
- Capture review state: `{agents_with_findings, unresolved_findings, files_reviewed}`
- If clean (no critical/high findings): skip to report

**Cycle 2: Targeted re-check**

- Fix critical/high findings from Cycle 1
- Re-run quality stack on modified files only
- For each agent type in `agents_with_findings`: spawn that agent directly (not `/review`) with a focused prompt scoped to modified files + prior findings
- Skip agents that were clean in Cycle 1
- Update review state

**Cycle 3: Minimal verification**

- Fix remaining critical/high findings
- Re-run quality stack only (no agents)
- If clean: proceed to report
- If still failing: stop and present findings to user — do not loop further

**Context optimization between cycles**:

- If context usage is high, write review state to `.claude/state/develop-review-state.md` before any compaction:
  ```
  # Develop Review State
  cycle: <N>
  resolved: [list]
  unresolved: [list]
  files_modified: [list]
  agents_with_issues: [list]
  ```
- After compaction, read it back to resume at the correct cycle
- Delete the file when the review loop completes

## Shared Step: Codex delegation (optional)

Read `.claude/skills/_shared/codex-delegation.md` and apply the delegation criteria. Delegate mechanical follow-up to Codex when an accurate, specific brief can be written.

Only include a `### Codex Delegation` section in the final report when tasks were actually delegated — omit entirely if nothing was delegated.

## Shared Step: Final report

The report format is defined in the mode file's `## Final Report` section. Use that template, then end with a `## Confidence` block per CLAUDE.md output standards.

## Team Mode (--team)

Detect `--team` flag in `$ARGUMENTS`. Each mode file defines its team assignments in a `## Team Assignments` section.

**Shared protocol:**

1. Lead performs Step 1 (scope analysis) in its own context
2. Lead reads the mode file's `## Team Assignments` to determine teammate roles and responsibilities
3. Spawn teammates per CLAUDE.md team rules:
   - Reasoning (sw-engineer, qa-specialist): model = `opus`
   - Execution (doc-scribe, linting-expert): model = `sonnet`
   - Max 3–5 teammates
4. Every spawn prompt includes:
   `Read .claude/TEAM_PROTOCOL.md — use AgentSpeak v2`
   - compact instructions (preserve: file paths, errors, test results, task IDs; discard: verbose tool output, handshakes)
   - Step 1 analysis broadcast to all teammates
5. Lead coordinates outputs, then runs shared quality stack + progressive review loop
6. Team shutdown via `SendMessage shutdown_request` after the final report

**When to trigger team mode (per mode):**

- feature: spans 3+ modules, OR changes public API, OR auth/payment/data scope
- fix: root cause unclear after Step 1, OR bug spans 3+ modules
- refactor: target is a directory OR cross-module scope

</workflow>

<notes>

- **Mode determines the entry contract** — use `feature` for net-new capability, `fix` for bugs, `refactor` for quality/structure improvements
- **Quality stack runs once** — it is shared across all modes; never skip it
- **Review loop is bounded** — after 3 cycles without a clean review, stop and re-scope with the user; do not retry indefinitely
- **`disable-model-invocation: true`** — you must type `/develop <mode> <description>` explicitly; once invoked, the parent model executes all workflow steps
- Related agents: `sw-engineer` (analysis + implementation), `qa-specialist` (tests + security), `doc-scribe` (documentation), `linting-expert` (type safety + style)
- Follow-up chains:
  - Feature changes public API → `/release` to prepare CHANGELOG + migration guide
  - Feature/fix is performance-sensitive → `/optimize` for baseline + bottleneck analysis
  - Any mode touches `.claude/` config files → spawn `self-mentor` on changed files, then `/sync` to propagate
  - Mechanical follow-up beyond Codex step → `/codex` to delegate additional tasks
  - External validation → `/review` if an independent multi-agent review is desired beyond the built-in self-review gates

</notes>
