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
echo "RUN_DIR=$RUN_DIR BASE_REF_MERGE=$BASE_REF_MERGE"  # literals the spawn prompts and agent-watch-qa.tsv need
```

When `$CHANGE_SCOPE=lint-only` (all selected items were typing/doc/formatting): skip `foundry:qa-specialist` — linting only. Otherwise spawn both in parallel:

```text
Agent(subagent_type="foundry:linting-expert", maxTurns=15, prompt="Review all files changed in the current branch since $BASE_REF_MERGE (expand to literal SHA before spawning). List every lint/type violation. Apply inline fixes for any that are auto-fixable. Write your full findings to $RUN_DIR/linting-expert-step9.md using the Write tool, then return ONLY a compact JSON envelope: {fixed: N, remaining: N, files: [...]}.")

Agent(subagent_type="foundry:qa-specialist", maxTurns=15, prompt="Review all files changed in the current branch since $BASE_REF_MERGE (expand to literal SHA before spawning) for correctness, edge cases, and regressions. Run only the targeted tests: python \"${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_test_plan.py\" targeted --base $BASE_REF_MERGE prints a JSON plan — run exactly its command (null = no targeted tests; say so). Never run the whole test suite: the orchestrator runs it at the end of this gate. Flag any blocking issues (bugs, broken contracts, missing test coverage for the changed logic). Write your full findings to $RUN_DIR/qa-specialist-step9.md using the Write tool, then return ONLY a compact JSON envelope: {blocking: N, warnings: N, issues: [...]}.")
```

> **Health monitoring** — SKILL.md §Agent wait discipline: both spawns run in background. Issue them in one message together with the Write of `$IMPL_DIR/agent-watch-qa.tsv` (rows `linting-expert<TAB><RUN_DIR>/linting-expert-step9.md<TAB>900` and `qa-specialist<TAB><RUN_DIR>/qa-specialist-step9.md<TAB>900`, `RUN_DIR` expanded), then **end the turn** — the completion notification is the resume signal. Never hold the turn open with `Bash(true)`, a "waiting" line, a poll, `ScheduleWakeup`, `ListAgents` or a `Monitor` loop. At each wake-up run the watch check; `timed_out`, or a notification without its output file → surface partial results from `$RUN_DIR` with ⏱ now.

- `foundry:linting-expert` made file changes → commit:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_lint_fixes.py"  # timeout: 3000
```

**Gate loop — QA gate** (max 3 iterations; every test run inside it is targeted, never the full suite):

1. Run truth-check — `foundry:qa-specialist` reports blocking issues
2. Fix — apply fixes inline or via `IMPL_AGENT`
3. Re-run `foundry:qa-specialist` — clean → proceed; still blocking → loop
4. Blocked after 3 iterations → **stop workflow** — do not push; surface all remaining blocking issues; print: `⛔ QA gate blocked push — review findings above, fix errors, then re-run /resolve or push manually after fixing.`

**Full suite — once after the QA loop is clean, again only after a fix.** Skip it when `$CHANGE_SCOPE=lint-only` (no tests ran there before either). Otherwise resolve the repository's own command — its contributor docs (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`), then a Makefile `test` target, then `python -m pytest` — into a script:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot stage the full-suite run"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_test_plan.py" full-command --script "$IMPL_DIR/full-suite.sh"  # timeout: 15000
```

Then run it once, as a **background** Bash call (`run_in_background: true` — a large suite outlives the ~10 min foreground cap), and end the turn; its exit re-invokes the orchestrator, no polling:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot run the full suite"; exit 1; }
_T0=$(date +%s)
bash "$IMPL_DIR/full-suite.sh" > "$IMPL_DIR/full-suite.log" 2>&1
echo "$?" > "$IMPL_DIR/full-suite.rc"
# a background call's own tool timing ends at dispatch, so the suite's wall time is recorded here instead
echo "$(( $(date +%s) - _T0 ))" > "$IMPL_DIR/full-suite.seconds"
tail -40 "$IMPL_DIR/full-suite.log"
```

Exit 0 → proceed. Non-zero → the failing tests are blocking issues and count against the same 3-iteration gate cap: fix them (gate-loop step 2; the failing ids may be rerun while fixing, but that is never the verification), then **rerun the full suite** — the gate-loop verification for a full-suite failure is always a full-suite rerun — with the same two blocks — background run, rc and log rewritten on disk — because a fix can break something outside the targeted set. Repeat until the full suite exits 0 or the cap is spent; at the cap stop with the step 4 message, `⛔ QA gate blocked push — review findings above, fix errors, then re-run /resolve or push manually after fixing.` A green full-suite run must be the last test evidence before Step 10. Record the command, its source and every run's result in the final report.

- Warnings (non-blocking) → record in report; don't block push

Revoke commit authorization (recompute sentinel path — main PR flow doesn't set `$SENTINEL`):

```bash
SENTINEL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/compute_commit_sentinel.py" 2>/dev/null || echo "")
rm -f "$SENTINEL"  # timeout: 3000
```
