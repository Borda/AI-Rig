<!-- file: vitality-codex-review.md — consumers: analyse/modes/vitality.md (Step 5 pointer, QUICK_MODE-gated) -->

## Step 5 — Codex Independent Repo Review

When `CODEX_AVAILABLE=1`: call `Skill(skill="bridge:review", args="Read <DATA_FILE> and independently assess <REPOSITORY> on activity, maintainer responsiveness, release hygiene, CI health, security, dependency health, documentation, community signals, and governance. Write the nine-axis verdict to <REVIEW_DIR>/bridge-codex-review.md with source evidence; do not read the main analysis report or apply fixes.")` to produce a parallel verdict for aggregation and divergence detection.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_DIR < "${TMPDIR:-/tmp}/vitality-review-dir-${CSID}" 2>/dev/null || REVIEW_DIR=".reports/analyse/vitality/$(date +%Y-%m-%d)-review"
IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/vitality-codex-available-${CSID}" 2>/dev/null || CODEX_AVAILABLE="0"
CODEX_REVIEW_OUT="$REVIEW_DIR/codex-repo-review.md"
mkdir -p "$REVIEW_DIR"  # timeout: 5000
```

**Bridge review instruction** (only when CODEX_AVAILABLE=1):

```text
You are performing an independent vitality assessment of {GH_OWNER}/{GH_REPO}.
Do NOT read the main analysis report. Assess the same 9 axes from raw evidence only (axes 1–9; read the weight table from $SCORING_FILE — do not use any other source for weights):

Use only this raw data: [pass all fetched API data: issue counts, PR counts, commit dates,
contributor stats, CI workflow/run data, root file list, branch protection, Dependabot status].

For each axis: assign a numeric score 0–10 and status 🟢/🟡/🔴/⚪. Provide one-sentence
evidence statement per axis. Compute overall Health Score %.

Write findings to {CODEX_REVIEW_OUT} using Write tool in this exact format:
# Codex Independent Review — {GH_OWNER}/{GH_REPO}
| Axis | Score | Status | Evidence |
|------|-------|--------|----------|
| 1 Responsiveness | N.N | 🟢/🟡/🔴 | {one sentence} |
...
| 9 Trajectory | N.N | 🟢/🟡/🔴 | {one sentence} |
| **Total Score** | **XX%** | 🟢/🟡/🔴 | — |

## Divergences
[note any axis where you expect main analysis to differ — include reasoning]

Write sentinel {REVIEW_DIR}/codex-repo-review.done on completion.
Return compact JSON only: {"status":"done","file":"{CODEX_REVIEW_OUT}","health_score":XX,"confidence":0.N}
```

**When CODEX_AVAILABLE=0**: skip step; note "codex unavailable — single-pass analysis only" in Codex Independent Review report section.

### Aggregation

After codex review completes (sentinel verified), compute per-axis delta:

```bash
# delta = abs(main_score[axis] - codex_score[axis]); flag axis "⚠ divergent" if delta >= 2.0
# aggregate health score = mean(main_health_score, codex_health_score)
```

Update report's `## Independent Codex Review` section (append via Edit tool) with:

- Codex scorecard table (from `$CODEX_REVIEW_OUT`)
- Aggregate health score
- Per-axis delta table with divergence flags
- Divergence explanations where delta ≥ 2.0

```markdown
## Independent Codex Review

Codex independently assessed the same 9 axes from raw fetched data — without reading the main analysis. Divergences ≥ 2.0 score points are flagged for human review.

**Codex Health Score:** {XX}% · **Aggregate (mean):** {XX}%

| Axis | Main | Codex | Delta | Agreement |
|------|------|-------|-------|-----------|
| 1 Responsiveness | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 2 Maintenance activity | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 3 Contributor health | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 4 Issue & PR health | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 5 CI/CD & code quality | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 6 Documentation | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 7 Governance | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 8 Security posture | N.N | N.N | ±N.N | ✓ / ⚠ divergent |
| 9 Trajectory | N.N | N.N | ±N.N | ✓ / ⚠ divergent |

### Divergences

_(Only axes with delta ≥ 2.0. If none: "Main analysis and Codex agree within 2.0 points on all axes.")_

#### Axis N — {name} (main: N.N · codex: N.N · delta: ±N.N)
**Main evidence:** {what main analysis used}
**Codex evidence:** {what codex found}
**Resolution:** {which reading is more likely correct and why — or "inconclusive, re-run recommended"}
```

Returns to `vitality.md` Step 6 (adversarial rework loop) after aggregation completes.
