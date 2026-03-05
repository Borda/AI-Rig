---
name: calibrate
description: Calibration testing for agents and skills. Generates synthetic problems with known outcomes (quasi-ground-truth), runs targets against them, and measures recall, precision, and confidence calibration — revealing whether self-reported confidence scores track actual quality.
argument-hint: '[agent-name|all|/audit|/review] [fast|full] [ab] [apply]'
allowed-tools: Read, Write, Edit, Bash, Agent
---

<objective>

Validate agents and skills by measuring their outputs against synthetic problems with defined ground truth. The primary signal is **calibration bias** — the gap between self-reported confidence and actual recall. A well-calibrated agent reports 0.9 confidence when it genuinely finds ~90% of issues. A miscalibrated one may report 0.9 while only finding 60%.

Calibration data drives the improvement loop: systematic gaps become instruction updates; persistent overconfidence adjusts effective re-run thresholds stored in MEMORY.md.

</objective>

<inputs>

- **$ARGUMENTS**: optional
  - Omitted / `all` → benchmark all agents + generate self-mentor proposals
  - `<agent-name>` → benchmark one agent + generate proposals (e.g., `sw-engineer`)
  - `/audit` or `/review` → benchmark a skill (only these two skill domains have calibration support)
  - Append `full` for 10 problems instead of 3 (e.g., `sw-engineer full`, `all full`)
  - Append `ab` to also run a `general-purpose` baseline on the same problems and report delta metrics — works for agents AND skills (e.g., `data-steward ab`, `/audit ab`, `all ab`)
  - Append `apply` to apply the proposals from the most recent run to the agent/skill files — skips benchmark (e.g., `sw-engineer apply`)

</inputs>

<constants>

- FAST_N: 3 problems per target
- FULL_N: 10 problems per target
- RECALL_THRESHOLD: 0.70 (below → agent needs instruction improvement)
- CALIBRATION_BORDERLINE: ±0.10 (|bias| within this → calibrated; between 0.10 and 0.15 → borderline)
- CALIBRATION_WARN: ±0.15 (bias beyond this → confidence decoupled from quality)
- CALIBRATE_LOG: `.claude/logs/calibrations.jsonl`
- AB_ADVANTAGE_THRESHOLD: 0.10 (delta recall or F1 above this → meaningful advantage; below → marginal or none)

Problem domain by agent:

- `sw-engineer` → Python bugs: type errors, logic errors, anti-patterns, bare `except:`, mutable defaults
- `qa-specialist` → coverage gaps: uncovered edge cases, missing exception tests, ML non-determinism
- `linting-expert` → violations: ruff rules, mypy errors, annotation gaps
- `self-mentor` → config issues: broken cross-refs, missing workflow blocks, wrong model, step gaps
- `doc-scribe` → docs gaps: missing docstrings, incomplete NumPy sections, broken examples
- `perf-optimizer` → perf issues: unnecessary loops, repeated computation, wrong dtype, missing vectorisation
- `ci-guardian` → CI issues: non-pinned action SHAs, missing cache, inefficient matrix
- `data-steward` → data issues: label leakage, split contamination, augmentation order bugs
- `ai-researcher` → paper analysis: missed contributions, wrong method attribution
- `solution-architect` → design issues: leaky abstractions, circular dependencies, missing ADR, backward-compat violations without deprecation path
- `web-explorer` → content quality: broken or unverified URLs, outdated docs, incomplete extraction from fetched pages
- `oss-maintainer` → OSS governance: incorrect SemVer decision, missing CHANGELOG entry, bad deprecation path, wrong release checklist item

Skill domains:

- `/audit` → synthetic `.claude/` config with N injected structural issues
- `/review` → synthetic Python module with N cross-domain issues (arch + tests + docs + lint)

</constants>

<workflow>

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create a new task.

## Step 1: Parse targets and create run directory

From `$ARGUMENTS`, determine:

- Target list: one agent, one skill, or all (expand "all" to the full agent list above)
- Mode: `fast` (N=3) or `full` (N=10) — default `fast`
- A/B flag: `ab` present → also spawn a `general-purpose` baseline per problem and compute delta metrics
- Fix flag: `apply` present → apply proposals only; otherwise always run benchmark + generate self-mentor proposals

**If `apply` is in `$ARGUMENTS`**: skip Steps 2–5 entirely, go directly to Step 6.

Otherwise, generate timestamp: `YYYYMMDDTHHMMSSZ` (UTC, e.g. `20260303T134448Z`). All run dirs use this timestamp.

## Step 2: Spawn per-target pipeline subagents (parallel)

Issue ALL `general-purpose` subagent spawns in a **single response** — one per target. Do not wait for one to finish before spawning the next.

Each subagent receives this self-contained prompt (substitute `<TARGET>`, `<DOMAIN>`, `<N>`, `<TIMESTAMP>`, `<MODE>`, `<AB_MODE>` before spawning — set `<AB_MODE>` to `true` or `false`):

______________________________________________________________________

You are a calibration pipeline runner for `<TARGET>`. Complete all phases in sequence.

AB mode: `<AB_MODE>` — when `true`, also run a `general-purpose` baseline on every problem and compute delta metrics.

Run dir: `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/`

### Phase 1 — Generate problems

Generate `<N>` synthetic calibration problems for `<TARGET>` targeting domain: `<DOMAIN>`.

For each problem produce a JSON object with these fields:

- `problem_id`: kebab-slug string
- `difficulty`: `"easy"`, `"medium"`, or `"hard"`
- `task_prompt`: the instruction to give the target — what to analyse (do NOT reveal the issues)
- `input`: the code / config / content inline (no file paths)
- `ground_truth`: array of objects, each with `issue` (concise description), `location` (function:line or section), and `severity` (`critical`, `high`, `medium`, or `low`)

Rules:

- Issues must be unambiguous — a domain expert would confirm them
- Cover ≥1 easy and ≥1 medium problem; hard is optional
- Each problem has 2–5 known issues; no runtime-only-detectable issues
- Return a valid JSON array only (no prose)

Write the JSON array to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/problems.json` (use Bash `mkdir -p` to create dirs).

### Phase 2 — Run target on each problem (parallel)

Spawn one `<TARGET>` named subagent per problem. Issue ALL spawns in a **single response** — no waiting between spawns.

The prompt for each subagent is exactly:

> `<task_prompt from that problem>`
>
> `<input from that problem>`
>
> End your response with a `## Confidence` block: **Score**: 0.N (high >=0.9 / moderate 0.7-0.9 / low \<0.7) and **Gaps**: what limited thoroughness.
>
> Do not self-review or refine before answering — report your initial analysis directly.

Write each subagent's full response to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/response-<problem_id>.md`.

For **skill targets** (target starts with `/`): spawn a `general-purpose` subagent with the skill's SKILL.md content prepended as context, running against the synthetic input from the problem.

### Phase 2b — Run general-purpose baseline (skip if AB_MODE is false)

Spawn one `general-purpose` subagent per problem using the **identical prompt** as Phase 2 (same task_prompt + input + Confidence instruction). Issue ALL spawns in a **single response** — no waiting between spawns.

Write each response to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/response-<problem_id>-general.md`.

### Phase 3 — Score responses in-context

Score each (problem, response) pair directly in this context — no separate scorer subagents.

For each ground truth issue: mark `true` if the target identified the same issue type at the same location (exact match or semantically equivalent description), `false` otherwise.

Extract confidence from the target's `## Confidence` block. If absent, use `0.5` and note the gap.

Count false positives: target-reported issues that have no corresponding ground truth item.

Compute per-problem:

- `recall = found_count / total_issues`
- `precision = found_count / (found_count + false_positives + 1e-9)`
- `f1 = 2·recall·precision / (recall + precision + 1e-9)`

Write all per-problem scores to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/scores.json` as a JSON array with fields: `problem_id`, `found` (array of booleans), `false_positives`, `confidence`, `recall`, `precision`, `f1`.

**If AB_MODE is true**: score each general-purpose response using identical criteria. Add to each scores.json entry: `recall_general`, `precision_general`, `f1_general`, `confidence_general`. Compute `delta_recall = recall - recall_general` and `delta_f1 = f1 - f1_general`.

### Phase 4 — Aggregate, write report and result

Compute aggregates:

- `mean_recall` = mean of all `recall` values
- `mean_confidence` = mean of all `confidence` values
- `calibration_bias` = `mean_confidence − mean_recall`
- `mean_f1` = mean of all `f1` values

Verdict:

- `|bias| < 0.10` → `calibrated`
- `0.10 ≤ |bias| ≤ 0.15` → `borderline`
- `bias > 0.15` → `overconfident`
- `bias < −0.15` → `underconfident`

Write full report to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/report.md` using this structure:

```
## Benchmark Report — <TARGET> — <date>
Mode: <MODE> | Problems: <N> | Total known issues: M

### Per-Problem Results
| Problem ID | Difficulty | Recall | Precision | Confidence | Cal. Δ |
| ...

### Aggregate
| Metric | Value | Status |
| ...

### A/B Comparison — specialized vs. general-purpose (AB mode only)
| Metric      | Specialized | General | Delta  | Verdict   |
|-------------|-------------|---------|--------|-----------|
| Mean Recall | X.XX        | X.XX    | ±X.XX  | advantage/marginal/none |
| Mean F1     | X.XX        | X.XX    | ±X.XX  |           |

Verdict: `significant` (delta_recall or delta_f1 > 0.10) / `marginal` (0.05–0.10) / `none` (<0.05)

### Systematic Gaps (missed in ≥2 problems)
...

### Improvement Signals
...
```

Write a single-line JSONL result to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/result.jsonl`:

`{"ts":"<TIMESTAMP>","target":"<TARGET>","mode":"<MODE>","mean_recall":0.N,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"problems":<N>,"verdict":"...","gaps":["..."]}`

**If AB_MODE is true**, append these fields to the same JSON line: `"delta_recall":0.N,"delta_f1":0.N,"ab_verdict":"significant|marginal|none"`

### Phase 5 — Propose instruction edits

Read the current agent/skill file:

- Agent: `.claude/agents/<TARGET>.md`
- Skill: `.claude/skills/<TARGET>/SKILL.md` (strip the leading `/` from target name)

Read `report.md` from Phase 4 — specifically the **Systematic Gaps** and **Improvement Signals** sections.

Spawn a **self-mentor** subagent with this prompt:

> You are reviewing the agent/skill file below in the context of a calibration benchmark.
>
> **Benchmark findings (from report.md):**
> [paste Systematic Gaps and Improvement Signals sections verbatim]
>
> **Current file content:**
> [paste full file content]
>
> Propose specific, minimal instruction edits that directly address each systematic gap (issues missed in ≥2/N problems) and each false-positive pattern. Be conservative: one targeted change per gap. Do not refactor sections unrelated to the findings.
>
> Format your response as:
>
> ```
> ## Proposed Changes — <TARGET>
>
> ### Change 1: <gap name>
> **File**: `.claude/agents/<TARGET>.md`
> **Section**: `<antipatterns_to_flag>` / `<workflow>` / `<notes>` / etc.
> **Current**: [exact verbatim text to replace; or "none" if inserting new content]
> **Proposed**: [exact replacement text]
> **Rationale**: one sentence — why this closes the gap
>
> [repeat for each gap — omit changes for calibrated targets with no actionable gaps]
> ```

Write the self-mentor response verbatim to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/proposal.md`.

### Return value

Return **only** this compact JSON (no prose before or after):

`{"target":"<TARGET>","mean_recall":0.N,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"verdict":"calibrated|borderline|overconfident|underconfident","gaps":["..."],"proposed_changes":N}`

If AB_MODE is true, also include: `"delta_recall":0.N,"delta_f1":0.N,"ab_verdict":"significant|marginal|none"`

______________________________________________________________________

## Step 3: Collect results and print combined report

After all pipeline subagents complete, parse the compact JSON summary from each.

Print the combined benchmark report:

```
## Calibrate — <date> — <MODE>

| Target           | Recall | Confidence | Bias   | F1   | Verdict    | Top Gap              |
|------------------|--------|-----------|--------|------|------------|----------------------|
| sw-engineer      | 0.83   | 0.85      | +0.02 ✓| 0.81 | calibrated | async error paths    |
| ...              |        |           |        |      |            |                      |
```

**If AB mode**, add two columns after F1: `ΔRecall` and `AB Verdict` — use `significant ✓`, `marginal ~`, or `none ⚠` to show whether the specialized agent earns its instruction overhead.

Flag any target where recall < 0.70 or |bias| > 0.15 with ⚠.

After the table, print the full content of each `proposal.md` for targets where `proposed_changes > 0`. Then print:

```
→ Review proposals above, then run `/calibrate <targets> apply` to apply them.
→ Proposals saved to: .claude/calibrate/runs/<TIMESTAMP>/<TARGET>/proposal.md
```

Targets with verdict `calibrated` and no proposed changes get a single line: `✓ <target> — no instruction changes needed`.

## Step 4: Concatenate JSONL logs

Append each target's result line to `.claude/logs/calibrations.jsonl` (create dir if needed):

```bash
mkdir -p .claude/logs
cat .claude/calibrate/runs/<TIMESTAMP>/*/result.jsonl >> .claude/logs/calibrations.jsonl
```

## Step 5: Surface improvement signals

For each flagged target (recall < 0.70 or |bias| > 0.15):

- **Recall < 0.70**: `→ Update <target> <antipatterns_to_flag> for: <gaps from result>`
- **Bias > 0.15**: `→ Raise effective re-run threshold for <target> in MEMORY.md (default 0.70 → ~<mean_confidence>)`
- **Bias < −0.15**: `→ <target> is conservative; threshold can stay at default`

Proposals shown in Step 3 already surface the actionable signals. End with:

`→ Run /calibrate <target> apply to apply the proposals above.`

## Step 6: Apply proposals (apply mode only)

Find the most recent run that contains `proposal.md` files:

```bash
LATEST=$(ls -td .claude/calibrate/runs/*/ 2>/dev/null | head -1)
```

For each target in the target list, check whether `$LATEST/<target>/proposal.md` exists. Collect the set of targets that have a proposal (`found`) and those that don't (`missing`).

Print `⚠ No proposal found for <target> — run /calibrate <target> first` for each missing target.

**Spawn one `general-purpose` subagent per found target. Issue ALL spawns in a single response — no waiting between spawns.**

Each subagent receives this self-contained prompt (substitute `<TARGET>`, `<PROPOSAL_PATH>`, `<AGENT_FILE>`):

______________________________________________________________________

Read the proposal file at `<PROPOSAL_PATH>` and apply each "Change N" block to `<AGENT_FILE>` (or the skill file if the target is a skill).

For each change:

1. Print: `Applying Change N to <file> [<section>]`
2. Use the Edit tool — `old_string` = **Current** text verbatim, `new_string` = **Proposed** text
3. If **Current** is `"none"` (new insertion): find the section header and insert the **Proposed** text after the last item in that block
4. Skip if **Current** text is not found verbatim → print `⚠ Skipped — current text not found`
5. Skip if **Proposed** text is already present → print `✓ Already applied — skipped`

After processing all changes return **only** this compact JSON:

`{"target":"<TARGET>","applied":N,"skipped":N}`

______________________________________________________________________

After all subagents complete, collect their JSON results and print the final summary:

```
## Fix Apply — <date>

| Target      | File                          | Applied | Skipped |
|-------------|-------------------------------|---------|---------|
| sw-engineer | .claude/agents/sw-engineer.md | 2       | 0       |

→ Run /calibrate <targets> to verify improvement.
```

</workflow>

<notes>

- **Context safety**: each target runs in its own pipeline subagent — only a compact JSON (~200 bytes) returns to the main context. `all` mode with 12 targets returns ~2.4KB total, well within context limits.
- **In-context scoring**: Phase 3 scores responses directly inside the pipeline subagent (3 responses × ~2KB = ~6KB for fast mode). No separate scorer agents needed. `full` mode (10 responses × ~2KB = ~20KB) still fits comfortably in one context.
- **Nesting depth**: main → pipeline subagent → target agent (2 levels). The pipeline subagent spawns target agents but does not nest further.
- **Quasi-ground-truth limitation**: problems are generated by Claude — the same model family as the agents under test. A truly adversarial benchmark requires expert-authored problems. This benchmark reliably catches systematic blind spots and calibration drift even with this limitation.
- **Calibration bias is the key signal**: positive bias (overconfident) → raise the agent's effective re-run threshold in MEMORY.md. Negative bias (underconfident) → confidence is conservative, no action needed. Near-zero → confidence is trustworthy.
- **Do NOT use real project files**: benchmark only against synthetic inputs — no sensitive data and real files have no ground truth.
- **Skill benchmarks** run the skill as a subagent against synthetic config or code; scored identically to agent benchmarks.
- **Improvement loop**: systematic gaps → `<antipatterns_to_flag>` | consistent low recall → consider model tier upgrade (sonnet → opus) | large calibration bias → document adjusted threshold in MEMORY.md | re-calibrate after instruction changes to quantify improvement.
- **benchmark + propose by default**: every run (except `apply`) benchmarks and generates self-mentor proposals. `apply` reads the proposals from the most recent run and applies them. The two-step design keeps a human review gate between diagnosis and mutation: you see exactly what will change before any file is touched.
- **Stale proposals**: `apply` uses verbatim text matching (`old_string` = **Current** from proposal). If the agent file was edited between the benchmark run and `apply`, any change whose **Current** text no longer matches is skipped with a warning — no silent clobbering of intermediate edits.
- Follow-up chains:
  - Recall < 0.70 or borderline → `/calibrate <agent>` → review proposals → `/calibrate <agent> apply` → `/calibrate <agent>` to verify improvement
  - Calibration bias > 0.15 → add adjusted threshold to MEMORY.md → note in next audit
  - Recommended cadence: run before and after any significant agent instruction change
- **Internal Quality Loop suppressed during benchmarking**: the Phase 2 prompt explicitly tells target agents not to self-review before answering. This ensures calibration measures raw instruction quality — not the `(agent + loop)` composite. If the loop were enabled, it would inflate both recall and confidence by an unknown ratio, masking real instruction gaps and making it impossible to attribute improvement to instruction changes vs. the loop self-correcting at inference time.
- **Skill-creator complement**: `/calibrate` benchmarks agents and skills via synthetic ground-truth problems; the official `skill-creator` from the anthropics/skills repository handles skill-level eval — trigger accuracy, A/B description testing, and description optimization. The two are complementary: run `/calibrate` for quality and recall, `skill-creator` for trigger reliability.
- **A/B mode rationale**: every specialized agent adds system-prompt tokens — if a `general-purpose` subagent matches its recall and F1, the specialization adds no value. `ab` mode quantifies this gap per-target so you can decide whether to keep, retrain, or retire an agent. `significant` (Δ>0.10) confirms the agent's domain depth earns its cost; `marginal` (0.05–0.10) suggests instruction improvements may help; `none` (\<0.05) signals the agent's current instructions add no measurable lift over a vanilla agent — consider strengthening domain-specific antipatterns and re-running. Token cost is informational (logged in scores.json) but not part of the verdict — prioritize recall/F1 delta as the primary signal.
- **AB mode nesting**: Phase 2b spawns `general-purpose` agents inside the pipeline subagent, keeping nesting at 2 levels (main → pipeline → target/general). No additional depth is added.

</notes>
