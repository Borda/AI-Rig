<!-- file: preflight.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:review -->

## Step 0: Parse flags and content-type pre-classification

Parse `$ARGUMENTS` flags first (via `bin/parse-skill-flags.py`, C5) — this sets `CLEAN_ARGS`, the mode flags, and `DIRECT_PATH_MODE` **before** any step below references them (the pre-classification and Step 1 both read them):

| Flag | Variable | Present | Absent |
| -- | -- | -- | -- |
| `--reply` | `REPLY_MODE` | `true` | `false` |
| `--no-challenge` | `CHALLENGE_ENABLED` | `false` | `true` |
| `--no-codemap` | `CODEMAP_FORCE_OFF` | `true` | `false` |
| `--codemap` | — strict mode, consumed by `detect_codemap.py` | stop and report if codemap missing | auto-detect |
| `--worktree` | `WT_ENABLED` | `true` | `false` |
| `--full` | `FANOUT_CAP` | `0` — no cap, all preselected | `3` (`FANOUT_MAX`) |
| `--fill-gaps <report.md>` | `FILL_GAPS_MODE` + `oss-review-fill-report` sentinel | `true` — targeted rerun of that run (`modes/fill-gaps.md`) | `false` |
| `--roles "<role,…>"` | `oss-review-fill-roles` sentinel | role names to re-run | `""` |
| `--candidates "<id,…>"` | `oss-review-fill-candidates` sentinel | candidate ids to re-validate | `""` |
| `--keep "<items>"` | `oss-review-keep-items` sentinel | value string | `""` |

`CLEAN_ARGS`: `$ARGUMENTS` with matched flags removed (including `--keep "<items>"` and its quoted value), leading whitespace stripped, leading `#` stripped.

Also resolves `_OSS_SHARED`/`REVIEW_SKILL_DIR` (Agent Resolution above) and direct-report fast-path mode (a review-report `.md` path passed instead of a PR number) in the same call — no decision point separates them from flag parsing:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# loads: oss-shared-resolver.md
# loads: review-section-taxonomy.md
# loads: compaction-contract.md
# cold-start fallback (sets $_OSS_SHARED)
_OSS_SHARED=$(python "$PLUGIN_ROOT/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
# --reply needs $_OSS_SHARED (Step8 shepherd-reply-protocol.md); else degrades gracefully
if [ ! -d "$_OSS_SHARED" ]; then
    # Guard runs before the full flag parse below, so derive --reply the same way
    # rather than substring-testing the raw argument text.
    if python "$PLUGIN_ROOT/bin/parse-skill-flags.py" --flags reply -- "$ARGUMENTS" | grep -qx 'FLAG_REPLY=true'; then  # timeout: 5000
        echo "! BLOCKED — _OSS_SHARED resolved to '$_OSS_SHARED' but dir absent; --reply requires oss plugin shared dir, verify oss plugin installed"
        exit 1
    else
        echo "⚠ _OSS_SHARED resolved to '$_OSS_SHARED' but dir absent — continuing with degraded functionality (oss skill-specific shared helpers unavailable; --reply mode will not work in this run)"
    fi
fi
echo "$_OSS_SHARED" > "${TMPDIR:-/tmp}/review-oss-shared-${CSID}"  # cross-block (Check 41)
[ -d "$_OSS_SHARED" ] && cat "$_OSS_SHARED/agent-resolution.md"  # timeout: 5000

REVIEW_SKILL_DIR="$PLUGIN_ROOT/skills/review"
echo "$REVIEW_SKILL_DIR" > "${TMPDIR:-/tmp}/review-skill-dir-${CSID}"  # cross-block (Check 41)

# parses --reply/--no-challenge/--worktree/--full/--keep + --fill-gaps/--roles/--candidates values; codemap flags detected-only, re-derived independently below
# shared flag/--keep parser (C5; also resolve/analyse SKILL.md); --keep-state: no eval, one oss-review-<flag>-${CSID} sentinel per flag + clean-args + keep-items (compaction-contract.md §keep: semantics), owner-safe clear of a crashed run's stale contract (§Lifecycle)
python "$PLUGIN_ROOT/bin/parse-skill-flags.py" --flags reply,no-challenge,no-codemap,codemap,worktree,full --value-flags fill-gaps,roles,candidates --keep-state oss-review -- "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r FLAG_FULL < "${TMPDIR:-/tmp}/oss-review-full-${CSID}" 2>/dev/null || FLAG_FULL=false
IFS= read -r REPLY_MODE < "${TMPDIR:-/tmp}/oss-review-reply-${CSID}" 2>/dev/null || REPLY_MODE=false
IFS= read -r WT_ENABLED < "${TMPDIR:-/tmp}/oss-review-worktree-${CSID}" 2>/dev/null || WT_ENABLED=false
IFS= read -r FLAG_NO_CHALLENGE < "${TMPDIR:-/tmp}/oss-review-no-challenge-${CSID}" 2>/dev/null || FLAG_NO_CHALLENGE=false
IFS= read -r VALUE_FILL_GAPS < "${TMPDIR:-/tmp}/oss-review-fill-gaps-${CSID}" 2>/dev/null || VALUE_FILL_GAPS=""
IFS= read -r VALUE_ROLES < "${TMPDIR:-/tmp}/oss-review-roles-${CSID}" 2>/dev/null || VALUE_ROLES=""
IFS= read -r VALUE_CANDIDATES < "${TMPDIR:-/tmp}/oss-review-candidates-${CSID}" 2>/dev/null || VALUE_CANDIDATES=""
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
FANOUT_CAP=3; [ "$FLAG_FULL" = "true" ] && FANOUT_CAP=0  # 0 = no cap: all scope-preselected dimensions
[ "$FLAG_NO_CHALLENGE" = "true" ] && CHALLENGE_ENABLED=false || CHALLENGE_ENABLED=true
# fill-gaps values rewritten every run — a crashed fill-gaps run must never leak its plan into a plain review
printf '%s\n' "$VALUE_FILL_GAPS" > "${TMPDIR:-/tmp}/oss-review-fill-report-${CSID}"
printf '%s\n' "$VALUE_ROLES" > "${TMPDIR:-/tmp}/oss-review-fill-roles-${CSID}"
printf '%s\n' "$VALUE_CANDIDATES" > "${TMPDIR:-/tmp}/oss-review-fill-candidates-${CSID}"
: > "${TMPDIR:-/tmp}/oss-review-fill-plan-${CSID}"; : > "${TMPDIR:-/tmp}/oss-review-fill-head-${CSID}"
FILL_GAPS_MODE=false; [ -n "$VALUE_FILL_GAPS$VALUE_ROLES$VALUE_CANDIDATES" ] && FILL_GAPS_MODE=true
if [ "$FILL_GAPS_MODE" = "true" ]; then
    [ -n "$VALUE_FILL_GAPS" ] || { echo "! BLOCKED — --roles/--candidates need --fill-gaps <path/to/review-report.md>"; exit 1; }
    [ "$REPLY_MODE$FLAG_FULL" = "falsefalse" ] || { echo "! BLOCKED — --fill-gaps cannot combine with --reply or --full; drop one"; exit 1; }
fi
# stale contract already cleared by --keep-state above; the report-dir sentinel of a run
# that died between Step 2 and Step 5 makes enforce-review-header.js deny every AskUserQuestion (Gate A,
# the existing-report guard) for its 2h staleness window — this run has not reached Step 2, so it is stale.
# run-dir sentinel likewise: Agent 0's gather reads it, a stale one points at a previous review's run dir
rm -f "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}"  # timeout: 5000

# flags sentinel; CHALLENGE_ENABLED kept in its own sentinel so the challenge-skip fence can rewrite it alone
{
    echo "REPLY_MODE=$REPLY_MODE"
    echo "WT_ENABLED=$WT_ENABLED"
    echo "FILL_GAPS_MODE=$FILL_GAPS_MODE"
} > "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
echo "$CHALLENGE_ENABLED" > "${TMPDIR:-/tmp}/oss-review-challenge-enabled-${CSID}"
echo "$CLEAN_ARGS" > "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}"

DIRECT_PATH_MODE=false
if [[ "$CLEAN_ARGS" == *.md ]]; then
    # reject plan files — no replies drafted from plan content
    if [[ "$CLEAN_ARGS" == .plans/* ]] || [[ "$CLEAN_ARGS" == *todo_*.md ]]; then
        echo "Error: plan files cannot be used as review report input. Pass a review report from .reports/review/pr-<N>/run-<NNN>/review-report.md or a PR number."
        exit 1
    fi
    if [ -f "$CLEAN_ARGS" ] && grep -qE '^(Outcome: *(APPROVE|MINOR_EDITS|NEEDS_WORK|INSUFFICIENT_BUT_PROMISING|REJECT|REQUEST_CHANGES)|Gate: *(REVIEW_V1_)?(PASS|BLOCK|REJECT_[A-Z]+))([[:space:]]|$)' "$CLEAN_ARGS" 2>/dev/null; then  # timeout: 5000
        DIRECT_PATH_MODE=true
        REVIEW_FILE="$CLEAN_ARGS"
    else
        echo "⚠ $CLEAN_ARGS is a .md file but lacks an Outcome: or Gate: review header — refusing direct-path fast-path; continuing with normal review path which expects a PR number."
    fi
fi
{
    echo "DIRECT_PATH_MODE=$DIRECT_PATH_MODE"
    [ "$DIRECT_PATH_MODE" = "true" ] && echo "REVIEW_FILE=$REVIEW_FILE"
} >> "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
```

**Content-type pre-classification (PR mode only)** — skip when `DIRECT_PATH_MODE=true`.

Classify PR from changed file patterns. Default `PR_TYPE=CODE`; override only when unambiguous.

**PR snapshot — fetch once, reuse everywhere.** All later steps (pre-classification, Step 1 scope/CI, acceptance gate, codemap battery, Step 3 checks, signals script) read these files instead of re-calling `gh` — one consistent PR snapshot per run, ~10+ fewer network round-trips:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
if [ "$DIRECT_PATH_MODE" = "false" ] && [[ "$CLEAN_ARGS" =~ ^[0-9]+$ ]]; then
    SNAP_DIR="${TMPDIR:-/tmp}/oss-review-snap-${CLEAN_ARGS}-${CSID}"
    mkdir -p "$SNAP_DIR"
    gh pr view "$CLEAN_ARGS" --json number,title,body,url,labels,milestone,reviews,headRefOid,baseRefName,baseRefOid > "$SNAP_DIR/pr-meta.json" || { echo "! BLOCKED — gh pr view failed for PR #$CLEAN_ARGS; check network/auth, PR number, cwd repository and gh repo set-default"; exit 1; }  # timeout: 6000
    PR_HEAD_SHA=$(jq -er '.headRefOid | select(test("^[0-9a-f]{40}$"))' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — snapshot has no full PR head SHA"; exit 1; }
    _PR_URL=$(jq -er '.url' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — PR repository URL unavailable"; exit 1; }
    _PR_REPO_URL="${_PR_URL%/pull/*}.git"
    _PR_REPO_PATH="${_PR_URL#*://}"; _PR_REPO_PATH="${_PR_REPO_PATH#*/}"; _PR_REPO_PATH="${_PR_REPO_PATH%/pull/*}"
    _PR_SOURCE="$_PR_REPO_URL"
    while IFS= read -r _REMOTE; do
        _FETCH_URL=$(git remote get-url "$_REMOTE") || continue
        case "$_FETCH_URL" in *[:/]"$_PR_REPO_PATH"|*[:/]"$_PR_REPO_PATH.git") _PR_SOURCE="$_REMOTE"; break ;; esac
    done < <(git remote)
    printf '%s\n' "$_PR_SOURCE" > "$SNAP_DIR/repo-source.txt"
    git fetch --no-tags "$_PR_SOURCE" "refs/pull/$CLEAN_ARGS/head" || { echo "! BLOCKED — PR head fetch failed for $_PR_REPO_URL"; exit 1; }  # timeout: 120000
    FETCHED_HEAD=$(git rev-parse 'FETCH_HEAD^{commit}') || { echo "! BLOCKED — fetched PR commit unavailable"; exit 1; }
    [ "$FETCHED_HEAD" = "$PR_HEAD_SHA" ] || { echo "! BLOCKED — PR head moved during snapshot; restart Step 0"; exit 1; }
    if gh pr diff "$CLEAN_ARGS" > "$SNAP_DIR/pr.diff"; then  # timeout: 15000
        gh pr diff "$CLEAN_ARGS" --name-only > "$SNAP_DIR/files.txt" || { echo "! BLOCKED — changed-file read failed"; exit 1; }  # timeout: 6000
    else
        BASE_SHA=$(jq -er '.baseRefOid' "$SNAP_DIR/pr-meta.json") || { echo "! BLOCKED — snapshot base SHA unavailable"; exit 1; }
        git fetch --no-tags "$_PR_SOURCE" "$BASE_SHA" || { echo "! BLOCKED — base commit fetch failed for $_PR_REPO_URL"; exit 1; }  # timeout: 120000
        git diff "$BASE_SHA...$PR_HEAD_SHA" > "$SNAP_DIR/pr.diff" && git diff --name-only "$BASE_SHA...$PR_HEAD_SHA" > "$SNAP_DIR/files.txt" || { echo "! BLOCKED — local PR diff fallback failed"; exit 1; }
        echo "⚠ PR diff via local git (gh diff unavailable)"
    fi
    [ -s "$SNAP_DIR/pr.diff" ] && [ -s "$SNAP_DIR/files.txt" ] || { echo "! BLOCKED — PR diff or changed-file snapshot empty"; exit 1; }
    _REMOTE_HEAD=$(git ls-remote "$_PR_SOURCE" "refs/pull/$CLEAN_ARGS/head") || { echo "! BLOCKED — PR head recheck failed for $_PR_REPO_URL"; exit 1; }  # timeout: 15000
    [ "${_REMOTE_HEAD%%[[:space:]]*}" = "$PR_HEAD_SHA" ] || { echo "! BLOCKED — PR head moved while collecting diff and files; restart Step 0"; exit 1; }
    # Explicit no-checks diagnostics are different from failed reads; retain errors for diagnosis.
    for _CHECK_SET in checks checks-required; do
        _CHECK_FLAGS=(); [ "$_CHECK_SET" != "checks-required" ] || _CHECK_FLAGS=(--required)
        gh pr checks "$CLEAN_ARGS" "${_CHECK_FLAGS[@]}" --json name,bucket > "$SNAP_DIR/$_CHECK_SET.json" 2> "$SNAP_DIR/$_CHECK_SET.err"  # timeout: 15000
        _CHECK_RC=$?
        case "$_CHECK_RC" in
            0) ;;
            1|8)
                if jq -e 'type == "array"' "$SNAP_DIR/$_CHECK_SET.json" >/dev/null 2>&1; then
                    :  # Status exits with complete JSON still carry usable check evidence.
                elif grep -qE '^no (required )?checks reported on ' "$SNAP_DIR/$_CHECK_SET.err"; then
                    printf '[]\n' > "$SNAP_DIR/$_CHECK_SET.json"
                else
                    cat "$SNAP_DIR/$_CHECK_SET.err"; echo "! BLOCKED — CI $_CHECK_SET read failed"; exit 1
                fi ;;
            *) cat "$SNAP_DIR/$_CHECK_SET.err"; echo "! BLOCKED — CI $_CHECK_SET read failed"; exit 1 ;;
        esac
    done
    jq -se 'length == 2 and all(.[]; type == "array")' "$SNAP_DIR/checks.json" "$SNAP_DIR/checks-required.json" >/dev/null || { echo "! BLOCKED — invalid CI checks snapshot"; exit 1; }
    echo "$SNAP_DIR" > "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}"
fi
```

### Fill-gaps gate — `--fill-gaps` only, before the existing-report guard

<!-- loads: modes/fill-gaps.md -->

`FILL_GAPS_MODE=true` → load and follow `modes/fill-gaps.md`: its gate validates the selection against the prior run before any run-dir allocation or spawn, and it lists every later step that narrows. Any other run skips this section.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
[ "${FILL_GAPS_MODE:-false}" = "true" ] || exit 0
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$REVIEW_SKILL_DIR/modes/fill-gaps.md"  # timeout: 5000
```

### Existing-report guard — before Step 1, before any worktree

A full review is a multi-agent fan-out; re-running one over an unchanged PR head spends that cost for a report already on disk. Nothing else in this skill checks — a real session re-reviewed a PR minutes after reviewing it, because the prior run had been compacted out of context. Check disk, not memory, and do it here: nothing below Step 0 (codemap gates, worktree entry, CI status, the codemap battery) has run yet, so a reuse costs only the snapshot above.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 15000
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
# fill-gaps names its prior run explicitly; its gate already wrote the prior-resolution sentinel — never overwrite it
[ "${FILL_GAPS_MODE:-false}" = "false" ] || { echo "PRIOR_REPORT="; exit 0; }
_PRIOR=""
if [ "$DIRECT_PATH_MODE" = "false" ] && [[ "$CLEAN_ARGS" =~ ^[0-9]+$ ]]; then
    python "$PLUGIN_ROOT/bin/find_review_report.py" --pr "$CLEAN_ARGS" \
        --path-out "${TMPDIR:-/tmp}/oss-review-prior-report-${CSID}" >/dev/null  # stderr kept: a failed sentinel write must be visible
    IFS= read -r _PRIOR < "${TMPDIR:-/tmp}/oss-review-prior-report-${CSID}" 2>/dev/null || _PRIOR=""
fi
if [ -n "$_PRIOR" ]; then
    # head-sha.txt sidecar is written by the run-dir allocation fence (Step 2, also run on the Stage 1 reject path);
    # a report from before that fix has none, so fall back to the @<sha> its Gate: line carries
    IFS= read -r _PRIOR_SHA < "$(dirname "$_PRIOR")/head-sha.txt" 2>/dev/null || _PRIOR_SHA=""
    [ -n "$_PRIOR_SHA" ] || _PRIOR_SHA=$(grep -m1 '^Gate:' "$_PRIOR" 2>/dev/null | grep -oE '@[0-9a-f]{7,40}' | tr -d @)
    _HEAD_SHA=$(jq -r '.headRefOid // empty' "$SNAP_DIR/pr-meta.json" 2>/dev/null)
    echo "PRIOR_DATE=$(grep -m1 '^Date:' "$_PRIOR" 2>/dev/null | cut -d: -f2- | tr -d ' ')"
    echo "PRIOR_SHA=${_PRIOR_SHA:-unknown} HEAD_SHA=${_HEAD_SHA:-unknown}"
    # a Gate: line may carry a short SHA — prefix match, never string equality
    case "${_HEAD_SHA:-x}" in "${_PRIOR_SHA:-y}"*) echo "SHA_MATCH=true" ;; *) echo "SHA_MATCH=false" ;; esac
fi
# /oss:resolve appends what it fixed/rejected to resolution.jsonl beside the report it consumed; a re-run carries it forward
_PRIOR_RES=""
[ -n "$_PRIOR" ] && [ -s "$(dirname "$_PRIOR")/resolution.jsonl" ] && _PRIOR_RES="$(dirname "$_PRIOR")/resolution.jsonl"
printf '%s\n' "$_PRIOR_RES" > "${TMPDIR:-/tmp}/oss-review-prior-resolution-${CSID}"
echo "PRIOR_REPORT=$_PRIOR"
[ -z "$_PRIOR_RES" ] || echo "PRIOR_RESOLUTION=$_PRIOR_RES ($(grep -c . "$_PRIOR_RES") records)"
```

Empty `PRIOR_REPORT` → proceed, no gate. Non-empty → invoke `AskUserQuestion` (actual tool call) before anything else runs:

<!-- branch: prior-report — fires only when a report for this PR already exists on disk -->

```text
"Review report for PR #<N> already exists (<PRIOR_DATE>). Re-run the full fan-out?"
  (a) Re-run full review — PR head moved, or the prior report is stale
  (b) Reuse it — print its path and stop; nothing to re-review  (Recommended when PRIOR_SHA = HEAD_SHA)
  (c) Reply-draft from the existing report — jumps to Step 8 with --reply
```

`SHA_MATCH=true` (prior SHA equals, or is a prefix of, the current head) means the prior report covers exactly this code; say so in the question text. Unknown on either side (no `head-sha.txt` sidecar and no `Gate: … @<sha>` line, or `gh` returned no `headRefOid`) → state that instead of guessing, and let the user decide.

Selected (a) → proceed with the full review. Selected (b) → `TaskUpdate(status="deleted")` for every Step 2–5b task already created, print `→ existing report: <PRIOR_REPORT>` and stop. Selected (c) → delete those same tasks, persist the redirect so Step 8 and a post-compaction resume both see it, then skip to Step 8:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_FILE < "${TMPDIR:-/tmp}/oss-review-prior-report-${CSID}" 2>/dev/null || REVIEW_FILE=""
# later lines win when the sentinel is read back — the Step 0 REPLY_MODE=false line stays, this overrides it
{
    echo "REPLY_MODE=true"
    echo "REVIEW_FILE=$REVIEW_FILE"
} >> "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
echo "REVIEW_FILE=$REVIEW_FILE"  # timeout: 3000
```

Step 8 reads `REVIEW_FILE` from the flags sentinel whenever it is set there — this redirect and the direct-report fast path share that source; only an unset value falls back to Step 5's output file.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
PR_TYPE="CODE"
DOCS_TYPING_MODE=false; TESTS_CI_MODE=false
if [ "$DIRECT_PATH_MODE" = "false" ] && [[ "$CLEAN_ARGS" =~ ^[0-9]+$ ]]; then
    _CHANGED=$(cat "$SNAP_DIR/files.txt" 2>/dev/null)
    # no `|| echo 0`: grep -c already prints 0 & exits 1 — fallback would double it to "0\n0", breaking `-eq 0` tests below
    # test patterns anchored to a path segment: unanchored `test_` made latest_version.py a test file
    _PY_LOGIC_COUNT=$(echo "$_CHANGED" | grep -E '\.py$' | grep -cvE '((^|/)test_[^/]*|_test|(^|/)conftest)\.py$|(^|/)tests?/' 2>/dev/null)
    _ALL_COUNT=$(echo "$_CHANGED" | grep -c . 2>/dev/null)
    # docs = md|rst only: .txt (requirements manifests) and .ipynb (executable) stay CODE → qa/OWASP path
    _DOC_COUNT=$(echo "$_CHANGED" | grep -cE '\.(md|rst|pyi)$' 2>/dev/null)
    # no .github/ or YAML: Actions changes route via scope detection to oss:cicd-steward, with CI status, acceptance gate and challenger
    _TEST_CI_COUNT=$(echo "$_CHANGED" | grep -cE '((^|/)test_[^/]*|_test|(^|/)conftest)\.py$|(^|/)tests?/|tox\.ini|Makefile' 2>/dev/null)

    if [ "${_PY_LOGIC_COUNT:-0}" -eq 0 ] && [ "${_ALL_COUNT:-0}" -gt 0 ]; then
        if [ "$_DOC_COUNT" -ge "$_ALL_COUNT" ]; then
            PR_TYPE="DOCS_TYPING"; DOCS_TYPING_MODE=true
        elif [ "$(( _TEST_CI_COUNT + _DOC_COUNT ))" -ge "$_ALL_COUNT" ]; then
            PR_TYPE="TESTS_CI"; TESTS_CI_MODE=true
        fi
    fi
    echo "→ PR_TYPE=$PR_TYPE (_py_logic=$_PY_LOGIC_COUNT, _all=$_ALL_COUNT)"
fi
# persist PR_TYPE/mode flags (Check 41) — reloaded by Steps 2/5
{
    echo "PR_TYPE=$PR_TYPE"
    echo "DOCS_TYPING_MODE=$DOCS_TYPING_MODE"
    echo "TESTS_CI_MODE=$TESTS_CI_MODE"
} > "${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}"
# challenge skip — challenger adds no value for non-logic PRs
IFS= read -r CHALLENGE_ENABLED < "${TMPDIR:-/tmp}/oss-review-challenge-enabled-${CSID}" 2>/dev/null; [ "$CHALLENGE_ENABLED" = "false" ] || CHALLENGE_ENABLED=true
if [ "$PR_TYPE" = "DOCS_TYPING" ] || [ "$PR_TYPE" = "TESTS_CI" ]; then
    CHALLENGE_ENABLED=false
fi
echo "$CHALLENGE_ENABLED" > "${TMPDIR:-/tmp}/oss-review-challenge-enabled-${CSID}"
```

Agent lineup — `PR_TYPE != CODE` overrides scope-based rules in Step 1:

| `PR_TYPE` | Agents | Challenger | Consolidator |
| -- | -- | -- | -- |
| `DOCS_TYPING` | `foundry:doc-scribe` only | skip | `foundry:doc-scribe` |
| `TESTS_CI` | `foundry:qa-specialist` + `foundry:linting-expert` | skip | `foundry:qa-specialist` |
| `CODE` | full scope-based lineup | per `--no-challenge` | `foundry:sw-engineer` |

When `DOCS_TYPING_MODE=true` or `TESTS_CI_MODE=true`: skip Step 1's File scope detection, Scope pre-check, SCOPE classification and codemap sections; every other Step 1 block still runs — notably CI STATUS and the acceptance gate, since a red required check that never reaches Stage 2 leaves Step 5 defaulting to `Gate: REVIEW_V1_PASS`. Then proceed to Step 2 agent launch.
