<!-- file: scope-context.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:review -->

## Step 1: Identify scope and context (run in parallel for PR mode)

Flags, `CLEAN_ARGS`, and `DIRECT_PATH_MODE` were parsed in Step 0 — reuse those values here.

**Unsupported flag check** — after all supported flags extracted, scan `$ARGUMENTS` for remaining `--<token>` tokens. Found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--reply`, `--no-challenge`, `--codemap`, `--no-codemap`, `--worktree`, `--full`, `--fill-gaps`, `--roles`, `--candidates`, `--keep`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Frozen PR source** — applies to the parent and every reviewer, verifier, bridge and consolidator. Read repository source with `git show <PR_HEAD_SHA>:<path>` and search with `git grep <pattern> <PR_HEAD_SHA> -- <paths>`. Never infer PR behavior from the invoking tree. Working-tree reads, codemap queries and targeted test runs are permitted only in a clean checkout whose `HEAD` equals the snapshot's full `headRefOid`; verify that equality and `git status --porcelain` immediately before each run. Otherwise use frozen Git reads and report execution as unavailable, or use `--worktree`. Load skill/protocol files and analysis artifacts from their resolved paths as usual.

**Worktree isolation** — when `WT_ENABLED=true` and this is a PR review: create a detached worktree at the fetched PR SHA, never branch from the invoking HEAD. Run this block before codemap preparation or any repository-source read:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
[ "${DIRECT_PATH_MODE:-false}" = "true" ] && exit 0
PR_HEAD_SHA=$(jq -er '.headRefOid' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — frozen PR head unavailable"; exit 1; }
_ORIG_ROOT=$(git rev-parse --show-toplevel) || exit 1
printf '%s\n' "$_ORIG_ROOT" > "${TMPDIR:-/tmp}/oss-review-orig-root-${CSID}"
if [ "${WT_ENABLED:-false}" = "true" ]; then
    IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" || exit 1
    mkdir -p "$_ORIG_ROOT/.claude/worktrees" || exit 1
    WT=$(mktemp -d "$_ORIG_ROOT/.claude/worktrees/oss-review-${CLEAN_ARGS}-${PR_HEAD_SHA:0:8}-XXXXXX") || exit 1
    git worktree add --detach "$WT" "$PR_HEAD_SHA" || { echo "! BLOCKED — detached PR worktree creation failed"; exit 1; }  # timeout: 15000
    printf '%s\n' "$WT" > "${TMPDIR:-/tmp}/oss-review-wt-${CSID}"
fi
```

`WT_ENABLED=true` → `EnterWorktree(path=<exact oss-review-wt sentinel path>)`, then verify `git rev-parse HEAD` equals `PR_HEAD_SHA`. Keep deliverables in the original root recorded above; at the follow-up gate use `ExitWorktree(action="keep")` and report the path and detached SHA. Never remove the worktree automatically. Without `--worktree`, use the frozen Git reads above. If HEAD differs or tracked/untracked source changes exist, disable codemap for this run (strict `--codemap` → `! BLOCKED`); never build or query an index for the invoking base tree and attribute it to the PR.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# loads: detect_codemap.py — consumers: resolve/SKILL.md, review/SKILL.md
_DETECT_CODEMAP="$PLUGIN_ROOT/bin/detect_codemap.py"
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
[ "${DIRECT_PATH_MODE:-false}" = "true" ] && exit 0
# codemap flags parsed inside the script: one argv slot, shlex-tokenised (same idiom as resolve)
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
PR_HEAD_SHA=$(jq -er '.headRefOid' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — frozen PR head unavailable"; exit 1; }
_TREE_SHA=$(git rev-parse HEAD) || { echo "! BLOCKED — checkout identity unavailable"; exit 1; }
_TREE_DIRTY=$(git status --porcelain) || { echo "! BLOCKED — checkout status unavailable"; exit 1; }
if [ "$_TREE_SHA" != "$PR_HEAD_SHA" ] || [ -n "$_TREE_DIRTY" ]; then
    IFS= read -r _STRICT_CODEMAP < "${TMPDIR:-/tmp}/oss-review-codemap-${CSID}" 2>/dev/null || _STRICT_CODEMAP=false
    [ "$_STRICT_CODEMAP" != "true" ] || { echo "! BLOCKED — strict codemap requires a clean checkout of the frozen PR head — rerun with --worktree"; exit 1; }
    python "$_DETECT_CODEMAP" --prefix review --force-off 2>&1  # timeout: 5000
else
    python "$_DETECT_CODEMAP" --prefix review --arguments "$ARGUMENTS" 2>&1  # timeout: 5000
fi
[ $? -ne 0 ] && { echo "! BLOCKED — codemap strict mode requested but codemap not installed or index missing"; exit 1; }
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/review-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/review-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="off"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/review-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 41)
IFS= read -r CODEMAP_FORCE_OFF < "${TMPDIR:-/tmp}/review-codemap-forced-off-${CSID}" 2>/dev/null || CODEMAP_FORCE_OFF="false"
[ "$CODEMAP_FORCE_OFF" = "true" ] || python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_SHARED/codemap-gates.md"  # timeout: 5000
```

**Codemap gates** — follow `codemap-gates.md` loaded above, including its preparation, preference, failure and fallback rules; apply the frozen-source restriction above before any query, and never model-invoke `codemap-py:scan-codebase` (`disable-model-invocation: true`, user-slash-only).

> `file-handoff-protocol.md`, `foundry--cross-validation-protocol.md` and `codex-delegation.md` (Steps 5/7/consolidator) ship in **this** plugin's `_shared`, kept identical to foundry's canonical by `propagate_shared.py` (the `foundry--` prefix marks a propagated copy — a plugin-local file can never collide with it). No separate resolution needed — `$_OSS_SHARED` from Step 0 covers them, and none of those steps degrade when foundry is absent.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
if [ "$DIRECT_PATH_MODE" = "false" ]; then
    if [ -z "$CLEAN_ARGS" ] || ! [[ "$CLEAN_ARGS" =~ ^[0-9]+$ ]]; then
        echo "Error: PR number required. Usage: /oss:review <PR number> [--reply] [--no-challenge]"
        exit 1
    fi
    # all from the Step-0 snapshot — no network
    IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
    [ -s "$SNAP_DIR/pr-meta.json" ] || { echo "! BLOCKED — PR snapshot missing; rerun the Step-0 snapshot block"; exit 1; }
    CHANGED_FILES=$(cat "$SNAP_DIR/files.txt" 2>/dev/null)  # reused by codemap block
    jq '{title,body,url,labels:[.labels[].name],milestone,reviews:(.reviews|length)}' "$SNAP_DIR/pr-meta.json"  # timeout: 5000
    cat "$SNAP_DIR/checks.json"  # timeout: 3000
    # scope-detection.md/SCOPE block run in fresh shells — w/o these sentinels: empty inputs, file-scope guard aborts, FIX→REFACTOR override never fires
    PR_LABELS=$(jq -r '[.labels[].name] | join(",")' "$SNAP_DIR/pr-meta.json" 2>/dev/null)  # timeout: 5000
    PR_TITLE=$(jq -r .title "$SNAP_DIR/pr-meta.json" 2>/dev/null)  # timeout: 5000
    printf '%s\n' "$CHANGED_FILES" > "${TMPDIR:-/tmp}/oss-review-changed-files-${CSID}"
    printf '%s\n' "$PR_LABELS" > "${TMPDIR:-/tmp}/oss-review-pr-labels-${CSID}"
    printf '%s\n' "$PR_TITLE" > "${TMPDIR:-/tmp}/oss-review-pr-title-${CSID}"
# CI STATUS: jq over the snapshot — no tab-literal quoting, no `grep -P` (absent on BSD/macOS); `bucket` is gh's own pass/fail/pending classification
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
# checks-required.json = merge-blocking set; without required checks, use the full set.
CI_CHECKS_FILE="$SNAP_DIR/checks-required.json"
_REQUIRED_COUNT=$(jq -er 'if type == "array" then length else error("expected checks array") end' "$CI_CHECKS_FILE") || { echo "! BLOCKED — required CI snapshot unavailable"; exit 1; }
[ "$_REQUIRED_COUNT" -gt 0 ] || CI_CHECKS_FILE="$SNAP_DIR/checks.json"
CI_FAILING_CHECKS=$(jq -r '[.[]|select(.bucket=="fail")|.name]|join(", ")' "$CI_CHECKS_FILE") || { echo "! BLOCKED — CI snapshot unavailable"; exit 1; }  # timeout: 5000
CI_COUNTS=$(jq -r '"\([.[]|select(.bucket=="pass")]|length)/\(length)"' "$SNAP_DIR/checks.json") || { echo "! BLOCKED — CI counts unavailable"; exit 1; }  # timeout: 5000
if [ -n "$CI_FAILING_CHECKS" ]; then CI_RED=true; else CI_RED=false; fi
# Stage-2 gate + consolidator run in fresh shells — w/o sentinels CI_RED reads unset, red CI never blocks
printf '%s\n' "$CI_RED" > "${TMPDIR:-/tmp}/oss-review-ci-red-${CSID}"
printf '%s\n' "$CI_FAILING_CHECKS" > "${TMPDIR:-/tmp}/oss-review-ci-failing-${CSID}"
printf '%s\n' "$CI_COUNTS" > "${TMPDIR:-/tmp}/oss-review-ci-counts-${CSID}"
echo "CI_RED=$CI_RED FAILING=[$CI_FAILING_CHECKS] COUNTS=$CI_COUNTS"
fi
```

**CI STATUS** (PR mode only): the block's last stdout line is the result — run the block verbatim, never hand-compose a checks parse. `CI_RED=true`: print `⚠ CI is red: [list failing check names] — review proceeds; status noted in report header.` Continue to Steps 2–8 regardless. Expand `$CI_RED`, `$CI_FAILING_CHECKS` and `$CI_COUNTS` to literal values in the consolidator spawn prompt (Step 5).

### File scope detection

<!-- loads: modes/scope-detection.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload REVIEW_SKILL_DIR (Check 41: fresh shell)
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
cat "$REVIEW_SKILL_DIR/modes/scope-detection.md"  # timeout: 5000
```

Follow above and execute its bash blocks inside the `DIRECT_PATH_MODE = "false"` guard. Sets `PY_FILES`, `DOC_FILES`, `CICD_FILES`, `OTHER_CODE_FILES`, `CICD_ONLY_MODE`, `DOCS_ONLY_MODE`, `DOCS_CICD_MODE`; persists flags to `${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}` for reload in Step 2.

### Scope pre-check

**DOCS_TYPING mode** (`DOCS_TYPING_MODE=true`): no Python logic files changed — only docs (md/rst) and/or `.pyi` type stubs (an annotation-only edit to a `.py` file is indistinguishable from logic mechanically, so it stays CODE). `.txt` and `.ipynb` are not docs: a requirements manifest or a notebook is code and gets the qa/OWASP path. Spawn: `foundry:doc-scribe` only; challenger disabled by Step 0; skip all other agents. Proceed directly to agent launch.

**TESTS_CI mode** (`TESTS_CI_MODE=true`): test files, `tox.ini` and `Makefile` only. GitHub Actions and other YAML config never qualify — they stay CODE so File scope detection routes workflows to `oss:cicd-steward` and the CI status, acceptance gate and challenger still run. Spawn: `foundry:qa-specialist` + `foundry:linting-expert`; challenger disabled by Step 0; skip all other agents. Proceed directly to agent launch.

**CI/CD-only mode** (`CICD_ONLY_MODE=true`): no `.py`/`.md`/`.rst` and no `OTHER_CODE_FILES` (notebook, manifest, config). Spawn: `oss:cicd-steward` + Agent 1 + pinned Agent 2 + Agent 7 (if `CHALLENGE_ENABLED=true`) + Codex; skip Agents 3–6. Proceed directly to agent launch.

**Docs-only mode** (`DOCS_ONLY_MODE=true`): no `.py`, no CI/CD and no `OTHER_CODE_FILES`. **foundry:doc-scribe (Agent 4) leads** — Agent 1 explicitly skipped (NOT for docs clause); linked-issue spawns also skip Agent 1. Spawn: Agent 4 + Agent 7 (if `CHALLENGE_ENABLED=true`) + Codex; skip Agents 1, 2, 3, 5, 6. Proceed directly to agent launch.

**Docs + CI/CD mode** (`DOCS_CICD_MODE=true`): no Python and no `OTHER_CODE_FILES`. Spawn: `oss:cicd-steward` (Agent 8) + pinned Agent 2 + `foundry:doc-scribe` (Agent 4) + Agent 7 (if `CHALLENGE_ENABLED=true`) + Codex; skip Agents 1, 3, 5, 6. Proceed directly to agent launch.

Before spawning agents (Python mode only — all three mode flags false), classify diff:

- Count files changed, lines added/removed, new classes/modules
- Classify: **FIX** (\<3 files, \<50 lines — a label only; review depth comes from `IMPACT_TIER`, never size), **REFACTOR** (internal restructure, no new public API), **FEATURE** (new public API or module), **CHORE** (deps, config, tooling — no logic changes), or **MIXED**
- **Short-diff multi-concern refactors**: FIX heuristic classifies by diff size, not intent. Override FIX → REFACTOR when PR labels include `perf`, `performance`, `optimization`, `refactor`, `architecture`, `cleanup` OR commit message keywords `refactor:`, `perf:`, `rewrite` OR diff touches different modules. Reuse `PR_LABELS` and `PR_TITLE` from the snapshot sentinels above. Small-diff perf refactors are exactly the case FIX would silently mishandle.
- **Complexity smell**: 8+ files changed OR `PY_LOC_DELTA >400` → note in report header

Assign `SCOPE` shell variable so the `EXPECTED` array (Step 2 health monitor) can branch on it without comparing to an undefined value:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
# rehydrate Step1 inputs (Check 41) — PY_FILES lived in scope-detection.md's shell
CHANGED_FILES=$(cat "${TMPDIR:-/tmp}/oss-review-changed-files-${CSID}" 2>/dev/null)
PY_FILES=$(echo "$CHANGED_FILES" | grep '\.py$' || true)
IFS= read -r PR_LABELS < "${TMPDIR:-/tmp}/oss-review-pr-labels-${CSID}" 2>/dev/null || PR_LABELS=""
IFS= read -r PR_TITLE < "${TMPDIR:-/tmp}/oss-review-pr-title-${CSID}" 2>/dev/null || PR_TITLE=""
PY_FILE_COUNT=$(echo "$PY_FILES" | grep -c . 2>/dev/null)
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
# PY_LOC_DELTA = total churn, not net — renames give >0 at net 0; label/keyword override handles it
PY_LOC_DELTA=$(awk '/^diff --git /{f=($0 ~ /\.py(\"?)$/); h=0; next} /^@@ /{h=1; next} f && h && /^[+-]/{c++} END{print c+0}' "$SNAP_DIR/pr.diff")  # timeout: 5000
# new API surface: added lines inside non-test __init__.py sections of the snapshot diff
NEW_API_LINES=$(awk '/^diff --git /{f=($0 ~ /\/__init__\.py(\"?)$/ && $0 !~ /\/(tests?|test_[^\/]*)\//); h=0; next} /^@@ /{h=1; next} f && h && /^\+/{c++} END{print c+0}' "$SNAP_DIR/pr.diff")  # timeout: 5000

# pure config/deps changes (no .py logic changes)
NON_CONFIG_PY=$(echo "$PY_FILES" | grep -vE '(pyproject\.toml|setup\.cfg|setup\.py|requirements.*\.txt|conftest\.py)' || true)

SCOPE=$(python "$PLUGIN_ROOT/bin/classify_pr_scope.py" --py-files "$PY_FILE_COUNT" --loc-delta "$PY_LOC_DELTA" --new-api-lines "$NEW_API_LINES" --labels "$PR_LABELS" --title "$PR_TITLE" 2>/dev/null)  # timeout: 10000
echo "→ SCOPE=$SCOPE (py_files=$PY_FILE_COUNT, py_loc=$PY_LOC_DELTA, new_api=$NEW_API_LINES)"

# persist — Step2 ranking + consolidator <SCOPE> substitution run in separate blocks
# notebooks execute code: keep qa-specialist (OWASP) like a dependency manifest
echo "$CHANGED_FILES" | grep -qE '(^|/)(requirements.*\.txt|pyproject\.toml|package.*\.json|Pipfile|poetry\.lock|setup\.cfg|.*\.lock|.*\.ipynb)$' && CHORE_DEPS=true || CHORE_DEPS=false
_REVIEW_SCOPE_FILE="${TMPDIR:-/tmp}/oss-review-scope-${CLEAN_ARGS}-${CSID}"
{
    echo "SCOPE=$SCOPE"
    echo "CHORE_DEPS=$CHORE_DEPS"
} > "$_REVIEW_SCOPE_FILE"
# fail-safe default — codemap block overrides with review_impact_tier.py verdict; skipped/absent codemap keeps FULL
echo "FULL · impact unknown: codemap unavailable" > "${TMPDIR:-/tmp}/oss-review-impact-${CLEAN_ARGS}-${CSID}"
```

**Impact tier** — review depth follows the code path the change sits on, never its line or file count. A few-line change to a private helper on the main feature path can break the main user story; a larger tweak to a plotting helper reaches nobody. `review_impact_tier.py` (codemap `diff-impact` + `fn-blast`) prints `FULL` or `LIGHT` plus reason:

- `FULL` — any changed symbol, public or private, reaches a public function outside tests/leaf modules (main path), or a changed main-path module has ≥5 importers, or a module-level main-path change. **Unknown impact (codemap off, query failed, unmapped file) = `FULL`.**
- `LIGHT` — every change and every transitive caller stays in tests, leaf modules (`viz`, `plot`, `examples`, `docs`, `notebooks`), or uncalled private code.

`conftest.py`, `docs/conf.py`, setup/release/build entrypoints and `scripts/` are always `FULL`.

Step 2's codemap-propagation block prints `IMPACT_TIER=` — apply the skips below from that value (codemap runs after this block). `IMPACT_TIER` gates only perf+arch for FIX; challenger, Codex and pinned qa-specialist run at either tier.

Skip optional agents by classification:

- FIX scope → skip Agent 3 (perf-optimizer), Agent 6 (solution-architect) **only when `IMPACT_TIER=LIGHT`**; `FULL` → keep both (they enter relevance ranking)
- REFACTOR scope → keep all agents; perf-optimizer runs to verify new structure isn't slower
- FEATURE/MIXED → spawn all agents, plus Agent 0 (blind-solve) — see §Agent 0 below
- CHORE scope → spawn Agents 1, 2, 4, 5, 7 (challenger, if `CHALLENGE_ENABLED=true`), Codex (if available); skip Agents 3, 6. Agent 2 remains pinned for security checks with or without dependency changes.

### Structural context + review pre-flight (codemap-py — only if `CODEMAP_ENABLED=true`)

**Skip entire section if `CODEMAP_ENABLED=false`** — sets `codemap_available=false` for downstream agent prompts; agents fall back to file reads.

<!-- loads: modes/codemap-context.md -->

> loads: modes/codemap-context.md

`CODEMAP_ENABLED=true`:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload REVIEW_SKILL_DIR (Check 41: fresh shell)
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$REVIEW_SKILL_DIR/modes/codemap-context.md"  # timeout: 5000
```

Follow above and execute its contents — stages `codemap_available` and `$CODEMAP_CONTEXT_STAGE` to TMPDIR (Step 2 copies into `$RUN_DIR/codemap-context.md`) and defines the Step-2 spawn-prompt substitution rules. `CODEMAP_ENABLED=false`: skip; agents fall back to file reads.

### Linked issue analysis (PR mode only)

`ISSUE_NUMS` = issue refs in the PR body (`Closes #N`, `Fixes #N`, `Resolves #N`, `refs #N` — case-insensitive), cap 3. The acceptance-gate block below computes it from the snapshot body and prints it as `issue_nums:` — read it from there; it runs before Step 2.

`ISSUE_NUMS` non-empty AND `PR_TYPE=CODE` AND `DOCS_ONLY_MODE`/`DOCS_CICD_MODE` not true: spawn ONE **foundry:sw-engineer** (root-cause analysis, not doc-scribe's remit) covering ALL linked issues in Step 2 alongside Codex (one spawn, not one per issue — fixed per-spawn overhead, Step 2 Spawn-count gate). The issue agent, per issue N: fetch `gh issue view <N> --json title,body,comments,state,labels` + `gh issue view <N> --comments`; produce `/oss:analyse`-style output (Summary, Root Cause Hypotheses top 3, Code Evidence); its prompt carries verbatim: "Read-only investigation: never edit, create or delete tracked files and never run `git stash`, `git checkout`, `git reset` or `git restore` — sibling reviewers share this working tree; run reproduction probes in a `mktemp -d` copy or under the run directory; write only your analysis files." (run directory here: `$RUN_DIR`); write each analysis to its own `$RUN_DIR/issue-<N>.md` (per-issue files are load-bearing — consumed by Agent 1, the consolidator, and the monitor list); return only a JSON array, one element per issue: `[{"status":"done","issue":N,"root_cause":"<one-line>","file":"$RUN_DIR/issue-<N>.md","confidence":0.N}, …]`.

`ISSUE_NUMS` empty → skip issue checks downstream.

The issue-agent prompt must also bind `Reviewed source: <full PR_HEAD_SHA>` and the frozen-source rule: `git show <PR_HEAD_SHA>:<path>` / `git grep <pattern> <PR_HEAD_SHA> -- <paths>` only; working-tree reads or reproduction runs require a clean checkout at that exact SHA immediately before use. Substitute the literal SHA before dispatch; the issue text never supplies source identity.

### Acceptance gate (PR mode only) — validate reject, then block

<!-- loads: modes/acceptance-gate.md -->

Skip if `DIRECT_PATH_MODE=true`. `FILL_GAPS_MODE=true` → skip Stage 1 only (the prior run judged the premise at this same head; the fill-gaps gate refuses a REJECT run); Stage 2 still runs. Two ordered stages, cheap, before Step 2's expensive fanout. **Reject is terminal** — no code change fixes the premise, pipeline stops. **Block is not** — premise is sound, current diff state has a fixable gap (red CI, a typo, a flaky test) — full fanout still runs, report just surfaces the fixable gap up front instead of burying it in consolidator output. Test to pick the stage: *"could revising the code, not the goal, resolve this?"* Yes → block. No → reject.

PR mode → load and follow `modes/acceptance-gate.md`; its terminal jump loads `modes/post-agent.md` at Step 5b and Step 7.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
[ "${DIRECT_PATH_MODE:-false}" = "true" ] && exit 0
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
cat "$REVIEW_SKILL_DIR/modes/acceptance-gate.md"  # timeout: 5000
```

### Direct report fast-path

`DIRECT_PATH_MODE=true`:

- `REPLY_MODE=false` → use `AskUserQuestion`: "A report path was passed without `--reply`. Did you mean `/oss:review <path.md> --reply`?" Options: (a) "Yes — continue with `--reply` mode" → set `REPLY_MODE=true`; then re-check: `[ ! -f "$REVIEW_FILE" ] && echo "Error: review file not found at $REVIEW_FILE" && exit 1`; proceed; (b) "No — review a PR instead" → print usage hint (`/oss:review <N> | path/to/dir`) and stop.
- `REPLY_MODE=true` and `[ ! -f "$REVIEW_FILE" ]` → print `Error: report not found: $REVIEW_FILE` and stop.
- `REPLY_MODE=true` and file exists → print `[direct] using $REVIEW_FILE` → **skip to Step 8**. Skip Steps 2–7.

### Agent 0 — blind-solve (FEATURE/MIXED only, general-purpose)

<!-- loads: modes/blind-solve.md -->

Skip when scope is FIX/REFACTOR/CHORE, DOCS_TYPING_MODE/TESTS_CI_MODE is true, or the gathered input has no issue and an empty/boilerplate PR body — note skip in report header, never fabricate a problem statement. Not a `FANOUT_MAX` unit — never counted against the cap, never ranked out.

FEATURE/MIXED only → load and follow `modes/blind-solve.md` before the Step 2 launch.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
cat "$REVIEW_SKILL_DIR/modes/blind-solve.md"  # timeout: 5000
```
