export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# loads: oss-shared-resolver.md
# loads: review-section-taxonomy.md
# loads: compaction-contract.md
# cold-start fallback (sets $_OSS_SHARED)
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
# --reply needs $_OSS_SHARED (Step8 shepherd-reply-protocol.md); else degrades gracefully
if [ ! -d "$_OSS_SHARED" ]; then
    # Guard runs before the full flag parse below, so derive --reply the same way
    # rather than substring-testing the raw argument text.
    eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags reply "$ARGUMENTS")"  # timeout: 5000
    if [ "$FLAG_REPLY" = "true" ]; then
        echo "⛔ _OSS_SHARED resolved to '$_OSS_SHARED' but dir absent — --reply requires oss plugin shared dir; verify oss plugin installed"
        exit 1
    else
        echo "⚠ _OSS_SHARED resolved to '$_OSS_SHARED' but dir absent — continuing with degraded functionality (oss skill-specific shared helpers unavailable; --reply mode will not work in this run)"
    fi
fi
echo "$_OSS_SHARED" > "${TMPDIR:-/tmp}/review-oss-shared-${CSID}"  # cross-block (Check 41)
[ -d "$_OSS_SHARED" ] && cat "$_OSS_SHARED/agent-resolution.md"  # timeout: 5000

REVIEW_SKILL_DIR="${CLAUDE_PLUGIN_ROOT:-}/skills/review"
[ -d "$REVIEW_SKILL_DIR" ] || REVIEW_SKILL_DIR=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/review 2>/dev/null | head -1)
[ -z "$REVIEW_SKILL_DIR" ] && REVIEW_SKILL_DIR="plugins/cc_oss/skills/review"
echo "$REVIEW_SKILL_DIR" > "${TMPDIR:-/tmp}/review-skill-dir-${CSID}"  # cross-block (Check 41)

# parses --reply/--no-challenge/--worktree/--full/--keep + --fill-gaps/--roles/--candidates values; codemap flags detected-only, re-derived independently below
# shared flag/--keep parser (C5; also resolve/analyse SKILL.md)
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags reply,no-challenge,no-codemap,codemap,worktree,full --value-flags fill-gaps,roles,candidates "$ARGUMENTS")"  # timeout: 5000
FANOUT_CAP=3; [ "$FLAG_FULL" = "true" ] && FANOUT_CAP=0  # 0 = no cap: all scope-preselected dimensions
REPLY_MODE="$FLAG_REPLY"
WT_ENABLED="$FLAG_WORKTREE"
[ "$FLAG_NO_CHALLENGE" = "true" ] && CHALLENGE_ENABLED=false || CHALLENGE_ENABLED=true
# fill-gaps values rewritten every run — a crashed fill-gaps run must never leak its plan into a plain review
printf '%s\n' "$VALUE_FILL_GAPS" > "${TMPDIR:-/tmp}/oss-review-fill-report-${CSID}"
printf '%s\n' "$VALUE_ROLES" > "${TMPDIR:-/tmp}/oss-review-fill-roles-${CSID}"
printf '%s\n' "$VALUE_CANDIDATES" > "${TMPDIR:-/tmp}/oss-review-fill-candidates-${CSID}"
: > "${TMPDIR:-/tmp}/oss-review-fill-plan-${CSID}"; : > "${TMPDIR:-/tmp}/oss-review-fill-head-${CSID}"
FILL_GAPS_MODE=false; [ -n "$VALUE_FILL_GAPS$VALUE_ROLES$VALUE_CANDIDATES" ] && FILL_GAPS_MODE=true
if [ "$FILL_GAPS_MODE" = "true" ]; then
    [ -n "$VALUE_FILL_GAPS" ] || { echo "! BLOCKED — --roles/--candidates need --fill-gaps <path/to/review-report.md>"; exit 1; }
    [ "$FLAG_REPLY$FLAG_FULL" = "falsefalse" ] || { echo "! BLOCKED — --fill-gaps cannot combine with --reply or --full; drop one"; exit 1; }
fi
# stale contract, crashed prior run (compaction-contract.md §Lifecycle); the report-dir sentinel of a run
# that died between Step 2 and Step 5 makes enforce-review-header.js deny every AskUserQuestion (Gate A,
# the existing-report guard) for its 2h staleness window — this run has not reached Step 2, so it is stale
rm -f .temp/state/skill-contract.md "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}"  # timeout: 5000

# flags sentinel; CHALLENGE_ENABLED kept in its own sentinel so the challenge-skip fence can rewrite it alone
{
    echo "REPLY_MODE=$REPLY_MODE"
    echo "WT_ENABLED=$WT_ENABLED"
    echo "FILL_GAPS_MODE=$FILL_GAPS_MODE"
} > "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
echo "$CHALLENGE_ENABLED" > "${TMPDIR:-/tmp}/oss-review-challenge-enabled-${CSID}"
echo "$CLEAN_ARGS" > "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}"
echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/oss-review-keep-items-${CSID}"  # timeout: 5000

DIRECT_PATH_MODE=false
if [[ "$CLEAN_ARGS" == *.md ]]; then
    # reject plan files — no replies drafted from plan content
    if [[ "$CLEAN_ARGS" == .plans/* ]] || [[ "$CLEAN_ARGS" == *todo_*.md ]]; then
        echo "Error: plan files cannot be used as review report input. Pass a review report from .reports/review/pr-<N>/run-<NNN>/review-report.md or a PR number."
        exit 1
    fi
    if [ -f "$CLEAN_ARGS" ] && grep -qE '(^## Summary|^verdict:|APPROVE|MINOR_EDITS|NEEDS_WORK|INSUFFICIENT_BUT_PROMISING|REJECT|REQUEST_CHANGES)' "$CLEAN_ARGS" 2>/dev/null; then  # timeout: 5000
        DIRECT_PATH_MODE=true
        REVIEW_FILE="$CLEAN_ARGS"
    else
        echo "⚠ $CLEAN_ARGS is a .md file but lacks review-report markers (## Summary | verdict: | APPROVE|MINOR_EDITS|NEEDS_WORK|INSUFFICIENT_BUT_PROMISING|REJECT|REQUEST_CHANGES) — refusing direct-path fast-path; continuing with normal review path which expects a PR number."
    fi
fi
{
    echo "DIRECT_PATH_MODE=$DIRECT_PATH_MODE"
    [ "$DIRECT_PATH_MODE" = "true" ] && echo "REVIEW_FILE=$REVIEW_FILE"
} >> "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
