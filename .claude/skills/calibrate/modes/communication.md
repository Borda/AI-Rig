<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: communication

> **Codex integration: disabled.** Problem generation and scoring are Claude-only for this mode. Ground truth requires deep knowledge of `file-handoff-protocol.md`, `TEAM_PROTOCOL.md`, and AgentSpeak v2 — Codex lacks this context and would produce superficial or incorrect problems.

Handover + team protocol compliance. Included in `all`. Use the explicit `communication` target to run this mode in isolation.

Target agent: `self-mentor`.

### Domain

Four subdomains — each ground truth issue must be tagged with its `subdomain` field so Phase 4 can compute per-subdomain recall:

```
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
- computed as: issues found in that subdomain / total issues in that subdomain (omit if 0 issues for a subdomain in this run)
- surfaced in `report.md` Aggregate section and `result.jsonl`; this is the primary signal for context pollution detection specifically

### Step 2: Spawn communication pipeline subagent

**N override** (communication problems are high-complexity — tighter N prevents context window overflow in the pipeline subagent): fast=3, full=5. Do NOT use the global FULL_N=10 for this mode.

Mark "Calibrate communication" in_progress. Use the standard pipeline template from `.claude/skills/calibrate/templates/pipeline-prompt.md` with `<TARGET>=self-mentor` and `<DOMAIN>` set to the domain string above. Required substitutions: `<TARGET>`, `<DOMAIN>`, `<N>`, `<TIMESTAMP>`, `<MODE>`, `<AB_MODE>`. Spawn a **single** `general-purpose` pipeline subagent — it runs self-mentor against synthetic agent responses, full/compact response pairs, and team transcripts with injected violations.

**Phase 2 batching**: instruct the pipeline to spawn Phase 2 target agents in **batches of 3** (not all at once), collecting acknowledgments between batches. Each self-mentor response is ~1–4KB of prompt + response context; batching prevents accumulation of all N problem inputs in the pipeline's context simultaneously. Add to the pipeline prompt: "Spawn Phase 2 agents in batches of 3 — await all acknowledgments in a batch before spawning the next. Maximum batches: ceil(N/3) — for fast (N=3) that is 1 batch; for full (N=5) that is 2 batches."

Run dir: `.reports/calibrate/<TIMESTAMP>/self-mentor/` (relative to project root)

### Active instruction — token optimization (additional scoring measure)

Append the following to every `task_prompt` in Phase 1 for `communication` problems:

> "Produce the most compact output that preserves all decision-relevant information. Omit prose explanations where a field name and value are self-evident. Any finding at severity≥high must appear; lower-severity findings may be summarized rather than detailed. Target: ≤30% of the raw response token count without losing critical signal."

The scorer (Phase 3) must evaluate two additional dimensions independently:

1. **Completeness loss** — essential fields omitted vs. total essential fields → `completeness_loss_ratio`
2. **Token overhead** — how much larger the response is compared to the minimum faithful representation → `token_overhead_ratio`

**`token_overhead_ratio` baseline — ground truth JSON char count**: compute `len(JSON.stringify(ground_truth))` (the character count of the serialised `GROUND_TRUTH_JSON` the scorer already holds). This is the minimum lossless representation of all required findings. The ratio `response_chars / gt_json_chars` measures overhead above that floor.

- ≤1.5 ✓ compact — response fits within 1.5× the bare findings (allows for confidence block, location formatting, severity labels)
- 1.5–2.0 ~ moderate — some prose wrapping, acceptable
- > 2.0 ⚠ verbose — significant narrative overhead above minimum content

For scope problems (ground_truth = []) use `response_chars / 50` as the baseline (50 chars ≈ a one-line decline/redirect). Set `completeness_loss_ratio = 0.0` if the response correctly declines.

**Why not `ground_truth_count × 150`**: that synthetic proxy miscounts per-issue size and produces misleading ratios (e.g. 1.83× when the actual overhead is 1.06×). The gt_json_chars baseline is always available to the scorer at Phase 3 (it is the `GROUND_TRUTH_JSON` field) — no extra agent calls needed.

Both fields are added to each problem's entry in `scores.json`. Phase 4 aggregates them: `mean_completeness_loss` and `mean_token_overhead`. Both appear in `report.md` Aggregate section and `result.jsonl`.

**Scoring guidance for scorers**: a response fails on completeness if `completeness_loss_ratio > 0` for any severity≥high finding (critical violation). A response is verbose if `token_overhead_ratio > 2.0`. Report both ratios regardless of pass/fail.
