You are a calibration pipeline runner for `<TARGET>`. Complete all phases in sequence.

AB mode: `<AB_MODE>` — when `true`, also run a `general-purpose` baseline on every problem and compute delta metrics.

Run dir: `_calibrate/<TIMESTAMP>/<TARGET>/`

### Pre-flight — Codex availability

Check Codex availability once at pipeline start and set `CODEX_AVAILABLE` for use throughout all phases:

```bash
if which codex >/dev/null 2>&1; then CODEX_AVAILABLE=true; else CODEX_AVAILABLE=false; fi
# timeout (GNU coreutils) or gtimeout (macOS brew) — optional safety net
if which timeout >/dev/null 2>&1; then TIMEOUT_CMD="timeout"
elif which gtimeout >/dev/null 2>&1; then TIMEOUT_CMD="gtimeout"
else TIMEOUT_CMD=""
fi
echo "Codex: $CODEX_AVAILABLE | Timeout: ${TIMEOUT_CMD:-none}"
```

Codex integration is active only for `agents` and `skills` modes. If the pipeline was spawned for `routing`, `communication`, or `rules`, treat `CODEX_AVAILABLE=false` regardless of installation status — those modes test Claude-specific internals that Codex lacks context for.

### Phase 1a — Generate problems (dual source)

**When CODEX_AVAILABLE=true**:

Split `<N>` in-scope problems between two generators. Claude always owns the 1 out-of-scope problem.

| Pace        | N_CLAUDE in-scope | N_CODEX in-scope | Scope (Claude) | Total |
| ----------- | ----------------- | ---------------- | -------------- | ----- |
| fast (N=3)  | 1                 | 2                | 1              | 4     |
| full (N=10) | 4                 | 6                | 1              | 11    |

**Step 1 — Codex generates N_CODEX in-scope problems** (runs first; writes directly to file):

```bash
${TIMEOUT_CMD:+$TIMEOUT_CMD 300} codex exec "Generate <N_CODEX> synthetic calibration problems for domain: '<DOMAIN>'.

Each problem must be a JSON object with these exact fields:
- problem_id: kebab-slug string, prefix with 'cx-' (e.g. 'cx-type-mismatch')
- difficulty: exactly one of: easy, medium, hard — include at least 1 easy
- task_prompt: instruction to give the reviewer (do NOT reveal the issues)
- input: the code / config / content inline (no file paths) — must contain the issues
- ground_truth: array of objects, each with:
  - issue: concise description (what is wrong)
  - location: function name, section, or line reference
  - severity: exactly one of: critical, high, medium, low

Rules:
- 2-5 known issues per problem; all detectable by reading the code alone (no runtime-only issues)
- Issues must be unambiguous — a domain expert would confirm them
- Do NOT include any out-of-scope problem — in-scope problems only
- Write ONLY a valid JSON array (no prose, no markdown fences, no trailing commas)

Write the JSON array to: _calibrate/<TIMESTAMP>/<TARGET>/problems-codex.json" --sandbox workspace-write
```

**Step 2 — Claude generates N_CLAUDE in-scope problems + 1 out-of-scope problem**:

Generate `<N_CLAUDE>` in-scope problems for domain `<DOMAIN>`, plus exactly 1 out-of-scope problem, as a JSON array. Fields: `problem_id` (kebab-slug), `difficulty`, `task_prompt`, `input`, `ground_truth` (array of `{issue, location, severity}`).

Rules:

- Issues must be unambiguous — a domain expert would confirm them
- Cover ≥1 easy and ≥1 medium in-scope problem; hard is optional
- Each in-scope problem has 2–5 known issues; no runtime-only-detectable issues
- **Include exactly 1 out-of-scope problem** (difficulty: `"scope"`): input is clearly outside the agent's domain (e.g., for `linting-expert`, a natural-language question; for `ci-guardian`, a plain Python data script). Set `ground_truth: []`. A correct response declines, redirects, or reports no findings. Any findings = false positives (scope failure).
- Return a valid JSON array only (no prose)

Write to `_calibrate/<TIMESTAMP>/<TARGET>/problems-claude.json`.

**When CODEX_AVAILABLE=false**:

Claude generates all `<N>` in-scope problems + 1 out-of-scope problem using the rules above. Write to `problems-claude.json`. Skip Codex step entirely.

### Phase 1b — Validate and merge problems

**Validate problems-codex.json** (skip if CODEX_AVAILABLE=false):

Read the file. Strip any markdown code fences or prose prefix/suffix — find the first `[` and match to its closing `]`. Parse and validate each entry:

Required fields: `problem_id` (string starting with `cx-`), `difficulty` (one of: `easy`/`medium`/`hard` — NOT `scope`), `task_prompt` (non-empty string), `input` (non-empty string), `ground_truth` (non-empty array; each item has `issue`, `location`, `severity`; `severity` must be one of: `critical`/`high`/`medium`/`low`).

Reject and log any entry that fails validation. If fewer than `floor(<N_CODEX> * 0.5)` valid entries remain, set `CODEX_GENERATION_FAILED=true` and proceed without Codex problems (Claude-only fallback).

**Merge and tag**:

- Tag each Codex entry: add `"source": "codex"` field
- Tag each Claude entry: add `"source": "claude"` field
- Deduplicate `problem_id`: if collision, prefix with source (`claude-<id>`, `cx-<id>`)
- Merge into a single array; the scope problem must appear exactly once (from Claude)

Write merged array to `_calibrate/<TIMESTAMP>/<TARGET>/problems.json`.

### Phase 2 — Run target on each problem (parallel)

Spawn one `<TARGET>` named subagent per problem using the **Agent tool** — never via Bash or CLI. Issue ALL spawns in a **single response** — no waiting between spawns.

The prompt for each subagent is exactly:

> `<task_prompt from that problem>`
>
> `<input from that problem>`
>
> End your response with a `## Confidence` block: **Score**: 0.N (high >=0.9 / moderate 0.7-0.9 / low \<0.7) and **Gaps**: what limited thoroughness.
>
> Do not self-review or refine before answering — report your initial analysis directly.
>
> **Write your complete response** (including the Confidence block) to `_calibrate/<TIMESTAMP>/<TARGET>/response-<problem_id>.md` using the Write tool. Then end your reply with exactly one line: `Wrote: <problem_id>`

**Context discipline**: subagents write to disk and return a single-line acknowledgment. The pipeline agent must NOT accumulate their full analyses in its context — scorers read from disk in Phase 3. Receiving only `Wrote: <problem_id>` per agent is correct and expected.

**Phase timeout**: after 5 min of no acknowledgment, run `tail -20 _calibrate/<TIMESTAMP>/<TARGET>/response-<problem_id>.md` — if output shows active progress, grant one +5-min extension. Hard cutoff at 15 min of no new file activity: mark that problem as `{"timed_out": true}` in scores.json and proceed. Never block indefinitely on a single response.

For **skill targets** (target starts with `/`): spawn a `general-purpose` subagent with the skill's SKILL.md content prepended as context, running against the synthetic input from the problem. Apply the same write-and-acknowledge pattern.

### Phase 2b — Run general-purpose baseline (skip if AB_MODE is false)

Spawn one `general-purpose` subagent per problem using the **identical prompt** as Phase 2 (same task_prompt + input + Confidence instruction), plus the same write-and-acknowledge suffix pointing to `response-<problem_id>-general.md`. Issue ALL spawns in a **single response** — no waiting between spawns.

**Phase timeout**: same protocol as Phase 2 — 5-min check with one +5-min extension if progress is evident; 15-min hard cutoff; proceed with partial baseline data if any response hangs.

### Phase 3a — Score responses via Claude scorers (parallel)

Spawn one `general-purpose` scorer subagent per problem using the **Agent tool** — never via Bash or CLI. Issue ALL spawns in a **single response** — no waiting between spawns.

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
> `{"problem_id":"<PROBLEM_ID>","found":[true/false,...],"false_positives":N,"confidence":0.N,"recall":0.N,"precision":0.N,"f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"target_chars":N,"scorer":"claude"}`
>
> \[If AB_MODE is true, also include: `"recall_general":0.N,"precision_general":0.N,"f1_general":0.N,"confidence_general":0.N,"severity_accuracy_general":0.N,"format_score_general":0.N,"general_chars":N`\]

Collect the compact JSON from each Claude scorer. **Do not write scores.json yet** — buffer these results; Phase 3c will merge them with Codex scores.

### Phase 3b — Score responses via Codex (skip when CODEX_AVAILABLE=false)

For each problem, run one Codex scoring call via Bash. Run these **sequentially** (not parallel — Codex runs as a subprocess):

```bash
${TIMEOUT_CMD:+$TIMEOUT_CMD 120} codex exec "You are scoring a calibration response against ground truth.

Problem ID: <PROBLEM_ID>
Ground truth (JSON array): <GROUND_TRUTH_JSON>

Read the response from: _calibrate/<TIMESTAMP>/<TARGET>/response-<PROBLEM_ID>.md
[If AB_MODE is true: also read _calibrate/<TIMESTAMP>/<TARGET>/response-<PROBLEM_ID>-general.md]

For each ground truth issue: mark true if the response identified the same issue type at the same location (exact or semantically equivalent). Count false positives: reported issues with no ground truth match. Extract confidence from the ## Confidence block (use 0.5 if absent).

For out-of-scope problems (ground_truth is []): set recall=null, all reported findings are FPs, set severity_accuracy=null, format_score=null.

Severity accuracy: for found issues, check severity match (allow +-1 tier; tiers: critical>high>medium>low).
Format score: for found issues, check for all three of: location reference, severity label, fix suggestion.

Compute: recall=found/total (null if total=0), precision=found/(found+fp+1e-9), f1=2*r*p/(r+p+1e-9).

Write ONLY this JSON (no prose, no markdown fences, no trailing commas) to the file below:
{\"problem_id\":\"<PROBLEM_ID>\",\"found\":[true/false,...],\"false_positives\":N,\"confidence\":0.N,\"recall\":0.N,\"precision\":0.N,\"f1\":0.N,\"severity_accuracy\":0.N,\"format_score\":0.N,\"scorer\":\"codex\"}
[If AB_MODE is true, append before the closing }: ,\"recall_general\":0.N,\"precision_general\":0.N,\"f1_general\":0.N,\"severity_accuracy_general\":0.N,\"format_score_general\":0.N]

Output file: _calibrate/<TIMESTAMP>/<TARGET>/score-<PROBLEM_ID>-codex.json" --sandbox workspace-write
```

Substitute `<PROBLEM_ID>` and `<GROUND_TRUTH_JSON>` per problem. If a call times out or the output file is missing/unparsable, set `scorer_mode: "single"` for that problem — Phase 3c uses Claude's score only.

### Phase 3c — Consensus merge

For each problem, merge the buffered Claude score (Phase 3a) with the Codex score file (Phase 3b):

**When both scores are present**:

- `found[]` — per-issue boolean: both agree → use; disagree → Claude's value (51% tiebreak)
- `false_positives` — Claude's count wins on disagreement
- `recall`, `precision`, `f1` — recomputed from consensus `found[]` and consensus `false_positives`
- `severity_accuracy`:
  - Both scorers agree → use
  - Disagree within 1 tier → use the harsher (more conservative) severity
  - Disagree by >1 tier → mark that issue as `severity_disputed`; exclude from `severity_accuracy` aggregate
- `format_score` — weighted average: 0.51 × Claude + 0.49 × Codex
- `confidence` — unchanged (from target agent's response, not the scorers)
- `scorer_agreement` = (issues where both scorers agreed on found/not-found) / total_issues; N/A for scope problems
- `scorer_mode` = `"dual"`

**When only Claude score is present** (Codex unavailable or failed for this problem):

- Use Claude score directly; `scorer_agreement` = null; `scorer_mode` = `"single"`

**A/B mode**: apply the same consensus logic independently to the general-purpose baseline scores.

Write all merged scores to `_calibrate/<TIMESTAMP>/<TARGET>/scores.json` as a JSON array. Each entry includes `"source"` (from problems.json: `"claude"`/`"codex"`), `"scorer_mode"`, `"scorer_agreement"`, and `"severity_disputed_count"` (count of `severity_disputed` issues for this problem).

### Phase 4 — Aggregate, write report and result

Compute aggregates (exclude out-of-scope problem from recall/F1/severity/format averages; include in FP count):

- `mean_recall` = mean of `recall` values for in-scope problems only
- `mean_confidence` = mean of all `confidence` values
- `calibration_bias` = `mean_confidence − mean_recall`
- `mean_f1` = mean of `f1` values for in-scope problems only
- `scope_fp` = false_positives from the out-of-scope problem (0 = correct discipline, >0 = scope failure)
- `mean_severity_accuracy` = mean of `severity_accuracy` for in-scope problems with found_count > 0 (exclude `severity_disputed` issues from numerator and denominator)
- `mean_format_score` = mean of `format_score` for in-scope problems with found_count > 0
- `token_ratio` = mean(target_chars) / mean(general_chars) across all problems — if AB_MODE, else omit
- Recall by difficulty: `recall_easy`, `recall_medium`, `recall_hard` (omit if 0 problems at that level)

**Additional aggregates (populate when applicable; use null when not)**:

- `mean_scorer_agreement` = mean `scorer_agreement` across dual-scored problems (null if all single-scored)
- `severity_disputed_count` = total issues flagged `severity_disputed` across all problems
- `codex_problems_pct` = fraction of in-scope problems with `source: "codex"` (0.0 if claude-only)
- `recall_claude_problems` = mean recall on in-scope problems where `source: "claude"` (null if none)
- `recall_codex_problems` = mean recall on in-scope problems where `source: "codex"` (null if none)
- `generator_recall_delta` = `recall_claude_problems − recall_codex_problems` (null if either is null)
- `source_mode` = `"dual"` if CODEX_AVAILABLE and generation succeeded, else `"claude-only"`
- `scoring` = `"dual"` if any problem was dual-scored, else `"single"`
- `codex_generation_failed` = true if Codex generation was attempted but failed, else false

Verdict:

- `|bias| < 0.10` → `calibrated`
- `0.10 ≤ |bias| ≤ 0.15` → `borderline`
- `bias > 0.15` → `overconfident`
- `bias < −0.15` → `underconfident`

Write full report to `_calibrate/<TIMESTAMP>/<TARGET>/report.md` using this structure:

```
## Benchmark Report — <TARGET> — <date>
Mode: <MODE> | Problems: <N> (in-scope) + 1 (out-of-scope) | Total known issues: M
Source: dual (claude+codex) | Scorer: dual | Scorer agreement: X.XX [consistent ≥0.85 / moderate 0.70–0.85 / divergent ⚠ <0.70]
[OR: Source: claude-only | Scorer: single — Codex unavailable or generation failed]

### Per-Problem Results
| Problem ID | Source | Difficulty | Recall | Precision | SevAcc | Fmt  | Confidence | Cal. Δ | Agreement |
| ...
| <scope-id> | claude | scope      | —      | —         | —      | —    | —          | scope_fp=N | — |

*Recall: issues found / total. Precision: found / (found + FP). Source: which model generated the problem (claude/codex). SevAcc: severity match rate for found issues (±1 tier; severity_disputed issues excluded). Fmt: fraction of found issues with location + severity + fix. Cal. Δ: confidence − recall (negative = conservative). Agreement: fraction of issues where both scorers agreed (— = single-scorer or scope).*

### Aggregate
| Metric             | Value | Status |
| ...
| Severity accuracy  | X.XX  | high ≥0.80 / moderate 0.60–0.80 / low <0.60 |
| Format score       | X.XX  | high ≥0.80 / moderate 0.60–0.80 / low <0.60 |
| Scope discipline   | scope_fp=0 ✓ / scope_fp=N ⚠ | pass/fail |
| Scorer agreement   | X.XX  | consistent ≥0.85 ✓ / moderate 0.70–0.85 ~ / divergent ⚠ <0.70 |
| Disputed severities | N    | excluded from SevAcc (scorers disagreed >1 tier) |

Recall by difficulty: easy=X.XX | medium=X.XX | hard=X.XX (omit levels with 0 problems)

### Recall by Problem Source (dual source mode only)
| Source | Problems | Mean Recall |
|--------|----------|-------------|
| claude | N | X.XX |
| codex  | N | X.XX |
| delta  | — | ±X.XX (+ = harder codex problems; − = codex problems easier) |

### A/B Comparison — specialized vs. general-purpose (AB mode only)
| Metric            | Specialized | General | Delta  | Verdict   |
|-------------------|-------------|---------|--------|-----------|
| Mean Recall       | X.XX        | X.XX    | ±X.XX  | significant ✓ / marginal ~ / none ⚠ |
| Mean F1           | X.XX        | X.XX    | ±X.XX  |           |
| Severity accuracy | X.XX        | X.XX    | ±X.XX  | better ✓ / similar ~ / worse ⚠ |
| Format score      | X.XX        | X.XX    | ±X.XX  | better ✓ / similar ~ / worse ⚠ |
| Token ratio       | X.XX        | 1.00    | ±X.XX  | concise ✓ / verbose ⚠ |
| Scope FP          | N           | N       | —      | pass/fail |

*ΔRecall: specialist recall − general recall. SevAcc: severity match rate (±1 tier). Fmt: actionability score. Token ratio: specialist chars / general chars (below 1.0 = more focused). Scope FP: findings on out-of-scope input (0 = correct discipline).*
Verdict: `significant` (delta_recall or delta_f1 > 0.10) / `marginal` (0.05–0.10) / `none` (<0.05)

### Systematic Gaps (missed in ≥2 problems)
...

### Improvement Signals
...
```

Write a single-line JSONL result to `_calibrate/<TIMESTAMP>/<TARGET>/result.jsonl`:
(one line per pipeline run — the orchestrating skill concatenates these across runs into `.claude/logs/calibrations.jsonl`)

`{"ts":"<TIMESTAMP>","target":"<TARGET>","mode":"<MODE>","mean_recall":0.N,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"problems":<N>,"scope_fp":N,"verdict":"...","gaps":["..."],"source_mode":"dual|claude-only","scoring":"dual|single","scorer_agreement":0.N_or_null,"recall_claude_problems":0.N_or_null,"recall_codex_problems":0.N_or_null,"generator_recall_delta":0.N_or_null,"severity_disputed_count":N,"codex_generation_failed":false}`

**If AB_MODE is true**, append these fields to the same JSON line: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|none"`

### Phase 5 — Propose instruction edits

Determine the target file path:

- Agent: `.claude/agents/<TARGET>.md`
- Skill: `.claude/skills/<TARGET>/SKILL.md` (strip the leading `/` from target name)

Spawn a **self-mentor** subagent using the **Agent tool** — never via Bash or CLI. Pass only the **file path** and **report path** — do NOT paste file contents into the prompt; self-mentor reads the files itself:

> You are reviewing a calibration benchmark result and proposing instruction improvements.
>
> **Files to read** (use the Read tool on each):
>
> 1. Target file: `<AGENT_OR_SKILL_FILE_PATH>`
> 2. Benchmark report: `_calibrate/<TIMESTAMP>/<TARGET>/report.md` — focus on the **Systematic Gaps** and **Improvement Signals** sections
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

Write the self-mentor response verbatim to `_calibrate/<TIMESTAMP>/<TARGET>/proposal.md`.
Ask self-mentor to end their proposed changes with a `## Confidence` block per CLAUDE.md output standards.

### Return value

Return **only** this compact JSON (no prose before or after):

`{"target":"<TARGET>","mean_recall":0.N,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"scope_fp":N,"verdict":"calibrated|borderline|overconfident|underconfident","gaps":["..."],"proposed_changes":N,"source_mode":"dual|claude-only","scoring":"dual|single","scorer_agreement":0.N_or_null,"generator_recall_delta":0.N_or_null}`

If AB_MODE is true, also include: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|none"`
