<!-- oss:resolve Step 9 — executed via: cat $_OSS_RESOLVE/modes/lint-qa-gate.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md -->

<!-- Input: resolve-base-ref-${CSID} sentinel from Step 4 or report mode, current working tree after Step 8; $RUN_DIR optional (created here if unset) -->

<!-- $CHANGE_SCOPE: lint-only | targeted | full (default=targeted; set in SKILL.md Step 8 effort classification) -->

<!-- Output: lint fixes committed (if any), or BLOCKING_ISSUES found -->

## Step 9: Lint and QA gate

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
[ -n "$BASE_REF" ] && git check-ref-format --branch "$BASE_REF" >/dev/null 2>&1 || { echo "⛔ BASE_REF sentinel missing or invalid; refusing vacuous QA range"; exit 1; }
[ -z "$RUN_DIR" ] && RUN_DIR=".reports/resolve/$(date -u +%Y-%m-%dT%H-%M-%SZ)"  # expand $RUN_DIR to literal value in prompts below — agents receive text, not shell context
mkdir -p "$RUN_DIR" # timeout: 5000
# merge-base for accurate diff range in agent prompts
BASE_REF_MERGE=$(git merge-base HEAD "origin/$BASE_REF" 2>/dev/null)
[ -n "$BASE_REF_MERGE" ] || { echo "⛔ Cannot establish a merge base with origin/$BASE_REF; refusing vacuous QA range"; exit 1; }
```

When `$CHANGE_SCOPE=lint-only` (all selected items were typing/doc/formatting): skip `foundry:qa-specialist` — linting only. Otherwise spawn both in parallel:

```text
Agent(subagent_type="foundry:linting-expert", maxTurns=15, prompt="Review all files changed in the current branch since $BASE_REF_MERGE (expand to literal SHA before spawning). List every lint/type violation. Apply inline fixes for any that are auto-fixable. Write your full findings to $RUN_DIR/linting-expert-step9.md using the Write tool, then return ONLY a compact JSON envelope: {fixed: N, remaining: N, files: [...]}.")

Agent(subagent_type="foundry:qa-specialist", maxTurns=15, prompt="Review all files changed in the current branch since $BASE_REF_MERGE (expand to literal SHA before spawning) for correctness, edge cases, and regressions. Run tests for changed modules only — do not run the full test suite unless $CHANGE_SCOPE=full. Flag any blocking issues (bugs, broken contracts, missing test coverage for the changed logic). Write your full findings to $RUN_DIR/qa-specialist-step9.md using the Write tool, then return ONLY a compact JSON envelope: {blocking: N, warnings: N, issues: [...]}.")
```

> **Health monitoring**: both spawns run in background. Issue in one message, then **end the turn** — completion notification is resume signal. Never hold turn open with `Bash(true)`, a "waiting" line, or a poll. On notification, read each output file; empty or missing → surface partial results from `$RUN_DIR` with ⏱.

- `foundry:linting-expert` made file changes → commit:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_lint_fixes.py"  # timeout: 3000
```

**Gate loop — QA gate** (max 3 iterations):

1. Run truth-check — `foundry:qa-specialist` reports blocking issues
2. Fix — apply fixes inline or via `IMPL_AGENT`
3. Re-run `foundry:qa-specialist` — clean → proceed; still blocking → loop
4. Blocked after 3 iterations → **stop workflow** — do not push; surface all remaining blocking issues; print: `⛔ QA gate blocked push — review findings above, fix errors, then re-run /resolve or push manually after fixing.`

- Warnings (non-blocking) → record in report; don't block push

Revoke commit authorization (recompute sentinel path — main PR flow doesn't set `$SENTINEL`):

```bash
SENTINEL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/compute_commit_sentinel.py" 2>/dev/null || echo "")
rm -f "$SENTINEL"  # timeout: 3000
```
