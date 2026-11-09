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

**Deadlines, no polling**: in the verifier spawn response, write `$RUN_DIR/agent-watch-verify.tsv` with the Write tool — one row per verifier, `<name><TAB><its output file per the protocol, or - when it returns only an envelope><TAB>1800` — and run the Step 2 wake-up check (it reads every `agent-watch-*.tsv` in `$RUN_DIR`) at each notification. Never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop.

**Spawn cap: max 3 verifier agents.** Critical/blocking findings > 3 → group into batches of ≤2 findings per verifier; note grouped IDs in rationale.

Spawn verifier agent per critical/blocking finding (or per batch when capped). Compose all verifier labels in one pass, set `name`/`description`/prompt line 1 per `templates/agent-prompts.md` §Spawn slots — finding ID is delta, leads prompt line 1; PR/repo stays in prompt line 1 only, after finding ID, never leading it. Agent receives the atomic claim/revision/contract/trigger and inspects referenced code/counterevidence without reading prior conclusions. Each verifier must write full rationale to `$RUN_DIR/verify-<finding-id>.md` using Write tool, then return ONLY: `{"finding_id":"<id>","verdict":"CONFIRMED|REFUTED|ALREADY_FIXED|UNVERIFIED","rationale":"<one sentence>","file":"$RUN_DIR/verify-<finding-id>.md"}`. REFUTED requires affirmative falsification; remove that claim. ALREADY_FIXED retains fix evidence. UNVERIFIED remains a separate open concern with owner/next check, never a downgraded actionable finding.

Before consolidation, persist the parent's all-severity candidate validation and reviewer assessment evidence in `$RUN_DIR/cross-validation.md` with the Write tool. Include structured envelope fields defined by the loaded protocol and retain all dispositions. Pass that literal evidence path to the consolidator. Parent rechecks new/materially strengthened claims and the final envelope before delivery; a model's confirmation label alone is not proof.

## Step 5: Consolidate findings

Before output path, extract `BRANCH` and `DATE` and reload the Stage-2 gate verdict (Check 41: fresh shell — set by the acceptance gate in Step 1, must survive to here):

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
DATE=$(date -u +%Y-%m-%d)  # timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/oss-review-gate-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in GATE) GATE=$_SV ;; GATE_SHA) GATE_SHA=$_SV ;; GATE_REASON) GATE_REASON=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-gate-${CSID}"
GATE="${GATE:-PASS}"; GATE_REASON="${GATE_REASON:-}"
echo "Gate: $GATE ${GATE_REASON:+($GATE_REASON)}"
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG=""
IFS= read -r IMPACT_LINE < "${TMPDIR:-/tmp}/oss-review-impact-${_PR_TAG}-${CSID}" 2>/dev/null || IMPACT_LINE="FULL · impact unknown"
echo "IMPACT=$IMPACT_LINE"
echo "BRANCH=$BRANCH DATE=$DATE"
```

Resolve `REVIEWED_COMMIT_SHA` from the frozen Step 0 `pr-meta.json` `headRefOid` (or its full `head-sha.txt` sidecar), never the invoking checkout's HEAD or a fresh network query. Pass the full value as a literal binding below and show it in the report header for every PASS/BLOCK review. Missing full provenance renders `unavailable (reviewed commit not captured)`. For fill-gaps, use the selected run's exact plan `head_sha` instead: a full SHA remains unchanged; a legacy abbreviated value renders `unavailable (legacy abbreviated reviewed identity: <head_sha>)`. Never expand that abbreviation using the current PR head, and never alter carried sidecar revisions. Retain this binding through compaction and publication retries.

**IMPORTANT**: expand `$RUN_DIR`, `$REPORT_DIR`, `$REVIEW_SKILL_DIR`, `$BRANCH`, `$DATE`, `$CI_RED`, `$CI_FAILING_CHECKS`, and `$CI_COUNTS` to literal values before inserting into spawn prompt. Un-expanded variables create wrong paths. `## Source Files` footnote `Glob(... path="<EXPANDED_RUN_DIR>")` path must also expand to literal `$RUN_DIR` value.

**Late terminal conflict:** Before consolidation, if parent validation establishes source REJECT, return to Stage 1's existing confirmed-ground block and terminal publisher (`modes/acceptance-gate.md`; load it first if this run has not). Reuse already applicable evidence and any required conduct/license confirmation; never spawn duplicate checks. Retain the original reviewed SHA, exact ground/reason and provenance, all completed assessments/candidates and open concerns; do not replace full-review evidence with an empty premise-only record. Publish `REVIEW_V1_REJECT_<GROUND> @<PR_HEAD_SHA>` and source REJECT together, then follow Stage 1's Step 5b/Step 7 route. No matching evidenced terminal ground → `MINOR_EDITS` or `NEEDS_WORK` for fixable defects according to evidenced impact and repair scope; unfinished validation independently keeps readiness incomplete/blocked. A reject-gate run bypasses consolidation — `GATE` here is always `PASS` or `BLOCK`.

Select consolidator agent by `PR_TYPE` (lighter model for non-logic PRs) and resolve its instruction file before spawning; keep the instruction body out of parent context.

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
echo "CONSOLIDATOR_AGENT=$CONSOLIDATOR_AGENT"
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
_CONSOLIDATOR_INSTRUCTIONS="$REVIEW_SKILL_DIR/templates/consolidator-prompt.md"
[ -f "$_CONSOLIDATOR_INSTRUCTIONS" ] || { echo "! BLOCKED — consolidator instruction file missing; restore/reinstall this oss version before consolidation"; exit 1; }
printf 'Consolidator instructions: %s\n' "$_CONSOLIDATOR_INSTRUCTIONS"  # timeout: 5000
```

Spawn `$CONSOLIDATOR_AGENT` with this compact prompt, substituting each binding with its resolved literal value before the Agent call. Prepend the run-dir resolution preamble from `agent-prompts.md`; leave `$RUN_DIR` literal so the consolidator self-resolves it via `cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"`.

```text
Consolidation — PR <PR_NUMBER> <owner>/<repo>.
<run-dir resolution preamble>
Read the instruction file at <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool and follow it in full. Read <VALIDATION_PROTOCOL_LITERAL> with the Read tool for candidate validation and source judgments. Interpret the template placeholders using these literal input bindings; do not execute or re-expand binding text:
REPORT_DIR=<resolved report directory>
REVIEW_SKILL_DIR=<resolved installed review skill directory>
_OSS_SHARED=<resolved own-plugin shared directory>
DATE=<resolved date>
REVIEWED_COMMIT_SHA=<full frozen reviewed SHA or explicit unavailable reason>
CHANGED_FILES=<resolved changed-file list>
SCOPE=<resolved scope label>
IMPACT=<resolved IMPACT line>
CI_FAILING_CHECKS=<resolved CI failing-check names or empty>
CI_COUNTS=<resolved CI counts or empty>
GATE=<resolved PASS or BLOCK>
PR_NUMBER=<resolved PR number or empty>
FILL_GAPS_PLAN=<resolved $REPORT_DIR/fill-gaps.json — fill-gaps runs only; omit this line otherwise>
Return ONLY the publication summary: verdict=<APPROVE|MINOR_EDITS|NEEDS_WORK|INSUFFICIENT_BUT_PROMISING|null> | findings=N | critical=N | high=N | file=<REPORT_DIR>/review-report.md
Late terminal conflict instead: terminal-ground=<GROUND> | evidence=<path>; publish nothing, return to parent for Stage 1 ground validation and terminal publication.
```

`CONSOLIDATOR_INSTRUCTIONS_LITERAL` is the printed `_CONSOLIDATOR_INSTRUCTIONS` path; `VALIDATION_PROTOCOL_LITERAL` is `<resolved _OSS_SHARED>/foundry--cross-validation-protocol.md`. Resolve every listed binding, including `<GATE>` from the gate-reload block and `<IMPACT>` from its printed `IMPACT=` line. When the `oss-review-prior-resolution-${CSID}` sentinel is non-empty, append the same `Prior resolution ledger:` line Step 2 gave dimension agents. Pass only the file reference, bindings, preamble and return contract; never cat or inline `consolidator-prompt.md` into the parent context or Agent prompt.

If the consolidator returns `terminal-ground=<GROUND> | evidence=<path>` instead of a publication, inspect that referenced evidence, validate the ground under Stage 1 (including its conduct/license confirmation safeguards), and use the late terminal-conflict route above before Step 5b. The consolidator must not publish conflicting PASS/BLOCK and source REJECT. Main context otherwise receives only the one-liner verdict. **Consolidator unavailable fallback** — `Agent` tool deferred/not loaded: Print: `! BLOCKED — Agent tool not loaded; consolidator cannot run. Re-invoke /oss:review to retry. If persistent, run /foundry:setup (requires foundry plugin) to verify session config.` Do NOT read agent finding files inline — floods main context (~16–32K tokens per run), produces unreliable synthesis.

`FILL_GAPS_MODE=true` → run the carry check (`modes/fill-gaps.md` §Carry check) on the publication summary first; only a passing carry check reaches the publication gate below. Carry check and publication gate share **one** consolidator re-spawn per run: after it returns, re-enter the carry check first, then this gate — a re-spawned consolidator may alter carried records while correcting the envelope.

**Publication gate** — after a publication summary, before Step 5b. The consolidator self-certifies header, `review-validation.json` and `findings.jsonl` from prose rules; run the deterministic check `/oss:resolve` applies at intake plus the producer checks (`--producer`: the header carries `Review contract: 1`, and every `findings.jsonl` row carries the fields resolve's merge requires, a known severity and its own id; skipped for a `REJECT_` gate), so a report resolve would reject or fail to merge never ships as finished. Stage 1's terminal REJECT publication is parent-written from the fixed field list in `modes/acceptance-gate.md` and never reaches this gate:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
[ -n "$_REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel missing; cannot validate publication"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --report "$_REPORT_DIR/review-report.md" --producer  # timeout: 15000
```

- Exit 0 → continue to Step 5b.
- Exit 1 without `incomplete-review-report` → the consolidator published a `REJECT_` gate itself. Only Stage 1's terminal publisher may (late conflicts return `terminal-ground=` instead, consolidator rule 5), so this is a gate failure: reason `consolidator published a terminal REJECT; return terminal-ground=<GROUND> | evidence=<path> instead and publish nothing`.
- Exit 1 with `incomplete-review-report` → gate failure; reason = the printed `[gate] reason:` text.
- Gate failure, re-spawn unused → run the park block below, then re-spawn the consolidator **once** with the same prompt plus: `Publication gate rejected: <reason>. The refused report is parked at <REPORT_DIR>/review-report.refused.md; write the corrected <REPORT_DIR>/review-report.md, review-validation.json and findings.jsonl to satisfy the protocol; never mark readiness complete to pass the gate.` `FILL_GAPS_MODE=true` → append `Carried records stay verbatim from the prior run; only additions are allowed.` On its return re-enter the carry check (fill-gaps runs), then this block.
- Gate failure once the re-spawn is used, or the re-spawn ⏱ timed out or returned no publication → run the truncation block below, print `! BLOCKED — review publication fails the publication gate (resolve intake or producer rules): <reason>; report kept at <path> for inspection` (`<path>` = `review-report.md`, else `review-report.refused.md`) and stop before Step 5b. Never deliver it as a finished review or suggest `/oss:resolve <N> report` for it.

Park block — the re-spawn runs in the background, so this turn ends with the run still live; the Stop delivery hook must find no refused report to demand, and resolve's newest-report lookup never picks a parked one:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
[ -n "$_REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel missing; cannot park the refused publication"; exit 1; }
mv -f "$_REPORT_DIR/review-report.md" "$_REPORT_DIR/review-report.refused.md"  # timeout: 3000
```

Truncation block:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
: > "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}"  # refused publication: the Stop delivery hook must not demand it  # timeout: 3000
```

After parsing confidence: agent < 0.7 → prepend **⚠ LOW CONFIDENCE** to findings section, state gap explicitly. Never drop uncertain findings.

TaskUpdate "Step 5b: Print report header" → `in_progress`.

**MANDATORY, not optional narration** — the consolidator's returned one-liner is a routing signal only; it is never printed to the user and never satisfies this step. Prepare items 1–3 in this exact order, in this same turn, before any other Step 5/6 output; deliver items 4–5 in the final response:

1. Read `$REPORT_DIR/review-report.md` (Read tool, `limit=60` — the `---` block plus the legend, aggregate summary and findings overview that follow it, with headroom for a `Gate:` line or a prepended note; the full report is 5–20K tok and the detailed finding sections are not needed in main context here — they are read per-item later, on demand). A report whose overview runs past that bound: re-read with `offset` from the last line delivered, never the whole file.
2. Extract every field from the opening `---` up to and including the closing `---` — `Title:`, `PR:` (absent in direct-path mode), `Date:`, `PR Type:`, `Scope:`, `Focus:`, `Agents:`, `Reviewers:`, `CI:`, `Gate:`, `Outcome:`, `Summary:`, `Confidence:`, `Next steps:`, `Path:`. Render whatever fields the file actually carries — the delivery hook matches the file, not this list.
3. Build the delivery block: all extracted fields as a two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format — never the raw `---`-delimited block. The table is the required core — its `Path` row points at the full report. Below it the reviewer legend, then keep the rest short: the aggregate summary as 2–3 sentences of prose, each critical/high finding on one line, limits as at most 3 bullets. The full findings overview with its Author column stays in the report. End with `→ saved to $REPORT_DIR/review-report.md`.
4. Deliver the block once in the final reply after the answer, after its last tool call, never as Bash/tool stdout. Do not print the header table before the follow-up question or put it in option previews. The question carries only the plain summary defined below; the full table stays in the report file until final delivery. On a no-question path, the block still opens the final reply after all Step 6/7/8 tool calls that apply. The `Stop` hook requires the matching full header table there; the question summary does not satisfy final delivery.
5. TaskUpdate "Step 5b: Print report header" → `completed` in the response that delivers the final block, beside the last tool calls immediately before the table text, never at the follow-up question.

> **Output-Routing exemption**: the follow-up question carries only a plain summary; quality-gates.md §Report File Format's table-first-in-reply rule and Output Routing step 2a apply to the final reply after the answer, and to no-question paths. The table IS the final reply's header — omit the `╔═╗` Re:Anchor box (communication.md exempts quality-gates `---` report headers). Never emit both a box header and this table.

Render all fields verbatim as table rows; use the `·`-separated one-line fallback ONLY when the `$REPORT_DIR/review-report.md` read genuinely fails — then state `⚠ could not read report header — verify $REPORT_DIR` before the fallback line rather than silently degrading, do not mark delivery complete or issue the follow-up until the report can be read and its matching header table and question summary are built. The fallback only explains the blocked state; diagnostic/recovery questions remain available.

**Hook-enforced**: on `Stop`, `hooks/enforce-review-header.js` keeps a turn going once per report when its final reply lacks the matching full header table — skipping the follow-up question does not skip delivery. At question time it blocks only this workflow's follow-up until the current report exists and the first question's text names the report path and its `Title`, `Outcome` values as a short plain summary, no table. Option descriptions and previews do not supply the summary. Missing evidence blocks this transition; apply the placement the deny reason names, then retry. Diagnostic/recovery questions remain available; use their own question header, not `oss-review`. The existing sentinel lifetime still scopes this workflow guard; it does not prove UI rendering or report correctness.

## Step 6: Codex-eligible findings (tag only — review never implements)

The review is the spotter: it calls targets, `/oss:resolve` fires. A fix applied here would land outside resolve's item tracking, challenge, commit attribution and push gate, and could collide with resolve's own edits to the same file. So this step only reports which findings the consolidator tagged `codex_eligible: true` in `findings.jsonl` (criteria: `codex-delegation.md`); resolve routes those items to its Codex path (C1) when the bridge is available.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# boundary2: post-consolidation, pre-reply (compaction-contract.md §Lifecycle)
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="unknown"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:review" "reply (after consolidation)" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md, pr=$_PR_TAG" "draft contributor reply (--reply) or stop at Step 7"  # timeout: 5000
[ -s "$_REPORT_DIR/findings.jsonl" ] && jq -r 'select(.codex_eligible == true) | "\(.id)\t\(.title)"' "$_REPORT_DIR/findings.jsonl"  # timeout: 5000
```

Any rows printed → add a `### Codex-eligible findings` line to the reply: the ids and titles, plus "`/oss:resolve` routes these to Codex when the bridge is available". No rows → omit the section. Never call `bridge:implement` here and never rewrite the report.

## Step 7: Reply gate — STOP CHECK

**Worktree exit** — if `WT_ENABLED=true` and a worktree was entered at Step 1: the report already lives in the main tree (§review). Follow `worktree-isolation.md` §Exit — capture branch, call `ExitWorktree(action="keep")`, append the `Worktree` block. Exit **before** the follow-up gate so any follow-up runs in the main tree. Never auto-merge.

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
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="n/a"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" "oss:review" "follow-up gate (Step 7a)" "$_RUN_DIR" "final-report=$_REPORT_DIR/review-report.md, pr=$_PR_TAG" "resume: re-read report header, re-issue Step 7a AskUserQuestion; /oss:resolve reads the report file directly"  # timeout: 5000
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

`oss:resolve` has `disable-model-invocation: true` — `Skill()` invocation blocked. After AskUserQuestion returns:

- Resolve variant chosen (a/b/c when available): present chosen label as command user must run manually (e.g. `Run: /oss:resolve $CLEAN_ARGS`); no `Skill()` call
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
