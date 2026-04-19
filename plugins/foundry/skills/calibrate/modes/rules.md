The compressed content was passed inline — no file to read. The error is the legend *inside* the report-format code block was compressed (caveman-shortened) when it must be preserved exactly.

Diff between ORIGINAL and COMPRESSED legend line inside the fenced block:

- ORIGINAL: `*Legend: Adherence — mean fraction of tasks where directive was followed (0–1, higher is better, ≥0.80 target). Outcome — fraction of applied directives that also produced the correct behavioral output (0–1, higher is better, ≥0.80 target; ...`
- COMPRESSED (wrong): `*Legend: Adherence — mean fraction of tasks where directive followed (0–1, higher better, ≥0.80 target). Outcome — fraction of applied directives that produced correct behavioral output (0–1, higher better, ≥0.80 target; ...`

Here is the fixed compressed file:

______________________________________________________________________

**Re: Compress markdown to caveman format**

<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: rules

> **Codex integration: disabled.** Problem generation and scoring Claude-only. Rule adherence tests Claude agent behavior with `.claude/rules/` loaded — Codex has no insight into Claude Code's rule-loading, path-scoping, or frontmatter parsing. Its problems and scores unreliable here.

Rule adherence test: for each rule file in `.claude/rules/`, measures three dimensions — trigger fidelity (rule fires right time), directive adherence (rule followed when loaded), outcome correctness (following rule = expected result). Included in `all`. Use explicit `rules` target to run in isolation.

### Three scoring dimensions

**1. Trigger fidelity** (path-scoped rules only — `paths:` frontmatter present) Rule load when should, stay silent when shouldn't?

- Trigger recall ≥ 0.95: rule fires for all matching file contexts
- Trigger precision ≥ 0.95: rule silent for non-matching contexts
- Global rules (no `paths:`) always load — no trigger test; set to `null`

**2. Directive adherence** Rule loaded — does `general-purpose` agent apply directives?

- Adherence recall ≥ 0.80 per directive (stricter than 0.70 agent threshold — rules are narrow action-prescribing directives)
- Three outcomes per task: `correct` / `missed` / `misapplied`

**3. Outcome correctness** Beyond stating intent, does response's actual content (commands used, flags omitted, files listed) satisfy directive?

- Outcome correctness ≥ 0.80 of "correct" adherence scores
- Distinguishes "agent acknowledged rule" from "agent actually followed it"

### Verdict mapping

| Adherence recall | Outcome correct | Verdict |
| --- | --- | --- |
| ≥ 0.80 | ≥ 0.80 | calibrated |
| ≥ 0.80 | < 0.80 | outcome-gap |
| < 0.80 | any | under-enforced |

*Legend: Adherence recall — fraction of tasks where directive followed (0–1, higher better). Outcome correct — fraction of applied directives that produced expected behavioral output, not just stated intent (0–1, higher better). Verdict: calibrated = rule effective; outcome-gap = rule mentioned but not truly applied; under-enforced = rule ignored.*

### Step 2: Spawn rules pipeline subagents

**N per directive** (fast=3, full=5). Mark "Calibrate rules" in_progress.

**Detect scope for each rule file**: check whether `paths:` frontmatter present and non-empty — set `IS_PATH_SCOPED=true` accordingly.

```bash
# Enumerate rule files
ls .claude/rules/*.md 2>/dev/null | sort

# Detect path-scoped rule (IS_PATH_SCOPED=true if paths: field is non-empty)
awk '/^---$/{c++; if(c==2)exit} c==1 && /^paths:/{found=1} END{print found+0}' <rule-file>
```

Read `.claude/skills/calibrate/templates/rules-pipeline-prompt.md`. For each rule file, substitute `<RULE_BASENAME>`, `<RULE_CONTENT>`, `<TIMESTAMP>`, `<MODE>`, `<N>`, `<IS_PATH_SCOPED>` and spawn **single** `general-purpose` pipeline subagent.

**Issue all spawns in single response** — rule files independent, run concurrently.

Run dir: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_BASENAME>/`

Each pipeline subagent handles all five phases internally (problem generation → target runs → dedicated scorer subagents → aggregate → self-mentor proposals) and returns ONLY compact JSON envelope.

### Report format (Step 3 output)

When target is `rules`, replace standard combined report table with:

```
## Rules Calibration — <date> — <MODE>

| Rule file              | Adherence | Outcome | Trig R | Trig P | Verdict          |
|------------------------|-----------|---------|--------|--------|------------------|
| git-commit.md          | 0.89      | 0.91    | —      | —      | ✓ calibrated     |
| python-code.md         | 0.67 ⚠    | —       | —      | —      | ⚠ under-enforced |
| hooks-js.md            | 0.82      | 0.70 ⚠  | 1.00   | 1.00   | ⚠ outcome-gap    |

*Legend: Adherence — mean fraction of tasks where directive was followed (0–1, higher is better, ≥0.80 target). Outcome — fraction of applied directives that also produced the correct behavioral output (0–1, higher is better, ≥0.80 target; — = no correct adherence scores to evaluate). Trig R — trigger recall, rule fired on matching-path contexts (0–1, higher is better, ≥0.95 target; — = global rule). Trig P — trigger precision, rule silent on non-matching contexts (0–1, higher is better, ≥0.95 target; — = global rule). Verdict: ✓ calibrated | ⚠ outcome-gap | ⚠ under-enforced.*
```

Flag any rule with adherence < 0.80, outcome_correctness < 0.80, trigger_recall < 0.95, or trigger_precision < 0.95 with ⚠.

After table, for each non-calibrated rule print `proposal.md` content (wording suggestions from self-mentor Phase 5).

Mark "Calibrate rules" completed.

### Follow-up chain

- `under-enforced` (adherence < 0.80) → reword directive to imperative mood with concrete action → re-run `/calibrate rules` to verify
- `outcome-gap` (adherence ≥ 0.80 but outcome < 0.80) → directive vague at behavioral level; add concrete example or constraint → re-run
- Trigger recall < 0.95 → `paths:` glob may not match file types where rule should apply; adjust glob pattern
- Trigger precision < 0.95 → `paths:` glob too broad; tighten pattern to avoid false loads
- Persistent failures after rewording → split rule into more focused directives. Max 3 re-run cycles; if rule still non-calibrated after third, surface persistent failures to user for manual review.

Proposals written to: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_BASENAME>/proposal.md`
