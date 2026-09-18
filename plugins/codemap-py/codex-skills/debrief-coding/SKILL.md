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

- CLI: `ts`, `layer`, `runtime`, `v`, `session`, `cmd`, `argv`, `result` (`count`, `query_complete`, `completeness_reason`, `stale`, `method`, `not_covered`, `error`; index also `trigger`, `changed_count`, `incremental`, `stale_before`, `result_currency`), `timing_ms`, optional `stderr`/`exit_code`.
- Skill: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `intent`, `hook_session`.
- Tool: `ts`, `layer`, `runtime`, `v`, `session`, `skill`, `event`, `tool`, `target`.

### 4. Analyse

Exclude `source: "bench"` and CLI records with empty `cmd` from organic stats; report count as "scripted/polluted records excluded: N". When present, group headline statistics by distinct `v`, overall, runtime, unattributed legacy records.

- CLI: invocations; success/error (`exit_code: 0` success; non-zero error; absent success unless `result.error`); completeness reasons; subcommands; median/p95/max `timing_ms`; non-empty `not_covered` fraction; top five error prefixes; stale fraction.
- Skill: starts by name, sessions, first/last timestamp.
- Cross-layer: linked skill→N CLI chains, average calls per skill session, refresh triggers, changed-count distribution, index-only sessions, incomplete/degraded fractions; legacy provenance unknown.

Join tool searches/reads to a complete (`query_complete: true`) CLI answer for the same module within the window: a match is an avoidance event, not an incorrect answer.

```bash
python PLUGIN_ROOT/bin/join_avoidance.py --logs .cache/codemap/logs --window-min 10 --json
```

High avoidance means guard/context/model dead-chain risk. Preserve `per_runtime` and `unattributed`; report count/rate in Overview and flagged modules when non-zero. Never claim measured token savings or live fresh-session activation.

### 5. Write report

Default output is `.reports/codemap/debrief-<YYYY-MM-DD>.md`; use `--output` if supplied.

```bash
mkdir -p .reports/codemap
```

Include Overview (2–3 sentences), subcommand distribution, performance (median/p95/max), coverage gaps, top-five error patterns (`none` when clean), skill invocations, and session timeline (first/last, sessions; full chronology for `--session`). Print the path.

## Security

Logs and salt are local. Never share `.cache/codemap/logs/.salt`; anonymized files are shareable without it.
