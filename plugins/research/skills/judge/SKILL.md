---
name: judge
description: Research-supervisor review of program.md — validates experimental methodology (hypothesis clarity, measurement validity, control adequacy, scope, strategy fit) and emits APPROVED / NEEDS-REVISION / BLOCKED verdict before the expensive run loop.
argument-hint: '[<program.md>] [--skip-validation]'
effort: medium
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Research-supervisor review of `program.md` — validates experimental methodology, emits APPROVED / NEEDS-REVISION / BLOCKED verdict before expensive run loop. Read-only; never modifies code or state.

NOT for: running experiments (use `/research:run`); designing hypotheses (use `research:scientist` agent); config quality (`/audit`).

</objective>

<workflow>

## Agent Resolution

> **Foundry plugin check**: run `Glob(pattern="foundry*", path="$HOME/.claude/plugins/cache/")` returning results = installed. If check fails, proceed as if foundry available — common case; only fall back if agent dispatch explicitly fails.

When foundry **not** installed, substitute `foundry:solution-architect` with `general-purpose`, prepend role description:

| foundry agent | Fallback | Model | Role description prefix |
| --- | --- | --- | --- |
| `foundry:solution-architect` | `general-purpose` | `opusplan` | `You are a system design specialist. Evaluate architectural trade-offs, assess scope coverage, and identify missing dependencies. Output structured JSON only.` |

`research:scientist` in same plugin as this skill — no fallback needed if research plugin installed.

## --team flag (committee mode)

If `--team` present in arguments: after verdict (Step J5/J6), read `${CLAUDE_SKILL_DIR}/../run/modes/team.md` Phase A (hypothesis generation only). Spawn 2–3 reviewers via team protocol to independently audit methodology — majority rules on verdict. This = "committee" review mode.

# Judge Mode (Steps J1–J6)

Triggered by `judge` or `judge <file.md>`.

**Task tracking**: create tasks for J1, J2, J3, J4, J5, J6 at start — before any tool calls.

## Step J1: Locate and parse program.md

**Input resolution** (priority order):

1. Explicit argument: `/research:judge path/to/plan.md`
2. Auto-detect: `program.md` at project root
3. Latest state: scan `.experiments/state/*/state.json` for most recent with `status: running` and non-null `program_file` field
4. If nothing found: stop with error:
   ```
   No program.md found. Run /research:plan <goal> first, or provide a path: /research:judge <path.md>
   ```

**Parsing** — use program-file section-parsing rules from Step R1 in `${CLAUDE_SKILL_DIR}/../run/SKILL.md` (find `## <Section>` headings, extract first fenced code block, parse as `key: value` lines, warn on unrecognized keys). `--skip-validation` flag and `colab_hw` are judge-specific, extracted independently — not part of R1.

**Placeholder substitution** — after parsing, apply same substitution step as R1: resolve all `{field_name}` tokens in `metric_cmd` and `guard_cmd` using corresponding field from `## Config`, falling back to declared default. No `clarification_prompt` in judge — skip clarification-override step.

Extract `<program_title>` from `# Program: <title>` line for reports (fall back to `# Campaign: <title>` for legacy files).

## Step J2: Completeness audit

Check each of 11 items. Produce findings list with severity. Each finding has: `id`, `check`, `status` (pass/fail/warn), `severity`, `detail`.

| ID | Check | Severity if failing | Description |
| --- | --- | --- | --- |
| C1 | `## Goal` present and non-empty | critical | Campaign cannot run without a goal |
| C2 | `## Metric` has `command` field | critical | No metric = no feedback loop |
| C3 | `## Metric` has `direction` field (higher/lower) | critical | Cannot decide keep/revert without direction |
| C4 | `## Guard` has `command` field | critical | Without guard, regressions go undetected |
| C5 | `scope_files` present in `## Config` | high | Without scope, ideation agent modifies arbitrary files |
| C6 | Each `scope_files` path exists on disk (glob match) | high | Non-matching patterns mean ideation agent has nothing to work with. If filesystem is unavailable, flag as `warn` unless the path name explicitly signals non-existence (e.g., `nonexistent`, `placeholder`, `todo`, `legacy_v1`, `deprecated`, `old`, `removed`). |
| C7 | `target` set in `## Metric` | medium | Without target, campaign runs to max_iterations — may waste compute |
| C8 | `max_iterations` in bounds (1–50) | medium | Missing defaults to 20 (acceptable); >50 violates SKILL.md constants |
| C9 | `agent_strategy` is valid (`auto`/`perf`/`code`/`ml`/`arch`) | medium | Invalid value silently falls back to `auto` |
| C10 | `compute` is valid (`local`/`colab`/`docker`) | low | Invalid defaults to `local` |
| C11 | `colab_hw` valid (if present) | low | `colab_hw` absent OR is one of `H100, L4, T4, A100` — fail detail: `"colab_hw '<value>' is not in known set {H100, L4, T4, A100} — may cause GPU identity check failure in run mode"` |
| C12 | `## Notes` section present | low | Notes are optional but improve ideation quality |

**Severity summary**: count findings per severity level. Any critical finding = verdict cannot be APPROVED.

**Placeholder token check (C2, C4 sub-rule)** — after confirming `command` field present in `## Metric` (C2) and `## Guard` (C4), scan each command for `{...}` tokens. For each token, verify corresponding field name exists in `## Config` (any value, including declared default). Token with no matching `## Config` field = unresolvable — add `high` finding. Do not flag `{field_name}` tokens as malformed; they're valid when resolvable.

**Command feasibility**: J2 validates command fields statically (presence, format). Actual executability deferred to J4. If `--skip-validation` passed, J4 skipped, command feasibility unverified — report in judge output as "validation skipped — commands unverified."

## Step J3: Methodology review

Pre-compute run directory before spawning:

```bash
RUN_DIR=".experiments/judge-$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"
```

Spawn `foundry:solution-architect` agent via `Agent(subagent_type="foundry:solution-architect", prompt="...")` (uses `opusplan` for high reasoning quality) with this prompt:

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

**Health monitoring** (CLAUDE.md §8):

```bash
LAUNCH_AT=$(date +%s)
CHECKPOINT="/tmp/judge-check-$LAUNCH_AT"
touch "$CHECKPOINT"
```

Poll every 5 min: `find <RUN_DIR> -newer "$CHECKPOINT" -type f | wc -l` — new files = alive; zero = stalled.

- **Hard cutoff: 15 min** no file activity → timed out
- **One extension (+5 min)**: if `tail -20 <RUN_DIR>/methodology.md` shows active progress (partial content written), grant one extension; second stall = hard cutoff
- **On timeout**: read `tail -100 <RUN_DIR>/methodology.md`; if file missing or empty, set `methodology_rating = "timed_out"`, continue to J6 with that value. Surface with ⏱ in report.

**Scientist health monitoring** — poll `<RUN_DIR>/scientific-review.md` on same 5-min cadence:

```bash
LAUNCH_AT_SCI=$(date +%s)
CHECKPOINT_SCI="/tmp/judge-check-sci-$LAUNCH_AT_SCI"
touch "$CHECKPOINT_SCI"
```

Poll every 5 min: `find <RUN_DIR> -name "scientific-review.md" -newer "$CHECKPOINT_SCI" | wc -l` — file present = alive; zero = stalled.

- **Hard cutoff: 15 min** no file activity → timed out
- **On timeout**: set `scientific_rating = "timed_out"`, continue to J6; surface with ⏱ in Scientific Rigor section.

Use `methodology_rating` from returned envelope for verdict computation in J6:

- `sound` → supports APPROVED
- `needs-refinement` → supports NEEDS-REVISION
- `fundamentally-flawed` → supports BLOCKED

Also spawn `research:scientist` in parallel via `Agent(subagent_type="research:scientist", prompt="...")` (dispatch both in single response at start of J3) to review scientific rigor:

```
Act as an ML research peer reviewer assessing experimental protocol rigor.

Read the campaign program file at <path_to_program.md>.

Review across four dimensions:
1. **Hypothesis falsifiability**: Is the goal precisely stated — can you tell unambiguously when the experiment has succeeded or failed?
2. **Goodhart's Law**: Could the metric improve while the actual goal is NOT achieved? Name any specific proxy-gaming risks.
3. **Missing baselines**: What standard controls, ablations, or baselines would a peer reviewer expect that are absent?
4. **Reproducibility risks**: List concrete factors that could produce non-reproducible results (randomness seeds, dataset splits, flaky tests, environment dependencies).

Write findings to `<RUN_DIR>/scientific-review.md`.
Return ONLY: {"status":"done","scientific_rating":"sound|needs-refinement|fundamentally-flawed","issues":N,"file":"<RUN_DIR>/scientific-review.md","confidence":0.N,"summary":"<one-line>"}
```

Use `scientific_rating` as **advisory** input in J6 report under **Scientific Rigor** section — informs but does not override verdict. Exception: `scientific_rating = "fundamentally-flawed"` elevates verdict to BLOCKED with note to redesign hypothesis.

## Step J4: Local validation

> Skip if `--skip-validation` flag present in arguments.

Execute each command once to verify. **Non-blocking** — failures become `critical` findings, not hard stops.

**Substitution invariant** — `metric_cmd` and `guard_cmd` fully resolved in J1. No literal `{...}` tokens should remain. If any `{field_name}` token still present, add `critical` finding: "Unresolved placeholder `{field_name}` in `<metric_cmd|guard_cmd>` — substitution failed in J1" and skip execution of that command.

```bash
# Metric validation — captures baseline value
<metric_cmd 2>&1  # timeout: 360000
```

Parse stdout for float value. If found, record as `baseline_value`. If not found or command exits non-zero: add critical finding: "Metric command failed or produced no numeric output".

```bash
# Guard validation
<guard_cmd  # timeout: 360000
```

If guard exits non-zero: add critical finding: "Guard command exited non-zero (exit <code>): \<first 3 lines of output>".

Record validation results for J6 report.

**Note**: J4 executes on current machine. For cross-machine workflows (e.g., plan locally, campaign on GPU), pass `--skip-validation`.

## Step J5: Codex adversarial review

Check Codex availability:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex'
```

**If available**: invoke adversarial review focused on specific gaps found in J2 and J3. Construct focus string from top 3 critical/high findings. Example (replace `<top finding N>` with actual findings from J2/J3):

```
Agent(subagent_type="codex:codex-rescue", prompt="Adversarial review of run program: check <top finding 1>, <top finding 2>, and <top finding 3> in the program.md. Read-only: do not apply fixes.")
```

Incorporate Codex findings into overall findings list with `source: "codex"`.

**If unavailable**: print one line and continue:

```
note: codex plugin not installed — skipping adversarial review (Claude-only judge)
```

## Step J6: Verdict and report

**Verdict computation** (deterministic — based on design soundness, not outcome prediction):

Evaluate top-to-bottom; **first match wins**. BLOCKED always takes precedence — do not continue to subsequent rows once matched.

| Condition | Verdict |
| --- | --- |
| any critical OR methodology_rating = `fundamentally-flawed` OR scientific_rating = `fundamentally-flawed` (note: `timed_out` does **not** trigger BLOCKED — it maps to NEEDS-REVISION via the next row) | BLOCKED |
| J3 agent timed out (`methodology_rating` = `timed_out` or null) | NEEDS-REVISION |
| 0 critical AND (high > 0 OR methodology_rating = `needs-refinement`) | NEEDS-REVISION |
| 0 critical AND 0 high AND methodology_rating = `sound` | APPROVED |

**Pre-compute**:

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')
```

**Write full report** to `.temp/output-judge-$BRANCH-$(date +%Y-%m-%d).md`:

```markdown
## Judge Report: <program_title>

**Program**: <path to program.md>
**Date**: <date>
**Verdict**: APPROVED | NEEDS-REVISION | BLOCKED

### Completeness Audit
| ID | Check | Status | Severity | Detail |
|----|-------|--------|----------|--------|

### Methodology Review
**Rating**: sound | needs-refinement | fundamentally-flawed | timed-out
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

### Scientific Rigor (advisory)
**Rating**: sound | needs-refinement | fundamentally-flawed | timed-out
Read full review: `<RUN_DIR>/scientific-review.md`

- Hypothesis falsifiability: <one-line finding>
- Goodhart's Law risk: <one-line finding>
- Missing baselines: <one-line finding>
- Reproducibility risks: <one-line finding>

### Dry-Run Results
| Command | Status | Output |
|---------|--------|--------|
| metric_cmd | pass/fail | <baseline value or first error line> |
| guard_cmd | pass/fail | exit 0 or exit N: <first error line> |

(Skipped — `--skip-validation`) [if applicable]

### Codex Review
<findings from Codex adversarial review, annotated with source: "codex">
(Skipped — codex plugin not installed) [if unavailable]

### Required Changes
<ordered list of specific fixes for each non-pass finding, critical first; include exact edits to program.md>

### Supervisor Decision
[APPROVED] Experimental protocol is sound. Proceed: `/research:run <path>`
[NEEDS-REVISION] Refine the protocol (see Required Changes above), then re-submit: `/research:judge <path>`
[BLOCKED] Fundamental design flaw — the experiment as designed cannot produce valid results. Fix items 1-N before proceeding.

## Confidence
**Score**: 0.N — [high|moderate|low]
**Gaps**:
- [specific limitation]
```

**Terminal summary** (compact):

```
---
Judge — <program_title>
Verdict:      APPROVED | NEEDS-REVISION | BLOCKED
Methodology:  sound | needs-refinement | fundamentally-flawed
Scientific:   sound | needs-refinement | fundamentally-flawed | timed-out  (advisory)
Findings:     <N> critical · <N> high · <N> medium · <N> low
Protocol gaps: <N>
Validation:   metric=<value> guard=pass|fail  (or "skipped — --skip-validation")
Codex:        reviewed | skipped
→ saved to .temp/output-judge-<branch>-<date>.md
---
Next: /research:run <path>                         [APPROVED]
Next: fix protocol, re-run /research:judge <path>      [NEEDS-REVISION or BLOCKED]
```

## Notes

- Judge read-only — never modifies code, commits, or writes to `.experiments/state/`
- `.experiments/judge-<timestamp>/` stores methodology review agent's full output for reference
- Validation commands execute on current machine — use `--skip-validation` for cross-machine workflows
- Verdict deterministic (finding counts + methodology_rating); not inferred from prose
- Re-run judge after editing `program.md` to confirm fixes resolved flagged items
- Judge run directories don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (exempt per `.claude/rules/artifact-lifecycle.md` TTL policy — no `result.jsonl` = cleanup skipped); remove manually when no longer needed (`rm -rf .experiments/judge-*/`)

</workflow>
