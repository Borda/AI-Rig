<!-- file: dispatch-closeout.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:resolve -->

## Step 8: Implement action items

*Skip when `SELECTED_ITEMS` is empty — jump to Step 9.*

When skipping: `TaskUpdate(task_id=TASK_IMPL, status="deleted")`. Otherwise: `TaskUpdate(task_id=TASK_IMPL, status="in_progress")`. Timing: see the **Zero bookkeeping-only turns** table.

Step 8 starts inside the Run 2 fan (Step 7b join): its prelude and the Phase 1 early wave run while the merge is still uncommitted; C1, the Phase 1 late wave and Phase 2 wait for the join.

**Codemap index identity (if `CODEMAP_ENABLED=true`)**: resolve the index path the next block reuses. No query runs here — per-item blast radius is dispatch-prelude.md's **Pre-loop blast-radius scan**, which resolves each item's canonical module first and passes it as `rdeps`' positional argument.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"  # timeout: 3000
if [ "$CODEMAP_ENABLED" = "true" ]; then
    # index dir anchors at git root, not cwd — subdir invocation otherwise misses an index that exists. _PROJ = raw basename; scanner writes it unsanitized, so `tr -cd` would seek a filename it never wrote.
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
    _PROJ=$(basename "$_ROOT")
    _IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"
    echo "${_IDX}/${_PROJ}.json" > "${TMPDIR:-/tmp}/resolve-codemap-index-${CSID}"  # timeout: 3000
fi
```

Blast radius, top callers and coupling pairs reach each implementation agent through the prelude's printed per-item `← callers:` lines (blast-radius scan), not from this step.

**Review pre-flight cache** — reuse per-module codemap answers `/review` already computed, so Step 8 blast-radius scan issues 0 duplicate pre-flight queries when a fresh review artifact exists (artifact shape and freshness rule in the `bin/codemap_cache.py` docstring; the source is the `$RUN_DIR/codemap-context.md` that `/oss:review` Step 2 copies from its `modes/codemap-context.md` staging; requires codemap). Locate latest review run-dir, materialize per-module cache once, before per-item loop. The newest context of any PR is taken on purpose: `codemap_cache.py read` freshness-gates every answer against the current index, so a context from another PR degrades to a cache miss, never to wrong data:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"  # timeout: 3000
IFS= read -r _IDX_FILE < "${TMPDIR:-/tmp}/resolve-codemap-index-${CSID}" 2>/dev/null || _IDX_FILE=""  # git-root-anchored path from the index-identity block
CODEMAP_CACHE_DIR=""
if [ "$CODEMAP_ENABLED" = "true" ]; then
    CODEMAP_CACHE_DIR=".temp/resolve/codemap-context"  # resolve-owned; stable across the run
    mkdir -p "$CODEMAP_CACHE_DIR"  # timeout: 3000
    # review's pre-flight blob: .temp/review/<ts>/codemap-context.md
    _REVIEW_CTX=$(ls -t .temp/review/*/codemap-context.md 2>/dev/null | head -1)
    if [ -n "$_REVIEW_CTX" ] && [ -f "$PLUGIN_ROOT/bin/codemap_cache.py" ]; then
        # .md wraps codemap-py query batch JSON under md headers — extract
        _BATCH_JSON="${TMPDIR:-/tmp}/resolve-review-batch-${CSID}.json"
        sed -n '/^{/,$p' "$_REVIEW_CTX" | head -1 > "$_BATCH_JSON" 2>/dev/null || true
        if [ -s "$_BATCH_JSON" ] && [ -f "$_IDX_FILE" ]; then
            python "$PLUGIN_ROOT/bin/codemap_cache.py" write \
                --batch "$_BATCH_JSON" --index "$_IDX_FILE" --cache-dir "$CODEMAP_CACHE_DIR" 2>/dev/null || true  # timeout: 5000
            echo "→ Review pre-flight cache materialized from $_REVIEW_CTX"
        fi
    fi
fi
echo "${CODEMAP_CACHE_DIR}" > "${TMPDIR:-/tmp}/resolve-codemap-cache-dir-${CSID}"  # timeout: 3000
```

`dispatch-prelude.md`'s per-item blast-radius scan reads this cache first (freshness-gated `codemap_cache.py read`) and only calls `codemap-py query` on a cache miss — see its **Pre-loop blast-radius scan**. Empty `CODEMAP_CACHE_DIR` (no review artifact, or oss helper absent) → every module is a cache miss and the scan queries live, unchanged from prior behaviour.

<!-- Step 8 defined in modes/dispatch-prelude.md, dispatch-phase1.md, dispatch-phase2.md, dispatch-phase3.md -->

> loads: dispatch-prelude.md · dispatch-phase1.md · dispatch-phase2.md · dispatch-phase3.md

Read `--no-challenge` from its sentinel, not from the raw argument blob — SKILL.md Step 1 strips every flag token before mode parsing, so `$ARGUMENTS` no longer carries it here. The block loads `dispatch-phase1.md` only without the flag:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
[ -f "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}" ] && IFS= read -r NO_CHALLENGE < "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}" || NO_CHALLENGE="false"
echo "NO_CHALLENGE=$NO_CHALLENGE"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/dispatch-prelude.md"
[ "$NO_CHALLENGE" = "true" ] || python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/dispatch-phase1.md"  # timeout: 5000
```

`NO_CHALLENGE=true` → skip Phase 1 entirely; `SURVIVING_ITEMS` = all `SELECTED_ITEMS`, every item treated `VALID`, Challenge Log section omitted from the report.

Execute the prelude (IMPL_AGENT routing, IMPL_DIR init, blast-radius scan, plus a branch mutex + HEAD fingerprint so a second concurrent resolve aborts and an external mid-flight write surfaces at merge-back, C1, shared Challenge-log append block), then run the three-phase dispatch directly in the orchestrator. Load each later phase file only when that phase starts — on resume, re-cat the prelude plus the current phase file only:

1. Phase 1 challenge (parallel by domain, read-only) — `dispatch-phase1.md`, loaded above.

2. Phase 2 implementation (parallel, one isolated `git worktree` per specialist; groups formed by specialist then a file-ownership + import-coupling tiebreak so items that would collide on same file — or across an import edge — land in one worktree). Load once the Step 7b join, C1 and the Phase 1 late wave have finished:

   ```bash
   PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""
   python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/dispatch-phase2.md"  # timeout: 5000
   ```

3. Phase 3 merge-back (sequential cherry-pick, whole worktree groups ordered most-central-first so foundational commits land before dependents, `TaskUpdate` per item as its commit lands). Load once every Phase 2 group's fences ran or it is ⏱:

   ```bash
   PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""
   python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/dispatch-phase3.md"  # timeout: 5000
   ```

`TaskUpdate` calls stay orchestrator-owned throughout — Phase 1/2 subagents never touch task list (subagent can't drive parent's task list); only Phase 3, run by orchestrator itself after each cherry-pick, flips a task to `completed`. Explains why tasks flip in item-priority order during Phase 3 even though the work producing them ran concurrently in Phase 2.

Step 8 has no per-pass item cap: every selected item runs in one pass, never a rerun for a remainder. It bounds load per agent instead — ≤12 items per Phase 1 challenge agent (`ceil(n/12)` chunks per domain), ≤5 per Phase 2 implementation spawn (≤8 under `per-specialist`; one file's overflow runs as sequential chained links) — and paces both in pool-capped waves.

**Straggler gate — before leaving Step 8**: the dispatch files' per-item close-out (REJECT, skipped, cherry-pick landed — including the C1 Codex-direct shortcut, which never enters Phase 1/2/3 at all) should have already terminated every id in `item-tasks.tsv`; this catches whichever one didn't. UNVERIFIED tasks are durable pending obligations with owner/next_check/closure, excluded from implementation recovery and carried into Step 9/final Open Concerns. Never move on to Step 9 over any other open child — that hid the original leak. Fails closed on a lost `IMPL_DIR` sentinel: distinct from "no items were selected," which the file's own absence still reports safely. One Bash call, run once, unedited: the gate loop lists each open id in `straggler-check-ids.txt`, then the confirmation loop checks every listed id in the same run.

```bash
# timeout: 8000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot verify child tasks before leaving Step 8"; exit 1; }
if [ -f "$IMPL_DIR/item-tasks.tsv" ]; then
    : > "$IMPL_DIR/straggler-check-ids.txt"  # the confirmation loop below reads its ids here, never from a placeholder
    _SKIPPED_IDS=$(cut -f1 "$IMPL_DIR/skipped-items.txt" 2>/dev/null)
    # anchored right after id= — resolution= is the field placed there, before any free-text field
    # (dispatch-prelude.md's producer template), so a reviewer's quoted text can never match it
    _REJECTED_IDS=$(grep -iE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null | sed -n 's/^id[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p')
    _ALREADY_FIXED_IDS=$(sed -n 's/^id=\([0-9][0-9]*\) resolution=already-fixed evidence=ALREADY_FIXED .*/\1/p' "$IMPL_DIR/challenge-log.txt")
    _UNVERIFIED_IDS=$(sed -n 's/^id=\([0-9][0-9]*\) resolution=unverified evidence=UNVERIFIED .*/\1/p' "$IMPL_DIR/challenge-log.txt")
    while IFS=$'\t' read -r item_id task_id; do
        case "$item_id" in ''|*[!0-9]*) continue ;; esac
        { printf '%s\n' "$_ALREADY_FIXED_IDS" | grep -qx "$item_id"; } && printf 'closed: item=%s (already-fixed)\n' "$item_id" && continue
        { printf '%s\n' "$_UNVERIFIED_IDS" | grep -qx "$item_id"; } && printf 'pending: item=%s (unverified) — retain owner/next_check/closure; no implementation recovery\n' "$item_id" && continue
        { printf '%s\n' "$_SKIPPED_IDS" "$_REJECTED_IDS" | grep -qx "$item_id"; } && printf 'closed: item=%s (rejected/skipped)\n' "$item_id" && continue
        [ -n "$task_id" ] || { echo "! skipping — item $item_id has an empty task id in item-tasks.tsv"; continue; }
        printf 'check: item=%s task=%s\n' "$item_id" "$task_id"  # not rejected/skipped — must have landed a commit; verify below, never assume
        printf '%s\n' "$item_id" >> "$IMPL_DIR/straggler-check-ids.txt"
    done < "$IMPL_DIR/item-tasks.tsv"
else
    echo "n/a — report mode or no items selected"
    exit 0
fi
# timeout: 3000
IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || COMMIT_MODE="unset"
IFS= read -r _BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || _BASE_SHA=""
[ -s "$IMPL_DIR/straggler-check-ids.txt" ] || { echo "n/a — no check: line above"; exit 0; }
[ -n "$_BASE_SHA" ] || echo "⚠ resolve-base-sha sentinel missing — confirmation below is unscoped, verify the matched commit's date/author before trusting it"
# every check: id from the gate loop's file, never an item_id placeholder: one invariant run confirms them all
case "$COMMIT_MODE" in each|grouped|all|stage) ;; *) echo "! BLOCKED — COMMIT_MODE is '$COMMIT_MODE', not each/grouped/all/stage"; exit 1 ;; esac
while IFS= read -r _ITEM_ID || [ -n "$_ITEM_ID" ]; do
    case "$_ITEM_ID" in ''|*[!0-9]*) continue ;; esac
    case "$COMMIT_MODE" in
        each)
            # anchored to the literal attribution token commit_action_item.py --build emits, and
            # range-bound to this run's commits — an unanchored, unscoped grep can match a different
            # item's id as a substring (No.1 inside No.12) or a prior run's commit on the same branch
            _HITS=$(git log --oneline -E --grep="\[resolve No\.${_ITEM_ID}\]" ${_BASE_SHA:+"$_BASE_SHA..HEAD"})  # timeout: 5000
            [ -n "$_HITS" ] && printf 'MATCH — item %s: %s\n' "$_ITEM_ID" "$_HITS" || echo "NO MATCH — item ${_ITEM_ID} has no [resolve No.${_ITEM_ID}] commit"
            ;;
        grouped)
            # tokenize + exact-match, no boundary regex: `\b` is GNU-only (never compiles under `git log -E` POSIX ERE);
            # --oneline is fail-open (subject only, never the body holding the item list → stray sha/subject digits false-confirm).
            # --format=%B reads the body's "[resolve group] … items <ids>" line; grep -qx exact token: "PR #1" never matches 1, "30" never 3
            # (verified on a live repo: group commits "30 4" and "7 8" — every real id matched, "1" did not)
            git log --format=%B --grep="items" ${_BASE_SHA:+"$_BASE_SHA..HEAD"} \
                | grep -E '^\[resolve group\]' | tr ' ' '\n' | grep -qx "${_ITEM_ID}" \
                && echo "MATCH — item ${_ITEM_ID} found in a group commit" \
                || echo "NO MATCH — item ${_ITEM_ID} not found in any group commit"  # timeout: 5000
            ;;
        all|stage)
            echo "item ${_ITEM_ID}: no per-item token exists in the commit message for COMMIT_MODE=$COMMIT_MODE — grep cannot confirm this item; use this turn's own memory of Phase 3's PLAN_FILE/cherry-pick output, or: git diff --cached --stat / git show --stat against the item's .file"
            ;;
    esac
done < "$IMPL_DIR/straggler-check-ids.txt"
```

- Bash printed `n/a` → no per-item tasks were created, proceed straight to the flip below.
- `closed:` lines need nothing further; ALREADY_FIXED already completed its task from validated historical evidence.
- `pending:` lines remain pending and visible in resolution.jsonl/final Open Concerns; never require an implementation commit, delete the task, or enter straggler implementation recovery for these UNVERIFIED obligations.
- A `! skipping` line names a malformed row (empty task id) — investigate `item-tasks.tsv` directly via `TaskList`/`grep` for that item id before proceeding; never guess its status.
- For every `check:` line: call `TaskList`; if that task is already `completed`/`deleted`, done. If it's still open, do NOT default it to `completed` — confirm independently that item's commit is actually on the branch before calling `TaskUpdate(status="completed")`. The block's confirmation half already printed one `MATCH`/`NO MATCH` line per `check:` id, read from `straggler-check-ids.txt` — use the lines for ids whose task is still open. The confirmation command depends on `COMMIT_MODE` — only `each` carries a per-item attribution token; `grouped` folds several ids into one message; `all`/`stage` carry none at all (`stage` never commits — the diff stays staged, per its own contract). A lost `resolve-base-sha` sentinel degrades the confirmation to an unscoped search across the whole branch, which can false-confirm a never-implemented item against a prior run's commit on the same branch — the block warns and treats any match with extra suspicion in that case; both reads and the mode dispatch happen inside it, not by hand.

No confirming commit found → this item was never closed by any exit path; that's the exact defect this gate exists to catch — surface it via `AskUserQuestion` (dispose as `deleted` with a stated reason, or leave open and investigate) rather than guessing either status. Only once every printed id is accounted for, continue to Step 9. Batch every `TaskList`/`TaskUpdate` this gate needs into one response.

`TaskUpdate(task_id=TASK_IMPL, status="completed")` — only once every id is accounted for; see the **Zero bookkeeping-only turns** table.
