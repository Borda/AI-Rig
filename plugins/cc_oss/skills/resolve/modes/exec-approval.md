<!-- oss:resolve Step 3d execution approvals — executed inline: cat $_OSS_RESOLVE/modes/exec-approval.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 4 and Step 3d), plugins/cc_oss/skills/resolve/modes/checkout.md, plugins/cc_oss/skills/resolve/modes/item-selection.md (slot table), plugins/cc_oss/skills/resolve/modes/dispatch-prelude.md (recovery) -->

## Step 3d: execution approvals — PR code and repository hooks

Both approvals are asked at Step 3d, last, so Run 2 stays unattended. They need only what Step 4 already produced: the PR head, the base ref and the checkout. `pr` and `pr+report` modes ask at Step 3d. In `report` mode with a PR number Step 3d comes before the checkout, so the Decide block runs at Step 4 and records what needs a question (`ask` plus the saved inspection or scan text) and the Step 8 prelude asks it from that record (recovery path); a `report` run with no PR number decides nothing here.

**Decide — runs right after the Step 4 checkout (before any merge or action-item commit) and again before the first selection call.** It asks nothing and is read-only apart from the files under `$IMPL_DIR`; a decision already recorded stays recorded and a question already found necessary (`ask`) stays one, so the second run only settles what the first could not and never loosens an earlier answer into an unasked run. The one exception drops nothing that could run: a pending hook question is decided again on the staged merge tree, and is dropped only when no hook would run (`none`, or `disabled` by a saved never policy). Otherwise its inspection is replaced, so the question shows the digest the merge commit checks; when that re-inspection fails the earlier text stays and `$IMPL_DIR/hooks-inspection-note.txt` says its digest predates the merge. It records, in `$IMPL_DIR/exec-plan.txt`, one value per approval:

- `pr-code=same-repo` (no question, no guard), `auto` (question skipped, see below), `ask`, `recorded` (`deny`, or `auto`, `sandbox` or `unsandboxed` with `code-approval.txt` beside it in `pr-code-decision.txt`; the word alone is not a record) or `deferred` (no checkout: the Step 8 prelude decides).
- `hooks=none|run|disabled` (the helper decided, nothing to ask; after an earlier `ask` only `none` or `disabled`; a failed first inspection keeps hooks disabled; the echo names its error file), `ask`, `recorded` or `deferred`. A run that loses its PR, head or base sentinel after an earlier `hooks=ask` writes `deferred` for PR code but keeps `hooks=ask`.

A failed scan is never `deferred` past Step 3d: it gives `ask`, and the question text says the scan was unavailable.

The hook decision passes `--head "$PR_HEAD_OID"`. The helper uses it only while a merge is pending (the Step 3d run after Step 5 staged the base merge): it then compares what the PR head and the checked-out commit commit, plus staged entries that match neither side of the merge, and does not count base-side edits the merge already staged (a version bump in `package.json`) as PR changes. At Step 4 no merge is pending, so it is ignored and the clean checkout is judged exactly as before, staged edits included. A `run` the helper made itself is refreshed the same way here and before every merge commit (`--refresh-approval`, `conflict-resolution.md`); a stale approval it cannot refresh is set aside there, so the run continues with hooks disabled rather than leaving the merge uncommitted.

**Auto — a clean scan skips the question; the runs stay in the sandbox.** Resolve merges the base, pulls later PR commits and edits files before it runs tests, so what runs is never exactly the head any scan read, and a run on unscanned bytes needs the sandbox. Only when all of these hold, checked by `code_safety_status.py check`:

- `oss:review` scanned this exact head and wrote `code-safety.json` beside its report, risk `none`.
- This fresh scan of the same head also has risk `none`.
- This host has a usable sandbox (the check reads the fresh scan's `isolation_available`); without one the question is asked.

The approval is bound with the default tier, exactly like the **Run PR code in sandbox** answer (never `--decision unsandboxed`), and recorded as `auto`; the Step 11 final report states it with the head (`PR code:` line, from `pr-code-decision.txt`), because Bash output is not reliably shown. The scan is a static heuristic, never a safety claim, so any finding, a different head, or a missing or unscanned review record gives `ask`, with the sandbox as the first option. `Run PR code in sandbox` stays available on every question.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
IFS= read -r PR_HEAD_OID < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || PR_HEAD_OID=""
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
IFS= read -r _XREPO < "${TMPDIR:-/tmp}/resolve-is-cross-repo-${CSID}" 2>/dev/null || _XREPO=true
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; Step 3a/3b did not finish"; exit 1; }
case "$PR_NUMBER" in ''|*[!0-9]*) _NO_PR=true ;; *) _NO_PR=false ;; esac
IFS= read -r _PREV < "$IMPL_DIR/exec-plan.txt" 2>/dev/null || _PREV=""
if [ "$_NO_PR" = true ] || [ -z "$PR_HEAD_OID" ] || [ -z "$BASE_REF" ]; then
    _KEEP=deferred; [ -s "$IMPL_DIR/hooks-inspection.json" ] && [ "${_PREV#*hooks=ask}" != "$_PREV" ] && _KEEP=ask  # a lost sentinel never erases an earlier ask
    echo "pr-code=deferred hooks=$_KEEP" > "$IMPL_DIR/exec-plan.txt"; cat "$IMPL_DIR/exec-plan.txt"; exit 0
fi
if [ -e "$IMPL_DIR/approved-hooks.txt" ]; then
    _HOOKS=recorded
    IFS= read -r _HD < "$IMPL_DIR/hooks-decision.txt" 2>/dev/null || _HD=""
    [ "$_HD" != run ] || python "$PLUGIN_ROOT/bin/resolve_commit_hooks.py" --refresh-approval --base "origin/$BASE_REF" --head "$PR_HEAD_OID" --approved-file "$IMPL_DIR/approved-hooks.txt" --decision-file "$IMPL_DIR/hooks-decision.txt" || { : > "$IMPL_DIR/approved-hooks.txt"; printf 'stale\n' > "$IMPL_DIR/hooks-decision.txt"; }  # timeout: 30000 — helper-made run only: digest must cover the staged merge tree or the merge commit is refused; a user's answer is never re-decided
elif [ -s "$IMPL_DIR/hooks-inspection.json" ] && [ "${_PREV#*hooks=ask}" != "$_PREV" ]; then
    _HOOKS=ask; _RD=""  # an earlier ask never becomes an unasked run; deciding again on the staged merge tree refreshes the digest the question shows
    echo "the digest below was taken before the base merge was staged (re-inspection failed: $IMPL_DIR/hooks-refresh.err); if the merge changes the hooks, an approval of it no longer matches at the merge commit and hooks then stay disabled" > "$IMPL_DIR/hooks-inspection-note.txt"
    if python "$PLUGIN_ROOT/bin/resolve_commit_hooks.py" --decide --base "origin/$BASE_REF" --head "$PR_HEAD_OID" > "$IMPL_DIR/hooks-refresh.json" 2> "$IMPL_DIR/hooks-refresh.err"; then  # timeout: 30000
        jq -b -r '.decision // empty' "$IMPL_DIR/hooks-refresh.json" > "$IMPL_DIR/hooks-refresh-decision.txt" 2> /dev/null
        IFS= read -r _RD < "$IMPL_DIR/hooks-refresh-decision.txt" 2>/dev/null || _RD=""
    fi
    case "$_RD" in
        ask|run) jq -b . "$IMPL_DIR/hooks-refresh.json" > "$IMPL_DIR/hooks-inspection.json" && : > "$IMPL_DIR/hooks-inspection-note.txt" ;;  # run keeps the question, shown with the merged tree's digest
        none|disabled)  # no hook would run (none installed, or a saved never policy): nothing left to approve
            jq -b . "$IMPL_DIR/hooks-refresh.json" > "$IMPL_DIR/hooks-inspection.json"; : > "$IMPL_DIR/hooks-inspection-note.txt"
            : > "$IMPL_DIR/approved-hooks.txt"; echo "$_RD" > "$IMPL_DIR/hooks-decision.txt"; _HOOKS=$_RD
            jq -b -r '"→ repository hooks: " + .decision + " — " + (.reason // "") + "; the earlier hook question is dropped"' "$IMPL_DIR/hooks-inspection.json" ;;
    esac
else
    _HOOKS=""
    if python "$PLUGIN_ROOT/bin/resolve_commit_hooks.py" --decide --base "origin/$BASE_REF" --head "$PR_HEAD_OID" --approved-file "$IMPL_DIR/approved-hooks.txt" > "$IMPL_DIR/hooks-inspection.json" 2> "$IMPL_DIR/hooks-inspection.err"; then  # timeout: 30000
        jq -b -r '.decision // empty' "$IMPL_DIR/hooks-inspection.json" > "$IMPL_DIR/hooks-decision.txt" 2>/dev/null
        IFS= read -r _HOOKS < "$IMPL_DIR/hooks-decision.txt" 2>/dev/null || _HOOKS=""
    fi
    case "$_HOOKS" in
        ask) ;;
        none|run|disabled) jq -b -r '"→ repository hooks: " + .decision + " — " + (.reason // "")' "$IMPL_DIR/hooks-inspection.json" ;;
        *) : > "$IMPL_DIR/approved-hooks.txt"; _HOOKS=disabled; echo "⚠ hook inspection failed — hooks stay disabled for this run ($IMPL_DIR/hooks-inspection.err)" ;;
    esac
fi
_DEC=""
if [ -e "$IMPL_DIR/pr-code-decision.txt" ]; then
    IFS= read -r _DEC < "$IMPL_DIR/pr-code-decision.txt" || [ -n "$_DEC" ] || _DEC=deny  # an unterminated last line still reads; an empty or unreadable file fails closed as deny
fi
if [ "$_XREPO" = false ]; then
    _PRCODE=same-repo
elif [ "$_DEC" = deny ] || { { [ "$_DEC" = auto ] || [ "$_DEC" = sandbox ] || [ "$_DEC" = unsandboxed ]; } && [ -e "$IMPL_DIR/code-approval.txt" ]; }; then
    _PRCODE=recorded  # a word without its approval file is not recorded: the prelude asks for that state too
elif [ -s "$IMPL_DIR/code-scan.txt" ] && [ "${_PREV#*pr-code=ask}" != "$_PREV" ]; then
    _PRCODE=ask  # an earlier ask is kept, never rescanned into a skip
else
    _PRCODE=ask
    if python -I "$PLUGIN_ROOT/bin/untrusted_code_guard.py" scan --head "$PR_HEAD_OID" --base "origin/$BASE_REF" --out "$IMPL_DIR/code-scan.json" --text > "$IMPL_DIR/code-scan.txt" 2> "$IMPL_DIR/code-scan.err"; then  # timeout: 60000
        case "$REPORT_FILE" in
            */*) python "$PLUGIN_ROOT/bin/code_safety_status.py" check --status "${REPORT_FILE%/*}/code-safety.json" --scan "$IMPL_DIR/code-scan.json" --head "$PR_HEAD_OID" > "$IMPL_DIR/code-safety-check.txt" ;;
            *) echo "ask reason=no-review-status" > "$IMPL_DIR/code-safety-check.txt" ;;
        esac
        IFS= read -r _CHECK < "$IMPL_DIR/code-safety-check.txt" 2>/dev/null || _CHECK="ask reason=check-failed"
        [ "$_DEC" != stale ] || _CHECK="ask reason=stale-approval"  # a refused approval is never re-bound without asking
        if [ "$_CHECK" = auto ] && python -I "$PLUGIN_ROOT/bin/untrusted_code_guard.py" bind --head "$PR_HEAD_OID" --base "origin/$BASE_REF" --scope run --out "$IMPL_DIR/code-approval.txt" > /dev/null; then
            echo auto > "$IMPL_DIR/pr-code-decision.txt"; _PRCODE=auto
            echo "→ PR code: review and this scan both found nothing at head ${PR_HEAD_OID:0:12}; runs in the sandbox, no question"
            cat "$IMPL_DIR/code-scan.txt"
        fi
    else
        echo "scan unavailable (see $IMPL_DIR/code-scan.err): no verdict or risk level to show; decide without it" > "$IMPL_DIR/code-scan.txt"
    fi
fi
echo "pr-code=$_PRCODE hooks=$_HOOKS" > "$IMPL_DIR/exec-plan.txt"; cat "$IMPL_DIR/exec-plan.txt"
```

**Stale approvals.** The approval is bound to the PR head, but Step 5 has already merged the base: when that merge changes a dependency manifest, or later work adds a high finding or touches one, the guard refuses the first test as stale (exit 3) and the question is asked once more, at the committed `HEAD` saved by the `PR code = stale` block. A second refusal at the same commit (uncommitted work the committed approval cannot cover) is final: the tests are reported as not run.

**Placement — last, never first.** Each value `ask` adds one question. They take the last free slots of the last Step 3d call, after push and topic-group. Where that call already holds 4 questions, they go together in one extra call straight after it, still before Run 2 starts. A bulk answer that stops the run (skip-all) never reaches them. Unanswered or dismissed: record nothing; the Step 8 prelude then asks as its recovery path, never a silent skip.

**PR-code question** (`pr-code` is `ask`; single-select, header `pr-code`). The question text carries the evidence, never reply text before the call (5.5-family models may return that as an empty progress update). It holds why PR code runs (every test the run executes, the QA gate and the full suite), the scan's `verdict`, `risk` and `why` lines copied from `$IMPL_DIR/code-scan.txt`, and `→ $IMPL_DIR/code-scan.txt` for the full evidence, at most 1,500 characters in all. The first line is one plain sentence on why this code is not treated as safe, built from the `why` lines in everyday words (it calls curl, so it can reach the internet), never a bare category name. When risk is `likely-malicious`, lead with "do not run"; choosing to run is the user's explicit override. Options:

- **Run PR code in sandbox** — disposable copy, network denied, home hidden.
- **Do not run PR code** — tests are reported as not run.
- **Run without sandbox** — always offered. Still a disposable copy and a cleared environment, but no containment: the network, `~/.ssh`, the keychain and the gh token file stay reachable, and that code can rewrite this run's recorded answers.

**Hook question** (`hooks` is `ask`; single-select, header `repo-hooks`). Show the exact hook commands, file contents and configuration from `$IMPL_DIR/hooks-inspection.json`, naming the PR-changed inputs in `changed` (or the `reason` when the base could not be compared). When `$IMPL_DIR/hooks-inspection-note.txt` is not empty, the question quotes its line. Approving commits or tests never approves PR-supplied hooks. Options: **Run these hooks** / **Keep hooks disabled** / **Run and remember for this repo** / **Never run hooks in this repo**.

**Record the answers — one fixed block per answer, run in one response with the other Step 3d answer blocks.**

PR code = sandbox:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r PR_HEAD_OID < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || PR_HEAD_OID=""
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
[ -n "$IMPL_DIR" ] && [ -n "$PR_HEAD_OID" ] && [ -n "$BASE_REF" ] || { echo "! BLOCKED — IMPL_DIR, PR head or base ref sentinel missing"; exit 1; }
IFS= read -r _BIND_HEAD < "$IMPL_DIR/pr-code-rebind-head.txt" 2>/dev/null || _BIND_HEAD=""; [ -n "$_BIND_HEAD" ] || _BIND_HEAD="$PR_HEAD_OID"  # after a stale refusal the answer binds the commit the refusal was about, not the PR head
python -I "$PLUGIN_ROOT/bin/untrusted_code_guard.py" bind --head "$_BIND_HEAD" --base "origin/$BASE_REF" --scope run --out "$IMPL_DIR/code-approval.txt" > /dev/null || { echo "! BLOCKED — guard bind failed; PR code stays unapproved"; exit 1; }  # timeout: 60000
echo sandbox > "$IMPL_DIR/pr-code-decision.txt"
```

PR code = unsandboxed:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r PR_HEAD_OID < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || PR_HEAD_OID=""
IFS= read -r BASE_REF < "${TMPDIR:-/tmp}/resolve-base-ref-${CSID}" 2>/dev/null || BASE_REF=""
[ -n "$IMPL_DIR" ] && [ -n "$PR_HEAD_OID" ] && [ -n "$BASE_REF" ] || { echo "! BLOCKED — IMPL_DIR, PR head or base ref sentinel missing"; exit 1; }
IFS= read -r _BIND_HEAD < "$IMPL_DIR/pr-code-rebind-head.txt" 2>/dev/null || _BIND_HEAD=""; [ -n "$_BIND_HEAD" ] || _BIND_HEAD="$PR_HEAD_OID"  # after a stale refusal the answer binds the commit the refusal was about, not the PR head
python -I "$PLUGIN_ROOT/bin/untrusted_code_guard.py" bind --head "$_BIND_HEAD" --base "origin/$BASE_REF" --scope run --tier env-only --decision unsandboxed --out "$IMPL_DIR/code-approval.txt" > /dev/null || { echo "! BLOCKED — guard bind failed; PR code stays unapproved"; exit 1; }  # timeout: 60000
echo unsandboxed > "$IMPL_DIR/pr-code-decision.txt"
```

PR code = do not run (records no approval, so the final report lists "tests not run — PR-code execution not approved" as an execution gap, never a pass):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing"; exit 1; }
[ ! -e "$IMPL_DIR/code-approval.txt" ] || mv -f "$IMPL_DIR/code-approval.txt" "$IMPL_DIR/code-approval.set-aside" || { echo "! BLOCKED — could not set the approval file aside; a declined approval must not stay usable"; exit 1; }  # runners read only the approval file, so a declined approval must not stay beside the word
echo deny > "$IMPL_DIR/pr-code-decision.txt"
```

PR code = stale (a guard refusal said the approval is stale, from the full suite, a QA run or an item agent; run from the checkout root; the question is then asked again at the saved current commit):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing"; exit 1; }
[ ! -e "$IMPL_DIR/code-approval.txt" ] || mv -f "$IMPL_DIR/code-approval.txt" "$IMPL_DIR/code-approval.set-aside" || { echo "! BLOCKED — could not set the stale approval file aside; the runners would still accept it"; exit 1; }  # a stale approval must not stay beside a later Do-not-run answer
git rev-parse HEAD > "$IMPL_DIR/pr-code-head-now.txt" || { echo "! BLOCKED — could not read the current HEAD for the new approval"; exit 1; }
IFS= read -r _NOW < "$IMPL_DIR/pr-code-head-now.txt" || [ -n "$_NOW" ] || _NOW=""
IFS= read -r _SAVED < "$IMPL_DIR/pr-code-rebind-head.txt" 2>/dev/null || [ -n "$_SAVED" ] || _SAVED=""
if [ -n "$_SAVED" ] && [ "$_SAVED" = "$_NOW" ]; then echo deny > "$IMPL_DIR/pr-code-decision.txt"; echo "⚠ the approval went stale again at the commit it was re-asked for (an uncommitted dependency or high-finding change?); no more questions this run: tests not run — PR-code execution not approved"; exit 0; fi  # a committed rebind cannot cover uncommitted work, so a second refusal at the same commit is final
echo "$_NOW" > "$IMPL_DIR/pr-code-rebind-head.txt"  # the stale refusal is about work since the PR head, so the new scan and bind use this commit
echo stale > "$IMPL_DIR/pr-code-decision.txt"
```

Hooks: **Run these hooks** and **Run and remember for this repo** save the `sha256` shown in `hooks-inspection.json` as the sole line of `$IMPL_DIR/approved-hooks.txt` with the Write tool; **Run and remember** also runs `python "$PLUGIN_ROOT/bin/resolve_commit_hooks.py" --remember approved --sha <the displayed sha256>` (the saved policy binds the digest the user was shown, never a later inspection), and **Never run hooks in this repo** runs `--remember never`. Every other answer writes an empty `approved-hooks.txt` (hooks disabled). The file existing, even empty, means this run already decided; never inherit it from an earlier run.
