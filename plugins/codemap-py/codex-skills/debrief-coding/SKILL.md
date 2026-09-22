---
name: debrief-coding
description: 'Telemetry report: `$codemap-py:debrief-coding [flags]`; skip integration/index/query.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Debrief Coding

Read `.cache/codemap/logs/` JSONL, analyse use, write diagnostic report. Include recursive `claude/`, `codex/`, `direct/` shards; keep legacy flat records unattributed. Codex records runtime-scoped CLI/tool shards, no skill starts; missing skill telemetry + cross-layer joins are evidence gaps.

NOT for: installation/integration health (use `$codemap-py:integration audit`); index build or structural query (use `$codemap-py:scan-codebase` or `$codemap-py:query-code`).

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve installed root once, substitute for `PLUGIN_ROOT`, retain in reasoning; shell state doesn't persist. Telemetry otherwise matches Claude: local JSONL under `.cache/codemap/logs/`.

## Flags

- `--since <YYYY-MM-DD>`: records on/after date; default all.
- `--session <id>`: one session UUID.
- `--anonymize`: run `anonymize.py`; pseudonymize qualified names and shard stems, preserve topology, store salt only at `.cache/codemap/logs/.salt`.
- `--output <path>`: report path; default `.reports/codemap/debrief-<YYYY-MM-DD>.md`.

## Workflow

### 1. Verify logs

```bash
find .cache/codemap/logs -type f -name '*.jsonl' -print 2>/dev/null
```

No files: stop: "No codemap telemetry found. Run any `$codemap-py:*` skill or `codemap-py query`/`index` command to start collecting logs." Collect every matching shard recursively; `cli_<session>.jsonl`, `skills_<session>.jsonl`, `tools_<session>.jsonl` live below each runtime directory. Flat shards remain unattributed; never infer Claude from tool names. `token_measurement` unavailable — host hooks expose no token usage.

### 2. Anonymize when requested

```bash
python PLUGIN_ROOT/bin/anonymize.py --input .cache/codemap/logs --out-dir .cache/codemap/export
```

Use only anonymized copies after this step; never mix with raw data. Separated from `.salt`; never target log directory. With no shard, stop: "no CLI or skill logs found — cannot produce an anonymized report." With one layer, anonymize it, report the gap.

### 3. Read and filter records

Read every CLI, skill, tool shard recursively; one file is incomplete. Each line is JSON. Filter `ts` by `--since`, `session` by `--session`; empty filter in one layer is expected — UUID can be absent there.

- CLI: `ts`, `layer`, `runtime`, `v`, `session`, `project` (0.39.4+), `cmd`, `argv`, `result`, `timing_ms`, optional `stderr`/`exit_code`. Query honesty block nests under `result.index` (`query_complete`, `stale`, `root_mismatch`, `compact`, `method`, `not_covered`, `index_path`); `completeness_reason`/`degraded` appear only in full mode or when incomplete — absent on a compact complete answer by design. Top-level `result` holds the payload and `error`. Index records: `trigger`, `changed_count`, `incremental`, `stale_before`, `result_currency`; `changed_count` is `null` when the caller supplied none (raw CLI; every hook refresh before 0.39.5).
- Skill: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `intent`, `hook_session`.
- Tool: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `tool`, `target` (Grep/Glob pattern, Read path, Bash command cut at 200 chars), `search_path` (Grep/Glob path; omitted input defaults to the hook's current directory), `search_scope` (producer-observed file/directory/unknown).

### 4. Analyse

Exclude `source: "bench"` and CLI records with empty `cmd` from organic stats; report count as "scripted/polluted records excluded: N". Select the newest observed version with recent records as the primary cohort; show dates, counts, and sample limits. Keep adjacent versions as separate comparisons and older/unknown versions historical; never pool them into current failure rates. Preserve project and runtime boundaries, separating plugin-development traffic when identifiable. No explicit diagnostic marker does not prove organic use. Records without `v` join no version cohort: exclude from every recent cut by default, report their count separately (`if v and older(v)` lets them through). The plugin installs once per machine — a project whose newest record is old was not used recently; version lag is inactivity, not a per-project upgrade gap.

- CLI: invocations; terminal success/error (`exit_code: 0` success; non-zero or `result.error` error; absent exit code means legacy outcome unverified); completeness reasons; subcommands; median/p95/max `timing_ms` split by `cmd == "index"` vs query commands (refreshes own the latency tail; pooled p95 measures refresh cost); static blind spots — `result.index.not_covered` is a fixed per-method limit list (import graph, call graph, `static-ast` symbol answers), present on nearly every complete query, never a coverage-gap fraction: report distinct slug lists with counts; top five error prefixes; `result.index.stale` fraction. Unrecorded attempts, abrupt termination, and telemetry-write failures prevent a complete failure-rate claim.
- Skill: starts by name, sessions, first/last timestamp. Zero starts beside many CLI records is the expected shape (preamble steers to direct `codemap-py query`; Codex has no skill-start hook): state "direct-CLI usage; per-skill joins have no denominator", no telemetry investigation on that alone.
- Cross-layer: linked skill→N CLI chains, average calls per skill session, refresh triggers, changed-count distribution, index-only sessions, incomplete/degraded fractions; legacy provenance unknown.

Join tool searches/reads to a complete, successful, non-stale answer for the same project/version/runtime/session/module within the window. A match is a module-overlap proxy, not confirmed misuse. Source-body/test/diff inspection can be legitimate; intent stays unknown unless the actual commands prove an equivalent structural repetition. Count identical command repetitions separately, never infer saved tokens or workflow time from engine durations.

```bash
python PLUGIN_ROOT/bin/join_avoidance.py --logs .cache/codemap/logs --window-min 10 --json
```

The helper scans its entire supplied log tree: run on a filtered copy preserving runtime topology for each requested project/version/date/session cohort, never silently substitute all-history results. Legacy `avoidance_count`/`rate` keys mean `module_overlap_proxy_v3`; version-separated joins and batch logical-answer denominators differ from older metrics. Explicit project identity and successful terminal outcomes are required; missing legacy fields stay unjoinable, never inferred from the log destination or backfilled. Report raw CLI/tool counts, eligible logical answers, failed/unjoinable batch children, unverified outcomes, and join coverage separately; absent skill starts do not prove non-use. Preserve `per_runtime` and `unattributed`. High overlap alone proves neither a broken guard nor redundant work. Never claim measured token savings or live fresh-session activation. Each event carries `kind`: `source_read`, `structural_search`, or `unknown`. Grep/Glob classification uses producer-observed `search_scope`: `file` is `source_read` whichever file it names, `directory` is `structural_search`, missing legacy scope stays `unknown`, never inferred from a pattern or the analysis host filesystem. Report `structural_search_count` and `unknown_count` beside total overlaps (overall and per runtime), not as confirmed misuse; only `unknown_count` separates a legacy cohort from a classified one with zero structural searches. Bash targets cut at 200 characters hide later flags and paths; recursive-looking Bash searches outside own-file inspection stay `unknown`, not `structural_search`.

### 5. Write report

Default output is `.reports/codemap/debrief-<YYYY-MM-DD>.md`; use `--output` if supplied.

```bash
mkdir -p .reports/codemap
```

Include Overview (2–3 sentences), subcommand distribution, performance (median/p95/max, queries and index refreshes as separate columns), static blind spots (`not_covered` slug lists with counts; incomplete queries by `completeness_reason`), top-five error patterns (`none` when clean), skill invocations, and session timeline (first/last, sessions; full chronology for `--session`). Print the path.

## Security

Logs and salt are local. Never share `.cache/codemap/logs/.salt`; anonymized files are shareable without it.
