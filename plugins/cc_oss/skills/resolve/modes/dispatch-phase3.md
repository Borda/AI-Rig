<!-- oss:resolve Step 8 phase3 — executed via: cat $_OSS_RESOLVE/modes/dispatch-phase3.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md -->

<!-- Input: Phase 2 envelopes + phase2-commits.jsonl, COMMIT_MODE/GROUP_STRATEGY; loaded after every Phase 2 group's fences ran or it is ⏱ -->

<!-- Output: items implemented/staged/committed; CHALLENGE_LOG populated; CHANGE_SCOPE set for Step 9 -->

### Phase 3: Merge-back — sequential, orchestrator-owned

**HEAD fingerprint check** — every Phase 2 worktree was pinned to `phase2-base-sha` at step 0 (a chain link to a tip descending from it, §Spawn base); verify the PR branch hasn't moved under us while Phase 2 ran. A move past `phase2-base-sha` means an external write (human push, or a run that slipped the mutex) landed during Phase 2 — cherry-picks still apply (they replay each diff onto the current tip), but overlapping edits now surface as conflicts, so surface the drift rather than stack silently. A move that stops at `phase2-base-sha` happened before Phase 2 spawned (C1's `each` commits): the worktrees already sit on it, so it is reported as such, never as drift. Either way `resolve-base-sha` is re-pointed to the current tip — the merge fence's guard and combined reset anchor on it.

Precompute every specialist's original pre-cherry-pick patch-id first, in its own fenced block. `extract_commit_shas.py ledger` validates every ledger row before the check below consumes its output; a non-zero exit blocks the check even when earlier rows produced a partial list. Keep the check block's guard reads shared with every other fence (`plugins/CLAUDE.md` §Blueprint Blocks). `$IMPL_DIR/phase2-plan-shas.txt`/`phase2-plan-ids.txt` are fixed paths under `IMPL_DIR`, overwritten every run — no `mktemp`/`rm -f` needed, so nothing here trips the manifest generator's destructive-command filter either:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot verify stranded picks before the HEAD-fingerprint check below"; exit 1; }
: > "$IMPL_DIR/phase2-plan-shas.txt"
if [ -s "$IMPL_DIR/phase2-commits.jsonl" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/extract_commit_shas.py" ledger "$IMPL_DIR/phase2-commits.jsonl" > "$IMPL_DIR/phase2-plan-shas.txt" \
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
    # worktrees pin to phase2-base-sha (after C1 each-commits); only a move past it is Phase 2 drift
    IFS= read -r _P2_BASE < "$IMPL_DIR/phase2-base-sha" 2>/dev/null || _P2_BASE="$_BASE_SHA"
    if [ "$_NOW_SHA" = "$_P2_BASE" ]; then
        echo "→ HEAD advanced before Phase 2 spawned: ${_BASE_SHA:0:8} → ${_NOW_SHA:0:8} (pre-Phase 2 commits, e.g. C1 each) — worktrees were pinned on it, not drift"
    else
        echo "⚠ base HEAD moved during Phase 2: ${_P2_BASE:0:8} → ${_NOW_SHA:0:8} (external write)."
        echo "  Cherry-picks apply onto the new base; any overlapping edit surfaces as a conflict → routed to Step 5a below."
    fi
    # re-point the persisted base at the new tip — the merge fence's combined reset targets this sentinel
    # absolutely (git reset --soft --end-of-options <sha>, not HEAD~n); leaving it at the stale pre-drift
    # value would rewind the branch PAST the external commit just detected, making it unreachable from HEAD
    echo "$_NOW_SHA" > "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}"  # timeout: 3000
fi
```

Build the cherry-pick plan in **original `SELECTED_ITEMS` priority order**, interleaved across specialist groups in that order (item ids are stable handles, not priorities) — NOT grouped by specialist, so the base order matches severity ranking regardless of which group finished first.

- This global sort is safe because Phase 1/2 grouping preserved each specialist's internal relative order (stable partition) — sorting by original priority never reorders two items from the same specialist relative to each other.
- Each entry also carries its worktree `group` tag (from Phase 2) and its `module` — the **canonical codemap name** for the item's `.file`, read from `file_module` in `$IMPL_DIR/codemap-maps.json` (built in Structural prep), blank when unresolved.
- Never hand-derive it with a sed transform: codemap names a package `__init__.py` after the package (`pkg`, not `pkg.__init__`), so a sed guess silently mismatches the centrality keys and scores 0.

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

**Centrality ordering** (lands the most foundational change first, so contract-defining commits precede their dependents): the `{module: rdep_count}` centrality map was already built once in Structural prep (`$IMPL_DIR/codemap-maps.json`, from a single authoritative `codemap-py query central` pass — not the 20-capped `BLAST_RADIUS_CONTEXT`, which saturates).

- Extract it to a file so the merge step can reorder **whole worktree groups** most-central-first.
- Safe precisely because the file-ownership tiebreak guarantees distinct groups touch disjoint files — reordering whole chains can't add a textual conflict, and commit order **within** a chain is never touched (chains may build on themselves).
- Missing maps (no `codemap-py query` / query failure) → flag omitted, plan applies in priority order unchanged.

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
# C1's unstaged-edit exemption (see the C1 section's note, dispatch-prelude.md) holds only when the C1 item's file is
# disjoint from every Phase 2 item's file — C1 is invisible to Phase 2's file-ownership tiebreak
# (it skips Phase 1/2 entirely), so nothing else in the file checks this. An overlap makes the
# cherry-pick below refuse identically to the pre-fix dirty-index case: phantom conflict, empty file
# list, no CHERRY_PICK_HEAD, no recovery route — so it must be caught before the cherry-pick runs.
if [ -s "$IMPL_DIR/c1-deferred-files.txt" ] && [ -s "$PLAN_FILE" ]; then
    _SHAS_FILE=$(mktemp)  # timeout: 3000
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/extract_commit_shas.py" plan "$PLAN_FILE" > "$_SHAS_FILE"  # timeout: 5000 — no 2>/dev/null: a read/parse failure here must surface, not silently read as "no shas, no overlap"
    [ $? -eq 0 ] || { echo "! BLOCKED — overlap guard: failed to read plan shas from $PLAN_FILE"; rm -f "$_SHAS_FILE"; exit 1; }
    # -z (NUL-delimited) is required, not cosmetic: a bare `for _f in $(...)` word-splits on
    # IFS, so a path containing a space is silently missed on one side and falsely flagged as an
    # unrelated overlap on the other (same class as the array fix in the C1 section, dispatch-prelude.md);
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
    _OVERLAP=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/compare_deferred_files.py" "$_ROOT" "$_DIFF_FILES" "$IMPL_DIR/c1-deferred-files.txt")
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
    sort -u "$IMPL_DIR/c1-deferred-files.txt" | tr '\n' '\0' | xargs -0 -r git add --  # timeout: 5000 — deferred until Phase 3 is clean because git add-ing them earlier would have dirtied the index and broken the cherry-pick loop just run (same mechanism, see the C1 section's note); -0/tr NUL-delimits so a Codex-supplied path containing a space or quote is never word-split or quote-interpreted by xargs
fi
```

**Conflict** → two specialists touched overlapping code; the script stops mid-cherry-pick on the reported item (`CHERRY_PICK_HEAD` present) and returns the still-unapplied `remaining` entries, having applied every earlier entry as a **real, uncollapsed commit** (no reset ever runs on a conflicted call).

- Route to `conflict-resolution.md`'s task-creation pattern (Step 5a), substituting `CHERRY_PICK_HEAD` for `MERGE_HEAD` in the state check; after resolving, `git cherry-pick --continue`, then rebuild `PLAN_FILE` scoped to only the `remaining` entries before re-invoking the merge fence — `PLAN_FILE` is a fixed path holding the *full* plan (the producer fence above), so re-running the merge fence unmodified would re-pick already-applied entries and fail.
- `remaining` is read straight from `merge-result.json` by `build_merge_plan.py --resume-from`, never typed in by hand — an operator substituting the wrong thing (or nothing) for a placeholder was the exact mechanism that let a legitimately-empty `remaining` become indistinguishable from a mistake:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_merge_plan.py" \
    --commits "$IMPL_DIR/phase2-commits.jsonl" --action-items "$IMPL_DIR/action-items.jsonl" \
    --resume-from "$IMPL_DIR/merge-result.json" --out "$IMPL_DIR/merge-plan.json" \
    --codemap-maps "$IMPL_DIR/codemap-maps.json"; _RC=$?  # timeout: 10000 — exit 3 = merge-result.json absent, unreadable or without a "remaining" list; an empty "remaining":[] still needs to run: build_merge_plan.py writes a valid empty plan `[]`, and the resumed merge fence call below must still execute (with --base-sha, applying nothing) to trigger the collapse — see run_plan's base_sha docstring
[ "$_RC" -ne 3 ] || { echo "! BLOCKED — could not read 'remaining' from merge-result.json; did the merge fence run and write it?"; exit 1; }
[ "$_RC" -eq 0 ] || { rm -f "$IMPL_DIR/merge-plan.json"; echo "! BLOCKED — resumed merge plan not built"; exit 1; }  # a failed build never leaves a stale plan for the merge fence; exit 3 above keeps the old plan, since nothing was read
```

Then re-run the merge fence — same `COMMIT_MODE`, same `--base-sha` (read from the same `resolve-base-sha-${CSID}` sentinel, unchanged since the first call this run) — **never skip this call even when `remaining` is empty**: the conflicted item's own `--continue` already landed its commit as a real, uncollapsed commit alongside every earlier entry from this run, and `run_plan`'s combined reset now fires whenever `--base-sha` is supplied, independent of whether this particular call applies anything — an empty-plan call with `--base-sha` still collapses everything already on the branch above that base.

- Never omit `--base-sha` on a resumed call, empty plan or not: the resumed plan is shorter than (or, in this empty case, absent from) the original, so sizing the reset off `len(applied)` for that call alone (the pre-`--base-sha` behavior) would collapse only the entries picked in *this* call — nothing, in the empty case — and leave every entry applied before the conflict (plus the one resolved via `--continue`) as permanent real commits, violating `stage` mode's "no commits" contract and giving `grouped`/`all` stray per-item commits alongside the group/bulk commit.
- Once the resumed call itself returns `conflict: null`, run the **Clean run** step above (including the C1 staging fence) — it was never reached on the conflicted call.

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

Cleanup — remove each specialist worktree once all its commits are cherry-picked, then release the resolve mutex (recompute the deterministic lock path — the entry-block shell var is gone by this separate bash call). Read from `specialist-worktrees.txt`, not a shell array — a shell variable set in Phase 2's dispatch turn cannot survive to this separate Bash call regardless (bash state does not persist between Bash tool invocations), so the durable file Phase 2 wrote per group (`dispatch-phase2.md`) is the only thing that can carry the paths this far:

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
# (mutex fence, dispatch-prelude.md) always overwrites it unconditionally before reading, so leaving it stale is safe.
```

> If Phase 3 stops on a conflict (routed to Step 5a) the lock is **not** released here — intentional: the run is still live. It clears on the retry's cleanup, or via the 30-min staleness override if the session is abandoned.

**After loop — `COMMIT_MODE=grouped` only**: group items per `GROUP_STRATEGY` (chosen at Step 3d alongside the commit-mode menu), commit each group. Read it back first:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}" ] && IFS= read -r GROUP_STRATEGY < "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}" || GROUP_STRATEGY="domain"
echo "GROUP_STRATEGY=$GROUP_STRATEGY"  # timeout: 3000
```

No strategy reaches the user here on the normal path — Step 3d collected every grouping answer, typed labels included; only a lost labels file asks again (below):

- `domain` (default) — topic = each item's `.change` field, mapped by the `auto` table below
- `file` — topic = the item's `file` basename without extension, derived with `basename "$_FILE" | sed 's/\.[^.]*$//' | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/-\+/-/g; s/^-//; s/-$//'` (items sharing a file share a commit) — a PR-controlled filename can contain characters git permits in a path but a commit-subject prefix should not carry literally; `my_module.py` → `my-module`, `` a`id`b.py `` → `a-id-b`
- `specialist` — topic = the Phase 2 `group` tag the item was dispatched under
- `labels` — topic = the label typed at Step 3d, read from `$IMPL_DIR/group-labels.tsv` with the Read tool (`<id>\t<topic>` rows, topics already sanitized to `[a-z0-9-]`); never ask again here

**`GROUP_STRATEGY=labels` only** — file missing or empty (a lost write, or no valid pair) → the typed labels never reached disk, so ask for them here — the same question Step 3d asks, recovery only:

```text
AskUserQuestion: "Assign a topic label to each implemented item (e.g. style, logic, tests, docs, config).
Items implemented:
  <for each item in SELECTED_ITEMS: "#<id>: <summary>">
Type a topic for each item ID (e.g. '1=style 2=logic 3=tests'), or type 'auto' to infer labels from change field."
```

- User types labels → parse `<id>=<topic>` pairs from response
- User types `auto` → infer topics with the `auto` mapping below
- User skips (empty response or blank) → fall back to `each` mode: commit each already-staged item individually using the same `commit_action_item.py` path as `COMMIT_MODE=each`

`auto` mapping (used by `domain`): topic from each item's `.change` field: `style`→`style`, `test`→`tests`, `docs`→`docs`, `ci`→`ci`, `config`→`config`, `code`|`refactor`→`logic`; default `misc` when unclassified. Every item lands in exactly one group; an implemented item with no label or classification → `misc`; a labelled id that was rejected or skipped is ignored.

Group items by topic label. For each unique topic group (ordered by first item ID in group), create `$IMPL_DIR/group-commit-now.txt` with the Write tool — one line, the topic then that group's item ids, space-separated (`tests 3 7 9`) — and run the commit fence unedited; it consumes the file, so each group writes its own. Then close out its tasks in a **separate** fence — this fence's own `git diff-tree`/`git log` calls need the guard reads shared with every other fence, and keeping them apart from the close-out loop's `awk`/`grep` calls keeps both independently covered (`plugins/CLAUDE.md` §Blueprint Blocks).

**SECURITY — file list and per-item summaries are derived from git itself, never from an LLM-typed array.** `phase2-commits.jsonl` (`{item_id, sha, group}`, written by the Phase 2 ledger fence in `dispatch-phase2.md`) already has, for every Phase-2-committed item, the real commit; `git diff-tree`/`git log` against that sha gives the exact files and subject with zero risk of the specialist's self-report omitting one (the residual risk `design-scope.md` already flags).

- A **C1** item (Codex-direct) skips Phase 1+2 entirely and so has no row in `phase2-commits.jsonl` — for those, fall back to `c1-item-files.tsv`/`c1-item-summary.tsv` (written by the C1 commit fence in `dispatch-prelude.md`).
- An id in neither source (rejected, skipped, or a stalled Phase-2 group) is warned and excluded from this group rather than hard-blocking the commit:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
echo failed > "$IMPL_DIR/group-commit-status.txt"  # reset first, not just overwritten at the end — a fence that dies mid-way (timeout/interrupt) between this and the "ok"/"failed" write must never leave a PRIOR group's stale "ok" for the close-out fence to read; no rm, which drops the whole block from the blueprint manifest
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
IFS= read -r PR_REF < "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" 2>/dev/null || PR_REF=""
if [ "$PR_NUMBER" = "n/a" ]; then
    [ "$PR_REF" = "n/a (local report)" ] || { echo "! BLOCKED — PR reference missing or stale for local report"; exit 1; }
else
    case "$PR_NUMBER" in ''|*[!0-9]*) echo "! BLOCKED — PR number missing or invalid"; exit 1 ;; esac
    case "$PR_REF" in "#$PR_NUMBER"|https://*/pull/"$PR_NUMBER") ;; *) echo "! BLOCKED — PR reference missing or mismatched"; exit 1 ;; esac
fi
[ -f "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" ] && IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" || CODEX_AVAILABLE="false"  # reload (Check 41: fresh shell) — set in Step 1; a bare shell var here would always read unset in this separate Bash call, so the Codex co-author trailer below could never fire
# topic + ids from a Write-tool file, never placeholders: block text stays invariant for the blueprint manifest
_GROUP_NOW="$IMPL_DIR/group-commit-now.txt"
[ -s "$_GROUP_NOW" ] || { echo "! BLOCKED — $_GROUP_NOW missing; create it with the Write tool (one line: <topic> then this group's item ids, space-separated) before running this block"; exit 1; }
_TOPIC=$(awk '{gsub(/\r/, "")} NF {n++; t = $1} END {if (n == 1) print t}' "$_GROUP_NOW")
_GROUP_IDS=$(awk '{gsub(/\r/, "")} NF {n++; $1 = ""; s = $0} END {if (n == 1) print s}' "$_GROUP_NOW")
case "$_TOPIC" in ''|*[!a-z0-9-]*) echo "! BLOCKED — topic must be lowercase alphanumeric/hyphen (first word of the one line in $_GROUP_NOW)"; exit 1 ;; esac
case "$_GROUP_IDS" in *[!0-9\ ]*|'') echo "! BLOCKED — group ids in $_GROUP_NOW must be numeric, space-separated, after the topic"; exit 1 ;; esac
case "$_GROUP_IDS" in *[0-9]*) ;; *) echo "! BLOCKED — group ids missing: $_GROUP_NOW names no item after the topic"; exit 1 ;; esac
# consumed: the next group writes its own line, so a stale one never commits twice
mv "$_GROUP_NOW" "$_GROUP_NOW.done"  # timeout: 3000
: > "$IMPL_DIR/group-files.txt"; : > "$IMPL_DIR/group-summaries.txt"
_COMMITTED_IDS=()  # only ids that actually contributed a file/summary below — an id with neither NEVER reaches group-ids.txt or the commit body, so the close-out fence and SKILL.md's straggler gate can't mark an unimplemented item completed
# cmd-substitution splits under zsh too, a bare $VAR does not
for _gid in $(printf '%s\n' "$_GROUP_IDS"); do
    _SHA=$(jq -r --arg id "$_gid" 'select((.item_id|tostring)==$id) | .sha' "$IMPL_DIR/phase2-commits.jsonl" 2>/dev/null | head -1)
    if [ -n "$_SHA" ]; then
        case "$_SHA" in *[!0-9a-f]*|'') echo "! BLOCKED — sha '$_SHA' for item $_gid is not hex"; exit 1 ;; esac
        [ "${#_SHA}" -ge 7 ] || { echo "! BLOCKED — sha '$_SHA' for item $_gid too short to be a real commit"; exit 1; }
        git diff-tree --no-commit-id --name-only -r --end-of-options "$_SHA" >> "$IMPL_DIR/group-files.txt" || { echo "! BLOCKED — git diff-tree failed for sha $_SHA"; exit 1; }
        git log -1 --format=%s --end-of-options "$_SHA" >> "$IMPL_DIR/group-summaries.txt" || { echo "! BLOCKED — git log failed for sha $_SHA"; exit 1; }
        _COMMITTED_IDS+=("$_gid")
    elif grep -q "^${_gid}$(printf '\t')" "$IMPL_DIR/c1-item-files.tsv" 2>/dev/null; then
        awk -F'\t' -v id="$_gid" '$1==id{print $2}' "$IMPL_DIR/c1-item-files.tsv" >> "$IMPL_DIR/group-files.txt"
        awk -F'\t' -v id="$_gid" '$1==id{print $2; exit}' "$IMPL_DIR/c1-item-summary.tsv" >> "$IMPL_DIR/group-summaries.txt"
        _COMMITTED_IDS+=("$_gid")
    else
        echo "⚠ item $_gid has no Phase-2 commit and no C1 record (rejected/skipped/stalled?) — excluded from group '$_TOPIC'"
    fi
done
printf '%s\n' "${_COMMITTED_IDS[@]}" > "$IMPL_DIR/group-ids.txt"  # persisted AFTER exclusion, from the array still live in this Bash call — close-out fence below (separate call) reads only ids that actually contributed
sort -u "$IMPL_DIR/group-files.txt" -o "$IMPL_DIR/group-files.txt"
[ -s "$IMPL_DIR/group-files.txt" ] || { echo "! BLOCKED — group '$_TOPIC' has no resolvable files across any of its items"; exit 1; }
# message built in Python: item subjects already carry type(scope): — topic never prepended (was "tests: test(x): …")
_BUILD_ARGS=(--build-group --topic "$_TOPIC" --summaries-file "$IMPL_DIR/group-summaries.txt" --pr "$PR_REF" --items "${_COMMITTED_IDS[*]}")
[ "${CODEX_AVAILABLE:-false}" = "true" ] && _BUILD_ARGS+=(--codex)
_GROUP_FILES=()
while IFS= read -r _f; do [ -n "$_f" ] && _GROUP_FILES+=("$_f"); done < "$IMPL_DIR/group-files.txt"
if python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_action_item.py" "${_BUILD_ARGS[@]}" \
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

Commit subject format: Conventional Commits, built by `commit_action_item.py --build-group` — never `<topic>: <type(scope): …>`. One item with a typed subject → that subject verbatim. Otherwise type and scope = what the items share, else derived from the topic (`tests`→`test`, `logic`→`fix`, `misc`/`config`→`chore`; a file/specialist/custom topic becomes the scope); description = first item's, plus `(+N more)`, ≤72 chars total (word-boundary cut with `…`). Multi-item body lists every item subject. One commit per unique topic. Print `→ Committed group "<topic>" — items <ids>` after the commit fence. `! BLOCKED` from the close-out fence → group commit failed, no task flipped; investigate before re-running. Otherwise call `TaskUpdate(task_id=<printed task id>, status="completed")` per printed target line.

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
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
IFS= read -r PR_REF < "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}" 2>/dev/null || PR_REF=""
if [ "$PR_NUMBER" = "n/a" ]; then
    [ "$PR_REF" = "n/a (local report)" ] || { echo "! BLOCKED — PR reference missing or stale for local report"; exit 1; }
else
    case "$PR_NUMBER" in ''|*[!0-9]*) echo "! BLOCKED — PR number missing or invalid"; exit 1 ;; esac
    case "$PR_REF" in "#$PR_NUMBER"|https://*/pull/"$PR_NUMBER") ;; *) echo "! BLOCKED — PR reference missing or mismatched"; exit 1 ;; esac
fi
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -f "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" ] && IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" || CODEX_AVAILABLE="false"  # reload (Check 41: fresh shell) — set in Step 1; a bare shell var here would always read unset in this separate Bash call, so --codex could never be passed
# grep -c prints 0 AND exits 1 on no match — `|| echo 0` would yield "0\n0" and commit_all_items.py rejects it; only a missing file leaves stdout empty
# anchored right after id= (resolution= is the field placed there, before any free-text field — see the append
# block, dispatch-prelude.md) so a reviewer's quoted text can never masquerade as the real field; -i absorbs casing drift
N_AS_SUGGESTED=$(grep -ciE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*as-suggested' "$IMPL_DIR/challenge-log.txt" 2>/dev/null || :); N_AS_SUGGESTED=${N_AS_SUGGESTED:-0}
N_SELF_RESOLVED=$(grep -ciE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*self-resolved' "$IMPL_DIR/challenge-log.txt" 2>/dev/null || :); N_SELF_RESOLVED=${N_SELF_RESOLVED:-0}
N_REJECTED=$(grep -ciE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null || :); N_REJECTED=${N_REJECTED:-0}  # same anchored pattern as _REJECTED_IDS below — must agree with it or the printed count and the actually-closed ids diverge
SUMMARIES_FILE=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/commit_all_items.py" "$PR_REF" "$N_AS_SUGGESTED" "$N_SELF_RESOLVED" "$N_REJECTED" "$SUMMARIES_FILE" $( [ "${CODEX_AVAILABLE:-false}" = "true" ] && echo "--codex" )  # timeout: 10000
```

After the commit succeeds, flip only selected items with an implementation record to completed (deferred from per-item loop body where commit had not yet happened). A Phase 2 commit record or a Git-verified C1 file record is required for each id; the clean Phase 3 result proves the recorded Phase 2 work was merged before the bulk commit. A selected item absent from both ledgers remains `in_progress` for the straggler gate. Exclude ids already closed above (REJECT, `skipped-items.txt`). The persisted `selected-items.txt` is authoritative for scope; task ids still come from `item-tasks.tsv`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; cannot reconcile item-tasks.tsv"; exit 1; }
[ -s "$IMPL_DIR/selected-items.txt" ] || { echo "! BLOCKED — selected-items.txt missing/empty; cannot complete tasks"; exit 1; }
[ -s "$IMPL_DIR/merge-result.json" ] && jq -e '.conflict == null and .remaining == []' "$IMPL_DIR/merge-result.json" >/dev/null || { echo "! BLOCKED — Phase 3 has no clean result; cannot complete bulk tasks"; exit 1; }
IFS= read -r _SELECTED_IDS < "$IMPL_DIR/selected-items.txt"
case "$_SELECTED_IDS" in ''|*[!0-9\ ]*) echo "! BLOCKED — selected-items.txt has invalid item ids"; exit 1 ;; esac
set -- $(printf '%s\n' "$_SELECTED_IDS")  # cmd-substitution splits under zsh too, a bare $VAR does not
[ "$#" -gt 0 ] || { echo "! BLOCKED — selected-items.txt contains no item ids"; exit 1; }
_SKIPPED_IDS=$(cut -f1 "$IMPL_DIR/skipped-items.txt" 2>/dev/null)
# anchored right after id= — see the append block's note; resolution= there can never be a quoted substring
_REJECTED_IDS=$(grep -iE '^id[[:space:]]*=[[:space:]]*[0-9]+[[:space:]]+resolution[[:space:]]*=[[:space:]]*rejected' "$IMPL_DIR/challenge-log.txt" 2>/dev/null | sed -n 's/^id[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p')
if [ -f "$IMPL_DIR/item-tasks.tsv" ]; then
    while IFS=$'\t' read -r item_id task_id; do
        case "$item_id" in ''|*[!0-9]*) continue ;; esac  # numeric-only, not blank-only — see skipped-items.txt loop above for why
        { printf '%s\n' "$@" | grep -qx "$item_id"; } || continue  # deferred task row is retained only for the straggler gate
        { printf '%s\n' "$_SKIPPED_IDS" "$_REJECTED_IDS" | grep -qx "$item_id"; } && continue  # already closed
        if ! jq -se --arg id "$item_id" 'any(.[]; (.item_id|tostring)==$id and (.sha|type)=="string" and (.sha|length)>0)' "$IMPL_DIR/phase2-commits.jsonl" >/dev/null 2>&1 \
            && ! awk -F'\t' -v id="$item_id" '$1==id && $2!=""{found=1} END{exit !found}' "$IMPL_DIR/c1-item-files.tsv" 2>/dev/null; then
            echo "! pending — item $item_id has no Phase 2 commit or Git-verified C1 file record"
            continue
        fi
        [ -n "$task_id" ] || { echo "! skipping — item $item_id has an empty task id in item-tasks.tsv"; continue; }
        echo "TaskUpdate target: item=$item_id task=$task_id"  # timeout: 3000
    done < "$IMPL_DIR/item-tasks.tsv"
else
    echo "n/a — item-tasks.tsv absent (report mode never runs Step 3e)"  # timeout: 3000
fi
```

Call `TaskUpdate(task_id=<printed task id>, status="completed")` per surviving line.
