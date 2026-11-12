<!-- file: batch-bisect.md — consumers: batch-mode.md §On batch failure -->

### On batch failure — bisect, never blind revert

Edit N's isolated patch is the diff between its own pre-snapshot and the next edit's pre-snapshot — never the cumulative pre-snapshot alone, and never `git apply -R` on a snapshot that predates edits other than N. The batch's edit count is derived once (never left to prose), then a real loop assigns `N` and computes every patch up front — nothing here waits for a "calling procedure" to substitute `N` by hand:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
IFS= read -r _N_TOTAL < "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" 2>/dev/null || _N_TOTAL=0
[ "$_N_TOTAL" -ge 1 ] || { echo "! bisect: batch-n sentinel missing or zero, cannot derive edit count"; exit 1; }
_BATCH_EDITS=$((_N_TOTAL - 1))   # N_total counts every snapshot incl. the closing one — see Batch run
echo "$_BATCH_EDITS" > "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}"
for N in $(seq 1 "$_BATCH_EDITS"); do
    IFS= read -r PRE_N < "${TMPDIR:-/tmp}/dev-batch-pre-${BATCH_ID}-${N}-${CSID}" 2>/dev/null || PRE_N=""
    IFS= read -r PRE_N_PLUS_1 < "${TMPDIR:-/tmp}/dev-batch-pre-${BATCH_ID}-$((N + 1))-${CSID}" 2>/dev/null || PRE_N_PLUS_1=""
    [ -n "$PRE_N" ] && [ -n "$PRE_N_PLUS_1" ] || { echo "! bisect: snapshot $N missing, aborting rather than diffing against an empty path"; exit 1; }
    git diff "$PRE_N" "$PRE_N_PLUS_1" > "${TMPDIR:-/tmp}/dev-batch-patch-${BATCH_ID}-${N}-${CSID}.diff"  # timeout: 5000
done
IFS= read -r _SKILL < "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}" 2>/dev/null || _SKILL="feature"
IFS= read -r DEV_DIR < "${TMPDIR:-/tmp}/dev-${_SKILL}-dev-dir-${CSID}" 2>/dev/null || DEV_DIR=""
python "$PLUGIN_ROOT/bin/dev_test_targets.py" --check-record "${DEV_DIR:-.developments}/test-targets.jsonl" --expect-exit 1 --save-failures "${TMPDIR:-/tmp}/dev-batch-failures-${BATCH_ID}-${CSID}" || exit 1
```

1. Split the candidate range in half, initially the whole batch.
2. Keep the prefix through the midpoint applied, including edits below the lower candidate bound. Revert only edits above the midpoint via `git apply -R`, newest-applied first — never the targeted revert's `git checkout HEAD -- <file>` (`foundry--quality-stack.md` §Recovery), which is file-scoped and would discard every edit to a shared file, not just the reverted one. The non-overlap predicate (disjoint files) guarantees each isolated patch applies/reverts cleanly.
3. Freeze the failing node IDs from the full-batch run once, then run that fixed set for every prefix. Re-apply **every** temporarily reverted patch before choosing either recursion branch. The block below persists the candidate bounds and the removed edit numbers; each fresh shell reloads them.
4. Prefix green → narrow to the second half; prefix red → narrow to the first half. Before printing a single culprit candidate, confirm its prefix is red and the preceding prefix is green. This establishes a failure boundary, not a unique explanation of every batch failure. Collection errors, missing fixed test nodes and empty selections cannot establish attribution. A patch failure stops with only the still-removed edits in the ledger: recover those patches before restarting.
5. With all edits restored, repair the isolated culprit. Never discard an untested half: it may contain another independent failure. If the complete batch is still red, preserve its edits and identity and report the remaining failures; take fresh snapshots before another isolation attempt because repairing a patch changes its reverse target.
6. A 4-edit batch needs `ceil(log2(4)) = 2` narrowing runs, then two boundary-confirmation runs and complete-batch verification.

```bash
# timeout: 600000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
IFS= read -r _BATCH_EDITS < "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}" 2>/dev/null || _BATCH_EDITS=0
IFS= read -r _LOW < "${TMPDIR:-/tmp}/dev-batch-low-${BATCH_ID}-${CSID}" 2>/dev/null || _LOW=1
IFS= read -r _HIGH < "${TMPDIR:-/tmp}/dev-batch-high-${BATCH_ID}-${CSID}" 2>/dev/null || _HIGH="$_BATCH_EDITS"
[ -n "$BATCH_ID" ] && [ "$_LOW" -ge 1 ] && [ "$_HIGH" -le "$_BATCH_EDITS" ] && [ "$_LOW" -le "$_HIGH" ] || { echo "! bisect: invalid candidate range"; exit 1; }
_REVERTED="${TMPDIR:-/tmp}/dev-batch-reverted-${BATCH_ID}-${CSID}"
[ ! -s "$_REVERTED" ] || { echo "! bisect: recover recorded removed patches before continuing: $_REVERTED"; exit 1; }
IFS= read -r _SKILL < "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}" 2>/dev/null || _SKILL="feature"
IFS= read -r DEV_DIR < "${TMPDIR:-/tmp}/dev-${_SKILL}-dev-dir-${CSID}" 2>/dev/null || DEV_DIR=""
_TARGETS="${TMPDIR:-/tmp}/dev-batch-failures-${BATCH_ID}-${CSID}"
[ -s "$_TARGETS" ] || { echo "! bisect: freeze full-batch failures before any trial"; exit 1; }
run_prefix() {
    _LIMIT="$1"
    for N in $(seq "$_BATCH_EDITS" -1 "$((_LIMIT + 1))"); do
        if [ -s "${TMPDIR:-/tmp}/dev-batch-patch-${BATCH_ID}-${N}-${CSID}.diff" ]; then
            git apply -R < "${TMPDIR:-/tmp}/dev-batch-patch-${BATCH_ID}-${N}-${CSID}.diff" || { echo "! bisect: revert failed for edit $N; inspect $_REVERTED"; return 2; }
            printf '%s\n' "$N" >> "$_REVERTED"
        fi
    done
    _HALF_DIR="${DEV_DIR:-.developments}/bisect-${BATCH_ID}-${_LIMIT}"
    _TRIAL_EXIT=2
    if mkdir -p "$_HALF_DIR" && : > "$_HALF_DIR/test-targets.jsonl"; then
        python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill "$_SKILL" --tests-from "$_TARGETS" --run --record-dir "$_HALF_DIR"
        _TRIAL_EXIT=$?
    fi
    while [ -s "$_REVERTED" ]; do
        N=$(tail -n 1 "$_REVERTED")
        git apply < "${TMPDIR:-/tmp}/dev-batch-patch-${BATCH_ID}-${N}-${CSID}.diff" || { echo "! bisect: re-apply failed for edit $N; inspect $_REVERTED"; return 2; }
        sed '$d' "$_REVERTED" > "$_REVERTED.next" && mv "$_REVERTED.next" "$_REVERTED" || return 2
    done
    case "$_TRIAL_EXIT" in 0|1) ;; *) echo "! bisect: runner error $_TRIAL_EXIT; all edits restored"; return 2;; esac
    python "$PLUGIN_ROOT/bin/dev_test_targets.py" --check-record "$_HALF_DIR/test-targets.jsonl" --expect-exit "$_TRIAL_EXIT" || return 2
    return "$_TRIAL_EXIT"
}
if [ "$_LOW" -lt "$_HIGH" ]; then
    _MID=$(((_LOW + _HIGH) / 2))
    run_prefix "$_MID"
    _HALF_EXIT=$?
    case "$_HALF_EXIT" in 0) _LOW=$((_MID + 1));; 1) _HIGH="$_MID";; *) exit 1;; esac
fi
echo "$_LOW" > "${TMPDIR:-/tmp}/dev-batch-low-${BATCH_ID}-${CSID}"
echo "$_HIGH" > "${TMPDIR:-/tmp}/dev-batch-high-${BATCH_ID}-${CSID}"
if [ "$_LOW" -eq "$_HIGH" ]; then
    run_prefix "$_LOW"
    _RED_EXIT=$?
    [ "$_RED_EXIT" -eq 1 ] || { echo "! bisect: candidate prefix did not reproduce red"; exit 1; }
    run_prefix "$((_LOW - 1))"
    _GREEN_EXIT=$?
    [ "$_GREEN_EXIT" -eq 0 ] || { echo "! bisect: preceding prefix did not establish green"; exit 1; }
    echo "culprit candidate: $_LOW"
fi
```

Recover a partially restored trial with this fence. Re-apply only the last listed edit when `git apply --check` succeeds, and remove its ledger line immediately after success. A failed check preserves the remaining ledger for inspection.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
_REVERTED="${TMPDIR:-/tmp}/dev-batch-reverted-${BATCH_ID}-${CSID}"
while [ -s "$_REVERTED" ]; do
    N=$(tail -n 1 "$_REVERTED")
    git apply --check < "${TMPDIR:-/tmp}/dev-batch-patch-${BATCH_ID}-${N}-${CSID}.diff" || { echo "! bisect: inspect edit $N before recovery"; exit 1; }
    git apply < "${TMPDIR:-/tmp}/dev-batch-patch-${BATCH_ID}-${N}-${CSID}.diff" || exit 1
    sed '$d' "$_REVERTED" > "$_REVERTED.next" && mv "$_REVERTED.next" "$_REVERTED" || exit 1
done
```

Restart isolation after a repair with all edits restored. This fence rebuilds each edit's snapshots in a temporary index from its original file set and the repaired tree; the working tree stays intact. Disjoint edit file sets are required. A repair introducing paths outside those sets fails the final tree comparison: inspect and assign those paths to their owning edit before retrying. Each successful restart gets a new batch epoch, clears prior bounds and the reverted ledger, resets the snapshot counter and retakes every prefix snapshot. Then rerun the complete batch through Batch run and regenerate isolated patches and frozen failed nodes with the first fence above; never reuse the prior epoch's fixed set or trial records.

```bash
# timeout: 30000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
IFS= read -r _BATCH_EDITS < "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}" 2>/dev/null || _BATCH_EDITS=0
[ -n "$BATCH_ID" ] && [ "$_BATCH_EDITS" -gt 0 ] || exit 1
_REVERTED="${TMPDIR:-/tmp}/dev-batch-reverted-${BATCH_ID}-${CSID}"
[ ! -s "$_REVERTED" ] || { echo "! bisect: recover removed patches before restart"; exit 1; }
IFS= read -r _EPOCH < "${TMPDIR:-/tmp}/dev-batch-epoch-${CSID}" 2>/dev/null || _EPOCH=0
_EPOCH=$((_EPOCH + 1))
echo "$_EPOCH" > "${TMPDIR:-/tmp}/dev-batch-epoch-${CSID}"
_NEXT_BATCH="${BATCH_ID}-r${_EPOCH}"
_TMP_IDX="${TMPDIR:-/tmp}/dev-batch-restart-idx-${_NEXT_BATCH}-${CSID}"
GIT_INDEX_FILE="$_TMP_IDX" git read-tree HEAD || exit 1
GIT_INDEX_FILE="$_TMP_IDX" git add -A :/ || exit 1
_TREE=$(GIT_INDEX_FILE="$_TMP_IDX" git write-tree) || exit 1
_REPAIRED=$(git commit-tree "$_TREE" -p HEAD -m "batch snapshot") || exit 1
IFS= read -r _BASE < "${TMPDIR:-/tmp}/dev-batch-pre-${BATCH_ID}-1-${CSID}" || exit 1
GIT_INDEX_FILE="$_TMP_IDX" git read-tree "$_BASE" || exit 1
N=0
retake_snapshot() {
    N=$((N + 1))
    _TREE=$(GIT_INDEX_FILE="$_TMP_IDX" git write-tree) || return 1
    _PRE=$(git commit-tree "$_TREE" -p HEAD -m "batch snapshot") || return 1
    echo "$_PRE" > "${TMPDIR:-/tmp}/dev-batch-pre-${_NEXT_BATCH}-${N}-${CSID}"
}
retake_snapshot || exit 1
for _EDIT in $(seq 1 "$_BATCH_EDITS"); do
    IFS= read -r PRE_N < "${TMPDIR:-/tmp}/dev-batch-pre-${BATCH_ID}-${_EDIT}-${CSID}" || exit 1
    IFS= read -r PRE_N_PLUS_1 < "${TMPDIR:-/tmp}/dev-batch-pre-${BATCH_ID}-$((_EDIT + 1))-${CSID}" || exit 1
    _PATHS="${TMPDIR:-/tmp}/dev-batch-paths-${_NEXT_BATCH}-${_EDIT}-${CSID}"
    _PATCH="${TMPDIR:-/tmp}/dev-batch-patch-${_NEXT_BATCH}-${_EDIT}-${CSID}.diff"
    git diff --name-only --no-renames -z "$PRE_N" "$PRE_N_PLUS_1" > "$_PATHS" || exit 1
    : > "$_PATCH"
    while IFS= read -r -d '' _EDIT_PATH; do
        git diff --binary --no-renames "$PRE_N" "$_REPAIRED" -- "$_EDIT_PATH" >> "$_PATCH" || exit 1
    done < "$_PATHS"
    if [ -s "$_PATCH" ]; then GIT_INDEX_FILE="$_TMP_IDX" git apply --cached < "$_PATCH" || exit 1; fi
    retake_snapshot || exit 1
done
git diff --quiet "$_REPAIRED" "$_PRE" || { echo "! bisect: rebuilt snapshots omit repair paths or overlap; keep prior identity"; exit 1; }
: > "${TMPDIR:-/tmp}/dev-batch-low-${BATCH_ID}-${CSID}"
: > "${TMPDIR:-/tmp}/dev-batch-high-${BATCH_ID}-${CSID}"
: > "$_REVERTED"
echo "$_NEXT_BATCH" > "${TMPDIR:-/tmp}/dev-batch-id-${CSID}"
echo "$N" > "${TMPDIR:-/tmp}/dev-batch-n-${CSID}"
echo "$_BATCH_EDITS" > "${TMPDIR:-/tmp}/dev-batch-edits-${_NEXT_BATCH}-${CSID}"
echo "Restarted isolation: $_NEXT_BATCH; $N fresh snapshots"
```

7. Only after all patches are restored and the complete repaired batch passes, write the batch size and clear identity — the only place either happens on a failure branch, mirroring Batch run's success gate. Two fences, never one: a fence containing `rm -f` is flagged `is_dangerous` and declined whole before any manifest lookup, so the write and the clear stay split exactly as in Batch run above. Run the clear fence only if the verification/write fence succeeds. The size sentinel reads `_SKILL` the same way — never a literal `<skill>` placeholder:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name develop) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
IFS= read -r _BATCH_EDITS < "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}" 2>/dev/null || _BATCH_EDITS=1
IFS= read -r _SKILL < "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}" 2>/dev/null || _SKILL="feature"
[ ! -s "${TMPDIR:-/tmp}/dev-batch-reverted-${BATCH_ID}-${CSID}" ] || { echo "! bisect: removed patches still need recovery"; exit 1; }
IFS= read -r DEV_DIR < "${TMPDIR:-/tmp}/dev-${_SKILL}-dev-dir-${CSID}" 2>/dev/null || DEV_DIR=""
_VERIFY_DIR="${DEV_DIR:-.developments}/bisect-${BATCH_ID}-full"
mkdir -p "$_VERIFY_DIR" && : > "$_VERIFY_DIR/test-targets.jsonl" || exit 1
python "$PLUGIN_ROOT/bin/dev_test_targets.py" --skill "$_SKILL" --run --record-dir "$_VERIFY_DIR" || { echo "! bisect: complete batch still fails; preserve edits and identity"; exit 1; }
python "$PLUGIN_ROOT/bin/dev_test_targets.py" --check-record "$_VERIFY_DIR/test-targets.jsonl" --expect-exit 0 || exit 1
echo "$_BATCH_EDITS" > "${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
rm -f "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}"
rm -f "${TMPDIR:-/tmp}/dev-batch-low-${BATCH_ID}-${CSID}" "${TMPDIR:-/tmp}/dev-batch-high-${BATCH_ID}-${CSID}" "${TMPDIR:-/tmp}/dev-batch-reverted-${BATCH_ID}-${CSID}"
```

Bisect reverts happen in-loop, no confirmation prompt — distinct in scope from `foundry--quality-stack.md` §Recovery, which fires only after the full quality stack has already reported failure and stays confirm-first, unchanged.
