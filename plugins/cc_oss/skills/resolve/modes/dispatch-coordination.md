<!-- file: dispatch-coordination.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:resolve -->

## Step 7b join: dispatch and collect conflict resolutions — Run 2 coordination

Run 2 opens with a fan, not a wait: conflict resolution (Steps 6–7a) and the Phase 1 early challenge wave start in one response, after the Step 3d answers are in. Order:

1. **First response** — `TaskUpdate(task_id=TASK_SELECT, status="completed")`: see the **Zero bookkeeping-only turns** table. Include Step 3e's `TaskCreate` batch and, when files conflict, the Step 6 context call (`conflict-resolution.md`; it needs only refs and the Step 3b motivation). The Step 3d confirm block rides here too.
2. **Prep response(s)** — Step 3e's map Write, then Step 8's `cat` load and every block through its prelude, C1-candidate split and verdict re-check, up to the first `Agent()` call. `report` mode skips 3e; zero selected items skips Step 8 and the fan holds Step 7a alone. In the last Prep response — or, with no Prep response (`report` mode and zero selected items), in a response of its own before the Fan response — run the write+gate block below before any fan spawn: the fan outlives the Step 3d contract, and a resume must never re-dispatch 7a while its watch row is open.

Persist the completed intake report in `pr-intelligence.md` and selected scope in `selected-items.txt` before this refresh.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
_PRESERVE="pr=$_PR_NUMBER, impl-dir=$_IMPL_DIR, items=$_IMPL_DIR/action-items.jsonl, selected-items=$_IMPL_DIR/selected-items.txt, item-tasks=$_IMPL_DIR/item-tasks.tsv, conflict-tasks=$_IMPL_DIR/conflict-tasks.tsv, conflict-envelope=$_IMPL_DIR/conflict-envelope.json, watch=$_IMPL_DIR/agent-watch-conflict.tsv + $_IMPL_DIR/agent-watch-challenge.tsv (after fan), Step 3d answered (sentinels hold commit-mode, dispatch-mode, push-auth)"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "Run 2 fan (conflict resolve ‖ Phase 1 early wave)" "$_IMPL_DIR" "$_PRESERVE; phase-report=$_IMPL_DIR/pr-intelligence.md" "resume: never re-issue Step 3d; re-cat own agent-resolution.md, modes/conflict-resolution.md, modes/dispatch-prelude.md and modes/dispatch-phase1.md (the last only when resolve-no-challenge is not true; current phase only); neither agent-watch-conflict.tsv nor agent-watch-challenge.tsv exists → fan not spawned: run the Fan response (7a + Phase 1 early wave, arm watches); else run agent_watch.py first; merge may be uncommitted (MERGE_HEAD) — never re-dispatch Step 7a while agent-watch-conflict.tsv has an open row; collect 7b when conflict-envelope.json exists, then C1, late wave, Phase 2" -- calls_ahead=40 "drop=expanded PR intelligence and action-item analysis"  # timeout: 5000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
python "$PLUGIN_ROOT/bin/compaction_boundary.py" "$_OSS_SHARED"
```

Follow the Boundary step printed above before any fan spawn; report is the run-dir’s `pr-intelligence.md`, with the preserved selection and task maps. Only an opt-in `stop` decision defers the fan until `continue`; an `advise` decision carries its hint into the fan response's final text.

3. **Fan response** — a `continue` or `advise` decision enters the fan: the Step 7a spawn and the Phase 1 early-wave spawns plus Structural prep, all in this one response, each batch armed in its own `agent-watch-*.tsv` (§Agent wait discipline). After spawning and arming its watches, end the turn.

No conflicted files → no 7a; `--no-challenge` or an empty early set → no early wave.

4. **Each notification** — persist any envelope, run the watch check, then consume what landed: a challenge reply goes through Phase 1's verdict processing at once; the conflict envelope goes to 7b below. Order between the two never matters.
5. **7b** (`### 7b: Verify and complete merge` in `conflict-resolution.md`) — confirm `git diff --name-only --diff-filter=U` is empty, no residual conflict markers remain staged, mark each conflict task `completed` (all in one response), commit the merge; `TaskUpdate(task_id=TASK_CONFLICT, status="completed")` rides with the commit call; with zero conflicts there is no 7b, so the close rides with the fan's first response instead. A conflict agent that returned nothing is `timed_out` — surface it with ⏱, run the stop-path release fence in `dispatch-prelude.md` (the run holds the branch mutex since the prelude), and stop before C1 and Phase 2 rather than implementing on an unmerged tree: jump to Step 11 and state `merge uncommitted — finish with git commit, or git merge --abort`. Challenge results stay on disk. Then re-point the HEAD fingerprint the Step 8 prelude took (when it ran) before the merge commit existed; the block refuses when the commit's first parent is not that fingerprint, since a foreign local commit would otherwise be absorbed:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PRIOR < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || _PRIOR=""
[ -z "$_PRIOR" ] || [ "$(git rev-parse HEAD^1)" = "$_PRIOR" ] || { echo "⚠ merge commit's first parent is not the prelude fingerprint — foreign commit during the fan; fingerprint left as is"; exit 0; }
git rev-parse HEAD > "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}"  # timeout: 3000
```

6. **Join complete** = merge committed and every early-wave chunk, single-item retry, caucus and double-timeout gate settled or ⏱. Only then do C1, the Phase 1 late wave and Phase 2 begin (`dispatch-prelude.md`, `dispatch-phase1.md`, `dispatch-phase2.md`): C1 edits the tree, and retries read it.

Nothing to fan (no conflicted files and an empty early set, or `report` mode with no PR#) → no-op, continue with the prep response.

## Step 3e: Create tasks for selected items

`report` mode skips Step 3e, whether or not the report header names a PR. Step 3a already persisted its action items, and Step 8's report-mode task handling expects no `item-tasks.tsv`. When a PR# was found, execute Steps 4–5 after the Step 3d gate, then Steps 6–7a in the Run 2 fan (Step 7b join) before continuing to Step 8; no PR# skips Steps 4–7 entirely. `pr` and `pr+report` create per-item tasks below.

For each item in `SELECTED_ITEMS`, call `TaskCreate` **once per item** — one task per action item; scoped to selected items only, not all pending (avoids bloat when 20+ items exist but only a subset is selected). Issue every item's `TaskCreate` in **one response**, never one per turn — the same response as the Step 7b join's first tool call, so it is never a bookkeeping-only turn:

```text
TaskCreate(
  subject="<type> <summary> — PR #<number>",   # <type> = full string with brackets, e.g. "[gh][req] rename param — PR #42"
  description="Author: @<author> | Change: <change> | Severity: <severity> | File: <file:line or '—'> | <full_comment_text>",
  activeForm="Implementing: <summary>"          # <summary> truncated to 80 chars
)
```

Store returned task ID in each `SELECTED_ITEMS` entry as `task_id`, **then write the whole map in one Write tool call** to `$IMPL_DIR/item-tasks.tsv` — one `<item_id>\t<task_id>` row per selected item, in the response right after the creates return (beside the Step 8 `cat` load), never one append per item. The file is the map; the Step 8 loop reads task IDs from it (a compaction between here and Step 8 would otherwise orphan every per-item task). Then validate it once:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; Step 3b never ran"; exit 1; }
[ -s "$IMPL_DIR/item-tasks.tsv" ] || { echo "! BLOCKED — item-tasks.tsv missing or empty; write it before Step 8"; exit 1; }
# every row: numeric item id, non-empty task id, no unsubstituted <placeholder>; an empty or non-numeric id would be
# parsed by every downstream numeric-only guard as a wrong-but-valid-looking item_id
awk -F'\t' 'NF != 2 || $1 !~ /^[0-9]+$/ || $2 == "" || $0 ~ /[<>]/ || seen[$1]++ { bad = bad " " NR } END { if (bad) { print "! BLOCKED — malformed or duplicate item-tasks.tsv row(s):" bad; exit 1 } print "item-tasks.tsv rows: " NR }' "$IMPL_DIR/item-tasks.tsv"  # timeout: 3000
```

**Applies to `pr` and `pr+report` modes only** — these run Step 3b (which initialises `IMPL_DIR`) and Step 3e. `report` mode skips both steps and has no per-item tasks; Step 3a initialises `IMPL_DIR` instead.
