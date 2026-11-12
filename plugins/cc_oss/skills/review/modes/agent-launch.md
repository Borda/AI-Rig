<!-- file: agent-launch.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:review -->

## Step 2: Codex + parallel agent launch

Set up run directory (shared by all agents) and resolve skill paths.

**Step 2 prelude — one call**: the run-dir allocation, then the file-handoff protocol, Codex availability and the agent prompt templates, all in one block; nothing between them depends on a decision.

<!-- loads: agent-prompts.md -->

```bash
# timeout: 20000
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
RUN_DIR=".temp/review/$TIMESTAMP"
mkdir -p "$RUN_DIR" || { echo "! BLOCKED — run directory allocation failed"; exit 1; } # timeout: 5000
# persisted for cat resolve — hand-typing caused leading-dot drops → stray temp/review/ dirs
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"
# deliverable → main tree (worktree-isolation.md §review): --worktree sets orig-root at §Enter, else pwd — report stays reachable outside worktree; RUN_DIR stays worktree-local
IFS= read -r _REPORT_BASE < "${TMPDIR:-/tmp}/oss-review-orig-root-${CSID}" 2>/dev/null || _REPORT_BASE="$(pwd)"
[ -n "$_REPORT_BASE" ] || _REPORT_BASE="$(pwd)"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
# fail closed: a lost/emptied sentinel must never fall through into a bare "pr-" directory nothing can find later
[[ "$CLEAN_ARGS" =~ ^[0-9]+$ ]] || { echo "! BLOCKED — PR tag sentinel empty or non-numeric ('$CLEAN_ARGS') — refusing to allocate a report dir"; exit 1; }
# fill-gaps without a written plan must never fall through to a plain run at the current head
IFS= read -r _FILL_REPORT < "${TMPDIR:-/tmp}/oss-review-fill-report-${CSID}" 2>/dev/null || _FILL_REPORT=""
IFS= read -r _FILL_HEAD < "${TMPDIR:-/tmp}/oss-review-fill-head-${CSID}" 2>/dev/null || _FILL_HEAD=""
[ -z "$_FILL_REPORT" ] || [ -n "$_FILL_HEAD" ] || { echo "! BLOCKED — --fill-gaps has no plan (fill-gaps gate not passed); refusing to allocate a report dir"; exit 1; }
# PR-scoped, run-indexed (find_review_report.py + oss:resolve glob this shape — ls .reports/review/pr-<N>/ finds every run for that PR at a glance)
PR_REPORT_DIR="$_REPORT_BASE/.reports/review/pr-$CLEAN_ARGS"
REPORT_DIR=$(python "$PLUGIN_ROOT/bin/next_run_dir.py" --pr-dir "$PR_REPORT_DIR") || { echo "! BLOCKED — report directory allocation failed"; exit 1; } # timeout: 5000
[ -n "$REPORT_DIR" ] && [ -d "$REPORT_DIR" ] || { echo "! BLOCKED — report allocator returned no directory"; exit 1; }
echo "$REPORT_DIR" > "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}"  # persist for contract-write
# sidecar, not a header field: lets Step 0's existing-report guard tell "covers this head" from "stale";
# read from the snapshot, not the acceptance-gate sentinel (DOCS_TYPING/TESTS_CI never write that one)
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
_HEAD_SHA=$(jq -r '.headRefOid // empty' "$SNAP_DIR/pr-meta.json" 2>/dev/null)
# fill-gaps: keep the prior run's exact revision string — carried candidates bind to it (modes/fill-gaps.md)
[ -n "$_FILL_HEAD" ] && { _HEAD_SHA="$_FILL_HEAD"; cp "${TMPDIR:-/tmp}/oss-review-fill-plan-${CSID}" "$REPORT_DIR/fill-gaps.json"; }
[ -n "$_HEAD_SHA" ] && echo "$_HEAD_SHA" > "$REPORT_DIR/head-sha.txt" || :
# _OSS_SHARED + REVIEW_SKILL_DIR were persisted by Step 0 (Check 41)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/review-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
cat "$_OSS_SHARED/file-handoff-protocol.md" 2>/dev/null || echo "⚠ file-handoff-protocol.md absent — continuing without it"
CODEX_STATUS=$(python "$PLUGIN_ROOT/bin/check_bridge.py" --status 2>/dev/null || echo "absent")
if [ "$CODEX_STATUS" = "available" ]; then CODEX_AVAILABLE=1; echo "bridge@borda-ai-rig available"; else CODEX_AVAILABLE=0; echo "⚠ bridge@borda-ai-rig is ${CODEX_STATUS} — skipping co-review"; fi
echo "$CODEX_AVAILABLE" > "${TMPDIR:-/tmp}/oss-review-codex-available-${CSID}"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$REVIEW_SKILL_DIR/templates/agent-prompts.md"  # timeout: 10000
```

Follow the handoff protocol above. File absent → warn and continue without it.

**IMPORTANT**: Replace `$REPORT_DIR`, `$REVIEW_SKILL_DIR`, `$BRANCH`, and `$DATE` with actual literal computed values in every Agent spawn prompt. Do NOT pass as shell variables — agents receive text, not shell context. **Exception — `$RUN_DIR`**: never hand-substitute it; agents self-resolve via `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"` per the run-dir preamble in `agent-prompts.md` (eliminates leading-dot transcription slips).

Also substitute the full snapshot `PR_HEAD_SHA` and include the shared read-only/frozen-source preamble in every source-reading prompt, including issue agents, verifiers and the consolidator. Agent 0 alone reads the frozen base SHA instead of the head.

Template (loaded above). Substitute `<REVIEW_SKILL_DIR>` → `$REVIEW_SKILL_DIR` before using content in spawn prompts. Leave `$RUN_DIR` literal in the prompt text — agents resolve it themselves via the run-dir preamble (`cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"`); the orchestrator must NOT retype the run-dir path.

**Prior resolution propagation**: when the existing-report guard recorded a prior `resolution.jsonl` (`IFS= read -r PRIOR_RESOLUTION < "${TMPDIR:-/tmp}/oss-review-prior-resolution-${CSID}"` non-empty — a re-run after `/oss:resolve` acted on the earlier report), add one line to every dimension-agent prompt and the consolidator prompt: `Prior resolution ledger: <literal path> — one record per earlier finding and resolve run, the last record per finding_id winning (finding_id, title, section, file, line, verdict fixed/rejected/skipped/self-resolved/pending/unverified/already-fixed, sha, why). Match a ledger record to your finding by file and claim, never by id alone: ids hash the earlier run's wording. Confirm "fixed" and "self-resolved" findings are actually fixed at the current head; a record with "shared_with_github": true was folded into a GitHub item, so its verdict covers that combined item, not the finding alone. Historical "rejected" records may include unverifiable claims; new rejected records require affirmative refutation. An "unverified" record keeps its owner/next check open, and "already-fixed" retains the original claim and fix evidence. For every earlier rejected record: re-check it at the current head, report it again when the code still shows the problem, and say what the earlier rejection missed.` The consolidator template turns that line into a `Review Confidence` entry for every prior `rejected` finding it leaves out, so no omission is silent. No ledger → add nothing.

**Codemap context propagation**: rehydrate `codemap_available` from Step 1 persist file, copy staged context into `$RUN_DIR/codemap-context.md`, substitute into every dimension-agent spawn prompt per the rules in the Structural-context block above. Block omitted when `codemap_available=false`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; refusing to copy codemap context to a root-relative path"; exit 1; }
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG=""
IFS= read -r codemap_available < "${TMPDIR:-/tmp}/oss-review-codemap-available-${_PR_TAG}-${CSID}" 2>/dev/null || codemap_available="false"
# review depth by code path — gates FIX perf+arch skip before ranking; missing sentinel = FULL
IFS= read -r IMPACT_LINE < "${TMPDIR:-/tmp}/oss-review-impact-${_PR_TAG}-${CSID}" 2>/dev/null || IMPACT_LINE="FULL · impact unknown"
echo "IMPACT_TIER=${IMPACT_LINE%% *}"
echo "IMPACT=$IMPACT_LINE"
IFS= read -r CODEMAP_CONTEXT_STAGE < "${TMPDIR:-/tmp}/oss-review-codemap-context-stage-${_PR_TAG}-${CSID}" 2>/dev/null || CODEMAP_CONTEXT_STAGE=""
if [ "$codemap_available" = "true" ] && [ -n "$CODEMAP_CONTEXT_STAGE" ] && [ -f "$CODEMAP_CONTEXT_STAGE" ]; then
    cp "$CODEMAP_CONTEXT_STAGE" "$RUN_DIR/codemap-context.md"
fi
```

**Health monitoring** (CLAUDE.md §6): per-agent deadlines armed in the launch turn (below), checked by `agent_watch.py` at each wake-up — no checkpoint file, no `find -newer` probe.

**Spawn-count gate — apply before spawning anything.** Follow `agent-resolution.md` §Agent budget (loaded above). Rules, all mandatory:

Two stages, in order — never collapse them:

1. **Scope preselection** (always): scope/mode rules above decide which dimensions are *relevant at all*. A dimension with no changed file in its territory is out here, never comes back, at any flag. Paired dimensions (perf+arch, docs+lint — see agent-prompts.md §Merged spawn units) form one spawn unit: unit survives when either member does, prompt carries only surviving members' instructions.
2. **Relevance ranking** (default only): rank surviving units by evidence — changed files/lines in each unit's territory, what Step 1 pre-classification found, what structural context flagged — spawn top `FANOUT_MAX` (3). **qa-specialist pinned outside cap** — spawns on every CODE PR, except Docs-only mode, including CI-only, docs+CI and CHORE; scope preselection never removes it outside that exception and it never occupies a ranked slot. With `--full` (`FANOUT_CAP=0`) skip this stage, spawn every survivor of stage 1.

- More work → give each agent more, never add agents.
- **Spawn fewest that keep each near `AGENT_CALL_BUDGET`** — not the most the cap allows. Small diff → merge surviving units into fewer spawns, **never zero**.
- **Fill-gaps exception**: `FILL_GAPS_MODE=true` → the lineup is exactly the plan's units (`modes/fill-gaps.md` §Step 2) — no ranking, no cap, no auto-pinned qa-specialist; a candidates-only plan spawns zero dimension agents.
- **Diff size never licenses inline review.** ~73-call inline rule covers work-displacement spawns only; review specialists are role-isolated (own system prompt, independent eyes), so it does not apply here. Small diff ≠ low risk: few-line change on hot path often riskiest. Orchestrator never substitutes itself for a ranked unit, pinned qa-specialist, challenger (`CHALLENGE_ENABLED=true`), or Codex (`CODEX_AVAILABLE=1`).
- **Merge before you split**: two dimensions whose files overlap go to one agent, not two.
- Every spawn prompt states budget, requires an envelope even on exhaustion — `partial: true` plus what was finished. An agent stalling past ~60 calls without an envelope forces full disk reconstruction.
- Dimensions dropped by cap listed in report; never silently skipped.

**Dimension-gated codemap supplement** — after the ranked lineup is decided and when `codemap_available=true`: run the qa block (qa-specialist in lineup) and/or docs block (documentation dimension in lineup — merged lint+docs unit or standalone doc-scribe) from codemap-context.md §Dimension-gated supplement, then re-run the copy block above so `$RUN_DIR/codemap-context.md` carries the supplement. Neither agent in lineup → skip (that is the point: those queries were 57% of battery volume feeding agents the cap usually drops).

Launch the bridge review, the issue agent (one spawn for all linked issues), and the review agents ranked under `FANOUT_MAX` in one message batch. When `CODEX_AVAILABLE=1` and DOCS_TYPING_MODE/TESTS_CI_MODE are false, that message also carries `Skill(skill="bridge:review", args="Read-only adversarial review of <REVIEW_TARGET>, using changed files and <RUN_DIR>/codemap-context.md when present. Identify bugs, missed edge cases, and inconsistencies; return every finding with its severity and exact file:line evidence, and do not apply fixes.")`. The `Skill()` call returns bridge instructions, not a running review: run its `bridge_call.py review` command as a background Bash call (`run_in_background: true` — the spawned agents are the independent work bridge:review names), then end the turn. Codex runs read-only and cannot write files, so on that call's exit notification Write its findings (envelope plus the transcript at the artifact path it reports) to `$RUN_DIR/foundry--codex.md` with the Write tool; a failed, timed-out or blocked envelope → write the status and fault there instead, never an empty file.

Append to the bridge `args` and its resulting review prompt before execution: `Reviewed source: <full PR_HEAD_SHA>. Read repository source with git show <PR_HEAD_SHA>:<path> and search with git grep <pattern> <PR_HEAD_SHA> -- <paths>. Working-tree reads, codemap and targeted tests require a clean checkout whose HEAD equals that SHA immediately before use; otherwise use frozen Git reads and report execution unavailable. Never edit tracked files or mutate the project environment.` Substitute the snapshot SHA as a literal; `<REVIEW_TARGET>` is that frozen commit, never the invoking branch.

Spawns are background: issue the whole batch in one message, then **end the turn**; follow `<constants>` §Agent health monitoring. Each agent's completion notification wakes the orchestrator; check expected output files then. An agent whose completion or idle notification arrives while its expected output file is still missing is ⏱ `timed_out` at once — never wait on it further, and never leave it for the user to ask about.

Persist the monitor list from the **actual launch batch** — never re-derive it from scope/mode flags (flag-derived lists include ranking-dropped dimensions; the monitor would then wait out a deadline per never-spawned agent). In the same turn as the launch message, use the **Write tool** (the lineup is a ranking decision the shell cannot see) to create `$RUN_DIR/agent-watch-review.tsv`: one row per output file of every agent actually spawned, `<agent name><TAB><absolute output path><TAB>1800` (`AGENT_DEADLINE_S`) — a merged spawn gets one row per file it writes. In the same response also write `$RUN_DIR/.expected-files` exactly as before (one absolute path per line, one line per output file of every agent actually spawned), so the run dir keeps every artifact it always had:

- `$RUN_DIR/foundry--codex.md` — only when the bridge review launched
- `$RUN_DIR/issue-<N>.md` — one per linked issue the issue agent covers
- one `$RUN_DIR/<agent>.md` per spawned review agent — basenames: `foundry--sw-engineer.md`, `foundry--qa-specialist.md`, `foundry--perf-optimizer.md`, `foundry--doc-scribe.md`, `foundry--linting-expert.md`, `foundry--solution-architect.md`, `foundry--challenger.md`, `oss--cicd-steward.md`, `foundry--blind-solve.md` (Agent 0, FEATURE/MIXED only)

On each wake-up — a completion or idle notification per `<constants>` §Agent health monitoring — run this once, before reading any agent output (fresh shell; it fails closed on an unbound run dir):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; agent deadlines cannot be checked"; exit 1; }
[ -s "$RUN_DIR/agent-watch-review.tsv" ] || { echo "! BLOCKED — $RUN_DIR/agent-watch-review.tsv missing or empty; write it (one row per spawned agent output) in the launch turn"; exit 1; }
python "$PLUGIN_ROOT/bin/agent_watch.py" --state-dir "$RUN_DIR"  # timeout: 5000
```

Act on every row: `done` → consume · `timed_out`, or a notification that arrived while its file is still missing → ⏱ `timed_out` now; read any partial file and surface it with ⏱ in the report · rows still open with no notification → end the turn. Never omit timed-out agents, and a ⏱ never skips a user question.

After all outputs collected (or timed out):

Agent phase reports must be written to the run-dir before this refresh.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
ls "$RUN_DIR/"*.md 2>/dev/null || echo "⚠ No agent output files found in $RUN_DIR — check that $RUN_DIR was expanded correctly in spawn prompts"
# boundary1: post-fanout, pre-consolidation (compaction-contract.md §Lifecycle)
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || _RUN_DIR=""
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="unknown"
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" 2>/dev/null || _REPORT_DIR=""
_FINDING_FILES=$(ls "$_RUN_DIR/"*.md 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')
_PRESERVE="run-dir=$_RUN_DIR, report-dir=$_REPORT_DIR, pr=$_PR_TAG, finding-files=$_FINDING_FILES"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-sentinel oss-review-keep-items "oss:review" "consolidation (after parallel review-agent fan-out)" "$_RUN_DIR" "$_PRESERVE" "resume: re-cat own agent-resolution.md and file-handoff-protocol.md; consolidate findings → final report (→ draft --reply if reply-mode)" -- calls_ahead=20 "drop=expanded review findings"  # timeout: 5000
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/review-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
python "$PLUGIN_ROOT/bin/compaction_boundary.py" "$_OSS_SHARED"
```

Follow the Boundary step printed above before consolidation; Step 3 checks already issued on Step 2 wake-ups are unaffected. Reports are the preserved finding-files in the run-dir.
