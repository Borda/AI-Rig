<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: communication

> **Codex integration: disabled.** Problem generation and scoring Claude-only. Ground truth needs deep knowledge of `file-handoff-protocol.md`, `TEAM_PROTOCOL.md`, AgentSpeak v2 — Codex lacks context, produces superficial/wrong problems.

Handover + team protocol compliance. Included in `all`. Use explicit `communication` target to isolate.

Target agent: `foundry:curator`.

### Domain

Four subdomains — each ground truth issue must tag `subdomain` field so Phase 4 computes per-subdomain recall:

```text
handover: malformed JSON envelopes (missing summary, plain text instead of JSON,
missing required fields, wrong status value, severity not an object);
context-contamination: spawn prompts include full conversation history or out-of-scope details
instead of task-relevant context only;
agentspeak: team AgentSpeak v2 violations (verbose prose instead of compact JSON, task IDs not
preserved, handshake phrases not pruned);
completeness: given a full/raw agent response paired with its compact envelope, identify cases
where the envelope omits essential information (missing findings, dropped severity entries,
truncated gaps list, absent confidence score) — a correct compact form retains all
decision-relevant signal at ≤30% of raw token count, and any omission of a severity≥high finding
is a critical violation
```

**Ground truth format** (extended for subdomain tagging): `{"issue": "...", "location": "...", "severity": "...", "subdomain": "handover|context-contamination|agentspeak|completeness"}`

**Per-subdomain recall** (Phase 4 aggregate addition):

- `recall_handover`, `recall_context_contamination`, `recall_agentspeak`, `recall_completeness`
- computed as: issues found in subdomain / total issues in subdomain (omit if 0 issues for subdomain that run)
- surfaced in `benchmark-report.md` Aggregate section and `result.jsonl`; primary signal for context pollution detection

### Step 2: Spawn communication pipeline subagent

**N override** (communication problems high-complexity — tighter N prevents context overflow in pipeline subagent): fast=3, full=5. Do NOT use global FULL_N=10 here.

Mark "Calibrate communication" in_progress. Load the pipeline template — no `~/.claude/skills/` copy exists (setup symlinks only `rules/*.md` and `TEAM_PROTOCOL.md`):

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "$PLUGIN_ROOT/bin/load_mode.py" calibrate templates pipeline-prompt.md $([ "$LOCAL_MODE" = "true" ] && echo --local)  # timeout: 5000
```

Use the standard pipeline template printed above with `<TARGET>` = `foundry:curator`, `<DOMAIN>` = domain string above, `<N>` = 3 (fast) or 5 (full), `<TIMESTAMP>` = current run timestamp, `<MODE>` = `fast` or `full`, `<AB_MODE>` = `true` or `false`, `<LOCAL_MODE>` = persisted local mode, `<TARGET_LABEL>` = `<RUN_SLUG>` = `communication`, `<PROBLEM_SET_VERSION>` = SKILL.md constant, `<FLOORS_ARG>` = empty. Domain keys (`mode:`, `haystack:`, `template_sections_not_fp:`, `sev_label:`, `real_fixtures:`, `fetch_store:`, `precision_gate:`) are read by the pipeline from `<DOMAIN>` itself, empty when absent. Spawn **single** `general-purpose` pipeline subagent with `model="sonnet"` — runs curator against synthetic agent responses, full/compact response pairs, team transcripts with injected violations.

**Phase 2 batching**: pipeline spawns Phase 2 target agents one at a time, collecting each result before dispatching the next, within SKILL.md's session-wide spawn budget. Each curator response ~1–4KB; Sonnet/Opus scorers and curator proposals also remain serial, while Haiku probes may use the reserved limit up to 4.

Run dir: `.reports/calibrate/<TIMESTAMP>/communication/` (relative to project root; distinct from the `foundry:curator` agent calibration)

### Active instruction — token optimization (additional scoring measure)

Append to every `task_prompt` in Phase 1 for `communication` problems:

> "Produce most compact output preserving all decision-relevant information. Omit prose where field name and value are self-evident. Any finding at severity≥high must appear; lower-severity findings may be summarized. Target: ≤30% of raw response token count without losing critical signal."

Scorer (Phase 3) evaluates two additional dimensions independently:

1. **Completeness loss** — essential fields omitted vs. total essential fields → `completeness_loss_ratio`
2. **Token overhead** — response size vs. minimum faithful representation → `token_overhead_ratio`

**`token_overhead_ratio` baseline — ground truth JSON char count**: compute `len(JSON.stringify(ground_truth))` (char count of serialised `GROUND_TRUTH_JSON` scorer holds) — minimum lossless representation of all required findings. Ratio `response_chars / gt_json_chars` measures overhead above that floor.

- ≤1.5 ✓ compact — fits within 1.5× bare findings (allows confidence block, location formatting, severity labels)
- 1.5–2.0 ~ moderate — some prose wrapping, acceptable
- > 2.0 ⚠ verbose — significant narrative overhead above minimum content

For scope problems (ground_truth = []) use `response_chars / 50` as baseline (50 chars ≈ one-line decline/redirect). Set `completeness_loss_ratio = 0.0` if response correctly declines.

**Why not `ground_truth_count × 150`**: synthetic proxy miscounts per-issue size, produces misleading ratios (e.g. 1.83× when actual overhead 1.06×). `gt_json_chars` always available to scorer at Phase 3 (it's `GROUND_TRUTH_JSON` field) — no extra agent calls.

Both fields added to each problem's entry in `scores.json`. Phase 4 aggregates: `mean_completeness_loss` and `mean_token_overhead`. Both appear in `benchmark-report.md` Aggregate section and `result.jsonl`.

**Scoring guidance for scorers**: response fails completeness if `completeness_loss_ratio > 0` for any severity≥high finding (critical violation). Response verbose if `token_overhead_ratio > 2.0`. Report both ratios regardless of pass/fail.
