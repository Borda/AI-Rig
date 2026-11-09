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
echo "$PR_HEAD_SHA" > "${TMPDIR:-/tmp}/oss-review-pr-head-sha-${CSID}"

# cheap mechanical signals for grounds 3/5/6 below — reuses data already fetched, one small gh call per linked issue
SCOPE_LABEL_HIT=false
case ",${PR_LABELS}," in *,wontfix,*|*,invalid,*|*,declined,*|*,out-of-scope,*) SCOPE_LABEL_HIT=true ;; esac

DUPLICATE_HIT=false; DUPLICATE_REASON=""
# same regex as Agent 0's gather; ISSUE_NUMS is assigned here, never in prose — an unset list leaves REJECT_DUPLICATE unreachable
ISSUE_NUMS=$(printf '%s' "$PR_BODY" | grep -oiE '(close[sd]?|fix(e[sd])?|resolve[sd]?|refs?) #[0-9]+' | grep -oE '[0-9]+' | sort -u | head -3)
echo "issue_nums:" $ISSUE_NUMS
for N in $ISSUE_NUMS; do
    _ISTATE=$(gh issue view "$N" --json state --jq .state 2>/dev/null)  # timeout: 6000
    if [ "$_ISTATE" = "CLOSED" ]; then
        _CLOSER=$(gh issue view "$N" --json closedByPullRequestsReferences --jq '.closedByPullRequestsReferences[0].number // empty' 2>/dev/null)  # timeout: 6000
        [ -n "$_CLOSER" ] && [ "$_CLOSER" != "$CLEAN_ARGS" ] && { DUPLICATE_HIT=true; DUPLICATE_REASON="issue #$N already closed by #$_CLOSER"; }
    fi
done

REVERT_CANDIDATE=""; [ -z "$CHANGED_FILES" ] || REVERT_CANDIDATE=$(git log --all --grep='^Revert' --oneline -- $CHANGED_FILES 2>/dev/null | head -3)  # timeout: 10000
echo "scope_label=$SCOPE_LABEL_HIT duplicate=$DUPLICATE_HIT revert_candidate=${REVERT_CANDIDATE:+yes}"
```

**Description drift caution** — `PR_BODY` is a snapshot written at PR-open time; it drifts from what diff actually does as commits land (further changes, or fixes pushed in response to earlier review feedback) and nobody edits description to match. Judge every ground below against **current diff behavior**, not stated text alone — read `CHANGED_FILES`/diff intent (already fetched in Step 0/1) alongside `PR_BODY`. Body says one thing, diff does another → trust diff; a stale description is not itself a reject ground, note mismatch in `Summary:` if material.

**Stage 1 — Reject (terminal).** Eight grounds — aligned with close-without-merge practice in K8s/CPython/Rust/Django contributing docs. Every ground needs affirmative evidence, never suspicion alone — disagreement-with-approach is a `NEEDS_WORK`/`[blocking]` finding, stage 2 or full review territory, never a reject. Grounds 1–2 already had detail; 3–8 are the agreed expansion:

1. **REJECT_GOAL** — stated goal factually/technically wrong even if well-intentioned. Test: does the goal — read from `PR_BODY`, cross-checked per the drift caution above — contradict a known invariant, spec, or domain fact — e.g. "raise this accuracy metric above 1.0" when the metric is bounded `[0,1]` by definition, goal is unreachable no matter how the code changes. Judge from PR description + package docs/spec, not the diff's mechanics. Orchestrator judgment only — no agent spawn.
2. **REJECT_CONDUCT** — contribution by design adversarial, malicious, or a Code of Conduct violation (not an accidental bug). Never reject on suspicion alone: requires the `foundry:challenger` confirmation below.
3. **REJECT_SCOPE** — out of project scope / against roadmap, maintainers already decided against this direction. Evidence: `SCOPE_LABEL_HIT=true` (maintainer already triaged `wontfix`/`invalid`/`declined`/`out-of-scope`), or an explicit "out of scope" statement in `CONTRIBUTING.md`/an ADR that the PR's stated intent directly matches — grep for it, don't assume. No documented evidence → not a reject, at most a `NEEDS_WORK` scope concern. Orchestrator judgment only.
4. **REJECT_LICENSE** — license/provenance conflict: incompatible license copied in (e.g. GPL source pasted into a permissive-licensed project), or plagiarized/copied source the contributor has no right to submit. Not the same as a missing CLA/DCO signature — that's Stage 2 `[blocking]`, fixable by signing; this is the source itself being unlicensable. Requires the `foundry:challenger` confirmation below when suspected (explicit "ported from `<project>`" in `PR_BODY`, or a license header in the diff that conflicts with this repo's license).
5. **REJECT_DUPLICATE** — another PR already merged solving this, or the linked issue already fixed upstream. Evidence: `DUPLICATE_HIT=true` (`$DUPLICATE_REASON`). No `ISSUE_NUMS` linked or issue still open → not this ground.
6. **REJECT_REVERTED** — reintroduces a previously reverted change without addressing why it was reverted. Evidence: `REVERT_CANDIDATE` non-empty (a prior revert touched the same files) **and** `PR_BODY` doesn't reference or address that revert/its reason — a candidate alone is not enough, read the revert commit message and compare intent before rejecting. Orchestrator judgment only.
7. **REJECT_SPAM** — spam/low-effort/AI-slop: no real change, hacktoberfest-farming pattern. Evidence needs both: diff is trivially low-value (whitespace/punctuation-only across the changed lines, no logic touched) **and** `PR_BODY` is generic/templated with no specifics tying it to this repo. Either alone is not enough — a genuine one-line critical fix is low-value-looking but not spam; judge the pairing, not the diff size alone. Orchestrator judgment only.
8. **REJECT_PHILOSOPHY** — contradicts a documented design principle (not a style preference). Evidence: an explicit principle stated in `README.md`/`CONTRIBUTING.md`/an ADR that the PR's intent directly violates — e.g. adding a GUI to a project whose docs state "CLI-only by design". Cite the exact doc line in `Summary:` — no citable line, no reject. Orchestrator judgment only.

**Challenger confirmation** (grounds 2 and 4 only — the two where accusing wrongdoing carries real reputational/legal stakes, so both share one call): only spawn when the orchestrator's own read of `PR_BODY`/diff/`CHANGED_FILES` raised a concrete suspicion for either — never spawn speculatively on every PR.

Prompt: "Investigate PR #<N> (body: \<PR_BODY>, diff: changed files) for two things: (1) is its intent a by-design malicious/adversarial contribution or Code of Conduct violation, vs. an accidental mistake; (2) is any changed content plagiarized or under an incompatible license the contributor has no right to submit, vs. original/properly licensed work. Read the diff and linked issue if any. Return ONLY: `{\"conduct\":{\"verdict\":\"BY_DESIGN\"|\"ACCIDENTAL\"|\"N/A\",\"confidence\":0.N},\"license\":{\"verdict\":\"CONFLICT\"|\"CLEAN\"|\"N/A\",\"confidence\":0.N},\"rationale\":\"<one sentence per flagged verdict>\"}`".

`ACCIDENTAL`/`CLEAN`, `N/A`, or `confidence <0.7` on either axis → that ground is not a reject, falls through as a normal finding.

Any ground confirmed — one block, two substitutions (`GATE_GROUND`, `GATE_REASON`); the whitelist aborts on an unedited placeholder or an unknown ground, so a verbatim run can never record the wrong code (`/oss:resolve` keys its refusal on it). This block carries user-chosen text, so it misses the blueprint manifest and prompts once — accepted: the reject gate fires rarely. <!-- policy-sibling: plugins/CLAUDE.md §Blueprint Blocks (canonical), plugins/cc_foundry/agents/challenger.md, plugins/cc_oss/skills/resolve/modes/item-selection.md (Step 3d), plugins/cc_oss/skills/resolve/modes/push.md (Step 10), plugins/cc_oss/skills/review/modes/acceptance-gate.md (reject gate) -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_HEAD_SHA < "${TMPDIR:-/tmp}/oss-review-pr-head-sha-${CSID}" 2>/dev/null || PR_HEAD_SHA=""
GATE_GROUND="<REJECT_GROUND>"  # one of: REJECT_GOAL REJECT_CONDUCT REJECT_SCOPE REJECT_LICENSE REJECT_DUPLICATE REJECT_REVERTED REJECT_SPAM REJECT_PHILOSOPHY
GATE_REASON="<one-line evidence for the ground that fired>"
case "$GATE_GROUND" in REJECT_GOAL|REJECT_CONDUCT|REJECT_SCOPE|REJECT_LICENSE|REJECT_DUPLICATE|REJECT_REVERTED|REJECT_SPAM|REJECT_PHILOSOPHY) ;; *) echo "! BLOCKED — GATE_GROUND is '$GATE_GROUND', not one of the 8 REJECT_* codes; substitute it before running"; exit 1 ;; esac
case "$GATE_REASON" in "<one-line evidence"*|"") echo "! BLOCKED — GATE_REASON still holds the placeholder; substitute the evidence line before running"; exit 1 ;; esac  # exact-prefix match: evidence may legitimately contain <https://…> URLs
{ echo "GATE=${GATE_GROUND}"; echo "GATE_SHA=${PR_HEAD_SHA}"; echo "GATE_REASON=${GATE_REASON}"; } > "${TMPDIR:-/tmp}/oss-review-gate-${CSID}"  # timeout: 3000
echo "gate: ${GATE_GROUND} @${PR_HEAD_SHA}"
```

<!-- policy-sibling: plugins/cc_oss/skills/review/SKILL.md, plugins/cc_oss/skills/resolve/SKILL.md — `Gate: REVIEW_V1_REJECT_<GROUND> @<sha>` line format (find_review_report.py normalizes the legacy `REJECT_*` prefix), both sides must agree -->

Skip Step 2's agent launch and Steps 3–4 entirely, but allocate the report dir first: run Step 2's first bash block (run-dir and report-dir allocation) unchanged, then the read-back below. `REPORT_DIR` is allocated nowhere else, and `/oss:resolve` finds a report only under `.reports/review/pr-<N>/run-*/` — a reject report written elsewhere never blocks the rejected PR. A late terminal conflict (Step 5) skips the allocation: Step 2 already ran it and `REPORT_DIR` exists.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || REPORT_DIR=""
[ -n "$REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel empty; run Step 2's allocation block first"; exit 1; }
echo "REPORT_DIR=$REPORT_DIR"
```

Orchestrator writes `$REPORT_DIR/review-report.md` itself (Write tool, expanded to the printed path; same `---` header format as `templates/review-report.md`) with:

- `Review contract: 1`, `Rating scale: source-5-v1`, and `Readiness: complete` for the established premise judgment; disclose any unassessed source scope
- `Commit SHA: <PR_HEAD_SHA>` using the full frozen `pr-meta.json` `headRefOid` retained by Stage 1 and `head-sha.txt`; never abbreviate or use the invoking checkout's HEAD. Missing full provenance is explicitly unavailable, never guessed. This field is required on initial and late terminal publications.
- `Gate: REVIEW_V1_REJECT_<GROUND> @<PR_HEAD_SHA>` (the `@<sha>` suffix is load-bearing — `/oss:resolve` parses it to refuse restarting on an unchanged, rejected PR; never omit it, regardless of which of the 8 grounds fired)
- `Outcome: REJECT` and the new five-stage legend; write review-validation.json with source_outcome=REJECT/source_rating=5 and evidenced parent assessment, plus empty findings.jsonl if no actionable source defects were assessed
- `Summary:` stating the specific evidence (factual contradiction, challenger rationale, label/issue/revert citation, doc line quoted)
- `Next steps:` recommends closing the PR with that rationale (drafted for user, never auto-posted — `gh pr close`/comment needs the user's `gh-write` approval per public-github.md)

Then jump straight to Step 5b's print sequence and Step 7's gate — no consolidator spawn needed, nothing to consolidate.

No ground confirmed → proceed to Stage 2.

**Stage 2 — Block (non-terminal).** Reloads `CI_RED`/`CI_FAILING_CHECKS` from the CI STATUS sentinels — no new fetch. Red CI is the only mechanically-cheap block signal available pre-fanout; a typo or a flaky test can't be told apart from a real regression without actually reading the diff or a rerun, so those stay classification guidance for the full-review agents (below), not a pre-fanout check.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CI_RED < "${TMPDIR:-/tmp}/oss-review-ci-red-${CSID}" 2>/dev/null || CI_RED=false
IFS= read -r CI_FAILING_CHECKS < "${TMPDIR:-/tmp}/oss-review-ci-failing-${CSID}" 2>/dev/null || CI_FAILING_CHECKS=""
if [ "${CI_RED:-false}" = "true" ]; then
    { echo "GATE=BLOCK"; echo "GATE_REASON=ci-red: ${CI_FAILING_CHECKS}"; } > "${TMPDIR:-/tmp}/oss-review-gate-${CSID}"
else
    { echo "GATE=PASS"; echo "GATE_REASON="; } > "${TMPDIR:-/tmp}/oss-review-gate-${CSID}"
fi
```

`GATE=BLOCK` does **not** skip Step 2 — proceed to full fanout regardless, `Gate: REVIEW_V1_BLOCK` is carried into the Step 5 report header alongside the normal `Outcome:` so the fixable blocker is visible immediately, not buried after N findings.

**Classification guidance for full-review agents and the consolidator** (applies once fanout runs, whichever gate state): tag a finding `[blocking]` only when it is (a) objectively fixable by more commits and (b) actually prevents merge until resolved. Design/architecture disagreements are never `[blocking]`; validate any current-contract defect, then choose `MINOR_EDITS` or `NEEDS_WORK` from its evidenced impact and repair scope. A goal-level disagreement should have been caught at Stage 1, not here. Per-category default: `<notes>` §Block-tier catalogue — canonical, don't re-derive per run.
