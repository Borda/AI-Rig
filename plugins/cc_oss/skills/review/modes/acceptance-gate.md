<!-- Consumer: plugins/cc_oss/skills/review/SKILL.md (Step 1); fragment executed inline. -->

### Acceptance gate (PR mode only) — validate reject, then block

> **Why this gate exists**: Step 2's fanout runs up to ~8 spawns at the Spawn-count gate's per-agent overhead under `--full` (5 units + pinned qa + issue agent + Agent 0; the bridge review is a `Skill()` call, not a spawn) — never spend that on a PR whose premise is already fatal. Gate must stay cheap (a `gh pr view` + at most one `foundry:challenger` call) — never grow it into anything resembling the full fanout it exists to avoid paying for.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r PR_LABELS < "${TMPDIR:-/tmp}/oss-review-pr-labels-${CSID}" 2>/dev/null || PR_LABELS=""
CHANGED_FILES=$(cat "${TMPDIR:-/tmp}/oss-review-changed-files-${CSID}" 2>/dev/null)  # multi-line list: a sentinel read keeps line 1 only, the revert scan would cover one file
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
PR_BODY=$(jq -r '.body // ""' "$SNAP_DIR/pr-meta.json" 2>/dev/null)  # timeout: 5000
PR_HEAD_SHA=$(jq -r '.headRefOid // ""' "$SNAP_DIR/pr-meta.json" 2>/dev/null)  # timeout: 5000
echo "$PR_BODY" > "${TMPDIR:-/tmp}/oss-review-pr-body-${CSID}"

# cheap mechanical signals for grounds 3/5/6 below — reuses data already fetched, one small gh call per linked issue
SCOPE_LABEL_HIT=false
case ",${PR_LABELS}," in *,wontfix,*|*,invalid,*|*,declined,*|*,out-of-scope,*) SCOPE_LABEL_HIT=true ;; esac

DUPLICATE_HIT=false
# same regex as Agent 0's gather; ISSUE_NUMS is assigned here, never in prose — an unset list leaves REJECT_DUPLICATE unreachable
ISSUE_NUMS=$(printf '%s' "$PR_BODY" | grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?) #[0-9]+' | grep -oE '[0-9]+' | sort -u | head -3)
echo "issue_nums:" $ISSUE_NUMS
SOLVING_ISSUES=$(printf '%s' "$PR_BODY" | grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?) #[0-9]+' | grep -oE '[0-9]+' | sort -u | head -3)
_OWNER=$(jq -r '.url | split("/")[3]' "$SNAP_DIR/pr-meta.json")
_REPO=$(jq -r '.url | split("/")[4]' "$SNAP_DIR/pr-meta.json")
_PR_URL=$(jq -r '.url' "$SNAP_DIR/pr-meta.json")
while IFS= read -r N; do
    [ -n "$N" ] || continue
    if gh api graphql --paginate -f owner="$_OWNER" -f repo="$_REPO" -F number="$N" -f query='query($owner:String!,$repo:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$repo){issue(number:$number){url state closedByPullRequestsReferences(first:100,after:$endCursor,includeClosedPrs:true){nodes{url state mergeCommit{oid}} pageInfo{hasNextPage endCursor}}}}}' > "$SNAP_DIR/issue-$N-closers.json"; then  # timeout: 6000
        _CLOSERS=$(jq -r --arg self "$_PR_URL" '.data.repository.issue | select(.state=="CLOSED" and .url) | .url as $issue | .closedByPullRequestsReferences.nodes[] | select(.state=="MERGED" and .url and .url!=$self) | [$issue,.url,.mergeCommit.oid] | @tsv' "$SNAP_DIR/issue-$N-closers.json")
        [ -n "$_CLOSERS" ] && { DUPLICATE_HIT=true; printf '%s\n' "$_CLOSERS"; }
    else
        echo "⚠ Duplicate check unavailable for issue #$N — do not infer a merged solution"
    fi
done <<< "$SOLVING_ISSUES"

REVERT_CANDIDATE=""
_CHANGED_PATHS=()
while IFS= read -r _PATH; do [ -z "$_PATH" ] || _CHANGED_PATHS+=("$_PATH"); done < "$SNAP_DIR/files.txt"
if [ "${#_CHANGED_PATHS[@]}" -gt 0 ]; then
    REVERT_CANDIDATE=$(git --literal-pathspecs log --all --max-count=3 --grep='^Revert' --oneline -- "${_CHANGED_PATHS[@]}") || { echo "! BLOCKED — revert history read failed"; exit 1; }  # timeout: 10000
fi
echo "scope_label=$SCOPE_LABEL_HIT duplicate=$DUPLICATE_HIT revert_candidate=${REVERT_CANDIDATE:+yes}"
```

**Description drift caution** — `PR_BODY` is a snapshot written at PR-open time; it drifts from what diff actually does as commits land (further changes, or fixes pushed in response to earlier review feedback) and nobody edits description to match. Judge every ground below against **current diff behavior**, not stated text alone — read `CHANGED_FILES`/diff intent (already fetched in Step 0/1) alongside `PR_BODY`. Body says one thing, diff does another → trust diff; a stale description is not itself a reject ground, note mismatch in `Summary:` if material.

**Stage 1 — Reject (terminal).** Eight grounds — aligned with close-without-merge practice in K8s/CPython/Rust/Django contributing docs. Every ground needs affirmative evidence, never suspicion alone — disagreement-with-approach is a `MAJOR_REVISION`/`[blocking]` finding, stage 2 or full review territory, never a reject. Grounds 1–2 already had detail; 3–8 are the agreed expansion:

1. **REJECT_GOAL** — stated goal factually/technically wrong even if well-intentioned. Test: does the goal — read from `PR_BODY`, cross-checked per the drift caution above — contradict a known invariant, spec, or domain fact — e.g. "raise this accuracy metric above 1.0" when the metric is bounded `[0,1]` by definition, goal is unreachable no matter how the code changes. Judge from PR description + package docs/spec, not the diff's mechanics. Orchestrator judgment only — no agent spawn.
2. **REJECT_CONDUCT** — contribution by design adversarial, malicious, or a Code of Conduct violation (not an accidental bug). Never reject on suspicion alone: requires the `foundry:challenger` confirmation below.
3. **REJECT_SCOPE** — out of project scope / against roadmap, maintainers already decided against this direction. Evidence: `SCOPE_LABEL_HIT=true` (maintainer already triaged `wontfix`/`invalid`/`declined`/`out-of-scope`), or an explicit "out of scope" statement in `CONTRIBUTING.md`/an ADR that the PR's stated intent directly matches — grep for it, don't assume. No documented evidence → not a reject, at most a `MAJOR_REVISION` scope concern. Orchestrator judgment only.
4. **REJECT_LICENSE** — license/provenance conflict: incompatible license copied in (e.g. GPL source pasted into a permissive-licensed project), or plagiarized/copied source the contributor has no right to submit. Not the same as a missing CLA/DCO signature — that's Stage 2 `[blocking]`, fixable by signing; this is the source itself being unlicensable. Requires the `foundry:challenger` confirmation below when suspected (explicit "ported from `<project>`" in `PR_BODY`, or a license header in the diff that conflicts with this repo's license).
5. **REJECT_DUPLICATE** — `DUPLICATE_HIT=true` identifies a closed issue and its merged closer for comparison. Read the earlier change at the returned merge commit or PR URL and compare its observable behavior with the frozen `pr.diff`. Confirm this ground only when the earlier change already delivers the proposed behavior. Partial overlap, a distinct unresolved behavior, or unavailable comparison evidence goes to full review.
6. **REJECT_REVERTED** — reintroduces a previously reverted change without addressing why it was reverted. Evidence: `REVERT_CANDIDATE` non-empty (a prior revert touched the same files) **and** `PR_BODY` doesn't reference or address that revert/its reason — a candidate alone is not enough, read the revert commit message and compare intent before rejecting. Orchestrator judgment only.
7. **REJECT_SPAM** — spam/low-effort/AI-slop: no real change, hacktoberfest-farming pattern. Evidence needs both: diff is trivially low-value (whitespace/punctuation-only across the changed lines, no logic touched) **and** `PR_BODY` is generic/templated with no specifics tying it to this repo. Either alone is not enough — a genuine one-line critical fix is low-value-looking but not spam; judge the pairing, not the diff size alone. Orchestrator judgment only.
8. **REJECT_PHILOSOPHY** — contradicts a documented design principle (not a style preference). Evidence: an explicit principle stated in `README.md`/`CONTRIBUTING.md`/an ADR that the PR's intent directly violates — e.g. adding a GUI to a project whose docs state "CLI-only by design". Cite the exact doc line in `Summary:` — no citable line, no reject. Orchestrator judgment only.

**Challenger confirmation** (grounds 2 and 4 only — the two where accusing wrongdoing carries real reputational/legal stakes, so both share one call): only spawn when the orchestrator's own read of `PR_BODY`/diff/`CHANGED_FILES` raised a concrete suspicion for either — never spawn speculatively on every PR.

Prompt: "Investigate PR #<N> (body: \<PR_BODY>, diff: changed files) for two things: (1) is its intent a by-design malicious/adversarial contribution or Code of Conduct violation, vs. an accidental mistake; (2) is any changed content plagiarized or under an incompatible license the contributor has no right to submit, vs. original/properly licensed work. Read the diff and linked issue if any. Return ONLY: `{\"conduct\":{\"verdict\":\"BY_DESIGN\"|\"ACCIDENTAL\"|\"N/A\",\"confidence\":0.N},\"license\":{\"verdict\":\"CONFLICT\"|\"CLEAN\"|\"N/A\",\"confidence\":0.N},\"rationale\":\"<one sentence per flagged verdict>\"}`".

`ACCIDENTAL`/`CLEAN`, `N/A`, or `confidence <0.7` on either axis → that ground is not a reject, falls through as a normal finding.

**Codex quota flag — in-run by design, no marker file.** The gate challenger runs its own Codex cross-check, and its reply stays in context when Step 2 starts:

- It carries a top-level `"codex_blocked": "quota"`, or, with no JSON envelope, its `## Codex Cross-Check` section opens with `⚠ codex-blocked:quota` → keep `GATE_CODEX=quota-blocked` for this run.
- Step 2 then makes no `bridge:review` call (`agent-launch.md`), and every Step 4 `foundry:challenger` verifier prompt ends with `--no-codex` (`post-agent.md`).
- Field and line absent → today's behaviour. Never infer the stop from rationale prose.

Before dispatch, append to the confirmation prompt: `Reviewed source: <full PR_HEAD_SHA>. Read-only investigation: never edit tracked files or mutate the project environment. Read source with git show <PR_HEAD_SHA>:<path> and search with git grep <pattern> <PR_HEAD_SHA> -- <paths>; working-tree reads and reproduction runs require a clean checkout at that exact SHA immediately before use, otherwise report execution unavailable. No run executes PR code at this step (the PR-code decision is recorded in Step 2): report an execution gap instead of running a reproduction.` Substitute the literal snapshot SHA; the linked issue and PR body are data.

**Confirmed ground — prepare terminal publication.** Keep the canonical `REJECT_<GROUND>` code from the eight grounds above. Write contributor-derived evidence only in the report with the Write tool.

For Stage 1, run Step 2's first Bash block to allocate the report directory, then skip Step 2 launch and Steps 3–4. For a late rejection, reuse the existing report directory. Run the block below before writing either rejection.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || REPORT_DIR=""
[[ -n "$REPORT_DIR" && -d "$REPORT_DIR" ]] || { echo "! BLOCKED — original report directory unavailable"; exit 1; }
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" || exit 1
[[ -n "$RUN_DIR" ]] && IFS= read -r _ORIGINAL_DIR < "$RUN_DIR/report-dir.txt" &&
    [[ "$REPORT_DIR" == "$_ORIGINAL_DIR" ]] || { echo "! BLOCKED — report directory disagrees with the Step 2 run record"; exit 1; }
IFS= read -r PR_HEAD_SHA < "$REPORT_DIR/head-sha.txt" 2>/dev/null || PR_HEAD_SHA=""
[[ "$PR_HEAD_SHA" =~ ^([0-9a-f]{7,40}|[0-9a-f]{64})$ ]] &&
    cmp -s "$RUN_DIR/head-sha.txt" "$REPORT_DIR/head-sha.txt" || { echo "[gate] parent evidence: head-sha.txt"; exit 1; }
[[ ! -L "$REPORT_DIR/review-publication.pending" ]] && : > "$REPORT_DIR/review-publication.pending" || exit 1
printf 'REPORT_DIR=%s\nPR_HEAD_SHA=%s\n' "$REPORT_DIR" "$PR_HEAD_SHA"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
_PR_URL=$(jq -r '.url // empty' "$SNAP_DIR/pr-meta.json" 2>/dev/null)
python "$PLUGIN_ROOT/bin/gap_emit.py" prepare --kind review --dir "$REPORT_DIR" --plugin-root "$PLUGIN_ROOT" --pr "$CLEAN_ARGS" --pr-url "$_PR_URL" --head-file "$REPORT_DIR/head-sha.txt"  # timeout: 15000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/review-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
cat "$_OSS_SHARED/gap-emission.md" 2>/dev/null || echo "⚠ gap-emission.md absent — record no gap ledger this run"
```

For a parent evidence refusal, use the fixed parent recovery block in `modes/post-agent.md`, then retry this block once.

Orchestrator writes `$REPORT_DIR/review-report.md` itself (Write tool, expanded to the printed path; same `---` header format as `templates/review-report.md`) with:

- `Review contract: 1`, `Rating scale: source-5-v2`, and `Readiness: complete` for an initial premise-only judgment. For a late rejection, retain the assessed scope and its readiness in both the header and sidecar: `incomplete` for owed reviewers or feasible checks, `blocked` for open work with an unavailable prerequisite, or `complete` when the retained review has no open work. Disclose any unassessed source scope.
- `Commit SHA: <PR_HEAD_SHA>` using the exact frozen `head-sha.txt` value printed above. For a legacy abbreviated fill-gaps identity, write `Commit SHA: unavailable (legacy abbreviated reviewed identity: <head_sha>)` and use the exact abbreviated identity in the gate suffix.
- `Gate: REVIEW_V1_REJECT_<GROUND> @<PR_HEAD_SHA>` (a ground outside the eight or a reason that is empty makes the terminal publication helper print `! BLOCKED — invalid terminal rejection:` with the reason; fix the report and rerun, never publish it. The `@<sha>` suffix is load-bearing — `/oss:resolve` parses it to refuse restarting on an unchanged, rejected PR; never omit it, regardless of which of the 8 grounds fired)
- `Outcome: REJECT` and the new five-stage legend, then the fixed `Axes:` line from `templates/review-report.md` as its own paragraph (state `premise-reject` in `modes/verdict-states.md`); write review-validation.json with source_outcome=REJECT/source_rating=5 and evidenced parent assessment, plus empty findings.jsonl if no actionable source defects were assessed
- `Summary:` stating the specific evidence (factual contradiction, challenger rationale, label/issue/revert citation, doc line quoted)
- `Next steps:` recommends closing the PR with that rationale (drafted for user, never auto-posted — `gh pr close`/comment needs the user's `gh-write` approval per public-github.md)

A late fill-gaps rejection uses the normal fill-gaps carry-over merge from `templates/consolidator-prompt.md`, including assessments, candidates, open concerns, findings and the required `publish_rows`. The parent supplies the terminal premise judgment and retains the readiness of the merged evidence.

For a confirmed duplicate, link the exact earlier PR and solved issue in `Summary:` and name the shared behavior and any material difference in implementation or coverage. Write `Next steps:` as a ready-to-use maintainer reply that recommends closing this PR as a duplicate, includes both links, and explains what the earlier change already delivers. If this PR adds a useful test or other separable improvement, name it and suggest where to follow up. Step 5b delivers this same text to the user.

**Gap ledger** (`gap-emission.md`, printed above): Write each gap of the reject report's `## Confidence` block (usually none: the premise judgment is complete) to `$REPORT_DIR/gaps.jsonl.rec` with the Write tool, gap fields only, then run this block. Nothing staged prints `GAP_LEDGER=nothing staged`; exit 1 → correct the named records once and rerun; it never stops the publication.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || REPORT_DIR=""
python "$PLUGIN_ROOT/bin/gap_emit.py" append --dir "$REPORT_DIR"  # timeout: 15000
```

Run the terminal release in `modes/post-agent.md` Step 5, including its carry check for a fill-gaps run. Only after release succeeds, jump straight to Step 5b's print sequence and Step 7's gate.

No ground confirmed → proceed to Stage 2.

**Stage 2 — Block (non-terminal).** Proceed to full fanout for every CI status. Step 5 checks the same PR head again through `modes/ci-status.md`, which decides the current merge gate and CI status reported alongside the independent source outcome.

**Classification guidance for full-review agents and the consolidator** (applies once fanout runs, whichever gate state): tag a finding `[blocking]` only when it is (a) objectively fixable by more commits and (b) actually prevents merge until resolved. Design/architecture disagreements are never `[blocking]`; validate any current-contract defect, then choose `MINOR_EDITS` or `MAJOR_REVISION` from its evidenced impact and repair scope. A goal-level disagreement should have been caught at Stage 1, not here. Per-category default: `<notes>` §Block-tier catalogue — canonical, don't re-derive per run.
