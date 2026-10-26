---
name: debrief-coding
description: 'Read local codemap telemetry logs and produce a diagnostic/usage report. Supports date filtering, session filtering, and optional anonymization before sharing. TRIGGER when: analyse recent codemap usage, debug query patterns, investigate errors, or prepare a shareable anonymized report of how codemap skills and CLI are being used.'
argument-hint: '[--since YYYY-MM-DD] [--session ID] [--anonymize] [--output PATH]'
allowed-tools: Read, Write, Bash, Glob
effort: low
---

<objective>

Aggregate codemap JSONL telemetry (log root `.cache/codemap/logs/` at the git top-level, or `CODEMAP_LOG_DIR`) via `debrief_stats.py`; write diagnostic report. Discover legacy flat shards + recursive `claude/`, `codex/`, `direct/` trees; keep legacy records unattributed. Codex hooks supply runtime-scoped CLI/tool shards, no skill-start events; missing skill telemetry + cross-layer joins remain evidence gaps.

NOT for: validating codemap installation health/integration (use `/codemap-py:integration audit`); building/querying structural index (use `/codemap-py:scan-codebase` or `/codemap-py:query-code`).

</objective>

<workflow>

## Flags

- `--since <YYYY-MM-DD>` — filter to records on or after this date (default: all)
- `--session <id>` — filter to a single session UUID
- `--anonymize` — run `anonymize.py` on the filtered copy of every log shard, all three layers (CLI, skill, tool), before aggregation; replaces qualified names with stable pseudonyms; keeps salt in `<log root>/.salt` (never in output). Output lands in a fresh run-scoped `.cache/codemap/export/<UTC stamp>/` preserving runtime topology, shard session stems pseudonymized.
- `--output <path>` — write report to this path (default: `.reports/codemap/debrief-<YYYY-MM-DD>.md` at the project root, `-2`, `-3`, … appended when taken — never overwrite)

## Step 1: Compute statistics

One helper does all reading, filtering, anonymization and aggregation. Never Read shards yourself: a busy tree holds hundreds of shards (millions of tokens); Read truncates large shards, so a hand-built report rests on a partial dataset with estimated numbers.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/debrief_stats.py" --arguments="$ARGUMENTS"  # timeout: 120000
```

- Exit `1` → no shards under the resolved log root: relay its message ("No codemap telemetry found …"); stop.
- Exit `2` → bad flag (`Unknown flag(s)`, missing value, `--since` not `YYYY-MM-DD`): relay its `!` line; stop.
- Exit `3` → `anonymize.py` or `join_avoidance.py` hung past its time cap; the scratch copy is already removed: relay its `!` line; stop.
- Exit `0` → one JSON object on stdout; Steps 2–3 use only it.

What the helper does, in order (so the JSON reads right):

- **Log root**: resolved once with the writer's resolver — git top-level, `CODEMAP_LOG_DIR` honoured (`log_root`). A run from a subdirectory reads the same tree the hooks wrote.
- **Shards**: legacy flat shards + recursive `claude/`, `codex/`, `direct/` trees; CLI `cli_<session>.jsonl`, skill `skills_<session>.jsonl`, tool `tools_<session>.jsonl`. Flat shards = unattributed legacy evidence (`unattributed` runtime). `token_measurement` unavailable: host hooks provide no token usage.
- **Filters before anything else**: `--since` against `ts` (unparsable `ts` dropped, counted in `ts_unparsable_excluded`), `--session` against raw `session`, into a private scratch copy preserving topology. Malformed lines counted in `malformed_lines`, never silently included.
- **`--session` + `--anonymize`**: session filter runs on raw shards *before* anonymizing — anonymize.py pseudonymizes `session`/`hook_session` to `sym_...` values a raw UUID cannot match. The scratch copy is deleted after the run; never written to export or beside `.salt`.
- **Anonymized mode** (`dataset: "anonymized"`) is active iff anonymize.py exited 0 **and** wrote at least one `-anon.jsonl` into this run's export dir — never just whether `--anonymize` was passed, never an older export (earlier exports stay out of this report). Otherwise `dataset: "raw"`, `anonymize.ok: false`, `anonymize.message` says why: warn in the report. All layers are read from the export when active — CLI `cli*-anon.jsonl`, skill `skills*-anon.jsonl`, tool `tools*-anon.jsonl`; raw `tools_*.jsonl` would leak verbatim Grep/Glob patterns and Read paths (`target`) into a shareable report. In anonymized mode `filters.session` is redacted; never copy `log_root` or a raw session id into the report.
- **`--session` guard**: `session_not_found_in` lists layers with no record for that session — expected (e.g. skills logs only skill events), not data loss. Report "session not found in <layer>".
- **Module-overlap proxy**: `join_avoidance.py` runs on the same filtered (or anonymized) dataset → `module_overlap`; never all-history results for a filtered report.

## Step 2: Interpret

**Pre-filter (all layers)** — already applied; report the counts:

- `excluded_scripted`: `source: "bench"` records (benchmark/demo load) and CLI records with empty `cmd` (pre-0.23 test pollution) — report "scripted/polluted records excluded: N".
- `primary_cohort` = newest observed version; report its dates (`first_ts`/`last_ts`), counts, and sample limits. `cohorts` holds every version separately: keep adjacent versions as separate comparisons, older versions historical. Never pool old failures into current rates. Preserve project/runtime boundaries; separate plugin-development traffic when identifiable. No explicit diagnostic marker does not prove organic use.
- `unversioned_excluded`: records without `v` belong to no cohort, excluded from every recent cut — report the count separately. A filter written as `if v and older(v)` lets them through — the plugin is installed once per machine, so a project whose newest record is old was simply not used recently; version lag is inactivity, not a per-project upgrade gap.

**CLI layer** (`cohorts.<v>.cli`):

- `outcomes`: `success` (`exit_code: 0`), `error` (nonzero exit code or non-empty `result.error`), `unverified` (absent exit code = legacy outcome unverified). Unrecorded attempts, abrupt termination, and telemetry-write failures prevent a complete failure-rate claim.
- `completeness_reasons`: `result.index.completeness_reason` (0.23+ veto slug: `stale` / `untracked` / `degraded` / `collision` / `root_mismatch` / `module_degraded`; `ok` = complete or absent by design on a compact complete answer): explains false query_complete.
- `subcommands`: count per `cmd`.
- `timing_ms.query` / `timing_ms.index`: median/p95/max, split on purpose; p95 = `sorted_ms[int(len(sorted_ms) * 0.95)]`. Background refreshes are a third of CLI records on active repos and occupy the whole latency tail; a pooled p95 describes refresh cost, not query cost.
- `not_covered_lists`: `result.index.not_covered` is a fixed list of analysis limits per method (`importlib.import_module`, `__import__`, `lazy-loading` for the import graph; `dynamic-dispatch`, `hook-callbacks`, `string-dispatch` for the call graph, plus `inherited-constructors` when constructor callers were merged; `dynamic-dispatch`, `runtime-injection` for `static-ast` symbol answers). Present on nearly every complete query and **not** a per-query coverage gap — report the distinct slug lists and how many queries carry each, never "fraction with non-empty `not_covered`".
- `top_errors`: `result.error` grouped by first 60 chars, top 5.
- `stale`: queries with `result.index.stale: true` over queries carrying an index block.
- `index_refresh`: `result.trigger` and `result.changed_count` distributions of `cmd: "index"` records (`changed_count` `null` when the caller did not supply it — raw CLI, and every hook-triggered refresh before 0.39.5).
- `by_runtime`: per `claude`, `codex`, `direct`, `unattributed`.

**Skill layer and cross-layer** (`cohorts.<v>`): `skill_starts` by skill, `sessions`, `first_ts`/`last_ts`, `linked_skill_sessions` + `avg_cli_calls_per_linked_session` (skill invoked → N CLI calls), `index_only_sessions`, `tool_records` (raw grep/read volume per runtime and tool). Zero skill starts beside hundreds of CLI records is the expected shape, not a broken hook: the prompt preamble steers agents to `codemap-py query` directly, and Codex has no skill-start hook at all. State it as "direct-CLI usage; per-skill joins have no denominator" and move on — do not open a telemetry investigation on that signal alone. Missing legacy provenance = `unknown`. Do not present debrief as measured token savings or live fresh-session activation evidence.

**Module-overlap proxy** (`module_overlap`): joins tool searches/reads to a complete, successful, non-stale answer in the same project/version/runtime/session/module and a 10-minute window. A match is an overlap proxy, not confirmed misuse. Source-body/test/diff inspection can legitimately follow a structural query. Intent stays unknown unless actual commands prove equivalent structural repetition; count identical-command repetition separately, without inferring saved tokens or workflow time from engine durations.

Legacy `avoidance_count`/`rate` keys mean `module_overlap_proxy_v3`; version-separated joins and batch logical-answer denominators differ from older metrics. Explicit project identity and successful terminal outcomes are required; missing legacy fields stay unjoinable, never inferred from the log destination or backfilled. Report raw CLI/tool counts, eligible logical answers, failed/unjoinable batch children, unverified outcomes, and join coverage separately — anonymized runs additionally show every legacy (pre-`result.module`) record as unjoinable, because `argv` is fully scrubbed and the command-name fallback can no longer match a `sym_...` pseudonym; expected under anonymization, not a bug, and already counted in `excluded_or_unjoinable_cli_records`. Preserve `per_runtime` and `unattributed`; absent skill starts do not prove non-use. High overlap alone proves neither a broken guard nor redundant work. If nonzero, list overlapping modules with these limits — in anonymized mode these render as `sym_<digest>` pseudonyms, which is expected, not data loss.

Each event carries `kind`: `source_read`, `structural_search`, or `unknown`. Grep/Glob and Bash classification uses producer-observed `search_scope`: `file` is `source_read` whichever file it names, `directory` is `structural_search`; Grep/Glob without either stays `unknown`, never inferred from a pattern or the analysis host filesystem. Report `unknown_count` beside `structural_search_count` (overall and per runtime): a legacy cohort and a classified cohort both show zero structural searches, only `unknown_count` separates them. Report `structural_search_count` beside total overlaps, not as confirmed misuse. Bash records without a file/directory scope (legacy, pipelines, ambiguous commands) fall back to the target, cut at 200 characters so later flags and paths are hidden; recursive-looking ones outside own-file inspection stay `unknown`, not `structural_search`.

**Record fields (reference, for `session_events` and error text)**:

CLI record fields: `ts`, `layer`, `runtime`, `v`, `session`, `project` (0.39.4+), `cmd`, `argv`, `result`, `timing_ms`, `stderr` (optional), `exit_code` (optional). Query results nest their honesty block under `result.index`: `query_complete`, `stale`, `root_mismatch`, `compact`, `method`, `not_covered`, `index_path`; `completeness_reason` and `degraded` appear only in full-coverage mode or when `query_complete` is false — a compact complete answer omits them by design, so their absence is not a gap. Top-level `result` carries the command payload (`count`, `imported_by`, `called_by`, …) and `error`. Index records (`cmd: "index"`) nest `trigger`, `changed_count`, `incremental`, `stale_before`, `result_currency` under `result` beside `modules_indexed`/`degraded` — never at top level.

Skill record fields: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `intent`, `hook_session`.

Tool fields (`layer: "tool"`, `log-tool-use.py`): `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `tool` (`Grep`|`Read`|`Glob`|`Bash`), `target` (Grep/Glob pattern, or path when no pattern; Read file_path; Bash search command truncated to 200 chars), `search_path` (Grep/Glob path, omitted input defaulting to the hook's current directory; Bash: the parsed grep/rg operands, `os.pathsep`-joined, or the event cwd for operand-less `rg`), `search_scope` (producer-observed file/directory/unknown; Bash is `unknown` for pipelines, command lists, shell expansions and unknown options). Never infer flat legacy records as Claude from tool names.

## Step 3: Write report

Output: the JSON's `report_path` (`--output` verbatim, else the first free `.reports/codemap/debrief-<YYYY-MM-DD>[-N].md`; parent dir already created). Create it via Write:

```markdown
---
Title: Codemap Debrief — <date>
Date: <date>
Scope: <since/session filters or "all"> · <dataset: raw | anonymized>
Focus: codemap telemetry usage and diagnostics
Agents: none
Outcome: <primary cohort> · <CLI invocations> CLI calls · <error count> errors
Confidence: <shards read, malformed_lines, unversioned/scripted excluded>
Next steps: <top follow-up or "none">
Path: <report_path>
---

# Codemap Debrief — <date>

**Scope**: <date range> · <total records> records · <anonymized: yes/no>

## Overview

<2–3 sentence summary: total CLI calls, distinct sessions, top subcommand, median timing>

## Subcommand distribution

| cmd | calls | % |
|-----|-------|---|
| ... | ...   |   |

## Performance

| metric | queries | index refreshes |
|--------|---------|-----------------|
| median timing_ms | ... | ... |
| p95 timing_ms | ... | ... |
| max timing_ms | ... | ... |

## Static blind spots

<distinct `result.index.not_covered` slug lists with the number of queries carrying each; incomplete queries by `completeness_reason`>

## Error patterns

<list top-5 error prefixes with counts; "none" if clean run>

## Skill invocations

| skill | starts |
|-------|--------|
| ...   | ...    |

## Session timeline

First: <ts> · Last: <ts> · Distinct sessions: N

<If --session given: `session_events` in order; note `truncated` when true>
```

Print report path on completion.

## Example invocations

```bash
/codemap-py:debrief-coding

/codemap-py:debrief-coding --since 2026-06-15

/codemap-py:debrief-coding --session 3f2e1a90-...

# use project-relative path, not /tmp; slash-command args are not shell-expanded, so write the date literally
/codemap-py:debrief-coding --anonymize --output .reports/codemap/debrief-anon-2026-06-15.md
```

## Security note

Logs stay local in the log root (default `.cache/codemap/logs/`). `<log root>/.salt` must stay local; never share with anonymized output. Anonymized logs are shareable; pseudonyms irreversible without salt.

Anonymization scrubs a documented, bounded field set: `session`/`hook_session` (stable pseudonyms); qualified names embedded in `error`/`stderr` free text; `intent`, `target`, `search_path`, `project`, and the identity-bearing `result` fields `module`/`qname`/`qualified_name`/`index_path`/`path`/`caller`/`callee` (every identifier token, not only dotted ones); each element of `imported_by`/`changed_modules`; and every `argv` token. This is a field denylist, not a schema-complete guarantee — anonymize.py's own docstring notes a future query-result shape introducing an identifying field name not on this list passes through unscrubbed until added. Never describe anonymized output as fully de-identified or a complete privacy guarantee — describe it as scrubbed for the fields above.

</workflow>
