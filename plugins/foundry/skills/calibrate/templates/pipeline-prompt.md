**Re: Compress calibration pipeline runner prompt to caveman format**

You are calibration pipeline runner for `<TARGET>`. Complete all phases in sequence.

AB mode: `<AB_MODE>` — when `true`, also run `general-purpose` baseline on every problem and compute delta metrics.

Run dir: `.reports/calibrate/<TIMESTAMP>/<TARGET>/`

### Pre-flight — Codex availability

Check Codex availability once at pipeline start and set `CODEX_AVAILABLE` for use throughout all phases:

```bash
if [ -n "$CLAUDE_PLUGIN_DATA" ] && echo "$CLAUDE_PLUGIN_DATA" | grep -q 'codex-openai-codex'; then CODEX_AVAILABLE=true; else CODEX_AVAILABLE=false; fi
echo "Codex plugin: $CODEX_AVAILABLE"
```

Uses `CLAUDE_PLUGIN_DATA` env var (inherited by all subagents) not `claude plugin list` — that requires CLI allow entry and fails silently in background agents when not pre-approved.

Codex integration active only for `agents` and `skills` modes. If pipeline spawned for `routing`, `communication`, or `rules`, treat `CODEX_AVAILABLE=false` regardless of install — those modes test Claude-specific internals Codex lacks context for.

### Phase 1a — Generate problems (dual source)

**When CODEX_AVAILABLE=true**:

Split `<N>` in-scope problems between two generators. Claude always owns 1 out-of-scope problem.

| Pace | N_CLAUDE in-scope | N_CODEX in-scope | Scope (Claude) | Total |
| --- | --- | --- | --- | --- |
| fast (N=3) | 1 | 2 | 1 | 4 |
| full (N=10) | 5 | 5 | 1 | 11 |

**Step 1 — Codex generates N_CODEX in-scope problems** (runs first; writes directly to file):

Agent(subagent_type="codex:codex-rescue", prompt="Generate \<N_CODEX> synthetic calibration problems for domain: '<DOMAIN>'.

Each problem must be JSON object with these exact fields:

- problem_id: kebab-slug string, prefix with 'cx-' (e.g. 'cx-type-mismatch')
- difficulty: exactly one of: trivial, low, medium, high, extreme — include at least 1 trivial in full mode
- task_prompt: instruction to give reviewer (do NOT reveal issues)
- input: code / config / content inline (no file paths) — must contain issues
- ground_truth: array of objects, each with:
  - issue: concise description (what is wrong)
  - location: function name, section, or line reference
  - severity: exactly one of: critical, high, medium, low

Difficulty tiers:

- **trivial**: single-line, obvious issue visible at glance; any reviewer finds immediately; used in full mode only
- **low**: isolated, obvious issue; single-function scope; domain expert finds immediately
- **medium**: requires reading 2-3 related functions or sections; non-obvious but unambiguous
- **high**: requires cross-function or cross-module reasoning; subtle but detectable by reading
- **extreme**: intentionally adversarial or near-unsolvable from reading alone — use ONE of these patterns:
  1. adversarial/misleading: code looks like common anti-pattern but actually correct in context (tests false-positive discipline); issue description in ground_truth explains why it IS problem despite appearance
  2. deep cross-function control flow: issue only visible by tracing state across 4+ functions
  3. subtle concurrency or ordering bug: requires reasoning about interleaved execution or init order
  4. incomplete detectability: issue real but only partially diagnosable from reading (e.g., depends on runtime config); ground_truth includes what IS detectable statically

Distribution rules for N_CODEX problems (fast=2, full=5):

- fast (N_CODEX=2): 1 low, 1 medium/high — no extreme in fast mode
- full (N_CODEX=5): exactly 1 problem at each tier — trivial, low, medium, high, extreme

Rules:

- 2-5 known issues per problem; extreme problems may have fewer (1-3) if issue inherently hard to detect
- Issues must be unambiguous — domain expert would confirm them
- Do NOT include any out-of-scope problem — in-scope only
- Write ONLY valid JSON array (no prose, no markdown fences, no trailing commas)

Write JSON array to: .reports/calibrate/<TIMESTAMP>/<TARGET>/problems-codex.json")

**Step 2 — Claude generates N_CLAUDE in-scope problems + 1 out-of-scope problem**:

Generate `<N_CLAUDE>` in-scope problems for domain `<DOMAIN>`, plus exactly 1 out-of-scope problem, as JSON array. Fields: `problem_id` (kebab-slug), `difficulty`, `task_prompt`, `input`, `ground_truth` (array of `{issue, location, severity}`).

Difficulty tiers: same as Step 1 above.

Rules:

- Issues must be unambiguous — domain expert would confirm them
- Distribution for N_CLAUDE in-scope problems: fast (N_CLAUDE=1): 1 medium or high — no extreme in fast mode; when N_CLAUDE=1, use medium or high; full (N_CLAUDE=5): exactly 1 problem at each tier — trivial, low, medium, high, extreme
- Extreme problems may have 1–3 known issues (fewer fine if issue inherently hard to detect)
- Each non-extreme in-scope problem has 2–5 known issues; no runtime-only-detectable issues
- **Include exactly 1 out-of-scope problem** (difficulty: `"scope"`): input clearly outside agent's domain (e.g., for `linting-expert`, natural-language question; for `ci-guardian`, plain Python data script). Set `ground_truth: []`. Correct response declines, redirects, or reports no findings. Any findings = false positives (scope failure).
- Return valid JSON array only (no prose)

Write to `.reports/calibrate/<TIMESTAMP>/<TARGET>/problems-claude.json`.

**When CODEX_AVAILABLE=false**:

Claude generates all `<N>` in-scope problems + 1 out-of-scope using rules above. Write to `problems-claude.json`. Skip Codex step entirely.

### Phase 1b — Validate and merge problems

**Validate problems-codex.json** (skip if CODEX_AVAILABLE=false):

Read file. Strip any markdown code fences or prose prefix/suffix — find first `[` and match to closing `]`. Parse and validate each entry:

Required fields: `problem_id` (string starting with `cx-`), `difficulty` (one of: `trivial`/`low`/`medium`/`high`/`extreme` — NOT `scope`), `task_prompt` (non-empty string), `input` (non-empty string), `ground_truth` (non-empty array; each item has `issue`, `location`, `severity`; `severity` must be one of: `critical`/`high`/`medium`/`low`).

Reject and log any entry failing validation. If fewer than `floor(<N_CODEX> * 0.5)` valid entries remain, set `CODEX_GENERATION_FAILED=true` and proceed without Codex problems (Claude-only fallback).

**Merge and tag**:

- Tag each Codex entry: add `"source": "codex"` field
- Tag each Claude entry: add `"source": "claude"` field
- Deduplicate `problem_id`: if collision, prefix with source (`claude-<id>`, `cx-<id>`)
- Merge into single array; scope problem must appear exactly once (from Claude)

Write merged array to `.reports/calibrate/<TIMESTAMP>/<TARGET>/problems.json`.

### Phase 2 — Run target on each problem (parallel)

Spawn one `<TARGET>` named subagent per problem using **Agent tool** — never via Bash or CLI.

```bash
touch /tmp/calibrate-<TARGET>-phase2-<TIMESTAMP>
```

Issue ALL spawns in **single response** — no waiting between spawns.

Prompt for each subagent:

> `<task_prompt from that problem>`
>
> `<input from that problem>`
>
> End your response with a `## Confidence` block: **Score**: 0.N (high >=0.9 / moderate 0.8-0.9 / low \<0.8) and **Gaps**: what limited thoroughness.
>
> Do not self-review or refine before answering — report your initial analysis directly.
>
> **Write your complete response** (including the Confidence block) to `.reports/calibrate/<TIMESTAMP>/<TARGET>/response-<problem_id>.md` using the Write tool. Then end your reply with exactly one line: `Wrote: <problem_id>`

**Context discipline**: subagents write to disk and return single-line acknowledgment. Pipeline agent must NOT accumulate their full analyses in context — scorers read from disk in Phase 3. Receiving only `Wrote: <problem_id>` per agent is correct and expected.

**Phase timeout**: after 5 min of no acknowledgment, run `find .reports/calibrate/<TIMESTAMP>/<TARGET>/ -newer /tmp/calibrate-<TARGET>-phase2-<TIMESTAMP> -name "response-*.md" | wc -l` — non-zero = alive, grant one +5-min extension. Hard cutoff at 15 min of no new file activity: mark that problem as `{"timed_out": true}` in scores.json and proceed. Never block indefinitely on single response.

For **skill targets** (target starts with `/`): spawn `general-purpose` subagent with skill's SKILL.md content prepended as context, running against synthetic input from problem. Apply same write-and-acknowledge pattern.

### Phase 2b — Run general-purpose baseline (skip if AB_MODE is false)

Spawn one `general-purpose` subagent per problem using **identical prompt** as Phase 2 (same task_prompt + input + Confidence instruction), plus same write-and-acknowledge suffix pointing to `response-<problem_id>-general.md`.

```bash
touch /tmp/calibrate-<TARGET>-phase2b-<TIMESTAMP>
```

Issue ALL spawns in **single response** — no waiting between spawns.

**Phase timeout**: after 5 min, run `find .reports/calibrate/<TIMESTAMP>/<TARGET>/ -newer /tmp/calibrate-<TARGET>-phase2b-<TIMESTAMP> -name "response-*-general.md" | wc -l` — non-zero = alive, grant one +5-min extension; 15-min hard cutoff; proceed with partial baseline data if any response hangs.

### Phase 3a — Score responses via Claude scorers (parallel)

Spawn one `general-purpose` scorer subagent per problem using **Agent tool** — never via Bash or CLI. Issue ALL spawns in **single response** — no waiting between spawns.

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
> Read target response from `<RUN_DIR>/response-<PROBLEM_ID>.md`. \[If AB_MODE is true: also read `<RUN_DIR>/response-<PROBLEM_ID>-general.md`.\]
>
> For each ground truth issue: mark `true` if response identified same issue type at same location (exact match or semantically equivalent). Count false positives: reported issues with no corresponding ground truth entry. Extract confidence from `## Confidence` block (use 0.5 if absent).
>
> **For out-of-scope problems** (`ground_truth: []`): recall = N/A (skip from recall aggregate). Count all reported findings as false positives. If response declines or reports nothing, false_positives = 0 (correct scope discipline). Set severity_accuracy = N/A and format_score = N/A for this problem.
>
> **Measure response length**: count characters in target response and (if AB_MODE) general response. Token efficiency proxy — shorter = more focused.
>
> **Severity accuracy**: for each found issue (true positive), check whether response assigned same severity as ground truth. Allow ±1 tier (tiers ordered: critical > high > medium > low — "critical" vs "high" is 1-tier miss; "critical" vs "low" is 3-tier miss). Count exact-or-adjacent matches. `severity_accuracy = correct_severity / found_count` (N/A if found_count = 0). Orthogonal to recall — agent can find everything but mislabel severity.
>
> **Format score**: for each found issue (true positive), check whether response includes all three of: (a) location reference (line number, function name, or section), (b) severity or priority label, (c) fix or action suggestion. `format_score = fully_structured_count / found_count` (N/A if found_count = 0). Measures actionability of findings, not just detection.
>
> Compute: `recall = found / total` (skip if total=0), `precision = found / (found + fp + 1e-9)`, `f1 = 2·r·p / (r+p+1e-9)`.
>
> Write following JSON (no prose, no markdown fences) to `<RUN_DIR>/score-<PROBLEM_ID>-claude.json` using Write tool: `{"problem_id":"<PROBLEM_ID>","found":[true/false,...],"false_positives":N,"confidence":0.N,"recall":0.N,"precision":0.N,"f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"target_chars":N,"scorer":"claude"}`
>
> \[If AB_MODE is true, also include before closing `}`: `,"recall_general":0.N,"precision_general":0.N,"f1_general":0.N,"confidence_general":0.N,"severity_accuracy_general":0.N,"format_score_general":0.N,"general_chars":N`\]
>
> Then return ONLY one line: `Scored: <PROBLEM_ID>`

**Context discipline**: scorers write results to `score-<PROBLEM_ID>-claude.json` and return single-line acknowledgment (`Scored: <PROBLEM_ID>`). Do NOT accumulate inline JSON in pipeline context — Phase 3c reads from disk.

### Phase 3b — Score responses via Codex (skip when CODEX_AVAILABLE=false)

For each problem, spawn one Codex scoring subagent using **Agent tool** — never via Bash or CLI. Run **sequentially** (not parallel — Codex subagents share filesystem state; parallel invocations risk write conflicts).

Agent(subagent_type="codex:codex-rescue", prompt="You are scoring a calibration response against ground truth.

```
Problem ID: \<PROBLEM_ID>
Ground truth (JSON array): \<GROUND_TRUTH_JSON>
```

Read response from: .reports/calibrate/<TIMESTAMP>/<TARGET>/response-\<PROBLEM_ID>.md \[If AB_MODE is true: also read .reports/calibrate/<TIMESTAMP>/<TARGET>/response-\<PROBLEM_ID>-general.md\]

For each ground truth issue: mark true if response identified same issue type at same location (exact or semantically equivalent). Count false positives: reported issues with no ground truth match. Extract confidence from ## Confidence block (use 0.5 if absent).

For out-of-scope problems (ground_truth is []): set recall=null, all reported findings are FPs, set severity_accuracy=null, format_score=null.

```
Severity accuracy: for found issues, check severity match (allow +-1 tier; tiers: critical>high>medium>low).
Format score: for found issues, check for all three of: location reference, severity label, fix suggestion.
```

Compute: recall=found/total (null if total=0), precision=found/(found+fp+1e-9), f1=2*r*p/(r+p+1e-9).

Write ONLY this JSON (no prose, no markdown fences, no trailing commas) to file below: {"problem_id":"\<PROBLEM_ID>","found":[true/false,...],"false_positives":N,"confidence":0.N,"recall":0.N,"precision":0.N,"f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"scorer":"codex"} [If AB_MODE is true, append before closing }: ,"recall_general":0.N,"confidence_general":0.N,"precision_general":0.N,"f1_general":0.N,"severity_accuracy_general":0.N,"format_score_general":0.N]

Output file: .reports/calibrate/<TIMESTAMP>/<TARGET>/score-\<PROBLEM_ID>-codex.json")

Substitute `<PROBLEM_ID>` and `<GROUND_TRUTH_JSON>` per problem. If output file missing or unparsable after agent completes, set `scorer_mode: "single"` for that problem — Phase 3c uses Claude's score only.

### Phase 3c — Consensus merge

For each problem, read `score-<PROBLEM_ID>-claude.json` (Phase 3a output) and `score-<PROBLEM_ID>-codex.json` (Phase 3b output) from `<RUN_DIR>` and merge:

**When both scores present**:

- `found[]` — per-issue boolean: both agree → use; disagree → Claude's value (51% tiebreak)
- `false_positives` — Claude's count wins on disagreement
- `recall`, `precision`, `f1` — recomputed from consensus `found[]` and consensus `false_positives`
- `severity_accuracy`:
  - Both agree → use
  - Disagree within 1 tier → use harsher (more conservative) severity
  - Disagree by >1 tier → mark that issue as `severity_disputed`; exclude from `severity_accuracy` aggregate
- `format_score` — weighted average: 0.51 × Claude + 0.49 × Codex
- `confidence` — unchanged (from target agent's response, not scorers)
- `scorer_agreement` = (issues where both scorers agreed on found/not-found) / total_issues; N/A for scope problems
- `scorer_mode` = `"dual"`

**When only Claude score present** (Codex unavailable or failed for this problem):

Use Claude score directly; `scorer_agreement` = null; `scorer_mode` = `"single"`

**A/B mode**: apply same consensus logic independently to general-purpose baseline scores.

Write all merged scores to `.reports/calibrate/<TIMESTAMP>/<TARGET>/scores.json` as JSON array. Each entry includes `"source"` (from problems.json: `"claude"`/`"codex"`), `"scorer_mode"`, `"scorer_agreement"`, and `"severity_disputed_count"` (count of `severity_disputed` issues for this problem).

### Phase 4 — Aggregate, write report and result

Compute aggregates (exclude out-of-scope problem from recall/F1/severity/format averages; include in FP count):

- `mean_recall` = mean of `recall` values for in-scope, **non-extreme** problems only (trivial through high included; extreme excluded — reported separately as `extreme_recall`)
- `extreme_recall` = mean of `recall` values for extreme problems only (null if none present); partial performance (0.4–0.7) informative, not alarming
- `mean_confidence` = mean of all `confidence` values (extreme problems included — confidence calibration applies across all tiers)
- `calibration_bias` = `mean_confidence − mean_recall` (uses non-extreme `mean_recall`; extreme problems don't affect verdict)
- `mean_f1` = mean of `f1` values for in-scope, non-extreme problems only
- `scope_fp` = false_positives from out-of-scope problem (0 = correct discipline, >0 = scope failure)
- `mean_severity_accuracy` = mean of `severity_accuracy` for in-scope problems with found_count > 0 (exclude `severity_disputed` issues from numerator and denominator; extreme problems included if found_count > 0)
- `mean_format_score` = mean of `format_score` for in-scope problems with found_count > 0
- `token_ratio` = mean(target_chars) / mean(general_chars) across all problems — if AB_MODE, else omit
- Recall by difficulty: `recall_trivial`, `recall_low`, `recall_medium`, `recall_high`, `recall_extreme` (omit if 0 problems at that level)

**Additional aggregates (populate when applicable; use null when not)**:

- `mean_scorer_agreement` = mean `scorer_agreement` across dual-scored problems (null if all single-scored)
- `severity_disputed_count` = total issues flagged `severity_disputed` across all problems
- `codex_problems_pct` = fraction of in-scope problems with `source: "codex"` (0.0 if claude-only)
- `recall_claude_problems` = mean recall on in-scope problems where `source: "claude"` (null if none)
- `recall_codex_problems` = mean recall on in-scope problems where `source: "codex"` (null if none)
- `generator_recall_delta` = `recall_claude_problems − recall_codex_problems` (null if either is null)
- `source_mode` = `"dual"` if CODEX_AVAILABLE and generation succeeded, else `"claude-only"`
- `scoring` = `"dual"` if any problem dual-scored, else `"single"`
- `codex_generation_failed` = true if Codex generation attempted but failed, else false

Verdict:

- `|bias| < 0.10` → `calibrated`
- `0.10 ≤ |bias| ≤ 0.15` → `borderline`
- `bias > 0.15` → `overconfident`
- `bias < −0.15` → `underconfident`

Write full report to `.reports/calibrate/<TIMESTAMP>/<TARGET>/report.md` using this structure:

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

Recall by difficulty: trivial=X.XX | low=X.XX | medium=X.XX | high=X.XX (omit levels with 0 problems)
Extreme recall: X.XX (extreme problems excluded from mean_recall and verdict — partial performance 0.4–0.7 is expected)

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

Write single-line JSONL result to `.reports/calibrate/<TIMESTAMP>/<TARGET>/result.jsonl`: (one line per pipeline run — orchestrating skill concatenates these across runs into `.claude/logs/calibrations.jsonl`)

`{"ts":"<TIMESTAMP>","target":"<TARGET>","mode":"<MODE>","mean_recall":0.N,"extreme_recall":0.N_or_null,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"problems":<N>,"scope_fp":N,"verdict":"...","gaps":["..."],"source_mode":"dual|claude-only","scoring":"dual|single","scorer_agreement":0.N_or_null,"recall_trivial":0.N_or_null,"recall_low":0.N_or_null,"recall_medium":0.N_or_null,"recall_high":0.N_or_null,"recall_extreme":0.N_or_null,"recall_claude_problems":0.N_or_null,"recall_codex_problems":0.N_or_null,"generator_recall_delta":0.N_or_null,"severity_disputed_count":N,"codex_generation_failed":false}`

**If AB_MODE is true**, append these fields to same JSON line: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|none"`

### Phase 5 — Propose instruction edits

Determine target file path:

- Agent: `.claude/agents/<TARGET>.md`
- Skill: `.claude/skills/<TARGET>/SKILL.md` (strip leading `/` from target name)

Spawn **self-mentor** subagent using **Agent tool** — never via Bash or CLI. Pass only **file path** and **report path** — do NOT paste file contents into prompt; self-mentor reads files itself:

> You are reviewing calibration benchmark result and proposing instruction improvements.
>
> **Files to read** (use Read tool on each):
>
> 1. Target file: `<AGENT_OR_SKILL_FILE_PATH>`
> 2. Benchmark report: `.reports/calibrate/<TIMESTAMP>/<TARGET>/report.md` — focus on **Systematic Gaps** and **Improvement Signals** sections
>
> Propose specific, minimal instruction edits that directly address each systematic gap (issues missed in ≥2/N problems) and each false-positive pattern. Conservative: one targeted change per gap. Don't refactor sections unrelated to findings.
>
> If no actionable systematic gaps (target calibrated with recall ≥ 0.70 and no repeated misses), write: `## Proposed Changes — <TARGET>\n\nNo changes needed — target is calibrated.`
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

Write self-mentor response verbatim to `.reports/calibrate/<TIMESTAMP>/<TARGET>/proposal.md`. Ask self-mentor to end proposed changes with `## Confidence` block per CLAUDE.md output standards.

### Return value

Return **only** this compact JSON (no prose before or after):

`{"target":"<TARGET>","mean_recall":0.N,"extreme_recall":0.N_or_null,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"scope_fp":N,"verdict":"calibrated|borderline|overconfident|underconfident","gaps":["..."],"proposed_changes":N,"source_mode":"dual|claude-only","scoring":"dual|single","scorer_agreement":0.N_or_null,"generator_recall_delta":0.N_or_null}`

If AB_MODE is true, also include: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|none"`
