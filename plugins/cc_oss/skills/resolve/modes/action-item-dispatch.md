<!-- oss:resolve Step 8 — executed via: cat $_OSS_RESOLVE/modes/action-item-dispatch.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md -->

<!-- Input: SELECTED_ITEMS (from Step 3e), COMMIT_MODE + GROUP_STRATEGY (from Step 3d), CODEX_AVAILABLE (from Step 1), PR_REF (from Step 4), $_OSS_RESOLVE, ARGUMENTS -->

<!-- Output: items implemented/staged/committed; CHALLENGE_LOG populated; CHANGE_SCOPE set for Step 9 -->

## Step 8: Implement action items

**Commit authorization — entire Step 8**: `COMMIT_MODE` from Step 3d governs all commits; never re-ask regardless of mode, item count, or sentinel state. Multiple resolve flows per session each honor own Step 3d choice.

Determine implementation agent, set up file-handoff dir, and authorize commits before the loop:

`IMPL_AGENT`'s default is a routing marker, not a value passed to `Agent(subagent_type=)`. It records that unrouted work belongs to the bridge, and is read by the C1 medium-effort shortcut (which dispatches `Skill(skill="bridge:implement")`) and by the >8-item batching gate in `SKILL.md`. Phase 2 never uses it: it groups by the `change` → specialist table below, whose values are all real subagent types. Only `--agent <name>` puts a caller-supplied value in this variable, and that value does reach `Agent(subagent_type=)`.

Substitute the Step 3e selection into `SELECTED_ITEMS` below as space-separated ids. It is orchestrator-held state, so this block is the only place it enters the shell; every later block reads the file this one writes.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags worktree --value-flags agent "$ARGUMENTS")"  # timeout: 5000
IMPL_AGENT="${VALUE_AGENT:-bridge:implement}"
[ -n "$VALUE_AGENT" ] && echo "→ Using --agent: $IMPL_AGENT"

# IMPL_DIR sentinel written at mktemp time in pr-intelligence.md — re-read here, never re-create
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -z "$IMPL_DIR" ] && { IMPL_DIR=$(mktemp -d); echo "$IMPL_DIR" > "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}"; }  # timeout: 3000 — report-mode has no Step 3b
mkdir -p "$IMPL_DIR"  # timeout: 3000
SELECTED_ITEMS="<space-separated selected ids>"
case "$SELECTED_ITEMS" in *'<'*'>'*|"") echo "! BLOCKED — SELECTED_ITEMS still holds the placeholder; substitute the Step 3d ids before running this block"; exit 1 ;; esac
printf '%s\n' "$SELECTED_ITEMS" > "$IMPL_DIR/selected-items.txt"
CHALLENGE_LOG="$IMPL_DIR/challenge-log.txt"; : > "$CHALLENGE_LOG"  # one record per line: id=… resolution=… evidence=… suggestion=… finding=… evidence_why=… suggestion_why=… detail=… — resolution right after id, before any free-text field, so a reviewer's quoted text can never be mistaken for it (every consumer greps this by field name, never by position, so the order itself carries no other meaning); file, not shell array: survives compaction + separate Bash calls, Step 11 renders from it
: > "$IMPL_DIR/skipped-items.txt"  # item_id<TAB>reason, one per line — Phase 2 appends (fenced block below), Phase 3 close-out consumes; initialized empty so a no-skip run still has a readable file
: > "$IMPL_DIR/phase2-commits.jsonl"  # one JSON object per line: {"item_id","sha","group"} — Phase 2 appends per group (fenced block below), Phase 3's build_merge_plan.py producer consumes; initialized empty so an all-C1/all-rejected run still has a readable file
: > "$IMPL_DIR/c1-deferred-files.txt"  # one file path per line — C1 fence appends for non-each COMMIT_MODE (fenced block below), Phase 3's clean-run staging fence consumes; initialized empty so a run with no C1 items (or CODEX_AVAILABLE=false) still has a readable file
: > "$IMPL_DIR/specialist-worktrees.txt"  # one absolute path per line — Phase 2 appends per group (fenced block below), Phase 3's cleanup loop consumes; initialized empty so an all-C1/all-rejected run (no Phase 2 dispatch) still has a readable file
```

**Concurrency guard — mutex + HEAD fingerprint** (Phase 2 holds worktrees open for the slowest specialist's whole runtime — minutes — so an external write to the branch, or a second resolve run, is far likelier to land mid-flight than under the old per-item design). The lock path is deterministic (recompute anytime from the git-common-dir + branch); the base SHA is a point-in-time value, so persist it to a tmpfile — shell vars don't survive between Step 8's separate bash calls:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" ] && IFS= read -r PR_REF < "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" || PR_REF="#${PR_NUMBER:-0}"  # set in Step 4 — #<N> same-repo, full PR_URL when committing to a fork
_GITDIR=$(git rev-parse --git-common-dir 2>/dev/null || echo ".git")  # timeout: 3000
_BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo "detached")  # timeout: 3000
RESOLVE_LOCK="$_GITDIR/oss-resolve-${_BRANCH}.lock"  # shared across worktrees (git-common-dir)
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/heal_git_artifacts.py" locks --pattern 'oss-resolve-*.lock' --apply  # timeout: 30000
if [ -f "$RESOLVE_LOCK" ]; then
    echo "⛔ another oss:resolve is active on branch '$_BRANCH' (lock: $RESOLVE_LOCK) — aborting."
    echo "  Healer kept it: holder PID is alive and the lock is under the age cap. Wait, or delete it if you know that run died."
    exit 1
fi
echo "$PPID $(date -u +%FT%TZ)" > "$RESOLVE_LOCK"  # PPID not $$ — $$ is a fresh shell per Bash call, dead before the next one reads it  # timeout: 3000
git rev-parse HEAD > "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || true  # HEAD fingerprint  # timeout: 3000
```

Lock released in Phase 3's cleanup block. Crash before that leaks it — by design: no `trap` can release it, since trap disposition is per-process and dies with the Bash call that registers it (`research:fortify` documents the same constraint). The healer is the recovery path: it reclaims a lock whose holder PID is provably dead immediately, and any lock past the 30-min age cap regardless. It sweeps **every** `oss-resolve-*.lock` in the common dir, not just this branch's — a leak on a branch never resolved again is otherwise never revisited and survives indefinitely (observed: 27 days).

**Reclaiming here is automatic but never silent** — print the healer's output verbatim whenever it reclaimed anything, naming each lock and why (dead holder / age). No approval gate: a lock file with a provably dead holder carries no user work, and this replaces an override that already fired unattended at 30 minutes. Worktree healing is the opposite case and does gate on approval — see `worktree-isolation.md`.

`change` → `IMPL_AGENT` routing table — drives Phase 2 specialist grouping **unconditionally** (not gated behind `CODEX_AVAILABLE`; Codex only ever handles items via the C1 medium-effort shortcut below, never as a Phase 2 specialist group). Keep in sync with `_shared/review-section-taxonomy.md`'s resolve `change` column:

| `change` value | `IMPL_AGENT` |
| -- | -- |
| `code` · `refactor` · `config` · `ci` | `foundry:sw-engineer` |
| `test` | `foundry:qa-specialist` |
| `docs` | `foundry:doc-scribe` |
| `style` | `foundry:linting-expert` |
| `perf` | `foundry:perf-optimizer` |
| `architecture` | `foundry:solution-architect` |

`CODEX_AVAILABLE=false`: C1 (medium-effort Codex shortcut, below) is skipped entirely — medium-effort items fall through to Phase 1+2 like any other item, routed by this same table. `xhigh`-effort, multi-file items still skip (`⚠ bridge@borda-ai-rig is absent or disabled — skipping item #<id> (xhigh effort)`) — too much surface for a single specialist without the bridge's broader Codex context; never blanket-skip anything below `xhigh`.

`--agent <name>` overrides this routing table unconditionally — every Phase 2 group uses `<name>` regardless of `change`.

> **Conflict gate**: verify all Step 5a conflict tasks `completed` before any action item. Still `pending`/`in_progress` → stop, surface list, wait. Items on unresolved conflicts compound diff.

Process items in `SELECTED_ITEMS` (from Step 3e) in priority order (`[req]` first, then `[suggest]`).

**Codex effort classification** — classify each item before dispatch; set `ITEM_EFFORT`; aggregate to `CHANGE_SCOPE` for Step 9:

- typo/spelling/whitespace/formatting/comment/rename-single/docstring → `medium`; multi-file/refactor/architecture/new-feature/redesign → `xhigh`; all else → `high` (default)
- Minimum effort is always `medium` — never `low`
- `ITEM_EFFORT` set per item; include in agent prompt as `"Effort level: $ITEM_EFFORT.\n..."` prefix
- `CHANGE_SCOPE` = aggregate across all `SELECTED_ITEMS`:
  - ALL items classified `medium` → `CHANGE_SCOPE=lint-only`
  - ANY item classified `xhigh` → `CHANGE_SCOPE=full`
  - otherwise → `CHANGE_SCOPE=targeted` (default)
- Compute `CHANGE_SCOPE` once before the loop; pass to Step 9 via shell variable

**Caps** — soft cap 10, hard cap 20 items per dispatch. When `SELECTED_ITEMS` > 10 (count from context, or `wc -w < "$IMPL_DIR/selected-items.txt"` after re-reading `IMPL_DIR` from its sentinel): invoke `AskUserQuestion` — (a) Apply first 10 now, re-run for remainder · (b) Apply all `[req]` only · (c) Proceed with all up to 20 (slow, context risk). Never silently start loop with >10 items; never exceed 20 in one dispatch (context budget boundary).

**Parallel specialist-worktree dispatch** (replaces sequential/one-item-at-a-time execution — real wall-clock lever, not just spawn-count reduction): C1 Codex-first routing (below) still runs first — batched ≤3 disjoint-file items per call, same-file items sequential. Everything falling through C1 splits into three passes: **Phase 1** challenge (read-only, parallel by domain), **Phase 2** implementation (one isolated `git worktree` per specialist, parallel), **Phase 3** merge-back (sequential, orchestrator-owned cherry-pick in original priority order). See Phase 1/2/3 below.

**Per action item** — loop over `SELECTED_ITEMS` in priority order. Per item, read full details from `$IMPL_DIR/action-items.jsonl` (written by Step 3b pr-intelligence subagent) — this is the authoritative source for `full_comment_text`, `file`, `line`, `change`, `severity`, `author`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
ITEM_DATA=$(jq -c ". | select(.id == <id>)" "$IMPL_DIR/action-items.jsonl")  # timeout: 5000
```

Use `.full_comment_text` for `IMPL_PROMPT`, `.file`/`.line` for commit scope and blast-radius lookup, `.change`/`.severity` for effort classification and agent routing.

**Pre-loop blast-radius scan** — run once in main orchestrator before loop starts; collect caller context per item so each impl subagent knows which contracts to preserve. Soft: missing `codemap-py query` is a no-op.

Group selected items by canonical module before querying: one `rdeps` answer per module per pre-loop, shared with every matching item. Read the **review pre-flight cache** first (materialized in SKILL.md Step 8; contract in `$_DEV_SHARED/codemap-context.md` §Review→resolve pre-flight cache). `codemap_cache.py read` validates index freshness; reuse requires an actual `rdeps` answer, including a valid empty caller list. Cache miss → one live query. Never use empty rendered text as a cache-miss signal. Reused hits retain their `delta.notes` marker for the existing health report.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""  # prelude's mktemp path
[ -f "$IMPL_DIR/selected-items.txt" ] && IFS= read -r SELECTED_ITEMS < "$IMPL_DIR/selected-items.txt" || SELECTED_ITEMS=""
# pre-loop; BLAST_RADIUS_CONTEXT shared with impl agents
BLAST_RADIUS_CONTEXT=""
[ -f "${TMPDIR:-/tmp}/resolve-codemap-cache-dir-${CSID}" ] && IFS= read -r CODEMAP_CACHE_DIR < "${TMPDIR:-/tmp}/resolve-codemap-cache-dir-${CSID}" || CODEMAP_CACHE_DIR=""  # timeout: 3000
# index dir anchors at git root, not cwd; raw basename, no `tr -cd` — the scanner writes the name unsanitized, so stripping would seek a file it never wrote
_ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
_IDX_FILE="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}/$(basename "$_ROOT").json"
_CACHE_BIN="${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/codemap_cache.py"
if command -v codemap-py >/dev/null 2>&1 && [ -f "$IMPL_DIR/action-items.jsonl" ]; then
    echo "→ Codemap pre-scan — caller context for selected action items:"
    # file→module from the index's own `name` field (same source as §Structural prep, and as the cache keys). A sed transform names pkg/__init__.py `pkg.__init__` while codemap calls it `pkg` — every package-init item then missed its cache entry AND errored on the live query.
    _MODMAP="$IMPL_DIR/codemap-maps.json"
    _MAP_STAMP=$(python -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); s=p.stat(); print(f"{p.resolve().as_posix()}:{s.st_size}:{s.st_mtime_ns}")' "$_IDX_FILE" 2>/dev/null)
    _ALL_PY=$(jq -r '.file // empty' "$IMPL_DIR/action-items.jsonl" | grep '\.py$' | paste -sd, -)  # timeout: 5000
    codemap-py query --timeout 15 central --top 100000 2>/dev/null \
        | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_centrality.py" --files "$_ALL_PY" > "$_MODMAP" 2>/dev/null || : > "$_MODMAP"  # timeout: 20000
    printf '%s\n' "$_MAP_STAMP" > "$IMPL_DIR/codemap-maps.stamp"
    _MODULE_ITEMS=$(jq -s --arg ids "$SELECTED_ITEMS" --slurpfile maps "$_MODMAP" '
        ($ids | split(" ")) as $selected |
        map(select((.id | tostring) as $id | $selected | index($id))) |
        map(. + {module: ($maps[0].file_module[.file] // "")}) |
        map(select(.module != "")) | group_by(.module) |
        map({key: .[0].module, value: map(.id)}) | from_entries
    ' "$IMPL_DIR/action-items.jsonl" 2>/dev/null)
    for _m in $(printf '%s' "$_MODULE_ITEMS" | jq -r 'keys[]'); do
        _c=""
        _CACHE_HIT=false
        # cache-first: reuse review's rdeps answer when fresh; only query on miss
        if [ -n "$CODEMAP_CACHE_DIR" ] && [ -f "$_CACHE_BIN" ] && [ -f "$_IDX_FILE" ]; then
            _V=$(python "$_CACHE_BIN" read --module "$_m" --index "$_IDX_FILE" --cache-dir "$CODEMAP_CACHE_DIR" 2>/dev/null)  # timeout: 5000
            if printf '%s' "$_V" | jq -e --arg module "$_m" '
                .reuse == true and (.answers.rdeps | type == "object") and
                (.answers.rdeps.module == $module) and (.answers.rdeps.imported_by | type == "array") and
                (.answers.rdeps.error == null) and
                ([.answers.rdeps, .answers.rdeps.index // {}] | all(
                    .query_complete != false and .exhaustive != false and
                    .stale != true and .root_mismatch != true and .truncated != true
                ))
            ' >/dev/null 2>&1; then
                _CACHE_HIT=true
                _c=$(printf '%s' "$_V" | python -c "import json,sys; print(json.dumps(json.load(sys.stdin)['answers']['rdeps']))")
                _ART="$CODEMAP_CACHE_DIR/${_m}.json"
                [ -f "$_ART" ] && python -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); d['delta']['notes'].append('reused'); json.dump(d,open(p,'w'))" "$_ART" 2>/dev/null || true
            fi
        fi
        [ "$_CACHE_HIT" = true ] || _c=$(codemap-py query rdeps "$_m" 2>/dev/null)  # timeout: 10000
        if [ -n "$_c" ]; then
            for _id in $(printf '%s' "$_MODULE_ITEMS" | jq -r --arg module "$_m" '.[$module][]'); do
                printf "  #%s %s ← callers: %s\n" "$_id" "$_m" "$(echo "$_c" | tr '\n' ' ')"
                BLAST_RADIUS_CONTEXT+="item #${_id} (${_m}) callers:"$'\n'"${_c}"$'\n\n'
            done
        fi
    done
    [ -z "$BLAST_RADIUS_CONTEXT" ] && echo "  (no Python callers found for selected items)"
    # health metric — reuse_ratio over the materialized cache
    [ -n "$CODEMAP_CACHE_DIR" ] && [ -f "$_CACHE_BIN" ] && python "$_CACHE_BIN" report --cache-dir "$CODEMAP_CACHE_DIR" 2>/dev/null || true  # timeout: 5000
fi
```

Per item before impl dispatch, extract this item's caller section:

```bash
item_id=$_id  # align with blast-radius scan loop variable
ITEM_CALLERS=$(awk "/^item #${item_id} /,/^[[:space:]]*$/" <<< "$BLAST_RADIUS_CONTEXT" | tail -n +2)
```

Include non-empty `$ITEM_CALLERS` in impl agent prompt — see Phase 2.

**C1 — Codex-first routing for `medium` effort items** (skip Phase 1+2 when Codex handles it):

When `ITEM_EFFORT=medium` AND `CODEX_AVAILABLE=true`: dispatch Codex for evidence check + implementation. **Batch disjoint-file items** — collect all C1-eligible items first, group ≤3 per call with pairwise-distinct `file` values (two items on the SAME file always go in separate calls — a shared-file batch makes the per-item diff inseparable, breaking one-commit-per-item and per-item CHALLENGE_LOG attribution). Each blocking round-trip then covers up to 3 items instead of 1.

```text
Skill(skill="bridge:implement", args="Effort level: medium. Review each action item independently and implement it if valid. Items touch disjoint files — never edit one item's file for another item.
Item <id>: <full_comment_text>  File: <file>  Line: <line>
[repeat per item, ≤3]
Each reviewer assertion is itself an unproven claim — if it asserts a fact the file alone can't settle (name/identifier/version/count wrong or non-standard), verify against the actual authoritative source before treating it as valid; can't verify → UNCERTAIN, not DONE. Verdicts are per item — one UNCERTAIN never blocks another item's DONE.
Return ONLY compact JSON array as your FINAL message, one element per item:
[{\"id\":<id>,\"verdict\":\"DONE\"|\"UNCERTAIN\",\"reason\":\"<one sentence>\",\"files_changed\":[\"<path>\", …]}, …]")
```

Parse the JSON array — per element, in item priority order:

- **DONE** → mark item resolved; commit/stage that item's `files_changed` using the fence below (per-item commit stays granular — the disjoint-file grouping guarantees the diff separates); append to `CHALLENGE_LOG`: `id=<id> resolution=codex-direct evidence=VALID suggestion=VALID finding=<full_comment_text, truncate ~80 chars> evidence_why=<Codex reason> suggestion_why=<Codex reason> detail=<Codex reason>` — `resolution=` sits right after `id=`, before any free-text field, so a reviewer's quoted text inside `finding=`/`detail=` can never be mistaken for it; skip Phase 1+2 for that item
- **UNCERTAIN** → that item falls through to Phase 1+2 (normal challenge + implementation flow)
- element missing for a dispatched item → treat as UNCERTAIN, never silently resolved

**Only `each` mode commits here.** `git add`-ing a C1 item's files before Phase 3 runs — even a `stage`/`grouped`/`all` item, even touching a file no Phase 2 item touches — leaves the index non-clean, and Phase 3's `merge_specialist_batch.py` cherry-picks refuse to run against a non-clean index (confirmed empirically: `git cherry-pick` exits 128, "your local changes would be overwritten", before it even reaches the merge). An **unstaged** working-tree edit does not trigger that refusal — **only when the C1 item's file is disjoint from every Phase 2 item's file**; an unstaged edit on a file a Phase 2 cherry-pick also touches reproduces the identical refusal (empty file list, no `CHERRY_PICK_HEAD`, no recovery route), confirmed empirically. C1 is invisible to Phase 2's file-ownership tiebreak (it skips Phase 1/2 entirely, so nothing else in the file compares a C1 item's file against a Phase 2 item's) — the merge fence's own guard right before it calls `merge_specialist_batch.py` is what actually catches this overlap; it is what makes deferring here safe, not the unstaged/staged distinction alone. So `stage`/`grouped`/`all` C1 items are left as plain unstaged edits here and only `git add`ed after Phase 3 returns clean (see the fence right after the `merge_specialist_batch.py` call below):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
[ -f "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" ] && IFS= read -r PR_REF < "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" || PR_REF="#${PR_NUMBER:-0}"
[ -f "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" ] && IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" || COMMIT_MODE="unset"
_SUMMARY="<short summary>"; _ID="<id>"; _AUTHOR="<author>"; _COMMENT="<full_comment_text>"; _FILES=(<files_changed for this item, one per array element>)
case "$_SUMMARY$_ID$_AUTHOR$_COMMENT${_FILES[*]}" in *'<'*'>'*) echo "! BLOCKED — C1 commit placeholder not substituted"; exit 1 ;; esac
if [ "$COMMIT_MODE" = "each" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_action_item.py" --build --summary "$_SUMMARY" \
        --item-id "$_ID" --author "$_AUTHOR" --pr "$PR_REF" --comment "$_COMMENT" \
        --challenge "evidence=VALID suggestion=VALID resolution=codex-direct" \
        --files "${_FILES[@]}"  # timeout: 10000
else
    printf '%s\n' "${_FILES[@]}" >> "$IMPL_DIR/c1-deferred-files.txt"  # not git-added yet — see note above; array form (not unquoted $_FILES) keeps a path containing a space on one line
fi
```

When `CODEX_AVAILABLE=false` OR `ITEM_EFFORT!=medium`: skip Codex routing; use Phase 1+2 directly.

> **Agent budget** — grouping already bounds parallelism here (one agent per domain group, roster-bounded; `comment-dispatch` batches at `BATCH_SIZE`), so no separate spawn cap applies. What still does: each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call. **Work under ~73 calls total is cheaper inline — spawn nothing**, the common case for a 1–3 item PR. Merge a single-item group into the nearest domain rather than giving it its own agent. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk — so every spawn prompt must require an envelope even on exhaustion (`partial: true` plus the items finished).

### Phase 1: Challenge — parallel by domain (skip when `--no-challenge`)

Read the flag from its sentinel, not from the raw argument blob — SKILL.md Step 1 strips every flag token before mode parsing, so `$ARGUMENTS` no longer carries it here:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}" ] && IFS= read -r NO_CHALLENGE < "${TMPDIR:-/tmp}/resolve-no-challenge-${CSID}" || NO_CHALLENGE="false"
echo "NO_CHALLENGE=$NO_CHALLENGE"  # timeout: 3000
```

`true` → skip this phase entirely; `SURVIVING_ITEMS` = all `SELECTED_ITEMS`, every item treated `VALID`, Challenge Log section omitted from the report. Otherwise route by domain to foreground challenge agent:

| Item domain | Challenger |
| -- | -- |
| Architecture, API design, coupling | `foundry:challenger` |
| Code logic, correctness, edge cases | `foundry:sw-engineer` |
| Test coverage, assertions, regressions | `foundry:qa-specialist` |
| Default / unclassified | `foundry:challenger` |

Set `DOMAIN_CHALLENGER` from routing table: architecture/API/coupling/default → `foundry:challenger`; code logic/correctness/edge-cases → `foundry:sw-engineer`; test coverage/assertions/regressions → `foundry:qa-specialist`. Use agent-resolution.md fallback if foundry absent.

Group items by `DOMAIN_CHALLENGER`, preserving each item's original priority-order position within its group (stable partition — needed later so Phase 3's merge plan also respects each specialist's internal commit order). One combined challenge call per domain group, covering ALL that group's items. Derive `<domain>` per group as a short kebab-case slug from the group's shared theme (e.g. `logic`, `tests`, `docs-api`) — the delta between groups, reused as `name="challenge-<domain>"`, as the prompt lead, and as the output filename suffix. `description` = 3–5 words naming that group's theme, never echoing `name` or the shared PR. Compose every group's labels in one pass and confirm the prompt leads differ in their first word — FleetView prints `name` plus the leading chars of prompt line 1, so a shared prefix there yields indistinguishable rows (task-lifecycle.md §Spawn slots):

```text
Agent(subagent_type="${DOMAIN_CHALLENGER}", prompt="<domain>: two-part challenge for these review items.
Part 1 — for each, does the stated problem exist in the code as described?
The reviewer's assertion is itself an unproven claim, not evidence — 'reads like X' != 'is X'.
When a finding asserts a fact reading the referenced file alone can't settle (a name/identifier/version/count is wrong, non-standard, or inconsistent — license names, API/symbol names, version numbers, spec IDs), verify it via WebFetch/WebSearch against the actual authoritative source for that claim (the specific project/library/spec it names — not a generic registry) before ruling VALID. Source unreachable or inconclusive → REJECT with evidence_rationale stating what couldn't be verified; never default VALID on the reviewer's word alone.
Part 2 — if problem exists, is the suggested fix the right approach?
Read each referenced file at <file:line>. Max 4 tool calls per item (the 4th reserved for one WebFetch/WebSearch when a claim needs external verification).
Items:
<id>: <full_comment_text> (<file>:<line>)
...
Write full analysis to $IMPL_DIR/challenge-domain-<domain>.md using the Write tool.
Return ONLY compact JSON as your FINAL message (nothing after it):
{\"items\":[{\"id\":N,\"evidence\":\"VALID\"|\"REJECT\",\"evidence_rationale\":\"<one sentence>\",\"suggestion\":\"VALID\"|\"REJECT\",\"suggestion_rationale\":\"<one sentence>\",\"alternative\":\"<brief alternative or null>\"}]}")
```

**Fire every domain group's `Agent()` call in the same response turn** — read-only (no working-tree writes), safe to run concurrently regardless of file overlap between domains.

**Structural prep — fire in this same turn, concurrently with the challenge agents** (the codemap queries below are read-only and depend only on item *files*, known from Step 3b — not on any challenge verdict — so they run under the challenge agents' latency shadow, adding ~0 wall-clock; grouping in Phase 2 then finds its maps already warm). Keyed off all `SELECTED_ITEMS` (not yet-unknown `SURVIVING_ITEMS`) — a few queries for items challenge later drops are cheap and hidden under the agent latency; Phase 2 filters to survivors. Resolve each file to its canonical module name + build the whole-repo centrality map (`resolve_centrality.py`), then capture each module's **forward imports** (`deps`, fan-*out*, naturally small — never the 20-cap that truncates reverse `rdeps`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -f "$IMPL_DIR/selected-items.txt" ] && IFS= read -r SELECTED_ITEMS < "$IMPL_DIR/selected-items.txt" || SELECTED_ITEMS=""
CODEMAP_MAPS="$IMPL_DIR/codemap-maps.json"
DEPS_MAP="$IMPL_DIR/codemap-deps.jsonl"; : > "$DEPS_MAP"
if command -v codemap-py >/dev/null 2>&1 && [ -f "$IMPL_DIR/action-items.jsonl" ]; then
    _FILES=$(for _id in $(printf '%s\n' "$SELECTED_ITEMS"); do  # cmd-substitution splits in both shells — bare `$VAR` is a silent 1-iteration no-op under zsh
        jq -r "select(.id == $_id) | .file // empty" "$IMPL_DIR/action-items.jsonl"
    done | paste -sd, -)  # timeout: 5000
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
    _IDX_FILE="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}/$(basename "$_ROOT").json"
    _MAP_STAMP=$(python -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); s=p.stat(); print(f"{p.resolve().as_posix()}:{s.st_size}:{s.st_mtime_ns}")' "$_IDX_FILE" 2>/dev/null)
    _SAVED_STAMP=""
    [ -f "$IMPL_DIR/codemap-maps.stamp" ] && IFS= read -r _SAVED_STAMP < "$IMPL_DIR/codemap-maps.stamp"
    if [ -z "$_MAP_STAMP" ] || [ "$_MAP_STAMP" != "$_SAVED_STAMP" ] || ! python -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(not set(filter(None,sys.argv[2].split(","))).issubset(d["file_module"]))' "$CODEMAP_MAPS" "$_FILES" 2>/dev/null; then
        codemap-py query central --top 100000 2>/dev/null \
            | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_centrality.py" --files "$_FILES" > "$CODEMAP_MAPS" 2>/dev/null \
            || : > "$CODEMAP_MAPS"
        printf '%s\n' "$_MAP_STAMP" > "$IMPL_DIR/codemap-maps.stamp"
    fi
    if [ -s "$CODEMAP_MAPS" ]; then
        for _m in $(python -c 'import json,sys; print(" ".join(sorted({v for v in json.load(open(sys.argv[1]))["file_module"].values() if v})))' "$CODEMAP_MAPS"); do
            codemap-py query deps "$_m" 2>/dev/null \
                | python -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({d["module"]: d.get("direct_imports", [])}))' >> "$DEPS_MAP"  # timeout: 5000
        done
    fi
fi
```

Parse each group's per-item verdict array — same granularity as a single-item challenge, never relaxed by grouping:

- Missing item id, or a present element with empty/null `evidence_rationale` or `suggestion_rationale` → treat as UNCERTAIN, same as the C1 missing-element rule above; never append that item to `CHALLENGE_LOG` on this pass. Re-dispatch it alone (single-item challenge call, same domain) once; still empty on retry → append with `evidence_why="challenge agent returned no rationale after retry"` rather than an empty field — SKILL.md's render must never receive an empty `suggestion_why`/`evidence_why` to fabricate filler for.
- `evidence=REJECT` → print `⊘ #<id> evidence rejected: <evidence_rationale>`; set type `[challenged:reject]`; append to `CHALLENGE_LOG`: `id=<id> resolution=rejected evidence=REJECT suggestion=— finding=<full_comment_text, truncate ~80 chars> evidence_why=<evidence_rationale> suggestion_why=— detail=<evidence_rationale>`; drop from `SURVIVING_ITEMS`; close its task — rejected item never reaches Phase 2/3, the only other place a `TaskUpdate` runs. The append block below prints the task id to dispose; call `TaskUpdate(status="deleted")` on it.
- `evidence=VALID` + `suggestion=VALID` → `SUGGESTION_VERDICT[id]=VALID`; use original suggestion for implementation
- `evidence=VALID` + `suggestion=REJECT` → `SUGGESTION_VERDICT[id]=REJECT`; self-resolve using `alternative` as guidance

Every "append to `CHALLENGE_LOG`" in this file runs this block once per record — `$CHALLENGE_LOG` from the prelude is gone in later Bash calls, so the path is re-derived; `$_REC` is a shell variable, so apostrophes or quotes inside the reviewer's comment text cannot break it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
_REC="<record: id=… resolution=… evidence=… suggestion=… finding=… evidence_why=… suggestion_why=… detail=…>"
case "$_REC" in "<record:"*|"") echo "! BLOCKED — _REC still holds the placeholder; substitute the verdict record before running"; exit 1 ;; esac
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; Step 3b/prelude never ran"; exit 1; }
_REC=$(printf '%s' "$_REC" | tr '\n\t' '  ')  # translate, not delete — a genuine tab-delimited record would fuse into an unparsable line if its separators vanished instead of becoming spaces; multi-line finding text still collapses to one physical line either way
printf '%s\n' "$_REC" >> "$IMPL_DIR/challenge-log.txt"  # timeout: 3000
# resolution= is the field right after id=, before any free-text field (finding=/evidence_why=/suggestion_why=/
# detail=) — so a reviewer's quoted text can never precede it and be mistaken for it; anchoring at line start
# makes the match unambiguous with no risk of matching a substring embedded in free text later in the line.
# -i (not tr-based normalization) absorbs casing drift; [[:space:]]*=[[:space:]]* absorbs spacing drift —
# on id= too, not just resolution=: a model that spaces one key=value pair tends to space all of them.
if printf '%s' "$_REC" | grep -qiE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected([[:space:]]|$)'; then
    printf '%s' "$_REC" | grep -qiE 'evidence[[:space:]]*=[[:space:]]*reject' || { echo "! BLOCKED — resolution=rejected without evidence=REJECT in record; malformed _REC, cannot dispose task safely: $_REC"; exit 1; }
    _ID=$(printf '%s\n' "$_REC" | sed -n 's/^id[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p')
    if [ -f "$IMPL_DIR/item-tasks.tsv" ]; then
        _TID=$(awk -F'\t' -v id="$_ID" '$1==id{print $2}' "$IMPL_DIR/item-tasks.tsv")
        [ -n "$_TID" ] || { echo "! BLOCKED — item $_ID has no task id in item-tasks.tsv; Step 3e never ran for it, or file is stale"; exit 1; }
        echo "TaskUpdate target (deleted): item=$_ID task=$_TID"  # timeout: 3000
    else
        echo "→ item $_ID rejected (no item-tasks.tsv — report mode never runs Step 3e, no per-item task to dispose)"  # timeout: 3000
    fi
fi
```

`item-tasks.tsv` legitimately does not exist in `report` mode (Step 3e is `pr`/`pr+report` only) — a missing file here is normal, not malformed input, so it must never abort the loop: every rejected item in a multi-item report-mode run has to be recorded, not just the first.

Append every surviving item's verdict (one line per item — the file is the log, no in-context copy): `id=<id> resolution=<as-suggested|self-resolved> evidence=VALID suggestion=<VALID|REJECT> finding=<full_comment_text, truncate ~80 chars> evidence_why=<evidence_rationale> suggestion_why=<suggestion_rationale> detail=<when suggestion=REJECT: the `alternative`text — what gets implemented instead; when suggestion=VALID: leave as`pending-impl:<id>`, Step 11 backfills it from the item's actual commit summary once Phase 2 lands, so the report never prints a bare label with no stated content>`. Items with `evidence=VALID` form `SURVIVING_ITEMS`.

### Phase 2: Implementation — parallel, one worktree per specialist

The codemap maps (`$IMPL_DIR/codemap-maps.json` — `file_module` + `centrality`; `$IMPL_DIR/codemap-deps.jsonl` — per-module `direct_imports`) were built in Phase 1's Structural prep, concurrently with the challenge agents, so both tiebreaks below read them with no fresh query. They cover all `SELECTED_ITEMS`; filter to survivors as needed.

Group `SURVIVING_ITEMS` by `IMPL_AGENT` (routing table at top of this file; `--agent` override applies to every group uniformly). Preserve original priority-order position within each group (stable partition, same reason as Phase 1).

**File-ownership tiebreak** (kills Phase 3 cherry-pick conflicts at the root, instead of only resolving them after the fact): before capping group size, check whether any `.file` is claimed by items in more than one group. Rank specialists least → most foundational/invasive — a change from a higher-ranked specialist is more likely to reshape the file, so lower-ranked items should defer to it rather than risk a conflicting concurrent edit:

`foundry:linting-expert < foundry:doc-scribe < foundry:qa-specialist < foundry:perf-optimizer < foundry:sw-engineer < foundry:solution-architect`

(`foundry:challenger` never appears here — Phase 1 only, read-only, holds no file ownership.) For each contested file, reassign **every** item touching it to the single highest-ranked group in the contest — the item's original `IMPL_AGENT` routing is overridden by ownership, not by its own `change` value. Print `→ #<id> reassigned <from> → <to> (file overlap: <path>)` per reassignment so it's auditable.

**Import-coupling merge** (soft — catches the *semantic* conflict the file-path tiebreak is blind to): file overlap only co-locates items editing the **same** file. Two items in **different** files still collide when one imports the other — item A renames a symbol in `pkg.auth`, item B edits `pkg.middleware` which imports it; both land, cherry-pick textually clean, code broken. Structural prep already captured the links: items A and B are **import-coupled** when one's module is in the other's `direct_imports` — B's module ∈ A's imports (or vice versa), reading `$IMPL_DIR/codemap-deps.jsonl` keyed by the module names in `codemap-maps.json`'s `file_module`. This uses forward `deps` (fan-out, bounded) rather than reverse `rdeps`, so recall is **not** truncated by the 20-caller display cap. After the file-overlap pass, for each import-coupled pair still split across two groups, reassign the lower-ranked item's group to the higher-ranked one (same specialist ranking above) so both land in one worktree and the specialist keeps them consistent. Print `→ #<id> reassigned <from> → <to> (import coupling: <mod> ↔ <mod>)`. This merge is **soft**, unlike file overlap: it yields to the 5-item cap below — if honoring it would push a group past 5, leave the pair split and rely on Phase 3's conflict fallback plus the blast-radius context already handed to each agent. Empty `codemap-deps.jsonl` (no codemap-py query / query failure) → no-op; file-overlap grouping stands.

Re-derive group membership after all reassignments (file overlap + import coupling), **then** cap 5 items/group — same context ceiling the old file-affinity batching used; a specialist with more than 5 items splits into `ceil(N/5)` groups, **keeping every file's items together in the same sub-group** (never split one file's items across two sub-groups — would reintroduce the exact conflict this tiebreak exists to prevent). Each resulting sub-group is one worktree with its own `group` tag (reused in Phase 3's merge plan).

Snapshot the worktree list before dispatch — Phase 3's cleanup accounts for worktrees via each group's own envelope, so a group that stalls and never returns (§Health monitoring below) never gets its path into `specialist-worktrees.txt`; this snapshot is what lets the cleanup fence tell "a worktree nothing ever reported" apart from "a worktree that was never created":

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
git worktree list --porcelain | sed -n 's/^worktree //p' > "$IMPL_DIR/worktrees-before.txt"  # timeout: 5000
```

Per group, mark its items' tasks in_progress, then dispatch with worktree isolation so concurrent specialists never race on a shared working tree (no stash dance needed — dirty state in one worktree can't collide with another):

```text
Agent(subagent_type="<specialist>", isolation="worktree", prompt="Effort level: <highest ITEM_EFFORT in group>.
Implement these action items one at a time. For each, apply the fix using best judgment
(if suggestion was rejected in challenge, fix the underlying issue instead — see rationale/alternative below),
then commit it individually before moving to the next item:
python \"${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_action_item.py\" --build --summary \"<short summary>\" \\
    --item-id \"<id>\" --author \"<author>\" --pr \"<PR_REF>\" --comment \"<full_comment_text>\" \\
    --challenge \"evidence=VALID suggestion=<VALID|REJECT> resolution=<as-suggested|self-resolved>\" \\
    --files <files-changed-by-this-item>
Items:
<id>: <IMPL_PROMPT for this item> — blast-radius callers: <ITEM_CALLERS for this item, if any>
...
Write findings (approach taken, files changed per item) to $IMPL_DIR/impl-worktree-<group_tag>.md using the Write tool.
Return ONLY compact JSON as your FINAL message (nothing after it):
{\"worktree\":\"<absolute path of YOUR OWN worktree, from: git rev-parse --show-toplevel>\",\"commits\":[{\"item_id\":N,\"sha\":\"<sha>\"}],\"skipped\":[{\"item_id\":N,\"reason\":\"<why no commit>\"}]}")
```

**Fire all specialist groups in the same response turn** — this is the actual wall-clock win: N specialists implementing and committing concurrently, each isolated in its own worktree/branch.

> **Health monitoring**: parallel foreground dispatch — same rule as any multi-agent fan-out (CLAUDE.md §6). No response from a group within ~15 min → surface partial results from the groups that did return; mark the stalled group ⏱, proceed to merge-back with whatever landed; its unresolved items stay `in_progress` and get reported alongside other pending work.

Parse each group's JSON: `commits` entries feed Phase 3's merge plan — appended to `$IMPL_DIR/phase2-commits.jsonl`, tagged with this group's own worktree tag (the JSON envelope carries no `group` field — it is added here, not by the specialist); `skipped` entries record `skipped — <reason>` (no empty commit, no cherry-pick attempt) and are appended to `$IMPL_DIR/skipped-items.txt`; `worktree` (the specialist's own absolute worktree path, so the orchestrator's cleanup fence at Phase 3's close doesn't need a shell array to survive the intervening Bash calls — nothing does) is appended to `$IMPL_DIR/specialist-worktrees.txt`. All three are durable records so Phase 3 survives a compaction between here and there. Every group's parsing happens in this same orchestrator turn (Phase 2 fans out agents, but the orchestrator reads their envelopes one at a time), so appends are sequential — no concurrent-write risk even with multiple groups returning at once. Run each block once per group, right after that group's envelope is read (`$IMPL_DIR` pre-initialized empty in the prelude, so a run with nothing to commit/skip still leaves readable, empty files for Phase 3) — including a group whose every item was skipped: its worktree still exists and still needs removing, so this block runs regardless of whether `commits` is empty:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
_GROUP_TAG="<this group's worktree tag>"; _COMMITS_JSON='<this group envelope'\''s "commits" array verbatim, e.g. [{"item_id":1,"sha":"abc1234"}]>'; _WORKTREE_PATH="<this group envelope's "worktree" field verbatim — the specialist's own absolute path>"
for _v in "$_GROUP_TAG" "$_COMMITS_JSON" "$_WORKTREE_PATH"; do
    case "$_v" in '<'*'>') echo "! BLOCKED — group tag/commits/worktree placeholder not substituted"; exit 1 ;; esac  # checked per-field, start-to-end anchored — a real worktree path or JSON blob containing a stray < or > (unlikely, but seen in the wild) must not false-positive the way a substring test across the concatenated fields would
done
python -c 'import json,sys
g, raw = sys.argv[1], sys.argv[2]
for c in json.loads(raw):
    c["group"] = g
    print(json.dumps(c))' "$_GROUP_TAG" "$_COMMITS_JSON" >> "$IMPL_DIR/phase2-commits.jsonl"  # timeout: 5000 — never gate this append on the worktree field below: the commits ledger must land regardless, or a missing worktree path (specialist envelope bug, not a merge-correctness issue) would silently drop this group's items from Phase 3's plan
if [ -n "$_WORKTREE_PATH" ]; then
    printf '%s\n' "$_WORKTREE_PATH" >> "$IMPL_DIR/specialist-worktrees.txt"  # timeout: 3000
else
    # an empty (not placeholder) value carries no angle bracket, so the guard above alone lets it
    # through — warn rather than silently leak, but never block: this group's commits already
    # landed above and must not be lost over a missing cleanup-only field
    echo "⚠ group $_GROUP_TAG: envelope omitted the worktree field — its worktree will not be auto-removed; reclaim manually via 'git worktree list' or heal_git_artifacts.py worktrees after this run"
fi
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
_ITEM_ID="<item_id>"; _REASON="<reason, single line — strip embedded newlines/tabs>"
case "$_ITEM_ID$_REASON" in *'<'*'>'*) echo "! BLOCKED — item_id/reason placeholder not substituted"; exit 1 ;; esac
printf '%s\t%s\n' "$_ITEM_ID" "$_REASON" >> "$IMPL_DIR/skipped-items.txt"  # timeout: 3000
```

### Phase 3: Merge-back — sequential, orchestrator-owned

**HEAD fingerprint check** — the worktrees branched from `resolve-base-sha`; verify the PR branch hasn't moved under us while Phase 2 ran. A moved base means an external write (human push, or a run that slipped the mutex) landed during Phase 2 — cherry-picks still apply (they replay each diff onto the current tip), but overlapping edits now surface as conflicts, so surface the drift rather than stack silently.

Precompute every specialist's original pre-cherry-pick patch-id first, in its own fenced block: the multi-line `python -c` call below must stay isolated from the check that consumes it, or the blueprint-manifest generator bails on per-command extraction for the whole surrounding block (`plugins/CLAUDE.md` §Blueprint Blocks) — the check block still needs the guard reads it shares with every other fence, and mixing them here has already cost this fence its auto-allow coverage once. `$IMPL_DIR/phase2-plan-shas.txt`/`phase2-plan-ids.txt` are fixed paths under `IMPL_DIR`, overwritten every run — no `mktemp`/`rm -f` needed, so nothing here trips the manifest generator's destructive-command filter either:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot verify stranded picks before the HEAD-fingerprint check below"; exit 1; }
: > "$IMPL_DIR/phase2-plan-shas.txt"
if [ -s "$IMPL_DIR/phase2-commits.jsonl" ]; then
    python -c 'import json,sys
for line in open(sys.argv[1]):
    line = line.strip()
    if line:
        sha = json.loads(line)["sha"]
        if not sha.strip():
            raise SystemExit(f"empty sha in ledger row: {line!r}")
        print(sha.strip())' "$IMPL_DIR/phase2-commits.jsonl" > "$IMPL_DIR/phase2-plan-shas.txt" \
        || { echo "! BLOCKED — phase2-commits.jsonl unparsable or holds an empty sha; cannot verify stranded picks safely"; exit 1; }  # timeout: 5000 — a truncated or empty-sha read here must not silently pass the stranded-pick check below on a partial list
fi
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" ] && IFS= read -r _BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" || _BASE_SHA=""  # timeout: 3000
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
_NOW_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")  # timeout: 3000
if [ -n "$IMPL_DIR" ] && [ -f "$IMPL_DIR/merge-result.json" ]; then
    : # a merge pass already ran this Phase 3 entry (this is a resumed call after a conflict) — HEAD
      # now legitimately carries our own cherry-picked commits, so comparing it to the pre-Phase-3
      # sentinel would misread as "external write" on every resumed entry; never re-point here
elif [ -n "$_BASE_SHA" ] && [ "$_NOW_SHA" != "$_BASE_SHA" ]; then
    # a crash/interrupt mid-plan (uncaught TimeoutExpired, a killed loop) can strand our own
    # already-landed picks with no merge-result.json (the parse-validation check above runs `rm -f`
    # before this ever sees the file) — so HEAD differs from base for the same reason an external
    # write would. Re-pointing here would permanently orphan those picks below the new base,
    # unreachable by any future combined reset. Cherry-pick assigns each pick a new sha but keeps its
    # diff, so match by patch-id, not message — matching by sha never fires (plan sha ≠ landed sha,
    # always), and a message-text match (the earlier `^\[resolve No\.` grep) misdiagnoses a
    # hand-written or copied commit body that happens to start a line with our own attribution text.
    # phase2-commits.jsonl — not merge-plan.json, which the plan-build block below always deletes and
    # rebuilds — is the durable record of every specialist's original pre-cherry-pick sha; the block
    # above this one already turned it into phase2-plan-shas.txt.
    [ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot verify stranded picks"; exit 1; }
    _PLAN_IDS_FILE="$IMPL_DIR/phase2-plan-ids.txt"
    : > "$_PLAN_IDS_FILE"
    if [ -s "$IMPL_DIR/phase2-plan-shas.txt" ]; then
        while IFS= read -r _s; do
            [ -n "$_s" ] || continue
            git show "$_s" 2>/dev/null | git patch-id --stable 2>/dev/null | cut -d' ' -f1
        done < "$IMPL_DIR/phase2-plan-shas.txt" >> "$_PLAN_IDS_FILE"  # timeout: 15000
    fi
    _STRANDED=""
    if [ -s "$_PLAN_IDS_FILE" ]; then
        while IFS= read -r _c; do
            [ -n "$_c" ] || continue
            _CID=$(git show "$_c" 2>/dev/null | git patch-id --stable 2>/dev/null | cut -d' ' -f1)
            [ -n "$_CID" ] || continue
            grep -qx "$_CID" "$_PLAN_IDS_FILE" && _STRANDED="$_STRANDED $_c"
        done < <(git rev-list "${_BASE_SHA}..${_NOW_SHA}" 2>/dev/null)  # timeout: 15000
    fi
    if [ -n "$_STRANDED" ]; then
        echo "! BLOCKED — HEAD carries our own stranded pick(s) above base (${_BASE_SHA:0:8} → ${_NOW_SHA:0:8}):$_STRANDED — a prior Phase 3 pass likely crashed mid-plan; reset to base, then re-run this fence"
        exit 1
    fi
    echo "⚠ base HEAD moved during Phase 2: ${_BASE_SHA:0:8} → ${_NOW_SHA:0:8} (external write)."
    echo "  Cherry-picks apply onto the new base; any overlapping edit surfaces as a conflict → routed to Step 5a below."
    # re-point the persisted base at the new tip — the merge fence's combined reset targets this sentinel
    # absolutely (git reset --soft --end-of-options <sha>, not HEAD~n); leaving it at the stale pre-drift
    # value would rewind the branch PAST the external commit just detected, making it unreachable from HEAD
    echo "$_NOW_SHA" > "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}"  # timeout: 3000
fi
```

Build the cherry-pick plan in **original `SELECTED_ITEMS` priority order**, interleaved across specialist groups by item id — NOT grouped by specialist, so the base order matches severity ranking regardless of which group finished first. This global sort is safe because Phase 1/2 grouping preserved each specialist's internal relative order (stable partition) — sorting by original priority never reorders two items from the same specialist relative to each other. Each entry also carries its worktree `group` tag (from Phase 2) and its `module` — the **canonical codemap name** for the item's `.file`, read from `file_module` in `$IMPL_DIR/codemap-maps.json` (built in Structural prep), blank when unresolved. Never hand-derive it with a sed transform: codemap names a package `__init__.py` after the package (`pkg`, not `pkg.__init__`), so a sed guess silently mismatches the centrality keys and scores 0.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
[ -f "$IMPL_DIR/selected-items.txt" ] && IFS= read -r SELECTED_ITEMS < "$IMPL_DIR/selected-items.txt" || SELECTED_ITEMS=""
rm -f "$IMPL_DIR/merge-plan.json"  # a stale plan from an earlier attempt must never be inherited by the merge fence below — cleared before the call, not just left to a failed overwrite
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_merge_plan.py" \
    --commits "$IMPL_DIR/phase2-commits.jsonl" --action-items "$IMPL_DIR/action-items.jsonl" \
    --priority-order "$SELECTED_ITEMS" --out "$IMPL_DIR/merge-plan.json" \
    --codemap-maps "$IMPL_DIR/codemap-maps.json" \
    || { echo "! BLOCKED — merge plan not built; Phase 3 cannot run"; exit 1; }  # timeout: 10000 — script degrades an absent/empty/malformed --codemap-maps to an empty module map on its own; a non-zero exit here is a real failure (malformed ledger row, unwritable IMPL_DIR), not a degraded-but-usable case
```

`$IMPL_DIR/merge-plan.json` is `PLAN_FILE` in the merge fence below — a fixed filename under `IMPL_DIR`, never carried across fences as a shell variable (bash state does not persist between Bash calls). An id present in `SELECTED_ITEMS` but absent from the plan (rejected in Phase 1, or skipped by its Phase 2 agent) is correctly omitted — its terminal state is tracked in `challenge-log.txt`/`skipped-items.txt`, not here.

`! BLOCKED` here with `duplicate item_id in commits ledger` means two Phase 2 groups both appended a row for the same item — normally impossible (each item routes to exactly one group), so first check whether a retry re-ran a group's envelope-parsing fence a second time. `awk '!seen[$0]++' "$IMPL_DIR/phase2-commits.jsonl" > "$IMPL_DIR/phase2-commits.jsonl.dedup" && mv "$IMPL_DIR/phase2-commits.jsonl.dedup" "$IMPL_DIR/phase2-commits.jsonl"` fixes an **identical** re-appended row (byte-for-byte duplicate line — the exact shape a re-run produces). Two rows for the same item with **different** shas is not that case — a real routing bug — and needs manual inspection, not this command.

**Centrality ordering** (lands the most foundational change first, so contract-defining commits precede their dependents): the `{module: rdep_count}` centrality map was already built once in Structural prep (`$IMPL_DIR/codemap-maps.json`, from a single authoritative `codemap-py query central` pass — not the 20-capped `BLAST_RADIUS_CONTEXT`, which saturates). Extract it to a file so the merge step can reorder **whole worktree groups** most-central-first. Safe precisely because the file-ownership tiebreak guarantees distinct groups touch disjoint files — reordering whole chains can't add a textual conflict, and commit order **within** a chain is never touched (chains may build on themselves). Missing maps (no `codemap-py query` / query failure) → flag omitted, plan applies in priority order unchanged.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -f "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" ] && IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" || COMMIT_MODE="unset"
case "$COMMIT_MODE" in each|grouped|all|stage) echo "merge fence: COMMIT_MODE=$COMMIT_MODE" ;; *) echo "! BLOCKED — COMMIT_MODE is '$COMMIT_MODE': Step 3d's commit-mode block never ran; run the block matching the user's answer, then re-run this fence"; exit 1 ;; esac  # fail closed — a silent default landed 12 per-item commits once
PLAN_FILE="$IMPL_DIR/merge-plan.json"
[ -f "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" ] && IFS= read -r _BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" || _BASE_SHA=""
if [ ! -f "$IMPL_DIR/merge-result.json" ] && [ -n "$_BASE_SHA" ]; then
    # first call this Phase 3 entry — the HEAD-fingerprint check above ran in a separate, earlier
    # Bash call; HEAD could have moved again in the window since (a push landing mid-turn). Catch
    # it here rather than silently cherry-picking onto content the fingerprint check never saw.
    _HEAD_PRE=$(git rev-parse HEAD 2>/dev/null || echo "")
    [ "$_HEAD_PRE" = "$_BASE_SHA" ] || { echo "! BLOCKED — HEAD moved (${_BASE_SHA:0:8} → ${_HEAD_PRE:0:8}) after the fingerprint check but before this merge call; re-run the HEAD fingerprint check block above, then this fence"; exit 1; }
elif [ -f "$IMPL_DIR/merge-result.json" ] && [ -f "$IMPL_DIR/head-at-conflict" ]; then
    # resumed call after a conflict — the `if` above is a no-op here by design (merge-result.json
    # already exists, so the external-write re-point above never fires either), so this is Phase 3's
    # only remaining checkpoint before the destructive `--soft` collapse below. Conflict resolution is
    # the longest human-time window in Phase 3 — the likeliest moment for a teammate commit to land
    # and get silently rewound onto stale history by that collapse.
    # no `|| _HEAD_AT_CONFLICT=""` here — the elif above already proved the file exists, so `read`'s
    # only non-zero exit is newline-less EOF, which has still assigned the line; a fallback would
    # discard that correctly-read sha instead of only covering the missing-file case it exists for
    _HEAD_AT_CONFLICT=""
    IFS= read -r _HEAD_AT_CONFLICT < "$IMPL_DIR/head-at-conflict"
    [ -n "$_HEAD_AT_CONFLICT" ] || { echo "! BLOCKED — head-at-conflict sentinel unreadable"; exit 1; }
    _AHEAD=$(git rev-list --count "${_HEAD_AT_CONFLICT}..HEAD" 2>/dev/null) || _AHEAD=""
    case "$_AHEAD" in
        ''|*[!0-9]*) echo "! BLOCKED — could not count commits since conflict (rev-list failed against $_HEAD_AT_CONFLICT)"; exit 1 ;;
    esac
    [ "$_AHEAD" -le 1 ] || { echo "! BLOCKED — HEAD carries $_AHEAD commit(s) since the conflict (expected ≤1, the --continue commit) — a foreign commit likely landed during conflict resolution: $(git log --oneline "${_HEAD_AT_CONFLICT}..HEAD" | tr '\n' ' ')"; exit 1; }
elif [ -f "$IMPL_DIR/merge-result.json" ]; then
    # merge-result.json present, head-at-conflict absent — the only way to reach that combination is
    # a rc=0 clean pass having already run this Phase 3 entry (rc=1 always writes head-at-conflict
    # below; the parse-validation check above removes merge-result.json on every other rc). No prose
    # in this file instructs re-invoking the merge fence after it reported clean — falling through
    # silently here let a teammate commit landing after that point survive only by accident.
    echo "! BLOCKED — Phase 3 already reported clean this entry — nothing left to merge; re-entry not expected here"
    exit 1
fi
# C1's unstaged-edit exemption (see the C1 section's note) holds only when the C1 item's file is
# disjoint from every Phase 2 item's file — C1 is invisible to Phase 2's file-ownership tiebreak
# (it skips Phase 1/2 entirely), so nothing else in the file checks this. An overlap makes the
# cherry-pick below refuse identically to the pre-fix dirty-index case: phantom conflict, empty file
# list, no CHERRY_PICK_HEAD, no recovery route — so it must be caught before the cherry-pick runs.
if [ -s "$IMPL_DIR/c1-deferred-files.txt" ] && [ -s "$PLAN_FILE" ]; then
    _SHAS_FILE=$(mktemp)  # timeout: 3000
    python -c 'import json,sys
for e in json.load(open(sys.argv[1])):
    print(e["sha"])' "$PLAN_FILE" > "$_SHAS_FILE"  # timeout: 5000 — no 2>/dev/null: a read/parse failure here must surface, not silently read as "no shas, no overlap"
    [ $? -eq 0 ] || { echo "! BLOCKED — overlap guard: failed to read plan shas from $PLAN_FILE"; rm -f "$_SHAS_FILE"; exit 1; }
    # -z (NUL-delimited) is required, not cosmetic: a bare `for _f in $(...)` word-splits on
    # IFS, so a path containing a space is silently missed on one side and falsely flagged as an
    # unrelated overlap on the other (same class as the array fix in the C1 section above);
    # -z also disables git's default quote/octal-escape of non-ASCII paths, which would otherwise
    # never string-equal the raw UTF-8 form c1-deferred-files.txt holds.
    _DIFF_FILES=$(mktemp)  # timeout: 3000
    while IFS= read -r _sha; do
        [ -n "$_sha" ] || continue
        # No pipe into tr: `${PIPESTATUS[0]}` is bash-only (zsh leaves it unset, and zsh's `[`
        # coerces the empty result to 0, so the old pipe form silently passed on every host that
        # runs this fence under zsh). Redirect to a temp file and check git's own $? directly —
        # a resolvable sha with no diff-tree output is legitimate (empty commit), an unresolvable
        # sha is a hard failure that must not silently read as "this sha touches nothing".
        _RAW_DIFF=$(mktemp)  # timeout: 3000
        git diff-tree --no-commit-id --name-only -r -z --end-of-options "$_sha" > "$_RAW_DIFF"  # timeout: 5000
        [ $? -eq 0 ] || { echo "! BLOCKED — overlap guard: git diff-tree failed to resolve sha $_sha"; rm -f "$_SHAS_FILE" "$_DIFF_FILES" "$_RAW_DIFF"; exit 1; }
        tr '\0' '\n' < "$_RAW_DIFF" >> "$_DIFF_FILES"
        rm -f "$_RAW_DIFF"
    done < "$_SHAS_FILE"
    rm -f "$_SHAS_FILE"
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
    # normalize both sides (strip a leading ./, resolve an absolute path relative to repo root) before
    # comparing — a leading-./ or absolute form on either side is otherwise a real overlap that misses
    _OVERLAP=$(python -c 'import os,sys
root, diff_file, deferred_file = sys.argv[1], sys.argv[2], sys.argv[3]
def norm(p):
    p = p.strip()
    if not p:
        return ""
    if os.path.isabs(p) and root:
        p = os.path.relpath(p, root)
    return os.path.normpath(p)
diff_set = {norm(l) for l in open(diff_file, encoding="utf-8") if l.strip()}
overlap = [norm(l) for l in open(deferred_file, encoding="utf-8") if l.strip() and norm(l) in diff_set]
print(" ".join(overlap))' "$_ROOT" "$_DIFF_FILES" "$IMPL_DIR/c1-deferred-files.txt")
    _OVERLAP_RC=$?  # timeout: 5000 — a bad-encoding line or other read failure inside this comparison must abort, not read as an empty (no-overlap) result
    rm -f "$_DIFF_FILES"
    [ "$_OVERLAP_RC" -eq 0 ] || { echo "! BLOCKED — overlap guard: comparison failed (rc=$_OVERLAP_RC) — cannot verify C1/Phase-2 file disjointedness safely"; exit 1; }
    [ -n "$_OVERLAP" ] && { echo "! BLOCKED — C1 deferred file(s) also touched by a Phase 2 item's commit:$_OVERLAP — resolve manually (git add and commit the C1 file separately before re-running this fence, or drop that C1 result and re-route the item through Phase 1/2)"; exit 1; }
fi
CENTRALITY_FILE=""
if [ -s "$IMPL_DIR/codemap-maps.json" ]; then
    _CF_TMP=$(mktemp)  # timeout: 3000
    trap 'rm -f "$_CF_TMP"' EXIT  # cleans up on every exit path of this Bash call — a separate var so the CENTRALITY_FILE="" blanking two lines below never blanks the trap's own removal target
    python -c 'import json, sys; json.dump(json.load(open(sys.argv[1]))["centrality"], open(sys.argv[2], "w"))' \
        "$IMPL_DIR/codemap-maps.json" "$_CF_TMP"  # timeout: 5000
    [ -s "$_CF_TMP" ] && CENTRALITY_FILE="$_CF_TMP"
fi

if [ -n "$_BASE_SHA" ] && [ "$COMMIT_MODE" != "each" ]; then
    # A foreign/stale base_sha would reset onto unrelated history and stage a partial revert of
    # it (git reset --soft accepts any reachable commit, ancestor or not). :53 rewrites the
    # sentinel from the current HEAD at every run entry, so this should always hold — refuse
    # rather than silently reset onto it if it somehow doesn't. Resolve first, separately from the
    # ancestor test: an unresolvable sha failing the ancestor test too would otherwise report the
    # wrong cause ("not an ancestor" for a sha that simply doesn't exist).
    git rev-parse --verify "${_BASE_SHA}^{commit}" >/dev/null 2>&1 || { echo "! BLOCKED — base sha $_BASE_SHA does not resolve"; exit 1; }
    git merge-base --is-ancestor "$_BASE_SHA" HEAD 2>/dev/null || { echo "! BLOCKED — base sha $_BASE_SHA is not an ancestor of HEAD; refusing to reset onto unrelated history"; exit 1; }
fi
# --centrality-file/--base-sha use the =-joined single-word form, not `${VAR:+--flag "$VAR"}`:
# the two-word form relies on the shell field-splitting the expansion result, which bash does
# but zsh does not (no SH_WORD_SPLIT by default) — under zsh the whole `--base-sha abc123` arrives
# as ONE argv word and argparse rejects it (rc=2), so Phase 3 never cherry-picks anything.
_MERGE_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/merge_specialist_batch.py" \
    --plan "$PLAN_FILE" --commit-mode "$COMMIT_MODE" \
    ${CENTRALITY_FILE:+--centrality-file="$CENTRALITY_FILE"} \
    ${_BASE_SHA:+--base-sha="$_BASE_SHA"})  # timeout: 30000 — same base on every pass (first or resumed) so the combined reset always collapses the WHOLE plan, not just this call's entries
_RC=$?
printf '%s\n' "$_MERGE_OUT"  # command substitution buffers, doesn't stream — print the full captured output (git's own cherry-pick lines included) for this call's own turn
# the script's cherry-pick subprocess calls inherit stdout (not capture_output=True), so git's own
# commit-summary/conflict lines share the same stream as the script's final `print(json.dumps(...))`
# — only the LAST line is guaranteed to be that JSON; writing the raw stream to merge-result.json
# would make it unparsable by every downstream `json.load` (Clean-run/Conflict routes, fingerprint gate)
printf '%s\n' "$_MERGE_OUT" | tail -1 > "$IMPL_DIR/merge-result.json"
# an uncaught exception (KeyError/FileNotFoundError/TimeoutExpired — anything but the script's own
# ValueError) prints a traceback to stderr, not stdout, and still exits rc=1 — indistinguishable from
# a legitimate conflict by rc alone. Left unparsed, that empty/garbage file would read as "merge pass
# already ran" to the fingerprint re-point gate above and "no merge pass ran yet" to the HEAD-drift
# assertion below, silently disabling both for the rest of this Phase 3 entry.
python -c 'import json,sys; json.load(open(sys.argv[1]))' "$IMPL_DIR/merge-result.json" \
    || { echo "! BLOCKED — merge fence produced no parsable JSON (rc=$_RC — an uncaught crash prints its traceback to stderr; a rejected flag would show rc=2; see stderr above)"; rm -f "$IMPL_DIR/merge-result.json"; exit 1; }
# rc=1 is a legitimate cherry-pick conflict carrying the JSON the Conflict route below consumes —
# never gate it like the sibling `|| exit 1` calls above, which would discard `remaining` and turn
# a recoverable state into an unrecoverable one. Only an argparse/sha-validation failure (rc=2) or
# an unexpected rc blocks.
case "$_RC" in
    0|1) ;;
    *) echo "! BLOCKED — merge_specialist_batch.py rc=$_RC (argparse/sha validation error — any JSON already printed above by the script itself, not by this fence)"; rm -f "$IMPL_DIR/merge-result.json"; exit 1 ;;  # a failed call never happened, as far as the fingerprint-check's re-point gate and the Conflict route's 'remaining' read are concerned — leaving a stale file here would tell both a pass ran when none did
esac
# a real conflict (rc=1, past the parse-validation check above) is the longest human-time window in
# Phase 3 — the likeliest moment for a teammate commit to land. Snapshot HEAD now so the resumed
# entry's own HEAD-drift assertion above can detect one; the fingerprint-check block and the first-
# call assertion above both intentionally skip this call (merge pass already ran), so without this
# sentinel a resumed collapse would silently rewind any such commit.
[ "$_RC" -eq 1 ] && git rev-parse HEAD > "$IMPL_DIR/head-at-conflict"
# clear on every rc=0 (not just non-each — this fence's own resumed-entry elif pair above checks this
# sentinel for every mode), so a later re-entry this same IMPL_DIR reads "no sentinel, merge-result.json
# present" as clean-already-ran, not as a stale in-progress conflict.
[ "$_RC" -eq 0 ] && rm -f "$IMPL_DIR/head-at-conflict"
if [ "$_RC" -eq 0 ] && [ "$COMMIT_MODE" != "each" ] && [ -n "$_BASE_SHA" ]; then
    # rc=0 means the whole plan applied cleanly, so run_plan's combined reset ran (unconditional on
    # base_sha) — but its own subprocess.run(check=False) discards a failed reset's exit code, so a
    # "clean" report can still leave real, uncollapsed commits standing. Assert HEAD actually landed.
    _HEAD_NOW=$(git rev-parse HEAD)
    _BASE_FULL=$(git rev-parse --verify "${_BASE_SHA}^{commit}" 2>/dev/null) || { echo "! BLOCKED — base sha $_BASE_SHA no longer resolves"; exit 1; }
    [ "$_HEAD_NOW" = "$_BASE_FULL" ] || { echo "! BLOCKED — collapse did not reach base (HEAD=$_HEAD_NOW expected=$_BASE_FULL) — merge_specialist_batch.py's reset may have failed silently"; exit 1; }
fi
```

`PLAN_FILE` = JSON array of `{"item_id", "sha", "group", "module"}` in priority order (assembled from every Phase 2 group's `commits`). With `--centrality-file` the script reorders whole groups most-central-first (`order_plan`) before cherry-picking each in turn:

- **`COMMIT_MODE=each`** — every entry lands and stays as its own real commit (own `[resolve No.<id>]` attribution message, carried over from the worktree commit); no reset.
- **`COMMIT_MODE=grouped` / `all` / `stage`** — every entry lands as a real commit during the cherry-pick loop (an in-progress soft-reset would leave the index non-clean and break the *next* cherry-pick, even for a disjoint file — see the script's module docstring), then, once the whole plan has applied cleanly, one combined `git reset --soft HEAD~<n>` collapses them into a single staged diff — same state the post-loop sections below already expect.

**Clean run** (`conflict: null`) → every item's commit is on the PR branch (or staged, per mode above). Run the **C1 deferred-files staging fence** below now — whether this is the merge fence's first call or a resumed call after a conflict (see **Conflict** below): it is keyed structurally on `$IMPL_DIR/merge-result.json`'s own `conflict: null`, read from disk rather than the orchestrator's prose memory of which call reached it, since a conflicted run that reaches clean state only via a resumed call must still stage these files:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
python -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["conflict"] is None else 1)' "$IMPL_DIR/merge-result.json" \
    || { echo "! BLOCKED — Phase 3 has not reached conflict:null yet; do not run this fence until the merge fence (or its resumed call) reports a clean run"; exit 1; }  # timeout: 5000
# conflict:null alone doesn't prove the combined --soft collapse actually reached base — the merge
# fence's own collapse assertion can BLOCK *after* merge-result.json already holds conflict:null
# (written before that exit), which would otherwise let this gate pass silently and stage C1 files
# on top of real, uncollapsed commits still sitting on the branch. Same check, at this point of use,
# for the modes where a collapse is expected at all.
[ -f "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" ] && IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" || COMMIT_MODE="unset"
[ -f "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" ] && IFS= read -r _BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" || _BASE_SHA=""
if [ "$COMMIT_MODE" != "each" ] && [ -n "$_BASE_SHA" ]; then
    _HEAD_NOW=$(git rev-parse HEAD 2>/dev/null || echo "")
    _BASE_FULL=$(git rev-parse --verify "${_BASE_SHA}^{commit}" 2>/dev/null) || { echo "! BLOCKED — base sha $_BASE_SHA no longer resolves"; exit 1; }
    [ "$_HEAD_NOW" = "$_BASE_FULL" ] || { echo "! BLOCKED — HEAD has not collapsed to base (HEAD=$_HEAD_NOW expected=$_BASE_FULL) — the merge fence's own collapse assertion should have caught this; refusing to stage C1 files onto uncollapsed commits"; exit 1; }
fi
if [ -s "$IMPL_DIR/c1-deferred-files.txt" ]; then
    sort -u "$IMPL_DIR/c1-deferred-files.txt" | xargs -r git add --  # timeout: 5000 — deferred until Phase 3 is clean because git add-ing them earlier would have dirtied the index and broken the cherry-pick loop just run (same mechanism, see the C1 section's note)
fi
```

**Conflict** → two specialists touched overlapping code; the script stops mid-cherry-pick on the reported item (`CHERRY_PICK_HEAD` present) and returns the still-unapplied `remaining` entries, having applied every earlier entry as a **real, uncollapsed commit** (no reset ever runs on a conflicted call). Route to `conflict-resolution.md`'s task-creation pattern (Step 5a), substituting `CHERRY_PICK_HEAD` for `MERGE_HEAD` in the state check; after resolving, `git cherry-pick --continue`, then rebuild `PLAN_FILE` scoped to only the `remaining` entries before re-invoking the merge fence — `PLAN_FILE` is a fixed path holding the *full* plan (the producer fence above), so re-running the merge fence unmodified would re-pick already-applied entries and fail. `remaining` is read straight from `merge-result.json`, never typed in by hand — an operator substituting the wrong thing (or nothing) for a placeholder was the exact mechanism that let a legitimately-empty `remaining` become indistinguishable from a mistake:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
_REMAINING=$(python -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["remaining"]))' "$IMPL_DIR/merge-result.json") \
    || { echo "! BLOCKED — could not read 'remaining' from merge-result.json; did the merge fence run and write it?"; exit 1; }  # timeout: 5000
rm -f "$IMPL_DIR/merge-plan.json"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_merge_plan.py" \
    --commits "$IMPL_DIR/phase2-commits.jsonl" --action-items "$IMPL_DIR/action-items.jsonl" \
    --priority-order "$_REMAINING" --out "$IMPL_DIR/merge-plan.json" \
    --codemap-maps "$IMPL_DIR/codemap-maps.json" \
    || { echo "! BLOCKED — resumed merge plan not built"; exit 1; }  # timeout: 10000 — an empty $_REMAINING (merge-result.json's "remaining":[]) still needs to run: build_merge_plan.py writes a valid empty plan `[]`, and the resumed merge fence call below must still execute (with --base-sha, applying nothing) to trigger the collapse — see run_plan's base_sha docstring
```

Then re-run the merge fence — same `COMMIT_MODE`, same `--base-sha` (read from the same `resolve-base-sha-${CSID}` sentinel, unchanged since the first call this run) — **never skip this call even when `_REMAINING` is empty**: the conflicted item's own `--continue` already landed its commit as a real, uncollapsed commit alongside every earlier entry from this run, and `run_plan`'s combined reset now fires whenever `--base-sha` is supplied, independent of whether this particular call applies anything — an empty-plan call with `--base-sha` still collapses everything already on the branch above that base. Never omit `--base-sha` on a resumed call, empty plan or not: the resumed plan is shorter than (or, in this empty case, absent from) the original, so sizing the reset off `len(applied)` for that call alone (the pre-`--base-sha` behavior) would collapse only the entries picked in *this* call — nothing, in the empty case — and leave every entry applied before the conflict (plus the one resolved via `--continue`) as permanent real commits, violating `stage` mode's "no commits" contract and giving `grouped`/`all` stray per-item commits alongside the group/bulk commit. Once the resumed call itself returns `conflict: null`, run the **Clean run** step above (including the C1 staging fence) — it was never reached on the conflicted call.

Mark item's task per `COMMIT_MODE`, right after its own cherry-pick lands — not when its specialist group returns (a group finishing early doesn't mean its items are safely on the PR branch yet). `<item.task_id>` below = the id paired with `<item_id>` in `$IMPL_DIR/item-tasks.tsv` (written at Step 3e) — read from the file, never from memory of the Step 3e TaskCreate calls:

```text
# each / stage → completed now (commit landed, or staged = terminal; no "staged" task status)
# all / grouped → leave in_progress; post-loop commit block below flips after the real commit
if COMMIT_MODE == "each" or COMMIT_MODE == "stage":
    TaskUpdate(task_id=<item.task_id>, status="completed")
```

No commit for an item (it was in Phase 2's `skipped` list) → record the agent's reason; do NOT create an empty commit or add it to `PLAN_FILE`. Close its task now, same terminal status as REJECT (no implementation landed):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot locate skipped-items.txt"; exit 1; }
while IFS=$'\t' read -r item_id _reason; do
    # numeric-only guard, not a blank check: catches non-numeric garbage rows. Does NOT catch a
    # "\t999" row where a leading empty item_id collapsed into this one (tab is IFS whitespace even
    # with IFS set to just tab) — that case is prevented upstream, where item-tasks.tsv is written
    # (SKILL.md Step 3e checks $_ITEM_ID alone for digits, not concatenated with $_TASK_ID)
    case "$item_id" in ''|*[!0-9]*) echo "! skipping malformed skipped-items.txt row: item_id='$item_id' reason='$_reason'"; continue ;; esac
    _TID=$(awk -F'\t' -v id="$item_id" '$1==id{print $2}' "$IMPL_DIR/item-tasks.tsv")
    [ -n "$_TID" ] || { echo "! skipping — item $item_id has no task id in item-tasks.tsv"; continue; }
    echo "TaskUpdate target (deleted): item=$item_id task=$_TID"  # timeout: 3000
done < "$IMPL_DIR/skipped-items.txt"
```

Call `TaskUpdate(task_id=<printed task id>, status="deleted")` per line above.

Cleanup — remove each specialist worktree once all its commits are cherry-picked, then release the resolve mutex (recompute the deterministic lock path — the entry-block shell var is gone by this separate bash call). Read from `specialist-worktrees.txt`, not a shell array — a shell variable set in Phase 2's dispatch turn cannot survive to this separate Bash call regardless (bash state does not persist between Bash tool invocations), so the durable file Phase 2 wrote per group (above) is the only thing that can carry the paths this far:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot locate specialist-worktrees.txt"; exit 1; }
[ -f "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" ] && IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" || COMMIT_MODE="unset"
_WT_TOTAL=0; _WT_REMOVED=0; _KEPT_UNCONFIRMED=""; _KEPT_UNCOMMITTED=""
while IFS= read -r _wt; do
    [ -n "$_wt" ] || continue
    _WT_TOTAL=$((_WT_TOTAL + 1))
    # capture BEFORE removal — the worktree dir (and any `git -C` into it) is gone once `remove` succeeds
    _wt_branch=$(git -C "$_wt" symbolic-ref --short HEAD 2>/dev/null)
    if git worktree remove "$_wt" --force 2>/dev/null; then  # timeout: 5000 — a failure (locked worktree, already gone) must not abort merge-back, but must not read as success either
        _WT_REMOVED=$((_WT_REMOVED + 1))
        if [ -n "$_wt_branch" ]; then
            if [ "$COMMIT_MODE" = "each" ]; then
                # a specialist envelope's `commits` array is self-reported and could omit a commit the
                # specialist actually made on this branch — verify from git history instead of trusting
                # it. `git cherry <upstream> <branch>` marks with `+` any commit on <branch> whose
                # patch-id has no equivalent yet on <upstream>; cherry-pick gives a landed commit a new
                # sha but the same diff, so a fully-landed branch shows zero `+` lines against HEAD.
                _CHERRY_OUT=$(git cherry HEAD "$_wt_branch")  # no 2>/dev/null — branch is kept either way; an operator diagnosing a non-zero rc needs git's own reason, not a blank one
                _CHERRY_RC=$?
                _UNLANDED=$(printf '%s\n' "$_CHERRY_OUT" | grep -c '^+')
                if [ "$_CHERRY_RC" -eq 0 ] && [ "$_UNLANDED" -eq 0 ]; then
                    git branch -D "$_wt_branch" 2>/dev/null
                else
                    _KEPT_UNCONFIRMED="$_KEPT_UNCONFIRMED $_wt_branch"
                    echo "⚠ $_wt_branch: git cherry could not confirm every commit landed (rc=$_CHERRY_RC, $_UNLANDED unmatched) — kept for recovery instead of deleted. Expected if this pick's content changed during conflict resolution (its patch-id then differs from the original); reclaim manually via 'git worktree list'/'git branch -d' once confirmed landed"
                fi
            else
                # grouped/all/stage: the soft-reset collapsed the specialist's commits into a staged
                # diff, not yet a commit — until that diff is actually committed, this branch is the
                # only other copy of the work, so deleting it here would be the sole recovery route
                # gone the moment something goes wrong between here and the commit fence
                _KEPT_UNCOMMITTED="$_KEPT_UNCOMMITTED $_wt_branch"
            fi
        fi
    fi
done < "$IMPL_DIR/specialist-worktrees.txt"
[ "$_WT_TOTAL" -gt 0 ] && echo "→ removed $_WT_REMOVED/$_WT_TOTAL specialist worktree(s)"
[ -n "$_KEPT_UNCONFIRMED" ] && echo "→ specialist branches kept as recovery — git cherry could not confirm every commit landed:$_KEPT_UNCONFIRMED"
[ -n "$_KEPT_UNCOMMITTED" ] && echo "→ specialist branches kept as recovery — the staged diff is not yet committed:$_KEPT_UNCOMMITTED"
if [ -f "$IMPL_DIR/worktrees-before.txt" ]; then
    # a worktree present now but absent from the before-Phase-2 snapshot AND never removed above
    # (the loop only ever iterates specialist-worktrees.txt) came from a group whose envelope never
    # arrived (§Health monitoring's stalled-group case) — report it, never auto-remove: the group
    # may still be running.
    git worktree list --porcelain | sed -n 's/^worktree //p' > "$IMPL_DIR/worktrees-after-cleanup.txt"  # timeout: 5000
    comm -13 <(sort "$IMPL_DIR/worktrees-before.txt") <(sort "$IMPL_DIR/worktrees-after-cleanup.txt") > "$IMPL_DIR/worktrees-unreported.txt"
    [ -s "$IMPL_DIR/worktrees-unreported.txt" ] && echo "⚠ worktree(s) created this run but never reported by any group's envelope (a stalled group?) — left untouched, reclaim manually once you've confirmed the group isn't still running: $(tr '\n' ' ' < "$IMPL_DIR/worktrees-unreported.txt")"
fi
_GITDIR=$(git rev-parse --git-common-dir 2>/dev/null || echo ".git")  # timeout: 3000
_BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo "detached")  # timeout: 3000
rm -f "$_GITDIR/oss-resolve-${_BRANCH}.lock"  # timeout: 3000
# resolve-base-sha-${CSID} deliberately NOT removed here — SKILL.md's straggler gate (after this whole
# Step 8) reads it to range-bound its confirmation grep to this run's commits. Next run's entry block
# (mutex fence above) always overwrites it unconditionally before reading, so leaving it stale is safe.
```

> If Phase 3 stops on a conflict (routed to Step 5a) the lock is **not** released here — intentional: the run is still live. It clears on the retry's cleanup, or via the 30-min staleness override if the session is abandoned.

**After loop — `COMMIT_MODE=grouped` only**: group items per `GROUP_STRATEGY` (chosen at Step 3d alongside the commit-mode menu), commit each group. Read it back first:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}" ] && IFS= read -r GROUP_STRATEGY < "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}" || GROUP_STRATEGY="domain"
echo "GROUP_STRATEGY=$GROUP_STRATEGY"  # timeout: 3000
```

Only `labels` reaches the user; the other three group without another idle window:

- `domain` (default) — topic = each item's `.change` field, mapped by the `auto` table below
- `file` — topic = the item's `file` basename without extension (items sharing a file share a commit)
- `specialist` — topic = the Phase 2 `group` tag the item was dispatched under
- `labels` — ask, using the block below

Invoke `AskUserQuestion` — **`GROUP_STRATEGY=labels` only** — after the implementation loop completes (all items staged, no commits yet):

```text
AskUserQuestion: "Assign a topic label to each implemented item (e.g. style, logic, tests, docs, config).
Items implemented:
  <for each item in SELECTED_ITEMS: "#<id>: <summary>">
Type a topic for each item ID (e.g. '1=style 2=logic 3=tests'), or type 'auto' to infer labels from change field."
```

- User types labels → parse `<id>=<topic>` pairs from response
- User types `auto` → infer topic from each item's `.change` field: `style`→`style`, `test`→`tests`, `docs`→`docs`, `ci`→`ci`, `config`→`config`, `code`|`refactor`→`logic`; default `misc` when unclassified
- Any item not assigned a label → assign topic `misc`
- User skips (empty response or blank) → fall back to `each` mode: commit each already-staged item individually using the same `commit_action_item.py` path as `COMMIT_MODE=each`

`GROUP_STRATEGY` ≠ `labels` → skip the question entirely; derive topics from the strategy above (`domain` uses the same `auto` mapping). Every item lands in exactly one group; unclassified → `misc`.

Group items by topic label. For each unique topic group (ordered by first item ID in group), commit the group, then close out its tasks in a **separate** fence — the commit fence below carries a heredoc, and the manifest generator bails out of per-command extraction for any block containing `<<` (loses every command's individual allow-entry, not just the heredoc's — `plugins/CLAUDE.md` §Blueprint Blocks). Splitting keeps the close-out loop's `awk`/`grep` calls covered:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
rm -f "$IMPL_DIR/group-commit-status.txt"  # cleared first, not just overwritten at the end — a fence that dies mid-way (timeout/interrupt) between this and the "ok"/"failed" write must never leave a PRIOR group's stale "ok" for the close-out fence to read
[ -f "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" ] && IFS= read -r PR_REF < "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" || PR_REF="#0"  # reload (Check 41: fresh shell) — set in Step 4
[ -f "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" ] && IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" || CODEX_AVAILABLE="false"  # reload (Check 41: fresh shell) — set in Step 1; a bare shell var here would always read unset in this separate Bash call, so the Codex co-author trailer below could never fire
GROUP_IDS=(<item ids in this group>)
GROUP_SUMMARIES=(<item summaries in this group>)
_TOPIC="<topic>"
_GROUP_FILES=(<all files changed by items in this group>)
case "$_TOPIC${GROUP_IDS[*]}${GROUP_SUMMARIES[*]}${_GROUP_FILES[*]}" in *'<'*'>'*) echo "! BLOCKED — group commit placeholder not substituted"; exit 1 ;; esac
printf '%s\n' "${GROUP_IDS[@]}" > "$IMPL_DIR/group-ids.txt"  # persist — GROUP_IDS array dies with this Bash call, close-out fence below is a separate call
COMBINED_SUMMARY=$(printf '%s\n' "${GROUP_SUMMARIES[@]}" | head -5 | paste -sd, -)  # one array element (a whole summary) per line, not split on internal spaces — `tr ' ' '\n'` would join "fix the parser" as three separate words
COMMIT_MSG=$(mktemp)  # timeout: 3000
trap 'rm -f "$COMMIT_MSG"' EXIT  # RETURN never fires at top level of a Bash-tool block (not a function/sourced script); EXIT does
cat >"$COMMIT_MSG" <<EOF
${_TOPIC}: ${COMBINED_SUMMARY}

[resolve group] PR ${PR_REF} — items ${GROUP_IDS[*]}

---
Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>
$([ "${CODEX_AVAILABLE:-false}" = "true" ] && echo "Co-authored-by: OpenAI Codex <codex@openai.com>")
EOF
if python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_action_item.py" \
    --message-file "$COMMIT_MSG" \
    --files "${_GROUP_FILES[@]}"; then  # timeout: 10000
    echo ok > "$IMPL_DIR/group-commit-status.txt"
else
    echo failed > "$IMPL_DIR/group-commit-status.txt"
fi
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
[ -f "$IMPL_DIR/group-commit-status.txt" ] && IFS= read -r _STATUS < "$IMPL_DIR/group-commit-status.txt" || _STATUS="failed"
if [ "$_STATUS" != "ok" ]; then
    echo "! BLOCKED — group commit failed; not flipping tasks for this group's items"
else
    # task id from item-tasks.tsv, never from memory — a compaction between Step 3e and here would drop any in-memory id map
    while IFS= read -r item_id; do
        case "$item_id" in ''|*[!0-9]*) continue ;; esac
        _TID=$(awk -F'\t' -v id="$item_id" '$1==id{print $2}' "$IMPL_DIR/item-tasks.tsv")
        [ -n "$_TID" ] || { echo "! skipping — item $item_id has no task id in item-tasks.tsv"; continue; }
        echo "TaskUpdate target: item=$item_id task=$_TID"  # timeout: 3000
    done < "$IMPL_DIR/group-ids.txt"
fi
```

Commit subject format: `<topic>: <combined summary of items in group>` (≤72 chars total; truncate combined summary with `…` if needed). One commit per unique topic. Print `→ Committed group "<topic>" — items <ids>` after the commit fence. `! BLOCKED` from the close-out fence → group commit failed, no task flipped; investigate before re-running. Otherwise call `TaskUpdate(task_id=<printed task id>, status="completed")` per printed target line.

**After loop — `COMMIT_MODE=grouped` only**: once every topic group has committed, assert the index is empty — a group's `<all files changed by items in this group>` substitution silently omitting one of that group's staged files would otherwise leave it stranded, uncommitted, invisible to any later check:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" ] && IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" || COMMIT_MODE="unset"
# structurally scoped to grouped — a populated index is stage mode's correct terminal state (it never
# commits), so running this fence there would false-block on intended behaviour; the heading above
# said "grouped only" in prose alone, which a fence run out of its documented mode can't enforce
case "$COMMIT_MODE" in
    grouped)
        git diff --cached --quiet || { echo "! BLOCKED — staged files remain after every group committed: $(git diff --cached --name-only | tr '\n' ' ') — a group's file-list substitution likely omitted one; commit it separately (it belongs to whichever group's items touch it) before continuing"; exit 1; }  # timeout: 5000
        ;;
    each|all|stage) echo "empty-index assertion skipped — COMMIT_MODE=$COMMIT_MODE, not grouped" ;;
    *) echo "! BLOCKED — COMMIT_MODE is '$COMMIT_MODE', not each/grouped/all/stage"; exit 1 ;;  # fail closed on a lost/empty/bogus sentinel — a bare *) skip here would silently drop the assertion instead of blocking, same class the merge fence's own COMMIT_MODE enumeration guards against
esac
```

**After loop — `COMMIT_MODE=all` only**: derive counters from `CHALLENGE_LOG`, create single commit:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" ] && IFS= read -r PR_REF < "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" || PR_REF="#0"  # reload (Check 41: fresh shell) — set in Step 4
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -f "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" ] && IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" || CODEX_AVAILABLE="false"  # reload (Check 41: fresh shell) — set in Step 1; a bare shell var here would always read unset in this separate Bash call, so --codex could never be passed
# grep -c prints 0 AND exits 1 on no match — `|| echo 0` would yield "0\n0" and commit_all_items.py rejects it; only a missing file leaves stdout empty
# anchored right after id= (resolution= is the field placed there, before any free-text field — see the append
# block above) so a reviewer's quoted text can never masquerade as the real field; -i absorbs casing drift
N_AS_SUGGESTED=$(grep -ciE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*as-suggested' "$IMPL_DIR/challenge-log.txt" 2>/dev/null || :); N_AS_SUGGESTED=${N_AS_SUGGESTED:-0}
N_SELF_RESOLVED=$(grep -ciE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*self-resolved' "$IMPL_DIR/challenge-log.txt" 2>/dev/null || :); N_SELF_RESOLVED=${N_SELF_RESOLVED:-0}
N_REJECTED=$(grep -ciE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null || :); N_REJECTED=${N_REJECTED:-0}  # same anchored pattern as _REJECTED_IDS below — must agree with it or the printed count and the actually-closed ids diverge
SUMMARIES_FILE=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_all_items.py" "$PR_REF" "$N_AS_SUGGESTED" "$N_SELF_RESOLVED" "$N_REJECTED" "$SUMMARIES_FILE" $( [ "${CODEX_AVAILABLE:-false}" = "true" ] && echo "--codex" )  # timeout: 10000
```

After the commit succeeds, flip all staged items to completed (deferred from per-item loop body where commit had not yet happened) — excludes ids already closed above (REJECT, `skipped-items.txt`); task id from `item-tasks.tsv`, never from memory:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot reconcile item-tasks.tsv"; exit 1; }
_SKIPPED_IDS=$(cut -f1 "$IMPL_DIR/skipped-items.txt" 2>/dev/null)
# anchored right after id= — see the append block's note; resolution= there can never be a quoted substring
_REJECTED_IDS=$(grep -iE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null | sed -n 's/^id[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p')
if [ -f "$IMPL_DIR/item-tasks.tsv" ]; then
    while IFS=$'\t' read -r item_id task_id; do
        case "$item_id" in ''|*[!0-9]*) continue ;; esac  # numeric-only, not blank-only — see skipped-items.txt loop above for why
        { printf '%s\n' "$_SKIPPED_IDS" "$_REJECTED_IDS" | grep -qx "$item_id"; } && continue  # already closed
        [ -n "$task_id" ] || { echo "! skipping — item $item_id has an empty task id in item-tasks.tsv"; continue; }
        echo "TaskUpdate target: item=$item_id task=$task_id"  # timeout: 3000
    done < "$IMPL_DIR/item-tasks.tsv"
else
    echo "n/a — item-tasks.tsv absent (report mode never runs Step 3e)"  # timeout: 3000
fi
```

Call `TaskUpdate(task_id=<printed task id>, status="completed")` per surviving line.

## Step 8 — design scope & residual limitations

Worktree isolation + the two grouping tiebreaks + centrality ordering *reduce* Phase 3 conflicts; they don't eliminate them. Phase 3's cherry-pick conflict path (Step 5a) is the catch-all for whatever slips through.

**Deliberate design choices (not limitations):**

- **Python-scoped semantic grouping** — codemap indexes `.py` (by design — the plugin's stated scope). Same-file *textual* conflict on `.yaml`/`.toml`/`.github/*.yml`/`.md` is still caught: the file-ownership tiebreak is path-based, not codemap-based, so it works for any language. Only the *semantic* layers (import-coupling, centrality) are Python-scoped; non-Python items skip them (no coupling merge, centrality 0 → ordered last). Config/CI PRs keep full textual safety.
- **Depth-1 coupling** — coupling merges only directly-importing pairs, not transitive A→B→C. Deliberate: every coupling-merge trades parallelism for conflict-safety; a direct import is a high break-risk (good trade), a transitive one is a rare break at the *same* parallelism cost (bad trade) — and a central module's transitive closure would collapse the whole batch into one group, defeating the parallelism the redesign exists for. Direct-only is the optimum, not a shortfall.
- **Import centrality, not call centrality** — ordering weight is module `rdep_count` (import graph), matching the module-granularity of the grouping. `fn-central` (call graph) is finer than the unit being ordered, so it wouldn't change whole-group order.

**Residual limitations (true gaps, all backstopped by Step 5a):**

- **Centrality ≠ semantic-break cure** — most-central-first ordering cuts conflict *cascade* and yields saner intermediate trees, but a dependent commit already contains its call to the old contract; landing order can't un-break it. The actual mitigation for semantic breakage is the per-agent blast-radius context (each specialist is told its callers); centrality is only ordering.
- **Stale index** — coupling + centrality read whatever codemap index exists. Currency is gated at skill entry (SKILL.md Gate B refreshes a stale index before Step 8), so mid-run staleness is the only exposure, and it only skews grouping slightly (all heuristic, never dangerous). `central` auto-build exceeding the 15 s budget → maps empty → plan falls back to priority order.
- **Mutex is advisory** — the branch lock stops a *second oss:resolve*, not a human `git push` or an unrelated tool writing the tree; that class is detected after the fact by the Phase 3 HEAD-fingerprint warning + Step 5a, not prevented.
