---
name: debrief-coding
description: 'Read local codemap telemetry logs and produce a diagnostic/usage report. Supports date filtering, session filtering, and optional anonymization before sharing. TRIGGER when: analyse recent codemap usage, debug query patterns, investigate errors, or prepare a shareable anonymized report of how codemap skills and CLI are being used.'
allowed-tools: Read, Write, Bash, Glob
model: haiku
effort: low
---

<objective>

Read `.cache/codemap/logs/` JSONL telemetry; analyze usage; write diagnostic report. Discover legacy flat shards + recursive `claude/`, `codex/`, `direct/` trees; keep legacy records unattributed. Codex hooks supply runtime-scoped CLI/tool shards, no skill-start events; missing skill telemetry + cross-layer joins remain evidence gaps.

NOT for: validating codemap installation health/integration (use `/codemap-py:integration audit`); building/querying structural index (use `/codemap-py:scan-codebase` or `/codemap-py:query-code`).

</objective>

<workflow>

## Flags

- `--since <YYYY-MM-DD>` — filter to records on or after this date (default: all)
- `--session <id>` — filter to a single session UUID
- `--anonymize` — run `anonymize.py` on every log shard, all three layers (CLI, skill, tool), before reading; replaces qualified names with stable pseudonyms; keeps salt in `.cache/codemap/logs/.salt` (never in output). Directory input preserves runtime topology below export root, pseudonymizes shard session stems.
- `--output <path>` — write report to this path (default: `.reports/codemap/debrief-<YYYY-MM-DD>.md`)

## Step 0: Verify logs exist

```bash
find .cache/codemap/logs -type f -name '*.jsonl' -print 2>/dev/null  # timeout: 5000
```

No files → stop: "No codemap telemetry found. Run any `/codemap-py:*` skill or `codemap-py query`/`index` command to start collecting logs."

Per-session shards under `logs/claude/`, `logs/codex/`, `logs/direct/`: CLI `cli_<session>.jsonl`; skill `skills_<session>.jsonl`; tool `tools_<session>.jsonl`. Older flat shards = unattributed legacy evidence. Collect every matching shard recursively, not only root glob. Preserve topology; report overall, per-runtime, unattributed summaries. `token_measurement` unavailable: host hooks provide no token usage.

## Step 1: Optionally anonymize

If `--anonymize` flag given:

**Guard**: anonymize every present CLI, skill, tool shard by passing log directory as `--input`; recursion covers flat + runtime shards. Copies land in `.cache/codemap/export/` with same topology. anonymize.py refuses writes beside `.salt`; never target logs dir. Step 2 must not mix anonymized/original data or exempt a layer.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/anonymize.py" \
    --input .cache/codemap/logs --out-dir .cache/codemap/export  # timeout: 15000
```

If neither set contains files: print `⚠ --anonymize: no CLI or skill logs found — cannot produce anonymized report.`; stop. If only one layer exists, anonymize it; note gap.

Step 2 uses `-anon` variants under `.cache/codemap/export/`. anonymize.py absent → warn; use originals.

## Step 2: Read log files

Resolve shard tree via `Glob`; Read every returned path. Never rely on variables from prior Bash block: each tool call gets fresh shell; Read never expands shell variables. After `--anonymize`, glob mirrored `.cache/codemap/export/`, not originals:

- CLI shards: recursive `Glob(".cache/codemap/logs/**/cli*.jsonl")` (or `.cache/codemap/export/**/cli*-anon.jsonl` when anonymized)
- Skill shards: recursive `Glob(".cache/codemap/logs/**/skills*.jsonl")` (or `.cache/codemap/export/**/skills*-anon.jsonl`)
- Tool shards: recursive `Glob(".cache/codemap/logs/**/tools*.jsonl")` (or `.cache/codemap/export/**/tools*-anon.jsonl` when anonymized)

All layers anonymized: `anonymize.py --input` is layer-agnostic; Step 1 includes tool shards. In anonymize mode read tools from `.cache/codemap/export/` too. Raw `tools_*.jsonl` would leak verbatim Grep/Glob patterns and Read paths (`target`) into shareable export.

Read **every** returned path; concatenate before analysis. Single-file reads miss sessions and forge near-empty dataset.

Each line = one JSON record. Filter `--since` against `ts`; filter `--session` when given.

**`--session` guard**: session UUID may be absent from one or both log files (e.g. skills.jsonl records only skill events, not all CLI events). Filtering absent session ID returns empty set for that file — expected, not error. Report "session not found in <file>" rather than treating empty result as data loss.

CLI record fields: `ts`, `layer`, `runtime`, `v`, `session`, `cmd`, `argv`, `result` (nested: `count`, `query_complete`, `completeness_reason`, `stale`, `method`, `not_covered`, `error`; index records also carry `trigger`, `changed_count`, `incremental`, `stale_before`, and `result_currency`), `timing_ms`, `stderr` (optional), `exit_code` (optional).

Skill record fields: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `intent`, `hook_session`.

Tool fields (`layer: "tool"`, `log-tool-use.py`): `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `tool` (`Grep`|`Read`|`Glob`|`Bash`), `target` (Grep/Glob pattern/search path, Read file_path, Bash search command truncated to 200 chars). Count raw grep/read volume per runtime/session. Never infer flat legacy records as Claude from tool names.

## Step 3: Analyse

Compute from filtered records:

**Pre-filter (both layers):**

- Exclude records with `source: "bench"` (benchmark/demo load) and CLI records with empty `cmd` (pre-0.23 test pollution) from organic stats; separately report "scripted/polluted records excluded: N".
- Select the newest observed version with recent records as the primary cohort; report dates, counts, and sample limits. Keep adjacent versions as separate comparisons, older/unknown versions historical. Never pool old failures into current rates. Preserve project/runtime boundaries; separate plugin-development traffic when identifiable. No explicit diagnostic marker does not prove organic use.

**CLI layer:**

- Total invocations, terminal success/error: `exit_code: 0` = success; present nonzero or non-empty `result.error` = error; absent exit code = legacy outcome unverified. Unrecorded attempts, abrupt termination, and telemetry-write failures prevent a complete failure-rate claim.
- Aggregate `result.index.completeness_reason` (0.23+ veto slug: `stale` / `untracked` / `degraded` / `collision` / `root_mismatch` / `module_degraded`; `ok` = complete): explains false query_complete.
- Subcommand distribution: count per `cmd` value
- Timing median/p95/max `timing_ms`; p95: `sorted_ms = sorted(r["timing_ms"] for r in cli_records if r.get("timing_ms") is not None); p95 = sorted_ms[int(len(sorted_ms) * 0.95)] if sorted_ms else 0`
- Coverage: fraction of results with `"not_covered": true` or non-empty `not_covered`
- Error patterns: group `result.error` strings by prefix (first 60 chars); list top-5 by count
- Stale-index warnings: fraction of results with `"stale": true`

**Skill layer:**

- Total skill starts by `skill` name
- Session count (distinct `session` values)
- Timeline: first and last `ts` in dataset

**Cross-layer:**

- Sessions in both layers → linked chains (skill invoked → N CLI calls)
- Average CLI calls per skill session
- Aggregate overall + by `runtime` (`claude`, `codex`, `direct`); flat legacy = `unattributed`.
- Report refresh triggers, changed-file counts, index-only sessions, incomplete-query reasons, stale/degraded fractions. Missing legacy provenance = `unknown`.
- Do not present debrief as measured token savings or live fresh-session activation evidence.

**Module-overlap proxy:**

Join tool searches/reads to a complete, successful, non-stale answer in the same project/version/runtime/session/module and time window. A match is an overlap proxy, not confirmed misuse. Source-body/test/diff inspection can legitimately follow a structural query. Intent stays unknown unless actual commands prove equivalent structural repetition; count identical-command repetition separately, without inferring saved tokens or workflow time from engine durations.

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/join_avoidance.py" --logs .cache/codemap/logs --window-min 10 --json  # timeout: 15000
```

The helper scans the entire supplied tree: run on a filtered copy preserving runtime topology for each requested project/version/date/session cohort, never silently substitute all-history results. Legacy `avoidance_count`/`rate` keys mean `module_overlap_proxy_v3`; version-separated joins and batch logical-answer denominators differ from older metrics. Explicit project identity and successful terminal outcomes are required; missing legacy fields stay unjoinable, never inferred from the log destination or backfilled. Report raw CLI/tool counts, eligible logical answers, failed/unjoinable batch children, unverified outcomes, and join coverage separately. Preserve `per_runtime` and `unattributed`; absent skill starts do not prove non-use. High overlap alone proves neither a broken guard nor redundant work. If nonzero, list overlapping modules with these limits.

## Step 4: Write report

Output: `--output`; default `.reports/codemap/debrief-<YYYY-MM-DD>.md` using today.

```bash
mkdir -p .reports/codemap  # timeout: 5000
```

Create report via Write with sections:

```markdown
# Codemap Debrief — <date>

**Scope**: <date range> · <total records> records · <anonymized: yes/no>

## Overview

<2–3 sentence summary: total CLI calls, distinct sessions, top subcommand, median timing>

## Subcommand distribution

| cmd | calls | % |
|-----|-------|---|
| ... | ...   |   |

## Performance

| metric | value |
|--------|-------|
| median timing_ms | ... |
| p95 timing_ms | ... |
| max timing_ms | ... |

## Coverage gaps

<fraction with not_covered; list top modules if available>

## Error patterns

<list top-5 error prefixes with counts; "none" if clean run>

## Skill invocations

| skill | starts |
|-------|--------|
| ...   | ...    |

## Session timeline

First: <ts> · Last: <ts> · Distinct sessions: N

<If --session given: full chronological event list for that session>
```

Print report path on completion.

## Example invocations

```bash
/codemap-py:debrief-coding

/codemap-py:debrief-coding --since 2026-06-15

/codemap-py:debrief-coding --session 3f2e1a90-...

# use project-relative path, not /tmp
/codemap-py:debrief-coding --anonymize --output .reports/codemap/debrief-anon-$(date +%Y-%m-%d).md
```

## Security note

Logs stay local in `.cache/codemap/logs/`. `.cache/codemap/logs/.salt` must stay local; never share with anonymized output. Anonymized logs are shareable; pseudonyms irreversible without salt.

</workflow>
