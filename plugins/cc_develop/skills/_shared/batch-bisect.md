<!-- file: batch-bisect.md — consumers: batch-mode.md §On batch failure -->

### On batch failure — bisect, never blind revert

Edit N's isolated patch is the diff between its own pre-snapshot and the next edit's pre-snapshot — never the cumulative pre-snapshot alone, and never `git apply -R` on a snapshot that predates edits other than N. The batch's edit count is derived once (never left to prose), then a real loop assigns `N` and computes every patch up front — nothing here waits for a "calling procedure" to substitute `N` by hand:

```bash
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
```

1. Split the batch in half.
2. Revert the second half's edits via `git apply -R < <patch-N>` per edit, newest-applied first — never the targeted revert's `git checkout HEAD -- <file>` (`foundry--quality-stack.md` §Recovery), which is file-scoped and would discard every edit to a shared file, not just the reverted one. The non-overlap predicate (disjoint files) guarantees each isolated patch applies/reverts cleanly against the current tree regardless of order within its half.
3. Re-run the first half's test set.
4. First half green → the culprit is in the reverted half; re-apply it edit-by-edit (still bisecting) until the single failing edit is isolated.
5. First half also red → the culprit is in the first half; recurse there instead, leaving the second half reverted.
6. Worst case for a 4-edit batch: `ceil(log2(4)) = 2` extra runs to isolate the culprit.
7. Once the culprit is isolated (or every edit confirmed individually), write the batch size and clear identity — the only place either happens on a failure branch, mirroring Batch run's success gate. Two fences, never one: a fence containing `rm -f` is flagged `is_dangerous` and declined whole before any manifest lookup, so the write and the clear stay split exactly as in Batch run above. The size sentinel reads `_SKILL` the same way — never a literal `<skill>` placeholder:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
IFS= read -r _BATCH_EDITS < "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}" 2>/dev/null || _BATCH_EDITS=1
IFS= read -r _SKILL < "${TMPDIR:-/tmp}/dev-batch-skill-${CSID}" 2>/dev/null || _SKILL="feature"
echo "$_BATCH_EDITS" > "${TMPDIR:-/tmp}/dev-${_SKILL}-batch-size-${CSID}"
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r BATCH_ID < "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" 2>/dev/null || BATCH_ID=""
rm -f "${TMPDIR:-/tmp}/dev-batch-id-${CSID}" "${TMPDIR:-/tmp}/dev-batch-n-${CSID}" "${TMPDIR:-/tmp}/dev-batch-edits-${BATCH_ID}-${CSID}"
```

Bisect reverts happen in-loop, no confirmation prompt — distinct in scope from `foundry--quality-stack.md` §Recovery, which fires only after the full quality stack has already reported failure and stays confirm-first, unchanged.
