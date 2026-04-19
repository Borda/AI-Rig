**Re: Compress quality-stack.md to caveman format**

# Shared Quality Stack

Used by develop mode skills (feature, fix, refactor, debug). Invoked via `Read(".claude/skills/_shared/quality-stack.md")`.

Skip branch safety guard in `plan` mode — plan makes no code changes.

## Branch Safety Guard

```bash
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)                                                         # timeout: 3000
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@') # timeout: 3000
if [ "$CURRENT_BRANCH" = "$DEFAULT_BRANCH" ] || [ "$CURRENT_BRANCH" = "main" ] || [ "$CURRENT_BRANCH" = "master" ]; then
    echo "⚠ On default branch ($CURRENT_BRANCH) — create a feature branch before running /develop"
    exit 1
fi
```

Guard fires: stop, report branch name, ask user create feature branch.

## Quality Stack

Run after all mode-specific steps complete:

```bash
# Linting and formatting
uv run ruff check <changed_files> --fix  # timeout: 30000
uv run ruff format <changed_files>  # timeout: 30000

# Type checking
uv run mypy <changed_files> --no-error-summary 2>&1 | head -30  # timeout: 30000

# Full test suite
uv run pytest <test_dir> -v --tb=short -q  # timeout: 600000

# Doctests (if applicable)
uv run pytest --doctest-modules <target_module> -v 2>&1 | tail -20  # timeout: 600000
```

Spawn **foundry:linting-expert** agent if mypy or ruff issues need non-trivial fixes.

## Codex Pre-pass

Mandatory after quality stack. Degrades gracefully if Codex unavailable.

Read `.claude/skills/_shared/codex-prepass.md` and run Codex pre-pass on changes.

### Codex pre-pass: additional inline steps (step 1 is in the shared file)

2. **Collect findings**: build `CODEX_FINDINGS` — bullet list of every flagged issue from `codex:review` output. Nothing found or step skipped → set `CODEX_FINDINGS=""`. Review read-only — no working-tree changes.
3. **Actor context**: note whether Codex involved (found real issues acted on). Pass as context when committing — `git-commit.md` decides trailers.

Include `### Codex Pre-pass` section in final report:

- Available + findings: list what Codex flagged (become `CODEX_FINDINGS` seed)
- Available + no issues: "Codex pre-pass: no issues found"
- Skipped (unavailable): "codex plugin (openai-codex) not installed — pre-pass skipped"

## Progressive Review Loop

Max 3 cycles. Applied after quality stack.

**Cycle 1: Full review**

- Invoke `/oss:review` for full multi-agent code review. `CODEX_FINDINGS` non-empty → prepend to review brief: "Codex pre-pass found the following — verify these, do not rediscover: $CODEX_FINDINGS"
- Capture review state: `{agents_with_findings, unresolved_findings, files_reviewed}`
- Clean (no critical/high findings): skip to report

**Cycle 2: Targeted re-check**

- Fix critical/high findings from Cycle 1
- Re-run quality stack on modified files only
- Set up run dir for file-based handoff: `RUN_DIR=".developments/$(date -u +%Y-%m-%dT%H-%M-%SZ)"; mkdir -p "$RUN_DIR"`
- For each agent type in `agents_with_findings`: spawn directly (not `/oss:review`) with focused prompt scoped to modified files + prior findings. Each agent prompt must end with: "Write your full findings to `$RUN_DIR/<agent-name>.md` using the Write tool. Return ONLY a compact JSON envelope: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.N,\"summary\":\"<agent-name>: N critical, N high\"}`"

Replace bare agent names in spawn prompts with `foundry:` prefixed equivalents: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`.

**Health monitoring** (CLAUDE.md SS8): after spawning, create checkpoint:

```bash
DEVELOP_CHECKPOINT="/tmp/develop-check-$(date +%s)" # timeout: 5000
touch "$DEVELOP_CHECKPOINT"                         # timeout: 5000
```

Every 5 min:

```bash
COUNT=$(find "$RUN_DIR" -newer "$DEVELOP_CHECKPOINT" -type f | wc -l) # timeout: 5000
touch "$DEVELOP_CHECKPOINT"                                           # refresh — next poll detects only new writes, not old ones  # timeout: 5000
```

`COUNT == 0` → stalled; `COUNT > 0` → alive. Hard cutoff: 15 min COUNT == 0 → timed out.

- Skip agents clean in Cycle 1
- Collect envelopes to update review state (don't read full finding files into context — check envelopes to determine if critical/high remain)

**Cycle 3: Minimal verification**

- Fix remaining critical/high findings
- Re-run quality stack only (no agents)
- Clean: proceed to report
- Still failing: stop, present findings to user — no further looping

**Context optimization between cycles**:

- Context usage high → write review state to `.claude/state/develop-review-state.md` before compaction:
  ```
  # Develop Review State
  cycle: <N>
  resolved: [list]
  unresolved: [list]
  files_modified: [list]
  agents_with_issues: [list]
  ```
- After compaction, read back to resume at correct cycle
- Delete file when review loop completes

## Codex Mechanical Delegation

Read `.claude/skills/_shared/codex-delegation.md` and apply delegation criteria. Delegate mechanical follow-up tasks to Codex when accurate specific brief writable.

Distinct from Codex pre-pass above — pre-pass checks implementation diff for correctness; mechanical delegation outsources low-level follow-up work (scaffolding, boilerplate, migration scripts, etc.) after review loop closes.

Include `### Codex Delegation` section in final report only when tasks actually delegated — omit entirely if nothing delegated.
