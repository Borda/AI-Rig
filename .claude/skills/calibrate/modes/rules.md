<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: rules

> **Codex integration: disabled.** Problem generation and scoring are Claude-only for this mode. Rule adherence tests the behavior of a Claude agent with `.claude/rules/` loaded — Codex has no insight into Claude Code's rule-loading mechanism, path-scoping, or frontmatter parsing, making its problems and scores unreliable here.

Rule adherence test: for each rule file in `.claude/rules/`, measures three dimensions of rule quality — trigger fidelity (rule fires at the right time), directive adherence (rule is followed when loaded), and outcome correctness (following the rule produces the expected result). Included in `all`. Use the explicit `rules` target to run this mode in isolation.

### Three scoring dimensions

**1. Trigger fidelity** (path-scoped rules only — `paths:` frontmatter present) Does the rule load when it should and stay silent when it shouldn't?

- Trigger recall ≥ 0.95: rule fires for all matching file contexts
- Trigger precision ≥ 0.95: rule stays silent for non-matching file contexts
- Global rules (no `paths:`) always load — no trigger test; set to `null`

**2. Directive adherence** Given the rule is loaded, does a `general-purpose` agent apply its directives?

- Adherence recall ≥ 0.80 per directive (stricter than the 0.70 agent threshold — rules are narrow action-prescribing directives)
- Three outcomes per task: `correct` / `missed` / `misapplied`

**3. Outcome correctness** Beyond stating intent, does the response's actual content (commands used, flags omitted, files listed) satisfy the directive?

- Outcome correctness ≥ 0.80 of "correct" adherence scores
- Distinguishes "agent acknowledged the rule" from "agent actually followed it"

### Verdict mapping

| Adherence recall | Outcome correct | Verdict        |
| ---------------- | --------------- | -------------- |
| ≥ 0.80           | ≥ 0.80          | calibrated     |
| ≥ 0.80           | < 0.80          | outcome-gap    |
| < 0.80           | any             | under-enforced |

*Legend: Adherence recall — fraction of tasks where directive was followed (0–1, higher is better). Outcome correct — fraction of applied directives that also produced the expected behavioral output, not just stated intent (0–1, higher is better). Verdict: calibrated = rule is effective; outcome-gap = rule is mentioned but not truly applied; under-enforced = rule is ignored.*

### Step 2: Spawn rules pipeline subagents

**N per directive** (fast=3, full=5). Mark "Calibrate rules" in_progress.

**Detect scope for each rule file**: check whether `paths:` frontmatter is present and non-empty — set `IS_PATH_SCOPED=true` accordingly.

```bash
# Enumerate rule files
ls .claude/rules/*.md 2>/dev/null | sort

# Detect path-scoped rule (IS_PATH_SCOPED=true if paths: field is non-empty)
awk '/^---$/{c++; if(c==2)exit} c==1 && /^paths:/{found=1} END{print found+0}' <rule-file>
```

Read `.claude/skills/calibrate/templates/rules-pipeline-prompt.md`. For each rule file, substitute `<RULE_BASENAME>`, `<RULE_CONTENT>`, `<TIMESTAMP>`, `<MODE>`, `<N>`, `<IS_PATH_SCOPED>` and spawn a **single** `general-purpose` pipeline subagent.

**Issue all spawns in a single response** — rule files are independent and run concurrently.

Run dir: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_BASENAME>/`

Each pipeline subagent handles all five phases internally (problem generation → target runs → dedicated scorer subagents → aggregate → self-mentor proposals) and returns ONLY a compact JSON envelope.

### Report format (Step 3 output)

When target is `rules`, replace the standard combined report table with:

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

After the table, for each non-calibrated rule print the proposal.md content (wording suggestions from self-mentor Phase 5).

Mark "Calibrate rules" completed.

### Follow-up chain

- `under-enforced` (adherence < 0.80) → reword directive to imperative mood with concrete action → re-run `/calibrate rules` to verify improvement
- `outcome-gap` (adherence ≥ 0.80 but outcome < 0.80) → directive is vague at the behavioral level; add a concrete example or constraint → re-run
- Trigger recall < 0.95 → `paths:` glob may not match the file types where the rule should apply; adjust the glob pattern
- Trigger precision < 0.95 → `paths:` glob is too broad; tighten the pattern to avoid false loads
- Persistent failures after rewording → consider splitting the rule into more focused directives. Max 3 re-run cycles; if the rule is still non-calibrated after the third, surface the persistent failures to the user for manual review.

Proposals written to: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_BASENAME>/proposal.md`
