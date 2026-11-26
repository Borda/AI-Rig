<!-- oss:resolve Step 11 — executed inline: cat $_OSS_RESOLVE/modes/final-report.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 11) -->

## Step 11: Final report

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary3: pre-final-report write (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR="n/a"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="unknown"
_PRESERVE="pr=${_PR_NUMBER}, final-report=pending-write, impl-dir=${_IMPL_DIR}, challenge-log=${_IMPL_DIR}/challenge-log.txt, item-tasks=${_IMPL_DIR}/item-tasks.tsv, push-status=${PUSH_STATUS}, push-unblock=${_IMPL_DIR}/push-unblock.txt"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "final-report (after push)" "$_IMPL_DIR" "${_PRESERVE}" "write final report → post-PR action gate"  # timeout: 5000
echo "PUSH_STATUS=$PUSH_STATUS"
if [[ "$_PR_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
    IFS= read -r _URL < "${TMPDIR:-/tmp}/resolve-pr-html-url-${CSID}" 2>/dev/null || _URL=""
    [[ "$_URL" =~ ^https://[^/]+/[^/]+/[^/]+/pull/${_PR_NUMBER}$ ]] || _URL="link unavailable"
    _DELIVERY="$PUSH_STATUS"
    if [ "$PUSH_STATUS" = pushed ]; then
        IFS= read -r _LOCAL_HEAD < "${TMPDIR:-/tmp}/resolve-local-head-${CSID}" 2>/dev/null || _LOCAL_HEAD=""
        IFS= read -r _PR_HEAD < "${TMPDIR:-/tmp}/resolve-pr-head-${CSID}" 2>/dev/null || _PR_HEAD=""
        _DELIVERY=unverified
        _OID_RE='^([0-9a-f]{40}|[0-9a-f]{64})$'  # SHA-1 or SHA-256 object id; a variable keeps ( | ) out of the =~ word
        [[ "$_LOCAL_HEAD" =~ $_OID_RE ]] && [ "$_PR_HEAD" = "$_LOCAL_HEAD" ] && _DELIVERY="verified at $_PR_HEAD"
    fi
    printf 'PR_HANDOFF=PR #%s %s — %s\n' "$_PR_NUMBER" "$_URL" "$_DELIVERY"
fi
case "$PUSH_STATUS" in blocked-guard|blocked-permission|blocked-needs-manual-push|rejected-remote) echo "PUSH_UNBLOCK=$_IMPL_DIR/push-unblock.txt"; cat "$_IMPL_DIR/push-unblock.txt" 2>/dev/null || echo "⚠ push-unblock.txt missing" ;; esac
case "$PUSH_STATUS" in rejected-remote) echo "PUSH_ERROR:"; cat "$_IMPL_DIR/push-error.txt" 2>/dev/null || echo "⚠ push-error.txt missing" ;; esac
IFS= read -r INTAKE_DEGRADED < "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" 2>/dev/null || INTAKE_DEGRADED=""
[ -z "$INTAKE_DEGRADED" ] || { echo "INTAKE=degraded $INTAKE_DEGRADED"; cat "${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}" 2>/dev/null || echo "⚠ intake gap list missing"; }
IFS= read -r _C1_QUOTA < "$_IMPL_DIR/c1-quota-blocked.txt" 2>/dev/null && echo "C1 $_C1_QUOTA"
find "$_IMPL_DIR" -maxdepth 1 -name 'c1-quota-partial-*.patch' 2>/dev/null | sed 's/^/C1_PARTIAL_PATCH=/'  # timeout: 5000
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
cat "$_OSS_RESOLVE/templates/resolve-report.md"  # timeout: 5000
```

Report template (loaded above) — use for section structure. Its `### Push` section shows the printed `PUSH_STATUS` through the template's status table, never a prose recollection of Step 10. Its `### Review Intake` section appears only when the block printed `INTAKE=degraded`, built from the gap JSON printed beside it, never from recollection of the triage. More than 50 items selected → the report's summary carries Step 3d's large-selection line.

`skipped-by-user` records the user's own answer (Step 3d "don't push" or Step 10 "Hold"): report it as that decision, never as a failure, a defect or a Confidence gap.

**Report file — required.** Step 9 left an incomplete stub at `<run dir>/resolve-report.md` the moment the run dir existed, and Step 10 updated it. This block names the run dir (minting one when Step 9 never ran), marks the stub as having reached Step 11 and prints `RESOLVE_REPORT=<path>` — the path the full report is written to below:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="unknown"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/resolve-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || RUN_DIR=".reports/resolve/$(date -u +%Y-%m-%dT%H-%M-%SZ)"  # Step 9 never ran: this run's dir starts here
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/resolve-run-dir-${CSID}"
python "$PLUGIN_ROOT/bin/resolve_report_stub.py" mark --run-dir "$RUN_DIR" --pr "$_PR_NUMBER" --impl-dir "$_IMPL_DIR" --step "Step 11 — final report being written (push status $PUSH_STATUS)"  # timeout: 5000
# gap ledger identity keyed on the head this run started from, so a rerun on the same head carries its open gaps
IFS= read -r _PR_URL < "${TMPDIR:-/tmp}/resolve-pr-html-url-${CSID}" 2>/dev/null || _PR_URL=""
IFS= read -r _START_HEAD < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || _START_HEAD=""
[ -n "$_START_HEAD" ] || IFS= read -r _START_HEAD < "${TMPDIR:-/tmp}/resolve-saved-sha-${CSID}" 2>/dev/null || _START_HEAD=""
python "$PLUGIN_ROOT/bin/gap_emit.py" prepare --kind resolve --dir "$RUN_DIR" --plugin-root "$PLUGIN_ROOT" --pr "$_PR_NUMBER" --pr-url "$_PR_URL" --head "$_START_HEAD"  # timeout: 15000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
cat "$_OSS_SHARED/gap-emission.md" 2>/dev/null || echo "⚠ gap-emission.md absent — record no gap ledger this run"
```

**Remote branch and run leftovers** — read once here, before the worktree exit below, so both lines describe the checkout that holds the run's commits:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="unknown"
IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-remote-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
IFS= read -r START_HEAD < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || START_HEAD=""
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
covered_by_bounds() {
    local _COUNT
    _COUNT=$(git rev-list --count "$1" --not "$START_HEAD" "$2" ${3:+"$3"}) && [ "$_COUNT" = 0 ]
}

base_extends_bounds() {
    local _COMMON _SHARED
    git merge-base --is-ancestor "$2" HEAD || return 1
    [ -z "$3" ] || git merge-base --is-ancestor "$3" "$2" || return 1
    # Only advance the exclusion if the new base carries none of the run's earlier work.
    _SHARED=$(git merge-base --all "$1" "$2") || return 1
    while IFS= read -r _COMMON; do
        covered_by_bounds "$_COMMON" "$4" "$3" || return 1
    done <<< "$_SHARED"
}

read_remote_bounds() {
    local _K _BEFORE _AFTER _EXTRA _PENDING="" _BASE="" _PULL="$START_HEAD"
    local _OID_RE='^([0-9a-f]{40}|[0-9a-f]{64})$'
    [[ "$START_HEAD" =~ $_OID_RE ]] || return 1
    while read -r _K _BEFORE _AFTER _EXTRA; do
        [[ "$_BEFORE" =~ $_OID_RE && "$_AFTER" =~ $_OID_RE && -z "$_EXTRA" ]] || return 1
        [ -z "$_PENDING" ] || [ "$_K" = pull-end ] || return 1
        case "$_K" in
            pull-start)
                [ "$_BEFORE" = "$_AFTER" ] || return 1
                _PENDING="$_BEFORE"
                ;;
            pull-end)
                [ "$_PENDING" = "$_BEFORE" ] || return 1
                _PENDING=""
                [ "$_BEFORE" != "$_AFTER" ] || continue
                # A whole-tip exclusion would also erase any run commits before this pull.
                git merge-base --is-ancestor "$_BEFORE" "$_AFTER" \
                    && covered_by_bounds "$_BEFORE" "$_PULL" "$_BASE" || return 1
                _PULL="$_AFTER"
                ;;
            base)
                [ "$_BASE" != "$_AFTER" ] || continue
                base_extends_bounds "$_BEFORE" "$_AFTER" "$_BASE" "$_PULL" || return 1
                _BASE="$_AFTER"
                ;;
            *) return 1 ;;
        esac
    done
    [ -z "$_K$_BEFORE$_AFTER$_EXTRA$_PENDING" ] && [ -n "$_BASE" ] && printf '%s %s\n' "$_BASE" "$_PULL"
}

_BOUNDS=""
_IMPORTS="${TMPDIR:-/tmp}/resolve-remote-imports-${CSID}"
if [ -s "$_IMPORTS" ]; then
    _BOUNDS=$(read_remote_bounds < "$_IMPORTS") || _BOUNDS=""
fi
read -r BASE_HEAD POST_PULL_HEAD <<< "$_BOUNDS"
python "$PLUGIN_ROOT/bin/resolve_remote_state.py" --push-status "$PUSH_STATUS" --remote "$FORK_REMOTE" --branch "$HEAD_REF" --start-head "$START_HEAD" --imports-file "$_IMPORTS"  # timeout: 45000
python "$PLUGIN_ROOT/bin/resolve_leftovers.py" --impl-dir "$_IMPL_DIR" --stash-before "${TMPDIR:-/tmp}/resolve-stash-before-${CSID}"  # timeout: 20000
[ ! -s "$_IMPL_DIR/group-replay-fallbacks.txt" ] || sed 's/^/GROUP_REPLAY_FALLBACK=/' "$_IMPL_DIR/group-replay-fallbacks.txt"
```

- `REMOTE_STATE` other than `n/a` → one `**Remote branch**:` line under `### Push`, after the status line. Use the printed `REMOTE_DETAIL` to give the observed head and distinguish all, some, or none of the run's commits. `pushed-externally` can mean all or part of the run. `moved` means the run made no commits, `absent` means the branch is missing, and `unknown` means the comparison was inconclusive. Attribute no branch movement to a specific actor or cause.
- Missing import bounds leave commit delivery unverified when the remote moved and requires an ancestry comparison. A fast-forward that includes earlier run commits cannot be used as a whole-tip exclusion.
- `LEFTOVER_BRANCH`, `LEFTOVER_WORKTREE` and `LEFTOVER_STASH` → list each entry under `### Run leftovers`, followed by every `CLEANUP=` command verbatim in one fenced block, in the printed order, and the `STASH_DROP=` text when present. Force-delete suggestions require branch ancestry or identical content on the paths changed since the merge base. An unproven branch may hold unique work. Its `git cherry -v HEAD` output is diagnostic and may omit merge resolutions. The user decides whether to run the listed commands. A stash is named by commit id and only known to have appeared during the run.
- `LEFTOVER_CHECK=incomplete` → include the printed reason under `### Run leftovers` even if `LEFTOVERS=none`. Otherwise `LEFTOVERS=none` omits the section. `STASH_CHECK=unavailable` → say stashes were not compared.

In the final user-facing reply, repeat the PR number and canonical link from `PR_HANDOFF`. For a missing link, reuse this PR's observed metadata URL or validated report link when available. Name a successful push with missing or different head evidence as pushed, PR update unverified. Give the recorded reason for a failed, blocked, declined or unattempted push and summarize what the remote comparison verified. Identify staged changes as local even when a separate merge commit was pushed. Missing push evidence means status unknown. For runs without a PR, identify the local checkout and result.

Codex quota stop: a printed `C1 codex-blocked:quota item=<id>` → the summary carries one `codex-blocked:quota` line:

- Codex quota exhausted at that item; no further C1 call this run.
- Later C1 candidates ran through Phase 1+2.
- Shown as its own outcome, never as a no-op or a failure.
- Each printed `C1_PARTIAL_PATCH=<path>` goes on that line too: the stopped call's partial edit, kept outside the tree.

A printed `C1 codex-blocked:quota source=challenger` → the same `codex-blocked:quota` line, stating instead:

- A challenger's own Codex cross-check hit the quota stop; no C1 call ran after it.
- C1 candidates not done by then ran through Phase 1+2; later challengers skipped their Codex cross-check.
- Shown as its own outcome, never as a no-op or a failure.

**Tell the review what happened.** When this run consumed a review report, append one outcome per review-sourced item to `resolution.jsonl` beside that report.

- Each outcome: `fixed` / `self-resolved` / `rejected` / `skipped` / `pending`, finding title and location, commit hash, reason.
- Grouped `fixed` and `self-resolved` outcomes require item proof from replay against the current checkout; an item absent from that proof stays `pending`. Other commit modes retain their existing Phase 2, Codex-direct and per-item commit evidence.
- The next `/oss:review` of this PR reads the ledger, confirms earlier fixes at the new head and re-checks rejected findings before deciding whether to report them again.
- Append-only and idempotent per run; the script builds records from this run's own files, never from recollection:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || BASE_SHA=""
IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || COMMIT_MODE="unset"
if [ -n "$REPORT_FILE" ] && [ -f "$REPORT_FILE" ] && [ -f "$IMPL_DIR/action-items.jsonl" ]; then
    python "$PLUGIN_ROOT/bin/append_resolution.py" --impl-dir "$IMPL_DIR" --review-dir "$(dirname "$REPORT_FILE")" --base-sha "$BASE_SHA" --commit-mode "$COMMIT_MODE"  # timeout: 30000
else
    echo "→ no review report consumed — resolution ledger skipped"
fi
```

**A push the user approved that failed.** Applies when `PUSH_STATUS` is `rejected-remote` or `rejected-non-ff`.

- The opening one-line summary names the attempt (`… push failed`) and states the observed delivery outcome when available.
- In `### Push`, use `Push rejected (non-fast-forward)` for `rejected-non-ff` and `Push approved but failed` with the printed `PUSH_ERROR` verbatim for `rejected-remote`. These replace the template's rejection lines. Put branch contents and any recovery action after the attempt status, using `REMOTE_STATE` and `REMOTE_DETAIL`.
- Apply no delivery deduction when the observed branch contains all run commits. For partial delivery, base the deduction on the missing commits. If the branch is absent or the observation proves none of the run's commits reached it, record that observed delivery gap.
- With `unknown` or missing comparison evidence, report delivery as unverified and base any deduction on the verification gap. A rejection alone gives no evidence of branch contents.

**Unblock push — last actionable item.** Applies to `blocked-guard`, `blocked-permission`, and `blocked-needs-manual-push`. For `rejected-remote`, include it only when remote evidence shows missing commits or delivery remains unverified. Omit it when all run commits are verified on the remote.

- The block above `cat`s the `PUSH_UNBLOCK` file; end the report — after `**Next**`, the Challenge Log, Confidence, and every other section — with a `## Unblock push` section that repeats its lines verbatim in a fenced block.
- The user returns to the bottom of the report and runs exactly those lines; never paraphrase, reorder, or regenerate them.
- File missing or empty → print `⚠ push-unblock.txt missing — scroll to Step 10 for the guard's exact lines` in that section instead.

Immediately before printing it — the one standalone bookkeeping call, so a compaction mid-report cannot leave the run `in_progress` (`rules/task-lifecycle.md` §TaskUpdate before long output):

```text
TaskUpdate(task_id=TASK_CLOSE, status="completed")
```

**Write the final report to the printed `RESOLVE_REPORT` path with the Write tool, then print it — including the full Action Items resolution table — inline to terminal.** Both are required: the file replaces the incomplete stub, so the run dir records how the run ended; the terminal copy is what the user acts on. The path already holds the stub, and the Write tool refuses to overwrite a file not read in this session: Read the stub at `RESOLVE_REPORT` with the Read tool first, then Write over it.

> **Output-Routing exemption (canonical)**: the Step 11 final report is a read-in-context, acted-on-immediately resolution summary the user must see to confirm every item and how it resolved.
>
> - Always print the full Action Items table inline to terminal regardless of row count — this is the whole point of the report.
> - Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply; never divert this table to a file in place of showing it.
> - The durable copy at `RESOLVE_REPORT` is required as well, never instead of the inline print; the inline print is never replaced by a prose summary.

**Gap ledger** (`gap-emission.md`, printed by the Report file block): after the report, Write each gap of its `## Confidence` **Gaps** to the printed `GAP_STAGE` path (`<run dir>/gaps.jsonl.rec`) with the Write tool, gap fields only, never a `GAP_CARRIED` id again; no gaps → stage nothing. The block below appends it.

Then confirm the file on disk is the final report, not the stub, and append the staged gaps:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/resolve-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
[ -n "$_RUN_DIR" ] || { echo "REPORT_STATE=missing (no run dir recorded — run the Report file block above)"; exit 0; }
python "$PLUGIN_ROOT/bin/resolve_report_stub.py" status --run-dir "$_RUN_DIR"  # timeout: 5000
python "$PLUGIN_ROOT/bin/gap_emit.py" append --dir "$_RUN_DIR"  # timeout: 15000
```

`REPORT_STATE` → what to do; the block asks nothing and never stops the run:

- `final` → continue.
- `incomplete` → Read the printed `RESOLVE_REPORT` path, Write the report over it and run this block once more.
- `missing` → Write the report to the printed `RESOLVE_REPORT` path and run this block once more.
- `unreadable` → the path exists but cannot be read (a directory, no permission): treat it as `missing` and try the Write once; refused → print `⚠ final report not saved: <RESOLVE_REPORT> unreadable` and add one Confidence line `final report not saved: <RESOLVE_REPORT> unreadable`, then continue — the inline copy still carries the report.

`GAP_LEDGER=refused` → correct the named records in the staged file once and rerun the block; refused again → one Confidence line `gap ledger refused: <first error>`, continue.

**GitHub writes for you.** An item only a GitHub write can close (a PR description edit, a review-thread resolution, a code-scanning alert dismissal, a reply) is never done by this run and never asked about here. List each one in a `### GitHub writes for you` section: the item and what the write does, one table row each. Every write that GraphQL can carry (thread replies and resolutions, comments) goes into ONE combined `gh api graphql -f query='mutation { … }'` command printed under the table, ready for the user's own `gh-write` approval as a single approval (`public-github.md` §One approval per request); never one command per item or per PR. Long text that needs `--body-file` goes to `<run dir>/gh-write-<id>.md` with the Write tool and keeps its own command. In the Action Items table such an item keeps `⊘` / `skipped`, never a failure; the section carries the handoff.

**Grouped-commit fallbacks.** Every `GROUP_REPLAY_FALLBACK=` line the remote-branch block printed goes into the Action Items summary as "committed with its group as a whole file".

**Action Items table** — one row per selected item, columns: `#` | `Type` | `Change` | `Status` | `Resolution` | `Commit`:

- `Status`: ✓ implemented · ⊘ skipped · ✗ challenge-rejected
- `Resolution`: `implemented` · `self-resolved` (challenger provided alternative) · `skipped` · `challenge-rejected`
- `Change`: action type — `code` / `test` / `docs` / `config` / `ci` / `style` / `refactor` / `perf` / `architecture`
- `Commit`: short SHA (7 chars); `—` when `COMMIT_MODE=stage`
- For `location: discussion` rows append `· thread (no GH resolve)` to Status — no GitHub Resolve button exists for PR main-thread comments

Include `### Challenge Log` section in report.

- Source of truth is `$IMPL_DIR/challenge-log.txt` (Read it; one `key=value` record per line, written by the shared append block in `dispatch-prelude.md`, called by C1 and Phase 1), never a recollection of verdicts from earlier in the conversation.
- File absent or empty (Step 8 skipped, skip-all, or `--no-challenge`) → omit the section; never treat the failed Read as an error.
- Columns: `#` | `Finding` | `Evidence` | `Resolution` — no `Suggestion` column: `Resolution` is the outcome and already names what was done with the suggestion, and `suggestion_why` stays in `challenge-log.txt`.

Every cell must be self-contained — reader gets full context from that row alone, never by cross-referencing another row or recalling earlier conversation:

- `Finding`: one-line gist of the reviewer's comment (from `finding` in `CHALLENGE_LOG`) — what was flagged, not just its id
- `Evidence`: bracketed flag + reason on one line, e.g. `[VALID] — <evidence_why>` or `[REJECT] — <evidence_why>`. Reason never empty, never generic — state in a few words what the verdict was about. Never print a bare `VALID`/`REJECT`, bracketed or not, with no reason
- `Resolution`: concrete outcome, never a bare label.
  - `detail=pending-impl:<id>` → backfill before printing: look up that id's `Commit` SHA in the Action Items table above and run `git log -1 --format=%s <sha>` for the one-line summary of what changed; render as `as-suggested: <that summary>`.
  - `detail=<alternative text>` (self-resolved rows) → render as `self-resolved: <alternative text>`.
  - `detail=<evidence_why>` (rejected rows) → render as `rejected: <evidence_why>`.
  - If a commit lookup fails, state `as-suggested: (commit summary unavailable, see commit <sha>)` — never fall back to printing the bare word `as-suggested` alone

Omit section when `--no-challenge`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SAVED_BRANCH < "${TMPDIR:-/tmp}/resolve-saved-branch-${CSID}" 2>/dev/null || SAVED_BRANCH=""
case "$SAVED_BRANCH" in HEAD) SAVED_BRANCH="" ;; esac
IFS= read -r SAVED_SHA < "${TMPDIR:-/tmp}/resolve-saved-sha-${CSID}" 2>/dev/null || SAVED_SHA="unknown"
IFS= read -r COMMIT_MODE < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || COMMIT_MODE="unset"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="none"
# stage mode: skip restore, else staged work lost
if [ "$COMMIT_MODE" = "stage" ]; then
    echo "⚠ COMMIT_MODE=stage: changes are staged on $(git branch --show-current) — caller restore skipped to preserve staged work (saved HEAD=$SAVED_SHA). Commit on this PR branch first; then restore the caller's branch or detached HEAD."
# Keep the checkout available for review when this run did not push.
elif [ -n "$SAVED_BRANCH" ] && [ "$PUSH_STATUS" != "pushed" ]; then
    echo "→ Staying on $(git branch --show-current) (PUSH_STATUS=$PUSH_STATUS). Return later with: git switch $SAVED_BRANCH"
elif [ -n "$SAVED_BRANCH" ]; then
    if git switch "$SAVED_BRANCH" 2>/dev/null; then echo "→ Restored to $SAVED_BRANCH"; else echo "⚠ Could not restore $SAVED_BRANCH — staying on $(git branch --show-current); saved caller HEAD=$SAVED_SHA"; fi  # timeout: 5000
else
    echo "⚠ Caller had detached HEAD at $SAVED_SHA — staying on $(git branch --show-current); restore that saved commit manually when ready"
fi
```

**Branch after the run** — the block above returns to `SAVED_BRANCH` only when `PUSH_STATUS=pushed`. Other attempt statuses leave the session on the PR branch for inspection. The block prints how to switch back.

**Worktree exit** — if `WT_ENABLED=true` and a worktree was entered at Step 4, follow `worktree-isolation.md` §Exit — `git branch --show-current`, then `ExitWorktree(action="keep")` to return the session to the main tree. Append the `Worktree` block with the path to any local work. Describe the worktree as disposable only when the push was verified and all changes were committed. The `SAVED_BRANCH` restore above was a no-op — the main tree was never switched. Never auto-merge.

Post-PR action — already answered by the Step 3d push question; no new `AskUserQuestion` here (after a run approval it stays `skip`; the report's **Link** line is the way to the PR):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r POST_PR_ACTION < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || POST_PR_ACTION="skip"
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
# n/a is what Step 1 stores when no PR# was parsed; never hand it to gh
[ "$POST_PR_ACTION" = "open" ] && [ "$_PR_NUMBER" != "n/a" ] && [ -n "$_PR_NUMBER" ] && gh pr view "$_PR_NUMBER" --web  # timeout: 10000
echo "POST_PR_ACTION=$POST_PR_ACTION"
```

The post-PR sentinel needs no cleanup — Step 1 re-initialises it to `skip` on every run, so nothing stale survives into the next invocation. Clear the compaction contract alone (its own fence: the `rm` keeps the block out of the blueprint manifest, and this exact path is allow-listed):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
rm -f .temp/state/skill-contract.md "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" "${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}" "${TMPDIR:-/tmp}/resolve-intake-rerun-${CSID}" "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" "${TMPDIR:-/tmp}/resolve-gate-out-${CSID}"  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```
