<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: skills

### Domain table

Skill domains:

- `/audit` → synthetic `.claude/` config with N injected structural issues
- `/review` → synthetic Python module with N cross-domain issues (arch + tests + docs + lint)
- `/optimize-plan` → synthetic optimization goal (e.g. "reduce pytest runtime by 30%"); measure whether the plan mode produces a complete, valid `program.md` with all required sections, a plausible `metric_cmd`, correct `direction`, and coherent `scope_files`
- `/optimize-judge` → synthetic `program.md` with N injected plan-quality issues (e.g. missing guard command, absent `direction`, non-existent `scope_files` path, invalid `agent_strategy`); measure whether judge correctly identifies each injected issue at the right severity

### Step 2: Spawn skill pipeline subagents

Mark "Calibrate skills" in_progress. For each skill in the domain table, spawn one `general-purpose` pipeline subagent. Issue ALL spawns in a **single response**.

For skill targets (target name starts with `/`): spawn a `general-purpose` subagent with the skill's `SKILL.md` content prepended as context, running against the synthetic input from the problem. The pipeline template write-and-acknowledge pattern still applies.

For mode-specific targets (`/optimize-plan`, `/optimize-judge`): instead of the full `SKILL.md`, prepend the relevant mode file as context — read `.claude/skills/optimize/modes/plan.md` (plan wizard steps P-P0–P-P3) for `/optimize-plan`, and `.claude/skills/optimize/modes/judge.md` (steps J1–J6) for `/optimize-judge`. The `<TARGET>` substitution uses the kebab form without leading slash (e.g. `optimize-plan`, `optimize-judge`).

For `/optimize-judge`, the calibration pattern mirrors `/audit`: inject N specific known issues into the synthetic `program.md`, then score recall of those injected issues against the judge's findings list. Ground truth is the set of injected issues and their severities (per the J2 severity table: critical/high/medium/low).

For `/optimize-plan`, the calibration measures output completeness: generate a synthetic goal and score whether the produced `program.md` (a) contains all four required sections (Goal, Metric, Guard, Config), (b) has a `direction` field, (c) has non-empty `scope_files`, and (d) includes a plausible `metric_cmd`. Ground truth is the checklist; recall = fraction of checklist items present.

Each subagent receives the pipeline template from `.claude/skills/calibrate/templates/pipeline-prompt.md` with these substitutions:

- `<TARGET>` = the skill name including `/` prefix (e.g., `/audit`)
- `<DOMAIN>` = the domain string from the table above for that skill
- `<N>` = 3 (fast) or 10 (full)
- `<TIMESTAMP>` = current run timestamp
- `<MODE>` = `fast` or `full`
- `<AB_MODE>` = `true` or `false`

**Partial-calibration principle**: individual skill modes with deterministic, auditable outputs can be calibrated even when the full orchestration skill cannot. The full `optimize run` loop (which requires live metric commands, git state, and real guard scripts) is excluded. But its sub-modes that produce structured, inspectable output are in scope:

- `optimize plan` — config wizard; output is a `program.md` checkable against a completeness schema
- `optimize judge` — plan auditor; output is a findings list checkable against injected known issues (same pattern as `/audit`)

Other orchestration-heavy skills remain excluded: `resolve`, `manage`, `develop`, `research`, `brainstorm`. Their outputs are too context-dependent or long-horizon for synthetic ground truth without significant test infrastructure.

Run dir per skill: `.reports/calibrate/<TIMESTAMP>/<TARGET>/` (strip `/` from target name for the dir, e.g. `audit` or `review`)

### Future Candidates

Modes evaluated for calibration but deferred due to significant barriers. `/audit` Check 19 skips modes listed here to avoid false-positive recommendations.

| Mode                 | Barrier                                                                                                                                                                                                                | Re-evaluate when                        |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| `/analyse-thread`    | Requires GitHub API mocking — thread analysis fetches live issue/PR data                                                                                                                                               | GitHub fixture infrastructure exists    |
| `/analyse-health`    | Requires live GitHub API — health overview fetches real repo stats (issue/PR counts)                                                                                                                                   | GitHub fixture infrastructure exists    |
| `/analyse-ecosystem` | Requires live GitHub API — ecosystem analysis fetches real package/dependency data                                                                                                                                     | GitHub fixture infrastructure exists    |
| `/release-notes`     | Requires controlled git history — output depends on real commit range                                                                                                                                                  | Git-history fixture helper exists       |
| `/release-changelog` | Same as `/release-notes` — git-history dependent                                                                                                                                                                       | Git-history fixture helper exists       |
| `/release-summary`   | Same as `/release-notes` — git-history dependent                                                                                                                                                                       | Git-history fixture helper exists       |
| `/release-audit`     | Requires controlled repo state (version tags, CHANGELOG, CI status)                                                                                                                                                    | Release fixture infrastructure exists   |
| `/develop-plan`      | Output is somewhat subjective; no clear ground-truth checklist beyond section presence                                                                                                                                 | Structured plan schema is formalized    |
| `/distill-review`    | Reads real agent/skill files; synthetic roster possible but overlaps `/audit` calibration                                                                                                                              | Distinct synthetic scenarios identified |
| `/distill-prune`     | Likely calibratable — construct a synthetic memory corpus with known entries to drop (stale, redundant, duplicated-in-CLAUDE.md), then score recall of correct drop/trim/keep decisions; ground truth is constructable | Synthetic memory corpus fixtures built  |
| `/distill-lessons`   | Reads real `.notes/lessons.md`; needs realistic synthetic lesson corpus                                                                                                                                                | Lesson corpus fixtures exist            |

**Excluded** (inherently non-calibratable — documented to avoid recurring evaluation):

- `/resolve` — orchestrates live PR review, lint, push; fully external-service-dependent
- `/manage` — CRUD on config files; no findings list to score
- `/develop feature/fix/refactor/debug` — full dev lifecycle; requires git, tests, linting
- `/research` — SOTA literature search; depends on live web results; no deterministic ground truth
- `/brainstorm` — creative ideation; no deterministic ground truth
- `/investigate` — open-ended diagnosis; output varies completely by symptom
- `/session` — session lifecycle management; no quality signal to measure
- `/sync` — drift detection; deterministic but trivially correct (diff-based, no findings to score)
- `/calibrate` itself — meta-calibration is circular
- `/optimize-run` — sustained iteration loop with live metric commands and git state
- `/optimize-resume` — continuation of run; same barriers as run
- `/optimize-sweep` — same barriers as `/optimize-run` — sustained iteration loop requiring live metrics and git state; not calibratable synthetically
