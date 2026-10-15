<!-- file: fill-gaps.md — consumers: plugins/cc_oss/skills/review/SKILL.md -->

# Fill-gaps mode — targeted rerun of one published run

`/oss:review <PR> --fill-gaps <run>/review-report.md --roles "<role>,…" --candidates "<id>,…"`. Re-run only named reviewer roles, re-validate only named candidates of one prior run; carry everything else unchanged into a NEW run dir. Usual caller: `/oss:resolve` intake triage. Prior run read-only — never edit, never re-mint, never delete it.

Names come verbatim from the prior run's `review-validation.json`: roles = `assessments[].role`, candidates = `candidates[].id` (any disposition). Either list may be empty, not both. A named candidate already CONFIRMED on the reviewed head, passing every publication rule with no open concern, but lacking its `findings.jsonl` row (`finding-missing` gap) lands in plan `publish_rows`: carried verbatim, never re-validated, only its row published. Every other named candidate lands in plan `candidates` (re-validate).

## Gate — before the existing-report guard, before any allocation or spawn

`bin/fill_gaps_scope.py plan` checks: run layout `.reports/review/pr-<PR>/run-<NNN>/review-report.md`; readable v1 sidecars + `head-sha.txt` holding a 7–40 hex commit id; not a REJECT run; every name known; every role maps to a review dimension (Codex role → bridge available); PR head starts with prior `head-sha.txt`; no gap a rerun cannot repair (`sidecar-*`, `contract-unsupported`, `concern-unknown-candidate`, a findings row naming no candidate); no carried candidate bound to another revision; carried projection passes the publication gate's record rules and its producer row rules (every carried findings row merge-ready, ids unique). Projection = prior envelope − re-run roles' assessments − re-validated candidates with their concerns and rows + one row per `publish_rows` id, every other record verbatim; only what the new consolidator may write varies: aggregate readiness, aggregate outcome (recomputed over the projected assessments: null only beside a carried completed undecidable reviewer, judged only beside a carried judging one, either with ≥1 re-run role; the prior's own label never counts), re-run roles' assessments, an added concern for an open carried candidate lacking one. `publish_rows` = named CONFIRMED candidates on that head that pass every record rule, have no open concern and lack only their row; any other named candidate is re-validated. Refusal names the carried records whose addition to `--roles`/`--candidates` makes the projection pass (a carried row failing a producer row rule names its candidate), or says none can (a failing row naming no recorded candidate needs a full review). Pass → plan + head sentinels written, summary and cost line printed. Refuse → `! BLOCKED — <reason>`, both sentinels emptied.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
IFS= read -r FILL_REPORT < "${TMPDIR:-/tmp}/oss-review-fill-report-${CSID}" 2>/dev/null || FILL_REPORT=""
IFS= read -r FILL_ROLES < "${TMPDIR:-/tmp}/oss-review-fill-roles-${CSID}" 2>/dev/null || FILL_ROLES=""
IFS= read -r FILL_CANDIDATES < "${TMPDIR:-/tmp}/oss-review-fill-candidates-${CSID}" 2>/dev/null || FILL_CANDIDATES=""
# `=` form: a value starting with `--` stays a value, never an option
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fill_gaps_scope.py" plan --pr="$CLEAN_ARGS" --report="$FILL_REPORT" --roles="$FILL_ROLES" --candidates="$FILL_CANDIDATES" --snapshot="$SNAP_DIR/pr-meta.json" --plan-out="${TMPDIR:-/tmp}/oss-review-fill-plan-${CSID}" --head-out="${TMPDIR:-/tmp}/oss-review-fill-head-${CSID}" || exit 1  # timeout: 10000
# resolve ledger beside the selected run → Step 2 prior-resolution propagation; guard skips in this mode
_PRIOR_RES="${FILL_REPORT%/*}/resolution.jsonl"
[ -s "$_PRIOR_RES" ] || _PRIOR_RES=""
printf '%s\n' "$_PRIOR_RES" > "${TMPDIR:-/tmp}/oss-review-prior-resolution-${CSID}"
```

Exit 1 → print the `! BLOCKED` line(s) as plain reply text and stop. No `AskUserQuestion`, no fallback to a full review on own initiative — the line names `/oss:review <PR>` when a full rerun is the way out. `TaskUpdate(deleted)` every Step 1–5b task.

Exit 0 → print the summary lines (incl. `cost:`) as reply text before any spawn, then continue the normal workflow with the narrowing below.

## Narrowed steps

| Step | Fill-gaps behaviour |
| -- | -- |
| 0 existing-report guard | Skipped (block exits `PRIOR_REPORT=` empty) — prior run named explicitly, no reuse question. |
| 1 | Runs as normal (PR_TYPE, CI, scope, codemap). Acceptance gate: Stage 1 skipped, Stage 2 runs (fresh CI → `GATE`). Linked-issue analysis and Agent 0 only when plan units hold `review-issue` / `blind-solve`. |
| 2 allocation | Unchanged block allocates `run-<N+1>`; plan copied to `$REPORT_DIR/fill-gaps.json`; `head-sha.txt` = prior run's string (carried candidates bind to it exactly). |
| 2 launch | See §Step 2. |
| 3 | Skipped; `TaskUpdate(deleted)` rides Step 4's first call. |
| 4 | See §Step 4. |
| 5 | Extra binding + carry check, see §Step 5. Publication gate unchanged. |
| 5b–8 | Unchanged. |

## Step 2

- Lineup = plan `units` exactly: one spawn per unit, `name` = unit (`review-qa`, `review-perf-api`, …), `subagent_type` = unit `agent`; `codex` unit = the `bridge:review` Skill call. No ranking, no `FANOUT_MAX`, no auto-pinned qa-specialist, `--no-challenge` never drops a selected challenger.
- Merged unit (`review-perf-api`, `review-docs-lint`) → prompt carries only dimensions whose files the unit lists.
- Each dimension prompt: normal template + one line `Fill-gaps run: re-assess as role "<role>" for this PR head; other roles were assessed in an earlier run.` Never pass prior findings — independent eyes.
- `agent-watch-review.tsv` + `.expected-files`: one row per unit file. Zero units (candidates-only) → no dimension spawn, no watch file; go to Step 4.

## Step 4

- Parent validates: plan `candidates` (start from their records in `prior_validation`) + every new claim in re-run agent files. Never re-validate a carried candidate; `publish_rows` ids are carried — no parent re-check, no verifier.
- New claim matching a carried candidate (same file + claim) → not new; carried record stands, add nothing.
- New candidate ids never reuse any prior id.
- Every re-validated or new candidate binds `revision`, and each check's `revision`, to plan `head_sha` — the new `head-sha.txt` string.
- Independent verifiers only for critical/blocking among these; cap 3 as normal.
- `cross-validation.md` holds only these candidates plus the re-run roles' assessments.

## Step 5

Consolidator prompt adds `FILL_GAPS_PLAN=<literal $REPORT_DIR/fill-gaps.json>`; consolidator merges per `consolidator-prompt.md` §Fill-gaps carry-over, incl. one new `findings.jsonl` row per `publish_rows` id. Zero units and zero `candidates` (publish-rows only) → skip Steps 2 launch and 4; consolidator only.

### Carry check

After the publication summary, before the Step 5 publication gate:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
[ -n "$_REPORT_DIR" ] || { echo "! BLOCKED — report-dir sentinel missing; cannot check fill-gaps carry-over"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fill_gaps_scope.py" verify --report "$_REPORT_DIR/review-report.md"  # timeout: 10000
```

- Exit 0 (`[carry] intact`) → Step 5 publication gate.
- Exit 1, re-spawn unused → run the publication gate's park block (SKILL.md Step 5), then re-spawn the consolidator **once**, same prompt plus `Carry-over check rejected: <every printed [carry] line>. The refused report is parked at <REPORT_DIR>/review-report.refused.md; write the corrected <REPORT_DIR>/review-report.md and restore each carried record verbatim from the prior run; only additions are allowed.` Re-run this block. The re-spawn budget is shared with the publication gate: one per run.
- Exit 1 once the re-spawn is used, or the re-spawn ⏱ timed out or returned no publication → run the publication gate's truncation block (SKILL.md Step 5), print `! BLOCKED — fill-gaps run altered carried prior records: <reasons>; report kept at <path> for inspection` and stop before Step 5b.
