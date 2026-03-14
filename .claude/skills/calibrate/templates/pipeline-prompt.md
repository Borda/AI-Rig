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
- **Include exactly 1 out-of-scope problem** (difficulty: `"scope"`): input is clearly outside the agent's domain (e.g., for `linting-expert`, a natural-language question; for `ci-guardian`, a Python data pipeline). Set `ground_truth: []`. A correct response is declining, redirecting, or returning no findings. Any findings reported = false positives (scope failure). This tests scope discipline directly.
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
>
> **Write your complete response** (including the Confidence block) to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/response-<problem_id>.md` using the Write tool. Then end your reply with exactly one line: `Wrote: <problem_id>`

**Context discipline**: subagents write to disk and return a single-line acknowledgment. The pipeline agent must NOT accumulate their full analyses in its context — scorers read from disk in Phase 3. Receiving only `Wrote: <problem_id>` per agent is correct and expected.

**Phase timeout (PHASE_TIMEOUT_MIN = 5 min)**: if the acknowledgment line is not received within 5 minutes, mark that problem as `{"timed_out": true}` in scores.json and proceed. Never block indefinitely on a single response.

For **skill targets** (target starts with `/`): spawn a `general-purpose` subagent with the skill's SKILL.md content prepended as context, running against the synthetic input from the problem. Apply the same write-and-acknowledge pattern.

### Phase 2b — Run general-purpose baseline (skip if AB_MODE is false)

Spawn one `general-purpose` subagent per problem using the **identical prompt** as Phase 2 (same task_prompt + input + Confidence instruction), plus the same write-and-acknowledge suffix pointing to `response-<problem_id>-general.md`. Issue ALL spawns in a **single response** — no waiting between spawns.

**Phase timeout**: same 5-minute budget applies — proceed with partial baseline data if any response hangs.

### Phase 3 — Score responses (parallel scorer subagents)

Spawn one `general-purpose` scorer subagent per problem. Issue ALL spawns in a **single response** — no waiting between spawns.

Each scorer receives this prompt (substitute `<PROBLEM_ID>`, `<GROUND_TRUTH_JSON>`, `<RUN_DIR>`, `<AB_MODE>`):

> You are scoring agent responses against calibration ground truth.
>
> **Problem ID**: `<PROBLEM_ID>`
>
> **Ground truth** (JSON array — each entry has `issue`, `location`, `severity`):
>
> ```text
> <GROUND_TRUTH_JSON>
> ```
>
> Read the target response from `<RUN_DIR>/response-<PROBLEM_ID>.md`.
> \[If AB_MODE is true: also read `<RUN_DIR>/response-<PROBLEM_ID>-general.md`.\]
>
> For each ground truth issue: mark `true` if the response identified the same issue type at the same location (exact match or semantically equivalent). Count false positives: reported issues with no corresponding ground truth entry. Extract confidence from the `## Confidence` block (use 0.5 if absent).
>
> **For out-of-scope problems** (`ground_truth: []`): recall = N/A (skip from recall aggregate). Count all reported findings as false positives. If the response declines or reports nothing, false_positives = 0 (correct scope discipline). Set severity_accuracy = N/A and format_score = N/A for this problem.
>
> **Measure response length**: count the number of characters in the target response and (if AB_MODE) the general response. This is a token efficiency proxy — shorter is more focused.
>
> **Severity accuracy**: for each found issue (true positive), check whether the response assigned the same severity as ground truth. Allow ±1 tier (tiers ordered: critical > high > medium > low — "critical" vs "high" is a 1-tier miss; "critical" vs "low" is a 3-tier miss). Count exact-or-adjacent matches. `severity_accuracy = correct_severity / found_count` (N/A if found_count = 0). This is orthogonal to recall — an agent can find everything but mislabel severity.
>
> **Format score**: for each found issue (true positive), check whether the response includes all three of: (a) a location reference (line number, function name, or section), (b) a severity or priority label, (c) a fix or action suggestion. `format_score = fully_structured_count / found_count` (N/A if found_count = 0). Measures actionability of findings, not just whether the issue was detected.
>
> Compute: `recall = found / total` (skip if total=0), `precision = found / (found + fp + 1e-9)`, `f1 = 2·r·p / (r+p+1e-9)`.
>
> Return **only** this JSON (no prose):
> `{"problem_id":"<PROBLEM_ID>","found":[true/false,...],"false_positives":N,"confidence":0.N,"recall":0.N,"precision":0.N,"f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"target_chars":N}`
>
> \[If AB_MODE is true, also include: `"recall_general":0.N,"precision_general":0.N,"f1_general":0.N,"confidence_general":0.N,"severity_accuracy_general":0.N,"format_score_general":0.N,"general_chars":N`\]

Collect the compact JSON from each scorer (each ~200 bytes). Write all to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/scores.json` as a JSON array.

### Phase 4 — Aggregate, write report and result

Compute aggregates (exclude out-of-scope problem from recall/F1/severity/format averages; include in False Positive (FP) count):

- `mean_recall` = mean of `recall` values for in-scope problems only
- `mean_confidence` = mean of all `confidence` values
- `calibration_bias` = `mean_confidence − mean_recall`
- `mean_f1` = mean of `f1` values for in-scope problems only
- `scope_fp` = false_positives from the out-of-scope problem (0 = correct discipline, >0 = scope failure)
- `mean_severity_accuracy` = mean of `severity_accuracy` values for in-scope problems with found_count > 0 (omit if no found issues)
- `mean_format_score` = mean of `format_score` values for in-scope problems with found_count > 0
- `token_ratio` = mean(target_chars) / mean(general_chars) across all problems — if AB_MODE, else omit (ratio < 1.0 = specialist more concise)
- **Recall by difficulty**: `recall_easy`, `recall_medium`, `recall_hard` — mean recall for in-scope problems at each difficulty level (omit if fewer than 1 problem at that level). Not a separate metric — surfaced in report display only to show where the agent struggles.

Verdict:

- `|bias| < 0.10` → `calibrated`
- `0.10 ≤ |bias| ≤ 0.15` → `borderline`
- `bias > 0.15` → `overconfident`
- `bias < −0.15` → `underconfident`

Write full report to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/report.md` using this structure:

```
## Benchmark Report — <TARGET> — <date>
Mode: <MODE> | Problems: <N> (in-scope) + 1 (out-of-scope) | Total known issues: M

### Per-Problem Results
| Problem ID | Difficulty | Recall | Precision | SevAcc | Fmt  | Confidence | Cal. Δ |
| ...
| <scope-id> | scope      | —      | —         | —      | —    | —          | scope_fp=N |

*Recall: issues found / total. Precision: found / (found + FP). SevAcc: severity match rate for found issues (±1 tier). Fmt: fraction of found issues with location + severity + fix. Cal. Δ: confidence − recall (negative = conservative).*

### Aggregate
| Metric            | Value | Status |
| ...
| Severity accuracy | X.XX  | high ≥0.80 / moderate 0.60–0.80 / low <0.60 |
| Format score      | X.XX  | high ≥0.80 / moderate 0.60–0.80 / low <0.60 |
| Scope discipline  | scope_fp=0 ✓ / scope_fp=N ⚠ | pass/fail |

Recall by difficulty: easy=X.XX | medium=X.XX | hard=X.XX (omit levels with 0 problems)

### A/B Comparison — specialized vs. general-purpose (AB mode only)
| Metric            | Specialized | General | Delta  | Verdict   |
|-------------------|-------------|---------|--------|-----------|
| Mean Recall       | X.XX        | X.XX    | ±X.XX  | significant ✓ / marginal ~ / none ⚠ |
| Mean F1           | X.XX        | X.XX    | ±X.XX  |           |
| Severity accuracy | X.XX        | X.XX    | ±X.XX  | better ✓ / similar ~ / worse ⚠ |
| Format score      | X.XX        | X.XX    | ±X.XX  | better ✓ / similar ~ / worse ⚠ |
| Token ratio       | X.XX        | 1.00    | ±X.XX  | concise ✓ / verbose ⚠ |
| Scope FP          | N           | N       | —      | pass/fail |

*ΔRecall: specialist recall − general recall. SevAcc: correct severity assignment rate (±1 tier) — independent of recall; high recall with low SevAcc means issues found but misprioritized. Fmt: fraction of findings with location + severity + fix — measures actionability, not just detection. Token ratio: specialist chars / general chars (below 1.0 = more focused). Scope FP: findings on out-of-scope input (0 = correct discipline).*
Verdict: `significant` (delta_recall or delta_f1 > 0.10) / `marginal` (0.05–0.10) / `none` (<0.05)

### Systematic Gaps (missed in ≥2 problems)
...

### Improvement Signals
...
```

Write a single-line JSONL result to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/result.jsonl`:

`{"ts":"<TIMESTAMP>","target":"<TARGET>","mode":"<MODE>","mean_recall":0.N,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"problems":<N>,"scope_fp":N,"verdict":"...","gaps":["..."]}`

**If AB_MODE is true**, append these fields to the same JSON line: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|none"`

### Phase 5 — Propose instruction edits

Determine the target file path:

- Agent: `.claude/agents/<TARGET>.md`
- Skill: `.claude/skills/<TARGET>/SKILL.md` (strip the leading `/` from target name)

Spawn a **self-mentor** subagent. Pass only the **file path** and **report path** — do NOT paste file contents into the prompt; self-mentor reads the files itself:

> You are reviewing a calibration benchmark result and proposing instruction improvements.
>
> **Files to read** (use the Read tool on each):
>
> 1. Target file: `<AGENT_OR_SKILL_FILE_PATH>`
> 2. Benchmark report: `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/report.md` — focus on the **Systematic Gaps** and **Improvement Signals** sections
>
> Propose specific, minimal instruction edits that directly address each systematic gap (issues missed in ≥2/N problems) and each false-positive pattern. Be conservative: one targeted change per gap. Do not refactor sections unrelated to the findings.
>
> If there are no actionable systematic gaps (target is calibrated with recall ≥ 0.70 and no repeated misses), write: `## Proposed Changes — <TARGET>\n\nNo changes needed — target is calibrated.`
>
> Otherwise format each change as:
>
> ```
> ## Proposed Changes — <TARGET>
>
> ### Change 1: <gap name>
> **File**: `<file path>`
> **Section**: `<antipatterns_to_flag>` / `<workflow>` / `<notes>` / etc.
> **Current**: [exact verbatim text to replace; or "none" if inserting new content]
> **Proposed**: [exact replacement text]
> **Rationale**: one sentence — why this closes the gap
> ```

Write the self-mentor response verbatim to `.claude/calibrate/runs/<TIMESTAMP>/<TARGET>/proposal.md`.

### Return value

Return **only** this compact JSON (no prose before or after):

`{"target":"<TARGET>","mean_recall":0.N,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"scope_fp":N,"verdict":"calibrated|borderline|overconfident|underconfident","gaps":["..."],"proposed_changes":N}`

If AB_MODE is true, also include: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|none"`
