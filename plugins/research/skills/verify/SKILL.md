---
name: verify
description: Paper-vs-code consistency audit. After research:scientist implements a method from a paper, verify the implementation matches paper claims across five dimensions — formula matching [F], hyperparameter parity [H], eval protocol [E], notation consistency [N], and citation chain [C]. Reads paper (PDF path / arXiv URL / pasted text), maps claims to codebase, emits verification table with match status and severity.
argument-hint: '<paper> [--scope <glob>] [--program <program.md>] [--strict] [--dim <F,H,E,N,C>]'
effort: high
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, WebSearch, WebFetch, TaskCreate, TaskUpdate, AskUserQuestion
disable-model-invocation: true
---

<objective>

Paper-vs-code consistency audit. After `research:scientist` implements a method from a paper, verify the implementation actually matches the paper claims. Audits across five dimensions — formula matching, hyperparameter parity, eval protocol, notation consistency, and citation chain. Emits verification table with match status and severity.

NOT for: running experiments (use `/research:run`); judging experimental methodology (use `/research:judge`); literature search (use `/research:topic`); general code review (use `/develop:review`). Verify audits implementation-vs-paper fidelity only — does not evaluate whether the paper's claims themselves are valid.

</objective>

<workflow>

## Agent Resolution

`research:scientist` is in the same plugin as this skill — no fallback needed if research plugin installed. Scientist handles all five audit dimensions in a single spawn to preserve cross-dimension context (e.g., a notation inconsistency explaining a formula mismatch requires holistic paper understanding).

## Verify Mode (Steps V1–V6)

Triggered by `verify <paper>` where `<paper>` is a PDF path, arXiv URL, or multi-line quoted text.

**Task tracking**: create tasks for V1, V2, V3, V4, V5, V6 at start — before any tool calls.

## Step V1: Parse paper input

**Input resolution** (priority order):

1. Path ending `.pdf` — read via Read tool (use `pages: "1-20"` for large PDFs; iterate with subsequent page ranges if needed — max 20 pages per Read call)
2. URL matching `arxiv.org` — convert `abs/<id>` to `https://arxiv.org/html/<id>` for structured content; also fetch abstract page for metadata. Use WebFetch (`timeout: 30000`).
3. URL matching `*.pdf` or `doi.org` — WebFetch (`timeout: 30000`)
4. Multi-line quoted text block — treat as literal paper content
5. No paper argument — stop: `"No paper provided. Usage: /research:verify <paper.pdf|arxiv-url|'pasted text'> [--scope <glob>]"`

From paper content, extract:

- **Header**: title, authors, year (for report)
- **Claims table**: each claim = `{id, section, claim_text, type}` where type is one of: `formula`, `hyperparameter`, `eval`, `architecture`, `result`
- Focus on: equations with concrete terms, specific hyperparameter values, evaluation protocols (metric names, split names, preprocessing steps), architectural specifics, reported numeric results

**Pre-compute run directory**:

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
RUN_DIR=".experiments/verify-$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR"  # timeout: 3000
```

## Step V2: Resolve codebase scope

**Scope resolution** (priority order):

1. `--scope <glob>` flag — use directly
2. `--program <program.md>` flag — Read the file, extract `scope_files` from `## Config` fenced block
3. Auto-detect — `Glob(pattern="**/*.py")` up to 100 files; prefer files containing ML-relevant imports (`torch`, `tensorflow`, `sklearn`, `numpy`, `jax`)

Apply `--dim` filter: if `--dim F,H` specified, only audit those dimensions. Default: all five (`F,H,E,N,C`).

## Step V3: Five-dimension audit via scientist

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")`. Single agent handles all five dimensions — cross-dimension context requires holistic paper understanding.

**Health monitoring** (CLAUDE.md section 8):

```bash
LAUNCH_AT=$(date +%s)
CHECKPOINT="/tmp/verify-check-$LAUNCH_AT"
touch "$CHECKPOINT"  # timeout: 3000
```

Poll every 5 min: `find $RUN_DIR -newer "$CHECKPOINT" -type f | wc -l` (`timeout: 5000`) — new files = alive; zero = stalled.

- **Hard cutoff: 15 min** no file activity — timed out
- **One extension (+5 min)**: if `tail -20 $RUN_DIR/audit-raw.md` shows active progress (partial content written), grant one extension; second stall = hard cutoff
- **On timeout**: read `tail -100 $RUN_DIR/audit-raw.md`; if file missing or empty, set `fidelity = null`, continue to V4 with `timed_out` status. Surface with a timeout marker in report.

**Scientist prompt**:

```markdown
Act as an ML reproducibility auditor verifying implementation fidelity against a published paper.

Paper: <title> (<year>) by <authors>
Paper content: <inline content or path to read>
Claims to verify (from V1 extraction):
<JSON claims table>

Codebase scope files:
<list of files from V2>

Active dimensions: <F,H,E,N,C or subset from --dim>

Audit the implementation against the paper across the active dimensions:

[F] Formula matching: every equation in the paper with concrete terms — does code implement the same math? Check loss functions, forward passes, normalization, gradient computations. Flag sign errors, missing terms, wrong reduction (mean vs sum).

[H] Hyperparameter parity: every hyperparameter the paper specifies (LR, batch size, weight decay, momentum, scheduler, warmup steps, dropout, hidden dim) — do code defaults match paper values? Flag divergences.

[E] Eval protocol: does the evaluation pipeline match the paper? Same metric (e.g., mAP@0.5 vs mAP@[0.5:0.95]), same test split, same preprocessing at inference, same post-processing thresholds.

[N] Notation consistency: variable names in code that map to paper notation — are they consistent? Flag confusing mappings (e.g., paper uses `alpha` for learning rate but code uses it for momentum).

[C] Citation chain: does the implementation originate from the cited paper or a derivative? If code implements a variant from a different paper, flag.

For each finding, produce:
- claim_id: from claims table
- dimension: F|H|E|N|C
- paper_reference: exact quote or equation from paper
- code_reference: file:line in codebase
- match_status: MATCH | MISMATCH | PARTIAL | UNVERIFIABLE
- severity: HIGH (would change results) | MEDIUM (affects reproducibility) | LOW (cosmetic)
- detail: one-sentence explanation

Also compute fidelity score: (MATCH + 0.5*PARTIAL) / total_verified_claims.

Write full audit to <RUN_DIR>/audit-raw.md using Write tool.
Include ## Confidence block.
Return ONLY: {"status":"done","claims_verified":N,"mismatches":N,"high":N,"medium":N,"low":N,"fidelity":0.N,"file":"<RUN_DIR>/audit-raw.md","confidence":0.N}
```

Use `timeout: 900000` for the Agent call (15-min budget).

On timeout: read `tail -100 $RUN_DIR/audit-raw.md`; if empty set `fidelity = null`, continue to V4 with `timed_out` status.

## Step V4: Severity assessment and fidelity rating

Post-process envelope from scientist:

| Fidelity score | Rating |
| --- | --- |
| >= 0.9 | HIGH fidelity |
| 0.7 -- 0.9 | MODERATE fidelity |
| < 0.7 | LOW fidelity |
| null (timed out) | TIMED OUT |

**Strict mode**: if `--strict` flag AND any HIGH severity mismatches exist in dimension F (formula) or E (eval):

```text
! BREAKING — HIGH severity mismatch in critical dimension (F or E). Fix before running experiments.
```

Then stop — do not proceed to V5/V6. Report the specific mismatches to terminal and exit.

## Step V5: Write verification report

**Pre-compute** (if not already done in V1):

```bash
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
```

Write to `.temp/output-verify-$BRANCH-$(date +%Y-%m-%d).md` via Write tool:

```markdown
## Verification Report: <paper title>

**Paper**: <title> (<year>) by <authors>
**Date**: <date>
**Fidelity**: HIGH | MODERATE | LOW (<score>) [or: TIMED OUT]
**Claims verified**: <N> (<match> match / <mismatch> mismatch / <partial> partial / <unverifiable> unverifiable)
**Dimensions**: <active dimensions>

### Verification Table

| # | Claim | Dim | Paper Reference | Code Reference | Status | Severity | Detail |
|---|-------|-----|-----------------|----------------|--------|----------|--------|
| 1 | ... | F | ... | file:line | MATCH | - | ... |
| 2 | ... | H | ... | file:line | MISMATCH | HIGH | ... |

### High-Severity Mismatches

(ordered list with specific fix instructions per mismatch — omit section if none)

1. **[claim_id] [dimension]**: <paper says X, code does Y> — fix at `file:line` by <specific change>

### Dimension Summary

| Dim | Name | Verified | Match | Mismatch | Partial | Unverifiable |
|-----|------|----------|-------|----------|---------|--------------|
| F | Formula | ... | ... | ... | ... | ... |
| H | Hyperparameter | ... | ... | ... | ... | ... |
| E | Eval protocol | ... | ... | ... | ... | ... |
| N | Notation | ... | ... | ... | ... | ... |
| C | Citation chain | ... | ... | ... | ... | ... |

### Recommended Fixes

(ordered by severity; each fix = file:line, what to change and why)

1. **HIGH** `src/model.py:42` — loss uses `mean` reduction but paper specifies `sum`; change `reduction='mean'` to `reduction='sum'`
2. **MEDIUM** `config.yaml:7` — learning rate 1e-3 but paper uses 3e-4; update default

Full audit: <RUN_DIR>/audit-raw.md

## Confidence
**Score**: 0.N
**Gaps**:
- [specific limitation]
```

## Step V6: Terminal summary

```text
---
Verify — <paper title>
Fidelity:    HIGH | MODERATE | LOW (<score>)  [or: TIMED OUT]
Claims:      <N> verified / <match> match / <mismatch> mismatch
Severity:    <N> HIGH / <N> MEDIUM / <N> LOW
Top issue:   <one-line from highest severity finding>   [or: "no mismatches found"]
-> saved to .temp/output-verify-<branch>-<date>.md
-> full audit: <RUN_DIR>/audit-raw.md
---
Next: fix mismatches, then /research:verify <paper> --scope <glob>
```

Omit "Next" line if no mismatches found.

## Notes

- Verify is read-only — never modifies code, commits, or writes to `.experiments/state/`
- `.experiments/verify-<timestamp>/` stores scientist agent's full audit output for reference
- Verify run directories don't write `result.jsonl` — exempt from automated 30-day TTL cleanup (exempt per `.claude/rules/artifact-lifecycle.md` TTL policy — no `result.jsonl` = cleanup skipped); remove manually when no longer needed (`rm -rf .experiments/verify-*/`)
- Re-run verify after fixing mismatches to confirm fixes resolved flagged items
- For papers with appendices beyond 20 pages, iterate Read with `pages: "21-40"` etc. to capture full hyperparameter tables
- Fidelity score is a ratio, not a probability — 0.9 means 90% of verified claims match, not 90% confidence

</workflow>
