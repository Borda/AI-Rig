---
name: verify
description: Paper-vs-code consistency audit. After research:scientist implements a method from a paper, verify the implementation matches paper claims across five dimensions — formula matching [F], hyperparameter parity [H], eval protocol [E], notation consistency [N], and citation chain [C]. Reads paper (PDF path / arXiv URL / pasted text), maps claims to codebase, emits verification table with match status and severity.
argument-hint: <paper> [--scope <glob>] [--program <program.md>] [--strict] [--dim <F,H,E,N,C>] [--codemap] [--no-codemap]
allowed-tools: Read, Write, Bash, Grep, Glob, Agent, WebFetch, TaskCreate, TaskUpdate, AskUserQuestion
effort: medium
disable-model-invocation: true
---

<constants>

```yaml
HARD_CUTOFF: 900  # seconds — advisory; Agent() runs in background and cannot be interrupted mid-flight
```

</constants>

<objective>

Paper-vs-code consistency audit. After `research:scientist` implements method from paper, verify implementation matches paper claims. Audits five dimensions — formula matching, hyperparameter parity, eval protocol, notation consistency, citation chain. Emits verification table with match status and severity.

NOT for: running experiments (use `/research:run`); judging experimental methodology (use `/research:judge`); literature search (use `/research:topic`); general code review (use `/develop:review` (requires `develop` plugin)). Verify audits implementation-vs-paper fidelity only — does not evaluate whether paper's claims are valid.

</objective>

<workflow>

## Agent Resolution

`research:scientist` same plugin as this skill — no fallback needed if research plugin installed. Scientist handles all five audit dimensions in single spawn to preserve cross-dimension context (e.g., notation inconsistency explaining formula mismatch needs holistic paper understanding).

## Verify Mode (Steps V1–V6)

Triggered by `verify <paper>` where `<paper>` is PDF path, arXiv URL, or multi-line quoted text.

**Task tracking**: create tasks for V1, V2, V3, V4, V5, V6 at start — before any tool calls.

### Step V1: Parse paper input

**Input resolution** (priority order):

1. Path ending `.pdf` — read via Read tool (use `pages: "1-20"` for large PDFs; iterate with subsequent page ranges if needed — max 20 pages per Read call)
2. URL matching `arxiv.org` — convert `abs/<id>` to `https://arxiv.org/pdf/<id>` for actual content fetching (e.g., `ARXIV_URL="${ARXIV_URL//arxiv.org\/abs\//arxiv.org\/pdf\/}"`). Use WebFetch (`timeout: 30000`). No separate abstract-page fetch — title/authors/year come from the PDF's first page.
3. URL matching `*.pdf` or `doi.org` — WebFetch (`timeout: 30000`)
4. Multi-line quoted text block — treat as literal paper content
5. No paper argument — stop: `"No paper provided. Usage: /research:verify <paper.pdf|arxiv-url|'pasted text'> [--scope <glob>]"`

**Pay for the paper once**: whatever the source above, write the resolved paper content to `$RUN_DIR/paper.md` (Write tool, right after the run-dir block below creates `$RUN_DIR`) — the V3 spawn prompt passes that path, never re-inlines the text and never re-fetches (a 20-page paper re-inlined is 15-25K tok billed twice).

From paper content, extract:

- **Header**: title, authors, year (for report)
- **Claims table**: each claim = `{id, section, claim_text, type}` where type is one of: `formula`, `hyperparameter`, `eval`, `architecture`, `result`
- Focus on: equations with concrete terms, specific hyperparameter values, evaluation protocols (metric names, split names, preprocessing steps), architectural specifics, reported numeric results

**Unsupported flag check**: load and follow the protocol below. Supported flags for this skill: `--scope`, `--program`, `--strict`, `--dim`, `--codemap`, `--no-codemap`.

```bash
# loads: unsupported-flag-protocol.md
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_RESEARCH_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve_shared.py" 2>/dev/null)  # timeout: 5000
[ -z "$_RESEARCH_SHARED" ] && { echo "! Plugin path resolution failed"; exit 1; }
echo "$_RESEARCH_SHARED" > "${TMPDIR:-/tmp}/research-shared-${CSID}"  # cold resolve — every later site reads this sentinel instead of re-running python
cat "$_RESEARCH_SHARED/unsupported-flag-protocol.md"
```

**Codemap auto-detection** — structural context (blast-radius, importers, coverage) for the audited codebase; on by default when codemap installed + index found. `--no-codemap` opts out; `--codemap` is strict (fail if unavailable). Note: `--codemap` is independent of `--strict` (audit strictness).

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Prints the resolved mode and writes true/false to research-verify-codemap-enabled-${CSID};
# exits 1 (already reporting `! BLOCKED`) when --codemap is strict but codemap is unavailable.
CODEMAP_RAW=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codemap-flag.py" research-verify "$ARGUMENTS") || exit 1
```

> loads: codemap-gates.md

When `CODEMAP_RAW` ≠ `off`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/codemap-gates.md"
```

Follow Gate A and Gate B.

**Pre-compute run directory** — persist `RUN_DIR` and `OUT` to temp files so V3/V4/V5 (separate Bash shells) can reload them (ADV-H20):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
DATE=$(date -u +%Y-%m-%d)  # timeout: 3000
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "verify" ".experiments" 2>/dev/null)  # timeout: 5000
mkdir -p .reports/research
BASE="verify-$BRANCH-$DATE"; OUT=".reports/research/$BASE.md"; COUNT=2; while [ -f "$OUT" ]; do OUT=".reports/research/${BASE}-${COUNT}.md"; COUNT=$((COUNT+1)); done
# persist for V3/V4/V5 (fresh shell each Bash call) — verify-latest-tag lets each shell rehydrate; epoch suffix distinguishes concurrent runs same branch/day
_VTAG="${BRANCH}-${DATE}-$(date +%s)"
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/verify-${_VTAG}-run-dir-${CSID}"
echo "$OUT"     > "${TMPDIR:-/tmp}/verify-${_VTAG}-out-${CSID}"
echo "$_VTAG"   > "${TMPDIR:-/tmp}/verify-latest-tag-${CSID}"  # pointer for rehydration blocks
```

**State-rehydration block** (paste at the top of every separate Bash invocation in V3, V4, V5):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _VTAG < "${TMPDIR:-/tmp}/verify-latest-tag-${CSID}" 2>/dev/null || _VTAG=""
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/verify-${_VTAG}-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
IFS= read -r OUT < "${TMPDIR:-/tmp}/verify-${_VTAG}-out-${CSID}" 2>/dev/null || OUT=""
# T-C1: one call reports every empty value at once. A trailing `[ -z "$X" ] && { …; }`
# guard also leaves the whole block's exit status at 1 whenever the value IS present.
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/require-vars.py" "$RUN_DIR" "verify: state files missing — V1 must run first" "$OUT" "verify: state files missing — V1 must run first" || exit 1
```

### Step V2: Resolve codebase scope

**Scope resolution** (priority order):

1. `--scope <glob>` flag — use directly
2. `--program <program.md>` flag — Read file, extract `scope_files` from `## Config` fenced block
3. Auto-detect — `Glob(pattern="**/*.py")` up to 100 files; prefer files with ML-relevant imports (`torch`, `tensorflow`, `sklearn`, `numpy`, `jax`). If Glob returns 100 files and additional `.py` files exist (i.e., total may exceed 100): print `⚠ Scope truncated at 100 files — large codebase. Fidelity score reflects verified subset only. Use --scope or --program to narrow to relevant modules.`

**Post-resolution validation** (applies to all three resolution methods above, including `--scope` and `--program`): after `scope_files` resolved, count entries:

- `len(scope_files) == 0` → print `! MISSING — Scope resolved to 0 files. Check --scope glob (typos like '**/*.pytroch' return zero matches), --program config block, or auto-detect coverage.` and stop.
- `len(scope_files) > 100` → print the truncation warning above regardless of resolution method, so user is aware fidelity reflects verified subset only.

Apply `--dim` filter: if `--dim F,H` specified, only audit those dimensions. Default: all five (`F,H,E,N,C`).

**`--dim` validation**: derive `$DIM` from the `--dim` flag (default `F,H,E,N,C` when flag absent), then validate each specified dimension token against the known set before proceeding. **Persist status to temp file** so V3 (separate Bash shell) can short-circuit when V2 failed (ADV-M25 — bash `exit 2` only terminates V2's shell, not the V3 invocation):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DIM=$(printf '%s\n' "$ARGUMENTS" | grep -oE -- '--dim [^ ]+' | head -1 | cut -d' ' -f2)
DIM="${DIM:-F,H,E,N,C}"
V2_STATUS="ok"
for _DIM_VAL in $(echo "$DIM" | tr ',' ' '); do
  case "$_DIM_VAL" in
    F|H|E|N|C) ;;
    *) echo "verify: unknown dimension: '$_DIM_VAL' — valid: F,H,E,N,C" >&2; V2_STATUS="failed" ;;
  esac
done
IFS= read -r _VTAG < "${TMPDIR:-/tmp}/verify-latest-tag-${CSID}" 2>/dev/null || _VTAG=""
echo "$V2_STATUS" > "${TMPDIR:-/tmp}/verify-${_VTAG}-v2-status-${CSID}"
[ "$V2_STATUS" = "failed" ] && exit 2
```

**V3 entry guard** (run before any V3 work — paste immediately after the state-rehydration block):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _VTAG < "${TMPDIR:-/tmp}/verify-latest-tag-${CSID}" 2>/dev/null || _VTAG=""
# Default ok = fail open: V2 may legitimately not have written a status yet. Any value other
# than `ok` closes the gate, so an unrecognised status is treated as a failure, not a pass.
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/gate-on-sentinel.py" "${TMPDIR:-/tmp}/verify-${_VTAG}-v2-status-${CSID}" ok ok "verify V3: dimension validation failed in V2 — skipping V3." || exit 1
```

### Step V3: Five-dimension audit via scientist

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

Spawn `research:scientist` via `Agent(subagent_type="research:scientist", prompt="...")`. Single agent handles all five dimensions — cross-dimension context requires holistic paper understanding.

**Codemap structural context** (only if `CODEMAP_ENABLED=true` — re-read from `${TMPDIR:-/tmp}/research-verify-codemap-enabled-${CSID}`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _RESEARCH_SHARED < "${TMPDIR:-/tmp}/research-shared-${CSID}" 2>/dev/null || _RESEARCH_SHARED=""  # warm read (Check 41)
cat "$_RESEARCH_SHARED/codemap-context.md"
```

Execute its block (leave `TARGET_MODULE`/`TARGET_FN` empty for `central` baseline, or set `TARGET_MODULE` to key module from `scope_files`). Prepend output to scientist prompt under `## Structural Context (codemap-py)` heading so architecture (N) and eval (E) dimensions reference real import/coverage structure instead of re-reading every file.

Codemap output non-empty: prepend this **codemap-first protocol** to the same heading (own copy — self-contained, no cross-plugin reference): (1) **Skill-first** — use the Structural Context above for import/caller/coverage questions before any supplementary Grep on the same target; this does NOT relax the mandatory "Read each file listed in Codebase scope files" instruction below — formula (F) and hyperparameter (H) fidelity require the actual file contents, codemap cannot substitute for that line-level comparison. (2) **Bounded call budget** — up to 5 additional `codemap-py query` calls this audit (raised from the plugin default of 3: a verify pass spans up to 100 scope files across 5 dimensions, wider surface than a single-file edit). (3) **Hard stop on `query_complete: true`** (or legacy `exhaustive: true`) — that result is final for its direction, no follow-up Grep/query to re-confirm it. Codemap output empty: omit this paragraph — scientist proceeds with the full-file-read protocol below unchanged.

<!-- Agent call runs in the background: spawn, end the turn, resume on the completion notification — never a filler call, a "waiting" line, or a sleep. HARD_CUTOFF (900s) is declared as a reference constant but is NOT enforceable within the skill — Agent() has no timeout parameter. On the notification, apply the single timeout policy declared in `<constants>`: check `$RUN_DIR/audit-raw.md`; if absent or empty, set `fidelity = null`, `status = TIMED_OUT`, mark ⏱ in report; if present, parse normally. Same limitation as research:topic. -->

**Scientist prompt** — before constructing the Agent() call, substitute the actual computed value of `$RUN_DIR` (e.g. `.experiments/verify-2026-05-13T10-00-00Z`) into every path in the prompt below; an unexpanded `$RUN_DIR` reaches the agent as literal dollar-sign text, the audit lands in a directory literally named `$RUN_DIR`, and the post-call check reports a false `TIMED_OUT`:

```markdown
Act as an ML reproducibility auditor verifying implementation fidelity against a published paper.

Paper: <title> (<year>) by <authors>
Paper content: read $RUN_DIR/paper.md with the Read tool (never re-fetch the paper from the web)
Claims to verify (from V1 extraction):
<JSON claims table>

Codebase scope files:
<list of files from V2>

Read the scope files mapped to each claim first (claim `section`/`type` names the relevant modules); do a full scope-file read-through only when the list has ≤40 files — your turn budget stalls near ~60 tool calls, and a wide scope read-all burns it before any auditing happens.

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
  - PARTIAL: use when claim is debatable — code implements a valid variant but not the exact paper formulation; include both interpretations in `detail`
- severity: HIGH (would change results) | MEDIUM (affects reproducibility) | LOW (cosmetic)
- detail: one-sentence explanation
- fix: concrete one-line fix (e.g., "change `reduction='mean'` to `reduction='sum'`" or "set `bias=False`") — required for all MISMATCH and PARTIAL findings; omit only for MATCH and UNVERIFIABLE

Also compute fidelity score: (MATCH + 0.5*PARTIAL) / total_verified_claims.

Write full audit to $RUN_DIR/audit-raw.md using Write tool.
Include ## Confidence block.
Return ONLY: {"status":"done","claims_verified":N,"mismatches":N,"high":N,"medium":N,"low":N,"fidelity":0.N,"file":"$RUN_DIR/audit-raw.md","confidence":0.N}
```

`timeout` is not a valid parameter on `Agent()` — do NOT pass it. The `HARD_CUTOFF: 900` constant is advisory only (see `<constants>`); a background `Agent()` call cannot be polled or interrupted mid-flight.

**Single timeout policy** (matches `<constants>`): after `Agent()` returns, read `$RUN_DIR/audit-raw.md`. If absent or empty → set `fidelity = null`, status = `TIMED_OUT`, continue to V4 with ⏱ marker in the report. If present → parse normally regardless of nominal budget. Never defer handling to a "next turn" or rely on context compaction.

### Step V4: Severity assessment and fidelity rating

Post-process envelope from scientist:

| Fidelity score | Rating |
| -- | -- |
| >= 0.9 | HIGH fidelity |
| 0.7 -- 0.9 | MODERATE fidelity |
| < 0.7 | LOW fidelity |
| null (timed out) | TIMED OUT |

**Strict mode**: if `--strict` flag AND any HIGH severity mismatches in dimension F (formula) or E (eval):

```text
! BREAKING — HIGH severity mismatch in critical dimension (F or E). Fix before running experiments.
```

**Do NOT write the partial report yet** — hold the partial-report markdown content in memory only. Premature writes to `$OUT` get overwritten by V5 if the user picks (b), and the (a) "keep partial report" description below is only honest if the write happens AFTER the user opts in.

Invoke `AskUserQuestion` — do NOT write options as plain text:

- question: "Strict mode hit HIGH severity mismatch — how to proceed?"
- (a) label: `Stop here` — description: write partial report (passing claims only) to `$OUT`; fix mismatches and re-run `/research:verify`
- (b) label: `Continue to full report` — description: proceed to V5/V6 and include failed claims in the full verification report

**On (a)**: the (a) branch runs after an `AskUserQuestion` turn boundary, i.e. in a fresh Bash call — so rehydrate `$OUT` self-containedly rather than assuming V1's state-rehydration block ran in this shell. `_VTAG` must come from the pointer file, never be recomputed (V1 stamps it with `$(date +%s)`; a recompute yields a different, non-existent filename):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _VTAG < "${TMPDIR:-/tmp}/verify-latest-tag-${CSID}" 2>/dev/null || _VTAG=""
IFS= read -r OUT   < "${TMPDIR:-/tmp}/verify-${_VTAG}-out-${CSID}" 2>/dev/null || OUT=""
[ -z "$OUT" ] && { echo "verify V4: report path unresolved — V1 state missing; partial report has no destination" >&2; exit 1; }
echo "$OUT"
```

The trailing `echo "$OUT"` is load-bearing: the Write tool takes a literal path and performs no shell expansion, so the resolved value must reach the transcript. Then write the held partial-report markdown to that path (verification table built so far plus a `! STRICT STOP — partial report; failed claims not yet written` banner at the top), surface the file path, and exit. Full audit remains at `$RUN_DIR/audit-raw.md`. Do NOT also dump the mismatch table to terminal — it is already inside the partial report.

**On (b)**: discard the held partial-report markdown and proceed directly to V5/V6 — V5 writes the full report to `$OUT` (failed claims included).

### Step V5: Write verification report

`$OUT` pre-computed in V1 — available here. Write to `$OUT` via Write tool (`BRANCH` and `DATE` computed in V1):

```markdown
---
Title:       Verify — [paper title]
Date:        [YYYY-MM-DD]
Scope:       [paper title] ([year]) / [code glob pattern]
Focus:       paper-to-code fidelity verification
Agents:      research:scientist (V3)
Outcome:     HIGH | MODERATE | LOW fidelity
Claims:      [N] verified / [N] match / [N] mismatch / [N] partial
Confidence:  [score] — [key gaps]
Next steps:  fix mismatches → /research:verify | proceed to /research:run <program.md>
Path:        → .reports/research/verify-<branch>-<date>.md
---

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
**Score**: 0.N — [high ≥0.9 | moderate 0.85–0.9 | low <0.85 ⚠]
**Gaps**:
- [e.g., implementation details not directly verifiable from paper alone]

**Refinements**: N passes.
```

### Step V6: Terminal summary

```text
---
Verify — <paper title>
Fidelity:    HIGH | MODERATE | LOW (<score>)  [or: TIMED OUT]
Claims:      <N> verified / <match> match / <mismatch> mismatch
Severity:    <N> HIGH / <N> MEDIUM / <N> LOW
Top issue:   <one-line from highest severity finding>   [or: "no mismatches found"]
-> saved to .reports/research/verify-<branch>-<date>.md
-> full audit: <RUN_DIR>/audit-raw.md
---
Next: fix mismatches, then /research:verify <paper> --scope <glob>
```

Omit "Next" line if no mismatches found.

Call `AskUserQuestion` tool after V6 output — do NOT write options as plain text. Before invoking, check whether `/develop:fix` is available so we can mention it as a plain-text suggestion (verify has no `Skill` tool and `/develop:fix` has `disable-model-invocation: true`, so it can never be offered as a dispatchable option):

```bash
ls ~/.claude/plugins/cache/borda-ai-rig/develop/*/skills/fix/SKILL.md >/dev/null 2>&1 && DEVELOP_FIX_AVAILABLE=true || DEVELOP_FIX_AVAILABLE=false  # timeout: 5000
echo "DEVELOP_FIX_AVAILABLE=$DEVELOP_FIX_AVAILABLE"  # the `|| ...=false` fallback makes the block exit 0 either way, so stdout is the only surviving channel
```

**Only when the block above printed `DEVELOP_FIX_AVAILABLE=true`**, print as plain text before the question: "Tip: `/develop:fix` (requires `develop` plugin) can also implement these fixes — run it manually." Printed `false` → omit the tip entirely; never emit it on the assumption the plugin is present.

- question: "What next?"
- (a) label: `fix mismatches then re-run verify` — description: fix listed mismatches and re-run `/research:verify <paper>`
- (b) label: `skip` — description: no further action

</workflow>

<notes>

- **Timeout advisory**: 900s HARD_CUTOFF is advisory only — a background `Agent()` cannot be interrupted mid-flight; on its completion notification check `$RUN_DIR/audit-raw.md`; if absent/empty → TIMED_OUT, mark ⏱.
- Verify read-only — never modifies code, commits, or writes to `.experiments/state/`
- `.experiments/verify-<timestamp>/` stores scientist agent's full audit output for reference
- Verify run dirs don't write `result.jsonl` — exempt from 30-day TTL cleanup (exempt per `.claude/rules/foundry-artifact-lifecycle.md` — no `result.jsonl` = cleanup skipped); remove manually when no longer needed (`rm -rf .experiments/verify-*/`)
- Re-run verify after fixing mismatches to confirm fixes resolved flagged items
- For papers with appendices beyond 20 pages, iterate Read with `pages: "21-40"` etc. to capture full hyperparameter tables
- Fidelity score = ratio, not probability — 0.9 means 90% of verified claims match, not 90% confidence
- **Dimension [C] (Citation chain) is best-effort** — paper provenance rarely resolvable from code alone; expect most [C] findings to come back `UNVERIFIABLE`. To skip [C] for faster, cleaner output, run with `--dim F,H,E,N`. Keep [C] with specific provenance suspicions (e.g., code may implement variant from different paper than one cited).

</notes>
