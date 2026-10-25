---
name: debug
description: 'Investigation-first debugging — gather evidence, form confirmed root-cause hypothesis, hand off to fix mode with diagnosis file. TRIGGER when: user reports a symptom or failing test with Python traceback, or asks to investigate a runtime/CI failure with reproducible evidence; phrases: "debug this failure", "why is X broken", "find the root cause of <error>", "investigate this CI failure". SKIP when: pure config quality issues (use `/foundry:audit`); broad system-wide diagnosis without traceback (use `/foundry:investigate`); user already knows the fix (use `/develop:fix`); non-Python project.'
argument-hint: '<symptom or issue # (plain 123 or #123)> [--issue <N>] [--repo <owner/repo>] [--no-challenge] [--challenge] [--team] [--worktree] [--ci-run <run-id-or-url>] [--codemap] [--no-codemap] [--keep "<items>"]'
effort: high
allowed-tools: Read, Write, Bash, Grep, Agent, TaskList, TaskCreate, TaskUpdate, AskUserQuestion, EnterWorktree, ExitWorktree
disable-model-invocation: true
---

<objective>

Investigation-first debugging. Gather evidence, trace data flow, form confirmed root-cause hypothesis, hand off to fix mode.

NOT for: production incidents without any CI run ID or local traceback (use `/foundry:investigate` (requires foundry plugin) for triage); `.claude/` config issues (use `/foundry:audit` (requires foundry plugin)); non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest, use language-native toolchain instead. CI-only failures ARE supported — pass `--ci-run <run-id or URL>` for GitHub Actions logs as evidence source.

**Issue ID routing note**: issue mode selected when `--issue` flag present, or argument (after other flags stripped) is a pure run of digits with optional `#` prefix (e.g. `123` or `#123`). No numeric threshold. Pass `--issue <N>` to force issue mode for any argument.

</objective>

<compaction>

- Key boundary: after Steps 1+2 — evidence gathered and pattern analysis complete, before hypothesis gate (Step 3).
- Preserve: debug mode, CI run ID if set, evidence signals (issue body, test path), tried-hypotheses ledger (candidate causes + verdicts — refuted/ruled-out/open), --keep items.
- Refresh also after any Step 3 probe ruling out a hypothesis — so post-compact gate doesn't re-test refuted causes (loop guard).

</compaction>

<workflow>

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (mounted by develop plugin init) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "" > "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # a prior run's dir must not outlive a failed --init; agent-resolution.md §Agent waits recreates it when empty
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" --init  # timeout: 5000 — cold resolve: writes dev-shared (every later block warm-reads it) and a fresh dev-agent-watch-dir per run (agent-resolution.md §Agent waits)
# loads: compaction-contract.md
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" agent-resolution.md
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" runner-detection.md
```

Contains: foundry check + fallback table, and §Agent waits — no polling (applies to every spawn below, team mode included). If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:challenger`.

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Turn budget** — measured debug runs made one tool call per turn throughout and spent up to 11 turns on task bookkeeping alone — the cost was the standalone turns, not the task count; every turn re-reads the whole context.

- **One task per step, never a bookkeeping-only turn.** Create one task per step — Step 1 Symptom · Step 2 Pattern analysis · Challenger gate · Step 3 Hypothesis and gate · Step 4 Hand off · Final Report — all in the same response as the first Project Detection call, so step-level progress stays visible. Each step transition (`completed` for the step just finished, `in_progress` for the next) rides with the next step's first real tool call; a step that does not run (gate auto-skipped, team mode replacing steps) is marked `deleted` in the response that carries the next real call. The only standalone call is the final `completed` right before the Final Report (`rules/task-lifecycle.md`).
- **Independent calls share one response — never across a question.** Agent Resolution also loads Project Detection's `runner-detection.md`; the language check runs next; the flag-parsing block in the next response, once any language question is answered. Every later preamble block keeps its order: each sits behind a question (codemap gates, unsupported flag) or the worktree entry, which must precede the codemap gate. Step 1 evidence gathering — log fetch, issue fetch, Reads of suspect files, Grep for the error pattern — in one response.

## Project Detection

`runner-detection.md` loaded by the Agent Resolution block above. Sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

**Language preflight gate**: detect project language; adjust test runner accordingly.

```bash
# timeout: 5000
LANG_HINT="python"
if [ ! -f "pyproject.toml" ] && [ ! -f "setup.py" ] && [ ! -f "setup.cfg" ] && [ ! -f "Pipfile" ]; then
    if [ -f "package.json" ]; then LANG_HINT="node"
    elif [ -f "go.mod" ]; then LANG_HINT="go"
    elif [ -f "Cargo.toml" ]; then LANG_HINT="rust"
    fi
fi
```

If `LANG_HINT` not `python`: invoke `AskUserQuestion` — "Non-Python project detected (`$LANG_HINT`). Toolchain assumes pytest. How to proceed?" · (a) **Abort** — use language-native runner · (b) **Continue** — repo also has Python sources. On Abort: stop.

**Checkpoint**: debug = investigation only — no code changes. `.plans/active/debug_<slug>.md` (written in Step 4) serves as implicit session state. No `.developments/` checkpoint needed.

## Flag parsing

Parse flags into actual shell variables (not prose) so downstream blocks see correct values:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract-keep-flag.py" dev-debug "$ARGUMENTS"  # timeout: 5000 — parses --keep, clears stale contract
rm -f "${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}"  # timeout: 3000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill debug --write-files "$ARGUMENTS"  # timeout: 10000
# URL normalization + log fetching: §URL Normalization in ci-log-extract.md
```

## Worktree isolation

> loads: worktree-isolation.md

When `--worktree` set, run the investigation in an isolated git worktree so reproduction attempts (repro scripts, temp edits) can never mutate the main sources — **before** codemap detection or Step 1.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-debug-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
if [ "$WORKTREE_ENABLED" = "true" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" worktree-isolation.md; fi
```

`WORKTREE_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`). **Read-only skill** — obey §Deliverable: the diagnosis file is written to the **main tree** (`$_ORIG_ROOT`) at Step 4 so `/develop:fix` can read it. Else skip — run in main tree (doc not loaded).

**Codemap resolve** — `CODEMAP_RAW` already written to `${TMPDIR:-/tmp}/dev-debug-codemap-${CSID}` (per-skill) and `${TMPDIR:-/tmp}/dev-codemap-raw-${CSID}` (legacy) by flag-parsing block above (via `dev_parse_args.py --skill debug --write-files`). Read per-skill path, then normalize via `codemap_resolve.py`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# skill-specific paths (dev-debug-codemap-*) avoid stale value from a prior feature --codemap run
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" debug) || exit 1
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" codemap-gates.md
```

> loads: codemap-gates.md

Follow Gate A and Gate B.

Downstream blocks read back: `IFS= read -r CHALLENGE_ENABLED < "${TMPDIR:-/tmp}/dev-challenge-enabled-${CSID}" 2>/dev/null || CHALLENGE_ENABLED=true`, `IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED=false`, `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`, `IFS= read -r CI_RUN_ID < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || CI_RUN_ID=""`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CI_RUN_ID < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || CI_RUN_ID=""
if [ -n "$CI_RUN_ID" ]; then python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" ci-log-extract.md; else echo "→ no --ci-run; CI log extract skipped"; fi  # timeout: 5000
```

`--ci-run` absent → nothing loaded; skip to the unsupported flag check. Else follow §URL Normalization to set `CI_RUN_ID`. If `CI_RUN_ID` set, follow §Log Fetching and §Log Parsing to set `CI_LOG_EVIDENCE`; use it as evidence source in Step 1 instead of local pytest.

**Unsupported flag check** — after ALL supported flags extracted (including `--issue` and `--keep` from blocks above), scan `$ARGUMENTS` for remaining `--<token>` tokens not in supported list. Do NOT include `--issue` or `--keep` in "unknown" set — both are consumed by the mode-detect and keep-parse blocks above. Supported: `--no-challenge`, `--challenge`, `--team`, `--worktree`, `--ci-run`, `--issue`, `--repo`, `--codemap`, `--no-codemap`, `--keep`. If truly unknown token found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Mode selection** — debug runs in one of two mutually-exclusive modes; set explicitly before any Step:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# strip flags first — "123 --no-challenge" would fail integer detection otherwise
# --keep-state: no eval; own dev-debug-args prefix, never dev_parse_args.py's dev-debug-clean-args; --inlined: Flag parsing already cleared
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags no-challenge,challenge,team,worktree,no-codemap,codemap --value-flags ci-run,repo,issue --keep-state dev-debug-args --inlined "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r VALUE_ISSUE < "${TMPDIR:-/tmp}/dev-debug-args-issue-${CSID}" 2>/dev/null || VALUE_ISSUE=""
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/dev-debug-args-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
if [ -n "$VALUE_ISSUE" ] || [[ "$CLEAN_ARGS" =~ ^#?[0-9]+$ ]]; then
    DEBUG_MODE="issue"
    echo "${VALUE_ISSUE:-$CLEAN_ARGS}" > ${TMPDIR:-/tmp}/dev-debug-issue-num-${CSID}
else
    DEBUG_MODE="symptom"
fi
echo "$DEBUG_MODE" > ${TMPDIR:-/tmp}/dev-debug-mode-${CSID}
```

**Run slug** — derived once, here, so every `.temp/debug/<slug>-*.md` path in Steps 1-3 and the Step 4 diagnosis file share one name; `<slug>` below = the printed `SLUG` value:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/dev-debug-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""  # every declared flag (incl. --repo, --ci-run) and its value already stripped by Flag parsing
SLUG=$(echo "$CLEAN_ARGS" | tr ' ' '\n' | grep -v '^--' | grep -v '^[0-9]\+$' | head -4 | tr '\n' '-' | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-' | sed 's/-$//'); [ -z "$SLUG" ] && SLUG="unnamed-$(date +%s)"
# grep -v strips bare numeric tokens (CI run IDs) — avoids filenames like debug_12345678.md
echo "$SLUG" > "${TMPDIR:-/tmp}/dev-debug-slug-${CSID}"  # Step 4 reloads it — bash state lost between calls
echo "SLUG=$SLUG"
```

Subsequent steps branch by `DEBUG_MODE`:

- **Issue mode**: Step 1 fetches issue body and extracts test path before invoking pytest; skip symptom-text pytest block. Stop after Step 4 (handoff) — do not run symptom-text branches.
- **Symptom mode**: Step 1 skips issue fetch; uses free-text symptom directly. Skip issue-mode pytest block entirely.

**If `TEAM_MODE=true`** — execute team investigation now: it **replaces** standard Steps 1-2 (parallel hypothesis investigation supplants serial evidence gathering). After team synthesis, Steps 3-4 (hypothesis gate + handoff to fix) still execute, inline within this block on the winning hypothesis — never loop back to standard Steps 1-2. Two gates in the replaced span are **not** Steps 3-4 and still run on the consensus root cause before Step 3: the Claim-validation gate (Step 1) and the `## Challenger gate` section. `--challenge` forces the latter in team mode exactly as in solo mode.

Team run directory (`TEAM_MODE=true` only; solo runs skip this block) — created once and persisted, so synthesis and deliverable checks in later calls reuse the same `TS` (step 4 below spawns into it). Its first line is the team-protocol guard shared by every develop team mode: printed `! BLOCKED` → stop the skill and relay that line as the final reply; never fall back to solo, never spawn teammates told to read a missing file:

```bash
# timeout: 5000
[ -f ~/.claude/TEAM_PROTOCOL.md ] || { echo "! BLOCKED — --team needs ~/.claude/TEAM_PROTOCOL.md, written by /foundry:setup (requires foundry plugin); run /foundry:setup, or re-run without --team"; exit 1; }
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
mkdir -p ".temp/develop/$TS"
echo "$TS" > "${TMPDIR:-/tmp}/dev-debug-team-ts-${CSID}"  # reload with IFS= read -r — bash state lost between calls
echo "TS=$TS"
```

1. `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" preflight-helpers.md` §Team Spawn Template. Confirm `[ROLE_PHRASE]` = symptom text (from `$ARGUMENTS` stripped of flags), `[FILE_SLUG]` = `debug-hypothesis`.
2. **Issue mode** (`DEBUG_MODE=issue`): fetch the issue body **before** spawning — run the Step 1 issue-fetch block (`dev_issue_fetch_wrap.py`) and the test-path extraction block, then use the issue title + body as `[ROLE_PHRASE]` in place of the bare number. Teammates given only a number have no evidence to investigate.
3. Run project detection (`export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" runner-detection.md`) to set `$TEST_CMD` and `$PYTEST_CMD`.
4. Use the `TS` printed by the team run-dir block above (sentinel `dev-debug-team-ts-${CSID}`; never a fresh timestamp). Spawn 2-3 `foundry:sw-engineer` agents (model=opus) in parallel — each investigating one independent root-cause hypothesis. Use Team Spawn Template from preflight-helpers: replace `[ROLE_PHRASE]` with symptom, `[FILE_SLUG]` with `debug-hypothesis`, assign each agent a distinct hypothesis number N. The agents run in parallel in this working tree, so every prompt keeps the template's line verbatim: "Read-only investigation: never edit, create or delete tracked files and never run `git stash`, `git checkout`, `git reset` or `git restore` — sibling investigators share this working tree; run reproduction probes in a `mktemp -d` copy or under the run directory; write only your analysis file." (run directory here: `.temp/develop/$TS/`). Each agent writes full output to `.temp/develop/$TS/debug-hypothesis-N-$TS.md` (run-dir timestamp, matching preflight-helpers §Team Spawn Template), returns compact JSON `{"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line description of hypothesis>"}`.
5. **Coordination**: lead broadcasts `{symptom: <description>, traceback: <key lines>}` to teammates before spawning. After all return, the lead cross-challenges the competing analyses inline — no teammate resume, no new spawn: check each hypothesis's claims against the other teammates' evidence files and note which evidence supports, contradicts, or is left unexplained by it. Convergence rule: select hypothesis with most direct evidence (observable in code or logs); truly tied → invoke `AskUserQuestion` presenting top 2 competing hypotheses.
6. **Synthesis trace (lead, inline — no spawn)**: after individual teammate reports, lead reads all teammate findings from `.temp/develop/$TS/debug-hypothesis-*.md` (2-3 files already on disk), produces the unified cross-cutting trace map itself — entry point, modules crossed, state mutations, invariant violations across hypotheses. Write to `.temp/develop/$TS/debug-trace-synthesis.md`. A dedicated synthesis agent costs ~120,851 tok fixed overhead for a read-and-merge the lead performs inline in fix's equivalent step — spawn nothing here.
7. Lead synthesises consensus root cause from trace map + competing hypotheses, then runs Steps 3-4 on winning hypothesis.
   - **Step 3 gate in team mode**: convergence reached by lead synthesis (all hypotheses point to same root cause, high confidence) → present converged hypothesis without new user confirmation prompt — state "Team converged on root cause (no ambiguity)", proceed directly to Step 4 handoff. Only invoke `AskUserQuestion` at Step 3 if competing hypotheses remain or convergence declared by default (tied evidence).
   - This is a deliberate divergence from solo mode's unconditional Step 3 gate (§Step 3: Hypothesis and gate, **Gate**): independent convergence across 2-3 investigators is the evidence the solo gate asks the user to supply. Divergent or tied evidence → the solo gate applies unchanged.

Arm batch `team` in the spawn response (deliverables `.temp/develop/$TS/debug-hypothesis-N-$TS.md`; `agent-resolution.md` §Agent waits — no polling, per-agent deadlines). Spawn the batch, **end the turn**, resume on each completion notification and run the §Agent waits check block (`agent_watch.py`) once — never a no-op call, a "waiting" line, or a sleep. Mark timed-out agents ⏱ in synthesis.

## Step 1: Understand the symptom

Collect all signals before forming any hypothesis.

**Substitution tokens** — every `<…>` token and every model-assigned variable in the blocks below (`TARGET_MODULE`, `FAILING_FN`, `TEST_PATH`, `FAILING_TEST_NODE`, `SUSPECT`) is resolved into its shell variable before the block runs. Never execute a literal `<X>` — bash reads `<` as a stdin redirect.

**Structural context (codemap-py — only if `CODEMAP_ENABLED=true`)**: if index available, run before codebase exploration to pre-load blast-radius context for failing module:

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/dev-debug-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
if [ "$CODEMAP_ENABLED" = "true" ]; then
    codemap-py query central --top 5 2>/dev/null
fi
```

After reading traceback or `$ARGUMENTS`, derive `TARGET_MODULE`: strip `src/`, `.py` suffix, replace `/` with `.` (e.g. `src/mypackage/auth.py` → `mypackage.auth`); capture the failing function name too, if known, as `FAILING_FN`. Both are substitution tokens (Step 1 head):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/dev-debug-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
[ "$CODEMAP_ENABLED" = "true" ] || exit 0
# resolve TARGET_MODULE/FAILING_FN first, e.g. TARGET_MODULE=mypackage.auth; FAILING_FN=validate
# timeout: 10000
if [ -z "$TARGET_MODULE" ]; then
    echo "⚠ TARGET_MODULE not resolved — skipping codemap rdeps/fn-blast query"
else
    codemap-py query rdeps "$TARGET_MODULE" 2>/dev/null
    [ -n "$FAILING_FN" ] && codemap-py query fn-blast "$TARGET_MODULE::$FAILING_FN" 2>/dev/null  # v3 index only
fi
```

If codemap-py results returned: prepend `## Structural Context (codemap-py)` block to foundry:sw-engineer spawn prompt (Step 1). Callers of failing module = likely affected paths to verify after fix. fn-blast shows transitive callers — high-depth callers = regression risk.

**Issue-number mode first** — if `$ARGUMENTS` is issue number, fetch issue body and extract test path BEFORE invoking pytest:

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# wrapper: cross-repo branch from dev-upstream, persists body to dev-issue-body-${CSID} for next block (avoids re-running gh)
IFS= read -r _ISSUE_NUM < "${TMPDIR:-/tmp}/dev-debug-issue-num-${CSID}" 2>/dev/null || _ISSUE_NUM=""
[ -n "$_ISSUE_NUM" ] || { echo "! BLOCKED — dev-debug-issue-num sentinel missing; re-run Mode selection (raw arguments still carry flags)"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_issue_fetch_wrap.py" debug "$_ISSUE_NUM" || echo "⚠ issue fetch failed (exit $?) — proceed without the issue body and say so"
```

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# grep file directly — `read -r` would capture only the first line of a multi-line issue body
TEST_PATH=$(grep -oE '(tests?/[^[:space:]]+\.py|test_[^[:space:]]+\.py)' "${TMPDIR:-/tmp}/dev-issue-body-${CSID}" 2>/dev/null | head -1)
if [ -z "$TEST_PATH" ]; then
  echo "→ No test file found in issue; running full test suite"
elif [ ! -f "$TEST_PATH" ]; then
  echo "⚠ test path from issue not found on disk: $TEST_PATH — running full suite"
  TEST_PATH=""
fi
echo "$TEST_PATH" > "${TMPDIR:-/tmp}/dev-debug-test-path-${CSID}"  # persist — three later blocks consume it
```

Run the repro (empty `$TEST_PATH` → full suite). `dev_run_repro.py` reads the `dev-pytest-cmd` and `dev-debug-test-path` sentinels, runs the allowlisted runner with `--tb=long -v` (no shell, no `eval`), prints the last 60 lines, writes `dev-gate-exit`, then prints `Bug reproduced — tests fail. Proceed to fix.` (non-zero exit) or `Tests pass — bug may not be reproducible via pytest; check symptom directly.` (zero). An empty runner prints `! PYTEST_CMD unresolved — re-run §Project Detection (runner-detection.md)` and exits 1 — never read that as a reproduced bug. Only pytest exit 1 counts as reproduced; any other non-zero code (2 collection error, 4 bad node id, 5 nothing collected) prints `! GATE ERROR: pytest exit N …` and exits 1 — fix the test path or collection error and re-run:

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_repro.py"
```

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TEST_PATH < "${TMPDIR:-/tmp}/dev-debug-test-path-${CSID}" 2>/dev/null || TEST_PATH=""
git log --oneline -20
COMMIT_COUNT=$(git rev-list --count HEAD 2>/dev/null || echo 1)
LOOKBACK=$(( COMMIT_COUNT < 5 ? COMMIT_COUNT : 5 ))
if [ "$LOOKBACK" -gt 1 ]; then
    # empty pathspec is fatal (exit 128) — omit `--` clause for full-repo diff
    if [ -n "$TEST_PATH" ]; then
        git diff "HEAD~${LOOKBACK}..HEAD" -- "$TEST_PATH"
    else
        git diff "HEAD~${LOOKBACK}..HEAD"
    fi
fi
```

**Cross-repo adaptation** (when `REPO_NAME` set) — issue from different codebase. After fetching issue:

1. Extract bug's root cause intent — what invariant violated, not just described symptoms (which may reference upstream structure or code paths)
2. Search LOCAL codebase for equivalent failure site — grep for related symbols; code paths may differ from upstream due to divergence
3. Treat upstream issue as debugging context, not as a map — trace actual failure in local code

**Symptom-text mode** — if `$ARGUMENTS` is free-text, skip issue fetch + extraction; locate the failing test path from the symptom (Grep against symptom keywords or heuristic file search) and substitute it for `<failing-test-path>` below — or `FULL_SUITE` when none is located. The block aborts on an unsubstituted or empty value, so a forgotten substitution never silently runs the full suite:

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TEST_PATH="<failing-test-path>"  # resolved path, e.g. from Grep of symptom keywords under tests/; FULL_SUITE when none located
case "$TEST_PATH" in *'<'*'>'*|"") echo "! BLOCKED — TEST_PATH unsubstituted; resolve the failing test path (or FULL_SUITE) first"; exit 1;; FULL_SUITE) TEST_PATH="";; esac
echo "$TEST_PATH" > "${TMPDIR:-/tmp}/dev-debug-test-path-${CSID}"  # persist — later blocks consume it
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_repro.py"  # same repro as above, on the path just persisted
```

**Claim-validation gate** — before debugging, validate user's expectation is itself correct. A bug report always contains an implicit or explicit claim: "X should behave like Y". Claim may be wrong — misread docs, misunderstood API contract, incorrect formula, outdated assumption. Fixing a correct implementation to match a wrong expectation wastes effort, introduces regressions.

Classify claim type and validate accordingly:

| Claim type | Example | Validation approach |
| -- | -- | -- |
| Numeric / metric result | "IoU should be 0.5 but returns 0.3" | Verify formula from authoritative source; compute expected value independently |
| API contract | "function should return list but returns generator" | Read docstring, type hints, and docs — not current implementation |
| Algorithm correctness | "sorting is wrong — element 3 should come before element 1" | Trace comparison logic against documented sort key or invariant |
| Behavioral invariant | "adding item twice should raise, not silently dedupe" | Check README, docs, or published contract — not assumed behavior |
| Cross-version assumption | "this worked in v1, now broken" | Check changelog/release notes for intentional breaking change |
| Domain-specific formula | ML metric, statistical estimator, signal processing | Spawn `research:scientist` (requires `research` plugin); pass metric name, formula used, claimed expected value; ask: "Is the claimed expected value correct per authoritative definition?" |

**Resolution rules:**

1. Claim verifiable from docs/type hints/tests → read source now; confirm before proceeding
2. Claim verifiable by quick computation → run inline script to compute expected value independently
3. Domain-specific claim requiring literature → spawn `research:scientist`; if plugin absent flag: `⚠ Expected value unverified — treating as assumption`
4. Claim contradicts docs/contract → it is a **documentation misunderstanding**, not a bug; surface this to user before any code change
5. **Gate**: do not form root-cause hypothesis until claimed expectation confirmed or explicitly flagged as unverified; wrong expectation → wrong fix

Use Grep (pattern: failing symbol, class, or error keyword) to trace call path, entry point to failure site. Path hint: `src/` if exists, else search from project root (`.`).

Spawn **foundry:sw-engineer** agent to map execution path — Arm batch `scope` in the spawn response with deliverable `.temp/debug/<slug>-analysis.md` (`agent-resolution.md` §Agent waits — no polling, per-agent deadlines). The prompt tells the agent to write its full analysis to that file with the Write tool and return ONLY `{"status":"done","file":"<path>","summary":"<one-line>"}`. It must produce:

- Entry point to failure: which modules does call cross?
- What state mutated along the way?
- What invariant violated at failure point?
- Any recent commit touching this path (from git log output)

Carry the analysis summary in the diagnosis file. Write the full analysis to `.temp/debug/<slug>-analysis.md` first — the agent's deliverable above; a notification without that file is ⏱ timed out, surfaced, never silently skipped; when the next gate fires, print the analysis summary once as the last reply text before the question, as normal Markdown. The question text carries counts, headline and `→ .temp/debug/<slug>-analysis.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview`; do not repeat the same analysis at later gates.

**Scope gate**: if root cause spans 3+ modules, flag complexity smell. Use `AskUserQuestion` to present scope concern before proceeding, with options: "Narrow scope (Recommended)" / "Proceed anyway".

**Flaky-test branch** — symptom intermittent (passes alone, fails in full suite): run binary-search isolation. Resolve the failing test node ID from `$ARGUMENTS` or prior pytest output into `FAILING_TEST_NODE` (e.g. `FAILING_TEST_NODE=tests/foo.py::test_bar`) before executing this block:

```bash
# resolve FAILING_TEST_NODE first:
# FAILING_TEST_NODE=$(echo "$ARGUMENTS" | grep -oE 'tests?/[^[:space:]]+::test_[^[:space:]]+' | head -1)
if [ -z "$FAILING_TEST_NODE" ]; then
    echo "⚠ FAILING_TEST_NODE not resolved — cannot run polluter isolation; surface failing test node ID first"
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/find-polluter.py" "$FAILING_TEST_NODE"  # timeout: 60000
fi
```

Output names polluting upstream test. `find-polluter.py` ships in this plugin's own `bin/` (kept identical to foundry's canonical by `propagate_shared.py`) — works on develop-only install. Run only when CI shows non-deterministic failure pattern.

## Step 2: Pattern analysis

Find nearest similar working code path, compare exhaustively:

1. Locate 2-3 code paths handling similar input or similar work *successfully*
2. List **every** difference between working path and broken one — not just obvious one
3. Check across axes:
   - Same input, different environment (versions, config, data shape)?
   - Same logic, different call order or timing?
   - Conditionals taking different branches on different inputs?
   - None/empty guards present in working path but absent in broken one?

Step catches non-obvious causes — ordering dependency, environment-specific state, type coercion silently changing behaviour.

**Record candidates (loop guard)** — as each candidate cause identified, append it to hypothesis ledger with verdict `open`. Ledger inlined into compaction contract at boundary below, so a mid-investigation compaction never loses which causes were already weighed. Verdict values: `open` · `refuted (challenger)` · `ruled-out (probe)`. The ledger is append-only: a later verdict on a candidate is a new `<candidate cause> :: <verdict>` line, never an edit of the earlier one, and readers take the **last** line per candidate as its current verdict.

```bash
# ledger survives compaction via contract — avoids re-testing refuted causes mid-investigation
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "<candidate cause> :: open" >> ${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}
```

## Challenger gate

**Decision** (default runs at any size — a narrow root cause on the main path is easy to misjudge; size never skips the gate):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**.
2. else → **always run**. `--challenge` (`CHALLENGE_FORCED`) is accepted for compatibility and changes nothing.

Arm batch `challenge` in the spawn response (`agent-resolution.md` §Agent waits). Spawn `foundry:challenger` with pattern analysis from Step 2 (differences between working/broken paths, candidate causes):

> "Review pattern analysis and candidate root causes. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result — record each candidate's verdict as you parse by appending a verdict event line to the hypothesis ledger (`${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}`), one `echo` per verdict in one block, same shape as the Step 2 block — `echo "<candidate cause> :: refuted (challenger)" >> ${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}`; never rewrite earlier lines:

- **Blockers found** → STOP. Write the blocker findings (one line each: claim · file:line · evidence) to `.temp/debug/<slug>-blockers.md` first; print them once as the last reply text before invoking `AskUserQuestion`, as normal Markdown. The question text carries the blocker count, headline and `→ .temp/debug/<slug>-blockers.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview` — "Challenger raised N blocker(s) on the candidate root cause. How to proceed?" · (a) **Address blockers** — revise candidates, re-run gate · (b) **Accept risk** — proceed to Step 3 with blockers documented in the diagnosis file · (c) **Abort**. On Abort: stop. Incorporate surviving challenges into the hypothesis list before the Step 3 gate; append a `<candidate cause> :: refuted (challenger)` event line for any candidate the challenger refuted.
- **Concerns only** → add as alternative hypotheses in Step 3; append each new concern to ledger as `:: open (alt)`; continue.
- **No findings / all refuted** → proceed.

```bash
# compaction boundary (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEBUG_MODE < "${TMPDIR:-/tmp}/dev-debug-mode-${CSID}" 2>/dev/null || _DEBUG_MODE="symptom"
IFS= read -r _CI_RUN < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || _CI_RUN=""
IFS= read -r _TEST_PATH < "${TMPDIR:-/tmp}/dev-debug-test-path-${CSID}" 2>/dev/null || _TEST_PATH=""
_TRIED=$(tail -8 "${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}" 2>/dev/null)  # cap keeps contract compact; tail keeps the latest verdict events (last line per candidate wins)
_PRESERVE="mode=$_DEBUG_MODE, ci-run=${_CI_RUN:-none}, test_path=${_TEST_PATH:-full-suite}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" --keep-sentinel dev-debug-keep-items "develop:debug" "hypothesis+handoff (after evidence gathered and pattern analysis)" ".plans/active/" "$_PRESERVE" "state hypothesis with evidence (Step 3) → confirm root cause → write diagnosis → handoff to /develop:fix. Skip any candidate marked refuted/ruled-out in the tried list below." "tried (do NOT re-test refuted/ruled-out)" "$_TRIED"  # timeout: 5000
```

## Step 3: Hypothesis and gate

Build the root cause hypothesis in one sentence before writing any code; present it only at the gate below, after premise grounding:

```text
Root cause: [root cause]
Evidence for: [signals that support this]
Evidence against: [anything that contradicts or remains unexplained]
Confidence: high / medium / low
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" premise-grounding.md  # timeout: 5000
```

§Premise Grounding Gate. Apply using **debug** context from Skill contexts table. Run before presenting hypothesis — any ungrounded premise in hypothesis produces a fix that addresses wrong mechanism.

**Gate**: present hypothesis to user, wait for confirmation or challenge before proceeding to Step 4 — as the turn's final reply, or, when asked via `AskUserQuestion`, Write the hypothesis and its evidence to `.temp/debug/<slug>-hypothesis.md` first and print it once as the last reply text before the question, as normal Markdown. The question text carries the root-cause headline, confidence and `→ .temp/debug/<slug>-hypothesis.md`, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence. Options carry a label and a one-line description, no `preview`. Wrong hypothesis produces fix that passes tests but doesn't resolve underlying problem.

If confidence low: propose targeted probe (minimal script, added log statement, single assertion) to gather missing signal — run before committing to fix. Probe rules out current hypothesis → append `<cause> :: ruled-out (probe)` to `${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}`, re-run boundary contract block above before re-hypothesizing — keeps loop guard current so ruled-out cause not revisited.

**Test impact (codemap-py) — hypothesis confirmed** — root cause now names a suspect module (and often a function). Query affected test set once here so `/develop:fix` reuses it instead of re-querying. Gated on `CODEMAP_ENABLED` + `codemap-py query` availability (same gate as Step 1). Assign `SUSPECT` (substitution token, Step 1 head) in the block below as the confirmed hypothesis in dotted qname form: `module.path::function` (fn known) or bare `module.path` (module-level), same derivation as `TARGET_MODULE` (Step 1: strip `src/`, drop `.py`, `/` → `.`):

```bash
# timeout: 8000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r CODEMAP_ENABLED  2>/dev/null < "${TMPDIR:-/tmp}/dev-debug-codemap-enabled-${CSID}" || CODEMAP_ENABLED="false"
SUSPECT=""  # set to confirmed Step 3 hypothesis as module.path::function (fn known) or bare module.path — same shape as TARGET_MODULE
if [ -z "$SUSPECT" ]; then
    echo "⚠ SUSPECT not resolved — skipping Test Impact query and section"
    rm -f ${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}
elif [ "$CODEMAP_ENABLED" = "true" ] && command -v codemap-py >/dev/null 2>&1; then
    codemap-py query test-impact "$SUSPECT" 2>/dev/null | tee ${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}
else
    rm -f ${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}  # no query — fix falls back to its own live query
fi
```

Captured JSON carries `pytest_cmd`, `test_files`, top-level `stale`, `index.not_covered`. Written into diagnosis file (Step 4) under marked section so fix reuses a fresh result. Query returns `"error"` or empty → skip silently; fix re-queries.

## Step 4: Hand off to fix

Root cause confirmed. Transition to fix mode with diagnosis as input — fix's Step 1 pre-answered.

Emit handoff block:

```text
Root cause: [confirmed hypothesis from Step 3]
Suspect file(s): [files identified in Steps 1–2]
Evidence: [key signals that confirmed the hypothesis]
```

**Write diagnosis to file** before handing off — enables `/develop:fix` to skip Step 1 analysis via `--diagnosis <path>`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SLUG < "${TMPDIR:-/tmp}/dev-debug-slug-${CSID}" 2>/dev/null || SLUG=""  # Run slug block (Mode selection) — same name as the .temp/debug/ files
[ -n "$SLUG" ] || { echo "! BLOCKED — dev-debug-slug sentinel missing; re-run the Run slug block"; exit 1; }
# main tree: orig-root sentinel (worktree §Enter) or pwd — /develop:fix must reach this file
IFS= read -r _DIAG_BASE < "${TMPDIR:-/tmp}/dev-debug-orig-root-${CSID}" 2>/dev/null || _DIAG_BASE="$(pwd)"
[ -n "$_DIAG_BASE" ] || _DIAG_BASE="$(pwd)"
DIAG_FILE="$_DIAG_BASE/.plans/active/debug_${SLUG}.md"
mkdir -p "$_DIAG_BASE/.plans/active"
echo "$DIAG_FILE" | tee "${TMPDIR:-/tmp}/dev-debug-diag-file-${CSID}"  # later blocks (Test Impact append, handoff) reload it — bash state is lost between calls
```

Write `$DIAG_FILE` with this structure:

> Use one sentence for the confirmed root cause, and list the signals that confirm it under `Evidence`.

```markdown
# Debug Diagnosis: [symptom]

## Root Cause
[confirmed root cause]

## Suspect Files
- path/to/file.py — [reason]

## Evidence
- [signal 1 that confirmed hypothesis]
- [signal 2]

## Confidence
[high | medium | low]
```

**Append Test Impact section** — only when Step 3 captured a non-empty, non-error result (`${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}` present). fix reads this to skip re-querying. Records raw JSON plus index `scanned_at` so fix can verify handoff is not older than current index (freshness guard):

````bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TI_FILE="${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}"
IFS= read -r DIAG_FILE < "${TMPDIR:-/tmp}/dev-debug-diag-file-${CSID}" 2>/dev/null || DIAG_FILE=""
[ -n "$DIAG_FILE" ] || { echo "! BLOCKED — diagnosis file path not persisted; re-run the 'Write diagnosis to file' block"; exit 1; }
if [ -s "$TI_FILE" ] && ! grep -q '"error"' "$TI_FILE"; then
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
    PROJ=$(basename "$_ROOT")   # raw basename — scanner writes it verbatim, never sanitized
    _IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}/${PROJ}.json"   # root-anchored: skill may run from a subdir
    IDX_SCANNED_AT=$(grep -o '"scanned_at"[[:space:]]*:[[:space:]]*"[^"]*"' "$_IDX" 2>/dev/null | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    {
        echo ""
        echo "## Test Impact (codemap-py)"
        echo "<!-- reused by /develop:fix Step 3 when index_scanned_at still matches the live index and stale != true -->"
        echo "- index_scanned_at: ${IDX_SCANNED_AT:-unknown}"
        echo '```json'
        cat "$TI_FILE"
        echo '```'
    } >> "$DIAG_FILE"
fi
````

Hand off: `-> /develop:fix --diagnosis $DIAG_FILE`. Root cause already known — fix's Step 1 analysis complete.

**Worktree exit** — `WORKTREE_ENABLED=true`: diagnosis file already lives in main tree (§Deliverable). Follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append `Worktree` block to report. Follow-up `/develop:fix` then runs in main tree against main-tree `$DIAG_FILE`. Never auto-merge.

## Final Report

After root cause confirmed and handoff to `/develop:fix` complete, emit terminal summary:

> Keep `Root Cause` to one sentence; list the suspect files and key confirming signals.

```markdown
Root Cause: [confirmed root cause]
File(s): [suspect files]
Evidence: [key signals]
→ Handed off to /develop:fix --diagnosis $DIAG_FILE

## Confidence
**Score**: 0.NN — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- (-0.NN) [e.g., unverified alternative hypotheses, hypothesis only — not confirmed via test reproduction]

**Refinements**: N passes.
```

**Next step** — print as plain text, not a selectable prompt (skill has `disable-model-invocation: true`, no `Skill` tool in `allowed-tools`, so `/develop:fix` cannot be invoked automatically here). Substitute resolved `$DIAG_FILE` path: `-> /develop:fix --diagnosis $DIAG_FILE` (e.g. `/develop:fix --diagnosis .plans/active/debug_<slug>.md`) for user to copy-paste.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md ${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}  # clear contract + ledger — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

</workflow>

<notes>

## Anti-Rationalizations

<!-- Reference only — execution-dead at runtime; included for agent behavioral context -->

| Temptation | Reality |
| -- | -- |
| "I already know root cause from traceback" | Tracebacks show where, not why. Unverified assumptions produce fixes for wrong bug. |
| "Fix obvious — Step 2 pattern analysis overkill" | Obvious causes often symptoms. Pattern comparison reveals ordering, timing, or environment differences invisible in traceback. |
| "I'll apply fix here instead of handing off to `/develop:fix`" | Debug = investigation only. Mixing investigation + implementation conflates history, skips regression test gate. |
| "Low confidence fine — I'll try fix and see" | Fix without confirmed hypothesis = guess. Guesses produce fixes that pass tests but don't resolve underlying problem. |

<!-- Team spawn logic: see Flag parsing block above for team mode branch -->

</notes>
