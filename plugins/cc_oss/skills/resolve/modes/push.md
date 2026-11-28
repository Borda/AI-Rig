<!-- oss:resolve Step 10 — executed inline: cat $_OSS_RESOLVE/modes/push.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 10) -->

## Step 10: Push

**Push intent from Step 3d, confirmation here — with the full scope.** One call reads the recorded Step 3d intent back and computes the push scope the confirmation must show:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PUSH_AUTH < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || PUSH_AUTH="unset"
echo "PUSH_AUTH=$PUSH_AUTH"
# the push question can idle for hours: the run dir's report names this step meanwhile
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/resolve-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
[ -z "$_RUN_DIR" ] || python "$PLUGIN_ROOT/bin/resolve_report_stub.py" mark --run-dir "$_RUN_DIR" --pr "$_PR_NUMBER" --impl-dir "$_IMPL_DIR" --step "Step 10 — push step reached (push intent $PUSH_AUTH)"  # timeout: 5000
# fresh shell (Check 43) — unbound here → empty push scope
IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-remote-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
# renamed fork: remote URL needs headRepository.name; empty → upstream name
IFS= read -r FORK_REPO < "${TMPDIR:-/tmp}/resolve-head-repo-name-${CSID}" 2>/dev/null || FORK_REPO=""
# --worktree: an approval is bound to the checkout it was asked from, so the push must name the worktree (`git -C <path> push …`)
IFS= read -r _WTON < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null || _WTON=false
_WTF="${TMPDIR:-/tmp}/resolve-push-worktree-${CSID}"
echo none > "$_WTF"
if [ "$_WTON" = true ]; then
    git rev-parse --show-toplevel > "$_WTF.top"
    IFS= read -r _WT < "$_WTF.top" 2>/dev/null || _WT=""
    case "$_WT" in ''|*[!A-Za-z0-9_./:-]*) _WT=unusable ;; /*|[A-Za-z]:/*) ;; *) _WT=unusable ;; esac
    echo "$_WT" > "$_WTF"
fi
printf 'WT_PUSH='; cat "$_WTF"
# target may have moved since Step 9 (push question idles for hours); runs first so derive_fork_remote's exit stays the block's
python "$PLUGIN_ROOT/bin/check_base_fresh.py" --base-ref "$BASE_REF"  # timeout: 90000
python "$PLUGIN_ROOT/bin/derive_fork_remote.py" --fork-remote "$FORK_REMOTE" --head-ref "$HEAD_REF" --base-ref "$BASE_REF" --fork-repo "$FORK_REPO"  # timeout: 10000
```

The block exits non-zero (`! BLOCKED` — fork remote or head ref unresolved, push scope not computable) → never push and never ask about an unknown scope; record `not-attempted` (**Record the push outcome** below) and continue to Step 11. If the current local branch differs from the remote `HEAD_REF`, use the explicit-refspec manual-push hand-off below without attempting plain `git push`; the alias must never become a new remote branch. Otherwise note the printed `BASE_FRESH` for the push confirmation and the printed `WT_PUSH`, then branch on the printed `PUSH_AUTH`.

**`WT_PUSH`** — the push spelling for the rest of this step. `none` → `git push …` as written below. A path → the run works in the session worktree, so every push, push question and guard-refusal retry below is written `git -C <WT_PUSH> push <remote> <branch>` (the path as literal words, unquoted) and each question's first line reads `Push <branch> from worktree <WT_PUSH> (not this session's checkout) to <remote>?`; a plain `git push` there is refused by the guard, and the approval from Step 3d covers only that spelling. `unusable` → never push and never ask, since no approval can cover that path: use the explicit-refspec manual-push hand-off below and record `blocked-needs-manual-push`, unless `PUSH_AUTH` is `skip`.

Branch on the printed `PUSH_AUTH`:

- `skip` → the user explicitly chose "don't push" at Step 3d: print `` → Push skipped (Step 3d answer) — run `git push` manually when ready. ``, record `skipped-by-user`, and jump to Step 11; the post-PR answer still applies there. No question.
- `push-run` → the user approved this push at Step 3d with the `git-push-run` question, so no push question here — never ask the user again to push. Print the scope that question could not show, `→ Pushing $PUSH_COUNT commits ($PUSH_STAT) to $FORK_REMOTE/$HEAD_REF — last: "$LAST_SUBJECT"`, with the drift line beside it when `BASE_FRESH` is not `yes`; with `BASE_FRESH=no` ask the **drift question** below first (an unanswered one means no push), then run the **run push** below. Q1 is asked only if the guard refuses that push (**Blocked by a push guard**).
- `push` → ask the **push confirmation** below — Q1 only; the post-PR action was already answered at Step 3d.
- anything else (`unset` — Step 3d's push question went unanswered or was dismissed, or no answer was ever recorded, e.g. a resume in a fresh session) → ask the **push confirmation** with both Q1 and Q2 in one call, as before the intent question existed. A missing answer is never a skip and never authorization.

<!-- branch: main-path — push confirmation (intent push or no recorded intent; skipped only on an explicit Step 3d "don't push" or an uncomputable scope) -->

**Drift question — its own `AskUserQuestion` call, `BASE_FRESH=no` only, before the push confirmation and never in the same call.** The target branch moved since Step 9's merge. Question text: the drift line below, then the `/compact` hint line. Options:

- (a) **Re-sync target first** — run Step 9 again from its drift gate (9.0 re-merges, then lint/QA), then return to the top of Step 10 with the new scope. Not counted against 9.0's 2-re-sync cap — the user chose it.
- (b) **Continue to the push** — keep the current merge; the push confirmation (or, after a Step 3d run approval, the scope line) shows the drift line.

Unanswered → (b) when the push still needs its own `Approve` (`PUSH_AUTH` `push` or `unset`). `PUSH_AUTH=push-run` and unanswered → no push: the user never saw this drift and the run approval did not name it; record `not-attempted`, print `` → Push not attempted — the target moved and the drift question went unanswered; run `git push` manually when ready. ``, and continue to Step 11. `BASE_FRESH` `yes` or `unknown` → no drift question.

**Push confirmation — one `AskUserQuestion` call.** When Step 10 needs a separate confirmation, ask the `git-push` question from `git-commit.md` before running the push. The user's `Approve` lets foundry's approval hook record a single-use token for this branch at this HEAD to the remote shown in the question. Second-longest idle window (measured up to 11 h). Put the `/compact` hint inside Q1 just before the final ask. The Boundary-2 contract already names every file Step 11 needs, and 5.5-family models may return reply text written before a tool call as an empty progress update.

Q1 — push: header `git-push`, `multiSelect` false. Render these context lines in Q1, in order, before the final ask.

- Target remote and branch: `$FORK_REMOTE/$HEAD_REF`
- HEAD read with `git rev-parse HEAD` in the checkout being pushed, using `git -C <WT_PUSH>` for a worktree
- Diff stat: `$PUSH_STAT` (e.g. `3 files changed, 47 insertions(+), 12 deletions(-)`)
- Commit count and last subject: `$PUSH_COUNT commits — last: "$LAST_SUBJECT"`
- Target drift, only when `BASE_FRESH` is not `yes`: `no` → `⚠ origin/<BASE_REF> advanced <BASE_BEHIND> commits since the last merge — newest: "<first listed subject>"`; `unknown` → `⚠ could not verify origin/<BASE_REF> is merged`
- The command it approves: `` `git push` ``, or with a `WT_PUSH` path `` `git -C <WT_PUSH> push <FORK_REMOTE> <HEAD_REF>` `` (its branch, HEAD and commits read with `git -C <WT_PUSH>`)
- Not covered: force, delete, tags, other refs, other remotes, URLs.
- Last line: `` Long wait? `/compact` now — commits landed, challenge log + item map in <IMPL_DIR>, resume lossless. ``

Leave one blank line after the context and end Q1 with `Push {branch} to {configured remote name}?` using the literal branch and remote. The guard reads the approved remote from that final question line for a plain `git push`.

Options: exactly these two labels.

- **Approve** — this push, once. Use the option description `one push of this branch at this HEAD to this remote; token valid 15 minutes; up to eight pending approvals per scope`.
- **Decline** — skip this push and cancel only a pending approval for this same question in this session.

`PUSH_AUTH=skip` with `BASE_FRESH=no` → still print the drift line beside the skip message, so the manual push is not made blind.

Q2 — `unset` intent only — after the final report: (a) **Open PR in browser** (`gh pr view <PR_NUMBER> --web`) · (b) **Skip**. Run the matching Step 3d post-PR block (`open` or `skip`) — never edit a block's value.

Only proceed to the `git push` below on Q1 `Approve`. On `Decline`: print `` → Push skipped — run `git push` manually when ready. ``, record `skipped-by-user`, and jump to Step 11 (the post-PR answer still applies there). Unanswered Q1 is never authorization: treat it as `Decline`. Never create, copy or edit the push token yourself.

<!-- policy-sibling: plugins/CLAUDE.md §Blueprint Blocks (canonical), plugins/cc_foundry/agents/challenger.md, plugins/cc_oss/skills/resolve/modes/item-selection.md (Step 3d), plugins/cc_oss/skills/resolve/modes/push.md (Step 10), plugins/cc_oss/skills/review/modes/acceptance-gate.md (reject gate) -->

```bash
git push # timeout: 30000
```

With a `WT_PUSH` path, the push after the Q1 `Approve` is exactly the span the question showed, the printed values as literal words:

```bash
git -C <WT_PUSH> push <FORK_REMOTE> <HEAD_REF> # timeout: 30000
```

**Run push** (`PUSH_AUTH=push-run`) — the guard covers a push that writes its remote and branch out, so the command carries the printed `$FORK_REMOTE` and `$HEAD_REF` as literal words, never as shell variables; the question named exactly this push:

```bash
git push <FORK_REMOTE> <HEAD_REF> # timeout: 30000
```

With a `WT_PUSH` path (the Step 3d question named the worktree):

```bash
git -C <WT_PUSH> push <FORK_REMOTE> <HEAD_REF> # timeout: 30000
```

Any other spelling is refused by the guard. Unsubstituted `<…>` text is a redirection the guard refuses as well; never run it.

An authorized push still stops on its own failures — never retried in a loop, never forced, never re-asked. The push guard and the absent `git push` allow rule are deliberate user safety controls: never weaken, bypass, or work around either, and never create, touch, or edit a guard's authorization file yourself.

- **Blocked by a push guard on the run push** (`PUSH_AUTH=push-run`: the token expired, the session restarted, or HEAD is not on the line of the PR head the question named) → print the guard's reason verbatim and ask the **push confirmation** (Q1, `git-push`) once; on `Approve` push with plain `git push`, or the `git -C <WT_PUSH>` spelling when `WT_PUSH` is a path; any further guard refusal follows the next bullet. This is the only path where a `push-run` run asks about pushing at Step 10.
- **Blocked by a push guard** (a hook rejects the call and asks for the `git-push` question: no `Approve` on record, the token expired, or HEAD moved since the answer) → print the guard's reason verbatim, do not retry, record `blocked-guard`, and write the push command it refused — copied character for character, no paraphrase — with the Write tool to `$IMPL_DIR/push-unblock.txt`, so the user can run it from their own shell or approve a new `git-push` question in a later run. Continue to Step 11.
- **Blocked by a denied permission** (the harness permission prompt for `git push` was refused) → do not retry, record `blocked-permission`, and write the exact push command that was denied (`git push`) with the Write tool to `$IMPL_DIR/push-unblock.txt`. Continue to Step 11.
- **Rejected as non-fast-forward** (the PR branch moved on the remote) → no force and no retry. Print `! BLOCKED — Push rejected (non-fast-forward) — the PR branch moved; merge the remote branch, then push manually.`, record `rejected-non-ff`, and continue to Step 11.
- **Rejected by the remote or transport** (403, permission denied, expired authentication, network failure, a server error such as HTTP 500, or another remote error) → the push was asked and approved, so this is a failed push, never a hand-off. Print the error, do not retry, record `rejected-remote`, write git's decisive error line (the `remote:` or `error:` line, verbatim) to `$IMPL_DIR/push-error.txt` and the exact push command that failed, copied character for character, to `$IMPL_DIR/push-unblock.txt`, both with the Write tool. Continue to Step 11, whose report flags it.
- **No upstream or wrong tracking** (plain `git push` cannot resolve its destination) → the manual-push hand-off below; the agent never runs a second push.
- **Push lands** → record `pushed` after the verification block below.

Push failed for lack of upstream tracking → manual-push hand-off. The explicit-refspec push names its remote and ref through run-time values, which a push approval never covers (it covers one push of this branch, remote and branch written out), so the user runs it by hand: no second push question, no agent push, none from a spawned agent either. The block prints the command with its values filled in:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""
IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-remote-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""
# empty refspec → the handed-over command would push to the wrong ref
[ -n "$FORK_REMOTE" ] && [ -n "$HEAD_REF" ] || { echo "! BLOCKED — Step 10 fallback: FORK_REMOTE/HEAD_REF unresolved — refusing explicit-refspec hand-off"; exit 1; }
printf 'git push %s HEAD:refs/heads/%s\n' "$FORK_REMOTE" "$HEAD_REF"  # timeout: 3000
```

The block exits non-zero → record `not-attempted`. Otherwise print `→ Push needs your shell — local alias, remote failure or unavailable tracking; run: <printed line>`, write the printed line — copied character for character — with the Write tool to `$IMPL_DIR/push-unblock.txt`, record `blocked-needs-manual-push`, and continue to Step 11.

Verify push reached GitHub — the PR head oid must equal local HEAD (headlines alone pass on matching subjects pushed to another ref); the last headlines are printed for the report:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "! BLOCKED — Step 10 PR number sentinel missing or invalid; cannot verify push"; exit 1 ;; esac
IFS= read -r PR_HTML_URL < "${TMPDIR:-/tmp}/resolve-pr-html-url-${CSID}" 2>/dev/null || PR_HTML_URL=""
_PR_HEAD_FILE="${TMPDIR:-/tmp}/resolve-pr-head-${CSID}"
_LOCAL_HEAD_FILE="${TMPDIR:-/tmp}/resolve-local-head-${CSID}"
gh pr view "${PR_HTML_URL:-$PR_NUMBER}" --json headRefOid,commits --jq '.headRefOid, (.commits[-3:] | .[].messageHeadline)' > "$_PR_HEAD_FILE" || : > "$_PR_HEAD_FILE"  # timeout: 6000
git rev-parse HEAD > "$_LOCAL_HEAD_FILE" || : > "$_LOCAL_HEAD_FILE"  # timeout: 3000
IFS= read -r _LOCAL_HEAD < "$_LOCAL_HEAD_FILE" || _LOCAL_HEAD=""
IFS= read -r _PR_HEAD < "$_PR_HEAD_FILE" || _PR_HEAD=""
_OID_RE='^([0-9a-f]{40}|[0-9a-f]{64})$'  # SHA-1 or SHA-256 object id; a variable keeps ( | ) out of the =~ word
[[ "$_LOCAL_HEAD" =~ $_OID_RE ]] && [ "$_PR_HEAD" = "$_LOCAL_HEAD" ] && echo "→ push verified: PR head equals local HEAD" || echo "⚠ push unverified: PR head differs or a head read was unavailable"
tail -n +2 "$_PR_HEAD_FILE"
```

Unverified → re-run the block once (GitHub lags a few seconds); still unverified → record the outcome below as usual (git exit 0 stands) but name `PR head <oid> ≠ local HEAD` under the final report's Confidence Gaps, and never claim the PR carries the local commits.

**Record the push outcome** — run **exactly one** block, the one matching what happened above; never edit a block's value (same trap as the Step 3d blocks). Step 11 reports this status, so a run that leaves Step 10 without recording one reports `none`.

`pushed`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo pushed > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`blocked-guard`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo blocked-guard > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`blocked-permission`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo blocked-permission > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`blocked-needs-manual-push`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo blocked-needs-manual-push > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`rejected-non-ff`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo rejected-non-ff > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`rejected-remote`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo rejected-remote > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`skipped-by-user`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo skipped-by-user > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

`not-attempted`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo not-attempted > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # timeout: 3000
```

**Retire the run approval** — `PUSH_AUTH=push-run` only, once the outcome above is recorded and whatever it is. The `git-push-run` token lasts 12 h and is single-use. An unspent token could authorize a later lead push of this branch. The revocation command removes the primary pending run-push token and pending run-push slots in the git common directory across worktrees. It leaves spent claims and the active reservation in place.

```bash
rm -f "$(git rev-parse --git-common-dir)"/claude-push-run-approval.json{,.slot-*}  # timeout: 5000
```
