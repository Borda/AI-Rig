---
description: Artifact directory layout, naming convention, TTL policy, and cleanup hook
paths:
  - .claude/**
---

## Canonical artifact layout

All runtime artifacts live at the **project root**, not inside `.claude/`. Skill output directories use a dot-prefix (`.reports/`, `.temp/`, `.plans/`, etc.) to signal they are ephemeral.

```
.plans/
  blueprint/             ← /brainstorm spec and tree files  (was .brainstorming/)
  active/                ← todo_*.md, plan_*.md
  closed/                ← completed plans
.notes/                  ← lessons.md, diary, guides  (was _tasks/_working/)
.reports/
  calibrate/             ← /calibrate skill runs
  resolve/               ← /resolve lint+QA gate runs
  audit/                 ← /audit skill runs
  review/                ← /review skill runs
  analyse/               ← /analyse skill (thread, ecosystem, health subdirs)
.experiments/            ← /optimize skill runs (run mode)
.developments/           ← /develop review-cycle runs
.cache/
  gh/                    ← shared GitHub API response cache (cross-skill)
.temp/                   ← quality-gates prose output (cross-cutting)
```

All dot-prefixed artifact dirs are gitignored — they are ephemeral and TTL-managed.

## Run directory naming

Every skill creates a timestamped subdirectory using its canonical base dir:

```bash
RUN_DIR=".reports/<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)" # for .reports/<skill>/ skills
# or: RUN_DIR=".<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)"   # for dedicated dirs (.experiments/, .developments/)
mkdir -p "$RUN_DIR"
```

Format: `YYYY-MM-DDTHH-MM-SSZ` (UTC, dashes throughout, filesystem-safe). Example: `.reports/calibrate/2026-03-27T20-06-22Z/`.

A completed run always contains `result.jsonl`. Incomplete runs (crashed, timed out) lack it — the TTL hook skips them (intentional: keeps them for debugging).

## TTL policy

| Location                                                                   | TTL     | Condition                                                                                           |
| -------------------------------------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------- |
| `.reports/<skill>/YYYY-MM-DDTHH-MM-SSZ/`, `.<skill>/YYYY-MM-DDTHH-MM-SSZ/` | 30 days | only dirs containing `result.jsonl`                                                                 |
| `.plans/blueprint/`                                                        | 30 days | keyed on file mtime (flat spec/tree files)                                                          |
| `.cache/gh/`                                                               | 30 days | keyed on file mtime (GitHub API response cache)                                                     |
| `.temp/`                                                                   | 30 days | keyed on file mtime                                                                                 |
| `.plans/active/`, `.plans/closed/`                                         | manual  | move to `closed/` when done; never auto-delete                                                      |
| `.notes/`                                                                  | manual  | human-maintained                                                                                    |
| `releases/<version>/`                                                      | manual  | release artefacts; archive or delete after shipping                                                 |
| `~/.claude/logs/`                                                          | forever | hook audit logs (invocations, compactions, timings) — global across all projects; rotate at 10 MB   |
| `.claude/logs/`                                                            | forever | skill-specific logs (calibrations, session-archive, audit-errors) — project-scoped; rotate at 10 MB |

## Cleanup hook (SessionEnd)

The `SessionEnd` hook runs this cleanup automatically:

```bash
# Delete completed skill runs older than 30 days
find .reports/calibrate .reports/resolve .reports/audit .reports/review .reports/analyse .experiments .developments \
    -maxdepth 2 -name "result.jsonl" -mtime +30 2>/dev/null |
xargs dirname | xargs rm -rf

# Delete stale blueprint specs, cache, and temp outputs older than 30 days
find .plans/blueprint .cache .temp -type f -mtime +30 2>/dev/null | xargs rm -f
```

## Settings.json allow entries

The deterministic dot-prefixed paths allow precise allow rules (keep in sync with `settings.json` — `/audit` Check 6 detects drift):

```text
"Bash(mkdir -p .cache/*)",
"Bash(mkdir -p .notes/)",
"Bash(mkdir -p .plans/active/)",
"Bash(mkdir -p .plans/blueprint/)",
"Bash(mkdir -p .plans/closed/)",
"Bash(mkdir -p .reports/calibrate/*)",
"Bash(mkdir -p .reports/resolve/*)",
"Bash(mkdir -p .reports/audit/*)",
"Bash(mkdir -p .reports/review/*)",
"Bash(mkdir -p .reports/analyse/*)",
"Bash(mkdir -p .experiments/*)",
"Bash(mkdir -p .developments/*)",
"Bash(mkdir -p .temp/)",
"Bash(find .cache*)",
"Bash(find .notes*)",
"Bash(find .plans*)",
"Bash(find .reports/calibrate*)",
"Bash(find .reports/resolve*)",
"Bash(find .reports/audit*)",
"Bash(find .reports/review*)",
"Bash(find .experiments*)",
"Bash(find .developments*)",
"Bash(find .reports*)",
"Bash(find .temp*)"
```
