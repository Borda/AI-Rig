---
description: Canonical artifact directory layout, run-dir naming convention, and TTL policy for all skill outputs
paths:
  - '**'
---

## Canonical artifact layout

Runtime artifacts at **project root**, not inside `.claude/`. Output dirs dot-prefixed (`.reports/`, `.temp/`, `.plans/`, etc.) — signals ephemeral.

```text
.plans/
  blueprint/             ← /brainstorm spec and tree files  (was .brainstorming/)
  active/                ← todo_*.md, plan_*.md
  closed/                ← completed plans
.notes/                  ← lessons.md, diary, guides  (was _tasks/_working/)
.reports/
  calibrate/             ← /foundry:calibrate — final calibration reports
  resolve/               ← /oss:resolve — final lint+QA gate reports
  audit/                 ← /foundry:audit — final audit reports
  review/<timestamp>/    ← /oss:review, /develop:review — final consolidated review reports
  analyse/               ← /oss:analyse skill (thread, ecosystem, health subdirs)
  release/               ← /oss:release audit — release readiness reports
  brainstorm/            ← /foundry:brainstorm — tree review reports
  research/              ← research plugin skills (topic, judge, verify, fortify, retro, run)
.experiments/            ← /research:run (run mode)
.developments/           ← /develop:feature, /develop:fix, /develop:refactor runs
.cache/
  gh/                    ← shared GitHub API response cache (cross-skill)
.temp/
  output-<slug>-*.md     ← quality-gates prose output (cross-cutting, no dedicated skill dir)
  review/<timestamp>/    ← /oss:review, /develop:review — intermediate subagent handover files
  <skill>/<timestamp>/   ← other skills migrating to three-tier convention (audit, resolve, calibrate — pending)
```

Dot-prefixed artifact dirs gitignored — ephemeral, TTL-managed.

## Run directory naming

Each skill creates timestamped subdir under canonical base dir:

```bash
# intermediate handover — NEVER in .reports/
RUN_DIR=".temp/<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"

# final consolidated report — one per skill run
REPORT_DIR=".reports/<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$REPORT_DIR"

# RUN_DIR=".<skill>/$(date -u +%Y-%m-%dT%H-%M-%SZ)"   # .experiments/, .developments/
```

Format: `YYYY-MM-DDTHH-MM-SSZ` (UTC, dashes throughout, filesystem-safe). Example: `.reports/calibrate/2026-03-27T20-06-22Z/`.

Completed run always has `result.jsonl`. Incomplete runs (crashed, timed out) lack it — TTL hook skips them, kept for debugging.

## TTL policy

| Location | TTL | Condition |
| -- | -- | -- |
| `.reports/<skill>/YYYY-MM-DDTHH-MM-SSZ/`, `.<skill>/YYYY-MM-DDTHH-MM-SSZ/` | 30 days | only dirs containing `result.jsonl` |
| `.reports/review/<timestamp>/` (legacy flat, still produced by `/develop:review`) | 30 days | keyed on dir mtime (no result.jsonl — hook uses separate find) |
| `.reports/review/pr-<N>/run-<NNN>/` (oss lineage) | 30 days | keyed per `run-<NNN>` dir, not per `pr-<N>` — an active PR's older runs still expire; empty `pr-<N>` parent swept once its last run ages out |
| `.temp/<skill>/YYYY-MM-DDTHH-MM-SSZ/` | 30 days | keyed on file mtime (intermediate subagent handover dirs) |
| `.plans/blueprint/` | 30 days | keyed on file mtime (flat spec/tree files) |
| `.cache/gh/` | 30 days | keyed on file mtime (GitHub API response cache) |
| `.temp/` | 30 days | keyed on file mtime |
| `.plans/active/`, `.plans/closed/` | manual | move to `closed/` when done; never auto-delete |
| `.notes/` | manual | human-maintained |
| `releases/<version>/` | manual | release artefacts; archive or delete after shipping |

Log file TTL and SessionEnd cleanup hook in `foundry-config.md` (foundry-infrastructure only).
