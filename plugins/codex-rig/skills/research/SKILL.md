---
name: research
description: Research docs, papers, or state of the art; provide source-backed recommendations and caveats.
---

# Research

Source-backed research for documentation, API migration, paper, or state-of-the-art questions.

## Input Schema

```json
{
  "question": "required research question",
  "mode": "docs|sota|paper|methodology|code-fidelity",
  "constraints": [
    "optional codebase, compute, version, or implementation constraints"
  ],
  "done_when": "recommendations are source-backed with caveats and confidence"
}
```

## Workflow

### 01: Create run directory

Run `create_run.py --skill research` per `../../shared/helper-cli-contract.md`.

### 02: Define research question, mode, and constraints

Modes:

- `docs`: current API/docs/migration answer.
- `sota`: method comparison and implementation recommendation.
- `paper`: single-paper analysis.
- `methodology`: experiment design, metric, guard, and ablation review.
- `code-fidelity`: compare paper/spec claims against implementation.

### 03: Gather sources

Write `<run-directory>/sources.md`:

```markdown
| Source | Type | Date/version | Why reliable | Used for |
| --- | --- | --- | --- | --- |
```

Source rules:

- Prefer primary docs, papers, specs, release notes, code.
- Use current live sources for volatile docs, dependencies, APIs.
- Mark stale/unavailable source explicitly.
- Do not cite secondary summaries for high-impact claims unless independently corroborated.

For `sota`, `paper`, `methodology`, or `code-fidelity`, read and apply `../../shared/specialist-orchestration.md` only when independent expertise improves correctness; otherwise do not load it. Write `<run-directory>/specialist-research-plan.md` with context packs for:

- `web-explorer`: current docs, release notes, API and dependency changes.
- `scientist`: formulas, methodology, metrics, ablations, benchmark claims.
- `solution-architect`: only when user expressly requests Sol or selects that role for implementation fit, API boundaries, or migration shape; it returns bounded read-only design artifact to Terra parent/session for next action and acceptance.
- `squeezer`: performance or resource claims.
- `data-steward`: datasets, splits, leakage, reproducibility.
- `challenger`: unsupported recommendation or overconfident source synthesis.

Do not send full papers, repositories, or all search results to every specialist. Give each only source excerpts, code files, questions needed for its axis.

### 04: Map to codebase context when implementation is relevant

Inspect `python PLUGIN_ROOT/shared/collect_diff.py --help`; collect `working-tree` scope into `<run-directory>/baseline`. Run topic scan separately; record unavailable paths/collection failures as evidence gaps.

**Structural context (optional)**: for `sota`/`code-fidelity` questions naming Python module/symbol, also probe codemap-py once: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category analysis [--target <qname>] --out <run-directory>/codemap-context.json`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with codebase mapping above. Persist result once here; any specialist context pack from step 03 includes `<run-directory>/codemap-context.json`, never fresh query.

### 05: Produce `<run-directory>/research.md` with:

- `Question`
- `Constraints`
- `Source Table`
- `Findings`
- `Comparison` for SOTA/method choices
- `Implementation Fit`
- `Risks And Unknowns`
- `Recommendation`
- `Next Checks`

### 06: For paper/code-fidelity mode, include dimensions:

- `F`: formula/math match
- `H`: hyperparameter parity
- `E`: evaluation protocol
- `N`: notation and naming consistency
- `C`: citation/derivation chain

### 07: Run review gate

Run `git diff --check` as argv command. Write its combined output to `<run-directory>/review.txt` and retain its exit status as review evidence; do not erase nonzero result.

### 08: Run shared gates and write the validated result artifact

Follow `../../shared/helper-cli-contract.md` and authoritative help. For research-only work, mark lint, format, types, tests inapplicable with concrete reasons; review requires non-empty `research.md`, `sources.md`, clean diff check. Write `RESEARCH_METADATA`, validate as `research`, promote only validated candidate.

Replace explicit skip with relevant command when research includes executable validation.

## Fail-Fast Rules

1. Missing question => fail.
2. No primary sources for high-impact/current claims => fail.
3. Recommendation not tied to constraints => fail.
4. Paper/code-fidelity claim without code or source reference => fail.
5. Result artifact missing => fail.

## Quality Gates

Required:

- `review`: source table, caveats, self-review, `git diff --check`.

Conditional:

- `tests`: when research includes executable validation or code-fidelity probe.

## Calibration Hooks

On source-protocol/recommendation-policy change, update calibration:

- behavioral cases: stale docs, unsupported SOTA claim, paper-code mismatch
- benchmark patterns: `research`

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared frame with `Next steps`. `Outcome`: recommendation/support level. `Results`: exactly `Recommendation | Evidence | Decision | Caveat / next check`, one row/recommendation. Include source freshness and each gap/caveat's next check.

Minimum artifact payload template: `result-template.json`.
