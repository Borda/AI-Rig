**Re: Compress quality-stack.md to caveman format**

# Shared Quality Stack

Used by develop mode skills (feature, fix, refactor). Invoked via `Read(".claude/skills/_shared/quality-stack.md")`.

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

Run after all mode-specific steps complete.

**Tool detection** — run once, reuse throughout:

```bash
# Detect runner
if command -v uv >/dev/null 2>&1; then RUNNER="uv run"
else RUNNER="python -m"; fi

# Verify ruff available
SKIP_RUFF=0
if ! $RUNNER ruff --version >/dev/null 2>&1; then
    echo "WARNING: ruff not available — skipping lint/format steps"
    SKIP_RUFF=1
fi

# Verify mypy available
SKIP_MYPY=0
if ! $RUNNER mypy --version >/dev/null 2>&1; then
    echo "WARNING: mypy not available — skipping type check step"
    SKIP_MYPY=1
fi
```

```bash
# Linting and formatting (skip if SKIP_RUFF=1)
[ "${SKIP_RUFF:-0}" -ne 1 ] && $RUNNER ruff check <changed_files> --fix  # timeout: 30000
[ "${SKIP_RUFF:-0}" -ne 1 ] && $RUNNER ruff format <changed_files>  # timeout: 30000

# Type checking (skip if SKIP_MYPY=1)
[ "${SKIP_MYPY:-0}" -ne 1 ] && { $RUNNER mypy <changed_files> --no-error-summary 2>&1 | head -30; MYPY_EXIT=${PIPESTATUS[0]}; }  # timeout: 30000
# Note: MYPY_EXIT captures mypy's exit code (non-zero = type errors found)

# Full test suite
$RUNNER pytest <test_dir> -v --tb=short  # timeout: 600000
SUITE_EXIT=$?

# Flaky test detection — if suite failed, retry twice
if [ $SUITE_EXIT -ne 0 ]; then
    PASS_COUNT=0
    for _i in 2 3; do
        $RUNNER pytest <test_dir> -v --tb=short 2>&1 | tail -5  # timeout: 600000
        [ ${PIPESTATUS[0]} -eq 0 ] && PASS_COUNT=$((PASS_COUNT + 1))
    done
    if [ $PASS_COUNT -gt 0 ]; then
        echo "⚠ FLAKY: test(s) passed $PASS_COUNT/2 retries — flag as flaky, do not block"
    else
        echo "✗ GENUINE FAILURE: test(s) failed all 3 runs"
        echo "Quality stack halted — do not proceed to doctests, Codex pre-pass, or review loop"
        exit 1
    fi
fi

# Doctests (if applicable)
$RUNNER pytest --doctest-modules <target_module> -v 2>&1 | tail -20  # timeout: 600000
DOCTEST_EXIT=${PIPESTATUS[0]}
# Note: DOCTEST_EXIT captures pytest exit code (non-zero = doctest failures)
```

Spawn **foundry:linting-expert** agent if mypy or ruff issues need non-trivial fixes.

**Post-change blast radius** (if codemap installed — soft check):

```bash
if command -v scan-query >/dev/null 2>&1; then
    # For each modified public function/class, check reverse dependencies
    # Derive <module> from changed file path (strip src/ prefix, replace / with ., drop .py)
    scan-query rdeps <module> 2>/dev/null | head -20
    echo "^ review rdeps — changes here may affect callers"
fi
```

## Recovery

When quality stack fails (tests, lint, or type check), choose rollback depth based on scope:

1. **Targeted revert** — single file broke: `git checkout HEAD -- <file>` then re-run stack on remaining files — **confirm with user before running**; discards all uncommitted changes in that file (destructive)
2. **Partial revert** — feature branch has mixed good/bad commits: `git revert <bad-commit>` (preserves history)
3. **Full revert** — nothing salvageable: `git reset --hard <last-clean-sha>` — **confirm with user before running**; destructive

Document which option was used in Final Report under "Recovery" subsection.

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

```bash
# Check oss plugin availability — skip Progressive Review Loop if absent
if ! claude plugin list 2>/dev/null | grep -q 'oss@'; then  # timeout: 15000
    echo "oss plugin not installed — Progressive Review Loop skipped; proceeding to Codex Mechanical Delegation"
    # Skip all 3 cycles
fi
```

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

**Health monitoring**: Agent calls are synchronous — framework awaits each response natively. No Bash polling is possible during an active Agent call. If an agent does not return within 15 min: use Read tool on `$RUN_DIR/<agent-name>.md` to surface partial results. Mark timed-out agents with ⏱ in final report.

- Skip agents clean in Cycle 1
- Collect envelopes to update review state (don't read full finding files into context — check envelopes to determine if critical/high remain)

**Cycle 3: Minimal verification**

- Fix remaining critical/high findings
- Re-run quality stack only (no agents)
- Clean: proceed to report
- Still failing: stop, present findings to user — no further looping

**Context optimization between cycles**:

- Context usage high → write review state to `.claude/state/develop-review-state.md` before compaction:
  ```markdown
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
