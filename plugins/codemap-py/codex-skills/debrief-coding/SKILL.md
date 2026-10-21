---
name: debrief-coding
description: 'Telemetry report: `$codemap-py:debrief-coding [flags]`; skip integration/index/query.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Debrief Coding

Aggregate codemap JSONL telemetry (log root `.cache/codemap/logs/` at the git top-level, or `CODEMAP_LOG_DIR`) via `debrief_stats.py`, write diagnostic report. Include recursive `claude/`, `codex/`, `direct/` shards; keep legacy flat records unattributed. Codex records runtime-scoped CLI/tool shards, no skill starts; missing skill telemetry + cross-layer joins are evidence gaps.

NOT for: installation/integration health (use `$codemap-py:integration audit`); index build or structural query (use `$codemap-py:scan-codebase` or `$codemap-py:query-code`).

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve installed root once, substitute for `PLUGIN_ROOT`, retain in reasoning; shell state doesn't persist. Invoke Python helpers through the quoted packaged launcher; on native Windows use `bin/python.cmd` with host-shell quoting. Telemetry otherwise matches Claude: local JSONL under the log root.

## Flags

- `--since <YYYY-MM-DD>`: records on/after date; default all.
- `--session <id>`: one session UUID.
- `--anonymize`: anonymize the filtered copy; pseudonymize qualified names and shard stems, preserve topology, write a fresh run-scoped `.cache/codemap/export/<UTC stamp>/`, store salt only at `<log root>/.salt`.
- `--output <path>`: report path; default `.reports/codemap/debrief-<YYYY-MM-DD>.md` at the project root, `-2`, `-3`, … appended when taken — never overwrite.

## Workflow

### 1. Compute statistics

```bash
"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/debrief_stats.py" [--since <YYYY-MM-DD>] [--session <id>] [--anonymize] [--output <path>]
```

Pass only the flags the user gave. Never read shards yourself: a busy tree holds hundreds of shards; file reads truncate and overflow context, so hand-built numbers rest on a partial dataset. Exit `1`: no telemetry — relay "No codemap telemetry found …" and stop. Exit `2`: bad flag — relay its `!` line and stop. Exit `3`: `anonymize.py` or `join_avoidance.py` hung past its time cap, scratch copy already removed — relay its `!` line and stop. Exit `0`: one JSON object; later steps use only it.

The helper, in order: resolves the log root once (git top-level, `CODEMAP_LOG_DIR` honoured); copies every `cli_*`, `skills_*`, `tools_*` shard (flat legacy + recursive `claude/`, `codex/`, `direct/`) into a private scratch tree keeping only lines matching `--since`/`--session`, counting malformed lines; with `--anonymize`, runs `"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/anonymize.py"` on that filtered copy (session filter before pseudonymization — a raw UUID never matches `sym_...`); aggregates the selected dataset; runs `"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/join_avoidance.py"` on the same dataset for `module_overlap`. Anonymized mode (`dataset: "anonymized"`) only when anonymize.py exited 0 and wrote `-anon.jsonl` files into this run's export dir; otherwise `dataset: "raw"` with `anonymize.message` — warn. Earlier exports never enter the report. Flat shards stay `unattributed`; never infer Claude from tool names. `token_measurement` unavailable — host hooks expose no token usage. `session_not_found_in` lists layers without that session — expected, not data loss.

### 2. Interpret

`excluded_scripted` = `source: "bench"` + CLI records with empty `cmd`; report as "scripted/polluted records excluded: N". `primary_cohort` = newest observed version; `cohorts` keeps every version separate — show dates, counts, sample limits; adjacent versions are comparisons, older ones historical; never pool them into current failure rates. Preserve project and runtime boundaries, separating plugin-development traffic when identifiable. No explicit diagnostic marker does not prove organic use. `unversioned_excluded` = records without `v`, in no cohort, excluded from every recent cut (`if v and older(v)` lets them through). The plugin installs once per machine — a project whose newest record is old was not used recently; version lag is inactivity, not a per-project upgrade gap.

- CLI (`cohorts.<v>.cli`): `outcomes` (`exit_code: 0` success; non-zero or `result.error` error; absent exit code = legacy outcome unverified); `completeness_reasons`; `subcommands`; `timing_ms.query` vs `timing_ms.index` median/p95/max (refreshes own the latency tail; pooled p95 measures refresh cost); `not_covered_lists` — `result.index.not_covered` is a fixed per-method limit list (import graph, call graph, `static-ast` symbol answers), present on nearly every complete query, never a coverage-gap fraction: report distinct slug lists with counts; `top_errors` (first 60 chars, top five); `stale` fraction; `index_refresh` triggers and `changed_count` (`null` when the caller supplied none: raw CLI, every hook refresh before 0.39.5); `by_runtime`. Unrecorded attempts, abrupt termination, and telemetry-write failures prevent a complete failure-rate claim.
- Skill + cross-layer (`cohorts.<v>`): `skill_starts`, `sessions`, `first_ts`/`last_ts`, linked skill→N CLI chains, average calls per linked session, index-only sessions, `tool_records` per runtime. Zero starts beside many CLI records is the expected shape (preamble steers to direct `codemap-py query`; Codex has no skill-start hook): state "direct-CLI usage; per-skill joins have no denominator", no telemetry investigation on that alone. Legacy provenance unknown.

`module_overlap` joins tool searches/reads to a complete, successful, non-stale answer for the same project/version/runtime/session/module within 10 minutes. A match is a module-overlap proxy, not confirmed misuse. Source-body/test/diff inspection can be legitimate; intent stays unknown unless the actual commands prove an equivalent structural repetition. Count identical command repetitions separately, never infer saved tokens or workflow time from engine durations. Legacy `avoidance_count`/`rate` keys mean `module_overlap_proxy_v3`; version-separated joins and batch logical-answer denominators differ from older metrics. Explicit project identity and successful terminal outcomes are required; missing legacy fields stay unjoinable, never inferred from the log destination or backfilled. Report raw CLI/tool counts, eligible logical answers, failed/unjoinable batch children, unverified outcomes, and join coverage separately; absent skill starts do not prove non-use. Preserve `per_runtime` and `unattributed`. High overlap alone proves neither a broken guard nor redundant work. Never claim measured token savings or live fresh-session activation. Each event carries `kind`: `source_read`, `structural_search`, or `unknown`. Grep/Glob and Bash classification uses producer-observed `search_scope`: `file` is `source_read` whichever file it names, `directory` is `structural_search`; Grep/Glob without either stays `unknown`, never inferred from a pattern or the analysis host filesystem. Report `structural_search_count` and `unknown_count` beside total overlaps (overall and per runtime), not as confirmed misuse; only `unknown_count` separates a legacy cohort from a classified one with zero structural searches. Unscoped Bash records (legacy, pipelines, ambiguous) fall back to the target cut at 200 characters, hiding later flags and paths; recursive-looking ones outside own-file inspection stay `unknown`, not `structural_search`.

Record fields, for `session_events` and error text — CLI: `ts`, `layer`, `runtime`, `v`, `session`, `project` (0.39.4+), `cmd`, `argv`, `result`, `timing_ms`, optional `stderr`/`exit_code`; query honesty block under `result.index` (`completeness_reason`/`degraded` absent on a compact complete answer by design); index records nest `trigger`, `changed_count`, `incremental`, `stale_before`, `result_currency` under `result`. Skill: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `intent`, `hook_session`. Tool: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `tool`, `target` (Grep/Glob pattern, Read path, Bash command cut at 200 chars), `search_path`, `search_scope` (producer-observed file/directory/unknown).

### 3. Write report

Write to the JSON's `report_path` (parent directory exists). Start with a YAML `---` block (`Title`, `Date`, `Scope`, `Focus`, `Agents`, `Outcome`, `Confidence`, `Next steps`, `Path`). Include Overview (2–3 sentences), subcommand distribution, performance (median/p95/max, queries and index refreshes as separate columns), static blind spots (`not_covered` slug lists with counts; incomplete queries by `completeness_reason`), top-five error patterns (`none` when clean), skill invocations, and session timeline (first/last, sessions; `session_events` in order for `--session`). In anonymized mode never copy `log_root` or a raw session id. Print the path.

## Security

Logs and salt are local. Never share `.cache/codemap/logs/.salt`; anonymized files are shareable without it.
