<!-- file: preflight.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:resolve -->

## Step 1: Pre-flight

- Capture caller branch first — Step 11 restore needs it even when Step 4 (`gh pr checkout`) skipped or fails mid-checkout. Init here so Step 11 restore path always defined.
- Preflight in `bin/resolve_preflight.py` — checks codex availability, `gh` binary + auth, syncs remote. Caches positive results under `.temp/state/preflight/` (4 h TTL).
- Writes `CODEX_AVAILABLE` and `GH_OK` to `${TMPDIR:-/tmp}/resolve-preflight-*-<CSID>`; status to stderr; exits non-zero only on hard failure (`gh` missing/unauthenticated, `git fetch` failure) — `gh` missing/unauthenticated aborts whole block below, flag parsing never runs.
- Codemap auto-detects here too (auto-on if installed; `--no-codemap` off; `--codemap` strict — stop if missing); merged into this same call since nothing between agent resolution, preflight, and codemap detection depends on a decision made in between.

```bash
# timeout: 45000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# loads: oss-shared-resolver.md
# loads: review-section-taxonomy.md
# loads: compaction-contract.md
_OSS_SHARED=$(python "$PLUGIN_ROOT/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
_OSS_RESOLVE=$(python "$PLUGIN_ROOT/bin/resolve_shared_path.py" oss skills/resolve 2>/dev/null)  # timeout: 5000
[ -z "$_OSS_RESOLVE" ] && _OSS_RESOLVE="plugins/cc_oss/skills/resolve"
echo "$_OSS_SHARED" > "${TMPDIR:-/tmp}/resolve-oss-shared-${CSID}"  # cross-block (Check 43)
echo "$_OSS_RESOLVE" > "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}"
cat "$_OSS_SHARED/agent-resolution.md"  # timeout: 5000

SAVED_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
echo "$SAVED_BRANCH" > "${TMPDIR:-/tmp}/resolve-saved-branch-${CSID}"
SAVED_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
echo "$SAVED_SHA" > "${TMPDIR:-/tmp}/resolve-saved-sha-${CSID}"
[ -n "$SAVED_BRANCH" ] || echo "⚠ Caller has detached HEAD at $SAVED_SHA — saved for recovery; Step 11 will report it instead of switching to an unnamed branch"
python "$PLUGIN_ROOT/bin/resolve_preflight.py"
_PREFLIGHT_RC=$?
[ "$_PREFLIGHT_RC" -ne 0 ] && { echo "! BLOCKED — resolve_preflight.py failed (gh missing/unauthenticated or git fetch failure); cannot proceed"; exit 1; }
IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/resolve-preflight-CODEX_AVAILABLE-${CSID}" 2>/dev/null || CODEX_AVAILABLE="false"
IFS= read -r GH_OK < "${TMPDIR:-/tmp}/resolve-preflight-GH_OK-${CSID}" 2>/dev/null || GH_OK="true"
# --worktree/--keep: worktree off HEAD pre-Step4 checkout (worktree-isolation.md §resolve)
# shared flag/--keep parser (C5; also analyse/review SKILL.md); --keep-state: no eval, writes resolve-{worktree,clean-args,keep-items}-${CSID} (compaction-contract.md §keep: semantics), owner-safe clear of a crashed run's stale contract (§Lifecycle)
python "$PLUGIN_ROOT/bin/parse-skill-flags.py" --flags worktree --keep-state resolve -- "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/resolve-worktree-${CSID}" 2>/dev/null || WT_ENABLED=false
echo "$WT_ENABLED" > "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}"

# codemap: auto-on if installed; --no-codemap off; --codemap strict (stop if missing)
# loads: detect_codemap.py — consumers: resolve/SKILL.md, review/SKILL.md
_DETECT_CODEMAP="$PLUGIN_ROOT/bin/detect_codemap.py"
# codemap flags parsed inside the script, before parse-resolve-args: one argv slot, shlex-tokenised
python "$_DETECT_CODEMAP" --prefix resolve --arguments "$ARGUMENTS" 2>&1  # timeout: 5000
[ $? -ne 0 ] && { echo "! BLOCKED — codemap strict mode requested but codemap not installed or index missing"; exit 1; }
IFS= read -r CODEMAP_ENABLED < "${TMPDIR:-/tmp}/resolve-codemap-enabled-${CSID}" 2>/dev/null || CODEMAP_ENABLED="false"
IFS= read -r CODEMAP_CURRENCY < "${TMPDIR:-/tmp}/resolve-codemap-currency-${CSID}" 2>/dev/null || CODEMAP_CURRENCY="off"
IFS= read -r CODEMAP_FORCE_OFF < "${TMPDIR:-/tmp}/resolve-codemap-forced-off-${CSID}" 2>/dev/null || CODEMAP_FORCE_OFF="false"
[ "$CODEMAP_FORCE_OFF" = "false" ] && python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_SHARED/codemap-gates.md"  # timeout: 5000
```

**Codemap gates** — when `CODEMAP_FORCE_OFF=false`, follow `codemap-gates.md` loaded above, including preparation, explicit preferences, required-mode failure and optional fallback rules. Skip both gates when `CODEMAP_FORCE_OFF=true` (`--no-codemap`); never model-invoke the user-slash-only scan skill.

Codex missing: set `CODEX_AVAILABLE=false` — Steps 3–7 work without it. Step 8 degradation:

- Simple, single-file items → `foundry:sw-engineer`
- Complex/multi-file → skip with: `⚠ bridge@borda-ai-rig is absent or disabled — skipping item #<id>. Install or enable the bridge and reload plugins.`

### Review-handoff auto-detect (when $ARGUMENTS is empty)

<!-- loads: handoff-detect.md -->

Load and execute only when `$ARGUMENTS` is empty; any other run skips it.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/handoff-detect.md"  # timeout: 5000
```

Execute its blocks (loaded above).

Parse $ARGUMENTS:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -n "$CLAUDE_PLUGIN_ROOT" ] || { echo "Error: CLAUDE_PLUGIN_ROOT is unset — verify oss plugin installation and that skill is invoked via Claude Code plugin system"; exit 1; }  # timeout: 5000
[ -f "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" ] || { echo "Error: parse-resolve-args.py not found — verify oss plugin installation"; exit 1; }  # timeout: 5000
# parse-resolve-args.py anchors on "<PR#> [report]" alone — ANY surviving flag token routes to
# comment-dispatch and drops PR_NUMBER. Every supported flag must be stripped here, not just codemap/keep.
# --keep-state also writes resolve-no-challenge-${CSID}, read by Step 8 Phase 1; no stale contract yet (first write: Step 3d), so the second clear is a no-op
python "${CLAUDE_PLUGIN_ROOT}/bin/parse-skill-flags.py" --flags no-codemap,codemap,worktree,no-challenge --value-flags agent --preserve-whitespace --keep-state resolve -- "$ARGUMENTS" >/dev/null || exit 1  # timeout: 5000
ARGUMENTS=""
IFS= read -r -d '' ARGUMENTS < "${TMPDIR:-/tmp}/resolve-clean-args-${CSID}" 2>/dev/null || :
ARGUMENTS=${ARGUMENTS%$'\n'}
IFS= read -r VALUE_AGENT < "${TMPDIR:-/tmp}/resolve-agent-${CSID}" 2>/dev/null || VALUE_AGENT=""
case "${VALUE_AGENT:-}" in
    '') ;;
    *:*) ;;
    *) VALUE_AGENT="foundry:$VALUE_AGENT" ;;
esac
# a leftover --token would route "42 --full" to comment dispatch (bridge:implement then gets that text as a review comment): strip it when the rest is a PR/report request; bare comment text keeps its dashes
_REST=$(printf '%s' "$ARGUMENTS" | sed -E 's/(^|[[:space:]])--[A-Za-z][A-Za-z-]*//g; s/^[[:space:]]+//; s/[[:space:]]+$//')  # timeout: 3000
if [ "$_REST" != "$ARGUMENTS" ] && [ -n "$_REST" ] && ! python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" "$_REST" | grep -qx 'MODE=comment-dispatch'; then
    echo "UNKNOWN_FLAGS=$(printf '%s\n' "$ARGUMENTS" | tr -s '[:space:]' '\n' | grep -E '^--[A-Za-z][A-Za-z-]*$' | tr '\n' ' ')"
    ARGUMENTS="$_REST"
fi
echo "${VALUE_AGENT:-}" > "${TMPDIR:-/tmp}/resolve-agent-override-${CSID}"
echo skip > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"  # Step 3d push question overwrites; a run that never reaches it must not inherit last run's `open`
# `unset`, not `skip`/`push`: a run that never records a Step 3d push answer must neither push nor inherit last run's `push` — Step 10 asks instead
echo unset > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"
echo none > "${TMPDIR:-/tmp}/resolve-push-status-${CSID}"  # Step 10 overwrites with what happened; `none` = Step 10 never ran (no PR#, skip-all)
# same reason: a run that never reaches Step 3d (zero pending with no closed items, bulk skip-all) must not inherit last run's `stage`/`grouped`.
# `unset`, not `each`: Step 8's merge fence aborts on it, so a skipped Step 3d block fails loud instead of landing per-item commits
echo unset > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"
echo domain > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"
# Step 3d overwrites; `auto` (not `unset`) because a skipped Step 3d means no Phase 2 dispatch either — nothing to widen or serialize
echo auto > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"
: > "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}"  # empty, not rm (danger filter): report mode skips Step 3b; a stale dir from the previous PR would feed its items into this run
: > "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}"  # gap triage Proceed writes the accepted report path; empty = full intake, never inherited
# NUL-delimited data preserves multiline comments and quotes without evaluating parser output.
tmpenv=$(mktemp)  # timeout: 3000
trap 'rm -f "$tmpenv"' EXIT INT TERM
python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" --null "$ARGUMENTS" >"$tmpenv" || exit 1  # timeout: 5000
while IFS= read -r -d '' _K && IFS= read -r -d '' _V; do case "$_K" in PR_NUMBER) PR_NUMBER=$_V;; PR_URL) PR_URL=$_V;; MODE) MODE=$_V;; ARGUMENTS) ARGUMENTS=$_V;; *) echo "Error: parse-resolve-args.py emitted an unexpected key — refusing to load"; exit 1;; esac; done < "$tmpenv"
# sets: PR_NUMBER, PR_URL, MODE, ARGUMENTS ('#' stripped, comment-dispatch only)
# parser keeps a non-/pull/ GitHub URL as MODE=pr, empty PR_NUMBER (never comment dispatch); every PR step keys on PR_NUMBER
if [ "$MODE" = pr ] && [ -z "$PR_NUMBER" ]; then echo "! BLOCKED — not a pull-request URL: $PR_URL — /oss:resolve takes a PR number or a .../pull/N URL"; exit 1; fi
echo "${PR_NUMBER:-n/a}" > "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}"  # timeout: 3000
# liveness sentinel for oss:review's Step 7 already-running check; write-only here, cleared at report/reject-gate stops below + Step 11/12 (review/SKILL.md)
# stale-tolerated like a crashed skill-contract.md, but no Step 0 rm: only overwritten when a later run reaches this line, so a stop before it
# (Step 3d skip-all, `! BLOCKED` sentinel-loss guard) suppresses oss:review's gate for that PR all session — accepted, not chased into every exit
# `report` value never numeric-matches a review's PR tag: a no-PR run must never suppress any review's gate
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "report" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; *) echo "$PR_NUMBER" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; esac  # timeout: 3000
: > "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}"  # Step 4 or report mode must publish this run's base before Step 9
: > "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}"  # empty = no PR branch; Step 9.0 would otherwise re-merge a prior PR's target into local work
: > "${TMPDIR:-/tmp}/resolve-remote-head-ref-${CSID}"
: > "${TMPDIR:-/tmp}/resolve-pr-ref-${CSID}"  # Step 4 or local report mode must publish this run's commit reference
echo targeted > "${TMPDIR:-/tmp}/resolve-change-scope-${CSID}"  # Step 8 prelude overwrites; a run that skips Step 8 must not inherit last run's lint-only (it would skip QA)
```

<!-- branch: unsupported-flags — isolated; ≤1 call; fires only when unknown flags present -->

**Unsupported flag check** — the parse block above prints `UNKNOWN_FLAGS=<tokens>` when a PR/report request carried a `--<token>` outside `{--no-challenge, --agent, --codemap, --no-codemap, --worktree}`, and has already removed those tokens from `$ARGUMENTS` (the parser anchors on `<PR#> [report]` alone: a surviving token would reroute the run to comment dispatch and drop `PR_NUMBER`). Printed → invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (the tokens are already stripped; proceed). Bare comment text is never scanned: its dashes are text. Supported: `--no-challenge`, `--agent <name>`, `--codemap`, `--no-codemap`, `--worktree`, `--keep "<items>"`.

- `MODE="pr+report"` or `MODE="report"` → resolve the report source with the **Report source resolution** block below (executable, not prose), then branch on its printed `REPORT_STATUS`.
  - `MODE="report"` additionally: extract PR# from the report header if present; no PR# in header → execute the **Local branch safety guard** below before Step 8, then reject tracked local edits: `[ -z "$(git status --porcelain -uno)" ] || { echo "! BLOCKED — uncommitted edits: report mode without PR# commits on this branch as-is; commit or stash them first"; exit 1; }`
- `MODE="pr"` → continue Step 2; only when the reject gate printed `incomplete-review-report`, run **Report source resolution** first for its gap triage
- `MODE="comment-dispatch"` → execute the **Local branch safety guard** below before Step 12; then `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-resolve-worktree-${CSID}" 2>/dev/null; [ "$WT_ENABLED" = "true" ] || WT_ENABLED=false; [ "$WT_ENABLED" = "true" ] && echo "⚠ --worktree has no effect in comment-dispatch mode"` → jump to Step 12

### Local branch safety guard

Execute only at the mode-specific point above: before Step 8 for a report without a PR#, or before Step 12 for comment dispatch. Both routes must refuse the default branch and an unknown default:

```bash
CURRENT=$(git branch --show-current)
DEFAULT=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||')
[ -z "$DEFAULT" ] && DEFAULT=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | awk '{print $NF}')
[ -z "$DEFAULT" ] && { printf "! BLOCKED — cannot determine default branch; refusing to proceed\n"; exit 1; }
if [ "$CURRENT" = "$DEFAULT" ]; then echo "! BLOCKED — on default branch '$CURRENT': local report/comment dispatch must use a feature branch"; exit 1; fi
```

### Reject-gate check (every mode — run the block even without a `PR_NUMBER`)

`/oss:review`'s acceptance gate can reject a PR at the premise level — `Gate: REVIEW_V1_REJECT_<GROUND> @<sha>`, one of `GOAL`/`CONDUCT`/`SCOPE`/`LICENSE`/`DUPLICATE`/`REVERTED`/`SPAM`/`PHILOSOPHY` (see `/oss:review` `modes/acceptance-gate.md` Stage 1 for what each means). Premise problem, not fixable by `/oss:resolve` editing code — never start the fix pipeline on a PR still in that state, regardless of which of the 8 grounds fired. Legacy unprefixed reports still parse. Only complete `REVIEW_V1_PASS` or `REVIEW_V1_BLOCK` reports are actionable fix queues, plus an incomplete report the user accepts at **Gap triage** with zero critical gaps. Missing, malformed, or ambiguous decision headers block the workflow; a newer unfinished run cannot replace an earlier decision. No prior report still permits PR-comments-only mode. Report-only mode also validates the selected report; it cannot clear a rejection by bypassing PR-aware intake.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
# --path-out: this lookup is PR-scoped; Steps 3a/3c reuse it instead of a second newest-of-any-PR glob
python "$PLUGIN_ROOT/bin/find_review_report.py" --pr "$PR_NUMBER" \
    --path-out "${TMPDIR:-/tmp}/resolve-report-file-${CSID}"  # timeout: 6000
```

The gate's `--path-out` sentinel (`${TMPDIR:-/tmp}/resolve-report-file-${CSID}`) is the **PR-scoped** answer to "does a review report for this PR already exist". Steps 3a and 3c reuse it; PR-scoped misses never glob another PR — never re-derive it from the gate's printed line, and never let the printed `Gate: …` verdict be the only thing parsed out of this block. A path-publication failure stops the workflow. An exit 1 that printed `⛔ BLOCKED — incomplete-review-report` is not yet a stop, in any mode: run **Report source resolution** next, which grades the gaps and asks (**Gap triage**). Every other exit 1 — a standing rejection, an invalid `--pr`, a failed path publication — stops here. With no `PR_NUMBER` the script skips the check and writes an **empty** sentinel — that write is why the block runs in every mode: a bare `/oss:resolve report` after an earlier `/oss:resolve 42 report` in the same session would otherwise inherit PR 42's path.

### Report source resolution (`report` and `pr + report` modes; `pr` mode only after an incomplete-report gate)

<!-- loads: report-intake.md -->

Single lookup for both modes, the **only** sanctioned way to conclude that no report exists, and the intake triage for an incomplete report (**Gap triage**, **Intake proceed**). Load and execute it when `MODE` is `report` or `pr + report`, or `pr` after the reject gate printed `incomplete-review-report`; `pr` mode otherwise and `comment-dispatch` skip it.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_RESOLVE < "${TMPDIR:-/tmp}/resolve-oss-resolve-${CSID}" 2>/dev/null || _OSS_RESOLVE=""  # reload (Check 43)
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RESOLVE/modes/report-intake.md"  # timeout: 5000
```

Execute its blocks (loaded above), then branch on the printed `REPORT_STATUS`.

### Create all workflow tasks upfront

After `PR_NUMBER` and `MODE` resolved above, create all major-step tasks now — every `TaskCreate` in **one response**, riding with the next real tool call (the Step 3a or 3b `cat` block). Store each returned `task_id` for step-level `TaskUpdate` calls. Conditional tasks: include condition in subject brackets; cancel via `TaskUpdate(status="deleted")` at skip point — never leave conditional tasks pending. All seven creates ride in that one response:

```text
TASK_GATHER   = TaskCreate(subject="Step 2: Gather action items — PR #<N>",              activeForm="Gathering action items for PR #<N>")
TASK_SELECT   = TaskCreate(subject="Step 3: Select action items — PR #<N>",               activeForm="Selecting action items")
TASK_CHECKOUT = TaskCreate(subject="Step 4: Checkout PR branch [if pr mode]",             activeForm="Checking out PR branch")
TASK_CONFLICT = TaskCreate(subject="Steps 5–7: Conflict resolution [if pr mode]",         activeForm="Resolving conflicts")
TASK_IMPL     = TaskCreate(subject="Step 8: Implement selected items [if items selected]", activeForm="Implementing action items")
TASK_LINT     = TaskCreate(subject="Step 9: Lint and QA gate",                             activeForm="Running lint and QA")
TASK_CLOSE    = TaskCreate(subject="Steps 10–11: Push and final report [if pr mode]",      activeForm="Pushing to fork and reporting")
```

**Zero bookkeeping-only turns.** Step-level progress stays visible — every task above moves `pending` → `in_progress` → `completed` (or `deleted` when skipped) exactly as before — but no response may consist only of `TaskCreate`/`TaskUpdate`/`TaskList` calls, per-item and per-conflict tasks included. Each update rides in the response that carries the named real tool call; the only standalone one is `TASK_CLOSE` → `completed` immediately before the long Step 11 report, using `TaskUpdate(completed)` (`rules/task-lifecycle.md` §TaskUpdate before long output):

| Step | Updates | Rides with |
| -- | -- | -- |
| 2 | `TASK_GATHER` → `in_progress` | the Step 3a/3b `cat` block |
| 4 | `TASK_CHECKOUT` → `in_progress` (or `TASK_CHECKOUT`, `TASK_CONFLICT` → `deleted` when skipped) | the Step 4 checkout block; when skipped, the next real call |
| 4 end | `TASK_CHECKOUT` → `completed` | the same Step 4 checkout block |
| 5 | `TASK_CONFLICT` → `in_progress` | the `conflict-resolution.md` load |
| 3d | `TASK_GATHER` → `completed`, `TASK_SELECT` → `in_progress` | the boundary-0 contract block, before the selection prompt |
| Run 2 fan | `TASK_SELECT` → `completed` | the fan's first tool call (Step 3e creates) |
| 7b join | `TASK_CONFLICT` → `completed` | the merge commit call (zero conflicts: the fan's first tool call) |
| 8 | `TASK_IMPL` → `in_progress` (or `deleted` when no items) | the codemap index block, or Step 9's first call |
| 8 end | `TASK_IMPL` → `completed` | Step 9's boundary-2 block, with `TASK_LINT` → `in_progress` |
| 9 end | `TASK_LINT` → `completed`, `TASK_CLOSE` → `in_progress` (or `deleted` when Step 10 is skipped) | Step 10's first block; when Step 10 is skipped, Step 11's first call |
| 11 | `TASK_CLOSE` → `completed` | standalone, right before the report |

Per-item tasks (Step 3e) and per-conflict tasks (Step 5a): create each set in one response, and close each item's task in the response that already carries the next real call.
