<!-- oss:resolve Report source resolution + Gap triage — executed inline: cat $_OSS_RESOLVE/modes/report-intake.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 1 Report source resolution) -->

### Report source resolution (`report` and `pr + report` modes; `pr` mode only after an incomplete-report gate)

Run this block — it is the single lookup for both modes, and the **only** sanctioned way to conclude that no report exists. A prose-only lookup here was silently skipped in a real run, and the orchestrator re-ran a full `oss:review` fan-out while a matching report sat on disk and its path had already been printed by the reject gate one block earlier.

It is also the single intake triage. A report failing the publication contract (`⛔ BLOCKED — incomplete-review-report`) is graded by `find_review_report.py --assess` instead of stopping blind; a standing rejection, an invalid `--pr` or a failed path publication stays `blocked`. Resolve never edits or regenerates the review's report, sidecars or findings to clear a gap — no self-heal. Its only write into a review run stays Step 11's `resolution.jsonl` ledger.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 45000 — gate and --assess may each call gh
_SENTINEL="${TMPDIR:-/tmp}/resolve-report-file-${CSID}"
_GATE_OUT="${TMPDIR:-/tmp}/resolve-gate-out-${CSID}"
_GAPS="${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}"
_RERUN="${TMPDIR:-/tmp}/resolve-intake-rerun-${CSID}"
unsetopt nomatch 2>/dev/null  # zsh: unmatched glob stays literal as in bash, ls drops it; NOMATCH would report a present report missing
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
IFS= read -r INTAKE_ACCEPTED < "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}" 2>/dev/null || INTAKE_ACCEPTED=""
# PR# → re-run the PR-scoped lookup (idempotent): sentinel trustworthy only if the reject gate ran THIS run for THIS PR,
# and another PR's newest report would merge the wrong findings. No PR# → gate wrote the sentinel empty; glob newest-of-any
case "$PR_NUMBER" in
    ""|n/a)
        REPORT_FILE=$(ls -t .reports/review/*/review-report.md .reports/review/*/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1)
        echo "$REPORT_FILE" > "$_SENTINEL"
        set -- --report "$REPORT_FILE" ;;
    *) REPORT_FILE=""; set -- --pr "$PR_NUMBER" ;;
esac
: > "$_GATE_OUT"
GATE_RC=0
case "$1:$REPORT_FILE" in
    --report: | --report:.reports/codex/review/*) ;;  # missing or codex-lineage: nothing to gate
    *) python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" "$@" --path-out "$_SENTINEL" > "$_GATE_OUT"; GATE_RC=$?; cat "$_GATE_OUT" ;;  # timeout: 6000
esac
# exit 1 without the incomplete marker = still-rejected PR, bad --pr or lost path; never swallowed, never triaged
if [ "$GATE_RC" -ne 0 ]; then
    grep -q 'incomplete-review-report' "$_GATE_OUT" || { echo "REPORT_STATUS=blocked"; rm -f "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"; exit 1; }
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" "$@" --assess --path-out "$_SENTINEL" > "$_GAPS"; ASSESS_RC=$?; cat "$_GAPS"  # timeout: 15000 — may call gh for head_moved
    # degraded-only gaps can still carry REJECT_: premise problem, never consumable
    grep -q '"gate": "Gate: REJECT_' "$_GAPS" && { : > "$_SENTINEL"; echo "! BLOCKED — incomplete review report still records a rejection — address the ground, then rerun /oss:review"; ASSESS_RC=1; }
    [ "$ASSESS_RC" -eq 1 ] && { echo "REPORT_STATUS=blocked"; rm -f "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"; exit 1; }
    IFS= read -r REPORT_FILE < "$_SENTINEL" 2>/dev/null || REPORT_FILE=""
    # -ef, not string equality: report mode accepts a relative path, a later PR-scoped lookup publishes it absolute
    if [ "$ASSESS_RC" -ne 0 ] || [ -z "$REPORT_FILE" ] || [ -z "$INTAKE_ACCEPTED" ] || ! [ "$REPORT_FILE" -ef "$INTAKE_ACCEPTED" ]; then
        : > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}"  # paused on the triage question; Intake proceed re-arms it
        # targeted-rerun admissibility comes from fill-gaps' own planner, never re-derived in prose
        python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fill_gaps_scope.py" triage --assessment="$_GAPS" --pr="$PR_NUMBER" > "$_RERUN" || : > "$_RERUN"; cat "$_RERUN"  # timeout: 15000
        [ "$ASSESS_RC" -eq 0 ] && echo "REPORT_STATUS=gaps" || echo "REPORT_STATUS=gaps-critical"
        echo "GAPS_FILE=$_GAPS"
        echo "RERUN_FILE=$_RERUN"
        exit 0
    fi
    echo "→ Intake degraded, accepted earlier this run: $REPORT_FILE"
fi
IFS= read -r REPORT_FILE < "$_SENTINEL" 2>/dev/null || REPORT_FILE=""
[ -f "$REPORT_FILE" ] || REPORT_FILE=""  # sentinel may outlive its report (TTL sweep, failed --path-out write)
echo "$REPORT_FILE" > "$_SENTINEL"
case "$REPORT_FILE" in
    "")                      echo "REPORT_STATUS=missing" ;;
    .reports/codex/review/*) echo "REPORT_STATUS=codex-lineage" ;;
    *)                       echo "REPORT_STATUS=ok" ;;
esac
echo "REPORT_FILE=$REPORT_FILE"
```

Branch on the printed `REPORT_STATUS` — read it from stdout, never assume it:

- `blocked` → stop the fix pipeline; retain the printed reason (rejected PR, including an incomplete report that still records a rejection; failed path publication; or an invalid `--pr` value). Diagnostic/recovery questions remain available. Repair or rerun the producer before consuming findings; never treat this as `missing` or start remediation from its notes.
- `gaps` / `gaps-critical` → **Gap triage** below. The first printed JSON line (also saved at `GAPS_FILE`) is the whole assessment, the second (`RERUN_FILE`) the targeted-rerun verdict; `gaps-critical` has already emptied the report sentinel, so no later step can consume that report.
- `ok` → print `→ Reusing review report: <REPORT_FILE>`; `report` mode continues at Step 3a, `pr + report` at Step 3c, `pr` mode at Step 2 (pr mode never merges report findings). **Never start a review when a report is already resolved.**
- `codex-lineage` → this parser reads `oss:review`'s section schema only, not codex's flat H1/H2/M1-bullet schema. Treat as `missing` for the gate below, stating the lineage as the reason.
- `missing` with **no `PR_NUMBER`** (bare `report` on the current branch) → nothing to offer: there is no second source and no PR to review. Stop with `No review report found in .reports/review/ or .reports/codex/review/ — run /oss:review <PR#> first, or provide a PR number`.
- `missing` with a known `PR_NUMBER` → **do not spawn anything.** The caller asked for report findings; producing them is a decision, not a fallback. Invoke `AskUserQuestion` (actual tool call — prose question is a violation):

<!-- branch: report-missing — fires only when `report` requested, PR# known, and no readable report resolved -->

```text
"No readable review report for PR #<N>. How should /oss:resolve get the findings you asked for?"
  (a) Continue without report findings — GitHub comments only (pr + report mode only)
  (b) Stop — print the /oss:review <PR#> command to run first, then re-invoke resolve  (Recommended)
  (c) Abort — I'll run the review myself
```

- Offer (a) only in `pr + report` — bare `report` has no second source, so its menu is (b)/(c).
- Selected (a) → print `⚠ no report findings merged — GitHub comments only` and continue in pr mode.
- Selected (b) → print `→ Run /oss:review <PR#>, then re-invoke /oss:resolve <PR#> report`, and stop. **Never invoke `Skill(skill="oss:review", …)` here** — `oss:review` ends its own run in a Step 7a `AskUserQuestion` asking the user what to do next, so there is no structural "return" to resume this block on: the whole nested multi-agent fan-out would run only to leave the resume instruction sitting in context, unenforceable across the exact kind of compaction this fix exists to survive. Print the command and let the user re-invoke `/oss:resolve` themselves — the same pattern `oss:review`'s own Step 7a already uses.
- Selected (c) → stop.

#### Gap triage

<!-- branch: report-gaps — fires only when the gate printed incomplete-review-report and --assess graded it -->

Never self-heal: no edit or regeneration of the review's report, `review-validation.json` or `findings.jsonl`, whatever the user picks; Step 11's `resolution.jsonl` append stays the only write into that run.

1. Print the gap table, once, as the last text before the question. First line `Review report <report> — <critical> critical · <degraded> degraded gaps (contract <contract>)`, then `| Rule | Severity | Subject |` with one row per `gaps[]` entry of the printed JSON, critical rows first. A non-empty `recorded_rejection` adds the line `⛔ report records a rejection (<recorded_rejection>) it cannot certify — a full rerun re-decides it`. The question text carries a plain summary of the gap table regardless — printed text before a tool call can vanish, so the decision never depends on it — in at most 6 lines: the first line, the rejection line when present, the `RERUN_FILE` verdict's `reason` when it is not admissible, then critical gaps as `rule subject`, as many as fit with a final `+N more` when they do not, and as its last line `Long wait? /compact now — gap assessment saved at <GAPS_FILE>, resume lossless.` (the question may idle past the prompt-cache lifetime).
2. Invoke `AskUserQuestion` (actual tool call), one question, `multiSelect: false`. The rerun options need a PR number — `PR_NUMBER`, else the `RERUN_FILE` JSON's `pr` (the report header's PR); none → drop both reruns. **Rerun review for selected gaps** is offered only when the `RERUN_FILE` JSON says `admissible: true` (empty or unreadable = not admissible): `fill_gaps_scope.py triage` applies the planner's own checks — selectable subjects, recorded commit id, moved head, terminal REJECT, and whether the carried records can pass the publication gate — and always requires a QA or security role a gap names. Required subjects may include a carried record no gap names (for example the candidate behind a row the grader could not attribute, or behind a carried row resolve's merge cannot use). Nothing required and no gap-named subject → the optional lists hold every recorded subject that plans alone. A gap-named QA or security role that no request can name or re-run makes the rerun not admissible. Not admissible → its `reason`, which always points to a full review, goes in the question summary (step 1). The gap JSON's `head_moved` is `null` (PR head not checked) and the rerun verdict is admissible → offer the same options but mark none recommended: the planner re-checks the head and may still refuse.
   - `gaps` → (a) **Proceed with valid parts** · (b) **Rerun review for selected gaps** (Recommended) · (c) **Full rerun** · (d) **Abort**. Not admissible → (a) **Proceed with valid parts** · (b) **Full rerun** (Recommended) · (c) **Abort**. No PR number → (a) **Proceed with valid parts** · (b) **Abort**, no option marked recommended.
   - `gaps-critical` → **never offer Proceed.** Admissible → (a) **Full rerun** · (b) **Rerun review for selected gaps** (Recommended) · (c) **Abort**. Not admissible → (a) **Full rerun** (Recommended) · (b) **Abort**. No PR number → no question: print `! BLOCKED — review report has critical gaps and names no PR — rerun /oss:review on its PR` and stop.
3. Act on the answer:
   - **Proceed with valid parts** → run the **Intake proceed** block below; on its `REPORT_STATUS=ok` continue as `ok` above. Confirmed `findings.jsonl` rows become action items through the normal Step 3a/3c merge, which passes `--accept-degraded` for exactly this report; the Phase 1 verdict re-check stays strict, so its parent proof is never reused as a shortcut; `open_concerns` stay non-actionable (`report-intelligence.md` **Review findings source**); the final report's **Review Intake** section lists every gap (Step 11).
   - **Rerun review for selected gaps** → choose the subjects from the `RERUN_FILE` JSON; never re-derive them from the gap table. `required_roles` and `required_candidates` are always in the selection (the rerun refuses without them). The optional `roles` and `candidates` are the user's choice: one optional subject and nothing required → no follow-up, take it. One optional subject beside required ones → a follow-up `AskUserQuestion`, `multiSelect: false`, options `Include <subject>` and `Required only`. 2–16 → a follow-up `AskUserQuestion`, `multiSelect: true`, one option per optional subject labelled `Role: <name>` or `Candidate: <id>`, split as evenly as possible over ceil(n/4) questions (`Gaps 1/2`, …), so each carries 2–4 options and the call at most 4 questions; its question text names the required subjects as already included. Past 16 → one question, `multiSelect: true`, options `All roles` and `All candidates` when both optional kinds exist; a single kind → `multiSelect: false`, options `All <kind>` and `Required only`. Print `→ Run /oss:review <N> --fill-gaps "<report>" --roles "<role,role>" --candidates "<id,id>"` with the JSON's `report` path inside the double quotes (paths may hold spaces), omitting a flag whose list is empty, then `→ then re-invoke /oss:resolve <same arguments>`, and stop. Nothing selected and nothing required → stop as **Abort**.
   - **Full rerun** → print `→ Run /oss:review <N>, then re-invoke /oss:resolve <same arguments>` and stop.
   - **Abort** → stop.

Neither rerun invokes `Skill(skill="oss:review", …)` — same reason as the report-missing gate's (b): review ends in its own `AskUserQuestion`, leaving no return point to resume intake on. The block already cleared the liveness sentinel for every stop.

**Intake proceed** — only on **Proceed with valid parts**. It re-grades the published report rather than trusting the earlier answer, so a stray run can never accept a critical or rejecting report:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
[ -f "$REPORT_FILE" ] || { echo "! BLOCKED — no consumable report published; Proceed needs zero critical gaps"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/find_review_report.py" --report "$REPORT_FILE" --assess > "${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}" || { echo "! BLOCKED — report has critical gaps; Proceed is not available"; exit 1; }  # timeout: 15000 — may call gh for head_moved
grep -q '"gate": "Gate: REJECT_' "${TMPDIR:-/tmp}/resolve-intake-gaps-${CSID}" && { echo "! BLOCKED — report records a rejection; Proceed is not available"; exit 1; }
printf '%s\n' "$REPORT_FILE" > "${TMPDIR:-/tmp}/resolve-intake-degraded-${CSID}"
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) echo "report" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; *) echo "$PR_NUMBER" > "${TMPDIR:-/tmp}/oss-resolve-active-${CSID}" ;; esac
echo "⚠ Review intake degraded — consuming the valid parts of $REPORT_FILE"
echo "REPORT_STATUS=ok"
echo "REPORT_FILE=$REPORT_FILE"
```
