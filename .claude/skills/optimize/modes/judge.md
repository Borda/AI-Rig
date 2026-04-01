# Judge Mode (Steps J1–J6)

Triggered by `judge` or `judge <file.md>`.

**Task tracking**: create tasks for J1, J2, J3, J4, J5, J6 at the start of judge mode — before any tool calls.

## Step J1: Locate and parse program.md

**Input resolution** (priority order):

1. Explicit argument: `/optimize judge path/to/plan.md`
2. Auto-detect: `program.md` at project root
3. Latest state: scan `_optimizations/state/*/state.json` for most recent with `status: running` and non-null `program_file` field
4. If nothing found: stop with error:
   ```
   No program.md found. Run /optimize plan <goal> first, or provide a path: /optimize judge <path.md>
   ```

**Parsing** — apply these rules (identical to C1 in campaign.md):

1. Find each `## <Section>` heading (case-insensitive)
2. Extract the first fenced code block following that heading
3. Parse the block as `key: value` lines; multi-value fields use indented `  - value` list items
4. Missing required fields are flagged as findings (not a hard stop)
5. `agent_strategy: auto` (or omitted) → apply keyword heuristics from `<constants>` in SKILL.md
6. `## Notes` and the `# Campaign:` title are read for context but not validated
7. Unrecognized keys → warn once, then ignore

Extract a `<campaign_title>` from the `# Campaign: <title>` line for use in reports.

## Step J2: Completeness audit

Check each of the 11 items below. Produce a findings list with severity. Each finding has: `id`, `check`, `status` (pass/fail/warn), `severity`, `detail`.

| ID  | Check                                                        | Severity if failing | Description                                                                                                                                                                                                          |
| --- | ------------------------------------------------------------ | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C1  | `## Goal` present and non-empty                              | critical            | Campaign cannot run without a goal                                                                                                                                                                                   |
| C2  | `## Metric` has `command` field                              | critical            | No metric = no feedback loop                                                                                                                                                                                         |
| C3  | `## Metric` has `direction` field (higher/lower)             | critical            | Cannot decide keep/revert without direction                                                                                                                                                                          |
| C4  | `## Guard` has `command` field                               | critical            | Without guard, regressions go undetected                                                                                                                                                                             |
| C5  | `scope_files` present in `## Config`                         | high                | Without scope, ideation agent modifies arbitrary files                                                                                                                                                               |
| C6  | Each `scope_files` path exists on disk (glob match)          | high                | Non-matching patterns mean ideation agent has nothing to work with. If filesystem is unavailable, flag as `warn` unless the path name explicitly signals non-existence (e.g., `nonexistent`, `placeholder`, `todo`). |
| C7  | `target` set in `## Metric`                                  | medium              | Without target, campaign runs to max_iterations — may waste compute                                                                                                                                                  |
| C8  | `max_iterations` in bounds (1–50)                            | medium              | Missing defaults to 20 (acceptable); >50 violates SKILL.md constants                                                                                                                                                 |
| C9  | `agent_strategy` is valid (`auto`/`perf`/`code`/`ml`/`arch`) | medium              | Invalid value silently falls back to `auto`                                                                                                                                                                          |
| C10 | `compute` is valid (`local`/`colab`)                         | low                 | Invalid defaults to `local`                                                                                                                                                                                          |
| C11 | `## Notes` section present                                   | low                 | Notes are optional but improve ideation quality                                                                                                                                                                      |

**Severity summary**: count findings at each severity level. Any critical finding means the verdict cannot be READY.

## Step J3: Methodology review

Pre-compute the run directory before spawning:

```bash
RUN_DIR="_optimizations/judge-$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"
```

Spawn a **solution-architect** agent (uses `opusplan` for high reasoning quality) with this prompt:

```
Act as a research supervisor reviewing a PhD student's experimental protocol.
Your job is NOT to predict whether the experiment will succeed — it is to judge whether the experimental design is methodologically sound and whether the student should be allowed to proceed.

Read the campaign program file at <path_to_program.md>.
Also read the codebase (Glob **/*.py, **/*.ts, **/*.js at project root, limit 50 files) for structural context.

Review the experimental protocol across seven dimensions:

1. **Hypothesis clarity**: Is the `## Goal` a clear, testable hypothesis? Can you tell what constitutes success vs failure? Vague goals produce unfocused experiments — flag if the hypothesis is ambiguous.
2. **Measurement validity**: Does `<metric_cmd>` correctly operationalize the hypothesis? Does it measure what the goal actually intends? Could the metric move in the right direction while the underlying goal is NOT achieved (Goodhart's Law)? Could noise dominate signal at the expected delta scale?
3. **Control adequacy**: Does `<guard_cmd>` serve as a valid control condition? Does it catch regressions that an ideation agent could inadvertently introduce? Is it too strict (would block valid improvements) or too permissive (would miss real breakage)?
4. **Experimental scope**: Do the `scope_files` define a coherent experimental boundary? Are there known dependencies outside scope that could confound results? Is the scope too broad (unfocused changes) or too narrow (the real lever is outside scope)?
5. **Protocol consistency**: Is `agent_strategy: <strategy>` logically consistent with the hypothesis type? (e.g., using `perf` strategy to improve code quality is a methodology mismatch — flag it)
6. **Stopping criteria**: Is the termination condition well-defined? A missing `target` means the experiment runs until budget exhaustion — flag if the goal implies a natural stopping point that is not encoded.
7. **Reproducibility concerns**: What aspects of the protocol could produce non-reproducible results across runs? (Flaky tests, non-deterministic metrics, environment-sensitive commands)

Also identify up to 3 **protocol gaps** — specific changes to `program.md` that would make the experiment more rigorous.

Write your full review to `<RUN_DIR>/methodology.md` using the Write tool.
Include a `## Verdict` section with a `methodology_rating`: `sound` (no significant design flaws), `needs-refinement` (fixable issues found), or `fundamentally-flawed` (a core design problem that would invalidate the experiment).
Include a `## Confidence` block per quality-gates.md.
Return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","review_dimensions":7,"methodology_rating":"sound|needs-refinement|fundamentally-flawed","protocol_gaps":N,"file":"<RUN_DIR>/methodology.md","confidence":0.N,"summary":"<one-line verdict>"}
```

Use `methodology_rating` from the returned envelope for verdict computation in J6:

- `sound` → supports APPROVED
- `needs-refinement` → supports NEEDS-REVISION
- `fundamentally-flawed` → supports BLOCKED

## Step J4: Dry-run validation

> Skip this step if `--no-dry-run` flag is present in the arguments.

Execute each command once to verify they work. These are **non-blocking** — failures become `critical` findings, not hard stops.

```bash
# Metric dry-run — captures baseline value
timeout 120 <metric_cmd> 2>&1
```

Parse stdout for a float value. If found, record as `baseline_value`. If not found or command exits non-zero: add critical finding: "Metric command failed or produced no numeric output".

```bash
# Guard dry-run
timeout 120 <guard_cmd>
```

If guard exits non-zero: add critical finding: "Guard command exited non-zero (exit <code>): \<first 3 lines of output>".

Record dry-run results for the J6 report.

**Note**: J4 executes commands on the current machine. For cross-machine workflows (e.g., planning locally, campaigning on GPU), use `--no-dry-run` to skip this step.

## Step J5: Codex adversarial review

Check Codex availability:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'
```

**If available**: invoke adversarial review focused on the specific gaps found in J2 and J3. Construct a focus string from the top 3 critical/high findings. Example:

```
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review of campaign plan: check metric_cmd feasibility, guard coverage gaps, and scope completeness in the program.md. Read-only: do not apply fixes.")
```

Incorporate Codex findings into the overall findings list with `source: "codex"`.

**If unavailable**: print one line and continue:

```
note: codex plugin not installed — skipping adversarial review (Claude-only judge)
```

## Step J6: Verdict and report

**Verdict computation** (deterministic — based on design soundness, not outcome prediction):

| Condition                                                            | Verdict        |
| -------------------------------------------------------------------- | -------------- |
| 0 critical AND 0 high AND methodology_rating = `sound`               | APPROVED       |
| 0 critical AND (high > 0 OR methodology_rating = `needs-refinement`) | NEEDS-REVISION |
| any critical OR methodology_rating = `fundamentally-flawed`          | BLOCKED        |

**Pre-compute**:

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')
```

**Write full report** to `_outputs/$(date +%Y)/$(date +%m)/output-optimize-judge-$BRANCH-$(date +%Y-%m-%d).md`:

```markdown
## Judge Report: <campaign_title>

**Program**: <path to program.md>
**Date**: <date>
**Verdict**: APPROVED | NEEDS-REVISION | BLOCKED

### Completeness Audit
| ID | Check | Status | Severity | Detail |
|----|-------|--------|----------|--------|
| C1 | Goal present | pass/fail | critical | ... |
| ... |

### Methodology Review
**Rating**: sound | needs-refinement | fundamentally-flawed
Read full review: <RUN_DIR>/methodology.md

- Hypothesis clarity: <one-line finding>
- Measurement validity: <one-line finding>
- Control adequacy: <one-line finding>
- Experimental scope: <one-line finding>
- Protocol consistency: <one-line finding>
- Stopping criteria: <one-line finding>
- Reproducibility: <one-line finding>

**Protocol gaps** (specific improvements to program.md):
1. <gap>
2. <gap>

### Dry-Run Results
| Command | Status | Output |
|---------|--------|--------|
| metric_cmd | pass/fail | <baseline value or first error line> |
| guard_cmd | pass/fail | exit 0 or exit N: <first error line> |

(Skipped — `--no-dry-run` flag) [if applicable]

### Codex Review
<findings from Codex adversarial review, annotated with source: "codex">
(Skipped — codex plugin not installed) [if unavailable]

### Required Changes
<ordered list of specific fixes for each non-pass finding, critical first; include exact edits to program.md>

### Supervisor Decision
[APPROVED] Experimental protocol is sound. Proceed: `/optimize campaign <path>`
[NEEDS-REVISION] Refine the protocol (see Required Changes above), then re-submit: `/optimize judge <path>`
[BLOCKED] Fundamental design flaw — the experiment as designed cannot produce valid results. Fix items 1-N before proceeding.

## Confidence
**Score**: 0.N — [high|moderate|low]
**Gaps**:
- [specific limitation]
```

**Terminal summary** (compact):

```
---
Judge — <campaign_title>
Verdict:      APPROVED | NEEDS-REVISION | BLOCKED
Methodology:  sound | needs-refinement | fundamentally-flawed
Findings:     <N> critical · <N> high · <N> medium · <N> low
Protocol gaps: <N>
Dry-run:      metric=<value> guard=pass|fail  (or "skipped — --no-dry-run")
Codex:        reviewed | skipped
→ saved to _outputs/YYYY/MM/output-optimize-judge-<branch>-<date>.md
---
Next: /optimize campaign <path>                        [APPROVED]
Next: fix protocol, re-run /optimize judge <path>      [NEEDS-REVISION or BLOCKED]
```

## Notes

- Judge is read-only — it never modifies code, commits, or writes to `_optimizations/state/`
- The `_optimizations/judge-<timestamp>/` run directory stores the methodology review agent's full output for later reference
- Dry-run commands execute on the current machine — use `--no-dry-run` for cross-machine workflows
- Verdict is deterministic (finding counts + methodology_rating); it is not inferred from prose
- Re-run judge after editing `program.md` to confirm fixes resolved the flagged items
