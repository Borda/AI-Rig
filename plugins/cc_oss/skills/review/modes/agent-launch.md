<!-- file: agent-launch.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:review -->

## Step 2: Codex + parallel agent launch

Set up run directory (shared by all agents) and resolve skill paths.

**Step 2 prelude — one call**: the run-dir allocation, then the file-handoff protocol, Codex availability and the agent prompt templates, all in one block; nothing between them depends on a decision.

<!-- loads: agent-prompts.md -->

<!-- loads: provision-env.md, claim-repro.md -->

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
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
_HEAD_SHA=$(jq -r '.headRefOid // empty' "$SNAP_DIR/pr-meta.json" 2>/dev/null)
_RUN_MODE=fresh
# fill-gaps: keep the prior run's exact revision string — carried candidates bind to it (modes/fill-gaps.md)
if [ -n "$_FILL_HEAD" ]; then
    _RUN_MODE=fill-gaps
    _HEAD_SHA="$_FILL_HEAD"
    cp "${TMPDIR:-/tmp}/oss-review-fill-plan-${CSID}" "$RUN_DIR/fill-gaps.json" &&
        cp "$RUN_DIR/fill-gaps.json" "$REPORT_DIR/fill-gaps.json" || exit 1
fi
[[ "$_HEAD_SHA" =~ ^([0-9a-f]{7,40}|[0-9a-f]{64})$ ]] || { echo "! BLOCKED — frozen review head unavailable"; exit 1; }
printf '%s\n' "$_HEAD_SHA" > "$RUN_DIR/head-sha.txt" &&
    cp "$RUN_DIR/head-sha.txt" "$REPORT_DIR/head-sha.txt" &&
    printf '%s\n' "$_RUN_MODE" > "$RUN_DIR/review-mode.txt" &&
    printf '%s\n' "$REPORT_DIR" > "$RUN_DIR/report-dir.txt" &&
    printf '%s\n' "$SNAP_DIR" > "$RUN_DIR/snapshot-dir.txt" || exit 1
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

**Reviewer environment and author claims — before the PR-code gate plans its command.** Load and follow `modes/provision-env.md` (provision block) and the extract block of `modes/claim-repro.md` §1, in that order. Provisioning comes first because the gate binds an exact argv and that argv must name `env_python` from `$RUN_DIR/review-env.json`. Docs-only mode skips both: no PR code runs in it.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_SKILL_DIR < "${TMPDIR:-/tmp}/review-skill-dir-${CSID}" 2>/dev/null || REVIEW_SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$REVIEW_SKILL_DIR/modes/provision-env.md"  # timeout: 10000
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$REVIEW_SKILL_DIR/modes/claim-repro.md"  # timeout: 10000
```

**IMPORTANT**: Replace `$REPORT_DIR`, `$REVIEW_SKILL_DIR`, `$BRANCH`, and `$DATE` with actual literal computed values in every Agent spawn prompt. Do NOT pass as shell variables — agents receive text, not shell context. **Exception — `$RUN_DIR`**: never hand-substitute it; agents self-resolve via `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"` per the run-dir preamble in `agent-prompts.md` (eliminates leading-dot transcription slips).

Also substitute the full snapshot `PR_HEAD_SHA` and include the shared read-only/frozen-source preamble in every source-reading prompt, including issue agents, verifiers and the consolidator. Agent 0 alone reads the frozen base SHA instead of the head.

Bind `CONVERSATION_SNAPSHOT` in every dimension prompt and the bridge review from the printed value below. Agent 0's blind solve keeps its base-only inputs.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
if [[ -n "$SNAP_DIR" && -f "$SNAP_DIR/conversation-coverage.txt" ]]; then
    printf 'CONVERSATION_SNAPSHOT=%s\n' "$SNAP_DIR"
else
    printf '%s\n' 'CONVERSATION_SNAPSHOT=unavailable'
fi
```

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
IFS= read -r IMPACT_LINE < "${TMPDIR:-/tmp}/oss-review-impact-${_PR_TAG}-${CSID}" 2>/dev/null || IMPACT_LINE="FULL · impact unknown: codemap unavailable"
echo "IMPACT_TIER=${IMPACT_LINE%% *}"
echo "IMPACT=$IMPACT_LINE"
_STRUCTURAL_RECORD="${TMPDIR:-/tmp}/oss-review-codemap-structural-${_PR_TAG}-${CSID}.json"
if [ -s "$_STRUCTURAL_RECORD" ]; then cp "$_STRUCTURAL_RECORD" "$RUN_DIR/codemap-structural-context.json"; fi
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
   - **Promotion — inside the cap, before ranking.** A surviving unit whose territory holds a promotion trigger is *promoted* and takes a slot ahead of every unpromoted unit. Triggers: docs+lint (or standalone doc-scribe) ← a changed `CHANGELOG`/`CHANGES`/`HISTORY`/`NEWS` file or a changed migration or upgrade guide; sw-engineer ← a changed GitHub Actions file (`.github/workflows/`, `.github/actions/`) in a code lineup, where `oss:cicd-steward` is not spawned — a CI test job goes to the qa-specialist instead, which is pinned outside the cap and takes no slot; perf+arch ← a stated performance claim (PR title, body, commit message or changed changelog line asserting a speed, memory, latency or throughput change). `oss:cicd-steward` is never promoted: it spawns only in CI-only and docs+CI modes, where the lineup has no ranking contest. Name each trigger in the receiving unit's prompt line 1 delta. Rank promoted units among themselves by the same evidence, then fill the remaining slots by ranking. `FANOUT_MAX` and `--full` stay the cost decision: promotion never adds a slot.
   - A promoted unit left without a slot (more promoted units than `FANOUT_MAX`) is an unreviewed dimension, never a silent drop: the consolidator records it as a missing required reviewer (`presence: missing`), so Readiness is `incomplete`. An unpromoted unit dropped by the cap leaves Readiness unchanged and carries the dropped-unit confidence ceiling (`modes/verdict-states.md`).

- More work → give each agent more, never add agents.
- **Spawn fewest that keep each near `AGENT_CALL_BUDGET`** — not the most the cap allows. Small diff → merge surviving units into fewer spawns, **never zero**.
- **Fill-gaps exception**: `FILL_GAPS_MODE=true` → the lineup is exactly the plan's units (`modes/fill-gaps.md` §Step 2) — no ranking, no cap, no auto-pinned qa-specialist; a candidates-only plan spawns zero dimension agents.
- **Diff size never licenses inline review.** ~73-call inline rule covers work-displacement spawns only; review specialists are role-isolated (own system prompt, independent eyes), so it does not apply here. Small diff ≠ low risk: few-line change on hot path often riskiest. Orchestrator never substitutes itself for a ranked unit, pinned qa-specialist, challenger (`CHALLENGE_ENABLED=true`), or Codex (`CODEX_AVAILABLE=1`).
- **Merge before you split**: two dimensions whose files overlap go to one agent, not two.
- Every spawn prompt states budget, requires an envelope even on exhaustion — `partial: true` plus what was finished. An agent stalling past ~60 calls without an envelope forces full disk reconstruction.
- In the launch turn of every fresh review that reaches fan-out, Write the full actual launch lineup once to `$RUN_DIR/lineup.launch.md`, then copy it into the report directory with the fixed parent recovery block in `modes/post-agent.md` before dispatch. It restores both `$REPORT_DIR/lineup.launch.md` and `$REPORT_DIR/lineup.md`. Include one line per scope-preselected unit and every launched review unit, including launches outside the cap. Use `<unit> | spawned | roles: <role>, <role>`, `<unit> | spawned | promoted: <trigger> | roles: <role>, <role>`, `<unit> | promoted-unslotted: <trigger> | roles: <role>, <role>` or `<unit> | dropped`. Record every exact assessment role on each `spawned` row, including each role covered by a merged spawn. Use the same names in the reviewer handoff and in `review-validation.json`. For a `promoted-unslotted` row, list every exact preselected assessment role that would have run. The publication gate compares the report copies and the consolidator discloses units dropped by the cap. Fill-gaps runs use the plan's exact `roles` and write no lineup file.

**Dimension-gated codemap supplement** — after the ranked lineup is decided and when `codemap_available=true`: run the qa block (qa-specialist in lineup) and/or docs block (documentation dimension in lineup — merged lint+docs unit or standalone doc-scribe) from codemap-context.md §Dimension-gated supplement, then re-run the copy block above so `$RUN_DIR/codemap-context.md` carries the supplement. Neither agent in lineup → skip (that is the point: those queries were 57% of battery volume feeding agents the cap usually drops).

Launch the bridge review, the issue agent (one spawn for all linked issues), and the review agents ranked under `FANOUT_MAX` in one message batch. When `CODEX_AVAILABLE=1` and DOCS_TYPING_MODE/TESTS_CI_MODE are false, that message also carries `Skill(skill="bridge:review", args="Read-only adversarial review of <REVIEW_TARGET>, using changed files and <RUN_DIR>/codemap-context.md when present. Identify bugs, missed edge cases, and inconsistencies; return every finding with its severity and exact file:line evidence, and do not apply fixes. End the verdict string with one line: source-5-v2 rating: <1-5> — <one-sentence rationale>, where 1 Approve, 2 Minor changes, 3 Major revision, 4 Insufficient but a good initiative, 5 Block.")`. The bridge runs the peer under a fixed output schema, so free text lands only in `verdict` or `details`: the consolidator reads the rating line from `verdict` in `codex-review-reply.json`, never from the transcript. It gives the consolidator a scoped Codex judgment; without it a completed Codex reply is no rating. The `Skill()` call returns bridge instructions, not a running review: run its `bridge_call.py review` command as a background Bash call (`run_in_background: true` — the spawned agents are the independent work bridge:review names), then end the turn. Codex runs read-only and cannot write files, so on that call's exit notification Write its findings (envelope plus the transcript at the artifact path it reports) to `$RUN_DIR/foundry--codex.md` with the Write tool; a failed, timed-out or blocked envelope → write the status and fault there instead, never an empty file. Whenever the call returned a JSON object, also Write the returned JSON object verbatim to `$RUN_DIR/codex-review-reply.json`: Step 4 reads its harness incident to tell a quota stop (`post-agent.md`).

**Gate Codex quota flag.** `GATE_CODEX=quota-blocked` from the Step 1 gate challenger (`acceptance-gate.md`) changes this launch, and Step 4 challenger verifiers then run without Codex (`post-agent.md`):

- No `bridge:review` call, even with `CODEX_AVAILABLE=1`: after a quota stop no Codex call in the same session can succeed.
- Write `$RUN_DIR/foundry--codex.md` as one line, `⊘ codex-blocked:quota — gate challenger's Codex cross-check stopped on quota; bridge review not launched`, so the report says why and Step 4 still finds the stop after a compaction. It gets no watch row: no call runs.

**Bridge absent.** `CODEX_AVAILABLE=0` (the prelude printed `bridge@borda-ai-rig is <status> — skipping co-review`) and DOCS_TYPING_MODE/TESTS_CI_MODE false → Write `$RUN_DIR/foundry--codex.md` as one line, `⊘ codex not run: peer-unavailable — bridge@borda-ai-rig is <status>`. `peer-unavailable` is the bridge's own `confidence_null_reason` for a child that never started, so the consolidator names the missing lane with one code set whether the bridge was absent or could not start. It gets no watch row: no call runs.

Append to the bridge `args` and its resulting review prompt before execution: `Reviewed source: <full PR_HEAD_SHA>. Read repository source with git show <PR_HEAD_SHA>:<path> and search with git grep <pattern> <PR_HEAD_SHA> -- <paths>. Working-tree reads, codemap and targeted tests require a clean checkout whose HEAD equals that SHA immediately before use, and a command that loads or executes PR code (a test, a reproduction probe, an overlay run, a type checker or linter that loads project plugins, a build or a dependency export) also follows <RUN_DIR>/code-clause.txt (absolute path substituted; a missing file means no run); otherwise use frozen Git reads and report execution unavailable. Never edit tracked files or mutate the project environment.` Substitute the snapshot SHA as a literal; `<REVIEW_TARGET>` is that frozen commit, never the invoking branch.

Spawns are background: issue the whole batch in one message, then **end the turn**; follow `<constants>` §Agent health monitoring. Each agent's completion notification wakes the orchestrator; check expected output files then. An agent whose completion or idle notification arrives while its expected output file is still missing is ⏱ `timed_out` at once — never wait on it further, and never leave it for the user to ask about.

Persist the monitor list from the **actual launch batch** — never re-derive it from scope/mode flags (flag-derived lists include ranking-dropped dimensions; the monitor would then wait out a deadline per never-spawned agent). In the same turn as the launch message, use the **Write tool** (the lineup is a ranking decision the shell cannot see) to create `$RUN_DIR/agent-watch-review.tsv`: one row per output file of every agent actually spawned, `<agent name><TAB><absolute output path><TAB>1800` (`AGENT_DEADLINE_S`) — a merged spawn gets one row per file it writes. In the same response also write `$RUN_DIR/.expected-files` exactly as before (one absolute path per line, one line per output file of every agent actually spawned), so the run dir keeps every artifact it always had:

- `$RUN_DIR/foundry--codex.md` — only when the bridge review launched
- `$RUN_DIR/issue-<N>.md` — one per linked issue the issue agent covers
- one `$RUN_DIR/<agent>.md` per spawned review agent — basenames: `foundry--sw-engineer.md`, `foundry--qa-specialist.md`, `foundry--perf-optimizer.md`, `foundry--doc-scribe.md`, `foundry--linting-expert.md`, `foundry--solution-architect.md`, `foundry--challenger.md`, `oss--cicd-steward.md`, `foundry--blind-solve.md` (Agent 0, FEATURE/MIXED only)

On each wake-up — a completion or idle notification per `<constants>` §Agent health monitoring — run this once before reading any agent output. Match the notification to its batch (`review`, `verify`, `consolidate` or `reply`) and require its own rows in the returned JSON. If that batch's rows are absent, stop with a missing watch record for that batch. Earlier batches cannot stand in for it.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — run-dir sentinel empty; agent deadlines cannot be checked"; exit 1; }
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

Follow the printed Boundary step before consolidation. Step 3 checks already issued on Step 2 wake-ups are unaffected. Reports are the preserved finding files in the run directory.

**Gap ledger preparation** — run before consolidation, including fill-gaps runs with no dimension watch file. The helper treats an absent review watch as an empty dimension batch.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" || exit 1
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" || exit 1
IFS= read -r _REPORT_DIR < "${TMPDIR:-/tmp}/oss-review-report-dir-${CSID}" || exit 1
[[ -n "$_RUN_DIR" && -n "$_REPORT_DIR" ]] || exit 1
IFS= read -r _SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || _SNAP_DIR=""
_PR_URL=$(jq -r '.url // empty' "$_SNAP_DIR/pr-meta.json" 2>/dev/null)
python "$PLUGIN_ROOT/bin/gap_emit.py" prepare --kind review --dir "$_REPORT_DIR" --plugin-root "$PLUGIN_ROOT" --pr "$_PR_TAG" --pr-url "$_PR_URL" --head-file "$_REPORT_DIR/head-sha.txt" --watch "$_RUN_DIR/agent-watch-review.tsv"  # timeout: 15000
```

The `GAP_*` lines are saved to `$REPORT_DIR/gap-prepare.txt` for the consolidator, which stages and appends this run's gap records. A `⚠ GAP_LEDGER=unavailable` line never stops the run.
