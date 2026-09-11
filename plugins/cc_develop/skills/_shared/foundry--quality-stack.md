# Shared Quality Stack

Used by develop mode skills (feature, fix, refactor). Canonical home: cc_foundry `_shared/quality-stack.md`; consumer plugins ship the propagated copy as `foundry--quality-stack.md` (source-plugin prefix — a plugin-local file can never collide with a propagated copy) and load it via `cat "$_SHARED/foundry--quality-stack.md"` (not the Read tool — `Bash(cat:*)` grant is version-proof).

> `$_SHARED` = **the loading plugin's own** `skills/_shared`, set by the consumer before `cat`-ing this file. This doc is a byte-identical `propagate_shared.py` copy present in every plugin that uses it, so it must never name a specific plugin's variable — the sibling files it loads below resolve out of whichever `_shared` the consumer set.

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
if command -v uv >/dev/null 2>&1; then RUNNER="uv run"
else RUNNER="python -m"; fi

SKIP_RUFF=0
if ! $RUNNER ruff --version >/dev/null 2>&1; then
    echo "WARNING: ruff not available — skipping lint/format steps"
    SKIP_RUFF=1
fi

SKIP_MYPY=0
if ! $RUNNER mypy --version >/dev/null 2>&1; then
    echo "WARNING: mypy not available — skipping type check step"
    SKIP_MYPY=1
fi
```

```bash
[ "${SKIP_RUFF:-0}" -ne 1 ] && $RUNNER ruff check <changed_files> --fix  # timeout: 30000
[ "${SKIP_RUFF:-0}" -ne 1 ] && $RUNNER ruff format <changed_files>  # timeout: 30000

[ "${SKIP_MYPY:-0}" -ne 1 ] && { $RUNNER mypy <changed_files> --no-error-summary 2>&1 | head -30; MYPY_EXIT=${PIPESTATUS[0]}; }  # timeout: 30000
# non-zero = type errors

$RUNNER pytest <test_dir> -v --tb=short  # timeout: 600000
SUITE_EXIT=$?

# flaky detection — retry twice on failure
RETRY_COUNT=2
if [ $SUITE_EXIT -ne 0 ]; then
    PASS_COUNT=0
    for _i in 2 3; do
        $RUNNER pytest <test_dir> -v --tb=short 2>&1 | tail -5  # timeout: 600000
        [ ${PIPESTATUS[0]} -eq 0 ] && PASS_COUNT=$((PASS_COUNT + 1))
    done
    if [ $PASS_COUNT -lt $RETRY_COUNT ] && [ $PASS_COUNT -gt 0 ]; then
        echo "⚠ FLAKY: test(s) passed $PASS_COUNT/$RETRY_COUNT retries"
    elif [ $PASS_COUNT -eq 0 ]; then
        echo "✗ GENUINE FAILURE: test(s) failed all 3 runs"
        echo "Quality stack halted — do not proceed to doctests, Codex pre-pass, or review loop"
        exit 1
    fi
fi
```

When `PASS_COUNT < RETRY_COUNT` and `PASS_COUNT > 0` (test is genuinely flaky):

- Print `⚠ FLAKY: test(s) passed $PASS_COUNT/$RETRY_COUNT retries`
- **Do NOT fall through** — invoke `AskUserQuestion`:
  - (a) **Mark and continue** — add `@pytest.mark.flaky(reruns=3)` marker and `# TODO: flaky — investigate <date>` comment to failing test(s); then continue quality stack
  - (b) **Fix now** — stop quality stack here; investigate and fix flaky test before proceeding
  - (c) **Abort** — cancel skill run
- On (b) or (c): stop quality stack execution immediately
- On (a): apply marker + comment, then continue to doctests

```bash
$RUNNER pytest --doctest-modules <target_module> -v 2>&1 | tail -20  # timeout: 600000
DOCTEST_EXIT=${PIPESTATUS[0]}  # non-zero = doctest failures
```

Spawn **foundry:linting-expert** agent if mypy or ruff issues need non-trivial fixes.

**Post-change blast radius** (if codemap installed — soft check):

```bash
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
PROJ=$(basename "$_ROOT")
_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
if command -v codemap-py >/dev/null 2>&1 && [ -f "${_IDX}/${PROJ}.json" ]; then
    codemap-py query rdeps <module> 2>/dev/null | head -20
    echo "^ review rdeps — changes here may affect callers"
fi
```

## Recovery

Stack fails (tests, lint, type check) — pick rollback depth by scope:

1. **Targeted revert** — single file broke: `git checkout HEAD -- <file>` then re-run stack on remaining files — **confirm with user before running**; discards all uncommitted changes in that file (destructive)
2. **Partial revert** — feature branch has mixed good/bad commits: `git revert <bad-commit>` (preserves history)
3. **Full revert** — nothing salvageable: `git reset --hard <last-clean-sha>` — **confirm with user before running**; destructive

Document option used in Final Report under "Recovery" subsection.

## Codex Pre-pass

Mandatory after quality stack. Degrades gracefully if Codex unavailable.

Load `codex-prepass.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof) and run Codex pre-pass on changes.

```bash
cat "$_SHARED/codex-prepass.md"  # timeout: 5000
```

### Codex pre-pass: additional inline steps (step 1 is in the shared file)

2. **Collect findings**: build `CODEX_FINDINGS` — bullet list of every flagged issue from `codex:review` output. Nothing found or step skipped → set `CODEX_FINDINGS=""`. Review read-only — no working-tree changes.
3. **Actor context**: note whether Codex involved (found real issues acted on). Pass as context when committing — `git-commit.md` decides trailers.

Include `### Codex Pre-pass` section in final report:

- Available + findings: list what Codex flagged (become `CODEX_FINDINGS` seed)
- Available + no issues: "Codex pre-pass: no issues found"
- Skipped (unavailable): "bridge@borda-ai-rig absent or disabled — pre-pass skipped"

## Progressive Review Loop

Max 3 cycles. Applied after quality stack. **`oss:*` skills are NEVER auto-invoked from develop flows** — they run only on explicit user request. Escalation below uses `/develop:review` (local-diff multi-agent review; requires `develop` plugin — the consumers of this file).

**Cycle 1: Confidence-gated review escalation**

- Compute concern signal after the quality stack: any unresolved critical/high finding, OR `CODEX_FINDINGS` non-empty and not yet verified. Envelope confidence alone is NOT a trigger — template envelopes print 0.88, so a `< 0.9` arm fired on nearly every run, nesting a full multi-agent `/develop:review` (~5-6 spawns) without a concrete finding to chase; low confidence without findings goes to the report as a stated gap instead
- No concern → skip directly to report; in the final report list optional follow-ups the user may explicitly request (`/develop:review` for a deeper local pass; `/oss:review` once a PR exists)
- Concern present → invoke `/develop:review` scoped to the modified files. `CODEX_FINDINGS` non-empty → prepend to review brief: "Codex pre-pass found the following — verify these, do not rediscover: $CODEX_FINDINGS". Also seed the quality-stack's own findings as "already checked — verify, do not rediscover"
- Capture review state: `{agents_with_findings, unresolved_findings, files_reviewed}`
- Clean (no critical/high findings): skip to report

**Cycle 2: Targeted re-check**

- Fix critical/high findings from Cycle 1
- Re-run quality stack on modified files only
- Set up run dir for file-based handoff: `RUN_DIR=".developments/$(date -u +%Y-%m-%dT%H-%M-%SZ)"; mkdir -p "$RUN_DIR"`
- For each agent type in `agents_with_findings`: spawn directly (not `/oss:review`) with focused prompt scoped to modified files + prior findings. Each agent prompt must end with: "Write your full findings to `$RUN_DIR/<agent-name>.md` using the Write tool. Return ONLY a compact JSON envelope: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.N,\"summary\":\"<agent-name>: N critical, N high\"}`"

Replace bare agent names in spawn prompts with `foundry:` prefixed equivalents: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`.

**Health monitoring**: Agent calls run in background. Spawn, end turn, resume on completion notification — no filler tool call, no "waiting" turn, no sleep. No file activity across wake-ups for 15 min: use Read tool on `$RUN_DIR/<agent-name>.md` to surface partial results. Mark timed-out agents with ⏱ in final report.

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

Load `codex-delegation.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof) and apply delegation criteria. Delegate mechanical follow-up tasks to Codex when accurate specific brief writable.

```bash
cat "$_SHARED/codex-delegation.md"  # timeout: 5000
```

Distinct from Codex pre-pass — pre-pass checks implementation diff for correctness; mechanical delegation outsources low-level follow-up work (scaffolding, boilerplate, migration scripts) after review loop closes.

Include `### Codex Delegation` section in final report only when tasks delegated — omit entirely if nothing delegated.
