<!-- file: gather-changes.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:release -->

## Gather changes

Find common base tag across ALL branches via `git tag --list` sorted by version, then `git merge-base --end-of-options HEAD <tag-commit>`. Use as range lower bound when current branch has no direct tag ancestry.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload Shared-setup vars (Check 41)
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
IFS= read -r CHERRY_PICK_SUBJECTS < "${TMPDIR:-/tmp}/release-setup-${CSID}/CHERRY_PICK_SUBJECTS" 2>/dev/null || CHERRY_PICK_SUBJECTS=""
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r BRANCH_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_REF" 2>/dev/null || BRANCH_REF=""
IFS= read -r BRANCH_KEY < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_KEY" 2>/dev/null || BRANCH_KEY=""
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE=""
# explicit v1->v2 from Mode detection; empty → marker/LAST_TAG default below
[ -n "$RANGE" ] || IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
if [ -z "$RANGE" ] && [ "$DO_APPEND" = "true" ]; then
    # marker present+valid -> "<sha>..HEAD"; else "$LAST_TAG..HEAD" (non-append default)
    RANGE=$(python "$PLUGIN_ROOT/bin/release_append_marker.py" resolve --branch "$BRANCH_REF" --last-tag "$LAST_TAG")  # timeout: 5000
else
    RANGE="${RANGE:-$LAST_TAG..HEAD}"
fi
[ -z "$RANGE" ] && echo "Error: could not determine commit range" && exit 1
if [ "$RELEASE_MODE" = notes ]; then
FROZEN_HEAD=$(git rev-parse --verify 'HEAD^{commit}' 2>/dev/null) || { echo "Error: notes HEAD unresolved" >&2; exit 1; }
FROZEN_BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null) || { echo "Error: notes branch unresolved" >&2; exit 1; }
[ -n "$BRANCH_REF" ] && [ "$FROZEN_BRANCH" = "$BRANCH_REF" ] || { echo "Error: notes branch changed after release setup" >&2; exit 1; }
FROZEN_TAGS_FILE="${TMPDIR:-/tmp}/release-append-tags-${CSID}"
git for-each-ref --sort=refname --format='%(refname) %(objectname)' refs/tags > "$FROZEN_TAGS_FILE" || { echo "Error: notes tag refs unresolved" >&2; exit 1; }
case "$RANGE" in *...*|*..*..*) echo "Error: notes range must have one two-dot endpoint" >&2; exit 1;; *..*) ;; *) echo "Error: notes range must have one two-dot endpoint" >&2; exit 1;; esac
RANGE_START="${RANGE%..*}"
RANGE_END="${RANGE##*..}"
FROZEN_START=$(git rev-parse --verify "$RANGE_START^{commit}" 2>/dev/null) || { echo "Error: notes range start unresolved" >&2; exit 1; }
FROZEN_END=$(git rev-parse --verify "$RANGE_END^{commit}" 2>/dev/null) || { echo "Error: notes range endpoint unresolved" >&2; exit 1; }
git merge-base --is-ancestor --end-of-options "$FROZEN_START" "$FROZEN_END" || { echo "Error: notes range start is not an ancestor of endpoint" >&2; exit 1; }
git merge-base --is-ancestor --end-of-options "$FROZEN_END" "$FROZEN_HEAD" || { echo "Error: notes range endpoint is not on gathered HEAD" >&2; exit 1; }
if [ "$DO_APPEND" = "true" ]; then
    [ "$FROZEN_END" = "$FROZEN_HEAD" ] || { echo "Error: append range does not end at frozen HEAD" >&2; exit 1; }
fi
if [ "$(python "$PLUGIN_ROOT/bin/release_append_marker.py" is-valid --branch "$BRANCH_REF" --last-tag "$LAST_TAG")" = true ]; then
    IFS= read -r SAVED_MARKER < ".temp/release-state-v2/$BRANCH_KEY/marker" || { echo "Error: saved marker unreadable" >&2; exit 1; }
    if [ "$DO_APPEND" = "true" ]; then
        [ "$FROZEN_START" = "$SAVED_MARKER" ] || { echo "Error: append range does not start at saved marker" >&2; exit 1; }
    else
        git merge-base --is-ancestor --end-of-options "$FROZEN_START" "$SAVED_MARKER" || { echo "Error: plain notes range skips commits after saved marker" >&2; exit 1; }
        git merge-base --is-ancestor --end-of-options "$SAVED_MARKER" "$FROZEN_END" || { echo "Error: plain notes range ends before saved marker" >&2; exit 1; }
    fi
fi
RANGE="$FROZEN_START..$FROZEN_END"
printf '%s\n' "$FROZEN_HEAD" > "${TMPDIR:-/tmp}/release-append-head-${CSID}"
printf '%s\n' "$FROZEN_START" > "${TMPDIR:-/tmp}/release-append-start-${CSID}"
printf '%s\n' "$FROZEN_END" > "${TMPDIR:-/tmp}/release-append-end-${CSID}"
printf '%s\n' "$FROZEN_BRANCH" > "${TMPDIR:-/tmp}/release-append-branch-${CSID}"
fi
if [ "$DO_APPEND" = "true" ] && [ "$(git rev-list --count --end-of-options "$RANGE" 2>/dev/null)" = 0 ]; then
    echo "No new commits to append; release artifacts unchanged. Stop this invocation."
    exit 0
fi
# persist (Check 41)
echo "${RANGE:-}" > "${TMPDIR:-/tmp}/release-range-${CSID}"

# quote RANGE — tags may carry unusual chars (e.g. v1.2-rc.1+build.42)
git log --oneline --no-merges --end-of-options "$RANGE" # timeout: 3000
git log --no-merges --format="--- %H%n%B" --end-of-options "$RANGE" # timeout: 3000
git diff --stat --end-of-options "$RANGE" # timeout: 3000

# prefer gh; fallback git remote show origin; never hardcode main
TRUNK=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null)  # timeout: 6000
if [ -z "$TRUNK" ]; then
    TRUNK=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | { read -r _ _ val; echo "$val"; })  # timeout: 5000
fi
PR_FIELDS="number,title,body,labels,mergedAt,author"
if [ -n "$TRUNK" ]; then
    PRS=$(gh pr list --state merged --base "$TRUNK" --limit 500 --json "$PR_FIELDS") || { echo "Error: gh pr list failed; cannot classify release PRs" >&2; exit 1; }  # timeout: 15000
else
    echo "⚠ Could not detect default branch — listing all merged PRs"
    PRS=$(gh pr list --state merged --limit 500 --json "$PR_FIELDS") || { echo "Error: gh pr list failed; cannot classify release PRs" >&2; exit 1; }  # timeout: 15000
fi
PR_COUNT=$(printf '%s' "$PRS" | jq 'length') || { echo "Error: could not count gh pr list results" >&2; exit 1; }
if [ "$PR_COUNT" -eq 500 ]; then
    echo "⚠ gh pr list reached its 500-item cap; retrying discovery within the release date window"
    PR_SINCE=$(TZ=UTC git log --date=format-local:%Y-%m-%d --format=%cd --end-of-options "$RANGE" | sort | head -1)
    if [ -n "$PR_SINCE" ]; then
        PR_RECOVERY_ARGS=(pr list --state merged --limit 500 --json "$PR_FIELDS" --search "merged:>=$PR_SINCE")
        if [ -n "$TRUNK" ]; then
            PR_RECOVERY_ARGS+=(--base "$TRUNK")
        fi
        if RECOVERED_PRS=$(gh "${PR_RECOVERY_ARGS[@]}"); then  # timeout: 60000
            PRS="$RECOVERED_PRS"
            PR_COUNT=$(printf '%s' "$PRS" | jq 'length') || { echo "Error: could not count bounded PR recovery" >&2; exit 1; }
        else
            echo "⚠ PR discovery coverage gap: bounded recovery failed; retaining the initial contextual list" >&2
        fi
    fi
    if [ "$PR_COUNT" -eq 500 ]; then
        echo "⚠ PR discovery coverage gap: list remains capped; use per-commit associations below for release membership and credits" >&2
    fi
fi
printf '%s\n' "$PRS"
```

Record any printed `⚠ PR discovery coverage gap` in the gather evidence and list it as a Confidence Gap in the final reply for both `notes` and `prepare`; for every associated PR missing from `$PRS`, fetch its body with `gh pr view <N> --json body` before classifying it.

Cross-reference commit bodies against PR descriptions — canonical source of truth for *why* change made. `BREAKING CHANGE:` footer = breaking change regardless of PR label.

**Subtract work already shipped off-branch.** `SHIPPED_ELSEWHERE_TAGS` (Shared setup) lists stable tags newer than `$LAST_TAG` that descend from it but are not reachable from HEAD — a maintenance branch's 1.11.1 and 1.11.2 next to HEAD's 1.11.0. `$LAST_TAG..HEAD` still contains what those tags backported, so it would be announced as new. Empty list → nothing to do, `$SHIPPED_FILE` stays empty.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
IFS= read -r SHIPPED_TAGS < "${TMPDIR:-/tmp}/release-setup-${CSID}/SHIPPED_ELSEWHERE_TAGS" 2>/dev/null || SHIPPED_TAGS=""
SHIPPED_FILE="${TMPDIR:-/tmp}/release-shipped-${CSID}"
: > "$SHIPPED_FILE"
[ -z "$SHIPPED_TAGS" ] || python "$PLUGIN_ROOT/bin/release_shipped_elsewhere.py" --range "$RANGE" --tags "$SHIPPED_TAGS" --out "$SHIPPED_FILE" || { echo "! BLOCKED — shipped-elsewhere subtraction failed; do not claim the range holds only new work" >&2; exit 1; }  # timeout: 90000
git log --no-merges --format='%H%x09%s' --end-of-options "$RANGE" | awk -F'\t' 'NR==FNR{if($2=="shipped")x[$1]=1;next}!($1 in x)' "$SHIPPED_FILE" -  # timeout: 5000
```

The last command prints the commits that are new to this release; use it, not the unfiltered `git log` above, as the candidate set for classification, contributors, counts and breaking-change checks. `$SHIPPED_FILE` rows (`sha`, category, tag, evidence): `shipped` = patch-equivalent in the older tag (`git cherry`) with no revert on the release side — dropped; record them as "already shipped in `<tag>`" in the gather evidence and never credit their authors. `review` = same `(#N)` PR number in the tag, or a revert on the release side — kept as new until verified: confirm the change exists in the released tree (`git show --end-of-options <tag>:<file>` against the commit's diff); present → treat as shipped, absent → keep. Subject, title or PR-number matches alone never subtract. Report the counts (`candidates`, `shipped`, `review`) in the report's scope line; unresolved `review` rows are a stated limit.

The default-branch list is discovery context, not the contributor inventory. Query every candidate commit independently of the default branch so maintenance-branch and squash-only PR authors cannot disappear:

```bash
# Commit association follows the release range, including maintenance-branch merges.
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -n "$RANGE" ] || IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
[ -n "$RANGE" ] || { echo "Error: release range unresolved; cannot enumerate candidate commits" >&2; exit 1; }
python "$PLUGIN_ROOT/bin/gh_batch_query.py" commit-prs --range "$RANGE" --exclude-file "${TMPDIR:-/tmp}/release-shipped-${CSID}" --offset-file "${TMPDIR:-/tmp}/release-assoc-offset-${CSID}" || { echo "Error: PR association coverage unavailable; retain gap, do not claim complete credits" >&2; exit 1; }  # timeout: 180000
```

One batched GraphQL request covers 25 commits (500 per run, about 20 requests). stderr printing `NEXT_OFFSET=<n>` → re-run this exact block until it stops printing it; association coverage is incomplete until then. Each output line is one commit's record `{commit, pull_requests: [{number, title, author, base, merge_commit_sha}]}`, merged PRs only; a `coverage_gap` field (commit unknown to GitHub, or more than 100 associated PRs) is an explicit coverage gap, never an empty association.

Retain each commit's association result, including empty results, in gathered evidence. Fetch the associated PR's metadata when needed to verify repository and commit/diff membership; do not discard an association because its base is not the default branch. Empty successful lookup means no associated merged PR was returned, not a fabricated author; keep Git authors/coauthors. Failed lookup, deleted author, or ambiguous association remains an explicit gap. Do not silently continue with the default-branch list after an association failure.

Retain verified commit-to-PR associations and each in-range PR author's known login/display name for Extract contributors, including PR-only authors of maintainer-authored squash commits. Preserve this identity inventory through classification, including grouped or net-zero changes; record missing association/identity metadata as contributor-coverage gaps. When delegated, write this evidence to `$GATHER_FILE`; merge dates and membership in the collected PR list alone do not establish release-range membership.

**Detect revert pairs**: scan `git log --no-merges --format="%H %s" --end-of-options "$RANGE"` for revert subjects — those beginning with `Revert "` (git default) or a conventional `revert:` / `revert(<scope>):` type, case-insensitive. For each: match the original by the body's `This reverts commit <sha>` trailer first; no trailer → extract the original subject (the quoted text, or the text after the type prefix) and search the range for it. Both found → `REVERT_SET` pair (net effect zero).

Record all `REVERT_SET` pairs before Classify. Commits in `REVERT_SET` excluded from standard sections; collected for 🔄 Reverted. If only revert is in range (original predates range) → classify as ❌ Removed (or ⚠️ Breaking Changes if API surface changed without prior deprecation) — NOT 🔄 Reverted; net user effect is non-zero.

**Cross-cycle revert/pivot detection** (`--append` only, when a prior `DRAFT.md`/`$CHANGELOG_FILE` exists — extends the "non-destructive except revert/pivot" rule across append cycles, not just within one range). Runs in the orchestrator after Detect revert pairs. Load the procedure and the Append notes only for an `--append` run:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
[ "$DO_APPEND" = "true" ] || exit 0
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/append-cross-cycle.md"  # timeout: 5000
```

<!-- loads: modes/append-cross-cycle.md -->

Follow above and execute. Collect all `CROSS_CYCLE_MATCH` entries for Audit changelog and Write release draft to consume.
