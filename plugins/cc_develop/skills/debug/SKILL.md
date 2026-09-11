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

NOT for: production incidents without any CI run ID or local traceback (use `/foundry:investigate` (requires foundry plugin) for triage); `.claude/` config issues (use `/foundry:audit` (requires foundry plugin)); non-Python projects (JS/TS/Go/Rust) — toolchain assumes pytest; use language-native toolchain instead. CI-only failures ARE supported — pass `--ci-run <run-id or URL>` to use GitHub Actions logs as evidence source.

**Issue ID routing note**: issue mode selected when `--issue` flag present, or when argument (after other flags stripped) is a pure run of digits with an optional `#` prefix (e.g. `123` or `#123`). No numeric threshold. Pass `--issue <N>` to force issue mode for any argument.

</objective>

<compaction>

- Key boundary: after Steps 1+2 — evidence gathered and pattern analysis complete, before hypothesis gate (Step 3).
- Preserve: debug mode, CI run ID if set, evidence signals (issue body, test path), tried-hypotheses ledger (candidate causes + verdicts — refuted/ruled-out/open), --keep items.
- Refresh also after any Step 3 probe that rules out a hypothesis — so post-compact gate does not re-test refuted causes (loop guard).

</compaction>

<workflow>

<!-- Agent resolution: see _DEV_SHARED/agent-resolution.md (mounted by develop plugin init) -->

## Agent Resolution

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_DEV_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_shared_resolve.py" 2>/dev/null)  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
echo "$_DEV_SHARED" > "${TMPDIR:-/tmp}/dev-shared-${CSID}"  # cold resolve — every later block warm-reads this
# loads: compaction-contract.md
cat "$_DEV_SHARED/agent-resolution.md"
```

Contains: foundry check + fallback table. If foundry not installed: substitute each `foundry:X` with `general-purpose` per table. Agents this skill uses: `foundry:sw-engineer`, `foundry:challenger`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/task-hygiene.md"
```

## Project Detection

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/runner-detection.md"
```

Sets `$TEST_CMD` (full suite) and `$PYTEST_CMD` (pytest flags). Run at skill start.

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
KEEP_ITEMS=""
if [[ "$ARGUMENTS" =~ --keep[[:space:]]\"([^\"]+)\" ]]; then
    KEEP_ITEMS="${BASH_REMATCH[1]}"
fi
echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/dev-debug-keep-items-${CSID}"
rm -f .temp/state/skill-contract.md ${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}  # timeout: 5000
```

```bash
# timeout: 10000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_parse_args.py" \
    --skill debug --write-files "$ARGUMENTS"
# URL normalization + log fetching: §URL Normalization in ci-log-extract.md
```

## Worktree isolation

> loads: worktree-isolation.md

When `--worktree` set, run the investigation in an isolated git worktree so reproduction attempts (repro scripts, temp edits) can never mutate the main sources — **before** codemap detection or Step 1.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WORKTREE_ENABLED < "${TMPDIR:-/tmp}/dev-debug-worktree-${CSID}" 2>/dev/null; [ "$WORKTREE_ENABLED" = "true" ] || WORKTREE_ENABLED=false
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/worktree-isolation.md"
```

`WORKTREE_ENABLED=true` → follow §Enter (base off HEAD, `EnterWorktree(path=…)`). **Read-only skill** — obey §Deliverable: the diagnosis file is written to the **main tree** (`$_ORIG_ROOT`) at Step 4 so `/develop:fix` can read it. Else skip — run in main tree.

**Codemap resolve** — `CODEMAP_RAW` already written to `${TMPDIR:-/tmp}/dev-debug-codemap-${CSID}` (per-skill) and `${TMPDIR:-/tmp}/dev-codemap-raw-${CSID}` (legacy) by flag-parsing block above (via `dev_parse_args.py --skill debug --write-files`). Read per-skill path, then normalize via `codemap_resolve.py`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# skill-specific paths (dev-debug-codemap-*) avoid stale value from a prior feature --codemap run
CODEMAP_ENABLED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_codemap_gate.py" debug) || exit 1
```

> loads: codemap-gates.md

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/codemap-gates.md"
```

Follow Gate A and Gate B.

Downstream blocks read back: `IFS= read -r CHALLENGE_ENABLED < "${TMPDIR:-/tmp}/dev-challenge-enabled-${CSID}" 2>/dev/null || CHALLENGE_ENABLED=true`, `IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED=false`, `IFS= read -r TEAM_MODE < "${TMPDIR:-/tmp}/dev-team-mode-${CSID}" 2>/dev/null || TEAM_MODE=false`, `IFS= read -r CI_RUN_ID < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || CI_RUN_ID=""`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/ci-log-extract.md"
```

Follow §URL Normalization to set `CI_RUN_ID`. If `CI_RUN_ID` set, follow §Log Fetching and §Log Parsing to set `CI_LOG_EVIDENCE`; use it as evidence source in Step 1 instead of local pytest.

**Unsupported flag check** — after ALL supported flags extracted (including `--issue` and `--keep` from blocks above), scan `$ARGUMENTS` for remaining `--<token>` tokens not in supported list. Do NOT include `--issue` or `--keep` in "unknown" set — both are consumed by the mode-detect and keep-parse blocks above. Supported: `--no-challenge`, `--challenge`, `--team`, `--worktree`, `--ci-run`, `--issue`, `--repo`, `--codemap`, `--no-codemap`, `--keep`. If truly unknown token found: print `` ! Unknown flag(s): `--<token>`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Mode selection** — debug runs in one of two mutually-exclusive modes; set explicitly before any Step:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# strip flags first — "123 --no-challenge" would fail integer detection otherwise
ARGUMENTS_FOR_MODE_DETECT=$(echo "$ARGUMENTS" | sed -E 's/--no-challenge|--challenge|--team|--worktree|--ci-run[= ]?[^ ]+|--issue|--repo[= ]?[^ ]+|--no-codemap|--codemap|--keep +"[^"]+"//g' | xargs)
if [[ " $ARGUMENTS " == *" --issue "* ]] || [[ "$ARGUMENTS_FOR_MODE_DETECT" =~ ^#?[0-9]+$ ]]; then
    DEBUG_MODE="issue"
else
    DEBUG_MODE="symptom"
fi
echo "$DEBUG_MODE" > ${TMPDIR:-/tmp}/dev-debug-mode-${CSID}
```

Subsequent steps branch by `DEBUG_MODE`:

- **Issue mode**: Step 1 fetches issue body and extracts test path before invoking pytest; skip symptom-text pytest block. Stop after Step 4 (handoff) — do not run symptom-text branches.
- **Symptom mode**: Step 1 skips issue fetch; uses free-text symptom directly. Skip issue-mode pytest block entirely.

**If `TEAM_MODE=true`** — execute team investigation now in place of standard Steps 1-2. After team synthesis completes, run Steps 3-4 inline (hypothesis gate + handoff to fix) on winning hypothesis — do not return to standard Steps 1-2. Authoritative reading: team mode **replaces** Steps 1-2 (parallel hypothesis investigation supplants serial evidence gathering); Steps 3-4 still execute (inline within this block, not by looping back to standard workflow):

1. `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""; [ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"; cat "$_DEV_SHARED/preflight-helpers.md"` §Team Spawn Template. Confirm `[ROLE_PHRASE]` = symptom text (from `$ARGUMENTS` stripped of flags), `[FILE_SLUG]` = `debug-hypothesis`.
2. Run project detection (`export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""; [ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"; cat "$_DEV_SHARED/runner-detection.md"`) to set `$TEST_CMD` and `$PYTEST_CMD`.
3. Compute `TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)` and `mkdir -p ".temp/develop/$TS"`. Spawn 2-3 `foundry:sw-engineer` agents (model=opus) in parallel — each investigating one independent root-cause hypothesis. Use Team Spawn Template from preflight-helpers: replace `[ROLE_PHRASE]` with symptom, `[FILE_SLUG]` with `debug-hypothesis`, assign each agent a distinct hypothesis number N. Each agent writes full output to `.temp/develop/$TS/debug-hypothesis-N-$TS.md` (run-dir timestamp, matching preflight-helpers §Team Spawn Template) and returns compact JSON `{"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line description of hypothesis>"}`.
4. **Coordination**: lead broadcasts `{symptom: <description>, traceback: <key lines>}` to teammates before spawning. After all return, facilitate cross-challenge between competing analyses. Convergence rule: select hypothesis with most direct evidence (observable in code or logs); if truly tied, invoke `AskUserQuestion` presenting top 2 competing hypotheses.
5. **Synthesis trace (lead, inline — no spawn)**: after individual teammate reports, lead reads all teammate findings from `.temp/develop/$TS/debug-hypothesis-*.md` (2-3 files already on disk) and produces the unified cross-cutting trace map itself — entry point, modules crossed, state mutations, invariant violations across hypotheses. Write to `.temp/develop/$TS/debug-trace-synthesis.md`. A dedicated synthesis agent costs ~120,851 tok of fixed overhead for a read-and-merge the lead performs inline in fix's equivalent step — spawn nothing here.
6. Lead synthesises consensus root cause from the trace map + competing hypotheses. Run Steps 3-4 of standard workflow (hypothesis gate + hand off to fix) on winning hypothesis — execute those steps inline here; do not loop back through Steps 1-2. **Step 3 gate in team mode**: if convergence reached by synthesis agent (all hypotheses point to same root cause with high confidence), present converged hypothesis without a new user confirmation prompt — state "Team converged on root cause (no ambiguity)" and proceed directly to Step 4 handoff. Only invoke `AskUserQuestion` at Step 3 if competing hypotheses remain or convergence declared by default (tied evidence).

Health monitoring (CLAUDE.md §6): for each spawned agent, use a **per-agent sentinel** keyed on loop counter `$N` (not literal `N`). Loop over agent indices in actual bash:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
for N in 1 2 3; do
    touch "${TMPDIR:-/tmp}/debug-team-check-${N}-${CSID}"
done
```

Poll each independently every 5 min via `find .temp/develop/$TS -name "debug-hypothesis-${N}*" -newer ${TMPDIR:-/tmp}/debug-team-check-${N}-${CSID} | wc -l` where `$N` is actual agent index in loop variable — the `-name` scope is load-bearing: a directory-wide `find` marks every agent alive whenever any sibling writes, collapsing exactly the per-agent isolation these sentinels exist for. Poll only the indices actually spawned (2-hypothesis run → poll N=1,2 only; the third touched sentinel is a harmless unused file). A single shared sentinel collapses health isolation — stalled agent N=2 cannot be distinguished from active agent N=1. Hard cutoff 15 min no-file-activity per agent; mark timed-out agents with ⏱ in synthesis.

## Step 1: Understand the symptom

Collect all signals before forming any hypothesis.

**Structural context (codemap-py — only if `CODEMAP_ENABLED=true`)**: if index available, run before codebase exploration to pre-load blast-radius context for failing module:

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-debug-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
if [ "$CODEMAP_ENABLED" = "true" ]; then
    codemap-py query central --top 5 2>/dev/null
fi
```

After reading traceback or `$ARGUMENTS`, derive `TARGET_MODULE`: strip `src/`, `.py` suffix, replace `/` with `.` (e.g. `src/mypackage/auth.py` → `mypackage.auth`); capture the failing function name too, if known, as `FAILING_FN`. `TARGET_MODULE` is a **substitution token** — resolve into the shell variable before the block below. Do NOT execute with literal `<TARGET_MODULE>` — bash would interpret `<` as stdin redirect:

```bash
# resolve TARGET_MODULE/FAILING_FN first, e.g. TARGET_MODULE=mypackage.auth; FAILING_FN=validate
# timeout: 10000
if [ -z "$TARGET_MODULE" ]; then
    echo "⚠ TARGET_MODULE not resolved — skipping codemap rdeps/fn-blast query"
else
    codemap-py query rdeps "$TARGET_MODULE" 2>/dev/null
    [ -n "$FAILING_FN" ] && codemap-py query fn-blast "$TARGET_MODULE::$FAILING_FN" 2>/dev/null  # v3 index only
fi
```

If codemap-py results returned: prepend `## Structural Context (codemap-py)` block to foundry:sw-engineer spawn prompt (Step 1). Callers of failing module = likely affected paths to verify after fix. fn-blast shows transitive callers — high-depth callers are regression risk.

**Issue-number mode first** — if `$ARGUMENTS` is issue number, fetch issue body and extract test path BEFORE invoking pytest:

```bash
# timeout: 6000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# wrapper: cross-repo branch from dev-upstream, persists body to dev-issue-body-${CSID} for next block (avoids re-running gh)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_issue_fetch_wrap.py" debug "$ARGUMENTS"
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

Run pytest with extracted path (empty `$TEST_PATH` → full suite). `$TEST_PATH` stays unquoted so an empty value collapses to no argument rather than an empty one:

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
IFS= read -r TEST_PATH  < "${TMPDIR:-/tmp}/dev-debug-test-path-${CSID}" 2>/dev/null || TEST_PATH=""
if [ -z "$PYTEST_CMD" ]; then
    echo "! PYTEST_CMD unresolved — re-run §Project Detection (runner-detection.md); an empty command would exit 127 and be misread as a reproduced bug"
else
    $PYTEST_CMD --tb=long ${TEST_PATH} -v 2>&1 | tail -60
    GATE_EXIT=${PIPESTATUS[0]}
    echo "$GATE_EXIT" > "${TMPDIR:-/tmp}/dev-gate-exit-${CSID}"
    if [ "$GATE_EXIT" -ne 0 ]; then
        echo "Bug reproduced — tests fail. Proceed to fix."
    else
        echo "Tests pass — bug may not be reproducible via pytest; check symptom directly."
    fi
fi
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

**Symptom-text mode** — if `$ARGUMENTS` is free-text, skip issue fetch + extraction; locate failing test path from symptom directly. `<test_path>` is a **substitution token** — resolve into shell variable `$TEST_PATH` first (via Grep against symptom keywords or heuristic file search), then use `$TEST_PATH` in pytest call. Do NOT execute with literal `<test_path>` string — bash would interpret `<` as stdin redirect from a file named `test_path`:

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
TEST_PATH=""   # REPLACE with the resolved failing test path, e.g. $(grep -rlE '<symptom keyword>' tests/ --include='*.py' | head -1); empty → full suite
echo "$TEST_PATH" > "${TMPDIR:-/tmp}/dev-debug-test-path-${CSID}"  # persist — later blocks consume it
if [ -z "$PYTEST_CMD" ]; then
    echo "! PYTEST_CMD unresolved — re-run §Project Detection (runner-detection.md); an empty command would exit 127 and be misread as a reproduced bug"
else
    $PYTEST_CMD --tb=long ${TEST_PATH} -v 2>&1 | tail -60
    GATE_EXIT=${PIPESTATUS[0]}
    echo "$GATE_EXIT" > "${TMPDIR:-/tmp}/dev-gate-exit-${CSID}"
    if [ "$GATE_EXIT" -ne 0 ]; then
        echo "Bug reproduced — tests fail. Proceed to fix."
    else
        echo "Tests pass — bug may not be reproducible via pytest; check symptom directly."
    fi
fi
```

**Claim-validation gate** — before debugging, validate that user's expectation is itself correct. A bug report always contains an implicit or explicit claim: "X should behave like Y". Claim may be wrong — misread docs, misunderstood API contract, incorrect formula, outdated assumption. Fixing a correct implementation to match a wrong expectation wastes effort and introduces regressions.

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

Use Grep (pattern: failing symbol, class, or error keyword) to trace call path from entry point to failure site. Path hint: use `src/` if exists, else search from project root (`.`).

Spawn **foundry:sw-engineer** agent to map execution path and produce:

- Entry point to failure: which modules does call cross?
- What state mutated along the way?
- What invariant violated at failure point?
- Any recent commit touching this path (from git log output)

**Scope gate**: if root cause spans 3+ modules, flag complexity smell. Use `AskUserQuestion` to present scope concern before proceeding, with options: "Narrow scope (Recommended)" / "Proceed anyway".

Present agent's analysis summary before proceeding.

**Flaky-test branch** — if symptom is intermittent (passes alone, fails in full suite): run binary-search isolation. `<failing-test-node-id>` is a **substitution token** — before executing this block, resolve failing test node ID from `$ARGUMENTS` or from prior pytest output (captured in a shell variable, e.g. `FAILING_TEST_NODE=tests/foo.py::test_bar`), then substitute literal node ID into command. Do NOT execute with literal `<failing-test-node-id>` string — bash would interpret `<` as stdin redirect:

```bash
# resolve FAILING_TEST_NODE first (bash reads literal <...> as redirect):
# FAILING_TEST_NODE=$(echo "$ARGUMENTS" | grep -oE 'tests?/[^[:space:]]+::test_[^[:space:]]+' | head -1)
if [ -z "$FAILING_TEST_NODE" ]; then
    echo "⚠ FAILING_TEST_NODE not resolved — cannot run polluter isolation; surface failing test node ID first"
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/find-polluter.py" "$FAILING_TEST_NODE"  # timeout: 60000
fi
```

Output names polluting upstream test. `find-polluter.py` ships in this plugin's own `bin/` (kept identical to foundry's canonical by `propagate_shared.py`), so this works on a develop-only install. Run only when CI shows non-deterministic failure pattern.

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

**Record candidates (loop guard)** — as each candidate cause identified, append it to hypothesis ledger with verdict `open`. Ledger inlined into compaction contract at boundary below, so a mid-investigation compaction never loses which causes were already weighed. Verdict values: `open` · `refuted (challenger)` · `ruled-out (probe)`.

```bash
# ledger survives compaction via contract — avoids re-testing refuted causes mid-investigation
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo "<candidate cause> :: open" >> ${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}
```

## Challenger gate

**Decision — three states** (default is NOT "skip": it runs on substantial root causes and auto-skips only narrow ones):

1. `--no-challenge` (`CHALLENGE_ENABLED=false`) → **skip gate entirely**, any size.
2. else `--challenge` (`IFS= read -r CHALLENGE_FORCED < "${TMPDIR:-/tmp}/dev-challenge-forced-${CSID}" 2>/dev/null || CHALLENGE_FORCED=false` = `true`) → **always run**, even on a narrow root cause.
3. else **default** → **run when root cause is substantial** (spans multiple files, a larger change, or touches public API); **auto-skip when narrow** (single file, ≲50 lines, no API change) — hypothesis simple enough to proceed directly.

Both flags exist because they cover opposite regimes: `--no-challenge` suppresses gate on substantial cases where it would otherwise fire; `--challenge` forces it on narrow cases where it would otherwise auto-skip.

Spawn `foundry:challenger` with pattern analysis from Step 2 (differences between working/broken paths, candidate causes):

> "Review pattern analysis and candidate root causes. Challenge across all 5 dimensions: Assumptions, Missing Cases, Security Risks, Architectural Concerns, Complexity Creep. Apply mandatory refutation step."

Parse result — update hypothesis ledger (`${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}`) with each candidate's verdict as you parse:

- **Blockers found** → STOP. Present findings. Incorporate challenger's surviving challenges into hypothesis list before Step 3 gate. Mark any candidate the challenger refuted `:: refuted (challenger)` in ledger.
- **Concerns only** → add as alternative hypotheses in Step 3; append each new concern to ledger as `:: open (alt)`; continue.
- **No findings / all refuted** → proceed.

```bash
# compaction boundary (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEBUG_MODE < "${TMPDIR:-/tmp}/dev-debug-mode-${CSID}" 2>/dev/null || _DEBUG_MODE="symptom"
IFS= read -r _CI_RUN < "${TMPDIR:-/tmp}/dev-ci-run-id-${CSID}" 2>/dev/null || _CI_RUN=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/dev-debug-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_TRIED=$(head -6 "${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}" 2>/dev/null)  # cap keeps contract ≤12 lines
_PRESERVE="mode=$_DEBUG_MODE, ci-run=${_CI_RUN:-none}"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/write_skill_contract.py" "develop:debug" "hypothesis+handoff (after evidence gathered and pattern analysis)" ".plans/active/" "$_PRESERVE" "state hypothesis with evidence (Step 3) → confirm root cause → write diagnosis → handoff to /develop:fix. Skip any candidate marked refuted/ruled-out in the tried list below." "tried (do NOT re-test refuted/ruled-out)" "$_TRIED"  # timeout: 5000
```

## Step 3: Hypothesis and gate

State root cause hypothesis explicitly before writing any code:

```text
Root cause: <one sentence — what is wrong and why>
Evidence for: [signals that support this]
Evidence against: [anything that contradicts or remains unexplained]
Confidence: high / medium / low
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""  # timeout: 5000
[ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
cat "$_DEV_SHARED/premise-grounding.md"
```

§Premise Grounding Gate. Apply using **debug** context from Skill contexts table. Run before presenting hypothesis — any ungrounded premise in hypothesis produces a fix that addresses wrong mechanism.

**Gate**: present hypothesis to user, wait for confirmation or challenge before proceeding to Step 4. Wrong hypothesis produces fix that passes tests but doesn't resolve underlying problem.

If confidence low: propose targeted probe (minimal script, added log statement, single assertion) to gather missing signal — run before committing to fix. If a probe rules out current hypothesis, append `<cause> :: ruled-out (probe)` to `${TMPDIR:-/tmp}/dev-debug-hypotheses-${CSID}` and re-run boundary contract block above before re-hypothesizing — keeps loop guard current so ruled-out cause not revisited.

**Test impact (codemap-py) — hypothesis confirmed** — root cause now names a suspect module (and often a function). Query affected test set once here so `/develop:fix` reuses it instead of re-querying. Gated on `CODEMAP_ENABLED` + `codemap-py query` availability (same gate as Step 1). `SUSPECT` is a **substitution token** — assign it in the block below as the confirmed hypothesis in dotted qname form: `module.path::function` (fn known) or bare `module.path` (module-level), same derivation as `TARGET_MODULE` (Step 1: strip `src/`, drop `.py`, `/` → `.`):

```bash
# timeout: 8000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/dev-debug-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
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

Captured JSON carries `pytest_cmd`, `test_files`, top-level `stale`, and `index.not_covered`. Written into diagnosis file (Step 4) under a marked section so fix can reuse a fresh result. Query returns `"error"` or empty → skip silently; fix re-queries.

## Step 4: Hand off to fix

Root cause confirmed. Transition to fix mode with diagnosis as input — fix's Step 1 pre-answered.

```bash
# timeout: 5000
if [ ! -f "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/skills/fix/SKILL.md" ]; then
    echo "⚠ /develop:fix not found — partial install detected; diagnosis file will be written but handoff cannot be invoked automatically"
fi
```

Emit handoff block:

```text
Root cause: <confirmed hypothesis from Step 3>
Suspect file(s): <files identified in Steps 1-2>
Evidence: <key signals that confirmed the hypothesis>
```

**Write diagnosis to file** before handing off — enables `/develop:fix` to skip Step 1 analysis via `--diagnosis <path>`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
SLUG=$(echo "$ARGUMENTS" | tr ' ' '\n' | grep -v '^--' | grep -v '^[0-9]\+$' | head -4 | tr '\n' '-' | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]-' | sed 's/-$//'); [ -z "$SLUG" ] && SLUG="unnamed-$(date +%s)"
# grep -v strips bare numeric tokens (CI run IDs) — avoids filenames like debug_12345678.md
# main tree: orig-root sentinel (worktree §Enter) or pwd — /develop:fix must reach this file
IFS= read -r _DIAG_BASE < "${TMPDIR:-/tmp}/dev-debug-orig-root-${CSID}" 2>/dev/null || _DIAG_BASE="$(pwd)"
[ -n "$_DIAG_BASE" ] || _DIAG_BASE="$(pwd)"
DIAG_FILE="$_DIAG_BASE/.plans/active/debug_${SLUG}.md"
mkdir -p "$_DIAG_BASE/.plans/active"
```

Write `$DIAG_FILE` with this structure:

```markdown
# Debug Diagnosis: <symptom>

## Root Cause
<one sentence — confirmed hypothesis>

## Suspect Files
- path/to/file.py — <reason>

## Evidence
- <signal 1 that confirmed hypothesis>
- <signal 2>

## Confidence
<high|medium|low>
```

**Append Test Impact section** — only when Step 3 captured a non-empty, non-error result (`${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}` present). fix reads this to skip re-querying. Records raw JSON plus index `scanned_at` so fix can verify handoff is not older than current index (freshness guard):

````bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TI_FILE="${TMPDIR:-/tmp}/dev-debug-test-impact-${CSID}"
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

**Worktree exit** — if `WORKTREE_ENABLED=true`: the diagnosis file already lives in the main tree (§Deliverable). Follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block to the report. The follow-up `/develop:fix` then runs in the main tree against the main-tree `$DIAG_FILE`. Never auto-merge.

## Final Report

After root cause confirmed and handoff to `/develop:fix` complete, emit terminal summary:

```markdown
Root Cause: <one sentence>
File(s): <suspect files>
Evidence: <key signals>
→ Handed off to /develop:fix --diagnosis $DIAG_FILE

## Confidence
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [e.g., unverified alternative hypotheses, hypothesis only — not confirmed via test reproduction]

**Refinements**: N passes.
```

**Next step** — print as plain text, not a selectable prompt (this skill has `disable-model-invocation: true` and no `Skill` tool in `allowed-tools`, so `/develop:fix` cannot be invoked automatically here). Substitute the resolved `$DIAG_FILE` path: `-> /develop:fix --diagnosis $DIAG_FILE` (e.g. `/develop:fix --diagnosis .plans/active/debug_<slug>.md`) for the user to copy-paste.

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
