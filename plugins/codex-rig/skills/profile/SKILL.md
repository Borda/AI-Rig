---
name: profile
description: Explain native Codex session and agent token usage, inclusive tool time, and optional supplied-rate cost estimates from selected telemetry; excludes application CPU/GPU profiling and billing statements.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md). Follow the [native execution rules](../../shared/native-skill-contract.md); this reporting skill adds no permissions or instrumentation.

# Profile

Use native `/status` for current session configuration/tokens and `/usage` for account usage when those answer the request. This skill adds a retained session/agent report over existing telemetry; it never enables instrumentation or changes Codex configuration. Native OTel already provides tool durations and token metrics; use an existing authorized export for deeper analysis rather than installing another collector. See [native telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry).

## Input Schema

```json
{
  "inputs": "optional explicit native session JSONL paths; default current session only when exactly identified",
  "rates": "optional local model-rate JSON; no prices assumed",
  "done_when": "observed counters, timing basis, coverage and unavailable estimates are explained"
}
```

## Workflow

1. Resolve the installed plugin root from this skill. Follow [helper-cli-contract.md](../../shared/helper-cli-contract.md); inspect `profile.py --help` and create a run with `create_run.py --skill profile`. Keep reports under `.reports/codex/profile/<timestamp>/`; no raw transcript copies.
2. Use explicit input paths. For the current session, resolve `CODEX_THREAD_ID` against filenames beneath the active `CODEX_HOME` sessions root (default `~/.codex/sessions`), then verify its `session_meta.id`. Read no unrelated transcript contents. If the host provides no identity, multiple matches, paginated history instead of supported JSONL, or inaccessible telemetry, report that coverage unavailable; never silently choose the newest session or query private databases. An explicit input set authorizes only those files. Add agent logs only when explicitly scoped or their exact recorded identity and user scope establish inclusion; list omitted agents and unverified lineage.
3. Run `profile.py` through the portable launcher with the selected inputs, retaining its sanitized stdout as `profile.json`. The helper reads a fixed byte prefix, verifies its hash, and reports later growth. Do not retain prompts, reasoning text, arguments, command strings, tool outputs or credentials. Unknown valid event families are ignored; malformed recognized telemetry fails. Unsupported telemetry is unavailable evidence, never zero usage. Inspect existing OTel exports only within the same authorized scope; label their separate source/basis instead of silently combining cumulative streams.
4. Explain the latest observed cumulative counters, model coverage, and completed-tool classes. Cached input and reasoning output are subsets. Native execution durations take priority over completed-item timestamp intervals; totals are inclusive, can overlap, and are not an exclusive wall-time partition or CPU time. Agent spawn/wait durations do not establish agent lifetime. Separate agent/session files remain separate rows with no inferred grand total. Do not attribute tokens or dollars to individual tools/skills, infer residual reasoning, or treat missing durations as zero. A selected prefix can omit earlier history, later activity or agents; record this in the report even when counters are present.
5. Estimate USD only when explicitly supplied rate evidence matches one observed model and complete relevant counters. Native per-request usage must explain every charged cumulative increment from zero under known model contexts; missing increments, unknown contexts, partial starting counters and legacy totals alone leave attribution unavailable. The local rate sheet has `as_of` (ISO date), `source`, and `models` mapping exact model names to finite nonnegative `input_per_million`, `cached_input_per_million`, and `output_per_million` rates. Retain its hash and explain the source/date and applicability, including service tier and cached-token treatment. Unknown cache-write pricing or mixed models make estimates unavailable. Estimates charge uncached input, cached input and output once; reasoning is already included in output. Never fetch prices automatically or represent list-rate estimates as subscription spend, an invoice or actual billing. No rates means no dollar estimate.
6. Write `profile-report.md` with `Scope`, `Observed usage`, `Inclusive timing`, `Cost basis`, and `Coverage and limits`. Link sanitized evidence, report capture time/byte boundary and missing fields, distinguish observed data from inference, and rank only supported measurements. Keep file paths outside terminal tables. If the requested breakdown is unavailable, explain the missing evidence and safe native alternative without claiming an empty successful profile.
7. Use the reader's report-check action to validate `profile.json` and derive the exact Results rows; copy those rows without changing measured cells in the standard handoff. An explicit exact-output request uses the shared `caller-contract` branch without tables; keep measurements and coverage in retained evidence and honor the requested output format. Bind the retained file's exact SHA-256 in `PROFILE_METADATA.profile` with `schema_version=1` and `profile_sha256`. Artifact admission checks report structure, arithmetic and digest for every branch, and reconciles standard handoff rows without reopening private source logs. Exact caller prose is not machine-reconciled to report rows; review its factual consistency. If telemetry is unavailable before capture, retain a schema-1 report with `aggregation="separate-sessions-no-grand-total"` and `sessions=[]`; use its derived unavailable row in a standard handoff and a failed result, never an invented session or passing zero.
8. Run the applicable shared quality gates with explicit inapplicability reasons. Review source-prefix bindings, arithmetic, omitted agents, overlapping timings and privacy. Follow the canonical result lifecycle using `result-template.json`, metadata variable `PROFILE_METADATA`, shared validator skill `profile`, and [final-handoff-contract.md](../../shared/final-handoff-contract.md). Render and validate the handoff, write and validate the candidate, then promote only a valid result. Re-entry resumes the first unmet checkpoint; a reader diagnostic alone is not a finished report.

## Fail-Fast Rules

- Missing/ambiguous current identity, unavailable telemetry or unsupported native history never authorizes broader discovery or instrumentation. Retain a failed/unavailable report with a concrete next action.
- Malformed recognized counters, inconsistent totals/subsets, duplicate session inputs, changed capture prefixes or invalid rate evidence stop the affected measurement. Counter decreases remain explicitly unavailable rather than summing ambiguous reset segments.
- Never modify source logs, Codex settings, installation state or credentials; never make a model/API call or enable an exporter to obtain profiling evidence.
- Do not claim a complete session, authenticated agent lineage, exclusive time attribution, per-skill cost or actual billing from a selected partial log.

## Quality Gates

Use [quality-gates.md](../../shared/quality-gates.md). Required `review`: source identity/prefix verification, numeric and timing basis, privacy projection, rate applicability, coverage and final artifact validation. Run reader regressions when its implementation changes; unrelated project tests, formatters and type checks may be inapplicable to a read-only profiling run with reasons. Missing requested data remains visible in a failed or explicitly partial outcome, not a passing zero.

## Calibration Hooks

Cover native-overlap routing, repeated cumulative counters across turns, absent counters, resets, mixed models, cached/reasoning subsets, overlapping tools, unobserved agents, private transcript content, append-safe captures and unsupported history. Synthetic fixture observations are parser/calibration evidence, never live model-quality or billing evidence.

## Output Contract

Standard final chat follows the shared ordered frame with `Next steps`; Results columns are exactly `Session / agent | Observed tokens | Inclusive tool time | Cost estimate | Coverage limit`. Use the reader-derived row for every selected session; explain every material missing requested breakdown in the report and remaining actions. Keep paths below the table. `Outcome` distinguishes complete, partial and unavailable evidence. Explicit exact-output requests follow the shared `caller-contract` branch instead. Metadata retains source prefix bindings, the report digest and confidence gaps; cost estimates remain explicitly hypothetical. After canonical validators and promotion pass, emit `final.md` verbatim.
