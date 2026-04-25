---
description: Canonical artifact directory layout, run-dir naming convention, and TTL policy for all skill outputs
paths:
  - '**'
---

## Canonical artifact layout

Runtime artifacts live at **project root**, not inside `.claude/`.
Skill output dirs use dot-prefix (`.reports/`, `.temp/`, `.plans/`, etc.) — signals ephemeral.

```text
.plans/
  blueprint/             ← /brainstorm spec and tree files  (was .brainstorming/)
  active/                ← todo_*.md, plan_*.md
  closed/                ← completed plans
.notes/                  ← lessons.md, diary, guides  (was _tasks/_working/)
.reports/
  calibrate/             ← /foundry:calibrate skill runs
  resolve/               ← /oss:resolve lint+QA gate runs
  audit/                 ← /foundry:audit skill runs
  review/                ← /oss:review or /develop:review skill runs
  analyse/               ← /oss:analyse skill (thread, ecosystem, health subdirs)
.experiments/            ← /research:run (run mode)
.developments/           ← /develop:feature, /develop:fix, /develop:refactor runs
.cache/
  gh/                    ← shared GitHub API response cache (cross-skill)
.temp/                   ← quality-gates prose output (cross-cutting)
```

Dot-prefixed artifact dirs gitignored — ephemeral, TTL-managed.

## Run directory naming

Each skill creates timestamped subdir under canonical base dir:

```bash
RUN_DIR=".reports/<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)" # for .reports/<skill>/ skills
# or: RUN_DIR=".<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)"   # for dedicated dirs (.experiments/, .developments/)
mkdir -p "$RUN_DIR"
```

Format: `YYYY-MM-DDTHH-MM-SSZ` (UTC, dashes throughout, filesystem-safe).
Example: `.reports/calibrate/2026-03-27T20-06-22Z/`.

Completed run always has `result.jsonl`.
Incomplete runs (crashed, timed out) lack it — TTL hook skips them (keeps for debugging).

## TTL policy

| Location | TTL | Condition |
| --- | --- | --- |
| `.reports/<skill>/YYYY-MM-DDTHH-MM-SSZ/`, `.<skill>/YYYY-MM-DDTHH-MM-SSZ/` | 30 days | only dirs containing `result.jsonl` |
| `.plans/blueprint/` | 30 days | keyed on file mtime (flat spec/tree files) |
| `.cache/gh/` | 30 days | keyed on file mtime (GitHub API response cache) |
| `.temp/` | 30 days | keyed on file mtime |
| `.plans/active/`, `.plans/closed/` | manual | move to `closed/` when done; never auto-delete |
| `.notes/` | manual | human-maintained |
| `releases/<version>/` | manual | release artefacts; archive or delete after shipping |

Log file TTL and SessionEnd cleanup hook in `foundry-config.md` (foundry-infrastructure only).
