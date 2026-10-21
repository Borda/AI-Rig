<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

<!-- loads: rules-pipeline-prompt.md -->

## Mode: rules

> **Codex integration: disabled.** Problem generation and scoring Claude-only. Rule adherence tests Claude agent behavior with installed rules loaded — Codex has no insight into Claude Code's rule-loading, path-scoping, or frontmatter parsing. Its problems and scores unreliable here.

Rule adherence test: for each resolved rule file (user-level `~/.claude/rules/*.md`, where `/foundry:setup` installs `foundry-*.md`, plus project `.claude/rules/*.md`; `--local` swaps the user tier for source `plugins/*/rules/*.md`), measures three dimensions — trigger fidelity (rule fires right time), directive adherence (rule followed when loaded), outcome correctness (following rule = expected result). Included in `all`. Use explicit `rules` target to isolate.

### Three scoring dimensions

**1. Trigger fidelity** (path-scoped rules only — `paths:` frontmatter present). Rule loads when it should, stays silent when it shouldn't?

- Trigger recall ≥ 0.95: rule fires for all matching file contexts
- Trigger precision ≥ 0.95: rule silent for non-matching contexts
- Global rules (no `paths:`) always load — no trigger test; set to `null`

**2. Directive adherence.** Rule loaded — does `general-purpose` agent apply directives?

- Adherence recall ≥ 0.80 per directive (stricter than 0.70 agent threshold — rules are narrow action-prescribing directives)
- Three outcomes per task: `correct` / `missed` / `misapplied`

**3. Outcome correctness.** Beyond stating intent, does response's actual content (commands used, flags omitted, files listed) satisfy directive?

- Outcome correctness ≥ 0.80 of "correct" adherence scores
- Distinguishes "agent acknowledged rule" from "agent actually followed it"

### Verdict mapping

| Adherence recall | Outcome correct | Verdict |
| -- | -- | -- |
| ≥ 0.80 | ≥ 0.80 | calibrated |
| ≥ 0.80 | < 0.80 | outcome-gap |
| < 0.80 | any | under-enforced |

*Legend: Adherence recall — fraction of tasks where directive followed (0–1, higher better). Outcome correct — fraction of applied directives that produced expected behavioral output, not just stated intent (0–1, higher better). Verdict: calibrated = rule effective; outcome-gap = rule mentioned but not truly applied; under-enforced = rule ignored.*

### Step 2: Spawn rules pipeline subagents

**N per directive** (fast=3, full=5). Mark "Calibrate rules" in_progress.

**Detect scope for each rule file**: check whether `paths:` frontmatter present and non-empty — set `IS_PATH_SCOPED=true` accordingly.

Resolve rule files — `rules/*.md` globs are one level deep, so `_full/` detail files never enter; dedupe by resolved target because installed rules are symlinks:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
RULE_GLOBS='~/.claude/rules/*.md .claude/rules/*.md'; [ "$LOCAL_MODE" = "true" ] && RULE_GLOBS='plugins/*/rules/*.md .claude/rules/*.md'
python -c "import glob,os,sys;s=set();[s.add(r) or print(p) for g in sys.argv[1].split() for p in sorted(glob.glob(os.path.expanduser(g))) for r in [os.path.realpath(p)] if r not in s]" "$RULE_GLOBS" > "${TMPDIR:-/tmp}/calibrate-state-${CSID}/rule-files"  # timeout: 5000
[ -s "${TMPDIR:-/tmp}/calibrate-state-${CSID}/rule-files" ] && cat "${TMPDIR:-/tmp}/calibrate-state-${CSID}/rule-files" || echo "! INCOMPLETE — no rule files resolved from: $RULE_GLOBS"
```

**Empty-set stop** — `! INCOMPLETE` printed: never report rules as calibrated or under-enforced. Target `rules` alone → hard stop, verdict `incomplete`, print the line, no spawns. Inside `all` → print `⚠ rules — no rule files resolved; adherence not measured`, mark "Calibrate rules" completed with verdict `incomplete`, continue other categories.

Per rule file:

```bash
awk '/^---$/{c++; if(c==2)exit} c==1 && /^paths:/{found=1} END{print found+0}' <rule-file>
```

Load rules pipeline template via Bash (not Read tool — Bash grant is version-proof):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate templates rules-pipeline-prompt.md --fallback-source  # timeout: 5000
```

For each rule file, substitute `<RULE_BASENAME>`, `<RULE_CONTENT>`, `<TIMESTAMP>`, `<MODE>`, `<N>`, `<IS_PATH_SCOPED>` and spawn **single** `general-purpose` pipeline subagent.

**Spawn in batches of `$PIPELINE_BATCH_SIZE` (5 when this category runs alone, 2 while two categories in flight — see constants)**: issue up to that many rule pipeline spawns per response, collect every result in the batch via completion notifications and the SKILL.md Step 2 batch deadline (never a waiting tool), spawn next batch. Rule files within a batch run concurrently; batches sequential.

Run dir: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/` (where `RULE_DIR="${RULE_BASENAME%.md}"` — `.md` stripped to avoid permission-matcher conflicts; `--local` source rules prefix the plugin dir, `<plugin>-<name>`, since several plugins ship the same basename)

Each pipeline subagent handles all five phases internally (problem generation → target runs → dedicated scorer subagents → aggregate → curator proposals) and returns ONLY compact JSON envelope.

### Report format (Step 3 output)

When target is `rules`, replace standard combined report table with:

```markdown
## Rules Calibration — <date> — <MODE>

| Rule file              | Adherence | Outcome | Trig R | Trig P | Verdict          |
|------------------------|-----------|---------|--------|--------|------------------|
| git-commit.md          | 0.89      | 0.91    | —      | —      | ✓ calibrated     |
| python-code.md         | 0.67 ⚠    | —       | —      | —      | ⚠ under-enforced |
| hooks-js.md            | 0.82      | 0.70 ⚠  | 1.00   | 1.00   | ⚠ outcome-gap    |

*Legend: Adherence — mean fraction of tasks where directive was followed (0–1, higher is better, ≥0.80 target). Outcome — fraction of applied directives that also produced the correct behavioral output (0–1, higher is better, ≥0.80 target; — = no correct adherence scores to evaluate). Trig R — trigger recall, rule fired on matching-path contexts (0–1, higher is better, ≥0.95 target; — = global rule). Trig P — trigger precision, rule silent on non-matching contexts (0–1, higher is better, ≥0.95 target; — = global rule). Verdict: ✓ calibrated | ⚠ outcome-gap | ⚠ under-enforced.*
```

Flag any rule with adherence < 0.80, outcome_correctness < 0.80, trigger_recall < 0.95, or trigger_precision < 0.95 with ⚠.

After table, for each non-calibrated rule print `proposal.md` content (wording suggestions from curator Phase 5).

Mark "Calibrate rules" completed.

### Follow-up chain

- `under-enforced` (adherence < 0.80): reword directive to imperative mood with concrete action, re-run `/calibrate rules` to verify
- `outcome-gap` (adherence ≥ 0.80 but outcome < 0.80): directive vague at behavioral level; add concrete example or constraint, re-run
- Trigger recall < 0.95: `paths:` glob may not match file types where rule should apply; adjust glob pattern
- Trigger precision < 0.95: `paths:` glob too broad; tighten pattern to avoid false loads
- Persistent failures after rewording: split rule into more focused directives. Max 3 re-run cycles; if rule still non-calibrated after third, surface persistent failures to user for manual review.

Proposals written to: `.reports/calibrate/<TIMESTAMP>/rules/<RULE_DIR>/proposal.md`
