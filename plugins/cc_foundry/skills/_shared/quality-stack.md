# Shared Quality Stack

Used by develop mode skills (feature, fix, refactor). Canonical home: cc_foundry `_shared/quality-stack.md`; consumer plugins ship the propagated copy as `foundry--quality-stack.md` (source-plugin prefix — a plugin-local file can never collide with a propagated copy) and load via `cat "$_SHARED/foundry--quality-stack.md"` (not the Read tool — `Bash(cat:*)` grant is version-proof).

> `$_SHARED` = **the loading plugin's own** `skills/_shared`, set by the consumer before `cat`-ing this file, and persisted by it in that same fence to `${TMPDIR:-/tmp}/qs-shared-dir-${CSID}` — the sibling-loading fences below run as separate `Bash()` calls and read `$_SHARED` back from that sentinel. This doc is a byte-identical `propagate_shared.py` copy present in every plugin using it, so it must never name a specific plugin's variable — the sibling files it loads below resolve out of whichever `_shared` the consumer set.

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
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# project lockfile picks the env — uv present but no uv.lock (poetry/pip repo) must not run outside its env
if command -v uv >/dev/null 2>&1 && [ -f uv.lock ]; then RUNNER="uv run"
elif command -v poetry >/dev/null 2>&1 && [ -f poetry.lock ]; then RUNNER="poetry run"
else RUNNER="python -m"; fi
case "$RUNNER" in "uv run") R=(uv run);; "poetry run") R=(poetry run);; "python -m") R=(python -m);; *) echo "! BLOCKED — runner state missing or not one of: uv run, poetry run, python -m (re-run Tool detection)"; exit 1;; esac
# ruff pinned in pre-commit → run the pinned hooks, never bare ruff (version/config drift vs CI)
USE_PRECOMMIT=0
[ -f .pre-commit-config.yaml ] && grep -q "ruff" .pre-commit-config.yaml && command -v pre-commit >/dev/null 2>&1 && USE_PRECOMMIT=1

SKIP_RUFF=0
if ! "${R[@]}" ruff --version >/dev/null 2>&1; then
    echo "WARNING: ruff not available — skipping lint/format steps"
    SKIP_RUFF=1
fi

SKIP_MYPY=0
if ! "${R[@]}" mypy --version >/dev/null 2>&1; then
    echo "WARNING: mypy not available — skipping type check step"
    SKIP_MYPY=1
fi

# each later block is its own Bash() call — bare $RUNNER/$SKIP_RUFF/$SKIP_MYPY would read empty there
echo "$RUNNER" > "${TMPDIR:-/tmp}/quality-stack-runner-${CSID}"
echo "$SKIP_RUFF" > "${TMPDIR:-/tmp}/quality-stack-skip-ruff-${CSID}"
echo "$SKIP_MYPY" > "${TMPDIR:-/tmp}/quality-stack-skip-mypy-${CSID}"
echo "$USE_PRECOMMIT" > "${TMPDIR:-/tmp}/quality-stack-precommit-${CSID}"
```

- `$RUNNER` (`"uv run"`, `"poetry run"`, `"python -m"`) is a two-token string — a bare `$RUNNER <cmd>` does not word-split under zsh (Claude Code's Bash tool login shell on macOS: no word-splitting on a bare `"$VAR"`, only on unquoted command substitution). Every fence therefore maps the saved string to the array `R` through a closed `case` and calls `"${R[@]}" <tool> …`; `"${R[@]}"` expands to separate words in bash and zsh alike.
- No shell re-parse of any string: a sentinel is a lookup key into the closed `case`, never a command. A value outside the three literals (stale, planted, or corrupted session file) prints `! BLOCKED` and exits 1 — nothing runs.
- `set -o pipefail` (bash/zsh/ksh extension, absent from POSIX `sh`/`dash` — present in both shells this Bash tool resolves to) replaces `${PIPESTATUS[0]}` — zsh has no bash-style `PIPESTATUS`, only a differently-indexed lowercase `$pipestatus`.
- Every fence below that uses `R`, `$SKIP_RUFF`, or `$SKIP_MYPY` re-reads its sentinel first — a fresh `Bash()` call never inherits this block's shell state.
- Optional multi-word arguments (`XDIST`, `_QS_DOCTEST_ARGS`, `RETRY`) are arrays too, expanded as `"${NAME[@]}"` (an empty array expands to nothing). Never enable `set -u` in these fences: bash 3.2 (macOS `/bin/bash`) aborts on `"${EMPTY[@]}"` under it.
- Data operands (a test id, module path, or other repo-derived value) are ordinary quoted arguments — `"$VAR"` — one argument each, no re-splitting, no globbing (a bare `[...]` in a pytest parametrized node-id aborts zsh with `NOMATCH` only when the word is unquoted). `<test_dir>`/`<target_module>` are literal template text, safe as-is.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUNNER < "${TMPDIR:-/tmp}/quality-stack-runner-${CSID}" 2>/dev/null || RUNNER=""
case "$RUNNER" in "uv run") R=(uv run);; "poetry run") R=(poetry run);; "python -m") R=(python -m);; *) echo "! BLOCKED — runner state missing or not one of: uv run, poetry run, python -m (re-run Tool detection)"; exit 1;; esac
IFS= read -r SKIP_RUFF < "${TMPDIR:-/tmp}/quality-stack-skip-ruff-${CSID}" 2>/dev/null || SKIP_RUFF=0
IFS= read -r SKIP_MYPY < "${TMPDIR:-/tmp}/quality-stack-skip-mypy-${CSID}" 2>/dev/null || SKIP_MYPY=0
IFS= read -r USE_PRECOMMIT < "${TMPDIR:-/tmp}/quality-stack-precommit-${CSID}" 2>/dev/null || USE_PRECOMMIT=0
set -o pipefail
if [ "${USE_PRECOMMIT:-0}" -eq 1 ]; then pre-commit run --files <changed_files>  # timeout: 120000
else
    [ "${SKIP_RUFF:-0}" -ne 1 ] && "${R[@]}" ruff check <changed_files> --fix  # timeout: 30000
    [ "${SKIP_RUFF:-0}" -ne 1 ] && "${R[@]}" ruff format <changed_files>  # timeout: 30000
fi
MYPY_EXIT=skipped
[ "${SKIP_MYPY:-0}" -ne 1 ] && { "${R[@]}" mypy <changed_files> --no-error-summary 2>&1 | head -30; MYPY_EXIT=$?; }  # timeout: 30000
echo "MYPY_EXIT=$MYPY_EXIT"
```

- Pinned path: `pre-commit run` applies the project's own pinned hooks (ruff included) to the changed files — hook fixes rewrite files, same as `ruff --fix`/`format`. `pre-commit run` exits non-zero whenever a hook modified a file, even with nothing left to fix → on non-zero re-run the same line once; second run non-zero = hook findings left to fix.
- `MYPY_EXIT` is advisory, never a halt: non-zero = type errors in changed files → fix before the report (or spawn **foundry:linting-expert**, below); list any left unfixed in the report. `skipped` = mypy unavailable.

**xdist capability probe** (wide run only — failure-reading retries below always stay serial). `cc_develop/skills/_shared/runner-detection.md` had its own copy of this probe; deleted — round-2 review (H2) found it write-only, its `XDIST_AVAILABLE`/`XDIST_ARGS` sentinel never consumed anywhere in `cc_develop`, and `plugins/CLAUDE.md` §Self-Contained `_shared` forbids this file reading `cc_develop`'s copy to wire the two together, so the dead code was deleted rather than force-wired:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUNNER < "${TMPDIR:-/tmp}/quality-stack-runner-${CSID}" 2>/dev/null || RUNNER=""
case "$RUNNER" in "uv run") R=(uv run);; "poetry run") R=(poetry run);; "python -m") R=(python -m);; *) echo "! BLOCKED — runner state missing or not one of: uv run, poetry run, python -m (re-run Tool detection)"; exit 1;; esac
XDIST_ARGS=""
"${R[@]}" pytest --help 2>/dev/null | grep -qi numprocesses && XDIST_ARGS="-n auto"
echo "$XDIST_ARGS" > "${TMPDIR:-/tmp}/quality-stack-xdist-args-${CSID}"
```

**Doctest-fold collection check** — run before merging, not after; a clean collection here is what makes the merge safe. `<target_module>` is the module(s) `--doctest-modules` would otherwise scan separately:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUNNER < "${TMPDIR:-/tmp}/quality-stack-runner-${CSID}" 2>/dev/null || RUNNER=""
case "$RUNNER" in "uv run") R=(uv run);; "poetry run") R=(poetry run);; "python -m") R=(python -m);; *) echo "! BLOCKED — runner state missing or not one of: uv run, poetry run, python -m (re-run Tool detection)"; exit 1;; esac
DOCTEST_MERGED=false
_QS_COLLECT_LOG="${TMPDIR:-/tmp}/quality-stack-doctest-collect-${CSID}.log"
if "${R[@]}" pytest <test_dir> --doctest-modules <target_module> --collect-only -q >"$_QS_COLLECT_LOG" 2>&1; then
    DOCTEST_MERGED=true
fi
echo "$DOCTEST_MERGED" > "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}"
```

No `rm -f` in this fence — a fence containing `rm -f` is flagged `is_dangerous` by `blueprint-allow.js`, which declines the *whole* submitted block before any manifest lookup regardless of what else shares it (same defect class R2 fixed on the wide-run fence below). Cleanup for this log lives in the isolated one-line fence below, alongside the wide-run log — same cost, already paid. Collection fails (exit non-zero — malformed `>>>` prompt, import error, etc.) → `DOCTEST_MERGED` stays `false`; the cause is on disk at `$_QS_COLLECT_LOG` either way now (previously kept only on the failure branch); doctests run as the separate pass below. `$RUNNER` here is a sentinel read-back from the detection fence (mapped to `R` by the closed `case`), not shell state — a `tox`/`make test` runner never reaches this block at all; those degrade via `runner-detection.md`'s own unwrapped-`PYTEST_CMD` fallback instead. This is a graceful degrade, not a halt — report status becomes `not-merged`, nothing more.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUNNER < "${TMPDIR:-/tmp}/quality-stack-runner-${CSID}" 2>/dev/null || RUNNER=""
case "$RUNNER" in "uv run") R=(uv run);; "poetry run") R=(poetry run);; "python -m") R=(python -m);; *) echo "! BLOCKED — runner state missing or not one of: uv run, poetry run, python -m (re-run Tool detection)"; exit 1;; esac
IFS= read -r XDIST_ARGS < "${TMPDIR:-/tmp}/quality-stack-xdist-args-${CSID}" 2>/dev/null || XDIST_ARGS=""
IFS= read -r DOCTEST_MERGED < "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}" 2>/dev/null || DOCTEST_MERGED=false
_QS_LOG="${TMPDIR:-/tmp}/quality-stack-run1-${CSID}.log"
_QS_DOCTEST_ARGS=()
[ "$DOCTEST_MERGED" = "true" ] && _QS_DOCTEST_ARGS=(--doctest-modules <target_module>)
XDIST=()
[ "$XDIST_ARGS" = "-n auto" ] && XDIST=(-n auto)
# Redirect to log, not pipe: pytest exit stays in $? (tee failure on unwritable $TMPDIR can't mask real exit-5 config error);
# log complete on disk before next line greps it (`> >(tee ...)` runs async in bash and zsh — grep would race a flushing tee).
"${R[@]}" pytest <test_dir> "${_QS_DOCTEST_ARGS[@]}" -v --tb=short -rfE "${XDIST[@]}" > "$_QS_LOG" 2>&1  # timeout: 600000
SUITE_EXIT=$?
tail -20 "$_QS_LOG"

if [ $SUITE_EXIT -eq 5 ]; then
    echo "✗ CONFIG ERROR: no tests collected (exit 5) — not a test failure; check test_dir/markers"
    echo "Quality stack halted — do not proceed further"
    exit 1
fi

if [ $SUITE_EXIT -ne 0 ]; then
    # Full untruncated log, not tail-capped — safe to grep every FAILED/ERROR line. -rfE reports fixture/collection ERRORs too: ERROR-only exit must still retry, never hit empty-set halt below.
    # sed captures id by pytest node-id grammar (whitespace-free prefix + at most one [bracket] group holding at most one nested [..] level; params only part with spaces/dashes), never a " - " split — reason text may contain " - " and over-capture.
    # [^[:space:]] not \S — BSD sed (macOS) has no \S. No awk '{print $2}' — truncates at first whitespace, breaks on space inside bracket params.
    FAILED_IDS=$(grep -E '^(FAILED|ERROR) ' "$_QS_LOG" | sed -E 's/^(FAILED|ERROR) ([^[:space:]]+(\[([^][]|\[[^]]*\])*\])?).*$/\2/' | sort -u)

    if [ -z "$FAILED_IDS" ]; then
        # Degenerate empty set — never invoke pytest with zero node-id args (widens scope to rootdir).
        echo "✗ GENUINE FAILURE: suite exit $SUITE_EXIT but no FAILED/ERROR node-ids parsed — inspect output above"
        echo "Quality stack halted — do not proceed further"
        exit 1
    fi

    # No mapfile (absent in zsh and bash 3.2) — ids go into an array with a while-read loop; each is one argument.

    # Doctest node-id file part matches neither default python_files pattern (test_*.py, *_test.py); custom python_files
    # degrades safely (worst case: real test misclassified as doctest, never the reverse).
    DOCTEST_FAILED_IDS=$(printf '%s\n' "$FAILED_IDS" | while IFS= read -r _id; do
        _base=$(basename "${_id%%::*}")
        case "$_base" in
            test_*.py|*_test.py) ;;
            *) printf '%s\n' "$_id" ;;
        esac
    done)

    if [ -n "$DOCTEST_FAILED_IDS" ]; then
        # Doctests deterministic, not flaky-eligible — mismatch = genuine failure on first read, never routed through
        # retry or FLAKY mark-and-ignore prompt (assumes a markable pytest test function).
        echo "✗ GENUINE FAILURE: doctest assertion failed:"
        printf '  %s\n' "$DOCTEST_FAILED_IDS"
        echo "Quality stack halted — do not proceed further"
        exit 1
    fi

    # Retry A: exactly the failing node-ids, serial (never -n while reading a failure).
    # Each id is one array element → one argument, never re-split or glob-eligible (zsh NOMATCH aborts on bare "[...]",
    # which every parametrized id contains). Process substitution, not a pipe: a piped loop runs in a subshell and loses the array;
    # not a heredoc: heredoc breaks fence parsing in list-item context and drops whole fence from blueprint manifest.
    RETRY=()
    while IFS= read -r _id; do
        [ -n "$_id" ] && RETRY+=("$_id")
    done < <(printf '%s\n' "$FAILED_IDS")
    "${R[@]}" pytest --tb=short -v "${RETRY[@]}"  # timeout: 600000
    RETRY_A_EXIT=$?
    if [ $RETRY_A_EXIT -ne 0 ]; then
        echo "✗ GENUINE FAILURE: failing node-id(s) fail again on isolated re-run"
        echo "Quality stack halted — do not proceed further"
        exit 1
    fi

    # Retry B: full-dir, serial — rules out order-dependency without conflating it with xdist contention.
    "${R[@]}" pytest <test_dir> -v --tb=short  # timeout: 600000
    RETRY_B_EXIT=$?
    if [ $RETRY_B_EXIT -ne 0 ]; then
        echo "✗ GENUINE FAILURE: order-dependent — passes isolated, fails in full-dir serial re-run"
        echo "Quality stack halted — do not proceed further"
        exit 1
    fi

    if [ -n "$XDIST_ARGS" ]; then
        echo "⚠ PARALLEL_FAIL_SERIAL_PASS: failed only under $XDIST_ARGS, passes fully serial"
        echo "Quality stack halted — likely test-isolation bug, not flaky (repo CLAUDE.md §Test Workflow: passes serially, fails only under -n = a real defect, never an xdist artifact). Investigate before proceeding."
        exit 1
    fi
    FLAKY_DETECTED=true
fi
```

Log cleanup lives in its own fence, not inline above — `rm -f` flags a fence `is_dangerous`, and `blueprint-allow.js` re-runs that check on the whole submitted block before any manifest lookup, so a `rm -f` inside the 47-command wide-run fence cost the *entire* fence its manifest coverage (prompts every call, R2). Isolated here, only this one-line fence pays that cost. Runs only when the fence above did not halt — every failure path `exit 1`s first, which leaves the log on disk for debugging; intentional, not a regression:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f "${TMPDIR:-/tmp}/quality-stack-run1-${CSID}.log" "${TMPDIR:-/tmp}/quality-stack-doctest-collect-${CSID}.log"
```

Deviation from the plan's stated `--junitxml` mechanism, noted explicitly: `--junitxml` was proposed to fix `run_pytest_short.py`'s lossy `tail -20` wrapper capture — that lossiness doesn't apply here, since this block calls `"${R[@]}" pytest ...` directly and captures full untruncated output to a session-scoped log file. `-rfE`'s `FAILED <node-id>`/`ERROR <node-id>` lines are already valid pytest node-id syntax, needing no `classname`/`name` → node-id reconstruction that XML parsing would require. Simpler, same non-lossy guarantee.

`FLAKY_DETECTED` is reachable only when run 1 was serial — a genuinely flaky test under `-n auto` takes the `PARALLEL_FAIL_SERIAL_PASS` branch above and halts as a suspected isolation bug instead, per the plan's stated design; no serial-re-run offer before that halt in this revision.

`FLAKY_DETECTED=true` (failed run 1, passed isolated re-run, passed full-dir serial re-run, run 1 was already serial — not `-n` — and no doctest id was among the failures):

- **Do NOT fall through** — invoke `AskUserQuestion` with question text `⚠ FLAKY: test(s) failed initial run, passed both isolated and full-dir serial re-runs` naming the tests (not printed as reply text before the call — text written before a tool call can arrive as an empty progress update):
  - (a) **Mark and continue** — add `@pytest.mark.flaky(reruns=3)` marker and `# TODO: flaky — investigate <date>` comment to failing test(s); then continue quality stack
  - (b) **Fix now** — stop quality stack here; investigate and fix flaky test before proceeding
  - (c) **Abort** — cancel skill run
- On (b) or (c): stop quality stack execution immediately
- On (a): apply marker + comment, then continue

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUNNER < "${TMPDIR:-/tmp}/quality-stack-runner-${CSID}" 2>/dev/null || RUNNER=""
case "$RUNNER" in "uv run") R=(uv run);; "poetry run") R=(poetry run);; "python -m") R=(python -m);; *) echo "! BLOCKED — runner state missing or not one of: uv run, poetry run, python -m (re-run Tool detection)"; exit 1;; esac
IFS= read -r DOCTEST_MERGED < "${TMPDIR:-/tmp}/quality-stack-doctest-merged-${CSID}" 2>/dev/null || DOCTEST_MERGED=false
set -o pipefail
if [ "$DOCTEST_MERGED" != "true" ]; then
    "${R[@]}" pytest --doctest-modules <target_module> -v 2>&1 | tail -20  # timeout: 600000
    DOCTEST_EXIT=$?  # pipefail: pytest's exit, not tail's
    # 5 = no doctests in module, not a failure; doctests deterministic → never retried
    case $DOCTEST_EXIT in
        0|5) ;;
        1)
            echo "✗ GENUINE FAILURE: doctest assertion failed in separate doctest pass"
            echo "Quality stack halted — do not proceed further"
            exit 1
            ;;
        *)
            echo "✗ CONFIG ERROR: separate doctest pass exit $DOCTEST_EXIT — pytest interrupted, internal or usage error"
            echo "Quality stack halted — do not proceed further"
            exit 1
            ;;
    esac
fi
```

**Doctest report status** (3-state, replaces the old pass/fail derivation — `report-templates.md:30` reads this): `not-merged` when `DOCTEST_MERGED=false` and the separate pass above exited `0` or `5` (degraded path, nothing failed); else `pass` when `SUITE_EXIT=0` or the merged run's only failures were non-doctest (already resolved by retry above); else `fail` — a doctest id was present in `DOCTEST_FAILED_IDS`, or the separate pass exited non-zero other than `5`, and the run already halted before reaching this line.

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

Document option used in Final Report, "Recovery" subsection.

## Codex Pre-pass

Mandatory after quality stack. Degrades gracefully if Codex unavailable.

Load `codex-prepass.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof) and run Codex pre-pass on changes.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _SHARED < "${TMPDIR:-/tmp}/qs-shared-dir-${CSID}" 2>/dev/null || _SHARED=""
[ -n "$_SHARED" ] || { echo "! BLOCKED — qs-shared-dir sentinel missing; consumer must persist its own _shared dir before loading this file"; exit 1; }
cat "$_SHARED/codex-prepass.md"  # timeout: 5000
```

### Codex pre-pass: additional inline steps (step 1 is in the shared file)

2. **Collect findings**: build `CODEX_FINDINGS` — bullet list of every flagged issue from `bridge:review` output. Nothing found or step skipped → set `CODEX_FINDINGS=""`. Review read-only, no working-tree changes.
3. **Actor context**: note whether Codex involved (found real issues acted on). Pass as context when committing — `git-commit.md` decides trailers.

Include `### Codex Pre-pass` section in final report:

- Available + findings: list what Codex flagged (become `CODEX_FINDINGS` seed)
- Available + no issues: "Codex pre-pass: no issues found"
- Skipped (unavailable): "bridge@borda-ai-rig absent or disabled — pre-pass skipped"

## Progressive Review Loop

Max 3 cycles. Applied after quality stack. **`oss:*` skills are NEVER auto-invoked from develop flows** — run only on explicit user request. Escalation below spawns review agents directly — `/develop:review` (requires `develop` plugin) is `disable-model-invocation`, offered only as a user-run follow-up.

**Cycle 1: Confidence-gated review escalation**

- Compute concern signal after quality stack: any unresolved critical/high finding, OR `CODEX_FINDINGS` non-empty and not yet verified. Envelope confidence alone is NOT a trigger — template envelopes print 0.88, so a `< 0.9` arm fired on nearly every run, nesting a full multi-agent `/develop:review` (~5-6 spawns) without a concrete finding to chase; low confidence without findings goes to the report as a stated gap instead
- No concern → skip directly to report; in the final report list optional follow-ups the user may explicitly request (`/develop:review` for a deeper local pass; `/oss:review` once a PR exists)
- Concern present → spawn review dimension agents directly, scoped to the modified files — `/develop:review` is `disable-model-invocation`, so a `Skill()` call never runs it. Pick agents by changed-file concern: `foundry:sw-engineer` (correctness, always), `foundry:qa-specialist` (test gaps), `foundry:linting-expert` (lint/type residue), `foundry:perf-optimizer` / `foundry:solution-architect` / `foundry:doc-scribe` only when the diff touches their domain. `CODEX_FINDINGS` non-empty → prepend to every brief: "Codex pre-pass found the following — verify these, do not rediscover: $CODEX_FINDINGS". Also seed quality-stack's own findings as "already checked — verify, do not rediscover". Run dir, prompt ending and health monitoring: same as Cycle 2 below — set up the run dir here, Cycle 2 reuses it
- Capture review state from the envelopes: `{agents_with_findings, unresolved_findings, files_reviewed}`
- Clean (no critical/high findings): skip to report

**Cycle 2: Targeted re-check**

- Fix critical/high findings from Cycle 1
- Re-run quality stack on modified files only — scoped via `codemap-py query test-impact "<changed_module>"` (per file, then union): non-empty `pytest_cmd` **and** the result's `index.stale` is false and `index.query_complete` is true → re-run that scoped test set instead of the quality stack's directory-wide pytest line. Both freshness fields live under the nested `index` object, never at the top level — a top-level read finds nothing and trusts a stale index. Codemap absent, empty result, `index.stale` true, or `index.query_complete` not true → honest fallback to the full quality-stack re-run — never silently claim file-scoping the instruction can't actually perform
- Scoped re-runs only shorten the fix loop: whenever Cycle 2 ends clean after any fix, run the full quality stack (its directory-wide pytest line, the repository's own settings) once before the report — the last code change is never verified by a scoped set alone
- Run dir for file-based handoff — Cycle 1 creates a fresh one before its spawns (first block; fresh every run, so a prior run's `<agent-name>.md` never masks a timeout), Cycle 2 re-reads it (second block); spawn prompts carry the printed literal path, never `$RUN_DIR`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_DIR=".developments/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"; echo "$RUN_DIR" > "${TMPDIR:-/tmp}/qs-review-run-dir-${CSID}"; echo "RUN_DIR=$RUN_DIR"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/qs-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — review run-dir sentinel missing; Cycle 1 run-dir block never ran"; exit 1; }
echo "RUN_DIR=$RUN_DIR"
```

- For each agent type in `agents_with_findings`: spawn directly (not `/oss:review`) with focused prompt scoped to modified files + prior findings. Each agent prompt must end with (`$RUN_DIR` substituted with the printed path): "Write your full findings to `$RUN_DIR/<agent-name>.md` using the Write tool. Return ONLY a compact JSON envelope: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"$RUN_DIR/<agent-name>.md\",\"confidence\":0.N,\"summary\":\"<agent-name>: N critical, N high\"}`"

Replace bare agent names in spawn prompts with `foundry:` prefixed equivalents: `foundry:sw-engineer`, `foundry:qa-specialist`, `foundry:linting-expert`, `foundry:doc-scribe`, `foundry:perf-optimizer`, `foundry:solution-architect`.

**Health monitoring**: Agent calls run in background. Spawn, end turn, resume on completion notification — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler tool call, a "waiting" turn, or a sleep. A notification whose `$RUN_DIR/<agent-name>.md` is empty or missing → that agent is ⏱ `timed_out` at once: Read whatever partial file exists, mark it ⏱ in final report, never wait for it further. A ⏱ never answers or skips a user decision.

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
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _SHARED < "${TMPDIR:-/tmp}/qs-shared-dir-${CSID}" 2>/dev/null || _SHARED=""
[ -n "$_SHARED" ] || { echo "! BLOCKED — qs-shared-dir sentinel missing; consumer must persist its own _shared dir before loading this file"; exit 1; }
cat "$_SHARED/codex-delegation.md"  # timeout: 5000
```

Distinct from Codex pre-pass — pre-pass checks implementation diff for correctness; mechanical delegation outsources low-level follow-up (scaffolding, boilerplate, migration scripts) after review loop closes.

Include `### Codex Delegation` section in final report only when tasks delegated — omit entirely if nothing delegated.
