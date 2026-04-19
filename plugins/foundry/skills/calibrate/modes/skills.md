**Re: Compress markdown to caveman format**

<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: skills

### Domain table

Skill domains:

- `/audit` → synthetic `.claude/` config with N injected structural issues
- `/oss:review` → synthetic Python module with N cross-domain issues (arch + tests + docs + lint)
- `/research:plan` → synthetic optimization goal (e.g. "reduce pytest runtime by 30%"); measure whether plan mode produces complete, valid `program.md` with all required sections, plausible `metric_cmd`, correct `direction`, coherent `scope_files`
- `/research:judge` → synthetic `program.md` with N injected plan-quality issues (e.g. missing guard command, absent `direction`, non-existent `scope_files` path, invalid `agent_strategy`); measure whether judge correctly identifies each injected issue at right severity
- `/develop:review` → synthetic Python file with N injected code-quality issues (style, correctness, coverage gaps); measure whether review identifies each injected issue at correct severity level
- `/codemap:query` → synthetic codemap index with known centrality/coupling values; measure whether `central`, `coupled`, `deps`, `rdeps`, `path` queries return correct modules matching ground-truth graph structure
- `/codemap:scan` → synthetic Python project with known module structure; measure whether scan correctly identifies modules, dependencies, and produces valid index
- `/codemap:integration` → synthetic project with known skill integration opportunities; measure whether integration correctly scores and ranks candidate skills

### Step 2: Spawn skill pipeline subagents

Mark "Calibrate skills" in_progress. For each skill in domain table, spawn one `general-purpose` pipeline subagent. Issue ALL spawns in **single response**.

For skill targets (target name starts with `/`): spawn `general-purpose` subagent with skill's `SKILL.md` content prepended as context, running against synthetic input from problem. Pipeline template write-and-acknowledge pattern still applies.

For mode-specific targets (`/research:plan`, `/research:judge`): prepend relevant mode file as context instead of full `SKILL.md` — read `plugins/research/skills/plan/SKILL.md` (plan wizard steps P-P0–P-P3) for `/research:plan`, and `plugins/research/skills/judge/SKILL.md` (steps J1–J6) for `/research:judge`. `<TARGET>` substitution uses kebab form without leading slash (e.g. `research-plan`, `research-judge`).

For `/research:judge`, calibration pattern mirrors `/audit`: inject N specific known issues into synthetic `program.md`, score recall of injected issues against judge's findings list. Ground truth = injected issues and severities (per J2 severity table: critical/high/medium/low).

For `/research:plan`, calibration measures output completeness: generate synthetic goal, score whether produced `program.md` (a) contains all four required sections (Goal, Metric, Guard, Config), (b) has `direction` field, (c) has non-empty `scope_files`, (d) includes plausible `metric_cmd`. Ground truth = checklist; recall = fraction of checklist items present.

Each subagent receives pipeline template from `.claude/skills/calibrate/templates/pipeline-prompt.md` with substitutions:

- `<TARGET>` = skill name including `/` prefix (e.g., `/audit`)
- `<DOMAIN>` = domain string from table above for that skill
- `<N>` = 3 (fast) or 10 (full)
- `<TIMESTAMP>` = current run timestamp
- `<MODE>` = `fast` or `full`
- `<AB_MODE>` = `true` or `false`

**Partial-calibration principle**: individual skill modes with deterministic, auditable outputs can be calibrated even when full orchestration skill cannot. Full `optimize run` loop (requires live metric commands, git state, real guard scripts) excluded. Sub-modes producing structured, inspectable output are in scope:

- `optimize plan` — config wizard; output is `program.md` checkable against completeness schema
- `optimize judge` — plan auditor; output is findings list checkable against injected known issues (same pattern as `/audit`)

Other orchestration-heavy skills excluded: `resolve`, `manage`, `develop`, `research`, `brainstorm`. Outputs too context-dependent or long-horizon for synthetic ground truth without significant test infrastructure.

Run dir per skill: `.reports/calibrate/<TIMESTAMP>/<TARGET>/` (strip `/` from target name for dir, e.g. `audit` or `review`)

### Future Candidates

Modes evaluated for calibration but deferred — significant barriers. `/audit` Check 19 skips modes listed here to avoid false-positive recommendations.

| Mode | Barrier | Re-evaluate when |
| --- | --- | --- |
| `/analyse-thread` | Requires GitHub API mocking — thread analysis fetches live issue/PR data | GitHub fixture infrastructure exists |
| `/analyse-health` | Requires live GitHub API — health overview fetches real repo stats (issue/PR counts) | GitHub fixture infrastructure exists |
| `/analyse-ecosystem` | Requires live GitHub API — ecosystem analysis fetches real package/dependency data | GitHub fixture infrastructure exists |
| `/release-notes` | Requires controlled git history — output depends on real commit range | Git-history fixture helper exists |
| `/release-changelog` | Same as `/release-notes` — git-history dependent | Git-history fixture helper exists |
| `/release-summary` | Same as `/release-notes` — git-history dependent | Git-history fixture helper exists |
| `/release-audit` | Requires controlled repo state (version tags, CHANGELOG, CI status) | Release fixture infrastructure exists |
| `/develop-plan` | Output is somewhat subjective; no clear ground-truth checklist beyond section presence | Structured plan schema is formalized |
| `/distill-review` | Reads real agent/skill files; synthetic roster possible but overlaps `/audit` calibration | Distinct synthetic scenarios identified |
| `/distill-prune` | Likely calibratable — construct a synthetic memory corpus with known entries to drop (stale, redundant, duplicated-in-CLAUDE.md), then score recall of correct drop/trim/keep decisions; ground truth is constructable | Synthetic memory corpus fixtures built |
| `/distill-lessons` | Reads real `.notes/lessons.md`; needs realistic synthetic lesson corpus | Lesson corpus fixtures exist |

**Excluded** (inherently non-calibratable — documented to avoid recurring evaluation):

- `/resolve` — orchestrates live PR review, lint, push; fully external-service-dependent
- `/manage` — CRUD on config files; no findings list to score
- `/develop:feature`/`/develop:fix`/`/develop:refactor`/`/develop:debug` — full dev lifecycle; requires git, tests, linting
- `/research:topic` — SOTA literature search; depends on live web results; no deterministic ground truth
- `/brainstorm` — creative ideation; no deterministic ground truth
- `/investigate` — open-ended diagnosis; output varies completely by symptom
- `/session` — session lifecycle management; no quality signal to measure
- `/calibrate` itself — meta-calibration circular
- `/research:run` — sustained iteration loop with live metric commands and git state
- `/research:run --resume` — continuation of run; same barriers as run
- `/research:sweep` — same barriers as `/research:run` — sustained iteration loop requiring live metrics and git state; not calibratable synthetically
