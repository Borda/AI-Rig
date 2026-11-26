<!-- Consumer: plugins/cc_oss/skills/review/SKILL.md (Steps 4–7); fragment executed inline. -->

## Step 4: Validate all candidates and independently verify critical/blocking concerns

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload _OSS_SHARED (Check 41: fresh shell)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/review-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
cat "$_OSS_SHARED/foundry--cross-validation-protocol.md"  # timeout: 5000
```

Follow the protocol above: parent validates all candidates before consolidation (`FILL_GAPS_MODE=true` → only the plan's candidates plus new claims from re-run agents, `modes/fill-gaps.md` §Step 4). File absent → record incomplete review-process coverage with owner/reinstall recovery; do not publish confirmed findings or unconditional approval from an unapplied protocol.

**Independence requirement**: the additional critical/blocking verification runs as a separate spawned agent of the finding's origin type; parent validation of every candidate remains mandatory.

**Codex quota stop — challenger verifiers.** A `foundry:challenger` verifier runs its own Codex cross-check, which cannot succeed once Codex is out of credits in this run. Before composing verifier prompts, run this block once, unedited. It prints `REVIEW_CODEX=quota-blocked` when:

- the Step 2 co-review reply persisted at `$RUN_DIR/codex-review-reply.json` links a harness incident whose `fault` is `quota-exhausted`; that reply decides alone, and its verdict or blockers text never does;
- or no reply was persisted and `$RUN_DIR/foundry--codex.md` opens with the launch's own `⊘ codex-blocked:quota` line (the gate stop, kept on disk across a compaction).

`REVIEW_CODEX=quota-blocked`, or `GATE_CODEX=quota-blocked` held from the Step 1 gate → every `foundry:challenger` verifier prompt ends with the line `--no-codex`. Other verifier types, and runs printing `REVIEW_CODEX=none` without the gate flag, are unchanged.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; Step 4 cannot read the Step 2 Codex record"; exit 1; }
_FAULT=none
if [ -s "$RUN_DIR/codex-review-reply.json" ]; then
    # persisted reply decides alone: harness incident fault, never verdict text
    _FAULT=$(python "$PLUGIN_ROOT/bin/bridge_quota_check.py" "$RUN_DIR/codex-review-reply.json" 2>/dev/null) || _FAULT=none  # timeout: 15000
else
    # no reply: only the launch's one-line gate stop record counts
    _CODEX_LINE=""; [ -f "$RUN_DIR/foundry--codex.md" ] && IFS= read -r _CODEX_LINE < "$RUN_DIR/foundry--codex.md"
    case "$_CODEX_LINE" in "⊘ codex-blocked:quota"*) _FAULT=quota-exhausted ;; esac
fi
if [ "$_FAULT" = quota-exhausted ]; then echo "REVIEW_CODEX=quota-blocked — Codex out of credits this run; every foundry:challenger verifier prompt ends with --no-codex"; else echo "REVIEW_CODEX=none"; fi
```

**Deadlines, no polling**: in the verifier spawn response, write `$RUN_DIR/agent-watch-verify.tsv` with the Write tool — one row per expected candidate rationale file, `<verifier name><TAB><absolute rationale path><TAB>1800` — and run the Step 2 wake-up check (it reads every `agent-watch-*.tsv` in `$RUN_DIR`) at each notification. Never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop.

**Spawn cap: max 3 verifier agents.** Critical/blocking findings > 3 → group into batches of ≤2 findings per verifier; note grouped IDs in rationale.

Spawn verifier agent per critical/blocking finding (or per batch when capped). Compose all verifier labels in one pass, set `name`/`description`/prompt line 1 per `templates/agent-prompts.md` §Spawn slots — the assigned finding IDs lead prompt line 1; PR/repo stays in prompt line 1 only, after the IDs, never leading it. Agent receives the atomic claim/revision/contract/trigger and inspects referenced code/counterevidence without reading prior conclusions. Its prompt also says that any command that loads or executes PR code (a test, a reproduction probe, an overlay run, a type checker or linter that loads project plugins, a build or a dependency export) follows `<RUN_DIR>/code-clause.txt` verbatim, the parent substituting the absolute run-directory path, and that a missing file, a refusal or a forbidding clause means no run: record an execution gap and verify by reading only. For each assigned candidate, the verifier must write its full rationale to `$RUN_DIR/verify-<finding-id>.md` using Write tool, then return ONLY a JSON array with one object per assigned candidate, including singleton batches: `[{"finding_id":"<id>","verdict":"CONFIRMED|REFUTED|ALREADY_FIXED|UNVERIFIED","rationale":"<one sentence>","file":"$RUN_DIR/verify-<finding-id>.md"}]`. Match entries against the assigned IDs and rationale files. A missing, duplicated or unmatched entry cannot verify a candidate. Retain any independently verified siblings and record the missing verification as open. REFUTED requires affirmative falsification; remove that claim. ALREADY_FIXED retains fix evidence. UNVERIFIED remains a separate open concern with owner/next check, never a downgraded actionable finding.

Before consolidation, persist the parent's all-severity candidate validation and reviewer assessment evidence in `$RUN_DIR/cross-validation.md` with the Write tool. Include structured envelope fields defined by the loaded protocol and retain all dispositions. Pass that literal evidence path to the consolidator. Parent rechecks new/materially strengthened claims and the final envelope before delivery; a model's confirmation label alone is not proof.

**Author claims.** Before Step 5, when `$RUN_DIR/claims.json` lists claims: run the plan, authorize and run blocks of `modes/claim-repro.md` (§2 to §4) in order, re-rendered below because a compaction since Step 2 may have dropped them. Pass `$RUN_DIR/claims-result.json` to the consolidator. Every claim reaches the report with its verdict, measured relative delta, spread, comparable-hardware decision and method. A `not-run` claim is an execution gap with its reason, never `not-reproduced`. A `hardware-unavailable` claim states the missing class and its probe evidence, nothing broader.

<!-- loads: claim-repro.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
jq -e '.claims | length > 0' "$RUN_DIR/claims.json" >/dev/null 2>&1 || { echo "claims: none to reproduce"; exit 0; }
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$REVIEW_SKILL_DIR/modes/claim-repro.md"  # timeout: 10000
```

## Step 5: Consolidate findings

In PR mode, select a separate destination for the final CI observation. Run the loaded `modes/ci-status.md` block once, even when the initial observation was green. Bind its printed `CI_STATUS` and `CI_GATE` to the report. A user-triggered rerun on the reviewed head supersedes the previous observation. An unavailable refresh is reported as unavailable and keeps a failure the initial observation established (`CI_GATE=BLOCK`). It also blocks when the initial observation left no complete gate record, including when the destination switch below fails: the block first withdraws the initial-observation grant. With a recorded pass it prints `PASS`. Continue with validated source findings in every case. A direct report path skips the CI block and uses `CI_STATUS=not applicable` and `GATE=PASS` if consolidation is needed.

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
DATE=$(date -u +%Y-%m-%d)  # timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG=""
# final observation never starts without a record: withdraw any initial-observation grant Step 0 left unconsumed
printf 'consumed\n' > "${TMPDIR:-/tmp}/oss-review-ci-initial-${CSID}"
if [[ "$_PR_TAG" =~ ^[0-9]+$ ]] && IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}"; then
    echo "$SNAP_DIR/ci-final" > "${TMPDIR:-/tmp}/oss-review-ci-dir-${CSID}"
fi
IFS= read -r IMPACT_LINE < "${TMPDIR:-/tmp}/oss-review-impact-${_PR_TAG}-${CSID}" 2>/dev/null || IMPACT_LINE="FULL · impact unknown: codemap unavailable"
echo "IMPACT=$IMPACT_LINE"
echo "BRANCH=$BRANCH DATE=$DATE"
```

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$PLUGIN_ROOT/skills/review/modes/ci-status.md"  # timeout: 5000
```

Resolve `REVIEWED_COMMIT_SHA` from the allocated `$REPORT_DIR/head-sha.txt`, which must match the frozen `$RUN_DIR/head-sha.txt`. Use the fixed parent recovery block below if the report copy is missing or mismatched. Bind candidates and their checks to that exact revision. Pass a full SHA unchanged as a literal binding below and show it in the report header. For a legacy abbreviated fill-gaps identity, keep the exact plan `head_sha` in the sidecars and render `unavailable (legacy abbreviated reviewed identity: <head_sha>)` in the header. Retain the frozen binding through compaction and publication retries.

**IMPORTANT**: expand `$RUN_DIR`, `$REPORT_DIR`, `$REVIEW_SKILL_DIR`, `$BRANCH`, `$DATE`, `$CI_STATUS`, and `$CI_GATE` to literal values before inserting into spawn prompt. Un-expanded variables create wrong paths. `## Source Files` footnote `Glob(... path="<EXPANDED_RUN_DIR>")` path must also expand to literal `$RUN_DIR` value.

**Late terminal conflict:** Before consolidation, if parent validation establishes source REJECT, use Stage 1's confirmed-ground preparation and terminal publisher (`modes/acceptance-gate.md`; load it first if this run has not). Reuse already applicable evidence and any required conduct/license confirmation; never spawn duplicate checks. Retain the original reviewed SHA, exact ground/reason and provenance, all completed assessments/candidates and open concerns; do not replace full-review evidence with an empty premise-only record. Publish `REVIEW_V1_REJECT_<GROUND> @<PR_HEAD_SHA>` and source REJECT together, release the rejection with the terminal release block below, then follow Stage 1's Step 5b/Step 7 route. No matching evidenced terminal ground → `MINOR_EDITS` or `MAJOR_REVISION` for fixable defects according to evidenced impact and repair scope; unfinished validation independently keeps readiness incomplete/blocked. A reject-gate run bypasses consolidation — `GATE` here is always `PASS` or `BLOCK`.

On either terminal path, bind `PR_HEAD_SHA` from this run's `$REPORT_DIR/head-sha.txt` using Stage 1's confirmed-ground preparation before writing the rejection.

Select consolidator agent by `PR_TYPE` (domain-matched agent; code PRs run `foundry:sw-engineer` on sonnet, see below) and resolve its instruction file before spawning; keep the instruction body out of parent context.

<!-- loads: consolidator-prompt.md — consolidator reads the literal installed path, parent passes bindings only -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
_REVIEW_MODE_FILE="${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}"
[ -f "$_REVIEW_MODE_FILE" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in PR_TYPE) PR_TYPE=$_SV ;; DOCS_TYPING_MODE) DOCS_TYPING_MODE=$_SV ;; TESTS_CI_MODE) TESTS_CI_MODE=$_SV ;; CICD_ONLY_MODE) CICD_ONLY_MODE=$_SV ;; DOCS_ONLY_MODE) DOCS_ONLY_MODE=$_SV ;; DOCS_CICD_MODE) DOCS_CICD_MODE=$_SV ;; esac; done < "$_REVIEW_MODE_FILE"
case "${PR_TYPE:-CODE}" in
    DOCS_TYPING) CONSOLIDATOR_AGENT="foundry:doc-scribe" ;;
    TESTS_CI)    CONSOLIDATOR_AGENT="foundry:qa-specialist" ;;
    *)           CONSOLIDATOR_AGENT="foundry:sw-engineer" ;;  # domain-match rule: file-handoff-protocol.md §Consolidator
esac
echo "CONSOLIDATOR_AGENT=$CONSOLIDATOR_AGENT DOCS_ONLY_MODE=${DOCS_ONLY_MODE:-false}"
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
_CONSOLIDATOR_INSTRUCTIONS="$REVIEW_SKILL_DIR/templates/consolidator-prompt.md"
[ -f "$_CONSOLIDATOR_INSTRUCTIONS" ] || { echo "! BLOCKED — consolidator instruction file missing; restore/reinstall this oss version before consolidation"; exit 1; }
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
if [[ -n "$SNAP_DIR" && -f "$SNAP_DIR/conversation-coverage.txt" ]]; then
    printf 'CONVERSATION_SNAPSHOT=%s\n' "$SNAP_DIR"
else
    printf '%s\n' 'CONVERSATION_SNAPSHOT=unavailable'
fi
printf 'Consolidator instructions: %s\n' "$_CONSOLIDATOR_INSTRUCTIONS"  # timeout: 5000
```

Before the first dispatch, check the Step 2 report directory against its run record and mark the publication pending. Run this for every PR consolidation, including fill-gaps runs that skip fan-out. Keep the original directory record for retries. Resolve intake and review reuse refuse the run until the parent gates release it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
if [[ ! -d "$_RUN_DIR" || ! -d "$_REPORT_DIR" ]]; then
    echo "! BLOCKED — Step 2 run or report directory missing; cannot record report-dir for recovery"
    exit 1
fi
_REPORT_RECORD="$_RUN_DIR/report-dir.txt"
if ! IFS= read -r _ORIGINAL_DIR < "$_REPORT_RECORD" || [[ "$_REPORT_DIR" != "$_ORIGINAL_DIR" ]]; then
    echo "! BLOCKED — report directory disagrees with the Step 2 run record"
    exit 1
fi
if [[ -L "$_REPORT_DIR/review-publication.pending" ]] || ! : > "$_REPORT_DIR/review-publication.pending"; then
    echo "! BLOCKED — cannot mark the publication pending"
    exit 1
fi
```

Before dispatch in a ranked run, render `RERUN_HINTS` from the Step 2 launch record. In a fill-gaps run set `RERUN_HINTS` to empty. A `[gate] parent evidence:` diagnostic follows the parent recovery rule below, then re-enters this block. Any other nonzero exit blocks dispatch with the tool output.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
[ -n "$_REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel missing; cannot render rerun hints"; exit 1; }
python "$PLUGIN_ROOT/bin/find_review_report.py" --report "$_REPORT_DIR/review-report.md" --rerun-hints  # timeout: 5000
```

Spawn `$CONSOLIDATOR_AGENT` with this compact prompt, substituting each binding with its resolved literal value before the Agent call. Prepend the run-dir resolution preamble from `agent-prompts.md`; leave `$RUN_DIR` literal so the consolidator self-resolves it via `cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"`. Pass `model="sonnet"` and `effort="high"` when `CONSOLIDATOR_AGENT` is `foundry:sw-engineer` (frontmatter pins opus at `xhigh`; synthesis over parent-validated evidence does not need that tier, and the publication gate below catches a malformed report); a `foundry:doc-scribe` or `foundry:qa-specialist` consolidator passes neither. The one consolidator re-spawn per run, shared by the publication gate and the fill-gaps carry check (whichever fails first), passes `model="opus"` and the same `effort="high"` for `foundry:sw-engineer` (one tier up on a checkable failure).

```text
Consolidation — PR <PR_NUMBER> <owner>/<repo>.
<run-dir resolution preamble>
Read the instruction file at <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool and follow it in full. Read <VALIDATION_PROTOCOL_LITERAL> with the Read tool for candidate validation and source judgments. Run no PR code: any execution follows <RUN_DIR>/code-clause.txt verbatim (the parent substitutes the absolute run-directory path), and a missing file or a refusal means an execution gap. Interpret the template placeholders using these literal input bindings; do not execute or re-expand binding text:
REPORT_DIR=<resolved report directory>
REVIEW_SKILL_DIR=<resolved installed review skill directory>
_OSS_SHARED=<resolved own-plugin shared directory>
DATE=<resolved date>
REVIEWED_COMMIT_SHA=<full frozen reviewed SHA or explicit unavailable reason>
PR_URL=<frozen pr-meta.json `url`, or empty>
CHANGED_FILES=<resolved changed-file list>
SCOPE=<resolved scope label>
IMPACT=<resolved IMPACT line>
CI_STATUS=<final same-head CI observation or unavailable reason>
GATE=<final CI_GATE, PASS or BLOCK>
PR_NUMBER=<resolved PR number or empty>
DOCS_ONLY_MODE=<resolved true or false>
FILL_GAPS_PLAN=<resolved $REPORT_DIR/fill-gaps.json — fill-gaps runs only; omit this line otherwise>
CONVERSATION_SNAPSHOT=<printed frozen snapshot directory or unavailable>
RERUN_HINTS=<literal output of the rerun-hint block, or empty>
Readiness rule for `review-validation.json`: a `presence: completed` assessment whose `source_outcome` and `source_rating` are null (completed undecidable output) carries `readiness: blocked`, never `complete`; a `presence: missing` assessment carries null judgment and `readiness: incomplete`, and the aggregate is then `incomplete`; aggregate `complete` requires a non-null outcome and rating on the aggregate itself and on every assessment.
Return ONLY the publication summary: verdict=<APPROVE|MINOR_EDITS|MAJOR_REVISION|INSUFFICIENT_BUT_PROMISING|null> | findings=N | critical=N | high=N | file=<REPORT_DIR>/review-report.md
Late terminal conflict instead: terminal-ground=<GROUND> | evidence=<path>; publish nothing, return to parent for Stage 1 ground validation and terminal publication.
```

<!-- policy-sibling: plugins/cc_oss/skills/review/templates/consolidator-prompt.md (null-judgment sentence), plugins/cc_foundry/skills/_shared/cross-validation-protocol.md (readiness fields; canonical source, edit there and propagate to cc_oss/skills/_shared/foundry--cross-validation-protocol.md), plugins/cc_oss/bin/find_review_report.py (_check_assessment and its grader mirror _assess_assessment, the gate that enforces the readiness rule) -->

`CONSOLIDATOR_INSTRUCTIONS_LITERAL` is the printed `_CONSOLIDATOR_INSTRUCTIONS` path; `VALIDATION_PROTOCOL_LITERAL` is `<resolved _OSS_SHARED>/foundry--cross-validation-protocol.md`. Resolve every listed binding, including `<GATE>` from the final CI observation and `<IMPACT>` from its printed `IMPACT=` line. When the `oss-review-prior-resolution-${CSID}` sentinel is non-empty, append the same `Prior resolution ledger:` line Step 2 gave dimension agents. Pass only the file reference, bindings, readiness rule, preamble and return contract; never cat or inline `consolidator-prompt.md` into the parent context or Agent prompt.

If the consolidator returns `terminal-ground=<GROUND> | evidence=<path>` instead of a publication, inspect the referenced evidence, validate the ground under Stage 1 (including its conduct/license confirmation safeguards), and use the late terminal-conflict route above. After the parent writes the terminal rejection and before Step 5b, run the block below to release it. A refusal stops delivery. The consolidator must not publish conflicting PASS/BLOCK and source REJECT. Main context otherwise receives only the one-liner verdict. **Consolidator unavailable fallback** — `Agent` tool deferred/not loaded: Print: `! BLOCKED — Agent tool not loaded; consolidator cannot run. Re-invoke /oss:review to retry. If persistent, run /foundry:setup (requires foundry plugin) to verify session config.` Do NOT read agent finding files inline — floods main context (~16–32K tokens per run), produces unreliable synthesis.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" || exit 1
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" || exit 1
[[ -n "$_REPORT_DIR" && -n "$_RUN_DIR" ]] || exit 1
IFS= read -r _ORIGINAL_DIR < "$_RUN_DIR/report-dir.txt" && [[ "$_REPORT_DIR" == "$_ORIGINAL_DIR" ]] ||
    { echo "! BLOCKED — report directory disagrees with the Step 2 run record"; exit 1; }
IFS= read -r _HEAD < "$_REPORT_DIR/head-sha.txt" 2>/dev/null || _HEAD=""
[[ "$_HEAD" =~ ^([0-9a-f]{7,40}|[0-9a-f]{64})$ ]] &&
    cmp -s "$_RUN_DIR/head-sha.txt" "$_REPORT_DIR/head-sha.txt" || { echo "[gate] parent evidence: head-sha.txt"; exit 1; }
_REPORT="$_REPORT_DIR/review-report.md"
sed -n '1d; /^---$/q; /^Gate:/p' "$_REPORT" |
    grep -qxE "Gate: REVIEW_V1_REJECT_[A-Z]+ @$_HEAD" || { echo "! BLOCKED — rejection gate disagrees with the frozen review head"; exit 1; }
IFS= read -r _RUN_MODE < "$_RUN_DIR/review-mode.txt" || _RUN_MODE=""
case "$_RUN_MODE" in
    fill-gaps)
        cmp -s "$_RUN_DIR/fill-gaps.json" "$_REPORT_DIR/fill-gaps.json" || { echo "[gate] parent evidence: fill-gaps.json"; exit 1; }
        python "$PLUGIN_ROOT/bin/fill_gaps_scope.py" verify --report "$_REPORT" || exit 1 ;;  # timeout: 10000
    fresh)
        [[ ! -e "$_RUN_DIR/fill-gaps.json" && ! -e "$_REPORT_DIR/fill-gaps.json" ]] || { echo "[gate] parent evidence: review-mode.txt"; exit 1; } ;;
    *) echo "[gate] parent evidence: review-mode.txt"; exit 1 ;;
esac
python "$PLUGIN_ROOT/bin/find_review_report.py" --report "$_REPORT" --publish-rejection  # timeout: 5000
```

For a terminal release refusal, apply these branches in order:

- `[gate] parent evidence: prior ` → follow `modes/fill-gaps.md` §Carry check's prior-evidence branch. The prior run stays read-only.
- Any refusal with a `[carry] ` line → the parent owns the terminal draft. Keep publication pending, restore the carried records from the prior evidence, and rerun the terminal release block. If the parent cannot restore them, stop before delivery with the carry diagnostic.
- Other `[gate] parent evidence: <file>` → restore the named current-run evidence once with the block below and retry release. Other refusals stop delivery with the diagnostic.

`FILL_GAPS_MODE=true` → run the carry check (`modes/fill-gaps.md` §Carry check) on the publication summary first; only a passing carry check reaches the publication gate below. Carry check and publication gate share **one** consolidator re-spawn per run: after it returns, re-enter the carry check first, then this gate — a re-spawned consolidator may alter carried records while correcting the envelope.

**Restore report-dir sentinel and parent evidence** — run this fixed block for an interrupted run or a parent evidence refusal. Use the current run-dir sentinel, or, if it was lost, use Write to set `${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}` to the exact run directory saved in the compaction contract. The block restores only the exact `REPORT_DIR` allocated for this run in Step 2 and its parent artifacts. If the block refuses, the check stays blocked.

When preflight supplies `PENDING_RUN`, resume only the original Step 2 run whose `report-dir.txt` resolves to that directory. Use the verified compaction contract's `RUN_DIR`, or locate the unique matching original `report-dir.txt` under `.temp/review/` in the original checkout or its review worktree. Verify the match before restoring sentinels, and run recovery in the checkout that owns `RUN_DIR` so recorded relative paths resolve there. If the original run or its checkpoint cannot be established, stop with `! BLOCKED` and name `PENDING_RUN` and the missing evidence. Restore the recorded mode and snapshot with the block below, then continue at the first unmet checkpoint without allocating a run or repeating completed spawns.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
_REPORT_RECORD="$_RUN_DIR/report-dir.txt"
if [[ -z "$_RUN_DIR" || ! -f "$_REPORT_RECORD" || -L "$_REPORT_RECORD" ]] || ! IFS= read -r _REPORT_DIR < "$_REPORT_RECORD"; then
    echo "! BLOCKED — original report-dir record unavailable"
    exit 1
fi
_PR_REPORT_DIR="${_REPORT_DIR%/*}"
_REVIEW_REPORT_ROOT="${_PR_REPORT_DIR%/*}"
_REPORT_LAYOUT='^(.*/)?[.]reports/review/pr-[0-9]+/run-[0-9]{3,}$'
if [[ ! "$_REPORT_DIR" =~ $_REPORT_LAYOUT || "/$_REPORT_DIR/" == */../* || ! -d "$_REPORT_DIR" ]]; then
    echo "! BLOCKED — original report directory is unavailable or outside the review run layout"
    exit 1
fi
if [[ -L "$_RUN_DIR" || -L "$_REPORT_DIR" || -L "$_PR_REPORT_DIR" || -L "$_REVIEW_REPORT_ROOT" || -L "${_REVIEW_REPORT_ROOT%/*}" ]]; then
    echo "! BLOCKED — a run directory uses a symlink"
    exit 1
fi
IFS= read -r _HEAD < "$_RUN_DIR/head-sha.txt" 2>/dev/null || _HEAD=""
[[ "$_HEAD" =~ ^([0-9a-f]{7,40}|[0-9a-f]{64})$ && ! -L "$_RUN_DIR/head-sha.txt" && ! -L "$_REPORT_DIR/head-sha.txt" ]] ||
    { echo "! BLOCKED — no safe original head-sha.txt evidence"; exit 1; }
IFS= read -r _RUN_MODE < "$_RUN_DIR/review-mode.txt" || _RUN_MODE=""
[[ "$_RUN_MODE" == fresh || "$_RUN_MODE" == fill-gaps ]] || { echo "! BLOCKED — original review mode unavailable"; exit 1; }
if [[ "$_RUN_MODE" == fill-gaps ]]; then
    [[ ! -L "$_RUN_DIR/fill-gaps.json" && ! -L "$_REPORT_DIR/fill-gaps.json" ]] &&
        jq -e --arg head "$_HEAD" '.head_sha == $head' "$_RUN_DIR/fill-gaps.json" >/dev/null &&
        cp "$_RUN_DIR/fill-gaps.json" "$_REPORT_DIR/fill-gaps.json" || { echo "! BLOCKED — original fill-gaps plan unavailable or invalid"; exit 1; }
fi
if [[ -e "$_RUN_DIR/lineup.launch.md" || -e "$_REPORT_DIR/lineup.launch.md" || -e "$_REPORT_DIR/lineup.md" ]]; then
    [[ -s "$_RUN_DIR/lineup.launch.md" && ! -L "$_RUN_DIR/lineup.launch.md" && ! -L "$_REPORT_DIR/lineup.launch.md" ]] &&
        cp "$_RUN_DIR/lineup.launch.md" "$_REPORT_DIR/lineup.launch.md" || { echo "! BLOCKED — original launch lineup unavailable"; exit 1; }
    python "$PLUGIN_ROOT/bin/find_review_report.py" --report "$_REPORT_DIR/review-report.md" --restore-lineup || exit 1  # timeout: 5000
fi
cp "$_RUN_DIR/head-sha.txt" "$_REPORT_DIR/head-sha.txt" &&
    printf '%s\n' "$_REPORT_DIR" > "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" || exit 1
if [[ -s "$_RUN_DIR/snapshot-dir.txt" ]]; then
    IFS= read -r _SNAP_DIR < "$_RUN_DIR/snapshot-dir.txt" || exit 1
    _SNAP_HEAD=$(jq -er --arg pr "${_PR_REPORT_DIR##*/pr-}" 'select((.number | tostring) == $pr) | .headRefOid | select(test("^([0-9a-f]{40}|[0-9a-f]{64})$"))' "$_SNAP_DIR/pr-meta.json") ||
        { echo "! BLOCKED — original PR snapshot unavailable or mismatched"; exit 1; }
    [[ "$_SNAP_HEAD" == "$_HEAD"* ]] || { echo "! BLOCKED — original snapshot disagrees with the frozen review head"; exit 1; }
    printf '%s\n' "$_SNAP_DIR" > "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" || exit 1
fi
FILL_GAPS_MODE=false
[[ "$_RUN_MODE" == fill-gaps ]] && FILL_GAPS_MODE=true
printf 'FILL_GAPS_MODE=%s\n' "$FILL_GAPS_MODE" >> "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" || exit 1
```

**Publication gate** — after a publication summary, before Step 5b. `--publish` verifies the selected allocation, frozen `head-sha.txt` and run mode before release. Run the deterministic intake check and the `--producer` rules: require `Review contract: 1`, merge-ready `findings.jsonl` with known severities and unique ids, and the allowed producer states in `modes/verdict-states.md`. For a fresh fan-out, require `lineup.md`, exactly one completed or missing assessment for every recorded spawned role, and the full generated disclosures in visible Review Confidence text. An UNVERIFIED candidate requires `missing_capability_kind`. A `FILL_GAPS_MODE=true` run adds `--fill-gaps`: this route uses the sealed plan instead of `lineup.md`, requires the plan's `head_sha` to equal the frozen head, and preserves the existing records for verbatim carried candidates. The terminal REJECT route uses the parent publisher above.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REPLY_MODE) REPLY_MODE=$_SV ;; WT_ENABLED) WT_ENABLED=$_SV ;; FILL_GAPS_MODE) FILL_GAPS_MODE=$_SV ;; DIRECT_PATH_MODE) DIRECT_PATH_MODE=$_SV ;; REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
[ -n "$_REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel missing; cannot validate publication"; exit 1; }
if [ "${FILL_GAPS_MODE:-false}" = "true" ]; then
    python "$PLUGIN_ROOT/bin/find_review_report.py" --report "$_REPORT_DIR/review-report.md" --producer --publish --fill-gaps  # timeout: 15000
else
    python "$PLUGIN_ROOT/bin/find_review_report.py" --report "$_REPORT_DIR/review-report.md" --producer --publish  # timeout: 15000
fi
```

- Exit 0 → the parent gates have cleared the pending marker. Continue to Step 5b.
- `[gate] parent evidence: prior ` or any `[carry] ` line → apply `modes/fill-gaps.md` §Carry check's matching refusal branch before generic parent recovery or gate failure. Prior-evidence refusal stops there without spending the shared re-spawn. A carry violation uses that branch's carry repair and shared re-spawn budget.
- `[gate] parent evidence: <file>` → run the fixed parent recovery block above once, without consuming the consolidator re-spawn, then retry the check. Refresh `RERUN_HINTS` before redispatch. Re-enter the carry check first for a fill-gaps publication. If the original is unavailable, recovery fails, or the refusal repeats, run the truncation block and stop with `! BLOCKED` naming the artifact and reason.
- Exit 1 with `rejected (Gate: REJECT_` → the consolidator published a `REJECT_` gate itself. Only Stage 1's terminal publisher may (late conflicts return `terminal-ground=` instead, consolidator rule 5), so this is a gate failure: reason `consolidator published a terminal REJECT; return terminal-ground=<GROUND> | evidence=<path> instead and publish nothing`.
- Exit 1 with `incomplete-review-report` → gate failure; reason = the printed `[gate] reason:` text.
- Any other nonzero result → run the truncation block and stop with `! BLOCKED — publication gate could not run` and its diagnostic.
- Gate failure, re-spawn unused → run the park block below, then re-spawn the consolidator **once** (`model="opus"`, `effort="high"` for a `foundry:sw-engineer` consolidator) with the same prompt plus: `Publication gate rejected: <reason>. The refused report is parked at <REPORT_DIR>/review-report.refused.md; write the corrected <REPORT_DIR>/review-report.md, review-validation.json and findings.jsonl to satisfy the protocol; never mark readiness complete to pass the gate.` `FILL_GAPS_MODE=true` → append `Carried records stay verbatim from the prior run; only additions are allowed.` On its return re-enter the carry check (fill-gaps runs), then this block.
- Gate failure once the re-spawn is used, or the re-spawn ⏱ timed out or returned no publication → run the truncation block below, print `! BLOCKED — review publication fails the publication gate (resolve intake or producer rules): <reason>; report kept at <path> for inspection` (`<path>` = `review-report.md`, else `review-report.refused.md`) and stop before Step 5b. Never deliver it as a finished review or suggest `/oss:resolve <N> report` for it.

Park block — the re-spawn runs in the background, so this turn ends with the run still live. Parking keeps the refused report away from the Stop delivery hook, and the pending marker keeps the next draft out of intake:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
[ -n "$_REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel missing; cannot park the refused publication"; exit 1; }
mv -f "$_REPORT_DIR/review-report.md" "$_REPORT_DIR/review-report.refused.md"  # timeout: 3000
```

Truncation block — leave the pending marker in the durable report directory so a terminal refusal stays ineligible:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
: > "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}"  # refused publication: the Stop delivery hook must not demand it  # timeout: 3000
```

After parsing confidence: agent < 0.7 → prepend **⚠ LOW CONFIDENCE** to findings section, state gap explicitly. Never drop uncertain findings.

TaskUpdate "Step 5b: Print report header" → `in_progress`.

For a completed report selected in Step 0, use its bound `REPORT_DIR` here. Deliver the same block on the no-question path with the saved confidence and the revision note from Step 0. After compaction, re-read the report to rebuild the block before final delivery.

**MANDATORY, not optional narration** — the consolidator's returned one-liner is a routing signal only; it is never printed to the user and never satisfies this step. Prepare items 1–3 in this exact order, in this same turn, before any other Step 5/6 output; deliver items 4–5 in the final response:

1. Read `$REPORT_DIR/review-report.md` (Read tool, `limit=60` — the `---` block plus the legend, aggregate summary and findings overview that follow it, with headroom for a `Gate:` line or a prepended note; the full report is 5–20K tok and the detailed finding sections are not needed in main context here — they are read per-item later, on demand). A report whose overview runs past that bound: re-read with `offset` from the last line delivered, never the whole file.
2. Extract every field from the opening `---` up to and including the closing `---` — `Title:`, `PR:` (absent in direct-path mode), `Link:` (absent in direct-path mode), `Date:`, `PR Type:`, `Scope:`, `Focus:`, `Agents:`, `Reviewers:`, `CI:`, `Gate:`, `Outcome:`, `Summary:`, `Confidence:`, `Next steps:`, `Path:`. Render whatever fields the file actually carries — the delivery hook matches the file, not this list.
3. Build the delivery block: the extracted fields as a two-column Markdown table (`Field | Value`, one row per key, file order) without the file-only rows `Link`, `Date`, `Commit SHA`, `Agents`, `Review contract`, `Rating scale`, `Readiness` and `Gate`, and with `PR` shown as `[#N](<Link value>)` per quality-gates.md §Report File Format — never the raw `---`-delimited block. The table is the required core — its `Path` row points at the full report. Below it the reviewer legend, then keep the rest short: the aggregate summary as 2–3 sentences of prose, each critical/high finding on one line, limits as at most 3 bullets. The full findings overview with its Author column stays in the report. End with `→ saved to $REPORT_DIR/review-report.md`.
4. Deliver the block once in the final reply after the answer, after its last tool call, never as Bash/tool stdout. Do not print the header table before the follow-up question or put it in option previews. The question carries only the plain summary defined below; the full table stays in the report file until final delivery. On a no-question path, the block still opens the final reply after all Step 6/7/8 tool calls that apply. The `Stop` hook requires the matching review header table there (the file-only rows may be left out); the question summary does not satisfy final delivery.
5. TaskUpdate "Step 5b: Print report header" → `completed` in the response that delivers the final block, beside the last tool calls immediately before the table text, never at the follow-up question.

> **Output-Routing exemption**: the follow-up question carries only a plain summary; quality-gates.md §Report File Format's table-first-in-reply rule and Output Routing step 2a apply to the final reply after the answer, and to no-question paths. The table IS the final reply's header — omit the `╔═╗` Re:Anchor box (communication.md exempts quality-gates `---` report headers). Never emit both a box header and this table.

Render the remaining fields verbatim as table rows; use the `·`-separated one-line fallback ONLY when the `$REPORT_DIR/review-report.md` read genuinely fails — then state `⚠ could not read report header — verify $REPORT_DIR` before the fallback line rather than silently degrading, do not mark delivery complete or issue the follow-up until the report can be read and its matching header table and question summary are built. The fallback only explains the blocked state; diagnostic/recovery questions remain available.

**Hook-enforced**: on `Stop`, `hooks/enforce-review-header.js` keeps a turn going once per report when its final reply lacks the matching review header table — skipping the follow-up question does not skip delivery. At question time it blocks only this workflow's follow-up until the current report exists and the first question's text names the report path and its `Title`, `Outcome` values as a short plain summary, no table. Option descriptions and previews do not supply the summary. Missing evidence blocks this transition; apply the placement the deny reason names, then retry. Diagnostic/recovery questions remain available; use their own question header, not `oss-review`. The existing sentinel lifetime still scopes this workflow guard; it does not prove UI rendering or report correctness.

## Step 6: Codex-eligible findings (tag only — review never implements)

The review is the spotter: it calls targets, `/oss:resolve` fires. A fix applied here would land outside resolve's item tracking, challenge, commit attribution and push gate, and could collide with resolve's own edits to the same file. So this step only reports which findings the consolidator tagged `codex_eligible: true` in `findings.jsonl` (criteria: `codex-delegation.md`); resolve routes those items to its Codex path (C1) when the bridge is available.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary2: post-consolidation, pre-reply (compaction-contract.md §Lifecycle)
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="unknown"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
python "$PLUGIN_ROOT/bin/write_skill_contract.py" "oss:review" "reply (after consolidation)" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md, pr=$_PR_TAG" "draft contributor reply (--reply) or stop at Step 7"  # timeout: 5000
[ -s "$_REPORT_DIR/findings.jsonl" ] && jq -r 'select(.codex_eligible == true) | "\(.id)\t\(.title)"' "$_REPORT_DIR/findings.jsonl"  # timeout: 5000
```

Any rows printed → add a `### Codex-eligible findings` line to the reply: the ids and titles, plus "`/oss:resolve` routes these to Codex when the bridge is available". No rows → omit the section. Never call `bridge:implement` here and never rewrite the report.

## Step 7: Reply gate — STOP CHECK

**Worktree exit** — if `WT_ENABLED=true` and a worktree was entered at Step 1: follow SKILL.md §Worktree isolation, report the final report path and detached SHA, call `ExitWorktree(action="keep")`, and append the `Worktree` block. Exit **before** the follow-up gate so any follow-up runs in the main tree. Never auto-merge.

**Hard gate**: before the follow-up question, the Step 5b header table and plain summary must already be built from the current report file. Not built → go back and do it now (see Step 5b). Delivery timing: Step 5b item 4. Keep "Step 5b: Print report header" `in_progress` until that final delivery.

**Confidence block ownership**: `REPLY_MODE=true` → block in Step 8. `REPLY_MODE=false` → block in Step 7b.

`REPLY_MODE=true`: proceed to Step 8 — no Confidence block here. `REPLY_MODE=false` — do NOT proceed to Step 8. Execute both sub-steps below.

**Already-running check** — before 7a, detect whether `/oss:resolve` for this same PR is already active in this session. Read via the liveness sentinel resolve writes at its own Step 1 and clears on completion (`resolve/SKILL.md`, `${TMPDIR:-/tmp}/oss-resolve-active-${CSID}`) — never via `.temp/state/skill-contract.md`: that file is this review's *own* compaction contract, written and cleared unconditionally at this skill's own Step 0/5b/8 regardless of what else is running, so it carries no reliable cross-skill liveness signal. `AskUserQuestion` cannot be honored when resolve is already covering this PR (`quality-gates.md` §"Don't ask what you can't honor") — a real incident idled this gate 10m30s asking a question resolve had already answered by running:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG=""
IFS= read -r _RESOLVE_PR < "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" 2>/dev/null || _RESOLVE_PR=""
_RESOLVE_ACTIVE=false
[[ "$_PR_TAG" =~ ^[0-9]+$ ]] && [ "$_RESOLVE_PR" = "$_PR_TAG" ] && _RESOLVE_ACTIVE=true
if [ "$_RESOLVE_ACTIVE" = "true" ]; then
    echo "/oss:resolve already running in this session for PR $_PR_TAG — it already covers these findings. Step 7a gate skipped; resolve will finish and report separately."
fi
```

`_RESOLVE_ACTIVE=true` → print that line as plain text in the reply (not just Bash stdout — tool output is not reliably shown to the user); do **not** call `AskUserQuestion`; skip straight to 7b. A no-PR review (`_PR_TAG` empty — local dir/path mode), or resolve active for a *different* PR, must never suppress the gate — the check only fires on an exact numeric PR match, so any unmatched or empty `_PR_TAG` leaves `_RESOLVE_ACTIVE=false`. `_RESOLVE_ACTIVE=false` → continue to 7a below as normal.

### 7a — Follow-up gate

Skip this entire sub-step when the already-running check above set `_RESOLVE_ACTIVE=true`.

This gate idles longest (measured up to 11 h on a real review). Refresh the contract right before it — boundary 2 was written before Step 6 pulled more into context — and print the hint so the user can `/compact` while deciding; a resolve follow-up then starts from the report file, not this transcript:

```bash
# compaction boundary 3 — immediately before the 7a idle gate (compaction-contract.md §Lifecycle)
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="n/a"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" "oss:review" "follow-up gate (Step 7a)" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md, pr=$_PR_TAG" "resume: re-read report header, re-issue Step 7a AskUserQuestion; /oss:resolve reads the report file directly"  # timeout: 5000
```

! IMPORTANT — invoke `AskUserQuestion` tool directly. Never write options as plain text. Not a hollow question: `walk through findings` and `skip` are honored inline, so quality-gates' "Don't ask what you can't honor" never skips this gate — only the already-running check above does. Single call — all options in one. The `/compact` hint rides in the question text, never as reply prose or Bash stdout before the call (text before a tool call can vanish on 5.5-family models):

- header: `oss-review`
- question: at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence: `Title: <report Title>`; `Outcome: <report Outcome>`; finding counts and blockers; `→ <REPORT_DIR>/review-report.md`; `What next?`; "Long wait? `/compact` now — report saved, resume lossless." Keep the `/compact` hint in the question text.
- (a) label: `/oss:resolve $CLEAN_ARGS` — description: fix this PR (implement review findings, resolve conflicts, push)
- (b) label: `/oss:resolve report` — description: newest review report on this branch, no GitHub re-fetch (any PR's — use (c) to pin this PR)
- (c) label: `/oss:resolve $CLEAN_ARGS report` — description: fix PR + resolve from full review report in one pass
- (d) label: `walk through findings` — description: go through each finding interactively
- (e) label: `skip` — description: no action
- every option: label and one-line description only, no `preview` (header table placement: Step 5b item 4).

For any `REVIEW_V1_REJECT_<GROUND>` gate, replace the three resolve choices with `draft close reply` — description: print the report's close recommendation as a draft for you to post. Resolve refuses every rejected head, whichever ground fired (its reject-gate check), so those choices cannot be honored. Keep `walk through findings` to explain the ground's evidence (for `REVIEW_V1_REJECT_DUPLICATE`, the comparison and any material difference), and `skip` to finish.

`oss:resolve` has `disable-model-invocation: true` — `Skill()` invocation blocked. After AskUserQuestion returns:

- Resolve variant chosen (a/b/c, offered only for a PASS or BLOCK gate): present chosen label as command user must run manually (e.g. `Run: /oss:resolve $CLEAN_ARGS`); no `Skill()` call
- `draft close reply`: print the report's `Next steps:` close recommendation (for a duplicate, the linked maintainer reply) as a draft for the user to post; never post a comment or close the PR — that needs the user's `gh-write` approval (public-github.md)
- `walk through findings` / `skip`: handle inline or stop

### 7b — Confidence block

End with `## Confidence` block per CLAUDE.md output standards — always, even when 7a was skipped.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG=""
IFS= read -r _RESOLVE_PR < "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" 2>/dev/null || _RESOLVE_PR=""
# re-check fresh — never trust the already-running check's pre-idle snapshot: 7a idles up to 11h on
# a real review, long enough for resolve to start or finish inside that window
if [[ "$_PR_TAG" =~ ^[0-9]+$ ]] && [ "$_RESOLVE_PR" = "$_PR_TAG" ]; then
    : # resolve is active for this PR now — never clear a contract it owns
else
    rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)
fi
```
