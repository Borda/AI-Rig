# Campaign Report Format — run/SKILL.md sidecar

Loaded by Step R6 at end of campaign run. Report structure and terminal summary format.

## Report structure

> Report guidance:
>
> - Replace descriptive fields with observed run values and omit these writing notes from the completed report.
> - Agents lists agents actually dispatched this run, from the R3 strategy resolution and any R0/team spawns; never emit the placeholder verbatim.
> - Summarize successful strategies, failed strategies, and next experiments in two to three sentences.
> - Omit the Codex co-pilot line when `--codex` was not used. Say “active (ran every iteration)” only when the recorded passes support it.

```markdown
---
Title:       Run — [Goal]
Date:        [YYYY-MM-DD]
Scope:       [program.md path] / [N] iterations planned
Focus:       ML optimization run
Agents:      [Agents used]
Outcome:     GOAL_ACHIEVED | IMPROVED | STALLED | DIVERGED
Best:        [metric_key] = [best] ([delta]% improvement)
Confidence:  [score] — [key gaps]
Next steps:  /research:retro | /research:fortify | /research:run --resume
Path:        → .reports/research/run-<branch>-<date>.md
---

## Run: [Goal]

**Run ID**: [Run ID]
**Date**: [Date]
**Iterations**: [Total iterations] ([Kept count] kept, [Reverted count] reverted, [Other count] other)
**Baseline**: [Metric] = [Baseline value]
**Best**: [Metric] = [Best value] ([Delta]% improvement)
**Best commit**: [Commit SHA]
**Diary**: ".experiments/state/<run-id>/diary.md"
**Codex co-pilot**: [Co-pilot status] — [Codex pass count] Codex passes run
**Codex wins**: [Codex kept count] Codex proposals kept vs [Claude kept count] Claude proposals kept

### Experiment History

| #   | Metric | Delta  | Status   | Description | Agent | Confidence |
| --- | ------ | ------ | -------- | ----------- | ----- | ---------- |
| N   | value  | +X.X%  | status   | desc        | agent | 0.N        |

### Summary
[Strategy summary]

### Recommended Follow-ups
- [next action]
```

## Terminal summary format

```text
---
Run — [Goal]
Iterations: [Total iterations]  Kept: [Kept count]  Reverted: [Reverted count]
Baseline:   [Metric key] = [Baseline value]
Best:       [Metric key] = [Best value] ([Delta]% improvement, commit [Commit SHA])
Agent:      [Agent used]
→ saved to .reports/research/run-<branch>-<date>.md
→ diary: .experiments/state/<run-id>/diary.md
---
```
