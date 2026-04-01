---
description: Artifact directory layout, naming convention, TTL policy, and cleanup hook
---

## Canonical artifact layout

All runtime artifacts live at the **project root** in `_<skill>/` directories, not inside `.claude/`. The underscore prefix sorts them together and signals "generated output, not source config".

```
_brainstorming/          ← /brainstorm spec files
_calibrations/           ← /calibrate skill runs
_resolutions/            ← /resolve lint+QA gate runs
_audits/                 ← /audit skill runs
_reviews/                ← /review skill runs
_optimizations/          ← /optimize skill runs (perf + campaign modes)
_developments/           ← /develop review-cycle runs
_analyse/                ← /analyse skill (cache-gh + thread subdirs)
_outputs/                ← quality-gates long output (cross-cutting)
  YYYY/MM/
tasks/_plans/            ← todo_*.md, plan_*.md (tracked)
  active/
  closed/
tasks/_working/          ← lessons.md, diary, guides (tracked)
```

All `_<skill>/` and `_outputs/` dirs are gitignored — they are ephemeral and TTL-managed.

## Run directory naming

Every skill creates a timestamped subdirectory:

```bash
RUN_DIR="_<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"
```

Format: `YYYY-MM-DDTHH-MM-SSZ` (UTC, dashes throughout, filesystem-safe). Example: `_calibrations/2026-03-27T20-06-22Z/`.

A completed run always contains `result.jsonl`. Incomplete runs (crashed, timed out) lack it — the TTL hook skips them (intentional: keeps them for debugging).

## TTL policy

| Location                         | TTL     | Condition                                      |
| -------------------------------- | ------- | ---------------------------------------------- |
| `_<skill>/YYYY-MM-DDTHH-MM-SSZ/` | 30 days | only dirs containing `result.jsonl`            |
| `_brainstorming/`                | 30 days | keyed on file mtime (flat spec files)          |
| `_analyse/`                      | 30 days | keyed on file mtime (flat cache files)         |
| `_outputs/`                      | 30 days | keyed on file mtime                            |
| `tasks/_plans/`                  | manual  | move to `closed/` when done; never auto-delete |
| `tasks/_working/`                | manual  | human-maintained                               |
| `.claude/logs/`                  | forever | rotate at 10 MB                                |

## Cleanup hook (SessionEnd)

The `SessionEnd` hook runs this cleanup automatically:

```bash
# Delete completed skill runs older than 30 days
find _calibrations _resolutions _audits _reviews _optimizations _developments \
  -maxdepth 2 -name "result.jsonl" -mtime +30 2>/dev/null \
  | xargs dirname | xargs rm -rf

# Delete stale brainstorm specs and temp outputs older than 30 days
find _brainstorming _analyse _outputs -type f -mtime +30 2>/dev/null | xargs rm -f

# Prune empty year/month dirs in _outputs
find _outputs -mindepth 1 -maxdepth 2 -type d -empty 2>/dev/null | xargs rmdir
```

## Settings.json allow entries

The deterministic `_*/` paths allow precise allow rules:

```text
"Bash(mkdir -p _brainstorming/*)",
"Bash(mkdir -p _analyse/*)",
"Bash(mkdir -p _calibrations/*)",
"Bash(mkdir -p _resolutions/*)",
"Bash(mkdir -p _audits/*)",
"Bash(mkdir -p _reviews/*)",
"Bash(mkdir -p _optimizations/*)",
"Bash(mkdir -p _developments/*)",
"Bash(mkdir -p _outputs/*/*)",
"Bash(find _brainstorming*)",
"Bash(find _analyse*)",
"Bash(find _calibrations*)",
"Bash(find _resolutions*)",
"Bash(find _audits*)",
"Bash(find _reviews*)",
"Bash(find _optimizations*)",
"Bash(find _developments*)",
"Bash(find _outputs*)"
```
