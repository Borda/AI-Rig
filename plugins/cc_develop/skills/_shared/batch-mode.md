<!-- file: batch-mode.md — consumers: feature/SKILL.md Step 3, refactor/SKILL.md Step 4 -->

## Batch mode

Default on, `--no-batch` opts out. Groups multiple non-overlapping edits into one test run per batch instead of one run per edit — preserves failure attribution via per-edit snapshots and bisect; never silently merges attribution into an unattributed group failure.

### Non-overlap predicate

An edit joins the current batch only when **both** hold against every edit already in it:

1. Its `codemap-py query test-impact "<changed_module>"` `pytest_cmd` set shares no test with any edit already batched.
2. It touches no source file any edit already batched touches — a per-file `git apply -R` revert can't isolate two edits to the same file from each other.

Codemap absent, or an edit fails either check against the current batch → close the batch (run it now), start a new batch with that edit alone. Cap: 4 edits per batch.

### Batch identity and per-edit snapshot

`BATCH_ID`/`N` are derived, not assumed — both persisted, since shell state does not survive across Bash() calls. Identity is stamped with its owning run (`<skill>:<DEV_DIR>`): a run aborted mid-batch (cap stop, user abort, halt) leaves `dev-batch-id`/`dev-batch-n` behind, and a later run in the same session would otherwise continue its `N` and bisect — then revert — the aborted run's edits. A stamp mismatch discards the leftover identity; a compaction resume inside the same run keeps it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _SKILL < "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}" 2>/dev/null || _SKILL="feature"
IFS= read -r _DEV_DIR < "${TMPDIR:-/tmp}/dev-${_SKILL}-dev-dir-${CSID}" 2>/dev/null || _DEV_DIR=""
IFS= read -r _OWNER < "${TMPDIR:-/tmp}/dev-batch-run-${CSID}" 2>/dev/null || _OWNER=""
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
[ "$_OWNER" = "${_SKILL}:${_DEV_DIR}" ] || BATCH_ID=""
if [ -z "$BATCH_ID" ]; then
    BATCH_ID=$(date -u +%Y%m%dT%H%M%SZ)
    echo "$BATCH_ID" > "${TMPDIR:-/tmp}/dev-batch-id-${CSID}"
    echo 0 > "${TMPDIR:-/tmp}/dev-batch-n-${CSID}"
    echo "${_SKILL}:${_DEV_DIR}" > "${TMPDIR:-/tmp}/dev-batch-run-${CSID}"
fi
IFS= read -r N < "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" 2>/dev/null || N=0
N=$((N + 1))
echo "$N" > "${TMPDIR:-/tmp}/dev-batch-n-${CSID}"
```

Snapshot the working tree **before** edit N is applied — a real commit object, not a diff, so an isolated per-edit patch can be computed later by diffing two snapshots directly. A plain `git diff` (no `HEAD`) omits staged changes and drops untracked files entirely — a batch member adding a new test file would be unrevertable — and a diff-of-diffs is not a meaningful patch:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
IFS= read -r N < "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" 2>/dev/null || N=0
_TMP_IDX="${TMPDIR:-/tmp}/dev-batch-idx-${BATCH_ID}-${N}-${CSID}"
GIT_INDEX_FILE="$_TMP_IDX" git read-tree HEAD    # timeout: 5000 — overwrites index content wholesale, no separate rm needed first (round-5 F1)
GIT_INDEX_FILE="$_TMP_IDX" git add -A :/         # timeout: 5000 — `:/` pathspec magic = repo root regardless of cwd; a bare `.` is CWD-scoped and would miss edits outside the directory the skill happens to run from (R4)
_TREE=$(GIT_INDEX_FILE="$_TMP_IDX" git write-tree)  # timeout: 5000
_PRE=$(git commit-tree "$_TREE" -p HEAD -m "batch snapshot")  # timeout: 5000
echo "$_PRE" > "${TMPDIR:-/tmp}/dev-batch-pre-${BATCH_ID}-${N}-${CSID}"
```

**`git stash create` was tried first and empirically fails here**: `git stash create` never captures untracked files at all (no `--include-untracked` on `create`, only on `push`), and combining it with `git add -N` intent-to-add entries reproducibly errors `error: Entry '<file>' not up-to-date. Cannot merge.` — confirmed against a live scratch repo.

`GIT_INDEX_FILE` redirects every index operation to a throwaway file instead: `read-tree HEAD` seeds it, `add -A :/` stages the whole real working tree (tracked changes *and* untracked files, repo root regardless of cwd) into that throwaway index only, `write-tree`/`commit-tree` turn it into a real commit object. The actual `.git/index`, branch, and stash list are never touched — verified by `git status --short` printing byte-identical output before and after.

**Snapshots are dangling commits** — reachable only through the `dev-batch-pre-*` sentinels, not any ref or branch. Safe within a session (sentinels + the objects they name survive until explicitly cleared or the session ends); not safe across a `git gc --prune`, which reclaims unreachable objects and would silently break an in-progress bisect's revert targets.

After the last edit in the batch is applied, snapshot once more the same way (one more `N` increment) — this closing snapshot is the "after" state for the batch's last edit and the input to its isolated patch below.

### Batch run

One pytest invocation covering the union of the batch's `test-impact` sets, capturing its exit as `BATCH_RUN_EXIT` — `dev_test_targets.py --skill "$_SKILL" --run` is that invocation: it selects across every file changed since HEAD, which includes every batch member, and runs the selection as one pytest process. The non-overlap predicate above keeps calling codemap directly: deciding attribution needs an index-grade test set per edit, while the heuristic fallback only selects what to run. **Only on success** persist the batch size for the caller's cap accounting and reset batch identity for the next batch — on failure, leave `BATCH_ID`/`N` in place so the bisect fences in `batch-bisect.md` (loaded on failure below) can still read them. The batch-size sentinel path is read from `_SKILL`, written once by the calling skill (`feature`/`refactor`) before loading this file — never a literal `<skill>` placeholder, which a fenced block can never substitute at runtime and can never match the permission manifest (`plugins/CLAUDE.md` §Blueprint Blocks):

```bash
# timeout: 600000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_CURRENCY  2>/dev/null < "${TMPDIR:-/tmp}/dev-codemap-currency-${CSID}" || CODEMAP_CURRENCY="no_index"
if [ "$CODEMAP_CURRENCY" = "stale" ]; then export SCAN_NO_AUTOBUILD=1; fi
IFS= read -r N < "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" 2>/dev/null || N=0
IFS= read -r _SKILL < "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}" 2>/dev/null || _SKILL="feature"
IFS= read -r PYTEST_CMD < "${TMPDIR:-/tmp}/dev-pytest-cmd-${CSID}" 2>/dev/null || PYTEST_CMD=""
IFS= read -r DEV_DIR < "${TMPDIR:-/tmp}/dev-${_SKILL}-dev-dir-${CSID}" 2>/dev/null || DEV_DIR=""
[ -n "$PYTEST_CMD" ] || { echo "! BLOCKED — PYTEST_CMD sentinel missing; re-run Project Detection"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_test_targets.py" --skill "$_SKILL" --run --record-dir "${DEV_DIR:-.developments}"
BATCH_RUN_EXIT=$?
echo "BATCH_RUN_EXIT=$BATCH_RUN_EXIT"
if [ "${BATCH_RUN_EXIT:-1}" -eq 0 ]; then
    # N counted every snapshot taken, including the one closing "after last edit" snapshot
    # (see "one more N increment" above) — edits processed = N - 1, not N.
    echo "$((N - 1))" > "${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}"
fi
exit "$BATCH_RUN_EXIT"
```

Identity clears in its own fence, never combined with the write above — a fence containing `rm -f` is flagged `is_dangerous` by `blueprint-allow.js`, which declines the *whole* submitted block before any manifest lookup regardless of what else is in it (same defect class R2 fixed in `quality-stack.md`). The calling procedure invokes this fence only when the run fence's printed `BATCH_RUN_EXIT=` value was `0` — on failure this fence never runs, leaving `BATCH_ID`/`N` in place for the bisect fences in `batch-bisect.md` (loaded on failure below):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" "${TMPDIR:-/tmp}/dev-batch-n-${CSID}"
```

### On batch failure — bisect, never blind revert

> loads: batch-bisect.md

Only when the run fence printed a non-zero `BATCH_RUN_EXIT=` — the identity-clear fence above never ran, so `BATCH_ID`/`N` are still in place. Load the bisect procedure and follow it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" batch-bisect.md
```

### Cap accounting

`MAX_INNER_CYCLES` counts **edits processed**, not batches or cycles. The caller's cap-check fence reads `${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}` (written above when the batch closes) and consumes it — `${BATCH_SIZE:-1}` only ever falls back to `1` when no batch closed since the last read, so the no-batch (`--no-batch`; gate printed `batch mode: off`) path is unaffected and keeps today's per-cycle cap exactly. A 4-edit batch that passes in one run consumes 4 against the cap; a batch that fails and bisects also consumes its base edit count (step 7's write in `batch-bisect.md`) — the extra pytest invocations bisect itself runs to isolate the culprit are not separately added to this count, only the batch's own edits are.

### Red-green reconciliation (TDD contexts only)

A batch writes all N members' red tests up front; **one run** confirms all N red, not N runs. Implement all N. **One run** confirms all N green. Red-before-green still holds per test — only the run *count* collapses, never the ordering guarantee.

### Skill contexts (substitute when loading this protocol)

**feature** (Step 3, TDD implementation loop):

- A batch member = one Step-2-shaped demo/test-writing act (one piece of functionality).
- Red-green test ownership stays lead / `foundry:sw-engineer` per `feature/SKILL.md` §Step 3: TDD implementation loop — batching changes run *count*, never who writes the tests.
- Scope: this loop only, never the Step 4 review loop, when explicitly requested (which keeps its own `test-impact`-scoped per-cycle re-runs, unbatched).

**refactor** (Step 4, refactor-with-safety-net loop):

- A batch member = one focused change (`refactor/SKILL.md` §Step 4: Refactor with safety net — "one focused change per edit," now applied per *batch member* rather than per test run).
- No red-green ownership rule to reconcile — refactor has no red-test step; a batch member's "confirm it fails" is not part of this loop.
- Scope: this loop only, never Step 5's review loop, when explicitly requested (which keeps its own per-cycle re-run, unbatched).
