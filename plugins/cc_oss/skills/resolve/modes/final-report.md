<!-- oss:resolve Step 11 — executed inline: cat $_OSS_RESOLVE/modes/final-report.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 11) -->

## Step 11: Final report

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary3: pre-final-report write (compaction-contract.md §Lifecycle)
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR="n/a"
IFS= read -r PUSH_STATUS < "${TMPDIR:-/tmp}/resolve-push-status-${CSID}" 2>/dev/null || PUSH_STATUS="none"
_PRESERVE="pr=${_PR_NUMBER}, final-report=pending-write, impl-dir=${_IMPL_DIR}, challenge-log=${_IMPL_DIR}/challenge-log.txt, item-tasks=${_IMPL_DIR}/item-tasks.tsv, push-status=${PUSH_STATUS}, push-unblock=${_IMPL_DIR}/push-unblock.txt"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "final-report (after push)" "$_IMPL_DIR" "${_PRESERVE}" "write final report → post-PR action gate"  # timeout: 5000
echo "PUSH_STATUS=$PUSH_STATUS"
case "$PUSH_STATUS" in blocked-guard|blocked-permission|blocked-needs-manual-push) echo "PUSH_UNBLOCK=$_IMPL_DIR/push-unblock.txt"; cat "$_IMPL_DIR/push-unblock.txt" 2>/dev/null || echo "⚠ push-unblock.txt missing" ;; esac
IFS= read -r INTAKE_DEGRADED < "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" 2>/dev/null || INTAKE_DEGRADED=""
[ -z "$INTAKE_DEGRADED" ] || { echo "INTAKE=degraded $INTAKE_DEGRADED"; cat "${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}" 2>/dev/null || echo "⚠ intake gap list missing"; }
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
cat "$_OSS_RESOLVE/templates/resolve-report.md"  # timeout: 5000
```

Report template (loaded above) — use for section structure. Its `### Push` section shows the printed `PUSH_STATUS` through the template's status table, never a prose recollection of Step 10. Its `### Review Intake` section appears only when the block printed `INTAKE=degraded`, built from the gap JSON printed beside it, never from recollection of the triage. More than 50 items selected → the report's summary carries Step 3d's large-selection line.

**Tell the review what happened.** When this run consumed a review report, append one outcome per review-sourced item (`fixed` / `self-resolved` / `rejected` / `skipped` / `pending`, finding title and location, commit hash, reason) to `resolution.jsonl` beside that report. `fixed` and `self-resolved` need an implementation record (Phase 2 commit, Codex-direct record, or a `[resolve No.<id>]` commit after this run's base head); an accepted but unmerged item stays `pending`. The next `/oss:review` of this PR reads the ledger, confirms earlier fixes at the new head and re-checks rejected findings before deciding whether to report them again. Append-only and idempotent per run; the script builds records from this run's own files, never from recollection:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r BASE_SHA < "${TMPDIR:-/tmp}/resolve-base-sha-${CSID}" 2>/dev/null || BASE_SHA=""
if [ -n "$REPORT_FILE" ] && [ -f "$REPORT_FILE" ] && [ -f "$IMPL_DIR/action-items.jsonl" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/append_resolution.py" --impl-dir "$IMPL_DIR" --review-dir "$(dirname "$REPORT_FILE")" --base-sha "$BASE_SHA"  # timeout: 30000
else
    echo "→ no review report consumed — resolution ledger skipped"
fi
```

**Unblock push — last actionable item.** `PUSH_STATUS` is `blocked-guard`, `blocked-permission` or `blocked-needs-manual-push` → the block above `cat`s the `PUSH_UNBLOCK` file; end the report — after `**Next**`, the Challenge Log, Confidence, and every other section — with a `## Unblock push` section that repeats its lines verbatim in a fenced block. The user returns to the bottom of the report and runs exactly those lines; never paraphrase, reorder, or regenerate them. File missing or empty → print `⚠ push-unblock.txt missing — scroll to Step 10 for the guard's exact lines` in that section instead.

Immediately before printing it — the one standalone bookkeeping call, so a compaction mid-report cannot leave the run `in_progress` (`rules/task-lifecycle.md` §TaskUpdate before long output):

```text
TaskUpdate(task_id=TASK_CLOSE, status="completed")
```

**Print the final report — including the full Action Items resolution table — inline to terminal.**

> **Output-Routing exemption (canonical)**: the Step 11 final report is a read-in-context, acted-on-immediately resolution summary the user must see to confirm every item and how it resolved. Always print the full Action Items table inline to terminal regardless of row count — this is the whole point of the report. Global Output Routing (*5+ findings → `.temp/output-*.md`, summary only*) does **not** apply; never divert this table to a file in place of showing it. Writing a durable copy to `.reports/resolve/` in addition is fine, but the inline terminal print is mandatory and never replaced by a prose summary.

**Action Items table** — one row per selected item, columns: `#` | `Type` | `Change` | `Status` | `Resolution` | `Commit`:

- `Status`: ✓ implemented · ⊘ skipped · ✗ challenge-rejected
- `Resolution`: `implemented` · `self-resolved` (challenger provided alternative) · `skipped` · `challenge-rejected`
- `Change`: action type — `code` / `test` / `docs` / `config` / `ci` / `style` / `refactor` / `perf` / `architecture`
- `Commit`: short SHA (7 chars); `—` when `COMMIT_MODE=stage`
- For `location: discussion` rows append `· thread (no GH resolve)` to Status — no GitHub Resolve button exists for PR main-thread comments

Include `### Challenge Log` section in report — source of truth is `$IMPL_DIR/challenge-log.txt` (Read it; one `key=value` record per line, written by the shared append block in `dispatch-prelude.md`, called by C1 and Phase 1), never a recollection of verdicts from earlier in the conversation. File absent or empty (Step 8 skipped, skip-all, or `--no-challenge`) → omit the section; never treat the failed Read as an error. Columns: `#` | `Finding` | `Evidence` | `Resolution` — no `Suggestion` column: `Resolution` is the outcome and already names what was done with the suggestion, and `suggestion_why` stays in `challenge-log.txt`. Every cell must be self-contained — reader gets full context from that row alone, never by cross-referencing another row or recalling earlier conversation:

- `Finding`: one-line gist of the reviewer's comment (from `finding` in `CHALLENGE_LOG`) — what was flagged, not just its id
- `Evidence`: bracketed flag + reason on one line, e.g. `[VALID] — <evidence_why>` or `[REJECT] — <evidence_why>`. Reason never empty, never generic — state in a few words what the verdict was about. Never print a bare `VALID`/`REJECT`, bracketed or not, with no reason
- `Resolution`: concrete outcome, never a bare label. `detail=pending-impl:<id>` → backfill before printing: look up that id's `Commit` SHA in the Action Items table above and run `git log -1 --format=%s <sha>` for the one-line summary of what changed; render as `as-suggested: <that summary>`. `detail=<alternative text>` (self-resolved rows) → render as `self-resolved: <alternative text>`. `detail=<evidence_why>` (rejected rows) → render as `rejected: <evidence_why>`. If a commit lookup fails, state `as-suggested: (commit summary unavailable, see commit <sha>)` — never fall back to printing the bare word `as-suggested` alone

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
# unpushed work (declined, blocked, rejected or never attempted): stay on the branch holding it, for review or a manual push
elif [ -n "$SAVED_BRANCH" ] && [ "$PUSH_STATUS" != "pushed" ]; then
    echo "→ Staying on $(git branch --show-current) — commits not pushed (PUSH_STATUS=$PUSH_STATUS). Return later with: git switch $SAVED_BRANCH"
elif [ -n "$SAVED_BRANCH" ]; then
    if git switch "$SAVED_BRANCH" 2>/dev/null; then echo "→ Restored to $SAVED_BRANCH"; else echo "⚠ Could not restore $SAVED_BRANCH — staying on $(git branch --show-current); saved caller HEAD=$SAVED_SHA"; fi  # timeout: 5000
else
    echo "⚠ Caller had detached HEAD at $SAVED_SHA — staying on $(git branch --show-current); restore that saved commit manually when ready"
fi
```

**Branch after the run** — the block above returns to `SAVED_BRANCH` only when `PUSH_STATUS=pushed`. Any unpushed outcome (push declined, blocked, rejected or never attempted) leaves the session on the PR branch that holds the commits, so the user can inspect them or push by hand; the block prints how to switch back.

**Worktree exit** — if `WT_ENABLED=true` and a worktree was entered at Step 4: when pushed, the commits are on the fork (the deliverable is remote); when not pushed, they exist only in that worktree, so keep it. Follow `worktree-isolation.md` §Exit — `git branch --show-current`, then `ExitWorktree(action="keep")` to return the session to the main tree, and append the `Worktree` block noting the local worktree is disposable (`git worktree remove` when done). The `SAVED_BRANCH` restore above was a no-op — the main tree was never switched. Never auto-merge.

Post-PR action — already answered by the Step 3d push question; no new `AskUserQuestion` here:

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
